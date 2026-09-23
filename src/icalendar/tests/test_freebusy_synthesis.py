"""Tests for :mod:`icalendar.freebusy`, the VFREEBUSY timeline synthesis."""

from datetime import datetime, timedelta, timezone
from itertools import pairwise, permutations

import pytest

from icalendar import (
    FBTYPE_UNKNOWN,
    FreeBusy,
    FreeBusySegment,
    FreeBusySource,
    synthesize_freebusy,
)
from icalendar.error import InvalidCalendar

UTC = timezone.utc


def at(hour, minute=0):
    """A UTC time on the test day."""
    return datetime(2026, 3, 10, hour, minute, tzinfo=UTC)


@pytest.fixture
def component_a():
    """Busy periods in both PERIOD forms, one starting before the window."""
    return FreeBusy.from_ical(
        "BEGIN:VFREEBUSY\n"
        "UID:a@example.com\n"
        "DTSTAMP:20260310T000000Z\n"
        "FREEBUSY:20260310T070000Z/20260310T090000Z\n"
        "FREEBUSY:20260310T090000Z/PT1H\n"
        "END:VFREEBUSY"
    )


@pytest.fixture
def component_b():
    """A tentative busy period."""
    return FreeBusy.from_ical(
        "BEGIN:VFREEBUSY\n"
        "UID:b@example.com\n"
        "DTSTAMP:20260310T000000Z\n"
        "FREEBUSY;FBTYPE=BUSY-TENTATIVE:20260310T093000Z/20260310T103000Z\n"
        "END:VFREEBUSY"
    )


@pytest.fixture
def component_c():
    """A free period and a period with an unknown FBTYPE."""
    return FreeBusy.from_ical(
        "BEGIN:VFREEBUSY\n"
        "UID:c@example.com\n"
        "DTSTAMP:20260310T000000Z\n"
        "FREEBUSY;FBTYPE=FREE:20260310T100000Z/20260310T110000Z\n"
        "FREEBUSY;FBTYPE=X-UNKNOWN-TYPE:20260310T103000Z/20260310T110000Z\n"
        "END:VFREEBUSY"
    )


def sources(*uid_fbtype):
    """A source set from (uid, fbtype) pairs."""
    return frozenset(
        FreeBusySource(uid=uid, fbtype=fbtype) for uid, fbtype in uid_fbtype
    )


def test_timeline_is_split_and_merged(component_a, component_b, component_c):
    """The window is clipped, split at every endpoint, and merged back."""
    report = synthesize_freebusy([component_a, component_b, component_c], at(8), at(12))

    assert report.segments == (
        # Clipped at the window start; the two touching busy periods of
        # component A merge because type and sources are identical.
        FreeBusySegment(at(8), at(9, 30), "BUSY", sources(("a@example.com", "BUSY"))),
        # The tentative period joins: same type, new sources, new segment.
        FreeBusySegment(
            at(9, 30),
            at(10),
            "BUSY",
            sources(("a@example.com", "BUSY"), ("b@example.com", "BUSY-TENTATIVE")),
        ),
        # A's periods end at 10:00; the tentative type now wins.
        FreeBusySegment(
            at(10),
            at(10, 30),
            "BUSY-TENTATIVE",
            sources(("b@example.com", "BUSY-TENTATIVE"), ("c@example.com", "FREE")),
        ),
        # The unknown FBTYPE ranks as BUSY and outranks free; the
        # tentative period ends at 10:30 and does not reach this slice.
        FreeBusySegment(
            at(10, 30),
            at(11),
            "BUSY",
            sources(
                ("c@example.com", "FREE"),
                ("c@example.com", "X-UNKNOWN-TYPE"),
            ),
        ),
        # Nothing covers the last hour: unknown, not free.
        FreeBusySegment(at(11), at(12), FBTYPE_UNKNOWN, frozenset()),
    )


def test_report_covers_exactly_the_window(component_a, component_b, component_c):
    """The segments tile the window without gaps or overlaps."""
    report = synthesize_freebusy([component_a, component_b, component_c], at(8), at(12))

    assert report.start == at(8)
    assert report.end == at(12)
    for segment, nxt in pairwise(report.segments):
        assert segment.end == nxt.start
    assert all(segment.start < segment.end for segment in report.segments)


def test_input_order_does_not_change_the_result(component_a, component_b, component_c):
    """Every permutation of the components yields the same report."""
    components = [component_a, component_b, component_c]
    reports = [
        synthesize_freebusy(list(permutation), at(8), at(12))
        for permutation in permutations(components)
    ]

    assert all(report == reports[0] for report in reports)


def test_caller_priority_picks_the_effective_type(component_c):
    """A priority that puts FREE first makes the free period win."""
    report = synthesize_freebusy(
        [component_c],
        at(10),
        at(11),
        fbtype_priority=["FREE", "BUSY", "BUSY-TENTATIVE"],
    )

    assert [segment.fbtype for segment in report.segments] == [
        "FREE",
        # The unknown FBTYPE still ranks as BUSY, which the caller
        # listed below FREE.
        "FREE",
    ]
    assert report.segments[0].fbtype == "FREE"


def test_unknown_fbtype_ranks_as_busy(component_c):
    """An unrecognized FBTYPE is treated as BUSY, not as free."""
    report = synthesize_freebusy([component_c], at(10, 30), at(11))

    assert len(report.segments) == 1
    assert report.segments[0].fbtype == "BUSY"
    # The raw value is kept in the source detail.
    assert report.segments[0].sources == sources(
        ("c@example.com", "FREE"), ("c@example.com", "X-UNKNOWN-TYPE")
    )


def test_uncovered_time_is_unknown_not_free(component_a):
    """Time no input covers is marked unknown."""
    report = synthesize_freebusy([component_a], at(8), at(12))

    uncovered = report.segments[-1]
    assert uncovered.fbtype == FBTYPE_UNKNOWN
    assert uncovered.is_unknown
    assert uncovered.sources == frozenset()


def test_touching_periods_do_not_overlap():
    """A period ending where another begins contributes nothing across."""
    first = FreeBusy.new(uid="first@example.com")
    first.add("FREEBUSY", (at(8), at(9)))
    second = FreeBusy.new(uid="second@example.com")
    second.add("FREEBUSY", (at(9), at(10)))

    report = synthesize_freebusy([first, second], at(8), at(10))

    assert report.segments == (
        FreeBusySegment(at(8), at(9), "BUSY", sources(("first@example.com", "BUSY"))),
        FreeBusySegment(at(9), at(10), "BUSY", sources(("second@example.com", "BUSY"))),
    )


def test_zero_length_period_is_ignored(component_a):
    """A zero-length period adds neither coverage nor endpoints."""
    component = FreeBusy.from_ical(
        "BEGIN:VFREEBUSY\n"
        "UID:zero@example.com\n"
        "DTSTAMP:20260310T000000Z\n"
        "FREEBUSY:20260310T083000Z/PT0S\n"
        "END:VFREEBUSY"
    )

    with_zero = synthesize_freebusy([component_a, component], at(8), at(10))
    without_zero = synthesize_freebusy([component_a], at(8), at(10))

    assert with_zero == without_zero


def test_negative_duration_is_rejected():
    """A period whose end precedes its start is invalid."""
    component = FreeBusy.new(uid="broken@example.com")
    component.add("FREEBUSY", (at(10), at(9)), encode=False)

    with pytest.raises(InvalidCalendar):
        synthesize_freebusy([component], at(8), at(12))


def test_window_end_before_start_is_rejected():
    """The query window must not be negative."""
    with pytest.raises(InvalidCalendar):
        synthesize_freebusy([], at(12), at(8))


def test_empty_window_has_no_segments():
    """A zero-length query window yields an empty timeline."""
    report = synthesize_freebusy([], at(8), at(8))

    assert report.segments == ()


def test_naive_window_is_read_as_utc(component_a):
    """Naive window bounds are interpreted as UTC."""
    report = synthesize_freebusy(
        [component_a], datetime(2026, 3, 10, 8), datetime(2026, 3, 10, 10)
    )

    assert report.start == at(8)
    assert report.end == at(10)


def test_to_freebusy_writes_standard_periods(component_a, component_b, component_c):
    """The VFREEBUSY output is standard; the sources stay in the report."""
    report = synthesize_freebusy([component_a, component_b, component_c], at(8), at(12))

    free_busy = report.to_freebusy(uid="out@example.com")
    # Unfold the content lines for the assertions.
    ical = free_busy.to_ical().decode().replace("\r\n ", "")

    assert "FREEBUSY;FBTYPE=BUSY;VALUE=PERIOD:20260310T080000Z/20260310T093000Z" in ical
    assert "FREEBUSY;FBTYPE=BUSY;VALUE=PERIOD:20260310T093000Z/20260310T100000Z" in ical
    assert (
        "FREEBUSY;FBTYPE=BUSY-TENTATIVE;VALUE=PERIOD:20260310T100000Z/20260310T103000Z"
        in ical
    )
    assert "FREEBUSY;FBTYPE=BUSY;VALUE=PERIOD:20260310T103000Z/20260310T110000Z" in ical
    # The unknown segment is not claimed, and no source detail leaks
    # into the PERIOD text.
    assert "20260310T110000Z/20260310T120000Z" not in ical
    assert "a@example.com" not in ical
    assert "b@example.com" not in ical
    assert "c@example.com" not in ical
    assert "X-UNKNOWN-TYPE" not in ical


def test_to_freebusy_round_trips_through_the_synthesis(
    component_a, component_b, component_c
):
    """Synthesizing the rendered VFREEBUSY reproduces the known segments."""
    report = synthesize_freebusy([component_a, component_b, component_c], at(8), at(12))

    rendered = report.to_freebusy(uid="out@example.com")
    parsed = FreeBusy.from_ical(rendered.to_ical())
    again = synthesize_freebusy([parsed], at(8), at(12))

    # The rendered component has a single source, so adjacent slices of
    # the same type merge, but the busy types and times are preserved.
    assert [
        (segment.start, segment.end, segment.fbtype) for segment in again.segments
    ] == [
        (at(8), at(10), "BUSY"),
        (at(10), at(10, 30), "BUSY-TENTATIVE"),
        (at(10, 30), at(11), "BUSY"),
        (at(11), at(12), FBTYPE_UNKNOWN),
    ]


def test_duration_and_end_forms_are_equivalent():
    """A period written with a duration equals one written with an end."""
    by_end = FreeBusy.new(uid="end@example.com")
    by_end.add("FREEBUSY", (at(9), at(10)))
    by_duration = FreeBusy.new(uid="duration@example.com")
    by_duration.add("FREEBUSY", (at(9), timedelta(hours=1)))

    report = synthesize_freebusy([by_end, by_duration], at(8), at(11))

    assert report.segments == (
        FreeBusySegment(at(8), at(9), FBTYPE_UNKNOWN, frozenset()),
        FreeBusySegment(
            at(9),
            at(10),
            "BUSY",
            sources(("end@example.com", "BUSY"), ("duration@example.com", "BUSY")),
        ),
        FreeBusySegment(at(10), at(11), FBTYPE_UNKNOWN, frozenset()),
    )
