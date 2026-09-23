"""Tests for :mod:`icalendar.free_busy_synthesis`."""

from datetime import date, datetime, timezone
from itertools import pairwise, permutations
from types import SimpleNamespace

import pytest

from icalendar import Calendar, FreeBusy
from icalendar.enums import FBTYPE
from icalendar.error import InvalidCalendar
from icalendar.free_busy_synthesis import compose_free_busy, unknown

UTC = timezone.utc


def dt(hour, minute=0):
    """A UTC datetime on the test day."""
    return datetime(2026, 5, 1, hour, minute, tzinfo=UTC)


WINDOW_START = dt(0)
WINDOW_END = dt(18)

THREE_COMPONENT_ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//icalendar//test//EN
BEGIN:VFREEBUSY
UID:one@example.com
DTSTAMP:20260401T000000Z
FREEBUSY;FBTYPE=BUSY:20260501T100000Z/20260501T140000Z
END:VFREEBUSY
BEGIN:VFREEBUSY
UID:two@example.com
DTSTAMP:20260401T000000Z
FREEBUSY;FBTYPE=BUSY-TENTATIVE:20260501T120000Z/PT2H
END:VFREEBUSY
BEGIN:VFREEBUSY
UID:three@example.com
DTSTAMP:20260401T000000Z
FREEBUSY;FBTYPE=X-HOLD:20260501T110000Z/20260501T130000Z
FREEBUSY;FBTYPE=FREE:20260501T150000Z/20260501T160000Z
FREEBUSY;FBTYPE=FREE:20260501T160000Z/20260501T170000Z
END:VFREEBUSY
END:VCALENDAR
"""


@pytest.fixture
def three_component_calendar():
    """A calendar with busy, tentative, free and unknown FBTYPE periods."""
    return Calendar.from_ical(THREE_COMPONENT_ICS)


@pytest.fixture
def three_components(three_component_calendar):
    """The three VFREEBUSY components of the fixture calendar."""
    return three_component_calendar.freebusy


def summarize(timeline):
    """A comparable summary of a timeline."""
    return [
        (
            segment.start,
            segment.end,
            str(segment.fbtype),
            tuple(sorted({source.uid for source in segment.sources})),
        )
        for segment in timeline
    ]


def coverage(timeline):
    """Merge adjacent segments with the same type, ignoring provenance."""
    merged = []
    for segment in timeline:
        fbtype = str(segment.fbtype)
        if merged and merged[-1][2] == fbtype:
            merged[-1] = (merged[-1][0], segment.end, fbtype)
        else:
            merged.append((segment.start, segment.end, fbtype))
    return merged


EXPECTED_DEMO = [
    (dt(0), dt(10), "UNKNOWN", ()),
    (dt(10), dt(11), "BUSY", ("one@example.com",)),
    (dt(11), dt(12), "BUSY", ("one@example.com", "three@example.com")),
    (
        dt(12),
        dt(13),
        "BUSY",
        ("one@example.com", "three@example.com", "two@example.com"),
    ),
    (dt(13), dt(14), "BUSY", ("one@example.com", "two@example.com")),
    (dt(14), dt(15), "UNKNOWN", ()),
    (dt(15), dt(17), "FREE", ("three@example.com",)),
    (dt(17), dt(18), "UNKNOWN", ()),
]


def test_three_components_split_then_merge(three_components):
    """The window is split at every endpoint, then merged where possible."""
    timeline = compose_free_busy(three_components, WINDOW_START, WINDOW_END)
    assert summarize(timeline) == EXPECTED_DEMO


def test_calendar_container_is_unpacked(three_component_calendar, three_components):
    """A VCALENDAR is searched for VFREEBUSY subcomponents."""
    from_calendar = compose_free_busy(
        [three_component_calendar], WINDOW_START, WINDOW_END
    )
    from_components = compose_free_busy(three_components, WINDOW_START, WINDOW_END)
    assert summarize(from_calendar) == summarize(from_components)


def test_input_order_does_not_change_output(three_components):
    """No permutation of the components changes the timeline."""
    timelines = [
        compose_free_busy(list(order), WINDOW_START, WINDOW_END)
        for order in permutations(three_components)
    ]
    first = timelines[0]
    for other in timelines[1:]:
        assert other == first


def test_property_order_does_not_change_output():
    """The order of FREEBUSY properties inside a component is irrelevant."""
    ics_template = """BEGIN:VFREEBUSY
UID:u@example.com
DTSTAMP:20260401T000000Z
%sEND:VFREEBUSY
"""
    forwards = ics_template % (
        "FREEBUSY;FBTYPE=BUSY:20260501T100000Z/20260501T110000Z\n"
        "FREEBUSY;FBTYPE=BUSY:20260501T110000Z/20260501T120000Z\n"
    )
    backwards = ics_template % (
        "FREEBUSY;FBTYPE=BUSY:20260501T110000Z/20260501T120000Z\n"
        "FREEBUSY;FBTYPE=BUSY:20260501T100000Z/20260501T110000Z\n"
    )
    first = compose_free_busy([FreeBusy.from_ical(forwards)], WINDOW_START, WINDOW_END)
    second = compose_free_busy(
        [FreeBusy.from_ical(backwards)], WINDOW_START, WINDOW_END
    )
    assert summarize(first) == summarize(second)


def test_timeline_covers_the_whole_window(three_components):
    """Segments are contiguous and span exactly the query window."""
    timeline = compose_free_busy(three_components, WINDOW_START, WINDOW_END)
    assert timeline.start == WINDOW_START
    assert timeline.end == WINDOW_END
    assert timeline.segments[0].start == WINDOW_START
    assert timeline.segments[-1].end == WINDOW_END
    for left, right in pairwise(timeline.segments):
        assert left.end == right.start


def free_busy_with(uid, *lines):
    """Build a VFREEBUSY component from FREEBUSY content lines."""
    ics = "\n".join(
        [
            "BEGIN:VFREEBUSY",
            f"UID:{uid}",
            "DTSTAMP:20260401T000000Z",
            *lines,
            "END:VFREEBUSY",
            "",
        ]
    )
    return FreeBusy.from_ical(ics)


def test_duration_and_end_form_cover_the_same_time():
    """Both PERIOD notations of RFC 5545 are supported."""
    by_end = free_busy_with("end@x", "FREEBUSY:20260501T100000Z/20260501T120000Z")
    by_duration = free_busy_with("dur@x", "FREEBUSY:20260501T100000Z/PT2H")
    timeline_end = compose_free_busy([by_end], WINDOW_START, WINDOW_END)
    timeline_dur = compose_free_busy([by_duration], WINDOW_START, WINDOW_END)
    busy_end = timeline_end.segments[1]
    busy_dur = timeline_dur.segments[1]
    assert (busy_end.start, busy_end.end) == (dt(10), dt(12))
    assert (busy_dur.start, busy_dur.end) == (dt(10), dt(12))
    assert busy_end.sources[0].by_duration is False
    assert busy_dur.sources[0].by_duration is True


def test_touching_endpoints_do_not_overlap():
    """A period ending at t does not cover the slice starting at t."""
    first = free_busy_with("a@x", "FREEBUSY:20260501T100000Z/20260501T120000Z")
    second = free_busy_with(
        "b@x", "FREEBUSY;FBTYPE=BUSY-TENTATIVE:20260501T120000Z/20260501T140000Z"
    )
    timeline = compose_free_busy([first, second], WINDOW_START, WINDOW_END)
    assert summarize(timeline)[1:3] == [
        (dt(10), dt(12), "BUSY", ("a@x",)),
        (dt(12), dt(14), "BUSY-TENTATIVE", ("b@x",)),
    ]


def test_same_type_from_different_components_does_not_merge():
    """Adjacent slices merge only when type and source set both match."""
    first = free_busy_with("a@x", "FREEBUSY:20260501T100000Z/20260501T120000Z")
    second = free_busy_with("b@x", "FREEBUSY:20260501T120000Z/20260501T140000Z")
    timeline = compose_free_busy([first, second], WINDOW_START, WINDOW_END)
    assert summarize(timeline)[1:3] == [
        (dt(10), dt(12), "BUSY", ("a@x",)),
        (dt(12), dt(14), "BUSY", ("b@x",)),
    ]


def test_adjacent_periods_of_one_component_merge():
    """Two adjacent periods of the same component and FBTYPE are one."""
    component = free_busy_with(
        "a@x",
        "FREEBUSY:20260501T100000Z/20260501T110000Z",
        "FREEBUSY:20260501T110000Z/20260501T120000Z",
    )
    timeline = compose_free_busy([component], WINDOW_START, WINDOW_END)
    busy = [s for s in timeline if s.fbtype == FBTYPE.BUSY]
    assert len(busy) == 1
    assert (busy[0].start, busy[0].end) == (dt(10), dt(12))
    assert len(busy[0].sources) == 2


def test_zero_length_period_is_ignored():
    """A period with start == end contributes nothing."""
    component = free_busy_with("a@x", "FREEBUSY:20260501T100000Z/20260501T100000Z")
    timeline = compose_free_busy([component], WINDOW_START, WINDOW_END)
    assert all(segment.is_unknown for segment in timeline)


def test_negative_period_is_rejected():
    """A period ending before it starts is invalid input."""
    component = free_busy_with("a@x", "FREEBUSY:20260501T100000Z/20260501T110000Z")
    broken = SimpleNamespace(
        start=dt(11), end=dt(10), FBTYPE=FBTYPE.BUSY, by_duration=False
    )
    component["FREEBUSY"] = broken
    with pytest.raises(InvalidCalendar, match="negative"):
        compose_free_busy([component], WINDOW_START, WINDOW_END)


def test_non_period_freebusy_value_is_rejected():
    """A FREEBUSY property that is not a period is invalid input."""
    component = free_busy_with("a@x", "FREEBUSY:20260501T100000Z/20260501T110000Z")
    component["FREEBUSY"] = "not a period"
    with pytest.raises(InvalidCalendar, match="PERIOD"):
        compose_free_busy([component], WINDOW_START, WINDOW_END)


def test_uncovered_time_is_unknown_not_free(three_components):
    """Uncovered slices are marked unknown and never claimed as FREE."""
    timeline = compose_free_busy(three_components, WINDOW_START, WINDOW_END)
    uncovered = [segment for segment in timeline if segment.is_unknown]
    assert uncovered
    for segment in uncovered:
        assert segment.fbtype is unknown
        assert segment.fbtype != FBTYPE.FREE
        assert str(segment.fbtype) == "UNKNOWN"
        assert segment.sources == ()


def test_caller_priority_decides_overlaps():
    """The caller can rank FREE above BUSY."""
    busy = free_busy_with("a@x", "FREEBUSY:20260501T100000Z/20260501T120000Z")
    free = free_busy_with(
        "b@x", "FREEBUSY;FBTYPE=FREE:20260501T110000Z/20260501T130000Z"
    )
    timeline = compose_free_busy(
        [busy, free],
        WINDOW_START,
        WINDOW_END,
        fbtype_priority=[FBTYPE.FREE, FBTYPE.BUSY],
    )
    middle = [s for s in timeline if s.start == dt(11)][0]
    assert middle.fbtype == FBTYPE.FREE
    assert {source.uid for source in middle.sources} == {"a@x", "b@x"}


def test_unknown_fbtype_is_treated_like_busy():
    """Unknown tokens share the rank of BUSY; BUSY wins the tie."""
    busy = free_busy_with("a@x", "FREEBUSY:20260501T100000Z/20260501T120000Z")
    weird = free_busy_with(
        "b@x", "FREEBUSY;FBTYPE=X-HOLD:20260501T100000Z/20260501T120000Z"
    )
    timeline = compose_free_busy([weird, busy], WINDOW_START, WINDOW_END)
    busy_segment = [s for s in timeline if s.start == dt(10)][0]
    assert busy_segment.fbtype == FBTYPE.BUSY
    assert {str(source.fbtype) for source in busy_segment.sources} == {
        "BUSY",
        "X-HOLD",
    }


def test_unknown_fbtype_alone_keeps_its_token():
    """An unknown FBTYPE alone on a slice stays visible as its raw token."""
    weird = free_busy_with(
        "b@x", "FREEBUSY;FBTYPE=X-HOLD:20260501T100000Z/20260501T120000Z"
    )
    timeline = compose_free_busy([weird], WINDOW_START, WINDOW_END)
    segment = [s for s in timeline if s.start == dt(10)][0]
    assert segment.fbtype == "X-HOLD"


def test_missing_fbtype_defaults_to_busy():
    """A FREEBUSY property without FBTYPE means BUSY."""
    component = free_busy_with("a@x", "FREEBUSY:20260501T100000Z/20260501T110000Z")
    timeline = compose_free_busy([component], WINDOW_START, WINDOW_END)
    segment = [s for s in timeline if s.start == dt(10)][0]
    assert segment.fbtype == FBTYPE.BUSY
    assert segment.sources[0].fbtype == FBTYPE.BUSY


def test_window_must_not_be_empty_or_reversed():
    """A zero-length or reversed query window is rejected."""
    with pytest.raises(InvalidCalendar, match="window"):
        compose_free_busy([], WINDOW_END, WINDOW_START)
    with pytest.raises(InvalidCalendar, match="window"):
        compose_free_busy([], WINDOW_START, WINDOW_START)


def test_window_must_be_datetimes():
    """Plain dates are not accepted as the query window."""
    with pytest.raises(InvalidCalendar, match="datetime"):
        compose_free_busy([], date(2026, 5, 1), WINDOW_END)


def test_naive_window_is_assumed_utc():
    """Naive datetimes are read as UTC."""
    component = free_busy_with("a@x", "FREEBUSY:20260501T100000Z/20260501T110000Z")
    timeline = compose_free_busy(
        [component], datetime(2026, 5, 1, 0), datetime(2026, 5, 1, 18)
    )
    assert timeline.start == WINDOW_START
    busy = [s for s in timeline if not s.is_unknown]
    assert (busy[0].start, busy[0].end) == (dt(10), dt(11))


def test_periods_are_clipped_to_the_window():
    """Periods sticking out of the window are clipped, sources included."""
    component = free_busy_with("a@x", "FREEBUSY:20260501T080000Z/20260501T200000Z")
    timeline = compose_free_busy([component], dt(9), dt(12))
    assert summarize(timeline) == [(dt(9), dt(12), "BUSY", ("a@x",))]
    source = timeline.segments[0].sources[0]
    assert (source.start, source.end) == (dt(9), dt(12))


def test_periods_outside_the_window_are_ignored():
    """Periods not intersecting the window contribute nothing."""
    component = free_busy_with("a@x", "FREEBUSY:20260501T200000Z/20260501T210000Z")
    timeline = compose_free_busy([component], WINDOW_START, WINDOW_END)
    assert all(segment.is_unknown for segment in timeline)


def test_vevent_is_not_expanded():
    """VEVENT components are ignored; no event expansion happens."""
    ics = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//icalendar//test//EN
BEGIN:VEVENT
UID:event@example.com
DTSTAMP:20260401T000000Z
DTSTART:20260501T100000Z
DTEND:20260501T120000Z
END:VEVENT
END:VCALENDAR
"""
    calendar = Calendar.from_ical(ics)
    timeline = compose_free_busy([calendar], WINDOW_START, WINDOW_END)
    assert all(segment.is_unknown for segment in timeline)


def test_write_back_uses_standard_periods(three_components):
    """The VFREEBUSY write-back carries FBTYPE and start/end PERIODs."""
    timeline = compose_free_busy(three_components, WINDOW_START, WINDOW_END)
    component = timeline.to_free_busy(stamp=datetime(2026, 4, 1, tzinfo=UTC))
    ical = component.to_ical().decode()
    assert "FREEBUSY;FBTYPE=BUSY:20260501T100000Z/20260501T110000Z" in ical
    assert "FREEBUSY;FBTYPE=BUSY:20260501T110000Z/20260501T120000Z" in ical
    assert "FREEBUSY;FBTYPE=BUSY:20260501T120000Z/20260501T130000Z" in ical
    assert "FREEBUSY;FBTYPE=BUSY:20260501T130000Z/20260501T140000Z" in ical
    assert "FREEBUSY;FBTYPE=FREE" not in ical
    assert "UNKNOWN" not in ical
    assert "X-HOLD" not in ical
    assert "DTSTART:20260501T000000Z" in ical
    assert "DTEND:20260501T180000Z" in ical


def test_write_back_can_include_free(three_components):
    """FREE segments are published when asked for."""
    timeline = compose_free_busy(three_components, WINDOW_START, WINDOW_END)
    component = timeline.to_free_busy(
        include_free=True, stamp=datetime(2026, 4, 1, tzinfo=UTC)
    )
    ical = component.to_ical().decode()
    assert "FREEBUSY;FBTYPE=FREE:20260501T150000Z/20260501T170000Z" in ical


def test_write_back_roundtrips(three_components):
    """The written VFREEBUSY synthesizes back into the same busy time."""
    timeline = compose_free_busy(three_components, WINDOW_START, WINDOW_END)
    written = timeline.to_free_busy(
        include_free=True, stamp=datetime(2026, 4, 1, tzinfo=UTC)
    )
    reparsed = FreeBusy.from_ical(written.to_ical())
    again = compose_free_busy([reparsed], WINDOW_START, WINDOW_END)
    assert coverage(again) == coverage(timeline)


def test_report_keeps_provenance_separate(three_components):
    """The report explains every slice with its contributing sources."""
    timeline = compose_free_busy(three_components, WINDOW_START, WINDOW_END)
    report = timeline.to_report()
    assert len(report) == len(timeline.segments)
    fullest = next(entry for entry in report if entry["start"] == dt(12))
    assert fullest["fbtype"] == "BUSY"
    assert {source["uid"] for source in fullest["sources"]} == {
        "one@example.com",
        "two@example.com",
        "three@example.com",
    }
    tentative = next(
        source for source in fullest["sources"] if source["uid"] == "two@example.com"
    )
    assert tentative["fbtype"] == "BUSY-TENTATIVE"
    assert (tentative["start"], tentative["end"]) == (dt(12), dt(14))
    uncovered = next(entry for entry in report if entry["start"] == dt(14))
    assert uncovered["fbtype"] == "UNKNOWN"
    assert uncovered["sources"] == []
