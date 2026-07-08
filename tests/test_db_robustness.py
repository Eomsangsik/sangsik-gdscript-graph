from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from gdscript_graph import db as gdb
from gdscript_graph.cli import _cmd_build, _cmd_mcp
from gdscript_graph.db import build_database
from gdscript_graph.discovery import find_gd_files


def test_pathologically_nested_expression_does_not_abort_whole_build(godot_project):
    """Regression test: a syntactically valid but pathologically deep
    expression (e.g. 1000+ levels of nested calls) parses fine at the Lark
    level but can exceed Python's recursion limit in our own tree walks --
    that must be isolated to the one file, not crash the whole build and
    discard every other file's data."""
    deep_expr = "0"
    for _ in range(1200):
        deep_expr = f"max({deep_expr}, 1)"
    godot_project.write("deep.gd", f"extends Node\nfunc bad():\n    return {deep_expr}\n")
    godot_project.write("good.gd", "extends Node\nfunc fine():\n    return 1\n")

    conn = godot_project.build()
    rows = {(s["name"], s["res_path"]) for s in conn.execute("SELECT name, res_path FROM symbols").fetchall()}
    assert ("fine", "res://good.gd") in rows


def test_deeply_nested_inner_classes_preserve_class_name_and_extends_for_inheritance(godot_project):
    """Regression test: when a file is too deeply nested to fully index
    (see test_pathologically_nested_expression_does_not_abort_whole_build),
    its `class_name`/`extends` must still be recovered -- they only scan
    the tree's direct top-level children (no recursion), so losing them
    too would silently break inheritance-chain resolution for every OTHER
    file that extends this one, well beyond the one pathological file."""
    godot_project.write("base.gd", "extends Node\nclass_name Base\nfunc base_ability():\n    pass\n")

    lines = ["extends Base", "class_name PathMid", ""]
    indent = ""
    for i in range(1200):
        lines.append(f"{indent}class C{i}:")
        indent += "    "
    lines.append(indent + "pass")
    godot_project.write("path_mid.gd", "\n".join(lines) + "\n")

    godot_project.write("grandchild.gd", """
extends PathMid
class_name Grandchild

func use_it():
    base_ability()
""")
    conn = godot_project.build()
    files = {r["res_path"]: dict(r) for r in conn.execute("SELECT res_path, class_name, extends, parse_error FROM files")}
    assert files["res://path_mid.gd"]["class_name"] == "PathMid"
    assert files["res://path_mid.gd"]["extends"] == "Base"
    assert files["res://path_mid.gd"]["parse_error"] is not None

    calls = conn.execute(
        "SELECT t.name AS tgt_fn, t.res_path AS tgt_file FROM calls c "
        "JOIN symbols s ON s.id = c.source_symbol_id JOIN symbols t ON t.id = c.target_symbol_id "
        "WHERE s.name = 'use_it'"
    ).fetchall()
    assert any(c["tgt_fn"] == "base_ability" and c["tgt_file"] == "res://base.gd" for c in calls)


def test_deep_nesting_in_calls_extraction_is_reported_as_a_parse_error(godot_project):
    """Regression test: a file whose SYMBOL extraction succeeds cleanly
    (ordinary functions/signal/field/enum) but whose CALLS extraction
    overflows the recursion limit (a pathologically deep expression buried
    in one otherwise-unrelated function) must be flagged in
    `files.parse_error`/`BuildStats.parse_error_count` -- otherwise the
    file looks fully clean while its entire call/connection graph for
    every function in it (not just the pathological one) silently
    vanishes with zero trace. (The whole file's calls are lost, including
    the perfectly ordinary `normal_func -> helper` one -- extraction isn't
    granular per-function -- but that loss must at least be visible.)"""
    deep_expr = "0"
    for _ in range(4000):
        deep_expr = f"({deep_expr} + 1)"
    godot_project.write("thing.gd", f"""
extends Node

func helper():
    pass

func normal_func():
    helper()

func pathological_func():
    return {deep_expr}
""")
    conn = godot_project.build()
    row = conn.execute("SELECT parse_error FROM files WHERE res_path = 'res://thing.gd'").fetchone()
    assert row["parse_error"] is not None

    names = {s["name"] for s in conn.execute("SELECT name FROM symbols").fetchall()}
    assert {"helper", "normal_func", "pathological_func"} <= names


def test_file_discovery_order_is_platform_independent_for_class_name_collisions(godot_project):
    """Regression test: `find_gd_files` used to sort raw `Path` objects,
    whose ordering is platform-flavor-dependent -- `PurePosixPath` compares
    case-sensitively, `PureWindowsPath` case-insensitively -- so building
    the identical, unchanged project on different host OSes could discover
    files in a different order and silently flip which file wins a
    duplicate `class_name` collision (`build_class_name_table`: "later
    file wins"). Sorting by the res://-relative POSIX path *string*
    instead makes discovery order (and thus the collision winner)
    deterministic regardless of build host."""
    godot_project.write("apple.gd", "extends Node\nclass_name Thing\n")
    godot_project.write("Enemy.gd", "extends Node\nclass_name Thing\n")

    files = find_gd_files(godot_project.root)
    names = [f.name for f in files]
    # "Enemy.gd" < "apple.gd" as plain strings (ASCII 'E' < 'a') -- this
    # must hold true regardless of which OS actually runs the build.
    assert names == sorted(names)
    assert names[-1] == "apple.gd"


def test_deep_nesting_inside_property_accessor_does_not_lose_sibling_symbols(godot_project):
    """Regression test: `iter_function_defs` used to lack the same
    stop-recursion boundaries (`property_body_def`, `lambda`) that its
    sibling `iter_scoped_subtrees` already had -- since neither can
    contain a nested named func/static/abstract func def, there was
    nothing to gain by recursing into them, only needless extra depth.
    A pathologically deep expression inside a property accessor's `get:`
    body used to overflow *symbol* extraction entirely (losing every
    other function/field/signal in the file, not just calls), instead of
    degrading no worse than the equivalent nesting in a plain function
    body (which only loses that file's calls, per the test above)."""
    deep_expr = "0"
    for _ in range(3000):
        deep_expr = f"max({deep_expr}, 1)"
    godot_project.write("thing.gd", f"""
extends Node

var health: int = 100:
    get:
        return {deep_expr}

func normal_func():
    pass
""")
    conn = godot_project.build()
    row = conn.execute("SELECT parse_error FROM files WHERE res_path = 'res://thing.gd'").fetchone()
    assert row["parse_error"] is not None

    names = {s["name"] for s in conn.execute("SELECT name FROM symbols").fetchall()}
    assert {"normal_func", "health", "health.get"} <= names


def test_search_escapes_like_wildcards(godot_project):
    godot_project.write("x.gd", """
extends Node

func on_died() -> void:
    pass

func onXdied() -> void:
    pass
""")
    conn = godot_project.build()
    results = {r["name"] for r in gdb.search_symbols(conn, "on_died")}
    assert results == {"on_died"}


def test_signal_handlers_distinguishes_same_named_handler_in_different_scopes(godot_project):
    """Regression test: two same-named signals (one top-level, one in a
    nested class), each connected to a same-named handler, must not produce
    indistinguishable result rows -- signal_scope/handler_scope disambiguate
    them, and `scope` can isolate one signal."""
    godot_project.write("x.gd", """
extends Node

signal died

class Inner:
    signal died

    func _on_died() -> void:
        pass

    func setup_inner() -> void:
        died.connect(_on_died)

func _on_died() -> void:
    pass

func setup_outer() -> void:
    died.connect(_on_died)
""")
    conn = godot_project.build()
    all_handlers = gdb.get_signal_handlers(conn, "died")
    assert len(all_handlers) == 2
    assert {r["signal_scope"] for r in all_handlers} == {None, "Inner"}
    assert {r["handler_scope"] for r in all_handlers} == {None, "Inner"}

    inner_only = gdb.get_signal_handlers(conn, "died", scope="Inner")
    assert len(inner_only) == 1
    assert inner_only[0]["handler_scope"] == "Inner"


def test_transitive_query_does_not_drop_ambiguous_seed_reached_via_real_edge(godot_project):
    """Regression test: when the query name is ambiguous (unfiltered,
    multiple distinct declarations share it) and one of those declarations
    genuinely calls another, the callee must still appear in the result --
    a single multi-source BFS pre-marking every same-named declaration as
    "visited at depth 0" used to silently swallow it (and report anything
    beyond it one hop too shallow) purely because it shared the query's
    name with a seed, not because it was actually the seed being queried."""
    godot_project.write("file_a.gd", """
extends Node

func util() -> void:
    var b: FileB = FileB.new()
    b.util()
""")
    godot_project.write("file_b.gd", """
extends Node
class_name FileB

func util() -> void:
    downstream()

func downstream() -> void:
    pass
""")
    conn = godot_project.build()
    unfiltered = gdb.get_callees_transitive(conn, "util")
    assert any(r["res_path"] == "res://file_b.gd" and r["name"] == "util" for r in unfiltered)

    disambiguated = gdb.get_callees_transitive(conn, "util", res_path="res://file_a.gd")
    by_name = {r["name"]: r["depth"] for r in disambiguated}
    assert by_name == {"util": 1, "downstream": 2}


def test_transitive_query_scope_param_disambiguates(godot_project):
    godot_project.write("x.gd", """
extends Node

class Inner:
    func foo() -> void:
        pass

func foo() -> void:
    pass

func caller_top() -> void:
    foo()
""")
    conn = godot_project.build()
    top_callers = gdb.get_callers_transitive(conn, "foo", scope=None)
    inner_callers = gdb.get_callers_transitive(conn, "foo", scope="Inner")
    assert any(r["name"] == "caller_top" for r in top_callers)
    assert inner_callers == []


def test_failed_rebuild_preserves_existing_database(godot_project, monkeypatch):
    godot_project.write("x.gd", "extends Node\nfunc a():\n    pass\n")
    db_path = godot_project.root.parent / "graph.db"
    build_database(godot_project.root, db_path)
    good_bytes = db_path.read_bytes()

    import gdscript_graph.db as db_module

    def boom(conn, project_root, old_parse_cache=None):
        conn.executescript(db_module.SCHEMA)
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(db_module, "_populate", boom)
    with pytest.raises(RuntimeError):
        build_database(godot_project.root, db_path)

    assert db_path.read_bytes() == good_bytes
    assert list(db_path.parent.glob(f"{db_path.name}.tmp-*")) == []


def test_failed_rename_cleans_up_tmp_file_and_preserves_existing_database(godot_project, monkeypatch):
    """Regression test: `os.replace(tmp_path, db_path)` (the final atomic
    swap) can itself fail -- e.g. disk full, or a permissions/cross-
    filesystem issue -- even though `_populate` above it succeeded. Without
    a dedicated try/except around just that call, such a failure leaked
    `tmp_path` forever on every occurrence (unlike a `_populate` failure,
    which already cleaned up after itself). This must both clean up the
    stray tmp file and preserve whatever database previously existed."""
    godot_project.write("x.gd", "extends Node\nfunc a():\n    pass\n")
    db_path = godot_project.root.parent / "graph.db"
    build_database(godot_project.root, db_path)
    good_bytes = db_path.read_bytes()

    def boom(src, dst):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(gdb.os, "replace", boom)
    with pytest.raises(OSError):
        build_database(godot_project.root, db_path)

    assert db_path.read_bytes() == good_bytes
    assert list(db_path.parent.glob(f"{db_path.name}.tmp-*")) == []


def test_build_output_path_as_existing_directory_raises_clear_error(godot_project):
    godot_project.write("x.gd", "extends Node\nfunc a():\n    pass\n")
    out_dir = godot_project.root.parent / "out_as_dir"
    out_dir.mkdir()
    with pytest.raises(IsADirectoryError):
        build_database(godot_project.root, out_dir)


def test_build_records_project_root_in_meta_table(godot_project):
    """Regression test: `run_server` recovers the project root a db was
    built from out of `meta` to know what directory to auto-watch for
    changes -- without this, a rebuild could silently drift out of sync
    with whatever directory the db actually indexes."""
    godot_project.write("x.gd", "extends Node\nfunc a():\n    pass\n")
    conn = godot_project.build()
    assert gdb.get_meta(conn, "project_root") == str(godot_project.root)
    assert gdb.get_meta(conn, "built_at") is not None
    assert gdb.get_meta(conn, "no_such_key") is None


def test_validate_schema_rejects_database_missing_meta_table(tmp_path):
    """Regression test: an older db built before the `meta` table existed
    must be rejected with a clear message, not silently treated as having
    no recorded project root (which `run_server` also handles, but as a
    distinct "disable watching" case, not a "this db is stale/invalid"
    case)."""
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        for table in gdb.REQUIRED_TABLES - {"meta"}:
            conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
        with pytest.raises(ValueError):
            gdb.validate_schema(conn)
    finally:
        conn.close()


def test_cli_build_rejects_missing_project_dir(tmp_path, capsys):
    args = SimpleNamespace(project_dir=str(tmp_path / "does_not_exist"), out=None)
    exit_code = _cmd_build(args)
    assert exit_code == 1
    assert "does not exist" in capsys.readouterr().err


def test_cli_build_rejects_output_path_as_directory_without_traceback(godot_project, capsys):
    godot_project.write("x.gd", "extends Node\nfunc a():\n    pass\n")
    out_dir = godot_project.root.parent / "out_as_dir"
    out_dir.mkdir()
    args = SimpleNamespace(project_dir=str(godot_project.root), out=str(out_dir))
    exit_code = _cmd_build(args)
    assert exit_code == 1
    assert "directory" in capsys.readouterr().err


def test_duplicate_class_name_is_reported_in_build_stats(godot_project):
    """Regression test: two files declaring the same class_name is a real
    authoring mistake that used to be resolved silently ("later file
    wins", per build_class_name_table) with zero signal anywhere that an
    ambiguity existed -- BuildStats must surface it."""
    godot_project.write("aaa_player.gd", "extends Node\nclass_name Player\nfunc heal():\n    pass\n")
    godot_project.write("zzz_player.gd", "extends Node\nclass_name Player\nfunc heal():\n    pass\n")
    godot_project.write("unique.gd", "extends Node\nclass_name Unique\n")

    db_path = godot_project.root.parent / "graph.db"
    stats = build_database(godot_project.root, db_path)
    assert stats.duplicate_class_names == {"Player": ["res://aaa_player.gd", "res://zzz_player.gd"]}


def test_cli_build_warns_about_duplicate_class_name(godot_project, capsys):
    godot_project.write("aaa_player.gd", "extends Node\nclass_name Player\n")
    godot_project.write("zzz_player.gd", "extends Node\nclass_name Player\n")
    args = SimpleNamespace(project_dir=str(godot_project.root), out=None)
    exit_code = _cmd_build(args)
    assert exit_code == 0
    err = capsys.readouterr().err
    assert "Player" in err
    assert "aaa_player.gd" in err and "zzz_player.gd" in err


def test_cli_mcp_rejects_missing_db_without_traceback(tmp_path, capsys):
    missing_db = tmp_path / "does_not_exist.db"
    args = SimpleNamespace(db=str(missing_db), no_watch=False, debounce_ms=2000.0)
    exit_code = _cmd_mcp(args)
    assert exit_code == 1
    assert "not found" in capsys.readouterr().err
    assert not missing_db.exists()


def test_mcp_server_startup_does_not_create_stray_file_for_missing_db(tmp_path):
    from gdscript_graph.mcp_server import run_server

    missing_db = tmp_path / "does_not_exist.db"
    with pytest.raises(ValueError):
        run_server(missing_db)
    assert not missing_db.exists()


def test_mcp_server_startup_rejects_directory_db_path(tmp_path):
    """Regression test: a directory passed as the db path must raise the
    same kind of clear error build_database gives for its equivalent -o
    case, not a raw sqlite3 OperationalError."""
    from gdscript_graph.mcp_server import run_server

    db_as_dir = tmp_path / "graph.db"
    db_as_dir.mkdir()
    with pytest.raises(ValueError, match="directory"):
        run_server(db_as_dir)


def test_unreadable_project_godot_does_not_crash_build(tmp_path):
    """Regression test: a non-UTF-8 project.godot must degrade gracefully
    (no autoloads found) rather than crashing the whole build."""
    root = tmp_path / "project"
    root.mkdir()
    (root / "project.godot").write_bytes(b"\xff\xfe not valid utf-8 \x80\x81")
    (root / "x.gd").write_text("extends Node\nfunc a():\n    pass\n")
    db_path = tmp_path / "graph.db"
    stats = build_database(root, db_path)
    assert stats.file_count == 1


def test_validate_schema_rejects_non_graph_database(tmp_path):
    db_path = tmp_path / "empty.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        with pytest.raises(ValueError):
            gdb.validate_schema(conn)
    finally:
        conn.close()
