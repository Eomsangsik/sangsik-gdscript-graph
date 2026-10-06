from __future__ import annotations

import re
from collections.abc import Iterator

# Where a scan outside any string can next change state: a comment or a quote.
_COMMENT_OR_QUOTE = re.compile(r"""[#"']""")
# Inside a string: an escape (which can't close it), or the closing quote.
_STRING_END = {
    quote: re.compile(r"\\.|" + re.escape(quote), re.DOTALL) for quote in ('"""', "'''", '"', "'")
}


def iter_string_literals(source: str) -> Iterator[tuple[int, int, int]]:
    """(start, end, quote length) of each string literal in GDScript
    `source`, quotes included, skipping comments. Stops at an unterminated
    string -- the rest of the file is malformed anyway."""
    pos = 0
    while True:
        match = _COMMENT_OR_QUOTE.search(source, pos)
        if match is None:
            return
        start = match.start()
        char = source[start]
        if char == "#":
            newline = source.find("\n", start)
            if newline == -1:
                return
            pos = newline + 1
            continue
        quote = char * 3 if source.startswith(char * 3, start) else char
        pos = start + len(quote)
        while True:
            end_match = _STRING_END[quote].search(source, pos)
            if end_match is None:
                return
            pos = end_match.end()
            if end_match.group() == quote:
                break
        yield start, pos, len(quote)


def triple_quote_multiline_strings(source: str) -> str:
    """`source` with every "..."/'...' string literal that spans lines
    rewritten as the equivalent triple-quoted one.

    Godot accepts a line break inside an ordinary string; gdtoolkit's
    grammar only inside a triple-quoted one, so it rejected the whole file
    -- a common shape in real code (a long BBCode label, a multi-line
    assert message, `text.split("<newline>")`). Rewriting the quotes keeps
    every line where it was: no line numbers shift, so nothing needs
    mapping back. Only the quote characters change, and code reading
    string values strips quotes either way. A `%"..."` unique-node path
    has no triple-quoted form in the grammar and is left alone."""
    pieces: list[str] = []
    last = 0
    for start, end, quote_len in iter_string_literals(source):
        if quote_len != 1 or "\n" not in source[start:end]:
            continue
        if start > 0 and source[start - 1] == "%":
            continue
        quote = source[start] * 3
        pieces += [source[last:start], quote, source[start + 1:end - 1], quote]
        last = end
    if not pieces:
        return source
    pieces.append(source[last:])
    return "".join(pieces)


def mask_strings(source: str) -> str:
    """`source` with every string literal's contents blanked to spaces
    (line breaks kept), so a line scan can't mistake text inside one for a
    declaration."""
    chars = list(source)
    for start, end, _ in iter_string_literals(source):
        for i in range(start, end):
            if chars[i] != "\n":
                chars[i] = " "
    return "".join(chars)
