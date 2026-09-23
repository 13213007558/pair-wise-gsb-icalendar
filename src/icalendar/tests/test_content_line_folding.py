"""Tests for byte-level content line unfolding and parameter token parsing.

Folding is defined on the encoded octet stream (:rfc:`5545#section-3.1`), so
a fold point may fall in the middle of a multi-byte UTF-8 character. The
parser must unfold bytes before decoding them and must not mistake
separators inside quoted parameter values for token boundaries.
"""

import pytest

from icalendar import Calendar, Parameters
from icalendar.error import ParseError
from icalendar.parser import Contentline, Contentlines
from icalendar.prop import vText


def _fold_bytes(data: bytes, *fold_points: int) -> bytes:
    """Insert CRLF + space folds into ``data`` at the given byte offsets."""
    for fold_at in sorted(fold_points, reverse=True):
        data = data[:fold_at] + b"\r\n " + data[fold_at:]
    return data


def _unfolded(data: bytes) -> bytes:
    """The content lines of ``data`` with all folds removed, as bytes."""
    return b"\r\n".join(
        line.encode("utf-8") for line in Contentlines.from_ical(data) if line
    )


class TestFoldingInsideMultiByteCharacters:
    """A fold point may split a multi-byte UTF-8 character in two.

    Unfolding must rejoin the bytes instead of corrupting the character.
    """

    @pytest.mark.parametrize("split_at", [1, 2])
    def test_fold_inside_cjk_character(self, split_at):
        char = "汉".encode()
        raw = b"DESCRIPTION:prefix" + _fold_bytes(char, split_at) + b"suffix"
        assert Contentline.from_ical(raw) == "DESCRIPTION:prefix汉suffix"

    @pytest.mark.parametrize("split_at", [1, 2, 3])
    def test_fold_inside_four_byte_emoji(self, split_at):
        char = "😀".encode()
        raw = b"DESCRIPTION:prefix" + _fold_bytes(char, split_at) + b"suffix"
        assert Contentline.from_ical(raw) == "DESCRIPTION:prefix😀suffix"

    def test_fold_inside_character_in_calendar(self):
        description = "描述一下 😀 吧"
        encoded = description.encode("utf-8")
        # fold inside the multi-byte character that starts at byte 5
        fold_at = len(b"DESCRIPTION:") + 6
        ics = (
            b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
            + _fold_bytes(b"DESCRIPTION:" + encoded, fold_at)
            + b"\r\nEND:VCALENDAR\r\n"
        )
        calendar = Calendar.from_ical(ics)
        assert calendar["DESCRIPTION"] == description


class TestFoldPointsAroundEscapesAndQuotes:
    """Folds next to escape sequences and inside quoted strings unfold
    back to the original value."""

    @pytest.mark.parametrize("offset", [0, 1])
    def test_fold_around_escaped_semicolon(self, offset):
        # fold between the backslash and the semicolon or right after it
        fold_at = len(b"DESCRIPTION:a" + b"\\") + offset
        raw = _fold_bytes(b"DESCRIPTION:a\\;b", fold_at)
        assert Contentline.from_ical(raw).parts() == (
            "DESCRIPTION",
            Parameters(),
            "a;b",
        )

    def test_fold_inside_quoted_parameter_value(self):
        raw = _fold_bytes(
            b'ATTENDEE;CN="Doe, John; CEO";ROLE=REQ-PARTICIPANT:mailto:a@b.c',
            20,
        )
        name, params, value = Contentline.from_ical(raw).parts()
        assert name == "ATTENDEE"
        assert params["CN"] == "Doe, John; CEO"
        assert params["ROLE"] == "REQ-PARTICIPANT"
        assert value == "mailto:a@b.c"


class TestRoundTrip:
    """parse/to_ical round-trips preserve the content byte for byte."""

    @pytest.mark.parametrize(
        "value",
        [
            "汉" * 40,  # folds land inside CJK characters
            "😀" * 20,  # folds land inside four-byte emoji
            "a;b," * 30,  # escaped semicolons and commas
        ],
    )
    def test_description_round_trip(self, value):
        ics = (
            b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nDESCRIPTION:"
            + vText(value).to_ical()
            + b"\r\nEND:VCALENDAR\r\n"
        )
        calendar = Calendar.from_ical(ics)
        assert calendar["DESCRIPTION"] == value
        # the serialized output unfolds back to exactly the same bytes
        assert _unfolded(calendar.to_ical()) == _unfolded(ics)

    def test_quoted_parameter_round_trip(self):
        ics = (
            b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
            b'ATTENDEE;CN="Doe, John; CEO":mailto:a@b.c\r\n'
            b"END:VCALENDAR\r\n"
        )
        calendar = Calendar.from_ical(ics)
        assert calendar["ATTENDEE"].params["CN"] == "Doe, John; CEO"
        assert _unfolded(calendar.to_ical()) == _unfolded(ics)

    def test_to_ical_does_not_split_multi_byte_characters(self):
        line = Contentline("DESCRIPTION:" + "汉😀" * 40)
        # unfolding the serialized line restores it exactly
        assert Contentline.from_ical(line.to_ical()) == line


class TestShortAsciiLinesUnchanged:
    def test_short_line_passes_through(self):
        assert Contentlines.from_ical("A short line\r\n") == ["A short line", ""]

    def test_short_line_to_ical(self):
        assert Contentline("A short line").to_ical() == b"A short line"

    def test_folded_ascii_line(self):
        assert Contentlines.from_ical("A faked\r\n  long line\r\n") == [
            "A faked long line",
            "",
        ]


class TestParseErrorOffsets:
    """Illegal input raises a ParseError pointing at the problem."""

    def test_bare_cr_bytes(self):
        with pytest.raises(ParseError) as exc_info:
            Contentlines.from_ical(b"A:1\rB:2", strict=True)
        assert exc_info.value.offset == 3

    def test_bare_cr_str(self):
        with pytest.raises(ParseError) as exc_info:
            Contentlines.from_ical("A:1\rB:2", strict=True)
        assert exc_info.value.offset == 3

    def test_lone_lf_is_an_invalid_fold(self):
        with pytest.raises(ParseError) as exc_info:
            Contentlines.from_ical(b"A:1\n B:2", strict=True)
        assert exc_info.value.offset == 3

    def test_invalid_utf_8(self):
        with pytest.raises(ParseError) as exc_info:
            Contentline.from_ical(b"DESCRIPTION:\xff", strict=True)
        assert exc_info.value.offset == 12

    def test_invalid_utf_8_offset_accounts_for_folds(self):
        raw = b"AB\r\n CD\xff"
        with pytest.raises(ParseError) as exc_info:
            Contentline.from_ical(raw, strict=True)
        assert exc_info.value.offset == raw.index(0xFF)

    def test_unclosed_quote(self):
        with pytest.raises(ParseError) as exc_info:
            Contentline('ATTENDEE;CN="Doe, John:mailto:a@b.c').parts()
        assert exc_info.value.offset == 12

    def test_parse_error_is_a_value_error(self):
        with pytest.raises(ValueError):
            Contentlines.from_ical(b"A:1\rB:2", strict=True)


class TestLenientModeToleranceUnchanged:
    """The default lenient mode keeps its established tolerance."""

    def test_bare_cr_is_kept(self):
        assert Contentlines.from_ical(b"A:1\rB:2") == ["A:1\rB:2", ""]

    def test_invalid_utf_8_is_replaced(self):
        assert Contentline.from_ical(b"DESCRIPTION:\xff") == "DESCRIPTION:\ufffd"

    def test_lone_lf_fold_is_accepted(self):
        assert Contentline.from_ical("A:1\n 2") == "A:12"

    def test_empty_folded_lines_collapse(self):
        assert Contentline.from_ical("A:b\r\n\r\n c") == "A:bc"


class TestParameterTokens:
    """Quoted and unquoted parameter tokens follow distinct rules."""

    def test_quoted_value_keeps_separators(self):
        params = Contentline('X;CN="Doe, John; CEO":v').parts()[1]
        assert params["CN"] == "Doe, John; CEO"

    def test_parameter_order_is_preserved(self):
        params = Contentline('X;B=2;A="1;1";C=3:v').parts()[1]
        assert list(params.keys()) == ["B", "A", "C"]

    def test_repeated_parameter_last_one_wins(self):
        params = Contentline('X;CN="a";CN="b":v').parts()[1]
        assert params["CN"] == "b"

    def test_unquoted_comma_separates_values(self):
        params = Contentline("X;CN=Doe,John:v").parts()[1]
        assert params["CN"] == ["Doe", "John"]

    def test_extra_quotes_are_rejected(self):
        with pytest.raises(ValueError):
            Contentline('X;CN="a""b":v').parts()

    def test_stray_quote_is_rejected(self):
        with pytest.raises(ValueError):
            Contentline('X;CN=a"b:v').parts()

    def test_value_escaping_is_separate_from_parameter_quoting(self):
        name, params, value = Contentline(r'X;P="a;b":x\;y\,z').parts()
        assert name == "X"
        assert params["P"] == "a;b"
        assert value == "x;y,z"
