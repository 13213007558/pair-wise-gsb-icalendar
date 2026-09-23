Add an optional lossless document mode, :class:`icalendar.lossless.LosslessDocument`.
It parses iCalendar files while keeping the original content lines, so a single
property can be replaced, added, or deleted - addressed by component path and
the occurrence index among same-named properties - and only the affected lines
are re-serialized (folded UTF-8-aware to 75 octets). Unmodified output is
byte-identical to the input; input that cannot be mapped reliably back to its
original bytes is rejected with :class:`icalendar.lossless.LosslessEditingError`
while the regular lenient parse stays available.
