"""
Event poll components from :rfc:`9073`.

``VPOLL`` groups ``POLL-ITEM`` candidates and ``VOTER`` records, and
``Poll.tally`` counts the votes without modifying the poll.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum, auto
from typing import TYPE_CHECKING

from icalendar.attr import (
    create_single_property,
    duration_property,
    organizer_property,
    rfc_7953_duration_property,
    sequence_property,
    single_string_enum_property,
    status_property,
    summary_property,
    uid_property,
)
from icalendar.cal.component import Component
from icalendar.enums import POLLMODE, POLLSTATUS, STATUS
from icalendar.error import InvalidCalendar

if TYPE_CHECKING:
    from collections.abc import Sequence

    from icalendar.compatibility import Self


def _optional_int_property(name: str, docs: str) -> property:
    """Create a property for an integer value that occurs at most once."""

    def fget(self: Component) -> int | None:
        """Get the value or None if it is not set."""
        value = self.get(name)
        if value is None or value == []:
            return None
        if isinstance(value, list):
            raise InvalidCalendar(f"Multiple {name} defined.")
        try:
            return int(value)
        except (TypeError, ValueError) as e:
            raise InvalidCalendar(f"{name} must be an integer, not {value!r}") from e

    def fset(self: Component, value: int | None) -> None:
        """Set the value. Setting it to None deletes the property."""
        fdel(self)
        if value is None:
            return
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError(f"{name} must be an int, got {value!r}")
        self.add(name, value)

    def fdel(self: Component) -> None:
        """Delete the value."""
        self.pop(name, None)

    return property(fget, fset, fdel, doc=docs)


poll_status_property = single_string_enum_property(
    "STATUS",
    POLLSTATUS,
    POLLSTATUS.CONFIRMED,
    "The status of the poll as a POLLSTATUS enum. Unset means CONFIRMED.",
)

poll_mode_property = single_string_enum_property(
    "POLL-MODE",
    POLLMODE,
    POLLMODE.BASIC,
    "The poll mode as a POLLMODE enum. Unset means BASIC.",
)


class DuplicateVotePolicy(Enum):
    """How :meth:`Poll.tally` handles VOTER components with the same UID."""

    OVERRIDE = auto()
    """The record with the highest SEQUENCE replaces the earlier ones."""

    REJECT = auto()
    """Every record of a voter who voted more than once is refused."""


class UncountedReason(Enum):
    """Why a single vote could not be counted by :meth:`Poll.tally`."""

    POLL_CANCELLED = auto()
    """The poll itself is cancelled, so no vote counts."""

    POLL_ITEM_UNKNOWN = auto()
    """The vote refers to a POLL-ITEM-ID that is not a candidate."""

    POLL_ITEM_CANCELLED = auto()
    """The candidate is cancelled and cannot receive votes."""

    VOTER_MISSING_UID = auto()
    """The VOTER component has no UID and cannot be attributed."""

    VOTE_MISSING_RESPONSE = auto()
    """The vote entry has no RESPONSE value."""

    VOTE_INVALID_RESPONSE = auto()
    """The RESPONSE value is not a positive integer."""

    DUPLICATE_VOTER = auto()
    """The voter cast several records and the policy rejects duplicates."""

    SUPERSEDED_VOTE = auto()
    """A newer record of the same voter overrides this one."""


class ParticipantStatus(Enum):
    """The tallied state of one voter."""

    COUNTED = auto()
    """All votes of this voter were counted."""

    PARTIALLY_COUNTED = auto()
    """Some votes of this voter were counted, others were not."""

    REJECTED = auto()
    """All votes were refused because of duplicate records."""

    INVALID = auto()
    """No vote of this voter could be counted."""


@dataclass(frozen=True)
class VoteRecord:
    """One vote cast by a voter for one candidate."""

    voter_uid: str | None
    poll_item_id: int | None
    response: int | None
    sequence: int


@dataclass(frozen=True)
class UncountedVote:
    """A vote that could not be counted, with the reason why."""

    voter_uid: str | None
    poll_item_id: int | None
    reason: UncountedReason
    detail: str = ""


@dataclass
class PollTally:
    """The result of :meth:`Poll.tally`.

    Attributes:
        poll_uid: The UID of the tallied poll.
        poll_status: The POLLSTATUS of the tallied poll.
        duplicate_policy: The policy that was used for duplicate voters.
        counts: The counted votes per candidate, keyed by POLL-ITEM-ID.
        participants: The ParticipantStatus of each voter, keyed by voter UID.
        sequences: The original SEQUENCE numbers seen for each voter UID.
        uncounted: The votes that could not be counted, with reasons.
    """

    poll_uid: str
    poll_status: POLLSTATUS
    duplicate_policy: DuplicateVotePolicy
    counts: dict[int, int]
    participants: dict[str, ParticipantStatus]
    sequences: dict[str, tuple[int, ...]]
    uncounted: list[UncountedVote] = field(default_factory=list)

    @property
    def total(self) -> int:
        """The total number of counted votes."""
        return sum(self.counts.values())

    def count_for(self, poll_item_id: int) -> int:
        """Return the number of counted votes for one candidate.

        Parameters:
            poll_item_id: The POLL-ITEM-ID of the candidate.
        """
        return self.counts.get(poll_item_id, 0)

    def uncounted_for(self, voter_uid: str | None) -> list[UncountedVote]:
        """Return the votes of one voter that could not be counted.

        Parameters:
            voter_uid: The UID of the voter, or None for anonymous records.
        """
        return [vote for vote in self.uncounted if vote.voter_uid == voter_uid]


class PollItem(Component):
    """A ``POLL-ITEM`` component from :rfc:`9073`.

    Inside a :class:`Poll`, a ``POLL-ITEM`` is a candidate that voters can
    choose: either a text option (``SUMMARY``) or a time option (``DTSTART``
    with ``DTEND`` or ``DURATION``). Inside a :class:`Voter`, a ``POLL-ITEM``
    records one vote and carries the ``POLL-ITEM-ID`` of the chosen candidate
    together with the ``RESPONSE`` value.
    """

    name = "POLL-ITEM"

    required = ("POLL-ITEM-ID",)
    singletons = (
        "POLL-ITEM-ID",
        "RESPONSE",
        "SUMMARY",
        "STATUS",
        "DTSTART",
        "DTEND",
        "DURATION",
        "DTSTAMP",
        "SEQUENCE",
    )

    poll_item_id = POLL_ITEM_ID = _optional_int_property(
        "POLL-ITEM-ID",
        "The stable integer identifier of this candidate, unique within its poll.",
    )
    response = RESPONSE = _optional_int_property(
        "RESPONSE",
        "The vote value of this entry. Only meaningful inside a VOTER component.",
    )
    summary = summary_property
    status = status_property
    sequence = sequence_property
    start = DTSTART = create_single_property(
        "DTSTART",
        "dt",
        (datetime, date),
        date,
        "The inclusive start of a time-based candidate.",
    )
    end = DTEND = create_single_property(
        "DTEND",
        "dt",
        (datetime, date),
        date,
        "The non-inclusive end of a time-based candidate.",
    )
    DURATION = duration_property("POLL-ITEM")
    duration = rfc_7953_duration_property

    @property
    def is_time_option(self) -> bool:
        """Whether this candidate is a point in time."""
        return self.DTSTART is not None

    @property
    def is_text_option(self) -> bool:
        """Whether this candidate is a text option."""
        return self.DTSTART is None and bool(self.summary)

    @property
    def is_active(self) -> bool:
        """Whether this candidate can receive votes.

        A candidate whose STATUS is CANCELLED cannot receive votes.
        """
        return self.status != STATUS.CANCELLED

    @classmethod
    def new(
        cls,
        /,
        poll_item_id: int | None = None,
        summary: str | None = None,
        start: date | datetime | None = None,
        end: date | datetime | None = None,
        duration: timedelta | None = None,
        response: int | None = None,
        status: str | STATUS | None = None,
    ) -> Self:
        """Create a new POLL-ITEM component.

        Parameters:
            poll_item_id: The stable :attr:`poll_item_id` of the candidate.
            summary: The text of a text option.
            start: The start of a time option.
            end: The end of a time option.
            duration: The duration of a time option, alternative to ``end``.
            response: The vote value if this entry records a vote.
            status: The :attr:`status` of the candidate.

        Returns:
            The new PollItem.
        """
        item = cls()
        item.poll_item_id = poll_item_id
        item.summary = summary
        item.DTSTART = start
        item.DTEND = end
        item.DURATION = duration
        item.response = response
        if status is not None:
            item.status = status
        return item


class Voter(Component):
    """A ``VOTER`` component from :rfc:`9073`.

    The ``UID`` identifies the voter, ``SEQUENCE`` versions the record,
    and each ``POLL-ITEM`` subcomponent records one vote.
    """

    name = "VOTER"

    required = ("UID", "DTSTAMP")
    singletons = (
        "UID",
        "DTSTAMP",
        "SEQUENCE",
        "PUBLIC-COMMENT",
        "STAY-INFORMED",
    )

    uid = uid_property
    sequence = sequence_property

    @property
    def votes(self) -> list[PollItem]:
        """The votes cast by this voter as POLL-ITEM subcomponents."""
        return [c for c in self.subcomponents if isinstance(c, PollItem)]

    def vote_records(self) -> list[VoteRecord]:
        """The votes of this voter as immutable records.

        Returns:
            One VoteRecord per POLL-ITEM subcomponent.
        """
        uid = str(self.uid) if self.uid else None
        return [
            VoteRecord(
                voter_uid=uid,
                poll_item_id=vote.poll_item_id,
                response=vote.response,
                sequence=self.sequence,
            )
            for vote in self.votes
        ]

    @classmethod
    def new(
        cls,
        /,
        uid: str | None = None,
        votes: Sequence[PollItem | tuple[int, int]] = (),
        sequence: int | None = None,
        stamp: datetime | None = None,
    ) -> Self:
        """Create a new VOTER component.

        Parameters:
            uid: The UID identifying the voter. Generated if not given.
            votes: PollItem vote entries or (poll_item_id, response) tuples.
            sequence: The SEQUENCE of this record, defaulting to 0.
            stamp: The DTSTAMP of this record, defaulting to now.

        Returns:
            The new Voter.
        """
        voter = cls()
        voter.uid = uid if uid is not None else str(uuid.uuid4())
        voter.DTSTAMP = stamp if stamp is not None else cls._utc_now()
        if sequence is not None:
            voter.sequence = sequence
        for vote in votes:
            if not isinstance(vote, PollItem):
                vote = PollItem.new(poll_item_id=vote[0], response=vote[1])
            voter.add_component(vote)
        return voter


class Poll(Component):
    """A ``VPOLL`` component from :rfc:`9073`.

    A poll groups candidates (:class:`PollItem` subcomponents) and the votes
    cast for them (:class:`Voter` subcomponents). Use :meth:`tally` to count
    the votes without modifying the poll.
    """

    name = "VPOLL"

    required = ("UID", "DTSTAMP")
    singletons = (
        "UID",
        "DTSTAMP",
        "DTSTART",
        "DTEND",
        "DURATION",
        "ORGANIZER",
        "SUMMARY",
        "STATUS",
        "POLL-MODE",
        "POLL-WINNER",
    )

    uid = uid_property
    organizer = organizer_property
    summary = summary_property
    status = poll_status_property
    poll_mode = POLL_MODE = poll_mode_property
    start = DTSTART = create_single_property(
        "DTSTART",
        "dt",
        (datetime, date),
        date,
        "The inclusive start of the voting period.",
    )
    end = DTEND = create_single_property(
        "DTEND",
        "dt",
        (datetime, date),
        date,
        "The non-inclusive end of the voting period.",
    )
    DURATION = duration_property("VPOLL")
    duration = rfc_7953_duration_property

    @property
    def poll_items(self) -> list[PollItem]:
        """The candidates of this poll.

        These are the direct POLL-ITEM subcomponents. The POLL-ITEM entries
        inside VOTER components are votes, not candidates.
        """
        return [c for c in self.subcomponents if isinstance(c, PollItem)]

    @property
    def voters(self) -> list[Voter]:
        """The VOTER subcomponents of this poll."""
        return [c for c in self.subcomponents if isinstance(c, Voter)]

    def get_poll_item(self, poll_item_id: int) -> PollItem | None:
        """Return the candidate with this POLL-ITEM-ID, if any.

        Parameters:
            poll_item_id: The POLL-ITEM-ID of the candidate.

        Returns:
            The matching PollItem or None.
        """
        for item in self.poll_items:
            if item.poll_item_id == poll_item_id:
                return item
        return None

    @property
    def is_closed(self) -> bool:
        """Whether the poll is closed and no longer accepts votes."""
        return self.status == POLLSTATUS.CLOSED

    @property
    def is_cancelled(self) -> bool:
        """Whether the poll is cancelled and its votes do not count."""
        return self.status == POLLSTATUS.CANCELLED

    @classmethod
    def new(
        cls,
        /,
        poll_items: Sequence[PollItem] = (),
        voters: Sequence[Voter] = (),
        uid: str | None = None,
        stamp: datetime | None = None,
        summary: str | None = None,
        start: date | datetime | None = None,
        end: date | datetime | None = None,
        duration: timedelta | None = None,
        organizer: str | None = None,
        status: str | POLLSTATUS | None = None,
        poll_mode: str | POLLMODE | None = None,
    ) -> Self:
        """Create a new VPOLL component.

        Parameters:
            poll_items: The candidates of the poll.
            voters: The VOTER subcomponents of the poll.
            uid: The UID of the poll. Generated if not given.
            stamp: The DTSTAMP of the poll, defaulting to now.
            summary: The question asked by the poll.
            start: The start of the voting period.
            end: The end of the voting period.
            duration: The length of the voting period, alternative to ``end``.
            organizer: The organizer of the poll as a CAL-ADDRESS.
            status: The STATUS of the poll, see POLLSTATUS.
            poll_mode: The POLL-MODE of the poll, see POLLMODE.

        Returns:
            The new Poll.
        """
        cls._validate_start_and_end(start, end)
        poll = cls()
        poll.uid = uid if uid is not None else str(uuid.uuid4())
        poll.DTSTAMP = stamp if stamp is not None else cls._utc_now()
        poll.summary = summary
        poll.DTSTART = start
        poll.DTEND = end
        poll.DURATION = duration
        poll.organizer = organizer
        if status is not None:
            poll.status = status
        if poll_mode is not None:
            poll.poll_mode = poll_mode
        for item in poll_items:
            poll.add_component(item)
        for voter in voters:
            poll.add_component(voter)
        return poll

    def tally(
        self, duplicate_policy: DuplicateVotePolicy = DuplicateVotePolicy.OVERRIDE
    ) -> PollTally:
        """Count the votes of this poll without modifying it.

        Parameters:
            duplicate_policy: How to handle several VOTER components with the
                same UID. With ``DuplicateVotePolicy.OVERRIDE``, the record
                with the highest SEQUENCE replaces the earlier ones. With
                ``DuplicateVotePolicy.REJECT``, all records of such a voter
                are refused.

        Returns:
            A PollTally with the vote count per candidate, the status of each
            participant, the original SEQUENCE numbers seen per voter, and
            the votes that could not be counted with the reasons why.
        """
        if not isinstance(duplicate_policy, DuplicateVotePolicy):
            raise TypeError(
                "duplicate_policy must be a DuplicateVotePolicy, "
                f"not {duplicate_policy!r}"
            )
        candidates: dict[int, PollItem] = {}
        for item in self.poll_items:
            if item.poll_item_id is not None:
                candidates.setdefault(item.poll_item_id, item)
        counts = {poll_item_id: 0 for poll_item_id in candidates}
        participants: dict[str, ParticipantStatus] = {}
        sequences: dict[str, tuple[int, ...]] = {}
        uncounted: list[UncountedVote] = []

        records_by_uid: dict[str, list[Voter]] = {}
        for voter in self.voters:
            uid = str(voter.uid).strip() if voter.uid else ""
            if not uid:
                uncounted.extend(
                    UncountedVote(
                        voter_uid=None,
                        poll_item_id=vote.poll_item_id,
                        reason=UncountedReason.VOTER_MISSING_UID,
                        detail="VOTER component without a UID",
                    )
                    for vote in voter.votes
                )
                continue
            records_by_uid.setdefault(uid, []).append(voter)

        cancelled = self.is_cancelled
        for uid, records in records_by_uid.items():
            original_sequences = tuple(record.sequence for record in records)
            sequences[uid] = original_sequences
            if len(records) > 1:
                if duplicate_policy is DuplicateVotePolicy.REJECT:
                    for record in records:
                        uncounted.extend(
                            UncountedVote(
                                voter_uid=uid,
                                poll_item_id=vote.poll_item_id,
                                reason=UncountedReason.DUPLICATE_VOTER,
                                detail=(
                                    f"voter cast {len(records)} records "
                                    f"with SEQUENCE {original_sequences}"
                                ),
                            )
                            for vote in record.votes
                        )
                    participants[uid] = ParticipantStatus.REJECTED
                    continue
                ordered = sorted(
                    records,
                    key=lambda record: (
                        record.sequence,
                        record.stamp or datetime.min.replace(tzinfo=timezone.utc),
                    ),
                )
                effective = ordered[-1]
                for record in ordered[:-1]:
                    uncounted.extend(
                        UncountedVote(
                            voter_uid=uid,
                            poll_item_id=vote.poll_item_id,
                            reason=UncountedReason.SUPERSEDED_VOTE,
                            detail=(
                                "superseded by record with "
                                f"SEQUENCE {effective.sequence}"
                            ),
                        )
                        for vote in record.votes
                    )
                records = [effective]

            counted = 0
            failed = 0
            for record in records:
                for vote in record.votes:
                    poll_item_id = vote.poll_item_id
                    response = vote.response
                    if cancelled:
                        reason = UncountedReason.POLL_CANCELLED
                        detail = "the poll is cancelled"
                    elif poll_item_id is None or poll_item_id not in candidates:
                        reason = UncountedReason.POLL_ITEM_UNKNOWN
                        detail = f"no candidate with POLL-ITEM-ID {poll_item_id}"
                    elif not candidates[poll_item_id].is_active:
                        reason = UncountedReason.POLL_ITEM_CANCELLED
                        detail = f"candidate {poll_item_id} is cancelled"
                    elif response is None:
                        reason = UncountedReason.VOTE_MISSING_RESPONSE
                        detail = "the vote entry has no RESPONSE"
                    elif response < 1:
                        reason = UncountedReason.VOTE_INVALID_RESPONSE
                        detail = f"RESPONSE must be positive, not {response}"
                    else:
                        counts[poll_item_id] += 1
                        counted += 1
                        continue
                    failed += 1
                    uncounted.append(
                        UncountedVote(
                            voter_uid=uid,
                            poll_item_id=poll_item_id,
                            reason=reason,
                            detail=detail,
                        )
                    )
            if counted and failed:
                participants[uid] = ParticipantStatus.PARTIALLY_COUNTED
            elif counted:
                participants[uid] = ParticipantStatus.COUNTED
            else:
                participants[uid] = ParticipantStatus.INVALID

        return PollTally(
            poll_uid=str(self.uid),
            poll_status=self.status,
            duplicate_policy=duplicate_policy,
            counts=counts,
            participants=participants,
            sequences=sequences,
            uncounted=uncounted,
        )


__all__ = [
    "DuplicateVotePolicy",
    "ParticipantStatus",
    "Poll",
    "PollItem",
    "PollTally",
    "UncountedReason",
    "UncountedVote",
    "VoteRecord",
    "Voter",
]
