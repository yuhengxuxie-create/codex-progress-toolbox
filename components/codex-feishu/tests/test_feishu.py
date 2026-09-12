"""飞书消息渠道的离线测试；不访问飞书网络。"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from progress_wx import feishu
from progress_wx.channel import ChannelAttachment, ChannelReply, MessageChannelOfflineError
from progress_wx.feishu import FeishuMessageChannel
from progress_wx.feature_center import (
    FEATURE_CENTER_ENTRY_COMMAND,
    FEATURE_CENTER_MENU_EVENT_KEY,
    feature_center_action,
    feature_center_action_fingerprint,
    feature_operation_action,
    feature_operation_action_fingerprint,
)
from progress_wx.session_query_card import (
    SESSION_QUERY_MENU_EVENT_KEY,
    build_session_query_card,
    build_thread_reply_form_card,
    session_query_action,
    session_query_action_fingerprint,
)
from progress_wx.slash_commands import (
    slash_card_action,
    slash_card_action_fingerprint,
)


@dataclass
class FakeInbound:
    sender_id: str = "ou_owner"
    message_id: str = "om_reply"
    reply_to_message_id: str | None = "om_notice"
    chat_type: str = "p2p"
    raw_content_type: str = "text"
    sender_is_bot: bool = False
    safe_content_text: str = "继续"
    content_text: str = "继续"
    chat_id: str = "oc_private"
    raw: dict[str, object] = field(default_factory=dict)
    batched_sources: list["FakeInbound"] | None = None
    resources: list[object] = field(default_factory=list)


class FakeSdkChannel:
    """只实现生产包装真正使用的官方 SDK 表面。"""

    def __init__(self, *, connect_error: BaseException | None = None) -> None:
        self.handlers: dict[str, object] = {}
        self.ws_client = SimpleNamespace(_conn=None)
        self.sent: list[tuple[str, object, dict[str, str]]] = []
        self.connect_error = connect_error
        self.chunk_ids: list[str] | None = None
        self.disconnect_calls = 0
        self.cache_results: list[object] = []
        self.resolve_calls: list[tuple[str, list[object]]] = []
        self.custom_event_handlers: dict[str, object] = {}

    def on(self, name: str, handler) -> None:
        self.handlers[name] = handler

    def register_custom_event(self, event_type: str, handler) -> None:
        self.custom_event_handlers[event_type] = handler

    async def connect_until_ready(self, *, timeout: float) -> None:
        del timeout
        if self.connect_error:
            raise self.connect_error
        self.ws_client._conn = object()

    def connection_snapshot(self):
        return SimpleNamespace(ready=self.ws_client._conn is not None)

    async def send(self, target: str, message: object, options: dict[str, str]):
        self.sent.append((target, message, options))
        message_id = self.chunk_ids[0] if self.chunk_ids else f"om_sent_{len(self.sent)}"
        return SimpleNamespace(
            success=True,
            message_id=message_id,
            chunk_ids=self.chunk_ids,
            raw={"code": 0, "data": {"chat_id": "oc_private"}},
        )

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.ws_client._conn = None

    async def resolve_resources_to_cache(self, *, message_id: str, resources: list[object]):
        self.resolve_calls.append((message_id, resources))
        return self.cache_results


def test_pairing_preloads_default_sdk_before_starting_asyncio(monkeypatch) -> None:
    """SDK 首次导入必须发生在 asyncio.run 之前，规避其模块级 loop 冲突。"""

    order: list[str] = []

    def preload():
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        order.append("preload")
        return ()

    class PairingSdk:
        def on(self, _name, handler):
            self.handler = handler

        async def connect_until_ready(self, *, timeout):
            del timeout
            order.append("connect")
            await self.handler(
                SimpleNamespace(
                    sender_id="ou_owner",
                    safe_content_text="PCPAIR-TEST",
                    content_text="PCPAIR-TEST",
                    chat_type="p2p",
                    sender_is_bot=False,
                )
            )

        async def disconnect(self):
            order.append("disconnect")

    monkeypatch.setattr(feishu, "_official_sdk_symbols", preload)
    monkeypatch.setattr(
        feishu,
        "_official_sdk_factory",
        lambda *_args: PairingSdk(),
    )

    assert feishu.discover_feishu_open_id(
        app_id="cli_test",
        app_secret="secret",
        pairing_code="PCPAIR-TEST",
        timeout_seconds=30,
    ) == "ou_owner"
    assert order == ["preload", "connect", "disconnect"]


def test_production_channel_preloads_sdk_before_worker_loop(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        feishu,
        "_official_sdk_symbols",
        lambda: calls.append("preload") or (),
    )

    FeishuMessageChannel(
        app_id="cli_test",
        app_secret="secret",
        target_open_id="ou_owner",
    )
    assert calls == ["preload"]


def test_running_sdk_import_loop_is_rejected() -> None:
    """被其他模块抢先绑定到运行 loop 时必须明确失败，不能隐蔽崩溃。"""

    with pytest.raises(feishu.FeishuDependencyError, match="请重启进度通知"):
        feishu._ensure_sdk_import_loop_idle(SimpleNamespace(is_running=lambda: True))
    feishu._ensure_sdk_import_loop_idle(SimpleNamespace(is_running=lambda: False))


def test_connection_failure_classifier_is_fail_closed_for_auth_and_transient_for_network() -> None:
    class SdkChannelError(RuntimeError):
        def __init__(self, code):
            super().__init__("redacted")
            self.code = code

    assert feishu.is_transient_channel_failure(OSError("offline")) is True
    assert feishu.is_transient_channel_failure(
        SdkChannelError("NOT_CONNECTED")
    ) is True
    assert feishu.is_transient_channel_failure(
        SdkChannelError(99991672)
    ) is False
    assert feishu.is_transient_channel_failure(
        SdkChannelError(403)
    ) is False
    assert feishu.is_transient_channel_failure(
        RuntimeError("Event loop is closed")
    ) is True
    # 未知异常默认永久，避免凭据/依赖问题被无限重连掩盖。
    assert feishu.is_transient_channel_failure(RuntimeError("unknown")) is False


def test_official_sdk_start_repairs_closed_loop_on_reused_executor_thread(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """旧 cache loop 关闭后，同一 executor 线程上的新 channel 仍可启动。"""

    first = feishu._official_sdk_factory(
        "cli_test", "secret", "ou_owner", 1, tmp_path / "media-first"
    )
    second = feishu._official_sdk_factory(
        "cli_test", "secret", "ou_owner", 1, tmp_path / "media-second"
    )
    base_channel_type = type(first).__mro__[1]
    observed_loops: list[asyncio.AbstractEventLoop] = []

    def fake_sdk_start(_self) -> None:
        loop = asyncio.get_event_loop()
        assert loop.is_closed() is False
        assert loop.is_running() is False
        observed_loops.append(loop)
        # 模拟这一代 channel 断开后回收 ExpiringCache 私有 loop。
        loop.close()

    monkeypatch.setattr(base_channel_type, "start", fake_sdk_start)

    from lark_channel.ws import client as ws_client_module

    module_ws_loop = ws_client_module.loop

    def reuse_one_worker() -> int:
        poisoned = asyncio.new_event_loop()
        asyncio.set_event_loop(poisoned)
        poisoned.close()
        first.start()
        second.start()
        return threading.get_ident()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        worker_id = executor.submit(reuse_one_worker).result(timeout=5)

    assert worker_id != threading.get_ident()
    assert len(observed_loops) == 2
    assert observed_loops[0] is not observed_loops[1]
    assert all(loop.is_closed() for loop in observed_loops)
    assert ws_client_module.loop is module_ws_loop
    assert module_ws_loop.is_closed() is False


def _start(fake: FakeSdkChannel) -> tuple[FeishuMessageChannel, list[ChannelReply]]:
    received: list[ChannelReply] = []
    channel = FeishuMessageChannel(
        app_id="cli_test",
        app_secret="secret",
        target_open_id="ou_owner",
        connect_timeout_seconds=5,
        retry_delays=(0, 0, 0, 0, 0),
        sdk_factory=lambda *_args: fake,
    )
    channel.start(received.append)
    return channel, received


def _emit(channel: FeishuMessageChannel, fake: FakeSdkChannel, message: FakeInbound) -> None:
    handler = fake.handlers["message"]
    assert channel._loop is not None
    future = asyncio.run_coroutine_threadsafe(handler(message), channel._loop)
    future.result(timeout=2)


def _flush(channel: FeishuMessageChannel) -> None:
    assert channel._loop is not None
    future = asyncio.run_coroutine_threadsafe(asyncio.sleep(0), channel._loop)
    future.result(timeout=2)


def test_inbound_callback_failure_reaches_durable_sink_before_error_handler() -> None:
    fake = FakeSdkChannel()
    failures: list[tuple[ChannelReply, BaseException]] = []
    errors: list[BaseException] = []

    def callback(_reply: ChannelReply) -> None:
        raise RuntimeError("synthetic callback failure")

    channel = FeishuMessageChannel(
        app_id="cli_test",
        app_secret="secret",
        target_open_id="ou_owner",
        retry_delays=(0, 0, 0, 0, 0),
        error_handler=errors.append,
        sdk_factory=lambda *_args: fake,
    )
    # Guardian binds this public hook after constructing the adapter.
    channel.inbound_failure_handler = (
        lambda reply, error: failures.append((reply, error))
    )
    channel.start(callback)
    try:
        _emit(channel, fake, FakeInbound())
        assert len(failures) == 1
        failed_reply, failed_error = failures[0]
        assert failed_reply.message_id == "om_reply"
        assert failed_reply.content == "继续"
        assert isinstance(failed_error, RuntimeError)
        assert errors == [failed_error]
        # The adapter callback completes normally from SDK's perspective; a
        # durable guardian sink, rather than SDK re-raise, owns recovery.
        _emit(channel, fake, FakeInbound())
        assert len(failures) == 1
    finally:
        channel.stop()


def test_inbound_failure_sink_exception_is_reported_without_claiming_durability() -> None:
    fake = FakeSdkChannel()
    errors: list[BaseException] = []

    def callback(_reply: ChannelReply) -> None:
        raise RuntimeError("synthetic callback failure")

    def broken_sink(_reply: ChannelReply, _error: BaseException) -> None:
        raise OSError("synthetic spool lock")

    channel = FeishuMessageChannel(
        app_id="cli_test",
        app_secret="secret",
        target_open_id="ou_owner",
        retry_delays=(0, 0, 0, 0, 0),
        error_handler=errors.append,
        inbound_failure_handler=broken_sink,
        sdk_factory=lambda *_args: fake,
    )
    channel.start(callback)
    try:
        with pytest.raises(OSError, match="synthetic spool lock"):
            _emit(channel, fake, FakeInbound())
        assert [type(item) for item in errors] == [OSError, RuntimeError]
    finally:
        channel.stop()


def test_session_query_card_is_sent_with_stable_uuid() -> None:
    fake = FakeSdkChannel()
    channel, _received = _start(fake)
    try:
        card = build_session_query_card()
        assert channel.send_card(card, idempotency_key="query-card-1") == "om_sent_1"
        target, message, options = fake.sent[-1]
        assert target == "ou_owner"
        assert message == {"card": card}
        assert options["receive_id_type"] == "open_id"
        assert len(options["uuid"]) == 32
    finally:
        channel.stop()


def test_classic_single_form_card_is_sent_with_stable_uuid() -> None:
    fake = FakeSdkChannel()
    channel, _received = _start(fake)
    try:
        card = build_thread_reply_form_card("合成会话")
        assert "schema" not in card
        assert channel.send_card(card, idempotency_key="reply-form-1") == "om_sent_1"
        target, message, options = fake.sent[-1]
        assert target == "ou_owner"
        assert message == {"card": card}
        assert options["receive_id_type"] == "open_id"
        assert len(options["uuid"]) == 32
    finally:
        channel.stop()


@pytest.mark.parametrize(
    "card",
    (
        {"header": {}, "elements": []},
        {
            "schema": "2.0",
            "header": {},
            "body": {"elements": []},
            "elements": [],
        },
        {
            "header": {},
            "elements": [
                {
                    "tag": "form",
                    "elements": [
                        {
                            "tag": "button",
                            "form_action_type": "submit",
                        }
                    ],
                }
            ],
        },
    ),
)
def test_send_card_rejects_incomplete_or_mixed_card_protocols(card) -> None:
    fake = FakeSdkChannel()
    channel, _received = _start(fake)
    try:
        with pytest.raises(ValueError, match="CardKit 2.0|经典交互卡"):
            channel.send_card(card, idempotency_key="invalid-card")
        assert fake.sent == []
    finally:
        channel.stop()


def test_pinned_sdk_normalizes_form_values_and_does_not_content_dedup(
    tmp_path: Path,
) -> None:
    """合成真实 SDK 1.2.0 回调；不连接飞书网络。"""

    from lark_channel.event.callback.model.p2_card_action_trigger import (
        P2CardActionTrigger,
    )

    sdk = feishu._official_sdk_factory(
        "cli_test",
        "secret",
        "ou_owner",
        1,
        tmp_path / "media",
    )
    assert sdk._config.safety.dedup.enabled is True
    received: list[object] = []
    sdk.on("cardAction", received.append)

    def trigger(event_id: str) -> P2CardActionTrigger:
        return P2CardActionTrigger(
            {
                "header": {"event_id": event_id},
                "event": {
                    "operator": {"open_id": "ou_owner"},
                    "context": {
                        "open_message_id": "om_card",
                        "open_chat_id": "oc_private",
                    },
                    "action": {
                        "tag": "button",
                        "name": "thread_reply_submit",
                        "value": session_query_action("thread_reply_submit"),
                        "form_value": {"thread_reply": "相同内容"},
                    },
                },
            }
        )

    sdk._ensure_bg_loop()
    try:
        assert sdk._bg_loop is not None
        for event_id in ("evt-form-1", "evt-form-2"):
            future = asyncio.run_coroutine_threadsafe(
                sdk._handle_interaction_event(trigger(event_id)), sdk._bg_loop
            )
            future.result(timeout=2)
        asyncio.run_coroutine_threadsafe(
            asyncio.sleep(0.05), sdk._bg_loop
        ).result(timeout=2)
    finally:
        sdk.stop()
    assert len(received) == 2
    for index, item in enumerate(received, start=1):
        assert item.message_id == "om_card"
        assert item.chat_id == "oc_private"
        assert item.operator.open_id == "ou_owner"
        assert item.action.form_value == {"thread_reply": "相同内容"}
        assert item.raw["header"]["event_id"] == f"evt-form-{index}"


def test_session_query_card_action_becomes_bound_management_reply() -> None:
    fake = FakeSdkChannel()
    channel, received = _start(fake)
    try:
        event = SimpleNamespace(
            message_id="om_query_card",
            chat_id="oc_private",
            operator=SimpleNamespace(open_id="ou_owner"),
            action=SimpleNamespace(
                tag="button",
                value=session_query_action("personal_sessions"),
            ),
            raw={"header": {"event_id": "evt-card-1"}},
        )
        assert channel._loop is not None
        future = asyncio.run_coroutine_threadsafe(
            fake.handlers["cardAction"](event), channel._loop
        )
        future.result(timeout=2)
        assert received == [
            ChannelReply(
                sender_id="ou_owner",
                content="查询个人会话",
                reply_to_message_id="om_query_card",
                message_id="feishu-card-evt-card-1",
                chat_id="oc_private",
                source_kind="card_action",
                action_name="personal_sessions",
                action_fingerprint=session_query_action_fingerprint(
                    session_query_action("personal_sessions")
                ) or "",
            )
        ]
    finally:
        channel.stop()


def test_thread_reply_open_card_action_becomes_bound_management_reply() -> None:
    """概览卡只发打开动作，续聊表单由管理层另发独立卡片。"""

    fake = FakeSdkChannel()
    channel, received = _start(fake)
    try:
        event = SimpleNamespace(
            message_id="om_overview_card",
            chat_id="oc_private",
            operator=SimpleNamespace(open_id="ou_owner"),
            action=SimpleNamespace(
                tag="button",
                name="thread_reply_open",
                value=session_query_action("thread_reply_open"),
                form_value=None,
            ),
            raw={"header": {"event_id": "evt-thread-reply-open"}},
        )
        assert channel._loop is not None
        future = asyncio.run_coroutine_threadsafe(
            fake.handlers["cardAction"](event), channel._loop
        )
        future.result(timeout=2)
        assert received == [
            ChannelReply(
                sender_id="ou_owner",
                content="继续对话",
                reply_to_message_id="om_overview_card",
                message_id="feishu-card-evt-thread-reply-open",
                chat_id="oc_private",
                source_kind="card_action",
                action_name="thread_reply_open",
                action_fingerprint=session_query_action_fingerprint(
                    session_query_action("thread_reply_open")
                )
                or "",
            )
        ]
    finally:
        channel.stop()


def test_session_query_card_form_actions_become_bound_management_replies() -> None:
    fake = FakeSdkChannel()
    channel, received = _start(fake)
    try:
        events = (
            SimpleNamespace(
                message_id="om_overview_card",
                chat_id="oc_private",
                operator=SimpleNamespace(open_id="ou_owner"),
                action=SimpleNamespace(
                    tag="button",
                    name="thread_reply_submit",
                    value=session_query_action("thread_reply_submit"),
                    form_value={"thread_reply": "  继续完成部署  "},
                ),
                raw={"header": {"event_id": "evt-thread-form"}},
            ),
            SimpleNamespace(
                message_id="om_search_card",
                chat_id="oc_private",
                operator=SimpleNamespace(open_id="ou_owner"),
                action=SimpleNamespace(
                    tag="button",
                    name="session_search_submit",
                    value=session_query_action("session_search_submit"),
                    form_value={
                        "session_name": "医疗科技选题",
                        "session_description": "机器人与选题",
                        "session_activity": "最近7天",
                    },
                ),
                raw={"header": {"event_id": "evt-search-form"}},
            ),
        )
        assert channel._loop is not None
        for event in events:
            future = asyncio.run_coroutine_threadsafe(
                fake.handlers["cardAction"](event), channel._loop
            )
            future.result(timeout=2)

        assert received == [
            ChannelReply(
                sender_id="ou_owner",
                content="  继续完成部署  ",
                reply_to_message_id="om_overview_card",
                message_id="feishu-card-evt-thread-form",
                chat_id="oc_private",
                source_kind="card_action",
                action_name="thread_reply_submit",
                action_fingerprint=session_query_action_fingerprint(
                    session_query_action("thread_reply_submit")
                ) or "",
            ),
            ChannelReply(
                sender_id="ou_owner",
                content=(
                    "会话名称：医疗科技选题\n"
                    "会话描述：机器人与选题\n"
                    "会话最后活动时间：最近7天"
                ),
                reply_to_message_id="om_search_card",
                message_id="feishu-card-evt-search-form",
                chat_id="oc_private",
                source_kind="card_action",
                action_name="session_search_submit",
                action_fingerprint=session_query_action_fingerprint(
                    session_query_action("session_search_submit")
                ) or "",
            ),
        ]
    finally:
        channel.stop()


def test_session_query_form_fallback_identity_includes_submitted_values() -> None:
    fake = FakeSdkChannel()
    channel, received = _start(fake)
    try:
        assert channel._loop is not None
        for content in ("第一次提交", "第二次提交"):
            event = SimpleNamespace(
                message_id="om_overview_card",
                chat_id="oc_private",
                operator=SimpleNamespace(open_id="ou_owner"),
                action=SimpleNamespace(
                    tag="button",
                    name="thread_reply_submit",
                    value=session_query_action("thread_reply_submit"),
                    form_value={"thread_reply": content},
                ),
                raw={},
            )
            future = asyncio.run_coroutine_threadsafe(
                fake.handlers["cardAction"](event), channel._loop
            )
            future.result(timeout=2)
        assert [item.content for item in received] == ["第一次提交", "第二次提交"]
        assert received[0].message_id != received[1].message_id
    finally:
        channel.stop()


def test_dynamic_session_query_card_action_maps_only_strict_label() -> None:
    fake = FakeSdkChannel()
    channel, received = _start(fake)
    try:
        event = SimpleNamespace(
            message_id="om_project_card",
            chat_id="oc_private",
            operator=SimpleNamespace(open_id="ou_owner"),
            action=SimpleNamespace(
                tag="button",
                value=session_query_action("expand_project", label="A01"),
            ),
            raw={"header": {"event_id": "evt-project-a01"}},
        )
        assert channel._loop is not None
        future = asyncio.run_coroutine_threadsafe(
            fake.handlers["cardAction"](event), channel._loop
        )
        future.result(timeout=2)
        assert received[-1].content == "展开A01"
        assert received[-1].reply_to_message_id == "om_project_card"
    finally:
        channel.stop()


def test_session_query_card_action_rejects_other_owner_and_unknown_value() -> None:
    fake = FakeSdkChannel()
    channel, received = _start(fake)
    try:
        assert channel._loop is not None
        for event in (
            SimpleNamespace(
                message_id="om_query_card",
                chat_id="oc_private",
                operator=SimpleNamespace(open_id="ou_other"),
                action=SimpleNamespace(
                    tag="button", value=session_query_action("project_sessions")
                ),
                raw={"header": {"event_id": "evt-card-other"}},
            ),
            SimpleNamespace(
                message_id="om_query_card",
                chat_id="oc_private",
                operator=SimpleNamespace(open_id="ou_owner"),
                action=SimpleNamespace(tag="button", value={"action": "archive"}),
                raw={"header": {"event_id": "evt-card-unknown"}},
            ),
            SimpleNamespace(
                message_id="om_query_card",
                chat_id="oc_private",
                operator=SimpleNamespace(open_id="ou_owner"),
                action=SimpleNamespace(
                    tag="button",
                    value=session_query_action("thread_reply_submit"),
                    form_value={
                        "thread_reply": "继续",
                        "thread_id": "thread-other",
                    },
                ),
                raw={"header": {"event_id": "evt-card-extra-field"}},
            ),
        ):
            future = asyncio.run_coroutine_threadsafe(
                fake.handlers["cardAction"](event), channel._loop
            )
            future.result(timeout=2)
        assert received == []
    finally:
        channel.stop()


def test_fixed_bot_menu_event_opens_session_query_entry_for_owner_only() -> None:
    fake = FakeSdkChannel()
    channel, received = _start(fake)
    try:
        handler = fake.custom_event_handlers[SESSION_QUERY_MENU_EVENT_KEY]
        handler(
            SimpleNamespace(
                header=SimpleNamespace(event_id="evt-menu-1"),
                event={
                    "event_key": SESSION_QUERY_MENU_EVENT_KEY,
                    "operator": {"operator_id": {"open_id": "ou_owner"}},
                },
            )
        )
        _flush(channel)
        assert received == [
            ChannelReply(
                sender_id="ou_owner",
                content="查询会话",
                reply_to_message_id="",
                message_id="feishu-menu-evt-menu-1",
                chat_id="",
                source_kind="bot_menu",
            )
        ]
        handler(
            SimpleNamespace(
                header=SimpleNamespace(event_id="evt-menu-2"),
                event={
                    "event_key": SESSION_QUERY_MENU_EVENT_KEY,
                    "operator": {"operator_id": {"open_id": "ou_other"}},
                },
            )
        )
        _flush(channel)
        assert len(received) == 1
    finally:
        channel.stop()


def test_fixed_bot_menu_event_opens_feature_center_entry_for_owner_only() -> None:
    fake = FakeSdkChannel()
    channel, received = _start(fake)
    try:
        handler = fake.custom_event_handlers[FEATURE_CENTER_MENU_EVENT_KEY]
        handler(
            SimpleNamespace(
                header=SimpleNamespace(event_id="evt-feature-menu-1"),
                event={
                    "event_key": FEATURE_CENTER_MENU_EVENT_KEY,
                    "operator": {"operator_id": {"open_id": "ou_owner"}},
                    "chat_type": "p2p",
                    "chat_id": "oc_private",
                },
            )
        )
        _flush(channel)
        assert received == [
            ChannelReply(
                sender_id="ou_owner",
                content=FEATURE_CENTER_ENTRY_COMMAND,
                reply_to_message_id="",
                message_id="feishu-menu-evt-feature-menu-1",
                chat_id="oc_private",
                source_kind="bot_menu",
            )
        ]
        for event in (
            SimpleNamespace(
                header=SimpleNamespace(event_id="evt-feature-menu-other"),
                event={
                    "event_key": FEATURE_CENTER_MENU_EVENT_KEY,
                    "operator": {"operator_id": {"open_id": "ou_other"}},
                    "chat_type": "p2p",
                    "chat_id": "oc_private",
                },
            ),
            SimpleNamespace(
                header=SimpleNamespace(event_id="evt-feature-menu-group"),
                event={
                    "event_key": FEATURE_CENTER_MENU_EVENT_KEY,
                    "operator": {"operator_id": {"open_id": "ou_owner"}},
                    "chat_type": "group",
                    "chat_id": "oc_group",
                },
            ),
            SimpleNamespace(
                header=SimpleNamespace(event_id="evt-feature-menu-unknown"),
                event={
                    "event_key": "progress_wx_unknown",
                    "operator": {"operator_id": {"open_id": "ou_owner"}},
                    "chat_type": "p2p",
                    "chat_id": "oc_private",
                },
            ),
        ):
            handler(event)
        _flush(channel)
        assert len(received) == 1
    finally:
        channel.stop()


def test_fixed_sdk_dispatcher_rebuild_keeps_feature_center_menu_registered(
    tmp_path, caplog
) -> None:
    """固定 SDK 的真实 dispatcher 重建后，p2 菜单仍只接受一次。"""

    from lark_channel.event.dispatcher_handler import EventDispatcherHandler

    class FixedSdkLifecycleChannel:
        def __init__(self) -> None:
            self._dispatcher = None
            self.build_count = 0

        def _build_dispatcher(self):
            self.build_count += 1
            return EventDispatcherHandler.builder("", "").build()

    sdk_channel = FixedSdkLifecycleChannel()
    replies: list[ChannelReply] = []
    target = FeishuMessageChannel(
        app_id="cli_test",
        app_secret="secret",
        target_open_id="ou_owner",
        sdk_factory=lambda *_args: sdk_channel,
        media_cache_dir=tmp_path / "media",
    )
    target._on_reply = replies.append

    def receive(data) -> None:
        asyncio.run(target._handle_bot_menu(data))

    feishu._register_bot_menu_event(sdk_channel, receive)
    # 同一个 channel 重复走注册入口必须保持幂等，不能替换或叠加处理器。
    feishu._register_bot_menu_event(sdk_channel, receive)
    # 模拟官方 start() 及后续同实例 dispatcher 重建；每次都必须有完整
    # p1/p2 菜单处理器，而不能只留在注册前被访问过的旧 dispatcher。
    for _ in range(3):
        sdk_channel._dispatcher = sdk_channel._build_dispatcher()
        assert {
            "p1.application.bot.menu_v6",
            "p2.application.bot.menu_v6",
        }.issubset(sdk_channel._dispatcher._processorMap)
    assert sdk_channel.build_count == 3

    def payload(*, event_id: str | None, event_key: str, sender_id: str) -> bytes:
        header = {"event_type": "application.bot.menu_v6"}
        if event_id is not None:
            header["event_id"] = event_id
        return json.dumps(
            {
                "schema": "2.0",
                "header": header,
                "event": {
                    "event_key": event_key,
                    "operator": {"operator_id": {"open_id": sender_id}},
                    "chat_type": "p2p",
                    "chat_id": "oc_private",
                },
            }
        ).encode("utf-8")

    with caplog.at_level("INFO", logger=feishu.LOGGER.name):
        sdk_channel._dispatcher._do_without_validation(
            payload(
                event_id="evt-feature-real-1",
                event_key=FEATURE_CENTER_MENU_EVENT_KEY,
                sender_id="ou_owner",
            )
        )
        for item in (
            payload(
                event_id="evt-feature-real-other",
                event_key=FEATURE_CENTER_MENU_EVENT_KEY,
                sender_id="ou_other",
            ),
            payload(
                event_id="evt-feature-real-unknown",
                event_key="progress_wx_unknown",
                sender_id="ou_owner",
            ),
            payload(
                event_id=None,
                event_key=FEATURE_CENTER_MENU_EVENT_KEY,
                sender_id="ou_owner",
            ),
        ):
            sdk_channel._dispatcher._do_without_validation(item)

    assert replies == [
        ChannelReply(
            sender_id="ou_owner",
            content=FEATURE_CENTER_ENTRY_COMMAND,
            reply_to_message_id="",
            message_id="feishu-menu-evt-feature-real-1",
            chat_id="oc_private",
            source_kind="bot_menu",
        )
    ]
    diagnostic_text = "\n".join(record.getMessage() for record in caplog.records)
    assert feishu._menu_event_id_hash("evt-feature-real-1") in diagnostic_text
    assert "evt-feature-real-1" not in diagnostic_text
    assert "ou_owner" not in diagnostic_text
    assert "oc_private" not in diagnostic_text
    assert FEATURE_CENTER_MENU_EVENT_KEY not in diagnostic_text


def test_fixed_sdk_menu_registration_fails_closed_on_processor_collision() -> None:
    """固定 SDK 已占用任一菜单键时，不留下半套 p1/p2 注册。"""

    from lark_channel.event.dispatcher_handler import EventDispatcherHandler

    class OccupiedSdkLifecycleChannel:
        def __init__(self) -> None:
            self.last_dispatcher = None

        def _build_dispatcher(self):
            dispatcher = EventDispatcherHandler.builder("", "").build()
            dispatcher._processorMap["p1.application.bot.menu_v6"] = object()
            self.last_dispatcher = dispatcher
            return dispatcher

    sdk_channel = OccupiedSdkLifecycleChannel()
    with pytest.raises(feishu.FeishuDependencyError, match="已占用"):
        feishu._register_bot_menu_event(sdk_channel, lambda _data: None)
        sdk_channel._build_dispatcher()
    assert sdk_channel.last_dispatcher is not None
    assert "p2.application.bot.menu_v6" not in sdk_channel.last_dispatcher._processorMap


def test_feature_center_operation_and_slash_card_actions_keep_event_context() -> None:
    fake = FakeSdkChannel()
    channel, received = _start(fake)
    try:
        events = (
            (
                "om_feature_card",
                "oc_private",
                feature_center_action("codex_slash"),
                None,
                "evt-feature-action",
                "斜杠指令",
                feature_center_action_fingerprint(feature_center_action("codex_slash")),
            ),
            (
                "om_monitor_card",
                "oc_private",
                feature_operation_action("monitor_refresh"),
                None,
                "evt-feature-operation",
                "监测设置",
                feature_operation_action_fingerprint(
                    feature_operation_action("monitor_refresh")
                ),
            ),
            (
                "om_slash_card",
                "oc_private",
                slash_card_action("catalog_2"),
                None,
                "evt-slash-page",
                "/commands page 2",
                slash_card_action_fingerprint(slash_card_action("catalog_2")),
            ),
            (
                "om_slash_card",
                "oc_private",
                slash_card_action("settings_form"),
                {"setting_kind": "reasoning", "setting_value": "high"},
                "evt-slash-form",
                "/reasoning high",
                slash_card_action_fingerprint(slash_card_action("settings_form")),
            ),
        )
        assert channel._loop is not None
        for (
            message_id,
            chat_id,
            value,
            form_value,
            event_id,
            content,
            fingerprint,
        ) in events:
            future = asyncio.run_coroutine_threadsafe(
                fake.handlers["cardAction"](
                    SimpleNamespace(
                        message_id=message_id,
                        chat_id=chat_id,
                        operator=SimpleNamespace(open_id="ou_owner"),
                        action=SimpleNamespace(
                            tag="button",
                            value=value,
                            form_value=form_value,
                        ),
                        raw={"header": {"event_id": event_id}},
                    )
                ),
                channel._loop,
            )
            future.result(timeout=2)
        assert [item.content for item in received] == [
            "斜杠指令",
            "监测设置",
            "/commands page 2",
            "/reasoning high",
        ]
        assert [item.reply_to_message_id for item in received] == [
            "om_feature_card",
            "om_monitor_card",
            "om_slash_card",
            "om_slash_card",
        ]
        assert [item.message_id for item in received] == [
            "feishu-card-evt-feature-action",
            "feishu-card-evt-feature-operation",
            "feishu-card-evt-slash-page",
            "feishu-card-evt-slash-form",
        ]
        assert [item.action_fingerprint for item in received] == [
            fingerprint
            for *_prefix, fingerprint in events
        ]
        assert all(item.source_kind == "card_action" for item in received)
        assert all(item.sender_id == "ou_owner" for item in received)
        assert all(item.chat_id == "oc_private" for item in received)
    finally:
        channel.stop()


def test_feature_operation_card_actions_map_all_monitor_commands() -> None:
    fake = FakeSdkChannel()
    channel, received = _start(fake)
    try:
        expected = {
            "monitor_refresh": "监测设置",
            "monitor_enable_request": "开启自动监测",
            "monitor_disable_request": "关闭自动监测",
            "monitor_enable_confirm": "确认开启自动监测",
            "monitor_disable_confirm": "确认关闭自动监测",
        }
        assert channel._loop is not None
        for index, (action, content) in enumerate(expected.items(), start=1):
            value = feature_operation_action(action)
            future = asyncio.run_coroutine_threadsafe(
                fake.handlers["cardAction"](
                    SimpleNamespace(
                        message_id="om_monitor_card",
                        chat_id="oc_private",
                        operator=SimpleNamespace(open_id="ou_owner"),
                        action=SimpleNamespace(
                            tag="button",
                            value=value,
                            form_value=None,
                        ),
                        raw={"header": {"event_id": f"evt-monitor-{index}"}},
                    )
                ),
                channel._loop,
            )
            future.result(timeout=2)
            assert received[-1].content == content
            assert received[-1].action_name == action
            assert received[-1].action_fingerprint == (
                feature_operation_action_fingerprint(value) or ""
            )
        assert len(received) == len(expected)
    finally:
        channel.stop()


def test_feature_and_slash_card_actions_fail_closed_for_forged_or_disabled_values() -> None:
    fake = FakeSdkChannel()
    channel, received = _start(fake)
    try:
        forged_values = (
            {"namespace": "progress_wx.feature_center", "version": 1, "action": "disabled_entry"},
            {"namespace": "progress_wx.feature_center", "version": 1, "action": "codex_slash", "owner": "ou_other"},
            {"namespace": "progress_wx.feature_operation", "version": 1, "action": "monitor_refresh", "extra": "forged"},
            {"namespace": "progress_wx.slash_commands", "version": 1, "action": "disabled"},
            {"namespace": "progress_wx.slash_commands", "version": 2, "action": "catalog_2"},
        )
        assert channel._loop is not None
        for index, value in enumerate(forged_values, start=1):
            future = asyncio.run_coroutine_threadsafe(
                fake.handlers["cardAction"](
                    SimpleNamespace(
                        message_id="om_old_card",
                        chat_id="oc_private",
                        operator=SimpleNamespace(open_id="ou_owner"),
                        action=SimpleNamespace(
                            tag="button",
                            value=value,
                            form_value=None,
                        ),
                        raw={"header": {"event_id": f"evt-forged-{index}"}},
                    )
                ),
                channel._loop,
            )
            future.result(timeout=2)
        invalid_form = asyncio.run_coroutine_threadsafe(
            fake.handlers["cardAction"](
                SimpleNamespace(
                    message_id="om_old_card",
                    chat_id="oc_private",
                    operator=SimpleNamespace(open_id="ou_owner"),
                    action=SimpleNamespace(
                        tag="button",
                        value=slash_card_action("settings_form"),
                        form_value={"setting_kind": "unknown", "setting_value": "x"},
                    ),
                    raw={"header": {"event_id": "evt-invalid-form"}},
                )
            ),
            channel._loop,
        )
        invalid_form.result(timeout=2)
        for index, value in enumerate(
            (
                feature_center_action("codex_slash"),
                feature_operation_action("monitor_refresh"),
            ),
            start=1,
        ):
            forged_form = asyncio.run_coroutine_threadsafe(
                fake.handlers["cardAction"](
                    SimpleNamespace(
                        message_id="om_old_card",
                        chat_id="oc_private",
                        operator=SimpleNamespace(open_id="ou_owner"),
                        action=SimpleNamespace(
                            tag="button",
                            value=value,
                            form_value={"forged": "input"},
                        ),
                        raw={"header": {"event_id": f"evt-forged-form-{index}"}},
                    )
                ),
                channel._loop,
            )
            forged_form.result(timeout=2)
        assert received == []
    finally:
        channel.stop()


def test_exact_private_quoted_reply_is_forwarded_once() -> None:
    fake = FakeSdkChannel()
    channel, received = _start(fake)
    try:
        message = FakeInbound()
        _emit(channel, fake, message)
        _emit(channel, fake, message)
        assert received == [
            ChannelReply(
                sender_id="ou_owner",
                content="继续",
                reply_to_message_id="om_notice",
                message_id="om_reply",
                chat_id="oc_private",
            )
        ]
    finally:
        channel.stop()


def test_p2p_parent_equal_to_root_is_recovered_from_raw_event() -> None:
    """SDK 清空 p2p 的 reply 属性时，原始 parent_id 仍可精确关联。"""

    fake = FakeSdkChannel()
    channel, received = _start(fake)
    try:
        _emit(
            channel,
            fake,
            FakeInbound(
                reply_to_message_id=None,
                raw={"parent_id": "om_notice", "root_id": "om_notice"},
            ),
        )
        assert len(received) == 1
        assert received[0].reply_to_message_id == "om_notice"
    finally:
        channel.stop()


def test_batched_plain_message_is_kept_separate_from_quoted_reply() -> None:
    """连续普通消息与引用回复必须逐条转发，不能把正文错误合并。"""

    fake = FakeSdkChannel()
    channel, received = _start(fake)
    plain = FakeInbound(
        message_id="om_plain",
        reply_to_message_id=None,
        safe_content_text="普通消息不得进入 Codex",
        content_text="普通消息不得进入 Codex",
    )
    quoted = FakeInbound(
        message_id="om_quoted",
        reply_to_message_id=None,
        safe_content_text="回传成功",
        content_text="回传成功",
        raw={"parent_id": "om_notice", "root_id": "om_notice"},
    )
    merged = FakeInbound(
        message_id="om_quoted",
        reply_to_message_id=None,
        safe_content_text="普通消息不得进入 Codex\n\n回传成功",
        content_text="普通消息不得进入 Codex\n\n回传成功",
        batched_sources=[plain, quoted],
    )
    try:
        _emit(channel, fake, merged)
        assert [item.message_id for item in received] == ["om_plain", "om_quoted"]
        assert received[0].reply_to_message_id == ""
        assert received[0].content == "普通消息不得进入 Codex"
        assert received[1].reply_to_message_id == "om_notice"
        assert received[1].content == "回传成功"
    finally:
        channel.stop()


@pytest.mark.parametrize(
    "message",
    [
        FakeInbound(sender_id="ou_other"),
        FakeInbound(chat_type="group"),
        FakeInbound(raw_content_type="image"),
        FakeInbound(sender_is_bot=True),
    ],
)
def test_other_user_group_and_non_text_messages_are_ignored(message: FakeInbound) -> None:
    fake = FakeSdkChannel()
    channel, received = _start(fake)
    try:
        _emit(channel, fake, message)
        assert received == []
    finally:
        channel.stop()


def test_quoted_image_is_cached_and_forwarded_as_verified_attachment(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "cached-image.jpg"
    image_path.write_bytes(b"verified-image")
    resource = SimpleNamespace(type="image", file_key="img-key")
    fake = FakeSdkChannel()
    fake.cache_results = [
        SimpleNamespace(
            decision="cached",
            reason=None,
            path=image_path,
            mime_type="image/jpeg",
            size=image_path.stat().st_size,
            sha256="a" * 64,
        )
    ]
    received: list[ChannelReply] = []
    channel = FeishuMessageChannel(
        app_id="cli_test",
        app_secret="secret",
        target_open_id="ou_owner",
        media_cache_dir=tmp_path,
        retry_delays=(0, 0, 0, 0, 0),
        sdk_factory=lambda *_args: fake,
    )
    channel.start(received.append)
    try:
        _emit(
            channel,
            fake,
            FakeInbound(
                raw_content_type="image",
                safe_content_text="![image](img-key)",
                content_text="![image](img-key)",
                resources=[resource],
            ),
        )
        assert fake.resolve_calls == [("om_reply", [resource])]
        assert received == [
            ChannelReply(
                sender_id="ou_owner",
                content="",
                reply_to_message_id="om_notice",
                message_id="om_reply",
                chat_id="oc_private",
                attachments=(
                    ChannelAttachment(
                        str(image_path.resolve()),
                        "image/jpeg",
                        "a" * 64,
                        image_path.stat().st_size,
                    ),
                ),
            )
        ]
    finally:
        channel.stop()


def test_quoted_post_image_and_text_are_forwarded_together_without_staging(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "combined-image.png"
    image_path.write_bytes(b"verified-combined-image")
    resource = SimpleNamespace(type="image", file_key="img-combined")
    fake = FakeSdkChannel()
    fake.cache_results = [
        SimpleNamespace(
            decision="cached",
            reason=None,
            path=image_path,
            mime_type="image/png",
            size=image_path.stat().st_size,
            sha256="d" * 64,
        )
    ]
    received: list[ChannelReply] = []
    channel = FeishuMessageChannel(
        app_id="cli_test",
        app_secret="secret",
        target_open_id="ou_owner",
        media_cache_dir=tmp_path,
        retry_delays=(0, 0, 0, 0, 0),
        sdk_factory=lambda *_args: fake,
    )
    channel.start(received.append)
    try:
        _emit(
            channel,
            fake,
            FakeInbound(
                raw_content_type="post",
                safe_content_text="请直接分析这张图片",
                content_text="请直接分析这张图片",
                resources=[resource],
            ),
        )
        assert fake.resolve_calls == [("om_reply", [resource])]
        assert received == [
            ChannelReply(
                sender_id="ou_owner",
                content="请直接分析这张图片",
                reply_to_message_id="om_notice",
                message_id="om_reply",
                chat_id="oc_private",
                attachments=(
                    ChannelAttachment(
                        str(image_path.resolve()),
                        "image/png",
                        "d" * 64,
                        image_path.stat().st_size,
                    ),
                ),
            )
        ]
    finally:
        channel.stop()


def test_download_failed_names_required_message_read_permission() -> None:
    resource = SimpleNamespace(type="image", file_key="img-denied")
    fake = FakeSdkChannel()
    fake.cache_results = [
        SimpleNamespace(
            decision="rejected",
            reason="download_failed",
            path=None,
            mime_type=None,
            size=None,
            sha256=None,
        )
    ]
    channel, received = _start(fake)
    try:
        _emit(
            channel,
            fake,
            FakeInbound(
                raw_content_type="post",
                safe_content_text="请分析图片",
                content_text="请分析图片",
                resources=[resource],
            ),
        )
        assert len(received) == 1
        assert received[0].attachments == ()
        assert "im:message:readonly" in received[0].attachment_error
        assert "im:resource 不能替代" in received[0].attachment_error
    finally:
        channel.stop()


def test_unquoted_image_is_not_downloaded_and_outside_cache_is_rejected(
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"outside")
    resource = SimpleNamespace(type="image", file_key="img-key")
    fake = FakeSdkChannel()
    fake.cache_results = [
        SimpleNamespace(
            decision="cached",
            reason=None,
            path=outside,
            mime_type="image/jpeg",
            size=outside.stat().st_size,
            sha256="b" * 64,
        )
    ]
    received: list[ChannelReply] = []
    channel = FeishuMessageChannel(
        app_id="cli_test",
        app_secret="secret",
        target_open_id="ou_owner",
        media_cache_dir=cache_dir,
        retry_delays=(0, 0, 0, 0, 0),
        sdk_factory=lambda *_args: fake,
    )
    channel.start(received.append)
    try:
        _emit(
            channel,
            fake,
            FakeInbound(
                message_id="om_unquoted",
                reply_to_message_id=None,
                raw_content_type="image",
                resources=[resource],
            ),
        )
        assert fake.resolve_calls == []

        _emit(
            channel,
            fake,
            FakeInbound(
                message_id="om_outside",
                raw_content_type="image",
                resources=[resource],
            ),
        )
        assert len(received) == 1
        assert received[0].attachments == ()
        assert "安全下载或格式校验" in received[0].attachment_error
    finally:
        channel.stop()


def test_whitelisted_plain_text_is_forwarded_without_trimming() -> None:
    fake = FakeSdkChannel()
    channel, received = _start(fake)
    try:
        _emit(
            channel,
            fake,
            FakeInbound(
                message_id="om_command",
                reply_to_message_id=None,
                safe_content_text="  查询项目列表\n",
                content_text="  查询项目列表\n",
            ),
        )
        assert received == [
            ChannelReply(
                sender_id="ou_owner",
                content="  查询项目列表\n",
                message_id="om_command",
                chat_id="oc_private",
            )
        ]
    finally:
        channel.stop()


def test_send_uses_exact_open_id_and_stable_uuid() -> None:
    fake = FakeSdkChannel()
    channel, _received = _start(fake)
    try:
        first = channel.send_text("通知", idempotency_key="thread:turn")
        second = channel.send_text("通知", idempotency_key="thread:turn")
        assert first == "om_sent_1"
        assert second == "om_sent_2"
        assert [item[0] for item in fake.sent] == ["ou_owner", "ou_owner"]
        assert fake.sent[0][2]["receive_id_type"] == "open_id"
        assert fake.sent[0][2]["uuid"] == fake.sent[1][2]["uuid"]
    finally:
        channel.stop()


def test_send_file_uses_original_bytes_name_and_stable_uuid() -> None:
    fake = FakeSdkChannel()
    channel, _received = _start(fake)
    try:
        original = b"\x89PNG\r\n\x1a\noriginal-image-bytes"
        first = channel.send_file(
            original,
            file_name="generated.png",
            idempotency_key="thread:turn:image:item",
        )
        second = channel.send_file(
            original,
            file_name="generated.png",
            idempotency_key="thread:turn:image:item",
        )
        assert first == "om_sent_1"
        assert second == "om_sent_2"
        assert fake.sent[0][1] == {
            "file": {"source": original, "file_name": "generated.png"}
        }
        assert fake.sent[0][2]["receive_id_type"] == "open_id"
        assert fake.sent[0][2]["uuid"] == fake.sent[1][2]["uuid"]
    finally:
        channel.stop()


def test_send_image_uses_previewable_image_message_and_stable_uuid() -> None:
    fake = FakeSdkChannel()
    channel, _received = _start(fake)
    try:
        original = b"\x89PNG\r\n\x1a\npreview-image-bytes"
        first = channel.send_image(
            original,
            idempotency_key="thread:turn:image:item",
        )
        second = channel.send_image(
            original,
            idempotency_key="thread:turn:image:item",
        )
        assert first == "om_sent_1"
        assert second == "om_sent_2"
        assert fake.sent[0][1] == {"image": {"source": original}}
        assert fake.sent[0][2]["receive_id_type"] == "open_id"
        assert fake.sent[0][2]["uuid"] == fake.sent[1][2]["uuid"]
    finally:
        channel.stop()


def test_send_result_preserves_safe_permanent_rejection_metadata() -> None:
    result = SimpleNamespace(
        success=False,
        error=SimpleNamespace(
            code=SimpleNamespace(value="permission_denied"),
            raw_code=99991672,
            retryable=False,
        ),
    )
    with pytest.raises(feishu.FeishuSendRejectedError) as captured:
        FeishuMessageChannel._message_ids_from_send_result(result)
    assert captured.value.code == "permission_denied"
    assert captured.value.raw_code == 99991672
    assert captured.value.retryable is False


def test_send_result_classifies_card_format_rejection_for_safe_text_fallback() -> None:
    result = SimpleNamespace(
        success=False,
        error=SimpleNamespace(
            code=SimpleNamespace(value="format_error"),
            raw_code=230099,
            retryable=False,
        ),
    )
    with pytest.raises(feishu.FeishuPayloadRejectedError) as captured:
        FeishuMessageChannel._message_ids_from_send_result(result)
    assert captured.value.code == "format_error"
    assert captured.value.raw_code == 230099
    assert captured.value.retryable is False


def test_notification_and_reply_labels_are_bold_but_values_are_plain() -> None:
    fake = FakeSdkChannel()
    channel, _received = _start(fake)
    try:
        channel.send_text(
            "对话名称：会话 **值不解析**\n\n"
            "当前进度：完成\n\n"
            "本轮完成：\n- 第一项\n\n"
            "本条消息时间：2026-08-25 14:00:00（北京时间）",
            idempotency_key="thread:styled-turn",
        )
        channel.send_text(
            "消息状态：已收到\n\n回复信息：已经转交",
            idempotency_key="reply-receipt:styled",
        )
        notification = fake.sent[0][1]["post"]["zh_cn"]["content"]
        receipt = fake.sent[1][1]["post"]["zh_cn"]["content"]
        expected_labels = ["对话名称：", "当前进度：", "本轮完成：", "本条消息时间："]
        styled_rows = [row for row in notification if row[0]["text"] in expected_labels]
        assert [row[0]["text"] for row in styled_rows] == expected_labels
        assert all(row[0]["style"] == ["bold"] for row in styled_rows)
        assert [index for index, row in enumerate(notification) if row == [{"tag": "text", "text": "\u00a0"}]] == [1, 3, 6]
        assert styled_rows[0][1] == {"tag": "text", "text": "会话 **值不解析**"}
        assert styled_rows[1][1] == {"tag": "text", "text": "完成"}
        assert receipt[0][0] == {"tag": "text", "text": "消息状态：", "style": ["bold"]}
        assert receipt[0][1] == {"tag": "text", "text": "已收到"}
        assert receipt[1] == [{"tag": "text", "text": "\u00a0"}]
        assert receipt[2][0] == {"tag": "text", "text": "回复信息：", "style": ["bold"]}
        assert receipt[2][1] == {"tag": "text", "text": "已经转交"}
    finally:
        channel.stop()


def test_information_rich_summary_section_labels_are_bold() -> None:
    fake = FakeSdkChannel()
    channel, _received = _start(fake)
    try:
        channel.send_text(
            "对话名称：飞书机器人开发\n\n"
            "当前进度：完成\n\n"
            "本轮完成：\n- 已完成摘要改造\n"
            "关键结果：\n- 保留详细信息\n"
            "剩余事项：\n- 无\n"
            "需要你处理：\n- 无需\n\n"
            "本条消息时间：2026-08-26 01:40:00（北京时间）",
            idempotency_key="thread:rich-summary",
        )

        rows = fake.sent[0][1]["post"]["zh_cn"]["content"]
        section_labels = {
            "本轮完成：",
            "关键结果：",
            "剩余事项：",
            "需要你处理：",
        }
        styled = {
            row[0]["text"]
            for row in rows
            if row and row[0].get("text") in section_labels
            and row[0].get("style") == ["bold"]
        }
        assert styled == section_labels
    finally:
        channel.stop()


def test_management_list_sections_are_bold_and_entries_remain_plain() -> None:
    fake = FakeSdkChannel()
    channel, _received = _start(fake)
    try:
        channel.send_text(
            "列表类型：Codex 个人会话\n"
            "页码：第 1/3 页\n"
            "总数：55 个\n\n"
            "会话列表：\n"
            "p01｜飞书机器人开发\n\n"
            "操作说明：\n"
            "- 选择会话：回复“选定p01”",
            idempotency_key="management:list-style",
        )
        rows = fake.sent[0][1]["post"]["zh_cn"]["content"]
        expected = {"列表类型：", "页码：", "总数：", "会话列表：", "操作说明："}
        styled = {
            row[0]["text"]
            for row in rows
            if row and row[0].get("style") == ["bold"]
        }
        assert expected <= styled
        entry = next(row for row in rows if row[0]["text"] == "p01｜飞书机器人开发")
        assert "style" not in entry[0]
    finally:
        channel.stop()


def test_session_search_post_bolds_only_labels_and_preserves_block_spacing() -> None:
    payload = feishu._rich_post_or_text(
        "列表类型：会话搜索候选\n"
        "搜索范围：最近5天\n"
        "页码：第 1/1 页\n"
        "总数：1 个\n\n"
        "会话列表：\n\n"
        "1｜《合成会话》\n"
        "匹配度：88%\n"
        "匹配说明：参考 [说明](https://example.invalid/help)，回复 `选择1`。\n\n"
        "操作说明：\n"
        "- 选择会话：回复“选择1”"
    )

    rows = payload["post"]["zh_cn"]["content"]
    blank_rows = [row for row in rows if row == [{"tag": "text", "text": "\u00a0"}]]
    assert len(blank_rows) == 3
    expected_labels = {
        "列表类型：",
        "搜索范围：",
        "页码：",
        "总数：",
        "会话列表：",
        "匹配度：",
        "匹配说明：",
        "操作说明：",
    }
    styled_labels = {
        row[0]["text"]
        for row in rows
        if row and row[0].get("style") == ["bold"]
    }
    assert styled_labels == expected_labels
    for row in rows:
        if row and row[0].get("style") == ["bold"] and len(row) == 2:
            assert "style" not in row[1]
    title_row = next(row for row in rows if row[0]["text"] == "1｜《合成会话》")
    assert "style" not in title_row[0]
    reason_row = next(row for row in rows if row[0]["text"] == "匹配说明：")
    assert reason_row[1]["text"] == "参考 [说明](https://example.invalid/help)，回复 `选择1`。"
    assert "style" not in reason_row[1]


def test_send_returns_every_sdk_chunk_message_id() -> None:
    fake = FakeSdkChannel()
    fake.chunk_ids = ["om_chunk_1", "om_chunk_2", "om_chunk_3"]
    channel, _received = _start(fake)
    try:
        assert channel.send_text("长通知", idempotency_key="thread:long-turn") == (
            "om_chunk_1",
            "om_chunk_2",
            "om_chunk_3",
        )
        assert channel.recipient_scope_for_messages(
            ("om_chunk_1", "om_chunk_2", "om_chunk_3")
        ) == ("ou_owner", "oc_private")
    finally:
        channel.stop()


def test_initial_transient_connection_failure_keeps_supervisor_alive_and_recovers() -> None:
    created: list[FakeSdkChannel] = []

    def factory(*_args) -> FakeSdkChannel:
        item = FakeSdkChannel(
            connect_error=OSError("offline") if not created else None
        )
        created.append(item)
        return item

    channel = FeishuMessageChannel(
        app_id="cli_test",
        app_secret="secret",
        target_open_id="ou_owner",
        connect_timeout_seconds=1,
        retry_delays=(0, 0, 0, 0, 0),
        sdk_factory=factory,
    )
    try:
        channel.start(lambda _reply: None)
        # 初始断网只表示监督已启动；不得把一次暂时离线升级为 start 失败。
        assert channel.connection_snapshot()["state"] == "offline"
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not channel.is_online():
            time.sleep(0.02)
        assert channel.is_online() is True
        assert len(created) >= 2
        assert channel.connection_snapshot()["ever_connected"] is True
        assert set(channel.connection_snapshot()) == {
            "state",
            "last_failure_class",
            "last_failure_type",
            "consecutive_failures",
            "retry_in_seconds",
            "ever_connected",
            "last_transition_at",
            "thread_alive",
        }
        assert channel.connection_snapshot()["thread_alive"] is True
        assert "secret" not in repr(channel.connection_snapshot())
    finally:
        channel.stop()
    assert channel.is_online() is False


def test_initial_credential_failure_still_fails_closed() -> None:
    class CredentialError(RuntimeError):
        code = 99991672

    errors: list[BaseException] = []
    channel = FeishuMessageChannel(
        app_id="cli_test",
        app_secret="secret",
        target_open_id="ou_owner",
        connect_timeout_seconds=1,
        error_handler=errors.append,
        sdk_factory=lambda *_args: FakeSdkChannel(
            connect_error=CredentialError("redacted")
        ),
    )
    try:
        with pytest.raises(MessageChannelOfflineError):
            channel.start(lambda _reply: None)
        assert channel.connection_snapshot()["state"] == "failed"
        assert errors == []
    finally:
        channel.stop()


def test_persistent_initial_network_failure_has_backoff_and_prompt_stop() -> None:
    created: list[FakeSdkChannel] = []

    def factory(*_args) -> FakeSdkChannel:
        item = FakeSdkChannel(connect_error=OSError("offline"))
        created.append(item)
        return item

    channel = FeishuMessageChannel(
        app_id="cli_test",
        app_secret="secret",
        target_open_id="ou_owner",
        connect_timeout_seconds=1,
        retry_delays=(0, 0, 0, 0, 0),
        sdk_factory=factory,
    )
    channel.start(lambda _reply: None)
    try:
        time.sleep(0.75)
        # 最小退避为 0.2 秒；即使调度偏差也不应出现毫秒级忙循环。
        assert 1 <= len(created) <= 8
        assert channel.connection_snapshot()["state"] in {"offline", "connecting"}
    finally:
        started = time.monotonic()
        channel.stop()
        assert time.monotonic() - started < 3


def test_stop_does_not_disconnect_sdk_concurrently_or_twice() -> None:
    """worker finally 与外部 stop 同时发生时，SDK 只能收到一次 disconnect。"""

    fake = FakeSdkChannel()
    channel, _received = _start(fake)
    channel.stop()
    channel.stop()
    assert fake.disconnect_calls == 1


def test_disconnect_marker_follows_each_sdk_instance() -> None:
    """清理标记属于对象本身，后续创建的新通道仍必须执行断开。"""

    channel = FeishuMessageChannel(
        app_id="cli_test",
        app_secret="secret",
        target_open_id="ou_owner",
        sdk_factory=lambda *_args: FakeSdkChannel(),
    )
    first = FakeSdkChannel()
    second = FakeSdkChannel()

    async def disconnect_all() -> None:
        await channel._disconnect(first)
        await channel._disconnect(first)
        await channel._disconnect(second)

    asyncio.run(disconnect_all())
    assert first.disconnect_calls == 1
    assert second.disconnect_calls == 1


def test_sdk_task_drain_cancels_pending_task_without_leak() -> None:
    """清理私有 WS loop 时应驱动取消完成，不能留下 pending task。"""

    loop = asyncio.new_event_loop()

    async def pending() -> None:
        await asyncio.sleep(60)

    task = loop.create_task(pending())
    loop.run_until_complete(asyncio.sleep(0))
    asyncio.run(FeishuMessageChannel._drain_sdk_tasks(loop, (task,)))
    assert task.done()
    assert task.cancelled()
    loop.close()


def test_expiring_cache_private_loop_is_collected_as_sdk_task_group() -> None:
    """SDK 缓存另建的从未运行 loop 也必须纳入退出回收。"""

    cache_loop = asyncio.new_event_loop()

    async def cache_cron() -> None:
        await asyncio.sleep(60)

    cron = cache_loop.create_task(cache_cron())
    raw_channel = SimpleNamespace(
        _ws_client=SimpleNamespace(
            _loop=None,
            _cache=SimpleNamespace(_cron=cron),
        )
    )
    ws_loop, groups = FeishuMessageChannel._snapshot_sdk_task_groups(raw_channel)
    assert ws_loop is None
    assert groups == ((cache_loop, (cron,)),)
    asyncio.run(FeishuMessageChannel._drain_sdk_tasks(cache_loop, (cron,)))
    assert cron.cancelled()
    cache_loop.close()


def test_official_sdk_executor_reuse_replaces_closed_cache_loop(monkeypatch) -> None:
    """A retry on the same executor thread must not inherit its closed loop."""

    channel = feishu._official_sdk_factory(
        "cli_test",
        "secret",
        "ou_owner",
        1,
    )
    base = type(channel).__mro__[1]
    observed: list[asyncio.AbstractEventLoop] = []

    def fake_start(_self) -> None:
        current = asyncio.get_event_loop()
        assert current.is_closed() is False
        observed.append(current)

    monkeypatch.setattr(base, "start", fake_start)

    def run_twice_on_one_worker() -> tuple[int, int]:
        stale = asyncio.new_event_loop()
        asyncio.set_event_loop(stale)
        stale_id = id(stale)
        stale.close()
        channel.start()
        replacement = asyncio.get_event_loop()
        replacement_id = id(replacement)
        replacement.close()
        return stale_id, replacement_id

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        stale_id, replacement_id = executor.submit(run_twice_on_one_worker).result(
            timeout=3
        )

    assert len(observed) == 1
    assert id(observed[0]) == replacement_id
    assert replacement_id != stale_id
    assert observed[0].is_closed() is True


def test_official_sdk_replaces_closed_module_loop_without_touching_open_loop(
    monkeypatch,
) -> None:
    from lark_channel.ws import client as ws_client_module

    closed = asyncio.new_event_loop()
    closed.close()
    monkeypatch.setattr(ws_client_module, "loop", closed)
    replacement = feishu._ensure_sdk_ws_loop_open(ws_client_module)
    assert replacement is ws_client_module.loop
    assert replacement.is_closed() is False
    assert feishu._ensure_sdk_ws_loop_open(ws_client_module) is replacement
    replacement.close()


def test_official_sdk_exact_closed_loop_error_is_transient_but_unknown_runtime_is_not(
    monkeypatch,
) -> None:
    channel = feishu._official_sdk_factory("cli_test", "secret", "ou_owner", 1)
    base = type(channel).__mro__[1]

    def closed_loop(_self) -> None:
        raise RuntimeError("Event loop is closed")

    monkeypatch.setattr(base, "start", closed_loop)
    with pytest.raises(MessageChannelOfflineError):
        channel.start()

    def unknown(_self) -> None:
        raise RuntimeError("unrelated invariant failure")

    monkeypatch.setattr(base, "start", unknown)
    with pytest.raises(RuntimeError, match="unrelated invariant failure"):
        channel.start()


def test_shutdown_serialization_only_patches_official_sdk_instance() -> None:
    """兼容补丁不得改变假通道或未来其他消息后端的清理方法。"""

    class OfficialFake:
        def _cleanup_failed_start(self, _generation) -> None:
            raise AssertionError("官方重复清理分支不应再执行")

    OfficialFake.__module__ = "lark_channel.channel.channel"
    official = OfficialFake()
    FeishuMessageChannel._serialize_official_sdk_shutdown(official)
    official._cleanup_failed_start(1)

    ordinary = SimpleNamespace(_cleanup_failed_start=lambda _generation: "kept")
    FeishuMessageChannel._serialize_official_sdk_shutdown(ordinary)
    assert ordinary._cleanup_failed_start(1) == "kept"


def test_runtime_disconnect_reconnects_without_global_error() -> None:
    """运行中断线持续退避重连，不再因有限次数耗尽停止服务。"""

    first = FakeSdkChannel()
    created: list[FakeSdkChannel] = []
    errors: list[BaseException] = []

    def factory(*_args) -> FakeSdkChannel:
        item = first if not created else FakeSdkChannel()
        created.append(item)
        return item

    def on_error(error: BaseException) -> None:
        errors.append(error)

    channel = FeishuMessageChannel(
        app_id="cli_test",
        app_secret="secret",
        target_open_id="ou_owner",
        connect_timeout_seconds=1,
        max_attempts=3,
        retry_delays=(0, 0, 0),
        error_handler=on_error,
        sdk_factory=factory,
    )
    channel.start(lambda _reply: None)
    try:
        # 模拟已在线连接从服务端断开，监督线程会开始有限重连。
        first.ws_client._conn = None
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not channel.is_online():
            time.sleep(0.02)
        assert channel.is_online() is True
        assert len(created) >= 2
        assert errors == []
        assert channel.connection_snapshot()["state"] == "online"
    finally:
        channel.stop()


def test_runtime_closed_executor_loop_is_retried_then_recovers() -> None:
    """SDK 构造区抛出的精确 closed-loop 错误不能升级为全局 fatal。"""

    first = FakeSdkChannel()
    created: list[FakeSdkChannel] = []
    errors: list[BaseException] = []

    def factory(*_args) -> FakeSdkChannel:
        if not created:
            item = first
        elif len(created) == 1:
            item = FakeSdkChannel(connect_error=RuntimeError("Event loop is closed"))
        else:
            item = FakeSdkChannel()
        created.append(item)
        return item

    channel = FeishuMessageChannel(
        app_id="cli_test",
        app_secret="secret",
        target_open_id="ou_owner",
        connect_timeout_seconds=1,
        retry_delays=(0, 0, 0, 0, 0),
        error_handler=errors.append,
        sdk_factory=factory,
    )
    channel.start(lambda _reply: None)
    try:
        first.ws_client._conn = None
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and (
            len(created) < 3 or not channel.is_online()
        ):
            time.sleep(0.02)
        assert len(created) >= 3
        assert channel.is_online() is True
        assert errors == []
        assert channel.connection_snapshot()["state"] == "online"
    finally:
        channel.stop()


def test_channel_stop_start_uses_new_sdk_and_keeps_message_id_exactly_once() -> None:
    """同一 wrapper 重启后使用新 SDK；旧飞书事件 ID 仍不重复回调。"""

    created: list[FakeSdkChannel] = []

    def factory(*_args) -> FakeSdkChannel:
        item = FakeSdkChannel()
        created.append(item)
        return item

    received: list[ChannelReply] = []
    channel = FeishuMessageChannel(
        app_id="cli_test",
        app_secret="secret",
        target_open_id="ou_owner",
        connect_timeout_seconds=1,
        retry_delays=(0, 0, 0, 0, 0),
        sdk_factory=factory,
    )
    channel.start(received.append)
    first = created[-1]
    _emit(channel, first, FakeInbound(message_id="om_once"))
    channel.stop()

    channel.start(received.append)
    try:
        second = created[-1]
        assert second is not first
        _emit(channel, second, FakeInbound(message_id="om_once"))
        _emit(channel, second, FakeInbound(message_id="om_new", safe_content_text="继续2", content_text="继续2"))
        assert [item.message_id for item in received] == ["om_once", "om_new"]
    finally:
        channel.stop()


def test_runtime_unknown_sdk_factory_failure_remains_fail_closed() -> None:
    first = FakeSdkChannel()
    calls = 0
    errors: list[BaseException] = []
    failed = threading.Event()

    def factory(*_args):
        nonlocal calls
        calls += 1
        if calls == 1:
            return first
        raise RuntimeError("factory unavailable")

    def on_error(error: BaseException) -> None:
        errors.append(error)
        failed.set()

    channel = FeishuMessageChannel(
        app_id="cli_test",
        app_secret="secret",
        target_open_id="ou_owner",
        connect_timeout_seconds=1,
        max_attempts=3,
        retry_delays=(0, 0, 0),
        error_handler=on_error,
        sdk_factory=factory,
    )
    channel.start(lambda _reply: None)
    try:
        first.ws_client._conn = None
        assert failed.wait(3)
        assert calls == 2  # 首次成功 + 一次未知错误；不作无界重试
        assert len(errors) == 1
        assert isinstance(errors[0], RuntimeError)
        assert channel.connection_snapshot()["state"] == "failed"
    finally:
        channel.stop()


def test_fetch_message_uses_official_async_driver_and_app_id_is_trusted() -> None:
    fake = FakeSdkChannel()
    fetched: list[str] = []

    async def fetch_message(message_id: str):
        fetched.append(message_id)
        return {"code": 0, "data": {"items": []}}

    fake.fetch_message = fetch_message  # type: ignore[attr-defined]
    channel, _received = _start(fake)
    try:
        assert channel.fetch_message("om_parent") == {
            "code": 0,
            "data": {"items": []},
        }
        assert fetched == ["om_parent"]
        # SDK bot_identity may still be None immediately after connect; the
        # configured app_id remains the authoritative app sender identity.
        assert channel.bot_sender_ids() == ("cli_test",)
    finally:
        channel.stop()
