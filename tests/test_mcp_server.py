from __future__ import annotations

import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _run_session(db_path, coro_fn, extra_args=()):
    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "gdscript_graph.cli", "mcp", str(db_path), *extra_args],
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
    assert names == {
        "search", "status", "node", "explore", "files", "callers", "callees", "signal_handlers", "impact",
    }


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


def test_status_reports_counts_and_freshness(godot_project):
    godot_project.write("x.gd", "extends Node\nsignal died\nfunc a():\n    b()\nfunc b():\n    pass\n")
    godot_project.build()
    db_path = godot_project.root.parent / "graph.db"

    result = _run_session(db_path, lambda session: session.call_tool("status", {}))
    payload = json.loads(result.content[0].text)

    assert payload["file_count"] == 1
    assert payload["parse_error_count"] == 0
    assert payload["symbol_counts"] == {"function": 2, "signal": 1}
    assert payload["resolved_calls"] == 1
    assert payload["unresolved_calls"] == 0
    assert payload["resolved_signal_connections"] == 0
    assert payload["unresolved_signal_connections"] == 0
    assert payload["project_root"] == str(godot_project.root)
    assert payload["built_at_unix"] is not None
    assert payload["seconds_since_build"] >= 0
    assert payload["watching"] is True


def test_status_watching_false_and_rebuild_pending_false_when_watch_disabled(godot_project):
    godot_project.write("x.gd", "extends Node\nfunc a():\n    pass\n")
    godot_project.build()
    db_path = godot_project.root.parent / "graph.db"

    result = _run_session(
        db_path, lambda session: session.call_tool("status", {}), extra_args=["--no-watch"]
    )
    payload = json.loads(result.content[0].text)
    assert payload["watching"] is False
    assert payload["rebuild_pending"] is False


def test_status_rebuild_pending_reflects_a_real_in_flight_debounced_rebuild(godot_project):
    """Regression test: `rebuild_pending` must actually track a live
    debounce/rebuild cycle end-to-end over the real stdio protocol -- not
    just report a hardcoded value -- verified by editing a file mid-session
    and observing the flag flip true then back to false as the real
    watcher reacts to it."""
    godot_project.write("x.gd", "extends Node\nfunc a():\n    pass\n")
    godot_project.build()
    db_path = godot_project.root.parent / "graph.db"

    async def scenario(session):
        # Let the reconcile-on-start rebuild settle before asserting a
        # clean baseline.
        for _ in range(30):
            payload = json.loads((await session.call_tool("status", {})).content[0].text)
            if not payload["rebuild_pending"]:
                break
            await asyncio.sleep(0.2)
        assert payload["rebuild_pending"] is False
        assert payload["symbol_counts"] == {"function": 1}

        godot_project.write("x.gd", "extends Node\nfunc a():\n    pass\nfunc c():\n    pass\n")

        saw_pending = False
        for _ in range(30):
            payload = json.loads((await session.call_tool("status", {})).content[0].text)
            if payload["rebuild_pending"]:
                saw_pending = True
                break
            await asyncio.sleep(0.05)
        assert saw_pending, "expected rebuild_pending to flip true while the debounced rebuild runs"

        for _ in range(30):
            payload = json.loads((await session.call_tool("status", {})).content[0].text)
            if not payload["rebuild_pending"]:
                break
            await asyncio.sleep(0.2)
        assert payload["rebuild_pending"] is False
        assert payload["symbol_counts"] == {"function": 2}

    _run_session(db_path, scenario, extra_args=["--debounce-ms", "300"])


def test_node_returns_source_and_callers_for_a_function(godot_project):
    godot_project.write("player.gd", """
extends Node

func check_death() -> void:
    pass

func take_damage() -> void:
    check_death()
""")
    godot_project.build()
    db_path = godot_project.root.parent / "graph.db"

    result = _run_session(db_path, lambda session: session.call_tool("node", {"name": "check_death"}))
    payload = json.loads(result.content[0].text)

    assert len(payload["matches"]) == 1
    assert payload["source"]["text"] == "func check_death() -> void:\n    pass\n"
    assert payload["callers"] == [
        {"caller_file": "res://player.gd", "caller_scope": None, "caller_function": "take_damage", "call_line": 8}
    ]


def test_node_returns_handlers_for_a_signal(godot_project):
    godot_project.write("player.gd", """
extends Node

signal died

func _ready() -> void:
    died.connect(_on_died)

func _on_died() -> void:
    pass
""")
    godot_project.build()
    db_path = godot_project.root.parent / "graph.db"

    result = _run_session(db_path, lambda session: session.call_tool("node", {"name": "died"}))
    payload = json.loads(result.content[0].text)

    assert payload["source"]["text"] == "signal died"
    assert len(payload["handlers"]) == 1
    assert payload["handlers"][0]["handler_function"] == "_on_died"


def test_node_ambiguous_name_returns_matches_only_no_source(godot_project):
    godot_project.write("a.gd", "extends Node\nfunc heal():\n    pass\n")
    godot_project.write("b.gd", "extends Node\nfunc heal():\n    pass\n")
    godot_project.build()
    db_path = godot_project.root.parent / "graph.db"

    result = _run_session(db_path, lambda session: session.call_tool("node", {"name": "heal"}))
    payload = json.loads(result.content[0].text)

    assert len(payload["matches"]) == 2
    assert "source" not in payload
    assert "callers" not in payload


def test_node_disambiguated_by_file_returns_full_detail(godot_project):
    godot_project.write("a.gd", "extends Node\nfunc heal():\n    pass\n")
    godot_project.write("b.gd", "extends Node\nfunc heal():\n    pass\n")
    godot_project.build()
    db_path = godot_project.root.parent / "graph.db"

    result = _run_session(
        db_path, lambda session: session.call_tool("node", {"name": "heal", "file": "res://a.gd"})
    )
    payload = json.loads(result.content[0].text)

    assert len(payload["matches"]) == 1
    assert payload["matches"][0]["res_path"] == "res://a.gd"
    assert payload["source"]["file"] == "res://a.gd"


def test_node_nonexistent_name_returns_empty_matches(godot_project):
    godot_project.write("x.gd", "extends Node\nfunc a():\n    pass\n")
    godot_project.build()
    db_path = godot_project.root.parent / "graph.db"

    result = _run_session(db_path, lambda session: session.call_tool("node", {"name": "does_not_exist"}))
    payload = json.loads(result.content[0].text)
    assert payload == {"matches": []}


def test_node_function_source_does_not_leak_into_an_interleaved_inner_class(godot_project):
    """Regression test: computing a symbol's source range must be based on
    that exact node's own parse-tree end position, not a heuristic derived
    from a neighboring symbol's start line -- the latter breaks as soon as
    an inner class (which has no line-numbered symbol of its own; only its
    members do) is interleaved between two top-level siblings, since the
    inner class's *members* line up well past where the class itself
    starts."""
    godot_project.write("thing.gd", """
extends Node

func foo():
    pass

class Inner:
    func bar():
        pass

func baz():
    pass
""")
    godot_project.build()
    db_path = godot_project.root.parent / "graph.db"

    result = _run_session(db_path, lambda session: session.call_tool("node", {"name": "foo"}))
    payload = json.loads(result.content[0].text)
    assert "class Inner" not in payload["source"]["text"]
    assert payload["source"]["text"] == "func foo():\n    pass\n"


def test_node_property_accessor_source_is_isolated_to_its_own_body(godot_project):
    godot_project.write("stats.gd", """
extends Node

var health: int = 10:
    set(value):
        health = value
        update_ui()
    get:
        return health

func update_ui() -> void:
    pass
""")
    godot_project.build()
    db_path = godot_project.root.parent / "graph.db"

    result = _run_session(db_path, lambda session: session.call_tool("node", {"name": "health.set"}))
    payload = json.loads(result.content[0].text)
    assert "func update_ui" not in payload["source"]["text"]
    assert "update_ui()" in payload["source"]["text"]
    assert "get:" not in payload["source"]["text"]


def test_files_lists_all_indexed_files_with_symbol_counts(godot_project):
    godot_project.write("player.gd", "extends Node\nsignal died\nfunc a():\n    pass\nfunc b():\n    pass\n")
    godot_project.write("enemies/goblin.gd", "extends Node\nclass_name Goblin\nfunc attack():\n    pass\n")
    godot_project.build()
    db_path = godot_project.root.parent / "graph.db"

    result = _run_session(db_path, lambda session: session.call_tool("files", {}))
    rows = result.structuredContent["result"]
    by_path = {r["res_path"]: r for r in rows}

    assert set(by_path) == {"res://player.gd", "res://enemies/goblin.gd"}
    assert by_path["res://player.gd"]["symbol_counts"] == {"function": 2, "signal": 1}
    assert by_path["res://enemies/goblin.gd"]["class_name"] == "Goblin"
    assert by_path["res://enemies/goblin.gd"]["symbol_counts"] == {"function": 1}


def test_files_prefix_narrows_to_a_subdirectory(godot_project):
    godot_project.write("player.gd", "extends Node\nfunc a():\n    pass\n")
    godot_project.write("enemies/goblin.gd", "extends Node\nfunc attack():\n    pass\n")
    godot_project.write("enemies/orc.gd", "extends Node\nfunc smash():\n    pass\n")
    godot_project.build()
    db_path = godot_project.root.parent / "graph.db"

    result = _run_session(
        db_path, lambda session: session.call_tool("files", {"prefix": "res://enemies/"})
    )
    rows = result.structuredContent["result"]
    assert {r["res_path"] for r in rows} == {"res://enemies/goblin.gd", "res://enemies/orc.gd"}


def test_files_reports_parse_error_for_unparseable_file(godot_project):
    godot_project.write("broken.gd", "func broken(\n    this is not valid gdscript !!!\n")
    godot_project.write("good.gd", "extends Node\nfunc a():\n    pass\n")
    godot_project.build()
    db_path = godot_project.root.parent / "graph.db"

    result = _run_session(db_path, lambda session: session.call_tool("files", {}))
    rows = result.structuredContent["result"]
    by_path = {r["res_path"]: r for r in rows}
    assert by_path["res://broken.gd"]["parse_error"] is not None
    assert by_path["res://good.gd"]["parse_error"] is None


def test_explore_finds_multi_hop_call_path_between_two_functions(godot_project):
    godot_project.write("player.gd", """
extends Node

func take_damage() -> void:
    apply_damage()

func apply_damage() -> void:
    check_death()

func check_death() -> void:
    pass

func unrelated() -> void:
    pass
""")
    godot_project.build()
    db_path = godot_project.root.parent / "graph.db"

    result = _run_session(
        db_path, lambda session: session.call_tool("explore", {"names": ["take_damage", "check_death"]})
    )
    payload = result.structuredContent

    assert set(payload["symbols"]) == {"take_damage", "check_death"}
    assert payload["symbols"]["take_damage"]["source"]["text"].startswith("func take_damage")
    assert payload["symbols"]["check_death"]["callers"][0]["caller_function"] == "apply_damage"

    path = payload["paths"]["take_damage -> check_death"]
    assert [n["name"] for n in path] == ["take_damage", "apply_damage", "check_death"]
    assert "check_death -> take_damage" not in payload["paths"]


def test_explore_reports_no_path_for_unrelated_functions(godot_project):
    godot_project.write("player.gd", """
extends Node

func take_damage() -> void:
    pass

func heal() -> void:
    pass
""")
    godot_project.build()
    db_path = godot_project.root.parent / "graph.db"

    result = _run_session(
        db_path, lambda session: session.call_tool("explore", {"names": ["take_damage", "heal"]})
    )
    assert result.structuredContent["paths"] == {}


def test_explore_handles_an_unresolvable_name_without_failing_the_rest(godot_project):
    godot_project.write("player.gd", "extends Node\nfunc take_damage() -> void:\n    pass\n")
    godot_project.build()
    db_path = godot_project.root.parent / "graph.db"

    result = _run_session(
        db_path, lambda session: session.call_tool("explore", {"names": ["does_not_exist", "take_damage"]})
    )
    payload = result.structuredContent
    assert payload["symbols"]["does_not_exist"] == {"matches": []}
    assert payload["symbols"]["take_damage"]["matches"][0]["name"] == "take_damage"
    assert payload["paths"] == {}


def test_explore_ambiguous_name_excluded_from_paths(godot_project):
    godot_project.write("a.gd", "extends Node\nfunc heal():\n    pass\n")
    godot_project.write("b.gd", "extends Node\nfunc heal():\n    pass\n")
    godot_project.write("c.gd", "extends Node\nfunc take_damage():\n    pass\n")
    godot_project.build()
    db_path = godot_project.root.parent / "graph.db"

    result = _run_session(
        db_path, lambda session: session.call_tool("explore", {"names": ["heal", "take_damage"]})
    )
    payload = result.structuredContent
    assert len(payload["symbols"]["heal"]["matches"]) == 2
    assert "source" not in payload["symbols"]["heal"]
    assert payload["paths"] == {}
