"""Tests for the read-only VTODO dependency analysis.

See :mod:`icalendar.cal.dependency`.
"""

from datetime import datetime, timezone

import pytest

from icalendar import (
    STATUS,
    Calendar,
    TodoDependencyAnalysis,
    UnknownDependencyReference,
    analyze_todo_dependencies,
)

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)

PLAN_ICS = """
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//icalendar//dependency-test//EN
BEGIN:VTODO
UID:design
DTSTART:20260901T090000Z
STATUS:COMPLETED
COMPLETED:20260902T170000Z
END:VTODO
BEGIN:VTODO
UID:build
DTSTART:20260920T090000Z
RELATED-TO;RELTYPE=FINISHTOSTART:design
END:VTODO
BEGIN:VTODO
UID:ship
RELATED-TO;RELTYPE=FINISHTOSTART:build
END:VTODO
BEGIN:VTODO
UID:draft
DTSTART:20260920T090000Z
END:VTODO
BEGIN:VTODO
UID:polish
RELATED-TO;RELTYPE=STARTTOSTART:draft
END:VTODO
BEGIN:VTODO
UID:publish
RELATED-TO;RELTYPE=FINISHTOFINISH:polish
END:VTODO
BEGIN:VTODO
UID:kickoff
DTSTART:20260922T100000Z
END:VTODO
BEGIN:VTODO
UID:sales
RELATED-TO;RELTYPE=STARTTOFINISH:kickoff
END:VTODO
BEGIN:VTODO
UID:alpha
RELATED-TO;RELTYPE=FINISHTOSTART:beta
END:VTODO
BEGIN:VTODO
UID:beta
RELATED-TO;RELTYPE=FINISHTOSTART:alpha
END:VTODO
END:VCALENDAR
"""


@pytest.fixture
def plan() -> Calendar:
    """A plan with two independent branches and one dependency cycle."""
    return Calendar.from_ical(PLAN_ICS)


@pytest.fixture
def analysis(plan) -> TodoDependencyAnalysis:
    """The analysis of the plan at a fixed point in time."""
    return plan.analyze_todo_dependencies(now=NOW)


def todos_by_uid(calendar: Calendar) -> dict:
    """Map the todos of a calendar by their UID."""
    return {str(todo["UID"]): todo for todo in calendar.todos}


def test_cycle_is_reported_concretely(analysis):
    """A cycle returns one concrete loop of UIDs."""
    assert analysis.cycle == ["alpha", "beta", "alpha"]
    assert not analysis.ok
    assert analysis.errors == ["dependency cycle: alpha -> beta -> alpha"]


def test_cycle_tasks_are_not_executable(analysis):
    """Tasks in a cycle can never satisfy each other."""
    assert "alpha" not in analysis.executable
    assert "beta" not in analysis.executable
    assert "alpha" not in analysis.order
    assert "beta" not in analysis.order


def test_executable_set(analysis):
    """STATUS, COMPLETED and DTSTART decide what can start now."""
    assert analysis.executable == [
        "build",  # design is completed
        "draft",  # no dependencies
        "kickoff",  # no dependencies
        "polish",  # draft started: DTSTART is in the past
        "publish",  # FINISHTOFINISH does not block the start
        "sales",  # STARTTOFINISH does not block the start
    ]


def test_completed_and_blocked_tasks_are_not_executable(analysis):
    """ship waits for build, and done tasks need no execution."""
    assert "ship" not in analysis.executable
    assert "design" not in analysis.executable
    assert "design" not in analysis.order


def test_topological_order(analysis):
    """The order respects every dependency type, not the component order."""
    order = analysis.order
    assert set(order) == {
        "build",
        "ship",
        "draft",
        "polish",
        "publish",
        "kickoff",
        "sales",
    }
    assert order.index("build") < order.index("ship")
    assert order.index("draft") < order.index("polish")
    assert order.index("polish") < order.index("publish")
    assert order.index("kickoff") < order.index("sales")


def test_output_is_stable_across_component_order(plan):
    """Unrelated branches produce the same output however they appear."""
    reference = plan.analyze_todo_dependencies(now=NOW)
    shuffled = Calendar()
    shuffled["VERSION"] = "2.0"
    shuffled["PRODID"] = "-//icalendar//dependency-test//EN"
    for todo in reversed(plan.todos):
        shuffled.add_component(todo)
    rerolled = shuffled.analyze_todo_dependencies(now=NOW)
    assert rerolled.order == reference.order
    assert rerolled.executable == reference.executable
    assert rerolled.cycle == reference.cycle


def test_removing_one_relation_breaks_the_cycle(plan):
    """Deleting alpha's dependency on beta makes alpha executable."""
    del todos_by_uid(plan)["alpha"]["RELATED-TO"]
    analysis = plan.analyze_todo_dependencies(now=NOW)
    assert analysis.cycle == []
    assert analysis.ok
    assert "alpha" in analysis.executable
    assert "beta" not in analysis.executable  # still waits for alpha
    assert analysis.order.index("alpha") < analysis.order.index("beta")


def test_partial_completion_does_not_unblock_successors():
    """A completed predecessor must not mark other successors executable."""
    calendar = Calendar.from_ical(
        """
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//icalendar//dependency-test//EN
BEGIN:VTODO
UID:e
STATUS:COMPLETED
END:VTODO
BEGIN:VTODO
UID:f
RELATED-TO;RELTYPE=FINISHTOSTART:e
END:VTODO
BEGIN:VTODO
UID:h
STATUS:NEEDS-ACTION
END:VTODO
BEGIN:VTODO
UID:g
RELATED-TO;RELTYPE=FINISHTOSTART:e
RELATED-TO;RELTYPE=FINISHTOSTART:h
END:VTODO
END:VCALENDAR
"""
    )
    analysis = calendar.analyze_todo_dependencies(now=NOW)
    assert "f" in analysis.executable
    assert "g" not in analysis.executable  # h is still incomplete
    # complete h and re-analyze
    todos_by_uid(calendar)["h"].status = STATUS.COMPLETED
    analysis = calendar.analyze_todo_dependencies(now=NOW)
    assert "g" in analysis.executable
    assert analysis.order == ["f", "g"]


def test_cancelled_predecessor_does_not_block():
    """A cancelled task is not actionable but no longer blocks others."""
    calendar = Calendar.from_ical(
        """
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//icalendar//dependency-test//EN
BEGIN:VTODO
UID:axed
STATUS:CANCELLED
END:VTODO
BEGIN:VTODO
UID:relief
RELATED-TO;RELTYPE=FINISHTOSTART:axed
END:VTODO
END:VCALENDAR
"""
    )
    analysis = calendar.analyze_todo_dependencies(now=NOW)
    assert analysis.executable == ["relief"]
    assert analysis.order == ["relief"]


def test_future_dtstart_is_not_executable_yet():
    """A task scheduled in the future cannot start now."""
    calendar = Calendar.from_ical(
        """
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//icalendar//dependency-test//EN
BEGIN:VTODO
UID:later
DTSTART:20990101T000000Z
END:VTODO
BEGIN:VTODO
UID:overdue
DUE:20260101T000000Z
END:VTODO
END:VCALENDAR
"""
    )
    analysis = calendar.analyze_todo_dependencies(now=NOW)
    assert analysis.executable == ["overdue"]
    assert set(analysis.order) == {"later", "overdue"}
    # the overdue task sorts before the far future one
    assert analysis.order.index("overdue") < analysis.order.index("later")


def test_structural_problems_are_reported_separately():
    """Missing UID, duplicate UID, unknown references and self loops."""
    calendar = Calendar.from_ical(
        """
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//icalendar//dependency-test//EN
BEGIN:VTODO
SUMMARY:no uid here
END:VTODO
BEGIN:VTODO
UID:dup
END:VTODO
BEGIN:VTODO
UID:dup
END:VTODO
BEGIN:VTODO
UID:dangling
RELATED-TO;RELTYPE=FINISHTOSTART:ghost
END:VTODO
BEGIN:VTODO
UID:loop
RELATED-TO;RELTYPE=STARTTOSTART:loop
END:VTODO
BEGIN:VTODO
UID:child
RELATED-TO:dup
END:VTODO
END:VCALENDAR
"""
    )
    analysis = calendar.analyze_todo_dependencies(now=NOW)
    assert len(analysis.missing_uids) == 1
    assert analysis.missing_uids[0]["SUMMARY"] == "no uid here"
    assert analysis.duplicate_uids == ["dup"]
    assert analysis.unknown_references == [
        UnknownDependencyReference("dangling", "ghost", "FINISHTOSTART")
    ]
    assert analysis.self_references == ["loop"]
    assert analysis.cycle == []
    assert not analysis.ok
    assert len(analysis.errors) == 4
    # a PARENT relation (the default) is not a temporal dependency
    assert "child" in analysis.executable


def test_analysis_does_not_modify_the_calendar(plan):
    """The analysis is read-only."""
    before = plan.to_ical()
    plan.analyze_todo_dependencies(now=NOW)
    assert plan.to_ical() == before


def test_recurring_tasks_are_not_expanded():
    """A recurring task stays a single node in the graph."""
    calendar = Calendar.from_ical(
        """
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//icalendar//dependency-test//EN
BEGIN:VTODO
UID:rec
DTSTART:20260901T090000Z
RRULE:FREQ=DAILY
END:VTODO
END:VCALENDAR
"""
    )
    analysis = calendar.analyze_todo_dependencies(now=NOW)
    assert analysis.order == ["rec"]
    assert analysis.executable == ["rec"]


def test_ics_roundtrip_preserves_reltype(plan):
    """RELTYPE parameters survive serialization and parsing."""
    reparsed = Calendar.from_ical(plan.to_ical())
    expected = {
        "build": "FINISHTOSTART",
        "ship": "FINISHTOSTART",
        "polish": "STARTTOSTART",
        "publish": "FINISHTOFINISH",
        "sales": "STARTTOFINISH",
        "alpha": "FINISHTOSTART",
        "beta": "FINISHTOSTART",
    }
    for uid, reltype in expected.items():
        related_to = todos_by_uid(reparsed)[uid].related_to
        assert len(related_to) == 1
        assert related_to[0].RELTYPE == reltype
    raw = plan.to_ical().decode()
    for reltype in (
        "FINISHTOSTART",
        "STARTTOSTART",
        "FINISHTOFINISH",
        "STARTTOFINISH",
    ):
        assert f"RELTYPE={reltype}" in raw
    # the analysis of the reparsed calendar is identical
    again = reparsed.analyze_todo_dependencies(now=NOW)
    reference = plan.analyze_todo_dependencies(now=NOW)
    assert again.order == reference.order
    assert again.executable == reference.executable
    assert again.cycle == reference.cycle


def test_module_level_entry_point(plan):
    """The function is also exported at the package level."""
    analysis = analyze_todo_dependencies(plan, now=NOW)
    assert isinstance(analysis, TodoDependencyAnalysis)
    assert analysis.cycle == ["alpha", "beta", "alpha"]


def test_now_defaults_to_the_current_time(plan):
    """The analysis works without an explicit point in time."""
    analysis = plan.analyze_todo_dependencies()
    assert "build" in analysis.executable
