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


class NotificationReason(StrEnum):
    """用户允许的通知分类。

    ``conversation_complete`` is retained as a read-only compatibility value
    for old rows and test doubles.  New model output must use ``task_complete``
    (the six values in :data:`POLICY_NOTIFICATION_REASONS`).
    """

    SILENT = "silent"
    ANSWER_READY = "answer_ready"
    REVIEW_READY = "review_ready"
    IMPORTANT_UPDATE = "important_update"
    USER_ACTION_REQUIRED = "user_action_required"
    TASK_COMPLETE = "task_complete"
    # v1.5/v1.6 compatibility.  Do not include this value in the model schema.
    CONVERSATION_COMPLETE = "conversation_complete"


POLICY_NOTIFICATION_REASONS = frozenset(
    {
        NotificationReason.SILENT.value,
        NotificationReason.ANSWER_READY.value,
        NotificationReason.REVIEW_READY.value,
        NotificationReason.IMPORTANT_UPDATE.value,
        NotificationReason.USER_ACTION_REQUIRED.value,
        NotificationReason.TASK_COMPLETE.value,
    }
)
LEGACY_NOTIFICATION_REASONS = frozenset(
    {NotificationReason.CONVERSATION_COMPLETE.value}
)
ALLOWED_NOTIFICATION_REASONS = POLICY_NOTIFICATION_REASONS | LEGACY_NOTIFICATION_REASONS
ALLOWED_STATUSES = frozenset(item.value for item in ProgressStatus)
STANDARD_STATUSES = frozenset(
    item.value for item in ProgressStatus if item is not ProgressStatus.UNKNOWN
)
CUSTOM_STATUS_MAX_CHARS = 20
PROGRESS_DETAILS_MAX_CHARS = 600
NOTIFICATION_CONTEXT_MAX_CHARS = 1_200
NOTIFICATION_TASK_STATE_MAX_CHARS = 320
NOTIFICATION_REASON_MAX_CHARS = 320
NOTIFICATION_REQUEST_MAX_CHARS = 1_200
NOTIFICATION_FACT_MAX_CHARS = 320
NOTIFICATION_MAX_FACTS = 5
NOTIFICATION_RECENT_MAX = 5
TERMINAL_TURN_STATUSES = frozenset({"completed", "interrupted", "failed"})


def _bounded_text(value: object, limit: int) -> str:
    """Normalize a context fragment without interpreting its contents."""

    text = str(value or "").replace("\x00", "").strip()
    text = "\n".join(" ".join(line.split()) for line in text.splitlines())
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


@dataclass(frozen=True, slots=True)
class NotificationContext:
    """Bounded context supplied to the notification policy model.

    The context is deliberately separate from :class:`TurnEvent`: it can be
    reconstructed from the exact Codex rollout and the local notification
    store after a process restart.  ``recent_successful_notifications`` only
    contains compact, already-sent summaries; it never contains credentials or
    platform message payloads.
    """

    user_request: str = ""
    task_state: str = ""
    recent_successful_notifications: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        request = _bounded_text(self.user_request, NOTIFICATION_REQUEST_MAX_CHARS)
        task_state = _bounded_text(self.task_state, NOTIFICATION_TASK_STATE_MAX_CHARS)
        try:
            recent = tuple(
                dict.fromkeys(
                    _bounded_text(item, NOTIFICATION_CONTEXT_MAX_CHARS)
                    for item in self.recent_successful_notifications
                )
            )
        except TypeError as exc:
            raise ValueError("recent_successful_notifications 必须是字符串序列") from exc
        recent = tuple(item for item in recent if item)[:NOTIFICATION_RECENT_MAX]
        object.__setattr__(self, "user_request", request)
        object.__setattr__(self, "task_state", task_state)
        object.__setattr__(self, "recent_successful_notifications", recent)

    @property
    def current_task_state(self) -> str:
        """兼容调用方使用更完整的字段名。"""

        return self.task_state

    @property
    def recent_notifications(self) -> tuple[str, ...]:
        """Short alias used by older integrations."""

        return self.recent_successful_notifications

    def to_dict(self) -> dict[str, object]:
        return {
            "user_request": self.user_request,
            "task_state": self.task_state,
            "recent_successful_notifications": list(
                self.recent_successful_notifications
            ),
        }


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
    delivered_files: tuple[Any, ...] = ()
    source: str = "codex-store"
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)
    final_answer_parts: tuple[str, ...] = ()

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
    """通知判定、状态、详情和可审计的分类证据。

    The first three positional fields remain compatible with the previous
    summarizer.  New callers should fill the evidence fields so that a policy
    decision can be persisted without copying the full assistant response.
    """

    status: ProgressStatus | str
    details: str
    notification_reason: NotificationReason | str = NotificationReason.SILENT
    decision_reason: str = ""
    matched_request: str = ""
    new_facts: tuple[str, ...] = ()
    model_error: str = ""

    def __post_init__(self) -> None:
        value = self.status.value if isinstance(self.status, ProgressStatus) else str(self.status)
        # 自拟状态只允许简短单行文本；异常或过长值保守退回 */*。
        value = " ".join(value.split()) or ProgressStatus.UNKNOWN.value
        if value not in ALLOWED_STATUSES and len(value) > CUSTOM_STATUS_MAX_CHARS:
            value = ProgressStatus.UNKNOWN.value
        reason = (
            self.notification_reason.value
            if isinstance(self.notification_reason, NotificationReason)
            else str(self.notification_reason or "").strip()
        )
        if reason not in ALLOWED_NOTIFICATION_REASONS:
            reason = NotificationReason.SILENT.value
        object.__setattr__(self, "status", value)
        object.__setattr__(self, "details", str(self.details or "").strip())
        object.__setattr__(self, "notification_reason", reason)
        object.__setattr__(
            self,
            "decision_reason",
            _bounded_text(self.decision_reason, NOTIFICATION_REASON_MAX_CHARS),
        )
        object.__setattr__(
            self,
            "matched_request",
            _bounded_text(self.matched_request, NOTIFICATION_REQUEST_MAX_CHARS),
        )
        try:
            facts = tuple(
                dict.fromkeys(
                    _bounded_text(item, NOTIFICATION_FACT_MAX_CHARS)
                    for item in self.new_facts
                )
            )
        except TypeError as exc:
            raise ValueError("new_facts 必须是字符串序列") from exc
        object.__setattr__(
            self,
            "new_facts",
            tuple(item for item in facts if item)[:NOTIFICATION_MAX_FACTS],
        )
        object.__setattr__(
            self,
            "model_error",
            _bounded_text(self.model_error, NOTIFICATION_REASON_MAX_CHARS),
        )

    @property
    def should_notify(self) -> bool:
        return self.notification_reason != NotificationReason.SILENT.value

    @property
    def reason(self) -> str:
        """Short alias for the persisted model rationale."""

        return self.decision_reason

    @property
    def request(self) -> str:
        """Short alias for the request the model says it answered."""

        return self.matched_request

    @property
    def is_task_complete(self) -> bool:
        """Recognize both the new and legacy completion category."""

        return self.notification_reason in {
            NotificationReason.TASK_COMPLETE.value,
            NotificationReason.CONVERSATION_COMPLETE.value,
        }

    @property
    def has_model_error(self) -> bool:
        return bool(self.model_error)


def structural_report(event: TurnEvent) -> ProgressReport:
    """只依据结构化 turn status 给出保守分类。

    completed 并不等同于“整个任务完成”，因此在没有 Codex/AI 明确分类时使用
    ``*/*``，避免把“本轮结束”误报成“项目完成”。
    """

    if event.status == "failed":
        return ProgressReport(
            ProgressStatus.BLOCKED,
            event.error_message or "Codex 本轮以 failed 状态结束。",
            NotificationReason.IMPORTANT_UPDATE,
            decision_reason="Codex 结构化状态明确记录了失败，需要保留异常结果。",
            new_facts=("当前轮次失败。",),
        )
    if event.status == "interrupted":
        return ProgressReport(
            ProgressStatus.STALLED,
            event.error_message or "Codex 本轮被中断，尚未形成正常完成结果。",
            NotificationReason.SILENT,
        )
    if event.status == "waitingOnApproval":
        return ProgressReport(
            ProgressStatus.APPROVAL_PENDING,
            event.final_message or "Codex 正在等待审批。",
            NotificationReason.USER_ACTION_REQUIRED,
            decision_reason="结构化状态明确表示需要审批。",
        )
    if event.status == "waitingOnUserInput":
        return ProgressReport(
            ProgressStatus.ROUTE_SELECTION,
            event.final_message or "Codex 正在等待人工输入。",
            NotificationReason.USER_ACTION_REQUIRED,
            decision_reason="结构化状态明确表示需要用户输入。",
        )
    return ProgressReport(
        ProgressStatus.UNKNOWN,
        event.final_message or "Codex 本轮已结束，但未提供可可靠映射的语义状态。",
        NotificationReason.SILENT,
    )
