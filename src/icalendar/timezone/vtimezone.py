"""Convert local times with the information from a VTIMEZONE component.

This module implements the :rfc:`5545` semantics directly from the
``STANDARD`` and ``DAYLIGHT`` sub-components. It never consults the
operating system timezone database:

*   transitions are generated from ``DTSTART``, ``RRULE`` and ``RDATE``
    inside a finite window,
*   ``EXDATE`` removes individual occurrences,
*   naive local times are classified as unique, repeated (fold) or
    skipped (gap),
*   the caller chooses an explicit :class:`FoldPolicy` and
    :class:`GapPolicy` and receives a :class:`LocalTimeDiagnosis`.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from typing import TYPE_CHECKING, Final

import dateutil.rrule

from icalendar.enums import StrEnum
from icalendar.error import (
    AmbiguousTimeError,
    InsufficientTimezoneDataError,
    NonExistentTimeError,
)

if TYPE_CHECKING:
    from icalendar.cal.timezone import Timezone, TimezoneStandard


class FoldPolicy(StrEnum):
    """What to do with a local time that happens twice.

    When clocks are set back, the wall-clock times between the end of
    daylight time and the end of standard time repeat.

    ``EARLIER``
        Use the offset in effect during the first occurrence.
    ``LATER``
        Use the offset in effect during the second occurrence.
    ``REJECT``
        Raise :class:`AmbiguousTimeError`.
    """

    EARLIER = "earlier"
    LATER = "later"
    REJECT = "reject"


class GapPolicy(StrEnum):
    """What to do with a local time that never happens.

    When clocks jump forward, the wall-clock times between the end of
    the old offset and the beginning of the new offset do not exist.

    ``SHIFT_FORWARD``
        Move the local time forward by the size of the gap.
    ``REJECT``
        Raise :class:`NonExistentTimeError`.
    """

    SHIFT_FORWARD = "shift-forward"
    REJECT = "reject"


class LocalTimeKind(StrEnum):
    """How a naive local time relates to the transitions."""

    UNAMBIGUOUS = "unambiguous"
    FOLD = "fold"
    GAP = "gap"


@dataclass(frozen=True)
class Transition:
    """A single UTC instant at which the offset changes.

    :param utc: UTC datetime of the transition (timezone aware, tz=UTC)
    :param local: wall-clock onset taken from ``DTSTART``/``RDATE``
    :param offset_from: offset in effect immediately before the transition
    :param offset_to: offset in effect immediately after the transition
    :param dst_offset: saving time offset after the transition
    :param is_dst: whether ``offset_to`` is a daylight time offset
    :param tzname: name of the observance in effect after the transition
    """

    utc: datetime
    local: datetime
    offset_from: timedelta
    offset_to: timedelta
    dst_offset: timedelta
    is_dst: bool
    tzname: str

    def __post_init__(self) -> None:
        if self.utc.tzinfo is None:
            raise ValueError("Transition.utc must be timezone aware.")
        if self.local.tzinfo is not None:
            raise ValueError("Transition.local must be a naive datetime.")


@dataclass(frozen=True)
class LocalTimeCandidate:
    """One valid interpretation of a naive local time.

    :param utc: the corresponding UTC instant
    :param offset: UTC offset in effect at that instant
    :param dst_offset: saving time offset in effect
    :param is_dst: whether daylight time is in effect
    :param tzname: name of the observance
    """

    utc: datetime
    offset: timedelta
    dst_offset: timedelta
    is_dst: bool
    tzname: str


@dataclass(frozen=True)
class LocalTimeDiagnosis:
    """The result of classifying a naive local time.

    :param local: the naive local time that was examined
    :param kind: whether the time is unique, repeated or missing
    :param candidates: zero, one or two valid interpretations
    :param earlier: the interpretation in the first occurrence (fold only)
    :param later: the interpretation in the second occurrence (fold only)
    :param gap: length of the skipped wall-clock interval (gap only)
    :param shift_forward: suggested local time after the gap (gap only)
    :param previous_transition: transition starting the repeated/missing range
    :param next_transition: transition ending the repeated/missing range
    """

    local: datetime
    kind: LocalTimeKind
    candidates: tuple[LocalTimeCandidate, ...] = field(default_factory=tuple)
    earlier: LocalTimeCandidate | None = None
    later: LocalTimeCandidate | None = None
    gap: timedelta | None = None
    shift_forward: datetime | None = None
    previous_transition: Transition | None = None
    next_transition: Transition | None = None

    @property
    def is_ambiguous(self) -> bool:
        """Whether the local time occurs twice."""
        return self.kind is LocalTimeKind.FOLD

    @property
    def is_nonexistent(self) -> bool:
        """Whether the local time never occurs."""
        return self.kind is LocalTimeKind.GAP

    @property
    def offsets(self) -> tuple[timedelta, ...]:
        """All offsets that could apply, ordered earlier then later."""
        return tuple(candidate.offset for candidate in self.candidates)


@dataclass(frozen=True)
class _Segment:
    """A half-open range of UTC instants with a constant offset.

    ``[start, end)`` uses UTC for ``start``/``end``. If ``end`` is
    ``None``, the segment extends forever.
    """

    start: datetime
    end: datetime | None
    offset: timedelta
    dst_offset: timedelta
    is_dst: bool
    tzname: str
    transition: Transition | None

_WINDOW_PADDING: Final = timedelta(days=2)
_TRANSITION_CACHE_SIZE: Final = 16


def _round_offset(offset: timedelta) -> timedelta:
    """Round an offset to whole minutes, like the existing code does."""
    total_seconds = int(offset.total_seconds())
    return timedelta(seconds=((total_seconds + 30) // 60) * 60)


def _component_name(component: TimezoneStandard, fallback: str) -> str:
    """Return the TZNAME or build a deterministic name."""
    if "TZNAME" in component:
        try:
            return str(component["TZNAME"])
        except UnicodeEncodeError:
            return component["TZNAME"].encode("ascii", "replace").decode()
    return (
        f"{fallback}_{component['DTSTART'].to_ical().decode('utf-8')}_"
        f"{component['TZOFFSETFROM'].to_ical()}_"
        f"{component['TZOFFSETTO'].to_ical()}"
    )


def _naive_datetime(value: datetime) -> datetime:
    """Return a naive datetime, removing any tzinfo."""
    return value.replace(tzinfo=None)


def _component_occurrences(
    component: TimezoneStandard,
    lower: datetime | None,
    upper: datetime | None,
    include_neighbors: bool = False,
) -> tuple[list[datetime], set[datetime]]:
    """Expand a STANDARD/DAYLIGHT component in a wall-clock window.

    Returns ``(occurrences, neighbors)`` where ``occurrences`` are the
    onsets inside ``[lower, upper]`` and ``neighbors`` are the nearest
    onsets just outside the window (only when requested).
    """
    dtstart = _naive_datetime(component.DTSTART)
    occurrences: set[datetime] = set()
    neighbors: set[datetime] = set()
    excluded: set[datetime] = set()

    def add_occurrence(value: datetime) -> None:
        naive = _naive_datetime(value)
        in_window = (lower is None or naive >= lower) and (
            upper is None or naive <= upper
        )
        if in_window:
            occurrences.add(naive)

    def add_neighbor(value: datetime | None) -> None:
        if value is not None:
            neighbors.add(_naive_datetime(value))

    if "RRULE" in component:
        rrules = component.get("RRULE")
        if not isinstance(rrules, list):
            rrules = [rrules]
        for vrecur in rrules:
            rrulestr = vrecur.to_ical().decode("utf-8")
            rule = dateutil.rrule.rrulestr(rrulestr, dtstart=dtstart)
            # Only the requested window is expanded; iterating a whole
            # infinite rule would never terminate.
            if lower is not None and upper is not None:
                window_occurrences = rule.between(lower, upper, inc=True)
            elif upper is not None:
                window_occurrences = rule.between(dtstart, upper, inc=True)
            elif lower is not None:
                occurrence = rule.after(lower, inc=True)
                window_occurrences = [] if occurrence is None else [occurrence]
            else:
                # No window: nothing is needed from an infinite rule.
                window_occurrences = []
            for occurrence in window_occurrences:
                add_occurrence(occurrence)
            if include_neighbors:
                # before/after also cover finite rules whose range ends
                # before or starts after the window.
                if lower is not None:
                    add_neighbor(rule.before(lower, inc=False))
                if upper is not None:
                    add_neighbor(rule.after(upper, inc=False))
    else:
        # Without an RRULE, DTSTART itself is the single onset, possibly
        # augmented by RDATE values.
        add_occurrence(dtstart)

    if "RDATE" in component:
        rdates = component.get("RDATE")
        if not isinstance(rdates, list):
            rdates = [rdates]
        for tree in rdates:
            for leaf in tree.dts:
                value = leaf.dt
                if isinstance(value, tuple):
                    value = value[0]
                add_occurrence(value)

    if "EXDATE" in component:
        exdates = component.get("EXDATE")
        if not isinstance(exdates, list):
            exdates = [exdates]
        excluded = {
            _naive_datetime(leaf.dt)
            for tree in exdates
            for leaf in tree.dts
        }
        occurrences.difference_update(excluded)
    if include_neighbors:
        neighbors.difference_update(excluded)
    if include_neighbors and "RRULE" not in component:
        # A component without an RRULE only occurs at DTSTART and its
        # RDATE values; expose at most the nearest neighbour on each side.
        all_onsets = {dtstart}
        if "RDATE" in component:
            rdate_values = component.get("RDATE")
            if not isinstance(rdate_values, list):
                rdate_values = [rdate_values]
            for tree in rdate_values:
                for leaf in tree.dts:
                    value = leaf.dt
                    if isinstance(value, tuple):
                        value = value[0]
                    all_onsets.add(_naive_datetime(value))
        if "EXDATE" in component:
            all_onsets.difference_update(excluded)
        before_onsets = sorted(
            value for value in all_onsets if lower is not None and value < lower
        )
        after_onsets = sorted(
            value for value in all_onsets if upper is not None and value > upper
        )
        if before_onsets:
            add_neighbor(before_onsets[-1])
        if after_onsets:
            add_neighbor(after_onsets[0])
    return sorted(occurrences), neighbors


class VTIMEZONEInfo(tzinfo):
    """A ``tzinfo`` computed only from a VTIMEZONE component.

    The operating system timezone database is never consulted. All
    offsets and transition instants come from the ``STANDARD`` and
    ``DAYLIGHT`` sub-components.

    Use :meth:`Timezone.to_tzinfo()
    <icalendar.cal.timezone.Timezone.to_tzinfo>` to create one.
    """

    def __init__(self, timezone_component: Timezone) -> None:
        self.zone = timezone_component.tz_name
        self.key = self.zone
        self._timezone = timezone_component
        self._cache: OrderedDict[tuple[datetime, datetime], tuple[_Segment, ...]] = (
            OrderedDict()
        )
        self._coverage_start: datetime | None = None
        self._coverage_known = False

    def __repr__(self) -> str:
        return f"VTIMEZONEInfo({self.zone!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, VTIMEZONEInfo):
            return NotImplemented
        return self.zone == other.zone and self._timezone is other._timezone

    def __hash__(self) -> int:
        return hash((VTIMEZONEInfo, self.zone))

    def __reduce__(self):
        return VTIMEZONEInfo, (self._timezone,)

    def _get_segments(self, pivot: datetime) -> tuple[_Segment, ...]:
        """Return segments covering ``pivot`` and both neighbours."""
        naive_pivot = _naive_datetime(pivot)
        lower = naive_pivot - _WINDOW_PADDING
        upper = naive_pivot + _WINDOW_PADDING
        cache_key = (lower, upper)
        cached = self._cache.get(cache_key)
        if cached is not None:
            self._cache.move_to_end(cache_key)
            return cached
        window, neighbors = self._timezone._get_transitions_with_neighbors(
            lower, upper, include_neighbors=True
        )
        segments = _build_segments_with_neighbors(
            window, neighbors, lower
        )
        self._cache[cache_key] = segments
        self._cache.move_to_end(cache_key)
        while len(self._cache) > _TRANSITION_CACHE_SIZE:
            self._cache.popitem(last=False)
        return segments

    def _coverage_start_datetime(self) -> datetime | None:
        """Earliest wall-clock time the components cover."""
        if not self._coverage_known:
            self._coverage_start = self._timezone.coverage_start
            self._coverage_known = True
        return self._coverage_start

    def _check_coverage(self, naive: datetime) -> None:
        """Raise if the components do not cover ``naive`` at all."""
        start = self._coverage_start_datetime()
        if start is not None and naive < start:
            raise InsufficientTimezoneDataError(
                f"The VTIMEZONE {self.zone!r} only covers local times "
                f"from {start.isoformat()} onward; {naive.isoformat()} "
                "is earlier than any DTSTART/RDATE. Add sub-components "
                "that cover the requested window.",
            )

    def diagnose(self, local: datetime) -> LocalTimeDiagnosis:
        """Classify a naive local time without resolving it."""
        return diagnose_local(self._timezone, local)

    def localize(
        self,
        local: datetime,
        fold: FoldPolicy | str = FoldPolicy.EARLIER,
        gap: GapPolicy | str = GapPolicy.REJECT,
    ) -> datetime:
        """Attach this timezone to a naive local time.

        See :meth:`Timezone.localize_naive
        <icalendar.cal.timezone.Timezone.localize_naive>`.
        """
        return localize_naive(self._timezone, local, fold=fold, gap=gap)

    def from_utc(self, utc: datetime) -> datetime:
        """Convert a UTC datetime to local wall-clock time."""
        if utc.tzinfo is None:
            raise ValueError("from_utc() requires a timezone-aware datetime.")
        utc_naive = utc.astimezone(timezone.utc).replace(tzinfo=None)
        # Coverage is specified in local wall time. The local result is at
        # most one day away (max realistic offset), so the padded window
        # of a day back covers the check as well.
        self._check_coverage(utc_naive + timedelta(days=1))
        segments = self._get_segments(utc_naive)
        segment_index = _segment_index_at_utc(segments, utc_naive)
        segment = segments[segment_index]
        local = utc_naive + segment.offset
        is_fold = False
        if _is_fold_instant(segments, segment_index, local):
            return local.replace(tzinfo=self, fold=1)
        return local.replace(tzinfo=self, fold=0)

    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        """Return the UTC offset for ``dt``."""
        if dt is None:
            return None
        if dt.tzinfo is not self:
            naive = dt.astimezone(timezone.utc).replace(tzinfo=None)
            segments = self._get_segments(naive)
            segment = segments[_segment_index_at_utc(segments, naive)]
            return segment.offset
        diagnosis = diagnose_local(self._timezone, dt.replace(tzinfo=None))
        return _offset_for_fold(diagnosis, dt.fold)

    def dst(self, dt: datetime | None) -> timedelta | None:
        """Return the saving time offset for ``dt``."""
        if dt is None:
            return None
        if dt.tzinfo is not self:
            naive = dt.astimezone(timezone.utc).replace(tzinfo=None)
            segments = self._get_segments(naive)
            segment = segments[_segment_index_at_utc(segments, naive)]
            return segment.dst_offset
        diagnosis = diagnose_local(self._timezone, dt.replace(tzinfo=None))
        candidate = _candidate_for_fold(diagnosis, dt.fold)
        return candidate.dst_offset

    def tzname(self, dt: datetime | None) -> str | None:
        """Return the observance name for ``dt``."""
        if dt is None:
            return None
        if dt.tzinfo is not self:
            naive = dt.astimezone(timezone.utc).replace(tzinfo=None)
            segments = self._get_segments(naive)
            segment = segments[_segment_index_at_utc(segments, naive)]
            return segment.tzname
        diagnosis = diagnose_local(self._timezone, dt.replace(tzinfo=None))
        return _candidate_for_fold(diagnosis, dt.fold).tzname

    def fromutc(self, dt: datetime) -> datetime:
        """Convert an aware datetime (as read in UTC) to local time.

        Called by :meth:`datetime.datetime.astimezone`; the input's
        ``fold`` selects which representation of an ambiguous instant
        is returned, as specified by :pep:`495`.
        """
        if dt.tzinfo is None:
            raise ValueError("fromutc() requires a timezone-aware datetime.")
        # Per the tzinfo contract, dt is already a UTC wall-clock time
        # tagged with this tzinfo; do not convert it with astimezone()
        # (which would call back into this method).
        utc_naive = dt.replace(tzinfo=None)
        self._check_coverage(utc_naive + timedelta(days=1))
        segments = self._get_segments(utc_naive)
        segment_index = _segment_index_at_utc(segments, utc_naive)
        segment = segments[segment_index]
        local = utc_naive + segment.offset
        is_fold = _is_fold_instant(segments, segment_index, local)
        # zoneinfo always returns the canonical (post-transition)
        # representation from fromutc(); the input fold only matters for
        # utcoffset()/dst()/tzname() calls made by datetime arithmetic.
        return local.replace(tzinfo=self, fold=1 if is_fold else 0)


def _offset_for_fold(
    diagnosis: LocalTimeDiagnosis, fold: int
) -> timedelta:
    """Pick the offset for a PEP 495 ``fold`` value."""
    return _candidate_for_fold(diagnosis, fold).offset


def _candidate_for_fold(
    diagnosis: LocalTimeDiagnosis, fold: int
) -> LocalTimeCandidate:
    """Pick the candidate for a PEP 495 ``fold`` value.

    For folds, ``fold=0`` is the earlier occurrence and ``fold=1`` the
    later one. For gaps, the two sides represent the imagined positions
    of the missing time.
    """
    if diagnosis.candidates:
        if diagnosis.kind is LocalTimeKind.UNAMBIGUOUS or fold == 0:
            return diagnosis.candidates[0]
        return diagnosis.candidates[1]
    # Gap: a self-aware datetime in the skipped range is imaginary, but
    # PEP 495 still asks for the surrounding offsets (fold 0 -> before,
    # fold 1 -> after). They are reconstructed from the transitions.
    transition = (
        diagnosis.previous_transition
        if fold == 0
        else diagnosis.next_transition
    )
    if transition is None:
        raise InsufficientTimezoneDataError(
            "Not enough VTIMEZONE data to determine either offset."
        )
    offset = (
        transition.offset_from if fold == 0 else transition.offset_to
    )
    return LocalTimeCandidate(
        utc=(diagnosis.local - offset).replace(tzinfo=timezone.utc),
        offset=offset,
        dst_offset=timedelta(0),
        is_dst=fold == 1,
        tzname=transition.tzname,
    )


def _segment_index_at_utc(
    segments: tuple[_Segment, ...], utc_naive: datetime
) -> int:
    """Return the index of the segment containing ``utc_naive``."""
    for index, segment in enumerate(segments):
        if segment.start <= utc_naive and (
            segment.end is None or utc_naive < segment.end
        ):
            return index
    raise InsufficientTimezoneDataError(
        "The VTIMEZONE does not cover this point in time. "
        "Add sub-components that cover the requested window."
    )


def _is_fold_instant(
    segments: tuple[_Segment, ...], segment_index: int, local: datetime
) -> bool:
    """Whether ``local`` (in segment ``segment_index``) repeats.

    After a backward transition at UTC T from offset A (previous segment)
    to B (current segment, B < A), the new segment's wall-clock range
    [T+B, T+A) is also covered by the old offset.
    """
    if segment_index == 0:
        return False
    previous = segments[segment_index - 1]
    current = segments[segment_index]
    fold_begin = current.start + current.offset
    fold_end = current.start + previous.offset
    return fold_begin <= local < fold_end


def _build_segments_with_neighbors(
    window: tuple[Transition, ...],
    neighbors: tuple[Transition, ...],
    window_start_local: datetime,
) -> tuple[_Segment, ...]:
    """Build segments from in-window transitions plus their neighbours.

    Neighbours pin down the offset on both sides of the window so that
    queries that fall in a year with no transition inside still work.
    """
    pinned = tuple(
        sorted(tuple(neighbors) + tuple(window), key=lambda item: item.utc)
    )
    if not pinned:
        return ()
    first = pinned[0]
    first_utc = first.utc.replace(tzinfo=None)
    segments: list[_Segment] = []
    for index, transition in enumerate(pinned):
        start_utc = transition.utc.replace(tzinfo=None)
        if index == 0:
            padding_start = window_start_local - transition.offset_from
            if padding_start < start_utc:
                segments.append(
                    _Segment(
                        start=padding_start,
                        end=start_utc,
                        offset=transition.offset_from,
                        dst_offset=timedelta(0),
                        is_dst=transition.is_dst,
                        tzname=transition.tzname,
                        transition=None,
                    )
                )
        end = (
            pinned[index + 1].utc.replace(tzinfo=None)
            if index + 1 < len(pinned)
            else None
        )
        segments.append(
            _Segment(
                start=start_utc,
                end=end,
                offset=transition.offset_to,
                dst_offset=transition.dst_offset,
                is_dst=transition.is_dst,
                tzname=transition.tzname,
                transition=transition,
            )
        )
    return tuple(segments)


def _candidate_from_segment(
    segment: _Segment, local: datetime
) -> LocalTimeCandidate:
    """Create the candidate represented by a segment at wall time."""
    utc = (local - segment.offset).replace(tzinfo=timezone.utc)
    return LocalTimeCandidate(
        utc=utc,
        offset=segment.offset,
        dst_offset=segment.dst_offset,
        is_dst=segment.is_dst,
        tzname=segment.tzname,
    )


def _match_segments(
    segments: tuple[_Segment, ...], local: datetime
) -> list[tuple[_Segment, LocalTimeCandidate]]:
    """Return all segments whose wall-clock range contains ``local``.

    Each segment maps its UTC interval with its own constant offset, so
    its wall-clock range is simply ``[start+X, end+X)``. A backward
    transition makes two adjacent ranges overlap (two matches = fold),
    a forward transition leaves a range covered by none (no matches =
    gap).
    """
    matches: list[tuple[_Segment, LocalTimeCandidate]] = []
    for index, segment in enumerate(segments):
        begin, end = _segment_wall_range(segments, index)
        if not begin <= local < end:
            continue
        matches.append((segment, _candidate_from_segment(segment, local)))
    return matches


def _segment_wall_range(
    segments: tuple[_Segment, ...], index: int
) -> tuple[datetime, datetime]:
    """Return the half-open wall-clock interval of one segment.

    For a transition at UTC T from offset A to B, the old segment maps
    UTC ``[.., T)`` to wall times ending at ``T+A`` (excluded) and the
    new segment maps ``[T, ..)`` starting at wall time ``T+B``
    (included). With B < A the two ranges overlap over ``[T+B, T+A)``
    (fold, including the first post-transition wall time); with B > A
    they leave the hole ``[T+A, T+B)`` (gap).
    """
    segment = segments[index]
    begin = segment.start + segment.offset
    end = (
        datetime.max
        if segment.end is None
        else segment.end + segment.offset
    )
    return begin, end


def diagnose_local(
    timezone_component: Timezone, local: datetime
) -> LocalTimeDiagnosis:
    """Classify a naive local time against a VTIMEZONE component.

    The result contains zero, one or two candidates and, for folds and
    gaps, the transitions bounding the unusual interval.
    """
    if local.tzinfo is not None:
        raise ValueError("diagnose_local() expects a naive datetime.")
    coverage_start = timezone_component.coverage_start
    if coverage_start is not None and local < coverage_start:
        raise InsufficientTimezoneDataError(
            (
                f"The VTIMEZONE {timezone_component.tz_name!r} only covers "
                f"local times from {coverage_start.isoformat()} onward; "
                f"{local.isoformat()} is earlier than any DTSTART/RDATE. "
                "Add sub-components that cover the requested window."
            ),
        )
    window, neighbors = timezone_component._get_transitions_with_neighbors(
        local - _WINDOW_PADDING,
        local + _WINDOW_PADDING,
        include_neighbors=True,
    )
    segments = _build_segments_with_neighbors(
        window, neighbors, local - _WINDOW_PADDING
    )
    if not segments:
        raise InsufficientTimezoneDataError(
            _insufficient_message(timezone_component, local, None)
        )

    matches = _match_segments(segments, local)

    if len(matches) >= 2:
        ordered = sorted(matches, key=lambda item: item[1].utc)
        diagnosis = LocalTimeDiagnosis(
            local=local,
            kind=LocalTimeKind.FOLD,
            candidates=tuple(candidate for _, candidate in ordered[:2]),
            earlier=ordered[0][1],
            later=ordered[1][1],
            previous_transition=ordered[1][0].transition,
            next_transition=ordered[1][0].transition,
        )
    elif len(matches) == 1:
        diagnosis = LocalTimeDiagnosis(
            local=local,
            kind=LocalTimeKind.UNAMBIGUOUS,
            candidates=(matches[0][1],),
        )
    else:
        diagnosis = _gap_diagnosis(timezone_component, local, segments)
    return diagnosis


def _gap_diagnosis(
    timezone_component: Timezone,
    local: datetime,
    segments: tuple[_Segment, ...],
) -> LocalTimeDiagnosis:
    """Build the diagnosis for a missing wall-clock time."""
    # A forward transition at UTC T from offset A to B > A skips wall
    # times [T+A, T+B). The old segment ends at T and the new segment
    # starts at T, so scan adjacent segment pairs for that hole.
    for index, previous in enumerate(segments):
        if index + 1 >= len(segments) or previous.end is None:
            continue
        following = segments[index + 1]
        if previous.end != following.start:
            continue
        transition_utc = following.start
        gap_start = transition_utc + previous.offset
        gap_end = transition_utc + following.offset
        if gap_start >= gap_end:
            continue  # fold, not gap
        if gap_start <= local < gap_end:
            return LocalTimeDiagnosis(
                local=local,
                kind=LocalTimeKind.GAP,
                candidates=(),
                gap=gap_end - gap_start,
                shift_forward=local + (gap_end - gap_start),
                previous_transition=following.transition,
                next_transition=following.transition,
            )
    return LocalTimeDiagnosis(
        local=local,
        kind=LocalTimeKind.GAP,
        candidates=(),
    )


def localize_naive(
    timezone_component: Timezone,
    local: datetime,
    fold: FoldPolicy | str = FoldPolicy.EARLIER,
    gap: GapPolicy | str = GapPolicy.REJECT,
) -> datetime:
    """Localize a naive datetime using explicit fold/gap policies.

    :param local: naive local time
    :param fold: policy for repeated local times
    :param gap: policy for missing local times
    :raises AmbiguousTimeError: with ``fold="reject"`` on a fold
    :raises NonExistentTimeError: with ``gap="reject"`` on a gap
    :raises InsufficientTimezoneDataError: if the components do not
        cover ``local``
    """
    if local.tzinfo is not None:
        raise ValueError("localize_naive() expects a naive datetime.")
    fold_policy = FoldPolicy(fold)
    gap_policy = GapPolicy(gap)
    diagnosis = diagnose_local(timezone_component, local)
    tzinfo_object = timezone_component.to_tzinfo()
    if diagnosis.kind is LocalTimeKind.UNAMBIGUOUS:
        return local.replace(tzinfo=tzinfo_object)
    if diagnosis.kind is LocalTimeKind.FOLD:
        if fold_policy is FoldPolicy.REJECT:
            raise AmbiguousTimeError(
                f"{local.isoformat()} is ambiguous in "
                f"{timezone_component.tz_name!r}: offsets "
                f"{diagnosis.earlier.offset} and {diagnosis.later.offset} "
                "both apply. Choose fold='earlier' or fold='later'.",
                diagnosis,
            )
        return local.replace(
            tzinfo=tzinfo_object,
            fold=0 if fold_policy is FoldPolicy.EARLIER else 1,
        )
    if gap_policy is GapPolicy.REJECT:
        raise NonExistentTimeError(
            _gap_message(timezone_component, diagnosis),
            diagnosis,
        )
    shifted = diagnosis.shift_forward
    if shifted is None:
        raise InsufficientTimezoneDataError(
            _insufficient_message(timezone_component, local, diagnosis),
            diagnosis,
        )
    shifted_diagnosis = diagnose_local(timezone_component, shifted)
    if shifted_diagnosis.kind is LocalTimeKind.GAP or not shifted_diagnosis.candidates:
        raise InsufficientTimezoneDataError(
            _insufficient_message(timezone_component, local, diagnosis),
            diagnosis,
        )
    return shifted.replace(
        tzinfo=tzinfo_object,
        fold=1 if shifted_diagnosis.is_ambiguous else 0,
    )


def _gap_message(
    timezone_component: Timezone, diagnosis: LocalTimeDiagnosis
) -> str:
    """Format the message for a rejected gap."""
    if diagnosis.gap is None or diagnosis.shift_forward is None:
        return (
            f"{diagnosis.local.isoformat()} does not exist in "
            f"{timezone_component.tz_name!r}."
        )
    return (
        f"{diagnosis.local.isoformat()} does not exist in "
        f"{timezone_component.tz_name!r}; clocks skip forward by "
        f"{diagnosis.gap}. Use gap='shift-forward' to map it to "
        f"{diagnosis.shift_forward.isoformat()}."
    )


def _insufficient_message(
    timezone_component: Timezone,
    local: datetime,
    diagnosis: LocalTimeDiagnosis,
) -> str:
    """Format the message for missing VTIMEZONE coverage."""
    start = timezone_component.coverage_start
    if start is not None:
        return (
            f"The VTIMEZONE {timezone_component.tz_name!r} only covers "
            f"local times from {start.isoformat()} onward; "
            f"{local.isoformat()} is not covered. Add STANDARD/DAYLIGHT "
            "components for the requested window instead of assuming UTC."
        )
    return (
        f"The VTIMEZONE {timezone_component.tz_name!r} contains no "
        "STANDARD/DAYLIGHT components covering the requested window."
    )


__all__ = [
    "FoldPolicy",
    "GapPolicy",
    "LocalTimeCandidate",
    "LocalTimeDiagnosis",
    "LocalTimeKind",
    "Transition",
    "VTIMEZONEInfo",
    "diagnose_local",
    "localize_naive",
]
