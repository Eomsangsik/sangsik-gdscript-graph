from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from gdscript_graph.calls import extract_calls_and_connections
from gdscript_graph.discovery import discover
from gdscript_graph.parsing import parse_all
from gdscript_graph.resolve import (
    build_class_name_table,
    build_function_index,
    build_inheritance_map,
    build_signal_index,
    resolve_calls,
    resolve_signal_connections,
)
from gdscript_graph.symbols import (
    FileSymbols,
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

SCHEMA = """
CREATE TABLE files (
    res_path TEXT PRIMARY KEY,
    class_name TEXT,
    extends TEXT,
    parse_error TEXT
);

CREATE TABLE symbols (
    id INTEGER PRIMARY KEY,
    res_path TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,       -- 'function' | 'signal' | 'var' | 'const' | 'enum'
    scope TEXT,               -- enclosing inner class path (dotted); NULL = top-level
    line INTEGER NOT NULL,
    is_static INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (res_path) REFERENCES files(res_path)
);
CREATE INDEX idx_symbols_name ON symbols(name);
CREATE INDEX idx_symbols_res_path ON symbols(res_path);

CREATE TABLE calls (
    id INTEGER PRIMARY KEY,
    source_symbol_id INTEGER NOT NULL,
    target_symbol_id INTEGER NOT NULL,
    line INTEGER NOT NULL,
    FOREIGN KEY (source_symbol_id) REFERENCES symbols(id),
    FOREIGN KEY (target_symbol_id) REFERENCES symbols(id)
);
CREATE INDEX idx_calls_source ON calls(source_symbol_id);
CREATE INDEX idx_calls_target ON calls(target_symbol_id);

CREATE TABLE unresolved_calls (
    id INTEGER PRIMARY KEY,
    source_res_path TEXT NOT NULL,
    source_scope TEXT,
    source_function TEXT NOT NULL,
    receiver TEXT,
    called_name TEXT NOT NULL,
    line INTEGER NOT NULL,
    reason TEXT NOT NULL
);

CREATE TABLE signal_connections (
    id INTEGER PRIMARY KEY,
    source_symbol_id INTEGER NOT NULL,   -- function containing the .connect() call
    signal_symbol_id INTEGER NOT NULL,
    handler_symbol_id INTEGER NOT NULL,
    line INTEGER NOT NULL,
    FOREIGN KEY (source_symbol_id) REFERENCES symbols(id),
    FOREIGN KEY (signal_symbol_id) REFERENCES symbols(id),
    FOREIGN KEY (handler_symbol_id) REFERENCES symbols(id)
);
CREATE INDEX idx_signal_connections_signal ON signal_connections(signal_symbol_id);
CREATE INDEX idx_signal_connections_handler ON signal_connections(handler_symbol_id);

CREATE TABLE unresolved_connections (
    id INTEGER PRIMARY KEY,
    source_res_path TEXT NOT NULL,
    source_function TEXT NOT NULL,
    signal_receiver TEXT,       -- NULL for a bare/self/inherited signal reference; otherwise
                                 -- the base identifier of a `<signal_receiver>.<signal_name>.connect(...)` chain
    signal_name TEXT NOT NULL,
    handler_receiver TEXT,
    handler_name TEXT,          -- NULL when the handler argument shape couldn't be parsed (e.g. a lambda)
    line INTEGER NOT NULL,
    reason TEXT NOT NULL
);
"""


@dataclass
class BuildStats:
    file_count: int
    parse_error_count: int
    function_count: int
    signal_count: int
    field_count: int
    enum_count: int
    resolved_call_count: int
    unresolved_call_count: int
    resolved_connection_count: int
    unresolved_connection_count: int
    # class_name -> the res:// paths that declare it, only entries with 2+
    # declarers -- a duplicate class_name is silently resolved by "later
    # file wins" (see build_class_name_table) with no other signal that an
    # ambiguity existed, which is usually a real authoring mistake worth
    # surfacing rather than silently picking a winner.
    duplicate_class_names: dict[str, list[str]]


def build_database(project_root: Path, db_path: Path) -> BuildStats:
    if db_path.exists() and db_path.is_dir():
        raise IsADirectoryError(f"-o path is a directory, not a file: {db_path}")

    # Build into a temp file and atomically swap it into place at the end,
    # so a failure partway through a rebuild never destroys a previously
    # working database.
    tmp_path = db_path.with_name(f"{db_path.name}.tmp-{os.getpid()}")
    tmp_path.unlink(missing_ok=True)

    conn = sqlite3.connect(tmp_path)
    try:
        stats = _populate(conn, project_root)
        conn.commit()
    except Exception:
        conn.close()
        tmp_path.unlink(missing_ok=True)
        raise
    else:
        conn.close()

    try:
        os.replace(tmp_path, db_path)
    except Exception:
        # e.g. disk full, or a permissions/cross-filesystem issue on the
        # final rename -- the populate step above already succeeded and
        # cleans up after itself on failure, but this rename can fail on
        # its own and previously leaked tmp_path forever on every such
        # failure, silently accumulating stray files across repeated
        # failed builds.
        tmp_path.unlink(missing_ok=True)
        raise
    return stats


def _populate(conn: sqlite3.Connection, project_root: Path) -> BuildStats:
    conn.executescript(SCHEMA)

    project = discover(project_root)
    parse_results = parse_all(project.gd_files, project.to_res_path)

    # A pathologically deep expression/call nesting (e.g. 1000+ levels of
    # nested calls) parses fine at the Lark level but can blow Python's
    # recursion limit in our own tree walks -- isolate that to this one
    # file (same as a genuine parse error) rather than letting it abort
    # the whole build and discard every other file's data.
    all_symbols: list[FileSymbols] = []
    for pr in parse_results:
        try:
            all_symbols.append(extract_symbols(pr))
        except RecursionError:
            pr.error = pr.error or "too deeply nested to index (exceeded a safe recursion depth)"
            # class_name/extends only scan the tree's direct top-level
            # children (no recursion), so they're still safe to compute
            # here even though the fuller extraction overflowed -- losing
            # them too would silently break inheritance-chain resolution
            # for every OTHER file that extends this one.
            all_symbols.append(FileSymbols(
                res_path=pr.res_path,
                class_name=extract_class_name(pr.tree),
                extends=extract_extends(pr.tree),
                functions=[], signals=[],
            ))

    class_name_table = build_class_name_table(all_symbols)
    function_index = build_function_index(all_symbols)
    signal_index = build_signal_index(all_symbols)
    inheritance_map = build_inheritance_map(all_symbols, class_name_table)

    class_name_declarers: dict[str, list[str]] = {}
    for fs in all_symbols:
        if fs.class_name:
            class_name_declarers.setdefault(fs.class_name, []).append(fs.res_path)
    duplicate_class_names = {name: paths for name, paths in class_name_declarers.items() if len(paths) > 1}

    # res_path -> its own top-level signal names, used below to let a bare
    # `<signal>.connect(...)` reach a signal inherited from an ancestor file
    # (inner-class inheritance isn't tracked, same limitation as elsewhere).
    top_level_signals_by_path: dict[str, set[str]] = {
        fs.res_path: {sig.name for sig in fs.signals if sig.scope is None} for fs in all_symbols
    }

    # First occurrence wins for duplicate (res_path, scope, name) pairs --
    # e.g. two overloaded-by-arity-only declarations. Known v1 limitation.
    symbol_lookup: dict[tuple[str, str | None, str], int] = {}
    parse_error_count = 0
    function_count = 0
    signal_count = 0
    field_count = 0
    enum_count = 0

    for pr, fs in zip(parse_results, all_symbols):
        if pr.error is not None:
            parse_error_count += 1
        conn.execute(
            "INSERT INTO files (res_path, class_name, extends, parse_error) VALUES (?, ?, ?, ?)",
            (fs.res_path, fs.class_name, fs.extends, pr.error),
        )
        for func in fs.functions:
            cur = conn.execute(
                "INSERT INTO symbols (res_path, name, kind, scope, line, is_static) "
                "VALUES (?, ?, 'function', ?, ?, ?)",
                (fs.res_path, func.name, func.scope, func.line, int(func.is_static)),
            )
            symbol_lookup.setdefault((fs.res_path, func.scope, func.name), cur.lastrowid)
            function_count += 1
        for sig in fs.signals:
            cur = conn.execute(
                "INSERT INTO symbols (res_path, name, kind, scope, line, is_static) "
                "VALUES (?, ?, 'signal', ?, ?, 0)",
                (fs.res_path, sig.name, sig.scope, sig.line),
            )
            symbol_lookup.setdefault((fs.res_path, sig.scope, sig.name), cur.lastrowid)
            signal_count += 1
        for fld in fs.fields:
            conn.execute(
                "INSERT INTO symbols (res_path, name, kind, scope, line, is_static) "
                "VALUES (?, ?, ?, ?, ?, 0)",
                (fs.res_path, fld.name, fld.kind, fld.scope, fld.line),
            )
            field_count += 1
        for enm in fs.enums:
            conn.execute(
                "INSERT INTO symbols (res_path, name, kind, scope, line, is_static) "
                "VALUES (?, ?, 'enum', ?, ?, 0)",
                (fs.res_path, enm.name, enm.scope, enm.line),
            )
            enum_count += 1

    resolved_call_count = 0
    unresolved_call_count = 0
    resolved_connection_count = 0
    unresolved_connection_count = 0

    for pr, fs in zip(parse_results, all_symbols):
        if pr.tree is None:
            continue

        try:
            signal_names_by_scope: dict[str | None, dict[str, str]] = {}
            for sig in fs.signals:
                signal_names_by_scope.setdefault(sig.scope, {})[sig.name] = fs.res_path

            # Walk the top-level extends chain so a bare `<signal>.connect(...)`
            # in a subclass can reach a signal declared in an ancestor file --
            # own-file declarations take precedence via setdefault.
            top_level_signal_names = signal_names_by_scope.setdefault(None, {})
            visited_ancestors: set[str] = {fs.res_path}
            ancestor = inheritance_map.get(fs.res_path)
            while ancestor is not None and ancestor not in visited_ancestors:
                visited_ancestors.add(ancestor)
                for name in top_level_signals_by_path.get(ancestor, ()):
                    top_level_signal_names.setdefault(name, ancestor)
                ancestor = inheritance_map.get(ancestor)

            raw_calls, raw_connections = extract_calls_and_connections(pr.tree, signal_names_by_scope)

            local_var_types: dict[tuple[str | None, str], dict[str, str | None]] = {}
            lambda_shadowed_names: dict[tuple[str | None, str], set[str]] = {}
            for fd in iter_function_defs(pr.tree):
                local_var_types[(fd.scope, fd.name)] = extract_local_var_types(fd.node)
                lambda_shadowed_names[(fd.scope, fd.name)] = extract_lambda_shadowed_names(fd.node)
            for pa in iter_property_accessor_defs(pr.tree):
                local_var_types[(pa.scope, pa.name)] = extract_property_accessor_local_var_types(pa)
                lambda_shadowed_names[(pa.scope, pa.name)] = extract_property_accessor_lambda_shadowed_names(pa)

            resolved, unresolved = resolve_calls(
                fs, raw_calls, class_name_table, project.autoloads, function_index,
                inheritance_map, local_var_types, lambda_shadowed_names,
            )
            resolved_conns, unresolved_conns = resolve_signal_connections(
                fs, raw_connections, class_name_table, project.autoloads, function_index, inheritance_map,
                local_var_types, lambda_shadowed_names, signal_index,
            )
        except RecursionError:
            # Same pathological-nesting hazard as the extract_symbols guard
            # above, just reachable independently here (a tree can be deep
            # enough to survive that walk but not this one, or vice versa)
            # -- skip this one file's calls/connections rather than
            # aborting the whole build. Unlike the extract_symbols guard,
            # this file's `files` row was already inserted (with whatever
            # parse_error extract_symbols left it with) before this loop
            # even started, so a plain `pr.error = ...` here wouldn't reach
            # it -- without an explicit UPDATE, the file would look fully
            # clean (parse_error NULL, correct symbol counts) while its
            # entire call/signal-connection graph silently vanished with no
            # trace anywhere.
            if pr.error is None:
                pr.error = "too deeply nested to extract calls/connections (exceeded a safe recursion depth)"
                parse_error_count += 1
                conn.execute(
                    "UPDATE files SET parse_error = ? WHERE res_path = ?", (pr.error, fs.res_path)
                )
            continue

        for rc in resolved:
            source_id = symbol_lookup.get((rc.source_res_path, rc.source_scope, rc.source_function))
            target_id = symbol_lookup.get((rc.target_res_path, rc.target_scope, rc.target_function))
            if source_id is None or target_id is None:
                continue
            conn.execute(
                "INSERT INTO calls (source_symbol_id, target_symbol_id, line) VALUES (?, ?, ?)",
                (source_id, target_id, rc.line),
            )
            resolved_call_count += 1
        for uc in unresolved:
            conn.execute(
                "INSERT INTO unresolved_calls "
                "(source_res_path, source_scope, source_function, receiver, called_name, line, reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (uc.source_res_path, uc.source_scope, uc.source_function, uc.receiver, uc.called_name, uc.line, uc.reason),
            )
            unresolved_call_count += 1

        for rc in resolved_conns:
            # A bare/self/inherited `<signal>.connect(...)` reference is
            # always declared in the same *scope* as the connect() call;
            # a signal reached through a chained receiver (autoload/
            # class_name/local) is always top-level (rc.signal_scope is
            # None either way it was resolved) -- rc.signal_scope tracks
            # which one applies, rc.signal_res_path which file declares it
            # (see resolve.py).
            source_id = symbol_lookup.get((rc.source_res_path, rc.source_scope, rc.source_function))
            signal_id = symbol_lookup.get((rc.signal_res_path, rc.signal_scope, rc.signal_name))
            handler_id = symbol_lookup.get((rc.handler_res_path, rc.handler_scope, rc.handler_function))
            if source_id is None or signal_id is None or handler_id is None:
                continue
            conn.execute(
                "INSERT INTO signal_connections (source_symbol_id, signal_symbol_id, handler_symbol_id, line) "
                "VALUES (?, ?, ?, ?)",
                (source_id, signal_id, handler_id, rc.line),
            )
            resolved_connection_count += 1
        for uc in unresolved_conns:
            conn.execute(
                "INSERT INTO unresolved_connections "
                "(source_res_path, source_function, signal_receiver, signal_name, handler_receiver, handler_name, line, reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    uc.source_res_path, uc.source_function, uc.signal_receiver, uc.signal_name,
                    uc.handler_receiver, uc.handler_name, uc.line, uc.reason,
                ),
            )
            unresolved_connection_count += 1

    return BuildStats(
        file_count=len(parse_results),
        parse_error_count=parse_error_count,
        function_count=function_count,
        signal_count=signal_count,
        field_count=field_count,
        enum_count=enum_count,
        resolved_call_count=resolved_call_count,
        unresolved_call_count=unresolved_call_count,
        resolved_connection_count=resolved_connection_count,
        unresolved_connection_count=unresolved_connection_count,
        duplicate_class_names=duplicate_class_names,
    )


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def search_symbols(conn: sqlite3.Connection, query: str, limit: int = 20) -> list[sqlite3.Row]:
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like = f"%{escaped}%"
    return conn.execute(
        "SELECT res_path, name, kind, scope, line FROM symbols WHERE name LIKE ? ESCAPE '\\' ORDER BY name LIMIT ?",
        (like, limit),
    ).fetchall()


def get_callers(
    conn: sqlite3.Connection, function_name: str, res_path: str | None = None, scope: str | None = None
) -> list[sqlite3.Row]:
    query = """
        SELECT src.res_path AS caller_file, src.scope AS caller_scope, src.name AS caller_function,
               c.line AS call_line
        FROM calls c
        JOIN symbols src ON src.id = c.source_symbol_id
        JOIN symbols tgt ON tgt.id = c.target_symbol_id
        WHERE tgt.name = ?
    """
    params: list[str] = [function_name]
    if res_path is not None:
        query += " AND tgt.res_path = ?"
        params.append(res_path)
    if scope is not None:
        query += " AND tgt.scope = ?"
        params.append(scope)
    query += " ORDER BY src.res_path, c.line"
    return conn.execute(query, params).fetchall()


def get_callees(
    conn: sqlite3.Connection, function_name: str, res_path: str | None = None, scope: str | None = None
) -> list[sqlite3.Row]:
    query = """
        SELECT tgt.res_path AS callee_file, tgt.scope AS callee_scope, tgt.name AS callee_function,
               c.line AS call_line
        FROM calls c
        JOIN symbols src ON src.id = c.source_symbol_id
        JOIN symbols tgt ON tgt.id = c.target_symbol_id
        WHERE src.name = ?
    """
    params: list[str] = [function_name]
    if res_path is not None:
        query += " AND src.res_path = ?"
        params.append(res_path)
    if scope is not None:
        query += " AND src.scope = ?"
        params.append(scope)
    query += " ORDER BY tgt.res_path, c.line"
    return conn.execute(query, params).fetchall()


def get_signal_handlers(
    conn: sqlite3.Connection, signal_name: str, res_path: str | None = None, scope: str | None = None
) -> list[sqlite3.Row]:
    query = """
        SELECT sig.res_path AS signal_file, sig.scope AS signal_scope,
               h.res_path AS handler_file, h.scope AS handler_scope, h.name AS handler_function,
               sc.line AS connect_line
        FROM signal_connections sc
        JOIN symbols sig ON sig.id = sc.signal_symbol_id
        JOIN symbols h ON h.id = sc.handler_symbol_id
        WHERE sig.name = ?
    """
    params: list[str] = [signal_name]
    if res_path is not None:
        query += " AND sig.res_path = ?"
        params.append(res_path)
    if scope is not None:
        query += " AND sig.scope = ?"
        params.append(scope)
    query += " ORDER BY h.res_path, sc.line"
    return conn.execute(query, params).fetchall()


def _load_call_edges(conn: sqlite3.Connection) -> list[tuple[int, int]]:
    return [
        (row["source_symbol_id"], row["target_symbol_id"])
        for row in conn.execute("SELECT source_symbol_id, target_symbol_id FROM calls")
    ]


def _seed_symbol_ids(
    conn: sqlite3.Connection, function_name: str, res_path: str | None, scope: str | None
) -> list[int]:
    query = "SELECT id FROM symbols WHERE name = ? AND kind = 'function'"
    params: list[str] = [function_name]
    if res_path is not None:
        query += " AND res_path = ?"
        params.append(res_path)
    if scope is not None:
        query += " AND scope = ?"
        params.append(scope)
    return [row["id"] for row in conn.execute(query, params).fetchall()]


def _bfs_symbols(
    conn: sqlite3.Connection,
    function_name: str,
    res_path: str | None,
    scope: str | None,
    max_depth: int,
    reverse: bool,
) -> list[dict]:
    """BFS over the `calls` edge list from the seed symbol(s). `reverse`
    walks caller-of edges (for callers_transitive) instead of callee edges.

    Runs one BFS per seed and merges by minimum depth, rather than a single
    multi-source BFS seeded with every match at once -- when `function_name`
    is ambiguous (unfiltered, multiple distinct declarations share it), a
    single multi-source BFS would pre-mark every same-named declaration as
    "visited at depth 0" purely for sharing the name, before any edge is
    even followed. That silently swallows a real edge between two same-named
    seeds (e.g. seed A genuinely calling seed B) -- B never appears in the
    output at all, and anything only reachable through B gets reported one
    hop shallower than its real distance from A. Each seed's own reflexive
    case is still excluded from its own run, but a same-named seed reached
    via a genuine edge from a *different* seed is a real result and must be
    reported."""
    adjacency: dict[int, set[int]] = {}
    for src, tgt in _load_call_edges(conn):
        a, b = (tgt, src) if reverse else (src, tgt)
        adjacency.setdefault(a, set()).add(b)

    seeds = _seed_symbol_ids(conn, function_name, res_path, scope)
    best_depth: dict[int, int] = {}
    for seed in seeds:
        visited: dict[int, int] = {seed: 0}
        frontier = [seed]
        depth = 0
        while frontier and depth < max_depth:
            depth += 1
            next_frontier: list[int] = []
            for sid in frontier:
                for neighbor in adjacency.get(sid, ()):
                    if neighbor not in visited:
                        visited[neighbor] = depth
                        next_frontier.append(neighbor)
            frontier = next_frontier
        for node, hop in visited.items():
            if node == seed:
                continue
            if node not in best_depth or hop < best_depth[node]:
                best_depth[node] = hop

    results = []
    for sid, hop in best_depth.items():
        row = conn.execute(
            "SELECT res_path, name, scope, line FROM symbols WHERE id = ?", (sid,)
        ).fetchone()
        if row is not None:
            results.append({
                "res_path": row["res_path"],
                "name": row["name"],
                "scope": row["scope"],
                "line": row["line"],
                "depth": hop,
            })
    results.sort(key=lambda r: (r["depth"], r["res_path"], r["name"]))
    return results


def get_callers_transitive(
    conn: sqlite3.Connection,
    function_name: str,
    res_path: str | None = None,
    scope: str | None = None,
    max_depth: int = 5,
) -> list[dict]:
    return _bfs_symbols(conn, function_name, res_path, scope, max_depth, reverse=True)


def get_callees_transitive(
    conn: sqlite3.Connection,
    function_name: str,
    res_path: str | None = None,
    scope: str | None = None,
    max_depth: int = 5,
) -> list[dict]:
    return _bfs_symbols(conn, function_name, res_path, scope, max_depth, reverse=False)


REQUIRED_TABLES = {
    "files", "symbols", "calls", "unresolved_calls",
    "signal_connections", "unresolved_connections",
}


def validate_schema(conn: sqlite3.Connection) -> None:
    """Raise a clear error if `conn` doesn't look like a gdscript-graph
    database -- e.g. the path didn't exist and sqlite3 silently created an
    empty file, or it was built by an incompatible/older schema version."""
    existing = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = REQUIRED_TABLES - existing
    if missing:
        raise ValueError(
            "not a valid gdscript-graph database (missing tables: "
            f"{', '.join(sorted(missing))}). Run `gdscript-graph build <project_dir>` "
            "first, or check the db path."
        )
