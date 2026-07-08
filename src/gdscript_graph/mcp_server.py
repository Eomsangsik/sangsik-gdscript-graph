from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from gdscript_graph import db as gdb


def _blank_to_none(value: str | None) -> str | None:
    # An empty string from a client that serializes an unset optional field
    # as "" rather than omitting it / sending null must still mean "no
    # filter" -- otherwise it silently matches zero rows (no symbol ever has
    # an empty res_path or scope) instead of behaving like the field was
    # never passed.
    return value if value else None


def run_server(db_path: Path) -> None:
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
    finally:
        startup_conn.close()

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

    mcp.run("stdio")
