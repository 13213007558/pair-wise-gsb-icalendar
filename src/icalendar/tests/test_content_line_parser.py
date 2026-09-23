"""Tests for RFC 5545 content line folding and parameter tokenization.

Folding is defined on the octet stream, so unfolding happens on the raw
bytes before decoding: a fold point can never corrupt a multi-byte UTF-8
character. Parameter parsing distinguishes quoted and unquoted tokens,
while property values use backslash escaping - two separate boundaries.
"""

from datetime import datetime, timezone

import pytest

from icalendar import Calendar, Event, vCalAddress
from icalendar.error import ParseError
from icalendar.parser import Contentline, Contentlines, Parameters


def fold_at(data: bytes, offset: int, marker: bytes = b"\r\n ") -> bytes:
    """Insert a fold marker into raw data at the given byte offset."""
    return data[:offset] + marker + data[offset:]


class TestByteFolding:
    """Unfolding must not split multi-byte UTF-8 characters."""

    def test_fold_inside_chinese_character(self):
        description = "DESCRIPTION:" + "汉" * 40
        raw = description.encode("utf-8")
        # fold in the middle of the 10th 汉 (3 UTF-8 bytes each)
        cut = len(b"DESCRIPTION:") + 3 * 10 + 1
        line = Contentline.from_ical(fold_at(raw, cut))
        assert line == description
        assert line.parts()[2] == "汉" * 40

    def test_fold_inside_four_byte_emoji(self):
        description = "DESCRIPTION:party " + "🎉" * 20
        raw = description.encode("utf-8")
        # fold between the 2nd and 3rd byte of the 5th emoji
        cut = len(b"DESCRIPTION:party ") + 4 * 4 + 2
        line = Contentline.from_ical(fold_at(raw, cut))
        assert line == description
        assert line.parts()[2] == "party " + "🎉" * 20

    def test_fold_inside_escaped_semicolon(self):
        # a fold between the backslash and the semicolon of an escaped ;
        line = Contentline.from_ical(b"DESCRIPTION:a\\" + b"\r\n " + b";b")
        assert line == "DESCRIPTION:a\\;b"
        assert line.parts()[2] == "a;b"
        # the raw parts keep the escape sequence verbatim
        assert line.raw_parts()[2] == "a\\;b"

    def test_fold_inside_quoted_parameter(self):
        line = Contentline.from_ical(
            b'ATTENDEE;CN="Doe, John' + b"\r\n " + b' Jr.";ROLE=REQ-PARTICIPANT'
            b":mailto:jd@example.com"
        )
        name, params, value = line.parts()
        assert name == "ATTENDEE"
        assert params["CN"] == "Doe, John Jr."
        assert params["ROLE"] == "REQ-PARTICIPANT"
        assert value == "mailto:jd@example.com"

    def test_fold_with_tab_continuation(self):
        description = "DESCRIPTION:" + "字" * 30
        raw = description.encode("utf-8")
        cut = len(b"DESCRIPTION:") + 3 * 7 + 1
        line = Contentline.from_ical(fold_at(raw, cut, marker=b"\r\n\t"))
        assert line == description

    def test_short_ascii_lines_unchanged(self):
        assert Contentline.from_ical(b"SUMMARY:Hello") == "SUMMARY:Hello"
        assert Contentline("SUMMARY:Hello").to_ical() == b"SUMMARY:Hello"
        assert Contentlines.from_ical(b"A:1\r\nB:2\r\n") == ["A:1", "B:2", ""]
        raw = (
            b"BEGIN:VCALENDAR\r\n"
            b"VERSION:2.0\r\n"
            b"PRODID:-//Test//Test//EN\r\n"
            b"END:VCALENDAR\r\n"
        )
        assert Calendar.from_ical(raw).to_ical() == raw


class TestCalendarRoundTrip:
    """A calendar with tricky fold points survives parse/to_ical byte-exactly."""

    @pytest.fixture()
    def raw_ics(self):
        cal = Calendar()
        cal.add("PRODID", "-//Test//Fold//EN")
        cal.add("VERSION", "2.0")
        event = Event()
        event.add("UID", "fold-test@example.com")
        event.add("DTSTAMP", datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc))
        event.add("DTSTART", datetime(2026, 9, 24, 9, 0, 0, tzinfo=timezone.utc))
        # Chinese characters, 4-byte emoji and escaped separators force
        # folds next to multi-byte characters and escape sequences.
        event.add(
            "DESCRIPTION",
            "会议记录" * 20 + "🎉🎊" * 15 + "agenda: item one; item two, item three",
        )
        # a long quoted parameter value folds inside the quotes
        attendee = vCalAddress("mailto:jd@example.com")
        attendee.params["CN"] = "张三, 李四; " + "🎉" * 30
        event.add("ATTENDEE", attendee)
        cal.add_component(event)
        return cal.to_ical()

    def test_folds_happen(self, raw_ics):
        assert b"\r\n " in raw_ics

    def test_fold_lands_inside_quoted_parameter(self, raw_ics):
        start = raw_ics.index(b'CN="')
        end = raw_ics.index(b'"', start + len(b'CN="'))
        assert b"\r\n " in raw_ics[start:end]

    def test_multibyte_characters_are_not_split(self, raw_ics):
        assert "🎉".encode() in raw_ics
        assert "会".encode() in raw_ics

    def test_round_trip_is_byte_exact(self, raw_ics):
        reparsed = Calendar.from_ical(raw_ics)
        assert reparsed.to_ical() == raw_ics

    def test_values_survive(self, raw_ics):
        event = Calendar.from_ical(raw_ics).walk("VEVENT")[0]
        description = event["DESCRIPTION"]
        assert "会议记录" in description
        assert "🎉🎊" in description
        # the escaped separators of the TEXT value are unescaped
        assert "item one; item two, item three" in description
        attendee = event["ATTENDEE"]
        assert attendee.params["CN"] == "张三, 李四; " + "🎉" * 30


class TestParameterTokenization:
    """Quoted and unquoted parameter tokens follow different rules."""

    def test_quoted_and_unquoted_tokens(self):
        line = Contentline(
            'ATTENDEE;ROLE=REQ-PARTICIPANT;CN="Doe, John";X-SEMI="a;b"'
            ":mailto:jd@example.com"
        )
        name, params, value = line.parts()
        assert name == "ATTENDEE"
        assert params["ROLE"] == "REQ-PARTICIPANT"
        assert params["CN"] == "Doe, John"
        assert params["X-SEMI"] == "a;b"
        assert value == "mailto:jd@example.com"

    def test_separators_inside_quotes_do_not_start_new_parameters(self):
        params = Parameters.from_ical('CN="a;b,c=d";ROLE=CHAIR')
        assert list(params.items()) == [("CN", "a;b,c=d"), ("ROLE", "CHAIR")]

    def test_parameter_order_is_preserved(self):
        params = Parameters.from_ical("B=2;A=1;C=3")
        assert list(params.keys()) == ["B", "A", "C"]

    def test_duplicate_parameter_last_one_wins(self):
        params = Parameters.from_ical("X-A=1;X-A=2")
        assert params["X-A"] == "2"

    def test_backslash_escapes_in_unquoted_value(self):
        # backslash escapes in the parameter section are resolved while
        # the content line is split, before Parameters sees the tokens
        _, params, _ = Contentline("X-P;LABEL=a\\,b\\;c:v").parts()
        assert params["LABEL"] == "a,b;c"

    def test_unquoted_values_are_uppercased_in_strict_mode(self):
        params = Parameters.from_ical('role=chair;CN="Doe, John"', strict=True)
        assert params["ROLE"] == "CHAIR"
        assert params["CN"] == "Doe, John"

    def test_value_escaping_and_parameter_quoting_are_separate(self):
        line = Contentline('DESCRIPTION;X-SEMI="a;b":x\\,y\\;z')
        _, params, value = line.parts()
        # the parameter value is protected by its quotes
        assert params["X-SEMI"] == "a;b"
        # the property value uses backslash escaping, not quoting
        assert value == "x,y;z"

    def test_percent_encoding_is_not_decoded_in_property_values(self):
        # the percent transport is internal to parameter parsing only
        assert Contentline("DESCRIPTION:100%2C sure").parts()[2] == "100%2C sure"


class TestParseErrors:
    """Malformed input raises ParseError with the offset of the problem."""

    def test_parse_error_is_a_value_error(self):
        assert issubclass(ParseError, ValueError)

    def test_bare_carriage_return(self):
        with pytest.raises(ParseError, match="Bare carriage return") as exc_info:
            Contentlines.from_ical(b"DESCRIPTION:a\rb", strict=True)
        assert exc_info.value.offset == 13

    def test_bare_line_feed(self):
        with pytest.raises(ParseError, match="Bare line feed") as exc_info:
            Contentlines.from_ical(b"DESCRIPTION:a\nb", strict=True)
        assert exc_info.value.offset == 13

    def test_lf_fold_is_illegal_in_strict_mode(self):
        with pytest.raises(ParseError, match="Bare line feed"):
            Contentline.from_ical(b"DESCRIPTION:a\n b", strict=True)

    def test_invalid_utf8(self):
        with pytest.raises(ParseError, match="not valid UTF-8") as exc_info:
            Contentline.from_ical(b"DESCRIPTION:bad \xff\xfe bytes", strict=True)
        assert exc_info.value.offset == 16

    def test_invalid_utf8_str_input_still_works(self):
        # str input cannot contain invalid UTF-8, it parses as before
        assert Contentline.from_ical("SUMMARY:ok", strict=True) == "SUMMARY:ok"

    def test_unclosed_quote_in_content_line(self):
        with pytest.raises(ParseError, match="Unclosed quotation mark") as exc_info:
            Contentline('ATTENDEE;CN="Doe, John:mailto:jd@example.com').parts()
        assert exc_info.value.offset == 12

    def test_unclosed_quote_in_parameters(self):
        with pytest.raises(ParseError, match="Unclosed quotation mark") as exc_info:
            Parameters.from_ical('CN="Doe, John;ROLE=CHAIR')
        assert exc_info.value.offset == 3

    def test_unbalanced_quote_in_value_is_tolerated(self):
        # a quotation mark after the value separator is part of the value
        assert Contentline('DESCRIPTION:a "b').parts()[2] == 'a "b'


class TestLenientMode:
    """The default lenient mode keeps its historical tolerance."""

    def test_bare_cr_is_kept(self):
        assert Contentlines.from_ical(b"DESCRIPTION:a\rb") == [
            "DESCRIPTION:a\rb",
            "",
        ]

    def test_lf_fold_is_tolerated(self):
        assert Contentline.from_ical(b"DESCRIPTION:a\n b") == "DESCRIPTION:ab"

    def test_invalid_utf8_is_replaced(self):
        assert (
            Contentline.from_ical(b"DESCRIPTION:bad \xff bytes")
            == "DESCRIPTION:bad \ufffd bytes"
        )

    def test_strict_mode_does_not_expand_lenient_tolerance(self):
        # lenient parsing accepts LF-only calendars; strict parsing does not
        data = b"BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR\n"
        assert Contentlines.from_ical(data) == [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "END:VCALENDAR",
            "",
        ]
        with pytest.raises(ParseError):
            Contentlines.from_ical(data, strict=True)
