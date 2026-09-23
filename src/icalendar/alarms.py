"""Compute the times and states of alarms.

This takes different calendar software into account and the RFC 9074 (Alarm Extension).

- RFC 9074 defines an ACKNOWLEDGED property in the VALARM.
- Outlook does not export VALARM information.
- Google Calendar uses the DTSTAMP to acknowledge the alarms.
- Thunderbird snoozes the alarms with a X-MOZ-SNOOZE-TIME attribute in the event.
- Thunderbird acknowledges the alarms with a X-MOZ-LASTACK attribute in the event.
- Etar deletes alarms that are acknowledged.
- Nextcloud's Webinterface does not do anything with the alarms when the time passes.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import TYPE_CHECKING, overload

from icalendar.cal.event import Event
from icalendar.cal.todo import Todo
from icalendar.error import (
    ComponentEndMissing,
    ComponentStartMissing,
    IncompleteAlarmInformation,
    InvalidCalendar,
    LocalTimezoneMissing,
)
from icalendar.timezone import tzp
from icalendar.tools import is_date, normalize_pytz, to_datetime

if TYPE_CHECKING:
    from collections.abc import Generator, Iterable

    from icalendar.cal.alarm import Alarm
    from icalendar.prop import vBinary, vUri

Parent = Event | Todo


class AlarmTime:
    """Represents a computed alarm occurrence with its timing and state.

    An AlarmTime instance combines an alarm component with its resolved
    trigger time and additional state information, such as acknowledgment
    and snoozing.
    """

    def __init__(
        self,
        alarm: Alarm,
        trigger: datetime,
        acknowledged_until: datetime | None = None,
        snoozed_until: datetime | None = None,
        parent: Parent | None = None,
    ) -> None:
        """Create an instance of ``AlarmTime`` with any of its parameters.

        Parameters:
            alarm: The underlying alarm component.
            trigger: A date or datetime at which to trigger the alarm.
            acknowledged_until: Optional datetime in UTC until which
                the alarm has been acknowledged.
            snoozed_until: Optional datetime in UTC until which
                the alarm has been snoozed.
            parent: Optional parent component to which the alarm refers.
        """
        self._alarm = alarm
        self._parent = parent
        self._trigger = trigger
        self._last_ack = acknowledged_until
        self._snooze_until = snoozed_until

    @property
    def acknowledged(self) -> datetime | None:
        """The time in UTC at which this alarm was last acknowledged.

        If the alarm was not acknowledged (dismissed), then this is None.
        """
        ack = self.alarm.ACKNOWLEDGED
        if ack is None:
            return self._last_ack
        if self._last_ack is None:
            return ack
        return max(ack, self._last_ack)

    @property
    def alarm(self) -> Alarm:
        """The alarm component."""
        return self._alarm

    @property
    def action(self) -> str:
        """The action invoked when this alarm triggers.

        This delegates to :attr:`Alarm.ACTION <icalendar.cal.alarm.Alarm.ACTION>`.
        """
        return self.alarm.ACTION

    @property
    def uid(self) -> str:
        """The persistent, globally unique identifier of this alarm.

        This delegates to :attr:`Alarm.uid <icalendar.cal.alarm.Alarm.uid>`.
        """
        return self.alarm.uid

    @property
    def summary(self) -> str | None:
        """The short summary or subject for this alarm.

        This delegates to :attr:`Alarm.summary <icalendar.cal.alarm.Alarm.summary>`.
        """
        return self.alarm.summary

    @property
    def description(self) -> str | None:
        """A more complete description of the alarm than that provided by the
        SUMMARY property.

        This delegates to :attr:`Alarm.description
        <icalendar.cal.alarm.Alarm.description>`.
        """
        return self.alarm.description

    @property
    def attendees(self) -> list[str]:
        """List of email addresses to notify when this alarm is triggered.

        This delegates to :attr:`Alarm.attendees
        <icalendar.cal.alarm.Alarm.attendees>`.
        """
        return self.alarm.attendees

    @property
    def attachments(self) -> list[vUri | vBinary]:
        """The attachments of this alarm.

        This delegates to :attr:`Alarm.attachments
        <icalendar.cal.alarm.Alarm.attachments>`.
        """
        return self.alarm.attachments

    @property
    def parent(self) -> Parent | None:
        """The component that contains the alarm.

        This is ``None`` if you didn't use :meth:`Alarms.add_component()
        <icalendar.alarms.Alarms.add_component>`.
        """
        return self._parent

    def is_active(self) -> bool:
        """Whether this alarm is active (``True``) or acknowledged (``False``).

        For example, in some calendar software, this is ``True`` until the user
        views the alarm message and dismisses it.

        Alarms can be in local time without a timezone. To calculate whether
        the alarm has occurred, the time must include timezone information.

        Raises:
            LocalTimezoneMissing: If a timezone is required but not given.
        """
        acknowledged = self.acknowledged
        if not acknowledged:
            return True
        if self._snooze_until is not None and self._snooze_until > acknowledged:
            return True
        trigger = self.trigger
        if trigger.tzinfo is None:
            raise LocalTimezoneMissing(
                "A local timezone is required to check if the alarm is still active. "
                "Use Alarms.set_local_timezone()."
            )
        return trigger > acknowledged

    @property
    def trigger(self) -> date:
        """The time at which the alarm triggers.

        If the alarm has been snoozed, this may differ from the TRIGGER property.
        """
        if self._snooze_until is not None and self._snooze_until > self._trigger:
            return self._snooze_until
        return self._trigger


_EARTH_RADIUS_METERS = 6_371_000.0

_GEO_URI_RE = re.compile(
    r"^geo:(?P<lat>[+-]?\d+(?:\.\d+)?),(?P<lon>[+-]?\d+(?:\.\d+)?)"
    r"(?:,[+-]?\d+(?:\.\d+)?)?(?P<params>(?:;[^;]*)*)$",
    re.IGNORECASE,
)


def _haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """The great-circle distance between two coordinates in meters."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_METERS * math.asin(math.sqrt(a))


@dataclass(frozen=True)
class LocationEvent:
    """A single external location sample used for proximity computation.

    The alarm computation does not do GPS or network positioning itself.
    Instead, callers feed a stream of these samples into
    :meth:`Alarms.proximity_times <icalendar.alarms.Alarms.proximity_times>`.

    Parameters:
        timestamp: The timezone-aware time at which the sample was taken.
        latitude: The latitude in degrees, between -90 and 90.
        longitude: The longitude in degrees, between -180 and 180.
    """

    timestamp: datetime
    latitude: float
    longitude: float

    def __post_init__(self) -> None:
        """Validate the sample."""
        if self.timestamp.tzinfo is None:
            raise LocalTimezoneMissing(
                "A location event needs a timezone-aware timestamp. "
                "Naive timestamps cannot be ordered reliably."
            )
        if not -90 <= self.latitude <= 90:
            raise ValueError(f"Latitude out of range: {self.latitude}")
        if not -180 <= self.longitude <= 180:
            raise ValueError(f"Longitude out of range: {self.longitude}")


class Proximity:
    """The parsed proximity definition of a VALARM, see :rfc:`9074`.

    A proximity alarm carries a ``PROXIMITY`` property (``ARRIVE`` or
    ``DEPART``) and a ``VLOCATION`` subcomponent whose ``URL`` is a
    ``geo:`` URI (:rfc:`5870`). The ``;u=`` uncertainty parameter of the
    URI is the radius in meters within which the location counts as
    "at" the target.

    Use :meth:`from_alarm` to create an instance from an alarm component.
    """

    ARRIVE = "ARRIVE"
    DEPART = "DEPART"
    DIRECTIONS = (ARRIVE, DEPART)

    def __init__(
        self,
        direction: str,
        latitude: float,
        longitude: float,
        radius: float,
        name: str | None = None,
        location_uid: str | None = None,
    ) -> None:
        """Create a proximity definition.

        Parameters:
            direction: ``ARRIVE`` or ``DEPART``.
            latitude: The latitude of the target location in degrees.
            longitude: The longitude of the target location in degrees.
            radius: The radius in meters within which the target counts
                as reached. Must be positive.
            name: The optional name of the ``VLOCATION``.
            location_uid: The optional UID of the ``VLOCATION``.

        Raises:
            ~icalendar.error.InvalidCalendar: If the direction is unknown
                or the radius is not a positive number.
        """
        direction = direction.upper()
        if direction not in self.DIRECTIONS:
            raise InvalidCalendar(
                f"Unknown PROXIMITY value: {direction!r}. "
                f"Expected one of {self.DIRECTIONS}."
            )
        if not math.isfinite(radius) or radius <= 0:
            raise InvalidCalendar(
                "The proximity radius must be a positive number of meters, "
                f"got {radius!r}."
            )
        self.direction = direction
        self.latitude = latitude
        self.longitude = longitude
        self.radius = radius
        self.name = name
        self.location_uid = location_uid

    def __repr__(self) -> str:
        """A debug representation."""
        return (
            f"Proximity({self.direction}, {self.latitude}, {self.longitude}, "
            f"radius={self.radius})"
        )

    def __eq__(self, other: object) -> bool:
        """Two proximity definitions are equal if all attributes match."""
        if not isinstance(other, Proximity):
            return NotImplemented
        return vars(self) == vars(other)

    def contains(self, latitude: float, longitude: float) -> bool:
        """Whether the coordinate is within the radius of the target."""
        return (
            _haversine_meters(self.latitude, self.longitude, latitude, longitude)
            <= self.radius
        )

    @classmethod
    def from_alarm(cls, alarm: Alarm) -> Proximity | None:
        """Parse the proximity definition of an alarm component.

        Returns ``None`` if the alarm has no ``PROXIMITY`` property and is
        therefore a purely time-based alarm.

        Raises:
            ~icalendar.error.InvalidCalendar: If the alarm has a
                ``PROXIMITY`` property but no usable ``VLOCATION`` with a
                ``geo:`` URL, or the radius is missing or invalid.
        """
        raw = alarm.get("PROXIMITY")
        if raw is None:
            return None
        direction = str(raw).upper()
        for sub in alarm.subcomponents:
            if sub.name != "VLOCATION":
                continue
            url = sub.get("URL")
            if url is None:
                continue
            latitude, longitude, radius = cls._parse_geo_uri(str(url))
            name = sub.get("NAME")
            uid = sub.get("UID")
            return cls(
                direction,
                latitude,
                longitude,
                radius,
                name=str(name) if name is not None else None,
                location_uid=str(uid) if uid is not None else None,
            )
        raise InvalidCalendar(
            "A proximity alarm requires a VLOCATION subcomponent with a "
            f"geo: URL. Alarm {alarm.uid!r} has none."
        )

    @staticmethod
    def _parse_geo_uri(uri: str) -> tuple[float, float, float]:
        """Parse a ``geo:`` URI into latitude, longitude and radius."""
        match = _GEO_URI_RE.match(uri.strip())
        if match is None:
            raise InvalidCalendar(f"The VLOCATION URL is not a valid geo: URI: {uri!r}")
        latitude = float(match.group("lat"))
        longitude = float(match.group("lon"))
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise InvalidCalendar(f"Coordinates out of range in geo: URI: {uri!r}")
        params = {}
        for param in match.group("params").split(";"):
            key, _, value = param.partition("=")
            if key:
                params[key.strip().lower()] = value.strip()
        raw_radius = params.get("u")
        if raw_radius is None:
            raise InvalidCalendar(
                "The geo: URI of a proximity alarm must carry a radius as "
                "uncertainty parameter, e.g. 'geo:40.4,-79.9;u=10':"
                f" {uri!r}"
            )
        try:
            radius = float(raw_radius)
        except ValueError:
            raise InvalidCalendar(
                f"Invalid proximity radius in geo: URI: {uri!r}"
            ) from None
        return latitude, longitude, radius


class ProximityTrigger(AlarmTime):
    """A computed proximity alarm occurrence with an explainable origin.

    Each record states which :class:`LocationEvent` caused the trigger,
    which repetition of the alarm it is, and the rule that produced it.
    """

    def __init__(
        self,
        alarm: Alarm,
        trigger: datetime,
        location_event: LocationEvent,
        repetition: int,
        rule: str,
        now: datetime,
        acknowledged_until: datetime | None = None,
        snoozed_until: datetime | None = None,
        parent: Parent | None = None,
    ) -> None:
        """Create a proximity trigger record.

        Parameters:
            alarm: The underlying alarm component.
            trigger: The time at which this occurrence triggers.
            location_event: The location sample that caused the trigger.
            repetition: The 0-based repetition index of this occurrence.
            rule: A human-readable description of the rule that fired.
            now: The reference time used to decide whether this occurrence
                is still pending.
            acknowledged_until: Optional datetime in UTC until which
                the alarm has been acknowledged.
            snoozed_until: Optional datetime in UTC until which
                the alarm has been snoozed.
            parent: Optional parent component to which the alarm refers.
        """
        super().__init__(alarm, trigger, acknowledged_until, snoozed_until, parent)
        self._location_event = location_event
        self._repetition = repetition
        self._rule = rule
        self._pending = self.trigger > now

    @property
    def location_event(self) -> LocationEvent:
        """The location sample that caused this trigger."""
        return self._location_event

    @property
    def repetition(self) -> int:
        """The 0-based repetition index of this occurrence.

        ``0`` is the initial trigger, higher values are repetitions
        created by ``REPEAT`` and ``DURATION``.
        """
        return self._repetition

    @property
    def rule(self) -> str:
        """A human-readable description of the rule that produced this."""
        return self._rule

    @property
    def pending(self) -> bool:
        """Whether this occurrence lies in the future of the reference time."""
        return self._pending

    def explain(self) -> str:
        """Explain why this trigger exists in one sentence."""
        event = self._location_event
        return (
            f"{self._rule}; caused by the location event at "
            f"{event.timestamp.isoformat()} "
            f"({event.latitude}, {event.longitude}), "
            f"repetition {self._repetition}"
        )


class Alarms:
    """Compute the times and states of alarms.

    This is an example using RFC 9074.
    One alarm is 30 minutes before the event and acknowledged.
    Another alarm is 15 minutes before the event and still active.

    >>> from icalendar import Event, Alarms
    >>> event = Event.from_ical(
    ... '''BEGIN:VEVENT
    ... CREATED:20210301T151004Z
    ... UID:AC67C078-CED3-4BF5-9726-832C3749F627
    ... DTSTAMP:20210301T151004Z
    ... DTSTART;TZID=America/New_York:20210302T103000
    ... DTEND;TZID=America/New_York:20210302T113000
    ... SUMMARY:Meeting
    ... BEGIN:VALARM
    ... UID:8297C37D-BA2D-4476-91AE-C1EAA364F8E1
    ... TRIGGER:-PT30M
    ... ACKNOWLEDGED:20210302T150004Z
    ... DESCRIPTION:Event reminder
    ... ACTION:DISPLAY
    ... END:VALARM
    ... BEGIN:VALARM
    ... UID:8297C37D-BA2D-4476-91AE-C1EAA364F8E1
    ... TRIGGER:-PT15M
    ... DESCRIPTION:Event reminder
    ... ACTION:DISPLAY
    ... END:VALARM
    ... END:VEVENT
    ... ''')
    >>> alarms = Alarms(event)
    >>> len(alarms.times)   # all alarms including those acknowledged
    2
    >>> len(alarms.active)  # the alarms that are not acknowledged, yet
    1
    >>> alarms.active[0].trigger  # this alarm triggers 15 minutes before 10:30
    datetime.datetime(2021, 3, 2, 10, 15, tzinfo=ZoneInfo(key='America/New_York'))

    RFC 9074 specifies that alarms can also be triggered by proximity.
    Use :meth:`proximity_times` with a stream of external
    :class:`LocationEvent` samples to compute those:

    >>> from icalendar.alarms import LocationEvent
    >>> from datetime import datetime, timezone
    >>> proximity_event = Event.from_ical(
    ... '''BEGIN:VEVENT
    ... UID:proximity-example
    ... DTSTAMP:20210301T151004Z
    ... DTSTART:20210302T103000Z
    ... DTEND:20210302T113000Z
    ... SUMMARY:Meeting
    ... LOCATION:Meeting room 1
    ... BEGIN:VALARM
    ... UID:alarm-1
    ... ACTION:DISPLAY
    ... TRIGGER:-PT15M
    ... PROXIMITY:ARRIVE
    ... DESCRIPTION:You are close to the meeting room
    ... BEGIN:VLOCATION
    ... UID:location-1
    ... NAME:Meeting room 1
    ... URL:geo:40.443,-79.945;u=20
    ... END:VLOCATION
    ... END:VALARM
    ... END:VEVENT
    ... ''')
    >>> alarms = Alarms(proximity_event)
    >>> now = datetime(2021, 3, 2, 10, 0, tzinfo=timezone.utc)
    >>> samples = [
    ...     LocationEvent(datetime(2021, 3, 2, 9, 50, tzinfo=timezone.utc),
    ...                   40.500, -79.945),  # far away
    ...     LocationEvent(datetime(2021, 3, 2, 9, 55, tzinfo=timezone.utc),
    ...                   40.443, -79.945),  # arrived
    ... ]
    >>> triggers = alarms.proximity_times(samples, now)
    >>> triggers[0].explain().split(";")[0]
    "ARRIVE within 20 m of 'Meeting room 1' (geo:40.443,-79.945)"
    """

    def __init__(self, component: Alarm | Event | Todo | None = None) -> None:
        """Start computing alarm times."""
        self._absolute_alarms: list[Alarm] = []
        self._start_alarms: list[Alarm] = []
        self._end_alarms: list[Alarm] = []
        self._proximity_alarms: list[Alarm] = []
        self._start: date | None = None
        self._end: date | None = None
        self._parent: Parent | None = None
        self._last_ack: datetime | None = None
        self._snooze_until: datetime | None = None
        self._local_tzinfo: tzinfo | None = None

        if component is not None:
            self.add_component(component)

    def add_component(self, component: Alarm | Parent) -> None:
        """Add a component.

        If this is an alarm, it is added.
        Events and Todos are added as a parent and all
        their alarms are added, too.
        """
        if isinstance(component, (Event, Todo)):
            self.set_parent(component)
            self.set_start(component.start)
            self.set_end(component.end)
            if component.is_thunderbird():
                self.acknowledge_until(component.X_MOZ_LASTACK)
                self.snooze_until(component.X_MOZ_SNOOZE_TIME)
            else:
                self.acknowledge_until(component.DTSTAMP)

        for alarm in component.walk("VALARM"):
            self.add_alarm(alarm)

    def set_parent(self, parent: Parent) -> None:
        """Set the parent of all the alarms.

        If you would like to collect alarms from a component, use add_component
        """
        if self._parent is not None and self._parent is not parent:
            raise ValueError("You can only set one parent for this alarm calculation.")
        self._parent = parent

    def add_alarm(self, alarm: Alarm) -> None:
        """Optional: Add an alarm component."""
        if alarm.get("PROXIMITY") is not None:
            self._proximity_alarms.append(alarm)
        trigger = alarm.TRIGGER
        if trigger is None:
            return
        if isinstance(trigger, date):
            self._absolute_alarms.append(alarm)
        elif alarm.TRIGGER_RELATED == "START":
            self._start_alarms.append(alarm)
        else:
            self._end_alarms.append(alarm)

    def set_start(self, dt: date | None) -> None:
        """Set the start of the component.

        If you have only absolute alarms, this is not required.
        If you have alarms relative to the start of a component, set the start here.
        """
        self._start = dt

    def set_end(self, dt: date | None) -> None:
        """Set the end of the component.

        If you have only absolute alarms, this is not required.
        If you have alarms relative to the end of a component, set the end here.
        """
        self._end = dt

    @overload
    def _add(self, dt: datetime, td: timedelta) -> datetime: ...

    @overload
    def _add(self, dt: date, td: timedelta) -> date: ...

    def _add(self, dt: date, td: timedelta) -> date | datetime:
        """Add a timedelta to a datetime."""
        if is_date(dt):
            if td.seconds == 0:
                return dt + td
            dt = to_datetime(dt)
        return normalize_pytz(dt + td)

    def acknowledge_until(self, dt: date | None) -> None:
        """The time in UTC when all the alarms of this component were acknowledged.

        Only the last call counts.

        Since RFC 9074 (Alarm Extension) was created later,
        calendar implementations differ in how they acknowledge alarms.
        For example, Thunderbird and Google Calendar store the last time
        an event has been acknowledged because of an alarm.
        All alarms that happen before this time count as acknowledged.
        """
        self._last_ack = tzp.localize_utc(dt) if dt is not None else None

    def snooze_until(self, dt: date | None) -> None:
        """This is the time in UTC when all the alarms of this component were snoozed.

        Only the last call counts.

        The alarms are supposed to turn up again at dt when they are not acknowledged
        but snoozed.
        """
        self._snooze_until = tzp.localize_utc(dt) if dt is not None else None

    def set_local_timezone(self, tzinfo: tzinfo | str | None) -> None:
        """Set the local timezone.

        Events are sometimes in local time.
        In order to compute the exact time of the alarm, some
        alarms without timezone are considered local.

        Some computations work without setting this, others don't.
        If they need this information, expect a
        :exc:`~icalendar.error.LocalTimezoneMissing` exception
        somewhere down the line.
        """
        self._local_tzinfo = tzp.timezone(tzinfo) if isinstance(tzinfo, str) else tzinfo

    @property
    def times(self) -> list[AlarmTime]:
        """Compute and return the times of the alarms given.

        If the information for calculation is incomplete, this will raise a
        :exc:`~icalendar.error.IncompleteAlarmInformation` exception.

        Please make sure to set all the required parameters before calculating.
        If you forget to set the acknowledged times, that is not problem.
        """
        return (
            self._get_end_alarm_times()
            + self._get_start_alarm_times()
            + self._get_absolute_alarm_times()
        )

    def _repeat(self, first: datetime, alarm: Alarm) -> Generator[datetime]:
        """The times when the alarm is triggered relative to start."""
        yield first  # we trigger at the start
        repeat = alarm.repeat
        duration = alarm.DURATION
        if repeat and duration:
            for i in range(1, repeat + 1):
                yield self._add(first, duration * i)

    def _alarm_time(self, alarm: Alarm, trigger: date) -> AlarmTime:
        """Create an alarm time with the additional attributes."""
        if getattr(trigger, "tzinfo", None) is None and self._local_tzinfo is not None:
            trigger = normalize_pytz(trigger.replace(tzinfo=self._local_tzinfo))
        return AlarmTime(
            alarm, trigger, self._last_ack, self._snooze_until, self._parent
        )

    def _get_absolute_alarm_times(self) -> list[AlarmTime]:
        """Return a list of absolute alarm times."""
        return [
            self._alarm_time(alarm, trigger)
            for alarm in self._absolute_alarms
            for trigger in self._repeat(alarm.TRIGGER, alarm)
        ]

    def _get_start_alarm_times(self) -> list[AlarmTime]:
        """Return a list of alarm times relative to the start of the component."""
        if self._start is None and self._start_alarms:
            raise ComponentStartMissing(
                "Use Alarms.set_start because at least one alarm is relative to the "
                "start of a component."
            )
        return [
            self._alarm_time(alarm, trigger)
            for alarm in self._start_alarms
            for trigger in self._repeat(self._add(self._start, alarm.TRIGGER), alarm)
        ]

    def _get_end_alarm_times(self) -> list[AlarmTime]:
        """Return a list of alarm times relative to the end of the component."""
        if self._end is None and self._end_alarms:
            raise ComponentEndMissing(
                "Use Alarms.set_end because at least one alarm is relative to the end "
                "of a component."
            )
        return [
            self._alarm_time(alarm, trigger)
            for alarm in self._end_alarms
            for trigger in self._repeat(self._add(self._end, alarm.TRIGGER), alarm)
        ]

    @property
    def active(self) -> list[AlarmTime]:
        """The alarm times that are still active and not acknowledged.

        This considers snoozed alarms.

        Alarms can be in local time (without a timezone).
        To calculate if the alarm really happened, we need it to be in a timezone.
        If a timezone is required but not given, we throw an
        :exc:`~icalendar.error.IncompleteAlarmInformation`.
        """
        return [alarm_time for alarm_time in self.times if alarm_time.is_active()]

    def proximity_times(
        self,
        location_events: Iterable[LocationEvent],
        now: datetime | None = None,
    ) -> list[ProximityTrigger]:
        """Compute proximity triggers from an external location event stream.

        This implements the proximity trigger semantics of :rfc:`9074` for
        all alarms with a ``PROXIMITY`` property. The computation is pure:
        it does not use GPS or the network, it only evaluates the given
        :class:`LocationEvent` samples against the alarm's ``VLOCATION``.

        Parameters:
            location_events: The location samples to evaluate. They may be
                given in any order; they are sorted by timestamp before the
                geofence transitions are computed, so out-of-order samples
                cannot move an acknowledged alarm back to active.
            now: The reference time used to decide whether a trigger is
                still :attr:`~ProximityTrigger.pending`. Defaults to the
                current UTC time. Pass an explicit value to control time
                in tests and simulations.

        Returns:
            The :class:`ProximityTrigger` records, sorted by trigger time.
            Each record explains which location event, which repetition,
            and which rule produced it. Acknowledged occurrences are
            included but report :meth:`~AlarmTime.is_active` as ``False``.

        Raises:
            ~icalendar.error.InvalidCalendar: If a proximity alarm has no
                usable location or an invalid radius.
            ~icalendar.error.LocalTimezoneMissing: If a location event has
                a naive timestamp.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        elif now.tzinfo is None:
            now = tzp.localize_utc(now)
        events = sorted(location_events, key=lambda event: event.timestamp)
        if not events or not self._proximity_alarms:
            return []
        if self._is_cancelled():
            return []
        triggers = []
        for alarm in self._proximity_alarms:
            proximity = Proximity.from_alarm(alarm)
            if proximity is None:
                continue
            triggers.extend(self._proximity_triggers(alarm, proximity, events, now))
        triggers.sort(
            key=lambda trigger: (
                trigger.trigger,
                trigger.location_event.timestamp,
                trigger.repetition,
            )
        )
        return triggers

    def _is_cancelled(self) -> bool:
        """Whether the parent component was cancelled."""
        if self._parent is None:
            return False
        status = getattr(self._parent, "status", "") or ""
        return str(status).upper() == "CANCELLED"

    def _proximity_triggers(
        self,
        alarm: Alarm,
        proximity: Proximity,
        events: list[LocationEvent],
        now: datetime,
    ) -> list[ProximityTrigger]:
        """Compute the trigger records of one proximity alarm."""
        triggers = []
        inside = None
        for event in events:
            is_inside = proximity.contains(event.latitude, event.longitude)
            crossed = (
                inside is not None
                and is_inside != inside
                and is_inside == (proximity.direction == Proximity.ARRIVE)
            )
            if crossed:
                rule = (
                    f"{proximity.direction} within {proximity.radius:g} m of "
                    f"'{proximity.name or 'location'}' "
                    f"(geo:{proximity.latitude},{proximity.longitude})"
                )
                for repetition, trigger in enumerate(
                    self._repeat(event.timestamp, alarm)
                ):
                    triggers.append(
                        ProximityTrigger(
                            alarm,
                            trigger,
                            event,
                            repetition,
                            rule,
                            now,
                            acknowledged_until=self._last_ack,
                            snoozed_until=self._snooze_until,
                            parent=self._parent,
                        )
                    )
            inside = is_inside
        return triggers


__all__ = [
    "AlarmTime",
    "Alarms",
    "ComponentEndMissing",
    "ComponentStartMissing",
    "IncompleteAlarmInformation",
    "LocationEvent",
    "Proximity",
    "ProximityTrigger",
]
