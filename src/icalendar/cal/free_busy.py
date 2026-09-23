""":rfc:`5545` VFREEBUSY component."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone, tzinfo
from itertools import pairwise
from typing import TYPE_CHECKING, NamedTuple, cast

import dateutil.rrule

from icalendar.attr import (
    CONCEPTS_TYPE_SETTER,
    LINKS_TYPE_SETTER,
    RELATED_TO_TYPE_SETTER,
    REQUEST_STATUS_property,
    contacts_property,
    create_single_property,
    organizer_property,
    uid_property,
    url_property,
)
from icalendar.cal.component import Component
from icalendar.cal.examples import get_example
from icalendar.enums import FBTYPE
from icalendar.error import IncompleteComponent, InvalidCalendar
from icalendar.prop import vPeriod
from icalendar.timezone import tzp
from icalendar.tools import to_datetime

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from icalendar.cal.availability import Availability
    from icalendar.cal.available import Available
    from icalendar.compatibility import Self
    from icalendar.prop import vCalAddress


class _TypedInterval(NamedTuple):
    """A half-open UTC interval together with its free/busy type."""

    start: datetime
    end: datetime
    fbtype: FBTYPE


#: Precedence of :class:`~icalendar.enums.FBTYPE` values when intervals of
#: different types overlap. ``BUSY-UNAVAILABLE`` wins over ``BUSY``, ``BUSY``
#: wins over ``BUSY-TENTATIVE``, and any busy type wins over ``FREE``.
_FBTYPE_PRECEDENCE = {
    FBTYPE.FREE: 0,
    FBTYPE.BUSY_TENTATIVE: 1,
    FBTYPE.BUSY: 2,
    FBTYPE.BUSY_UNAVAILABLE: 3,
}

#: A busy period as accepted by :meth:`FreeBusy.from_availability`.
#: The end of a period can be given as an end :class:`~datetime.datetime`
#: or as a :class:`~datetime.timedelta` duration.
BusyPeriod = (
    vPeriod
    | tuple[datetime, datetime | timedelta]
    | tuple[datetime, datetime | timedelta, FBTYPE | str]
)


def _normalize_tz(tz: tzinfo | str | None) -> tzinfo | None:
    """Return the timezone for a timezone or a timezone id."""
    if tz is None or isinstance(tz, tzinfo):
        return tz
    result = tzp.timezone(tz)
    if result is None:
        raise ValueError(f"Unknown timezone: {tz!r}")
    return result


def _localize(dt: date | datetime, tz: tzinfo | None, what: str) -> datetime:
    """Interpret a datetime, giving floating times an explicit timezone.

    Floating times are interpreted in ``tz``, which the caller must provide
    explicitly. The timezone of the process is never used.
    """
    if not isinstance(dt, datetime):
        dt = to_datetime(dt)
    if dt.tzinfo is None:
        if tz is None:
            raise ValueError(
                f"{what} is a floating time. "
                "Pass tz=... to say in which timezone floating times are."
            )
        dt = tzp.localize(dt, tz)
    return dt


def _to_utc(dt: date | datetime, tz: tzinfo | None, what: str) -> datetime:
    """Convert a datetime to an aware UTC datetime."""
    return _localize(dt, tz, what).astimezone(timezone.utc)


def _raw_dt(component, name: str) -> date | datetime | None:
    """The raw datetime of a property, keeping floating times floating.

    The component properties such as :attr:`Available.start` assume UTC for
    floating times. Here, floating times need to be detected instead, so the
    caller can be asked for their timezone.
    """
    value = component.get(name)
    if value is None:
        return None
    return getattr(value, "dt", value)


def _component_end(component, tz: tzinfo | None, what: str) -> datetime | None:
    """The UTC end of a component from its raw DTEND or DURATION."""
    end = _raw_dt(component, "DTEND")
    if end is not None:
        return _to_utc(end, tz, f"The DTEND of {what}")
    duration = component.DURATION
    if duration is None:
        return None
    start = _raw_dt(component, "DTSTART")
    if start is None:
        raise IncompleteComponent(
            f"Cannot compute free/busy time: {what} has a DURATION " "but no DTSTART."
        )
    return _to_utc(start, tz, f"The DTSTART of {what}") + duration


def _required_duration(
    available: Available, start_utc: datetime, tz: tzinfo | None
) -> timedelta:
    """The exact duration of an AVAILABLE component."""
    duration = available.DURATION
    if duration is not None:
        return duration
    end = _raw_dt(available, "DTEND")
    if end is None:
        raise IncompleteComponent(
            "Cannot compute free/busy time: "
            "an AVAILABLE component needs DTEND or DURATION."
        )
    return _to_utc(end, tz, "The DTEND of an AVAILABLE component") - start_utc


def _expand_available(
    available: Available,
    window_start: datetime,
    window_end: datetime,
    tz: tzinfo | None,
) -> list[tuple[datetime, datetime]]:
    """Expand one AVAILABLE component into half-open UTC intervals.

    The component must not have a RECURRENCE-ID. Only instances overlapping
    the window are computed, so infinite recurrence rules terminate.
    Recurrences are computed in the timezone of DTSTART so that instances
    keep their local time across daylight saving time transitions.
    """
    start = _raw_dt(available, "DTSTART")
    if start is None:
        raise IncompleteComponent(
            "Cannot compute free/busy time: an AVAILABLE component needs DTSTART."
        )
    start_local = _localize(start, tz, "The DTSTART of an AVAILABLE component")
    start_utc = start_local.astimezone(timezone.utc)
    duration = _required_duration(available, start_utc, tz)
    occurrences: dict[datetime, datetime] = {}
    rrules = available.rrules
    if rrules:
        ruleset = dateutil.rrule.rruleset()
        for recur in rrules:
            rule = dateutil.rrule.rrulestr(
                recur.to_ical().decode("utf-8"), dtstart=start_local
            )
            tzp.fix_rrule_until(rule, recur)
            ruleset.rrule(rule)
        local_tz = start_local.tzinfo
        after = (window_start - duration).astimezone(local_tz)
        before = window_end.astimezone(local_tz)
        for occurrence in ruleset.between(after, before, inc=True):
            occurrence_utc = occurrence.astimezone(timezone.utc)
            occurrences[occurrence_utc] = occurrence_utc + duration
    # DTSTART is always part of the recurrence set.
    occurrences.setdefault(start_utc, start_utc + duration)
    for rdate_start, rdate_end in available.rdates:
        occurrence = _to_utc(rdate_start, tz, "An RDATE of an AVAILABLE component")
        occurrences[occurrence] = (
            _to_utc(rdate_end, tz, "An RDATE of an AVAILABLE component")
            if rdate_end is not None
            else occurrence + duration
        )
    exdates = {
        _to_utc(exdate, tz, "An EXDATE of an AVAILABLE component")
        for exdate in available.exdates
    }
    return [
        (occurrence, end)
        for occurrence, end in occurrences.items()
        if occurrence not in exdates and occurrence < window_end and end > window_start
    ]


def _expand_availables(
    availables: Sequence[Available],
    window_start: datetime,
    window_end: datetime,
    tz: tzinfo | None,
) -> list[tuple[datetime, datetime]]:
    """Expand all AVAILABLE subcomponents of a VAVAILABILITY.

    Components sharing a UID form a recurrence set: components with a
    RECURRENCE-ID replace the matching instance of the recurring components.
    """
    by_uid: dict[str, list[Available]] = {}
    for available in availables:
        uid = available.uid
        by_uid.setdefault(
            str(uid) if uid is not None else f"_{id(available)}", []
        ).append(available)
    intervals = []
    for group in by_uid.values():
        bases = [available for available in group if available.RECURRENCE_ID is None]
        overrides = [
            available for available in group if available.RECURRENCE_ID is not None
        ]
        occurrences = [
            interval
            for base in bases
            for interval in _expand_available(base, window_start, window_end, tz)
        ]
        for override in overrides:
            recurrence_id_value = _raw_dt(override, "RECURRENCE-ID")
            if recurrence_id_value is None:
                continue
            recurrence_id = _to_utc(
                recurrence_id_value,
                tz,
                "A RECURRENCE-ID of an AVAILABLE component",
            )
            occurrences = [
                interval for interval in occurrences if interval[0] != recurrence_id
            ]
            start = _raw_dt(override, "DTSTART")
            if start is None:
                raise IncompleteComponent(
                    "Cannot compute free/busy time: "
                    "an AVAILABLE component needs DTSTART."
                )
            start_utc = _to_utc(start, tz, "The DTSTART of an AVAILABLE component")
            occurrences.append(
                (start_utc, start_utc + _required_duration(override, start_utc, tz))
            )
        intervals.extend(occurrences)
    return intervals


def _merge_intervals(
    intervals: Iterable[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """Merge overlapping and adjacent half-open intervals."""
    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


class _AvailabilityGroup(NamedTuple):
    """The intervals of all VAVAILABILITY components of one priority."""

    #: The covered half-open intervals with the busy type of their component.
    spans: list[tuple[datetime, datetime, FBTYPE]]
    #: The merged free intervals of all AVAILABLE subcomponents.
    free: list[tuple[datetime, datetime]]


def _effective_priority(availability: Availability) -> int:
    """The priority used to order VAVAILABILITY components, lower wins.

    :rfc:`7953` uses the PRIORITY property to combine VAVAILABILITY
    components: 1 is the highest and 9 the lowest priority. Components
    without a defined priority (0, the default) rank below all defined
    priorities.
    """
    priority = availability.priority
    return priority if 1 <= priority <= 9 else 10


def _group_intervals(
    availabilities: Iterable[Availability],
    window_start: datetime,
    window_end: datetime,
    tz: tzinfo | None,
) -> list[_AvailabilityGroup]:
    """Compute the covered and free intervals per priority, highest first."""
    groups: dict[int, _AvailabilityGroup] = {}
    for availability in availabilities:
        start = _raw_dt(availability, "DTSTART")
        end = _component_end(availability, tz, "a VAVAILABILITY component")
        span_start = (
            window_start
            if start is None
            else max(
                _to_utc(start, tz, "The DTSTART of a VAVAILABILITY component"),
                window_start,
            )
        )
        span_end = window_end if end is None else min(end, window_end)
        if span_start >= span_end:
            continue
        free = _merge_intervals(
            (max(occurrence_start, span_start), min(occurrence_end, span_end))
            for occurrence_start, occurrence_end in _expand_availables(
                availability.available, window_start, window_end, tz
            )
            if occurrence_start < span_end and occurrence_end > span_start
        )
        busy_type = FBTYPE(availability.busy_type.value)
        priority = _effective_priority(availability)
        group = groups.setdefault(priority, _AvailabilityGroup([], []))
        group.spans.append((span_start, span_end, busy_type))
        group.free.extend(free)
    return [
        _AvailabilityGroup(group.spans, _merge_intervals(group.free))
        for _, group in sorted(groups.items())
    ]


def _busy_interval(period: BusyPeriod, tz: tzinfo | None) -> _TypedInterval:
    """Normalize a busy period to a typed half-open UTC interval."""
    fbtype: FBTYPE | str = FBTYPE.BUSY
    if isinstance(period, vPeriod):
        period_start: date | datetime = period.start
        period_end: date | datetime | timedelta = period.end
        fbtype = period.params.get("FBTYPE", fbtype)
    else:
        values = tuple(period)
        if len(values) not in (2, 3):
            raise ValueError(
                "A busy period is a (start, end) or (start, end, fbtype) tuple."
            )
        period_start = cast("date | datetime", values[0])
        period_end = cast("date | datetime | timedelta", values[1])
        if len(values) == 3:
            fbtype = cast("FBTYPE | str", values[2])
    period_end_dt: date | datetime = (
        period_start + period_end if isinstance(period_end, timedelta) else period_end
    )
    start = _to_utc(period_start, tz, "The start of a busy period")
    end = _to_utc(period_end_dt, tz, "The end of a busy period")
    if end <= start:
        raise InvalidCalendar("A busy period needs to end after it starts.")
    return _TypedInterval(start, end, FBTYPE(fbtype))


def _compute_intervals(
    groups: list[_AvailabilityGroup],
    busy: list[_TypedInterval],
    window_start: datetime,
    window_end: datetime,
    default: FBTYPE,
) -> list[_TypedInterval]:
    """Combine all intervals into non-overlapping typed intervals.

    The window is split at every interval boundary. Each part takes the type
    of the highest priority VAVAILABILITY group covering it, or ``default``
    where no group applies. Busy periods then override the type where their
    type has a higher precedence.
    """
    points = {window_start, window_end}
    for group in groups:
        for start, end, _fbtype in group.spans:
            points.add(start)
            points.add(end)
        for start, end in group.free:
            points.add(start)
            points.add(end)
    for interval in busy:
        points.add(min(max(interval.start, window_start), window_end))
        points.add(min(max(interval.end, window_start), window_end))
    ordered = sorted(points)
    intervals = []
    for start, end in pairwise(ordered):
        fbtype = None
        for group in groups:
            covering = [
                busy_type
                for span_start, span_end, busy_type in group.spans
                if span_start <= start and span_end >= end
            ]
            if not covering:
                continue
            if any(
                free_start <= start and free_end >= end
                for free_start, free_end in group.free
            ):
                fbtype = FBTYPE.FREE
            else:
                fbtype = max(covering, key=_FBTYPE_PRECEDENCE.__getitem__)
            break
        if fbtype is None:
            fbtype = default
        for interval in busy:
            if (
                interval.start < end
                and interval.end > start
                and _FBTYPE_PRECEDENCE[interval.fbtype] > _FBTYPE_PRECEDENCE[fbtype]
            ):
                fbtype = interval.fbtype
        intervals.append(_TypedInterval(start, end, fbtype))
    merged: list[_TypedInterval] = []
    for interval in intervals:
        if (
            merged
            and merged[-1].fbtype == interval.fbtype
            and merged[-1].end == interval.start
        ):
            merged[-1] = merged[-1]._replace(end=interval.end)
        else:
            merged.append(interval)
    return merged


class FreeBusy(Component):
    """
        A "VFREEBUSY" calendar component is a grouping of component
        properties that represents either a request for free or busy time
        information, a reply to a request for free or busy time
        information, or a published set of busy time information.

    Examples:
        Create a new FreeBusy:

            >>> from icalendar import FreeBusy
            >>> free_busy = FreeBusy.new()
            >>> print(free_busy.to_ical())
            BEGIN:VFREEBUSY
            DTSTAMP:20250517T080612Z
            UID:d755cef5-2311-46ed-a0e1-6733c9e15c63
            END:VFREEBUSY

        Get the example FreeBusy.

        .. code-block:: pycon

            >>> from icalendar import FreeBusy
            >>> free_busy = FreeBusy.example()
            >>> print(free_busy.to_ical().decode())
            BEGIN:VFREEBUSY
            DTEND:19980410T234500Z
            DTSTAMP:19970901T120000Z
            DTSTART:19980313T141711Z
            FREEBUSY:19980314T233000Z/19980315T003000Z
            FREEBUSY:19980316T153000Z/19980316T163000Z
            FREEBUSY:19980318T030000Z/19980318T040000Z
            ORGANIZER:jsmith@example.com
            UID:19970901T115957Z-76A912@example.com
            URL:http://www.example.com/calendar/busytime/jsmith.ifb
            END:VFREEBUSY

    """

    name = "VFREEBUSY"

    required = (
        "UID",
        "DTSTAMP",
    )
    singletons = (
        "CONTACT",
        "DTSTART",
        "DTEND",
        "DTSTAMP",
        "ORGANIZER",
        "UID",
        "URL",
    )
    multiple = (
        "ATTENDEE",
        "COMMENT",
        "FREEBUSY",
        "REQUEST-STATUS",
    )
    uid = uid_property
    url = url_property
    organizer = organizer_property
    contacts = contacts_property
    REQUEST_STATUS = REQUEST_STATUS_property
    start = DTSTART = create_single_property(
        "DTSTART",
        "dt",
        (datetime, date),
        date,
        'The "DTSTART" property for a "VFREEBUSY" specifies the inclusive start of the component.',
    )
    end = DTEND = create_single_property(
        "DTEND",
        "dt",
        (datetime, date),
        date,
        'The "DTEND" property for a "VFREEBUSY" calendar component specifies the non-inclusive end of the component.',
    )

    @property
    def duration(self) -> timedelta | None:
        """The duration computed from start and end."""
        if self.DTSTART is None or self.DTEND is None:
            return None
        return self.DTEND - self.DTSTART

    @classmethod
    def new(
        cls,
        /,
        comments: list[str] | str | None = None,
        concepts: CONCEPTS_TYPE_SETTER = None,
        contacts: list[str] | str | None = None,
        end: date | datetime | None = None,
        links: LINKS_TYPE_SETTER = None,
        organizer: vCalAddress | str | None = None,
        refids: list[str] | str | None = None,
        related_to: RELATED_TO_TYPE_SETTER = None,
        request_status: list[str] | str | None = None,
        stamp: date | None = None,
        start: date | datetime | None = None,
        uid: str | uuid.UUID | None = None,
        url: str | None = None,
    ) -> Self:
        """Create a new FreeBusy component with all required properties,
        in accordance with :rfc:`5545#section-3.6.4`.

        The FreeBusy component has the required properties of UID and DTSTAMP,
        which may be set with the parameters of ``uid`` and ``stamp``.

        Parameters:
            comments: The :attr:`~icalendar.cal.component.Component.comments` of the component.
            concepts: The :attr:`~icalendar.cal.component.Component.concepts` of the component.
            contacts: The :attr:`contacts` of the component.
            end: The :attr:`end` of the component.
            links: The :attr:`~icalendar.cal.component.Component.links` of the component.
            organizer: The :attr:`organizer` of the component.
            refids: :attr:`~icalendar.cal.component.Component.refids` of the component.
            related_to: :attr:`~icalendar.cal.component.Component.related_to` of the component.
            request_status: The :attr:`REQUEST_STATUS` of the component.
            stamp: The :attr:`~icalendar.cal.component.Component.DTSTAMP` of the component.
                If None, this is set to the current time.
            start: The :attr:`start` of the component.
            uid: The :attr:`uid` of the component.
                If None, this is set to a new :func:`uuid.uuid4`.
            url: The :attr:`url` of the component.

        Returns:
            :class:`FreeBusy`

        Raises:
            :exc:`~icalendar.error.InvalidCalendar`: If the content is not valid
                according to :rfc:`5545`.

        .. warning:: As time progresses, we will be stricter with the validation.
        """
        free_busy: Self = super().new(
            stamp=stamp if stamp is not None else cls._utc_now(),
            comments=comments,
            links=links,
            related_to=related_to,
            refids=refids,
            concepts=concepts,
        )
        free_busy.uid = uid if uid is not None else uuid.uuid4()
        free_busy.url = url
        free_busy.organizer = organizer
        free_busy.contacts = contacts
        free_busy.REQUEST_STATUS = request_status
        free_busy.end = end
        free_busy.start = start

        if cls._validate_new:
            cls._validate_start_and_end(start, end)
        return free_busy

    @classmethod
    def example(cls, name: str = "example") -> FreeBusy:
        """Return the FreeBusy example with the given name."""
        return cls.from_ical(get_example("freebusy", name))

    @classmethod
    def from_availability(
        cls,
        availabilities: Availability | Iterable[Availability] | None = None,
        busy: Iterable[BusyPeriod] = (),
        *,
        start: datetime,
        end: datetime,
        default: FBTYPE | str = FBTYPE.FREE,
        tz: tzinfo | str | None = None,
        stamp: date | None = None,
        uid: str | uuid.UUID | None = None,
        organizer: vCalAddress | str | None = None,
    ) -> Self:
        """Compute a VFREEBUSY from VAVAILABILITY components and busy periods.

        This combines :rfc:`7953` VAVAILABILITY components with actual busy
        periods into a VFREEBUSY component that covers the window
        [``start``, ``end``) with non-overlapping, half-open UTC
        intervals. Adjacent intervals of the same type are merged.

        The computation follows :rfc:`7953`:

        -   ``AVAILABLE`` subcomponents are expanded using their ``DTSTART``,
            ``DTEND`` or ``DURATION``, and their recurrence rules. Infinite
            recurrence rules are only evaluated within the window.
        -   Time covered by an ``AVAILABLE`` subcomponent is ``FREE``, other
            time covered by a ``VAVAILABILITY`` component uses its
            ``BUSYTYPE`` (``BUSY-UNAVAILABLE`` if not set).
        -   Where ``VAVAILABILITY`` components overlap, the one with the
            higher ``PRIORITY`` (lower value) wins, but only for the
            overlapping part. Components without a defined priority rank
            below all defined priorities. Components of the same priority
            are merged.
        -   Time not covered by any ``VAVAILABILITY`` component gets the
            ``default`` type.
        -   ``busy`` periods are overlaid last. Where types conflict, the
            precedence is ``BUSY-UNAVAILABLE`` > ``BUSY`` >
            ``BUSY-TENTATIVE`` > ``FREE``.

        Parameters:
            availabilities: One or more :class:`~icalendar.Availability`
                components.
            busy: Busy periods to overlay. Each period is a
                :class:`~icalendar.prop.vPeriod`, a ``(start, end)`` tuple,
                or a ``(start, end, fbtype)`` tuple. The end can be given as
                a :class:`~datetime.datetime` or as a
                :class:`~datetime.timedelta` duration. The type defaults to
                ``BUSY``.
            start: The inclusive start of the window.
            end: The non-inclusive end of the window.
            default: The :class:`~icalendar.enums.FBTYPE` of window parts not
                covered by any availability information.
            tz: The timezone or timezone id used to interpret floating
                (naive) times. Floating times are never interpreted in the
                timezone of the process: if any involved time is floating and
                ``tz`` is not given, a :exc:`ValueError` is raised.
            stamp: The :attr:`~icalendar.cal.component.Component.DTSTAMP` of
                the component. If None, this is set to the current time.
            uid: The :attr:`uid` of the component.
                If ``None``, this is set to a new :func:`uuid.uuid4`.
            organizer: The :attr:`organizer` of the component.

        Returns:
            :class:`FreeBusy`

        Raises:
            ValueError: If a floating time is used without ``tz``.
            ~error.InvalidCalendar: If the window or a busy period is invalid.
            ~error.IncompleteComponent: If an ``AVAILABLE`` component lacks
                the information needed to expand it.

        Example:
            Compute the free/busy time for one week of office hours:

            .. code-block:: pycon

                >>> from datetime import datetime, timezone
                >>> from icalendar import Availability, Available, FreeBusy
                >>> office = Available.new(
                ...     start=datetime(2026, 9, 21, 9, tzinfo=timezone.utc),
                ...     end=datetime(2026, 9, 21, 17, tzinfo=timezone.utc),
                ... )
                >>> office.add("RRULE", {"FREQ": "WEEKLY", "BYDAY": ["MO", "TU", "WE", "TH", "FR"]})
                >>> availability = Availability.new(components=[office])
                >>> free_busy = FreeBusy.from_availability(
                ...     availability,
                ...     start=datetime(2026, 9, 21, tzinfo=timezone.utc),
                ...     end=datetime(2026, 9, 28, tzinfo=timezone.utc),
                ... )
                >>> print(free_busy.to_ical().decode())
                BEGIN:VFREEBUSY
                DTEND:20260928T000000Z
                DTSTAMP:20250517T080612Z
                DTSTART:20260921T000000Z
                FREEBUSY;FBTYPE=BUSY-UNAVAILABLE:20260921T000000Z/20260921T090000Z
                FREEBUSY;FBTYPE=FREE:20260921T090000Z/20260921T170000Z
                FREEBUSY;FBTYPE=BUSY-UNAVAILABLE:20260921T170000Z/20260922T090000Z
                FREEBUSY;FBTYPE=FREE:20260922T090000Z/20260922T170000Z
                FREEBUSY;FBTYPE=BUSY-UNAVAILABLE:20260922T170000Z/20260923T090000Z
                FREEBUSY;FBTYPE=FREE:20260923T090000Z/20260923T170000Z
                FREEBUSY;FBTYPE=BUSY-UNAVAILABLE:20260923T170000Z/20260924T090000Z
                FREEBUSY;FBTYPE=FREE:20260924T090000Z/20260924T170000Z
                FREEBUSY;FBTYPE=BUSY-UNAVAILABLE:20260924T170000Z/20260925T090000Z
                FREEBUSY;FBTYPE=FREE:20260925T090000Z/20260925T170000Z
                FREEBUSY;FBTYPE=BUSY-UNAVAILABLE:20260925T170000Z/20260928T000000Z
                UID:d755cef5-2311-46ed-a0e1-6733c9e15c63
                END:VFREEBUSY

        """
        from icalendar.cal.availability import Availability

        tz = _normalize_tz(tz)
        window_start = _to_utc(start, tz, "The start of the free/busy window")
        window_end = _to_utc(end, tz, "The end of the free/busy window")
        if window_end <= window_start:
            raise InvalidCalendar("The free/busy window needs to end after it starts.")
        if availabilities is None:
            availabilities = []
        elif isinstance(availabilities, Availability):
            availabilities = [availabilities]
        groups = _group_intervals(availabilities, window_start, window_end, tz)
        busy_intervals = [_busy_interval(period, tz) for period in busy]
        intervals = _compute_intervals(
            groups, busy_intervals, window_start, window_end, FBTYPE(default)
        )
        free_busy = cls.new(
            start=window_start,
            end=window_end,
            stamp=stamp,
            uid=uid,
            organizer=organizer,
        )
        for interval in intervals:
            period = vPeriod((interval.start, interval.end))
            # PERIOD is the default value type, so the parameter is redundant.
            period.params.pop("VALUE", None)
            free_busy.add(
                "FREEBUSY",
                period,
                parameters={"FBTYPE": interval.fbtype.value},
            )
        return free_busy


__all__ = ["BusyPeriod", "FreeBusy"]
