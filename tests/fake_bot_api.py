"""A tiny in-process imitation of the Telegram Bot API for end-to-end tests."""

from __future__ import annotations

import email
import email.policy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs


class FakeBotApi:
    def __init__(self, token: str) -> None:
        self.token = token
        self.calls: list[dict[str, Any]] = []  # {"method", "fields", "files"}
        self.updates: list[dict[str, Any]] = []
        self._next_update_id = 100
        self._cond = threading.Condition()
        self._message_id = 0
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # keep test output quiet
                pass

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                prefix = f"/bot{api.token}/"
                if not self.path.startswith(prefix):
                    self._reply(401, {"ok": False, "error_code": 401, "description": "Unauthorized"})
                    return
                method = self.path[len(prefix):]
                fields, files = api.parse_body(self.headers.get("Content-Type", ""), body)
                status, payload = api.handle(method, fields, files)
                self._reply(status, payload)

            def _reply(self, status: int, payload: dict[str, Any]) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> "FakeBotApi":
        self._thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    @staticmethod
    def parse_body(content_type: str, body: bytes) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
        fields: dict[str, str] = {}
        files: dict[str, tuple[str, bytes]] = {}
        if content_type.startswith("multipart/form-data"):
            message = email.message_from_bytes(
                f"Content-Type: {content_type}\r\n\r\n".encode() + body, policy=email.policy.HTTP
            )
            for part in message.iter_parts():
                name = part.get_param("name", header="content-disposition")
                payload = part.get_payload(decode=True) or b""
                filename = part.get_filename()
                if filename is not None:
                    files[name] = (filename, payload)
                else:
                    fields[name] = payload.decode("utf-8")
        else:
            for key, values in parse_qs(body.decode("utf-8"), keep_blank_values=True).items():
                fields[key] = values[-1]
        return fields, files

    def handle(self, method: str, fields: dict[str, str], files: dict[str, tuple[str, bytes]]):
        if method == "getUpdates":
            return 200, {"ok": True, "result": self._get_updates(fields)}
        with self._cond:
            self.calls.append({"method": method, "fields": fields, "files": files})
            self._message_id += 1
            self._cond.notify_all()
        if method == "getMe":
            return 200, {"ok": True, "result": {"id": 1, "is_bot": True, "username": "test_pc_bot"}}
        if method in ("sendMessage", "sendDocument"):
            return 200, {"ok": True, "result": {"message_id": self._message_id}}
        if method == "sendMediaGroup":
            return 200, {"ok": True, "result": [{"message_id": self._message_id}]}
        return 200, {"ok": True, "result": True}

    def _get_updates(self, fields: dict[str, str]) -> list[dict[str, Any]]:
        offset = int(fields.get("offset") or 0)
        timeout = min(float(fields.get("timeout") or 0), 1.0)
        with self._cond:
            if offset < 0:
                return self.updates[offset:]
            self.updates = [u for u in self.updates if u["update_id"] >= offset]
            if not self.updates and timeout:
                self._cond.wait(timeout)
            return [u for u in self.updates if u["update_id"] >= offset]

    def push_message(self, text: str, chat_id: int) -> None:
        with self._cond:
            self.updates.append({
                "update_id": self._next_update_id,
                "message": {
                    "message_id": self._next_update_id,
                    "chat": {"id": chat_id, "type": "private"},
                    "from": {"id": chat_id, "username": "someone"},
                    "text": text,
                },
            })
            self._next_update_id += 1
            self._cond.notify_all()

    def texts(self, chat_id: str | None = None) -> list[str]:
        with self._cond:
            return [
                c["fields"]["text"]
                for c in self.calls
                if c["method"] == "sendMessage" and (chat_id is None or c["fields"].get("chat_id") == chat_id)
            ]

    def uploaded_files(self) -> list[tuple[str, bytes]]:
        with self._cond:
            return [f for c in self.calls for f in c["files"].values()]

    def wait_for(self, predicate, timeout: float = 15.0) -> bool:
        import time

        deadline = time.monotonic() + timeout
        with self._cond:
            while not predicate(self):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(min(remaining, 0.2))
            return True
