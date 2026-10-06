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


# Results a list-returning tool sends per call unless asked for more. A
# common name can have thousands of call sites, and an MCP client truncates
# (Claude Code: ~25k tokens) or rejects a response that size outright.
DEFAULT_LIMIT = 50
MAX_LIMIT = 500
# Callers inlined by `node` / per symbol by `explore`, and candidate
# locations listed for an ambiguous name.
NODE_CALLERS_LIMIT = 20
EXPLORE_CALLERS_LIMIT = 10
MATCHES_LIMIT = 50

SERVER_INSTRUCTIONS = """\
A function-level call graph of a Godot project's GDScript, from a database
rebuilt automatically as files change.

- Tools listing many results (callers, callees, signal_handlers, impact,
  files) return one page -- `{total, next_offset, by_file}`, 50 entries by
  default -- grouped by file. Pass `offset=next_offset` for the next page,
  or narrow with `file`/`scope`/`prefix`, rather than raising `limit`.
- A result with `stale: true` was answered while a rebuild was pending or
  running, i.e. files changed after the database was built; if the answer
  matters, ask again once `status` shows `rebuild_pending: false`.
"""

_CALLER_KEYS = {"caller_function": "function", "caller_scope": "scope", "call_line": "line"}
_CALLEE_KEYS = {"callee_function": "function", "callee_scope": "scope", "call_line": "line"}
_HANDLER_KEYS = {"handler_function": "function", "handler_scope": "scope", "connect_line": "line"}


def _page(
    rows: list[dict], limit: int, offset: int, group_by: str, rename: dict[str, str] | None = None
) -> dict[str, Any]:
    """One page of `rows` (`offset`, then up to `limit`), grouped by each
    row's `group_by` value under `by_file` -- so a file path shared by
    hundreds of rows is sent once -- with keys renamed per `rename` and
    None values left out. `total` counts every row; `next_offset` is the
    `offset` of the next page, None on the last one."""
    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, offset)
    page = rows[offset:offset + limit]
    by_file: dict[str, list[dict]] = {}
    for row in page:
        item = {
            (rename or {}).get(key, key): value
            for key, value in row.items()
            if key != group_by and value is not None
        }
        by_file.setdefault(row[group_by], []).append(item)
    end = offset + len(page)
    return {"total": len(rows), "next_offset": end if end < len(rows) else None, "by_file": by_file}


def _resolve_symbol_detail(
    conn, name: str, file: str | None, scope: str | None, kind: str | None, callers_limit: int
) -> dict[str, Any]:
    """Shared by `node` and `explore`: resolve `name` to exactly one
    symbol (given the filters) and return its location, verbatim source,
    and the first `callers_limit` callers (function) / handlers (signal) --
    or, if still ambiguous, just the candidate locations under `matches`
    (the first `MATCHES_LIMIT`, plus `total_matches` when there are more)."""
    rows = [dict(r) for r in gdb.find_symbol_locations(conn, name, file, scope, kind)]
    if len(rows) != 1:
        result: dict[str, Any] = {"matches": rows[:MATCHES_LIMIT]}
        if len(rows) > MATCHES_LIMIT:
            result["total_matches"] = len(rows)
        return result

    match = rows[0]
    project_root = gdb.get_meta(conn, "project_root")
    source = (
        find_symbol_source(project_root, match["res_path"], match["kind"], match["scope"], match["name"])
        if project_root is not None
        else None
    )
    result = {"matches": rows, "source": source}
    if match["kind"] == "function":
        callers = gdb.get_callers(conn, match["name"], match["res_path"], match["scope"])
        result["callers"] = _page(callers, callers_limit, 0, "caller_file", _CALLER_KEYS)
    elif match["kind"] == "signal":
        handlers = gdb.get_signal_handlers(conn, match["name"], match["res_path"], match["scope"])
        result["handlers"] = _page(handlers, callers_limit, 0, "handler_file", _HANDLER_KEYS)
    return result


def _function_declarations(conn, name: str, file: str | None, scope: str | None) -> list[dict]:
    return [dict(r) for r in gdb.find_symbol_locations(conn, name, file, scope, "function")]


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

    mcp = FastMCP("gdscript-graph", instructions=SERVER_INSTRUCTIONS)

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
            result = fn(conn)
        finally:
            conn.close()
        # A rebuild counting down or running (here, or in the server that
        # rebuilds for this one) means files changed since this db was
        # built: say so on every result, not just in `status`.
        if watch_handle is not None and isinstance(result, dict) and watch_handle.is_pending():
            result["stale"] = True
        return result

    @mcp.tool()
    def search(
        query: str, limit: int = 20, kind: str | None = None, path: str | None = None
    ) -> dict[str, Any]:
        """Search GDScript symbols (functions, signals, vars, consts, enums)
        by case-insensitive substring match on name, best matches first: an
        exact name, then the same name in another case, then names starting
        with `query`, then the rest (shorter names first within each).

        Pass `kind` (`function`/`signal`/`var`/`const`/`enum`) and/or `path`
        (a res:// prefix, e.g. `res://features/battle/`) to narrow.
        Returns up to `limit` results (default 20, capped at 200), `total`
        matches, and `truncated: true` if there are more than `limit` --
        narrow `query`/`kind`/`path` (or raise `limit`) to see the rest
        instead of assuming the result set is exhaustive."""
        capped_limit = max(1, min(limit, 200))
        kind, path = _blank_to_none(kind), _blank_to_none(path)

        def run(conn):
            rows, total = gdb.search_symbols(conn, query, capped_limit, kind, path)
            return {"results": [dict(r) for r in rows], "total": total, "truncated": total > len(rows)}

        return _query(run)

    @mcp.tool()
    def status() -> dict[str, Any]:
        """Report the index's health and freshness: file/symbol/call/signal
        counts, when it was last built, and (if the file watcher is
        enabled) whether a rebuild is currently pending or in flight --
        useful for telling "just edited, hasn't caught up yet" apart from
        a genuinely stale index before trusting a query result.
        `unresolved_calls` counts only calls that may be into project code
        the graph couldn't follow (e.g. through an untyped variable);
        `engine_calls` counts calls into the engine or GDScript itself
        (`int()`, `queue_free()`, `some_dict.has()`), which never resolve
        to project code. `unresolved_calls_by_reason` breaks both down.
        `watch_role` is "leader" for the one server that watches and
        rebuilds this db, "follower" for any other one sharing it."""

        def run(conn):
            file_count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            parse_error_count = conn.execute(
                "SELECT COUNT(*) FROM files WHERE parse_error IS NOT NULL"
            ).fetchone()[0]
            symbol_counts = {
                row["kind"]: row["n"]
                for row in conn.execute("SELECT kind, COUNT(*) AS n FROM symbols GROUP BY kind")
            }
            unresolved_by_reason = {
                row["reason"]: row["n"]
                for row in conn.execute("SELECT reason, COUNT(*) AS n FROM unresolved_calls GROUP BY reason")
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
                # Calls the graph may have failed to follow into project code;
                # calls into the engine/GDScript built-ins are counted apart.
                "unresolved_calls": sum(
                    n for reason, n in unresolved_by_reason.items() if reason in gdb.POSSIBLY_MISSED_REASONS
                ),
                "engine_calls": sum(
                    n for reason, n in unresolved_by_reason.items() if reason not in gdb.POSSIBLY_MISSED_REASONS
                ),
                "unresolved_calls_by_reason": unresolved_by_reason,
                "resolved_signal_connections": conn.execute(
                    "SELECT COUNT(*) FROM signal_connections"
                ).fetchone()[0],
                "unresolved_signal_connections": conn.execute(
                    "SELECT COUNT(*) FROM unresolved_connections"
                ).fetchone()[0],
                "resolved_scene_connections": conn.execute(
                    "SELECT COUNT(*) FROM scene_connections"
                ).fetchone()[0],
                "unresolved_scene_connections": conn.execute(
                    "SELECT COUNT(*) FROM unresolved_scene_connections"
                ).fetchone()[0],
                "watching": watch_handle is not None,
                # Only one server per db watches and rebuilds; the others
                # ("follower") serve its db and take over when it exits.
                "watch_role": (
                    None if watch_handle is None else "leader" if watch_handle.is_leader else "follower"
                ),
                "rebuild_pending": watch_handle.is_pending() if watch_handle is not None else False,
            }

        return _query(run)

    @mcp.tool()
    def node(
        name: str,
        file: str | None = None,
        scope: str | None = None,
        kind: str | None = None,
        callers_limit: int = NODE_CALLERS_LIMIT,
    ) -> dict[str, Any]:
        """Look up a symbol (function, signal, var, const, or enum) by name
        and return its verbatim source -- re-read fresh from disk, not just
        whatever the last build captured, since a build can lag a live edit
        by up to the watcher's debounce window -- plus its callers (for a
        function) or handlers (for a signal). Collapses what would
        otherwise be `search` + reading the file + `callers`/
        `signal_handlers` into one call.

        Callers/handlers come as `{total, next_offset, by_file}` like the
        `callers` tool, limited to the first `callers_limit` (default 20);
        page through the rest with `callers(..., offset=next_offset)`.

        Pass `file` (a res:// path), `scope` (the enclosing inner class
        name), and/or `kind` (`function`/`signal`/`var`/`const`/`enum`) to
        disambiguate when multiple declarations share a name. If the name
        is still ambiguous after filtering, only the list of matching
        locations is returned under `matches` (like `search`; at most 50,
        with `total_matches` when there are more) -- narrow with
        `file`/`scope`/`kind` and call again for the source/callers
        detail. `source` is `None` if the file can't be re-read/re-parsed
        or the symbol can no longer be found in it (e.g. renamed since the
        last build)."""
        file, scope, kind = _blank_to_none(file), _blank_to_none(scope), _blank_to_none(kind)
        return _query(lambda conn: _resolve_symbol_detail(conn, name, file, scope, kind, callers_limit))

    @mcp.tool()
    def explore(
        names: list[str], max_path_depth: int = 6, callers_limit: int = EXPLORE_CALLERS_LIMIT
    ) -> dict[str, Any]:
        """Given several symbol names, return each one's location(s) +
        verbatim source + callers/handlers (the same detail `node` gives
        for one name), plus the actual function-call path connecting each
        resolved pair of functions (checked in both directions), when one
        exists within `max_path_depth` hops -- collapsing what would
        otherwise be several `node` calls plus manually tracing `callees`
        by hand into one call. Useful for "how does A reach B" or "show me
        how these N functions relate" in a single request. A function's
        `callers` include its signal-handler registrations, exactly as the
        `callers` tool reports them -- the first `callers_limit` (default
        10) of them, with `total`; get the rest from `callers`.

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
        `callees`: a call through an untyped variable isn't tracked, so a
        path through one can't be found either.
        """

        def run(conn):
            symbols: dict[str, Any] = {}
            resolved: dict[str, dict] = {}
            for name in names:
                detail = _resolve_symbol_detail(conn, name, None, None, None, callers_limit)
                symbols[name] = detail
                if len(detail["matches"]) == 1 and detail["matches"][0]["kind"] == "function":
                    resolved[name] = detail["matches"][0]

            paths: dict[str, list[dict]] = {}
            resolved_names = list(resolved)
            # The call graph is read once for every pair, not once per pair.
            adjacency = gdb.call_adjacency(conn) if len(resolved_names) > 1 else {}
            for i, a in enumerate(resolved_names):
                for b in resolved_names[i + 1:]:
                    path_ab = gdb.find_call_path(
                        conn, resolved[a]["id"], resolved[b]["id"], max_path_depth, adjacency
                    )
                    if path_ab:
                        paths[f"{a} -> {b}"] = path_ab
                    path_ba = gdb.find_call_path(
                        conn, resolved[b]["id"], resolved[a]["id"], max_path_depth, adjacency
                    )
                    if path_ba:
                        paths[f"{b} -> {a}"] = path_ba

            return {"symbols": symbols, "paths": paths}

        return _query(run)

    @mcp.tool()
    def files(prefix: str | None = None, limit: int = DEFAULT_LIMIT, offset: int = 0) -> dict[str, Any]:
        """List indexed files with their `class_name`/`extends` (if any),
        `parse_error` (if the file failed to parse), and a breakdown of
        symbol counts by kind (function/signal/var/const/enum) -- a
        project-structure overview without walking the filesystem.

        Pass `prefix` (a res:// path, e.g. `res://enemies/` or
        `res://player.gd`) to narrow to a subdirectory or a single file.
        Returns `{total, next_offset, files}`: up to `limit` files (default
        50, max 500) from `offset` on. When that isn't all of them,
        `directories` also counts the files in each subdirectory directly
        under `prefix` -- narrow `prefix` to one of those rather than
        paging through everything."""
        prefix = _blank_to_none(prefix)

        def run(conn):
            rows = gdb.list_files(conn, prefix)
            page = _page(rows, limit, offset, "res_path")
            result: dict[str, Any] = {
                "total": page["total"],
                "next_offset": page["next_offset"],
                "files": [{"res_path": path, **items[0]} for path, items in page["by_file"].items()],
            }
            if len(result["files"]) < len(rows):
                base = prefix if prefix is not None and prefix.endswith("/") else "res://"
                directories: dict[str, int] = {}
                for row in rows:
                    rest = row["res_path"].removeprefix(base)
                    if "/" in rest:
                        directory = base + rest.split("/", 1)[0] + "/"
                        directories[directory] = directories.get(directory, 0) + 1
                result["directories"] = directories
            return result

        return _query(run)

    @mcp.tool()
    def callers(
        function_name: str,
        file: str | None = None,
        scope: str | None = None,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List everything that makes the given function run: its direct
        call sites, plus -- marked with `via` -- its registrations as a
        signal handler: `"connect"` (with `.connect()` in code; the caller is
        the function containing the `.connect()`, plus `signal`) or
        `"scene"` (connected in a .tscn scene in the Godot editor; the
        caller is the scene file, with no `function`, plus
        `signal`/`from_node`/`to_node`). Changing a handler's signature
        breaks its `connect`/`scene` entries just like a call site.

        Returns `{total, next_offset, by_file}`: `by_file` maps each caller
        file to its entries (`function`, `scope` if in an inner class,
        `line`), up to `limit` entries (default 50, max 500) starting at
        `offset` -- pass `offset=next_offset` for the next page.

        Pass `file` (a res:// path) and/or `scope` (the enclosing inner
        class name) to disambiguate when multiple declarations share a name.
        If several still do, the results for all of them are merged, each
        entry naming the one it reaches (`target_file`, `target_scope`), and
        `declarations` says how many there are.
        An empty or omitted `scope` means "no scope filter" (matches every
        scope, not just top-level) -- there's no way to explicitly request
        only the top-level declaration when it collides with an inner
        class's same-named one.

        Not exhaustive: calls are tracked through typed locals, params,
        member vars (including `:= ... as T` / `:= T.new()` / `:= f()` with
        a `-> T` return type, and chains like `self.hud.menu.f()`), call
        results (`make().f()`), autoloads, `class_name`s and `preload`
        consts -- but not through an untyped variable (`var x = ...`), an
        engine/built-in-typed one, a function with no declared return type,
        or Godot 3's string-based `connect("signal", obj, "method")`. An
        empty result can mean "no callers" or "callers exist but through an
        untracked receiver kind"; `status` counts the latter."""
        file, scope = _blank_to_none(file), _blank_to_none(scope)

        def run(conn):
            declarations = len(_function_declarations(conn, function_name, file, scope))
            rows = gdb.get_callers(conn, function_name, file, scope, with_target=declarations > 1)
            result = _page(rows, limit, offset, "caller_file", _CALLER_KEYS)
            if declarations > 1:
                result["declarations"] = declarations
            return result

        return _query(run)

    @mcp.tool()
    def callees(
        function_name: str,
        file: str | None = None,
        scope: str | None = None,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List functions the given function calls, plus the signal
        handlers it registers with `.connect()` (marked `via: "connect"`,
        with `signal`).

        Returns `{total, next_offset, by_file}` grouped by callee file,
        paged with `limit`/`offset` exactly like `callers`.

        Pass `file` (a res:// path) and/or `scope` (the enclosing inner
        class name) to disambiguate when multiple declarations share a name.
        If several still do, the results for all of them are merged, each
        entry naming the one it comes from (`source_file`, `source_scope`),
        and `declarations` says how many there are.
        An empty or omitted `scope` means "no scope filter" (matches every
        scope, not just top-level) -- there's no way to explicitly request
        only the top-level declaration when it collides with an inner
        class's same-named one.

        Not exhaustive, for the same reasons as `callers`."""
        file, scope = _blank_to_none(file), _blank_to_none(scope)

        def run(conn):
            declarations = len(_function_declarations(conn, function_name, file, scope))
            rows = gdb.get_callees(conn, function_name, file, scope, with_source=declarations > 1)
            result = _page(rows, limit, offset, "callee_file", _CALLEE_KEYS)
            if declarations > 1:
                result["declarations"] = declarations
            return result

        return _query(run)

    @mcp.tool()
    def signal_handlers(
        signal_name: str,
        file: str | None = None,
        scope: str | None = None,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List functions connected as handlers for the given signal, with
        `.connect(...)` in code or in a .tscn scene in the Godot editor
        (marked `via: "scene"`, plus `from_node`/`to_node`); `connected_in`
        is the file the connection is made in. Without
        `file`/`scope`, connections to an engine built-in signal of
        this name (e.g. `pressed`) are included too, with no
        `signal_file`. Pass `file` (a res:// path) and/or `scope` (the
        signal's enclosing inner class name) to disambiguate when multiple
        declarations share a name. Each entry includes `signal_file` and
        `signal_scope` (when set) so same-named signals in different scopes
        are always distinguishable, even without filtering. An empty or
        omitted `scope` means "no scope filter" (matches every scope, not
        just top-level) -- there's no way to explicitly request only the
        top-level declaration when it collides with an inner class's
        same-named one.

        Returns `{total, next_offset, by_file}` grouped by handler file
        (entries: `function`, `scope` if in an inner class, `line` of the
        connection), paged with `limit`/`offset` exactly like `callers`.

        Both the signal side and the handler side of `.connect(...)` are
        tracked the same way as a call receiver (see `callers`): a
        bare/self reference (including one inherited from an ancestor
        class), or a typed local/member/autoload/`class_name` receiver --
        e.g. `GameManager.card_drawn.connect(handler)`,
        `menu.closed.connect(hud.refresh)` both resolve. Not exhaustive, for
        the same reasons as `callers`."""
        file, scope = _blank_to_none(file), _blank_to_none(scope)
        return _query(lambda conn: _page(
            gdb.get_signal_handlers(conn, signal_name, file, scope), limit, offset, "handler_file", _HANDLER_KEYS,
        ))

    @mcp.tool()
    def impact(
        function_name: str,
        file: str | None = None,
        scope: str | None = None,
        direction: str = "callers",
        max_depth: int = 5,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Transitively walk the call graph from a function to find
        everything that could be affected by changing it.

        `direction="callers"` walks who (transitively) calls this function
        -- useful for "what breaks if I change this signature". Pass
        `direction="callees"` to instead walk what this function
        (transitively) calls. Signal-handler registrations count as edges
        too, like in `callers`/`callees`. Each result includes `depth`, the
        number of hops from the seed function; one reached through a
        signal registration rather than a direct call is marked `via`:
        `"connect"`, or -- callers direction only -- `"scene"`, a .tscn
        connection into a function in the result, with no `name` and
        `signal`/`from_node`/`to_node`/`method` instead (a dead end:
        nothing calls a scene).

        Returns `{total, next_offset, by_file}`, nearest first, paged with
        `limit`/`offset` exactly like `callers`. If the name is declared in
        more than one file and `file` doesn't pick one, nothing is walked:
        the result is `{ambiguous: true, total, next_offset, by_file}` with
        the candidate declarations instead -- call again with `file` (and
        `scope`) set to the one you mean. (A walk merged across every
        same-named declaration, e.g. each script's `_ready`, says nothing
        useful about any one of them.) An empty or omitted `scope` means
        "no scope filter" (matches every scope, not just top-level) --
        there's no way to explicitly request only the top-level declaration
        when it collides with an inner class's same-named one.

        Not exhaustive, for the same reason as `callers`/`callees`: a call
        through an untyped variable isn't tracked, so the walk can't follow
        it either.
        """
        if direction not in ("callers", "callees"):
            raise ValueError(f"direction must be 'callers' or 'callees', got {direction!r}")
        file, scope = _blank_to_none(file), _blank_to_none(scope)

        def run(conn):
            declarations = _function_declarations(conn, function_name, file, scope)
            if len({d["res_path"] for d in declarations}) > 1:
                candidates = [
                    {"res_path": d["res_path"], "scope": d["scope"], "line": d["line"]} for d in declarations
                ]
                return {"ambiguous": True, **_page(candidates, limit, offset, "res_path")}
            walk = gdb.get_callees_transitive if direction == "callees" else gdb.get_callers_transitive
            return _page(walk(conn, function_name, file, scope, max_depth), limit, offset, "res_path")

        return _query(run)

    try:
        mcp.run("stdio")
    finally:
        if watch_handle is not None:
            watch_handle.stop()
