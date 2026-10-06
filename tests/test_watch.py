from __future__ import annotations

import subprocess
import sys
import threading
import time

from gdscript_graph import db as gdb
from gdscript_graph import watch as watch_module
from gdscript_graph.db import build_database
from gdscript_graph.locking import build_lock
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
        watch_module._DebouncedRebuildHandler, "_rebuild", lambda self, if_changed=False: rebuild_calls.append(1)
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


class _FakeEvent:
    def __init__(self, event_type: str, src_path: str, is_directory: bool = False) -> None:
        self.event_type = event_type
        self.src_path = src_path
        self.is_directory = is_directory


def test_open_and_read_only_close_events_do_not_schedule_a_rebuild(tmp_path):
    """Regression test: inotify reports plain opens and read-only closes
    (`opened`, `closed_no_write`) -- and a rebuild itself opens and reads
    every .gd file, so reacting to those turned each rebuild into the
    trigger for the next one, rebuilding forever on Linux with nothing
    edited. Checked against the handler directly (not a live watcher) so it
    fails on every OS, not just the one whose backend emits these events."""
    handler = watch_module._DebouncedRebuildHandler(tmp_path, tmp_path / "graph.db", debounce_seconds=60)
    scheduled: list[str] = []
    handler._schedule_rebuild = lambda delay=None: scheduled.append("rebuild")

    gd_path = str(tmp_path / "main.gd")
    for event_type in ("opened", "closed_no_write", "closed"):
        handler.on_any_event(_FakeEvent(event_type, gd_path))
    assert scheduled == []

    for event_type in ("created", "modified", "deleted", "moved"):
        handler.on_any_event(_FakeEvent(event_type, gd_path))
    assert len(scheduled) == 4


def test_reading_watched_files_does_not_trigger_a_rebuild(godot_project):
    """Regression test (live watcher): merely reading a .gd/.tscn file --
    exactly what every rebuild, and the `node` tool's fresh source lookup,
    does -- must not trigger a rebuild. On Linux this used to loop forever."""
    godot_project.write("main.gd", "extends Node\nfunc foo():\n    pass\n")
    godot_project.write("main.tscn", '[gd_scene format=3]\n\n[node name="Main" type="Node"]\n')
    conn = godot_project.build()
    conn.close()

    db_path = godot_project.root.parent / "graph.db"
    handle = start_watching(godot_project.root, db_path, debounce_seconds=0.3)
    try:
        assert _wait_for(lambda: not handle.is_pending())  # let the reconcile-on-start rebuild settle
        time.sleep(0.5)
        built_at_before = gdb.get_meta(gdb.connect(db_path), "built_at")

        for name in ("main.gd", "main.tscn", "project.godot"):
            (godot_project.root / name).read_text()
        time.sleep(1.5)

        assert not handle.is_pending()
        assert gdb.get_meta(gdb.connect(db_path), "built_at") == built_at_before
    finally:
        handle.stop()


def test_rebuilds_never_overlap_and_edits_during_one_coalesce_into_one_more(tmp_path):
    """Regression test: each event used to start its own timer, so an edit
    saved while a rebuild was running started a second rebuild alongside it
    (the two then clobbered each other's temp db). Rebuilds must run one at
    a time, and any number of edits during one must add exactly one more."""
    handler = watch_module._DebouncedRebuildHandler(tmp_path, tmp_path / "graph.db", debounce_seconds=0.05)
    running = 0
    max_running = 0
    runs = 0
    first_started = threading.Event()

    def slow_rebuild(if_changed=False):
        nonlocal running, max_running, runs
        running += 1
        max_running = max(max_running, running)
        first_started.set()
        time.sleep(0.4)
        runs += 1
        running -= 1

    handler._rebuild = slow_rebuild
    try:
        handler._schedule_rebuild(delay=0)
        assert first_started.wait(5)
        for _ in range(5):  # a burst of saves while the first rebuild runs
            handler._schedule_rebuild()
            time.sleep(0.02)
        assert handler.is_pending()
        assert _wait_for(lambda: not handler.is_pending(), timeout_s=5)
        time.sleep(0.2)
        assert (runs, max_running) == (2, 1)
    finally:
        handler.stop()


def test_only_one_watcher_per_db_rebuilds_and_another_takes_over_when_it_stops(monkeypatch, godot_project):
    """Regression test: every MCP server (one per editor session) used to
    watch the project and rebuild the same db on every save -- two sessions,
    two full rebuilds per save. Only one (the leader) may rebuild; another
    takes over, catching up with a rebuild, once the leader stops."""
    monkeypatch.setattr(watch_module, "LEADER_RETRY_SECONDS", 0.1)
    godot_project.write("main.gd", "extends Node\nfunc foo():\n    pass\n")
    godot_project.build().close()
    db_path = godot_project.root.parent / "graph.db"

    rebuilds: list[int] = []
    monkeypatch.setattr(
        watch_module._DebouncedRebuildHandler, "_rebuild", lambda self, if_changed=False: rebuilds.append(id(self))
    )
    first = start_watching(godot_project.root, db_path, debounce_seconds=0.2)
    second = start_watching(godot_project.root, db_path, debounce_seconds=0.2)
    try:
        assert (first.is_leader, second.is_leader) == (True, False)
        assert _wait_for(lambda: len(rebuilds) == 1)  # the leader's reconcile-on-start
        time.sleep(0.5)
        godot_project.write("main.gd", "extends Node\nfunc bar():\n    pass\n")
        assert _wait_for(lambda: len(rebuilds) == 2)
        time.sleep(1.0)
        assert set(rebuilds) == {id(first._handler)} and len(rebuilds) == 2

        first.stop()
        assert _wait_for(lambda: second.is_leader)
        assert _wait_for(lambda: id(second._handler) in rebuilds)
    finally:
        first.stop()
        second.stop()


def test_a_server_whose_leader_process_dies_takes_over(monkeypatch, godot_project):
    """The leader is usually another process -- one that can exit or crash
    (its session ended) without any cleanup. The OS drops its lock either
    way, and a follower must take over."""
    monkeypatch.setattr(watch_module, "LEADER_RETRY_SECONDS", 0.1)
    godot_project.write("main.gd", "extends Node\nfunc foo():\n    pass\n")
    godot_project.build().close()
    db_path = godot_project.root.parent / "graph.db"

    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sys, time; from pathlib import Path; from gdscript_graph.locking import watch_lock; "
         "lock = watch_lock(Path(sys.argv[1])); lock.acquire(); print('held', flush=True); time.sleep(60)",
         str(db_path)],
        stdout=subprocess.PIPE, text=True,
    )
    handle = None
    try:
        assert holder.stdout.readline().strip() == "held"
        handle = start_watching(godot_project.root, db_path, debounce_seconds=0.2)
        time.sleep(0.3)
        assert not handle.is_leader
        holder.kill()
        holder.wait()
        assert _wait_for(lambda: handle.is_leader)
    finally:
        holder.kill()
        if handle is not None:
            handle.stop()


def test_build_waits_for_a_build_of_the_same_db_in_another_process(godot_project):
    godot_project.write("main.gd", "extends Node\nfunc foo():\n    pass\n")
    db_path = godot_project.root.parent / "graph.db"
    lock = build_lock(db_path)
    lock.acquire()
    done = threading.Event()
    builder = threading.Thread(target=lambda: (build_database(godot_project.root, db_path), done.set()))
    try:
        builder.start()
        assert not done.wait(0.5)
    finally:
        lock.release()
    assert done.wait(10)
    builder.join()


def test_build_removes_temp_files_left_by_builds_that_never_finished(godot_project):
    """Regression test: a server killed mid-build (its session ended) left
    its temp db behind for good -- one project had 34 of them."""
    godot_project.write("main.gd", "extends Node\nfunc foo():\n    pass\n")
    db_path = godot_project.root.parent / "graph.db"
    leftovers = [db_path.with_name(f"{db_path.name}.tmp-{name}") for name in ("4029", "4029-journal", "77-abc123")]
    for path in leftovers:
        path.write_bytes(b"partial")
    build_database(godot_project.root, db_path)
    assert not any(path.exists() for path in leftovers)

    # Also when a server start finds the db current and skips the build.
    for path in leftovers:
        path.write_bytes(b"partial")
    assert build_database(godot_project.root, db_path, if_changed=True) is None
    assert not any(path.exists() for path in leftovers)


def test_start_watching_skips_the_catch_up_rebuild_when_the_db_is_current(godot_project):
    godot_project.write("main.gd", "extends Node\nfunc foo():\n    pass\n")
    godot_project.build().close()
    db_path = godot_project.root.parent / "graph.db"
    built_at = gdb.get_meta(gdb.connect(db_path), "built_at")

    handle = start_watching(godot_project.root, db_path, debounce_seconds=0.2)
    try:
        assert _wait_for(lambda: not handle.is_pending())
        assert gdb.get_meta(gdb.connect(db_path), "built_at") == built_at
    finally:
        handle.stop()
