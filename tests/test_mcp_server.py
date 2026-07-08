from __future__ import annotations

import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _run_session(db_path, coro_fn):
    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "gdscript_graph.cli", "mcp", str(db_path)],
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await coro_fn(session)

    return asyncio.run(run())


def test_tools_list_includes_all_tools(godot_project):
    godot_project.write("x.gd", "extends Node\nfunc a():\n    pass\n")
    godot_project.build()
    db_path = godot_project.root.parent / "graph.db"

    tools = _run_session(db_path, lambda session: session.list_tools())
    names = {t.name for t in tools.tools}
    assert names == {"search", "callers", "callees", "signal_handlers", "impact"}


def test_impact_rejects_invalid_direction(godot_project):
    godot_project.write("x.gd", "extends Node\nfunc a():\n    b()\nfunc b():\n    pass\n")
    godot_project.build()
    db_path = godot_project.root.parent / "graph.db"

    result = _run_session(
        db_path,
        lambda session: session.call_tool("impact", {"function_name": "a", "direction": "bogus"}),
    )
    assert result.isError
    assert "direction" in result.content[0].text


def test_search_reports_truncation_beyond_limit(godot_project):
    lines = ["extends Node", ""]
    for i in range(25):
        lines += [f"func target_{i:02d}():", "    pass"]
    godot_project.write("x.gd", "\n".join(lines))
    godot_project.build()
    db_path = godot_project.root.parent / "graph.db"

    capped = _run_session(db_path, lambda session: session.call_tool("search", {"query": "target_"}))
    capped_payload = json.loads(capped.content[0].text)
    assert len(capped_payload["results"]) == 20
    assert capped_payload["truncated"] is True

    full = _run_session(
        db_path, lambda session: session.call_tool("search", {"query": "target_", "limit": 30})
    )
    full_payload = json.loads(full.content[0].text)
    assert len(full_payload["results"]) == 25
    assert full_payload["truncated"] is False


def test_search_populates_structured_content_like_other_tools(godot_project):
    """Regression test: `search`'s `-> dict` return annotation used to be
    too vague for FastMCP's schema builder to introspect, so it silently
    never populated `outputSchema`/`structuredContent` while every other
    tool (typed `-> list[dict]`) did -- a client reading `structuredContent`
    instead of re-parsing `content[0].text` as JSON got nothing back."""
    godot_project.write("x.gd", "extends Node\nfunc a():\n    pass\n")
    godot_project.build()
    db_path = godot_project.root.parent / "graph.db"

    tools = _run_session(db_path, lambda session: session.list_tools())
    search_tool = next(t for t in tools.tools if t.name == "search")
    assert search_tool.outputSchema is not None

    result = _run_session(db_path, lambda session: session.call_tool("search", {"query": "a"}))
    assert result.structuredContent is not None
    assert result.structuredContent["truncated"] is False


def test_mid_session_db_deletion_gives_clear_error_without_stray_file(godot_project):
    """Regression test: deleting the db file while the MCP server is still
    running (not rebuilding it, just removing it) must give the same clear
    "database not found" error as a missing db at startup, and must not
    silently recreate a stray empty file at that path -- the per-call
    reconnect had no existence check, unlike the startup check, so it
    reproduced exactly the hazard the startup check's own comment says it
    prevents, plus a much less helpful raw "no such table" error."""
    godot_project.write("x.gd", "extends Node\nfunc a():\n    pass\n")
    godot_project.build()
    db_path = godot_project.root.parent / "graph.db"

    async def scenario(session):
        first = await session.call_tool("search", {"query": "a"})
        db_path.unlink()
        second = await session.call_tool("search", {"query": "a"})
        return first, second

    first, second = _run_session(db_path, scenario)
    assert not first.isError
    assert second.isError
    assert "database not found" in second.content[0].text
    assert not db_path.exists()


def test_callees_and_signal_handlers_are_actually_invocable_over_real_session(godot_project):
    """Regression test: `test_tools_list_includes_all_tools` only confirms
    `callees`/`signal_handlers` are *registered*, not that a real
    `call_tool` against them actually works end-to-end over the stdio
    protocol -- this is exactly the class of bug `search`'s
    structuredContent regression was (a schema/serialization quirk that
    only shows up through the real protocol, not a direct db.py call)."""
    godot_project.write("x.gd", """
extends Node

signal died

func run() -> void:
    helper()
    died.connect(_on_died)

func helper() -> void:
    pass

func _on_died() -> void:
    pass
""")
    godot_project.build()
    db_path = godot_project.root.parent / "graph.db"

    callees_result = _run_session(db_path, lambda session: session.call_tool("callees", {"function_name": "run"}))
    assert not callees_result.isError
    callees_payload = json.loads(callees_result.content[0].text)
    assert callees_payload["callee_function"] == "helper"

    handlers_result = _run_session(
        db_path, lambda session: session.call_tool("signal_handlers", {"signal_name": "died"})
    )
    assert not handlers_result.isError
    handlers_payload = json.loads(handlers_result.content[0].text)
    assert handlers_payload["handler_function"] == "_on_died"


def test_callers_blank_scope_behaves_like_omitted(godot_project):
    """Regression test: a client that sends `scope=""` for an unset optional
    field must still get unfiltered results, not a silent empty match."""
    godot_project.write("x.gd", """
extends Node

class Inner:
    func setup() -> void:
        pass

    func run_inner() -> void:
        setup()

func setup() -> void:
    pass

func run_outer() -> void:
    setup()
""")
    godot_project.build()
    db_path = godot_project.root.parent / "graph.db"

    omitted = _run_session(db_path, lambda session: session.call_tool("callers", {"function_name": "setup"}))
    blank = _run_session(
        db_path, lambda session: session.call_tool("callers", {"function_name": "setup", "scope": ""})
    )
    assert len(blank.content) == len(omitted.content) == 2
