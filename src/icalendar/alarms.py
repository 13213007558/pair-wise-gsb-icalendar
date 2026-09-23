"""Compute the times and states of alarms.

This takes different calendar software into account and the RFC 9074 (Alarm Extension).

- RFC 9074 defines an ACKNOWLEDGED property in the VALARM.
- RFC 9074 defines proximity alarms that trigger when arriving at or
  departing from a location. See :meth:`Alarms.proximity_triggers`.
- Outlook does not export VALARM information.
- Google Calendar uses the DTSTAMP to acknowledge the alarms.
- Thunderbird snoozes the alarms with a X-MOZ-SNOOZE-TIME attribute in the event.
- Thunderbird acknowledges the alarms with a X-MOZ-LASTACK attribute in the event.
- Etar deletes alarms that are acknowledged.
- Nextcloud's Webinterface does not do anything with the alarms when the time passes.
"""

from __future__ import annotations

from datetime import date, timedelta, tzinfo
from math import asin, cos, radians, sin, sqrt
from typing import TYPE_CHECKING, NamedTuple, overload

from icalendar.cal.event import Event
from icalendar.cal.todo import Todo
from icalendar.error import (
    ComponentEndMissing,
    ComponentStartMissing,
    IncompleteAlarmInformation,
    InvalidCalendar,
    InvalidProximityRadius,
    LocalTimezoneMissing,
    ProximityLocationMissing,
)
from icalendar.timezone import tzp
from icalendar.tools import is_date, normalize_pytz, to_datetime

if TYPE_CHECKING:
    from collections.abc import Generator, Iterable
    from datetime import datetime

    from icalendar.cal.alarm import Alarm
    from icalendar.cal.component import Component
    from icalendar.prop import vBinary, vUri

Parent = Event | Todo

#: The default radius in meters around a proximity alarm location
#: when the `geo:` URI does not carry an uncertainty (`u`) parameter.
DEFAULT_PROXIMITY_RADIUS_METERS = 100.0


class LocationEvent(NamedTuple):
    """A single position sample from an external location source.

    The alarm computation does not do GPS or network positioning itself.
    Instead, it consumes an ordered stream of these samples, for example
    from a device location service, and matches them against the
    locations of proximity alarms.

    Parameters:
        when: The timezone-aware time at which the position was measured.
        latitude: The latitude of the position in degrees.
        longitude: The longitude of the position in degrees.
    """

    when: datetime
    latitude: float
    longitude: float


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


def _distance_meters(
    latitude_1: float, longitude_1: float, latitude_2: float, longitude_2: float
) -> float:
    """Compute the great-circle distance between two positions in meters."""
    earth_radius = 6371000.0
    phi_1, phi_2 = radians(latitude_1), radians(latitude_2)
    delta_phi = radians(latitude_2 - latitude_1)
    delta_lambda = radians(longitude_2 - longitude_1)
    a = sin(delta_phi / 2) ** 2 + cos(phi_1) * cos(phi_2) * sin(delta_lambda / 2) ** 2
    return 2 * earth_radius * asin(sqrt(a))


def _parse_geo_uri(uri: str, default_radius: float) -> tuple[float, float, float]:
    """Parse a `geo:` URI into latitude, longitude and radius in meters.

    The radius is the `u` (uncertainty) parameter of :rfc:`5870` `geo:`
    URIs. If it is missing, `default_radius` is used.

    Raises:
        InvalidCalendar: If the URI is not a valid `geo:` URI.
        InvalidProximityRadius: If the radius is not a non-negative number.
    """
    if not uri.lower().startswith("geo:"):
        raise InvalidCalendar(f"Proximity alarm locations must use a geo: URI, not {uri!r}.")
    coordinates, _, parameters = uri[4:].partition(";")
    parts = coordinates.split(",")
    if len(parts) < 2:
        raise InvalidCalendar(f"Invalid geo: URI in proximity alarm: {uri!r}.")
    try:
        latitude, longitude = float(parts[0]), float(parts[1])
    except ValueError:
        raise InvalidCalendar(f"Invalid geo: URI in proximity alarm: {uri!r}.") from None
    radius = default_radius
    for parameter in parameters.split(";"):
        name, _, value = parameter.partition("=")
        if name.lower() == "u":
            try:
                radius = float(value)
            except ValueError:
                raise InvalidProximityRadius(
                    f"The radius of the proximity alarm location is not a number: {uri!r}."
                ) from None
            if radius < 0:
                raise InvalidProximityRadius(
                    f"The radius of the proximity alarm location is negative: {uri!r}."
                )
    return latitude, longitude, radius


class ProximityTrigger:
    """One explainable trigger record of a proximity alarm.

    Each record states which :class:`LocationEvent` caused the trigger,
    which repetition of the alarm's `REPEAT`/`DURATION` schedule it is,
    and which `rule` produced it:

    - `"proximity"` - entering or leaving the radius of a location
    - `"repeat"` - a repetition of a proximity trigger
    - `"snooze"` - a trigger rescheduled by snoozing the alarm

    See :meth:`Alarms.proximity_triggers` for the computation.
    """

    def __init__(
        self,
        alarm: Alarm,
        trigger: datetime,
        location_event: LocationEvent,
        vlocation: Component,
        repetition: int,
        rule: str,
        acknowledged_until: datetime | None = None,
        snoozed_until: datetime | None = None,
        parent: Parent | None = None,
    ) -> None:
        """Create a trigger record. See :meth:`Alarms.proximity_triggers`."""
        self._alarm = alarm
        self._trigger = trigger
        self._location_event = location_event
        self._vlocation = vlocation
        self._repetition = repetition
        self._rule = rule
        self._last_ack = acknowledged_until
        self._snooze_until = snoozed_until
        self._parent = parent

    def __repr__(self) -> str:
        """A representation that explains the trigger."""
        return (
            f"ProximityTrigger({self._alarm.PROXIMITY} "
            f"rule={self._rule!r} repetition={self._repetition} "
            f"trigger={self._trigger!r})"
        )

    @property
    def alarm(self) -> Alarm:
        """The alarm component this trigger belongs to."""
        return self._alarm

    @property
    def parent(self) -> Parent | None:
        """The event or todo that contains the alarm."""
        return self._parent

    @property
    def trigger(self) -> datetime:
        """The time at which this trigger fires, including snoozing."""
        return self._trigger

    @property
    def location_event(self) -> LocationEvent:
        """The position sample that caused this trigger."""
        return self._location_event

    @property
    def location(self) -> Component:
        """The `VLOCATION` component of the alarm that was matched."""
        return self._vlocation

    @property
    def repetition(self) -> int:
        """The index in the alarm's `REPEAT` schedule. `0` is the initial trigger."""
        return self._repetition

    @property
    def rule(self) -> str:
        """The rule that produced this record: `proximity`, `repeat` or `snooze`."""
        return self._rule

    @property
    def acknowledged(self) -> datetime | None:
        """The time in UTC at which this trigger was acknowledged, or None."""
        ack = self.alarm.ACKNOWLEDGED
        if ack is None:
            return self._last_ack
        if self._last_ack is None:
            return ack
        return max(ack, self._last_ack)

    def is_active(self) -> bool:
        """Whether this trigger still fires (`True`) or was acknowledged (`False`).

        Acknowledgment is monotonic: once a trigger is acknowledged, later
        or reordered location events cannot make it fire again.
        """
        acknowledged = self.acknowledged
        if acknowledged is None:
            return True
        return self._trigger > acknowledged


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
    See :meth:`proximity_triggers` for alarms that fire when arriving at
    or departing from a location.
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
        if alarm.PROXIMITY:
            # RFC 9074: the TRIGGER of a proximity alarm is ignored.
            self._proximity_alarms.append(alarm)
            return
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


    def proximity_triggers(
        self,
        locations: Iterable[LocationEvent],
        now: datetime | None = None,
        default_radius: float = DEFAULT_PROXIMITY_RADIUS_METERS,
    ) -> list[ProximityTrigger]:
        """Compute the triggers of proximity alarms from a location event stream.

        This implements the proximity trigger semantics of :rfc:`9074`.
        The computation does not do GPS or network positioning itself: it
        matches an externally produced stream of :class:`LocationEvent`
        samples against the `VLOCATION` subcomponents of the alarms.

        Parameters:
            locations: The position samples to match. They may arrive out
                of order; they are sorted by their timestamp before
                matching, so reordering cannot change the result or roll
                back an acknowledgment.
            now: An optional, controllable current time. If given, only
                triggers that fire at or before this time are returned.
            default_radius: The radius in meters used for locations whose
                `geo:` URI has no uncertainty (`u`) parameter.

        Returns:
            The trigger records sorted by their trigger time. Each record
            explains which location event, repetition and rule produced
            it. Acknowledged triggers are included but report
            :meth:`ProximityTrigger.is_active` as `False`.

        Raises:
            ~icalendar.error.ProximityLocationMissing: If a proximity
                alarm has no `VLOCATION` with a `geo:` URI.
            ~icalendar.error.InvalidProximityRadius: If the radius of a
                location is invalid.
            ~icalendar.error.InvalidCalendar: If the `PROXIMITY` value
                or a `geo:` URI is invalid.
            ~icalendar.error.LocalTimezoneMissing: If a location event
                has no timezone.

        A cancelled event never triggers:

        >>> from icalendar import Event, Alarms, LocationEvent
        >>> from datetime import datetime, timezone
        >>> event = Event.from_ical(
        ... '''BEGIN:VEVENT
        ... UID:12345
        ... DTSTAMP:20260301T000000Z
        ... DTSTART:20260302T100000Z
        ... STATUS:CANCELLED
        ... BEGIN:VALARM
        ... ACTION:DISPLAY
        ... TRIGGER;VALUE=DATE-TIME:19760401T005545Z
        ... PROXIMITY:ARRIVE
        ... BEGIN:VLOCATION
        ... UID:loc-1
        ... URL:geo:40.443,-79.945;u=10
        ... END:VLOCATION
        ... END:VALARM
        ... END:VEVENT
        ... ''')
        >>> alarms = Alarms(event)
        >>> alarms.proximity_triggers([
        ...     LocationEvent(datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc), 40.443, -79.945)
        ... ])
        []
        """
        if (
            self._parent is not None
            and (getattr(self._parent, "status", "") or "").upper() == "CANCELLED"
        ):
            return []
        samples = sorted(locations, key=lambda sample: sample.when)
        for sample in samples:
            if sample.when.tzinfo is None:
                raise LocalTimezoneMissing(
                    "Location events must have a timezone. "
                    "Naive timestamps cannot be matched against proximity alarms."
                )
        records = [
            record
            for alarm in self._proximity_alarms
            for record in self._proximity_alarm_triggers(alarm, samples, default_radius)
        ]
        records.sort(key=lambda record: record.trigger)
        if now is not None:
            now = tzp.localize_utc(now)
            records = [record for record in records if record.trigger <= now]
        return records

    def _proximity_alarm_triggers(
        self,
        alarm: Alarm,
        samples: list[LocationEvent],
        default_radius: float,
    ) -> list[ProximityTrigger]:
        """Compute the trigger records of one proximity alarm."""
        direction = alarm.PROXIMITY.upper()
        if direction not in ("ARRIVE", "DEPART"):
            raise InvalidCalendar(
                f"PROXIMITY must be ARRIVE or DEPART, not {alarm.PROXIMITY!r}."
            )
        targets = []
        for vlocation in alarm.locations:
            url = vlocation.get("URL")
            if url is None:
                continue
            targets.append((vlocation, _parse_geo_uri(str(url), default_radius)))
        if not targets:
            raise ProximityLocationMissing(
                "The proximity alarm has no VLOCATION with a geo: URI. "
                "Add one to compute when it triggers."
            )
        return [
            record
            for vlocation, (latitude, longitude, radius) in targets
            for record in self._proximity_location_triggers(
                alarm, vlocation, latitude, longitude, radius, samples, direction
            )
        ]

    def _proximity_location_triggers(
        self,
        alarm: Alarm,
        vlocation: Component,
        latitude: float,
        longitude: float,
        radius: float,
        samples: list[LocationEvent],
        direction: str,
    ) -> list[ProximityTrigger]:
        """Compute the records created by entering or leaving one location."""
        records = []
        inside = None
        for sample in samples:
            now_inside = (
                _distance_meters(
                    sample.latitude, sample.longitude, latitude, longitude
                )
                <= radius
            )
            if inside is None:
                # The first sample only establishes where we are.
                inside = now_inside
                continue
            entered = not inside and now_inside
            left = inside and not now_inside
            inside = now_inside
            if (direction == "ARRIVE" and entered) or (direction == "DEPART" and left):
                records.extend(self._proximity_records(alarm, vlocation, sample))
        return records

    def _proximity_records(
        self,
        alarm: Alarm,
        vlocation: Component,
        sample: LocationEvent,
    ) -> list[ProximityTrigger]:
        """Combine a proximity trigger with REPEAT, DURATION, ACKNOWLEDGED and snooze."""
        repeat = alarm.repeat
        duration = alarm.DURATION
        repetitions = repeat + 1 if repeat and duration else 1
        records = []
        for repetition in range(repetitions):
            trigger = sample.when + (duration * repetition if duration else timedelta(0))
            rule = "proximity" if repetition == 0 else "repeat"
            if self._snooze_until is not None and self._snooze_until > trigger:
                trigger = self._snooze_until
                rule = "snooze"
            records.append(
                ProximityTrigger(
                    alarm,
                    trigger,
                    sample,
                    vlocation,
                    repetition,
                    rule,
                    self._last_ack,
                    self._snooze_until,
                    self._parent,
                )
            )
        return records


__all__ = [
    "AlarmTime",
    "Alarms",
    "ComponentEndMissing",
    "ComponentStartMissing",
    "IncompleteAlarmInformation",
    "LocationEvent",
    "ProximityTrigger",
]
