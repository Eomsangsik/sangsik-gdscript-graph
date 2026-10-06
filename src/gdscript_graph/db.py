from __future__ import annotations

import functools
import hashlib
import os
import sqlite3
import stat
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from gdscript_graph.discovery import ProjectFiles, discover
from gdscript_graph.extract import ExtractCache, extract_all, load_cache, save_cache
from gdscript_graph.locking import build_lock
from gdscript_graph.resolve import (
    build_project_index,
    resolve_calls,
    resolve_scene_connections,
    resolve_signal_connections,
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

-- Every input file the build read, as it was on disk then: a server start
-- compares this against the project to skip a rebuild when nothing changed.
CREATE TABLE inputs (
    res_path TEXT PRIMARY KEY,
    mtime_ns INTEGER NOT NULL,
    size INTEGER NOT NULL
);

-- Carries each file's extraction (symbols, raw calls, local types -- see
-- extract.FileExtract) forward from one build to the next, keyed by a
-- content hash; meta's `extract_cache_version` says which extractor wrote
-- it. Not part of REQUIRED_TABLES: it's a pure performance optimization,
-- never required for a db to be valid -- if missing or from another
-- version, every file is simply extracted fresh.
CREATE TABLE extract_cache (
    res_path TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    blob BLOB NOT NULL
);
"""


# `unresolved_calls.reason`s of a call that may really be into project code
# the graph failed to follow; every other reason is a call into the engine
# or GDScript itself (see resolve.UnresolvedCall).
POSSIBLY_MISSED_REASONS = ("unknown_receiver", "method_not_found_in_target")

# A build's temp db is `<db name><TEMP_INFIX><pid>-<random>`, next to the db.
TEMP_INFIX = ".tmp-"


@dataclass
class BuildStats:
    file_count: int
    parse_error_count: int
    function_count: int
    signal_count: int
    field_count: int
    enum_count: int
    resolved_call_count: int
    unresolved_call_count: int  # possibly-missed project calls (see POSSIBLY_MISSED_REASONS)
    engine_call_count: int  # calls into the engine / GDScript built-ins
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
    # How many of `file_count` files reused the previous build's extraction
    # (skipping the parse and tree walks) vs. how many were extracted fresh
    # (new/changed file, or no usable previous build).
    cache_hits: int
    cache_misses: int


def build_database(
    project_root: Path,
    db_path: Path,
    if_changed: bool = False,
    on_build_start: Callable[[], None] | None = None,
) -> BuildStats | None:
    """Build (or rebuild) the graph of `project_root` into `db_path`. With
    `if_changed`, skip it -- returning None -- when `db_path` is already a
    build of the project exactly as it is now (see `_is_current`).
    `on_build_start` is called once it's decided a build will happen."""
    if db_path.exists() and db_path.is_dir():
        raise IsADirectoryError(f"-o path is a directory, not a file: {db_path}")

    project = discover(project_root)
    manifest = input_manifest(project)
    # Checked before taking the lock too: a read-only look that shouldn't
    # make anyone wait, or look like a rebuild in progress.
    if if_changed and _is_current(db_path, manifest):
        lock = build_lock(db_path)
        if lock.acquire(blocking=False):
            try:
                _remove_stale_temp_files(db_path)
            finally:
                lock.release()
        return None

    # One build of a db at a time, across every process (MCP servers of
    # several sessions, a CLI build): a later build waits and then reuses the
    # earlier one's cache instead of redoing all of its work concurrently.
    with build_lock(db_path):
        _remove_stale_temp_files(db_path)
        # ...and checked again, in case the build waited on just did it.
        if if_changed and _is_current(db_path, manifest):
            return None
        if on_build_start is not None:
            on_build_start()
        return _build_locked(project, manifest, db_path)


def input_manifest(project: ProjectFiles) -> dict[str, tuple[int, int]]:
    """res:// path -> (mtime_ns, size) of every file a build reads."""
    manifest: dict[str, tuple[int, int]] = {}
    for path in (*project.gd_files, *project.scene_files, project.root / "project.godot"):
        try:
            st = path.stat()
        except OSError:
            continue
        manifest[project.to_res_path(path)] = (st.st_mtime_ns, st.st_size)
    return manifest


@functools.cache
def builder_version() -> str:
    """Identifies the code (and bundled engine API data) that builds a db:
    a db built by any other version is rebuilt even if no project file
    changed since."""
    package = Path(__file__).parent
    digest = hashlib.sha256()
    for path in sorted([*package.glob("*.py"), *package.glob("*.json")]):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


# A file modified this close to a build's start could have changed again
# within the same modification-time tick (1-2 s on some filesystems) after
# that build read it -- so its unchanged mtime proves nothing.
_MTIME_TRUST_MARGIN_NS = 2_000_000_000


def _unchanged_since_last_build(db_path: Path, manifest: dict[str, tuple[int, int]]) -> set[str]:
    """res:// paths whose modification time and size match what the
    previous build of `db_path` recorded, and which were last modified
    well before it started -- so its cached extraction can be reused
    without reading the file at all (reading and hashing every file was
    most of an unchanged rebuild's time)."""
    try:
        conn = connect(db_path)
        try:
            built_at = get_meta(conn, "built_at")
            stored = {
                row["res_path"]: (row["mtime_ns"], row["size"])
                for row in conn.execute("SELECT res_path, mtime_ns, size FROM inputs")
            }
        finally:
            conn.close()
    except (sqlite3.Error, ValueError):
        return set()
    if built_at is None:
        return set()
    cutoff = int(float(built_at) * 1e9) - _MTIME_TRUST_MARGIN_NS
    return {path for path, stat in manifest.items() if stored.get(path) == stat and stat[0] < cutoff}


def _is_current(db_path: Path, manifest: dict[str, tuple[int, int]]) -> bool:
    """Whether `db_path` is a complete build, by this code, of exactly the
    input files in `manifest` -- same paths, modification times and sizes.
    Lets a server start skip the rebuild it would otherwise run on every
    launch to catch edits made while no server was running."""
    if not db_path.exists():
        return False
    try:
        conn = connect(db_path)
        try:
            validate_schema(conn)
            if get_meta(conn, "builder_version") != builder_version():
                return False
            stored = {
                row["res_path"]: (row["mtime_ns"], row["size"])
                for row in conn.execute("SELECT res_path, mtime_ns, size FROM inputs")
            }
        finally:
            conn.close()
    except (sqlite3.Error, ValueError):
        return False
    return stored == manifest


def _remove_stale_temp_files(db_path: Path) -> None:
    """Delete temp dbs (and their journals) left by builds that never
    finished -- a server killed mid-build, e.g. when its session ended.
    Only called under the build lock, so no live build owns one."""
    for path in db_path.parent.glob(f"{db_path.name}{TEMP_INFIX}*"):
        try:
            path.unlink()
        except OSError:
            pass


def _build_locked(project: ProjectFiles, manifest: dict[str, tuple[int, int]], db_path: Path) -> BuildStats:
    # Load the previous build's cache (if any) *before* touching db_path --
    # this is the only chance to read it before the atomic swap below
    # replaces the file it lives in.
    old_cache = load_cache(db_path)
    unchanged = _unchanged_since_last_build(db_path, manifest) if old_cache else set()

    # Build into a temp file and atomically swap it into place at the end,
    # so a failure partway through a rebuild never destroys a previously
    # working database. The name is unique per build, not just per process:
    # two builds sharing one temp file used to unlink each other's work, and
    # the first to finish swapped the other's still-empty db into place.
    fd, tmp_name = tempfile.mkstemp(dir=db_path.parent, prefix=f"{db_path.name}{TEMP_INFIX}{os.getpid()}-")
    os.close(fd)
    tmp_path = Path(tmp_name)
    # mkstemp creates the file owner-only (0600); keep the db's own mode.
    try:
        mode = stat.S_IMODE(db_path.stat().st_mode)
    except OSError:
        mode = 0o644
    os.chmod(tmp_path, mode)

    conn = sqlite3.connect(tmp_path)
    try:
        stats = _populate(conn, project, manifest, old_cache, unchanged)
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


def _populate(
    conn: sqlite3.Connection,
    project: ProjectFiles,
    manifest: dict[str, tuple[int, int]],
    old_cache: ExtractCache | None = None,
    unchanged: set[str] | frozenset[str] = frozenset(),
) -> BuildStats:
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO meta (key, value) VALUES (?, ?)",
        [
            ("project_root", str(project.root)),
            ("built_at", str(time.time())),
            ("builder_version", builder_version()),
        ],
    )
    conn.executemany(
        "INSERT INTO inputs (res_path, mtime_ns, size) VALUES (?, ?, ?)",
        [(path, mtime_ns, size) for path, (mtime_ns, size) in manifest.items()],
    )

    extracts, new_cache, cache_hits = extract_all(project.gd_files, project.to_res_path, old_cache or {}, unchanged)
    save_cache(conn, new_cache)

    all_symbols = [fx.symbols for fx in extracts]
    index = build_project_index(all_symbols, project.autoloads)

    class_name_declarers: dict[str, list[str]] = {}
    for fs in all_symbols:
        if fs.class_name:
            class_name_declarers.setdefault(fs.class_name, []).append(fs.res_path)
    duplicate_class_names = {name: paths for name, paths in class_name_declarers.items() if len(paths) > 1}

    # Symbol ids are assigned here rather than read back per insert, so
    # each table goes in with one executemany. First occurrence wins for
    # duplicate (res_path, scope, name) pairs -- e.g. two overloaded-by-
    # arity-only declarations. Known v1 limitation.
    symbol_lookup: dict[tuple[str, str | None, str], int] = {}
    file_rows: list[tuple] = []
    symbol_rows: list[tuple] = []
    parse_error_count = function_count = signal_count = field_count = enum_count = 0

    def add_symbol(res_path: str, name: str, kind: str, scope: str | None, line: int, is_static: bool) -> int:
        symbol_id = len(symbol_rows) + 1
        symbol_rows.append((symbol_id, res_path, name, kind, scope, line, int(is_static)))
        return symbol_id

    for fx in extracts:
        fs = fx.symbols
        if fx.error is not None:
            parse_error_count += 1
        file_rows.append((fs.res_path, fs.class_name, fs.extends, fx.error))
        for func in fs.functions:
            symbol_id = add_symbol(fs.res_path, func.name, "function", func.scope, func.line, func.is_static)
            symbol_lookup.setdefault((fs.res_path, func.scope, func.name), symbol_id)
            function_count += 1
        for sig in fs.signals:
            symbol_id = add_symbol(fs.res_path, sig.name, "signal", sig.scope, sig.line, False)
            symbol_lookup.setdefault((fs.res_path, sig.scope, sig.name), symbol_id)
            signal_count += 1
        for fld in fs.fields:
            add_symbol(fs.res_path, fld.name, fld.kind, fld.scope, fld.line, False)
            field_count += 1
        for enm in fs.enums:
            add_symbol(fs.res_path, enm.name, "enum", enm.scope, enm.line, False)
            enum_count += 1
    conn.executemany("INSERT INTO files (res_path, class_name, extends, parse_error) VALUES (?, ?, ?, ?)", file_rows)
    conn.executemany(
        "INSERT INTO symbols (id, res_path, name, kind, scope, line, is_static) VALUES (?, ?, ?, ?, ?, ?, ?)",
        symbol_rows,
    )

    call_rows: list[tuple] = []
    unresolved_call_rows: list[tuple] = []
    connection_rows: list[tuple] = []
    unresolved_connection_rows: list[tuple] = []
    for fx in extracts:
        fs = fx.symbols
        resolved, unresolved = resolve_calls(fs, fx.calls, index, fx.local_var_types, fx.lambda_shadowed_names)
        resolved_conns, unresolved_conns = resolve_signal_connections(
            fs, fx.connections, index, fx.local_var_types, fx.lambda_shadowed_names,
        )

        for rc in resolved:
            source_id = symbol_lookup.get((rc.source_res_path, rc.source_scope, rc.source_function))
            target_id = symbol_lookup.get((rc.target_res_path, rc.target_scope, rc.target_function))
            if source_id is not None and target_id is not None:
                call_rows.append((source_id, target_id, rc.line))
        unresolved_call_rows += [
            (uc.source_res_path, uc.source_scope, uc.source_function, uc.receiver, uc.called_name, uc.line, uc.reason)
            for uc in unresolved
        ]

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
            if source_id is not None and handler_id is not None:
                connection_rows.append((source_id, rc.signal_name, signal_id, handler_id, rc.line))
        unresolved_connection_rows += [
            (
                uc.source_res_path, uc.source_function, uc.signal_receiver, uc.signal_name,
                uc.handler_receiver, uc.handler_name, uc.line, uc.reason,
            )
            for uc in unresolved_conns
        ]

    conn.executemany("INSERT INTO calls (source_symbol_id, target_symbol_id, line) VALUES (?, ?, ?)", call_rows)
    conn.executemany(
        "INSERT INTO unresolved_calls "
        "(source_res_path, source_scope, source_function, receiver, called_name, line, reason) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        unresolved_call_rows,
    )
    conn.executemany(
        "INSERT INTO signal_connections "
        "(source_symbol_id, signal_name, signal_symbol_id, handler_symbol_id, line) VALUES (?, ?, ?, ?, ?)",
        connection_rows,
    )
    conn.executemany(
        "INSERT INTO unresolved_connections "
        "(source_res_path, source_function, signal_receiver, signal_name, handler_receiver, handler_name, line, reason) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        unresolved_connection_rows,
    )
    resolved_call_count = len(call_rows)
    unresolved_call_count = sum(1 for row in unresolved_call_rows if row[-1] in POSSIBLY_MISSED_REASONS)
    engine_call_count = len(unresolved_call_rows) - unresolved_call_count
    resolved_connection_count, unresolved_connection_count = len(connection_rows), len(unresolved_connection_rows)

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
        file_count=len(extracts),
        parse_error_count=parse_error_count,
        function_count=function_count,
        signal_count=signal_count,
        field_count=field_count,
        enum_count=enum_count,
        resolved_call_count=resolved_call_count,
        unresolved_call_count=unresolved_call_count,
        engine_call_count=engine_call_count,
        resolved_connection_count=resolved_connection_count,
        unresolved_connection_count=unresolved_connection_count,
        resolved_scene_connection_count=resolved_scene_connection_count,
        unresolved_scene_connection_count=unresolved_scene_connection_count,
        duplicate_class_names=duplicate_class_names,
        cache_hits=cache_hits,
        cache_misses=len(extracts) - cache_hits,
    )


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_symbols(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 20,
    kind: str | None = None,
    path_prefix: str | None = None,
) -> tuple[list[sqlite3.Row], int]:
    """Symbols whose name contains `query` (case-insensitively), optionally
    only of one `kind` and/or under a res:// `path_prefix` -- best matches
    first: an exact name, then the exact name in another case, then names
    starting with `query`, then the rest, shorter names first within each.
    (Ordered by name alone, an exact `get` sank below `get_a`... `get_z`.)
    Returns (up to `limit` rows, how many match in all)."""
    escaped = _like_escape(query)
    where = "name LIKE ? ESCAPE '\\'"
    params: list = [f"%{escaped}%"]
    if kind is not None:
        where += " AND kind = ?"
        params.append(kind)
    if path_prefix is not None:
        where += " AND res_path LIKE ? ESCAPE '\\'"
        params.append(f"{_like_escape(path_prefix)}%")
    total = conn.execute(f"SELECT COUNT(*) FROM symbols WHERE {where}", params).fetchone()[0]
    rows = conn.execute(
        f"""SELECT res_path, name, kind, scope, line FROM symbols WHERE {where}
            ORDER BY CASE
                WHEN name = ? THEN 0
                WHEN lower(name) = lower(?) THEN 1
                WHEN name LIKE ? ESCAPE '\\' THEN 2
                ELSE 3
            END, length(name), name, res_path, line
            LIMIT ?""",
        [*params, query, query, f"{escaped}%", limit],
    ).fetchall()
    return rows, total


def list_files(conn: sqlite3.Connection, prefix: str | None = None) -> list[dict]:
    query = "SELECT res_path, class_name, extends, parse_error FROM files"
    params: list[str] = []
    if prefix is not None:
        # Escape LIKE metacharacters same as search_symbols -- a prefix
        # containing a literal "%"/"_" (unusual in a res:// path, but not
        # impossible) must be matched literally, not as a wildcard.
        query += " WHERE res_path LIKE ? ESCAPE '\\'"
        params.append(f"{_like_escape(prefix)}%")
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


def _endpoint(role: str, row: sqlite3.Row, include: bool) -> dict:
    """`{<role>_file, <role>_scope}` from a row's `tgt_*`/`src_*` columns
    (whichever `role` names), or nothing unless `include`."""
    if not include:
        return {}
    prefix = "tgt" if role == "target" else "src"
    return {f"{role}_file": row[f"{prefix}_path"], f"{role}_scope": row[f"{prefix}_scope"]}


def get_callers(
    conn: sqlite3.Connection,
    function_name: str,
    res_path: str | None = None,
    scope: str | None = None,
    with_target: bool = False,
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
    small as before for a function with hundreds of call sites).
    `with_target` adds which declaration each row reaches
    (`target_file`/`target_scope`) -- for a name several declare."""
    condition, params = _symbol_filter("tgt", function_name, res_path, scope)
    rows: list[dict] = [
        {
            "caller_file": r["res_path"], "caller_scope": r["scope"], "caller_function": r["name"],
            "call_line": r["line"], **_endpoint("target", r, with_target),
        }
        for r in conn.execute(f"""
            SELECT src.res_path, src.scope, src.name, c.line, tgt.res_path AS tgt_path, tgt.scope AS tgt_scope
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
            **_endpoint("target", r, with_target),
        }
        for r in conn.execute(f"""
            SELECT src.res_path, src.scope, src.name, sc.line, sc.signal_name,
                   tgt.res_path AS tgt_path, tgt.scope AS tgt_scope
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
            "from_node": r["from_node"], "to_node": r["to_node"], **_endpoint("target", r, with_target),
        }
        for r in conn.execute(f"""
            SELECT sc.scene_res_path, sc.line, sc.signal_name, sc.from_node, sc.to_node,
                   tgt.res_path AS tgt_path, tgt.scope AS tgt_scope
            FROM scene_connections sc
            JOIN symbols tgt ON tgt.id = sc.handler_symbol_id
            WHERE {condition}
        """, params)
    ]
    rows.sort(key=lambda r: (r["caller_file"], r["call_line"]))
    return rows


def get_callees(
    conn: sqlite3.Connection,
    function_name: str,
    res_path: str | None = None,
    scope: str | None = None,
    with_source: bool = False,
) -> list[dict]:
    """Functions the given function calls, and the signal handlers it
    registers with `<signal>.connect(<handler>)` (marked `via: "connect"`)
    -- code that runs because of it either way. `with_source` adds which
    declaration each row comes from (`source_file`/`source_scope`) -- for a
    name several declare."""
    condition, params = _symbol_filter("src", function_name, res_path, scope)
    rows: list[dict] = [
        {
            "callee_file": r["res_path"], "callee_scope": r["scope"], "callee_function": r["name"],
            "call_line": r["line"], **_endpoint("source", r, with_source),
        }
        for r in conn.execute(f"""
            SELECT tgt.res_path, tgt.scope, tgt.name, c.line, src.res_path AS src_path, src.scope AS src_scope
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
            **_endpoint("source", r, with_source),
        }
        for r in conn.execute(f"""
            SELECT h.res_path, h.scope, h.name, sc.line, sc.signal_name,
                   src.res_path AS src_path, src.scope AS src_scope
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
    symbol_rows = _rows_by_id(conn, "SELECT id, res_path, name, scope, line FROM symbols WHERE id IN ({})", best)
    for sid, (hop, kind) in best.items():
        row = symbol_rows.get(sid)
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
        handler_ids = [sid for sid, hop in reached_at.items() if hop + 1 <= max_depth]
        for sc in _query_in_chunks(
            conn,
            "SELECT sc.id, sc.handler_symbol_id, sc.scene_res_path, sc.line, sc.signal_name, sc.from_node, "
            "sc.to_node, h.name AS method FROM scene_connections sc JOIN symbols h ON h.id = sc.handler_symbol_id "
            "WHERE sc.handler_symbol_id IN ({})",
            handler_ids,
        ):
            hop = reached_at[sc["handler_symbol_id"]]
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


# Ids per `IN (...)` query -- well under SQLite's bound-parameter limit.
_IN_CHUNK = 500


def _query_in_chunks(conn: sqlite3.Connection, sql: str, ids) -> list[sqlite3.Row]:
    """`sql` (with one `IN ({})` slot) run over `ids` in chunks: one query
    per few hundred ids instead of one per id."""
    ids = list(ids)
    rows: list[sqlite3.Row] = []
    for start in range(0, len(ids), _IN_CHUNK):
        chunk = ids[start:start + _IN_CHUNK]
        rows += conn.execute(sql.format(",".join("?" * len(chunk))), chunk).fetchall()
    return rows


def _rows_by_id(conn: sqlite3.Connection, sql: str, ids) -> dict[int, sqlite3.Row]:
    return {row["id"]: row for row in _query_in_chunks(conn, sql, ids)}


def call_adjacency(conn: sqlite3.Connection) -> dict[int, set[int]]:
    """Caller symbol id -> the ids it calls: the direct-call graph, loaded
    once and shared by every `find_call_path` of one request."""
    adjacency: dict[int, set[int]] = {}
    for src, tgt in _load_call_edges(conn):
        adjacency.setdefault(src, set()).add(tgt)
    return adjacency


def find_call_path(
    conn: sqlite3.Connection,
    from_symbol_id: int,
    to_symbol_id: int,
    max_depth: int = 6,
    adjacency: dict[int, set[int]] | None = None,
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

    if adjacency is None:
        adjacency = call_adjacency(conn)

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
