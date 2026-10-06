from __future__ import annotations

from pathlib import Path

from conftest import make_config

from tpms_agent.commands import CommandHandler, CommandPoller, parse_command
from tpms_agent.telegram import TelegramError


class StubAgent:
    def __init__(self, config) -> None:
        self.config = config
        self.replies: list[tuple[str, str]] = []
        self.paused = False
        self.host = "pc"
        self.calls: list[tuple] = []

    def reply(self, chat_id, text):
        self.replies.append((chat_id, text))

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def project_statuses(self, names=None, *, with_git=True):
        self.calls.append(("statuses", names))
        return []

    def send_project_diff(self, name, chat_id):
        self.calls.append(("diff", name, chat_id))

    def request_build(self, name, chat_id):
        self.calls.append(("build", name, chat_id))


def update(text, chat_id=42, update_id=1):
    return {"update_id": update_id, "message": {"chat": {"id": chat_id, "type": "private"}, "text": text}}


def setup(tmp_path: Path, projects=("TPMS ECU", "ESP Diagnostic Tool", "Diagnostic Tool")):
    config = make_config(tmp_path, [{"name": n, "path": str(tmp_path)} for n in projects])
    agent = StubAgent(config)
    return agent, CommandHandler(agent, config.authorized_chat_ids)


def test_parse_command():
    assert parse_command("/status") == ("status", "")
    assert parse_command("/Status@MyPcBot  tpms ecu ") == ("status", "tpms ecu")
    assert parse_command("hello") is None
    assert parse_command("/") is None


def test_unauthorized_chats_are_ignored_silently(tmp_path):
    agent, handler = setup(tmp_path)
    handler.handle_update(update("/stop", chat_id=999))
    assert agent.replies == [] and agent.paused is False


def test_stop_and_start(tmp_path):
    agent, handler = setup(tmp_path)
    handler.handle_update(update("/stop"))
    assert agent.paused
    handler.handle_update(update("/start"))
    assert not agent.paused
    assert "paused" in agent.replies[0][1] and "resumed" in agent.replies[1][1]
    assert all(chat == "42" for chat, _ in agent.replies)


def test_project_resolution(tmp_path):
    agent, handler = setup(tmp_path)
    assert handler.resolve_project("2")[0].name == "ESP Diagnostic Tool"
    assert handler.resolve_project("diagnostic tool")[0].name == "Diagnostic Tool"
    assert handler.resolve_project("tpms")[0].name == "TPMS ECU"
    project, error = handler.resolve_project("")
    assert project is None and "1. TPMS ECU" in error
    assert handler.resolve_project("9")[0] is None
    assert handler.resolve_project("zzz")[0] is None


def test_single_project_needs_no_argument(tmp_path):
    agent, handler = setup(tmp_path, projects=("Only",))
    handler.handle_update(update("/diff"))
    handler.handle_update(update("/build"))
    assert agent.calls == [("diff", "Only", "42"), ("build", "Only", "42")]


def test_status_with_and_without_project(tmp_path):
    agent, handler = setup(tmp_path)
    handler.handle_update(update("/status"))
    handler.handle_update(update("/status 1"))
    assert agent.calls == [("statuses", None), ("statuses", ["TPMS ECU"])]
    assert all("PC Agent" in text for _, text in agent.replies)


def test_unknown_command_and_plain_text(tmp_path):
    agent, handler = setup(tmp_path)
    handler.handle_update(update("/reboot"))
    handler.handle_update(update("hi"))
    assert "Unknown command" in agent.replies[0][1]
    assert "/help" in agent.replies[1][1]


def test_help_lists_commands(tmp_path):
    agent, handler = setup(tmp_path)
    handler.handle_update(update("/help"))
    text = agent.replies[0][1]
    for name in ("/status", "/projects", "/diff", "/build", "/log", "/stop", "/start"):
        assert name in text


def test_log_command(tmp_path):
    agent, handler = setup(tmp_path)
    log_file = agent.config.logging.file
    log_file.parent.mkdir(parents=True)
    log_file.write_text("".join(f"line {i} <x>\n" for i in range(100)))
    handler.handle_update(update("/log 3"))
    text = agent.replies[0][1]
    assert "line 99 &lt;x&gt;" in text and "line 96" not in text


def test_command_errors_are_reported(tmp_path):
    agent, handler = setup(tmp_path)

    def boom(name, chat_id):
        raise RuntimeError("git exploded")

    agent.send_project_diff = boom
    handler.handle_update(update("/diff 1"))
    assert "git exploded" in agent.replies[0][1]


class ScriptedClient:
    def __init__(self, batches):
        self.batches = list(batches)
        self.offsets = []

    def set_my_commands(self, commands):
        self.commands = commands

    def get_updates(self, offset=None, *, timeout=25):
        self.offsets.append(offset)
        if not self.batches:
            raise TelegramError("unauthorized", error_code=401)  # ends the poller
        batch = self.batches.pop(0)
        if isinstance(batch, Exception):
            raise batch
        return batch


def test_poller_skips_old_updates_and_tracks_offset(tmp_path):
    agent, handler = setup(tmp_path)
    client = ScriptedClient([
        [update("/stop", update_id=10)],  # pending before start: skipped
        [update("/stop", update_id=11), update("/help", update_id=12)],
    ])
    poller = CommandPoller(client, handler, poll_timeout=0)
    poller._run()
    assert client.offsets == [-1, 11, 13]
    assert agent.paused  # /stop 11 was handled
    assert [c for c, _ in client.commands][:3] == ["status", "projects", "diff"]
