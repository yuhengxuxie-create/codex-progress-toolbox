"""Small, dependency-free domain models used by the notifier.

The Codex ``notify`` command receives one JSON object for each completed agent
turn.  Keeping the external spelling (hyphenated keys) at this boundary makes
the rest of the application independent from that transport detail.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping, Sequence


AGENT_TURN_COMPLETE = "agent-turn-complete"


class ProgressStatus(StrEnum):
    """Statuses whose meaning is part of the public notification contract."""

    STALLED = "停滞"
    BLOCKED = "阻塞"
    ROUTE_SELECTION = "路线选择"
    COMPLETED = "完成"
    MANUAL_TEST = "待人工测试"
    APPROVAL_PENDING = "待审批"
    UNKNOWN = "情况未知"


STANDARD_STATUSES: frozenset[str] = frozenset(
    status.value for status in ProgressStatus if status is not ProgressStatus.UNKNOWN
)
ALL_CLASSIFIER_STATUSES: frozenset[str] = STANDARD_STATUSES | {
    ProgressStatus.UNKNOWN.value
}


class EventValidationError(ValueError):
    """Raised when a payload is not a valid Codex completed-turn event."""


def _first_string(payload: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return ""


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        return ()

    result: list[str] = []
    for item in value:
        if isinstance(item, str):
            result.append(item)
            continue
        if isinstance(item, Mapping):
            # Some event producers preserve message objects.  We retain only
            # text content, never infer progress from it at this layer.
            text = item.get("content")
            if isinstance(text, str):
                result.append(text)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class AgentTurnComplete:
    """Canonical representation of Codex's ``agent-turn-complete`` event."""

    thread_id: str
    thread_title: str = ""
    last_assistant_message: str = ""
    turn_id: str = ""
    input_messages: tuple[str, ...] = ()
    cwd: str = ""
    event_type: str = AGENT_TURN_COMPLETE
    raw_payload: Mapping[str, Any] = field(
        default_factory=dict, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if not isinstance(self.thread_id, str) or not self.thread_id.strip():
            raise EventValidationError("agent-turn-complete payload is missing thread-id")
        if self.event_type != AGENT_TURN_COMPLETE:
            raise EventValidationError(
                f"unsupported Codex notify event type: {self.event_type!r}"
            )

        object.__setattr__(self, "thread_id", self.thread_id.strip())
        object.__setattr__(self, "thread_title", str(self.thread_title or "").strip())
        object.__setattr__(
            self, "last_assistant_message", str(self.last_assistant_message or "")
        )
        object.__setattr__(self, "turn_id", str(self.turn_id or "").strip())
        object.__setattr__(self, "cwd", str(self.cwd or ""))
        object.__setattr__(self, "input_messages", tuple(self.input_messages))
        object.__setattr__(self, "raw_payload", MappingProxyType(dict(self.raw_payload)))

    @property
    def display_title(self) -> str:
        """A non-empty title suitable for a human-facing notification."""

        return self.thread_title or self.thread_id

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "AgentTurnComplete":
        if not isinstance(payload, Mapping):
            raise EventValidationError("Codex notify payload must be a JSON object")

        event_type = _first_string(payload, "type", "event_type", "event-type")
        if event_type != AGENT_TURN_COMPLETE:
            raise EventValidationError(
                f"unsupported Codex notify event type: {event_type or '<missing>'!r}"
            )

        return cls(
            thread_id=_first_string(payload, "thread-id", "thread_id"),
            thread_title=_first_string(
                payload,
                "thread-title",
                "thread_title",
                "conversation-title",
                "conversation_title",
                "title",
            ),
            last_assistant_message=_first_string(
                payload, "last-assistant-message", "last_assistant_message"
            ),
            turn_id=_first_string(payload, "turn-id", "turn_id"),
            input_messages=_string_tuple(
                payload.get("input-messages", payload.get("input_messages"))
            ),
            cwd=_first_string(payload, "cwd"),
            event_type=event_type,
            raw_payload=payload,
        )


def parse_agent_turn_complete(payload: Mapping[str, Any]) -> AgentTurnComplete:
    """Parse a Codex notify payload using the stable public event API."""

    return AgentTurnComplete.from_payload(payload)


@dataclass(frozen=True, slots=True)
class ProgressReport:
    """Semantic progress classification for one completed turn.

    Custom statuses are accepted when none of the six standard states precisely
    describes the current situation.
    """

    status: str
    details: str

    def __post_init__(self) -> None:
        status = str(self.status or "").strip() or ProgressStatus.UNKNOWN.value
        details = str(self.details or "").strip()
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "details", details)

    @property
    def is_standard(self) -> bool:
        return self.status in STANDARD_STATUSES

    @property
    def is_unknown(self) -> bool:
        return self.status == ProgressStatus.UNKNOWN.value

    @classmethod
    def unknown(
        cls,
        details: str = "语义分类不可用，无法准确判断当前进度。",
    ) -> "ProgressReport":
        return cls(ProgressStatus.UNKNOWN.value, details)


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """Metadata returned after a configured transport accepts a notification."""

    provider: str
    status_code: int
    attempts: int
    response_json: Mapping[str, Any] | None = field(
        default=None, repr=False, compare=False
    )

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300
