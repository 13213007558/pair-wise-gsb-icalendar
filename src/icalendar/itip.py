"""Process iTIP (:rfc:`5546`) scheduling messages into a local inbox state.

This module implements the receiving side of iTIP for REQUEST and
CANCEL messages.  The caller is responsible for authenticating the
sender of a message; this module only takes care of versioning
(SEQUENCE / DTSTAMP) and of merging meeting components keyed by
UID, ORGANIZER and RECURRENCE-ID.

Out of scope, by design:

- REPLY and COUNTER messages are rejected.
- RECURRENCE-ID values with RANGE=THISANDFUTURE are rejected.
- No e-mail is sent and no attendee identity is verified.

Cancellations are kept as tombstones: removing a component from the list
of effective events never loses the version memory of its cancellation,
so a stale message that arrives late cannot resurrect a cancelled meeting.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any

from icalendar.cal import Calendar, Event

__all__ = [
    "ApplyStatus",
    "ComponentKey",
    "ConflictRecord",
    "ITipError",
    "ITipInbox",
    "ITipResult",
    "InboxEntry",
    "InvalidITipMessage",
    "UnsupportedITipMessage",
]


class ITipError(ValueError):
    """Base class for iTIP messages this module refuses to process."""


class UnsupportedITipMessage(ITipError):
    """The message uses a METHOD or RANGE this module does not handle."""


class InvalidITipMessage(ITipError):
    """The message is missing information required to file it."""


class ApplyStatus(Enum):
    """The outcome of filing one component of a message."""

    APPLIED = "applied"
    IGNORED = "ignored"
    CONFLICT = "conflict"


def _normalise_dtstamp(value: datetime | None) -> datetime:
    """Return a comparable, UTC-based datetime for a possibly missing DTSTAMP."""
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if value.tzinfo is None:
        # DTSTAMP is required to be UTC by RFC 5545; assume UTC if naive.
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, eq=False)
class _Version:
    """Ordering key of a component version per RFC 5546.

    A higher SEQUENCE always wins; DTSTAMP breaks the tie.
    """

    sequence: int
    dtstamp: datetime | None

    def _key(self) -> tuple[int, datetime]:
        return (self.sequence, _normalise_dtstamp(self.dtstamp))

    def __lt__(self, other: _Version) -> bool:
        return self._key() < other._key()

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, _Version):
            return NotImplemented
        return self._key() == other._key()

    def __hash__(self) -> int:
        return hash(self._key())


@dataclass(frozen=True)
class ComponentKey:
    """Identity of a meeting component: master event or single exception."""

    uid: str
    organizer: str
    recurrence_id: str | None  # ISO format, None for the master event


@dataclass
class ConflictRecord:
    """A same-version message whose content disagrees with the stored state."""

    reason: str
    component: Event


@dataclass
class InboxEntry:
    """Versioned state of one meeting component.

    A cancelled entry is a tombstone: it keeps its SEQUENCE and
    DTSTAMP so that older messages arriving later are still ignored.
    """

    key: ComponentKey
    sequence: int
    dtstamp: datetime | None
    cancelled: bool
    component: Event | None
    conflicts: list[ConflictRecord] = field(default_factory=list)

    @property
    def version(self) -> _Version:
        return _Version(self.sequence, self.dtstamp)


@dataclass
class ITipResult:
    """The per-component outcome of :meth:`ITipInbox.apply`."""

    status: ApplyStatus
    key: ComponentKey
    reason: str


def _component_key(component: Event) -> ComponentKey:
    uid = component.get("UID")
    if uid is None:
        raise InvalidITipMessage("VEVENT is missing its UID property")
    recurrence_id_prop = component.get("RECURRENCE-ID")
    recurrence_id: str | None = None
    if recurrence_id_prop is not None:
        range_param = recurrence_id_prop.params.get("RANGE")
        if range_param is not None and str(range_param).upper() != "THISANDPRIOR":
            raise UnsupportedITipMessage(
                f"RECURRENCE-ID with RANGE={range_param} is not supported"
            )
        decoded = component.decoded("RECURRENCE-ID")
        value = getattr(decoded, "dt", decoded)
        if isinstance(value, (datetime, date)):
            recurrence_id = value.isoformat()
        else:
            recurrence_id = str(value)
    organizer = component.get("ORGANIZER")
    return ComponentKey(
        uid=str(uid),
        organizer="" if organizer is None else str(organizer),
        recurrence_id=recurrence_id,
    )


def _component_version(component: Event) -> _Version:
    sequence = component.decoded("SEQUENCE", 0)
    dtstamp = component.decoded("DTSTAMP", None)
    return _Version(
        sequence=int(sequence),
        dtstamp=getattr(dtstamp, "dt", dtstamp),
    )


def _copy(component: Event) -> Event:
    return component.copy(recursive=True)


def _same_content(entry: InboxEntry, component: Event, cancelled: bool) -> bool:
    return entry.cancelled == cancelled and entry.component == component


class ITipInbox:
    """Merge iTIP REQUEST/CANCEL messages into a versioned state.

    The inbox never mutates the calendars it is given; components are
    copied before they are stored.  The state can be serialized with
    :meth:`to_json` and restored with :meth:`from_json`.
    """

    def __init__(self, entries: dict[ComponentKey, InboxEntry] | None = None) -> None:
        self._entries: dict[ComponentKey, InboxEntry] = dict(entries or {})

    def apply(self, message: Calendar) -> list[ITipResult]:
        """File one iTIP message and return one result per VEVENT in it.

        Raises:
            UnsupportedITipMessage: for methods other than REQUEST/CANCEL
                (in particular REPLY and COUNTER) and for
                RANGE=THISANDFUTURE recurrence ids.
            InvalidITipMessage: if the message cannot be filed at all.
        """
        method = message.get("METHOD")
        if method is None:
            raise InvalidITipMessage("iTIP message is missing its METHOD")
        method = str(method).upper()
        if method not in ("REQUEST", "CANCEL"):
            raise UnsupportedITipMessage(f"iTIP METHOD {method} is not supported")
        cancelled = method == "CANCEL"
        results = [
            self._apply_component(component, cancelled)
            for component in message.walk("VEVENT")
        ]
        if not results:
            raise InvalidITipMessage("iTIP message contains no VEVENT component")
        return results

    def _apply_component(self, component: Event, cancelled: bool) -> ITipResult:
        key = _component_key(component)
        version = _component_version(component)
        entry = self._entries.get(key)
        if entry is None:
            self._entries[key] = InboxEntry(
                key=key,
                sequence=version.sequence,
                dtstamp=version.dtstamp,
                cancelled=cancelled,
                component=_copy(component),
            )
            return ITipResult(ApplyStatus.APPLIED, key, "new component")
        if version < entry.version:
            return ITipResult(
                ApplyStatus.IGNORED,
                key,
                f"stale version: SEQUENCE={version.sequence} "
                f"superseded by SEQUENCE={entry.sequence}",
            )
        if version == entry.version:
            if _same_content(entry, component, cancelled):
                return ITipResult(ApplyStatus.IGNORED, key, "duplicate message")
            entry.conflicts.append(
                ConflictRecord(
                    reason=(
                        "same SEQUENCE and DTSTAMP but different content "
                        f"(SEQUENCE={version.sequence})"
                    ),
                    component=_copy(component),
                )
            )
            return ITipResult(
                ApplyStatus.CONFLICT, key, "same version, different content"
            )
        entry.sequence = version.sequence
        entry.dtstamp = version.dtstamp
        entry.cancelled = cancelled
        entry.component = _copy(component)
        entry.conflicts.clear()
        return ITipResult(ApplyStatus.APPLIED, key, "newer version")

    @property
    def entries(self) -> list[InboxEntry]:
        """All known entries, including tombstones, sorted by key."""
        return [
            self._entries[key]
            for key in sorted(
                self._entries,
                key=lambda k: (k.uid, k.organizer, k.recurrence_id or ""),
            )
        ]

    @property
    def events(self) -> list[Event]:
        """Copies of the currently effective (not cancelled) components."""
        return [
            _copy(entry.component)
            for entry in self.entries
            if not entry.cancelled and entry.component is not None
        ]

    @property
    def tombstones(self) -> list[InboxEntry]:
        """Cancelled entries, kept for their version memory."""
        return [entry for entry in self.entries if entry.cancelled]

    @property
    def conflicts(self) -> list[InboxEntry]:
        """Entries that have recorded same-version content conflicts."""
        return [entry for entry in self.entries if entry.conflicts]

    def to_dict(self) -> dict[str, Any]:
        """Serialize the state, sorted by key so the output is canonical."""

        def entry_dict(entry: InboxEntry) -> dict[str, Any]:
            return {
                "uid": entry.key.uid,
                "organizer": entry.key.organizer,
                "recurrence_id": entry.key.recurrence_id,
                "sequence": entry.sequence,
                "dtstamp": entry.dtstamp.isoformat() if entry.dtstamp else None,
                "cancelled": entry.cancelled,
                "component": (
                    entry.component.to_ical().decode("utf-8")
                    if entry.component is not None
                    else None
                ),
                "conflicts": [
                    {
                        "reason": conflict.reason,
                        "component": conflict.component.to_ical().decode("utf-8"),
                    }
                    for conflict in entry.conflicts
                ],
            }

        return {"entries": [entry_dict(entry) for entry in self.entries]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ITipInbox:
        """Restore a state previously serialized with :meth:`to_dict`."""
        entries: dict[ComponentKey, InboxEntry] = {}
        for entry_data in data.get("entries", []):
            key = ComponentKey(
                uid=entry_data["uid"],
                organizer=entry_data["organizer"],
                recurrence_id=entry_data["recurrence_id"],
            )
            component_data = entry_data["component"]
            entries[key] = InboxEntry(
                key=key,
                sequence=int(entry_data["sequence"]),
                dtstamp=(
                    datetime.fromisoformat(entry_data["dtstamp"])
                    if entry_data["dtstamp"]
                    else None
                ),
                cancelled=bool(entry_data["cancelled"]),
                component=(Event.from_ical(component_data) if component_data else None),
                conflicts=[
                    ConflictRecord(
                        reason=conflict["reason"],
                        component=Event.from_ical(conflict["component"]),
                    )
                    for conflict in entry_data.get("conflicts", [])
                ],
            )
        return cls(entries)

    def to_json(self) -> str:
        """Serialize the state to a JSON string."""
        return json.dumps(self.to_dict(), sort_keys=True)

    @classmethod
    def from_json(cls, payload: str | bytes) -> ITipInbox:
        """Restore a state previously serialized with :meth:`from_json`."""
        return cls.from_dict(json.loads(payload))
