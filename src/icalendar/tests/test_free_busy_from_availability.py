"""Test computing a VFREEBUSY from VAVAILABILITY components per RFC 7953."""

from datetime import datetime, timedelta, timezone
from itertools import pairwise
from zoneinfo import ZoneInfo

import pytest

from icalendar import (
    BUSYTYPE,
    FBTYPE,
    Availability,
    Available,
    Calendar,
    FreeBusy,
)
from icalendar.error import InvalidCalendar
from icalendar.prop import vPeriod

UTC = timezone.utc
NEW_YORK = ZoneInfo("America/New_York")


def dt(day: int, hour: int = 0, minute: int = 0) -> datetime:
    """A UTC datetime in September 2026."""
    return datetime(2026, 9, day, hour, minute, tzinfo=UTC)


def segments(free_busy: FreeBusy) -> list[tuple[datetime, datetime, FBTYPE]]:
    """The (start, end, fbtype) tuples of a VFREEBUSY."""
    periods = free_busy["FREEBUSY"]
    if not isinstance(periods, list):
        periods = [periods]
    return [
        (period.start, period.end, FBTYPE(period.params["FBTYPE"]))
        for period in periods
    ]


def assert_covers_window(free_busy: FreeBusy, start: datetime, end: datetime) -> None:
    """The segments partition the whole window without gaps or overlaps."""
    parts = segments(free_busy)
    assert parts[0][0] == start
    assert parts[-1][1] == end
    for left, right in pairwise(parts):
        assert left[1] == right[0]
        assert left[0] < left[1]


WINDOW_START = dt(21)
WINDOW_END = dt(28)


@pytest.fixture
def office_hours() -> Availability:
    """Recurring Monday-Friday 9:00-17:00 office hours in New York."""
    available = Available.new(
        start=datetime(2026, 9, 21, 9, tzinfo=NEW_YORK),
        end=datetime(2026, 9, 21, 17, tzinfo=NEW_YORK),
    )
    available.add("RRULE", {"FREQ": "WEEKLY", "BYDAY": ["MO", "TU", "WE", "TH", "FR"]})
    return Availability.new(components=[available])


@pytest.fixture
def vacation() -> Availability:
    """High-priority vacation covering Wednesday and Thursday in New York."""
    return Availability.new(
        priority=1,
        busy_type=BUSYTYPE.BUSY_UNAVAILABLE,
        start=datetime(2026, 9, 23, 0, tzinfo=NEW_YORK),
        end=datetime(2026, 9, 25, 0, tzinfo=NEW_YORK),
    )


@pytest.fixture
def free_busy(office_hours, vacation) -> FreeBusy:
    """Office hours plus a vacation override and two meetings."""
    # A meeting that starts before the window and ends during Monday
    # office hours (BUSY wins over FREE, BUSY-UNAVAILABLE wins over BUSY).
    boundary_meeting = (dt(20, 20), dt(21, 15), FBTYPE.BUSY)
    # A tentative meeting of one hour expressed with a duration.
    tentative_meeting = (dt(22, 14), timedelta(hours=1), FBTYPE.BUSY_TENTATIVE)
    return FreeBusy.from_availability(
        [office_hours, vacation],
        [boundary_meeting, tentative_meeting],
        start=WINDOW_START,
        end=WINDOW_END,
    )


EXPECTED_SEGMENTS = [
    # Monday: unavailable before work, busy during the boundary meeting, free.
    (dt(21, 0), dt(21, 13), FBTYPE.BUSY_UNAVAILABLE),
    (dt(21, 13), dt(21, 15), FBTYPE.BUSY),
    (dt(21, 15), dt(21, 21), FBTYPE.FREE),
    # Monday evening until Tuesday office hours.
    (dt(21, 21), dt(22, 13), FBTYPE.BUSY_UNAVAILABLE),
    # Tuesday: the tentative meeting splits the free office hours.
    (dt(22, 13), dt(22, 14), FBTYPE.FREE),
    (dt(22, 14), dt(22, 15), FBTYPE.BUSY_TENTATIVE),
    (dt(22, 15), dt(22, 21), FBTYPE.FREE),
    # Tuesday evening through Friday morning: the vacation overrides the
    # Wednesday/Thursday office hours and merges with the surrounding
    # unavailable time.
    (dt(22, 21), dt(25, 13), FBTYPE.BUSY_UNAVAILABLE),
    # Friday office hours and the weekend.
    (dt(25, 13), dt(25, 21), FBTYPE.FREE),
    (dt(25, 21), dt(28), FBTYPE.BUSY_UNAVAILABLE),
]


def test_office_hours_vacation_and_meetings(free_busy):
    """The combined segments have the expected types and endpoints."""
    assert segments(free_busy) == EXPECTED_SEGMENTS


def test_window_is_fully_covered(free_busy):
    """The segments partition the window without gaps or overlaps."""
    assert_covers_window(free_busy, WINDOW_START, WINDOW_END)


def test_meeting_crossing_window_start_is_clipped(free_busy):
    """Only the part of the boundary meeting within the window appears."""
    assert all(segment[0] >= WINDOW_START for segment in segments(free_busy))
    # The part before office hours stays BUSY-UNAVAILABLE.
    assert segments(free_busy)[0][2] == FBTYPE.BUSY_UNAVAILABLE


def test_round_trip_through_ics(free_busy):
    """Serialization and re-parsing keeps the window and all intervals."""
    calendar = Calendar()
    calendar.add_component(free_busy)
    parsed: FreeBusy = Calendar.from_ical(calendar.to_ical()).freebusy[0]
    assert parsed.start == WINDOW_START
    assert parsed.end == WINDOW_END
    assert segments(parsed) == EXPECTED_SEGMENTS
    assert_covers_window(parsed, WINDOW_START, WINDOW_END)


def test_floating_window_needs_timezone(office_hours):
    """Floating times are not interpreted in the process timezone."""
    with pytest.raises(ValueError, match="floating time"):
        FreeBusy.from_availability(
            office_hours,
            start=datetime(2026, 9, 21),
            end=datetime(2026, 9, 28),
        )


def test_floating_window_uses_given_timezone(office_hours):
    """With tz given, a floating window is interpreted in that timezone."""
    free_busy = FreeBusy.from_availability(
        office_hours,
        start=datetime(2026, 9, 21, 9),
        end=datetime(2026, 9, 21, 17),
        tz=NEW_YORK,
    )
    # 09:00-17:00 New York is 13:00-21:00 UTC in September.
    assert free_busy.start == dt(21, 13)
    assert free_busy.end == dt(21, 21)
    assert segments(free_busy) == [(dt(21, 13), dt(21, 21), FBTYPE.FREE)]


def test_floating_available_needs_timezone():
    """Floating AVAILABLE times also need an explicit timezone."""
    available = Available.from_ical(
        "BEGIN:AVAILABLE\r\n"
        "UID:floating-1\r\n"
        "DTSTART:20260921T090000\r\n"
        "DTEND:20260921T170000\r\n"
        "END:AVAILABLE\r\n"
    )
    availability = Availability.new(components=[available])
    with pytest.raises(ValueError, match="floating time"):
        FreeBusy.from_availability(availability, start=dt(21), end=dt(28))


def test_floating_available_uses_given_timezone():
    """A floating AVAILABLE time is interpreted in the given timezone."""
    available = Available.from_ical(
        "BEGIN:AVAILABLE\r\n"
        "UID:floating-2\r\n"
        "DTSTART:20260921T090000\r\n"
        "DTEND:20260921T170000\r\n"
        "END:AVAILABLE\r\n"
    )
    availability = Availability.new(components=[available])
    free_busy = FreeBusy.from_availability(
        availability, start=dt(21), end=dt(22), tz=NEW_YORK
    )
    assert (dt(21, 13), dt(21, 21), FBTYPE.FREE) in segments(free_busy)


def test_timezone_id_string_is_accepted(office_hours):
    """tz accepts a timezone id as well as a tzinfo."""
    free_busy = FreeBusy.from_availability(
        office_hours,
        start=datetime(2026, 9, 21, 9),
        end=datetime(2026, 9, 21, 17),
        tz="America/New_York",
    )
    assert free_busy.start == dt(21, 13)


def test_default_type_used_outside_bounded_availability():
    """Window parts without availability information use the default type."""
    available = Available.new(start=dt(22, 10), end=dt(22, 12))
    availability = Availability.new(start=dt(22, 0), end=dt(23, 0))
    availability.add_component(available)
    free_busy = FreeBusy.from_availability(
        availability, start=dt(21), end=dt(24), default=FBTYPE.FREE
    )
    assert segments(free_busy) == [
        (dt(21), dt(22), FBTYPE.FREE),
        (dt(22), dt(22, 10), FBTYPE.BUSY_UNAVAILABLE),
        (dt(22, 10), dt(22, 12), FBTYPE.FREE),
        (dt(22, 12), dt(23), FBTYPE.BUSY_UNAVAILABLE),
        (dt(23), dt(24), FBTYPE.FREE),
    ]


def test_default_can_be_busy():
    """The default type can be a busy type."""
    free_busy = FreeBusy.from_availability(
        None, start=dt(21), end=dt(22), default=FBTYPE.BUSY
    )
    assert segments(free_busy) == [(dt(21), dt(22), FBTYPE.BUSY)]


def test_same_priority_free_time_is_merged():
    """AVAILABLE intervals of same-priority components form a union."""
    morning = Available.new(start=dt(21, 9), end=dt(21, 12))
    afternoon = Available.new(start=dt(21, 12), end=dt(21, 17))
    first = Availability.new(start=dt(21, 0), end=dt(22, 0), components=[morning])
    second = Availability.new(start=dt(21, 0), end=dt(22, 0), components=[afternoon])
    free_busy = FreeBusy.from_availability([first, second], start=dt(21), end=dt(22))
    assert segments(free_busy) == [
        (dt(21, 0), dt(21, 9), FBTYPE.BUSY_UNAVAILABLE),
        (dt(21, 9), dt(21, 17), FBTYPE.FREE),
        (dt(21, 17), dt(22), FBTYPE.BUSY_UNAVAILABLE),
    ]


def test_high_priority_yields_only_in_covered_part():
    """A higher priority component only overrides the part it covers."""
    low = Availability.new(
        start=dt(21, 0),
        end=dt(22, 0),
        components=[Available.new(start=dt(21, 9), end=dt(21, 17))],
    )
    high = Availability.new(
        priority=1,
        start=dt(21, 10),
        end=dt(21, 12),
        busy_type=BUSYTYPE.BUSY,
    )
    free_busy = FreeBusy.from_availability([low, high], start=dt(21), end=dt(22))
    assert segments(free_busy) == [
        (dt(21, 0), dt(21, 9), FBTYPE.BUSY_UNAVAILABLE),
        (dt(21, 9), dt(21, 10), FBTYPE.FREE),
        (dt(21, 10), dt(21, 12), FBTYPE.BUSY),
        (dt(21, 12), dt(21, 17), FBTYPE.FREE),
        (dt(21, 17), dt(22), FBTYPE.BUSY_UNAVAILABLE),
    ]


def test_infinite_rrule_only_generates_window_instances():
    """An unlimited rule is bounded by the window."""
    available = Available.new(start=dt(21, 9), end=dt(21, 10))
    available.add("RRULE", {"FREQ": "DAILY"})
    availability = Availability.new(components=[available])
    free_busy = FreeBusy.from_availability(availability, start=dt(21), end=dt(28))
    free_parts = [part for part in segments(free_busy) if part[2] == FBTYPE.FREE]
    assert len(free_parts) == 7
    assert free_parts[0][0] == dt(21, 9)
    assert free_parts[-1][1] == dt(27, 10)


def test_recurrence_follows_local_time_across_dst():
    """Instances keep their local time when the UTC offset changes."""
    available = Available.from_ical(
        "BEGIN:AVAILABLE\r\n"
        "UID:dst\r\n"
        "DTSTART;TZID=America/New_York:20261026T090000\r\n"
        "DTEND;TZID=America/New_York:20261026T100000\r\n"
        "RRULE:FREQ=DAILY\r\n"
        "END:AVAILABLE\r\n"
    )
    availability = Availability.new(components=[available])
    # DST in New York ends at 2:00 on 2026-11-01: UTC-4 becomes UTC-5.
    free_busy = FreeBusy.from_availability(
        availability,
        start=datetime(2026, 10, 31, tzinfo=UTC),
        end=datetime(2026, 11, 3, tzinfo=UTC),
    )
    free_parts = [part for part in segments(free_busy) if part[2] == FBTYPE.FREE]
    assert free_parts == [
        (
            datetime(2026, 10, 31, 13, tzinfo=UTC),
            datetime(2026, 10, 31, 14, tzinfo=UTC),
            FBTYPE.FREE,
        ),
        (
            datetime(2026, 11, 1, 14, tzinfo=UTC),
            datetime(2026, 11, 1, 15, tzinfo=UTC),
            FBTYPE.FREE,
        ),
        (
            datetime(2026, 11, 2, 14, tzinfo=UTC),
            datetime(2026, 11, 2, 15, tzinfo=UTC),
            FBTYPE.FREE,
        ),
    ]


def test_busy_period_as_duration():
    """A busy period can be a start and a duration."""
    free_busy = FreeBusy.from_availability(
        None,
        [(dt(21, 9), timedelta(hours=2))],
        start=dt(21),
        end=dt(22),
    )
    assert (dt(21, 9), dt(21, 11), FBTYPE.BUSY) in segments(free_busy)


def test_busy_period_as_vperiod_by_duration():
    """A vPeriod written with a duration is accepted."""
    period = vPeriod(vPeriod.from_ical("20260921T090000Z/PT2H"))
    period.params["FBTYPE"] = "BUSY-TENTATIVE"
    free_busy = FreeBusy.from_availability(None, [period], start=dt(21), end=dt(22))
    assert (dt(21, 9), dt(21, 11), FBTYPE.BUSY_TENTATIVE) in segments(free_busy)


def test_available_with_duration_property():
    """AVAILABLE can be defined with DTSTART and DURATION instead of DTEND."""
    available = Available.new(start=dt(21, 9), end=dt(21, 10))
    del available.DTEND
    available.DURATION = timedelta(hours=1)
    availability = Availability.new(components=[available])
    free_busy = FreeBusy.from_availability(availability, start=dt(21), end=dt(22))
    assert (dt(21, 9), dt(21, 10), FBTYPE.FREE) in segments(free_busy)


def test_rdate_and_exdate():
    """RDATE adds instances and EXDATE removes them."""
    available = Available.new(start=dt(21, 9), end=dt(21, 10))
    available.add("RRULE", {"FREQ": "DAILY", "COUNT": "3"})
    available.add("RDATE", [dt(25, 9)])
    available.add("EXDATE", [dt(22, 9)])
    availability = Availability.new(components=[available])
    free_busy = FreeBusy.from_availability(availability, start=dt(21), end=dt(28))
    free_parts = [part for part in segments(free_busy) if part[2] == FBTYPE.FREE]
    assert free_parts == [
        (dt(21, 9), dt(21, 10), FBTYPE.FREE),
        (dt(23, 9), dt(23, 10), FBTYPE.FREE),
        (dt(25, 9), dt(25, 10), FBTYPE.FREE),
    ]


def test_recurrence_id_override():
    """An AVAILABLE with RECURRENCE-ID replaces the matching instance."""
    available = Available.new(start=dt(21, 9), end=dt(21, 10), uid="weekly")
    available.add("RRULE", {"FREQ": "DAILY", "COUNT": "3"})
    moved = Available.new(
        start=dt(22, 14), end=dt(22, 15), uid="weekly", recurrence_id=dt(22, 9)
    )
    availability = Availability.new(components=[available, moved])
    free_busy = FreeBusy.from_availability(availability, start=dt(21), end=dt(28))
    free_parts = [part for part in segments(free_busy) if part[2] == FBTYPE.FREE]
    assert free_parts == [
        (dt(21, 9), dt(21, 10), FBTYPE.FREE),
        (dt(22, 14), dt(22, 15), FBTYPE.FREE),
        (dt(23, 9), dt(23, 10), FBTYPE.FREE),
    ]


def test_window_must_not_be_empty():
    """The window needs to end after it starts."""
    with pytest.raises(InvalidCalendar):
        FreeBusy.from_availability(None, start=dt(22), end=dt(21))


def test_busy_period_must_not_be_empty():
    """A busy period needs to end after it starts."""
    with pytest.raises(InvalidCalendar):
        FreeBusy.from_availability(
            None, [(dt(21, 10), dt(21, 9))], start=dt(21), end=dt(22)
        )
