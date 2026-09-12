"""飞书企业自建应用机器人消息渠道。

本模块使用飞书官方 ``lark-channel-sdk`` 的 WebSocket 长连接，不依赖
电脑端飞书窗口，也不启动本地 HTTP 服务。所有入站消息在交给主服务前，
都会再次执行单聊、发送者白名单和结构化引用三项校验。
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from functools import lru_cache
import hashlib
import json
import logging
from pathlib import Path
import threading
import time
from typing import Any, Callable, Mapping
import uuid

from .channel import (
    ChannelAttachment,
    ChannelErrorHandler,
    ChannelReply,
    MessageChannelError,
    MessageChannelOfflineError,
    MessageChannelPayloadRejectedError,
    ReplyHandler,
)
from .session_query_card import (
    SESSION_QUERY_ENTRY_COMMAND,
    SESSION_QUERY_MENU_EVENT_KEY,
    session_query_action_fingerprint,
    session_query_command,
    session_query_form_command,
)
from .remote_control import (
    REMOTE_CONTROL_ENTRY_COMMAND,
    REMOTE_CONTROL_MENU_EVENT_KEY,
    remote_control_action_fingerprint,
    remote_control_command,
)
from .feature_center import (
    FEATURE_CENTER_ENTRY_COMMAND,
    FEATURE_CENTER_MENU_EVENT_KEY,
    feature_center_action_command,
    feature_center_action_fingerprint,
    feature_operation_action_command,
    feature_operation_action_fingerprint,
)
from .slash_commands import (
    slash_card_action_fingerprint,
    slash_card_command,
)


LOGGER = logging.getLogger(__name__)
_IDEMPOTENCY_NAMESPACE = uuid.UUID("be46c6a9-5027-4d70-aa62-873b94c06e78")
# These are the limits documented by the Feishu Open Platform upload APIs.
# The API uses decimal MB in its wording, so keep the exact byte boundaries
# instead of silently converting them to MiB.
FEISHU_FILE_MAX_BYTES = 30 * 1000 * 1000
FEISHU_IMAGE_MAX_BYTES = 10 * 1000 * 1000
_MEDIA_RETRYABLE_RAW_CODES = frozenset({429, 500, 502, 503, 504, 232096, 234044})
_BOLD_FIELD_LABELS = (
    "对话名称：",
    "当前进度：",
    "本轮完成：",
    "关键结果：",
    "剩余事项：",
    "需要你处理：",
    "本条消息时间：",
    "消息状态：",
    "回复信息：",
    "列表类型：",
    "页码：",
    "总数：",
    "项目列表：",
    "会话列表：",
    "项目名称：",
    "会话名称：",
    "会话描述：",
    "会话最后一轮结果：",
    "会话最后活动时间：",
    "归属：",
    "状态：",
    "最近更新：",
    "整体概览：",
    "最后一轮结果：",
    "操作说明：",
    "搜索范围：",
    "匹配度：",
    "匹配说明：",
    "操作类型：",
    "填写说明：",
    "首轮对话提示词：",
    "是否需要第一段会话：",
    "执行结果：",
    "任务 ID：",
    "提交状态：",
    "目录：",
    "监测状态：",
    "监测来源：",
    "剩余有效时间：",
    "版本：",
    "更新日期：",
    "入口命令：",
    "回复规则：",
    "监测规则：",
    "图片输入：",
    "生成图片回传：",
    "生成图片：",
    "最近生成图片：",
    "进度通知：",
    "安全与故障处理：",
    "重要提醒：",
    "查看会话：",
    "新建对话：",
    "继续已有对话：",
    "发送图片：",
    "管理进度监测：",
    "其他帮助：",
    "当前可用额度：",
    "下次刷新日期：",
    "额外重置卡：",
    "Codex 每周额度：",
    "剩余重置卡：",
    "具体操作：",
    "可复用范围：",
)


def _rich_post_or_text(payload: str) -> dict[str, object]:
    """生成带粗体标签的 post，并用空段落保留原始空白行。"""

    rows: list[list[dict[str, object]]] = []
    has_styled_label = False
    for line in payload.splitlines():
        if not line:
            # 飞书会忽略真正的空 text 节点；NBSP 不可见但能稳定保留行高。
            rows.append([{"tag": "text", "text": "\u00a0"}])
            continue
        label = next((item for item in _BOLD_FIELD_LABELS if line.startswith(item)), None)
        if label is None:
            rows.append([{"tag": "text", "text": line}])
            continue
        has_styled_label = True
        nodes: list[dict[str, object]] = [
            {"tag": "text", "text": label, "style": ["bold"]}
        ]
        value = line[len(label):]
        if value:
            nodes.append({"tag": "text", "text": value})
        rows.append(nodes)
    if not has_styled_label:
        return {"text": payload}
    return {
        "post": {
            "zh_cn": {
                "title": "",
                "content": rows,
            }
        }
    }


class FeishuDependencyError(MessageChannelError):
    """飞书官方 SDK 未安装或版本不兼容。"""


class FeishuSendError(MessageChannelError):
    """飞书明确拒绝发送或未返回可关联的消息 ID。"""

    def __init__(
        self,
        message: str,
        *,
        uploaded: bool | None = None,
        media_key: str | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        super().__init__(message)
        # ``uploaded=True`` is the durable boundary used by the guardian:
        # once a media key exists, an unknown message result must stay frozen
        # and must not trigger another upload.
        self.uploaded = uploaded
        self.media_key = media_key
        self.idempotency_key = idempotency_key


class FeishuSendRejectedError(FeishuSendError):
    """飞书返回了可分类的明确拒绝；字段仅包含脱敏错误元数据。"""

    def __init__(
        self,
        *,
        code: str,
        raw_code: int | None,
        retryable: bool,
        context: Mapping[str, object] | None = None,
        raw_msg: str | None = None,
    ) -> None:
        super().__init__(f"飞书明确拒绝发送消息（分类={code}，错误码={raw_code}）")
        self.code = str(code or "unknown")
        self.raw_code = raw_code
        self.retryable = bool(retryable)
        # Keep the SDK's structured diagnostic context available to the
        # durable caller.  In particular, resolve_media_key places the
        # server's raw_code/raw_msg here; never put the raw message in logs.
        self.context = dict(context or {})
        self.raw_msg = str(raw_msg) if raw_msg is not None else None


class FeishuPayloadRejectedError(
    FeishuSendRejectedError,
    MessageChannelPayloadRejectedError,
):
    """飞书明确拒绝了可重新编码的消息载荷（例如卡片格式错误）。"""


class FeishuSendNotSubmittedError(MessageChannelOfflineError):
    """发送调用在进入飞书 SDK/HTTP 写入前即被本地离线门禁拒绝。"""


# 飞书 SDK 在不同版本中使用不同的异常类和错误码；不能只依赖一个类名，
# 也不能把所有 ``ClientException`` 都当成可重试（凭据/权限错误会因此被
# 无限重连掩盖）。这些集合只用于连接监督分类，不会把 SDK 的异常正文写入
# 日志或状态。
_PERMANENT_FEISHU_CODES = frozenset(
    {
        230001,  # 请求格式/参数错误
        230003,
        230010,
        99991400,
        99991401,
        99991663,
        99991664,
        99991665,
        99991666,
        99991668,
        99991672,
        99991679,
        99991680,
        99991681,
        1000040344,  # 未配置凭据
    }
)
_TRANSIENT_FEISHU_CODES = frozenset(
    {
        408,
        425,
        429,
        500,
        502,
        503,
        504,
        99991402,
        11020,
        11021,
        1000040350,  # WebSocket 连接数暂时超限
    }
)
_PERMANENT_ERROR_MARKERS = (
    "auth",
    "credential",
    "forbidden",
    "permission",
    "invalidtoken",
    "unauthorized",
    "accessdenied",
    "invalidparameter",
)
_TRANSIENT_ERROR_MARKERS = (
    "connection",
    "connecterror",
    "connectionclosed",
    "connectionreset",
    "brokenpipe",
    "invalidhandshake",
    "timeout",
    "incompleteread",
    "websocket",
    "network",
    "dns",
    "socket",
    "ssl",
    "tls",
    "serverexception",
    "serverunreachable",
    "notconnected",
    "temporarilyunavailable",
)


def _exception_chain(error: BaseException):
    """Yield an exception chain without inspecting potentially sensitive text."""

    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        cause = current.__cause__
        if cause is None and not current.__suppress_context__:
            cause = current.__context__
        current = cause


def _numeric_error_codes(error: BaseException) -> set[int]:
    """Extract only numeric SDK/HTTP codes; never stringify exception messages."""

    codes: set[int] = set()
    for item in _exception_chain(error):
        for attribute in ("raw_code", "status_code", "http_status", "code"):
            value = getattr(item, attribute, None)
            value = getattr(value, "value", value)
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                codes.add(value)
                continue
            if isinstance(value, str) and value.strip().isdigit():
                try:
                    codes.add(int(value.strip()))
                except ValueError:
                    pass
    return codes


def _connection_failure_class(error: BaseException) -> str:
    """Classify a connection failure as ``transient`` or ``permanent``.

    The default is permanent.  That fail-closed default is intentional: an
    unknown SDK/credential error must be surfaced instead of causing an
    unbounded reconnect loop.  Known network failures, SDK NOT_CONNECTED and
    server/timeout failures are transient and are supervised with backoff.
    """

    if isinstance(error, MessageChannelOfflineError):
        return "transient"
    codes = _numeric_error_codes(error)
    if codes & _PERMANENT_FEISHU_CODES:
        return "permanent"
    if codes & _TRANSIENT_FEISHU_CODES:
        return "transient"
    # HTTP 4xx generally means a configuration/permission problem.  The
    # explicitly transient rate/timeout codes above are exempted.
    if any(400 <= code < 500 for code in codes):
        return "permanent"
    for item in _exception_chain(error):
        name = str(type(item).__name__).casefold()
        module = str(type(item).__module__).casefold()
        qualified = f"{module}.{name}"
        for attribute in ("code", "error_code", "reason"):
            value = getattr(item, attribute, None)
            value = getattr(value, "value", value)
            code_name = str(value or "").casefold()
            compact_code_name = "".join(
                character for character in code_name if character.isalnum()
            )
            if any(marker in code_name or marker in compact_code_name for marker in _PERMANENT_ERROR_MARKERS):
                return "permanent"
            if any(marker in code_name or marker in compact_code_name for marker in _TRANSIENT_ERROR_MARKERS):
                return "transient"
        if any(marker in qualified for marker in _PERMANENT_ERROR_MARKERS):
            return "permanent"
        if isinstance(
            item,
            (OSError, ConnectionError, TimeoutError, asyncio.TimeoutError, EOFError),
        ):
            return "transient"
        # SDK 1.2.0 may construct ExpiringCache before its own connection
        # exception wrapper and encounter the executor thread's old closed
        # loop.  Only this exact asyncio lifecycle error is retryable; other
        # RuntimeError instances remain fail-closed below.
        if isinstance(item, RuntimeError) and item.args == ("Event loop is closed",):
            return "transient"
        if any(marker in qualified for marker in _TRANSIENT_ERROR_MARKERS):
            return "transient"
    return "permanent"


def is_transient_channel_failure(error: BaseException) -> bool:
    """Return whether an exception is safe for channel-supervised retry.

    This small public predicate lets the service isolate ``RetryExhausted``
    sends without importing or depending on a particular SDK exception class.
    """

    return _connection_failure_class(error) == "transient"


SdkFactory = Callable[[str, str, str, float], Any]
# Optional additive state hooks.  They deliberately carry only a digest,
# stable logical key, filename and platform media key; the payload bytes never
# cross this boundary.  The service can bind these to its durable store.
MediaKeyLookup = Callable[[str, str, str, str], str | None]
MediaKeyStore = Callable[[str, str, str, str, str], None]
# Called after an inbound business callback fails.  The callback receives the
# complete, already-validated reply and the original exception so a guardian
# can persist a private failure spool before the SDK safety layer marks the
# source message seen.  The adapter never serializes the exception itself.
InboundFailureHandler = Callable[[ChannelReply, BaseException], None]


_MENU_DISPATCHER_HANDLER_ATTR = "_progress_wx_menu_handler"
_MENU_DISPATCHER_WRAPPER_ATTR = "_progress_wx_menu_dispatcher_wrapper"


def _menu_event_id(data: Any) -> str:
    """读取菜单事件 ID；支持固定 SDK 的对象 header 与测试映射。"""

    header = getattr(data, "header", None)
    if isinstance(header, Mapping):
        return str(header.get("event_id") or "").strip()
    return str(getattr(header, "event_id", "") or "").strip()


def _menu_event_id_hash(event_id: str) -> str:
    """返回脱敏事件标识；菜单诊断不得记录原始事件 ID。"""

    if not event_id:
        return "-"
    return hashlib.sha256(event_id.encode("utf-8")).hexdigest()[:16]


def _install_bot_menu_processors(
    dispatcher: Any,
    handler: Callable[[Any], None],
    processor_type: Any,
) -> Any:
    """在一个刚构建的固定 SDK dispatcher 上幂等装载 p1/p2 菜单。"""

    processor_map = getattr(dispatcher, "_processorMap", None)
    if not isinstance(processor_map, dict):
        raise FeishuDependencyError("当前飞书 SDK 的事件分派结构不兼容")
    event_types = tuple(
        f"{prefix}.application.bot.menu_v6" for prefix in ("p1", "p2")
    )
    # 先完整预检，避免 p1 已写入而 p2 冲突时留下半套注册。
    for event_type in event_types:
        existing = processor_map.get(event_type)
        if existing is None:
            continue
        existing_handler = getattr(existing, _MENU_DISPATCHER_HANDLER_ATTR, None)
        if existing_handler != handler:
            raise FeishuDependencyError(
                "飞书 SDK 已占用固定菜单事件处理器"
            )
    processors: dict[str, Any] = {}
    for event_type in event_types:
        if event_type in processor_map:
            continue
        processor = processor_type(handler)
        setattr(processor, _MENU_DISPATCHER_HANDLER_ATTR, handler)
        processors[event_type] = processor
    processor_map.update(processors)
    return dispatcher


def _wrap_bot_menu_dispatcher_builder(
    channel: Any,
    handler: Callable[[Any], None],
    processor_type: Any,
) -> None:
    """让 SDK 每次重建 dispatcher 时都带上固定菜单处理器。

    ``FeishuChannel.start()`` 会在真正创建 WS 之前覆盖 ``_dispatcher``，
    因而只改当前 ``channel.dispatcher`` 的旧实现会在启动时丢失。这里仅
    包装当前 channel 实例的私有构建方法，不修改已安装 SDK；新 channel
    或 SDK 自己重建 dispatcher 时都会经过同一幂等安装点。
    """

    previous_handler = getattr(channel, _MENU_DISPATCHER_WRAPPER_ATTR, None)
    if previous_handler is not None:
        if previous_handler != handler:
            raise FeishuDependencyError("固定菜单事件处理器重复绑定")
        dispatcher = getattr(channel, "_dispatcher", None)
        if dispatcher is not None:
            _install_bot_menu_processors(dispatcher, handler, processor_type)
        return

    original_builder = getattr(channel, "_build_dispatcher", None)
    if not callable(original_builder):
        raise FeishuDependencyError("当前飞书 SDK 不支持固定菜单事件接入")

    def build_with_bot_menu(*args: Any, **kwargs: Any) -> Any:
        dispatcher = original_builder(*args, **kwargs)
        return _install_bot_menu_processors(dispatcher, handler, processor_type)

    try:
        setattr(channel, _MENU_DISPATCHER_WRAPPER_ATTR, handler)
        # 这是实例属性，调用时不会额外注入 channel；original_builder 已经
        # 是原始绑定方法，故可安全地保留 SDK 的 self 语义。
        setattr(channel, "_build_dispatcher", build_with_bot_menu)
    except (AttributeError, TypeError) as exc:
        raise FeishuDependencyError("当前飞书 SDK 不支持固定菜单事件接入") from exc

    dispatcher = getattr(channel, "_dispatcher", None)
    if dispatcher is not None:
        _install_bot_menu_processors(dispatcher, handler, processor_type)


def _register_bot_menu_event(channel: Any, handler: Callable[[Any], None]) -> None:
    """注册受信固定菜单事件，兼容测试替身与固定 SDK 1.2.0。"""

    test_register = getattr(channel, "register_custom_event", None)
    if callable(test_register):
        for event_key in (
            SESSION_QUERY_MENU_EVENT_KEY,
            REMOTE_CONTROL_MENU_EVENT_KEY,
            FEATURE_CENTER_MENU_EVENT_KEY,
        ):
            test_register(event_key, handler)
        return
    try:
        from lark_channel.event.custom import CustomizedEventProcessor
    except (ImportError, AttributeError, TypeError) as exc:
        raise FeishuDependencyError("当前飞书 SDK 不支持固定菜单事件接入") from exc
    _wrap_bot_menu_dispatcher_builder(channel, handler, CustomizedEventProcessor)


def _ensure_sdk_import_loop_idle(sdk_loop: Any) -> None:
    """拒绝复用在 SDK 导入时已经运行的模块级事件循环。"""

    if sdk_loop is not None and sdk_loop.is_running():
        raise FeishuDependencyError(
            "飞书 SDK 被其他模块在运行中的事件循环内提前加载；请重启进度通知后重试"
        )


def _ensure_sdk_executor_loop_open() -> asyncio.AbstractEventLoop:
    """Ensure the SDK executor thread never reuses its previously closed loop.

    ``lark-channel-sdk`` creates ``ExpiringCache`` inside the worker used by
    ``run_in_executor`` and asks ``asyncio.get_event_loop()`` for that worker's
    thread-local loop.  Our bounded disconnect cleanup closes that private
    cache loop.  ThreadPoolExecutor may later reuse the same worker, in which
    case the SDK receives the closed loop and fails before the WebSocket can
    reconnect.  Replace only a missing/closed executor-local loop; the SDK's
    separate module-level WebSocket loop is deliberately untouched.
    """

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop
    if loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop


def _ensure_sdk_ws_loop_open(ws_client_module: Any | None = None) -> asyncio.AbstractEventLoop:
    """Repair only a closed/missing SDK module-level WebSocket loop."""

    if ws_client_module is None:
        from lark_channel.ws import client as ws_client_module
    loop = getattr(ws_client_module, "loop", None)
    if not isinstance(loop, asyncio.AbstractEventLoop) or loop.is_closed():
        loop = asyncio.new_event_loop()
        setattr(ws_client_module, "loop", loop)
    _ensure_sdk_import_loop_idle(loop)
    return loop


@lru_cache(maxsize=1)
def _official_sdk_symbols() -> tuple[Any, ...]:
    """在本工具创建 asyncio loop 前一次性载入官方 SDK。

    ``lark-channel-sdk 1.2.0`` 的底层 WS 模块会在导入时保存当前事件
    循环，之后在工作线程中调用 ``run_until_complete``。若首次导入发生在
    已运行的 loop 内，就会稳定触发 ``This event loop is already running``。
    因而绑定入口和生产通道构造器都会先在同步调用栈执行本函数；仍保持
    惰性导入，离线核心命令不会无故加载网络 SDK。
    """

    try:
        from lark_channel import (
            ChannelConfig,
            DedupConfig,
            FeishuChannel,
            InboundConfig,
            LogLevel,
            MediaCapabilities,
            MediaCacheConfig,
            OutboundConfig,
            PolicyConfig,
            RetryConfig,
            SafetyConfig,
            SecurityConfig,
            TransportConfig,
        )
        from lark_channel.ws import client as ws_client_module
    except (ImportError, AttributeError) as exc:
        raise FeishuDependencyError(
            "缺少兼容的 lark-channel-sdk；请运行飞书依赖安装脚本"
        ) from exc
    # SDK 1.2.0 在导入时把当前 loop 保存为模块全局变量。如果其他模块已在
    # 正在运行的 asyncio loop 内抢先导入它，之后 WSClient.start() 必然触发
    # “This event loop is already running”。此时只能明确拒绝并要求重启，
    # 不能冒险复用那个属于其他线程/调用方的 loop。
    _ensure_sdk_ws_loop_open(ws_client_module)
    return (
        ChannelConfig,
        DedupConfig,
        FeishuChannel,
        InboundConfig,
        LogLevel,
        MediaCapabilities,
        MediaCacheConfig,
        OutboundConfig,
        PolicyConfig,
        RetryConfig,
        SafetyConfig,
        SecurityConfig,
        TransportConfig,
    )


def _official_sdk_factory(
    app_id: str,
    app_secret: str,
    target_open_id: str,
    connect_timeout_seconds: float,
    media_cache_dir: Path | None = None,
) -> Any:
    """从已预载的官方 SDK 符号构造严格最小权限渠道。"""

    (
        ChannelConfig,
        DedupConfig,
        FeishuChannel,
        InboundConfig,
        LogLevel,
        MediaCapabilities,
        MediaCacheConfig,
        OutboundConfig,
        PolicyConfig,
        RetryConfig,
        SafetyConfig,
        SecurityConfig,
        TransportConfig,
    ) = _official_sdk_symbols()

    channel_config = ChannelConfig(
        media_cache=MediaCacheConfig(
            enabled=True,
            root_dir=media_cache_dir,
            ttl_seconds=24 * 60 * 60,
            max_entries=256,
            max_bytes=256 * 1024 * 1024,
            max_file_bytes=20 * 1024 * 1024,
            image_max_bytes=20 * 1024 * 1024,
        )
    )
    class _PersistentCardActionChannel(FeishuChannel):
        """让 SQLite event_id 去重成为卡片提交的最终幂等边界。

        SDK 1.2.0 会按卡片内容构造 action event_id，因而同一表单再次提交
        相同正文会在进入应用前被静默吞掉。这里仅为 card action 的 SDK
        safety key 加入每次分发 nonce，仍复用原 safety 队列完成同聊天串行；
        Feishu header.event_id 随后由适配层写入持久去重，不影响普通消息。
        """

        async def _through_action_safety(
            self,
            *,
            event_id: str,
            queue_scope: str,
            handler: Callable[[], Any],
        ) -> None:
            if event_id.startswith("card:"):
                event_id = f"{event_id}:delivery:{uuid.uuid4().hex}"
            await super()._through_action_safety(
                event_id=event_id,
                queue_scope=queue_scope,
                handler=handler,
            )

        def start(self) -> None:
            # ``start`` runs in the SDK's executor thread.  Repair that
            # thread's cache loop before WSClient constructs ExpiringCache.
            _ensure_sdk_executor_loop_open()
            _ensure_sdk_ws_loop_open()
            try:
                super().start()
            except RuntimeError as exc:
                # Only the SDK's exact stale-loop lifecycle failure is safe
                # to rebuild.  Unknown RuntimeError instances remain fatal.
                if exc.args == ("Event loop is closed",):
                    raise MessageChannelOfflineError(
                        "飞书 SDK 事件循环已关闭，将重新建立连接"
                    ) from exc
                raise

    return _PersistentCardActionChannel(
        config=channel_config,
        app_id=app_id,
        app_secret=app_secret,
        log_level=LogLevel.ERROR,
        transport=TransportConfig(
            kind="ws",
            auto_reconnect=False,
            handshake_timeout_seconds=connect_timeout_seconds,
        ),
        policy=PolicyConfig(
            dm_policy="allowlist" if target_open_id else "open",
            group_policy="disabled",
            allow_from=[target_open_id] if target_open_id else None,
            sender_identity_fields=["open_id"],
        ),
        safety=SafetyConfig(dedup=DedupConfig()),
        inbound=InboundConfig(
            expand_merge_forward=False,
            fetch_interactive_card=False,
            reaction_notifications="off",
            media_capabilities=MediaCapabilities(
                image=True,
                audio=False,
                video=False,
                file=False,
                sticker=False,
            ),
            # SDK 1.2.0 会在 p2p 引用中遇到 parent_id == root_id 时把
            # reply_to_message_id 归一化为空；只保留内存中的原始 message
            # 字典以读取 parent_id，不发出 raw 事件，也不记录/持久化它。
            include_raw=True,
            emit_raw_events=False,
        ),
        outbound=OutboundConfig(retry=RetryConfig(max_attempts=1)),
        security=SecurityConfig(
            mode="strict",
            strict_content_text=True,
            max_ws_fragment_parts=128,
            max_ws_fragment_bytes=8 * 1024 * 1024,
            max_concurrent_ws_handlers=4,
            resource_overflow_policy="drop",
        ),
    )


def discover_feishu_open_id(
    *,
    app_id: str,
    app_secret: str,
    pairing_code: str,
    timeout_seconds: float = 180.0,
    sdk_factory: SdkFactory | None = None,
) -> str:
    """短暂接收唯一绑定码，返回发送者 open_id；群聊与其他正文均忽略。"""

    expected = str(pairing_code or "").strip()
    if not expected:
        raise ValueError("飞书绑定码不能为空")
    if sdk_factory is None:
        # 必须发生在下方 asyncio.run() 之前；见 _official_sdk_symbols 注释。
        _official_sdk_symbols()
    factory = sdk_factory or _official_sdk_factory

    async def discover() -> str:
        channel = factory(app_id, app_secret, "", min(30.0, timeout_seconds))
        loop = asyncio.get_running_loop()
        found: asyncio.Future[str] = loop.create_future()

        async def on_message(message: Any) -> None:
            sender_id = str(getattr(message, "sender_id", "") or "")
            content = str(
                getattr(message, "safe_content_text", "")
                or getattr(message, "content_text", "")
                or ""
            ).strip()
            if (
                getattr(message, "chat_type", "") == "p2p"
                and not bool(getattr(message, "sender_is_bot", False))
                and sender_id.startswith("ou_")
                and content == expected
                and not found.done()
            ):
                found.set_result(sender_id)

        channel.on("message", on_message)
        try:
            await channel.connect_until_ready(timeout=min(30.0, timeout_seconds))
            return await asyncio.wait_for(found, timeout=timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise MessageChannelOfflineError("等待手机发送飞书绑定码超时") from exc
        finally:
            await channel.disconnect()

    return asyncio.run(discover())


class FeishuMessageChannel:
    """在独立低负载线程中监督飞书 WebSocket 与异步发送。"""

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        target_open_id: str,
        connect_timeout_seconds: float = 30.0,
        max_attempts: int = 5,
        retry_delays: tuple[float, ...] = (1, 2, 4, 8, 16),
        error_handler: ChannelErrorHandler | None = None,
        inbound_failure_handler: InboundFailureHandler | None = None,
        sdk_factory: SdkFactory | None = None,
        media_cache_dir: str | Path | None = None,
        media_key_lookup: MediaKeyLookup | None = None,
        media_key_store: MediaKeyStore | None = None,
    ) -> None:
        self.app_id = str(app_id or "").strip()
        self._app_secret = str(app_secret or "")
        self.target_open_id = str(target_open_id or "").strip()
        self.connect_timeout_seconds = float(connect_timeout_seconds)
        self.max_attempts = int(max_attempts)
        self.retry_delays = tuple(float(item) for item in retry_delays)
        if not self.app_id or not self._app_secret or not self.target_open_id:
            raise ValueError("app_id、app_secret 和 target_open_id 均不能为空")
        if not 1 <= self.max_attempts <= 5 or len(self.retry_delays) < self.max_attempts:
            raise ValueError("飞书重试必须提供一到五次及足够的退避间隔")
        if sdk_factory is None:
            # 构造器由同步服务初始化路径调用，在线程内 asyncio.run() 前预载。
            _official_sdk_symbols()
        self._error_handler = error_handler or (lambda _error: None)
        # Guardian binds this after construction, so keep the hook public and
        # read it at dispatch time rather than capturing a stale callback.
        self.inbound_failure_handler = inbound_failure_handler
        self.media_cache_dir = Path(
            media_cache_dir or (Path.cwd() / ".state" / "feishu-media")
        ).resolve()
        self.media_cache_dir.mkdir(parents=True, exist_ok=True)
        self._sdk_factory = (
            sdk_factory
            if sdk_factory is not None
            else lambda app_id, app_secret, target_open_id, timeout: _official_sdk_factory(
                app_id,
                app_secret,
                target_open_id,
                timeout,
                self.media_cache_dir,
            )
        )
        self._media_key_lookup = media_key_lookup
        self._media_key_store = media_key_store
        self._on_reply: ReplyHandler | None = None
        self._stop_event = threading.Event()
        self._online_event = threading.Event()
        self._start_event = threading.Event()
        self._start_error: BaseException | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._channel: Any = None
        self._lifecycle_lock = threading.RLock()
        self._recipient_scope_lock = threading.RLock()
        self._recipient_scopes: OrderedDict[str, tuple[str, str]] = OrderedDict()
        # A media upload is separate from the message create call in the
        # official SDK.  Keep the successful key by logical idempotency key so
        # a retry after an uncertain message response can reuse that upload.
        # The digest/name guard prevents accidental reuse when a caller
        # violates the stable-key contract with different bytes.
        self._media_upload_lock = threading.RLock()
        self._media_uploads: OrderedDict[
            tuple[str, str], tuple[str, str, str, bool]
        ] = OrderedDict()
        # 连接监督状态只保存在内存中；服务层可通过 connection_snapshot() 采样
        # 到自己的健康表。这里绝不保存 app secret、URL 或异常正文。
        self._health_state = "stopped"
        self._health_failure_class = ""
        self._health_failure_type = ""
        self._health_consecutive_failures = 0
        self._health_retry_deadline = 0.0
        self._health_ever_connected = False
        self._health_last_transition_at = 0.0
        # 官方 SDK 1.2.0 的 stop()/disconnect() 不是并发安全的；外部 stop
        # 与 worker 的 finally 若同时调用，会让 DeviceFlowClient.close() 留下
        # 未 await 协程。按 SDK channel 对象去重，确保每个实例只清理一次。
        self._seen_lock = threading.Lock()
        self._seen_message_ids: OrderedDict[str, None] = OrderedDict()

    @staticmethod
    def _raw_event_id(raw: object) -> str:
        if not isinstance(raw, dict):
            return ""
        header = raw.get("header")
        if not isinstance(header, dict):
            return ""
        return str(header.get("event_id") or "").strip()

    def _dispatch_reply(self, reply: ChannelReply) -> None:
        callback = self._on_reply
        if callback is None:
            return
        try:
            callback(reply)
        except BaseException as exc:
            failure_handler = self.inbound_failure_handler
            if failure_handler is not None:
                try:
                    failure_handler(reply, exc)
                except BaseException as failure_exc:
                    # A failure journal is itself an external boundary.  Keep
                    # the original callback error visible and report a broken
                    # journal separately; neither is claimed as delivered.
                    self._error_handler(failure_exc)
                    self._error_handler(exc)
                    raise
            self._error_handler(exc)

    def set_inbound_failure_handler(
        self, handler: InboundFailureHandler | None
    ) -> None:
        """Bind or clear the guardian's durable inbound-failure sink.

        Guardian is constructed after the channel in the standalone launcher,
        so this setter keeps the adapter integration explicit without exposing
        mutable callback state to the SDK object.
        """

        if handler is not None and not callable(handler):
            raise TypeError("inbound_failure_handler 必须是可调用对象或 None")
        with self._lifecycle_lock:
            self.inbound_failure_handler = handler

    async def _handle_card_action(self, event: Any) -> None:
        """把受信卡片按钮变成绑定原卡片的既有精确管理命令。"""

        sender_id = str(getattr(getattr(event, "operator", None), "open_id", "") or "")
        message_id = str(getattr(event, "message_id", "") or "").strip()
        chat_id = str(getattr(event, "chat_id", "") or "").strip()
        action = getattr(event, "action", None)
        if (
            sender_id != self.target_open_id
            or not message_id
            or not chat_id
            or str(getattr(action, "tag", "") or "") != "button"
        ):
            return
        action_value = getattr(action, "value", None)
        form_value = getattr(action, "form_value", None)
        command = session_query_command(action_value)
        if command is None:
            command = session_query_form_command(
                action_value,
                form_value,
            )
        if command is None:
            command = remote_control_command(
                action_value,
                form_value,
            )
        if command is None and form_value in (None, {}):
            command = feature_center_action_command(action_value)
        if command is None and form_value in (None, {}):
            command = feature_operation_action_command(action_value)
        if command is None:
            command = slash_card_command(
                action_value,
                form_value,
            )
        if command is None:
            return
        raw = getattr(event, "raw", None)
        event_id = self._raw_event_id(raw)
        if not event_id:
            stable = json.dumps(
                {
                    "message_id": message_id,
                    "sender_id": sender_id,
                    "value": action_value,
                    "name": getattr(action, "name", None),
                    "form_value": getattr(action, "form_value", None),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            event_id = hashlib.sha256(stable.encode("utf-8")).hexdigest()
        self._dispatch_reply(
            ChannelReply(
                sender_id=sender_id,
                content=command,
                reply_to_message_id=message_id,
                message_id=f"feishu-card-{event_id}",
                chat_id=chat_id,
                source_kind="card_action",
                action_name=str(action_value.get("action") or "")
                if isinstance(action_value, Mapping)
                else "",
                action_fingerprint=(
                    session_query_action_fingerprint(action_value)
                    or remote_control_action_fingerprint(action_value)
                    or feature_center_action_fingerprint(action_value)
                    or feature_operation_action_fingerprint(action_value)
                    or slash_card_action_fingerprint(action_value)
                    or ""
                ),
            )
        )

    async def _handle_bot_menu(self, data: Any) -> None:
        """只接受 owner 点击的固定“查询会话”菜单事件。"""

        event = getattr(data, "event", None)
        event_id = _menu_event_id(data)
        event_id_hash = _menu_event_id_hash(event_id)
        if not isinstance(event, Mapping):
            LOGGER.info(
                "固定菜单事件结果=reject_invalid_payload event_id_hash=%s",
                event_id_hash,
            )
            return
        operator = event.get("operator")
        operator_id = (
            operator.get("operator_id")
            if isinstance(operator, Mapping)
            else None
        )
        sender_id = (
            str(operator_id.get("open_id") or "")
            if isinstance(operator_id, Mapping)
            else ""
        )
        event_key = str(event.get("event_key") or "")
        menu_commands = {
            SESSION_QUERY_MENU_EVENT_KEY: SESSION_QUERY_ENTRY_COMMAND,
            REMOTE_CONTROL_MENU_EVENT_KEY: REMOTE_CONTROL_ENTRY_COMMAND,
            FEATURE_CENTER_MENU_EVENT_KEY: FEATURE_CENTER_ENTRY_COMMAND,
        }
        chat_type = str(event.get("chat_type") or "").strip().casefold()
        if not event_id:
            LOGGER.info(
                "固定菜单事件结果=reject_missing_event_id event_id_hash=%s",
                event_id_hash,
            )
            return
        if event_key not in menu_commands:
            LOGGER.info(
                "固定菜单事件结果=reject_unknown_key event_id_hash=%s",
                event_id_hash,
            )
            return
        if sender_id != self.target_open_id:
            LOGGER.info(
                "固定菜单事件结果=reject_unauthorized_sender event_id_hash=%s",
                event_id_hash,
            )
            return
        if chat_type not in {"", "p2p"}:
            LOGGER.info(
                "固定菜单事件结果=reject_invalid_chat_type event_id_hash=%s",
                event_id_hash,
            )
            return
        LOGGER.info(
            "固定菜单事件结果=accepted event_id_hash=%s",
            event_id_hash,
        )
        self._dispatch_reply(
            ChannelReply(
                sender_id=sender_id,
                content=menu_commands[event_key],
                reply_to_message_id="",
                message_id=f"feishu-menu-{event_id}",
                chat_id=str(event.get("chat_id") or ""),
                source_kind="bot_menu",
            )
        )

    def _schedule_bot_menu(self, data: Any) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(self._handle_bot_menu(data), loop)

    def _set_health(
        self,
        state: str,
        *,
        failure: BaseException | None = None,
        failure_class: str | None = None,
        failure_type: str | None = None,
        consecutive_failures: int | None = None,
        retry_deadline: float | None = None,
        ever_connected: bool | None = None,
    ) -> None:
        """更新脱敏连接状态，供 service/status 采样。"""

        with self._lifecycle_lock:
            previous = self._health_state
            self._health_state = str(state)
            if failure is not None:
                self._health_failure_type = str(type(failure).__name__)
                self._health_failure_class = str(
                    failure_class or _connection_failure_class(failure)
                )
            elif failure_class is not None:
                self._health_failure_class = str(failure_class)
            if failure_type is not None:
                self._health_failure_type = str(failure_type)
            if consecutive_failures is not None:
                self._health_consecutive_failures = max(0, int(consecutive_failures))
            if retry_deadline is not None:
                self._health_retry_deadline = max(0.0, float(retry_deadline))
            if ever_connected is not None:
                self._health_ever_connected = bool(ever_connected)
            if previous != self._health_state:
                self._health_last_transition_at = time.time()

    def connection_snapshot(self) -> dict[str, object]:
        """Return a safe, non-secret snapshot of channel supervision state.

        ``retry_in_seconds`` is intentionally relative rather than an absolute
        timestamp so this API cannot disclose machine clock details or paths.
        """

        with self._lifecycle_lock:
            retry_deadline = self._health_retry_deadline
            state = self._health_state
            failure_class = self._health_failure_class
            failure_type = self._health_failure_type
            consecutive_failures = self._health_consecutive_failures
            ever_connected = self._health_ever_connected
            last_transition_at = self._health_last_transition_at
            thread = self._thread
        return {
            "state": state,
            "last_failure_class": failure_class,
            "last_failure_type": failure_type,
            "consecutive_failures": consecutive_failures,
            "retry_in_seconds": round(max(0.0, retry_deadline - time.monotonic()), 3),
            "ever_connected": ever_connected,
            "last_transition_at": last_transition_at,
            "thread_alive": thread.is_alive() if thread is not None else None,
        }

    @staticmethod
    def _connected(channel: Any) -> bool:
        try:
            snapshot = channel.connection_snapshot()
            ws_client = channel.ws_client
            return bool(snapshot.ready and ws_client is not None and ws_client._conn is not None)
        except (AttributeError, RuntimeError):
            return False

    def _remember_inbound(self, message_id: str) -> bool:
        """入站 message_id 进程内去重；持久层还会做第二次唯一消费。"""

        with self._seen_lock:
            if message_id in self._seen_message_ids:
                return False
            self._seen_message_ids[message_id] = None
            if len(self._seen_message_ids) > 2048:
                self._seen_message_ids.popitem(last=False)
            return True

    @staticmethod
    def _reply_to_message_id(message: Any) -> str:
        """取得结构化引用 ID，并兼容 SDK 1.2.0 的 p2p 归一化缺陷。"""

        normalized = str(getattr(message, "reply_to_message_id", "") or "").strip()
        if normalized:
            return normalized
        if str(getattr(message, "chat_type", "") or "").casefold() != "p2p":
            return ""
        raw = getattr(message, "raw", None)
        if not isinstance(raw, dict):
            return ""
        # 飞书原始事件只有真正回复消息时才携带 parent_id。即使平台同时把
        # root_id 设为同一个值，也仍是可验证的引用；下游还必须精确命中
        # 本工具保存的出站 message_id，绝不凭正文或 root_id 猜测关联。
        return str(raw.get("parent_id") or "").strip()

    async def _handle_single_message(self, message: Any) -> None:
        """逐条执行严格过滤；是否允许未引用入口由主服务精确判定。"""

        sender_id = str(getattr(message, "sender_id", "") or "")
        message_id = str(getattr(message, "message_id", "") or "")
        reply_to = self._reply_to_message_id(message)
        chat_type = str(getattr(message, "chat_type", "") or "").casefold()
        content_type = str(getattr(message, "raw_content_type", "") or "").casefold()
        sender_is_bot = bool(getattr(message, "sender_is_bot", False))
        if (
            sender_id != self.target_open_id
            or sender_is_bot
            or chat_type != "p2p"
            or content_type not in {"text", "image", "post"}
            or not message_id
        ):
            return
        if not self._remember_inbound(message_id):
            return
        content = str(
            getattr(message, "safe_content_text", "")
            or getattr(message, "content_text", "")
            or ""
        )
        resources = list(getattr(message, "resources", None) or [])
        image_resources = [
            item for item in resources if str(getattr(item, "type", "")) == "image"
        ]
        if content_type in {"image", "post"} and not image_resources:
            return
        if content_type == "image":
            # SDK 的 image 正文是 file_key 占位符，不是用户说明，不能送入 Codex。
            content = ""
        attachments: list[ChannelAttachment] = []
        attachment_error = ""
        if image_resources:
            if not reply_to:
                # 图片没有精确引用时无法确定目标 Codex 任务，不下载也不猜测。
                return
            sdk_channel = self._channel
            if sdk_channel is None:
                attachment_error = "飞书图片通道尚未就绪，请稍后重试。"
            else:
                cached = await sdk_channel.resolve_resources_to_cache(
                    message_id=message_id,
                    resources=image_resources,
                )
                for item in cached:
                    if str(getattr(item, "decision", "")) != "cached":
                        continue
                    raw_path = getattr(item, "path", None)
                    mime_type = str(getattr(item, "mime_type", "") or "").casefold()
                    size = int(getattr(item, "size", 0) or 0)
                    sha256 = str(getattr(item, "sha256", "") or "").casefold()
                    if raw_path is None:
                        continue
                    path = Path(raw_path).resolve()
                    if (
                        not path.is_relative_to(self.media_cache_dir)
                        or mime_type not in {"image/png", "image/jpeg", "image/gif", "image/webp"}
                        or not 0 < size <= 20 * 1024 * 1024
                        or len(sha256) != 64
                        or not path.is_file()
                    ):
                        continue
                    attachments.append(
                        ChannelAttachment(str(path), mime_type, sha256, size)
                    )
                if not attachments:
                    reasons = [
                        str(getattr(item, "reason", "") or "")
                        for item in cached
                        if str(getattr(item, "reason", "") or "")
                    ]
                    reason = reasons[0] if reasons else ""
                    if reason == "download_failed":
                        attachment_error = (
                            "飞书未允许机器人读取这张消息图片（download_failed）。"
                            "请在开放平台开通 im:message:readonly，发布新版本并重启后台服务后"
                            "重新发送；im:resource 不能替代这项消息读取权限。"
                        )
                    else:
                        suffix = f"（{reason}）" if reason else ""
                        attachment_error = (
                            f"图片未能通过安全下载或格式校验{suffix}，请重新发送。"
                        )
        if not content.strip() and not attachments and not attachment_error:
            return
        callback = self._on_reply
        if callback is None:
            return
        self._dispatch_reply(
            ChannelReply(
                sender_id=sender_id,
                content=content,
                reply_to_message_id=reply_to,
                message_id=message_id,
                chat_id=str(getattr(message, "chat_id", "") or ""),
                attachments=tuple(attachments),
                attachment_error=attachment_error,
                created_at=(lambda stamp: stamp // 1000 if stamp >= 10**12 else stamp)(int(getattr(message, "create_time", 0) or 0)),
            )
        )

    async def _handle_message(self, message: Any) -> None:
        """拆分 SDK 批处理，避免普通消息与引用回复正文被错误合并。"""

        raw_sources = getattr(message, "batched_sources", None)
        sources = raw_sources if isinstance(raw_sources, list) and raw_sources else [message]
        for source in sources:
            await self._handle_single_message(source)

    @staticmethod
    def _sdk_ws_loop(channel: Any) -> asyncio.AbstractEventLoop | None:
        """取得官方 SDK WS 私有 loop；不存在时不做猜测性清理。"""

        ws = getattr(channel, "_ws_client", None)
        loop = getattr(ws, "_loop", None)
        return loop if isinstance(loop, asyncio.AbstractEventLoop) else None

    @staticmethod
    def _is_sdk_task(task: asyncio.Task[Any]) -> bool:
        """只接管 lark-channel 自己创建的任务，避免误伤宿主事件循环。"""

        try:
            coroutine = task.get_coro()
            code = getattr(coroutine, "cr_code", None) or getattr(
                coroutine, "gi_code", None
            )
            filename = str(getattr(code, "co_filename", "")).casefold()
        except BaseException:
            return False
        return "lark_channel" in filename

    @classmethod
    def _snapshot_sdk_tasks(
        cls, channel: Any
    ) -> tuple[asyncio.AbstractEventLoop | None, tuple[asyncio.Task[Any], ...]]:
        """在 SDK stop 清空私有字段前保存 WS 任务引用，供后续回收异常。"""

        loop = cls._sdk_ws_loop(channel)
        if loop is None or loop.is_closed():
            return loop, ()
        try:
            tasks = tuple(task for task in asyncio.all_tasks(loop) if cls._is_sdk_task(task))
        except RuntimeError:
            # loop 恰好在关闭；SDK 自己仍会尽力释放连接，不能阻塞主服务。
            return loop, ()
        return loop, tasks

    @classmethod
    def _snapshot_sdk_task_groups(
        cls, channel: Any
    ) -> tuple[
        asyncio.AbstractEventLoop | None,
        tuple[tuple[asyncio.AbstractEventLoop, tuple[asyncio.Task[Any], ...]], ...],
    ]:
        """收集 WS loop 及 ExpiringCache 私有 loop 上的 SDK 任务。"""

        ws_loop, ws_tasks = cls._snapshot_sdk_tasks(channel)
        groups: dict[asyncio.AbstractEventLoop, list[asyncio.Task[Any]]] = {}
        if ws_loop is not None and ws_tasks:
            groups.setdefault(ws_loop, []).extend(ws_tasks)

        ws = getattr(channel, "_ws_client", None)
        cache = getattr(ws, "_cache", None)
        cron = getattr(cache, "_cron", None)
        if isinstance(cron, asyncio.Task):
            try:
                cron_loop = cron.get_loop()
            except RuntimeError:
                cron_loop = None
            if cron_loop is not None and not cron_loop.is_closed():
                bucket = groups.setdefault(cron_loop, [])
                if cron not in bucket:
                    bucket.append(cron)
        return ws_loop, tuple(
            (task_loop, tuple(tasks)) for task_loop, tasks in groups.items()
        )

    @staticmethod
    async def _cancel_sdk_tasks(tasks: tuple[asyncio.Task[Any], ...]) -> None:
        """在任务所属 loop 内取消并 await SDK 任务，消费 close 1000 异常。"""

        current = asyncio.current_task()
        pending = [task for task in tasks if task is not current and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        # 某些 SDK 任务在清理前已经因 ConnectionClosedOK 结束；显式读取
        # exception()，避免解释器退出时输出 “Task exception was never retrieved”。
        for task in tasks:
            if task is current or not task.done() or task.cancelled():
                continue
            try:
                task.exception()
            except BaseException:
                pass

    @classmethod
    async def _drain_sdk_tasks(
        cls,
        loop: asyncio.AbstractEventLoop | None,
        tasks: tuple[asyncio.Task[Any], ...],
    ) -> None:
        """线程安全地驱动 SDK loop 一小段，完成取消任务。"""

        if loop is None or loop.is_closed() or not tasks:
            return
        coroutine = cls._cancel_sdk_tasks(tasks)
        if loop.is_running():
            try:
                future = asyncio.run_coroutine_threadsafe(coroutine, loop)
            except RuntimeError:
                # run_coroutine_threadsafe 在 loop 竞态关闭时不会接管 coroutine。
                coroutine.close()
                return
            try:
                await asyncio.wait_for(asyncio.wrap_future(future), timeout=2)
            except BaseException:
                future.cancel()
            return
        try:
            # 当前函数本身运行在服务 worker loop 中，不能在同一线程嵌套
            # run_until_complete；把已停止的 SDK loop 放到执行器线程短暂驱动。
            await asyncio.wait_for(
                asyncio.to_thread(loop.run_until_complete, coroutine),
                timeout=2,
            )
        except BaseException:
            coroutine.close()

    @staticmethod
    def _normal_close_filter(stop_requested: bool) -> logging.Filter | None:
        """主动停止时仅屏蔽 SDK 已知的正常 close 1000/1001 ERROR。"""

        if not stop_requested:
            return None

        class NormalCloseFilter(logging.Filter):
            def filter(self, record: logging.LogRecord) -> bool:
                message = record.getMessage().casefold()
                if "receive message loop exit" not in message:
                    return True
                return not (
                    "1000 (ok)" in message
                    or "1001 (going away)" in message
                    or "connectionclosedok" in message
                )

        return NormalCloseFilter()

    @staticmethod
    def _serialize_official_sdk_shutdown(channel: Any) -> None:
        """阻止 SDK 1.2.0 的 start 执行器与 disconnect 并发清理。

        WS loop 被 stop 后，官方 ``FeishuChannel.start`` 的异常分支还会调用
        ``_cleanup_failed_start``；与此同时 ``FeishuChannel.stop`` 正在关闭
        DeviceFlow 后台 loop。两边竞争会留下未 await 的 close 协程。该通道
        已进入一次性销毁流程，因此让 disconnect 成为唯一清理所有者最安全。
        """

        module_name = str(type(channel).__module__ or "")
        cleanup = getattr(channel, "_cleanup_failed_start", None)
        if not module_name.startswith("lark_channel.") or not callable(cleanup):
            return
        try:
            setattr(channel, "_cleanup_failed_start", lambda _generation: None)
        except (AttributeError, TypeError):
            # 未来 SDK 若改成 slots/只读属性，则退回官方行为，不猜测性修改。
            return

    async def _disconnect(self, channel: Any) -> None:
        with self._lifecycle_lock:
            # 标记跟随 SDK 实例本身销毁，避免长期重连时 Python 复用 id(channel)
            # 而把全新的连接误判为已经清理。
            if bool(getattr(channel, "_progress_wx_disconnect_started", False)):
                return
            setattr(channel, "_progress_wx_disconnect_started", True)
        sdk_loop, sdk_task_groups = self._snapshot_sdk_task_groups(channel)
        close_filter = self._normal_close_filter(self._stop_event.is_set())
        sdk_logger = logging.getLogger("Lark") if close_filter is not None else None
        if sdk_logger is not None:
            sdk_logger.addFilter(close_filter)
        try:
            # SDK disconnect 必须完整 await；外部 stop 不再并发调用同一对象。
            self._serialize_official_sdk_shutdown(channel)
            await channel.disconnect()
        except Exception:
            LOGGER.debug("飞书连接清理失败", exc_info=True)
        finally:
            # SDK stop 只请求 WS loop 停止，并不 join 运行 start() 的执行器。
            # 给它一个严格有界的退出窗口，再在当前线程驱动残留任务完成取消。
            for task_loop, sdk_tasks in sdk_task_groups:
                deadline = time.monotonic() + 2.0
                while task_loop.is_running() and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                await self._drain_sdk_tasks(task_loop, sdk_tasks)
                # ExpiringCache 在 SDK start 执行器线程中创建一个从未运行的
                # 私有 loop；该 loop 不会被未来通道复用，回收任务后即可关闭。
                if (
                    task_loop is not sdk_loop
                    and not task_loop.is_running()
                    and not task_loop.is_closed()
                ):
                    task_loop.close()
            if sdk_logger is not None:
                sdk_logger.removeFilter(close_filter)

    async def _wait_or_stop(self, delay: float) -> None:
        deadline = time.monotonic() + max(0.0, delay)
        while not self._stop_event.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(min(0.2, max(0.0, deadline - time.monotonic())))

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        ever_connected = False
        consecutive_failures = 0
        while not self._stop_event.is_set():
            channel: Any | None = None
            self._set_health(
                "connecting",
                retry_deadline=0.0,
                ever_connected=ever_connected,
            )
            try:
                # 创建 SDK、注册回调和握手均在监督循环内。暂时网络故障不能
                # 终止本线程；依赖、凭据或权限错误仍由 start/error_handler
                # 明确暴露并 fail-closed。
                channel = self._sdk_factory(
                    self.app_id,
                    self._app_secret,
                    self.target_open_id,
                    self.connect_timeout_seconds,
                )
                channel.on("message", self._handle_message)
                channel.on("cardAction", self._handle_card_action)
                _register_bot_menu_event(channel, self._schedule_bot_menu)
                with self._lifecycle_lock:
                    self._channel = channel
                await channel.connect_until_ready(timeout=self.connect_timeout_seconds)
                if not self._connected(channel):
                    raise MessageChannelOfflineError("飞书握手完成后未进入可收发状态")
                ever_connected = True
                consecutive_failures = 0
                self._set_health(
                    "online",
                    failure_class="",
                    consecutive_failures=0,
                    retry_deadline=0.0,
                    ever_connected=True,
                )
                self._online_event.set()
                self._start_event.set()
                while not self._stop_event.is_set():
                    if not self._connected(channel):
                        raise MessageChannelOfflineError("飞书 WebSocket 已断开")
                    await asyncio.sleep(0.2)
                return
            except BaseException as exc:
                self._online_event.clear()
                if self._stop_event.is_set():
                    self._set_health("stopping", retry_deadline=0.0)
                    return
                failure_class = _connection_failure_class(exc)
                if failure_class != "transient":
                    self._set_health(
                        "failed",
                        failure=exc,
                        failure_class=failure_class,
                        retry_deadline=0.0,
                        ever_connected=ever_connected,
                    )
                    # 初始凭据/依赖/参数失败仍需让 _initialize 明确失败；
                    # 运行中同类错误交给 service 的致命隔离路径。
                    if not self._start_event.is_set():
                        self._start_error = exc
                        self._start_event.set()
                    else:
                        self._error_handler(exc)
                    return
                consecutive_failures += 1
                delay_index = min(
                    consecutive_failures - 1,
                    len(self.retry_delays) - 1,
                )
                # 配置中的 0 主要用于单元测试；生产监督仍保留最小间隔，
                # 防止网络断开时创建/销毁 SDK 形成忙循环。
                delay = max(0.2, self.retry_delays[delay_index])
                retry_deadline = time.monotonic() + delay
                self._set_health(
                    "offline",
                    failure=exc,
                    failure_class="transient",
                    consecutive_failures=consecutive_failures,
                    retry_deadline=retry_deadline,
                    ever_connected=ever_connected,
                )
                if (
                    consecutive_failures == 1
                    or consecutive_failures % self.max_attempts == 0
                ):
                    LOGGER.warning(
                        "飞书连接暂时不可用，将在 %.1f 秒后重试（阶段=%s，异常类型=%s，连续失败=%d）",
                        delay,
                        "运行中断线" if ever_connected else "初始连接",
                        type(exc).__name__,
                        consecutive_failures,
                    )
                if not self._start_event.is_set():
                    self._start_event.set()
                await self._wait_or_stop(delay)
            finally:
                if channel is not None:
                    await self._disconnect(channel)
                with self._lifecycle_lock:
                    if self._channel is channel:
                        self._channel = None
        self._online_event.clear()

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except BaseException as exc:
            self._online_event.clear()
            self._set_health(
                "failed",
                failure=exc,
                failure_class="permanent",
                retry_deadline=0.0,
            )
            if not self._start_event.is_set():
                self._start_error = exc
                self._start_event.set()
            elif not self._stop_event.is_set():
                self._error_handler(exc)
        finally:
            self._online_event.clear()
            self._loop = None

    def start(self, on_reply: ReplyHandler) -> None:
        with self._lifecycle_lock:
            if self.is_online():
                self._on_reply = on_reply
                return
            if self._thread is not None and self._thread.is_alive():
                raise MessageChannelError("飞书连接正在启动或停止")
            self._on_reply = on_reply
            self._stop_event.clear()
            self._online_event.clear()
            self._start_event.clear()
            self._start_error = None
            self._set_health(
                "starting",
                failure_class="",
                failure_type="",
                consecutive_failures=0,
                retry_deadline=0.0,
                ever_connected=False,
            )
            self._thread = threading.Thread(
                target=self._thread_main,
                name="progress-feishu-ws",
                daemon=True,
            )
            self._thread.start()
        if not self._start_event.wait(self.connect_timeout_seconds + 5):
            self.stop()
            raise MessageChannelOfflineError("等待飞书 WebSocket 就绪超时")
        if self._start_error is not None:
            error = self._start_error
            if self._thread is not None:
                self._thread.join(timeout=2)
            raise MessageChannelOfflineError("飞书 WebSocket 启动失败") from error
        # 初始网络暂时不可用时 _run 已进入脱敏 offline 状态并继续退避；
        # start() 成功表示监督线程已建立，而不是承诺此刻网络在线。
        if self._thread is None or not self._thread.is_alive():
            raise MessageChannelOfflineError("飞书连接监督线程未保持运行")

    @staticmethod
    def _coerce_raw_code(value: object) -> int | None:
        value = getattr(value, "value", value)
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            try:
                return int(value.strip())
            except ValueError:
                return None
        return None

    @classmethod
    def _sdk_error_metadata(
        cls, error: BaseException
    ) -> tuple[str, int | None, str | None, bool, dict[str, object]]:
        """Read only typed metadata from an SDK error chain.

        ``resolve_media_key`` deliberately stores the API's ``raw_code`` and
        ``raw_msg`` in ``FeishuChannelError.context``.  Keep those fields when
        adapting the SDK boundary; do not parse or log the exception text.
        """

        code = ""
        raw_code: int | None = None
        raw_msg: str | None = None
        retryable: bool | None = None
        context: dict[str, object] = {}
        for item in _exception_chain(error):
            item_context = getattr(item, "context", None)
            if isinstance(item_context, Mapping):
                for key, value in item_context.items():
                    if isinstance(key, str):
                        context[key] = value
            if not code:
                candidate = getattr(item, "code", None)
                candidate = getattr(candidate, "value", candidate)
                if candidate is not None and not isinstance(candidate, (dict, list)):
                    code = str(candidate).strip()
            if raw_code is None:
                raw_code = cls._coerce_raw_code(
                    context.get("raw_code", getattr(item, "raw_code", None))
                )
            if raw_msg is None:
                candidate_msg = context.get("raw_msg", getattr(item, "raw_msg", None))
                if candidate_msg is not None:
                    raw_msg = str(candidate_msg)
            candidate_retryable = getattr(item, "retryable", None)
            if retryable is None and isinstance(candidate_retryable, bool):
                retryable = candidate_retryable

        code = code or "unknown"
        if raw_code is None:
            raw_code = cls._coerce_raw_code(context.get("raw_code"))
        if raw_msg is None and context.get("raw_msg") is not None:
            raw_msg = str(context["raw_msg"])
        if raw_code is not None:
            context.setdefault("raw_code", raw_code)
        if raw_msg is not None:
            context.setdefault("raw_msg", raw_msg)
        if retryable is None:
            retryable = (
                code == "rate_limited"
                or raw_code in _MEDIA_RETRYABLE_RAW_CODES
                or _connection_failure_class(error) == "transient"
            )
        return code, raw_code, raw_msg, bool(retryable), context

    @classmethod
    def _raise_media_exception(
        cls,
        error: BaseException,
        *,
        uploaded: bool,
        media_key: str | None,
        idempotency_key: str,
    ) -> None:
        """Convert one SDK boundary failure to the guardian-facing outcome."""

        if isinstance(error, FeishuSendNotSubmittedError):
            raise error
        if isinstance(error, FeishuSendRejectedError):
            raise error
        if isinstance(error, FeishuSendError):
            if uploaded:
                raise FeishuSendError(
                    "飞书媒体已上传但消息结果未知",
                    uploaded=True,
                    media_key=media_key,
                    idempotency_key=idempotency_key,
                ) from error
            raise error

        code, raw_code, raw_msg, retryable, context = cls._sdk_error_metadata(error)
        if code == "not_connected":
            raise FeishuSendNotSubmittedError("飞书调用尚未提交") from error

        explicit_rejection_codes = {
            "format_error",
            "permission_denied",
            "rate_limited",
            "target_revoked",
            "upload_failed",
            "ssrf_blocked",
        }
        if not uploaded or code in explicit_rejection_codes:
            # An upload transport timeout is still classified as retryable at
            # the upload boundary.  Once the upload has returned a key, the
            # message boundary below becomes the durable uncertain state.
            rejected_type = (
                FeishuPayloadRejectedError
                if code == "format_error" or raw_code == 230099
                else FeishuSendRejectedError
            )
            raise rejected_type(
                code=code if code != "unknown" else "upload_failed",
                raw_code=raw_code,
                retryable=retryable,
                context=context,
                raw_msg=raw_msg,
            ) from error

        raise FeishuSendError(
            "飞书媒体已上传但消息结果未知",
            uploaded=True,
            media_key=media_key,
            idempotency_key=idempotency_key,
        ) from error

    @classmethod
    def _message_ids_from_send_result(
        cls,
        result: Any,
        *,
        uploaded: bool = False,
        media_key: str | None = None,
        idempotency_key: str | None = None,
    ) -> str | tuple[str, ...]:
        if not bool(getattr(result, "success", False)):
            error = getattr(result, "error", None)
            raw = getattr(result, "raw", None)
            raw_mapping = raw if isinstance(raw, Mapping) else {}
            error_code = getattr(error, "code", "unknown")
            code = str(getattr(error_code, "value", error_code) or "unknown")
            context = getattr(error, "context", None)
            context = dict(context) if isinstance(context, Mapping) else {}
            raw_code = cls._coerce_raw_code(
                getattr(error, "raw_code", context.get("raw_code"))
            )
            if raw_code is None:
                raw_code = cls._coerce_raw_code(raw_mapping.get("code"))
            raw_msg = context.get("raw_msg", getattr(error, "raw_msg", None))
            if raw_msg is None and raw_mapping.get("msg") is not None:
                raw_msg = str(raw_mapping.get("msg"))
            if raw_code is not None:
                context.setdefault("raw_code", raw_code)
            if raw_msg is not None:
                context.setdefault("raw_msg", raw_msg)
            retryable = getattr(error, "retryable", None)
            if not isinstance(retryable, bool):
                retryable = (
                    code == "rate_limited"
                    or raw_code in _MEDIA_RETRYABLE_RAW_CODES
                )
            if code == "not_connected":
                raise FeishuSendNotSubmittedError("飞书调用尚未提交")
            if uploaded and code in {"unknown", "send_timeout"}:
                raise FeishuSendError(
                    "飞书媒体已上传但消息结果未知",
                    uploaded=True,
                    media_key=media_key,
                    idempotency_key=idempotency_key,
                )
            rejected_type = (
                FeishuPayloadRejectedError
                if code == "format_error" or raw_code == 230099
                else FeishuSendRejectedError
            )
            raise rejected_type(
                code=code,
                raw_code=raw_code,
                retryable=bool(retryable),
                context=context,
                raw_msg=str(raw_msg) if raw_msg is not None else None,
            )
        message_id = str(getattr(result, "message_id", "") or "")
        if not message_id:
            if uploaded:
                raise FeishuSendError(
                    "飞书媒体已上传但消息结果未知",
                    uploaded=True,
                    media_key=media_key,
                    idempotency_key=idempotency_key,
                )
            raise FeishuSendError("飞书发送成功但没有返回 message_id")
        raw_chunks = getattr(result, "chunk_ids", None)
        if raw_chunks:
            chunk_ids = tuple(
                dict.fromkeys(str(item or "").strip() for item in raw_chunks)
            )
            if (
                not chunk_ids
                or chunk_ids[0] != message_id
                or any(not item for item in chunk_ids)
            ):
                if uploaded:
                    raise FeishuSendError(
                        "飞书媒体已上传但消息结果未知",
                        uploaded=True,
                        media_key=media_key,
                        idempotency_key=idempotency_key,
                    )
                raise FeishuSendError("飞书分片发送返回了无效 message_id 列表")
            return chunk_ids
        return message_id

    def _remember_send_result_scope(
        self,
        result: Any,
        message_ids: str | tuple[str, ...],
    ) -> None:
        """缓存官方发送结果里的收件会话；不保存消息正文或凭据。"""

        raw = getattr(result, "raw", None)
        data = raw.get("data") if isinstance(raw, Mapping) else None
        chat_id = (
            str(data.get("chat_id") or "").strip()
            if isinstance(data, Mapping)
            else ""
        )
        if not chat_id:
            return
        identifiers = (
            (message_ids,) if isinstance(message_ids, str) else tuple(message_ids)
        )
        scope = (self.target_open_id, chat_id)
        with self._recipient_scope_lock:
            for message_id in identifiers:
                normalized = str(message_id or "").strip()
                if not normalized:
                    continue
                self._recipient_scopes[normalized] = scope
                self._recipient_scopes.move_to_end(normalized)
            while len(self._recipient_scopes) > 4096:
                self._recipient_scopes.popitem(last=False)

    def recipient_scope_for_messages(
        self, message_ids: tuple[str, ...]
    ) -> tuple[str, str] | None:
        """返回一组刚发送文本分片的相同 ``(owner, chat_id)`` 作用域。"""

        identifiers = tuple(
            dict.fromkeys(str(item or "").strip() for item in message_ids)
        )
        if not identifiers or any(not item for item in identifiers):
            return None
        with self._recipient_scope_lock:
            scopes = tuple(self._recipient_scopes.get(item) for item in identifiers)
        if any(scope is None for scope in scopes):
            return None
        first = scopes[0]
        if first is None or any(scope != first for scope in scopes[1:]):
            return None
        return first

    def send_text(self, text: str, *, idempotency_key: str) -> str | tuple[str, ...] | None:
        payload = str(text or "")
        if not payload:
            raise ValueError("飞书消息正文不能为空")
        if not idempotency_key:
            raise ValueError("飞书发送必须提供稳定幂等键")
        with self._lifecycle_lock:
            channel = self._channel
            loop = self._loop
        if channel is None or loop is None or not self.is_online():
            raise FeishuSendNotSubmittedError("飞书当前不在线")
        stable_uuid = uuid.uuid5(_IDEMPOTENCY_NAMESPACE, idempotency_key).hex

        async def send() -> Any:
            return await channel.send(
                self.target_open_id,
                _rich_post_or_text(payload),
                {"receive_id_type": "open_id", "uuid": stable_uuid},
            )

        future = asyncio.run_coroutine_threadsafe(send(), loop)
        try:
            result = future.result(timeout=max(30.0, self.connect_timeout_seconds))
        except BaseException as exc:
            future.cancel()
            raise FeishuSendError("飞书发送失败或结果未知") from exc
        message_ids = self._message_ids_from_send_result(result)
        self._remember_send_result_scope(result, message_ids)
        return message_ids

    def fetch_message(self, message_id: str) -> Mapping[str, Any]:
        """按精确平台 ``message_id`` 读取一条飞书消息。

        这个入口只包装官方 SDK 已提供的 ``im.v1.message.get``，不会按时间、
        正文或最近消息猜测目标。SDK 的异步对象运行在本类专属事件循环；调用
        方（尤其是 WebSocket 入站回调）必须通过本同步包装器，不能在回调循环
        里直接等待 SDK，避免死锁。
        """

        identifier = str(message_id or "").strip()
        if not identifier or len(identifier) > 512:
            raise ValueError("飞书父消息 message_id 无效")
        with self._lifecycle_lock:
            channel = self._channel
            loop = self._loop
        if channel is None or loop is None or not self.is_online():
            raise MessageChannelOfflineError("飞书当前不在线，无法核验被引用消息")

        async def fetch() -> Any:
            return await channel.fetch_message(identifier)

        future = asyncio.run_coroutine_threadsafe(fetch(), loop)
        try:
            result = future.result(timeout=max(30.0, self.connect_timeout_seconds))
        except BaseException as exc:
            future.cancel()
            raise MessageChannelOfflineError("飞书父消息查询失败") from exc
        if not isinstance(result, Mapping):
            raise MessageChannelError("飞书父消息查询返回了无效结构")
        return result

    def bot_sender_ids(self) -> tuple[str, ...]:
        """返回官方 SDK 已解析的本机 bot 身份标识。

        读取只使用已连接 SDK 对象的内存身份，不发起额外网络请求。配置中的
        ``app_id`` 是官方 ``sender.id_type=app_id`` 的权威身份，即使 SDK
        尚未填充 ``bot_identity`` 也必须保留；上层仍会同时要求 app/bot
        sender_type，不会仅凭一个字符串放行伪造父消息。
        """

        with self._lifecycle_lock:
            channel = self._channel
        values: list[object] = [self.app_id]
        if channel is not None:
            identity = getattr(channel, "bot_identity", None)
            if identity is not None:
                values.extend(
                    (
                        getattr(identity, "open_id", None),
                        getattr(identity, "user_id", None),
                        getattr(identity, "app_id", None),
                    )
                )
        return tuple(
            dict.fromkeys(
                str(value).strip()
                for value in values
                if str(value or "").strip()
            )
        )

    def send_card(
        self,
        card: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> str | tuple[str, ...] | None:
        payload = dict(card or {})
        is_cardkit_v2 = (
            payload.get("schema") == "2.0"
            and isinstance(payload.get("body"), dict)
            and isinstance(payload["body"].get("elements"), list)
            and "elements" not in payload
        )
        classic_elements = payload.get("elements")
        classic_forms = (
            [item for item in classic_elements if isinstance(item, dict) and item.get("tag") == "form"]
            if isinstance(classic_elements, list)
            else []
        )
        is_classic_form = (
            "schema" not in payload
            and "body" not in payload
            and isinstance(payload.get("header"), dict)
            and isinstance(classic_elements, list)
            and len(classic_forms) == 1
            and any(
                isinstance(item, dict)
                and item.get("tag") == "button"
                and item.get("action_type") == "form_submit"
                and "form_action_type" not in item
                for item in classic_forms[0].get("elements", [])
            )
        )
        if not (is_cardkit_v2 or is_classic_form):
            raise ValueError(
                "飞书卡片必须是完整 CardKit 2.0 对象或单表单经典交互卡"
            )
        if not idempotency_key:
            raise ValueError("飞书发送必须提供稳定幂等键")
        with self._lifecycle_lock:
            channel = self._channel
            loop = self._loop
        if channel is None or loop is None or not self.is_online():
            raise FeishuSendNotSubmittedError("飞书当前不在线")
        stable_uuid = uuid.uuid5(_IDEMPOTENCY_NAMESPACE, idempotency_key).hex

        async def send() -> Any:
            return await channel.send(
                self.target_open_id,
                {"card": payload},
                {"receive_id_type": "open_id", "uuid": stable_uuid},
            )

        future = asyncio.run_coroutine_threadsafe(send(), loop)
        try:
            result = future.result(timeout=max(30.0, self.connect_timeout_seconds))
        except BaseException as exc:
            future.cancel()
            raise FeishuSendError("飞书卡片发送失败或结果未知") from exc
        return self._message_ids_from_send_result(result)

    def _send_media(
        self,
        payload: bytes,
        *,
        kind: str,
        file_name: str,
        idempotency_key: str,
        max_bytes: int,
    ) -> str | tuple[str, ...] | None:
        """Upload once, persist the key, then create the media message.

        The public SDK exposes ``upload_media`` separately from ``send``.  A
        successful upload is recorded before the message request starts so a
        subsequent retry can send the same key.  This is the boundary that
        keeps an unknown message result from uploading the payload again.
        """

        if not idempotency_key:
            raise ValueError("飞书发送必须提供稳定幂等键")
        if not payload:
            raise FeishuSendRejectedError(
                code="upload_failed",
                raw_code=234010,
                retryable=False,
                context={"kind": kind, "size": 0, "limit": max_bytes},
                raw_msg="File's size can't be 0",
            )
        if len(payload) > max_bytes:
            raise FeishuSendRejectedError(
                code="upload_failed",
                raw_code=234006,
                retryable=False,
                context={"kind": kind, "size": len(payload), "limit": max_bytes},
                raw_msg="The file size exceed the max value",
            )
        digest = hashlib.sha256(payload).hexdigest()
        cache_token = (kind, idempotency_key)

        with self._lifecycle_lock:
            channel = self._channel
            loop = self._loop
        if channel is None or loop is None or not self.is_online():
            raise FeishuSendNotSubmittedError("飞书当前不在线")
        stable_uuid = uuid.uuid5(_IDEMPOTENCY_NAMESPACE, idempotency_key).hex

        # Serialise the upload/cache/create sequence per adapter.  The SDK
        # still performs the network work on its own loop; this lock only
        # prevents two same-key callers from racing the cache boundary.
        with self._media_upload_lock:
            record = self._media_uploads.get(cache_token)
            if record is not None:
                old_digest, old_name, media_key, persisted = record
                if old_digest != digest or old_name != file_name:
                    raise FeishuSendRejectedError(
                        code="format_error",
                        raw_code=230001,
                        retryable=False,
                        context={"kind": kind, "reason": "idempotency_payload_conflict"},
                        raw_msg="idempotency key was reused with different media",
                    )
                self._media_uploads.move_to_end(cache_token)
            else:
                media_key = None
                loaded_from_lookup = False
                lookup = self._media_key_lookup
                if lookup is not None:
                    try:
                        media_key = lookup(
                            kind,
                            idempotency_key,
                            digest,
                            file_name,
                        )
                    except BaseException as exc:
                        raise FeishuSendNotSubmittedError(
                            "媒体键持久化查询暂不可用"
                        ) from exc
                    if media_key is not None:
                        media_key = str(media_key).strip()
                        if not media_key:
                            raise FeishuSendError(
                                "媒体键持久化查询返回了无效键",
                                uploaded=True,
                                idempotency_key=idempotency_key,
                            )
                        loaded_from_lookup = True
                if not media_key:
                    upload_media = getattr(channel, "upload_media", None)
                    if not callable(upload_media):
                        # Keep compatibility with older test doubles and SDK
                        # wrappers.  The official SDK path below always uses
                        # upload_media, which is required for key reuse.
                        async def legacy_send() -> Any:
                            message = (
                                {"image": {"source": payload}}
                                if kind == "image"
                                else {"file": {"source": payload, "file_name": file_name}}
                            )
                            return await channel.send(
                                self.target_open_id,
                                message,
                                {"receive_id_type": "open_id", "uuid": stable_uuid},
                            )

                        future = asyncio.run_coroutine_threadsafe(legacy_send(), loop)
                        try:
                            result = future.result(
                                timeout=max(60.0, self.connect_timeout_seconds)
                            )
                        except BaseException as exc:
                            future.cancel()
                            self._raise_media_exception(
                                exc,
                                uploaded=False,
                                media_key=None,
                                idempotency_key=idempotency_key,
                            )
                        return self._message_ids_from_send_result(
                            result,
                            idempotency_key=idempotency_key,
                        )

                if not media_key:
                    try:
                        from lark_channel.channel.types import MediaSource
                    except (ImportError, AttributeError) as exc:
                        raise FeishuDependencyError(
                            "缺少兼容的 lark-channel-sdk 媒体类型"
                        ) from exc

                    async def upload() -> Any:
                        return await upload_media(
                            MediaSource(kind="buffer", buffer=payload),
                            kind=kind,
                            file_name=file_name,
                        )

                    future = asyncio.run_coroutine_threadsafe(upload(), loop)
                    try:
                        media_key = str(
                            future.result(
                                timeout=max(90.0, self.connect_timeout_seconds)
                            )
                            or ""
                        ).strip()
                    except BaseException as exc:
                        future.cancel()
                        self._raise_media_exception(
                            exc,
                            uploaded=False,
                            media_key=None,
                            idempotency_key=idempotency_key,
                        )
                    if not media_key:
                        raise FeishuSendRejectedError(
                            code="upload_failed",
                            raw_code=None,
                            retryable=False,
                            context={"kind": kind, "reason": "missing_media_key"},
                            raw_msg="upload returned no media key",
                        )

                # Keep this in memory before the durable callback: if the
                # callback is temporarily unavailable, the next retry can
                # retry the callback without uploading a second copy.
                persisted = loaded_from_lookup or self._media_key_store is None
                self._media_uploads[cache_token] = (
                    digest,
                    file_name,
                    media_key,
                    persisted,
                )
                if self._media_key_store is not None:
                    try:
                        self._media_key_store(
                            kind,
                            idempotency_key,
                            digest,
                            file_name,
                            media_key,
                        )
                    except BaseException as exc:
                        raise FeishuSendError(
                            "飞书媒体已上传但媒体键尚未持久化",
                            uploaded=True,
                            media_key=media_key,
                            idempotency_key=idempotency_key,
                        ) from exc
                    self._media_uploads[cache_token] = (
                        digest,
                        file_name,
                        media_key,
                        True,
                    )
                    persisted = True
                while len(self._media_uploads) > 1024:
                    self._media_uploads.popitem(last=False)

            if not persisted and self._media_key_store is not None:
                try:
                    self._media_key_store(
                        kind,
                        idempotency_key,
                        digest,
                        file_name,
                        media_key,
                    )
                except BaseException as exc:
                    raise FeishuSendError(
                        "飞书媒体已上传但媒体键尚未持久化",
                        uploaded=True,
                        media_key=media_key,
                        idempotency_key=idempotency_key,
                    ) from exc
                self._media_uploads[cache_token] = (
                    digest,
                    file_name,
                    media_key,
                    True,
                )

            try:
                from lark_channel.channel.types import MediaSource
            except (ImportError, AttributeError) as exc:
                raise FeishuDependencyError(
                    "缺少兼容的 lark-channel-sdk 媒体类型"
                ) from exc
            key_source = MediaSource(kind="key", key=media_key)

            async def send() -> Any:
                message = (
                    {"image": {"source": key_source}}
                    if kind == "image"
                    else {"file": {"source": key_source, "file_name": file_name}}
                )
                return await channel.send(
                    self.target_open_id,
                    message,
                    {"receive_id_type": "open_id", "uuid": stable_uuid},
                )

            future = asyncio.run_coroutine_threadsafe(send(), loop)
            try:
                result = future.result(timeout=max(60.0, self.connect_timeout_seconds))
            except BaseException as exc:
                future.cancel()
                self._raise_media_exception(
                    exc,
                    uploaded=True,
                    media_key=media_key,
                    idempotency_key=idempotency_key,
                )
            return self._message_ids_from_send_result(
                result,
                uploaded=True,
                media_key=media_key,
                idempotency_key=idempotency_key,
            )

    def send_file(
        self,
        data: bytes,
        *,
        file_name: str,
        idempotency_key: str,
    ) -> str | tuple[str, ...] | None:
        payload = bytes(data)
        name = Path(str(file_name or "")).name.strip()
        if not name or name in {".", ".."}:
            raise FeishuSendRejectedError(
                code="format_error",
                raw_code=230001,
                retryable=False,
                context={"kind": "file", "reason": "invalid_file_name"},
                raw_msg="file_name is invalid",
            )
        return self._send_media(
            payload,
            kind="file",
            file_name=name,
            idempotency_key=idempotency_key,
            max_bytes=FEISHU_FILE_MAX_BYTES,
        )

    def send_image(
        self,
        data: bytes,
        *,
        idempotency_key: str,
    ) -> str | tuple[str, ...] | None:
        """上传并发送飞书 ``image`` 消息，使客户端直接展示图片。"""

        return self._send_media(
            bytes(data),
            kind="image",
            file_name="image",
            idempotency_key=idempotency_key,
            max_bytes=FEISHU_IMAGE_MAX_BYTES,
        )

    def is_online(self) -> bool:
        with self._lifecycle_lock:
            thread = self._thread
            channel = self._channel
        return bool(
            self._online_event.is_set()
            and thread is not None
            and thread.is_alive()
            and channel is not None
            and self._connected(channel)
        )

    def stop(self) -> None:
        self._stop_event.set()
        self._online_event.clear()
        self._set_health("stopping", retry_deadline=0.0)
        with self._lifecycle_lock:
            loop = self._loop
            channel = self._channel
            thread = self._thread
        # 已经进入正常运行阶段时，worker 会在 finally 中自行 disconnect；
        # 只有启动尚未发出 start_event 时才由 stop 代为唤醒 SDK。
        needs_unblock = (
            thread is not None
            and thread.is_alive()
            and not self._start_event.is_set()
        )
        if needs_unblock and loop is not None and channel is not None and loop.is_running():
            try:
                future = asyncio.run_coroutine_threadsafe(self._disconnect(channel), loop)
                future.result(timeout=5)
            except Exception:
                pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=10)
        with self._lifecycle_lock:
            self._channel = None
            self._thread = None
            self._loop = None
        self._set_health("stopped", retry_deadline=0.0)
