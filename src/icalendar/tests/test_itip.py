"""Tests for the iTIP REQUEST/CANCEL inbox state object."""

import itertools
import json
from datetime import datetime, timezone

import pytest

from icalendar import Calendar, Event
from icalendar.itip import (
    ITIPInbox,
    ITIPStatus,
    UnsupportedITIPMessage,
)

ORGANIZER = "mailto:boss@example.com"
UID = "meeting-001@example.com"
STAMP_0 = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
STAMP_1 = datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc)
STAMP_2 = datetime(2026, 9, 3, 9, 0, tzinfo=timezone.utc)
STAMP_3 = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)
STAMP_4 = datetime(2026, 9, 5, 9, 0, tzinfo=timezone.utc)
START = datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)
OCCURRENCE = datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc)


def make_message(
    method,
    sequence=0,
    stamp=STAMP_0,
    summary="Weekly sync",
    uid=UID,
    recurrence_id=None,
    range_param=None,
):
    """Build a minimal iTIP message calendar with one VEVENT."""
    calendar = Calendar()
    calendar.add("PRODID", "-//icalendar//itip-test//EN")
    calendar.add("VERSION", "2.0")
    calendar.add("METHOD", method)
    event = Event()
    event.add("UID", uid)
    event.add("ORGANIZER", ORGANIZER)
    event.add("SEQUENCE", sequence)
    event.add("DTSTAMP", stamp)
    event.add("SUMMARY", summary)
    event.add("DTSTART", start_for(recurrence_id))
    if recurrence_id is not None:
        parameters = {"RANGE": range_param} if range_param else None
        event.add("RECURRENCE-ID", recurrence_id, parameters=parameters)
    calendar.add_component(event)
    return calendar


def start_for(recurrence_id):
    return recurrence_id if recurrence_id is not None else START


def statuses(results):
    return [result.status for result in results]


def test_late_invite_does_not_resurrect_cancelled_meeting():
    """A cancel followed by a delayed older invite stays cancelled."""
    inbox = ITIPInbox()
    cancel = inbox.process(make_message("CANCEL", sequence=2, stamp=STAMP_2))
    invite = inbox.process(make_message("REQUEST", sequence=1, stamp=STAMP_1))
    assert statuses(cancel) == [ITIPStatus.APPLIED]
    assert statuses(invite) == [ITIPStatus.IGNORED]
    assert inbox.events() == []
    tombstones = inbox.tombstones()
    assert len(tombstones) == 1
    assert tombstones[0].sequence == 2
    assert tombstones[0].recurrence_id is None


def test_stale_update_is_ignored():
    inbox = ITIPInbox()
    inbox.process(make_message("REQUEST", sequence=2, stamp=STAMP_2, summary="new"))
    results = inbox.process(
        make_message("REQUEST", sequence=1, stamp=STAMP_1, summary="old")
    )
    assert statuses(results) == [ITIPStatus.IGNORED]
    assert inbox.events()[0]["SUMMARY"] == "new"


def test_dtstamp_breaks_sequence_ties():
    inbox = ITIPInbox()
    inbox.process(make_message("REQUEST", sequence=1, stamp=STAMP_2, summary="later"))
    results = inbox.process(
        make_message("REQUEST", sequence=1, stamp=STAMP_1, summary="earlier")
    )
    assert statuses(results) == [ITIPStatus.IGNORED]
    assert inbox.events()[0]["SUMMARY"] == "later"


def test_newer_request_after_cancel_is_applied():
    """A genuinely newer version is an update, not a delayed duplicate."""
    inbox = ITIPInbox()
    inbox.process(make_message("CANCEL", sequence=2, stamp=STAMP_2))
    results = inbox.process(make_message("REQUEST", sequence=3, stamp=STAMP_3))
    assert statuses(results) == [ITIPStatus.APPLIED]
    assert len(inbox.events()) == 1
    assert inbox.tombstones() == []


def test_duplicate_delivery_is_ignored():
    inbox = ITIPInbox()
    inbox.process(make_message("REQUEST", sequence=1, stamp=STAMP_1))
    results = inbox.process(make_message("REQUEST", sequence=1, stamp=STAMP_1))
    assert statuses(results) == [ITIPStatus.IGNORED]


def test_series_and_occurrence_cancels_are_separate():
    """A late master update must not erase a newer single-occurrence cancel."""
    inbox = ITIPInbox()
    inbox.process(make_message("CANCEL", sequence=3, stamp=STAMP_3,
                               recurrence_id=OCCURRENCE))
    results = inbox.process(make_message("REQUEST", sequence=2, stamp=STAMP_2))
    assert statuses(results) == [ITIPStatus.APPLIED]
    # The occurrence tombstone survives the master update.
    tombstones = inbox.tombstones()
    assert len(tombstones) == 1
    assert tombstones[0].recurrence_id is not None
    assert tombstones[0].sequence == 3
    # The master event is effective and tracked independently.
    assert len(inbox.events()) == 1
    assert inbox.events()[0].get("RECURRENCE-ID") is None


def test_late_master_update_does_not_erase_occurrence_tombstone():
    inbox = ITIPInbox()
    inbox.process(make_message("REQUEST", sequence=0, stamp=STAMP_0))
    inbox.process(make_message("CANCEL", sequence=3, stamp=STAMP_3,
                               recurrence_id=OCCURRENCE))
    results = inbox.process(make_message("REQUEST", sequence=1, stamp=STAMP_1))
    assert statuses(results) == [ITIPStatus.APPLIED]
    assert len(inbox.tombstones()) == 1
    assert inbox.tombstones()[0].recurrence_id is not None


def test_same_version_conflict_is_inspectable_and_deterministic():
    first = make_message("REQUEST", sequence=1, stamp=STAMP_1, summary="alpha")
    second = make_message("REQUEST", sequence=1, stamp=STAMP_1, summary="omega")

    inbox_ab = ITIPInbox()
    inbox_ab.process(first)
    results = inbox_ab.process(second)
    assert statuses(results) == [ITIPStatus.CONFLICT]
    conflict = results[0].conflict
    assert conflict is not None
    assert conflict.uid == UID
    assert conflict.sequence == 1
    assert conflict.kept != conflict.discarded
    assert inbox_ab.conflicts() == [conflict]

    inbox_ba = ITIPInbox()
    inbox_ba.process(second)
    results_ba = inbox_ba.process(first)
    assert statuses(results_ba) == [ITIPStatus.CONFLICT]
    # The surviving version does not depend on the arrival order.
    assert inbox_ab.to_dict()["entries"] == inbox_ba.to_dict()["entries"]


def test_reply_and_counter_are_rejected():
    inbox = ITIPInbox()
    with pytest.raises(UnsupportedITIPMessage):
        inbox.process(make_message("REPLY"))
    with pytest.raises(UnsupportedITIPMessage):
        inbox.process(make_message("COUNTER"))


def test_missing_method_is_rejected():
    calendar = Calendar()
    calendar.add_component(Event())
    with pytest.raises(UnsupportedITIPMessage):
        ITIPInbox().process(calendar)


def test_thisandfuture_range_is_rejected():
    inbox = ITIPInbox()
    message = make_message(
        "CANCEL", recurrence_id=OCCURRENCE, range_param="THISANDFUTURE"
    )
    with pytest.raises(UnsupportedITIPMessage):
        inbox.process(message)


def test_received_calendar_is_not_modified():
    message = make_message("REQUEST", sequence=1, stamp=STAMP_1)
    before = message.to_ical()
    ITIPInbox().process(message)
    assert message.to_ical() == before


def test_state_roundtrip_through_serialization():
    inbox = ITIPInbox()
    inbox.process(make_message("REQUEST", sequence=1, stamp=STAMP_1))
    inbox.process(make_message("CANCEL", sequence=3, stamp=STAMP_3,
                               recurrence_id=OCCURRENCE))
    inbox.process(make_message("REQUEST", sequence=1, stamp=STAMP_1,
                               summary="conflicting"))
    restored = ITIPInbox.from_dict(json.loads(json.dumps(inbox.to_dict())))
    assert restored.to_dict() == inbox.to_dict()
    assert len(restored.tombstones()) == 1
    assert len(restored.conflicts()) == 1


def message_set():
    """The same meeting as invite, reschedule, exception and cancellations."""
    return [
        make_message("REQUEST", sequence=0, stamp=STAMP_0, summary="invite"),
        make_message("REQUEST", sequence=1, stamp=STAMP_1, summary="rescheduled"),
        make_message("REQUEST", sequence=2, stamp=STAMP_2,
                     summary="moved occurrence", recurrence_id=OCCURRENCE),
        make_message("CANCEL", sequence=3, stamp=STAMP_3,
                     recurrence_id=OCCURRENCE),
        make_message("CANCEL", sequence=4, stamp=STAMP_4),
    ]


def canonical_state(inbox):
    return inbox.to_dict()["entries"]


def test_replay_in_any_order_converges():
    """All arrival orders of the same messages yield the same final state."""
    messages = message_set()
    reference = None
    for order in itertools.permutations(messages):
        inbox = ITIPInbox()
        for message in order:
            inbox.process(message)
        state = canonical_state(inbox)
        if reference is None:
            reference = state
        assert state == reference
    # Final state: the series and the single occurrence are both cancelled.
    inbox = ITIPInbox()
    for message in messages:
        inbox.process(message)
    assert inbox.events() == []
    tombstones = inbox.tombstones()
    assert len(tombstones) == 2
    assert sorted(t.recurrence_id is None for t in tombstones) == [False, True]
