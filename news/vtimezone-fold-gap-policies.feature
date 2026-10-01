Added explicit fold and gap policies to the VTIMEZONE conversion interface.

:class:`~icalendar.Timezone` now converts naive local times solely from
its ``STANDARD``/``DAYLIGHT`` sub-components without consulting the
operating system timezone database:

- :meth:`~icalendar.Timezone.get_transitions_between` generates the
  transitions from ``DTSTART``, ``RRULE`` and ``RDATE`` (honoring
  ``EXDATE``) inside a finite window.
- :meth:`~icalendar.Timezone.diagnose_local` returns a
  :class:`~icalendar.LocalTimeDiagnosis` with the unique offset, both
  candidates for a fold, or the gap details for a missing time.
- :meth:`~icalendar.Timezone.localize_naive` accepts
  ``fold="earlier"|"later"|"reject"`` and
  ``gap="shift-forward"|"reject"``.
- :meth:`~icalendar.Timezone.to_tzinfo` returns a pure-VTIMEZONE
  :class:`~icalendar.VTIMEZONEInfo` that round-trips through UTC.

Times not covered by the component raise
:class:`~icalendar.InsufficientTimezoneDataError` instead of silently
assuming UTC.
