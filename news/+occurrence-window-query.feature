Add :func:`icalendar.occurrences_between` and
:meth:`icalendar.Calendar.occurrences_between`: a bounded time-window
occurrence query that combines ``DTSTART``, ``RRULE``, ``RDATE``,
``EXDATE`` and detached ``VEVENT`` exceptions. It returns the actually
happening instances together with their original recurrence identity and
source component, supports ``RANGE=THISANDFUTURE`` reschedules, skips
cancelled exceptions, interprets floating times only with an explicit
timezone, and marks results incomplete when the candidate limit is reached.
