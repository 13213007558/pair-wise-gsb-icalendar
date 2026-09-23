"""Test proximity-triggered VALARM computation.

RFC 9074 extends VALARM with a PROXIMITY property so alarms fire when the
user arrives at or departs from a location instead of at a fixed time.

See https://www.rfc-editor.org/rfc/rfc9074.html
"""

from datetime import datetime, timezone

import pytest

from icalendar import Alarms, Event, LocationEvent, Proximity, ProximityTrigger
from icalendar.error import InvalidCalendar, LocalTimezoneMissing

UTC = timezone.utc

# The meeting room is at 40.443, -79.945 with a 100 m radius.
ROOM_LAT = 40.443
ROOM_LON = -79.945
# About 1.1 km north of the room: safely outside the radius.
FAR_LAT = 40.453
FAR_LON = -79.945


def at(hour, minute):
    """A timestamp on the day of the meeting."""
    return datetime(2021, 3, 2, hour, minute, tzinfo=UTC)


def sample(hour, minute, latitude, longitude):
    """A location sample on the day of the meeting."""
    return LocationEvent(at(hour, minute), latitude, longitude)


def walk_samples():
    """Simulate entering, dwelling, leaving and re-entering the room."""
    return [
        sample(9, 45, FAR_LAT, FAR_LON),  # outside
        sample(9, 50, ROOM_LAT, ROOM_LON),  # enter
        sample(9, 55, ROOM_LAT, ROOM_LON),  # dwell
        sample(10, 5, FAR_LAT, FAR_LON),  # leave
        sample(10, 10, ROOM_LAT, ROOM_LON),  # re-enter
    ]


def event_ics(
    event_lines="",
    alarm_lines="",
    proximity="ARRIVE",
    vlocation_lines=None,
    dtstart="DTSTART:20210302T103000Z",
):
    """Build a VEVENT with one proximity VALARM."""
    if vlocation_lines is None:
        vlocation_lines = [
            "BEGIN:VLOCATION",
            "UID:location-1",
            "NAME:Meeting room 1",
            "URL:geo:40.443,-79.945;u=100",
            "END:VLOCATION",
        ]
    lines = [
        "BEGIN:VEVENT",
        "UID:proximity-event-1",
        "DTSTAMP:20210301T151004Z",
        dtstart,
        "DTEND:20210302T113000Z",
        "SUMMARY:Meeting",
        "LOCATION:Meeting room 1",
        *event_lines.splitlines(),
        "BEGIN:VALARM",
        "UID:alarm-1",
        "ACTION:DISPLAY",
        "TRIGGER:-PT15M",
        "DESCRIPTION:You are close to the meeting room",
        "REPEAT:2",
        "DURATION:PT5M",
        f"PROXIMITY:{proximity}",
        *alarm_lines.splitlines(),
        *vlocation_lines,
        "END:VALARM",
        "END:VEVENT",
    ]
    return "\n".join(line for line in lines if line)


@pytest.fixture()
def alarms():
    """Alarms of a meeting with an ARRIVE proximity alarm."""
    return Alarms(Event.from_ical(event_ics()))


def test_enter_dwell_leave_reenter(alarms):
    """Each geofence crossing triggers the alarm with all its repetitions."""
    triggers = alarms.proximity_times(walk_samples(), now=at(11, 0))
    assert len(triggers) == 6
    assert [t.trigger for t in triggers] == [
        at(9, 50),
        at(9, 55),
        at(10, 0),
        at(10, 10),
        at(10, 15),
        at(10, 20),
    ]
    assert [t.repetition for t in triggers] == [0, 1, 2, 0, 1, 2]
    assert all(isinstance(t, ProximityTrigger) for t in triggers)
    assert all(not t.pending for t in triggers)


def test_trigger_records_explain_their_origin(alarms):
    """Each record states the location event, the repetition and the rule."""
    triggers = alarms.proximity_times(walk_samples(), now=at(11, 0))
    first = triggers[0]
    assert first.location_event == sample(9, 50, ROOM_LAT, ROOM_LON)
    assert first.repetition == 0
    assert "ARRIVE" in first.rule
    assert "Meeting room 1" in first.rule
    assert first.parent is not None
    assert first.alarm.uid == "alarm-1"
    explanation = first.explain()
    assert "repetition 0" in explanation
    assert "2021-03-02T09:50:00+00:00" in explanation
    reenter = triggers[3]
    assert reenter.location_event == sample(10, 10, ROOM_LAT, ROOM_LON)


def test_pending_repetitions_use_the_reference_time(alarms):
    """Repetitions after `now` are marked as pending."""
    triggers = alarms.proximity_times(walk_samples(), now=at(9, 57))
    assert [t.pending for t in triggers] == [False, False, True, True, True, True]


def test_depart_direction():
    """A DEPART alarm fires when leaving the radius, not when entering."""
    alarms = Alarms(Event.from_ical(event_ics(proximity="DEPART")))
    triggers = alarms.proximity_times(walk_samples(), now=at(11, 0))
    assert [t.trigger for t in triggers] == [at(10, 5), at(10, 10), at(10, 15)]
    assert all("DEPART" in t.rule for t in triggers)


def test_first_sample_inside_does_not_trigger(alarms):
    """The first sample only establishes the state, no arrival is observed."""
    triggers = alarms.proximity_times(
        [sample(9, 50, ROOM_LAT, ROOM_LON), sample(9, 55, ROOM_LAT, ROOM_LON)],
        now=at(11, 0),
    )
    assert triggers == []


def test_acknowledged_alarms_do_not_retrigger():
    """Occurrences at or before ACKNOWLEDGED stay silent but explainable."""
    alarms = Alarms(
        Event.from_ical(event_ics(alarm_lines="ACKNOWLEDGED:20210302T095700Z"))
    )
    triggers = alarms.proximity_times(walk_samples(), now=at(11, 0))
    assert len(triggers) == 6  # acknowledged records are still reported
    active = [t for t in triggers if t.is_active()]
    assert [t.trigger for t in active] == [
        at(10, 0),
        at(10, 10),
        at(10, 15),
        at(10, 20),
    ]
    acknowledged = [t for t in triggers if not t.is_active()]
    assert [t.trigger for t in acknowledged] == [at(9, 50), at(9, 55)]
    assert all(t.acknowledged == at(9, 57) for t in acknowledged)


def test_out_of_order_location_events_do_not_regress():
    """Shuffled samples produce the same records and the same ACK boundary."""
    alarms = Alarms(
        Event.from_ical(event_ics(alarm_lines="ACKNOWLEDGED:20210302T095700Z"))
    )
    ordered = alarms.proximity_times(walk_samples(), now=at(11, 0))
    shuffled = alarms.proximity_times(list(reversed(walk_samples())), now=at(11, 0))
    assert [t.trigger for t in ordered] == [t.trigger for t in shuffled]
    assert [t.is_active() for t in ordered] == [t.is_active() for t in shuffled]
    assert [t.repetition for t in ordered] == [t.repetition for t in shuffled]


def test_cancelled_event_does_not_trigger():
    """A cancelled event produces no proximity triggers."""
    alarms = Alarms(Event.from_ical(event_ics(event_lines="STATUS:CANCELLED")))
    assert alarms.proximity_times(walk_samples(), now=at(11, 0)) == []


def test_missing_location_is_an_error():
    """A proximity alarm without a VLOCATION has a clear result."""
    alarms = Alarms(Event.from_ical(event_ics(vlocation_lines=[])))
    with pytest.raises(InvalidCalendar, match="VLOCATION"):
        alarms.proximity_times(walk_samples(), now=at(11, 0))


@pytest.mark.parametrize(
    "url",
    [
        "geo:40.443,-79.945",  # no radius at all
        "geo:40.443,-79.945;u=0",  # zero radius
        "geo:40.443,-79.945;u=-5",  # negative radius
        "geo:40.443,-79.945;u=abc",  # not a number
        "https://example.com/map",  # not a geo URI
    ],
)
def test_invalid_radius_is_an_error(url):
    """An illegal radius must not silently trigger."""
    alarms = Alarms(
        Event.from_ical(
            event_ics(
                vlocation_lines=[
                    "BEGIN:VLOCATION",
                    "UID:location-1",
                    f"URL:{url}",
                    "END:VLOCATION",
                ]
            )
        )
    )
    with pytest.raises(InvalidCalendar):
        alarms.proximity_times(walk_samples(), now=at(11, 0))


def test_unknown_proximity_value_is_an_error():
    """Only ARRIVE and DEPART are defined by RFC 9074."""
    alarms = Alarms(Event.from_ical(event_ics(proximity="TELEPORT")))
    with pytest.raises(InvalidCalendar, match="TELEPORT"):
        alarms.proximity_times(walk_samples(), now=at(11, 0))


def test_cross_timezone_dtstart():
    """A DTSTART in another timezone does not break proximity computation."""
    # NB: do not use America/New_York or Europe/Berlin here. The doctests of
    # other modules assert on their ZoneInfo repr and tzp caches timezones.
    alarms = Alarms(
        Event.from_ical(event_ics(dtstart="DTSTART;TZID=Asia/Shanghai:20210302T103000"))
    )
    triggers = alarms.proximity_times(walk_samples(), now=at(11, 0))
    assert len(triggers) == 6
    assert triggers[0].trigger == at(9, 50)
    assert all(t.trigger.tzinfo is not None for t in triggers)
    # the time-based path still resolves the relative trigger in the event's timezone
    assert len(alarms.times) == 3


def test_relative_time_alarms_still_use_the_old_path(alarms):
    """DTSTART/DTEND relative alarms are computed as before."""
    times = alarms.times
    assert [t.trigger for t in times] == [at(10, 15), at(10, 20), at(10, 25)]


def test_time_based_alarm_has_no_proximity_triggers():
    """An alarm without PROXIMITY is not location-triggered."""
    event = Event.from_ical(event_ics().replace("PROXIMITY:ARRIVE\n", ""))
    alarms = Alarms(event)
    assert alarms.proximity_times(walk_samples(), now=at(11, 0)) == []
    assert len(alarms.times) == 3


def test_empty_location_stream(alarms):
    """No samples, no triggers."""
    assert alarms.proximity_times([], now=at(11, 0)) == []


def test_naive_location_timestamp_is_an_error():
    """Location samples must be timezone-aware to be ordered reliably."""
    with pytest.raises(LocalTimezoneMissing):
        LocationEvent(datetime(2021, 3, 2, 9, 50), ROOM_LAT, ROOM_LON)


def test_snooze_delays_the_proximity_trigger():
    """A snoozed alarm fires at the snooze time, not the crossing time."""
    alarms = Alarms(
        Event.from_ical(event_ics(event_lines="X-MOZ-SNOOZE-TIME:20210302T103000Z"))
    )
    triggers = alarms.proximity_times(walk_samples(), now=at(11, 0))
    assert triggers[0].trigger == at(10, 30)
    assert triggers[0].location_event == sample(9, 50, ROOM_LAT, ROOM_LON)


def test_serialization_roundtrip_preserves_triggers_and_ack_boundary():
    """After to_ical/from_ical, trigger order and ACK boundary are identical."""
    event = Event.from_ical(event_ics(alarm_lines="ACKNOWLEDGED:20210302T095700Z"))
    event["X-CUSTOM-EXTENSION"] = "preserved-value"
    event.subcomponents[0]["X-TEAM-CHANNEL"] = "ops"
    reparsed = Event.from_ical(event.to_ical().decode())

    assert reparsed["X-CUSTOM-EXTENSION"] == "preserved-value"
    alarm = next(c for c in reparsed.walk("VALARM"))
    assert alarm["X-TEAM-CHANNEL"] == "ops"

    before = Alarms(event).proximity_times(walk_samples(), now=at(11, 0))
    after = Alarms(reparsed).proximity_times(walk_samples(), now=at(11, 0))
    assert [t.trigger for t in after] == [t.trigger for t in before]
    assert [t.is_active() for t in after] == [t.is_active() for t in before]
    assert [t.repetition for t in after] == [t.repetition for t in before]
    assert [t.rule for t in after] == [t.rule for t in before]


def test_proximity_parsing():
    """The Proximity value object exposes direction, target and radius."""
    alarm = Event.from_ical(event_ics()).subcomponents[0]
    proximity = Proximity.from_alarm(alarm)
    assert proximity == Proximity(
        "ARRIVE", ROOM_LAT, ROOM_LON, 100.0, "Meeting room 1", "location-1"
    )
    assert proximity.contains(ROOM_LAT, ROOM_LON)
    assert not proximity.contains(FAR_LAT, FAR_LON)


def test_proximity_radius_must_be_positive():
    """The radius is validated on construction, too."""
    with pytest.raises(InvalidCalendar):
        Proximity("ARRIVE", ROOM_LAT, ROOM_LON, 0)
    with pytest.raises(InvalidCalendar):
        Proximity("SIDEWAYS", ROOM_LAT, ROOM_LON, 100)
