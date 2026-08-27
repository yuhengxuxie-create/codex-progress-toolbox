"""飞书企业自建应用机器人消息渠道。

本模块使用飞书官方 ``lark-channel-sdk`` 的 WebSocket 长连接，不依赖
电脑端飞书窗口，也不启动本地 HTTP 服务。所有入站消息在交给主服务前，
都会再次执行单聊、发送者白名单和结构化引用三项校验。
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from functools import lru_cache
import logging
from pathlib import Path
import threading
import time
from typing import Any, Callable
import uuid

from .channel import (
    ChannelAttachment,
    ChannelErrorHandler,
    ChannelReply,
    MessageChannelError,
    MessageChannelOfflineError,
    ReplyHandler,
)


LOGGER = logging.getLogger(__name__)
_IDEMPOTENCY_NAMESPACE = uuid.UUID("be46c6a9-5027-4d70-aa62-873b94c06e78")
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
    "归属：",
    "状态：",
    "最近更新：",
    "整体概览：",
    "最后一轮结果：",
    "操作说明：",
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


class FeishuSendRejectedError(FeishuSendError):
    """飞书返回了可分类的明确拒绝；字段仅包含脱敏错误元数据。"""

    def __init__(
        self,
        *,
        code: str,
        raw_code: int | None,
        retryable: bool,
    ) -> None:
        super().__init__(f"飞书明确拒绝发送消息（分类={code}，错误码={raw_code}）")
        self.code = str(code or "unknown")
        self.raw_code = raw_code
        self.retryable = bool(retryable)


SdkFactory = Callable[[str, str, str, float], Any]


def _ensure_sdk_import_loop_idle(sdk_loop: Any) -> None:
    """拒绝复用在 SDK 导入时已经运行的模块级事件循环。"""

    if sdk_loop is not None and sdk_loop.is_running():
        raise FeishuDependencyError(
            "飞书 SDK 被其他模块在运行中的事件循环内提前加载；请重启进度通知后重试"
        )


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
            FeishuChannel,
            InboundConfig,
            LogLevel,
            MediaCapabilities,
            MediaCacheConfig,
            OutboundConfig,
            PolicyConfig,
            RetryConfig,
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
    _ensure_sdk_import_loop_idle(getattr(ws_client_module, "loop", None))
    return (
        ChannelConfig,
        FeishuChannel,
        InboundConfig,
        LogLevel,
        MediaCapabilities,
        MediaCacheConfig,
        OutboundConfig,
        PolicyConfig,
        RetryConfig,
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
        FeishuChannel,
        InboundConfig,
        LogLevel,
        MediaCapabilities,
        MediaCacheConfig,
        OutboundConfig,
        PolicyConfig,
        RetryConfig,
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
    return FeishuChannel(
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
        sdk_factory: SdkFactory | None = None,
        media_cache_dir: str | Path | None = None,
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
        self._on_reply: ReplyHandler | None = None
        self._stop_event = threading.Event()
        self._online_event = threading.Event()
        self._start_event = threading.Event()
        self._start_error: BaseException | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._channel: Any = None
        self._lifecycle_lock = threading.RLock()
        # 官方 SDK 1.2.0 的 stop()/disconnect() 不是并发安全的；外部 stop
        # 与 worker 的 finally 若同时调用，会让 DeviceFlowClient.close() 留下
        # 未 await 协程。按 SDK channel 对象去重，确保每个实例只清理一次。
        self._seen_lock = threading.Lock()
        self._seen_message_ids: OrderedDict[str, None] = OrderedDict()

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
        try:
            callback(
                ChannelReply(
                    sender_id=sender_id,
                    content=content,
                    reply_to_message_id=reply_to,
                    message_id=message_id,
                    chat_id=str(getattr(message, "chat_id", "") or ""),
                    attachments=tuple(attachments),
                    attachment_error=attachment_error,
                )
            )
        except BaseException as exc:
            self._error_handler(exc)

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
        runtime_failures = 0
        while not self._stop_event.is_set():
            # 首次成功连接后的掉线只是触发重连，不占用重试名额；随后每个
            # 未能稳定恢复的连接尝试才计一次，确保“最多重试 N 次”没有少一次。
            is_reconnect_attempt = ever_connected
            channel: Any | None = None
            connected_at = 0.0
            try:
                # 创建 SDK、注册回调、握手和在线监督必须处于同一个有限重试
                # 边界内；运行时升级/依赖异常不能绕过五次熔断直接杀死线程。
                channel = self._sdk_factory(
                    self.app_id,
                    self._app_secret,
                    self.target_open_id,
                    self.connect_timeout_seconds,
                )
                channel.on("message", self._handle_message)
                with self._lifecycle_lock:
                    self._channel = channel
                await channel.connect_until_ready(timeout=self.connect_timeout_seconds)
                if not self._connected(channel):
                    raise MessageChannelOfflineError("飞书握手完成后未进入可收发状态")
                connected_at = time.monotonic()
                ever_connected = True
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
                    return
                if not ever_connected:
                    self._start_error = exc
                    self._start_event.set()
                    return
                stable_connection = bool(
                    connected_at and time.monotonic() - connected_at >= 60
                )
                if stable_connection:
                    runtime_failures = 0
                elif is_reconnect_attempt:
                    runtime_failures += 1
                if runtime_failures >= self.max_attempts:
                    self._error_handler(
                        MessageChannelOfflineError(
                            f"飞书连续 {self.max_attempts} 次重连仍失败，消息渠道已停止"
                        )
                    )
                    return
                await self._wait_or_stop(self.retry_delays[runtime_failures])
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
        if not self.is_online():
            raise MessageChannelOfflineError("飞书连接未保持在线")

    @staticmethod
    def _message_ids_from_send_result(result: Any) -> str | tuple[str, ...]:
        if not bool(getattr(result, "success", False)):
            error = getattr(result, "error", None)
            error_code = getattr(error, "code", "unknown")
            code = str(getattr(error_code, "value", error_code) or "unknown")
            raw_code = getattr(error, "raw_code", None)
            raise FeishuSendRejectedError(
                code=code,
                raw_code=raw_code if isinstance(raw_code, int) else None,
                retryable=bool(getattr(error, "retryable", False)),
            )
        message_id = str(getattr(result, "message_id", "") or "")
        if not message_id:
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
                raise FeishuSendError("飞书分片发送返回了无效 message_id 列表")
            return chunk_ids
        return message_id

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
            raise MessageChannelOfflineError("飞书当前不在线")
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
        return self._message_ids_from_send_result(result)

    def send_file(
        self,
        data: bytes,
        *,
        file_name: str,
        idempotency_key: str,
    ) -> str | tuple[str, ...] | None:
        payload = bytes(data)
        name = Path(str(file_name or "")).name.strip()
        if not payload:
            raise ValueError("飞书文件内容不能为空")
        if not name or name in {".", ".."}:
            raise ValueError("飞书文件名无效")
        if not idempotency_key:
            raise ValueError("飞书发送必须提供稳定幂等键")
        with self._lifecycle_lock:
            channel = self._channel
            loop = self._loop
        if channel is None or loop is None or not self.is_online():
            raise MessageChannelOfflineError("飞书当前不在线")
        stable_uuid = uuid.uuid5(_IDEMPOTENCY_NAMESPACE, idempotency_key).hex

        async def send() -> Any:
            return await channel.send(
                self.target_open_id,
                {"file": {"source": payload, "file_name": name}},
                {"receive_id_type": "open_id", "uuid": stable_uuid},
            )

        future = asyncio.run_coroutine_threadsafe(send(), loop)
        try:
            result = future.result(timeout=max(60.0, self.connect_timeout_seconds))
        except BaseException as exc:
            future.cancel()
            raise FeishuSendError("飞书原文件发送失败或结果未知") from exc
        return self._message_ids_from_send_result(result)

    def send_image(
        self,
        data: bytes,
        *,
        idempotency_key: str,
    ) -> str | tuple[str, ...] | None:
        """上传并发送飞书 ``image`` 消息，使客户端直接展示图片。"""

        payload = bytes(data)
        if not payload:
            raise ValueError("飞书图片内容不能为空")
        if not idempotency_key:
            raise ValueError("飞书发送必须提供稳定幂等键")
        with self._lifecycle_lock:
            channel = self._channel
            loop = self._loop
        if channel is None or loop is None or not self.is_online():
            raise MessageChannelOfflineError("飞书当前不在线")
        stable_uuid = uuid.uuid5(_IDEMPOTENCY_NAMESPACE, idempotency_key).hex

        async def send() -> Any:
            # lark-channel-sdk 会先调用 /im/v1/images 上传，再以
            # msg_type=image + image_key 发送；与 file 消息不同，客户端可
            # 在聊天流中直接预览。
            return await channel.send(
                self.target_open_id,
                {"image": {"source": payload}},
                {"receive_id_type": "open_id", "uuid": stable_uuid},
            )

        future = asyncio.run_coroutine_threadsafe(send(), loop)
        try:
            result = future.result(timeout=max(60.0, self.connect_timeout_seconds))
        except BaseException as exc:
            future.cancel()
            raise FeishuSendError("飞书图片发送失败或结果未知") from exc
        return self._message_ids_from_send_result(result)

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
