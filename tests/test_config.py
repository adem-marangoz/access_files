from __future__ import annotations

from pathlib import Path

import pytest

from tpms_agent.config import ConfigError, expand_env, load_config, load_dotenv, parse_config


def minimal(**overrides):
    raw = {
        "telegram": {"bot_token": "123456:abc", "chat_id": 42},
        "projects": [{"name": "Demo", "path": "demo"}],
    }
    raw.update(overrides)
    return raw


def test_defaults_and_relative_paths(tmp_path: Path):
    config = parse_config(minimal(), base_dir=tmp_path)
    assert config.telegram.chat_id == "42"
    assert config.notifications.level == 2
    assert config.notifications.send_files is False
    assert config.watch.debounce_seconds == 5
    assert config.projects[0].path == tmp_path / "demo"
    assert config.projects[0].git is None  # auto
    assert config.logging.file == tmp_path / "logs" / "agent.log"
    assert config.authorized_chat_ids == {"42"}


def test_env_expansion_with_default(monkeypatch):
    monkeypatch.setenv("TOKEN_X", "secret")
    monkeypatch.delenv("UNSET_X", raising=False)
    missing: set[str] = set()
    value = expand_env({"a": "${TOKEN_X}", "b": ["${UNSET_X:-fallback}", "${UNSET_X}"]}, missing)
    assert value == {"a": "secret", "b": ["fallback", ""]}
    assert missing == {"UNSET_X"}


def test_missing_env_var_is_named_in_error(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    raw = minimal(telegram={"bot_token": "${TELEGRAM_BOT_TOKEN}", "chat_id": "1"})
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        parse_config(raw, base_dir=tmp_path)


def test_chat_id_optional_for_setup_helper(tmp_path: Path):
    raw = minimal(telegram={"bot_token": "123456:abc"})
    with pytest.raises(ConfigError, match="chat_id"):
        parse_config(raw, base_dir=tmp_path)
    assert parse_config(raw, base_dir=tmp_path, require_chat_id=False).telegram.chat_id == ""


def test_all_errors_are_reported_together(tmp_path: Path):
    raw = minimal(
        notifications={"level": 7, "send_diff": "maybe"},
        projects=[{"name": "A", "path": "a"}, {"name": "a", "path": "b", "build": {"run_on": "never"}}],
    )
    with pytest.raises(ConfigError) as info:
        parse_config(raw, base_dir=tmp_path)
    message = str(info.value)
    assert "notifications.level" in message
    assert "notifications.send_diff" in message
    assert "duplicate project name" in message
    assert "run_on" in message
    assert "command" in message


def test_unknown_keys_produce_warnings(tmp_path: Path):
    config = parse_config(minimal(notifications={"levle": 3}), base_dir=tmp_path)
    assert any("notifications.levle" in w for w in config.warnings)


def test_build_section(tmp_path: Path):
    raw = minimal(projects=[{
        "name": "Fw", "path": "fw", "git": False,
        "build": {"command": ["make", "-j4"], "test_command": "ctest", "run_on": "task_complete", "cwd": "build"},
    }])
    project = parse_config(raw, base_dir=tmp_path).projects[0]
    assert project.git is False
    assert project.build.command == ["make", "-j4"]
    assert project.build.run_on == "task_complete"
    assert project.build.cwd == tmp_path / "fw" / "build"


def test_load_config_reads_dotenv(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MY_BOT_TOKEN", raising=False)
    (tmp_path / ".env").write_text("# comment\nexport MY_BOT_TOKEN='999:from-dotenv'\n", encoding="utf-8")
    (tmp_path / "config.yaml").write_text(
        "telegram:\n  bot_token: ${MY_BOT_TOKEN}\n  chat_id: 5\nprojects:\n  - name: P\n    path: .\n",
        encoding="utf-8",
    )
    config = load_config(tmp_path / "config.yaml")
    assert config.telegram.bot_token == "999:from-dotenv"
    assert config.source == (tmp_path / "config.yaml").resolve()


def test_dotenv_does_not_override_environment(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("KEEP_ME", "env")
    (tmp_path / ".env").write_text("KEEP_ME=file\n", encoding="utf-8")
    load_dotenv(tmp_path / ".env")
    import os

    assert os.environ["KEEP_ME"] == "env"


def test_missing_config_file_has_helpful_message(tmp_path: Path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_example_config_is_valid(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    root = Path(__file__).resolve().parent.parent
    config = load_config(root / "config.example.yaml")
    assert config.notifications.level == 4
    assert [p.name for p in config.projects] == ["TPMS ECU", "ESP Diagnostic Tool", "Diagnostic Tool"]
    assert config.warnings == []
