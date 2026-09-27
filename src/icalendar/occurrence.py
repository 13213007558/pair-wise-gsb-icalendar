"""Query the actual occurrences of events within a finite time window.

This module expands the recurrence information of a calendar -
``DTSTART``, ``RRULE``, ``RDATE``, and ``EXDATE`` of a master component
together with its detached exception components (``RECURRENCE-ID``) -
into the instances that actually occur within a given, finite time window.

The main entry points are :func:`occurrences_in_window` and
:meth:`Calendar.occurrences <icalendar.cal.calendar.Calendar.occurrences>`.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from typing import TYPE_CHECKING

from dateutil.rrule import rrulestr

from icalendar.error import InvalidCalendar
from icalendar.timezone import tzp
from icalendar.tools import is_date

if TYPE_CHECKING:
    from collections.abc import Iterable

    from icalendar.cal import Event

#: The default maximum number of candidates generated from a single query's
#: RRULE expansions. Reaching the limit marks the result as incomplete.
DEFAULT_MAX_CANDIDATES = 1000

_THISANDFUTURE = "THISANDFUTURE"


@dataclass
class Occurrence:
    """One actual instance of an event inside a queried time window.

    Attributes:
        recurrence_id: The original recurrence identity of this instance.
            This is the value a ``RECURRENCE-ID`` property would use to
            refer to the instance: the start it would have had according to
            the master's ``DTSTART``/``RRULE``/``RDATE``, before any
            rescheduling. For a non-recurring event this equals its
            ``DTSTART``.
        start: The actual start of this instance, after applying detached
            exception components. A :class:`datetime.date` for all-day
            events, a :class:`datetime.datetime` otherwise.
        end: The actual, non-inclusive end of this instance.
        component: A deep copy of the source component this instance was
            derived from - the master component or a detached exception
            component. Modifying it does not modify the original calendar.
        is_exception: Whether this instance was derived from a detached
            exception component (one with a ``RECURRENCE-ID``).
    """

    recurrence_id: date | datetime
    start: date | datetime
    end: date | datetime
    component: Event
    is_exception: bool = False

    @property
    def duration(self) -> timedelta:
        """The actual duration of this instance."""
        return self.end - self.start


class Occurrences(list):
    """The occurrences found within a time window, sorted by start.

    Attributes:
        incomplete: ``True`` if the candidate limit (``max_candidates``)
            was reached while expanding recurrence rules, in which case the
            result may be missing occurrences.
    """

    def __init__(
        self, occurrences: Iterable[Occurrence] = (), incomplete: bool = False
    ) -> None:
        super().__init__(occurrences)
        self.incomplete = incomplete


def _is_cancelled(component: Event) -> bool:
    """Whether the component is cancelled."""
    status = component.get("STATUS")
    return status is not None and str(status).upper() == "CANCELLED"


def _recurrence_id_range(component: Event) -> str:
    """The RANGE parameter of the component's RECURRENCE-ID, uppercased."""
    recurrence_id = component.get("RECURRENCE-ID")
    if recurrence_id is None:
        return ""
    return str(recurrence_id.params.get("RANGE", "")).upper()


class _Query:
    """Internal state and logic of one occurrence query.

    All values are normalized to an internal representation before any
    comparison happens: all-day events are represented as naive midnight
    datetimes, timed events as timezone-aware datetimes. This keeps
    all-day ``DATE`` values and timezone-aware ``DATE-TIME`` values in
    strictly separate domains.
    """

    def __init__(
        self,
        start: date | datetime,
        end: date | datetime,
        tz: tzinfo | str | None,
        max_candidates: int,
    ) -> None:
        if is_date(start) and is_date(end):
            self.all_day = True
            self._window_start = self._midnight(start)
            self._window_end = self._midnight(end)
        elif isinstance(start, datetime) and isinstance(end, datetime):
            self.all_day = False
            if start.tzinfo is None or end.tzinfo is None:
                if tz is None:
                    raise ValueError(
                        "The query window uses floating datetimes. "
                        "Pass tz= to state the timezone they are in."
                    )
                start = tzp.localize(start, tz)
                end = tzp.localize(end, tz)
            self._window_start = start
            self._window_end = end
        else:
            raise ValueError(
                "The query window must be two dates (all-day query) or "
                "two datetimes (timed query), not a mix of both."
            )
        if not self._window_start < self._window_end:
            raise ValueError("The query window must not be empty: start < end.")
        self.tz = tz
        self.max_candidates = max_candidates
        self._candidates = 0
        self.incomplete = False

    @staticmethod
    def _midnight(value: date) -> datetime:
        return datetime.combine(value, time.min)

    def _to_internal(self, value: date | datetime) -> datetime:
        """Normalize a component value to the internal representation."""
        if self.all_day:
            if isinstance(value, datetime):
                value = value.date()
            return self._midnight(value)
        if is_date(value):
            raise InvalidCalendar(
                "All-day DATE value in a component with DATE-TIME values."
            )
        if value.tzinfo is None:
            if self.tz is None:
                raise ValueError(
                    "The calendar contains floating datetimes. "
                    "Pass tz= to state the timezone to interpret them in."
                )
            return tzp.localize(value, self.tz)
        return value

    def _to_public(self, value: datetime) -> date | datetime:
        """Convert an internal value back to the public representation."""
        return value.date() if self.all_day else value

    def _matches_mode(self, value: date | datetime) -> bool:
        """Whether a component value fits the query's value type."""
        return is_date(value) == self.all_day

    def _intersects(self, start: datetime, end: datetime) -> bool:
        """Whether [start, end) intersects the half-open query window."""
        if end <= start:
            # a zero-duration instant
            return self._window_start <= start < self._window_end
        return start < self._window_end and end > self._window_start

    def run(self, events: Iterable[Event]) -> Occurrences:
        """Group the events by UID and compute the occurrences."""
        groups: dict[str, dict[str, object]] = {}
        singles: list[Event] = []
        for event in events:
            uid = event.get("UID")
            if uid is None:
                singles.append(event)
                continue
            group = groups.setdefault(str(uid), {"master": None, "exceptions": []})
            if event.get("RECURRENCE-ID") is None:
                if group["master"] is None:
                    group["master"] = event
                else:
                    # duplicate master components: treat as singles
                    singles.append(event)
            else:
                group["exceptions"].append(event)
        occurrences: list[Occurrence] = []
        for event in singles:
            occurrences.extend(self._group_occurrences(event, []))
        for group in groups.values():
            occurrences.extend(
                self._group_occurrences(group["master"], group["exceptions"])
            )
        occurrences.sort(
            key=lambda occurrence: (occurrence.start, occurrence.recurrence_id)
        )
        return Occurrences(occurrences, incomplete=self.incomplete)

    def _group_occurrences(
        self, master: Event | None, exceptions: list[Event]
    ) -> list[Occurrence]:
        """Compute the occurrences of one UID group."""
        if master is None:
            return self._masterless_occurrences(exceptions)
        if _is_cancelled(master) or not self._matches_mode(master.start):
            return []
        exceptions = [
            exception
            for exception in exceptions
            if self._matches_mode(exception.RECURRENCE_ID)
        ]
        exact: dict[datetime, Event] = {}
        futures: list[tuple[datetime, Event]] = []
        for exception in exceptions:
            recurrence_id = self._to_internal(exception.RECURRENCE_ID)
            if _recurrence_id_range(exception) == _THISANDFUTURE and not _is_cancelled(
                exception
            ):
                futures.append((recurrence_id, exception))
            else:
                # A cancelled THISANDFUTURE exception is treated as cancelling
                # only its own instance.
                exact[recurrence_id] = exception
        futures.sort(key=lambda item: item[0])

        identities, period_ends = self._expand(master, exceptions, futures)
        exdates = {
            self._to_internal(exdate)
            for exdate in master.exdates
            if self._matches_mode(exdate)
        }
        future_ids = {recurrence_id for recurrence_id, _ in futures}
        master_duration = master.duration

        occurrences = []
        for identity in sorted(identities):
            if identity in exact:
                exception = exact[identity]
                if _is_cancelled(exception):
                    continue
                start = self._to_internal(exception.start)
                end = self._to_internal(exception.end)
                source, is_exception = exception, True
            elif identity in exdates and identity not in future_ids:
                continue
            else:
                future = self._applicable_future(futures, identity)
                if future is not None:
                    recurrence_id, exception = future
                    shift = self._to_internal(exception.start) - recurrence_id
                    start = identity + shift
                    end = start + exception.duration
                    source, is_exception = exception, True
                else:
                    start = identity
                    end = period_ends.get(identity) or identity + master_duration
                    source, is_exception = master, False
            if self._intersects(start, end):
                occurrences.append(
                    self._occurrence(identity, start, end, source, is_exception)
                )
        return occurrences

    @staticmethod
    def _applicable_future(
        futures: list[tuple[datetime, Event]], identity: datetime
    ) -> tuple[datetime, Event] | None:
        """The latest THISANDFUTURE exception applying to an identity.

        The range is applied by the original recurrence identity, not by the
        rescheduled start, so an instance moved to another day still keeps
        its identity and stays governed by the exception.
        """
        applicable = None
        for recurrence_id, exception in futures:
            if recurrence_id <= identity:
                applicable = (recurrence_id, exception)
        return applicable

    def _masterless_occurrences(self, exceptions: list[Event]) -> list[Occurrence]:
        """Occurrences of detached exceptions whose master is not present."""
        occurrences = []
        for exception in exceptions:
            if _is_cancelled(exception) or not self._matches_mode(exception.start):
                continue
            start = self._to_internal(exception.start)
            end = self._to_internal(exception.end)
            if self._intersects(start, end):
                identity = self._to_internal(exception.RECURRENCE_ID)
                occurrences.append(
                    self._occurrence(identity, start, end, exception, True)
                )
        return occurrences

    def _occurrence(
        self,
        identity: datetime,
        start: datetime,
        end: datetime,
        source: Event,
        is_exception: bool,
    ) -> Occurrence:
        """Create an occurrence, copying the source component for safety."""
        return Occurrence(
            recurrence_id=self._to_public(identity),
            start=self._to_public(start),
            end=self._to_public(end),
            component=copy.deepcopy(source),
            is_exception=is_exception,
        )

    def _expand(
        self,
        master: Event,
        exceptions: list[Event],
        futures: list[tuple[datetime, Event]],
    ) -> tuple[set[datetime], dict[datetime, datetime]]:
        """Generate the candidate recurrence identities of the master.

        Returns the candidate identities and the explicit ends of RDATE
        periods. Infinite rules are only expanded within a bounded window
        derived from the query window, the event durations, and the shifts
        of the THISANDFUTURE exceptions.
        """
        rrules = master.rrules
        if len(rrules) > 1:
            raise InvalidCalendar(
                "The recurrence set of multiple RRULE properties is undefined "
                "and not supported by this query."
            )
        dtstart = self._to_internal(master.start)

        durations = [master.duration]
        durations.extend(
            exception.duration
            for exception in exceptions
            if not _is_cancelled(exception)
        )
        period_ends: dict[datetime, datetime] = {}
        rdate_starts: list[datetime] = []
        for rdate_start, rdate_end in master.rdates:
            if not self._matches_mode(rdate_start):
                continue
            rdate_identity = self._to_internal(rdate_start)
            rdate_starts.append(rdate_identity)
            if rdate_end is not None:
                period_ends[rdate_identity] = self._to_internal(rdate_end)
                durations.append(period_ends[rdate_identity] - rdate_identity)
        max_duration = max(durations, default=timedelta(0))

        shifts = [
            self._to_internal(exception.start) - recurrence_id
            for recurrence_id, exception in futures
        ]
        max_shift = max(shifts, default=timedelta(0))
        min_shift = min(shifts, default=timedelta(0))

        # Instances starting before this bound cannot intersect the window,
        # and identities past the upper bound cannot be shifted into it.
        lower = self._window_start - max_duration - max_shift
        upper = self._window_end - min_shift

        identities: set[datetime] = set()
        if rrules:
            rrule_string = rrules[0].to_ical().decode("utf-8")
            rule = rrulestr(rrule_string, dtstart=dtstart)
            for candidate in rule.between(lower, upper, inc=True):
                self._candidates += 1
                if self._candidates > self.max_candidates:
                    self.incomplete = True
                    break
                identities.add(candidate)
        else:
            identities.add(dtstart)
            identities.update(rdate_starts)
        # Detached exceptions can move an instance into the window from
        # outside the expansion bounds, so their identities are candidates
        # no matter where the expansion placed them.
        for exception in exceptions:
            identities.add(self._to_internal(exception.RECURRENCE_ID))
        return identities, period_ends


def occurrences_in_window(
    events: Iterable[Event],
    start: date | datetime,
    end: date | datetime,
    *,
    tz: tzinfo | str | None = None,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
) -> Occurrences:
    """Compute the actual occurrences of events within ``[start, end)``.

    This combines the ``DTSTART``, ``RRULE``, ``RDATE``, and ``EXDATE``
    properties of each master component with its detached exception
    components (same ``UID``, with ``RECURRENCE-ID``) and returns the
    instances that actually occur, each with the ``RECURRENCE-ID`` value
    identifying it.

    Parameters:
        events: The events to consider, for example ``calendar.events``.
        start: The inclusive start of the query window.
        end: The exclusive end of the query window.
        tz: The timezone used to interpret floating (naive) datetimes.
            Required whenever the window or any considered event uses
            floating datetimes. A ``tzinfo`` or a timezone name.
        max_candidates: The maximum number of candidates generated from
            RRULE expansions. Reaching the limit stops the expansion and
            marks the result as :attr:`Occurrences.incomplete`.

    Returns:
        The occurrences intersecting the window, sorted by start.

    The window is half-open: an instance is included if and only if it
    intersects ``[start, end)``. All-day events (``DATE`` values) and timed
    events (``DATE-TIME`` values) are queried separately: pass two
    :class:`datetime.date` objects to query all-day events, or two
    :class:`datetime.datetime` objects to query timed events.

    Detached exceptions with ``STATUS:CANCELLED`` remove their instance.
    ``RECURRENCE-ID;RANGE=THISANDFUTURE`` exceptions reschedule their own
    and all later instances - by start and by duration - based on the
    original recurrence identity, so an instance moved to another day still
    keeps its original ``RECURRENCE-ID``.

    Components with more than one ``RRULE`` property are undefined by
    :rfc:`5545` and raise :exc:`~icalendar.error.InvalidCalendar`.
    """
    return _Query(start, end, tz, max_candidates).run(events)


__all__ = [
    "DEFAULT_MAX_CANDIDATES",
    "Occurrence",
    "Occurrences",
    "occurrences_in_window",
]
