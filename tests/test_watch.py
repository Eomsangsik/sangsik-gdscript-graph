from __future__ import annotations

import time

from gdscript_graph import db as gdb
from gdscript_graph import watch as watch_module
from gdscript_graph.watch import start_watching


def _wait_for(predicate, timeout_s=10, interval_s=0.1) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return False


def test_editing_a_gd_file_triggers_debounced_auto_rebuild(godot_project):
    """Regression test: the whole point of the file watcher is that a
    project edit gets reflected in the db without anyone manually re-running
    `gdscript-graph build` -- verified end-to-end with a real OS-level
    watcher (not a mock), a real debounce timer, and a real rebuild."""
    godot_project.write("main.gd", "extends Node\nfunc foo_v1():\n    pass\n")
    conn = godot_project.build()
    conn.close()

    db_path = godot_project.root.parent / "graph.db"
    handle = start_watching(godot_project.root, db_path, debounce_seconds=0.3)
    try:
        def has_symbol(name):
            def check():
                c = gdb.connect(db_path)
                try:
                    return c.execute("SELECT 1 FROM symbols WHERE name = ?", (name,)).fetchone() is not None
                finally:
                    c.close()
            return check

        assert _wait_for(has_symbol("foo_v1"))

        time.sleep(0.5)  # settle past any initial-scan events before editing
        godot_project.write("main.gd", "extends Node\nfunc foo_v2():\n    pass\n")

        assert _wait_for(has_symbol("foo_v2"), timeout_s=15)
    finally:
        handle.stop()


def test_start_watching_reconciles_offline_edits_made_before_it_started(godot_project):
    """Regression test: a live OS file watcher only sees events from the
    moment it starts -- it can't retroactively know about an edit made
    while nothing was watching (e.g. a fresh `gdscript-graph mcp` process
    spawned by an MCP client after the project was edited in the Godot
    editor with no server/watcher running at all). `start_watching` must
    catch up on that gap itself, without waiting for a *further* live edit
    to happen to trigger the fix."""
    godot_project.write("main.gd", "extends Node\nfunc foo_v1():\n    pass\n")
    conn = godot_project.build()
    conn.close()

    # Simulate an "offline" edit made while no watcher was running at all.
    godot_project.write("main.gd", "extends Node\nfunc foo_offline_edit():\n    pass\n")

    db_path = godot_project.root.parent / "graph.db"
    handle = start_watching(godot_project.root, db_path, debounce_seconds=0.3)
    try:
        def has_symbol(name):
            def check():
                c = gdb.connect(db_path)
                try:
                    return c.execute("SELECT 1 FROM symbols WHERE name = ?", (name,)).fetchone() is not None
                finally:
                    c.close()
            return check

        assert _wait_for(has_symbol("foo_offline_edit"), timeout_s=15)
    finally:
        handle.stop()


def test_reconcile_on_start_false_skips_the_immediate_rebuild(monkeypatch, godot_project):
    """Regression test: `reconcile_on_start=False` must skip the immediate
    catch-up rebuild. Verified at the call level rather than via real
    filesystem timing: on macOS, FSEvents' own event-coalescing latency can
    report an edit made just *before* the watch started as if it were a
    live event shortly *after* -- making a black-box "the offline edit
    never appears" assertion inherently flaky and not actually specific to
    this feature."""
    godot_project.write("main.gd", "extends Node\nfunc foo_v1():\n    pass\n")
    conn = godot_project.build()
    conn.close()

    db_path = godot_project.root.parent / "graph.db"
    rebuild_calls = []
    monkeypatch.setattr(
        watch_module._DebouncedRebuildHandler, "_rebuild", lambda self: rebuild_calls.append(1)
    )

    handle = start_watching(godot_project.root, db_path, debounce_seconds=0.3, reconcile_on_start=False)
    try:
        time.sleep(0.5)
        assert rebuild_calls == []
    finally:
        handle.stop()


def test_editing_an_unrelated_file_does_not_trigger_a_rebuild(godot_project):
    """Regression test: the watcher must filter to .gd/.tscn/project.godot
    -- otherwise its own db writes (a plain file, not one of those
    extensions) would retrigger themselves in an infinite rebuild loop, and
    editor swap files/unrelated assets would cause pointless rebuilds."""
    godot_project.write("main.gd", "extends Node\nfunc foo_v1():\n    pass\n")
    conn = godot_project.build()
    conn.close()

    db_path = godot_project.root.parent / "graph.db"
    handle = start_watching(godot_project.root, db_path, debounce_seconds=0.3)
    try:
        time.sleep(1.0)  # let the reconcile-on-start rebuild (if any) settle first
        built_at_before = gdb.get_meta(gdb.connect(db_path), "built_at")

        godot_project.write("notes.txt", "this is not GDScript")
        time.sleep(1.5)  # long enough for a rebuild to have happened if one was (wrongly) triggered

        built_at_after = gdb.get_meta(gdb.connect(db_path), "built_at")
        assert built_at_after == built_at_before
    finally:
        handle.stop()
