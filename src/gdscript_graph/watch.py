from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from watchdog.events import (
    EVENT_TYPE_CREATED,
    EVENT_TYPE_DELETED,
    EVENT_TYPE_MODIFIED,
    EVENT_TYPE_MOVED,
    FileSystemEventHandler,
)
from watchdog.observers import Observer

from gdscript_graph.db import build_database
from gdscript_graph.locking import build_lock, watch_lock

logger = logging.getLogger(__name__)

_WATCHED_SUFFIXES = (".gd", ".tscn")
_WATCHED_NAMES = ("project.godot",)

DEFAULT_DEBOUNCE_SECONDS = 2.0

# How often a follower checks whether the leader has gone (see WatchHandle).
LEADER_RETRY_SECONDS = 5.0

# Only events that can actually change a file's contents. inotify (Linux)
# also reports plain opens and read-only closes (`opened`,
# `closed_no_write`) -- and a rebuild itself opens and reads every watched
# file, so reacting to those would make each rebuild schedule the next one,
# forever, with nothing edited. A real write still always arrives as
# `modified` (alongside its `closed`), so ignoring `closed` loses nothing.
_REBUILD_EVENT_TYPES = frozenset({
    EVENT_TYPE_CREATED,
    EVENT_TYPE_DELETED,
    EVENT_TYPE_MODIFIED,
    EVENT_TYPE_MOVED,
})


def _is_watched_path(path: str) -> bool:
    name = Path(path).name
    return name in _WATCHED_NAMES or name.endswith(_WATCHED_SUFFIXES)


class _DebouncedRebuildHandler(FileSystemEventHandler):
    """Collapses a burst of filesystem events into a single rebuild, fired
    `debounce_seconds` after the *last* relevant event -- an editor save
    often touches a file more than once (write + rename, or several files
    in one multi-file save), and rebuilding on every individual event would
    otherwise re-parse the whole project once per event instead of once per
    edit.

    Every rebuild runs on one long-lived worker thread, so two rebuilds can
    never overlap: an edit that lands while a rebuild is running just marks
    another one due, which starts after the current one finishes -- however
    many edits arrive meanwhile, that's one more rebuild, not one each.
    (A timer per event used to start a second rebuild alongside a running
    one, and the two clobbered each other's temp db, briefly swapping in an
    empty database.)"""

    def __init__(self, project_root: Path, db_path: Path, debounce_seconds: float) -> None:
        self._project_root = project_root
        self._db_path = db_path
        self._debounce_seconds = debounce_seconds
        self._cond = threading.Condition()
        self._due_at: float | None = None  # time.monotonic() when the requested rebuild may start
        # The requested rebuild only catches up on edits nothing saw (see
        # `start_watching`): skip it if the db is already current.
        self._if_changed = False
        self._building = False
        self._stopped = False
        self._worker = threading.Thread(target=self._run, name="gdscript-graph-rebuild", daemon=True)
        self._worker.start()

    def _schedule_rebuild(self, delay: float | None = None, if_changed: bool = False) -> None:
        with self._cond:
            # A seen edit makes the next rebuild unconditional.
            self._if_changed = if_changed and (self._if_changed or self._due_at is None)
            self._due_at = time.monotonic() + (self._debounce_seconds if delay is None else delay)
            self._cond.notify()

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._stopped and (self._due_at is None or time.monotonic() < self._due_at):
                    self._cond.wait(None if self._due_at is None else self._due_at - time.monotonic())
                if self._stopped:
                    return
                self._due_at = None
                if_changed, self._if_changed = self._if_changed, False
                # A check that finds the db current isn't a pending rebuild
                # (see `_rebuild`).
                self._building = not if_changed
            try:
                self._rebuild(if_changed)
            finally:
                with self._cond:
                    self._building = False

    def _rebuild(self, if_changed: bool = False) -> None:
        try:
            build_database(
                self._project_root, self._db_path, if_changed=if_changed, on_build_start=self._mark_building,
            )
        except Exception:
            # A rebuild failure (e.g. a transient read error on a file mid-
            # save) must not kill the worker thread -- the next edit should
            # still trigger a retry rather than silently watching forever
            # with no further rebuilds.
            logger.exception("gdscript-graph: auto-rebuild failed")

    def _mark_building(self) -> None:
        with self._cond:
            self._building = True

    def is_pending(self) -> bool:
        """True while a rebuild is counting down or running."""
        with self._cond:
            return self._building or self._due_at is not None

    def stop(self) -> None:
        # Doesn't wait for a rebuild in flight: it finishes (or dies with the
        # process) on its own, and either way swaps in a complete db or none.
        with self._cond:
            self._stopped = True
            self._cond.notify()

    def on_any_event(self, event) -> None:
        if event.is_directory or event.event_type not in _REBUILD_EVENT_TYPES:
            return
        paths = [event.src_path]
        dest_path = getattr(event, "dest_path", "")
        if dest_path:
            paths.append(dest_path)
        if any(_is_watched_path(p) for p in paths):
            self._schedule_rebuild()


class WatchHandle:
    """The file watcher of one server process. Several servers can share a
    db (one per open editor session), but only one at a time -- the leader,
    holding the db's watch lock -- watches the project and rebuilds; the
    rest are followers that only serve queries from the db the leader keeps
    current, each retrying every `LEADER_RETRY_SECONDS` to take over once
    the leader exits. Otherwise every save made each server rebuild the
    same db, all at once."""

    def __init__(
        self, project_root: Path, db_path: Path, debounce_seconds: float, reconcile_on_start: bool
    ) -> None:
        self._project_root = project_root
        self._handler = _DebouncedRebuildHandler(project_root, db_path, debounce_seconds)
        self._leader_lock = watch_lock(db_path)
        self._build_lock = build_lock(db_path)
        self._state_lock = threading.Lock()
        self._observer: Observer | None = None
        self._stopped = threading.Event()
        if not self._try_to_lead(reconcile=reconcile_on_start):
            threading.Thread(target=self._retry_leadership, name="gdscript-graph-leader", daemon=True).start()

    def _try_to_lead(self, reconcile: bool) -> bool:
        with self._state_lock:
            if self._stopped.is_set() or not self._leader_lock.acquire(blocking=False):
                return False
            observer = Observer()
            observer.schedule(self._handler, str(self._project_root), recursive=True)
            observer.daemon = True
            observer.start()
            self._observer = observer
        if reconcile:
            self._handler._schedule_rebuild(delay=0, if_changed=True)
        return True

    def _retry_leadership(self) -> None:
        # Taking over always reconciles: nothing watched the project between
        # the old leader's exit and now.
        while not self._stopped.wait(LEADER_RETRY_SECONDS):
            if self._try_to_lead(reconcile=True):
                return

    @property
    def is_leader(self) -> bool:
        with self._state_lock:
            return self._observer is not None

    def is_pending(self) -> bool:
        """Whether a rebuild is counting down or running -- in this process,
        or (a leader's, or a CLI build) in any other."""
        return self._handler.is_pending() or self._build_lock.is_held_elsewhere()

    def stop(self) -> None:
        with self._state_lock:
            self._stopped.set()
            observer, self._observer = self._observer, None
        if observer is not None:
            observer.stop()
            observer.join()
        self._handler.stop()
        self._leader_lock.release()


def start_watching(
    project_root: Path,
    db_path: Path,
    debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS,
    reconcile_on_start: bool = True,
) -> WatchHandle:
    """Start a background OS-level file watcher (FSEvents/inotify/
    ReadDirectoryChangesW via `watchdog`) over `project_root`, rebuilding
    `db_path` `debounce_seconds` after the last relevant `.gd`/`.tscn`/
    `project.godot` change -- or, if another process already watches for
    `db_path`, stand by to take over from it (see `WatchHandle`). Returns a
    `WatchHandle`; call `.stop()` on it to shut the watcher down.

    A live OS file watcher only ever sees events from the moment it starts
    -- it has no way to know about edits made *before* that, e.g. the
    common case of an MCP client spawning a fresh server process each
    session while the project was edited in between sessions (in the
    Godot editor, another tool, or just with the AI assistant not running).
    `reconcile_on_start` closes that gap: it immediately schedules one
    rebuild (not blocking the caller) to catch up on any such offline
    changes before the live watcher's first event even arrives -- skipped
    when every input file's modification time and size still match the
    db's record of them (`build_database(..., if_changed=True)`)."""
    return WatchHandle(project_root, db_path, debounce_seconds, reconcile_on_start)
