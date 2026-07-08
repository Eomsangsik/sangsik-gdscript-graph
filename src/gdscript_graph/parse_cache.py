from __future__ import annotations

import hashlib
import pickle
import sqlite3
from pathlib import Path

from gdscript_graph.parsing import ParseResult, parse_file

# {res_path: (content_hash, pickled_tree_blob)}
ParseCache = dict[str, tuple[str, bytes]]


def load_cache(db_path: Path) -> ParseCache:
    """Load a previous build's cached, already-parsed trees from
    `db_path`'s `parse_cache` table, if any. Any failure (db doesn't exist
    yet, corrupt file, table missing because it predates this feature)
    just means "no cache this build" -- this is a pure performance layer
    on top of an always-correct full-reparse fallback, so it must never
    turn into a hard build failure."""
    if not db_path.exists():
        return {}
    try:
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute("SELECT res_path, content_hash, tree_blob FROM parse_cache").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    return {res_path: (content_hash, tree_blob) for res_path, content_hash, tree_blob in rows}


def parse_all_cached(gd_files: list[Path], to_res_path, old_cache: ParseCache) -> tuple[list[ParseResult], ParseCache, int]:
    """Like `parsing.parse_all`, but reuses a cached tree for any file
    whose content hash still matches `old_cache` instead of re-running the
    (~20-30x more expensive, measured) Lark parser on it.

    This is always safe, unlike caching anything *downstream* of parsing
    (symbol/call extraction, whose output for one file can depend on other
    files' signal declarations): parsing is a pure function of a file's own
    bytes, so an unchanged content hash guarantees a bit-for-bit identical
    tree. Every file's tree -- cached or freshly parsed -- still flows
    through the exact same extraction and resolution code every single
    build; only the parse step itself is ever skipped.

    Returns (parse results, the updated cache to persist for next build --
    covering every currently-discovered file, naturally dropping entries
    for removed files -- and how many files were served from cache)."""
    results: list[ParseResult] = []
    new_cache: ParseCache = {}
    cache_hits = 0
    for file_path in gd_files:
        res_path = to_res_path(file_path)
        try:
            source = file_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as exc:
            results.append(ParseResult(file_path, res_path, None, "", str(exc)))
            continue

        content_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
        cached = old_cache.get(res_path)
        if cached is not None and cached[0] == content_hash:
            try:
                tree = pickle.loads(cached[1])
            except Exception:
                # A corrupt/unreadable cache entry (e.g. written by an
                # incompatible past version) must degrade to a fresh parse,
                # not crash the build over what's purely a speed
                # optimization -- fall through to the cache-miss path below.
                pass
            else:
                results.append(ParseResult(file_path, res_path, tree, source, None))
                new_cache[res_path] = cached
                cache_hits += 1
                continue

        pr = parse_file(file_path, res_path)
        results.append(pr)
        if pr.tree is not None:
            try:
                blob = pickle.dumps(pr.tree, protocol=pickle.HIGHEST_PROTOCOL)
            except RecursionError:
                # A pathologically deep tree (e.g. 1000+ nested calls) can
                # overflow Python's recursion limit in pickle's own
                # (recursive) serializer, same as it can in our tree walks
                # elsewhere -- just skip caching this one file rather than
                # letting a caching optimization crash an otherwise-successful
                # parse. It'll simply be re-parsed fresh every build, same as
                # if no cache existed for it at all.
                continue
            new_cache[res_path] = (content_hash, blob)

    return results, new_cache, cache_hits


def save_cache(conn: sqlite3.Connection, cache: ParseCache) -> None:
    conn.executemany(
        "INSERT INTO parse_cache (res_path, content_hash, tree_blob) VALUES (?, ?, ?)",
        [(res_path, content_hash, tree_blob) for res_path, (content_hash, tree_blob) in cache.items()],
    )
