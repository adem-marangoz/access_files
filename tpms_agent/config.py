"""Configuration loading and validation.

The agent is configured with a YAML file (see ``config.example.yaml``).
String values may reference environment variables as ``${NAME}`` or
``${NAME:-default}`` so secrets such as the bot token never have to be
written into the file. A ``.env`` file next to the config file is loaded
into the environment first (existing variables are never overridden).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_NAME = "config.yaml"
CONFIG_ENV_VAR = "TPMS_AGENT_CONFIG"

BUILD_TRIGGERS = ("manual", "change", "task_complete")

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(Exception):
    """Raised when the configuration file is missing or invalid."""


@dataclass
class TelegramConfig:
    bot_token: str
    chat_id: str
    api_base_url: str = "https://api.telegram.org"
    timeout_seconds: float = 30.0


@dataclass
class CommandsConfig:
    enabled: bool = True
    # Chat IDs allowed to send commands, in addition to telegram.chat_id.
    authorized_chat_ids: list[str] = field(default_factory=list)
    poll_timeout_seconds: int = 25


@dataclass
class NotificationsConfig:
    level: int = 2
    send_files: bool = False
    send_diff: bool = False
    send_build_result: bool = True
    inline_diff_max_lines: int = 20
    max_file_size_kb: int = 1024
    max_files: int = 10
    max_listed_files: int = 25
    notify_on_startup: bool = True
    # Level 4: intermediate "files modified" messages arrive without a sound.
    silent_intermediate: bool = True


@dataclass
class WatchConfig:
    debounce_seconds: float = 5.0
    max_batch_seconds: float = 60.0
    task_idle_seconds: float = 120.0
    ignore: list[str] = field(default_factory=list)
    use_default_ignores: bool = True


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: Path | None = None
    max_bytes: int = 1_000_000
    backup_count: int = 3


@dataclass
class BuildConfig:
    command: str | list[str] | None = None
    test_command: str | list[str] | None = None
    run_on: str = "manual"
    timeout_seconds: float = 600.0
    cwd: Path | None = None


@dataclass
class ProjectConfig:
    name: str
    path: Path
    # None means "detect automatically".
    git: bool | None = None
    ignore: list[str] = field(default_factory=list)
    build: BuildConfig | None = None


@dataclass
class AgentConfig:
    telegram: TelegramConfig
    commands: CommandsConfig
    notifications: NotificationsConfig
    watch: WatchConfig
    logging: LoggingConfig
    projects: list[ProjectConfig]
    source: Path | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def authorized_chat_ids(self) -> set[str]:
        ids = {str(self.telegram.chat_id)} if self.telegram.chat_id else set()
        ids.update(str(c) for c in self.commands.authorized_chat_ids)
        return ids

    def find_project(self, name: str) -> ProjectConfig | None:
        for project in self.projects:
            if project.name.casefold() == name.casefold():
                return project
        return None


def load_dotenv(path: Path) -> dict[str, str]:
    """Load ``KEY=VALUE`` lines from *path* into ``os.environ``.

    Variables that are already set win over the file. Returns the values
    that were read (including ones that were not applied).
    """
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
        os.environ.setdefault(key, value)
    return values


def expand_env(value: Any, missing: set[str]) -> Any:
    """Recursively expand ``${VAR}`` references in strings.

    Unset variables without a default expand to an empty string and are
    recorded in *missing* so validation can name them in its error.
    """
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            missing.add(name)
            return ""

        return _ENV_REF.sub(replace, value)
    if isinstance(value, list):
        return [expand_env(v, missing) for v in value]
    if isinstance(value, dict):
        return {k: expand_env(v, missing) for k, v in value.items()}
    return value


def find_config_file(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Locate the configuration file.

    Order: the explicit path, ``$TPMS_AGENT_CONFIG``, ``./config.yaml``,
    then ``config.yaml`` next to the program (useful for the packaged exe).
    """
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigError(f"Config file not found: {path}")
        return path.resolve()
    candidates = []
    if os.environ.get(CONFIG_ENV_VAR):
        candidates.append(Path(os.environ[CONFIG_ENV_VAR]).expanduser())
    candidates.append(Path.cwd() / DEFAULT_CONFIG_NAME)
    candidates.append(_program_dir() / DEFAULT_CONFIG_NAME)
    for path in candidates:
        if path.is_file():
            return path.resolve()
    searched = "\n  ".join(str(p) for p in candidates)
    raise ConfigError(
        "No config file found. Copy config.example.yaml to config.yaml and edit it.\n"
        f"Searched:\n  {searched}"
    )


def _program_dir() -> Path:
    import sys

    if getattr(sys, "frozen", False):  # PyInstaller executable
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def load_config(
    path: str | os.PathLike[str] | None = None,
    *,
    require_chat_id: bool = True,
) -> AgentConfig:
    """Load, expand and validate the configuration file."""
    config_path = find_config_file(path)
    load_dotenv(config_path.parent / ".env")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8-sig"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{config_path}: invalid YAML: {exc}") from exc
    config = parse_config(raw or {}, base_dir=config_path.parent, require_chat_id=require_chat_id)
    config.source = config_path
    return config


def parse_config(
    raw: dict[str, Any],
    *,
    base_dir: Path,
    require_chat_id: bool = True,
) -> AgentConfig:
    """Build an :class:`AgentConfig` from already-parsed YAML data."""
    if not isinstance(raw, dict):
        raise ConfigError("The config file must contain a mapping at the top level.")
    missing_env: set[str] = set()
    raw = expand_env(raw, missing_env)
    v = _Validator(missing_env)

    known_sections = {"telegram", "commands", "notifications", "watch", "logging", "projects"}
    v.unknown_keys(raw, known_sections, "")

    tg = v.section(raw, "telegram")
    v.unknown_keys(tg, {"bot_token", "chat_id", "api_base_url", "timeout_seconds"}, "telegram")
    telegram = TelegramConfig(
        bot_token=v.string(tg, "bot_token", "telegram", required=True),
        chat_id=v.string(tg, "chat_id", "telegram", required=require_chat_id),
        api_base_url=v.string(tg, "api_base_url", "telegram", default="https://api.telegram.org").rstrip("/"),
        timeout_seconds=v.number(tg, "timeout_seconds", "telegram", default=30.0, minimum=1),
    )

    cm = v.section(raw, "commands")
    v.unknown_keys(cm, {"enabled", "authorized_chat_ids", "poll_timeout_seconds"}, "commands")
    commands = CommandsConfig(
        enabled=v.boolean(cm, "enabled", "commands", default=True),
        authorized_chat_ids=[str(c) for c in v.list_of(cm, "authorized_chat_ids", "commands", (str, int))],
        poll_timeout_seconds=int(v.number(cm, "poll_timeout_seconds", "commands", default=25, minimum=0, maximum=50)),
    )

    nt = v.section(raw, "notifications")
    nt_defaults = NotificationsConfig()
    v.unknown_keys(nt, set(NotificationsConfig.__dataclass_fields__), "notifications")
    notifications = NotificationsConfig(
        level=int(v.number(nt, "level", "notifications", default=nt_defaults.level, minimum=1, maximum=4, integer=True)),
        send_files=v.boolean(nt, "send_files", "notifications", default=nt_defaults.send_files),
        send_diff=v.boolean(nt, "send_diff", "notifications", default=nt_defaults.send_diff),
        send_build_result=v.boolean(nt, "send_build_result", "notifications", default=nt_defaults.send_build_result),
        inline_diff_max_lines=int(v.number(nt, "inline_diff_max_lines", "notifications", default=nt_defaults.inline_diff_max_lines, minimum=0, integer=True)),
        max_file_size_kb=int(v.number(nt, "max_file_size_kb", "notifications", default=nt_defaults.max_file_size_kb, minimum=1, maximum=50 * 1024, integer=True)),
        max_files=int(v.number(nt, "max_files", "notifications", default=nt_defaults.max_files, minimum=0, integer=True)),
        max_listed_files=int(v.number(nt, "max_listed_files", "notifications", default=nt_defaults.max_listed_files, minimum=1, integer=True)),
        notify_on_startup=v.boolean(nt, "notify_on_startup", "notifications", default=nt_defaults.notify_on_startup),
        silent_intermediate=v.boolean(nt, "silent_intermediate", "notifications", default=nt_defaults.silent_intermediate),
    )

    wt = v.section(raw, "watch")
    wt_defaults = WatchConfig()
    v.unknown_keys(wt, set(WatchConfig.__dataclass_fields__), "watch")
    watch = WatchConfig(
        debounce_seconds=v.number(wt, "debounce_seconds", "watch", default=wt_defaults.debounce_seconds, minimum=0.05),
        max_batch_seconds=v.number(wt, "max_batch_seconds", "watch", default=wt_defaults.max_batch_seconds, minimum=0.1),
        task_idle_seconds=v.number(wt, "task_idle_seconds", "watch", default=wt_defaults.task_idle_seconds, minimum=1),
        ignore=v.list_of(wt, "ignore", "watch", (str,)),
        use_default_ignores=v.boolean(wt, "use_default_ignores", "watch", default=True),
    )
    if watch.max_batch_seconds < watch.debounce_seconds:
        v.warn("watch.max_batch_seconds is smaller than watch.debounce_seconds; using debounce_seconds")
        watch.max_batch_seconds = watch.debounce_seconds

    lg = v.section(raw, "logging")
    v.unknown_keys(lg, {"level", "file", "max_bytes", "backup_count"}, "logging")
    level = v.string(lg, "level", "logging", default="INFO").upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        v.error(f"logging.level must be DEBUG, INFO, WARNING, ERROR or CRITICAL (got {level!r})")
    log_file = v.string(lg, "file", "logging", default="logs/agent.log")
    logging_config = LoggingConfig(
        level=level,
        file=_resolve_path(log_file, base_dir) if log_file else None,
        max_bytes=int(v.number(lg, "max_bytes", "logging", default=1_000_000, minimum=10_000, integer=True)),
        backup_count=int(v.number(lg, "backup_count", "logging", default=3, minimum=0, integer=True)),
    )

    projects: list[ProjectConfig] = []
    raw_projects = raw.get("projects")
    if raw_projects is None or raw_projects == []:
        v.error("projects: at least one project must be configured")
    elif not isinstance(raw_projects, list):
        v.error("projects must be a list")
    else:
        seen: set[str] = set()
        for index, item in enumerate(raw_projects):
            where = f"projects[{index}]"
            if not isinstance(item, dict):
                v.error(f"{where} must be a mapping with at least 'name' and 'path'")
                continue
            project = _parse_project(item, where, base_dir, v)
            if project is None:
                continue
            key = project.name.casefold()
            if key in seen:
                v.error(f"{where}: duplicate project name {project.name!r}")
            seen.add(key)
            projects.append(project)

    v.raise_if_errors()
    return AgentConfig(
        telegram=telegram,
        commands=commands,
        notifications=notifications,
        watch=watch,
        logging=logging_config,
        projects=projects,
        warnings=v.warnings,
    )


def _parse_project(item: dict[str, Any], where: str, base_dir: Path, v: "_Validator") -> ProjectConfig | None:
    v.unknown_keys(item, {"name", "path", "git", "ignore", "build"}, where)
    name = v.string(item, "name", where, required=True)
    path_str = v.string(item, "path", where, required=True)
    if not name or not path_str:
        return None
    git_value = item.get("git", "auto")
    git: bool | None
    if isinstance(git_value, bool):
        git = git_value
    elif isinstance(git_value, str) and git_value.lower() == "auto":
        git = None
    else:
        v.error(f"{where}.git must be true, false or auto")
        git = None

    build = None
    raw_build = item.get("build")
    if raw_build is not None:
        bw = f"{where}.build"
        if not isinstance(raw_build, dict):
            v.error(f"{bw} must be a mapping")
        else:
            v.unknown_keys(raw_build, {"command", "test_command", "run_on", "timeout_seconds", "cwd"}, bw)
            run_on = v.string(raw_build, "run_on", bw, default="manual")
            if run_on not in BUILD_TRIGGERS:
                v.error(f"{bw}.run_on must be one of {', '.join(BUILD_TRIGGERS)} (got {run_on!r})")
            command = _command_value(raw_build, "command", bw, v)
            test_command = _command_value(raw_build, "test_command", bw, v)
            if not command and not test_command:
                v.error(f"{bw} needs a 'command' and/or a 'test_command'")
            cwd = v.string(raw_build, "cwd", bw, default="")
            project_path = _resolve_path(path_str, base_dir)
            build = BuildConfig(
                command=command,
                test_command=test_command,
                run_on=run_on,
                timeout_seconds=v.number(raw_build, "timeout_seconds", bw, default=600.0, minimum=1),
                cwd=_resolve_path(cwd, project_path) if cwd else None,
            )

    return ProjectConfig(
        name=name,
        path=_resolve_path(path_str, base_dir),
        git=git,
        ignore=v.list_of(item, "ignore", where, (str,)),
        build=build,
    )


def _command_value(data: dict[str, Any], key: str, where: str, v: "_Validator") -> str | list[str] | None:
    value = data.get(key)
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, list) and value and all(isinstance(x, (str, int, float)) for x in value):
        return [str(x) for x in value]
    v.error(f"{where}.{key} must be a string or a non-empty list of strings")
    return None


def _resolve_path(value: str, base_dir: Path) -> Path:
    path = Path(os.path.expandvars(os.path.expanduser(value)))
    if not path.is_absolute():
        path = base_dir / path
    return Path(os.path.normpath(path))


class _Validator:
    """Collects all configuration problems so they can be reported at once."""

    def __init__(self, missing_env: set[str]) -> None:
        self.missing_env = missing_env
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def raise_if_errors(self) -> None:
        if self.errors:
            details = "\n  - ".join(self.errors)
            raise ConfigError(f"Invalid configuration:\n  - {details}")

    def section(self, raw: dict[str, Any], name: str) -> dict[str, Any]:
        value = raw.get(name)
        if value is None:
            return {}
        if not isinstance(value, dict):
            self.error(f"{name} must be a mapping")
            return {}
        return value

    def unknown_keys(self, data: dict[str, Any], known: set[str], where: str) -> None:
        for key in data:
            if key not in known:
                location = f"{where}.{key}" if where else str(key)
                self.warn(f"Unknown config key '{location}' is ignored")

    def string(self, data: dict[str, Any], key: str, where: str, *, required: bool = False, default: str = "") -> str:
        value = data.get(key)
        if value is None or value == "":
            if required:
                hint = self._env_hint()
                self.error(f"{where}.{key} is required{hint}")
            return default
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            self.error(f"{where}.{key} must be a string")
            return default
        return str(value).strip()

    def _env_hint(self) -> str:
        if self.missing_env:
            names = ", ".join(sorted(self.missing_env))
            return f" (environment variable(s) not set: {names})"
        return ""

    def boolean(self, data: dict[str, Any], key: str, where: str, *, default: bool) -> bool:
        value = data.get(key)
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "yes", "on", "1", "false", "no", "off", "0"):
            return value.lower() in ("true", "yes", "on", "1")
        self.error(f"{where}.{key} must be true or false")
        return default

    def number(
        self,
        data: dict[str, Any],
        key: str,
        where: str,
        *,
        default: float,
        minimum: float | None = None,
        maximum: float | None = None,
        integer: bool = False,
    ) -> float:
        value = data.get(key)
        if value is None or value == "":
            return default
        if isinstance(value, bool):
            self.error(f"{where}.{key} must be a number")
            return default
        try:
            number = float(value)
        except (TypeError, ValueError):
            self.error(f"{where}.{key} must be a number")
            return default
        if integer and not number.is_integer():
            self.error(f"{where}.{key} must be a whole number")
            return default
        if minimum is not None and number < minimum:
            self.error(f"{where}.{key} must be >= {minimum:g}")
            return default
        if maximum is not None and number > maximum:
            self.error(f"{where}.{key} must be <= {maximum:g}")
            return default
        return number

    def list_of(self, data: dict[str, Any], key: str, where: str, types: tuple[type, ...]) -> list[Any]:
        value = data.get(key)
        if value is None:
            return []
        if not isinstance(value, list) or not all(isinstance(x, types) and not isinstance(x, bool) for x in value):
            self.error(f"{where}.{key} must be a list")
            return []
        return [x for x in value if x != ""]
