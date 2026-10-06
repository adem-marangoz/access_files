"""Directory monitoring, debouncing and grouping of filesystem events.

AI coding agents often write the same file many times within a few
seconds. Instead of notifying on every event, events are collected per
project into a :class:`ChangeBatch`. A batch is flushed once the project
has been quiet for ``debounce_seconds`` (or, under continuous activity,
after ``max_batch_seconds`` at the latest).
"""

from __future__ import annotations

import enum
import fnmatch
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

log = logging.getLogger(__name__)

# Paths that are almost never interesting and would otherwise cause noise.
DEFAULT_IGNORE = [
    ".git/",
    ".svn/",
    ".hg/",
    "__pycache__/",
    "node_modules/",
    ".vs/",
    ".idea/",
    ".pytest_cache/",
    ".mypy_cache/",
    "*.pyc",
    "*.pyo",
    "*.swp",
    "*.swo",
    "*.swx",
    "*~",
    ".#*",
    "#*#",
    "*.tmp",
    "*.temp",
    "~$*",
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
]

# Files inside .git whose change means "HEAD or a branch moved".
_GIT_META_FILES = (".git/HEAD", ".git/packed-refs")
_GIT_META_PREFIX = ".git/refs/heads/"


class ChangeKind(str, enum.Enum):
    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"


@dataclass
class FileChange:
    path: str  # relative to the project root, "/"-separated
    kind: ChangeKind
    old_path: str | None = None  # previous path for renames
    is_dir: bool = False


class PathFilter:
    """Gitignore-like matching of project-relative paths.

    * ``name/`` matches a directory with that name at any depth.
    * ``*.o`` (no slash) matches any file or directory name at any depth.
    * ``Debug/*.o`` (contains a slash) is matched against the whole
      relative path; a leading ``**/`` also matches at the root.
    """

    def __init__(self, patterns: Iterable[str], *, case_sensitive: bool | None = None) -> None:
        self.case_sensitive = (os.name != "nt") if case_sensitive is None else case_sensitive
        self._dir_patterns: list[str] = []
        self._path_patterns: list[str] = []
        self._name_patterns: list[str] = []
        for raw in patterns:
            pattern = raw.strip().replace("\\", "/")
            if not pattern or pattern.startswith("#"):
                continue
            if not self.case_sensitive:
                pattern = pattern.lower()
            if pattern.endswith("/"):
                self._dir_patterns.append(pattern.rstrip("/").lstrip("/"))
            elif "/" in pattern.lstrip("/"):
                self._path_patterns.append(pattern.lstrip("/"))
            else:
                self._name_patterns.append(pattern.lstrip("/"))

    def matches(self, rel_path: str, *, is_dir: bool = False) -> bool:
        rel = rel_path.replace("\\", "/").strip("/")
        if not rel:
            return False
        if not self.case_sensitive:
            rel = rel.lower()
        parts = rel.split("/")
        dir_parts = parts if is_dir else parts[:-1]

        for pattern in self._name_patterns:
            if any(fnmatch.fnmatchcase(part, pattern) for part in parts):
                return True
        for pattern in self._dir_patterns:
            if "/" in pattern:
                if self._match_path(rel, pattern) or self._match_path(rel, pattern + "/*"):
                    return True
            elif any(fnmatch.fnmatchcase(part, pattern) for part in dir_parts):
                return True
        for pattern in self._path_patterns:
            if self._match_path(rel, pattern):
                return True
        return False

    @staticmethod
    def _match_path(rel: str, pattern: str) -> bool:
        if fnmatch.fnmatchcase(rel, pattern):
            return True
        if pattern.startswith("**/"):
            return fnmatch.fnmatchcase(rel, pattern[3:])
        return False


def _merge(previous: ChangeKind | None, new: ChangeKind) -> ChangeKind | None:
    """Combine two events on the same path. ``None`` means "no net change"."""
    if previous is None:
        return new
    if previous is ChangeKind.CREATED:
        if new is ChangeKind.DELETED:
            return None  # transient file
        return ChangeKind.CREATED
    if previous is ChangeKind.DELETED:
        if new in (ChangeKind.CREATED, ChangeKind.MODIFIED):
            return ChangeKind.MODIFIED  # e.g. editors that save by delete + recreate
        return ChangeKind.DELETED
    if previous is ChangeKind.RENAMED:
        return ChangeKind.DELETED if new is ChangeKind.DELETED else ChangeKind.RENAMED
    # previous is MODIFIED
    if new is ChangeKind.CREATED:
        return ChangeKind.MODIFIED
    return new


class ChangeBatch:
    """Coalesced changes of one project during one debounce window."""

    def __init__(self, project: str, now: float, wall_time: float | None = None) -> None:
        self.project = project
        self.changes: dict[str, FileChange] = {}
        self.git_meta = False  # HEAD / branch refs changed inside .git
        self.first_event = now
        self.last_event = now
        self.started_wall = time.time() if wall_time is None else wall_time
        self.last_wall = self.started_wall
        self.event_count = 0

    def __bool__(self) -> bool:
        return bool(self.changes) or self.git_meta

    def add(self, kind: ChangeKind, path: str, *, is_dir: bool = False) -> None:
        existing = self.changes.get(path)
        if existing is not None and existing.kind is ChangeKind.RENAMED and kind is ChangeKind.DELETED:
            # a -> b, then b deleted: net effect is "a deleted".
            del self.changes[path]
            if existing.old_path:
                self.add(ChangeKind.DELETED, existing.old_path, is_dir=is_dir)
            return
        merged = _merge(existing.kind if existing else None, kind)
        if merged is None:
            self.changes.pop(path, None)
            return
        old_path = existing.old_path if existing and merged is ChangeKind.RENAMED else None
        self.changes[path] = FileChange(path, merged, old_path, is_dir or bool(existing and existing.is_dir))

    def add_move(self, src: str, dest: str, *, is_dir: bool = False) -> None:
        source = self.changes.pop(src, None)
        if source is not None and source.kind is ChangeKind.CREATED:
            # Created and renamed within one window: it's simply a new file.
            self.add(ChangeKind.CREATED, dest, is_dir=is_dir)
            return
        original = src
        if source is not None and source.kind is ChangeKind.RENAMED and source.old_path:
            original = source.old_path
        if original == dest:
            self.add(ChangeKind.MODIFIED, dest, is_dir=is_dir)
            return
        self.changes[dest] = FileChange(dest, ChangeKind.RENAMED, original, is_dir)

    def sorted_changes(self) -> list[FileChange]:
        return [self.changes[k] for k in sorted(self.changes)]


class Debouncer:
    """Groups events per key and flushes a key after a quiet period.

    ``on_flush(key, batch)`` is called from the debouncer thread.
    """

    def __init__(
        self,
        quiet_seconds: float,
        max_delay_seconds: float,
        on_flush: Callable[[str, ChangeBatch], None],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.quiet_seconds = quiet_seconds
        self.max_delay_seconds = max(max_delay_seconds, quiet_seconds)
        self.on_flush = on_flush
        self._clock = clock
        self._batches: dict[str, ChangeBatch] = {}
        self._suppressed: set[str] = set()
        self._quiet_until: dict[str, float] = {}
        self._cond = threading.Condition()
        self._stopped = False
        self._thread: threading.Thread | None = None

    def record(self, key: str, update: Callable[[ChangeBatch], None]) -> None:
        """Apply *update* to the pending batch of *key* (thread-safe)."""
        with self._cond:
            now = self._clock()
            if key in self._suppressed or now < self._quiet_until.get(key, 0.0):
                return
            batch = self._batches.get(key)
            if batch is None:
                batch = ChangeBatch(key, now)
                self._batches[key] = batch
            update(batch)
            batch.last_event = now
            batch.last_wall = time.time()
            batch.event_count += 1
            self._cond.notify()

    def suppress(self, key: str) -> None:
        """Ignore events of *key* until :meth:`release` (e.g. during a build)."""
        with self._cond:
            self._suppressed.add(key)
            self._batches.pop(key, None)

    def release(self, key: str, grace_seconds: float = 1.0) -> None:
        """Stop ignoring *key*, after a short grace period for late events."""
        with self._cond:
            self._suppressed.discard(key)
            self._batches.pop(key, None)
            self._quiet_until[key] = self._clock() + grace_seconds

    def pending(self, key: str) -> bool:
        with self._cond:
            return key in self._batches

    def _deadline(self, batch: ChangeBatch) -> float:
        return min(batch.last_event + self.quiet_seconds, batch.first_event + self.max_delay_seconds)

    def pop_due(self, now: float | None = None) -> list[tuple[str, ChangeBatch]]:
        """Remove and return batches whose deadline has passed."""
        with self._cond:
            now = self._clock() if now is None else now
            due = [k for k, b in self._batches.items() if self._deadline(b) <= now]
            return [(k, self._batches.pop(k)) for k in due]

    def pop_all(self) -> list[tuple[str, ChangeBatch]]:
        with self._cond:
            items = list(self._batches.items())
            self._batches.clear()
            return items

    def _wait_timeout(self) -> float | None:
        if not self._batches:
            return None
        next_deadline = min(self._deadline(b) for b in self._batches.values())
        return max(0.0, next_deadline - self._clock())

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="debouncer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self._cond:
            self._stopped = True
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while True:
            with self._cond:
                if self._stopped:
                    return
                due = self.pop_due()
                if not due:
                    self._cond.wait(self._wait_timeout())
                    continue
            for key, batch in due:
                if not batch:
                    continue  # everything cancelled out (e.g. temp files)
                try:
                    self.on_flush(key, batch)
                except Exception:  # pragma: no cover - defensive
                    log.exception("Error while flushing changes of %s", key)


class ProjectEventHandler(FileSystemEventHandler):
    """Translates watchdog events of one project into batch updates."""

    def __init__(
        self,
        key: str,
        root: Path,
        path_filter: PathFilter,
        record: Callable[[str, Callable[[ChangeBatch], None]], None],
        *,
        track_git_meta: bool = False,
        ignored_paths: Iterable[Path] = (),
    ) -> None:
        self.key = key
        self.root = os.path.abspath(root)
        self.path_filter = path_filter
        self.record = record
        self.track_git_meta = track_git_meta
        self.ignored_paths = [os.path.normcase(os.path.abspath(p)) for p in ignored_paths]

    def on_any_event(self, event: FileSystemEvent) -> None:
        try:
            self._handle(event)
        except Exception:
            log.exception("Failed to handle filesystem event %r", event)

    def _relative(self, path: str | bytes) -> str | None:
        absolute = os.path.abspath(os.fsdecode(path))
        folded = os.path.normcase(absolute)
        for ignored in self.ignored_paths:
            if folded == ignored or folded.startswith(ignored + os.sep):
                return None
        try:
            # relpath keeps the case of *absolute*; normcase would lowercase on Windows.
            rel = os.path.relpath(absolute, self.root)
        except ValueError:  # different drive on Windows
            return None
        if rel in (".", "..") or rel.startswith(".." + os.sep):
            return None
        return rel.replace(os.sep, "/")

    def _is_git_meta(self, rel: str | None) -> bool:
        if not self.track_git_meta or rel is None:
            return False
        return rel in _GIT_META_FILES or rel.startswith(_GIT_META_PREFIX)

    def _wanted(self, rel: str | None, is_dir: bool) -> bool:
        return rel is not None and not self.path_filter.matches(rel, is_dir=is_dir)

    def _handle(self, event: FileSystemEvent) -> None:
        etype = event.event_type
        if etype not in ("created", "modified", "deleted", "moved"):
            return  # opened / closed / closed_no_write
        is_dir = event.is_directory
        src = self._relative(event.src_path)
        dest = self._relative(event.dest_path) if etype == "moved" and event.dest_path else None

        if self._is_git_meta(src) or self._is_git_meta(dest):
            self.record(self.key, _mark_git_meta)
            return

        if is_dir and etype == "modified":
            return  # directory mtime changes whenever its contents change

        if etype == "moved":
            src_ok, dest_ok = self._wanted(src, is_dir), self._wanted(dest, is_dir)
            if src_ok and dest_ok:
                self.record(self.key, lambda b: b.add_move(src, dest, is_dir=is_dir))
            elif dest_ok:
                # Temp file renamed over the real file ("atomic save").
                self.record(self.key, lambda b: b.add(ChangeKind.MODIFIED, dest, is_dir=is_dir))
            elif src_ok:
                self.record(self.key, lambda b: b.add(ChangeKind.DELETED, src, is_dir=is_dir))
            return

        if not self._wanted(src, is_dir):
            return
        kind = ChangeKind(etype)
        self.record(self.key, lambda b: b.add(kind, src, is_dir=is_dir))


def _mark_git_meta(batch: ChangeBatch) -> None:
    batch.git_meta = True


class WatchManager:
    """Owns the watchdog observer and one handler per project."""

    def __init__(self, debouncer: Debouncer) -> None:
        self.debouncer = debouncer
        self._observer = Observer()
        self._started = False

    def add_project(
        self,
        key: str,
        root: Path,
        ignore_patterns: Iterable[str],
        *,
        track_git_meta: bool = False,
        ignored_paths: Iterable[Path] = (),
    ) -> ProjectEventHandler:
        handler = ProjectEventHandler(
            key,
            root,
            PathFilter(ignore_patterns),
            self.debouncer.record,
            track_git_meta=track_git_meta,
            ignored_paths=ignored_paths,
        )
        self._observer.schedule(handler, str(root), recursive=True)
        log.info("Watching %s (%s)", root, key)
        return handler

    def start(self) -> None:
        self._observer.start()
        self._started = True

    def stop(self) -> None:
        if self._started:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._started = False


def build_ignore_patterns(use_defaults: bool, *extra: Iterable[str], git_project: bool = False) -> list[str]:
    patterns: list[str] = []
    if use_defaults:
        patterns.extend(DEFAULT_IGNORE)
    for group in extra:
        patterns.extend(group)
    if git_project and ".git/" not in patterns:
        patterns.append(".git/")
    return patterns
