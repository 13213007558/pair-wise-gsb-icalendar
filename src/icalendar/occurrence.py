"""Query actual occurrences of recurring events within a finite time window.

This module implements the recurrence set expansion described in
:rfc:`5545#section-3.8.5`:

-   ``DTSTART``, ``RRULE``, ``RDATE`` and ``EXDATE`` of the master ``VEVENT``
    are combined into the set of recurrence identities.
-   Detached ``VEVENT`` components (those carrying a ``RECURRENCE-ID``)
    override the matching identity. Cancelled exceptions
    (``STATUS:CANCELLED``) are removed from the result.
-   ``RECURRENCE-ID;RANGE=THISANDFUTURE`` moves this and all following
    instances, including their duration, keyed by the *original*
    recurrence identity.
-   The query window is left-closed and right-open; an instance is returned
    when its actual (possibly rescheduled) interval intersects the window.
-   Infinite recurrence rules are only expanded inside a bounded region and
    the number of generated candidates is limited. When the limit is hit,
    the result is marked as incomplete.

All-day events (``VALUE=DATE``) and timed events (``DATE-TIME``) are handled
separately. Floating ``DATE-TIME`` values (without ``TZID`` and without a
trailing ``Z``) are only expanded when the caller provides the timezone in
which they are to be interpreted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import TYPE_CHECKING

from dateutil.rrule import rrulestr

from icalendar.error import FloatingTimeError, InvalidCalendar, UnsupportedRecurrence
from icalendar.tools import is_date

if TYPE_CHECKING:
    from collections.abc import Iterable

    from icalendar.cal.event import Event
    from icalendar.prop import vRecur


UTC = timezone.utc

#: Default upper bound for the number of recurrence candidates one query may
#: generate per calendar. If the expansion reaches this number, the returned
#: :class:`Occurrences` is marked as incomplete.
DEFAULT_MAX_CANDIDATES = 1000


@dataclass
class Occurrence:
    """One actually occurring instance of a recurring event.

    Attributes:
        start: The actual start of the instance. This has the same value type
            as the master event's ``DTSTART``: a :class:`~datetime.date` for
            all-day events and a :class:`~datetime.datetime` otherwise.
        end: The actual, non-inclusive end of the instance.
        recurrence_id: The original recurrence identity, i.e. the start the
            instance would have had without any exception. This equals the
            ``RECURRENCE-ID`` value a detached component would use.
        uid: The ``UID`` shared by all components of the event series.
        component: The component that defines this occurrence: the master
            ``VEVENT`` for regular and merely shifted instances, the
            detached ``VEVENT`` for exceptions. Treat it as read-only.
        modified: ``True`` when a detached ``VEVENT`` or
            ``RANGE=THISANDFUTURE`` changes this instance.

    The returned value objects belong to the occurrence, not to the calendar;
    changing them never mutates the source calendar. ``component`` is the
    original component and should not be modified.
    """

    start: date | datetime
    end: date | datetime
    recurrence_id: date | datetime
    uid: str
    component: Event
    modified: bool = False

    @property
    def duration(self) -> timedelta:
        """The actual duration of the occurrence."""
        return self.end - self.start


class Occurrences(list[Occurrence]):
    """The occurrences returned by a windowed query.

    This is a plain list of :class:`Occurrence` sorted by start time,
    extended by the :attr:`complete` flag.
    """

    complete: bool

    def __init__(self, iterable: Iterable[Occurrence] = (), *, complete: bool = True):
        """Create the result list.

        Parameters:
            iterable: The occurrences to include.
            complete: Whether the expansion could consider every possible
                candidate. ``False`` means the expansion produced more
                candidates than allowed and occurrences may be missing.
        """
        super().__init__(iterable)
        self.complete = complete


class _CandidateBudget:
    """Count generated candidates and flag exhaustion."""

    def __init__(self, limit: int) -> None:
        self.remaining = limit
        self.exhausted = False

    def consume(self, amount: int = 1) -> bool:
        """Consume ``amount`` candidates.

        Returns ``False`` once the budget is exhausted and marks the result
        as incomplete.
        """
        if self.remaining <= 0:
            self.exhausted = True
            return False
        self.remaining -= amount
        return True


def _to_comparable(value: date, floating_tz: tzinfo | None) -> datetime:
    """Map any start/end value to an aware ``datetime`` in UTC.

    Floating ``DATE-TIME`` values require an explicit timezone; all-day
    ``DATE`` values are interpreted at midnight in ``floating_tz`` (or UTC).
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            if floating_tz is None:
                raise FloatingTimeError(
                    "A floating DATE-TIME needs an explicit timezone: pass "
                    "floating_tz=<tzinfo> to the occurrence query."
                )
            return value.replace(tzinfo=floating_tz).astimezone(UTC)
        return value.astimezone(UTC)
    reference = floating_tz if floating_tz is not None else UTC
    midnight = datetime(value.year, value.month, value.day, tzinfo=reference)
    return midnight.astimezone(UTC)


def _window_bound(value: date, floating_tz: tzinfo | None) -> datetime:
    """Normalize one window bound to an aware UTC ``datetime``."""
    if is_date(value):
        reference = floating_tz if floating_tz is not None else UTC
        midnight = datetime(value.year, value.month, value.day, tzinfo=reference)
        return midnight.astimezone(UTC)
    if value.tzinfo is None:
        raise FloatingTimeError(
            "The query window must use aware datetimes or dates: a naive "
            "datetime window cannot be matched against recurring events."
        )
    return value.astimezone(UTC)


def _fix_until(
    until: date, rule_dtstart: datetime, floating_tz: tzinfo | None
) -> datetime:
    """Make an RRULE ``UNTIL`` value compatible with the rrule ``DTSTART``."""
    result = datetime.combine(until, time.min) if is_date(until) else until
    start_is_aware = rule_dtstart.tzinfo is not None
    until_is_aware = result.tzinfo is not None
    if start_is_aware and not until_is_aware:
        # RFC 5545 requires a UTC UNTIL for an aware DTSTART, but tolerantly
        # interpret a floating UNTIL as being in the DTSTART timezone.
        result = result.replace(tzinfo=rule_dtstart.tzinfo)
    elif not start_is_aware and until_is_aware:
        reference = floating_tz if floating_tz is not None else UTC
        result = result.astimezone(reference).replace(tzinfo=None)
    return result


def _build_rrule(recur: vRecur, dtstart: date, floating_tz: tzinfo | None):
    """Build a dateutil rrule from the calendar component's RRULE."""
    rule_dtstart = dtstart
    if is_date(dtstart):
        rule_dtstart = datetime.combine(dtstart, time.min)
    until_values = recur.get("UNTIL")
    if until_values:
        recur = type(recur)(recur)
        recur["UNTIL"] = [_fix_until(until_values[0], rule_dtstart, floating_tz)]
    return rrulestr(f"RRULE:{recur.to_ical().decode()}", dtstart=rule_dtstart)


@dataclass
class _SingleOverride:
    """A detached VEVENT overriding exactly one recurrence identity."""

    start: date | datetime
    duration: timedelta
    cancelled: bool
    component: Event


@dataclass
class _ThisAndFuture:
    """A ``RANGE=THISANDFUTURE`` exception."""

    identity_key: datetime
    delta: timedelta
    duration: timedelta
    cancelled: bool
    component: Event


def _delta(old: date, new: date) -> timedelta:
    """Compute the timedelta between two values of matching value types."""
    if type(old) is not type(new):
        raise InvalidCalendar(
            "DTSTART and RECURRENCE-ID must use the same value type "
            f"({type(old).__name__} vs {type(new).__name__})."
        )
    if isinstance(old, datetime) and (old.tzinfo is None) != (new.tzinfo is None):
        raise InvalidCalendar(
            "DTSTART and RECURRENCE-ID must both be floating or both be "
            "timezone-aware DATE-TIME values."
        )
    return new - old


def _expand_series(
    master: Event,
    detached: list[Event],
    window_start: datetime,
    window_end: datetime,
    floating_tz: tzinfo | None,
    budget: _CandidateBudget,
) -> list[Occurrence]:
    """Expand one master event together with its detached exceptions."""
    dtstart = master.start
    master_duration = master.duration
    all_day = is_date(dtstart)
    if not all_day and dtstart.tzinfo is None and floating_tz is None:
        raise FloatingTimeError(
            "A floating DTSTART needs an explicit timezone: pass "
            "floating_tz=<tzinfo> to the occurrence query."
        )

    rrules = master.rrules
    if len(rrules) > 1:
        raise UnsupportedRecurrence(
            "Combining multiple RRULE properties on one component is "
            "undefined by RFC 5545 and not supported."
        )

    uid = str(master.get("UID", ""))

    # Recurrence identities keyed by their comparable UTC instant. The mapped
    # value preserves the original value type used as the recurrence id.
    identities: dict[datetime, date] = {}
    period_ends: dict[datetime, date] = {}

    def add_identity(value: date) -> datetime:
        key = _to_comparable(value, floating_tz)
        identities.setdefault(key, value)
        return key

    budget.consume()  # DTSTART is always the first identity
    add_identity(dtstart)

    # Explicit RDATEs are finite, so all of them can be considered, even when
    # their period overlaps the window from far outside.
    for rdate_start, rdate_end in master.rdates:
        if not budget.consume():
            break
        key = add_identity(rdate_start)
        if rdate_end is not None:
            period_ends[key] = rdate_end

    singles: dict[datetime, _SingleOverride] = {}
    shifts: list[_ThisAndFuture] = []
    for exception in detached:
        recurrence_property = exception["RECURRENCE-ID"]
        recurrence_id = recurrence_property.dt
        range_value = recurrence_property.params.get("RANGE")
        if range_value not in (None, "THISANDFUTURE"):
            raise UnsupportedRecurrence(
                f"RECURRENCE-ID;RANGE={range_value} is not supported."
            )
        cancelled = str(exception.get("STATUS", "")).upper() == "CANCELLED"
        key = _to_comparable(recurrence_id, floating_tz)
        if range_value == "THISANDFUTURE":
            shifts.append(
                _ThisAndFuture(
                    identity_key=key,
                    delta=_delta(recurrence_id, exception.start),
                    duration=exception.duration,
                    cancelled=cancelled,
                    component=exception,
                )
            )
        else:
            singles[key] = _SingleOverride(
                start=exception.start,
                duration=exception.duration,
                cancelled=cancelled,
                component=exception,
            )
    shifts.sort(key=lambda shift: shift.identity_key)

    # The rule must also cover instances that overlap the window from before
    # their start (long duration) or that a RANGE=THISANDFUTURE shift moves
    # across a window boundary.
    forward = max([timedelta(0), *(shift.delta for shift in shifts)])
    backward = max([timedelta(0), *(-shift.delta for shift in shifts)])
    rule_low = window_start - master_duration - forward
    rule_high = window_end + backward

    if rrules:
        rrule = _build_rrule(rrules[0], dtstart, floating_tz)
        for generated in rrule:
            if not budget.consume():
                break
            value = generated.date() if all_day else generated
            key = _to_comparable(value, floating_tz)
            if key < rule_low:
                continue
            if key >= rule_high:
                break
            add_identity(value)

    # Detached identities may live outside the bounded rule expansion, e.g. a
    # single instance of an infinite rule moved into the window from far away.
    detached_keys = {*singles, *(shift.identity_key for shift in shifts)}
    for exception in detached:
        key = _to_comparable(exception["RECURRENCE-ID"].dt, floating_tz)
        if key in detached_keys and key not in identities:
            if not budget.consume():
                break
            add_identity(exception["RECURRENCE-ID"].dt)

    # EXDATE wins over RRULE, RDATE and DTSTART.
    for exdate in master.exdates:
        identities.pop(_to_comparable(exdate, floating_tz), None)

    def shift_for(key: datetime) -> _ThisAndFuture | None:
        applicable = None
        for shift in shifts:
            if shift.identity_key <= key:
                applicable = shift
            else:
                break
        return applicable

    occurrences: list[Occurrence] = []
    for key, identity in identities.items():
        start = identity
        duration = master_duration
        explicit_end = period_ends.get(key)
        component = master
        modified = False

        shift = shift_for(key)
        if shift is not None:
            if shift.cancelled:
                # The whole tail of the series is cancelled.
                continue
            start = identity + shift.delta
            duration = shift.duration
            explicit_end = None
            modified = True
            if shift.identity_key == key:
                component = shift.component

        single = singles.get(key)
        if single is not None:
            if single.cancelled:
                continue
            start = single.start
            duration = single.duration
            explicit_end = None
            component = single.component
            modified = True

        end = explicit_end if explicit_end is not None else start + duration

        instance_start = _to_comparable(start, floating_tz)
        instance_end = _to_comparable(end, floating_tz)
        if instance_end < instance_start:
            continue  # broken component with negative duration
        if instance_start == instance_end:
            if not (window_start <= instance_start < window_end):
                continue
        elif not (instance_start < window_end and instance_end > window_start):
            continue

        occurrences.append(
            Occurrence(
                start=start,
                end=end,
                recurrence_id=identity,
                uid=uid,
                component=component,
                modified=modified,
            )
        )
    return occurrences


def occurrences_between(
    events,
    start: date | datetime,
    end: date | datetime,
    *,
    floating_tz: tzinfo | None = None,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
) -> Occurrences:
    """Return the event occurrences intersecting the window ``[start, end)``.

    Parameters:
        events: A :class:`~icalendar.Calendar` or an iterable of
            :class:`~icalendar.Event` components. Detached ``VEVENT``
            components (with ``RECURRENCE-ID``) are matched to their master
            by ``UID``.
        start: The inclusive window start. A ``date`` or an aware
            ``datetime``.
        end: The exclusive window end. Same value type rules as ``start``.
        floating_tz: The timezone used to interpret floating ``DATE-TIME``
            values. Required when any event or the window uses floating time.
            All-day ``DATE`` values are interpreted at midnight in this
            timezone (or UTC when omitted).
        max_candidates: Upper bound for the number of recurrence candidates
            generated while expanding. When the bound is reached, expansion
            stops and the result is marked as incomplete.

    Returns:
        :class:`Occurrences` sorted by actual start. ``result.complete`` is
        ``False`` when the candidate limit was reached and occurrences may
        be missing.

    Raises:
        FloatingTimeError: If a floating ``DATE-TIME`` is encountered
            without ``floating_tz``.
        UnsupportedRecurrence: If a component uses multiple ``RRULE``
            properties or an unsupported ``RANGE`` parameter.
        ValueError: If ``start`` >= ``end`` or ``max_candidates`` < 1.
    """
    if max_candidates < 1:
        raise ValueError("max_candidates must be at least 1.")
    window_start = _window_bound(start, floating_tz)
    window_end = _window_bound(end, floating_tz)
    if window_start >= window_end:
        raise ValueError("The query window start must be before its end.")

    if hasattr(events, "events"):
        events = events.events

    masters: dict[str, Event] = {}
    detached: dict[str, list[Event]] = {}
    order: list[str] = []
    for event in events:
        uid = str(event.get("UID", ""))
        if uid not in masters and uid not in detached:
            order.append(uid)
        if "RECURRENCE-ID" in event:
            detached.setdefault(uid, []).append(event)
        else:
            masters.setdefault(uid, event)

    budget = _CandidateBudget(max_candidates)
    result = Occurrences()
    for uid in order:
        master = masters.get(uid)
        if master is None:
            continue  # detached exceptions without their master
        if str(master.get("STATUS", "")).upper() == "CANCELLED":
            continue  # the whole series is cancelled
        result.extend(
            _expand_series(
                master,
                detached.get(uid, []),
                window_start,
                window_end,
                floating_tz,
                budget,
            )
        )
    result.sort(key=lambda occurrence: _to_comparable(occurrence.start, floating_tz))
    result.complete = not budget.exhausted
    return result


__all__ = [
    "DEFAULT_MAX_CANDIDATES",
    "Occurrence",
    "Occurrences",
    "occurrences_between",
]
