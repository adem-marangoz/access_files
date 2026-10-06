"""Minimal Telegram Bot API client (sendMessage, sendDocument, getUpdates...).

The PC only makes outgoing HTTPS requests to the Bot API, so no port
forwarding, static IP or shared network with the phone is needed.
See https://core.telegram.org/bots/api
"""

from __future__ import annotations

import html
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

import requests

log = logging.getLogger(__name__)

API_BASE_URL = "https://api.telegram.org"
# Telegram allows 4096 characters per message and 1024 per caption; keep a
# margin because the limit is measured after entity parsing (UTF-16).
MAX_MESSAGE_CHARS = 4000
MAX_CAPTION_CHARS = 1000
MAX_MEDIA_GROUP = 10


class TelegramError(Exception):
    def __init__(
        self,
        message: str,
        *,
        error_code: int | None = None,
        description: str = "",
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.description = description
        self.retry_after = retry_after


@dataclass(frozen=True)
class Document:
    filename: str
    data: bytes
    caption: str | None = None


def escape(text: object) -> str:
    """Escape text for Telegram's HTML parse mode."""
    return html.escape(str(text), quote=False)


def strip_html(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def split_message(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Split *text* into chunks of at most *limit* characters.

    Splits preferably between paragraphs, then between lines, so that the
    HTML of each chunk stays balanced in the common case.
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""

    def push(piece: str, sep: str) -> None:
        nonlocal current
        candidate = f"{current}{sep}{piece}" if current else piece
        if len(candidate) <= limit:
            current = candidate
            return
        if current:
            chunks.append(current)
        current = piece

    for paragraph in text.split("\n\n"):
        if len(paragraph) <= limit:
            push(paragraph, "\n\n")
            continue
        for line in paragraph.split("\n"):
            while len(line) > limit:
                push(line[:limit], "\n")
                line = line[limit:]
            push(line, "\n")
    if current:
        chunks.append(current)
    return [c for c in chunks if c.strip()]


def _form_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


class TelegramClient:
    def __init__(
        self,
        token: str,
        chat_id: str | int | None,
        *,
        api_base_url: str = API_BASE_URL,
        timeout: float = 30.0,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.token = token
        self.chat_id = str(chat_id) if chat_id not in (None, "") else None
        self.api_base_url = api_base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self._sleep = sleep
        self._local = threading.local()

    # -- transport -------------------------------------------------------

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            self._local.session = session
        return session

    def redact(self, text: object) -> str:
        message = str(text)
        return message.replace(self.token, "<redacted>") if self.token else message

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        files: dict[str, tuple[str, bytes]] | None = None,
        timeout: float | None = None,
        retries: int | None = None,
    ) -> Any:
        """Call a Bot API method and return its ``result``.

        Network errors, HTTP 5xx and 429 (flood control) are retried.
        """
        url = f"{self.api_base_url}/bot{self.token}/{method}"
        data = {k: _form_value(v) for k, v in (params or {}).items() if v is not None}
        retries = self.max_retries if retries is None else retries
        timeout = self.timeout if timeout is None else timeout
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self._session().post(url, data=data, files=files, timeout=timeout)
            except requests.RequestException as exc:
                if attempt > retries:
                    raise TelegramError(f"{method}: network error: {self.redact(exc)}") from None
                delay = min(2 ** (attempt - 1), 30)
                log.warning("Telegram %s failed (%s); retrying in %ss", method, self.redact(exc), delay)
                self._sleep(delay)
                continue

            try:
                payload = response.json()
            except ValueError:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            if response.ok and payload.get("ok"):
                return payload.get("result")

            code = int(payload.get("error_code") or response.status_code)
            description = str(payload.get("description") or response.text[:200]).strip()
            retry_after = (payload.get("parameters") or {}).get("retry_after")
            if attempt <= retries and (code == 429 or code >= 500):
                delay = float(retry_after) if retry_after else min(2 ** (attempt - 1), 30)
                delay = min(delay, 60.0)
                log.warning("Telegram %s returned %s (%s); retrying in %.0fs", method, code, description, delay)
                self._sleep(delay)
                continue
            raise TelegramError(
                f"{method} failed ({code}): {self.redact(description)}",
                error_code=code,
                description=description,
                retry_after=retry_after,
            )

    def _target(self, chat_id: str | int | None) -> str:
        target = chat_id if chat_id not in (None, "") else self.chat_id
        if target in (None, ""):
            raise TelegramError("No chat_id configured")
        return str(target)

    # -- API methods -----------------------------------------------------

    def get_me(self) -> dict[str, Any]:
        return self.call("getMe", retries=1)

    def send_chat_action(self, action: str = "typing", *, chat_id: str | int | None = None) -> None:
        self.call("sendChatAction", {"chat_id": self._target(chat_id), "action": action}, retries=1)

    def send_message(
        self,
        text: str,
        *,
        chat_id: str | int | None = None,
        silent: bool = False,
        parse_mode: str | None = "HTML",
    ) -> list[Any]:
        """Send *text*, split into several messages if it is too long."""
        target = self._target(chat_id)
        results = []
        for chunk in split_message(text):
            params = {
                "chat_id": target,
                "text": chunk,
                "parse_mode": parse_mode,
                "disable_notification": silent,
                "link_preview_options": {"is_disabled": True},
            }
            try:
                results.append(self.call("sendMessage", params))
            except TelegramError as exc:
                if parse_mode and exc.error_code == 400 and "parse" in exc.description.lower():
                    # Broken markup (e.g. a split <pre> block): send as plain text.
                    log.warning("Telegram rejected message markup (%s); resending as plain text", exc.description)
                    params.update(text=strip_html(chunk), parse_mode=None)
                    results.append(self.call("sendMessage", params))
                else:
                    raise
        return results

    def send_document(
        self,
        document: Document,
        *,
        chat_id: str | int | None = None,
        silent: bool = False,
    ) -> Any:
        params = {
            "chat_id": self._target(chat_id),
            "disable_notification": silent,
        }
        if document.caption:
            params["caption"] = document.caption[:MAX_CAPTION_CHARS]
            params["parse_mode"] = "HTML"
        return self.call(
            "sendDocument",
            params,
            files={"document": (document.filename, document.data)},
            timeout=max(self.timeout, 120),
        )

    def send_documents(
        self,
        documents: Sequence[Document],
        *,
        chat_id: str | int | None = None,
        silent: bool = False,
    ) -> None:
        """Send files, grouped into albums of up to 10 documents."""
        for start in range(0, len(documents), MAX_MEDIA_GROUP):
            group = list(documents[start:start + MAX_MEDIA_GROUP])
            if len(group) == 1:
                self.send_document(group[0], chat_id=chat_id, silent=silent)
                continue
            media = []
            files: dict[str, tuple[str, bytes]] = {}
            for index, doc in enumerate(group):
                name = f"file{index}"
                item: dict[str, Any] = {"type": "document", "media": f"attach://{name}"}
                if doc.caption:
                    item["caption"] = doc.caption[:MAX_CAPTION_CHARS]
                    item["parse_mode"] = "HTML"
                media.append(item)
                files[name] = (doc.filename, doc.data)
            params = {"chat_id": self._target(chat_id), "media": media, "disable_notification": silent}
            try:
                self.call("sendMediaGroup", params, files=files, timeout=max(self.timeout, 180))
            except TelegramError as exc:
                if exc.error_code is None or exc.error_code >= 500 or exc.error_code == 429:
                    raise
                log.warning("sendMediaGroup failed (%s); sending files one by one", exc)
                for doc in group:
                    self.send_document(doc, chat_id=chat_id, silent=silent)

    def get_updates(self, offset: int | None = None, *, timeout: int = 25) -> list[dict[str, Any]]:
        params = {"offset": offset, "timeout": timeout, "allowed_updates": ["message"]}
        result = self.call("getUpdates", params, timeout=timeout + 15, retries=0)
        return result or []

    def set_my_commands(self, commands: Iterable[tuple[str, str]]) -> None:
        payload = [{"command": c, "description": d} for c, d in commands]
        self.call("setMyCommands", {"commands": payload}, retries=1)
