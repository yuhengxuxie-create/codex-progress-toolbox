from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from progress_wx.delivered_files import DeliveredFileCandidate, discover_delivered_files
from progress_wx.feishu import FeishuSendError
from progress_wx.file_delivery import FileDeliveryQueue
from progress_wx.models import GeneratedImageArtifact, ProgressReport, TurnEvent
from progress_wx.service import ProgressService
from progress_wx.state import CorrelationCodec, StateStore


SECRET = b"native-image-test-secret-32-bytes!!"


class NativeChannel:
    """Offline channel that records the distinct native and file send paths."""

    def __init__(self, *, online: bool = True, image_failure: Exception | None = None):
        self.online = online
        self.image_failure = image_failure
        self.text_calls: list[tuple[str, str]] = []
        self.file_calls: list[tuple[str, str, str]] = []
        self.image_calls: list[tuple[bytes, str]] = []

    def is_online(self) -> bool:
        return self.online

    def send_text(self, text: str, *, idempotency_key: str) -> list[str]:
        self.text_calls.append((text, idempotency_key))
        return [f"text-{len(self.text_calls)}"]

    def send_file(self, data: bytes, *, file_name: str, idempotency_key: str) -> list[str]:
        self.file_calls.append((file_name, idempotency_key, data.hex()))
        return [f"file-{len(self.file_calls)}"]

    def send_image(self, data: bytes, *, idempotency_key: str) -> list[str]:
        self.image_calls.append((data, idempotency_key))
        if self.image_failure is not None:
            raise self.image_failure
        return [f"image-{len(self.image_calls)}"]


class ImmediateSummarizer:
    def __init__(self, report: ProgressReport):
        self.report = report

    def immediate_report(self, _event: TurnEvent) -> ProgressReport:
        return self.report


def _image(tmp_path: Path, *, name: str = "native.png", payload: bytes | None = None) -> GeneratedImageArtifact:
    if payload is None:
        payload = b"\x89PNG\r\n\x1a\nsynthetic-native-image"
    path = tmp_path / name
    path.write_bytes(payload)
    return GeneratedImageArtifact(
        item_id="image-1",
        path=str(path),
        mime_type="image/png",
        sha256=hashlib.sha256(payload).hexdigest(),
        size=len(payload),
        file_name=name,
    )


def _generated_image_for_thread(
    tmp_path: Path,
    thread_id: str,
    *,
    payload: bytes = b"\x89PNG\r\n\x1a\nsynthetic-native-image",
) -> GeneratedImageArtifact:
    directory = tmp_path / "generated_images" / thread_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "image-1.png"
    path.write_bytes(payload)
    return GeneratedImageArtifact(
        item_id="image-1",
        path=str(path),
        mime_type="image/png",
        sha256=hashlib.sha256(payload).hexdigest(),
        size=len(payload),
        file_name=path.name,
    )


def _event(image: GeneratedImageArtifact, *, final_message: str = "阶段完成") -> TurnEvent:
    return TurnEvent(
        thread_id="thread-native-image",
        turn_id="turn-native-image",
        status="completed",
        final_message=final_message,
        generated_images=(image,),
    )


def _generic_candidate(image: GeneratedImageArtifact) -> DeliveredFileCandidate:
    path = Path(image.path)
    payload = path.read_bytes()
    return DeliveredFileCandidate(
        candidate_id="generic-native-image",
        path=path,
        display_name=path.name,
        source_kind="explicit_delivery",
        status="ready",
        delivery_requested=True,
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def _queue(tmp_path: Path, channel: NativeChannel) -> tuple[StateStore, FileDeliveryQueue]:
    store = StateStore(tmp_path / "state.db")
    queue = FileDeliveryQueue(
        store,
        tmp_path / "blobs",
        channel,
        bind_messages=lambda _row, _ids, _notice: None,
        start=False,
    )
    return store, queue


def _service(store: StateStore, queue: FileDeliveryQueue, channel: NativeChannel, *, immediate=None):
    service = ProgressService.__new__(ProgressService)
    service.config = SimpleNamespace(
        messaging=SimpleNamespace(backend="feishu", pending_ttl_hours=24),
        service=SimpleNamespace(max_attempts=1, retry_delays=(0,)),
    )
    service.store = store
    service.codec = CorrelationCodec(SECRET)
    service.channel = channel
    service.file_delivery_queue = queue
    service.summarizer = immediate if immediate is not None else object()
    service.stop_event = threading.Event()
    service._public_event_title = lambda event: event
    service._notification_policy_context = lambda event: SimpleNamespace(
        user_request="", task_state=event.status, recent_successful_notifications=()
    )
    service._summarize_with_policy_context = lambda _event, _context: ProgressReport(
        "silent", "", notification_reason="silent"
    )
    service._policy = lambda: SimpleNamespace(max_attempts=1, delays=(0,))
    service._retry_sleep = lambda _delay: None
    service._on_retry = lambda _operation: None
    service._format_event_notification = lambda *_args, **_kwargs: "synthetic notification"
    service._persist_notification_judgment = lambda *_args, **_kwargs: None
    service._completion_raw_binding_values = lambda *_args, **_kwargs: None
    return service


def _artifact_rows(store: StateStore) -> list[dict]:
    with store._lock:
        rows = store._connection.execute(
            "SELECT delivery_id, candidate_id, media_kind, state, reason, notice_state, "
            "message_ids_json, snapshot_json "
            "FROM artifact_file_deliveries ORDER BY delivery_id"
        ).fetchall()
    return [dict(row) for row in rows]


def test_silent_native_image_sends_once_via_send_image(tmp_path: Path):
    channel = NativeChannel()
    store, queue = _queue(tmp_path, channel)
    try:
        image = _image(tmp_path)
        service = _service(store, queue, channel)

        service._send_event(_event(image))
        queue.drain_once()

        assert len(channel.image_calls) == 1
        assert channel.file_calls == []
        assert channel.text_calls == []
        assert _artifact_rows(store)[0]["media_kind"] == "image"
        assert _artifact_rows(store)[0]["state"] == "done"
    finally:
        queue.stop()
        store.close()


def test_non_silent_native_image_has_no_old_pipeline_double_send(tmp_path: Path):
    channel = NativeChannel()
    store, queue = _queue(tmp_path, channel)
    try:
        image = _image(tmp_path)
        event = _event(image)
        service = _service(
            store,
            queue,
            channel,
            immediate=ImmediateSummarizer(
                ProgressReport("important", "重要更新", notification_reason="important_update")
            ),
        )

        service._send_event(event)
        queue.drain_once()

        assert len(channel.image_calls) == 1
        assert len(channel.text_calls) == 1
        assert channel.file_calls == []
        with store._lock:
            old_media_count = store._connection.execute(
                "SELECT COUNT(*) FROM notification_media_deliveries"
            ).fetchone()[0]
        assert old_media_count == 0
        assert _artifact_rows(store)[0]["state"] == "done"
    finally:
        queue.stop()
        store.close()


def test_file_markdown_same_native_image_is_one_image_delivery(tmp_path: Path):
    channel = NativeChannel()
    store, queue = _queue(tmp_path, channel)
    try:
        image = _image(tmp_path)
        result = discover_delivered_files(
            [
                {
                    "item_id": "final-native-image",
                    "type": "agentMessage",
                    "text": f"已交付：[图像](<{image.path}>)",
                }
            ],
            turn_id="turn-native-image",
            inspect_content=False,
        )
        assert len(result.candidates) == 1
        event = _event(image)
        queue.reserve(event, result.candidates)
        queue.drain_once()

        rows = _artifact_rows(store)
        assert len(rows) == 1
        assert rows[0]["media_kind"] == "image"
        assert rows[0]["state"] == "done"
        assert len(channel.image_calls) == 1
        assert channel.file_calls == []
    finally:
        queue.stop()
        store.close()


def test_unknown_native_image_restart_keeps_blob_and_does_not_resend(tmp_path: Path):
    first_channel = NativeChannel(image_failure=FeishuSendError("transport status unknown"))
    store, queue = _queue(tmp_path, first_channel)
    try:
        image = _image(tmp_path)
        event = _event(image)
        queue.reserve(event, ())
        queue.drain_once()

        first_row = _artifact_rows(store)[0]
        assert first_row["state"] == "uncertain"
        assert first_row["snapshot_json"]
        snapshot = json.loads(first_row["snapshot_json"])
        blob_path = queue.blobs.root / snapshot["name"]
        assert blob_path.is_file()
        assert len(first_channel.image_calls) == 1

        second_channel = NativeChannel()
        restarted = FileDeliveryQueue(
            store,
            tmp_path / "blobs",
            second_channel,
            bind_messages=lambda _row, _ids, _notice: None,
            start=False,
        )
        try:
            restarted.drain_once()
            assert second_channel.image_calls == []
            assert _artifact_rows(store)[0]["state"] == "uncertain"
            assert blob_path.is_file()
        finally:
            restarted.stop()
    finally:
        queue.stop()
        store.close()


def test_legacy_media_outbox_is_not_migrated_or_resent(tmp_path: Path):
    channel = NativeChannel()
    store, queue = _queue(tmp_path, channel)
    try:
        image = _image(tmp_path)
        event = _event(image)
        codec = CorrelationCodec(SECRET)
        store.reserve_notification(event, codec.issue(), "old native media", 24)
        store.bind_channel_messages(event.dedupe_key, ("old-parent-message",))
        store.mark_sent(event.dedupe_key)
        store.reserve_notification_media(event.dedupe_key, (image,))
        legacy = store.pending_notification_media(event_key=event.dedupe_key)
        assert len(legacy) == 1
        assert store.claim_notification_media(legacy[0].delivery_id) is not None
        store.mark_notification_media_delivered_with_message_ids(
            legacy[0].delivery_id, ("old-native-image-message",)
        )

        handled = queue.reserve(event, ())

        assert handled == set()
        assert _artifact_rows(store) == []
        assert channel.image_calls == []
        assert channel.file_calls == []
        with store._lock:
            old_media = store._connection.execute(
                "SELECT delivered_at, uncertain_at, discarded_at "
                "FROM notification_media_deliveries WHERE delivery_id = ?",
                (legacy[0].delivery_id,),
            ).fetchone()
        assert old_media[0] is not None
        assert old_media[1] is None
        assert old_media[2] is None
    finally:
        queue.stop()
        store.close()


def test_native_image_binding_supports_reply_to_original_task(tmp_path: Path):
    channel = NativeChannel()
    store, queue = _queue(tmp_path, channel)
    try:
        service = _service(store, queue, channel)
        queue.bind_messages = service._bind_artifact_messages
        image = _image(tmp_path)
        event = _event(image)
        queue.reserve(event, ())
        queue.drain_once()

        row = _artifact_rows(store)[0]
        message_id = json.loads(row["message_ids_json"])[0]
        reply_code = store.code_for_channel_message(message_id)
        assert reply_code
        reply = store.enqueue_turn_reply(
            reply_code,
            "reply-native-image",
            "reply-fingerprint",
            service.codec,
            reply_text="继续修改",
        )
        assert reply is not None
        assert reply.thread_id == event.thread_id
        assert reply.turn_id == event.turn_id
    finally:
        queue.stop()
        store.close()


def test_empty_body_tool_only_still_reserves_native_image(tmp_path: Path):
    channel = NativeChannel()
    store, queue = _queue(tmp_path, channel)
    try:
        image = _image(tmp_path)
        event = _event(image, final_message="")
        service = _service(store, queue, channel)

        service._send_event(event)
        queue.drain_once()

        assert len(channel.image_calls) == 1
        assert channel.text_calls == []
        assert channel.file_calls == []
        assert _artifact_rows(store)[0]["state"] == "done"
    finally:
        queue.stop()
        store.close()


@pytest.mark.parametrize("projection_mode", ("completed_at", "current_parent"))
def test_late_native_image_projection_for_current_processed_turn(
    tmp_path: Path, projection_mode: str
):
    channel = NativeChannel()
    store, queue = _queue(tmp_path, channel)
    try:
        image = _image(tmp_path)
        now = int(time.time())
        plain = TurnEvent(
            thread_id="thread-late-image",
            turn_id="turn-late-image",
            status="completed",
            final_message="完成",
            completed_at=now if projection_mode == "completed_at" else None,
        )
        service = _service(store, queue, channel)
        if projection_mode == "completed_at":
            # The first pass is a real silent decision.  The structured image
            # projection arrives afterward with the same completed turn time.
            service._send_event(plain)
        else:
            # A current parent notification is the alternate late-projection
            # evidence when the snapshot has no trustworthy completed_at.
            store.reserve_notification(
                plain, service.codec.issue(), "current parent", 24
            )
            store.mark_sent(plain.dedupe_key)
            store.mark_processed(plain.dedupe_key)

        late = TurnEvent(
            thread_id=plain.thread_id,
            turn_id=plain.turn_id,
            status=plain.status,
            final_message=plain.final_message,
            completed_at=plain.completed_at,
            generated_images=(image,),
        )
        service._send_event(late)
        queue.drain_once()

        assert len(channel.image_calls) == 1
        assert _artifact_rows(store)[0]["media_kind"] == "image"
    finally:
        queue.stop()
        store.close()


def test_processed_historical_turn_without_late_evidence_does_not_backfill_image(
    tmp_path: Path,
):
    channel = NativeChannel()
    store, queue = _queue(tmp_path, channel)
    try:
        image = _image(tmp_path)
        plain = TurnEvent(
            thread_id="thread-old-image",
            turn_id="turn-old-image",
            status="completed",
            final_message="完成",
        )
        service = _service(store, queue, channel)
        service._send_event(plain)

        late = TurnEvent(
            thread_id=plain.thread_id,
            turn_id=plain.turn_id,
            status=plain.status,
            final_message=plain.final_message,
            generated_images=(image,),
        )
        assert queue.reserve(late, ()) == {
            str(Path(image.path).resolve()).casefold()
        }
        queue.drain_once()

        assert channel.image_calls == []
        assert _artifact_rows(store) == []
    finally:
        queue.stop()
        store.close()


def test_source_changed_generic_row_stays_rejected_when_native_image_arrives_late(
    tmp_path: Path,
):
    channel = NativeChannel()
    store, queue = _queue(tmp_path, channel)
    try:
        image = _image(tmp_path, payload=b"original-bytes")
        generic_event = TurnEvent(
            thread_id="thread-source-changed",
            turn_id="turn-source-changed",
            status="completed",
            final_message="交付文件",
        )
        queue.reserve(generic_event, (_generic_candidate(image),))

        # Change the source after discovery but before capture.  The original
        # candidate identity must remain authoritative for this delivery.
        image_path = Path(image.path)
        image_path.write_bytes(b"later-bytes")
        queue.drain_once()

        rejected = _artifact_rows(store)[0]
        assert rejected["state"] == "rejected"
        assert rejected["media_kind"] == "file"
        assert rejected["reason"] == "source_changed_before_snapshot"
        assert rejected["notice_state"] == "done"
        assert len(channel.text_calls) == 1
        assert "未能发送" in channel.text_calls[0][0]

        later_payload = image_path.read_bytes()
        late_image = GeneratedImageArtifact(
            item_id=image.item_id,
            path=image.path,
            mime_type=image.mime_type,
            sha256=hashlib.sha256(later_payload).hexdigest(),
            size=len(later_payload),
            file_name=image.file_name,
        )
        late_event = TurnEvent(
            thread_id=generic_event.thread_id,
            turn_id=generic_event.turn_id,
            status=generic_event.status,
            final_message=generic_event.final_message,
            generated_images=(late_image,),
        )
        queue.reserve(late_event, ())
        queue.drain_once()

        preserved = _artifact_rows(store)[0]
        assert preserved["state"] == "rejected"
        assert preserved["media_kind"] == "file"
        assert preserved["reason"] == "source_changed_before_snapshot"
        assert len(channel.image_calls) == 0
        assert len(channel.file_calls) == 0
        assert len(channel.text_calls) == 1
    finally:
        queue.stop()
        store.close()


def test_pending_generic_row_upgrades_to_native_image_on_late_projection(
    tmp_path: Path,
):
    channel = NativeChannel(online=False)
    store, queue = _queue(tmp_path, channel)
    try:
        image = _image(tmp_path, payload=b"pending-native-image")
        generic_event = TurnEvent(
            thread_id="thread-pending-upgrade",
            turn_id="turn-pending-upgrade",
            status="completed",
            final_message="交付文件",
        )
        queue.reserve(generic_event, (_generic_candidate(image),))
        queue.drain_once()
        assert _artifact_rows(store)[0]["state"] == "pending"
        assert _artifact_rows(store)[0]["media_kind"] == "file"

        late_event = TurnEvent(
            thread_id=generic_event.thread_id,
            turn_id=generic_event.turn_id,
            status=generic_event.status,
            final_message=generic_event.final_message,
            generated_images=(image,),
        )
        queue.reserve(late_event, ())
        assert _artifact_rows(store)[0]["media_kind"] == "image"

        channel.online = True
        queue.drain_once()
        assert len(channel.image_calls) == 1
        assert channel.file_calls == []
        assert _artifact_rows(store)[0]["state"] == "done"
    finally:
        queue.stop()
        store.close()


@pytest.mark.parametrize("old_turn_time", ("before_waterline", "missing"))
def test_late_native_image_before_waterline_does_not_enter_legacy_media_outbox(
    tmp_path: Path, old_turn_time: str
):
    channel = NativeChannel()
    store, queue = _queue(tmp_path, channel)
    try:
        thread_id = f"thread-waterline-{old_turn_time}"
        turn_id = f"turn-waterline-{old_turn_time}"
        completed_at = (
            int(queue.enabled_at) - 1 if old_turn_time == "before_waterline" else None
        )
        parent = TurnEvent(
            thread_id=thread_id,
            turn_id=turn_id,
            status="completed",
            final_message="旧 parent",
            completed_at=completed_at,
        )
        store.reserve_notification(parent, CorrelationCodec(SECRET).issue(), "old parent", 24)
        with store._lock, store._connection:
            store._connection.execute(
                "UPDATE notifications SET created_at=?, expires_at=? WHERE event_key=?",
                (int(queue.enabled_at) - 1, int(queue.enabled_at) + 86_400, parent.dedupe_key),
            )
        store.mark_sent(parent.dedupe_key)
        store.mark_processed(parent.dedupe_key)

        image = _generated_image_for_thread(tmp_path, thread_id)
        late = TurnEvent(
            thread_id=thread_id,
            turn_id=turn_id,
            status="completed",
            final_message="旧 parent",
            completed_at=completed_at,
            generated_images=(image,),
        )
        service = _service(store, queue, channel)
        service._send_event(late)

        assert channel.image_calls == []
        assert channel.file_calls == []
        assert channel.text_calls == []
        assert _artifact_rows(store) == []
        with store._lock:
            media_count = store._connection.execute(
                "SELECT COUNT(*) FROM notification_media_deliveries WHERE event_key=?",
                (late.dedupe_key,),
            ).fetchone()[0]
        assert media_count == 0
    finally:
        queue.stop()
        store.close()
