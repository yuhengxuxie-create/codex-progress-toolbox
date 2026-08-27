"""长期运行的监控编排：Codex 事件 → 消息通知 → 引用回复 → Codex。"""

from __future__ import annotations

import ctypes
import hashlib
import inspect
import json
import logging
import os
import queue
import re
import signal
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .codex_rpc import (
    CodexAppServer,
    CodexRPCClosed,
    CodexRPCError,
    CodexRPCUnhandledRequest,
    CodexRPCTimeout,
    ServerRequest,
    TurnCompletedEvent,
)
from .codex_account import CodexAccountError, CodexAccountReader, format_rate_limits
from .approval_bridge import (
    ApprovalBridge,
    ApprovalBridgeError,
    ApprovalRequest,
    persist_execpolicy_rule,
)
from .codex_app_tools import (
    DesktopAppToolsClient,
    DesktopAppToolsError,
    DesktopAppToolsResultUnknown,
    DesktopAppToolsUnavailable,
    VerifiedDesktopAppTools,
)
from .codex_management import CodexManagementController, ManagementUserError
from .codex_projects import CodexProjectRegistry, ProjectRegistryError
from .codex_gateway import active_shared_websocket_url
from .codex_store import (
    CodexStore,
    CodexStoreReadError,
    read_generated_image_bytes,
    StorePaths,
    ThreadRecord,
    ThreadSnapshot,
    ThreadStatus,
)
from .channel import (
    ChannelAttachment,
    ChannelReply,
    MessageChannel,
    WechatMessageChannel,
    codex_prompt_for_reply,
)
from .config import AppConfig, ReloadingConfig
from .feishu import FeishuMessageChannel, FeishuSendRejectedError
from .formatting import format_notification, format_reply_receipt
from .models import GeneratedImageArtifact, TERMINAL_TURN_STATUSES, TurnEvent, structural_report
from .retry import RetryPolicy, call_with_retry
from .secrets import DpapiSecretStore
from .state import CorrelationCodec, StateStore
from .summarizer import ProgressSummarizer
from .wechat import QuoteMessage, WechatService, WxAutoX4Adapter


LOGGER = logging.getLogger("progress_wx.service")
AUTO_MONITOR_TTL_SECONDS = 24 * 60 * 60
IMAGE_REPLY_STAGE_TTL_SECONDS = 10 * 60
APPROVAL_BRIDGE_POLL_SECONDS = 0.5


_APPROVAL_SECRET_PATTERN = re.compile(
    r"(?i)(api[_-]?key|token|secret|authorization|password)(\s*[:=]\s*)([^\s,;]+)"
)


class ServiceFatalError(RuntimeError):
    """必须告警并停止整个服务的不可恢复错误。"""


class DesktopTurnBusyError(ServiceFatalError):
    """目标轮次由另一个桌面 app-server 持有，当前进程不能安全接管。"""


class ReplyDeferred(RuntimeError):
    """正文尚未进入非幂等临界区，可等待目标轮次空闲后安全重试。"""


class ServiceStopping(RuntimeError):
    """用户主动停止服务时，用于退出正在等待远程输入的工作线程。"""


def _file_identity(path: Path) -> tuple[Path, int, int, int, int]:
    """取得敏感文件的稳定身份；无法核验时失败关闭。"""

    resolved = path.expanduser().resolve()
    try:
        stat = resolved.stat()
    except OSError as exc:
        raise ServiceFatalError("飞书 App Secret 文件无法核验；请停止后重新配置") from exc
    if not resolved.is_file():
        raise ServiceFatalError("飞书 App Secret 路径不是普通文件")
    return (resolved, stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _codex_connection_identity(config: AppConfig) -> tuple[object, ...]:
    """只把当前回复传输真正会使用的字段纳入热重载身份。"""

    identity: tuple[object, ...] = (
        config.codex.home,
        config.codex.command,
        config.codex.reply_transport,
    )
    if config.codex.reply_transport == "shared_websocket":
        identity += (
            config.codex.shared_websocket_url,
            config.codex.gateway_pid_file,
            config.codex.shared_desktop_state_file,
        )
    elif config.codex.reply_transport == "desktop_app_tools":
        identity += (
            config.codex.desktop_log_dir,
            config.codex.managed_project_root,
        )
    return identity


@dataclass(frozen=True, slots=True)
class ReplyJob:
    code: str
    thread_id: str
    reply_text: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class ReplyReceiptJob:
    """已通过白名单与引用关联检查的飞书回复回执。"""

    received: bool
    details: str
    idempotency_key: str


@dataclass(slots=True)
class PendingServerReply:
    """仅在拥有该 app-server 连接的进程内有效，重启后绝不伪造恢复。"""

    request: ServerRequest
    responses: queue.Queue[dict[str, Any]]


def _approval_decision(text: str, *, allow_similar: bool) -> str:
    normalized = "".join(str(text or "").strip().casefold().split())
    if normalized in {"a", "allow", "允许", "允许一次"}:
        return "allow"
    if normalized in {"d", "deny", "decline", "拒绝"}:
        return "deny"
    if normalized in {"s", "similar", "允许类似操作", "允许同类操作"}:
        if not allow_similar:
            raise ValueError("Codex 本次没有提供可安全复用的明确规则，只能选择允许一次或拒绝")
        return "allow_similar"
    choices = "A（允许一次）、S（允许类似操作）或 D（拒绝）" if allow_similar else "A（允许一次）或 D（拒绝）"
    raise ValueError(f"请只回复 {choices}")


def _approval_operation(request: ApprovalRequest) -> str:
    tool_input = request.tool_input
    candidates = (
        tool_input.get("cmd"),
        tool_input.get("command"),
        tool_input.get("path"),
        tool_input.get("file_path"),
        tool_input.get("justification"),
    )
    value = next((str(item).strip() for item in candidates if str(item or "").strip()), "")
    if not value:
        value = request.tool_name or "Codex 请求执行受保护操作"
    value = " ".join(value.split())
    value = _APPROVAL_SECRET_PATTERN.sub(r"\1\2<已隐藏>", value)
    return value if len(value) <= 800 else value[:799].rstrip() + "…"


def _approval_message(request: ApprovalRequest, title: str) -> str:
    lines = [
        f"对话名称：{title or request.session_id or 'Codex 会话'}",
        "当前进度：待审批",
        f"操作类型：{request.tool_name or '受保护操作'}",
    ]
    if request.cwd:
        lines.append(f"目录：{request.cwd}")
    lines.extend(
        [
            f"具体操作：{_approval_operation(request)}",
            "",
            "操作说明：",
            "- 回复 A 或“允许一次”：只批准本次操作",
        ]
    )
    if request.reusable_prefix:
        prefix = " ".join(request.reusable_prefix)
        prefix = _APPROVAL_SECRET_PATTERN.sub(r"\1\2<已隐藏>", prefix)
        if len(prefix) > 500:
            prefix = prefix[:499].rstrip() + "…"
        lines.extend(
            [
                "- 回复 S 或“允许类似操作”：保存 Codex 明确提出的规则，以后同类命令可直接执行",
                f"- 可复用范围：{prefix}",
            ]
        )
    else:
        lines.append("- 本次没有可安全复用的明确规则，因此不提供“允许类似操作”")
    lines.append("- 回复 D 或“拒绝”：拒绝本次操作")
    return "\n".join(lines)


_APPROVAL_METHODS = frozenset(
    {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
    }
)
_USER_INPUT_METHOD = "item/tool/requestUserInput"
_APPROVAL_CODES = {
    "a": "accept",
    "accept": "accept",
    "s": "acceptForSession",
    "acceptforsession": "acceptForSession",
    "d": "decline",
    "decline": "decline",
    "c": "cancel",
    "cancel": "cancel",
}


def _questions(request: ServerRequest) -> list[dict[str, Any]]:
    raw_questions = request.params.get("questions")
    if not isinstance(raw_questions, list) or not raw_questions:
        raise ServiceFatalError("Codex requestUserInput 缺少结构化 questions")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_questions:
        if not isinstance(raw, dict):
            raise ServiceFatalError("Codex requestUserInput question 格式错误")
        question_id = str(raw.get("id") or "").strip()
        question_text = str(raw.get("question") or "").strip()
        if not question_id or not question_text or question_id in seen:
            raise ServiceFatalError("Codex requestUserInput question 缺少唯一 id 或正文")
        if raw.get("isSecret") is True:
            raise ServiceFatalError("Codex 请求秘密输入；禁止通过消息渠道明文转发")
        seen.add(question_id)
        normalized = dict(raw)
        normalized["id"] = question_id
        normalized["question"] = question_text
        result.append(normalized)
    return result


def server_request_response(request: ServerRequest, reply_text: str) -> dict[str, Any]:
    """把明确的控制码/结构化答案转成官方响应；不做自然语言意图猜测。"""

    content = str(reply_text or "").strip()
    if not content:
        raise ValueError("回复不能为空")
    if request.method in _APPROVAL_METHODS:
        decision = _APPROVAL_CODES.get(content.casefold())
        if decision is None:
            raise ValueError("审批回复只接受 A、S、D、C 或对应完整协议值")
        return {"decision": decision}
    if request.method != _USER_INPUT_METHOD:
        raise ServiceFatalError(f"不支持的 Codex 服务端请求：{request.method}")

    questions = _questions(request)
    if len(questions) == 1:
        return {"answers": {questions[0]["id"]: {"answers": [content]}}}
    try:
        decoded = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError("多问题回复必须是 JSON 数组或以问题 id 为键的 JSON 对象") from exc
    answers: dict[str, dict[str, list[str]]] = {}
    if isinstance(decoded, list):
        if len(decoded) != len(questions):
            raise ValueError("JSON 数组答案数量与问题数量不一致")
        pairs = zip((item["id"] for item in questions), decoded)
    elif isinstance(decoded, dict):
        expected = {str(item["id"]) for item in questions}
        if set(decoded) != expected:
            raise ValueError("JSON 对象必须恰好包含全部问题 id")
        pairs = ((item["id"], decoded[item["id"]]) for item in questions)
    else:
        raise ValueError("多问题回复必须是 JSON 数组或对象")
    for question_id, value in pairs:
        values = value if isinstance(value, list) else [value]
        if not values or any(not isinstance(item, str) or not item.strip() for item in values):
            raise ValueError("每个问题的答案必须是非空字符串或字符串数组")
        answers[str(question_id)] = {"answers": [item.strip() for item in values]}
    return {"answers": answers}


def server_request_event(request: ServerRequest, thread: ThreadRecord | None) -> TurnEvent:
    """仅依据 JSON-RPC method/params 构造等待状态，不分析正文关键词。"""

    if not request.thread_id or not request.turn_id:
        raise ServiceFatalError("Codex 服务端请求缺少 threadId 或 turnId")
    if request.method in _APPROVAL_METHODS:
        status = "waitingOnApproval"
        reason = str(request.params.get("reason") or "").strip()
        detail = "回复 A=允许、S=本会话允许、D=拒绝、C=中止"
        if reason:
            detail += f"；{reason}"
    elif request.method == _USER_INPUT_METHOD:
        status = "waitingOnUserInput"
        questions = _questions(request)
        first_question = str(questions[0]["question"])
        if len(questions) == 1:
            detail = f"请直接引用回复答案：{first_question}"
        else:
            detail = f"共 {len(questions)} 问；请按顺序回复 JSON 数组。第 1 问：{first_question}"
    else:
        raise ServiceFatalError(f"不支持的 Codex 服务端请求：{request.method}")
    identity = f"{request.method}|{request.request_id}"
    token = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    actual_turn_id = request.turn_id or "unknown-turn"
    return TurnEvent(
        thread_id=request.thread_id or (thread.thread_id if thread else "unknown-thread"),
        turn_id=f"{actual_turn_id}-rpc-{token}",
        status=status,
        title=(thread.title if thread else ""),
        cwd=(thread.cwd if thread else ""),
        final_message=detail,
        source="codex-app-server-request",
        raw=request.raw,
    )


def desktop_attention_event(
    poll: dict[str, Any],
    thread: ThreadRecord | None,
) -> TurnEvent | None:
    """把 Desktop ``wait_threads`` 的结构化待输入状态投影成通知事件。

    正文只用于展示最新说明；是否需要通知完全由 ``activeFlags`` 决定，不做
    “请回复”“选择”等关键词猜测。cursor 被纳入合成事件 ID，使同一长轮次中
    先后出现的多个独立提问都能各通知一次。
    """

    raw_thread = poll.get("thread")
    if not isinstance(raw_thread, dict):
        return None
    thread_id = str(raw_thread.get("id") or "").strip()
    raw_status = raw_thread.get("status")
    if not thread_id or not isinstance(raw_status, dict) or raw_status.get("type") != "active":
        return None
    raw_flags = raw_status.get("activeFlags")
    if not isinstance(raw_flags, list):
        return None
    normalized_flags = {
        "".join(char for char in str(flag).casefold() if char.isalnum())
        for flag in raw_flags
    }
    if "waitingonuserinput" not in normalized_flags:
        return None
    latest_turn = poll.get("latestTurn")
    if not isinstance(latest_turn, dict):
        return None
    actual_turn_id = str(latest_turn.get("id") or "").strip()
    cursor = str(poll.get("cursor") or "").strip()
    if not actual_turn_id or not cursor:
        return None
    latest_message = poll.get("latestAssistantMessage")
    message_text = (
        str(latest_message.get("text") or "").strip()
        if isinstance(latest_message, dict)
        else ""
    )
    details = "Codex 正在等待你选择下一步；当前任务仍在运行，尚未暂停。"
    if message_text:
        details += f"\n最新说明：\n{message_text}"
    else:
        details += "\nDesktop 已进入人工输入状态，但本次状态快照没有附带问题正文。"
    details += "\n请直接引用回复本消息；你的正文会送回仍在运行的原任务。"
    revision = hashlib.sha256(cursor.encode("utf-8")).hexdigest()[:16]
    event_turn_id = f"{actual_turn_id}@attention-{revision}"
    if thread is not None and thread.thread_id != thread_id:
        raise ServiceFatalError("Desktop 待输入状态与本地任务元数据不一致")
    return TurnEvent(
        thread_id=thread_id,
        turn_id=event_turn_id,
        status="waitingOnUserInput",
        title=thread.title if thread else "",
        cwd=thread.cwd if thread else "",
        final_message=details,
        source="codex-desktop-wait-threads",
        raw={
            "poll": poll,
            "actual_turn_id": actual_turn_id,
            "cursor": cursor,
        },
    )


def desktop_attention_source(
    listing: dict[str, Any],
    monitored_thread_ids: set[str],
) -> str:
    """选择一个非监控 Codex 任务作为只读 ``wait_threads`` 调用上下文。"""

    candidates: list[tuple[int, int, str]] = []
    for section in ("pinnedThreads", "threads"):
        raw_items = listing.get(section)
        if not isinstance(raw_items, list):
            continue
        for item in raw_items:
            if not isinstance(item, dict) or item.get("kind") != "codex":
                continue
            thread_id = str(item.get("id") or "").strip()
            host_id = str(item.get("hostId") or "").strip()
            if not thread_id or not host_id or thread_id in monitored_thread_ids:
                continue
            # idle 最稳定，active 次之。当前 Desktop 也允许 notLoaded 的本地
            # Codex 历史任务作为只读 wait_threads 调用上下文，因此把它作为
            # 最后兜底；目标任务本身仍严格排除，避免来源与目标相同。
            status = item.get("status")
            if status not in {"idle", "active", "notLoaded"}:
                continue
            rank = {"idle": 0, "active": 1, "notLoaded": 2}[status]
            try:
                updated_at = int(item.get("updatedAt") or 0)
            except (TypeError, ValueError):
                updated_at = 0
            candidates.append((rank, -updated_at, thread_id))
    if not candidates:
        raise DesktopAppToolsUnavailable(
            "wait_threads 需要一个未被监控的本地 Codex 任务作为调用上下文"
        )
    candidates.sort()
    return candidates[0][2]


def desktop_loaded_monitors(
    listing: dict[str, Any],
    monitored_thread_ids: set[str],
) -> tuple[str, ...]:
    """只返回 Desktop 当前已加载、可安全交给 ``wait_threads`` 的监控任务。"""

    loaded: set[str] = set()
    for section in ("pinnedThreads", "threads"):
        raw_items = listing.get(section)
        if not isinstance(raw_items, list):
            continue
        for item in raw_items:
            if not isinstance(item, dict) or item.get("kind") != "codex":
                continue
            thread_id = str(item.get("id") or "").strip()
            if (
                thread_id in monitored_thread_ids
                and str(item.get("hostId") or "").strip()
                and item.get("status") in {"idle", "active"}
            ):
                loaded.add(thread_id)
    return tuple(sorted(loaded))


def started_turn_id(response: dict[str, Any]) -> str:
    """从官方 ``turn/start`` result 提取实际 turn id；缺失时禁止猜测。"""

    result = response.get("result")
    if not isinstance(result, dict):
        raise ServiceFatalError("turn/start 响应缺少 result")
    turn = result.get("turn")
    candidates = (
        turn.get("id") if isinstance(turn, dict) else None,
        result.get("turnId"),
        result.get("turn_id"),
    )
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    raise ServiceFatalError("turn/start 响应缺少实际 turn id；投递结果未知")


def steered_turn_id(response: dict[str, Any]) -> str:
    """从官方 ``turn/steer`` result 提取已接受的活动 turn id。"""

    result = response.get("result")
    if not isinstance(result, dict):
        raise ServiceFatalError("turn/steer 响应缺少 result；追加结果未知")
    for key in ("turnId", "turn_id"):
        candidate = result.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    raise ServiceFatalError("turn/steer 响应缺少实际 turn id；追加结果未知")


def _error_text(raw: str | None) -> str:
    """只抽取结构化 error_json 的 message 字段，不做状态关键词判断。"""

    if not raw:
        return ""
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return str(raw)[:1000]
    if isinstance(value, dict):
        message = value.get("message")
        if isinstance(message, str):
            return message[:1000]
    return json.dumps(value, ensure_ascii=False)[:1000]


def snapshot_to_event(snapshot: ThreadSnapshot) -> TurnEvent | None:
    turn = snapshot.latest_turn
    if turn is None or turn.status.value not in TERMINAL_TURN_STATUSES:
        return None
    # Codex 可能先把轮次投影为 completed，再稍后写入 final_agent_item_id 对应
    # 的正文。此时必须等待下一次轮询，不能把空输入交给摘要器并误报项目阻塞。
    if turn.status is ThreadStatus.COMPLETED and not turn.final_message.strip():
        return None
    thread_id = turn.thread_id or snapshot.thread_id
    source = (
        "codex-rollout"
        if turn.raw.get("source") == "codex-rollout"
        else "codex-sqlite"
    )
    return TurnEvent(
        thread_id=thread_id,
        turn_id=turn.turn_id,
        status=turn.status.value,
        title=snapshot.title,
        cwd=snapshot.cwd,
        final_message=turn.final_message,
        error_message=_error_text(turn.error_json),
        completed_at=turn.completed_at,
        generated_images=turn.generated_images,
        source=source,
        raw=turn.raw,
    )


def hook_payload_to_event(payload: dict[str, object], thread: ThreadRecord | None) -> TurnEvent:
    return TurnEvent(
        thread_id=str(payload.get("thread-id") or payload.get("thread_id") or ""),
        turn_id=str(payload.get("turn-id") or payload.get("turn_id") or ""),
        status="completed",
        title=(thread.title if thread else ""),
        cwd=str(payload.get("cwd") or (thread.cwd if thread else "")),
        final_message=str(payload.get("last-assistant-message") or payload.get("last_assistant_message") or ""),
        source="codex-notify",
        raw=payload,
    )


class ProgressService:
    """单进程服务；主线程低频轮询，回复工作线程只在收到引用时运行。"""

    def __init__(self, config_path: str | os.PathLike[str]):
        self.config_source = ReloadingConfig(config_path)
        self.stop_event = threading.Event()
        self.reply_queue: queue.Queue[ReplyJob | None] = queue.Queue()
        self.receipt_queue: queue.Queue[ReplyReceiptJob | None] = queue.Queue()
        self.management_queue: queue.Queue[ChannelReply | None] = queue.Queue()
        self._reply_schedule_lock = threading.Lock()
        self._scheduled_reply_codes: set[str] = set()
        self._deferred_reply_codes: set[str] = set()
        self._image_upload_blocked_events: set[str] = set()
        self.config: AppConfig | None = None
        self.store: StateStore | None = None
        self.codec: CorrelationCodec | None = None
        self.codex_store: CodexStore | None = None
        self.channel: MessageChannel | None = None
        self.summarizer: ProgressSummarizer | None = None
        self.reply_thread: threading.Thread | None = None
        self.receipt_thread: threading.Thread | None = None
        self.attention_thread: threading.Thread | None = None
        self.management_thread: threading.Thread | None = None
        self.approval_thread: threading.Thread | None = None
        self.management: CodexManagementController | None = None
        self.account_reader: CodexAccountReader | None = None
        self.approval_bridge: ApprovalBridge | None = None
        self._announced_approval_requests: set[str] = set()
        self._initial_channel_config: object | None = None
        self._initial_wechat_config = None  # 旧测试/扩展兼容；飞书不读取此字段。
        self._initial_service_identity: tuple[Path, Path, Path, int] | None = None
        self._initial_feishu_secret_identity: tuple[Path, int, int, int, int] | None = None
        self._codex_identity: tuple[object, ...] | None = None
        self._fatal: BaseException | None = None
        self._last_wechat_health = 0.0
        self._pending_lock = threading.RLock()
        self._pending_server_replies: dict[str, PendingServerReply] = {}
        self._active_rpc_lock = threading.RLock()
        self._active_rpc: CodexAppServer | None = None
        self._attention_session_lock = threading.RLock()
        self._active_attention_session: VerifiedDesktopAppTools | None = None

    @property
    def wechat(self) -> MessageChannel | None:
        """兼容旧测试与扩展的别名；新代码统一使用 ``channel``。"""

        return self.channel

    @wechat.setter
    def wechat(self, value: MessageChannel | None) -> None:
        self.channel = value

    def request_stop(self, *_args: object) -> None:
        self.stop_event.set()
        self._close_active_rpc()
        self._close_attention_session()

    def _close_active_rpc(self) -> None:
        with self._active_rpc_lock:
            rpc = self._active_rpc
        if rpc is not None:
            rpc.close()

    def _close_attention_session(self) -> None:
        with self._attention_session_lock:
            session = self._active_attention_session
        if session is not None:
            session.close()

    def _retry_sleep(self, delay: float) -> None:
        """让重试退避可被协作停止立即打断。"""

        if self.stop_event.wait(delay):
            raise ServiceStopping("服务正在停止")

    def _policy(self) -> RetryPolicy:
        assert self.config is not None
        return RetryPolicy(self.config.service.max_attempts, self.config.service.retry_delays)

    def _on_retry(self, operation: str):
        def log_failure(attempt: int, error: BaseException) -> None:
            LOGGER.warning("%s 第 %d/%d 次失败：%s", operation, attempt, self._policy().max_attempts, type(error).__name__)
        return log_failure

    def _send_channel_text(self, text: str, *, idempotency_key: str) -> tuple[str, ...]:
        """发送渠道文本；兼容尚未实现幂等参数的旧测试/微信适配器。"""

        if self.channel is None:
            raise ServiceFatalError("消息渠道尚未初始化")
        method = self.channel.send_text
        try:
            parameters = inspect.signature(method).parameters.values()
            supports_key = any(
                item.name == "idempotency_key"
                or item.kind is inspect.Parameter.VAR_KEYWORD
                for item in parameters
            )
        except (TypeError, ValueError):
            supports_key = True
        result = (
            method(text, idempotency_key=idempotency_key)
            if supports_key
            else method(text)  # type: ignore[call-arg]
        )
        if result is None:
            return ()
        values = (result,) if isinstance(result, str) else tuple(result)
        normalized = tuple(dict.fromkeys(str(item or "").strip() for item in values))
        if any(not item for item in normalized):
            raise ServiceFatalError("消息渠道返回了无效 message_id")
        return normalized

    def _send_channel_file(
        self,
        data: bytes,
        *,
        file_name: str,
        idempotency_key: str,
    ) -> tuple[str, ...]:
        if self.channel is None:
            raise ServiceFatalError("消息渠道尚未初始化")
        method = getattr(self.channel, "send_file", None)
        if not callable(method):
            raise ServiceFatalError("当前消息渠道不支持原文件发送")
        result = method(
            data,
            file_name=file_name,
            idempotency_key=idempotency_key,
        )
        if result is None:
            return ()
        values = (result,) if isinstance(result, str) else tuple(result)
        normalized = tuple(dict.fromkeys(str(item or "").strip() for item in values))
        if any(not item for item in normalized):
            raise ServiceFatalError("消息渠道返回了无效文件 message_id")
        return normalized

    def _send_channel_image(
        self,
        data: bytes,
        *,
        idempotency_key: str,
    ) -> tuple[str, ...]:
        if self.channel is None:
            raise ServiceFatalError("消息渠道尚未初始化")
        method = getattr(self.channel, "send_image", None)
        if not callable(method):
            raise ServiceFatalError("当前消息渠道不支持图片发送")
        result = method(data, idempotency_key=idempotency_key)
        if result is None:
            return ()
        values = (result,) if isinstance(result, str) else tuple(result)
        normalized = tuple(dict.fromkeys(str(item or "").strip() for item in values))
        if any(not item for item in normalized):
            raise ServiceFatalError("消息渠道返回了无效图片 message_id")
        return normalized

    def _initialize(self) -> None:
        config = self.config_source.get()
        self.config = config
        if not config.codex.home.is_dir():
            raise ServiceFatalError(f"Codex home 不存在：{config.codex.home}")
        paths = StorePaths.from_codex_home(config.codex.home)
        if not paths.state_db.is_file() or not paths.history_db.is_file():
            raise ServiceFatalError("Codex 结构化状态数据库不完整")
        CorrelationCodec.create_secret_file(config.messaging.secret_file)
        self.codec = CorrelationCodec.from_file(config.messaging.secret_file)
        self.store = StateStore(config.service.database)
        self.store.prune(retention_days=30)
        self.codex_store = CodexStore(paths=paths)
        try:
            # 启动前先核验显式 monitor.ids；若数据库暂时不可读，交给下面的
            # 轮询重试路径处理，避免把瞬时锁/崩溃误判成“ID 不存在”。
            # 但数据库明确可读且 ID 不存在时必须立即 fail-closed，不能先
            # 启动消息渠道后静默地轮询一个不存在的对话。
            self._refresh_monitor_registry(config)
            self._selected_threads(config)
        except CodexStoreReadError as exc:
            LOGGER.warning(
                "启动时 Codex 状态暂不可读，将在监控轮询中按有限重试处理：%s",
                type(exc).__name__,
            )
        self._codex_identity = _codex_connection_identity(config)
        self.summarizer = ProgressSummarizer(config.summary)
        if config.messaging.backend == "probe_only":
            raise ServiceFatalError(
                "微信后端处于 probe_only 安全模式；只读能力验收前禁止启动服务"
            )
        if config.messaging.backend == "feishu":
            secret_identity_before = _file_identity(config.feishu.app_secret_file)
            app_secret = DpapiSecretStore(config.feishu.app_secret_file).load()
            if not app_secret:
                raise ServiceFatalError("飞书 App Secret 尚未安全保存")
            secret_identity_after = _file_identity(config.feishu.app_secret_file)
            if secret_identity_before != secret_identity_after:
                raise ServiceFatalError("读取期间飞书 App Secret 文件发生变化；请重新启动")
            self._initial_feishu_secret_identity = secret_identity_after
            self.channel = FeishuMessageChannel(
                app_id=config.feishu.app_id,
                app_secret=app_secret,
                target_open_id=config.feishu.target_open_id,
                connect_timeout_seconds=config.feishu.connect_timeout_seconds,
                max_attempts=config.service.max_attempts,
                retry_delays=config.service.retry_delays,
                error_handler=self._on_channel_error,
                media_cache_dir=config.service.database.parent / "feishu-media",
            )
            self.account_reader = CodexAccountReader(config.codex.command)
            self._initial_channel_config = (config.messaging, config.feishu)
        elif config.messaging.backend == "wxautox4":
            self.channel = WechatMessageChannel(
                WechatService(
                    WxAutoX4Adapter(account_nickname=config.wechat.tool_account_nickname),
                    tool_wechat_id=config.wechat.tool_wechat_id,
                    chat_name=config.wechat.target_chat,
                    target_wechat_id=config.wechat.target_wechat_id,
                    error_handler=self._on_channel_error,
                )
            )
            self._initial_channel_config = (config.messaging, config.wechat)
            self._initial_wechat_config = config.wechat
        else:
            raise ServiceFatalError("fake 消息后端只能由测试代码显式注入")
        self._initial_service_identity = (
            config.service.database,
            config.service.log_dir,
            config.service.pid_file,
            config.service.log_retention_days,
        )
        call_with_retry(
            "消息渠道启动",
            lambda: self.channel.start(self._on_channel_reply),
            self._policy(),
            sleep=self._retry_sleep,
            on_failure=self._on_retry("消息渠道启动"),
        )
        if (
            config.messaging.backend == "feishu"
            and config.codex.reply_transport == "desktop_app_tools"
        ):
            selected_sources = tuple(self._selected_threads(config))
            self.management = CodexManagementController(
                store=self.store,
                codex_store=self.codex_store,
                desktop_client=DesktopAppToolsClient(
                    config.codex.desktop_log_dir,
                    connect_timeout=2.0,
                    response_timeout=30.0,
                ),
                project_registry=CodexProjectRegistry(
                    config.codex.home / ".codex-global-state.json",
                    config.codex.managed_project_root,
                ),
                source_thread_ids=selected_sources,
                send_text=lambda text, key: self._send_channel_text(
                    text, idempotency_key=key
                ),
                send_image=lambda data, key: self._send_channel_image(
                    data,
                    idempotency_key=key,
                ),
                send_file=lambda data, file_name, key: self._send_channel_file(
                    data,
                    file_name=file_name,
                    idempotency_key=key,
                ),
                account_reader=self.account_reader,
            )
            self.management_thread = threading.Thread(
                target=self._management_worker,
                name="codex-feishu-management",
                daemon=True,
            )
            self.management_thread.start()
            self.approval_bridge = ApprovalBridge(
                config.service.database.parent / "approval-bridge",
                config.messaging.secret_file,
            )
            self.approval_thread = threading.Thread(
                target=self._approval_bridge_worker,
                name="codex-feishu-global-approvals",
                daemon=True,
            )
            self.approval_thread.start()
        uncertain = self.store.uncertain_turn_replies()
        if uncertain:
            raise ServiceFatalError(
                "发现上次退出时结果未知的 Codex 回复；为避免重复输入，需人工核对"
            )
        self.receipt_thread = threading.Thread(
            target=self._receipt_worker,
            name="progress-feishu-receipts",
            daemon=True,
        )
        self.receipt_thread.start()
        self.reply_thread = threading.Thread(target=self._reply_worker, name="progress-channel-replies", daemon=True)
        self.reply_thread.start()
        if config.codex.reply_transport == "desktop_app_tools":
            self.attention_thread = threading.Thread(
                target=self._attention_worker,
                name="progress-desktop-attention",
                daemon=True,
            )
            self.attention_thread.start()
        for code, thread_id, reply_text, fingerprint in self.store.pending_turn_replies():
            self._enqueue_reply_job(ReplyJob(code, thread_id, reply_text, fingerprint))

    def _attention_worker(self) -> None:
        """监听 Desktop 结构化待输入状态；本路径不调用摘要模型。"""

        assert self.config is not None
        client = DesktopAppToolsClient(
            self.config.codex.desktop_log_dir,
            connect_timeout=2.0,
            response_timeout=20.0,
        )
        while not self.stop_event.is_set():
            session: VerifiedDesktopAppTools | None = None
            try:
                session = client.open_verified(
                    required_tools=("list_threads", "wait_threads")
                )
                with self._attention_session_lock:
                    self._active_attention_session = session
                self._attention_connection_loop(session)
            except ServiceStopping:
                return
            except (
                DesktopAppToolsError,
                DesktopAppToolsUnavailable,
                OSError,
                EOFError,
                TimeoutError,
            ) as exc:
                if not self.stop_event.is_set():
                    LOGGER.warning(
                        "Codex Desktop 待输入监听暂不可用，将自动重连（异常类型=%s，原因=%s）",
                        type(exc).__name__,
                        exc,
                    )
                    self.stop_event.wait(5.0)
            except BaseException as exc:
                LOGGER.exception("Codex Desktop 待输入监听失败：%s", type(exc).__name__)
                if self._fatal is None:
                    self._fatal = exc
                self.stop_event.set()
                self._close_active_rpc()
                return
            finally:
                with self._attention_session_lock:
                    if self._active_attention_session is session:
                        self._active_attention_session = None
                if session is not None:
                    session.close()

    def _attention_connection_loop(self, session: VerifiedDesktopAppTools) -> None:
        assert self.config is not None and self.codex_store is not None
        cursors: dict[str, str] = {}
        source_thread_id = ""
        selected_signature: tuple[str, ...] = ()
        loaded_signature: tuple[str, ...] = ()
        batch_offset = 0
        records: dict[str, ThreadRecord | None] = {}
        next_listing_refresh = 0.0
        while not self.stop_event.is_set():
            config = self.config
            selected = self._selected_threads(config)
            signature = tuple(sorted(selected))
            if not signature:
                raise ServiceFatalError("Desktop 待输入监听没有监控目标")
            if signature != selected_signature or not source_thread_id:
                listing = self._attention_listing(session, signature)
                selected_signature = signature
                loaded_signature = desktop_loaded_monitors(listing, set(signature))
                source_thread_id = (
                    loaded_signature[0] if loaded_signature else signature[0]
                )
                records = selected
                cursors = {key: value for key, value in cursors.items() if key in selected}
                batch_offset = 0
                next_listing_refresh = time.monotonic() + 30.0
                LOGGER.info(
                    "Codex Desktop 待输入监听已建立（已加载目标=%d，配置目标=%d）",
                    len(loaded_signature),
                    len(signature),
                )
            elif time.monotonic() >= next_listing_refresh:
                listing = session.list_threads(source_thread_id)
                loaded_signature = desktop_loaded_monitors(listing, set(signature))
                next_listing_refresh = time.monotonic() + 30.0
            if not loaded_signature:
                if self.stop_event.wait(5.0):
                    raise ServiceStopping("服务正在停止")
                next_listing_refresh = 0.0
                continue
            if len(loaded_signature) > 1:
                batch_size = min(8, len(loaded_signature) - 1)
                batch = tuple(
                    loaded_signature[(batch_offset + offset) % len(loaded_signature)]
                    for offset in range(batch_size)
                )
                batch_offset = (batch_offset + batch_size) % len(loaded_signature)
                wait_source = next(
                    item for item in loaded_signature if item not in set(batch)
                )
            else:
                batch = loaded_signature
                top_level_sources = [
                    thread_id
                    for thread_id, record in records.items()
                    if thread_id not in batch
                    and (record is None or record.thread_source != "subagent")
                ]
                if top_level_sources:
                    wait_source = top_level_sources[0]
                else:
                    fallback = desktop_attention_source(listing, set(signature))
                    fallback_record = self.codex_store.get_thread(fallback)
                    self.codex_store.require_readable(
                        f"验证待输入监听来源 {fallback}"
                    )
                    if fallback_record is not None and fallback_record.thread_source == "subagent":
                        raise DesktopAppToolsUnavailable(
                            "待输入监听没有可用的顶层 Codex 任务作为调用来源"
                        )
                    wait_source = fallback
            targets: list[dict[str, str]] = []
            for thread_id in batch:
                target = {"threadId": thread_id, "hostId": "local"}
                cursor = cursors.get(thread_id)
                if cursor:
                    target["afterCursor"] = cursor
                targets.append(target)
            payload = session.wait_threads(
                wait_source,
                targets,
                timeout_ms=1_000 if len(loaded_signature) > 8 else 10_000,
            )
            polls = payload.get("polls")
            if not isinstance(polls, list):
                raise DesktopAppToolsError("Desktop wait_threads 缺少 polls")
            for raw_poll in polls:
                if not isinstance(raw_poll, dict):
                    raise DesktopAppToolsError("Desktop wait_threads poll 格式错误")
                raw_thread = raw_poll.get("thread")
                thread_id = (
                    str(raw_thread.get("id") or "").strip()
                    if isinstance(raw_thread, dict)
                    else ""
                )
                cursor = str(raw_poll.get("cursor") or "").strip()
                if not thread_id or thread_id not in records or not cursor:
                    raise DesktopAppToolsError("Desktop wait_threads 返回了越界任务或空 cursor")
                cursors[thread_id] = cursor
                event = desktop_attention_event(raw_poll, records[thread_id])
                if event is not None and not self.stop_event.is_set():
                    self._send_event(event)

    @staticmethod
    def _attention_listing(
        session: VerifiedDesktopAppTools,
        candidates: tuple[str, ...],
    ) -> dict[str, Any]:
        """用第一个已加载的监控任务取得 Desktop 列表，跳过历史冷任务。"""

        errors: list[DesktopAppToolsError] = []
        for thread_id in candidates:
            try:
                return session.list_threads(thread_id)
            except DesktopAppToolsError as exc:
                errors.append(exc)
        raise DesktopAppToolsUnavailable(
            "全部监控任务在 Codex Desktop 中均未加载，暂不能建立待输入监听"
        ) from (errors[-1] if errors else None)

    def _reload(self) -> AppConfig:
        config = self.config_source.get()
        current_channel_config: object = (
            (config.messaging, config.feishu)
            if config.messaging.backend == "feishu"
            else (config.messaging, config.wechat)
        )
        expected_channel_config = self._initial_channel_config
        if expected_channel_config is None and self._initial_wechat_config is not None:
            expected_channel_config = self._initial_wechat_config
            current_channel_config = config.wechat
        if current_channel_config != expected_channel_config:
            raise ServiceFatalError("消息目标或安全配置已修改；为避免串号，请重启服务")
        if config.messaging.backend == "feishu":
            if _file_identity(config.feishu.app_secret_file) != self._initial_feishu_secret_identity:
                raise ServiceFatalError("飞书 App Secret 文件已修改；为避免凭证串用，请重启服务")
        service_identity = (
            config.service.database,
            config.service.log_dir,
            config.service.pid_file,
            config.service.log_retention_days,
        )
        if service_identity != self._initial_service_identity:
            raise ServiceFatalError("状态库、日志或 PID 配置已修改；请重启服务后生效")
        identity = _codex_connection_identity(config)
        if identity != self._codex_identity:
            raise ServiceFatalError("Codex 连接配置已修改；请重启服务后生效")
        if self.config is not None and config.summary != self.config.summary:
            self.summarizer = ProgressSummarizer(config.summary)
        self.config = config
        return config

    def _legacy_selected_threads(self, config: AppConfig) -> dict[str, ThreadRecord]:
        assert self.codex_store is not None
        selected: dict[str, ThreadRecord] = {}
        for thread_id in config.codex.selectors.ids:
            record = self.codex_store.get_thread(thread_id)
            # get_thread/select_threads 在底层读失败时返回空结果并保留类型化错误。
            # 这里必须先提升读取错误；读取明确成功但没有该 ID 时，也必须
            # 立即报出诊断，否则服务会把不存在的对话静默当成正常空状态。
            self.codex_store.require_readable(f"选择 Codex thread {thread_id}")
            if record is None:
                raise ServiceFatalError(
                    f"配置的 Codex 监控 ID 不存在或当前用户不可见：{thread_id}"
                )
            if record.thread_source != "subagent":
                selected[thread_id] = record
        for title in config.codex.selectors.titles:
            records = self.codex_store.select_threads(title=title)
            for record in records:
                if record.thread_source != "subagent":
                    selected[record.thread_id] = record
            self.codex_store.require_readable(f"按标题选择 Codex thread {title}")
            if not records:
                raise ServiceFatalError(
                    f"配置的 Codex 监控标题不存在或当前用户不可见：{title}"
                )
        for cwd in config.codex.selectors.paths:
            records = self.codex_store.select_threads(cwd=cwd)
            for record in records:
                if record.thread_source != "subagent":
                    selected[record.thread_id] = record
            self.codex_store.require_readable(f"按路径选择 Codex thread {cwd}")
            if not records:
                raise ServiceFatalError(
                    f"配置的 Codex 监控路径不存在或当前用户不可见：{cwd}"
                )
        return selected

    @staticmethod
    def _record_activity_seconds(record: ThreadRecord, *, now: int) -> int:
        raw = record.updated_at_ms or record.created_at_ms
        return int(raw // 1000) if raw else now

    def _selected_threads(self, config: AppConfig) -> dict[str, ThreadRecord | None]:
        """合并旧配置与动态注册表；YAML 只作为向后兼容的手动来源。"""

        assert self.codex_store is not None
        now = int(time.time())
        legacy = self._legacy_selected_threads(config)
        # 离线诊断与单元测试可只提供 Codex 只读存储；生产初始化总会先创建状态库。
        if self.store is None:
            return dict(legacy)
        for thread_id, record in legacy.items():
            self.store.ensure_legacy_manual_monitor(
                thread_id,
                last_activity_at=self._record_activity_seconds(record, now=now),
                now=now,
            )
        selected: dict[str, ThreadRecord | None] = {}
        for item in self.store.monitor_subscriptions(now=now):
            thread_id = str(item["thread_id"])
            record = legacy.get(thread_id) or self.codex_store.get_thread(thread_id)
            self.codex_store.require_readable(f"读取监测任务 {thread_id}")
            if record is not None and record.thread_source == "subagent":
                continue
            selected[thread_id] = record
        return selected

    def _refresh_monitor_registry(self, config: AppConfig) -> None:
        """发现 24 小时内活跃的顶层任务，并为首次升级建立无补发基线。"""

        assert self.codex_store is not None and self.store is not None
        if not hasattr(self.codex_store, "select_threads"):
            return
        now = int(time.time())
        # 先迁移显式配置，确保它们永远不会被自动 TTL 降级。
        self._selected_threads(config)
        records = self.codex_store.select_threads(include_archived=False)
        self.codex_store.require_readable("自动发现 Codex 顶层任务")
        bootstrap = not self.store.monitor_bootstrap_complete()
        for record in records:
            if record.thread_source == "subagent":
                continue
            activity = self._record_activity_seconds(record, now=now)
            created = self.store.discover_auto_monitor(
                record.thread_id,
                last_activity_at=activity,
                now=now,
                ttl_seconds=AUTO_MONITOR_TTL_SECONDS,
            )
            if bootstrap and created:
                snapshot = self.codex_store.snapshot(record.thread_id)
                snapshot.require_readable()
                event = snapshot_to_event(snapshot)
                if event is not None:
                    self.store.mark_processed(event.dedupe_key)
        if bootstrap:
            self.store.mark_monitor_bootstrap_complete()

    def _image_stage_target_is_valid(self, reply_to_message_id: str) -> bool:
        """只允许把图片暂存到可验证的进度通知或会话概览。"""

        if self.store is None or self.codec is None:
            return False
        code = self.store.code_for_channel_message(reply_to_message_id)
        if code and self.store.peek_reply(code, self.codec) is not None:
            return True
        context = self.store.management_context_for_message(reply_to_message_id)
        return context is not None and context[0] == "thread_overview"

    def _restored_staged_attachments(
        self,
        staged: dict[str, Any],
    ) -> tuple[ChannelAttachment, ...]:
        """重新核对暂存路径的边界、文件类型和大小。"""

        if self.config is None:
            return ()
        cache_root = (self.config.service.database.parent / "feishu-media").resolve()
        restored: list[ChannelAttachment] = []
        raw_attachments = staged.get("attachments")
        if not isinstance(raw_attachments, tuple):
            return ()
        for raw in raw_attachments:
            if not isinstance(raw, dict):
                return ()
            try:
                path = Path(str(raw.get("path") or "")).resolve()
                mime_type = str(raw.get("mime_type") or "").casefold()
                sha256 = str(raw.get("sha256") or "").casefold()
                size = int(raw.get("size") or 0)
                actual_size = path.stat().st_size
            except (OSError, TypeError, ValueError):
                return ()
            if (
                not path.is_relative_to(cache_root)
                or not path.is_file()
                or mime_type
                not in {"image/png", "image/jpeg", "image/gif", "image/webp"}
                or len(sha256) != 64
                or any(char not in "0123456789abcdef" for char in sha256)
                or not 0 < size <= 20 * 1024 * 1024
                or actual_size != size
            ):
                return ()
            restored.append(ChannelAttachment(str(path), mime_type, sha256, size))
        return tuple(restored)

    def _prepare_staged_image_reply(
        self,
        message: ChannelReply,
    ) -> tuple[ChannelReply | None, bool]:
        """把手机端分开发送的图片和下一条文字合成一次回复。"""

        if (
            self.config is None
            or self.store is None
            or self.config.messaging.backend != "feishu"
        ):
            return message, False
        content = message.content.strip()
        if message.reply_to_message_id and message.attachments and not content:
            if not self._image_stage_target_is_valid(message.reply_to_message_id):
                self._queue_reply_receipt(
                    received=False,
                    details="这条机器人消息不能定位可继续的 Codex 会话，请引用最新的进度通知或会话概览重新发图。",
                    fingerprint=hashlib.sha256(
                        f"image-stage-invalid|{message.message_id}".encode("utf-8")
                    ).hexdigest(),
                )
                return None, False
            try:
                count, replaced, _expires_at = self.store.stage_image_reply(
                    sender_id=message.sender_id,
                    chat_id=message.chat_id,
                    reply_to_message_id=message.reply_to_message_id,
                    source_message_id=message.message_id,
                    attachments=(
                        {
                            "path": item.path,
                            "mime_type": item.mime_type,
                            "sha256": item.sha256,
                            "size": item.size,
                        }
                        for item in message.attachments
                    ),
                    ttl_seconds=IMAGE_REPLY_STAGE_TTL_SECONDS,
                )
            except ValueError as exc:
                self._queue_reply_receipt(
                    received=False,
                    details=str(exc),
                    fingerprint=hashlib.sha256(
                        f"image-stage-rejected|{message.message_id}".encode("utf-8")
                    ).hexdigest(),
                )
                return None, False
            replaced_text = "已清除之前未发送的图片。" if replaced else ""
            self._queue_reply_receipt(
                received=True,
                details=(
                    f"{replaced_text}已暂存 {count} 张图片，10 分钟内直接发送文字说明即可合并。"
                    "如果只发图片，请发送“.发送”；不想发了请发送“.取消”。"
                ),
                fingerprint=hashlib.sha256(
                    f"image-staged|{message.message_id}".encode("utf-8")
                ).hexdigest(),
            )
            return None, False
        if message.reply_to_message_id:
            return message, False
        staged = self.store.staged_image_reply(message.sender_id, message.chat_id)
        if staged is None:
            return message, False
        if content == ".取消":
            self.store.clear_staged_image_reply(message.sender_id, message.chat_id)
            self._queue_reply_receipt(
                received=True,
                details="已取消这次图片暂存，没有向 Codex 发送任何内容。",
                fingerprint=hashlib.sha256(
                    f"image-stage-cancel|{message.message_id}".encode("utf-8")
                ).hexdigest(),
            )
            return None, False
        if not content:
            return None, False
        reply_to = str(staged.get("reply_to_message_id") or "")
        attachments = self._restored_staged_attachments(staged)
        if not reply_to or not attachments or not self._image_stage_target_is_valid(reply_to):
            self.store.clear_staged_image_reply(message.sender_id, message.chat_id)
            self._queue_reply_receipt(
                received=False,
                details="图片暂存已过期或文件已变化，请重新引用机器人消息发图。",
                fingerprint=hashlib.sha256(
                    f"image-stage-expired|{message.message_id}".encode("utf-8")
                ).hexdigest(),
            )
            return None, False
        combined = ChannelReply(
            sender_id=message.sender_id,
            content="" if content == ".发送" else message.content,
            reply_to_message_id=reply_to,
            message_id=message.message_id,
            chat_id=message.chat_id,
            quote_content=message.quote_content,
            message_hash=message.message_hash,
            attachments=attachments,
        )
        return combined, True

    def _on_channel_reply(self, message: ChannelReply) -> None:
        """渠道回调只做常数时间校验和入队，绝不在回调线程调用 Codex。"""

        try:
            prepared, used_staged_images = self._prepare_staged_image_reply(message)
            if prepared is None:
                return
            message = prepared
            if self.management is not None and self.management.accepts(message):
                self.management_queue.put(message)
                if used_staged_images and self.store is not None:
                    self.store.clear_staged_image_reply(
                        message.sender_id, message.chat_id
                    )
                return
            accepted = self._process_channel_reply(message)
            if used_staged_images and accepted and self.store is not None:
                self.store.clear_staged_image_reply(message.sender_id, message.chat_id)
        except BaseException as exc:
            # 第三方回调线程的异常必须传回主循环，不能静默杀死监听线程。
            LOGGER.exception("消息渠道引用回复处理失败：%s", type(exc).__name__)
            if self._fatal is None:
                self._fatal = exc
            self.stop_event.set()
            self._close_active_rpc()

    def _management_worker(self) -> None:
        """串行执行管理命令；用户格式错误和 Desktop 暂不可用都不拖垮监控。"""

        while not self.stop_event.is_set():
            message = self.management_queue.get()
            if message is None:
                return
            controller = self.management
            if controller is None:
                continue
            try:
                controller.handle(message)
            except ManagementUserError as exc:
                LOGGER.info("Codex 飞书管理请求格式不匹配：%s", exc)
                try:
                    controller.send_user_error(message, str(exc))
                except BaseException:
                    LOGGER.exception("发送 Codex 管理格式提示失败")
            except (
                DesktopAppToolsError,
                DesktopAppToolsUnavailable,
                ProjectRegistryError,
                OSError,
                TimeoutError,
            ) as exc:
                LOGGER.warning(
                    "Codex 飞书管理请求暂不可用（异常类型=%s）",
                    type(exc).__name__,
                )
                try:
                    controller.send_system_error(message)
                except BaseException:
                    LOGGER.exception("发送 Codex 管理故障提示失败")
            except BaseException as exc:
                LOGGER.exception("Codex 飞书管理请求失败：%s", type(exc).__name__)
                try:
                    controller.send_system_error(message)
                except BaseException:
                    LOGGER.exception("发送 Codex 管理故障提示失败")

    def _approval_bridge_worker(self) -> None:
        """Deliver globally captured PermissionRequest hooks without using a model."""

        while not self.stop_event.is_set():
            bridge = self.approval_bridge
            if bridge is None:
                return
            try:
                requests = bridge.pending()
                pending_ids = {request.request_id for request in requests}
                self._announced_approval_requests.intersection_update(pending_ids)
                for request in requests:
                    if self.stop_event.is_set():
                        return
                    if request.request_id in self._announced_approval_requests:
                        continue
                    self._announce_global_approval(request)
                    self._announced_approval_requests.add(request.request_id)
            except ApprovalBridgeError as exc:
                LOGGER.warning("全局审批桥暂不可用：%s", exc)
            except BaseException as exc:
                LOGGER.exception("全局审批桥处理失败：%s", type(exc).__name__)
            self.stop_event.wait(APPROVAL_BRIDGE_POLL_SECONDS)

    def _announce_global_approval(self, request: ApprovalRequest) -> None:
        assert self.config and self.store and self.codec and self.codex_store
        thread = self.codex_store.get_thread(request.session_id) if request.session_id else None
        event = TurnEvent(
            thread_id=request.session_id or f"permission-{request.request_id}",
            turn_id=request.request_id,
            status="waitingOnApproval",
            title=thread.title if thread else "",
            cwd=request.cwd or (thread.cwd if thread else ""),
            final_message=_approval_operation(request),
            source="codex-permission-hook",
            raw={"request_id": request.request_id, "tool_name": request.tool_name},
        )
        if self.store.was_processed(event.dedupe_key):
            return
        code = self.codec.issue()
        message = _approval_message(request, event.display_title)
        stored_code, stored_message = self.store.reserve_notification(
            event,
            code,
            message,
            self.config.messaging.pending_ttl_hours,
            reply_kind="hook",
        )
        message_ids = call_with_retry(
            "飞书发送全局审批请求",
            lambda: self._send_channel_text(
                stored_message,
                idempotency_key=f"global-approval:{request.request_id}",
            ),
            self._policy(),
            sleep=self._retry_sleep,
            on_failure=self._on_retry("飞书发送全局审批请求"),
        )
        if message_ids:
            self.store.bind_channel_messages(event.dedupe_key, message_ids)
        self.store.mark_sent(event.dedupe_key)
        LOGGER.info("已发送一条全局 Codex 审批请求（编号=%s）", stored_code)

    def _on_quote(self, message: QuoteMessage) -> None:
        """旧微信测试/扩展的兼容入口。"""

        self._on_channel_reply(
            ChannelReply(
                sender_id=str(message.sender or ""),
                content=message.content,
                message_id=str(message.message_id or ""),
                chat_id=message.chat_name,
                quote_content=message.quote_content,
                message_hash=str(message.message_hash or ""),
            )
        )

    def _on_channel_error(self, error: BaseException) -> None:
        """把第三方监听线程的故障提升到主循环告警停机路径。"""

        LOGGER.error("消息渠道入站或连接故障：%s", type(error).__name__)
        if self._fatal is None:
            self._fatal = error
        self.stop_event.set()
        self._close_active_rpc()

    def _on_wechat_error(self, error: BaseException) -> None:
        """旧微信测试/扩展的兼容入口。"""

        self._on_channel_error(error)

    def _process_channel_reply(self, message: ChannelReply) -> bool:
        """校验、持久化并路由一条结构化引用回复。"""

        if self.store is None or self.codec is None:
            return False
        if (
            self.config is not None
            and self.config.messaging.backend == "feishu"
            and message.sender_id != self.config.feishu.target_open_id
        ):
            LOGGER.warning("忽略非白名单飞书用户的消息")
            return False
        code_by_id = self.store.code_for_channel_message(message.reply_to_message_id)
        code_by_text = CorrelationCodec.extract(message.quote_content)
        if code_by_id and code_by_text and code_by_id != code_by_text:
            LOGGER.warning("拒绝平台消息 ID 与通知编号不一致的引用回复")
            return
        code = code_by_id or code_by_text
        if message.attachment_error:
            self._queue_reply_receipt(
                received=False,
                details=message.attachment_error,
                fingerprint=hashlib.sha256(
                    f"{message.message_id}|{message.attachment_error}".encode("utf-8")
                ).hexdigest(),
            )
            return False
        content = codex_prompt_for_reply(message).strip()
        if not code or not content:
            LOGGER.info("忽略缺少有效编号或正文的引用回复")
            return False
        # 部分 wxauto 版本可能不提供消息 id/hash；加入通知编号可避免不同轮次
        # 使用相同回复正文时被全局唯一指纹误判为重放。
        raw_identity = (
            f"{message.sender_id}|{message.chat_id}|{message.message_id}|"
            f"{message.message_hash}|{message.reply_to_message_id}|{code}|{content}"
        )
        fingerprint = hashlib.sha256(raw_identity.encode("utf-8")).hexdigest()
        # 持有 pending 锁直到检查和消费完成，避免 worker 同时超时清理连接。
        with self._pending_lock:
            mapping = self.store.peek_reply(code, self.codec)
            if mapping is None:
                LOGGER.warning("拒绝已过期、已消费或签名无效的引用回复")
                return False
            pending = self._pending_server_replies.get(code)
            prepared_response: dict[str, Any] | None = None
            hook_decision: str | None = None
            if mapping[2] == "rpc":
                if pending is None:
                    # app-server 请求只能在原 stdio 连接上回答；保留编号未消费。
                    LOGGER.warning("拒绝已失去原 app-server 连接的请求回复；编号未消费")
                    return
                try:
                    prepared_response = server_request_response(pending.request, content)
                except ValueError as exc:
                    # 不消费编号，让用户仍可重新引用原通知并按指定格式回答。
                    LOGGER.warning("拒绝格式错误的 Codex 服务端请求回复：%s", exc)
                    self._queue_reply_receipt(
                        received=False,
                        details=f"回复格式不符合当前请求要求：{exc}。请引用原通知重新回复。",
                        fingerprint=fingerprint,
                    )
                    return False
            elif mapping[2] == "turn":
                if pending is not None:
                    LOGGER.error("普通轮次编号意外绑定了 RPC 连接，拒绝消费")
                    return False
            elif mapping[2] == "hook":
                bridge = self.approval_bridge
                if bridge is None:
                    LOGGER.warning("全局审批桥尚未初始化；编号未消费")
                    return False
                try:
                    request = bridge.load_request(mapping[1])
                    hook_decision = _approval_decision(
                        content,
                        allow_similar=bool(request.reusable_prefix),
                    )
                    if hook_decision == "allow_similar":
                        assert self.config is not None
                        persist_execpolicy_rule(
                            self.config.codex.home / "rules" / "feishu-approved.rules",
                            request.reusable_prefix,
                            codex_command=self.config.codex.command,
                        )
                    # First commit the signed response that unblocks Codex. If the
                    # process exits after this write, the user's decision is still
                    # durable and the hook can continue safely.
                    bridge.respond(request.request_id, hook_decision)
                except (ApprovalBridgeError, ValueError) as exc:
                    self._queue_reply_receipt(
                        received=False,
                        details=f"没有执行审批：{exc}。请引用原通知重新回复。",
                        fingerprint=fingerprint,
                    )
                    return False
            else:
                LOGGER.error("拒绝未知 reply_kind=%s", mapping[2])
                return False
            consumed = self.store.consume_reply(
                code,
                fingerprint,
                self.codec,
                reply_text=content,
            )
            if consumed is None:
                LOGGER.warning("拒绝已过期、已消费或签名无效的引用回复")
                return False
            if consumed != mapping:
                raise ServiceFatalError("引用回复映射在消费期间发生不可解释的变化")
            if consumed[2] == "rpc":
                assert pending is not None and prepared_response is not None
                pending.responses.put(prepared_response)
                self._queue_reply_receipt(
                    received=True,
                    details=(
                        "已接收你的审批或回答并交给当前 Codex 请求；接下来会按该决定继续处理，"
                        "后续进度仍会通过飞书通知。"
                    ),
                    fingerprint=fingerprint,
                )
                return True
            if consumed[2] == "hook":
                self.store.mark_processed(
                    f"{consumed[0]}:{consumed[1]}:waitingOnApproval"
                )
                label = {
                    "allow": "允许一次",
                    "allow_similar": "允许类似操作",
                    "deny": "拒绝",
                }[hook_decision or "deny"]
                self._queue_reply_receipt(
                    received=True,
                    details=f"已接收审批决定：{label}。Codex 会按该决定继续处理。",
                    fingerprint=fingerprint,
                )
                return True
        if self._enqueue_reply_job(ReplyJob(code, consumed[0], content, fingerprint)):
            self._queue_reply_receipt(
                received=True,
                details=(
                    "已将你的回复加入原 Codex 会话的转交队列；接下来会按这条回复继续处理，"
                    "后续需要你测试、选择或审批时会再通知。"
                ),
                fingerprint=fingerprint,
            )
        return True

    def _queue_reply_receipt(
        self,
        *,
        received: bool,
        details: str,
        fingerprint: str,
    ) -> None:
        """只为飞书严格校验后的回复排队；不阻塞 SDK WebSocket 线程。"""

        if self.config is None or self.config.messaging.backend != "feishu":
            return
        self.receipt_queue.put(
            ReplyReceiptJob(
                received=received,
                details=details,
                idempotency_key=f"reply-receipt:{fingerprint}",
            )
        )

    def _enqueue_reply_job(self, job: ReplyJob) -> bool:
        """把 code 加入 queued/running 集合；同一持久回复进程内只调度一次。"""

        with self._reply_schedule_lock:
            if job.code in self._scheduled_reply_codes:
                return False
            self._scheduled_reply_codes.add(job.code)
        self.reply_queue.put(job)
        return True

    def _requeue_owned_reply_job(self, job: ReplyJob) -> None:
        """worker 已持有该 code，仅移动队列位置，不释放去重所有权。"""

        with self._reply_schedule_lock:
            self._scheduled_reply_codes.add(job.code)
        self.reply_queue.put(job)

    def _finish_reply_job(self, code: str) -> None:
        """Codex 已明确接收正文后释放进程内调度标记。"""

        with self._reply_schedule_lock:
            self._scheduled_reply_codes.discard(code)
        self._deferred_reply_codes.discard(code)

    def _process_quote(self, message: QuoteMessage) -> None:
        """旧微信测试/扩展的兼容入口。"""

        self._on_quote(message)

    def _receipt_worker(self) -> None:
        """独立发送飞书回执，避免被 Codex 长轮次或回复延期阻塞。"""

        while not self.stop_event.is_set():
            try:
                job = self.receipt_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if job is None:
                return
            try:
                call_with_retry(
                    "飞书回复回执发送",
                    lambda: self._send_channel_text(
                        format_reply_receipt(job.received, job.details),
                        idempotency_key=job.idempotency_key,
                    ),
                    self._policy(),
                    sleep=self._retry_sleep,
                    on_failure=self._on_retry("飞书回复回执发送"),
                )
                LOGGER.info("飞书回复回执已发送")
            except ServiceStopping:
                return
            except BaseException as exc:
                LOGGER.exception("飞书回复回执发送失败：%s", type(exc).__name__)
                self._fatal = exc
                self.stop_event.set()
                return

    def _reply_worker(self) -> None:
        while not self.stop_event.is_set():
            try:
                job = self.reply_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if job is None:
                return
            try:
                self._deliver_reply(job)
                self._finish_reply_job(job.code)
            except ReplyDeferred:
                # 目标 Desktop 正在运行且没有共享 gateway 时，正文尚未 claim、
                # 也没有调用 turn/start/turn/steer；持久任务可以安全放回队尾。
                if self.stop_event.is_set():
                    return
                if job.code not in self._deferred_reply_codes:
                    LOGGER.info("目标 Codex 轮次仍在运行；引用回复已持久化等待空闲")
                    self._deferred_reply_codes.add(job.code)
                self._requeue_owned_reply_job(job)
                config = self.config
                delay = config.service.poll_seconds if config is not None else 2.0
                self.stop_event.wait(max(0.2, min(float(delay), 5.0)))
            except ServiceStopping:
                return
            except BaseException as exc:
                LOGGER.exception("消息渠道引用回复投递失败：%s", type(exc).__name__)
                self._fatal = exc
                self.stop_event.set()
                return

    def _deliver_reply(self, job: ReplyJob) -> None:
        assert self.config is not None and self.codex_store is not None and self.store is not None
        config = self.config
        codex_store = self.codex_store
        store = self.store

        if config.codex.reply_transport == "desktop_app_tools":
            self._deliver_reply_via_desktop_tools(job)
            return

        shared_websocket_url: str | None = None
        if config.codex.reply_transport == "shared_websocket":
            shared_websocket_url = active_shared_websocket_url(
                websocket_url=config.codex.shared_websocket_url,
                gateway_pid_file=config.codex.gateway_pid_file,
                state_file=config.codex.shared_desktop_state_file,
            )
        observed_status = ThreadStatus.UNKNOWN

        if self.stop_event.is_set():
            raise ServiceStopping("服务正在停止")

        def preflight() -> bool:
            nonlocal observed_status
            if self.stop_event.is_set():
                raise ServiceStopping("服务正在停止")
            status = codex_store.status(job.thread_id)
            observed_status = status
            if status == ThreadStatus.IN_PROGRESS and shared_websocket_url is not None:
                return True
            if status == ThreadStatus.IN_PROGRESS:
                # stdio 模式不能安全 steer 另一个进程持有的 Desktop turn；这不是
                # 连接错误，且尚未提交正文，应持久等待终态而非消耗五次重试。
                return False
            if status not in {
                ThreadStatus.COMPLETED,
                ThreadStatus.INTERRUPTED,
                ThreadStatus.FAILED,
            }:
                raise DesktopTurnBusyError(
                    f"目标对话状态为 {status.value}；无法证明它处于可安全恢复的终态"
                )
            return True

        ready = call_with_retry(
            "Codex 回复前检查",
            preflight,
            self._policy(),
            sleep=self._retry_sleep,
            on_failure=self._on_retry("Codex 回复前检查"),
        )
        if not ready:
            consumed_at = store.pending_turn_reply_consumed_at(job.code)
            if consumed_at is None:
                raise ServiceFatalError("延期回复的持久状态不一致")
            if time.time() - consumed_at >= config.codex.reply_timeout_seconds:
                raise ServiceFatalError(
                    "目标 Codex 轮次持续运行；回复仍安全保留，等待人工处理"
                )
            raise ReplyDeferred("目标 Desktop 轮次仍在运行，等待终态后投递")
        if self.stop_event.is_set():
            raise ServiceStopping("服务正在停止")
        # resume 可安全重试；此时尚未提交用户正文。
        rpc = CodexAppServer(
            config.codex.command,
            timeout_seconds=30,
            websocket_url=shared_websocket_url,
        )
        with self._active_rpc_lock:
            if self.stop_event.is_set():
                raise ServiceStopping("服务正在停止")
            self._active_rpc = rpc
        deferred_jobs: list[ReplyJob] = []
        try:
            deadline = time.monotonic() + config.codex.reply_timeout_seconds

            def remaining_time() -> float:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CodexRPCTimeout("远程回复发起的 Codex 轮次已超过总时限")
                return remaining

            if observed_status == ThreadStatus.IN_PROGRESS:
                active_turn_id = ""

                def read_active_turn() -> str:
                    response = rpc.read_thread(
                        job.thread_id,
                        include_turns=True,
                        timeout_seconds=remaining_time(),
                    )
                    turn_id = rpc.active_turn_id(response)
                    if not turn_id:
                        raise CodexRPCError("共享 thread 没有唯一活动 turn")
                    return turn_id

                active_turn_id = call_with_retry(
                    "Codex 活动 turn 读取",
                    read_active_turn,
                    self._policy(),
                    sleep=self._retry_sleep,
                    on_failure=self._on_retry("Codex 活动 turn 读取"),
                )
                accepted = self._steer_active_reply(
                    rpc,
                    job,
                    active_turn_id,
                    timeout_seconds=remaining_time(),
                    on_server_request=None,
                )
                if not accepted:
                    # 明确拒绝表示竞态中轮次已结束；claim 已撤销，可按新 turn 再取一次。
                    raise ReplyDeferred("活动轮次已明确拒绝 steer，等待终态后重新投递")
                return

            active_drain: Callable[[], None] | None = None

            def handle_early_request(request: ServerRequest) -> None:
                self._handle_server_request(
                    rpc,
                    request,
                    deadline,
                    drain_active_replies=active_drain,
                )

            def resume() -> dict[str, Any]:
                if self.stop_event.is_set():
                    raise ServiceStopping("服务正在停止")
                return rpc.resume_thread(
                    job.thread_id,
                    timeout_seconds=remaining_time(),
                    on_server_request=handle_early_request,
                )

            call_with_retry(
                "Codex thread/resume",
                resume,
                self._policy(),
                sleep=self._retry_sleep,
                on_failure=self._on_retry("Codex thread/resume"),
            )
            if self.stop_event.is_set():
                raise ServiceStopping("服务正在停止")
            if not store.claim_turn_reply(job.code):
                raise ServiceFatalError("回复未能进入唯一的 Codex 投递临界区")
            # turn/start 是非幂等写操作，只提交一次。超时意味着结果未知，禁止盲目重发。
            try:
                start_response = rpc.start_turn(
                    job.thread_id,
                    job.reply_text,
                    timeout_seconds=remaining_time(),
                    on_server_request=handle_early_request,
                )
            except CodexRPCTimeout as exc:
                raise ServiceFatalError("turn/start 结果未知，为避免重复输入已停止") from exc
            expected_turn_id = started_turn_id(start_response)
            store.mark_reply_delivered(job.code)
            draining_replies = False

            def drain_active_replies() -> None:
                """在等待同一轮次时，把后续同线程引用作为 ``turn/steer`` 追加。"""

                nonlocal draining_replies
                if draining_replies:
                    return
                draining_replies = True
                try:
                    while True:
                        try:
                            additional = self.reply_queue.get_nowait()
                        except queue.Empty:
                            return
                        if additional is None:
                            raise ServiceStopping("服务正在停止")
                        if additional.thread_id != job.thread_id:
                            deferred_jobs.append(additional)
                            continue
                        accepted = self._steer_active_reply(
                            rpc,
                            additional,
                            expected_turn_id,
                            timeout_seconds=remaining_time(),
                            on_server_request=handle_early_request,
                        )
                        if not accepted:
                            # 服务端明确拒绝通常表示轮次刚结束；稍后按普通新轮次处理。
                            deferred_jobs.append(additional)
                        else:
                            self._finish_reply_job(additional.code)
                finally:
                    draining_replies = False

            active_drain = drain_active_replies

            while True:
                drain_active_replies()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CodexRPCTimeout("等待远程回复发起的 Codex 轮次结束超时")
                try:
                    item = rpc.listen_event(
                        job.thread_id,
                        turn_id=expected_turn_id,
                        timeout_seconds=min(0.5, remaining),
                    )
                except CodexRPCTimeout:
                    # 短超时用于轮询后续渠道引用，不代表总轮次超时。
                    continue
                except CodexRPCClosed as exc:
                    if self.stop_event.is_set():
                        raise ServiceStopping("服务正在停止") from exc
                    raise
                if isinstance(item, TurnCompletedEvent):
                    if (
                        item.thread_id != job.thread_id
                        or item.turn_id != expected_turn_id
                        or not item.is_terminal
                    ):
                        raise ServiceFatalError("Codex 完成事件与本次投递的 thread/turn/终态不一致")
                    return
                self._handle_server_request(
                    rpc,
                    item,
                    deadline,
                    drain_active_replies=drain_active_replies,
                )
        finally:
            rpc.close()
            with self._active_rpc_lock:
                if self._active_rpc is rpc:
                    self._active_rpc = None
            if not self.stop_event.is_set():
                for deferred in deferred_jobs:
                    self._requeue_owned_reply_job(deferred)

    def _deliver_reply_via_desktop_tools(self, job: ReplyJob) -> None:
        """通过 Codex Desktop 官方任务工具投递一条飞书引用回复。

        ``tools/list`` 握手可以安全重试；只有在同一连接已验明工具身份后才 claim。
        ``tools/call`` 是非幂等写操作，只调用一次。进入写入阶段后若结果未知，保留
        claim 并停止服务，避免把同一条用户回复重复送入 Codex。
        """

        assert self.config is not None and self.codex_store is not None and self.store is not None
        config = self.config
        store = self.store

        if self.stop_event.is_set():
            raise ServiceStopping("服务正在停止")

        def verify_thread() -> bool:
            status = self.codex_store.status(job.thread_id)
            if status not in {
                ThreadStatus.IN_PROGRESS,
                ThreadStatus.COMPLETED,
                ThreadStatus.INTERRUPTED,
                ThreadStatus.FAILED,
            }:
                raise DesktopTurnBusyError(
                    f"目标对话状态为 {status.value}；无法证明它是可投递的本地任务"
                )
            return True

        call_with_retry(
            "Codex Desktop 目标任务检查",
            verify_thread,
            self._policy(),
            sleep=self._retry_sleep,
            on_failure=self._on_retry("Codex Desktop 目标任务检查"),
        )
        client = DesktopAppToolsClient(
            config.codex.desktop_log_dir,
            connect_timeout=2.0,
            response_timeout=30.0,
        )

        def open_verified():
            if self.stop_event.is_set():
                raise ServiceStopping("服务正在停止")
            return client.open_verified()

        session = call_with_retry(
            "Codex Desktop 应用工具握手",
            open_verified,
            self._policy(),
            sleep=self._retry_sleep,
            on_failure=self._on_retry("Codex Desktop 应用工具握手"),
        )
        try:
            if self.stop_event.is_set():
                raise ServiceStopping("服务正在停止")
            if not store.claim_turn_reply(job.code):
                raise ServiceFatalError("回复未能进入唯一的 Desktop 工具投递临界区")
            try:
                session.send_message(
                    job.thread_id,
                    job.reply_text,
                    call_tag=job.code,
                )
            except DesktopAppToolsResultUnknown as exc:
                raise ServiceFatalError(
                    "send_message_to_thread 结果未知，为避免重复输入已停止"
                ) from exc
            except (OSError, EOFError, TimeoutError, DesktopAppToolsUnavailable) as exc:
                raise ServiceFatalError(
                    "send_message_to_thread 写入后连接中断，结果未知；为避免重复输入已停止"
                ) from exc
            store.mark_reply_delivered(job.code)
            LOGGER.info("飞书引用回复已由 Codex Desktop 官方任务工具接受")
        finally:
            session.close()

    def _steer_active_reply(
        self,
        rpc: CodexAppServer,
        job: ReplyJob,
        expected_turn_id: str,
        *,
        timeout_seconds: float,
        on_server_request: Callable[[ServerRequest], None] | None,
    ) -> bool:
        """把一条已持久化回复追加到本连接拥有的活动轮次。

        返回 ``False`` 只表示 app-server 给出明确 JSON-RPC 拒绝，此时可以安全
        撤销 claim，等活动轮次结束后改走新 ``turn/start``。超时、断连或响应
        turn id 不一致都属于结果未知，必须保留 claim 并停机人工核对。
        """

        assert self.store is not None
        if not self.store.claim_turn_reply(job.code):
            raise ServiceFatalError("追加回复未能进入唯一的 Codex 投递临界区")
        try:
            response = rpc.steer_turn(
                job.thread_id,
                expected_turn_id,
                job.reply_text,
                timeout_seconds=timeout_seconds,
                on_server_request=on_server_request,
            )
        except (CodexRPCTimeout, CodexRPCClosed, CodexRPCUnhandledRequest) as exc:
            raise ServiceFatalError(
                "turn/steer 结果未知，为避免重复输入已停止"
            ) from exc
        except CodexRPCError:
            # JSON-RPC error 是服务端的明确拒绝，不是超时；正文没有被接受。
            if not self.store.resolve_uncertain_reply(job.code, delivered=False):
                raise ServiceFatalError("无法撤销被明确拒绝的 turn/steer claim")
            return False
        if steered_turn_id(response) != expected_turn_id:
            raise ServiceFatalError("turn/steer 返回了非预期活动 turn id；追加结果未知")
        self.store.mark_reply_delivered(job.code)
        return True

    def _handle_server_request(
        self,
        rpc: CodexAppServer,
        request: ServerRequest,
        deadline: float,
        *,
        drain_active_replies: Callable[[], None] | None = None,
    ) -> None:
        """把本连接拥有的审批/人工输入请求通知到消息渠道并在原连接上回答。"""

        assert (
            self.config
            and self.store
            and self.codec
            and self.channel
            and self.codex_store
            and self.summarizer
        )
        thread = self.codex_store.get_thread(request.thread_id) if request.thread_id else None
        event = server_request_event(request, thread)
        # 官方请求方法已经精确表达等待类型；人工介入通知无需调用模型。
        report = structural_report(event)
        code = self.codec.issue()
        message = format_notification(
            event,
            report,
            code,
            include_reply_code=self.config.messaging.backend != "feishu",
        )
        stored_code, stored_message = self.store.reserve_notification(
            event,
            code,
            message,
            self.config.messaging.pending_ttl_hours,
            reply_kind="rpc",
        )
        pending = PendingServerReply(request, queue.Queue(maxsize=1))
        with self._pending_lock:
            if stored_code in self._pending_server_replies:
                raise ServiceFatalError("Codex 服务端请求编号发生冲突")
            self._pending_server_replies[stored_code] = pending
        try:
            channel_message_ids = call_with_retry(
                "消息渠道发送审批/输入请求",
                lambda: self._send_channel_text(
                    stored_message,
                    idempotency_key=f"notification:{event.dedupe_key}",
                ),
                self._policy(),
                sleep=self._retry_sleep,
                on_failure=self._on_retry("消息渠道发送审批/输入请求"),
            )
            if channel_message_ids:
                self.store.bind_channel_messages(event.dedupe_key, channel_message_ids)
            self.store.mark_sent(event.dedupe_key)
            while True:
                if self.stop_event.is_set():
                    raise ServiceStopping("服务正在停止")
                if drain_active_replies is not None:
                    drain_active_replies()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CodexRPCTimeout("等待远程审批/人工输入回复超时")
                try:
                    response = pending.responses.get(timeout=min(0.5, remaining))
                    break
                except queue.Empty:
                    continue
            # JSON-RPC 响应是一次性写操作；失败时结果未知，禁止盲目重发。
            rpc.respond(request.request_id, response)
            self.store.mark_processed(event.dedupe_key)
        finally:
            with self._pending_lock:
                self._pending_server_replies.pop(stored_code, None)

    def _progress_quota_footer(self) -> str:
        """读取进度通知所附额度；失败不得阻断主通知。"""

        if self.account_reader is None:
            return ""
        try:
            return format_rate_limits(self.account_reader.read())
        except CodexAccountError as exc:
            LOGGER.warning("进度通知额度附注暂不可读：%s", exc)
        except Exception:
            LOGGER.exception("进度通知额度附注发生未预期错误")
        return "Codex 每周额度：暂时无法读取\n剩余重置卡：暂时无法读取"

    def _send_event(self, event: TurnEvent) -> None:
        assert self.config and self.store and self.codec and self.channel and self.summarizer
        if event.status == "completed" and not event.final_message.strip():
            LOGGER.info(
                "暂缓正文尚未就绪的完成事件 thread=%s turn=%s",
                event.thread_id,
                event.turn_id,
            )
            return
        if self.store.was_processed(event.dedupe_key):
            return
        if event.dedupe_key in self._image_upload_blocked_events:
            return
        if event.status in {"waitingOnApproval", "waitingOnUserInput"}:
            # 结构化状态足以精确分类；避免为持续监听或人工介入消耗模型额度。
            report = structural_report(event)
        else:
            report = call_with_retry(
                "进度摘要",
                lambda: self.summarizer.summarize(event, wait=self.stop_event.wait),
                self._policy(),
                sleep=self._retry_sleep,
                on_failure=self._on_retry("进度摘要"),
            )
        code = self.codec.issue()
        message = format_notification(
            event,
            report,
            code,
            include_reply_code=self.config.messaging.backend != "feishu",
        )
        prepared_images: list[tuple[GeneratedImageArtifact, bytes]] = []
        skipped_images = 0
        if event.generated_images:
            if self.config.messaging.backend == "feishu":
                for artifact in event.generated_images:
                    try:
                        data = read_generated_image_bytes(artifact)
                    except ValueError:
                        skipped_images += 1
                        LOGGER.warning(
                            "跳过已变化或不可读的生成图片 thread=%s turn=%s item=%s",
                            event.thread_id,
                            event.turn_id,
                            artifact.item_id,
                        )
                    else:
                        prepared_images.append((artifact, data))
            else:
                skipped_images = len(event.generated_images)
        if prepared_images:
            message += (
                f"\n\n生成图片：{len(prepared_images)} 张图片将在下方直接展示"
                "（上传前不转码、不压缩）。"
            )
        if skipped_images:
            message += f"\n\n生成图片：{skipped_images} 张原文件不可安全读取，未发送。"
        quota_footer = self._progress_quota_footer()
        if quota_footer:
            message += f"\n\n{quota_footer}"
        stored_code, stored_message = self.store.reserve_notification(
            event, code, message, self.config.messaging.pending_ttl_hours
        )
        sent_message_ids = list(call_with_retry(
            "消息渠道发送",
            lambda: self._send_channel_text(
                stored_message,
                idempotency_key=f"notification:{event.dedupe_key}",
            ),
            self._policy(),
            sleep=self._retry_sleep,
            on_failure=self._on_retry("消息渠道发送"),
        ))
        if sent_message_ids:
            self.store.bind_channel_messages(event.dedupe_key, sent_message_ids)
        # 正文一旦可靠送达就立即开放引用回复。图片属于同一事件的附加交付，
        # 其失败不能让已收到的正文在状态库中继续伪装成“未发送”。
        self.store.mark_sent(event.dedupe_key)
        for artifact, data in prepared_images:
            try:
                image_message_ids = call_with_retry(
                    "生成图片消息发送",
                    lambda artifact=artifact, data=data: self._send_channel_image(
                        data,
                        idempotency_key=(
                            f"notification:{event.dedupe_key}:image:"
                            f"{artifact.item_id}:{artifact.sha256}"
                        ),
                    ),
                    self._policy(),
                    sleep=self._retry_sleep,
                    on_failure=self._on_retry("生成图片消息发送"),
                    should_retry=lambda error: not isinstance(
                        error, FeishuSendRejectedError
                    ) or error.retryable,
                )
            except FeishuSendRejectedError as exc:
                self._image_upload_blocked_events.add(event.dedupe_key)
                if exc.code == "permission_denied":
                    detail = (
                        "图片暂未发送：飞书应用缺少资源上传权限 "
                        "im:resource（或 im:resource:upload）。请在飞书开放"
                        "平台开通权限并发布，"
                        "然后重启后台服务，系统会重试本张图片。"
                    )
                else:
                    detail = (
                        "图片暂未发送：飞书明确拒绝上传"
                        f"（分类 {exc.code}，错误码 {exc.raw_code}）。"
                        "其他进度监测仍会继续运行。"
                    )
                warning_ids = call_with_retry(
                    "图片回传失败提示",
                    lambda: self._send_channel_text(
                        detail,
                        idempotency_key=f"image-delivery-blocked:{event.dedupe_key}",
                    ),
                    self._policy(),
                    sleep=self._retry_sleep,
                    on_failure=self._on_retry("图片回传失败提示"),
                )
                if warning_ids:
                    self.store.bind_channel_messages(event.dedupe_key, warning_ids)
                LOGGER.warning(
                    "生成图片被飞书永久拒绝，本次运行暂停该图片交付 "
                    "thread=%s turn=%s code=%s raw_code=%s",
                    event.thread_id,
                    event.turn_id,
                    exc.code,
                    exc.raw_code,
                )
                return
            sent_message_ids.extend(image_message_ids)
            if image_message_ids:
                self.store.bind_channel_messages(event.dedupe_key, image_message_ids)
        del stored_code
        self.store.mark_processed(event.dedupe_key)
        LOGGER.info("已发送 thread=%s turn=%s status=%s", event.thread_id, event.turn_id, event.status)

    def _poll_once(self, config: AppConfig) -> None:
        assert self.store is not None and self.codex_store is not None
        if time.monotonic() - self._last_wechat_health >= 30:
            if self.channel is None or not self.channel.is_online():
                raise ServiceFatalError("消息渠道已离线")
            self._last_wechat_health = time.monotonic()
        self._refresh_monitor_registry(config)
        selected = self._selected_threads(config)
        for event_key, payload in self.store.pending_hook_payloads():
            thread_id = str(payload.get("thread-id") or payload.get("thread_id") or "")
            if thread_id in selected:
                event = hook_payload_to_event(dict(payload), selected[thread_id])
                if event.status == "completed" and not event.final_message.strip():
                    # notify 钩子偶尔早于历史库正文落盘。只允许用同一 thread、
                    # 同一 turn 的严格结构化数据库事件补全，禁止串到其他轮次。
                    snapshot = self.codex_store.snapshot(thread_id)
                    snapshot.require_readable()
                    projected = snapshot_to_event(snapshot)
                    if (
                        projected is None
                        or projected.thread_id != event.thread_id
                        or projected.turn_id != event.turn_id
                        or projected.status != event.status
                    ):
                        if self.store.was_processed(event.dedupe_key):
                            self.store.mark_hook_consumed(event_key)
                        continue
                    event = projected
                self._send_event(event)
            self.store.mark_hook_consumed(event_key)
        for thread_id in selected:
            # ``unknown`` 是健康数据库中没有轮次/未知显式 status 的合法结果；
            # 只有 errors 才表示读取异常，必须抛给 run() 的有限重试熔断路径。
            snapshot = self.codex_store.snapshot(thread_id)
            snapshot.require_readable()
            event = snapshot_to_event(snapshot)
            if event is not None:
                self._send_event(event)

    def _alert(self, error: BaseException) -> None:
        self._close_active_rpc()
        summary = f"【进度通知已停止】发生不可恢复错误或连续失败达到上限：{type(error).__name__}。请查看本机 logs。"
        sent = False
        if self.channel is not None:
            try:
                if self.channel.is_online():
                    self._send_channel_text(
                        summary,
                        idempotency_key=(
                            "fatal-alert:" + hashlib.sha256(summary.encode("utf-8")).hexdigest()
                        ),
                    )
                    sent = True
            except Exception as exc:
                # 只记录异常类型，不记录异常正文，避免第三方 SDK 将凭证或消息内容
                # 拼入异常文本后落入本地日志；随后仍继续走本地弹窗兜底。
                LOGGER.warning(
                    "致命告警无法通过消息渠道发送，将尝试本地弹窗（异常类型=%s）",
                    type(exc).__name__,
                )
            finally:
                # 弹窗是同步调用；先回收监听线程，避免弹窗期间仍接收业务消息。
                try:
                    self.channel.stop()
                except Exception:
                    pass
        if sent or os.name != "nt":
            return
        try:
            ctypes.windll.user32.MessageBoxW(None, summary, "进度通知需要帮助", 0x00000010 | 0x00040000)
        except Exception:
            pass

    def run(self) -> int:
        try:
            self._initialize()
            try:
                signal.signal(signal.SIGINT, self.request_stop)
                signal.signal(signal.SIGTERM, self.request_stop)
            except ValueError:
                pass
            while not self.stop_event.is_set():
                cycle_config: AppConfig | None = None

                def cycle() -> None:
                    nonlocal cycle_config
                    if self.stop_event.is_set():
                        raise ServiceStopping("服务正在停止")
                    cycle_config = self._reload()
                    self._poll_once(cycle_config)

                call_with_retry(
                    "监控轮询",
                    cycle,
                    self._policy(),
                    sleep=self._retry_sleep,
                    on_failure=self._on_retry("监控轮询"),
                )
                assert cycle_config is not None
                config = cycle_config
                self.stop_event.wait(config.service.poll_seconds)
            if self._fatal:
                raise self._fatal
            return 0
        except ServiceStopping:
            return 0
        except BaseException as exc:
            LOGGER.exception("服务因致命错误停止：%s", type(exc).__name__)
            self.stop_event.set()
            self._alert(exc)
            return 1
        finally:
            self.request_stop()
            self.reply_queue.put(None)
            self.receipt_queue.put(None)
            self.management_queue.put(None)
            if self.approval_thread is not None and self.approval_thread is not threading.current_thread():
                self.approval_thread.join(timeout=10)
            if self.attention_thread is not None and self.attention_thread is not threading.current_thread():
                self.attention_thread.join(timeout=15)
            if self.channel is not None:
                try:
                    self.channel.stop()
                except Exception:
                    pass
            if self.reply_thread is not None and self.reply_thread is not threading.current_thread():
                self.reply_thread.join(timeout=10)
            if self.receipt_thread is not None and self.receipt_thread is not threading.current_thread():
                self.receipt_thread.join(timeout=10)
            if self.management_thread is not None and self.management_thread is not threading.current_thread():
                self.management_thread.join(timeout=10)
            worker_stopped = self.reply_thread is None or not self.reply_thread.is_alive()
            if not worker_stopped:
                LOGGER.critical("回复线程未能在 10 秒内停止；保留状态库连接以避免并发关闭")
            if self.store is not None and worker_stopped:
                self.store.close()
