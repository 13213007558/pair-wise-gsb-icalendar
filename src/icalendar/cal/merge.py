"""Merge calendars while resolving conflicting VTIMEZONE definitions.

Two calendars can declare the same ``TZID`` (for example ``Office``) with
different ``VTIMEZONE`` definitions.  Putting their components into one
calendar would make them share a single definition, so events from one
calendar would be interpreted with the wrong timezone.

:func:`merge_calendars` compares the timezone definitions semantically and
assigns a new identity (TZID) to the ones that truly conflict, updating only
the components that reference them.
"""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING

from icalendar.cal.calendar import Calendar
from icalendar.error import InvalidCalendar
from icalendar.prop import vText

if TYPE_CHECKING:
    from collections.abc import Callable

    from icalendar.cal.component import Component
    from icalendar.cal.timezone import Timezone

__all__ = ["merge_calendars"]


def _canonical_parameters(params) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Return the canonical form of property parameters for comparison."""
    canonical = []
    for key, value in params.items():
        if isinstance(value, (list, tuple)):
            values = tuple(str(item) for item in value)
        else:
            values = (str(value),)
        canonical.append((key.upper(), tuple(sorted(values))))
    return tuple(sorted(canonical))


def _canonical_property(name: str, value) -> tuple[str, tuple, bytes]:
    """Return the canonical form of a property for comparison."""
    params = _canonical_parameters(getattr(value, "params", None) or {})
    to_ical = getattr(value, "to_ical", None)
    encoded = to_ical() if callable(to_ical) else repr(value)
    if isinstance(encoded, str):
        encoded = encoded.encode("utf-8")
    return (name.upper(), params, encoded)


def _component_fingerprint(component: Component) -> tuple:
    """Return an order-insensitive fingerprint of a component.

    The properties and subcomponents are sorted, so their order in the
    input does not matter.  Values are compared after parsing, so line
    folding differences do not matter either.
    """
    properties = []
    for name in component:
        values = component[name]
        if not isinstance(values, list):
            values = [values]
        properties.extend(_canonical_property(name, value) for value in values)
    subcomponents = [_component_fingerprint(sub) for sub in component.subcomponents]
    return (
        component.name,
        tuple(sorted(properties)),
        tuple(sorted(subcomponents)),
    )


def _timezone_fingerprint(timezone: Timezone) -> tuple:
    """Return the semantic fingerprint of a VTIMEZONE definition.

    The ``TZID`` property is the identity of the definition, not part of
    it, so it is excluded.  Two timezones with the same fingerprint have
    the same offsets and STANDARD/DAYLIGHT transition rules, even if the
    property order or line folding of their input differs.
    """
    _name, properties, subcomponents = _component_fingerprint(timezone)
    properties = tuple(prop for prop in properties if prop[0] != "TZID")
    return (timezone.name, properties, subcomponents)


def _top_level_timezones(calendar: Calendar) -> list[Timezone]:
    """Return the VTIMEZONE components defined directly in the calendar."""
    return [c for c in calendar.subcomponents if c.name == "VTIMEZONE"]


def _validate_calendar(calendar: Calendar, index: int) -> None:
    """Report missing and contradictory timezone definitions.

    Raises:
        InvalidCalendar: If the calendar references a TZID without defining
            it, or defines the same TZID twice with contradictory
            definitions.
    """
    missing = calendar.get_missing_tzids()
    if missing:
        raise InvalidCalendar(
            f"Calendar {index} references TZIDs without a VTIMEZONE "
            f"definition: {', '.join(sorted(missing))}."
        )
    definitions: dict[str, tuple] = {}
    for timezone in _top_level_timezones(calendar):
        tzid = timezone.get("TZID")
        if tzid is None:
            raise InvalidCalendar(
                f"Calendar {index} contains a VTIMEZONE without a TZID."
            )
        tzid = str(tzid)
        fingerprint = _timezone_fingerprint(timezone)
        if tzid in definitions and definitions[tzid] != fingerprint:
            raise InvalidCalendar(
                f"Calendar {index} contains contradictory VTIMEZONE "
                f"definitions for TZID {tzid!r}."
            )
        definitions[tzid] = fingerprint


def _rename_tzid(calendar: Calendar, old_tzid: str, new_tzid: str) -> None:
    """Rename a timezone definition and every reference to it in place.

    Only the ``TZID`` property of the matching VTIMEZONE components and
    the ``TZID`` parameter of date/time properties (such as ``DTSTART``,
    ``DTEND``, ``RECURRENCE-ID``, ``RDATE``, and ``EXDATE``, including
    multi-value properties) are modified.  Text values such as
    ``DESCRIPTION`` are left untouched, and no datetime value is
    converted.
    """
    for component in calendar.walk():
        for name in component:
            values = component[name]
            if not isinstance(values, list):
                values = [values]
            for value in values:
                params = getattr(value, "params", None)
                if params and str(params.get("TZID", "")) == old_tzid:
                    params["TZID"] = new_tzid
        if component.name == "VTIMEZONE" and str(component.get("TZID", "")) == old_tzid:
            component["TZID"] = vText(new_tzid)


def _unique_tzid(candidate: str, taken: set[str]) -> str:
    """Return the candidate, or a suffixed variant that is not taken."""
    if candidate not in taken:
        return candidate
    suffix = 2
    while f"{candidate}-{suffix}" in taken:
        suffix += 1
    return f"{candidate}-{suffix}"


def _default_tzid_namer(tzid: str, index: int) -> str:
    """Name a conflicting timezone after the calendar it came from."""
    return f"{tzid}-{index + 1}"


def merge_calendars(
    *calendars: Calendar,
    tzid_namer: Callable[[str, int], str] | None = None,
) -> tuple[Calendar, list[dict[str, str]]]:
    r"""Merge calendars, resolving conflicting VTIMEZONE definitions.

    The calendars are combined into a new one.  Timezone definitions are
    compared semantically: property order and line folding differences do
    not create a conflict, but different STANDARD/DAYLIGHT transition
    rules do.  When two calendars define the same TZID differently, the
    later definition is assigned a new TZID and only the components
    referencing it are updated.  Wall-clock times and transition rules
    are preserved; nothing is converted to UTC.

    The input calendars are not modified.  Calendar-level properties are
    taken from the first calendar.

    Parameters:
        calendars: The calendars to merge, in order.
        tzid_namer: A callable ``(tzid, calendar_index) -> new_tzid``
            that names a conflicting timezone.  If the generated name is
            already used in any input calendar, a numeric suffix is
            appended until it is unique.  Defaults to naming the timezone
            after the 1-based index of the calendar it came from, for
            example ``Office-2``.

    Returns:
        A tuple of the merged calendar and a list of rename mappings, one
        per input calendar, mapping the original TZID to the new TZID.
        A calendar whose timezones did not conflict has an empty mapping.

    Raises:
        ValueError: If fewer than two calendars are given.
        InvalidCalendar: If a calendar references a TZID without defining
            it, or defines the same TZID twice with contradictory
            definitions.  This is reported before anything is merged.

    Example:
        Merge two calendars that both declare ``TZID:Office`` with
        different offsets.

        .. code-block:: pycon

            >>> from icalendar import Calendar
            >>> from icalendar.cal.merge import merge_calendars
            >>> def make_calendar(offset):
            ...     return Calendar.from_ical(
            ...         "BEGIN:VCALENDAR\n"
            ...         "VERSION:2.0\n"
            ...         "PRODID:merge-example\n"
            ...         "BEGIN:VTIMEZONE\n"
            ...         "TZID:Office\n"
            ...         "BEGIN:STANDARD\n"
            ...         "DTSTART:19700101T000000\n"
            ...         f"TZOFFSETFROM:{offset}\n"
            ...         f"TZOFFSETTO:{offset}\n"
            ...         "END:STANDARD\n"
            ...         "END:VTIMEZONE\n"
            ...         "BEGIN:VEVENT\n"
            ...         "UID:meeting\n"
            ...         "DTSTART;TZID=Office:20261015T090000\n"
            ...         "SUMMARY:Standup\n"
            ...         "END:VEVENT\n"
            ...         "END:VCALENDAR\n"
            ...     )
            >>> merged, renames = merge_calendars(
            ...     make_calendar("+0800"), make_calendar("-0500")
            ... )
            >>> renames
            [{}, {'Office': 'Office-2'}]
            >>> sorted(timezone.tz_name for timezone in merged.timezones)
            ['Office', 'Office-2']
            >>> [event["DTSTART"].params["TZID"] for event in merged.events]
            ['Office', 'Office-2']
            >>> [event.start.strftime("%H:%M") for event in merged.events]
            ['09:00', '09:00']

    ..  versionadded:: 7.4.0
    """
    if len(calendars) < 2:
        raise ValueError("Merging requires at least two calendars.")
    if tzid_namer is None:
        tzid_namer = _default_tzid_namer
    for index, calendar in enumerate(calendars):
        _validate_calendar(calendar, index)

    taken_tzids: set[str] = set()
    for calendar in calendars:
        taken_tzids.update(calendar.get_used_tzids())
        for timezone in _top_level_timezones(calendar):
            tzid = timezone.get("TZID")
            if tzid is not None:
                taken_tzids.add(str(tzid))

    merged = Calendar()
    for name in calendars[0]:
        merged[name] = deepcopy(calendars[0][name])

    registered: dict[str, tuple] = {}
    renames: list[dict[str, str]] = []
    for index, calendar in enumerate(calendars):
        working = calendar.copy(recursive=True)
        calendar_renames: dict[str, str] = {}
        timezones: dict[str, Timezone] = {}
        other_components = []
        for component in working.subcomponents:
            if component.name == "VTIMEZONE":
                # identical duplicates were validated above, keep the first
                timezones.setdefault(str(component["TZID"]), component)
            else:
                other_components.append(component)
        merged_timezones = []
        for tzid, timezone in timezones.items():
            fingerprint = _timezone_fingerprint(timezone)
            existing = registered.get(tzid)
            if existing is None:
                registered[tzid] = fingerprint
                merged_timezones.append(timezone)
            elif existing != fingerprint:
                new_tzid = _unique_tzid(tzid_namer(tzid, index), taken_tzids)
                taken_tzids.add(new_tzid)
                registered[new_tzid] = fingerprint
                _rename_tzid(working, tzid, new_tzid)
                calendar_renames[tzid] = new_tzid
                merged_timezones.append(timezone)
            # an identical definition is already merged: drop the duplicate
        for component in [*merged_timezones, *other_components]:
            merged.add_component(component)
        renames.append(calendar_renames)
    return merged, renames
