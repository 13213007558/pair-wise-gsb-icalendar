"""Tests for resolving naive local times with a VTIMEZONE component.

The main VTIMEZONE below uses a 30-minute DST offset on top of a +10:00
standard offset. Clocks spring forward 30 minutes on the first Sunday
of April at 02:00 and fall back 30 minutes on the last Sunday of
October at 02:30, so the local times 02:00-02:30 are skipped in April
and repeated in October.
"""

from datetime import datetime, timedelta, timezone

import pytest

from icalendar import Calendar, Timezone
from icalendar.cal.timezone import TimezoneTransition
from icalendar.error import (
    AmbiguousLocalTime,
    InsufficientTimezoneData,
    NonExistentLocalTime,
)

UTC = timezone.utc
STD = timedelta(hours=10)
DST = timedelta(hours=10, minutes=30)

HALF_HOUR_CALENDAR = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//icalendar//test//EN
BEGIN:VTIMEZONE
TZID:Custom/Half-Hour
BEGIN:STANDARD
DTSTART:19671029T023000
TZOFFSETFROM:+1030
TZOFFSETTO:+1000
TZNAME:CHST
RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:19670402T020000
TZOFFSETFROM:+1000
TZOFFSETTO:+1030
TZNAME:CHDT
RRULE:FREQ=YEARLY;BYMONTH=4;BYDAY=1SU
END:DAYLIGHT
END:VTIMEZONE
END:VCALENDAR
"""

# 2026-04-05 02:00 +10:00 -> 2026-04-04 16:00 UTC (spring forward)
SPRING_FORWARD_UTC = datetime(2026, 4, 4, 16, 0, tzinfo=UTC)
# 2026-10-25 02:30 +10:30 -> 2026-10-24 16:00 UTC (fall back)
FALL_BACK_UTC = datetime(2026, 10, 24, 16, 0, tzinfo=UTC)

FOLD_LOCAL = datetime(2026, 10, 25, 2, 15)
GAP_LOCAL = datetime(2026, 4, 5, 2, 15)


def vtimezone(ics: str) -> Timezone:
    """Parse the first VTIMEZONE of a calendar."""
    return Calendar.from_ical(ics).walk("VTIMEZONE")[0]


@pytest.fixture
def half_hour() -> Timezone:
    """The custom 30-minute DST timezone."""
    return vtimezone(HALF_HOUR_CALENDAR)


class TestUnambiguousLocalTime:
    """Ordinary local times keep their unambiguous conversion."""

    def test_winter_time(self, half_hour):
        assert half_hour.local_to_utc(datetime(2026, 1, 15, 12, 0)) == datetime(
            2026, 1, 15, 2, 0, tzinfo=UTC
        )

    def test_summer_time(self, half_hour):
        assert half_hour.local_to_utc(datetime(2026, 6, 1, 12, 0)) == datetime(
            2026, 6, 1, 1, 30, tzinfo=UTC
        )

    def test_offsets_for_local(self, half_hour):
        assert half_hour.offsets_for_local(datetime(2026, 1, 15, 12, 0)) == (STD,)
        assert half_hour.offsets_for_local(datetime(2026, 6, 1, 12, 0)) == (DST,)

    def test_utc_offset_at(self, half_hour):
        assert half_hour.utc_offset_at(datetime(2026, 1, 15)) == STD
        assert half_hour.utc_offset_at(datetime(2026, 6, 1)) == DST

    def test_utc_offset_at_aware_input(self, half_hour):
        five_hours = timezone(timedelta(hours=5))
        aware = datetime(2026, 6, 1, 5, 0, tzinfo=five_hours)
        assert half_hour.utc_offset_at(aware) == half_hour.utc_offset_at(
            datetime(2026, 6, 1, 0, 0)
        )


class TestFold:
    """The repeated wall-clock interval of the fall-back transition."""

    def test_two_candidate_offsets(self, half_hour):
        offsets = half_hour.offsets_for_local(FOLD_LOCAL)
        assert set(offsets) == {STD, DST}

    def test_fold_earlier(self, half_hour):
        assert half_hour.local_to_utc(FOLD_LOCAL, fold="earlier") == datetime(
            2026, 10, 24, 15, 45, tzinfo=UTC
        )

    def test_fold_later(self, half_hour):
        assert half_hour.local_to_utc(FOLD_LOCAL, fold="later") == datetime(
            2026, 10, 24, 16, 15, tzinfo=UTC
        )

    def test_fold_reject_raises_with_candidates(self, half_hour):
        with pytest.raises(AmbiguousLocalTime) as exc_info:
            half_hour.local_to_utc(FOLD_LOCAL, fold="reject")
        error = exc_info.value
        assert error.dt == FOLD_LOCAL
        assert set(error.offsets) == {STD, DST}

    def test_default_uses_dt_fold_attribute(self, half_hour):
        earlier = half_hour.local_to_utc(FOLD_LOCAL)
        later = half_hour.local_to_utc(FOLD_LOCAL.replace(fold=1))
        assert earlier == datetime(2026, 10, 24, 15, 45, tzinfo=UTC)
        assert later == datetime(2026, 10, 24, 16, 15, tzinfo=UTC)

    def test_fold_round_trip(self, half_hour):
        earlier = half_hour.local_to_utc(FOLD_LOCAL, fold="earlier")
        later = half_hour.local_to_utc(FOLD_LOCAL, fold="later")
        assert half_hour.utc_to_local(earlier) == FOLD_LOCAL.replace(fold=0)
        assert half_hour.utc_to_local(later) == FOLD_LOCAL.replace(fold=1)
        assert half_hour.local_to_utc(half_hour.utc_to_local(earlier)) == earlier
        assert half_hour.local_to_utc(half_hour.utc_to_local(later)) == later


class TestGap:
    """The skipped wall-clock interval of the spring-forward transition."""

    def test_no_candidate_offsets(self, half_hour):
        assert half_hour.offsets_for_local(GAP_LOCAL) == ()

    def test_shift_forward_is_default(self, half_hour):
        # 02:15 does not exist; shifting by the 30-minute gap gives 02:45
        # local, which is 16:15 UTC with the new +10:30 offset
        assert half_hour.local_to_utc(GAP_LOCAL) == datetime(
            2026, 4, 4, 16, 15, tzinfo=UTC
        )
        assert half_hour.local_to_utc(GAP_LOCAL, gap="shift-forward") == datetime(
            2026, 4, 4, 16, 15, tzinfo=UTC
        )

    def test_shift_forward_result_is_consistent(self, half_hour):
        shifted_utc = half_hour.local_to_utc(GAP_LOCAL, gap="shift-forward")
        assert half_hour.utc_to_local(shifted_utc) == datetime(2026, 4, 5, 2, 45)

    def test_gap_reject_raises_with_diagnostic(self, half_hour):
        with pytest.raises(NonExistentLocalTime) as exc_info:
            half_hour.local_to_utc(GAP_LOCAL, gap="reject")
        error = exc_info.value
        assert error.dt == GAP_LOCAL
        assert error.offset_before == STD
        assert error.offset_after == DST
        assert error.gap == timedelta(minutes=30)


class TestTransitionsBetween:
    """Transitions are computed from the sub-components in a finite window."""

    def test_transitions_of_2026(self, half_hour):
        transitions = half_hour.transitions_between(
            datetime(2026, 1, 1), datetime(2027, 1, 1)
        )
        assert transitions == [
            TimezoneTransition(
                utc=SPRING_FORWARD_UTC,
                offset_from=STD,
                offset_to=DST,
                tzname="CHDT",
                is_dst=True,
            ),
            TimezoneTransition(
                utc=FALL_BACK_UTC,
                offset_from=DST,
                offset_to=STD,
                tzname="CHST",
                is_dst=False,
            ),
        ]

    def test_window_boundaries_are_included(self, half_hour):
        transitions = half_hour.transitions_between(SPRING_FORWARD_UTC, FALL_BACK_UTC)
        assert [t.utc for t in transitions] == [SPRING_FORWARD_UTC, FALL_BACK_UTC]

    def test_naive_datetimes_are_utc(self, half_hour):
        aware = half_hour.transitions_between(
            datetime(2026, 1, 1, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC)
        )
        naive = half_hour.transitions_between(
            datetime(2026, 1, 1), datetime(2027, 1, 1)
        )
        assert aware == naive

    def test_end_before_start(self, half_hour):
        with pytest.raises(ValueError, match="must not be before"):
            half_hour.transitions_between(datetime(2027, 1, 1), datetime(2026, 1, 1))


class TestRoundTrip:
    """Converting forth and back keeps the identity."""

    @pytest.mark.parametrize("onset", [SPRING_FORWARD_UTC, FALL_BACK_UTC])
    def test_utc_local_utc_around_transition(self, half_hour, onset):
        start = onset - timedelta(hours=2)
        for step in range(4 * 12 + 1):
            instant = start + timedelta(minutes=5 * step)
            local = half_hour.utc_to_local(instant)
            assert half_hour.local_to_utc(local) == instant

    @pytest.mark.parametrize(
        "local",
        [
            datetime(2026, 1, 15, 12, 0),
            datetime(2026, 6, 1, 12, 0),
            datetime(2026, 4, 5, 1, 59, 59),
            datetime(2026, 4, 5, 2, 30),
            datetime(2026, 10, 25, 1, 59, 59),
            datetime(2026, 10, 25, 2, 30),
        ],
    )
    def test_local_utc_local(self, half_hour, local):
        assert half_hour.utc_to_local(half_hour.local_to_utc(local)) == local


CROSS_MIDNIGHT_CALENDAR = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//icalendar//test//EN
BEGIN:VTIMEZONE
TZID:Custom/Cross-Midnight
BEGIN:STANDARD
DTSTART:19671029T003000
TZOFFSETFROM:+1030
TZOFFSETTO:+1000
TZNAME:XST
RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:19670402T003000
TZOFFSETFROM:+1000
TZOFFSETTO:+1030
TZNAME:XDT
RRULE:FREQ=YEARLY;BYMONTH=4;BYDAY=1SU
END:DAYLIGHT
END:VTIMEZONE
END:VCALENDAR
"""


class TestCrossMidnightTransition:
    """Transitions just after midnight happen on the previous UTC day."""

    @pytest.fixture
    def cross_midnight(self) -> Timezone:
        return vtimezone(CROSS_MIDNIGHT_CALENDAR)

    def test_transition_utc_dates(self, cross_midnight):
        transitions = cross_midnight.transitions_between(
            datetime(2026, 1, 1), datetime(2027, 1, 1)
        )
        # local 2026-04-05 00:30 +10:00 and 2026-10-25 00:30 +10:30
        assert [t.utc for t in transitions] == [
            datetime(2026, 4, 4, 14, 30, tzinfo=UTC),
            datetime(2026, 10, 24, 14, 0, tzinfo=UTC),
        ]

    def test_fold_after_midnight(self, cross_midnight):
        local = datetime(2026, 10, 25, 0, 15)
        assert set(cross_midnight.offsets_for_local(local)) == {STD, DST}
        assert cross_midnight.local_to_utc(local, fold="earlier") == datetime(
            2026, 10, 24, 13, 45, tzinfo=UTC
        )
        assert cross_midnight.local_to_utc(local, fold="later") == datetime(
            2026, 10, 24, 14, 15, tzinfo=UTC
        )

    def test_gap_after_midnight(self, cross_midnight):
        local = datetime(2026, 4, 5, 0, 45)
        assert cross_midnight.offsets_for_local(local) == ()
        assert cross_midnight.local_to_utc(local, gap="shift-forward") == datetime(
            2026, 4, 4, 14, 45, tzinfo=UTC
        )


NEGATIVE_DST_CALENDAR = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//icalendar//test//EN
BEGIN:VTIMEZONE
TZID:Custom/Negative-DST
BEGIN:STANDARD
DTSTART:19671029T020000
TZOFFSETFROM:+0100
TZOFFSETTO:+0200
TZNAME:NST
RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:19670326T020000
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
TZNAME:NDT
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU
END:DAYLIGHT
END:VTIMEZONE
END:VCALENDAR
"""


class TestNegativeDstOffset:
    """A DAYLIGHT observance whose offset is below the standard offset."""

    @pytest.fixture
    def negative_dst(self) -> Timezone:
        return vtimezone(NEGATIVE_DST_CALENDAR)

    def test_dst_transition_is_recorded(self, negative_dst):
        transitions = negative_dst.transitions_between(
            datetime(2026, 1, 1), datetime(2027, 1, 1)
        )
        assert [(t.utc, t.is_dst) for t in transitions] == [
            (datetime(2026, 3, 29, 0, 0, tzinfo=UTC), True),
            (datetime(2026, 10, 25, 1, 0, tzinfo=UTC), False),
        ]
        daylight = transitions[0]
        assert daylight.offset_to < daylight.offset_from

    def test_fold_when_clocks_go_back_in_spring(self, negative_dst):
        local = datetime(2026, 3, 29, 1, 30)
        offsets = negative_dst.offsets_for_local(local)
        assert set(offsets) == {timedelta(hours=1), timedelta(hours=2)}
        assert negative_dst.local_to_utc(local, fold="earlier") == datetime(
            2026, 3, 28, 23, 30, tzinfo=UTC
        )
        assert negative_dst.local_to_utc(local, fold="later") == datetime(
            2026, 3, 29, 0, 30, tzinfo=UTC
        )

    def test_gap_when_clocks_go_forward_in_autumn(self, negative_dst):
        local = datetime(2026, 10, 25, 2, 30)
        assert negative_dst.offsets_for_local(local) == ()
        assert negative_dst.local_to_utc(local, gap="shift-forward") == datetime(
            2026, 10, 25, 1, 30, tzinfo=UTC
        )


HISTORY_CALENDAR = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//icalendar//test//EN
BEGIN:VTIMEZONE
TZID:Custom/History
BEGIN:STANDARD
DTSTART:19671105T020000
TZOFFSETFROM:+0930
TZOFFSETTO:+1000
TZNAME:HST
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:19700405T020000
TZOFFSETFROM:+1000
TZOFFSETTO:+1030
TZNAME:HDT
RRULE:FREQ=YEARLY;BYMONTH=4;BYDAY=1SU
END:DAYLIGHT
END:VTIMEZONE
END:VCALENDAR
"""

FIXED_CALENDAR = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//icalendar//test//EN
BEGIN:VTIMEZONE
TZID:Custom/Fixed
BEGIN:STANDARD
DTSTART:19670101T000000
TZOFFSETFROM:+0930
TZOFFSETTO:+0930
TZNAME:FIX
END:STANDARD
END:VTIMEZONE
END:VCALENDAR
"""

RDATE_CALENDAR = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//icalendar//test//EN
BEGIN:VTIMEZONE
TZID:Custom/Rdates
BEGIN:STANDARD
DTSTART:19670101T000000
TZOFFSETFROM:+1030
TZOFFSETTO:+1000
TZNAME:RST
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:19700405T020000
TZOFFSETFROM:+1000
TZOFFSETTO:+1030
TZNAME:RDT
RDATE:19710404T020000
RDATE:19720402T020000
END:DAYLIGHT
END:VTIMEZONE
END:VCALENDAR
"""


class TestHistoricalRules:
    """One-time DTSTART onsets and RDATE lists define transitions, too."""

    def test_dtstart_only_onset_applies_before_rules_start(self):
        tz = vtimezone(HISTORY_CALENDAR)
        # after the one-time 1967 transition, before the rules from 1970
        assert tz.local_to_utc(datetime(1968, 6, 1, 12, 0)) == datetime(
            1968, 6, 1, 2, 0, tzinfo=UTC
        )

    def test_before_first_onset_is_insufficient(self):
        tz = vtimezone(HISTORY_CALENDAR)
        with pytest.raises(InsufficientTimezoneData):
            tz.local_to_utc(datetime(1967, 11, 4, 12, 0))

    def test_fixed_offset_zone_is_open_ended(self):
        tz = vtimezone(FIXED_CALENDAR)
        assert tz.local_to_utc(datetime(2026, 6, 1, 12, 0)) == datetime(
            2026, 6, 1, 2, 30, tzinfo=UTC
        )
        assert tz.utc_offset_at(datetime(2026, 6, 1)) == timedelta(hours=9, minutes=30)

    def test_fixed_offset_zone_starts_at_dtstart(self):
        tz = vtimezone(FIXED_CALENDAR)
        with pytest.raises(InsufficientTimezoneData):
            tz.local_to_utc(datetime(1966, 12, 31, 12, 0))

    def test_rdate_transitions(self):
        tz = vtimezone(RDATE_CALENDAR)
        transitions = tz.transitions_between(datetime(1970, 1, 1), datetime(1973, 1, 1))
        assert [t.utc for t in transitions] == [
            datetime(1970, 4, 4, 16, 0, tzinfo=UTC),
            datetime(1971, 4, 3, 16, 0, tzinfo=UTC),
            datetime(1972, 4, 1, 16, 0, tzinfo=UTC),
        ]
        assert tz.local_to_utc(datetime(1971, 6, 1, 12, 0)) == datetime(
            1971, 6, 1, 1, 30, tzinfo=UTC
        )


FINITE_CALENDAR = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//icalendar//test//EN
BEGIN:VTIMEZONE
TZID:Custom/Finite
BEGIN:STANDARD
DTSTART:19671029T023000
TZOFFSETFROM:+1030
TZOFFSETTO:+1000
TZNAME:FST
RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU;UNTIL=20201025T023000Z
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:19670402T020000
TZOFFSETFROM:+1000
TZOFFSETTO:+1030
TZNAME:FDT
RRULE:FREQ=YEARLY;BYMONTH=4;BYDAY=1SU;UNTIL=20200405T020000Z
END:DAYLIGHT
END:VTIMEZONE
END:VCALENDAR
"""


class TestInsufficientData:
    """Requests outside the rule coverage are reported, not answered with UTC."""

    def test_within_finite_rules_still_works(self):
        tz = vtimezone(FINITE_CALENDAR)
        assert tz.local_to_utc(datetime(2019, 6, 1, 12, 0)) == datetime(
            2019, 6, 1, 1, 30, tzinfo=UTC
        )

    def test_after_rules_end(self):
        tz = vtimezone(FINITE_CALENDAR)
        with pytest.raises(InsufficientTimezoneData) as exc_info:
            tz.local_to_utc(datetime(2026, 6, 1, 12, 0))
        assert exc_info.value.tzid == "Custom/Finite"

    def test_after_rules_end_utc_offset_at(self):
        tz = vtimezone(FINITE_CALENDAR)
        with pytest.raises(InsufficientTimezoneData):
            tz.utc_offset_at(datetime(2026, 6, 1))

    def test_empty_vtimezone_is_not_utc(self):
        tz = Timezone()
        tz.add("TZID", "Custom/Empty")
        with pytest.raises(InsufficientTimezoneData):
            tz.local_to_utc(datetime(2026, 6, 1, 12, 0))
        with pytest.raises(InsufficientTimezoneData):
            tz.utc_offset_at(datetime(2026, 6, 1))


class TestNoTimezoneDatabase:
    """The conversion derives everything from the VTIMEZONE component."""

    def test_no_timezone_provider_is_consulted(self, half_hour, monkeypatch):
        from icalendar.timezone import tzp

        def fail(*args, **kwargs):
            raise AssertionError("the timezone database must not be consulted")

        monkeypatch.setattr(tzp.__class__, "timezone", fail)
        monkeypatch.setattr(tzp.__class__, "create_timezone", fail)
        assert half_hour.local_to_utc(FOLD_LOCAL, fold="earlier") == datetime(
            2026, 10, 24, 15, 45, tzinfo=UTC
        )
        assert half_hour.local_to_utc(GAP_LOCAL) == datetime(
            2026, 4, 4, 16, 15, tzinfo=UTC
        )


class TestIcsWriteBack:
    """The VTIMEZONE survives serialization with identical conversions."""

    def test_timezone_component_round_trip(self, half_hour):
        reparsed = Timezone.from_ical(half_hour.to_ical())
        window = (datetime(2026, 1, 1), datetime(2027, 1, 1))
        assert reparsed.transitions_between(*window) == (
            half_hour.transitions_between(*window)
        )
        assert reparsed.local_to_utc(FOLD_LOCAL, fold="later") == (
            half_hour.local_to_utc(FOLD_LOCAL, fold="later")
        )
        assert reparsed.local_to_utc(GAP_LOCAL) == half_hour.local_to_utc(GAP_LOCAL)

    def test_calendar_round_trip(self):
        calendar = Calendar.from_ical(HALF_HOUR_CALENDAR)
        ics = calendar.to_ical()
        assert b"BEGIN:VTIMEZONE" in ics
        assert b"TZID:Custom/Half-Hour" in ics
        reparsed = Calendar.from_ical(ics).walk("VTIMEZONE")[0]
        assert reparsed.tz_name == "Custom/Half-Hour"
        assert reparsed.local_to_utc(FOLD_LOCAL, fold="earlier") == datetime(
            2026, 10, 24, 15, 45, tzinfo=UTC
        )
        assert reparsed.offsets_for_local(FOLD_LOCAL) != ()
        with pytest.raises(NonExistentLocalTime):
            reparsed.local_to_utc(GAP_LOCAL, gap="reject")


class TestInvalidInput:
    """Misuse of the conversion interface is reported."""

    def test_aware_local_time(self, half_hour):
        aware = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
        with pytest.raises(ValueError, match="naive local time"):
            half_hour.local_to_utc(aware)
        with pytest.raises(ValueError, match="naive local time"):
            half_hour.offsets_for_local(aware)

    def test_unknown_fold_policy(self, half_hour):
        with pytest.raises(ValueError, match="fold"):
            half_hour.local_to_utc(FOLD_LOCAL, fold="ignore")

    def test_unknown_gap_policy(self, half_hour):
        with pytest.raises(ValueError, match="gap"):
            half_hour.local_to_utc(GAP_LOCAL, gap="shift-backward")
