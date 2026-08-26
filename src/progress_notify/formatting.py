"""Deterministic, four-line Chinese plain-text notification formatting."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unicodedata

from .models import AgentTurnComplete, ProgressReport, ProgressStatus


BEIJING_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")
STATUS_MAX_CHARS = 12
DETAILS_MAX_CHARS = 50
# Kept as a compatibility alias for callers importing the old public name.
STANDARD_DETAILS_MAX_CHARS = DETAILS_MAX_CHARS
WECOM_MARKDOWN_MAX_BYTES = 4096


def beijing_now() -> datetime:
    return datetime.now(BEIJING_TIMEZONE)


def _beijing_time(value: datetime | None) -> datetime:
    if value is None:
        return beijing_now()
    if value.tzinfo is None:
        # A caller-supplied naive timestamp is treated as Beijing local time.
        return value.replace(tzinfo=BEIJING_TIMEZONE)
    return value.astimezone(BEIJING_TIMEZONE)


def _one_line(value: object) -> str:
    # split() covers CR/LF as well as Unicode line/paragraph separators.  This
    # guarantees the final plain-text message always consists of four lines.
    text = str(value or "")
    without_controls = "".join(
        " " if unicodedata.category(character).startswith("C") else character
        for character in text
    )
    return " ".join(without_controls.split())


def _truncate_characters(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    if limit <= 0:
        return ""
    if limit == 1:
        return "…"
    return value[: limit - 1].rstrip() + "…"


def normalize_report(report: ProgressReport) -> ProgressReport:
    """Normalize a concise status and cap every detail at 50 characters."""

    status = _one_line(report.status).replace("*", "").replace("`", "")
    if not status or status == "/":
        status = ProgressStatus.UNKNOWN.value
    status = _truncate_characters(status, STATUS_MAX_CHARS)
    details = (
        _one_line(report.details).replace("*", "").replace("`", "")
        or "暂无可用的进度摘要。"
    )
    details = _truncate_characters(details, DETAILS_MAX_CHARS)
    return ProgressReport(status=status, details=details)


def _lines(
    event: AgentTurnComplete,
    report: ProgressReport,
    sent_at: datetime,
) -> tuple[str, str, str, str]:
    normalized = normalize_report(report)
    title = _one_line(event.display_title).replace("*", "").replace("`", "")
    timestamp = sent_at.strftime("%Y-%m-%d %H:%M:%S（北京时间）")
    return (
        f"对话名称：{title}",
        f"当前进度：{normalized.status}",
        f"进度详情：{normalized.details}",
        f"本条消息时间：{timestamp}",
    )


def format_notification(
    event: AgentTurnComplete,
    report: ProgressReport,
    now: datetime | None = None,
) -> str:
    """Return exactly four plain-text lines in the required order."""

    return "\n".join(_lines(event, report, _beijing_time(now)))


def format_notification_limited(
    event: AgentTurnComplete,
    report: ProgressReport,
    *,
    max_utf8_bytes: int,
    now: datetime | None = None,
) -> str:
    """Format four lines while enforcing the provider's UTF-8 byte limit."""

    if max_utf8_bytes <= 0:
        raise ValueError("max_utf8_bytes must be positive")

    result = format_notification(event, report, now=now)
    if len(result.encode("utf-8")) > max_utf8_bytes:
        raise ValueError("notification title/timestamp exceed the UTF-8 byte limit")
    return result
