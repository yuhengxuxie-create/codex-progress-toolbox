"""轻量 SQLite 状态库：事件队列、投递去重和引用回复的一次性映射。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .models import (
    GeneratedImageArtifact,
    NotificationContext,
    ProgressReport,
    TurnEvent,
)
from .user_reply_chain import REQUIRED_COLUMNS as USER_REPLY_CHAIN_COLUMNS
from .user_reply_chain import SCHEMA_SQL as USER_REPLY_CHAIN_SCHEMA_SQL
from .file_delivery import SCHEMA_SQL as ARTIFACT_SCHEMA_SQL


SCHEMA_VERSION = 23
_SQLITE_LOCK_WAIT_SECONDS = 5.0
_SQLITE_LOCK_RETRY_SECONDS = 0.02
_HOOK_ENQUEUE_MIN_SCHEMA_VERSION = 15
_READ_ONLY_MIN_SCHEMA_VERSION = 16
_PERMANENT_EXPIRY = 9_223_372_036_854_775_807
_STAGED_IMAGE_MAX_COUNT = 5
_STAGED_IMAGE_MAX_TOTAL_BYTES = 50 * 1024 * 1024
_AUTO_MONITORING_ENABLED_KEY = "auto_monitoring_enabled_v1"
_AUTO_MONITORING_EFFECTIVE_AT_KEY = "auto_monitoring_effective_at_v1"
_CURRENT_THREAD_BINDING_TTL_DAYS = 30
_CURRENT_THREAD_BINDING_MAX_TTL_DAYS = 365
_CURRENT_THREAD_TITLE_MAX_CHARS = 64
_NOTIFICATION_JUDGMENT_TEXT_MAX_CHARS = 1_200
_NOTIFICATION_JUDGMENT_REASON_MAX_CHARS = 320
_NOTIFICATION_JUDGMENT_FACT_MAX_CHARS = 320
_NOTIFICATION_JUDGMENT_MAX_FACTS = 5
_NOTIFICATION_JUDGMENT_RECENT_MAX = 5
_NOTIFICATION_SECRET_PATTERN = re.compile(
    r"(?i)(app[_ -]?secret|api[_ -]?key|access[_ -]?token|refresh[_ -]?token|token|secret|password|密码|密钥|令牌)"
    r"(\s*[:=：]\s*)([^\s,，;；]+)"
)
_NOTIFICATION_URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
_NOTIFICATION_PATH_PATTERN = re.compile(
    r"(?:[A-Za-z]:[\\/]|\\\\)[^\s，。；：、]+", re.IGNORECASE
)
_NOTIFICATION_HASH_PATTERN = re.compile(r"\b[0-9a-f]{24,}\b", re.IGNORECASE)

# These are the only remote-control actions that may cross the Codex App
# Server write boundary.  Read-only discovery (goal_get/skills_list) is not
# recorded in this table; it has no external side effect to protect.  Keep the
# ordered tuple alongside the set so that the SQLite CHECK and validation
# error remain deterministic when new typed actions are added.
_REMOTE_CONTROL_ACTION_NAMES = (
    "goal_set",
    "goal_clear",
    "plan_start",
    "skill_start",
    "compact_start",
    "fork_start",
    "review_start",
    "feedback_upload",
    "model_set",
    "personality_set",
    "reasoning_set",
    "fast_toggle",
    "memories_set",
    "monitor_auto_set",
)
REMOTE_CONTROL_ACTIONS = frozenset(_REMOTE_CONTROL_ACTION_NAMES)


class StateError(RuntimeError):
    """本地状态库无法安全读写。"""


def enqueue_hook_payload_only(
    path: str | Path,
    payload: Mapping[str, Any],
    *,
    now: int | None = None,
) -> bool:
    """只向已经初始化的状态库追加一条 Codex Hook 事件。

    这是短命 ``notify`` 进程唯一允许使用的数据库入口。它故意不复用
    :class:`StateStore`：不会切换 journal mode、创建表、执行 ALTER，也不会推进
    ``schema_version``。正式 schema 迁移只能由受控启动的长期服务或明确的管理
    CLI 承担。
    """

    database = Path(path).expanduser().resolve()
    if not database.is_file():
        raise StateError("Hook 状态库尚未由服务初始化")
    thread_id = str(payload.get("thread-id") or payload.get("thread_id") or "").strip()
    turn_id = str(payload.get("turn-id") or payload.get("turn_id") or "").strip()
    event_type = str(payload.get("type") or "").strip()
    if event_type != "agent-turn-complete" or not thread_id or not turn_id:
        raise StateError("Codex notify 缺少合法的 type/thread-id/turn-id")
    event_key = f"{thread_id}:{turn_id}:completed"
    encoded = json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":"))

    # ``mode=rw`` 保证路径异常时拒绝，而不是由 Hook 偷偷创建一个空数据库。
    uri = f"file:{database.as_posix()}?mode=rw"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=_SQLITE_LOCK_WAIT_SECONDS)
    except sqlite3.Error as exc:
        raise StateError("Hook 状态库无法安全打开") from exc
    try:
        connection.execute(f"PRAGMA busy_timeout={int(_SQLITE_LOCK_WAIT_SECONDS * 1000)}")
        deadline = time.monotonic() + _SQLITE_LOCK_WAIT_SECONDS
        while True:
            try:
                connection.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                    raise
                time.sleep(min(_SQLITE_LOCK_RETRY_SECONDS, max(0.0, deadline - time.monotonic())))

        table_names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('meta','hook_events')"
            )
        }
        if table_names != {"meta", "hook_events"}:
            raise StateError("Hook 状态库缺少既有 meta/hook_events 表")
        version_row = connection.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        try:
            version = int(version_row[0]) if version_row is not None else -1
        except (TypeError, ValueError) as exc:
            raise StateError("Hook 状态库 schema_version 无效") from exc
        if version < _HOOK_ENQUEUE_MIN_SCHEMA_VERSION:
            raise StateError(
                f"Hook 状态库版本 {version} 低于只入队入口支持的 "
                f"{_HOOK_ENQUEUE_MIN_SCHEMA_VERSION}"
            )
        if version > SCHEMA_VERSION:
            raise StateError(
                f"Hook 状态库版本 {version} 高于本程序支持的 {SCHEMA_VERSION}，拒绝降级写入"
            )
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(hook_events)")
        }
        required = {"event_key", "payload_json", "created_at", "consumed_at"}
        missing = required - columns
        if missing:
            raise StateError(
                "Hook 状态库 hook_events 缺少字段：" + ", ".join(sorted(missing))
            )
        cursor = connection.execute(
            "INSERT OR IGNORE INTO hook_events(event_key, payload_json, created_at) VALUES(?,?,?)",
            (event_key, encoded, int(time.time()) if now is None else int(now)),
        )
        connection.commit()
        return cursor.rowcount == 1
    except StateError:
        connection.rollback()
        raise
    except sqlite3.Error as exc:
        connection.rollback()
        raise StateError("Hook 事件只入队失败") from exc
    finally:
        connection.close()


@dataclass(frozen=True, slots=True)
class TurnReplyDelivery:
    """一条普通引用回复的独立、可持久恢复投递记录。"""

    delivery_id: str
    parent_code: str
    thread_id: str
    turn_id: str
    reply_text: str
    fingerprint: str
    sequence: int
    is_new: bool
    state: str


@dataclass(frozen=True, slots=True)
class NotificationMediaDelivery:
    """一张通知图片的持久、逐项投递状态。"""

    delivery_id: str
    event_key: str
    ordinal: int
    item_id: str
    path: str
    mime_type: str
    sha256: str
    size: int
    file_name: str
    attempt_count: int
    claimed_at: int | None
    delivered_at: int | None
    rejected_at: int | None
    uncertain_at: int | None
    discarded_at: int | None
    next_attempt_at: int
    channel_message_id: str | None
    last_error_code: str | None
    warning_sent_at: int | None


@dataclass(frozen=True, slots=True)
class NotificationSummaryDelivery:
    """一条完成摘要的持久投递记录。

    该记录只保存摘要文本和投递状态；原始最终答复、事件正文以及其它隐私
    内容不进入此表。thread/turn/code 从 ``notifications`` 只读联接得到，
    便于发送器定位父通知而不会在摘要 outbox 复制正文。
    """

    delivery_id: str
    event_key: str
    code: str
    thread_id: str
    turn_id: str
    created_at: int
    next_attempt_at: int
    attempt_count: int
    claimed_at: int | None
    message_text: str
    prepared_at: int | None
    submitted_at: int | None
    delivered_at: int | None
    rejected_at: int | None
    uncertain_at: int | None
    channel_message_ids: tuple[str, ...]
    last_error: str | None
    state: str


@dataclass(frozen=True, slots=True)
class NotificationJudgment:
    """Persisted, bounded evidence for one notification policy decision.

    The record deliberately stores compact context rather than the full Codex
    rollout or the platform payload.  It is useful for audit and retry
    decisions after a service restart, while user text remains capped and
    common secret/path/hash forms are redacted before insertion.
    """

    event_key: str
    created_at: int
    updated_at: int
    status: str
    notification_reason: str
    decision_reason: str
    matched_request: str
    user_request: str
    task_state: str
    recent_successful_notifications: tuple[str, ...]
    new_facts: tuple[str, ...]
    model_error: str
    model_attempts: int
    input_digest: str

    @property
    def reason(self) -> str:
        return self.decision_reason

    @property
    def category(self) -> str:
        return self.notification_reason

    @property
    def is_model_failure(self) -> bool:
        return bool(self.model_error)


@dataclass(frozen=True, slots=True)
class NotificationSummaryRecoveryCandidate:
    """已跨外部提交边界但本地结果未知的摘要恢复候选。

    这只是恢复器的内部最小索引，不携带 Codex 正文或其它用户内容；
    ``message_text`` 是飞书出站摘要正文，供 service 在官方按 ID 取回父
    消息后做逐字/SHA 唯一匹配。候选读取本身不改变任何状态。
    """

    event_key: str
    code: str
    thread_id: str
    turn_id: str
    message_text: str
    created_at: int
    submitted_at: int
    uncertain_at: int
    expires_at: int
    reply_kind: str
    sent_at: int
    raw_binding_prepared: bool


@dataclass(frozen=True, slots=True)
class NotificationRawContext:
    """一条完成摘要分片对应的精确原文读取身份；不保存原文。"""

    message_id: str
    event_key: str
    sender_id: str
    chat_id: str
    thread_id: str
    turn_id: str
    content_sha256: str
    created_at: int


@dataclass(frozen=True, slots=True)
class NotificationRawLegacySource:
    """一条尚未物化原文上下文的旧完成通知；不保存摘要或原文。"""

    message_id: str
    event_key: str
    thread_id: str
    turn_id: str


@dataclass(frozen=True, slots=True)
class NotificationRawDelivery:
    """一次 ``.原文`` 请求的持久、可恢复文本投递状态。"""

    delivery_id: str
    inbound_message_id: str
    parent_message_id: str
    event_key: str
    sender_id: str
    chat_id: str
    thread_id: str
    turn_id: str
    content_sha256: str
    fingerprint: str
    created_at: int
    next_attempt_at: int
    attempt_count: int
    claimed_at: int | None
    response_kind: str
    prepared_at: int | None
    submitted_at: int | None
    delivered_at: int | None
    rejected_at: int | None
    uncertain_at: int | None
    result_message_ids: tuple[str, ...]
    last_error_code: str | None
    state: str
    is_new: bool = False


@dataclass(frozen=True, slots=True)
class ResetAlertDelivery:
    """一条 Codex 重置预警的持久投递状态。"""

    delivery_id: str
    event_key: str
    message_text: str
    created_at: int
    next_attempt_at: int
    attempt_count: int
    claimed_at: int | None
    submitted_at: int | None
    delivered_at: int | None
    rejected_at: int | None
    uncertain_at: int | None
    expired_at: int | None
    channel_message_ids: tuple[str, ...]
    last_error_code: str | None
    state: str


@dataclass(frozen=True, slots=True)
class ManagementContextRecord:
    """按飞书出站消息定位到的不可变管理上下文及其所有者。"""

    context_id: str
    context_kind: str
    payload: Mapping[str, Any]
    sender_id: str
    chat_id: str
    # Kept optional for source compatibility with callers that construct the
    # historical five-field record directly.  StateStore always populates
    # these values from the immutable database row.
    created_at: int = 0
    expires_at: int = _PERMANENT_EXPIRY


@dataclass(frozen=True, slots=True)
class ManagementActionReservation:
    """一次 context/action 原子占用的结果。"""

    status: str
    attempt_count: int


@dataclass(frozen=True, slots=True)
class RemoteControlActionReservation:
    """一次远程 Codex 写动作的 context/action/request 原子占用结果。

    ``request_hash`` 让同一个查询上下文可以安全地提交不同的合法请求，
    同时保证同一请求在飞书重复投递、并发双击或服务重启后不会被重放。
    """

    status: str
    attempt_count: int
    request_hash: str = ""


class CorrelationCodec:
    """生成并校验不含账号信息的短 HMAC 通知编号。"""

    PREFIX = "PCWX"

    def __init__(self, secret: bytes):
        if len(secret) < 32:
            raise ValueError("HMAC 密钥至少需要 32 字节")
        self._secret = secret

    @classmethod
    def from_file(cls, path: Path) -> "CorrelationCodec":
        try:
            encoded = path.read_text(encoding="ascii").strip()
            secret = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        except (OSError, ValueError) as exc:
            raise StateError(f"无法读取 HMAC 密钥：{path}") from exc
        return cls(secret)

    @staticmethod
    def create_secret_file(path: Path) -> None:
        """首次安装时原子创建密钥；已存在时绝不覆盖。"""

        path.parent.mkdir(parents=True, exist_ok=True)
        payload = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=") + b"\n"
        created = False
        try:
            with path.open("xb") as handle:
                handle.write(payload)
            created = True
        except FileExistsError:
            pass
        try:
            if os.name == "nt":
                domain = os.environ.get("USERDOMAIN", "").strip()
                username = os.environ.get("USERNAME", "").strip()
                identity = f"{domain}\\{username}" if domain and username else username
                if not identity:
                    raise StateError("无法确定当前 Windows 身份，不能安全设置 HMAC ACL")
                completed = subprocess.run(
                    [
                        "icacls.exe",
                        str(path),
                        "/inheritance:r",
                        "/grant:r",
                        f"{identity}:(F)",
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if completed.returncode != 0:
                    raise StateError("收紧 HMAC 密钥 ACL 失败")
            else:
                os.chmod(path, 0o600)
        except BaseException:
            if created:
                path.unlink(missing_ok=True)
            raise

    def issue(self) -> str:
        token = base64.b32encode(secrets.token_bytes(8)).decode("ascii").rstrip("=")
        signature = hmac.new(self._secret, token.encode("ascii"), hashlib.sha256).hexdigest()[:12].upper()
        return f"{self.PREFIX}-{token}-{signature}"

    def valid(self, code: str) -> bool:
        parts = str(code or "").strip().upper().split("-")
        if len(parts) != 3 or parts[0] != self.PREFIX:
            return False
        token, supplied = parts[1], parts[2]
        if not token or len(supplied) != 12:
            return False
        expected = hmac.new(self._secret, token.encode("ascii", "ignore"), hashlib.sha256).hexdigest()[:12].upper()
        return hmac.compare_digest(supplied, expected)

    @classmethod
    def extract(cls, quoted_text: str) -> str | None:
        """从被引用原文中提取严格格式编号，不解释其他文本。"""

        match = re.search(r"(?<![A-Z0-9])PCWX-[A-Z2-7]{8,32}-[A-F0-9]{12}(?![A-Z0-9])", str(quoted_text or "").upper())
        return match.group(0) if match else None


class StateStore:
    """线程安全的 SQLite 包装；跨进程写入由 WAL 与 busy_timeout 协调。"""

    # 只读入口需要检查 schema16 的全部既有业务表。媒体 outbox 自 schema15
    # 起已存在；只有 schema17 新增的摘要 outbox 可在 schema16 中缺失。
    _READ_ONLY_REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
        "meta": frozenset({"key", "value"}),
        "hook_events": frozenset({"event_key", "payload_json", "created_at", "consumed_at"}),
        "notifications": frozenset({
            "event_key", "code", "thread_id", "turn_id", "reply_kind", "message_text",
            "created_at", "expires_at", "sent_at", "consumed_at", "reply_fingerprint",
            "reply_text", "claimed_at", "delivered_at", "channel_message_id", "discarded_at",
        }),
        "processed_turns": frozenset({"event_key", "processed_at"}),
        "notification_message_ids": frozenset({"message_id", "event_key", "created_at"}),
        "reply_deliveries": frozenset({
            "sequence", "delivery_id", "parent_code", "inbound_message_id",
            "reply_fingerprint", "reply_text", "created_at", "claimed_at", "delivered_at",
            "discarded_at", "receipt_required", "receipt_sent_at",
        }),
        "management_contexts": frozenset({
            "context_id", "context_kind", "payload_json", "created_at", "expires_at",
            "sender_id", "chat_id",
        }),
        "management_message_ids": frozenset({"message_id", "context_id", "created_at"}),
        "management_inbound_messages": frozenset({
            "message_id", "sender_id", "content_hash", "created_at", "completed_at",
        }),
        "management_context_actions": frozenset({
            "context_id", "action", "inbound_message_id", "created_at", "updated_at",
            "attempt_count", "claimed_at", "submitted_at", "succeeded_at", "rejected_at",
            "uncertain_at", "result_message_ids_json", "last_error_code",
        }),
        "staged_image_replies": frozenset({
            "sender_id", "chat_id", "reply_to_message_id", "attachments_json",
            "source_message_ids_json", "created_at", "expires_at",
        }),
        "monitor_subscriptions": frozenset({
            "thread_id", "origin", "added_at", "last_activity_at", "expires_at",
        }),
        "monitor_suppressions": frozenset({"thread_id", "removed_at"}),
        "session_search_cache": frozenset({
            "thread_id", "content_hash", "latest_turn_id", "description", "evidence_json",
            "last_result", "last_activity_at", "updated_at",
        }),
        "session_search_judgments": frozenset({
            "query_hash", "thread_id", "content_hash", "score", "confidence",
            "classification", "display_title", "reason", "updated_at",
        }),
        "thread_title_recoveries": frozenset({
            "thread_id", "content_hash", "display_title", "source", "created_at", "updated_at",
        }),
        "notification_media_deliveries": frozenset({
            "delivery_id", "event_key", "ordinal", "item_id", "path", "mime_type",
            "sha256", "size", "file_name", "created_at", "next_attempt_at", "attempt_count",
            "claimed_at", "delivered_at", "rejected_at", "uncertain_at", "discarded_at",
            "channel_message_id", "last_error_code", "warning_sent_at",
        }),
    }
    _READ_ONLY_SCHEMA17_COLUMNS: dict[str, frozenset[str]] = {
        "notification_summary_deliveries": frozenset({
            "event_key", "created_at", "next_attempt_at", "attempt_count", "claimed_at",
            "message_text", "prepared_at", "submitted_at", "delivered_at", "rejected_at",
            "uncertain_at", "channel_message_ids_json", "last_error",
        }),
    }
    _READ_ONLY_SCHEMA18_COLUMNS: dict[str, frozenset[str]] = {
        "reset_alert_state": frozenset({
            "singleton", "enabled", "bootstrap_completed_at", "last_attempt_at",
            "last_success_at", "next_check_at", "window_start_at", "window_end_at",
            "run_slot_at", "last_completed_slot_at", "last_run_status",
            "last_error_code", "worker_started_at", "worker_heartbeat_at",
            "worker_stopped_at", "updated_at",
        }),
        "reset_alert_sources": frozenset({
            "source_id", "cursor_json", "last_attempt_at", "last_success_at",
            "last_item_at", "baseline_completed_at", "health", "last_error_code",
            "payload_hash", "updated_at",
        }),
        "reset_alert_signals": frozenset({
            "signal_key", "source_id", "source_item_id", "source_url",
            "published_at", "observed_at", "content_hash", "signal_kind",
            "is_official", "payload_json",
        }),
        "reset_alert_events": frozenset({
            "event_key", "level", "evidence", "window_text", "advice",
            "source_ids_json", "created_at", "expires_at", "fingerprint",
            "notified_at",
        }),
        "reset_alert_deliveries": frozenset({
            "delivery_id", "event_key", "message_text", "created_at",
            "next_attempt_at", "attempt_count", "claimed_at", "submitted_at",
            "delivered_at", "rejected_at", "uncertain_at",
            "expired_at", "channel_message_ids_json", "last_error_code",
        }),
    }
    _READ_ONLY_SCHEMA19_COLUMNS: dict[str, frozenset[str]] = {
        "remote_control_actions": frozenset({
            "context_id", "action", "request_hash", "inbound_message_id",
            "created_at", "updated_at", "attempt_count", "claimed_at",
            "submitted_at", "succeeded_at", "rejected_at", "uncertain_at",
            "result_json", "last_error_code",
        }),
    }
    _READ_ONLY_SCHEMA20_COLUMNS: dict[str, frozenset[str]] = {
        "management_current_bindings": frozenset({
            "sender_id", "chat_id", "thread_id", "display_title", "created_at",
            "updated_at", "expires_at",
        }),
    }
    _READ_ONLY_SCHEMA21_COLUMNS: dict[str, frozenset[str]] = {
        "notification_raw_bindings": frozenset({
            "event_key", "sender_id", "thread_id", "turn_id", "content_sha256",
            "created_at", "finalized_at",
        }),
        "notification_raw_contexts": frozenset({
            "message_id", "event_key", "sender_id", "chat_id", "thread_id", "turn_id",
            "content_sha256", "created_at",
        }),
        "notification_raw_deliveries": frozenset({
            "delivery_id", "inbound_message_id", "parent_message_id", "event_key", "sender_id",
            "chat_id", "thread_id", "turn_id", "content_sha256", "fingerprint",
            "created_at", "next_attempt_at", "attempt_count", "claimed_at",
            "response_kind", "prepared_at", "submitted_at", "delivered_at",
            "rejected_at", "uncertain_at", "result_message_ids_json",
            "last_error_code",
        }),
    }
    _READ_ONLY_SCHEMA22_COLUMNS: dict[str, frozenset[str]] = {
        "user_reply_chain_messages": USER_REPLY_CHAIN_COLUMNS,
    }

    def __init__(
        self,
        path: str | Path,
        *,
        mode: str = "rw",
        migrate: bool = True,
    ):
        mode = str(mode).strip().casefold()
        if mode not in {"rw", "ro"}:
            raise ValueError("StateStore mode 必须是 rw 或 ro")
        if mode == "ro" and migrate:
            raise ValueError("只读 StateStore 必须显式使用 migrate=False")
        if mode == "rw" and not migrate:
            raise ValueError("migrate=False 只允许用于 mode=ro 的只读连接")
        self.path = Path(path).resolve()
        self.read_only = mode == "ro"
        self._read_only_tables: frozenset[str] = frozenset()
        self._read_only_schema_version: int | None = None
        if not self.read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        if self.read_only:
            if not self.path.is_file():
                raise StateError(f"只读状态库不存在：{self.path}")
            try:
                # URI mode=ro 是硬性写保护；不会创建数据库、WAL 或临时迁移表。
                # 没有活动 WAL 时再使用 immutable，避免 SQLite 为只读连接
                # 创建 -shm；若已有 WAL 则保留普通 ro 读取最新提交。
                wal_path = Path(f"{self.path}-wal")
                # 使用 ``as_uri`` 正确转义空格、# 等合法 Windows 路径字符；
                # 手工拼接 ``file:`` URI 会把它们误解为 URI 语法，导致只读
                # 命令在不同安装目录下无法打开同一份状态库。
                uri = f"{self.path.as_uri()}?mode=ro"
                if not wal_path.exists():
                    uri += "&immutable=1"
                self._connection = sqlite3.connect(
                    uri,
                    uri=True,
                    timeout=5,
                    check_same_thread=False,
                )
            except sqlite3.Error as exc:
                raise StateError(f"只读状态库无法打开：{self.path}") from exc
        else:
            self._connection = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._connection.execute("PRAGMA foreign_keys=ON")
        if self.read_only:
            # URI mode=ro 是文件级写保护；query_only 也覆盖误用该连接的
            # 事务/触发器路径，令只读意图在 SQLite 连接自身再次得到约束。
            self._connection.execute("PRAGMA query_only=ON")
        try:
            if self.read_only:
                self._validate_read_only_schema()
            else:
                # 切换 journal_mode 本身需要数据库写锁；两个短命 hook 进程可能在
                # 同一时刻首次打开旧库，而 SQLite 对该 PRAGMA 不总是按 busy_timeout
                # 等待。显式短暂重试，避免并发初始化把合法状态库误判为损坏。
                self._execute_locked_pragma("PRAGMA journal_mode=WAL")
                self._connection.execute("PRAGMA synchronous=NORMAL")
                self._initialize()
        except sqlite3.DatabaseError as exc:
            self._connection.close()
            raise StateError("状态库结构损坏或无法迁移") from exc
        except BaseException:
            self._connection.close()
            raise

    @classmethod
    def open_read_only(cls, path: str | Path) -> "StateStore":
        """打开已由服务初始化的状态库；绝不创建、迁移或切换 journal。"""

        return cls(path, mode="ro", migrate=False)

    def _validate_read_only_schema(self) -> None:
        """在 mode=ro 连接上检查兼容性；任何缺表/缺列都只读拒绝。"""

        table_names = {
            str(row[0])
            for row in self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "meta" not in table_names:
            raise StateError("只读状态库缺少既有 meta 表；请由服务受控升级")
        version_row = self._connection.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        try:
            version = int(version_row[0]) if version_row is not None else -1
        except (TypeError, ValueError) as exc:
            raise StateError("只读状态库 schema_version 无效") from exc
        if version < _READ_ONLY_MIN_SCHEMA_VERSION:
            raise StateError(
                f"只读命令不兼容状态库版本 {version}；请先由服务受控升级到 "
                f"{_READ_ONLY_MIN_SCHEMA_VERSION} 或更高"
            )
        if version > SCHEMA_VERSION:
            raise StateError(
                f"状态库版本 {version} 高于本程序支持的 {SCHEMA_VERSION}，拒绝只读查询"
            )
        self._read_only_schema_version = version
        required = dict(self._READ_ONLY_REQUIRED_COLUMNS)
        if version >= 17:
            required.update(self._READ_ONLY_SCHEMA17_COLUMNS)
        if version >= 18:
            required.update(self._READ_ONLY_SCHEMA18_COLUMNS)
        if version >= 19:
            required.update(self._READ_ONLY_SCHEMA19_COLUMNS)
        if version >= 20:
            required.update(self._READ_ONLY_SCHEMA20_COLUMNS)
        if version >= 21:
            required.update(self._READ_ONLY_SCHEMA21_COLUMNS)
        if version >= 22:
            required.update(self._READ_ONLY_SCHEMA22_COLUMNS)
        if version >= 23:
            required["artifact_file_deliveries"] = {"delivery_id", "event_key", "state", "snapshot_json", "sha256", "notice_state", "message_ids_json", "media_kind", "notice_key"}
        missing_tables = set(required) - table_names
        if missing_tables:
            raise StateError(
                "只读状态库缺少既有表：" + ", ".join(sorted(missing_tables))
                + "；未执行修复，请由服务受控升级"
            )
        for table, expected in required.items():
            actual = {
                str(row[1])
                for row in self._connection.execute(f'PRAGMA table_info("{table}")')
            }
            missing = expected - actual
            if missing:
                raise StateError(
                    f"只读状态库表 {table} 缺少字段：{', '.join(sorted(missing))}"
                    "；未执行修复，请由服务受控升级"
                )
        self._read_only_tables = frozenset(table_names)

    def _execute_locked_pragma(self, statement: str) -> None:
        """在数据库短暂被其他初始化连接占用时等待并重试 PRAGMA。"""

        deadline = time.monotonic() + _SQLITE_LOCK_WAIT_SECONDS
        while True:
            try:
                self._connection.execute(statement)
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(_SQLITE_LOCK_RETRY_SECONDS, remaining))

    def _initialize(self) -> None:
        with self._connection:
            # 服务与短命 hook 可能同时首次打开旧库；写锁必须覆盖版本读取和全部 ALTER。
            deadline = time.monotonic() + _SQLITE_LOCK_WAIT_SECONDS
            while True:
                try:
                    self._connection.execute("BEGIN IMMEDIATE")
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower():
                        raise
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise
                    time.sleep(min(_SQLITE_LOCK_RETRY_SECONDS, remaining))
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            version_row = self._connection.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            existing_version = 0
            if version_row is not None:
                try:
                    existing_version = int(version_row[0])
                except (TypeError, ValueError) as exc:
                    raise StateError("状态库 schema_version 无效") from exc
                if existing_version > SCHEMA_VERSION:
                    raise StateError(
                        f"状态库版本 {existing_version} 高于本程序支持的 {SCHEMA_VERSION}，拒绝降级"
                    )
            schema_statements = (
                """CREATE TABLE IF NOT EXISTS hook_events (
                    event_key TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    consumed_at INTEGER
                )""",
                """CREATE TABLE IF NOT EXISTS notifications (
                    event_key TEXT PRIMARY KEY,
                    code TEXT NOT NULL UNIQUE,
                    thread_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    reply_kind TEXT NOT NULL DEFAULT 'turn',
                    message_text TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    sent_at INTEGER,
                    channel_message_id TEXT,
                    consumed_at INTEGER,
                    reply_fingerprint TEXT UNIQUE,
                    reply_text TEXT,
                    claimed_at INTEGER,
                    delivered_at INTEGER,
                    discarded_at INTEGER
                )""",
                """CREATE TABLE IF NOT EXISTS processed_turns (
                    event_key TEXT PRIMARY KEY,
                    processed_at INTEGER NOT NULL
                )""",
                """CREATE TABLE IF NOT EXISTS notification_message_ids (
                    message_id TEXT PRIMARY KEY,
                    event_key TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(event_key) REFERENCES notifications(event_key) ON DELETE CASCADE
                )""",
                "CREATE INDEX IF NOT EXISTS idx_notifications_code ON notifications(code)",
                "CREATE INDEX IF NOT EXISTS idx_notifications_expiry ON notifications(expires_at, consumed_at)",
                "CREATE INDEX IF NOT EXISTS idx_notification_message_event ON notification_message_ids(event_key)",
                """CREATE TABLE IF NOT EXISTS notification_summary_deliveries (
                    event_key TEXT PRIMARY KEY,
                    created_at INTEGER NOT NULL,
                    next_attempt_at INTEGER NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    claimed_at INTEGER,
                    message_text TEXT NOT NULL DEFAULT '',
                    prepared_at INTEGER,
                    submitted_at INTEGER,
                    delivered_at INTEGER,
                    rejected_at INTEGER,
                    uncertain_at INTEGER,
                    channel_message_ids_json TEXT NOT NULL DEFAULT '[]',
                    last_error TEXT,
                    FOREIGN KEY(event_key) REFERENCES notifications(event_key) ON DELETE CASCADE
                )""",
                "CREATE INDEX IF NOT EXISTS idx_notification_summary_pending "
                "ON notification_summary_deliveries(delivered_at, uncertain_at, claimed_at, next_attempt_at, created_at)",
                """CREATE TABLE IF NOT EXISTS notification_judgments (
                    event_key TEXT PRIMARY KEY,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    notification_reason TEXT NOT NULL,
                    decision_reason TEXT NOT NULL DEFAULT '',
                    matched_request TEXT NOT NULL DEFAULT '',
                    user_request TEXT NOT NULL DEFAULT '',
                    task_state TEXT NOT NULL DEFAULT '',
                    recent_successful_notifications_json TEXT NOT NULL DEFAULT '[]',
                    new_facts_json TEXT NOT NULL DEFAULT '[]',
                    model_error TEXT NOT NULL DEFAULT '',
                    model_attempts INTEGER NOT NULL DEFAULT 0,
                    input_digest TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(event_key) REFERENCES notifications(event_key) ON DELETE CASCADE
                )""",
                "CREATE INDEX IF NOT EXISTS idx_notification_judgments_updated "
                "ON notification_judgments(updated_at, event_key)",
                """CREATE TABLE IF NOT EXISTS notification_raw_bindings (
                    event_key TEXT PRIMARY KEY,
                    sender_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    finalized_at INTEGER,
                    FOREIGN KEY(event_key) REFERENCES notifications(event_key) ON DELETE CASCADE,
                    CHECK(length(sender_id) > 0),
                    CHECK(length(thread_id) > 0),
                    CHECK(length(turn_id) > 0),
                    CHECK(length(content_sha256) = 64)
                )""",
                """CREATE TABLE IF NOT EXISTS notification_raw_contexts (
                    message_id TEXT PRIMARY KEY,
                    event_key TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    CHECK(length(sender_id) > 0),
                    CHECK(length(chat_id) > 0),
                    CHECK(length(thread_id) > 0),
                    CHECK(length(turn_id) > 0),
                    CHECK(length(content_sha256) = 64)
                )""",
                "CREATE INDEX IF NOT EXISTS idx_notification_raw_context_turn "
                "ON notification_raw_contexts(thread_id, turn_id)",
                """CREATE TABLE IF NOT EXISTS notification_raw_deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    inbound_message_id TEXT NOT NULL UNIQUE,
                    parent_message_id TEXT NOT NULL,
                    event_key TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    next_attempt_at INTEGER NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    claimed_at INTEGER,
                    response_kind TEXT NOT NULL DEFAULT '',
                    prepared_at INTEGER,
                    submitted_at INTEGER,
                    delivered_at INTEGER,
                    rejected_at INTEGER,
                    uncertain_at INTEGER,
                    result_message_ids_json TEXT NOT NULL DEFAULT '[]',
                    last_error_code TEXT,
                    CHECK(length(inbound_message_id) > 0),
                    CHECK(length(parent_message_id) > 0),
                    CHECK(length(sender_id) > 0),
                    CHECK(length(chat_id) > 0),
                    CHECK(length(thread_id) > 0),
                    CHECK(length(turn_id) > 0),
                    CHECK(length(content_sha256) = 64)
                )""",
                "CREATE INDEX IF NOT EXISTS idx_notification_raw_pending "
                "ON notification_raw_deliveries(delivered_at, uncertain_at, claimed_at, next_attempt_at, created_at)",
                """CREATE TABLE IF NOT EXISTS notification_media_deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    event_key TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    item_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    file_name TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    next_attempt_at INTEGER NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    claimed_at INTEGER,
                    delivered_at INTEGER,
                    rejected_at INTEGER,
                    uncertain_at INTEGER,
                    discarded_at INTEGER,
                    channel_message_id TEXT,
                    last_error_code TEXT,
                    warning_sent_at INTEGER,
                    FOREIGN KEY(event_key) REFERENCES notifications(event_key) ON DELETE CASCADE,
                    UNIQUE(event_key, item_id, sha256),
                    UNIQUE(event_key, ordinal)
                )""",
                "CREATE INDEX IF NOT EXISTS idx_notification_media_pending "
                "ON notification_media_deliveries(delivered_at, uncertain_at, discarded_at, next_attempt_at, created_at, ordinal)",
                "CREATE INDEX IF NOT EXISTS idx_notification_media_event "
                "ON notification_media_deliveries(event_key, ordinal)",
                """CREATE TABLE IF NOT EXISTS reply_deliveries (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    delivery_id TEXT NOT NULL UNIQUE,
                    parent_code TEXT NOT NULL,
                    inbound_message_id TEXT NOT NULL,
                    reply_fingerprint TEXT NOT NULL,
                    reply_text TEXT,
                    created_at INTEGER NOT NULL,
                    claimed_at INTEGER,
                    delivered_at INTEGER,
                    discarded_at INTEGER,
                    receipt_required INTEGER NOT NULL DEFAULT 0,
                    receipt_sent_at INTEGER,
                    FOREIGN KEY(parent_code) REFERENCES notifications(code) ON DELETE CASCADE,
                    UNIQUE(parent_code, inbound_message_id)
                )""",
                "CREATE INDEX IF NOT EXISTS idx_reply_delivery_parent ON reply_deliveries(parent_code, sequence)",
                "CREATE INDEX IF NOT EXISTS idx_reply_delivery_pending ON reply_deliveries(claimed_at, delivered_at, discarded_at, sequence)",
                """CREATE TABLE IF NOT EXISTS management_contexts (
                    context_id TEXT PRIMARY KEY,
                    context_kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    sender_id TEXT NOT NULL DEFAULT '',
                    chat_id TEXT NOT NULL DEFAULT ''
                )""",
                # schema20：每个 owner 的 p2p 私聊最多保留一个当前 Codex
                # 会话。它只保存不可逆路由所需的 thread_id，不复制管理卡片
                # payload、cwd、技能路径或用户正文；目标是否仍可见由每次
                # slash/绑定操作重新读取当前 Codex 目录确认。
                """CREATE TABLE IF NOT EXISTS management_current_bindings (
                    sender_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    display_title TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    PRIMARY KEY(sender_id, chat_id),
                    CHECK(length(sender_id) > 0),
                    CHECK(length(chat_id) > 0),
                    CHECK(length(thread_id) > 0)
                )""",
                """CREATE TABLE IF NOT EXISTS management_message_ids (
                    message_id TEXT PRIMARY KEY,
                    context_id TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(context_id) REFERENCES management_contexts(context_id) ON DELETE CASCADE
                )""",
                """CREATE TABLE IF NOT EXISTS management_inbound_messages (
                    message_id TEXT PRIMARY KEY,
                    sender_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    completed_at INTEGER
                )""",
                "CREATE INDEX IF NOT EXISTS idx_management_context_expiry ON management_contexts(expires_at)",
                "CREATE INDEX IF NOT EXISTS idx_management_message_context ON management_message_ids(context_id)",
                """CREATE TABLE IF NOT EXISTS management_context_actions (
                    context_id TEXT NOT NULL,
                    action TEXT NOT NULL CHECK(action IN ('raw', 'archive')),
                    inbound_message_id TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    claimed_at INTEGER,
                    submitted_at INTEGER,
                    succeeded_at INTEGER,
                    rejected_at INTEGER,
                    uncertain_at INTEGER,
                    result_message_ids_json TEXT,
                    last_error_code TEXT,
                    PRIMARY KEY(context_id, action),
                    FOREIGN KEY(context_id) REFERENCES management_contexts(context_id) ON DELETE CASCADE
                )""",
                "CREATE INDEX IF NOT EXISTS idx_management_action_state "
                "ON management_context_actions(succeeded_at, uncertain_at, claimed_at, updated_at)",
                # schema19：远程 Codex 写动作与 legacy ``raw/archive`` 完全隔离。
                # request_hash 是请求内容的不可逆幂等键，不在状态库保存原始目标、
                # prompt、cwd 或技能路径。
                """CREATE TABLE IF NOT EXISTS remote_control_actions (
                    context_id TEXT NOT NULL,
                    action TEXT NOT NULL CHECK(action IN (
                        'goal_set', 'goal_clear', 'plan_start', 'skill_start',
                        'compact_start', 'fork_start', 'review_start',
                        'feedback_upload', 'model_set', 'personality_set',
                        'reasoning_set', 'fast_toggle', 'memories_set',
                        'monitor_auto_set'
                    )),
                    request_hash TEXT NOT NULL CHECK(
                        length(request_hash)=64
                        AND request_hash NOT GLOB '*[^0-9a-f]*'
                    ),
                    inbound_message_id TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    claimed_at INTEGER,
                    submitted_at INTEGER,
                    succeeded_at INTEGER,
                    rejected_at INTEGER,
                    uncertain_at INTEGER,
                    result_json TEXT,
                    last_error_code TEXT,
                    PRIMARY KEY(context_id, action, request_hash),
                    FOREIGN KEY(context_id) REFERENCES management_contexts(context_id)
                        ON DELETE CASCADE
                )""",
                "CREATE INDEX IF NOT EXISTS idx_remote_control_action_state "
                "ON remote_control_actions(succeeded_at, uncertain_at, claimed_at, updated_at)",
                """CREATE TABLE IF NOT EXISTS staged_image_replies (
                    sender_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    reply_to_message_id TEXT NOT NULL,
                    attachments_json TEXT NOT NULL,
                    source_message_ids_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    PRIMARY KEY(sender_id, chat_id)
                )""",
                "CREATE INDEX IF NOT EXISTS idx_staged_image_expiry ON staged_image_replies(expires_at)",
                """CREATE TABLE IF NOT EXISTS monitor_subscriptions (
                    thread_id TEXT PRIMARY KEY,
                    origin TEXT NOT NULL CHECK(origin IN ('manual', 'auto')),
                    added_at INTEGER NOT NULL,
                    last_activity_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL
                )""",
                """CREATE TABLE IF NOT EXISTS monitor_suppressions (
                    thread_id TEXT PRIMARY KEY,
                    removed_at INTEGER NOT NULL
                )""",
                "CREATE INDEX IF NOT EXISTS idx_monitor_expiry ON monitor_subscriptions(origin, expires_at)",
                """CREATE TABLE IF NOT EXISTS session_search_cache (
                    thread_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    latest_turn_id TEXT NOT NULL,
                    description TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    last_result TEXT NOT NULL,
                    last_activity_at INTEGER,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY(thread_id, content_hash)
                )""",
                "CREATE INDEX IF NOT EXISTS idx_session_search_cache_thread ON session_search_cache(thread_id, updated_at)",
                """CREATE TABLE IF NOT EXISTS session_search_judgments (
                    query_hash TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    score REAL NOT NULL,
                    confidence TEXT NOT NULL,
                    classification TEXT NOT NULL,
                    display_title TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY(query_hash, thread_id, content_hash)
                )""",
                "CREATE INDEX IF NOT EXISTS idx_session_search_judgment_query ON session_search_judgments(query_hash, updated_at)",
                """CREATE TABLE IF NOT EXISTS thread_title_recoveries (
                    thread_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    display_title TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY(thread_id, content_hash)
                )""",
                "CREATE INDEX IF NOT EXISTS idx_thread_title_recovery_thread ON thread_title_recoveries(thread_id, updated_at)",
                """CREATE TABLE IF NOT EXISTS reset_alert_state (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
                    bootstrap_completed_at INTEGER,
                    last_attempt_at INTEGER,
                    last_success_at INTEGER,
                    next_check_at INTEGER,
                    window_start_at INTEGER,
                    window_end_at INTEGER,
                    run_slot_at INTEGER,
                    last_completed_slot_at INTEGER,
                    last_run_status TEXT NOT NULL DEFAULT 'never',
                    last_error_code TEXT,
                    worker_started_at INTEGER,
                    worker_heartbeat_at INTEGER,
                    worker_stopped_at INTEGER,
                    updated_at INTEGER NOT NULL
                )""",
                """CREATE TABLE IF NOT EXISTS reset_alert_sources (
                    source_id TEXT PRIMARY KEY,
                    cursor_json TEXT NOT NULL DEFAULT '{}',
                    last_attempt_at INTEGER,
                    last_success_at INTEGER,
                    last_item_at INTEGER,
                    baseline_completed_at INTEGER,
                    health TEXT NOT NULL DEFAULT 'never',
                    last_error_code TEXT,
                    payload_hash TEXT,
                    updated_at INTEGER NOT NULL
                )""",
                """CREATE TABLE IF NOT EXISTS reset_alert_signals (
                    signal_key TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    source_item_id TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    published_at INTEGER NOT NULL,
                    observed_at INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    signal_kind TEXT NOT NULL,
                    is_official INTEGER NOT NULL CHECK(is_official IN (0,1)),
                    payload_json TEXT NOT NULL,
                    UNIQUE(source_id, source_item_id, content_hash)
                )""",
                "CREATE INDEX IF NOT EXISTS idx_reset_alert_signal_time ON reset_alert_signals(published_at, source_id)",
                """CREATE TABLE IF NOT EXISTS reset_alert_events (
                    event_key TEXT PRIMARY KEY,
                    level TEXT NOT NULL CHECK(level IN ('A','B')),
                    evidence TEXT NOT NULL,
                    window_text TEXT NOT NULL,
                    advice TEXT NOT NULL,
                    source_ids_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    fingerprint TEXT NOT NULL UNIQUE,
                    notified_at INTEGER
                )""",
                "CREATE INDEX IF NOT EXISTS idx_reset_alert_event_time ON reset_alert_events(created_at, level)",
                """CREATE TABLE IF NOT EXISTS reset_alert_deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    event_key TEXT NOT NULL UNIQUE,
                    message_text TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    next_attempt_at INTEGER NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    claimed_at INTEGER,
                    submitted_at INTEGER,
                    delivered_at INTEGER,
                    rejected_at INTEGER,
                    uncertain_at INTEGER,
                    expired_at INTEGER,
                    channel_message_ids_json TEXT NOT NULL DEFAULT '[]',
                    last_error_code TEXT,
                    FOREIGN KEY(event_key) REFERENCES reset_alert_events(event_key) ON DELETE CASCADE
                )""",
                "CREATE INDEX IF NOT EXISTS idx_reset_alert_delivery_pending ON reset_alert_deliveries(delivered_at, uncertain_at, claimed_at, next_attempt_at, created_at)",
                *USER_REPLY_CHAIN_SCHEMA_SQL,
                *ARTIFACT_SCHEMA_SQL,
            )
            for statement in schema_statements:
                self._connection.execute(statement)
            columns = {
                str(row[1])
                for row in self._connection.execute("PRAGMA table_info(notifications)")
            }
            if "reply_kind" not in columns:
                # v1 → v2：旧通知都代表“开始新轮次”，可无损赋默认值。
                self._connection.execute(
                    "ALTER TABLE notifications ADD COLUMN reply_kind TEXT NOT NULL DEFAULT 'turn'"
                )
            for column, declaration in (
                ("reply_text", "TEXT"),
                ("claimed_at", "INTEGER"),
                ("delivered_at", "INTEGER"),
                ("channel_message_id", "TEXT"),
                ("discarded_at", "INTEGER"),
            ):
                if column not in columns:
                    self._connection.execute(
                        f"ALTER TABLE notifications ADD COLUMN {column} {declaration}"
                    )
            judgment_columns = {
                str(row[1])
                for row in self._connection.execute(
                    "PRAGMA table_info(session_search_judgments)"
                )
            }
            if "display_title" not in judgment_columns:
                # v11 → v12：旧评分没有模型生成的展示名。保留旧记录但填空；
                # 读取时把空展示名视为缓存未命中，在下一次正常搜索同批重评。
                self._connection.execute(
                    "ALTER TABLE session_search_judgments "
                    "ADD COLUMN display_title TEXT NOT NULL DEFAULT ''"
                )
            management_context_columns = {
                str(row[1])
                for row in self._connection.execute(
                    "PRAGMA table_info(management_contexts)"
                )
            }
            for column in ("sender_id", "chat_id"):
                if column not in management_context_columns:
                    self._connection.execute(
                        f"ALTER TABLE management_contexts ADD COLUMN {column} "
                        "TEXT NOT NULL DEFAULT ''"
                    )
            management_action_columns = {
                str(row[1])
                for row in self._connection.execute(
                    "PRAGMA table_info(management_context_actions)"
                )
            }
            if "submitted_at" not in management_action_columns:
                self._connection.execute(
                    "ALTER TABLE management_context_actions ADD COLUMN submitted_at INTEGER"
                )
            # schema20 was initially deployed with only the routing identity
            # and timestamps.  Complete that forward-compatible shape in
            # place when such a database is reopened: existing bindings keep
            # their identity and remain valid until the explicit expiry
            # column can be managed by the new API.
            binding_columns = {
                str(row[1])
                for row in self._connection.execute(
                    "PRAGMA table_info(management_current_bindings)"
                )
            }
            if "display_title" not in binding_columns:
                self._connection.execute(
                    "ALTER TABLE management_current_bindings ADD COLUMN "
                    "display_title TEXT NOT NULL DEFAULT ''"
                )
            if "expires_at" not in binding_columns:
                self._connection.execute(
                    "ALTER TABLE management_current_bindings ADD COLUMN "
                    f"expires_at INTEGER NOT NULL DEFAULT {_PERMANENT_EXPIRY}"
                )
            required_columns = {
                "hook_events": {"event_key", "payload_json", "created_at", "consumed_at"},
                "notifications": {
                    "event_key", "code", "thread_id", "turn_id", "reply_kind",
                    "message_text", "created_at", "expires_at", "sent_at", "consumed_at",
                    "reply_fingerprint", "reply_text", "claimed_at", "delivered_at",
                    "channel_message_id", "discarded_at",
                },
                "processed_turns": {"event_key", "processed_at"},
                "notification_message_ids": {"message_id", "event_key", "created_at"},
                "notification_summary_deliveries": {
                    "event_key", "created_at", "next_attempt_at", "attempt_count",
                    "claimed_at", "message_text", "prepared_at", "submitted_at",
                    "delivered_at", "rejected_at", "uncertain_at",
                    "channel_message_ids_json", "last_error",
                },
                "notification_judgments": {
                    "event_key", "created_at", "updated_at", "status",
                    "notification_reason", "decision_reason", "matched_request",
                    "user_request", "task_state",
                    "recent_successful_notifications_json", "new_facts_json",
                    "model_error", "model_attempts", "input_digest",
                },
                "notification_raw_bindings": {
                    "event_key", "sender_id", "thread_id", "turn_id",
                    "content_sha256", "created_at", "finalized_at",
                },
                "notification_raw_contexts": {
                    "message_id", "event_key", "sender_id", "chat_id", "thread_id", "turn_id",
                    "content_sha256", "created_at",
                },
                "notification_raw_deliveries": {
                    "delivery_id", "inbound_message_id", "parent_message_id", "event_key",
                    "sender_id", "chat_id", "thread_id", "turn_id",
                    "content_sha256", "fingerprint", "created_at", "next_attempt_at",
                    "attempt_count", "claimed_at", "response_kind", "prepared_at",
                    "submitted_at", "delivered_at", "rejected_at", "uncertain_at",
                    "result_message_ids_json", "last_error_code",
                },
                "notification_media_deliveries": {
                    "delivery_id", "event_key", "ordinal", "item_id", "path",
                    "mime_type", "sha256", "size", "file_name", "created_at",
                    "next_attempt_at", "attempt_count", "claimed_at", "delivered_at",
                    "rejected_at", "uncertain_at", "discarded_at", "channel_message_id",
                    "last_error_code", "warning_sent_at",
                },
                "reply_deliveries": {
                    "sequence", "delivery_id", "parent_code", "inbound_message_id",
                    "reply_fingerprint", "reply_text", "created_at", "claimed_at",
                    "delivered_at", "discarded_at", "receipt_required", "receipt_sent_at",
                },
                "management_contexts": {
                    "context_id", "context_kind", "payload_json", "created_at", "expires_at",
                    "sender_id", "chat_id",
                },
                "management_current_bindings": {
                    "sender_id", "chat_id", "thread_id", "display_title", "created_at",
                    "updated_at", "expires_at",
                },
                "management_message_ids": {"message_id", "context_id", "created_at"},
                "management_context_actions": {
                    "context_id", "action", "inbound_message_id", "created_at",
                    "updated_at", "attempt_count", "claimed_at", "succeeded_at",
                    "submitted_at",
                    "rejected_at", "uncertain_at", "result_message_ids_json",
                    "last_error_code",
                },
                "remote_control_actions": {
                    "context_id", "action", "request_hash", "inbound_message_id",
                    "created_at", "updated_at", "attempt_count", "claimed_at",
                    "submitted_at", "succeeded_at", "rejected_at", "uncertain_at",
                    "result_json", "last_error_code",
                },
                "management_inbound_messages": {
                    "message_id", "sender_id", "content_hash", "created_at", "completed_at",
                },
                "staged_image_replies": {
                    "sender_id", "chat_id", "reply_to_message_id", "attachments_json",
                    "source_message_ids_json", "created_at", "expires_at",
                },
                "monitor_subscriptions": {
                    "thread_id", "origin", "added_at", "last_activity_at", "expires_at",
                },
                "monitor_suppressions": {"thread_id", "removed_at"},
                "session_search_cache": {
                    "thread_id", "content_hash", "latest_turn_id", "description",
                    "evidence_json", "last_result", "last_activity_at", "updated_at",
                },
                "session_search_judgments": {
                    "query_hash", "thread_id", "content_hash", "score", "confidence",
                    "classification", "display_title", "reason", "updated_at",
                },
                "thread_title_recoveries": {
                    "thread_id", "content_hash", "display_title", "source",
                    "created_at", "updated_at",
                },
                "reset_alert_state": {
                    "singleton", "enabled", "bootstrap_completed_at", "last_attempt_at",
                    "last_success_at", "next_check_at", "window_start_at", "window_end_at",
                    "run_slot_at", "last_completed_slot_at", "last_run_status",
                    "last_error_code", "worker_started_at", "worker_heartbeat_at",
                    "worker_stopped_at", "updated_at",
                },
                "reset_alert_sources": {
                    "source_id", "cursor_json", "last_attempt_at", "last_success_at",
                    "last_item_at", "baseline_completed_at", "health", "last_error_code",
                    "payload_hash", "updated_at",
                },
                "reset_alert_signals": {
                    "signal_key", "source_id", "source_item_id", "source_url",
                    "published_at", "observed_at", "content_hash", "signal_kind",
                    "is_official", "payload_json",
                },
                "reset_alert_events": {
                    "event_key", "level", "evidence", "window_text", "advice",
                    "source_ids_json", "created_at", "expires_at", "fingerprint",
                    "notified_at",
                },
                "reset_alert_deliveries": {
                    "delivery_id", "event_key", "message_text", "created_at",
                    "next_attempt_at", "attempt_count", "claimed_at", "submitted_at",
                    "delivered_at", "rejected_at", "uncertain_at",
                    "expired_at", "channel_message_ids_json", "last_error_code",
                },
                "user_reply_chain_messages": USER_REPLY_CHAIN_COLUMNS,
            }
            for table, required in required_columns.items():
                actual = {
                    str(row[1])
                    for row in self._connection.execute(f"PRAGMA table_info({table})")
                }
                missing = required - actual
                if missing:
                    raise StateError(f"状态库表 {table} 缺少字段：{', '.join(sorted(missing))}")
            self._connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_notifications_channel_message "
                "ON notifications(channel_message_id) WHERE channel_message_id IS NOT NULL"
            )
            # v4 → v5：把旧单消息 ID 无损迁入一对多关联表；保留原列作为
            # 首分片兼容字段，旧版本若回滚仍能识别单分片通知。
            self._connection.execute(
                """
                INSERT OR IGNORE INTO notification_message_ids(message_id, event_key, created_at)
                SELECT channel_message_id, event_key, created_at FROM notifications
                WHERE channel_message_id IS NOT NULL
                """
            )
            if existing_version < 13:
                # v12 -> v13：父通知只负责稳定路由，普通 turn 回复迁入独立子投递。
                # 原时间与不确定状态逐字段保留；历史 delivered 不补发新回执。
                legacy_rows = self._connection.execute(
                    """
                    SELECT code, consumed_at, reply_fingerprint, reply_text,
                           claimed_at, delivered_at, discarded_at
                    FROM notifications
                    WHERE reply_kind='turn' AND consumed_at IS NOT NULL
                    ORDER BY consumed_at, code
                    """
                ).fetchall()
                for row in legacy_rows:
                    parent_code = str(row["code"])
                    digest = hashlib.sha256(
                        f"legacy-reply-v1\0{parent_code}".encode("utf-8")
                    ).hexdigest()
                    fingerprint = str(row["reply_fingerprint"] or f"legacy:{digest}")
                    delivered_at = row["delivered_at"]
                    self._connection.execute(
                        """
                        INSERT OR IGNORE INTO reply_deliveries(
                            delivery_id, parent_code, inbound_message_id,
                            reply_fingerprint, reply_text, created_at, claimed_at,
                            delivered_at, discarded_at, receipt_required, receipt_sent_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            f"legacy-{digest}",
                            parent_code,
                            f"legacy-{digest}",
                            fingerprint,
                            row["reply_text"],
                            int(row["consumed_at"]),
                            row["claimed_at"],
                            delivered_at,
                            row["discarded_at"],
                            0,
                            delivered_at,
                        ),
                    )
            if existing_version < 8:
                # v7 → v8：管理导航必须支持回复任意历史机器人消息；普通进度
                # 通知仍保持原有时效，只把管理上下文迁成永久哨兵。
                self._connection.execute(
                    "UPDATE management_contexts SET expires_at=?",
                    (_PERMANENT_EXPIRY,),
                )
            self._connection.execute(
                """
                INSERT OR IGNORE INTO reset_alert_state(
                    singleton, enabled, last_run_status, updated_at
                ) VALUES(1, 1, 'never', ?)
                """,
                (int(time.time()),),
            )
            self._connection.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @staticmethod
    def _monitor_thread_id(thread_id: object) -> str:
        normalized = str(thread_id or "").strip()
        if not normalized or len(normalized) > 512:
            raise ValueError("thread_id 不能为空且不得超过 512 字符")
        return normalized

    def add_manual_monitor(
        self,
        thread_id: str,
        *,
        last_activity_at: int | None = None,
        now: int | None = None,
    ) -> None:
        """明确添加或提升为永久手动监测，并解除此前的用户抑制。"""

        normalized = self._monitor_thread_id(thread_id)
        timestamp = int(time.time()) if now is None else int(now)
        activity = timestamp if last_activity_at is None else int(last_activity_at)
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM monitor_suppressions WHERE thread_id=?", (normalized,)
            )
            self._connection.execute(
                """
                INSERT INTO monitor_subscriptions(
                    thread_id, origin, added_at, last_activity_at, expires_at
                ) VALUES(?, 'manual', ?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    origin='manual',
                    last_activity_at=MAX(monitor_subscriptions.last_activity_at, excluded.last_activity_at),
                    expires_at=excluded.expires_at
                """,
                (normalized, timestamp, activity, _PERMANENT_EXPIRY),
            )

    def ensure_legacy_manual_monitor(
        self,
        thread_id: str,
        *,
        last_activity_at: int | None = None,
        now: int | None = None,
    ) -> bool:
        """把旧 YAML 选择器迁为手动监测；明确移除后的抑制拥有更高优先级。"""

        normalized = self._monitor_thread_id(thread_id)
        timestamp = int(time.time()) if now is None else int(now)
        activity = timestamp if last_activity_at is None else int(last_activity_at)
        with self._lock, self._connection:
            suppressed = self._connection.execute(
                "SELECT 1 FROM monitor_suppressions WHERE thread_id=?", (normalized,)
            ).fetchone()
            if suppressed is not None:
                return False
            before = self._connection.total_changes
            self._connection.execute(
                """
                INSERT INTO monitor_subscriptions(
                    thread_id, origin, added_at, last_activity_at, expires_at
                ) VALUES(?, 'manual', ?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    origin='manual',
                    last_activity_at=MAX(monitor_subscriptions.last_activity_at, excluded.last_activity_at),
                    expires_at=excluded.expires_at
                """,
                (normalized, timestamp, activity, _PERMANENT_EXPIRY),
            )
            return self._connection.total_changes > before

    def _auto_monitoring_settings_locked(self) -> tuple[bool, int | None]:
        rows = self._connection.execute(
            "SELECT key, value FROM meta WHERE key IN (?, ?)",
            (_AUTO_MONITORING_ENABLED_KEY, _AUTO_MONITORING_EFFECTIVE_AT_KEY),
        ).fetchall()
        values = {str(row["key"]): str(row["value"]) for row in rows}
        raw_enabled = values.get(_AUTO_MONITORING_ENABLED_KEY)
        if raw_enabled is None:
            enabled = True
        elif raw_enabled == "1":
            enabled = True
        elif raw_enabled == "0":
            enabled = False
        else:
            raise StateError("自动监测开关状态无效")
        raw_effective_at = values.get(_AUTO_MONITORING_EFFECTIVE_AT_KEY)
        if raw_effective_at is None:
            effective_at = None
        else:
            try:
                effective_at = int(raw_effective_at)
            except (TypeError, ValueError) as exc:
                raise StateError("自动监测开关生效时间无效") from exc
            if effective_at < 0:
                raise StateError("自动监测开关生效时间无效")
        return enabled, effective_at

    def auto_monitoring_settings(self) -> dict[str, bool | int | None]:
        """返回自动监测全局开关；旧数据库默认保持开启。"""

        with self._lock:
            enabled, effective_at = self._auto_monitoring_settings_locked()
        return {
            "auto_monitoring_enabled": enabled,
            "effective_at": effective_at,
        }

    def set_auto_monitoring_enabled(
        self,
        enabled: bool,
        *,
        now: int | None = None,
    ) -> dict[str, bool | int | None]:
        """原子设置自动发现开关，并返回是否发生实际变化。"""

        if not isinstance(enabled, bool):
            raise ValueError("enabled 必须是布尔值")
        timestamp = int(time.time()) if now is None else int(now)
        if timestamp < 0:
            raise ValueError("生效时间无效")
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                current, effective_at = self._auto_monitoring_settings_locked()
                changed = current != enabled
                if changed:
                    self._connection.execute(
                        """
                        INSERT INTO meta(key, value) VALUES(?, ?)
                        ON CONFLICT(key) DO UPDATE SET value=excluded.value
                        """,
                        (_AUTO_MONITORING_ENABLED_KEY, "1" if enabled else "0"),
                    )
                    self._connection.execute(
                        """
                        INSERT INTO meta(key, value) VALUES(?, ?)
                        ON CONFLICT(key) DO UPDATE SET value=excluded.value
                        """,
                        (_AUTO_MONITORING_EFFECTIVE_AT_KEY, str(timestamp)),
                    )
                    effective_at = timestamp
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
        return {
            "auto_monitoring_enabled": enabled,
            "changed": changed,
            "effective_at": effective_at,
        }

    def discover_auto_monitor(
        self,
        thread_id: str,
        *,
        last_activity_at: int,
        now: int | None = None,
        ttl_seconds: int = 86_400,
    ) -> bool:
        """发现最近活跃任务；关闭时只刷新已有手动项的活动时间。"""

        normalized = self._monitor_thread_id(thread_id)
        timestamp = int(time.time()) if now is None else int(now)
        activity = int(last_activity_at)
        if ttl_seconds < 60:
            raise ValueError("自动监测 TTL 不得少于 60 秒")
        if activity + int(ttl_seconds) < timestamp:
            return False
        with self._lock, self._connection:
            auto_enabled, _effective_at = self._auto_monitoring_settings_locked()
            if not auto_enabled:
                existing = self._connection.execute(
                    "SELECT origin, last_activity_at FROM monitor_subscriptions WHERE thread_id=?",
                    (normalized,),
                ).fetchone()
                if existing is not None and str(existing["origin"]) == "manual":
                    latest = max(int(existing["last_activity_at"]), activity)
                    self._connection.execute(
                        "UPDATE monitor_subscriptions SET last_activity_at=? WHERE thread_id=?",
                        (latest, normalized),
                    )
                # 已有 auto 项不删除、不续期；由原 expires_at 自然退出。
                return False
            suppressed = self._connection.execute(
                "SELECT 1 FROM monitor_suppressions WHERE thread_id=?", (normalized,)
            ).fetchone()
            if suppressed is not None:
                return False
            existing = self._connection.execute(
                "SELECT origin, last_activity_at FROM monitor_subscriptions WHERE thread_id=?",
                (normalized,),
            ).fetchone()
            if existing is None:
                self._connection.execute(
                    """
                    INSERT INTO monitor_subscriptions(
                        thread_id, origin, added_at, last_activity_at, expires_at
                    ) VALUES(?, 'auto', ?, ?, ?)
                    """,
                    (normalized, timestamp, activity, activity + int(ttl_seconds)),
                )
                return True
            latest = max(int(existing["last_activity_at"]), activity)
            if str(existing["origin"]) == "manual":
                self._connection.execute(
                    "UPDATE monitor_subscriptions SET last_activity_at=? WHERE thread_id=?",
                    (latest, normalized),
                )
            else:
                self._connection.execute(
                    """
                    UPDATE monitor_subscriptions
                    SET last_activity_at=?, expires_at=? WHERE thread_id=?
                    """,
                    (latest, latest + int(ttl_seconds), normalized),
                )
            return False

    def remove_monitor(self, thread_id: str, *, now: int | None = None) -> bool:
        """明确移除监测并永久抑制自动发现，直至用户再次手动添加。"""

        normalized = self._monitor_thread_id(thread_id)
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "DELETE FROM monitor_subscriptions WHERE thread_id=?", (normalized,)
            )
            self._connection.execute(
                """
                INSERT INTO monitor_suppressions(thread_id, removed_at) VALUES(?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET removed_at=excluded.removed_at
                """,
                (normalized, timestamp),
            )
        return cursor.rowcount == 1

    def monitor_subscriptions(
        self, *, now: int | None = None
    ) -> list[dict[str, int | str | None]]:
        """返回当前有效监测。

        正式服务沿用读取时清理到期自动项；只读诊断不能把“读取列表”变成
        写事务，因此在 ``mode=ro`` 下仅用 WHERE 排除到期项，并把清理留给
        正式服务或明确的维护命令。
        """

        timestamp = int(time.time()) if now is None else int(now)
        with self._lock:
            if self.read_only:
                rows = self._connection.execute(
                    """
                    SELECT thread_id, origin, added_at, last_activity_at, expires_at
                    FROM monitor_subscriptions
                    WHERE origin!='auto' OR expires_at>=?
                    ORDER BY last_activity_at DESC, thread_id
                    """,
                    (timestamp,),
                ).fetchall()
            else:
                with self._connection:
                    self._connection.execute(
                        "DELETE FROM monitor_subscriptions WHERE origin='auto' AND expires_at<?",
                        (timestamp,),
                    )
                    rows = self._connection.execute(
                        """
                        SELECT thread_id, origin, added_at, last_activity_at, expires_at
                        FROM monitor_subscriptions
                        ORDER BY last_activity_at DESC, thread_id
                        """
                    ).fetchall()
        return [
            {
                "thread_id": str(row["thread_id"]),
                "origin": str(row["origin"]),
                "added_at": int(row["added_at"]),
                "last_activity_at": int(row["last_activity_at"]),
                "expires_at": (
                    None if int(row["expires_at"]) == _PERMANENT_EXPIRY
                    else int(row["expires_at"])
                ),
            }
            for row in rows
        ]

    def monitor_bootstrap_complete(self) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM meta WHERE key='monitor_registry_bootstrap_v1'"
            ).fetchone()
        return row is not None and str(row[0]) == "complete"

    def mark_monitor_bootstrap_complete(self) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT OR REPLACE INTO meta(key, value)
                VALUES('monitor_registry_bootstrap_v1', 'complete')
                """
            )

    def create_management_context(
        self,
        context_kind: str,
        payload: Mapping[str, Any],
        *,
        sender_id: str = "",
        chat_id: str = "",
        ttl_days: int | None = None,
        now: int | None = None,
    ) -> str:
        """保存一份不可变飞书导航上下文，待出站 message_id 返回后再绑定。"""

        kind = str(context_kind or "").strip()
        if not kind:
            raise ValueError("context_kind 不能为空")
        if ttl_days is not None and not 1 <= int(ttl_days) <= 365:
            raise ValueError("ttl_days 必须介于 1 和 365，或使用 None 表示永久")
        timestamp = int(time.time()) if now is None else int(now)
        expires_at = (
            _PERMANENT_EXPIRY
            if ttl_days is None
            else timestamp + int(ttl_days) * 86400
        )
        context_id = uuid.uuid4().hex
        encoded = json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":"))
        owner_sender = str(sender_id or "").strip()
        owner_chat = str(chat_id or "").strip()
        if len(owner_sender) > 1024 or len(owner_chat) > 1024:
            raise ValueError("管理上下文所有者标识过长")
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO management_contexts(
                    context_id, context_kind, payload_json, created_at, expires_at,
                    sender_id, chat_id
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    context_id,
                    kind,
                    encoded,
                    timestamp,
                    expires_at,
                    owner_sender,
                    owner_chat,
                ),
            )
        return context_id

    def bind_management_messages(
        self,
        context_id: str,
        message_ids: Iterable[str],
        *,
        now: int | None = None,
    ) -> None:
        """把一次响应的所有飞书分片都绑定到同一不可变上下文。"""

        normalized = tuple(dict.fromkeys(str(item or "").strip() for item in message_ids))
        if not normalized or any(not item for item in normalized):
            raise ValueError("message_ids 不能为空且不能包含空值")
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
            exists = self._connection.execute(
                "SELECT 1 FROM management_contexts WHERE context_id=?",
                (context_id,),
            ).fetchone()
            if exists is None:
                raise StateError("管理上下文不存在")
            for message_id in normalized:
                existing = self._connection.execute(
                    "SELECT context_id FROM management_message_ids WHERE message_id=?",
                    (message_id,),
                ).fetchone()
                if existing is not None:
                    if str(existing["context_id"]) != str(context_id):
                        raise StateError("同一飞书 message_id 不能绑定到不同管理上下文")
                    continue
                self._connection.execute(
                    "INSERT INTO management_message_ids(message_id, context_id, created_at) "
                    "VALUES(?, ?, ?)",
                    (message_id, context_id, timestamp),
                )

    def management_context_for_message(
        self,
        message_id: str,
        *,
        now: int | None = None,
    ) -> tuple[str, Mapping[str, Any]] | None:
        """按被回复的飞书 message_id 精确读取仍有效的上下文。"""

        record = self.management_context_record_for_message(message_id, now=now)
        if record is None:
            return None
        return record.context_kind, record.payload

    def management_context_exists_for_message(self, message_id: str) -> bool:
        """判断平台消息是否曾绑定过管理上下文，不受过期时间影响。

        恢复未知摘要只允许处理完全没有本地父映射的引用；过期或旧版本的
        管理卡片也必须留在原有 fail-closed 路径，不能被另一个 uncertain
        摘要的官方正文误绑定。
        """

        normalized = str(message_id or "").strip()
        if not normalized:
            return False
        with self._lock:
            return (
                self._connection.execute(
                    "SELECT 1 FROM management_message_ids WHERE message_id=? LIMIT 1",
                    (normalized,),
                ).fetchone()
                is not None
            )

    def bind_management_context_chat(
        self,
        context_id: str,
        sender_id: str,
        chat_id: str,
    ) -> bool:
        """为菜单创建的 owner 私聊卡片原子补齐首次合法点击的 chat_id。

        只允许已有非空 sender 完全匹配，且 chat 仍为空或已经是同一个值；
        不会把升级前 ownerless 上下文变成可执行卡片上下文。
        """

        context = str(context_id or "").strip()
        sender = str(sender_id or "").strip()
        chat = str(chat_id or "").strip()
        if not context or not sender or not chat:
            return False
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE management_contexts
                SET chat_id=?
                WHERE context_id=? AND sender_id=?
                  AND (chat_id='' OR chat_id=?)
                """,
                (chat, context, sender, chat),
            )
        return cursor.rowcount == 1

    def management_context_record_for_message(
        self,
        message_id: str,
        *,
        now: int | None = None,
    ) -> ManagementContextRecord | None:
        """读取上下文身份、不可变载荷及创建该响应的 sender/chat 约束。"""

        normalized = str(message_id or "").strip()
        if not normalized:
            return None
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock:
            row = self._connection.execute(
                """
                SELECT c.context_id, c.context_kind, c.payload_json,
                       c.sender_id, c.chat_id, c.created_at, c.expires_at
                FROM management_message_ids AS m
                JOIN management_contexts AS c ON c.context_id=m.context_id
                WHERE m.message_id=? AND c.expires_at>=?
                """,
                (normalized, timestamp),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(str(row["payload_json"]))
        except json.JSONDecodeError as exc:
            raise StateError("管理上下文 JSON 损坏") from exc
        if not isinstance(payload, dict):
            raise StateError("管理上下文根节点不是对象")
        return ManagementContextRecord(
            context_id=str(row["context_id"]),
            context_kind=str(row["context_kind"]),
            payload=payload,
            sender_id=str(row["sender_id"] or ""),
            chat_id=str(row["chat_id"] or ""),
            created_at=int(row["created_at"]),
            expires_at=int(row["expires_at"]),
        )

    def claim_management_target_selection(
        self,
        context_id: str,
        *,
        marker: str = "target",
        now: int | None = None,
    ) -> bool:
        """Atomically consume a remote-target selection context once.

        A pending slash command is carried by the target-selector context so
        that selection can continue the original request.  A user can,
        however, click an old card more than once and each click arrives with
        a new inbound message id.  The inbound-message idempotency gate alone
        therefore cannot protect the pending command.  Marking the context
        payload under the store lock makes the selection itself one-shot; an
        already claimed (or expired) selector fails closed without creating a
        second remote-control context or RPC.  ``marker='pending'`` is used
        by a derived project-list chain to consume the original pending
        command exactly once across child contexts.
        """

        self._require_binding_writable()
        context = str(context_id or "").strip()
        if not context or len(context) > 512:
            raise ValueError("context_id 不能为空且不得超过 512 字符")
        if type(marker) is not str:
            raise ValueError("管理目标选择消费标记必须是文本")
        marker_names = {
            "target": "_target_selection_claimed_at",
            "pending": "_pending_command_claimed_at",
            "write": "_write_action_claimed_at",
        }
        claim_field = marker_names.get(marker)
        if claim_field is None:
            raise ValueError("未知的管理目标选择消费标记")
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
            row = self._connection.execute(
                """
                SELECT payload_json, expires_at
                FROM management_contexts
                WHERE context_id=? AND expires_at>=?
                """,
                (context, timestamp),
            ).fetchone()
            if row is None:
                return False
            encoded = str(row["payload_json"])
            try:
                payload = json.loads(encoded)
            except json.JSONDecodeError as exc:
                raise StateError("管理上下文 JSON 损坏") from exc
            if not isinstance(payload, dict):
                raise StateError("管理上下文根节点不是对象")
            if payload.get(claim_field) is not None:
                return False
            payload[claim_field] = timestamp
            updated = self._connection.execute(
                """
                UPDATE management_contexts
                SET payload_json=?
                WHERE context_id=? AND expires_at>=? AND payload_json=?
                """,
                (
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    context,
                    timestamp,
                    encoded,
                ),
            )
        return updated.rowcount == 1

    @staticmethod
    def _binding_owner(sender_id: object, chat_id: object) -> tuple[str, str]:
        sender = str(sender_id or "").strip()
        chat = str(chat_id or "").strip()
        if not sender or not chat:
            raise ValueError("当前会话绑定必须包含 sender_id 和 chat_id")
        if len(sender) > 1024 or len(chat) > 1024:
            raise ValueError("当前会话绑定所有者标识过长")
        return sender, chat

    @staticmethod
    def _binding_thread_id(thread_id: object) -> str:
        normalized = str(thread_id or "").strip()
        if not normalized or len(normalized) > 512:
            raise ValueError("当前会话绑定的 thread_id 不能为空且不得超过 512 字符")
        return normalized

    @staticmethod
    def _safe_binding_title(title: object, thread_id: str) -> str:
        """Return a display-only title, rejecting values that can expose paths.

        The current binding is intentionally a tiny routing record.  A title
        is only a convenience snapshot for a controller; it must never become
        a second source of cwd, rollout path, prompt, or raw thread identity.
        Unsafe values are dropped rather than truncated, so a path cannot be
        partially exposed in a later public response.
        """

        value = " ".join(str(title or "").split()).strip()
        if not value or len(value) > _CURRENT_THREAD_TITLE_MAX_CHARS:
            return ""
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            return ""
        # A slash/backslash or URI scheme is sufficient evidence that this is
        # a path/URI rather than a safe human-facing title.  Keep the check
        # deliberately conservative because this snapshot may be forwarded.
        if any(marker in value for marker in ("/", "\\", "://")):
            return ""
        if re.match(r"^[A-Za-z]:", value) or re.match(r"^\\.+", value):
            return ""
        if thread_id and thread_id in value:
            return ""
        return value

    def _require_binding_writable(self) -> None:
        if self.read_only:
            raise StateError("只读状态库不允许写入当前会话绑定")

    def _binding_table_available(self) -> bool:
        """Return whether this connection may read the schema20 table.

        schema19 read-only callers are intentionally allowed to inspect the
        rest of the state without being forced to create/repair this new
        table.  Even if a future table happens to be present in such a file,
        the older schema version is not authoritative for this feature.
        """

        if not self.read_only:
            return True
        return bool(
            self._read_only_schema_version is not None
            and self._read_only_schema_version >= 20
            and "management_current_bindings" in self._read_only_tables
        )

    def set_current_thread(
        self,
        sender_id: str,
        chat_id: str,
        thread_id: str,
        title: str = "",
        ttl_days: int = _CURRENT_THREAD_BINDING_TTL_DAYS,
        now: int | None = None,
    ) -> None:
        """Atomically replace the current Codex thread for one p2p owner.

        The primary key is exactly ``(sender_id, chat_id)``.  A switch is an
        intentional replacement of the immutable routing value, never a
        lookup by title/path or a fallback to another owner's chat.  Only a
        conservative human-facing title snapshot is retained.
        """

        self._require_binding_writable()
        sender, chat = self._binding_owner(sender_id, chat_id)
        thread = self._binding_thread_id(thread_id)
        if isinstance(ttl_days, bool):
            raise ValueError("ttl_days 必须是整数天数")
        try:
            days = int(ttl_days)
        except (TypeError, ValueError) as exc:
            raise ValueError("ttl_days 必须是整数天数") from exc
        if not 1 <= days <= _CURRENT_THREAD_BINDING_MAX_TTL_DAYS:
            raise ValueError(
                "ttl_days 必须介于 1 和 "
                f"{_CURRENT_THREAD_BINDING_MAX_TTL_DAYS} 之间"
            )
        timestamp = int(time.time()) if now is None else int(now)
        expires_at = timestamp + days * 86_400
        display_title = self._safe_binding_title(title, thread)
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO management_current_bindings(
                    sender_id, chat_id, thread_id, display_title,
                    created_at, updated_at, expires_at
                ) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(sender_id, chat_id) DO UPDATE SET
                    thread_id=excluded.thread_id,
                    display_title=excluded.display_title,
                    updated_at=excluded.updated_at,
                    expires_at=excluded.expires_at
                """,
                (sender, chat, thread, display_title, timestamp, timestamp, expires_at),
            )

    def current_thread(
        self,
        sender_id: str,
        chat_id: str,
        now: int | None = None,
    ) -> Mapping[str, Any] | None:
        """Return the unexpired binding for exactly one owner and p2p chat.

        Expired rows and schema19 read-only databases fail closed as ``None``;
        this method never deletes on read, which keeps diagnostic/CLI paths
        genuinely read-only.
        """

        sender, chat = self._binding_owner(sender_id, chat_id)
        if not self._binding_table_available():
            return None
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock:
            row = self._connection.execute(
                """
                SELECT thread_id, display_title, created_at, updated_at, expires_at
                FROM management_current_bindings
                WHERE sender_id=? AND chat_id=? AND expires_at>?
                """,
                (sender, chat, timestamp),
            ).fetchone()
        if row is None:
            return None
        title = self._safe_binding_title(
            row["display_title"], str(row["thread_id"])
        )
        return {
            "thread_id": str(row["thread_id"]),
            "title": title,
            "created_at": int(row["created_at"]),
            "updated_at": int(row["updated_at"]),
            "expires_at": int(row["expires_at"]),
        }

    def clear_current_thread(
        self,
        sender_id: str,
        chat_id: str,
        *,
        expected_thread_id: str | None = None,
    ) -> bool:
        """Clear exactly one owner's current binding.

        ``expected_thread_id`` is an optional compare-and-delete guard for a
        stale selection card; a mismatching owner/chat/thread never clears a
        newer binding.
        """

        self._require_binding_writable()
        sender, chat = self._binding_owner(sender_id, chat_id)
        parameters: tuple[Any, ...]
        query = "DELETE FROM management_current_bindings WHERE sender_id=? AND chat_id=?"
        parameters = (sender, chat)
        if expected_thread_id is not None:
            thread = self._binding_thread_id(expected_thread_id)
            query += " AND thread_id=?"
            parameters += (thread,)
        with self._lock, self._connection:
            cursor = self._connection.execute(query, parameters)
        return cursor.rowcount == 1

    # Compatibility names used by the first schema20 controller prototype.
    # They intentionally delegate to the same owner/chat/expiry-safe API.
    def set_current_management_binding(
        self,
        sender_id: str,
        chat_id: str,
        thread_id: str,
        *,
        now: int | None = None,
    ) -> None:
        self.set_current_thread(
            sender_id, chat_id, thread_id, ttl_days=_CURRENT_THREAD_BINDING_TTL_DAYS, now=now
        )

    def current_management_binding(
        self,
        sender_id: str,
        chat_id: str,
        *,
        now: int | None = None,
    ) -> Mapping[str, Any] | None:
        """Compatibility getter retaining the original owner fields."""

        sender, chat = self._binding_owner(sender_id, chat_id)
        binding = self.current_thread(sender, chat, now=now)
        if binding is None:
            return None
        return {
            "sender_id": sender,
            "chat_id": chat,
            **dict(binding),
        }

    def clear_current_management_binding(
        self,
        sender_id: str,
        chat_id: str,
        *,
        expected_thread_id: str | None = None,
    ) -> bool:
        return self.clear_current_thread(
            sender_id, chat_id, expected_thread_id=expected_thread_id
        )

    # Names matching the generic upsert/get/clear terminology used in state
    # integrations.  Keep them as thin aliases so there is one implementation.
    def upsert_current_thread_binding(
        self,
        sender_id: str,
        chat_id: str,
        thread_id: str,
        title: str = "",
        ttl_days: int = _CURRENT_THREAD_BINDING_TTL_DAYS,
        now: int | None = None,
    ) -> None:
        self.set_current_thread(sender_id, chat_id, thread_id, title, ttl_days, now)

    def get_current_thread_binding(
        self,
        sender_id: str,
        chat_id: str,
        now: int | None = None,
    ) -> Mapping[str, Any] | None:
        return self.current_thread(sender_id, chat_id, now)

    def clear_current_thread_binding(
        self,
        sender_id: str,
        chat_id: str,
        *,
        expected_thread_id: str | None = None,
    ) -> bool:
        return self.clear_current_thread(
            sender_id, chat_id, expected_thread_id=expected_thread_id
        )

    @staticmethod
    def _management_action(action: object) -> str:
        normalized = str(action or "").strip().lower()
        if normalized not in {"raw", "archive"}:
            raise ValueError("management action 必须是 raw 或 archive")
        return normalized

    def begin_management_context_action(
        self,
        context_id: str,
        action: str,
        inbound_message_id: str,
        *,
        now: int | None = None,
    ) -> ManagementActionReservation:
        """按 context/action 原子占用；成功和 unknown 永不被第二次执行。"""

        context = str(context_id or "").strip()
        normalized_action = self._management_action(action)
        inbound = str(inbound_message_id or "").strip()
        if not context or not inbound:
            raise ValueError("context_id 和 inbound_message_id 不能为空")
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
            exists = self._connection.execute(
                "SELECT 1 FROM management_contexts WHERE context_id=? AND expires_at>=?",
                (context, timestamp),
            ).fetchone()
            if exists is None:
                raise StateError("管理上下文不存在")
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO management_context_actions(
                    context_id, action, inbound_message_id, created_at, updated_at,
                    attempt_count, claimed_at
                ) VALUES(?,?,?,?,?,1,?)
                """,
                (
                    context,
                    normalized_action,
                    inbound,
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
            if cursor.rowcount == 1:
                return ManagementActionReservation("claimed", 1)
            row = self._connection.execute(
                """
                SELECT attempt_count, claimed_at, succeeded_at, uncertain_at
                FROM management_context_actions
                WHERE context_id=? AND action=?
                """,
                (context, normalized_action),
            ).fetchone()
            if row is None:
                raise StateError("管理动作状态读取失败")
            if row["succeeded_at"] is not None:
                return ManagementActionReservation("succeeded", int(row["attempt_count"]))
            if row["uncertain_at"] is not None:
                return ManagementActionReservation("uncertain", int(row["attempt_count"]))
            if row["claimed_at"] is not None:
                return ManagementActionReservation("busy", int(row["attempt_count"]))
            cursor = self._connection.execute(
                """
                UPDATE management_context_actions
                SET inbound_message_id=?, updated_at=?, attempt_count=attempt_count+1,
                    claimed_at=?, submitted_at=NULL, rejected_at=NULL,
                    last_error_code=NULL
                WHERE context_id=? AND action=? AND claimed_at IS NULL
                  AND succeeded_at IS NULL AND uncertain_at IS NULL
                """,
                (
                    inbound,
                    timestamp,
                    timestamp,
                    context,
                    normalized_action,
                ),
            )
            if cursor.rowcount != 1:
                return ManagementActionReservation("busy", int(row["attempt_count"]))
            return ManagementActionReservation("claimed", int(row["attempt_count"]) + 1)

    def mark_management_context_action_submitted(
        self,
        context_id: str,
        action: str,
        *,
        now: int | None = None,
    ) -> bool:
        """在首次外部写入前落下提交边界；其后异常必须失败关闭。"""

        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE management_context_actions
                SET updated_at=?, submitted_at=?
                WHERE context_id=? AND action=? AND claimed_at IS NOT NULL
                  AND submitted_at IS NULL AND succeeded_at IS NULL
                  AND uncertain_at IS NULL
                """,
                (
                    timestamp,
                    timestamp,
                    str(context_id),
                    self._management_action(action),
                ),
            )
        return cursor.rowcount == 1

    def complete_management_context_action(
        self,
        context_id: str,
        action: str,
        *,
        message_ids: Iterable[str] = (),
        redact_snapshot_key: str = "",
        now: int | None = None,
    ) -> bool:
        timestamp = int(time.time()) if now is None else int(now)
        normalized = tuple(
            dict.fromkeys(str(item or "").strip() for item in message_ids)
        )
        if any(not item for item in normalized):
            raise ValueError("message_ids 不能包含空值")
        encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
        snapshot_key = str(redact_snapshot_key or "").strip().lower()
        if snapshot_key and (
            len(snapshot_key) != 64
            or any(character not in "0123456789abcdef" for character in snapshot_key)
        ):
            raise ValueError("redact_snapshot_key 必须是 64 位十六进制键")
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE management_context_actions
                SET updated_at=?, succeeded_at=?, result_message_ids_json=?,
                    rejected_at=NULL, last_error_code=NULL
                WHERE context_id=? AND action=? AND claimed_at IS NOT NULL
                  AND succeeded_at IS NULL AND uncertain_at IS NULL
                """,
                (
                    timestamp,
                    timestamp,
                    encoded,
                    str(context_id),
                    self._management_action(action),
                ),
            )
            if cursor.rowcount == 1 and snapshot_key:
                rows = self._connection.execute(
                    "SELECT context_id, payload_json FROM management_contexts"
                ).fetchall()

                def redact(value: Any) -> bool:
                    changed = False
                    if isinstance(value, dict):
                        if (
                            str(value.get("snapshot_key") or "").lower() == snapshot_key
                            and "raw_final" in value
                        ):
                            value["raw_final"] = ""
                            value["raw_released_at"] = timestamp
                            changed = True
                        for child in value.values():
                            changed = redact(child) or changed
                    elif isinstance(value, list):
                        for child in value:
                            changed = redact(child) or changed
                    return changed

                for row in rows:
                    try:
                        payload = json.loads(str(row["payload_json"]))
                    except json.JSONDecodeError as exc:
                        raise StateError("清除原文时发现管理上下文 JSON 损坏") from exc
                    if redact(payload):
                        self._connection.execute(
                            "UPDATE management_contexts SET payload_json=? WHERE context_id=?",
                            (
                                json.dumps(
                                    payload,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                ),
                                str(row["context_id"]),
                            ),
                        )
        return cursor.rowcount == 1

    def release_management_context_action(
        self,
        context_id: str,
        action: str,
        error_code: str,
        *,
        allow_submitted: bool = False,
        now: int | None = None,
    ) -> bool:
        """释放可证明未被接受的动作。

        默认只允许释放尚未跨过外部提交边界的 claim。调用方若从官方接口或
        渠道得到“明确未接受/写入前失败”的结构化证据，必须显式传入
        ``allow_submitted=True``；状态层不会把一般异常误当成可安全重试。
        """

        if not isinstance(allow_submitted, bool):
            raise ValueError("allow_submitted 必须是布尔值")
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE management_context_actions
                SET updated_at=?, claimed_at=NULL, submitted_at=NULL,
                    rejected_at=?, last_error_code=?
                WHERE context_id=? AND action=? AND claimed_at IS NOT NULL
                  AND succeeded_at IS NULL AND uncertain_at IS NULL
                  AND (submitted_at IS NULL OR ?=1)
                """,
                (
                    timestamp,
                    timestamp,
                    str(error_code or "rejected")[:120],
                    str(context_id),
                    self._management_action(action),
                    1 if allow_submitted else 0,
                ),
            )
        return cursor.rowcount == 1

    def mark_management_context_action_uncertain(
        self,
        context_id: str,
        action: str,
        error_code: str,
        *,
        now: int | None = None,
    ) -> bool:
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE management_context_actions
                SET updated_at=?, uncertain_at=?, last_error_code=?
                WHERE context_id=? AND action=? AND claimed_at IS NOT NULL
                  AND succeeded_at IS NULL AND uncertain_at IS NULL
                """,
                (
                    timestamp,
                    timestamp,
                    str(error_code or "result_unknown")[:120],
                    str(context_id),
                    self._management_action(action),
                ),
            )
        return cursor.rowcount == 1

    def management_context_action(
        self, context_id: str, action: str
    ) -> Mapping[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT * FROM management_context_actions
                WHERE context_id=? AND action=?
                """,
                (str(context_id), self._management_action(action)),
            ).fetchone()
        return dict(row) if row is not None else None

    def recover_interrupted_management_actions(
        self, *, now: int | None = None
    ) -> Mapping[str, int]:
        """仅由主服务启动调用；只释放未提交 claim，已提交一律失败关闭。"""

        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
            unsubmitted = self._connection.execute(
                """
                UPDATE management_context_actions
                SET updated_at=?, claimed_at=NULL, rejected_at=?,
                    last_error_code='restart_before_submit'
                WHERE claimed_at IS NOT NULL AND submitted_at IS NULL
                  AND succeeded_at IS NULL AND uncertain_at IS NULL
                """,
                (timestamp, timestamp),
            )
            submitted = self._connection.execute(
                """
                UPDATE management_context_actions
                SET updated_at=?, uncertain_at=?,
                    last_error_code='restart_after_submit'
                WHERE claimed_at IS NOT NULL AND submitted_at IS NOT NULL
                  AND succeeded_at IS NULL AND uncertain_at IS NULL
                """,
                (timestamp, timestamp),
            )
        return {
            "unsubmitted_released": max(0, unsubmitted.rowcount),
            "submitted_uncertain": max(0, submitted.rowcount),
        }

    @staticmethod
    def _remote_context_id(context_id: object) -> str:
        normalized = str(context_id or "").strip()
        if not normalized or len(normalized) > 512:
            raise ValueError("context_id 不能为空且不得超过 512 字符")
        return normalized

    @staticmethod
    def _remote_control_action(action: object) -> str:
        normalized = str(action or "").strip().lower()
        if normalized not in REMOTE_CONTROL_ACTIONS:
            raise ValueError(
                "remote control action 必须是 "
                + "、".join(_REMOTE_CONTROL_ACTION_NAMES)
            )
        return normalized

    @staticmethod
    def _remote_request_hash(request_hash: object) -> str:
        normalized = str(request_hash or "").strip().lower()
        if len(normalized) != 64 or re.fullmatch(r"[0-9a-f]{64}", normalized) is None:
            raise ValueError("request_hash 必须是 64 位十六进制键")
        return normalized

    @staticmethod
    def _remote_result_json(value: object) -> str | None:
        """编码有限的结果证据；远程请求正文永远不由状态层保存。"""

        if value is None:
            return None
        if isinstance(value, str):
            encoded = value
            try:
                json.loads(encoded)
            except json.JSONDecodeError as exc:
                raise ValueError("result_json 必须是合法 JSON") from exc
        else:
            try:
                encoded = json.dumps(
                    value,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("远程动作结果证据必须可编码为 JSON") from exc
        # 结果字段只能放最小状态证据，避免把 App Server 原文或 prompt
        # 复制进永久状态；详细原文不属于远程动作状态机。
        if len(encoded.encode("utf-8")) > 8192:
            raise ValueError("远程动作结果证据不得超过 8192 字节")
        return encoded

    @staticmethod
    def _remote_action_state(row: Mapping[str, Any]) -> str:
        if row.get("succeeded_at") is not None:
            return "succeeded"
        if row.get("uncertain_at") is not None:
            return "uncertain"
        if row.get("claimed_at") is not None:
            return "claimed"
        if row.get("rejected_at") is not None:
            return "rejected"
        return "available"

    def begin_remote_control_action(
        self,
        context_id: str,
        action: str,
        request_hash: str,
        inbound_message_id: str,
        *,
        now: int | None = None,
    ) -> RemoteControlActionReservation:
        """按 ``context/action/request_hash`` 原子占用一个远程写动作。

        ``request_hash`` 是调用方对规范化请求的 SHA-256；表中不保存原始
        prompt。只有 rejected（或尚未跨提交边界的重启释放）可以再次 claim，
        succeeded/uncertain 永远不可重放。
        """

        context = self._remote_context_id(context_id)
        normalized_action = self._remote_control_action(action)
        normalized_hash = self._remote_request_hash(request_hash)
        inbound = str(inbound_message_id or "").strip()
        if not inbound or len(inbound) > 512:
            raise ValueError("inbound_message_id 不能为空且不得超过 512 字符")
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
            context_row = self._connection.execute(
                "SELECT expires_at FROM management_contexts WHERE context_id=?",
                (context,),
            ).fetchone()
            if context_row is None or int(context_row["expires_at"]) < timestamp:
                raise StateError("管理上下文不存在或已过期")
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO remote_control_actions(
                    context_id, action, request_hash, inbound_message_id,
                    created_at, updated_at, attempt_count, claimed_at
                ) VALUES(?,?,?,?,?,?,1,?)
                """,
                (
                    context,
                    normalized_action,
                    normalized_hash,
                    inbound,
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
            if cursor.rowcount == 1:
                return RemoteControlActionReservation(
                    "claimed", 1, normalized_hash
                )
            row = self._connection.execute(
                """
                SELECT attempt_count, claimed_at, submitted_at, succeeded_at,
                       rejected_at, uncertain_at
                FROM remote_control_actions
                WHERE context_id=? AND action=? AND request_hash=?
                """,
                (context, normalized_action, normalized_hash),
            ).fetchone()
            if row is None:
                raise StateError("远程动作状态读取失败")
            attempt_count = int(row["attempt_count"])
            if row["succeeded_at"] is not None:
                return RemoteControlActionReservation(
                    "succeeded", attempt_count, normalized_hash
                )
            if row["uncertain_at"] is not None:
                return RemoteControlActionReservation(
                    "uncertain", attempt_count, normalized_hash
                )
            if row["claimed_at"] is not None:
                return RemoteControlActionReservation("busy", attempt_count, normalized_hash)
            # A rejected row is reusable.  The update predicate also protects
            # against a concurrent claimant that won the race after the read.
            cursor = self._connection.execute(
                """
                UPDATE remote_control_actions
                SET inbound_message_id=?, updated_at=?, attempt_count=attempt_count+1,
                    claimed_at=?, submitted_at=NULL, rejected_at=NULL,
                    last_error_code=NULL, result_json=NULL
                WHERE context_id=? AND action=? AND request_hash=?
                  AND claimed_at IS NULL AND succeeded_at IS NULL
                  AND uncertain_at IS NULL
                """,
                (
                    inbound,
                    timestamp,
                    timestamp,
                    context,
                    normalized_action,
                    normalized_hash,
                ),
            )
            if cursor.rowcount != 1:
                return RemoteControlActionReservation(
                    "busy", attempt_count, normalized_hash
                )
            return RemoteControlActionReservation(
                "claimed", attempt_count + 1, normalized_hash
            )

    def mark_remote_control_action_submitted(
        self,
        context_id: str,
        action: str,
        request_hash: str,
        *,
        now: int | None = None,
    ) -> bool:
        """在调用官方写方法前记录提交边界。"""

        context = self._remote_context_id(context_id)
        normalized_action = self._remote_control_action(action)
        normalized_hash = self._remote_request_hash(request_hash)
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE remote_control_actions
                SET updated_at=?, submitted_at=?
                WHERE context_id=? AND action=? AND request_hash=?
                  AND claimed_at IS NOT NULL AND submitted_at IS NULL
                  AND succeeded_at IS NULL AND uncertain_at IS NULL
                """,
                (
                    timestamp,
                    timestamp,
                    context,
                    normalized_action,
                    normalized_hash,
                ),
            )
        return cursor.rowcount == 1

    def complete_remote_control_action(
        self,
        context_id: str,
        action: str,
        request_hash: str,
        *,
        result: object = None,
        result_json: object = None,
        now: int | None = None,
    ) -> bool:
        """记录官方动作成功；必须已经跨过 submitted 边界。

        ``result``/``result_json`` 仅用于少量成功证据（例如官方返回的
        ``{"ok":true}``），不应传入 prompt、原文或完整 RPC 响应。
        """

        if result is not None and result_json is not None:
            raise ValueError("result 和 result_json 只能提供一个")
        evidence = self._remote_result_json(
            result_json if result_json is not None else result
        )
        context = self._remote_context_id(context_id)
        normalized_action = self._remote_control_action(action)
        normalized_hash = self._remote_request_hash(request_hash)
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE remote_control_actions
                SET updated_at=?, succeeded_at=?, result_json=?,
                    rejected_at=NULL, last_error_code=NULL
                WHERE context_id=? AND action=? AND request_hash=?
                  AND claimed_at IS NOT NULL AND submitted_at IS NOT NULL
                  AND succeeded_at IS NULL AND uncertain_at IS NULL
                """,
                (
                    timestamp,
                    timestamp,
                    evidence,
                    context,
                    normalized_action,
                    normalized_hash,
                ),
            )
        return cursor.rowcount == 1

    def release_remote_control_action(
        self,
        context_id: str,
        action: str,
        request_hash: str,
        error_code: str,
        *,
        allow_submitted: bool = False,
        now: int | None = None,
    ) -> bool:
        """释放确定未被官方接受的动作；unknown 不得走该路径。"""

        if not isinstance(allow_submitted, bool):
            raise ValueError("allow_submitted 必须是布尔值")
        context = self._remote_context_id(context_id)
        normalized_action = self._remote_control_action(action)
        normalized_hash = self._remote_request_hash(request_hash)
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE remote_control_actions
                SET updated_at=?, claimed_at=NULL, submitted_at=NULL,
                    rejected_at=?, result_json=NULL, last_error_code=?
                WHERE context_id=? AND action=? AND request_hash=?
                  AND claimed_at IS NOT NULL AND succeeded_at IS NULL
                  AND uncertain_at IS NULL
                  AND (submitted_at IS NULL OR ?=1)
                """,
                (
                    timestamp,
                    timestamp,
                    str(error_code or "rejected")[:120],
                    context,
                    normalized_action,
                    normalized_hash,
                    1 if allow_submitted else 0,
                ),
            )
        return cursor.rowcount == 1

    def mark_remote_control_action_uncertain(
        self,
        context_id: str,
        action: str,
        request_hash: str,
        error_code: str,
        *,
        now: int | None = None,
    ) -> bool:
        """冻结已提交但结果未知的动作，禁止盲目重放。"""

        context = self._remote_context_id(context_id)
        normalized_action = self._remote_control_action(action)
        normalized_hash = self._remote_request_hash(request_hash)
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE remote_control_actions
                SET updated_at=?, uncertain_at=?, last_error_code=?
                WHERE context_id=? AND action=? AND request_hash=?
                  AND claimed_at IS NOT NULL AND submitted_at IS NOT NULL
                  AND succeeded_at IS NULL AND uncertain_at IS NULL
                """,
                (
                    timestamp,
                    timestamp,
                    str(error_code or "result_unknown")[:120],
                    context,
                    normalized_action,
                    normalized_hash,
                ),
            )
        return cursor.rowcount == 1

    def remote_control_action(
        self,
        context_id: str,
        action: str,
        request_hash: str,
    ) -> Mapping[str, Any] | None:
        """读取单个远程动作状态；不返回请求正文。"""

        context = self._remote_context_id(context_id)
        normalized_action = self._remote_control_action(action)
        normalized_hash = self._remote_request_hash(request_hash)
        with self._lock:
            row = self._connection.execute(
                """
                SELECT * FROM remote_control_actions
                WHERE context_id=? AND action=? AND request_hash=?
                """,
                (context, normalized_action, normalized_hash),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["state"] = self._remote_action_state(result)
        return result

    def list_remote_control_actions(
        self,
        context_id: str | None = None,
        *,
        limit: int = 100,
    ) -> list[Mapping[str, Any]]:
        """返回有限的动作状态列表，供状态页/恢复审计使用。"""

        if limit < 1 or limit > 1000:
            raise ValueError("limit 必须介于 1 和 1000")
        parameters: tuple[Any, ...]
        if context_id is None:
            query = (
                "SELECT * FROM remote_control_actions "
                "ORDER BY updated_at DESC, context_id, action, request_hash LIMIT ?"
            )
            parameters = (int(limit),)
        else:
            context = self._remote_context_id(context_id)
            query = (
                "SELECT * FROM remote_control_actions WHERE context_id=? "
                "ORDER BY updated_at DESC, action, request_hash LIMIT ?"
            )
            parameters = (context, int(limit))
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        result: list[Mapping[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["state"] = self._remote_action_state(item)
            result.append(item)
        return result

    def recover_interrupted_remote_control_actions(
        self, *, now: int | None = None
    ) -> Mapping[str, int]:
        """服务启动恢复：提交前 claim 可释放，提交后统一冻结 unknown。"""

        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
            unsubmitted = self._connection.execute(
                """
                UPDATE remote_control_actions
                SET updated_at=?, claimed_at=NULL, rejected_at=?,
                    last_error_code='restart_before_submit'
                WHERE claimed_at IS NOT NULL AND submitted_at IS NULL
                  AND succeeded_at IS NULL AND uncertain_at IS NULL
                """,
                (timestamp, timestamp),
            )
            submitted = self._connection.execute(
                """
                UPDATE remote_control_actions
                SET updated_at=?, uncertain_at=?,
                    last_error_code='restart_after_submit'
                WHERE submitted_at IS NOT NULL
                  AND succeeded_at IS NULL AND uncertain_at IS NULL
                """,
                (timestamp, timestamp),
            )
        return {
            "unsubmitted_released": max(0, unsubmitted.rowcount),
            "submitted_uncertain": max(0, submitted.rowcount),
        }

    def reserve_management_inbound(
        self,
        message_id: str,
        sender_id: str,
        content: str,
        *,
        now: int | None = None,
    ) -> bool:
        """持久化占用一条入站消息，防止飞书重投造成重复创建或重复续聊。"""

        normalized = str(message_id or "").strip()
        sender = str(sender_id or "").strip()
        if not normalized or not sender:
            raise ValueError("message_id 和 sender_id 不能为空")
        digest = hashlib.sha256(str(content).encode("utf-8")).hexdigest()
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO management_inbound_messages(
                    message_id, sender_id, content_hash, created_at
                ) VALUES(?, ?, ?, ?)
                """,
                (normalized, sender, digest, timestamp),
            )
        return cursor.rowcount == 1

    def management_inbound_status(
        self,
        message_id: str,
        *,
        sender_id: str | None = None,
        content: str | None = None,
    ) -> str:
        """Read the durable management hand-off state for one inbound ID.

        ``completed_at`` is the ACK boundary.  A row that was only reserved
        before the management worker crashed remains ``pending`` and must be
        replayed by guardian.  Optional identity/content arguments let the
        callback reject a forged reuse of an existing Feishu message ID.
        """

        normalized = str(message_id or "").strip()
        if not normalized:
            raise ValueError("message_id 不能为空")
        sender = None if sender_id is None else str(sender_id or "").strip()
        digest = (
            None
            if content is None
            else hashlib.sha256(str(content).encode("utf-8")).hexdigest()
        )
        with self._lock:
            row = self._connection.execute(
                """
                SELECT sender_id, content_hash, completed_at
                FROM management_inbound_messages
                WHERE message_id=?
                """,
                (normalized,),
            ).fetchone()
        if row is None:
            return "missing"
        if sender is not None and str(row["sender_id"]) != sender:
            return "conflict"
        if digest is not None and str(row["content_hash"]) != digest:
            return "conflict"
        return "accepted" if row["completed_at"] is not None else "pending"

    def reply_delivery_status(self, inbound_message_id: str) -> str:
        """Read whether a normal reply outbox row is durably ACK-able.

        The query requires the stored reply text and the exact ordinary-turn
        target.  It treats a later delivery claim as accepted because the
        guardian ACK protects the inbound event, while the reply outbox has
        its own crash/retry lifecycle.
        """

        normalized = str(inbound_message_id or "").strip()
        if not normalized:
            raise ValueError("inbound_message_id 不能为空")
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT d.delivery_id, d.parent_code, d.reply_fingerprint,
                       d.reply_text, d.discarded_at,
                       n.thread_id, n.turn_id, n.reply_kind, n.sent_at
                FROM reply_deliveries AS d
                LEFT JOIN notifications AS n ON n.code=d.parent_code
                WHERE d.inbound_message_id=?
                ORDER BY d.sequence
                """,
                (normalized,),
            ).fetchall()
        if not rows:
            return "missing"
        if len(rows) != 1:
            return "conflict"
        row = rows[0]
        if row["discarded_at"] is not None or row["reply_text"] is None:
            return "rejected"
        if (
            not str(row["delivery_id"] or "").strip()
            or not str(row["parent_code"] or "").strip()
            or not str(row["reply_fingerprint"] or "").strip()
        ):
            return "rejected"
        if (
            row["thread_id"] is None
            or row["turn_id"] is None
            or str(row["reply_kind"] or "") != "turn"
            or row["sent_at"] is None
        ):
            return "pending"
        return "accepted"

    def complete_management_inbound(
        self, message_id: str, *, now: int | None = None
    ) -> None:
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE management_inbound_messages SET completed_at=? WHERE message_id=?",
                (timestamp, str(message_id or "").strip()),
            )

    def session_search_cache(
        self, thread_id: str, content_hash: str
    ) -> Mapping[str, Any] | None:
        """读取与精确会话内容版本绑定的本地检索证据缓存。"""

        normalized = self._monitor_thread_id(thread_id)
        digest = str(content_hash or "").strip().lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("content_hash 必须是 SHA-256")
        with self._lock:
            row = self._connection.execute(
                """
                SELECT latest_turn_id, description, evidence_json, last_result,
                       last_activity_at, updated_at
                FROM session_search_cache
                WHERE thread_id=? AND content_hash=?
                """,
                (normalized, digest),
            ).fetchone()
        if row is None:
            return None
        try:
            evidence = json.loads(str(row["evidence_json"]))
        except json.JSONDecodeError as exc:
            raise StateError("会话搜索证据缓存 JSON 损坏") from exc
        if not isinstance(evidence, dict):
            raise StateError("会话搜索证据缓存根节点不是对象")
        return {
            "latest_turn_id": str(row["latest_turn_id"]),
            "description": str(row["description"]),
            "evidence": evidence,
            "last_result": str(row["last_result"]),
            "last_activity_at": (
                int(row["last_activity_at"])
                if row["last_activity_at"] is not None
                else None
            ),
            "updated_at": int(row["updated_at"]),
        }

    def put_session_search_cache(
        self,
        *,
        thread_id: str,
        content_hash: str,
        latest_turn_id: str,
        description: str,
        evidence: Mapping[str, Any],
        last_result: str,
        last_activity_at: int | None,
        now: int | None = None,
    ) -> None:
        """原子写入描述、证据与进度同款最后结果；不写日志。"""

        normalized = self._monitor_thread_id(thread_id)
        digest = str(content_hash or "").strip().lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("content_hash 必须是 SHA-256")
        turn_id = str(latest_turn_id or "").strip()
        if not turn_id:
            raise ValueError("latest_turn_id 不能为空")
        encoded = json.dumps(dict(evidence), ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 256 * 1024:
            raise ValueError("会话搜索证据缓存超过 256 KiB 上限")
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO session_search_cache(
                    thread_id, content_hash, latest_turn_id, description,
                    evidence_json, last_result, last_activity_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(thread_id, content_hash) DO UPDATE SET
                    latest_turn_id=excluded.latest_turn_id,
                    description=excluded.description,
                    evidence_json=excluded.evidence_json,
                    last_result=excluded.last_result,
                    last_activity_at=excluded.last_activity_at,
                    updated_at=excluded.updated_at
                """,
                (
                    normalized, digest, turn_id, str(description or "")[:500],
                    encoded, str(last_result or "")[:1000],
                    None if last_activity_at is None else int(last_activity_at),
                    timestamp,
                ),
            )
            # 精确 content_hash 查询天然不会命中旧内容版本。不要在新版本写入时
            # 物理删除旧缓存/评分：保留 90 天审计与回滚窗口，统一由 prune()
            # 负责到期清理。

    def session_search_judgment(
        self, query_hash: str, thread_id: str, content_hash: str
    ) -> Mapping[str, Any] | None:
        query = str(query_hash or "").strip().lower()
        digest = str(content_hash or "").strip().lower()
        normalized = self._monitor_thread_id(thread_id)
        if any(len(item) != 64 or any(c not in "0123456789abcdef" for c in item) for item in (query, digest)):
            raise ValueError("query_hash 和 content_hash 必须是 SHA-256")
        with self._lock:
            row = self._connection.execute(
                """
                SELECT score, confidence, classification, display_title, reason, updated_at
                FROM session_search_judgments
                WHERE query_hash=? AND thread_id=? AND content_hash=?
                """,
                (query, normalized, digest),
            ).fetchone()
        if row is None:
            return None
        display_title = str(row["display_title"] or "").strip()
        if not display_title:
            # v11 旧评分缺少展示名；让下一次正常搜索在原批次内重评，
            # 不额外启动专门的标题模型调用。
            return None
        return {
            "score": float(row["score"]),
            "confidence": str(row["confidence"]),
            "classification": str(row["classification"]),
            "display_title": display_title,
            "reason": str(row["reason"]),
            "updated_at": int(row["updated_at"]),
        }

    def put_session_search_judgment(
        self,
        *,
        query_hash: str,
        thread_id: str,
        content_hash: str,
        score: float,
        confidence: str,
        classification: str,
        display_title: str,
        reason: str,
        now: int | None = None,
    ) -> None:
        query = str(query_hash or "").strip().lower()
        digest = str(content_hash or "").strip().lower()
        normalized = self._monitor_thread_id(thread_id)
        if any(len(item) != 64 or any(c not in "0123456789abcdef" for c in item) for item in (query, digest)):
            raise ValueError("query_hash 和 content_hash 必须是 SHA-256")
        numeric = float(score)
        if not 0 <= numeric <= 1:
            raise ValueError("score 必须介于 0 和 1")
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT OR REPLACE INTO session_search_judgments(
                    query_hash, thread_id, content_hash, score, confidence,
                    classification, display_title, reason, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    query, normalized, digest, numeric,
                    str(confidence or "")[:32], str(classification or "")[:32],
                    str(display_title or "")[:64], str(reason or "")[:500], timestamp,
                ),
            )

    def thread_title_recovery(
        self, thread_id: str, content_hash: str
    ) -> Mapping[str, Any] | None:
        """读取与当前内容版本严格匹配的历史异常恢复名。"""

        normalized = self._monitor_thread_id(thread_id)
        digest = str(content_hash or "").strip().lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("content_hash 必须是 SHA-256")
        with self._lock:
            row = self._connection.execute(
                """
                SELECT display_title, source, created_at, updated_at
                FROM thread_title_recoveries
                WHERE thread_id=? AND content_hash=?
                """,
                (normalized, digest),
            ).fetchone()
        if row is None:
            return None
        return {
            "display_title": str(row["display_title"] or "").strip(),
            "source": str(row["source"] or "").strip(),
            "created_at": int(row["created_at"]),
            "updated_at": int(row["updated_at"]),
        }

    def put_thread_title_recovery(
        self,
        *,
        thread_id: str,
        content_hash: str,
        display_title: str,
        source: str = "luna_recovery",
        now: int | None = None,
    ) -> None:
        """持久保存一次性异常恢复名；不改 Codex 自己的标题数据库。"""

        normalized = self._monitor_thread_id(thread_id)
        digest = str(content_hash or "").strip().lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("content_hash 必须是 SHA-256")
        title = " ".join(str(display_title or "").split()).strip("《》 ")
        if not 2 <= len(title) <= 32:
            raise ValueError("display_title 必须介于 2 和 32 个字符")
        origin = str(source or "").strip()
        if origin not in {"luna_recovery", "search_assessment"}:
            raise ValueError("恢复名 source 无效")
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO thread_title_recoveries(
                    thread_id, content_hash, display_title, source, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(thread_id, content_hash) DO UPDATE SET
                    display_title=excluded.display_title,
                    source=excluded.source,
                    updated_at=excluded.updated_at
                """,
                (normalized, digest, title, origin, timestamp, timestamp),
            )

    def enqueue_hook_payload(self, payload: Mapping[str, Any]) -> bool:
        """快速接收 Codex notify；相同 thread/turn/status 只入队一次。"""

        thread_id = str(payload.get("thread-id") or payload.get("thread_id") or "").strip()
        turn_id = str(payload.get("turn-id") or payload.get("turn_id") or "").strip()
        event_type = str(payload.get("type") or "").strip()
        if event_type != "agent-turn-complete" or not thread_id or not turn_id:
            raise StateError("Codex notify 缺少合法的 type/thread-id/turn-id")
        event_key = f"{thread_id}:{turn_id}:completed"
        encoded = json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "INSERT OR IGNORE INTO hook_events(event_key, payload_json, created_at) VALUES(?,?,?)",
                (event_key, encoded, int(time.time())),
            )
        return cursor.rowcount == 1

    def pending_hook_payloads(self, limit: int = 100) -> list[tuple[str, Mapping[str, Any]]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT event_key, payload_json FROM hook_events WHERE consumed_at IS NULL ORDER BY created_at LIMIT ?",
                (limit,),
            ).fetchall()
        result: list[tuple[str, Mapping[str, Any]]] = []
        for row in rows:
            try:
                value = json.loads(row["payload_json"])
            except json.JSONDecodeError as exc:
                raise StateError("本地 Codex hook 事件 JSON 损坏，拒绝静默消费") from exc
            if not isinstance(value, dict):
                raise StateError("本地 Codex hook 事件根节点不是对象，拒绝静默消费")
            result.append((row["event_key"], value))
        return result

    def mark_hook_consumed(self, event_key: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE hook_events SET consumed_at=? WHERE event_key=? AND consumed_at IS NULL",
                (int(time.time()), event_key),
            )

    def pending_hook_count(self) -> int:
        """返回尚未消费的 Codex hook 数量，不读取或输出事件正文。"""

        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) FROM hook_events WHERE consumed_at IS NULL"
            ).fetchone()
        return int(row[0])

    def baseline_pending_hooks(self, expected_count: int) -> int:
        """原子建立首次启用基线，防止把停用期历史事件突发推送到消息渠道。

        调用方必须先停机，并把刚刚只读观察到的数量作为 ``expected_count``
        传回。数量在确认期间发生变化时整个事务会拒绝提交，避免静默丢掉
        用户没有确认过的新事件。正常运行后的故障恢复不调用此方法，因此
        仍会保留未完成投递。
        """

        if expected_count < 0:
            raise ValueError("expected_count 不能为负数")
        timestamp = int(time.time())
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT COUNT(*) FROM hook_events WHERE consumed_at IS NULL"
            ).fetchone()
            current_count = int(row[0])
            if current_count != expected_count:
                raise StateError(
                    "待处理 hook 数量在确认期间发生变化；已拒绝建立基线，请重新检查"
                )
            cursor = self._connection.execute(
                "UPDATE hook_events SET consumed_at=? WHERE consumed_at IS NULL",
                (timestamp,),
            )
        if cursor.rowcount != expected_count:
            raise StateError("建立启用前 hook 基线时数量不一致")
        return cursor.rowcount

    def was_processed(self, event_key: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM processed_turns WHERE event_key=?", (event_key,)
            ).fetchone()
        return row is not None

    def mark_processed(self, event_key: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO processed_turns(event_key, processed_at) VALUES(?,?)",
                (event_key, int(time.time())),
            )

    @staticmethod
    def _judgment_text(value: object, limit: int) -> str:
        """Normalize and redact bounded policy evidence before persistence."""

        text = str(value or "").replace("\x00", "").strip()
        text = _NOTIFICATION_URL_PATTERN.sub("链接", text)
        text = _NOTIFICATION_PATH_PATTERN.sub("本地文件", text)
        text = _NOTIFICATION_HASH_PATTERN.sub("校验值", text)
        text = _NOTIFICATION_SECRET_PATTERN.sub(r"\1\2已隐藏", text)
        text = "\n".join(" ".join(line.split()) for line in text.splitlines())
        if len(text) > limit:
            text = text[: limit - 1].rstrip() + "…"
        return text

    @classmethod
    def _judgment_text_list(
        cls, values: Iterable[object] | None, *, limit: int, maximum: int
    ) -> tuple[str, ...]:
        if values is None:
            return ()
        if isinstance(values, (str, bytes)):
            values = (values,)
        try:
            result = tuple(
                dict.fromkeys(
                    cls._judgment_text(value, limit) for value in values
                )
            )
        except TypeError as exc:
            raise ValueError("通知判定证据必须是字符串序列") from exc
        return tuple(item for item in result if item)[:maximum]

    @classmethod
    def _judgment_json_list(cls, raw: object, field: str) -> tuple[str, ...]:
        try:
            value = json.loads(str(raw or "[]"))
        except json.JSONDecodeError as exc:
            raise StateError(f"通知判定 {field} JSON 损坏") from exc
        if not isinstance(value, list):
            raise StateError(f"通知判定 {field} JSON 根节点不是数组")
        return cls._judgment_text_list(
            value,
            limit=(
                _NOTIFICATION_JUDGMENT_FACT_MAX_CHARS
                if field == "new_facts"
                else _NOTIFICATION_JUDGMENT_TEXT_MAX_CHARS
            ),
            maximum=(
                _NOTIFICATION_JUDGMENT_MAX_FACTS
                if field == "new_facts"
                else _NOTIFICATION_JUDGMENT_RECENT_MAX
            ),
        )

    @classmethod
    def _notification_judgment_from_row(
        cls, row: sqlite3.Row
    ) -> NotificationJudgment:
        recent = cls._judgment_json_list(
            row["recent_successful_notifications_json"],
            "recent_successful_notifications",
        )
        facts = cls._judgment_json_list(row["new_facts_json"], "new_facts")
        digest = str(row["input_digest"] or "").strip().lower()
        if digest and (len(digest) != 64 or re.fullmatch(r"[0-9a-f]{64}", digest) is None):
            raise StateError("通知判定 input_digest 无效")
        return NotificationJudgment(
            event_key=str(row["event_key"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
            status=cls._judgment_text(row["status"], 64),
            notification_reason=cls._judgment_text(
                row["notification_reason"], 64
            ),
            decision_reason=cls._judgment_text(
                row["decision_reason"], _NOTIFICATION_JUDGMENT_REASON_MAX_CHARS
            ),
            matched_request=cls._judgment_text(
                row["matched_request"], _NOTIFICATION_JUDGMENT_TEXT_MAX_CHARS
            ),
            user_request=cls._judgment_text(
                row["user_request"], _NOTIFICATION_JUDGMENT_TEXT_MAX_CHARS
            ),
            task_state=cls._judgment_text(row["task_state"], 320),
            recent_successful_notifications=recent,
            new_facts=facts,
            model_error=cls._judgment_text(
                row["model_error"], _NOTIFICATION_JUDGMENT_REASON_MAX_CHARS
            ),
            model_attempts=max(0, int(row["model_attempts"])),
            input_digest=digest,
        )

    def record_notification_judgment(
        self,
        event_key: str,
        report: ProgressReport,
        context: NotificationContext | None = None,
        *,
        model_attempts: int | None = None,
        now: int | None = None,
    ) -> NotificationJudgment:
        """Persist one policy result and its bounded audit context.

        This method is idempotent for an event key.  A later retry may update
        the rationale or error marker, but it cannot lower the attempt count
        or overwrite the original creation time.  The parent notification must
        already exist so retention and foreign-key cleanup remain aligned.
        """

        normalized = self._summary_event_key(event_key)
        if not isinstance(report, ProgressReport):
            raise TypeError("report 必须是 ProgressReport")
        if context is None:
            context = NotificationContext(
                user_request=report.matched_request,
                task_state=str(report.status),
            )
        elif not isinstance(context, NotificationContext):
            raise TypeError("context 必须是 NotificationContext")
        timestamp = int(time.time()) if now is None else int(now)
        if timestamp < 0:
            raise ValueError("通知判定时间不能为负数")
        if model_attempts is not None:
            if isinstance(model_attempts, bool) or int(model_attempts) < 0:
                raise ValueError("model_attempts 必须是非负整数")
            supplied_attempts = int(model_attempts)
        else:
            supplied_attempts = 0

        status = self._judgment_text(report.status, 64)
        reason = self._judgment_text(report.notification_reason, 64)
        decision_reason = self._judgment_text(
            report.decision_reason, _NOTIFICATION_JUDGMENT_REASON_MAX_CHARS
        )
        matched_request = self._judgment_text(
            report.matched_request, _NOTIFICATION_JUDGMENT_TEXT_MAX_CHARS
        )
        user_request = self._judgment_text(
            context.user_request, _NOTIFICATION_JUDGMENT_TEXT_MAX_CHARS
        )
        task_state = self._judgment_text(context.task_state, 320)
        recent = self._judgment_text_list(
            context.recent_successful_notifications,
            limit=_NOTIFICATION_JUDGMENT_TEXT_MAX_CHARS,
            maximum=_NOTIFICATION_JUDGMENT_RECENT_MAX,
        )
        facts = self._judgment_text_list(
            report.new_facts,
            limit=_NOTIFICATION_JUDGMENT_FACT_MAX_CHARS,
            maximum=_NOTIFICATION_JUDGMENT_MAX_FACTS,
        )
        model_error = self._judgment_text(
            report.model_error, _NOTIFICATION_JUDGMENT_REASON_MAX_CHARS
        )
        digest_payload = {
            "user_request": user_request,
            "task_state": task_state,
            "recent_successful_notifications": list(recent),
        }
        input_digest = hashlib.sha256(
            json.dumps(
                digest_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        encoded_recent = json.dumps(recent, ensure_ascii=False, separators=(",", ":"))
        encoded_facts = json.dumps(facts, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connection:
            parent = self._connection.execute(
                "SELECT 1 FROM notifications WHERE event_key=?", (normalized,)
            ).fetchone()
            if parent is None:
                raise StateError("无法为不存在的通知保存判定")
            previous = self._connection.execute(
                "SELECT model_attempts, created_at FROM notification_judgments WHERE event_key=?",
                (normalized,),
            ).fetchone()
            attempts = max(
                supplied_attempts,
                (int(previous["model_attempts"]) + 1 if previous is not None else 1),
            )
            created_at = int(previous["created_at"]) if previous is not None else timestamp
            self._connection.execute(
                """
                INSERT INTO notification_judgments(
                    event_key, created_at, updated_at, status, notification_reason,
                    decision_reason, matched_request, user_request, task_state,
                    recent_successful_notifications_json, new_facts_json,
                    model_error, model_attempts, input_digest
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(event_key) DO UPDATE SET
                    updated_at=excluded.updated_at,
                    status=excluded.status,
                    notification_reason=excluded.notification_reason,
                    decision_reason=excluded.decision_reason,
                    matched_request=excluded.matched_request,
                    user_request=excluded.user_request,
                    task_state=excluded.task_state,
                    recent_successful_notifications_json=excluded.recent_successful_notifications_json,
                    new_facts_json=excluded.new_facts_json,
                    model_error=excluded.model_error,
                    model_attempts=MAX(notification_judgments.model_attempts, excluded.model_attempts),
                    input_digest=excluded.input_digest
                """,
                (
                    normalized,
                    created_at,
                    timestamp,
                    status,
                    reason,
                    decision_reason,
                    matched_request,
                    user_request,
                    task_state,
                    encoded_recent,
                    encoded_facts,
                    model_error,
                    attempts,
                    input_digest,
                ),
            )
            row = self._connection.execute(
                "SELECT * FROM notification_judgments WHERE event_key=?", (normalized,)
            ).fetchone()
        if row is None:
            raise StateError("通知判定保存后无法读取")
        return self._notification_judgment_from_row(row)

    def notification_judgment(self, event_key: str) -> NotificationJudgment | None:
        """Read one persisted policy judgment without changing outbox state."""

        normalized = self._summary_event_key(event_key)
        if self.read_only and "notification_judgments" not in self._read_only_tables:
            return None
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM notification_judgments WHERE event_key=?", (normalized,)
            ).fetchone()
        return None if row is None else self._notification_judgment_from_row(row)

    def successful_notification_reply_texts(self, thread_id: str) -> tuple[str, ...]:
        """Bounded local proof for exact-turn Desktop input; never match by title."""
        thread=str(thread_id or '').strip()
        if not thread:
            return ()
        with self._lock:
            rows=self._connection.execute(
                """SELECT d.reply_text FROM reply_deliveries AS d
                   JOIN notifications AS n ON n.code=d.parent_code
                   WHERE n.thread_id=? AND d.delivered_at IS NOT NULL
                     AND d.discarded_at IS NULL AND d.reply_text IS NOT NULL
                   ORDER BY d.sequence DESC LIMIT 64""", (thread,),
            ).fetchall()
        return tuple(str(row[0]).strip() for row in rows if str(row[0]).strip() and len(str(row[0]))<=64000)

    def recent_successful_notification_context(
        self,
        thread_id: str,
        *,
        current_event_key: str | None = None,
        limit: int = _NOTIFICATION_JUDGMENT_RECENT_MAX,
    ) -> tuple[str, ...]:
        """Return compact summaries already delivered for one exact thread."""

        thread = str(thread_id or "").strip()
        if not thread:
            return ()
        if isinstance(limit, bool) or not 1 <= int(limit) <= _NOTIFICATION_JUDGMENT_RECENT_MAX:
            raise ValueError("通知历史 limit 必须在 1 到 5 之间")
        maximum = int(limit)
        current = None if current_event_key is None else self._summary_event_key(current_event_key)
        where_current = "" if current is None else " AND n.event_key<>?"
        parameters: tuple[Any, ...] = (thread,)
        if current is not None:
            parameters += (current,)
        parameters += (maximum,)
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT n.event_key,
                       COALESCE(NULLIF(s.message_text, ''), n.message_text) AS body,
                       COALESCE(s.delivered_at, n.sent_at) AS delivered_time
                FROM notifications AS n
                LEFT JOIN notification_summary_deliveries AS s
                  ON s.event_key=n.event_key
                WHERE n.thread_id=? AND n.reply_kind='turn'
                  {where_current}
                  AND n.discarded_at IS NULL
                  AND (
                      (s.event_key IS NOT NULL AND s.delivered_at IS NOT NULL)
                      OR (s.event_key IS NULL AND n.sent_at IS NOT NULL)
                  )
                ORDER BY delivered_time DESC, n.event_key DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        result: list[str] = []
        for row in rows:
            body = self._judgment_text(row["body"], _NOTIFICATION_JUDGMENT_TEXT_MAX_CHARS)
            if body and body not in result:
                result.append(body)
        return tuple(result[:maximum])

    @staticmethod
    def _summary_event_key(event_key: object) -> str:
        normalized = str(event_key or "").strip()
        if not normalized or len(normalized) > 512:
            raise ValueError("summary event_key 不能为空且不得超过 512 字符")
        return normalized

    @staticmethod
    def _summary_channel_message_ids(value: Iterable[str] | None) -> tuple[str, ...]:
        """校验并规范摘要出站消息 ID；不接受空 ID 或嵌套结构。"""

        if value is None:
            return ()
        try:
            normalized = tuple(
                dict.fromkeys(str(item or "").strip() for item in value)
            )
        except TypeError as exc:
            raise ValueError("channel_message_ids 必须是字符串序列") from exc
        if any(not item for item in normalized):
            raise ValueError("channel_message_ids 不能包含空值")
        if len(normalized) > 64 or any(len(item) > 512 for item in normalized):
            raise ValueError("channel_message_ids 超出安全上限")
        return normalized

    @classmethod
    def _summary_channel_message_ids_json(cls, raw: object) -> tuple[str, ...]:
        try:
            value = json.loads(str(raw or "[]"))
        except json.JSONDecodeError as exc:
            raise StateError("摘要投递消息 ID JSON 损坏") from exc
        if not isinstance(value, list):
            raise StateError("摘要投递消息 ID JSON 根节点不是数组")
        return cls._summary_channel_message_ids(value)

    @staticmethod
    def _summary_state(row: sqlite3.Row) -> str:
        if row["delivered_at"] is not None:
            return "delivered"
        if row["uncertain_at"] is not None:
            return "uncertain"
        if row["submitted_at"] is not None:
            return "submitted"
        if row["claimed_at"] is not None:
            return "prepared" if row["prepared_at"] is not None else "claimed"
        if row["rejected_at"] is not None:
            return "rejected"
        return "prepared" if row["prepared_at"] is not None else "pending"

    @classmethod
    def _summary_delivery_from_row(
        cls, row: sqlite3.Row
    ) -> NotificationSummaryDelivery:
        channel_ids = cls._summary_channel_message_ids_json(
            row["channel_message_ids_json"]
        )
        return NotificationSummaryDelivery(
            delivery_id=str(row["event_key"]),
            event_key=str(row["event_key"]),
            code=str(row["notification_code"]),
            thread_id=str(row["notification_thread_id"]),
            turn_id=str(row["notification_turn_id"]),
            created_at=int(row["created_at"]),
            next_attempt_at=int(row["next_attempt_at"]),
            attempt_count=int(row["attempt_count"]),
            claimed_at=(None if row["claimed_at"] is None else int(row["claimed_at"])),
            message_text=str(row["message_text"] or ""),
            prepared_at=(None if row["prepared_at"] is None else int(row["prepared_at"])),
            submitted_at=(None if row["submitted_at"] is None else int(row["submitted_at"])),
            delivered_at=(None if row["delivered_at"] is None else int(row["delivered_at"])),
            rejected_at=(None if row["rejected_at"] is None else int(row["rejected_at"])),
            uncertain_at=(None if row["uncertain_at"] is None else int(row["uncertain_at"])),
            channel_message_ids=channel_ids,
            last_error=(None if row["last_error"] is None else str(row["last_error"])),
            state=cls._summary_state(row),
        )

    def _notification_summary_row_locked(
        self, event_key: str
    ) -> sqlite3.Row | None:
        return self._connection.execute(
            """
            SELECT s.event_key, s.created_at, s.next_attempt_at,
                   s.attempt_count, s.claimed_at, s.message_text,
                   s.prepared_at, s.submitted_at, s.delivered_at,
                   s.rejected_at, s.uncertain_at,
                   s.channel_message_ids_json, s.last_error,
                   n.code AS notification_code,
                   n.thread_id AS notification_thread_id,
                   n.turn_id AS notification_turn_id,
                   n.reply_kind AS notification_reply_kind,
                   n.expires_at AS notification_expires_at,
                   n.sent_at AS notification_sent_at,
                   n.discarded_at AS notification_discarded_at
            FROM notification_summary_deliveries AS s
            JOIN notifications AS n ON n.event_key=s.event_key
            WHERE s.event_key=?
            """,
            (event_key,),
        ).fetchone()

    def notification_summary_recovery_candidates(
        self,
        *,
        now: int | None = None,
        limit: int = 64,
    ) -> tuple[NotificationSummaryRecoveryCandidate, ...]:
        """读取可由官方父消息核验的摘要 ``result_unknown`` 候选。

        结果 unknown 已经越过飞书提交边界，普通摘要 worker 绝不能盲发。
        这里仅返回正文已冻结、仍未过期、有效 ``turn`` 通知且没有任何已知
        平台消息 ID 的记录；调用方随后必须用官方 ``message_id``、发送者、
        chat、时间窗、正文和 HMAC 做全部验证。此方法只读，不改变 outbox。
        """

        if isinstance(limit, bool):
            raise ValueError("摘要恢复候选 limit 必须是整数")
        try:
            maximum = int(limit)
        except (TypeError, ValueError) as exc:
            raise ValueError("摘要恢复候选 limit 必须是整数") from exc
        if not 1 <= maximum <= 64:
            raise ValueError("摘要恢复候选 limit 必须在 1 到 64 之间")
        timestamp = int(time.time()) if now is None else int(now)
        if timestamp < 0:
            raise ValueError("摘要恢复查询时间不能为负数")
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT s.event_key, s.created_at, s.message_text,
                       s.submitted_at, s.uncertain_at,
                       s.channel_message_ids_json,
                       n.code AS notification_code,
                       n.thread_id AS notification_thread_id,
                       n.turn_id AS notification_turn_id,
                       n.reply_kind AS notification_reply_kind,
                       n.expires_at AS notification_expires_at,
                       n.sent_at AS notification_sent_at,
                       n.discarded_at AS notification_discarded_at,
                       EXISTS(
                           SELECT 1 FROM notification_raw_bindings AS raw
                           WHERE raw.event_key=s.event_key
                       ) AS raw_binding_prepared
                FROM notification_summary_deliveries AS s
                JOIN notifications AS n ON n.event_key=s.event_key
                WHERE s.delivered_at IS NULL
                  AND s.uncertain_at IS NOT NULL
                  AND s.submitted_at IS NOT NULL
                  AND s.prepared_at IS NOT NULL
                  AND s.message_text<>''
                  AND n.reply_kind='turn'
                  AND n.sent_at IS NOT NULL
                  AND n.discarded_at IS NULL
                  AND n.expires_at>=?
                ORDER BY s.uncertain_at, s.event_key
                LIMIT ?
                """,
                (timestamp, maximum),
            ).fetchall()
        candidates: list[NotificationSummaryRecoveryCandidate] = []
        for row in rows:
            # A non-empty/malformed existing ID set is an inconsistent state and
            # must not be silently treated as recoverable. The parser raises
            # StateError for malformed JSON; valid non-empty sets are skipped.
            existing_ids = self._summary_channel_message_ids_json(
                row["channel_message_ids_json"]
            )
            if existing_ids:
                continue
            submitted = int(row["submitted_at"])
            uncertain = int(row["uncertain_at"])
            if uncertain < submitted:
                continue
            candidates.append(
                NotificationSummaryRecoveryCandidate(
                    event_key=str(row["event_key"]),
                    code=str(row["notification_code"]),
                    thread_id=str(row["notification_thread_id"]),
                    turn_id=str(row["notification_turn_id"]),
                    message_text=str(row["message_text"]),
                    created_at=int(row["created_at"]),
                    submitted_at=submitted,
                    uncertain_at=uncertain,
                    expires_at=int(row["notification_expires_at"]),
                    reply_kind=str(row["notification_reply_kind"]),
                    sent_at=int(row["notification_sent_at"]),
                    raw_binding_prepared=bool(row["raw_binding_prepared"]),
                )
            )
        return tuple(candidates)

    def recover_notification_summary_delivery(
        self,
        event_key: str,
        message_id: str,
        *,
        chat_id: str,
        now: int | None = None,
    ) -> bool:
        """在一个事务内恢复已实际到达的摘要父消息。

        调用方必须先用 ``CorrelationCodec`` 验证通知 code，并完成官方消息
        的 sender/chat/time/body 唯一校验；本方法只接受那个已经核验过的精确
        平台 ID。它不发送消息，也不把 unknown 重新变成可重试状态。
        """

        event = self._summary_event_key(event_key)
        identifier = self._notification_raw_identity(message_id, field="message_id")
        chat = self._notification_raw_identity(chat_id, field="chat_id")
        timestamp = int(time.time()) if now is None else int(now)
        if timestamp < 0:
            raise ValueError("摘要恢复时间不能为负数")
        with self._lock, self._connection:
            row = self._notification_summary_row_locked(event)
            if row is None:
                raise StateError("摘要投递记录不存在")
            existing_ids = self._summary_channel_message_ids_json(
                row["channel_message_ids_json"]
            )
            if row["delivered_at"] is not None:
                # Idempotent retry of the same proof is safe. A different ID
                # would indicate a conflicting external message and is rejected.
                return identifier in existing_ids
            if (
                row["uncertain_at"] is None
                or row["submitted_at"] is None
                or row["prepared_at"] is None
                or row["notification_reply_kind"] != "turn"
                or row["notification_sent_at"] is None
                or row["notification_discarded_at"] is not None
                or int(row["notification_expires_at"]) < timestamp
                or int(row["uncertain_at"]) < int(row["submitted_at"])
            ):
                return False
            if existing_ids:
                return False
            if not str(row["message_text"] or ""):
                return False

            # Both the platform-message alias and an already prepared raw
            # binding are committed or rolled back together. This method never
            # clears media rows and therefore cannot re-release/re-send images.
            self._bind_channel_messages_locked(
                event,
                (identifier,),
                allow_additional=True,
                timestamp=timestamp,
            )
            raw_binding = self._connection.execute(
                "SELECT finalized_at FROM notification_raw_bindings WHERE event_key=?",
                (event,),
            ).fetchone()
            if raw_binding is not None:
                if raw_binding["finalized_at"] is not None:
                    raise StateError("摘要原文绑定已完成但摘要消息 ID 缺失，拒绝猜测恢复")
                self._finalize_notification_raw_binding_locked(
                    event, chat, (identifier,), timestamp
                )
            encoded = json.dumps([identifier], ensure_ascii=False, separators=(",", ":"))
            cursor = self._connection.execute(
                """
                UPDATE notification_summary_deliveries
                SET delivered_at=?, uncertain_at=NULL,
                    channel_message_ids_json=?,
                    rejected_at=NULL, last_error=NULL
                WHERE event_key=? AND submitted_at IS NOT NULL
                  AND uncertain_at IS NOT NULL AND delivered_at IS NULL
                """,
                (timestamp, encoded, event),
            )
            if cursor.rowcount != 1:
                raise StateError("摘要恢复状态在事务中发生变化")
            return True

    def reserve_notification_summary(
        self,
        event_key: str,
        *,
        now: int | None = None,
        next_attempt_at: int | None = None,
    ) -> NotificationSummaryDelivery:
        """为已存在的父通知建立唯一摘要 outbox 记录。

        ``event_key`` 是稳定幂等键；表中不复制 thread/turn 或原始答复正文，
        这些字段在读取时从 ``notifications`` 联接。重复 reserve 只返回原记录。
        """

        normalized = self._summary_event_key(event_key)
        timestamp = int(time.time()) if now is None else int(now)
        scheduled = timestamp if next_attempt_at is None else int(next_attempt_at)
        if timestamp < 0 or scheduled < 0:
            raise ValueError("摘要投递时间不能为负数")
        with self._lock, self._connection:
            parent = self._connection.execute(
                "SELECT 1 FROM notifications WHERE event_key=?", (normalized,)
            ).fetchone()
            if parent is None:
                raise StateError("无法为不存在的通知保留摘要 outbox")
            self._connection.execute(
                """
                INSERT OR IGNORE INTO notification_summary_deliveries(
                    event_key, created_at, next_attempt_at, attempt_count,
                    channel_message_ids_json
                ) VALUES(?,?,?,?, '[]')
                """,
                (normalized, timestamp, scheduled, 0),
            )
            row = self._notification_summary_row_locked(normalized)
            if row is None:
                raise StateError("无法读取摘要投递 outbox 记录")
            return self._summary_delivery_from_row(row)

    def claim_notification_summary(
        self,
        event_key: str | None = None,
        *,
        now: int | None = None,
    ) -> NotificationSummaryDelivery | None:
        """原子占用一条可重试摘要；并发调用最多一个进程获得 claim。"""

        normalized = None if event_key is None else self._summary_event_key(event_key)
        timestamp = int(time.time()) if now is None else int(now)
        if timestamp < 0:
            raise ValueError("摘要投递时间不能为负数")
        event_predicate = "" if normalized is None else " AND s.event_key=?"
        parameters: tuple[Any, ...]
        if normalized is None:
            parameters = (timestamp,)
        else:
            parameters = (timestamp, normalized)
        with self._lock, self._connection:
            # UPDATE + 子查询在 SQLite 写事务内完成，避免两个 StateStore
            # 实例先后读到同一 pending 行而都认为自己取得了 claim。
            cursor = self._connection.execute(
                f"""
                UPDATE notification_summary_deliveries
                SET claimed_at=?, attempt_count=attempt_count+1,
                    rejected_at=NULL, last_error=NULL
                WHERE event_key=(
                    SELECT s.event_key
                    FROM notification_summary_deliveries AS s
                    JOIN notifications AS n ON n.event_key=s.event_key
                    WHERE s.delivered_at IS NULL
                      AND s.uncertain_at IS NULL
                      AND s.claimed_at IS NULL
                      AND n.sent_at IS NOT NULL
                      AND s.next_attempt_at<=?
                      {event_predicate}
                    ORDER BY s.created_at, s.event_key
                    LIMIT 1
                )
                  AND delivered_at IS NULL
                  AND uncertain_at IS NULL
                  AND claimed_at IS NULL
                RETURNING event_key
                """,
                (timestamp, *parameters),
            )
            claimed_row = cursor.fetchone()
            if claimed_row is None:
                return None
            claimed_event_key = str(claimed_row["event_key"])
            row = self._notification_summary_row_locked(claimed_event_key)
            if row is None:
                raise StateError("摘要 claim 后无法读取投递记录")
            return self._summary_delivery_from_row(row)

    def prepare_notification_summary(
        self,
        event_key: str,
        message_text: str,
        *,
        now: int | None = None,
    ) -> bool:
        """为当前 claim 写入摘要文本；同一已准备文本可幂等重放。"""

        normalized = self._summary_event_key(event_key)
        text = str(message_text)
        if not text.strip():
            raise ValueError("摘要 message_text 不能为空")
        if len(text.encode("utf-8")) > 128 * 1024:
            raise ValueError("摘要 message_text 超过 128 KiB 上限")
        timestamp = int(time.time()) if now is None else int(now)
        if timestamp < 0:
            raise ValueError("摘要准备时间不能为负数")
        with self._lock, self._connection:
            row = self._notification_summary_row_locked(normalized)
            if row is None:
                raise StateError("摘要投递记录不存在")
            if row["prepared_at"] is not None:
                if str(row["message_text"] or "") != text:
                    raise StateError("摘要投递已准备了不同正文，拒绝覆盖")
                return True
            if (
                row["claimed_at"] is None
                or row["submitted_at"] is not None
                or row["delivered_at"] is not None
                or row["uncertain_at"] is not None
            ):
                return False
            cursor = self._connection.execute(
                """
                UPDATE notification_summary_deliveries
                SET message_text=?, prepared_at=?
                WHERE event_key=? AND claimed_at IS NOT NULL
                  AND prepared_at IS NULL AND submitted_at IS NULL
                  AND delivered_at IS NULL AND uncertain_at IS NULL
                """,
                (text, timestamp, normalized),
            )
            return cursor.rowcount == 1

    def mark_notification_summary_submitted(
        self,
        event_key: str,
        channel_message_ids: Iterable[str] | None = None,
        *,
        now: int | None = None,
    ) -> bool:
        """落下外部发送提交边界；之后未知结果只能进入 uncertain。"""

        normalized = self._summary_event_key(event_key)
        supplied_ids = self._summary_channel_message_ids(channel_message_ids)
        timestamp = int(time.time()) if now is None else int(now)
        if timestamp < 0:
            raise ValueError("摘要提交时间不能为负数")
        encoded = json.dumps(supplied_ids, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connection:
            row = self._notification_summary_row_locked(normalized)
            if row is None:
                raise StateError("摘要投递记录不存在")
            existing_ids = self._summary_channel_message_ids_json(
                row["channel_message_ids_json"]
            )
            if supplied_ids and existing_ids and supplied_ids != existing_ids:
                raise StateError("摘要投递已绑定不同的渠道消息 ID")
            if row["delivered_at"] is not None:
                return True
            if row["uncertain_at"] is not None:
                return False
            if row["submitted_at"] is not None:
                return True
            if (
                row["claimed_at"] is None
                or row["prepared_at"] is None
                or row["delivered_at"] is not None
            ):
                return False
            if not supplied_ids:
                encoded = str(row["channel_message_ids_json"] or "[]")
            cursor = self._connection.execute(
                """
                UPDATE notification_summary_deliveries
                SET submitted_at=?, channel_message_ids_json=?
                WHERE event_key=? AND claimed_at IS NOT NULL
                  AND prepared_at IS NOT NULL AND submitted_at IS NULL
                  AND delivered_at IS NULL AND uncertain_at IS NULL
                """,
                (timestamp, encoded, normalized),
            )
            return cursor.rowcount == 1

    def mark_notification_summary_delivered(
        self,
        event_key: str,
        channel_message_ids: Iterable[str] | None = None,
        *,
        now: int | None = None,
    ) -> bool:
        """确认摘要已送达；无渠道消息证据时拒绝伪造 delivered。"""

        normalized = self._summary_event_key(event_key)
        supplied_ids = self._summary_channel_message_ids(channel_message_ids)
        timestamp = int(time.time()) if now is None else int(now)
        if timestamp < 0:
            raise ValueError("摘要送达时间不能为负数")
        with self._lock, self._connection:
            row = self._notification_summary_row_locked(normalized)
            if row is None:
                raise StateError("摘要投递记录不存在")
            existing_ids = self._summary_channel_message_ids_json(
                row["channel_message_ids_json"]
            )
            if supplied_ids and existing_ids and supplied_ids != existing_ids:
                raise StateError("摘要投递已绑定不同的渠道消息 ID")
            already_delivered = row["delivered_at"] is not None
            if row["uncertain_at"] is not None or (
                not already_delivered and row["submitted_at"] is None
            ):
                return False
            effective_ids = supplied_ids or existing_ids
            if not effective_ids:
                raise ValueError("确认摘要送达必须提供渠道消息 ID")
            encoded = json.dumps(effective_ids, ensure_ascii=False, separators=(",", ":"))
            parent = self._connection.execute(
                "SELECT channel_message_id FROM notifications WHERE event_key=?",
                (normalized,),
            ).fetchone()
            if parent is None:
                raise StateError("摘要父通知不存在")
            linked_ids = {
                str(item[0])
                for item in self._connection.execute(
                    "SELECT message_id FROM notification_message_ids WHERE event_key=?",
                    (normalized,),
                )
            }
            if len(linked_ids | set(effective_ids)) > 64:
                raise StateError("同一通知绑定了过多平台消息 ID")
            for message_id in effective_ids:
                linked = self._connection.execute(
                    "SELECT event_key FROM notification_message_ids WHERE message_id=?",
                    (message_id,),
                ).fetchone()
                if linked is not None and str(linked["event_key"]) != normalized:
                    raise StateError("平台消息 ID 已绑定到另一条通知")
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO notification_message_ids(
                        message_id, event_key, created_at
                    ) VALUES(?,?,?)
                    """,
                    (message_id, normalized, timestamp),
                )
            self._connection.execute(
                """
                UPDATE notifications
                SET channel_message_id=COALESCE(channel_message_id, ?)
                WHERE event_key=?
                """,
                (effective_ids[0], normalized),
            )
            if already_delivered:
                return True
            cursor = self._connection.execute(
                """
                UPDATE notification_summary_deliveries
                SET delivered_at=?, channel_message_ids_json=?,
                    rejected_at=NULL, last_error=NULL
                WHERE event_key=? AND submitted_at IS NOT NULL
                  AND delivered_at IS NULL AND uncertain_at IS NULL
                """,
                (timestamp, encoded, normalized),
            )
            return cursor.rowcount == 1

    def mark_notification_summary_delivered_with_raw_context(
        self,
        event_key: str,
        channel_message_ids: Iterable[str],
        *,
        chat_id: str,
        now: int | None = None,
    ) -> bool:
        """原子确认摘要送达、绑定全部分片并建立可回看的原文上下文。"""

        normalized = self._summary_event_key(event_key)
        supplied_ids = self._summary_channel_message_ids(channel_message_ids)
        if not supplied_ids:
            raise ValueError("确认摘要送达必须提供渠道消息 ID")
        chat = self._notification_raw_identity(chat_id, field="chat_id")
        timestamp = int(time.time()) if now is None else int(now)
        if timestamp < 0:
            raise ValueError("摘要送达时间不能为负数")
        encoded = json.dumps(
            supplied_ids, ensure_ascii=False, separators=(",", ":")
        )
        with self._lock, self._connection:
            row = self._notification_summary_row_locked(normalized)
            if row is None:
                raise StateError("摘要投递记录不存在")
            existing_ids = self._summary_channel_message_ids_json(
                row["channel_message_ids_json"]
            )
            if existing_ids and existing_ids != supplied_ids:
                raise StateError("摘要投递已绑定不同的渠道消息 ID")
            already_delivered = row["delivered_at"] is not None
            if row["uncertain_at"] is not None or (
                not already_delivered and row["submitted_at"] is None
            ):
                return False
            self._bind_channel_messages_locked(
                normalized,
                supplied_ids,
                allow_additional=True,
                timestamp=timestamp,
            )
            self._finalize_notification_raw_binding_locked(
                normalized, chat, supplied_ids, timestamp
            )
            if already_delivered:
                return True
            cursor = self._connection.execute(
                """
                UPDATE notification_summary_deliveries
                SET delivered_at=?, channel_message_ids_json=?,
                    rejected_at=NULL, last_error=NULL
                WHERE event_key=? AND submitted_at IS NOT NULL
                  AND delivered_at IS NULL AND uncertain_at IS NULL
                """,
                (timestamp, encoded, normalized),
            )
            if cursor.rowcount != 1:
                raise StateError("摘要送达状态在事务中发生变化")
            return True

    def release_notification_summary(
        self,
        event_key: str,
        error_code: str,
        *,
        allow_submitted: bool = False,
        next_attempt_at: int | None = None,
        now: int | None = None,
    ) -> bool:
        """释放可证明未被渠道接受的摘要 claim。

        默认拒绝已跨过提交边界的释放；只有调用方拿到明确的“未接受”证据
        才能显式传 ``allow_submitted=True``。"""

        normalized = self._summary_event_key(event_key)
        if not isinstance(allow_submitted, bool):
            raise ValueError("allow_submitted 必须是布尔值")
        timestamp = int(time.time()) if now is None else int(now)
        scheduled = timestamp if next_attempt_at is None else int(next_attempt_at)
        if timestamp < 0 or scheduled < 0:
            raise ValueError("摘要重试时间不能为负数")
        code = str(error_code or "rejected")[:120]
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE notification_summary_deliveries
                SET claimed_at=NULL, submitted_at=NULL, rejected_at=?,
                    next_attempt_at=?, last_error=?, channel_message_ids_json='[]'
                WHERE event_key=? AND claimed_at IS NOT NULL
                  AND delivered_at IS NULL AND uncertain_at IS NULL
                  AND (submitted_at IS NULL OR ?=1)
                """,
                (timestamp, scheduled, code, normalized, 1 if allow_submitted else 0),
            )
            return cursor.rowcount == 1

    def mark_notification_summary_uncertain(
        self,
        event_key: str,
        error_code: str,
        *,
        now: int | None = None,
    ) -> bool:
        """把已提交但外部结果未知的摘要冻结为终态，禁止盲重发。"""

        normalized = self._summary_event_key(event_key)
        timestamp = int(time.time()) if now is None else int(now)
        if timestamp < 0:
            raise ValueError("摘要不确定时间不能为负数")
        code = str(error_code or "result_unknown")[:120]
        with self._lock, self._connection:
            row = self._notification_summary_row_locked(normalized)
            if row is None:
                raise StateError("摘要投递记录不存在")
            if row["delivered_at"] is not None:
                return True
            if row["uncertain_at"] is not None:
                return True
            if row["submitted_at"] is None:
                return False
            cursor = self._connection.execute(
                """
                UPDATE notification_summary_deliveries
                SET uncertain_at=?, last_error=?
                WHERE event_key=? AND submitted_at IS NOT NULL
                  AND delivered_at IS NULL AND uncertain_at IS NULL
                """,
                (timestamp, code, normalized),
            )
            return cursor.rowcount == 1

    def notification_summary_delivery(
        self, event_key: str
    ) -> NotificationSummaryDelivery | None:
        normalized = self._summary_event_key(event_key)
        with self._lock:
            row = self._notification_summary_row_locked(normalized)
            return None if row is None else self._summary_delivery_from_row(row)

    def notification_summary_delivered(self, event_key: str) -> bool:
        normalized = self._summary_event_key(event_key)
        with self._lock:
            row = self._connection.execute(
                "SELECT delivered_at FROM notification_summary_deliveries WHERE event_key=?",
                (normalized,),
            ).fetchone()
        return row is not None and row["delivered_at"] is not None

    def notification_summary_terminal(self, event_key: str) -> bool:
        normalized = self._summary_event_key(event_key)
        with self._lock:
            row = self._connection.execute(
                """
                SELECT delivered_at, uncertain_at
                FROM notification_summary_deliveries WHERE event_key=?
                """,
                (normalized,),
            ).fetchone()
        return row is not None and (
            row["delivered_at"] is not None or row["uncertain_at"] is not None
        )

    def pending_notification_summary_count(self) -> int:
        """返回尚未 delivered/uncertain 的摘要数量（含当前 claim）。"""

        with self._lock:
            row = self._connection.execute(
                """
                SELECT COUNT(*) FROM notification_summary_deliveries
                WHERE delivered_at IS NULL AND uncertain_at IS NULL
                """
            ).fetchone()
        return int(row[0])

    def recover_interrupted_notification_summaries(
        self, *, now: int | None = None
    ) -> Mapping[str, int]:
        """服务重启恢复：未提交 claim 可重试，已提交 claim 一律冻结 unknown。"""

        timestamp = int(time.time()) if now is None else int(now)
        if timestamp < 0:
            raise ValueError("摘要恢复时间不能为负数")
        with self._lock, self._connection:
            unsubmitted = self._connection.execute(
                """
                UPDATE notification_summary_deliveries
                SET claimed_at=NULL, rejected_at=?, next_attempt_at=?,
                    last_error='restart_before_submit'
                WHERE claimed_at IS NOT NULL AND submitted_at IS NULL
                  AND delivered_at IS NULL AND uncertain_at IS NULL
                """,
                (timestamp, timestamp),
            )
            submitted = self._connection.execute(
                """
                UPDATE notification_summary_deliveries
                SET uncertain_at=?, last_error='restart_after_submit'
                WHERE submitted_at IS NOT NULL AND delivered_at IS NULL
                  AND uncertain_at IS NULL
                """,
                (timestamp,),
            )
        return {
            "unsubmitted_released": max(0, unsubmitted.rowcount),
            "submitted_uncertain": max(0, submitted.rowcount),
        }

    @staticmethod
    def _notification_raw_identity(value: object, *, field: str) -> str:
        normalized = str(value or "").strip()
        if not normalized or len(normalized) > 512:
            raise ValueError(f"{field} 不能为空且不得超过 512 字符")
        return normalized

    @staticmethod
    def _notification_raw_digest(value: object) -> str:
        normalized = str(value or "").strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", normalized) is None:
            raise ValueError("content_sha256 必须是 64 位十六进制摘要")
        return normalized

    @staticmethod
    def _notification_raw_fingerprint(value: object) -> str:
        normalized = str(value or "").strip()
        if not normalized or len(normalized) > 512:
            raise ValueError("fingerprint 不能为空且不得超过 512 字符")
        return normalized

    @staticmethod
    def _notification_raw_state(row: sqlite3.Row) -> str:
        if row["delivered_at"] is not None:
            return "delivered"
        if row["uncertain_at"] is not None:
            return "uncertain"
        if row["submitted_at"] is not None:
            return "submitted"
        if row["claimed_at"] is not None:
            return "prepared" if row["prepared_at"] is not None else "claimed"
        if row["rejected_at"] is not None:
            return "rejected"
        return "prepared" if row["prepared_at"] is not None else "pending"

    @classmethod
    def _notification_raw_delivery_from_row(
        cls,
        row: sqlite3.Row,
        *,
        is_new: bool = False,
    ) -> NotificationRawDelivery:
        return NotificationRawDelivery(
            delivery_id=str(row["delivery_id"]),
            inbound_message_id=str(row["inbound_message_id"]),
            parent_message_id=str(row["parent_message_id"]),
            event_key=str(row["event_key"]),
            sender_id=str(row["sender_id"]),
            chat_id=str(row["chat_id"]),
            thread_id=str(row["thread_id"]),
            turn_id=str(row["turn_id"]),
            content_sha256=str(row["content_sha256"]),
            fingerprint=str(row["fingerprint"]),
            created_at=int(row["created_at"]),
            next_attempt_at=int(row["next_attempt_at"]),
            attempt_count=int(row["attempt_count"]),
            claimed_at=(
                None if row["claimed_at"] is None else int(row["claimed_at"])
            ),
            response_kind=str(row["response_kind"] or ""),
            prepared_at=(
                None if row["prepared_at"] is None else int(row["prepared_at"])
            ),
            submitted_at=(
                None if row["submitted_at"] is None else int(row["submitted_at"])
            ),
            delivered_at=(
                None if row["delivered_at"] is None else int(row["delivered_at"])
            ),
            rejected_at=(
                None if row["rejected_at"] is None else int(row["rejected_at"])
            ),
            uncertain_at=(
                None if row["uncertain_at"] is None else int(row["uncertain_at"])
            ),
            result_message_ids=cls._summary_channel_message_ids_json(
                row["result_message_ids_json"]
            ),
            last_error_code=(
                None
                if row["last_error_code"] is None
                else str(row["last_error_code"])
            ),
            state=cls._notification_raw_state(row),
            is_new=is_new,
        )

    def prepare_notification_raw_binding(
        self,
        event_key: str,
        *,
        sender_id: str,
        thread_id: str,
        turn_id: str,
        content_sha256: str,
        now: int | None = None,
    ) -> None:
        """在发送完成摘要前冻结精确 turn 身份；不保存最终答复正文。"""

        event = self._summary_event_key(event_key)
        sender = self._notification_raw_identity(sender_id, field="sender_id")
        thread = self._notification_raw_identity(thread_id, field="thread_id")
        turn = self._notification_raw_identity(turn_id, field="turn_id")
        digest = self._notification_raw_digest(content_sha256)
        timestamp = int(time.time()) if now is None else int(now)
        if timestamp < 0:
            raise ValueError("原文绑定时间不能为负数")
        with self._lock, self._connection:
            parent = self._connection.execute(
                "SELECT thread_id, turn_id FROM notifications WHERE event_key=?",
                (event,),
            ).fetchone()
            if parent is None:
                raise StateError("原文绑定对应的父通知不存在")
            if str(parent["thread_id"]) != thread or str(parent["turn_id"]) != turn:
                raise StateError("原文绑定与父通知的 thread/turn 不一致")
            existing = self._connection.execute(
                "SELECT * FROM notification_raw_bindings WHERE event_key=?",
                (event,),
            ).fetchone()
            expected = (sender, thread, turn, digest)
            if existing is not None:
                actual = tuple(
                    str(existing[field])
                    for field in (
                        "sender_id",
                        "thread_id",
                        "turn_id",
                        "content_sha256",
                    )
                )
                if actual != expected:
                    raise StateError("同一通知已冻结不同的原文身份")
                return
            self._connection.execute(
                """
                INSERT INTO notification_raw_bindings(
                    event_key, sender_id, thread_id, turn_id,
                    content_sha256, created_at
                ) VALUES(?,?,?,?,?,?)
                """,
                (event, sender, thread, turn, digest, timestamp),
            )

    def _finalize_notification_raw_binding_locked(
        self,
        event: str,
        chat: str,
        identifiers: tuple[str, ...],
        timestamp: int,
    ) -> tuple[sqlite3.Row, ...]:
        binding = self._connection.execute(
            "SELECT * FROM notification_raw_bindings WHERE event_key=?",
            (event,),
        ).fetchone()
        if binding is None:
            raise StateError("原文绑定尚未准备")
        expected = (
            event,
            str(binding["sender_id"]),
            chat,
            str(binding["thread_id"]),
            str(binding["turn_id"]),
            str(binding["content_sha256"]),
        )
        for message_id in identifiers:
            linked = self._connection.execute(
                "SELECT event_key FROM notification_message_ids WHERE message_id=?",
                (message_id,),
            ).fetchone()
            if linked is None or str(linked["event_key"]) != event:
                raise StateError(
                    "原文上下文只能绑定到已确认属于同一通知的平台消息 ID"
                )
        for message_id in identifiers:
            existing = self._connection.execute(
                "SELECT * FROM notification_raw_contexts WHERE message_id=?",
                (message_id,),
            ).fetchone()
            if existing is not None:
                actual = tuple(
                    str(existing[field])
                    for field in (
                        "event_key",
                        "sender_id",
                        "chat_id",
                        "thread_id",
                        "turn_id",
                        "content_sha256",
                    )
                )
                if actual != expected:
                    raise StateError("平台消息 ID 已绑定到另一条原文上下文")
                continue
            self._connection.execute(
                """
                INSERT INTO notification_raw_contexts(
                    message_id, event_key, sender_id, chat_id, thread_id, turn_id,
                    content_sha256, created_at
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (message_id, *expected, timestamp),
            )
        self._connection.execute(
            """
            UPDATE notification_raw_bindings
            SET finalized_at=COALESCE(finalized_at, ?)
            WHERE event_key=?
            """,
            (timestamp, event),
        )
        return tuple(
            self._connection.execute(
                "SELECT * FROM notification_raw_contexts WHERE message_id=?",
                (message_id,),
            ).fetchone()
            for message_id in identifiers
        )

    def finalize_notification_raw_binding(
        self,
        event_key: str,
        *,
        chat_id: str,
        message_ids: Iterable[str],
        now: int | None = None,
    ) -> tuple[NotificationRawContext, ...]:
        """把所有完成摘要分片永久绑定到冻结 turn；可安全幂等重放。"""

        event = self._summary_event_key(event_key)
        chat = self._notification_raw_identity(chat_id, field="chat_id")
        identifiers = self._summary_channel_message_ids(message_ids)
        if not identifiers:
            raise ValueError("原文绑定至少需要一个平台消息 ID")
        timestamp = int(time.time()) if now is None else int(now)
        if timestamp < 0:
            raise ValueError("原文绑定完成时间不能为负数")
        with self._lock, self._connection:
            rows = self._finalize_notification_raw_binding_locked(
                event, chat, identifiers, timestamp
            )
        return tuple(
            NotificationRawContext(
                message_id=str(row["message_id"]),
                event_key=str(row["event_key"]),
                sender_id=str(row["sender_id"]),
                chat_id=str(row["chat_id"]),
                thread_id=str(row["thread_id"]),
                turn_id=str(row["turn_id"]),
                content_sha256=str(row["content_sha256"]),
                created_at=int(row["created_at"]),
            )
            for row in rows
            if row is not None
        )

    def notification_raw_binding_prepared(self, event_key: str) -> bool:
        event = self._summary_event_key(event_key)
        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM notification_raw_bindings WHERE event_key=?",
                (event,),
            ).fetchone()
        return row is not None

    def _notification_raw_context_candidates_locked(
        self, identifier: str
    ) -> tuple[NotificationRawContext, ...]:
        """返回某平台消息 ID 的全部唯一原文路由身份。

        正常的新记录同时存在 raw_context 和 raw outbox 结果，两者身份完全
        相同，因此按完整身份去重。若历史异常库把同一平台 ID 写进不同
        sender/chat/thread/turn，则调用方必须把多候选视为歧义并拒绝。
        """

        if self.read_only and not {
            "notification_raw_contexts",
            "notification_raw_deliveries",
        }.issubset(self._read_only_tables):
            return ()
        candidates: dict[tuple[str, ...], NotificationRawContext] = {}
        direct = self._connection.execute(
            "SELECT * FROM notification_raw_contexts WHERE message_id=?",
            (identifier,),
        ).fetchone()
        if direct is not None:
            context = NotificationRawContext(
                message_id=identifier,
                event_key=str(direct["event_key"]),
                sender_id=str(direct["sender_id"]),
                chat_id=str(direct["chat_id"]),
                thread_id=str(direct["thread_id"]),
                turn_id=str(direct["turn_id"]),
                content_sha256=str(direct["content_sha256"]),
                created_at=int(direct["created_at"]),
            )
            candidates[
                (
                    context.event_key,
                    context.sender_id,
                    context.chat_id,
                    context.thread_id,
                    context.turn_id,
                    context.content_sha256,
                )
            ] = context
        rows = self._connection.execute(
            """
            SELECT event_key, sender_id, chat_id, thread_id, turn_id,
                   content_sha256, created_at, result_message_ids_json
            FROM notification_raw_deliveries
            WHERE delivered_at IS NOT NULL AND uncertain_at IS NULL
            """
        ).fetchall()
        for row in rows:
            if identifier not in self._summary_channel_message_ids_json(
                row["result_message_ids_json"]
            ):
                continue
            context = NotificationRawContext(
                message_id=identifier,
                event_key=str(row["event_key"]),
                sender_id=str(row["sender_id"]),
                chat_id=str(row["chat_id"]),
                thread_id=str(row["thread_id"]),
                turn_id=str(row["turn_id"]),
                content_sha256=str(row["content_sha256"]),
                created_at=int(row["created_at"]),
            )
            candidates[
                (
                    context.event_key,
                    context.sender_id,
                    context.chat_id,
                    context.thread_id,
                    context.turn_id,
                    context.content_sha256,
                )
            ] = context
        return tuple(candidates.values())

    def notification_raw_context_for_message(
        self, message_id: str
    ) -> NotificationRawContext | None:
        identifier = self._notification_raw_identity(
            message_id, field="message_id"
        )
        with self._lock:
            candidates = self._notification_raw_context_candidates_locked(identifier)
        return candidates[0] if len(candidates) == 1 else None

    def notification_raw_context_exists_for_message(self, message_id: str) -> bool:
        """判断平台消息是否已有任意原文路由映射（包括身份歧义）。

        ``notification_raw_context_for_message`` 为了让调用方安全路由，
        会把多身份冲突折叠成 ``None``。恢复未知摘要时不能把这种 ``None``
        当成“从未见过”，否则一个同 ID 的不确定摘要可能触发官方取消息；
        此入口只回答是否存在，不暴露或猜测映射内容。
        """

        identifier = self._notification_raw_identity(
            message_id, field="message_id"
        )
        with self._lock:
            if self.read_only and not {
                "notification_raw_contexts",
                "notification_raw_deliveries",
            }.issubset(self._read_only_tables):
                return False
            direct = self._connection.execute(
                "SELECT 1 FROM notification_raw_contexts WHERE message_id=? LIMIT 1",
                (identifier,),
            ).fetchone()
            if direct is not None:
                return True
            rows = self._connection.execute(
                """
                SELECT result_message_ids_json
                FROM notification_raw_deliveries
                WHERE delivered_at IS NOT NULL AND uncertain_at IS NULL
                """
            ).fetchall()
            return any(
                identifier in self._summary_channel_message_ids_json(
                    row["result_message_ids_json"]
                )
                for row in rows
            )

    def _notification_raw_legacy_source_locked(
        self, message_id: str
    ) -> NotificationRawLegacySource | None:
        row = self._connection.execute(
            """
            SELECT ids.message_id, parent.event_key, parent.thread_id,
                   parent.turn_id, parent.reply_kind, parent.sent_at,
                   parent.discarded_at
            FROM notification_message_ids AS ids
            JOIN notifications AS parent USING(event_key)
            WHERE ids.message_id=?
            """,
            (message_id,),
        ).fetchone()
        if (
            row is None
            or row["sent_at"] is None
            or row["discarded_at"] is not None
            or str(row["reply_kind"]) != "turn"
        ):
            return None
        event_key = str(row["event_key"] or "")
        thread_id = str(row["thread_id"] or "")
        turn_id = str(row["turn_id"] or "")
        if (
            not thread_id
            or not turn_id
            or event_key != f"{thread_id}:{turn_id}:completed"
        ):
            return None
        return NotificationRawLegacySource(
            message_id=str(row["message_id"]),
            event_key=event_key,
            thread_id=thread_id,
            turn_id=turn_id,
        )

    def notification_raw_legacy_source_for_message(
        self, message_id: str
    ) -> NotificationRawLegacySource | None:
        """只读取证一条旧消息是否为精确 completed turn 通知。"""

        identifier = self._notification_raw_identity(
            message_id, field="message_id"
        )
        with self._lock:
            return self._notification_raw_legacy_source_locked(identifier)

    def materialize_notification_raw_legacy_context(
        self,
        source: NotificationRawLegacySource,
        *,
        sender_id: str,
        chat_id: str,
        content_sha256: str,
        now: int | None = None,
    ) -> NotificationRawContext:
        """按需升级一条旧完成通知，并只绑定本次引用的消息分片。

        notification 映射、冻结身份与 message context 在同一事务内再次校验并
        提交；任何冲突都会整体回滚。已有同事件上下文同时锁定 owner/chat，
        防止另一分片被跨聊天重新认领。
        """

        message_id = self._notification_raw_identity(
            source.message_id, field="message_id"
        )
        event = self._summary_event_key(source.event_key)
        thread = self._notification_raw_identity(
            source.thread_id, field="thread_id"
        )
        turn = self._notification_raw_identity(source.turn_id, field="turn_id")
        sender = self._notification_raw_identity(sender_id, field="sender_id")
        chat = self._notification_raw_identity(chat_id, field="chat_id")
        digest = self._notification_raw_digest(content_sha256)
        timestamp = int(time.time()) if now is None else int(now)
        if timestamp < 0:
            raise ValueError("旧原文绑定时间不能为负数")
        with self._lock, self._connection:
            current = self._notification_raw_legacy_source_locked(message_id)
            if current is None or (
                current.event_key,
                current.thread_id,
                current.turn_id,
            ) != (event, thread, turn):
                raise StateError("旧完成通知的持久映射已经变化或不再可用")

            scoped = self._connection.execute(
                """
                SELECT sender_id, chat_id
                FROM notification_raw_contexts
                WHERE event_key=?
                LIMIT 1
                """,
                (event,),
            ).fetchone()
            if scoped is not None and (
                str(scoped["sender_id"]),
                str(scoped["chat_id"]),
            ) != (sender, chat):
                raise StateError("旧完成通知已绑定到另一用户或聊天")

            binding = self._connection.execute(
                "SELECT * FROM notification_raw_bindings WHERE event_key=?",
                (event,),
            ).fetchone()
            expected = (sender, thread, turn, digest)
            if binding is None:
                self._connection.execute(
                    """
                    INSERT INTO notification_raw_bindings(
                        event_key, sender_id, thread_id, turn_id,
                        content_sha256, created_at
                    ) VALUES(?,?,?,?,?,?)
                    """,
                    (event, *expected, timestamp),
                )
            else:
                actual = tuple(
                    str(binding[field])
                    for field in (
                        "sender_id",
                        "thread_id",
                        "turn_id",
                        "content_sha256",
                    )
                )
                if actual != expected:
                    raise StateError("旧完成通知已冻结不同的原文身份")

            rows = self._finalize_notification_raw_binding_locked(
                event, chat, (message_id,), timestamp
            )
            row = rows[0]
            if row is None:
                raise StateError("旧完成通知原文上下文写入失败")
            return NotificationRawContext(
                message_id=str(row["message_id"]),
                event_key=str(row["event_key"]),
                sender_id=str(row["sender_id"]),
                chat_id=str(row["chat_id"]),
                thread_id=str(row["thread_id"]),
                turn_id=str(row["turn_id"]),
                content_sha256=str(row["content_sha256"]),
                created_at=int(row["created_at"]),
            )

    def reserve_notification_raw_delivery(
        self,
        context: NotificationRawContext,
        *,
        inbound_message_id: str,
        fingerprint: str,
        now: int | None = None,
    ) -> NotificationRawDelivery:
        """为一条入站 ``.原文`` 建立唯一、可恢复的 exactly-once outbox。"""

        inbound = self._notification_raw_identity(
            inbound_message_id, field="inbound_message_id"
        )
        fp = self._notification_raw_fingerprint(fingerprint)
        parent = self._notification_raw_identity(
            context.message_id, field="parent_message_id"
        )
        sender = self._notification_raw_identity(context.sender_id, field="sender_id")
        chat = self._notification_raw_identity(context.chat_id, field="chat_id")
        thread = self._notification_raw_identity(context.thread_id, field="thread_id")
        turn = self._notification_raw_identity(context.turn_id, field="turn_id")
        digest = self._notification_raw_digest(context.content_sha256)
        event = self._summary_event_key(context.event_key)
        timestamp = int(time.time()) if now is None else int(now)
        if timestamp < 0:
            raise ValueError("原文请求时间不能为负数")
        delivery_id = hashlib.sha256(
            f"notification-raw-v1\0{inbound}\0{fp}".encode("utf-8")
        ).hexdigest()
        expected = (parent, event, sender, chat, thread, turn, digest, fp)
        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT * FROM notification_raw_deliveries WHERE inbound_message_id=?",
                (inbound,),
            ).fetchone()
            is_new = False
            if existing is None:
                self._connection.execute(
                    """
                    INSERT INTO notification_raw_deliveries(
                        delivery_id, inbound_message_id, parent_message_id,
                        event_key, sender_id, chat_id, thread_id, turn_id, content_sha256,
                        fingerprint, created_at, next_attempt_at,
                        result_message_ids_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?, '[]')
                    """,
                    (
                        delivery_id,
                        inbound,
                        parent,
                        event,
                        sender,
                        chat,
                        thread,
                        turn,
                        digest,
                        fp,
                        timestamp,
                        timestamp,
                    ),
                )
                existing = self._connection.execute(
                    "SELECT * FROM notification_raw_deliveries WHERE delivery_id=?",
                    (delivery_id,),
                ).fetchone()
                is_new = True
            if existing is None:
                raise StateError("原文投递预留后无法读取")
            actual = tuple(
                str(existing[field])
                for field in (
                    "parent_message_id",
                    "event_key",
                    "sender_id",
                    "chat_id",
                    "thread_id",
                    "turn_id",
                    "content_sha256",
                    "fingerprint",
                )
            )
            if actual != expected:
                raise StateError("同一入站消息已绑定不同的原文请求")
            return self._notification_raw_delivery_from_row(
                existing, is_new=is_new
            )

    def claim_notification_raw_delivery(
        self, *, now: int | None = None
    ) -> NotificationRawDelivery | None:
        timestamp = int(time.time()) if now is None else int(now)
        if timestamp < 0:
            raise ValueError("原文投递时间不能为负数")
        with self._lock, self._connection:
            row = self._connection.execute(
                """
                UPDATE notification_raw_deliveries
                SET claimed_at=?, attempt_count=attempt_count+1,
                    rejected_at=NULL, last_error_code=NULL
                WHERE delivery_id=(
                    SELECT delivery_id FROM notification_raw_deliveries
                    WHERE delivered_at IS NULL AND uncertain_at IS NULL
                      AND claimed_at IS NULL AND next_attempt_at<=?
                    ORDER BY created_at, delivery_id LIMIT 1
                )
                  AND delivered_at IS NULL AND uncertain_at IS NULL
                  AND claimed_at IS NULL
                RETURNING *
                """,
                (timestamp, timestamp),
            ).fetchone()
        return (
            None
            if row is None
            else self._notification_raw_delivery_from_row(row)
        )

    def notification_raw_delivery(
        self, delivery_id: str
    ) -> NotificationRawDelivery | None:
        identifier = self._notification_raw_identity(
            delivery_id, field="delivery_id"
        )
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM notification_raw_deliveries WHERE delivery_id=?",
                (identifier,),
            ).fetchone()
        return (
            None
            if row is None
            else self._notification_raw_delivery_from_row(row)
        )

    def prepare_notification_raw_delivery(
        self,
        delivery_id: str,
        response_kind: str,
        *,
        now: int | None = None,
    ) -> bool:
        identifier = self._notification_raw_identity(
            delivery_id, field="delivery_id"
        )
        kind = str(response_kind or "").strip()
        if kind not in {
            "raw",
            "turn_unavailable",
            "turn_changed",
            "turn_not_completed",
        }:
            raise ValueError("不支持的原文响应类型")
        timestamp = int(time.time()) if now is None else int(now)
        if timestamp < 0:
            raise ValueError("原文准备时间不能为负数")
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM notification_raw_deliveries WHERE delivery_id=?",
                (identifier,),
            ).fetchone()
            if row is None:
                raise StateError("原文投递记录不存在")
            existing = str(row["response_kind"] or "")
            if row["prepared_at"] is not None:
                if existing != kind:
                    raise StateError("原文投递已准备了不同的响应类型")
                return True
            if (
                row["claimed_at"] is None
                or row["submitted_at"] is not None
                or row["delivered_at"] is not None
                or row["uncertain_at"] is not None
            ):
                return False
            cursor = self._connection.execute(
                """
                UPDATE notification_raw_deliveries
                SET response_kind=?, prepared_at=?
                WHERE delivery_id=? AND claimed_at IS NOT NULL
                  AND prepared_at IS NULL AND submitted_at IS NULL
                  AND delivered_at IS NULL AND uncertain_at IS NULL
                """,
                (kind, timestamp, identifier),
            )
            return cursor.rowcount == 1

    def mark_notification_raw_submitted(
        self, delivery_id: str, *, now: int | None = None
    ) -> bool:
        identifier = self._notification_raw_identity(
            delivery_id, field="delivery_id"
        )
        timestamp = int(time.time()) if now is None else int(now)
        if timestamp < 0:
            raise ValueError("原文提交时间不能为负数")
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM notification_raw_deliveries WHERE delivery_id=?",
                (identifier,),
            ).fetchone()
            if row is None:
                raise StateError("原文投递记录不存在")
            if row["delivered_at"] is not None:
                return True
            if row["uncertain_at"] is not None:
                return False
            if row["submitted_at"] is not None:
                return True
            if row["claimed_at"] is None or row["prepared_at"] is None:
                return False
            cursor = self._connection.execute(
                """
                UPDATE notification_raw_deliveries SET submitted_at=?
                WHERE delivery_id=? AND claimed_at IS NOT NULL
                  AND prepared_at IS NOT NULL AND submitted_at IS NULL
                  AND delivered_at IS NULL AND uncertain_at IS NULL
                """,
                (timestamp, identifier),
            )
            return cursor.rowcount == 1

    def mark_notification_raw_delivered(
        self,
        delivery_id: str,
        message_ids: Iterable[str],
        *,
        now: int | None = None,
    ) -> bool:
        identifier = self._notification_raw_identity(
            delivery_id, field="delivery_id"
        )
        identifiers = self._summary_channel_message_ids(message_ids)
        if not identifiers:
            raise ValueError("确认原文送达必须提供平台消息 ID")
        timestamp = int(time.time()) if now is None else int(now)
        if timestamp < 0:
            raise ValueError("原文送达时间不能为负数")
        encoded = json.dumps(identifiers, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM notification_raw_deliveries WHERE delivery_id=?",
                (identifier,),
            ).fetchone()
            if row is None:
                raise StateError("原文投递记录不存在")
            existing = self._summary_channel_message_ids_json(
                row["result_message_ids_json"]
            )
            if existing and existing != identifiers:
                raise StateError("原文投递已绑定不同的结果消息 ID")
            if row["delivered_at"] is not None:
                return True
            if row["submitted_at"] is None or row["uncertain_at"] is not None:
                return False
            event_key = str(row["event_key"])
            chat_id = str(row["chat_id"])
            # ``.原文`` 可能被飞书拆成多条平台消息。把每个已确认送达的
            # message_id 原子登记为原通知的别名，这样用户引用任意一段继续
            # 回复时仍能精确回到同一 Codex thread/turn；绝不靠正文猜测。
            self._bind_channel_messages_locked(
                event_key,
                identifiers,
                allow_additional=True,
                timestamp=timestamp,
            )
            self._finalize_notification_raw_binding_locked(
                event_key,
                chat_id,
                identifiers,
                timestamp,
            )
            cursor = self._connection.execute(
                """
                UPDATE notification_raw_deliveries
                SET delivered_at=?, result_message_ids_json=?,
                    rejected_at=NULL, last_error_code=NULL
                WHERE delivery_id=? AND submitted_at IS NOT NULL
                  AND delivered_at IS NULL AND uncertain_at IS NULL
                """,
                (timestamp, encoded, identifier),
            )
            return cursor.rowcount == 1

    def release_notification_raw_delivery(
        self,
        delivery_id: str,
        error_code: str,
        *,
        allow_submitted: bool = False,
        next_attempt_at: int | None = None,
        now: int | None = None,
    ) -> bool:
        identifier = self._notification_raw_identity(
            delivery_id, field="delivery_id"
        )
        timestamp = int(time.time()) if now is None else int(now)
        scheduled = timestamp if next_attempt_at is None else int(next_attempt_at)
        if timestamp < 0 or scheduled < 0:
            raise ValueError("原文重试时间不能为负数")
        code = str(error_code or "rejected")[:120]
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE notification_raw_deliveries
                SET claimed_at=NULL, submitted_at=NULL, rejected_at=?,
                    next_attempt_at=?, last_error_code=?,
                    response_kind='', prepared_at=NULL,
                    result_message_ids_json='[]'
                WHERE delivery_id=? AND claimed_at IS NOT NULL
                  AND delivered_at IS NULL AND uncertain_at IS NULL
                  AND (submitted_at IS NULL OR ?=1)
                """,
                (
                    timestamp,
                    scheduled,
                    code,
                    identifier,
                    1 if allow_submitted else 0,
                ),
            )
            return cursor.rowcount == 1

    def mark_notification_raw_uncertain(
        self,
        delivery_id: str,
        error_code: str,
        *,
        now: int | None = None,
    ) -> bool:
        identifier = self._notification_raw_identity(
            delivery_id, field="delivery_id"
        )
        timestamp = int(time.time()) if now is None else int(now)
        if timestamp < 0:
            raise ValueError("原文不确定时间不能为负数")
        code = str(error_code or "result_unknown")[:120]
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM notification_raw_deliveries WHERE delivery_id=?",
                (identifier,),
            ).fetchone()
            if row is None:
                raise StateError("原文投递记录不存在")
            if row["delivered_at"] is not None or row["uncertain_at"] is not None:
                return True
            if row["submitted_at"] is None:
                return False
            cursor = self._connection.execute(
                """
                UPDATE notification_raw_deliveries
                SET uncertain_at=?, last_error_code=?
                WHERE delivery_id=? AND submitted_at IS NOT NULL
                  AND delivered_at IS NULL AND uncertain_at IS NULL
                """,
                (timestamp, code, identifier),
            )
            return cursor.rowcount == 1

    def recover_interrupted_notification_raw_deliveries(
        self, *, now: int | None = None
    ) -> Mapping[str, int]:
        timestamp = int(time.time()) if now is None else int(now)
        if timestamp < 0:
            raise ValueError("原文恢复时间不能为负数")
        with self._lock, self._connection:
            unsubmitted = self._connection.execute(
                """
                UPDATE notification_raw_deliveries
                SET claimed_at=NULL, rejected_at=?, next_attempt_at=?,
                    last_error_code='restart_before_submit'
                WHERE claimed_at IS NOT NULL AND submitted_at IS NULL
                  AND delivered_at IS NULL AND uncertain_at IS NULL
                """,
                (timestamp, timestamp),
            )
            submitted = self._connection.execute(
                """
                UPDATE notification_raw_deliveries
                SET uncertain_at=?, last_error_code='restart_after_submit'
                WHERE submitted_at IS NOT NULL AND delivered_at IS NULL
                  AND uncertain_at IS NULL
                """,
                (timestamp,),
            )
        return {
            "unsubmitted_released": max(0, unsubmitted.rowcount),
            "submitted_uncertain": max(0, submitted.rowcount),
        }

    def reserve_notification(
        self,
        event: TurnEvent,
        code: str,
        message_text: str,
        ttl_hours: int,
        *,
        reply_kind: str = "turn",
        needs_summary: bool = False,
        raw_sender_id: str | None = None,
        raw_content_sha256: str | None = None,
    ) -> tuple[str, str]:
        """为事件创建稳定 outbox 记录；崩溃重启后复用相同编号与正文。

        ``needs_summary=True`` 会在同一 SQLite 事务中同时建立摘要 outbox。
        这样 placeholder 已准备发送时，即使服务在两步之间崩溃，摘要任务也
        不会因为尚未单独 reserve 而丢失。重复调用只补齐缺失的摘要记录，不
        覆盖既有父通知正文或其稳定编号。
        """

        if reply_kind not in {"turn", "rpc", "hook", "notice"}:
            raise ValueError("reply_kind 仅允许 turn、rpc、hook 或 notice")
        if not isinstance(needs_summary, bool):
            raise ValueError("needs_summary 必须是布尔值")
        if (raw_sender_id is None) != (raw_content_sha256 is None):
            raise ValueError("原文预绑定必须同时提供 sender_id 和 content_sha256")
        raw_sender = (
            None
            if raw_sender_id is None
            else self._notification_raw_identity(raw_sender_id, field="sender_id")
        )
        raw_digest = (
            None
            if raw_content_sha256 is None
            else self._notification_raw_digest(raw_content_sha256)
        )
        now = int(time.time())
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO notifications(
                    event_key, code, thread_id, turn_id, reply_kind,
                    message_text, created_at, expires_at
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    event.dedupe_key,
                    code,
                    event.thread_id,
                    event.turn_id,
                    reply_kind,
                    message_text,
                    now,
                    now + ttl_hours * 3600,
                ),
            )
            if needs_summary:
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO notification_summary_deliveries(
                        event_key, created_at, next_attempt_at, attempt_count,
                        channel_message_ids_json
                    ) VALUES(?,?,?,?, '[]')
                    """,
                    (event.dedupe_key, now, now, 0),
                )
            if raw_sender is not None and raw_digest is not None:
                existing_binding = self._connection.execute(
                    "SELECT * FROM notification_raw_bindings WHERE event_key=?",
                    (event.dedupe_key,),
                ).fetchone()
                expected = (
                    raw_sender,
                    event.thread_id,
                    event.turn_id,
                    raw_digest,
                )
                if existing_binding is None:
                    self._connection.execute(
                        """
                        INSERT INTO notification_raw_bindings(
                            event_key, sender_id, thread_id, turn_id,
                            content_sha256, created_at
                        ) VALUES(?,?,?,?,?,?)
                        """,
                        (event.dedupe_key, *expected, now),
                    )
                else:
                    actual = tuple(
                        str(existing_binding[field])
                        for field in (
                            "sender_id",
                            "thread_id",
                            "turn_id",
                            "content_sha256",
                        )
                    )
                    if actual != expected:
                        raise StateError("同一通知已冻结不同的原文身份")
            row = self._connection.execute(
                "SELECT code, message_text FROM notifications WHERE event_key=?",
                (event.dedupe_key,),
            ).fetchone()
        if row is None:
            raise StateError("无法创建通知 outbox 记录")
        return str(row["code"]), str(row["message_text"])

    def reserve_notification_with_summary(
        self,
        event: TurnEvent,
        code: str,
        placeholder_text: str,
        ttl_hours: int,
        *,
        reply_kind: str = "turn",
    ) -> tuple[str, str]:
        """显式命名的 placeholder + 摘要原子 reserve 便捷入口。"""

        return self.reserve_notification(
            event,
            code,
            placeholder_text,
            ttl_hours,
            reply_kind=reply_kind,
            needs_summary=True,
        )

    def reserve_notification_summary_only(
        self,
        event: TurnEvent,
        code: str,
        ttl_hours: int,
        *,
        reply_kind: str = "turn",
    ) -> tuple[str, str]:
        """原子保留仅摘要事件，不建立任何用户可见父占位消息。"""

        if reply_kind not in {"turn", "rpc", "hook"}:
            raise ValueError("reply_kind 仅允许 turn、rpc 或 hook")
        now = int(time.time())
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO notifications(
                    event_key, code, thread_id, turn_id, reply_kind,
                    message_text, created_at, expires_at, sent_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    event.dedupe_key,
                    code,
                    event.thread_id,
                    event.turn_id,
                    reply_kind,
                    "",
                    now,
                    now + ttl_hours * 3600,
                    now,
                ),
            )
            self._connection.execute(
                """
                INSERT OR IGNORE INTO notification_summary_deliveries(
                    event_key, created_at, next_attempt_at, attempt_count,
                    channel_message_ids_json
                ) VALUES(?,?,?,?, '[]')
                """,
                (event.dedupe_key, now, now, 0),
            )
            row = self._connection.execute(
                "SELECT code, message_text FROM notifications WHERE event_key=?",
                (event.dedupe_key,),
            ).fetchone()
        if row is None:
            raise StateError("无法创建仅摘要 outbox 记录")
        return str(row["code"]), str(row["message_text"])

    def discard_notification_summary(
        self,
        event_key: str,
        reason: str,
        *,
        now: int | None = None,
    ) -> bool:
        """把已判定为静默的摘要、父记录和媒体在同一事务内终结。"""

        normalized = self._summary_event_key(event_key)
        timestamp = int(time.time()) if now is None else int(now)
        if timestamp < 0:
            raise ValueError("静默消费时间不能为负数")
        error_code = str(reason or "notification_policy_silent")[:120]
        with self._lock, self._connection:
            row = self._notification_summary_row_locked(normalized)
            if row is None:
                return self._connection.execute(
                    "SELECT 1 FROM processed_turns WHERE event_key=?", (normalized,)
                ).fetchone() is not None
            if row["submitted_at"] is not None or row["delivered_at"] is not None or row["uncertain_at"] is not None:
                return False
            self._connection.execute(
                "UPDATE notifications SET discarded_at=COALESCE(discarded_at, ?) WHERE event_key=?",
                (timestamp, normalized),
            )
            self._connection.execute(
                """
                UPDATE notification_media_deliveries
                SET discarded_at=COALESCE(discarded_at, ?),
                    claimed_at=NULL, last_error_code=?
                WHERE event_key=? AND delivered_at IS NULL
                  AND uncertain_at IS NULL AND discarded_at IS NULL
                """,
                (timestamp, error_code, normalized),
            )
            self._connection.execute(
                "INSERT OR IGNORE INTO processed_turns(event_key, processed_at) VALUES(?,?)",
                (normalized, timestamp),
            )
            cursor = self._connection.execute(
                "DELETE FROM notification_summary_deliveries WHERE event_key=?",
                (normalized,),
            )
            return cursor.rowcount == 1

    def mark_sent(self, event_key: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE notifications SET sent_at=COALESCE(sent_at, ?) WHERE event_key=?",
                (int(time.time()), event_key),
            )

    def notification_sent(self, event_key: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT sent_at FROM notifications WHERE event_key=?", (event_key,)
            ).fetchone()
        return row is not None and row["sent_at"] is not None

    def pending_notification_texts(self) -> tuple[tuple[str, str], ...]:
        """返回崩溃前已保留、尚未确认送达的父通知正文。"""

        with self._lock:
            rows = self._connection.execute(
                """
                SELECT event_key, message_text FROM notifications
                WHERE sent_at IS NULL AND discarded_at IS NULL
                  AND message_text<>''
                ORDER BY created_at, event_key
                LIMIT 64
                """
            ).fetchall()
        return tuple((str(row["event_key"]), str(row["message_text"])) for row in rows)

    @staticmethod
    def _notification_media_id(
        event_key: str, artifact: GeneratedImageArtifact
    ) -> str:
        digest = hashlib.sha256(
            (
                "notification-media-v1\0"
                + event_key
                + "\0"
                + artifact.item_id
                + "\0"
                + artifact.sha256
            ).encode("utf-8")
        ).hexdigest()
        return f"nmedia-{digest}"

    def reserve_notification_media(
        self,
        event_key: str,
        artifacts: Iterable[GeneratedImageArtifact],
    ) -> tuple[str, ...]:
        """把同轮结构化图片幂等写入逐图 outbox。

        已存在记录必须逐字段一致；任何路径、摘要或顺序漂移都拒绝覆盖，避免
        一个稳定幂等键在重启后指向不同字节。
        """

        items = tuple(artifacts)
        if len(items) > 16:
            raise StateError("单轮生成图片数量超过安全上限")
        now = int(time.time())
        delivery_ids: list[str] = []
        with self._lock, self._connection:
            parent = self._connection.execute(
                "SELECT 1 FROM notifications WHERE event_key=?", (event_key,)
            ).fetchone()
            if parent is None:
                raise StateError("无法为不存在的通知保留图片 outbox")
            for ordinal, artifact in enumerate(items, start=1):
                delivery_id = self._notification_media_id(event_key, artifact)
                delivery_ids.append(delivery_id)
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO notification_media_deliveries(
                        delivery_id, event_key, ordinal, item_id, path, mime_type,
                        sha256, size, file_name, created_at, next_attempt_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        delivery_id,
                        event_key,
                        ordinal,
                        artifact.item_id,
                        artifact.path,
                        artifact.mime_type,
                        artifact.sha256,
                        artifact.size,
                        artifact.file_name,
                        now,
                        now,
                    ),
                )
                row = self._connection.execute(
                    """
                    SELECT event_key, ordinal, item_id, path, mime_type, sha256,
                           size, file_name
                    FROM notification_media_deliveries WHERE delivery_id=?
                    """,
                    (delivery_id,),
                ).fetchone()
                expected = (
                    event_key,
                    ordinal,
                    artifact.item_id,
                    artifact.path,
                    artifact.mime_type,
                    artifact.sha256,
                    artifact.size,
                    artifact.file_name,
                )
                actual = tuple(row) if row is not None else ()
                if actual != expected:
                    raise StateError("图片 outbox 的稳定身份对应了不同元数据")
        return tuple(delivery_ids)

    @staticmethod
    def _media_delivery_from_row(row: sqlite3.Row) -> NotificationMediaDelivery:
        return NotificationMediaDelivery(
            delivery_id=str(row["delivery_id"]),
            event_key=str(row["event_key"]),
            ordinal=int(row["ordinal"]),
            item_id=str(row["item_id"]),
            path=str(row["path"]),
            mime_type=str(row["mime_type"]),
            sha256=str(row["sha256"]),
            size=int(row["size"]),
            file_name=str(row["file_name"]),
            attempt_count=int(row["attempt_count"]),
            claimed_at=(None if row["claimed_at"] is None else int(row["claimed_at"])),
            delivered_at=(None if row["delivered_at"] is None else int(row["delivered_at"])),
            rejected_at=(None if row["rejected_at"] is None else int(row["rejected_at"])),
            uncertain_at=(None if row["uncertain_at"] is None else int(row["uncertain_at"])),
            discarded_at=(None if row["discarded_at"] is None else int(row["discarded_at"])),
            next_attempt_at=int(row["next_attempt_at"]),
            channel_message_id=(None if row["channel_message_id"] is None else str(row["channel_message_id"])),
            last_error_code=(None if row["last_error_code"] is None else str(row["last_error_code"])),
            warning_sent_at=(None if row["warning_sent_at"] is None else int(row["warning_sent_at"])),
        )

    def pending_notification_media(
        self,
        *,
        event_key: str | None = None,
        now: int | None = None,
    ) -> tuple[NotificationMediaDelivery, ...]:
        """返回可安全尝试的图片；同一通知严格按 ordinal 顺序。"""

        timestamp = int(time.time()) if now is None else int(now)
        parameters: list[Any] = [timestamp]
        event_filter = ""
        if event_key is not None:
            event_filter = " AND media.event_key=?"
            parameters.append(str(event_key))
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT media.*
                FROM notification_media_deliveries AS media
                JOIN notifications AS parent ON parent.event_key=media.event_key
                WHERE parent.sent_at IS NOT NULL
                  AND NOT EXISTS (
                    SELECT 1
                    FROM notification_summary_deliveries AS summary
                    WHERE summary.event_key=media.event_key
                      AND summary.delivered_at IS NULL
                      AND summary.uncertain_at IS NULL
                  )
                  AND media.delivered_at IS NULL
                  AND media.uncertain_at IS NULL
                  AND media.discarded_at IS NULL
                  AND media.claimed_at IS NULL
                  AND media.next_attempt_at<=?
                  {event_filter}
                  AND NOT EXISTS (
                    SELECT 1 FROM notification_media_deliveries AS earlier
                    WHERE earlier.event_key=media.event_key
                      AND earlier.ordinal<media.ordinal
                      AND earlier.delivered_at IS NULL
                      AND earlier.uncertain_at IS NULL
                      AND earlier.discarded_at IS NULL
                  )
                ORDER BY media.created_at, media.event_key, media.ordinal
                """,
                tuple(parameters),
            ).fetchall()
        return tuple(self._media_delivery_from_row(row) for row in rows)

    def claim_notification_media(
        self, delivery_id: str, *, now: int | None = None
    ) -> NotificationMediaDelivery | None:
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE notification_media_deliveries
                SET claimed_at=?, attempt_count=attempt_count+1
                WHERE delivery_id=? AND claimed_at IS NULL
                  AND delivered_at IS NULL AND uncertain_at IS NULL
                  AND discarded_at IS NULL AND next_attempt_at<=?
                """,
                (timestamp, delivery_id, timestamp),
            )
            if cursor.rowcount != 1:
                return None
            row = self._connection.execute(
                "SELECT * FROM notification_media_deliveries WHERE delivery_id=?",
                (delivery_id,),
            ).fetchone()
        return None if row is None else self._media_delivery_from_row(row)

    def mark_notification_media_delivered(
        self, delivery_id: str, message_id: str, *, now: int | None = None
    ) -> None:
        self.mark_notification_media_delivered_with_message_ids(
            delivery_id,
            (message_id,),
            now=now,
        )

    def mark_notification_media_delivered_with_message_ids(
        self,
        delivery_id: str,
        message_ids: Iterable[str],
        *,
        now: int | None = None,
    ) -> None:
        identifiers = self._summary_channel_message_ids(message_ids)
        if not identifiers:
            raise StateError("图片渠道返回了无效 message_id")
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
            row = self._connection.execute(
                """
                SELECT event_key FROM notification_media_deliveries
                WHERE delivery_id=? AND claimed_at IS NOT NULL
                  AND delivered_at IS NULL AND uncertain_at IS NULL
                  AND discarded_at IS NULL
                """,
                (delivery_id,),
            ).fetchone()
            if row is None:
                raise StateError("图片 outbox 不在可确认送达状态")
            self._bind_channel_messages_locked(
                str(row["event_key"]),
                identifiers,
                allow_additional=True,
                timestamp=timestamp,
            )
            cursor = self._connection.execute(
                """
                UPDATE notification_media_deliveries
                SET delivered_at=?, channel_message_id=?, last_error_code=NULL
                WHERE delivery_id=? AND claimed_at IS NOT NULL
                  AND delivered_at IS NULL AND uncertain_at IS NULL
                  AND discarded_at IS NULL
                """,
                (timestamp, identifiers[0], delivery_id),
            )
            if cursor.rowcount != 1:
                raise StateError("图片 outbox 不在可确认送达状态")

    def defer_notification_media(
        self,
        delivery_id: str,
        *,
        error_code: str,
        next_attempt_at: int,
        rejected: bool = False,
    ) -> None:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE notification_media_deliveries
                SET claimed_at=NULL, rejected_at=CASE WHEN ? THEN ? ELSE rejected_at END,
                    next_attempt_at=?, last_error_code=?
                WHERE delivery_id=? AND claimed_at IS NOT NULL
                  AND delivered_at IS NULL AND uncertain_at IS NULL
                  AND discarded_at IS NULL
                """,
                (
                    1 if rejected else 0,
                    int(time.time()),
                    int(next_attempt_at),
                    str(error_code or "unknown")[:128],
                    delivery_id,
                ),
            )
        if cursor.rowcount != 1:
            raise StateError("图片 outbox 不在可延后状态")

    def mark_notification_media_uncertain(
        self, delivery_id: str, *, error_code: str, now: int | None = None
    ) -> None:
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE notification_media_deliveries
                SET uncertain_at=?, last_error_code=?
                WHERE delivery_id=? AND claimed_at IS NOT NULL
                  AND delivered_at IS NULL AND uncertain_at IS NULL
                  AND discarded_at IS NULL
                """,
                (timestamp, str(error_code or "unknown")[:128], delivery_id),
            )
        if cursor.rowcount != 1:
            raise StateError("图片 outbox 不在可标记结果未知状态")

    def discard_notification_media(
        self, delivery_id: str, *, error_code: str, now: int | None = None
    ) -> None:
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE notification_media_deliveries
                SET discarded_at=?, last_error_code=?
                WHERE delivery_id=? AND claimed_at IS NOT NULL
                  AND delivered_at IS NULL AND uncertain_at IS NULL
                  AND discarded_at IS NULL
                """,
                (timestamp, str(error_code or "invalid")[:128], delivery_id),
            )
        if cursor.rowcount != 1:
            raise StateError("图片 outbox 不在可丢弃状态")

    def mark_notification_media_warning_sent(
        self, delivery_id: str, *, now: int | None = None
    ) -> bool:
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE notification_media_deliveries SET warning_sent_at=?
                WHERE delivery_id=? AND warning_sent_at IS NULL
                """,
                (timestamp, delivery_id),
            )
        return cursor.rowcount == 1

    def bind_channel_message(self, event_key: str, message_id: str) -> None:
        """把平台消息 ID 持久绑定到通知；已绑定不同 ID 时拒绝覆盖。"""

        normalized = str(message_id or "").strip()
        if not normalized or len(normalized) > 512:
            raise StateError("消息渠道返回了无效 message_id")
        self.bind_channel_messages(event_key, (normalized,), allow_additional=False)

    def _bind_channel_messages_locked(
        self,
        event_key: str,
        normalized: tuple[str, ...],
        *,
        allow_additional: bool,
        timestamp: int,
    ) -> None:
        row = self._connection.execute(
            "SELECT channel_message_id FROM notifications WHERE event_key=?",
            (event_key,),
        ).fetchone()
        if row is None:
            raise StateError("无法为不存在的通知绑定消息 ID")
        existing = str(row["channel_message_id"] or "")
        if existing and not allow_additional and existing != normalized[0]:
            raise StateError("同一通知返回了不同的平台消息 ID，拒绝覆盖")
        existing_ids = {
            str(item[0])
            for item in self._connection.execute(
                "SELECT message_id FROM notification_message_ids WHERE event_key=?",
                (event_key,),
            )
        }
        if existing_ids and not allow_additional and existing_ids != set(normalized):
            raise StateError("同一通知返回了不同的平台消息 ID，拒绝覆盖")
        if len(existing_ids | set(normalized)) > 64:
            raise StateError("同一通知绑定了过多平台消息 ID")
        try:
            self._connection.execute(
                "UPDATE notifications SET channel_message_id=? WHERE event_key=?",
                (existing or normalized[0], event_key),
            )
            for message_id in normalized:
                linked = self._connection.execute(
                    "SELECT event_key FROM notification_message_ids WHERE message_id=?",
                    (message_id,),
                ).fetchone()
                if linked is not None and str(linked["event_key"]) != event_key:
                    raise StateError("平台消息 ID 已绑定到另一条通知")
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO notification_message_ids(message_id, event_key, created_at)
                    VALUES(?,?,?)
                    """,
                    (message_id, event_key, timestamp),
                )
        except sqlite3.IntegrityError as exc:
            raise StateError("平台消息 ID 已绑定到另一条通知") from exc

    def bind_channel_messages(
        self,
        event_key: str,
        message_ids: tuple[str, ...] | list[str],
        *,
        allow_additional: bool = True,
    ) -> None:
        """绑定逻辑通知的全部平台分片 ID，任一分片都可用于引用关联。"""

        normalized = tuple(
            dict.fromkeys(str(item or "").strip() for item in message_ids)
        )
        if (
            not normalized
            or len(normalized) > 64
            or any(not item or len(item) > 512 for item in normalized)
        ):
            raise StateError("消息渠道返回了无效或过多的 message_id")
        with self._lock, self._connection:
            self._bind_channel_messages_locked(
                event_key,
                normalized,
                allow_additional=allow_additional,
                timestamp=int(time.time()),
            )

    def bind_channel_messages_with_raw_context(
        self,
        event_key: str,
        message_ids: Iterable[str],
        *,
        chat_id: str,
        now: int | None = None,
    ) -> None:
        """原子绑定通知分片及其原文上下文，消除已知成功后的本地崩溃缝隙。"""

        event = self._summary_event_key(event_key)
        identifiers = self._summary_channel_message_ids(message_ids)
        if not identifiers:
            raise ValueError("绑定通知至少需要一个平台消息 ID")
        chat = self._notification_raw_identity(chat_id, field="chat_id")
        timestamp = int(time.time()) if now is None else int(now)
        if timestamp < 0:
            raise ValueError("通知绑定时间不能为负数")
        with self._lock, self._connection:
            self._bind_channel_messages_locked(
                event,
                identifiers,
                allow_additional=True,
                timestamp=timestamp,
            )
            self._finalize_notification_raw_binding_locked(
                event, chat, identifiers, timestamp
            )

    def code_for_channel_message(self, message_id: str) -> str | None:
        """按被引用的平台消息 ID 查找 HMAC 通知编号。"""

        normalized = str(message_id or "").strip()
        if not normalized:
            return None
        with self._lock:
            raw_candidates = self._notification_raw_context_candidates_locked(normalized)
            if len(raw_candidates) > 1:
                return None
            row = self._connection.execute(
                """
                SELECT notifications.code
                FROM notification_message_ids
                JOIN notifications USING(event_key)
                WHERE notification_message_ids.message_id=?
                  AND notifications.sent_at IS NOT NULL
                """,
                (normalized,),
            ).fetchone()
            if row is None:
                # 兼容迁移中断前的旧库；正常 v5 初始化已完成回填。
                row = self._connection.execute(
                    """
                    SELECT code FROM notifications
                    WHERE channel_message_id=? AND sent_at IS NOT NULL
                    """,
                    (normalized,),
                ).fetchone()
            if row is None:
                # 兼容 schema21 已经送达、但在“原文分片可继续回复”补丁前
                # 创建的记录。这里只按平台 message_id 做精确匹配，不读取或
                # 比较原文；新投递会在 mark_notification_raw_delivered 中走
                # 上面的索引化原子绑定，因此该分支只服务历史记录。
                raw_context = raw_candidates[0] if raw_candidates else None
                row = self._connection.execute(
                    """
                    SELECT code FROM notifications
                    WHERE event_key=? AND sent_at IS NOT NULL
                    """,
                    (raw_context.event_key if raw_context is not None else "",),
                ).fetchone()
        return str(row["code"]) if row is not None else None

    @staticmethod
    def _staged_identity(value: object, label: str) -> str:
        normalized = str(value or "").strip()
        if not normalized or len(normalized) > 512:
            raise ValueError(f"{label} 不能为空且不得超过 512 字符")
        return normalized

    @staticmethod
    def _staged_attachment(value: Mapping[str, Any]) -> dict[str, Any]:
        path = str(value.get("path") or "").strip()
        mime_type = str(value.get("mime_type") or "").strip().casefold()
        sha256 = str(value.get("sha256") or "").strip().casefold()
        try:
            size = int(value.get("size") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("暂存图片大小无效") from exc
        if not path or len(path) > 4096:
            raise ValueError("暂存图片路径无效")
        if mime_type not in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
            raise ValueError("暂存图片格式不受支持")
        if len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256):
            raise ValueError("暂存图片 SHA-256 无效")
        if not 0 < size <= 20 * 1024 * 1024:
            raise ValueError("暂存图片大小超出限制")
        return {
            "path": path,
            "mime_type": mime_type,
            "sha256": sha256,
            "size": size,
        }

    @classmethod
    def _decode_staged_attachments(cls, raw: object) -> tuple[dict[str, Any], ...]:
        try:
            parsed = json.loads(str(raw or ""))
        except json.JSONDecodeError as exc:
            raise StateError("图片暂存记录不是有效 JSON") from exc
        if not isinstance(parsed, list) or not parsed:
            raise StateError("图片暂存记录缺少附件")
        try:
            attachments = tuple(
                cls._staged_attachment(item)
                for item in parsed
                if isinstance(item, Mapping)
            )
        except ValueError as exc:
            raise StateError("图片暂存记录字段无效") from exc
        if len(attachments) != len(parsed):
            raise StateError("图片暂存记录包含非对象附件")
        if (
            len(attachments) > _STAGED_IMAGE_MAX_COUNT
            or sum(int(item["size"]) for item in attachments)
            > _STAGED_IMAGE_MAX_TOTAL_BYTES
        ):
            raise StateError("图片暂存记录超出数量或总大小限制")
        return attachments

    @staticmethod
    def _decode_staged_message_ids(raw: object) -> tuple[str, ...]:
        try:
            parsed = json.loads(str(raw or ""))
        except json.JSONDecodeError as exc:
            raise StateError("图片暂存来源不是有效 JSON") from exc
        if (
            not isinstance(parsed, list)
            or not parsed
            or any(not isinstance(item, str) or not item.strip() for item in parsed)
        ):
            raise StateError("图片暂存来源无效")
        normalized = tuple(dict.fromkeys(item.strip() for item in parsed))
        if len(normalized) > 64 or any(
            len(item) > 512 for item in normalized
        ):
            raise StateError("图片暂存来源过多或过长")
        return normalized

    def stage_image_reply(
        self,
        *,
        sender_id: str,
        chat_id: str,
        reply_to_message_id: str,
        source_message_id: str,
        attachments: Iterable[Mapping[str, Any]],
        ttl_seconds: int,
        now: int | None = None,
    ) -> tuple[int, bool, int]:
        """暂存手机端分开发送的图片；同一引用可累加最多五张。"""

        sender = self._staged_identity(sender_id, "sender_id")
        chat = self._staged_identity(chat_id, "chat_id")
        reply_to = self._staged_identity(reply_to_message_id, "reply_to_message_id")
        source_message = self._staged_identity(source_message_id, "source_message_id")
        if not 1 <= int(ttl_seconds) <= 24 * 60 * 60:
            raise ValueError("ttl_seconds 必须介于 1 秒和 24 小时之间")
        incoming = tuple(self._staged_attachment(item) for item in attachments)
        if not incoming:
            raise ValueError("至少需要一张可暂存图片")
        timestamp = int(time.time()) if now is None else int(now)
        expires_at = timestamp + int(ttl_seconds)
        with self._lock, self._connection:
            row = self._connection.execute(
                """
                SELECT reply_to_message_id, attachments_json,
                       source_message_ids_json, created_at, expires_at
                FROM staged_image_replies WHERE sender_id=? AND chat_id=?
                """,
                (sender, chat),
            ).fetchone()
            append = (
                row is not None
                and int(row["expires_at"]) >= timestamp
                and str(row["reply_to_message_id"]) == reply_to
            )
            replaced = row is not None and not append
            existing = self._decode_staged_attachments(row["attachments_json"]) if append else ()
            existing_sources = (
                self._decode_staged_message_ids(row["source_message_ids_json"])
                if append
                else ()
            )
            merged_by_digest = {
                (str(item["sha256"]), str(item["path"])): item for item in existing
            }
            for item in incoming:
                merged_by_digest[(str(item["sha256"]), str(item["path"]))] = item
            merged = tuple(merged_by_digest.values())
            if len(merged) > _STAGED_IMAGE_MAX_COUNT:
                raise ValueError("一次最多暂存 5 张图片")
            if sum(int(item["size"]) for item in merged) > _STAGED_IMAGE_MAX_TOTAL_BYTES:
                raise ValueError("暂存图片总大小不得超过 50 MB")
            sources = tuple(dict.fromkeys((*existing_sources, source_message)))
            if len(sources) > 64:
                raise ValueError("同一次图片暂存的飞书来源消息过多")
            created_at = int(row["created_at"]) if append else timestamp
            self._connection.execute(
                """
                INSERT INTO staged_image_replies(
                    sender_id, chat_id, reply_to_message_id, attachments_json,
                    source_message_ids_json, created_at, expires_at
                ) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(sender_id, chat_id) DO UPDATE SET
                    reply_to_message_id=excluded.reply_to_message_id,
                    attachments_json=excluded.attachments_json,
                    source_message_ids_json=excluded.source_message_ids_json,
                    created_at=excluded.created_at,
                    expires_at=excluded.expires_at
                """,
                (
                    sender,
                    chat,
                    reply_to,
                    json.dumps(merged, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(sources, ensure_ascii=False, separators=(",", ":")),
                    created_at,
                    expires_at,
                ),
            )
        return len(merged), replaced, expires_at

    def staged_image_reply(
        self,
        sender_id: str,
        chat_id: str,
        *,
        now: int | None = None,
    ) -> dict[str, Any] | None:
        """返回同一用户私聊内未过期的图片暂存。"""

        sender = self._staged_identity(sender_id, "sender_id")
        chat = self._staged_identity(chat_id, "chat_id")
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM staged_image_replies WHERE expires_at<?",
                (timestamp,),
            )
            row = self._connection.execute(
                "SELECT * FROM staged_image_replies WHERE sender_id=? AND chat_id=?",
                (sender, chat),
            ).fetchone()
            if row is None:
                return None
            return {
                "sender_id": sender,
                "chat_id": chat,
                "reply_to_message_id": str(row["reply_to_message_id"]),
                "attachments": self._decode_staged_attachments(row["attachments_json"]),
                "source_message_ids": self._decode_staged_message_ids(
                    row["source_message_ids_json"]
                ),
                "created_at": int(row["created_at"]),
                "expires_at": int(row["expires_at"]),
            }

    def clear_staged_image_reply(self, sender_id: str, chat_id: str) -> bool:
        sender = self._staged_identity(sender_id, "sender_id")
        chat = self._staged_identity(chat_id, "chat_id")
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "DELETE FROM staged_image_replies WHERE sender_id=? AND chat_id=?",
                (sender, chat),
            )
        return cursor.rowcount == 1

    def consume_reply(
        self,
        code: str,
        reply_fingerprint: str,
        codec: CorrelationCodec,
        *,
        reply_text: str = "",
        now: int | None = None,
    ) -> tuple[str, str, str] | None:
        """原子消费 RPC/Hook 一次性编号；普通 turn 使用独立子投递。"""

        if not codec.valid(code) or not reply_fingerprint or not str(reply_text).strip():
            return None
        persisted_reply = str(reply_text).strip()
        if len(persisted_reply) > 50_000:
            return None
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
            row = self._connection.execute(
                """
                SELECT thread_id, turn_id, reply_kind FROM notifications
                WHERE code=? AND sent_at IS NOT NULL AND consumed_at IS NULL
                  AND reply_kind IN ('rpc','hook') AND expires_at>=?
                """,
                (code, timestamp),
            ).fetchone()
            if row is None:
                return None
            try:
                cursor = self._connection.execute(
                    """
                    UPDATE notifications
                    SET consumed_at=?, reply_fingerprint=?, reply_text=?
                    WHERE code=? AND consumed_at IS NULL
                    """,
                    (timestamp, reply_fingerprint, persisted_reply, code),
                )
            except sqlite3.IntegrityError:
                return None
            if cursor.rowcount != 1:
                return None
        return str(row["thread_id"]), str(row["turn_id"]), str(row["reply_kind"])

    def peek_reply(
        self,
        code: str,
        codec: CorrelationCodec,
        *,
        now: int | None = None,
    ) -> tuple[str, str, str] | None:
        """只读检查回复路由；turn 在有效期内可重复使用。"""

        status, mapping = self.inspect_reply_route(code, codec, now=now)
        return mapping if status == "ready" else None

    def inspect_reply_route(
        self,
        code: str,
        codec: CorrelationCodec,
        *,
        now: int | None = None,
    ) -> tuple[str, tuple[str, str, str] | None]:
        """返回精确路由状态：ready/invalid/missing/expired/consumed。"""

        if not codec.valid(code):
            return "invalid", None
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock:
            row = self._connection.execute(
                """
                SELECT thread_id, turn_id, reply_kind, expires_at, consumed_at
                FROM notifications WHERE code=? AND sent_at IS NOT NULL
                """,
                (code,),
            ).fetchone()
        if row is None:
            return "missing", None
        mapping = (
            str(row["thread_id"]),
            str(row["turn_id"]),
            str(row["reply_kind"]),
        )
        if int(row["expires_at"]) < timestamp:
            return "expired", mapping
        if mapping[2] != "turn" and row["consumed_at"] is not None:
            return "consumed", mapping
        return "ready", mapping

    @staticmethod
    def _delivery_state(row: sqlite3.Row) -> str:
        if row["discarded_at"] is not None:
            return "discarded"
        if row["delivered_at"] is not None:
            return "delivered"
        if row["claimed_at"] is not None:
            return "uncertain"
        return "pending"

    def enqueue_turn_reply(
        self,
        code: str,
        inbound_message_id: str,
        reply_fingerprint: str,
        codec: CorrelationCodec,
        *,
        reply_text: str,
        receipt_required: bool = False,
        now: int | None = None,
    ) -> TurnReplyDelivery | None:
        """为普通引用回复创建独立幂等子投递；重复事件返回原记录。"""

        persisted_reply = str(reply_text).strip()
        fingerprint = str(reply_fingerprint or "").strip()
        inbound_id = str(inbound_message_id or "").strip()
        if not codec.valid(code) or not persisted_reply or not fingerprint:
            return None
        if len(persisted_reply) > 50_000 or len(fingerprint) > 512:
            return None
        if not inbound_id:
            inbound_id = f"fingerprint:{fingerprint}"
        if len(inbound_id) > 512:
            return None
        timestamp = int(time.time()) if now is None else int(now)
        digest = hashlib.sha256(
            f"reply-delivery-v1\0{code}\0{inbound_id}\0{fingerprint}".encode("utf-8")
        ).hexdigest()
        delivery_id = f"reply-{digest}"
        with self._lock, self._connection:
            parent = self._connection.execute(
                """
                SELECT thread_id, turn_id FROM notifications
                WHERE code=? AND sent_at IS NOT NULL AND reply_kind='turn'
                  AND expires_at>=?
                """,
                (code, timestamp),
            ).fetchone()
            if parent is None:
                return None
            existing = self._connection.execute(
                """
                SELECT * FROM reply_deliveries
                WHERE parent_code=? AND inbound_message_id=?
                """,
                (code, inbound_id),
            ).fetchone()
            is_new = existing is None
            if existing is None:
                self._connection.execute(
                    """
                    INSERT INTO reply_deliveries(
                        delivery_id, parent_code, inbound_message_id,
                        reply_fingerprint, reply_text, created_at, receipt_required
                    ) VALUES(?,?,?,?,?,?,?)
                    """,
                    (
                        delivery_id,
                        code,
                        inbound_id,
                        fingerprint,
                        persisted_reply,
                        timestamp,
                        1 if receipt_required else 0,
                    ),
                )
                existing = self._connection.execute(
                    "SELECT * FROM reply_deliveries WHERE delivery_id=?",
                    (delivery_id,),
                ).fetchone()
            elif (
                str(existing["reply_fingerprint"]) != fingerprint
                or str(existing["reply_text"] or "") != persisted_reply
            ):
                raise StateError("同一入站 message_id 对应了不同回复内容，拒绝覆盖")
        if existing is None:
            raise StateError("无法创建普通回复子投递")
        return TurnReplyDelivery(
            delivery_id=str(existing["delivery_id"]),
            parent_code=code,
            thread_id=str(parent["thread_id"]),
            turn_id=str(parent["turn_id"]),
            reply_text=str(existing["reply_text"] or ""),
            fingerprint=str(existing["reply_fingerprint"]),
            sequence=int(existing["sequence"]),
            is_new=is_new,
            state=self._delivery_state(existing),
        )

    def pending_turn_replies(self) -> list[tuple[str, str, str, str]]:
        """返回已持久接收、尚未进入非幂等提交阶段的普通回复。"""

        with self._lock:
            rows = self._connection.execute(
                """
                SELECT d.delivery_id, n.thread_id, d.reply_text, d.reply_fingerprint
                FROM reply_deliveries AS d
                JOIN notifications AS n ON n.code=d.parent_code
                WHERE d.claimed_at IS NULL
                  AND d.delivered_at IS NULL
                  AND d.discarded_at IS NULL
                  AND d.reply_text IS NOT NULL
                ORDER BY d.sequence
                """
            ).fetchall()
        return [
            (
                str(row["delivery_id"]),
                str(row["thread_id"]),
                str(row["reply_text"]),
                str(row["reply_fingerprint"]),
            )
            for row in rows
        ]

    def pending_turn_reply_consumed_at(self, code: str) -> int | None:
        """返回普通回复开始安全等待的时间；只允许尚未 claim 的记录。"""

        with self._lock:
            row = self._connection.execute(
                """
                SELECT created_at FROM reply_deliveries
                WHERE delivery_id=?
                  AND claimed_at IS NULL
                  AND delivered_at IS NULL
                  AND discarded_at IS NULL
                """,
                (code,),
            ).fetchone()
        return int(row["created_at"]) if row is not None else None

    def uncertain_turn_replies(self) -> list[str]:
        """列出已进入非幂等提交临界区、但未确认投递的编号。"""

        with self._lock:
            rows = self._connection.execute(
                """
                SELECT delivery_id FROM reply_deliveries
                WHERE claimed_at IS NOT NULL AND delivered_at IS NULL
                  AND discarded_at IS NULL
                ORDER BY claimed_at, sequence
                """
            ).fetchall()
        return [str(row["delivery_id"]) for row in rows]

    def claim_turn_reply(self, code: str, *, now: int | None = None) -> bool:
        """在 ``turn/start`` 前原子进入不可自动重试的临界区。"""

        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE reply_deliveries SET claimed_at=?
                WHERE delivery_id=?
                  AND claimed_at IS NULL
                  AND delivered_at IS NULL
                  AND discarded_at IS NULL
                """,
                (timestamp, code),
            )
        return cursor.rowcount == 1

    def discard_stale_pending_turn_replies(
        self,
        expected_count: int,
        *,
        older_than_seconds: int,
        now: int | None = None,
    ) -> int:
        """停机维护时精确丢弃全部陈旧、未 claim 的普通回复。

        同时核对总数与最小年龄，避免在操作期间误丢刚收到的新回复；正文会立即清空。
        """

        if expected_count < 0:
            raise ValueError("expected_count 不能为负数")
        if older_than_seconds < 300:
            raise ValueError("older_than_seconds 不能小于 300 秒")
        timestamp = int(time.time()) if now is None else int(now)
        cutoff = timestamp - int(older_than_seconds)
        predicate = """
            created_at IS NOT NULL
            AND claimed_at IS NULL
            AND delivered_at IS NULL
            AND discarded_at IS NULL
            AND reply_text IS NOT NULL
        """
        with self._lock, self._connection:
            total = int(
                self._connection.execute(
                    f"SELECT COUNT(*) FROM reply_deliveries WHERE {predicate}"
                ).fetchone()[0]
            )
            eligible = int(
                self._connection.execute(
                    f"SELECT COUNT(*) FROM reply_deliveries WHERE {predicate} AND created_at<=?",
                    (cutoff,),
                ).fetchone()[0]
            )
            if total != expected_count or eligible != expected_count:
                raise StateError(
                    "待丢弃回复的数量或年龄在确认期间发生变化；已拒绝操作"
                )
            cursor = self._connection.execute(
                f"""
                UPDATE reply_deliveries
                SET discarded_at=?, reply_text=NULL
                WHERE {predicate} AND created_at<=?
                """,
                (timestamp, cutoff),
            )
        if cursor.rowcount != expected_count:
            raise StateError("陈旧回复丢弃数量不一致")
        return max(0, cursor.rowcount)

    def mark_reply_delivered(self, code: str, *, now: int | None = None) -> None:
        """记录 Codex 已明确接受 ``turn/start``，后续不得再次提交正文。"""

        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE reply_deliveries SET delivered_at=?
                WHERE delivery_id=?
                  AND claimed_at IS NOT NULL
                  AND delivered_at IS NULL
                """,
                (timestamp, code),
            )
        if cursor.rowcount != 1:
            raise StateError("回复投递状态不一致")

    def resolve_uncertain_reply(
        self,
        code: str,
        *,
        delivered: bool,
        now: int | None = None,
    ) -> bool:
        """按用户人工核对结果解决未知投递；调用方必须先验证 HMAC 和停机状态。"""

        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
            rows = self._connection.execute(
                """
                SELECT delivery_id FROM reply_deliveries
                WHERE (delivery_id=? OR parent_code=?)
                  AND claimed_at IS NOT NULL AND delivered_at IS NULL
                  AND discarded_at IS NULL
                ORDER BY sequence
                """,
                (code, code),
            ).fetchall()
            if len(rows) != 1:
                return False
            delivery_id = str(rows[0]["delivery_id"])
            if delivered:
                cursor = self._connection.execute(
                    """
                    UPDATE reply_deliveries SET delivered_at=?
                    WHERE delivery_id=?
                      AND claimed_at IS NOT NULL AND delivered_at IS NULL
                    """,
                    (timestamp, delivery_id),
                )
            else:
                cursor = self._connection.execute(
                    """
                    UPDATE reply_deliveries SET claimed_at=NULL
                    WHERE delivery_id=?
                      AND claimed_at IS NOT NULL AND delivered_at IS NULL
                    """,
                    (delivery_id,),
                )
        return cursor.rowcount == 1

    def discard_turn_reply(self, delivery_id: str, *, now: int | None = None) -> bool:
        """在 claim 前明确结束不可继续的普通回复，并清空正文。"""

        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE reply_deliveries SET discarded_at=?, reply_text=NULL
                WHERE delivery_id=? AND claimed_at IS NULL
                  AND delivered_at IS NULL AND discarded_at IS NULL
                """,
                (timestamp, delivery_id),
            )
        return cursor.rowcount == 1

    def pending_delivery_receipts(self) -> list[tuple[str, str]]:
        """返回已交付但尚未向飞书确认“已追加”的子投递。"""

        with self._lock:
            rows = self._connection.execute(
                """
                SELECT delivery_id, reply_fingerprint FROM reply_deliveries
                WHERE receipt_required=1 AND delivered_at IS NOT NULL
                  AND receipt_sent_at IS NULL AND discarded_at IS NULL
                ORDER BY sequence
                """
            ).fetchall()
        return [
            (str(row["delivery_id"]), str(row["reply_fingerprint"]))
            for row in rows
        ]

    def mark_delivery_receipt_sent(
        self, delivery_id: str, *, now: int | None = None
    ) -> bool:
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE reply_deliveries SET receipt_sent_at=?
                WHERE delivery_id=? AND receipt_required=1
                  AND delivered_at IS NOT NULL AND receipt_sent_at IS NULL
                """,
                (timestamp, delivery_id),
            )
        return cursor.rowcount == 1

    @staticmethod
    def _reset_alert_delivery_from_row(row: sqlite3.Row) -> ResetAlertDelivery:
        try:
            raw_ids = json.loads(str(row["channel_message_ids_json"] or "[]"))
        except json.JSONDecodeError:
            raw_ids = []
        message_ids = tuple(
            str(item).strip()
            for item in raw_ids
            if isinstance(item, str) and str(item).strip()
        )
        if row["delivered_at"] is not None:
            state = "delivered"
        elif row["expired_at"] is not None and row["rejected_at"] is not None:
            state = "rejected"
        elif row["expired_at"] is not None:
            state = "expired"
        elif row["uncertain_at"] is not None:
            state = "uncertain"
        elif row["claimed_at"] is not None:
            state = "claimed"
        elif row["rejected_at"] is not None:
            state = "retrying"
        else:
            state = "pending"
        return ResetAlertDelivery(
            delivery_id=str(row["delivery_id"]),
            event_key=str(row["event_key"]),
            message_text=str(row["message_text"]),
            created_at=int(row["created_at"]),
            next_attempt_at=int(row["next_attempt_at"]),
            attempt_count=int(row["attempt_count"]),
            claimed_at=(int(row["claimed_at"]) if row["claimed_at"] is not None else None),
            submitted_at=(int(row["submitted_at"]) if row["submitted_at"] is not None else None),
            delivered_at=(int(row["delivered_at"]) if row["delivered_at"] is not None else None),
            rejected_at=(int(row["rejected_at"]) if row["rejected_at"] is not None else None),
            uncertain_at=(int(row["uncertain_at"]) if row["uncertain_at"] is not None else None),
            expired_at=(int(row["expired_at"]) if row["expired_at"] is not None else None),
            channel_message_ids=message_ids,
            last_error_code=(str(row["last_error_code"]) if row["last_error_code"] else None),
            state=state,
        )

    def reset_alert_status(self) -> dict[str, Any]:
        """返回预警模块运行态；schema16/17 只读快照不做隐式升级。"""

        if self.read_only and "reset_alert_state" not in self._read_only_tables:
            return {
                "available": False,
                "enabled": False,
                "state": "upgrade_required",
                "pending": 0,
                "uncertain": 0,
            }
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM reset_alert_state WHERE singleton=1"
            ).fetchone()
            timestamp = int(time.time())
            pending = int(
                self._connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM reset_alert_deliveries AS d
                    JOIN reset_alert_events AS e ON e.event_key=d.event_key
                    WHERE d.delivered_at IS NULL AND d.uncertain_at IS NULL
                      AND d.expired_at IS NULL AND e.expires_at>?
                    """,
                    (timestamp,),
                ).fetchone()[0]
            )
            uncertain = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM reset_alert_deliveries WHERE uncertain_at IS NOT NULL"
                ).fetchone()[0]
            )
            source_rows = self._connection.execute(
                """
                SELECT source_id, last_attempt_at, last_success_at, last_item_at,
                       baseline_completed_at, health, last_error_code,
                       payload_hash, cursor_json, updated_at
                FROM reset_alert_sources ORDER BY source_id
                """
            ).fetchall()
        if row is None:
            raise StateError("schema18 缺少 reset_alert_state 单例；请由服务受控修复")
        worker_started_at = row["worker_started_at"]
        worker_heartbeat_at = row["worker_heartbeat_at"]
        worker_stopped_at = row["worker_stopped_at"]
        worker_running = bool(
            worker_started_at is not None
            and (worker_stopped_at is None or int(worker_stopped_at) < int(worker_started_at))
            and worker_heartbeat_at is not None
            and timestamp - int(worker_heartbeat_at) <= 60
        )
        return {
            "available": True,
            "enabled": bool(row["enabled"]),
            "state": str(row["last_run_status"]),
            "bootstrap_completed_at": row["bootstrap_completed_at"],
            "last_attempt_at": row["last_attempt_at"],
            "last_success_at": row["last_success_at"],
            "next_check_at": row["next_check_at"],
            "window_start_at": row["window_start_at"],
            "window_end_at": row["window_end_at"],
            "run_slot_at": row["run_slot_at"],
            "last_completed_slot_at": row["last_completed_slot_at"],
            "last_error_code": row["last_error_code"],
            "worker_running": worker_running,
            "worker_started_at": worker_started_at,
            "worker_heartbeat_at": worker_heartbeat_at,
            "worker_stopped_at": worker_stopped_at,
            "updated_at": int(row["updated_at"]),
            "pending": pending,
            "uncertain": uncertain,
            "sources": [dict(item) for item in source_rows],
        }

    def mark_reset_alert_worker_started(self, *, now: int | None = None) -> None:
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE reset_alert_state
                SET worker_started_at=?, worker_heartbeat_at=?, worker_stopped_at=NULL,
                    updated_at=?
                WHERE singleton=1
                """,
                (timestamp, timestamp, timestamp),
            )

    def mark_reset_alert_worker_heartbeat(self, *, now: int | None = None) -> None:
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE reset_alert_state
                SET worker_heartbeat_at=?, updated_at=?
                WHERE singleton=1 AND worker_started_at IS NOT NULL
                  AND worker_stopped_at IS NULL
                """,
                (timestamp, timestamp),
            )

    def mark_reset_alert_worker_stopped(self, *, now: int | None = None) -> None:
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE reset_alert_state
                SET worker_stopped_at=?, worker_heartbeat_at=?, updated_at=?
                WHERE singleton=1
                """,
                (timestamp, timestamp, timestamp),
            )

    def begin_reset_alert_run(
        self,
        *,
        run_slot_at: int,
        window_start_at: int,
        window_end_at: int,
        next_check_at: int,
        now: int | None = None,
    ) -> bool:
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE reset_alert_state
                SET last_attempt_at=?, next_check_at=?, window_start_at=?,
                    window_end_at=?, run_slot_at=?, last_run_status='running', last_error_code=NULL,
                    updated_at=?
                WHERE singleton=1 AND last_run_status!='running'
                  AND COALESCE(last_completed_slot_at, 0)<?
                """,
                (
                    timestamp, int(next_check_at), int(window_start_at), int(window_end_at),
                    int(run_slot_at), timestamp, int(run_slot_at),
                ),
            )
        return cursor.rowcount == 1

    def finish_reset_alert_run(
        self,
        *,
        success: bool,
        bootstrap_completed: bool,
        next_check_at: int,
        error_code: str | None = None,
        now: int | None = None,
    ) -> None:
        timestamp = int(time.time()) if now is None else int(now)
        normalized_error = str(error_code or "").strip()[:120] or None
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE reset_alert_state
                SET bootstrap_completed_at=CASE
                        WHEN ?=1 THEN COALESCE(bootstrap_completed_at, ?)
                        ELSE bootstrap_completed_at END,
                    last_success_at=CASE WHEN ?=1 THEN ? ELSE last_success_at END,
                    last_completed_slot_at=run_slot_at,
                    next_check_at=?, last_run_status=?, last_error_code=?, updated_at=?
                WHERE singleton=1
                """,
                (
                    1 if bootstrap_completed else 0,
                    timestamp,
                    1 if success else 0,
                    timestamp,
                    int(next_check_at),
                    "ok" if success else "partial_failure",
                    normalized_error,
                    timestamp,
                ),
            )

    def recover_interrupted_reset_alert_run(self, *, now: int | None = None) -> bool:
        """服务启动时释放未完成整点；同一时段可重新执行且不忙循环。"""

        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE reset_alert_state
                SET last_run_status='interrupted', last_error_code='service_restarted',
                    updated_at=?
                WHERE singleton=1 AND last_run_status='running'
                """,
                (timestamp,),
            )
        return cursor.rowcount == 1

    def update_reset_alert_endpoint(self, endpoint: str, value: Mapping[str, Any]) -> None:
        """Persist one X endpoint immediately; preserve other cursor and source facts."""
        if endpoint not in {'syndication', 'oembed', 'x_parent'}:
            raise ValueError('Unknown X endpoint')
        timestamp = int(time.time())
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT cursor_json FROM reset_alert_sources WHERE source_id='x_thsottiaux'"
            ).fetchone()
            cursor = json.loads(row[0]) if row else {}
            endpoints = dict(cursor.get('x_endpoints') or {})
            endpoints[endpoint] = dict(value)
            cursor['x_endpoints'] = endpoints
            self._connection.execute(
                """INSERT INTO reset_alert_sources(source_id,cursor_json,updated_at)
                   VALUES('x_thsottiaux',?,?) ON CONFLICT(source_id) DO UPDATE SET
                   cursor_json=excluded.cursor_json,updated_at=excluded.updated_at""",
                (json.dumps(cursor, ensure_ascii=False, sort_keys=True, separators=(',', ':')), timestamp))

    def upsert_reset_alert_source(
        self,
        source_id: str,
        *,
        cursor: Mapping[str, Any],
        success: bool,
        last_item_at: int | None,
        payload_hash: str | None,
        error_code: str | None,
        mark_baseline: bool = False,
        attempted: bool = True,
        now: int | None = None,
    ) -> None:
        source = str(source_id or "").strip()
        if not source or len(source) > 80:
            raise ValueError("source_id 无效")
        timestamp = int(time.time()) if now is None else int(now)
        cursor_json = json.dumps(dict(cursor), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        success = bool(success and attempted)
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO reset_alert_sources(
                    source_id, cursor_json, last_attempt_at, last_success_at,
                    last_item_at, baseline_completed_at, health, last_error_code,
                    payload_hash, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source_id) DO UPDATE SET
                    cursor_json=excluded.cursor_json,
                    last_attempt_at=COALESCE(excluded.last_attempt_at,reset_alert_sources.last_attempt_at),
                    last_success_at=CASE WHEN excluded.health='ok'
                        THEN excluded.last_success_at ELSE reset_alert_sources.last_success_at END,
                    last_item_at=CASE WHEN excluded.health='ok'
                        THEN COALESCE(excluded.last_item_at, reset_alert_sources.last_item_at)
                        ELSE reset_alert_sources.last_item_at END,
                    baseline_completed_at=CASE
                        WHEN excluded.baseline_completed_at IS NOT NULL
                        THEN COALESCE(reset_alert_sources.baseline_completed_at,
                                      excluded.baseline_completed_at)
                        ELSE reset_alert_sources.baseline_completed_at END,
                    health=excluded.health,
                    last_error_code=excluded.last_error_code,
                    payload_hash=CASE WHEN excluded.health='ok'
                        THEN COALESCE(excluded.payload_hash, reset_alert_sources.payload_hash)
                        ELSE reset_alert_sources.payload_hash END,
                    updated_at=excluded.updated_at
                """,
                (
                    source,
                    cursor_json,
                    timestamp if attempted else None,
                    timestamp if success and attempted else None,
                    int(last_item_at) if last_item_at is not None else None,
                    timestamp if success and mark_baseline else None,
                    "ok" if success else "unavailable",
                    str(error_code or "").strip()[:120] or None,
                    str(payload_hash or "").strip()[:128] or None,
                    timestamp,
                ),
            )

    def record_reset_alert_signal(
        self,
        *,
        signal_key: str,
        source_id: str,
        source_item_id: str,
        source_url: str,
        published_at: int,
        content_hash: str,
        signal_kind: str,
        is_official: bool,
        payload: Mapping[str, Any],
        observed_at: int | None = None,
    ) -> bool:
        timestamp = int(time.time()) if observed_at is None else int(observed_at)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO reset_alert_signals(
                    signal_key, source_id, source_item_id, source_url,
                    published_at, observed_at, content_hash, signal_kind,
                    is_official, payload_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    str(signal_key), str(source_id), str(source_item_id), str(source_url),
                    int(published_at), timestamp, str(content_hash), str(signal_kind),
                    1 if is_official else 0,
                    json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                ),
            )
        return cursor.rowcount == 1

    def ensure_reset_alert_rule_epoch(self, *, now: int) -> int:
        """Persist rule activation before the first scheduled fetch, without a migration."""
        with self._lock, self._connection:
            rows = self._connection.execute("SELECT source_id, cursor_json FROM reset_alert_sources").fetchall()
            cursors = {row["source_id"]: json.loads(row["cursor_json"]) for row in rows}
            epochs = [c["rules_v2_started_at"] for c in cursors.values()
                      if isinstance(c.get("rules_v2_started_at"), int)]
            if epochs:
                return min(epochs)
            cursor = {**cursors.get("forecast", {}), "rules_v2_started_at": int(now)}
            self._connection.execute(
                """INSERT INTO reset_alert_sources(source_id,cursor_json,updated_at)
                   VALUES('forecast',?,?) ON CONFLICT(source_id) DO UPDATE SET cursor_json=excluded.cursor_json""",
                (json.dumps(cursor, sort_keys=True), int(now)),
            )
        return int(now)

    def recent_reset_alert_signals(self, *, since: int, limit: int = 500) -> list[dict[str, Any]]:
        """Bounded public evidence cache; callers never open another database."""
        with self._lock:
            rows = self._connection.execute(
                """SELECT * FROM reset_alert_signals
                   WHERE published_at>=? OR (signal_kind='x_parent_context' AND observed_at>=?)
                   ORDER BY observed_at DESC LIMIT ?""",
                (int(since), int(since), min(1000, max(1, int(limit)))),
            ).fetchall()
        return [dict(row) for row in rows]

    def reserve_reset_alert_event(
        self,
        *,
        event_key: str,
        level: str,
        evidence: str,
        window_text: str,
        advice: str,
        source_ids: Iterable[str],
        fingerprint: str,
        expires_at: int,
        now: int | None = None,
    ) -> bool:
        timestamp = int(time.time()) if now is None else int(now)
        normalized_level = str(level).strip().upper()
        if normalized_level not in {"A", "B"}:
            raise ValueError("预警级别只允许 A 或 B")
        normalized_sources = tuple(dict.fromkeys(str(item).strip() for item in source_ids if str(item).strip()))
        message = (
            f"【Codex 重置预警｜{normalized_level} 级】\n"
            f"证据：{str(evidence).strip()}\n"
            f"窗口：{str(window_text).strip()}\n"
            f"建议：{str(advice).strip()}"
        )
        if len(message.splitlines()) != 4:
            raise ValueError("预警消息必须严格为四行")
        delivery_id = "reset-" + hashlib.sha256(str(event_key).encode("utf-8")).hexdigest()[:32]
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO reset_alert_events(
                    event_key, level, evidence, window_text, advice,
                    source_ids_json, created_at, expires_at, fingerprint
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    str(event_key), normalized_level, str(evidence).strip(),
                    str(window_text).strip(), str(advice).strip(),
                    json.dumps(normalized_sources, ensure_ascii=False), timestamp,
                    int(expires_at), str(fingerprint),
                ),
            )
            self._connection.execute(
                """
                INSERT OR IGNORE INTO reset_alert_deliveries(
                    delivery_id, event_key, message_text, created_at, next_attempt_at
                ) VALUES(?,?,?,?,?)
                """,
                (delivery_id, str(event_key), message, timestamp, timestamp),
            )
        return cursor.rowcount == 1

    def recover_interrupted_reset_alerts(self, *, now: int | None = None) -> dict[str, int]:
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
            released = self._connection.execute(
                """
                UPDATE reset_alert_deliveries
                SET claimed_at=NULL, next_attempt_at=?, last_error_code='interrupted_before_submit'
                WHERE claimed_at IS NOT NULL AND submitted_at IS NULL
                  AND delivered_at IS NULL AND uncertain_at IS NULL
                """,
                (timestamp,),
            ).rowcount
            uncertain = self._connection.execute(
                """
                UPDATE reset_alert_deliveries
                SET uncertain_at=?, last_error_code='interrupted_after_submit'
                WHERE submitted_at IS NOT NULL AND delivered_at IS NULL
                  AND uncertain_at IS NULL
                """,
                (timestamp,),
            ).rowcount
        return {"unsubmitted_released": max(0, released), "submitted_uncertain": max(0, uncertain)}

    def expire_reset_alert_deliveries(self, *, now: int | None = None) -> int:
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE reset_alert_deliveries
                SET expired_at=?, claimed_at=NULL, submitted_at=NULL,
                    last_error_code='event_expired'
                WHERE delivery_id IN (
                    SELECT d.delivery_id
                    FROM reset_alert_deliveries AS d
                    JOIN reset_alert_events AS e ON e.event_key=d.event_key
                    WHERE d.delivered_at IS NULL AND d.uncertain_at IS NULL
                      AND d.expired_at IS NULL AND d.submitted_at IS NULL
                      AND e.expires_at<=?
                )
                """,
                (timestamp, timestamp),
            )
        return max(0, cursor.rowcount)

    def claim_reset_alert_delivery(self, *, now: int | None = None) -> ResetAlertDelivery | None:
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
            row = self._connection.execute(
                """
                SELECT d.* FROM reset_alert_deliveries AS d
                JOIN reset_alert_events AS e ON e.event_key=d.event_key
                WHERE d.delivered_at IS NULL AND d.uncertain_at IS NULL
                  AND d.expired_at IS NULL AND d.claimed_at IS NULL
                  AND d.next_attempt_at<=? AND e.expires_at>?
                ORDER BY d.created_at, d.delivery_id LIMIT 1
                """,
                (timestamp, timestamp),
            ).fetchone()
            if row is None:
                return None
            delivery_id = str(row["delivery_id"])
            cursor = self._connection.execute(
                """
                UPDATE reset_alert_deliveries
                SET claimed_at=?, submitted_at=NULL, rejected_at=NULL,
                    attempt_count=attempt_count+1, last_error_code=NULL
                WHERE delivery_id=? AND claimed_at IS NULL
                  AND delivered_at IS NULL AND uncertain_at IS NULL
                  AND expired_at IS NULL
                """,
                (timestamp, delivery_id),
            )
            if cursor.rowcount != 1:
                return None
            claimed = self._connection.execute(
                "SELECT * FROM reset_alert_deliveries WHERE delivery_id=?",
                (delivery_id,),
            ).fetchone()
        return self._reset_alert_delivery_from_row(claimed)

    def mark_reset_alert_submitted(self, delivery_id: str, *, now: int | None = None) -> bool:
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE reset_alert_deliveries SET submitted_at=?
                WHERE delivery_id=? AND claimed_at IS NOT NULL
                  AND submitted_at IS NULL AND delivered_at IS NULL
                  AND uncertain_at IS NULL AND expired_at IS NULL
                """,
                (timestamp, str(delivery_id)),
            )
        return cursor.rowcount == 1

    def release_reset_alert_delivery(
        self,
        delivery_id: str,
        *,
        next_attempt_at: int,
        error_code: str,
        rejected: bool = False,
        allow_submitted: bool = False,
        now: int | None = None,
    ) -> bool:
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE reset_alert_deliveries
                SET claimed_at=NULL, submitted_at=NULL, next_attempt_at=?,
                    rejected_at=?, last_error_code=?
                WHERE delivery_id=? AND delivered_at IS NULL AND uncertain_at IS NULL
                  AND expired_at IS NULL
                  AND (?=1 OR submitted_at IS NULL)
                """,
                (
                    int(next_attempt_at), timestamp if rejected else None,
                    str(error_code or "")[:120], str(delivery_id),
                    1 if allow_submitted else 0,
                ),
            )
        return cursor.rowcount == 1

    def mark_reset_alert_delivered(
        self,
        delivery_id: str,
        message_ids: Iterable[str],
        *,
        now: int | None = None,
    ) -> bool:
        timestamp = int(time.time()) if now is None else int(now)
        normalized = tuple(dict.fromkeys(str(item).strip() for item in message_ids if str(item).strip()))
        if not normalized:
            raise ValueError("预警投递成功必须包含 message_id")
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT event_key FROM reset_alert_deliveries WHERE delivery_id=?",
                (str(delivery_id),),
            ).fetchone()
            if row is None:
                return False
            cursor = self._connection.execute(
                """
                UPDATE reset_alert_deliveries
                SET delivered_at=?, channel_message_ids_json=?, last_error_code=NULL
                WHERE delivery_id=? AND submitted_at IS NOT NULL
                  AND delivered_at IS NULL AND uncertain_at IS NULL
                  AND expired_at IS NULL
                """,
                (timestamp, json.dumps(normalized, ensure_ascii=False), str(delivery_id)),
            )
            if cursor.rowcount == 1:
                self._connection.execute(
                    "UPDATE reset_alert_events SET notified_at=? WHERE event_key=?",
                    (timestamp, str(row["event_key"])),
                )
        return cursor.rowcount == 1

    def mark_reset_alert_rejected(
        self,
        delivery_id: str,
        *,
        error_code: str,
        now: int | None = None,
    ) -> bool:
        """记录飞书明确且不可重试的拒绝，终止该投递而不伪装成过期。"""

        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE reset_alert_deliveries
                SET claimed_at=NULL, submitted_at=NULL, rejected_at=?, expired_at=?,
                    last_error_code=?
                WHERE delivery_id=? AND submitted_at IS NOT NULL
                  AND delivered_at IS NULL AND uncertain_at IS NULL
                  AND expired_at IS NULL
                """,
                (
                    timestamp,
                    timestamp,
                    str(error_code or "")[:120],
                    str(delivery_id),
                ),
            )
        return cursor.rowcount == 1

    def mark_reset_alert_uncertain(
        self,
        delivery_id: str,
        *,
        error_code: str,
        now: int | None = None,
    ) -> bool:
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE reset_alert_deliveries
                SET uncertain_at=?, last_error_code=?
                WHERE delivery_id=? AND submitted_at IS NOT NULL
                  AND delivered_at IS NULL AND uncertain_at IS NULL
                  AND expired_at IS NULL
                """,
                (timestamp, str(error_code or "")[:120], str(delivery_id)),
            )
        return cursor.rowcount == 1

    def latest_reset_alerts(self, *, limit: int = 10) -> list[dict[str, Any]]:
        if limit < 1 or limit > 100:
            raise ValueError("limit 必须介于 1 和 100")
        if self.read_only and "reset_alert_events" not in self._read_only_tables:
            return []
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT e.event_key, e.level, e.evidence, e.window_text, e.advice,
                       e.source_ids_json, e.created_at, e.expires_at, e.notified_at,
                       d.delivery_id, d.next_attempt_at, d.attempt_count, d.claimed_at,
                       d.submitted_at, d.delivered_at, d.rejected_at, d.uncertain_at,
                       d.expired_at, d.channel_message_ids_json, d.last_error_code,
                       d.message_text
                FROM reset_alert_events AS e
                JOIN reset_alert_deliveries AS d ON d.event_key=e.event_key
                ORDER BY e.created_at DESC, e.event_key DESC LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            delivery = self._reset_alert_delivery_from_row(row)
            result.append(
                {
                    "event_key": str(row["event_key"]),
                    "level": str(row["level"]),
                    "evidence": str(row["evidence"]),
                    "window": str(row["window_text"]),
                    "advice": str(row["advice"]),
                    "source_ids": json.loads(str(row["source_ids_json"] or "[]")),
                    "created_at": int(row["created_at"]),
                    "expires_at": int(row["expires_at"]),
                    "notified_at": row["notified_at"],
                    "delivery": {
                        "delivery_id": delivery.delivery_id,
                        "state": delivery.state,
                        "attempt_count": delivery.attempt_count,
                        "next_attempt_at": delivery.next_attempt_at,
                        "last_error_code": delivery.last_error_code,
                    },
                }
            )
        return result

    def prune(self, *, retention_days: int = 30, now: int | None = None) -> dict[str, int]:
        """清理已消费的事件、终态子投递与失效父通知；永久保留 turn 去重键。

        ``processed_turns`` 很小且承担跨重启防重复通知职责，不能按时间删除。
        """

        if retention_days < 1:
            raise ValueError("retention_days 必须至少为 1")
        timestamp = int(time.time()) if now is None else int(now)
        cutoff = timestamp - retention_days * 86400
        with self._lock, self._connection:
            hook_cursor = self._connection.execute(
                "DELETE FROM hook_events WHERE consumed_at IS NOT NULL AND consumed_at<?",
                (cutoff,),
            )
            delivery_cursor = self._connection.execute(
                """
                DELETE FROM reply_deliveries
                WHERE created_at<? AND (
                    delivered_at IS NOT NULL OR discarded_at IS NOT NULL
                )
                """,
                (cutoff,),
            )
            # 原文上下文需长期保留，直到 Codex 自身 exact turn 不再可读；
            # 但已经送达/结果未知的入站请求 outbox 可按普通投递周期清理。
            self._connection.execute(
                """
                DELETE FROM notification_raw_deliveries
                WHERE created_at<? AND (
                    delivered_at IS NOT NULL OR uncertain_at IS NOT NULL
                )
                """,
                (cutoff,),
            )
            summary_count_before = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM notification_summary_deliveries"
                ).fetchone()[0]
            )
            notification_cursor = self._connection.execute(
                """
                DELETE FROM notifications
                WHERE created_at<? AND (
                    (reply_kind='turn' AND expires_at<? AND NOT EXISTS (
                        SELECT 1 FROM reply_deliveries AS d
                        WHERE d.parent_code=notifications.code
                          AND d.delivered_at IS NULL AND d.discarded_at IS NULL
                    ) AND NOT EXISTS (
                        SELECT 1 FROM notification_media_deliveries AS media
                        WHERE media.event_key=notifications.event_key
                          AND media.delivered_at IS NULL
                          AND media.discarded_at IS NULL
                    ) AND NOT EXISTS (
                        SELECT 1 FROM notification_summary_deliveries AS summary
                        WHERE summary.event_key=notifications.event_key
                          AND summary.delivered_at IS NULL
                          AND summary.uncertain_at IS NULL
                    ))
                    OR (reply_kind IN ('rpc','hook') AND consumed_at IS NOT NULL)
                )
                """,
                (cutoff, timestamp),
            )
            summary_count_after = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM notification_summary_deliveries"
                ).fetchone()[0]
            )
            context_cursor = self._connection.execute(
                "DELETE FROM management_contexts WHERE expires_at<?",
                (timestamp,),
            )
            inbound_cursor = self._connection.execute(
                "DELETE FROM management_inbound_messages WHERE created_at<?",
                (cutoff,),
            )
            search_cache_cursor = self._connection.execute(
                "DELETE FROM session_search_cache WHERE updated_at<?",
                (timestamp - max(retention_days, 90) * 86400,),
            )
            search_judgment_cursor = self._connection.execute(
                "DELETE FROM session_search_judgments WHERE updated_at<?",
                (timestamp - max(retention_days, 90) * 86400,),
            )
            title_recovery_cursor = self._connection.execute(
                "DELETE FROM thread_title_recoveries WHERE updated_at<?",
                (timestamp - max(retention_days, 90) * 86400,),
            )
            reset_cutoff = timestamp - max(retention_days, 180) * 86400
            reset_signal_cursor = self._connection.execute(
                "DELETE FROM reset_alert_signals WHERE observed_at<?",
                (reset_cutoff,),
            )
            reset_event_cursor = self._connection.execute(
                """
                DELETE FROM reset_alert_events
                WHERE created_at<? AND notified_at IS NOT NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM reset_alert_deliveries AS delivery
                    WHERE delivery.event_key=reset_alert_events.event_key
                      AND delivery.delivered_at IS NULL
                  )
                """,
                (reset_cutoff,),
            )
        return {
            "hook_events": max(0, hook_cursor.rowcount),
            "reply_deliveries": max(0, delivery_cursor.rowcount),
            "notifications": max(0, notification_cursor.rowcount),
            "notification_summary_deliveries": max(
                0, summary_count_before - summary_count_after
            ),
            "management_contexts": max(0, context_cursor.rowcount),
            "management_inbound_messages": max(0, inbound_cursor.rowcount),
            "session_search_cache": max(0, search_cache_cursor.rowcount),
            "session_search_judgments": max(0, search_judgment_cursor.rowcount),
            "thread_title_recoveries": max(0, title_recovery_cursor.rowcount),
            "reset_alert_signals": max(0, reset_signal_cursor.rowcount),
            "reset_alert_events": max(0, reset_event_cursor.rowcount),
        }

    def stats(self) -> dict[str, int]:
        with self._lock:
            result = {}
            for table in (
                "hook_events", "notifications", "reply_deliveries", "processed_turns", "management_contexts",
                "management_current_bindings",
                "notification_summary_deliveries",
                "notification_media_deliveries",
                "management_inbound_messages",
                "management_context_actions",
                "remote_control_actions",
                "session_search_cache", "session_search_judgments",
                "thread_title_recoveries",
                "user_reply_chain_messages",
                "reset_alert_signals", "reset_alert_events", "reset_alert_deliveries",
            ):
                # schema16 只有 schema17 的摘要 outbox 可以缺失。只读查询可
                # 报告该项为空，但不得为了补齐统计而 CREATE TABLE 或迁移。
                if self.read_only and table not in self._read_only_tables:
                    result[table] = 0
                    continue
                result[table] = int(self._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            if "reset_alert_deliveries" in self._read_only_tables or not self.read_only:
                result["reset_alert_pending"] = int(
                    self._connection.execute(
                        """
                        SELECT COUNT(*) FROM reset_alert_deliveries
                        WHERE delivered_at IS NULL AND uncertain_at IS NULL
                        """
                    ).fetchone()[0]
                )
                result["reset_alert_uncertain"] = int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM reset_alert_deliveries WHERE uncertain_at IS NOT NULL"
                    ).fetchone()[0]
                )
            else:
                result["reset_alert_signals"] = 0
                result["reset_alert_events"] = 0
                result["reset_alert_deliveries"] = 0
                result["reset_alert_pending"] = 0
                result["reset_alert_uncertain"] = 0
            if "notification_summary_deliveries" in self._read_only_tables or not self.read_only:
                result["notification_summary_pending"] = int(
                    self._connection.execute(
                        """
                        SELECT COUNT(*) FROM notification_summary_deliveries
                        WHERE delivered_at IS NULL AND uncertain_at IS NULL
                        """
                    ).fetchone()[0]
                )
                result["notification_summary_uncertain"] = int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM notification_summary_deliveries "
                        "WHERE uncertain_at IS NOT NULL"
                    ).fetchone()[0]
                )
                result["notification_summary_delivered"] = int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM notification_summary_deliveries "
                        "WHERE delivered_at IS NOT NULL"
                    ).fetchone()[0]
                )
            else:
                result["notification_summary_pending"] = 0
                result["notification_summary_uncertain"] = 0
                result["notification_summary_delivered"] = 0
            if "notification_media_deliveries" in self._read_only_tables or not self.read_only:
                result["notification_media_pending"] = int(
                    self._connection.execute(
                        """
                        SELECT COUNT(*) FROM notification_media_deliveries
                        WHERE delivered_at IS NULL AND uncertain_at IS NULL
                          AND discarded_at IS NULL
                        """
                    ).fetchone()[0]
                )
                result["notification_media_uncertain"] = int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM notification_media_deliveries "
                        "WHERE uncertain_at IS NOT NULL"
                    ).fetchone()[0]
                )
                result["notification_media_delivered"] = int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM notification_media_deliveries "
                        "WHERE delivered_at IS NOT NULL"
                    ).fetchone()[0]
                )
            else:
                result["notification_media_pending"] = 0
                result["notification_media_uncertain"] = 0
                result["notification_media_delivered"] = 0
            if "remote_control_actions" in self._read_only_tables or not self.read_only:
                result["remote_control_pending"] = int(
                    self._connection.execute(
                        """
                        SELECT COUNT(*) FROM remote_control_actions
                        WHERE claimed_at IS NOT NULL AND succeeded_at IS NULL
                          AND uncertain_at IS NULL
                        """
                    ).fetchone()[0]
                )
                result["remote_control_uncertain"] = int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM remote_control_actions "
                        "WHERE uncertain_at IS NOT NULL"
                    ).fetchone()[0]
                )
            else:
                result["remote_control_pending"] = 0
                result["remote_control_uncertain"] = 0
        return result
