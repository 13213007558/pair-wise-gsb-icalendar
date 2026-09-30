"""Tests for the explicit recurring-series consistency check.

See ``calendars/series_consistency_conflicts.ics`` for the mixed fixture
containing healthy and conflicting series.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from icalendar import (
    Calendar,
    Event,
    SeriesConsistencyError,
    SeriesConsistencyErrorFound,
    SeriesConsistencyReport,
    SeriesErrorCode,
    check_series_consistency,
)


ICS_PATH = Path(__file__).parent / "calendars/series_consistency_conflicts.ics"


def _event_lines(
    uid: str,
    *,
    recurrence_id: str | None = None,
    dtstart: str,
    extra: str = "",
) -> str:
    lines = "BEGIN:VEVENT\n"
    lines += f"UID:{uid}\nDTSTAMP:20260101T000000Z\n"
    if recurrence_id is not None:
        lines += f"RECURRENCE-ID{recurrence_id}\n"
    lines += f"DTSTART{dtstart}\n{extra}END:VEVENT\n"
    return lines


def _calendar(*events: str) -> str:
    return (
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//test//EN\n"
        + "".join(events)
        + "END:VCALENDAR\n"
    )


def test_mixed_calendar_reports_the_full_error_set(calendars):
    """Every independent conflict is reported in one pass."""
    report = check_series_consistency(
        calendars.series_consistency_conflicts
    )
    assert not report.ok
    assert len(report) == 15
    assert report.codes() == [
        SeriesErrorCode.PARSE_ERROR,
        SeriesErrorCode.PARSE_ERROR,
        SeriesErrorCode.RRULE_COUNT_AND_UNTIL,
        SeriesErrorCode.RRULE_UNTIL_VALUE_TYPE_MISMATCH,
        SeriesErrorCode.RRULE_UNTIL_TIME_FORM_MISMATCH,
        SeriesErrorCode.NEGATIVE_DURATION,
        SeriesErrorCode.RDATE_VALUE_TYPE_MISMATCH,
        SeriesErrorCode.EXDATE_VALUE_TYPE_MISMATCH,
        SeriesErrorCode.END_AND_DURATION,
        SeriesErrorCode.END_BEFORE_START,
        SeriesErrorCode.RECURRENCE_ID_VALUE_TYPE_MISMATCH,
        SeriesErrorCode.RECURRENCE_ID_VALUE_TYPE_MISMATCH,
        SeriesErrorCode.DUPLICATE_RECURRENCE_ID,
        SeriesErrorCode.MASTER_NOT_UNIQUE,
        SeriesErrorCode.THISANDFUTURE_CONFLICT,
    ]
    duplicate = report.for_uid("date-form@example.com")[-1]
    assert duplicate.component_index == 4
    assert duplicate.other_component_index == 3
    assert duplicate.property_path == ("RECURRENCE-ID",)
    conflict = report.for_uid("this-and-future@example.com")[0]
    assert conflict.component_index == 9
    assert conflict.other_component_index == 10


def test_error_identifiers_stay_stable_when_one_error_is_fixed(calendars):
    """Fixing one error in place must not change the other error identities.

    ``COUNT`` is dropped from the master's ``RRULE``, removing only the
    COUNT/UNTIL exclusivity error. Its DATE-vs-DATE-TIME UNTIL error and all
    other components keep their component index and property path.
    """
    calendar = calendars.series_consistency_conflicts
    original = calendar.check_series_consistency()

    master = next(
        component
        for component in calendar.subcomponents
        if component.name == "VEVENT"
        and str(component.get("UID")) == "date-form@example.com"
        and component.get("RECURRENCE-ID") is None
    )
    rrule = master["RRULE"]
    del rrule["COUNT"]
    fixed = calendar.check_series_consistency()

    removed = {
        (SeriesErrorCode.RRULE_COUNT_AND_UNTIL, 2, ("RRULE",)),
    }
    def identity(error: SeriesConsistencyError):
        return error.code, error.component_index, error.property_path

    before = [identity(error) for error in original]
    after = [identity(error) for error in fixed]
    assert after == [item for item in before if item not in removed]


def test_healthy_series_is_ok(calendars):
    """A normal all-day series with one exception has no errors."""
    report = calendars.series_consistency_conflicts.check_series_consistency()
    assert report.for_uid("ok-series@example.com") == ()


def test_parse_errors_are_reported_and_checking_continues():
    """Broken properties become PARSE_ERROR entries, never abort the check."""
    ics = _calendar(
        _event_lines(
            "broken@example.com",
            dtstart=":NOT-A-DATE",
            extra="RRULE:FREQ=DAILY;UNTIL=also-broken\nX-KEEP:value\n",
        )
    )
    report = check_series_consistency(ics)
    codes = {(error.code, error.path) for error in report}
    assert (SeriesErrorCode.PARSE_ERROR, "DTSTART") in codes
    assert (SeriesErrorCode.PARSE_ERROR, "RRULE") in codes
    calendar = Calendar.from_ical(ics)
    assert str(calendar.events[0]["X-KEEP"]) == "value"
    assert str(calendar.events[0]["DTSTART"]) == "NOT-A-DATE"


def test_unknown_x_properties_are_preserved(calendars):
    """The checker does not touch non-standard properties."""
    event = calendars.series_consistency_conflicts.events[5]
    assert str(event["X-APPLE-CREATOR-IDENTITY"]) == "preserve-me"


def test_from_ical_stays_lenient():
    """Calendar.from_ical keeps its existing tolerant VEVENT reading."""
    ics = _calendar(
        _event_lines("lenient@example.com", dtstart=":20260101T100000Z")
    )
    calendar = Calendar.from_ical(ics)
    assert calendar.check_series_consistency().ok


def test_recurrence_id_time_form_mismatch_is_detected():
    """UTC master DTSTART vs time-zoned RECURRENCE-ID is a time-form error."""
    ics = _calendar(
        _event_lines(
            "form@example.com",
            dtstart=":20260101T100000Z",
            extra="RRULE:FREQ=DAILY\n",
        ),
        _event_lines(
            "form@example.com",
            recurrence_id=";TZID=America/New_York:20260103T100000",
            dtstart=";TZID=America/New_York:20260103T100000",
        ),
    )
    report = check_series_consistency(ics)
    assert report.codes() == [SeriesErrorCode.RECURRENCE_ID_TIME_FORM_MISMATCH]


def test_thisandfuture_conflict_ignores_moved_dtstart():
    """Exception identity is RECURRENCE-ID, not the modified DTSTART."""
    ics = _calendar(
        _event_lines(
            "future@example.com",
            dtstart=";VALUE=DATE:20260101",
            extra="RRULE:FREQ=DAILY\n",
        ),
        _event_lines(
            "future@example.com",
            recurrence_id=";RANGE=THISANDFUTURE;VALUE=DATE:20260110",
            dtstart=";VALUE=DATE:20260120",
        ),
        _event_lines(
            "future@example.com",
            recurrence_id=";RANGE=THISANDFUTURE;VALUE=DATE:20260115",
            dtstart=";VALUE=DATE:20260115",
        ),
    )
    report = check_series_consistency(ics)
    assert report.codes() == [SeriesErrorCode.THISANDFUTURE_CONFLICT]
    error = report.errors[0]
    assert error.component_index == 1
    assert error.other_component_index == 2


def test_non_overlapping_thisandfuture_claims_are_ok():
    """Two THISANDFUTURE exceptions on unrelated series are independent."""
    ics = _calendar(
        _event_lines("a@example.com", dtstart=";VALUE=DATE:20260101"),
        _event_lines(
            "a@example.com",
            recurrence_id=";RANGE=THISANDFUTURE;VALUE=DATE:20260110",
            dtstart=";VALUE=DATE:20260110",
        ),
        _event_lines("b@example.com", dtstart=";VALUE=DATE:20260101"),
        _event_lines(
            "b@example.com",
            recurrence_id=";RANGE=THISANDFUTURE;VALUE=DATE:20260110",
            dtstart=";VALUE=DATE:20260110",
        ),
    )
    assert check_series_consistency(ics).ok


def test_count_until_and_until_rules_on_built_components():
    """The check works on calendars built programmatically, not just parsed."""
    calendar = Calendar()
    master = Event()
    master.add("UID", "built@example.com")
    master.add("DTSTART", datetime(2026, 1, 1, 10, tzinfo=timezone.utc))
    master.add(
        "RRULE",
        {
            "FREQ": ["DAILY"],
            "COUNT": [3],
            "UNTIL": [datetime(2026, 1, 10, 10, tzinfo=timezone.utc)],
        },
    )
    calendar.add_component(master)
    report = calendar.check_series_consistency()
    assert report.codes() == [SeriesErrorCode.RRULE_COUNT_AND_UNTIL]


def test_end_and_duration_and_all_day_duration_rules():
    """DTEND+DURATION, negative durations and all-day durations are caught."""
    ics = _calendar(
        _event_lines(
            "end@example.com",
            dtstart=";VALUE=DATE:20260101",
            extra=(
                "DTEND;VALUE=DATE:20260102\n"
                "DURATION:PT2H\n"
                "RRULE:FREQ=DAILY\n"
            ),
        )
    )
    report = check_series_consistency(ics)
    assert set(report.codes()) == {
        SeriesErrorCode.END_AND_DURATION,
        SeriesErrorCode.DURATION_VALUE_TYPE_MISMATCH,
    }


def test_infinite_series_is_not_expanded():
    """A series with no COUNT/UNTIL is checked structurally only."""
    ics = _calendar(
        _event_lines(
            "infinite@example.com",
            dtstart=":20260101T100000Z",
            extra="RRULE:FREQ=DAILY\n",
        )
    )
    assert check_series_consistency(ics).ok


def test_report_is_a_public_dataclass_and_can_raise():
    """Report attributes and the explicit raise helper are usable."""
    report = SeriesConsistencyReport(())
    assert report.ok and len(report) == 0 and list(report) == []
    bad = check_series_consistency(
        _calendar(
            _event_lines("x@example.com", dtstart=":20260101T100000Z"),
            _event_lines("x@example.com", dtstart=":20260102T100000Z"),
        )
    )
    assert isinstance(bad, SeriesConsistencyReport)
    with pytest.raises(SeriesConsistencyErrorFound) as exc_info:
        bad.raise_if_errors()
    assert exc_info.value.report is bad
    assert "MASTER_NOT_UNIQUE" in str(exc_info.value)


def test_path_rendering():
    """Property paths are rendered in a stable, readable form."""
    error = SeriesConsistencyError(
        code=SeriesErrorCode.RRULE_UNTIL_VALUE_TYPE_MISMATCH,
        uid="u@example.com",
        component_index=2,
        property_path=("RRULE", 1, "UNTIL"),
        message="bad",
    )
    assert error.path == "RRULE[1].UNTIL"
    assert "component[2]" in str(error)
    assert "u@example.com" in str(error)


def test_bytes_str_and_path_inputs_are_accepted(calendars):
    """Raw input types all route through the same read-only check."""
    raw = ICS_PATH.read_bytes()
    assert check_series_consistency(raw).codes() == check_series_consistency(
        raw.decode()
    ).codes()
    assert check_series_consistency(ICS_PATH).codes() == check_series_consistency(
        Calendar.from_ical(raw)
    ).codes()


def test_rdate_periods_are_compatible_with_datetime_series():
    """PERIOD RDATE values are fine on a DATE-TIME series."""
    ics = _calendar(
        _event_lines(
            "periods@example.com",
            dtstart=":20260101T100000Z",
            extra="RDATE;VALUE=PERIOD:20260105T100000Z/20260105T110000Z\n",
        )
    )
    assert check_series_consistency(ics).ok


def test_todo_due_and_duration_and_repeated_property_paths():
    """VTODO uses DUE as its end property; occurrence indices stay stable."""
    ics = (
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//test//EN\n"
        "BEGIN:VTODO\nUID:todo@example.com\n"
        "DTSTART;VALUE=DATE:20260101\n"
        "DUE;VALUE=DATE:20260201\nDURATION:P1D\n"
        "RRULE:FREQ=DAILY\nRRULE:FREQ=WEEKLY;COUNT=2;UNTIL=20260110\n"
        "EXDATE;VALUE=DATE:20260103\n"
        "EXDATE:20260104T100000Z\n"
        "END:VTODO\nEND:VCALENDAR\n"
    )
    report = check_series_consistency(ics)
    paths = {(error.code, error.path) for error in report}
    assert (SeriesErrorCode.END_AND_DURATION, "DURATION") in paths
    assert (
        SeriesErrorCode.RRULE_COUNT_AND_UNTIL,
        "RRULE[1]",
    ) in paths
    assert (
        SeriesErrorCode.EXDATE_VALUE_TYPE_MISMATCH,
        "EXDATE[1]",
    ) in paths
    # The well-formed first RRULE and first EXDATE produce nothing.
    assert all(
        error.property_path[:1] != ("RRULE", 0) for error in report.errors
    )


def test_second_exdate_same_line_uses_single_occurrence_path():
    """Comma-separated values on one line share the property occurrence."""
    ics = (
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//test//EN\n"
        "BEGIN:VEVENT\nUID:multi@example.com\n"
        "DTSTART:20260101T100000Z\n"
        "EXDATE:20260103T100000Z,20260104T100000Z\n"
        "END:VEVENT\nEND:VCALENDAR\n"
    )
    assert check_series_consistency(ics).ok


def test_exceptions_without_uid_are_still_checked():
    """Components without a UID cannot be grouped, but self-checks run."""
    ics = (
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//test//EN\n"
        "BEGIN:VEVENT\n"
        "DTSTART:20260101T100000Z\nDURATION:-PT1H\n"
        "END:VEVENT\nEND:VCALENDAR\n"
    )
    report = check_series_consistency(ics)
    assert report.codes() == [SeriesErrorCode.NEGATIVE_DURATION]
    assert report.errors[0].uid is None
