"""Logging helpers that avoid exposing credentials or signed webhook URLs."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterable
from urllib.parse import SplitResult, urlsplit, urlunsplit


LOGGER_NAME = "progress_notify"


def redact_url(url: str) -> str:
    """Keep only a URL's scheme, hostname and port for safe diagnostics."""

    try:
        parts = urlsplit(url)
    except ValueError:
        return "<invalid-url>"
    if not parts.scheme or not parts.hostname:
        return "<invalid-url>"
    host = parts.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = f":{parts.port}" if parts.port is not None else ""
    except ValueError:
        port = ""
    return urlunsplit(SplitResult(parts.scheme, host + port, "", "", ""))


def redact_text(value: object, secrets: Iterable[str] = ()) -> str:
    """Redact explicit secret values and common authorization token shapes."""

    text = str(value)
    for secret in sorted(
        {item for item in secrets if isinstance(item, str) and item},
        key=len,
        reverse=True,
    ):
        text = text.replace(secret, "<redacted>")

    # Avoid logging an accidentally formatted Authorization header.  This is
    # deliberately a small scanner rather than a progress-classification regex.
    lower = text.casefold()
    for marker in ("authorization: bearer ", "bearer "):
        cursor = 0
        while True:
            start = lower.find(marker, cursor)
            if start < 0:
                break
            value_start = start + len(marker)
            value_end = value_start
            while value_end < len(text) and text[value_end] not in " \t\r\n,;)}]":
                value_end += 1
            text = text[:value_start] + "<redacted>" + text[value_end:]
            lower = text.casefold()
            cursor = value_start + len("<redacted>")
    return text


class RedactingFilter(logging.Filter):
    """Render a record once, replace secrets, and discard original arguments."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = tuple(secret for secret in secrets if secret)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            message = "unrenderable log message"
        record.msg = redact_text(message, self._secrets)
        record.args = ()
        return True


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def close_logging(logger: logging.Logger | None = None) -> None:
    """Detach and close every package handler (important on Windows)."""

    selected = logger or get_logger()
    for handler in list(selected.handlers):
        selected.removeHandler(handler)
        try:
            handler.flush()
        except (OSError, ValueError):
            pass
        try:
            handler.close()
        except (OSError, ValueError):
            pass


def configure_logging(
    log_file: str | Path | None = None,
    *,
    level: int = logging.INFO,
    secrets: Iterable[str] = (),
) -> logging.Logger:
    """Configure the package logger without mutating the root logger."""

    logger = get_logger()
    logger.setLevel(level)
    logger.propagate = False
    close_logging(logger)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    redactor = RedactingFilter(secrets)

    if log_file is None:
        handler: logging.Handler = logging.StreamHandler()
    else:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path,
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
    handler.setLevel(level)
    handler.setFormatter(formatter)
    handler.addFilter(redactor)
    logger.addHandler(handler)
    return logger
