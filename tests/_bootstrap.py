"""Test bootstrap helpers; tests depend only on the Python standard library."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def write_config(path: Path, **overrides: Any) -> Path:
    """Write the smallest valid local configuration for a unit test."""

    data: dict[str, Any] = {
        "thread_ids": ["thr_selected"],
        "notification": {
            "provider": "generic",
            "webhook_url": "https://example.invalid/hook",
            "allow_http_localhost": False,
            "timeout_seconds": 2,
            "max_attempts": 1,
        },
        "classifier": {
            "mode": "disabled",
            "api_key": "",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-5-mini",
            "timeout_seconds": 2,
        },
        "codex": {
            "command": "codex",
            "title_overrides": {"thr_selected": "测试对话"},
            "request_timeout_seconds": 2,
        },
        "log_file": ".state/test.log",
    }
    for key, value in overrides.items():
        data[key] = value
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def close_package_logging() -> None:
    """Release file handlers before Windows temporary directories are removed."""

    from progress_notify.logging_utils import get_logger

    logger = get_logger()
    for handler in tuple(logger.handlers):
        handler.close()
        logger.removeHandler(handler)
