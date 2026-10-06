from __future__ import annotations

import functools
import hashlib
import importlib.metadata
import pickle
import re
import sqlite3
import sys
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from gdscript_graph.calls import RawCall, RawConnection, extract_calls_and_connections
from gdscript_graph.lexical import mask_strings
from gdscript_graph.parsing import parse_source
from gdscript_graph.symbols import (
    CallType,
    FileSymbols,
    FunctionSymbol,
    SignalSymbol,
    extract_class_name,
    extract_extends,
    extract_lambda_shadowed_names,
    extract_local_var_types,
    extract_property_accessor_lambda_shadowed_names,
    extract_property_accessor_local_var_types,
    extract_symbols,
    iter_function_defs,
    iter_property_accessor_defs,
)

# (scope, function name) -> local name -> its static type name (or None)
LocalVarTypes = dict[tuple[str | None, str], dict[str, str | CallType | None]]
# (scope, function name) -> names some lambda in the function re-declares
LambdaShadowedNames = dict[tuple[str | None, str], set[str]]

# {res_path: (content hash, packed FileExtract)}
ExtractCache = dict[str, tuple[str, bytes]]

_CACHE_VERSION_META_KEY = "extract_cache_version"


@dataclass
class FileExtract:
    """Everything a build takes from one `.gd` file. A pure function of the
    file's bytes and its res:// path -- nothing from any other file goes in
    (that's resolve.py's job, redone every build) -- so it's cached by
    content hash and only recomputed for a file that changed. Reloading
    this is ~30x faster than reloading a parsed tree (the previous cache)
    and a fraction of its size."""
    symbols: FileSymbols
    error: str | None = None  # a parse error, or why extraction stopped short
    calls: list[RawCall] = field(default_factory=list)
    connections: list[RawConnection] = field(default_factory=list)
    local_var_types: LocalVarTypes = field(default_factory=dict)
    lambda_shadowed_names: LambdaShadowedNames = field(default_factory=dict)

    @property
    def res_path(self) -> str:
        return self.symbols.res_path


def _empty_symbols(res_path: str) -> FileSymbols:
    return FileSymbols(res_path=res_path, class_name=None, extends=None, functions=[], signals=[])


def unreadable_file(res_path: str, error: str) -> FileExtract:
    return FileExtract(_empty_symbols(res_path), error)


def extract_source(res_path: str, source: str) -> FileExtract:
    pr = parse_source(source, res_path)
    if pr.tree is None:
        return FileExtract(
            scan_declarations(source, res_path),
            f"{pr.error}\n(declarations recovered by a line scan; calls in this file are not indexed)",
        )
    tree = pr.tree

    # A pathologically deep expression/call nesting (e.g. 1000+ levels of
    # nested calls) parses fine at the Lark level but can blow Python's
    # recursion limit in our own tree walks -- isolate that to this one
    # file (same as a genuine parse error) rather than letting it abort
    # the whole build and discard every other file's data.
    try:
        symbols = extract_symbols(pr)
    except RecursionError:
        # class_name/extends only scan the tree's direct top-level
        # children (no recursion), so they're still safe to compute here
        # even though the fuller extraction overflowed -- losing them too
        # would silently break inheritance-chain resolution for every
        # OTHER file that extends this one.
        symbols = _empty_symbols(res_path)
        symbols.class_name = extract_class_name(tree)
        symbols.extends = extract_extends(tree)
        return FileExtract(symbols, "too deeply nested to index (exceeded a safe recursion depth)")

    try:
        calls, connections = extract_calls_and_connections(tree)
        local_var_types: LocalVarTypes = {}
        lambda_shadowed_names: LambdaShadowedNames = {}
        for fd in iter_function_defs(tree):
            local_var_types[(fd.scope, fd.name)] = extract_local_var_types(fd.node, res_path)
            lambda_shadowed_names[(fd.scope, fd.name)] = extract_lambda_shadowed_names(fd.node)
        for pa in iter_property_accessor_defs(tree):
            local_var_types[(pa.scope, pa.name)] = extract_property_accessor_local_var_types(pa, res_path)
            lambda_shadowed_names[(pa.scope, pa.name)] = extract_property_accessor_lambda_shadowed_names(pa)
    except RecursionError:
        # Same hazard, reachable independently (a tree can be deep enough
        # to survive the symbol walk but not this one). The symbols stay,
        # but the file's calls are lost -- reported as an error so the
        # file doesn't look fully indexed while its whole call graph is
        # missing.
        return FileExtract(
            symbols, "too deeply nested to extract calls/connections (exceeded a safe recursion depth)"
        )
    return FileExtract(symbols, None, calls, connections, local_var_types, lambda_shadowed_names)


_ANNOTATIONS = r"(?:@\w+(?:\([^)]*\))?\s+)*"
_FUNC_LINE = re.compile(_ANNOTATIONS + r"(static\s+)?func\s+(\w+)")
_SIGNAL_LINE = re.compile(_ANNOTATIONS + r"signal\s+(\w+)")
_CLASS_LINE = re.compile(r"class\s+(\w+)\b")
_CLASS_EXTENDS = re.compile(r"""class\s+\w+\s+extends\s+("[^"]*"|'[^']*'|[\w.]+)""")
_CLASS_NAME_LINE = re.compile(r"class_name\s+(\w+)(?:\s+extends\s+(.+?))?\s*(?:#.*)?$")
_EXTENDS_LINE = re.compile(r"extends\s+(.+?)\s*(?:#.*)?$")


def _extends_value(text: str) -> str:
    text = text.strip().rstrip(":").strip()
    return text.strip("\"'") if text[:1] in "\"'" else text


def scan_declarations(source: str, res_path: str) -> FileSymbols:
    """The file's `class_name`, `extends`, functions and signals (with
    their inner-class scope, from indentation), found by scanning lines --
    for a file the parser rejects outright. Far cruder than a parse, but a
    file the parser can't read would otherwise contribute nothing at all:
    every call *into* it would go unresolved and its functions would look
    like they didn't exist."""
    masked_lines = mask_strings(source).split("\n")
    lines = source.split("\n")
    symbols = FileSymbols(res_path=res_path, class_name=None, extends=None, functions=[], signals=[])
    class_stack: list[tuple[int, str]] = []  # (indent, name) of enclosing inner classes
    for number, (masked, line) in enumerate(zip(masked_lines, lines), start=1):
        stripped = masked.lstrip(" \t")
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(masked) - len(stripped)
        while class_stack and indent <= class_stack[-1][0]:
            class_stack.pop()
        scope = ".".join(name for _, name in class_stack) or None
        original = line[indent:]

        if (match := _FUNC_LINE.match(stripped)) is not None:
            symbols.functions.append(FunctionSymbol(match.group(2), number, match.group(1) is not None, scope))
        elif (match := _SIGNAL_LINE.match(stripped)) is not None:
            symbols.signals.append(SignalSymbol(match.group(1), number, scope))
        elif (match := _CLASS_LINE.match(stripped)) is not None:
            class_stack.append((indent, match.group(1)))
            extends = _CLASS_EXTENDS.match(original)
            inner_scope = ".".join(name for _, name in class_stack)
            symbols.class_extends[inner_scope] = _extends_value(extends.group(1)) if extends else None
        elif scope is not None and (match := _EXTENDS_LINE.match(original)) is not None:
            symbols.class_extends[scope] = _extends_value(match.group(1))  # `extends` in the class body
        elif indent == 0 and (match := _CLASS_NAME_LINE.match(original)) is not None:
            symbols.class_name = symbols.class_name or match.group(1)
            if match.group(2) and symbols.extends is None:
                symbols.extends = _extends_value(match.group(2))
        elif indent == 0 and symbols.extends is None and (match := _EXTENDS_LINE.match(original)) is not None:
            symbols.extends = _extends_value(match.group(1))
    return symbols


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@functools.cache
def extractor_version() -> str:
    """Identifies the code that produced a cached `FileExtract` -- a cache
    written by any other version (a changed extractor, a different
    gdtoolkit grammar) is ignored wholesale, never half-trusted. Derived
    from the extraction modules' own source, so it can't be forgotten."""
    from gdscript_graph import calls, lexical, parsing, receivers, symbols

    digest = hashlib.sha256()
    for module in (parsing, lexical, receivers, symbols, calls, sys.modules[__name__]):
        try:
            digest.update(Path(module.__file__).read_bytes())
        except (OSError, TypeError):
            digest.update(module.__name__.encode())
    try:
        digest.update(importlib.metadata.version("gdtoolkit").encode())
    except importlib.metadata.PackageNotFoundError:
        pass
    return digest.hexdigest()[:16]


def pack(extract: FileExtract) -> bytes:
    return zlib.compress(pickle.dumps(extract, protocol=pickle.HIGHEST_PROTOCOL), 1)


def unpack(blob: bytes) -> FileExtract:
    return pickle.loads(zlib.decompress(blob))


def load_cache(db_path: Path) -> ExtractCache:
    """The previous build's per-file extracts, if `db_path` has a cache
    written by this exact extractor version. Any failure (no db yet, a
    corrupt file, an older format) just means "no cache this build" -- this
    is a pure performance layer over always-correct full extraction, so it
    must never turn into a build failure."""
    if not db_path.exists():
        return {}
    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (_CACHE_VERSION_META_KEY,)).fetchone()
            if row is None or row[0] != extractor_version():
                return {}
            rows = conn.execute("SELECT res_path, content_hash, blob FROM extract_cache").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    return {res_path: (digest, blob) for res_path, digest, blob in rows}


def save_cache(conn: sqlite3.Connection, cache: ExtractCache) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?)", (_CACHE_VERSION_META_KEY, extractor_version())
    )
    conn.executemany(
        "INSERT INTO extract_cache (res_path, content_hash, blob) VALUES (?, ?, ?)",
        [(res_path, digest, blob) for res_path, (digest, blob) in cache.items()],
    )


def extract_all(
    gd_files: list[Path], to_res_path, old_cache: ExtractCache, unchanged: set[str] | frozenset[str] = frozenset()
) -> tuple[list[FileExtract], ExtractCache, int]:
    """Extract every file, reusing `old_cache` for each one whose content
    hash still matches -- or, for a res:// path in `unchanged` (known not to
    have changed since the cache was written), without even reading it.
    Returns (extracts, the cache to save for the next build -- covering
    exactly the current files, so removed ones drop out -- and how many
    files were served from cache)."""
    extracts: list[FileExtract] = []
    new_cache: ExtractCache = {}
    hits = 0
    for file_path in gd_files:
        res_path = to_res_path(file_path)
        cached = old_cache.get(res_path)
        if cached is not None and res_path in unchanged:
            try:
                extract = unpack(cached[1])
            except Exception:
                pass  # fall back to reading and hashing the file below
            else:
                extracts.append(extract)
                new_cache[res_path] = cached
                hits += 1
                continue
        try:
            data = file_path.read_bytes()
        except OSError as exc:
            extracts.append(unreadable_file(res_path, str(exc)))
            continue

        digest = content_hash(data)
        if cached is not None and cached[0] == digest:
            try:
                extract = unpack(cached[1])
            except Exception:
                # A corrupt entry degrades to a fresh extraction below.
                pass
            else:
                extracts.append(extract)
                new_cache[res_path] = cached
                hits += 1
                continue

        try:
            # Newlines normalized as `read_text()`'s universal-newlines mode
            # would -- the grammar only knows "\n".
            source = data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        except UnicodeDecodeError as exc:
            extracts.append(unreadable_file(res_path, str(exc)))
            continue
        extract = extract_source(res_path, source)
        extracts.append(extract)
        new_cache[res_path] = (digest, pack(extract))
    return extracts, new_cache, hits
