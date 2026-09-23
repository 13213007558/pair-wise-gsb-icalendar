"""Synthesize the ``FREEBUSY`` periods of several ``VFREEBUSY`` components.

This is the read side of a free/busy reply. It clips every ``PERIOD`` to a
UTC query window, splits the window at every period endpoint, chooses the
effective :class:`~icalendar.enums.FBTYPE` of each slice with a caller given
priority, and merges adjacent slices only when the winning type and the set
of contributing sources are identical. The result is a non-overlapping
timeline that explains, for every point in the window, which input
components claim that point and which ``FBTYPE`` wins.

This module reads ``VFREEBUSY`` components only. It neither expands
``VEVENT`` components nor sends calendar queries. Time that no input period
covers is marked :data:`unknown`; it is never claimed as ``FREE``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import pairwise
from typing import TYPE_CHECKING

from icalendar.cal import Calendar, FreeBusy
from icalendar.enums import FBTYPE
from icalendar.error import InvalidCalendar

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import date as date_type

    from icalendar.cal import Component

    #: An :class:`~icalendar.enums.FBTYPE` or an unknown ``FBTYPE`` token.
    FreeBusyType = FBTYPE | str


class _UnknownFreeBusyType:
    """Marker for a slice that no ``FREEBUSY`` period covers.

    This is intentionally not an :class:`~icalendar.enums.FBTYPE` value:
    :rfc:`5545` defines no ``FBTYPE`` for "not covered by any input", and
    such a slice must not be published as ``FREE``.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "unknown"

    def __str__(self) -> str:
        return "UNKNOWN"


#: The :attr:`FreeBusySegment.fbtype` of an uncovered slice of the window.
unknown = _UnknownFreeBusyType()


@dataclass(frozen=True)
class FreeBusySource:
    """One ``FREEBUSY`` property contributing to a timeline segment.

    Attributes:
        component: The :class:`~icalendar.cal.free_busy.FreeBusy` component
            that holds the property.
        uid: Its ``UID``, or ``None`` if it has none.
        fbtype: The ``FBTYPE`` parameter, as an :class:`~icalendar.enums.FBTYPE`
            when known and as the raw x-name or iana-token string otherwise.
        start: The UTC start of the period after clipping to the window.
        end: The exclusive UTC end of the period after clipping.
        property_index: The zero based position of the property inside its
            component, so adjacent properties of one component stay distinct.
        by_duration: Whether the period was written with a duration rather
            than with an end.
    """

    component: FreeBusy
    uid: str | None
    fbtype: FreeBusyType
    start: datetime
    end: datetime
    property_index: int
    by_duration: bool

    @property
    def duration(self) -> timedelta:
        """The clipped duration of the period."""
        return self.end - self.start


@dataclass(frozen=True)
class FreeBusySegment:
    """A non-overlapping, half-open slice of the queried time window.

    Attributes:
        start: The inclusive UTC start of the slice.
        end: The exclusive UTC end of the slice.
        fbtype: The effective ``FBTYPE`` of the slice, or :data:`unknown`
            when no input period covers it.
        sources: Every period covering the whole slice, in a deterministic
            order. A scheduling service can use these to explain why the
            slice was judged busy, including periods that lost the priority
            decision.
    """

    start: datetime
    end: datetime
    fbtype: FreeBusyType | _UnknownFreeBusyType
    sources: tuple[FreeBusySource, ...]

    @property
    def duration(self) -> timedelta:
        """The length of the slice."""
        return self.end - self.start

    @property
    def is_unknown(self) -> bool:
        """Whether no input period covers the slice."""
        return self.fbtype is unknown


class FreeBusyTimeline:
    """The synthesized, non-overlapping answer to a free/busy query.

    Instances are returned by :func:`compose_free_busy`. Iterate them to
    receive :class:`FreeBusySegment` instances. The segments cover the
    whole query window, in order, without gaps or overlaps.

    Attributes:
        start: The inclusive UTC start of the query window.
        end: The exclusive UTC end of the query window.
        segments: The timeline segments.
    """

    def __init__(
        self,
        start: datetime,
        end: datetime,
        segments: Sequence[FreeBusySegment],
    ) -> None:
        self.start = start
        self.end = end
        self.segments: tuple[FreeBusySegment, ...] = tuple(segments)

    def __iter__(self):
        return iter(self.segments)

    def __len__(self) -> int:
        return len(self.segments)

    def __getitem__(self, index: int) -> FreeBusySegment:
        return self.segments[index]

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, FreeBusyTimeline):
            return NotImplemented
        return (
            self.start == other.start
            and self.end == other.end
            and self.segments == other.segments
        )

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return f"<FreeBusyTimeline {self.start} to {self.end}, {len(self)} segments>"

    def to_report(self) -> list[dict[str, object]]:
        """Return the source detail as plain data structures.

        This separate report is the place for provenance information. It is
        never encoded into the ``PERIOD`` text of a ``FREEBUSY``
        property. The ``fbtype`` of an uncovered segment is the string
        ``"UNKNOWN"``. Each source lists the ``UID`` of its component,
        the ``FBTYPE`` of its period, and the period as clipped to the
        query window.

        Returns:
            One dictionary per segment, in timeline order.
        """
        return [
            {
                "start": segment.start,
                "end": segment.end,
                "fbtype": str(segment.fbtype),
                "sources": [
                    {
                        "uid": source.uid,
                        "fbtype": str(source.fbtype),
                        "start": source.start,
                        "end": source.end,
                        "property_index": source.property_index,
                    }
                    for source in segment.sources
                ],
            }
            for segment in self.segments
        ]

    def to_free_busy(
        self,
        *,
        include_free: bool = False,
        stamp: datetime | None = None,
    ) -> FreeBusy:
        """Write the covered segments back as a ``VFREEBUSY`` component.

        Each covered segment becomes one standard ``FREEBUSY`` property
        with its ``FBTYPE`` parameter and a start/end ``PERIOD``.
        Uncovered (:data:`unknown`) segments are omitted: a free/busy reply
        lists busy time and must not claim that missing information means
        free time. Provenance stays in :meth:`to_report` and is never added
        to the period text.

        Parameters:
            include_free: Also publish ``FBTYPE=FREE`` segments. By
                default they are omitted, as the component is a list of busy
                time.
            stamp: The ``DTSTAMP`` of the component. Defaults to the
                current time.

        Returns:
            A :class:`~icalendar.cal.free_busy.FreeBusy` with ``DTSTART``
            and ``DTEND`` set to the query window.
        """
        from icalendar.prop import vPeriod

        component = FreeBusy.new(start=self.start, end=self.end, stamp=stamp)
        for segment in self.segments:
            if segment.is_unknown:
                continue
            if not include_free and segment.fbtype == FBTYPE.FREE:
                continue
            period = vPeriod(
                (segment.start, segment.end),
                params={"FBTYPE": str(segment.fbtype)},
            )
            component.add("FREEBUSY", period)
        return component


#: Default priority: stronger busy states win, tentative loses to confirmed
#: busy, and free loses to every busy state. Unknown ``FBTYPE`` tokens are
#: treated like ``BUSY``, per :rfc:`5545#section-3.2.9`.
DEFAULT_FBTYPE_PRIORITY: tuple[FBTYPE, ...] = (
    FBTYPE.BUSY_UNAVAILABLE,
    FBTYPE.BUSY,
    FBTYPE.BUSY_TENTATIVE,
    FBTYPE.FREE,
)


def _as_utc(value: date_type | datetime, what: str) -> datetime:
    """Return a datetime in UTC. Naive datetimes are assumed to be UTC."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    raise InvalidCalendar(f"{what} must be a UTC datetime, got date {value!r}")


def _free_busy_components(components: Iterable[Component]):
    """Yield VFREEBUSY components, descending into VCALENDAR containers."""
    seen: set[int] = set()
    for component in components:
        if isinstance(component, FreeBusy):
            targets = [component]
        elif isinstance(component, Calendar):
            targets = [
                subcomponent
                for subcomponent in component.walk()
                if isinstance(subcomponent, FreeBusy)
            ]
        else:
            # VEVENT and every other component type are ignored on purpose:
            # this entry point does not expand events.
            continue
        for target in targets:
            if id(target) not in seen:
                seen.add(id(target))
                yield target


def _periods_of(component: FreeBusy) -> list:
    """Return the FREEBUSY properties of a component as a list."""
    value = component.get("FREEBUSY")
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _fbtype_of(period) -> FreeBusyType:
    """Return the FBTYPE parameter, defaulting to BUSY per RFC 5545."""
    fbtype = getattr(period, "FBTYPE", None)
    return fbtype if fbtype is not None else FBTYPE.BUSY


def _priority_rank(
    fbtype: FreeBusyType,
    ranks: dict[str, int],
    busy_rank: int,
) -> int:
    """Rank of an FBTYPE; unknown tokens share the rank of BUSY."""
    return ranks.get(str(fbtype), busy_rank)


def _tie_key(fbtype: FreeBusyType) -> tuple[int, str]:
    """Break priority ties deterministically and independently of input order.

    A known ``BUSY`` wins over unknown tokens that share its rank (RFC 5545
    tells applications to treat unknown tokens *like* BUSY, not to prefer
    them), and remaining ties go to the lexicographically smaller token.
    """
    return (0 if fbtype == FBTYPE.BUSY else 1, str(fbtype))


def _source_identity(source: FreeBusySource) -> tuple[object, str]:
    """Identity used to compare the source sets of adjacent segments.

    A source is the contributing component and the type with which its
    period covers the segment. Two adjacent periods of the same component
    and FBTYPE describe one continuous contribution; a different component
    or a different FBTYPE does not.
    """
    identity: object = source.uid if source.uid is not None else id(source.component)
    return identity, str(source.fbtype)


def _source_sort_key(source: FreeBusySource) -> tuple:
    """Order sources independently of the caller's input order."""
    return (
        source.uid is None,
        str(source.uid) if source.uid is not None else "",
        str(source.fbtype),
        source.start,
        source.end,
        source.property_index,
        source.component.to_ical(),
    )


def _collect_coverage(
    components: Iterable[Component],
    window_start: datetime,
    window_end: datetime,
) -> tuple[list[FreeBusySource], set[datetime]]:
    """Clip every period to the window and collect it with its endpoints.

    Zero-length periods, including periods clipped to a point, are ignored.
    """
    coverage: list[FreeBusySource] = []
    endpoints: set[datetime] = {window_start, window_end}
    for component in _free_busy_components(components):
        uid_value = component.get("UID")
        uid = None if uid_value is None else str(uid_value)
        for index, period in enumerate(_periods_of(component)):
            start = getattr(period, "start", None)
            end = getattr(period, "end", None)
            if not isinstance(start, datetime) or not isinstance(end, datetime):
                raise InvalidCalendar(
                    "A FREEBUSY property must hold a PERIOD with a start and "
                    f"an end, got {period!r}"
                )
            start = _as_utc(start, "A FREEBUSY period start")
            end = _as_utc(end, "A FREEBUSY period end")
            if start > end:
                raise InvalidCalendar(
                    f"A FREEBUSY period must not be negative: {start} to {end}"
                )
            clipped_start = max(start, window_start)
            clipped_end = min(end, window_end)
            if clipped_start >= clipped_end:
                # Zero-length periods and periods outside the window are ignored.
                continue
            coverage.append(
                FreeBusySource(
                    component=component,
                    uid=uid,
                    fbtype=_fbtype_of(period),
                    start=clipped_start,
                    end=clipped_end,
                    property_index=index,
                    by_duration=bool(getattr(period, "by_duration", False)),
                )
            )
            endpoints.add(clipped_start)
            endpoints.add(clipped_end)
    return coverage, endpoints


def _segment_source_key(segment: FreeBusySegment) -> frozenset[tuple[object, str]]:
    """Identity of the contributing source set of a segment."""
    return frozenset(_source_identity(source) for source in segment.sources)


def _merge_sources(
    first: tuple[FreeBusySource, ...],
    second: tuple[FreeBusySource, ...],
) -> tuple[FreeBusySource, ...]:
    """Union the sources of two merged segments, without duplicates."""
    merged: list[FreeBusySource] = []
    identities: set[tuple[int, int]] = set()
    for source in (*first, *second):
        identity = (id(source.component), source.property_index)
        if identity not in identities:
            identities.add(identity)
            merged.append(source)
    return tuple(sorted(merged, key=_source_sort_key))


def compose_free_busy(
    components: Iterable[Component],
    window_start: datetime,
    window_end: datetime,
    fbtype_priority: Sequence[FreeBusyType] = DEFAULT_FBTYPE_PRIORITY,
) -> FreeBusyTimeline:
    """Synthesize ``FREEBUSY`` periods into one non-overlapping timeline.

    Periods are clipped to the half-open UTC query window
    ``[window_start, window_end)`, then the window is split at every
    period endpoint. For each atomic slice, all periods covering it are
    kept as sources, and the winner is chosen with ``fbtype_priority``:
    the period whose ``FBTYPE`` appears first in that sequence wins.
    Adjacent slices are merged only when both their winning ``FBTYPE``
    and their complete source sets are identical.

    Endpoints that merely touch do not overlap: a period ending at ``t``
    does not cover a slice starting at ``t``. Zero-length periods are
    ignored. A period with a negative duration, or a period ending before
    it starts, raises :exc:`~icalendar.error.InvalidCalendar`.

    Slices not covered by any period get :data:`unknown` as
    ``fbtype``; they are never marked ``FREE``. The output does not
    depend on the order of ``components`` or of the ``FREEBUSY``
    properties inside them.

    Parameters:
        components: Components to read. ``VFREEBUSY`` components are
            used directly, ``VCALENDAR`` components are searched for
            ``VFREEBUSY`` subcomponents, and everything else (including
            ``VEVENT``) is ignored. No calendar query is sent.
        window_start: Inclusive start of the query window. Aware datetimes
            are converted to UTC; naive datetimes are assumed to be UTC.
        window_end: Exclusive end of the query window. It must come after
            ``window_start``.
        fbtype_priority: FBTYPE values ordered from highest to lowest
            priority. The default ranks busy states above ``FREE``.
            Unknown x-name or iana-token values are treated like
            ``BUSY``, as required by :rfc:`5545#section-3.2.9`; ties
            prefer a known ``BUSY`` and then the lexicographically
            smaller token.

    Returns:
        A :class:`FreeBusyTimeline` covering the whole query window.

    Raises:
        ~icalendar.error.InvalidCalendar: If the window is empty or
            reversed, if a period has a negative duration, or if a
            ``FREEBUSY`` property does not hold a valid period.

    Example:
        Three components overlap: a confirmed busy period, a tentative one
        written with a duration, an unknown ``FBTYPE``, and two adjacent
        free periods of one component. The window is split at every
        endpoint, then merged again where the winner and the source set
        agree. The confirmed ``BUSY`` period hides the tentative and the
        unknown one, which stay visible as sources.

        >>> from datetime import datetime, timedelta, timezone
        >>> from icalendar import FreeBusy
        >>> from icalendar.free_busy_synthesis import compose_free_busy
        >>> from icalendar.prop import vPeriod
        >>> def dt(hour):
        ...     return datetime(2026, 5, 1, hour, tzinfo=timezone.utc)
        >>> def component(uid, *periods):
        ...     new = FreeBusy.new(uid=uid)
        ...     for fbtype, start, end_or_duration in periods:
        ...         period = vPeriod(
        ...             (start, end_or_duration), params={"FBTYPE": fbtype}
        ...         )
        ...         new.add("FREEBUSY", period)
        ...     return new
        >>> one = component("one@example.com", ("BUSY", dt(10), dt(14)))
        >>> two = component(
        ...     "two@example.com",
        ...     ("BUSY-TENTATIVE", dt(12), timedelta(hours=2)),
        ... )
        >>> three = component(
        ...     "three@example.com",
        ...     ("X-HOLD", dt(11), dt(13)),
        ...     ("FREE", dt(15), dt(16)),
        ...     ("FREE", dt(16), dt(17)),
        ... )
        >>> timeline = compose_free_busy([three, one, two], dt(0), dt(18))
        >>> for segment in timeline:
        ...     uids = sorted({source.uid for source in segment.sources})
        ...     print(segment.start.hour, segment.end.hour,
        ...           str(segment.fbtype), uids)
        0 10 UNKNOWN []
        10 11 BUSY ['one@example.com']
        11 12 BUSY ['one@example.com', 'three@example.com']
        12 13 BUSY ['one@example.com', 'three@example.com', 'two@example.com']
        13 14 BUSY ['one@example.com', 'two@example.com']
        14 15 UNKNOWN []
        15 17 FREE ['three@example.com']
        17 18 UNKNOWN []
    """
    start = _as_utc(window_start, "The query window start")
    end = _as_utc(window_end, "The query window end")
    if start >= end:
        raise InvalidCalendar(
            f"The query window is empty or reversed: {start} to {end}"
        )
    ranks = {str(token): rank for rank, token in enumerate(fbtype_priority)}
    busy_rank = ranks.get(str(FBTYPE.BUSY), len(fbtype_priority))
    coverage, endpoints = _collect_coverage(components, start, end)
    boundaries = sorted(endpoints)
    segments: list[FreeBusySegment] = []
    for left, right in pairwise(boundaries):
        sources = tuple(
            sorted(
                (
                    source
                    for source in coverage
                    if source.start <= left and source.end >= right
                ),
                key=_source_sort_key,
            )
        )
        if sources:
            fbtype = min(
                (source.fbtype for source in sources),
                key=lambda token: (
                    _priority_rank(token, ranks, busy_rank),
                    _tie_key(token),
                ),
            )
            segment = FreeBusySegment(left, right, fbtype, sources)
        else:
            segment = FreeBusySegment(left, right, unknown, ())
        if (
            segments
            and segments[-1].fbtype == segment.fbtype
            and _segment_source_key(segments[-1]) == _segment_source_key(segment)
        ):
            previous = segments.pop()
            segment = FreeBusySegment(
                previous.start,
                segment.end,
                previous.fbtype,
                _merge_sources(previous.sources, segment.sources),
            )
        segments.append(segment)
    return FreeBusyTimeline(start, end, segments)


__all__ = [
    "DEFAULT_FBTYPE_PRIORITY",
    "FreeBusySegment",
    "FreeBusySource",
    "FreeBusyTimeline",
    "compose_free_busy",
    "unknown",
]
