"""Tests for the :rfc:`9073` VPOLL, VOTER and POLL-ITEM components.

The fixture poll has three voters, two candidates, one vote change
(a voter who casts a second record with a higher SEQUENCE) and one
invalid VOTER record without a UID.
"""

from datetime import datetime, timedelta, timezone

import pytest

from icalendar import POLLMODE, POLLSTATUS, STATUS, Calendar, Component
from icalendar.cal.poll import (
    DuplicateVotePolicy,
    ParticipantStatus,
    Poll,
    PollItem,
    UncountedReason,
    Voter,
)

POLL_ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Example//Poll//EN
BEGIN:VPOLL
UID:poll-42@example.com
DTSTAMP:20260924T080000Z
SUMMARY:Where should we go for lunch?
DTSTART:20260924T090000Z
DTEND:20260926T090000Z
ORGANIZER:mailto:boss@example.com
POLL-MODE:BASIC
X-POLL-CHANNEL;X-HOPS=2:main
BEGIN:POLL-ITEM
POLL-ITEM-ID:1
SUMMARY:Pizza place
END:POLL-ITEM
BEGIN:POLL-ITEM
POLL-ITEM-ID:2
SUMMARY:Sushi bar
X-CHEF:Yuki
END:POLL-ITEM
BEGIN:VOTER
UID:voter-alice@example.com
DTSTAMP:20260924T100000Z
BEGIN:POLL-ITEM
POLL-ITEM-ID:1
RESPONSE:1
END:POLL-ITEM
END:VOTER
BEGIN:VOTER
UID:voter-bob@example.com
DTSTAMP:20260924T101500Z
BEGIN:POLL-ITEM
POLL-ITEM-ID:2
RESPONSE:1
END:POLL-ITEM
END:VOTER
BEGIN:VOTER
UID:voter-carol@example.com
DTSTAMP:20260924T103000Z
SEQUENCE:0
BEGIN:POLL-ITEM
POLL-ITEM-ID:1
RESPONSE:1
END:POLL-ITEM
END:VOTER
BEGIN:VOTER
UID:voter-carol@example.com
DTSTAMP:20260924T110000Z
SEQUENCE:1
BEGIN:POLL-ITEM
POLL-ITEM-ID:2
RESPONSE:1
END:POLL-ITEM
END:VOTER
BEGIN:VOTER
DTSTAMP:20260924T104500Z
BEGIN:POLL-ITEM
POLL-ITEM-ID:2
RESPONSE:1
END:POLL-ITEM
END:VOTER
END:VPOLL
END:VCALENDAR
"""


@pytest.fixture()
def poll() -> Poll:
    """The parsed fixture poll."""
    calendar = Calendar.from_ical(POLL_ICS)
    polls = [c for c in calendar.subcomponents if isinstance(c, Poll)]
    assert len(polls) == 1
    return polls[0]


def test_parse_component_hierarchy(poll: Poll):
    """The parser creates dedicated classes and keeps the nesting."""
    assert poll.name == "VPOLL"
    assert poll.uid == "poll-42@example.com"
    assert poll.DTSTAMP == datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)
    assert poll.DTSTART == datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc)
    assert poll.DTEND == datetime(2026, 9, 26, 9, 0, tzinfo=timezone.utc)
    assert poll.duration == timedelta(days=2)
    assert str(poll.organizer) == "mailto:boss@example.com"
    assert poll.summary == "Where should we go for lunch?"
    assert poll.poll_mode == POLLMODE.BASIC

    items = poll.poll_items
    assert len(items) == 2
    assert all(isinstance(item, PollItem) for item in items)
    assert [item.poll_item_id for item in items] == [1, 2]
    assert [item.summary for item in items] == ["Pizza place", "Sushi bar"]
    assert all(item.is_text_option for item in items)
    assert not any(item.is_time_option for item in items)

    voters = poll.voters
    assert len(voters) == 5
    assert all(isinstance(voter, Voter) for voter in voters)
    for voter in voters:
        for vote in voter.votes:
            assert isinstance(vote, PollItem)
            assert vote.poll_item_id in (1, 2)
            assert vote.response == 1


def test_candidates_have_stable_ids(poll: Poll):
    """Candidates can be looked up by their POLL-ITEM-ID."""
    assert poll.get_poll_item(1).summary == "Pizza place"
    assert poll.get_poll_item(2).summary == "Sushi bar"
    assert poll.get_poll_item(99) is None


def test_tally_overrides_changed_vote(poll: Poll):
    """The vote change is counted once, with the original sequences kept."""
    tally = poll.tally()
    assert tally.duplicate_policy is DuplicateVotePolicy.OVERRIDE
    assert tally.poll_status == POLLSTATUS.CONFIRMED
    assert tally.counts == {1: 1, 2: 2}
    assert tally.count_for(1) == 1
    assert tally.count_for(2) == 2
    assert tally.total == 3
    assert tally.participants == {
        "voter-alice@example.com": ParticipantStatus.COUNTED,
        "voter-bob@example.com": ParticipantStatus.COUNTED,
        "voter-carol@example.com": ParticipantStatus.COUNTED,
    }
    assert tally.sequences["voter-carol@example.com"] == (0, 1)
    assert tally.sequences["voter-alice@example.com"] == (0,)
    superseded = tally.uncounted_for("voter-carol@example.com")
    assert len(superseded) == 1
    assert superseded[0].reason is UncountedReason.SUPERSEDED_VOTE
    assert superseded[0].poll_item_id == 1


def test_tally_rejects_changed_vote(poll: Poll):
    """With the REJECT policy, both records of the voter are refused."""
    tally = poll.tally(duplicate_policy=DuplicateVotePolicy.REJECT)
    assert tally.counts == {1: 1, 2: 1}
    assert tally.participants["voter-carol@example.com"] == (ParticipantStatus.REJECTED)
    reasons = {vote.reason for vote in tally.uncounted_for("voter-carol@example.com")}
    assert reasons == {UncountedReason.DUPLICATE_VOTER}
    assert tally.sequences["voter-carol@example.com"] == (0, 1)


def test_tally_rejects_unknown_policy(poll: Poll):
    """The duplicate policy must be explicit."""
    with pytest.raises(TypeError):
        poll.tally(duplicate_policy="override")


def test_invalid_voter_record_is_not_counted(poll: Poll):
    """A VOTER without a UID cannot be attributed to a participant."""
    tally = poll.tally()
    anonymous = tally.uncounted_for(None)
    assert len(anonymous) == 1
    assert anonymous[0].reason is UncountedReason.VOTER_MISSING_UID
    assert anonymous[0].poll_item_id == 2
    assert None not in tally.participants


def test_tally_does_not_modify_the_poll(poll: Poll):
    """Tallying leaves the VPOLL untouched."""
    before = poll.to_ical()
    poll.tally()
    poll.tally(duplicate_policy=DuplicateVotePolicy.REJECT)
    assert poll.to_ical() == before


def test_ics_round_trip(poll: Poll):
    """Writing and reading back preserves the hierarchy and the tally."""
    ics = poll.to_ical()
    reread = Component.from_ical(ics)
    assert isinstance(reread, Poll)
    assert reread == poll
    assert [item.poll_item_id for item in reread.poll_items] == [1, 2]
    assert len(reread.voters) == 5
    assert reread.tally().counts == {1: 1, 2: 2}


def test_x_properties_survive_ics_round_trip(poll: Poll):
    """Unknown X properties and their parameters are preserved."""
    reread = Component.from_ical(poll.to_ical())
    assert reread["X-POLL-CHANNEL"] == "main"
    assert reread["X-POLL-CHANNEL"].params["X-HOPS"] == "2"
    assert reread.get_poll_item(2)["X-CHEF"] == "Yuki"


def test_jcal_round_trip(poll: Poll):
    """The jCal representation carries the same components and votes."""
    jcal = poll.to_jcal()
    assert jcal[0] == "vpoll"
    reread = Component.from_jcal(jcal)
    assert isinstance(reread, Poll)
    assert reread == poll
    assert [item.poll_item_id for item in reread.poll_items] == [1, 2]
    assert len(reread.voters) == 5
    assert reread.tally().counts == {1: 1, 2: 2}
    assert reread["X-POLL-CHANNEL"].params["X-HOPS"] == "2"


def test_json_round_trip(poll: Poll):
    """The JSON jCal form parses back into the same poll."""
    reread = Component.from_jcal(poll.to_json())
    assert isinstance(reread, Poll)
    assert reread.tally().counts == {1: 1, 2: 2}


def test_recursive_copy_preserves_votes_and_x_properties(poll: Poll):
    """A recursive copy keeps subcomponents and unknown properties."""
    copied = poll.copy(recursive=True)
    assert copied is not poll
    assert copied == poll
    assert copied.voters[0] is not poll.voters[0]
    assert copied["X-POLL-CHANNEL"] == "main"
    assert copied.tally().counts == {1: 1, 2: 2}


def test_cancelled_poll_counts_nothing(poll: Poll):
    """A cancelled poll is detected via the enum, and no vote counts."""
    poll.status = POLLSTATUS.CANCELLED
    assert poll.is_cancelled
    assert not poll.is_closed
    tally = poll.tally()
    assert tally.poll_status == POLLSTATUS.CANCELLED
    assert tally.counts == {1: 0, 2: 0}
    assert tally.total == 0
    reasons = {vote.reason for vote in tally.uncounted}
    assert UncountedReason.POLL_CANCELLED in reasons


def test_closed_poll_still_tallies(poll: Poll):
    """A closed poll accepts no new votes, but its votes count."""
    poll.status = POLLSTATUS.CLOSED
    assert poll.is_closed
    assert not poll.is_cancelled
    tally = poll.tally()
    assert tally.poll_status == POLLSTATUS.CLOSED
    assert tally.counts == {1: 1, 2: 2}


def test_cancelled_candidate_cannot_receive_votes(poll: Poll):
    """Votes for a cancelled candidate are reported, not counted."""
    poll.get_poll_item(2).status = STATUS.CANCELLED
    assert not poll.get_poll_item(2).is_active
    tally = poll.tally()
    assert tally.counts == {1: 1, 2: 0}
    cancelled = [
        vote
        for vote in tally.uncounted
        if vote.reason is UncountedReason.POLL_ITEM_CANCELLED
    ]
    assert {vote.poll_item_id for vote in cancelled} == {2}


def test_vote_for_unknown_candidate(poll: Poll):
    """A vote for a POLL-ITEM-ID that is not a candidate is reported."""
    poll.add_component(Voter.new(uid="voter-dave@example.com", votes=[(99, 1)]))
    tally = poll.tally()
    unknown = tally.uncounted_for("voter-dave@example.com")
    assert len(unknown) == 1
    assert unknown[0].reason is UncountedReason.POLL_ITEM_UNKNOWN
    assert unknown[0].poll_item_id == 99
    assert tally.participants["voter-dave@example.com"] == ParticipantStatus.INVALID


def test_time_based_candidates():
    """Candidates can be points in time with DTSTART/DTEND/DURATION."""
    start = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    item = PollItem.new(poll_item_id=7, start=start, duration=timedelta(hours=1))
    assert item.is_time_option
    assert not item.is_text_option
    assert item.DTSTART == start
    assert item.duration == timedelta(hours=1)
    reread = Component.from_ical(item.to_ical())
    assert isinstance(reread, PollItem)
    assert reread.poll_item_id == 7
    assert reread.DTSTART == start
    assert reread.duration == timedelta(hours=1)


def test_new_poll_has_required_properties():
    """Poll.new sets UID and DTSTAMP and nests the subcomponents."""
    poll = Poll.new(
        summary="When?",
        poll_items=[PollItem.new(poll_item_id=1, summary="Monday")],
        voters=[Voter.new(uid="voter-eve@example.com", votes=[(1, 1)])],
    )
    assert poll.uid
    assert poll.DTSTAMP is not None
    assert poll.poll_mode == POLLMODE.BASIC
    assert poll.status == POLLSTATUS.CONFIRMED
    assert poll.poll_items[0].poll_item_id == 1
    assert poll.voters[0].uid == "voter-eve@example.com"
    assert poll.tally().counts == {1: 1}


def test_vote_records_keep_sequence():
    """Vote records expose the SEQUENCE of their VOTER component."""
    voter = Voter.new(uid="voter-frank@example.com", votes=[(1, 1)], sequence=3)
    (record,) = voter.vote_records()
    assert record.voter_uid == "voter-frank@example.com"
    assert record.poll_item_id == 1
    assert record.response == 1
    assert record.sequence == 3
