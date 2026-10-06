from __future__ import annotations

import sys
import time
from pathlib import Path

from tpms_agent.build_monitor import BuildMonitor, run_command
from tpms_agent.config import BuildConfig, ProjectConfig

PY = [sys.executable, "-c"]


def test_successful_command_captures_output(tmp_path: Path):
    result = run_command("build", PY + ["print('compiled ok')"], tmp_path, timeout=30)
    assert result.success and result.status_text == "SUCCESS"
    assert "compiled ok" in result.output


def test_failing_command(tmp_path: Path):
    result = run_command("build", PY + ["import sys; print('error: x'); sys.exit(3)"], tmp_path, timeout=30)
    assert not result.success
    assert result.returncode == 3
    assert result.status_text == "FAILED (exit code 3)"
    assert result.output_tail(1) == "error: x"


def test_shell_string_command(tmp_path: Path):
    result = run_command("test", "echo hello && echo world", tmp_path, timeout=30)
    assert result.success and "world" in result.output


def test_timeout_kills_the_process(tmp_path: Path):
    start = time.monotonic()
    result = run_command("build", PY + ["import time; time.sleep(30)"], tmp_path, timeout=0.5)
    assert result.timed_out and not result.success
    assert time.monotonic() - start < 15
    assert result.status_text.startswith("TIMED OUT")


def test_missing_executable(tmp_path: Path):
    result = run_command("build", ["definitely-not-a-real-program-xyz"], tmp_path, timeout=5)
    assert result.error and not result.success


def project(tmp_path: Path, command, test_command) -> ProjectConfig:
    return ProjectConfig("P", tmp_path, build=BuildConfig(command=command, test_command=test_command))


def test_tests_run_after_successful_build(tmp_path: Path):
    monitor = BuildMonitor()
    report = monitor.run(project(tmp_path, PY + ["pass"], PY + ["print('3 passed')"]))
    assert report.success and report.build.success and "3 passed" in report.test.output
    assert monitor.last_reports["P"] is report


def test_tests_skipped_when_build_fails(tmp_path: Path):
    report = BuildMonitor().run(project(tmp_path, PY + ["raise SystemExit(1)"], PY + ["pass"]))
    assert not report.success and report.test is None
    assert report.failed is report.build


def test_tests_only(tmp_path: Path):
    report = BuildMonitor().run(project(tmp_path, None, PY + ["raise SystemExit(2)"]))
    assert report.build is None and report.failed is report.test


def test_no_build_configured(tmp_path: Path):
    assert BuildMonitor().run(ProjectConfig("P", tmp_path)) is None
