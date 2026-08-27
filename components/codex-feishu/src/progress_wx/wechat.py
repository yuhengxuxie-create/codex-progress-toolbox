"""微信侧适配层。

本模块只依赖 Python 标准库。生产环境中的 ``wxautox4`` 会在创建
``WxAutoX4Adapter`` 时动态导入，因此测试环境和没有安装 wxauto 的机器
也可以安全地导入本模块。

设计原则：

* 只依据 wxauto 的结构化 ``attr`` 和 ``type`` 字段识别引用消息，不做
  “完成”“阻塞”等关键词判断。
* 所有对 wxauto 客户端对象的调用都经过同一把可重入锁，避免 UIAutomation
  在监听回调、发送消息、查询好友详情之间并发操作。
* 先按工具小号昵称绑定微信窗口，再用 ``GetMyInfo`` 精确核验小号
  微信号；身份不匹配时不得查询好友、监听或发送。
* 目标好友的微信号必须由 ``GetFriendDetails`` 唯一验证通过；无法证明
  唯一匹配时拒绝启动监听，防止同名联系人误收发消息。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import importlib
import inspect
import logging
import threading
from typing import Any, Protocol, TypeAlias, runtime_checkable


LOGGER = logging.getLogger(__name__)

# wxauto 文档约定的结构化字段。它们是协议字段，不是自然语言关键词。
FRIEND_ATTR = "friend"
QUOTE_TYPE = "quote"
DIRECT_CHAT_TYPE = "friend"


class WechatAdapterError(RuntimeError):
    """微信适配器的基类异常。"""


class AdapterCapabilityError(WechatAdapterError):
    """底层 wxauto 客户端缺少本适配器需要的能力。"""


class FriendVerificationError(WechatAdapterError):
    """目标聊天未能唯一验证为指定微信号。"""


class ToolAccountVerificationError(WechatAdapterError):
    """当前绑定窗口未能验证为指定工具小号。"""


class WechatOfflineError(WechatAdapterError):
    """微信客户端当前不在线。"""


@dataclass(frozen=True, slots=True)
class RawWechatMessage:
    """在 wxauto 锁内复制出的消息快照。

    wxauto 的消息对象可能持有 UIAutomation 对象引用；适配器把需要的
    字段复制成普通值后才交给业务回调，避免回调在 UI 线程之外继续访问
    第三方对象。
    """

    attr: Any = None
    type: Any = None
    content: Any = None
    quote_content: Any = None
    quote_nickname: Any = None
    sender: Any = None
    chat_type: Any = None
    id: Any = None
    hash: Any = None


@dataclass(frozen=True, slots=True)
class QuoteMessage:
    """经过结构化字段过滤后的引用回复。

    ``message_id`` 和 ``message_hash`` 是当前收到的回复消息的标识。
    wxauto 的公开 QuoteMessage 接口不保证提供被引用原消息的服务器 ID，
    所以本对象不会伪造一个 ``original_message_id``。
    """

    chat_name: str
    content: str
    quote_content: str
    quote_nickname: str
    sender: str | None = None
    message_id: str | int | None = None
    message_hash: str | int | None = None

    def as_dict(self) -> dict[str, Any]:
        """返回便于日志和序列化的普通字典。"""

        return {
            "chat_name": self.chat_name,
            "content": self.content,
            "quote_content": self.quote_content,
            "quote_nickname": self.quote_nickname,
            "sender": self.sender,
            "message_id": self.message_id,
            "message_hash": self.message_hash,
        }


# 监听回调的参数顺序与 wxauto 的 AddListenChat 回调一致：消息、聊天名称。
MessageHandler: TypeAlias = Callable[[object, str], None]
QuoteHandler: TypeAlias = Callable[[QuoteMessage], None]
WechatErrorHandler: TypeAlias = Callable[[BaseException], None]


@runtime_checkable
class WechatAdapter(Protocol):
    """可测试的微信适配器协议。

    ``WechatService`` 只依赖此协议，因此可以用内存 fake 替代真实微信。
    ``verify_account`` 必须先于所有其他 UI 操作；``verify_friend``
    必须在注册监听器前完成精确微信号校验。
    """

    def verify_account(self, wechat_id: str) -> bool:
        """确认当前绑定窗口登录的是指定工具小号。"""

    def verify_friend(self, chat_name: str, wechat_id: str) -> bool:
        """确认聊天名称只对应指定微信号。"""

    def send_text(self, chat_name: str, text: str) -> None:
        """向指定聊天发送文本。"""

    def start_listening(self, chat_name: str, callback: MessageHandler) -> None:
        """开始监听一个指定聊天。"""

    def stop_listening(self, chat_name: str) -> None:
        """停止监听指定聊天。"""

    def is_online(self) -> bool:
        """返回微信客户端在线状态。"""


# 便于调用方按更明确的名称导入；两者是同一个运行时协议。
WechatAdapterProtocol = WechatAdapter


def _read_field(value: object, field_name: str, default: Any = None) -> Any:
    """从字典或对象读取字段，不对字段值做自然语言解释。"""

    if isinstance(value, Mapping):
        return value.get(field_name, default)
    try:
        return getattr(value, field_name)
    except Exception:
        # UIAutomation 属性读取失败时按字段缺失处理，禁止异常消息穿透
        # 到业务层并误触发双向同步。
        return default


def _as_text(value: Any) -> str | None:
    """把消息字段变成文本；缺字段时返回 None 以便安全丢弃。"""

    if value is None:
        return None
    if isinstance(value, str):
        return value
    # wxauto 通常返回字符串；对少数实现返回的可打印标量做保守兼容。
    if isinstance(value, (int, float, bool)):
        return str(value)
    return None


def extract_quote_message(
    message: object,
    chat_name: str,
    *,
    expected_chat_name: str | None = None,
) -> QuoteMessage | None:
    """从一条消息中提取合法的好友引用回复。

    返回 ``None`` 的情况包括：聊天名称不匹配、消息不是好友消息、消息
    类型不是 ``quote``、或引用文本字段缺失。这些过滤均基于结构化字段，
    不使用关键词匹配。
    """

    if expected_chat_name is not None and chat_name != expected_chat_name:
        return None
    if _read_field(message, "chat_type") != DIRECT_CHAT_TYPE:
        return None
    if _read_field(message, "attr") != FRIEND_ATTR:
        return None
    if _read_field(message, "type") != QUOTE_TYPE:
        return None

    content = _as_text(_read_field(message, "content"))
    quote_content = _as_text(_read_field(message, "quote_content"))
    if content is None or quote_content is None:
        # 缺少引用原文时不能安全地同步到 Codex，宁可丢弃也不猜测。
        return None

    quote_nickname_value = _read_field(message, "quote_nickname", "")
    quote_nickname = _as_text(quote_nickname_value)
    if quote_nickname is None:
        quote_nickname = ""

    sender_value = _read_field(message, "sender")
    sender = _as_text(sender_value)
    message_id = _read_field(message, "id")
    message_hash = _read_field(message, "hash")

    return QuoteMessage(
        chat_name=chat_name,
        content=content,
        quote_content=quote_content,
        quote_nickname=quote_nickname,
        sender=sender,
        message_id=message_id,
        message_hash=message_hash,
    )


# 语义更短的别名，方便业务层和测试调用。
parse_quote_message = extract_quote_message


def _contains_id_field(value: object) -> bool:
    """判断一个好友详情记录是否包含可核验的微信号字段。"""

    if isinstance(value, Mapping):
        return any(
            key in value
            for key in (
                "微信号",
                "wxid",
                "wx_id",
                "wechat_id",
                "wechatId",
                "UserName",
                "username",
                "user_name",
            )
        )
    return any(
        _read_field(value, key) is not None
        for key in ("wxid", "wx_id", "wechat_id", "wechatId", "UserName", "username", "user_name")
    )


def _extract_friend_id(value: object) -> str | None:
    """从 GetFriendDetails 返回的单条记录中提取微信号。"""

    if isinstance(value, str):
        return value or None
    for field_name in (
        "微信号",
        "wxid",
        "wx_id",
        "wechat_id",
        "wechatId",
        "UserName",
        "username",
        "user_name",
    ):
        candidate = _read_field(value, field_name)
        if candidate is not None:
            text = _as_text(candidate)
            if text:
                return text
    return None


def _extract_friend_names(value: object) -> set[str]:
    """提取好友记录里可用于聊天路由的昵称与备注。

    wxauto 的发送和监听接口仍以显示名定位，所以即使唯一微信号已经匹配，
    也必须证明该显示名在好友详情列表中只对应一条记录；同名时宁可拒绝。
    """

    return {
        text
        for field_name in ("昵称", "备注", "remark", "nickname", "name")
        if (text := _as_text(_read_field(value, field_name)))
    }


def _friend_records(details: Any) -> list[object]:
    """把好友详情结果规范化为记录列表，无法识别时保留为单条坏记录。"""

    if details is None:
        return []
    if isinstance(details, Mapping):
        # 常见 wxauto 返回值是单个字段字典；含微信号字段时只能算一条。
        if _contains_id_field(details):
            return [details]
        # 兼容“微信号 -> 详情”的映射形式，但没有可核验记录时仍会失败。
        values = list(details.values())
        if values and all(_contains_id_field(item) for item in values):
            return values
        return [details]
    if isinstance(details, Sequence) and not isinstance(details, (str, bytes, bytearray)):
        return list(details)
    return [details]


def _verify_friend_details(
    details: Any,
    expected_wechat_id: str,
    expected_chat_name: str | None = None,
) -> bool:
    """严格验证好友详情中昵称/备注和微信号只有一个精确组合。"""

    if not isinstance(expected_wechat_id, str) or not expected_wechat_id:
        return False
    records = _friend_records(details)
    id_matches = [record for record in records if _extract_friend_id(record) == expected_wechat_id]
    if len(id_matches) != 1:
        return False
    if expected_chat_name is None:
        return True
    name_matches = [
        record for record in records if expected_chat_name in _extract_friend_names(record)
    ]
    # 发送/监听按显示名路由：同名、缺名或名称对应其他微信号时全部失败关闭。
    return len(name_matches) == 1 and name_matches[0] is id_matches[0]


def _signature_parameters(method: Callable[..., Any]) -> tuple[inspect.Signature | None, dict[str, inspect.Parameter]]:
    """尽量取得方法签名，用于兼容 wxauto 的位置/关键字参数差异。"""

    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        return None, {}
    return signature, dict(signature.parameters)


def _invoke_friend_details(method: Callable[..., Any], chat_name: str) -> Any:
    """调用 GetFriendDetails；官方接口无聊天名参数，需一次扫描后本地精确过滤。"""

    _signature, parameters = _signature_parameters(method)
    if "nickname" in parameters:
        return method(nickname=chat_name)
    if "chat_name" in parameters:
        return method(chat_name=chat_name)
    if "name" in parameters:
        return method(name=chat_name)
    # 官方 wxautox4 参数是 n/timeout/callback，不把聊天名误传成 n。
    if any(name in parameters for name in ("n", "timeout", "callback")):
        return method()
    # 对无法反射签名的实现使用官方无参数形式，避免重复 UI 扫描。
    return method()


def _invoke_send_text(method: Callable[..., Any], chat_name: str, text: str) -> Any:
    """调用 wxauto SendMsg。"""

    _signature, parameters = _signature_parameters(method)
    if "who" in parameters or not parameters:
        kwargs: dict[str, Any] = {"who": chat_name}
        if "exact" in parameters:
            kwargs["exact"] = True
        return method(text, **kwargs)
    if "nickname" in parameters:
        return method(text, nickname=chat_name)
    if "chat_name" in parameters:
        return method(text, chat_name=chat_name)
    return method(text, chat_name)


def _invoke_verified_chat_send(method: Callable[..., Any], text: str) -> Any:
    """通过已验证的独立好友聊天对象发送，不再按名称重新搜索。"""

    _signature, parameters = _signature_parameters(method)
    if "who" in parameters:
        return method(text, who=None)
    return method(text)


def _invoke_add_listener(method: Callable[..., Any], chat_name: str, callback: MessageHandler) -> Any:
    """调用 wxauto AddListenChat。"""

    _signature, parameters = _signature_parameters(method)
    if "nickname" in parameters and "callback" in parameters:
        return method(nickname=chat_name, callback=callback)
    if "chat_name" in parameters and "callback" in parameters:
        return method(chat_name=chat_name, callback=callback)
    return method(chat_name, callback)


def _invoke_stop_listener(method: Callable[..., Any], chat_name: str) -> Any:
    """调用 StopListening/RemoveListenChat，并兼容无参数实现。"""

    _signature, parameters = _signature_parameters(method)
    if not parameters:
        return method()
    if "nickname" in parameters:
        return method(nickname=chat_name)
    if "chat_name" in parameters:
        return method(chat_name=chat_name)
    if "remove" in parameters:
        return method(remove=True)
    return method(chat_name)


def _invoke_stop_all(method: Callable[..., Any], *, remove: bool) -> Any:
    """停止 wxauto 监听线程，并显式控制是否同时移除聊天对象。"""

    _signature, parameters = _signature_parameters(method)
    if "remove" in parameters:
        return method(remove=remove)
    return method()


def _require_success(result: Any, operation: str) -> None:
    """wxauto 成功时可能返回 None/Chat，失败 WxResponse 的布尔值为 False。"""

    if result is None:
        return
    try:
        accepted = bool(result)
    except Exception as exc:
        raise WechatAdapterError(f"{operation} 返回值无法判定") from exc
    if not accepted:
        raise WechatAdapterError(f"{operation} 返回失败")


class WxAutoX4Adapter:
    """基于 wxautox4 的本地 Windows 微信适配器。

    ``client`` 参数只用于依赖注入和测试；正常生产调用不传它，适配器会
    在运行时执行 ``importlib.import_module("wxautox4")``。因此本项目不把
    wxautox4 打包进源码，也不会在无微信环境中导入失败。
    """

    def __init__(
        self,
        client: object | None = None,
        *,
        account_nickname: str | None = None,
        module_name: str = "wxautox4",
    ) -> None:
        self._lock = threading.RLock()
        self._listeners: dict[str, MessageHandler] = {}
        self._listener_chats: dict[str, object] = {}
        self._owns_client = client is None
        self._account_nickname = account_nickname
        if client is None:
            if not isinstance(account_nickname, str) or not account_nickname.strip():
                raise AdapterCapabilityError(
                    "生产模式必须提供工具小号昵称，禁止默认绑定第一个微信窗口"
                )
            try:
                module = importlib.import_module(module_name)
            except ImportError as exc:
                raise AdapterCapabilityError(
                    f"无法导入 {module_name}，请在目标 Windows 环境单独安装 wxautox4"
                ) from exc
            factory = getattr(module, "WeChat", None)
            if not callable(factory):
                raise AdapterCapabilityError(f"{module_name} 未提供可调用的 WeChat 工厂")
            _signature, parameters = _signature_parameters(factory)
            if "nickname" not in parameters or "start_listener" not in parameters:
                raise AdapterCapabilityError(
                    f"{module_name} 的 WeChat 不支持 nickname/start_listener，拒绝在双开环境中降级"
                )
            kwargs: dict[str, Any] = {
                "nickname": account_nickname.strip(),
                "start_listener": False,
            }
            if "resize" in parameters:
                # 身份核验前不调整任何微信窗口尺寸。
                kwargs["resize"] = False
            with self._lock:
                # 绝不回退到 WeChat()：双开时默认窗口可能是用户主号。
                self._client = factory(**kwargs)
        else:
            self._client = client

    @property
    def client(self) -> object:
        """返回注入的底层客户端，主要供诊断和测试使用。"""

        return self._client

    def _method(self, *names: str) -> Callable[..., Any] | None:
        with self._lock:
            for name in names:
                candidate = getattr(self._client, name, None)
                if callable(candidate):
                    return candidate
        return None

    def verify_account(self, wechat_id: str) -> bool:
        """用 GetMyInfo 精确核验当前绑定的发送端账号。"""

        if not isinstance(wechat_id, str) or not wechat_id:
            return False
        method = self._method("GetMyInfo", "get_my_info")
        if method is None:
            raise AdapterCapabilityError("wxauto 客户端缺少 GetMyInfo，无法隔离双开账号")
        with self._lock:
            info = method()
        records = _friend_records(info)
        return len(records) == 1 and _extract_friend_id(records[0]) == wechat_id

    def get_friend_details(self, chat_name: str) -> Any:
        """串行调用底层 GetFriendDetails。"""

        method = self._method("GetFriendDetails", "get_friend_details")
        if method is None:
            raise AdapterCapabilityError("wxauto 客户端缺少 GetFriendDetails")
        with self._lock:
            return _invoke_friend_details(method, chat_name)

    # 为需要直接调用传统 wxauto 命名的代码保留一个安全别名。
    def GetFriendDetails(self, chat_name: str) -> Any:  # noqa: N802
        return self.get_friend_details(chat_name)

    def verify_friend(self, chat_name: str, wechat_id: str) -> bool:
        """只有唯一详情且微信号精确匹配时才返回 True。"""

        try:
            details = self.get_friend_details(chat_name)
        except Exception:
            LOGGER.exception("查询好友详情失败，拒绝启动聊天监听: %s", chat_name)
            return False
        return _verify_friend_details(details, wechat_id, chat_name)

    def send_text(self, chat_name: str, text: str) -> None:
        """串行通过已验证的独立好友聊天对象发送文本。"""

        if not isinstance(chat_name, str) or not chat_name:
            raise ValueError("chat_name 不能为空")
        if not isinstance(text, str):
            raise TypeError("text 必须是字符串")
        with self._lock:
            chat = self._listener_chats.get(chat_name)
            if chat is None:
                raise AdapterCapabilityError("目标好友聊天尚未完成私聊身份验证")
            actual_name, chat_type = self._chat_identity(chat)
            if actual_name != chat_name or chat_type != DIRECT_CHAT_TYPE:
                raise FriendVerificationError("已验证的目标窗口不再是配置的好友私聊")
            method = getattr(chat, "SendMsg", None)
            if not callable(method):
                method = getattr(chat, "send_text", None)
            if not callable(method):
                raise AdapterCapabilityError("已验证的好友聊天对象缺少 SendMsg")
            result = _invoke_verified_chat_send(method, text)
            _require_success(result, "SendMsg")

    def _snapshot_message(self, message: object, *, chat_type: str | None) -> RawWechatMessage:
        """在锁内复制消息字段，避免把 UI 对象泄漏给业务线程。"""

        return RawWechatMessage(
            attr=_read_field(message, "attr"),
            type=_read_field(message, "type"),
            content=_read_field(message, "content"),
            quote_content=_read_field(message, "quote_content"),
            quote_nickname=_read_field(message, "quote_nickname"),
            sender=_read_field(message, "sender"),
            chat_type=chat_type,
            id=_read_field(message, "id"),
            hash=_read_field(message, "hash"),
        )

    @staticmethod
    def _chat_name(chat: object) -> str:
        """从 wxauto Chat 对象取得名称；无法读取时失败关闭。"""

        if isinstance(chat, str) and chat:
            return chat
        for field_name in ("who", "nickname", "name", "chat_name"):
            candidate = _as_text(_read_field(chat, field_name))
            if candidate:
                return candidate
        # 不回退为配置的目标名，否则会把“来源未知”伪装成白名单聊天。
        return ""

    @classmethod
    def _chat_identity(cls, chat: object) -> tuple[str, str]:
        """读取 ChatInfo 的结构化会话类型；无法证明时返回空值。"""

        if chat is None or isinstance(chat, str):
            return "", ""
        method = getattr(chat, "ChatInfo", None)
        if not callable(method):
            method = getattr(chat, "chat_info", None)
        if not callable(method):
            return "", ""
        try:
            info = method()
        except Exception:
            return "", ""
        name = _as_text(_read_field(info, "chat_name")) or cls._chat_name(chat)
        chat_type = _as_text(_read_field(info, "chat_type")) or ""
        return name, chat_type.casefold()

    def start_listening(self, chat_name: str, callback: MessageHandler) -> None:
        """串行注册指定聊天的监听器。"""

        if not isinstance(chat_name, str) or not chat_name:
            raise ValueError("chat_name 不能为空")
        if not callable(callback):
            raise TypeError("callback 必须可调用")
        method = self._method("AddListenChat", "add_listen_chat")
        if method is None:
            raise AdapterCapabilityError("wxauto 客户端缺少 AddListenChat")

        def on_message(message: object, chat: object = None) -> None:
            # 监听回调可能由 wxauto worker 线程触发；读取 UI 消息对象时仍须持锁。
            with self._lock:
                actual_chat_name, chat_type = self._chat_identity(chat)
                snapshot = self._snapshot_message(message, chat_type=chat_type)
            callback(snapshot, actual_chat_name)

        with self._lock:
            result = _invoke_add_listener(method, chat_name, on_message)
            _require_success(result, "AddListenChat")
            actual_chat_name, chat_type = self._chat_identity(result)
            if actual_chat_name != chat_name or chat_type != DIRECT_CHAT_TYPE:
                remove_method = self._method("RemoveListenChat", "remove_listen_chat")
                if remove_method is not None:
                    try:
                        _invoke_stop_listener(remove_method, chat_name)
                    except Exception:
                        LOGGER.exception("私聊身份验证失败后注销聊天监听也失败")
                raise FriendVerificationError("AddListenChat 未返回配置的好友私聊，拒绝启动")
            start_method = self._method("StartListening", "start_listening")
            if start_method is None and self._owns_client:
                remove_method = self._method("RemoveListenChat", "remove_listen_chat")
                if remove_method is not None:
                    _invoke_stop_listener(remove_method, chat_name)
                raise AdapterCapabilityError("wxautox4 客户端缺少 StartListening")
            try:
                if start_method is not None:
                    _require_success(start_method(), "StartListening")
            except BaseException:
                # AddListenChat 已成功时必须回滚，避免下一次重试重复注册。
                remove_method = self._method("RemoveListenChat", "remove_listen_chat")
                if remove_method is not None:
                    try:
                        _invoke_stop_listener(remove_method, chat_name)
                    except Exception:
                        LOGGER.exception("StartListening 失败后注销聊天监听也失败")
                raise
            self._listeners[chat_name] = callback
            self._listener_chats[chat_name] = result

    def stop_listening(self, chat_name: str) -> None:
        """串行注销指定聊天监听器。"""

        remove_method = self._method("RemoveListenChat", "remove_listen_chat")
        stop_method = self._method("StopListening", "stop_listening")
        if remove_method is None and stop_method is None:
            raise AdapterCapabilityError("wxauto 客户端缺少 StopListening/RemoveListenChat")
        with self._lock:
            if stop_method is not None:
                # 先停线程但保留聊天，确保后续 RemoveListenChat 失败时仍可重试。
                result = _invoke_stop_all(stop_method, remove=remove_method is None)
                _require_success(result, "停止监听线程")
            if remove_method is not None:
                _require_success(_invoke_stop_listener(remove_method, chat_name), "注销聊天监听")
            self._listeners.pop(chat_name, None)
            self._listener_chats.pop(chat_name, None)

    # 兼容项目其他模块可能使用的首字母大写接口。
    def StopListening(self, chat_name: str) -> None:  # noqa: N802
        self.stop_listening(chat_name)

    def is_online(self) -> bool:
        """串行读取 wxauto 在线状态；缺少能力时按离线处理。"""

        # 不同 wxauto 版本有方法和布尔属性两种暴露方式；两者都按离线
        # 默认值处理未知情况，避免在状态不明时继续发送消息。
        with self._lock:
            method: Callable[..., Any] | None = None
            for name in ("IsOnline", "is_online"):
                candidate = getattr(self._client, name, None)
                if callable(candidate):
                    method = candidate
                    break
                if isinstance(candidate, bool):
                    return candidate
            if method is None:
                return False
            try:
                return bool(method())
            except Exception:
                LOGGER.exception("读取微信在线状态失败")
                return False

    def IsOnline(self) -> bool:  # noqa: N802
        return self.is_online()


class WechatService:
    """对微信适配器进行目标校验、消息过滤和生命周期管理。"""

    def __init__(
        self,
        adapter: WechatAdapter,
        *,
        tool_wechat_id: str,
        chat_name: str,
        target_wechat_id: str,
        error_handler: WechatErrorHandler | None = None,
    ) -> None:
        if not tool_wechat_id:
            raise ValueError("tool_wechat_id 不能为空")
        if not chat_name:
            raise ValueError("chat_name 不能为空")
        if not target_wechat_id:
            raise ValueError("target_wechat_id 不能为空")
        self._adapter = adapter
        self._tool_wechat_id = tool_wechat_id
        self._chat_name = chat_name
        self._target_wechat_id = target_wechat_id
        self._lock = threading.RLock()
        self._quote_handler: QuoteHandler | None = None
        self._error_handler = error_handler
        self._started = False
        self._account_mismatch = False

    @property
    def chat_name(self) -> str:
        return self._chat_name

    @property
    def target_wechat_id(self) -> str:
        return self._target_wechat_id

    @property
    def tool_wechat_id(self) -> str:
        return self._tool_wechat_id

    def _require_tool_account(self) -> None:
        """核验发送端小号；明确串号后本进程永不自动恢复。"""

        if self._account_mismatch:
            raise ToolAccountVerificationError("本进程已检测到微信串号，拒绝继续操作")
        try:
            verified = self._adapter.verify_account(self._tool_wechat_id)
        except Exception as exc:
            raise ToolAccountVerificationError("无法读取工具小号身份，拒绝继续操作") from exc
        if verified is not True:
            self._account_mismatch = True
            raise ToolAccountVerificationError(
                f"当前绑定窗口不是工具小号 {self._tool_wechat_id!r}"
            )

    def _require_target_friend(self) -> None:
        """重新证明显示名仍唯一指向配置的联系人微信号。"""

        try:
            verified = self._adapter.verify_friend(self._chat_name, self._target_wechat_id)
        except Exception as exc:
            raise FriendVerificationError(
                f"无法验证聊天 {self._chat_name!r} 的唯一微信号，拒绝操作"
            ) from exc
        if verified is not True:
            raise FriendVerificationError(
                f"聊天 {self._chat_name!r} 未唯一匹配微信号 {self._target_wechat_id!r}"
            )

    @property
    def started(self) -> bool:
        with self._lock:
            return self._started

    def start(self, on_quote: QuoteHandler) -> None:
        """先验证工具小号和目标好友，再注册监听器。"""

        if not callable(on_quote):
            raise TypeError("on_quote 必须可调用")
        with self._lock:
            if self._started:
                return
            self._require_tool_account()
            if not self._adapter.is_online():
                raise WechatOfflineError("微信不在线，拒绝启动监听")
            self._require_target_friend()

            # 先保存回调，以兼容某些 fake/实现会在注册时同步触发首条事件。
            self._quote_handler = on_quote
            try:
                self._adapter.start_listening(self._chat_name, self._handle_message)
            except Exception:
                self._quote_handler = None
                raise
            self._started = True

    def _handle_message(self, message: object, chat_name: str) -> None:
        """过滤聊天、发送者属性和消息类型后派发引用回复。"""

        event = extract_quote_message(
            message,
            chat_name,
            expected_chat_name=self._chat_name,
        )
        if event is None:
            return
        try:
            # 只在真正的引用回复到达时重新核验，不增加空闲 UI 轮询。
            # 工具账号与目标好友两端都必须仍精确匹配，避免运行期间改备注、
            # 新增同名联系人或好友身份漂移后把内容转入 Codex。
            self._require_tool_account()
            self._require_target_friend()
        except (ToolAccountVerificationError, FriendVerificationError) as exc:
            LOGGER.critical("入站引用回复到达时账号或好友身份校验失败，已丢弃消息")
            with self._lock:
                error_handler = self._error_handler
            if error_handler is not None:
                try:
                    error_handler(exc)
                except Exception:
                    # 第三方监听线程不能因上层错误回调再次异常而静默退出。
                    LOGGER.exception("上报微信身份故障失败")
            return
        with self._lock:
            handler = self._quote_handler
        if handler is not None:
            handler(event)

    def send_text(self, text: str) -> None:
        """向已经验证的目标聊天发送文本。"""

        with self._lock:
            if not self._started:
                raise FriendVerificationError("微信目标尚未完成唯一校验和监听启动，拒绝发送")
            self._require_tool_account()
            # 备注变化或新增同名联系人后必须立即拒绝，不能沿用启动时的旧证明。
            self._require_target_friend()
            self._adapter.send_text(self._chat_name, text)

    def stop(self) -> None:
        """停止目标聊天监听；重复停止是安全的。"""

        with self._lock:
            if not self._started:
                return
            try:
                self._require_tool_account()
            except ToolAccountVerificationError:
                # 串号后 RemoveListenChat 也可能操作到用户主号窗口；进程退出会
                # 终止监听线程，因此这里只清空本地状态，不再触碰任何微信 UI。
                LOGGER.critical("停止监听时工具账号身份不可证明，跳过底层微信窗口清理")
                self._started = False
                self._quote_handler = None
                return
            # 只有底层确认停止后才提交状态；失败时保留回调以允许再次清理。
            self._adapter.stop_listening(self._chat_name)
            self._started = False
            self._quote_handler = None

    def stop_listening(self) -> None:
        """语义别名，便于上层统一调用。"""

        self.stop()

    def StopListening(self) -> None:  # noqa: N802
        self.stop()

    def is_online(self) -> bool:
        """查询在线状态，并将工具小号身份纳入健康判定。"""

        try:
            self._require_tool_account()
        except ToolAccountVerificationError:
            return False
        return self._adapter.is_online()

    def IsOnline(self) -> bool:  # noqa: N802
        return self.is_online()


# 兼容常见类名写法。
WeChatService = WechatService


__all__ = [
    "AdapterCapabilityError",
    "FRIEND_ATTR",
    "FriendVerificationError",
    "MessageHandler",
    "QUOTE_TYPE",
    "QuoteHandler",
    "QuoteMessage",
    "RawWechatMessage",
    "ToolAccountVerificationError",
    "WechatAdapter",
    "WechatAdapterError",
    "WechatAdapterProtocol",
    "WechatErrorHandler",
    "WechatOfflineError",
    "WechatService",
    "WeChatService",
    "WxAutoX4Adapter",
    "extract_quote_message",
    "parse_quote_message",
]
