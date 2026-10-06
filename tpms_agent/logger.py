"""Local logging for troubleshooting.

Logs go to the console (when one exists) and to a rotating log file.
Secrets such as the Telegram bot token are redacted from every record,
because HTTP error messages from ``requests`` contain the full API URL.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from collections import deque
from pathlib import Path
from typing import Iterable

LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"
REDACTED = "<redacted>"


class RedactingFilter(logging.Filter):
    """Replace secret strings in log messages and exception text."""

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        self.secrets = [s for s in secrets if s and len(s) >= 6]

    def redact(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, REDACTED)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.secrets:
            return True
        message = record.getMessage()
        redacted = self.redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = None
        if record.exc_info and record.exc_info[1] is not None:
            # Render the traceback now so it can be redacted, then drop exc_info
            # so handlers don't render the unredacted version again.
            formatter = logging.Formatter()
            record.exc_text = self.redact(formatter.formatException(record.exc_info))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = self.redact(record.exc_text)
        return True


def setup_logging(
    level: str = "INFO",
    log_file: Path | None = None,
    *,
    secrets: Iterable[str] = (),
    max_bytes: int = 1_000_000,
    backup_count: int = 3,
    console: bool = True,
) -> logging.Logger:
    """Configure the root logger. Safe to call more than once."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.setLevel(level.upper())

    redactor = RedactingFilter(secrets)
    formatter = logging.Formatter(LOG_FORMAT)

    # Under pythonw.exe / a windowed executable there is no stderr.
    if console and sys.stderr is not None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        stream.addFilter(redactor)
        root.addHandler(stream)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(redactor)
        root.addHandler(file_handler)

    # Third-party libraries are chatty at DEBUG level.
    for noisy in ("urllib3", "watchdog"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return root


def tail(path: Path | None, lines: int = 50) -> str:
    """Return the last *lines* lines of a text file ('' if unavailable)."""
    if path is None or lines <= 0:
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return "".join(deque(handle, maxlen=lines))
    except OSError:
        return ""
