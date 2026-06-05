"""Filesystem watcher that flips a per-repo dirty flag on any change.

The graph cache previously walked the whole repo on every call to
compute the most-recent mtime. That is O(files) per tool call and
dominates latency on medium repos.

`RepoWatcher` replaces that walk with a watchdog observer: a background
thread fires events for every create/modify/delete under the repo, and
we simply set ``dirty=True`` whenever an event passes the ignore
filter. The next ``get_graph`` call sees the flag and rebuilds.
"""

from __future__ import annotations

import logging
from pathlib import Path
from threading import Lock

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from repo_flow_mcp.settings import ScanSettings

_log = logging.getLogger(__name__)


class _DirtyHandler(FileSystemEventHandler):
    def __init__(self, watcher: "RepoWatcher") -> None:
        self._watcher = watcher

    def on_any_event(self, event: FileSystemEvent) -> None:
        # Watchdog reports both file and directory events; we only care
        # about anything that could change the graph build, which is any
        # path not in an ignored directory.
        raw = event.src_path
        path_str = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        if self._watcher._is_ignored(path_str):
            return
        self._watcher.mark_dirty()


class RepoWatcher:
    """Watches a single repo root; exposes a thread-safe dirty flag."""

    def __init__(self, root: Path, settings: ScanSettings) -> None:
        self._root = root
        self._settings = settings
        self._dirty = False
        self._lock = Lock()
        self._observer: Observer | None = None  # type: ignore[valid-type]

    def start(self) -> None:
        if self._observer is not None:
            return
        observer = Observer()
        try:
            observer.schedule(_DirtyHandler(self), str(self._root), recursive=True)
            observer.daemon = True
            observer.start()
        except OSError as exc:
            # Filesystem watching can fail (e.g. inotify limits hit). Fall
            # back to "always dirty" so the cache rebuilds on every call;
            # that matches pre-watcher behaviour and is the safe default.
            _log.warning("repo watcher failed to start for %s: %s", self._root, exc)
            self._observer = None
            with self._lock:
                self._dirty = True
            return
        self._observer = observer

    def stop(self) -> None:
        observer = self._observer
        self._observer = None
        if observer is None:
            return
        try:
            observer.stop()
            observer.join(timeout=2.0)
        except RuntimeError:
            pass

    def mark_dirty(self) -> None:
        with self._lock:
            self._dirty = True

    def consume_dirty(self) -> bool:
        """Atomically read-and-clear the dirty flag."""
        with self._lock:
            was_dirty = self._dirty
            self._dirty = False
        return was_dirty

    def is_running(self) -> bool:
        return self._observer is not None

    def _is_ignored(self, raw_path: str) -> bool:
        try:
            rel = Path(raw_path).resolve().relative_to(self._root)
        except (ValueError, OSError):
            return True
        for part in rel.parts:
            if part in self._settings.ignore_dirs:
                return True
            if (
                not self._settings.include_hidden
                and part.startswith(".")
                and part != ".github"
            ):
                return True
        return False
