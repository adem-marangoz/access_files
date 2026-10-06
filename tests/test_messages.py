from __future__ import annotations

from datetime import datetime

from tpms_agent import messages
from tpms_agent.build_monitor import BuildReport, CommandResult
from tpms_agent.diff import ChangeSummary, FileSummary


def summary(**kwargs) -> ChangeSummary:
    defaults = dict(
        project="TPMS ECU",
        is_git=True,
        branch="ecu_securt_comm",
        timestamp=datetime(2026, 1, 1, 9, 15, 23),
        files=[
            FileSummary("Sources/CAN_ResponseTask.c", "M", added=50, deleted=30),
            FileSummary("Sources/CAN_ResponseTask.h", "M", added=4, deleted=1),
            FileSummary("Sources/FirmwareUpdate.c", "A", added=30, deleted=0),
        ],
    )
    defaults.update(kwargs)
    return ChangeSummary(**defaults)


def test_level_1_lists_file_names_only():
    text = messages.format_changes(summary(), level=1)
    assert text.splitlines()[0] == "📝 <b>3 files changed</b> · TPMS ECU"
    assert "• Sources/FirmwareUpdate.c" in text
    assert "+84" not in text and "Branch" not in text


def test_level_1_commit_without_file_changes():
    text = messages.format_changes(summary(files=[], new_commits=["abc1234 Fix"]), level=1)
    assert text.splitlines() == ["📝 <b>Repository updated</b> · TPMS ECU", "1 new commit(s)"]


def test_level_2_git_summary():
    text = messages.format_changes(summary(), level=2)
    assert "<b>🤖 Project Updated</b>" in text
    assert "<b>Branch:</b> <code>ecu_securt_comm</code>" in text
    assert "• <code>A</code> Sources/FirmwareUpdate.c  <i>+30 -0</i>" in text
    assert "<b>Changes:</b> +84 / -31 lines" in text
    assert "<b>Time:</b> 09:15:23" in text


def test_html_is_escaped():
    s = summary(project="A<B>", files=[FileSummary("x&y<z>.c", "M", added=1, deleted=0)])
    text = messages.format_changes(s, level=2)
    assert "A&lt;B&gt;" in text and "x&amp;y&lt;z&gt;.c" in text


def test_preview_commits_and_branch_switch():
    s = summary(previous_branch="main", new_commits=["abc1234 Fix CAN <timing>"])
    text = messages.format_changes(s, level=3, preview="+vTaskDelay(10);")
    assert "(switched from <code>main</code>)" in text
    assert "• <code>abc1234</code> Fix CAN &lt;timing&gt;" in text
    assert '<pre><code class="language-diff">+vTaskDelay(10);</code></pre>' in text


def test_file_list_is_truncated():
    files = [FileSummary(f"f{i}.c", "M", added=1, deleted=0) for i in range(30)]
    text = messages.format_changes(summary(files=files), level=2, max_listed=5)
    assert "f4.c" in text and "f5.c" not in text and "… and 25 more" in text


def build_report(ok: bool) -> BuildReport:
    build = CommandResult("build", "make", 0, 12.34, "ok")
    test = CommandResult("test", "ctest", 0 if ok else 1, 2.0, "1 test failed\nFAIL: crc")
    return BuildReport("TPMS ECU", build, test)


def test_task_completed_message():
    text = messages.format_task_completed(summary(), duration=277, batches=4, build=build_report(True))
    assert "<b>🤖 AI TASK COMPLETED</b>" in text
    assert "<b>Build:</b> ✅ SUCCESS (12.3s)" in text
    assert "3 files changed" in text and "+84 / -31 lines" in text
    assert "<b>Duration:</b> 04:37 (4 updates)" in text


def test_task_completed_build_not_executed():
    text = messages.format_task_completed(summary(), duration=5, batches=1, build_configured=True)
    assert "<b>Build:</b> not executed" in text


def test_build_finished_shows_failure_output():
    text = messages.format_build_finished(build_report(False))
    assert text.startswith("❌ <b>Build finished</b>")
    assert "<b>Tests:</b> ❌ FAILED (exit code 1)" in text
    assert "FAIL: crc" in text


def test_status_message():
    status = messages.ProjectStatus(
        name="TPMS ECU", path="C:/p", is_git=True, branch="dev", changed_files=3,
        insertions=84, deletions=31, last_activity_ago=125, build=build_report(True), build_configured=True,
    )
    text = messages.format_status([status], host="PC1", paused=True)
    assert "Notifications are paused" in text
    assert "<b>Modified files:</b> 3 (+84 / -31)" in text
    assert "<b>Build:</b> SUCCESS" in text
    assert "<b>Last activity:</b> 2 minutes ago" in text


def test_time_formatting():
    assert messages.format_duration(277) == "04:37"
    assert messages.format_duration(3725) == "1:02:05"
    assert messages.format_ago(None) == "never"
    assert messages.format_ago(5) == "just now"
    assert messages.format_ago(3600) == "1 hour ago"
