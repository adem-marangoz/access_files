"""Git integration: repository detection, branch, status, statistics and diffs.

All paths returned by this module are relative to the repository root and
use "/" separators, exactly like Git prints them. A monitored project may
be a sub-directory of a repository; :attr:`GitRepo.prefix` holds its path
inside the repository and all queries are limited to it.
"""

from __future__ import annotations

import logging
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

log = logging.getLogger(__name__)

# Don't flash a console window for every git call when running windowless.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_PATHSPEC_CHUNK = 100
MAX_UNTRACKED_PATCH_BYTES = 1_000_000
_BINARY_SNIFF_BYTES = 8000


class GitError(RuntimeError):
    """A git command failed."""


@dataclass(frozen=True)
class LineStats:
    added: int | None  # None for binary files
    deleted: int | None

    @property
    def binary(self) -> bool:
        return self.added is None


@dataclass(frozen=True)
class StatusEntry:
    """One line of ``git status --porcelain``."""

    path: str
    index: str
    worktree: str
    orig_path: str | None = None

    @property
    def code(self) -> str:
        return self.index + self.worktree

    @property
    def untracked(self) -> bool:
        return self.code == "??"

    @property
    def kind(self) -> str:
        code = self.code
        if code == "??":
            return "untracked"
        if "U" in code or code in ("AA", "DD"):
            return "conflict"
        if "R" in code:
            return "renamed"
        if "C" in code:
            return "copied"
        if "D" in code:
            return "deleted"
        if self.index == "A":
            return "added"
        return "modified"

    @property
    def letter(self) -> str:
        """Single-letter code for display (A, M, D, R, C, U)."""
        return {
            "untracked": "A",
            "added": "A",
            "conflict": "U",
            "renamed": "R",
            "copied": "C",
            "deleted": "D",
        }.get(self.kind, "M")


@dataclass
class GitSnapshot:
    branch: str
    head: str | None
    entries: list[StatusEntry]


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    # Never take index.lock for a read-only "git status" (it would race with
    # the AI agent / IDE running git at the same time) and never prompt.
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def run_git(
    args: Sequence[str],
    cwd: Path,
    *,
    input: bytes | None = None,
    check: bool = True,
    timeout: float = 60,
    git: str = "git",
) -> subprocess.CompletedProcess[bytes]:
    cmd = [git, "-c", "core.quotepath=false", "-c", "color.ui=false", *args]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            input=input,
            stdin=subprocess.DEVNULL if input is None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=_git_env(),
            creationflags=NO_WINDOW,
        )
    except FileNotFoundError as exc:
        raise GitError(f"git executable not found ({git!r}); is Git installed and on PATH?") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {args[0]} timed out after {timeout:g}s") from exc
    except OSError as exc:
        raise GitError(f"could not run git: {exc}") from exc
    if check and proc.returncode != 0:
        message = proc.stderr.decode("utf-8", "replace").strip() or f"exit code {proc.returncode}"
        raise GitError(f"git {' '.join(args[:2])} failed: {message}")
    return proc


def git_version(git: str = "git") -> str | None:
    try:
        return run_git(["--version"], Path.cwd(), git=git).stdout.decode().strip()
    except GitError:
        return None


def _decode(data: bytes) -> str:
    return data.decode("utf-8", "surrogateescape")


def _literal(paths: Iterable[str]) -> list[str]:
    return [f":(top,literal){p}" for p in paths]


def _chunks(items: Sequence[str], size: int = _PATHSPEC_CHUNK) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def is_binary(data: bytes) -> bool:
    return b"\0" in data[:_BINARY_SNIFF_BYTES]


class GitRepo:
    """Read-only access to the Git repository containing a project."""

    def __init__(self, path: Path, toplevel: Path, prefix: str = "", *, git: str = "git", timeout: float = 60) -> None:
        self.path = Path(path)
        self.toplevel = Path(toplevel)
        self.prefix = prefix.strip("/")
        self.git = git
        self.timeout = timeout

    @classmethod
    def discover(cls, path: Path, *, git: str = "git") -> "GitRepo | None":
        """Return a :class:`GitRepo` if *path* is inside a work tree."""
        try:
            proc = run_git(["rev-parse", "--show-toplevel", "--show-prefix"], path, git=git)
        except GitError as exc:
            log.debug("%s is not a git work tree: %s", path, exc)
            return None
        lines = _decode(proc.stdout).splitlines()
        if not lines or not lines[0].strip():
            return None  # e.g. path is inside the .git directory
        prefix = lines[1].strip() if len(lines) > 1 else ""
        return cls(path, Path(lines[0].strip()), prefix, git=git)

    # -- helpers ---------------------------------------------------------

    def _run(self, args: Sequence[str], **kwargs) -> subprocess.CompletedProcess[bytes]:
        kwargs.setdefault("timeout", self.timeout)
        return run_git(args, self.toplevel, git=self.git, **kwargs)

    def _scope(self) -> list[str]:
        """Pathspec limiting a command to the project directory."""
        return _literal([self.prefix]) if self.prefix else []

    def to_repo_path(self, project_path: str) -> str:
        project_path = project_path.strip("/")
        return f"{self.prefix}/{project_path}" if self.prefix else project_path

    def to_project_path(self, repo_path: str) -> str:
        if self.prefix and repo_path.startswith(self.prefix + "/"):
            return repo_path[len(self.prefix) + 1:]
        return repo_path

    def file_path(self, repo_path: str) -> Path:
        return self.toplevel / Path(*repo_path.split("/"))

    # -- queries ---------------------------------------------------------

    def head(self) -> str | None:
        proc = self._run(["rev-parse", "--verify", "-q", "HEAD"], check=False)
        sha = _decode(proc.stdout).strip()
        return sha if proc.returncode == 0 and sha else None

    def branch(self) -> str:
        name = _decode(self._run(["branch", "--show-current"]).stdout).strip()
        if name:
            return name
        head = self.head()
        return f"detached at {head[:8]}" if head else "(no commits)"

    def status(self) -> list[StatusEntry]:
        proc = self._run(
            ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *self._scope()]
        )
        return parse_status(proc.stdout)

    def snapshot(self) -> GitSnapshot:
        return GitSnapshot(branch=self.branch(), head=self.head(), entries=self.status())

    def empty_tree(self) -> str:
        return _decode(self._run(["hash-object", "-t", "tree", "--stdin"], input=b"").stdout).strip()

    def _diff_runs(self, extra: Sequence[str], repo_paths: Sequence[str] | None, base: str | None) -> Iterable[bytes]:
        # Without commits there is no HEAD: compare against the empty tree.
        base = base or self.head() or self.empty_tree()
        cmd = ["diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--no-color", *extra, base, "--"]
        if repo_paths is None:
            yield self._run([*cmd, *self._scope()]).stdout
            return
        for chunk in _chunks(list(repo_paths)):
            yield self._run([*cmd, *_literal(chunk)]).stdout

    # The diff queries below compare the working tree (staged + unstaged
    # changes) with *base*, which defaults to HEAD. ``repo_paths=None``
    # means the whole project; directories in *repo_paths* match everything
    # below them. Untracked files are not included; see untracked_*().

    def name_status(self, repo_paths: Sequence[str] | None = None, *, base: str | None = None) -> dict[str, str]:
        """Change letter (A, M, D, T, U) of every changed tracked file."""
        if repo_paths is not None and not repo_paths:
            return {}
        result: dict[str, str] = {}
        for output in self._diff_runs(["--name-status", "-z"], repo_paths, base):
            result.update(parse_name_status(output))
        return result

    def numstat(self, repo_paths: Sequence[str] | None = None, *, base: str | None = None) -> dict[str, LineStats]:
        """Inserted/deleted line counts of every changed tracked file."""
        if repo_paths is not None and not repo_paths:
            return {}
        stats: dict[str, LineStats] = {}
        for output in self._diff_runs(["--numstat", "-z"], repo_paths, base):
            stats.update(parse_numstat(output))
        return stats

    def diff(self, repo_paths: Sequence[str] | None = None, *, base: str | None = None) -> str:
        """Unified diff of tracked files."""
        if repo_paths is not None and not repo_paths:
            return ""
        return "".join(_decode(out) for out in self._diff_runs([], repo_paths, base))

    def untracked_stats(self, repo_path: str) -> LineStats:
        try:
            with open(self.file_path(repo_path), "rb") as handle:
                head = handle.read(_BINARY_SNIFF_BYTES)
                if is_binary(head):
                    return LineStats(None, None)
                lines = head.count(b"\n")
                last = head[-1:] if head else b""
                for block in iter(lambda: handle.read(1 << 20), b""):
                    lines += block.count(b"\n")
                    last = block[-1:]
        except OSError:
            return LineStats(0, 0)
        if last and last != b"\n":
            lines += 1
        return LineStats(lines, 0)

    def untracked_patch(self, repo_path: str) -> str:
        """A ``git diff``-style patch that adds the untracked file *repo_path*."""
        file = self.file_path(repo_path)
        try:
            info = file.stat()
            mode = "100755" if os.name != "nt" and info.st_mode & stat.S_IXUSR else "100644"
            header = f"diff --git a/{repo_path} b/{repo_path}\nnew file mode {mode}\n"
            if info.st_size > MAX_UNTRACKED_PATCH_BYTES:
                return header + f"(new file not included: {info.st_size} bytes)\n"
            data = file.read_bytes()
        except OSError:
            return ""
        if is_binary(data):
            return header + f"Binary files /dev/null and b/{repo_path} differ\n"
        if not data:
            return header
        text = data.decode("utf-8", "replace")
        lines = text.split("\n")
        missing_newline = lines[-1] != ""
        if not missing_newline:
            lines.pop()
        body = "".join(f"+{line}\n" for line in lines)
        if missing_newline:
            body += "\\ No newline at end of file\n"
        return f"{header}--- /dev/null\n+++ b/{repo_path}\n@@ -0,0 +1,{len(lines)} @@\n{body}"

    def commits_between(self, old: str | None, new: str | None, max_count: int = 10) -> list[str]:
        """One-line descriptions of commits reachable from *new* but not *old*."""
        if not new:
            return []
        args = ["log", "--no-color", "--format=%h %s", f"-n{max_count}"]
        rev = f"{old}..{new}" if old else new
        proc = self._run([*args, rev], check=False)
        if proc.returncode != 0 and old:
            proc = self._run([*args, "-n1", new], check=False)
        if proc.returncode != 0:
            return []
        return [line for line in _decode(proc.stdout).splitlines() if line.strip()]


def parse_status(output: bytes) -> list[StatusEntry]:
    """Parse ``git status --porcelain=v1 -z`` output."""
    entries: list[StatusEntry] = []
    records = output.split(b"\0")
    i = 0
    while i < len(records):
        record = records[i]
        i += 1
        if len(record) < 4:
            continue
        x, y = chr(record[0]), chr(record[1])
        path = _decode(record[3:])
        orig = None
        if x in "RC" or y in "RC":
            # In -z mode the original path follows as its own record.
            if i < len(records):
                orig = _decode(records[i])
                i += 1
        entries.append(StatusEntry(path, x, y, orig))
    return entries


def parse_name_status(output: bytes) -> dict[str, str]:
    """Parse ``git diff --name-status -z --no-renames`` output."""
    result: dict[str, str] = {}
    records = output.split(b"\0")
    for i in range(0, len(records) - 1, 2):
        letter, path = records[i], records[i + 1]
        if letter and path:
            result[_decode(path)] = chr(letter[0])
    return result


def parse_numstat(output: bytes) -> dict[str, LineStats]:
    """Parse ``git diff --numstat -z --no-renames`` output."""
    stats: dict[str, LineStats] = {}
    for record in output.split(b"\0"):
        if not record:
            continue
        parts = record.split(b"\t", 2)
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        if added == b"-" or deleted == b"-":
            stats[_decode(path)] = LineStats(None, None)
        else:
            try:
                stats[_decode(path)] = LineStats(int(added), int(deleted))
            except ValueError:
                continue
    return stats
