"""Optional build and test execution for monitored projects."""

from __future__ import annotations

import locale
import logging
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import ProjectConfig
from .git_monitor import NO_WINDOW

log = logging.getLogger(__name__)

MAX_OUTPUT_BYTES = 2_000_000


@dataclass
class CommandResult:
    name: str  # "build" or "test"
    command: str
    returncode: int | None
    duration: float
    output: str
    timed_out: bool = False
    error: str | None = None

    @property
    def success(self) -> bool:
        return self.returncode == 0 and not self.timed_out and self.error is None

    @property
    def status_text(self) -> str:
        if self.error:
            return f"ERROR ({self.error})"
        if self.timed_out:
            return f"TIMED OUT after {self.duration:.0f}s"
        if self.returncode == 0:
            return "SUCCESS"
        return f"FAILED (exit code {self.returncode})"

    def output_tail(self, lines: int = 20, max_chars: int = 3000) -> str:
        tail = "\n".join(self.output.rstrip().splitlines()[-lines:])
        return tail[-max_chars:]


@dataclass
class BuildReport:
    project: str
    build: CommandResult | None = None
    test: CommandResult | None = None
    started: datetime = field(default_factory=datetime.now)

    @property
    def results(self) -> list[CommandResult]:
        return [r for r in (self.build, self.test) if r is not None]

    @property
    def success(self) -> bool:
        return all(r.success for r in self.results)

    @property
    def duration(self) -> float:
        return sum(r.duration for r in self.results)

    @property
    def failed(self) -> CommandResult | None:
        return next((r for r in self.results if not r.success), None)


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode(locale.getpreferredencoding(False) or "latin-1", "replace")


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=NO_WINDOW,
                timeout=30,
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def run_command(name: str, command: str | list[str], cwd: Path, timeout: float) -> CommandResult:
    """Run *command* (a shell string or an argv list) and capture its output."""
    display = command if isinstance(command, str) else subprocess.list2cmdline(command)
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = NO_WINDOW
    else:
        kwargs["start_new_session"] = True  # own process group, so we can kill the tree
    log.info("Running %s in %s: %s", name, cwd, display)
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            command,
            shell=isinstance(command, str),
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            **kwargs,
        )
    except OSError as exc:
        return CommandResult(name, display, None, 0.0, "", error=str(exc))

    timed_out = False
    try:
        output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        try:
            output, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            output = b""
    duration = time.monotonic() - start
    if len(output) > MAX_OUTPUT_BYTES:
        output = output[-MAX_OUTPUT_BYTES:]
    result = CommandResult(name, display, proc.returncode, duration, _decode(output), timed_out=timed_out)
    log.info("%s finished in %.1fs: %s", name.capitalize(), duration, result.status_text)
    return result


class BuildMonitor:
    """Runs a project's build and test commands, one run per project at a time."""

    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self.last_reports: dict[str, BuildReport] = {}

    def _lock(self, project: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(project, threading.Lock())

    def is_running(self, project: str) -> bool:
        return self._lock(project).locked()

    def run(self, project: ProjectConfig) -> BuildReport | None:
        config = project.build
        if config is None:
            return None
        cwd = config.cwd or project.path
        with self._lock(project.name):
            report = BuildReport(project.name)
            if config.command:
                report.build = run_command("build", config.command, cwd, config.timeout_seconds)
            if config.test_command and (report.build is None or report.build.success):
                report.test = run_command("test", config.test_command, cwd, config.timeout_seconds)
            self.last_reports[project.name] = report
            return report
