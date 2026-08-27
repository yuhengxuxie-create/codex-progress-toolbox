from __future__ import annotations

from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from progress_wx.logging_utils import configure_logging


def test_retention_count_includes_current_log(tmp_path: Path) -> None:
    logger = configure_logging(tmp_path, retention_days=7)
    try:
        handler = next(item for item in logger.handlers if isinstance(item, TimedRotatingFileHandler))
        assert handler.backupCount == 6
    finally:
        for handler in logger.handlers:
            handler.close()
        logger.handlers.clear()
