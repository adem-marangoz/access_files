from __future__ import annotations

from datetime import datetime
from pathlib import Path

from conftest import git, requires_git

from tpms_agent.diff import (
    ChangeSummary,
    FileSummary,
    diff_preview,
    make_file_documents,
    make_patch_document,
    summarize_git,
    summarize_plain,
)
from tpms_agent.git_monitor import GitRepo
from tpms_agent.watcher import ChangeKind, FileChange


@requires_git
def test_summarize_only_touched_files(make_repo):
    repo_path = make_repo(files={"a.c": "1\n", "b.c": "1\n", "ignored.log": "x\n"})
    (repo_path / "a.c").write_text("1\n2\n")
    (repo_path / "b.c").write_text("changed\n")
    (repo_path / "c.c").write_text("new\nfile\n")
    repo = GitRepo.discover(repo_path)
    summary = summarize_git(repo, "P", repo.snapshot(), files=["a.c", "c.c", "unchanged.c"], include_patch=True)
    assert [(f.path, f.letter, f.added, f.deleted) for f in summary.files] == [
        ("a.c", "M", 1, 0),
        ("c.c", "A", 2, 0),
    ]
    assert summary.insertions == 3 and summary.deletions == 0
    assert summary.branch == "main"
    assert summary.worktree_files == 3
    assert "+2" in summary.patch and "+++ b/c.c" in summary.patch and "b.c" not in summary.patch


@requires_git
def test_whole_project_and_deleted_directory(make_repo):
    repo_path = make_repo(files={"lib/x.c": "1\n", "lib/y.c": "1\n", "main.c": "1\n"})
    for name in ("x.c", "y.c"):
        (repo_path / "lib" / name).unlink()
    (repo_path / "lib").rmdir()
    repo = GitRepo.discover(repo_path)
    # A deleted directory may be reported as a plain path by the watcher.
    summary = summarize_git(repo, "P", repo.snapshot(), files=["lib"])
    assert [(f.path, f.letter) for f in summary.files] == [("lib/x.c", "D"), ("lib/y.c", "D")]
    whole = summarize_git(repo, "P", repo.snapshot())
    assert len(whole.files) == 2


@requires_git
def test_watcher_rename_is_combined(make_repo):
    repo_path = make_repo(files={"old.c": "a\nb\n"})
    (repo_path / "old.c").rename(repo_path / "new.c")
    repo = GitRepo.discover(repo_path)
    summary = summarize_git(
        repo, "P", repo.snapshot(), files=["new.c", "old.c"], renames={"new.c": "old.c"}, include_patch=True
    )
    [item] = summary.files
    assert (item.letter, item.path, item.old_path) == ("R", "new.c", "old.c")
    assert "deleted file mode" in summary.patch  # old side is part of the patch


@requires_git
def test_task_summary_against_start_commit(make_repo):
    repo_path = make_repo(files={"a.c": "1\n"})
    repo = GitRepo.discover(repo_path)
    start = repo.head()
    (repo_path / "a.c").write_text("1\n2\n")
    git(repo_path, "commit", "-qam", "agent commit")
    summary = summarize_git(repo, "P", repo.snapshot(), files=["a.c"], base=start)
    assert [(f.path, f.added) for f in summary.files] == [("a.c", 1)]
    assert summarize_git(repo, "P", repo.snapshot(), files=["a.c"]).files == []


def test_plain_summary(tmp_path: Path):
    changes = [
        FileChange("src/a.c", ChangeKind.MODIFIED),
        FileChange("b.c", ChangeKind.RENAMED, old_path="old_b.c"),
        FileChange("gen", ChangeKind.CREATED, is_dir=True),
    ]
    summary = summarize_plain("P", tmp_path, changes)
    assert [(f.display_path, f.letter, f.old_path) for f in summary.files] == [
        ("src/a.c", "M", None),
        ("b.c", "R", "old_b.c"),
        ("gen/", "A", None),
    ]
    assert summary.files[0].abs_path == tmp_path / "src" / "a.c"
    assert not summary.has_line_stats


def test_patch_document_and_truncation():
    summary = ChangeSummary(project="TPMS ECU", branch="dev", patch="diff --git a/x b/x\n" + "+line\n" * 100,
                            timestamp=datetime(2026, 1, 2, 9, 15, 23))
    doc = make_patch_document(summary, max_bytes=100_000)
    assert doc.filename == "changes-tpms-ecu-20260102-091523.patch"
    assert doc.data.startswith(b"# Project: TPMS ECU\n# Branch: dev\n")
    small = make_patch_document(summary, max_bytes=200)
    assert len(small.data) <= 200 and b"truncated" in small.data and small.caption
    assert make_patch_document(ChangeSummary(project="x"), max_bytes=10) is None


def test_file_documents_respect_limits(tmp_path: Path):
    (tmp_path / "a.c").write_text("a")
    (tmp_path / "big.bin").write_bytes(b"x" * 5000)
    (tmp_path / "c.c").write_text("c")
    summary = ChangeSummary(project="P", files=[
        FileSummary("a.c", "M", abs_path=tmp_path / "a.c"),
        FileSummary("gone.c", "D", abs_path=tmp_path / "gone.c"),
        FileSummary("big.bin", "M", abs_path=tmp_path / "big.bin"),
        FileSummary("c.c", "A", abs_path=tmp_path / "c.c"),
        FileSummary("missing.c", "M", abs_path=tmp_path / "missing.c"),
    ])
    docs, skipped = make_file_documents(summary, max_files=5, max_bytes=1000)
    assert [d.filename for d in docs] == ["a.c", "c.c"]
    assert any("big.bin" in s for s in skipped) and any("missing.c" in s for s in skipped)
    docs, skipped = make_file_documents(summary, max_files=1, max_bytes=1000)
    assert [d.filename for d in docs] == ["a.c"]
    assert any("limit" in s for s in skipped)


def test_diff_preview():
    patch = (
        "diff --git a/src/x.c b/src/x.c\nindex 1..2 100644\n--- a/src/x.c\n+++ b/src/x.c\n"
        "@@ -1,2 +1,3 @@\n FLEXCAN_DRV_Send(INST_CANCOM1, TX_MAILBOX_PC, ...);\n+vTaskDelay(pdMS_TO_TICKS(10));\n"
    )
    preview = diff_preview(patch, 20)
    assert preview.splitlines() == [
        "── src/x.c ──",
        "@@ -1,2 +1,3 @@",
        " FLEXCAN_DRV_Send(INST_CANCOM1, TX_MAILBOX_PC, ...);",
        "+vTaskDelay(pdMS_TO_TICKS(10));",
    ]
    assert diff_preview(patch, 2).endswith("… (truncated)")
    assert diff_preview(patch, 0) is None
    assert diff_preview("", 10) is None
