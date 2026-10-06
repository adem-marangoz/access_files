from __future__ import annotations

import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from tpms_agent.config import parse_config
from tpms_agent.telegram import Document

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def make_repo(tmp_path: Path) -> Callable[..., Path]:
    """Create a Git repository with an optional initial commit."""

    def factory(name: str = "repo", files: dict[str, str] | None = None, commit: bool = True) -> Path:
        repo = tmp_path / name
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.email", "test@example.com")
        git(repo, "config", "user.name", "Test")
        git(repo, "config", "commit.gpgsign", "false")
        for rel, content in (files or {}).items():
            path = repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        if commit and files:
            git(repo, "add", "-A")
            git(repo, "commit", "-q", "-m", "initial")
        return repo

    return factory


class FakeTelegram:
    """Records what the agent sends instead of calling the Bot API."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.documents: list[dict[str, Any]] = []
        self._cond = threading.Condition()

    def send_message(self, text: str, *, chat_id: str | None = None, silent: bool = False, parse_mode: str | None = "HTML"):
        with self._cond:
            self.messages.append({"text": text, "chat_id": chat_id, "silent": silent})
            self._cond.notify_all()
        return []

    def send_documents(self, documents: list[Document], *, chat_id: str | None = None, silent: bool = False):
        with self._cond:
            for doc in documents:
                self.documents.append({"doc": doc, "chat_id": chat_id, "silent": silent})
            self._cond.notify_all()

    def send_document(self, document: Document, **kwargs: Any):
        self.send_documents([document], **kwargs)

    def get_updates(self, offset=None, *, timeout: int = 25):
        time.sleep(0.05)
        return []

    def set_my_commands(self, commands):
        pass

    def texts(self) -> list[str]:
        with self._cond:
            return [m["text"] for m in self.messages]

    def wait_for(self, predicate: Callable[["FakeTelegram"], bool], timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._cond:
            while not predicate(self):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            return True


@pytest.fixture
def fake_telegram() -> FakeTelegram:
    return FakeTelegram()


def make_config(tmp_path: Path, projects: list[dict[str, Any]], **sections: dict[str, Any]):
    raw: dict[str, Any] = {
        "telegram": {"bot_token": "123456:TEST-TOKEN-abcdef", "chat_id": "42"},
        "commands": {"enabled": False},
        "notifications": {"notify_on_startup": False},
        "watch": {"debounce_seconds": 0.3, "max_batch_seconds": 5, "task_idle_seconds": 1},
        "logging": {"file": str(tmp_path / "logs" / "agent.log")},
        "projects": projects,
    }
    for name, values in sections.items():
        raw.setdefault(name, {}).update(values)
    return parse_config(raw, base_dir=tmp_path)
