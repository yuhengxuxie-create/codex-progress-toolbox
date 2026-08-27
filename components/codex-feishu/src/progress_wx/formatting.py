"""跨消息渠道的纯文本通知格式与字符限制。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unicodedata

from .models import (
    PROGRESS_DETAILS_MAX_CHARS,
    ProgressReport,
    STANDARD_STATUSES,
    TurnEvent,
)


BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
STANDARD_DETAILS_MAX_CHARS = PROGRESS_DETAILS_MAX_CHARS
REPLY_RECEIPT_MAX_CHARS = 200


def _one_line(value: object) -> str:
    text = str(value or "")
    cleaned = "".join(" " if unicodedata.category(char).startswith("C") else char for char in text)
    return " ".join(cleaned.split())


def _details_text(value: object) -> str:
    """清理详情但保留列表换行，避免项目符号被挤成一个长段落。"""

    lines: list[str] = []
    for raw_line in str(value or "").splitlines():
        cleaned = "".join(
            " " if unicodedata.category(char).startswith("C") else char
            for char in raw_line
        )
        line = " ".join(cleaned.split())
        if not line:
            continue
        if line.startswith(("* ", "• ", "· ")):
            line = "- " + line[2:].lstrip()
        lines.append(line)
    return "\n".join(lines)


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def format_notification(
    event: TurnEvent,
    report: ProgressReport,
    code: str | None = None,
    *,
    now: datetime | None = None,
    include_reply_code: bool = False,
) -> str:
    """生成便于手机扫读的通知；详情正文直接展示，不再套冗余标题。"""

    sent_at = now or datetime.now(BEIJING_TZ)
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=BEIJING_TZ)
    else:
        sent_at = sent_at.astimezone(BEIJING_TZ)
    title = _one_line(event.display_title)
    status = str(report.status)
    details = _details_text(report.details) or "暂无可展示的进度信息。"
    if status in STANDARD_STATUSES:
        details = _truncate(details, STANDARD_DETAILS_MAX_CHARS)
    lines = [
        f"对话名称：{title}",
        f"当前进度：{status}",
        details,
        f"本条消息时间：{sent_at:%Y-%m-%d %H:%M:%S}（北京时间）",
    ]
    if include_reply_code and code:
        lines.append(f"回复编号：{_one_line(code)}")
    # 字段之间留一空行，便于手机端快速扫读。
    return "\n\n".join(lines)


def format_reply_receipt(received: bool, details: object) -> str:
    """生成飞书回复回执；详情始终压成一行并限制在 200 字内。"""

    status = "已收到" if received else "未收到"
    fallback = (
        "已收到你的回复，正在转交原 Codex 会话继续处理。"
        if received
        else "这条回复未能安全转交 Codex，请引用最新通知后重试。"
    )
    detail_text = _truncate(_one_line(details) or fallback, REPLY_RECEIPT_MAX_CHARS)
    return f"消息状态：{status}\n\n回复信息：{detail_text}"
