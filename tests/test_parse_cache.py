from __future__ import annotations

import sqlite3

from gdscript_graph.db import build_database, connect
from helpers import resolved_calls, symbols


def test_second_build_with_nothing_changed_is_a_full_cache_hit(godot_project):
    """Regression test: rebuilding with zero source changes must reuse
    every file's cached tree instead of re-parsing from scratch -- the
    entire point of the parse cache."""
    godot_project.write("a.gd", "extends Node\nfunc a():\n    b()\n\nfunc b():\n    pass\n")
    db_path = godot_project.root.parent / "graph.db"

    stats1 = build_database(godot_project.root, db_path)
    assert stats1.parse_cache_hits == 0
    assert stats1.parse_cache_misses == 1

    stats2 = build_database(godot_project.root, db_path)
    assert stats2.parse_cache_hits == 1
    assert stats2.parse_cache_misses == 0

    # Output must be identical whether or not caching kicked in.
    conn = connect(db_path)
    calls = resolved_calls(conn)
    assert any(c["src_fn"] == "a" and c["tgt_fn"] == "b" for c in calls)


def test_editing_one_file_only_reparses_that_file(godot_project):
    """Regression test: changing one file among several must reparse only
    that file -- the others' cached trees stay valid since parsing is a
    pure function of a file's own content."""
    godot_project.write("a.gd", "extends Node\nfunc a():\n    pass\n")
    godot_project.write("b.gd", "extends Node\nfunc b():\n    pass\n")
    godot_project.write("c.gd", "extends Node\nfunc c():\n    pass\n")
    db_path = godot_project.root.parent / "graph.db"
    build_database(godot_project.root, db_path)

    godot_project.write("b.gd", "extends Node\nfunc b_renamed():\n    pass\n")
    stats = build_database(godot_project.root, db_path)

    assert stats.parse_cache_hits == 2
    assert stats.parse_cache_misses == 1

    conn = connect(db_path)
    names = {s["name"] for s in symbols(conn)}
    assert "b_renamed" in names
    assert "b" not in names


def test_removing_a_file_is_reflected_and_does_not_leave_a_stale_cache_entry(godot_project):
    """Regression test: a deleted file must disappear from the graph, and
    its now-orphaned cache entry must not accumulate forever (the cache is
    always rebuilt from the currently-discovered file set, so removed
    files are naturally dropped)."""
    godot_project.write("a.gd", "extends Node\nfunc a():\n    pass\n")
    godot_project.write("b.gd", "extends Node\nfunc b():\n    pass\n")
    db_path = godot_project.root.parent / "graph.db"
    build_database(godot_project.root, db_path)

    (godot_project.root / "b.gd").unlink()
    stats = build_database(godot_project.root, db_path)
    assert stats.parse_cache_hits == 1
    assert stats.file_count == 1

    conn = connect(db_path)
    assert conn.execute("SELECT 1 FROM parse_cache WHERE res_path = 'res://b.gd'").fetchone() is None
    names = {s["name"] for s in symbols(conn)}
    assert "b" not in names


def test_corrupt_cache_entry_falls_back_to_a_fresh_parse_instead_of_crashing(godot_project):
    """Regression test: a cache entry that can't be unpickled (e.g. written
    by an incompatible past version, or bit rot) is purely a performance
    optimization gone stale -- it must degrade to re-parsing that file, not
    crash the whole build."""
    godot_project.write("a.gd", "extends Node\nfunc a():\n    pass\n")
    db_path = godot_project.root.parent / "graph.db"
    build_database(godot_project.root, db_path)

    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE parse_cache SET tree_blob = ? WHERE res_path = 'res://a.gd'", (b"not a valid pickle",))
    conn.commit()
    conn.close()

    stats = build_database(godot_project.root, db_path)
    assert stats.parse_cache_misses == 1

    names = {s["name"] for s in symbols(connect(db_path))}
    assert "a" in names
