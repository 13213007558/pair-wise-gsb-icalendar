"""parsing and generation of content lines"""

import re

from icalendar.error import ParseError
from icalendar.parser.parameter import Parameters
from icalendar.parser.property import unescape_backslash, unescape_list_or_string
from icalendar.parser.string import (
    _escape_string,
    _foldline,
    _unescape_string,
    validate_token,
)
from icalendar.parser_tools import DEFAULT_ENCODING, ICAL_TYPE, to_unicode

# Equivalent to the natural ``(\r?\n)+[ \t]`` but without its quadratic cost:
# the greedy run is re-tried at every line break of a long unfoldable block, so
# a megabyte of bare newlines takes seconds. The leading lookbehinds pin a match
# to the first line break of a run, making a failed attempt O(1) instead of a
# full rescan, while matching exactly the same strings.
UFOLD = re.compile(r"(?:(?<!\n)\r\n|(?<![\r\n])\n)(?:\r?\n)*[ \t]")
NEWLINE = re.compile(r"\r?\n")

# Byte-level equivalent of UFOLD: folding is defined on the encoded octet
# stream, so bytes are unfolded before they are decoded.
UFOLD_BYTES = re.compile(rb"(?:(?<!\n)\r\n|(?<![\r\n])\n)(?:\r?\n)*[ \t]")

# In strict mode only a CRLF followed by a single space or tab is a fold.
FOLD_STRICT = re.compile(r"\r\n[ \t]")
FOLD_STRICT_BYTES = re.compile(rb"\r\n[ \t]")

# A CR must be followed by an LF and an LF must be preceded by a CR.
INVALID_LINE_BREAK = re.compile(r"\r(?!\n)|(?<!\r)\n")
INVALID_LINE_BREAK_BYTES = re.compile(rb"\r(?!\n)|(?<!\r)\n")


def _check_line_breaks(data: str | bytes) -> None:
    """Validate that every line break in ``data`` is a CRLF.

    Raises:
        ParseError: If a bare CR or a bare LF is found. The ``offset``
            attribute carries the position of the offending byte or
            character.
    """
    if isinstance(data, str):
        pattern = INVALID_LINE_BREAK
        cr = "\r"
    else:
        pattern = INVALID_LINE_BREAK_BYTES
        cr = 0x0D
    match = pattern.search(data)
    if match is None:
        return
    if data[match.start()] == cr:
        raise ParseError(
            "Invalid line break: CR must be followed by LF",
            offset=match.start(),
        )
    raise ParseError(
        "Invalid line break: LF must be preceded by CR",
        offset=match.start(),
    )


def _unfold(data: str | bytes, strict: bool = False) -> str | bytes:
    """Unfold content lines as defined in :rfc:`5545#section-3.1`.

    A fold is a CRLF immediately followed by a single space or tab.
    Unfolding ``bytes`` before decoding them keeps multi-byte UTF-8
    characters that straddle a fold point intact.

    In lenient mode (``strict=False``) lone LF line breaks and runs of
    empty folded lines are tolerated, as before. In strict mode every
    line break must be a CRLF and only CRLF + WSP is unfolded.
    """
    if isinstance(data, bytes):
        if strict:
            _check_line_breaks(data)
            return FOLD_STRICT_BYTES.sub(b"", data)
        return UFOLD_BYTES.sub(b"", data)
    if strict:
        _check_line_breaks(data)
        return FOLD_STRICT.sub("", data)
    return UFOLD.sub("", data)


def _original_offset(data: bytes, unfolded_offset: int) -> int:
    """Map an offset in unfolded bytes back to its offset in ``data``."""
    removed = 0
    for match in FOLD_STRICT_BYTES.finditer(data):
        if match.start() - removed > unfolded_offset:
            break
        removed += match.end() - match.start()
    return unfolded_offset + removed


def _unfold_and_decode(data: str | bytes, strict: bool = False) -> str:
    """Unfold ``data`` and return it as ``str``.

    Bytes are unfolded before they are decoded so that a fold point
    inside a multi-byte UTF-8 character does not corrupt it. In lenient
    mode invalid UTF-8 is replaced as before; in strict mode it raises.

    Raises:
        ParseError: In strict mode, if a line break is not a CRLF or the
            bytes are not valid UTF-8.
    """
    if isinstance(data, bytes):
        unfolded = _unfold(data, strict=strict)
        if strict:
            try:
                return unfolded.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise ParseError(
                    f"Invalid UTF-8: {exc.reason}",
                    offset=_original_offset(data, exc.start),
                ) from exc
        return unfolded.decode("utf-8-sig", "replace")
    return _unfold(data, strict=strict)


OWS = " \t"
# ``[ \t]*([;=])[ \t]*`` in one pass rescans a long whitespace run at every
# position. Splitting it into two anchored passes (leading then trailing) keeps
# the result identical but removes the quadratic blow-up.
OWS_BEFORE_DELIMITER_RE = re.compile(r"(?<![ \t])[ \t]+([;=])")
OWS_AFTER_DELIMITER_RE = re.compile(r"([;=])[ \t]+")


def _strip_ows_around_delimiters(st: str, delimiters: str = ";=") -> str:
    """Strip optional whitespace around delimiters outside of quoted sections,
    respecting backslash escapes so that escaped delimiters are not treated as
    separators.

    This is a lenient parsing helper (used when strict=False) to support
    iCalendar content lines that contain extra whitespace around tokens.
    """
    if not st:
        return st

    # Fast path for the common case in non-strict mode:
    # no whitespace in the parameter section means there is nothing to normalize.
    if " " not in st and "\t" not in st:
        return st

    # Fast regex-based path for simple parameter sections without quoting/escaping.
    if delimiters == ";=" and '"' not in st and "\\" not in st:
        st = OWS_BEFORE_DELIMITER_RE.sub(r"\1", st)
        return OWS_AFTER_DELIMITER_RE.sub(r"\1", st).strip()

    out: list[str] = []
    pending_ws: list[str] = []
    in_quotes = False
    escaped = False
    # True only if the last appended char was a raw delimiter.
    last_was_delimiter = False

    def flush_pending() -> None:
        nonlocal pending_ws
        if not pending_ws:
            return
        if not last_was_delimiter:
            out.extend(pending_ws)
        pending_ws.clear()

    for ch in st:
        # Handle escaped character (the backslash set escaped in previous iteration)
        if escaped:
            flush_pending()
            out.append(ch)
            escaped = False
            last_was_delimiter = False
            continue

        # Handle backslash to escape next character
        if ch == "\\" and not in_quotes:
            flush_pending()
            out.append(ch)
            escaped = True
            last_was_delimiter = False
            continue

        # Handle quote toggling
        if ch == '"' and not escaped:
            in_quotes = not in_quotes
            flush_pending()
            out.append(ch)
            last_was_delimiter = False
            continue

        # Whitespace outside quotes is buffered
        if not in_quotes and not escaped and ch in OWS:
            pending_ws.append(ch)
            continue

        # Raw delimiter (unescaped and outside quotes)
        if not in_quotes and not escaped and ch in delimiters:
            pending_ws.clear()
            while out and out[-1] in OWS:
                out.pop()
            out.append(ch)
            last_was_delimiter = True
            continue

        # Regular character
        flush_pending()
        out.append(ch)
        last_was_delimiter = False

    if pending_ws and not last_was_delimiter:
        out.extend(pending_ws)

    return "".join(out).strip()


class Contentline(str):
    r"""A content line is a string that can be folded and parsed into parts.

    Raises:
        ValueError: If the value contains ``\n``.

    """

    __slots__ = ("strict",)

    def __new__(cls, value, strict=False, encoding=DEFAULT_ENCODING):
        value = to_unicode(value, encoding=encoding)
        if "\n" in value:
            raise ValueError(
                "Content line can not contain unescaped new line characters."
            )
        self = super().__new__(cls, value)
        self.strict = strict
        return self

    @classmethod
    def from_parts(
        cls,
        name: ICAL_TYPE,
        params: Parameters,
        values,
        sorted: bool = True,  # noqa: A002
    ):
        r"""Turn a parts into a content line.

        Raises:
            TypeError: If ``params`` is not type ``Parameters``.

        """
        if not isinstance(params, Parameters):
            raise TypeError(f"Expected Parameters, got {type(params).__name__}")
        if hasattr(values, "to_ical"):
            values = values.to_ical()
        else:
            from icalendar.prop import vText

            values = vText(values).to_ical()
        # elif isinstance(values, basestring):
        #    values = escape_char(values)

        # TODO: after unicode only, remove this
        # Convert back to unicode, after to_ical encoded it.
        name = to_unicode(name)
        values = to_unicode(values)
        if params:
            params = to_unicode(params.to_ical(sorted=sorted))
            if params:
                # some parameter values can be skipped during serialization
                return cls(f"{name};{params}:{values}")
        return cls(f"{name}:{values}")

    def raw_parts(self) -> tuple[str, Parameters, str]:
        """Split the line into ``name``, ``parameters``, and raw ``values`` parts.

        This is :meth:`parts` without the unescaping: the values are returned
        verbatim, preserving both backslash sequences and URL encoding. It is
        used for :rfc:`7265` ``UNKNOWN`` values, whose real value type—and
        therefore whose escaping rules—are not known.

        See :meth:`parts` for the parts themselves and for examples.
        """
        try:
            name_split: int | None = None
            value_split: int | None = None
            in_quotes: bool = False
            escaped: bool = False
            quote_start: int | None = None

            for i, ch in enumerate(self):
                if ch == '"' and not escaped:
                    in_quotes = not in_quotes
                    quote_start = i if in_quotes else None
                elif ch == "\\" and not in_quotes:
                    escaped = True
                    continue
                elif not in_quotes and not escaped:
                    # Find first delimiter for name
                    if ch in ":;" and name_split is None:
                        name_split = i
                    # Find value delimiter (first colon)
                    if ch == ":" and value_split is None:
                        value_split = i

                escaped = False

            # Validate parsing results
            if in_quotes and value_split is None:
                # A quote opened in the parameter section was never
                # closed, so the value separator could not be found.
                raise ParseError(  # noqa: TRY301
                    "Unclosed quoted string in content line parameters",
                    offset=quote_start,
                )
            if not value_split:
                # No colon found - value is empty, use end of string
                value_split = len(self)

            # Extract name - if no delimiter,
            #   take whole string for validate_token to reject
            name = self[:name_split] if name_split else self
            if not self.strict:
                name = re.sub(r"[ \t]+", "", name.strip())
            validate_token(name)

            if not name_split or name_split + 1 == value_split:
                # No delimiter or empty parameter section
                raise ValueError("Invalid content line")  # noqa: TRY301
            # Parse parameters - they still need to be escaped/unescaped
            # for proper handling of commas, semicolons, etc. in parameter values
            raw_param_str = self[name_split + 1 : value_split]
            if not self.strict:
                raw_param_str = _strip_ows_around_delimiters(raw_param_str)
            param_str = _escape_string(raw_param_str)
            params = Parameters.from_ical(param_str, strict=self.strict)
            params = Parameters(
                (_unescape_string(key), unescape_list_or_string(value))
                for key, value in iter(params.items())
            )
            values = self[value_split + 1 :]
        except ParseError:
            # Keep the precise offset instead of wrapping it.
            raise
        except ValueError as exc:
            raise ValueError(
                f"Content line could not be parsed into parts: '{self}': {exc}"
            ) from exc
        return (name, params, values)

    def parts(self) -> tuple[str, Parameters, str]:
        """Split the line into ``name``, ``parameters``, and unescaped ``values`` parts.

        Properly handles escaping with backslashes and double-quote sections
        to avoid corrupting URL-encoded characters in values.

        The backslash sequences in the values are unescaped, as for the values
        of ``TEXT`` properties, while URL encoding is preserved. Use
        :meth:`raw_parts` to get the values verbatim instead.

        Examples:

            With parameter:

            ..  code-block:: ics

                DESCRIPTION;ALTREP="cid:part1.0001@example.org":The Fall'98 Wild

            Without parameters:

            ..  code-block:: ics

                DESCRIPTION:The Fall'98 Wild
        """
        name, params, values = self.raw_parts()
        return (name, params, unescape_backslash(values))

    def value_separator_index(self) -> int:
        r"""Return the index of the colon that separates the value.

        This is the first colon that is not inside a quoted parameter section.
        A colon inside a quoted parameter value (for example
        ``ALTREP="http://x"``) is skipped, and a colon that belongs to the
        value (``TEXT`` does not escape ``:``) is not mistaken for the
        separator. Backslash has no special meaning in the parameter grammar
        (:rfc:`5545#section-3.1`), so it is treated as an ordinary character.

        Returns:
            An integer representing the index position of the separator,
            or ``-1`` if there is none.
        """
        in_quotes = False
        for i, ch in enumerate(self):
            if ch == '"':
                in_quotes = not in_quotes
            elif ch == ":" and not in_quotes:
                return i
        return -1

    @classmethod
    def from_ical(cls, ical, strict=False):
        """Unfold the content lines in an iCalendar into long content lines.

        Bytes are unfolded before they are decoded, so a fold point inside
        a multi-byte UTF-8 character does not corrupt the character.

        Raises:
            ParseError: In strict mode, if a line break is not a CRLF or
                the bytes are not valid UTF-8.
        """
        return cls(_unfold_and_decode(ical, strict=strict), strict=strict)

    def to_ical(self):
        """Long content lines are folded so they are less than 75 characters
        wide.
        """
        return _foldline(self).encode(DEFAULT_ENCODING)


class Contentlines(list[Contentline]):
    """I assume that iCalendar files generally are a few kilobytes in size.
    Then this should be efficient. for Huge files, an iterator should probably
    be used instead.
    """

    def to_ical(self):
        """Simply join self."""
        return b"\r\n".join(line.to_ical() for line in self if line) + b"\r\n"

    @classmethod
    def from_ical(cls, st, strict=False):
        """Parses a string into content lines.

        Bytes are unfolded before they are decoded, so a fold point inside
        a multi-byte UTF-8 character does not corrupt the character.

        Raises:
            ParseError: In strict mode, if a line break is not a CRLF or
                the bytes are not valid UTF-8.
        """
        try:
            # a fold is carriage return followed by either a space or a tab
            unfolded = _unfold_and_decode(st, strict=strict)
            lines = cls(
                Contentline(line, strict=strict)
                for line in NEWLINE.split(unfolded)
                if line
            )
            lines.append("")  # '\r\n' at the end of every content line
        except ParseError:
            # Keep the precise offset instead of wrapping it.
            raise
        except Exception as e:
            raise ValueError("Expected StringType with content lines") from e
        return lines


__all__ = ["Contentline", "Contentlines"]
