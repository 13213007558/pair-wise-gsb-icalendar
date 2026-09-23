Add proximity-triggered ``VALARM`` computation following :rfc:`9074`.
:meth:`Alarms.proximity_times <icalendar.alarms.Alarms.proximity_times>` evaluates an external
:class:`~icalendar.alarms.LocationEvent` stream against the alarm's ``VLOCATION`` geofence
(``ARRIVE``/``DEPART``) and returns explainable :class:`~icalendar.alarms.ProximityTrigger`
records that combine ``REPEAT``, ``DURATION``, ``ACKNOWLEDGED`` and snooze state.
Time-based alarms still use the existing :attr:`Alarms.times <icalendar.alarms.Alarms.times>` path.
