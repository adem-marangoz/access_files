"""Main application: loads the configuration, starts the file watchers,
analyzes changes with Git, runs builds and sends Telegram notifications.

Threads:
* watchdog observer -> :class:`Debouncer` (groups events per project)
* ``worker`` processes flushed batches, builds and AI-task completion
  one at a time, so a project is never analyzed concurrently
* ``telegram-commands`` long-polls Telegram for /commands
"""

from __future__ import annotations

import argparse
import logging
import queue
import signal
import socket
import sys
import threading
import time
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Callable, Sequence

from . import __version__, messages
from .build_monitor import BuildMonitor, BuildReport
from .commands import CommandHandler, CommandPoller
from .config import AgentConfig, ConfigError, ProjectConfig, load_config
from .diff import (
    ChangeSummary,
    diff_preview,
    make_file_documents,
    make_patch_document,
    slugify,
    summarize_git,
    summarize_plain,
)
from .git_monitor import GitError, GitRepo, git_version
from .logger import setup_logging
from .telegram import Document, TelegramClient, TelegramError, escape
from .watcher import ChangeBatch, ChangeKind, Debouncer, FileChange, WatchManager, build_ignore_patterns

log = logging.getLogger("tpms_agent")

_IDLE = object()


@dataclass
class TaskState:
    """An "AI task": a burst of activity that ends after ``task_idle_seconds`` of quiet."""

    started_wall: float
    last_wall: float
    last_activity: float  # agent clock
    start_head: str | None
    start_branch: str | None
    changes: ChangeBatch
    batches: int = 0

    def merge(self, batch: ChangeBatch) -> None:
        for change in batch.sorted_changes():
            if change.kind is ChangeKind.RENAMED and change.old_path:
                self.changes.add_move(change.old_path, change.path, is_dir=change.is_dir)
            else:
                self.changes.add(change.kind, change.path, is_dir=change.is_dir)
        self.batches += 1
        self.last_wall = max(self.last_wall, batch.last_wall)


@dataclass
class ProjectRuntime:
    config: ProjectConfig
    available: bool = False
    repo: GitRepo | None = None
    branch: str | None = None
    head: str | None = None
    task: TaskState | None = None
    last_activity: float | None = None  # agent clock

    @property
    def name(self) -> str:
        return self.config.name


def make_client(config: AgentConfig) -> TelegramClient:
    return TelegramClient(
        config.telegram.bot_token,
        config.telegram.chat_id or None,
        api_base_url=config.telegram.api_base_url,
        timeout=config.telegram.timeout_seconds,
    )


def _touched_paths(repo: GitRepo, changes: Sequence[FileChange]) -> tuple[list[str], list[str], dict[str, str]]:
    """Repository paths (files, directories, renames new->old) of watcher changes."""
    files: set[str] = set()
    dirs: set[str] = set()
    renames: dict[str, str] = {}
    for change in changes:
        target = dirs if change.is_dir else files
        path = repo.to_repo_path(change.path)
        target.add(path)
        if change.old_path:
            old = repo.to_repo_path(change.old_path)
            target.add(old)
            if not change.is_dir:
                renames[path] = old
    return sorted(files), sorted(dirs), renames


class Agent:
    def __init__(
        self,
        config: AgentConfig,
        telegram: TelegramClient | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.telegram = telegram or make_client(config)
        self.host = socket.gethostname()
        self.builds = BuildMonitor()
        self.runtimes: dict[str, ProjectRuntime] = {p.name: ProjectRuntime(p) for p in config.projects}
        self._clock = clock
        self.debouncer = Debouncer(
            config.watch.debounce_seconds, config.watch.max_batch_seconds, self._on_flush, clock=clock
        )
        self.watchers = WatchManager(self.debouncer)
        self._jobs: queue.Queue[object] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._poller: CommandPoller | None = None
        self._shutdown = threading.Event()
        self._paused = threading.Event()
        self._queued_builds: set[str] = set()
        self._queued_lock = threading.Lock()
        self._started = False
        self._stopping = False

    # -- lifecycle ---------------------------------------------------------

    def init_projects(self) -> None:
        """Check project directories and detect Git repositories."""
        for rt in self.runtimes.values():
            project = rt.config
            if not project.path.is_dir():
                log.error("Project %r: directory %s does not exist; it will not be monitored", rt.name, project.path)
                continue
            rt.available = True
            if project.git is False:
                log.info("Project %r: %s (plain folder)", rt.name, project.path)
                continue
            rt.repo = GitRepo.discover(project.path)
            if rt.repo is None:
                if project.git:
                    log.warning(
                        "Project %r: %s is not in a Git repository (or Git is not installed); "
                        "monitoring it as a plain folder",
                        rt.name,
                        project.path,
                    )
                continue
            try:
                rt.branch, rt.head = rt.repo.branch(), rt.repo.head()
            except GitError as exc:
                log.warning("Project %r: %s", rt.name, exc)
            log.info("Project %r: %s (Git, branch %s)", rt.name, project.path, rt.branch)

    def _ignored_paths(self) -> list[Path]:
        """The agent's own files, which must never trigger notifications."""
        log_file = self.config.logging.file
        if log_file is None:
            return []
        paths = [log_file]
        paths += [Path(f"{log_file}.{i}") for i in range(1, self.config.logging.backup_count + 1)]
        return paths

    def start(self) -> None:
        self.init_projects()
        available = [rt for rt in self.runtimes.values() if rt.available]
        if not available:
            raise RuntimeError("None of the configured project directories exist; nothing to monitor")

        self._worker = threading.Thread(target=self._work_loop, name="worker", daemon=True)
        self._worker.start()
        self.debouncer.start()
        ignored = self._ignored_paths()
        for rt in available:
            patterns = build_ignore_patterns(
                self.config.watch.use_default_ignores,
                self.config.watch.ignore,
                rt.config.ignore,
                git_project=rt.repo is not None,
            )
            self.watchers.add_project(
                rt.name,
                rt.config.path,
                patterns,
                track_git_meta=rt.repo is not None,
                ignored_paths=ignored,
            )
        self.watchers.start()

        if self.config.commands.enabled:
            handler = CommandHandler(self, self.config.authorized_chat_ids)
            self._poller = CommandPoller(
                self.telegram, handler, poll_timeout=self.config.commands.poll_timeout_seconds
            )
            self._poller.start()

        self._started = True
        log.info(
            "PC agent %s started: level %d, %d project(s), commands %s",
            __version__,
            self.config.notifications.level,
            len(available),
            "on" if self._poller else "off",
        )
        if self.config.notifications.notify_on_startup:
            text = messages.format_startup(
                self.project_statuses(with_git=False),
                host=self.host,
                level=self.config.notifications.level,
                commands=self._poller is not None,
            )
            self._jobs.put(partial(self.notify, text))

    def stop(self, *, notify: bool = True) -> None:
        if self._stopping:
            return
        self._stopping = True
        log.info("Stopping agent")
        if self._poller is not None:
            self._poller.stop()
        self.watchers.stop()
        self.debouncer.stop()
        for key, batch in self.debouncer.pop_all():  # don't lose the last changes
            if batch:
                self._jobs.put(partial(self._process_batch, key, batch))
        if notify and self._started and self.config.notifications.notify_on_startup:
            self._jobs.put(partial(self.notify, messages.format_shutdown(self.host)))
        self._jobs.put(None)
        if self._worker is not None:
            self._worker.join(timeout=120)
        log.info("Agent stopped")

    def request_shutdown(self, *_args: object) -> None:
        self._shutdown.set()

    def run_forever(self) -> None:
        self._install_signal_handlers()
        try:
            self.start()
            while not self._shutdown.wait(1.0):
                pass
        except KeyboardInterrupt:
            log.info("Interrupted")
        finally:
            self.stop()

    def _install_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for name in ("SIGTERM", "SIGBREAK", "SIGHUP"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, self.request_shutdown)
            except (OSError, ValueError):
                pass

    # -- pause / resume (/stop, /start) -----------------------------------

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def pause(self) -> None:
        self._paused.set()
        self._jobs.put(self._reset_tasks)
        log.info("Notifications paused")

    def resume(self) -> None:
        self._paused.clear()
        log.info("Notifications resumed")

    def _reset_tasks(self) -> None:
        for rt in self.runtimes.values():
            rt.task = None

    # -- sending -------------------------------------------------------------

    def _send(self, text: str, *, chat_id: str | None = None, silent: bool = False) -> bool:
        try:
            self.telegram.send_message(text, chat_id=chat_id, silent=silent)
            return True
        except TelegramError as exc:
            log.error("Could not send Telegram message: %s", exc)
            return False

    def _send_documents(self, documents: Sequence[Document], *, chat_id: str | None = None, silent: bool = True) -> None:
        if not documents:
            return
        try:
            self.telegram.send_documents(documents, chat_id=chat_id, silent=silent)
        except TelegramError as exc:
            log.error("Could not send attachment(s) to Telegram: %s", exc)

    def notify(self, text: str, *, silent: bool = False) -> None:
        """Automatic notification; suppressed while paused."""
        if self.paused:
            log.debug("Paused; not sending: %s", text[:80])
            return
        self._send(text, silent=silent)

    def notify_documents(self, documents: Sequence[Document]) -> None:
        if not self.paused:
            self._send_documents(documents)

    def reply(self, chat_id: str, text: str) -> None:
        """Answer to a command (always sent, even while paused)."""
        self._send(text, chat_id=chat_id)

    def attachments(self, summary: ChangeSummary) -> tuple[list[Document], list[str]]:
        cfg = self.config.notifications
        max_bytes = cfg.max_file_size_kb * 1024
        documents: list[Document] = []
        skipped: list[str] = []
        if cfg.send_diff:
            patch = make_patch_document(summary, max_bytes=max_bytes)
            if patch is not None:
                documents.append(patch)
        if cfg.send_files and cfg.max_files > 0:
            files, skipped = make_file_documents(summary, max_files=cfg.max_files, max_bytes=max_bytes)
            documents.extend(files)
        return documents, skipped

    def _send_attachments(self, summary: ChangeSummary, *, chat_id: str | None = None) -> None:
        documents, skipped = self.attachments(summary)
        if chat_id is None:
            self.notify_documents(documents)
        else:
            self._send_documents(documents, chat_id=chat_id)
        if skipped:
            log.info("Not attached: %s", "; ".join(skipped))
            note = "📎 Not attached:\n" + "\n".join(f"• {escape(s)}" for s in skipped[:10])
            if chat_id is None:
                self.notify(note, silent=True)
            else:
                self.reply(chat_id, note)

    def _build_log_document(self, report: BuildReport) -> Document | None:
        failed = report.failed
        if failed is None or not failed.output.strip():
            return None
        data = failed.output.encode("utf-8", "replace")
        max_bytes = self.config.notifications.max_file_size_kb * 1024
        if len(data) > max_bytes:
            data = data[-max_bytes:]
        name = f"{failed.name}-{slugify(report.project)}-{report.started:%Y%m%d-%H%M%S}.log"
        return Document(name, data, f"{escape(failed.name)} output ({escape(failed.status_text)})")

    # -- change processing (worker thread) ---------------------------------

    def _on_flush(self, key: str, batch: ChangeBatch) -> None:
        self._jobs.put(partial(self._process_batch, key, batch))

    def _work_loop(self) -> None:
        while True:
            try:
                job = self._jobs.get(timeout=1.0)
            except queue.Empty:
                job = _IDLE
            if job is None:
                return
            if job is not _IDLE:
                try:
                    job()  # type: ignore[operator]
                except Exception:
                    log.exception("Unexpected error in worker job")
            if not self._stopping:
                try:
                    self._check_idle_tasks()
                except Exception:
                    log.exception("Unexpected error while checking AI tasks")

    def _summarize_batch(self, rt: ProjectRuntime, batch: ChangeBatch, *, include_patch: bool) -> ChangeSummary | None:
        changes = batch.sorted_changes()
        if rt.repo is None:
            return summarize_plain(rt.name, rt.config.path, changes) if changes else None
        repo = rt.repo
        try:
            snapshot = repo.snapshot()
            files, dirs, renames = _touched_paths(repo, changes)
            summary = summarize_git(
                repo, rt.name, snapshot, files=files, dirs=dirs, renames=renames, include_patch=include_patch
            )
            if snapshot.head != rt.head:
                summary.new_commits = repo.commits_between(rt.head, snapshot.head)
        except GitError as exc:
            log.warning("Git analysis of %s failed: %s", rt.name, exc)
            return summarize_plain(rt.name, rt.config.path, changes) if changes else None
        if rt.branch is not None and snapshot.branch != rt.branch:
            summary.previous_branch = rt.branch
        rt.branch, rt.head = snapshot.branch, snapshot.head
        if not summary:
            # e.g. only ignored build output, or a file saved without changes
            log.debug("No reportable changes in %s (%d event(s))", rt.name, batch.event_count)
            return None
        return summary

    def _process_batch(self, name: str, batch: ChangeBatch) -> None:
        rt = self.runtimes[name]
        if self.paused:
            log.info("Paused: ignoring %d change(s) in %s", len(batch.changes), name)
            return
        cfg = self.config.notifications
        level = cfg.level
        head_before, branch_before = rt.head, rt.branch
        summary = self._summarize_batch(rt, batch, include_patch=level == 3 and cfg.send_diff)
        if summary is None:
            return
        log.info(
            "%s: %d file(s) changed%s",
            name,
            len(summary.files),
            f", {len(summary.new_commits)} new commit(s)" if summary.new_commits else "",
        )

        now = self._clock()
        rt.last_activity = now
        first_in_task = rt.task is None
        if rt.task is None:
            rt.task = TaskState(
                started_wall=batch.started_wall,
                last_wall=batch.last_wall,
                last_activity=now,
                start_head=head_before,
                start_branch=branch_before,
                changes=ChangeBatch(name, now),
            )
        rt.task.merge(batch)
        rt.task.last_activity = now

        report = None
        build = rt.config.build
        if build is not None and build.run_on == "change" and not self._stopping:
            report = self._run_build(rt)

        if level >= 4:
            title = messages.TITLE_TASK_STARTED if first_in_task else messages.TITLE_FILES_MODIFIED
            silent = not first_in_task and cfg.silent_intermediate
        else:
            title, silent = messages.TITLE_UPDATED, False
        preview = None
        if level == 3 and cfg.send_diff and summary.patch:
            preview = diff_preview(summary.patch, cfg.inline_diff_max_lines)
        text = messages.format_changes(
            summary,
            level=level,
            title=title,
            max_listed=cfg.max_listed_files,
            build=report if cfg.send_build_result else None,
            preview=preview,
        )
        self.notify(text, silent=silent)
        if level == 3:
            self._send_attachments(summary)
        if report is not None and cfg.send_build_result and not report.success:
            doc = self._build_log_document(report)
            if doc is not None:
                self.notify_documents([doc])

    def _run_build(self, rt: ProjectRuntime) -> BuildReport | None:
        # Build output must not be reported as changes (or re-trigger builds).
        self.debouncer.suppress(rt.name)
        try:
            return self.builds.run(rt.config)
        finally:
            self.debouncer.release(rt.name)

    def _check_idle_tasks(self) -> None:
        now = self._clock()
        idle = self.config.watch.task_idle_seconds
        for rt in self.runtimes.values():
            task = rt.task
            if task is None or now - task.last_activity < idle:
                continue
            if self.debouncer.pending(rt.name):
                continue  # more changes are about to arrive
            rt.task = None
            self._complete_task(rt, task)

    def _summarize_task(self, rt: ProjectRuntime, task: TaskState, *, include_patch: bool) -> ChangeSummary:
        changes = task.changes.sorted_changes()
        if rt.repo is None:
            return summarize_plain(rt.name, rt.config.path, changes)
        repo = rt.repo
        try:
            snapshot = repo.snapshot()
            files, dirs, renames = _touched_paths(repo, changes)
            # Compare with the commit at task start, so work the AI agent
            # committed during the task is included.
            base = task.start_head or repo.empty_tree()
            try:
                summary = summarize_git(
                    repo, rt.name, snapshot, files=files, dirs=dirs, renames=renames,
                    base=base, include_patch=include_patch,
                )
            except GitError:
                summary = summarize_git(
                    repo, rt.name, snapshot, files=files, dirs=dirs, renames=renames, include_patch=include_patch
                )
            if snapshot.head != task.start_head:
                summary.new_commits = repo.commits_between(task.start_head, snapshot.head)
            if task.start_branch and snapshot.branch != task.start_branch:
                summary.previous_branch = task.start_branch
            return summary
        except GitError as exc:
            log.warning("Git analysis of %s failed: %s", rt.name, exc)
            return summarize_plain(rt.name, rt.config.path, changes)

    def _complete_task(self, rt: ProjectRuntime, task: TaskState) -> None:
        if self.paused:
            return
        cfg = self.config.notifications
        build = rt.config.build
        run_build = build is not None and build.run_on == "task_complete"
        log.info("%s: AI task finished after %d update(s)", rt.name, task.batches)

        if cfg.level < 4:
            if run_build:
                report = self._run_build(rt)
                if report is not None and cfg.send_build_result:
                    self.notify(messages.format_build_finished(report))
                    doc = self._build_log_document(report)
                    if doc is not None and not report.success:
                        self.notify_documents([doc])
            return

        report = None
        if run_build:
            self.notify(messages.format_build_started(rt.name, self.build_commands(rt.config)), silent=cfg.silent_intermediate)
            report = self._run_build(rt)
        summary = self._summarize_task(rt, task, include_patch=cfg.send_diff)
        preview = None
        if cfg.send_diff and summary.patch:
            preview = diff_preview(summary.patch, cfg.inline_diff_max_lines)
        text = messages.format_task_completed(
            summary,
            duration=task.last_wall - task.started_wall,
            batches=task.batches,
            max_listed=cfg.max_listed_files,
            build=report if cfg.send_build_result else None,
            build_configured=build is not None and cfg.send_build_result,
            preview=preview,
        )
        self.notify(text)
        self._send_attachments(summary)
        if report is not None and cfg.send_build_result and not report.success:
            doc = self._build_log_document(report)
            if doc is not None:
                self.notify_documents([doc])

    # -- queries used by commands and the CLI -----------------------------

    @staticmethod
    def build_commands(project: ProjectConfig) -> list[str]:
        build = project.build
        if build is None:
            return []
        commands = [build.command, build.test_command]
        return [c if isinstance(c, str) else " ".join(c) for c in commands if c]

    def project_summary(self, rt: ProjectRuntime, *, include_patch: bool) -> ChangeSummary | None:
        """All uncommitted changes of a Git project (None for plain folders)."""
        if rt.repo is None:
            return None
        snapshot = rt.repo.snapshot()
        return summarize_git(rt.repo, rt.name, snapshot, include_patch=include_patch)

    def project_statuses(
        self, names: Sequence[str] | None = None, *, with_git: bool = True
    ) -> list[messages.ProjectStatus]:
        now = self._clock()
        statuses = []
        for rt in self.runtimes.values():
            if names is not None and rt.name not in names:
                continue
            status = messages.ProjectStatus(
                name=rt.name,
                path=str(rt.config.path),
                available=rt.available,
                is_git=rt.repo is not None,
                branch=rt.branch,
                build=self.builds.last_reports.get(rt.name),
                build_running=self.builds.is_running(rt.name),
                build_configured=rt.config.build is not None,
            )
            if rt.last_activity is not None:
                status.last_activity_ago = now - rt.last_activity
            task = rt.task  # read once: the worker thread may clear it
            if task is not None:
                status.task_active_for = time.time() - task.started_wall
            if with_git and rt.repo is not None and status.available:
                try:
                    summary = self.project_summary(rt, include_patch=False)
                    if summary is not None:
                        status.branch = summary.branch
                        status.changed_files = len(summary.files)
                        status.insertions = summary.insertions
                        status.deletions = summary.deletions
                except GitError as exc:
                    status.error = str(exc)
            statuses.append(status)
        return statuses

    def send_project_diff(self, name: str, chat_id: str) -> None:
        """Answer /diff: summary plus the full diff as a .patch file."""
        rt = self.runtimes[name]
        cfg = self.config.notifications
        if not cfg.send_diff:
            self.reply(chat_id, "Sending diffs is disabled (<code>notifications.send_diff: false</code>).")
            return
        if rt.repo is None:
            self.reply(chat_id, f"{escape(name)} is not a Git repository; there is no diff to show.")
            return
        summary = self.project_summary(rt, include_patch=True)
        if summary is None or not summary.files:
            branch = escape(summary.branch) if summary and summary.branch else "?"
            self.reply(chat_id, f"✅ {escape(name)}: no uncommitted changes on <code>{branch}</code>.")
            return
        text = messages.format_changes(
            summary, level=max(cfg.level, 2), title="📋 Current Diff", max_listed=cfg.max_listed_files
        )
        self.reply(chat_id, text)
        patch = make_patch_document(summary, max_bytes=cfg.max_file_size_kb * 1024)
        if patch is not None:
            self._send_documents([patch], chat_id=chat_id)

    def request_build(self, name: str, chat_id: str) -> None:
        """Answer /build: queue a build of *name* and report back to *chat_id*."""
        rt = self.runtimes[name]
        if rt.config.build is None:
            self.reply(chat_id, f"No build is configured for {escape(name)} (add a <code>build</code> section in config.yaml).")
            return
        if not rt.available:
            self.reply(chat_id, f"The directory of {escape(name)} does not exist.")
            return
        with self._queued_lock:
            busy = name in self._queued_builds or self.builds.is_running(name)
            if not busy:
                self._queued_builds.add(name)
        if busy:
            self.reply(chat_id, f"🔨 A build of {escape(name)} is already queued or running.")
            return
        self.reply(chat_id, messages.format_build_started(name, self.build_commands(rt.config)))
        self._jobs.put(partial(self._manual_build, rt, chat_id))

    def _manual_build(self, rt: ProjectRuntime, chat_id: str) -> None:
        try:
            report = self._run_build(rt)
        finally:
            with self._queued_lock:
                self._queued_builds.discard(rt.name)
        if report is None:
            return
        self.reply(chat_id, messages.format_build_finished(report))
        doc = self._build_log_document(report)
        if doc is not None and not report.success:
            self._send_documents([doc], chat_id=chat_id)


# -- command line ----------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    # -c/-v are accepted before and after the sub-command ("agent.py run -c x.yaml").
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-c", "--config", default=argparse.SUPPRESS, help="path to config.yaml (default: ./config.yaml)"
    )
    common.add_argument(
        "-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help="enable debug logging"
    )
    parser = argparse.ArgumentParser(
        prog="tpms-agent",
        description="Monitor project folders and send change notifications to Telegram.",
        parents=[common],
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.add_parser("run", parents=[common], help="start monitoring (default)")
    sub.add_parser("check", parents=[common], help="validate the configuration and test the Telegram connection")
    send = sub.add_parser("send", parents=[common], help="send a test message")
    send.add_argument("text", nargs="*", help="message text")
    report = sub.add_parser(
        "report",
        parents=[common],
        help="send a one-off report of uncommitted changes (e.g. from an AI agent hook)",
    )
    report.add_argument("-p", "--project", help="project name (default: every Git project with changes)")
    report.add_argument("--title", help="message title (default: 📋 Project Report)")
    report.add_argument("--build", action="store_true", help="run the configured build/tests first")
    sub.add_parser(
        "chat-id", parents=[common], help="list the chat IDs that recently messaged the bot (setup helper)"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = args.command or "run"
    try:
        config = load_config(getattr(args, "config", None), require_chat_id=command != "chat-id")
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    level = "DEBUG" if getattr(args, "verbose", False) else config.logging.level
    # Only the long-running agent writes the log file (one writer per file).
    setup_logging(
        level,
        config.logging.file if command == "run" else None,
        secrets=[config.telegram.bot_token],
        max_bytes=config.logging.max_bytes,
        backup_count=config.logging.backup_count,
    )
    for warning in config.warnings:
        log.warning(warning)

    if command == "run":
        return _cmd_run(config)
    if command == "check":
        return _cmd_check(config)
    if command == "send":
        return _cmd_send(config, " ".join(args.text))
    if command == "report":
        return _cmd_report(config, args.project, args.title, args.build)
    if command == "chat-id":
        return _cmd_chat_id(config)
    return 2


def _cmd_run(config: AgentConfig) -> int:
    agent = Agent(config)
    try:
        agent.run_forever()
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1
    return 0


def _cmd_check(config: AgentConfig) -> int:
    ok = True
    print(f"Config file: {config.source}")
    for warning in config.warnings:
        print(f"  [WARN] {warning}")
    version = git_version()
    print(f"Git: {version or 'not found (Git features disabled)'}")
    level = config.notifications
    print(
        f"Notifications: level {level.level}, send_diff={level.send_diff}, "
        f"send_files={level.send_files}, send_build_result={level.send_build_result}"
    )
    print("Projects:")
    for project in config.projects:
        if not project.path.is_dir():
            print(f"  [FAIL] {project.name}: directory not found: {project.path}")
            ok = False
            continue
        repo = GitRepo.discover(project.path) if project.git is not False and version else None
        if repo is not None:
            try:
                kind = f"Git, branch {repo.branch()}"
            except GitError as exc:
                kind = f"Git ({exc})"
        else:
            kind = "plain folder"
            if project.git:
                kind += " (git: true, but no repository found)"
        print(f"  [ OK ] {project.name}: {project.path} ({kind})")
        if project.build is not None:
            commands = ", ".join(Agent.build_commands(project))
            print(f"         build on {project.build.run_on}: {commands}")
    client = make_client(config)
    try:
        me = client.get_me()
        print(f"Telegram bot: @{me.get('username')} [ OK ]")
    except TelegramError as exc:
        print(f"Telegram bot: [FAIL] {exc}")
        return 1
    try:
        client.send_chat_action("typing")
        print(f"Telegram chat {config.telegram.chat_id}: reachable [ OK ]")
    except TelegramError as exc:
        print(
            f"Telegram chat {config.telegram.chat_id}: [FAIL] {exc}\n"
            "  Open your bot in Telegram and press Start, and check telegram.chat_id "
            "(run 'chat-id' to find it)."
        )
        ok = False
    print("All checks passed." if ok else "Some checks failed.")
    return 0 if ok else 1


def _cmd_send(config: AgentConfig, text: str) -> int:
    client = make_client(config)
    message = escape(text) if text else f"✅ Test message from PC Agent on {escape(socket.gethostname())}"
    try:
        client.send_message(message)
    except TelegramError as exc:
        print(f"Sending failed: {exc}", file=sys.stderr)
        return 1
    print("Message sent.")
    return 0


def _cmd_report(config: AgentConfig, project: str | None, title: str | None, build: bool) -> int:
    agent = Agent(config)
    agent.init_projects()
    runtimes = list(agent.runtimes.values())
    if project:
        runtimes = [rt for rt in runtimes if rt.name.casefold() == project.casefold()]
        if not runtimes:
            names = ", ".join(p.name for p in config.projects)
            print(f"Unknown project {project!r}. Configured: {names}", file=sys.stderr)
            return 2
    cfg = config.notifications
    sent = 0
    for rt in runtimes:
        if not rt.available:
            print(f"{rt.name}: directory not found, skipped")
            continue
        report = agent.builds.run(rt.config) if build and rt.config.build else None
        try:
            summary = agent.project_summary(rt, include_patch=cfg.send_diff)
        except GitError as exc:
            print(f"{rt.name}: Git error: {exc}", file=sys.stderr)
            continue
        if summary is None:
            if report is None:
                print(f"{rt.name}: not a Git repository, skipped")
                continue
            summary = ChangeSummary(project=rt.name)
        if not summary.files and report is None and not project:
            print(f"{rt.name}: no uncommitted changes, skipped")
            continue
        preview = None
        if cfg.level >= 3 and cfg.send_diff and summary.patch:
            preview = diff_preview(summary.patch, cfg.inline_diff_max_lines)
        text = messages.format_changes(
            summary,
            level=max(cfg.level, 2),
            title=title or messages.TITLE_REPORT,
            max_listed=cfg.max_listed_files,
            build=report if cfg.send_build_result else None,
            preview=preview,
        )
        if not agent._send(text):
            return 1
        if cfg.level >= 3:
            agent._send_attachments(summary)
        if report is not None and cfg.send_build_result and not report.success:
            doc = agent._build_log_document(report)
            if doc is not None:
                agent._send_documents([doc])
        sent += 1
        print(f"{rt.name}: report sent ({len(summary.files)} file(s))")
    if sent == 0:
        print("Nothing to report.")
    return 0


def _cmd_chat_id(config: AgentConfig) -> int:
    client = make_client(config)
    try:
        me = client.get_me()
        updates = client.get_updates(None, timeout=0)
    except TelegramError as exc:
        print(f"Telegram error: {exc}", file=sys.stderr)
        return 1
    username = me.get("username", "?")
    chats: dict[str, str] = {}
    for update in updates:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        if "id" in chat:
            name = chat.get("title") or " ".join(
                x for x in (chat.get("first_name"), chat.get("last_name")) if x
            ) or chat.get("username") or "?"
            chats[str(chat["id"])] = f"{chat.get('type', '?')}: {name}"
    print(f"Bot: @{username}")
    if not chats:
        print(
            f"No recent messages. Open https://t.me/{username} in Telegram, press Start "
            "(or send any message), then run this command again."
        )
        return 1
    print("Chats that recently messaged the bot:")
    for chat_id, description in chats.items():
        print(f"  chat_id: {chat_id}    ({description})")
    print("Put your chat_id into TELEGRAM_CHAT_ID (.env) or telegram.chat_id (config.yaml).")
    return 0
