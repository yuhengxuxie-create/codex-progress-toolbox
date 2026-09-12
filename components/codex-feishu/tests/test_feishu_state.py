"""飞书消息 ID 与一次性 HMAC 通知的持久关联测试。"""

from __future__ import annotations

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from progress_wx.models import GeneratedImageArtifact, TurnEvent
from progress_wx.state import (
    SCHEMA_VERSION,
    CorrelationCodec,
    StateError,
    StateStore,
    enqueue_hook_payload_only,
)


def _codec() -> CorrelationCodec:
    return CorrelationCodec(b"x" * 32)


def test_channel_message_mapping_survives_restart(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    event = TurnEvent("thread", "turn", "completed")
    codec = _codec()
    code = codec.issue()
    store = StateStore(database)
    store.reserve_notification(event, code, "正文", 72)
    store.bind_channel_message(event.dedupe_key, "om_notice")
    store.mark_sent(event.dedupe_key)
    store.close()

    reopened = StateStore(database)
    try:
        assert reopened.code_for_channel_message("om_notice") == code
        assert reopened.peek_reply(code, codec) == ("thread", "turn", "turn")
    finally:
        reopened.close()


def test_media_delivery_and_all_message_aliases_commit_atomically(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    codec = _codec()
    event = TurnEvent("thread-media", "turn-media", "completed")
    other = TurnEvent("thread-other", "turn-other", "completed")
    code = codec.issue()
    try:
        store.reserve_notification(event, code, "正文", 72)
        store.bind_channel_messages(event.dedupe_key, ("om_parent",))
        store.mark_sent(event.dedupe_key)
        artifact = GeneratedImageArtifact(
            item_id="image-1",
            path=str(tmp_path / "image.png"),
            mime_type="image/png",
            sha256="a" * 64,
            size=1,
            file_name="image.png",
        )
        (delivery_id,) = store.reserve_notification_media(
            event.dedupe_key,
            (artifact,),
        )
        assert store.claim_notification_media(delivery_id) is not None

        other_code = codec.issue()
        store.reserve_notification(other, other_code, "其它", 72)
        store.bind_channel_messages(other.dedupe_key, ("om_conflict",))
        store.mark_sent(other.dedupe_key)
        with pytest.raises(StateError):
            store.mark_notification_media_delivered_with_message_ids(
                delivery_id,
                ("om_media_new", "om_conflict"),
            )
        media = store._connection.execute(  # noqa: SLF001 - atomicity assertion
            "SELECT delivered_at, channel_message_id FROM notification_media_deliveries "
            "WHERE delivery_id=?",
            (delivery_id,),
        ).fetchone()
        assert tuple(media) == (None, None)
        assert store.code_for_channel_message("om_media_new") is None

        store.mark_notification_media_delivered_with_message_ids(
            delivery_id,
            ("om_media_1", "om_media_2"),
        )
        assert store.code_for_channel_message("om_media_1") == code
        assert store.code_for_channel_message("om_media_2") == code
    finally:
        store.close()


def test_channel_message_id_cannot_be_rebound_or_shared(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    first = TurnEvent("thread", "turn-1", "completed")
    second = TurnEvent("thread", "turn-2", "completed")
    codec = _codec()
    try:
        store.reserve_notification(first, codec.issue(), "一", 72)
        store.reserve_notification(second, codec.issue(), "二", 72)
        store.bind_channel_message(first.dedupe_key, "om_one")
        with pytest.raises(StateError):
            store.bind_channel_message(first.dedupe_key, "om_other")
        with pytest.raises(StateError):
            store.bind_channel_message(second.dedupe_key, "om_one")
    finally:
        store.close()


def test_all_message_chunks_route_to_same_notification_after_restart(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    event = TurnEvent("thread", "long-turn", "completed")
    codec = _codec()
    code = codec.issue()
    store = StateStore(database)
    store.reserve_notification(event, code, "长正文", 72)
    store.bind_channel_messages(
        event.dedupe_key,
        ("om_chunk_1", "om_chunk_2", "om_chunk_3"),
    )
    store.mark_sent(event.dedupe_key)
    store.close()

    reopened = StateStore(database)
    try:
        assert reopened.code_for_channel_message("om_chunk_1") == code
        assert reopened.code_for_channel_message("om_chunk_2") == code
        assert reopened.code_for_channel_message("om_chunk_3") == code
    finally:
        reopened.close()


def test_message_chunk_cannot_be_shared_between_notifications(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    first = TurnEvent("thread", "long-1", "completed")
    second = TurnEvent("thread", "long-2", "completed")
    codec = _codec()
    try:
        store.reserve_notification(first, codec.issue(), "一", 72)
        store.reserve_notification(second, codec.issue(), "二", 72)
        store.bind_channel_messages(first.dedupe_key, ("om_first", "om_shared"))
        with pytest.raises(StateError, match="另一条通知"):
            store.bind_channel_messages(second.dedupe_key, ("om_second", "om_shared"))
    finally:
        store.close()


def test_management_context_survives_restart_and_all_chunks_resolve(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    store = StateStore(database)
    context_id = store.create_management_context(
        "project_list", {"projects": [{"label": "A01", "project_id": "p1"}]}
    )
    store.bind_management_messages(context_id, ("om_page_1", "om_page_2"))
    store.close()

    reopened = StateStore(database)
    try:
        expected = ("project_list", {"projects": [{"label": "A01", "project_id": "p1"}]})
        assert reopened.management_context_for_message("om_page_1") == expected
        assert reopened.management_context_for_message("om_page_2") == expected
        assert reopened.management_context_for_message("om_page_1", now=4_000_000_000) == expected
        reopened.prune(now=4_000_000_000)
        assert reopened.management_context_for_message("om_page_2", now=4_000_000_000) == expected
    finally:
        reopened.close()


def test_owner_card_context_binds_first_private_chat_atomically(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    try:
        context_id = store.create_management_context(
            "session_query_menu",
            {"_card_source": "owner_open_id_direct"},
            sender_id="ou_owner",
            chat_id="",
        )
        store.bind_management_messages(context_id, ("om_card",))
        assert store.bind_management_context_chat(
            context_id, "ou_other", "oc_private"
        ) is False
        assert store.bind_management_context_chat(
            context_id, "ou_owner", "oc_private"
        ) is True
        assert store.bind_management_context_chat(
            context_id, "ou_owner", "oc_other"
        ) is False
        record = store.management_context_record_for_message("om_card")
        assert record is not None
        assert record.sender_id == "ou_owner"
        assert record.chat_id == "oc_private"

        ownerless = store.create_management_context("session_query_menu", {})
        assert store.bind_management_context_chat(
            ownerless, "ou_owner", "oc_private"
        ) is False
    finally:
        store.close()


def test_management_inbound_message_is_reserved_once(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    store = StateStore(database)
    try:
        assert store.reserve_management_inbound("om_in", "ou_owner", "新建个人会话") is True
        assert store.reserve_management_inbound("om_in", "ou_owner", "被重投的不同正文") is False
        store.complete_management_inbound("om_in")
    finally:
        store.close()
    reopened = StateStore(database)
    try:
        assert reopened.reserve_management_inbound(
            "om_in", "ou_owner", "重启后的重复卡片事件"
        ) is False
    finally:
        reopened.close()


def test_schema15_management_state_migrates_owner_submitted_and_actions(
    tmp_path: Path,
) -> None:
    """旧管理上下文迁移后具备 owner、提交边界和动作表。"""

    database = tmp_path / "schema15.sqlite"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta(key, value) VALUES('schema_version', '15');
        CREATE TABLE management_contexts (
            context_id TEXT PRIMARY KEY,
            context_kind TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL
        );
        CREATE TABLE management_message_ids (
            message_id TEXT PRIMARY KEY,
            context_id TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            FOREIGN KEY(context_id) REFERENCES management_contexts(context_id)
                ON DELETE CASCADE
        );
        CREATE TABLE management_inbound_messages (
            message_id TEXT PRIMARY KEY,
            sender_id TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            completed_at INTEGER
        );
        """
    )
    connection.commit()
    connection.close()

    store = StateStore(database)
    try:
        assert store._connection.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0] == str(SCHEMA_VERSION)
        context_columns = {
            row[1]
            for row in store._connection.execute(
                "PRAGMA table_info(management_contexts)"
            )
        }
        action_columns = {
            row[1]
            for row in store._connection.execute(
                "PRAGMA table_info(management_context_actions)"
            )
        }
        assert {"sender_id", "chat_id"} <= context_columns
        assert {
            "context_id",
            "action",
            "claimed_at",
            "submitted_at",
            "succeeded_at",
            "rejected_at",
            "uncertain_at",
        } <= action_columns

        context_id = store.create_management_context(
            "thread_overview",
            {"thread": {"id": "thread-1"}},
            sender_id="ou_owner",
            chat_id="oc_private",
        )
        store.bind_management_messages(context_id, ("om_schema16",))
        record = store.management_context_record_for_message("om_schema16")
        assert record is not None
        assert record.context_id == context_id
        assert record.sender_id == "ou_owner"
        assert record.chat_id == "oc_private"
    finally:
        store.close()


def test_schema16_migrates_summary_outbox_table_without_touching_notifications(
    tmp_path: Path,
) -> None:
    """schema16 旧库新增摘要表，既有父通知保持原样。"""

    database = tmp_path / "schema16-summary.sqlite"
    store = StateStore(database)
    event = TurnEvent("summary-thread", "summary-turn", "completed")
    code = _codec().issue()
    store.reserve_notification(event, code, "旧版父通知", 72)
    with store._lock, store._connection:
        store._connection.execute("DROP TABLE notification_summary_deliveries")
        store._connection.execute(
            "UPDATE meta SET value='16' WHERE key='schema_version'"
        )
    store.close()

    migrated = StateStore(database)
    try:
        assert migrated._connection.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0] == str(SCHEMA_VERSION)
        columns = {
            row[1]
            for row in migrated._connection.execute(
                "PRAGMA table_info(notification_summary_deliveries)"
            )
        }
        assert {
            "event_key", "created_at", "next_attempt_at", "attempt_count",
            "claimed_at", "message_text", "prepared_at", "submitted_at",
            "delivered_at", "rejected_at", "uncertain_at",
            "channel_message_ids_json", "last_error",
        } <= columns
        parent = migrated._connection.execute(
            "SELECT code, message_text FROM notifications WHERE event_key=?",
            (event.dedupe_key,),
        ).fetchone()
        assert parent is not None
        assert (parent["code"], parent["message_text"]) == (code, "旧版父通知")
        assert migrated.stats()["notification_summary_deliveries"] == 0
    finally:
        migrated.close()


def test_hook_enqueue_only_accepts_supported_versions_and_rejects_future_schema(
    tmp_path: Path,
) -> None:
    database = tmp_path / "hook-schema-compat.sqlite"
    StateStore(database).close()
    for version in range(15, SCHEMA_VERSION + 1):
        connection = sqlite3.connect(database)
        connection.execute(
            "UPDATE meta SET value=? WHERE key='schema_version'", (str(version),)
        )
        connection.commit()
        connection.close()
        assert enqueue_hook_payload_only(
            database,
            {
                "type": "agent-turn-complete",
                "thread-id": "hook-thread",
                "turn-id": f"turn-{version}",
            },
            now=version,
        ) is True

    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE meta SET value=? WHERE key='schema_version'",
        (str(SCHEMA_VERSION + 1),),
    )
    connection.commit()
    connection.close()
    with pytest.raises(StateError, match="高于本程序支持"):
        enqueue_hook_payload_only(
            database,
            {
                "type": "agent-turn-complete",
                "thread-id": "hook-thread",
                "turn-id": "turn-future",
            },
            now=SCHEMA_VERSION + 1,
        )


def test_summary_reserve_is_atomic_and_claim_requires_sent_parent(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "summary-atomic.sqlite")
    event = TurnEvent("summary-thread", "summary-turn", "completed")
    codec = _codec()
    code = codec.issue()
    try:
        first_code, first_text = store.reserve_notification(
            event,
            code,
            "这轮回复已结束，详细摘要整理中",
            72,
            needs_summary=True,
        )
        assert (first_code, first_text) == (
            code,
            "这轮回复已结束，详细摘要整理中",
        )
        reserved = store.notification_summary_delivery(event.dedupe_key)
        assert reserved is not None
        assert reserved.code == code
        assert reserved.thread_id == event.thread_id
        assert reserved.turn_id == event.turn_id
        assert reserved.message_text == ""
        # 父 placeholder 还未确认送达，摘要不可抢占。
        assert store.claim_notification_summary(event.dedupe_key) is None

        store.mark_sent(event.dedupe_key)
        claimed = store.claim_notification_summary(
            event.dedupe_key, now=int(time.time()) + 1
        )
        assert claimed is not None
        assert claimed.state == "claimed"
        assert claimed.attempt_count == 1
        assert store.prepare_notification_summary(
            event.dedupe_key, "已完成本轮工作。", now=101
        ) is True
        assert store.prepare_notification_summary(
            event.dedupe_key, "已完成本轮工作。", now=102
        ) is True
        with pytest.raises(StateError, match="不同正文"):
            store.prepare_notification_summary(
                event.dedupe_key, "另一段摘要", now=103
            )
        assert store.mark_notification_summary_submitted(
            event.dedupe_key, now=104
        ) is True
        # 已跨提交边界，普通 release 不得把它伪装成可重试。
        assert store.release_notification_summary(
            event.dedupe_key, "unsafe-release", now=105
        ) is False
        assert store.mark_notification_summary_delivered(
            event.dedupe_key, ("om_summary_1",), now=106
        ) is True
        assert store.notification_summary_delivered(event.dedupe_key) is True
        assert store.notification_summary_terminal(event.dedupe_key) is True
        delivered = store.notification_summary_delivery(event.dedupe_key)
        assert delivered is not None
        assert delivered.state == "delivered"
        assert delivered.channel_message_ids == ("om_summary_1",)
        # 摘要送达与引用消息绑定必须在同一事务完成；服务即使在
        # mark_notification_summary_delivered 返回后崩溃，用户也能引用摘要续聊。
        assert store.code_for_channel_message("om_summary_1") == code
        # 摘要正文不进入计数型 stats。
        assert "已完成本轮工作" not in str(store.stats())
    finally:
        store.close()


def test_summary_only_reserve_has_no_visible_parent_and_silent_is_atomic(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "summary-only.sqlite")
    event = TurnEvent("summary-only-thread", "summary-only-turn", "completed")
    code = _codec().issue()
    try:
        assert store.reserve_notification_summary_only(event, code, 72) == (code, "")
        assert store.pending_notification_texts() == ()
        claimed = store.claim_notification_summary(
            event.dedupe_key, now=int(time.time()) + 1
        )
        assert claimed is not None
        assert claimed.message_text == ""
        assert store.discard_notification_summary(
            event.dedupe_key, "policy:silent", now=102
        ) is True
        assert store.notification_summary_delivery(event.dedupe_key) is None
        assert store.was_processed(event.dedupe_key) is True
        parent = store._connection.execute(
            "SELECT message_text, sent_at, discarded_at FROM notifications WHERE event_key=?",
            (event.dedupe_key,),
        ).fetchone()
        assert parent is not None
        assert parent["message_text"] == ""
        assert parent["sent_at"] is not None
        assert parent["discarded_at"] == 102
        assert store.discard_notification_summary(
            event.dedupe_key, "policy:silent", now=103
        ) is True
    finally:
        store.close()


def test_summary_delivery_message_binding_collision_rolls_back_atomically(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "summary-binding-collision.sqlite")
    codec = _codec()
    summary_event = TurnEvent("summary-thread", "summary-turn", "completed")
    other_event = TurnEvent("other-thread", "other-turn", "completed")
    summary_code = codec.issue()
    other_code = codec.issue()
    try:
        store.reserve_notification_with_summary(
            summary_event, summary_code, "placeholder", 72
        )
        store.mark_sent(summary_event.dedupe_key)
        assert store.claim_notification_summary(summary_event.dedupe_key) is not None
        assert store.prepare_notification_summary(
            summary_event.dedupe_key, "详细摘要", now=101
        )
        assert store.mark_notification_summary_submitted(
            summary_event.dedupe_key, now=102
        )

        store.reserve_notification(other_event, other_code, "other", 72)
        store.mark_sent(other_event.dedupe_key)
        store.bind_channel_message(other_event.dedupe_key, "om_shared")

        with pytest.raises(StateError, match="已绑定到另一条通知"):
            store.mark_notification_summary_delivered(
                summary_event.dedupe_key, ("om_shared",), now=103
            )

        delivery = store.notification_summary_delivery(summary_event.dedupe_key)
        assert delivery is not None
        assert delivery.state == "submitted"
        assert delivery.delivered_at is None
        assert store.code_for_channel_message("om_shared") == other_code
        assert store.code_for_channel_message("om_missing") is None
    finally:
        store.close()


def test_summary_reserve_existing_parent_only_fills_missing_summary(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "summary-fill.sqlite")
    event = TurnEvent("summary-thread", "summary-turn", "completed")
    codec = _codec()
    code = codec.issue()
    try:
        store.reserve_notification(event, code, "稳定 placeholder", 72)
        # 同一父通知重复 reserve 只补摘要行，不覆盖 code/正文。
        assert store.reserve_notification(
            event,
            "另一个 code",
            "不能覆盖的正文",
            72,
            needs_summary=True,
        ) == (code, "稳定 placeholder")
        assert store.stats()["notification_summary_deliveries"] == 1
        parent = store._connection.execute(
            "SELECT code, message_text FROM notifications WHERE event_key=?",
            (event.dedupe_key,),
        ).fetchone()
        assert (parent["code"], parent["message_text"]) == (
            code,
            "稳定 placeholder",
        )
    finally:
        store.close()


def test_summary_claim_is_single_winner_across_store_instances(
    tmp_path: Path,
) -> None:
    database = tmp_path / "summary-concurrent.sqlite"
    owner = StateStore(database)
    event = TurnEvent("summary-thread", "summary-turn", "completed")
    owner.reserve_notification_with_summary(
        event, _codec().issue(), "placeholder", 72
    )
    owner.mark_sent(event.dedupe_key)
    owner.close()
    stores = [StateStore(database), StateStore(database)]
    try:
        def claim(index: int):
            return stores[index].claim_notification_summary(now=int(time.time()) + 1)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, range(2)))
        assert [result is not None for result in results].count(True) == 1
        assert stores[0].pending_notification_summary_count() == 1
    finally:
        for store in stores:
            store.close()


def test_summary_reject_retry_and_unknown_recovery_are_safe(
    tmp_path: Path,
) -> None:
    database = tmp_path / "summary-recovery.sqlite"
    store = StateStore(database)
    first = TurnEvent("summary-thread", "summary-turn-1", "completed")
    second = TurnEvent("summary-thread", "summary-turn-2", "completed")
    try:
        store.reserve_notification_with_summary(first, _codec().issue(), "p1", 72)
        store.reserve_notification_with_summary(second, _codec().issue(), "p2", 72)
        store.mark_sent(first.dedupe_key)
        store.mark_sent(second.dedupe_key)

        claim = store.claim_notification_summary(
            first.dedupe_key, now=int(time.time()) + 1
        )
        assert claim is not None
        assert store.prepare_notification_summary(first.dedupe_key, "摘要一", now=301)
        assert store.release_notification_summary(
            first.dedupe_key, "explicit_rejected", next_attempt_at=400, now=302
        ) is True
        assert store.claim_notification_summary(first.dedupe_key, now=399) is None
        retry = store.claim_notification_summary(first.dedupe_key, now=400)
        assert retry is not None and retry.attempt_count == 2
        assert store.prepare_notification_summary(first.dedupe_key, "摘要一", now=401)

        second_claim = store.claim_notification_summary(
            second.dedupe_key, now=int(time.time()) + 1
        )
        assert second_claim is not None
        assert store.prepare_notification_summary(second.dedupe_key, "摘要二", now=301)
        assert store.mark_notification_summary_submitted(second.dedupe_key, now=302)
        store.close()

        reopened = StateStore(database)
        try:
            recovered = reopened.recover_interrupted_notification_summaries(now=500)
            assert recovered == {"unsubmitted_released": 1, "submitted_uncertain": 1}
            first_row = reopened.notification_summary_delivery(first.dedupe_key)
            second_row = reopened.notification_summary_delivery(second.dedupe_key)
            assert first_row is not None and first_row.state == "rejected"
            assert second_row is not None and second_row.state == "uncertain"
            assert reopened.claim_notification_summary(first.dedupe_key, now=500) is not None
            assert reopened.claim_notification_summary(second.dedupe_key, now=500) is None
            assert reopened.notification_summary_terminal(second.dedupe_key) is True
        finally:
            reopened.close()
            store = None
    finally:
        if store is not None:
            store.close()


def test_summary_prune_preserves_pending_and_cascades_terminal_rows(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "summary-prune.sqlite")
    pending = TurnEvent("summary-thread", "summary-pending", "completed")
    terminal = TurnEvent("summary-thread", "summary-terminal", "completed")
    try:
        store.reserve_notification_with_summary(pending, _codec().issue(), "p", 72)
        store.reserve_notification_with_summary(terminal, _codec().issue(), "t", 72)
        store.mark_sent(pending.dedupe_key)
        store.mark_sent(terminal.dedupe_key)
        terminal_claim = store.claim_notification_summary(
            terminal.dedupe_key, now=int(time.time()) + 1
        )
        assert terminal_claim is not None
        assert store.prepare_notification_summary(terminal.dedupe_key, "终态摘要", now=101)
        assert store.mark_notification_summary_submitted(terminal.dedupe_key, now=102)
        assert store.mark_notification_summary_uncertain(
            terminal.dedupe_key, "unknown", now=103
        ) is True
        with store._lock, store._connection:
            store._connection.execute(
                "UPDATE notifications SET created_at=1, expires_at=1 WHERE event_key IN (?,?)",
                (pending.dedupe_key, terminal.dedupe_key),
            )
            store._connection.execute(
                "UPDATE notification_summary_deliveries SET created_at=1 WHERE event_key IN (?,?)",
                (pending.dedupe_key, terminal.dedupe_key),
            )
        removed = store.prune(retention_days=30, now=91 * 86_400 + 1)
        assert removed["notifications"] == 1
        assert removed["notification_summary_deliveries"] == 1
        assert store.notification_summary_delivery(pending.dedupe_key) is not None
        assert store.notification_summary_delivery(terminal.dedupe_key) is None
    finally:
        store.close()


def test_management_context_actions_are_independent_and_success_is_not_reentrant(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    try:
        context_id = store.create_management_context(
            "thread_overview",
            {"thread": {"id": "thread-1"}},
            sender_id="ou_owner",
            chat_id="oc_private",
        )

        raw = store.begin_management_context_action(context_id, "raw", "in-raw")
        assert raw.status == "claimed"

        assert store.complete_management_context_action(
            context_id, "raw", message_ids=("om_raw_1", "om_raw_2")
        ) is True
        assert store.begin_management_context_action(
            context_id, "raw", "in-raw-duplicate"
        ).status == "succeeded"
        assert store.management_context_action(context_id, "raw")[
            "result_message_ids_json"
        ] == '["om_raw_1","om_raw_2"]'

        # raw 的成功消费不能影响同一查询的 archive 动作；archive 仍可首次占用。
        archive = store.begin_management_context_action(
            context_id, "archive", "in-archive"
        )
        assert archive.status == "claimed"
        assert store.begin_management_context_action(
            context_id, "archive", "in-archive-duplicate"
        ).status == "busy"
        assert store.complete_management_context_action(context_id, "archive") is True
        assert store.begin_management_context_action(
            context_id, "archive", "in-archive-after-success"
        ).status == "succeeded"
    finally:
        store.close()


def test_raw_success_redacts_every_copy_of_one_query_but_not_another_query(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    snapshot_key = "a" * 64
    other_snapshot_key = "b" * 64

    def create(message_id: str, key: str, raw: str) -> str:
        context_id = store.create_management_context(
            "thread_overview",
            {
                "thread": {"id": "thread-1"},
                "query_snapshot": {
                    "snapshot_key": key,
                    "turn_id": "turn-1",
                    "content_hash": "c" * 64,
                    "raw_sha256": "d" * 64,
                    "raw_final": raw,
                },
            },
            sender_id="ou_owner",
            chat_id="oc_private",
            ttl_days=30,
        )
        store.bind_management_messages(context_id, (message_id,))
        return context_id

    first = create("om_first", snapshot_key, "同一查询原文")
    create("om_same_query_copy", snapshot_key, "同一查询原文")
    create("om_other_query", other_snapshot_key, "另一查询原文")
    try:
        assert store.begin_management_context_action(first, "raw", "in_raw").status == "claimed"
        assert store.mark_management_context_action_submitted(first, "raw") is True
        assert store.complete_management_context_action(
            first,
            "raw",
            message_ids=("om_raw_result",),
            redact_snapshot_key=snapshot_key,
        ) is True

        for message_id in ("om_first", "om_same_query_copy"):
            record = store.management_context_record_for_message(message_id)
            assert record is not None
            snapshot = record.payload["query_snapshot"]
            assert snapshot["raw_final"] == ""
            assert snapshot["raw_released_at"] > 0
            assert snapshot["turn_id"] == "turn-1"

        other = store.management_context_record_for_message("om_other_query")
        assert other is not None
        assert other.payload["query_snapshot"]["raw_final"] == "另一查询原文"
        assert "raw_final" not in str(store.stats())
    finally:
        store.close()


def test_management_context_action_concurrent_begin_has_one_claim(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite"
    owner = StateStore(database)
    context_id = owner.create_management_context(
        "thread_overview",
        {"thread": {"id": "thread-1"}},
        sender_id="ou_owner",
        chat_id="oc_private",
    )
    owner.close()
    stores = [StateStore(database), StateStore(database)]
    try:
        def begin(index: int):
            return stores[index].begin_management_context_action(
                context_id, "archive", f"in-concurrent-{index}"
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(begin, range(2)))
        assert [item.status for item in results].count("claimed") == 1
        assert [item.status for item in results].count("busy") == 1
        row = stores[0].management_context_action(context_id, "archive")
        assert row is not None
        assert row["attempt_count"] == 1
    finally:
        for store in stores:
            store.close()


def test_management_context_action_release_can_retry_but_success_cannot(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    try:
        context_id = store.create_management_context(
            "thread_overview",
            {"thread": {"id": "thread-1"}},
            sender_id="ou_owner",
            chat_id="oc_private",
        )
        assert store.begin_management_context_action(
            context_id, "archive", "in-reject-1"
        ).status == "claimed"
        assert store.release_management_context_action(
            context_id, "archive", "desktop_rejected"
        ) is True
        retry = store.begin_management_context_action(
            context_id, "archive", "in-retry"
        )
        assert retry.status == "claimed"
        assert retry.attempt_count == 2
        assert store.complete_management_context_action(context_id, "archive") is True
        assert store.begin_management_context_action(
            context_id, "archive", "in-after-success"
        ).status == "succeeded"

        submitted_context = store.create_management_context(
            "thread_overview",
            {"thread": {"id": "thread-submitted"}},
            sender_id="ou_owner",
            chat_id="oc_private",
        )
        assert store.begin_management_context_action(
            submitted_context, "archive", "in-submitted"
        ).status == "claimed"
        assert store.mark_management_context_action_submitted(
            submitted_context, "archive"
        ) is True
        # 状态层默认禁止把已越过提交边界的动作降回可重试。
        assert store.release_management_context_action(
            submitted_context, "archive", "unsafe_release"
        ) is False
        submitted = store.management_context_action(submitted_context, "archive")
        assert submitted is not None and submitted["submitted_at"] is not None
        assert submitted["claimed_at"] is not None
        # 只有调用方取得“明确未接受”的结构化证据时才可显式释放。
        assert store.release_management_context_action(
            submitted_context,
            "archive",
            "explicit_not_submitted",
            allow_submitted=True,
        ) is True
        assert store.begin_management_context_action(
            submitted_context, "archive", "in-submitted-retry"
        ).status == "claimed"
    finally:
        store.close()


def test_management_context_action_restart_distinguishes_submitted_unknown(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite"
    store = StateStore(database)
    submitted_context = store.create_management_context(
        "thread_overview",
        {"thread": {"id": "thread-submitted"}},
        sender_id="ou_owner",
        chat_id="oc_private",
    )
    unsubmitted_context = store.create_management_context(
        "thread_overview",
        {"thread": {"id": "thread-unsubmitted"}},
        sender_id="ou_owner",
        chat_id="oc_private",
    )
    assert store.begin_management_context_action(
        submitted_context, "archive", "in-submitted"
    ).status == "claimed"
    assert store.mark_management_context_action_submitted(
        submitted_context, "archive"
    ) is True
    assert store.begin_management_context_action(
        unsubmitted_context, "raw", "in-unsubmitted"
    ).status == "claimed"
    store.close()

    reopened = StateStore(database)
    try:
        result = reopened.recover_interrupted_management_actions(now=2_000)
        assert result == {"unsubmitted_released": 1, "submitted_uncertain": 1}

        submitted = reopened.management_context_action(submitted_context, "archive")
        assert submitted is not None
        assert submitted["submitted_at"] is not None
        assert submitted["uncertain_at"] == 2_000
        assert reopened.begin_management_context_action(
            submitted_context, "archive", "in-submitted-retry"
        ).status == "uncertain"

        unsubmitted = reopened.management_context_action(
            unsubmitted_context, "raw"
        )
        assert unsubmitted is not None
        assert unsubmitted["submitted_at"] is None
        assert unsubmitted["claimed_at"] is None
        assert unsubmitted["rejected_at"] == 2_000
        assert reopened.begin_management_context_action(
            unsubmitted_context, "raw", "in-unsubmitted-retry"
        ).status == "claimed"
    finally:
        reopened.close()


def test_management_context_owner_and_all_chunks_survive_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite"
    store = StateStore(database)
    context_id = store.create_management_context(
        "thread_overview",
        {
            "thread": {"id": "thread-1", "hostId": "local"},
            "raw_snapshot": {
                "turn_id": "turn-1",
                "content_hash": "a" * 64,
                "final_message": "冻结原文",
            },
        },
        sender_id="ou_owner",
        chat_id="oc_private",
    )
    store.bind_management_messages(
        context_id, ("om_overview_1", "om_overview_2", "om_overview_image_1")
    )
    store.close()

    reopened = StateStore(database)
    try:
        for message_id in (
            "om_overview_1",
            "om_overview_2",
            "om_overview_image_1",
        ):
            record = reopened.management_context_record_for_message(message_id)
            assert record is not None
            assert record.context_id == context_id
            assert record.sender_id == "ou_owner"
            assert record.chat_id == "oc_private"
            assert record.payload["raw_snapshot"]["final_message"] == "冻结原文"
    finally:
        reopened.close()


def test_management_message_binding_is_idempotent_same_context_but_not_cross_context(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    try:
        first = store.create_management_context(
            "thread_overview",
            {"thread": {"id": "thread-1"}},
            sender_id="ou_owner",
            chat_id="oc_private",
        )
        second = store.create_management_context(
            "thread_overview",
            {"thread": {"id": "thread-2"}},
            sender_id="ou_owner",
            chat_id="oc_private",
        )
        store.bind_management_messages(first, ("om_same", "om_same"))
        store.bind_management_messages(first, ("om_same",))
        with pytest.raises(StateError, match="不同管理上下文"):
            store.bind_management_messages(second, ("om_same",))
    finally:
        store.close()
