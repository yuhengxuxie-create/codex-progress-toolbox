"""按天轮换七天的日志，并在落盘前脱敏。"""

from __future__ import annotations

import logging
import re
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


_SECRET_PATTERN = re.compile(
    r"(?i)(api[_-]?key|token|secret|authorization|password)(\s*[:=]\s*)([^\s,;]+)"
)
_CODE_PATTERN = re.compile(r"\bPCWX-[A-Z2-7]+-[A-F0-9]{12}\b")


class RedactingFilter(logging.Filter):
    """避免把密钥、令牌和完整一次性编号写入日志。"""

    def filter(self, record: logging.LogRecord) -> bool:
        rendered = record.getMessage()
        rendered = _SECRET_PATTERN.sub(r"\1\2<redacted>", rendered)
        rendered = _CODE_PATTERN.sub("PCWX-<redacted>", rendered)
        record.msg, record.args = rendered, ()
        return True


def configure_logging(log_dir: Path, retention_days: int = 7, *, verbose: bool = False) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("progress_wx")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    file_handler = TimedRotatingFileHandler(
        log_dir / "progress-wx.log",
        when="midnight",
        interval=1,
        # 当前文件也占一天，因此只保留 retention_days - 1 个历史文件。
        backupCount=max(0, retention_days - 1),
        encoding="utf-8",
        utc=False,
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(RedactingFilter())
    logger.addHandler(file_handler)
    if verbose:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        stream.addFilter(RedactingFilter())
        logger.addHandler(stream)
    logger.propagate = False
    return logger
