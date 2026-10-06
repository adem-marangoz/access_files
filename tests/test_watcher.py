from __future__ import annotations

import os
import threading
from pathlib import Path

from watchdog.events import (
    DirModifiedEvent,
    FileClosedEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
)

from tpms_agent.watcher import (
    DEFAULT_IGNORE,
    ChangeBatch,
    ChangeKind,
    Debouncer,
    PathFilter,
    ProjectEventHandler,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# -- PathFilter --------------------------------------------------------------


def test_default_ignores():
    f = PathFilter(DEFAULT_IGNORE, case_sensitive=True)
    assert f.matches(".git/index")
    assert f.matches("src/__pycache__/x.pyc")
    assert f.matches("src/.main.c.swp")
    assert f.matches("Sources/CAN.c~")
    assert f.matches("docs/~$report.docx")
    assert not f.matches("Sources/CAN_ResponseTask.c")
    assert not f.matches("gitignore.txt")


def test_pattern_kinds():
    f = PathFilter(["Debug/", "*.o", "build/out/*.map", "**/gen.h", "/top.txt"], case_sensitive=True)
    assert f.matches("Debug/main.elf")
    assert f.matches("sub/Debug/x.c")
    assert not f.matches("Debug")  # a file named Debug is not the directory
    assert f.matches("Debug", is_dir=True)
    assert f.matches("src/main.o")
    assert f.matches("build/out/app.map")
    assert not f.matches("other/build/out/app.map")
    assert f.matches("gen.h") and f.matches("a/b/gen.h")
    assert f.matches("top.txt")
    assert not f.matches("src/main.c")


def test_case_insensitive_matching():
    f = PathFilter(["debug/"], case_sensitive=False)
    assert f.matches("DEBUG/x.o")


# -- ChangeBatch coalescing --------------------------------------------------


def kinds(batch: ChangeBatch) -> dict[str, tuple[str, str | None]]:
    return {p: (c.kind.value, c.old_path) for p, c in batch.changes.items()}


def test_repeated_modifications_collapse():
    b = ChangeBatch("p", 0)
    for _ in range(5):
        b.add(ChangeKind.MODIFIED, "a.c")
    b.add(ChangeKind.MODIFIED, "b.c")
    assert kinds(b) == {"a.c": ("modified", None), "b.c": ("modified", None)}


def test_created_then_deleted_cancels_out():
    b = ChangeBatch("p", 0)
    b.add(ChangeKind.CREATED, "tmp.c")
    b.add(ChangeKind.MODIFIED, "tmp.c")
    b.add(ChangeKind.DELETED, "tmp.c")
    assert not b


def test_delete_and_recreate_is_a_modification():
    b = ChangeBatch("p", 0)
    b.add(ChangeKind.DELETED, "a.c")
    b.add(ChangeKind.CREATED, "a.c")
    assert kinds(b) == {"a.c": ("modified", None)}


def test_rename_tracking():
    b = ChangeBatch("p", 0)
    b.add_move("a.c", "b.c")
    b.add(ChangeKind.MODIFIED, "b.c")
    assert kinds(b) == {"b.c": ("renamed", "a.c")}
    b.add_move("b.c", "c.c")
    assert kinds(b) == {"c.c": ("renamed", "a.c")}
    b.add_move("c.c", "a.c")  # renamed back
    assert kinds(b) == {"a.c": ("modified", None)}


def test_renamed_then_deleted_is_original_deleted():
    b = ChangeBatch("p", 0)
    b.add_move("a.c", "b.c")
    b.add(ChangeKind.DELETED, "b.c")
    assert kinds(b) == {"a.c": ("deleted", None)}


def test_new_file_renamed_stays_new():
    b = ChangeBatch("p", 0)
    b.add(ChangeKind.CREATED, "draft.c")
    b.add_move("draft.c", "final.c")
    assert kinds(b) == {"final.c": ("created", None)}


# -- Debouncer ---------------------------------------------------------------


def test_debounce_waits_for_quiet_period():
    clock = FakeClock()
    d = Debouncer(5, 60, lambda k, b: None, clock=clock)
    for second in range(4):
        clock.now = 1000 + second
        d.record("p", lambda b: b.add(ChangeKind.MODIFIED, "a.c"))
    clock.now = 1007.9  # last event at 1003 -> due at 1008
    assert d.pop_due() == []
    clock.now = 1008
    [(key, batch)] = d.pop_due()
    assert key == "p" and batch.event_count == 4 and list(batch.changes) == ["a.c"]
    assert d.pop_due() == []


def test_max_delay_flushes_during_continuous_activity():
    clock = FakeClock()
    d = Debouncer(5, 20, lambda k, b: None, clock=clock)
    flushed = []
    for second in range(0, 30, 2):
        clock.now = 1000 + second
        flushed += d.pop_due()
        d.record("p", lambda b: b.add(ChangeKind.MODIFIED, "a.c"))
    assert len(flushed) == 1
    assert flushed[0][1].first_event == 1000


def test_projects_are_debounced_independently():
    clock = FakeClock()
    d = Debouncer(5, 60, lambda k, b: None, clock=clock)
    d.record("a", lambda b: b.add(ChangeKind.MODIFIED, "x"))
    clock.now += 3
    d.record("b", lambda b: b.add(ChangeKind.MODIFIED, "y"))
    clock.now += 2
    assert [k for k, _ in d.pop_due()] == ["a"]
    clock.now += 3
    assert [k for k, _ in d.pop_due()] == ["b"]


def test_suppress_and_release():
    clock = FakeClock()
    d = Debouncer(1, 10, lambda k, b: None, clock=clock)
    d.record("p", lambda b: b.add(ChangeKind.MODIFIED, "before.c"))
    d.suppress("p")
    d.record("p", lambda b: b.add(ChangeKind.CREATED, "build.o"))
    assert not d.pending("p")
    d.release("p", grace_seconds=1)
    d.record("p", lambda b: b.add(ChangeKind.CREATED, "late.o"))
    assert not d.pending("p")
    clock.now += 2
    d.record("p", lambda b: b.add(ChangeKind.MODIFIED, "after.c"))
    assert d.pending("p")


def test_debouncer_thread_calls_on_flush():
    flushed = threading.Event()
    received = []

    def on_flush(key, batch):
        received.append((key, sorted(batch.changes)))
        flushed.set()

    d = Debouncer(0.1, 1, on_flush)
    d.start()
    try:
        d.record("p", lambda b: b.add(ChangeKind.MODIFIED, "a.c"))
        assert flushed.wait(3)
    finally:
        d.stop()
    assert received == [("p", ["a.c"])]


def test_empty_batches_are_not_flushed():
    received = []
    flushed = threading.Event()
    d = Debouncer(0.05, 1, lambda k, b: (received.append(k), flushed.set()))
    d.start()
    try:
        d.record("p", lambda b: b.add(ChangeKind.CREATED, "tmp"))
        d.record("p", lambda b: b.add(ChangeKind.DELETED, "tmp"))
        assert not flushed.wait(0.5)
    finally:
        d.stop()
    assert received == []


# -- event handler -----------------------------------------------------------


def make_handler(root: Path, **kwargs):
    batch = ChangeBatch("p", 0)
    handler = ProjectEventHandler(
        "p", root, PathFilter(DEFAULT_IGNORE + ["*.o"]), lambda key, update: update(batch), **kwargs
    )
    return handler, batch


def test_handler_translates_events(tmp_path: Path):
    handler, batch = make_handler(tmp_path)
    p = lambda rel: os.path.join(str(tmp_path), *rel.split("/"))  # noqa: E731
    handler.dispatch(FileModifiedEvent(p("src/a.c")))
    handler.dispatch(FileCreatedEvent(p("src/new.c")))
    handler.dispatch(FileDeletedEvent(p("old.c")))
    handler.dispatch(FileMovedEvent(p("x.c"), p("y.c")))
    handler.dispatch(FileModifiedEvent(p("main.o")))  # ignored pattern
    handler.dispatch(FileModifiedEvent(p(".git/index")))  # ignored
    handler.dispatch(DirModifiedEvent(p("src")))  # directory mtime noise
    handler.dispatch(FileClosedEvent(p("src/a.c")))  # not a change
    assert kinds(batch) == {
        "src/a.c": ("modified", None),
        "src/new.c": ("created", None),
        "old.c": ("deleted", None),
        "y.c": ("renamed", "x.c"),
    }
    assert not batch.git_meta


def test_atomic_save_through_temp_file(tmp_path: Path):
    handler, batch = make_handler(tmp_path)
    handler.dispatch(FileCreatedEvent(str(tmp_path / "a.c.tmp")))
    handler.dispatch(FileMovedEvent(str(tmp_path / "a.c.tmp"), str(tmp_path / "a.c")))
    assert kinds(batch) == {"a.c": ("modified", None)}


def test_git_meta_events(tmp_path: Path):
    handler, batch = make_handler(tmp_path, track_git_meta=True)
    handler.dispatch(FileModifiedEvent(str(tmp_path / ".git" / "index")))
    assert not batch
    handler.dispatch(FileMovedEvent(str(tmp_path / ".git" / "HEAD.lock"), str(tmp_path / ".git" / "HEAD")))
    assert batch.git_meta and not batch.changes


def test_agent_log_file_is_ignored(tmp_path: Path):
    log_file = tmp_path / "logs" / "agent.log"
    handler, batch = make_handler(tmp_path, ignored_paths=[log_file])
    handler.dispatch(FileModifiedEvent(str(log_file)))
    assert not batch
