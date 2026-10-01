"""VTIMEZONE conversion with explicit fold/gap policies and diagnostics.

These tests use a hand-crafted VTIMEZONE with 30-minute offsets and a
fall-back transition at 02:00 wall time. The operating system timezone
database is never consulted.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from icalendar import Calendar, Event
from icalendar.error import (
    AmbiguousTimeError,
    InsufficientTimezoneDataError,
    NonExistentTimeError,
)
from icalendar.timezone.vtimezone import (
    FoldPolicy,
    GapPolicy,
    LocalTimeKind,
    VTIMEZONEInfo,
)


HALF_HOUR_VTIMEZONE = b"""BEGIN:VTIMEZONE
TZID:Custom/HalfHour
BEGIN:STANDARD
DTSTART:19700101T000000
TZOFFSETFROM:+0600
TZOFFSETTO:+0530
TZNAME:HTS
RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU;BYHOUR=2;BYMINUTE=0
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:19700301T023000
TZOFFSETFROM:+0530
TZOFFSETTO:+0600
TZNAME:HDT
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=1SU;BYHOUR=2;BYMINUTE=30
END:DAYLIGHT
END:VTIMEZONE"""


@pytest.fixture()
def half_hour():
    """The custom 30-minute-offset VTIMEZONE."""
    return Calendar.from_ical(HALF_HOUR_VTIMEZONE)


STD = timedelta(hours=5, minutes=30)
DST = timedelta(hours=6)


class TestTransitions:
    """Finite-window transition generation."""

    def test_transitions_are_generated_from_rrule(self, half_hour):
        """Both STANDARD and DAYLIGHT occurrences appear in the window."""
        transitions = half_hour.get_transitions_between(
            datetime(2024, 1, 1), datetime(2025, 1, 1)
        )
        assert [t.local for t in transitions] == [
            datetime(2024, 3, 3, 2, 30),
            datetime(2024, 11, 3, 2, 0),
        ]
        assert transitions[0].utc == datetime(
            2024, 3, 2, 21, 0, tzinfo=timezone.utc
        )
        assert transitions[0].offset_from == STD
        assert transitions[0].offset_to == DST
        assert transitions[0].dst_offset == timedelta(minutes=30)
        assert transitions[1].utc == datetime(
            2024, 11, 2, 20, 0, tzinfo=timezone.utc
        )
        assert transitions[1].offset_from == DST
        assert transitions[1].offset_to == STD

    def test_window_is_finite_and_exclusive_outside(self, half_hour):
        """Only onsets inside the requested window are returned."""
        transitions = half_hour.get_transitions_between(
            datetime(2024, 4, 1), datetime(2024, 10, 1)
        )
        assert transitions == ()

    def test_window_bounds_are_inclusive(self, half_hour):
        """Onsets exactly on the bounds are included."""
        transitions = half_hour.get_transitions_between(
            datetime(2024, 3, 3, 2, 30), datetime(2024, 11, 3, 2, 0)
        )
        assert len(transitions) == 2

    def test_window_rejects_aware_datetimes(self, half_hour):
        """Window bounds are naive local wall-clock datetimes."""
        with pytest.raises(ValueError, match="naive"):
            half_hour.get_transitions_between(
                datetime(2024, 1, 1, tzinfo=timezone.utc),
                datetime(2025, 1, 1),
            )

    def test_rdate_transitions(self):
        """RDATE values generate one-off transitions."""
        vtimezone = Calendar.from_ical(
            b"BEGIN:VTIMEZONE\n"
            b"TZID:Rdate\n"
            b"BEGIN:STANDARD\n"
            b"DTSTART:19451001T020000\n"
            b"TZOFFSETFROM:+0600\nTZOFFSETTO:+0530\n"
            b"RDATE:19451001T020000\nEND:STANDARD\n"
            b"BEGIN:DAYLIGHT\n"
            b"DTSTART:19450401T023000\n"
            b"TZOFFSETFROM:+0530\nTZOFFSETTO:+0600\n"
            b"RDATE:19450401T023000\nEND:DAYLIGHT\n"
            b"END:VTIMEZONE"
        )
        transitions = vtimezone.get_transitions_between(
            datetime(1945, 1, 1), datetime(1946, 1, 1)
        )
        assert [t.local for t in transitions] == [
            datetime(1945, 4, 1, 2, 30),
            datetime(1945, 10, 1, 2, 0),
        ]

    def test_exdate_removes_occurrence(self):
        """EXDATE removes a single RRULE occurrence."""
        vtimezone = Calendar.from_ical(
            b"BEGIN:VTIMEZONE\nTZID:Ex\n"
            b"BEGIN:STANDARD\nDTSTART:20240101T000000\n"
            b"TZOFFSETFROM:+0600\nTZOFFSETTO:+0530\n"
            b"RRULE:FREQ=MONTHLY;BYMONTHDAY=1\n"
            b"EXDATE:20240301T000000\nEND:STANDARD\n"
            b"END:VTIMEZONE"
        )
        transitions = vtimezone.get_transitions_between(
            datetime(2024, 1, 1), datetime(2024, 5, 1)
        )
        assert datetime(2024, 3, 1) not in [t.local for t in transitions]
        assert datetime(2024, 4, 1) in [t.local for t in transitions]

    def test_finite_count_rule_continues_after_last_occurrence(self):
        """After the last COUNT occurrence the final offset continues."""
        vtimezone = Calendar.from_ical(
            b"BEGIN:VTIMEZONE\nTZID:Count\n"
            b"BEGIN:STANDARD\nDTSTART:20200101T000000\n"
            b"TZOFFSETFROM:+0530\nTZOFFSETTO:+0530\n"
            b"RRULE:FREQ=YEARLY;COUNT=3\nEND:STANDARD\n"
            b"END:VTIMEZONE"
        )
        # The rule ends 2022-01-01; 2024 must still resolve, not report
        # insufficient data.
        localized = vtimezone.localize_naive(datetime(2024, 6, 1, 12))
        assert localized.utcoffset() == STD

    def test_until_rule_stops_generating_transitions(self):
        """An UNTIL rule produces nothing past its end, but stays valid."""
        vtimezone = Calendar.from_ical(
            b"BEGIN:VTIMEZONE\nTZID:Until\n"
            b"BEGIN:STANDARD\nDTSTART:20200101T000000\n"
            b"TZOFFSETFROM:+0530\nTZOFFSETTO:+0530\n"
            b"RRULE:FREQ=YEARLY;UNTIL=20220101T000000\nEND:STANDARD\n"
            b"END:VTIMEZONE"
        )
        transitions = vtimezone.get_transitions_between(
            datetime(2023, 1, 1), datetime(2025, 1, 1)
        )
        assert transitions == ()
        localized = vtimezone.localize_naive(datetime(2024, 1, 1, 0))
        assert localized.utcoffset() == STD


class TestUnambiguous:
    """Ordinary local times keep their single, obvious offset."""

    @pytest.mark.parametrize(
        ("local", "offset", "name", "utc"),
        [
            (
                datetime(2024, 1, 15, 12),
                STD,
                "HTS",
                datetime(2024, 1, 15, 6, 30, tzinfo=timezone.utc),
            ),
            (
                datetime(2024, 6, 15, 12),
                DST,
                "HDT",
                datetime(2024, 6, 15, 6, 0, tzinfo=timezone.utc),
            ),
            (
                datetime(2024, 11, 3, 2, 0),
                STD,
                "HTS",
                datetime(2024, 11, 2, 20, 30, tzinfo=timezone.utc),
            ),
            (
                datetime(2024, 3, 3, 3, 0),
                DST,
                "HDT",
                datetime(2024, 3, 2, 21, 0, tzinfo=timezone.utc),
            ),
        ],
    )
    def test_unambiguous_localization(
        self, half_hour, local, offset, name, utc
    ):
        """Unique local times convert to exactly one UTC instant."""
        diagnosis = half_hour.diagnose_local(local)
        assert diagnosis.kind is LocalTimeKind.UNAMBIGUOUS
        assert diagnosis.offsets == (offset,)
        localized = half_hour.localize_naive(local)
        assert localized.utcoffset() == offset
        assert localized.tzname() == name
        assert localized.astimezone(timezone.utc) == utc

    def test_unambiguous_round_trip(self, half_hour):
        """UTC -> local -> UTC preserves the instant."""
        info = half_hour.to_tzinfo()
        local = datetime(2024, 6, 15, 12)
        localized = half_hour.localize_naive(local)
        back = localized.astimezone(timezone.utc).astimezone(info)
        assert back.replace(tzinfo=None) == local
        assert back.utcoffset() == DST


class TestFold:
    """Local times that occur twice when clocks are set back."""

    @pytest.mark.parametrize(
        "local",
        [
            datetime(2024, 11, 3, 1, 30),  # first repeated wall time
            datetime(2024, 11, 3, 1, 45),
            datetime(2024, 11, 3, 1, 59, 59),
        ],
    )
    def test_fold_diagnosis_has_two_candidates(self, half_hour, local):
        """The fold reports both offsets, ordered earlier then later UTC."""
        diagnosis = half_hour.diagnose_local(local)
        assert diagnosis.kind is LocalTimeKind.FOLD
        assert diagnosis.is_ambiguous
        assert diagnosis.offsets == (DST, STD)
        assert diagnosis.earlier.offset == DST
        assert diagnosis.later.offset == STD
        assert diagnosis.earlier.utc < diagnosis.later.utc
        assert diagnosis.later.utc - diagnosis.earlier.utc == timedelta(
            minutes=30
        )
        assert diagnosis.previous_transition is not None

    def test_fold_exact_boundaries(self, half_hour):
        """01:30 is the fold start, 02:00 is standard time only."""
        start = half_hour.diagnose_local(datetime(2024, 11, 3, 1, 30))
        end = half_hour.diagnose_local(datetime(2024, 11, 3, 2, 0))
        assert start.kind is LocalTimeKind.FOLD
        assert end.kind is LocalTimeKind.UNAMBIGUOUS
        assert end.offsets == (STD,)

    def test_fold_earlier_policy(self, half_hour):
        """fold=earlier uses the daylight (first) occurrence."""
        local = datetime(2024, 11, 3, 1, 45)
        result = half_hour.localize_naive(local, fold=FoldPolicy.EARLIER)
        assert result.utcoffset() == DST
        assert result.astimezone(timezone.utc) == datetime(
            2024, 11, 2, 19, 45, tzinfo=timezone.utc
        )

    def test_fold_later_policy(self, half_hour):
        """fold=later uses the standard (second) occurrence."""
        local = datetime(2024, 11, 3, 1, 45)
        result = half_hour.localize_naive(local, fold=FoldPolicy.LATER)
        assert result.utcoffset() == STD
        assert result.astimezone(timezone.utc) == datetime(
            2024, 11, 2, 20, 15, tzinfo=timezone.utc
        )

    def test_fold_reject_policy(self, half_hour):
        """fold=reject raises AmbiguousTimeError with the diagnosis."""
        local = datetime(2024, 11, 3, 1, 45)
        with pytest.raises(AmbiguousTimeError) as exc_info:
            half_hour.localize_naive(local, fold=FoldPolicy.REJECT)
        diagnosis = exc_info.value.diagnosis
        assert diagnosis.kind is LocalTimeKind.FOLD
        assert diagnosis.offsets == (DST, STD)

    def test_fold_policy_strings_accepted(self, half_hour):
        """Policies can be passed as their string values."""
        local = datetime(2024, 11, 3, 1, 45)
        earlier = half_hour.localize_naive(local, fold="earlier")
        later = half_hour.localize_naive(local, fold="later")
        assert earlier.utcoffset() != later.utcoffset()

    def test_fold_pep495_fold_attribute(self, half_hour):
        """The returned datetime carries fold=0/1 per PEP 495."""
        local = datetime(2024, 11, 3, 1, 45)
        assert half_hour.localize_naive(local, fold="earlier").fold == 0
        assert half_hour.localize_naive(local, fold="later").fold == 1

    def test_fold_round_trip_identity_both_ways(self, half_hour):
        """Both fold interpretations survive UTC conversion and back."""
        info = half_hour.to_tzinfo()
        local = datetime(2024, 11, 3, 1, 45)
        for policy, offset in (
            (FoldPolicy.EARLIER, DST),
            (FoldPolicy.LATER, STD),
        ):
            localized = half_hour.localize_naive(local, fold=policy)
            back = localized.astimezone(timezone.utc).astimezone(info)
            assert back.replace(tzinfo=None) == local
            assert back.utcoffset() == offset


class TestGap:
    """Local times that never occur because clocks jump forward."""

    @pytest.mark.parametrize(
        "local",
        [
            datetime(2024, 3, 3, 2, 30),  # first missing wall time
            datetime(2024, 3, 3, 2, 45),
            datetime(2024, 3, 3, 2, 59, 59),
        ],
    )
    def test_gap_diagnosis_has_no_candidates(self, half_hour, local):
        """The gap reports its length and suggested shifted time."""
        diagnosis = half_hour.diagnose_local(local)
        assert diagnosis.kind is LocalTimeKind.GAP
        assert diagnosis.is_nonexistent
        assert diagnosis.candidates == ()
        assert diagnosis.gap == timedelta(minutes=30)
        assert diagnosis.shift_forward == local + timedelta(minutes=30)

    def test_gap_exact_boundaries(self, half_hour):
        """02:30 starts the gap, 03:00 is daylight time only."""
        start = half_hour.diagnose_local(datetime(2024, 3, 3, 2, 30))
        end = half_hour.diagnose_local(datetime(2024, 3, 3, 3, 0))
        assert start.kind is LocalTimeKind.GAP
        assert end.kind is LocalTimeKind.UNAMBIGUOUS
        assert end.offsets == (DST,)

    def test_gap_reject_policy(self, half_hour):
        """gap=reject raises NonExistentTimeError with the diagnosis."""
        local = datetime(2024, 3, 3, 2, 45)
        with pytest.raises(NonExistentTimeError) as exc_info:
            half_hour.localize_naive(local)
        diagnosis = exc_info.value.diagnosis
        assert diagnosis.kind is LocalTimeKind.GAP
        assert diagnosis.gap == timedelta(minutes=30)

    def test_gap_shift_forward_policy(self, half_hour):
        """gap=shift-forward moves the time across the skipped range."""
        local = datetime(2024, 3, 3, 2, 45)
        result = half_hour.localize_naive(
            local, gap=GapPolicy.SHIFT_FORWARD
        )
        assert result.replace(tzinfo=None) == datetime(2024, 3, 3, 3, 15)
        assert result.utcoffset() == DST
        assert result.astimezone(timezone.utc) == datetime(
            2024, 3, 2, 21, 15, tzinfo=timezone.utc
        )

    def test_gap_shifted_round_trip(self, half_hour):
        """The shifted time converts back to the same local wall time."""
        info = half_hour.to_tzinfo()
        local = datetime(2024, 3, 3, 2, 45)
        shifted = half_hour.localize_naive(
            local, gap=GapPolicy.SHIFT_FORWARD
        )
        back = shifted.astimezone(timezone.utc).astimezone(info)
        assert back.replace(tzinfo=None) == datetime(2024, 3, 3, 3, 15)

    def test_gap_policy_string_accepted(self, half_hour):
        """The gap policy can be passed as a string."""
        result = half_hour.localize_naive(
            datetime(2024, 3, 3, 2, 45), gap="shift-forward"
        )
        assert result.utcoffset() == DST


class TestCoverage:
    """Behaviour outside the window covered by the VTIMEZONE."""

    def test_before_first_dtstart_is_insufficient(self, half_hour):
        """Times before coverage start report insufficient data, not UTC."""
        local = datetime(1900, 1, 1, 12)
        with pytest.raises(InsufficientTimezoneDataError) as exc_info:
            half_hour.localize_naive(local)
        assert "UTC" not in str(exc_info.value)

    def test_diagnose_before_coverage_raises(self, half_hour):
        """Diagnosis also refuses times earlier than the data."""
        with pytest.raises(InsufficientTimezoneDataError):
            half_hour.diagnose_local(datetime(1899, 12, 31, 23))

    def test_at_coverage_start_is_covered(self):
        """The earliest DTSTART itself is covered, not insufficient."""
        vtimezone = Calendar.from_ical(
            b"BEGIN:VTIMEZONE\nTZID:Start\n"
            b"BEGIN:STANDARD\nDTSTART:20200101T000000\n"
            b"TZOFFSETFROM:+0530\nTZOFFSETTO:+0530\n"
            b"RRULE:FREQ=YEARLY\nEND:STANDARD\nEND:VTIMEZONE"
        )
        localized = vtimezone.localize_naive(datetime(2020, 1, 1, 0))
        assert localized.utcoffset() == STD

    def test_offset_continues_after_last_transition(self):
        """A finite RDATE rule keeps its last offset indefinitely."""
        vtimezone = Calendar.from_ical(
            b"BEGIN:VTIMEZONE\nTZID:Finite\n"
            b"BEGIN:STANDARD\nDTSTART:20200101T000000\n"
            b"TZOFFSETFROM:+0530\nTZOFFSETTO:+0530\n"
            b"RDATE:20200101T000000\nEND:STANDARD\n"
            b"END:VTIMEZONE"
        )
        localized = vtimezone.localize_naive(datetime(2100, 6, 1, 12))
        assert localized.utcoffset() == STD

    def test_no_utc_default_assumed(self, half_hour):
        """Uncovered times never silently resolve with a zero offset."""
        try:
            half_hour.localize_naive(datetime(1900, 1, 1, 12))
        except InsufficientTimezoneDataError:
            pass
        else:  # pragma: no cover
            pytest.fail("Expected InsufficientTimezoneDataError")


NEGATIVE_DST_VTIMEZONE = b"""BEGIN:VTIMEZONE
TZID:Custom/Negative
BEGIN:STANDARD
DTSTART:19700101T030000
TZOFFSETFROM:+0100
TZOFFSETTO:+0200
TZNAME:NST
RRULE:FREQ=YEARLY;BYMONTH=10;BYMONTHDAY=25;BYHOUR=3
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:19700329T020000
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
TZNAME:NDT
RRULE:FREQ=YEARLY;BYMONTH=3;BYMONTHDAY=29;BYHOUR=2
END:DAYLIGHT
END:VTIMEZONE"""

MIDNIGHT_VTIMEZONE = b"""BEGIN:VTIMEZONE
TZID:Custom/Midnight
BEGIN:STANDARD
DTSTART:19700101T000000
TZOFFSETFROM:+0400
TZOFFSETTO:+0300
TZNAME:MS
RRULE:FREQ=YEARLY;BYMONTH=6;BYMONTHDAY=1;BYHOUR=0
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:19700101T000000
TZOFFSETFROM:+0300
TZOFFSETTO:+0400
TZNAME:MD
RRULE:FREQ=YEARLY;BYMONTH=12;BYMONTHDAY=1;BYHOUR=0
END:DAYLIGHT
END:VTIMEZONE"""


class TestSpecialRules:
    """Cross-midnight transitions and negative DST offsets."""

    def test_negative_dst_fold(self):
        """DST offset smaller than standard produces a backward fold."""
        vtimezone = Calendar.from_ical(NEGATIVE_DST_VTIMEZONE)
        # DAYLIGHT onset 2024-03-29 02:00 wall: +02:00 -> +01:00 makes
        # 01:00..02:00 repeat; STANDARD onset 2024-10-25 03:00 wall:
        # +01:00 -> +02:00 skips 03:00..04:00.
        fold = vtimezone.diagnose_local(datetime(2024, 3, 29, 1, 30))
        assert fold.kind is LocalTimeKind.FOLD
        assert fold.offsets == (timedelta(hours=2), timedelta(hours=1))
        gap = vtimezone.diagnose_local(datetime(2024, 10, 25, 3, 30))
        assert gap.kind is LocalTimeKind.GAP
        assert gap.gap == timedelta(hours=1)

    def test_midnight_transition(self):
        """A transition exactly at midnight changes the previous day."""
        vtimezone = Calendar.from_ical(MIDNIGHT_VTIMEZONE)
        # 2024-06-01 00:00 +04:00 -> +03:00 repeats 23:00..24:00 of
        # May 31.
        fold = vtimezone.diagnose_local(datetime(2024, 5, 31, 23, 30))
        assert fold.kind is LocalTimeKind.FOLD
        assert fold.offsets == (timedelta(hours=4), timedelta(hours=3))
        earlier = vtimezone.localize_naive(
            datetime(2024, 5, 31, 23, 30), fold="earlier"
        )
        assert earlier.astimezone(timezone.utc) == datetime(
            2024, 5, 31, 19, 30, tzinfo=timezone.utc
        )
        # 2024-12-01 00:00 +03:00 -> +04:00 skips 00:00..01:00 wall.
        gap = vtimezone.diagnose_local(datetime(2024, 12, 1, 0, 30))
        assert gap.kind is LocalTimeKind.GAP
        assert gap.gap == timedelta(hours=1)

    def test_historical_dtstart_identity(self):
        """One-off 1940s rules round-trip on both sides of the changes."""
        vtimezone = Calendar.from_ical(
            b"BEGIN:VTIMEZONE\nTZID:Hist\n"
            b"BEGIN:STANDARD\nDTSTART:19451001T020000\n"
            b"TZOFFSETFROM:+0600\nTZOFFSETTO:+0530\n"
            b"RDATE:19451001T020000\nEND:STANDARD\n"
            b"BEGIN:DAYLIGHT\nDTSTART:19450401T023000\n"
            b"TZOFFSETFROM:+0530\nTZOFFSETTO:+0600\n"
            b"RDATE:19450401T023000\nEND:DAYLIGHT\n"
            b"END:VTIMEZONE"
        )
        info = vtimezone.to_tzinfo()
        for local, offset in (
            (datetime(1945, 6, 1, 12), DST),
            (datetime(1945, 12, 1, 12), STD),
        ):
            localized = vtimezone.localize_naive(local)
            assert localized.utcoffset() == offset
            back = localized.astimezone(timezone.utc).astimezone(info)
            assert back.replace(tzinfo=None) == local
        fold = vtimezone.diagnose_local(datetime(1945, 10, 1, 1, 45))
        assert fold.kind is LocalTimeKind.FOLD


class TestVTIMEZONEInfo:
    """The pure VTIMEZONE tzinfo implementation."""

    def test_tzinfo_is_vtimezone_info(self, half_hour):
        """to_tzinfo returns the VTIMEZONE-based implementation."""
        assert isinstance(half_hour.to_tzinfo(), VTIMEZONEInfo)

    def test_tzinfo_does_not_use_zoneinfo(self, half_hour, monkeypatch):
        """The conversion must not consult the OS timezone database."""
        import zoneinfo

        def fail(*args, **kwargs):
            raise AssertionError("OS timezone database must not be used")

        monkeypatch.setattr(zoneinfo, "ZoneInfo", fail)
        info = half_hour.to_tzinfo()
        localized = half_hour.localize_naive(datetime(2024, 6, 15, 12))
        assert localized.astimezone(timezone.utc).astimezone(info) is not None

    def test_from_utc(self, half_hour):
        """from_utc converts an aware UTC datetime to local time."""
        utc = datetime(2024, 6, 15, 6, 0, tzinfo=timezone.utc)
        assert half_hour.from_utc(utc).replace(tzinfo=None) == datetime(
            2024, 6, 15, 12
        )
        assert half_hour.from_utc(utc).utcoffset() == DST

    def test_from_utc_rejects_naive(self, half_hour):
        """from_utc requires a timezone-aware datetime."""
        with pytest.raises(ValueError, match="aware"):
            half_hour.from_utc(datetime(2024, 6, 15, 6))

    def test_localize_rejects_aware(self, half_hour):
        """localize_naive requires a naive datetime."""
        with pytest.raises(ValueError, match="naive"):
            half_hour.localize_naive(
                datetime(2024, 6, 15, 12, tzinfo=timezone.utc)
            )

    def test_tzid_attribute_matches_component(self, half_hour):
        """The tzinfo carries the TZID for serialization."""
        info = half_hour.to_tzinfo()
        assert info.zone == "Custom/HalfHour"
        assert info.key == "Custom/HalfHour"

    def test_pickle_round_trip(self, half_hour):
        """The tzinfo can be pickled and restored."""
        import pickle

        info = half_hour.to_tzinfo()
        restored = pickle.loads(pickle.dumps(info))
        localized = half_hour.localize_naive(datetime(2024, 6, 15, 12))
        assert localized.astimezone(restored).utcoffset() == DST


class TestICSWriteBack:
    """Events created with the custom tzinfo serialize and re-parse."""

    def test_event_with_custom_offset_round_trips(self, half_hour):
        """An ambiguous event serializes with TZID and parses back."""
        later = half_hour.localize_naive(
            datetime(2024, 11, 3, 1, 45), fold="later"
        )
        calendar = Calendar()
        calendar.add("version", "2.0")
        calendar.add("prodid", "-//Test//EN")
        calendar.add_component(half_hour)
        event = Event()
        event.add("uid", "event@example.com")
        event.add("dtstart", later)
        event.add("summary", "fold")
        calendar.add_component(event)

        written = calendar.to_ical()
        assert b"DTSTART;TZID=Custom/HalfHour:20241103T014500" in written
        assert b"TZOFFSETTO:+0530" in written
        assert b"TZOFFSETTO:+0600" in written

        reparsed = Calendar.from_ical(written)
        reparsed_tz = reparsed.walk("VTIMEZONE")[0]
        reparsed_start = reparsed.walk("VEVENT")[0]["DTSTART"].dt
        diagnosis = reparsed_tz.diagnose_local(
            reparsed_start.replace(tzinfo=None)
        )
        assert diagnosis.kind is LocalTimeKind.FOLD
        # Both candidates are still available after the ICS round trip.
        assert diagnosis.offsets == (DST, STD)

    def test_unambiguous_event_preserves_utc(self, half_hour):
        """An ordinary event keeps the same UTC instant after write-back."""
        localized = half_hour.localize_naive(datetime(2024, 6, 15, 12))
        calendar = Calendar()
        calendar.add("version", "2.0")
        calendar.add("prodid", "-//Test//EN")
        calendar.add_component(half_hour)
        event = Event()
        event.add("uid", "event2@example.com")
        event.add("dtstart", localized)
        calendar.add_component(event)
        reparsed = Calendar.from_ical(calendar.to_ical())
        reparsed_start = reparsed.walk("VEVENT")[0]["DTSTART"].dt
        assert reparsed_start.astimezone(timezone.utc) == datetime(
            2024, 6, 15, 6, 0, tzinfo=timezone.utc
        )
