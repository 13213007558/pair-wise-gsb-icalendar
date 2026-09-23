Add :func:`icalendar.merge_calendars`, a new entry point for merging calendars.
When the inputs declare the same ``TZID`` with conflicting ``VTIMEZONE``
definitions, the conflicting timezone is assigned a new identity and only the
components referencing it are updated.  Timezone definitions are compared
semantically, so property order and line folding differences do not create
conflicts.  Wall-clock times and transition rules are preserved, the inputs
are left unchanged, and a rename mapping is returned.  Missing or
contradictory timezone definitions are reported before anything is merged.
