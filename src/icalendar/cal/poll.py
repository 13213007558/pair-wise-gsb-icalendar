"""RFC 9073 polling components."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import TYPE_CHECKING, Any

from icalendar.attr import create_single_property
from icalendar.enums import (
    DUPLICATE_RESPONSE_POLICY,
    POLL_ITEM_STATUS,
    POLL_STATUS,
    RESPONSE,
    VOTER_STATUS,
)

from .component import Component

if TYPE_CHECKING:
    from icalendar.prop import vCalAddress


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        return _text(value[0]) if value else None
    return str(value)


def _enum_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, Enum):
        return value.value
    text = _text(value)
    return None if text == "" else text


def _enum_or_raw(enum: type[Enum], value: Any) -> Enum | str | None:
    text = _enum_value(value)
    if text is None:
        return None
    try:
        return enum(text)
    except ValueError:
        return text


def _values(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


class PollItem(Component):
    """A candidate in a :class:`VPoll` component."""

    name = "POLL-ITEM"
    canonical_order = (
        "POLL-ITEM-ID",
        "SUMMARY",
        "DESCRIPTION",
        "DTSTART",
        "DTEND",
        "DURATION",
        "STATUS",
        "UID",
        "DTSTAMP",
    )
    singletons = (
        "POLL-ITEM-ID",
        "SUMMARY",
        "DESCRIPTION",
        "DTSTART",
        "DTEND",
        "DURATION",
        "STATUS",
        "UID",
        "DTSTAMP",
    )
    exclusive = ("DTEND", "DURATION")

    DTSTART = create_single_property(
        "DTSTART",
        "dt",
        (datetime, date, time),
        date | datetime | time,
        "The date, date-time, or time represented by a candidate.",
    )
    DTEND = create_single_property(
        "DTEND",
        "dt",
        (datetime, date, time),
        date | datetime | time,
        "The end represented by a time-based candidate.",
    )
    DURATION = create_single_property(
        "DURATION",
        "dt",
        (timedelta,),
        timedelta,
        "The duration represented by a time-based candidate.",
    )

    @property
    def poll_item_id(self) -> str:
        """Stable identifier of the candidate."""
        return _text(self.get("POLL-ITEM-ID")) or ""

    @poll_item_id.setter
    def poll_item_id(self, value: str | None) -> None:
        self.pop("POLL-ITEM-ID", None)
        if value is not None:
            self.add("POLL-ITEM-ID", value)

    @poll_item_id.deleter
    def poll_item_id(self) -> None:
        self.pop("POLL-ITEM-ID", None)

    start = DTSTART
    end = DTEND
    duration = DURATION

    @property
    def option(self) -> str | date | datetime | time | None:
        """The text or temporal value offered by this candidate."""
        if "SUMMARY" in self:
            return _text(self.get("SUMMARY"))
        return self.start

    @property
    def status(self) -> POLL_ITEM_STATUS | str | None:
        """Structured candidate status, or a raw unregistered IANA value."""
        return _enum_or_raw(POLL_ITEM_STATUS, self.get("STATUS"))

    @status.setter
    def status(self, value: POLL_ITEM_STATUS | str | None) -> None:
        self.pop("STATUS", None)
        if value is not None:
            self.add("STATUS", _enum_value(value))


class Voter(Component):
    """A voter and that voter's responses inside a :class:`VPoll`."""

    name = "VOTER"
    canonical_order = (
        "UID",
        "ATTENDEE",
        "RESPONSE",
        "DTSTAMP",
    )
    singletons = (
        "UID",
        "ATTENDEE",
        "DTSTAMP",
    )
    multiple = ("RESPONSE",)

    @property
    def address(self) -> vCalAddress | None:
        """The calendar address identifying this voter."""
        address = self.get("ATTENDEE")
        if isinstance(address, list):
            return address[0] if address else None
        return address

    @address.setter
    def address(self, value: vCalAddress | str | None) -> None:
        self.pop("ATTENDEE", None)
        if value is not None:
            self.add("ATTENDEE", value)

    @property
    def voter_id(self) -> str:
        """Stable identifier of the voter, preferring ``UID`` over address."""
        return _text(self.get("UID")) or _text(self.address) or ""

    @property
    def participant_status(self) -> VOTER_STATUS | str | None:
        """The voter's participation status."""
        address = self.address
        if address is None or not hasattr(address, "params"):
            return None
        return _enum_or_raw(VOTER_STATUS, address.params.get("PARTSTAT"))

    @participant_status.setter
    def participant_status(self, value: VOTER_STATUS | str | None) -> None:
        address = self.address
        if address is None:
            raise ValueError("Set the voter's ATTENDEE before setting PARTSTAT.")
        if value is None:
            address.params.pop("PARTSTAT", None)
        else:
            address.params["PARTSTAT"] = _enum_value(value)

    def responses(self) -> list[tuple[str, RESPONSE | str, int, Any]]:
        """Return candidate ID, response, original source sequence, and property."""
        result = []
        for sequence, response in enumerate(_values(self.get("RESPONSE"))):
            candidate_id = str(response.params.get("POLL-ITEM-ID", ""))
            result.append(
                (
                    candidate_id,
                    _enum_or_raw(RESPONSE, response),
                    sequence,
                    response,
                )
            )
        return result


@dataclass(frozen=True)
class VoteRecord:
    """One response retained or rejected while tallying a poll."""

    voter_id: str
    candidate_id: str
    response: RESPONSE | str | None
    participant_status: VOTER_STATUS | str | None
    original_sequence: int
    counted: bool
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation."""
        return {
            "voter_id": self.voter_id,
            "candidate_id": self.candidate_id,
            "response": _enum_value(self.response),
            "participant_status": _enum_value(self.participant_status),
            "original_sequence": self.original_sequence,
            "counted": self.counted,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class PollItemTally:
    """The tally and validity state of one candidate."""

    id: str
    option: str | date | datetime | time | None
    status: POLL_ITEM_STATUS | str | None
    yes: int = 0
    no: int = 0
    abstentions: int = 0
    participants: dict[str, VOTER_STATUS | str | None] = field(
        default_factory=dict
    )
    votes: list[VoteRecord] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def vote_count(self) -> int:
        """Number of affirmative votes."""
        return self.yes

    @property
    def is_countable(self) -> bool:
        """Whether this candidate can receive votes."""
        return not self.reasons and self.status in (None, POLL_ITEM_STATUS.ACTIVE)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation."""
        return {
            "id": self.id,
            "option": self.option,
            "status": _enum_value(self.status),
            "yes": self.yes,
            "no": self.no,
            "abstentions": self.abstentions,
            "vote_count": self.vote_count,
            "participants": {
                voter: _enum_value(status)
                for voter, status in self.participants.items()
            },
            "votes": [vote.as_dict() for vote in self.votes],
            "reasons": list(self.reasons),
            "countable": self.is_countable,
        }


@dataclass(frozen=True)
class PollSummary:
    """Aggregated result of a :class:`VPoll`."""

    uid: str
    status: POLL_STATUS | str | None
    items: list[PollItemTally]
    votes: list[VoteRecord]
    reasons: list[str]

    @property
    def is_cancelled(self) -> bool:
        """Whether the poll is cancelled."""
        return self.status is POLL_STATUS.CANCELLED

    @property
    def is_closed(self) -> bool:
        """Whether votes can no longer be changed."""
        return self.status in (POLL_STATUS.COMPLETED, POLL_STATUS.CANCELLED)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation."""
        return {
            "uid": self.uid,
            "status": _enum_value(self.status),
            "cancelled": self.is_cancelled,
            "closed": self.is_closed,
            "items": [item.as_dict() for item in self.items],
            "votes": [vote.as_dict() for vote in self.votes],
            "reasons": list(self.reasons),
        }


class VPoll(Component):
    """A ``VPOLL`` polling component defined by :rfc:`9073`."""

    name = "VPOLL"
    canonical_order = (
        "UID",
        "DTSTAMP",
        "DTSTART",
        "DTEND",
        "DURATION",
        "STATUS",
        "SUMMARY",
        "DESCRIPTION",
        "ORGANIZER",
    )
    singletons = (
        "UID",
        "DTSTAMP",
        "DTSTART",
        "DTEND",
        "DURATION",
        "STATUS",
        "SUMMARY",
        "DESCRIPTION",
        "ORGANIZER",
    )
    exclusive = ("DTEND", "DURATION")
    multiple = (
        "COMMENT",
        "ATTACH",
        "CATEGORIES",
    )

    DTSTART = create_single_property(
        "DTSTART",
        "dt",
        (datetime, date, time),
        date | datetime | time,
        "The inclusive start of the poll.",
    )
    DTEND = create_single_property(
        "DTEND",
        "dt",
        (datetime, date, time),
        date | datetime | time,
        "The non-inclusive end of the poll.",
    )
    DURATION = create_single_property(
        "DURATION",
        "dt",
        (timedelta,),
        timedelta,
        "The duration of the poll.",
    )

    start = DTSTART
    end = DTEND
    duration = DURATION

    @property
    def poll_items(self) -> list[PollItem]:
        """Candidate components in source order."""
        return [
            component
            for component in self.subcomponents
            if component.name == "POLL-ITEM"
        ]

    @property
    def voters(self) -> list[Voter]:
        """Voter components in source order."""
        return [
            component
            for component in self.subcomponents
            if component.name == "VOTER"
        ]

    @property
    def status(self) -> POLL_STATUS | str | None:
        """Structured poll status, or a raw unregistered IANA value."""
        return _enum_or_raw(POLL_STATUS, self.get("STATUS"))

    @status.setter
    def status(self, value: POLL_STATUS | str | None) -> None:
        self.pop("STATUS", None)
        if value is not None:
            self.add("STATUS", _enum_value(value))

    @property
    def is_cancelled(self) -> bool:
        """Whether this poll has the structured cancelled state."""
        return self.status is POLL_STATUS.CANCELLED

    @property
    def is_closed(self) -> bool:
        """Whether this poll is completed or cancelled."""
        return self.status in (POLL_STATUS.COMPLETED, POLL_STATUS.CANCELLED)

    def tally_votes(
        self,
        duplicate_response_policy: DUPLICATE_RESPONSE_POLICY | str,
    ) -> PollSummary:
        """Summarize all candidates without modifying this VPOLL."""
        policy = DUPLICATE_RESPONSE_POLICY(_enum_value(duplicate_response_policy))
        seen_ids: set[str] = set()
        duplicate_ids: set[str] = set()
        for item in self.poll_items:
            item_id = item.poll_item_id
            if not item_id:
                continue
            if item_id in seen_ids:
                duplicate_ids.add(item_id)
            seen_ids.add(item_id)

        tallies: dict[str, PollItemTally] = {}
        for item in self.poll_items:
            item_id = item.poll_item_id
            reasons = []
            if not item_id:
                reasons.append("POLL-ITEM has no POLL-ITEM-ID.")
            if item_id in duplicate_ids:
                reasons.append("POLL-ITEM-ID is not unique.")
            item_status = item.status
            if item_status not in (None, POLL_ITEM_STATUS.ACTIVE):
                if item_status is POLL_ITEM_STATUS.CANCELLED:
                    reasons.append("POLL-ITEM is cancelled.")
                elif item_status is POLL_ITEM_STATUS.COMPLETED:
                    reasons.append("POLL-ITEM is completed.")
                else:
                    reasons.append(f"Unknown POLL-ITEM status: {item_status}")
            tally = PollItemTally(
                id=item_id,
                option=item.option,
                status=item_status,
                reasons=reasons,
            )
            if item_id:
                tallies[item_id] = tally

        all_by_key: dict[tuple[str, str], list[VoteRecord]] = {}
        poll_status = self.status
        if poll_status is not None and not isinstance(poll_status, POLL_STATUS):
            poll_reason = f"Unknown VPOLL status: {poll_status}"
        elif poll_status is POLL_STATUS.CANCELLED:
            poll_reason = "VPOLL is cancelled."
        elif poll_status is POLL_STATUS.COMPLETED:
            poll_reason = "VPOLL is closed."
        else:
            poll_reason = None

        for voter in self.voters:
            voter_id = voter.voter_id
            participant_status = voter.participant_status
            voter_reason = None if voter.address is not None else "VOTER has no ATTENDEE."
            for candidate_id, response_value, sequence, _property in voter.responses():
                reasons = []
                if voter_reason:
                    reasons.append(voter_reason)
                if poll_reason:
                    reasons.append(poll_reason)
                if candidate_id not in tallies:
                    reasons.append("RESPONSE does not refer to a POLL-ITEM-ID.")
                elif tallies[candidate_id].reasons:
                    reasons.append("POLL-ITEM is not countable.")
                if response_value not in tuple(RESPONSE):
                    reasons.append(f"Unknown RESPONSE value: {response_value}")

                record = VoteRecord(
                    voter_id=voter_id,
                    candidate_id=candidate_id,
                    response=response_value,
                    participant_status=participant_status,
                    original_sequence=sequence,
                    counted=False,
                    reason=" ".join(reasons) if reasons else None,
                )
                key = (voter_id, candidate_id)
                all_by_key.setdefault(key, []).append(record)
        final_records: list[VoteRecord] = []
        accepted: set[tuple[str, str, int]] = set()
        for key, records in all_by_key.items():
            accepted_index = -1 if policy is DUPLICATE_RESPONSE_POLICY.OVERWRITE else 0
            if records:
                accepted.add(
                    (
                        key[0],
                        key[1],
                        records[accepted_index].original_sequence,
                    )
                )
            duplicate_reason = (
                "Duplicate RESPONSE overwritten by a later RESPONSE."
                if policy is DUPLICATE_RESPONSE_POLICY.OVERWRITE
                else "Duplicate RESPONSE rejected."
            )
            for index, record in enumerate(records):
                if len(records) > 1 and index != accepted_index:
                    reasons = [record.reason] if record.reason else []
                    reasons.append(duplicate_reason)
                    reason = " ".join(reasons)
                else:
                    reason = record.reason
                final_records.append(
                    VoteRecord(
                        voter_id=record.voter_id,
                        candidate_id=record.candidate_id,
                        response=record.response,
                        participant_status=record.participant_status,
                        original_sequence=record.original_sequence,
                        counted=reason is None,
                        reason=reason,
                    )
                )

        for record in final_records:
            tally = tallies.get(record.candidate_id)
            if tally is None:
                continue
            tally.votes.append(record)
            tally.participants[record.voter_id] = record.participant_status
            if not record.counted:
                continue
            if record.response is RESPONSE.YES:
                tally.yes += 1
            elif record.response is RESPONSE.NO:
                tally.no += 1
            elif record.response is RESPONSE.ABSTAIN:
                tally.abstentions += 1

        summary_reasons = []
        if poll_reason:
            summary_reasons.append(poll_reason)
        return PollSummary(
            uid=_text(self.get("UID")) or "",
            status=poll_status,
            items=list(tallies.values()),
            votes=final_records,
            reasons=summary_reasons,
        )


def summarize_poll(
    poll: VPoll,
    duplicate_response_policy: DUPLICATE_RESPONSE_POLICY | str,
) -> PollSummary:
    """Return a non-mutating :class:`PollSummary` for a VPOLL."""
    return poll.tally_votes(duplicate_response_policy)


__all__ = [
    "PollItem",
    "PollItemTally",
    "PollSummary",
    "VPoll",
    "VoteRecord",
    "Voter",
    "summarize_poll",
]
