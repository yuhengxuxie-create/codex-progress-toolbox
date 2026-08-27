"""与传输实现无关的领域模型。

本模块只接收 Codex 的结构化字段；任何正文内容都不会用于状态关键词匹配。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping


class ProgressStatus(StrEnum):
    """通知协议允许的进度状态。"""

    STALLED = "停滞"
    BLOCKED = "阻塞"
    ROUTE_SELECTION = "路线选择"
    COMPLETED = "完成"
    MANUAL_TEST = "待人工测试"
    APPROVAL_PENDING = "待审批"
    UNKNOWN = "*/*"


ALLOWED_STATUSES = frozenset(item.value for item in ProgressStatus)
STANDARD_STATUSES = frozenset(
    item.value for item in ProgressStatus if item is not ProgressStatus.UNKNOWN
)
CUSTOM_STATUS_MAX_CHARS = 20
PROGRESS_DETAILS_MAX_CHARS = 600
TERMINAL_TURN_STATUSES = frozenset({"completed", "interrupted", "failed"})


@dataclass(frozen=True, slots=True)
class GeneratedImageArtifact:
    """Codex 结构化历史中可安全回传的原始生成图片。"""

    item_id: str
    path: str
    mime_type: str
    sha256: str
    size: int
    file_name: str

    def __post_init__(self) -> None:
        item_id = str(self.item_id or "").strip()
        path = str(self.path or "").strip()
        mime_type = str(self.mime_type or "").strip().lower()
        digest = str(self.sha256 or "").strip().lower()
        file_name = str(self.file_name or "").strip()
        size = int(self.size)
        if not item_id or not path or not file_name:
            raise ValueError("生成图片的 item_id、path 和 file_name 不能为空")
        if mime_type not in {"image/png", "image/jpeg", "image/webp"}:
            raise ValueError("生成图片 MIME 类型不受支持")
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("生成图片 SHA-256 无效")
        if size <= 0:
            raise ValueError("生成图片大小必须为正数")
        object.__setattr__(self, "item_id", item_id)
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "mime_type", mime_type)
        object.__setattr__(self, "sha256", digest)
        object.__setattr__(self, "size", size)
        object.__setattr__(self, "file_name", file_name)


@dataclass(frozen=True, slots=True)
class TurnEvent:
    """一轮 Codex 对话的结构化状态快照。"""

    thread_id: str
    turn_id: str
    status: str
    title: str = ""
    cwd: str = ""
    final_message: str = ""
    error_message: str = ""
    completed_at: int | None = None
    generated_images: tuple[GeneratedImageArtifact, ...] = ()
    source: str = "codex-store"
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        thread_id = str(self.thread_id or "").strip()
        turn_id = str(self.turn_id or "").strip()
        status = str(self.status or "").strip()
        if not thread_id or not turn_id or not status:
            raise ValueError("thread_id、turn_id 和 status 均不能为空")
        object.__setattr__(self, "thread_id", thread_id)
        object.__setattr__(self, "turn_id", turn_id)
        object.__setattr__(self, "status", status)

    @property
    def display_title(self) -> str:
        return self.title.strip() or self.thread_id

    @property
    def dedupe_key(self) -> str:
        return f"{self.thread_id}:{self.turn_id}:{self.status}"


@dataclass(frozen=True, slots=True)
class ProgressReport:
    """状态与详情；通知层会保留足够的远程阅读信息并限制总长度。"""

    status: ProgressStatus | str
    details: str

    def __post_init__(self) -> None:
        value = self.status.value if isinstance(self.status, ProgressStatus) else str(self.status)
        # 自拟状态只允许简短单行文本；异常或过长值保守退回 */*。
        value = " ".join(value.split()) or ProgressStatus.UNKNOWN.value
        if value not in ALLOWED_STATUSES and len(value) > CUSTOM_STATUS_MAX_CHARS:
            value = ProgressStatus.UNKNOWN.value
        object.__setattr__(self, "status", value)
        object.__setattr__(self, "details", str(self.details or "").strip())


def structural_report(event: TurnEvent) -> ProgressReport:
    """只依据结构化 turn status 给出保守分类。

    completed 并不等同于“整个任务完成”，因此在没有 Codex/AI 明确分类时使用
    ``*/*``，避免把“本轮结束”误报成“项目完成”。
    """

    if event.status == "failed":
        return ProgressReport(
            ProgressStatus.BLOCKED,
            event.error_message or "Codex 本轮以 failed 状态结束。",
        )
    if event.status == "interrupted":
        return ProgressReport(
            ProgressStatus.STALLED,
            event.error_message or "Codex 本轮被中断，尚未形成正常完成结果。",
        )
    if event.status == "waitingOnApproval":
        return ProgressReport(ProgressStatus.APPROVAL_PENDING, event.final_message or "Codex 正在等待审批。")
    if event.status == "waitingOnUserInput":
        return ProgressReport(ProgressStatus.ROUTE_SELECTION, event.final_message or "Codex 正在等待人工输入。")
    return ProgressReport(
        ProgressStatus.UNKNOWN,
        event.final_message or "Codex 本轮已结束，但未提供可可靠映射的语义状态。",
    )
