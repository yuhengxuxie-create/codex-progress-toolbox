from __future__ import annotations

import hashlib
import io
import queue
import random
import logging
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image
import progress_wx.service as service_module

from progress_wx.approval_bridge import ApprovalBridge
from progress_wx.channel import (
    ChannelAttachment,
    ChannelReply,
    MessageChannelOfflineError,
)
from progress_wx.codex_account import (
    AccountRateLimits,
    CodexAccountError,
    RateLimitWindow,
)
from progress_wx.codex_app_tools import (
    DesktopAppToolsNotSubmitted,
    DesktopAppToolsRejected,
    DesktopAppToolsResultUnknown,
    DesktopAppToolsUnavailable,
)
from progress_wx.codex_store import (
    CodexStoreReadError,
    ThreadRecord,
    ThreadSnapshot,
    ThreadStatus,
    TurnRecord,
)
from progress_wx.codex_rpc import CodexRPCError, CodexRPCTimeout, ServerRequest, TurnCompletedEvent
from progress_wx.config import load_config
from progress_wx.formatting import REPLY_RECEIPT_MAX_CHARS, format_reply_receipt
from progress_wx.feishu import (
    FeishuMessageChannel,
    FeishuSendError,
    FeishuSendNotSubmittedError,
    FeishuSendRejectedError,
)
from progress_wx.file_delivery import FileDeliveryQueue
from progress_wx.models import (
    GeneratedImageArtifact,
    NotificationReason,
    ProgressReport,
    TurnEvent,
)
from progress_wx.retry import RetryExhausted
from progress_wx.service import (
    PendingServerReply,
    ProgressService,
    ReplyJob,
    ReplyReceiptJob,
    ReplyDeferred,
    ServiceFatalError,
    ServiceStopping,
    _codex_connection_identity,
    _file_identity,
    _parse_heartbeat_control,
    _prepare_feishu_image,
    desktop_attention_event,
    desktop_loaded_monitors,
    desktop_attention_source,
    hook_payload_to_event,
    server_request_event,
    server_request_response,
    snapshot_to_event,
    started_turn_id,
    steered_turn_id,
)
from progress_wx.state import CorrelationCodec, StateStore
from progress_wx.wechat import QuoteMessage
from progress_wx.wechat import FriendVerificationError


def make_config(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(
        f"""
codex:
  home: "{tmp_path.as_posix()}"
  command: codex
  reply_timeout_seconds: 30
monitor:
  ids: [thread-1]
wechat:
  backend: fake
  tool_account_nickname: 通知小号
  tool_wechat_id: wxid-tool
  target_chat: 主号
  target_wechat_id: wxid-main
  secret_file: "{(tmp_path / 'secret').as_posix()}"
service:
  max_attempts: 5
  retry_delays: [0, 0, 0, 0, 0]
  database: "{(tmp_path / 'state.sqlite').as_posix()}"
  log_dir: "{(tmp_path / 'logs').as_posix()}"
summary:
  mode: codex_final
""",
        encoding="utf-8",
    )
    config = load_config(path)
    config.validate_ready()
    return config


def make_generated_artifact(
    tmp_path: Path,
    *,
    thread_id: str,
    item_id: str,
    suffix: str = ".png",
    body: bytes = b"synthetic-image",
) -> GeneratedImageArtifact:
    headers = {
        ".png": b"\x89PNG\r\n\x1a\n",
        ".jpg": b"\xff\xd8\xff\xe0",
        ".webp": b"RIFF\x00\x00\x00\x00WEBP",
    }
    mime_types = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".webp": "image/webp",
    }
    path = tmp_path / "generated_images" / thread_id / f"{item_id}{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = headers[suffix] + body
    path.write_bytes(data)
    return GeneratedImageArtifact(
        item_id=item_id,
        path=str(path),
        mime_type=mime_types[suffix],
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        file_name=path.name,
    )


def install_artifact_queue(service: ProgressService) -> FileDeliveryQueue:
    """Use a deterministic native artifact queue in service tests."""
    assert service.store is not None
    queue = FileDeliveryQueue(
        service.store,
        service.store.path.parent / "artifact-snapshots",
        service.channel,
        service._bind_artifact_messages,
        start=False,
    )
    service.file_delivery_queue = queue
    return queue


def test_management_worker_channel_rejection_never_sends_desktop_error() -> None:
    class RejectedManagementController:
        def __init__(self) -> None:
            self.handled: list[ChannelReply] = []
            self.system_errors: list[ChannelReply] = []

        def handle(self, message: ChannelReply) -> None:
            self.handled.append(message)
            if len(self.handled) == 1:
                raise FeishuSendRejectedError(
                    code="permission_denied",
                    raw_code=99991672,
                    retryable=False,
                )

        def send_system_error(self, message: ChannelReply) -> None:
            self.system_errors.append(message)

    service = object.__new__(ProgressService)
    service.stop_event = threading.Event()
    service.management_queue = queue.Queue()
    controller = RejectedManagementController()
    service.management = controller
    rejected = ChannelReply(
        sender_id="ou_owner",
        content="合成管理动作",
        message_id="synthetic-management-rejected",
        chat_id="oc_private",
    )
    following = ChannelReply(
        sender_id="ou_owner",
        content="后续管理动作",
        message_id="synthetic-management-following",
        chat_id="oc_private",
    )
    service.management_queue.put(rejected)
    service.management_queue.put(following)
    service.management_queue.put(None)

    service._management_worker()

    assert controller.handled == [rejected, following]
    assert controller.system_errors == []
    assert service.stop_event.is_set() is False


def test_snapshot_and_hook_mapping_use_structured_status() -> None:
    thread = ThreadRecord("thread-1", title="示例", cwd="D:/work")
    turn = TurnRecord(
        "thread-1",
        "turn-1",
        ThreadStatus.FAILED,
        error_json='{"message":"权限被拒绝"}',
        final_message="结构化最终答复",
    )
    event = snapshot_to_event(ThreadSnapshot(thread, turn, ThreadStatus.FAILED, True, True))
    assert event is not None
    assert event.status == "failed"
    assert event.error_message == "权限被拒绝"
    assert event.final_message == "结构化最终答复"

    hook = hook_payload_to_event(
        {
            "type": "agent-turn-complete",
            "thread-id": "thread-1",
            "turn-id": "turn-2",
            "last-assistant-message": "本轮结果",
        },
        thread,
    )
    assert hook.status == "completed"
    assert hook.final_message == "本轮结果"


def test_heartbeat_control_parser_is_strict_and_preserves_plain_text() -> None:
    assert _parse_heartbeat_control(
        """
        <heartbeat>
          <automation_id>codex</automation_id>
          <decision>DONT_NOTIFY</decision>
          <message>无预警</message>
        </heartbeat>
        """
    ) == ("DONT_NOTIFY", "无预警")
    assert _parse_heartbeat_control(
        """
        <heartbeat>
          <message>检测到额度即将刷新</message>
          <decision>notify</decision>
          <automation_id>codex-reset</automation_id>
        </heartbeat>
        """
    ) == ("NOTIFY", "检测到额度即将刷新")
    assert _parse_heartbeat_control(
        "这里引用示例：<heartbeat><automation_id>x</automation_id>"
        "<decision>DONT_NOTIFY</decision><message>无预警</message></heartbeat>"
    ) is None
    assert _parse_heartbeat_control(
        "<!DOCTYPE heartbeat><heartbeat><automation_id>x</automation_id>"
        "<decision>DONT_NOTIFY</decision><message>无预警</message></heartbeat>"
    ) is None


def test_desktop_attention_uses_active_flag_and_cursor_not_text_keywords() -> None:
    thread = ThreadRecord("thread-1", title="飞书机器人开发", cwd="D:/work")
    poll = {
        "cursor": "cursor-1",
        "thread": {
            "id": "thread-1",
            "status": {"type": "active", "activeFlags": ["waitingOnUserInput"]},
        },
        "latestTurn": {"id": "turn-goal", "status": "inProgress"},
        "latestAssistantMessage": {"phase": "commentary", "text": "请选择路线 A 或 B。"},
    }

    first = desktop_attention_event(poll, thread)
    second = desktop_attention_event({**poll, "cursor": "cursor-2"}, thread)

    assert first is not None and second is not None
    assert first.status == "waitingOnUserInput"
    assert first.title == "飞书机器人开发"
    assert "请选择路线 A 或 B" in first.final_message
    assert first.turn_id != second.turn_id

    no_flag = {
        **poll,
        "thread": {
            "id": "thread-1",
            "status": {"type": "active", "activeFlags": []},
        },
        "latestAssistantMessage": {"text": "请回复我"},
    }
    assert desktop_attention_event(no_flag, thread) is None


def test_desktop_attention_source_prefers_unmonitored_idle_codex_thread() -> None:
    listing = {
        "pinnedThreads": [],
        "threads": [
            {"id": "monitored", "kind": "codex", "hostId": "local", "status": "idle"},
            {"id": "active-source", "kind": "codex", "hostId": "local", "status": "active"},
            {"id": "idle-source", "kind": "codex", "hostId": "local", "status": "idle"},
            {"id": "chat", "kind": "chatgpt", "status": "idle"},
        ],
    }

    assert desktop_attention_source(listing, {"monitored"}) == "idle-source"


def test_desktop_attention_source_uses_unmonitored_not_loaded_thread_as_fallback() -> None:
    listing = {
        "pinnedThreads": [],
        "threads": [
            {"id": "monitored", "kind": "codex", "hostId": "local", "status": "active"},
            {
                "id": "old-cold-source",
                "kind": "codex",
                "hostId": "local",
                "status": "notLoaded",
                "updatedAt": 10,
            },
            {
                "id": "new-cold-source",
                "kind": "codex",
                "hostId": "local",
                "status": "notLoaded",
                "updatedAt": 20,
            },
            {"id": "remote-cold", "kind": "codex", "status": "notLoaded"},
        ],
    }

    assert desktop_attention_source(listing, {"monitored"}) == "new-cold-source"


def test_desktop_loaded_monitors_excludes_not_loaded_history() -> None:
    listing = {
        "pinnedThreads": [
            {"id": "active", "kind": "codex", "hostId": "local", "status": "active"}
        ],
        "threads": [
            {"id": "idle", "kind": "codex", "hostId": "local", "status": "idle"},
            {
                "id": "cold",
                "kind": "codex",
                "hostId": "local",
                "status": "notLoaded",
            },
            {"id": "other", "kind": "codex", "hostId": "local", "status": "active"},
        ],
    }

    assert desktop_loaded_monitors(listing, {"active", "idle", "cold"}) == (
        "active",
        "idle",
    )


def test_attention_listing_skips_unloaded_monitor_context() -> None:
    calls: list[str] = []

    class FakeSession:
        def list_threads(self, thread_id: str):
            calls.append(thread_id)
            if thread_id == "cold-thread":
                from progress_wx.codex_app_tools import DesktopAppToolsError

                raise DesktopAppToolsError("calling thread is not loaded")
            return {"threads": [{"id": "source"}], "pinnedThreads": []}

    listing = ProgressService._attention_listing(  # type: ignore[arg-type]
        FakeSession(),
        ("cold-thread", "loaded-thread"),
    )

    assert calls == ["cold-thread", "loaded-thread"]
    assert listing["threads"][0]["id"] == "source"


def test_completed_snapshot_without_final_body_waits_for_next_poll() -> None:
    thread = ThreadRecord("thread-1", title="示例", cwd="D:/work")
    turn = TurnRecord(
        "thread-1",
        "turn-completed-before-body",
        ThreadStatus.COMPLETED,
        final_agent_item_id="item-pending",
        final_message="",
    )

    assert snapshot_to_event(
        ThreadSnapshot(thread, turn, ThreadStatus.COMPLETED, True, True)
    ) is None


def test_failed_snapshot_without_final_body_still_reports_structured_error() -> None:
    thread = ThreadRecord("thread-1", title="示例", cwd="D:/work")
    turn = TurnRecord(
        "thread-1",
        "turn-failed-without-body",
        ThreadStatus.FAILED,
        error_json='{"message":"执行失败"}',
        final_message="",
    )

    event = snapshot_to_event(
        ThreadSnapshot(thread, turn, ThreadStatus.FAILED, True, True)
    )

    assert event is not None
    assert event.status == "failed"
    assert event.error_message == "执行失败"


def test_empty_completed_hook_waits_and_uses_same_turn_database_body(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    store = StateStore(config.service.database)
    thread = ThreadRecord("thread-1", title="飞书机器人开发", cwd="D:/work")

    class DeferredBodyCodexStore:
        body = ""

        def snapshot(self, _thread_id: str) -> ThreadSnapshot:
            turn = TurnRecord(
                "thread-1",
                "turn-deferred-body",
                ThreadStatus.COMPLETED,
                final_agent_item_id="item-deferred",
                final_message=self.body,
            )
            return ThreadSnapshot(
                thread,
                turn,
                ThreadStatus.COMPLETED,
                True,
                True,
            )

    captured_bodies: list[str] = []

    class CapturingSummarizer:
        def summarize(self, event, *, wait=None):
            del wait
            captured_bodies.append(event.final_message)
            return ProgressReport(
                "完成",
                "已读取真实最终答复。",
                NotificationReason.CONVERSATION_COMPLETE,
            )

    channel = FakeWechat()
    codex = DeferredBodyCodexStore()
    service = ProgressService(config.path)
    service.config = config
    service.store = store
    service.codec = CorrelationCodec(b"b" * 32)
    service.channel = channel  # type: ignore[assignment]
    service.codex_store = codex  # type: ignore[assignment]
    service.summarizer = CapturingSummarizer()  # type: ignore[assignment]
    service._selected_threads = lambda _config: {"thread-1": thread}  # type: ignore[method-assign]

    store.enqueue_hook_payload(
        {
            "type": "agent-turn-complete",
            "thread-id": "thread-1",
            "turn-id": "turn-deferred-body",
        }
    )

    service._poll_once(config)
    assert channel.messages == []
    assert captured_bodies == []
    assert store.pending_hook_count() == 1

    codex.body = "数据库稍后写入的真实最终答复"
    service._poll_once(config)

    assert len(channel.messages) == 1
    assert captured_bodies == ["数据库稍后写入的真实最终答复"]
    assert store.pending_hook_count() == 0
    assert store.was_processed("thread-1:turn-deferred-body:completed")
    store.close()


def test_reply_receipt_format_is_exact_and_bounded() -> None:
    message = format_reply_receipt(True, "下一步" * 100)
    fields = message.split("\n\n")
    assert fields[0] == "消息状态：已收到"
    assert fields[1].startswith("回复信息：")
    assert len(fields[1].removeprefix("回复信息：")) <= REPLY_RECEIPT_MAX_CHARS
    assert format_reply_receipt(False, "格式错误") == (
        "消息状态：未收到\n\n回复信息：格式错误"
    )


class FakeWechat:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send_text(self, text: str) -> None:
        self.messages.append(text)

    def is_online(self) -> bool:
        return True


class FakeSummarizer:
    def summarize(self, _event, *, wait=None):
        del wait
        return ProgressReport(
            "完成",
            "自动测试通过。",
            NotificationReason.CONVERSATION_COMPLETE,
        )


class TwoStageSummarizer:
    def __init__(self, details: str = "后台摘要已整理完成。") -> None:
        self.details = details
        self.calls = 0

    def immediate_report(self, _event):
        return None

    def summarize(self, _event, *, wait=None):
        del wait
        self.calls += 1
        return ProgressReport(
            "完成",
            self.details,
            NotificationReason.CONVERSATION_COMPLETE,
        )


def test_feishu_short_answer_waits_for_decision_and_sends_one_final_message(
    tmp_path: Path,
) -> None:
    base = make_config(tmp_path)
    config = replace(base, messaging=replace(base.messaging, backend="feishu"))

    class DirectSummarizer:
        model_calls = 0

        def immediate_report(self, _event):
            return None

        def summarize(self, _event, *, wait=None):
            del wait
            self.model_calls += 1
            return ProgressReport(
                "完成",
                "已经处理好了。",
                NotificationReason.CONVERSATION_COMPLETE,
            )

    class Channel:
        texts: list[str] = []

        def send_text(self, text: str, *, idempotency_key: str) -> str:
            del idempotency_key
            self.texts.append(text)
            return "om_direct"

        def is_online(self) -> bool:
            return True

    store = StateStore(config.service.database)
    service = ProgressService(config.path)
    service.config = config
    service.store = store
    service.codec = CorrelationCodec(b"d" * 32)
    channel = Channel()
    service.channel = channel  # type: ignore[assignment]
    summarizer = DirectSummarizer()
    service.summarizer = summarizer  # type: ignore[assignment]
    event = TurnEvent(
        "thread-direct",
        "turn-direct",
        "completed",
        final_message="已经处理好了。",
    )

    started = time.monotonic()
    service._send_event(event)

    assert time.monotonic() - started < 0.5
    assert channel.texts == []
    delivery = store.claim_notification_summary(event.dedupe_key)
    assert delivery is not None
    service._process_notification_summary(delivery)
    assert len(channel.texts) == 1
    assert "已经处理好了" in channel.texts[0]
    assert summarizer.model_calls == 1
    assert store.notification_summary_delivered(event.dedupe_key) is True
    assert store.was_processed(event.dedupe_key) is True
    store.close()


def test_intermediate_completed_turns_are_silent_without_placeholder(
    tmp_path: Path,
) -> None:
    base = make_config(tmp_path)
    config = replace(base, messaging=replace(base.messaging, backend="feishu"))

    class SilentSummarizer:
        def immediate_report(self, _event):
            return None

        def summarize(self, _event, *, wait=None):
            del wait
            return ProgressReport(
                "继续施工",
                "内部检查点完成，后续工作仍在继续。",
                NotificationReason.SILENT,
            )

    class Channel:
        texts: list[str] = []

        def send_text(self, text: str, *, idempotency_key: str) -> str:
            del idempotency_key
            self.texts.append(text)
            return "om_unexpected"

        def is_online(self) -> bool:
            return True

    store = StateStore(config.service.database)
    service = ProgressService(config.path)
    service.config = config
    service.store = store
    service.codec = CorrelationCodec(b"v" * 32)
    channel = Channel()
    service.channel = channel  # type: ignore[assignment]
    service.summarizer = SilentSummarizer()  # type: ignore[assignment]

    events = tuple(
        TurnEvent(
            "thread-auto-goal",
            f"turn-auto-{index}",
            "completed",
            final_message="继续验证，当前只是内部检查点。",
        )
        for index in range(3)
    )
    for event in events:
        service._send_event(event)
        assert channel.texts == []
        delivery = store.claim_notification_summary(event.dedupe_key)
        assert delivery is not None
        service._process_notification_summary(delivery)

    assert channel.texts == []
    assert all(store.was_processed(event.dedupe_key) for event in events)
    assert store.pending_notification_summary_count() == 0
    assert store.pending_notification_texts() == ()
    store.close()


def test_completed_action_request_sends_exactly_one_message_without_placeholder(
    tmp_path: Path,
) -> None:
    base = make_config(tmp_path)
    config = replace(base, messaging=replace(base.messaging, backend="feishu"))

    class ActionSummarizer(TwoStageSummarizer):
        def summarize(self, _event, *, wait=None):
            del wait
            self.calls += 1
            return ProgressReport(
                "待人工测试",
                "需要你处理：请打开候选窗口确认一次视觉结果。",
                NotificationReason.USER_ACTION_REQUIRED,
            )

    class Channel:
        texts: list[str] = []

        def send_text(self, text: str, *, idempotency_key: str) -> str:
            assert idempotency_key.startswith("notification-summary:")
            self.texts.append(text)
            return "om_action"

        def is_online(self) -> bool:
            return True

    store = StateStore(config.service.database)
    service = ProgressService(config.path)
    service.config = config
    service.store = store
    service.codec = CorrelationCodec(b"a" * 32)
    channel = Channel()
    service.channel = channel  # type: ignore[assignment]
    service.summarizer = ActionSummarizer()  # type: ignore[assignment]
    event = TurnEvent(
        "thread-action",
        "turn-action",
        "completed",
        final_message="候选已经准备好，需要用户打开确认。",
    )

    service._send_event(event)
    assert channel.texts == []
    delivery = store.claim_notification_summary(event.dedupe_key)
    assert delivery is not None
    service._process_notification_summary(delivery)

    assert len(channel.texts) == 1
    assert "需要你处理" in channel.texts[0]
    assert "详细摘要整理中" not in channel.texts[0]
    assert store.was_processed(event.dedupe_key)
    store.close()


def test_feishu_complex_turn_sends_only_final_persisted_summary_and_images(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = make_config(tmp_path)
    config = replace(base, messaging=replace(base.messaging, backend="feishu"))
    artifact = make_generated_artifact(
        tmp_path,
        thread_id="thread-two-stage",
        item_id="item-two-stage",
    )

    class Channel:
        def __init__(self) -> None:
            self.texts: list[tuple[str, str]] = []
            self.images: list[tuple[bytes, str]] = []

        def send_text(self, text: str, *, idempotency_key: str) -> str:
            self.texts.append((text, idempotency_key))
            return f"om_text_{len(self.texts)}"

        def send_image(self, data: bytes, *, idempotency_key: str) -> str:
            self.images.append((data, idempotency_key))
            return "om_image_1"

        def is_online(self) -> bool:
            return True

    store = StateStore(config.service.database)
    bind_calls: list[tuple[str, tuple[str, ...]]] = []
    original_bind = store.bind_channel_messages

    def capture_bind(
        event_key: str,
        message_ids: tuple[str, ...] | list[str],
        *,
        allow_additional: bool = True,
    ) -> None:
        normalized_ids = tuple(message_ids)
        bind_calls.append((event_key, normalized_ids))
        original_bind(
            event_key,
            normalized_ids,
            allow_additional=allow_additional,
        )

    monkeypatch.setattr(store, "bind_channel_messages", capture_bind)
    service = ProgressService(config.path)
    service.config = config
    service.store = store
    service.codec = CorrelationCodec(b"s" * 32)
    channel = Channel()
    service.channel = channel  # type: ignore[assignment]
    artifact_queue = install_artifact_queue(service)
    summarizer = TwoStageSummarizer()
    service.summarizer = summarizer  # type: ignore[assignment]
    event = TurnEvent(
        "thread-two-stage",
        "turn-two-stage",
        "completed",
        title="两阶段通知测试",
        final_message="这是需要模型整理的复杂长答复。" * 20,
        generated_images=(artifact,),
    )

    service._send_event(event)

    assert channel.texts == []
    assert channel.images == []
    assert summarizer.calls == 0
    assert store.was_processed(event.dedupe_key) is False
    assert store.pending_notification_summary_count() == 1
    assert store.pending_notification_media(event_key=event.dedupe_key) == ()

    delivery = store.claim_notification_summary(event.dedupe_key)
    assert delivery is not None
    service._process_notification_summary(delivery)
    artifact_queue.drain_once()

    assert summarizer.calls == 1
    assert len(channel.texts) == 1
    assert "后台摘要已整理完成" in channel.texts[0][0]
    assert channel.texts[0][1] == f"notification-summary:{event.dedupe_key}"
    assert len(channel.images) == 1
    assert store.notification_summary_delivered(event.dedupe_key) is True
    assert store.was_processed(event.dedupe_key) is True
    assert (event.dedupe_key, ("om_text_1",)) not in bind_calls
    assert store.code_for_channel_message("om_text_1") is not None
    artifact_rows = artifact_queue._rows(
        "SELECT media_kind, state, message_ids_json "
        "FROM artifact_file_deliveries WHERE event_key=? ORDER BY ordinal",
        (event.dedupe_key,),
    )
    assert len(artifact_rows) == 1
    assert artifact_rows[0]["media_kind"] == "image"
    assert artifact_rows[0]["state"] == "done"
    assert "om_image_1" in artifact_rows[0]["message_ids_json"]
    artifact_queue.stop()
    store.close()


def test_feishu_heartbeat_dont_notify_never_reserves_placeholder(tmp_path: Path) -> None:
    base = make_config(tmp_path)
    config = replace(base, messaging=replace(base.messaging, backend="feishu"))

    class Channel:
        texts: list[str] = []

        def send_text(self, text: str, *, idempotency_key: str) -> str:
            del idempotency_key
            self.texts.append(text)
            return "om_unexpected"

        def is_online(self) -> bool:
            return True

    store = StateStore(config.service.database)
    service = ProgressService(config.path)
    service.config = config
    service.store = store
    service.codec = CorrelationCodec(b"h" * 32)
    channel = Channel()
    service.channel = channel  # type: ignore[assignment]
    service.summarizer = TwoStageSummarizer()  # type: ignore[assignment]
    event = TurnEvent(
        "thread-heartbeat-silent",
        "turn-heartbeat-silent",
        "completed",
        final_message=(
            "<heartbeat><automation_id>synthetic</automation_id>"
            "<decision>DONT_NOTIFY</decision><message>无预警</message></heartbeat>"
        ),
    )

    service._send_event(event)

    assert channel.texts == []
    assert store.was_processed(event.dedupe_key) is True
    assert store.notification_summary_delivery(event.dedupe_key) is None
    store.close()


def test_prepared_summary_restarts_without_repeating_model_call(tmp_path: Path) -> None:
    base = make_config(tmp_path)
    config = replace(base, messaging=replace(base.messaging, backend="feishu"))

    class Channel:
        def __init__(self) -> None:
            self.texts: list[tuple[str, str]] = []

        def send_text(self, text: str, *, idempotency_key: str) -> str:
            self.texts.append((text, idempotency_key))
            return f"om_restart_{len(self.texts)}"

        def is_online(self) -> bool:
            return True

    first_store = StateStore(config.service.database)
    first = ProgressService(config.path)
    first.config = config
    first.store = first_store
    first.codec = CorrelationCodec(b"r" * 32)
    first_channel = Channel()
    first.channel = first_channel  # type: ignore[assignment]
    first.summarizer = TwoStageSummarizer()  # type: ignore[assignment]
    event = TurnEvent(
        "thread-summary-restart",
        "turn-summary-restart",
        "completed",
        final_message="复杂答复" * 40,
    )
    first._send_event(event)
    delivery = first_store.claim_notification_summary(event.dedupe_key)
    assert delivery is not None
    assert first_store.prepare_notification_summary(
        event.dedupe_key, "冻结后的详细摘要"
    )
    assert first_store.release_notification_summary(
        event.dedupe_key,
        "synthetic_restart_before_submit",
        next_attempt_at=0,
    )
    first_store.close()

    class MustNotSummarize(TwoStageSummarizer):
        def summarize(self, _event, *, wait=None):
            del wait
            raise AssertionError("已准备摘要在重启后不应再次调用模型")

    reopened = StateStore(config.service.database)
    second = ProgressService(config.path)
    second.config = config
    second.store = reopened
    second.codec = CorrelationCodec(b"r" * 32)
    second_channel = Channel()
    second.channel = second_channel  # type: ignore[assignment]
    second.summarizer = MustNotSummarize()  # type: ignore[assignment]
    resumed = reopened.claim_notification_summary(event.dedupe_key)
    assert resumed is not None
    second._process_notification_summary(resumed)

    assert len(second_channel.texts) == 1
    assert second_channel.texts[0][0] == "冻结后的详细摘要"
    assert reopened.was_processed(event.dedupe_key) is True
    reopened.close()


def test_slow_background_summary_does_not_block_later_placeholders(
    tmp_path: Path,
) -> None:
    base = make_config(tmp_path)
    config = replace(base, messaging=replace(base.messaging, backend="feishu"))
    started = threading.Event()
    release = threading.Event()

    class SlowSummarizer(TwoStageSummarizer):
        def summarize(self, _event, *, wait=None):
            del wait
            self.calls += 1
            started.set()
            assert release.wait(3)
            return ProgressReport(
                "完成",
                "慢摘要完成",
                NotificationReason.CONVERSATION_COMPLETE,
            )

    class Channel:
        def __init__(self) -> None:
            self.texts: list[str] = []
            self.lock = threading.Lock()

        def send_text(self, text: str, *, idempotency_key: str) -> str:
            del idempotency_key
            with self.lock:
                self.texts.append(text)
                return f"om_slow_{len(self.texts)}"

        def is_online(self) -> bool:
            return True

    store = StateStore(config.service.database)
    service = ProgressService(config.path)
    service.config = config
    service.store = store
    service.codec = CorrelationCodec(b"w" * 32)
    channel = Channel()
    service.channel = channel  # type: ignore[assignment]
    service.summarizer = SlowSummarizer()  # type: ignore[assignment]
    service.summary_thread = threading.Thread(target=service._summary_worker)
    service.summary_thread.start()

    first = TurnEvent(
        "thread-slow-first",
        "turn-slow-first",
        "completed",
        final_message="第一条复杂长答复" * 30,
    )
    second = TurnEvent(
        "thread-slow-second",
        "turn-slow-second",
        "completed",
        final_message="第二条复杂长答复" * 30,
    )
    service._send_event(first)
    assert started.wait(2)
    before = time.monotonic()
    service._send_event(second)
    elapsed = time.monotonic() - before

    assert elapsed < 0.5
    assert channel.texts == []

    release.set()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not store.was_processed(second.dedupe_key):
        time.sleep(0.01)
    service.stop_event.set()
    service._summary_wakeup.set()
    service.summary_thread.join(timeout=2)
    assert not service.summary_thread.is_alive()
    assert store.was_processed(first.dedupe_key) is True
    assert store.was_processed(second.dedupe_key) is True
    assert len(channel.texts) == 2
    store.close()


@pytest.mark.parametrize(
    ("failure", "expected_state", "processed"),
    [
        (
            FeishuSendRejectedError(
                code="synthetic_rejected", raw_code=40001, retryable=False
            ),
            "rejected",
            False,
        ),
        (FeishuSendError("synthetic unknown"), "uncertain", True),
    ],
)
def test_background_summary_rejected_retries_but_unknown_freezes(
    tmp_path: Path,
    failure: BaseException,
    expected_state: str,
    processed: bool,
) -> None:
    base = make_config(tmp_path)
    config = replace(base, messaging=replace(base.messaging, backend="feishu"))

    class Channel:
        calls = 0
        image_calls = 0

        def send_text(self, text: str, *, idempotency_key: str) -> str:
            del text, idempotency_key
            self.calls += 1
            raise failure

        def is_online(self) -> bool:
            return True

        def send_image(self, data: bytes, *, idempotency_key: str) -> str:
            del data, idempotency_key
            self.image_calls += 1
            return "om_summary_terminal_image"

    store = StateStore(config.service.database)
    service = ProgressService(config.path)
    service.config = config
    service.store = store
    service.codec = CorrelationCodec(b"x" * 32)
    channel = Channel()
    service.channel = channel  # type: ignore[assignment]
    artifact_queue = install_artifact_queue(service)
    service.summarizer = TwoStageSummarizer()  # type: ignore[assignment]
    artifact = make_generated_artifact(
        tmp_path,
        thread_id=f"thread-{expected_state}",
        item_id=f"item-{expected_state}",
    )
    event = TurnEvent(
        f"thread-{expected_state}",
        f"turn-{expected_state}",
        "completed",
        final_message="复杂通知答复" * 40,
        generated_images=(artifact,),
    )
    service._send_event(event)
    delivery = store.claim_notification_summary(event.dedupe_key)
    assert delivery is not None
    service._process_notification_summary(delivery)
    if expected_state == "uncertain":
        artifact_queue.drain_once()

    state = store.notification_summary_delivery(event.dedupe_key)
    assert state is not None
    assert state.state == expected_state
    assert store.was_processed(event.dedupe_key) is processed
    if expected_state == "rejected":
        assert store.claim_notification_summary(event.dedupe_key) is None
        assert channel.image_calls == 0
    else:
        assert store.notification_summary_terminal(event.dedupe_key) is True
        assert channel.image_calls == 1
    artifact_rows = artifact_queue._rows(
        "SELECT media_kind, state, message_ids_json "
        "FROM artifact_file_deliveries WHERE event_key=? ORDER BY ordinal",
        (event.dedupe_key,),
    )
    assert len(artifact_rows) == 1
    assert artifact_rows[0]["media_kind"] == "image"
    if expected_state == "uncertain":
        assert artifact_rows[0]["state"] == "done"
        assert "om_summary_terminal_image" in artifact_rows[0]["message_ids_json"]
    else:
        assert artifact_rows[0]["state"] == "capture_pending"
    artifact_queue.stop()
    store.close()


def test_summary_worker_stop_releases_claim_before_external_submit(
    tmp_path: Path,
) -> None:
    base = make_config(tmp_path)
    config = replace(base, messaging=replace(base.messaging, backend="feishu"))

    class Channel:
        calls = 0

        def send_text(self, text: str, *, idempotency_key: str) -> str:
            del text, idempotency_key
            self.calls += 1
            return "om_stop_parent"

        def is_online(self) -> bool:
            return True

    store = StateStore(config.service.database)
    service = ProgressService(config.path)
    service.config = config
    service.store = store
    service.codec = CorrelationCodec(b"z" * 32)
    channel = Channel()
    service.channel = channel  # type: ignore[assignment]
    service.summarizer = TwoStageSummarizer()  # type: ignore[assignment]
    event = TurnEvent(
        "thread-stop-summary",
        "turn-stop-summary",
        "completed",
        final_message="复杂答复" * 50,
    )
    service._send_event(event)
    delivery = store.claim_notification_summary(event.dedupe_key)
    assert delivery is not None
    service.stop_event.set()

    with pytest.raises(ServiceStopping):
        service._process_notification_summary(delivery)

    state = store.notification_summary_delivery(event.dedupe_key)
    assert state is not None
    assert state.claimed_at is None
    assert state.submitted_at is None
    assert store.was_processed(event.dedupe_key) is False
    assert channel.calls == 0
    store.close()


def test_complex_hook_is_consumed_only_after_detailed_summary_terminal(
    tmp_path: Path,
) -> None:
    base = make_config(tmp_path)
    config = replace(base, messaging=replace(base.messaging, backend="feishu"))
    thread = ThreadRecord("thread-hook-summary", title="Hook摘要测试", cwd="D:/work")
    turn = TurnRecord(
        "thread-hook-summary",
        "turn-hook-summary",
        ThreadStatus.COMPLETED,
        final_agent_item_id="item-hook-summary",
        final_message="需要后台整理的复杂Hook答复" * 30,
    )

    class Codex:
        def snapshot(self, _thread_id: str) -> ThreadSnapshot:
            return ThreadSnapshot(thread, turn, ThreadStatus.COMPLETED, True, True)

    class Channel:
        calls = 0

        def send_text(self, text: str, *, idempotency_key: str) -> str:
            del text, idempotency_key
            self.calls += 1
            return f"om_hook_summary_{self.calls}"

        def is_online(self) -> bool:
            return True

    store = StateStore(config.service.database)
    service = ProgressService(config.path)
    service.config = config
    service.store = store
    service.codec = CorrelationCodec(b"k" * 32)
    service.channel = Channel()  # type: ignore[assignment]
    service.summarizer = TwoStageSummarizer()  # type: ignore[assignment]
    service.codex_store = Codex()  # type: ignore[assignment]
    service._last_wechat_health = time.monotonic()
    service._refresh_monitor_registry = lambda _config: None  # type: ignore[method-assign]
    service._selected_threads = lambda _config: {thread.thread_id: thread}  # type: ignore[method-assign]
    store.enqueue_hook_payload(
        {
            "type": "agent-turn-complete",
            "thread-id": thread.thread_id,
            "turn-id": turn.turn_id,
            "last-assistant-message": turn.final_message,
        }
    )

    service._poll_once(config)
    assert store.pending_hook_count() == 1
    assert store.was_processed(
        f"{thread.thread_id}:{turn.turn_id}:completed"
    ) is False

    delivery = store.claim_notification_summary(
        f"{thread.thread_id}:{turn.turn_id}:completed"
    )
    assert delivery is not None
    service._process_notification_summary(delivery)
    service._poll_once(config)

    assert store.pending_hook_count() == 0
    assert store.was_processed(
        f"{thread.thread_id}:{turn.turn_id}:completed"
    ) is True
    store.close()


class FakeAccountReader:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.read_calls = 0

    def read(self) -> AccountRateLimits:
        self.read_calls += 1
        if self.fail:
            raise CodexAccountError("测试额度不可用")
        return AccountRateLimits(
            windows=(
                RateLimitWindow(
                    bucket_id="codex",
                    bucket_name="Codex",
                    window_name="每周额度",
                    used_percent=60.0,
                    available_percent=40.0,
                    duration_minutes=7 * 24 * 60,
                    resets_at=None,
                ),
            ),
            reset_credit_count=2,
            reset_credits=(),
        )


def test_progress_notification_appends_fresh_quota_at_the_end(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store = StateStore(config.service.database)
    channel = FakeWechat()
    service = ProgressService(config.path)
    service.config = config
    service.codec = CorrelationCodec(b"q" * 32)
    service.store = store
    service.channel = channel  # type: ignore[assignment]
    service.summarizer = FakeSummarizer()  # type: ignore[assignment]
    reader = FakeAccountReader()
    service.account_reader = reader  # type: ignore[assignment]

    service._send_event(
        TurnEvent(
            "thread-quota",
            "turn-quota",
            "completed",
            title="额度附注测试",
            final_message="完成。",
        )
    )

    assert reader.read_calls == 1
    assert channel.messages[0].endswith(
        "Codex 每周额度：40%\n剩余重置卡：2 张"
    )
    store.close()


def test_progress_notification_survives_quota_read_failure(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store = StateStore(config.service.database)
    channel = FakeWechat()
    service = ProgressService(config.path)
    service.config = config
    service.codec = CorrelationCodec(b"u" * 32)
    service.store = store
    service.channel = channel  # type: ignore[assignment]
    service.summarizer = FakeSummarizer()  # type: ignore[assignment]
    reader = FakeAccountReader(fail=True)
    service.account_reader = reader  # type: ignore[assignment]

    service._send_event(
        TurnEvent(
            "thread-quota-failure",
            "turn-quota-failure",
            "completed",
            title="额度失败测试",
            final_message="主进度必须送达。",
        )
    )

    assert reader.read_calls == 1
    assert "自动测试通过。" in channel.messages[0]
    assert channel.messages[0].endswith(
        "Codex 每周额度：暂时无法读取\n剩余重置卡：暂时无法读取"
    )
    store.close()


def test_progress_notification_requeues_after_summary_model_retries(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    store = StateStore(config.service.database)
    channel = FakeWechat()

    class BrokenSummarizer:
        calls = 0

        def summarize(self, _event, *, wait=None):
            del wait
            self.calls += 1
            raise RuntimeError("摘要模型暂时不可用")

    summarizer = BrokenSummarizer()
    service = ProgressService(config.path)
    service.config = config
    service.codec = CorrelationCodec(b"f" * 32)
    service.store = store
    service.channel = channel  # type: ignore[assignment]
    service.summarizer = summarizer  # type: ignore[assignment]
    event = TurnEvent(
        "thread-summary-fallback",
        "turn-summary-fallback",
        "completed",
        title="摘要失败测试",
        final_message=(
            "补丁已经生成并通过测试。\n"
            "完整路径：D:/Software/Tool/share/hotfix.zip\n"
            + "安装说明很长。" * 100
        ),
    )

    service._send_event(event)

    assert summarizer.calls == 5
    assert channel.messages == []
    assert store.was_processed(event.dedupe_key) is False
    delivery = store.notification_summary_delivery(event.dedupe_key)
    assert delivery is not None
    judgment = store.notification_judgment(event.dedupe_key)
    assert judgment is not None
    assert judgment.model_error == "semantic_summary_unavailable"
    assert judgment.model_attempts == 5
    store.close()


def test_dont_notify_heartbeat_is_consumed_without_side_effects(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store = StateStore(config.service.database)
    channel = FakeWechat()

    class ForbiddenSummarizer:
        def summarize(self, *_args, **_kwargs):
            raise AssertionError("DONT_NOTIFY 心跳不得调用摘要器")

    service = ProgressService(config.path)
    service.config = config
    service.codec = CorrelationCodec(b"h" * 32)
    service.store = store
    service.channel = channel  # type: ignore[assignment]
    service.summarizer = ForbiddenSummarizer()  # type: ignore[assignment]
    reader = FakeAccountReader()
    service.account_reader = reader  # type: ignore[assignment]
    event = TurnEvent(
        "thread-heartbeat",
        "turn-silent",
        "completed",
        title="Codex Reset Alert",
        final_message=(
            "<heartbeat><automation_id>codex</automation_id>"
            "<decision>DONT_NOTIFY</decision><message>无预警</message></heartbeat>"
        ),
    )

    service._send_event(event)
    service._send_event(event)

    assert channel.messages == []
    assert reader.read_calls == 0
    assert store.was_processed(event.dedupe_key)
    store.close()


def test_notify_heartbeat_only_exposes_readable_message(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store = StateStore(config.service.database)
    channel = FakeWechat()

    class CapturingSummarizer:
        def __init__(self) -> None:
            self.messages: list[str] = []

        def summarize(self, event, *, wait=None):
            del wait
            self.messages.append(event.final_message)
            return ProgressReport(
                "完成",
                event.final_message,
                NotificationReason.CONVERSATION_COMPLETE,
            )

    summarizer = CapturingSummarizer()
    service = ProgressService(config.path)
    service.config = config
    service.codec = CorrelationCodec(b"n" * 32)
    service.store = store
    service.channel = channel  # type: ignore[assignment]
    service.summarizer = summarizer  # type: ignore[assignment]
    event = TurnEvent(
        "thread-heartbeat",
        "turn-notify",
        "completed",
        title="Codex Reset Alert",
        final_message=(
            "<heartbeat><automation_id>codex</automation_id>"
            "<decision>NOTIFY</decision>"
            "<message>检测到本周额度即将刷新。</message></heartbeat>"
        ),
    )

    service._send_event(event)

    assert summarizer.messages == ["检测到本周额度即将刷新。"]
    assert len(channel.messages) == 1
    assert "检测到本周额度即将刷新。" in channel.messages[0]
    assert "<heartbeat>" not in channel.messages[0]
    assert "automation_id" not in channel.messages[0]
    assert "NOTIFY" not in channel.messages[0]
    store.close()


def test_waiting_input_notification_is_structural_and_uses_no_summary_model(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    store = StateStore(config.service.database)
    channel = FakeWechat()

    class ForbiddenSummarizer:
        def summarize(self, *_args, **_kwargs):
            raise AssertionError("结构化待输入通知不得调用摘要模型")

    service = ProgressService(config.path)
    service.config = config
    service.codec = CorrelationCodec(b"i" * 32)
    service.store = store
    service.channel = channel  # type: ignore[assignment]
    service.summarizer = ForbiddenSummarizer()  # type: ignore[assignment]
    event = desktop_attention_event(
        {
            "cursor": "cursor-zero-model",
            "thread": {
                "id": "thread-1",
                "status": {
                    "type": "active",
                    "activeFlags": ["waitingOnUserInput"],
                },
            },
            "latestTurn": {"id": "turn-goal", "status": "inProgress"},
            "latestAssistantMessage": {"text": "请选择继续开发或先测试。"},
        },
        ThreadRecord("thread-1", title="飞书机器人开发"),
    )
    assert event is not None

    service._send_event(event)
    service._send_event(event)

    assert len(channel.messages) == 1
    assert "当前进度：路线选择" in channel.messages[0]
    assert "请选择继续开发或先测试" in channel.messages[0]
    store.close()


def test_send_event_is_deduplicated_and_quote_is_one_time(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    codec = CorrelationCodec(b"k" * 32)
    store = StateStore(config.service.database)
    wechat = FakeWechat()
    service = ProgressService(config.path)
    service.config = config
    service.codec = codec
    service.store = store
    service.wechat = wechat  # type: ignore[assignment]
    service.summarizer = FakeSummarizer()  # type: ignore[assignment]
    event = hook_payload_to_event(
        {
            "type": "agent-turn-complete",
            "thread-id": "thread-1",
            "turn-id": "turn-1",
            "last-assistant-message": "实现完成。",
        },
        ThreadRecord("thread-1", title="示例"),
    )
    service._send_event(event)
    service._send_event(event)
    assert len(wechat.messages) == 1
    code = CorrelationCodec.extract(wechat.messages[0])
    assert code is not None

    quote = QuoteMessage("主号", "继续", wechat.messages[0], "小号", message_id="m1", message_hash="h1")
    service._on_quote(quote)
    first = service.reply_queue.get_nowait()
    assert first is not None and first.thread_id == "thread-1" and first.reply_text == "继续"
    service._on_quote(quote)
    with pytest.raises(queue.Empty):
        service.reply_queue.get_nowait()
    store.close()


def test_channel_message_id_routes_reply_after_service_restart(tmp_path: Path) -> None:
    """飞书只返回父消息 ID 时，也能在重启后找到正确 Codex 对话。"""

    base = make_config(tmp_path)
    config = replace(
        base,
        messaging=replace(base.messaging, backend="feishu"),
        feishu=replace(base.feishu, target_open_id="ou_owner"),
    )
    codec = CorrelationCodec(b"f" * 32)

    class FakeChannel(FakeWechat):
        def send_text(self, text: str, *, idempotency_key: str) -> str:
            assert idempotency_key.startswith("notification:")
            self.messages.append(text)
            return "om_notice"

    first_store = StateStore(config.service.database)
    first = ProgressService(config.path)
    first.config = config
    first.codec = codec
    first.store = first_store
    first.channel = FakeChannel()  # type: ignore[assignment]
    first.summarizer = FakeSummarizer()  # type: ignore[assignment]
    event = hook_payload_to_event(
        {
            "type": "agent-turn-complete",
            "thread-id": "thread-1",
            "turn-id": "turn-feishu",
            "last-assistant-message": "等待手机回复。",
        },
        ThreadRecord("thread-1", title="飞书关联"),
    )
    first._send_event(event)
    sent = first.channel.messages[0]  # type: ignore[attr-defined]
    lines = sent.split("\n\n")
    assert len(lines) == 4
    assert lines[:3] == [
        "对话名称：飞书关联",
        "当前进度：完成",
        "自动测试通过。",
    ]
    assert lines[3].startswith("本条消息时间：")
    assert "回复编号" not in sent
    first_store.close()

    reopened = StateStore(config.service.database)
    second = ProgressService(config.path)
    second.config = config
    second.codec = codec
    second.store = reopened
    second._on_channel_reply(
        ChannelReply(
            sender_id="ou_owner",
            content="继续",
            reply_to_message_id="om_notice",
            message_id="om_reply",
            chat_id="oc_private",
        )
    )
    job = second.reply_queue.get_nowait()
    assert job is not None
    assert job.thread_id == "thread-1"
    assert job.reply_text == "继续"
    receipt = second.receipt_queue.get_nowait()
    assert receipt is not None and receipt.received is True
    assert receipt.idempotency_key.startswith("reply-receipt:")
    assert "已排队" in receipt.details
    assert "继续引用同一条进度汇报" in receipt.details
    assert "已送达" not in receipt.details
    reopened.close()


def test_bot_menu_with_or_without_chat_id_skips_image_staging_and_reaches_management(
    tmp_path: Path,
) -> None:
    """菜单不论是否带 chat_id，都不能参与两步图片暂存。"""

    base = make_config(tmp_path)
    config = replace(
        base,
        messaging=replace(base.messaging, backend="feishu"),
        feishu=replace(base.feishu, target_open_id="ou_owner"),
    )

    class NoImageStageStore:
        def staged_image_reply(self, *_args, **_kwargs):
            raise AssertionError("菜单事件不得访问图片暂存")

    class Management:
        def accepts(self, message: ChannelReply) -> bool:
            return message.source_kind == "bot_menu"

    service = object.__new__(ProgressService)
    service.config = config
    service.store = NoImageStageStore()
    service.management = Management()
    service.management_queue = queue.Queue()
    service.stop_event = threading.Event()
    service._fatal = None

    first = ChannelReply(
        sender_id="ou_owner",
        content="功能中心",
        message_id="feishu-menu-1",
        source_kind="bot_menu",
    )
    second = replace(first, message_id="feishu-menu-2")
    third = replace(first, message_id="feishu-menu-3", chat_id="oc_private")
    service._on_channel_reply(first)
    service._on_channel_reply(second)
    service._on_channel_reply(third)

    assert service.management_queue.get_nowait() == first
    assert service.management_queue.get_nowait() == second
    assert service.management_queue.get_nowait() == third
    with pytest.raises(queue.Empty):
        service.management_queue.get_nowait()
    assert service._fatal is None
    assert service.stop_event.is_set() is False


def test_empty_chat_id_from_non_menu_is_rejected_without_service_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """其它不完整外部入站只丢弃并告警，不能让回调线程停服。"""

    base = make_config(tmp_path)
    config = replace(base, messaging=replace(base.messaging, backend="feishu"))

    class NoImageStageStore:
        def staged_image_reply(self, *_args, **_kwargs):
            raise AssertionError("非法空 chat_id 不得访问图片暂存")

    service = object.__new__(ProgressService)
    service.config = config
    service.store = NoImageStageStore()
    service.management = None
    service.management_queue = queue.Queue()
    service.stop_event = threading.Event()
    service._fatal = None

    warnings: list[str] = []
    monkeypatch.setattr(
        service_module.LOGGER,
        "warning",
        lambda message, *args, **kwargs: warnings.append(str(message)),
    )
    service._on_channel_reply(
        ChannelReply(
            sender_id="ou_owner",
            content="不完整入站",
            message_id="malformed-empty-chat",
            source_kind="message",
        )
    )

    assert service.management_queue.empty()
    assert service._fatal is None
    assert service.stop_event.is_set() is False
    assert any("缺少有效会话标识" in message for message in warnings)


def test_same_progress_message_accepts_three_ordered_turn_replies_and_deduplicates(
    tmp_path: Path,
) -> None:
    base = make_config(tmp_path)
    config = replace(
        base,
        messaging=replace(base.messaging, backend="feishu"),
        feishu=replace(base.feishu, target_open_id="ou_owner"),
    )
    codec = CorrelationCodec(b"m" * 32)
    store = StateStore(config.service.database)
    event = TurnEvent("thread-multi", "turn-parent", "completed", title="多轮回复")
    code = codec.issue()
    store.reserve_notification(event, code, "通知", 72)
    store.bind_channel_message(event.dedupe_key, "om_parent")
    store.mark_sent(event.dedupe_key)
    service = ProgressService(config.path)
    service.config = config
    service.codec = codec
    service.store = store

    messages = (
        ChannelReply("ou_owner", "同样正文", "om_parent", "om_1", "oc_private"),
        ChannelReply("ou_owner", "同样正文", "om_parent", "om_2", "oc_private"),
        ChannelReply("ou_owner", "第三条", "om_parent", "om_3", "oc_private"),
    )
    for message in messages:
        service._on_channel_reply(message)
    jobs = [service.reply_queue.get_nowait() for _ in range(3)]
    receipts = [service.receipt_queue.get_nowait() for _ in range(3)]

    assert [job.thread_id for job in jobs] == ["thread-multi"] * 3
    assert [job.reply_text for job in jobs] == ["同样正文", "同样正文", "第三条"]
    assert len({job.code for job in jobs}) == 3
    assert all("已排队" in receipt.details for receipt in receipts)
    assert all("继续引用同一条进度汇报" in receipt.details for receipt in receipts)
    assert [row[0] for row in store.pending_turn_replies()] == [job.code for job in jobs]
    parent = store._connection.execute(
        "SELECT consumed_at FROM notifications WHERE code=?", (code,)
    ).fetchone()
    assert parent[0] is None

    # 同一飞书事件重放不产生第4个子投递或回执。
    service._on_channel_reply(messages[1])
    assert service.reply_queue.empty()
    assert service.receipt_queue.empty()
    assert len(store.pending_turn_replies()) == 3
    store.close()


def test_expired_progress_message_gives_explicit_cannot_continue_receipt(
    tmp_path: Path,
) -> None:
    base = make_config(tmp_path)
    config = replace(
        base,
        messaging=replace(base.messaging, backend="feishu"),
        feishu=replace(base.feishu, target_open_id="ou_owner"),
    )
    codec = CorrelationCodec(b"x" * 32)
    store = StateStore(config.service.database)
    event = TurnEvent("thread-expired", "turn-parent", "completed")
    code = codec.issue()
    store.reserve_notification(event, code, "通知", 72)
    store.bind_channel_message(event.dedupe_key, "om_expired")
    store.mark_sent(event.dedupe_key)
    with store._connection:
        store._connection.execute(
            "UPDATE notifications SET expires_at=1 WHERE code=?", (code,)
        )
    service = ProgressService(config.path)
    service.config = config
    service.codec = codec
    service.store = store
    service._on_channel_reply(
        ChannelReply("ou_owner", "继续", "om_expired", "om_reply", "oc_private")
    )

    assert service.reply_queue.empty()
    receipt = service.receipt_queue.get_nowait()
    assert receipt.received is False
    assert "已过期" in receipt.details
    assert "重新查询会话" in receipt.details
    store.close()


def test_delivered_receipt_is_persistent_and_marked_after_send(
    tmp_path: Path,
) -> None:
    base = make_config(tmp_path)
    config = replace(
        base,
        messaging=replace(base.messaging, backend="feishu"),
        feishu=replace(base.feishu, target_open_id="ou_owner"),
    )
    codec = CorrelationCodec(b"z" * 32)
    store = StateStore(config.service.database)
    event = TurnEvent("thread-receipt", "turn-parent", "completed")
    code = codec.issue()
    store.reserve_notification(event, code, "通知", 72)
    store.mark_sent(event.dedupe_key)
    delivery = store.enqueue_turn_reply(
        code,
        "om_delivery",
        "fingerprint-delivery",
        codec,
        reply_text="继续",
        receipt_required=True,
    )
    assert delivery is not None
    assert store.claim_turn_reply(delivery.delivery_id)
    store.mark_reply_delivered(delivery.delivery_id)
    assert store.pending_delivery_receipts() == [
        (delivery.delivery_id, "fingerprint-delivery")
    ]

    service = ProgressService(config.path)
    service.config = config
    service.store = store
    service.channel = FakeWechat()  # type: ignore[assignment]
    service._queue_delivered_reply_receipt(
        delivery.delivery_id, "fingerprint-delivery"
    )
    service.receipt_queue.put(None)
    service._receipt_worker()

    assert len(service.channel.messages) == 1  # type: ignore[attr-defined]
    assert "已追加" in service.channel.messages[0]  # type: ignore[attr-defined]
    assert "继续引用同一条进度汇报" in service.channel.messages[0]  # type: ignore[attr-defined]
    assert store.pending_delivery_receipts() == []
    store.close()


def test_generated_image_is_sent_as_preview_and_image_reply_routes_to_task(tmp_path: Path) -> None:
    base = make_config(tmp_path)
    config = replace(
        base,
        messaging=replace(base.messaging, backend="feishu"),
        feishu=replace(base.feishu, target_open_id="ou_owner"),
    )
    image_path = tmp_path / "generated_images" / "thread-image" / "item-image.png"
    image_path.parent.mkdir(parents=True)
    original = b"\x89PNG\r\n\x1a\nexact-original-image"
    image_path.write_bytes(original)
    artifact = GeneratedImageArtifact(
        item_id="item-image",
        path=str(image_path),
        mime_type="image/png",
        sha256=hashlib.sha256(original).hexdigest(),
        size=len(original),
        file_name="generated.png",
    )

    class FakeImageChannel:
        def __init__(self) -> None:
            self.texts: list[tuple[str, str]] = []
            self.images: list[tuple[bytes, str]] = []

        def send_text(self, text: str, *, idempotency_key: str) -> str:
            self.texts.append((text, idempotency_key))
            return "om_notice"

        def send_image(
            self,
            data: bytes,
            *,
            idempotency_key: str,
        ) -> str:
            self.images.append((data, idempotency_key))
            return "om_image"

        def is_online(self) -> bool:
            return True

    store = StateStore(config.service.database)
    service = ProgressService(config.path)
    service.config = config
    service.codec = CorrelationCodec(b"i" * 32)
    service.store = store
    channel = FakeImageChannel()
    service.channel = channel  # type: ignore[assignment]
    artifact_queue = install_artifact_queue(service)
    service.summarizer = FakeSummarizer()  # type: ignore[assignment]
    service._send_event(
        TurnEvent(
            "thread-image",
            "turn-image",
            "completed",
            title="生图任务",
            final_message="图片生成完成。",
            generated_images=(artifact,),
        )
    )
    artifact_queue.drain_once()

    artifact_rows = artifact_queue._rows(
        "SELECT media_kind, state, message_ids_json "
        "FROM artifact_file_deliveries WHERE event_key=? ORDER BY ordinal",
        ("thread-image:turn-image:completed",),
    )
    assert len(artifact_rows) == 1
    assert artifact_rows[0]["media_kind"] == "image"
    assert artifact_rows[0]["state"] == "done"
    assert "om_image" in artifact_rows[0]["message_ids_json"]
    artifact_row = artifact_queue._rows(
        "SELECT * FROM artifact_file_deliveries WHERE event_key=?",
        ("thread-image:turn-image:completed",),
    )[0]
    assert channel.images == [(original, artifact_queue._key(artifact_row))]
    assert channel.texts[0][1] == "notification:thread-image:turn-image:completed"
    service._on_channel_reply(
        ChannelReply(
            sender_id="ou_owner",
            content="继续修改这张图",
            reply_to_message_id="om_image",
            message_id="om_reply_image",
            chat_id="oc_private",
        )
    )
    job = service.reply_queue.get_nowait()
    assert job.thread_id == "thread-image"
    assert job.reply_text == "继续修改这张图"
    artifact_queue.stop()
    store.close()


def test_oversized_generated_image_is_proportionally_reduced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pixels = random.Random(42).randbytes(96 * 48 * 3)
    image = Image.frombytes("RGB", (96, 48), pixels)
    source = io.BytesIO()
    image.save(source, format="PNG")
    limit = len(source.getvalue()) // 2
    monkeypatch.setattr(service_module, "FEISHU_IMAGE_DIRECT_MAX_BYTES", limit)

    transformed, changed = _prepare_feishu_image(source.getvalue(), "image/png")

    assert changed is True
    assert len(transformed) <= limit
    result = Image.open(io.BytesIO(transformed))
    assert result.width / result.height == pytest.approx(2.0, abs=0.05)


def test_completed_hook_with_text_enriches_same_turn_generated_image(
    tmp_path: Path,
) -> None:
    base = make_config(tmp_path)
    config = replace(base, messaging=replace(base.messaging, backend="feishu"))
    artifact = make_generated_artifact(
        tmp_path, thread_id="thread-hook-image", item_id="item-hook-image"
    )
    thread = ThreadRecord("thread-hook-image", title="合成生图任务")

    class StructuredStore:
        def snapshot(self, thread_id: str) -> ThreadSnapshot:
            assert thread_id == "thread-hook-image"
            turn = TurnRecord(
                thread_id,
                "turn-hook-image",
                ThreadStatus.COMPLETED,
                final_agent_item_id="final-hook-image",
                final_message="数据库正文",
                generated_images=(artifact,),
            )
            return ThreadSnapshot(thread, turn, ThreadStatus.COMPLETED, True, True)

    class Channel:
        def __init__(self) -> None:
            self.texts: list[str] = []
            self.images: list[bytes] = []

        def send_text(self, text: str, *, idempotency_key: str) -> str:
            del idempotency_key
            self.texts.append(text)
            return "om_hook_text"

        def send_image(self, data: bytes, *, idempotency_key: str) -> str:
            del idempotency_key
            self.images.append(data)
            return "om_hook_image"

        def is_online(self) -> bool:
            return True

    state = StateStore(config.service.database)
    state.enqueue_hook_payload(
        {
            "type": "agent-turn-complete",
            "thread-id": "thread-hook-image",
            "turn-id": "turn-hook-image",
            "last-assistant-message": "Hook 已有正文",
        }
    )
    service = ProgressService(config.path)
    service.config = config
    service.store = state
    service.codec = CorrelationCodec(b"h" * 32)
    channel = Channel()
    service.channel = channel  # type: ignore[assignment]
    artifact_queue = install_artifact_queue(service)
    service.codex_store = StructuredStore()  # type: ignore[assignment]
    service.summarizer = FakeSummarizer()  # type: ignore[assignment]
    service._selected_threads = lambda _config: {"thread-hook-image": thread}  # type: ignore[method-assign]

    service._poll_once(config)
    artifact_queue.drain_once()

    assert len(channel.texts) == 1
    assert channel.images == [Path(artifact.path).read_bytes()]
    assert state.pending_hook_count() == 0
    rows = artifact_queue._rows(
        "SELECT media_kind, state, message_ids_json "
        "FROM artifact_file_deliveries WHERE thread_id=? ORDER BY ordinal",
        ("thread-hook-image",),
    )
    assert len(rows) == 1
    assert rows[0]["media_kind"] == "image"
    assert rows[0]["state"] == "done"
    assert "om_hook_image" in rows[0]["message_ids_json"]
    assert state.stats()["notification_media_deliveries"] == 0
    artifact_queue.stop()
    state.close()


def test_late_structured_image_adds_media_without_repeating_processed_text(
    tmp_path: Path,
) -> None:
    base = make_config(tmp_path)
    config = replace(base, messaging=replace(base.messaging, backend="feishu"))
    artifact = make_generated_artifact(
        tmp_path, thread_id="thread-late-image", item_id="item-late-image"
    )

    class Channel:
        def __init__(self) -> None:
            self.texts = 0
            self.images = 0

        def send_text(self, text: str, *, idempotency_key: str) -> str:
            del text, idempotency_key
            self.texts += 1
            return "om_late_text"

        def send_image(self, data: bytes, *, idempotency_key: str) -> str:
            del data, idempotency_key
            self.images += 1
            return "om_late_image"

        def is_online(self) -> bool:
            return True

    state = StateStore(config.service.database)
    service = ProgressService(config.path)
    service.config = config
    service.store = state
    service.codec = CorrelationCodec(b"l" * 32)
    channel = Channel()
    service.channel = channel  # type: ignore[assignment]
    artifact_queue = install_artifact_queue(service)
    service.summarizer = FakeSummarizer()  # type: ignore[assignment]
    base_event = TurnEvent(
        "thread-late-image",
        "turn-late-image",
        "completed",
        final_message="同一轮先到的正文",
    )

    service._send_event(base_event)
    service._send_event(replace(base_event, generated_images=(artifact,)))
    artifact_queue.drain_once()

    assert channel.texts == 1
    assert channel.images == 1
    rows = artifact_queue._rows(
        "SELECT media_kind, state, message_ids_json "
        "FROM artifact_file_deliveries WHERE event_key=? ORDER BY ordinal",
        (base_event.dedupe_key,),
    )
    assert len(rows) == 1
    assert rows[0]["media_kind"] == "image"
    assert rows[0]["state"] == "done"
    assert "om_late_image" in rows[0]["message_ids_json"]
    assert state.stats()["notification_media_deliveries"] == 0
    artifact_queue.stop()
    state.close()


def test_partial_multi_image_delivery_resumes_after_restart_without_duplicates(
    tmp_path: Path,
) -> None:
    base = make_config(tmp_path)
    config = replace(base, messaging=replace(base.messaging, backend="feishu"))
    artifacts = tuple(
        make_generated_artifact(
            tmp_path,
            thread_id="thread-restart-images",
            item_id=f"item-restart-{index}",
            body=f"image-{index}".encode(),
        )
        for index in (1, 2)
    )

    class FirstChannel:
        def __init__(self) -> None:
            self.image_calls = 0

        def send_text(self, text: str, *, idempotency_key: str) -> str:
            del text, idempotency_key
            return "om_restart_text"

        def send_image(self, data: bytes, *, idempotency_key: str) -> str:
            del data, idempotency_key
            self.image_calls += 1
            if self.image_calls == 2:
                raise FeishuSendNotSubmittedError("offline before submit")
            return "om_restart_image_1"

        def is_online(self) -> bool:
            return True

    event = TurnEvent(
        "thread-restart-images",
        "turn-restart-images",
        "completed",
        final_message="两张图完成",
        generated_images=artifacts,
    )
    state = StateStore(config.service.database)
    service = ProgressService(config.path)
    service.config = config
    service.store = state
    service.codec = CorrelationCodec(b"r" * 32)
    first = FirstChannel()
    service.channel = first  # type: ignore[assignment]
    artifact_queue = install_artifact_queue(service)
    service.summarizer = FakeSummarizer()  # type: ignore[assignment]
    service._send_event(event)
    artifact_queue.drain_once()
    assert first.image_calls == 2
    first_rows = artifact_queue._rows(
        "SELECT ordinal, state, message_ids_json "
        "FROM artifact_file_deliveries WHERE event_key=? ORDER BY ordinal",
        (event.dedupe_key,),
    )
    assert [row["ordinal"] for row in first_rows] == [0, 1]
    assert first_rows[0]["state"] == "done"
    assert first_rows[1]["state"] == "pending"
    artifact_queue.stop()
    state.close()

    class RecoveredChannel:
        def __init__(self) -> None:
            self.images = 0

        def send_image(self, data: bytes, *, idempotency_key: str) -> str:
            del data, idempotency_key
            self.images += 1
            return "om_restart_image_2"

        def is_online(self) -> bool:
            return True

    recovered = StateStore(config.service.database)
    with recovered._lock, recovered._connection:
        recovered._connection.execute(
            "UPDATE artifact_file_deliveries SET next_at=0 "
            "WHERE state='pending'"
        )
    resumed = ProgressService(config.path)
    resumed.config = config
    resumed.store = recovered
    resumed.codec = CorrelationCodec(b"r" * 32)
    resumed.channel = RecoveredChannel()  # type: ignore[assignment]
    recovered_queue = install_artifact_queue(resumed)
    recovered_queue.drain_once()

    assert resumed.channel.images == 1  # type: ignore[attr-defined]
    rows = recovered_queue._rows(
        "SELECT ordinal, state, message_ids_json "
        "FROM artifact_file_deliveries WHERE event_key=? ORDER BY ordinal",
        (event.dedupe_key,),
    )
    assert [row["ordinal"] for row in rows] == [0, 1]
    assert all(row["state"] == "done" for row in rows)
    assert "om_restart_image_1" in rows[0]["message_ids_json"]
    assert "om_restart_image_2" in rows[1]["message_ids_json"]
    recovered_queue.stop()
    recovered.close()


def test_unknown_image_result_freezes_only_that_item_and_service_continues(
    tmp_path: Path,
) -> None:
    base = make_config(tmp_path)
    config = replace(base, messaging=replace(base.messaging, backend="feishu"))
    artifacts = tuple(
        make_generated_artifact(
            tmp_path,
            thread_id="thread-unknown-image",
            item_id=f"item-unknown-image-{index}",
            body=f"unknown-{index}".encode(),
        )
        for index in (1, 2)
    )

    class Channel:
        def __init__(self) -> None:
            self.image_calls = 0
            self.text_calls = 0

        def send_text(self, text: str, *, idempotency_key: str) -> str:
            del text, idempotency_key
            self.text_calls += 1
            return f"om_unknown_text_{self.text_calls}"

        def send_image(self, data: bytes, *, idempotency_key: str) -> str:
            del data, idempotency_key
            self.image_calls += 1
            if self.image_calls == 1:
                raise FeishuSendError("result unknown")
            return "om_after_unknown_image"

        def is_online(self) -> bool:
            return True

    state = StateStore(config.service.database)
    service = ProgressService(config.path)
    service.config = config
    service.store = state
    service.codec = CorrelationCodec(b"u" * 32)
    channel = Channel()
    service.channel = channel  # type: ignore[assignment]
    artifact_queue = install_artifact_queue(service)
    service.summarizer = FakeSummarizer()  # type: ignore[assignment]
    service._send_event(
        TurnEvent(
            "thread-unknown-image",
            "turn-unknown-image",
            "completed",
            final_message="图片完成",
            generated_images=artifacts,
        )
    )
    service._send_event(
        TurnEvent(
            "thread-after-unknown",
            "turn-after-unknown",
            "completed",
            final_message="后续普通任务",
        )
    )
    artifact_queue.drain_once()
    artifact_queue.drain_once()

    assert channel.image_calls == 2
    assert channel.text_calls == 3  # 两条正文 + 一条结果未知提示
    assert service._fatal is None
    rows = artifact_queue._rows(
        "SELECT ordinal, state, notice_state, message_ids_json, notice_ids_json "
        "FROM artifact_file_deliveries WHERE event_key=? ORDER BY ordinal",
        ("thread-unknown-image:turn-unknown-image:completed",),
    )
    assert [row["ordinal"] for row in rows] == [0, 1]
    assert rows[0]["state"] == "uncertain"
    assert rows[0]["notice_state"] == "done"
    assert rows[0]["message_ids_json"] == "[]"
    assert "om_unknown_text_3" in rows[0]["notice_ids_json"]
    assert rows[1]["state"] == "done"
    assert "om_after_unknown_image" in rows[1]["message_ids_json"]
    artifact_queue.stop()
    state.close()


def test_mobile_image_then_plain_text_are_combined_for_same_task(tmp_path: Path) -> None:
    base = make_config(tmp_path)
    config = replace(
        base,
        messaging=replace(base.messaging, backend="feishu"),
        feishu=replace(base.feishu, target_open_id="ou_owner"),
    )
    codec = CorrelationCodec(b"m" * 32)
    store = StateStore(config.service.database)
    event = TurnEvent("thread-mobile", "turn-mobile", "completed", title="手机发图")
    code = codec.issue()
    store.reserve_notification(event, code, "测试通知", 72)
    store.bind_channel_message(event.dedupe_key, "om_notice")
    store.mark_sent(event.dedupe_key)

    cache = config.service.database.parent / "feishu-media"
    cache.mkdir(parents=True, exist_ok=True)
    image_path = cache / "mobile.png"
    data = b"\x89PNG\r\n\x1a\nmobile-two-step"
    image_path.write_bytes(data)
    attachment = ChannelAttachment(
        str(image_path.resolve()),
        "image/png",
        hashlib.sha256(data).hexdigest(),
        len(data),
    )
    service = ProgressService(config.path)
    service.config = config
    service.codec = codec
    service.store = store

    service._on_channel_reply(
        ChannelReply(
            sender_id="ou_owner",
            content="",
            reply_to_message_id="om_notice",
            message_id="om_image",
            chat_id="oc_private",
            attachments=(attachment,),
        )
    )

    with pytest.raises(queue.Empty):
        service.reply_queue.get_nowait()
    staged = store.staged_image_reply("ou_owner", "oc_private")
    assert staged is not None and len(staged["attachments"]) == 1
    staged_receipt = service.receipt_queue.get_nowait()
    assert staged_receipt is not None and staged_receipt.received is True
    assert "10 分钟内直接发送文字说明" in staged_receipt.details
    assert ".发送" in staged_receipt.details
    assert ".取消" in staged_receipt.details

    service._on_channel_reply(
        ChannelReply(
            sender_id="ou_owner",
            content="把背景改成白色",
            message_id="om_text",
            chat_id="oc_private",
        )
    )

    job = service.reply_queue.get_nowait()
    assert job.thread_id == "thread-mobile"
    assert job.reply_text.startswith("把背景改成白色\n\n用户通过飞书发送了以下图片")
    assert str(image_path.resolve()) in job.reply_text
    assert store.staged_image_reply("ou_owner", "oc_private") is None
    forwarded_receipt = service.receipt_queue.get_nowait()
    assert forwarded_receipt is not None and forwarded_receipt.received is True
    store.close()


def test_mobile_rich_post_image_and_text_bypass_staging(tmp_path: Path) -> None:
    base = make_config(tmp_path)
    config = replace(
        base,
        messaging=replace(base.messaging, backend="feishu"),
        feishu=replace(base.feishu, target_open_id="ou_owner"),
    )
    codec = CorrelationCodec(b"r" * 32)
    store = StateStore(config.service.database)
    event = TurnEvent("thread-rich", "turn-rich", "completed", title="合并发图")
    code = codec.issue()
    store.reserve_notification(event, code, "测试通知", 72)
    store.bind_channel_message(event.dedupe_key, "om_notice")
    store.mark_sent(event.dedupe_key)

    cache = config.service.database.parent / "feishu-media"
    cache.mkdir(parents=True, exist_ok=True)
    image_path = cache / "combined.png"
    data = b"\x89PNG\r\n\x1a\ncombined-message"
    image_path.write_bytes(data)
    attachment = ChannelAttachment(
        str(image_path.resolve()),
        "image/png",
        hashlib.sha256(data).hexdigest(),
        len(data),
    )
    service = ProgressService(config.path)
    service.config = config
    service.codec = codec
    service.store = store

    service._on_channel_reply(
        ChannelReply(
            sender_id="ou_owner",
            content="请直接分析这张图片",
            reply_to_message_id="om_notice",
            message_id="om_combined",
            chat_id="oc_private",
            attachments=(attachment,),
        )
    )

    job = service.reply_queue.get_nowait()
    assert job.thread_id == "thread-rich"
    assert job.reply_text.startswith(
        "请直接分析这张图片\n\n用户通过飞书发送了以下图片"
    )
    assert str(image_path.resolve()) in job.reply_text
    assert store.staged_image_reply("ou_owner", "oc_private") is None
    receipt = service.receipt_queue.get_nowait()
    assert receipt is not None and receipt.received is True
    assert "已暂存" not in receipt.details
    assert "已排队" in receipt.details
    assert "继续引用同一条进度汇报" in receipt.details
    assert "已送达" not in receipt.details
    store.close()


def test_mobile_staged_image_dot_cancel_does_not_reach_codex(tmp_path: Path) -> None:
    base = make_config(tmp_path)
    config = replace(
        base,
        messaging=replace(base.messaging, backend="feishu"),
        feishu=replace(base.feishu, target_open_id="ou_owner"),
    )
    codec = CorrelationCodec(b"c" * 32)
    store = StateStore(config.service.database)
    event = TurnEvent("thread-cancel", "turn-cancel", "completed")
    code = codec.issue()
    store.reserve_notification(event, code, "测试通知", 72)
    store.bind_channel_message(event.dedupe_key, "om_notice")
    store.mark_sent(event.dedupe_key)
    cache = config.service.database.parent / "feishu-media"
    cache.mkdir(parents=True, exist_ok=True)
    image_path = cache / "cancel.jpg"
    data = b"\xff\xd8\xffcancel"
    image_path.write_bytes(data)
    service = ProgressService(config.path)
    service.config = config
    service.codec = codec
    service.store = store
    service._on_channel_reply(
        ChannelReply(
            sender_id="ou_owner",
            content="",
            reply_to_message_id="om_notice",
            message_id="om_image_cancel",
            chat_id="oc_private",
            attachments=(
                ChannelAttachment(
                    str(image_path.resolve()),
                    "image/jpeg",
                    hashlib.sha256(data).hexdigest(),
                    len(data),
                ),
            ),
        )
    )
    service.receipt_queue.get_nowait()
    service._on_channel_reply(
        ChannelReply(
            sender_id="ou_owner",
            content=".取消",
            message_id="om_cancel",
            chat_id="oc_private",
        )
    )
    assert store.staged_image_reply("ou_owner", "oc_private") is None
    with pytest.raises(queue.Empty):
        service.reply_queue.get_nowait()
    cancelled = service.receipt_queue.get_nowait()
    assert cancelled is not None and "没有向 Codex 发送" in cancelled.details
    assert store.peek_reply(code, codec) == ("thread-cancel", "turn-cancel", "turn")

    service._on_channel_reply(
        ChannelReply(
            sender_id="ou_owner",
            content="",
            reply_to_message_id="om_notice",
            message_id="om_image_send",
            chat_id="oc_private",
            attachments=(
                ChannelAttachment(
                    str(image_path.resolve()),
                    "image/jpeg",
                    hashlib.sha256(data).hexdigest(),
                    len(data),
                ),
            ),
        )
    )
    service.receipt_queue.get_nowait()
    service._on_channel_reply(
        ChannelReply(
            sender_id="ou_owner",
            content=".发送",
            message_id="om_send",
            chat_id="oc_private",
        )
    )
    image_only = service.reply_queue.get_nowait()
    assert image_only.thread_id == "thread-cancel"
    assert image_only.reply_text.startswith(
        "用户通过飞书发送了以下图片，请直接查看图片并结合当前任务处理："
    )
    assert ".发送" not in image_only.reply_text
    assert str(image_path.resolve()) in image_only.reply_text
    assert store.staged_image_reply("ou_owner", "oc_private") is None
    store.close()


def test_permanent_image_permission_error_does_not_stop_or_repeat_in_same_run(
    tmp_path: Path,
) -> None:
    base = make_config(tmp_path)
    config = replace(
        base,
        messaging=replace(base.messaging, backend="feishu"),
        feishu=replace(base.feishu, target_open_id="ou_owner"),
    )
    image_path = (
        tmp_path / "generated_images" / "thread-permission" / "item-permission.png"
    )
    image_path.parent.mkdir(parents=True)
    original = b"\x89PNG\r\n\x1a\npermission-test-image"
    image_path.write_bytes(original)
    artifact = GeneratedImageArtifact(
        item_id="item-permission",
        path=str(image_path),
        mime_type="image/png",
        sha256=hashlib.sha256(original).hexdigest(),
        size=len(original),
        file_name="generated.png",
    )

    class PermissionDeniedChannel:
        def __init__(self) -> None:
            self.texts: list[tuple[str, str]] = []
            self.image_calls = 0

        def send_text(self, text: str, *, idempotency_key: str) -> str:
            self.texts.append((text, idempotency_key))
            return "om_notice" if len(self.texts) == 1 else "om_permission_help"

        def send_image(
            self,
            data: bytes,
            *,
            idempotency_key: str,
        ) -> str:
            del data, idempotency_key
            self.image_calls += 1
            raise FeishuSendRejectedError(
                code="permission_denied",
                raw_code=99991672,
                retryable=False,
            )

        def is_online(self) -> bool:
            return True

    event = TurnEvent(
        "thread-permission",
        "turn-permission",
        "completed",
        title="图片权限测试",
        final_message="图片生成完成。",
        generated_images=(artifact,),
    )
    store = StateStore(config.service.database)
    service = ProgressService(config.path)
    service.config = config
    service.codec = CorrelationCodec(b"p" * 32)
    service.store = store
    channel = PermissionDeniedChannel()
    service.channel = channel  # type: ignore[assignment]
    artifact_queue = install_artifact_queue(service)
    service.summarizer = FakeSummarizer()  # type: ignore[assignment]

    service._send_event(event)
    service._send_event(event)
    artifact_queue.drain_once()
    artifact_queue.drain_once()

    assert channel.image_calls == 1
    assert len(channel.texts) == 2
    assert store.was_processed(event.dedupe_key) is True
    assert service._fatal is None
    rows = artifact_queue._rows(
        "SELECT state, reason, notice_state, message_ids_json, notice_ids_json "
        "FROM artifact_file_deliveries WHERE event_key=?",
        (event.dedupe_key,),
    )
    assert len(rows) == 1
    assert rows[0]["state"] == "rejected"
    assert rows[0]["reason"] == "permission_denied:99991672"
    assert rows[0]["notice_state"] == "done"
    assert rows[0]["message_ids_json"] == "[]"
    assert "om_permission_help" in rows[0]["notice_ids_json"]
    assert store.stats()["notification_media_deliveries"] == 0
    assert store.code_for_channel_message("om_notice") is not None
    assert store.code_for_channel_message("om_permission_help") is not None
    artifact_queue.stop()
    store.close()


def test_receipt_worker_sends_without_waiting_for_codex(tmp_path: Path) -> None:
    base = make_config(tmp_path)
    config = replace(
        base,
        messaging=replace(base.messaging, backend="feishu"),
        feishu=replace(base.feishu, target_open_id="ou_owner"),
    )

    class FakeReceiptChannel:
        def __init__(self) -> None:
            self.messages: list[tuple[str, str]] = []

        def send_text(self, text: str, *, idempotency_key: str) -> str:
            self.messages.append((text, idempotency_key))
            return "om_receipt"

        def is_online(self) -> bool:
            return True

    service = ProgressService(config.path)
    service.config = config
    channel = FakeReceiptChannel()
    service.channel = channel  # type: ignore[assignment]
    service.receipt_queue.put(
        ReplyReceiptJob(
            received=True,
            details="已进入原 Codex 会话，接下来继续执行。",
            idempotency_key="reply-receipt:fp",
        )
    )
    service.receipt_queue.put(None)
    service._receipt_worker()

    assert channel.messages == [
        (
            "消息状态：已收到\n\n回复信息：已进入原 Codex 会话，接下来继续执行。",
            "reply-receipt:fp",
        )
    ]


def test_feishu_offline_receipt_is_requeued_without_service_fatal(tmp_path: Path) -> None:
    base = make_config(tmp_path)
    config = replace(
        base,
        messaging=replace(base.messaging, backend="feishu"),
        feishu=replace(base.feishu, target_open_id="ou_owner"),
    )

    class FlakyReceiptChannel:
        def __init__(self) -> None:
            self.calls = 0

        def send_text(self, _text: str, *, idempotency_key: str) -> str:
            del idempotency_key
            self.calls += 1
            if self.calls == 1:
                raise MessageChannelOfflineError("synthetic offline")
            service.stop_event.set()
            return "om_receipt"

    service = ProgressService(config.path)
    service.config = config
    channel = FlakyReceiptChannel()
    service.channel = channel  # type: ignore[assignment]
    service.receipt_queue.put(
        ReplyReceiptJob(
            received=True,
            details="已进入原 Codex 会话。",
            idempotency_key="reply-receipt:offline",
        )
    )
    service._receipt_worker()

    assert channel.calls == 2
    assert service._fatal is None


def test_feishu_offline_poll_keeps_service_running_and_defers_outbox(tmp_path: Path, monkeypatch) -> None:
    config = make_config(tmp_path)
    service = ProgressService(config.path)
    service.store = object()  # type: ignore[assignment]
    service.codex_store = object()  # type: ignore[assignment]
    channel = FeishuMessageChannel(
        app_id="cli_test",
        app_secret="secret",
        target_open_id="ou_owner",
        media_cache_dir=tmp_path / "media",
        sdk_factory=lambda *_args: None,
    )
    service.channel = channel
    monkeypatch.setattr(service, "_refresh_monitor_registry", lambda _config: None)
    monkeypatch.setattr(service, "_selected_threads", lambda _config: {})

    service._poll_once(config)

    assert service._fatal is None
    assert service.stop_event.is_set() is False
    assert service._channel_offline_reported is True


def test_feishu_offline_channel_callback_is_not_global_fatal(tmp_path: Path) -> None:
    service = ProgressService(make_config(tmp_path).path)

    service._on_channel_error(MessageChannelOfflineError("synthetic offline"))

    assert service._fatal is None
    assert service.stop_event.is_set() is False


class BusyCodexStore:
    def __init__(self, status: ThreadStatus = ThreadStatus.IN_PROGRESS) -> None:
        self.calls = 0
        self.result = status

    def status(self, _thread_id: str):
        self.calls += 1
        return self.result


class FailingCodexStore:
    """模拟结构化数据库读取失败，验证服务不会把异常当作空选择。"""

    def __init__(self) -> None:
        self.calls = 0

    def get_thread(self, _thread_id: str):
        return None

    def require_readable(self, _operation: str = "") -> None:
        self.calls += 1
        raise CodexStoreReadError("读取 Codex 状态", ("state:read",))

    def snapshot(self, _thread_id: str):  # pragma: no cover - 选择阶段已失败
        return ThreadSnapshot(None, None, ThreadStatus.UNKNOWN, False, False, ("state:read",))


class MissingThreadCodexStore:
    """模拟数据库可读但配置的显式 thread ID 不存在。"""

    def __init__(self) -> None:
        self.snapshot_calls = 0

    def get_thread(self, _thread_id: str):
        return None

    def require_readable(self, _operation: str = "") -> None:
        return None

    def select_threads(self, **_kwargs):
        return []

    def snapshot(self, _thread_id: str):
        self.snapshot_calls += 1
        return ThreadSnapshot(None, None, ThreadStatus.UNKNOWN, True, True)


def test_missing_explicit_monitor_id_fails_closed_before_polling(tmp_path: Path) -> None:
    """可读数据库中不存在的显式 ID 不得被静默当成 unknown。"""

    config = make_config(tmp_path)
    service = ProgressService(config.path)
    service.config = config
    service.store = StateStore(config.service.database)
    missing = MissingThreadCodexStore()
    service.codex_store = missing  # type: ignore[assignment]
    # 跳过与本测试无关的微信健康检查，直接验证监控选择器。
    import time

    service._last_wechat_health = time.monotonic()

    with pytest.raises(ServiceFatalError, match="thread-1"):
        service._poll_once(config)

    assert missing.snapshot_calls == 0
    service.store.close()


@pytest.mark.parametrize(
    ("selector_field", "selector_value", "expected_label"),
    [
        ("titles", "不存在的标题", "监控标题不存在"),
        ("paths", r"D:\\不存在的路径", "监控路径不存在"),
    ],
)
def test_missing_title_or_path_selector_fails_closed(
    tmp_path: Path,
    selector_field: str,
    selector_value: str,
    expected_label: str,
) -> None:
    """标题/路径显式选择器也不能在零匹配时静默运行。"""

    config = make_config(tmp_path)
    selectors = config.codex.selectors
    selectors = replace(
        selectors,
        ids=(),
        titles=(selector_value,) if selector_field == "titles" else (),
        paths=(selector_value,) if selector_field == "paths" else (),
    )
    config = replace(config, codex=replace(config.codex, selectors=selectors))
    service = ProgressService(config.path)
    service.config = config
    service.codex_store = MissingThreadCodexStore()  # type: ignore[assignment]

    with pytest.raises(ServiceFatalError, match=expected_label):
        service._selected_threads(config)


def test_unknown_desktop_thread_stops_after_five_checks(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    service = ProgressService(config.path)
    service.config = config
    service.store = StateStore(config.service.database)
    fake = BusyCodexStore(ThreadStatus.UNKNOWN)
    service.codex_store = fake  # type: ignore[assignment]
    from progress_wx.service import ReplyJob

    with pytest.raises(RetryExhausted):
        service._deliver_reply(ReplyJob("code", "thread-1", "继续", "fp"))
    assert fake.calls == 5
    service.store.close()


def test_active_desktop_without_gateway_is_deferred_before_claim(
    tmp_path: Path, monkeypatch
) -> None:
    """轮次忙不是故障；正文未写入前应保留为可重启恢复的 pending。"""

    config = make_config(tmp_path)
    service = ProgressService(config.path)
    store = StateStore(config.service.database)
    codec = CorrelationCodec(b"d" * 32)
    service.config = config
    service.store = store
    fake = BusyCodexStore(ThreadStatus.IN_PROGRESS)
    service.codex_store = fake  # type: ignore[assignment]
    job = _pending_reply(store, codec, turn_id="busy-turn", reply_text="稍后继续")

    # stdio 默认模式绝不能探测或依赖历史共享 gateway。
    monkeypatch.setattr(
        "progress_wx.service.active_shared_websocket_url",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("stdio 模式不应探测共享 gateway")
        ),
    )

    from progress_wx.service import ReplyDeferred

    with pytest.raises(ReplyDeferred):
        service._deliver_reply(job)
    assert fake.calls == 1
    assert store.pending_turn_replies() == [
        (job.code, "thread-1", "稍后继续", job.fingerprint)
    ]
    assert store.uncertain_turn_replies() == []
    store.close()


def test_desktop_app_tools_accepts_reply_while_target_is_running(
    tmp_path: Path, monkeypatch
) -> None:
    """官方 Desktop 任务工具可在活动 task 中追加回复，且只提交一次正文。"""

    base = make_config(tmp_path)
    config = replace(
        base,
        codex=replace(base.codex, reply_transport="desktop_app_tools"),
    )
    service = ProgressService(config.path)
    store = StateStore(config.service.database)
    codec = CorrelationCodec(b"a" * 32)
    service.config = config
    service.store = store
    service.codex_store = BusyCodexStore(ThreadStatus.IN_PROGRESS)  # type: ignore[assignment]
    job = _pending_reply(store, codec, turn_id="busy-turn", reply_text="飞书回传验收")
    calls: list[tuple] = []

    class Session:
        def send_message(self, thread_id, prompt, *, call_tag):
            calls.append(("send", thread_id, prompt, call_tag))
            return {"content": []}

        def close(self):
            calls.append(("close",))

    class Client:
        def __init__(self, log_dir, **_kwargs):
            calls.append(("client", log_dir))

        def open_verified(self):
            calls.append(("verified",))
            return Session()

    monkeypatch.setattr("progress_wx.service.DesktopAppToolsClient", Client)

    service._deliver_reply(job)

    assert ("send", "thread-1", "飞书回传验收", job.code) in calls
    assert store.pending_turn_replies() == []
    assert store.uncertain_turn_replies() == []
    store.close()


def test_desktop_app_tools_unavailable_before_claim_is_persistently_deferred(
    tmp_path: Path, monkeypatch
) -> None:
    """tools/list 耗尽仍在 claim 前，必须保留 pending 而不是全局 fatal。"""

    base = make_config(tmp_path)
    config = replace(
        base,
        codex=replace(base.codex, reply_transport="desktop_app_tools"),
    )
    service = ProgressService(config.path)
    store = StateStore(config.service.database)
    codec = CorrelationCodec(b"u" * 32)
    service.config = config
    service.store = store
    service.codex_store = BusyCodexStore(ThreadStatus.COMPLETED)  # type: ignore[assignment]
    service._retry_sleep = lambda _delay: None  # type: ignore[method-assign]
    job = _pending_reply(store, codec, turn_id="offline", reply_text="稍后自动继续")
    calls = 0

    class Client:
        def __init__(self, _log_dir, **_kwargs):
            pass

        def open_verified(self):
            nonlocal calls
            calls += 1
            raise DesktopAppToolsUnavailable("desktop offline")

    monkeypatch.setattr("progress_wx.service.DesktopAppToolsClient", Client)

    with pytest.raises(ReplyDeferred, match="工具暂不可用"):
        service._deliver_reply(job)
    assert calls == 5
    assert store.pending_turn_replies() == [
        (job.code, "thread-1", "稍后自动继续", job.fingerprint)
    ]
    assert store.uncertain_turn_replies() == []
    assert service._fatal is None
    store.close()


def test_reply_worker_keeps_service_alive_and_waits_when_desktop_is_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    """长期不可用时每轮均有等待，不能忙循环或设置全局 fatal。"""

    config = make_config(tmp_path)
    service = ProgressService(config.path)
    service.config = replace(config, service=replace(config.service, poll_seconds=1.0))
    job = ReplyJob("offline-code", "thread-1", "稍后继续", "fp")
    waits: list[float] = []

    def defer(_job: ReplyJob) -> None:
        raise ReplyDeferred("Codex Desktop 工具暂不可用")

    monkeypatch.setattr(service, "_deliver_reply", defer)
    monkeypatch.setattr(
        service.stop_event,
        "wait",
        lambda delay: (
            waits.append(float(delay)),
            service.stop_event.set() if len(waits) >= 2 else None,
            service.stop_event.is_set(),
        )[-1],
    )
    service.reply_queue.put(job)
    service._reply_worker()

    assert service._fatal is None
    assert service.stop_event.is_set() is True
    assert waits == [1.0, 1.0]
    assert service.reply_queue.empty()


@pytest.mark.parametrize(
    "failure_type",
    [DesktopAppToolsNotSubmitted, DesktopAppToolsRejected],
)
def test_desktop_explicit_non_acceptance_releases_claim_for_safe_retry(
    tmp_path: Path, monkeypatch, failure_type
) -> None:
    """写入前失败或明确拒绝可以撤销 claim，随后成功时恰好接受一次。"""

    base = make_config(tmp_path)
    config = replace(
        base,
        codex=replace(base.codex, reply_transport="desktop_app_tools"),
    )
    service = ProgressService(config.path)
    store = StateStore(config.service.database)
    codec = CorrelationCodec(b"r" * 32)
    service.config = config
    service.store = store
    service.codex_store = BusyCodexStore(ThreadStatus.COMPLETED)  # type: ignore[assignment]
    job = _pending_reply(store, codec, turn_id="retry", reply_text="只接受一次")
    attempts = 0
    accepted: list[str] = []

    class Session:
        def send_message(self, _thread_id, prompt, *, call_tag):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise failure_type("not accepted")
            accepted.append(prompt)
            return {"content": []}

        def close(self):
            pass

    class Client:
        def __init__(self, _log_dir, **_kwargs):
            pass

        def open_verified(self):
            return Session()

    monkeypatch.setattr("progress_wx.service.DesktopAppToolsClient", Client)

    with pytest.raises(ReplyDeferred, match="明确未接受"):
        service._deliver_reply(job)
    assert store.pending_turn_replies() == [
        (job.code, "thread-1", "只接受一次", job.fingerprint)
    ]
    assert store.uncertain_turn_replies() == []

    service._deliver_reply(job)
    assert attempts == 2
    assert accepted == ["只接受一次"]
    assert store.pending_turn_replies() == []
    assert store.uncertain_turn_replies() == []
    store.close()


def test_desktop_result_unknown_after_claim_remains_fatal_and_uncertain(
    tmp_path: Path, monkeypatch
) -> None:
    """请求已进入写入阶段但结果未知时，必须停机且禁止自动重发。"""

    base = make_config(tmp_path)
    config = replace(
        base,
        codex=replace(base.codex, reply_transport="desktop_app_tools"),
    )
    service = ProgressService(config.path)
    store = StateStore(config.service.database)
    codec = CorrelationCodec(b"x" * 32)
    service.config = config
    service.store = store
    service.codex_store = BusyCodexStore(ThreadStatus.COMPLETED)  # type: ignore[assignment]
    job = _pending_reply(store, codec, turn_id="unknown", reply_text="不要重复")

    class Session:
        def send_message(self, *_args, **_kwargs):
            raise DesktopAppToolsResultUnknown("response lost")

        def close(self):
            pass

    class Client:
        def __init__(self, _log_dir, **_kwargs):
            pass

        def open_verified(self):
            return Session()

    monkeypatch.setattr("progress_wx.service.DesktopAppToolsClient", Client)

    with pytest.raises(ServiceFatalError, match="结果未知"):
        service._deliver_reply(job)
    assert store.pending_turn_replies() == []
    assert store.uncertain_turn_replies() == [job.code]
    store.close()


def test_desktop_delivers_large_original_image_prompt_like_plain_text(
    tmp_path: Path, monkeypatch
) -> None:
    """原图只作为已校验本地路径进入 prompt，不改变 exactly-once 投递语义。"""

    image_path = tmp_path / "original.png"
    with image_path.open("wb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\n")
        handle.seek(1_653_747 - 1)
        handle.write(b"\0")
    prompt = f"请结合图片继续处理。\n\n用户通过飞书发送了以下图片：\n- {image_path}"
    base = make_config(tmp_path)
    config = replace(
        base,
        codex=replace(base.codex, reply_transport="desktop_app_tools"),
    )
    service = ProgressService(config.path)
    store = StateStore(config.service.database)
    codec = CorrelationCodec(b"i" * 32)
    service.config = config
    service.store = store
    service.codex_store = BusyCodexStore(ThreadStatus.COMPLETED)  # type: ignore[assignment]
    job = _pending_reply(store, codec, turn_id="image", reply_text=prompt)
    received: list[str] = []

    class Session:
        def send_message(self, _thread_id, value, *, call_tag):
            received.append(value)
            return {"content": []}

        def close(self):
            pass

    class Client:
        def __init__(self, _log_dir, **_kwargs):
            pass

        def open_verified(self):
            return Session()

    monkeypatch.setattr("progress_wx.service.DesktopAppToolsClient", Client)

    service._deliver_reply(job)
    assert image_path.stat().st_size == 1_653_747
    assert received == [prompt]
    assert store.pending_turn_replies() == []
    assert store.uncertain_turn_replies() == []
    store.close()


def test_stdio_connection_identity_ignores_shared_gateway_changes(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    changed_gateway = replace(
        config,
        codex=replace(
            config.codex,
            shared_websocket_url="ws://127.0.0.1:6249",
            gateway_pid_file=tmp_path / "different-gateway.pid",
            shared_desktop_state_file=tmp_path / "different-desktop.json",
        ),
    )
    assert _codex_connection_identity(config) == _codex_connection_identity(
        changed_gateway
    )

    shared = replace(
        config,
        codex=replace(config.codex, reply_transport="shared_websocket"),
    )
    changed_shared = replace(
        shared,
        codex=replace(shared.codex, shared_websocket_url="ws://127.0.0.1:6249"),
    )
    assert _codex_connection_identity(shared) != _codex_connection_identity(
        changed_shared
    )


def test_deferred_reply_timeout_stops_but_keeps_pending(
    tmp_path: Path, monkeypatch
) -> None:
    """等待超过总时限要告警停机，但绝不能 claim 或丢失正文。"""

    config = make_config(tmp_path)
    service = ProgressService(config.path)
    store = StateStore(config.service.database)
    codec = CorrelationCodec(b"e" * 32)
    service.config = config
    service.store = store
    service.codex_store = BusyCodexStore(ThreadStatus.IN_PROGRESS)  # type: ignore[assignment]
    job = _pending_reply(store, codec, turn_id="long-busy", reply_text="仍需处理")
    consumed_at = store.pending_turn_reply_consumed_at(job.code)
    assert consumed_at is not None
    monkeypatch.setattr(
        "progress_wx.service.time.time",
        lambda: consumed_at + config.codex.reply_timeout_seconds + 1,
    )

    with pytest.raises(ServiceFatalError, match="回复仍安全保留"):
        service._deliver_reply(job)
    assert store.pending_turn_replies() == [
        (job.code, "thread-1", "仍需处理", job.fingerprint)
    ]
    assert store.uncertain_turn_replies() == []
    store.close()


def test_deferred_reply_delivers_once_after_thread_becomes_terminal(
    tmp_path: Path, monkeypatch
) -> None:
    """忙时不 claim；终态后恢复同一任务并只执行一次 turn/start。"""

    config = make_config(tmp_path)
    service = ProgressService(config.path)
    store = StateStore(config.service.database)
    codec = CorrelationCodec(b"f" * 32)
    codex = BusyCodexStore(ThreadStatus.IN_PROGRESS)
    service.config = config
    service.store = store
    service.codex_store = codex  # type: ignore[assignment]
    job = _pending_reply(store, codec, turn_id="busy-then-done", reply_text="继续执行")

    from progress_wx.service import ReplyDeferred

    with pytest.raises(ReplyDeferred):
        service._deliver_reply(job)
    calls: list[tuple[str, str]] = []
    codex.result = ThreadStatus.COMPLETED

    class RPC:
        def __init__(self, *_args, **_kwargs):
            pass

        def resume_thread(self, thread_id, **_kwargs):
            calls.append(("resume", thread_id))
            return {"result": {"thread": {"id": thread_id}}}

        def start_turn(self, thread_id, message, **_kwargs):
            calls.append(("start", message))
            return {"result": {"turn": {"id": "new-turn"}}}

        def listen_event(self, thread_id, *, turn_id, **_kwargs):
            return TurnCompletedEvent(
                thread_id,
                turn_id,
                ThreadStatus.COMPLETED,
                raw_status="completed",
            )

        def close(self):
            pass

    monkeypatch.setattr("progress_wx.service.CodexAppServer", RPC)
    service._deliver_reply(job)

    assert calls.count(("start", "继续执行")) == 1
    assert store.pending_turn_replies() == []
    assert store.uncertain_turn_replies() == []
    store.close()


def test_reply_worker_retries_in_place_and_preserves_followup_order(
    tmp_path: Path,
) -> None:
    """第一条延期时必须原地等待，第二条不得越序投递。"""

    from progress_wx.service import ReplyDeferred

    config = make_config(tmp_path)
    service = ProgressService(config.path)
    service.config = replace(
        config,
        service=replace(config.service, poll_seconds=0.01),
    )
    first = ReplyJob("first", "thread-1", "第一条", "fp-1")
    second = ReplyJob("second", "thread-1", "第二条", "fp-2")
    attempts: list[str] = []
    first_attempts = 0

    def deliver(job: ReplyJob) -> None:
        nonlocal first_attempts
        attempts.append(job.code)
        if job.code == "first":
            first_attempts += 1
            if first_attempts == 1:
                raise ReplyDeferred("busy")
        if job.code == "second":
            service.stop_event.set()

    service._deliver_reply = deliver  # type: ignore[method-assign]
    service.reply_queue.put(first)
    service.reply_queue.put(second)
    service._reply_worker()

    assert service._fatal is None
    assert attempts == ["first", "first", "second"]
    assert service.reply_queue.empty()


def test_concurrent_reply_enqueue_schedules_same_code_once(tmp_path: Path) -> None:
    """启动恢复与渠道回调竞态时，同一持久 code 只能进入队列一次。"""

    service = ProgressService(make_config(tmp_path).path)
    job = ReplyJob("same-code", "thread-1", "继续", "fp")
    barrier = threading.Barrier(3)
    results: list[bool] = []

    def enqueue() -> None:
        barrier.wait()
        results.append(service._enqueue_reply_job(job))

    workers = [threading.Thread(target=enqueue) for _ in range(2)]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join(timeout=2)

    assert sorted(results) == [False, True]
    assert service.reply_queue.get_nowait() == job
    with pytest.raises(queue.Empty):
        service.reply_queue.get_nowait()


def test_poll_does_not_silently_ignore_codex_store_read_errors(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    service = ProgressService(config.path)
    service.config = config
    service.store = StateStore(config.service.database)
    service.codex_store = FailingCodexStore()  # type: ignore[assignment]
    # 跳过与本测试无关的微信健康检查。
    import time

    service._last_wechat_health = time.monotonic()

    with pytest.raises(CodexStoreReadError):
        service._poll_once(config)
    assert service.codex_store.calls == 1  # type: ignore[union-attr]
    service.store.close()


def test_inbound_wechat_identity_error_stops_main_service(tmp_path: Path) -> None:
    service = ProgressService(make_config(tmp_path).path)
    error = FriendVerificationError("目标联系人漂移")

    service._on_wechat_error(error)

    assert service.stop_event.is_set()
    assert service._fatal is error


def test_runtime_reload_allows_monitor_change_but_rejects_wechat_rebind(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    service = ProgressService(config.path)
    service.config = config
    service._initial_wechat_config = config.wechat
    service._initial_service_identity = (
        config.service.database,
        config.service.log_dir,
        config.service.pid_file,
        config.service.log_retention_days,
    )
    service._codex_identity = _codex_connection_identity(config)
    new_selectors = replace(config.codex.selectors, ids=("thread-2", "thread-3"))
    updated = replace(config, codex=replace(config.codex, selectors=new_selectors))
    service.config_source = type("Source", (), {"get": lambda _self: updated})()

    assert service._reload().codex.selectors.ids == ("thread-2", "thread-3")

    rebound = replace(updated, wechat=replace(updated.wechat, target_chat="另一个联系人"))
    service.config_source = type("Source", (), {"get": lambda _self: rebound})()
    with pytest.raises(ServiceFatalError, match="避免串号"):
        service._reload()


def test_runtime_reload_rejects_feishu_secret_file_replacement(tmp_path: Path) -> None:
    base = make_config(tmp_path)
    secret = tmp_path / "feishu-secret.dpapi"
    secret.write_bytes(b"first-protected-value")
    config = replace(
        base,
        messaging=replace(base.messaging, backend="feishu"),
        feishu=replace(
            base.feishu,
            app_id="cli_test",
            app_secret_file=secret,
            target_open_id="ou_target",
        ),
    )
    service = ProgressService(config.path)
    service.config = config
    service._initial_channel_config = (config.messaging, config.feishu)
    service._initial_feishu_secret_identity = _file_identity(secret)
    service._initial_service_identity = (
        config.service.database,
        config.service.log_dir,
        config.service.pid_file,
        config.service.log_retention_days,
    )
    service._codex_identity = _codex_connection_identity(config)
    service.config_source = type("Source", (), {"get": lambda _self: config})()

    replacement = tmp_path / "replacement.dpapi"
    replacement.write_bytes(b"second-protected-value")
    replacement.replace(secret)

    with pytest.raises(ServiceFatalError, match="App Secret 文件已修改"):
        service._reload()


def test_poll_codex_store_read_error_uses_five_attempt_stop_path(
    tmp_path: Path, monkeypatch
) -> None:
    config = make_config(tmp_path)
    service = ProgressService(config.path)
    service.config = config
    service.store = StateStore(config.service.database)
    failing = FailingCodexStore()
    service.codex_store = failing  # type: ignore[assignment]
    import time

    service._last_wechat_health = time.monotonic()
    monkeypatch.setattr(service, "_initialize", lambda: None)
    monkeypatch.setattr(service, "_reload", lambda: config)
    alerts: list[BaseException] = []
    monkeypatch.setattr(service, "_alert", alerts.append)

    assert service.run() == 1
    assert failing.calls == 5
    assert len(alerts) == 1
    assert isinstance(alerts[0], RetryExhausted)
    assert isinstance(alerts[0].last_error, CodexStoreReadError)


def test_alert_logs_channel_failure_and_keeps_local_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    """消息告警失败不得静默吞掉，并仍应调用 Windows 本地弹窗兜底。"""

    config = make_config(tmp_path)
    service = ProgressService(config.path)
    calls: list[object] = []
    warnings: list[str] = []

    class FailingChannel:
        def is_online(self) -> bool:
            return True

        def send_text(self, _text: str, *, idempotency_key: str):
            calls.append(("send", idempotency_key))
            raise RuntimeError("secret-value-must-not-be-logged")

        def stop(self) -> None:
            calls.append("stop")

    service.channel = FailingChannel()  # type: ignore[assignment]
    monkeypatch.setattr("progress_wx.service.os.name", "nt")

    class User32:
        def MessageBoxW(self, _owner, text, title, _flags):
            calls.append(("popup", text, title))
            return 0

    monkeypatch.setattr("progress_wx.service.ctypes.windll.user32", User32())
    monkeypatch.setattr(
        "progress_wx.service.LOGGER.warning",
        lambda message, *args: warnings.append(message % args),
    )
    service._alert(RuntimeError("error-body-must-not-be-logged"))

    assert calls[0][0] == "send"
    assert "stop" in calls
    assert any(item[0] == "popup" for item in calls if isinstance(item, tuple))
    assert any("致命告警无法通过消息渠道发送" in item for item in warnings)
    assert all("secret-value-must-not-be-logged" not in item for item in warnings)


def test_turn_start_response_requires_actual_turn_id() -> None:
    assert started_turn_id({"result": {"turn": {"id": "turn-2"}}}) == "turn-2"
    assert started_turn_id({"result": {"turnId": "turn-3"}}) == "turn-3"
    with pytest.raises(ServiceFatalError):
        started_turn_id({"result": {}})


def test_turn_steer_response_requires_actual_matching_turn_id() -> None:
    assert steered_turn_id({"result": {"turnId": "turn-2"}}) == "turn-2"
    assert steered_turn_id({"result": {"turn_id": "turn-3"}}) == "turn-3"
    with pytest.raises(ServiceFatalError, match="turn/steer"):
        steered_turn_id({"result": {}})


def _pending_reply(
    store: StateStore,
    codec: CorrelationCodec,
    *,
    turn_id: str,
    reply_text: str,
) -> ReplyJob:
    event = hook_payload_to_event(
        {
            "type": "agent-turn-complete",
            "thread-id": "thread-1",
            "turn-id": turn_id,
            "last-assistant-message": "结果",
        },
        ThreadRecord("thread-1", title="示例"),
    )
    code = codec.issue()
    store.reserve_notification(event, code, "回复编号：" + code, 72)
    store.mark_sent(event.dedupe_key)
    fingerprint = "fp-" + turn_id
    delivery = store.enqueue_turn_reply(
        code,
        f"message-{turn_id}",
        fingerprint,
        codec,
        reply_text=reply_text,
    )
    assert delivery is not None and delivery.is_new is True
    return ReplyJob(delivery.delivery_id, "thread-1", reply_text, fingerprint)


def test_active_turn_steer_accepts_exact_turn_and_marks_delivered(tmp_path: Path) -> None:
    service = ProgressService(make_config(tmp_path).path)
    store = StateStore(tmp_path / "state.sqlite")
    codec = CorrelationCodec(b"t" * 32)
    service.store = store
    job = _pending_reply(store, codec, turn_id="old-turn", reply_text="补充要求")
    calls: list[tuple] = []

    class RPC:
        def steer_turn(self, thread_id, expected_turn_id, message, **_kwargs):
            calls.append((thread_id, expected_turn_id, message))
            return {"result": {"turnId": expected_turn_id}}

    assert service._steer_active_reply(
        RPC(),  # type: ignore[arg-type]
        job,
        "active-turn",
        timeout_seconds=5,
        on_server_request=lambda _request: None,
    ) is True
    assert calls == [("thread-1", "active-turn", "补充要求")]
    assert store.uncertain_turn_replies() == []
    assert store.pending_turn_replies() == []
    store.close()


def test_auto_monitor_discovery_baselines_only_first_existing_top_level_tasks(
    monkeypatch, tmp_path: Path
) -> None:
    config = make_config(tmp_path)
    manual = ThreadRecord(
        "thread-1", "手动任务", updated_at_ms=1_990_000, thread_source="user"
    )
    existing_auto = ThreadRecord(
        "thread-auto", "已有自动任务", updated_at_ms=1_995_000, thread_source="user"
    )
    internal = ThreadRecord(
        "thread-sub", "内部任务", updated_at_ms=1_999_000, thread_source="subagent"
    )

    class DiscoveryStore:
        def __init__(self):
            self.records = [manual, existing_auto, internal]

        def get_thread(self, thread_id: str):
            return next((item for item in self.records if item.thread_id == thread_id), None)

        def select_threads(self, **kwargs):
            if "title" in kwargs:
                return [item for item in self.records if item.title == kwargs["title"]]
            if "cwd" in kwargs:
                return [item for item in self.records if item.cwd == kwargs["cwd"]]
            return list(self.records)

        def require_readable(self, _operation: str):
            return None

        def snapshot(self, thread_id: str):
            record = self.get_thread(thread_id)
            turn = TurnRecord(
                thread_id,
                f"turn-{thread_id}",
                ThreadStatus.COMPLETED,
                final_agent_item_id=f"item-{thread_id}",
                final_message="已完成",
            )
            return ThreadSnapshot(record, turn, ThreadStatus.COMPLETED, True, True)

    monkeypatch.setattr("progress_wx.service.time.time", lambda: 2_000)
    state = StateStore(config.service.database)
    codex = DiscoveryStore()
    service = ProgressService(config.path)
    service.store = state
    service.codex_store = codex  # type: ignore[assignment]
    try:
        service._refresh_monitor_registry(config)
        subscriptions = {
            item["thread_id"]: item["origin"] for item in state.monitor_subscriptions(now=2_000)
        }
        assert subscriptions == {"thread-1": "manual", "thread-auto": "auto"}
        assert state.was_processed("thread-auto:turn-thread-auto:completed")
        assert "thread-sub" not in subscriptions

        new_auto = ThreadRecord(
            "thread-new", "刚创建任务", updated_at_ms=2_001_000, thread_source="user"
        )
        codex.records.append(new_auto)
        monkeypatch.setattr("progress_wx.service.time.time", lambda: 2_001)
        service._refresh_monitor_registry(config)
        assert not state.was_processed("thread-new:turn-thread-new:completed")
        assert {
            item["thread_id"] for item in state.monitor_subscriptions(now=2_001)
        } == {"thread-1", "thread-auto", "thread-new"}
    finally:
        state.close()


def test_running_service_observes_external_auto_monitor_setting_without_restart(
    monkeypatch, tmp_path: Path
) -> None:
    config = make_config(tmp_path)
    manual = ThreadRecord(
        "thread-1", "手动任务", updated_at_ms=1_999_000, thread_source="user"
    )
    first = ThreadRecord(
        "thread-first", "关闭时任务", updated_at_ms=2_000_000, thread_source="user"
    )

    class DiscoveryStore:
        def __init__(self):
            self.records = [manual, first]

        def get_thread(self, thread_id: str):
            return next((item for item in self.records if item.thread_id == thread_id), None)

        def select_threads(self, **_kwargs):
            return list(self.records)

        def require_readable(self, _operation: str):
            return None

        def snapshot(self, thread_id: str):
            raise AssertionError(f"关闭或已完成基线后不应读取快照：{thread_id}")

    monkeypatch.setattr("progress_wx.service.time.time", lambda: 2_000)
    state = StateStore(config.service.database)
    external = StateStore(config.service.database)
    service = ProgressService(config.path)
    service.store = state
    service.codex_store = DiscoveryStore()  # type: ignore[assignment]
    try:
        external.set_auto_monitoring_enabled(False, now=2_000)
        service._refresh_monitor_registry(config)
        disabled = {
            item["thread_id"]: item["origin"]
            for item in state.monitor_subscriptions(now=2_000)
        }
        assert disabled == {"thread-1": "manual"}

        external.set_auto_monitoring_enabled(True, now=2_001)
        monkeypatch.setattr("progress_wx.service.time.time", lambda: 2_001)
        service._refresh_monitor_registry(config)
        enabled = {
            item["thread_id"]: item["origin"]
            for item in state.monitor_subscriptions(now=2_001)
        }
        assert enabled == {"thread-first": "auto", "thread-1": "manual"}
    finally:
        external.close()
        state.close()


def test_rejected_steer_is_released_but_timeout_stays_uncertain(tmp_path: Path) -> None:
    service = ProgressService(make_config(tmp_path).path)
    store = StateStore(tmp_path / "state.sqlite")
    codec = CorrelationCodec(b"u" * 32)
    service.store = store
    rejected = _pending_reply(store, codec, turn_id="old-1", reply_text="稍后新开一轮")

    class RejectedRPC:
        def steer_turn(self, *_args, **_kwargs):
            raise CodexRPCError("明确 JSON-RPC error")

    assert service._steer_active_reply(
        RejectedRPC(),  # type: ignore[arg-type]
        rejected,
        "active-turn",
        timeout_seconds=5,
        on_server_request=lambda _request: None,
    ) is False
    assert store.pending_turn_replies() == [
        (rejected.code, "thread-1", "稍后新开一轮", rejected.fingerprint)
    ]

    uncertain = _pending_reply(store, codec, turn_id="old-2", reply_text="不要重复")

    class TimeoutRPC:
        def steer_turn(self, *_args, **_kwargs):
            raise CodexRPCTimeout("结果未知")

    with pytest.raises(ServiceFatalError, match="结果未知"):
        service._steer_active_reply(
            TimeoutRPC(),  # type: ignore[arg-type]
            uncertain,
            "active-turn",
            timeout_seconds=5,
            on_server_request=lambda _request: None,
        )
    assert store.uncertain_turn_replies() == [uncertain.code]
    store.close()


def test_desktop_active_turn_uses_verified_shared_websocket(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base = make_config(tmp_path)
    config = replace(
        base,
        codex=replace(base.codex, reply_transport="shared_websocket"),
    )
    service = ProgressService(config.path)
    store = StateStore(config.service.database)
    codec = CorrelationCodec(b"w" * 32)
    service.config = config
    service.store = store
    service.codex_store = BusyCodexStore(ThreadStatus.IN_PROGRESS)  # type: ignore[assignment]
    job = _pending_reply(store, codec, turn_id="old-turn", reply_text="桌面中途追加")
    calls: list[tuple] = []

    monkeypatch.setattr(
        "progress_wx.service.active_shared_websocket_url",
        lambda **_kwargs: "ws://127.0.0.1:6230",
    )

    class RPC:
        def __init__(self, *_args, websocket_url=None, **_kwargs):
            calls.append(("connect", websocket_url))

        def read_thread(self, thread_id, **_kwargs):
            calls.append(("read", thread_id))
            return {
                "result": {
                    "thread": {
                        "turns": [{"id": "desktop-active", "status": "inProgress"}]
                    }
                }
            }

        def active_turn_id(self, _response):
            return "desktop-active"

        def steer_turn(self, thread_id, expected_turn_id, message, **_kwargs):
            calls.append(("steer", thread_id, expected_turn_id, message))
            return {"result": {"turnId": expected_turn_id}}

        def resume_thread(self, *_args, **_kwargs):
            raise AssertionError("活动 Desktop turn 不得 resume")

        def start_turn(self, *_args, **_kwargs):
            raise AssertionError("活动 Desktop turn 不得 start 新轮次")

        def close(self):
            calls.append(("close",))

    monkeypatch.setattr("progress_wx.service.CodexAppServer", RPC)

    service._deliver_reply(job)

    assert ("connect", "ws://127.0.0.1:6230") in calls
    assert ("read", "thread-1") in calls
    assert (
        "steer",
        "thread-1",
        "desktop-active",
        "桌面中途追加",
    ) in calls
    assert store.uncertain_turn_replies() == []
    assert store.pending_turn_replies() == []
    store.close()


def test_second_quote_during_tool_owned_turn_uses_steer(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = make_config(tmp_path)
    service = ProgressService(config.path)
    store = StateStore(config.service.database)
    codec = CorrelationCodec(b"v" * 32)
    service.config = config
    service.store = store
    service.codex_store = BusyCodexStore(ThreadStatus.COMPLETED)  # type: ignore[assignment]
    event = hook_payload_to_event(
        {
            "type": "agent-turn-complete",
            "thread-id": "thread-1",
            "turn-id": "shared-parent",
            "last-assistant-message": "结果",
        },
        ThreadRecord("thread-1", title="示例"),
    )
    code = codec.issue()
    store.reserve_notification(event, code, "回复编号：" + code, 72)
    store.mark_sent(event.dedupe_key)
    deliveries = [
        store.enqueue_turn_reply(
            code,
            f"message-{index}",
            f"fingerprint-{index}",
            codec,
            reply_text=reply_text,
        )
        for index, reply_text in enumerate(("开始", "追加", "再追加"), start=1)
    ]
    assert all(item is not None and item.is_new for item in deliveries)
    first_delivery, second_delivery, third_delivery = deliveries
    assert first_delivery is not None
    assert second_delivery is not None
    assert third_delivery is not None
    first = ReplyJob(
        first_delivery.delivery_id,
        "thread-1",
        first_delivery.reply_text,
        first_delivery.fingerprint,
    )
    second = ReplyJob(
        second_delivery.delivery_id,
        "thread-1",
        second_delivery.reply_text,
        second_delivery.fingerprint,
    )
    third = ReplyJob(
        third_delivery.delivery_id,
        "thread-1",
        third_delivery.reply_text,
        third_delivery.fingerprint,
    )
    service.reply_queue.put(second)
    service.reply_queue.put(third)
    calls: list[tuple] = []

    class RPC:
        def __init__(self, *_args, **_kwargs):
            pass

        def resume_thread(self, thread_id, **_kwargs):
            calls.append(("resume", thread_id))
            return {"result": {"thread": {"id": thread_id}}}

        def start_turn(self, thread_id, message, **_kwargs):
            calls.append(("start", thread_id, message))
            return {"result": {"turn": {"id": "active-turn"}}}

        def steer_turn(self, thread_id, expected_turn_id, message, **_kwargs):
            calls.append(("steer", thread_id, expected_turn_id, message))
            return {"result": {"turnId": expected_turn_id}}

        def listen_event(self, thread_id, *, turn_id, **_kwargs):
            return TurnCompletedEvent(
                thread_id,
                turn_id,
                ThreadStatus.COMPLETED,
                raw_status="completed",
            )

        def close(self):
            calls.append(("close",))

    monkeypatch.setattr("progress_wx.service.CodexAppServer", RPC)

    service._deliver_reply(first)

    assert ("start", "thread-1", "开始") in calls
    assert ("steer", "thread-1", "active-turn", "追加") in calls
    assert ("steer", "thread-1", "active-turn", "再追加") in calls
    assert calls.index(("steer", "thread-1", "active-turn", "追加")) < calls.index(
        ("steer", "thread-1", "active-turn", "再追加")
    )
    parent = store._connection.execute(
        "SELECT consumed_at FROM notifications WHERE code=?", (code,)
    ).fetchone()
    assert parent is not None and parent["consumed_at"] is None
    assert store.uncertain_turn_replies() == []
    assert store.pending_turn_replies() == []
    store.close()


def test_approval_and_user_input_use_exact_protocol_shapes() -> None:
    approval = ServerRequest(
        "approval-1",
        "item/commandExecution/requestApproval",
        {"threadId": "thread-1", "turnId": "turn-1"},
    )
    assert server_request_response(approval, "A") == {"decision": "accept"}
    assert server_request_response(approval, "S") == {"decision": "acceptForSession"}
    with pytest.raises(ValueError):
        server_request_response(approval, "继续")

    one_question = ServerRequest(
        "input-1",
        "item/tool/requestUserInput",
        {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "questions": [{"id": "choice", "question": "选 A 还是 B？"}],
        },
    )
    assert server_request_response(one_question, "A") == {
        "answers": {"choice": {"answers": ["A"]}}
    }
    multiple = ServerRequest(
        "input-2",
        "item/tool/requestUserInput",
        {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "questions": [
                {"id": "first", "question": "第一问"},
                {"id": "second", "question": "第二问"},
            ],
        },
    )
    assert server_request_response(multiple, '["甲", "乙"]') == {
        "answers": {
            "first": {"answers": ["甲"]},
            "second": {"answers": ["乙"]},
        }
    }


def test_secret_or_unknown_server_request_fails_closed() -> None:
    secret = ServerRequest(
        1,
        "item/tool/requestUserInput",
        {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "questions": [{"id": "password", "question": "密码", "isSecret": True}],
        },
    )
    with pytest.raises(ServiceFatalError):
        server_request_event(secret, ThreadRecord("thread-1", title="示例"))
    unknown = ServerRequest(2, "item/tool/call", {"threadId": "thread-1"})
    with pytest.raises(ServiceFatalError):
        server_request_response(unknown, "任意内容")


def test_distinct_intervention_phases_in_one_turn_are_not_deduplicated() -> None:
    """同一长任务可以先后多次请求用户介入；只对同一个官方请求去重。"""

    thread = ThreadRecord("thread-1", title="长任务")
    route_request = ServerRequest(
        "input-route",
        "item/tool/requestUserInput",
        {
            "threadId": "thread-1",
            "turnId": "turn-long",
            "questions": [{"id": "route", "question": "请选择技术路线"}],
        },
    )
    test_request = ServerRequest(
        "input-test",
        "item/tool/requestUserInput",
        {
            "threadId": "thread-1",
            "turnId": "turn-long",
            "questions": [{"id": "result", "question": "请运行验收并回复结果"}],
        },
    )

    route_event = server_request_event(route_request, thread)
    repeated_route_event = server_request_event(route_request, thread)
    test_event = server_request_event(test_request, thread)

    assert route_event.dedupe_key == repeated_route_event.dedupe_key
    assert route_event.dedupe_key != test_event.dedupe_key
    assert route_event.turn_id.startswith("turn-long-rpc-")
    assert test_event.turn_id.startswith("turn-long-rpc-")


def test_rpc_quote_routes_to_pending_connection_instead_of_new_turn(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    codec = CorrelationCodec(b"r" * 32)
    store = StateStore(config.service.database)
    service = ProgressService(config.path)
    service.config = config
    service.codec = codec
    service.store = store
    request = ServerRequest(
        "approval-1",
        "item/fileChange/requestApproval",
        {"threadId": "thread-1", "turnId": "turn-1"},
    )
    event = server_request_event(request, ThreadRecord("thread-1", title="示例"))
    code = codec.issue()
    message = "回复编号：" + code
    store.reserve_notification(event, code, message, 72, reply_kind="rpc")
    store.mark_sent(event.dedupe_key)
    pending = PendingServerReply(request, queue.Queue(maxsize=1))
    service._pending_server_replies[code] = pending

    service._on_quote(
        QuoteMessage("主号", "A", message, "小号", message_id="m1", message_hash="h1")
    )
    assert pending.responses.get_nowait() == {"decision": "accept"}
    with pytest.raises(queue.Empty):
        service.reply_queue.get_nowait()
    store.close()


def test_stale_rpc_quote_is_not_consumed_without_original_connection(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    codec = CorrelationCodec(b"s" * 32)
    store = StateStore(config.service.database)
    service = ProgressService(config.path)
    service.config = config
    service.codec = codec
    service.store = store
    request = ServerRequest(
        "approval-stale",
        "item/fileChange/requestApproval",
        {"threadId": "thread-1", "turnId": "turn-1"},
    )
    event = server_request_event(request, ThreadRecord("thread-1", title="示例"))
    code = codec.issue()
    message = "回复编号：" + code
    store.reserve_notification(event, code, message, 72, reply_kind="rpc")
    store.mark_sent(event.dedupe_key)

    service._on_quote(
        QuoteMessage("主号", "A", message, "小号", message_id="m2", message_hash="h2")
    )

    assert store.peek_reply(code, codec) == ("thread-1", event.turn_id, "rpc")
    store.close()


def test_global_hook_quote_writes_signed_response_without_new_turn(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    CorrelationCodec.create_secret_file(config.messaging.secret_file)
    codec = CorrelationCodec.from_file(config.messaging.secret_file)
    store = StateStore(config.service.database)
    bridge = ApprovalBridge(tmp_path / "approval-bridge", config.messaging.secret_file)
    request = bridge.submit(
        {
            "hook_event_name": "PermissionRequest",
            "session_id": "thread-1",
            "turn_id": "turn-1",
            "cwd": str(tmp_path),
            "tool_name": "exec_command",
            "tool_input": {"cmd": "git status"},
        },
        timeout_seconds=30,
    )
    event = TurnEvent(
        "thread-1", request.request_id, "waitingOnApproval", source="codex-permission-hook"
    )
    code = codec.issue()
    message = "回复编号：" + code
    store.reserve_notification(event, code, message, 72, reply_kind="hook")
    store.mark_sent(event.dedupe_key)
    service = ProgressService(config.path)
    service.config = config
    service.codec = codec
    service.store = store
    service.approval_bridge = bridge

    service._on_quote(
        QuoteMessage("主号", "允许一次", message, "小号", message_id="m-hook", message_hash="h-hook")
    )

    assert bridge.wait_for_response(request) == "allow"
    assert store.peek_reply(code, codec) is None
    with pytest.raises(queue.Empty):
        service.reply_queue.get_nowait()
    bridge.complete(request)
    store.close()


def test_quote_callback_failure_stops_main_service(tmp_path: Path, monkeypatch) -> None:
    config = make_config(tmp_path)
    codec = CorrelationCodec(b"e" * 32)
    store = StateStore(config.service.database)
    service = ProgressService(config.path)
    service.codec = codec
    service.store = store
    code = codec.issue()
    monkeypatch.setattr(
        store,
        "inspect_reply_route",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk")),
    )

    service._on_quote(
        QuoteMessage("主号", "继续", "回复编号：" + code, "小号", message_id="m3", message_hash="h3")
    )

    assert service.stop_event.is_set()
    assert isinstance(service._fatal, OSError)
    store.close()


def test_same_text_can_reply_to_different_codes_without_message_ids(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    codec = CorrelationCodec(b"f" * 32)
    store = StateStore(config.service.database)
    service = ProgressService(config.path)
    service.codec = codec
    service.store = store
    codes: list[str] = []
    for index in (1, 2):
        event = hook_payload_to_event(
            {
                "type": "agent-turn-complete",
                "thread-id": "thread-1",
                "turn-id": f"turn-{index}",
                "last-assistant-message": "结果",
            },
            ThreadRecord("thread-1", title="示例"),
        )
        code = codec.issue()
        codes.append(code)
        store.reserve_notification(event, code, "回复编号：" + code, 72)
        store.mark_sent(event.dedupe_key)
        service._on_quote(QuoteMessage("主号", "继续", "回复编号：" + code, "小号"))

    queued = [service.reply_queue.get_nowait(), service.reply_queue.get_nowait()]
    assert len({item.code for item in queued}) == 2
    parent_codes = [
        store._connection.execute(
            "SELECT parent_code FROM reply_deliveries WHERE delivery_id=?",
            (item.code,),
        ).fetchone()[0]
        for item in queued
    ]
    assert parent_codes == codes
    store.close()


def test_turn_reply_survives_restart_until_non_idempotent_claim(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    codec = CorrelationCodec(b"p" * 32)
    event = hook_payload_to_event(
        {
            "type": "agent-turn-complete",
            "thread-id": "thread-1",
            "turn-id": "turn-1",
            "last-assistant-message": "完成",
        },
        ThreadRecord("thread-1", title="示例"),
    )
    code = codec.issue()
    store = StateStore(config.service.database)
    store.reserve_notification(event, code, "回复编号：" + code, 72)
    store.mark_sent(event.dedupe_key)
    delivery = store.enqueue_turn_reply(
        code,
        "message-restart",
        "fp",
        codec,
        reply_text="继续",
    )
    assert delivery is not None
    store.close()

    reopened = StateStore(config.service.database)
    assert reopened.pending_turn_replies() == [
        (delivery.delivery_id, "thread-1", "继续", "fp")
    ]
    assert reopened.claim_turn_reply(delivery.delivery_id) is True
    assert reopened.pending_turn_replies() == []
    assert reopened.uncertain_turn_replies() == [delivery.delivery_id]
    reopened.close()


def _make_synthetic_rollout_poll_store(
    tmp_path: Path,
    *,
    history_turn_id: str,
    history_status: str,
    state_updated_at_ms: int,
    rollout_turn_id: str,
    rollout_completed_at_ms: int,
) -> CodexStore:
    """Create isolated Codex state/history plus one trusted append-only rollout."""

    import json
    import sqlite3

    from progress_wx.codex_store import CodexStore, StorePaths

    codex_home = tmp_path / "codex-home"
    sessions = codex_home / "sessions" / "2026" / "09" / "04"
    sessions.mkdir(parents=True, exist_ok=True)
    rollout = sessions / "rollout-thread-poll.jsonl"
    rollout.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": rollout_turn_id,
                    "started_at": rollout_completed_at_ms - 1_000,
                    "completed_at": rollout_completed_at_ms,
                    "last_agent_message": "轮询发现的最新完成回复",
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    state_db = codex_home / "state_5.sqlite"
    connection = sqlite3.connect(state_db)
    try:
        connection.executescript(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                name TEXT,
                cwd TEXT NOT NULL,
                updated_at_ms INTEGER,
                created_at_ms INTEGER,
                archived INTEGER NOT NULL DEFAULT 0,
                preview TEXT,
                source TEXT,
                thread_source TEXT,
                rollout_path TEXT
            );
            """
        )
        connection.execute(
            """
            INSERT INTO threads(
                id, title, name, cwd, updated_at_ms, created_at_ms,
                archived, preview, source, thread_source, rollout_path
            ) VALUES (?, ?, '', ?, ?, ?, 0, '', 'codex', 'user', ?)
            """,
            (
                "thread-poll",
                "合成轮询监测",
                str(codex_home / "workspace"),
                state_updated_at_ms,
                state_updated_at_ms - 1_000,
                str(rollout),
            ),
        )
        connection.commit()
    finally:
        connection.close()

    history_db = codex_home / "thread_history_1.sqlite"
    connection = sqlite3.connect(history_db)
    try:
        connection.executescript(
            """
            CREATE TABLE thread_turns (
                thread_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                rollout_ordinal INTEGER NOT NULL,
                status TEXT NOT NULL,
                error_json TEXT,
                started_at INTEGER,
                completed_at INTEGER,
                duration_ms INTEGER,
                final_agent_item_id TEXT
            );
            CREATE TABLE thread_items (
                thread_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                rollout_ordinal INTEGER,
                created_at_ms INTEGER,
                item_json TEXT,
                item_type TEXT,
                updated_at_ordinal INTEGER
            );
            """
        )
        history_completed_at = (
            state_updated_at_ms
            if history_status == "completed"
            else None
        )
        final_item_id = (
            "item-history-old" if history_status == "completed" else None
        )
        connection.execute(
            """
            INSERT INTO thread_turns(
                thread_id, turn_id, rollout_ordinal, status, started_at,
                completed_at, final_agent_item_id
            ) VALUES (?, ?, 1, ?, ?, ?, ?)
            """,
            (
                "thread-poll",
                history_turn_id,
                history_status,
                state_updated_at_ms - 2_000,
                history_completed_at,
                final_item_id,
            ),
        )
        if final_item_id:
            connection.execute(
                """
                INSERT INTO thread_items(
                    thread_id, turn_id, item_id, rollout_ordinal,
                    created_at_ms, item_json, item_type
                ) VALUES (?, ?, ?, 1, ?, ?, 'agentMessage')
                """,
                (
                    "thread-poll",
                    history_turn_id,
                    final_item_id,
                    state_updated_at_ms,
                    json.dumps(
                        {
                            "type": "agentMessage",
                            "id": final_item_id,
                            "phase": "final_answer",
                            "text": "历史投影里的旧完成回复",
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
        connection.commit()
    finally:
        connection.close()

    return CodexStore(
        StorePaths(
            state_db=state_db,
            history_db=history_db,
            session_index=codex_home / "session_index.jsonl",
        ),
        timeout_seconds=0.2,
    )


@pytest.mark.parametrize(
    "updated_offset_ms",
    (-1_000, 0, 1_000, 1_999),
    ids=("stale", "equal", "within-1s", "within-2s"),
)
def test_monitor_poll_detects_new_rollout_when_state_timestamp_lags(
    tmp_path: Path,
    updated_offset_ms: int,
) -> None:
    """A monitor poll must not miss a newer trusted rollout behind a stale index."""

    from progress_wx.config import ThreadSelectors

    base = make_config(tmp_path)
    config = replace(
        base,
        codex=replace(
            base.codex,
            selectors=ThreadSelectors(ids=("thread-poll",)),
        ),
    )
    old_completed_at_ms = 1_700_000_000_000
    rollout_completed_at_ms = old_completed_at_ms + 10_000
    codex = _make_synthetic_rollout_poll_store(
        tmp_path,
        history_turn_id="turn-history-old",
        history_status="completed",
        state_updated_at_ms=old_completed_at_ms + updated_offset_ms,
        rollout_turn_id="turn-rollout-new",
        rollout_completed_at_ms=rollout_completed_at_ms,
    )
    state = StateStore(config.service.database)

    class RecordingSummarizer:
        def __init__(self) -> None:
            self.events: list[TurnEvent] = []

        def summarize(self, event, *, wait=None):
            del wait
            self.events.append(event)
            return ProgressReport(
                "完成",
                "轮询通知已发送。",
                NotificationReason.CONVERSATION_COMPLETE,
            )

    service = ProgressService(config.path)
    service.config = config
    service.store = state
    service.codec = CorrelationCodec(b"m" * 32)
    service.channel = FakeWechat()  # type: ignore[assignment]
    summarizer = RecordingSummarizer()
    service.summarizer = summarizer  # type: ignore[assignment]
    service.codex_store = codex
    service._last_wechat_health = time.monotonic()
    service._refresh_monitor_registry = lambda _config: None  # type: ignore[method-assign]

    service._poll_once(config)
    service._poll_once(config)

    assert len(summarizer.events) == 1
    assert summarizer.events[0].turn_id == "turn-rollout-new"
    assert summarizer.events[0].final_message == "轮询发现的最新完成回复"
    assert len(service.channel.messages) == 1  # type: ignore[attr-defined]
    assert state.was_processed("thread-poll:turn-rollout-new:completed")
    state.close()
    codex.close() if hasattr(codex, "close") else None


def test_monitor_poll_promotes_rollout_completion_over_same_turn_incomplete_history(
    tmp_path: Path,
) -> None:
    """A rollout completion must complete a same-turn stale inProgress history row."""

    from progress_wx.config import ThreadSelectors

    base = make_config(tmp_path)
    config = replace(
        base,
        codex=replace(
            base.codex,
            selectors=ThreadSelectors(ids=("thread-poll",)),
        ),
    )
    old_started_at_ms = 1_700_000_000_000
    rollout_completed_at_ms = old_started_at_ms + 10_000
    codex = _make_synthetic_rollout_poll_store(
        tmp_path,
        history_turn_id="turn-same",
        history_status="inProgress",
        state_updated_at_ms=old_started_at_ms,
        rollout_turn_id="turn-same",
        rollout_completed_at_ms=rollout_completed_at_ms,
    )
    state = StateStore(config.service.database)

    class RecordingSummarizer:
        def __init__(self) -> None:
            self.events: list[TurnEvent] = []

        def summarize(self, event, *, wait=None):
            del wait
            self.events.append(event)
            return ProgressReport(
                "完成",
                "同一轮完成通知已发送。",
                NotificationReason.CONVERSATION_COMPLETE,
            )

    service = ProgressService(config.path)
    service.config = config
    service.store = state
    service.codec = CorrelationCodec(b"n" * 32)
    service.channel = FakeWechat()  # type: ignore[assignment]
    summarizer = RecordingSummarizer()
    service.summarizer = summarizer  # type: ignore[assignment]
    service.codex_store = codex
    service._last_wechat_health = time.monotonic()
    service._refresh_monitor_registry = lambda _config: None  # type: ignore[method-assign]

    service._poll_once(config)
    service._poll_once(config)

    assert len(summarizer.events) == 1
    assert summarizer.events[0].turn_id == "turn-same"
    assert summarizer.events[0].final_message == "轮询发现的最新完成回复"
    assert len(service.channel.messages) == 1  # type: ignore[attr-defined]
    assert state.was_processed("thread-poll:turn-same:completed")
    state.close()
    codex.close() if hasattr(codex, "close") else None
