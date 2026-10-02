from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from gdscript_graph.calls import extract_calls_and_connections
from gdscript_graph.discovery import discover
from gdscript_graph.parse_cache import ParseCache, load_cache, parse_all_cached, save_cache
from gdscript_graph.resolve import (
    build_project_index,
    resolve_calls,
    resolve_scene_connections,
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
    signal_name TEXT NOT NULL,
    signal_symbol_id INTEGER,            -- NULL = not a project-declared signal (an engine built-in,
                                         -- e.g. `$Button.pressed`) or one on a receiver of unknown type
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

-- Signal connections saved in a .tscn scene by the editor (`[connection
-- signal=... from=... to=... method=...]`) -- Godot calls these handlers
-- itself, with no `.connect()` anywhere in code.
CREATE TABLE scene_connections (
    id INTEGER PRIMARY KEY,
    scene_res_path TEXT NOT NULL,
    line INTEGER NOT NULL,
    signal_name TEXT NOT NULL,
    from_node TEXT NOT NULL,             -- node paths relative to the scene root ("." = root)
    to_node TEXT NOT NULL,
    signal_symbol_id INTEGER,            -- NULL = an engine built-in signal (e.g. Button.pressed),
                                         -- or not found on the emitting node's script
    handler_symbol_id INTEGER NOT NULL,
    FOREIGN KEY (signal_symbol_id) REFERENCES symbols(id),
    FOREIGN KEY (handler_symbol_id) REFERENCES symbols(id)
);
CREATE INDEX idx_scene_connections_signal ON scene_connections(signal_symbol_id);
CREATE INDEX idx_scene_connections_handler ON scene_connections(handler_symbol_id);

CREATE TABLE unresolved_scene_connections (
    id INTEGER PRIMARY KEY,
    scene_res_path TEXT NOT NULL,
    line INTEGER NOT NULL,
    signal_name TEXT NOT NULL,
    from_node TEXT NOT NULL,
    to_node TEXT NOT NULL,
    method TEXT NOT NULL,
    reason TEXT NOT NULL
);

-- Key/value store for build-time facts about the database itself, as
-- opposed to facts about the indexed project. Lets `gdscript-graph mcp
-- <db>` recover the project root it was built from (to start a file
-- watcher against it) without requiring it as a separate, easy-to-typo
-- CLI argument that could silently point at the wrong directory.
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Carries each file's already-parsed tree forward from one build to the
-- next, keyed by a content hash -- an unchanged hash guarantees a
-- bit-for-bit identical tree (parsing is a pure function of a file's own
-- bytes), so skipping re-parsing on a cache hit is always safe. Not part
-- of REQUIRED_TABLES: it's a pure performance optimization, never
-- required for a db to be valid -- if missing (e.g. the previous build
-- predates this feature, or the db is fresh), every file simply parses
-- fresh, same as before this cache existed.
CREATE TABLE parse_cache (
    res_path TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    tree_blob BLOB NOT NULL
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
    resolved_scene_connection_count: int
    unresolved_scene_connection_count: int
    # class_name -> the res:// paths that declare it, only entries with 2+
    # declarers -- a duplicate class_name is silently resolved by "later
    # file wins" (see build_class_name_table) with no other signal that an
    # ambiguity existed, which is usually a real authoring mistake worth
    # surfacing rather than silently picking a winner.
    duplicate_class_names: dict[str, list[str]]
    # How many of `file_count` files reused a cached tree from the previous
    # build (skipping the ~20-30x more expensive Lark parse) vs. how many
    # were freshly parsed (new/changed file, or no usable previous build).
    parse_cache_hits: int
    parse_cache_misses: int


def build_database(project_root: Path, db_path: Path) -> BuildStats:
    if db_path.exists() and db_path.is_dir():
        raise IsADirectoryError(f"-o path is a directory, not a file: {db_path}")

    # Load the previous build's cached trees (if any) *before* touching
    # db_path -- this is the only chance to read them before the atomic
    # swap below replaces the file they live in.
    old_parse_cache = load_cache(db_path)

    # Build into a temp file and atomically swap it into place at the end,
    # so a failure partway through a rebuild never destroys a previously
    # working database.
    tmp_path = db_path.with_name(f"{db_path.name}.tmp-{os.getpid()}")
    tmp_path.unlink(missing_ok=True)

    conn = sqlite3.connect(tmp_path)
    try:
        stats = _populate(conn, project_root, old_parse_cache)
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


def _populate(conn: sqlite3.Connection, project_root: Path, old_parse_cache: ParseCache | None = None) -> BuildStats:
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO meta (key, value) VALUES (?, ?)",
        [("project_root", str(project_root)), ("built_at", str(time.time()))],
    )

    project = discover(project_root)
    parse_results, new_parse_cache, cache_hits = parse_all_cached(
        project.gd_files, project.to_res_path, old_parse_cache or {}
    )
    save_cache(conn, new_parse_cache)

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

    index = build_project_index(all_symbols, project.autoloads)
    inheritance_map = index.inheritance_map

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
                local_var_types[(fd.scope, fd.name)] = extract_local_var_types(fd.node, fs.res_path)
                lambda_shadowed_names[(fd.scope, fd.name)] = extract_lambda_shadowed_names(fd.node)
            for pa in iter_property_accessor_defs(pr.tree):
                local_var_types[(pa.scope, pa.name)] = extract_property_accessor_local_var_types(pa, fs.res_path)
                lambda_shadowed_names[(pa.scope, pa.name)] = extract_property_accessor_lambda_shadowed_names(pa)

            resolved, unresolved = resolve_calls(fs, raw_calls, index, local_var_types, lambda_shadowed_names)
            resolved_conns, unresolved_conns = resolve_signal_connections(
                fs, raw_connections, index, local_var_types, lambda_shadowed_names,
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
            # always declared in the same *scope* as the connect() call; a
            # signal reached through a receiver is declared wherever that
            # receiver's type says -- rc.signal_scope/rc.signal_res_path
            # carry whichever applies (see resolve.py).
            source_id = symbol_lookup.get((rc.source_res_path, rc.source_scope, rc.source_function))
            signal_id = (
                symbol_lookup.get((rc.signal_res_path, rc.signal_scope, rc.signal_name))
                if rc.signal_res_path is not None else None
            )
            handler_id = symbol_lookup.get((rc.handler_res_path, rc.handler_scope, rc.handler_function))
            if source_id is None or handler_id is None:
                continue
            conn.execute(
                "INSERT INTO signal_connections "
                "(source_symbol_id, signal_name, signal_symbol_id, handler_symbol_id, line) VALUES (?, ?, ?, ?, ?)",
                (source_id, rc.signal_name, signal_id, handler_id, rc.line),
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

    resolved_scene_connection_count = 0
    unresolved_scene_connection_count = 0
    scenes = project.scenes
    for scene_file in project.scene_files if scenes is not None else ():
        scene = scenes.get(project.to_res_path(scene_file))
        if scene is None:
            continue
        resolved_scene_conns, unresolved_scene_conns = resolve_scene_connections(scene, scenes, index)
        for sc in resolved_scene_conns:
            handler_id = symbol_lookup.get((sc.handler_res_path, None, sc.handler_function))
            if handler_id is None:
                continue
            signal_id = (
                symbol_lookup.get((sc.signal_res_path, None, sc.signal_name))
                if sc.signal_res_path is not None else None
            )
            conn.execute(
                "INSERT INTO scene_connections "
                "(scene_res_path, line, signal_name, from_node, to_node, signal_symbol_id, handler_symbol_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sc.scene_res_path, sc.line, sc.signal_name, sc.from_node, sc.to_node, signal_id, handler_id),
            )
            resolved_scene_connection_count += 1
        for uc in unresolved_scene_conns:
            conn.execute(
                "INSERT INTO unresolved_scene_connections "
                "(scene_res_path, line, signal_name, from_node, to_node, method, reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (uc.scene_res_path, uc.line, uc.signal_name, uc.from_node, uc.to_node, uc.method, uc.reason),
            )
            unresolved_scene_connection_count += 1

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
        resolved_scene_connection_count=resolved_scene_connection_count,
        unresolved_scene_connection_count=unresolved_scene_connection_count,
        duplicate_class_names=duplicate_class_names,
        parse_cache_hits=cache_hits,
        parse_cache_misses=len(parse_results) - cache_hits,
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


def list_files(conn: sqlite3.Connection, prefix: str | None = None) -> list[dict]:
    query = "SELECT res_path, class_name, extends, parse_error FROM files"
    params: list[str] = []
    if prefix is not None:
        # Escape LIKE metacharacters same as search_symbols -- a prefix
        # containing a literal "%"/"_" (unusual in a res:// path, but not
        # impossible) must be matched literally, not as a wildcard.
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query += " WHERE res_path LIKE ? ESCAPE '\\'"
        params.append(f"{escaped}%")
    query += " ORDER BY res_path"
    file_rows = conn.execute(query, params).fetchall()

    # One query for symbol counts across every file, not one query per
    # file -- a `files()` call must stay cheap regardless of project size.
    counts_by_file: dict[str, dict[str, int]] = {}
    for row in conn.execute("SELECT res_path, kind, COUNT(*) AS n FROM symbols GROUP BY res_path, kind"):
        counts_by_file.setdefault(row["res_path"], {})[row["kind"]] = row["n"]

    return [
        {**dict(row), "symbol_counts": counts_by_file.get(row["res_path"], {})}
        for row in file_rows
    ]


def find_symbol_locations(
    conn: sqlite3.Connection,
    name: str,
    res_path: str | None = None,
    scope: str | None = None,
    kind: str | None = None,
) -> list[sqlite3.Row]:
    query = "SELECT id, res_path, name, kind, scope, line, is_static FROM symbols WHERE name = ?"
    params: list[str] = [name]
    if res_path is not None:
        query += " AND res_path = ?"
        params.append(res_path)
    if scope is not None:
        query += " AND scope = ?"
        params.append(scope)
    if kind is not None:
        query += " AND kind = ?"
        params.append(kind)
    query += " ORDER BY res_path, scope, line"
    return conn.execute(query, params).fetchall()


def _symbol_filter(alias: str, name: str, res_path: str | None, scope: str | None) -> tuple[str, list[str]]:
    """SQL condition (and its params) matching symbols named `name` under
    table alias `alias`, optionally narrowed to one file and/or scope."""
    condition = f"{alias}.name = ?"
    params = [name]
    if res_path is not None:
        condition += f" AND {alias}.res_path = ?"
        params.append(res_path)
    if scope is not None:
        condition += f" AND {alias}.scope = ?"
        params.append(scope)
    return condition, params


def get_callers(
    conn: sqlite3.Connection, function_name: str, res_path: str | None = None, scope: str | None = None
) -> list[dict]:
    """Everything that makes the given function run: direct calls, and its
    registrations as a signal handler -- a `<signal>.connect(<function>)`
    in code (`via: "connect"`, with the function containing the
    `.connect()` as the caller) or a `[connection]` saved in a .tscn scene
    (`via: "scene"`, with the scene file as the caller and no caller
    function). A signal handler is rarely called directly, so without the
    latter two it would look like dead code -- and changing its signature
    breaks exactly those connection sites. Only those two kinds carry
    `via`; a row without it is a direct call (the common case, kept as
    small as before for a function with hundreds of call sites)."""
    condition, params = _symbol_filter("tgt", function_name, res_path, scope)
    rows: list[dict] = [
        {
            "caller_file": r["res_path"], "caller_scope": r["scope"], "caller_function": r["name"],
            "call_line": r["line"],
        }
        for r in conn.execute(f"""
            SELECT src.res_path, src.scope, src.name, c.line
            FROM calls c
            JOIN symbols src ON src.id = c.source_symbol_id
            JOIN symbols tgt ON tgt.id = c.target_symbol_id
            WHERE {condition}
        """, params)
    ]
    rows += [
        {
            "caller_file": r["res_path"], "caller_scope": r["scope"], "caller_function": r["name"],
            "call_line": r["line"], "via": "connect", "signal": r["signal_name"],
        }
        for r in conn.execute(f"""
            SELECT src.res_path, src.scope, src.name, sc.line, sc.signal_name
            FROM signal_connections sc
            JOIN symbols src ON src.id = sc.source_symbol_id
            JOIN symbols tgt ON tgt.id = sc.handler_symbol_id
            WHERE {condition}
        """, params)
    ]
    rows += [
        {
            "caller_file": r["scene_res_path"], "caller_scope": None, "caller_function": None,
            "call_line": r["line"], "via": "scene", "signal": r["signal_name"],
            "from_node": r["from_node"], "to_node": r["to_node"],
        }
        for r in conn.execute(f"""
            SELECT sc.scene_res_path, sc.line, sc.signal_name, sc.from_node, sc.to_node
            FROM scene_connections sc
            JOIN symbols tgt ON tgt.id = sc.handler_symbol_id
            WHERE {condition}
        """, params)
    ]
    rows.sort(key=lambda r: (r["caller_file"], r["call_line"]))
    return rows


def get_callees(
    conn: sqlite3.Connection, function_name: str, res_path: str | None = None, scope: str | None = None
) -> list[dict]:
    """Functions the given function calls, and the signal handlers it
    registers with `<signal>.connect(<handler>)` (marked `via: "connect"`)
    -- code that runs because of it either way."""
    condition, params = _symbol_filter("src", function_name, res_path, scope)
    rows: list[dict] = [
        {
            "callee_file": r["res_path"], "callee_scope": r["scope"], "callee_function": r["name"],
            "call_line": r["line"],
        }
        for r in conn.execute(f"""
            SELECT tgt.res_path, tgt.scope, tgt.name, c.line
            FROM calls c
            JOIN symbols src ON src.id = c.source_symbol_id
            JOIN symbols tgt ON tgt.id = c.target_symbol_id
            WHERE {condition}
        """, params)
    ]
    rows += [
        {
            "callee_file": r["res_path"], "callee_scope": r["scope"], "callee_function": r["name"],
            "call_line": r["line"], "via": "connect", "signal": r["signal_name"],
        }
        for r in conn.execute(f"""
            SELECT h.res_path, h.scope, h.name, sc.line, sc.signal_name
            FROM signal_connections sc
            JOIN symbols src ON src.id = sc.source_symbol_id
            JOIN symbols h ON h.id = sc.handler_symbol_id
            WHERE {condition}
        """, params)
    ]
    rows.sort(key=lambda r: (r["callee_file"], r["call_line"]))
    return rows


def get_signal_handlers(
    conn: sqlite3.Connection, signal_name: str, res_path: str | None = None, scope: str | None = None
) -> list[dict]:
    """Functions connected to the given signal -- with `.connect()` in code,
    or in a .tscn scene (marked `via: "scene"`, plus `from_node`/`to_node`).
    `connected_in` is the file the connection is made in. Without a `res_path`/`scope`
    filter, connections to a signal of that name that isn't
    project-declared (an engine built-in like `pressed`, or one on a
    receiver of unknown type) are included too, with
    `signal_file`/`signal_scope` None."""
    condition, params = _symbol_filter("sig", signal_name, res_path, scope)
    if res_path is None and scope is None:
        # Match by the recorded name, so unlinked (engine) signals count too.
        condition, params = "connection.signal_name = ?", [signal_name]
    rows: list[dict] = [
        {
            "signal_file": r["signal_file"], "signal_scope": r["signal_scope"],
            "handler_file": r["handler_file"], "handler_scope": r["handler_scope"],
            "handler_function": r["handler_function"], "connect_line": r["line"],
            "connected_in": r["connected_in"],
        }
        for r in conn.execute(f"""
            SELECT sig.res_path AS signal_file, sig.scope AS signal_scope,
                   h.res_path AS handler_file, h.scope AS handler_scope, h.name AS handler_function,
                   connection.line, src.res_path AS connected_in
            FROM signal_connections connection
            LEFT JOIN symbols sig ON sig.id = connection.signal_symbol_id
            JOIN symbols h ON h.id = connection.handler_symbol_id
            JOIN symbols src ON src.id = connection.source_symbol_id
            WHERE {condition}
        """, params)
    ]
    rows += [
        {
            "signal_file": r["signal_file"], "signal_scope": r["signal_scope"],
            "handler_file": r["handler_file"], "handler_scope": r["handler_scope"],
            "handler_function": r["handler_function"], "connect_line": r["line"],
            "via": "scene", "connected_in": r["scene_res_path"],
            "from_node": r["from_node"], "to_node": r["to_node"],
        }
        for r in conn.execute(f"""
            SELECT sig.res_path AS signal_file, sig.scope AS signal_scope,
                   h.res_path AS handler_file, h.scope AS handler_scope, h.name AS handler_function,
                   connection.line, connection.scene_res_path, connection.from_node, connection.to_node
            FROM scene_connections connection
            LEFT JOIN symbols sig ON sig.id = connection.signal_symbol_id
            JOIN symbols h ON h.id = connection.handler_symbol_id
            WHERE {condition}
        """, params)
    ]
    rows.sort(key=lambda r: (r["handler_file"], r["connected_in"], r["connect_line"]))
    return rows


def _load_call_edges(conn: sqlite3.Connection) -> list[tuple[int, int]]:
    return [
        (row["source_symbol_id"], row["target_symbol_id"])
        for row in conn.execute("SELECT source_symbol_id, target_symbol_id FROM calls")
    ]


def _load_connect_edges(conn: sqlite3.Connection) -> list[tuple[int, int]]:
    """(function containing a `.connect()`, the handler it registers)."""
    return [
        (row["source_symbol_id"], row["handler_symbol_id"])
        for row in conn.execute("SELECT source_symbol_id, handler_symbol_id FROM signal_connections")
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
    """BFS from the seed symbol(s) over call edges and `.connect()` edges
    (connecting function -> handler). `reverse` walks them backwards (for
    callers_transitive), and then also reports the .tscn scene connections
    into any function reached -- terminal entries, since a scene isn't a
    function with callers of its own. An entry reached through a connect
    edge or a scene connection says so in `via` ("connect" / "scene"); one
    without `via` was reached through a direct call.

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
    # neighbor -> edge kind; a direct call wins over a connect between the same pair.
    adjacency: dict[int, dict[int, str | None]] = {}
    for kind, edges in (("connect", _load_connect_edges(conn)), (None, _load_call_edges(conn))):
        for src, tgt in edges:
            a, b = (tgt, src) if reverse else (src, tgt)
            adjacency.setdefault(a, {})[b] = kind

    seeds = _seed_symbol_ids(conn, function_name, res_path, scope)
    best: dict[int, tuple[int, str | None]] = {}  # symbol id -> (min depth, via)
    reached_at: dict[int, int] = {}  # every reached function, seeds included -> min depth
    for seed in seeds:
        visited: dict[int, tuple[int, str | None]] = {seed: (0, None)}
        frontier = [seed]
        depth = 0
        while frontier and depth < max_depth:
            depth += 1
            next_frontier: list[int] = []
            for sid in frontier:
                for neighbor, kind in adjacency.get(sid, {}).items():
                    if neighbor not in visited:
                        visited[neighbor] = (depth, kind)
                        next_frontier.append(neighbor)
            frontier = next_frontier
        for node, (hop, kind) in visited.items():
            if node not in reached_at or hop < reached_at[node]:
                reached_at[node] = hop
            if node == seed:
                continue
            if node not in best or hop < best[node][0]:
                best[node] = (hop, kind)

    results = []
    for sid, (hop, kind) in best.items():
        row = conn.execute(
            "SELECT res_path, name, scope, line FROM symbols WHERE id = ?", (sid,)
        ).fetchone()
        if row is not None:
            entry = {
                "res_path": row["res_path"],
                "name": row["name"],
                "scope": row["scope"],
                "line": row["line"],
                "depth": hop,
            }
            if kind is not None:
                entry["via"] = kind
            results.append(entry)

    if reverse:
        scene_best: dict[int, tuple[int, sqlite3.Row]] = {}
        for sid, hop in reached_at.items():
            if hop + 1 > max_depth:
                continue
            for sc in conn.execute(
                "SELECT sc.id, sc.scene_res_path, sc.line, sc.signal_name, sc.from_node, sc.to_node, "
                "h.name AS method FROM scene_connections sc JOIN symbols h ON h.id = sc.handler_symbol_id "
                "WHERE sc.handler_symbol_id = ?",
                (sid,),
            ):
                if sc["id"] not in scene_best or hop + 1 < scene_best[sc["id"]][0]:
                    scene_best[sc["id"]] = (hop + 1, sc)
        for hop, sc in scene_best.values():
            results.append({
                "res_path": sc["scene_res_path"],
                "name": None,
                "scope": None,
                "line": sc["line"],
                "depth": hop,
                "via": "scene",
                "signal": sc["signal_name"],
                "from_node": sc["from_node"],
                "to_node": sc["to_node"],
                "method": sc["method"],
            })

    results.sort(key=lambda r: (r["depth"], r["res_path"], r["name"] or "", r["line"]))
    return results


def find_call_path(
    conn: sqlite3.Connection, from_symbol_id: int, to_symbol_id: int, max_depth: int = 6
) -> list[dict] | None:
    """BFS over `calls` from `from_symbol_id` to `to_symbol_id`, returning
    one concrete sequence of function symbols connecting them (inclusive of
    both endpoints), or None if `to_symbol_id` isn't reachable within
    `max_depth` hops. Unlike `get_callees_transitive` (which reports every
    reachable symbol's minimum depth, merged across possibly-ambiguous
    seeds), this reconstructs an actual path between two specific,
    already-resolved symbol ids -- for `explore` to show *how* one named
    symbol reaches another, not just whether it can."""
    if from_symbol_id == to_symbol_id:
        row = conn.execute(
            "SELECT res_path, scope, name FROM symbols WHERE id = ?", (from_symbol_id,)
        ).fetchone()
        return [dict(row)] if row is not None else None

    adjacency: dict[int, set[int]] = {}
    for src, tgt in _load_call_edges(conn):
        adjacency.setdefault(src, set()).add(tgt)

    parent: dict[int, int] = {}
    visited = {from_symbol_id}
    frontier = [from_symbol_id]
    depth = 0
    while frontier and depth < max_depth:
        depth += 1
        next_frontier: list[int] = []
        for sid in frontier:
            for neighbor in adjacency.get(sid, ()):
                if neighbor in visited:
                    continue
                visited.add(neighbor)
                parent[neighbor] = sid
                if neighbor == to_symbol_id:
                    chain = [neighbor]
                    while chain[-1] != from_symbol_id:
                        chain.append(parent[chain[-1]])
                    chain.reverse()
                    rows = {
                        row["id"]: row
                        for row in conn.execute(
                            f"SELECT id, res_path, scope, name FROM symbols WHERE id IN ({','.join('?' * len(chain))})",
                            chain,
                        )
                    }
                    return [dict(rows[sid]) for sid in chain]
                next_frontier.append(neighbor)
        frontier = next_frontier
    return None


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
    "scene_connections", "unresolved_scene_connections", "meta",
}


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row is not None else None


def validate_schema(conn: sqlite3.Connection) -> None:
    """Raise a clear error if `conn` doesn't look like a gdscript-graph
    database -- e.g. the path didn't exist and sqlite3 silently created an
    empty file, or it was built by an incompatible/older schema version."""
    existing = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = REQUIRED_TABLES - existing
    if missing:
        raise ValueError(
            "not a valid gdscript-graph database, or one built by an older version "
            f"(missing tables: {', '.join(sorted(missing))}). Run `gdscript-graph build "
            "<project_dir>` to (re)build it, or check the db path."
        )
