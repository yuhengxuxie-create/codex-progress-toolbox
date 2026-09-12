"""长期运行的监控编排：Codex 事件 → 消息通知 → 引用回复 → Codex。"""

from __future__ import annotations

import ctypes
import hashlib
import io
import inspect
import json
import logging
import os
import queue
import re
import signal
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
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
    discover_desktop_codex_command,
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
    DesktopAppToolsNotSubmitted,
    DesktopAppToolsRejected,
    DesktopAppToolsResultUnknown,
    DesktopAppToolsUnavailable,
    VerifiedDesktopAppTools,
)
from .codex_management import (
    CodexManagementController, ManagementUserError,
    is_direct_management_candidate, is_dot_command,
)
from .remote_control import (
    AppServerRemoteControl,
    VALIDATED_REMOTE_WRITE_CAPABILITIES,
)
from .codex_projects import CodexProjectRegistry, ProjectRegistryError
from .codex_gateway import active_shared_websocket_url
from .codex_store import (
    CodexStore,
    CodexStoreReadError,
    public_thread_title,
    read_generated_image_bytes,
    StorePaths,
    ThreadRecord,
    ThreadSnapshot,
    ThreadStatus,
    thread_title_recovery_hash,
)
from .channel import (
    ChannelAttachment,
    ChannelReply,
    MessageChannel,
    MessageChannelOfflineError,
    WechatMessageChannel,
    codex_prompt_for_reply,
)
from .config import AppConfig, ReloadingConfig
from .feishu import FeishuMessageChannel, FeishuSendError, FeishuSendRejectedError
from .formatting import format_notification, format_reply_receipt
from .models import (
    GeneratedImageArtifact,
    NotificationContext,
    NotificationReason,
    ProgressReport,
    TERMINAL_TURN_STATUSES,
    TurnEvent,
    structural_report,
)
from .retry import RetryExhausted, RetryPolicy, call_with_retry
from .reset_alert import ResetAlertWorker
from .process_control import write_channel_health
from .secrets import DpapiSecretStore
from .state import (
    CorrelationCodec,
    NotificationMediaDelivery,
    NotificationRawContext,
    NotificationRawDelivery,
    NotificationSummaryDelivery,
    NotificationSummaryRecoveryCandidate,
    StateError,
    StateStore,
)
from .user_reply_chain import (
    UserReplyChainError,
    UserReplyChainStore,
    content_hash as user_reply_content_hash,
)
from .session_search import LunaSemanticJudge, SessionSearchEngine
from .summarizer import (
    ProgressSummarizer,
    SummaryCancelled,
    fallback_report,
)
from .wechat import QuoteMessage, WechatService, WxAutoX4Adapter


LOGGER = logging.getLogger("progress_wx.service")
AUTO_MONITOR_TTL_SECONDS = 24 * 60 * 60
IMAGE_REPLY_STAGE_TTL_SECONDS = 10 * 60
APPROVAL_BRIDGE_POLL_SECONDS = 0.5
HEARTBEAT_CONTROL_MAX_CHARS = 16_384
FEISHU_IMAGE_DIRECT_MAX_BYTES = 20 * 1024 * 1024
SUMMARY_WORKER_IDLE_SECONDS = 0.5
SUMMARY_WORKER_STOP_TIMEOUT_SECONDS = 10.0
NOTIFICATION_RAW_WORKER_IDLE_SECONDS = 0.5
NOTIFICATION_RAW_WORKER_STOP_TIMEOUT_SECONDS = 10.0
PARENT_RECOVERY_WORKER_STOP_TIMEOUT_SECONDS = 10.0
PARENT_RECOVERY_CLOCK_SKEW_SECONDS = 5


def _prepare_feishu_image(data: bytes, mime_type: str) -> tuple[bytes, bool]:
    """Return direct-display bytes, proportionally shrinking oversized images."""

    payload = bytes(data)
    if len(payload) <= FEISHU_IMAGE_DIRECT_MAX_BYTES:
        return payload, False
    # Pillow is part of the locked Windows runtime for image delivery. Import it
    # lazily so text-only installations can still validate configuration.
    from PIL import Image

    source = Image.open(io.BytesIO(payload))
    source.load()
    if source.width <= 0 or source.height <= 0:
        raise ValueError("生成图片尺寸无效")
    image = source.copy()
    image_format = {
        "image/png": "PNG",
        "image/jpeg": "JPEG",
        "image/webp": "WEBP",
    }.get(mime_type)
    if image_format is None:
        raise ValueError("生成图片 MIME 类型不受支持")
    for _attempt in range(10):
        output = io.BytesIO()
        save_args: dict[str, object] = {"format": image_format}
        if image_format == "PNG":
            save_args["optimize"] = True
        elif image_format == "JPEG":
            if image.mode not in {"RGB", "L"}:
                image = image.convert("RGB")
            save_args.update(optimize=True, quality=90)
        else:
            save_args.update(method=6, quality=90)
        image.save(output, **save_args)
        transformed = output.getvalue()
        if transformed and len(transformed) <= FEISHU_IMAGE_DIRECT_MAX_BYTES:
            return transformed, True
        width = max(1, int(image.width * 0.82))
        height = max(1, int(image.height * 0.82))
        if (width, height) == image.size:
            break
        image = image.resize((width, height), Image.Resampling.LANCZOS)
    raise ValueError("生成图片按比例压缩后仍超过飞书安全上限")


_APPROVAL_SECRET_PATTERN = re.compile(
    r"(?i)(api[_-]?key|token|secret|authorization|password)(\s*[:=]\s*)([^\s,;]+)"
)


def _parse_heartbeat_control(message: str) -> tuple[str, str] | None:
    """严格解析 Codex 自动化心跳的完整控制报文。

    只有整条最终回复都是无命名空间、无属性、无嵌套节点的 ``heartbeat``
    报文时才视为控制协议。普通正文即使提到 ``DONT_NOTIFY`` 或粘贴一段
    示例 XML，也不得被静默丢弃。拒绝声明和 DTD，并限制长度，避免把 XML
    解析器暴露给不受限输入。
    """

    text = str(message or "").strip()
    if (
        not text
        or len(text) > HEARTBEAT_CONTROL_MAX_CHARS
        or "<!" in text
        or "<?" in text
    ):
        return None
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    if root.tag != "heartbeat" or root.attrib or (root.text or "").strip():
        return None

    required = frozenset({"automation_id", "decision", "message"})
    fields: dict[str, str] = {}
    for child in root:
        if (
            child.tag not in required
            or child.tag in fields
            or child.attrib
            or len(child)
            or (child.tail or "").strip()
        ):
            return None
        fields[child.tag] = child.text or ""
    if fields.keys() != required or not fields["automation_id"].strip():
        return None
    decision = fields["decision"].strip().upper()
    if decision not in {"NOTIFY", "DONT_NOTIFY"}:
        return None
    readable_message = fields["message"].strip()
    if decision == "NOTIFY" and not readable_message:
        readable_message = "自动化请求发送通知，但没有提供可读正文。"
    return decision, readable_message


class ServiceFatalError(RuntimeError):
    """必须告警并停止整个服务的不可恢复错误。"""


class DesktopTurnBusyError(ServiceFatalError):
    """目标轮次由另一个桌面 app-server 持有，当前进程不能安全接管。"""


class ReplyDeferred(RuntimeError):
    """正文尚未进入非幂等临界区，可等待目标轮次空闲后安全重试。"""


class ReplyCannotContinue(RuntimeError):
    """父消息或目标任务已明确不可继续；可安全丢弃当前子投递。"""


class ServiceStopping(RuntimeError):
    """用户主动停止服务时，用于退出正在等待远程输入的工作线程。"""


def _is_channel_offline_failure(error: BaseException) -> bool:
    """识别可安全延后的消息渠道离线，不把未知结果误当成离线。"""

    if isinstance(error, MessageChannelOfflineError):
        return True
    if isinstance(error, RetryExhausted):
        return _is_channel_offline_failure(error.last_error)
    return False


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
    delivery_id: str = ""


@dataclass(frozen=True, slots=True)
class _FetchedParentMessage:
    """仅保留官方父消息恢复所需的结构化、脱敏字段。"""

    message_id: str
    message_type: str
    text: str
    sender_type: str
    sender_ids: tuple[str, ...]
    chat_id: str
    created_at: int


@dataclass(frozen=True, slots=True)
class _ParentRecoveryResult:
    """父消息恢复结果；``detail`` 只用于用户可见的安全失败提示。"""

    code: str = ""
    detail: str = ""


def _decode_feishu_content(value: object) -> object | None:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _feishu_message_timestamp(value: object) -> int | None:
    """解析消息 get 接口常见的秒/毫秒时间，失败即拒绝恢复。"""

    if isinstance(value, bool) or value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        number = float(raw)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    if number < 0:
        return None
    # Feishu message timestamps are normally Unix milliseconds, while a few
    # compatible gateways expose seconds. Keep the conversion deterministic.
    if number >= 1_000_000_000_000_000:
        number /= 1_000_000
    elif number >= 1_000_000_000_000:
        number /= 1_000
    return int(number)


def _feishu_post_text(value: object) -> str | None:
    decoded = _decode_feishu_content(value)
    if not isinstance(decoded, Mapping):
        return None
    post = decoded.get("post") if isinstance(decoded.get("post"), Mapping) else decoded
    if not isinstance(post, Mapping):
        return None
    locale: object = post.get("zh_cn")
    if not isinstance(locale, Mapping):
        locale = next(
            (item for item in post.values() if isinstance(item, Mapping)), None
        )
    if not isinstance(locale, Mapping):
        return None
    rows = locale.get("content")
    if not isinstance(rows, list):
        return None
    lines: list[str] = []
    for row in rows:
        if not isinstance(row, list):
            return None
        parts: list[str] = []
        for node in row:
            if not isinstance(node, Mapping) or str(node.get("tag") or "") != "text":
                # The notification formatter only emits text nodes. Reject
                # links/mentions/cards rather than matching a partial body.
                return None
            text = node.get("text")
            if not isinstance(text, str):
                return None
            parts.append("" if text == "\u00a0" else text)
        lines.append("".join(parts))
    return "\n".join(lines)


def _extract_feishu_parent_message(
    raw: object, expected_message_id: str
) -> _FetchedParentMessage | None:
    if not isinstance(raw, Mapping):
        return None
    # The official SDK normally returns code=0, but gateways and error
    # envelopes may still contain a plausible ``data`` object.  Never accept
    # that object as proof of delivery.
    if raw.get("code") not in (None, 0, "", "0"):
        return None
    data = raw.get("data")
    item: Mapping[str, object] | None = None
    if isinstance(data, Mapping):
        items = data.get("items")
        if items is not None:
            # A get-by-ID response must identify exactly one message. Never
            # silently select the first item from a malformed/multi-item body.
            if (
                not isinstance(items, list)
                or len(items) != 1
                or not isinstance(items[0], Mapping)
            ):
                return None
            item = items[0]
        elif isinstance(data.get("message"), Mapping):
            item = data.get("message")  # type: ignore[assignment]
    if item is None and isinstance(raw.get("message_id"), str):
        item = raw  # type: ignore[assignment]
    if item is None:
        return None
    message_id = str(item.get("message_id") or item.get("id") or "").strip()
    if message_id != expected_message_id:
        return None
    message_type = str(item.get("message_type") or item.get("msg_type") or "").strip().casefold()
    if message_type not in {"text", "post"}:
        return None
    content = item.get("content")
    body = item.get("body")
    if content is None and isinstance(body, Mapping):
        content = body.get("content")
    decoded = _decode_feishu_content(content)
    if message_type == "text":
        if isinstance(decoded, Mapping):
            text = decoded.get("text")
            if not isinstance(text, str):
                return None
        elif isinstance(decoded, str):
            text = decoded
        else:
            return None
    else:
        text = _feishu_post_text(decoded)
        if text is None:
            return None

    sender = item.get("sender")
    if not isinstance(sender, Mapping):
        sender = {
            "sender_id": item.get("sender_id"),
            "sender_type": item.get("sender_type"),
        }
    sender_type = str(sender.get("sender_type") or sender.get("type") or "").strip().casefold()
    sender_id_value = sender.get("sender_id")
    sender_ids: list[str] = []
    if isinstance(sender_id_value, Mapping):
        for key in ("open_id", "user_id", "union_id", "app_id", "id"):
            value = str(sender_id_value.get(key) or "").strip()
            if value:
                sender_ids.append(value)
    else:
        value = str(sender_id_value or "").strip()
        if value:
            sender_ids.append(value)
    for key in ("open_id", "user_id", "union_id", "app_id", "id"):
        value = str(sender.get(key) or "").strip()
        if value:
            sender_ids.append(value)
    chat_id = str(item.get("chat_id") or item.get("conversation_id") or "").strip()
    created_at = _feishu_message_timestamp(item.get("create_time"))
    if not sender_type or not sender_ids or not chat_id or created_at is None:
        return None
    return _FetchedParentMessage(
        message_id=message_id,
        message_type=message_type,
        text=text,
        sender_type=sender_type,
        sender_ids=tuple(dict.fromkeys(sender_ids)),
        chat_id=chat_id,
        created_at=created_at,
    )


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
    if turn.status is ThreadStatus.COMPLETED and not turn.final_message.strip() and not turn.delivered_files and not turn.generated_images:
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
        final_answer_parts=turn.final_answer_parts,
        error_message=_error_text(turn.error_json),
        completed_at=turn.completed_at,
        generated_images=turn.generated_images,
        delivered_files=turn.delivered_files,
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


class _PollCycleTiming:
    """Slow-cycle diagnostics only; never acts as a service heartbeat."""

    PHASES = frozenset({'channel_health', 'monitor_registry', 'thread_selection',
        'notification_text_outbox', 'notification_media_outbox', 'hook_events',
        'thread_snapshots', 'offline_snapshots'})

    def __init__(self):
        self.started = self.last = time.monotonic()
        self.phase = 'channel_health'
        self.durations = []

    def enter(self, phase):
        if phase not in self.PHASES:
            raise ValueError('unknown polling phase')
        now = time.monotonic()
        self.durations.append((self.phase, max(0.0, now-self.last)))
        self.phase, self.last = phase, now

    def finish(self):
        now = time.monotonic()
        total = max(0.0, now-self.started)
        if total < 30:
            return
        phases = [*self.durations, (self.phase, max(0.0, now-self.last))]
        LOGGER.warning('poll_cycle_slow duration_seconds=%.3f phases=%s', total,
            ','.join(f'{name}:{duration:.3f}' for name, duration in phases))


class ProgressService:
    """单进程服务；主线程低频轮询，回复工作线程只在收到引用时运行。"""

    def __init__(self, config_path: str | os.PathLike[str]):
        self.config_source = ReloadingConfig(config_path)
        self.stop_event = threading.Event()
        self.reply_queue: queue.Queue[ReplyJob | None] = queue.Queue()
        self.receipt_queue: queue.Queue[ReplyReceiptJob | None] = queue.Queue()
        self.management_queue: queue.Queue[ChannelReply | None] = queue.Queue()
        # Unknown-result parent recovery performs the official message lookup
        # outside the SDK WebSocket callback thread.  Each item retains the
        # original staged-image flag so recovery cannot lose a valid attachment.
        self.parent_recovery_queue: queue.Queue[tuple[ChannelReply, bool] | None] = queue.Queue()
        self._reply_schedule_lock = threading.Lock()
        self._scheduled_reply_codes: set[str] = set()
        self._deferred_reply_codes: set[str] = set()
        self.config: AppConfig | None = None
        self.store: StateStore | None = None
        self.user_reply_chain: UserReplyChainStore | None = None
        self.codec: CorrelationCodec | None = None
        self.codex_store: CodexStore | None = None
        self.channel: MessageChannel | None = None
        self.summarizer: ProgressSummarizer | None = None
        self.reply_thread: threading.Thread | None = None
        self.receipt_thread: threading.Thread | None = None
        self.attention_thread: threading.Thread | None = None
        self.desktop_approval_thread: threading.Thread | None = None
        self.management_thread: threading.Thread | None = None
        self.approval_thread: threading.Thread | None = None
        self.summary_thread: threading.Thread | None = None
        self.file_delivery_queue = None
        self.notification_raw_thread: threading.Thread | None = None
        self.parent_recovery_thread: threading.Thread | None = None
        self.reset_alert_worker: ResetAlertWorker | None = None
        self.reset_alert_thread: threading.Thread | None = None
        self._summary_wakeup = threading.Event()
        self._notification_raw_wakeup = threading.Event()
        self._summary_event_lock = threading.RLock()
        self._summary_events: dict[str, TurnEvent] = {}
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
        self._channel_offline_reported = False
        self._channel_health_write_failed = False
        self._pending_lock = threading.RLock()
        self._pending_server_replies: dict[str, PendingServerReply] = {}
        self._active_rpc_lock = threading.RLock()
        self._active_rpc: CodexAppServer | None = None
        self._attention_session_lock = threading.RLock()
        self._active_attention_session: VerifiedDesktopAppTools | None = None

    def _start_reset_alert_worker(self) -> None:
        """启动唯一预警 worker；重复调用只更新配置，不创建第二线程。"""

        if self.config is None or self.store is None or self.channel is None:
            return
        if self.config.messaging.backend != "feishu":
            return
        if self.reset_alert_worker is None:
            self.reset_alert_worker = ResetAlertWorker(
                store=self.store,
                config=self.config.reset_alert,
                send_text=lambda text, key: self._send_channel_text(
                    text, idempotency_key=key
                ),
                is_online=lambda: (
                    self.channel is not None and self.channel.is_online()
                ),
                stop_event=self.stop_event,
            )
        else:
            self.reset_alert_worker.update_config(self.config.reset_alert)
        if self.reset_alert_thread is not None and self.reset_alert_thread.is_alive():
            return
        if self.stop_event.is_set():
            return
        self.reset_alert_thread = threading.Thread(
            target=self.reset_alert_worker.run,
            name="codex-reset-alerts",
            daemon=True,
        )
        self.reset_alert_thread.start()

    def _publish_channel_health(self, *, forced_state: str | None = None) -> None:
        """发布不含隐私的渠道运行态；失败不应拖停业务服务。"""

        if self.config is None:
            return
        if isinstance(self.channel, FeishuMessageChannel) or getattr(self.channel,"is_guardian_proxy",False):
            snapshot = self.channel.connection_snapshot()
        else:
            online = bool(self.channel is not None and self.channel.is_online())
            snapshot = {
                "state": "online" if online else "offline",
                "last_failure_class": "",
                "last_failure_type": "",
                "consecutive_failures": 0,
                "retry_in_seconds": 0.0,
                "ever_connected": online,
                "last_transition_at": time.time(),
            }
        if forced_state is not None:
            snapshot = dict(snapshot)
            snapshot["state"] = forced_state
            snapshot["retry_in_seconds"] = 0.0
        try:
            published = write_channel_health(
                self.config.service.pid_file,
                snapshot,
            )
            if published:
                self._channel_health_write_failed = False
        except (OSError, RuntimeError, ValueError):
            if not self._channel_health_write_failed:
                LOGGER.warning("渠道健康快照暂时无法写入；业务服务继续运行")
            self._channel_health_write_failed = True

    @property
    def wechat(self) -> MessageChannel | None:
        """兼容旧测试与扩展的别名；新代码统一使用 ``channel``。"""

        return self.channel

    @wechat.setter
    def wechat(self, value: MessageChannel | None) -> None:
        self.channel = value

    def _public_thread_record(
        self, record: ThreadRecord | None
    ) -> ThreadRecord | None:
        """把共享展示名投影给通知事件，不修改 Codex 自己的标题。"""

        if record is None or self.store is None:
            return record
        cached = self.store.thread_title_recovery(
            record.thread_id, thread_title_recovery_hash(record)
        )
        recovered = str(cached.get("display_title") or "") if cached else ""
        title, title_origin = public_thread_title(record, recovered)
        if title == record.title and title_origin == record.title_source:
            return record
        return replace(record, title=title, title_source=title_origin)

    def _public_event_title(self, event: TurnEvent) -> TurnEvent:
        if self.codex_store is None or not callable(
            getattr(self.codex_store, "get_thread", None)
        ):
            return event
        # 运行中的通知只来自当前可见监控任务；使用兼容旧扩展替身的默认调用，
        # 不要求所有 CodexStore 适配器都实现 include_archived 关键字。
        record = self.codex_store.get_thread(event.thread_id)
        self.codex_store.require_readable("刷新进度通知会话名称")
        public_record = self._public_thread_record(record)
        if public_record is None or public_record.title == event.title:
            return event
        return replace(event, title=public_record.title)

    def request_stop(self, *_args: object) -> None:
        self.stop_event.set()
        self._summary_wakeup.set()
        self._notification_raw_wakeup.set()
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

    def _send_channel_card(
        self,
        card: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> tuple[str, ...]:
        if self.channel is None:
            raise ServiceFatalError("消息渠道尚未初始化")
        method = getattr(self.channel, "send_card", None)
        if not callable(method):
            raise ServiceFatalError("当前消息渠道不支持交互卡片")
        result = method(card, idempotency_key=idempotency_key)
        if result is None:
            return ()
        values = (result,) if isinstance(result, str) else tuple(result)
        normalized = tuple(dict.fromkeys(str(item or "").strip() for item in values))
        if any(not item for item in normalized):
            raise ServiceFatalError("消息渠道返回了无效卡片 message_id")
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
        # The controlled StateStore migration creates this table.  Opening it
        # separately keeps the chain's lock local while retaining the same
        # SQLite/WAL durability boundary as reply_deliveries.
        self.user_reply_chain = UserReplyChainStore(config.service.database)
        recovered_reply_chain = self.user_reply_chain.reconcile_prepared()
        if recovered_reply_chain:
            LOGGER.info(
                "恢复用户引用链待入队记录：%d 条",
                len(recovered_reply_chain),
            )
        management_recovery = self.store.recover_interrupted_management_actions()
        if any(int(value) for value in management_recovery.values()):
            LOGGER.warning(
                "恢复上次中断的一次性管理动作：未提交已释放=%d，已提交结果未知=%d",
                int(management_recovery.get("unsubmitted_released", 0)),
                int(management_recovery.get("submitted_uncertain", 0)),
            )
        remote_recovery = self.store.recover_interrupted_remote_control_actions()
        if any(int(value) for value in remote_recovery.values()):
            LOGGER.warning(
                "恢复上次中断的远程控制动作：未提交已释放=%d，已提交结果未知=%d",
                int(remote_recovery.get("unsubmitted_released", 0)),
                int(remote_recovery.get("submitted_uncertain", 0)),
            )
        summary_recovery = self.store.recover_interrupted_notification_summaries()
        if any(int(value) for value in summary_recovery.values()):
            LOGGER.warning(
                "恢复上次中断的详细摘要投递：未提交已释放=%d，已提交结果未知=%d",
                int(summary_recovery.get("unsubmitted_released", 0)),
                int(summary_recovery.get("submitted_uncertain", 0)),
            )
        raw_recovery = self.store.recover_interrupted_notification_raw_deliveries()
        if any(int(value) for value in raw_recovery.values()):
            LOGGER.warning(
                "恢复上次中断的原文投递：未提交已释放=%d，已提交结果未知=%d",
                int(raw_recovery.get("unsubmitted_released", 0)),
                int(raw_recovery.get("submitted_uncertain", 0)),
            )
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
            guardian_generation = getattr(self,"guardian_generation",None)
            app_secret = None if guardian_generation else DpapiSecretStore(config.feishu.app_secret_file).load()
            if not guardian_generation and not app_secret:
                raise ServiceFatalError("飞书 App Secret 尚未安全保存")
            secret_identity_after = _file_identity(config.feishu.app_secret_file)
            if secret_identity_before != secret_identity_after:
                raise ServiceFatalError("读取期间飞书 App Secret 文件发生变化；请重新启动")
            self._initial_feishu_secret_identity = secret_identity_after
            if guardian_generation:
                from .guardian_channel import GuardianChannel
                self.channel = GuardianChannel(config,guardian_generation)
                self.channel.error_handler = self._on_channel_error
            else:
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
        if config.messaging.backend == "feishu":
            # Start this before opening the SDK WebSocket so an early inbound
            # callback can always hand unknown-parent recovery off without
            # waiting on its own asyncio loop.
            self.parent_recovery_thread = threading.Thread(
                target=self._parent_recovery_worker,
                name="progress-feishu-parent-recovery",
                daemon=True,
            )
            self.parent_recovery_thread.start()
        call_with_retry(
            "消息渠道启动",
            lambda: self.channel.start(self._on_channel_reply),
            self._policy(),
            sleep=self._retry_sleep,
            on_failure=self._on_retry("消息渠道启动"),
        )
        self._publish_channel_health()
        self._start_reset_alert_worker()
        self.summary_thread = threading.Thread(
            target=self._summary_worker,
            name="progress-notification-summaries",
            daemon=True,
        )
        self.summary_thread.start()
        if self.config.messaging.backend == "feishu":
            self._artifact_queue()
        self.notification_raw_thread = threading.Thread(
            target=self._notification_raw_worker,
            name="progress-notification-raw",
            daemon=True,
        )
        self.notification_raw_thread.start()
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
                send_card=lambda card, key: self._send_channel_card(
                    dict(card), idempotency_key=key
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
                session_search=SessionSearchEngine(
                    state=self.store,
                    codex_store=self.codex_store,
                    codex_home=config.codex.home,
                    summarizer=self.summarizer,
                    judge=LunaSemanticJudge(
                        config.summary.codex_command or config.codex.command,
                        timeout_seconds=config.summary.timeout_seconds,
                        retry_policy=self._policy(),
                        sleep=self._retry_sleep,
                    ),
                    project_registry=CodexProjectRegistry(
                        config.codex.home / ".codex-global-state.json",
                        config.codex.managed_project_root,
                    ),
                    summary_retry_policy=self._policy(),
                    summary_sleep=self._retry_sleep,
                ),
                remote_control=AppServerRemoteControl(
                    lambda: CodexAppServer(
                        discover_desktop_codex_command(config.codex.command),
                        timeout_seconds=30,
                        client_name="progress_wx_feishu_control",
                        experimental_api=True,
                    ),
                    # 只开放已在当前 Desktop bundled App Server 上用隔离
                    # thread 或可恢复专用任务真实验证过的官方方法。
                    # 未验证/已拒绝的能力仍在外部提交前失败关闭。
                    write_capabilities=VALIDATED_REMOTE_WRITE_CAPABILITIES,
                ),
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
        for delivery_id, fingerprint in self.store.pending_delivery_receipts():
            self._queue_delivered_reply_receipt(delivery_id, fingerprint)
        self.reply_thread = threading.Thread(target=self._reply_worker, name="progress-channel-replies", daemon=True)
        self.reply_thread.start()
        if config.codex.reply_transport == "desktop_app_tools":
            self.attention_thread = threading.Thread(
                target=self._attention_worker,
                name="progress-desktop-attention",
                daemon=True,
            )
            self.attention_thread.start()
            self.desktop_approval_thread=threading.Thread(target=self._desktop_approval_worker,name='progress-desktop-approval',daemon=True)
            self.desktop_approval_thread.start()
        for code, thread_id, reply_text, fingerprint in self.store.pending_turn_replies():
            self._enqueue_reply_job(ReplyJob(code, thread_id, reply_text, fingerprint))

    def _desktop_approval_worker(self) -> None:
        """Independent bounded reads; list/wait failures cannot gate this path."""
        from .desktop_approval_watch import DesktopApprovalWatch,is_approval
        assert self.config is not None and self.codex_store is not None
        watch=DesktopApprovalWatch(self.config.service.database.parent/'desktop-approval-observations.sqlite')
        client=DesktopAppToolsClient(self.config.codex.desktop_log_dir,connect_timeout=1.0,response_timeout=3.0)
        session=None;offset=0;records={};targets=();next_registry_refresh=0.0
        while not self.stop_event.is_set():
            try:
                if time.monotonic()>=next_registry_refresh:
                    records=self._selected_threads(self.config)
                    targets=tuple(sorted(records))
                    next_registry_refresh=time.monotonic()+15.0
                if not targets:
                    self.stop_event.wait(1);continue
                target=targets[offset % len(targets)];offset+=1
                if session is None:session=client.open_verified(required_tools=('read_thread',))
                snapshot=session.read_thread(target,target,turn_limit=1,include_outputs=False,max_output_chars_per_item=0,call_tag='approval-observe')
                # Reading a full local turn may scan rollout/artifact metadata.
                # Only do it to validate an actual waiting/resolution observation.
                need_local=is_approval(snapshot.get('thread',{}).get('status')) is True or watch.has_pending(target)
                local=self.codex_store.latest_turn(target) if need_local else None
                turns=snapshot.get('turns',[])
                turn=turns[0] if isinstance(turns,list) and turns and isinstance(turns[0],dict) else {}
                local_start=getattr(local,'started_at',None)
                remote_start=turn.get('startedAt')
                expected=None
                if isinstance(local_start,(int,float)) and isinstance(remote_start,(int,float)):
                    if local_start>1e12:local_start/=1000
                    if remote_start>1e12:remote_start/=1000
                    if local_start>=remote_start:expected=local.turn_id
                event=watch.observe(snapshot,target,records.get(target),expected_turn_id=expected)
                if event is None:
                    resolved=watch.resolved_event_key(target)
                    if resolved:self._sync_desktop_approval_notice(resolved)
                if event is not None and not self.store.was_processed(event.dedupe_key):
                    # Resolve again immediately before submitting a new alert.
                    fresh=session.read_thread(target,target,turn_limit=1,include_outputs=False,max_output_chars_per_item=0,call_tag='approval-presend')
                    current=watch.observe(fresh,target,records.get(target),expected_turn_id=event.raw['actual_turn_id'])
                    if current is not None and current.dedupe_key==event.dedupe_key and not self.stop_event.is_set():self._sync_desktop_approval_notice(event.dedupe_key,event)
                    elif current is None and watch.resolved_event_key(target):self._sync_desktop_approval_notice(event.dedupe_key)
            except ServiceStopping:
                break
            except Exception as exc:
                LOGGER.warning('Desktop approval observation/retry unavailable (%s)',type(exc).__name__)
                if session is not None:session.close();session=None
            self.stop_event.wait(0.5)
        if session is not None:session.close()

    def _sync_desktop_approval_notice(self,event_key,event=None):
        """Nonblocking guardian outbox; a submitted/unknown outcome is never reset."""
        if event is not None and self.approval_bridge is not None:
            if any(r.session_id==event.thread_id and r.turn_id==event.raw.get('actual_turn_id') for r in self.approval_bridge.pending()):
                event=None  # Existing actionable signed hook owns this wait.
        if not getattr(self.channel,'is_guardian_proxy',False):
            raise RuntimeError('desktop_approval_requires_guardian_outbox')
        transport=self.channel.store
        key='notification:'+event_key
        outcome=transport.outcome(key)
        if outcome and outcome['state']=='done':
            ids=json.loads(outcome['result'])
            ids=[ids] if isinstance(ids,str) else ids
            if not isinstance(ids,list) or not ids or not all(isinstance(i,str) and i for i in ids):raise ValueError('approval_missing_message_ids')
            self.store.bind_channel_messages(event_key,ids)
            self.store.mark_sent(event_key);self.store.mark_processed(event_key)
            return
        if event is None:
            with transport.lock,transport.db:
                transport.db.execute("UPDATE outgoing SET state='superseded',error='approval_resolved_before_submit',updated=? WHERE key=? AND state='pending'",(time.time(),key))
            return
        if self.store.notification_sent(event_key):return
        report=structural_report(event)
        code=self.codec.issue()
        message=self._format_event_notification(event,report,code,include_media_notice=False,include_quota=False)
        _,stored=self.store.reserve_notification(event,code,message,self.config.messaging.pending_ttl_hours,reply_kind='notice')
        if outcome is None:
            transport.enqueue(key,'send_text',{'text':stored})
        # Existing pending/submitted/rejected/uncertain/superseded stay owned by
        # guardian. No synchronous platform wait and no uncertain-state replay.

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
        if self.reset_alert_worker is not None:
            self.reset_alert_worker.update_config(config.reset_alert)
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
        # 固定菜单事件由飞书平台直接投递，官方 payload 不保证提供 chat_id。
        # 无论平台当前是否附带 chat_id，菜单都不是用户的图片/文字回复，绝不能
        # 与两步图片暂存合并；否则空 chat_id 会触发 StateStore 身份异常，而未来
        # 带 chat_id 的菜单又可能误吃掉同一私聊的待发图片。其它缺少会话身份的
        # 入站数据仍然拒绝，但只记脱敏告警，不得拖垮服务。
        normalized_sender_id = str(message.sender_id or "").strip()
        normalized_chat_id = str(message.chat_id or "").strip()
        if not normalized_sender_id or len(normalized_sender_id) > 512:
            LOGGER.warning("消息渠道入站被忽略：缺少有效发送者标识（来源已脱敏）")
            return None, False
        if message.source_kind == "bot_menu":
            if (
                message.attachments
                or message.reply_to_message_id
                or len(normalized_chat_id) > 512
            ):
                LOGGER.warning("固定菜单入站被忽略：载荷边界不合法（来源已脱敏）")
                return None, False
            return message, False
        if not normalized_chat_id:
            LOGGER.warning("消息渠道入站被忽略：缺少有效会话标识（来源已脱敏）")
            return None, False
        if len(normalized_chat_id) > 512:
            LOGGER.warning("消息渠道入站被忽略：会话标识无效（来源已脱敏）")
            return None, False
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
        # Control inputs must never consume a staged image as prompt text.
        # Only the two explicit image-stage commands operate on that staging.
        if message.content not in {'.发送','.取消'} and is_direct_management_candidate(message.content):
            return message, False
        staged = self.store.staged_image_reply(message.sender_id, message.chat_id)
        if staged is None:
            return message, False
        if message.content == ".取消":
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
            content="" if message.content == ".发送" else message.content,
            reply_to_message_id=reply_to,
            message_id=message.message_id,
            chat_id=message.chat_id,
            quote_content=message.quote_content,
            message_hash=message.message_hash,
            attachments=attachments,
        )
        return combined, True

    def _trusted_bot_sender_ids(self) -> tuple[str, ...]:
        """读取渠道已解析的 bot 身份；缺失时恢复必须 fail-closed。"""

        channel = self.channel
        if channel is None:
            return ()
        value = getattr(channel, "bot_sender_ids", None)
        try:
            value = value() if callable(value) else value
        except BaseException as exc:
            LOGGER.warning(
                "核验飞书父消息时 bot 身份暂不可读（异常类型=%s）",
                type(exc).__name__,
            )
            return ()
        if isinstance(value, str):
            value = (value,)
        if not isinstance(value, (tuple, list, set, frozenset)):
            return ()
        return tuple(
            dict.fromkeys(
                str(item or "").strip()
                for item in value
                if str(item or "").strip()
            )
        )

    def _recover_unknown_parent(
        self, message: ChannelReply
    ) -> _ParentRecoveryResult:
        """按精确父 message_id 恢复一个跨提交边界的摘要。

        所有平台证据都在这里变成最小结构后才进入状态事务。失败路径不
        消费任何 outbox，也不重发原摘要；仅返回安全的用户提示或空结果，
        让既有引用路由继续处理正常、有编号的消息。
        """

        if self.store is None or self.codec is None:
            return _ParentRecoveryResult()
        parent_id = str(message.reply_to_message_id or "").strip()
        if not parent_id:
            return _ParentRecoveryResult()
        try:
            candidates = self.store.notification_summary_recovery_candidates()
        except BaseException as exc:
            LOGGER.warning(
                "引用父消息恢复候选读取失败（异常类型=%s）",
                type(exc).__name__,
            )
            return _ParentRecoveryResult(
                detail="暂时无法核验这条被引用的机器人消息，请稍后重试。"
            )
        if not candidates:
            return _ParentRecoveryResult()
        fetcher = getattr(self.channel, "fetch_message", None)
        if not callable(fetcher):
            LOGGER.info("引用父消息恢复不可用：当前消息渠道没有官方取消息能力")
            return _ParentRecoveryResult(
                detail="暂时无法核验这条被引用的机器人消息，请稍后重试。"
            )
        try:
            raw = fetcher(parent_id)
            # The production wrapper is synchronous and waits from its own
            # worker thread. Do not ever try to synchronously drive an awaitable
            # returned by a callback-loop adapter; treating it as unavailable
            # is safer than risking an event-loop deadlock.
            if inspect.isawaitable(raw):
                try:
                    raw.close()  # type: ignore[attr-defined]
                except BaseException:
                    pass
                return _ParentRecoveryResult(
                    detail="暂时无法核验这条被引用的机器人消息，请稍后重试。"
                )
        except BaseException as exc:
            LOGGER.warning(
                "官方父消息查询失败（异常类型=%s）",
                type(exc).__name__,
            )
            return _ParentRecoveryResult(
                detail="暂时无法核验这条被引用的机器人消息，请稍后重试。"
            )
        fetched = _extract_feishu_parent_message(raw, parent_id)
        if fetched is None:
            LOGGER.info("拒绝恢复父消息：官方返回结构或消息 ID 不匹配")
            return _ParentRecoveryResult(
                detail="无法安全核验这条被引用的机器人消息，请重新查询后再发送。"
            )
        if fetched.sender_type not in {"app", "bot"}:
            LOGGER.info("拒绝恢复父消息：发送者类型不是本机 bot")
            return _ParentRecoveryResult(
                detail="无法安全核验这条被引用的机器人消息，请重新查询后再发送。"
            )
        trusted_ids = self._trusted_bot_sender_ids()
        if not trusted_ids or not set(fetched.sender_ids).intersection(trusted_ids):
            LOGGER.info("拒绝恢复父消息：发送者不是已解析的本机 bot")
            return _ParentRecoveryResult(
                detail="无法安全核验这条被引用的机器人消息，请重新查询后再发送。"
            )
        if not message.chat_id or fetched.chat_id != str(message.chat_id).strip():
            LOGGER.info("拒绝恢复父消息：chat 不一致")
            return _ParentRecoveryResult(
                detail="无法安全核验这条被引用的机器人消息，请重新查询后再发送。"
            )

        body_matches: list[NotificationSummaryRecoveryCandidate] = []
        for candidate in candidates:
            if not candidate.message_text:
                continue
            if not (
                candidate.submitted_at - PARENT_RECOVERY_CLOCK_SKEW_SECONDS
                <= fetched.created_at
                <= candidate.uncertain_at + PARENT_RECOVERY_CLOCK_SKEW_SECONDS
            ):
                continue
            if fetched.text != candidate.message_text:
                continue
            # Keep both the exact comparison and a digest check in the proof;
            # this prevents a future normalization change from silently turning
            # a partial/altered post into a match.
            if hashlib.sha256(fetched.text.encode("utf-8")).hexdigest() != hashlib.sha256(
                candidate.message_text.encode("utf-8")
            ).hexdigest():
                continue
            body_matches.append(candidate)
        valid_matches = [item for item in body_matches if self.codec.valid(item.code)]
        if len(body_matches) != 1 or len(valid_matches) != 1:
            reason = "没有候选" if not body_matches else "候选不唯一"
            LOGGER.info(
                "拒绝恢复父消息：正文/时间证明%s（候选=%d）",
                reason,
                len(body_matches),
            )
            return _ParentRecoveryResult(
                detail="无法安全核验这条被引用的机器人消息，请重新查询后再发送。"
            )
        candidate = valid_matches[0]
        try:
            recovered = self.store.recover_notification_summary_delivery(
                candidate.event_key,
                parent_id,
                chat_id=str(message.chat_id),
            )
        except (StateError, ValueError) as exc:
            LOGGER.warning(
                "摘要父消息恢复事务拒绝（异常类型=%s）",
                type(exc).__name__,
            )
            return _ParentRecoveryResult(
                detail="无法安全核验这条被引用的机器人消息，请重新查询后再发送。"
            )
        if not recovered:
            LOGGER.info("摘要父消息恢复事务未改变状态")
            return _ParentRecoveryResult(
                detail="无法安全核验这条被引用的机器人消息，请重新查询后再发送。"
            )
        LOGGER.info("已按官方父消息证据恢复一条摘要引用（候选=1）")
        return _ParentRecoveryResult(code=candidate.code)

    def _parent_recovery_is_needed(self, message: ChannelReply) -> bool:
        """在 SDK 回调线程只做读检查，判断是否需要移交恢复 worker。"""

        if self.store is None or not str(message.reply_to_message_id or "").strip():
            return False
        if message.source_kind == "bot_menu":
            return False
        if (
            self.config is not None
            and self.config.messaging.backend == "feishu"
            and message.sender_id != self.config.feishu.target_open_id
        ):
            # Do not spend an official read on an untrusted inbound sender.
            return False
        parent_id = str(message.reply_to_message_id).strip()
        try:
            # A user's own previously queued message is an exact local route,
            # even though it is not a notification_message_ids row.  Do not
            # send it to the unrelated result-unknown summary recovery path.
            chain_store = getattr(self, "user_reply_chain", None)
            if chain_store is not None and chain_store.get(parent_id) is not None:
                return False
            if self.store.code_for_channel_message(parent_id):
                return False
            # A known management/raw context is already an authoritative local
            # route.  An unrelated uncertain summary must never cause us to
            # fetch an arbitrary parent message for it.
            raw_exists = getattr(
                self.store, "notification_raw_context_exists_for_message", None
            )
            if callable(raw_exists):
                if raw_exists(parent_id):
                    return False
            elif self.store.notification_raw_context_for_message(parent_id) is not None:
                return False
            management_exists = getattr(
                self.store, "management_context_exists_for_message", None
            )
            if callable(management_exists) and management_exists(parent_id):
                return False
            if self.management is not None and self.management.accepts(message):
                return False
        except BaseException:
            # Mapping state is part of the security boundary.  If it cannot be
            # read, fail closed here; do not let an unrelated uncertain summary
            # trigger an official fetch for a possibly-known management/raw ID.
            return False
        if CorrelationCodec.extract(message.quote_content):
            return False
        if not callable(getattr(self.channel, "fetch_message", None)):
            return False
        try:
            return bool(self.store.notification_summary_recovery_candidates(limit=1))
        except BaseException:
            # Let the worker produce a safe unavailable receipt rather than
            # synchronously touching the official SDK from the callback.
            return True

    def _enqueue_parent_recovery(
        self, message: ChannelReply, used_staged_images: bool
    ) -> bool:
        thread = getattr(self, "parent_recovery_thread", None)
        if thread is None or not thread.is_alive():
            return False
        if not self._parent_recovery_is_needed(message):
            return False
        self.parent_recovery_queue.put((message, used_staged_images))
        return True

    def _parent_recovery_worker(self) -> None:
        """在非 SDK 回调线程取父消息；单项失败不得拖停主服务。"""

        while True:
            item = self.parent_recovery_queue.get()
            if item is None:
                return
            if self.stop_event.is_set():
                continue
            message, used_staged_images = item
            try:
                # Recover before image staging.  A pure image reply to an
                # unknown post must not be rejected by _image_stage_target_is_valid
                # before the official parent proof has installed its alias.
                recovery = self._recover_unknown_parent(message)
                if recovery.detail:
                    self._queue_reply_receipt(
                        received=False,
                        details=recovery.detail,
                        fingerprint=hashlib.sha256(
                            f"{message.message_id}|parent-recovery".encode("utf-8")
                        ).hexdigest(),
                    )
                    continue
                prepared, used_after_prepare = self._prepare_staged_image_reply(message)
                if prepared is None:
                    continue
                if self.management is not None and self.management.accepts(prepared):
                    self.management_queue.put(prepared)
                    accepted = True
                else:
                    # The proof (when present) has already installed the local
                    # alias.  Skip a second fetch; a missing proof falls through
                    # to the normal fail-closed missing-parent receipt.
                    accepted = self._process_channel_reply(
                        prepared, _skip_parent_recovery=True
                    )
                if used_after_prepare and accepted and self.store is not None:
                    self.store.clear_staged_image_reply(
                        prepared.sender_id, prepared.chat_id
                    )
            except BaseException as exc:
                # A failed official lookup is recoverable and must never become
                # the process-wide fatal flag. The state transaction remains
                # untouched unless the proof completed atomically.
                LOGGER.exception(
                    "引用父消息恢复后台处理失败（异常类型=%s）",
                    type(exc).__name__,
                )

    def _on_channel_reply(self, message: ChannelReply) -> bool:
        """处理渠道入站；返回值表示是否已经达到 guardian durable ACK 边界。"""

        try:
            # This check must precede image staging.  The first half of a
            # split image reply has no locally-known alias when the original
            # summary crossed the external submit boundary with an unknown
            # result, so staging would otherwise reject it permanently.
            if self._enqueue_parent_recovery(message, False):
                return False
            # If shutdown raced with an inbound SDK callback, never fall back
            # to _recover_unknown_parent here: FeishuMessageChannel.fetch_message
            # is a synchronous bridge to this same SDK loop and would deadlock.
            # Known local aliases remain routable; an unknown parent receives a
            # safe retry receipt until the service is healthy again.
            recovery_worker = getattr(self, "parent_recovery_thread", None)
            recovery_worker_alive = bool(
                recovery_worker is not None and recovery_worker.is_alive()
            )
            fetch_available = callable(
                getattr(getattr(self, "channel", None), "fetch_message", None)
            )
            if (
                self.config is not None
                and self.config.messaging.backend == "feishu"
                and fetch_available
                and not recovery_worker_alive
                and self._parent_recovery_is_needed(message)
            ):
                self._queue_reply_receipt(
                    received=False,
                    details="暂时无法核验这条被引用的机器人消息，请稍后重试。",
                    fingerprint=hashlib.sha256(
                        f"{message.message_id}|parent-recovery-worker-unavailable".encode(
                            "utf-8"
                        )
                    ).hexdigest(),
                )
                return False
            prepared, used_staged_images = self._prepare_staged_image_reply(message)
            if prepared is None:
                return False
            message = prepared
            if self.management is not None and self.management.accepts(message):
                status_reader = getattr(self.store, "management_inbound_status", None)
                reserve = getattr(self.store, "reserve_management_inbound", None)
                if callable(status_reader):
                    try:
                        status = status_reader(
                            message.message_id,
                            sender_id=message.sender_id,
                            content=message.content,
                        )
                    except (StateError, ValueError):
                        LOGGER.warning("管理入站状态无法核验，保留 guardian 消息待重试")
                        return False
                    if status == "conflict":
                        LOGGER.warning("拒绝复用冲突的管理入站 message_id")
                        return False
                    if status == "accepted":
                        return True
                    if status == "pending":
                        # The prior generation reserved the row but may have
                        # crashed before completion.  Requeue it for the
                        # serial management worker; it will replay the exact
                        # sender/content after checking the durable row.
                        self.management_queue.put(message)
                        return False
                    if status != "missing":
                        return False
                if not callable(reserve):
                    # Lightweight test/extension stores predating the
                    # management-inbound schema can still receive the item,
                    # but this path deliberately remains non-ACK-able for
                    # guardian until a real StateStore is installed.
                    LOGGER.warning("管理入站缺少持久占用接口，保留 guardian 消息")
                    self.management_queue.put(message)
                    if used_staged_images and self.store is not None:
                        self.store.clear_staged_image_reply(
                            message.sender_id, message.chat_id
                        )
                    return False
                try:
                    reserved = reserve(
                        message.message_id,
                        message.sender_id,
                        message.content,
                    )
                except (StateError, ValueError):
                    LOGGER.warning("管理入站持久占用失败，保留 guardian 消息待重试")
                    return False
                if not reserved:
                    try:
                        status = status_reader(
                            message.message_id,
                            sender_id=message.sender_id,
                            content=message.content,
                        )
                    except (StateError, ValueError):
                        return False
                    if status == "accepted":
                        return True
                    if status == "conflict":
                        return False
                self.management_queue.put(message)
                if used_staged_images and self.store is not None:
                    self.store.clear_staged_image_reply(
                        message.sender_id, message.chat_id
                    )
                # The reservation is durable, but completed_at is written
                # only after the management worker finishes.  Guardian must
                # poll status before acknowledging this event.
                return False
            # The production Feishu callback runs on the SDK event loop. Once
            # the dedicated recovery worker exists, never synchronously call
            # the wrapper's future.result() from this callback, even when the
            # cheap candidate check says recovery is unnecessary.
            skip_recovery = recovery_worker_alive
            if (
                self.config is not None
                and self.config.messaging.backend == "feishu"
                and fetch_available
            ):
                # A live Feishu service always starts the recovery worker before
                # opening the SDK.  If it is absent/dead, the guard above has
                # already handled unknown candidates; skip the synchronous
                # bridge for all remaining local routes as well.
                skip_recovery = True
            accepted = self._process_channel_reply(
                message, _skip_parent_recovery=skip_recovery
            )
            if used_staged_images and accepted and self.store is not None:
                self.store.clear_staged_image_reply(message.sender_id, message.chat_id)
            return bool(accepted)
        except BaseException as exc:
            # 第三方回调线程的异常必须传回主循环，不能静默杀死监听线程。
            LOGGER.exception("消息渠道引用回复处理失败：%s", type(exc).__name__)
            if self._fatal is None:
                self._fatal = exc
            self.stop_event.set()
            self._close_active_rpc()
            return False

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
            except FeishuSendRejectedError as exc:
                # 渠道已经明确拒绝出站消息；这不是 Codex Desktop 故障，而且
                # 再发送一条“Desktop 不可用”只会制造误导或重复失败。
                LOGGER.warning(
                    "Codex 飞书管理出站消息被明确拒绝（分类=%s，错误码=%s）",
                    exc.code,
                    exc.raw_code,
                )
            except (FeishuSendError, MessageChannelOfflineError) as exc:
                # 结果未知时绝不能再盲发故障回执；离线时也没有可靠回执通道。
                LOGGER.warning(
                    "Codex 飞书管理出站消息暂不可用（异常类型=%s）",
                    type(exc).__name__,
                )
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
        thread = self._public_thread_record(thread)
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
        # FeishuMessageChannel 对已识别的临时网络故障会自行监督重连；
        # 这里保留防御分支，避免旧/替换适配器把一次可恢复离线升级成
        # 全局 fatal。只有明确未知/永久故障才能停止服务。
        if _is_channel_offline_failure(error):
            LOGGER.warning("消息渠道暂时离线，服务保持运行并等待通道恢复")
            return
        if self._fatal is None:
            self._fatal = error
        self.stop_event.set()
        self._close_active_rpc()

    def _on_wechat_error(self, error: BaseException) -> None:
        """旧微信测试/扩展的兼容入口。"""

        self._on_channel_error(error)

    def _process_notification_raw_request(
        self, message: ChannelReply
    ) -> bool | None:
        """截获完成总结上的精确 ``.原文``；其它正文返回 ``None``。"""

        if message.content.strip() != ".原文":
            return None
        if self.store is None:
            return False
        fingerprint = hashlib.sha256(
            (
                "notification-raw-request-v1\0"
                f"{message.sender_id}\0{message.chat_id}\0{message.message_id}\0"
                f"{message.reply_to_message_id}\0.原文"
            ).encode("utf-8")
        ).hexdigest()
        if message.source_kind != "message":
            self._queue_reply_receipt(
                received=False,
                details="无法获取原文：请在私聊中引用一条任务完成总结，并发送“.原文”。",
                fingerprint=fingerprint,
            )
            return True
        if message.attachments or message.attachment_error:
            self._queue_reply_receipt(
                received=False,
                details="无法获取原文：“.原文”必须单独发送，不能同时附带图片或文件。",
                fingerprint=fingerprint,
            )
            return True
        if (
            not message.reply_to_message_id
            or not message.message_id
            or not message.sender_id
            or not message.chat_id
        ):
            self._queue_reply_receipt(
                received=False,
                details="无法获取原文：请引用一条任务完成总结后再发送“.原文”。",
                fingerprint=fingerprint,
            )
            return True
        context = self.store.notification_raw_context_for_message(
            message.reply_to_message_id
        )
        if context is None:
            context, legacy_error = self._materialize_legacy_notification_raw_context(
                message
            )
            if context is None:
                self._queue_reply_receipt(
                    received=False,
                    details=(
                        legacy_error
                        or "无法获取原文：被引用的消息不是可回看原文的任务完成总结，"
                        "或其持久关联已经归档。"
                    ),
                    fingerprint=fingerprint,
                )
                return True
        if (
            context.sender_id != message.sender_id
            or context.chat_id != message.chat_id
        ):
            self._queue_reply_receipt(
                received=False,
                details="无法获取原文：这条完成总结不属于当前私聊。",
                fingerprint=fingerprint,
            )
            return True
        try:
            delivery = self.store.reserve_notification_raw_delivery(
                context,
                inbound_message_id=message.message_id,
                fingerprint=fingerprint,
            )
        except (StateError, ValueError):
            self._queue_reply_receipt(
                received=False,
                details="无法接收：同一条飞书消息出现了冲突内容，请重新发送。",
                fingerprint=fingerprint,
            )
            return True
        if delivery.is_new:
            self._notification_raw_wakeup.set()
        else:
            LOGGER.info("忽略已持久接收的重复原文请求")
        return True

    def _materialize_legacy_notification_raw_context(
        self, message: ChannelReply
    ) -> tuple[NotificationRawContext | None, str | None]:
        """按需升级 schema21 部署前已经发出的精确 completed 通知。"""

        assert self.store is not None
        source = self.store.notification_raw_legacy_source_for_message(
            message.reply_to_message_id
        )
        if source is None:
            return None, None
        if (
            self.config is None
            or self.codex_store is None
            or self.config.messaging.backend != "feishu"
            or message.sender_id != self.config.feishu.target_open_id
            or not message.chat_id
        ):
            return None, "无法获取原文：这条旧完成总结不属于当前私聊。"
        getter = getattr(self.codex_store, "get_turn", None)
        if not callable(getter):
            return None, "无法获取原文：当前 Codex 数据源不支持精确读取该轮回复。"
        try:
            turn = getter(source.thread_id, source.turn_id)
            require_readable = getattr(self.codex_store, "require_readable", None)
            if callable(require_readable):
                require_readable("按需升级旧完成总结原文身份")
        except CodexStoreReadError:
            return None, "暂时无法读取这条旧完成总结对应的 Codex 记录，请稍后重试。"
        if turn is None or not turn.final_message.strip():
            return (
                None,
                "无法获取原文：这条旧完成总结对应的精确 Codex 轮次已被清理、"
                "损坏或当前不可读。系统没有改用其它轮次代替。",
            )
        if (
            turn.thread_id != source.thread_id
            or turn.turn_id != source.turn_id
            or turn.status is not ThreadStatus.COMPLETED
        ):
            return (
                None,
                "无法获取原文：被引用的旧消息没有对应到一轮精确且已完成的 "
                "Codex 回复。",
            )
        digest = hashlib.sha256(turn.final_message.encode("utf-8")).hexdigest()
        try:
            return (
                self.store.materialize_notification_raw_legacy_context(
                    source,
                    sender_id=message.sender_id,
                    chat_id=message.chat_id,
                    content_sha256=digest,
                ),
                None,
            )
        except (StateError, ValueError):
            return (
                None,
                "无法获取原文：这条旧完成总结的安全绑定已变化或属于另一私聊。",
            )

    def _process_channel_reply(
        self,
        message: ChannelReply,
        *,
        _skip_parent_recovery: bool = False,
    ) -> bool:
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
        if (
            not _skip_parent_recovery
            and message.reply_to_message_id
            and self.store.code_for_channel_message(message.reply_to_message_id) is None
            and CorrelationCodec.extract(message.quote_content) is None
        ):
            recovery = self._recover_unknown_parent(message)
            if recovery.code:
                # The atomic state transition has installed the exact parent
                # alias; the normal raw/turn route below now resolves by ID.
                pass
            elif recovery.detail:
                self._queue_reply_receipt(
                    received=False,
                    details=recovery.detail,
                    fingerprint=hashlib.sha256(
                        f"{message.message_id}|parent-recovery".encode("utf-8")
                    ).hexdigest(),
                )
                return False
        raw_handled = self._process_notification_raw_request(message)
        if raw_handled is not None:
            return raw_handled
        if is_dot_command(message.content):
            self._queue_reply_receipt(
                received=False,
                details='这条机器人指令未进入有效操作入口，没有作为 Codex 正文发送。请发送“.功能中心”查看入口。',
                fingerprint=hashlib.sha256(f'{message.message_id}|unhandled-dot-command'.encode('utf-8')).hexdigest(),
            )
            return False
        chain_store = getattr(self, "user_reply_chain", None)
        chain_route = None
        chain_parent_status = "missing"
        if chain_store is not None and message.reply_to_message_id:
            try:
                chain_resolution = chain_store.inspect_parent(
                    message.reply_to_message_id,
                    sender_id=message.sender_id,
                    chat_id=message.chat_id,
                )
                chain_parent_status = chain_resolution.status
                if chain_resolution.status == "ready":
                    chain_route = chain_resolution.record
                elif chain_resolution.status != "missing":
                    # Once an ID is known as a user message, never fall back
                    # to the current chat, quoted prose, or another task.  A
                    # prepared-but-not-queued row remains explicitly retryable
                    # after the queue writer/recovery worker finishes.
                    self._queue_reply_receipt(
                        received=False,
                        details=(
                            "无法继续：被引用的用户消息尚未可靠入队、已过期、"
                            "属于另一私聊，或其任务关联已失效。请引用原机器人进度消息重试。"
                        ),
                        fingerprint=hashlib.sha256(
                            f"{message.message_id}|user-reply-chain|{chain_resolution.status}".encode(
                                "utf-8"
                            )
                        ).hexdigest(),
                    )
                    return False
            except Exception as exc:
                LOGGER.warning(
                    "用户引用链核验失败（异常类型=%s）",
                    type(exc).__name__,
                )
                self._queue_reply_receipt(
                    received=False,
                    details="无法安全核验被引用的用户消息，请引用原机器人进度消息重试。",
                    fingerprint=hashlib.sha256(
                        f"{message.message_id}|user-reply-chain|error".encode("utf-8")
                    ).hexdigest(),
                )
                return False
        code_by_id = self.store.code_for_channel_message(message.reply_to_message_id)
        raw_context = (
            self.store.notification_raw_context_for_message(message.reply_to_message_id)
            if message.reply_to_message_id
            else None
        )
        if raw_context is not None and (
            raw_context.sender_id != message.sender_id
            or raw_context.chat_id != message.chat_id
        ):
            LOGGER.warning("拒绝跨用户或跨聊天范围引用原文消息")
            return False
        code_by_text = CorrelationCodec.extract(message.quote_content)
        if (
            chain_store is not None
            and message.reply_to_message_id
            and chain_parent_status == "missing"
            and not code_by_id
            and code_by_text
        ):
            # An exact platform parent ID that cannot be proved locally must
            # not be bypassed by a copied correlation token in quoted prose.
            self._queue_reply_receipt(
                received=False,
                details="无法安全核验被引用的消息，请引用原机器人进度消息重试。",
                fingerprint=hashlib.sha256(
                    f"{message.message_id}|unknown-exact-parent".encode("utf-8")
                ).hexdigest(),
            )
            return False
        if chain_route is not None:
            if code_by_id and code_by_id != chain_route.parent_code:
                LOGGER.warning("拒绝用户引用链与平台父消息通知编号不一致的回复")
                return False
            if code_by_text and code_by_text != chain_route.parent_code:
                LOGGER.warning("拒绝用户引用链与引用正文通知编号不一致的回复")
                return False
            code_by_id = chain_route.parent_code
        if code_by_id and code_by_text and code_by_id != code_by_text:
            LOGGER.warning("拒绝平台消息 ID 与通知编号不一致的引用回复")
            return False
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
            if message.reply_to_message_id and content:
                self._queue_reply_receipt(
                    received=False,
                    details=(
                        "无法继续：被引用的机器人消息已归档、关联已失效，"
                        "或不是可回复的进度汇报。请重新查询会话后再发送。"
                    ),
                    fingerprint=hashlib.sha256(
                        f"{message.message_id}|missing-parent".encode("utf-8")
                    ).hexdigest(),
                )
            return False
        # 部分 wxauto 版本可能不提供消息 id/hash；加入通知编号可避免不同轮次
        # 使用相同回复正文时被全局唯一指纹误判为重放。
        raw_identity = (
            f"{message.sender_id}|{message.chat_id}|{message.message_id}|"
            f"{message.message_hash}|{message.reply_to_message_id}|{code}|{content}"
        )
        fingerprint = hashlib.sha256(raw_identity.encode("utf-8")).hexdigest()
        # 持有 pending 锁直到检查和消费完成，避免 worker 同时超时清理连接。
        delivery = None
        chain_prepared = None
        chain_durable = True
        with self._pending_lock:
            route_status, mapping = self.store.inspect_reply_route(code, self.codec)
            if route_status != "ready" or mapping is None:
                details = {
                    "expired": "无法继续：这条进度汇报已过期，请重新查询会话后再发送。",
                    "consumed": "无法继续：这是一条一次性审批或等待消息，并且已经处理过。",
                    "missing": "无法继续：这条机器人消息的关联记录已归档或不存在。",
                    "invalid": "无法继续：这条机器人消息的关联签名无效。",
                }.get(route_status, "无法继续：这条机器人消息当前不可用。")
                LOGGER.warning("拒绝不可用的引用回复（状态=%s）", route_status)
                self._queue_reply_receipt(
                    received=False,
                    details=details,
                    fingerprint=fingerprint,
                )
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
                getter = getattr(self.codex_store, "get_thread", None)
                thread = getter(mapping[0]) if callable(getter) else None
                if callable(getter) and (
                    thread is None or bool(getattr(thread, "archived", False))
                ):
                    self._queue_reply_receipt(
                        received=False,
                        details=(
                            "无法继续：对应的 Codex 会话已经归档到不可访问位置、"
                            "已删除或当前记录损坏。请重新查询会话。"
                        ),
                        fingerprint=fingerprint,
                    )
                    return False
            elif mapping[2] == "notice":
                self._queue_reply_receipt(received=False,details='这是一条仅提醒的审批通知，请在 Codex 原任务审批框操作；没有批准或转交本条回复。',fingerprint=fingerprint)
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
            if mapping[2] == "turn":
                if chain_store is not None and message.reply_to_message_id:
                    try:
                        # This is the first phase of the durable hand-off.  It
                        # proves the exact bot/user parent, sender/chat scope,
                        # and target before the normal outbox write below.  A
                        # prepared row is intentionally not routable until
                        # complete_queued_reply observes that write.
                        chain_prepared = chain_store.prepare_reply(
                            inbound_message_id=message.message_id,
                            sender_id=message.sender_id,
                            chat_id=message.chat_id,
                            quoted_message_id=message.reply_to_message_id,
                            parent_code=code,
                            reply_fingerprint=fingerprint,
                            content_digest=user_reply_content_hash(content),
                        )
                    except Exception as exc:
                        LOGGER.warning(
                            "拒绝无法建立持久用户引用链（异常类型=%s）",
                            type(exc).__name__,
                        )
                        self._queue_reply_receipt(
                            received=False,
                            details=(
                                "无法安全接收这条引用回复：父消息或任务关联未能可靠核验。"
                                "请引用原机器人进度消息重试。"
                            ),
                            fingerprint=fingerprint,
                        )
                        return False
                try:
                    delivery = self.store.enqueue_turn_reply(
                        code,
                        message.message_id,
                        fingerprint,
                        self.codec,
                        reply_text=content,
                        receipt_required=(
                            self.config is not None
                            and self.config.messaging.backend == "feishu"
                        ),
                    )
                except StateError as exc:
                    LOGGER.warning("拒绝入站 message_id 冲突的引用回复：%s", exc)
                    if chain_prepared is not None and chain_store is not None:
                        try:
                            chain_store.abort_prepared_reply(
                                message.message_id, error_code="queue_conflict"
                            )
                        except UserReplyChainError:
                            # Preserve a recoverable prepared row if the
                            # abort itself cannot obtain the SQLite lock.
                            LOGGER.warning("用户引用链冲突记录暂未能关闭，保留待恢复")
                    self._queue_reply_receipt(
                        received=False,
                        details="无法接收：同一条飞书消息出现了冲突内容，请重新发送。",
                        fingerprint=fingerprint,
                    )
                    return False
                if delivery is None:
                    if chain_prepared is not None and chain_store is not None:
                        try:
                            chain_store.abort_prepared_reply(
                                message.message_id, error_code="queue_unavailable"
                            )
                        except UserReplyChainError:
                            LOGGER.warning("用户引用链失效记录暂未能关闭，保留待恢复")
                    self._queue_reply_receipt(
                        received=False,
                        details="无法继续：进度汇报已在接收过程中失效，请重新查询会话。",
                        fingerprint=fingerprint,
                    )
                    return False
                if chain_prepared is not None and chain_store is not None:
                    try:
                        chain_store.complete_queued_reply(
                            message.message_id,
                            delivery_id=delivery.delivery_id,
                        )
                    except Exception as exc:
                        # The outbox row is already durable.  Keep the
                        # prepared link for startup reconciliation instead of
                        # aborting it and losing the exact continuation route.
                        # Keep the guardian event pending; the next service
                        # start (or a later reconcile poll) will complete it.
                        LOGGER.warning(
                            "普通回复已入队但用户引用链尚未完成（异常类型=%s）",
                            type(exc).__name__,
                        )
                        chain_durable = False
                if not delivery.is_new:
                    LOGGER.info("忽略已持久接收的重复飞书回复事件")
                    return chain_durable
                consumed = mapping
            else:
                consumed = self.store.consume_reply(
                    code,
                    fingerprint,
                    self.codec,
                    reply_text=content,
                )
                if consumed is None:
                    LOGGER.warning("拒绝已过期、已消费或签名无效的一次性引用回复")
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
        assert delivery is not None
        if self._enqueue_reply_job(
            ReplyJob(
                delivery.delivery_id,
                delivery.thread_id,
                delivery.reply_text,
                delivery.fingerprint,
            )
        ):
            self._queue_reply_receipt(
                received=True,
                details=(
                    "已排队：你的回复已经安全保存，将按发送顺序追加到原 Codex 会话。"
                    "你可以继续引用同一条进度汇报追加需求；真正送达后会再收到“已追加”提示。"
                ),
                fingerprint=fingerprint,
            )
        return chain_durable

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

    def guardian_inbound_status(self, message_id: str) -> str:
        """Return the durable ACK state for a guardian-delivered inbound ID.

        The guardian may hand the same event to several worker generations.
        This read-only query is the worker's ACK contract: only a completed
        management record or a fully materialized ordinary reply outbox row is
        ``accepted``.  A user-reply-chain row remains ``pending`` until its
        second phase reaches ``queued`` and proves the same outbox row.  Unknown
        IDs are also pending so a transient callback failure cannot be mistaken
        for durable receipt.  The guardian owns the eventual retention/expiry
        decision for IDs that never acquire evidence.
        """

        identifier = str(message_id or "").strip()
        if not identifier:
            raise ValueError("message_id 不能为空")
        store = getattr(self, "store", None)
        if store is None:
            return "pending"
        statuses: list[str] = []
        chain_store = getattr(self, "user_reply_chain", None)
        if chain_store is not None:
            try:
                chain_status = chain_store.durable_ack_status(identifier)
            except Exception as exc:
                # A locked/unreadable chain database is not proof of a bad
                # inbound message.  Keep the guardian payload until a later
                # poll or worker generation can read it.
                LOGGER.warning(
                    "用户引用链 ACK 状态暂不可读，保留 pending（异常类型=%s）",
                    type(exc).__name__,
                )
                chain_status = "pending"
            if chain_status != "missing":
                statuses.append(chain_status)
        management_status_reader = getattr(store, "management_inbound_status", None)
        if callable(management_status_reader):
            try:
                management_status = management_status_reader(identifier)
            except Exception as exc:
                LOGGER.warning(
                    "管理入站 ACK 状态暂不可读，保留 pending（异常类型=%s）",
                    type(exc).__name__,
                )
                management_status = "pending"
            if management_status != "missing":
                statuses.append(str(management_status))
        delivery_status_reader = getattr(store, "reply_delivery_status", None)
        if callable(delivery_status_reader):
            try:
                delivery_status = delivery_status_reader(identifier)
            except Exception as exc:
                LOGGER.warning(
                    "普通回复 ACK 状态暂不可读，保留 pending（异常类型=%s）",
                    type(exc).__name__,
                )
                delivery_status = "pending"
            if delivery_status != "missing":
                statuses.append(str(delivery_status))
        if not statuses:
            return "pending"
        # Pending evidence always wins over an unrelated accepted row.  This
        # keeps the chain's prepared->queued crash window fail-closed.  Any
        # explicit rejection/conflict also wins, because ACKing a conflicting
        # identity would permanently discard a possible retry.
        if "rejected" in statuses or "conflict" in statuses:
            return "rejected"
        if "pending" in statuses:
            return "pending"
        return "accepted" if all(item == "accepted" for item in statuses) else "pending"

    def _queue_delivered_reply_receipt(
        self, delivery_id: str, fingerprint: str
    ) -> None:
        """持久子投递成功后，排队发送可重启恢复的“已追加”回执。"""

        if self.config is None or self.config.messaging.backend != "feishu":
            return
        self.receipt_queue.put(
            ReplyReceiptJob(
                received=True,
                details=(
                    "已追加：这条回复已经送达原 Codex 会话。"
                    "你仍可以继续引用同一条进度汇报追加下一条需求。"
                ),
                idempotency_key=f"reply-delivered:{delivery_id}",
                delivery_id=delivery_id,
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
                if job.delivery_id:
                    if self.store is None or not self.store.mark_delivery_receipt_sent(
                        job.delivery_id
                    ):
                        raise ServiceFatalError("已追加回执发送成功，但持久状态无法确认")
                LOGGER.info("飞书回复回执已发送")
            except ServiceStopping:
                return
            except BaseException as exc:
                if _is_channel_offline_failure(exc):
                    # 回执尚未得到任何飞书 message_id；保留同一幂等键并
                    # 延后重试，绝不因为暂时断网终止主服务。
                    config = self.config
                    configured = (
                        config.service.retry_delays[-1]
                        if config is not None and config.service.retry_delays
                        else 5.0
                    )
                    delay = max(0.5, min(float(configured), 30.0))
                    LOGGER.warning(
                        "飞书回复回执暂未发送，%.1f 秒后保留原任务重试（异常类型=%s）",
                        delay,
                        type(exc).__name__,
                    )
                    if self.stop_event.wait(delay):
                        return
                    self.receipt_queue.put(job)
                    continue
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
            while not self.stop_event.is_set():
                try:
                    self._deliver_reply(job)
                    self._queue_delivered_reply_receipt(job.code, job.fingerprint)
                    self._finish_reply_job(job.code)
                    break
                except ReplyDeferred as exc:
                    # 原地等待同一持久任务，后续回复不能越过它，保证严格顺序。
                    if job.code not in self._deferred_reply_codes:
                        LOGGER.warning(
                            "Codex 引用回复暂不可投递；已持久化等待重试（原因=%s）",
                            exc,
                        )
                        self._deferred_reply_codes.add(job.code)
                    config = self.config
                    delay = config.service.poll_seconds if config is not None else 2.0
                    self.stop_event.wait(max(0.2, min(float(delay), 5.0)))
                except ReplyCannotContinue as exc:
                    if self.store is None or not self.store.discard_turn_reply(job.code):
                        fatal = ServiceFatalError("不可继续的回复无法安全写入丢弃状态")
                        LOGGER.exception("消息渠道引用回复丢弃失败：%s", type(fatal).__name__)
                        self._fatal = fatal
                        self.stop_event.set()
                        return
                    self._queue_reply_receipt(
                        received=False,
                        details=str(exc),
                        fingerprint=job.fingerprint,
                    )
                    self._finish_reply_job(job.code)
                    break
                except ServiceStopping:
                    return
                except BaseException as exc:
                    if _is_channel_offline_failure(exc):
                        # 该错误只可能发生在渠道未提交前；保留原 job，
                        # 避免重启/恢复时丢失用户回复或形成忙循环。
                        if job.code not in self._deferred_reply_codes:
                            LOGGER.warning(
                                "消息渠道暂时离线，引用回复保留并退避重试（异常类型=%s）",
                                type(exc).__name__,
                            )
                            self._deferred_reply_codes.add(job.code)
                        config = self.config
                        configured = (
                            config.service.retry_delays[-1]
                            if config is not None and config.service.retry_delays
                            else 5.0
                        )
                        delay = max(0.5, min(float(configured), 30.0))
                        if self.stop_event.wait(delay):
                            return
                        continue
                    LOGGER.exception("消息渠道引用回复投递失败：%s", type(exc).__name__)
                    self._fatal = exc
                    self.stop_event.set()
                    return

    def _deliver_reply(self, job: ReplyJob) -> None:
        assert self.config is not None and self.codex_store is not None and self.store is not None
        config = self.config
        codex_store = self.codex_store
        store = self.store

        getter = getattr(codex_store, "get_thread", None)
        if callable(getter) and getter(job.thread_id) is None:
            raise ReplyCannotContinue(
                "无法继续：对应的 Codex 会话已删除、归档到不可访问位置或记录损坏。"
                "请重新查询会话后再发送。"
            )

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

        try:
            session = call_with_retry(
                "Codex Desktop 应用工具握手",
                open_verified,
                self._policy(),
                sleep=self._retry_sleep,
                on_failure=self._on_retry("Codex Desktop 应用工具握手"),
            )
        except RetryExhausted as exc:
            # tools/list 仍在 claim 前；明确的 Desktop 暂不可用可以无限期保留
            # 持久 ReplyJob，并按 worker 的有界等待节奏重试。其它异常仍交给
            # 全局安全熔断，避免掩盖程序错误或不可判断的状态。
            if isinstance(exc.last_error, DesktopAppToolsUnavailable):
                raise ReplyDeferred("Codex Desktop 工具暂不可用") from exc
            raise
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
            except (DesktopAppToolsNotSubmitted, DesktopAppToolsRejected) as exc:
                # 已取得同一管道的 tools/list 身份，但工具在任何写入前失败，
                # 或 Desktop 明确返回拒绝。两种情况都证明正文未被接受；只有
                # 成功撤销 claim 后才允许回到持久 pending 队列。
                if not store.resolve_uncertain_reply(job.code, delivered=False):
                    raise ServiceFatalError(
                        "Desktop 明确未接受回复，但无法安全释放投递 claim"
                    ) from exc
                raise ReplyDeferred("Codex Desktop 明确未接受本次回复") from exc
            except DesktopAppToolsResultUnknown as exc:
                raise ServiceFatalError(
                    "send_message_to_thread 结果未知，为避免重复输入已停止"
                ) from exc
            except (OSError, EOFError, TimeoutError, DesktopAppToolsUnavailable) as exc:
                raise ServiceFatalError(
                    "send_message_to_thread 写入后连接中断，结果未知；为避免重复输入已停止"
                ) from exc
            except DesktopAppToolsError as exc:
                raise ServiceFatalError(
                    "send_message_to_thread 已进入写入阶段但结果无法证明；为避免重复输入已停止"
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
        thread = self._public_thread_record(thread)
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

    @staticmethod
    def _notification_stage_key(event_key: str) -> str:
        """Return a privacy-safe correlation token for timing logs."""

        return hashlib.sha256(str(event_key).encode("utf-8")).hexdigest()[:12]

    def _remember_summary_event(self, event: TurnEvent) -> None:
        with self._summary_event_lock:
            self._summary_events[event.dedupe_key] = event

    def _forget_summary_event(self, event_key: str) -> None:
        with self._summary_event_lock:
            self._summary_events.pop(event_key, None)

    def _restore_summary_event(
        self, delivery: NotificationSummaryDelivery
    ) -> TurnEvent | None:
        """Rebuild one exact turn without persisting its original answer in our DB."""

        assert self.store is not None
        with self._summary_event_lock:
            cached = self._summary_events.get(delivery.event_key)
        if (
            cached is not None
            and cached.thread_id == delivery.thread_id
            and cached.turn_id == delivery.turn_id
        ):
            return cached

        assert self.codex_store is not None

        hook_event: TurnEvent | None = None
        for event_key, payload in self.store.pending_hook_payloads(limit=1000):
            if event_key != delivery.event_key:
                continue
            thread = self.codex_store.get_thread(delivery.thread_id)
            self.codex_store.require_readable("恢复详细摘要的会话信息")
            hook_event = hook_payload_to_event(dict(payload), thread)
            break

        thread = self.codex_store.get_thread(delivery.thread_id)
        self.codex_store.require_readable("恢复详细摘要的会话信息")
        turn = self.codex_store.get_turn(delivery.thread_id, delivery.turn_id)
        self.codex_store.require_readable("恢复详细摘要的精确轮次")
        projected = (
            snapshot_to_event(
                ThreadSnapshot(
                    thread,
                    turn,
                    turn.status if turn is not None else ThreadStatus.UNKNOWN,
                    True,
                    True,
                )
            )
            if turn is not None
            else None
        )
        if projected is not None and hook_event is not None:
            projected = replace(
                projected,
                final_message=(
                    projected.final_message if projected.final_answer_parts
                    else hook_event.final_message.strip() or projected.final_message
                ),
                source=hook_event.source,
                raw=hook_event.raw,
            )
        event = projected or hook_event
        if (
            event is None
            or event.thread_id != delivery.thread_id
            or event.turn_id != delivery.turn_id
            or event.status != "completed"
            or not event.final_message.strip()
        ):
            return None
        event = self._public_event_title(event)
        self._remember_summary_event(event)
        return event

    def _notification_policy_context(self, event: TurnEvent) -> NotificationContext:
        """Build bounded policy context from exact Codex and local state reads."""

        context = NotificationContext(task_state=event.status)
        getter = getattr(self.codex_store, "notification_context", None)
        if callable(getter):
            try:
                options = {}
                proof_getter = getattr(self.store, 'successful_notification_reply_texts', None)
                if callable(proof_getter) and 'verified_reply_texts' in inspect.signature(getter).parameters:
                    options['verified_reply_texts'] = proof_getter(event.thread_id)
                candidate = getter(event.thread_id, event.turn_id, **options)
                if isinstance(candidate, NotificationContext):
                    context = candidate
            except Exception as exc:  # noqa: BLE001 - context is optional evidence
                LOGGER.warning(
                    "通知策略上下文读取失败（异常类型=%s），继续使用结构化状态",
                    type(exc).__name__,
                )
        recent_getter = getattr(self.store, "recent_successful_notification_context", None)
        recent: tuple[str, ...] = ()
        if callable(recent_getter):
            try:
                recent = tuple(
                    recent_getter(
                        event.thread_id,
                        current_event_key=event.dedupe_key,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - history is optional evidence
                LOGGER.warning(
                    "通知策略历史读取失败（异常类型=%s），继续当前事件",
                    type(exc).__name__,
                )
        return NotificationContext(
            user_request=context.user_request,
            task_state=context.task_state or event.status,
            recent_successful_notifications=recent,
        )

    def _summarize_with_policy_context(
        self,
        event: TurnEvent,
        context: NotificationContext,
    ) -> ProgressReport:
        """Call the new summarizer API while keeping old test doubles usable."""

        assert self.summarizer is not None
        summarize = self.summarizer.summarize
        try:
            parameters = inspect.signature(summarize).parameters
            accepts_var_kwargs = any(
                item.kind is inspect.Parameter.VAR_KEYWORD
                for item in parameters.values()
            )
            accepts_context = "context" in parameters or accepts_var_kwargs
            accepts_wait = "wait" in parameters or accepts_var_kwargs
        except (TypeError, ValueError):
            accepts_context = True
            accepts_wait = True
        kwargs: dict[str, Any] = {}
        if accepts_context:
            kwargs["context"] = context
        if accepts_wait:
            kwargs["wait"] = self.stop_event.wait
        return summarize(event, **kwargs)

    def _persist_notification_judgment(
        self,
        event_key: str,
        report: ProgressReport,
        context: NotificationContext,
        *,
        model_attempts: int | None = None,
    ) -> None:
        assert self.store is not None
        recorder = getattr(self.store, "record_notification_judgment", None)
        if not callable(recorder):
            return
        kwargs: dict[str, Any] = {"context": context}
        if model_attempts is not None:
            kwargs["model_attempts"] = model_attempts
        recorder(event_key, report, **kwargs)

    def _format_event_notification(
        self,
        event: TurnEvent,
        report: ProgressReport,
        code: str,
        *,
        include_media_notice: bool,
        include_quota: bool,
        media_after_summary: bool = False,
    ) -> str:
        assert self.config is not None
        message = format_notification(
            event,
            report,
            code,
            include_reply_code=self.config.messaging.backend != "feishu",
        )
        if include_media_notice:
            media_artifacts = (
                event.generated_images
                if self.config.messaging.backend == "feishu"
                else ()
            )
            if media_artifacts:
                if media_after_summary:
                    message += (
                        f"\n\n生成图片：{len(media_artifacts)} 张图片将在详细摘要后按原顺序展示。"
                    )
                else:
                    message += (
                        f"\n\n生成图片：{len(media_artifacts)} 张图片将在下方直接展示。"
                    )
                if any(
                    artifact.size > FEISHU_IMAGE_DIRECT_MAX_BYTES
                    for artifact in media_artifacts
                ):
                    message += " 超过飞书安全上限的图片会保持比例压缩，并另行说明。"
            elif event.generated_images:
                message += "\n\n生成图片：当前消息渠道不支持图片直接展示。"
        if include_quota:
            quota_footer = self._progress_quota_footer()
            if quota_footer:
                message += f"\n\n{quota_footer}"
        return message

    def _summary_backoff(self, attempt_count: int) -> int:
        return min(3600, 30 * (2 ** min(max(0, attempt_count - 1), 7)))

    @staticmethod
    def _notification_raw_backoff(attempt_count: int) -> int:
        return min(3600, 30 * (2 ** min(max(0, attempt_count - 1), 7)))

    def _notification_raw_response(
        self, delivery: NotificationRawDelivery
    ) -> tuple[str, str]:
        assert self.codex_store is not None
        turn = self.codex_store.get_turn(delivery.thread_id, delivery.turn_id)
        require_readable = getattr(self.codex_store, "require_readable", None)
        if callable(require_readable):
            require_readable("读取完成总结对应原文")
        if turn is None or not turn.final_message.strip():
            return (
                "turn_unavailable",
                "无法获取原文：这轮 Codex 记录已被清理、损坏或当前不可读。"
                "系统没有改用其它轮次代替。",
            )
        if turn.status is not ThreadStatus.COMPLETED:
            return (
                "turn_not_completed",
                "无法获取原文：被引用的总结没有对应到一轮已完成的 Codex 回复。",
            )
        digest = hashlib.sha256(turn.final_message.encode("utf-8")).hexdigest()
        if digest != delivery.content_sha256:
            return (
                "turn_changed",
                "无法获取原文：原始完成轮次的内容校验值已经变化。"
                "为避免回错内容，系统已拒绝发送。",
            )
        return "raw", turn.final_message

    def _notification_raw_worker(self) -> None:
        """独立投递完成总结原文；单项失败不拖停监控、摘要或飞书收件。"""

        assert self.store is not None and self.codex_store is not None
        while not self.stop_event.is_set():
            delivery: NotificationRawDelivery | None = None
            try:
                delivery = self.store.claim_notification_raw_delivery()
                if delivery is None:
                    self._notification_raw_wakeup.wait(
                        NOTIFICATION_RAW_WORKER_IDLE_SECONDS
                    )
                    self._notification_raw_wakeup.clear()
                    continue
                self._process_notification_raw_delivery(delivery)
            except ServiceStopping:
                return
            except BaseException as exc:
                if delivery is not None:
                    current = self.store.notification_raw_delivery(
                        delivery.delivery_id
                    )
                    if current is not None and current.state not in {
                        "delivered",
                        "uncertain",
                    }:
                        if current.submitted_at is not None:
                            self.store.mark_notification_raw_uncertain(
                                delivery.delivery_id,
                                f"worker_unknown:{type(exc).__name__}",
                            )
                        elif current.claimed_at is not None:
                            self.store.release_notification_raw_delivery(
                                delivery.delivery_id,
                                f"worker_local:{type(exc).__name__}",
                                next_attempt_at=int(time.time())
                                + self._notification_raw_backoff(
                                    current.attempt_count
                                ),
                            )
                LOGGER.exception(
                    "完成总结原文后台处理失败（异常类型=%s）",
                    type(exc).__name__,
                )
                self.stop_event.wait(NOTIFICATION_RAW_WORKER_IDLE_SECONDS)

    def _process_notification_raw_delivery(
        self, delivery: NotificationRawDelivery
    ) -> None:
        assert self.store is not None
        if self.stop_event.is_set():
            self.store.release_notification_raw_delivery(
                delivery.delivery_id,
                "service_stopping_before_submit",
                next_attempt_at=int(time.time()),
            )
            raise ServiceStopping("服务正在停止")
        try:
            response_kind, response_text = self._notification_raw_response(delivery)
        except CodexStoreReadError as exc:
            self.store.release_notification_raw_delivery(
                delivery.delivery_id,
                f"codex_store:{type(exc).__name__}",
                next_attempt_at=int(time.time())
                + self._notification_raw_backoff(delivery.attempt_count),
            )
            return
        if not self.store.prepare_notification_raw_delivery(
            delivery.delivery_id, response_kind
        ):
            raise StateError("原文投递无法进入 prepared 状态")
        if self.stop_event.is_set():
            self.store.release_notification_raw_delivery(
                delivery.delivery_id,
                "service_stopping_before_submit",
                next_attempt_at=int(time.time()),
            )
            raise ServiceStopping("服务正在停止")
        if not self.store.mark_notification_raw_submitted(delivery.delivery_id):
            raise StateError("原文投递无法进入 submitted 状态")
        try:
            message_ids = self._send_channel_text(
                response_text,
                idempotency_key=f"notification-raw:{delivery.delivery_id}",
            )
            if not message_ids:
                raise FeishuSendError("原文发送结果缺少 message_id")
        except MessageChannelOfflineError:
            self.store.release_notification_raw_delivery(
                delivery.delivery_id,
                "channel_offline",
                allow_submitted=True,
                next_attempt_at=int(time.time())
                + self._notification_raw_backoff(delivery.attempt_count),
            )
            return
        except FeishuSendRejectedError as exc:
            self.store.release_notification_raw_delivery(
                delivery.delivery_id,
                f"rejected:{exc.code}:{exc.raw_code}",
                allow_submitted=True,
                next_attempt_at=int(time.time())
                + self._notification_raw_backoff(delivery.attempt_count),
            )
            return
        except FeishuSendError as exc:
            self.store.mark_notification_raw_uncertain(
                delivery.delivery_id,
                f"result_unknown:{type(exc).__name__}",
            )
            LOGGER.error("原文发送结果未知，已冻结单项以避免重复")
            return
        except BaseException as exc:
            self.store.mark_notification_raw_uncertain(
                delivery.delivery_id,
                f"result_unknown:{type(exc).__name__}",
            )
            LOGGER.error(
                "原文发送结果未知，已冻结单项（异常类型=%s）",
                type(exc).__name__,
            )
            return
        if not self.store.mark_notification_raw_delivered(
            delivery.delivery_id, message_ids
        ):
            raise StateError("原文送达状态未能持久确认")
        LOGGER.info("完成总结原文已送达")

    def _completion_raw_binding_values(
        self,
        event: TurnEvent,
        report: ProgressReport,
    ) -> tuple[str, str] | None:
        """返回 owner/hash；只读取 exact turn，不把原始正文交给状态库。"""

        if (
            self.config is None
            or self.store is None
            or self.codex_store is None
            or self.config.messaging.backend != "feishu"
            or event.status != "completed"
            or not report.is_task_complete
        ):
            return None
        getter = getattr(self.codex_store, "get_turn", None)
        if not callable(getter):
            return None
        turn = getter(event.thread_id, event.turn_id)
        require_readable = getattr(self.codex_store, "require_readable", None)
        if callable(require_readable):
            require_readable("冻结完成总结原文身份")
        if (
            turn is None
            or turn.status is not ThreadStatus.COMPLETED
            or not turn.final_message.strip()
        ):
            return None
        digest = hashlib.sha256(turn.final_message.encode("utf-8")).hexdigest()
        return self.config.feishu.target_open_id, digest

    def _prepare_completion_raw_binding(
        self,
        event: TurnEvent,
        report: ProgressReport,
    ) -> bool:
        """为已有父通知冻结 exact turn/hash；用于后台摘要发送前。"""

        assert self.store is not None
        values = self._completion_raw_binding_values(event, report)
        if values is None:
            return False
        sender_id, digest = values
        self.store.prepare_notification_raw_binding(
            event.dedupe_key,
            sender_id=sender_id,
            thread_id=event.thread_id,
            turn_id=event.turn_id,
            content_sha256=digest,
        )
        return True

    def _completion_raw_chat_id(
        self,
        event_key: str,
        message_ids: tuple[str, ...],
    ) -> str | None:
        """读取官方发送结果缓存的 owner-bound 私聊 ID。"""

        if (
            self.config is None
            or self.store is None
            or self.channel is None
            or not message_ids
            or not self.store.notification_raw_binding_prepared(event_key)
        ):
            return None
        resolver = getattr(self.channel, "recipient_scope_for_messages", None)
        if not callable(resolver):
            return None
        scope = resolver(tuple(message_ids))
        if not isinstance(scope, tuple) or len(scope) != 2:
            return None
        sender_id, chat_id = (str(scope[0] or "").strip(), str(scope[1] or "").strip())
        if sender_id != self.config.feishu.target_open_id or not chat_id:
            raise StateError("完成总结返回的私聊作用域与配置不一致")
        return chat_id

    def _bind_notification_text_with_raw_context(
        self,
        event_key: str,
        message_ids: tuple[str, ...],
    ) -> None:
        """原子绑定普通通知文本；无 raw 候选时保持旧语义。"""

        assert self.store is not None
        chat_id = self._completion_raw_chat_id(event_key, message_ids)
        if chat_id is None:
            self.store.bind_channel_messages(event_key, message_ids)
            return
        self.store.bind_channel_messages_with_raw_context(
            event_key,
            message_ids,
            chat_id=chat_id,
        )

    def _summary_worker(self) -> None:
        """Generate and deliver long-form summaries outside the monitor event loop."""

        assert self.store is not None and self.summarizer is not None
        while not self.stop_event.is_set():
            delivery: NotificationSummaryDelivery | None = None
            try:
                delivery = self.store.claim_notification_summary()
                if delivery is None:
                    self._summary_wakeup.wait(SUMMARY_WORKER_IDLE_SECONDS)
                    self._summary_wakeup.clear()
                    continue
                self._process_notification_summary(delivery)
            except (SummaryCancelled, ServiceStopping):
                return
            except BaseException as exc:
                # A broken single summary must not take down Feishu or monitoring.
                if delivery is not None:
                    current = self.store.notification_summary_delivery(
                        delivery.event_key
                    )
                    if current is not None and current.state not in {
                        "delivered",
                        "uncertain",
                    }:
                        if current.submitted_at is not None:
                            self.store.mark_notification_summary_uncertain(
                                delivery.event_key,
                                f"worker_unknown:{type(exc).__name__}",
                            )
                            self.store.mark_processed(delivery.event_key)
                            self._forget_summary_event(delivery.event_key)
                            self._drain_notification_media(delivery.event_key)
                        elif current.claimed_at is not None:
                            self.store.release_notification_summary(
                                delivery.event_key,
                                f"worker_local:{type(exc).__name__}",
                                next_attempt_at=int(time.time())
                                + self._summary_backoff(current.attempt_count),
                            )
                LOGGER.exception(
                    "详细摘要后台处理失败（异常类型=%s）",
                    type(exc).__name__,
                )
                self.stop_event.wait(SUMMARY_WORKER_IDLE_SECONDS)

    def _process_notification_summary(
        self, delivery: NotificationSummaryDelivery
    ) -> None:
        assert self.store is not None and self.summarizer is not None
        stage_key = self._notification_stage_key(delivery.event_key)
        if self.stop_event.is_set():
            self.store.release_notification_summary(
                delivery.event_key,
                "service_stopping_before_submit",
                next_attempt_at=int(time.time()),
            )
            raise ServiceStopping("服务正在停止")
        message = delivery.message_text
        if not message:
            event = self._restore_summary_event(delivery)
            if event is None:
                self.store.release_notification_summary(
                    delivery.event_key,
                    "exact_turn_not_ready",
                    next_attempt_at=int(time.time()) + 30,
                )
                LOGGER.warning(
                    "通知阶段 event=%s phase=summary_deferred reason=turn_not_ready",
                    stage_key,
                )
                return
            started = time.monotonic()
            LOGGER.info("通知阶段 event=%s phase=summary_started", stage_key)
            policy_context = self._notification_policy_context(event)
            try:
                report = call_with_retry(
                    "后台进度摘要",
                    lambda: self._summarize_with_policy_context(event, policy_context),
                    self._policy(),
                    sleep=self._retry_sleep,
                    on_failure=self._on_retry("后台进度摘要"),
                )
            except RetryExhausted as exc:
                report = fallback_report(event)
                self._persist_notification_judgment(
                    delivery.event_key,
                    report,
                    policy_context,
                    model_attempts=self.config.service.max_attempts
                    if self.config is not None
                    else None,
                )
                LOGGER.warning(
                    "通知阶段 event=%s phase=summary_model_failed attempts=%s error=%s",
                    stage_key,
                    self.config.service.max_attempts if self.config is not None else "unknown",
                    type(exc).__name__,
                )
                # A model failure is not a policy decision.  Keep the claimed
                # outbox row retryable and preserve the explicit error marker;
                # never discard it as ``silent``.
                self.store.release_notification_summary(
                    delivery.event_key,
                    "policy_model_failure",
                    next_attempt_at=int(time.time())
                    + self._summary_backoff(delivery.attempt_count),
                )
                return
            except (SummaryCancelled, ServiceStopping):
                self.store.release_notification_summary(
                    delivery.event_key,
                    "service_stopping_before_submit",
                    next_attempt_at=int(time.time()),
                )
                raise
            self._persist_notification_judgment(
                delivery.event_key,
                report,
                policy_context,
            )
            if report.has_model_error:
                self.store.release_notification_summary(
                    delivery.event_key,
                    "policy_model_failure",
                    next_attempt_at=int(time.time())
                    + self._summary_backoff(delivery.attempt_count),
                )
                LOGGER.warning(
                    "通知阶段 event=%s phase=summary_model_error",
                    stage_key,
                )
                return
            if not report.should_notify:
                if not self.store.discard_notification_summary(
                    delivery.event_key,
                    f"policy:{report.notification_reason}",
                ):
                    raise StateError("静默摘要无法原子终结")
                self._forget_summary_event(delivery.event_key)
                LOGGER.info(
                    "通知阶段 event=%s phase=summary_silent",
                    stage_key,
                )
                return
            self._prepare_completion_raw_binding(event, report)
            message = self._format_event_notification(
                event,
                report,
                delivery.code,
                include_media_notice=True,
                include_quota=True,
                media_after_summary=True,
            )
            if not self.store.prepare_notification_summary(
                delivery.event_key, message
            ):
                raise StateError("详细摘要无法进入 prepared 状态")
            LOGGER.info(
                "通知阶段 event=%s phase=summary_prepared elapsed_ms=%d",
                stage_key,
                int((time.monotonic() - started) * 1000),
            )

        if self.stop_event.is_set():
            self.store.release_notification_summary(
                delivery.event_key,
                "service_stopping_before_submit",
                next_attempt_at=int(time.time()),
            )
            raise ServiceStopping("服务正在停止")
        if not self.store.mark_notification_summary_submitted(delivery.event_key):
            raise StateError("详细摘要无法进入 submitted 状态")
        try:
            message_ids = tuple(
                self._send_channel_text(
                    message,
                    idempotency_key=f"notification-summary:{delivery.event_key}",
                )
            )
            if not message_ids:
                raise FeishuSendError("详细摘要发送结果缺少 message_id")
        except MessageChannelOfflineError:
            self.store.release_notification_summary(
                delivery.event_key,
                "channel_offline",
                allow_submitted=True,
                next_attempt_at=int(time.time())
                + self._summary_backoff(delivery.attempt_count),
            )
            return
        except FeishuSendRejectedError as exc:
            self.store.release_notification_summary(
                delivery.event_key,
                f"rejected:{exc.code}:{exc.raw_code}",
                allow_submitted=True,
                next_attempt_at=int(time.time())
                + self._summary_backoff(delivery.attempt_count),
            )
            return
        except FeishuSendError as exc:
            self.store.mark_notification_summary_uncertain(
                delivery.event_key,
                f"result_unknown:{type(exc).__name__}",
            )
            self.store.mark_processed(delivery.event_key)
            self._forget_summary_event(delivery.event_key)
            self._drain_notification_media(delivery.event_key)
            LOGGER.error(
                "通知阶段 event=%s phase=summary_unknown",
                stage_key,
            )
            return
        except BaseException as exc:
            # Once the send call has crossed submitted, an unclassified exception
            # cannot be assumed pre-submit; freeze rather than risk a duplicate.
            self.store.mark_notification_summary_uncertain(
                delivery.event_key,
                f"result_unknown:{type(exc).__name__}",
            )
            self.store.mark_processed(delivery.event_key)
            self._forget_summary_event(delivery.event_key)
            self._drain_notification_media(delivery.event_key)
            LOGGER.error(
                "通知阶段 event=%s phase=summary_unknown exception=%s",
                stage_key,
                type(exc).__name__,
            )
            return
        raw_chat_id = self._completion_raw_chat_id(
            delivery.event_key, message_ids
        )
        delivered = (
            self.store.mark_notification_summary_delivered_with_raw_context(
                delivery.event_key,
                message_ids,
                chat_id=raw_chat_id,
            )
            if raw_chat_id is not None
            else self.store.mark_notification_summary_delivered(
                delivery.event_key, message_ids
            )
        )
        if not delivered:
            raise StateError("详细摘要送达状态未能持久确认")
        self.store.mark_processed(delivery.event_key)
        self._forget_summary_event(delivery.event_key)
        # Images are intentionally released only after the detailed text reaches
        # a terminal delivery state, preserving text -> image ordinal ordering.
        self._drain_notification_media(delivery.event_key)
        LOGGER.info("通知阶段 event=%s phase=summary_sent", stage_key)

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

    @staticmethod
    def _notification_media_backoff(attempt_count: int) -> int:
        return min(3600, 30 * (2 ** min(max(0, attempt_count - 1), 7)))

    def _send_notification_media_warning(
        self,
        delivery: NotificationMediaDelivery,
        detail: str,
    ) -> None:
        """Best-effort one-time warning; image state remains authoritative."""

        assert self.store is not None
        if delivery.warning_sent_at is not None:
            return
        try:
            message_ids = self._send_channel_text(
                detail,
                idempotency_key=f"notification-media-warning:{delivery.delivery_id}",
            )
            if message_ids:
                self.store.bind_channel_messages(delivery.event_key, message_ids)
            self.store.mark_notification_media_warning_sent(delivery.delivery_id)
        except Exception as exc:
            LOGGER.warning(
                "图片投递提示暂未送达 delivery=%s exception=%s",
                delivery.delivery_id,
                type(exc).__name__,
            )

    def _drain_notification_media(self, event_key: str | None = None) -> None:
        """Send persisted media one item at a time without taking down the service."""

        assert self.store is not None
        for _ in range(64):
            pending = self.store.pending_notification_media(event_key=event_key)
            if not pending:
                return
            claimed = self.store.claim_notification_media(pending[0].delivery_id)
            if claimed is None:
                continue
            artifact = GeneratedImageArtifact(
                item_id=claimed.item_id,
                path=claimed.path,
                mime_type=claimed.mime_type,
                sha256=claimed.sha256,
                size=claimed.size,
                file_name=claimed.file_name,
            )
            transformed = False
            try:
                original = read_generated_image_bytes(artifact)
                payload, transformed = _prepare_feishu_image(
                    original, artifact.mime_type
                )
            except (OSError, ValueError) as exc:
                self.store.discard_notification_media(
                    claimed.delivery_id, error_code="unsafe_or_invalid_media"
                )
                self._send_notification_media_warning(
                    claimed,
                    "有 1 张生成图片未发送：原文件未通过安全或格式校验。"
                    "其余进度通知会继续运行。",
                )
                LOGGER.warning(
                    "生成图片安全校验失败并停止重试 delivery=%s exception=%s",
                    claimed.delivery_id,
                    type(exc).__name__,
                )
                continue
            try:
                image_message_ids = self._send_channel_image(
                    payload,
                    idempotency_key=f"notification-media:{claimed.delivery_id}",
                )
                if not image_message_ids:
                    raise FeishuSendError("图片发送结果缺少 message_id")
            except MessageChannelOfflineError:
                self.store.defer_notification_media(
                    claimed.delivery_id,
                    error_code="channel_offline",
                    next_attempt_at=int(time.time())
                    + self._notification_media_backoff(claimed.attempt_count),
                )
                LOGGER.warning(
                    "消息渠道离线，图片已持久化延后 delivery=%s",
                    claimed.delivery_id,
                )
                return
            except FeishuSendRejectedError as exc:
                self.store.defer_notification_media(
                    claimed.delivery_id,
                    error_code=f"rejected:{exc.code}:{exc.raw_code}",
                    next_attempt_at=int(time.time())
                    + self._notification_media_backoff(claimed.attempt_count),
                    rejected=True,
                )
                detail = (
                    "有 1 张生成图片暂未发送：飞书明确拒绝了图片上传"
                    f"（分类 {exc.code}）。系统已保留该图片并会退避重试，"
                    "其余进度通知不受影响。"
                )
                self._send_notification_media_warning(claimed, detail)
                LOGGER.warning(
                    "飞书明确拒绝图片，已持久化退避 delivery=%s code=%s raw=%s",
                    claimed.delivery_id,
                    exc.code,
                    exc.raw_code,
                )
                return
            except FeishuSendError as exc:
                # HTTP/SDK 已经开始发送但没有可证明结果。保留 claim 并冻结本张，
                # 主服务继续；绝不能自动重发造成重复图片。
                self.store.mark_notification_media_uncertain(
                    claimed.delivery_id,
                    error_code=f"result_unknown:{type(exc).__name__}",
                )
                self._send_notification_media_warning(
                    claimed,
                    "有 1 张生成图片的飞书发送结果无法确认。系统已停止自动重发"
                    "该图片以避免重复，其余进度通知会继续运行。",
                )
                LOGGER.error(
                    "图片发送结果未知，已冻结单项而不停止服务 delivery=%s",
                    claimed.delivery_id,
                )
                continue
            except Exception as exc:
                # 调用前的本地能力/参数错误可证明没有被飞书接受，安全退避。
                self.store.defer_notification_media(
                    claimed.delivery_id,
                    error_code=f"local:{type(exc).__name__}",
                    next_attempt_at=int(time.time())
                    + self._notification_media_backoff(claimed.attempt_count),
                )
                LOGGER.exception(
                    "图片本地投递准备失败，已持久化延后 delivery=%s",
                    claimed.delivery_id,
                )
                return
            self.store.mark_notification_media_delivered_with_message_ids(
                claimed.delivery_id, image_message_ids
            )
            if transformed:
                self._send_notification_media_warning(
                    claimed,
                    "有 1 张原图超过飞书直接展示安全上限，已保持长宽比例压缩后发送。",
                )
            LOGGER.info(
                "生成图片已送达 delivery=%s ordinal=%s transformed=%s",
                claimed.delivery_id,
                claimed.ordinal,
                transformed,
            )

    def _drain_notification_texts(self) -> None:
        """Recover notification text reserved before a process interruption."""

        assert self.store is not None
        for event_key, message_text in self.store.pending_notification_texts():
            message_ids = call_with_retry(
                "持久通知正文恢复发送",
                lambda event_key=event_key, message_text=message_text: self._send_channel_text(
                    message_text,
                    idempotency_key=f"notification:{event_key}",
                ),
                self._policy(),
                sleep=self._retry_sleep,
                on_failure=self._on_retry("持久通知正文恢复发送"),
            )
            if message_ids:
                self._bind_notification_text_with_raw_context(
                    event_key, message_ids
                )
            self.store.mark_sent(event_key)

    def _deliver_event_report(
        self,
        event: TurnEvent,
        report: ProgressReport,
    ) -> None:
        """Deliver a one-message notification for structural or safe direct results."""

        assert self.config and self.store and self.codec
        if not report.should_notify:
            self.store.mark_processed(event.dedupe_key)
            LOGGER.info(
                "通知阶段 event=%s phase=direct_silent",
                self._notification_stage_key(event.dedupe_key),
            )
            return
        code = self.codec.issue()
        message = self._format_event_notification(
            event,
            report,
            code,
            include_media_notice=True,
            include_quota=True,
        )
        media_artifacts = (
            event.generated_images
            if self.config.messaging.backend == "feishu"
            else ()
        )
        _stored_code, stored_message = self.store.reserve_notification(
            event,
            code,
            message,
            self.config.messaging.pending_ttl_hours,
            reply_kind='notice' if event.source=='codex-desktop-approval-observation' else 'turn',
            **(
                {
                    "raw_sender_id": raw_values[0],
                    "raw_content_sha256": raw_values[1],
                }
                if (raw_values := self._completion_raw_binding_values(event, report))
                is not None
                else {}
            ),
        )
        self._persist_notification_judgment(
            event.dedupe_key,
            report,
            self._notification_policy_context(event),
        )
        if media_artifacts:
            self.store.reserve_notification_media(event.dedupe_key, media_artifacts)
        if not self.store.notification_sent(event.dedupe_key):
            sent_message_ids = tuple(
                call_with_retry(
                    "消息渠道发送",
                    lambda: self._send_channel_text(
                        stored_message,
                        idempotency_key=f"notification:{event.dedupe_key}",
                    ),
                    self._policy(),
                    sleep=self._retry_sleep,
                    on_failure=self._on_retry("消息渠道发送"),
                )
            )
            if sent_message_ids:
                self._bind_notification_text_with_raw_context(
                    event.dedupe_key, sent_message_ids
                )
        self.store.mark_sent(event.dedupe_key)
        self.store.mark_processed(event.dedupe_key)
        self._drain_notification_media(event.dedupe_key)
        LOGGER.info(
            "通知阶段 event=%s phase=direct_sent",
            self._notification_stage_key(event.dedupe_key),
        )

    def _queue_background_notification_decision(
        self, event: TurnEvent, *, wake: bool = True
    ) -> None:
        """Persist a background notification decision without a visible placeholder."""

        assert self.config and self.store and self.codec
        stage_key = self._notification_stage_key(event.dedupe_key)
        code = self.codec.issue()
        self.store.reserve_notification_summary_only(
            event,
            code,
            self.config.messaging.pending_ttl_hours,
        )
        if event.generated_images:
            self.store.reserve_notification_media(
                event.dedupe_key, event.generated_images
            )
        summary = self.store.notification_summary_delivery(event.dedupe_key)
        if summary is None:
            raise StateError("详细摘要 outbox 未与父通知原子建立")
        if self.store.notification_summary_terminal(event.dedupe_key):
            self.store.mark_processed(event.dedupe_key)
            self._forget_summary_event(event.dedupe_key)
            self._drain_notification_media(event.dedupe_key)
            return
        self._remember_summary_event(event)
        LOGGER.info(
            "通知阶段 event=%s phase=decision_queued",
            stage_key,
        )
        if wake:
            self._summary_wakeup.set()

    def _artifact_queue(self):
        if self.file_delivery_queue is None:
            from .file_delivery import FileDeliveryQueue
            self.file_delivery_queue = FileDeliveryQueue(
                self.store, self.store.path.parent / "artifact-snapshots", self.channel,
                self._bind_artifact_messages)
        return self.file_delivery_queue

    def _bind_artifact_messages(self, row, ids, notice):
        event = TurnEvent(thread_id=row['thread_id'],turn_id=row['turn_id'],
                          status='artifact-'+row['delivery_id']+('-notice' if notice else ''),
                          title=row['title'])
        # A parent is created only after a known platform result. Each file has
        # its own binding, so the existing per-message limit does not cap files.
        self.store.reserve_notification(event,self.codec.issue(),'',
                                        self.config.messaging.pending_ttl_hours)
        self.store.bind_channel_messages(event.dedupe_key,ids,allow_additional=True)
        self.store.mark_sent(event.dedupe_key)

    def _send_event(self, event: TurnEvent) -> None:
        assert self.config and self.store and self.codec and self.channel and self.summarizer
        event = self._public_event_title(event)
        if (event.delivered_files or event.generated_images) and self.config.messaging.backend == "feishu":
            handled_images=self._artifact_queue().reserve(event,event.delivered_files)
            if handled_images:
                event=replace(event,generated_images=tuple(item for item in event.generated_images
                    if str(Path(item.path).resolve()).casefold() not in handled_images))
        if self.store.was_processed(event.dedupe_key):
            # Hook 可能先以纯文字消费同一轮，结构化数据库稍后才投影图片。
            # 已发送父通知仍允许幂等补入本轮媒体，不重新摘要或重复正文。
            if (
                event.generated_images
                and self.config.messaging.backend == "feishu"
                and self.store.notification_sent(event.dedupe_key)
            ):
                self.store.reserve_notification_media(
                    event.dedupe_key, event.generated_images
                )
                self._drain_notification_media(event.dedupe_key)
            return
        if event.status == "completed" and not event.final_message.strip():
            LOGGER.info(
                "暂缓正文尚未就绪的完成事件 thread=%s turn=%s",
                event.thread_id,
                event.turn_id,
            )
            return
        heartbeat = (
            _parse_heartbeat_control(event.final_message)
            if event.status == "completed"
            else None
        )
        if heartbeat is not None:
            decision, readable_message = heartbeat
            if decision == "DONT_NOTIFY":
                self.store.mark_processed(event.dedupe_key)
                LOGGER.info(
                    "已静默消费自动化心跳 thread=%s turn=%s",
                    event.thread_id,
                    event.turn_id,
                )
                return
            event = replace(event, final_message=readable_message)
        immediate_method = getattr(self.summarizer, "immediate_report", None)
        supports_immediate = callable(immediate_method)
        immediate = (
            structural_report(event)
            if event.status in {"waitingOnApproval", "waitingOnUserInput"}
            else immediate_method(event)
            if supports_immediate
            else None
        )
        if immediate is not None:
            self._deliver_event_report(event, immediate)
            return
        if (
            self.config.messaging.backend == "feishu"
            and event.status == "completed"
            and supports_immediate
        ):
            self._queue_background_notification_decision(event)
            return
        # 非飞书后端没有可引用的两阶段消息语义，继续采用单条同步摘要。
        policy_context = self._notification_policy_context(event)
        try:
            report = call_with_retry(
                "进度摘要",
                lambda: self._summarize_with_policy_context(event, policy_context),
                self._policy(),
                sleep=self._retry_sleep,
                on_failure=self._on_retry("进度摘要"),
            )
        except RetryExhausted as exc:
            # 摘要失败不能伪装成策略模型选择的 silent。把事件转入同一
            # 持久 outbox，由后台 worker 按退避策略继续重试并记录错误标记。
            LOGGER.error("进度摘要连续失败，转入持久摘要重试：%s", type(exc).__name__)
            # Queue without waking the worker first so the explicit model
            # failure judgment is stored before a fast retry can overwrite it.
            self._queue_background_notification_decision(event, wake=False)
            self._persist_notification_judgment(
                event.dedupe_key,
                fallback_report(event),
                policy_context,
                model_attempts=self.config.service.max_attempts,
            )
            self._summary_wakeup.set()
            return
        self._deliver_event_report(event, report)

    def _poll_once(self, config: AppConfig) -> None:
        timing = _PollCycleTiming()
        try:
            self._poll_once_measured(config, timing)
        finally:
            timing.finish()

    def _poll_once_measured(self, config: AppConfig, timing: _PollCycleTiming) -> None:
        assert self.store is not None and self.codex_store is not None
        self._publish_channel_health()
        channel_online = self.channel is not None and self.channel.is_online()
        feishu_channel_offline = (isinstance(self.channel, FeishuMessageChannel) or getattr(self.channel,"is_guardian_proxy",False)) and not channel_online
        if time.monotonic() - self._last_wechat_health >= 30:
            if feishu_channel_offline:
                # 飞书通道拥有独立监督线程；暂时断网时继续轮询本地状态，
                # 但不触发新的渠道发送或全局 fatal。
                if not self._channel_offline_reported:
                    LOGGER.warning(
                        "飞书消息渠道暂时离线，服务保持运行并等待自动重连"
                    )
                    self._channel_offline_reported = True
            elif self.channel is None or not channel_online:
                # 旧微信/测试适配器保持历史 fail-closed 语义。
                raise ServiceFatalError("消息渠道已离线")
            else:
                if self._channel_offline_reported:
                    LOGGER.info("飞书消息渠道已恢复在线")
                self._channel_offline_reported = False
            self._last_wechat_health = time.monotonic()
        timing.enter('monitor_registry')
        self._refresh_monitor_registry(config)
        timing.enter('thread_selection')
        selected = self._selected_threads(config)
        if self.file_delivery_queue is not None:
            self.file_delivery_queue.wakeup.set()
        if feishu_channel_offline:
            timing.enter('offline_snapshots')
            for thread_id in selected:
                snapshot = self.codex_store.snapshot(thread_id)
                snapshot.require_readable()
                event = snapshot_to_event(snapshot)
                if event is not None and (event.delivered_files or event.generated_images):
                    self._artifact_queue().reserve(event,event.delivered_files)
            # 不消费 Hook、不创建摘要、不领取 outbox；等通道恢复后由同一
            # 持久状态继续处理，避免“文字成功、媒体/回执丢失”。
            return
        try:
            timing.enter('notification_text_outbox')
            self._drain_notification_texts()
            # 图片 outbox 独立于新事件恢复；服务重启后只续送尚未完成的单项。
            timing.enter('notification_media_outbox')
            self._drain_notification_media()
        except BaseException as exc:
            if _is_channel_offline_failure(exc):
                LOGGER.warning(
                    "消息渠道在恢复 outbox 时暂时离线，保留任务稍后重试（异常类型=%s）",
                    type(exc).__name__,
                )
                return
            raise
        timing.enter('hook_events')
        for event_key, payload in self.store.pending_hook_payloads():
            thread_id = str(payload.get("thread-id") or payload.get("thread_id") or "")
            if thread_id in selected:
                event = hook_payload_to_event(dict(payload), selected[thread_id])
                if event.status == "completed":
                    # notify 钩子不携带 imageGeneration；无论正文是否已经存在，
                    # 都尝试用同一 thread、同一 turn 的结构化数据库事件补全。
                    # 严格身份不匹配时保留 Hook 正文，稍后的数据库轮询仍可把
                    # 同轮媒体幂等补入已发送父通知。
                    snapshot = self.codex_store.snapshot(thread_id)
                    snapshot.require_readable()
                    projected = snapshot_to_event(snapshot)
                    if (
                        projected is not None
                        and projected.thread_id == event.thread_id
                        and projected.turn_id == event.turn_id
                        and projected.status == event.status
                    ):
                        event = replace(
                            projected,
                            final_message=(
                                projected.final_message if projected.final_answer_parts
                                else event.final_message.strip() or projected.final_message
                            ),
                            source=event.source,
                            raw=event.raw,
                        )
                    elif (
                        not event.final_message.strip()
                        and (
                        projected is None
                        or projected.thread_id != event.thread_id
                        or projected.turn_id != event.turn_id
                        or projected.status != event.status
                        )
                    ):
                        if self.store.was_processed(event.dedupe_key):
                            self.store.mark_hook_consumed(event_key)
                        continue
                try:
                    self._send_event(event)
                except BaseException as exc:
                    if _is_channel_offline_failure(exc):
                        LOGGER.warning(
                            "消息渠道在处理 Hook 时暂时离线，保留事件稍后重试（异常类型=%s）",
                            type(exc).__name__,
                        )
                        return
                    raise
                if self.store.was_processed(event.dedupe_key):
                    self.store.mark_hook_consumed(event_key)
            else:
                self.store.mark_hook_consumed(event_key)
        timing.enter('thread_snapshots')
        for thread_id in selected:
            # ``unknown`` 是健康数据库中没有轮次/未知显式 status 的合法结果；
            # 只有 errors 才表示读取异常，必须抛给 run() 的有限重试熔断路径。
            snapshot = self.codex_store.snapshot(thread_id)
            snapshot.require_readable()
            event = snapshot_to_event(snapshot)
            if event is not None:
                try:
                    self._send_event(event)
                except BaseException as exc:
                    if _is_channel_offline_failure(exc):
                        LOGGER.warning(
                            "消息渠道在处理监测事件时暂时离线，保留事件稍后重试（异常类型=%s）",
                            type(exc).__name__,
                        )
                        return
                    raise

    def _alert(self, error: BaseException) -> None:
        self._close_active_rpc()
        if getattr(self.channel,"is_guardian_proxy",False):
            # Guardian observes this worker generation ending and owns the single alert.
            LOGGER.error("业务停止 type=%s",type(error).__name__)
            return
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
            if getattr(self.channel,"is_guardian_proxy",False):
                self.channel.heartbeat(ready=True)
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
                if getattr(self.channel,"is_guardian_proxy",False):
                    if not self.channel.heartbeat(ready=True):
                        self.request_stop()
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
            if self.file_delivery_queue is not None:
                self.file_delivery_queue.stop()
            self.reply_queue.put(None)
            self.receipt_queue.put(None)
            self.management_queue.put(None)
            self.parent_recovery_queue.put(None)
            if self.approval_thread is not None and self.approval_thread is not threading.current_thread():
                self.approval_thread.join(timeout=10)
            if self.attention_thread is not None and self.attention_thread is not threading.current_thread():
                self.attention_thread.join(timeout=15)
            if self.desktop_approval_thread is not None and self.desktop_approval_thread is not threading.current_thread():
                self.desktop_approval_thread.join(timeout=10)
            if self.summary_thread is not None and self.summary_thread is not threading.current_thread():
                self._summary_wakeup.set()
                self.summary_thread.join(timeout=SUMMARY_WORKER_STOP_TIMEOUT_SECONDS)
            if (
                self.notification_raw_thread is not None
                and self.notification_raw_thread is not threading.current_thread()
            ):
                self._notification_raw_wakeup.set()
                self.notification_raw_thread.join(
                    timeout=NOTIFICATION_RAW_WORKER_STOP_TIMEOUT_SECONDS
                )
            if (
                self.parent_recovery_thread is not None
                and self.parent_recovery_thread is not threading.current_thread()
            ):
                self.parent_recovery_thread.join(
                    timeout=PARENT_RECOVERY_WORKER_STOP_TIMEOUT_SECONDS
                )
            if self.reset_alert_thread is not None and self.reset_alert_thread is not threading.current_thread():
                self.reset_alert_thread.join(timeout=10)
            if self.channel is not None:
                try:
                    self.channel.stop()
                except Exception:
                    pass
                self._publish_channel_health(forced_state="stopped")
            if self.reply_thread is not None and self.reply_thread is not threading.current_thread():
                self.reply_thread.join(timeout=10)
            if self.receipt_thread is not None and self.receipt_thread is not threading.current_thread():
                self.receipt_thread.join(timeout=10)
            if self.management_thread is not None and self.management_thread is not threading.current_thread():
                self.management_thread.join(timeout=10)
            reply_worker_stopped = self.reply_thread is None or not self.reply_thread.is_alive()
            summary_worker_stopped = (
                self.summary_thread is None or not self.summary_thread.is_alive()
            )
            raw_worker_stopped = (
                self.notification_raw_thread is None
                or not self.notification_raw_thread.is_alive()
            )
            parent_recovery_worker_stopped = (
                self.parent_recovery_thread is None
                or not self.parent_recovery_thread.is_alive()
            )
            reset_worker_stopped = (
                self.reset_alert_thread is None
                or not self.reset_alert_thread.is_alive()
            )
            worker_stopped = (
                reply_worker_stopped
                and summary_worker_stopped
                and raw_worker_stopped
                and parent_recovery_worker_stopped
                and reset_worker_stopped
                and (self.file_delivery_queue is None or self.file_delivery_queue.thread is None or not self.file_delivery_queue.thread.is_alive())
            )
            if not reply_worker_stopped:
                LOGGER.critical("回复线程未能在 10 秒内停止；保留状态库连接以避免并发关闭")
            if not summary_worker_stopped:
                LOGGER.critical(
                    "详细摘要线程未能在 %s 秒内停止；保留状态库连接以避免并发关闭",
                    SUMMARY_WORKER_STOP_TIMEOUT_SECONDS,
                )
            if not raw_worker_stopped:
                LOGGER.critical(
                    "原文投递线程未能在 %s 秒内停止；保留状态库连接以避免并发关闭",
                    NOTIFICATION_RAW_WORKER_STOP_TIMEOUT_SECONDS,
                )
            if not parent_recovery_worker_stopped:
                LOGGER.critical(
                    "父消息恢复线程未能在 %s 秒内停止；保留状态库连接以避免并发关闭",
                    PARENT_RECOVERY_WORKER_STOP_TIMEOUT_SECONDS,
                )
            if not reset_worker_stopped:
                LOGGER.critical(
                    "重置预警线程未能在 10 秒内停止；保留状态库连接以避免并发关闭"
                )
            if getattr(self, "user_reply_chain", None) is not None and worker_stopped:
                self.user_reply_chain.close()
                self.user_reply_chain = None
            if self.store is not None and worker_stopped:
                self.store.close()
