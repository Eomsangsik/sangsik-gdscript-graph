from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

from gdscript_graph import db as db_module
from gdscript_graph import extract
from gdscript_graph.db import build_database, connect
from helpers import resolved_calls, signal_connections, symbols


def test_second_build_with_nothing_changed_is_a_full_cache_hit(godot_project):
    """Regression test: rebuilding with zero source changes must reuse
    every file's cached extraction instead of re-parsing from scratch --
    the entire point of the cache."""
    godot_project.write("a.gd", "extends Node\nfunc a():\n    b()\n\nfunc b():\n    pass\n")
    db_path = godot_project.root.parent / "graph.db"

    stats1 = build_database(godot_project.root, db_path)
    assert stats1.cache_hits == 0
    assert stats1.cache_misses == 1

    stats2 = build_database(godot_project.root, db_path)
    assert stats2.cache_hits == 1
    assert stats2.cache_misses == 0

    # Output must be identical whether or not caching kicked in.
    conn = connect(db_path)
    calls = resolved_calls(conn)
    assert any(c["src_fn"] == "a" and c["tgt_fn"] == "b" for c in calls)


def test_editing_one_file_only_reextracts_that_file(godot_project):
    """Regression test: changing one file among several must re-extract
    only that file -- the others' cached extractions stay valid since
    extraction is a pure function of a file's own content."""
    godot_project.write("a.gd", "extends Node\nfunc a():\n    pass\n")
    godot_project.write("b.gd", "extends Node\nfunc b():\n    pass\n")
    godot_project.write("c.gd", "extends Node\nfunc c():\n    pass\n")
    db_path = godot_project.root.parent / "graph.db"
    build_database(godot_project.root, db_path)

    godot_project.write("b.gd", "extends Node\nfunc b_renamed():\n    pass\n")
    stats = build_database(godot_project.root, db_path)

    assert stats.cache_hits == 2
    assert stats.cache_misses == 1

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
    assert stats.cache_hits == 1
    assert stats.file_count == 1

    conn = connect(db_path)
    assert conn.execute("SELECT 1 FROM extract_cache WHERE res_path = 'res://b.gd'").fetchone() is None
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
    conn.execute("UPDATE extract_cache SET blob = ? WHERE res_path = 'res://a.gd'", (b"not a valid pickle",))
    conn.commit()
    conn.close()

    stats = build_database(godot_project.root, db_path)
    assert stats.cache_misses == 1

    names = {s["name"] for s in symbols(connect(db_path))}
    assert "a" in names


def _dump(conn) -> dict[str, list]:
    tables = [
        "files", "symbols", "calls", "unresolved_calls", "signal_connections", "unresolved_connections",
        "scene_connections", "unresolved_scene_connections",
    ]
    return {t: sorted(map(tuple, conn.execute(f"SELECT * FROM {t}").fetchall())) for t in tables}


def test_cached_rebuild_is_identical_to_a_fresh_build(tmp_path):
    """Everything a cached file contributes must be exactly what extracting
    it fresh would -- checked across the benchmark fixture project, which
    exercises inheritance, signals, autoloads and scenes."""
    project = Path(__file__).parent.parent / "benchmarks" / "fixture_project"
    cached_db = tmp_path / "cached.db"
    build_database(project, cached_db)
    stats = build_database(project, cached_db)
    assert stats.cache_misses == 0

    fresh_db = tmp_path / "fresh.db"
    build_database(project, fresh_db)
    assert _dump(connect(cached_db)) == _dump(connect(fresh_db))


def test_unchanged_subclass_relinks_an_inherited_signal_its_parent_changes(godot_project):
    """A bare `died.connect(...)` links to whichever ancestor declares
    `died` -- a fact about *another* file. The subclass's cached extraction
    must not freeze that link: it's worked out at resolution, every build."""
    godot_project.write("base.gd", "class_name Base\nextends Node\nsignal died\n")
    godot_project.write("child.gd", "extends Base\nfunc _ready():\n    died.connect(_on_died)\nfunc _on_died():\n    pass\n")
    db_path = godot_project.root.parent / "graph.db"
    build_database(godot_project.root, db_path)
    assert [c["signal_file"] for c in signal_connections(connect(db_path))] == ["res://base.gd"]

    godot_project.write("base.gd", "class_name Base\nextends Node\n")
    stats = build_database(godot_project.root, db_path)
    assert (stats.cache_hits, stats.cache_misses) == (1, 1)
    assert [c["signal_file"] for c in signal_connections(connect(db_path))] == [None]

    godot_project.write("base.gd", "class_name Base\nextends Node\nsignal died\n")
    build_database(godot_project.root, db_path)
    assert [c["signal_file"] for c in signal_connections(connect(db_path))] == ["res://base.gd"]


def test_cache_from_another_extractor_version_is_ignored(godot_project, monkeypatch):
    """A changed extractor (or gdtoolkit grammar) can produce different
    results from the same bytes -- its cache must not be trusted."""
    godot_project.write("a.gd", "extends Node\nfunc a():\n    pass\n")
    db_path = godot_project.root.parent / "graph.db"
    build_database(godot_project.root, db_path)

    monkeypatch.setattr(extract, "extractor_version", lambda: "some-other-version")
    stats = build_database(godot_project.root, db_path)
    assert (stats.cache_hits, stats.cache_misses) == (0, 1)


def test_if_changed_skips_a_build_only_when_nothing_it_reads_changed(godot_project, monkeypatch):
    """Regression test: every server start rebuilt the db unconditionally
    (16 s on a 2,000-file project) to catch offline edits, even with none.
    The check compares every input's mtime and size with the db's record."""
    godot_project.write("a.gd", "extends Node\nfunc a():\n    pass\n")
    godot_project.write("a.tscn", "[gd_scene format=3]\n")
    db_path = godot_project.root.parent / "graph.db"
    assert build_database(godot_project.root, db_path, if_changed=True) is not None  # no db yet
    assert build_database(godot_project.root, db_path, if_changed=True) is None

    def touch(name):
        path = godot_project.root / name
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

    for change in (
        lambda: touch("a.gd"),
        lambda: touch("a.tscn"),
        lambda: touch("project.godot"),
        lambda: godot_project.write("b.gd", "extends Node\n"),
        lambda: (godot_project.root / "b.gd").unlink(),
        lambda: monkeypatch.setattr(db_module, "builder_version", lambda: "a newer gdscript-graph"),
    ):
        change()
        assert build_database(godot_project.root, db_path, if_changed=True) is not None
        assert build_database(godot_project.root, db_path, if_changed=True) is None


def test_unchanged_files_are_not_even_read_again(godot_project, monkeypatch):
    """Reading and hashing every file to find the changed ones was most of
    an unchanged rebuild's time. A file whose modification time and size
    match the previous build's record reuses its extraction unread."""
    for name in ("a", "b"):
        godot_project.write(f"{name}.gd", f"extends Node\nfunc {name}():\n    pass\n")
        hour_ago = time.time_ns() - 3600 * 10**9
        os.utime(godot_project.root / f"{name}.gd", ns=(hour_ago, hour_ago))
    db_path = godot_project.root.parent / "graph.db"
    build_database(godot_project.root, db_path)

    reads: list[str] = []
    real_read_bytes = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: (reads.append(self.name), real_read_bytes(self))[1])
    stats = build_database(godot_project.root, db_path)
    assert (stats.cache_hits, reads) == (2, [])
    assert {s["name"] for s in symbols(connect(db_path))} == {"a", "b"}


def test_a_same_size_edit_right_after_a_build_is_not_missed(godot_project):
    """An edit landing within the same modification-time tick as the
    previous build can leave mtime and size unchanged; a file that recent
    must be re-read, not trusted (git's "racy" index problem)."""
    godot_project.write("a.gd", "extends Node\nfunc aaa():\n    pass\n")
    path = godot_project.root / "a.gd"
    db_path = godot_project.root.parent / "graph.db"
    build_database(godot_project.root, db_path)

    st = path.stat()
    path.write_text("extends Node\nfunc bbb():\n    pass\n")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    build_database(godot_project.root, db_path)
    assert {s["name"] for s in symbols(connect(db_path))} == {"bbb"}


def test_builder_version_covers_the_bundled_engine_api(monkeypatch):
    """An update that only regenerates godot_api.json changes how calls are
    classified -- a server start must not keep a db built with the old one."""
    before = db_module.builder_version.__wrapped__()
    real_read_bytes = Path.read_bytes
    monkeypatch.setattr(
        Path, "read_bytes",
        lambda self: b"{}" if self.name == "godot_api.json" else real_read_bytes(self),
    )
    assert db_module.builder_version.__wrapped__() != before
