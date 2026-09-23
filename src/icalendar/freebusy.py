"""Synthesize a free/busy timeline from ``VFREEBUSY`` components.

This module combines the ``FREEBUSY`` periods of several components into
one non-overlapping timeline for a UTC query window. Every segment of the
timeline carries the effective ``FBTYPE`` and the set of sources that
contribute to it, so a scheduling service can explain why a time is busy.

The synthesis only reads the ``FREEBUSY`` properties already present on
the given components. It does not expand ``VEVENT`` components and it
does not send calendar queries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import pairwise
from typing import TYPE_CHECKING

from icalendar.cal.free_busy import FreeBusy
from icalendar.enums import FBTYPE
from icalendar.error import InvalidCalendar
from icalendar.prop import vPeriod

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

FBTYPE_UNKNOWN = "UNKNOWN"
"""The effective type of a segment that no input period covers.

Uncovered time is reported as unknown rather than free: the inputs only
describe what they know, so the absence of a period is not a claim that
the time is free.
"""

DEFAULT_FBTYPE_PRIORITY: tuple[str, ...] = (
    FBTYPE.BUSY_UNAVAILABLE,
    FBTYPE.BUSY,
    FBTYPE.BUSY_TENTATIVE,
    FBTYPE.FREE,
)
"""The default order in which overlapping ``FBTYPE`` values win."""

_KNOWN_FBTYPES = frozenset(member.value for member in FBTYPE)


def _as_utc(dt: datetime) -> datetime:
    """Return the datetime in UTC, reading a naive one as UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass(frozen=True)
class FreeBusySource:
    """One component's contribution to a timeline segment.

    Parameters:
        uid: The ``UID`` of the contributing component.
        fbtype: The ``FBTYPE`` of the contributing ``FREEBUSY`` period as
            written on the component. Unrecognized values are kept as-is
            here even though they rank as ``BUSY``.
    """

    uid: str
    fbtype: str


@dataclass(frozen=True)
class FreeBusySegment:
    """A non-overlapping slice of the synthesized timeline.

    Parameters:
        start: The inclusive start of the segment, in UTC.
        end: The non-inclusive end of the segment, in UTC.
        fbtype: The effective ``FBTYPE`` of the segment, chosen with the
            caller's priority, or :data:`FBTYPE_UNKNOWN` when no input
            period covers the segment.
        sources: Every source whose period covers the segment, including
            the ones that lost the priority contest.
    """

    start: datetime
    end: datetime
    fbtype: str
    sources: frozenset[FreeBusySource] = field(default_factory=frozenset)

    @property
    def is_unknown(self) -> bool:
        """Whether no input period covers this segment."""
        return self.fbtype == FBTYPE_UNKNOWN

    @property
    def sorted_sources(self) -> list[FreeBusySource]:
        """The sources in a stable order, for display."""
        return sorted(self.sources, key=lambda source: (source.uid, source.fbtype))


@dataclass(frozen=True)
class FreeBusyReport:
    """The synthesized timeline and its source detail.

    Parameters:
        start: The inclusive start of the query window, in UTC.
        end: The non-inclusive end of the query window, in UTC.
        segments: The non-overlapping, time-ordered segments covering
            exactly ``[start, end)``.
    """

    start: datetime
    end: datetime
    segments: tuple[FreeBusySegment, ...]

    def to_freebusy(self, uid: str | None = None) -> FreeBusy:
        """Render the known segments as a standard-conformant ``VFREEBUSY``.

        Each known segment becomes one ``FREEBUSY`` property with an
        ``FBTYPE`` parameter. Unknown segments are omitted, since a
        ``VFREEBUSY`` cannot claim them, and the source detail stays in
        this report instead of being stuffed into the ``PERIOD`` text.

        Parameters:
            uid: The ``UID`` of the new component, or ``None`` for a
                fresh random one.

        Returns:
            :class:`~icalendar.cal.free_busy.FreeBusy`
        """
        free_busy = FreeBusy.new(start=self.start, end=self.end, uid=uid)
        for segment in self.segments:
            if segment.is_unknown:
                continue
            free_busy.add(
                "FREEBUSY",
                (segment.start, segment.end),
                parameters={"FBTYPE": segment.fbtype},
            )
        return free_busy


def _iter_periods(component: FreeBusy) -> Iterable[vPeriod]:
    """Yield the FREEBUSY periods of a component."""
    value = component.get("FREEBUSY")
    if value is None:
        return
    periods = value if isinstance(value, list) else [value]
    for value_or_period in periods:
        period = value_or_period
        if not isinstance(period, vPeriod):
            try:
                period = vPeriod(period)
            except (TypeError, ValueError) as error:
                raise InvalidCalendar(
                    f"Invalid FREEBUSY period: {value_or_period!r}"
                ) from error
        yield period


def _period_fbtype(period: vPeriod) -> str:
    """The FBTYPE of a period, defaulting to BUSY as :rfc:`5545` requires."""
    return str(period.params.get("FBTYPE", FBTYPE.BUSY)).upper()


def synthesize_freebusy(
    components: Iterable[FreeBusy],
    start: datetime,
    end: datetime,
    fbtype_priority: Sequence[str] = DEFAULT_FBTYPE_PRIORITY,
) -> FreeBusyReport:
    """Synthesize a non-overlapping free/busy timeline for a UTC window.

    The ``FREEBUSY`` periods of all components are clipped to the window,
    the window is split at every period endpoint, and each slice gets the
    effective ``FBTYPE`` that wins the caller's priority among the
    periods covering it. All covering sources are kept on the segment.
    Adjacent slices merge only when both the effective type and the
    source set are identical. Periods that touch at an endpoint do not
    overlap. Slices no input covers are marked :data:`FBTYPE_UNKNOWN`,
    never silently free. The result does not depend on the order of the
    components.

    Parameters:
        components: The ``VFREEBUSY`` components to combine. Both PERIOD
            forms, start/end and start/duration, are understood.
        start: The inclusive start of the query window. A naive datetime
            is read as UTC.
        end: The non-inclusive end of the query window. A naive datetime
            is read as UTC.
        fbtype_priority: The ``FBTYPE`` values from strongest to weakest.
            The first value present on a covering period wins the slice.
            Unrecognized values rank as ``BUSY``, as :rfc:`5545`
            requires; values missing from the priority lose to every
            listed value, with ties broken by name so the result stays
            deterministic.

    Returns:
        :class:`FreeBusyReport`

    Raises:
        ~icalendar.error.InvalidCalendar: If the window ends before it
            starts, or a ``FREEBUSY`` period has a negative duration.
            Zero-length periods are ignored.
    """
    window_start = _as_utc(start)
    window_end = _as_utc(end)
    if window_end < window_start:
        raise InvalidCalendar(
            "The query window ends before it starts: "
            f"{window_start!r} to {window_end!r}"
        )
    priority = tuple(str(value).upper() for value in fbtype_priority)

    def rank(fbtype: str) -> tuple[int, str]:
        # Applications MUST treat unrecognized FBTYPE values as BUSY.
        canonical = fbtype if fbtype in _KNOWN_FBTYPES else FBTYPE.BUSY.value
        try:
            return (priority.index(canonical), canonical)
        except ValueError:
            return (len(priority), canonical)

    entries = []
    for index, component in enumerate(components):
        uid = component.get("UID")
        uid = str(uid) if uid is not None else f"component-{index}"
        for period in _iter_periods(component):
            period_start = _as_utc(period.start)
            period_end = _as_utc(period.end)
            if period_end < period_start:
                raise InvalidCalendar(
                    "FREEBUSY period has a negative duration: "
                    f"{period_start!r} to {period_end!r}"
                )
            clipped_start = max(period_start, window_start)
            clipped_end = min(period_end, window_end)
            if clipped_start >= clipped_end:
                # Zero-length periods and periods outside the window
                # carry no information.
                continue
            entries.append((clipped_start, clipped_end, _period_fbtype(period), uid))

    points = sorted(
        {window_start, window_end}
        | {entry[0] for entry in entries}
        | {entry[1] for entry in entries}
    )

    segments = []
    for segment_start, segment_end in pairwise(points):
        if segment_start >= segment_end:
            continue
        covering = [
            entry
            for entry in entries
            if entry[0] <= segment_start and entry[1] >= segment_end
        ]
        if not covering:
            fbtype = FBTYPE_UNKNOWN
            sources = frozenset()
        else:
            fbtype = min(rank(entry[2]) for entry in covering)[1]
            sources = frozenset(
                FreeBusySource(uid=entry[3], fbtype=entry[2]) for entry in covering
            )
        if (
            segments
            and segments[-1].fbtype == fbtype
            and segments[-1].sources == sources
        ):
            segments[-1] = FreeBusySegment(
                start=segments[-1].start,
                end=segment_end,
                fbtype=fbtype,
                sources=sources,
            )
        else:
            segments.append(
                FreeBusySegment(
                    start=segment_start,
                    end=segment_end,
                    fbtype=fbtype,
                    sources=sources,
                )
            )

    return FreeBusyReport(
        start=window_start,
        end=window_end,
        segments=tuple(segments),
    )


__all__ = [
    "DEFAULT_FBTYPE_PRIORITY",
    "FBTYPE_UNKNOWN",
    "FreeBusyReport",
    "FreeBusySegment",
    "FreeBusySource",
    "synthesize_freebusy",
]
