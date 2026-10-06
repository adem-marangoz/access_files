"""Two-way Telegram commands (/status, /diff, /build, ...).

Commands are received by long-polling ``getUpdates`` (an outgoing HTTPS
request, so it works behind NAT/firewalls). Only chats listed in the
allowlist (``telegram.chat_id`` plus ``commands.authorized_chat_ids``)
are served; everything else is logged and ignored without a reply.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

from . import messages
from .config import ProjectConfig
from .logger import tail
from .telegram import TelegramClient, TelegramError, escape

if TYPE_CHECKING:
    from .agent import Agent

log = logging.getLogger(__name__)

BOT_COMMANDS = [(name, description) for name, _args, description in messages.COMMAND_HELP]


def parse_command(text: str) -> tuple[str, str] | None:
    """``"/status@MyBot tpms ecu"`` -> ``("status", "tpms ecu")``."""
    text = text.strip()
    if not text.startswith("/"):
        return None
    head, _, rest = text.partition(" ")
    name = head[1:].split("@", 1)[0].lower()
    if not name:
        return None
    return name, rest.strip()


class CommandHandler:
    def __init__(self, agent: "Agent", authorized_chat_ids: set[str]) -> None:
        self.agent = agent
        self.authorized = {str(c) for c in authorized_chat_ids}

    # -- dispatch --------------------------------------------------------

    def handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id", ""))
        text = message.get("text") or ""
        sender = (message.get("from") or {}).get("username") or chat.get("title") or "?"
        if chat_id not in self.authorized:
            log.warning("Ignoring message from unauthorized chat %s (@%s): %r", chat_id, sender, text[:50])
            return
        parsed = parse_command(text)
        if parsed is None:
            self.agent.reply(chat_id, "Send /help to see the available commands.")
            return
        name, args = parsed
        handler = getattr(self, f"cmd_{name}", None)
        if handler is None:
            self.agent.reply(chat_id, f"Unknown command /{escape(name)}. Send /help for the list.")
            return
        log.info("Command /%s %s from chat %s", name, args, chat_id)
        try:
            handler(chat_id, args)
        except Exception as exc:
            log.exception("Command /%s failed", name)
            self.agent.reply(chat_id, f"⚠️ /{escape(name)} failed: {escape(exc)}")

    def resolve_project(self, arg: str) -> tuple[ProjectConfig | None, str | None]:
        """Find a project by number, name or unique name prefix."""
        projects = self.agent.config.projects
        arg = arg.strip()
        if not arg:
            if len(projects) == 1:
                return projects[0], None
            return None, "Please name a project:\n" + self._project_choices()
        if arg.isdigit():
            index = int(arg) - 1
            if 0 <= index < len(projects):
                return projects[index], None
            return None, f"There is no project number {escape(arg)}.\n" + self._project_choices()
        folded = arg.casefold()
        for project in projects:
            if project.name.casefold() == folded:
                return project, None
        matches = [p for p in projects if p.name.casefold().startswith(folded)]
        if len(matches) == 1:
            return matches[0], None
        return None, f"No unique project matches “{escape(arg)}”.\n" + self._project_choices()

    def _project_choices(self) -> str:
        return "\n".join(f"{i}. {escape(p.name)}" for i, p in enumerate(self.agent.config.projects, start=1))

    # -- commands --------------------------------------------------------

    def cmd_help(self, chat_id: str, args: str) -> None:
        self.agent.reply(chat_id, messages.format_help())

    def cmd_start(self, chat_id: str, args: str) -> None:
        was_paused = self.agent.paused
        self.agent.resume()
        text = "▶️ Notifications resumed." if was_paused else "▶️ Notifications are active."
        self.agent.reply(chat_id, f"{text}\nSend /help for commands.")

    def cmd_stop(self, chat_id: str, args: str) -> None:
        self.agent.pause()
        self.agent.reply(chat_id, "⏸ Notifications paused. Changes are ignored until you send /start.")

    def cmd_projects(self, chat_id: str, args: str) -> None:
        self.agent.reply(chat_id, messages.format_projects(self.agent.project_statuses(with_git=False)))

    def cmd_status(self, chat_id: str, args: str) -> None:
        names = None
        if args:
            project, error = self.resolve_project(args)
            if project is None:
                self.agent.reply(chat_id, error or "")
                return
            names = [project.name]
        statuses = self.agent.project_statuses(names)
        self.agent.reply(chat_id, messages.format_status(statuses, host=self.agent.host, paused=self.agent.paused))

    def cmd_diff(self, chat_id: str, args: str) -> None:
        project, error = self.resolve_project(args)
        if project is None:
            self.agent.reply(chat_id, error or "")
            return
        self.agent.send_project_diff(project.name, chat_id)

    def cmd_build(self, chat_id: str, args: str) -> None:
        project, error = self.resolve_project(args)
        if project is None:
            self.agent.reply(chat_id, error or "")
            return
        self.agent.request_build(project.name, chat_id)

    def cmd_log(self, chat_id: str, args: str) -> None:
        try:
            count = int(args) if args else 30
        except ValueError:
            count = 30
        count = max(1, min(count, 200))
        text = tail(self.agent.config.logging.file, count)
        if not text.strip():
            self.agent.reply(chat_id, "The log is empty (or logging to a file is disabled).")
            return
        text = text[-3500:]
        self.agent.reply(chat_id, f"📄 <b>Last {count} log lines</b>\n<pre>{escape(text)}</pre>")


class CommandPoller:
    """Long-polls Telegram for commands on a background thread."""

    def __init__(self, client: TelegramClient, handler: CommandHandler, *, poll_timeout: int = 25) -> None:
        self.client = client
        self.handler = handler
        self.poll_timeout = poll_timeout
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="telegram-commands", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _skip_pending(self) -> int | None:
        """Ignore commands sent while the agent was not running."""
        try:
            updates = self.client.get_updates(-1, timeout=0)
        except TelegramError as exc:
            log.warning("Could not read pending Telegram updates: %s", exc)
            return None
        if updates:
            return int(updates[-1]["update_id"]) + 1
        return None

    def _run(self) -> None:
        try:
            self.client.set_my_commands(BOT_COMMANDS)  # command menu in the Telegram app
        except TelegramError as exc:
            log.warning("Could not register the bot command menu: %s", exc)
        offset = self._skip_pending()
        backoff = 1.0
        while not self._stop.is_set():
            try:
                updates = self.client.get_updates(offset, timeout=self.poll_timeout)
            except TelegramError as exc:
                if exc.error_code == 401:
                    log.error("Telegram rejected the bot token; command polling stopped")
                    return
                if exc.error_code == 409:
                    log.warning(
                        "Telegram reports a conflict (another agent instance or a webhook is "
                        "consuming updates): %s. Retrying in 30s",
                        exc.description,
                    )
                    self._stop.wait(30)
                else:
                    log.warning("Polling Telegram failed: %s; retrying in %.0fs", exc, backoff)
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, 60.0)
                continue
            backoff = 1.0
            for update in updates:
                offset = int(update.get("update_id", 0)) + 1
                if self._stop.is_set():
                    return
                try:
                    self.handler.handle_update(update)
                except Exception:
                    log.exception("Failed to handle Telegram update")
