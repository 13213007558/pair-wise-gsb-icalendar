"""Acceptance tests for occurrence queries within a finite time window.

These tests combine DST transitions, cross-midnight events, single
reschedules, and THISANDFUTURE reschedules, and check the recurrence
identity, the actual start/end, and the source component of each
occurrence.
"""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from icalendar import Calendar, InvalidCalendar
from icalendar.timezone import tzp

UTC = timezone.utc
NY = ZoneInfo("America/New_York")


@pytest.fixture(autouse=True, scope="module")
def reset_timezone_provider_cache():
    """Do not leak cached timezones into doctests running after this module."""
    yield
    tzp.use_default()

WEEKLY_NY = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:weekly-ny
DTSTAMP:20260101T000000Z
DTSTART;TZID=America/New_York:20260302T090000
DTEND;TZID=America/New_York:20260302T100000
RRULE:FREQ=WEEKLY
END:VEVENT
END:VCALENDAR
"""


def test_occurrences_across_spring_forward_dst():
    """Weekly 9:00 in New York keeps its local time and identity over DST."""
    calendar = Calendar.from_ical(WEEKLY_NY)
    occurrences = calendar.occurrences(
        datetime(2026, 3, 1, tzinfo=NY), datetime(2026, 3, 23, tzinfo=NY)
    )
    assert not occurrences.incomplete
    assert [o.start for o in occurrences] == [
        datetime(2026, 3, 2, 9, tzinfo=NY),
        datetime(2026, 3, 9, 9, tzinfo=NY),
        datetime(2026, 3, 16, 9, tzinfo=NY),
    ]
    # the wall clock time survives the DST transition on 2026-03-08
    assert [o.start.utcoffset() for o in occurrences] == [
        timedelta(hours=-5),
        timedelta(hours=-4),
        timedelta(hours=-4),
    ]
    # unmodified instances are identified by their own start
    assert [o.recurrence_id for o in occurrences] == [o.start for o in occurrences]
    assert [o.end for o in occurrences] == [
        datetime(2026, 3, 2, 10, tzinfo=NY),
        datetime(2026, 3, 9, 10, tzinfo=NY),
        datetime(2026, 3, 16, 10, tzinfo=NY),
    ]
    assert all(not o.is_exception for o in occurrences)


def test_occurrences_across_fall_back_dst():
    """The same holds when the clock is turned back."""
    calendar = Calendar.from_ical(WEEKLY_NY.replace("20260302", "20261026"))
    occurrences = calendar.occurrences(
        datetime(2026, 10, 25, tzinfo=NY), datetime(2026, 11, 16, tzinfo=NY)
    )
    assert [o.start for o in occurrences] == [
        datetime(2026, 10, 26, 9, tzinfo=NY),
        datetime(2026, 11, 2, 9, tzinfo=NY),
        datetime(2026, 11, 9, 9, tzinfo=NY),
    ]
    assert [o.start.utcoffset() for o in occurrences] == [
        timedelta(hours=-4),
        timedelta(hours=-5),
        timedelta(hours=-5),
    ]


CROSS_MIDNIGHT = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:cross-midnight
DTSTAMP:20260101T000000Z
DTSTART:20260101T230000Z
DTEND:20260102T010000Z
RRULE:FREQ=DAILY
END:VEVENT
END:VCALENDAR
"""


def test_cross_midnight_event_intersects_window():
    """An event is found if it overlaps the window, even across midnight."""
    calendar = Calendar.from_ical(CROSS_MIDNIGHT)
    occurrences = calendar.occurrences(
        datetime(2026, 1, 3, 0, 30, tzinfo=UTC),
        datetime(2026, 1, 4, 0, 30, tzinfo=UTC),
    )
    assert [(o.start, o.end) for o in occurrences] == [
        (datetime(2026, 1, 2, 23, tzinfo=UTC), datetime(2026, 1, 3, 1, tzinfo=UTC)),
        (datetime(2026, 1, 3, 23, tzinfo=UTC), datetime(2026, 1, 4, 1, tzinfo=UTC)),
    ]


def test_window_is_half_open():
    """[start, end): touching the window from the outside does not intersect."""
    calendar = Calendar.from_ical(CROSS_MIDNIGHT)
    # the instance ends exactly at the window start
    assert (
        calendar.occurrences(
            datetime(2026, 1, 3, 1, tzinfo=UTC), datetime(2026, 1, 3, 22, tzinfo=UTC)
        )
        == []
    )
    # the instance starts exactly at the window end
    assert (
        calendar.occurrences(
            datetime(2026, 1, 3, 2, tzinfo=UTC), datetime(2026, 1, 3, 23, tzinfo=UTC)
        )
        == []
    )


ZERO_DURATION = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:instant
DTSTAMP:20260101T000000Z
DTSTART:20260110T100000Z
END:VEVENT
END:VCALENDAR
"""


def test_zero_duration_event_boundaries():
    """A zero-duration event occurs at an instant inside [start, end)."""
    calendar = Calendar.from_ical(ZERO_DURATION)
    at = datetime(2026, 1, 10, 10, tzinfo=UTC)
    assert [
        o.start for o in calendar.occurrences(at, datetime(2026, 1, 10, 11, tzinfo=UTC))
    ] == [at]
    assert calendar.occurrences(datetime(2026, 1, 10, 9, tzinfo=UTC), at) == []
    assert (
        calendar.occurrences(
            datetime(2026, 1, 10, 11, tzinfo=UTC), datetime(2026, 1, 10, 12, tzinfo=UTC)
        )
        == []
    )


def weekly_calendar(*extra_events):
    """A weekly Monday 09:00-10:00 UTC meeting plus detached exceptions."""
    ics = "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//test//EN\n" + WEEKLY_MASTER
    for extra in extra_events:
        ics += extra
    ics += "END:VCALENDAR\n"
    return Calendar.from_ical(ics)


WEEKLY_MASTER = """BEGIN:VEVENT
UID:weekly
DTSTAMP:20260101T000000Z
DTSTART:20260105T090000Z
DTEND:20260105T100000Z
RRULE:FREQ=WEEKLY
END:VEVENT
"""


MOVED_OUT = """BEGIN:VEVENT
UID:weekly
DTSTAMP:20260101T000000Z
RECURRENCE-ID:20260112T090000Z
DTSTART:20260113T140000Z
DTEND:20260113T150000Z
SUMMARY:Moved to Tuesday
END:VEVENT
"""


def test_single_instance_moved_within_window():
    """A moved instance occurs at its new time, identified by the old one."""
    calendar = weekly_calendar(MOVED_OUT)
    occurrences = calendar.occurrences(
        datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC)
    )
    assert [(o.recurrence_id, o.start, o.end) for o in occurrences] == [
        (datetime(2026, 1, 5, 9, tzinfo=UTC),) * 2
        + (datetime(2026, 1, 5, 10, tzinfo=UTC),),
        (
            datetime(2026, 1, 12, 9, tzinfo=UTC),
            datetime(2026, 1, 13, 14, tzinfo=UTC),
            datetime(2026, 1, 13, 15, tzinfo=UTC),
        ),
        (datetime(2026, 1, 19, 9, tzinfo=UTC),) * 2
        + (datetime(2026, 1, 19, 10, tzinfo=UTC),),
        (datetime(2026, 1, 26, 9, tzinfo=UTC),) * 2
        + (datetime(2026, 1, 26, 10, tzinfo=UTC),),
    ]
    moved = occurrences[1]
    assert moved.is_exception
    # the source component of the moved instance is the detached exception
    assert moved.component == calendar.events[1]
    assert moved.component is not calendar.events[1]
    assert str(moved.component["SUMMARY"]) == "Moved to Tuesday"
    assert occurrences[0].component == calendar.events[0]


def test_single_instance_moved_out_of_window():
    """An instance moved out of the window does not occur in it."""
    moved_far_away = MOVED_OUT.replace("20260113T14", "20260213T14").replace(
        "20260113T15", "20260213T15"
    )
    calendar = weekly_calendar(moved_far_away)
    occurrences = calendar.occurrences(
        datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC)
    )
    assert [o.recurrence_id for o in occurrences] == [
        datetime(2026, 1, 5, 9, tzinfo=UTC),
        datetime(2026, 1, 19, 9, tzinfo=UTC),
        datetime(2026, 1, 26, 9, tzinfo=UTC),
    ]
    # ... but it occurs in a window covering its new time
    later = calendar.occurrences(
        datetime(2026, 2, 1, tzinfo=UTC), datetime(2026, 3, 1, tzinfo=UTC)
    )
    assert [o.recurrence_id for o in later] == [
        datetime(2026, 2, 2, 9, tzinfo=UTC),
        datetime(2026, 2, 9, 9, tzinfo=UTC),
        datetime(2026, 1, 12, 9, tzinfo=UTC),
        datetime(2026, 2, 16, 9, tzinfo=UTC),
        datetime(2026, 2, 23, 9, tzinfo=UTC),
    ]
    assert later[2].start == datetime(2026, 2, 13, 14, tzinfo=UTC)
    assert later[2].is_exception


def test_single_instance_moved_into_window():
    """An instance whose identity is outside the window can be moved in."""
    moved_into = """BEGIN:VEVENT
UID:weekly
DTSTAMP:20260101T000000Z
RECURRENCE-ID:20260209T090000Z
DTSTART:20260115T090000Z
DTEND:20260115T100000Z
END:VEVENT
"""
    calendar = weekly_calendar(moved_into)
    occurrences = calendar.occurrences(
        datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC)
    )
    assert [(o.recurrence_id, o.start) for o in occurrences] == [
        (datetime(2026, 1, 5, 9, tzinfo=UTC), datetime(2026, 1, 5, 9, tzinfo=UTC)),
        (datetime(2026, 1, 12, 9, tzinfo=UTC), datetime(2026, 1, 12, 9, tzinfo=UTC)),
        (datetime(2026, 2, 9, 9, tzinfo=UTC), datetime(2026, 1, 15, 9, tzinfo=UTC)),
        (datetime(2026, 1, 19, 9, tzinfo=UTC), datetime(2026, 1, 19, 9, tzinfo=UTC)),
        (datetime(2026, 1, 26, 9, tzinfo=UTC), datetime(2026, 1, 26, 9, tzinfo=UTC)),
    ]
    moved = occurrences[2]
    assert moved.is_exception
    assert moved.component == calendar.events[1]
    # and the original February slot no longer occurs
    february = calendar.occurrences(
        datetime(2026, 2, 1, tzinfo=UTC), datetime(2026, 3, 1, tzinfo=UTC)
    )
    assert datetime(2026, 2, 9, 9, tzinfo=UTC) not in [o.start for o in february]


CANCELLED = """BEGIN:VEVENT
UID:weekly
DTSTAMP:20260101T000000Z
RECURRENCE-ID:20260112T090000Z
DTSTART:20260112T090000Z
DTEND:20260112T100000Z
STATUS:CANCELLED
END:VEVENT
"""


def test_cancelled_exception_is_not_returned():
    """A detached exception with STATUS:CANCELLED removes its instance."""
    calendar = weekly_calendar(CANCELLED)
    occurrences = calendar.occurrences(
        datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC)
    )
    assert [o.recurrence_id for o in occurrences] == [
        datetime(2026, 1, 5, 9, tzinfo=UTC),
        datetime(2026, 1, 19, 9, tzinfo=UTC),
        datetime(2026, 1, 26, 9, tzinfo=UTC),
    ]


def test_exdate_removes_instance():
    """An EXDATE on the master removes the instance as well."""
    calendar = weekly_calendar()
    calendar.events[0].add("EXDATE", datetime(2026, 1, 12, 9, tzinfo=UTC))
    occurrences = calendar.occurrences(
        datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC)
    )
    assert [o.recurrence_id for o in occurrences] == [
        datetime(2026, 1, 5, 9, tzinfo=UTC),
        datetime(2026, 1, 19, 9, tzinfo=UTC),
        datetime(2026, 1, 26, 9, tzinfo=UTC),
    ]


THISANDFUTURE_LATER = """BEGIN:VEVENT
UID:weekly
DTSTAMP:20260101T000000Z
RECURRENCE-ID;RANGE=THISANDFUTURE:20260119T090000Z
DTSTART:20260119T093000Z
DTEND:20260119T103000Z
END:VEVENT
"""


def test_thisandfuture_shifts_start_and_duration():
    """From one week on, the meeting is half an hour later."""
    calendar = weekly_calendar(THISANDFUTURE_LATER)
    occurrences = calendar.occurrences(
        datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 9, tzinfo=UTC)
    )
    assert [(o.recurrence_id, o.start, o.end) for o in occurrences] == [
        (datetime(2026, 1, 5, 9, tzinfo=UTC),) * 2
        + (datetime(2026, 1, 5, 10, tzinfo=UTC),),
        (datetime(2026, 1, 12, 9, tzinfo=UTC),) * 2
        + (datetime(2026, 1, 12, 10, tzinfo=UTC),),
        (
            datetime(2026, 1, 19, 9, tzinfo=UTC),
            datetime(2026, 1, 19, 9, 30, tzinfo=UTC),
            datetime(2026, 1, 19, 10, 30, tzinfo=UTC),
        ),
        (
            datetime(2026, 1, 26, 9, tzinfo=UTC),
            datetime(2026, 1, 26, 9, 30, tzinfo=UTC),
            datetime(2026, 1, 26, 10, 30, tzinfo=UTC),
        ),
        (
            datetime(2026, 2, 2, 9, tzinfo=UTC),
            datetime(2026, 2, 2, 9, 30, tzinfo=UTC),
            datetime(2026, 2, 2, 10, 30, tzinfo=UTC),
        ),
    ]
    # the identities stay on the original Mondays at 09:00
    assert [o.is_exception for o in occurrences] == [False, False, True, True, True]


THISANDFUTURE_OTHER_DAY = """BEGIN:VEVENT
UID:weekly
DTSTAMP:20260101T000000Z
RECURRENCE-ID;RANGE=THISANDFUTURE:20260119T090000Z
DTSTART:20260120T140000Z
DTEND:20260120T150000Z
END:VEVENT
"""


def test_thisandfuture_moving_to_another_day_keeps_identity():
    """Instances moved to Tuesday are not mistaken for other instances."""
    calendar = weekly_calendar(THISANDFUTURE_OTHER_DAY)
    occurrences = calendar.occurrences(
        datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 9, tzinfo=UTC)
    )
    assert [(o.recurrence_id, o.start) for o in occurrences] == [
        (datetime(2026, 1, 5, 9, tzinfo=UTC), datetime(2026, 1, 5, 9, tzinfo=UTC)),
        (datetime(2026, 1, 12, 9, tzinfo=UTC), datetime(2026, 1, 12, 9, tzinfo=UTC)),
        (datetime(2026, 1, 19, 9, tzinfo=UTC), datetime(2026, 1, 20, 14, tzinfo=UTC)),
        (datetime(2026, 1, 26, 9, tzinfo=UTC), datetime(2026, 1, 27, 14, tzinfo=UTC)),
        (datetime(2026, 2, 2, 9, tzinfo=UTC), datetime(2026, 2, 3, 14, tzinfo=UTC)),
    ]
    # no instance occurs at the old Monday 09:00 slots any more
    starts = [o.start for o in occurrences]
    assert datetime(2026, 1, 19, 9, tzinfo=UTC) not in starts
    assert datetime(2026, 1, 26, 9, tzinfo=UTC) not in starts


def test_thisandfuture_combined_with_single_reschedule():
    """A single reschedule wins over THISANDFUTURE for its own instance."""
    single = """BEGIN:VEVENT
UID:weekly
DTSTAMP:20260101T000000Z
RECURRENCE-ID:20260126T090000Z
DTSTART:20260126T150000Z
DTEND:20260126T160000Z
END:VEVENT
"""
    calendar = weekly_calendar(THISANDFUTURE_LATER, single)
    occurrences = calendar.occurrences(
        datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 9, tzinfo=UTC)
    )
    assert [(o.recurrence_id, o.start) for o in occurrences] == [
        (datetime(2026, 1, 5, 9, tzinfo=UTC), datetime(2026, 1, 5, 9, tzinfo=UTC)),
        (datetime(2026, 1, 12, 9, tzinfo=UTC), datetime(2026, 1, 12, 9, tzinfo=UTC)),
        (
            datetime(2026, 1, 19, 9, tzinfo=UTC),
            datetime(2026, 1, 19, 9, 30, tzinfo=UTC),
        ),
        (datetime(2026, 1, 26, 9, tzinfo=UTC), datetime(2026, 1, 26, 15, tzinfo=UTC)),
        (datetime(2026, 2, 2, 9, tzinfo=UTC), datetime(2026, 2, 2, 9, 30, tzinfo=UTC)),
    ]


RDATE_EVENT = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:rdates
DTSTAMP:20260101T000000Z
DTSTART:20260105T090000Z
DTEND:20260105T100000Z
RDATE:20260108T090000Z
RDATE;VALUE=PERIOD:20260110T140000Z/20260110T163000Z
END:VEVENT
END:VCALENDAR
"""


def test_rdate_and_rdate_period():
    """RDATEs add instances; PERIOD RDATEs carry their own end."""
    calendar = Calendar.from_ical(RDATE_EVENT)
    occurrences = calendar.occurrences(
        datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC)
    )
    assert [(o.recurrence_id, o.start, o.end) for o in occurrences] == [
        (datetime(2026, 1, 5, 9, tzinfo=UTC),) * 2
        + (datetime(2026, 1, 5, 10, tzinfo=UTC),),
        (datetime(2026, 1, 8, 9, tzinfo=UTC),) * 2
        + (datetime(2026, 1, 8, 10, tzinfo=UTC),),
        (
            datetime(2026, 1, 10, 14, tzinfo=UTC),
            datetime(2026, 1, 10, 14, tzinfo=UTC),
            datetime(2026, 1, 10, 16, 30, tzinfo=UTC),
        ),
    ]


ALL_DAY = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:all-day
DTSTAMP:20260101T000000Z
DTSTART;VALUE=DATE:20260301
RRULE:FREQ=DAILY
END:VEVENT
BEGIN:VEVENT
UID:multi-day
DTSTAMP:20260101T000000Z
DTSTART;VALUE=DATE:20260302
DTEND;VALUE=DATE:20260304
END:VEVENT
BEGIN:VEVENT
UID:timed
DTSTAMP:20260101T000000Z
DTSTART:20260302T090000Z
DTEND:20260302T100000Z
END:VEVENT
END:VCALENDAR
"""


def test_all_day_events_with_date_window():
    """A date window queries all-day events, and only those."""
    calendar = Calendar.from_ical(ALL_DAY)
    occurrences = calendar.occurrences(date(2026, 3, 1), date(2026, 3, 4))
    assert [(o.recurrence_id, o.start, o.end) for o in occurrences] == [
        (date(2026, 3, 1), date(2026, 3, 1), date(2026, 3, 2)),
        (date(2026, 3, 2), date(2026, 3, 2), date(2026, 3, 3)),
        (date(2026, 3, 2), date(2026, 3, 2), date(2026, 3, 4)),
        (date(2026, 3, 3), date(2026, 3, 3), date(2026, 3, 4)),
    ]
    assert all(
        isinstance(o.start, date) and not isinstance(o.start, datetime)
        for o in occurrences
    )


def test_all_day_and_timed_events_are_separate():
    """A datetime window does not return all-day events and vice versa."""
    calendar = Calendar.from_ical(ALL_DAY)
    timed = calendar.occurrences(
        datetime(2026, 3, 1, tzinfo=UTC), datetime(2026, 3, 4, tzinfo=UTC)
    )
    assert [o.component["UID"] for o in timed] == ["timed"]
    all_day = calendar.occurrences(date(2026, 3, 1), date(2026, 3, 4))
    assert "timed" not in [o.component["UID"] for o in all_day]


def test_mixed_window_types_are_rejected():
    calendar = Calendar.from_ical(ALL_DAY)
    with pytest.raises(ValueError, match="mix"):
        calendar.occurrences(date(2026, 3, 1), datetime(2026, 3, 4, tzinfo=UTC))


FLOATING = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:floating
DTSTAMP:20260101T000000Z
DTSTART:20260105T090000
DTEND:20260105T100000
RRULE:FREQ=DAILY;COUNT=3
END:VEVENT
END:VCALENDAR
"""


def test_floating_time_requires_timezone():
    """Floating datetimes need an explicit interpretation timezone."""
    calendar = Calendar.from_ical(FLOATING)
    with pytest.raises(ValueError, match="floating"):
        calendar.occurrences(
            datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC)
        )
    with pytest.raises(ValueError, match="floating"):
        calendar.occurrences(datetime(2026, 1, 1), datetime(2026, 2, 1))
    occurrences = calendar.occurrences(
        datetime(2026, 1, 1), datetime(2026, 2, 1), tz="America/New_York"
    )
    assert [o.start for o in occurrences] == [
        datetime(2026, 1, 5, 9, tzinfo=NY),
        datetime(2026, 1, 6, 9, tzinfo=NY),
        datetime(2026, 1, 7, 9, tzinfo=NY),
    ]


def test_infinite_rule_is_bounded_and_limit_is_reported():
    """Infinite rules only expand within the window, up to the limit."""
    calendar = Calendar.from_ical(
        FLOATING.replace("RRULE:FREQ=DAILY;COUNT=3", "RRULE:FREQ=DAILY")
    )
    occurrences = calendar.occurrences(
        datetime(2026, 1, 1), datetime(2027, 1, 1), tz="UTC"
    )
    assert len(occurrences) == 361  # daily from 2026-01-05 to 2026-12-31
    assert not occurrences.incomplete
    limited = calendar.occurrences(
        datetime(2026, 1, 1), datetime(2027, 1, 1), tz="UTC", max_candidates=10
    )
    assert len(limited) == 10
    assert limited.incomplete


def test_multiple_rrules_are_not_supported():
    """The recurrence set of multiple RRULEs is undefined."""
    calendar = Calendar.from_ical(
        FLOATING.replace(
            "RRULE:FREQ=DAILY;COUNT=3", "RRULE:FREQ=DAILY\nRRULE:FREQ=WEEKLY"
        )
    )
    with pytest.raises(InvalidCalendar, match="multiple RRULE"):
        calendar.occurrences(
            datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC)
        )


def test_modifying_occurrences_does_not_pollute_the_calendar():
    """The components of the occurrences are copies."""
    calendar = weekly_calendar(MOVED_OUT)
    occurrences = calendar.occurrences(
        datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC)
    )
    for occurrence in occurrences:
        occurrence.component["SUMMARY"] = "POLLUTED"
        occurrence.component["DTSTART"] = "garbage"
    for event in calendar.events:
        assert "SUMMARY" not in event or str(event["SUMMARY"]) == "Moved to Tuesday"
        assert event["DTSTART"].dt != "garbage"
    # querying again returns the same, unpolluted results
    again = calendar.occurrences(
        datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC)
    )
    assert [o.start for o in again] == [o.start for o in occurrences]


def test_detached_exception_without_master():
    """A detached exception whose master is missing still occurs."""
    calendar = Calendar.from_ical(
        """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:lonely
DTSTAMP:20260101T000000Z
RECURRENCE-ID:20260112T090000Z
DTSTART:20260113T140000Z
DTEND:20260113T150000Z
END:VEVENT
END:VCALENDAR
"""
    )
    occurrences = calendar.occurrences(
        datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC)
    )
    assert [(o.recurrence_id, o.start) for o in occurrences] == [
        (datetime(2026, 1, 12, 9, tzinfo=UTC), datetime(2026, 1, 13, 14, tzinfo=UTC))
    ]
    assert occurrences[0].is_exception
