"""本机 Codex 会话的分层、可缓存语义搜索。

同一引擎同时供飞书管理入口和 ``session-search`` CLI 使用。模块只读 Codex
会话数据；搜索正文只进入本机内存、受控临时目录和本机状态库，不写日志。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from collections import deque
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from .codex_projects import CodexProjectRegistry, ProjectRegistryError
from .codex_store import (
    CodexStore,
    CodexStoreReadError,
    ThreadRecord,
    TurnRecord,
    ThreadStatus,
    independent_thread_title,
    public_thread_title,
    thread_title_recovery_hash,
)
from .config import SummaryConfig
from .models import TurnEvent
from .retry import RetryPolicy, call_with_retry
from .state import StateStore
from .summarizer import ProgressSummarizer, SummaryError, fallback_report


SESSION_SEARCH_SCHEMA_VERSION = 1
# 本项目面向的北京时间/香港时间当前均为固定 UTC+08:00。内嵌 Python 不保证
# 自带 IANA tzdata，因此这里使用标准库固定偏移，避免任意旧 CLI 在 import 阶段崩溃。
BEIJING = timezone(timedelta(hours=8), name="Asia/Hong_Kong")
SCOPE_ORDER = ("hint", "recent_30d", "recent_180d", "all")
MAX_SEMANTIC_CANDIDATES = 12
SEMANTIC_BATCH_SIZE = 6
MAX_ROLLOUT_SCAN_BYTES = 128 * 1024 * 1024
MAX_JSONL_LINE_BYTES = 2 * 1024 * 1024
DISPLAY_TITLE_MAX_CHARS = 32
EMPTY_QUERY_TITLE_PROMPT = (
    "用户没有填写检索线索；请仅根据每个候选的真实内容生成描述和简洁展示名。"
)
TITLE_RECOVERY_PROMPT = (
    "这些历史会话的 Codex 独立标题已经确认缺失。请仅根据真实会话内容生成"
    "忠实、简洁、可长期识别的展示名；不要评分用户线索。"
)


class SessionSearchError(RuntimeError):
    """搜索无法产生可信结果。"""


class SessionSearchCancelled(SessionSearchError):
    """调用方通过取消文件停止了尚未完成的搜索。"""


@dataclass(frozen=True, slots=True)
class SearchRequest:
    name: str = ""
    description: str = ""
    last_activity: str = ""
    scope: str = "auto"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SearchRequest":
        allowed = {"schema_version", "name", "description", "last_activity", "scope"}
        extras = set(value) - allowed
        if extras:
            raise ValueError(f"搜索请求包含未知字段：{', '.join(sorted(extras))}")
        version = value.get("schema_version", SESSION_SEARCH_SCHEMA_VERSION)
        if version != SESSION_SEARCH_SCHEMA_VERSION:
            raise ValueError("搜索请求 schema_version 不受支持")

        def clean(key: str, limit: int = 1000) -> str:
            raw = value.get(key, "")
            if raw is None:
                return ""
            if not isinstance(raw, str):
                raise ValueError(f"{key} 必须是字符串")
            text = raw.replace("\x00", "").strip()
            if len(text) > limit:
                raise ValueError(f"{key} 超过 {limit} 字符上限")
            return text

        scope = clean("scope", 32).casefold() or "auto"
        if scope not in {"auto", *SCOPE_ORDER}:
            raise ValueError("scope 仅支持 auto、hint、recent_30d、recent_180d、all")
        return cls(
            name=clean("name"),
            description=clean("description"),
            last_activity=clean("last_activity"),
            scope=scope,
        )

    @property
    def semantic_clues(self) -> tuple[str, ...]:
        return tuple(item for item in (self.name, self.description, self.last_activity) if item)

    @property
    def query_text(self) -> str:
        return "\n".join(self.semantic_clues)

    @property
    def query_hash(self) -> str:
        normalized = "\n".join(" ".join(item.casefold().split()) for item in self.semantic_clues)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def with_scope(self, scope: str) -> "SearchRequest":
        return SearchRequest(self.name, self.description, self.last_activity, scope)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SESSION_SEARCH_SCHEMA_VERSION,
            "name": self.name,
            "description": self.description,
            "last_activity": self.last_activity,
            "scope": self.scope,
        }


@dataclass(frozen=True, slots=True)
class TimeHint:
    start: datetime
    end: datetime
    label: str


@dataclass(slots=True)
class SessionEvidence:
    record: ThreadRecord
    turn: TurnRecord
    project_id: str = ""
    project_name: str = "个人会话"
    user_messages: tuple[str, ...] = ()
    assistant_messages: tuple[str, ...] = ()
    artifact_evidence: tuple[str, ...] = ()
    content_hash: str = ""
    local_score: float = 0.0
    description: str = ""
    last_result: str = ""

    @property
    def activity_seconds(self) -> int | None:
        return _unix_seconds(self.turn.completed_at or self.turn.started_at)

    def cache_payload(self) -> dict[str, Any]:
        return {
            "title": self.record.title,
            "title_source": self.record.title_source,
            "preview": self.record.preview,
            "project_name": self.project_name,
            "user_messages": list(self.user_messages),
            "assistant_messages": list(self.assistant_messages),
            "artifact_evidence": list(self.artifact_evidence),
        }


@dataclass(frozen=True, slots=True)
class SemanticAssessment:
    thread_id: str
    score: float
    confidence: str
    classification: str
    description: str
    display_title: str
    reason: str


@dataclass(frozen=True, slots=True)
class SearchMatch:
    thread_id: str
    title: str
    description: str
    last_result: str
    last_activity_at_beijing: str
    score: float
    confidence: str
    classification: str
    reason: str
    project_id: str
    project_name: str
    archived: bool
    host_id: str
    monitor: Mapping[str, Any]
    title_origin: str = "codex_generated"
    snapshot_turn_id: str = ""
    snapshot_content_hash: str = ""
    raw_final_snapshot: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "title": self.title,
            "title_origin": self.title_origin,
            "description": self.description,
            "last_result": self.last_result,
            "last_activity_at_beijing": self.last_activity_at_beijing,
            "score": round(self.score, 4),
            "confidence": self.confidence,
            "classification": self.classification,
            "reason": self.reason,
            "project_id": self.project_id,
            "project_name": self.project_name,
            "archived": self.archived,
            "host_id": self.host_id,
            "monitor": dict(self.monitor),
        }

    def to_management_dict(self) -> dict[str, Any]:
        """返回飞书管理私有快照；公开 CLI ``to_dict`` 永不暴露原文。"""

        result = self.to_dict()
        result["_query_snapshot"] = {
            "turn_id": self.snapshot_turn_id,
            "content_hash": self.snapshot_content_hash,
            "raw_final": self.raw_final_snapshot,
            "raw_sha256": hashlib.sha256(
                self.raw_final_snapshot.encode("utf-8")
            ).hexdigest(),
        }
        return result


@dataclass(frozen=True, slots=True)
class SearchResult:
    search_id: str
    status: str
    scope: str
    scope_label: str
    can_expand: bool
    next_scope: str | None
    cost_warning: str
    matches: tuple[SearchMatch, ...]
    examined_count: int
    semantic_candidate_count: int
    model_call_count: int
    warnings: tuple[Mapping[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SESSION_SEARCH_SCHEMA_VERSION,
            "search_id": self.search_id,
            "status": self.status,
            "scope": self.scope,
            "scope_label": self.scope_label,
            "can_expand": self.can_expand,
            "next_scope": self.next_scope,
            "cost_warning": self.cost_warning,
            "examined_count": self.examined_count,
            "semantic_candidate_count": self.semantic_candidate_count,
            "model_call_count": self.model_call_count,
            "matches": [item.to_dict() for item in self.matches],
            "warnings": [dict(item) for item in self.warnings],
        }

    def to_management_dict(self) -> dict[str, Any]:
        """返回仅供同进程飞书管理上下文持久化的查询快照。"""

        result = self.to_dict()
        result["matches"] = [item.to_management_dict() for item in self.matches]
        return result


class ProgressSink(Protocol):
    def write(self, phase: str, current: int, total: int, message: str) -> None: ...


class NullProgress:
    def write(self, phase: str, current: int, total: int, message: str) -> None:
        del phase, current, total, message


class AtomicProgressFile:
    """供 WinForms 轮询的原子 JSON 文件；正文线索不会写入进度。"""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path).expanduser().resolve()

    def write(self, phase: str, current: int, total: int, message: str) -> None:
        payload = {
            "schema_version": SESSION_SEARCH_SCHEMA_VERSION,
            "phase": str(phase),
            "current": max(0, int(current)),
            "total": max(0, int(total)),
            "message": str(message or "")[:300],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _unix_seconds(value: int | None) -> int | None:
    if value is None:
        return None
    numeric = int(value)
    return numeric // 1000 if abs(numeric) >= 10_000_000_000 else numeric


def _beijing_text(value: int | None) -> str:
    seconds = _unix_seconds(value)
    if seconds is None:
        return "未知"
    return datetime.fromtimestamp(seconds, tz=BEIJING).strftime("%Y-%m-%d %H:%M")


def parse_time_hint(value: str, *, now: datetime | None = None) -> TimeHint | None:
    """解析常用北京时间表达；无法识别时只作为语义线索，不报错。"""

    text = " ".join(str(value or "").strip().split())
    if not text:
        return None
    current = (now or datetime.now(tz=BEIJING)).astimezone(BEIJING)
    today = current.replace(hour=0, minute=0, second=0, microsecond=0)
    if "这几天" in text:
        return TimeHint(current - timedelta(days=5), current, "最近5天（“这几天”）")
    match = re.search(r"最近\s*([1-9][0-9]?)\s*天", text)
    if match:
        days = int(match.group(1))
        return TimeHint(current - timedelta(days=days), current, f"最近{days}天")
    if "今天" in text:
        return TimeHint(today, current, "今天")
    if "昨天" in text:
        return TimeHint(today - timedelta(days=1), today, "昨天")
    if "前天" in text:
        return TimeHint(today - timedelta(days=2), today - timedelta(days=1), "前天")
    if "本周" in text:
        start = today - timedelta(days=today.weekday())
        return TimeHint(start, current, "本周")
    if "上周" in text:
        end = today - timedelta(days=today.weekday())
        return TimeHint(end - timedelta(days=7), end, "上周")
    ago = re.search(r"([1-9][0-9]?)\s*天前", text)
    if ago:
        target = today - timedelta(days=int(ago.group(1)))
        return TimeHint(target - timedelta(days=1), target + timedelta(days=2), f"约{ago.group(1)}天前")
    exact = re.search(
        r"(?P<year>20[0-9]{2})\s*(?:年|[-/.])\s*(?P<month>1[0-2]|0?[1-9])\s*(?:月|[-/.])\s*(?P<day>3[01]|[12][0-9]|0?[1-9])(?:\s*日)?(?:\s+(?P<hour>[01]?[0-9]|2[0-3])[:：](?P<minute>[0-5][0-9]))?",
        text,
    )
    if exact:
        try:
            target = datetime(
                int(exact.group("year")), int(exact.group("month")), int(exact.group("day")),
                int(exact.group("hour") or 0), int(exact.group("minute") or 0), tzinfo=BEIJING,
            )
        except ValueError:
            return None
        if exact.group("hour"):
            return TimeHint(target - timedelta(days=1), target + timedelta(days=1), "填写时间前后1天")
        return TimeHint(target - timedelta(days=1), target + timedelta(days=2), "填写日期前后1天")
    short = re.search(
        r"(?<![0-9])(?P<month>1[0-2]|0?[1-9])\s*(?:月|[-/.])\s*(?P<day>3[01]|[12][0-9]|0?[1-9])(?:\s*日)?",
        text,
    )
    if short:
        try:
            target = datetime(current.year, int(short.group("month")), int(short.group("day")), tzinfo=BEIJING)
        except ValueError:
            return None
        if target > current + timedelta(days=7):
            target = target.replace(year=target.year - 1)
        return TimeHint(target - timedelta(days=1), target + timedelta(days=2), "填写日期前后1天")
    return None


def _scope_for(request: SearchRequest, hint: TimeHint | None) -> str:
    if request.scope != "auto":
        if request.scope == "hint" and hint is None:
            return "recent_30d"
        return request.scope
    return "hint" if hint is not None else "recent_30d"


def _scope_label(scope: str, hint: TimeHint | None) -> str:
    return {
        "hint": hint.label if hint is not None else "最近30天",
        "recent_30d": "最近30天",
        "recent_180d": "最近180天",
        "all": "全部用户会话（含归档）",
    }[scope]


def _next_scope(scope: str) -> str | None:
    index = SCOPE_ORDER.index(scope)
    return SCOPE_ORDER[index + 1] if index + 1 < len(SCOPE_ORDER) else None


def _in_scope(activity: int | None, scope: str, hint: TimeHint | None, now: datetime) -> bool:
    if activity is None:
        return scope == "all"
    value = datetime.fromtimestamp(activity, tz=BEIJING)
    if scope == "all":
        return True
    if scope == "hint" and hint is not None:
        return hint.start <= value <= hint.end
    days = 30 if scope == "recent_30d" else 180
    return now - timedelta(days=days) <= value <= now + timedelta(days=1)


def _clean_fragment(value: object, limit: int) -> str:
    text = " ".join(str(value or "").replace("\x00", "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, Mapping):
            continue
        for key in ("text", "input_text", "output_text"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value)
                break
    return "\n".join(parts)


def _append_sample(first: list[str], recent: deque[str], value: str) -> None:
    text = _clean_fragment(value, 1600)
    if not text:
        return
    if len(first) < 8:
        first.append(text)
    recent.append(text)


def _comparison_path(path: Path) -> str:
    """归一化 Windows extended-length 前缀后再做目录边界比较。"""

    value = os.path.normcase(os.path.abspath(os.fspath(path)))
    if os.name == "nt":
        lowered = value.casefold()
        if lowered.startswith("\\\\?\\unc\\"):
            value = "\\\\" + value[8:]
        elif lowered.startswith("\\\\?\\"):
            value = value[4:]
    return os.path.normcase(os.path.normpath(value))


def _extract_rollout_evidence(
    record: ThreadRecord, codex_home: Path
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], str | None]:
    raw_path = str(record.rollout_path or "").strip()
    if not raw_path:
        return (), (), (), "rollout_missing"
    root = (codex_home / "sessions").resolve(strict=False)
    path = Path(raw_path).expanduser().resolve(strict=False)
    try:
        root_key = _comparison_path(root)
        path_key = _comparison_path(path)
        if os.path.commonpath((root_key, path_key)) != root_key:
            return (), (), (), "rollout_outside_sessions"
    except (OSError, RuntimeError, ValueError):
        return (), (), (), "rollout_outside_sessions"
    if not path.is_file():
        return (), (), (), "rollout_missing"
    user_first: list[str] = []
    assistant_first: list[str] = []
    artifact_first: list[str] = []
    user_recent: deque[str] = deque(maxlen=32)
    assistant_recent: deque[str] = deque(maxlen=24)
    artifact_recent: deque[str] = deque(maxlen=16)
    read_bytes = 0
    warning: str | None = None
    try:
        with path.open("rb") as handle:
            for raw in handle:
                read_bytes += len(raw)
                if read_bytes > MAX_ROLLOUT_SCAN_BYTES:
                    warning = "rollout_scan_limit"
                    break
                if len(raw) > MAX_JSONL_LINE_BYTES:
                    warning = warning or "rollout_line_limit"
                    continue
                try:
                    item = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    warning = warning or "rollout_invalid_json"
                    continue
                if not isinstance(item, Mapping):
                    continue
                envelope = str(item.get("type") or "")
                payload = item.get("payload")
                if not isinstance(payload, Mapping):
                    continue
                payload_type = str(payload.get("type") or "")
                if envelope == "event_msg" and payload_type == "user_message":
                    _append_sample(user_first, user_recent, str(payload.get("message") or ""))
                    continue
                if envelope == "event_msg" and payload_type == "agent_message":
                    _append_sample(assistant_first, assistant_recent, str(payload.get("message") or ""))
                    continue
                if envelope == "response_item" and payload_type == "message":
                    role = str(payload.get("role") or "")
                    text = _content_text(payload.get("content"))
                    if role == "user":
                        _append_sample(user_first, user_recent, text)
                    elif role == "assistant":
                        _append_sample(assistant_first, assistant_recent, text)
                    continue
                if envelope == "response_item" and payload_type in {
                    "function_call", "function_call_output", "custom_tool_call", "custom_tool_call_output",
                }:
                    tool_name = str(payload.get("name") or payload.get("call_id") or payload_type)
                    output = payload.get("output") or payload.get("arguments") or ""
                    _append_sample(
                        artifact_first,
                        artifact_recent,
                        f"{tool_name}: {_clean_fragment(output, 700)}",
                    )
    except OSError:
        return (), (), (), "rollout_unreadable"

    def merge(first: Sequence[str], recent: Iterable[str]) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*first, *recent)))

    return (
        merge(user_first, user_recent),
        merge(assistant_first, assistant_recent),
        merge(artifact_first, artifact_recent),
        warning,
    )


def _tokens(value: str) -> set[str]:
    text = " ".join(str(value or "").casefold().split())
    result = set(re.findall(r"[a-z0-9_+-]{2,}", text))
    for sequence in re.findall(r"[\u3400-\u9fff]+", text):
        if len(sequence) <= 4:
            result.add(sequence)
        for size in (2, 3, 4):
            result.update(sequence[index : index + size] for index in range(max(0, len(sequence) - size + 1)))
    return result


def _local_score(query: str, evidence: SessionEvidence, hint: TimeHint | None) -> float:
    if not query.strip():
        return 0.0
    query_tokens = _tokens(query)
    if not query_tokens:
        return 0.0
    title = f"{evidence.record.title} {evidence.record.preview} {evidence.project_name}"
    body = "\n".join((*evidence.user_messages, *evidence.assistant_messages, *evidence.artifact_evidence))
    title_overlap = len(query_tokens & _tokens(title)) / len(query_tokens)
    body_overlap = len(query_tokens & _tokens(body)) / len(query_tokens)
    normalized_query = "".join(query.casefold().split())
    normalized_title = "".join(title.casefold().split())
    stage_adjustment = 0.0
    selection_query = any(token in normalized_query for token in ("选题", "候选主题", "主题清单"))
    selection_evidence = any(
        token in "".join(f"{title}\n{body}".casefold().split())
        for token in ("选题", "候选主题", "主题清单")
    )
    negative_downstream = bool(
        re.search(r"(?:没有|没|未|不曾|尚未|停止|到此).{0,12}(?:继续|生成|写|制作|正文|文档|稿件)", normalized_query)
        or re.search(r"(?:只|仅).{0,12}(?:选题|候选|检索)", normalized_query)
    )
    if selection_query and selection_evidence:
        stage_adjustment += 0.12
        if negative_downstream:
            result_evidence = "".join(
                "\n".join((*evidence.assistant_messages, *evidence.artifact_evidence)).casefold().split()
            )
            downstream_completed = bool(
                re.search(
                    r"(?:生成|完成|撰写|写完|制作|保存|交付).{0,18}(?:正文|文章|文档|word|docx|稿件)",
                    result_evidence,
                )
                or re.search(
                    r"(?:正文|文章|文档|word|docx|稿件).{0,18}(?:生成|完成|撰写|写完|制作|保存|交付)",
                    result_evidence,
                )
            )
            stage_adjustment += -0.22 if downstream_completed else 0.22
    exact_bonus = 0.0
    if normalized_query and normalized_query in normalized_title:
        exact_bonus = 0.25
    time_bonus = 0.0
    seconds = evidence.activity_seconds
    if hint is not None and seconds is not None:
        moment = datetime.fromtimestamp(seconds, tz=BEIJING)
        if hint.start <= moment <= hint.end:
            time_bonus = 0.1
    return max(
        0.0,
        min(
            1.0,
            0.45 * title_overlap
            + 0.45 * body_overlap
            + exact_bonus
            + time_bonus
            + stage_adjustment,
        ),
    )


_JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "minItems": 1,
            "maxItems": SEMANTIC_BATCH_SIZE,
            "items": {
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "string", "minLength": 1, "maxLength": 64},
                    "score": {"type": "integer", "minimum": 0, "maximum": 100},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "classification": {
                        "type": "string",
                        "enum": ["strong_match", "possible_match", "unlikely"],
                    },
                    "description": {"type": "string", "minLength": 1, "maxLength": 180},
                    "display_title": {"type": "string", "minLength": 2, "maxLength": 32},
                    "reason": {"type": "string", "minLength": 1, "maxLength": 180},
                },
                "required": [
                    "candidate_id", "score", "confidence", "classification",
                    "description", "display_title", "reason",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}


_JUDGE_INSTRUCTIONS = """你是 Codex 会话检索器。用户可能记错标题、时间，也可能把线索填错字段；必须理解所有线索的共同语义，不能只做关键词包含。
对每个候选判断它是否真正完成过用户描述的工作，并区分流程阶段，例如“完成选题检索但没有继续生成正文”和“已经形成正文/文档”。
score 表示该候选与全部线索的综合匹配度。description 用不超过 180 字的大白话说明这段会话实际做了什么；display_title 用 12～24 个简体中文字符概括实际工作内容，硬上限 32 字，不要照抄首轮提示词、路径、引号、编号，不要添加《》；reason 指出支持或反对匹配的具体证据。证据不足时必须低分，禁止为了凑结果抬高随机候选。
只分析输入 JSON，不得调用工具、读取文件、访问网络或猜测未提供内容。必须为每个 candidate_id 返回且只返回一项。"""


class SemanticJudge(Protocol):
    call_count: int

    def judge(self, query: str, candidates: Sequence[SessionEvidence]) -> list[SemanticAssessment]: ...


class LunaSemanticJudge:
    """使用已登录 Codex CLI 的 gpt-5.6-luna/low 做严格批量判断。"""

    def __init__(
        self,
        codex_command: str,
        *,
        timeout_seconds: float = 120.0,
        retry_policy: RetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.codex_command = codex_command
        self.timeout_seconds = float(timeout_seconds)
        self.retry_policy = retry_policy or RetryPolicy()
        self.sleep = sleep
        self.call_count = 0

    def judge(self, query: str, candidates: Sequence[SessionEvidence]) -> list[SemanticAssessment]:
        if not 1 <= len(candidates) <= SEMANTIC_BATCH_SIZE:
            raise ValueError("单批 Luna 候选必须介于 1 和 6")
        return call_with_retry(
            "Luna 会话语义评分",
            lambda: self._judge_once(query, candidates),
            self.retry_policy,
            sleep=self.sleep,
        )

    def _judge_once(self, query: str, candidates: Sequence[SessionEvidence]) -> list[SemanticAssessment]:
        configured = self.codex_command.strip()
        explicit = Path(configured).expanduser()
        command = str(explicit) if explicit.is_file() else shutil.which(configured)
        if not command:
            raise SessionSearchError("Codex CLI 不存在，无法调用 Luna")
        payload = {
            "all_user_clues": query,
            "candidates": [
                {
                    "candidate_id": item.record.thread_id,
                    "title": item.record.title,
                    "preview": item.record.preview,
                    "project": item.project_name,
                    "completed_at_beijing": _beijing_text(item.turn.completed_at),
                    "user_messages": list(item.user_messages),
                    "assistant_results": list(item.assistant_messages),
                    "tool_or_artifact_evidence": list(item.artifact_evidence),
                }
                for item in candidates
            ],
        }
        prompt = _JUDGE_INSTRUCTIONS + "\n\n输入 JSON：\n" + json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        )
        child_env = os.environ.copy()
        child_env.pop("OPENAI_API_KEY", None)
        child_env.pop("CODEX_API_KEY", None)
        startupinfo = None
        creationflags = 0
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        with tempfile.TemporaryDirectory(prefix="progress-wx-session-search-") as directory:
            root = Path(directory)
            schema_path = root / "schema.json"
            output_path = root / "result.json"
            schema_path.write_text(json.dumps(_JUDGE_SCHEMA, ensure_ascii=False), encoding="utf-8")
            # 计数的是实际发起的模型进程尝试；超时和非零退出也会消耗时间/额度，
            # 不能只统计成功返回到此处的调用。
            self.call_count += 1
            completed = subprocess.run(
                [
                    command, "exec", "--ephemeral", "--ignore-user-config", "--ignore-rules",
                    "--skip-git-repo-check", "--sandbox", "read-only", "--model", "gpt-5.6-luna",
                    "--config", 'model_reasoning_effort="low"', "--color", "never",
                    "--output-schema", str(schema_path), "--output-last-message", str(output_path),
                    "--cd", str(root), "-",
                ],
                input=prompt,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout_seconds,
                check=False,
                env=child_env,
                startupinfo=startupinfo,
                creationflags=creationflags,
            )
            if completed.returncode != 0:
                raise SessionSearchError(f"Luna 评分退出码 {completed.returncode}")
            try:
                raw = output_path.read_bytes()
                parsed = json.loads(raw.decode("utf-8-sig"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SessionSearchError("Luna 评分没有返回有效 JSON") from exc
        if not isinstance(parsed, Mapping) or set(parsed) != {"results"}:
            raise SessionSearchError("Luna 评分 JSON 字段不匹配")
        raw_results = parsed.get("results")
        if not isinstance(raw_results, list):
            raise SessionSearchError("Luna 评分 results 不是数组")
        expected = {item.record.thread_id for item in candidates}
        assessments: list[SemanticAssessment] = []
        for raw in raw_results:
            if not isinstance(raw, Mapping) or set(raw) != {
                "candidate_id", "score", "confidence", "classification", "description",
                "display_title", "reason"
            }:
                raise SessionSearchError("Luna 评分候选字段不匹配")
            candidate_id = str(raw.get("candidate_id") or "").strip()
            score = raw.get("score")
            confidence = str(raw.get("confidence") or "")
            classification = str(raw.get("classification") or "")
            description = _clean_fragment(raw.get("description"), 180)
            display_title = _clean_fragment(raw.get("display_title"), DISPLAY_TITLE_MAX_CHARS)
            reason = _clean_fragment(raw.get("reason"), 180)
            if (
                candidate_id not in expected
                or isinstance(score, bool)
                or not isinstance(score, int)
                or not 0 <= score <= 100
                or confidence not in {"high", "medium", "low"}
                or classification not in {"strong_match", "possible_match", "unlikely"}
                or not description
                or len(display_title) < 2
                or not reason
            ):
                raise SessionSearchError("Luna 评分候选内容无效")
            assessments.append(
                SemanticAssessment(
                    candidate_id, score / 100.0, confidence, classification,
                    description, display_title, reason,
                )
            )
        if len(assessments) != len(expected) or {item.thread_id for item in assessments} != expected:
            raise SessionSearchError("Luna 评分没有逐一覆盖候选")
        return assessments


def _event_for(record: ThreadRecord, turn: TurnRecord) -> TurnEvent:
    return TurnEvent(
        thread_id=record.thread_id,
        turn_id=turn.turn_id,
        status=turn.status.value,
        title=record.title,
        cwd=record.cwd,
        final_message=turn.final_message,
        error_message=turn.error_json or "",
        completed_at=turn.completed_at,
        generated_images=turn.generated_images,
        source="session-search",
        raw=turn.raw,
    )


class SessionSearchEngine:
    """分层检索：元数据/本地证据缩圈 → 少量 Luna → 同款进度摘要。"""

    def __init__(
        self,
        *,
        state: StateStore,
        codex_store: CodexStore,
        codex_home: Path,
        summarizer: ProgressSummarizer,
        judge: SemanticJudge,
        project_registry: CodexProjectRegistry | None = None,
        summary_retry_policy: RetryPolicy | None = None,
        summary_sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.state = state
        self.codex_store = codex_store
        self.codex_home = Path(codex_home).expanduser().resolve()
        self.summarizer = summarizer
        self.judge = judge
        self.project_registry = project_registry
        self.summary_retry_policy = summary_retry_policy or RetryPolicy()
        self.summary_sleep = summary_sleep
        self.now = now or (lambda: datetime.now(tz=BEIJING))

    def _cancelled(self, cancel_file: Path | None) -> None:
        if cancel_file is not None and cancel_file.exists():
            raise SessionSearchCancelled("搜索已由调用方取消")

    @staticmethod
    def _user_visible(record: ThreadRecord) -> bool:
        return record.thread_source in {"user", "agent_created_thread", ""} and record.thread_source != "subagent"

    def _projects(self) -> tuple[dict[str, str], dict[str, str], dict[str, Any] | None]:
        if self.project_registry is None:
            return {}, {}, None
        try:
            snapshot = self.project_registry.snapshot()
        except ProjectRegistryError as exc:
            return {}, {}, {"thread_id": "", "code": "project_registry_error", "details": [str(exc)]}
        names = {item.project_id: item.name for item in snapshot.projects}
        return dict(snapshot.thread_assignments), names, None

    def _collect(
        self,
        request: SearchRequest,
        scope: str,
        hint: TimeHint | None,
        progress: ProgressSink,
        cancel_file: Path | None,
    ) -> tuple[list[SessionEvidence], list[SessionEvidence], list[dict[str, Any]], int]:
        records = self.codex_store.select_threads(include_archived=True)
        self.codex_store.require_readable("枚举会话搜索目录")
        records = [item for item in records if self._user_visible(item)]
        assignments, project_names, project_warning = self._projects()
        warnings: list[dict[str, Any]] = []
        if project_warning is not None:
            warnings.append(project_warning)
        now = self.now().astimezone(BEIJING)
        selected: list[SessionEvidence] = []
        all_terminal: list[SessionEvidence] = []
        total = len(records)
        for index, record in enumerate(records, start=1):
            self._cancelled(cancel_file)
            progress.write("collecting", index, total, "正在读取会话完成时间")
            turn = self.codex_store.latest_completed_result_turn(record.thread_id)
            errors = self.codex_store.last_errors
            if errors:
                warnings.append({"thread_id": record.thread_id, "code": "codex_read_error", "details": list(errors)})
                continue
            if turn is None:
                warnings.append({"thread_id": record.thread_id, "code": "no_terminal_turn"})
                continue
            activity = _unix_seconds(turn.completed_at or turn.started_at)
            project_id = assignments.get(record.thread_id, "")
            candidate = SessionEvidence(
                record=record,
                turn=turn,
                project_id=project_id,
                project_name=project_names.get(project_id, "个人会话" if not project_id else project_id),
            )
            all_terminal.append(candidate)
            if _in_scope(activity, scope, hint, now):
                selected.append(candidate)
        return selected, all_terminal, warnings, len(records)

    def _read_evidence(
        self,
        selected: Sequence[SessionEvidence],
        progress: ProgressSink,
        cancel_file: Path | None,
        warnings: list[dict[str, Any]],
    ) -> None:
        for index, candidate in enumerate(selected, start=1):
            self._cancelled(cancel_file)
            progress.write("reading", index, len(selected), "正在提取候选会话证据")
            users, assistants, artifacts, warning = _extract_rollout_evidence(candidate.record, self.codex_home)
            candidate.user_messages = users
            candidate.assistant_messages = assistants
            candidate.artifact_evidence = artifacts
            if warning:
                warnings.append({"thread_id": candidate.record.thread_id, "code": warning})
            content = {
                "thread_id": candidate.record.thread_id,
                "turn_id": candidate.turn.turn_id,
                "completed_at": candidate.turn.completed_at,
                "final_message": candidate.turn.final_message,
                "evidence": candidate.cache_payload(),
            }
            candidate.content_hash = hashlib.sha256(
                json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            cached = self.state.session_search_cache(candidate.record.thread_id, candidate.content_hash)
            if cached is not None:
                candidate.description = str(cached.get("description") or "")
                candidate.last_result = str(cached.get("last_result") or "")
            else:
                self.state.put_session_search_cache(
                    thread_id=candidate.record.thread_id,
                    content_hash=candidate.content_hash,
                    latest_turn_id=candidate.turn.turn_id,
                    description="",
                    evidence=candidate.cache_payload(),
                    last_result="",
                    last_activity_at=candidate.activity_seconds,
                )

    def _summarize(self, candidate: SessionEvidence) -> str:
        if candidate.last_result:
            return candidate.last_result
        event = _event_for(candidate.record, candidate.turn)
        try:
            report = call_with_retry(
                "生成会话最后结果摘要",
                lambda: self.summarizer.summarize(event),
                self.summary_retry_policy,
                sleep=self.summary_sleep,
            )
        except (SummaryError, RuntimeError):
            report = fallback_report(event)
        candidate.last_result = report.details
        self.state.put_session_search_cache(
            thread_id=candidate.record.thread_id,
            content_hash=candidate.content_hash,
            latest_turn_id=candidate.turn.turn_id,
            description=candidate.description,
            evidence=candidate.cache_payload(),
            last_result=candidate.last_result,
            last_activity_at=candidate.activity_seconds,
        )
        return candidate.last_result

    def result_snapshot_for_thread(self, thread_id: str) -> Mapping[str, Any] | None:
        """冻结一次会话概览所用的摘要与同一完成轮次原文。"""

        record = self.codex_store.get_thread(thread_id, include_archived=True)
        self.codex_store.require_readable("读取会话概览")
        turn = self.codex_store.latest_completed_result_turn(thread_id)
        self.codex_store.require_readable("读取会话最后一轮")
        if record is None or turn is None:
            return None
        candidate = SessionEvidence(record=record, turn=turn)
        users, assistants, artifacts, _warning = _extract_rollout_evidence(record, self.codex_home)
        candidate.user_messages, candidate.assistant_messages, candidate.artifact_evidence = users, assistants, artifacts
        content = {
            "thread_id": record.thread_id,
            "turn_id": turn.turn_id,
            "completed_at": turn.completed_at,
            "final_message": turn.final_message,
            "evidence": candidate.cache_payload(),
        }
        candidate.content_hash = hashlib.sha256(
            json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        cached = self.state.session_search_cache(thread_id, candidate.content_hash)
        if cached is not None:
            candidate.description = str(cached.get("description") or "")
            candidate.last_result = str(cached.get("last_result") or "")
        else:
            self.state.put_session_search_cache(
                thread_id=thread_id,
                content_hash=candidate.content_hash,
                latest_turn_id=turn.turn_id,
                description="",
                evidence=candidate.cache_payload(),
                last_result="",
                last_activity_at=candidate.activity_seconds,
            )
        return {
            "summary": self._summarize(candidate),
            "completed_at": candidate.activity_seconds,
            "turn_id": turn.turn_id,
            "content_hash": candidate.content_hash,
            "raw_final": turn.final_message,
            "raw_sha256": hashlib.sha256(turn.final_message.encode("utf-8")).hexdigest(),
        }

    def last_result_for_thread(self, thread_id: str) -> tuple[str, int | None]:
        snapshot = self.result_snapshot_for_thread(thread_id)
        if snapshot is None:
            return "最近轮次暂无可展示的最终答复。", None
        completed_at = snapshot.get("completed_at")
        return (
            str(snapshot.get("summary") or "最近轮次暂无可展示的最终答复。"),
            int(completed_at) if completed_at is not None else None,
        )

    def _monitor_map(self) -> dict[str, Mapping[str, Any]]:
        return {str(item["thread_id"]): item for item in self.state.monitor_subscriptions()}

    def _public_title(self, record: ThreadRecord) -> tuple[str, str]:
        digest = thread_title_recovery_hash(record)
        cached = self.state.thread_title_recovery(record.thread_id, digest)
        recovered = str(cached.get("display_title") or "") if cached else ""
        return public_thread_title(record, recovered)

    def repair_missing_titles(self, *, max_model_calls: int = 3) -> Mapping[str, Any]:
        """一次性恢复真正缺失标题的历史用户会话，列表刷新不会调用本方法。"""

        if not 1 <= int(max_model_calls) <= 3:
            raise ValueError("max_model_calls 必须介于 1 和 3")
        records = self.codex_store.select_threads(include_archived=True)
        self.codex_store.require_readable("枚举标题异常会话")
        anomalies = [
            item
            for item in records
            if self._user_visible(item) and not independent_thread_title(item)
        ]
        pending: list[SessionEvidence] = []
        cached_count = 0
        warnings: dict[str, int] = {}
        for record in anomalies:
            digest = thread_title_recovery_hash(record)
            if self.state.thread_title_recovery(record.thread_id, digest) is not None:
                cached_count += 1
                continue
            turn = self.codex_store.latest_completed_result_turn(record.thread_id)
            self.codex_store.require_readable("读取标题异常会话的最后完成轮次")
            if turn is None:
                seconds = (
                    int(record.updated_at_ms // 1000)
                    if record.updated_at_ms is not None
                    else None
                )
                turn = TurnRecord(
                    thread_id=record.thread_id,
                    turn_id=f"title-recovery-{record.thread_id[:8]}",
                    status=ThreadStatus.UNKNOWN,
                    started_at=seconds,
                    completed_at=seconds,
                )
            candidate = SessionEvidence(record=record, turn=turn)
            users, assistants, artifacts, warning = _extract_rollout_evidence(
                record, self.codex_home
            )
            candidate.user_messages = users
            candidate.assistant_messages = assistants
            candidate.artifact_evidence = artifacts
            candidate.content_hash = digest
            if warning:
                warnings[warning] = warnings.get(warning, 0) + 1
            pending.append(candidate)

        allowed = int(max_model_calls) * SEMANTIC_BATCH_SIZE
        selected = pending[:allowed]
        initial_calls = int(getattr(self.judge, "call_count", 0))
        recovered_count = 0
        for offset in range(0, len(selected), SEMANTIC_BATCH_SIZE):
            batch = selected[offset : offset + SEMANTIC_BATCH_SIZE]
            assessments = self.judge.judge(TITLE_RECOVERY_PROMPT, batch)
            by_id = {item.thread_id: item for item in assessments}
            for candidate in batch:
                assessment = by_id.get(candidate.record.thread_id)
                if assessment is None:
                    raise SessionSearchError("标题恢复没有逐一覆盖候选")
                self.state.put_thread_title_recovery(
                    thread_id=candidate.record.thread_id,
                    content_hash=candidate.content_hash,
                    display_title=assessment.display_title,
                    source="luna_recovery",
                )
                recovered_count += 1
        model_calls = max(
            0, int(getattr(self.judge, "call_count", 0)) - initial_calls
        )
        return {
            "schema_version": 1,
            "total_anomalies": len(anomalies),
            "already_recovered": cached_count,
            "recovered": recovered_count,
            "remaining": max(0, len(pending) - recovered_count),
            "model_call_count": model_calls,
            "warning_counts": warnings,
        }

    def _match(
        self,
        candidate: SessionEvidence,
        assessment: SemanticAssessment,
        combined: float,
        monitors: Mapping[str, Mapping[str, Any]],
    ) -> SearchMatch:
        candidate.description = assessment.description or candidate.description or candidate.record.preview or candidate.record.title
        last_result = self._summarize(candidate)
        monitor = monitors.get(candidate.record.thread_id)
        if monitor is None:
            monitor_payload: Mapping[str, Any] = {"monitored": False, "origin": None, "expires_at": None}
        else:
            monitor_payload = {
                "monitored": True,
                "origin": str(monitor.get("origin") or ""),
                "expires_at": monitor.get("expires_at"),
            }
        self.state.put_session_search_cache(
            thread_id=candidate.record.thread_id,
            content_hash=candidate.content_hash,
            latest_turn_id=candidate.turn.turn_id,
            description=candidate.description,
            evidence=candidate.cache_payload(),
            last_result=last_result,
            last_activity_at=candidate.activity_seconds,
        )
        codex_title = independent_thread_title(candidate.record)
        if not codex_title and assessment.display_title:
            self.state.put_thread_title_recovery(
                thread_id=candidate.record.thread_id,
                content_hash=thread_title_recovery_hash(candidate.record),
                display_title=assessment.display_title,
                source="search_assessment",
            )
        title, title_origin = self._public_title(candidate.record)
        return SearchMatch(
            thread_id=candidate.record.thread_id,
            # schema v1 的 title 是供界面显示的名称：Codex 人工/自动标题优先；
            # 仅在 Codex 标题生成超时等可识别的历史异常中，才使用同一轮
            # Luna 已返回的 display_title 作为检索展示兜底。两者都不写回
            # Codex 任务元数据；title_source 进入内容哈希，使异步标题落盘后
            # 旧判断自然失效并重新读取真实标题。
            title=title,
            description=candidate.description,
            last_result=last_result,
            last_activity_at_beijing=_beijing_text(candidate.turn.completed_at or candidate.turn.started_at),
            score=combined,
            confidence=("high" if combined >= 0.78 else "medium" if combined >= 0.55 else "low"),
            classification=assessment.classification,
            reason=assessment.reason,
            project_id=candidate.project_id,
            project_name=candidate.project_name,
            archived=candidate.record.archived,
            host_id="local",
            monitor=monitor_payload,
            title_origin=title_origin,
            snapshot_turn_id=candidate.turn.turn_id,
            snapshot_content_hash=candidate.content_hash,
            raw_final_snapshot=candidate.turn.final_message,
        )

    def _expansion_warning(
        self, records: Sequence[SessionEvidence], next_scope: str | None, hint: TimeHint | None
    ) -> str:
        if next_scope is None:
            return "已经检查全部用户会话，不能再扩大范围。"
        now = self.now().astimezone(BEIJING)
        count = sum(1 for item in records if _in_scope(item.activity_seconds, next_scope, hint, now))
        semantic = count if next_scope == "all" else min(count, MAX_SEMANTIC_CANDIDATES)
        calls = math.ceil(semantic / SEMANTIC_BATCH_SIZE) if semantic else 0
        estimate = max(1, 2 + calls * 2)
        return (
            f"扩大后预计检查 {count} 个会话，最多对 {semantic} 个候选调用 {calls} 轮 Luna；"
            f"通常约需 {estimate}～{estimate + 4} 分钟，并会使用少量 Codex 每周额度。"
        )

    def search(
        self,
        request: SearchRequest,
        *,
        progress: ProgressSink | None = None,
        cancel_file: str | os.PathLike[str] | None = None,
    ) -> SearchResult:
        sink = progress or NullProgress()
        cancel_path = Path(cancel_file).expanduser().resolve() if cancel_file else None
        self._cancelled(cancel_path)
        hint = parse_time_hint(request.last_activity, now=self.now())
        scope = _scope_for(request, hint)
        search_id = uuid.uuid4().hex
        sink.write("collecting", 0, 0, "正在枚举用户会话")
        selected, all_terminal, warnings, _visible_total = self._collect(
            request, scope, hint, sink, cancel_path
        )
        self._read_evidence(selected, sink, cancel_path, warnings)
        query = request.query_text
        for candidate in selected:
            candidate.local_score = _local_score(query, candidate, hint)
        selected.sort(
            key=lambda item: (item.local_score, item.activity_seconds or -1, item.record.thread_id),
            reverse=True,
        )
        # 普通范围只让零模型排序前 12 名进入 Luna。用户明确扩大到 all 后，
        # 若前批没有形成可由剩余候选上界证明的唯一结果，则每批 6 个继续，
        # 直至全部候选完成或唯一结果已不可能被反超。
        candidate_pool = selected if scope == "all" else selected[:MAX_SEMANTIC_CANDIDATES]
        next_scope = _next_scope(scope)
        cost_warning = self._expansion_warning(all_terminal, next_scope, hint)
        if not candidate_pool:
            sink.write("completed", 0, 0, "当前范围没有可检索的已完成会话")
            return SearchResult(
                search_id, "not_found", scope, _scope_label(scope, hint), next_scope is not None,
                next_scope, cost_warning, (), 0, 0, 0, tuple(warnings),
            )
        monitors = self._monitor_map()
        if not query.strip():
            recent = candidate_pool[:3]
            assessments: dict[str, SemanticAssessment] = {}
            uncached: list[SessionEvidence] = []
            initial_calls = int(getattr(self.judge, "call_count", 0))
            for candidate in recent:
                real_title = independent_thread_title(candidate.record)
                if real_title:
                    assessments[candidate.record.thread_id] = SemanticAssessment(
                        candidate.record.thread_id,
                        0.5,
                        "low",
                        "possible_match",
                        candidate.description
                        or candidate.record.preview
                        or real_title
                        or "该会话暂无描述。",
                        real_title,
                        "没有填写线索，按最后活动时间列出最近会话。",
                    )
                    continue
                cached = self.state.session_search_judgment(
                    request.query_hash,
                    candidate.record.thread_id,
                    candidate.content_hash,
                )
                if cached is None:
                    uncached.append(candidate)
                    continue
                assessments[candidate.record.thread_id] = SemanticAssessment(
                    candidate.record.thread_id,
                    0.5,
                    "low",
                    "possible_match",
                    candidate.description
                    or candidate.record.preview
                    or candidate.record.title
                    or "该会话暂无描述。",
                    str(cached["display_title"]),
                    "没有填写线索，按最后活动时间列出最近会话。",
                )
            for offset in range(0, len(uncached), SEMANTIC_BATCH_SIZE):
                batch = uncached[offset : offset + SEMANTIC_BATCH_SIZE]
                sink.write(
                    "scoring",
                    min(offset + len(batch), len(uncached)),
                    len(uncached),
                    "正在为最近会话生成简洁名称",
                )
                for assessment in self.judge.judge(EMPTY_QUERY_TITLE_PROMPT, batch):
                    candidate = next(
                        item
                        for item in batch
                        if item.record.thread_id == assessment.thread_id
                    )
                    candidate.description = assessment.description
                    assessments[assessment.thread_id] = SemanticAssessment(
                        assessment.thread_id,
                        0.5,
                        "low",
                        "possible_match",
                        assessment.description,
                        assessment.display_title,
                        "没有填写线索，按最后活动时间列出最近会话。",
                    )
                    self.state.put_session_search_judgment(
                        query_hash=request.query_hash,
                        thread_id=assessment.thread_id,
                        content_hash=candidate.content_hash,
                        score=assessment.score,
                        confidence=assessment.confidence,
                        classification=assessment.classification,
                        display_title=assessment.display_title,
                        reason=assessment.reason,
                    )
            matches: list[SearchMatch] = []
            for candidate in recent:
                assessment = assessments.get(candidate.record.thread_id)
                if assessment is None:
                    raise SessionSearchError("最近会话展示名生成结果不完整")
                matches.append(self._match(candidate, assessment, 0.5, monitors))
            model_calls = max(
                0, int(getattr(self.judge, "call_count", 0)) - initial_calls
            )
            sink.write(
                "completed",
                len(matches),
                len(matches),
                "未填写线索，已列出最近会话",
            )
            return SearchResult(
                search_id,
                "ambiguous",
                scope,
                _scope_label(scope, hint),
                next_scope is not None,
                next_scope,
                cost_warning,
                tuple(matches),
                len(selected),
                len(uncached),
                model_calls,
                tuple(warnings),
                )
        sink.write("narrowing", len(candidate_pool), len(selected), "已完成零模型候选缩圈")
        assessments: dict[str, SemanticAssessment] = {}
        uncached: list[SessionEvidence] = []
        for candidate in candidate_pool:
            cached = self.state.session_search_judgment(
                request.query_hash, candidate.record.thread_id, candidate.content_hash
            )
            if cached is None:
                uncached.append(candidate)
                continue
            assessments[candidate.record.thread_id] = SemanticAssessment(
                candidate.record.thread_id,
                float(cached["score"]),
                str(cached["confidence"]),
                str(cached["classification"]),
                candidate.description or candidate.record.preview or candidate.record.title or "该会话暂无描述。",
                str(cached["display_title"]),
                str(cached["reason"]),
            )
        initial_calls = int(getattr(self.judge, "call_count", 0))
        def combined_score(candidate: SessionEvidence, assessment: SemanticAssessment) -> float:
            return min(1.0, assessment.score * 0.85 + candidate.local_score * 0.15)

        def safely_unique(remaining: Sequence[SessionEvidence]) -> bool:
            assessed = [
                (combined_score(candidate, assessments[candidate.record.thread_id]), candidate)
                for candidate in candidate_pool
                if candidate.record.thread_id in assessments
            ]
            if not assessed:
                return False
            assessed.sort(key=lambda item: (item[0], item[1].activity_seconds or -1), reverse=True)
            top = assessed[0][0]
            competitor = assessed[1][0] if len(assessed) > 1 else 0.0
            if remaining:
                # 未评分候选的模型分最高为 1.0，因此综合分严格不超过此上界。
                competitor = max(
                    competitor,
                    max(0.85 + candidate.local_score * 0.15 for candidate in remaining),
                )
            return top >= 0.78 and top - competitor >= 0.12

        for offset in range(0, len(uncached), SEMANTIC_BATCH_SIZE):
            self._cancelled(cancel_path)
            batch = uncached[offset : offset + SEMANTIC_BATCH_SIZE]
            sink.write("scoring", min(offset + len(batch), len(uncached)), len(uncached), "正在使用 Luna 判断少量候选")
            for assessment in self.judge.judge(query, batch):
                candidate = next(item for item in batch if item.record.thread_id == assessment.thread_id)
                candidate.description = assessment.description
                assessments[assessment.thread_id] = assessment
                self.state.put_session_search_judgment(
                    query_hash=request.query_hash,
                    thread_id=assessment.thread_id,
                    content_hash=candidate.content_hash,
                    score=assessment.score,
                    confidence=assessment.confidence,
                    classification=assessment.classification,
                    display_title=assessment.display_title,
                    reason=assessment.reason,
                )
                self.state.put_session_search_cache(
                    thread_id=candidate.record.thread_id,
                    content_hash=candidate.content_hash,
                    latest_turn_id=candidate.turn.turn_id,
                    description=candidate.description,
                    evidence=candidate.cache_payload(),
                    last_result=candidate.last_result,
                    last_activity_at=candidate.activity_seconds,
                )
            remaining = uncached[offset + len(batch) :]
            if scope == "all" and safely_unique(remaining):
                break
        evaluated = [
            candidate
            for candidate in candidate_pool
            if candidate.record.thread_id in assessments
        ]
        ranked: list[tuple[float, SessionEvidence, SemanticAssessment]] = []
        for candidate in evaluated:
            assessment = assessments.get(candidate.record.thread_id)
            if assessment is None:
                raise SessionSearchError("语义评分结果不完整")
            combined = combined_score(candidate, assessment)
            ranked.append((combined, candidate, assessment))
        ranked.sort(key=lambda item: (item[0], item[1].activity_seconds or -1), reverse=True)
        plausible = [item for item in ranked if item[0] >= 0.55 and item[2].classification != "unlikely"]
        if ranked and ranked[0][0] >= 0.78 and (
            len(ranked) == 1 or ranked[0][0] - ranked[1][0] >= 0.12
        ):
            chosen = [ranked[0]]
            status = "found"
        elif plausible:
            chosen = plausible
            status = "ambiguous"
        else:
            chosen = []
            status = "not_found"
        matches = tuple(self._match(candidate, assessment, score, monitors) for score, candidate, assessment in chosen)
        model_calls = int(getattr(self.judge, "call_count", 0)) - initial_calls
        sink.write("completed", len(matches), len(matches), "会话搜索完成")
        return SearchResult(
            search_id=search_id,
            status=status,
            scope=scope,
            scope_label=_scope_label(scope, hint),
            can_expand=next_scope is not None,
            next_scope=next_scope,
            cost_warning=cost_warning,
            matches=matches,
            examined_count=len(selected),
            semantic_candidate_count=len(evaluated),
            model_call_count=model_calls,
            warnings=tuple(warnings),
        )


def build_session_search_engine(
    *,
    state: StateStore,
    codex_store: CodexStore,
    codex_home: Path,
    summary_config: SummaryConfig,
    codex_command: str,
    project_registry: CodexProjectRegistry | None,
    retry_policy: RetryPolicy,
) -> SessionSearchEngine:
    return SessionSearchEngine(
        state=state,
        codex_store=codex_store,
        codex_home=codex_home,
        summarizer=ProgressSummarizer(summary_config),
        judge=LunaSemanticJudge(
            codex_command,
            timeout_seconds=summary_config.timeout_seconds,
            retry_policy=retry_policy,
        ),
        project_registry=project_registry,
        summary_retry_policy=retry_policy,
    )


__all__ = [
    "AtomicProgressFile",
    "BEIJING",
    "LunaSemanticJudge",
    "NullProgress",
    "SearchMatch",
    "SearchRequest",
    "SearchResult",
    "SemanticAssessment",
    "SessionEvidence",
    "SessionSearchCancelled",
    "SessionSearchEngine",
    "SessionSearchError",
    "SESSION_SEARCH_SCHEMA_VERSION",
    "build_session_search_engine",
    "parse_time_hint",
]
