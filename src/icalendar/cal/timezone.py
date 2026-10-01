""":rfc:`5545` components for timezone information."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import TYPE_CHECKING, Literal

import dateutil.rrule
import dateutil.tz

from icalendar.attr import (
    create_single_property,
    exdates_property,
    rdates_property,
    rrules_property,
)
from icalendar.cal.component import Component
from icalendar.cal.examples import get_example
from icalendar.error import (
    AmbiguousLocalTime,
    InsufficientTimezoneData,
    NonExistentLocalTime,
)
from icalendar.prop import vUTCOffset
from icalendar.timezone import TZP, tzp
from icalendar.timezone.tzid import tzid_from_tzinfo
from icalendar.tools import to_datetime

if TYPE_CHECKING:
    from icalendar.cal import Calendar


def _rounded_utc_offset(offset: timedelta) -> timedelta:
    """Round a UTC offset to the next minute.

    We might lose up to 30 seconds accuracy, but it can't be helped
    (datetime supposedly cannot handle smaller offsets).
    """
    seconds = int((offset.seconds + 30) / 60) * 60
    return timedelta(days=offset.days, seconds=seconds)


def _as_naive_utc(dt: datetime) -> datetime:
    """Return the naive UTC representation of dt; naive input is assumed UTC."""
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


FoldPolicy = Literal["earlier", "later", "reject"]
"""How to resolve a local time that occurs twice.

- 'earlier': pick the occurrence that is earlier in UTC.
- 'later': pick the occurrence that is later in UTC.
- 'reject': raise AmbiguousLocalTime with both candidate offsets.
"""

GapPolicy = Literal["shift-forward", "reject"]
"""How to resolve a local time that never occurs.

- 'shift-forward': move the time forward by the size of the gap.
- 'reject': raise NonExistentLocalTime with the gap diagnostic.
"""


@dataclass(frozen=True)
class TimezoneTransition:
    """A single UTC offset transition of a VTIMEZONE component."""

    utc: datetime
    """The aware UTC time at which the transition occurs."""

    offset_from: timedelta
    """The UTC offset in effect before the transition (TZOFFSETFROM)."""

    offset_to: timedelta
    """The UTC offset in effect after the transition (TZOFFSETTO)."""

    tzname: str | None
    """The TZNAME of the observance that starts, or None if not given."""

    is_dst: bool
    """Whether the observance that starts is a DAYLIGHT component."""


@dataclass(frozen=True)
class _Observance:
    """A STANDARD or DAYLIGHT sub-component prepared for computation."""

    offset_from: timedelta
    offset_to: timedelta
    onsets: dateutil.rrule.rruleset
    open_ended: bool
    tzname: str | None
    is_dst: bool


class Timezone(Component):
    """
    A "VTIMEZONE" calendar component is a grouping of component
    properties that defines a time zone. It is used to describe the
    way in which a time zone changes its offset from UTC over time.
    """

    subcomponents: list[TimezoneStandard | TimezoneDaylight]

    name = "VTIMEZONE"
    canonical_order = ("TZID",)
    required = ("TZID",)  # it also requires one of components DAYLIGHT and STANDARD
    singletons = (
        "TZID",
        "LAST-MODIFIED",
        "TZURL",
    )

    DEFAULT_FIRST_DATE = date(1970, 1, 1)
    DEFAULT_LAST_DATE = date(2038, 1, 1)

    @classmethod
    def example(cls, name: str = "pacific_fiji") -> Calendar:
        """Return the timezone example with the given name."""
        return cls.from_ical(get_example("timezones", name))

    @staticmethod
    def _extract_offsets(component: TimezoneDaylight | TimezoneStandard, tzname: str):
        """extract offsets and transition times from a VTIMEZONE component
        :param component: a STANDARD or DAYLIGHT component
        :param tzname: the name of the zone
        """
        offsetfrom = component.TZOFFSETFROM
        offsetto = component.TZOFFSETTO
        dtstart = component.DTSTART

        # offsets need to be rounded to the next minute, we might loose up
        # to 30 seconds accuracy, but it can't be helped (datetime
        # supposedly cannot handle smaller offsets)
        offsetto = _rounded_utc_offset(offsetto)
        offsetfrom = _rounded_utc_offset(offsetfrom)

        # expand recurrences
        if "RRULE" in component:
            # to be paranoid about correct weekdays
            # evaluate the rrule with the current offset
            tzi = dateutil.tz.tzoffset("(offsetfrom)", offsetfrom)
            rrstart = dtstart.replace(tzinfo=tzi)

            rrulestr = component["RRULE"].to_ical().decode("utf-8")
            rrule = dateutil.rrule.rrulestr(rrulestr, dtstart=rrstart)
            tzp.fix_rrule_until(rrule, component["RRULE"])

            # constructing the timezone requires UTC transition times.
            # here we construct local times without tzinfo, the offset to UTC
            # gets subtracted in to_tz().
            transtimes = [dt.replace(tzinfo=None) for dt in rrule]

        # or rdates
        elif "RDATE" in component:
            if not isinstance(component["RDATE"], list):
                rdates = [component["RDATE"]]
            else:
                rdates = component["RDATE"]
            transtimes = [dtstart] + [leaf.dt for tree in rdates for leaf in tree.dts]
        else:
            transtimes = [dtstart]

        transitions = [
            (transtime, offsetfrom, offsetto, tzname) for transtime in set(transtimes)
        ]

        if component.name == "STANDARD":
            is_dst = 0
        elif component.name == "DAYLIGHT":
            is_dst = 1
        return is_dst, transitions

    @staticmethod
    def _make_unique_tzname(tzname, tznames):
        """
        :param tzname: Candidate tzname
        :param tznames: Other tznames
        """
        # TODO better way of making sure tznames are unique
        while tzname in tznames:
            tzname += "_1"
        tznames.add(tzname)
        return tzname

    def to_tz(self, tzp: TZP = tzp, lookup_tzid: bool = True):
        """convert this VTIMEZONE component to a timezone object

        :param tzp: timezone provider to use
        :param lookup_tzid: whether to use the TZID property to look up existing
                            timezone definitions with tzp.
                            If it is False, a new timezone will be created.
                            If it is True, the existing timezone will be used
                            if it exists, otherwise a new timezone will be created.
        """
        if lookup_tzid:
            tz = tzp.timezone(self.tz_name)
            if tz is not None:
                return tz
        return tzp.create_timezone(self)

    @property
    def tz_name(self) -> str:
        """Return the name of the timezone component.

        Please note that the names of the timezone are different from this name
        and may change with winter/summer time.
        """
        try:
            return str(self["TZID"])
        except UnicodeEncodeError:
            return self["TZID"].encode("ascii", "replace")

    def get_transitions(
        self,
    ) -> tuple[list[datetime], list[tuple[timedelta, timedelta, str]]]:
        """Return a tuple of (transition_times, transition_info)

        - transition_times = [datetime, ...]
        - transition_info = [(TZOFFSETTO, dts_offset, tzname)]

        """
        zone = self.tz_name
        transitions = []
        dst = {}
        tznames = set()
        for component in self.walk():
            if isinstance(component, Timezone):
                continue
            try:
                tzname = str(component["TZNAME"])
            except UnicodeEncodeError:
                tzname = component["TZNAME"].encode("ascii", "replace")
                tzname = self._make_unique_tzname(tzname, tznames)
            except KeyError:
                # for whatever reason this is str/unicode
                tzname = (
                    f"{zone}_{component['DTSTART'].to_ical().decode('utf-8')}_"
                    f"{component['TZOFFSETFROM'].to_ical()}_"
                    f"{component['TZOFFSETTO'].to_ical()}"
                )
                tzname = self._make_unique_tzname(tzname, tznames)

            dst[tzname], component_transitions = self._extract_offsets(
                component, tzname
            )
            transitions.extend(component_transitions)

        transitions.sort()
        transition_times = [
            transtime - osfrom for transtime, osfrom, _, _ in transitions
        ]

        # transition_info is a list with tuples in the format
        # (utcoffset, dstoffset, name)
        # dstoffset = 0, if current transition is to standard time
        #           = this_utcoffset - prev_standard_utcoffset, otherwise
        transition_info = []
        for num, (_transtime, osfrom, osto, name) in enumerate(transitions):
            dst_offset = False
            if not dst[name]:
                dst_offset = timedelta(seconds=0)
            else:
                # go back in time until we find a transition to dst
                for index in range(num - 1, -1, -1):
                    if not dst[transitions[index][3]]:  # [3] is the name
                        dst_offset = osto - transitions[index][2]  # [2] is osto
                        break
                # when the first transition is to dst, we didn't find anything
                # in the past, so we have to look into the future
                if not dst_offset:
                    for index in range(num, len(transitions)):
                        if not dst[transitions[index][3]]:  # [3] is the name
                            dst_offset = osto - transitions[index][2]  # [2] is osto
                            break
                # If we still haven't found a STANDARD transition
                # (only DAYLIGHT exists), calculate dst_offset as the
                # difference from TZOFFSETFROM. Handles Issue #321.
                if dst_offset is False:
                    dst_offset = osto - osfrom
            transition_info.append((osto, dst_offset, name))
        return transition_times, transition_info

    #: Margin around a requested instant when collecting transitions.
    #: UTC offsets stay well below one day, so two days are generous.
    _TRANSITION_WINDOW_MARGIN = timedelta(days=2)

    def _observance_components(self):
        """Yield the STANDARD and DAYLIGHT sub-components in order."""
        for component in self.subcomponents:
            if isinstance(component, (TimezoneStandard, TimezoneDaylight)):
                yield component

    @staticmethod
    def _onset_ruleset(
        component: TimezoneStandard | TimezoneDaylight,
        offset_from: timedelta,
    ) -> tuple[dateutil.rrule.rruleset, bool]:
        """Compute the local onset times of a STANDARD or DAYLIGHT component.

        Returns the onset rruleset and whether the observance is open-ended,
        i.e. whether its offset stays in effect indefinitely after its last
        onset. This is the case for an unbounded RRULE and for a bare
        DTSTART without RRULE or RDATE.

        The datetimes in the set carry offset_from as a fixed-offset tzinfo
        so that aware UNTIL values compare correctly; their wall time is
        the local onset time.
        """
        tzi = dateutil.tz.tzoffset(None, offset_from)
        start = to_datetime(component.DTSTART).replace(tzinfo=tzi)
        onsets = dateutil.rrule.rruleset()
        if "RRULE" in component:
            recur = component["RRULE"]
            rule = dateutil.rrule.rrulestr(
                recur.to_ical().decode("utf-8"), dtstart=start
            )
            until = rule._until  # noqa: SLF001
            if until is not None and until.tzinfo is None:
                # a naive UNTIL is a local time, just like DTSTART
                rule._until = until.replace(tzinfo=tzi)  # noqa: SLF001
            onsets.rrule(rule)
            open_ended = "UNTIL" not in recur and "COUNT" not in recur
        else:
            open_ended = "RDATE" not in component
        onsets.rdate(start)  # DTSTART itself is always an onset
        for rdate, _end in component.rdates:
            onsets.rdate(to_datetime(rdate).replace(tzinfo=tzi))
        for exdate in component.exdates:
            onsets.exdate(to_datetime(exdate).replace(tzinfo=tzi))
        return onsets, open_ended

    def _observances(self) -> list[_Observance]:
        """The observances of this VTIMEZONE prepared for computation."""
        observances = []
        for component in self._observance_components():
            offset_from = _rounded_utc_offset(component.TZOFFSETFROM)
            onsets, open_ended = self._onset_ruleset(component, offset_from)
            tzname = component.get("TZNAME")
            observances.append(
                _Observance(
                    offset_from=offset_from,
                    offset_to=_rounded_utc_offset(component.TZOFFSETTO),
                    onsets=onsets,
                    open_ended=open_ended,
                    tzname=None if tzname is None else str(tzname),
                    is_dst=isinstance(component, TimezoneDaylight),
                )
            )
        return observances

    def transitions_between(
        self, start: datetime, end: datetime
    ) -> list[TimezoneTransition]:
        """Compute the transitions with a UTC onset between start and end.

        The transitions are derived only from the STANDARD and DAYLIGHT
        sub-components with their DTSTART, RRULE and RDATE properties;
        the timezone database of the operating system is not consulted.
        Naive datetimes are assumed to be in UTC. The result is sorted
        by UTC onset.
        """
        start_utc = _as_naive_utc(start)
        end_utc = _as_naive_utc(end)
        if end_utc < start_utc:
            raise ValueError(f"end {end!r} must not be before start {start!r}.")
        transitions = []
        for observance in self._observances():
            tzi = dateutil.tz.tzoffset(None, observance.offset_from)
            first = (start_utc + observance.offset_from).replace(tzinfo=tzi)
            last = (end_utc + observance.offset_from).replace(tzinfo=tzi)
            for onset in observance.onsets.between(first, last, inc=True):
                utc_onset = onset.replace(tzinfo=None) - observance.offset_from
                transitions.append(
                    TimezoneTransition(
                        utc=utc_onset.replace(tzinfo=timezone.utc),
                        offset_from=observance.offset_from,
                        offset_to=observance.offset_to,
                        tzname=observance.tzname,
                        is_dst=observance.is_dst,
                    )
                )
        transitions.sort(key=lambda transition: transition.utc)
        return transitions

    def _transitions_around(
        self, observances: list[_Observance], instant_utc: datetime
    ) -> list[tuple[datetime, timedelta, timedelta, bool]]:
        """Collect the transition entries around a naive UTC instant.

        Each entry is (utc_onset, offset_from, offset_to, open_ended).
        The latest onset at or before the window is included so that the
        offset in effect at the start of the window is known.
        """
        window_start = instant_utc - self._TRANSITION_WINDOW_MARGIN
        window_end = instant_utc + self._TRANSITION_WINDOW_MARGIN
        entries = []
        for observance in observances:
            tzi = dateutil.tz.tzoffset(None, observance.offset_from)
            first = (window_start + observance.offset_from).replace(tzinfo=tzi)
            last = (window_end + observance.offset_from).replace(tzinfo=tzi)
            local_onsets = set(observance.onsets.between(first, last, inc=True))
            previous = observance.onsets.before(first, inc=True)
            if previous is not None:
                local_onsets.add(previous)
            for onset in local_onsets:
                utc_onset = onset.replace(tzinfo=None) - observance.offset_from
                entries.append(
                    (
                        utc_onset,
                        observance.offset_from,
                        observance.offset_to,
                        observance.open_ended,
                    )
                )
        entries.sort(key=lambda entry: entry[0])
        return entries

    @staticmethod
    def _offset_at(
        entries: list[tuple[datetime, timedelta, timedelta, bool]],
        utc_instant: datetime,
    ) -> timedelta | None:
        """The UTC offset in effect at utc_instant, or None if unknown."""
        offset = None
        for onset, _offset_from, offset_to, _open_ended in entries:
            if onset > utc_instant:
                break
            offset = offset_to
        return offset

    def _require_coverage(
        self,
        observances: list[_Observance],
        entries: list[tuple[datetime, timedelta, timedelta, bool]],
        utc_instant: datetime,
    ) -> None:
        """Raise InsufficientTimezoneData if the rules say nothing about an instant."""
        latest = None
        for entry in entries:
            if entry[0] > utc_instant:
                break
            latest = entry
        if latest is None:
            raise InsufficientTimezoneData(
                f"The VTIMEZONE {self.tz_name!r} has no transition at or before "
                f"{utc_instant} UTC: its rules do not cover this instant.",
                dt=utc_instant,
                tzid=self.tz_name,
            )
        if latest[3]:
            # the observance of the latest onset is open-ended:
            # its offset stays in effect
            return
        if any(entry[0] > utc_instant for entry in entries):
            return
        # the window is exhausted; ask the observances whether any later
        # onset exists at all
        for observance in observances:
            tzi = dateutil.tz.tzoffset(None, observance.offset_from)
            later = observance.onsets.after(
                (utc_instant + observance.offset_from).replace(tzinfo=tzi),
                inc=True,
            )
            if later is not None:
                return
        raise InsufficientTimezoneData(
            f"The rules of the VTIMEZONE {self.tz_name!r} end before "
            f"{utc_instant} UTC and do not cover this instant.",
            dt=utc_instant,
            tzid=self.tz_name,
        )

    def utc_offset_at(self, dt: datetime) -> timedelta:
        """Return the UTC offset in effect at the UTC instant dt.

        A naive dt is assumed to be in UTC. The offset is derived only
        from this component; the timezone database is not consulted.

        Raises:
            InsufficientTimezoneData: if the rules of this VTIMEZONE
                do not cover the instant.
        """
        instant = _as_naive_utc(dt)
        observances = self._observances()
        entries = self._transitions_around(observances, instant)
        self._require_coverage(observances, entries, instant)
        offset = self._offset_at(entries, instant)
        if offset is None:  # pragma: no cover - guarded by _require_coverage
            raise InsufficientTimezoneData(
                f"The VTIMEZONE {self.tz_name!r} does not define an offset "
                f"for {instant} UTC.",
                dt=instant,
                tzid=self.tz_name,
            )
        return offset

    @staticmethod
    def _candidate_offsets(
        entries: list[tuple[datetime, timedelta, timedelta, bool]],
        local: datetime,
    ) -> list[timedelta]:
        """The offsets with which the naive local time occurs."""
        candidates = []
        for _onset, _offset_from, offset_to, _open_ended in entries:
            if offset_to in candidates:
                continue
            if Timezone._offset_at(entries, local - offset_to) == offset_to:
                candidates.append(offset_to)
        return candidates

    def offsets_for_local(self, dt: datetime) -> tuple[timedelta, ...]:
        """Return the UTC offsets with which the naive local time dt occurs.

        - one offset: the local time is unambiguous
        - two offsets: the local time occurs twice (clocks are turned back)
        - no offsets: the local time never occurs (clocks are turned forward)

        Raises:
            InsufficientTimezoneData: if the rules of this VTIMEZONE
                do not cover the local time.
        """
        if dt.tzinfo is not None:
            raise ValueError(f"dt must be a naive local time, got {dt!r}.")
        observances = self._observances()
        entries = self._transitions_around(observances, dt)
        self._require_coverage(observances, entries, dt)
        return tuple(self._candidate_offsets(entries, dt))

    @staticmethod
    def _enclosing_gap(
        entries: list[tuple[datetime, timedelta, timedelta, bool]],
        local: datetime,
    ) -> tuple[datetime, timedelta, timedelta]:
        """Find the forward transition whose skipped interval contains the time.

        Returns (utc_onset, offset_from, offset_to). If no transition's gap
        contains the time, the nearest forward transition is used.
        """
        forward = [
            (onset, offset_from, offset_to)
            for onset, offset_from, offset_to, _open_ended in entries
            if offset_to > offset_from
        ]
        for onset, offset_from, offset_to in forward:
            if onset + offset_from <= local < onset + offset_to:
                return onset, offset_from, offset_to
        if forward:
            return min(
                forward,
                key=lambda transition: abs(transition[0] + transition[1] - local),
            )
        raise InsufficientTimezoneData(
            f"No forward transition explains the missing local time {local!r}.",
            dt=local,
        )

    def local_to_utc(
        self,
        dt: datetime,
        *,
        fold: FoldPolicy | None = None,
        gap: GapPolicy = "shift-forward",
    ) -> datetime:
        """Convert a naive local time of this VTIMEZONE to an aware UTC datetime.

        Parameters:
            dt: the naive local wall-clock time.
            fold: how to handle a local time that occurs twice when clocks
                are turned back: 'earlier', 'later' or 'reject'.
                If None, the fold attribute of dt is used as in PEP 495:
                fold=0 picks the earlier, fold=1 the later occurrence.
            gap: how to handle a local time that never occurs when clocks
                are turned forward: 'shift-forward' moves the time forward
                by the size of the gap, 'reject' raises an error.

        Raises:
            AmbiguousLocalTime: a repeated local time with fold='reject'.
            NonExistentLocalTime: a skipped local time with gap='reject'.
            InsufficientTimezoneData: the rules do not cover the local time.
        """
        if dt.tzinfo is not None:
            raise ValueError(f"dt must be a naive local time, got {dt!r}.")
        if fold not in (None, "earlier", "later", "reject"):
            raise ValueError(
                f"fold must be None, 'earlier', 'later' or 'reject', got {fold!r}."
            )
        if gap not in ("shift-forward", "reject"):
            raise ValueError(f"gap must be 'shift-forward' or 'reject', got {gap!r}.")
        observances = self._observances()
        entries = self._transitions_around(observances, dt)
        self._require_coverage(observances, entries, dt)
        candidates = self._candidate_offsets(entries, dt)
        if len(candidates) == 1:
            return (dt - candidates[0]).replace(tzinfo=timezone.utc)
        if candidates:
            policy = fold
            if policy is None:
                # PEP 495: fold=0 is the earlier, fold=1 the later occurrence
                policy = "later" if dt.fold else "earlier"
            if policy == "reject":
                raise AmbiguousLocalTime(dt, candidates)
            instants = sorted(dt - offset for offset in candidates)
            chosen = instants[0] if policy == "earlier" else instants[-1]
            return chosen.replace(tzinfo=timezone.utc)
        _onset, offset_from, offset_to = self._enclosing_gap(entries, dt)
        if gap == "reject":
            raise NonExistentLocalTime(dt, offset_from, offset_to)
        shifted = dt + (offset_to - offset_from)
        return (shifted - offset_to).replace(tzinfo=timezone.utc)

    def utc_to_local(self, dt: datetime) -> datetime:
        """Convert a UTC instant to the naive local time of this VTIMEZONE.

        A naive dt is assumed to be in UTC. If the local time occurs twice,
        the fold attribute of the result distinguishes the occurrences, so
        that local_to_utc(utc_to_local(dt)) == dt.

        Raises:
            InsufficientTimezoneData: if the rules of this VTIMEZONE
                do not cover the instant.
        """
        instant = _as_naive_utc(dt)
        offset = self.utc_offset_at(instant)
        local = instant + offset
        candidates = self.offsets_for_local(local)
        if len(candidates) > 1:
            instants = sorted(local - candidate for candidate in candidates)
            local = local.replace(fold=min(instants.index(instant), 1))
        return local

    # binary search
    _from_tzinfo_skip_search = [
        timedelta(days=days) for days in (64, 32, 16, 8, 4, 2, 1)
    ] + [
        # we know it happens in the night usually around 1am
        timedelta(hours=4),
        timedelta(hours=1),
        # adding some minutes and seconds for faster search
        timedelta(minutes=20),
        timedelta(minutes=5),
        timedelta(minutes=1),
        timedelta(seconds=20),
        timedelta(seconds=5),
        timedelta(seconds=1),
    ]

    @classmethod
    def from_tzinfo(
        cls,
        timezone: tzinfo,
        tzid: str | None = None,
        first_date: date = DEFAULT_FIRST_DATE,
        last_date: date = DEFAULT_LAST_DATE,
    ) -> Timezone:
        """Return a VTIMEZONE component from a timezone object.

        This works with pytz and zoneinfo and any other timezone.
        The offsets are calculated from the tzinfo object.

        Parameters:

        :param tzinfo: the timezone object
        :param tzid: the tzid for this timezone. If None, it will be extracted from the tzinfo.
        :param first_date: a datetime that is earlier than anything that happens in the calendar
        :param last_date: a datetime that is later than anything that happens in the calendar
        :raises ValueError: If we have no tzid and cannot extract one.

        .. note::
            This can take some time. Please cache the results.
        """
        if tzid is None:
            tzid = tzid_from_tzinfo(timezone)
            if tzid is None:
                raise ValueError(
                    f"Cannot get TZID from {timezone}. Please set the tzid parameter."
                )
        normalize = getattr(timezone, "normalize", lambda dt: dt)  # pytz compatibility
        first_datetime = datetime(first_date.year, first_date.month, first_date.day)
        last_datetime = datetime(last_date.year, last_date.month, last_date.day)
        if hasattr(timezone, "localize"):  # pytz compatibility
            first_datetime = timezone.localize(first_datetime)
            last_datetime = timezone.localize(last_datetime)
        else:
            first_datetime = first_datetime.replace(tzinfo=timezone)
            last_datetime = last_datetime.replace(tzinfo=timezone)
        # from, to, tzname, is_standard -> start
        offsets: dict[tuple[timedelta | None, timedelta, str, bool], list[datetime]] = (
            defaultdict(list)
        )
        start = first_datetime
        offset_to = None
        while start < last_datetime:
            offset_from = offset_to
            end = start
            offset_to = end.utcoffset()
            for add_offset in cls._from_tzinfo_skip_search:
                last_end = end  # we need to save this as we might be left and right of the time change
                end = normalize(end + add_offset)
                try:
                    while end.utcoffset() == offset_to:
                        last_end = end
                        end = normalize(end + add_offset)
                except OverflowError:
                    # zoninfo does not go all the way
                    break
                # retract if we overshoot
                end = last_end
            # Now, start (inclusive) -> end (exclusive) are one timezone
            is_standard = start.dst() == timedelta()
            name = start.tzname()
            if name is None:
                name = str(offset_to)
            key = (offset_from, offset_to, name, is_standard)
            # first_key = (None,) + key[1:]
            # if first_key in offsets:
            #     # remove the first one and claim it changes at that day
            #     offsets[first_key] = offsets.pop(first_key)
            offsets[key].append(start.replace(tzinfo=None))
            start = normalize(end + cls._from_tzinfo_skip_search[-1])
        tz = cls()
        tz.add("TZID", tzid)
        tz.add("COMMENT", f"This timezone only works from {first_date} to {last_date}.")
        for (offset_from, offset_to, tzname, is_standard), starts in offsets.items():
            first_start = min(starts)
            starts.remove(first_start)
            if first_start.date() == last_date:
                first_start = datetime(last_date.year, last_date.month, last_date.day)
            subcomponent = TimezoneStandard() if is_standard else TimezoneDaylight()
            if offset_from is None:
                offset_from = offset_to
            subcomponent.TZOFFSETFROM = offset_from
            subcomponent.TZOFFSETTO = offset_to
            subcomponent.add("TZNAME", tzname)
            subcomponent.DTSTART = first_start
            if starts:
                subcomponent.add("RDATE", starts)
            tz.add_component(subcomponent)
        return tz

    @classmethod
    def from_tzid(
        cls,
        tzid: str,
        tzp: TZP = tzp,
        first_date: date = DEFAULT_FIRST_DATE,
        last_date: date = DEFAULT_LAST_DATE,
    ) -> Timezone:
        """Create a VTIMEZONE from a tzid like ``"Europe/Berlin"``.

        :param tzid: the id of the timezone
        :param tzp: the timezone provider
        :param first_date: a datetime that is earlier than anything
            that happens in the calendar
        :param last_date: a datetime that is later than anything
            that happens in the calendar
        :raises ValueError: If the tzid is unknown.

        >>> from icalendar import Timezone
        >>> tz = Timezone.from_tzid("Europe/Berlin")
        >>> print(tz.to_ical()[:36])
        BEGIN:VTIMEZONE
        TZID:Europe/Berlin

        .. note::
            This can take some time. Please cache the results.
        """
        tz = tzp.timezone(tzid)
        if tz is None:
            raise ValueError(f"Unkown timezone {tzid}.")
        return cls.from_tzinfo(tz, tzid, first_date, last_date)

    @property
    def standard(self) -> list[TimezoneStandard]:
        """The STANDARD subcomponents as a list."""
        return self.walk("STANDARD")

    @property
    def daylight(self) -> list[TimezoneDaylight]:
        """The DAYLIGHT subcomponents as a list.

        These are for the daylight saving time.
        """
        return self.walk("DAYLIGHT")


class TimezoneStandard(Component):
    """
    The "STANDARD" sub-component of "VTIMEZONE" defines the standard
    time offset from UTC for a time zone. It represents a time zone's
    standard time, typically used during winter months in locations
    that observe Daylight Saving Time.
    """

    name = "STANDARD"
    required = ("DTSTART", "TZOFFSETTO", "TZOFFSETFROM")
    singletons = (
        "DTSTART",
        "TZOFFSETTO",
        "TZOFFSETFROM",
    )
    multiple = ("COMMENT", "RDATE", "TZNAME", "RRULE", "EXDATE")

    DTSTART = create_single_property(
        "DTSTART",
        "dt",
        (datetime,),
        datetime,
        """The mandatory "DTSTART" property gives the effective onset date
        and local time for the time zone sub-component definition.
        "DTSTART" in this usage MUST be specified as a date with a local
        time value.""",
        convert=to_datetime,
    )

    TZOFFSETTO = create_single_property(
        "TZOFFSETTO",
        "td",
        (timedelta,),
        timedelta,
        """The mandatory "TZOFFSETTO" property gives the UTC offset for the
        time zone sub-component (Standard Time or Daylight Saving Time)
        when this observance is in use.
        """,
        vUTCOffset,
    )
    TZOFFSETFROM = create_single_property(
        "TZOFFSETFROM",
        "td",
        (timedelta,),
        timedelta,
        """The mandatory "TZOFFSETFROM" property gives the UTC offset that is
        in use when the onset of this time zone observance begins.
        "TZOFFSETFROM" is combined with "DTSTART" to define the effective
        onset for the time zone sub-component definition.  For example,
        the following represents the time at which the observance of
        Standard Time took effect in Fall 1967 for New York City:

            DTSTART:19671029T020000
            TZOFFSETFROM:-0400
        """,
        vUTCOffset,
    )
    rdates = rdates_property
    exdates = exdates_property
    rrules = rrules_property


class TimezoneDaylight(Component):
    """
    The "DAYLIGHT" sub-component of "VTIMEZONE" defines the daylight
    saving time offset from UTC for a time zone. It represents a time
    zone's daylight saving time, typically used during summer months
    in locations that observe Daylight Saving Time.
    """

    name = "DAYLIGHT"
    required = TimezoneStandard.required
    singletons = TimezoneStandard.singletons
    multiple = TimezoneStandard.multiple

    DTSTART = TimezoneStandard.DTSTART
    TZOFFSETTO = TimezoneStandard.TZOFFSETTO
    TZOFFSETFROM = TimezoneStandard.TZOFFSETFROM

    rdates = rdates_property
    exdates = exdates_property
    rrules = rrules_property


__all__ = [
    "FoldPolicy",
    "GapPolicy",
    "Timezone",
    "TimezoneDaylight",
    "TimezoneStandard",
    "TimezoneTransition",
]
