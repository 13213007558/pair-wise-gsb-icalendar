"""Tests for :mod:`icalendar.lossless` - the lossless document editing mode.

The fixture :file:`mixed_content.ics` combines repeated properties and
multi-value parameters, escaped commas, mixed folding styles (CRLF+space and
LF+tab), folded Chinese text, a lowercase property name, and unknown X-
properties.
"""

from pathlib import Path

import pytest

from icalendar import Calendar, LosslessDocument, LosslessEditingError

FIXTURE = Path(__file__).parent / "mixed_content.ics"
VCALENDAR = ("VCALENDAR",)
VEVENT = ("VCALENDAR", "VEVENT")

NEW_SUMMARY = (
    "季度同步会议：讨论第三季度规划、预算与资源安排，请所有成员提前准备材料并准时参加。"
)

SUMMARY_LINE = b"SUMMARY:Team sync\\, Q3 planning\r\n"
DESCRIPTION_LINES = (
    b"DESCRIPTION:Weekly sync with a long description that is folded across\r\n"
    b" two physical lines using CRLF and a space.\r\n"
)
FIRST_ATTENDEE = (
    b'ATTENDEE;ROLE=REQ-PARTICIPANT;PARTSTAT=ACCEPTED;CN="Doe, John":'
    b"mailto:jd@example.com\r\n"
)
X_CUSTOM_PROP = (
    b"X-CUSTOM-PROP;X-FOO=bar:First part of a long unknown value that keeps going\n"
    b"\ton the next physical line with LF and a tab\n"
)
X_CHINESE_NOTE = (
    "X-CHINESE-NOTE:这是一段需要折行的中文备注，\r\n"
    " 用来验证未知属性在编辑后保持字节不变。\r\n"
).encode()


@pytest.fixture
def original() -> bytes:
    return FIXTURE.read_bytes()


@pytest.fixture
def doc(original) -> LosslessDocument:
    return LosslessDocument.from_ical(original)


def vevent_of(data: bytes):
    return Calendar.from_ical(data).walk("VEVENT")[0]


class TestZeroModification:
    def test_roundtrip_is_byte_identical(self, doc, original):
        assert doc.to_ical() == original

    def test_read_only_access_does_not_modify(self, doc, original):
        doc.get_value(VEVENT, "SUMMARY")
        doc.get_params(VEVENT, "ATTENDEE", 1)
        doc.count(VEVENT, "ATTENDEE")
        assert doc.to_ical() == original


class TestReplace:
    def test_only_the_summary_line_changes(self, doc, original):
        doc.set(VEVENT, "SUMMARY", NEW_SUMMARY)
        out = doc.to_ical()
        prefix, suffix = original.split(SUMMARY_LINE)
        assert out.startswith(prefix)
        assert out.endswith(suffix)

    def test_long_chinese_value_is_folded_by_utf8_bytes(self, doc):
        doc.set(VEVENT, "SUMMARY", NEW_SUMMARY)
        out = doc.to_ical()
        start = out.index(b"SUMMARY:")
        end = out.index(b"DESCRIPTION:")
        middle = out[start:end]
        assert b"\r\n " in middle  # the long value was folded
        physical_lines = middle[:-2].split(b"\r\n")
        assert len(physical_lines) > 1
        for line in physical_lines:
            assert len(line) <= 75
            line.decode("utf-8")  # no multi-byte character may be split
        unfolded = middle[:-2].replace(b"\r\n ", b"").decode("utf-8")
        assert unfolded == "SUMMARY:" + NEW_SUMMARY

    def test_occurrence_selects_among_same_named_properties(self, doc, original):
        doc.set(VEVENT, "ATTENDEE", "mailto:replacement@example.com", occurrence=1)
        out = doc.to_ical()
        # the first ATTENDEE keeps its original bytes
        assert FIRST_ATTENDEE in out
        assert b"mailto:jane@example.com" not in out
        # the second ATTENDEE keeps its original parameters (the line is
        # longer than 75 octets now, so it was re-folded)
        unfolded = out.replace(b"\r\n ", b"")
        assert (
            b'CN=Jane;DELEGATED-TO="mailto:a@example.com","mailto:b@example.com"'
            in unfolded
        )
        event = vevent_of(out)
        attendees = event["ATTENDEE"]
        assert str(attendees[0]) == "mailto:jd@example.com"
        assert str(attendees[1]) == "mailto:replacement@example.com"
        assert attendees[1].params["CN"] == "Jane"
        assert attendees[1].params["DELEGATED-TO"] == [
            "mailto:a@example.com",
            "mailto:b@example.com",
        ]

    def test_set_keeps_original_name_case_and_parameter_text(self, doc):
        doc.set(VEVENT, "X-MIXED-CASE", "New Value")
        assert b"x-mixed-case;X-ParaM=Kept:New Value\r\n" in doc.to_ical()

    def test_set_with_new_parameters(self, doc):
        doc.set(
            VEVENT,
            "ATTENDEE",
            "mailto:new@example.com",
            occurrence=0,
            params={"CN": "New Person"},
        )
        event = vevent_of(doc.to_ical())
        first = event["ATTENDEE"][0]
        assert str(first) == "mailto:new@example.com"
        assert first.params["CN"] == "New Person"
        assert "ROLE" not in first.params


class TestAdd:
    def test_add_inserts_before_component_end(self, doc, original):
        doc.add(VEVENT, "LOCATION", "会议室A")
        inserted = "LOCATION:会议室A\r\n".encode()
        assert doc.to_ical() == original.replace(
            b"END:VEVENT\r\n", inserted + b"END:VEVENT\r\n"
        )

    def test_add_with_parameters(self, doc):
        doc.add(VCALENDAR, "X-WR-TIMEZONE", "Asia/Shanghai", params={"X-SRC": "test"})
        calendar = Calendar.from_ical(doc.to_ical())
        assert str(calendar["X-WR-TIMEZONE"]) == "Asia/Shanghai"
        assert calendar["X-WR-TIMEZONE"].params["X-SRC"] == "test"


class TestDelete:
    def test_delete_removes_all_continuation_lines(self, doc, original):
        doc.delete(VEVENT, "DESCRIPTION")
        out = doc.to_ical()
        prefix, suffix = original.split(DESCRIPTION_LINES)
        assert out == prefix + suffix
        assert b"two physical lines" not in out

    def test_delete_one_occurrence_keeps_the_other(self, doc):
        doc.delete(VEVENT, "ATTENDEE", occurrence=0)
        out = doc.to_ical()
        assert FIRST_ATTENDEE not in out
        assert b"mailto:jane@example.com" in out
        assert str(vevent_of(out)["ATTENDEE"]) == "mailto:jane@example.com"


class TestPreservation:
    def test_unknown_properties_and_mixed_folding_survive_edits(self, doc):
        doc.set(VEVENT, "SUMMARY", "Short")
        out = doc.to_ical()
        assert X_CUSTOM_PROP in out  # LF + tab fold, unknown X- property
        assert X_CHINESE_NOTE in out  # folded Chinese unknown property
        assert (
            b'X-ALT;ALTREP="cid:part1.0001@example.org":The Fall\'98 Wildfire\r\n'
            in out
        )
        assert b"x-mixed-case;X-ParaM=Kept:Some Value\r\n" in out
        assert b"X-WR-CALNAME:Team Calendar\r\n" in out

    def test_new_values_are_readable_with_calendar_from_ical(self, doc):
        doc.set(VEVENT, "SUMMARY", NEW_SUMMARY)
        doc.set(VEVENT, "ATTENDEE", "mailto:new@example.com", occurrence=1)
        doc.add(VEVENT, "LOCATION", "会议室A")
        doc.delete(VEVENT, "DESCRIPTION")
        event = vevent_of(doc.to_ical())
        assert event["SUMMARY"] == NEW_SUMMARY
        assert [str(a) for a in event["ATTENDEE"]] == [
            "mailto:jd@example.com",
            "mailto:new@example.com",
        ]
        assert event["LOCATION"] == "会议室A"
        assert "DESCRIPTION" not in event
        assert event["CATEGORIES"].cats == ["Work,Internal", "Planning"]


class TestAccess:
    def test_get_value_unescapes_text(self, doc):
        assert doc.get_value(VEVENT, "SUMMARY") == "Team sync, Q3 planning"

    def test_get_params(self, doc):
        params = doc.get_params(VEVENT, "ATTENDEE", 0)
        assert params["CN"] == "Doe, John"
        assert params["ROLE"] == "REQ-PARTICIPANT"
        multi = doc.get_params(VEVENT, "ATTENDEE", 1)
        assert multi["DELEGATED-TO"] == [
            "mailto:a@example.com",
            "mailto:b@example.com",
        ]

    def test_count(self, doc):
        assert doc.count(VEVENT, "ATTENDEE") == 2
        assert doc.count(VEVENT, "SUMMARY") == 1
        assert doc.count(VEVENT, "LOCATION") == 0

    def test_unknown_component_path_raises_key_error(self, doc):
        with pytest.raises(KeyError):
            doc.get_value(("VCALENDAR", "VTODO"), "SUMMARY")

    def test_component_occurrence_out_of_range_raises_key_error(self, doc):
        with pytest.raises(KeyError):
            doc.get_value(("VCALENDAR", ("VEVENT", 1)), "SUMMARY")

    def test_property_occurrence_out_of_range_raises_key_error(self, doc):
        with pytest.raises(KeyError):
            doc.get_value(VEVENT, "ATTENDEE", 2)


CORRUPT = {
    "unclosed_component": b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:1\r\nEND:VEVENT\r\n",
    "mismatched_end": b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VEVENT\r\n",
    "end_without_begin": b"END:VCALENDAR\r\n",
    "property_outside_component": b"VERSION:2.0\r\nBEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n",
    "line_without_colon": b"BEGIN:VCALENDAR\r\nGARBAGE\r\nEND:VCALENDAR\r\n",
    "continuation_without_start": b" folded\r\nBEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n",
    "invalid_utf8": b"BEGIN:VCALENDAR\r\nX-BAD:\xff\xfe\r\nEND:VCALENDAR\r\n",
    "begin_with_params": b"BEGIN;X=1:VCALENDAR\r\nEND:VCALENDAR\r\n",
}


class TestCorruptInput:
    @pytest.mark.parametrize("data", CORRUPT.values(), ids=CORRUPT.keys())
    def test_editable_mode_is_refused(self, data):
        with pytest.raises(LosslessEditingError):
            LosslessDocument.from_ical(data)

    def test_lenient_parse_still_available(self):
        """The regular lenient parser still accepts what we refuse to edit."""
        data = CORRUPT["mismatched_end"]
        with pytest.raises(LosslessEditingError):
            LosslessDocument.from_ical(data)
        calendar = Calendar.from_ical(data)
        assert calendar["VERSION"] == "2.0"
