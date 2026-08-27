"""微信适配层测试；全部使用 fake，不需要安装 wxautox4 或启动微信。"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.progress_wx.wechat import (
    AdapterCapabilityError,
    FriendVerificationError,
    QuoteMessage,
    ToolAccountVerificationError,
    WechatService,
    WxAutoX4Adapter,
    extract_quote_message,
)


@dataclass
class FakeMessage:
    """模拟 wxauto Message 的结构化字段。"""

    attr: str = "friend"
    type: str = "quote"
    content: str = "继续执行"
    quote_content: str = "请检查测试"
    quote_nickname: str = "Codex小号"
    sender: str = "wxid_main"
    chat_type: str = "friend"
    id: int = 101
    hash: int = 202


class FakeAdapter:
    """可控的内存微信适配器。"""

    def __init__(self, *, details=None, online: bool = True, account_id: str = "wxid_tool") -> None:
        self.details = {"wxid": "wxid_main"} if details is None else details
        self.online = online
        self.account_id = account_id
        self.account_calls: list[str] = []
        self.verify_calls: list[tuple[str, str]] = []
        self.start_calls: list[str] = []
        self.stop_calls: list[str] = []
        self.sent: list[tuple[str, str]] = []
        self.callback = None

    def verify_account(self, wechat_id: str) -> bool:
        self.account_calls.append(wechat_id)
        return self.account_id == wechat_id

    def verify_friend(self, chat_name: str, wechat_id: str) -> bool:
        self.verify_calls.append((chat_name, wechat_id))
        if isinstance(self.details, BaseException):
            raise self.details
        if isinstance(self.details, list):
            return len(self.details) == 1 and self.details[0].get("wxid") == wechat_id
        return self.details.get("wxid") == wechat_id

    def send_text(self, chat_name: str, text: str) -> None:
        self.sent.append((chat_name, text))

    def start_listening(self, chat_name: str, callback) -> None:
        self.start_calls.append(chat_name)
        self.callback = callback

    def stop_listening(self, chat_name: str) -> None:
        self.stop_calls.append(chat_name)

    def is_online(self) -> bool:
        return self.online

    def emit(self, message, chat_name: str = "Codex小号") -> None:
        assert self.callback is not None
        self.callback(message, chat_name)


class RetryStopAdapter(FakeAdapter):
    """第一次停止失败、第二次成功的清理适配器。"""

    def __init__(self) -> None:
        super().__init__()
        self.fail_stop = True

    def stop_listening(self, chat_name: str) -> None:
        self.stop_calls.append(chat_name)
        if self.fail_stop:
            self.fail_stop = False
            raise RuntimeError("暂时无法停止")


def test_quote_reply_is_extracted_and_forwarded() -> None:
    """好友引用消息应完整提取字段，普通发送能力也应保持可用。"""

    adapter = FakeAdapter()
    received: list[QuoteMessage] = []
    service = WechatService(
        adapter,
        tool_wechat_id="wxid_tool",
        chat_name="Codex小号",
        target_wechat_id="wxid_main",
    )

    service.start(received.append)
    adapter.emit(FakeMessage())
    service.send_text("本轮进度：完成")

    assert len(received) == 1
    event = received[0]
    assert event.chat_name == "Codex小号"
    assert event.content == "继续执行"
    assert event.quote_content == "请检查测试"
    assert event.quote_nickname == "Codex小号"
    assert event.sender == "wxid_main"
    assert event.message_id == 101
    assert event.message_hash == 202
    assert adapter.sent == [("Codex小号", "本轮进度：完成")]
    assert adapter.verify_calls == [
        ("Codex小号", "wxid_main"),
        ("Codex小号", "wxid_main"),
        ("Codex小号", "wxid_main"),
    ]
    # 启动、入站引用和发送前都会核验工具小号。
    assert adapter.account_calls == ["wxid_tool", "wxid_tool", "wxid_tool"]


@pytest.mark.parametrize(
    "message",
    [
        FakeMessage(type="text"),
        FakeMessage(attr="self"),
        FakeMessage(attr="system"),
    ],
)
def test_non_quote_or_non_friend_is_rejected(message: FakeMessage) -> None:
    """非 quote 或非 friend 消息不能触发同步回调。"""

    received: list[QuoteMessage] = []
    assert extract_quote_message(message, "Codex小号", expected_chat_name="Codex小号") is None

    adapter = FakeAdapter()
    service = WechatService(adapter, tool_wechat_id="wxid_tool", chat_name="Codex小号", target_wechat_id="wxid_main")
    service.start(received.append)
    adapter.emit(message)
    assert received == []


def test_wrong_chat_is_rejected() -> None:
    """非白名单私聊/群聊即使字段合法也不能触发回调或发送。"""

    adapter = FakeAdapter()
    received: list[QuoteMessage] = []
    service = WechatService(adapter, tool_wechat_id="wxid_tool", chat_name="Codex小号", target_wechat_id="wxid_main")
    service.start(received.append)

    adapter.emit(FakeMessage(), chat_name="另一个聊天")

    assert received == []
    assert adapter.sent == []
    assert adapter.start_calls == ["Codex小号"]


@pytest.mark.parametrize(
    "details",
    [
        [],
        [{"wxid": "wxid_main"}, {"wxid": "wxid_main"}],
        {"wxid": "其他好友"},
        {"nickname": "Codex小号"},
    ],
)
def test_friend_verification_fails_closed(details) -> None:
    """无法唯一精确匹配微信号时，不得注册监听器。"""

    adapter = FakeAdapter(details=details)
    service = WechatService(adapter, tool_wechat_id="wxid_tool", chat_name="Codex小号", target_wechat_id="wxid_main")

    with pytest.raises(FriendVerificationError):
        service.start(lambda _event: None)

    assert adapter.start_calls == []
    assert service.started is False


def test_lifecycle_online_and_stop() -> None:
    """在线状态和 StopListening 生命周期接口可用。"""

    adapter = FakeAdapter()
    service = WechatService(adapter, tool_wechat_id="wxid_tool", chat_name="Codex小号", target_wechat_id="wxid_main")
    service.start(lambda _event: None)
    assert service.IsOnline() is True
    service.StopListening()
    service.StopListening()  # 重复停止不应产生第二次底层调用。
    assert adapter.stop_calls == ["Codex小号"]
    assert service.started is False


def test_failed_stop_keeps_started_state_for_cleanup_retry() -> None:
    adapter = RetryStopAdapter()
    service = WechatService(adapter, tool_wechat_id="wxid_tool", chat_name="Codex小号", target_wechat_id="wxid_main")
    service.start(lambda _event: None)

    with pytest.raises(RuntimeError, match="暂时无法停止"):
        service.stop()
    assert service.started is True

    service.stop()
    assert service.started is False
    assert adapter.stop_calls == ["Codex小号", "Codex小号"]


class FakeVerifiedChat:
    """模拟 AddListenChat 返回的已打开独立聊天窗口。"""

    def __init__(self, owner: "FakeWxAutoClient", *, chat_type: str = "friend") -> None:
        self.owner = owner
        self.who = "Codex小号"
        self.chat_type = chat_type

    def ChatInfo(self):
        self.owner.calls.append(("chat-info", self.chat_type))
        return {"chat_name": self.who, "chat_type": self.chat_type}

    def SendMsg(self, text: str, who=None):
        self.owner.calls.append(("chat-send", text, who))


class FakeWxAutoClient:
    """用于验证生产适配器的动态客户端调用和 UI 调用入口。"""

    def __init__(self) -> None:
        self.listener = None
        self.calls: list[tuple] = []
        self.chat = FakeVerifiedChat(self)

    def IsOnline(self) -> bool:
        self.calls.append(("online",))
        return True

    def GetMyInfo(self):
        self.calls.append(("my-info",))
        return {"微信号": "wxid_tool", "昵称": "通知小号"}

    def GetFriendDetails(self, nickname: str):
        self.calls.append(("details", nickname))
        return {"wxid": "wxid_main", "nickname": "Codex小号"}

    def SendMsg(self, text: str, who: str):
        self.calls.append(("send", text, who))

    def AddListenChat(self, nickname: str, callback):
        self.calls.append(("add", nickname))
        self.listener = callback
        return self.chat

    def RemoveListenChat(self, nickname: str):
        self.calls.append(("remove", nickname))


class RetryRemoveWxAutoClient(FakeWxAutoClient):
    """第一次移除聊天失败，用于证明停止顺序可以安全重试。"""

    def __init__(self) -> None:
        super().__init__()
        self.fail_remove = True

    def StopListening(self, remove: bool = True):
        self.calls.append(("stop-all", remove))

    def RemoveListenChat(self, nickname: str):
        self.calls.append(("remove", nickname))
        if self.fail_remove:
            self.fail_remove = False
            raise RuntimeError("暂时无法移除")


def test_wxautox4_adapter_uses_injected_client_and_serial_api() -> None:
    """真实适配器可用 fake 客户端测试，且不在模块导入时导入 wxautox4。"""

    client = FakeWxAutoClient()
    adapter = WxAutoX4Adapter(client=client)
    assert adapter.IsOnline() is True
    assert adapter.verify_account("wxid_tool") is True
    assert adapter.verify_friend("Codex小号", "wxid_main") is True
    adapter.start_listening("Codex小号", lambda _message, _chat: None)
    adapter.send_text("Codex小号", "测试")
    adapter.StopListening("Codex小号")

    assert client.calls == [
        ("online",),
        ("my-info",),
        ("details", "Codex小号"),
        ("add", "Codex小号"),
        ("chat-info", "friend"),
        ("chat-info", "friend"),
        ("chat-send", "测试", None),
        ("remove", "Codex小号"),
    ]


def test_adapter_stop_order_preserves_retry_after_remove_failure() -> None:
    """停止线程后移除聊天失败，第二次清理仍应能完成且不丢本地状态。"""

    client = RetryRemoveWxAutoClient()
    adapter = WxAutoX4Adapter(client=client)
    adapter.start_listening("Codex小号", lambda _message, _chat: None)

    with pytest.raises(RuntimeError, match="暂时无法移除"):
        adapter.stop_listening("Codex小号")
    adapter.stop_listening("Codex小号")

    assert client.calls == [
        ("add", "Codex小号"),
        ("chat-info", "friend"),
        ("stop-all", False),
        ("remove", "Codex小号"),
        ("stop-all", False),
        ("remove", "Codex小号"),
    ]
    with pytest.raises(AdapterCapabilityError, match="尚未完成私聊身份验证"):
        adapter.send_text("Codex小号", "不应发送")


def test_wxautox4_callback_without_chat_identity_is_rejected() -> None:
    """底层回调无法证明聊天来源时，不得回退成白名单名称。"""

    client = FakeWxAutoClient()
    adapter = WxAutoX4Adapter(client=client)
    received: list[QuoteMessage] = []
    service = WechatService(
        adapter,
        tool_wechat_id="wxid_tool",
        chat_name="Codex小号",
        target_wechat_id="wxid_main",
    )
    service.start(received.append)
    assert client.listener is not None

    client.listener(FakeMessage(), None)

    assert received == []


def test_group_with_same_name_is_rejected_before_listener_start() -> None:
    """群名与好友名相同时，ChatInfo 类型仍必须使启动失败。"""

    client = FakeWxAutoClient()
    client.chat.chat_type = "group"
    adapter = WxAutoX4Adapter(client=client)

    with pytest.raises(FriendVerificationError, match="好友私聊"):
        adapter.start_listening("Codex小号", lambda _message, _chat: None)

    assert client.calls == [
        ("add", "Codex小号"),
        ("chat-info", "group"),
        ("remove", "Codex小号"),
    ]


def test_callback_chat_type_drift_is_rejected() -> None:
    """监听启动后回调对象变成群聊时，即使 quote 字段合法也不能进入业务层。"""

    client = FakeWxAutoClient()
    adapter = WxAutoX4Adapter(client=client)
    received: list[QuoteMessage] = []
    service = WechatService(
        adapter,
        tool_wechat_id="wxid_tool",
        chat_name="Codex小号",
        target_wechat_id="wxid_main",
    )
    service.start(received.append)
    client.chat.chat_type = "group"

    assert client.listener is not None
    client.listener(FakeMessage(), client.chat)

    assert received == []


def test_official_chinese_friend_detail_fields_are_verified() -> None:
    class OfficialClient:
        def GetFriendDetails(self, n=None, timeout=0xFFFFF, callback=None):
            return [
                {"昵称": "Codex小号", "微信号": "wxid_main"},
                {"昵称": "别人", "微信号": "wxid_other"},
            ]

    adapter = WxAutoX4Adapter(client=OfficialClient())
    assert adapter.verify_friend("Codex小号", "wxid_main") is True
    assert adapter.verify_friend("别人", "wxid_main") is False


def test_duplicate_chat_name_is_rejected_even_when_target_id_is_unique() -> None:
    """显示名路由存在同名联系人时，不能把唯一微信号校验误当成唯一发送目标。"""

    class DuplicateNameClient:
        def GetFriendDetails(self, n=None, timeout=0xFFFFF, callback=None):
            return [
                {"备注": "唯一联系人", "昵称": "主号", "微信号": "wxid_main"},
                {"昵称": "唯一联系人", "微信号": "wxid_other"},
            ]

    adapter = WxAutoX4Adapter(client=DuplicateNameClient())
    assert adapter.verify_friend("唯一联系人", "wxid_main") is False


def test_tool_account_mismatch_fails_before_any_friend_or_listener_operation() -> None:
    """绑定到主号时必须在所有其他 UI 操作之前失败关闭。"""

    adapter = FakeAdapter(account_id="wxid_main")
    service = WechatService(
        adapter,
        tool_wechat_id="wxid_tool",
        chat_name="主号",
        target_wechat_id="wxid_main",
    )

    with pytest.raises(ToolAccountVerificationError):
        service.start(lambda _event: None)

    assert adapter.verify_calls == []
    assert adapter.start_calls == []
    assert adapter.sent == []
    assert service.started is False


def test_tool_account_drift_blocks_inbound_and_outbound_messages() -> None:
    """启动后同一窗口换号时，不得再转发或发送任何内容。"""

    adapter = FakeAdapter()
    received: list[QuoteMessage] = []
    service = WechatService(
        adapter,
        tool_wechat_id="wxid_tool",
        chat_name="Codex小号",
        target_wechat_id="wxid_main",
    )
    service.start(received.append)
    adapter.account_id = "wxid_main"

    adapter.emit(FakeMessage())
    with pytest.raises(ToolAccountVerificationError):
        service.send_text("不应发出")

    assert received == []
    assert adapter.sent == []
    assert service.is_online() is False


def test_target_friend_is_reverified_before_every_send() -> None:
    """启动后联系人身份或唯一性变化时，不得沿用旧校验结果发送。"""

    adapter = FakeAdapter()
    service = WechatService(
        adapter,
        tool_wechat_id="wxid_tool",
        chat_name="Codex小号",
        target_wechat_id="wxid_main",
    )
    service.start(lambda _event: None)
    adapter.details = {"wxid": "wxid_other"}

    with pytest.raises(FriendVerificationError):
        service.send_text("不应发出")

    assert adapter.sent == []
    assert adapter.verify_calls == [
        ("Codex小号", "wxid_main"),
        ("Codex小号", "wxid_main"),
    ]


def test_target_friend_drift_blocks_inbound_quote() -> None:
    """合法 Quote 到达时也必须重新证明唯一联系人，不能只信启动时结果。"""

    adapter = FakeAdapter()
    received: list[QuoteMessage] = []
    errors: list[BaseException] = []
    service = WechatService(
        adapter,
        tool_wechat_id="wxid_tool",
        chat_name="Codex小号",
        target_wechat_id="wxid_main",
        error_handler=errors.append,
    )
    service.start(received.append)
    adapter.details = {"wxid": "wxid_other"}

    adapter.emit(FakeMessage())

    assert received == []
    assert len(errors) == 1
    assert isinstance(errors[0], FriendVerificationError)
    assert adapter.sent == []
    assert adapter.verify_calls == [
        ("Codex小号", "wxid_main"),
        ("Codex小号", "wxid_main"),
    ]


def test_account_drift_skips_stop_ui_cleanup() -> None:
    """账号已切换时，停止流程不能再操作可能属于主号的微信窗口。"""

    adapter = FakeAdapter()
    service = WechatService(
        adapter,
        tool_wechat_id="wxid_tool",
        chat_name="Codex小号",
        target_wechat_id="wxid_main",
    )
    service.start(lambda _event: None)
    adapter.account_id = "wxid_main"

    service.stop()

    assert adapter.stop_calls == []
    assert service.started is False


def test_production_factory_requires_explicit_account_nickname(monkeypatch) -> None:
    """生产工厂只能用显式小号昵称构造，不得无参降级。"""

    calls: list[dict[str, object]] = []

    def factory(nickname=None, start_listener=True, resize=True):
        calls.append(
            {"nickname": nickname, "start_listener": start_listener, "resize": resize}
        )
        return FakeWxAutoClient()

    class FakeModule:
        WeChat = staticmethod(factory)

    monkeypatch.setattr("src.progress_wx.wechat.importlib.import_module", lambda _name: FakeModule())
    adapter = WxAutoX4Adapter(account_nickname="通知小号")

    assert adapter.verify_account("wxid_tool") is True
    assert calls == [
        {"nickname": "通知小号", "start_listener": False, "resize": False}
    ]

    with pytest.raises(Exception, match="禁止默认绑定"):
        WxAutoX4Adapter()
