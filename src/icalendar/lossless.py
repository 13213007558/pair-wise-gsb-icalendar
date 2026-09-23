"""Lossless iCalendar document editing.

This module implements an optional *lossless document mode* on top of the
regular, lenient parser. Subscribers that byte-hash the original .ics
file can change a single property - say one SUMMARY - without the whole
file being reordered, refolded, or having unknown extensions rewritten.

Parsing keeps every original content line (including its physical folding,
line endings, parameter quoting, and casing). Edits are addressed by a
component path plus the occurrence index among same-named properties, so two
ATTENDEE lines are never mixed up. Only the affected logical lines are
re-serialized; everything else is emitted byte for byte.

Supported edits are replacing, adding, and deleting properties. Moving
components is not supported. Input that cannot be mapped reliably back to
its original bytes (broken structure, undecodable content, lines that only
the lenient parser would accept) is rejected with
:class:`LosslessEditingError`; the regular lenient parse via
Calendar.from_ical remains available for such files.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, TypeAlias

from icalendar.error import InvalidCalendar
from icalendar.parser.content_line import Contentline
from icalendar.parser.parameter import Parameters
from icalendar.parser.string import _foldline, validate_token
from icalendar.parser_tools import DEFAULT_ENCODING, to_unicode
from icalendar.prop import vText

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["LosslessDocument", "LosslessEditingError"]


class LosslessEditingError(InvalidCalendar):
    """The input cannot be edited losslessly.

    Raised when the iCalendar data cannot be mapped reliably back to its
    original bytes - for example unbalanced BEGIN/END blocks, properties
    outside any component, undecodable bytes, or lines that only the lenient
    parser would accept. The regular lenient parse via Calendar.from_ical
    may still be able to read such files.
    """


#: One element of a component path: a component name (first occurrence) or a
#: (name, index) tuple selecting the index-th same-named component.
PathElement: TypeAlias = str | tuple[str, int]

_LINE_BREAK = re.compile(rb"\r\n|\n")
_CONTINUATION_PREFIXES = (b" ", b"\t")


def _split_physical_lines(data: bytes) -> list[tuple[bytes, bytes]]:
    """Split *data* into (content, terminator) pairs, preserving bytes."""
    lines: list[tuple[bytes, bytes]] = []
    pos = 0
    for match in _LINE_BREAK.finditer(data):
        lines.append((data[pos : match.start()], match.group()))
        pos = match.end()
    if pos < len(data):
        lines.append((data[pos:], b""))
    return lines


@dataclass
class _Line:
    """One logical content line and its link to the original physical lines."""

    #: Indices into the document's physical lines, or None for added lines.
    phys: list[int] | None
    #: The current unfolded text of the line.
    text: str
    #: Whether the line was modified and must be re-serialized.
    dirty: bool = False
    #: Whether the line (and all its continuations) was deleted.
    deleted: bool = False
    #: Blank lines are preserved verbatim and never take part in parsing.
    passthrough: bool = False


@dataclass
class _Property:
    """A property occurrence inside a component."""

    name: str  # original casing from the file
    line: _Line

    @property
    def uname(self) -> str:
        return self.name.upper()


@dataclass
class _Component:
    """A component in the document tree."""

    name: str  # upper-cased
    begin: _Line
    parent: _Component | None
    end: _Line | None = None
    children: list[_Component] = field(default_factory=list)
    properties: list[_Property] = field(default_factory=list)


def _serialize_value(value) -> str:
    """Serialize a property value to its content-line text form."""
    if hasattr(value, "to_ical"):
        return to_unicode(value.to_ical())
    return to_unicode(vText(value).to_ical())


class LosslessDocument:
    """An iCalendar document that preserves unedited bytes exactly.

    Create an instance with :meth:`from_ical`, address properties by a
    component *path* (e.g. ("VCALENDAR", "VEVENT") or
    ("VCALENDAR", ("VEVENT", 1))) plus the *occurrence* index among
    same-named properties, and write the result with :meth:`to_ical`.
    A document that was not modified serializes to the exact input bytes.
    """

    def __init__(self, original: bytes) -> None:
        self._original = original
        self._modified = False
        self._phys = _split_physical_lines(original)
        self._lines: list[_Line] = []
        self._roots: list[_Component] = []
        self._parse()

    @classmethod
    def from_ical(cls, st: bytes | str | Path) -> LosslessDocument:
        """Parse iCalendar data into an editable lossless document.

        Parameters:
            st: iCalendar data as bytes or str, or a pathlib.Path to an
                iCalendar file.

        Raises:
            LosslessEditingError: If the input cannot be mapped reliably
                back to its original bytes.

        """
        if isinstance(st, Path):
            st = st.read_bytes()
        if isinstance(st, str):
            st = st.encode(DEFAULT_ENCODING)
        return cls(st)

    def _parse(self) -> None:
        """Group physical lines into logical lines and build the tree."""
        lines: list[_Line] = []
        current: _Line | None = None
        for index, (content, _terminator) in enumerate(self._phys):
            if content[:1] in _CONTINUATION_PREFIXES:
                if current is None:
                    raise LosslessEditingError(
                        "Cannot edit losslessly: continuation line without a "
                        "preceding content line."
                    )
                current.phys.append(index)
            elif content == b"":
                lines.append(_Line(phys=[index], text="", passthrough=True))
                current = None
            else:
                current = _Line(phys=[index], text="")
                lines.append(current)
        for line in lines:
            if line.passthrough:
                continue
            raw = bytearray()
            for position, phys_index in enumerate(line.phys):
                content = self._phys[phys_index][0]
                raw += content if position == 0 else content[1:]
            try:
                line.text = bytes(raw).decode(DEFAULT_ENCODING, errors="strict")
            except UnicodeDecodeError as exc:
                raise LosslessEditingError(
                    "Cannot edit losslessly: line is not valid "
                    f"{DEFAULT_ENCODING}: {exc}"
                ) from exc
        self._lines = lines
        self._build_tree()

    def _build_tree(self) -> None:
        """Validate the logical lines and build the component tree."""
        stack: list[_Component] = []
        for line in self._lines:
            if line.passthrough:
                continue
            try:
                name, params, values = Contentline(line.text, strict=True).raw_parts()
            except ValueError as exc:
                raise LosslessEditingError(f"Cannot edit losslessly: {exc}") from exc
            uname = name.upper()
            if uname == "BEGIN":
                if params:
                    raise LosslessEditingError(
                        "Cannot edit losslessly: BEGIN must not have "
                        f"parameters: {line.text!r}"
                    )
                try:
                    validate_token(values)
                except ValueError as exc:
                    raise LosslessEditingError(
                        "Cannot edit losslessly: invalid component name in "
                        f"{line.text!r}"
                    ) from exc
                component = _Component(
                    name=values.upper(),
                    begin=line,
                    parent=stack[-1] if stack else None,
                )
                if stack:
                    stack[-1].children.append(component)
                else:
                    self._roots.append(component)
                stack.append(component)
            elif uname == "END":
                if params:
                    raise LosslessEditingError(
                        "Cannot edit losslessly: END must not have "
                        f"parameters: {line.text!r}"
                    )
                if not stack:
                    raise LosslessEditingError(
                        "Cannot edit losslessly: END without a matching "
                        f"BEGIN: {line.text!r}"
                    )
                component = stack.pop()
                if values.upper() != component.name:
                    raise LosslessEditingError(
                        f"Cannot edit losslessly: END:{values} does not match "
                        f"BEGIN:{component.name}"
                    )
                component.end = line
            else:
                if not stack:
                    raise LosslessEditingError(
                        "Cannot edit losslessly: property outside of any "
                        f"component: {line.text!r}"
                    )
                stack[-1].properties.append(_Property(name=name, line=line))
        if stack:
            raise LosslessEditingError(
                f"Cannot edit losslessly: unclosed component BEGIN:{stack[-1].name}"
            )

    @staticmethod
    def _normalize_path_element(element: PathElement) -> tuple[str, int]:
        if isinstance(element, str):
            return element.upper(), 0
        name, index = element
        return name.upper(), index

    def _resolve(self, path: Sequence[PathElement]) -> _Component:
        """Resolve a component path to its component node."""
        if not path:
            raise KeyError("The component path must not be empty.")
        candidates = self._roots
        component = None
        for element in path:
            name, index = self._normalize_path_element(element)
            matches = [c for c in candidates if c.name == name]
            if index >= len(matches):
                raise KeyError(
                    f"No component {name!r} (occurrence {index}) in path "
                    f"{tuple(path)!r}."
                )
            component = matches[index]
            candidates = component.children
        return component

    def _find_property(
        self, path: Sequence[PathElement], name: str, occurrence: int
    ) -> tuple[_Component, _Property]:
        component = self._resolve(path)
        uname = name.upper()
        matches = [p for p in component.properties if p.uname == uname]
        if occurrence >= len(matches):
            raise KeyError(
                f"No property {uname!r} (occurrence {occurrence}) in "
                f"component {'/'.join(str(e) for e in path)}."
            )
        return component, matches[occurrence]

    def count(self, path: Sequence[PathElement], name: str) -> int:
        """Return how often property *name* occurs in the addressed component."""
        component = self._resolve(path)
        uname = name.upper()
        return sum(1 for p in component.properties if p.uname == uname)

    def get_value(
        self, path: Sequence[PathElement], name: str, occurrence: int = 0
    ) -> str:
        """Return the unescaped value of the addressed property."""
        _, prop = self._find_property(path, name, occurrence)
        return Contentline(prop.line.text).parts()[2]

    def get_params(
        self, path: Sequence[PathElement], name: str, occurrence: int = 0
    ) -> Parameters:
        """Return the parameters of the addressed property."""
        _, prop = self._find_property(path, name, occurrence)
        return Contentline(prop.line.text).parts()[1]

    def set(
        self,
        path: Sequence[PathElement],
        name: str,
        value,
        occurrence: int = 0,
        params: Parameters | dict | None = None,
    ) -> None:
        """Replace the value (and optionally parameters) of a property.

        Only the addressed occurrence is re-serialized. When params is not
        given, the original name casing and parameter text of the line are
        kept byte for byte and only the value part is rewritten.
        """
        _, prop = self._find_property(path, name, occurrence)
        text = prop.line.text
        if params is None:
            separator = Contentline(text, strict=True).value_separator_index()
            prefix = text[: separator + 1] if separator >= 0 else text + ":"
        else:
            parameters = (
                params if isinstance(params, Parameters) else Parameters(params)
            )
            params_text = to_unicode(parameters.to_ical())
            prefix = f"{prop.name};{params_text}:" if params_text else f"{prop.name}:"
        new_text = prefix + _serialize_value(value)
        if "\n" in new_text:
            raise ValueError("Content line can not contain unescaped new lines.")
        prop.line.text = new_text
        prop.line.dirty = True
        self._modified = True

    def add(
        self,
        path: Sequence[PathElement],
        name: str,
        value,
        params: Parameters | dict | None = None,
    ) -> None:
        """Add a new property at the end of the addressed component."""
        component = self._resolve(path)
        parameters = params if isinstance(params, Parameters) else Parameters(params)
        line = Contentline.from_parts(name.upper(), parameters, value)
        new_line = _Line(phys=None, text=str(line), dirty=True)
        end = component.end
        if end is None:  # pragma: no cover - unclosed components are rejected
            raise LosslessEditingError(f"Component {component.name} is not closed.")
        self._lines.insert(self._lines.index(end), new_line)
        component.properties.append(_Property(name=name.upper(), line=new_line))
        self._modified = True

    def delete(
        self, path: Sequence[PathElement], name: str, occurrence: int = 0
    ) -> None:
        """Delete the addressed property occurrence and all its folded lines."""
        component, prop = self._find_property(path, name, occurrence)
        prop.line.deleted = True
        component.properties.remove(prop)
        self._modified = True

    def to_ical(self) -> bytes:
        """Serialize the document.

        Unmodified lines are emitted with their original bytes - casing,
        parameter quoting, folding, and line endings included. Modified or
        added lines are folded to 75 octets, UTF-8 aware so that no
        multi-byte character is split. A document without edits returns the
        exact input bytes.
        """
        if not self._modified:
            return self._original
        out: list[bytes] = []
        for line in self._lines:
            if line.deleted:
                continue
            if line.phys is not None and not line.dirty:
                for phys_index in line.phys:
                    content, terminator = self._phys[phys_index]
                    out.append(content)
                    out.append(terminator)
            else:
                out.append(_foldline(line.text).encode(DEFAULT_ENCODING) + b"\r\n")
        return b"".join(out)
