"""飞书消息渠道的离线测试；不访问飞书网络。"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from progress_wx import feishu
from progress_wx.channel import ChannelAttachment, ChannelReply, MessageChannelOfflineError
from progress_wx.feishu import FeishuMessageChannel


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

    def on(self, name: str, handler) -> None:
        self.handlers[name] = handler

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
    finally:
        channel.stop()


def test_initial_connection_failure_is_reported_without_background_leak() -> None:
    fake = FakeSdkChannel(connect_error=OSError("offline"))
    channel = FeishuMessageChannel(
        app_id="cli_test",
        app_secret="secret",
        target_open_id="ou_owner",
        connect_timeout_seconds=5,
        sdk_factory=lambda *_args: fake,
    )
    with pytest.raises(MessageChannelOfflineError):
        channel.start(lambda _reply: None)
    channel.stop()
    assert channel.is_online() is False


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


def test_runtime_disconnect_stops_after_exactly_configured_reconnect_attempts() -> None:
    """首次掉线不算重试；其后恰好尝试三次，失败后只上报一次致命错误。"""

    first = FakeSdkChannel()
    created: list[FakeSdkChannel] = []
    errors: list[BaseException] = []
    failed = threading.Event()

    def factory(*_args) -> FakeSdkChannel:
        if not created:
            item = first
        else:
            item = FakeSdkChannel(connect_error=OSError("offline"))
        created.append(item)
        return item

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
        # 模拟已在线连接从服务端断开，监督线程会开始有限重连。
        first.ws_client._conn = None
        assert failed.wait(3)
        assert len(created) == 4  # 首次连接 + 三次真正的重连尝试
        assert len(errors) == 1
        assert isinstance(errors[0], MessageChannelOfflineError)
        assert "连续 3 次" in str(errors[0])
        assert channel.is_online() is False
    finally:
        channel.stop()


def test_runtime_sdk_factory_failures_use_same_bounded_reconnect_path() -> None:
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
        assert calls == 4  # 首次成功 + 三次创建 SDK 的重连尝试
        assert len(errors) == 1
        assert "连续 3 次" in str(errors[0])
    finally:
        channel.stop()
