from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from gdscript_graph.db import build_database

logger = logging.getLogger(__name__)

_WATCHED_SUFFIXES = (".gd", ".tscn")
_WATCHED_NAMES = ("project.godot",)

DEFAULT_DEBOUNCE_SECONDS = 2.0


def _is_watched_path(path: str) -> bool:
    name = Path(path).name
    return name in _WATCHED_NAMES or name.endswith(_WATCHED_SUFFIXES)


class _DebouncedRebuildHandler(FileSystemEventHandler):
    """Collapses a burst of filesystem events into a single rebuild, fired
    `debounce_seconds` after the *last* relevant event -- an editor save
    often touches a file more than once (write + rename, or several files
    in one multi-file save), and rebuilding on every individual event would
    otherwise re-parse the whole project once per event instead of once per
    edit."""

    def __init__(self, project_root: Path, db_path: Path, debounce_seconds: float) -> None:
        self._project_root = project_root
        self._db_path = db_path
        self._debounce_seconds = debounce_seconds
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    def _schedule_rebuild(self, delay: float | None = None) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce_seconds if delay is None else delay, self._rebuild)
            self._timer.daemon = True
            self._timer.start()

    def _rebuild(self) -> None:
        try:
            build_database(self._project_root, self._db_path)
        except Exception:
            # A rebuild failure (e.g. a transient read error on a file mid-
            # save) must not kill the watcher thread -- the next edit should
            # still trigger a retry rather than silently watching forever
            # with no further rebuilds.
            logger.exception("gdscript-graph: auto-rebuild failed")

    def is_pending(self) -> bool:
        # A `threading.Timer` is alive from `.start()` until its target
        # function *returns* -- i.e. this is true both while the debounce
        # countdown is running and while `_rebuild` is actively executing
        # inside it, so `status` can report "a rebuild is in flight" with
        # no separate flag to keep in sync.
        with self._lock:
            return self._timer is not None and self._timer.is_alive()

    def on_any_event(self, event) -> None:
        if event.is_directory:
            return
        paths = [event.src_path]
        dest_path = getattr(event, "dest_path", "")
        if dest_path:
            paths.append(dest_path)
        if any(_is_watched_path(p) for p in paths):
            self._schedule_rebuild()


@dataclass
class WatchHandle:
    """Handle to a running file watcher. `is_pending()` reports whether a
    debounced or reconciliation rebuild is currently counting down or
    in-flight -- e.g. for the `status` MCP tool to report freshness."""

    _observer: Observer
    _handler: _DebouncedRebuildHandler

    def is_pending(self) -> bool:
        return self._handler.is_pending()

    def stop(self) -> None:
        self._observer.stop()
        self._observer.join()


def start_watching(
    project_root: Path,
    db_path: Path,
    debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS,
    reconcile_on_start: bool = True,
) -> WatchHandle:
    """Start a background OS-level file watcher (FSEvents/inotify/
    ReadDirectoryChangesW via `watchdog`) over `project_root`, rebuilding
    `db_path` `debounce_seconds` after the last relevant `.gd`/`.tscn`/
    `project.godot` change. Returns a `WatchHandle`; call `.stop()` on it
    to shut the watcher down.

    A live OS file watcher only ever sees events from the moment it starts
    -- it has no way to know about edits made *before* that, e.g. the
    common case of an MCP client spawning a fresh server process each
    session while the project was edited in between sessions (in the
    Godot editor, another tool, or just with the AI assistant not running).
    `reconcile_on_start` closes that gap: it immediately schedules one
    rebuild (not blocking the caller) to catch up on any such offline
    changes before the live watcher's first event even arrives. This is
    cheap to do unconditionally thanks to the parse cache (a rebuild where
    nothing actually changed is mostly cache hits)."""
    handler = _DebouncedRebuildHandler(project_root, db_path, debounce_seconds)
    observer = Observer()
    observer.schedule(handler, str(project_root), recursive=True)
    observer.daemon = True
    observer.start()
    if reconcile_on_start:
        handler._schedule_rebuild(delay=0)
    return WatchHandle(observer, handler)
