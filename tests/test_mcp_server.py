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
    assert [e["function"] for e in callees_payload["by_file"]["res://x.gd"]] == ["helper", "_on_died"]

    handlers_result = _run_session(
        db_path, lambda session: session.call_tool("signal_handlers", {"signal_name": "died"})
    )
    assert not handlers_result.isError
    handlers_payload = json.loads(handlers_result.content[0].text)
    assert handlers_payload["by_file"]["res://x.gd"][0]["function"] == "_on_died"


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

    # No watcher: a rebuild it schedules (macOS can replay the writes just
    # made as live events) would mark one of the two results `stale`.
    omitted = _run_session(
        db_path, lambda session: session.call_tool("callers", {"function_name": "setup"}), ["--no-watch"]
    )
    blank = _run_session(
        db_path, lambda session: session.call_tool("callers", {"function_name": "setup", "scope": ""}), ["--no-watch"]
    )
    assert json.loads(blank.content[0].text) == json.loads(omitted.content[0].text)
    assert json.loads(omitted.content[0].text)["total"] == 2


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
    assert payload["resolved_scene_connections"] == 0
    assert payload["unresolved_scene_connections"] == 0
    assert payload["project_root"] == str(godot_project.root)
    assert payload["built_at_unix"] is not None
    assert payload["seconds_since_build"] >= 0
    assert payload["watching"] is True


def test_two_servers_on_one_db_elect_a_single_rebuilding_leader(godot_project):
    """Regression test: two editor sessions on one project start two
    servers on the same db -- only one may watch and rebuild."""
    godot_project.write("x.gd", "extends Node\nfunc a():\n    pass\n")
    godot_project.build()
    db_path = godot_project.root.parent / "graph.db"

    async def run():
        params = StdioServerParameters(command=sys.executable, args=["-m", "gdscript_graph.cli", "mcp", str(db_path)])
        roles = []
        async with stdio_client(params) as (read1, write1), ClientSession(read1, write1) as first:
            await first.initialize()
            roles.append(json.loads((await first.call_tool("status", {})).content[0].text)["watch_role"])
            async with stdio_client(params) as (read2, write2), ClientSession(read2, write2) as second:
                await second.initialize()
                roles.append(json.loads((await second.call_tool("status", {})).content[0].text)["watch_role"])
        return roles

    assert asyncio.run(run()) == ["leader", "follower"]


def test_status_watching_false_and_rebuild_pending_false_when_watch_disabled(godot_project):
    godot_project.write("x.gd", "extends Node\nfunc a():\n    pass\n")
    godot_project.build()
    db_path = godot_project.root.parent / "graph.db"

    result = _run_session(
        db_path, lambda session: session.call_tool("status", {}), extra_args=["--no-watch"]
    )
    payload = json.loads(result.content[0].text)
    assert payload["watching"] is False
    assert payload["watch_role"] is None
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
    assert payload["callers"] == {
        "total": 1, "next_offset": None,
        "by_file": {"res://player.gd": [{"function": "take_damage", "line": 8}]},
    }


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
    assert payload["handlers"]["total"] == 1
    assert payload["handlers"]["by_file"]["res://player.gd"][0]["function"] == "_on_died"


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

    result = _run_session(
        db_path, lambda session: session.call_tool("node", {"name": "does_not_exist"}), ["--no-watch"]
    )
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
    rows = result.structuredContent["files"]
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
    rows = result.structuredContent["files"]
    assert {r["res_path"] for r in rows} == {"res://enemies/goblin.gd", "res://enemies/orc.gd"}


def test_files_reports_parse_error_for_unparseable_file(godot_project):
    godot_project.write("broken.gd", "func broken(\n    this is not valid gdscript !!!\n")
    godot_project.write("good.gd", "extends Node\nfunc a():\n    pass\n")
    godot_project.build()
    db_path = godot_project.root.parent / "graph.db"

    result = _run_session(db_path, lambda session: session.call_tool("files", {}))
    rows = result.structuredContent["files"]
    by_path = {r["res_path"]: r for r in rows}
    assert by_path["res://broken.gd"]["parse_error"] is not None
    assert "parse_error" not in by_path["res://good.gd"]


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
    assert payload["symbols"]["check_death"]["callers"]["by_file"]["res://player.gd"] == [
        {"function": "apply_damage", "line": 8}
    ]

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


def test_callers_and_impact_report_scene_connections_over_real_session(godot_project):
    """Regression test: a handler connected only in a .tscn must show up
    as a caller (and in `impact`) through the real stdio protocol --
    including the null `caller_function`/`name` fields a scene entry has."""
    godot_project.write("menu.gd", "extends Control\nfunc _on_resume_pressed():\n    pass\n")
    godot_project.write("menu.tscn", """[gd_scene format=3]

[ext_resource type="Script" path="res://menu.gd" id="1"]

[node name="Menu" type="Control"]
script = ExtResource("1")

[node name="Resume" type="Button" parent="."]

[connection signal="pressed" from="Resume" to="." method="_on_resume_pressed"]
""")
    godot_project.build()
    db_path = godot_project.root.parent / "graph.db"

    async def scenario(session):
        callers = await session.call_tool("callers", {"function_name": "_on_resume_pressed"})
        impact = await session.call_tool("impact", {"function_name": "_on_resume_pressed"})
        status = await session.call_tool("status", {})
        return callers, impact, status

    callers, impact, status = _run_session(db_path, scenario)
    assert not callers.isError and not impact.isError
    [caller] = json.loads(callers.content[0].text)["by_file"]["res://menu.tscn"]
    assert (caller["via"], caller.get("function"), caller["signal"]) == ("scene", None, "pressed")
    [entry] = json.loads(impact.content[0].text)["by_file"]["res://menu.tscn"]
    assert (entry["via"], entry.get("name"), entry["depth"]) == ("scene", None, 1)
    status_payload = json.loads(status.content[0].text)
    assert status_payload["resolved_scene_connections"] == 1
    assert status_payload["unresolved_scene_connections"] == 0


def test_list_tools_page_large_results_instead_of_returning_everything(godot_project):
    """Regression test: list tools returned every row at once -- a common
    helper's ~2,300 call sites came to ~100k tokens, far past what an MCP
    client accepts (Claude Code truncates at ~25k). They return a page
    (default 50) with `total`/`next_offset`, grouped by file."""
    godot_project.write("util.gd", "class_name Util\nextends Node\nstatic func t(x):\n    return x\n")
    for i in range(12):
        body = "".join(f"func f{j}():\n    Util.t({j})\n" for j in range(10))
        godot_project.write(f"screens/s{i}.gd", f"extends Node\n{body}")
    godot_project.write("a.gd", "extends Node\nfunc heal():\n    pass\nfunc run():\n    heal()\n")
    godot_project.write("b.gd", "extends Node\nfunc heal():\n    pass\nfunc run():\n    heal()\n")
    godot_project.build()
    db_path = godot_project.root.parent / "graph.db"

    async def scenario(session):
        async def call(tool, args):
            result = await session.call_tool(tool, args)
            assert not result.isError, result.content
            return json.loads(result.content[0].text)

        first = await call("callers", {"function_name": "t"})
        assert (first["total"], first["next_offset"]) == (120, 50)
        assert sum(len(v) for v in first["by_file"].values()) == 50
        seen = []
        offset = 0
        while offset is not None:
            page = await call("callers", {"function_name": "t", "offset": offset, "limit": 40})
            seen += [(f, e["function"]) for f, entries in page["by_file"].items() for e in entries]
            offset = page["next_offset"]
        assert len(seen) == len(set(seen)) == 120

        node = await call("node", {"name": "t"})
        assert (node["callers"]["total"], node["callers"]["next_offset"]) == (120, 20)

        merged = await call("callers", {"function_name": "heal"})
        assert merged["declarations"] == 2
        assert {(f, e["target_file"]) for f, entries in merged["by_file"].items() for e in entries} == {
            ("res://a.gd", "res://a.gd"), ("res://b.gd", "res://b.gd"),
        }

        ambiguous = await call("impact", {"function_name": "heal"})
        assert ambiguous["ambiguous"] is True
        assert set(ambiguous["by_file"]) == {"res://a.gd", "res://b.gd"}
        picked = await call("impact", {"function_name": "heal", "file": "res://a.gd"})
        assert "ambiguous" not in picked
        assert picked["by_file"] == {"res://a.gd": [{"name": "run", "line": 4, "depth": 1}]}

        files = await call("files", {"limit": 5})
        assert (files["total"], len(files["files"])) == (15, 5)
        assert files["directories"] == {"res://screens/": 12}
        narrowed = await call("files", {"prefix": "res://screens/"})
        assert narrowed["total"] == 12 and "directories" not in narrowed

    _run_session(db_path, scenario)


def test_results_say_stale_while_a_rebuild_is_pending(godot_project):
    """A query answered between an edit and the rebuild it triggers reflects
    the old code -- every result must say so, not only `status`."""
    godot_project.write("x.gd", "extends Node\nfunc a():\n    pass\n")
    godot_project.build()
    db_path = godot_project.root.parent / "graph.db"

    async def scenario(session):
        async def search():
            return json.loads((await session.call_tool("search", {"query": "a"})).content[0].text)

        for _ in range(50):  # let the startup freshness check settle
            if "stale" not in await search():
                break
            await asyncio.sleep(0.1)
        assert "stale" not in await search()

        godot_project.write("x.gd", "extends Node\nfunc a():\n    pass\nfunc b():\n    pass\n")
        for _ in range(50):
            if (await search()).get("stale") is True:
                return
            await asyncio.sleep(0.1)
        raise AssertionError("no result was marked stale during the debounce window")

    _run_session(db_path, scenario, extra_args=["--debounce-ms", "5000"])
