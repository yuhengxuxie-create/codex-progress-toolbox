"""普通任务完成总结的 ``.原文`` 持久路由与 exactly-once 回归。"""

from __future__ import annotations

import hashlib
import json
import queue
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from progress_wx.channel import ChannelAttachment, ChannelReply, MessageChannelOfflineError
from progress_wx.codex_store import CodexStore, StorePaths, ThreadStatus, TurnRecord
from progress_wx.feishu import FeishuMessageChannel
from progress_wx.models import NotificationReason, ProgressReport, TurnEvent
from progress_wx.service import ProgressService, _extract_feishu_parent_message
from progress_wx.state import SCHEMA_VERSION, CorrelationCodec, StateError, StateStore


OWNER = "ou_owner"
CHAT = "oc_private"
RAW = "完整原始答复\n\n第二段，保留原始换行。"
DIGEST = hashlib.sha256(RAW.encode("utf-8")).hexdigest()


def _raw_context(
    store: StateStore,
    *,
    event: TurnEvent | None = None,
    message_ids: tuple[str, ...] = ("om_summary",),
):
    item = event or TurnEvent("thread-raw", "turn-raw", "completed")
    store.reserve_notification(
        item,
        CorrelationCodec(b"r" * 32).issue(),
        "摘要",
        72,
        raw_sender_id=OWNER,
        raw_content_sha256=DIGEST,
    )
    store.bind_channel_messages_with_raw_context(
        item.dedupe_key,
        message_ids,
        chat_id=CHAT,
    )
    context = store.notification_raw_context_for_message(message_ids[0])
    assert context is not None
    return item, context


def _legacy_notification(
    store: StateStore,
    *,
    event: TurnEvent | None = None,
    message_ids: tuple[str, ...] = ("om_legacy",),
    reply_kind: str = "turn",
) -> TurnEvent:
    item = event or TurnEvent("thread-raw", "turn-raw", "completed")
    store.reserve_notification(
        item,
        CorrelationCodec(b"l" * 32).issue(),
        "旧完成摘要",
        72,
        reply_kind=reply_kind,
    )
    store.bind_channel_messages(item.dedupe_key, message_ids)
    store.mark_sent(item.dedupe_key)
    for message_id in message_ids:
        assert store.notification_raw_context_for_message(message_id) is None
    return item


def _service(tmp_path: Path, store: StateStore, turn: TurnRecord | None):
    service = ProgressService(tmp_path / "unused-config.yaml")
    service.config = SimpleNamespace(
        messaging=SimpleNamespace(backend="feishu", pending_ttl_hours=72),
        feishu=SimpleNamespace(target_open_id=OWNER),
        service=SimpleNamespace(
            max_attempts=1,
            retry_delays=(0.0,),
            pending_ttl_hours=72,
        ),
    )
    service.store = store
    service.codec = CorrelationCodec(b"s" * 32)

    class Codex:
        def get_turn(self, thread_id: str, turn_id: str):
            if turn is None:
                return None
            assert (thread_id, turn_id) == (turn.thread_id, turn.turn_id)
            return turn

        def require_readable(self, _operation: str) -> None:
            return None

    class Channel:
        def __init__(self) -> None:
            self.sent: list[tuple[str, str]] = []
            self.ids: tuple[str, ...] = ("om_raw_1",)

        def send_text(self, text: str, *, idempotency_key: str):
            self.sent.append((text, idempotency_key))
            return self.ids

        def is_online(self) -> bool:
            return True

        def recipient_scope_for_messages(self, message_ids: tuple[str, ...]):
            return (OWNER, CHAT) if message_ids == self.ids else None

    service.codex_store = Codex()  # type: ignore[assignment]
    service.channel = Channel()  # type: ignore[assignment]
    return service


def test_direct_reserve_and_all_summary_chunks_survive_restart(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    event = TurnEvent("thread", "turn", "completed")
    store = StateStore(database)
    store.reserve_notification(
        event,
        CorrelationCodec(b"a" * 32).issue(),
        "摘要",
        72,
        raw_sender_id=OWNER,
        raw_content_sha256=DIGEST,
    )
    store.close()

    reopened = StateStore(database)
    try:
        assert reopened.notification_raw_binding_prepared(event.dedupe_key)
        reopened.bind_channel_messages_with_raw_context(
            event.dedupe_key,
            ("om_chunk_1", "om_chunk_2", "om_chunk_3"),
            chat_id=CHAT,
        )
        for message_id in ("om_chunk_1", "om_chunk_2", "om_chunk_3"):
            context = reopened.notification_raw_context_for_message(message_id)
            assert context is not None
            assert context.event_key == event.dedupe_key
            assert (context.thread_id, context.turn_id) == ("thread", "turn")
            assert context.content_sha256 == DIGEST
    finally:
        reopened.close()


def test_schema20_migrates_to_current_and_read_only_schema20_does_not_create_raw_tables(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite"
    current = StateStore(database)
    current.close()
    connection = sqlite3.connect(database)
    try:
        connection.execute("DROP TABLE notification_raw_deliveries")
        connection.execute("DROP TABLE notification_raw_contexts")
        connection.execute("DROP TABLE notification_raw_bindings")
        connection.execute(
            "UPDATE meta SET value='20' WHERE key='schema_version'"
        )
        connection.commit()
    finally:
        connection.close()

    before = database.read_bytes()
    readonly = StateStore.open_read_only(database)
    readonly.close()
    assert database.read_bytes() == before

    migrated = StateStore(database)
    try:
        assert migrated.notification_raw_context_for_message("om_missing") is None
        row = migrated._connection.execute(  # noqa: SLF001 - migration evidence
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        assert row is not None and row[0] == str(SCHEMA_VERSION)
    finally:
        migrated.close()


def test_schema20_legacy_completion_materializes_only_quoted_chunk_and_repeats(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite"
    event = TurnEvent("thread-raw", "turn-raw", "completed")
    current = StateStore(database)
    _legacy_notification(
        current,
        event=event,
        message_ids=("om_legacy_1", "om_legacy_2"),
    )
    current.close()
    connection = sqlite3.connect(database)
    try:
        connection.execute("DROP TABLE notification_raw_deliveries")
        connection.execute("DROP TABLE notification_raw_contexts")
        connection.execute("DROP TABLE notification_raw_bindings")
        connection.execute("UPDATE meta SET value='20' WHERE key='schema_version'")
        connection.commit()
    finally:
        connection.close()

    store = StateStore(database)
    turn = TurnRecord(
        event.thread_id,
        event.turn_id,
        ThreadStatus.COMPLETED,
        final_agent_item_id="item-final",
        final_message=RAW,
    )
    service = _service(tmp_path, store, turn)
    first = ChannelReply(
        sender_id=OWNER,
        chat_id=CHAT,
        message_id="om_legacy_request_1",
        reply_to_message_id="om_legacy_2",
        content=".原文",
    )
    try:
        assert service._process_channel_reply(first) is True
        context = store.notification_raw_context_for_message("om_legacy_2")
        assert context is not None
        assert context.content_sha256 == DIGEST
        assert store.notification_raw_context_for_message("om_legacy_1") is None

        claimed = store.claim_notification_raw_delivery()
        assert claimed is not None
        service._process_notification_raw_delivery(claimed)
        assert service._process_channel_reply(first) is True
        assert store.claim_notification_raw_delivery() is None

        second = ChannelReply(
            sender_id=OWNER,
            chat_id=CHAT,
            message_id="om_legacy_request_2",
            reply_to_message_id="om_legacy_2",
            content=".原文",
        )
        assert service._process_channel_reply(second) is True
        claimed_again = store.claim_notification_raw_delivery()
        assert claimed_again is not None
        assert claimed_again.inbound_message_id == "om_legacy_request_2"
    finally:
        store.close()


def test_legacy_completion_recovers_exact_raw_from_trusted_rollout_after_restart(
    tmp_path: Path,
) -> None:
    service_database = tmp_path / "service.sqlite"
    event = TurnEvent("thread-rollout", "turn-requested", "completed")
    initial = StateStore(service_database)
    _legacy_notification(
        initial,
        event=event,
        message_ids=("om_legacy_rollout",),
    )
    initial.close()

    codex_home = tmp_path / "codex-home"
    state_database = codex_home / "state_5.sqlite"
    history_database = codex_home / "thread_history_1.sqlite"
    sessions = codex_home / "sessions" / "2026" / "09" / "04"
    sessions.mkdir(parents=True)
    rollout = sessions / "rollout-thread-rollout.jsonl"
    rollout.write_text(
        "\n".join(
            json.dumps(item, ensure_ascii=False)
            for item in (
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": "turn-requested",
                        "last_agent_message": RAW,
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": "turn-newer",
                        "last_agent_message": "newer response",
                    },
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )
    connection = sqlite3.connect(state_database)
    try:
        connection.executescript(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                title TEXT,
                name TEXT,
                cwd TEXT,
                updated_at_ms INTEGER,
                created_at_ms INTEGER,
                archived INTEGER,
                preview TEXT,
                source TEXT,
                thread_source TEXT,
                rollout_path TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO threads VALUES (?, ?, '', ?, ?, ?, 0, '', '', 'user', ?)",
            (
                event.thread_id,
                "合成旧完成任务",
                "D:/synthetic",
                2_000,
                1_000,
                str(rollout),
            ),
        )
        connection.commit()
    finally:
        connection.close()
    connection = sqlite3.connect(history_database)
    try:
        connection.executescript(
            """
            CREATE TABLE thread_turns (
                thread_id TEXT,
                turn_id TEXT,
                rollout_ordinal INTEGER,
                status TEXT,
                error_json TEXT,
                started_at INTEGER,
                completed_at INTEGER,
                duration_ms INTEGER,
                final_agent_item_id TEXT
            );
            CREATE TABLE thread_items (
                thread_id TEXT,
                turn_id TEXT,
                item_id TEXT,
                rollout_ordinal INTEGER,
                created_at_ms INTEGER,
                item_json TEXT,
                item_type TEXT,
                updated_at_ordinal INTEGER
            );
            """
        )
        connection.commit()
    finally:
        connection.close()

    reopened = StateStore(service_database)
    service = _service(tmp_path, reopened, None)
    service.codex_store = CodexStore(
        StorePaths(state_database, history_database, codex_home / "session_index.jsonl")
    )
    message = ChannelReply(
        sender_id=OWNER,
        chat_id=CHAT,
        message_id="om_legacy_rollout_request",
        reply_to_message_id="om_legacy_rollout",
        content=".原文",
    )
    try:
        assert service._process_channel_reply(message) is True
        claimed = reopened.claim_notification_raw_delivery()
        assert claimed is not None
        service._process_notification_raw_delivery(claimed)
        assert service.channel.sent[0][0] == RAW  # type: ignore[attr-defined]
        assert "newer response" not in service.channel.sent[0][0]  # type: ignore[attr-defined]
    finally:
        reopened.close()


def test_legacy_source_rejects_wrong_status_event_and_rpc_hook(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    try:
        cases = (
            (TurnEvent("thread-wait", "turn-wait", "waitingOnApproval"), "turn"),
            (TurnEvent("thread-rpc", "turn-rpc", "completed"), "rpc"),
            (TurnEvent("thread-hook", "turn-hook", "completed"), "hook"),
            (TurnEvent("thread-fake", "turn-fake", "completed-extra"), "turn"),
        )
        for index, (event, reply_kind) in enumerate(cases):
            message_id = f"om_legacy_invalid_{index}"
            _legacy_notification(
                store,
                event=event,
                message_ids=(message_id,),
                reply_kind=reply_kind,
            )
            assert (
                store.notification_raw_legacy_source_for_message(message_id)
                is None
            )
    finally:
        store.close()


@pytest.mark.parametrize(
    ("turn", "message_id"),
    (
        (None, "om_legacy_missing"),
        (
            TurnRecord(
                "thread-raw",
                "turn-raw",
                ThreadStatus.IN_PROGRESS,
                final_agent_item_id="item-final",
                final_message=RAW,
            ),
            "om_legacy_noncompleted",
        ),
    ),
)
def test_legacy_completion_requires_exact_readable_completed_turn(
    tmp_path: Path,
    turn: TurnRecord | None,
    message_id: str,
) -> None:
    store = StateStore(tmp_path / f"{message_id}.sqlite")
    _legacy_notification(store, message_ids=(message_id,))
    service = _service(tmp_path, store, turn)
    try:
        assert service._process_channel_reply(
            ChannelReply(
                sender_id=OWNER,
                chat_id=CHAT,
                message_id=f"{message_id}_request",
                reply_to_message_id=message_id,
                content=".原文",
            )
        ) is True
        assert store.notification_raw_context_for_message(message_id) is None
        assert store.claim_notification_raw_delivery() is None
    finally:
        store.close()


def test_legacy_completion_rejects_cross_owner_then_locks_first_chat(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    _legacy_notification(
        store, message_ids=("om_legacy_scope_1", "om_legacy_scope_2")
    )
    turn = TurnRecord(
        "thread-raw",
        "turn-raw",
        ThreadStatus.COMPLETED,
        final_agent_item_id="item-final",
        final_message=RAW,
    )
    service = _service(tmp_path, store, turn)
    try:
        assert service._process_channel_reply(
            ChannelReply(
                sender_id="ou_other",
                chat_id=CHAT,
                message_id="om_cross_owner",
                reply_to_message_id="om_legacy_scope_1",
                content=".原文",
            )
        ) is False
        assert (
            store.notification_raw_context_for_message("om_legacy_scope_1")
            is None
        )

        assert service._process_channel_reply(
            ChannelReply(
                sender_id=OWNER,
                chat_id=CHAT,
                message_id="om_first_chat",
                reply_to_message_id="om_legacy_scope_1",
                content=".原文",
            )
        ) is True
        assert (
            store.notification_raw_context_for_message("om_legacy_scope_1")
            is not None
        )
        assert service._process_channel_reply(
            ChannelReply(
                sender_id=OWNER,
                chat_id="oc_other",
                message_id="om_cross_chat",
                reply_to_message_id="om_legacy_scope_2",
                content=".原文",
            )
        ) is True
        assert (
            store.notification_raw_context_for_message("om_legacy_scope_2")
            is None
        )
        deliveries = store._connection.execute(  # noqa: SLF001 - scope evidence
            "SELECT COUNT(*) FROM notification_raw_deliveries"
        ).fetchone()
        assert deliveries is not None and deliveries[0] == 1
    finally:
        store.close()


def test_legacy_materialization_rechecks_mapping_and_pruned_parent(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    event = _legacy_notification(store, message_ids=("om_legacy_recheck",))
    source = store.notification_raw_legacy_source_for_message("om_legacy_recheck")
    assert source is not None
    try:
        with store._connection:  # noqa: SLF001 - simulate mapping removal race
            store._connection.execute(  # noqa: SLF001
                "DELETE FROM notifications WHERE event_key=?",
                (event.dedupe_key,),
            )
        with pytest.raises(StateError):
            store.materialize_notification_raw_legacy_context(
                source,
                sender_id=OWNER,
                chat_id=CHAT,
                content_sha256=DIGEST,
            )
        assert (
            store.notification_raw_context_for_message("om_legacy_recheck")
            is None
        )
    finally:
        store.close()


def test_raw_context_rejects_unbound_or_other_event_message_ids_atomically(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    first = TurnEvent("thread", "turn-1", "completed")
    second = TurnEvent("thread", "turn-2", "completed")
    try:
        for event in (first, second):
            store.reserve_notification(
                event,
                CorrelationCodec(b"b" * 32).issue(),
                "摘要",
                72,
                raw_sender_id=OWNER,
                raw_content_sha256=DIGEST,
            )
        store.bind_channel_messages(second.dedupe_key, ("om_other",))
        with pytest.raises(StateError):
            store.finalize_notification_raw_binding(
                first.dedupe_key,
                chat_id=CHAT,
                message_ids=("om_unbound",),
            )
        with pytest.raises(StateError):
            store.finalize_notification_raw_binding(
                first.dedupe_key,
                chat_id=CHAT,
                message_ids=("om_first", "om_other"),
            )
        assert store.code_for_channel_message("om_first") is None
        assert store.notification_raw_context_for_message("om_first") is None
    finally:
        store.close()


def test_summary_delivered_and_raw_context_commit_in_one_transaction(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    event = TurnEvent("thread", "turn", "completed")
    try:
        store.reserve_notification_summary_only(
            event, CorrelationCodec(b"c" * 32).issue(), 72
        )
        store.prepare_notification_raw_binding(
            event.dedupe_key,
            sender_id=OWNER,
            thread_id=event.thread_id,
            turn_id=event.turn_id,
            content_sha256=DIGEST,
        )
        claimed = store.claim_notification_summary(event.dedupe_key)
        assert claimed is not None
        assert store.prepare_notification_summary(event.dedupe_key, "摘要")
        assert store.mark_notification_summary_submitted(event.dedupe_key)
        assert store.mark_notification_summary_delivered_with_raw_context(
            event.dedupe_key,
            ("om_summary_1", "om_summary_2"),
            chat_id=CHAT,
        )
        assert store.notification_summary_delivered(event.dedupe_key)
        assert (
            store.notification_raw_context_for_message("om_summary_2")
            is not None
        )
    finally:
        store.close()


def test_summary_raw_context_collision_rolls_back_delivered_and_all_ids(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    event = TurnEvent("thread", "turn", "completed")
    other = TurnEvent("other", "other-turn", "completed")
    try:
        store.reserve_notification_summary_only(
            event, CorrelationCodec(b"d" * 32).issue(), 72
        )
        store.prepare_notification_raw_binding(
            event.dedupe_key,
            sender_id=OWNER,
            thread_id=event.thread_id,
            turn_id=event.turn_id,
            content_sha256=DIGEST,
        )
        claimed = store.claim_notification_summary(event.dedupe_key)
        assert claimed is not None
        assert store.prepare_notification_summary(event.dedupe_key, "摘要")
        assert store.mark_notification_summary_submitted(event.dedupe_key)
        store.reserve_notification(
            other, CorrelationCodec(b"e" * 32).issue(), "其它", 72
        )
        store.bind_channel_messages(other.dedupe_key, ("om_other",))

        with pytest.raises(StateError):
            store.mark_notification_summary_delivered_with_raw_context(
                event.dedupe_key,
                ("om_new", "om_other"),
                chat_id=CHAT,
            )
        assert store.notification_summary_delivered(event.dedupe_key) is False
        assert store.code_for_channel_message("om_new") is None
        assert store.notification_raw_context_for_message("om_new") is None
    finally:
        store.close()


def test_same_summary_allows_multiple_requests_but_same_inbound_is_once(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    try:
        _event, context = _raw_context(store)
        first = store.reserve_notification_raw_delivery(
            context, inbound_message_id="om_in_1", fingerprint="f" * 64
        )
        replay = store.reserve_notification_raw_delivery(
            context, inbound_message_id="om_in_1", fingerprint="f" * 64
        )
        second = store.reserve_notification_raw_delivery(
            context, inbound_message_id="om_in_2", fingerprint="g" * 64
        )
        assert first.is_new is True
        assert replay.is_new is False
        assert replay.delivery_id == first.delivery_id
        assert second.is_new is True
        assert second.delivery_id != first.delivery_id
    finally:
        store.close()


def test_raw_context_survives_parent_notification_prune(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    event, _context = _raw_context(store)
    try:
        store.mark_sent(event.dedupe_key)
        with store._connection:  # noqa: SLF001 - force a synthetic old parent
            store._connection.execute(  # noqa: SLF001
                "UPDATE notifications SET created_at=0, expires_at=0 WHERE event_key=?",
                (event.dedupe_key,),
            )
        store.prune(retention_days=1, now=200_000)
        assert store.code_for_channel_message("om_summary") is None
        context = store.notification_raw_context_for_message("om_summary")
        assert context is not None
        assert context.event_key == event.dedupe_key
        delivery = store.reserve_notification_raw_delivery(
            context, inbound_message_id="om_after_prune", fingerprint="p" * 64
        )
        assert delivery.is_new is True
    finally:
        store.close()


def test_service_returns_exact_raw_in_all_chunks_and_deduplicates_inbound(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    _event, _context = _raw_context(
        store, message_ids=("om_summary_1", "om_summary_2")
    )
    turn = TurnRecord(
        "thread-raw",
        "turn-raw",
        ThreadStatus.COMPLETED,
        final_agent_item_id="item-final",
        final_message=RAW,
    )
    service = _service(tmp_path, store, turn)
    service.channel.ids = ("om_raw_1", "om_raw_2", "om_raw_3")  # type: ignore[attr-defined]
    message = ChannelReply(
        sender_id=OWNER,
        chat_id=CHAT,
        message_id="om_inbound",
        reply_to_message_id="om_summary_2",
        content=".原文",
    )
    try:
        store.mark_sent(_event.dedupe_key)
        assert service._process_channel_reply(message) is True
        delivery = store.claim_notification_raw_delivery()
        assert delivery is not None
        service._process_notification_raw_delivery(delivery)
        saved = store.notification_raw_delivery(delivery.delivery_id)
        assert saved is not None
        assert saved.state == "delivered"
        assert saved.result_message_ids == ("om_raw_1", "om_raw_2", "om_raw_3")
        raw_code = store.code_for_channel_message("om_raw_2")
        assert raw_code is not None
        assert store.peek_reply(raw_code, CorrelationCodec(b"r" * 32)) == (
            "thread-raw",
            "turn-raw",
            "turn",
        )
        service.codec = CorrelationCodec(b"r" * 32)
        followup = ChannelReply(
            sender_id=OWNER,
            chat_id=CHAT,
            message_id="om_followup",
            reply_to_message_id="om_raw_2",
            content="只回复收到，不执行其它操作",
        )
        assert service._process_channel_reply(followup) is True
        job = service.reply_queue.get_nowait()
        assert job.thread_id == "thread-raw"
        assert job.reply_text == "只回复收到，不执行其它操作"
        cross_chat = ChannelReply(
            sender_id=OWNER,
            chat_id="oc_other_private",
            message_id="om_cross_chat",
            reply_to_message_id="om_raw_2",
            content="不应跨聊天投递",
        )
        assert service._process_channel_reply(cross_chat) is False
        assert service.reply_queue.empty()
        assert service.channel.sent == [  # type: ignore[attr-defined]
            (RAW, f"notification-raw:{delivery.delivery_id}")
        ]
        assert service._process_channel_reply(message) is True
        assert store.claim_notification_raw_delivery() is None
        assert len(service.channel.sent) == 1  # type: ignore[attr-defined]
    finally:
        store.close()


def test_raw_result_message_aliases_survive_restart_and_legacy_rows_resolve(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite"
    store = StateStore(database)
    event, context = _raw_context(store)
    store.mark_sent(event.dedupe_key)
    delivery = store.reserve_notification_raw_delivery(
        context,
        inbound_message_id="om_raw_request",
        fingerprint="q" * 64,
    )
    claimed = store.claim_notification_raw_delivery()
    assert claimed is not None and claimed.delivery_id == delivery.delivery_id
    assert store.prepare_notification_raw_delivery(delivery.delivery_id, "raw")
    assert store.mark_notification_raw_submitted(delivery.delivery_id)
    assert store.mark_notification_raw_delivered(
        delivery.delivery_id,
        ("om_raw_a", "om_raw_b"),
    )
    store.close()

    reopened = StateStore(database)
    try:
        for message_id in ("om_raw_a", "om_raw_b"):
            code = reopened.code_for_channel_message(message_id)
            assert code is not None
            assert reopened.peek_reply(code, CorrelationCodec(b"r" * 32)) == (
                event.thread_id,
                event.turn_id,
                "turn",
            )

        # 模拟补丁部署前已经成功送达的 schema21 记录：结果 ID 仍只在
        # raw outbox 的 JSON 中。兼容读取必须精确恢复，不能要求用户重查会话。
        with reopened._connection:  # noqa: SLF001 - synthetic legacy row
            reopened._connection.execute(  # noqa: SLF001
                "DELETE FROM notification_raw_contexts WHERE message_id='om_raw_b'"
            )
            reopened._connection.execute(  # noqa: SLF001
                "DELETE FROM notification_message_ids WHERE message_id='om_raw_b'"
            )
        legacy_code = reopened.code_for_channel_message("om_raw_b")
        assert legacy_code is not None
        assert reopened.peek_reply(legacy_code, CorrelationCodec(b"r" * 32)) == (
            event.thread_id,
            event.turn_id,
            "turn",
        )

        other_event = TurnEvent("other-thread", "other-turn", "completed")
        _, other_context = _raw_context(
            reopened,
            event=other_event,
            message_ids=("om_other_summary",),
        )
        reopened.mark_sent(other_event.dedupe_key)
        other_delivery = reopened.reserve_notification_raw_delivery(
            other_context,
            inbound_message_id="om_ambiguous_request",
            fingerprint="a" * 64,
        )
        assert reopened.claim_notification_raw_delivery() is not None
        assert reopened.prepare_notification_raw_delivery(other_delivery.delivery_id, "raw")
        assert reopened.mark_notification_raw_submitted(other_delivery.delivery_id)
        with reopened._connection:  # noqa: SLF001 - synthetic corrupt legacy row
            reopened._connection.execute(  # noqa: SLF001
                """
                UPDATE notification_raw_deliveries
                SET delivered_at=1, result_message_ids_json='["om_raw_b"]'
                WHERE delivery_id=?
                """,
                (other_delivery.delivery_id,),
            )
        assert reopened.notification_raw_context_for_message("om_raw_b") is None
        assert reopened.code_for_channel_message("om_raw_b") is None
    finally:
        reopened.close()


def test_raw_result_alias_collision_rolls_back_delivery_and_new_aliases(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    event, context = _raw_context(store)
    other = TurnEvent("other-thread", "other-turn", "completed")
    try:
        store.mark_sent(event.dedupe_key)
        store.reserve_notification(
            other,
            CorrelationCodec(b"o" * 32).issue(),
            "other",
            72,
        )
        store.bind_channel_messages(other.dedupe_key, ("om_conflict",))
        store.mark_sent(other.dedupe_key)
        delivery = store.reserve_notification_raw_delivery(
            context,
            inbound_message_id="om_collision_request",
            fingerprint="c" * 64,
        )
        assert store.claim_notification_raw_delivery() is not None
        assert store.prepare_notification_raw_delivery(delivery.delivery_id, "raw")
        assert store.mark_notification_raw_submitted(delivery.delivery_id)

        with pytest.raises(StateError):
            store.mark_notification_raw_delivered(
                delivery.delivery_id,
                ("om_new_alias", "om_conflict"),
            )

        saved = store.notification_raw_delivery(delivery.delivery_id)
        assert saved is not None and saved.state == "submitted"
        assert saved.delivered_at is None
        assert saved.result_message_ids == ()
        assert store.code_for_channel_message("om_new_alias") is None
        assert store.notification_raw_context_for_message("om_new_alias") is None
    finally:
        store.close()


def test_replying_to_raw_fragment_rejects_archived_source_thread(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    event, context = _raw_context(store)
    service = _service(tmp_path, store, None)
    service.codec = CorrelationCodec(b"r" * 32)
    try:
        store.mark_sent(event.dedupe_key)
        delivery = store.reserve_notification_raw_delivery(
            context,
            inbound_message_id="om_archive_raw_request",
            fingerprint="z" * 64,
        )
        assert store.claim_notification_raw_delivery() is not None
        assert store.prepare_notification_raw_delivery(delivery.delivery_id, "raw")
        assert store.mark_notification_raw_submitted(delivery.delivery_id)
        assert store.mark_notification_raw_delivered(
            delivery.delivery_id,
            ("om_archived_raw",),
        )
        service.codex_store.get_thread = (  # type: ignore[attr-defined,method-assign]
            lambda _thread_id: SimpleNamespace(archived=True)
        )
        accepted = service._process_channel_reply(
            ChannelReply(
                sender_id=OWNER,
                chat_id=CHAT,
                message_id="om_archived_followup",
                reply_to_message_id="om_archived_raw",
                content="不应投递到归档会话",
            )
        )
        assert accepted is False
        assert service.reply_queue.empty()
        receipt = service.receipt_queue.get_nowait()
        assert receipt.received is False
        assert "归档" in receipt.details
    finally:
        store.close()


def test_background_completion_summary_atomically_binds_all_chunks(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    event = TurnEvent(
        "thread-raw",
        "turn-raw",
        "completed",
        title="测试会话",
        final_message=RAW,
    )
    turn = TurnRecord(
        event.thread_id,
        event.turn_id,
        ThreadStatus.COMPLETED,
        final_agent_item_id="item-final",
        final_message=RAW,
    )
    service = _service(tmp_path, store, turn)
    service.channel.ids = ("om_summary_a", "om_summary_b")  # type: ignore[attr-defined]

    class Summarizer:
        def summarize(self, _event: TurnEvent, *, wait=None):
            del wait
            return ProgressReport(
                "完成",
                "这是易读摘要。",
                NotificationReason.CONVERSATION_COMPLETE,
            )

    service.summarizer = Summarizer()  # type: ignore[assignment]
    service._remember_summary_event(event)
    store.reserve_notification_summary_only(
        event, CorrelationCodec(b"j" * 32).issue(), 72
    )
    delivery = store.claim_notification_summary(event.dedupe_key)
    assert delivery is not None
    try:
        service._process_notification_summary(delivery)
        assert store.notification_summary_delivered(event.dedupe_key)
        for message_id in ("om_summary_a", "om_summary_b"):
            context = store.notification_raw_context_for_message(message_id)
            assert context is not None
            assert context.event_key == event.dedupe_key
            assert context.content_sha256 == DIGEST
        for table in (
            "notification_raw_bindings",
            "notification_raw_contexts",
            "notification_raw_deliveries",
        ):
            rows = store._connection.execute(  # noqa: SLF001 - privacy assertion
                f'SELECT * FROM "{table}"'
            ).fetchall()
            assert RAW not in repr([tuple(row) for row in rows])
    finally:
        store.close()


def test_direct_completion_report_reserves_and_binds_raw_in_same_flow(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    event = TurnEvent(
        "thread-raw",
        "turn-raw",
        "completed",
        title="测试会话",
        final_message=RAW,
    )
    turn = TurnRecord(
        event.thread_id,
        event.turn_id,
        ThreadStatus.COMPLETED,
        final_agent_item_id="item-final",
        final_message=RAW,
    )
    service = _service(tmp_path, store, turn)
    service.channel.ids = ("om_direct",)  # type: ignore[attr-defined]
    service.codec = CorrelationCodec(b"k" * 32)
    try:
        service._deliver_event_report(
            event,
            ProgressReport(
                "完成",
                "直接完成摘要。",
                NotificationReason.CONVERSATION_COMPLETE,
            ),
        )
        context = store.notification_raw_context_for_message("om_direct")
        assert context is not None
        assert (context.thread_id, context.turn_id) == (
            event.thread_id,
            event.turn_id,
        )
    finally:
        store.close()


def test_silent_or_noncompletion_report_never_creates_raw_context(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    event = TurnEvent("thread-raw", "turn-raw", "completed", final_message=RAW)
    turn = TurnRecord(
        event.thread_id,
        event.turn_id,
        ThreadStatus.COMPLETED,
        final_agent_item_id="item-final",
        final_message=RAW,
    )
    service = _service(tmp_path, store, turn)
    try:
        service._deliver_event_report(
            event,
            ProgressReport("*/*", "仍在继续。", NotificationReason.SILENT),
        )
        assert store.notification_raw_binding_prepared(event.dedupe_key) is False
        assert service.channel.sent == []  # type: ignore[attr-defined]
    finally:
        store.close()


@pytest.mark.parametrize(
    ("turn", "expected"),
    [
        (None, "已被清理"),
        (
            TurnRecord(
                "thread-raw",
                "turn-raw",
                ThreadStatus.IN_PROGRESS,
                final_message=RAW,
            ),
            "已完成",
        ),
        (
            TurnRecord(
                "thread-raw",
                "turn-raw",
                ThreadStatus.COMPLETED,
                final_agent_item_id="changed",
                final_message="已变化",
            ),
            "校验值",
        ),
    ],
)
def test_service_never_guesses_another_turn_when_raw_is_unavailable(
    tmp_path: Path,
    turn: TurnRecord | None,
    expected: str,
) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    _event, context = _raw_context(store)
    request = store.reserve_notification_raw_delivery(
        context, inbound_message_id="om_in", fingerprint="h" * 64
    )
    service = _service(tmp_path, store, turn)
    try:
        claimed = store.claim_notification_raw_delivery()
        assert claimed is not None and claimed.delivery_id == request.delivery_id
        service._process_notification_raw_delivery(claimed)
        assert expected in service.channel.sent[0][0]  # type: ignore[attr-defined]
        assert RAW not in service.channel.sent[0][0]  # type: ignore[attr-defined]
    finally:
        store.close()


def test_raw_directive_rejects_unquoted_attachment_and_wrong_scope(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    _raw_context(store)
    turn = TurnRecord(
        "thread-raw",
        "turn-raw",
        ThreadStatus.COMPLETED,
        final_agent_item_id="item",
        final_message=RAW,
    )
    service = _service(tmp_path, store, turn)
    messages = (
        ChannelReply(sender_id=OWNER, chat_id=CHAT, message_id="in-1", content=".原文"),
        ChannelReply(
            sender_id=OWNER,
            chat_id=CHAT,
            message_id="in-2",
            reply_to_message_id="om_summary",
            content=".原文",
            attachments=(
                ChannelAttachment("D:/safe.png", "image/png", "0" * 64, 1),
            ),
        ),
        ChannelReply(
            sender_id=OWNER,
            chat_id="oc_other",
            message_id="in-3",
            reply_to_message_id="om_summary",
            content=".原文",
        ),
    )
    try:
        for item in messages:
            assert service._process_channel_reply(item) is True
        assert store.claim_notification_raw_delivery() is None
        receipts = tuple(service.receipt_queue.get_nowait() for _ in messages)
        assert "引用" in receipts[0].details
        assert "单独发送" in receipts[1].details
        assert "不属于当前私聊" in receipts[2].details
    finally:
        store.close()


def test_management_query_raw_keeps_priority_over_notification_raw(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    _raw_context(store)
    service = _service(tmp_path, store, None)

    class Management:
        def accepts(self, _message: ChannelReply) -> bool:
            return True

    service.management = Management()  # type: ignore[assignment]
    message = ChannelReply(
        sender_id=OWNER,
        chat_id=CHAT,
        message_id="om_query_raw",
        reply_to_message_id="om_summary",
        content=".原文",
    )
    try:
        service._on_channel_reply(message)
        assert service.management_queue.get_nowait() == message
        assert store.claim_notification_raw_delivery() is None
    finally:
        store.close()


def test_raw_delivery_restart_and_safe_offline_retry(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    store = StateStore(database)
    _event, context = _raw_context(store)
    delivery = store.reserve_notification_raw_delivery(
        context, inbound_message_id="om_restart", fingerprint="i" * 64
    )
    claimed = store.claim_notification_raw_delivery()
    assert claimed is not None
    store.close()

    reopened = StateStore(database)
    try:
        recovery = reopened.recover_interrupted_notification_raw_deliveries()
        assert recovery == {"unsubmitted_released": 1, "submitted_uncertain": 0}
        claimed = reopened.claim_notification_raw_delivery()
        assert claimed is not None and claimed.delivery_id == delivery.delivery_id
        service = _service(
            tmp_path,
            reopened,
            TurnRecord(
                "thread-raw",
                "turn-raw",
                ThreadStatus.COMPLETED,
                final_agent_item_id="item",
                final_message=RAW,
            ),
        )

        def offline(_text: str, *, idempotency_key: str):
            del idempotency_key
            raise MessageChannelOfflineError("offline")

        service.channel.send_text = offline  # type: ignore[method-assign]
        service._process_notification_raw_delivery(claimed)
        saved = reopened.notification_raw_delivery(delivery.delivery_id)
        assert saved is not None and saved.state == "rejected"
        assert saved.submitted_at is None
    finally:
        reopened.close()


def test_restart_after_submit_freezes_raw_delivery_without_replay(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite"
    store = StateStore(database)
    _event, context = _raw_context(store)
    delivery = store.reserve_notification_raw_delivery(
        context, inbound_message_id="om_unknown", fingerprint="u" * 64
    )
    claimed = store.claim_notification_raw_delivery()
    assert claimed is not None
    assert store.prepare_notification_raw_delivery(delivery.delivery_id, "raw")
    assert store.mark_notification_raw_submitted(delivery.delivery_id)
    store.close()

    reopened = StateStore(database)
    try:
        recovery = reopened.recover_interrupted_notification_raw_deliveries()
        assert recovery == {"unsubmitted_released": 0, "submitted_uncertain": 1}
        saved = reopened.notification_raw_delivery(delivery.delivery_id)
        assert saved is not None and saved.state == "uncertain"
        assert reopened.claim_notification_raw_delivery() is None
    finally:
        reopened.close()


def test_non_owner_cannot_probe_raw_context(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    _raw_context(store)
    service = _service(tmp_path, store, None)
    try:
        accepted = service._process_channel_reply(
            ChannelReply(
                sender_id="ou_other",
                chat_id=CHAT,
                message_id="om_other_user",
                reply_to_message_id="om_summary",
                content=".原文",
            )
        )
        assert accepted is False
        assert store.claim_notification_raw_delivery() is None
        assert service.receipt_queue.empty()
    finally:
        store.close()


def test_feishu_send_result_scope_covers_all_chunks(tmp_path: Path) -> None:
    channel = FeishuMessageChannel(
        app_id="cli_test",
        app_secret="secret",
        target_open_id=OWNER,
        sdk_factory=lambda *_args: None,
        media_cache_dir=tmp_path,
    )
    result = SimpleNamespace(
        raw={"code": 0, "data": {"chat_id": CHAT}},
    )
    channel._remember_send_result_scope(
        result, ("om_chunk_1", "om_chunk_2", "om_chunk_3")
    )
    assert channel.recipient_scope_for_messages(
        ("om_chunk_1", "om_chunk_2", "om_chunk_3")
    ) == (OWNER, CHAT)
    assert channel.recipient_scope_for_messages(("om_missing",)) is None


def _unknown_summary(
    service: ProgressService,
    store: StateStore,
    event: TurnEvent,
    *,
    message_text: str,
    raw_binding: bool = False,
) -> tuple[str, int, int]:
    """Create a submitted-but-unknown summary without calling a channel."""

    assert service.codec is not None
    code = service.codec.issue()
    now = int(time.time())
    kwargs: dict[str, object] = {"needs_summary": True}
    if raw_binding:
        kwargs.update(raw_sender_id=OWNER, raw_content_sha256=DIGEST)
    store.reserve_notification(
        event,
        code,
        "占位消息",
        72,
        **kwargs,  # type: ignore[arg-type]
    )
    store.mark_sent(event.dedupe_key)
    claimed = store.claim_notification_summary(event.dedupe_key, now=now + 2)
    assert claimed is not None
    assert store.prepare_notification_summary(
        event.dedupe_key, message_text, now=now + 3
    )
    assert store.mark_notification_summary_submitted(
        event.dedupe_key, now=now + 4
    )
    assert store.mark_notification_summary_uncertain(
        event.dedupe_key, "result_unknown", now=now + 5
    )
    return code, now + 4, now + 5


def _parent_post(
    message_id: str,
    text: str,
    *,
    created_at: int,
    sender_id: str = "cli_bot",
    sender_type: str = "app",
    chat_id: str = CHAT,
    code: int = 0,
) -> dict[str, object]:
    rows = [
        [{"tag": "text", "text": line if line else "\u00a0"}]
        for line in text.split("\n")
    ]
    return {
        "code": code,
        "data": {
            "items": [
                {
                    "message_id": message_id,
                    "msg_type": "post",
                    "content": json.dumps(
                        {
                            "post": {
                                "zh_cn": {
                                    "title": "",
                                    "content": rows,
                                }
                            }
                        },
                        ensure_ascii=False,
                    ),
                    "sender": {
                        "sender_id": {"app_id": sender_id},
                        "sender_type": sender_type,
                    },
                    "chat_id": chat_id,
                    "create_time": str(created_at * 1000),
                }
            ]
        },
    }


def _attach_recovery_channel(
    service: ProgressService,
    raw: object,
) -> list[str]:
    calls: list[str] = []
    channel = service.channel
    assert channel is not None

    def fetch(message_id: str) -> object:
        calls.append(message_id)
        if isinstance(raw, BaseException):
            raise raw
        return raw

    channel.fetch_message = fetch  # type: ignore[attr-defined]
    channel.bot_sender_ids = lambda: ("cli_bot",)  # type: ignore[attr-defined]
    return calls


def test_unknown_post_without_visible_code_recovers_once_and_clears_uncertain(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    service = _service(tmp_path, store, None)
    event = TurnEvent("thread-recovery", "turn-recovery", "completed")
    body = "消息状态：完成\n\n摘要正文"
    code, submitted, _uncertain = _unknown_summary(
        service, store, event, message_text=body
    )
    parent_id = "om_unknown_parent"
    calls = _attach_recovery_channel(
        service,
        _parent_post(parent_id, body, created_at=submitted),
    )
    message = ChannelReply(
        sender_id=OWNER,
        chat_id=CHAT,
        message_id="in-recover-1",
        reply_to_message_id=parent_id,
        content="继续",
    )
    try:
        result = service._recover_unknown_parent(message)
        assert result.code == code
        assert calls == [parent_id]
        delivery = store.notification_summary_delivery(event.dedupe_key)
        assert delivery is not None
        assert delivery.state == "delivered"
        assert delivery.uncertain_at is None
        assert delivery.delivered_at is not None
        assert delivery.channel_message_ids == (parent_id,)
        assert store.code_for_channel_message(parent_id) == code
        # A repeated quote cannot re-fetch or re-deliver this summary.
        repeated = service._recover_unknown_parent(message)
        assert repeated.code == ""
        assert repeated.detail == ""
        assert calls == [parent_id]
    finally:
        store.close()


def test_unknown_recovery_finalizes_prepared_raw_binding_atomically(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    service = _service(tmp_path, store, None)
    event = TurnEvent("thread-raw-recover", "turn-raw-recover", "completed")
    body = "消息状态：完成\n\n带原文摘要"
    code, submitted, _uncertain = _unknown_summary(
        service,
        store,
        event,
        message_text=body,
        raw_binding=True,
    )
    parent_id = "om_raw_recovered"
    _attach_recovery_channel(
        service,
        _parent_post(parent_id, body, created_at=submitted),
    )
    try:
        recovered = service._recover_unknown_parent(
            ChannelReply(
                sender_id=OWNER,
                chat_id=CHAT,
                message_id="in-raw-recover",
                reply_to_message_id=parent_id,
                content="继续",
            )
        )
        assert recovered.code == code
        context = store.notification_raw_context_for_message(parent_id)
        assert context is not None
        assert context.sender_id == OWNER
        assert context.chat_id == CHAT
        delivery = store.notification_summary_delivery(event.dedupe_key)
        assert delivery is not None and delivery.state == "delivered"
        assert delivery.uncertain_at is None
    finally:
        store.close()


@pytest.mark.parametrize(
    "variant",
    ("nonbot", "crosschat", "outside_window", "tampered", "api_error"),
)
def test_unknown_recovery_rejects_untrusted_or_unproven_parent(
    tmp_path: Path, variant: str
) -> None:
    store = StateStore(tmp_path / f"{variant}.sqlite")
    service = _service(tmp_path, store, None)
    event = TurnEvent(f"thread-{variant}", f"turn-{variant}", "completed")
    body = "消息状态：完成\n\n不可伪造摘要"
    _code, submitted, _uncertain = _unknown_summary(
        service, store, event, message_text=body
    )
    parent_id = f"om_{variant}"
    if variant == "api_error":
        raw: object = RuntimeError("transport")
    else:
        raw = _parent_post(
            parent_id,
            body if variant != "tampered" else body + "x",
            created_at=(submitted - 100 if variant == "outside_window" else submitted),
            sender_type=("user" if variant == "nonbot" else "app"),
            chat_id=("oc_other" if variant == "crosschat" else CHAT),
        )
    _attach_recovery_channel(service, raw)
    try:
        result = service._recover_unknown_parent(
            ChannelReply(
                sender_id=OWNER,
                chat_id=CHAT,
                message_id=f"in-{variant}",
                reply_to_message_id=parent_id,
                content="继续",
            )
        )
        assert result.code == ""
        assert result.detail
        delivery = store.notification_summary_delivery(event.dedupe_key)
        assert delivery is not None
        assert delivery.state == "uncertain"
        assert delivery.uncertain_at is not None
        assert store.code_for_channel_message(parent_id) is None
    finally:
        store.close()


def test_unknown_recovery_rejects_error_and_multi_item_envelopes() -> None:
    single = _parent_post("om_extract", "正文", created_at=10)
    assert _extract_feishu_parent_message(
        {**single, "code": 123}, "om_extract"
    ) is None
    item = single["data"]["items"][0]  # type: ignore[index]
    multi = {"code": 0, "data": {"items": [item, item]}}
    assert _extract_feishu_parent_message(multi, "om_extract") is None


def test_unknown_recovery_collision_rolls_back_alias_and_state(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    service = _service(tmp_path, store, None)
    event = TurnEvent("thread-collision", "turn-collision", "completed")
    body = "消息状态：完成\n\n冲突摘要"
    _code, submitted, _uncertain = _unknown_summary(
        service, store, event, message_text=body
    )
    parent_id = "om_collision"
    other = TurnEvent("thread-other", "turn-other", "completed")
    assert service.codec is not None
    other_code = service.codec.issue()
    store.reserve_notification(other, other_code, "其它", 72)
    store.bind_channel_messages(other.dedupe_key, (parent_id,))
    store.mark_sent(other.dedupe_key)
    _attach_recovery_channel(
        service,
        _parent_post(parent_id, body, created_at=submitted),
    )
    try:
        result = service._recover_unknown_parent(
            ChannelReply(
                sender_id=OWNER,
                chat_id=CHAT,
                message_id="in-collision",
                reply_to_message_id=parent_id,
                content="继续",
            )
        )
        assert result.code == ""
        delivery = store.notification_summary_delivery(event.dedupe_key)
        assert delivery is not None and delivery.state == "uncertain"
        assert delivery.channel_message_ids == ()
        assert store.code_for_channel_message(parent_id) == other_code
    finally:
        store.close()


def test_unknown_recovery_raw_context_failure_rolls_back_new_alias(
    tmp_path: Path,
) -> None:
    """A raw-context conflict after alias insertion must roll back both writes."""

    store = StateStore(tmp_path / "state.sqlite")
    service = _service(tmp_path, store, None)
    event = TurnEvent("thread-raw-tx", "turn-raw-tx", "completed")
    body = "消息状态：完成\n\n原文事务冲突"
    _code, submitted, _uncertain = _unknown_summary(
        service,
        store,
        event,
        message_text=body,
        raw_binding=True,
    )
    parent_id = "om_raw_tx_conflict"
    other = TurnEvent("thread-raw-other", "turn-raw-other", "completed")
    assert service.codec is not None
    other_code = service.codec.issue()
    store.reserve_notification(other, other_code, "其它摘要", 72)
    store.mark_sent(other.dedupe_key)
    # Synthetic legacy state: the same platform ID is in a raw context but not
    # yet in notification_message_ids.  Recovery inserts its new alias first;
    # raw finalization then detects the conflicting immutable identity.
    with store._lock, store._connection:  # noqa: SLF001 - transaction fixture
        store._connection.execute(  # noqa: SLF001 - transaction fixture
            """
            INSERT INTO notification_raw_contexts(
                message_id, event_key, sender_id, chat_id, thread_id, turn_id,
                content_sha256, created_at
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                parent_id,
                other.dedupe_key,
                OWNER,
                "oc_other",
                other.thread_id,
                other.turn_id,
                DIGEST,
                submitted,
            ),
        )
    _attach_recovery_channel(
        service,
        _parent_post(parent_id, body, created_at=submitted),
    )
    try:
        result = service._recover_unknown_parent(
            ChannelReply(
                sender_id=OWNER,
                chat_id=CHAT,
                message_id="in-raw-tx",
                reply_to_message_id=parent_id,
                content="继续",
            )
        )
        assert result.code == ""
        delivery = store.notification_summary_delivery(event.dedupe_key)
        assert delivery is not None and delivery.state == "uncertain"
        assert delivery.channel_message_ids == ()
        assert store.code_for_channel_message(parent_id) == other_code
        preserved = store.notification_raw_context_for_message(parent_id)
        assert preserved is not None and preserved.event_key == other.dedupe_key
        alias_count = store._connection.execute(  # noqa: SLF001 - assertion
            "SELECT COUNT(*) FROM notification_message_ids WHERE event_key=?",
            (event.dedupe_key,),
        ).fetchone()
        assert alias_count is not None and int(alias_count[0]) == 0
    finally:
        store.close()


def test_unknown_parent_callback_queues_before_image_staging(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    service = _service(tmp_path, store, None)
    event = TurnEvent("thread-image-recovery", "turn-image-recovery", "completed")
    body = "消息状态：完成\n\n图片前置恢复"
    _unknown_summary(service, store, event, message_text=body)
    parent_id = "om_image_unknown"
    _attach_recovery_channel(
        service,
        _parent_post(parent_id, body, created_at=int(time.time())),
    )
    service.parent_recovery_thread = SimpleNamespace(is_alive=lambda: True)  # type: ignore[assignment]

    def must_not_stage(_message: ChannelReply):
        raise AssertionError("回调线程不应在父恢复前进入图片暂存")

    service._prepare_staged_image_reply = must_not_stage  # type: ignore[method-assign]
    image = ChannelReply(
        sender_id=OWNER,
        chat_id=CHAT,
        message_id="in-image-unknown",
        reply_to_message_id=parent_id,
        content="",
        attachments=(ChannelAttachment("C:/safe.png", "image/png", "0" * 64, 1),),
    )
    try:
        service._on_channel_reply(image)
        queued, used = service.parent_recovery_queue.get_nowait()
        assert queued == image
        assert used is False
        assert service._fatal is None
    finally:
        store.close()


def test_unknown_parent_callback_fails_closed_when_recovery_worker_is_unavailable(
    tmp_path: Path,
) -> None:
    """SDK 回调不得在恢复 worker 缺失时同步等待同一个事件循环。"""

    store = StateStore(tmp_path / "state-worker-unavailable.sqlite")
    service = _service(tmp_path, store, None)
    event = TurnEvent("thread-worker-unavailable", "turn-worker-unavailable", "completed")
    body = "消息状态：完成\n\n等待恢复 worker"
    _unknown_summary(service, store, event, message_text=body)
    parent_id = "om_worker_unavailable"
    calls = _attach_recovery_channel(
        service,
        _parent_post(parent_id, body, created_at=int(time.time())),
    )
    receipts: list[tuple[bool, str]] = []
    service.parent_recovery_thread = None
    service._queue_reply_receipt = (  # type: ignore[method-assign]
        lambda *, received, details, fingerprint: receipts.append(
            (bool(received), str(details))
        )
    )

    def must_not_stage(_message: ChannelReply):
        raise AssertionError("恢复 worker 缺失时不得继续图片暂存或同步官方取消息")

    service._prepare_staged_image_reply = must_not_stage  # type: ignore[method-assign]
    try:
        service._on_channel_reply(
            ChannelReply(
                sender_id=OWNER,
                chat_id=CHAT,
                message_id="in-worker-unavailable",
                reply_to_message_id=parent_id,
                content="继续",
            )
        )
        assert calls == []
        assert receipts and receipts[0][0] is False
        assert "稍后重试" in receipts[0][1]
        assert service._fatal is None
        delivery = store.notification_summary_delivery(event.dedupe_key)
        assert delivery is not None and delivery.state == "uncertain"
    finally:
        store.close()


def test_unknown_parent_worker_recovers_then_stages_image(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    service = _service(tmp_path, store, None)
    event = TurnEvent("thread-image-worker", "turn-image-worker", "completed")
    body = "消息状态：完成\n\n图片 worker 恢复"
    _code, submitted, _uncertain = _unknown_summary(
        service, store, event, message_text=body
    )
    parent_id = "om_image_worker"
    _attach_recovery_channel(
        service,
        _parent_post(parent_id, body, created_at=submitted),
    )
    image = ChannelReply(
        sender_id=OWNER,
        chat_id=CHAT,
        message_id="in-image-worker",
        reply_to_message_id=parent_id,
        content="",
        attachments=(ChannelAttachment("C:/safe.png", "image/png", "0" * 64, 1),),
    )
    worker = threading.Thread(target=service._parent_recovery_worker)
    worker.start()
    service.parent_recovery_queue.put((image, False))
    service.parent_recovery_queue.put(None)
    try:
        worker.join(timeout=5)
        assert not worker.is_alive()
        delivery = store.notification_summary_delivery(event.dedupe_key)
        assert delivery is not None and delivery.state == "delivered"
        staged = store.staged_image_reply(OWNER, CHAT)
        assert staged is not None
        assert staged["reply_to_message_id"] == parent_id
        assert service._fatal is None
    finally:
        store.close()


def test_unknown_parent_worker_recovers_and_routes_same_reply_once(
    tmp_path: Path,
) -> None:
    """官方 post 证明只恢复一次，然后继续路由原入站回复。"""

    store = StateStore(tmp_path / "state-route.sqlite")
    service = _service(tmp_path, store, None)
    event = TurnEvent("thread-route", "turn-route", "completed")
    body = "消息状态：完成\n\n恢复后继续路由"
    code, submitted, _uncertain = _unknown_summary(
        service, store, event, message_text=body
    )
    parent_id = "om_route_parent"
    calls = _attach_recovery_channel(
        service,
        _parent_post(parent_id, body, created_at=submitted),
    )
    message = ChannelReply(
        sender_id=OWNER,
        chat_id=CHAT,
        message_id="in-route-1",
        reply_to_message_id=parent_id,
        content="继续处理",
    )
    worker = threading.Thread(target=service._parent_recovery_worker)
    worker.start()
    service.parent_recovery_queue.put((message, False))
    service.parent_recovery_queue.put(None)
    try:
        worker.join(timeout=5)
        assert not worker.is_alive()
        delivery = store.notification_summary_delivery(event.dedupe_key)
        assert delivery is not None
        assert delivery.state == "delivered"
        assert delivery.uncertain_at is None
        assert delivery.channel_message_ids == (parent_id,)
        assert store.code_for_channel_message(parent_id) == code

        job = service.reply_queue.get_nowait()
        assert job.thread_id == event.thread_id
        assert job.reply_text == "继续处理"
        with pytest.raises(queue.Empty):
            service.reply_queue.get_nowait()
        receipt = service.receipt_queue.get_nowait()
        assert receipt.received is True
        assert "已排队" in receipt.details
        with pytest.raises(queue.Empty):
            service.receipt_queue.get_nowait()
        # The unknown summary is never sent again; only the recovered turn
        # reply is queued for Codex.
        assert service.channel.sent == []  # type: ignore[attr-defined]
        assert calls == [parent_id]

        # Replaying the same inbound event is idempotent: the local alias now
        # routes it without another official read or a second ReplyJob.
        service._on_channel_reply(message)
        assert calls == [parent_id]
        with pytest.raises(queue.Empty):
            service.reply_queue.get_nowait()
        with pytest.raises(queue.Empty):
            service.receipt_queue.get_nowait()
        again = store.notification_summary_delivery(event.dedupe_key)
        assert again is not None and again.channel_message_ids == (parent_id,)
    finally:
        service.stop_event.set()
        store.close()


def test_known_management_context_skips_unknown_parent_fetch(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    service = _service(tmp_path, store, None)
    event = TurnEvent("thread-known-management", "turn-known-management", "completed")
    body = "消息状态：完成\n\n不应触发官方读取"
    _unknown_summary(service, store, event, message_text=body)
    parent_id = "om_unknown_management"
    context_id = store.create_management_context(
        "thread_overview",
        {"thread_id": "thread-known-management"},
        sender_id=OWNER,
        chat_id=CHAT,
    )
    store.bind_management_messages(context_id, (parent_id,))
    calls = _attach_recovery_channel(service, RuntimeError("must-not-fetch"))
    service.management = SimpleNamespace(accepts=lambda _message: False)  # type: ignore[assignment]
    message = ChannelReply(
        sender_id=OWNER,
        chat_id=CHAT,
        message_id="in-management",
        reply_to_message_id=parent_id,
        content="继续",
    )
    try:
        assert store.management_context_exists_for_message(parent_id)
        assert not service._parent_recovery_is_needed(message)
        assert calls == []
    finally:
        store.close()


def test_known_raw_mapping_skips_unknown_parent_fetch(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    service = _service(tmp_path, store, None)
    _raw_context(store, message_ids=("om_known_raw",))
    event = TurnEvent("thread-raw-candidate", "turn-raw-candidate", "completed")
    _unknown_summary(
        service,
        store,
        event,
        message_text="消息状态：完成\n\n不能覆盖已有原文映射",
    )
    calls = _attach_recovery_channel(service, RuntimeError("must-not-fetch"))
    message = ChannelReply(
        sender_id=OWNER,
        chat_id=CHAT,
        message_id="in-known-raw",
        reply_to_message_id="om_known_raw",
        content="继续",
    )
    try:
        assert store.notification_raw_context_exists_for_message("om_known_raw")
        assert not service._parent_recovery_is_needed(message)
        assert calls == []
    finally:
        store.close()
