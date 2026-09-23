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
UFOLD_BYTES = re.compile(rb"(?:(?<!\n)\r\n|(?<![\r\n])\n)(?:\r?\n)*[ \t]")
NEWLINE = re.compile(r"\r?\n")

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


def _validate_line_breaks(data: str | bytes) -> None:
    """Raise a ParseError for line breaks that are not CRLF.

    :rfc:`5545#section-3.1` delimits content lines with CRLF, so a bare
    carriage return or a bare line feed is invalid in strict parsing.
    The reported offset is a byte offset for ``bytes`` input and a
    character offset for ``str`` input.
    """
    cr = b"\r" if isinstance(data, bytes) else "\r"
    lf = b"\n" if isinstance(data, bytes) else "\n"
    for i in range(len(data)):
        char = data[i : i + 1]
        if char == cr and data[i + 1 : i + 2] != lf:
            raise ParseError("Bare carriage return in content line", offset=i)
        if char == lf and data[i - 1 : i] != cr:
            raise ParseError("Bare line feed in content line", offset=i)


def _unfold_and_decode(data: str | bytes, strict: bool = False) -> str:
    """Unfold content lines and return them as text.

    Folding is defined on the octet stream (:rfc:`5545#section-3.1`), so
    for ``bytes`` input the fold markers (CRLF followed by a space or
    tab) are removed before decoding. A fold point can therefore never
    split a multi-byte UTF-8 character into orphaned continuation bytes,
    and the unfolded value is preserved exactly.

    In strict mode, line breaks that are not CRLF and invalid UTF-8 raise
    a :class:`~icalendar.error.ParseError` with the offset of the
    problem. In lenient mode, the historical tolerance is kept: bare line
    feeds unfold as before and undecodable bytes are replaced.
    """
    if isinstance(data, bytes):
        if strict:
            _validate_line_breaks(data)
        unfolded = UFOLD_BYTES.sub(b"", data)
        try:
            return unfolded.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            if strict:
                raise ParseError(
                    "Content line is not valid UTF-8", offset=exc.start
                ) from exc
            return unfolded.decode("utf-8-sig", "replace")
    data = to_unicode(data)
    if strict:
        _validate_line_breaks(data)
    return UFOLD.sub("", data)


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
            quote_offset: int | None = None

            for i, ch in enumerate(self):
                if ch == '"' and not escaped:
                    in_quotes = not in_quotes
                    if in_quotes:
                        quote_offset = i
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

            if in_quotes and value_split is None:
                # A quotation mark in the name or parameter section was
                # never closed, so it swallowed the value separator.
                # An unbalanced quotation mark after the value separator
                # is part of the value and left untouched.
                raise ParseError(  # noqa: TRY301
                    "Unclosed quotation mark in content line", offset=quote_offset
                )

            # Validate parsing results
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

        Folding is removed on the raw bytes before decoding, so multi-byte
        UTF-8 characters are never split at a fold point. In strict mode,
        illegal line breaks and invalid UTF-8 raise
        :class:`~icalendar.error.ParseError` with an offset.
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
        """Parses a string into content lines."""
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
            raise
        except Exception as e:
            raise ValueError("Expected StringType with content lines") from e
        return lines


__all__ = ["Contentline", "Contentlines"]
