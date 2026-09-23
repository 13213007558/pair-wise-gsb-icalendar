Add :func:`icalendar.analyze_todo_dependencies` and
:meth:`icalendar.Calendar.analyze_todo_dependencies`, a read-only analysis of
the ``RELATED-TO`` dependency graph of ``VTODO`` components. It reports
missing and duplicate ``UID`` values, unknown and self references and one
concrete dependency cycle, and computes a topological execution order together
with the tasks that can start now based on ``STATUS``, ``COMPLETED`` and
``DTSTART``/``DUE``.
