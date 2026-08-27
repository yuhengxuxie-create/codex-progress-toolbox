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
from pathlib import Path
from typing import Any, Iterable, Mapping

from .models import TurnEvent


SCHEMA_VERSION = 10
_SQLITE_LOCK_WAIT_SECONDS = 5.0
_SQLITE_LOCK_RETRY_SECONDS = 0.02
_PERMANENT_EXPIRY = 9_223_372_036_854_775_807
_STAGED_IMAGE_MAX_COUNT = 5
_STAGED_IMAGE_MAX_TOTAL_BYTES = 50 * 1024 * 1024
_AUTO_MONITORING_ENABLED_KEY = "auto_monitoring_enabled_v1"
_AUTO_MONITORING_EFFECTIVE_AT_KEY = "auto_monitoring_effective_at_v1"


class StateError(RuntimeError):
    """本地状态库无法安全读写。"""


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

    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._connection.execute("PRAGMA foreign_keys=ON")
        # 切换 journal_mode 本身需要数据库写锁；两个短命 hook 进程可能在
        # 同一时刻首次打开旧库，而 SQLite 对该 PRAGMA 不总是按 busy_timeout
        # 等待。显式短暂重试，避免并发初始化把合法状态库误判为损坏。
        self._execute_locked_pragma("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        try:
            self._initialize()
        except sqlite3.DatabaseError as exc:
            self._connection.close()
            raise StateError("状态库结构损坏或无法迁移") from exc
        except BaseException:
            self._connection.close()
            raise

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
                """CREATE TABLE IF NOT EXISTS management_contexts (
                    context_id TEXT PRIMARY KEY,
                    context_kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL
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
                "management_contexts": {
                    "context_id", "context_kind", "payload_json", "created_at", "expires_at",
                },
                "management_message_ids": {"message_id", "context_id", "created_at"},
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
            if existing_version < 8:
                # v7 → v8：管理导航必须支持回复任意历史机器人消息；普通进度
                # 通知仍保持原有时效，只把管理上下文迁成永久哨兵。
                self._connection.execute(
                    "UPDATE management_contexts SET expires_at=?",
                    (_PERMANENT_EXPIRY,),
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
        """返回当前有效监测；读取时顺带清理到期的自动项。"""

        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
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
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO management_contexts(
                    context_id, context_kind, payload_json, created_at, expires_at
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (context_id, kind, encoded, timestamp, expires_at),
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
            self._connection.executemany(
                "INSERT INTO management_message_ids(message_id, context_id, created_at) VALUES(?, ?, ?)",
                ((message_id, context_id, timestamp) for message_id in normalized),
            )

    def management_context_for_message(
        self,
        message_id: str,
        *,
        now: int | None = None,
    ) -> tuple[str, Mapping[str, Any]] | None:
        """按被回复的飞书 message_id 精确读取仍有效的上下文。"""

        normalized = str(message_id or "").strip()
        if not normalized:
            return None
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock:
            row = self._connection.execute(
                """
                SELECT c.context_kind, c.payload_json
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
        return str(row["context_kind"]), payload

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

    def complete_management_inbound(
        self, message_id: str, *, now: int | None = None
    ) -> None:
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE management_inbound_messages SET completed_at=? WHERE message_id=?",
                (timestamp, str(message_id or "").strip()),
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

    def reserve_notification(
        self,
        event: TurnEvent,
        code: str,
        message_text: str,
        ttl_hours: int,
        *,
        reply_kind: str = "turn",
    ) -> tuple[str, str]:
        """为事件创建稳定 outbox 记录；崩溃重启后复用相同编号与正文。"""

        if reply_kind not in {"turn", "rpc", "hook"}:
            raise ValueError("reply_kind 仅允许 turn、rpc 或 hook")
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
            row = self._connection.execute(
                "SELECT code, message_text FROM notifications WHERE event_key=?",
                (event.dedupe_key,),
            ).fetchone()
        if row is None:
            raise StateError("无法创建通知 outbox 记录")
        return str(row["code"]), str(row["message_text"])

    def mark_sent(self, event_key: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE notifications SET sent_at=COALESCE(sent_at, ?) WHERE event_key=?",
                (int(time.time()), event_key),
            )

    def bind_channel_message(self, event_key: str, message_id: str) -> None:
        """把平台消息 ID 持久绑定到通知；已绑定不同 ID 时拒绝覆盖。"""

        normalized = str(message_id or "").strip()
        if not normalized or len(normalized) > 512:
            raise StateError("消息渠道返回了无效 message_id")
        self.bind_channel_messages(event_key, (normalized,), allow_additional=False)

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
                now = int(time.time())
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
                        (message_id, event_key, now),
                    )
            except sqlite3.IntegrityError as exc:
                raise StateError("平台消息 ID 已绑定到另一条通知") from exc

    def code_for_channel_message(self, message_id: str) -> str | None:
        """按被引用的平台消息 ID 查找 HMAC 通知编号。"""

        normalized = str(message_id or "").strip()
        if not normalized:
            return None
        with self._lock:
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
        """原子消费一次性编号，返回 ``(thread_id, turn_id, reply_kind)``。"""

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
                WHERE code=? AND sent_at IS NOT NULL AND consumed_at IS NULL AND expires_at>=?
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
        """只读检查仍可消费的编号，不改变一次性状态。"""

        if not codec.valid(code):
            return None
        timestamp = int(time.time()) if now is None else int(now)
        with self._lock:
            row = self._connection.execute(
                """
                SELECT thread_id, turn_id, reply_kind FROM notifications
                WHERE code=? AND sent_at IS NOT NULL AND consumed_at IS NULL AND expires_at>=?
                """,
                (code, timestamp),
            ).fetchone()
        if row is None:
            return None
        return str(row["thread_id"]), str(row["turn_id"]), str(row["reply_kind"])

    def pending_turn_replies(self) -> list[tuple[str, str, str, str]]:
        """返回已持久接收、尚未进入非幂等提交阶段的普通回复。"""

        with self._lock:
            rows = self._connection.execute(
                """
                SELECT code, thread_id, reply_text, reply_fingerprint
                FROM notifications
                WHERE reply_kind='turn'
                  AND consumed_at IS NOT NULL
                  AND claimed_at IS NULL
                  AND delivered_at IS NULL
                  AND discarded_at IS NULL
                  AND reply_text IS NOT NULL
                ORDER BY consumed_at, code
                """
            ).fetchall()
        return [
            (
                str(row["code"]),
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
                SELECT consumed_at FROM notifications
                WHERE code=? AND reply_kind='turn'
                  AND consumed_at IS NOT NULL
                  AND claimed_at IS NULL
                  AND delivered_at IS NULL
                  AND discarded_at IS NULL
                """,
                (code,),
            ).fetchone()
        return int(row["consumed_at"]) if row is not None else None

    def uncertain_turn_replies(self) -> list[str]:
        """列出已进入非幂等提交临界区、但未确认投递的编号。"""

        with self._lock:
            rows = self._connection.execute(
                """
                SELECT code FROM notifications
                WHERE reply_kind='turn' AND claimed_at IS NOT NULL AND delivered_at IS NULL
                ORDER BY claimed_at, code
                """
            ).fetchall()
        return [str(row["code"]) for row in rows]

    def claim_turn_reply(self, code: str, *, now: int | None = None) -> bool:
        """在 ``turn/start`` 前原子进入不可自动重试的临界区。"""

        timestamp = int(time.time()) if now is None else int(now)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE notifications SET claimed_at=?
                WHERE code=? AND reply_kind='turn'
                  AND consumed_at IS NOT NULL
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
            reply_kind='turn'
            AND consumed_at IS NOT NULL
            AND claimed_at IS NULL
            AND delivered_at IS NULL
            AND discarded_at IS NULL
            AND reply_text IS NOT NULL
        """
        with self._lock, self._connection:
            total = int(
                self._connection.execute(
                    f"SELECT COUNT(*) FROM notifications WHERE {predicate}"
                ).fetchone()[0]
            )
            eligible = int(
                self._connection.execute(
                    f"SELECT COUNT(*) FROM notifications WHERE {predicate} AND consumed_at<=?",
                    (cutoff,),
                ).fetchone()[0]
            )
            if total != expected_count or eligible != expected_count:
                raise StateError(
                    "待丢弃回复的数量或年龄在确认期间发生变化；已拒绝操作"
                )
            cursor = self._connection.execute(
                f"""
                UPDATE notifications
                SET discarded_at=?, reply_text=NULL
                WHERE {predicate} AND consumed_at<=?
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
                UPDATE notifications SET delivered_at=?
                WHERE code=? AND reply_kind='turn'
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
            if delivered:
                cursor = self._connection.execute(
                    """
                    UPDATE notifications SET delivered_at=?
                    WHERE code=? AND reply_kind='turn'
                      AND claimed_at IS NOT NULL AND delivered_at IS NULL
                    """,
                    (timestamp, code),
                )
            else:
                cursor = self._connection.execute(
                    """
                    UPDATE notifications SET claimed_at=NULL
                    WHERE code=? AND reply_kind='turn'
                      AND claimed_at IS NOT NULL AND delivered_at IS NULL
                    """,
                    (code,),
                )
        return cursor.rowcount == 1

    def prune(self, *, retention_days: int = 30, now: int | None = None) -> dict[str, int]:
        """清理已消费的事件与失效通知正文；永久保留 turn 去重键。

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
            notification_cursor = self._connection.execute(
                """
                DELETE FROM notifications
                WHERE created_at<? AND (
                    (consumed_at IS NULL AND expires_at<?)
                    OR (reply_kind IN ('rpc','hook') AND consumed_at IS NOT NULL)
                    OR (reply_kind='turn' AND delivered_at IS NOT NULL)
                    OR (reply_kind='turn' AND discarded_at IS NOT NULL)
                )
                """,
                (cutoff, timestamp),
            )
            context_cursor = self._connection.execute(
                "DELETE FROM management_contexts WHERE expires_at<?",
                (timestamp,),
            )
            inbound_cursor = self._connection.execute(
                "DELETE FROM management_inbound_messages WHERE created_at<?",
                (cutoff,),
            )
        return {
            "hook_events": max(0, hook_cursor.rowcount),
            "notifications": max(0, notification_cursor.rowcount),
            "management_contexts": max(0, context_cursor.rowcount),
            "management_inbound_messages": max(0, inbound_cursor.rowcount),
        }

    def stats(self) -> dict[str, int]:
        with self._lock:
            result = {}
            for table in (
                "hook_events", "notifications", "processed_turns", "management_contexts",
                "management_inbound_messages",
            ):
                result[table] = int(self._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        return result
