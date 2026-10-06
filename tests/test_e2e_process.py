"""Runs the real ``python agent.py`` process against a fake Bot API server."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import requires_git
from fake_bot_api import FakeBotApi

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "123456:E2E-test-token"

pytestmark = [
    requires_git,
    pytest.mark.skipif(os.name == "nt", reason="uses SIGTERM to stop the agent"),
]


@pytest.fixture
def bot_api():
    api = FakeBotApi(TOKEN).start()
    yield api
    api.stop()


def write_config(tmp_path: Path, api_url: str, repo: Path) -> Path:
    config = tmp_path / "config.yaml"
    config.write_text(
        f"""
telegram:
  bot_token: "${{TEST_BOT_TOKEN}}"
  chat_id: "42"
  api_base_url: "{api_url}"
commands:
  enabled: true
  poll_timeout_seconds: 1
notifications:
  level: 3
  send_diff: true
  send_files: true
watch:
  debounce_seconds: 0.3
  task_idle_seconds: 30
projects:
  - name: TPMS ECU
    path: "{repo.as_posix()}"
    git: true
""",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(f"TEST_BOT_TOKEN={TOKEN}\n", encoding="utf-8")
    return config


def agent_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "TEST_BOT_TOKEN"}  # token comes from .env
    no_proxy = ",".join(filter(None, [env.get("NO_PROXY", ""), "127.0.0.1", "localhost"]))
    env["NO_PROXY"] = env["no_proxy"] = no_proxy  # the fake server is local
    return env


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    env = agent_env()
    return subprocess.run(
        [sys.executable, str(ROOT / "agent.py"), *args], capture_output=True, text=True, timeout=60, env=env, cwd=ROOT
    )


def test_full_agent_process(tmp_path, make_repo, bot_api):
    repo = make_repo(files={"Sources/CAN_ResponseTask.c": "FLEXCAN_DRV_Send(INST_CANCOM1, TX_MAILBOX_PC, ...);\n"})
    config = write_config(tmp_path, bot_api.url, repo)

    env = agent_env()
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "agent.py"), "run", "-c", str(config)],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        assert bot_api.wait_for(lambda api: any("PC Agent started" in t for t in api.texts())), "no startup message"
        assert bot_api.wait_for(lambda api: any(c["method"] == "setMyCommands" for c in api.calls))
        time.sleep(0.5)

        # A change produces a notification with inline preview + patch + file.
        target = repo / "Sources" / "CAN_ResponseTask.c"
        target.write_text(target.read_text() + "vTaskDelay(pdMS_TO_TICKS(10));\n")
        assert bot_api.wait_for(lambda api: any("Project Updated" in t for t in api.texts()))
        update = next(t for t in bot_api.texts() if "Project Updated" in t)
        assert "+vTaskDelay(pdMS_TO_TICKS(10));" in update
        assert bot_api.wait_for(lambda api: len(api.uploaded_files()) >= 2)
        names = [name for name, _ in bot_api.uploaded_files()]
        assert names[0].startswith("changes-tpms-ecu-") and "CAN_ResponseTask.c" in names
        patch = dict(bot_api.uploaded_files())[names[0]].decode()
        assert "+vTaskDelay(pdMS_TO_TICKS(10));" in patch

        # Commands from the authorized chat are answered...
        bot_api.push_message("/status", chat_id=42)
        assert bot_api.wait_for(lambda api: any("<b>Modified files:</b> 1 (+1 / -0)" in t for t in api.texts("42")))
        # ...commands from anyone else are ignored.
        before = len(bot_api.calls)
        bot_api.push_message("/stop", chat_id=666)
        bot_api.push_message("/help", chat_id=42)
        assert bot_api.wait_for(lambda api: any("PC Agent commands" in t for t in api.texts()))
        assert not any(c["fields"].get("chat_id") == "666" for c in bot_api.calls[before:])
        assert not any("paused" in t for t in bot_api.texts())

        proc.send_signal(signal.SIGTERM)
        output, _ = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    assert proc.returncode == 0, output
    assert any("PC Agent stopped" in t for t in bot_api.texts())
    assert TOKEN not in output
    log_text = (tmp_path / "logs" / "agent.log").read_text()
    assert "Ignoring message from unauthorized chat 666" in log_text
    assert TOKEN not in log_text


def test_cli_check_send_report_and_chat_id(tmp_path, make_repo, bot_api):
    repo = make_repo(files={"a.c": "1\n"})
    (repo / "a.c").write_text("1\n2\n")
    config = write_config(tmp_path, bot_api.url, repo)

    check = run_cli("check", "-c", str(config))
    assert check.returncode == 0, check.stdout + check.stderr
    assert "Telegram bot: @test_pc_bot [ OK ]" in check.stdout
    assert "TPMS ECU" in check.stdout and "branch main" in check.stdout

    sent = run_cli("send", "-c", str(config), "hello <world>")
    assert sent.returncode == 0
    assert "hello &lt;world&gt;" in bot_api.texts()

    report = run_cli("report", "-c", str(config), "--project", "tpms ecu", "--title", "🤖 AI TASK COMPLETED")
    assert report.returncode == 0, report.stdout + report.stderr
    assert any(t.startswith("<b>🤖 AI TASK COMPLETED</b>") and "a.c" in t for t in bot_api.texts())

    bot_api.push_message("hi", chat_id=4242)
    chat_id = run_cli("chat-id", "-c", str(config))
    assert "chat_id: 4242" in chat_id.stdout


def test_cli_reports_config_errors(tmp_path):
    bad = tmp_path / "config.yaml"
    bad.write_text("telegram: {}\nprojects: []\n", encoding="utf-8")
    result = run_cli("check", "-c", str(bad))
    assert result.returncode == 2
    assert "telegram.bot_token is required" in result.stderr
    assert "at least one project" in result.stderr
