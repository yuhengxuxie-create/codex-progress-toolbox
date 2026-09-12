"""Exact-parent user reply chain tests.

All cases use synthetic SQLite files.  They never open the Feishu SDK, invoke
Codex, or touch the production database.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import queue
import threading
import time

import pytest

from progress_wx.channel import ChannelAttachment, ChannelReply
from progress_wx.guardian import control_root
from progress_wx.guardian_channel import GuardianChannel
from progress_wx.guardian_store import GuardianStore
from progress_wx.service import ProgressService
from progress_wx.state import SCHEMA_VERSION, CorrelationCodec, StateStore
from progress_wx.user_reply_chain import (
    UserReplyChainConflict,
    UserReplyChainExpired,
    UserReplyChainNotQueued,
    UserReplyChainRejected,
    UserReplyChainStore,
    content_hash,
    initialize_user_reply_chain_schema,
)


def _database(path: Path, *, expires_at: int = 10_000) -> Path:
    state = StateStore(path)
    try:
        state._connection.execute(
            """
            INSERT INTO notifications(
                event_key, code, thread_id, turn_id, reply_kind, message_text,
                created_at, expires_at, sent_at, channel_message_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            ("event-1", "code-1", "thread-1", "turn-1", "turn", "notice",
             100, expires_at, 101, "bot-message-1"),
        )
        state._connection.execute(
            """
            INSERT INTO notification_message_ids(message_id, event_key, created_at)
            VALUES(?,?,?)
            """,
            ("bot-message-1", "event-1", 101),
        )
        state._connection.commit()
    finally:
        state.close()
    return path


def _delivery(path: Path, *, message_id: str, fingerprint: str, text: str,
              delivery_id: str, parent_code: str = "code-1") -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            INSERT INTO reply_deliveries(
                delivery_id, parent_code, inbound_message_id,
                reply_fingerprint, reply_text, created_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (delivery_id, parent_code, message_id, fingerprint, text, 110),
        )
        connection.commit()
    finally:
        connection.close()


def _prepare(store: UserReplyChainStore, *, message_id: str, quoted: str,
             sender: str = "user-1", chat: str = "chat-1", text: str = "hello",
             fingerprint: str = "fp-1", code: str = "code-1", now: int = 200):
    return store.prepare_reply(
        inbound_message_id=message_id,
        sender_id=sender,
        chat_id=chat,
        quoted_message_id=quoted,
        parent_code=code,
        reply_fingerprint=fingerprint,
        content_digest=content_hash(text),
        now=now,
    )


def test_schema_is_explicit_and_read_only_open_does_not_create_or_migrate(
    tmp_path: Path,
) -> None:
    assert SCHEMA_VERSION >= 22
    path = tmp_path / "chain.sqlite"
    state = StateStore(path)
    try:
        # StateStore's controlled schema22 migration owns the table now; a
        # read-only chain open must validate it without doing any write.
        readonly = UserReplyChainStore.open_read_only(path)
        readonly.close()
        connection = state._connection
        initialize_user_reply_chain_schema(connection)
        connection.commit()
        row = connection.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        assert row is not None and row[0] == str(SCHEMA_VERSION)
    finally:
        state.close()
    readonly = UserReplyChainStore.open_read_only(path)
    try:
        assert readonly.get("missing") is None
        with pytest.raises(Exception):
            readonly.abort_prepared_reply("missing")
    finally:
        readonly.close()


def test_prepare_complete_and_two_level_chain_survive_restart(tmp_path: Path) -> None:
    path = _database(tmp_path / "chain.sqlite")
    store = UserReplyChainStore(path, initialize=True)
    first = _prepare(store, message_id="user-message-1", quoted="bot-message-1")
    assert first.is_new and first.state == "prepared"
    _delivery(path, message_id="user-message-1", fingerprint="fp-1",
              text="hello", delivery_id="delivery-1")
    queued = store.complete_queued_reply("user-message-1", delivery_id="delivery-1", now=200)
    assert queued.state == "queued" and queued.delivery_sequence == 1
    assert store.resolve_parent(
        "user-message-1", sender_id="user-1", chat_id="chat-1", now=200
    ).thread_id == "thread-1"

    second = _prepare(
        store,
        message_id="user-message-2",
        quoted="user-message-1",
        text="send the image too",
        fingerprint="fp-2",
    )
    assert second.root_message_id == "bot-message-1"
    assert second.chain_sequence == 2
    _delivery(path, message_id="user-message-2", fingerprint="fp-2",
              text="send the image too", delivery_id="delivery-2")
    store.complete_queued_reply("user-message-2", delivery_id="delivery-2", now=201)
    store.close()

    reopened = UserReplyChainStore(path)
    try:
        resolution = reopened.inspect_parent(
            "user-message-2", sender_id="user-1", chat_id="chat-1", now=201
        )
        assert resolution.status == "ready"
        assert resolution.record is not None
        assert resolution.record.turn_id == "turn-1"
        assert reopened.get("user-message-1").delivery_id == "delivery-1"
    finally:
        reopened.close()


def test_real_state_store_enqueue_complete_and_restart_reconcile(tmp_path: Path) -> None:
    """Exercise the link around the production StateStore outbox method."""

    path = tmp_path / "real-outbox.sqlite"
    state = StateStore(path)
    codec = CorrelationCodec(b"c" * 32)
    code = codec.issue()
    try:
        state._connection.execute(
            """
            INSERT INTO notifications(
                event_key, code, thread_id, turn_id, reply_kind, message_text,
                created_at, expires_at, sent_at, channel_message_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            ("event-real", code, "thread-real", "turn-real", "turn", "notice",
             100, 10_000, 101, "bot-real"),
        )
        state._connection.execute(
            "INSERT INTO notification_message_ids(message_id,event_key,created_at) VALUES(?,?,?)",
            ("bot-real", "event-real", 101),
        )
        state._connection.commit()
        chain = UserReplyChainStore(path, initialize=True)
        prepared = chain.prepare_reply(
            inbound_message_id="user-real",
            sender_id="user-1",
            chat_id="chat-1",
            quoted_message_id="bot-real",
            parent_code=code,
            reply_fingerprint="real-fp",
            content_digest=content_hash("real queued text"),
            now=200,
        )
        assert prepared.state == "prepared"
        delivery = state.enqueue_turn_reply(
            code,
            "user-real",
            "real-fp",
            codec,
            reply_text="real queued text",
            now=200,
        )
        assert delivery is not None and delivery.is_new
        # Simulate a crash immediately after StateStore committed the queue.
        chain.close()
        state.close()
    except BaseException:
        state.close()
        raise

    restarted = UserReplyChainStore(path)
    try:
        recovered = restarted.reconcile_prepared(now=201)
        assert len(recovered) == 1
        assert recovered[0].delivery_id == delivery.delivery_id
        route = restarted.resolve_parent(
            "user-real", sender_id="user-1", chat_id="chat-1", now=201
        )
        assert route is not None and route.thread_id == "thread-real"
    finally:
        restarted.close()


def test_prepared_link_is_not_routable_until_queue_proof_and_reconcile_recovers(
    tmp_path: Path,
) -> None:
    path = _database(tmp_path / "reconcile.sqlite")
    store = UserReplyChainStore(path, initialize=True)
    _prepare(store, message_id="user-message-1", quoted="bot-message-1")
    assert store.resolve_parent(
        "user-message-1", sender_id="user-1", chat_id="chat-1", now=200
    ) is None
    assert store.inspect_parent(
        "user-message-1", sender_id="user-1", chat_id="chat-1", now=200
    ).status == "not_queued"

    # Simulate a process crash after StateStore committed its outbox row but
    # before complete_queued_reply ran.
    _delivery(path, message_id="user-message-1", fingerprint="fp-1",
              text="hello", delivery_id="delivery-1")
    recovered = store.reconcile_prepared(now=200)
    assert [item.delivery_id for item in recovered] == ["delivery-1"]
    assert store.resolve_parent(
        "user-message-1", sender_id="user-1", chat_id="chat-1", now=200
    ).state == "queued"
    store.close()


def test_duplicate_is_idempotent_but_identity_parent_and_content_conflicts_fail(
    tmp_path: Path,
) -> None:
    path = _database(tmp_path / "duplicate.sqlite")
    store = UserReplyChainStore(path, initialize=True)
    first = _prepare(store, message_id="same", quoted="bot-message-1")
    duplicate = _prepare(store, message_id="same", quoted="bot-message-1")
    assert duplicate.message_id == first.message_id
    assert duplicate.state == first.state == "prepared"
    assert duplicate.is_new is False and first.is_new is True
    with pytest.raises(UserReplyChainConflict):
        _prepare(store, message_id="same", quoted="bot-message-1", text="changed")
    with pytest.raises(UserReplyChainConflict):
        _prepare(store, message_id="same", quoted="other-parent")
    store.close()


def test_cross_user_chat_unknown_parent_and_expired_parent_fail_closed(
    tmp_path: Path,
) -> None:
    path = _database(tmp_path / "scope.sqlite", expires_at=300)
    store = UserReplyChainStore(path, initialize=True)
    _prepare(store, message_id="user-message-1", quoted="bot-message-1", now=200)
    _delivery(path, message_id="user-message-1", fingerprint="fp-1",
              text="hello", delivery_id="delivery-1")
    store.complete_queued_reply("user-message-1", delivery_id="delivery-1", now=200)
    with pytest.raises(UserReplyChainRejected):
        _prepare(store, message_id="other-user-message", quoted="user-message-1",
                 sender="user-2", now=200)
    with pytest.raises(UserReplyChainRejected):
        _prepare(store, message_id="other-chat-message", quoted="user-message-1",
                 chat="chat-2", now=200)
    with pytest.raises(UserReplyChainRejected):
        _prepare(store, message_id="unknown-parent", quoted="not-bound", now=200)
    with pytest.raises(UserReplyChainExpired):
        _prepare(store, message_id="late", quoted="bot-message-1", now=301)
    store.close()


def test_queue_record_content_or_route_tampering_is_rejected(tmp_path: Path) -> None:
    path = _database(tmp_path / "tamper.sqlite")
    store = UserReplyChainStore(path, initialize=True)
    _prepare(store, message_id="user-message-1", quoted="bot-message-1")
    _delivery(path, message_id="user-message-1", fingerprint="fp-1",
              text="tampered", delivery_id="delivery-1")
    with pytest.raises(UserReplyChainConflict):
        store.complete_queued_reply("user-message-1", delivery_id="delivery-1", now=200)
    store.close()


def test_concurrent_prepare_same_message_id_is_idempotent(tmp_path: Path) -> None:
    path = _database(tmp_path / "concurrent.sqlite")

    def prepare_once():
        with UserReplyChainStore(path, initialize=True) as store:
            return _prepare(store, message_id="same", quoted="bot-message-1")

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _item: prepare_once(), range(2)))
    assert {item.message_id for item in results} == {"same"}
    assert sum(item.is_new for item in results) == 1


def test_legacy_queued_delivery_can_be_materialized_when_exact_parent_is_known(
    tmp_path: Path,
) -> None:
    """Existing outbox rows remain importable once the adapter proves scope."""

    path = _database(tmp_path / "legacy.sqlite")
    _delivery(path, message_id="old-user-message", fingerprint="old-fp",
              text="historical reply", delivery_id="old-delivery")
    store = UserReplyChainStore(path, initialize=True)
    prepared = _prepare(
        store,
        message_id="old-user-message",
        quoted="bot-message-1",
        text="historical reply",
        fingerprint="old-fp",
    )
    assert prepared.state == "prepared"
    imported = store.complete_queued_reply(
        "old-user-message", delivery_id="old-delivery", now=200
    )
    assert imported.state == "queued"
    assert store.resolve_parent(
        "old-user-message", sender_id="user-1", chat_id="chat-1", now=200
    ) is not None
    store.close()


def test_durable_ack_status_requires_queued_link_and_rejects_tampering(
    tmp_path: Path,
) -> None:
    path = _database(tmp_path / "ack-status.sqlite")
    chain = UserReplyChainStore(path, initialize=True)
    _prepare(chain, message_id="ack-message", quoted="bot-message-1")
    assert chain.durable_ack_status("ack-message", now=200) == "pending"
    _delivery(
        path,
        message_id="ack-message",
        fingerprint="fp-1",
        text="hello",
        delivery_id="ack-delivery",
    )
    assert chain.durable_ack_status("ack-message", now=200) == "pending"
    chain.complete_queued_reply("ack-message", delivery_id="ack-delivery", now=200)
    assert chain.durable_ack_status("ack-message", now=200) == "accepted"
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "UPDATE reply_deliveries SET reply_text='tampered' "
            "WHERE delivery_id='ack-delivery'"
        )
        connection.commit()
    finally:
        connection.close()
    assert chain.durable_ack_status("ack-message", now=200) == "rejected"
    chain.close()


def test_state_ack_queries_distinguish_management_pending_and_reply_queue(
    tmp_path: Path,
) -> None:
    path = _database(tmp_path / "state-ack.sqlite")
    state = StateStore(path)
    codec = CorrelationCodec(b"a" * 32)
    code = codec.issue()
    try:
        state._connection.execute(
            "INSERT INTO notifications("
            "event_key, code, thread_id, turn_id, reply_kind, message_text, "
            "created_at, expires_at, sent_at, channel_message_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("ack-event", code, "thread-ack", "turn-ack", "turn", "notice",
             100, 10_000, 101, "bot-ack"),
        )
        state._connection.commit()
        assert state.management_inbound_status("management-ack") == "missing"
        assert state.reserve_management_inbound(
            "management-ack", "user-1", "管理命令"
        ) is True
        assert state.management_inbound_status(
            "management-ack", sender_id="user-1", content="管理命令"
        ) == "pending"
        assert state.management_inbound_status(
            "management-ack", sender_id="other", content="管理命令"
        ) == "conflict"
        state.complete_management_inbound("management-ack")
        assert state.management_inbound_status("management-ack") == "accepted"
        assert state.reply_delivery_status("reply-ack") == "missing"
        queued = state.enqueue_turn_reply(
            code,
            "reply-ack",
            "reply-fingerprint",
            codec,
            reply_text="正文",
            now=200,
        )
        assert queued is not None
        assert state.reply_delivery_status("reply-ack") == "accepted"
    finally:
        state.close()


def test_service_routes_two_level_chain_and_guardian_ack_after_outbox_commit(
    tmp_path: Path,
) -> None:
    path = _database(tmp_path / "service-chain.sqlite", expires_at=10_000_000_000)
    state = StateStore(path)
    codec = CorrelationCodec(b"s" * 32)
    valid_code = codec.issue()
    state._connection.execute(
        "UPDATE notifications SET code=? WHERE code='code-1'", (valid_code,)
    )
    state._connection.commit()
    chain = UserReplyChainStore(path)
    service = object.__new__(ProgressService)
    service.config = SimpleNamespace(
        messaging=SimpleNamespace(backend="fake"),
        feishu=SimpleNamespace(target_open_id="user-1"),
    )
    service.store = state
    service.codec = codec
    service.user_reply_chain = chain
    service.codex_store = None
    service.management = None
    service._pending_lock = threading.RLock()
    service._pending_server_replies = {}
    service.reply_queue = queue.Queue()
    service.receipt_queue = queue.Queue()
    service._reply_schedule_lock = threading.Lock()
    service._scheduled_reply_codes = set()
    service._deferred_reply_codes = set()
    service.stop_event = threading.Event()
    try:
        first = ChannelReply(
            sender_id="user-1",
            content="第一条",
            reply_to_message_id="bot-message-1",
            message_id="user-message-1",
            chat_id="chat-1",
        )
        assert service._process_channel_reply(first, _skip_parent_recovery=True) is True
        assert service.guardian_inbound_status("user-message-1") == "accepted"
        first_job = service.reply_queue.get_nowait()
        assert first_job.thread_id == "thread-1"

        second = ChannelReply(
            sender_id="user-1",
            content="第二条连续需求",
            reply_to_message_id="user-message-1",
            message_id="user-message-2",
            chat_id="chat-1",
        )
        assert service._process_channel_reply(second, _skip_parent_recovery=True) is True
        assert service.guardian_inbound_status("user-message-2") == "accepted"
        second_job = service.reply_queue.get_nowait()
        assert second_job.thread_id == first_job.thread_id
        assert second_job.reply_text == "第二条连续需求"
        assert chain.resolve_parent(
            "user-message-2", sender_id="user-1", chat_id="chat-1", now=200
        ) is not None
        assert valid_code
    finally:
        chain.close()
        state.close()


def test_service_routes_image_text_continuation_with_persisted_attachment_prompt(
    tmp_path: Path,
) -> None:
    path = _database(tmp_path / "service-image-chain.sqlite", expires_at=10_000_000_000)
    image_path = tmp_path / "quoted.png"
    image_path.write_bytes(b"verified-image")
    state = StateStore(path)
    codec = CorrelationCodec(b"i" * 32)
    valid_code = codec.issue()
    state._connection.execute(
        "UPDATE notifications SET code=? WHERE code='code-1'", (valid_code,)
    )
    state._connection.commit()
    chain = UserReplyChainStore(path)
    service = object.__new__(ProgressService)
    service.config = SimpleNamespace(
        messaging=SimpleNamespace(backend="fake"),
        feishu=SimpleNamespace(target_open_id="user-1"),
    )
    service.store = state
    service.codec = codec
    service.user_reply_chain = chain
    service.codex_store = None
    service.management = None
    service._pending_lock = threading.RLock()
    service._pending_server_replies = {}
    service.reply_queue = queue.Queue()
    service.receipt_queue = queue.Queue()
    service._reply_schedule_lock = threading.Lock()
    service._scheduled_reply_codes = set()
    service._deferred_reply_codes = set()
    service.stop_event = threading.Event()
    try:
        first = ChannelReply(
            sender_id="user-1",
            content="先建立连续会话",
            reply_to_message_id="bot-message-1",
            message_id="image-parent",
            chat_id="chat-1",
        )
        assert service._process_channel_reply(first, _skip_parent_recovery=True) is True
        service.reply_queue.get_nowait()
        second = ChannelReply(
            sender_id="user-1",
            content="请分析这张图",
            reply_to_message_id="image-parent",
            message_id="image-child",
            chat_id="chat-1",
            attachments=(
                ChannelAttachment(
                    str(image_path.resolve()),
                    "image/png",
                    hashlib.sha256(image_path.read_bytes()).hexdigest(),
                    image_path.stat().st_size,
                ),
            ),
        )
        assert service._process_channel_reply(second, _skip_parent_recovery=True) is True
        job = service.reply_queue.get_nowait()
        assert job.thread_id == "thread-1"
        assert job.reply_text.startswith("请分析这张图\n\n用户通过飞书发送了以下图片")
        assert str(image_path.resolve()) in job.reply_text
        record = chain.get("image-child")
        assert record is not None and record.state == "queued"
        assert service.guardian_inbound_status("image-child") == "accepted"
    finally:
        chain.close()
        state.close()


def test_guardian_rechecks_pending_handed_event_until_worker_durable_ack(
    tmp_path: Path,
) -> None:
    config = SimpleNamespace(
        service=SimpleNamespace(database=tmp_path / "business.sqlite"),
        feishu=SimpleNamespace(target_open_id="user-1", app_id="app-1"),
    )
    transport = GuardianStore(control_root(config))
    transport.receive(
        "guardian-message",
        {
            "sender_id": "user-1",
            "content": "继续",
            "message_id": "guardian-message",
            "chat_id": "chat-1",
        },
    )
    transport.close()

    class Worker:
        def __init__(self) -> None:
            self.calls = 0
            self.received: list[str] = []

        def handle(self, message: ChannelReply) -> bool:
            self.received.append(message.message_id)
            return False

        def guardian_inbound_status(self, message_id: str) -> str:
            assert message_id == "guardian-message"
            self.calls += 1
            return "accepted" if self.calls >= 3 else "pending"

    worker = Worker()
    channel = GuardianChannel(config, "generation-1")
    channel.start(worker.handle)
    deadline = time.monotonic() + 3
    try:
        while time.monotonic() < deadline:
            row = channel.store.db.execute(
                "SELECT acknowledged_at FROM incoming WHERE key=?",
                ("guardian-message",),
            ).fetchone()
            if row is not None and row[0] is not None:
                break
            time.sleep(0.05)
        row = channel.store.db.execute(
            "SELECT acknowledged_at FROM incoming WHERE key=?",
            ("guardian-message",),
        ).fetchone()
        assert row is not None and row[0] is not None
        assert worker.received == ["guardian-message"]
        assert worker.calls >= 3
    finally:
        channel.stop()
        channel.store.close()


def test_guardian_keyset_ack_rotation_does_not_starve_later_handed_rows(
    tmp_path: Path,
) -> None:
    """A pending first page must not prevent later rows from being revisited."""

    config = SimpleNamespace(
        service=SimpleNamespace(database=tmp_path / "business.sqlite"),
        feishu=SimpleNamespace(target_open_id="user-1", app_id="app-1"),
    )
    channel = GuardianChannel(config, "generation-1")
    keys = [f"guardian-pressure-{index:02d}" for index in range(1, 41)]
    for key in keys:
        channel.store.receive(
            key,
            {
                "sender_id": "user-1",
                "content": "继续",
                "message_id": key,
                "chat_id": "chat-1",
            },
        )
        # Model a worker hand-off that has not yet reached its durable ACK
        # boundary.  The payload remains available to the next generation.
        channel.store.accepted(key, "generation-1")

    class Worker:
        def __init__(self) -> None:
            self.calls: dict[str, int] = {}

        def handle(self, message: ChannelReply) -> bool:
            raise AssertionError("already handed rows must be polled, not re-delivered")

        def guardian_inbound_status(self, message_id: str) -> str:
            count = self.calls.get(message_id, 0) + 1
            self.calls[message_id] = count
            number = int(message_id.rsplit("-", 1)[1])
            if number == 1:
                return "accepted" if count >= 2 else "pending"
            if number <= 32:
                return "pending"
            return "accepted"

    worker = Worker()
    channel.start(worker.handle)
    deadline = time.monotonic() + 3
    try:
        while time.monotonic() < deadline:
            rows = channel.store.db.execute(
                "SELECT key, acknowledged_at FROM incoming "
                "WHERE key IN (?, ?) ORDER BY key",
                (keys[0], keys[-1]),
            ).fetchall()
            if rows and all(row[1] is not None for row in rows):
                break
            time.sleep(0.02)
        rows = channel.store.db.execute(
            "SELECT key, acknowledged_at FROM incoming "
            "WHERE key IN (?, ?) ORDER BY key",
            (keys[0], keys[-1]),
        ).fetchall()
        assert rows and all(row[1] is not None for row in rows)
        assert worker.calls[keys[0]] >= 2
        assert worker.calls[keys[-1]] >= 1
        # The other first-page rows remain pending by design; they must have
        # been revisited only after the second page had a chance to complete.
        pending = channel.store.db.execute(
            "SELECT count(*) FROM incoming WHERE acknowledged_at IS NULL "
            "AND key LIKE 'guardian-pressure-%'"
        ).fetchone()[0]
        assert pending == 31
    finally:
        channel.stop()
        channel.store.close()
