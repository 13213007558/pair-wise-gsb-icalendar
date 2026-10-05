"""Tests for the read-only recurrence instance iterator."""

import os
import time
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from icalendar import Calendar, Event
from icalendar.recurrence import (
    DiagnosticKind,
    InstanceSource,
    StopReason,
    iter_instances,
)
from icalendar.timezone import tzp as _tzp

UTC = timezone.utc


@pytest.fixture(autouse=True)
def _clean_timezone_cache():
    """Reset the global timezone cache after each test.

    Resolved timezones are cached globally; doctests running later
    expect freshly created timezone objects.
    """
    yield
    _tzp.use_default()


def expand(ics, start, end, tz="UTC"):
    """Expand a calendar and return the instances and the iterator."""
    calendar = Calendar.from_ical(ics)
    iterator = iter_instances(calendar, start, end, tz=tz)
    return list(iterator), iterator


MONTH_END = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:month-end
DTSTART:20240131T090000Z
RRULE:FREQ=MONTHLY;COUNT=4
END:VEVENT
END:VCALENDAR
"""

MONTH_END_LAST_DAY = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:month-end-last
DTSTART:20240131T090000Z
RRULE:FREQ=MONTHLY;COUNT=3;BYMONTHDAY=-1
END:VEVENT
END:VCALENDAR
"""

DST_GAP = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:dst-gap
DTSTART;TZID=Europe/Berlin:20240330T023000
RRULE:FREQ=DAILY;COUNT=3
END:VEVENT
END:VCALENDAR
"""

DST_FOLD = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:dst-fold
DTSTART;TZID=Europe/Berlin:20241026T023000
RRULE:FREQ=DAILY;COUNT=3
END:VEVENT
END:VCALENDAR
"""

OVERLAP = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:overlap
DTSTART:20240101T090000Z
RRULE:FREQ=DAILY;COUNT=3
RDATE:20240102T090000Z
RDATE:20240105T090000Z
END:VEVENT
END:VCALENDAR
"""

EXDATE = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:exdate
DTSTART:20240101T090000Z
RRULE:FREQ=DAILY;COUNT=4
EXDATE:20240102T090000Z
END:VEVENT
END:VCALENDAR
"""

MOVED = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:moved
DTSTART:20240301T090000Z
RRULE:FREQ=DAILY;COUNT=4
END:VEVENT
BEGIN:VEVENT
UID:moved
RECURRENCE-ID:20240302T090000Z
DTSTART:20240301T200000Z
SUMMARY:Moved meeting
END:VEVENT
END:VCALENDAR
"""

CANCELLED = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:cancelled
DTSTART:20240301T090000Z
RRULE:FREQ=DAILY;COUNT=4
END:VEVENT
BEGIN:VEVENT
UID:cancelled
RECURRENCE-ID:20240303T090000Z
DTSTART:20240303T090000Z
STATUS:CANCELLED
END:VEVENT
END:VCALENDAR
"""

FLOATING = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:floating
DTSTART:20240601T090000
RRULE:FREQ=DAILY;COUNT=2
END:VEVENT
END:VCALENDAR
"""

ALL_DAY = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:all-day
DTSTART;VALUE=DATE:20240101
RRULE:FREQ=WEEKLY;COUNT=2
END:VEVENT
END:VCALENDAR
"""

INFINITE = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:infinite
DTSTART:20240101T090000Z
RRULE:FREQ=DAILY
END:VEVENT
END:VCALENDAR
"""

UNTIL = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:until
DTSTART:20240101T090000Z
RRULE:FREQ=DAILY;UNTIL=20240104T090000Z
END:VEVENT
END:VCALENDAR
"""

NO_FREQ = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:no-freq
DTSTART:20240101T090000Z
RRULE:COUNT=3
END:VEVENT
END:VCALENDAR
"""

RSCALE = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:rscale
DTSTART:20240101T090000Z
RRULE:FREQ=DAILY;COUNT=2;RSCALE=CHINESE
END:VEVENT
END:VCALENDAR
"""

ORPHAN = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:orphan
RECURRENCE-ID:20240302T090000Z
DTSTART:20240302T100000Z
END:VEVENT
END:VCALENDAR
"""

EXCLUDED_EXCEPTION = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:excluded
DTSTART:20240101T090000Z
RRULE:FREQ=DAILY;COUNT=3
EXDATE:20240102T090000Z
END:VEVENT
BEGIN:VEVENT
UID:excluded
RECURRENCE-ID:20240102T090000Z
DTSTART:20240102T100000Z
END:VEVENT
END:VCALENDAR
"""

TWO_RRULES = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:two-rrules
DTSTART:20240101T090000Z
RRULE:FREQ=DAILY;COUNT=2
RRULE:FREQ=WEEKLY;COUNT=2
END:VEVENT
END:VCALENDAR
"""


class TestMonthEnd:
    def test_monthly_from_31st_skips_short_months(self):
        instances, _ = expand(MONTH_END, "2024-01-01", "2025-01-01")
        assert [i.identity for i in instances] == [
            datetime(2024, 1, 31, 9, tzinfo=UTC),
            datetime(2024, 3, 31, 9, tzinfo=UTC),
            datetime(2024, 5, 31, 9, tzinfo=UTC),
            datetime(2024, 7, 31, 9, tzinfo=UTC),
        ]
        assert [i.source for i in instances] == [
            InstanceSource.DTSTART,
            InstanceSource.RRULE,
            InstanceSource.RRULE,
            InstanceSource.RRULE,
        ]

    def test_last_day_of_month(self):
        instances, _ = expand(MONTH_END_LAST_DAY, "2024-01-01", "2024-04-01")
        assert [i.identity for i in instances] == [
            datetime(2024, 1, 31, 9, tzinfo=UTC),
            datetime(2024, 2, 29, 9, tzinfo=UTC),
            datetime(2024, 3, 31, 9, tzinfo=UTC),
        ]


class TestDST:
    def test_nonexistent_time_in_gap(self):
        instances, _ = expand(DST_GAP, "2024-03-29", "2024-04-03", tz="Europe/Berlin")
        assert [i.identity.replace(tzinfo=None) for i in instances] == [
            datetime(2024, 3, 30, 2, 30),
            datetime(2024, 3, 31, 2, 30),
            datetime(2024, 4, 1, 2, 30),
        ]
        kinds = [[d.kind for d in i.diagnostics] for i in instances]
        assert kinds == [[], [DiagnosticKind.NONEXISTENT_TIME], []]

    def test_ambiguous_time_in_fold(self):
        instances, _ = expand(DST_FOLD, "2024-10-25", "2024-10-29", tz="Europe/Berlin")
        kinds = [[d.kind for d in i.diagnostics] for i in instances]
        assert kinds == [[], [DiagnosticKind.AMBIGUOUS_TIME], []]


class TestMergeAndDedupe:
    def test_rdate_overlapping_rrule_is_deduplicated(self):
        instances, _ = expand(OVERLAP, "2024-01-01", "2024-02-01")
        assert [i.identity for i in instances] == [
            datetime(2024, 1, 1, 9, tzinfo=UTC),
            datetime(2024, 1, 2, 9, tzinfo=UTC),
            datetime(2024, 1, 3, 9, tzinfo=UTC),
            datetime(2024, 1, 5, 9, tzinfo=UTC),
        ]
        assert instances[1].source is InstanceSource.RRULE
        assert instances[3].source is InstanceSource.RDATE

    def test_exdate_removes_occurrence(self):
        instances, _ = expand(EXDATE, "2024-01-01", "2024-02-01")
        assert [i.identity for i in instances] == [
            datetime(2024, 1, 1, 9, tzinfo=UTC),
            datetime(2024, 1, 3, 9, tzinfo=UTC),
            datetime(2024, 1, 4, 9, tzinfo=UTC),
        ]

    def test_multiple_rrules_are_merged(self):
        instances, iterator = expand(TWO_RRULES, "2024-01-01", "2024-02-01")
        assert [i.identity for i in instances] == [
            datetime(2024, 1, 1, 9, tzinfo=UTC),
            datetime(2024, 1, 2, 9, tzinfo=UTC),
            datetime(2024, 1, 8, 9, tzinfo=UTC),
        ]
        assert len(iterator.terminations) == 2


class TestExceptions:
    def test_moved_exception_keeps_identity(self):
        instances, _ = expand(MOVED, "2024-03-01", "2024-03-10")
        assert [i.identity for i in instances] == [
            datetime(2024, 3, 1, 9, tzinfo=UTC),
            datetime(2024, 3, 2, 9, tzinfo=UTC),
            datetime(2024, 3, 3, 9, tzinfo=UTC),
            datetime(2024, 3, 4, 9, tzinfo=UTC),
        ]
        moved = instances[1]
        assert moved.moved
        assert moved.start == datetime(2024, 3, 1, 20, tzinfo=UTC)
        assert moved.component["SUMMARY"] == "Moved meeting"
        assert not instances[0].moved

    def test_cancelled_exception_produces_no_instance(self):
        instances, _ = expand(CANCELLED, "2024-03-01", "2024-03-10")
        assert [i.identity for i in instances] == [
            datetime(2024, 3, 1, 9, tzinfo=UTC),
            datetime(2024, 3, 2, 9, tzinfo=UTC),
            datetime(2024, 3, 4, 9, tzinfo=UTC),
        ]

    def test_exdate_beats_exception(self):
        instances, iterator = expand(EXCLUDED_EXCEPTION, "2024-01-01", "2024-02-01")
        assert [i.identity for i in instances] == [
            datetime(2024, 1, 1, 9, tzinfo=UTC),
            datetime(2024, 1, 3, 9, tzinfo=UTC),
        ]
        kinds = [d.kind for d in iterator.diagnostics]
        assert kinds == [DiagnosticKind.EXCLUDED_EXCEPTION]

    def test_orphan_exception_is_diagnosed(self):
        instances, iterator = expand(ORPHAN, "2024-03-01", "2024-03-10")
        assert instances == []
        kinds = [d.kind for d in iterator.diagnostics]
        assert kinds == [DiagnosticKind.ORPHAN_EXCEPTION]


class TestIdentity:
    def test_identity_used_for_update(self):
        calendar = Calendar.from_ical(MOVED)
        instances = list(iter_instances(calendar, "2024-03-01", "2024-03-10", tz="UTC"))
        target = instances[2]
        exception = Event()
        exception.add("UID", "moved")
        exception.add("RECURRENCE-ID", target.recurrence_id)
        exception.add("DTSTART", datetime(2024, 3, 3, 16, tzinfo=UTC))
        calendar.add_component(exception)
        updated = list(iter_instances(calendar, "2024-03-01", "2024-03-10", tz="UTC"))
        assert updated[2].identity == datetime(2024, 3, 3, 9, tzinfo=UTC)
        assert updated[2].start == datetime(2024, 3, 3, 16, tzinfo=UTC)
        assert updated[2].moved

    def test_key_is_hashable_and_stable(self):
        first, _ = expand(MOVED, "2024-03-01", "2024-03-10")
        second, _ = expand(MOVED, "2024-03-01", "2024-03-10")
        lookup = {instance.key: instance for instance in first}
        expected = [i.identity for i in first]
        assert [lookup[i.key].identity for i in second] == expected


class TestTermination:
    def test_count_termination(self):
        _, iterator = expand(MONTH_END, "2024-01-01", "2025-01-01")
        assert [t.reason for t in iterator.terminations] == [StopReason.COUNT]

    def test_until_termination(self):
        instances, iterator = expand(UNTIL, "2024-01-01", "2024-03-01")
        assert [i.identity for i in instances] == [
            datetime(2024, 1, day, 9, tzinfo=UTC) for day in range(1, 5)
        ]
        assert [t.reason for t in iterator.terminations] == [StopReason.UNTIL]

    def test_infinite_rrule_stops_at_window(self):
        instances, iterator = expand(INFINITE, "2024-01-01", "2024-01-11")
        assert len(instances) == 10
        assert [t.reason for t in iterator.terminations] == [StopReason.WINDOW]


class TestTimezones:
    def test_floating_uses_window_timezone(self):
        shanghai, _ = expand(FLOATING, "2024-06-01", "2024-06-03", tz="Asia/Shanghai")
        new_york, _ = expand(
            FLOATING, "2024-06-01", "2024-06-03", tz="America/New_York"
        )
        assert shanghai[0].start == datetime(2024, 6, 1, 9, 0)
        assert shanghai[0].key.sort == datetime(
            2024, 6, 1, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")
        )
        assert new_york[0].key.sort == datetime(
            2024, 6, 1, 9, 0, tzinfo=ZoneInfo("America/New_York")
        )
        assert shanghai[0].key.sort != new_york[0].key.sort

    def test_date_instances(self):
        instances, _ = expand(ALL_DAY, "2024-01-01", "2024-02-01")
        assert [i.identity for i in instances] == [
            date(2024, 1, 1),
            date(2024, 1, 8),
        ]

    def test_window_bounds(self):
        instances, _ = expand(
            INFINITE,
            datetime(2024, 1, 2, 12, tzinfo=UTC),
            datetime(2024, 1, 4, 12, tzinfo=UTC),
        )
        assert [i.identity for i in instances] == [
            datetime(2024, 1, 3, 9, tzinfo=UTC),
            datetime(2024, 1, 4, 9, tzinfo=UTC),
        ]

    def test_window_end_is_exclusive(self):
        instances, _ = expand(
            INFINITE, "2024-01-01", datetime(2024, 1, 3, 9, tzinfo=UTC)
        )
        assert [i.identity for i in instances] == [
            datetime(2024, 1, 1, 9, tzinfo=UTC),
            datetime(2024, 1, 2, 9, tzinfo=UTC),
        ]

    def test_tz_is_required(self):
        calendar = Calendar.from_ical(MONTH_END)
        with pytest.raises(TypeError):
            iter_instances(calendar, "2024-01-01", "2025-01-01")

    def test_unknown_timezone(self):
        calendar = Calendar.from_ical(MONTH_END)
        with pytest.raises(ValueError, match="Unknown timezone"):
            iter_instances(calendar, "2024-01-01", "2025-01-01", tz="Nowhere/Special")

    def test_process_timezone_is_ignored(self):
        old_tz = os.environ.get("TZ")
        os.environ["TZ"] = "Pacific/Kiritimati"
        time.tzset()
        try:
            instances, _ = expand(MONTH_END, "2024-01-01", "2025-01-01")
        finally:
            if old_tz is None:
                del os.environ["TZ"]
            else:
                os.environ["TZ"] = old_tz
            time.tzset()
        assert [i.identity for i in instances] == [
            datetime(2024, 1, 31, 9, tzinfo=UTC),
            datetime(2024, 3, 31, 9, tzinfo=UTC),
            datetime(2024, 5, 31, 9, tzinfo=UTC),
            datetime(2024, 7, 31, 9, tzinfo=UTC),
        ]


class TestDiagnostics:
    def test_missing_freq(self):
        instances, iterator = expand(NO_FREQ, "2024-01-01", "2024-02-01")
        assert [i.identity for i in instances] == [datetime(2024, 1, 1, 9, tzinfo=UTC)]
        kinds = [d.kind for d in iterator.diagnostics]
        assert kinds == [DiagnosticKind.INVALID_RULE]
        assert [t.reason for t in iterator.terminations] == [StopReason.INVALID]

    def test_unsupported_rule_part(self):
        _, iterator = expand(RSCALE, "2024-01-01", "2024-02-01")
        kinds = [d.kind for d in iterator.diagnostics]
        assert kinds == [DiagnosticKind.INVALID_RULE]
        assert "RSCALE" in iterator.diagnostics[0].message


class TestSource:
    def test_event_source(self):
        calendar = Calendar.from_ical(MONTH_END)
        event = calendar.events[0]
        instances = list(iter_instances(event, "2024-01-01", "2025-01-01", tz="UTC"))
        assert len(instances) == 4

    def test_invalid_source_type(self):
        with pytest.raises(TypeError):
            iter_instances("not a calendar", "2024-01-01", "2025-01-01", tz="UTC")

    def test_empty_window(self):
        calendar = Calendar.from_ical(MONTH_END)
        with pytest.raises(ValueError, match="window end"):
            iter_instances(calendar, "2024-01-01", "2024-01-01", tz="UTC")

    def test_iteration_does_not_modify_calendar(self):
        calendar = Calendar.from_ical(MOVED)
        before = calendar.to_ical()
        list(iter_instances(calendar, "2024-03-01", "2024-03-10", tz="UTC"))
        assert calendar.to_ical() == before
