"""Read-only dependency analysis of ``VTODO`` components.

``RELATED-TO`` properties with the temporal ``RELTYPE`` parameter values
from :rfc:`9253` express ordering constraints between tasks:

-   ``FINISHTOSTART``: the referenced task must finish before this one can start.
-   ``STARTTOSTART``: the referenced task must start before this one can start.
-   ``FINISHTOFINISH``: this task can only finish after the referenced one finishes.
-   ``STARTTOFINISH``: the start of the referenced task controls this one's finish.

This module builds the dependency graph of all ``VTODO`` components of a
:class:`~icalendar.cal.calendar.Calendar` without modifying the calendar
and without expanding recurring tasks. It reports structural problems
(missing ``UID``, duplicate ``UID``, unknown references, self references
and dependency cycles) together with a topological execution order and
the set of tasks that can be started right now.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from typing import TYPE_CHECKING

from icalendar.enums import RELTYPE

if TYPE_CHECKING:
    from icalendar.cal.calendar import Calendar
    from icalendar.cal.todo import Todo

DEPENDENCY_RELTYPES = frozenset(
    {
        RELTYPE.FINISHTOSTART,
        RELTYPE.FINISHTOFINISH,
        RELTYPE.STARTTOFINISH,
        RELTYPE.STARTTOSTART,
    }
)
"""The ``RELTYPE`` values that create a temporal dependency between tasks."""

_STATUS_COMPLETED = "COMPLETED"
_STATUS_CANCELLED = "CANCELLED"
_STATUS_IN_PROCESS = "IN-PROCESS"


@dataclass(frozen=True)
class UnknownDependencyReference:
    """A ``RELATED-TO`` dependency pointing to a ``UID`` no ``VTODO`` provides."""

    source_uid: str
    """The ``UID`` of the ``VTODO`` that holds the ``RELATED-TO`` property."""

    target_uid: str
    """The referenced ``UID`` that does not exist in the calendar."""

    reltype: str
    """The ``RELTYPE`` parameter of the dangling reference."""


@dataclass
class TodoDependencyAnalysis:
    """The result of :func:`analyze_todo_dependencies`.

    Attributes:
        order: The ``UID`` of every actionable task (not completed and not
            cancelled) in a topological order that respects all dependency
            types. Tasks stuck behind a dependency cycle are excluded.
            The order is deterministic and independent of the order in
            which the components appear in the calendar.
        executable: The ``UID`` of every task that can be started now:
            it is actionable, its ``DTSTART`` is not in the future and all
            of its start-blocking dependencies are satisfied.
        missing_uids: The ``VTODO`` components without a ``UID`` property.
            They cannot take part in the dependency graph.
        duplicate_uids: Every ``UID`` used by more than one ``VTODO``.
            Only the first occurrence of a duplicated ``UID`` takes part
            in the dependency graph.
        unknown_references: Every dependency whose target ``UID`` is not
            provided by any ``VTODO`` in the calendar.
        self_references: Every ``UID`` whose task depends on itself.
        cycle: One concrete dependency cycle as a list of ``UID`` in
            dependency order, with the first ``UID`` repeated at the end
            to close the loop. Empty if the graph is acyclic.
    """

    order: list[str] = field(default_factory=list)
    executable: list[str] = field(default_factory=list)
    missing_uids: list[Todo] = field(default_factory=list)
    duplicate_uids: list[str] = field(default_factory=list)
    unknown_references: list[UnknownDependencyReference] = field(default_factory=list)
    self_references: list[str] = field(default_factory=list)
    cycle: list[str] = field(default_factory=list)

    @property
    def errors(self) -> list[str]:
        """Human-readable descriptions of every problem found."""
        errors = [
            f"VTODO is missing a UID: {todo.get('SUMMARY', repr(todo))}"
            for todo in self.missing_uids
        ]
        errors.extend(f"duplicate UID: {uid}" for uid in self.duplicate_uids)
        errors.extend(
            f"unknown reference: {ref.source_uid} "
            f"depends on missing {ref.target_uid} ({ref.reltype})"
            for ref in self.unknown_references
        )
        errors.extend(f"self reference: {uid}" for uid in self.self_references)
        if self.cycle:
            errors.append(f"dependency cycle: {' -> '.join(self.cycle)}")
        return errors

    @property
    def ok(self) -> bool:
        """Whether the analysis found no problems."""
        return not (
            self.missing_uids
            or self.duplicate_uids
            or self.unknown_references
            or self.self_references
            or self.cycle
        )


def _get_datetime(todo: Todo, name: str) -> date | datetime | None:
    """Get a date or datetime property value or None if it is absent."""
    prop = todo.get(name)
    return prop.dt if prop is not None else None


def _to_naive_datetime(value: date | datetime) -> datetime:
    """Normalize a date or datetime to a naive UTC datetime for comparison."""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    return datetime.combine(value, time.min)


def _status(todo: Todo) -> str:
    """The STATUS of the todo as an uppercase string, "" if absent."""
    return str(todo.get("STATUS", "")).upper()


def _is_completed(todo: Todo) -> bool:
    """Whether the todo is done according to STATUS or COMPLETED."""
    return _status(todo) == _STATUS_COMPLETED or "COMPLETED" in todo


def _is_cancelled(todo: Todo) -> bool:
    """Whether the todo has been cancelled."""
    return _status(todo) == _STATUS_CANCELLED


def _has_started(todo: Todo, now: datetime) -> bool:
    """Whether work on the todo has begun.

    A todo counts as started if it is completed, in process,
    or its DTSTART has been reached.
    """
    if _is_completed(todo) or _status(todo) == _STATUS_IN_PROCESS:
        return True
    dtstart = _get_datetime(todo, "DTSTART")
    return dtstart is not None and _to_naive_datetime(dtstart) <= now


def _sort_key(todo: Todo, uid: str) -> tuple[datetime, str]:
    """A deterministic sort key based on DTSTART, then DUE, then UID."""
    start = _get_datetime(todo, "DTSTART")
    if start is None:
        start = _get_datetime(todo, "DUE")
    if start is None:
        return (datetime.max, uid)
    return (_to_naive_datetime(start), uid)


def _find_cycle(nodes: list[str], successors: dict[str, list[str]]) -> list[str]:
    """Find one concrete dependency cycle, deterministic for the same graph.

    Returns:
        The cycle as a list of UIDs with the first UID repeated at the end,
        or an empty list if the graph is acyclic.
    """
    color: dict[str, int] = {}  # 1 = on the DFS stack, 2 = done
    stack: list[str] = []

    def visit(uid: str) -> list[str]:
        color[uid] = 1
        stack.append(uid)
        for nxt in successors.get(uid, []):
            state = color.get(nxt, 0)
            if state == 0:
                cycle = visit(nxt)
                if cycle:
                    return cycle
            elif state == 1:
                return stack[stack.index(nxt) :] + [nxt]
        stack.pop()
        color[uid] = 2
        return []

    for uid in nodes:
        if color.get(uid, 0) == 0:
            cycle = visit(uid)
            if cycle:
                return cycle
    return []


def analyze_todo_dependencies(
    calendar: Calendar, now: datetime | None = None
) -> TodoDependencyAnalysis:
    """Analyze the dependency graph of the VTODO components of a calendar.

    The calendar is only read, never modified, and recurring tasks are not
    expanded. Dependencies are the ``RELATED-TO`` properties whose
    ``RELTYPE`` parameter is one of :data:`DEPENDENCY_RELTYPES`; other
    relationship types such as ``PARENT`` are not ordering constraints and
    are ignored.

    Parameters:
        calendar: The calendar whose VTODO components are analyzed.
        now: The point in time used to decide which tasks can start.
            Defaults to the current time.

    Returns:
        A :class:`TodoDependencyAnalysis` with the problems found,
        a topological execution order and the currently executable tasks.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    now = _to_naive_datetime(now)
    result = TodoDependencyAnalysis()

    todos: dict[str, Todo] = {}
    duplicates: set[str] = set()
    for todo in calendar.todos:
        uid_prop = todo.get("UID")
        if uid_prop is None:
            result.missing_uids.append(todo)
            continue
        uid = str(uid_prop)
        if uid in todos:
            duplicates.add(uid)
            continue
        todos[uid] = todo
    result.duplicate_uids = sorted(duplicates)

    # edges: predecessor UID -> successor UID, annotated with the reltype
    predecessors: dict[str, list[tuple[str, str]]] = {uid: [] for uid in todos}
    successors: dict[str, list[str]] = {uid: [] for uid in todos}
    seen_edges: set[tuple[str, str, str]] = set()
    unknown: set[UnknownDependencyReference] = set()
    self_references: set[str] = set()
    for uid in sorted(todos):
        for related_to in todos[uid].related_to:
            reltype = str(related_to.params.get("RELTYPE", "PARENT")).upper()
            if reltype not in DEPENDENCY_RELTYPES:
                continue
            target = str(related_to)
            if target == uid:
                self_references.add(uid)
                continue
            if target not in todos:
                unknown.add(UnknownDependencyReference(uid, target, reltype))
                continue
            edge = (target, uid, reltype)
            if edge in seen_edges:
                continue
            seen_edges.add(edge)
            predecessors[uid].append((target, reltype))
            successors[target].append(uid)
    result.unknown_references = sorted(
        unknown, key=lambda ref: (ref.source_uid, ref.target_uid, ref.reltype)
    )
    result.self_references = sorted(self_references)
    for uid in successors:
        successors[uid].sort()

    result.cycle = _find_cycle(sorted(todos), successors)

    def actionable(uid: str) -> bool:
        todo = todos[uid]
        return not _is_completed(todo) and not _is_cancelled(todo)

    # Topological order of the actionable tasks with Kahn's algorithm.
    # Edges from completed or cancelled predecessors are already satisfied
    # and do not constrain the order of what remains to be done.
    remaining = {
        uid: sum(1 for pred, _ in predecessors[uid] if actionable(pred))
        for uid in todos
        if actionable(uid)
    }
    ready = [
        (_sort_key(todos[uid], uid), uid)
        for uid, count in remaining.items()
        if count == 0
    ]
    heapq.heapify(ready)
    while ready:
        _, uid = heapq.heappop(ready)
        result.order.append(uid)
        for nxt in successors[uid]:
            if nxt in remaining:
                remaining[nxt] -= 1
                if remaining[nxt] == 0:
                    heapq.heappush(ready, (_sort_key(todos[nxt], nxt), nxt))

    # The tasks that can start right now.
    for uid in sorted(todos):
        todo = todos[uid]
        if not actionable(uid):
            continue
        dtstart = _get_datetime(todo, "DTSTART")
        if dtstart is not None and _to_naive_datetime(dtstart) > now:
            continue  # scheduled in the future
        satisfied = True
        for pred, reltype in predecessors[uid]:
            pred_todo = todos[pred]
            if _is_cancelled(pred_todo):
                continue  # a cancelled predecessor no longer blocks
            if reltype == RELTYPE.FINISHTOSTART and not _is_completed(pred_todo):
                satisfied = False
                break
            if reltype == RELTYPE.STARTTOSTART and not _has_started(pred_todo, now):
                satisfied = False
                break
            # FINISHTOFINISH and STARTTOFINISH constrain the finish of
            # this task, not its start.
        if satisfied:
            result.executable.append(uid)
    result.executable.sort(key=lambda uid: _sort_key(todos[uid], uid))

    return result


__all__ = [
    "DEPENDENCY_RELTYPES",
    "TodoDependencyAnalysis",
    "UnknownDependencyReference",
    "analyze_todo_dependencies",
]
