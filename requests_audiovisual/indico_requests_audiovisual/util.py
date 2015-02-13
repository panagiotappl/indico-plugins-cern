from __future__ import unicode_literals

import json
from itertools import chain

import requests
from requests import RequestException

from indico.core.db.util import run_after_commit
from indico.modules.rb.models.equipment import EquipmentType
from indico.modules.rb.models.locations import Location
from indico.modules.rb.models.rooms import Room
from indico.util.user import retrieve_principals
from MaKaC.conference import SubContribution
from MaKaC.webinterface.common.contribFilters import PosterFilterField

from indico_requests_audiovisual import SERVICES


def is_av_manager(user):
    """Checks if a user is an AV manager"""
    from indico_requests_audiovisual.plugin import AVRequestsPlugin
    principals = retrieve_principals(AVRequestsPlugin.settings.get('managers'))
    return any(principal.containsUser(user) for principal in principals)


def get_av_capable_rooms():
    """Returns a list of rooms with AV equipment"""
    eq_types = EquipmentType.find_all(EquipmentType.name == 'Webcast/Recording', Location.name == 'CERN',
                                      _join=EquipmentType.location)
    return set(Room.find_with_filters({'available_equipment': eq_types}))


def _contrib_key(contrib):
    # key function to sort contributions and their subcontributions properly
    is_subcontrib = isinstance(contrib, SubContribution)
    return (contrib.getContribution().startDate,
            contrib.getContribution().id,
            is_subcontrib,
            (contrib.getContribution().getSubContributionList().index(contrib) if is_subcontrib else None),
            contrib.getTitle())


def get_contributions(event):
    """Returns a list of contributions in rooms with AV equipment

    :return: a list of ``(contribution, capable, custom_room)`` tuples
    """
    from indico_requests_audiovisual.plugin import AVRequestsPlugin
    not_poster = PosterFilterField(event, False, False)
    contribs = [cont for cont in event.getContributionList() if not_poster.satisfies(cont)]
    if AVRequestsPlugin.settings.get('allow_subcontributions'):
        contribs.extend(list(chain.from_iterable(cont.getSubContributionList() for cont in contribs)))
    contribs = sorted(contribs, key=_contrib_key)
    av_capable_rooms = {r.name for r in get_av_capable_rooms()}
    event_room = event.getRoom() and event.getRoom().getName()
    return [(c,
             (c.getLocation() and c.getLocation().getName() == 'CERN' and
              c.getRoom() and c.getRoom().getName() in av_capable_rooms),
             (c.getRoom().getName() if c.getRoom() and c.getRoom().getName() != event_room else None))
            for c in contribs]


def contribution_id(contrib_or_subcontrib):
    """Returns an ID for the contribution/subcontribution"""
    if isinstance(contrib_or_subcontrib, SubContribution):
        return '{}-{}'.format(contrib_or_subcontrib.getContribution().id, contrib_or_subcontrib.id)
    else:
        return contrib_or_subcontrib.id


def get_selected_contributions(req):
    """Gets the selected contributions for a request.

    :return: list of ``(contribution, capable, custom_room)`` tuples
    """
    if req.event.getType() == 'simple_event':
        return []
    contributions = get_contributions(req.event)
    if not req.data.get('all_contributions', True):
        selected = set(req.data['contributions'])
        contributions = [x for x in contributions if contribution_id(x[0]) in selected]
    return contributions


def get_selected_services(req):
    """Gets the selected services

    :return: list of service names
    """
    return [SERVICES.get(s, s) for s in req.data['services']]


def has_capable_contributions(event):
    """Checks if there are any contributions in AV-capable rooms"""
    if event.getType() == 'simple_event':
        av_capable_rooms = {r.name for r in get_av_capable_rooms()}
        return event.getRoom() and event.getRoom().getName() in av_capable_rooms
    else:
        return any(capable for _, capable, _ in get_contributions(event))


def has_any_contributions(event):
    """Checks if there are any contributions in the event"""
    if event.getType() == 'simple_event':
        # a lecture is basically a contribution on its own
        return True
    else:
        return bool(get_contributions(event))


def _get_location_tuple(obj):
    location = obj.getLocation().getName() if obj.getLocation() else None
    room = obj.getRoom().getName() if obj.getRoom() else None
    return location, room


def _get_date_tuple(obj):
    if not hasattr(obj, 'getStartDate') or not hasattr(obj, 'getEndDate'):
        # subcontributions don't have dates
        return None
    return obj.getStartDate().isoformat(), obj.getEndDate().isoformat()


def get_data_identifiers(req):
    """Returns identifiers to determine if relevant data changed.

    Only the event and selected contributions are taken into account.
    While the event date/location doesn't really matter since we already
    check all the contribution dates/locations, we still keep it since a
    location change of the main event could still be relevant to the AV team.

    :return: a dict containing `dates` and `locations`
    """
    event = req.event
    location_identifiers = {}
    date_identifiers = {}
    for obj in [event] + [x[0] for x in get_selected_contributions(req)]:
        obj_id = type(obj).__name__, obj.id
        date_identifiers[obj_id] = _get_date_tuple(obj)
        location_identifiers[obj_id] = _get_location_tuple(obj)
    # we do a json cycle here so we have something that can be compared with data
    # coming from a json storage later. for example, we need lists instead of tuples
    return json.loads(json.dumps({
        'dates': sorted(date_identifiers.items()),
        'locations': sorted(location_identifiers.items())
    }))


@run_after_commit  # otherwise the remote side might read old data
def send_webcast_ping():
    """Sends a ping notification when a webcast request changes"""
    from indico_requests_audiovisual.plugin import AVRequestsPlugin
    url = AVRequestsPlugin.settings.get('webcast_ping_url')
    if not url:
        return
    AVRequestsPlugin.logger.info('Sending webcast ping to {}'.format(url))
    try:
        response = requests.get(url, timeout=10, verify=False)
        response.raise_for_status()
    except RequestException:
        AVRequestsPlugin.logger.exception('Could not send webcast ping')