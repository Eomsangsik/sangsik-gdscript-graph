from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from gdscript_graph import db as gdb
from gdscript_graph.source_lookup import find_symbol_source
from gdscript_graph.watch import DEFAULT_DEBOUNCE_SECONDS, start_watching


def _blank_to_none(value: str | None) -> str | None:
    # An empty string from a client that serializes an unset optional field
    # as "" rather than omitting it / sending null must still mean "no
    # filter" -- otherwise it silently matches zero rows (no symbol ever has
    # an empty res_path or scope) instead of behaving like the field was
    # never passed.
    return value if value else None


def _resolve_symbol_detail(
    conn, name: str, file: str | None, scope: str | None, kind: str | None
) -> dict[str, Any]:
    """Shared by `node` and `explore`: resolve `name` to exactly one
    symbol (given the filters) and return its location, verbatim source,
    and callers (function) / handlers (signal) -- or, if still ambiguous,
    just the list of candidate locations under `matches`."""
    rows = [dict(r) for r in gdb.find_symbol_locations(conn, name, file, scope, kind)]
    if len(rows) != 1:
        return {"matches": rows}

    match = rows[0]
    project_root = gdb.get_meta(conn, "project_root")
    source = (
        find_symbol_source(project_root, match["res_path"], match["kind"], match["scope"], match["name"])
        if project_root is not None
        else None
    )
    result: dict[str, Any] = {"matches": rows, "source": source}
    if match["kind"] == "function":
        result["callers"] = [dict(r) for r in gdb.get_callers(conn, match["name"], match["res_path"], match["scope"])]
    elif match["kind"] == "signal":
        result["handlers"] = [
            dict(r) for r in gdb.get_signal_handlers(conn, match["name"], match["res_path"], match["scope"])
        ]
    return result


def run_server(db_path: Path, watch: bool = True, debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS) -> None:
    if db_path.is_dir():
        raise ValueError(f"db path is a directory, not a file: {db_path}")

    # Check existence ourselves before connecting -- sqlite3.connect()
    # silently creates an empty file at db_path if it's missing, which
    # would leave a stray file behind even though validate_schema then
    # rejects it as not a real gdscript-graph database.
    if not db_path.exists():
        raise ValueError(
            f"database not found: {db_path}. Run `gdscript-graph build <project_dir>` first."
        )

    # Fail fast with a clear message if db_path isn't a valid sqlite file,
    # or was built by an incompatible schema -- rather than erroring,
    # confusingly, on the first tool call.
    startup_conn = gdb.connect(db_path)
    try:
        gdb.validate_schema(startup_conn)
        project_root_str = gdb.get_meta(startup_conn, "project_root")
    finally:
        startup_conn.close()

    watch_handle = None
    if watch:
        # Recover the project root the db was built from out of `meta`
        # instead of requiring it as a second CLI argument that could
        # silently drift out of sync with the db's actual source directory.
        if project_root_str is None:
            print("warning: db has no recorded project root, auto-rebuild on file changes is disabled "
                  "(rebuild with a current `gdscript-graph build` to enable it)", file=sys.stderr)
        else:
            project_root = Path(project_root_str)
            if not project_root.is_dir():
                print(f"warning: recorded project root no longer exists ({project_root}), "
                      "auto-rebuild on file changes is disabled", file=sys.stderr)
            else:
                watch_handle = start_watching(project_root, db_path, debounce_seconds)

    mcp = FastMCP("gdscript-graph")

    def _query(fn):
        # Reopen the connection per call instead of holding one for the
        # server's lifetime -- otherwise rebuilding the db at the same path
        # while the server is running leaves it silently serving pre-rebuild
        # data forever (the old connection keeps reading the unlinked inode).
        # Re-check existence first, same as the startup check above and for
        # the same reason -- sqlite3.connect() on a path that's been deleted
        # mid-session (not rebuilt, just removed) would otherwise silently
        # recreate a stray empty file and fail later with a confusing raw
        # "no such table" error instead of this tool's own clear message.
        if not db_path.exists():
            raise ValueError(
                f"database not found: {db_path}. Run `gdscript-graph build <project_dir>` first."
            )
        conn = gdb.connect(db_path)
        try:
            return fn(conn)
        finally:
            conn.close()

    @mcp.tool()
    def search(query: str, limit: int = 20) -> dict[str, Any]:
        """Search GDScript symbols (functions, signals, vars, consts, enums)
        by substring match on name, ordered by name.

        Returns up to `limit` results (default 20, capped at 200) plus
        `truncated: true` if more matches exist beyond `limit` -- raise
        `limit` or narrow `query` to see the rest instead of assuming the
        result set is exhaustive."""
        capped_limit = max(1, min(limit, 200))

        def run(conn):
            rows = gdb.search_symbols(conn, query, limit=capped_limit + 1)
            return {
                "results": [dict(r) for r in rows[:capped_limit]],
                "truncated": len(rows) > capped_limit,
            }

        return _query(run)

    @mcp.tool()
    def status() -> dict[str, Any]:
        """Report the index's health and freshness: file/symbol/call/signal
        counts, when it was last built, and (if the file watcher is
        enabled) whether a rebuild is currently pending or in flight --
        useful for telling "just edited, hasn't caught up yet" apart from
        a genuinely stale index before trusting a query result."""

        def run(conn):
            file_count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            parse_error_count = conn.execute(
                "SELECT COUNT(*) FROM files WHERE parse_error IS NOT NULL"
            ).fetchone()[0]
            symbol_counts = {
                row["kind"]: row["n"]
                for row in conn.execute("SELECT kind, COUNT(*) AS n FROM symbols GROUP BY kind")
            }
            built_at_str = gdb.get_meta(conn, "built_at")
            built_at = float(built_at_str) if built_at_str is not None else None
            return {
                "project_root": gdb.get_meta(conn, "project_root"),
                "built_at_unix": built_at,
                "seconds_since_build": (time.time() - built_at) if built_at is not None else None,
                "file_count": file_count,
                "parse_error_count": parse_error_count,
                "symbol_counts": symbol_counts,
                "resolved_calls": conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0],
                "unresolved_calls": conn.execute("SELECT COUNT(*) FROM unresolved_calls").fetchone()[0],
                "resolved_signal_connections": conn.execute(
                    "SELECT COUNT(*) FROM signal_connections"
                ).fetchone()[0],
                "unresolved_signal_connections": conn.execute(
                    "SELECT COUNT(*) FROM unresolved_connections"
                ).fetchone()[0],
                "watching": watch_handle is not None,
                "rebuild_pending": watch_handle.is_pending() if watch_handle is not None else False,
            }

        return _query(run)

    @mcp.tool()
    def node(name: str, file: str | None = None, scope: str | None = None, kind: str | None = None) -> dict[str, Any]:
        """Look up a symbol (function, signal, var, const, or enum) by name
        and return its verbatim source -- re-read fresh from disk, not just
        whatever the last build captured, since a build can lag a live edit
        by up to the watcher's debounce window -- plus its callers (for a
        function) or handlers (for a signal). Collapses what would
        otherwise be `search` + reading the file + `callers`/
        `signal_handlers` into one call.

        Pass `file` (a res:// path), `scope` (the enclosing inner class
        name), and/or `kind` (`function`/`signal`/`var`/`const`/`enum`) to
        disambiguate when multiple declarations share a name. If the name
        is still ambiguous after filtering, only the list of matching
        locations is returned under `matches` (like `search`) -- narrow
        with `file`/`scope`/`kind` and call again for the source/callers
        detail. `source` is `None` if the file can't be re-read/re-parsed
        or the symbol can no longer be found in it (e.g. renamed since the
        last build)."""
        file, scope, kind = _blank_to_none(file), _blank_to_none(scope), _blank_to_none(kind)
        return _query(lambda conn: _resolve_symbol_detail(conn, name, file, scope, kind))

    @mcp.tool()
    def explore(names: list[str], max_path_depth: int = 6) -> dict[str, Any]:
        """Given several symbol names, return each one's location(s) +
        verbatim source + callers/handlers (the same detail `node` gives
        for one name), plus the actual function-call path connecting each
        resolved pair of functions (checked in both directions), when one
        exists within `max_path_depth` hops -- collapsing what would
        otherwise be several `node` calls plus manually tracing `callees`
        by hand into one call. Useful for "how does A reach B" or "show me
        how these N functions relate" in a single request.

        Each name is resolved independently, with no shared `file`/`scope`
        filter across the list -- a name that's still ambiguous just gets
        its `matches` list back (like `node`) and doesn't participate in
        `paths`; disambiguate it with a separate `node` call (passing
        `file`/`scope`) and pass the now-unique name again.

        Only direct function-call edges are followed for `paths` -- a
        connection that exists only via a signal `.connect()` handler (no
        direct call edge) won't appear there; check the signal's own
        `handlers` (or `signal_handlers`) for that side of the graph
        instead. Not exhaustive for the same reasons as `callers`/
        `callees`: a call through an untyped variable or a class-level
        field isn't tracked, so a path through one can't be found either.
        """

        def run(conn):
            symbols: dict[str, Any] = {}
            resolved: dict[str, dict] = {}
            for name in names:
                detail = _resolve_symbol_detail(conn, name, None, None, None)
                symbols[name] = detail
                if len(detail["matches"]) == 1 and detail["matches"][0]["kind"] == "function":
                    resolved[name] = detail["matches"][0]

            paths: dict[str, list[dict]] = {}
            resolved_names = list(resolved)
            for i, a in enumerate(resolved_names):
                for b in resolved_names[i + 1:]:
                    path_ab = gdb.find_call_path(conn, resolved[a]["id"], resolved[b]["id"], max_path_depth)
                    if path_ab:
                        paths[f"{a} -> {b}"] = path_ab
                    path_ba = gdb.find_call_path(conn, resolved[b]["id"], resolved[a]["id"], max_path_depth)
                    if path_ba:
                        paths[f"{b} -> {a}"] = path_ba

            return {"symbols": symbols, "paths": paths}

        return _query(run)

    @mcp.tool()
    def files(prefix: str | None = None) -> list[dict]:
        """List indexed files with their `class_name`/`extends` (if any),
        `parse_error` (if the file failed to parse), and a breakdown of
        symbol counts by kind (function/signal/var/const/enum) -- a
        project-structure overview without walking the filesystem.

        Pass `prefix` (a res:// path, e.g. `res://enemies/` or
        `res://player.gd`) to narrow to a subdirectory or a single file."""
        prefix = _blank_to_none(prefix)
        return _query(lambda conn: gdb.list_files(conn, prefix))

    @mcp.tool()
    def callers(function_name: str, file: str | None = None, scope: str | None = None) -> list[dict]:
        """List call sites that call the given function name.

        Pass `file` (a res:// path) and/or `scope` (the enclosing inner
        class name) to disambiguate when multiple declarations share a name.
        An empty or omitted `scope` means "no scope filter" (matches every
        scope, not just top-level) -- there's no way to explicitly request
        only the top-level declaration when it collides with an inner
        class's same-named one.

        Not exhaustive: a call through an untyped variable, one typed with
        an engine/built-in class (only a project-declared `class_name` type
        is tracked), or a class-level field (e.g. `@onready var x: T =
        $Node`) isn't tracked. `super.` is only tracked from a top-level
        (not inner-class) caller. An empty result can mean "no callers" or
        "callers exist but through an untracked receiver kind".
        """
        file, scope = _blank_to_none(file), _blank_to_none(scope)
        return _query(lambda conn: [dict(r) for r in gdb.get_callers(conn, function_name, file, scope)])

    @mcp.tool()
    def callees(function_name: str, file: str | None = None, scope: str | None = None) -> list[dict]:
        """List functions called from within the given function.

        Pass `file` (a res:// path) and/or `scope` (the enclosing inner
        class name) to disambiguate when multiple declarations share a name.
        An empty or omitted `scope` means "no scope filter" (matches every
        scope, not just top-level) -- there's no way to explicitly request
        only the top-level declaration when it collides with an inner
        class's same-named one.

        Not exhaustive: a call through an untyped variable, one typed with
        an engine/built-in class (only a project-declared `class_name` type
        is tracked), or a class-level field (e.g. `@onready var x: T =
        $Node`) isn't tracked. `super.` is only tracked from a top-level
        (not inner-class) caller."""
        file, scope = _blank_to_none(file), _blank_to_none(scope)
        return _query(lambda conn: [dict(r) for r in gdb.get_callees(conn, function_name, file, scope)])

    @mcp.tool()
    def signal_handlers(signal_name: str, file: str | None = None, scope: str | None = None) -> list[dict]:
        """List functions connected as handlers for the given signal via
        `.connect(...)`. Pass `file` (a res:// path) and/or `scope` (the
        signal's enclosing inner class name) to disambiguate when multiple
        declarations share a name. Each result includes `signal_scope` and
        `handler_scope` so same-named signals/handlers in different scopes
        are always distinguishable, even without filtering. An empty or
        omitted `scope` means "no scope filter" (matches every scope, not
        just top-level) -- there's no way to explicitly request only the
        top-level declaration when it collides with an inner class's
        same-named one.

        Both the signal side and the handler side of `.connect(...)` are
        tracked the same way: a bare/self reference (including one
        inherited from an ancestor class), an autoload, or a receiver typed
        as a project-declared `class_name` (walking its inheritance chain,
        top-level only) -- e.g. `GameManager.card_drawn.connect(handler)`
        and `unit.died.connect(handler)` both resolve. Not exhaustive: a
        reference through an untyped variable, one typed with an engine/
        built-in class, or a class-level field (e.g. `@onready var x: T =
        $Node`) isn't tracked on either side."""
        file, scope = _blank_to_none(file), _blank_to_none(scope)
        return _query(lambda conn: [dict(r) for r in gdb.get_signal_handlers(conn, signal_name, file, scope)])

    @mcp.tool()
    def impact(
        function_name: str,
        file: str | None = None,
        scope: str | None = None,
        direction: str = "callers",
        max_depth: int = 5,
    ) -> list[dict]:
        """Transitively walk the call graph from a function to find
        everything that could be affected by changing it.

        `direction="callers"` walks who (transitively) calls this function
        -- useful for "what breaks if I change this signature". Pass
        `direction="callees"` to instead walk what this function
        (transitively) calls. Each result includes `depth`, the number of
        hops from the seed function. Pass `file`/`scope` to disambiguate
        when multiple declarations share a name. An empty or omitted
        `scope` means "no scope filter" (matches every scope, not just
        top-level) -- there's no way to explicitly request only the
        top-level declaration when it collides with an inner class's
        same-named one.

        Not exhaustive, for the same reason as `callers`/`callees`: a call
        through an untyped variable or a class-level field isn't tracked,
        so the walk can't follow it either.
        """
        if direction not in ("callers", "callees"):
            raise ValueError(f"direction must be 'callers' or 'callees', got {direction!r}")
        file, scope = _blank_to_none(file), _blank_to_none(scope)
        if direction == "callees":
            return _query(lambda conn: gdb.get_callees_transitive(conn, function_name, file, scope, max_depth))
        return _query(lambda conn: gdb.get_callers_transitive(conn, function_name, file, scope, max_depth))

    try:
        mcp.run("stdio")
    finally:
        if watch_handle is not None:
            watch_handle.stop()
