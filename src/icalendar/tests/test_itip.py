"""Tests for the iTIP REQUEST/CANCEL inbox state machine."""

from __future__ import annotations

import itertools

import pytest

from icalendar import Calendar
from icalendar.itip import (
    ApplyStatus,
    InvalidITipMessage,
    ITipInbox,
    UnsupportedITipMessage,
)

UID = "meeting-1@example.com"
ORGANIZER = "mailto:boss@example.com"
RID = "20260308T090000Z"


def make_message(
    method,
    uid=UID,
    organizer=ORGANIZER,
    sequence=0,
    dtstamp="20260101T000000Z",
    summary="Standup",
    recurrence_id=None,
    start="20260301T090000Z",
):
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//icalendar//itip test//EN",
        f"METHOD:{method}",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"ORGANIZER:{organizer}",
        f"SEQUENCE:{sequence}",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART:{start}",
        f"SUMMARY:{summary}",
    ]
    if recurrence_id is not None:
        lines.append(f"RECURRENCE-ID:{recurrence_id}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return Calendar.from_ical("\r\n".join(lines))


def statuses(results):
    return [result.status for result in results]


def test_request_is_applied_and_duplicate_ignored():
    inbox = ITipInbox()
    assert statuses(inbox.apply(make_message("REQUEST"))) == [ApplyStatus.APPLIED]
    assert statuses(inbox.apply(make_message("REQUEST"))) == [ApplyStatus.IGNORED]
    assert len(inbox.events) == 1
    assert inbox.tombstones == []


def test_stale_request_is_ignored():
    inbox = ITipInbox()
    inbox.apply(make_message("REQUEST", sequence=2))
    assert statuses(inbox.apply(make_message("REQUEST", sequence=1))) == [
        ApplyStatus.IGNORED
    ]
    assert inbox.events[0]["SEQUENCE"] == 2


def test_cancel_then_late_old_request_cannot_revive():
    """A cancelled meeting stays cancelled when an old invite arrives late."""
    inbox = ITipInbox()
    inbox.apply(make_message("REQUEST", sequence=1))
    assert statuses(inbox.apply(make_message("CANCEL", sequence=2))) == [
        ApplyStatus.APPLIED
    ]
    assert inbox.events == []
    # the old invite, delayed in the network, arrives after the cancellation
    assert statuses(inbox.apply(make_message("REQUEST", sequence=1))) == [
        ApplyStatus.IGNORED
    ]
    assert inbox.events == []
    (tombstone,) = inbox.tombstones
    assert tombstone.sequence == 2
    assert tombstone.cancelled


def test_cancel_without_prior_request_still_creates_tombstone():
    inbox = ITipInbox()
    assert statuses(inbox.apply(make_message("CANCEL", sequence=5))) == [
        ApplyStatus.APPLIED
    ]
    assert statuses(inbox.apply(make_message("REQUEST", sequence=5))) == [
        ApplyStatus.CONFLICT
    ]
    assert statuses(inbox.apply(make_message("REQUEST", sequence=4))) == [
        ApplyStatus.IGNORED
    ]
    assert inbox.events == []
    assert len(inbox.tombstones) == 1


def test_occurrence_cancel_is_separate_from_series():
    inbox = ITipInbox()
    inbox.apply(make_message("REQUEST", sequence=1))
    assert statuses(
        inbox.apply(make_message("CANCEL", sequence=2, recurrence_id=RID))
    ) == [ApplyStatus.APPLIED]
    # the master event is still effective, the occurrence is a tombstone
    assert len(inbox.events) == 1
    (tombstone,) = inbox.tombstones
    assert tombstone.key.recurrence_id is not None


def test_late_master_update_keeps_newer_occurrence_cancellation():
    inbox = ITipInbox()
    inbox.apply(make_message("REQUEST", sequence=1))
    inbox.apply(make_message("CANCEL", sequence=3, recurrence_id=RID))
    # a master update written before the occurrence cancel arrives late
    assert statuses(
        inbox.apply(make_message("REQUEST", sequence=2, summary="Moved"))
    ) == [ApplyStatus.APPLIED]
    assert inbox.events[0]["SUMMARY"] == "Moved"
    (tombstone,) = inbox.tombstones
    assert tombstone.sequence == 3
    assert tombstone.key.recurrence_id is not None


def test_series_cancel_does_not_remove_occurrence_tombstone():
    inbox = ITipInbox()
    inbox.apply(make_message("REQUEST", sequence=1))
    inbox.apply(make_message("CANCEL", sequence=3, recurrence_id=RID))
    inbox.apply(make_message("CANCEL", sequence=2))
    assert inbox.events == []
    assert len(inbox.tombstones) == 2
    assert {t.key.recurrence_id for t in inbox.tombstones} == {
        None,
        "2026-03-08T09:00:00+00:00",
    }


def test_same_sequence_dtstamp_breaks_tie():
    inbox = ITipInbox()
    inbox.apply(make_message("REQUEST", sequence=1, dtstamp="20260102T000000Z"))
    assert statuses(
        inbox.apply(make_message("REQUEST", sequence=1, dtstamp="20260101T000000Z"))
    ) == [ApplyStatus.IGNORED]
    inbox2 = ITipInbox()
    inbox2.apply(make_message("REQUEST", sequence=1, dtstamp="20260101T000000Z"))
    assert statuses(
        inbox2.apply(make_message("REQUEST", sequence=1, dtstamp="20260102T000000Z"))
    ) == [ApplyStatus.APPLIED]


def test_same_version_different_content_is_a_checkable_conflict():
    inbox = ITipInbox()
    inbox.apply(make_message("REQUEST", sequence=1, summary="Original"))
    assert statuses(
        inbox.apply(make_message("REQUEST", sequence=1, summary="Forged"))
    ) == [ApplyStatus.CONFLICT]
    # first version wins, the conflict stays inspectable
    assert inbox.events[0]["SUMMARY"] == "Original"
    (entry,) = inbox.conflicts
    (conflict,) = entry.conflicts
    assert conflict.component["SUMMARY"] == "Forged"
    assert "SEQUENCE=1" in conflict.reason


def test_newer_version_resolves_conflict():
    inbox = ITipInbox()
    inbox.apply(make_message("REQUEST", sequence=1, summary="Original"))
    inbox.apply(make_message("REQUEST", sequence=1, summary="Forged"))
    inbox.apply(make_message("REQUEST", sequence=2, summary="Resolved"))
    assert inbox.conflicts == []
    assert inbox.events[0]["SUMMARY"] == "Resolved"


@pytest.mark.parametrize("method", ["REPLY", "COUNTER", "PUBLISH", "ADD"])
def test_unsupported_methods_are_rejected(method):
    with pytest.raises(UnsupportedITipMessage):
        ITipInbox().apply(make_message(method))


def test_thisandfuture_range_is_rejected():
    message = make_message("CANCEL", sequence=1, recurrence_id=RID)
    event = message.walk("VEVENT")[0]
    event["RECURRENCE-ID"].params["RANGE"] = "THISANDFUTURE"
    with pytest.raises(UnsupportedITipMessage):
        ITipInbox().apply(message)


def test_missing_method_and_missing_uid_are_rejected():
    message = make_message("REQUEST")
    del message["METHOD"]
    with pytest.raises(InvalidITipMessage):
        ITipInbox().apply(message)
    message = make_message("REQUEST")
    del message.walk("VEVENT")[0]["UID"]
    with pytest.raises(InvalidITipMessage):
        ITipInbox().apply(message)


def test_received_calendar_is_not_modified():
    inbox = ITipInbox()
    message = make_message("REQUEST", sequence=1)
    before = message.to_ical()
    inbox.apply(message)
    assert message.to_ical() == before
    # mutating the stored copy must not leak back either
    inbox.events[0]["SUMMARY"] = "tampered"
    assert inbox.events[0]["SUMMARY"] == "Standup"


def test_state_survives_serialization_roundtrip():
    inbox = ITipInbox()
    inbox.apply(make_message("REQUEST", sequence=1))
    inbox.apply(make_message("REQUEST", sequence=1, summary="Forged"))
    inbox.apply(make_message("CANCEL", sequence=3, recurrence_id=RID))
    inbox.apply(make_message("REQUEST", uid="other@example.com", sequence=0))
    restored = ITipInbox.from_json(inbox.to_json())
    assert restored.to_dict() == inbox.to_dict()
    assert restored.to_json() == inbox.to_json()
    # the restored state keeps its version memory
    assert statuses(
        restored.apply(make_message("CANCEL", sequence=2, recurrence_id=RID))
    ) == [ApplyStatus.IGNORED]


def replay_messages():
    """One meeting's worth of invites, updates and cancellations."""
    return {
        "invite": make_message("REQUEST", sequence=0, summary="Standup"),
        "update": make_message("REQUEST", sequence=1, summary="Standup (moved)"),
        "cancel-occurrence": make_message("CANCEL", sequence=2, recurrence_id=RID),
        "cancel-series": make_message("CANCEL", sequence=3),
        "stale-invite": make_message("REQUEST", sequence=0, summary="Standup"),
    }


@pytest.mark.parametrize(
    "order",
    [
        pytest.param(perm, id="-".join(perm))
        for perm in itertools.permutations(
            ["invite", "update", "cancel-occurrence", "cancel-series", "stale-invite"]
        )
    ],
)
def test_final_state_is_independent_of_arrival_order(order):
    messages = replay_messages()
    inbox = ITipInbox()
    for name in order:
        inbox.apply(messages[name])
    assert inbox.events == []
    assert len(inbox.tombstones) == 2
    assert inbox.to_dict() == EXPECTED_FINAL_STATE


def _expected_final_state():
    inbox = ITipInbox()
    for name in [
        "invite",
        "update",
        "cancel-occurrence",
        "cancel-series",
        "stale-invite",
    ]:
        inbox.apply(replay_messages()[name])
    return inbox.to_dict()


EXPECTED_FINAL_STATE = _expected_final_state()
