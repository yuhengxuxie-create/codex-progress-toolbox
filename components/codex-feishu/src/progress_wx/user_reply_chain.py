"""Persistent exact-parent routing for replies to the user's own messages.

The normal Feishu route starts at a bot message ID.  Once a user replies to
that message, Feishu gives the user's message its own ID and a later reply may
quote that ID instead.  The existing ``reply_deliveries`` table proves that a
message was queued for a Codex turn, but deliberately does not contain the
sender/chat scope or the exact quoted parent.  This module stores that small
amount of routing evidence without storing user text or attachment paths.

The service integrates this module around ``StateStore.enqueue_turn_reply``:

1. :meth:`UserReplyChainStore.prepare_reply` validates the exact parent and
   creates a ``prepared`` link.
2. ``StateStore.enqueue_turn_reply`` persists the normal reply outbox row.
3. :meth:`UserReplyChainStore.complete_queued_reply` verifies that row and
   changes the link to ``queued``.

The two-phase link closes the crash window on either side of the existing
outbox write.  :meth:`UserReplyChainStore.reconcile_prepared` can be called at
startup to finish a link left prepared after a process crash.  No route is
ready until the normal outbox row exists.

This module intentionally does not import ``StateStore`` or mutate its schema
version.  The migration owner should execute :data:`SCHEMA_SQL` in the same
controlled migration that creates the table, then instantiate this class with
the already-initialized database.  ``initialize_user_reply_chain_schema`` is
provided for that owner and for isolated tests; it is never called implicitly
by a read-only open.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import sqlite3
import threading
import time
from typing import Any, Iterable, Mapping


USER_REPLY_CHAIN_SCHEMA_VERSION = 1
USER_REPLY_CHAIN_TABLE = "user_reply_chain_messages"

_STATES = frozenset(
    {
        "prepared",
        "queued",
        "processing",
        "delivered",
        "uncertain",
        "discarded",
        "expired",
        "rejected",
    }
)
# ``uncertain`` is deliberately recoverable: the normal outbox may later be
# resolved as delivered after a bounded external check.  The route remains
# target-stable while that check is pending.
_TERMINAL_STATES = frozenset({"delivered", "discarded", "expired", "rejected"})
_HEX = frozenset("0123456789abcdef")

# This SQL is deliberately independent of StateStore._initialize().  The
# parent migration can splice the CREATE/INDEX statements into its existing
# transaction without importing this module's private connection class.
SCHEMA_SQL: tuple[str, ...] = (
    f"""CREATE TABLE IF NOT EXISTS {USER_REPLY_CHAIN_TABLE} (
        message_id TEXT PRIMARY KEY,
        sender_id TEXT NOT NULL,
        chat_id TEXT NOT NULL,
        quoted_message_id TEXT NOT NULL,
        root_message_id TEXT NOT NULL,
        parent_code TEXT NOT NULL,
        thread_id TEXT NOT NULL,
        turn_id TEXT NOT NULL,
        delivery_id TEXT NOT NULL DEFAULT '',
        delivery_sequence INTEGER NOT NULL DEFAULT 0,
        chain_sequence INTEGER NOT NULL,
        reply_fingerprint TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'prepared'
            CHECK(state IN ('prepared','queued','processing','delivered',
                            'uncertain','discarded','expired','rejected')),
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        last_error_code TEXT,
        CHECK(length(message_id) > 0 AND length(message_id) <= 512),
        CHECK(length(sender_id) > 0 AND length(sender_id) <= 1024),
        CHECK(length(chat_id) > 0 AND length(chat_id) <= 1024),
        CHECK(length(quoted_message_id) > 0 AND length(quoted_message_id) <= 512),
        CHECK(length(root_message_id) > 0 AND length(root_message_id) <= 512),
        CHECK(length(parent_code) > 0 AND length(parent_code) <= 512),
        CHECK(length(thread_id) > 0 AND length(thread_id) <= 512),
        CHECK(length(turn_id) > 0 AND length(turn_id) <= 512),
        CHECK(length(delivery_id) <= 512),
        CHECK(delivery_sequence >= 0 AND chain_sequence > 0),
        CHECK(length(reply_fingerprint) > 0 AND length(reply_fingerprint) <= 512),
        CHECK(length(content_hash) = 64
              AND content_hash NOT GLOB '*[^0-9a-f]*'),
        CHECK(created_at >= 0 AND updated_at >= 0 AND expires_at >= 0)
    )""",
    f"CREATE INDEX IF NOT EXISTS idx_user_reply_chain_parent "
    f"ON {USER_REPLY_CHAIN_TABLE}(quoted_message_id)",
    f"CREATE INDEX IF NOT EXISTS idx_user_reply_chain_route "
    f"ON {USER_REPLY_CHAIN_TABLE}(sender_id, chat_id, parent_code, chain_sequence)",
    f"CREATE INDEX IF NOT EXISTS idx_user_reply_chain_state "
    f"ON {USER_REPLY_CHAIN_TABLE}(state, expires_at, updated_at)",
)

REQUIRED_COLUMNS = frozenset(
    {
        "message_id",
        "sender_id",
        "chat_id",
        "quoted_message_id",
        "root_message_id",
        "parent_code",
        "thread_id",
        "turn_id",
        "delivery_id",
        "delivery_sequence",
        "chain_sequence",
        "reply_fingerprint",
        "content_hash",
        "state",
        "created_at",
        "updated_at",
        "expires_at",
        "last_error_code",
    }
)


class UserReplyChainError(RuntimeError):
    """Base class for fail-closed chain routing errors."""


class UserReplyChainSchemaError(UserReplyChainError):
    """The controlled migration has not installed a compatible table."""


class UserReplyChainRejected(UserReplyChainError):
    """The exact parent or the persistent queue proof is not acceptable."""


class UserReplyChainConflict(UserReplyChainRejected):
    """An existing message ID was presented with different identity/content."""


class UserReplyChainExpired(UserReplyChainRejected):
    """The parent notification or chain link is outside its TTL."""


class UserReplyChainNotQueued(UserReplyChainRejected):
    """A link exists but its normal reply outbox row is not durable yet."""


@dataclass(frozen=True, slots=True)
class ReplyChainRecord:
    """Minimal durable route evidence for one inbound user message."""

    message_id: str
    sender_id: str
    chat_id: str
    quoted_message_id: str
    root_message_id: str
    parent_code: str
    thread_id: str
    turn_id: str
    delivery_id: str
    delivery_sequence: int
    chain_sequence: int
    reply_fingerprint: str
    content_hash: str
    state: str
    created_at: int
    updated_at: int
    expires_at: int
    last_error_code: str | None = None
    is_new: bool = False


@dataclass(frozen=True, slots=True)
class ReplyChainResolution:
    """A detailed, non-secret result for an exact parent lookup."""

    status: str
    record: ReplyChainRecord | None = None
    reason: str = ""


def content_hash(value: str) -> str:
    """Return the only representation of user text persisted by this module."""

    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _normalize(value: object, label: str, *, maximum: int) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{label} 不能为空且不得超过 {maximum} 字符")
    return normalized


def _optional_code(value: object) -> str:
    normalized = str(value or "").strip()
    if len(normalized) > 512:
        raise ValueError("parent_code 不得超过 512 字符")
    return normalized


def _hash(value: object, label: str) -> str:
    normalized = str(value or "").strip().casefold()
    if len(normalized) != 64 or any(char not in _HEX for char in normalized):
        raise ValueError(f"{label} 必须是 SHA-256")
    return normalized


def _timestamp(value: int | None) -> int:
    timestamp = int(time.time()) if value is None else int(value)
    if timestamp < 0:
        raise ValueError("时间不能为负数")
    return timestamp


def initialize_user_reply_chain_schema(connection: sqlite3.Connection) -> None:
    """Create the chain table and indexes inside the caller's transaction.

    The function intentionally does not create a database, change journal mode,
    or update the project's global ``schema_version``.  The service migration
    owns those operations.
    """

    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("connection 必须是 sqlite3.Connection")
    try:
        for statement in SCHEMA_SQL:
            connection.execute(statement)
    except sqlite3.Error as exc:
        raise UserReplyChainSchemaError("用户回复链表初始化失败") from exc


def validate_user_reply_chain_schema(connection: sqlite3.Connection) -> None:
    """Validate the installed table without writing anything."""

    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("connection 必须是 sqlite3.Connection")
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (USER_REPLY_CHAIN_TABLE,),
        ).fetchone()
        if exists is None:
            raise UserReplyChainSchemaError(
                f"状态库缺少 {USER_REPLY_CHAIN_TABLE}；请由受控迁移安装"
            )
        columns = {
            str(row[1])
            for row in connection.execute(
                f'PRAGMA table_info("{USER_REPLY_CHAIN_TABLE}")'
            )
        }
    except sqlite3.Error as exc:
        raise UserReplyChainSchemaError("用户回复链表只读校验失败") from exc
    missing = REQUIRED_COLUMNS - columns
    if missing:
        raise UserReplyChainSchemaError(
            "用户回复链表缺少字段：" + ", ".join(sorted(missing))
        )


def _connection_from_path(path: str | Path, *, read_only: bool) -> sqlite3.Connection:
    database = Path(path).expanduser().resolve()
    if read_only:
        if not database.is_file():
            raise UserReplyChainSchemaError(f"只读状态库不存在：{database}")
        uri = f"{database.as_uri()}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=5, check_same_thread=False)
        except sqlite3.Error as exc:
            raise UserReplyChainSchemaError("用户回复链只读状态库无法打开") from exc
    else:
        if not database.is_file():
            raise UserReplyChainSchemaError(
                "不会为用户回复链隐式创建状态库；请先由服务初始化"
            )
        try:
            connection = sqlite3.connect(database, timeout=5, check_same_thread=False)
        except sqlite3.Error as exc:
            raise UserReplyChainSchemaError("用户回复链状态库无法打开") from exc
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=5000")
    if read_only:
        connection.execute("PRAGMA query_only=ON")
    return connection


class UserReplyChainStore:
    """Persistent exact-parent resolver backed by the service SQLite file."""

    def __init__(
        self,
        database_or_connection: str | Path | sqlite3.Connection,
        *,
        read_only: bool = False,
        initialize: bool = False,
    ) -> None:
        if isinstance(database_or_connection, sqlite3.Connection):
            if initialize and read_only:
                raise ValueError("只读连接不能初始化 schema")
            self._connection = database_or_connection
            self._owns_connection = False
            if self._connection.row_factory is None:
                self._connection.row_factory = sqlite3.Row
        else:
            self._connection = _connection_from_path(database_or_connection, read_only=read_only)
            self._owns_connection = True
        self._read_only = bool(read_only)
        self._lock = threading.RLock()
        if initialize:
            if self._read_only:
                raise ValueError("只读连接不能初始化 schema")
            with self._lock, self._connection:
                initialize_user_reply_chain_schema(self._connection)
        validate_user_reply_chain_schema(self._connection)

    @classmethod
    def open_read_only(cls, path: str | Path) -> "UserReplyChainStore":
        """Open a hard read-only connection; never creates or migrates tables."""

        return cls(path, read_only=True)

    def close(self) -> None:
        if self._owns_connection:
            self._connection.close()

    def __enter__(self) -> "UserReplyChainStore":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @staticmethod
    def _validate_hashes(reply_fingerprint: object, digest: object) -> tuple[str, str]:
        fingerprint = _normalize(reply_fingerprint, "reply_fingerprint", maximum=512)
        content_digest = _hash(digest, "content_hash")
        return fingerprint, content_digest

    @staticmethod
    def _record(row: sqlite3.Row, *, is_new: bool = False) -> ReplyChainRecord:
        return ReplyChainRecord(
            message_id=str(row["message_id"]),
            sender_id=str(row["sender_id"]),
            chat_id=str(row["chat_id"]),
            quoted_message_id=str(row["quoted_message_id"]),
            root_message_id=str(row["root_message_id"]),
            parent_code=str(row["parent_code"]),
            thread_id=str(row["thread_id"]),
            turn_id=str(row["turn_id"]),
            delivery_id=str(row["delivery_id"] or ""),
            delivery_sequence=int(row["delivery_sequence"]),
            chain_sequence=int(row["chain_sequence"]),
            reply_fingerprint=str(row["reply_fingerprint"]),
            content_hash=str(row["content_hash"]),
            state=str(row["state"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
            expires_at=int(row["expires_at"]),
            last_error_code=(
                None if row["last_error_code"] is None else str(row["last_error_code"])
            ),
            is_new=is_new,
        )

    def _require_writable(self) -> None:
        if self._read_only:
            raise UserReplyChainError("用户回复链只读连接拒绝写入")

    def _begin_immediate(self) -> None:
        try:
            self._connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise UserReplyChainError("用户回复链暂时被另一进程占用") from exc

    def _notification_route_locked(
        self, quoted_message_id: str, *, parent_code: str, now: int
    ) -> tuple[str, str, str, int] | None:
        """Return code/thread/turn/expiry for one exact bot message ID."""

        rows = self._connection.execute(
            """
            SELECT n.code, n.thread_id, n.turn_id, n.expires_at,
                   n.reply_kind, n.sent_at, n.discarded_at
            FROM notification_message_ids AS m
            JOIN notifications AS n ON n.event_key=m.event_key
            WHERE m.message_id=?
            UNION ALL
            SELECT n.code, n.thread_id, n.turn_id, n.expires_at,
                   n.reply_kind, n.sent_at, n.discarded_at
            FROM notifications AS n
            WHERE n.channel_message_id=?
            """,
            (quoted_message_id, quoted_message_id),
        ).fetchall()
        unique: dict[str, sqlite3.Row] = {}
        for row in rows:
            unique[str(row["code"])] = row
        if len(unique) != 1:
            if not unique:
                return None
            raise UserReplyChainConflict("精确父消息对应多个通知编号")
        row = next(iter(unique.values()))
        if str(row["code"]) != parent_code:
            raise UserReplyChainConflict("父消息与当前通知编号不一致")
        if (
            str(row["reply_kind"] or "") != "turn"
            or row["sent_at"] is None
            or row["discarded_at"] is not None
        ):
            raise UserReplyChainRejected("被引用消息不是可继续的已发送普通进度通知")
        expires_at = int(row["expires_at"])
        if expires_at < now:
            raise UserReplyChainExpired("被引用进度通知已过期")
        raw_context = self._connection.execute(
            "SELECT chat_id FROM notification_raw_contexts WHERE message_id=?",
            (quoted_message_id,),
        ).fetchall()
        if len({str(item["chat_id"]) for item in raw_context}) > 1:
            raise UserReplyChainConflict("父消息存在冲突私聊范围")
        return (
            str(row["code"]),
            str(row["thread_id"]),
            str(row["turn_id"]),
            expires_at,
        )

    def _chain_parent_locked(
        self,
        quoted_message_id: str,
        *,
        sender_id: str,
        chat_id: str,
        parent_code: str,
        now: int,
    ) -> tuple[ReplyChainRecord, str, str, str, int] | None:
        row = self._connection.execute(
            f"SELECT * FROM {USER_REPLY_CHAIN_TABLE} WHERE message_id=?",
            (quoted_message_id,),
        ).fetchone()
        if row is None:
            return None
        if str(row["sender_id"]) != sender_id or str(row["chat_id"]) != chat_id:
            raise UserReplyChainRejected("拒绝跨用户或跨私聊引用用户消息")
        if str(row["parent_code"]) != parent_code:
            raise UserReplyChainConflict("用户消息链与当前通知编号不一致")
        if int(row["expires_at"]) < now:
            raise UserReplyChainExpired("用户消息链已过期")
        if str(row["state"]) in {"prepared", "rejected", "expired", "discarded"}:
            raise UserReplyChainNotQueued("被引用用户消息尚未可靠入队或已失效")
        if str(row["state"]) not in _STATES:
            raise UserReplyChainSchemaError("用户消息链状态损坏")
        delivery_id = str(row["delivery_id"] or "")
        delivery = self._connection.execute(
            """
            SELECT d.delivery_id, d.parent_code, d.inbound_message_id,
                   d.reply_fingerprint, d.reply_text, d.discarded_at,
                   n.thread_id, n.turn_id, n.expires_at
            FROM reply_deliveries AS d
            JOIN notifications AS n ON n.code=d.parent_code
            WHERE d.delivery_id=?
            """,
            (delivery_id,),
        ).fetchone()
        if delivery is None:
            raise UserReplyChainNotQueued("用户消息对应的普通回复队列记录不存在")
        if (
            str(delivery["inbound_message_id"]) != quoted_message_id
            or str(delivery["parent_code"]) != parent_code
            or str(delivery["reply_fingerprint"]) != str(row["reply_fingerprint"])
        ):
            raise UserReplyChainConflict("用户消息链与普通回复队列记录冲突")
        if delivery["discarded_at"] is not None:
            raise UserReplyChainRejected("被引用用户消息的回复已被丢弃")
        if delivery["reply_text"] is None:
            raise UserReplyChainConflict("用户消息链对应的回复正文缺失")
        if content_hash(str(delivery["reply_text"]).strip()) != str(row["content_hash"]):
            raise UserReplyChainConflict("用户消息链对应的回复正文发生变化")
        if int(delivery["expires_at"]) < now:
            raise UserReplyChainExpired("被引用用户消息的原始通知已过期")
        if (
            str(delivery["thread_id"]) != str(row["thread_id"])
            or str(delivery["turn_id"]) != str(row["turn_id"])
        ):
            raise UserReplyChainConflict("用户消息链目标与普通回复队列目标冲突")
        return (
            self._record(row),
            str(row["root_message_id"]),
            str(row["thread_id"]),
            str(row["turn_id"]),
            int(row["expires_at"]),
        )

    def _existing_or_conflict(
        self,
        row: sqlite3.Row,
        *,
        sender_id: str,
        chat_id: str,
        quoted_message_id: str,
        parent_code: str,
        fingerprint: str,
        digest: str,
    ) -> ReplyChainRecord:
        if any(
            (
                str(row["sender_id"]) != sender_id,
                str(row["chat_id"]) != chat_id,
                str(row["quoted_message_id"]) != quoted_message_id,
                str(row["parent_code"]) != parent_code,
                str(row["reply_fingerprint"]) != fingerprint,
                str(row["content_hash"]) != digest,
            )
        ):
            raise UserReplyChainConflict("同一入站 message_id 对应了不同父消息、身份或正文")
        return self._record(row)

    def prepare_reply(
        self,
        *,
        inbound_message_id: str,
        sender_id: str,
        chat_id: str,
        quoted_message_id: str,
        parent_code: str,
        reply_fingerprint: str,
        content_digest: str,
        now: int | None = None,
    ) -> ReplyChainRecord:
        """Validate an exact parent and create a non-routable prepared link.

        ``parent_code`` is only a lookup constraint; the target thread/turn and
        expiry are always read from the existing state tables.  For a first
        user reply, ``quoted_message_id`` must be a locally bound bot message.
        For a continuation it must be a previously completed chain link in the
        same sender/chat scope.
        """

        self._require_writable()
        inbound = _normalize(inbound_message_id, "inbound_message_id", maximum=512)
        sender = _normalize(sender_id, "sender_id", maximum=1024)
        chat = _normalize(chat_id, "chat_id", maximum=1024)
        quoted = _normalize(quoted_message_id, "quoted_message_id", maximum=512)
        code = _normalize(parent_code, "parent_code", maximum=512)
        fingerprint, digest = self._validate_hashes(reply_fingerprint, content_digest)
        if inbound == quoted:
            raise ValueError("入站 message_id 不能引用自身")
        timestamp = _timestamp(now)
        with self._lock:
            try:
                self._begin_immediate()
                existing = self._connection.execute(
                    f"SELECT * FROM {USER_REPLY_CHAIN_TABLE} WHERE message_id=?",
                    (inbound,),
                ).fetchone()
                if existing is not None:
                    result = self._existing_or_conflict(
                        existing,
                        sender_id=sender,
                        chat_id=chat,
                        quoted_message_id=quoted,
                        parent_code=code,
                        fingerprint=fingerprint,
                        digest=digest,
                    )
                    self._connection.commit()
                    return result

                chain_parent = self._chain_parent_locked(
                    quoted,
                    sender_id=sender,
                    chat_id=chat,
                    parent_code=code,
                    now=timestamp,
                )
                if chain_parent is not None:
                    parent_record, root_message, thread_id, turn_id, expires_at = chain_parent
                else:
                    direct = self._notification_route_locked(
                        quoted, parent_code=code, now=timestamp
                    )
                    if direct is None:
                        raise UserReplyChainRejected(
                            "被引用消息没有可证明的本地任务关联"
                        )
                    _, thread_id, turn_id, expires_at = direct
                    root_message = quoted

                chain_sequence_row = self._connection.execute(
                    f"""
                    SELECT COALESCE(MAX(chain_sequence), 0) AS max_sequence
                    FROM {USER_REPLY_CHAIN_TABLE}
                    WHERE sender_id=? AND chat_id=? AND root_message_id=?
                      AND parent_code=?
                    """,
                    (sender, chat, root_message, code),
                ).fetchone()
                chain_sequence = int(chain_sequence_row["max_sequence"]) + 1
                self._connection.execute(
                    f"""
                    INSERT INTO {USER_REPLY_CHAIN_TABLE}(
                        message_id, sender_id, chat_id, quoted_message_id,
                        root_message_id, parent_code, thread_id, turn_id,
                        delivery_id, delivery_sequence, chain_sequence,
                        reply_fingerprint, content_hash, state,
                        created_at, updated_at, expires_at, last_error_code
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'prepared',?,?,?,NULL)
                    """,
                    (
                        inbound,
                        sender,
                        chat,
                        quoted,
                        root_message,
                        code,
                        thread_id,
                        turn_id,
                        "",
                        0,
                        chain_sequence,
                        fingerprint,
                        digest,
                        timestamp,
                        timestamp,
                        expires_at,
                    ),
                )
                row = self._connection.execute(
                    f"SELECT * FROM {USER_REPLY_CHAIN_TABLE} WHERE message_id=?",
                    (inbound,),
                ).fetchone()
                if row is None:
                    raise UserReplyChainError("用户消息链准备后无法读取记录")
                self._connection.commit()
                return self._record(row, is_new=True)
            except BaseException:
                self._connection.rollback()
                raise

    def complete_queued_reply(
        self,
        inbound_message_id: str,
        *,
        delivery_id: str,
        now: int | None = None,
    ) -> ReplyChainRecord:
        """Finalize a prepared link only after ``reply_deliveries`` exists."""

        self._require_writable()
        inbound = _normalize(inbound_message_id, "inbound_message_id", maximum=512)
        delivery_key = _normalize(delivery_id, "delivery_id", maximum=512)
        timestamp = _timestamp(now)
        with self._lock:
            try:
                self._begin_immediate()
                row = self._connection.execute(
                    f"SELECT * FROM {USER_REPLY_CHAIN_TABLE} WHERE message_id=?",
                    (inbound,),
                ).fetchone()
                if row is None:
                    raise UserReplyChainRejected("用户消息链准备记录不存在")
                if str(row["delivery_id"] or ""):
                    if str(row["delivery_id"]) != delivery_key:
                        raise UserReplyChainConflict("同一用户消息已经绑定了另一条队列记录")
                    self._connection.commit()
                    return self._record(row)
                if str(row["state"]) != "prepared":
                    raise UserReplyChainNotQueued("用户消息链不在待入队状态")
                delivery = self._connection.execute(
                    """
                    SELECT d.delivery_id, d.parent_code, d.inbound_message_id,
                           d.reply_fingerprint, d.reply_text, d.sequence,
                           d.discarded_at, n.thread_id, n.turn_id, n.expires_at,
                           n.sent_at, n.reply_kind
                    FROM reply_deliveries AS d
                    JOIN notifications AS n ON n.code=d.parent_code
                    WHERE d.delivery_id=?
                    """,
                    (delivery_key,),
                ).fetchone()
                if delivery is None:
                    raise UserReplyChainNotQueued("普通回复队列记录尚未持久化")
                if (
                    str(delivery["inbound_message_id"]) != inbound
                    or str(delivery["parent_code"]) != str(row["parent_code"])
                    or str(delivery["reply_fingerprint"]) != str(row["reply_fingerprint"])
                    or str(delivery["thread_id"]) != str(row["thread_id"])
                    or str(delivery["turn_id"]) != str(row["turn_id"])
                    or str(delivery["reply_kind"]) != "turn"
                    or delivery["sent_at"] is None
                ):
                    raise UserReplyChainConflict("普通回复队列记录与用户消息链不一致")
                if delivery["discarded_at"] is not None or delivery["reply_text"] is None:
                    raise UserReplyChainRejected("普通回复队列记录已被丢弃")
                if int(delivery["expires_at"]) < timestamp:
                    raise UserReplyChainExpired("普通回复队列记录对应通知已过期")
                if content_hash(str(delivery["reply_text"]).strip()) != str(row["content_hash"]):
                    raise UserReplyChainConflict("普通回复队列正文哈希与用户消息链不一致")
                cursor = self._connection.execute(
                    f"""
                    UPDATE {USER_REPLY_CHAIN_TABLE}
                    SET delivery_id=?, delivery_sequence=?, state='queued',
                        updated_at=?, last_error_code=NULL
                    WHERE message_id=? AND state='prepared' AND delivery_id=''
                    """,
                    (
                        delivery_key,
                        int(delivery["sequence"]),
                        timestamp,
                        inbound,
                    ),
                )
                if cursor.rowcount != 1:
                    raise UserReplyChainError("用户消息链入队状态发生并发变化")
                updated = self._connection.execute(
                    f"SELECT * FROM {USER_REPLY_CHAIN_TABLE} WHERE message_id=?",
                    (inbound,),
                ).fetchone()
                if updated is None:
                    raise UserReplyChainError("用户消息链入队后无法读取记录")
                self._connection.commit()
                return self._record(updated)
            except BaseException:
                self._connection.rollback()
                raise

    def abort_prepared_reply(
        self,
        inbound_message_id: str,
        *,
        error_code: str = "queue_rejected",
        now: int | None = None,
    ) -> bool:
        """Close a prepared link when the normal outbox explicitly failed."""

        self._require_writable()
        inbound = _normalize(inbound_message_id, "inbound_message_id", maximum=512)
        error = _normalize(error_code, "error_code", maximum=120)
        timestamp = _timestamp(now)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                f"""
                UPDATE {USER_REPLY_CHAIN_TABLE}
                SET state='rejected', updated_at=?, last_error_code=?
                WHERE message_id=? AND state='prepared' AND delivery_id=''
                """,
                (timestamp, error, inbound),
            )
        return cursor.rowcount == 1

    def reconcile_prepared(
        self, *, now: int | None = None, limit: int = 256
    ) -> tuple[ReplyChainRecord, ...]:
        """Recover prepared links whose normal outbox write won the race.

        Rows with no matching outbox entry are deliberately left prepared so a
        caller can retry or explicitly abort them.  Expired rows are marked
        expired and can never become routes later.
        """

        self._require_writable()
        if isinstance(limit, bool) or not 1 <= int(limit) <= 2048:
            raise ValueError("limit 必须介于 1 和 2048 之间")
        timestamp = _timestamp(now)
        recovered: list[ReplyChainRecord] = []
        with self._lock:
            try:
                self._begin_immediate()
                rows = self._connection.execute(
                    f"""
                    SELECT * FROM {USER_REPLY_CHAIN_TABLE}
                    WHERE state='prepared' ORDER BY created_at, message_id LIMIT ?
                    """,
                    (int(limit),),
                ).fetchall()
                for row in rows:
                    if int(row["expires_at"]) < timestamp:
                        self._connection.execute(
                            f"""
                            UPDATE {USER_REPLY_CHAIN_TABLE}
                            SET state='expired', updated_at=?, last_error_code='expired'
                            WHERE message_id=? AND state='prepared'
                            """,
                            (timestamp, str(row["message_id"])),
                        )
                        continue
                    delivery = self._connection.execute(
                        """
                        SELECT d.delivery_id, d.reply_text, d.sequence,
                               d.discarded_at, d.reply_fingerprint,
                               n.thread_id, n.turn_id, n.expires_at,
                               n.sent_at, n.reply_kind
                        FROM reply_deliveries AS d
                        JOIN notifications AS n ON n.code=d.parent_code
                        WHERE d.inbound_message_id=? AND d.parent_code=?
                        ORDER BY d.sequence DESC LIMIT 1
                        """,
                        (str(row["message_id"]), str(row["parent_code"])),
                    ).fetchone()
                    if delivery is None:
                        continue
                    if (
                        delivery["discarded_at"] is not None
                        or delivery["reply_text"] is None
                    ):
                        self._connection.execute(
                            f"""
                            UPDATE {USER_REPLY_CHAIN_TABLE}
                            SET state='rejected', updated_at=?, last_error_code='queue_discarded'
                            WHERE message_id=? AND state='prepared'
                            """,
                            (timestamp, str(row["message_id"])),
                        )
                        continue
                    if (
                        str(delivery["reply_fingerprint"]) != str(row["reply_fingerprint"])
                        or content_hash(str(delivery["reply_text"]).strip())
                        != str(row["content_hash"])
                        or str(delivery["thread_id"]) != str(row["thread_id"])
                        or str(delivery["turn_id"]) != str(row["turn_id"])
                        or delivery["sent_at"] is None
                        or str(delivery["reply_kind"]) != "turn"
                    ):
                        self._connection.execute(
                            f"""
                            UPDATE {USER_REPLY_CHAIN_TABLE}
                            SET state='rejected', updated_at=?, last_error_code='queue_conflict'
                            WHERE message_id=? AND state='prepared'
                            """,
                            (timestamp, str(row["message_id"])),
                        )
                        continue
                    self._connection.execute(
                        f"""
                        UPDATE {USER_REPLY_CHAIN_TABLE}
                        SET delivery_id=?, delivery_sequence=?, state='queued',
                            updated_at=?, last_error_code=NULL
                        WHERE message_id=? AND state='prepared'
                        """,
                        (
                            str(delivery["delivery_id"]),
                            int(delivery["sequence"]),
                            timestamp,
                            str(row["message_id"]),
                        ),
                    )
                    updated = self._connection.execute(
                        f"SELECT * FROM {USER_REPLY_CHAIN_TABLE} WHERE message_id=?",
                        (str(row["message_id"]),),
                    ).fetchone()
                    if updated is not None and str(updated["state"]) == "queued":
                        recovered.append(self._record(updated))
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
        return tuple(recovered)

    def inspect_parent(
        self,
        message_id: str,
        *,
        sender_id: str,
        chat_id: str,
        now: int | None = None,
    ) -> ReplyChainResolution:
        """Resolve only an exact quoted user message in its owner/chat scope."""

        identifier = _normalize(message_id, "message_id", maximum=512)
        sender = _normalize(sender_id, "sender_id", maximum=1024)
        chat = _normalize(chat_id, "chat_id", maximum=1024)
        timestamp = _timestamp(now)
        with self._lock:
            row = self._connection.execute(
                f"SELECT * FROM {USER_REPLY_CHAIN_TABLE} WHERE message_id=?",
                (identifier,),
            ).fetchone()
            if row is None:
                return ReplyChainResolution("missing", reason="未知的用户消息父 ID")
            record = self._record(row)
            if record.sender_id != sender or record.chat_id != chat:
                return ReplyChainResolution("cross_scope", reason="父消息不属于当前用户或私聊")
            if record.expires_at < timestamp:
                return ReplyChainResolution("expired", record, "用户消息链已过期")
            if record.state in {"prepared"}:
                return ReplyChainResolution("not_queued", record, "用户消息尚未可靠入队")
            if record.state in {"rejected", "discarded", "expired"}:
                return ReplyChainResolution(record.state, record, "用户消息链已失效")
            delivery = self._connection.execute(
                """
                SELECT d.parent_code, d.inbound_message_id, d.reply_fingerprint,
                       d.reply_text, d.discarded_at, n.thread_id, n.turn_id, n.expires_at,
                       n.sent_at, n.reply_kind
                FROM reply_deliveries AS d
                JOIN notifications AS n ON n.code=d.parent_code
                WHERE d.delivery_id=?
                """,
                (record.delivery_id,),
            ).fetchone()
            if delivery is None:
                return ReplyChainResolution(
                    "not_queued", record, "普通回复队列记录不存在"
                )
            if str(delivery["inbound_message_id"]) != identifier:
                return ReplyChainResolution("conflict", record, "队列入站 ID 与父消息不一致")
            if str(delivery["parent_code"]) != record.parent_code:
                return ReplyChainResolution("conflict", record, "队列通知编号与父消息不一致")
            if str(delivery["reply_fingerprint"]) != record.reply_fingerprint:
                return ReplyChainResolution("conflict", record, "队列指纹与父消息不一致")
            if delivery["discarded_at"] is not None:
                return ReplyChainResolution("discarded", record, "队列记录已被丢弃")
            if delivery["reply_text"] is None:
                return ReplyChainResolution("conflict", record, "队列正文缺失")
            if content_hash(str(delivery["reply_text"]).strip()) != record.content_hash:
                return ReplyChainResolution("conflict", record, "队列正文发生变化")
            if delivery["sent_at"] is None or str(delivery["reply_kind"]) != "turn":
                return ReplyChainResolution("not_queued", record, "队列目标尚未成为普通轮次")
            if int(delivery["expires_at"]) < timestamp:
                return ReplyChainResolution("expired", record, "原始进度通知已过期")
            if (
                str(delivery["thread_id"]) != record.thread_id
                or str(delivery["turn_id"]) != record.turn_id
            ):
                return ReplyChainResolution("conflict", record, "队列目标与父消息记录不一致")
            return ReplyChainResolution("ready", record)

    def resolve_parent(
        self,
        message_id: str,
        *,
        sender_id: str,
        chat_id: str,
        now: int | None = None,
    ) -> ReplyChainRecord | None:
        """Return a route only for a ``ready`` exact-parent proof."""

        result = self.inspect_parent(
            message_id, sender_id=sender_id, chat_id=chat_id, now=now
        )
        return result.record if result.status == "ready" else None

    def get(self, message_id: str) -> ReplyChainRecord | None:
        identifier = _normalize(message_id, "message_id", maximum=512)
        with self._lock:
            row = self._connection.execute(
                f"SELECT * FROM {USER_REPLY_CHAIN_TABLE} WHERE message_id=?",
                (identifier,),
            ).fetchone()
        return None if row is None else self._record(row)

    def durable_ack_status(
        self, message_id: str, *, now: int | None = None
    ) -> str:
        """Return the worker-side transport ACK state for one inbound ID.

        ``accepted`` means the ordinary reply outbox row and its exact route
        evidence are committed.  It deliberately does not mean that Codex has
        already consumed the row: ``queued`` is the durable hand-off boundary
        that lets guardian stop redelivering the same Feishu event.  A
        ``prepared`` row is still ``pending`` so a crash between the two
        writes remains recoverable.  The method never changes state and is
        safe to call from a guardian polling thread.
        """

        identifier = _normalize(message_id, "message_id", maximum=512)
        timestamp = _timestamp(now)
        with self._lock:
            row = self._connection.execute(
                f"SELECT * FROM {USER_REPLY_CHAIN_TABLE} WHERE message_id=?",
                (identifier,),
            ).fetchone()
            if row is None:
                return "missing"
            state = str(row["state"] or "")
            if state == "prepared":
                return "pending"
            if state in _TERMINAL_STATES:
                return "rejected"
            if state not in {"queued", "processing", "delivered", "uncertain"}:
                return "rejected"
            if int(row["expires_at"]) < timestamp:
                return "rejected"
            delivery_id = str(row["delivery_id"] or "")
            if not delivery_id:
                return "pending"
            delivery = self._connection.execute(
                """
                SELECT d.delivery_id, d.parent_code, d.inbound_message_id,
                       d.reply_fingerprint, d.reply_text, d.discarded_at,
                       n.thread_id, n.turn_id, n.sent_at, n.reply_kind
                FROM reply_deliveries AS d
                LEFT JOIN notifications AS n ON n.code=d.parent_code
                WHERE d.delivery_id=?
                """,
                (delivery_id,),
            ).fetchone()
            if delivery is None:
                return "pending"
            if (
                str(delivery["inbound_message_id"] or "") != identifier
                or str(delivery["parent_code"] or "") != str(row["parent_code"])
                or str(delivery["reply_fingerprint"] or "")
                != str(row["reply_fingerprint"])
                or str(delivery["thread_id"] or "") != str(row["thread_id"])
                or str(delivery["turn_id"] or "") != str(row["turn_id"])
                or str(delivery["reply_kind"] or "") != "turn"
                or delivery["sent_at"] is None
                or delivery["discarded_at"] is not None
                or delivery["reply_text"] is None
                or content_hash(str(delivery["reply_text"]).strip())
                != str(row["content_hash"])
            ):
                return "rejected"
            return "accepted"

    def set_state(
        self,
        message_id: str,
        state: str,
        *,
        error_code: str | None = None,
        now: int | None = None,
    ) -> bool:
        """Persist service processing state without accepting route changes."""

        self._require_writable()
        identifier = _normalize(message_id, "message_id", maximum=512)
        normalized = _normalize(state, "state", maximum=32)
        if normalized not in _STATES:
            raise ValueError("未知的用户消息链状态")
        error = None if error_code is None else _normalize(error_code, "error_code", maximum=120)
        timestamp = _timestamp(now)
        with self._lock, self._connection:
            row = self._connection.execute(
                f"SELECT state FROM {USER_REPLY_CHAIN_TABLE} WHERE message_id=?",
                (identifier,),
            ).fetchone()
            if row is None:
                return False
            old = str(row["state"])
            if old in _TERMINAL_STATES and normalized != old:
                raise UserReplyChainConflict("终态用户消息链不能回退或改写")
            cursor = self._connection.execute(
                f"""
                UPDATE {USER_REPLY_CHAIN_TABLE}
                SET state=?, updated_at=?, last_error_code=?
                WHERE message_id=? AND state=?
                """,
                (normalized, timestamp, error, identifier, old),
            )
        return cursor.rowcount == 1


__all__ = [
    "SCHEMA_SQL",
    "USER_REPLY_CHAIN_SCHEMA_VERSION",
    "USER_REPLY_CHAIN_TABLE",
    "ReplyChainRecord",
    "ReplyChainResolution",
    "UserReplyChainConflict",
    "UserReplyChainError",
    "UserReplyChainExpired",
    "UserReplyChainNotQueued",
    "UserReplyChainRejected",
    "UserReplyChainSchemaError",
    "UserReplyChainStore",
    "content_hash",
    "initialize_user_reply_chain_schema",
    "validate_user_reply_chain_schema",
]
