"""Test proximity-triggered VALARM computation.

RFC 9074 extends VALARM with a PROXIMITY property: the alarm fires when
the user arrives at or departs from a location instead of at a fixed
time. The computation consumes an external stream of position samples
and a controllable time; it does not do GPS or network positioning.

See https://www.rfc-editor.org/rfc/rfc9074.html
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from icalendar import (
    Alarms,
    Event,
    InvalidCalendar,
    InvalidProximityRadius,
    LocalTimezoneMissing,
    LocationEvent,
    ProximityLocationMissing,
)

UTC = timezone.utc

OFFICE_LATITUDE = 40.443
OFFICE_LONGITUDE = -79.945


def make_event(
    proximity="ARRIVE",
    url=f"geo:{OFFICE_LATITUDE},{OFFICE_LONGITUDE};u=50",
    extra_alarm_properties="",
    extra_event_properties="",
    vlocation=None,
):
    """Create an event with one proximity alarm."""
    if vlocation is None:
        vlocation = (
            "BEGIN:VLOCATION\n"
            "UID:loc-1\n"
            "NAME:Office\n"
            f"URL:{url}\n"
            "END:VLOCATION\n"
        )
    return Event.from_ical(
        "BEGIN:VEVENT\n"
        "UID:meeting-1\n"
        "DTSTAMP:20260301T000000Z\n"
        "DTSTART;TZID=America/New_York:20260302T103000\n"
        "DTEND;TZID=America/New_York:20260302T113000\n"
        "SUMMARY:Meeting\n"
        f"{extra_event_properties}"
        "BEGIN:VALARM\n"
        "UID:alarm-1\n"
        "ACTION:DISPLAY\n"
        "TRIGGER;VALUE=DATE-TIME:19760401T005545Z\n"
        "DESCRIPTION:Remember the meeting\n"
        f"PROXIMITY:{proximity}\n"
        f"{extra_alarm_properties}"
        f"{vlocation}"
        "END:VALARM\n"
        "END:VEVENT\n"
    )


def at(hour, minute, tzinfo=UTC):
    """A location sample time on the day of the meeting."""
    return datetime(2026, 3, 2, hour, minute, tzinfo=tzinfo)


def outside(when):
    """A sample far away from the office."""
    return LocationEvent(when, 40.0, -79.0)


def inside(when):
    """A sample at the office."""
    return LocationEvent(when, OFFICE_LATITUDE, OFFICE_LONGITUDE)


@pytest.fixture
def enter_stay_leave_reenter():
    """Simulate entering, staying, leaving and re-entering the office."""
    return [
        outside(at(14, 0)),
        inside(at(14, 5)),  # enter
        inside(at(14, 10)),  # stay
        outside(at(14, 20)),  # leave
        inside(at(14, 30)),  # re-enter
    ]


def test_arrive_triggers_only_on_entering(enter_stay_leave_reenter):
    """ARRIVE fires on each outside -> inside transition, not while staying."""
    alarms = Alarms(make_event())
    records = alarms.proximity_triggers(enter_stay_leave_reenter)
    assert [record.trigger for record in records] == [at(14, 5), at(14, 30)]
    assert all(record.rule == "proximity" for record in records)
    assert all(record.repetition == 0 for record in records)
    assert [record.location_event for record in records] == [
        inside(at(14, 5)),
        inside(at(14, 30)),
    ]
    assert all(record.is_active() for record in records)


def test_depart_triggers_only_on_leaving(enter_stay_leave_reenter):
    """DEPART fires on each inside -> outside transition."""
    alarms = Alarms(make_event(proximity="DEPART"))
    records = alarms.proximity_triggers(enter_stay_leave_reenter)
    assert [record.trigger for record in records] == [at(14, 20)]
    assert records[0].location_event == outside(at(14, 20))


def test_repeat_and_duration_create_explainable_records():
    """REPEAT and DURATION add records that explain their origin."""
    alarms = Alarms(make_event(extra_alarm_properties="REPEAT:2\nDURATION:PT5M\n"))
    records = alarms.proximity_triggers([outside(at(14, 0)), inside(at(14, 5))])
    assert [record.trigger for record in records] == [
        at(14, 5),
        at(14, 10),
        at(14, 15),
    ]
    assert [record.rule for record in records] == ["proximity", "repeat", "repeat"]
    assert [record.repetition for record in records] == [0, 1, 2]
    # all repetitions are caused by the same location event and location
    assert {record.location_event for record in records} == {inside(at(14, 5))}
    assert {record.location["UID"] for record in records} == {"loc-1"}


def test_acknowledged_alarm_does_not_fire_again():
    """An acknowledgment at or after the trigger time suppresses the trigger."""
    event = make_event(extra_alarm_properties="ACKNOWLEDGED:20260302T140500Z\n")
    (record,) = Alarms(event).proximity_triggers([outside(at(14, 0)), inside(at(14, 5))])
    assert record.acknowledged == at(14, 5)
    assert not record.is_active()


def test_acknowledgement_before_trigger_still_fires():
    """An acknowledgment before the trigger time does not suppress it."""
    event = make_event(extra_alarm_properties="ACKNOWLEDGED:20260302T140400Z\n")
    (record,) = Alarms(event).proximity_triggers([outside(at(14, 0)), inside(at(14, 5))])
    assert record.is_active()


def test_out_of_order_events_do_not_roll_back_acknowledgement(
    enter_stay_leave_reenter,
):
    """Reordered location events give the same records and ACK state."""
    event = make_event(extra_alarm_properties="ACKNOWLEDGED:20260302T140500Z\n")
    ordered = Alarms(event).proximity_triggers(enter_stay_leave_reenter)
    shuffled = Alarms(event).proximity_triggers(
        list(reversed(enter_stay_leave_reenter))
    )
    assert [r.trigger for r in shuffled] == [r.trigger for r in ordered]
    assert [r.is_active() for r in shuffled] == [r.is_active() for r in ordered]
    # the first trigger is acknowledged, the re-entry still fires
    assert [r.is_active() for r in ordered] == [False, True]


def test_snooze_reschedules_the_trigger():
    """A snoozed alarm fires at the snooze time with an explaining record."""
    event = make_event(
        extra_event_properties="X-MOZ-SNOOZE-TIME:20260302T145000Z\n"
    )
    (record,) = Alarms(event).proximity_triggers([outside(at(14, 0)), inside(at(14, 5))])
    assert record.rule == "snooze"
    assert record.trigger == at(14, 50)
    assert record.location_event == inside(at(14, 5))


def test_missing_location_has_clear_result():
    """A proximity alarm without VLOCATION cannot be computed."""
    alarms = Alarms(make_event(vlocation=""))
    with pytest.raises(ProximityLocationMissing):
        alarms.proximity_triggers([outside(at(14, 0)), inside(at(14, 5))])


@pytest.mark.parametrize("url", ["geo:40.443,-79.945;u=-5", "geo:40.443,-79.945;u=abc"])
def test_invalid_radius_has_clear_result(url):
    """A negative or non-numeric radius is invalid."""
    alarms = Alarms(make_event(url=url))
    with pytest.raises(InvalidProximityRadius):
        alarms.proximity_triggers([outside(at(14, 0)), inside(at(14, 5))])


def test_invalid_radius_is_an_invalid_calendar():
    """The radius error is also reported as an invalid calendar."""
    alarms = Alarms(make_event(url="geo:40.443,-79.945;u=-5"))
    with pytest.raises(InvalidCalendar):
        alarms.proximity_triggers([outside(at(14, 0)), inside(at(14, 5))])


def test_invalid_geo_uri_has_clear_result():
    """A location URL that is not a geo: URI is invalid."""
    alarms = Alarms(make_event(url="https://example.com/map"))
    with pytest.raises(InvalidCalendar):
        alarms.proximity_triggers([outside(at(14, 0)), inside(at(14, 5))])


def test_invalid_proximity_value_has_clear_result():
    """PROXIMITY must be ARRIVE or DEPART."""
    alarms = Alarms(make_event(proximity="NEAR"))
    with pytest.raises(InvalidCalendar):
        alarms.proximity_triggers([outside(at(14, 0)), inside(at(14, 5))])


def test_cancelled_event_never_triggers(enter_stay_leave_reenter):
    """A cancelled event has a clear result: no triggers."""
    event = make_event(extra_event_properties="STATUS:CANCELLED\n")
    assert Alarms(event).proximity_triggers(enter_stay_leave_reenter) == []


def test_location_events_need_a_timezone():
    """Naive timestamps cannot be matched and raise a clear error."""
    alarms = Alarms(make_event())
    with pytest.raises(LocalTimezoneMissing):
        alarms.proximity_triggers([LocationEvent(datetime(2026, 3, 2, 14, 5), 40.443, -79.945)])


def test_now_limits_the_triggers():
    """The controllable time selects the triggers that are due."""
    alarms = Alarms(make_event(extra_alarm_properties="REPEAT:2\nDURATION:PT5M\n"))
    samples = [outside(at(14, 0)), inside(at(14, 5))]
    assert [r.trigger for r in alarms.proximity_triggers(samples, now=at(14, 10))] == [
        at(14, 5),
        at(14, 10),
    ]
    assert alarms.proximity_triggers(samples, now=at(14, 4)) == []


def test_default_radius_is_used_without_uncertainty():
    """A geo: URI without u= uses the configurable default radius."""
    event = make_event(url=f"geo:{OFFICE_LATITUDE},{OFFICE_LONGITUDE}")
    nearby = LocationEvent(at(14, 5), OFFICE_LATITUDE + 0.0005, OFFICE_LONGITUDE)
    assert len(Alarms(event).proximity_triggers([outside(at(14, 0)), nearby])) == 1
    assert (
        Alarms(event).proximity_triggers(
            [outside(at(14, 0)), nearby], default_radius=10
        )
        == []
    )


def test_samples_in_other_timezones():
    """Location events may carry any timezone; triggers are absolute."""
    shanghai = ZoneInfo("Asia/Shanghai")
    samples = [
        outside(at(22, 0, shanghai)),
        inside(at(22, 5, shanghai)),
    ]
    (record,) = Alarms(make_event()).proximity_triggers(samples)
    assert record.trigger == at(14, 5)


def test_time_based_alarms_still_use_the_old_path():
    """A proximity alarm does not leak into the time-based computation."""
    event = make_event()
    alarms = Alarms(event)
    assert alarms.times == []
    # a normal relative alarm in the same event is unaffected
    relative = Event.from_ical(
        "BEGIN:VEVENT\n"
        "UID:meeting-2\n"
        "DTSTAMP:20260301T000000Z\n"
        "DTSTART:20260302T103000Z\n"
        "DTEND:20260302T113000Z\n"
        "BEGIN:VALARM\n"
        "ACTION:DISPLAY\n"
        "DESCRIPTION:15 minutes before\n"
        "TRIGGER:-PT15M\n"
        "END:VALARM\n"
        "END:VEVENT\n"
    )
    (alarm_time,) = Alarms(relative).times
    assert alarm_time.trigger == datetime(2026, 3, 2, 10, 15, tzinfo=UTC)


def test_serialization_roundtrip_preserves_triggers_and_ack_boundary():
    """Serializing and parsing again keeps trigger order and the ACK boundary."""
    event = make_event(
        extra_alarm_properties="REPEAT:1\n"
        "DURATION:PT5M\n"
        "ACKNOWLEDGED:20260302T140500Z\n"
        "X-CUSTOM-EXTENSION:keep-me\n"
    )
    parsed = Event.from_ical(event.to_ical())
    # unknown extensions survive the roundtrip
    assert parsed.subcomponents[0]["X-CUSTOM-EXTENSION"] == "keep-me"
    samples = [outside(at(14, 0)), inside(at(14, 5)), outside(at(14, 20)), inside(at(14, 30))]
    records = Alarms(parsed).proximity_triggers(samples)
    assert [record.trigger for record in records] == [
        at(14, 5),
        at(14, 10),
        at(14, 30),
        at(14, 35),
    ]
    assert [record.rule for record in records] == [
        "proximity",
        "repeat",
        "proximity",
        "repeat",
    ]
    # ACK boundary: triggers at or before 14:05 are acknowledged
    assert [record.is_active() for record in records] == [False, True, True, True]
