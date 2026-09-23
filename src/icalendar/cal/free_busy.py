""":rfc:`5545` VFREEBUSY component."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone, tzinfo
from itertools import pairwise
from typing import TYPE_CHECKING

from dateutil.rrule import rruleset, rrulestr

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
from icalendar.enums import BUSYTYPE, FBTYPE
from icalendar.error import InvalidCalendar
from icalendar.prop import vPeriod, vRecur
from icalendar.timezone import tzp

from .availability import Availability

if TYPE_CHECKING:
    from collections.abc import Iterable

    from icalendar.compatibility import Self
    from icalendar.prop import vCalAddress

_UTC = timezone.utc

#: Precedence of "FBTYPE" values when busy intervals of different types
#: overlap: "BUSY-UNAVAILABLE" wins over "BUSY", "BUSY" wins over
#: "BUSY-TENTATIVE", and any busy type wins over "FREE".
FBTYPE_PRECEDENCE: dict[FBTYPE, int] = {
    FBTYPE.FREE: 0,
    FBTYPE.BUSY_TENTATIVE: 1,
    FBTYPE.BUSY: 2,
    FBTYPE.BUSY_UNAVAILABLE: 3,
}


def _floating_timezone(floating_tz: tzinfo | str | None, what: str) -> tzinfo:
    """Return the timezone to interpret floating times in.

    Floating times must never be interpreted in the process-local timezone,
    so an explicit timezone is required.
    """
    if floating_tz is None:
        raise ValueError(
            f"{what} is a floating time; pass floating_tz to say how to interpret it."
        )
    if isinstance(floating_tz, str):
        tz = tzp.timezone(floating_tz)
        if tz is None:
            raise ValueError(f"Unknown timezone {floating_tz!r} for {what}.")
        return tz
    return floating_tz


def _localize(
    dt: date | datetime, floating_tz: tzinfo | str | None, what: str
) -> datetime:
    """Return an aware datetime, interpreting floating times with floating_tz."""
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            return tzp.localize(dt, _floating_timezone(floating_tz, what))
        return dt
    return tzp.localize(
        datetime(dt.year, dt.month, dt.day), _floating_timezone(floating_tz, what)
    )


def _as_utc(
    dt: date | datetime, floating_tz: tzinfo | str | None, what: str
) -> datetime:
    """Return an aware UTC datetime, interpreting floating times with floating_tz."""
    return _localize(dt, floating_tz, what).astimezone(_UTC)


def _coerce_fbtype(value: FBTYPE | BUSYTYPE | str) -> FBTYPE:
    """Convert a busy type to an :class:`~icalendar.enums.FBTYPE`."""
    if isinstance(value, FBTYPE):
        return value
    if isinstance(value, BUSYTYPE):
        return FBTYPE(value.value)
    return FBTYPE(str(value))


def _raw_dt(component: Component, name: str) -> date | datetime | None:
    """The raw value of a datetime property, keeping its own timezone.

    The component properties normalize to UTC, but expanding recurrences
    and detecting floating times need the value as written.
    """
    value = component.get(name)
    if value is None:
        return None
    return getattr(value, "dt", value)


def _rrule_string(recur: vRecur) -> str:
    """Serialize an RRULE, normalizing UNTIL to UTC as :rfc:`5545` requires.

    A date or floating UNTIL is interpreted as UTC so that the rule can be
    evaluated against a timezone-aware DTSTART.
    """
    recur = vRecur(recur)
    if "UNTIL" in recur:
        recur["UNTIL"] = [
            _as_utc(until, _UTC, "RRULE UNTIL") for until in recur["UNTIL"]
        ]
    return recur.to_ical().decode("utf-8")


def _union(
    intervals: Iterable[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """Merge overlapping or adjacent half-open intervals."""
    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _subtract(
    base: Iterable[tuple[datetime, datetime]],
    remove: Iterable[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """Subtract the remove intervals from the base intervals.

    The base intervals must not overlap, but adjacent intervals are kept
    separate so that their attributes can be told apart afterwards.
    """
    remaining = _union(remove)
    result: list[tuple[datetime, datetime]] = []
    for start, end in sorted(base):
        current = start
        for remove_start, remove_end in remaining:
            if remove_end <= current:
                continue
            if remove_start >= end:
                break
            if remove_start > current:
                result.append((current, remove_start))
            current = max(current, remove_end)
            if current >= end:
                break
        if current < end:
            result.append((current, end))
    return result


def _merge_typed(
    intervals: Iterable[tuple[datetime, datetime, FBTYPE]],
) -> list[tuple[datetime, datetime, FBTYPE]]:
    """Merge typed half-open intervals into non-overlapping intervals.

    Where intervals overlap, the type with the highest :data:`FBTYPE_PRECEDENCE`
    wins. Adjacent intervals of the same type are merged, and "FREE" intervals
    are dropped.
    """
    intervals = [(s, e, t) for s, e, t in intervals if e > s]
    points = sorted({point for interval in intervals for point in interval[:2]})
    merged: list[tuple[datetime, datetime, FBTYPE]] = []
    for start, end in pairwise(points):
        best: FBTYPE | None = None
        for interval_start, interval_end, fbtype in intervals:
            if (
                interval_start < end
                and interval_end > start
                and (
                    best is None or FBTYPE_PRECEDENCE[fbtype] > FBTYPE_PRECEDENCE[best]
                )
            ):
                best = fbtype
        if best is None or best is FBTYPE.FREE:
            continue
        if merged and merged[-1][1] == start and merged[-1][2] is best:
            merged[-1] = (merged[-1][0], end, best)
        else:
            merged.append((start, end, best))
    return merged


def _expand_available(
    available: Availability,
    window_start: datetime,
    window_end: datetime,
    floating_tz: tzinfo | str | None,
) -> list[tuple[datetime, datetime]]:
    """Expand an "AVAILABLE" component into UTC intervals within the window.

    Recurrence rules are only evaluated within the window, so infinite rules
    terminate. The result is clipped to the half-open window.
    """
    dtstart = _raw_dt(available, "DTSTART")
    if dtstart is None:
        raise InvalidCalendar("AVAILABLE requires a DTSTART to compute free/busy time.")
    start = _localize(dtstart, floating_tz, "Available DTSTART")
    raw_end = _raw_dt(available, "DTEND")
    if raw_end is not None:
        duration = _localize(raw_end, floating_tz, "Available DTEND") - start
    else:
        duration = available.DURATION
        if duration is None:
            duration = timedelta(0)
    if duration < timedelta(0):
        raise InvalidCalendar("AVAILABLE has a negative duration.")
    exdates = {
        _localize(exdate, floating_tz, "Available EXDATE")
        for exdate in available.exdates
    }
    instances: list[tuple[datetime, timedelta]] = []
    if available.rrules or available.rdates:
        recurrence_set = rruleset()
        # DTSTART is always part of the recurrence set.
        recurrence_set.rdate(start)
        for recur in available.rrules:
            recurrence_set.rrule(rrulestr(_rrule_string(recur), dtstart=start))
        for rdate_start, rdate_end in available.rdates:
            rdate_start = _localize(rdate_start, floating_tz, "Available RDATE")
            if rdate_end is None:
                recurrence_set.rdate(rdate_start)
            elif rdate_start not in exdates:
                # an RDATE of value type PERIOD carries its own duration
                instances.append(
                    (
                        rdate_start,
                        _localize(rdate_end, floating_tz, "Available RDATE")
                        - rdate_start,
                    )
                )
        for exdate in exdates:
            recurrence_set.exdate(exdate)
        instances.extend(
            (occurrence, duration)
            for occurrence in recurrence_set.between(
                window_start - duration, window_end, inc=True
            )
        )
    elif start not in exdates:
        instances.append((start, duration))
    return [
        (
            max(instance_start.astimezone(_UTC), window_start),
            min((instance_start + instance_duration).astimezone(_UTC), window_end),
        )
        for instance_start, instance_duration in instances
        if instance_start < window_end
        and instance_start + instance_duration > window_start
    ]


def _availability_busy_intervals(
    availability: Availability,
    window_start: datetime,
    window_end: datetime,
    floating_tz: tzinfo | str | None,
) -> tuple[tuple[datetime, datetime], list[tuple[datetime, datetime, FBTYPE]]] | None:
    """Compute the coverage and the typed busy intervals of one VAVAILABILITY.

    The coverage is the "VAVAILABILITY" time range clipped to the window.
    Within the coverage, time not marked by any "AVAILABLE" subcomponent is
    busy with the component's "BUSYTYPE" (default "BUSY-UNAVAILABLE").
    """
    start = _raw_dt(availability, "DTSTART")
    end = _raw_dt(availability, "DTEND")
    if end is None:
        duration = availability.DURATION
        if duration is not None:
            if start is None:
                raise InvalidCalendar("VAVAILABILITY has a DURATION but no DTSTART.")
            end = _localize(start, floating_tz, "Availability DTSTART") + duration
    coverage_start = (
        window_start
        if start is None
        else max(_as_utc(start, floating_tz, "Availability DTSTART"), window_start)
    )
    coverage_end = (
        window_end
        if end is None
        else min(_as_utc(end, floating_tz, "Availability DTEND"), window_end)
    )
    if coverage_end <= coverage_start:
        return None
    free: list[tuple[datetime, datetime]] = []
    for available in availability.available:
        free.extend(
            _expand_available(available, coverage_start, coverage_end, floating_tz)
        )
    busy_type = _coerce_fbtype(availability.busy_type or BUSYTYPE.BUSY_UNAVAILABLE)
    busy = [
        (busy_start, busy_end, busy_type)
        for busy_start, busy_end in _subtract(
            [(coverage_start, coverage_end)], _union(free)
        )
    ]
    return (coverage_start, coverage_end), busy


def _normalize_busy_period(
    period, floating_tz: tzinfo | str | None
) -> tuple[datetime, datetime, FBTYPE]:
    """Normalize a busy period input to (start, end, fbtype) in UTC.

    The period end can be given as an end datetime or as a duration.
    """
    fbtype: FBTYPE | BUSYTYPE | str = FBTYPE.BUSY
    if isinstance(period, vPeriod):
        start, end_or_duration = period.start, period.end
        fbtype = period.params.get("FBTYPE", fbtype)
    elif len(period) == 3:
        start, end_or_duration, fbtype = period
    else:
        start, end_or_duration = period
    start = _as_utc(start, floating_tz, "busy period start")
    if isinstance(end_or_duration, timedelta):
        end = start + end_or_duration
    else:
        end = _as_utc(end_or_duration, floating_tz, "busy period end")
    if end <= start:
        raise ValueError(f"Busy period ends before it starts: {period!r}")
    return start, end, _coerce_fbtype(fbtype)


def compute_free_busy_intervals(
    availabilities: Availability | Iterable[Availability],
    start: datetime,
    end: datetime,
    *,
    busy_periods: Iterable = (),
    default_busy_type: FBTYPE | BUSYTYPE | str = FBTYPE.BUSY,
    floating_tz: tzinfo | str | None = None,
) -> list[tuple[datetime, datetime, FBTYPE]]:
    """Compute the busy intervals of a query window from availability information.

    This combines "VAVAILABILITY" components according to :rfc:`7953` and
    overlays actual busy periods, producing the busy content of a "VFREEBUSY"
    component for the window [start, end).

    Parameters:
        availabilities:
            One "VAVAILABILITY" component, an iterable of them, or a component
            such as a :class:`~icalendar.Calendar` to walk for them.
            Components are combined by their "PRIORITY" property: a lower
            value takes precedence, and a missing "PRIORITY" means 0, as
            specified in :rfc:`7953`. Where a higher-priority component
            covers time, lower-priority components only yield in the covered
            part; components of the same priority are merged.
        start: The inclusive start of the query window.
        end: The non-inclusive end of the query window.
        busy_periods:
            Actual busy periods to overlay, for example meetings. Each period
            is a :class:`~icalendar.prop.vPeriod`, a
            (start, end_or_duration) tuple, or a
            (start, end_or_duration, fbtype) tuple; the type defaults to
            "BUSY". Where types conflict, :data:`FBTYPE_PRECEDENCE` decides:
            "BUSY-UNAVAILABLE" > "BUSY" > "BUSY-TENTATIVE" > "FREE".
        default_busy_type:
            The busy type of window parts not covered by any "VAVAILABILITY"
            component. Use "FREE" to leave those parts unreported.
        floating_tz:
            The timezone used to interpret floating (naive) times, as a
            :class:`datetime.tzinfo` or a TZID string. Floating times are
            never interpreted in the process-local timezone: if any floating
            time occurs and this is not given, a :class:`ValueError` is
            raised.

    Returns:
        A sorted list of non-overlapping half-open (start, end, fbtype)
        intervals in UTC. Adjacent intervals of the same type are merged.

    Raises:
        ValueError: If a floating time occurs without floating_tz, or if
            the window or a busy period ends before it starts.
        ~icalendar.error.InvalidCalendar: If an "AVAILABLE" component cannot
            be expanded.
    """
    if isinstance(availabilities, Availability):
        availabilities = (availabilities,)
    elif isinstance(availabilities, Component):
        availabilities = availabilities.walk(Availability.name)
    window_start = _as_utc(start, floating_tz, "query window start")
    window_end = _as_utc(end, floating_tz, "query window end")
    if window_end <= window_start:
        raise ValueError(
            f"The query window ends before it starts: {start!r} to {end!r}"
        )

    by_priority: dict[int, list] = {}
    for availability in availabilities:
        computed = _availability_busy_intervals(
            availability, window_start, window_end, floating_tz
        )
        if computed is not None:
            by_priority.setdefault(availability.priority, []).append(computed)

    intervals: list[tuple[datetime, datetime, FBTYPE]] = []
    covered: list[tuple[datetime, datetime]] = []
    for priority in sorted(by_priority):
        group = by_priority[priority]
        group_busy = _merge_typed(interval for _, busy in group for interval in busy)
        for busy_start, busy_end in _subtract(
            [(busy[0], busy[1]) for busy in group_busy], covered
        ):
            fbtype = next(
                t for s, e, t in group_busy if s <= busy_start and e >= busy_end
            )
            intervals.append((busy_start, busy_end, fbtype))
        covered = _union(covered + [coverage for coverage, _ in group])

    default = _coerce_fbtype(default_busy_type)
    if default is not FBTYPE.FREE:
        intervals.extend(
            (default_start, default_end, default)
            for default_start, default_end in _subtract(
                [(window_start, window_end)], covered
            )
        )

    for period in busy_periods:
        period_start, period_end, fbtype = _normalize_busy_period(period, floating_tz)
        period_start = max(period_start, window_start)
        period_end = min(period_end, window_end)
        if period_end > period_start:
            intervals.append((period_start, period_end, fbtype))

    return _merge_typed(intervals)


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
        availabilities: Availability | Iterable[Availability],
        start: datetime,
        end: datetime,
        *,
        busy_periods: Iterable = (),
        default_busy_type: FBTYPE | BUSYTYPE | str = FBTYPE.BUSY,
        floating_tz: tzinfo | str | None = None,
        comments: list[str] | str | None = None,
        organizer: vCalAddress | str | None = None,
        stamp: date | None = None,
        uid: str | uuid.UUID | None = None,
        url: str | None = None,
    ) -> Self:
        """Create a "VFREEBUSY" component from availability information.

        This computes the busy time of the query window [start, end) from
        "VAVAILABILITY" components as specified in :rfc:`7953` and overlays
        actual busy periods such as meetings. The result is a "VFREEBUSY"
        component with one "FREEBUSY" property per busy interval, ready to be
        sent as a reply to a free/busy request.

        Parameters:
            availabilities:
                One "VAVAILABILITY" component, an iterable of them, or a
                component such as a :class:`~icalendar.Calendar` to walk for
                them. See :func:`compute_free_busy_intervals` for how
                "PRIORITY", "BUSYTYPE", and the "AVAILABLE" subcomponents are
                combined.
            start: The inclusive start of the query window.
            end: The non-inclusive end of the query window.
            busy_periods:
                Actual busy periods to overlay. Each period is a
                :class:`~icalendar.prop.vPeriod`, a
                (start, end_or_duration) tuple, or a
                (start, end_or_duration, fbtype) tuple; the type defaults to
                "BUSY". Type conflicts follow :data:`FBTYPE_PRECEDENCE`.
            default_busy_type:
                The busy type of window parts not covered by any
                "VAVAILABILITY" component. Use "FREE" to leave those parts
                unreported.
            floating_tz:
                The timezone used to interpret floating (naive) times, as a
                :class:`datetime.tzinfo` or a TZID string. Floating times
                are never interpreted in the process-local timezone.
            comments: The :attr:`~icalendar.cal.component.Component.comments`
                of the component.
            organizer: The :attr:`organizer` of the component.
            stamp: The :attr:`~icalendar.cal.component.Component.DTSTAMP` of
                the component. If None, this is set to the current time.
            uid: The :attr:`uid` of the component.
                If None, this is set to a new :func:`uuid.uuid4`.
            url: The :attr:`url` of the component.

        Returns:
            :class:`FreeBusy` with "DTSTART" and "DTEND" set to the query
            window in UTC and a "FREEBUSY" property with an "FBTYPE"
            parameter for each non-overlapping busy interval.

        Example:
            A user available Monday to Friday, 9:00 to 17:00 UTC, is busy
            with the default "BUSY-UNAVAILABLE" type outside those hours:

            .. code-block:: pycon

                >>> from datetime import datetime, timezone
                >>> from icalendar import Availability, Available, FreeBusy
                >>> from icalendar.prop import vRecur
                >>> office = Available.new(
                ...     start=datetime(2011, 10, 3, 9, tzinfo=timezone.utc),
                ...     end=datetime(2011, 10, 3, 17, tzinfo=timezone.utc),
                ... )
                >>> office.add("RRULE", vRecur.from_ical("FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"))
                >>> availability = Availability.new(
                ...     start=datetime(2011, 10, 3, tzinfo=timezone.utc),
                ...     components=[office],
                ... )
                >>> free_busy = FreeBusy.from_availability(
                ...     availability,
                ...     datetime(2011, 10, 3, tzinfo=timezone.utc),
                ...     datetime(2011, 10, 5, tzinfo=timezone.utc),
                ... )
                >>> for period in free_busy["FREEBUSY"]:
                ...     print(period.to_ical().decode(), period.params["FBTYPE"])
                20111003T000000Z/20111003T090000Z BUSY-UNAVAILABLE
                20111003T170000Z/20111004T090000Z BUSY-UNAVAILABLE
                20111004T170000Z/20111005T000000Z BUSY-UNAVAILABLE

        .. versionadded:: 7.0.0
        """
        intervals = compute_free_busy_intervals(
            availabilities,
            start,
            end,
            busy_periods=busy_periods,
            default_busy_type=default_busy_type,
            floating_tz=floating_tz,
        )
        free_busy = cls.new(
            comments=comments,
            end=_as_utc(end, floating_tz, "query window end"),
            organizer=organizer,
            stamp=stamp,
            start=_as_utc(start, floating_tz, "query window start"),
            uid=uid,
            url=url,
        )
        for interval_start, interval_end, fbtype in intervals:
            free_busy.add(
                "FREEBUSY",
                (interval_start, interval_end),
                parameters={"FBTYPE": fbtype.value},
            )
        return free_busy


__all__ = ["FBTYPE_PRECEDENCE", "FreeBusy", "compute_free_busy_intervals"]
