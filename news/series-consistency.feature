Added :func:`icalendar.check_series_consistency` and
:meth:`Calendar.check_series_consistency()` for explicit, read-only
consistency checking of recurring series. The check groups components by
``UID`` and reports -- in one pass, with stable error codes, component
indices and property paths -- duplicate masters, duplicate
``RECURRENCE-ID`` exception identities, ``RECURRENCE-ID``/``RRULE``/``RDATE``/
``EXDATE`` value type and time form mismatches against the master
``DTSTART``, ``COUNT``/``UNTIL`` and ``DTEND``/``DURATION`` rule violations,
negative or out-of-range durations, conflicting ``RANGE=THISANDFUTURE``
claims, and previously recorded parse errors. It never expands a series or
modifies the input.
