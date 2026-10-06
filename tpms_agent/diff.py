"""Change summaries, ``.patch`` documents and short diff previews."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .git_monitor import GitRepo, GitSnapshot, LineStats
from .telegram import Document, escape
from .watcher import ChangeKind, FileChange

_LETTER_FOR_KIND = {
    ChangeKind.CREATED: "A",
    ChangeKind.MODIFIED: "M",
    ChangeKind.DELETED: "D",
    ChangeKind.RENAMED: "R",
}


@dataclass
class FileSummary:
    path: str  # relative to the project root
    letter: str  # A (added), M (modified), D (deleted), R (renamed), T, U
    old_path: str | None = None
    added: int | None = None  # None: unknown or binary
    deleted: int | None = None
    binary: bool = False
    abs_path: Path | None = None
    repo_path: str | None = None
    untracked: bool = False
    is_dir: bool = False

    @property
    def display_path(self) -> str:
        return self.path + "/" if self.is_dir else self.path


@dataclass
class ChangeSummary:
    project: str
    files: list[FileSummary] = field(default_factory=list)
    is_git: bool = False
    branch: str | None = None
    previous_branch: str | None = None
    new_commits: list[str] = field(default_factory=list)
    patch: str = ""
    worktree_files: int | None = None  # uncommitted files in the project
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def insertions(self) -> int:
        return sum(f.added or 0 for f in self.files)

    @property
    def deletions(self) -> int:
        return sum(f.deleted or 0 for f in self.files)

    @property
    def has_line_stats(self) -> bool:
        return self.is_git and any(f.added is not None for f in self.files)

    def __bool__(self) -> bool:
        return bool(self.files or self.new_commits or self.previous_branch)


def summarize_plain(project: str, root: Path, changes: Iterable[FileChange]) -> ChangeSummary:
    """Summary of a non-Git folder: file names and event types only."""
    files = [
        FileSummary(
            path=c.path,
            letter=_LETTER_FOR_KIND[c.kind],
            old_path=c.old_path,
            abs_path=root / Path(*c.path.split("/")),
            is_dir=c.is_dir,
        )
        for c in changes
    ]
    return ChangeSummary(project=project, files=files)


def summarize_git(
    repo: GitRepo,
    project: str,
    snapshot: GitSnapshot,
    *,
    files: Iterable[str] | None = None,
    dirs: Iterable[str] = (),
    renames: dict[str, str] | None = None,
    base: str | None = None,
    include_patch: bool = False,
) -> ChangeSummary:
    """Summarize changes of a Git project versus *base* (default: HEAD).

    *files* and *dirs* are repository-relative paths limiting the summary
    (``files=None`` and no *dirs* means the whole project). Tracked files
    are compared with *base*; untracked files count as added.
    *renames* maps new -> old paths that the watcher saw being renamed.
    """
    file_set = set(files) if files is not None else None
    dir_set = set(dirs)
    pathspec: list[str] | None
    if file_set is None and not dir_set:
        pathspec = None
    else:
        pathspec = sorted((file_set or set()) | dir_set)

    def in_scope(path: str) -> bool:
        # Like a git pathspec: an entry matches itself and everything below it
        # (a deleted directory may have been reported as a plain path).
        if pathspec is None:
            return True
        return any(path == p or path.startswith(p + "/") for p in pathspec)

    letters = repo.name_status(pathspec, base=base)
    stats = repo.numstat(pathspec, base=base)
    untracked = [e.path for e in snapshot.entries if e.untracked and in_scope(e.path)]

    by_path: dict[str, FileSummary] = {}
    for path, letter in letters.items():
        line_stats = stats.get(path, LineStats(0, 0))
        by_path[path] = _file_summary(repo, path, letter, line_stats)
    for path in untracked:
        if path not in by_path:
            summary = _file_summary(repo, path, "A", repo.untracked_stats(path))
            summary.untracked = True
            by_path[path] = summary

    for new, old in (renames or {}).items():
        added, removed = by_path.get(new), by_path.get(old)
        if added and removed and added.letter == "A" and removed.letter == "D":
            del by_path[old]
            added.letter = "R"
            added.old_path = removed.path
            if added.added is not None and removed.added is not None:
                added.deleted = (added.deleted or 0) + (removed.deleted or 0)

    patch = ""
    if include_patch and by_path:
        tracked = [p for p in by_path if not by_path[p].untracked] + [
            old for old in (renames or {}).values() if old in letters and old not in by_path
        ]
        patch = repo.diff(sorted(tracked), base=base) if tracked else ""
        patch += "".join(repo.untracked_patch(p) for p in sorted(p for p in by_path if by_path[p].untracked))

    return ChangeSummary(
        project=project,
        files=[by_path[p] for p in sorted(by_path)],
        is_git=True,
        branch=snapshot.branch,
        patch=patch,
        worktree_files=len(snapshot.entries),
    )


def _file_summary(repo: GitRepo, path: str, letter: str, stats: LineStats) -> FileSummary:
    return FileSummary(
        path=repo.to_project_path(path),
        letter=letter,
        added=stats.added,
        deleted=stats.deleted,
        binary=stats.binary,
        abs_path=repo.file_path(path),
        repo_path=path,
    )


def format_line_stats(added: int | None, deleted: int | None) -> str:
    if added is None or deleted is None:
        return "binary"
    return f"+{added} -{deleted}"


def slugify(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return slug or "project"


def make_patch_document(summary: ChangeSummary, *, max_bytes: int) -> Document | None:
    """The full diff as a ``.patch`` attachment (truncated to *max_bytes*)."""
    if not summary.patch:
        return None
    stamp = summary.timestamp
    header = [f"# Project: {summary.project}"]
    if summary.branch:
        header.append(f"# Branch: {summary.branch}")
    header.append(f"# Generated: {stamp:%Y-%m-%d %H:%M:%S}")
    header.append(f"# Files: {len(summary.files)}, +{summary.insertions} -{summary.deletions}")
    text = "\n".join(header) + "\n\n" + summary.patch
    data = text.encode("utf-8", "surrogateescape")
    caption = None
    if len(data) > max_bytes:
        marker = b"\n\n# ... diff truncated (size limit) ...\n"
        data = data[: max(0, max_bytes - len(marker))] + marker
        caption = "⚠️ Diff truncated to the configured size limit"
    filename = f"changes-{slugify(summary.project)}-{stamp:%Y%m%d-%H%M%S}.patch"
    return Document(filename, data, caption)


def make_file_documents(
    summary: ChangeSummary,
    *,
    max_files: int,
    max_bytes: int,
) -> tuple[list[Document], list[str]]:
    """Modified files as attachments. Returns (documents, skipped notes)."""
    documents: list[Document] = []
    skipped: list[str] = []
    for item in summary.files:
        if item.letter == "D" or item.is_dir or item.abs_path is None:
            continue
        if len(documents) >= max_files:
            skipped.append(f"{item.path} (file limit reached)")
            continue
        try:
            size = item.abs_path.stat().st_size
            if size > max_bytes:
                skipped.append(f"{item.path} ({size // 1024} KB, too large)")
                continue
            data = item.abs_path.read_bytes()
        except OSError as exc:
            skipped.append(f"{item.path} ({exc.strerror or 'unreadable'})")
            continue
        documents.append(Document(item.abs_path.name, data, f"<code>{escape(item.path)}</code>"))
    return documents, skipped


def diff_preview(patch: str, max_lines: int, *, max_chars: int = 2500, max_line_chars: int = 160) -> str | None:
    """A compact version of *patch* for inline display.

    File headers are reduced to one line; returns ``None`` if there is
    nothing to show. Long previews are cut with a "see attachment" marker.
    """
    if not patch or max_lines <= 0:
        return None
    lines: list[str] = []
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            match = re.match(r"diff --git a/(.*?) b/(.*)$", line)
            lines.append(f"── {match.group(2) if match else line[11:]} ──")
        elif line.startswith(("index ", "--- ", "+++ ", "new file mode", "deleted file mode", "old mode", "new mode", "similarity", "rename ")):
            continue
        else:
            lines.append(line if len(line) <= max_line_chars else line[: max_line_chars - 1] + "…")
    if not lines:
        return None
    truncated = False
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        truncated = True
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars].rsplit("\n", 1)[0]
        truncated = True
    if truncated:
        text += "\n… (truncated)"
    return text
