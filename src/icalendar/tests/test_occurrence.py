"""Acceptance tests for windowed occurrence queries.

These cover the combination of DTSTART, RRULE, RDATE, EXDATE and detached
VEVENT exceptions, including DST transitions, cross-day events, single
reschedules and RANGE=THISANDFUTURE reschedules.
"""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from icalendar import Calendar
from icalendar.error import FloatingTimeError, UnsupportedRecurrence
from icalendar.occurrence import Occurrences, occurrences_between
from icalendar.timezone import tzp

UTC = timezone.utc
NEW_YORK = ZoneInfo("America/New_York")
BERLIN = ZoneInfo("Europe/Berlin")


@pytest.fixture(autouse=True, scope="module")
def clean_timezone_cache():
    """Reset the shared timezone provider cache around these tests.

    Parsing TZIDs fills the cache of the global timezone provider; other
    tests (e.g. doctests) rely on a clean cache.
    """
    tzp.use_default()
    yield
    tzp.use_default()


def parse_calendar(*components: str) -> Calendar:
    """Build a calendar from VEVENT component bodies."""
    ics = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//test//occurrences//EN\r\n"
        + "".join(components)
        + "END:VCALENDAR\r\n"
    )
    return Calendar.from_ical(ics)


def weekly_meeting() -> Calendar:
    """A weekly Monday 10:00-11:00 New York meeting with 6 instances."""
    return parse_calendar(
        "BEGIN:VEVENT\r\n"
        "UID:weekly\r\n"
        "DTSTAMP:20240101T000000Z\r\n"
        "DTSTART;TZID=America/New_York:20240304T100000\r\n"
        "DTEND;TZID=America/New_York:20240304T110000\r\n"
        "RRULE:FREQ=WEEKLY;COUNT=6\r\n"
        "END:VEVENT\r\n"
    )


def detached(
    rid: str, start: str, end: str, *, range_: str = "", status: str = ""
) -> str:
    """Create a detached VEVENT body for the weekly meeting."""
    range_part = f";RANGE={range_}" if range_ else ""
    status_line = f"STATUS:{status}\r\n" if status else ""
    return (
        "BEGIN:VEVENT\r\n"
        "UID:weekly\r\n"
        "DTSTAMP:20240101T000000Z\r\n"
        f"RECURRENCE-ID;TZID=America/New_York{range_part}:{rid}\r\n"
        f"DTSTART;TZID=America/New_York:{start}\r\n"
        f"DTEND;TZID=America/New_York:{end}\r\n"
        f"{status_line}"
        "END:VEVENT\r\n"
    )


MARCH = (datetime(2024, 3, 1, tzinfo=UTC), datetime(2024, 5, 1, tzinfo=UTC))


class TestBasicExpansion:
    """DTSTART + RRULE form the recurrence set with stable identities."""

    def test_weekly_instances_with_identity_and_source(self):
        calendar = weekly_meeting()
        occurrences = calendar.occurrences_between(*MARCH)
        assert isinstance(occurrences, Occurrences)
        assert occurrences.complete
        assert len(occurrences) == 6
        master = calendar.events[0]
        for occurrence in occurrences:
            assert occurrence.recurrence_id == occurrence.start
            assert occurrence.component is master
            assert not occurrence.modified
            assert occurrence.uid == "weekly"
            assert occurrence.duration == timedelta(hours=1)

    def test_occurrences_sorted_by_start(self):
        calendar = weekly_meeting()
        occurrences = calendar.occurrences_between(*MARCH)
        starts = [o.start for o in occurrences]
        assert starts == sorted(starts)

    def test_single_event_without_rrule(self):
        calendar = parse_calendar(
            "BEGIN:VEVENT\r\n"
            "UID:single\r\n"
            "DTSTAMP:20240101T000000Z\r\n"
            "DTSTART:20240305T100000Z\r\n"
            "DTEND:20240305T110000Z\r\n"
            "END:VEVENT\r\n"
        )
        occurrences = calendar.occurrences_between(*MARCH)
        assert len(occurrences) == 1
        assert occurrences[0].start == datetime(2024, 3, 5, 10, tzinfo=UTC)
        assert occurrences[0].recurrence_id == occurrences[0].start

    def test_window_without_instances_is_empty_and_complete(self):
        calendar = weekly_meeting()
        occurrences = calendar.occurrences_between(
            datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 2, 1, tzinfo=UTC)
        )
        assert list(occurrences) == []
        assert occurrences.complete


class TestWindowIntersection:
    """The window is left-closed, right-open and matched by intersection."""

    @pytest.fixture
    def calendar(self):
        return parse_calendar(
            "BEGIN:VEVENT\r\n"
            "UID:one\r\n"
            "DTSTAMP:20240101T000000Z\r\n"
            "DTSTART:20240305T100000Z\r\n"
            "DTEND:20240305T110000Z\r\n"
            "END:VEVENT\r\n"
        )

    def test_instance_starting_at_window_start_is_included(self, calendar):
        occurrences = calendar.occurrences_between(
            datetime(2024, 3, 5, 10, tzinfo=UTC), datetime(2024, 3, 6, tzinfo=UTC)
        )
        assert len(occurrences) == 1

    def test_instance_ending_at_window_start_is_excluded(self, calendar):
        occurrences = calendar.occurrences_between(
            datetime(2024, 3, 5, 11, tzinfo=UTC), datetime(2024, 3, 6, tzinfo=UTC)
        )
        assert list(occurrences) == []

    def test_instance_starting_at_window_end_is_excluded(self, calendar):
        occurrences = calendar.occurrences_between(
            datetime(2024, 3, 5, 9, tzinfo=UTC), datetime(2024, 3, 5, 10, tzinfo=UTC)
        )
        assert list(occurrences) == []

    def test_instance_overlapping_window_end_is_included(self, calendar):
        occurrences = calendar.occurrences_between(
            datetime(2024, 3, 5, 9, tzinfo=UTC),
            datetime(2024, 3, 5, 10, 30, tzinfo=UTC),
        )
        assert len(occurrences) == 1

    def test_instance_covering_the_whole_window_is_included(self, calendar):
        occurrences = calendar.occurrences_between(
            datetime(2024, 3, 5, 10, 15, tzinfo=UTC),
            datetime(2024, 3, 5, 10, 45, tzinfo=UTC),
        )
        assert len(occurrences) == 1

    def test_zero_duration_instance_inside_window(self):
        calendar = parse_calendar(
            "BEGIN:VEVENT\r\n"
            "UID:point\r\n"
            "DTSTAMP:20240101T000000Z\r\n"
            "DTSTART:20240305T100000Z\r\n"
            "END:VEVENT\r\n"
        )
        inside = calendar.occurrences_between(
            datetime(2024, 3, 5, 10, tzinfo=UTC), datetime(2024, 3, 5, 11, tzinfo=UTC)
        )
        assert len(inside) == 1
        outside = calendar.occurrences_between(
            datetime(2024, 3, 5, 11, tzinfo=UTC), datetime(2024, 3, 5, 12, tzinfo=UTC)
        )
        assert list(outside) == []

    def test_cross_day_event_found_via_second_day(self):
        calendar = parse_calendar(
            "BEGIN:VEVENT\r\n"
            "UID:late\r\n"
            "DTSTAMP:20240101T000000Z\r\n"
            "DTSTART:20240305T220000Z\r\n"
            "DTEND:20240306T010000Z\r\n"
            "END:VEVENT\r\n"
        )
        # The window only covers the morning after the event started.
        occurrences = calendar.occurrences_between(
            datetime(2024, 3, 6, 0, tzinfo=UTC), datetime(2024, 3, 6, 6, tzinfo=UTC)
        )
        assert len(occurrences) == 1
        assert occurrences[0].start == datetime(2024, 3, 5, 22, tzinfo=UTC)
        assert occurrences[0].end == datetime(2024, 3, 6, 1, tzinfo=UTC)

    def test_cross_day_event_not_found_before_it_starts(self):
        calendar = parse_calendar(
            "BEGIN:VEVENT\r\n"
            "UID:late\r\n"
            "DTSTAMP:20240101T000000Z\r\n"
            "DTSTART:20240305T220000Z\r\n"
            "DTEND:20240306T010000Z\r\n"
            "END:VEVENT\r\n"
        )
        occurrences = calendar.occurrences_between(
            datetime(2024, 3, 5, 12, tzinfo=UTC), datetime(2024, 3, 5, 21, tzinfo=UTC)
        )
        assert list(occurrences) == []

    def test_invalid_window_rejected(self):
        calendar = weekly_meeting()
        moment = datetime(2024, 3, 5, tzinfo=UTC)
        with pytest.raises(ValueError, match="before"):
            calendar.occurrences_between(moment, moment)


class TestExdateAndRdate:
    """EXDATE removes identities, RDATE adds them."""

    def test_exdate_removes_instance(self):
        calendar = weekly_meeting()
        calendar.events[0].add("EXDATE", datetime(2024, 3, 11, 10, tzinfo=NEW_YORK))
        occurrences = calendar.occurrences_between(*MARCH)
        assert len(occurrences) == 5
        assert datetime(2024, 3, 11, 10, tzinfo=NEW_YORK) not in [
            o.recurrence_id for o in occurrences
        ]

    def test_rdate_adds_instance(self):
        calendar = weekly_meeting()
        calendar.events[0].add("RDATE", datetime(2024, 3, 13, 18, tzinfo=NEW_YORK))
        occurrences = calendar.occurrences_between(*MARCH)
        assert len(occurrences) == 7
        extra = [
            o
            for o in occurrences
            if o.recurrence_id == datetime(2024, 3, 13, 18, tzinfo=NEW_YORK)
        ]
        assert len(extra) == 1
        assert extra[0].duration == timedelta(hours=1)

    def test_rdate_period_defines_its_own_end(self):
        calendar = parse_calendar(
            "BEGIN:VEVENT\r\n"
            "UID:periods\r\n"
            "DTSTAMP:20240101T000000Z\r\n"
            "DTSTART:20240301T100000Z\r\n"
            "DTEND:20240301T110000Z\r\n"
            "RDATE;VALUE=PERIOD:20240305T100000Z/20240305T123000Z\r\n"
            "END:VEVENT\r\n"
        )
        occurrences = calendar.occurrences_between(*MARCH)
        assert len(occurrences) == 2
        period = [
            o
            for o in occurrences
            if o.recurrence_id == datetime(2024, 3, 5, 10, tzinfo=UTC)
        ]
        assert len(period) == 1
        assert period[0].end == datetime(2024, 3, 5, 12, 30, tzinfo=UTC)


class TestDetachedExceptions:
    """Detached VEVENTs override, cancel and move single instances."""

    def test_single_instance_moved(self):
        calendar = parse_calendar(
            "BEGIN:VEVENT\r\n"
            "UID:weekly\r\n"
            "DTSTAMP:20240101T000000Z\r\n"
            "DTSTART;TZID=America/New_York:20240304T100000\r\n"
            "DTEND;TZID=America/New_York:20240304T110000\r\n"
            "RRULE:FREQ=WEEKLY;COUNT=6\r\n"
            "END:VEVENT\r\n",
            detached("20240311T100000", "20240312T140000", "20240312T150000"),
        )
        occurrences = calendar.occurrences_between(*MARCH)
        assert occurrences.complete
        assert len(occurrences) == 6
        moved = [o for o in occurrences if o.modified]
        assert len(moved) == 1
        moved = moved[0]
        # The identity stays the original Monday 10:00 ...
        assert moved.recurrence_id == datetime(2024, 3, 11, 10, tzinfo=NEW_YORK)
        # ... while the instance actually happens on Tuesday 14:00.
        assert moved.start == datetime(2024, 3, 12, 14, tzinfo=NEW_YORK)
        assert moved.end == datetime(2024, 3, 12, 15, tzinfo=NEW_YORK)
        assert moved.component is calendar.events[1]
        assert "RECURRENCE-ID" in moved.component
        # The original slot is empty now.
        assert datetime(2024, 3, 11, 10, tzinfo=NEW_YORK) not in [
            o.start for o in occurrences
        ]

    def test_cancelled_exception_is_not_returned(self):
        calendar = parse_calendar(
            "BEGIN:VEVENT\r\n"
            "UID:weekly\r\n"
            "DTSTAMP:20240101T000000Z\r\n"
            "DTSTART;TZID=America/New_York:20240304T100000\r\n"
            "DTEND;TZID=America/New_York:20240304T110000\r\n"
            "RRULE:FREQ=WEEKLY;COUNT=6\r\n"
            "END:VEVENT\r\n",
            detached(
                "20240318T100000",
                "20240318T100000",
                "20240318T110000",
                status="CANCELLED",
            ),
        )
        occurrences = calendar.occurrences_between(*MARCH)
        assert len(occurrences) == 5
        assert datetime(2024, 3, 18, 10, tzinfo=NEW_YORK) not in [
            o.recurrence_id for o in occurrences
        ]

    def test_instance_moved_out_of_the_window(self):
        calendar = parse_calendar(
            "BEGIN:VEVENT\r\n"
            "UID:weekly\r\n"
            "DTSTAMP:20240101T000000Z\r\n"
            "DTSTART;TZID=America/New_York:20240304T100000\r\n"
            "DTEND;TZID=America/New_York:20240304T110000\r\n"
            "RRULE:FREQ=WEEKLY;COUNT=6\r\n"
            "END:VEVENT\r\n",
            detached("20240311T100000", "20240611T140000", "20240611T150000"),
        )
        # March/April no longer contain the moved instance ...
        assert len(calendar.occurrences_between(*MARCH)) == 5
        # ... it shows up in June instead, with its original identity.
        june = calendar.occurrences_between(
            datetime(2024, 6, 1, tzinfo=UTC), datetime(2024, 7, 1, tzinfo=UTC)
        )
        assert len(june) == 1
        assert june[0].recurrence_id == datetime(2024, 3, 11, 10, tzinfo=NEW_YORK)
        assert june[0].start == datetime(2024, 6, 11, 14, tzinfo=NEW_YORK)

    def test_instance_of_infinite_rule_moved_into_the_window(self):
        calendar = parse_calendar(
            "BEGIN:VEVENT\r\n"
            "UID:weekly\r\n"
            "DTSTAMP:20240101T000000Z\r\n"
            "DTSTART;TZID=America/New_York:20240101T100000\r\n"
            "DTEND;TZID=America/New_York:20240101T110000\r\n"
            "RRULE:FREQ=WEEKLY\r\n"
            "END:VEVENT\r\n",
            detached("20240108T100000", "20240611T140000", "20240611T150000"),
        )
        june = calendar.occurrences_between(
            datetime(2024, 6, 1, tzinfo=UTC), datetime(2024, 7, 1, tzinfo=UTC)
        )
        assert june.complete
        # 4 regular Monday instances plus the one moved in from January.
        assert len(june) == 5
        moved = [o for o in june if o.modified]
        assert len(moved) == 1
        assert moved[0].recurrence_id == datetime(2024, 1, 8, 10, tzinfo=NEW_YORK)
        assert moved[0].start == datetime(2024, 6, 11, 14, tzinfo=NEW_YORK)
        # January no longer contains the moved instance.
        january = calendar.occurrences_between(
            datetime(2024, 1, 8, tzinfo=UTC), datetime(2024, 1, 9, tzinfo=UTC)
        )
        assert list(january) == []


class TestThisAndFuture:
    """RANGE=THISANDFUTURE adjusts start and duration of the whole tail."""

    @pytest.fixture
    def calendar(self):
        return parse_calendar(
            "BEGIN:VEVENT\r\n"
            "UID:weekly\r\n"
            "DTSTAMP:20240101T000000Z\r\n"
            "DTSTART;TZID=America/New_York:20240304T100000\r\n"
            "DTEND;TZID=America/New_York:20240304T110000\r\n"
            "RRULE:FREQ=WEEKLY;COUNT=6\r\n"
            "END:VEVENT\r\n",
            detached(
                "20240318T100000",
                "20240318T103000",
                "20240318T111500",
                range_="THISANDFUTURE",
            ),
        )

    def test_tail_is_shifted_with_new_duration(self, calendar):
        occurrences = calendar.occurrences_between(*MARCH)
        assert len(occurrences) == 6
        head = [
            o
            for o in occurrences
            if o.recurrence_id < datetime(2024, 3, 18, 10, tzinfo=NEW_YORK)
        ]
        tail = [
            o
            for o in occurrences
            if o.recurrence_id >= datetime(2024, 3, 18, 10, tzinfo=NEW_YORK)
        ]
        assert len(head) == 2
        assert len(tail) == 4
        for occurrence in head:
            assert occurrence.start == occurrence.recurrence_id
            assert occurrence.duration == timedelta(hours=1)
            assert not occurrence.modified
        for occurrence in tail:
            # Identity stays at the original 10:00 start ...
            assert occurrence.recurrence_id.time() == datetime(2024, 3, 18, 10).time()
            # ... while the instance actually starts 30 minutes later and
            # lasts 45 minutes.
            assert occurrence.start == occurrence.recurrence_id + timedelta(minutes=30)
            assert occurrence.duration == timedelta(minutes=45)
            assert occurrence.modified

    def test_thisandfuture_instance_uses_detached_component(self, calendar):
        occurrences = calendar.occurrences_between(*MARCH)
        first = [
            o
            for o in occurrences
            if o.recurrence_id == datetime(2024, 3, 18, 10, tzinfo=NEW_YORK)
        ]
        assert len(first) == 1
        assert first[0].component is calendar.events[1]
        later = [
            o
            for o in occurrences
            if o.recurrence_id == datetime(2024, 3, 25, 10, tzinfo=NEW_YORK)
        ]
        assert len(later) == 1
        assert later[0].component is calendar.events[0]

    def test_shift_to_another_day_keeps_identity(self):
        # A Monday 23:00 meeting moved to Tuesday 01:00 from the second week.
        calendar = parse_calendar(
            "BEGIN:VEVENT\r\n"
            "UID:late-weekly\r\n"
            "DTSTAMP:20240101T000000Z\r\n"
            "DTSTART;TZID=America/New_York:20240304T230000\r\n"
            "DTEND;TZID=America/New_York:20240305T000000\r\n"
            "RRULE:FREQ=WEEKLY;COUNT=4\r\n"
            "END:VEVENT\r\n",
            (
                "BEGIN:VEVENT\r\n"
                "UID:late-weekly\r\n"
                "DTSTAMP:20240101T000000Z\r\n"
                "RECURRENCE-ID;TZID=America/New_York;RANGE=THISANDFUTURE:20240311T230000\r\n"
                "DTSTART;TZID=America/New_York:20240312T010000\r\n"
                "DTEND;TZID=America/New_York:20240312T020000\r\n"
                "END:VEVENT\r\n"
            ),
        )
        # Querying Tuesday night finds the shifted instance ...
        tuesday = calendar.occurrences_between(
            datetime(2024, 3, 26, 0, tzinfo=UTC), datetime(2024, 3, 26, 12, tzinfo=UTC)
        )
        assert len(tuesday) == 1
        occurrence = tuesday[0]
        # ... and it is still the Monday 23:00 instance, not a new one.
        assert occurrence.recurrence_id == datetime(2024, 3, 25, 23, tzinfo=NEW_YORK)
        assert occurrence.start == datetime(2024, 3, 26, 1, tzinfo=NEW_YORK)
        assert occurrence.end == datetime(2024, 3, 26, 2, tzinfo=NEW_YORK)
        # The Monday slot is empty from the second week on.
        monday = calendar.occurrences_between(
            datetime(2024, 3, 25, 12, tzinfo=UTC), datetime(2024, 3, 26, 0, tzinfo=UTC)
        )
        assert list(monday) == []
        # In total there are still exactly 4 instances.
        everything = calendar.occurrences_between(
            datetime(2024, 3, 1, tzinfo=UTC), datetime(2024, 5, 1, tzinfo=UTC)
        )
        assert len(everything) == 4

    def test_cancelled_thisandfuture_removes_the_tail(self):
        calendar = parse_calendar(
            "BEGIN:VEVENT\r\n"
            "UID:weekly\r\n"
            "DTSTAMP:20240101T000000Z\r\n"
            "DTSTART;TZID=America/New_York:20240304T100000\r\n"
            "DTEND;TZID=America/New_York:20240304T110000\r\n"
            "RRULE:FREQ=WEEKLY;COUNT=6\r\n"
            "END:VEVENT\r\n",
            detached(
                "20240318T100000",
                "20240318T100000",
                "20240318T110000",
                range_="THISANDFUTURE",
                status="CANCELLED",
            ),
        )
        occurrences = calendar.occurrences_between(*MARCH)
        assert len(occurrences) == 2

    def test_unsupported_range_is_rejected(self):
        calendar = parse_calendar(
            "BEGIN:VEVENT\r\n"
            "UID:weekly\r\n"
            "DTSTAMP:20240101T000000Z\r\n"
            "DTSTART;TZID=America/New_York:20240304T100000\r\n"
            "DTEND;TZID=America/New_York:20240304T110000\r\n"
            "RRULE:FREQ=WEEKLY;COUNT=6\r\n"
            "END:VEVENT\r\n",
            detached(
                "20240318T100000",
                "20240318T100000",
                "20240318T110000",
                range_="THISANDPRIOR",
            ),
        )
        with pytest.raises(UnsupportedRecurrence):
            calendar.occurrences_between(*MARCH)


class TestDST:
    """Local wall-clock time survives DST transitions; UTC instants move."""

    def test_spring_forward_keeps_wall_clock(self):
        calendar = parse_calendar(
            "BEGIN:VEVENT\r\n"
            "UID:dst\r\n"
            "DTSTAMP:20240101T000000Z\r\n"
            "DTSTART;TZID=America/New_York:20240304T090000\r\n"
            "DTEND;TZID=America/New_York:20240304T100000\r\n"
            "RRULE:FREQ=WEEKLY;COUNT=3\r\n"
            "END:VEVENT\r\n"
        )
        occurrences = calendar.occurrences_between(*MARCH)
        assert len(occurrences) == 3
        # 2024-03-10 was the spring forward in America/New_York.
        offsets = [o.start.utcoffset() for o in occurrences]
        assert offsets == [
            timedelta(hours=-5),
            timedelta(hours=-4),
            timedelta(hours=-4),
        ]
        assert [o.start.hour for o in occurrences] == [9, 9, 9]
        assert [o.duration for o in occurrences] == [timedelta(hours=1)] * 3

    def test_fall_back_keeps_wall_clock(self):
        calendar = parse_calendar(
            "BEGIN:VEVENT\r\n"
            "UID:dst\r\n"
            "DTSTAMP:20240101T000000Z\r\n"
            "DTSTART;TZID=America/New_York:20241028T090000\r\n"
            "DTEND;TZID=America/New_York:20241028T100000\r\n"
            "RRULE:FREQ=WEEKLY;COUNT=3\r\n"
            "END:VEVENT\r\n"
        )
        occurrences = calendar.occurrences_between(
            datetime(2024, 10, 1, tzinfo=UTC), datetime(2024, 12, 1, tzinfo=UTC)
        )
        assert len(occurrences) == 3
        # 2024-11-03 was the fall back in America/New_York.
        offsets = [o.start.utcoffset() for o in occurrences]
        assert offsets == [
            timedelta(hours=-4),
            timedelta(hours=-5),
            timedelta(hours=-5),
        ]

    def test_dst_transition_with_reschedule_mix(self):
        # Weekly Berlin meeting across the 2024-03-31 spring forward, with
        # one instance moved and the tail shifted by 30 minutes.
        calendar = parse_calendar(
            "BEGIN:VEVENT\r\n"
            "UID:berlin\r\n"
            "DTSTAMP:20240101T000000Z\r\n"
            "DTSTART;TZID=Europe/Berlin:20240325T100000\r\n"
            "DTEND;TZID=Europe/Berlin:20240325T110000\r\n"
            "RRULE:FREQ=WEEKLY;COUNT=4\r\n"
            "END:VEVENT\r\n",
            (
                "BEGIN:VEVENT\r\n"
                "UID:berlin\r\n"
                "DTSTAMP:20240101T000000Z\r\n"
                "RECURRENCE-ID;TZID=Europe/Berlin:20240401T100000\r\n"
                "DTSTART;TZID=Europe/Berlin:20240402T140000\r\n"
                "DTEND;TZID=Europe/Berlin:20240402T150000\r\n"
                "END:VEVENT\r\n"
            ),
            (
                "BEGIN:VEVENT\r\n"
                "UID:berlin\r\n"
                "DTSTAMP:20240101T000000Z\r\n"
                "RECURRENCE-ID;TZID=Europe/Berlin;RANGE=THISANDFUTURE:20240408T100000\r\n"
                "DTSTART;TZID=Europe/Berlin:20240408T103000\r\n"
                "DTEND;TZID=Europe/Berlin:20240408T113000\r\n"
                "END:VEVENT\r\n"
            ),
        )
        occurrences = calendar.occurrences_between(
            datetime(2024, 3, 1, tzinfo=UTC), datetime(2024, 5, 1, tzinfo=UTC)
        )
        assert len(occurrences) == 4
        by_identity = {o.recurrence_id: o for o in occurrences}
        first = by_identity[datetime(2024, 3, 25, 10, tzinfo=BERLIN)]
        assert first.start == datetime(2024, 3, 25, 10, tzinfo=BERLIN)
        moved = by_identity[datetime(2024, 4, 1, 10, tzinfo=BERLIN)]
        assert moved.start == datetime(2024, 4, 2, 14, tzinfo=BERLIN)
        shifted = by_identity[datetime(2024, 4, 8, 10, tzinfo=BERLIN)]
        assert shifted.start == datetime(2024, 4, 8, 10, 30, tzinfo=BERLIN)
        last = by_identity[datetime(2024, 4, 15, 10, tzinfo=BERLIN)]
        assert last.start == datetime(2024, 4, 15, 10, 30, tzinfo=BERLIN)
        # The single move wins over the tail shift for its own identity:
        # the April 1 identity is not shifted by 30 minutes but moved.
        assert moved.component is calendar.events[1]


class TestAllDayEvents:
    """All-day DATE events are handled separately from DATE-TIME events."""

    @pytest.fixture
    def calendar(self):
        return parse_calendar(
            "BEGIN:VEVENT\r\n"
            "UID:allday\r\n"
            "DTSTAMP:20240101T000000Z\r\n"
            "DTSTART;VALUE=DATE:20240601\r\n"
            "RRULE:FREQ=DAILY;COUNT=3\r\n"
            "END:VEVENT\r\n"
        )

    def test_all_day_instances_keep_date_type(self, calendar):
        occurrences = calendar.occurrences_between(date(2024, 6, 1), date(2024, 6, 10))
        assert len(occurrences) == 3
        for occurrence in occurrences:
            assert type(occurrence.start) is date
            assert type(occurrence.end) is date
            assert type(occurrence.recurrence_id) is date
            # No DTEND means the event lasts one day.
            assert occurrence.end == occurrence.start + timedelta(days=1)

    def test_all_day_window_is_half_open(self, calendar):
        occurrences = calendar.occurrences_between(date(2024, 6, 2), date(2024, 6, 3))
        assert [o.start for o in occurrences] == [date(2024, 6, 2)]

    def test_multi_day_event_overlapping_window(self):
        calendar = parse_calendar(
            "BEGIN:VEVENT\r\n"
            "UID:long\r\n"
            "DTSTAMP:20240101T000000Z\r\n"
            "DTSTART;VALUE=DATE:20240601\r\n"
            "DTEND;VALUE=DATE:20240604\r\n"
            "END:VEVENT\r\n"
        )
        occurrences = calendar.occurrences_between(date(2024, 6, 3), date(2024, 6, 5))
        assert len(occurrences) == 1
        assert occurrences[0].start == date(2024, 6, 1)
        assert occurrences[0].end == date(2024, 6, 4)

    def test_all_day_reschedule(self):
        calendar = parse_calendar(
            "BEGIN:VEVENT\r\n"
            "UID:allday\r\n"
            "DTSTAMP:20240101T000000Z\r\n"
            "DTSTART;VALUE=DATE:20240601\r\n"
            "RRULE:FREQ=DAILY;COUNT=3\r\n"
            "END:VEVENT\r\n",
            (
                "BEGIN:VEVENT\r\n"
                "UID:allday\r\n"
                "DTSTAMP:20240101T000000Z\r\n"
                "RECURRENCE-ID;VALUE=DATE:20240602\r\n"
                "DTSTART;VALUE=DATE:20240605\r\n"
                "END:VEVENT\r\n"
            ),
        )
        occurrences = calendar.occurrences_between(date(2024, 6, 1), date(2024, 6, 10))
        assert [o.start for o in occurrences] == [
            date(2024, 6, 1),
            date(2024, 6, 3),
            date(2024, 6, 5),
        ]
        moved = [o for o in occurrences if o.modified]
        assert moved[0].recurrence_id == date(2024, 6, 2)


class TestFloatingTime:
    """Floating DATE-TIME needs an explicit interpretation timezone."""

    @pytest.fixture
    def calendar(self):
        return parse_calendar(
            "BEGIN:VEVENT\r\n"
            "UID:floating\r\n"
            "DTSTAMP:20240101T000000Z\r\n"
            "DTSTART:20240304T100000\r\n"
            "DTEND:20240304T110000\r\n"
            "RRULE:FREQ=WEEKLY;COUNT=2\r\n"
            "END:VEVENT\r\n"
        )

    def test_floating_event_without_timezone_is_rejected(self, calendar):
        with pytest.raises(FloatingTimeError):
            calendar.occurrences_between(*MARCH)

    def test_floating_event_with_timezone(self, calendar):
        occurrences = calendar.occurrences_between(*MARCH, floating_tz=NEW_YORK)
        assert len(occurrences) == 2
        assert occurrences[0].start == datetime(2024, 3, 4, 10)
        # 10:00 floating in New York is 15:00 UTC before the spring forward.
        assert occurrences[0].start.replace(tzinfo=NEW_YORK).astimezone(
            UTC
        ) == datetime(2024, 3, 4, 15, tzinfo=UTC)

    def test_naive_window_is_rejected(self, calendar):
        with pytest.raises(FloatingTimeError):
            calendar.occurrences_between(
                datetime(2024, 3, 1), datetime(2024, 4, 1), floating_tz=NEW_YORK
            )


class TestCandidateLimit:
    """Infinite rules expand only inside the bounded window, with a cap."""

    @pytest.fixture
    def calendar(self):
        return parse_calendar(
            "BEGIN:VEVENT\r\n"
            "UID:infinite\r\n"
            "DTSTAMP:20240101T000000Z\r\n"
            "DTSTART:20240101T100000Z\r\n"
            "DTEND:20240101T110000Z\r\n"
            "RRULE:FREQ=DAILY\r\n"
            "END:VEVENT\r\n"
        )

    def test_infinite_rule_expands_within_window(self, calendar):
        occurrences = calendar.occurrences_between(
            datetime(2024, 6, 1, tzinfo=UTC), datetime(2024, 6, 10, tzinfo=UTC)
        )
        assert occurrences.complete
        assert len(occurrences) == 9

    def test_candidate_limit_marks_result_incomplete(self, calendar):
        occurrences = calendar.occurrences_between(
            datetime(2024, 6, 1, tzinfo=UTC),
            datetime(2024, 6, 10, tzinfo=UTC),
            max_candidates=5,
        )
        assert not occurrences.complete
        assert len(occurrences) < 9

    def test_invalid_limit_rejected(self, calendar):
        with pytest.raises(ValueError, match="max_candidates"):
            calendar.occurrences_between(*MARCH, max_candidates=0)


class TestUnsupportedDefinitions:
    """Undefined combinations are rejected instead of guessed."""

    def test_multiple_rrules_are_not_supported(self):
        calendar = parse_calendar(
            "BEGIN:VEVENT\r\n"
            "UID:multi\r\n"
            "DTSTAMP:20240101T000000Z\r\n"
            "DTSTART:20240304T100000Z\r\n"
            "DTEND:20240304T110000Z\r\n"
            "RRULE:FREQ=WEEKLY;COUNT=4\r\n"
            "RRULE:FREQ=DAILY;COUNT=3\r\n"
            "END:VEVENT\r\n"
        )
        with pytest.raises(UnsupportedRecurrence, match="RRULE"):
            calendar.occurrences_between(*MARCH)


class TestResultIsolation:
    """Modifying results never pollutes the source calendar."""

    def test_mutating_occurrences_does_not_change_calendar(self):
        calendar = weekly_meeting()
        before = calendar.to_ical()
        occurrences = calendar.occurrences_between(*MARCH)
        first = occurrences[0]
        first.start = datetime(2030, 1, 1, tzinfo=UTC)
        first.end = datetime(2030, 1, 2, tzinfo=UTC)
        first.recurrence_id = datetime(2030, 1, 1, tzinfo=UTC)
        occurrences.append(first)
        occurrences.clear()
        assert calendar.to_ical() == before
        again = calendar.occurrences_between(*MARCH)
        assert len(again) == 6
        assert again[0].start == datetime(2024, 3, 4, 10, tzinfo=NEW_YORK)

    def test_repeated_queries_return_independent_results(self):
        calendar = weekly_meeting()
        first = calendar.occurrences_between(*MARCH)
        second = calendar.occurrences_between(*MARCH)
        assert first is not second
        assert first[0] is not second[0]
        assert first[0].start == second[0].start


class TestFunctionAPI:
    """The module-level function accepts calendars and event iterables."""

    def test_accepts_calendar(self):
        calendar = weekly_meeting()
        occurrences = occurrences_between(calendar, *MARCH)
        assert len(occurrences) == 6

    def test_accepts_event_iterable(self):
        calendar = weekly_meeting()
        occurrences = occurrences_between(calendar.events, *MARCH)
        assert len(occurrences) == 6

    def test_detached_without_master_is_skipped(self):
        calendar = parse_calendar(
            detached("20240311T100000", "20240312T140000", "20240312T150000"),
        )
        occurrences = calendar.occurrences_between(*MARCH)
        assert list(occurrences) == []
        assert occurrences.complete
