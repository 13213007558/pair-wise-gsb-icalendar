"""Tests for merging calendars with conflicting VTIMEZONE definitions."""

from datetime import timezone as dt_timezone

import pytest

from icalendar import Calendar, InvalidCalendar, merge_calendars

CALENDAR_CN = (
    "BEGIN:VCALENDAR\n"
    "VERSION:2.0\n"
    "PRODID:merge-test\n"
    "BEGIN:VTIMEZONE\n"
    "TZID:Office\n"
    "BEGIN:STANDARD\n"
    "DTSTART:19700101T000000\n"
    "TZOFFSETFROM:+0800\n"
    "TZOFFSETTO:+0800\n"
    "TZNAME:CST\n"
    "END:STANDARD\n"
    "END:VTIMEZONE\n"
    "BEGIN:VEVENT\n"
    "UID:standup-cn\n"
    "DTSTAMP:20260922T000000Z\n"
    "DTSTART;TZID=Office:20261015T090000\n"
    "DTEND;TZID=Office:20261015T093000\n"
    "SUMMARY:CN standup\n"
    "DESCRIPTION:Daily sync at the Office\n"
    "END:VEVENT\n"
    "END:VCALENDAR\n"
)

CALENDAR_US = (
    "BEGIN:VCALENDAR\n"
    "VERSION:2.0\n"
    "PRODID:merge-test\n"
    "BEGIN:VTIMEZONE\n"
    "TZID:Office\n"
    "BEGIN:DAYLIGHT\n"
    "DTSTART:19700308T020000\n"
    "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU\n"
    "TZOFFSETFROM:-0500\n"
    "TZOFFSETTO:-0400\n"
    "TZNAME:EDT\n"
    "END:DAYLIGHT\n"
    "BEGIN:STANDARD\n"
    "DTSTART:19701101T020000\n"
    "RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU\n"
    "TZOFFSETFROM:-0400\n"
    "TZOFFSETTO:-0500\n"
    "TZNAME:EST\n"
    "END:STANDARD\n"
    "END:VTIMEZONE\n"
    "BEGIN:VEVENT\n"
    "UID:standup-us\n"
    "DTSTAMP:20260922T000000Z\n"
    "DTSTART;TZID=Office:20261015T090000\n"
    "DTEND;TZID=Office:20261015T093000\n"
    "RRULE:FREQ=WEEKLY;COUNT=4\n"
    "EXDATE;TZID=Office:20261029T090000\n"
    "RDATE;TZID=Office:20261101T090000,20261102T090000\n"
    "RDATE;TZID=Office:20261103T090000\n"
    "SUMMARY:US standup\n"
    "DESCRIPTION:Daily sync at the Office\n"
    "END:VEVENT\n"
    "BEGIN:VEVENT\n"
    "UID:standup-us-moved\n"
    "DTSTAMP:20260922T000000Z\n"
    "RECURRENCE-ID;TZID=Office:20261022T090000\n"
    "DTSTART;TZID=Office:20261022T100000\n"
    "DTEND;TZID=Office:20261022T103000\n"
    "SUMMARY:US standup moved\n"
    "END:VEVENT\n"
    "END:VCALENDAR\n"
)

DATETIME_PROPS = ("DTSTART", "DTEND", "RECURRENCE-ID", "RDATE", "EXDATE")


def _datetimes(value):
    """All datetimes of a property value, including multi-value lists."""
    for dt in getattr(value, "dts", [value]):
        yield getattr(dt, "dt", dt)


def _tzid_of(value):
    return str(value.params["TZID"])


def event_times_utc(calendar, event):
    """The (property, UTC datetime) pairs of an event's date/time properties."""
    timezones = {tz.tz_name: tz.to_tz(lookup_tzid=False) for tz in calendar.timezones}
    result = set()
    for name in DATETIME_PROPS:
        values = event.get(name)
        if values is None:
            continue
        if not isinstance(values, list):
            values = [values]
        for value in values:
            tzinfo = timezones[_tzid_of(value)]
            for dt in _datetimes(value):
                result.add(
                    (name, dt.replace(tzinfo=tzinfo).astimezone(dt_timezone.utc))
                )
    return result


def event_wall_times(event):
    """The (property, naive wall-clock datetime) pairs of an event."""
    result = set()
    for name in DATETIME_PROPS:
        values = event.get(name)
        if values is None:
            continue
        if not isinstance(values, list):
            values = [values]
        for value in values:
            for dt in _datetimes(value):
                result.add((name, dt))
    return result


def events_by_uid(calendar):
    return {event.uid: event for event in calendar.events}


@pytest.fixture
def calendar_cn():
    return Calendar.from_ical(CALENDAR_CN)


@pytest.fixture
def calendar_us():
    return Calendar.from_ical(CALENDAR_US)


@pytest.fixture
def merged(calendar_cn, calendar_us):
    return merge_calendars(calendar_cn, calendar_us)


def test_conflicting_timezone_is_renamed(merged):
    """The conflicting VTIMEZONE gets a new identity; the first keeps its TZID."""
    calendar, renames = merged
    assert renames == [{}, {"Office": "Office-2"}]
    assert sorted(tz.tz_name for tz in calendar.timezones) == ["Office", "Office-2"]


def test_only_referencing_components_are_updated(merged):
    """TZID parameters are renamed; text mentioning the TZID is not."""
    calendar, _ = merged
    events = events_by_uid(calendar)
    cn_event = events["standup-cn"]
    us_event = events["standup-us"]
    moved_event = events["standup-us-moved"]
    assert cn_event["DTSTART"].params["TZID"] == "Office"
    assert us_event["DTSTART"].params["TZID"] == "Office-2"
    assert us_event["DTEND"].params["TZID"] == "Office-2"
    assert us_event["EXDATE"].params["TZID"] == "Office-2"
    for rdate in us_event["RDATE"]:
        assert rdate.params["TZID"] == "Office-2"
    assert moved_event["RECURRENCE-ID"].params["TZID"] == "Office-2"
    assert str(us_event["DESCRIPTION"]) == "Daily sync at the Office"


def test_inputs_are_not_modified(calendar_cn, calendar_us):
    """Merging must not change the input calendars."""
    before_cn = calendar_cn.to_ical()
    before_us = calendar_us.to_ical()
    merge_calendars(calendar_cn, calendar_us)
    assert calendar_cn.to_ical() == before_cn
    assert calendar_us.to_ical() == before_us


def test_wall_clock_times_are_preserved(calendar_cn, calendar_us, merged):
    """Wall-clock times are kept as they are, not converted to UTC."""
    calendar, _ = merged
    merged_events = events_by_uid(calendar)
    for original in (calendar_cn, calendar_us):
        for uid, event in events_by_uid(original).items():
            assert event_wall_times(merged_events[uid]) == event_wall_times(event)


def test_utc_times_match_after_roundtrip(calendar_cn, calendar_us, merged, tmp_path):
    """Events mean the same UTC times after merging, writing, and re-reading."""
    calendar, _ = merged
    ics_file = tmp_path / "merged.ics"
    ics_file.write_bytes(calendar.to_ical())
    reread = Calendar.from_ical(ics_file.read_bytes())
    merged_events = events_by_uid(reread)
    for original in (calendar_cn, calendar_us):
        for uid, event in events_by_uid(original).items():
            assert event_times_utc(reread, merged_events[uid]) == event_times_utc(
                original, event
            )


def test_transition_rules_are_preserved(calendar_us, merged):
    """The renamed VTIMEZONE keeps the exact transition rules of its input."""
    calendar, _ = merged
    renamed = next(tz for tz in calendar.timezones if tz.tz_name == "Office-2")
    original = calendar_us.timezones[0]
    assert renamed.get_transitions() == original.get_transitions()


def test_same_definition_does_not_conflict():
    """Property order and line folding differences do not create a conflict."""
    folded = (
        "BEGIN:VCALENDAR\n"
        "VERSION:2.0\n"
        "PRODID:merge-test\n"
        "BEGIN:VTIMEZONE\n"
        "TZID:Office\n"
        "BEGIN:DAYLIGHT\n"
        "TZNAME:EasternDaylightTimeWithAnExtraLongNameThatIs\n"
        " Folded\n"
        "TZOFFSETTO:-0400\n"
        "TZOFFSETFROM:-0500\n"
        "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU\n"
        "DTSTART:19700308T020000\n"
        "END:DAYLIGHT\n"
        "BEGIN:STANDARD\n"
        "TZNAME:EST\n"
        "TZOFFSETTO:-0500\n"
        "TZOFFSETFROM:-0400\n"
        "RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU\n"
        "DTSTART:19701101T020000\n"
        "END:STANDARD\n"
        "END:VTIMEZONE\n"
        "BEGIN:VEVENT\n"
        "UID:standup-us\n"
        "DTSTAMP:20260922T000000Z\n"
        "DTSTART;TZID=Office:20261015T090000\n"
        "END:VEVENT\n"
        "END:VCALENDAR\n"
    )
    unfolded = (
        "BEGIN:VCALENDAR\n"
        "VERSION:2.0\n"
        "PRODID:merge-test\n"
        "BEGIN:VTIMEZONE\n"
        "TZID:Office\n"
        "BEGIN:STANDARD\n"
        "DTSTART:19701101T020000\n"
        "RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU\n"
        "TZOFFSETFROM:-0400\n"
        "TZOFFSETTO:-0500\n"
        "TZNAME:EST\n"
        "END:STANDARD\n"
        "BEGIN:DAYLIGHT\n"
        "DTSTART:19700308T020000\n"
        "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU\n"
        "TZOFFSETFROM:-0500\n"
        "TZOFFSETTO:-0400\n"
        "TZNAME:EasternDaylightTimeWithAnExtraLongNameThatIsFolded\n"
        "END:DAYLIGHT\n"
        "END:VTIMEZONE\n"
        "BEGIN:VEVENT\n"
        "UID:standup-us\n"
        "DTSTAMP:20260922T000000Z\n"
        "DTSTART;TZID=Office:20261015T090000\n"
        "END:VEVENT\n"
        "END:VCALENDAR\n"
    )
    merged, renames = merge_calendars(
        Calendar.from_ical(folded), Calendar.from_ical(unfolded)
    )
    assert renames == [{}, {}]
    assert [tz.tz_name for tz in merged.timezones] == ["Office"]


def test_identical_duplicate_definitions_are_deduplicated():
    """The same VTIMEZONE twice in one calendar is merged only once."""
    timezone = CALENDAR_CN.split("BEGIN:VTIMEZONE")[1].split(
        "END:VTIMEZONE", maxsplit=1
    )[0]
    ics = CALENDAR_CN.replace(
        "BEGIN:VEVENT",
        "BEGIN:VTIMEZONE" + timezone + "END:VTIMEZONE\nBEGIN:VEVENT",
        1,
    )
    merged, renames = merge_calendars(
        Calendar.from_ical(ics), Calendar.from_ical(CALENDAR_CN)
    )
    assert renames == [{}, {}]
    assert [tz.tz_name for tz in merged.timezones] == ["Office"]


def test_missing_definition_is_reported():
    """A referenced TZID without a VTIMEZONE is reported before merging."""
    ics = (
        "BEGIN:VCALENDAR\n"
        "VERSION:2.0\n"
        "PRODID:merge-test\n"
        "BEGIN:VEVENT\n"
        "UID:missing\n"
        "DTSTART;TZID=Nowhere:20261015T090000\n"
        "END:VEVENT\n"
        "END:VCALENDAR\n"
    )
    with pytest.raises(InvalidCalendar, match="Nowhere"):
        merge_calendars(Calendar.from_ical(ics), Calendar.from_ical(CALENDAR_CN))


def test_contradictory_definitions_are_reported():
    """Two contradictory VTIMEZONEs for one TZID are reported before merging."""
    contradictory = CALENDAR_CN.replace("+0800", "+0900").replace(
        "TZNAME:CST", "TZNAME:JST"
    )
    timezone = contradictory.split("BEGIN:VTIMEZONE")[1].split("END:VTIMEZONE")[0]
    ics = CALENDAR_CN.replace(
        "BEGIN:VEVENT",
        "BEGIN:VTIMEZONE" + timezone + "END:VTIMEZONE\nBEGIN:VEVENT",
        1,
    )
    with pytest.raises(InvalidCalendar, match="contradictory"):
        merge_calendars(Calendar.from_ical(ics), Calendar.from_ical(CALENDAR_CN))


def test_custom_namer_is_used(calendar_cn, calendar_us):
    """The caller decides how conflicting timezones are named."""
    _, renames = merge_calendars(
        calendar_cn, calendar_us, tzid_namer=lambda tzid, i: f"{tzid}@cal-{i}"
    )
    assert renames == [{}, {"Office": "Office@cal-1"}]


def test_generated_name_does_not_collide(calendar_cn, calendar_us):
    """A generated TZID never collides with a TZID of any input calendar."""
    ics = CALENDAR_CN.replace("TZID:Office", "TZID:Office-2").replace(
        "TZID=Office", "TZID=Office-2"
    )
    calendar_with_office_2 = Calendar.from_ical(ics)
    merged, renames = merge_calendars(
        calendar_cn,
        calendar_with_office_2,
        calendar_us,
        tzid_namer=lambda tzid, _index: f"{tzid}-2",
    )
    assert renames == [{}, {}, {"Office": "Office-2-2"}]
    assert sorted(tz.tz_name for tz in merged.timezones) == [
        "Office",
        "Office-2",
        "Office-2-2",
    ]


def test_merging_requires_two_calendars(calendar_cn):
    with pytest.raises(ValueError, match="at least two"):
        merge_calendars(calendar_cn)
