from __future__ import annotations

import json
import logging

import pytest
import requests

from tpms_agent.logger import RedactingFilter
from tpms_agent.telegram import (
    Document,
    TelegramClient,
    TelegramError,
    escape,
    split_message,
    strip_html,
)

TOKEN = "123456:SECRET-token-value"


class FakeResponse:
    def __init__(self, status: int, payload: dict | None) -> None:
        self.status_code = status
        self.ok = 200 <= status < 300
        self._payload = payload
        self.text = json.dumps(payload) if payload is not None else "<html>bad gateway</html>"

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, data=None, files=None, timeout=None):
        self.calls.append({"url": url, "data": data, "files": files, "timeout": timeout})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def make_client(responses, **kwargs):
    sleeps = []
    client = TelegramClient(TOKEN, 42, sleep=sleeps.append, **kwargs)
    session = FakeSession(responses)
    client._local.session = session
    return client, session, sleeps


def ok(result=True):
    return FakeResponse(200, {"ok": True, "result": result})


def test_send_message_parameters():
    client, session, _ = make_client([ok({"message_id": 1})])
    client.send_message("<b>hi</b>", silent=True)
    call = session.calls[0]
    assert call["url"] == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert call["data"]["chat_id"] == "42"
    assert call["data"]["parse_mode"] == "HTML"
    assert call["data"]["disable_notification"] == "true"
    assert json.loads(call["data"]["link_preview_options"]) == {"is_disabled": True}


def test_retries_on_flood_control_and_server_errors():
    client, session, sleeps = make_client([
        FakeResponse(429, {"ok": False, "error_code": 429, "description": "Too Many Requests", "parameters": {"retry_after": 3}}),
        FakeResponse(502, None),
        ok(),
    ])
    client.send_message("x")
    assert len(session.calls) == 3
    assert sleeps == [3.0, 2]


def test_network_errors_are_retried_then_raised_without_token():
    error = requests.ConnectionError(f"Max retries exceeded with url: /bot{TOKEN}/sendMessage")
    client, session, sleeps = make_client([error] * 4)
    with pytest.raises(TelegramError) as info:
        client.send_message("x")
    assert TOKEN not in str(info.value)
    assert "<redacted>" in str(info.value)
    assert len(session.calls) == 4 and sleeps == [1, 2, 4]


def test_client_errors_are_not_retried():
    client, session, _ = make_client([FakeResponse(400, {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"})])
    with pytest.raises(TelegramError) as info:
        client.send_message("x")
    assert info.value.error_code == 400 and "chat not found" in str(info.value)
    assert len(session.calls) == 1


def test_broken_markup_falls_back_to_plain_text():
    client, session, _ = make_client([
        FakeResponse(400, {"ok": False, "error_code": 400, "description": "Bad Request: can't parse entities"}),
        ok(),
    ])
    client.send_message("<b>bold &amp; <pre>broken")
    retry = session.calls[1]["data"]
    assert retry["text"] == "bold & broken"
    assert "parse_mode" not in retry


def test_long_messages_are_split():
    client, session, _ = make_client([ok(), ok()])
    client.send_message("\n\n".join(["x" * 3000, "y" * 3000]))
    assert [c["data"]["text"][0] for c in session.calls] == ["x", "y"]


def test_split_message_limits():
    text = "\n\n".join(f"para {i} " + "z" * 50 for i in range(200))
    chunks = split_message(text, limit=500)
    assert all(len(c) <= 500 for c in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")
    assert split_message("a" * 1200, limit=500) == ["a" * 500, "a" * 500, "a" * 200]


def test_send_single_document():
    client, session, _ = make_client([ok()])
    client.send_documents([Document("changes.patch", b"diff", "<code>x</code>")])
    call = session.calls[0]
    assert call["url"].endswith("/sendDocument")
    assert call["files"] == {"document": ("changes.patch", b"diff")}
    assert call["data"]["caption"] == "<code>x</code>"


def test_send_documents_as_media_groups():
    client, session, _ = make_client([ok(), ok()])
    docs = [Document(f"f{i}.c", b"x") for i in range(11)]  # album of 10 + a single document
    client.send_documents(docs)
    first, second = session.calls
    assert first["url"].endswith("/sendMediaGroup") and len(first["files"]) == 10
    media = json.loads(first["data"]["media"])
    assert media[0] == {"type": "document", "media": "attach://file0"}
    assert second["url"].endswith("/sendDocument")


def test_media_group_failure_falls_back_to_single_documents():
    client, session, _ = make_client([
        FakeResponse(400, {"ok": False, "error_code": 400, "description": "Bad Request: group send failed"}),
        ok(),
        ok(),
    ])
    client.send_documents([Document("a", b"1"), Document("b", b"2")])
    assert [c["url"].rsplit("/", 1)[1] for c in session.calls] == ["sendMediaGroup", "sendDocument", "sendDocument"]


def test_get_updates_is_not_retried():
    client, session, _ = make_client([ok([{"update_id": 5}])])
    assert client.get_updates(3, timeout=20) == [{"update_id": 5}]
    data = session.calls[0]["data"]
    assert data["offset"] == "3" and data["timeout"] == "20"
    assert json.loads(data["allowed_updates"]) == ["message"]
    assert session.calls[0]["timeout"] == 35


def test_escape_and_strip_html():
    assert escape("a<b> & c") == "a&lt;b&gt; &amp; c"
    assert strip_html("<b>a&lt;b</b>") == "a<b"


def test_log_redaction(caplog):
    logger = logging.getLogger("redaction-test")
    logger.addFilter(RedactingFilter([TOKEN]))
    with caplog.at_level(logging.INFO, logger="redaction-test"):
        logger.info("calling %s", f"https://api.telegram.org/bot{TOKEN}/getMe")
        try:
            raise RuntimeError(f"boom {TOKEN}")
        except RuntimeError:
            logger.exception("failed")
    assert TOKEN not in caplog.text
    assert "<redacted>" in caplog.text
