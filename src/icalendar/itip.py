"""Process iTIP REQUEST and CANCEL messages into a mergeable inbox state.

This module implements the receiving side of iTIP (:rfc:`5546`) for the
``REQUEST`` and ``CANCEL`` methods.  It is deliberately small in scope:

- The caller is responsible for authenticating the sender.  This module only
  merges versions and components.
- ``REPLY`` and ``COUNTER`` messages are rejected, as are
  ``RECURRENCE-ID`` values with a ``RANGE`` parameter such as
  ``THISANDFUTURE``.
- No e-mail is sent and no attendee identity is verified.

Meetings are keyed by ``UID``, ``ORGANIZER`` and ``RECURRENCE-ID`` so the
master event and each single-occurrence exception are tracked separately.
Update ordering uses ``SEQUENCE`` first and ``DTSTAMP`` as a tiebreaker.
Stale versions are ignored.  Two messages with the same version but
different content produce an inspectable :class:`ITIPConflict`.

A ``CANCEL`` never deletes the version memory of a meeting: it leaves a
tombstone that keeps the ``SEQUENCE``/``DTSTAMP`` of the cancellation, so a
late, older ``REQUEST`` cannot resurrect the meeting.  A cancellation of the
whole series (no ``RECURRENCE-ID``) and a cancellation of a single
occurrence are independent tombstones; a late update of the master event
never touches the tombstone of an occurrence and vice versa.

The state is serializable with :meth:`ITIPInbox.to_dict` and restorable with
:meth:`ITIPInbox.from_dict`.  Received calendars are never mutated.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Optional

from icalendar.cal.calendar import Calendar
from icalendar.cal.event import Event
from icalendar.enums import StrEnum

__all__ = [
    "ITIPConflict",
    "ITIPEntry",
    "ITIPInbox",
    "ITIPResult",
    "ITIPStatus",
    "UnsupportedITIPMessage",
]


class ITIPStatus(StrEnum):
    """The outcome of processing one iTIP component."""

    APPLIED = "applied"
    """The message was newer and has been merged into the state."""

    IGNORED = "ignored"
    """The message was stale or a duplicate and changed nothing."""

    CONFLICT = "conflict"
    """The message has the same version but different content."""


class UnsupportedITIPMessage(ValueError):
    """Raised for iTIP messages this module refuses to process.

    This covers methods other than ``REQUEST`` and ``CANCEL`` (such as
    ``REPLY`` and ``COUNTER``) and ``RECURRENCE-ID`` properties with a
    ``RANGE`` parameter (such as ``THISANDFUTURE``).
    """


@dataclass
class ITIPConflict:
    """An inspectable conflict between two versions of the same component.

    Both versions carry the same ``SEQUENCE`` and ``DTSTAMP`` but differ in
    content.  ``kept`` is the component that deterministically remains in
    the state; ``discarded`` is the one that was dropped.
    """

    uid: str
    organizer: Optional[str]
    recurrence_id: Optional[str]
    sequence: int
    dtstamp: Optional[datetime]
    kept: str
    discarded: str


@dataclass
class ITIPResult:
    """The result of processing one ``VEVENT`` of an iTIP message."""

    status: ITIPStatus
    uid: str
    organizer: Optional[str]
    recurrence_id: Optional[str]
    cancelled: bool
    reason: str = ""
    conflict: Optional[ITIPConflict] = None


@dataclass
class ITIPEntry:
    """The remembered state of one meeting component.

    A tombstone is an entry with ``cancelled=True``: the component is no
    longer effective, but its version is still remembered so that older
    messages cannot resurrect it.
    """

    uid: str
    organizer: Optional[str]
    recurrence_id: Optional[str]
    sequence: int
    dtstamp: Optional[datetime]
    cancelled: bool
    component: Event

    def snapshot(self) -> "ITIPEntry":
        """Return a copy that does not share mutable state with the inbox."""
        return ITIPEntry(
            uid=self.uid,
            organizer=self.organizer,
            recurrence_id=self.recurrence_id,
            sequence=self.sequence,
            dtstamp=self.dtstamp,
            cancelled=self.cancelled,
            component=copy.deepcopy(self.component),
        )


def _normalize_dtstamp(value: Any) -> datetime:
    """Return an aware UTC datetime for comparison purposes."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    return datetime.min.replace(tzinfo=timezone.utc)


def _recurrence_id_key(event: Event) -> Optional[str]:
    """Return a stable key for the ``RECURRENCE-ID`` of an event."""
    prop = event.get("RECURRENCE-ID")
    if prop is None:
        return None
    params = getattr(prop, "params", {})
    range_value = params.get("RANGE")
    if range_value is not None:
        raise UnsupportedITIPMessage(
            f"RECURRENCE-ID with RANGE={range_value} is not supported."
        )
    key = prop.to_ical().decode("utf-8")
    tzid = params.get("TZID")
    if tzid:
        key = f"TZID={tzid}:{key}"
    return key


def _content_fingerprint(event: Event) -> bytes:
    """Serialize an event without ``DTSTAMP`` for content comparison."""
    clone = copy.deepcopy(event)
    clone.pop("DTSTAMP", None)
    return clone.to_ical()


class ITIPInbox:
    """Mergeable receiving state for iTIP ``REQUEST``/``CANCEL`` messages.

    The inbox remembers one :class:`ITIPEntry` per
    ``(UID, ORGANIZER, RECURRENCE-ID)`` triple.  Entries whose version is
    superseded are replaced; cancellations become tombstones that keep the
    version memory alive.
    """

    SERIALIZATION_VERSION = 1

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str, Optional[str]], ITIPEntry] = {}
        self._conflicts: list[ITIPConflict] = []

    @staticmethod
    def _method_of(calendar: Calendar) -> str:
        method = calendar.get("METHOD")
        if method is None:
            raise UnsupportedITIPMessage(
                "Calendar has no METHOD; only iTIP REQUEST and CANCEL are supported."
            )
        method = str(method).upper()
        if method not in ("REQUEST", "CANCEL"):
            raise UnsupportedITIPMessage(
                f"iTIP method {method} is not supported; "
                "only REQUEST and CANCEL are."
            )
        return method

    @staticmethod
    def _entry_for(event: Event, cancelled: bool) -> ITIPEntry:
        uid = event.get("UID")
        if uid is None:
            raise UnsupportedITIPMessage("VEVENT without UID cannot be processed.")
        organizer = event.get("ORGANIZER")
        sequence = event.get("SEQUENCE")
        return ITIPEntry(
            uid=str(uid),
            organizer=str(organizer) if organizer is not None else None,
            recurrence_id=_recurrence_id_key(event),
            sequence=int(sequence) if sequence is not None else 0,
            dtstamp=_normalize_dtstamp(event.decoded("DTSTAMP", None)),
            cancelled=cancelled,
            component=copy.deepcopy(event),
        )

    @staticmethod
    def _key_of(entry: ITIPEntry) -> tuple[str, str, Optional[str]]:
        organizer = entry.organizer.lower() if entry.organizer else ""
        return (entry.uid, organizer, entry.recurrence_id)

    @staticmethod
    def _version(entry: ITIPEntry) -> tuple[int, datetime]:
        return (entry.sequence, entry.dtstamp)

    def process(self, calendar: Calendar) -> list[ITIPResult]:
        """Merge one received iTIP message into the state.

        The received calendar is not modified.  One :class:`ITIPResult` is
        returned per ``VEVENT`` in the message, so the receiving service can
        decide whether to notify the user.
        """
        method = self._method_of(calendar)
        cancelled = method == "CANCEL"
        results = []
        for event in calendar.walk("VEVENT"):
            results.append(self._process_event(event, cancelled))
        return results

    def _process_event(self, event: Event, cancelled: bool) -> ITIPResult:
        incoming = self._entry_for(event, cancelled)
        key = self._key_of(incoming)
        existing = self._entries.get(key)
        base = {
            "uid": incoming.uid,
            "organizer": incoming.organizer,
            "recurrence_id": incoming.recurrence_id,
            "cancelled": cancelled,
        }
        if existing is None:
            self._entries[key] = incoming
            return ITIPResult(status=ITIPStatus.APPLIED, reason="new component", **base)

        incoming_version = self._version(incoming)
        existing_version = self._version(existing)
        if incoming_version < existing_version:
            return ITIPResult(
                status=ITIPStatus.IGNORED,
                reason="stale version",
                **base,
            )
        if incoming_version > existing_version:
            self._entries[key] = incoming
            return ITIPResult(
                status=ITIPStatus.APPLIED, reason="newer version", **base
            )

        if _content_fingerprint(existing.component) == _content_fingerprint(
            incoming.component
        ):
            return ITIPResult(
                status=ITIPStatus.IGNORED,
                reason="duplicate of the stored version",
                **base,
            )

        kept, discarded = existing, incoming
        if _content_fingerprint(incoming.component) < _content_fingerprint(
            existing.component
        ):
            kept, discarded = incoming, existing
            self._entries[key] = incoming
        conflict = ITIPConflict(
            uid=incoming.uid,
            organizer=incoming.organizer,
            recurrence_id=incoming.recurrence_id,
            sequence=incoming.sequence,
            dtstamp=incoming.dtstamp,
            kept=kept.component.to_ical().decode("utf-8"),
            discarded=discarded.component.to_ical().decode("utf-8"),
        )
        self._conflicts.append(conflict)
        return ITIPResult(
            status=ITIPStatus.CONFLICT,
            reason="same SEQUENCE and DTSTAMP but different content",
            conflict=conflict,
            **base,
        )

    def entries(self) -> list[ITIPEntry]:
        """Return snapshots of all remembered entries, sorted deterministically."""
        return [
            self._entries[key].snapshot()
            for key in sorted(self._entries, key=lambda k: (k[0], k[1], k[2] or ""))
        ]

    def events(self) -> list[Event]:
        """Return copies of the currently effective (not cancelled) events."""
        return [entry.component for entry in self.entries() if not entry.cancelled]

    def tombstones(self) -> list[ITIPEntry]:
        """Return snapshots of all cancelled components."""
        return [entry for entry in self.entries() if entry.cancelled]

    def conflicts(self) -> list[ITIPConflict]:
        """Return the conflicts recorded so far."""
        return list(self._conflicts)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the state into JSON-compatible built-in types."""
        return {
            "version": self.SERIALIZATION_VERSION,
            "entries": [
                {
                    "uid": entry.uid,
                    "organizer": entry.organizer,
                    "recurrence_id": entry.recurrence_id,
                    "sequence": entry.sequence,
                    "dtstamp": entry.dtstamp.isoformat(),
                    "cancelled": entry.cancelled,
                    "component": entry.component.to_ical().decode("utf-8"),
                }
                for entry in self.entries()
            ],
            "conflicts": [
                {
                    "uid": conflict.uid,
                    "organizer": conflict.organizer,
                    "recurrence_id": conflict.recurrence_id,
                    "sequence": conflict.sequence,
                    "dtstamp": conflict.dtstamp.isoformat()
                    if conflict.dtstamp
                    else None,
                    "kept": conflict.kept,
                    "discarded": conflict.discarded,
                }
                for conflict in self._conflicts
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ITIPInbox":
        """Restore an inbox previously serialized with :meth:`to_dict`."""
        if data.get("version") != cls.SERIALIZATION_VERSION:
            raise ValueError(
                f"Unsupported serialization version: {data.get('version')!r}."
            )
        inbox = cls()
        for raw in data.get("entries", []):
            component = Event.from_ical(raw["component"])
            entry = ITIPEntry(
                uid=raw["uid"],
                organizer=raw["organizer"],
                recurrence_id=raw["recurrence_id"],
                sequence=int(raw["sequence"]),
                dtstamp=_normalize_dtstamp(datetime.fromisoformat(raw["dtstamp"])),
                cancelled=bool(raw["cancelled"]),
                component=component,
            )
            inbox._entries[inbox._key_of(entry)] = entry
        for raw in data.get("conflicts", []):
            dtstamp = raw.get("dtstamp")
            inbox._conflicts.append(
                ITIPConflict(
                    uid=raw["uid"],
                    organizer=raw["organizer"],
                    recurrence_id=raw["recurrence_id"],
                    sequence=int(raw["sequence"]),
                    dtstamp=datetime.fromisoformat(dtstamp) if dtstamp else None,
                    kept=raw["kept"],
                    discarded=raw["discarded"],
                )
            )
        return inbox
