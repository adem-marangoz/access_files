"""End-to-end tests: real watchdog observer + real Git repositories + fake Telegram."""

from __future__ import annotations

import sys
import time

import pytest
from conftest import git, make_config, requires_git

from tpms_agent.agent import Agent

pytestmark = requires_git


@pytest.fixture
def run_agent(fake_telegram):
    agents = []

    def start(config):
        agent = Agent(config, telegram=fake_telegram)
        agent.start()
        agents.append(agent)
        time.sleep(0.3)  # let the observer settle
        return agent

    yield start
    for agent in agents:
        agent.stop(notify=False)


def has_text(fragment: str):
    return lambda tg: any(fragment in t for t in tg.texts())


def test_level2_git_notification(tmp_path, make_repo, run_agent, fake_telegram):
    repo = make_repo(files={"Sources/CAN_ResponseTask.c": "int a;\n"})
    (repo / ".gitignore").write_text("build/\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-qm", "ignore build")
    (repo / "build").mkdir()
    config = make_config(tmp_path, [{"name": "TPMS ECU", "path": str(repo)}], notifications={"level": 2})
    run_agent(config)

    target = repo / "Sources" / "CAN_ResponseTask.c"
    for i in range(5):  # rapid edits are grouped into one notification
        target.write_text("int a;\n" + "".join(f"int b{j};\n" for j in range(i + 1)))
        time.sleep(0.05)
    (repo / "Sources" / "FirmwareUpdate.c").write_text("void update(void);\n")
    (repo / "build" / "out.o").write_text("ignored by .gitignore")

    assert fake_telegram.wait_for(has_text("Project Updated"))
    time.sleep(0.8)
    texts = fake_telegram.texts()
    assert len(texts) == 1
    text = texts[0]
    assert "<b>Branch:</b> <code>main</code>" in text
    assert "<code>M</code> Sources/CAN_ResponseTask.c  <i>+5 -0</i>" in text
    assert "<code>A</code> Sources/FirmwareUpdate.c  <i>+1 -0</i>" in text
    assert "<b>Changes:</b> +6 / -0 lines" in text
    assert "out.o" not in text
    assert fake_telegram.documents == []  # level 2: no attachments


def test_level3_attaches_patch_and_files(tmp_path, make_repo, run_agent, fake_telegram):
    repo = make_repo(files={"main.c": "int main(void) { return 0; }\n"})
    config = make_config(
        tmp_path,
        [{"name": "Demo", "path": str(repo)}],
        notifications={"level": 3, "send_diff": True, "send_files": True},
    )
    run_agent(config)
    (repo / "main.c").write_text("int main(void) { return 1; }\n")

    assert fake_telegram.wait_for(lambda tg: len(tg.documents) >= 2)
    text = fake_telegram.texts()[0]
    assert '<pre><code class="language-diff">' in text and "+int main(void) { return 1; }" in text
    names = [d["doc"].filename for d in fake_telegram.documents]
    assert names[0].startswith("changes-demo-") and names[0].endswith(".patch")
    assert names[1] == "main.c"
    patch = fake_telegram.documents[0]["doc"].data.decode()
    assert "-int main(void) { return 0; }" in patch


def test_plain_folder_notification(tmp_path, run_agent, fake_telegram):
    folder = tmp_path / "plain"
    folder.mkdir()
    (folder / "keep.txt").write_text("a")
    config = make_config(tmp_path, [{"name": "Notes", "path": str(folder), "git": False}])
    run_agent(config)
    (folder / "keep.txt").write_text("b")
    (folder / "new.txt").write_text("c")
    (folder / "editor.swp").write_text("ignored")

    assert fake_telegram.wait_for(lambda tg: len(tg.texts()) >= 1)
    time.sleep(0.5)
    text = fake_telegram.texts()[0]
    assert "<code>M</code> keep.txt" in text and "<code>A</code> new.txt" in text
    assert "swp" not in text and "Branch" not in text


def test_level4_task_lifecycle_with_build(tmp_path, make_repo, run_agent, fake_telegram):
    repo = make_repo(files={"app.c": "v1\n"})
    (repo / ".gitignore").write_text("out/\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-qm", "ignore out")
    build_cmd = [sys.executable, "-c", "import os; os.makedirs('out', exist_ok=True); open('out/app.bin','w').write('bin'); print('build ok')"]
    config = make_config(
        tmp_path,
        [{"name": "Firmware", "path": str(repo), "build": {"command": build_cmd, "run_on": "task_complete"}}],
        notifications={"level": 4, "send_diff": True},
        watch={"task_idle_seconds": 1.0},
    )
    run_agent(config)

    (repo / "app.c").write_text("v1\nv2\n")
    assert fake_telegram.wait_for(has_text("AI Task Started"))
    (repo / "app.c").write_text("v1\nv2\nv3\n")  # second update within the same task
    assert fake_telegram.wait_for(has_text("AI TASK COMPLETED"), timeout=15)
    time.sleep(0.5)

    texts = fake_telegram.texts()
    titles = [t.splitlines()[0] for t in texts]
    assert titles[0] == "<b>🤖 AI Task Started</b>"
    assert "🔨 <b>Build started</b> · Firmware" in titles
    assert titles[-1] == "<b>🤖 AI TASK COMPLETED</b>"
    completed = texts[-1]
    assert "<b>Build:</b> ✅ SUCCESS" in completed
    assert "<code>M</code> app.c  <i>+2 -0</i>" in completed
    assert "+2 / -0 lines" in completed
    # Build output (out/app.bin) must not start a new task.
    assert sum("AI Task Started" in t for t in texts) == 1
    assert fake_telegram.documents[0]["doc"].filename.endswith(".patch")
    # Intermediate messages are silent, start/completion are not.
    by_title = {m["text"].splitlines()[0]: m["silent"] for m in fake_telegram.messages}
    assert by_title["<b>🤖 AI Task Started</b>"] is False
    assert by_title["<b>🤖 AI TASK COMPLETED</b>"] is False


def test_commit_and_branch_switch_are_reported(tmp_path, make_repo, run_agent, fake_telegram):
    repo = make_repo(files={"a.c": "1\n"})
    config = make_config(tmp_path, [{"name": "P", "path": str(repo)}], notifications={"level": 2})
    run_agent(config)

    (repo / "a.c").write_text("1\n2\n")
    assert fake_telegram.wait_for(has_text("Project Updated"))
    git(repo, "commit", "-qam", "Add line 2")
    assert fake_telegram.wait_for(has_text("Add line 2"))
    git(repo, "checkout", "-q", "-b", "feature")
    assert fake_telegram.wait_for(has_text("switched from <code>main</code>"))


def test_pause_suppresses_notifications(tmp_path, make_repo, run_agent, fake_telegram):
    repo = make_repo(files={"a.c": "1\n"})
    config = make_config(tmp_path, [{"name": "P", "path": str(repo)}], notifications={"level": 2})
    agent = run_agent(config)
    agent.pause()
    (repo / "a.c").write_text("paused change\n")
    time.sleep(1.2)
    assert fake_telegram.texts() == []
    agent.resume()
    (repo / "a.c").write_text("resumed change\n")
    assert fake_telegram.wait_for(has_text("Project Updated"))


def test_no_message_when_file_saved_without_changes(tmp_path, make_repo, run_agent, fake_telegram):
    repo = make_repo(files={"a.c": "same\n"})
    config = make_config(tmp_path, [{"name": "P", "path": str(repo)}], notifications={"level": 2})
    run_agent(config)
    (repo / "a.c").write_text("same\n")
    time.sleep(1.2)
    assert fake_telegram.texts() == []


def test_startup_and_status(tmp_path, make_repo, run_agent, fake_telegram):
    repo = make_repo(files={"a.c": "1\n"})
    (repo / "a.c").write_text("1\n2\n")
    config = make_config(
        tmp_path,
        [{"name": "P", "path": str(repo)}, {"name": "Missing", "path": str(tmp_path / "nope")}],
        notifications={"notify_on_startup": True},
    )
    agent = run_agent(config)
    assert fake_telegram.wait_for(has_text("PC Agent started"))
    startup = fake_telegram.texts()[0]
    assert "• <b>P</b> (<code>main</code>)" in startup
    assert "⚠️ Missing: directory not found" in startup

    statuses = {s.name: s for s in agent.project_statuses()}
    assert statuses["P"].changed_files == 1 and statuses["P"].insertions == 1
    assert statuses["Missing"].available is False


def test_diff_and_build_commands(tmp_path, make_repo, run_agent, fake_telegram):
    repo = make_repo(files={"a.c": "1\n"})
    (repo / "a.c").write_text("1\n2\n")
    build_cmd = [sys.executable, "-c", "print('compiling'); raise SystemExit(2)"]
    config = make_config(
        tmp_path,
        [{"name": "P", "path": str(repo), "build": {"command": build_cmd}}],
        notifications={"send_diff": True},
    )
    agent = run_agent(config)

    agent.send_project_diff("P", "42")
    assert any("Current Diff" in t for t in fake_telegram.texts())
    assert fake_telegram.documents[-1]["doc"].filename.endswith(".patch")
    assert fake_telegram.documents[-1]["chat_id"] == "42"

    agent.request_build("P", "42")
    agent.request_build("P", "42")  # second request while the first is queued/running
    assert fake_telegram.wait_for(has_text("Build finished"))
    assert sum("already queued or running" in t for t in fake_telegram.texts()) == 1
    finished = [t for t in fake_telegram.texts() if "Build finished" in t][0]
    assert "FAILED (exit code 2)" in finished and "compiling" in finished
    assert fake_telegram.wait_for(lambda tg: tg.documents[-1]["doc"].filename.startswith("build-p-"))
