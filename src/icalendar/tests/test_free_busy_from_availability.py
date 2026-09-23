"""Compute a VFREEBUSY component from VAVAILABILITY information.

This follows RFC 7953: AVAILABLE subcomponents are expanded into the
query window, VAVAILABILITY components are combined by priority, the busy
types are resolved, and actual busy periods are overlaid.
"""

from datetime import datetime, timedelta, timezone
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
from icalendar.cal.free_busy import compute_free_busy_intervals
from icalendar.error import InvalidCalendar
from icalendar.prop import vPeriod, vRecur

UTC = timezone.utc
NY = "America/New_York"


def _dt(*args) -> datetime:
    """A UTC datetime."""
    return datetime(*args, tzinfo=UTC)


def _periods(free_busy: FreeBusy) -> list[tuple[datetime, datetime, str]]:
    """Return (start, end, fbtype) triples for all FREEBUSY properties."""
    values = free_busy["FREEBUSY"]
    if not isinstance(values, list):
        values = [values]
    result = []
    for value in values:
        start, second = value.dt
        end = start + second if isinstance(second, timedelta) else second
        result.append((start, end, value.params["FBTYPE"]))
    return result


@pytest.fixture()
def office_hours() -> Availability:
    """Weekly Monday to Friday 9:00-17:00 in America/New_York, low priority.

    Outside the office hours the user is BUSY-UNAVAILABLE.
    """
    office = Available.new(
        start=datetime(2026, 3, 9, 9, tzinfo=ZoneInfo(NY)),
        end=datetime(2026, 3, 9, 17, tzinfo=ZoneInfo(NY)),
    )
    office.add("RRULE", vRecur.from_ical("FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"))
    return Availability.new(
        priority=9,
        start=datetime(2026, 3, 9, tzinfo=ZoneInfo(NY)),
        components=[office],
    )


@pytest.fixture()
def vacation() -> Availability:
    """Temporary high-priority leave, Wednesday-Thursday in New York."""
    return Availability.new(
        priority=1,
        busy_type=BUSYTYPE.BUSY,
        start=datetime(2026, 3, 11, tzinfo=ZoneInfo(NY)),
        end=datetime(2026, 3, 13, tzinfo=ZoneInfo(NY)),
    )


WINDOW_START = _dt(2026, 3, 9)
WINDOW_END = _dt(2026, 3, 16)


def test_weekly_hours_with_vacation_and_meetings(office_hours, vacation):
    """End-to-end scenario with recurrence, priority, busy types and periods.

    America/New_York is UTC-4 during the window (EDT), so office hours are
    13:00-21:00 UTC. The high-priority vacation (11 04:00 UTC to 13 04:00
    UTC) makes the whole block BUSY, overriding the office hours. The
    office continues to apply before and after it.
    """
    meetings = [
        # existing meeting crossing the start of the query window
        (_dt(2026, 3, 8, 23), _dt(2026, 3, 9, 1)),
        # tentative meeting given with a duration, overlapping office hours
        (_dt(2026, 3, 10, 12), timedelta(hours=2), FBTYPE.BUSY_TENTATIVE),
    ]

    intervals = compute_free_busy_intervals(
        [office_hours, vacation],
        WINDOW_START,
        WINDOW_END,
        busy_periods=meetings,
        default_busy_type=FBTYPE.BUSY,
    )

    expected = [
        (_dt(2026, 3, 9, 0), _dt(2026, 3, 9, 4), FBTYPE.BUSY),
        (_dt(2026, 3, 9, 4), _dt(2026, 3, 9, 13), FBTYPE.BUSY_UNAVAILABLE),
        (_dt(2026, 3, 9, 21), _dt(2026, 3, 10, 13), FBTYPE.BUSY_UNAVAILABLE),
        (_dt(2026, 3, 10, 13), _dt(2026, 3, 10, 14), FBTYPE.BUSY_TENTATIVE),
        (_dt(2026, 3, 10, 21), _dt(2026, 3, 11, 4), FBTYPE.BUSY_UNAVAILABLE),
        (_dt(2026, 3, 11, 4), _dt(2026, 3, 13, 4), FBTYPE.BUSY),
        (_dt(2026, 3, 13, 4), _dt(2026, 3, 13, 13), FBTYPE.BUSY_UNAVAILABLE),
        (_dt(2026, 3, 13, 21), _dt(2026, 3, 16, 0), FBTYPE.BUSY_UNAVAILABLE),
    ]
    assert intervals == expected

    # non-overlapping, ordered, half-open UTC intervals
    assert all(start < end for start, end, _ in intervals)
    assert all(
        intervals[i][1] <= intervals[i + 1][0] for i in range(len(intervals) - 1)
    )
    assert all(start.tzinfo == UTC and end.tzinfo == UTC for start, end, _ in intervals)


def test_result_serializes_and_reads_back(office_hours, vacation):
    """The serialized ICS keeps the window and every endpoint on read-back."""
    meetings = [
        (_dt(2026, 3, 8, 23), _dt(2026, 3, 9, 1)),
        (_dt(2026, 3, 10, 12), timedelta(hours=2), FBTYPE.BUSY_TENTATIVE),
    ]
    free_busy = FreeBusy.from_availability(
        [office_hours, vacation],
        WINDOW_START,
        WINDOW_END,
        busy_periods=meetings,
        default_busy_type=FBTYPE.BUSY,
    )

    read_back = FreeBusy.from_ical(free_busy.to_ical())

    assert read_back.DTSTART == WINDOW_START
    assert read_back.DTEND == WINDOW_END
    assert _periods(read_back) == [
        (start, end, fbtype.value)
        for start, end, fbtype in compute_free_busy_intervals(
            [office_hours, vacation],
            WINDOW_START,
            WINDOW_END,
            busy_periods=meetings,
            default_busy_type=FBTYPE.BUSY,
        )
    ]


def test_calendar_is_accepted_as_source(office_hours, vacation):
    """A Calendar is walked for VAVAILABILITY components."""
    calendar = Calendar()
    calendar.add_component(vacation)
    calendar.add_component(office_hours)
    free_busy = FreeBusy.from_availability(
        calendar, WINDOW_START, WINDOW_END, default_busy_type=FBTYPE.BUSY
    )
    assert _periods(free_busy)


def test_infinite_rrule_only_expands_in_window():
    """An infinite RRULE terminates and only covers window instances."""
    weekly = Available.new(start=_dt(2020, 1, 6, 9), end=_dt(2020, 1, 6, 17))
    weekly.add("RRULE", vRecur.from_ical("FREQ=WEEKLY"))
    availability = Availability.new(start=_dt(2020, 1, 1), components=[weekly])

    intervals = compute_free_busy_intervals(
        availability, _dt(2026, 3, 9), _dt(2026, 3, 10)
    )

    # Monday 2026-03-09 is busy except 9:00-17:00; nothing leaks past the window
    assert intervals == [
        (_dt(2026, 3, 9, 0), _dt(2026, 3, 9, 9), FBTYPE.BUSY_UNAVAILABLE),
        (_dt(2026, 3, 9, 17), _dt(2026, 3, 10, 0), FBTYPE.BUSY_UNAVAILABLE),
    ]


def test_exdate_removes_an_available_instance():
    """EXDATE makes the excluded instance fall back to the busy type."""
    office = Available.new(start=_dt(2026, 3, 9, 9), end=_dt(2026, 3, 9, 17))
    office.add("RRULE", vRecur.from_ical("FREQ=WEEKLY;BYDAY=MO,TU,WE"))
    office.add("EXDATE", _dt(2026, 3, 10, 9))
    availability = Availability.new(start=_dt(2026, 3, 9), components=[office])

    intervals = compute_free_busy_intervals(
        availability, _dt(2026, 3, 9), _dt(2026, 3, 12)
    )

    assert intervals == [
        (_dt(2026, 3, 9, 0), _dt(2026, 3, 9, 9), FBTYPE.BUSY_UNAVAILABLE),
        (_dt(2026, 3, 9, 17), _dt(2026, 3, 11, 9), FBTYPE.BUSY_UNAVAILABLE),
        (_dt(2026, 3, 11, 17), _dt(2026, 3, 12, 0), FBTYPE.BUSY_UNAVAILABLE),
    ]


def test_unbounded_availability_covers_the_whole_window():
    """A VAVAILABILITY without DTSTART/DTEND covers the complete window."""
    office = Available.new(start=_dt(2026, 3, 10, 10), end=_dt(2026, 3, 10, 12))
    availability = Availability.new(components=[office])

    intervals = compute_free_busy_intervals(
        availability, _dt(2026, 3, 10), _dt(2026, 3, 11)
    )

    assert intervals == [
        (_dt(2026, 3, 10, 0), _dt(2026, 3, 10, 10), FBTYPE.BUSY_UNAVAILABLE),
        (_dt(2026, 3, 10, 12), _dt(2026, 3, 11, 0), FBTYPE.BUSY_UNAVAILABLE),
    ]


def test_default_free_leaves_uncovered_parts_out():
    """Parts without any availability definition can default to FREE."""
    availability = Availability.new(start=_dt(2026, 3, 10), end=_dt(2026, 3, 11))
    intervals = compute_free_busy_intervals(
        availability,
        _dt(2026, 3, 9),
        _dt(2026, 3, 12),
        default_busy_type=FBTYPE.FREE,
    )

    # the covered day is busy; the uncovered days are not reported
    assert intervals == [
        (_dt(2026, 3, 10, 0), _dt(2026, 3, 11, 0), FBTYPE.BUSY_UNAVAILABLE),
    ]


def test_same_priority_availabilities_are_merged():
    """Components with the same priority merge instead of overriding."""
    first = Availability.new(start=_dt(2026, 3, 10, 9), end=_dt(2026, 3, 10, 12))
    second = Availability.new(
        busy_type=BUSYTYPE.BUSY,
        start=_dt(2026, 3, 10, 10),
        end=_dt(2026, 3, 10, 14),
    )

    intervals = compute_free_busy_intervals(
        [first, second], _dt(2026, 3, 10, 8), _dt(2026, 3, 10, 15)
    )

    # the overlap at 10:00-12:00 keeps the higher busy type precedence,
    # and adjacent intervals of the same type are merged
    assert intervals == [
        (_dt(2026, 3, 10, 8), _dt(2026, 3, 10, 9), FBTYPE.BUSY),
        (_dt(2026, 3, 10, 9), _dt(2026, 3, 10, 12), FBTYPE.BUSY_UNAVAILABLE),
        (_dt(2026, 3, 10, 12), _dt(2026, 3, 10, 15), FBTYPE.BUSY),
    ]


def test_adjacent_same_type_intervals_are_merged():
    """Adjacent busy intervals with the same type become one interval."""
    intervals = compute_free_busy_intervals(
        [],
        _dt(2026, 3, 10),
        _dt(2026, 3, 11),
        busy_periods=[
            (_dt(2026, 3, 10, 9), _dt(2026, 3, 10, 10)),
            (_dt(2026, 3, 10, 10), _dt(2026, 3, 10, 11)),
        ],
        default_busy_type=FBTYPE.FREE,
    )
    assert intervals == [
        (_dt(2026, 3, 10, 9), _dt(2026, 3, 10, 11), FBTYPE.BUSY),
    ]


def test_vperiod_and_duration_busy_periods():
    """Busy periods accept vPeriod objects, end times, and durations."""
    intervals = compute_free_busy_intervals(
        [],
        _dt(2026, 3, 10),
        _dt(2026, 3, 11),
        busy_periods=[
            vPeriod((_dt(2026, 3, 10, 9), _dt(2026, 3, 10, 10))),
            vPeriod(
                (_dt(2026, 3, 10, 12), timedelta(hours=1)),
                params={"FBTYPE": "BUSY-TENTATIVE"},
            ),
            (_dt(2026, 3, 10, 15), timedelta(minutes=30), "BUSY-UNAVAILABLE"),
        ],
        default_busy_type=FBTYPE.FREE,
    )
    assert intervals == [
        (_dt(2026, 3, 10, 9), _dt(2026, 3, 10, 10), FBTYPE.BUSY),
        (_dt(2026, 3, 10, 12), _dt(2026, 3, 10, 13), FBTYPE.BUSY_TENTATIVE),
        (_dt(2026, 3, 10, 15), _dt(2026, 3, 10, 15, 30), FBTYPE.BUSY_UNAVAILABLE),
    ]


def test_floating_time_requires_an_explicit_timezone():
    """Floating times must not fall back to the process timezone."""
    office = Available()
    office.add("DTSTART", datetime(2026, 3, 9, 9))
    office.add("DTEND", datetime(2026, 3, 9, 17))
    availability = Availability()
    availability.add("DTSTART", datetime(2026, 3, 9))
    availability.add_component(office)

    with pytest.raises(ValueError, match="floating_tz"):
        compute_free_busy_intervals(
            availability, datetime(2026, 3, 9), datetime(2026, 3, 10)
        )

    intervals = compute_free_busy_intervals(
        availability,
        datetime(2026, 3, 9),
        datetime(2026, 3, 10),
        floating_tz="America/New_York",
    )
    # the floating window and times are read in New York (UTC-4, EDT):
    # the window is 04:00 UTC to 04:00 UTC and 9:00-17:00 is 13:00-21:00 UTC
    assert intervals == [
        (_dt(2026, 3, 9, 4), _dt(2026, 3, 9, 13), FBTYPE.BUSY_UNAVAILABLE),
        (_dt(2026, 3, 9, 21), _dt(2026, 3, 10, 4), FBTYPE.BUSY_UNAVAILABLE),
    ]


def test_floating_busy_period_requires_an_explicit_timezone():
    """A floating busy period also needs floating_tz."""
    with pytest.raises(ValueError, match="floating_tz"):
        compute_free_busy_intervals(
            [],
            _dt(2026, 3, 10),
            _dt(2026, 3, 11),
            busy_periods=[(datetime(2026, 3, 10, 9), timedelta(hours=1))],
        )


def test_unknown_floating_timezone():
    """An unknown TZID is reported."""
    with pytest.raises(ValueError, match="Unknown timezone"):
        compute_free_busy_intervals(
            [],
            datetime(2026, 3, 10),
            datetime(2026, 3, 11),
            floating_tz="Not/AZone",
        )


def test_window_must_not_be_empty_or_inverted():
    """The query window must have a positive duration."""
    with pytest.raises(ValueError, match="query window"):
        compute_free_busy_intervals([], _dt(2026, 3, 11), _dt(2026, 3, 10))


def test_busy_period_must_not_be_inverted():
    """A busy period must have a positive duration."""
    with pytest.raises(ValueError, match="Busy period"):
        compute_free_busy_intervals(
            [],
            _dt(2026, 3, 10),
            _dt(2026, 3, 11),
            busy_periods=[(_dt(2026, 3, 10, 10), _dt(2026, 3, 10, 9))],
        )


def test_available_without_dtstart_is_invalid():
    """An AVAILABLE component without DTSTART cannot be expanded."""
    availability = Availability.new(start=_dt(2026, 3, 9))
    availability.add_component(Available())

    with pytest.raises(InvalidCalendar, match="DTSTART"):
        compute_free_busy_intervals(availability, _dt(2026, 3, 9), _dt(2026, 3, 10))
