"""Telegram message templates (HTML parse mode)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Sequence

from .build_monitor import BuildReport
from .diff import ChangeSummary, FileSummary, format_line_stats
from .telegram import escape as h

TITLE_UPDATED = "🤖 Project Updated"
TITLE_TASK_STARTED = "🤖 AI Task Started"
TITLE_FILES_MODIFIED = "📝 Files Modified"
TITLE_TASK_COMPLETED = "🤖 AI TASK COMPLETED"
TITLE_REPORT = "📋 Project Report"

COMMAND_HELP: Sequence[tuple[str, str, str]] = (
    ("status", "[project]", "branch, changes, build, last activity"),
    ("projects", "", "list monitored projects"),
    ("diff", "[project]", "current Git diff as a .patch file"),
    ("build", "[project]", "run the configured build/tests"),
    ("log", "[lines]", "last lines of the agent log"),
    ("stop", "", "pause notifications"),
    ("start", "", "resume notifications"),
    ("help", "", "show this help"),
)


def format_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def format_ago(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    seconds = max(0, int(seconds))
    if seconds < 10:
        return "just now"
    if seconds < 60:
        return f"{seconds} seconds ago"
    minutes = seconds // 60
    if minutes < 60:
        return "1 minute ago" if minutes == 1 else f"{minutes} minutes ago"
    hours = minutes // 60
    if hours < 48:
        return "1 hour ago" if hours == 1 else f"{hours} hours ago"
    return f"{hours // 24} days ago"


def _file_line(item: FileSummary, with_stats: bool) -> str:
    if item.old_path:
        name = f"{h(item.old_path)} → {h(item.display_path)}"
    else:
        name = h(item.display_path)
    line = f"• <code>{h(item.letter)}</code> {name}"
    if with_stats and (item.added is not None or item.binary):
        line += f"  <i>{format_line_stats(item.added, item.deleted)}</i>"
    return line


def file_lines(files: Sequence[FileSummary], max_listed: int, *, with_stats: bool) -> list[str]:
    lines = [_file_line(f, with_stats) for f in files[:max_listed]]
    if len(files) > max_listed:
        lines.append(f"<i>… and {len(files) - max_listed} more</i>")
    return lines


def build_lines(report: BuildReport) -> list[str]:
    lines = []
    for result in report.results:
        icon = "✅" if result.success else "❌"
        label = "Build" if result.name == "build" else "Tests"
        lines.append(f"<b>{label}:</b> {icon} {h(result.status_text)} ({result.duration:.1f}s)")
    return lines


def _header(summary: ChangeSummary, title: str) -> list[str]:
    lines = [f"<b>{h(title)}</b>", "", f"<b>Project:</b> {h(summary.project)}"]
    if summary.branch:
        branch = f"<b>Branch:</b> <code>{h(summary.branch)}</code>"
        if summary.previous_branch:
            branch += f" (switched from <code>{h(summary.previous_branch)}</code>)"
        lines.append(branch)
    return lines


def _commit_lines(commits: Iterable[str]) -> list[str]:
    lines = []
    for commit in commits:
        sha, _, subject = commit.partition(" ")
        lines.append(f"• <code>{h(sha)}</code> {h(subject)}")
    return lines


def _preview_block(preview: str) -> str:
    return f'<pre><code class="language-diff">{h(preview)}</code></pre>'


def format_changes(
    summary: ChangeSummary,
    *,
    level: int,
    title: str = TITLE_UPDATED,
    max_listed: int = 25,
    build: BuildReport | None = None,
    preview: str | None = None,
) -> str:
    """Notification for one group of changes."""
    if level <= 1:
        count = len(summary.files)
        heading = {0: "Repository updated", 1: "File changed"}.get(count, f"{count} files changed")
        lines = [f"📝 <b>{heading}</b> · {h(summary.project)}"]
        lines += [f"• {h(f.display_path)}" for f in summary.files[:max_listed]]
        if count > max_listed:
            lines.append(f"<i>… and {count - max_listed} more</i>")
        if summary.previous_branch and summary.branch:
            lines.append(f"Branch switched to <code>{h(summary.branch)}</code>")
        if summary.new_commits:
            lines.append(f"{len(summary.new_commits)} new commit(s)")
        if build is not None:
            lines += build_lines(build)
        return "\n".join(lines)

    lines = _header(summary, title)
    if summary.files:
        lines += ["", "<b>Modified files:</b>"]
        lines += file_lines(summary.files, max_listed, with_stats=summary.is_git)
        if summary.has_line_stats:
            lines += ["", f"<b>Changes:</b> +{summary.insertions} / -{summary.deletions} lines"]
    if summary.new_commits:
        lines += ["", "<b>New commits:</b>", *_commit_lines(summary.new_commits)]
    if build is not None:
        lines += ["", *build_lines(build)]
    if preview:
        lines += ["", _preview_block(preview)]
    lines += ["", f"<b>Time:</b> {summary.timestamp:%H:%M:%S}"]
    return "\n".join(lines)


def format_task_completed(
    summary: ChangeSummary,
    *,
    duration: float,
    batches: int,
    max_listed: int = 25,
    build: BuildReport | None = None,
    build_configured: bool = False,
    preview: str | None = None,
) -> str:
    """Level 4 summary sent once an AI task has gone idle."""
    lines = _header(summary, TITLE_TASK_COMPLETED)
    lines += ["", "<b>Modified:</b>"]
    if summary.files:
        lines += file_lines(summary.files, max_listed, with_stats=summary.is_git)
    else:
        lines.append("<i>no net changes</i>")
    if build is not None:
        lines += ["", *build_lines(build)]
    elif build_configured:
        lines += ["", "<b>Build:</b> not executed"]
    if summary.is_git:
        lines += ["", "<b>Git:</b>"]
        count = len(summary.files)
        lines.append(f"  {count} file{'s' if count != 1 else ''} changed")
        lines.append(f"  +{summary.insertions} / -{summary.deletions} lines")
        if summary.worktree_files is not None:
            lines.append(f"  {summary.worktree_files} uncommitted file(s) in total")
        if summary.new_commits:
            lines += ["", "<b>Commits:</b>", *_commit_lines(summary.new_commits)]
    if preview:
        lines += ["", _preview_block(preview)]
    lines += [
        "",
        f"<b>Duration:</b> {format_duration(duration)} ({batches} update{'s' if batches != 1 else ''})",
        f"<b>Finished:</b> {summary.timestamp:%H:%M:%S}",
    ]
    return "\n".join(lines)


def format_build_started(project: str, commands: Sequence[str]) -> str:
    lines = [f"🔨 <b>Build started</b> · {h(project)}"]
    lines += [f"<code>{h(c)}</code>" for c in commands]
    return "\n".join(lines)


def format_build_finished(report: BuildReport, *, include_output: bool = True) -> str:
    icon = "✅" if report.success else "❌"
    lines = [f"{icon} <b>Build finished</b> · {h(report.project)}", "", *build_lines(report)]
    failed = report.failed
    if include_output and failed is not None and failed.output.strip():
        lines += ["", f"<b>Last lines of {h(failed.name)} output:</b>", f"<pre>{h(failed.output_tail(15, 2500))}</pre>"]
    return "\n".join(lines)


@dataclass
class ProjectStatus:
    name: str
    path: str
    available: bool = True
    is_git: bool = False
    branch: str | None = None
    changed_files: int | None = None
    insertions: int | None = None
    deletions: int | None = None
    last_activity_ago: float | None = None
    task_active_for: float | None = None
    build: BuildReport | None = None
    build_running: bool = False
    build_configured: bool = False
    error: str | None = None


def format_status(statuses: Sequence[ProjectStatus], *, host: str, paused: bool) -> str:
    lines = [f"🤖 <b>PC Agent</b> · {h(host)}"]
    if paused:
        lines.append("⏸ <i>Notifications are paused (/start to resume)</i>")
    for status in statuses:
        lines += ["", f"<b>Project:</b> {h(status.name)}"]
        if not status.available:
            lines.append("⚠️ directory not found")
            continue
        if status.error:
            lines.append(f"⚠️ {h(status.error)}")
        if status.branch:
            lines.append(f"<b>Branch:</b> <code>{h(status.branch)}</code>")
        if status.changed_files is not None:
            changes = f"<b>Modified files:</b> {status.changed_files}"
            if status.changed_files and status.insertions is not None:
                changes += f" (+{status.insertions} / -{status.deletions})"
            lines.append(changes)
        if status.build_running:
            lines.append("<b>Build:</b> 🔨 running")
        elif status.build is not None:
            word = "SUCCESS" if status.build.success else "FAILED"
            ago = format_ago((datetime.now() - status.build.started).total_seconds())
            lines.append(f"<b>Build:</b> {word} ({ago})")
        elif status.build_configured:
            lines.append("<b>Build:</b> not executed yet")
        if status.task_active_for is not None:
            lines.append(f"<b>AI task:</b> active for {format_duration(status.task_active_for)}")
        lines.append(f"<b>Last activity:</b> {format_ago(status.last_activity_ago)}")
    return "\n".join(lines)


def format_projects(statuses: Sequence[ProjectStatus]) -> str:
    lines = ["📁 <b>Monitored projects</b>", ""]
    for index, status in enumerate(statuses, start=1):
        kind = "Git" if status.is_git else "folder"
        if not status.available:
            kind = "⚠️ not found"
        extras = []
        if status.build_configured:
            extras.append("build")
        extra = f", {', '.join(extras)}" if extras else ""
        lines.append(f"{index}. <b>{h(status.name)}</b> ({kind}{extra})")
        lines.append(f"    <code>{h(status.path)}</code>")
    return "\n".join(lines)


def format_startup(statuses: Sequence[ProjectStatus], *, host: str, level: int, commands: bool) -> str:
    watched = [s for s in statuses if s.available]
    lines = [
        f"🟢 <b>PC Agent started</b> · {h(host)}",
        f"Notification level {level}, monitoring {len(watched)} project{'s' if len(watched) != 1 else ''}:",
    ]
    for status in statuses:
        if not status.available:
            lines.append(f"⚠️ {h(status.name)}: directory not found")
        elif status.branch:
            lines.append(f"• <b>{h(status.name)}</b> (<code>{h(status.branch)}</code>)")
        else:
            lines.append(f"• <b>{h(status.name)}</b>")
    if commands:
        lines += ["", "Send /help for commands."]
    return "\n".join(lines)


def format_shutdown(host: str) -> str:
    return f"🔴 <b>PC Agent stopped</b> · {h(host)}"


def format_help() -> str:
    lines = ["🤖 <b>PC Agent commands</b>", ""]
    for name, args, description in COMMAND_HELP:
        usage = f"/{name} {args}".rstrip()
        lines.append(f"{h(usage)} — {h(description)}")
    lines += ["", "Projects can be given by name or by number (see /projects)."]
    return "\n".join(lines)
