"""通过飞书精确命令管理 Codex Desktop 项目和会话。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import logging
import math
import ntpath
from pathlib import Path
import re
import time
from typing import Any, Callable, Mapping, Sequence

from .channel import (
    ChannelReply,
    MessageChannelOfflineError,
    MessageChannelPayloadRejectedError,
    codex_prompt_for_reply,
)
from .codex_account import (
    CodexAccountError,
    CodexAccountReader,
    format_rate_limits,
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
from .codex_rpc import (
    CodexRPCClosed,
    CodexRPCError,
    CodexRPCRejected,
    CodexRPCTimeout,
)
from .codex_projects import CodexProjectRegistry, LocalProject, ProjectRegistryError
from .codex_store import (
    CodexStore,
    CodexStoreReadError,
    ThreadRecord,
    independent_thread_title,
    public_thread_title,
    read_generated_image_bytes,
    thread_title_recovery_hash,
)
from .models import GeneratedImageArtifact
from .session_search import (
    BEIJING,
    SearchMatch,
    SearchRequest,
    SearchResult,
    SessionSearchEngine,
    SessionSearchError,
)
from .session_query_card import (
    SESSION_QUERY_ENTRY_COMMAND,
    build_project_list_card,
    build_session_search_form_card,
    build_session_query_card,
    build_thread_list_card,
    build_thread_overview_card,
    build_thread_reply_form_card,
    session_query_card_action_fingerprints,
    session_query_text_menu,
)
from .remote_control import (
    AppServerRemoteControl,
    GoalSnapshot,
    REMOTE_CONTROL_ENTRY_COMMAND,
    RemoteCommand,
    RemoteWriteUnavailable,
    SkillSnapshot,
    build_goal_set_form_card,
    build_goal_clear_confirmation_card,
    build_plan_start_form_card,
    build_remote_control_card,
    build_remote_control_entry_card,
    build_skills_card,
    confirmed_goal_clear_command,
    CURRENT_THREAD_CLEAR_COMMAND,
    CURRENT_THREAD_COMMANDS,
    CURRENT_THREAD_CLEAR_LABEL,
    CURRENT_THREAD_SWITCH_COMMAND,
    CURRENT_THREAD_SWITCH_LABEL,
    CURRENT_THREAD_VIEW_COMMAND,
    CURRENT_THREAD_VIEW_LABEL,
    parse_remote_command,
    remote_control_card_action_fingerprints,
)
from .feature_center import (
    FEATURE_CENTER_ENTRY_COMMAND,
    build_feature_center_card,
    build_monitor_settings_card,
    feature_center_card_action_fingerprints,
    feature_center_direct_commands,
    feature_center_text_fallback,
    feature_operation_card_action_fingerprints,
)
from .slash_commands import (
    SLASH_COMMAND_ENTRY_COMMAND,
    SlashCommand,
    build_confirmation_card,
    build_slash_catalog_card,
    build_slash_operations_card,
    parse_slash_command,
    slash_card_action_fingerprints,
)
from .state import StateError, StateStore
from .usage import (
    USAGE_IMAGE_FOOTER,
    USAGE_VERSION,
    feishu_usage_images,
    feishu_usage_text,
)


PAGE_SIZE = 20
LIST_TITLE_MAX_CHARS = 36
LIST_PROJECT_MAX_CHARS = 48
OVERVIEW_TITLE_MAX_CHARS = 80
CURRENT_THREAD_TTL_DAYS = 30
CURRENT_SELECTION_MAX_AGE_SECONDS = 60 * 60
LOGGER = logging.getLogger("progress_wx.codex_management")
_DIRECT_FEATURE_ALIASES = {
    **feature_center_direct_commands(),
    "指令使用": REMOTE_CONTROL_ENTRY_COMMAND,
    "Codex管理": REMOTE_CONTROL_ENTRY_COMMAND,
    "Codex 管理": REMOTE_CONTROL_ENTRY_COMMAND,
}
_LEGACY_TOP_LEVEL_COMMANDS = frozenset({
    SESSION_QUERY_ENTRY_COMMAND,
    REMOTE_CONTROL_ENTRY_COMMAND,
    FEATURE_CENTER_ENTRY_COMMAND,
    SLASH_COMMAND_ENTRY_COMMAND,
    "查询项目列表",
    "查询个人会话",
    "新建项目",
    "新建个人会话",
    "查询监测列表",
    "添加监测任务",
    "移除监测任务",
    "监测设置",
    "开启自动监测",
    "关闭自动监测",
    "重置预警状态",
    "最近预警",
    "使用说明",
    "文字版使用说明",
    "查询剩余额度",
    "搜索会话",
    "查看指令列表",
    "查看当前会话",
    "切换当前会话",
    "清除当前会话",
    "/commands",
    "/project",
    "/task",
    "/local",
    "/worktree",
    *CURRENT_THREAD_COMMANDS,
    CURRENT_THREAD_VIEW_LABEL,
    CURRENT_THREAD_SWITCH_LABEL,
    CURRENT_THREAD_CLEAR_LABEL,
    *_DIRECT_FEATURE_ALIASES,
})
LEGACY_TEXT_COMMANDS = frozenset(command for command in _LEGACY_TOP_LEVEL_COMMANDS if not command.startswith('/'))
TOP_LEVEL_COMMANDS = frozenset(
    command if command.startswith('/') else '.'+command
    for command in _LEGACY_TOP_LEVEL_COMMANDS
)
_BOT_MENU_COMMANDS = frozenset({SESSION_QUERY_ENTRY_COMMAND, REMOTE_CONTROL_ENTRY_COMMAND, FEATURE_CENTER_ENTRY_COMMAND})
_CURRENT_THREAD_INPUT_ALIASES = {
    CURRENT_THREAD_VIEW_COMMAND: CURRENT_THREAD_VIEW_COMMAND,
    CURRENT_THREAD_SWITCH_COMMAND: CURRENT_THREAD_SWITCH_COMMAND,
    CURRENT_THREAD_CLEAR_COMMAND: CURRENT_THREAD_CLEAR_COMMAND,
    CURRENT_THREAD_VIEW_LABEL: CURRENT_THREAD_VIEW_COMMAND,
    CURRENT_THREAD_SWITCH_LABEL: CURRENT_THREAD_SWITCH_COMMAND,
    CURRENT_THREAD_CLEAR_LABEL: CURRENT_THREAD_CLEAR_COMMAND,
}
_PAGE = re.compile(r"^第([1-9][0-9]*)页$")
_EXPAND_PROJECT = re.compile(r"^展开(A[0-9]{2,})$")
_SELECT_PROJECT_THREAD = re.compile(r"^选定(a[0-9]{2,})$")
_SELECT_PERSONAL_THREAD = re.compile(r"^选定(p[0-9]{2,})$")
_ADD_PROJECT_THREAD_MONITOR = re.compile(r"^添加监测(a[0-9]{2,})$")
_ADD_PERSONAL_THREAD_MONITOR = re.compile(r"^添加监测(p[0-9]{2,})$")
_REMOVE_MONITOR = re.compile(r"^移除(m[0-9]{2,})$")
_SELECT_SEARCH_RESULT = re.compile(r"^(?:选择|选定)([1-9][0-9]*)$")
SEARCH_PAGE_SIZE = 3
SEARCH_EMPTY_TITLE = "会话名称暂不可用"
SEARCH_TITLE_FORMAT_VERSION = 2
_ONE_TIME_ACTIONS = {".原文": "raw", ".归档": "archive"}


class ManagementUserError(ValueError):
    """用户输入与所回复消息的上下文不匹配，可修正后重试。"""


@dataclass(frozen=True, slots=True)
class _DesktopSession:
    tools: VerifiedDesktopAppTools
    source_thread_id: str
    listing: Mapping[str, Any]


def _text(value: Any) -> str:
    return str(value or "").strip()


def _is_direct_control_line(value: object) -> bool:
    """Only a literal leading slash/dollar may enter the control plane."""

    # A control candidate is one physical line only.  In particular, a
    # message whose first line happens to start with a slash but contains a
    # second line must remain an ordinary message; otherwise text after the
    # command could accidentally cross the control-plane boundary.
    return (
        type(value) is str
        and value.startswith(("/", "$"))
        and "\r" not in value
        and "\n" not in value
    )


def is_dot_command(value: object) -> bool:
    """A literal ASCII dot reserves the input for bot control, even if invalid."""
    return type(value) is str and value.startswith('.')


def is_direct_management_candidate(value: object) -> bool:
    return (
        type(value) is str and (
            value in TOP_LEVEL_COMMANDS or value in LEGACY_TEXT_COMMANDS
            or is_dot_command(value) or _is_direct_control_line(value)
        )
    )


def _timestamp(value: Any) -> int:
    try:
        number = int(float(value or 0))
    except (TypeError, ValueError):
        return 0
    return number // 1000 if number > 10_000_000_000 else number


_NO_FINAL_RESULT = "最近轮次暂无可展示的最终答复。"


def _canonical_turn_status(value: object) -> str:
    """把结构化 turn 状态归一为管理消息使用的有限标签。"""

    if isinstance(value, Mapping):
        value = value.get("type") or value.get("status")
    raw = _text(getattr(value, "value", value))
    normalized = raw.casefold().replace("_", "").replace("-", "")
    if normalized in {"active", "running", "inprogress"}:
        return "active"
    if normalized == "completed":
        return "completed"
    if normalized == "failed":
        return "failed"
    if normalized in {"cancelled", "canceled", "interrupted", "aborted"}:
        return "cancelled"
    return raw


def _detail_latest_turn(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    turns = payload.get("turns")
    if not isinstance(turns, list):
        return None
    return next((item for item in turns if isinstance(item, Mapping)), None)


def _turn_time_value(turn: object) -> object:
    if isinstance(turn, Mapping):
        completed = turn.get("completedAt", turn.get("completed_at"))
        if completed is not None:
            return completed
        return turn.get("startedAt", turn.get("started_at"))
    completed = getattr(turn, "completed_at", None)
    return completed if completed is not None else getattr(turn, "started_at", None)


def _format_management_time(value: object) -> str:
    seconds = _timestamp(value)
    if not seconds:
        return "未知"
    return datetime.fromtimestamp(seconds, tz=BEIJING).strftime("%Y-%m-%d %H:%M")


def _label(prefix: str, index: int) -> str:
    return f"{prefix}{index:02d}"


def _page_bounds(total: int, requested: int) -> tuple[int, int, int]:
    pages = max(1, math.ceil(total / PAGE_SIZE))
    if not 1 <= requested <= pages:
        raise ManagementUserError(f"只有 {pages} 页，请回复有效页码。")
    start = (requested - 1) * PAGE_SIZE
    return start, min(total, start + PAGE_SIZE), pages


def _clean_line(value: str) -> str:
    return value.strip(" \t\r")


_FORM_PREAMBLE_LINES = {
    "新建 Codex 项目",
    "新建 Codex 个人会话",
    "新建 Codex 项目会话",
    "请回复本消息并保留字段名：",
    "请只填写下面字段并回复本消息；不要添加标题或说明：",
    "搜索 Codex 会话",
    "运行方式请填写“本地”或“工作树”；工作树只支持 Git 项目。",
    "运行方式已固定为“本地”，请保留原样。",
    "运行方式已固定为“工作树”，请保留原样。",
    "“自动”会让 Git 项目使用工作树，其他项目使用本地目录。",
}


def _is_form_preamble_line(line: str) -> bool:
    cleaned = _clean_line(line)
    if cleaned in _FORM_PREAMBLE_LINES:
        return True
    if cleaned.startswith(("操作类型：", "填写说明：", "兼容旧模板：")):
        return True
    return cleaned.startswith("在“") and cleaned.endswith("”中新建 Codex 会话")


def _parse_form(text: str, fields: Sequence[str], prompt_field: str) -> dict[str, str]:
    """解析固定字段表单；最后一个提示词字段之后的正文保持原样。"""

    normalized = str(text).replace("\r\n", "\n").replace("\r", "\n")
    marker = f"{prompt_field}："
    marker_index = normalized.find(marker)
    if marker_index < 0:
        raise ManagementUserError(f"缺少字段“{prompt_field}：”。")
    header = normalized[:marker_index]
    prompt = normalized[marker_index + len(marker):]
    if prompt.startswith("\n"):
        prompt = prompt[1:]
    values: dict[str, str] = {prompt_field: prompt}
    lines = [line for line in header.split("\n") if line.strip()]
    for field in fields:
        prefix = f"{field}："
        matches = [line for line in lines if line.startswith(prefix)]
        if len(matches) != 1:
            raise ManagementUserError(f"字段“{field}：”必须恰好出现一次。")
        values[field] = _clean_line(matches[0][len(prefix):])
    allowed = tuple(f"{field}：" for field in fields)
    extras = [
        line
        for line in lines
        if not line.startswith(allowed) and not _is_form_preamble_line(line)
    ]
    if extras:
        unknown = _compact(_clean_line(extras[0]), 48)
        raise ManagementUserError(
            f"无法识别表单中的这一行：“{unknown}”。"
            "请删除该行，或将内容写在对应字段后面。"
        )
    return values


def _prompt_from_reply(
    text: str,
    fields: Sequence[str],
    *,
    allow_plain: bool,
    plain_error: str,
) -> dict[str, str]:
    """Accept the legacy labelled form or, when routing is fixed, plain text.

    The form marker is recognized only at a physical line start.  This keeps
    an ordinary prompt that happens to mention the field name in its prose in
    the prompt plane while preserving every existing copied-form entry point.
    """

    normalized = str(text).replace("\r\n", "\n").replace("\r", "\n")
    marker = "首轮对话提示词："
    if _has_prompt_form_marker(normalized):
        return _parse_form(normalized, fields, "首轮对话提示词")
    if not allow_plain:
        raise ManagementUserError(plain_error)
    return {"首轮对话提示词": normalized}


def _has_prompt_form_marker(text: str) -> bool:
    return bool(re.search(r"(?:^|\n)首轮对话提示词：", str(text)))


def _compact(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def _message_field(label: str, value: object) -> str:
    """生成一个稳定字段行；粗体边界统一交给飞书富文本转换器。"""

    clean_label = str(label or "").strip().rstrip("：:")
    if not clean_label:
        raise ValueError("消息字段名不能为空")
    return f"{clean_label}：{str(value or '').strip()}"


def _message_blocks(*blocks: Sequence[str]) -> str:
    """按“块间一个空行、块内逐行”生成管理消息正文。"""

    rendered: list[str] = []
    for block in blocks:
        lines = [str(line).rstrip() for line in block if str(line).strip()]
        if not lines:
            continue
        if rendered:
            rendered.append("")
        rendered.extend(lines)
    return "\n".join(rendered)


def _windows_path_key(value: object) -> str:
    """归一化 Codex 记录中的 Windows 扩展路径前缀。"""

    text = str(value or "").strip()
    if text.startswith("\\\\?\\UNC\\"):
        text = "\\\\" + text[8:]
    elif text.startswith("\\\\?\\"):
        text = text[4:]
    return ntpath.normcase(ntpath.normpath(text)) if text else ""


def _fallback_thread_title(item: Mapping[str, Any]) -> str:
    for key in ("title", "name", "summary", "preview"):
        value = _compact(_text(item.get(key)), LIST_TITLE_MAX_CHARS)
        if value:
            return value
    created = _timestamp(item.get("createdAt"))
    if created:
        return f"新建会话（{datetime.fromtimestamp(created):%m-%d %H:%M}）"
    thread_id = _text(item.get("id"))
    return f"新建会话（{thread_id[:8]}）" if thread_id else "新建会话"


def _book_title(value: object, limit: int) -> str:
    """只在飞书展示层加一层中文书名号，原始标题保持不变。"""

    title = _text(value) or SEARCH_EMPTY_TITLE
    if title.startswith("《") and title.endswith("》"):
        title = title[1:-1].strip() or SEARCH_EMPTY_TITLE
    return f"《{_compact(title, limit)}》"


def _latest_final(payload: Mapping[str, Any]) -> str:
    turns = payload.get("turns")
    if not isinstance(turns, list):
        return ""
    legacy_completed_fallback = ""
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        items = turn.get("items")
        if not isinstance(items, list):
            continue
        final = ""
        fallback = ""
        for item in items:
            if not isinstance(item, dict) or item.get("type") != "agentMessage":
                continue
            text = str(item.get("text") or "")
            if not text.strip():
                continue
            fallback = text
            if item.get("phase") == "final_answer":
                final = text
        if final:
            return final
        status = _text(turn.get("status")).casefold()
        if (
            not legacy_completed_fallback
            and fallback
            and status in {"completed", "failed", "interrupted", "cancelled", "canceled"}
        ):
            legacy_completed_fallback = fallback
    return legacy_completed_fallback


class CodexManagementController:
    """无全局菜单状态；每次只按被回复的出站 message_id 路由。"""

    def __init__(
        self,
        *,
        store: StateStore,
        codex_store: CodexStore,
        desktop_client: DesktopAppToolsClient,
        project_registry: CodexProjectRegistry,
        source_thread_ids: Sequence[str],
        send_text: Callable[[str, str], tuple[str, ...]],
        send_card: Callable[[Mapping[str, Any], str], tuple[str, ...]] | None = None,
        send_image: Callable[[bytes, str], tuple[str, ...]] | None = None,
        send_file: Callable[[bytes, str, str], tuple[str, ...]] | None = None,
        account_reader: CodexAccountReader | None = None,
        session_search: SessionSearchEngine | None = None,
        remote_control: AppServerRemoteControl | None = None,
        context_ttl_days: int | None = None,
    ) -> None:
        self.store = store
        self.codex_store = codex_store
        self.desktop_client = desktop_client
        self.project_registry = project_registry
        self.source_thread_ids = tuple(dict.fromkeys(_text(item) for item in source_thread_ids if _text(item)))
        self.send_text = send_text
        self.send_card = send_card
        self.send_image = send_image
        self.send_file = send_file
        self.account_reader = account_reader
        self.session_search = session_search
        self.remote_control = remote_control
        self.context_ttl_days = (
            None if context_ttl_days is None else int(context_ttl_days)
        )
        if not self.source_thread_ids:
            raise ValueError("source_thread_ids 不能为空")
        self._active_owner: ChannelReply | None = None

    def _thread_display_title(
        self, record: ThreadRecord, *preferred_titles: object
    ) -> tuple[str, str]:
        """复用全局标题生命周期；Desktop 独立标题可补本地投影延迟。"""

        preferred = independent_thread_title(record, *preferred_titles)
        if preferred:
            source = (
                "codex_manual"
                if record.title_source == "manual_name"
                else "codex_generated"
            )
            return preferred, source
        cached = self.store.thread_title_recovery(
            record.thread_id, thread_title_recovery_hash(record)
        )
        recovered = str(cached.get("display_title") or "") if cached else ""
        return public_thread_title(record, recovered)

    @staticmethod
    def _require_fresh_selection_context(
        *, owner_bound: bool, context_created_at: int
    ) -> None:
        """A long-lived list card must not silently replace today's target."""

        if not owner_bound:
            raise ManagementUserError(
                "这份会话列表生成于安全绑定升级前，不能设为当前会话；请重新查询。"
            )
        now = int(time.time())
        created_at = int(context_created_at or 0)
        if (
            created_at <= 0
            or created_at > now + 300
            or now - created_at > CURRENT_SELECTION_MAX_AGE_SECONDS
        ):
            raise ManagementUserError(
                "这份会话列表已过期，不能修改当前会话；请重新查询后选择。"
            )

    def _set_current_thread(
        self,
        message: ChannelReply,
        thread: Mapping[str, Any],
    ) -> str:
        sender_id = _text(message.sender_id)
        chat_id = _text(message.chat_id)
        thread_id = _text(thread.get("id"))
        if not sender_id or not chat_id:
            raise ManagementUserError(
                "当前会话只能在已验证的机器人私聊中设置，请重新打开会话列表。"
            )
        if not thread_id:
            raise ManagementUserError("选中的会话缺少任务身份，请重新查询。")
        title = _text(thread.get("title")) or "未命名会话"
        self.store.set_current_thread(
            sender_id,
            chat_id,
            thread_id,
            title,
            ttl_days=CURRENT_THREAD_TTL_DAYS,
        )
        return title

    def _create_remote_context(
        self, message: ChannelReply, thread: Mapping[str, Any], group: str
    ) -> str:
        sender_id = _text(message.sender_id)
        chat_id = _text(message.chat_id)
        if not sender_id or not chat_id:
            raise ManagementUserError("远程指令只能在已验证的机器人私聊中执行。")
        return self.store.create_management_context(
            "remote_control",
            self._remote_payload(thread, group),
            sender_id=sender_id,
            chat_id=chat_id,
            ttl_days=CURRENT_THREAD_TTL_DAYS,
        )

    def accepts(self, message: ChannelReply) -> bool:
        if not message.reply_to_message_id:
            return is_direct_management_candidate(message.content)
        return (
            self.store.management_context_for_message(message.reply_to_message_id) is not None
            or (is_dot_command(message.content) and message.content != '.原文')
        )

    def handle(self, message: ChannelReply) -> None:
        status_reader = getattr(self.store, "management_inbound_status", None)
        if callable(status_reader):
            status = status_reader(
                message.message_id,
                sender_id=message.sender_id,
                content=message.content,
            )
            if status == "accepted":
                return
            if status == "conflict":
                raise ManagementUserError(
                    "同一条入站消息的发送者或内容发生冲突，不能重复执行。"
                )
            if status == "missing" and not self.store.reserve_management_inbound(
                message.message_id, message.sender_id, message.content
            ):
                # Another callback won the reservation.  Re-read it so a
                # concurrent/replayed event is only allowed to continue when
                # the durable row still belongs to this exact sender/content.
                status = status_reader(
                    message.message_id,
                    sender_id=message.sender_id,
                    content=message.content,
                )
                if status == "accepted":
                    return
                if status == "conflict":
                    raise ManagementUserError(
                        "同一条入站消息的发送者或内容发生冲突，不能重复执行。"
                    )
                if status != "pending":
                    return
        elif not self.store.reserve_management_inbound(
            message.message_id, message.sender_id, message.content
        ):
            return
        previous_owner, self._active_owner = self._active_owner, message
        try:
            if message.reply_to_message_id:
                context = self.store.management_context_record_for_message(
                    message.reply_to_message_id
                )
                if context is None:
                    if is_dot_command(message.content):
                        self._handle_top(message)
                        self.store.complete_management_inbound(message.message_id)
                        return
                    raise ManagementUserError("这条机器人消息的操作上下文已过期，请重新查询。")
                if message.source_kind == "card_action":
                    card_actions = context.payload.get("_card_action_fingerprints")
                    if (
                        context.payload.get("_card_source")
                        not in {"owner_dm", "owner_open_id_direct"}
                        or not isinstance(card_actions, list)
                        or type(message.action_fingerprint) is not str
                        or message.action_fingerprint not in card_actions
                        or not context.sender_id
                        or not message.chat_id
                    ):
                        raise ManagementUserError(
                            "这次卡片操作无法验证来源，请重新发送查询命令。"
                        )
                    if not context.chat_id:
                        if not self.store.bind_management_context_chat(
                            context.context_id,
                            message.sender_id,
                            message.chat_id,
                        ):
                            raise ManagementUserError(
                                "这次卡片操作无法绑定当前私聊，请重新发送查询命令。"
                            )
                        refreshed = self.store.management_context_record_for_message(
                            message.reply_to_message_id
                        )
                        if refreshed is None:
                            raise ManagementUserError(
                                "这条机器人消息的操作上下文已过期，请重新查询。"
                            )
                        context = refreshed
                if context.sender_id and context.sender_id != message.sender_id:
                    raise ManagementUserError("这条查询结果不属于当前发送者，不能执行操作。")
                if context.chat_id and context.chat_id != message.chat_id:
                    raise ManagementUserError("这条查询结果不属于当前聊天，不能执行操作。")
                if is_dot_command(message.content) and message.content not in _ONE_TIME_ACTIONS:
                    self._handle_top(message)
                    self.store.complete_management_inbound(message.message_id)
                    return
                if message.content in _ONE_TIME_ACTIONS and context.context_kind not in {'thread_overview','thread_reply_form'}:
                    raise ManagementUserError('请引用任务概览使用这条指令；没有发送任务正文。')
                self._handle_context(
                    message,
                    context.context_id,
                    context.context_kind,
                    context.payload,
                    owner_bound=bool(context.sender_id and context.chat_id),
                    context_created_at=context.created_at,
                )
            else:
                self._handle_top(message)
            self.store.complete_management_inbound(message.message_id)
        finally:
            self._active_owner = previous_owner

    def send_user_error(self, message: ChannelReply, details: str) -> None:
        text = f"没有执行。{details}\n\n请修改后继续回复原消息，或重新发送入口命令。"
        context = self.store.management_context_record_for_message(
            message.reply_to_message_id
        )
        if context is not None:
            # 错误回执只追加一个新的飞书 message_id，不复制查询载荷。尤其不能
            # 把冻结原文复制进默认永久的错误上下文；继续引用本回执时仍由原
            # context_id 做 owner、一次性动作和 30 天最小化生命周期约束。
            self._respond_in_context(
                context.context_id,
                text,
                f"management-error:{message.message_id}",
            )
            return
        self._respond(
            text,
            "management_error",
            {},
            f"management-error:{message.message_id}",
            owner=message,
        )

    def send_system_error(self, message: ChannelReply) -> None:
        self._respond(
            "Codex Desktop 当前无法完成这项操作；没有创建会话，也没有发送提示词。"
            "请确认 Codex 桌面端正在运行后重新发送。",
            "management_error",
            {},
            f"management-system-error:{message.message_id}",
            owner=message,
        )

    def _respond(
        self,
        text: str,
        kind: str,
        payload: Mapping[str, Any],
        key: str,
        *,
        owner: ChannelReply | None = None,
        ttl_days: int | None = None,
    ) -> str:
        actual_owner = owner or self._active_owner
        context_id = self.store.create_management_context(
            kind,
            payload,
            sender_id=actual_owner.sender_id if actual_owner is not None else "",
            chat_id=actual_owner.chat_id if actual_owner is not None else "",
            ttl_days=(self.context_ttl_days if ttl_days is None else ttl_days),
        )
        message_ids = self.send_text(text, key)
        if not message_ids:
            raise DesktopAppToolsError("飞书渠道未返回可绑定的 message_id")
        self.store.bind_management_messages(context_id, message_ids)
        return context_id

    def _respond_in_context(self, context_id: str, text: str, key: str) -> tuple[str, ...]:
        """发送回执并绑定回原查询，不创建新的动作次数。"""

        message_ids = self.send_text(text, key)
        if not message_ids:
            raise DesktopAppToolsError("飞书渠道未返回可绑定的 message_id")
        self.store.bind_management_messages(context_id, message_ids)
        return tuple(message_ids)

    def _bind_text_response_context(
        self,
        kind: str,
        payload: Mapping[str, Any],
        message_ids: Sequence[str],
        *,
        actual_owner: ChannelReply | None,
        ttl_days: int | None,
    ) -> str:
        """绑定文字回执；文字回执永远不带卡片动作指纹。"""

        context_id = self.store.create_management_context(
            kind,
            payload,
            sender_id=actual_owner.sender_id if actual_owner is not None else "",
            chat_id=actual_owner.chat_id if actual_owner is not None else "",
            ttl_days=(self.context_ttl_days if ttl_days is None else ttl_days),
        )
        self.store.bind_management_messages(context_id, message_ids)
        return context_id

    def _respond_card(
        self,
        card: Mapping[str, Any],
        kind: str,
        payload: Mapping[str, Any],
        key: str,
        *,
        fallback_text: str | None = None,
        ttl_days: int | None = None,
    ) -> str:
        """发送卡片并像文字消息一样绑定 owner 与引用上下文。"""

        if self.send_card is None:
            return self._respond(
                fallback_text or session_query_text_menu(),
                kind,
                payload,
                key,
                ttl_days=ttl_days,
            )
        actual_owner = self._active_owner
        bound_payload = dict(payload)
        bound_payload["_card_source"] = (
            "owner_dm"
            if actual_owner is not None and actual_owner.chat_id
            else "owner_open_id_direct"
        )
        bound_payload["_card_action_fingerprints"] = list(
            dict.fromkeys(
                (
                    *session_query_card_action_fingerprints(card),
                    *remote_control_card_action_fingerprints(card),
                    *feature_center_card_action_fingerprints(card),
                    *feature_operation_card_action_fingerprints(card),
                    *slash_card_action_fingerprints(card),
                )
            )
        )
        try:
            message_ids = self.send_card(card, key)
        except MessageChannelPayloadRejectedError as exc:
            # 飞书已经明确拒绝卡片，证明该 idempotency key 没有产生出站消息；
            # 此时改用文字兜底是安全的。文字回执必须走普通上下文，不能
            # 携带卡片来源/动作指纹，否则会把不存在的卡片冒充为卡片成功，
            # 也会让伪造的 card_action 绑定到这条文字回执。未知结果或网络
            # 超时不走这里，避免重复。
            LOGGER.warning(
                "Codex 管理卡片被渠道明确拒绝，改用文字兜底 "
                "kind=%s error=%s",
                kind,
                type(exc).__name__,
            )
            message_ids = self.send_text(
                fallback_text or session_query_text_menu(),
                f"{key}:card-format-fallback",
            )
            if not message_ids:
                raise DesktopAppToolsError("飞书渠道未返回可绑定的文字 message_id")
            return self._bind_text_response_context(
                kind,
                payload,
                message_ids,
                actual_owner=actual_owner,
                ttl_days=ttl_days,
            )
        if not message_ids:
            raise DesktopAppToolsError("飞书渠道未返回可绑定的卡片 message_id")
        context_id = self.store.create_management_context(
            kind,
            bound_payload,
            sender_id=actual_owner.sender_id if actual_owner is not None else "",
            chat_id=actual_owner.chat_id if actual_owner is not None else "",
            ttl_days=(self.context_ttl_days if ttl_days is None else ttl_days),
        )
        self.store.bind_management_messages(context_id, message_ids)
        return context_id

    @staticmethod
    def _continued_overview_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
        """续聊/监测回执只保留线程定位，绝不复制一次性查询快照。"""

        return {
            "thread": dict(payload.get("thread") or {}),
            "group": _text(payload.get("group")) or "个人会话",
        }

    def _open_desktop(self, required: Sequence[str]) -> _DesktopSession:
        required_tools = tuple(dict.fromkeys(("list_threads", *required)))
        tools = self.desktop_client.open_verified(required_tools=required_tools)
        errors: list[BaseException] = []
        for source_thread_id in self.source_thread_ids:
            try:
                listing = tools.list_threads(source_thread_id, limit=50, call_tag="management-list")
                return _DesktopSession(tools, source_thread_id, listing)
            except DesktopAppToolsError as exc:
                errors.append(exc)
        tools.close()
        raise DesktopAppToolsUnavailable("没有已加载的 Codex 会话可作为管理调用来源") from (
            errors[-1] if errors else None
        )

    @staticmethod
    def _desktop_items(listing: Mapping[str, Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for key in ("pinnedThreads", "threads"):
            raw = listing.get(key)
            if not isinstance(raw, list):
                continue
            for item in raw:
                if not isinstance(item, dict):
                    continue
                thread_id = _text(item.get("id"))
                if not thread_id or thread_id in seen:
                    continue
                seen.add(thread_id)
                result.append(dict(item))
        return result

    def _catalog(self, listing: Mapping[str, Any]) -> tuple[list[dict[str, Any]], Any]:
        registry = self.project_registry.snapshot()
        desktop = {item["id"]: item for item in self._desktop_items(listing) if item.get("kind") == "codex"}
        local_records = self.codex_store.select_threads(include_archived=False)
        self.codex_store.require_readable("列出 Codex 会话")
        project_roots = [
            (project.project_id, _windows_path_key(root))
            for project in registry.projects
            for root in project.root_paths
            if _windows_path_key(root)
        ]

        def project_id_for(record: ThreadRecord, item: Mapping[str, Any]) -> str | None:
            explicit = registry.thread_assignments.get(record.thread_id)
            if explicit:
                return explicit
            desktop_project = _text(item.get("projectId"))
            if desktop_project:
                return desktop_project
            if record.thread_id in registry.projectless_thread_ids:
                return None
            cwd = _windows_path_key(record.cwd)
            matches = [
                (project_id, root)
                for project_id, root in project_roots
                if cwd == root or cwd.startswith(root + "\\")
            ]
            return max(matches, key=lambda pair: len(pair[1]))[0] if matches else None

        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for record in local_records:
            # state_5.sqlite 同时保存内部多代理工作线程；它们不是用户在侧栏
            # 创建或管理的会话，不能出现在项目/个人列表中。
            if record.thread_source == "subagent":
                continue
            item = dict(desktop.get(record.thread_id) or {})
            item.setdefault("id", record.thread_id)
            item.setdefault("kind", "codex")
            item.setdefault("hostId", "local")
            display_title, title_origin = self._thread_display_title(
                record, item.get("title"), item.get("name")
            )
            item["title"] = display_title
            item["titleOrigin"] = title_origin
            item.setdefault("summary", record.preview)
            item.setdefault("cwd", record.cwd)
            item.setdefault("updatedAt", _timestamp(record.updated_at_ms))
            item["projectId"] = project_id_for(record, item)
            item["title"] = _fallback_thread_title(item)
            result.append(item)
            seen.add(record.thread_id)
        for thread_id, item in desktop.items():
            if thread_id in seen:
                continue
            copied = dict(item)
            copied["projectId"] = registry.thread_assignments.get(
                thread_id, copied.get("projectId")
            )
            copied["title"] = _fallback_thread_title(copied)
            result.append(copied)
        result.sort(key=lambda item: _timestamp(item.get("updatedAt")), reverse=True)
        return result, registry

    def _handle_top(self, message: ChannelReply, *, contextual: bool = False) -> None:
        raw = message.content
        if contextual:
            command = raw
        elif message.source_kind == 'bot_menu':
            if raw not in _BOT_MENU_COMMANDS:
                raise ManagementUserError('这不是受支持的固定菜单操作。')
            command = raw
        elif is_dot_command(raw):
            if raw[1:] not in LEGACY_TEXT_COMMANDS:
                if raw in {'.发送','.取消'}:
                    detail = '当前没有可处理的暂存图片；请先引用任务消息发送图片。'
                elif raw in _ONE_TIME_ACTIONS:
                    detail = '请引用对应的任务消息使用这条指令。'
                else:
                    detail = '无法识别这条机器人指令。请发送“.功能中心”查看入口。'
                self._respond(detail+'没有向 Codex 发送正文。', 'management_error', {}, f'management-dot-invalid:{message.message_id}')
                return
            command = raw[1:]
        elif raw in LEGACY_TEXT_COMMANDS:
            self._respond(f'机器人文字操作现在以半角 . 开头。请发送“.{raw}”。本次没有执行操作，也没有发送任务正文。',
                'management_error', {}, f'management-dot-required:{message.message_id}')
            return
        else:
            command = raw
        if message.attachments and not contextual:
            raise ManagementUserError('机器人入口指令请单独发送；图片没有作为任务正文提交。')
        command = _DIRECT_FEATURE_ALIASES.get(command, command)
        binding_command = _CURRENT_THREAD_INPUT_ALIASES.get(command)
        if binding_command is not None:
            self._handle_current_thread_command(message, binding_command)
            return
        if command == FEATURE_CENTER_ENTRY_COMMAND:
            self._respond_card(
                build_feature_center_card(),
                "feature_center",
                {"version": 1},
                f"management-feature-center:{message.message_id}",
                fallback_text=feature_center_text_fallback(),
            )
            return
        slash = parse_slash_command(command)
        if slash is not None and slash.kind in {"catalog", "catalog_page"}:
            page = int(slash.argument or "1")
            self._respond_card(
                build_slash_catalog_card(page),
                "slash_catalog",
                {"page": page},
                f"management-slash-catalog:{message.message_id}:{page}",
                fallback_text=self._slash_catalog_text(page),
            )
            return
        if slash is not None and slash.kind in {
            "existing_project", "existing_local", "existing_task", "existing_worktree"
        }:
            if slash.kind == "existing_task":
                self._new_personal_form(message.message_id)
            else:
                self._open_new_project_task_selector(
                    message.message_id,
                    environment_mode=(
                        "local"
                        if slash.kind == "existing_local"
                        else "worktree"
                        if slash.kind == "existing_worktree"
                        else "choose"
                    ),
                )
            return
        remote_text = command if (
            parse_remote_command(command) is not None
            or (slash is not None and slash.kind not in {"unavailable"})
        ) else ""
        if remote_text:
            self._dispatch_bound_remote_command(message, remote_text)
            return
        if slash is not None and slash.kind == "unavailable":
            self._respond(
                f"{slash.argument} 当前不能从飞书安全执行。\n原因：{slash.secondary}",
                "slash_unavailable",
                {},
                f"management-slash-unavailable:{message.message_id}:{slash.argument}",
            )
            return
        if _is_direct_control_line(command):
            self._respond(
                "无法识别或解析这条行首控制指令；没有执行。请发送“.查看指令列表”或 /commands。",
                "slash_invalid",
                {},
                f"management-slash-invalid:{message.message_id}",
            )
            return
        if command == REMOTE_CONTROL_ENTRY_COMMAND:
            status, target, group, _binding = self._resolve_current_thread(message)
            if status == "ok" and target is not None:
                self._send_remote_control_card(target, group, message.message_id)
            elif status == "none":
                self._send_remote_target_selector(message.message_id)
            elif status == "stale":
                raise ManagementUserError(
                    "当前绑定的 Codex 会话已不存在或已归档；请先发送“.切换会话”。"
                )
            elif status == "owner_missing":
                raise ManagementUserError(
                    "当前私聊身份未完成绑定；请在一对一私聊中重试。"
                )
            else:
                raise ManagementUserError(
                    "当前绑定暂时无法验证；请稍后重试，或发送“.切换会话”。"
                )
            return
        if command == SESSION_QUERY_ENTRY_COMMAND:
            self._respond_card(
                build_session_query_card(),
                "session_query_menu",
                {"version": 1},
                f"management-session-query-menu:{message.message_id}",
            )
            return
        if command == "查询剩余额度":
            if self.account_reader is None:
                raise ManagementUserError("当前版本尚未启用额度查询。")
            try:
                snapshot = self.account_reader.read()
            except CodexAccountError:
                self._respond(
                    "当前未能从 Codex 官方服务读取额度。没有使用缓存数据，也没有估算；"
                    "请稍后重新发送“.查询剩余额度”。",
                    "account_rate_limits",
                    {},
                    f"management-rate-limits-error:{message.message_id}",
                )
            else:
                self._respond(
                    format_rate_limits(snapshot),
                    "account_rate_limits",
                    {},
                    f"management-rate-limits:{message.message_id}",
                )
            return
        if command == "搜索会话":
            if self.session_search is None:
                raise ManagementUserError("当前版本尚未启用会话搜索。")
            self._search_form(message.message_id)
            return
        if command == "监测设置":
            self._show_monitor_settings(message.message_id)
            return
        if command in {"开启自动监测", "关闭自动监测"}:
            self._show_monitor_settings_confirmation(
                message.message_id,
                desired=(command == "开启自动监测"),
            )
            return
        if command == "重置预警状态":
            self._send_reset_alert_status(message.message_id)
            return
        if command == "最近预警":
            self._send_recent_reset_alerts(message.message_id)
            return
        if command == "使用说明":
            context_id = self.store.create_management_context(
                "usage_guide", {}, ttl_days=self.context_ttl_days
            )
            if self._send_usage_images(context_id, message.message_id):
                footer_ids = self.send_text(
                    USAGE_IMAGE_FOOTER,
                    f"management-usage-footer:{message.message_id}:{USAGE_VERSION}",
                )
                if not footer_ids:
                    raise DesktopAppToolsError("飞书渠道未返回使用说明提示 message_id")
                self.store.bind_management_messages(context_id, footer_ids)
            return
        if command == "文字版使用说明":
            self._respond(
                feishu_usage_text(),
                "usage_guide",
                {},
                f"management-usage-text:{message.message_id}",
            )
            return
        if command == "添加监测任务":
            self._monitor_form("add", message.message_id)
            return
        if command == "移除监测任务":
            self._monitor_form("remove", message.message_id)
            return
        if command == "新建项目":
            self._new_project_form(message.message_id)
            return
        if command == "新建个人会话":
            self._new_personal_form(message.message_id)
            return
        session = self._open_desktop(("list_projects",))
        try:
            catalog, registry = self._catalog(session.listing)
            if command == "查询项目列表":
                projects_payload = session.tools.list_projects(session.source_thread_id)
                projects = self._project_snapshot(projects_payload, registry.projects, catalog)
                self._send_project_page(projects, 1, message.message_id)
            elif command == "查询个人会话":
                personal = [item for item in catalog if not _text(item.get("projectId"))]
                self._send_personal_page(personal, 1, message.message_id)
            elif command == "查询监测列表":
                self._send_monitor_page(catalog, registry, 1, message.message_id)
            else:
                raise ManagementUserError("不是已启用的精确入口命令。")
        finally:
            session.tools.close()

    def _resolve_current_thread(
        self, message: ChannelReply
    ) -> tuple[str, Mapping[str, Any] | None, str, Mapping[str, Any] | None]:
        """Resolve one owner/chat binding against a fresh non-archived catalog.

        The binding table is only a routing hint.  Every use rechecks the
        owner-scoped expiry and then resolves the stored task id from the
        current Codex catalog.  A missing/archived target is deliberately
        distinct from an unavailable Desktop so callers can fail closed with
        a useful short explanation without leaking an id or path.
        """

        sender = _text(message.sender_id)
        chat = _text(message.chat_id)
        if not sender or not chat:
            return "owner_missing", None, "", None
        try:
            binding = self.store.current_thread(sender, chat)
        except ValueError:
            return "owner_missing", None, "", None
        except StateError:
            return "unavailable", None, "", None
        if binding is None:
            return "none", None, "", None
        thread_id = _text(binding.get("thread_id"))
        if not thread_id:
            return "stale", None, "", binding
        session: _DesktopSession | None = None
        try:
            session = self._open_desktop(())
            catalog, registry = self._catalog(session.listing)
            target = next(
                (
                    dict(item)
                    for item in catalog
                    if _text(item.get("id")) == thread_id
                    and _text(item.get("kind")) in {"", "codex"}
                ),
                None,
            )
            if target is None:
                self.store.clear_current_thread(
                    sender, chat, expected_thread_id=thread_id
                )
                return "stale", None, "", binding
            if target.get("archived") is True or target.get("isArchived") is True:
                self.store.clear_current_thread(
                    sender, chat, expected_thread_id=thread_id
                )
                return "stale", None, "", binding
            project_id = _text(target.get("projectId"))
            project_names = {
                _text(item.project_id): _text(item.name)
                for item in registry.projects
                if _text(item.project_id)
            }
            group = (
                project_names.get(project_id, "项目会话")
                if project_id
                else "个人会话"
            )
            # A binding is an inactivity lease, not an immutable title cache.
            # Refresh it only after the authoritative current catalog confirms
            # that the exact task is still visible and not archived.
            self.store.set_current_thread(
                sender,
                chat,
                thread_id,
                _text(target.get("title")) or "未命名会话",
                ttl_days=CURRENT_THREAD_TTL_DAYS,
            )
            return "ok", target, group, binding
        except (
            DesktopAppToolsError,
            ProjectRegistryError,
            CodexStoreReadError,
            OSError,
            TimeoutError,
        ):
            return "unavailable", None, "", binding
        finally:
            if session is not None:
                session.tools.close()

    def _handle_current_thread_command(
        self,
        message: ChannelReply,
        command: str,
        *,
        expected_thread_id: str | None = None,
    ) -> None:
        """View, switch, or clear the owner/chat-scoped current binding."""

        sender = _text(message.sender_id)
        chat = _text(message.chat_id)
        if not sender or not chat:
            self._respond(
                "当前私聊身份未完成绑定；本次没有执行，请在一对一私聊中重试。",
                "current_binding_error",
                {},
                f"management-current-thread-owner-missing:{message.message_id}",
            )
            return
        if command == CURRENT_THREAD_SWITCH_COMMAND:
            self._send_remote_target_selector(
                message.message_id,
                selection_mode="binding_switch",
            )
            return
        if command == CURRENT_THREAD_CLEAR_COMMAND:
            cleared = self.store.clear_current_thread(
                sender,
                chat,
                expected_thread_id=expected_thread_id,
            )
            if expected_thread_id and not cleared:
                text = "当前绑定已经变化，本次没有清除；请重新发送“.当前会话”确认。"
            elif cleared:
                text = "已清除当前会话绑定；下一条远程指令会先请你选择会话。"
            else:
                text = "当前没有会话绑定，本次没有执行。"
            self._respond(
                text,
                "current_binding",
                {},
                f"management-current-thread-clear:{message.message_id}",
            )
            return
        status, target, group, binding = self._resolve_current_thread(message)
        if status == "none":
            text = "当前没有绑定 Codex 会话。请发送“.切换会话”选择。"
        elif status == "ok" and target is not None:
            title = _compact(
                _text(target.get("title"))
                or _text((binding or {}).get("title"))
                or "未命名会话",
                OVERVIEW_TITLE_MAX_CHARS,
            )
            state = _compact(_text(target.get("status")) or "未知", 32)
            text = (
                f"当前会话：{_book_title(title, OVERVIEW_TITLE_MAX_CHARS)}\n"
                f"归属：{_text(group) or '个人会话'}\n"
                f"状态：{state}\n\n"
                "发送“.切换会话”更换目标，或发送“.清除会话绑定”解除绑定。"
            )
        elif status == "stale":
            text = (
                "当前绑定的 Codex 会话已不存在或已归档，绑定已清除；本次没有执行。"
                "请发送“.切换会话”重新选择。"
            )
        else:
            text = (
                "当前绑定存在，但暂时无法验证目标会话；本次没有执行。"
                "请稍后重试，或发送“.切换会话”重新选择。"
            )
        self._respond(
            text,
            "current_binding",
            {},
            f"management-current-thread-view:{message.message_id}",
        )

    def _dispatch_bound_remote_command(
        self, message: ChannelReply, command: str
    ) -> None:
        """Run a slash command on the current target or open one selector."""

        status, target, group, _binding = self._resolve_current_thread(message)
        if status == "none":
            self._send_remote_target_selector(
                message.message_id,
                pending_command=command,
            )
            return
        if status == "ok" and target is not None:
            context_id = self._create_remote_context(message, target, group)
            self._handle_remote_control(
                message,
                context_id,
                self._remote_payload(target, group),
                command,
                owner_bound=True,
            )
            return
        if status == "stale":
            text = (
                "当前绑定的 Codex 会话已不存在或已归档；本次指令没有执行。"
                "请先发送“.切换会话”。"
            )
        elif status == "owner_missing":
            text = "当前私聊身份未完成绑定；本次指令没有执行，请在一对一私聊中重试。"
        else:
            text = (
                "当前绑定存在，但暂时无法验证目标会话；本次指令没有执行。"
                "请稍后重试或发送“.切换会话”。"
            )
        self._respond(
            text,
            "current_binding_error",
            {},
            f"management-current-thread-dispatch-error:{message.message_id}",
        )

    @staticmethod
    def _project_snapshot(
        payload: Mapping[str, Any],
        local_projects: Sequence[LocalProject],
        catalog: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        local_by_id = {item.project_id: item for item in local_projects}
        raw = payload.get("projects")
        source = raw if isinstance(raw, list) else []
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in source:
            if not isinstance(item, dict):
                continue
            project_id = _text(item.get("projectId"))
            if not project_id or project_id in seen or item.get("projectKind") != "local":
                continue
            seen.add(project_id)
            local = local_by_id.get(project_id)
            result.append({
                "project_id": project_id,
                "name": _text(item.get("label")) or (local.name if local else "未命名项目"),
                "path": _text(item.get("path")) or (local.root_paths[0] if local else ""),
                "host_id": _text(item.get("hostId")) or "local",
                "is_git": bool(item.get("isGitRepository")),
                "thread_count": sum(1 for thread in catalog if thread.get("projectId") == project_id),
            })
        for local in local_projects:
            if local.project_id in seen:
                continue
            result.append({
                "project_id": local.project_id,
                "name": local.name,
                "path": local.root_paths[0],
                "host_id": "local",
                "is_git": (Path(local.root_paths[0]) / ".git").exists(),
                "thread_count": sum(1 for thread in catalog if thread.get("projectId") == local.project_id),
            })
        for index, item in enumerate(result, start=1):
            item["label"] = _label("A", index)
        return result

    def _send_project_page(
        self,
        projects: Sequence[Mapping[str, Any]],
        page: int,
        request_id: str,
        *,
        selection_mode: str = "",
        pending_command: str = "",
        selection_started_at: int | None = None,
        selection_guard_context_id: str = "",
    ) -> None:
        selection_origin = int(selection_started_at or time.time())
        start, end, pages = _page_bounds(len(projects), page)
        lines = [
            "列表类型：Codex 项目",
            f"页码：第 {page}/{pages} 页",
            f"总数：{len(projects)} 个",
            "",
            "项目列表：",
        ]
        if not projects:
            lines.append("目前没有项目。")
        else:
            for item in projects[start:end]:
                name = _compact(_text(item.get("name")) or "未命名项目", LIST_PROJECT_MAX_CHARS)
                lines.append(
                    f"{item['label']}｜{name}｜{item['thread_count']} 个会话"
                )
        lines.extend((
            "",
            "操作说明：",
            "- 展开项目：回复“展开A01”",
            "- 翻页：回复“第2页”",
            "- 也可回复“新建项目”或“新建项目会话”",
        ))
        text = "\n".join(lines)
        payload = {
            "projects": list(projects),
            "page": page,
            "selection_mode": selection_mode,
            "pending_command": pending_command,
            "selection_started_at": selection_origin,
            "selection_guard_context_id": _text(selection_guard_context_id),
        }
        self._respond_card(
            build_project_list_card(
                list(projects[start:end]),
                page=page,
                pages=pages,
                total=len(projects),
            ),
            "project_list",
            payload,
            f"management-project-list:{request_id}:{page}",
            fallback_text=text,
        )

    def _send_personal_page(
        self,
        threads: Sequence[Mapping[str, Any]],
        page: int,
        request_id: str,
        *,
        selection_mode: str = "",
        pending_command: str = "",
        selection_started_at: int | None = None,
        selection_guard_context_id: str = "",
    ) -> None:
        selection_origin = int(selection_started_at or time.time())
        snapshot = [dict(item) for item in threads]
        for index, item in enumerate(snapshot, start=1):
            item["label"] = _label("p", index)
        start, end, pages = _page_bounds(len(snapshot), page)
        lines = [
            "列表类型：Codex 个人会话",
            f"页码：第 {page}/{pages} 页",
            f"总数：{len(snapshot)} 个",
            "",
            "会话列表：",
        ]
        if not snapshot:
            lines.append("目前没有个人会话。")
        else:
            for item in snapshot[start:end]:
                title = _compact(_text(item.get("title")) or "未命名会话", LIST_TITLE_MAX_CHARS)
                lines.append(f"{item['label']}｜{title}")
        lines.extend((
            "",
            "操作说明：",
            "- 选择会话：回复“选定p01”",
            "- 永久监测：回复“添加监测p01”",
            "- 翻页：回复“第2页”",
            "- 也可回复“新建个人会话”",
        ))
        text = "\n".join(lines)
        payload = {
            "threads": snapshot,
            "page": page,
            "selection_mode": selection_mode,
            "pending_command": pending_command,
            "selection_started_at": selection_origin,
            "selection_guard_context_id": _text(selection_guard_context_id),
        }
        self._respond_card(
            build_thread_list_card(
                snapshot[start:end],
                page=page,
                pages=pages,
                total=len(snapshot),
            ),
            "personal_list",
            payload,
            f"management-personal-list:{request_id}:{page}",
            fallback_text=text,
        )

    def _send_monitor_page(
        self,
        catalog: Sequence[Mapping[str, Any]],
        registry: Any,
        page: int,
        request_id: str,
    ) -> None:
        catalog_by_id = {_text(item.get("id")): item for item in catalog}
        project_names = {item.project_id: item.name for item in registry.projects}
        snapshot: list[dict[str, Any]] = []
        now = int(time.time())
        for index, subscription in enumerate(self.store.monitor_subscriptions(now=now), start=1):
            thread_id = str(subscription["thread_id"])
            thread = catalog_by_id.get(thread_id, {})
            project_id = _text(thread.get("projectId"))
            expires_at = subscription["expires_at"]
            snapshot.append({
                "label": _label("m", index),
                "thread_id": thread_id,
                "title": _fallback_thread_title(thread) if thread else f"任务 {thread_id[:8]}",
                "group": project_names.get(project_id, "个人会话"),
                "origin": str(subscription["origin"]),
                "last_activity_at": int(subscription["last_activity_at"]),
                "expires_at": expires_at,
            })
        start, end, pages = _page_bounds(len(snapshot), page)
        lines = [
            "列表类型：Codex 监测任务",
            f"页码：第 {page}/{pages} 页",
            f"总数：{len(snapshot)} 个",
            "",
            "会话列表：",
        ]
        if not snapshot:
            lines.append("目前没有监测任务。")
        for item in snapshot[start:end]:
            origin = "手动永久" if item["origin"] == "manual" else "自动"
            lines.append(
                f"{item['label']}｜{_compact(item['title'], LIST_TITLE_MAX_CHARS)}｜"
                f"{item['group']}｜{origin}"
            )
        lines.extend((
            "",
            "操作说明：",
            "- 移除监测：回复“移除m01”",
            "- 翻页：回复“第2页”",
            "- 添加任务：发送入口命令“.添加监测任务”",
        ))
        self._respond(
            "\n".join(lines),
            "monitor_list",
            {"items": snapshot, "page": page},
            f"management-monitor-list:{request_id}:{page}",
        )

    def _handle_context(
        self,
        message: ChannelReply,
        context_id: str,
        kind: str,
        payload: Mapping[str, Any],
        *,
        owner_bound: bool,
        context_created_at: int | None = None,
    ) -> None:
        # Preserve leading whitespace for slash strictness.  Human-readable
        # card replies still use the historical trim behavior.
        raw_content = message.content
        if raw_content.startswith(("/", "$")):
            content = raw_content.rstrip()
        elif raw_content.lstrip().startswith(("/", "$")):
            # A leading space is meaningful: it keeps the text in the ordinary
            # prompt plane.  Never strip it into an executable control command.
            content = raw_content.rstrip()
        else:
            content = raw_content.strip()
        if kind == "feature_center":
            # Feishu 已把严格 action 映射成真实文字命令；这里复用唯一顶层控制器。
            self._handle_top(message, contextual=True)
        elif kind == "current_binding":
            if (
                _CURRENT_THREAD_INPUT_ALIASES.get(content) is None
                and not _is_direct_control_line(content)
            ):
                raise ManagementUserError(
                    "请发送“.查看当前会话”“.切换当前会话”“.清除当前会话”，"
                    "或发送一条精确的行首控制指令。"
                )
            self._handle_top(message, contextual=True)
        elif kind == "slash_catalog":
            slash = parse_slash_command(content)
            if slash is None or slash.kind not in {"catalog", "catalog_page"}:
                raise ManagementUserError("请点击斜杠目录的翻页按钮，或重新发送“.斜杠指令”。")
            page = int(slash.argument or "1")
            self._respond_card(
                build_slash_catalog_card(page),
                "slash_catalog",
                {"page": page},
                f"management-slash-catalog:{message.message_id}:{page}",
                fallback_text=self._slash_catalog_text(page),
            )
        elif kind == "remote_control_menu":
            self._handle_remote_control_menu(
                message,
                context_id,
                payload,
                content,
                owner_bound=owner_bound,
                context_created_at=context_created_at,
            )
        elif kind in {
            "remote_control",
            "remote_goal_set_form",
            "remote_plan_start_form",
        }:
            self._handle_remote_control(
                message, context_id, payload, content, owner_bound=owner_bound
            )
        elif kind == "remote_goal_clear_confirm":
            self._handle_remote_goal_clear_confirmation(
                message, context_id, payload, content, owner_bound=owner_bound
            )
        elif kind == "monitor_settings":
            self._handle_monitor_settings(
                message,
                context_id,
                payload,
                content,
                owner_bound=owner_bound,
            )
        elif kind == "session_query_menu":
            if content not in {"查询项目列表", "查询个人会话", "搜索会话"}:
                raise ManagementUserError("请点击卡片中的查询按钮，或重新发送“.查询会话”。")
            self._handle_top(message, contextual=True)
        elif kind == "project_list":
            self._handle_project_list(
                message,
                payload,
                content,
                owner_bound=owner_bound,
                context_created_at=context_created_at,
            )
        elif kind == "project_threads":
            self._handle_project_threads(
                message,
                context_id,
                payload,
                content,
                owner_bound=owner_bound,
                context_created_at=context_created_at,
            )
        elif kind == "personal_list":
            self._handle_personal_list(
                message,
                context_id,
                payload,
                content,
                owner_bound=owner_bound,
                context_created_at=context_created_at,
            )
        elif kind == "monitor_list":
            self._handle_monitor_list(message, payload, content)
        elif kind in {"thread_overview", "thread_reply_form"}:
            self._continue_thread(
                message, context_id, payload, owner_bound=owner_bound
            )
        elif kind == "new_project_form":
            self._create_project(message, context_id)
        elif kind == "new_project_thread_form":
            self._create_project_thread(message, payload, context_id)
        elif kind == "new_personal_thread_form":
            self._create_personal_thread(message, context_id)
        elif kind == "monitor_add_form":
            self._apply_monitor_form(message, "add", context_id)
        elif kind == "monitor_remove_form":
            self._apply_monitor_form(message, "remove", context_id)
        elif kind == "session_search_form":
            self._apply_search_form(message)
        elif kind == "session_search_results":
            self._handle_search_results(message, payload, content)
        elif kind == "session_search_expand":
            self._handle_search_expand(message, payload, content)
        else:
            raise ManagementUserError("这条消息不支持继续操作，请重新发送入口命令。")

    def _handle_remote_control_menu(
        self,
        message: ChannelReply,
        context_id: str,
        payload: Mapping[str, Any],
        content: str,
        *,
        owner_bound: bool = False,
        context_created_at: int | None = None,
    ) -> None:
        binding_command = _CURRENT_THREAD_INPUT_ALIASES.get(content)
        if binding_command is not None:
            self._handle_current_thread_command(message, binding_command)
            return
        if content not in {"选择项目会话", "选择个人会话"}:
            raise ManagementUserError(
                "请点击卡片选择项目或个人会话，或回复“选择项目会话”/“选择个人会话”。"
            )
        selection_mode = _text(payload.get("selection_mode")) or "remote_control"
        if selection_mode not in {"remote_control", "binding_switch"}:
            raise ManagementUserError("这张目标选择卡片已失效，请重新发送“.切换会话”。")
        pending_command = _text(payload.get("pending_command"))
        if pending_command:
            self._require_fresh_selection_context(
                owner_bound=owner_bound,
                context_created_at=int(context_created_at or 0),
            )
            # A selector carrying a pending command remains one-shot: two
            # independent child contexts could otherwise execute one write
            # twice.  Plain navigation selectors are deliberately reusable.
            if not self.store.claim_management_target_selection(context_id):
                raise ManagementUserError(
                    "这张目标选择卡片已经处理过，本次没有重复执行；请重新发送命令。"
                )
        session = self._open_desktop(("list_projects",))
        try:
            catalog, registry = self._catalog(session.listing)
            if content == "选择项目会话":
                projects_payload = session.tools.list_projects(session.source_thread_id)
                projects = self._project_snapshot(
                    projects_payload, registry.projects, catalog
                )
                self._send_project_page(
                    projects,
                    1,
                    message.message_id,
                    selection_mode=selection_mode,
                    pending_command=pending_command,
                    selection_started_at=int(
                        payload.get("selection_started_at")
                        or context_created_at
                        or 0
                    ),
                    selection_guard_context_id=context_id,
                )
            else:
                personal = [
                    item for item in catalog if not _text(item.get("projectId"))
                ]
                self._send_personal_page(
                    personal,
                    1,
                    message.message_id,
                    selection_mode=selection_mode,
                    pending_command=_text(payload.get("pending_command")),
                    selection_started_at=int(
                        payload.get("selection_started_at")
                        or context_created_at
                        or 0
                    ),
                    selection_guard_context_id=context_id,
                )
        finally:
            session.tools.close()

    def _handle_project_list(
        self,
        message: ChannelReply,
        payload: Mapping[str, Any],
        content: str,
        *,
        owner_bound: bool = False,
        context_created_at: int | None = None,
    ) -> None:
        projects = payload.get("projects")
        if not isinstance(projects, list):
            raise ManagementUserError("项目快照已损坏，请重新查询。")
        page_match = _PAGE.fullmatch(content)
        if page_match:
            self._send_project_page(
                projects,
                int(page_match.group(1)),
                message.message_id,
                selection_mode=_text(payload.get("selection_mode")),
                pending_command=_text(payload.get("pending_command")),
                selection_started_at=int(
                    payload.get("selection_started_at") or context_created_at or 0
                ),
                selection_guard_context_id=_text(
                    payload.get("selection_guard_context_id")
                ),
            )
            return
        if content == "新建项目":
            self._new_project_form(message.message_id)
            return
        if content == "新建项目会话":
            self._new_project_thread_form(
                None,
                message.message_id,
                projects=projects,
            )
            return
        match = _EXPAND_PROJECT.fullmatch(content)
        if not match:
            raise ManagementUserError(
                "请回复“展开A01”“第2页”“新建项目”或“新建项目会话”。"
            )
        project = next((item for item in projects if isinstance(item, dict) and item.get("label") == match.group(1)), None)
        if project is None:
            raise ManagementUserError("项目标号不在这份历史快照中。")
        selection_mode = _text(payload.get("selection_mode"))
        if selection_mode in {"remote_control", "binding_switch"}:
            self._require_fresh_selection_context(
                owner_bound=owner_bound,
                context_created_at=int(
                    payload.get("selection_started_at") or context_created_at or 0
                ),
            )
        if selection_mode.startswith("new_task_"):
            environment_mode = selection_mode.removeprefix("new_task_")
            if environment_mode not in {"choose", "local", "worktree"}:
                raise ManagementUserError("项目创建方式已损坏，请重新发送斜杠指令。")
            self._new_project_thread_form(
                project,
                message.message_id,
                environment_mode=environment_mode,
            )
            return
        session = self._open_desktop(())
        try:
            catalog, _registry = self._catalog(session.listing)
            threads = [item for item in catalog if item.get("projectId") == project.get("project_id")]
        finally:
            session.tools.close()
        self._send_project_threads(
            project,
            threads,
            1,
            message.message_id,
            selection_mode=_text(payload.get("selection_mode")),
            pending_command=_text(payload.get("pending_command")),
            selection_started_at=int(
                payload.get("selection_started_at") or context_created_at or 0
            ),
            selection_guard_context_id=_text(
                payload.get("selection_guard_context_id")
            ),
        )

    def _send_project_threads(
        self,
        project: Mapping[str, Any],
        threads: Sequence[Mapping[str, Any]],
        page: int,
        request_id: str,
        *,
        selection_mode: str = "",
        pending_command: str = "",
        selection_started_at: int | None = None,
        selection_guard_context_id: str = "",
    ) -> None:
        selection_origin = int(selection_started_at or time.time())
        snapshot = [dict(item) for item in threads]
        for index, item in enumerate(snapshot, start=1):
            item["label"] = _label("a", index)
        start, end, pages = _page_bounds(len(snapshot), page)
        lines = [
            "列表类型：Codex 项目会话",
            f"项目名称：{project.get('label')}｜"
            f"{_compact(_text(project.get('name')) or '未命名项目', LIST_PROJECT_MAX_CHARS)}",
            f"页码：第 {page}/{pages} 页",
            f"总数：{len(snapshot)} 个",
            "",
            "会话列表：",
        ]
        if not snapshot:
            lines.append("该项目还没有会话。")
        else:
            for item in snapshot[start:end]:
                title = _compact(_text(item.get("title")) or "未命名会话", LIST_TITLE_MAX_CHARS)
                lines.append(f"{item['label']}｜{title}")
        lines.extend((
            "",
            "操作说明：",
            "- 选择会话：回复“选定a01”",
            "- 永久监测：回复“添加监测a01”",
            "- 翻页：回复“第2页”",
            "- 也可回复“新建项目会话”",
        ))
        text = "\n".join(lines)
        payload = {
            "project": dict(project),
            "threads": snapshot,
            "page": page,
            "selection_mode": selection_mode,
            "pending_command": pending_command,
            "selection_started_at": selection_origin,
            "selection_guard_context_id": _text(selection_guard_context_id),
        }
        self._respond_card(
            build_thread_list_card(
                snapshot[start:end],
                page=page,
                pages=pages,
                total=len(snapshot),
                project_name=_compact(
                    _text(project.get("name")) or "未命名项目",
                    LIST_PROJECT_MAX_CHARS,
                ),
            ),
            "project_threads",
            payload,
            f"management-project-threads:{request_id}:{page}",
            fallback_text=text,
        )

    def _handle_project_threads(
        self,
        message: ChannelReply,
        context_id: str,
        payload: Mapping[str, Any],
        content: str,
        *,
        owner_bound: bool = False,
        context_created_at: int | None = None,
    ) -> None:
        project = payload.get("project")
        threads = payload.get("threads")
        if not isinstance(project, dict) or not isinstance(threads, list):
            raise ManagementUserError("项目会话快照已损坏，请重新查询。")
        page_match = _PAGE.fullmatch(content)
        if page_match:
            self._send_project_threads(
                project,
                threads,
                int(page_match.group(1)),
                message.message_id,
                selection_mode=_text(payload.get("selection_mode")),
                pending_command=_text(payload.get("pending_command")),
                selection_started_at=int(
                    payload.get("selection_started_at") or context_created_at or 0
                ),
                selection_guard_context_id=_text(
                    payload.get("selection_guard_context_id")
                ),
            )
            return
        if content == "新建项目会话":
            self._new_project_thread_form(project, message.message_id)
            return
        add_match = _ADD_PROJECT_THREAD_MONITOR.fullmatch(content)
        if add_match:
            thread = next((item for item in threads if isinstance(item, dict) and item.get("label") == add_match.group(1)), None)
            if thread is None:
                raise ManagementUserError("会话标号不在这份历史快照中。")
            self._add_manual_monitor(thread, message, "project_threads", payload)
            return
        match = _SELECT_PROJECT_THREAD.fullmatch(content)
        if not match:
            raise ManagementUserError(
                "请回复“选定a01”“添加监测a01”“第2页”或“新建项目会话”。"
            )
        thread = next((item for item in threads if isinstance(item, dict) and item.get("label") == match.group(1)), None)
        if thread is None:
            raise ManagementUserError("会话标号不在这份历史快照中。")
        selection_mode = _text(payload.get("selection_mode"))
        pending_command = _text(payload.get("pending_command"))
        if pending_command:
            self._require_fresh_selection_context(
                owner_bound=owner_bound,
                context_created_at=int(
                    payload.get("selection_started_at") or context_created_at or 0
                ),
            )
            guard_context_id = (
                _text(payload.get("selection_guard_context_id")) or context_id
            )
            if not self.store.claim_management_target_selection(
                guard_context_id,
                marker=("pending" if guard_context_id != context_id else "target"),
            ):
                raise ManagementUserError(
                    "这次会话选择已经处理过，本次没有重复执行；请重新发送命令。"
                )
        if selection_mode in {
            "remote_control",
            "binding_switch",
        }:
            self._activate_remote_target(
                thread,
                _text(project.get("name")),
                message,
                pending_command,
                selection_mode=selection_mode,
            )
        else:
            self._set_current_thread(message, thread)
            self._send_overview(
                thread,
                _text(project.get("name")),
                message.message_id,
                selected_current=True,
            )

    def _handle_personal_list(
        self,
        message: ChannelReply,
        context_id: str,
        payload: Mapping[str, Any],
        content: str,
        *,
        owner_bound: bool = False,
        context_created_at: int | None = None,
    ) -> None:
        threads = payload.get("threads")
        if not isinstance(threads, list):
            raise ManagementUserError("个人会话快照已损坏，请重新查询。")
        page_match = _PAGE.fullmatch(content)
        if page_match:
            self._send_personal_page(
                threads,
                int(page_match.group(1)),
                message.message_id,
                selection_mode=_text(payload.get("selection_mode")),
                pending_command=_text(payload.get("pending_command")),
                selection_started_at=int(
                    payload.get("selection_started_at") or context_created_at or 0
                ),
                selection_guard_context_id=_text(
                    payload.get("selection_guard_context_id")
                ),
            )
            return
        if content == "新建个人会话":
            self._new_personal_form(message.message_id)
            return
        add_match = _ADD_PERSONAL_THREAD_MONITOR.fullmatch(content)
        if add_match:
            thread = next((item for item in threads if isinstance(item, dict) and item.get("label") == add_match.group(1)), None)
            if thread is None:
                raise ManagementUserError("会话标号不在这份历史快照中。")
            self._add_manual_monitor(thread, message, "personal_list", payload)
            return
        match = _SELECT_PERSONAL_THREAD.fullmatch(content)
        if not match:
            raise ManagementUserError(
                "请回复“选定p01”“添加监测p01”“第2页”或“新建个人会话”。"
            )
        thread = next((item for item in threads if isinstance(item, dict) and item.get("label") == match.group(1)), None)
        if thread is None:
            raise ManagementUserError("会话标号不在这份历史快照中。")
        selection_mode = _text(payload.get("selection_mode"))
        pending_command = _text(payload.get("pending_command"))
        if pending_command:
            self._require_fresh_selection_context(
                owner_bound=owner_bound,
                context_created_at=int(
                    payload.get("selection_started_at") or context_created_at or 0
                ),
            )
            guard_context_id = (
                _text(payload.get("selection_guard_context_id")) or context_id
            )
            if not self.store.claim_management_target_selection(
                guard_context_id,
                marker=("pending" if guard_context_id != context_id else "target"),
            ):
                raise ManagementUserError(
                    "这次会话选择已经处理过，本次没有重复执行；请重新发送命令。"
                )
        if selection_mode in {
            "remote_control",
            "binding_switch",
        }:
            self._activate_remote_target(
                thread,
                "个人会话",
                message,
                pending_command,
                selection_mode=selection_mode,
            )
        else:
            self._set_current_thread(message, thread)
            self._send_overview(
                thread,
                "个人会话",
                message.message_id,
                selected_current=True,
            )

    def _search_form(self, request_id: str) -> None:
        fallback = (
            "搜索 Codex 会话\n\n"
            "请回复本消息并保留字段名：\n"
            "会话名称：\n"
            "会话描述：\n"
            "会话最后活动时间："
        )
        self._respond_card(
            build_session_search_form_card(),
            "session_search_form",
            {},
            f"management-session-search-form:{request_id}",
            fallback_text=fallback,
        )

    def _search_display_titles(
        self,
        items: Sequence[Mapping[str, Any]],
        *,
        trust_result_titles: bool,
    ) -> tuple[dict[str, str], set[str]]:
        """按当前 Desktop/会话目录元数据刷新搜索标题，不改搜索结果契约。"""

        thread_ids = tuple(
            dict.fromkeys(
                _text(item.get("thread_id"))
                for item in items
                if _text(item.get("thread_id"))
            )
        )
        if not thread_ids:
            return {}, set()

        records: dict[str, ThreadRecord] = {}
        try:
            records = {
                record.thread_id: record
                for record in self.codex_store.select_threads(include_archived=True)
                if record.thread_id in thread_ids
            }
            self.codex_store.require_readable("刷新会话搜索标题")
        except CodexStoreReadError as exc:
            LOGGER.warning("会话搜索标题目录暂不可读 error=%s", type(exc).__name__)

        desktop_titles: dict[str, str] = {}
        session: _DesktopSession | None = None
        try:
            session = self._open_desktop(())
            for entry in self._desktop_items(session.listing):
                thread_id = _text(entry.get("id"))
                if thread_id in thread_ids:
                    desktop_titles[thread_id] = (
                        _text(entry.get("title")) or _text(entry.get("name"))
                    )
        except DesktopAppToolsError as exc:
            # 搜索本身不依赖 Desktop 管道；标题刷新失败时仍可使用本地权威索引。
            LOGGER.debug("Desktop 会话标题暂不可刷新 error=%s", type(exc).__name__)
        finally:
            if session is not None:
                session.tools.close()

        resolved: dict[str, str] = {}
        recovered: set[str] = set()
        for item in items:
            thread_id = _text(item.get("thread_id"))
            if not thread_id:
                continue
            record = records.get(thread_id)
            desktop_title = desktop_titles.get(thread_id, "")
            if record is not None:
                title, title_origin = self._thread_display_title(
                    record, desktop_title
                )
                if title_origin == "unavailable":
                    # 新版 SearchMatch.title 是同一轮 Luna 生成的 display_title；
                    # 旧上下文若仍保存提示词式 title，会在这里继续被拒绝。
                    result_title = (
                        _text(item.get("title"))
                        if trust_result_titles
                        and _text(item.get("title_origin")) == "recovered_summary"
                        else ""
                    )
                    if result_title:
                        title = result_title
                        recovered.add(thread_id)
                elif title_origin == "recovered_summary":
                    recovered.add(thread_id)
            else:
                # 当前引擎的 schema v1 title 已是展示名，可以在同一版上下文中
                # 安全复用。升级前的持久上下文没有版本标记；本地目录又不可读
                # 时不能判断它是否为首轮提示词，因此拒绝直接展示。
                title = _text(item.get("title")) if trust_result_titles else ""
                if title and _text(item.get("title_origin")) == "recovered_summary":
                    recovered.add(thread_id)
            if not title:
                if not trust_result_titles:
                    raise ManagementUserError(
                        "这份历史搜索结果来自旧版，无法安全确认会话名称；"
                        "请重新发送“.搜索会话”。"
                    )
                raise ManagementUserError(
                    "会话搜索结果缺少可展示名称，请重新发送“.搜索会话”。"
                )
            resolved[thread_id] = title
        return resolved, recovered

    @staticmethod
    def _search_request_from_form(content: str) -> SearchRequest:
        values = _parse_form(
            content,
            ("会话名称", "会话描述"),
            "会话最后活动时间",
        )
        return SearchRequest(
            name=values["会话名称"],
            description=values["会话描述"],
            last_activity=values["会话最后活动时间"],
            scope="auto",
        )

    def _apply_search_form(self, message: ChannelReply) -> None:
        request = self._search_request_from_form(message.content)
        self._run_search(request, message.message_id)

    def _run_search(self, request: SearchRequest, request_id: str) -> None:
        if self.session_search is None:
            raise ManagementUserError("当前版本尚未启用会话搜索。")
        try:
            result = self.session_search.search(request)
        except SessionSearchError as exc:
            LOGGER.warning("会话搜索未产生可信结果 error=%s", type(exc).__name__)
            self._respond(
                "会话搜索暂时没有完成，系统没有拿随机会话冒充结果。\n"
                "请稍后重新发送“.搜索会话”再试。",
                "session_search_error",
                {},
                f"management-session-search-error:{request_id}",
            )
            return
        management_result = result.to_management_dict()
        raw_matches = management_result.get("matches")
        if isinstance(raw_matches, list):
            for item in raw_matches:
                if not isinstance(item, dict):
                    continue
                snapshot = item.get("_query_snapshot")
                if not isinstance(snapshot, dict):
                    continue
                snapshot["snapshot_key"] = hashlib.sha256(
                    (
                        f"management-search-snapshot-v1\0{request_id}\0"
                        f"{_text(item.get('thread_id'))}\0"
                        f"{_text(snapshot.get('turn_id'))}\0"
                        f"{_text(snapshot.get('content_hash'))}"
                    ).encode("utf-8")
                ).hexdigest()
        if result.status == "found" and len(result.matches) == 1:
            self._send_search_match(management_result["matches"][0], request_id)
            return
        if result.status == "ambiguous" and result.matches:
            self._send_search_page(management_result, 1, request_id)
            return
        self._send_search_not_found(request, result, request_id)

    def _send_search_page(
        self,
        result: Mapping[str, Any],
        page: int,
        request_id: str,
        *,
        trust_result_titles: bool = True,
    ) -> None:
        raw_matches = result.get("matches")
        if not isinstance(raw_matches, list):
            raise ManagementUserError("会话搜索结果快照已损坏，请重新搜索。")
        total = len(raw_matches)
        pages = max(1, math.ceil(total / SEARCH_PAGE_SIZE))
        if page > pages:
            self._respond(
                "已经到最后一页，没有更多候选会话。\n"
                "请回复上一条候选列表中的编号进行选择。",
                "session_search_results",
                {
                    "result": dict(result),
                    "page": pages,
                    "title_format_version": (
                        SEARCH_TITLE_FORMAT_VERSION if trust_result_titles else 0
                    ),
                },
                f"management-session-search-end:{request_id}:{pages}",
                ttl_days=30,
            )
            return
        start = (page - 1) * SEARCH_PAGE_SIZE
        end = min(total, start + SEARCH_PAGE_SIZE)
        header = [
            "列表类型：会话搜索候选",
            _message_field("搜索范围", _text(result.get("scope_label")) or "未说明"),
            _message_field("页码", f"第 {page}/{pages} 页"),
            _message_field("总数", f"{total} 个"),
        ]
        display_titles, recovered_title_ids = self._search_display_titles(
            [item for item in raw_matches if isinstance(item, Mapping)],
            trust_result_titles=trust_result_titles,
        )
        candidate_blocks: list[list[str]] = []
        for index in range(start, end):
            item = raw_matches[index]
            if not isinstance(item, Mapping):
                raise ManagementUserError("会话搜索候选已损坏，请重新搜索。")
            percent = round(float(item.get("score") or 0) * 100)
            thread_id = _text(item.get("thread_id"))
            block = [
                f"{index + 1}｜{_book_title(display_titles.get(thread_id), 48)}",
            ]
            if thread_id in recovered_title_ids:
                block.append(
                    "名称说明：Codex 当前没有可用的独立标题，当前名称为内容概括；"
                    "在 Codex 中重命名后会自动更新。"
                )
            block.extend([
                _message_field("匹配度", f"{percent}%"),
                _message_field(
                    "匹配说明",
                    _compact(_text(item.get("reason")), 90) or "暂无补充说明。",
                ),
            ])
            candidate_blocks.append(block)
        instructions = ["操作说明：", "- 选择会话：回复“选择1”"]
        if end < total:
            instructions.append("- 下一页：回复“翻页”")
        else:
            instructions.append("- 已到最后一页")
        payload = _message_blocks(
            header,
            ["会话列表："],
            *candidate_blocks,
            instructions,
        )
        self._respond(
            payload,
            "session_search_results",
            {
                "result": dict(result),
                "page": page,
                "title_format_version": (
                    SEARCH_TITLE_FORMAT_VERSION if trust_result_titles else 0
                ),
            },
            f"management-session-search-results:{request_id}:{page}",
            ttl_days=30,
        )

    def _handle_search_results(
        self, message: ChannelReply, payload: Mapping[str, Any], content: str
    ) -> None:
        result = payload.get("result")
        page = payload.get("page")
        if not isinstance(result, dict) or not isinstance(page, int):
            raise ManagementUserError("会话搜索结果上下文已损坏，请重新搜索。")
        raw_matches = result.get("matches")
        if not isinstance(raw_matches, list):
            raise ManagementUserError("会话搜索候选已损坏，请重新搜索。")
        if content == "翻页":
            self._send_search_page(
                result,
                page + 1,
                message.message_id,
                trust_result_titles=(
                    payload.get("title_format_version") == SEARCH_TITLE_FORMAT_VERSION
                ),
            )
            return
        if content == "取消搜索":
            self._respond(
                "已取消本次会话搜索；没有打开或修改任何会话。",
                "session_search_cancelled",
                {},
                f"management-session-search-cancelled:{message.message_id}",
            )
            return
        match = _SELECT_SEARCH_RESULT.fullmatch(content)
        if match is None:
            raise ManagementUserError("请回复“选择1”“翻页”或“取消搜索”。")
        selected = int(match.group(1))
        if not 1 <= selected <= len(raw_matches):
            raise ManagementUserError("选择编号不在这份候选列表中。")
        item = raw_matches[selected - 1]
        if not isinstance(item, Mapping):
            raise ManagementUserError("会话搜索候选已损坏，请重新搜索。")
        self._send_search_match(
            item,
            message.message_id,
            trust_result_title=(
                payload.get("title_format_version") == SEARCH_TITLE_FORMAT_VERSION
            ),
        )

    def _send_search_not_found(
        self, request: SearchRequest, result: SearchResult, request_id: str
    ) -> None:
        lines = [
            "没有找到。",
            f"当前检索范围：{result.scope_label}",
        ]
        if result.warnings:
            lines.append(f"读取提醒：有 {len(result.warnings)} 个会话记录不完整，系统没有静默当作正常结果。")
        if result.can_expand and result.next_scope:
            lines.extend((
                "",
                "是否扩大搜索范围？",
                result.cost_warning,
                "如需继续，请引用本消息回复“确认增加搜索范围”。",
            ))
        else:
            lines.extend(("", "已经检查全部用户会话（包括归档），不能再扩大范围。"))
        self._respond(
            "\n".join(lines),
            "session_search_expand",
            {"request": request.to_dict(), "result": result.to_dict()},
            f"management-session-search-not-found:{request_id}:{result.scope}",
        )

    def _handle_search_expand(
        self, message: ChannelReply, payload: Mapping[str, Any], content: str
    ) -> None:
        request_raw = payload.get("request")
        result = payload.get("result")
        if not isinstance(request_raw, Mapping) or not isinstance(result, Mapping):
            raise ManagementUserError("会话搜索范围上下文已损坏，请重新搜索。")
        if content == "取消搜索":
            self._respond(
                "已取消扩大范围；没有打开或修改任何会话。",
                "session_search_cancelled",
                {},
                f"management-session-search-expand-cancelled:{message.message_id}",
            )
            return
        if "会话名称：" in message.content and "会话最后活动时间：" in message.content:
            self._run_search(self._search_request_from_form(message.content), message.message_id)
            return
        if content != "确认增加搜索范围":
            raise ManagementUserError(
                "请回复“确认增加搜索范围”“取消搜索”，或重新复制搜索表单填写新线索。"
            )
        if result.get("can_expand") is not True or not _text(result.get("next_scope")):
            raise ManagementUserError("已经搜索全部用户会话，不能继续扩大范围。")
        request = SearchRequest.from_mapping(request_raw).with_scope(_text(result.get("next_scope")))
        self._run_search(request, message.message_id)

    def _send_search_match(
        self,
        item: Mapping[str, Any],
        request_id: str,
        *,
        trust_result_title: bool = True,
    ) -> None:
        thread_id = _text(item.get("thread_id"))
        if not thread_id:
            raise ManagementUserError("会话搜索结果缺少任务 ID，请重新搜索。")
        monitor = item.get("monitor")
        origin = _text(monitor.get("origin")) if isinstance(monitor, Mapping) else ""
        monitor_status = (
            "手动永久" if origin == "manual" else
            "自动（按原到期规则）" if origin == "auto" else
            "未监测"
        )
        display_titles, recovered_title_ids = self._search_display_titles(
            [item], trust_result_titles=trust_result_title
        )
        display_title = display_titles.get(thread_id, SEARCH_EMPTY_TITLE)
        identity = [
            _message_field(
                "会话名称",
                _book_title(display_title, OVERVIEW_TITLE_MAX_CHARS),
            ),
            _message_field("归属", _text(item.get("project_name")) or "个人会话"),
            _message_field("监测状态", monitor_status),
        ]
        if thread_id in recovered_title_ids:
            identity.append(
                "名称说明：Codex 当前没有可用的独立标题，当前名称为内容概括；"
                "在 Codex 中重命名后会自动更新。"
            )
        payload = _message_blocks(
            identity,
            [
                _message_field(
                    "会话描述",
                    _compact(
                        _text(item.get("description")) or "该会话暂无描述。",
                        220,
                    ),
                )
            ],
            [
                _message_field(
                    "会话最后一轮结果",
                    _compact(
                        _text(item.get("last_result"))
                        or "最近轮次暂无可展示的最终答复。",
                        500,
                    ),
                ),
                _message_field(
                    "会话最后活动时间",
                    _text(item.get("last_activity_at_beijing")) or "未知",
                ),
            ],
            [
                _message_field(
                    "匹配说明",
                    _compact(_text(item.get("reason")), 180) or "暂无补充说明。",
                )
            ],
            [
                "操作说明：",
                "- 继续会话：直接回复本消息并发送文字",
                "- 管理监测：回复“添加监测”或“移除监测”",
                "- 查看本次原文：回复“.原文”（本次查询限一次）",
                "- 归档该会话：回复“.归档”（本次查询限一次）",
            ],
        )
        thread = {
            "id": thread_id,
            "title": display_title,
            "hostId": _text(item.get("host_id")) or "local",
            "projectId": _text(item.get("project_id")) or None,
            "archived": bool(item.get("archived")),
        }
        overview_facts = [
            ("归属", _text(item.get("project_name")) or "个人会话"),
            ("监测状态", monitor_status),
            (
                "最后活动时间",
                _text(item.get("last_activity_at_beijing")) or "未知",
            ),
        ]
        overview_sections: list[tuple[str, str]] = [
            (
                "会话描述",
                _compact(
                    _text(item.get("description")) or "该会话暂无描述。",
                    220,
                ),
            ),
            (
                "最后一轮结果",
                _compact(
                    _text(item.get("last_result"))
                    or "最近轮次暂无可展示的最终答复。",
                    500,
                ),
            ),
            (
                "匹配说明",
                _compact(_text(item.get("reason")), 180) or "暂无补充说明。",
            ),
        ]
        if thread_id in recovered_title_ids:
            overview_sections.insert(
                0,
                (
                    "名称说明",
                    "Codex 当前没有可用的独立标题，当前名称为内容概括；"
                    "在 Codex 中重命名后会自动更新。",
                ),
            )
        self._respond_card(
            build_thread_overview_card(
                title=display_title,
                facts=overview_facts,
                sections=overview_sections,
                monitor_status=monitor_status,
            ),
            "thread_overview",
            {
                "thread": thread,
                "group": _text(item.get("project_name")) or "个人会话",
                "query_snapshot": dict(item.get("_query_snapshot") or {}),
            },
            f"management-session-search-match:{request_id}:{thread_id}",
            fallback_text=payload,
            ttl_days=30,
        )

    @staticmethod
    def _remote_payload(
        thread: Mapping[str, Any], group: str
    ) -> dict[str, Any]:
        return {
            "thread": {
                "id": _text(thread.get("id")),
                "title": _text(thread.get("title")) or "新建会话",
                "hostId": _text(thread.get("hostId")) or "local",
                "projectId": _text(thread.get("projectId")) or None,
            },
            "group": _text(group) or "个人会话",
        }

    @staticmethod
    def _slash_catalog_text(page: int) -> str:
        from .slash_commands import OFFICIAL_SLASH_CAPABILITIES, SLASH_PAGE_SIZE

        pages = math.ceil(len(OFFICIAL_SLASH_CAPABILITIES) / SLASH_PAGE_SIZE)
        start = (page - 1) * SLASH_PAGE_SIZE
        labels = {
            "remote": "可远程执行",
            "existing": "复用现有流程",
            "context": "仅对应消息可用",
            "disabled": "当前不可远程执行",
        }
        lines = [f"Codex 斜杠指令｜第 {page}/{pages} 页"]
        for item in OFFICIAL_SLASH_CAPABILITIES[start : start + SLASH_PAGE_SIZE]:
            lines.extend(("", f"{item.command}｜{item.label}", item.description, f"状态：{labels[item.mode]}"))
            if item.disabled_reason:
                lines.append(f"原因：{item.disabled_reason}")
        lines.extend(("", "翻页：回复“/commands page 2”等精确页码。"))
        return "\n".join(lines)

    def _send_remote_target_selector(
        self,
        request_id: str,
        pending_command: str = "",
        *,
        selection_mode: str = "remote_control",
    ) -> None:
        if selection_mode not in {"remote_control", "binding_switch"}:
            raise ValueError("未知的远程目标选择模式")
        self._respond_card(
            build_remote_control_entry_card(),
            "remote_control_menu",
            {
                "version": 1,
                "pending_command": pending_command,
                "selection_mode": selection_mode,
                "selection_started_at": int(time.time()),
            },
            f"management-remote-target:{request_id}",
            fallback_text=(
                "请选择这次要操作的 Codex 会话：\n"
                "- 回复“选择项目会话”\n"
                "- 回复“选择个人会话”"
            ),
        )

    def _activate_remote_target(
        self,
        thread: Mapping[str, Any],
        group: str,
        message: ChannelReply,
        pending_command: str,
        *,
        selection_mode: str = "remote_control",
    ) -> None:
        if selection_mode not in {"remote_control", "binding_switch"}:
            raise ManagementUserError("这张目标选择卡片已失效，请重新选择会话。")
        title = self._set_current_thread(message, thread)
        self._respond(
            f"已设为当前会话：{_book_title(title, OVERVIEW_TITLE_MAX_CHARS)}",
            "current_binding",
            {},
            f"management-current-thread-selected:{message.message_id}",
        )
        if pending_command:
            context_id = self._create_remote_context(message, thread, group)
            self._handle_remote_control(
                message,
                context_id,
                self._remote_payload(thread, group),
                pending_command,
                owner_bound=True,
            )
        elif selection_mode == "remote_control":
            self._send_remote_control_card(thread, group, message.message_id)

    def _open_new_project_task_selector(
        self, request_id: str, *, environment_mode: str
    ) -> None:
        if environment_mode not in {"choose", "local", "worktree"}:
            raise ValueError("未知项目运行方式")
        session = self._open_desktop(("list_projects",))
        try:
            catalog, registry = self._catalog(session.listing)
            projects_payload = session.tools.list_projects(session.source_thread_id)
            projects = self._project_snapshot(projects_payload, registry.projects, catalog)
        finally:
            session.tools.close()
        if environment_mode == "worktree":
            projects = [item for item in projects if item.get("is_git") is True]
        self._send_project_page(
            projects,
            1,
            request_id,
            selection_mode=f"new_task_{environment_mode}",
        )

    def _show_monitor_settings(self, request_id: str) -> None:
        settings = self.store.auto_monitoring_settings()
        enabled = settings.get("auto_monitoring_enabled") is True
        effective_at = settings.get("effective_at")
        self._respond_card(
            build_monitor_settings_card(
                enabled,
                effective_at if type(effective_at) is int else None,
            ),
            "monitor_settings",
            {"view": "current"},
            f"management-monitor-settings:{request_id}",
            fallback_text=(
                f"自动监测：{'已开启' if enabled else '已关闭'}\n"
                "修改：回复本消息发送“开启自动监测”或“关闭自动监测”。\n"
                "任何修改都会再要求一次确认。"
            ),
            ttl_days=30,
        )

    def _show_monitor_settings_confirmation(
        self, request_id: str, *, desired: bool
    ) -> None:
        settings = self.store.auto_monitoring_settings()
        current = settings.get("auto_monitoring_enabled") is True
        if current == desired:
            self._respond(
                f"自动监测已经{'开启' if desired else '关闭'}，本次没有重复写入。",
                "monitor_settings",
                {"view": "unchanged"},
                f"management-monitor-settings-unchanged:{request_id}:{int(desired)}",
                ttl_days=30,
            )
            return
        effective_at = settings.get("effective_at")
        self._respond_card(
            build_monitor_settings_card(
                current,
                effective_at if type(effective_at) is int else None,
                confirm=desired,
            ),
            "monitor_settings",
            {"view": "confirm", "desired": desired},
            f"management-monitor-settings-confirm:{request_id}:{int(desired)}",
            fallback_text=(
                f"确认把自动监测设置为{'开启' if desired else '关闭'}。\n"
                f"请回复本消息发送“确认{'开启' if desired else '关闭'}自动监测”；"
                "没有确认就不会修改。"
            ),
            ttl_days=30,
        )

    def _handle_monitor_settings(
        self,
        message: ChannelReply,
        context_id: str,
        payload: Mapping[str, Any],
        content: str,
        *,
        owner_bound: bool,
    ) -> None:
        if message.attachments:
            raise ManagementUserError("监测设置不能同时附带图片。")
        if content == "监测设置":
            self._show_monitor_settings(message.message_id)
            return
        if content in {"开启自动监测", "关闭自动监测"}:
            self._show_monitor_settings_confirmation(
                message.message_id,
                desired=(content == "开启自动监测"),
            )
            return
        if content not in {"确认开启自动监测", "确认关闭自动监测"}:
            raise ManagementUserError(
                "请使用监测设置卡片，或回复“开启自动监测”/“关闭自动监测”。"
            )
        if not owner_bound:
            raise ManagementUserError("这条确认卡没有安全绑定，请重新发送“.监测设置”。")
        desired = content == "确认开启自动监测"
        if payload.get("view") != "confirm" or payload.get("desired") is not desired:
            raise ManagementUserError("确认卡状态与操作不一致，请重新发送“.监测设置”。")
        self._apply_monitor_setting(context_id, message, desired)

    def _apply_monitor_setting(
        self, context_id: str, message: ChannelReply, desired: bool
    ) -> None:
        command = RemoteCommand(
            "monitor_auto_set", "true" if desired else "false"
        )
        request_hash = command.request_hash()
        reservation = self.store.begin_remote_control_action(
            context_id,
            command.kind,
            request_hash,
            message.message_id,
        )
        if self._remote_action_state_reply(
            context_id, command, reservation.status, message.message_id
        ):
            return
        submitted = False
        try:
            # 这是本地持久开关的精确提交边界；边界之后任何异常都必须冻结，
            # 不能猜测写入是否已经提交并自动重放。
            if not self.store.mark_remote_control_action_submitted(
                context_id, command.kind, request_hash
            ):
                raise StateError("自动监测提交边界写入失败")
            submitted = True
            changed_result = self.store.set_auto_monitoring_enabled(desired)
            readback = self.store.auto_monitoring_settings()
            if readback.get("auto_monitoring_enabled") is not desired:
                raise StateError("自动监测写后读回与目标值不一致")
            evidence = {
                "verified": True,
                "kind": command.kind,
                "enabled": desired,
                "changed": changed_result.get("changed") is True,
                "effective_at": readback.get("effective_at"),
            }
        except Exception as exc:
            if submitted:
                self.store.mark_remote_control_action_uncertain(
                    context_id, command.kind, request_hash, type(exc).__name__
                )
                self._respond_in_context(
                    context_id,
                    "自动监测设置已经跨过提交边界，但最终状态无法确认。"
                    "系统已冻结自动重试，请重新打开“监测设置”核对。",
                    f"management-monitor-settings-uncertain:{context_id}:{message.message_id}",
                )
                return
            self.store.release_remote_control_action(
                context_id, command.kind, request_hash, "preflight_failed"
            )
            raise ManagementUserError(
                "自动监测设置在写入前失败，没有改变状态，可以稍后重试。"
            ) from exc
        if not self.store.complete_remote_control_action(
            context_id, command.kind, request_hash, result=evidence
        ):
            self.store.mark_remote_control_action_uncertain(
                context_id, command.kind, request_hash, "completion_state_failed"
            )
            self._respond_in_context(
                context_id,
                "自动监测已经读回目标状态，但本地成功证据写入失败。"
                "系统已冻结重复提交，请重新打开设置核对。",
                f"management-monitor-settings-completion-uncertain:{context_id}",
            )
            return
        self._respond_in_context(
            context_id,
            f"自动监测已{'开启' if desired else '关闭'}。\n"
            f"是否发生变化：{'是' if evidence['changed'] else '否'}\n"
            "关闭时只停止自动发现与自动加入；已有自动项按原到期规则退出，"
            "手动长期监测及通知不受影响。",
            f"management-monitor-settings-success:{context_id}:{message.message_id}",
        )

    def _send_reset_alert_status(self, request_id: str) -> None:
        status = self.store.reset_alert_status()
        if status.get("available") is not True:
            text = "重置预警状态：当前状态库版本不支持只读查看，需要受控升级后再试。"
        else:
            text = "\n".join(
                (
                    "重置预警状态：",
                    f"功能开关：{'已开启' if status.get('enabled') is True else '已关闭'}",
                    f"后台检查：{'运行中' if status.get('worker_running') is True else '未运行'}",
                    f"最近状态：{_text(status.get('state')) or '暂无'}",
                    f"最近成功：{_format_management_time(status.get('last_success_at'))}",
                    f"下次检查：{_format_management_time(status.get('next_check_at'))}",
                    f"待发送预警：{int(status.get('pending') or 0)}",
                    f"结果未知：{int(status.get('uncertain') or 0)}",
                    "说明：本次仅读取现有状态，没有触发抓取或改变通知规则。",
                )
            )
        self._respond(
            text,
            "reset_alert_status",
            {},
            f"management-reset-alert-status:{request_id}",
        )

    def _send_recent_reset_alerts(self, request_id: str) -> None:
        alerts = [
            item
            for item in self.store.latest_reset_alerts(limit=10)
            if _text(item.get("level")) in {"A", "B"}
        ]
        lines = ["最近已经触发的重置预警："]
        if not alerts:
            lines.append("目前没有已触发的 A/B 预警。")
        else:
            for index, item in enumerate(alerts, start=1):
                lines.extend(
                    (
                        "",
                        f"{index}｜{_text(item.get('level'))} 级｜"
                        f"{_format_management_time(item.get('created_at'))}",
                        f"时间窗口：{_compact(_text(item.get('window')) or '未提供', 120)}",
                        f"依据：{_compact(_text(item.get('evidence')) or '未提供', 240)}",
                        f"建议：{_compact(_text(item.get('advice')) or '未提供', 180)}",
                    )
                )
        lines.extend(("", "说明：本次只读取已触发事件，没有主动抓取或写入状态。"))
        self._respond(
            "\n".join(lines),
            "reset_alert_recent",
            {},
            f"management-reset-alert-recent:{request_id}",
        )

    def _send_remote_control_form(
        self,
        thread: Mapping[str, Any],
        group: str,
        request_id: str,
        *,
        form_kind: str,
    ) -> str:
        """发送一个只含单表单的远程控制子卡。"""

        title = _compact(
            _text(thread.get("title")) or "新建会话", OVERVIEW_TITLE_MAX_CHARS
        )
        if form_kind == "goal_set":
            card = build_goal_set_form_card(title)
            context_kind = "remote_goal_set_form"
            key_suffix = "goal-set-form"
            fallback = (
                f"会话名称：{title}\n\n"
                "当前渠道无法显示“设置 Goal”卡片；本次没有执行。\n"
                "请直接回复“/goal 你的长期目标”提交。"
            )
        elif form_kind == "plan_start":
            card = build_plan_start_form_card(title)
            context_kind = "remote_plan_start_form"
            key_suffix = "plan-start-form"
            fallback = (
                f"会话名称：{title}\n\n"
                "当前渠道无法显示“启动 Plan”卡片；本次没有执行。\n"
                "请直接回复“/plan 需要规划的任务”提交。"
            )
        else:
            raise ValueError("未知的远程控制表单类型")
        return self._respond_card(
            card,
            context_kind,
            self._remote_payload(thread, group),
            f"management-remote-{key_suffix}:{request_id}",
            fallback_text=fallback,
            ttl_days=30,
        )

    def _send_remote_control_card(
        self, thread: Mapping[str, Any], group: str, request_id: str
    ) -> str:
        thread_id = _text(thread.get("id"))
        if not thread_id:
            raise ManagementUserError("会话上下文缺少任务 ID，请重新选择。")
        title = _compact(
            _text(thread.get("title")) or "新建会话", OVERVIEW_TITLE_MAX_CHARS
        )
        return self._respond_card(
            build_remote_control_card(title),
            "remote_control",
            self._remote_payload(thread, group),
            f"management-remote-control:{request_id}:{thread_id}",
            fallback_text=(
                f"会话名称：{title}\n"
                f"归属：{_text(group) or '个人会话'}\n\n"
                "操作说明：\n"
                "- 查看 Skills：回复“/skills”\n"
                "- 查看全部斜杠指令：回复“/commands”\n"
                "- 具体指令可直接在聊天框输入"
            ),
            ttl_days=30,
        )

    def _remote_action_state_reply(
        self,
        context_id: str,
        command: RemoteCommand,
        status: str,
        message_id: str,
    ) -> bool:
        if status == "claimed":
            return False
        labels = {
            "goal_set": "设置 Goal",
            "goal_clear": "清除 Goal",
            "plan_start": "启动 Plan",
            "skill_start": "调用 Skill",
            "compact_start": "压缩上下文",
            "fork_start": "复制会话",
            "review_start": "启动代码审查",
            "model_set": "切换模型",
            "personality_set": "切换个性",
            "reasoning_set": "切换推理强度",
            "fast_toggle": "切换 Fast",
            "memories_set": "设置记忆模式",
            "monitor_auto_set": "设置自动监测",
        }
        label = labels.get(command.kind, "远程操作")
        if status == "succeeded":
            text = f"这次“{label}”已经成功执行，没有重复提交。"
        elif status == "uncertain":
            text = (
                f"这次“{label}”的结果无法确认。为避免重复改变 Codex 状态，"
                "系统已冻结自动重试；请在 Codex 桌面端核对后重新查询。"
            )
        else:
            text = f"这次“{label}”正在处理，请勿重复提交。"
        self._respond_in_context(
            context_id,
            text,
            f"management-remote-action-state:{context_id}:{command.kind}:{message_id}",
        )
        return True

    def _remote_prepare(
        self, thread_id: str
    ):
        if self.remote_control is None:
            raise ManagementUserError("当前本地候选尚未启用 Codex 指令使用连接。")
        try:
            return self.remote_control.prepare(thread_id)
        except CodexRPCError as exc:
            raise ManagementUserError(
                "当前无法安全连接 Codex App Server；没有提交任何远程操作。"
            ) from exc

    def _handle_remote_control(
        self,
        message: ChannelReply,
        context_id: str,
        payload: Mapping[str, Any],
        content: str,
        *,
        owner_bound: bool,
    ) -> None:
        if not owner_bound:
            raise ManagementUserError("这条指令使用卡片没有安全绑定，请重新选择会话。")
        thread = payload.get("thread")
        if not isinstance(thread, Mapping) or not _text(thread.get("id")):
            raise ManagementUserError("指令使用目标已损坏，请重新选择会话。")
        binding_command = _CURRENT_THREAD_INPUT_ALIASES.get(content)
        if binding_command is not None:
            self._handle_current_thread_command(
                message,
                binding_command,
                expected_thread_id=(
                    _text(thread.get("id"))
                    if binding_command == CURRENT_THREAD_CLEAR_COMMAND
                    else None
                ),
            )
            return
        command = parse_remote_command(content)
        if command is None:
            if content in {"设置 Goal", "启动 Plan", "/plan"}:
                if message.attachments:
                    raise ManagementUserError("指令使用表单入口不能附带图片。")
                self._send_remote_control_form(
                    thread,
                    _text(payload.get("group")) or "个人会话",
                    message.message_id,
                    form_kind=("goal_set" if content == "设置 Goal" else "plan_start"),
                )
                return
            if _DIRECT_FEATURE_ALIASES.get(content, content) == REMOTE_CONTROL_ENTRY_COMMAND:
                self._send_remote_control_card(
                    thread,
                    _text(payload.get("group")) or "个人会话",
                    message.message_id,
                )
                return
            if content == "斜杠控制":
                title = _compact(
                    _text(thread.get("title")) or "新建会话",
                    OVERVIEW_TITLE_MAX_CHARS,
                )
                self._respond_card(
                    build_slash_operations_card(title),
                    "remote_control",
                    self._remote_payload(
                        thread, _text(payload.get("group")) or "个人会话"
                    ),
                    f"management-slash-operations:{message.message_id}",
                    fallback_text=(
                        "可用操作：/mcp、/status、/model、/personality、/reasoning、"
                        "/memories、/compact、/fork、/fast、/review、/commands"
                    ),
                    ttl_days=30,
                )
                return
            slash = parse_slash_command(content)
            if slash is not None:
                self._handle_slash_control(
                    message, context_id, payload, thread, slash
                )
                return
            raise ManagementUserError(
                "请使用卡片中的操作，或发送 /goal、/plan、/skills、/mcp、"
                "/status、/model 等精确命令。"
            )
        if command.kind == "goal_clear_request":
            if message.attachments:
                raise ManagementUserError("清除 Goal 的确认请求不能附带图片。")
            title = _compact(
                _text(thread.get("title")) or "新建会话",
                OVERVIEW_TITLE_MAX_CHARS,
            )
            self._respond_card(
                build_goal_clear_confirmation_card(title),
                "remote_goal_clear_confirm",
                self._remote_payload(
                    thread, _text(payload.get("group")) or "个人会话"
                ),
                f"management-remote-goal-clear-confirm:{message.message_id}",
                fallback_text=(
                    "清除 Goal 需要二次确认。当前渠道无法显示确认卡片；"
                    "本次没有执行，请稍后重新发送“/goal clear”。"
                ),
                ttl_days=30,
            )
            return
        if message.attachments:
            raise ManagementUserError("指令不能同时附带图片。")
        if command.kind == "goal_get":
            self._remote_goal_get(context_id, payload, thread, message)
            return
        if command.kind == "skills_list":
            self._remote_skills_list(context_id, payload, thread, message, command)
            return
        self._remote_write(context_id, payload, thread, message, command)

    def _handle_slash_control(
        self,
        message: ChannelReply,
        context_id: str,
        payload: Mapping[str, Any],
        thread: Mapping[str, Any],
        command: SlashCommand,
    ) -> None:
        if message.attachments:
            raise ManagementUserError("Codex 控制指令不能同时附带图片。")
        title = _compact(
            _text(thread.get("title")) or "新建会话", OVERVIEW_TITLE_MAX_CHARS
        )
        if command.kind in {"catalog", "catalog_page"}:
            page = int(command.argument or "1")
            self._respond_card(
                build_slash_catalog_card(page),
                "remote_control",
                self._remote_payload(
                    thread, _text(payload.get("group")) or "个人会话"
                ),
                f"management-slash-catalog-bound:{message.message_id}:{page}",
                fallback_text=self._slash_catalog_text(page),
                ttl_days=30,
            )
            return
        if command.kind == "unavailable":
            self._respond_in_context(
                context_id,
                f"{command.argument} 当前不能从飞书安全执行。\n原因：{command.secondary}",
                f"management-slash-unavailable:{context_id}:{message.message_id}",
            )
            return
        if command.kind == "mcp_list":
            session = self._remote_prepare(_text(thread.get("id")))
            try:
                servers = []
                cursor: str | None = None
                for _page in range(20):
                    page_rows, cursor = session.mcp_servers(cursor=cursor, limit=20)
                    servers.extend(page_rows)
                    if not cursor:
                        break
                else:
                    raise CodexRPCError("MCP 状态分页超过安全上限")
            finally:
                session.close()
            lines = [f"会话名称：{title}", "", f"MCP 服务：{len(servers)} 个"]
            if not servers:
                lines.append("当前没有已配置的 MCP 服务。")
            for item in servers:
                lines.append(
                    f"- {item.name}｜运行 {item.runtime_status or 'unknown'}｜"
                    f"认证 {item.auth_status or 'unknown'}｜工具 {item.tool_count}｜"
                    f"资源 {item.resource_count}｜模板 {item.template_count}"
                )
            self._respond_in_context(
                context_id,
                "\n".join(lines),
                f"management-mcp-status:{context_id}:{message.message_id}",
            )
            return
        if command.kind == "status_get":
            session = self._remote_prepare(_text(thread.get("id")))
            try:
                snapshot = session.runtime_snapshot()
            finally:
                session.close()
            lines = [
                f"会话名称：{title}",
                f"会话状态：{snapshot.status or '当前官方响应未提供'}",
                f"模型：{snapshot.model or '当前官方响应未提供'}",
                f"推理强度：{snapshot.effort or '当前官方响应未提供'}",
                f"服务档位：{snapshot.service_tier or '默认'}",
                "上下文占用：当前官方远程协议未提供可靠快照",
            ]
            if self.account_reader is not None:
                try:
                    lines.extend(("", format_rate_limits(self.account_reader.read())))
                except CodexAccountError:
                    lines.extend(("", "额度：当前官方服务未返回可靠数据"))
            self._respond_in_context(
                context_id,
                "\n".join(lines),
                f"management-slash-status:{context_id}:{message.message_id}",
            )
            return
        if command.kind in {"model_list", "reasoning_list", "personality_list"}:
            session = self._remote_prepare(_text(thread.get("id")))
            try:
                current = session.runtime_snapshot()
                models = session.models()
            finally:
                session.close()
            if command.kind == "model_list":
                lines = [f"当前模型：{current.model or '未返回'}", "可选模型："]
                lines.extend(f"- {item.model_id}｜{item.display_name}" for item in models)
                lines.append("设置：回复“/model 模型ID”。")
            elif command.kind == "reasoning_list":
                selected = next((item for item in models if item.model_id == current.model), None)
                efforts = selected.efforts if selected is not None else ()
                lines = [f"当前推理强度：{current.effort or '未返回'}", "当前模型支持："]
                lines.extend(f"- {item}" for item in efforts)
                lines.append("设置：回复“/reasoning 档位”。")
            else:
                selected = next((item for item in models if item.model_id == current.model), None)
                lines = [
                    "官方可选个性：none、friendly、pragmatic",
                    "当前值只能在写后由 thread/settings/updated 精确确认。",
                    (
                        "当前模型支持个性设置。"
                        if selected is not None and selected.supports_personality
                        else "当前模型未声明支持个性设置。"
                    ),
                    "设置：回复“/personality 值”。",
                ]
            self._respond_in_context(
                context_id,
                "\n".join(lines),
                f"management-slash-options:{context_id}:{command.kind}:{message.message_id}",
            )
            return
        if command.kind == "memories_list":
            self._respond_in_context(
                context_id,
                "记忆模式可设置为 enabled 或 disabled。\n"
                "当前官方协议没有可靠的只读当前值；请选择后会先二次确认。\n"
                "操作：回复“/memories enabled”或“/memories disabled”。",
                f"management-memory-options:{context_id}:{message.message_id}",
            )
            return
        confirmation = {
            "compact_request": ("compact", "请求压缩当前会话上下文；服务接受不等于已经压缩完成。"),
            "fork_request": ("fork", "复制当前会话为新的同目录本地会话；不会传入路径或设置覆盖。"),
            "review_request": ("review", "在当前会话中启动对未提交改动的代码审查。"),
            "fast_request": ("fast", "按当前模型目录中的官方 Fast 档位切换。"),
            "memories_request": (
                "memory_enable" if command.argument == "enabled" else "memory_disable",
                f"把当前会话的记忆模式设置为 {command.argument}。",
            ),
        }.get(command.kind)
        if confirmation is not None:
            action, explanation = confirmation
            self._respond_card(
                build_confirmation_card(title, action, explanation),
                "remote_control",
                self._remote_payload(
                    thread, _text(payload.get("group")) or "个人会话"
                ),
                f"management-slash-confirm:{message.message_id}:{action}",
                fallback_text=(
                    f"需要二次确认：{explanation}\n"
                    "当前渠道无法显示确认按钮，本次没有执行。"
                ),
                ttl_days=30,
            )
            return
        if command.is_write:
            self._remote_slash_write(
                context_id, payload, thread, message, command
            )
            return
        raise ManagementUserError("该斜杠指令没有可执行的远程语义。")

    def _handle_remote_goal_clear_confirmation(
        self,
        message: ChannelReply,
        context_id: str,
        payload: Mapping[str, Any],
        content: str,
        *,
        owner_bound: bool,
    ) -> None:
        if not owner_bound:
            raise ManagementUserError("这条确认卡片没有安全绑定，请重新选择会话。")
        if content != "/goal clear confirm" or message.attachments:
            raise ManagementUserError("请点击“确认清除”；没有确认就不会改变 Goal。")
        thread = payload.get("thread")
        if not isinstance(thread, Mapping) or not _text(thread.get("id")):
            raise ManagementUserError("指令使用目标已损坏，请重新选择会话。")
        self._remote_write(
            context_id,
            payload,
            thread,
            message,
            confirmed_goal_clear_command(),
        )

    def _remote_goal_get(
        self,
        context_id: str,
        payload: Mapping[str, Any],
        thread: Mapping[str, Any],
        message: ChannelReply,
    ) -> None:
        session = self._remote_prepare(_text(thread.get("id")))
        try:
            goal = session.goal()
        except CodexRPCError as exc:
            raise ManagementUserError("Codex 当前没有返回可验证的 Goal 状态。") from exc
        finally:
            session.close()
        title = _compact(
            _text(thread.get("title")) or "新建会话", OVERVIEW_TITLE_MAX_CHARS
        )
        if goal is None:
            result = "当前没有设置正式 Goal。"
        else:
            lines = [f"当前 Goal：{goal.objective}"]
            if goal.status:
                lines.append(f"状态：{goal.status}")
            if goal.token_budget is not None:
                lines.append(f"Token 预算：{goal.token_budget}")
            if goal.tokens_used is not None:
                lines.append(f"已使用 Token：{goal.tokens_used}")
            result = "\n".join(lines)
        self._respond_in_context(
            context_id,
            f"会话名称：{title}\n\n{result}",
            f"management-remote-goal-get:{message.message_id}",
        )

    def _remote_skills_list(
        self,
        context_id: str,
        payload: Mapping[str, Any],
        thread: Mapping[str, Any],
        message: ChannelReply,
        command: RemoteCommand,
    ) -> None:
        session = self._remote_prepare(_text(thread.get("id")))
        try:
            # 打开、翻页和刷新都不能信任旧卡片缓存；实际调用前还会再次
            # forceReload 并校验 enabled/path，形成同一条实时能力链。
            skills = session.skills(force_reload=True)
        except CodexRPCError as exc:
            raise ManagementUserError("Codex 当前没有返回可验证的 Skills 列表。") from exc
        finally:
            session.close()
        page = int(command.argument or "1")
        pages = max(1, math.ceil(len(skills) / 10))
        if page > pages:
            raise ManagementUserError(
                f"当前 Skills 只有 {pages} 页，请重新打开或选择有效页码。"
            )
        start = (page - 1) * 10
        visible = skills[start : start + 10]
        title = _compact(
            _text(thread.get("title")) or "新建会话", OVERVIEW_TITLE_MAX_CHARS
        )
        fallback_lines = [
            f"会话名称：{title}",
            "",
            f"已启用 Skills：{len(skills)} 个｜第 {page}/{pages} 页",
        ]
        fallback_lines.extend(f"- {item.name}" for item in visible)
        fallback_lines.extend(
            (
                "",
                "操作说明：回复“/skill 技能名 具体要求”或“$技能名 具体要求”。",
            )
        )
        self._respond_card(
            build_skills_card(
                title,
                skills,
                page=page,
                refreshed_at=datetime.now(tz=BEIJING).strftime("%Y-%m-%d %H:%M:%S"),
            ),
            "remote_control",
            self._remote_payload(
                thread, _text(payload.get("group")) or "个人会话"
            ),
            f"management-remote-skills:{message.message_id}:{page}",
            fallback_text="\n".join(fallback_lines),
            ttl_days=30,
        )

    @staticmethod
    def _verified_turn_result(result: Mapping[str, Any]) -> tuple[str, str]:
        turn = result.get("turn")
        if not isinstance(turn, Mapping):
            raise CodexRPCError("turn/start 响应缺少 turn")
        turn_id = _text(turn.get("id"))
        status = _text(turn.get("status"))
        if not turn_id or status.casefold().replace("_", "") != "inprogress":
            raise CodexRPCError("turn/start 响应无法证明新轮次已启动")
        return turn_id, status

    def _remote_write(
        self,
        context_id: str,
        payload: Mapping[str, Any],
        thread: Mapping[str, Any],
        message: ChannelReply,
        command: RemoteCommand,
    ) -> None:
        if command.kind not in {"goal_set", "goal_clear", "plan_start", "skill_start"}:
            raise ManagementUserError("不是允许的远程写操作。")
        request_hash = command.request_hash()
        reservation = self.store.begin_remote_control_action(
            context_id,
            command.kind,
            request_hash,
            message.message_id,
        )
        if self._remote_action_state_reply(
            context_id, command, reservation.status, message.message_id
        ):
            return
        session = None
        submitted = False
        title = _compact(
            _text(thread.get("title")) or "新建会话", OVERVIEW_TITLE_MAX_CHARS
        )

        def before_send() -> None:
            nonlocal submitted
            if not self.store.mark_remote_control_action_submitted(
                context_id, command.kind, request_hash
            ):
                raise StateError("远程动作提交边界写入失败")
            submitted = True

        try:
            session = self._remote_prepare(_text(thread.get("id")))
            plan_mode: Mapping[str, Any] | None = None
            selected_skill: SkillSnapshot | None = None
            if command.kind == "plan_start":
                if session.is_active():
                    raise ManagementUserError(
                        "目标会话当前正在运行，Plan 模式没有启动；请等本轮结束后重试。"
                    )
                plan_mode = session.plan_mode()
                # 模式发现可能耗时；提交前必须再次读取官方活动状态。
                if session.is_active():
                    raise ManagementUserError(
                        "目标会话刚刚进入运行状态，Plan 模式没有启动。"
                    )
            elif command.kind == "skill_start":
                if session.is_active():
                    raise ManagementUserError(
                        "目标会话当前正在运行，Skill 没有启动；请等本轮结束后重试。"
                    )
                # forceReload 是提交前的第二次服务端校验；路径只取本次结果。
                matches = [
                    item
                    for item in session.skills(force_reload=True)
                    if item.name == command.skill_name
                ]
                if len(matches) != 1:
                    raise ManagementUserError(
                        "这个 Skill 当前未启用或已变化，没有提交请求；请重新发送 /skills。"
                    )
                selected_skill = matches[0]
                if session.is_active():
                    raise ManagementUserError(
                        "目标会话刚刚进入运行状态，Skill 没有启动。"
                    )
            if command.kind == "goal_set":
                result = session.set_goal(command.argument, before_send=before_send)
                goal = result.get("goal")
                if (
                    not isinstance(goal, Mapping)
                    or _text(goal.get("objective")) != command.argument
                ):
                    raise CodexRPCError("Goal 写入响应无法核对 objective")
                readback = session.goal()
                if readback is None or readback.objective != command.argument:
                    raise CodexRPCError("Goal 官方读回与提交值不一致")
                response_text = (
                    f"执行结果：已设置正式 Goal\n会话名称：{title}\n"
                    f"当前 Goal：{readback.objective}\n提交状态：官方接口已确认并读回。"
                )
                evidence = {"verified": True, "kind": "goal_set"}
            elif command.kind == "goal_clear":
                session.clear_goal(before_send=before_send)
                if session.goal() is not None:
                    raise CodexRPCError("Goal 清除后官方读回仍存在目标")
                response_text = (
                    f"执行结果：已清除正式 Goal\n会话名称：{title}\n"
                    "提交状态：官方接口已确认，读回显示当前无 Goal。"
                )
                evidence = {"verified": True, "kind": "goal_clear"}
            elif command.kind == "plan_start":
                assert plan_mode is not None
                turn_id, _status = self._verified_turn_result(
                    session.start_plan(
                        command.argument, plan_mode, before_send=before_send
                    )
                )
                response_text = (
                    f"执行结果：Plan 模式已启动\n会话名称：{title}\n"
                    "提交状态：Codex 官方 turn/start 已确认接受；后续进度仍会正常通知。"
                )
                evidence = {
                    "verified": True,
                    "kind": "plan_start",
                    "turn_id_sha256": hashlib.sha256(
                        turn_id.encode("utf-8")
                    ).hexdigest(),
                }
            else:
                assert selected_skill is not None
                turn_id, _status = self._verified_turn_result(
                    session.start_skill(
                        selected_skill, command.argument, before_send=before_send
                    )
                )
                response_text = (
                    f"执行结果：Skill 已启动\n会话名称：{title}\n"
                    f"Skill：{selected_skill.name}\n"
                    "提交状态：Codex 官方 turn/start 已确认接受；后续进度仍会正常通知。"
                )
                evidence = {
                    "verified": True,
                    "kind": "skill_start",
                    "turn_id_sha256": hashlib.sha256(
                        turn_id.encode("utf-8")
                    ).hexdigest(),
                }
        except ManagementUserError:
            if not submitted:
                self.store.release_remote_control_action(
                    context_id,
                    command.kind,
                    request_hash,
                    "preflight_rejected",
                )
            raise
        except RemoteWriteUnavailable as exc:
            self.store.release_remote_control_action(
                context_id, command.kind, request_hash, "capability_unverified"
            )
            raise ManagementUserError(
                "该 Codex 写操作尚未通过无 writer 抢占的真实能力验证；"
                "本次没有提交，也没有改发普通提示词。"
            ) from exc
        except CodexRPCRejected as exc:
            self.store.release_remote_control_action(
                context_id,
                command.kind,
                request_hash,
                "codex_rejected",
                allow_submitted=submitted,
            )
            raise ManagementUserError(
                "Codex 官方接口明确拒绝了这次操作；没有把它当作成功。"
            ) from exc
        except (CodexRPCClosed, CodexRPCTimeout, CodexRPCError, StateError) as exc:
            if submitted:
                self.store.mark_remote_control_action_uncertain(
                    context_id,
                    command.kind,
                    request_hash,
                    type(exc).__name__,
                )
                self._respond_in_context(
                    context_id,
                    "这次远程操作已经跨过提交边界，但结果无法确认。"
                    "为避免重复改变 Codex 状态，系统已冻结自动重试；"
                    "请在 Codex 桌面端核对后重新查询。",
                    f"management-remote-uncertain:{context_id}:{command.kind}:{message.message_id}",
                )
                return
            self.store.release_remote_control_action(
                context_id,
                command.kind,
                request_hash,
                "preflight_failed",
            )
            raise ManagementUserError(
                "远程操作在提交前失败；没有改变 Codex 状态，可以稍后重试。"
            ) from exc
        finally:
            if session is not None:
                session.close()
        if not self.store.complete_remote_control_action(
            context_id,
            command.kind,
            request_hash,
            result=evidence,
        ):
            self.store.mark_remote_control_action_uncertain(
                context_id,
                command.kind,
                request_hash,
                "completion_state_failed",
            )
            self._respond_in_context(
                context_id,
                "Codex 已返回明确成功，但本地成功证据写入失败。"
                "系统已停止重试，请勿再次提交同一动作。",
                f"management-remote-completion-uncertain:{context_id}:{command.kind}",
            )
            return
        self._respond_in_context(
            context_id,
            response_text,
            f"management-remote-success:{context_id}:{command.kind}:{message.message_id}",
        )

    def _remote_slash_write(
        self,
        context_id: str,
        payload: Mapping[str, Any],
        thread: Mapping[str, Any],
        message: ChannelReply,
        command: SlashCommand,
    ) -> None:
        allowed = {
            "compact_start", "fork_start", "review_start", "model_set",
            "personality_set", "reasoning_set", "fast_toggle", "memories_set",
        }
        if command.kind not in allowed:
            raise ManagementUserError("不是允许的远程斜杠写操作。")
        request_hash = command.request_hash()
        reservation = self.store.begin_remote_control_action(
            context_id,
            command.kind,
            request_hash,
            message.message_id,
        )
        synthetic = RemoteCommand(command.kind, command.argument)
        if self._remote_action_state_reply(
            context_id, synthetic, reservation.status, message.message_id
        ):
            return
        session = None
        submitted = False
        title = _compact(
            _text(thread.get("title")) or "新建会话", OVERVIEW_TITLE_MAX_CHARS
        )

        def before_send() -> None:
            nonlocal submitted
            if not self.store.mark_remote_control_action_submitted(
                context_id, command.kind, request_hash
            ):
                raise StateError("远程动作提交边界写入失败")
            submitted = True

        try:
            session = self._remote_prepare(_text(thread.get("id")))
            current = session.runtime_snapshot()
            if command.kind in {"compact_start", "fork_start", "review_start"}:
                if session.is_active():
                    raise ManagementUserError(
                        "目标会话当前正在运行，本次操作没有提交；请等本轮结束后重试。"
                    )
                if session.is_active():
                    raise ManagementUserError("目标会话状态刚刚变化，本次操作没有提交。")
            if command.kind == "compact_start":
                session.compact(before_send=before_send)
                response_text = (
                    f"执行结果：Codex 已接受上下文压缩请求\n会话名称：{title}\n"
                    "说明：这是异步请求，接受不等于已经压缩完成。"
                )
                evidence = {"verified": True, "kind": command.kind, "accepted": True}
            elif command.kind == "fork_start":
                result = session.fork(before_send=before_send)
                created = result.get("thread")
                created_id = _text(created.get("id")) if isinstance(created, Mapping) else ""
                source_id = _text(thread.get("id"))
                if not created_id or created_id == source_id:
                    raise CodexRPCError("thread/fork 响应没有可验证的新 thread id")
                response_text = (
                    f"执行结果：已复制为新的同目录本地会话\n来源会话：{title}\n"
                    f"新任务 ID：{created_id}"
                )
                evidence = {
                    "verified": True,
                    "kind": command.kind,
                    "new_thread_id_sha256": hashlib.sha256(created_id.encode("utf-8")).hexdigest(),
                }
            elif command.kind == "review_start":
                result = session.review_uncommitted(before_send=before_send)
                review_thread_id = _text(result.get("reviewThreadId"))
                turn = result.get("turn")
                turn_id = _text(turn.get("id")) if isinstance(turn, Mapping) else ""
                status = _text(turn.get("status")) if isinstance(turn, Mapping) else ""
                if (
                    not review_thread_id
                    or not turn_id
                    or status.casefold().replace("_", "") != "inprogress"
                ):
                    raise CodexRPCError("review/start 响应无法证明内联审查已启动")
                response_text = (
                    f"执行结果：未提交改动审查已启动\n会话名称：{title}\n"
                    f"审查任务 ID：{review_thread_id}"
                )
                evidence = {
                    "verified": True,
                    "kind": command.kind,
                    "turn_id_sha256": hashlib.sha256(turn_id.encode("utf-8")).hexdigest(),
                }
            elif command.kind == "memories_set":
                session.set_memory_mode(command.argument, before_send=before_send)
                response_text = (
                    f"执行结果：记忆模式设置请求已确认\n会话名称：{title}\n"
                    f"模式：{command.argument}\n"
                    "说明：当前协议没有独立只读端点，未伪造旧值。"
                )
                evidence = {"verified": True, "kind": command.kind, "mode": command.argument}
            else:
                models = session.models()
                selected = next((item for item in models if item.model_id == current.model), None)
                field: str
                expected: str | None
                if command.kind == "model_set":
                    target = next((item for item in models if item.model_id == command.argument), None)
                    if target is None:
                        raise ManagementUserError(
                            "该模型不在本次官方 model/list 中，没有提交设置。"
                        )
                    field, expected = "model", target.model_id
                elif command.kind == "reasoning_set":
                    supported_efforts = (
                        selected.efforts
                        if selected is not None
                        else tuple(
                            dict.fromkeys(
                                effort
                                for item in models
                                for effort in item.efforts
                            )
                        )
                    )
                    if command.argument not in supported_efforts:
                        raise ManagementUserError(
                            "该推理强度不在本次官方 model/list 中，没有提交设置。"
                        )
                    field, expected = "effort", command.argument
                elif command.kind == "personality_set":
                    if selected is not None and not selected.supports_personality:
                        raise ManagementUserError(
                            "当前模型未声明支持 personality，没有提交设置。"
                        )
                    field, expected = "personality", command.argument
                else:
                    if selected is None:
                        raise ManagementUserError("当前模型不在官方目录中，无法切换 Fast。")
                    fast_tiers = [
                        tier
                        for tier in selected.service_tiers
                        if re.search(
                            r"(?<![a-z])fast(?![a-z])",
                            f"{tier[1]} {tier[2]}",
                            re.I,
                        )
                    ]
                    if len(fast_tiers) != 1:
                        raise ManagementUserError(
                            "当前模型目录没有唯一、语义明确的 Fast 档位，没有提交设置。"
                        )
                    fast_tier = fast_tiers[0]
                    expected = (
                        selected.default_service_tier
                        if current.service_tier == fast_tier[0]
                        else fast_tier[0]
                    )
                    field = "serviceTier"
                settings = session.update_setting(
                    field,
                    expected,
                    before_send=before_send,
                )
                if field not in settings or settings[field] != expected:
                    raise CodexRPCError("thread/settings/updated 与提交值不一致")
                response_text = (
                    f"执行结果：Codex 后续轮次设置已更新\n会话名称：{title}\n"
                    f"设置项：{field}\n当前值：{expected if expected is not None else '默认'}"
                )
                evidence = {
                    "verified": True,
                    "kind": command.kind,
                    "field": field,
                    "value": expected,
                }
        except ManagementUserError:
            if not submitted:
                self.store.release_remote_control_action(
                    context_id, command.kind, request_hash, "preflight_rejected"
                )
            raise
        except RemoteWriteUnavailable as exc:
            self.store.release_remote_control_action(
                context_id, command.kind, request_hash, "capability_unverified"
            )
            raise ManagementUserError(
                "该 Codex 写操作尚未通过无 writer 抢占的真实能力验证；"
                "本次没有提交，也没有改发普通提示词。"
            ) from exc
        except CodexRPCRejected as exc:
            self.store.release_remote_control_action(
                context_id,
                command.kind,
                request_hash,
                "codex_rejected",
                allow_submitted=submitted,
            )
            raise ManagementUserError(
                "Codex 官方接口明确拒绝了这次操作；没有把它当作成功。"
            ) from exc
        except (CodexRPCClosed, CodexRPCTimeout, CodexRPCError, StateError) as exc:
            if submitted:
                self.store.mark_remote_control_action_uncertain(
                    context_id, command.kind, request_hash, type(exc).__name__
                )
                self._respond_in_context(
                    context_id,
                    "这次 Codex 操作已经跨过提交边界，但结果无法确认。"
                    "系统已冻结自动重试，请在桌面端核对。",
                    f"management-slash-uncertain:{context_id}:{command.kind}:{message.message_id}",
                )
                return
            self.store.release_remote_control_action(
                context_id, command.kind, request_hash, "preflight_failed"
            )
            raise ManagementUserError(
                "操作在提交前失败，没有改变 Codex 状态，可以稍后重试。"
            ) from exc
        finally:
            if session is not None:
                session.close()
        if not self.store.complete_remote_control_action(
            context_id, command.kind, request_hash, result=evidence
        ):
            self.store.mark_remote_control_action_uncertain(
                context_id, command.kind, request_hash, "completion_state_failed"
            )
            self._respond_in_context(
                context_id,
                "Codex 已返回明确成功，但本地成功证据写入失败；系统已停止重试。",
                f"management-slash-completion-uncertain:{context_id}:{command.kind}",
            )
            return
        self._respond_in_context(
            context_id,
            response_text,
            f"management-slash-success:{context_id}:{command.kind}:{message.message_id}",
        )

    def _send_overview(
        self,
        thread: Mapping[str, Any],
        group: str,
        request_id: str,
        *,
        selected_current: bool = False,
    ) -> None:
        thread_id = _text(thread.get("id"))
        # 概览的轮次状态必须取真正最新的一轮；``latest_terminal_turn`` 刻意
        # 会跳过活动轮次，只适合读取上一条已结束结果，不能用于此处展示。
        latest_turn = self.codex_store.latest_turn(thread_id)
        query_snapshot: dict[str, Any] = {}
        detail: Mapping[str, Any] = {}
        session: _DesktopSession | None = None
        try:
            session = self._open_desktop(("read_thread",))
            detail = session.tools.read_thread(
                session.source_thread_id,
                thread_id,
                host_id=_text(thread.get("hostId")),
                turn_limit=10,
                include_outputs=False,
                max_output_chars_per_item=4000,
            )
        except DesktopAppToolsError as exc:
            # read_thread 对正在运行、尚未加载或较旧的任务可能暂时不可读。
            # 会话已经由本地完整索引精确选中，因此详情读取只用于增强展示，
            # 不能让它阻断概览绑定和后续续聊。
            LOGGER.warning(
                "Codex 会话详情暂不可读，使用本地结构化记录生成概览 "
                "thread=%s error=%s",
                thread_id,
                type(exc).__name__,
            )
        finally:
            if session is not None:
                session.tools.close()
        overall = _compact(_text(thread.get("summary")) or _text(thread.get("preview")), 260)
        detail_latest_turn = _detail_latest_turn(detail)
        detail_latest_status = (
            _canonical_turn_status(detail_latest_turn.get("status"))
            if detail_latest_turn is not None
            else ""
        )
        latest_round_status = (
            _canonical_turn_status(getattr(latest_turn, "status", ""))
            if latest_turn is not None
            else detail_latest_status
        )
        latest_round_time = (
            _turn_time_value(latest_turn)
            if latest_turn is not None
            else _turn_time_value(detail_latest_turn)
        )
        completed_summary = ""
        completed_snapshot_id = ""
        completed_turn = None
        if self.session_search is not None:
            snapshot_reader = getattr(
                self.session_search, "result_snapshot_for_thread", None
            )
            if callable(snapshot_reader):
                frozen = snapshot_reader(thread_id)
            else:
                latest_raw, legacy_completed_at = (
                    self.session_search.last_result_for_thread(thread_id)
                )
                candidate_turn = self.codex_store.latest_completed_result_turn(thread_id)
                candidate_status = (
                    _canonical_turn_status(getattr(candidate_turn, "status", ""))
                    if candidate_turn is not None
                    else ""
                )
                completed_turn = (
                    candidate_turn if candidate_status == "completed" else None
                )
                legacy_raw = (
                    completed_turn.final_message if completed_turn is not None else ""
                )
                legacy_hash = hashlib.sha256(legacy_raw.encode("utf-8")).hexdigest()
                frozen = {
                    # 旧兼容接口可能只返回一段摘要而没有完成轮次身份；当
                    # 当前已经读到 active/failed/cancelled 时，不能把它当作
                    # 一条旧完成结果。无明确最新轮次时保留旧测试/部署兼容。
                    "summary": (
                        latest_raw
                        if completed_turn is not None or not latest_round_status
                        else ""
                    ),
                    "completed_at": legacy_completed_at,
                    "turn_id": completed_turn.turn_id if completed_turn is not None else "",
                    "content_hash": legacy_hash if completed_turn is not None else "",
                    "raw_final": legacy_raw,
                    "raw_sha256": legacy_hash,
                }
            if isinstance(frozen, Mapping):
                completed_summary = _compact(_text(frozen.get("summary")), 500)
                if completed_summary == _NO_FINAL_RESULT:
                    completed_summary = ""
                completed_snapshot_id = _text(frozen.get("turn_id"))
            if isinstance(frozen, Mapping) and completed_snapshot_id:
                query_snapshot = {
                    "turn_id": completed_snapshot_id,
                    "content_hash": _text(frozen.get("content_hash")),
                    "raw_final": str(frozen.get("raw_final") or ""),
                    "raw_sha256": _text(frozen.get("raw_sha256")),
                }
        else:
            completed_turn = self.codex_store.latest_completed_result_turn(thread_id)
            completed_status = (
                _canonical_turn_status(getattr(completed_turn, "status", ""))
                if completed_turn is not None
                else ""
            )
            local_final = (
                str(completed_turn.final_message or "")
                if completed_turn is not None and completed_status == "completed"
                else ""
            )
            completed_summary = _compact(local_final, 500)
            if completed_turn is not None and completed_status == "completed":
                completed_snapshot_id = _text(completed_turn.turn_id)
                raw_hash = hashlib.sha256(local_final.encode("utf-8")).hexdigest()
                query_snapshot = {
                    "turn_id": completed_turn.turn_id,
                    "content_hash": raw_hash,
                    "raw_final": local_final,
                    "raw_sha256": raw_hash,
                }
        if not completed_summary and latest_round_status in {"", "completed"}:
            # 旧 Desktop 详情没有本地最终结果投影时，仍兼容读取其结构化完成
            # 答复；活动/失败/取消轮次绝不能让这段旧文本冒充最新结果。
            completed_summary = _compact(_latest_final(detail), 500)

        latest_result = ""
        recent_completed_result = ""
        if latest_round_status == "completed":
            if (
                latest_turn is None
                or not completed_snapshot_id
                or _text(getattr(latest_turn, "turn_id", "")) == completed_snapshot_id
            ):
                latest_result = completed_summary
            else:
                latest_result = _compact(
                    str(getattr(latest_turn, "final_message", "") or ""), 500
                )
                recent_completed_result = completed_summary
        elif latest_round_status:
            latest_result = _NO_FINAL_RESULT
            recent_completed_result = completed_summary
        else:
            # 没有可读的结构化最新轮次时，保留旧版本概览的兼容行为；一旦
            # 读到了 active/failed/cancelled，则上面的分支会明确隔离旧结果。
            latest_result = completed_summary
        updated_text = _format_management_time(thread.get("updatedAt"))
        if query_snapshot:
            query_snapshot["snapshot_key"] = hashlib.sha256(
                (
                    f"management-overview-snapshot-v1\0{request_id}\0{thread_id}\0"
                    f"{_text(query_snapshot.get('turn_id'))}\0"
                    f"{_text(query_snapshot.get('content_hash'))}"
                ).encode("utf-8")
            ).hexdigest()
        raw_status = (
            detail.get("thread", {}).get("status")
            if isinstance(detail.get("thread"), dict)
            else ""
        )
        detail_status = (
            _text(raw_status.get("type"))
            if isinstance(raw_status, dict)
            else _text(raw_status)
        )
        display_status = latest_round_status or _canonical_turn_status(
            _text(thread.get("status"))
        ) or _canonical_turn_status(detail_status) or "未知"
        subscription = next(
            (
                item for item in self.store.monitor_subscriptions()
                if item["thread_id"] == _text(thread.get("id"))
            ),
            None,
        )
        monitor_status = (
            "手动永久"
            if subscription is not None and subscription["origin"] == "manual"
            else "自动（24 小时无活动后到期）"
            if subscription is not None
            else "未监测"
        )
        prepared_images: list[tuple[GeneratedImageArtifact, bytes]] = []
        skipped_images = 0
        image_turn = (
            latest_turn if latest_turn is not None and latest_round_status == "completed" else None
        )
        if self.send_image is not None and image_turn is not None:
            for artifact in image_turn.generated_images:
                try:
                    data = read_generated_image_bytes(artifact)
                except ValueError:
                    skipped_images += 1
                    LOGGER.warning(
                        "跳过已变化或不可读的会话概览图片 thread=%s turn=%s item=%s",
                        image_turn.thread_id,
                        image_turn.turn_id,
                        artifact.item_id,
                    )
                else:
                    prepared_images.append((artifact, data))
        display_title = _text(thread.get("title")) or "未命名会话"
        overview_blocks: list[Sequence[str]] = []
        if selected_current:
            overview_blocks.append(
                [
                    "已设为当前会话："
                    f"{_book_title(display_title, OVERVIEW_TITLE_MAX_CHARS)}"
                ]
            )
        overview_blocks.extend([
            [
                _message_field(
                    "会话名称",
                    _compact(
                        display_title,
                        OVERVIEW_TITLE_MAX_CHARS,
                    ),
                ),
                _message_field("归属", group),
                _message_field(
                    "状态", display_status
                ),
                _message_field("最近更新", updated_text),
                _message_field("监测状态", monitor_status),
                *(
                    [
                        _message_field("最后一轮状态", latest_round_status),
                        _message_field(
                            "最后一轮时间", _format_management_time(latest_round_time)
                        ),
                    ]
                    if latest_round_status
                    else []
                ),
            ],
            [_message_field("整体概览", overall or "Codex 暂未提供概览。")],
            [
                _message_field(
                    "最后一轮结果",
                    latest_result or _NO_FINAL_RESULT,
                )
            ],
        ])
        if recent_completed_result:
            overview_blocks.append(
                [_message_field("最近完成结果", recent_completed_result)]
            )
        if prepared_images:
            overview_blocks.append(
                [_message_field("最近生成图片", f"{len(prepared_images)} 张将在下方直接展示。")]
            )
        if skipped_images:
            overview_blocks.append(
                [_message_field("最近生成图片", f"{skipped_images} 张原图已失效，未发送。")]
            )
        overview_blocks.append([
            "操作说明：",
            "- 继续会话：直接回复本消息并发送文字",
            "- 管理监测：回复“添加监测”或“移除监测”",
            "- 查看本次原文：回复“.原文”（本次查询限一次）",
            "- 归档该会话：回复“.归档”（本次查询限一次）",
        ])
        payload = _message_blocks(*overview_blocks)
        stored = dict(thread)
        stored.pop("summary", None)
        card_facts = [
            ("归属", group),
            ("状态", display_status),
            ("最近更新", updated_text),
            ("监测状态", monitor_status),
        ]
        if latest_round_status:
            card_facts.extend(
                (
                    ("最后一轮状态", latest_round_status),
                    ("最后一轮时间", _format_management_time(latest_round_time)),
                )
            )
        card_sections: list[tuple[str, str]] = []
        if selected_current:
            card_sections.append(
                (
                    "当前会话",
                    "已设为当前会话："
                    f"{_book_title(display_title, OVERVIEW_TITLE_MAX_CHARS)}",
                )
            )
        card_sections.extend([
            ("整体概览", overall or "Codex 暂未提供概览。"),
            ("最后一轮结果", latest_result or _NO_FINAL_RESULT),
        ])
        if recent_completed_result:
            card_sections.append(("最近完成结果", recent_completed_result))
        if prepared_images:
            card_sections.append(
                ("最近生成图片", f"{len(prepared_images)} 张将在卡片下方直接展示。")
            )
        if skipped_images:
            card_sections.append(
                ("图片提示", f"{skipped_images} 张原图已失效，未发送。")
            )
        context_id = self._respond_card(
            build_thread_overview_card(
                title=_text(thread.get("title")) or "新建会话",
                facts=card_facts,
                sections=card_sections,
                monitor_status=monitor_status,
            ),
            "thread_overview",
            {
                "thread": stored,
                "group": group,
                "query_snapshot": query_snapshot,
            },
            f"management-overview:{request_id}:{thread.get('id')}",
            fallback_text=payload,
            ttl_days=30,
        )
        failed_images = 0
        for artifact, data in prepared_images:
            try:
                message_ids = self.send_image(
                    data,
                    (
                        f"management-overview:{request_id}:{thread.get('id')}:image:"
                        f"{artifact.item_id}:{artifact.sha256}"
                    ),
                ) if self.send_image is not None else ()
                if not message_ids:
                    raise DesktopAppToolsError("飞书渠道未返回可绑定的图片 message_id")
                self.store.bind_management_messages(context_id, message_ids)
            except Exception as exc:
                failed_images += 1
                LOGGER.warning(
                    "会话概览图片发送失败 thread=%s item=%s error=%s",
                    _text(thread.get("id")),
                    artifact.item_id,
                    type(exc).__name__,
                )
        if failed_images:
            try:
                warning_ids = self.send_text(
                    f"图片发送失败：{failed_images} 张原图暂未送达，请稍后重新选定该会话。",
                    f"management-overview-image-warning:{request_id}:{thread.get('id')}",
                )
                if warning_ids:
                    self.store.bind_management_messages(context_id, warning_ids)
            except Exception:
                LOGGER.exception(
                    "会话概览图片失败提示发送失败 thread=%s",
                    _text(thread.get("id")),
                )

    def _send_usage_images(self, context_id: str, request_id: str) -> bool:
        if self.send_image is None:
            return False
        try:
            images = feishu_usage_images()
        except OSError as exc:
            LOGGER.warning("使用说明课堂图片不可读 error=%s", type(exc).__name__)
            warning_ids = self.send_text(
                "课堂图片暂时不可读；可发送“.文字版使用说明”查看文字内容。",
                f"management-usage-images-missing:{request_id}:{USAGE_VERSION}",
            )
            if warning_ids:
                self.store.bind_management_messages(context_id, warning_ids)
            return False
        for index, (_name, data) in enumerate(images, start=1):
            message_ids = self.send_image(
                data,
                f"management-usage:{request_id}:image:{index}:{USAGE_VERSION}",
            )
            if not message_ids:
                raise DesktopAppToolsError("飞书渠道未返回课堂图片 message_id")
            self.store.bind_management_messages(context_id, message_ids)
        return True

    def _action_status_reply(
        self,
        context_id: str,
        action_label: str,
        status: str,
        message_id: str,
    ) -> bool:
        if status == "claimed":
            return False
        if status == "succeeded":
            text = (
                f"该次查询的“{action_label}”已使用；没有再次执行。\n"
                "未来重新查询仍会默认显示总结。"
            )
        elif status == "uncertain":
            text = (
                f"该次查询的“{action_label}”结果无法确认。\n"
                "为避免重复外部操作，系统已停止自动重试；请重新查询后核对状态。"
            )
        else:
            text = f"该次查询的“{action_label}”正在处理，请勿重复提交。"
        self._respond_in_context(
            context_id,
            text,
            f"management-action-state:{context_id}:{action_label}:{message_id}",
        )
        return True

    @staticmethod
    def _raw_query_snapshot(payload: Mapping[str, Any]) -> Mapping[str, str] | None:
        raw_snapshot = payload.get("query_snapshot")
        if not isinstance(raw_snapshot, Mapping):
            return None
        raw = str(raw_snapshot.get("raw_final") or "")
        turn_id = _text(raw_snapshot.get("turn_id"))
        content_hash = _text(raw_snapshot.get("content_hash"))
        raw_sha256 = _text(raw_snapshot.get("raw_sha256"))
        snapshot_key = _text(raw_snapshot.get("snapshot_key"))
        if raw and raw_sha256 != hashlib.sha256(raw.encode("utf-8")).hexdigest():
            raise ManagementUserError("这次查询的原文快照校验失败，请重新查询。")
        if raw and (
            not turn_id
            or re.fullmatch(r"[0-9a-f]{64}", content_hash) is None
            or re.fullmatch(r"[0-9a-f]{64}", snapshot_key) is None
        ):
            raise ManagementUserError("这次查询的原文快照标识已损坏，请重新查询。")
        return {
            "raw_final": raw,
            "turn_id": turn_id,
            "content_hash": content_hash,
            "raw_sha256": raw_sha256,
            "snapshot_key": snapshot_key,
        }

    def _send_raw_action(
        self,
        context_id: str,
        message: ChannelReply,
        payload: Mapping[str, Any],
    ) -> None:
        snapshot = self._raw_query_snapshot(payload)
        if snapshot is None:
            self._respond_in_context(
                context_id,
                "这条消息不是带原文快照的新查询结果。请重新查询会话后，再回复“.原文”。",
                f"management-raw-unavailable:{context_id}:{message.message_id}",
            )
            return
        existing = self.store.management_context_action(context_id, "raw")
        if existing is not None:
            status = (
                "succeeded" if existing.get("succeeded_at") is not None else
                "uncertain" if existing.get("uncertain_at") is not None else
                "busy" if existing.get("claimed_at") is not None else
                "retryable"
            )
            if status != "retryable" and self._action_status_reply(
                context_id, ".原文", status, message.message_id
            ):
                return
        raw = snapshot["raw_final"]
        if (
            not raw
            or not snapshot["turn_id"]
            or not snapshot["content_hash"]
            or not snapshot["snapshot_key"]
        ):
            self._respond_in_context(
                context_id,
                "这次查询没有对应的可用最终答复原文；没有消耗“.原文”机会。",
                f"management-raw-empty:{context_id}:{message.message_id}",
            )
            return
        reservation = self.store.begin_management_context_action(
            context_id, "raw", message.message_id
        )
        if self._action_status_reply(
            context_id, ".原文", reservation.status, message.message_id
        ):
            return
        if not self.store.mark_management_context_action_submitted(context_id, "raw"):
            self.store.release_management_context_action(
                context_id, "raw", "submit_boundary_failed"
            )
            raise StateError("无法落下原文发送提交边界")
        key = f"management-action:{context_id}:raw"
        try:
            message_ids = self.send_text(
                "以下是本次查询摘要对应的最后一轮原文：\n\n"
                f"{raw}\n\n"
                "（本次仅查看原文，不会改变以后查询默认使用总结。）",
                key,
            )
        except (MessageChannelOfflineError, ValueError):
            self.store.release_management_context_action(
                context_id,
                "raw",
                "not_submitted",
                allow_submitted=True,
            )
            raise
        except BaseException as exc:
            self.store.mark_management_context_action_uncertain(
                context_id, "raw", type(exc).__name__
            )
            raise
        if not message_ids:
            self.store.mark_management_context_action_uncertain(
                context_id, "raw", "missing_message_id"
            )
            raise DesktopAppToolsResultUnknown("飞书原文发送结果缺少 message_id")
        try:
            self.store.bind_management_messages(context_id, message_ids)
            if not self.store.complete_management_context_action(
                context_id,
                "raw",
                message_ids=message_ids,
                redact_snapshot_key=snapshot["snapshot_key"],
            ):
                raise StateError("原文已发送但动作完成状态未写入")
        except BaseException as exc:
            self.store.mark_management_context_action_uncertain(
                context_id, "raw", type(exc).__name__
            )
            raise

    def _archive_action(
        self,
        context_id: str,
        message: ChannelReply,
        payload: Mapping[str, Any],
        thread: Mapping[str, Any],
    ) -> None:
        if "query_snapshot" not in payload:
            self._respond_in_context(
                context_id,
                "这条消息不是新的查询结果。请重新查询会话后，再回复“.归档”。",
                f"management-archive-unavailable:{context_id}:{message.message_id}",
            )
            return
        reservation = self.store.begin_management_context_action(
            context_id, "archive", message.message_id
        )
        if self._action_status_reply(
            context_id, ".归档", reservation.status, message.message_id
        ):
            return
        title = _compact(
            _text(thread.get("title")) or "新建会话", OVERVIEW_TITLE_MAX_CHARS
        )
        if bool(thread.get("archived")):
            if not self.store.complete_management_context_action(context_id, "archive"):
                raise StateError("已归档状态无法写入动作完成记录")
            self._respond_in_context(
                context_id,
                f"执行结果：该会话已经归档\n会话名称：{title}",
                f"management-archive-already:{context_id}",
            )
            return
        session: _DesktopSession | None = None
        try:
            session = self._open_desktop(("set_thread_archived",))
        except DesktopAppToolsError as exc:
            self.store.release_management_context_action(
                context_id, "archive", type(exc).__name__
            )
            self._respond_in_context(
                context_id,
                "归档尚未提交：Codex Desktop 当前不可用。该次“.归档”没有消耗，可稍后重试。",
                f"management-archive-retry:{context_id}:{message.message_id}",
            )
            return
        try:
            if not self.store.mark_management_context_action_submitted(
                context_id, "archive"
            ):
                self.store.release_management_context_action(
                    context_id, "archive", "submit_boundary_failed"
                )
                raise StateError("无法落下归档提交边界")
            session.tools.set_thread_archived(
                _text(thread.get("id")),
                archived=True,
                source_thread_id=session.source_thread_id,
                host_id=_text(thread.get("hostId")),
                call_tag=f"management-archive-{message.message_id[:12]}",
            )
        except (
            DesktopAppToolsUnavailable,
            DesktopAppToolsNotSubmitted,
            DesktopAppToolsRejected,
        ) as exc:
            self.store.release_management_context_action(
                context_id,
                "archive",
                type(exc).__name__,
                allow_submitted=True,
            )
            self._respond_in_context(
                context_id,
                "归档被明确拒绝或尚未提交。该次“.归档”没有消耗，可稍后重试。",
                f"management-archive-rejected:{context_id}:{message.message_id}",
            )
            return
        except (
            DesktopAppToolsResultUnknown,
            OSError,
            EOFError,
            TimeoutError,
            DesktopAppToolsError,
        ) as exc:
            self.store.mark_management_context_action_uncertain(
                context_id, "archive", type(exc).__name__
            )
            self._respond_in_context(
                context_id,
                "归档结果无法确认。为避免重复操作，系统不会盲目重试；"
                "请在 Codex 桌面端核对，或重新查询后再决定。",
                f"management-archive-uncertain:{context_id}",
            )
            return
        finally:
            if session is not None:
                try:
                    session.tools.close()
                except BaseException as exc:
                    # 官方归档调用已返回明确成功时，关闭本地命名管道失败不改变
                    # 外部操作结果，也不能阻止本地动作写入成功。只记录异常类型，
                    # 不包含线程标题、原文或其它用户内容。
                    LOGGER.warning(
                        "归档完成后关闭 Codex Desktop 工具管道失败（异常类型=%s）",
                        type(exc).__name__,
                    )
        if not self.store.complete_management_context_action(context_id, "archive"):
            self.store.mark_management_context_action_uncertain(
                context_id, "archive", "completion_state_failed"
            )
            self._respond_in_context(
                context_id,
                "归档调用已返回，但本地确认状态写入失败。为避免重复操作，系统已停止重试。",
                f"management-archive-completion-uncertain:{context_id}",
            )
            return
        self._respond_in_context(
            context_id,
            f"执行结果：已归档会话\n会话名称：{title}\n提交状态：Codex Desktop 官方工具已确认接受。",
            f"management-archive-success:{context_id}",
        )

    def _continue_thread(
        self,
        message: ChannelReply,
        context_id: str,
        payload: Mapping[str, Any],
        *,
        owner_bound: bool,
    ) -> None:
        thread = payload.get("thread")
        if not isinstance(thread, dict) or not _text(thread.get("id")):
            raise ManagementUserError("会话上下文已损坏，请重新查询并选定。")
        if message.attachment_error:
            raise ManagementUserError(message.attachment_error)
        raw_content = message.content
        command = raw_content.strip()
        if (
            message.source_kind == "card_action"
            and message.action_name == "thread_reply_open"
        ):
            if not owner_bound:
                raise ManagementUserError(
                    "这条会话概览没有安全绑定，请重新查询后再继续对话。"
                )
            title = _compact(
                _text(thread.get("title")) or "新建会话", OVERVIEW_TITLE_MAX_CHARS
            )
            self._respond_card(
                build_thread_reply_form_card(title),
                "thread_reply_form",
                self._continued_overview_payload(payload),
                f"management-thread-reply-form:{context_id}:{message.message_id}",
                fallback_text=(
                    f"会话名称：{title}\n\n"
                    "当前渠道无法显示续聊表单。请直接引用本消息发送下一步要求；"
                    "以 / 或 $ 开头的内容仍按正式控制指令处理。"
                ),
                ttl_days=30,
            )
            return
        action = _ONE_TIME_ACTIONS.get(command)
        if action is not None:
            if message.attachments:
                raise ManagementUserError("一次性点指令不能同时附带图片，请只回复指令本身。")
            if not owner_bound:
                self._respond_in_context(
                    context_id,
                    "这条查询结果生成于安全绑定升级前，不能执行“.原文”或“.归档”。"
                    "请重新查询该会话后再操作；普通续聊和监测不受影响。",
                    f"management-action-ownerless:{context_id}:{message.message_id}",
                )
                return
            if action == "raw":
                self._send_raw_action(context_id, message, payload)
            else:
                self._archive_action(context_id, message, payload, thread)
            return
        if _DIRECT_FEATURE_ALIASES.get(command, command) == REMOTE_CONTROL_ENTRY_COMMAND:
            if not owner_bound:
                raise ManagementUserError(
                    "这条会话概览没有安全绑定，请重新查询后再打开指令使用。"
                )
            self._send_remote_control_card(
                thread,
                _text(payload.get("group")) or "个人会话",
                message.message_id,
            )
            return
        if _is_direct_control_line(raw_content):
            direct_command = raw_content.rstrip()
            self._handle_remote_control(
                message,
                context_id,
                payload,
                direct_command,
                owner_bound=owner_bound,
            )
            return
        if command == "添加监测":
            self._add_manual_monitor(
                thread,
                message,
                "thread_overview",
                self._continued_overview_payload(payload),
            )
            return
        if command == "移除监测":
            self.store.remove_monitor(_text(thread.get("id")))
            self._respond(
                "执行结果：已移除监测\n"
                f"会话名称：{_compact(_text(thread.get('title')) or '新建会话', OVERVIEW_TITLE_MAX_CHARS)}\n"
                "提交状态：已写入抑制，除非你手动重新添加，否则不会被自动发现恢复。",
                "thread_overview",
                self._continued_overview_payload(payload),
                f"management-monitor-remove-overview:{message.message_id}",
            )
            return
        prompt = codex_prompt_for_reply(message)
        if not prompt.strip():
            raise ManagementUserError("发送内容不能为空。")
        session = self._open_desktop(("send_message_to_thread",))
        try:
            session.tools.send_message(
                _text(thread.get("id")),
                prompt,
                call_tag=f"management-send-{message.message_id[:12]}",
                source_thread_id=session.source_thread_id,
                host_id=_text(thread.get("hostId")),
            )
        finally:
            session.tools.close()
        self._respond(
            "执行结果：消息已发送\n"
            f"会话名称：{_compact(_text(thread.get('title')) or '新建会话', OVERVIEW_TITLE_MAX_CHARS)}\n"
            "提交状态：正文已原样送达；后续进度、完成结果或路线选择仍会通过飞书通知。\n\n"
            "操作说明：可继续回复本消息，再发送下一段内容。",
            "thread_overview",
            self._continued_overview_payload(payload),
            f"management-sent:{message.message_id}",
        )

    def _new_project_form(self, request_id: str) -> None:
        self._respond(
            "操作类型：新建 Codex 项目\n\n"
            "填写说明：复制整段，然后只在冒号后面填上想要的内容就可以了哦。\n"
            "项目名称：\n"
            "是否需要第一段会话：\n"
            "首轮对话提示词：",
            "new_project_form",
            {},
            f"management-new-project-form:{request_id}",
        )

    def _monitor_form(self, action: str, request_id: str) -> None:
        adding = action == "add"
        self._respond(
            f"操作类型：{'添加' if adding else '移除'}监测任务\n\n"
            "填写说明：请粘贴完整任务 ID 并回复本消息。\n"
            "任务 ID：",
            "monitor_add_form" if adding else "monitor_remove_form",
            {},
            f"management-monitor-{action}-form:{request_id}",
        )

    def _claim_write_form(self, context_id: str) -> None:
        if not self.store.claim_management_target_selection(
            context_id, marker="write"
        ):
            raise ManagementUserError(
                "这张写入表单已经提交过，本次没有重复执行；请重新打开对应功能。"
            )

    def _apply_monitor_form(
        self, message: ChannelReply, action: str, context_id: str
    ) -> None:
        form = _parse_form(message.content, (), "任务 ID")
        thread_id = form["任务 ID"].strip()
        if not thread_id:
            raise ManagementUserError("任务 ID 不能为空。")
        if action == "add":
            record = self.codex_store.get_thread(thread_id)
            self.codex_store.require_readable("验证待添加监测任务")
            if record is None or record.thread_source == "subagent":
                raise ManagementUserError("任务不存在、不可见或属于内部子任务。")
            activity = _timestamp(record.updated_at_ms) or int(time.time())
            self._claim_write_form(context_id)
            self.store.add_manual_monitor(thread_id, last_activity_at=activity)
            result = "已添加永久手动监测"
        else:
            self._claim_write_form(context_id)
            self.store.remove_monitor(thread_id)
            result = "已移除监测并抑制自动恢复"
        self._respond(
            f"执行结果：{result}\n任务 ID：{thread_id}",
            "management_created",
            {"thread_id": thread_id, "action": action},
            f"management-monitor-{action}:{message.message_id}",
        )

    def _add_manual_monitor(
        self,
        thread: Mapping[str, Any],
        message: ChannelReply,
        context_kind: str,
        payload: Mapping[str, Any],
    ) -> None:
        thread_id = _text(thread.get("id"))
        activity = _timestamp(thread.get("updatedAt")) or int(time.time())
        self.store.add_manual_monitor(thread_id, last_activity_at=activity)
        self._respond(
            "执行结果：已添加永久手动监测\n"
            f"会话名称：{_compact(_text(thread.get('title')) or '新建会话', OVERVIEW_TITLE_MAX_CHARS)}\n"
            f"任务 ID：{thread_id}",
            context_kind,
            payload,
            f"management-monitor-add:{message.message_id}",
        )

    def _register_created_auto_monitor(self, created: Mapping[str, Any]) -> None:
        thread_id = _text(created.get("threadId")) or _text(created.get("clientThreadId"))
        if thread_id:
            now = int(time.time())
            self.store.discover_auto_monitor(
                thread_id, last_activity_at=now, now=now, ttl_seconds=86_400
            )

    def _handle_monitor_list(
        self, message: ChannelReply, payload: Mapping[str, Any], content: str
    ) -> None:
        items = payload.get("items")
        if not isinstance(items, list):
            raise ManagementUserError("监测列表快照已损坏，请重新查询。")
        page_match = _PAGE.fullmatch(content)
        if page_match:
            # 重新查询可反映刚发生的自动到期或手动操作。
            session = self._open_desktop(("list_projects",))
            try:
                catalog, registry = self._catalog(session.listing)
                self._send_monitor_page(
                    catalog, registry, int(page_match.group(1)), message.message_id
                )
            finally:
                session.tools.close()
            return
        match = _REMOVE_MONITOR.fullmatch(content)
        if not match:
            raise ManagementUserError("请回复“移除m01”或“第2页”。")
        item = next(
            (item for item in items if isinstance(item, dict) and item.get("label") == match.group(1)),
            None,
        )
        if item is None:
            raise ManagementUserError("监测标号不在这份历史快照中。")
        self.store.remove_monitor(_text(item.get("thread_id")))
        self._respond(
            "执行结果：已移除监测\n"
            f"会话名称：{_text(item.get('title'))}\n"
            "提交状态：已抑制自动恢复；手动添加后才会重新监测。",
            "monitor_list",
            payload,
            f"management-monitor-remove:{message.message_id}",
        )

    def _new_personal_form(self, request_id: str) -> None:
        self._respond(
            "操作类型：新建 Codex 个人会话\n\n"
            "请回复这条消息，直接写下要做的事并发送。",
            "new_personal_thread_form",
            {"prompt_input": "direct_or_form", "schema_version": 1},
            f"management-new-personal-form:{request_id}",
        )

    def _new_project_thread_form(
        self,
        project: Mapping[str, Any] | None,
        request_id: str,
        *,
        projects: Sequence[Mapping[str, Any]] = (),
        environment_mode: str = "auto",
    ) -> None:
        if environment_mode not in {"auto", "choose", "local", "worktree"}:
            raise ValueError("未知项目运行方式")
        selected = dict(project) if project is not None else None
        project_name = _text(selected.get("name")) if selected is not None else ""
        heading = (
            f"在项目“{project_name}”中新建会话"
            if selected is not None
            else "新建 Codex 项目会话"
        )
        payload: dict[str, Any] = (
            {"project": selected, "environment_mode": environment_mode}
            if selected is not None
            else {
                "projects": [dict(item) for item in projects],
                "environment_mode": environment_mode,
            }
        )
        if selected is not None and environment_mode != "choose":
            environment_text = {
                "local": "本地",
                "worktree": "工作树",
                "auto": "自动",
            }[environment_mode]
            prompt_help = (
                f"已选项目：{project_name}\n"
                f"运行方式：{environment_text}\n"
                "请回复这条消息，直接写下要做的事并发送。"
            )
            input_mode = "direct_or_form"
        elif selected is not None:
            prompt_help = (
                f"已选项目：{project_name}\n"
                "请回复“本地”或“工作树”选定运行方式。"
            )
            input_mode = "choose_environment"
        else:
            prompt_help = "请回复项目编号（例如 A01）选定项目。"
            input_mode = "select_project"
        self._respond(
            f"操作类型：{heading}\n\n"
            f"{prompt_help}",
            "new_project_thread_form",
            {
                **payload,
                "prompt_input": input_mode,
                "schema_version": 1,
            },
            f"management-new-project-thread-form:{request_id}",
        )

    def _create_personal_thread(
        self, message: ChannelReply, context_id: str
    ) -> None:
        form = _prompt_from_reply(
            message.content,
            (),
            allow_plain=True,
            plain_error="请直接回复首轮对话正文，或保留旧模板中的“首轮对话提示词：”字段。",
        )
        if not form["首轮对话提示词"].strip():
            raise ManagementUserError("首轮对话提示词不能为空。")
        session = self._open_desktop(("create_thread",))
        try:
            self._claim_write_form(context_id)
            created = session.tools.create_thread(
                session.source_thread_id,
                form["首轮对话提示词"],
                {"type": "projectless"},
                call_tag=f"management-create-personal-{message.message_id[:12]}",
            )
        finally:
            session.tools.close()
        created_id = _text(created.get("threadId")) or _text(created.get("clientThreadId"))
        self._register_created_auto_monitor(created)
        self._respond(
            "执行结果：个人会话已创建\n"
            "提交状态：标题将由 Codex 根据首轮对话自动生成；首轮提示词已原样提交。\n"
            f"任务 ID：{created_id or '正在由 Codex 分配'}\n"
            "操作说明：稍后可发送“.查询个人会话”找到它。",
            "management_created",
            {"created": created},
            f"management-created-personal:{message.message_id}",
        )

    def _create_project_thread(
        self,
        message: ChannelReply,
        payload: Mapping[str, Any],
        context_id: str,
    ) -> None:
        raw_environment_mode = payload.get("environment_mode")
        # schema19 前已经发出的历史表单没有运行方式字段；它们继续沿用旧的
        # Git=>worktree、非Git=>local 行为，不能因升级突然失效。
        legacy_form = raw_environment_mode is None
        environment_mode = (
            "auto" if legacy_form else _text(raw_environment_mode)
        )
        if environment_mode not in {"auto", "choose", "local", "worktree"}:
            raise ManagementUserError("项目运行方式上下文已损坏，请重新选择项目。")
        required_fields = (
            ("项目名称",)
            if legacy_form
            else ("项目名称", "运行方式")
        )
        selected_project = isinstance(payload.get("project"), dict)
        plain_allowed = selected_project and environment_mode != "choose"
        has_form = _has_prompt_form_marker(message.content)
        if not has_form and not selected_project:
            # The first short reply to an unselected project prompt is only a
            # durable selector.  A body that is not an exact immutable label
            # cannot safely identify a target, even when only one project is
            # currently visible.
            projects = payload.get("projects")
            if not isinstance(projects, list):
                raise ManagementUserError("项目列表快照已损坏，请重新选择项目。")
            project_value = str(message.content or "").strip()
            matches = [
                item
                for item in projects
                if isinstance(item, dict)
                and _text(item.get("label")) == project_value
            ]
            if len(matches) != 1:
                labels = [
                    _text(item.get("label"))
                    for item in projects
                    if isinstance(item, dict) and _text(item.get("label"))
                ]
                example = labels[0] if labels else "A01"
                raise ManagementUserError(
                    f"请先回复唯一项目编号（例如 {example}）选定项目；"
                    "不能从首轮正文猜测项目。"
                )
            project = matches[0]
            if not self.store.claim_management_target_selection(
                context_id, marker="target"
            ):
                raise ManagementUserError(
                    "这次项目选择已经处理过，请回复最新的项目提示消息。"
                )
            self._new_project_thread_form(
                project,
                message.message_id,
                environment_mode=environment_mode,
            )
            return
        if not has_form and selected_project and environment_mode == "choose":
            requested_environment = str(message.content or "").strip()
            next_mode = {
                "本地": "local",
                "工作树": "worktree",
            }.get(requested_environment)
            if next_mode is None:
                raise ManagementUserError(
                    "请先回复“本地”或“工作树”选定运行方式；"
                    "选定后请回复新的项目提示消息，直接写下要做的事并发送。"
                )
            if not self.store.claim_management_target_selection(
                context_id, marker="target"
            ):
                raise ManagementUserError(
                    "这次运行方式选择已经处理过，请回复最新的项目提示消息。"
                )
            self._new_project_thread_form(
                payload["project"],
                message.message_id,
                environment_mode=next_mode,
            )
            return
        form = _prompt_from_reply(
            message.content,
            required_fields,
            allow_plain=plain_allowed,
            plain_error=(
                "请先明确填写项目名称和运行方式，再填写首轮对话提示词；"
                "系统不能从纯正文猜测目标。"
            ),
        )
        project_value = _text(form.get("项目名称"))
        project = payload.get("project")
        if not project_value and isinstance(project, dict) and plain_allowed:
            # A selected project is already durable routing state.  Plain
            # replies intentionally do not repeat its display name.
            project_value = _text(project.get("name")) or _text(project.get("label"))
        if isinstance(project, dict):
            if project_value not in {_text(project.get("name")), _text(project.get("label"))}:
                raise ManagementUserError("项目名称被改动；请保留模板中预填的项目名称。")
        else:
            projects = payload.get("projects")
            if not isinstance(projects, list):
                raise ManagementUserError("项目列表快照已损坏，请重新查询项目列表。")
            label_match = next(
                (
                    item
                    for item in projects
                    if isinstance(item, dict) and _text(item.get("label")) == project_value
                ),
                None,
            )
            name_matches = [
                item
                for item in projects
                if isinstance(item, dict) and _text(item.get("name")) == project_value
            ]
            if label_match is not None:
                project = label_match
            elif len(name_matches) == 1:
                project = name_matches[0]
            elif len(name_matches) > 1:
                raise ManagementUserError("存在重名项目，请在“项目名称”中填写列表标号，例如 A01。")
            else:
                raise ManagementUserError("项目不在这份历史列表中，请填写项目标号，例如 A01。")
        if not form["首轮对话提示词"].strip():
            raise ManagementUserError("首轮对话提示词不能为空。")
        if legacy_form:
            requested_environment = "自动"
        elif "运行方式" in form:
            requested_environment = form["运行方式"].strip()
        else:
            # The direct-reply path is available only for a selected project
            # with a fixed or automatic environment, so there is no choice to
            # infer from the prompt body.
            requested_environment = {
                "auto": "自动",
                "local": "本地",
                "worktree": "工作树",
            }.get(environment_mode, "")
        if environment_mode == "choose":
            if requested_environment not in {"本地", "工作树"}:
                raise ManagementUserError("运行方式只能填写“本地”或“工作树”。")
        elif environment_mode == "local":
            if requested_environment != "本地":
                raise ManagementUserError("运行方式被改动；本次必须保留“本地”。")
        elif environment_mode == "worktree":
            if requested_environment != "工作树":
                raise ManagementUserError("运行方式被改动；本次必须保留“工作树”。")
        else:
            if requested_environment != "自动":
                raise ManagementUserError("运行方式被改动；请保留模板中的“自动”。")
        session = self._open_desktop(("create_thread", "list_projects"))
        try:
            projects_payload = session.tools.list_projects(session.source_thread_id)
            raw_projects = projects_payload.get("projects")
            current = next(
                (item for item in raw_projects if isinstance(item, dict) and item.get("projectId") == project.get("project_id")),
                None,
            ) if isinstance(raw_projects, list) else None
            if current is None:
                raise ManagementUserError("Codex Desktop 当前找不到该项目，请重新查询项目列表。")
            is_git = current.get("isGitRepository") is True
            if environment_mode == "choose":
                environment_type = "worktree" if requested_environment == "工作树" else "local"
            elif environment_mode == "worktree":
                environment_type = "worktree"
            elif environment_mode == "local":
                environment_type = "local"
            else:
                environment_type = "worktree" if is_git else "local"
            if environment_type == "worktree" and not is_git:
                raise ManagementUserError(
                    "Codex Desktop 当前确认该项目不是 Git 仓库，不能创建工作树任务。"
                )
            environment = {"type": environment_type}
            self._claim_write_form(context_id)
            created = session.tools.create_thread(
                session.source_thread_id,
                form["首轮对话提示词"],
                {"type": "project", "projectId": project["project_id"], "environment": environment},
                call_tag=f"management-create-project-thread-{message.message_id[:12]}",
            )
        finally:
            session.tools.close()
        created_id = _text(created.get("threadId")) or _text(created.get("clientThreadId"))
        self._register_created_auto_monitor(created)
        self._respond(
            "执行结果：项目会话已创建\n"
            f"项目名称：{project.get('name')}\n"
            f"运行方式：{'工作树' if environment['type'] == 'worktree' else '本地'}\n"
            f"任务 ID：{created_id or '正在由 Codex 分配'}\n"
            "提交状态：标题将由 Codex 根据首轮对话自动生成；首轮提示词已原样提交。",
            "management_created",
            {"created": created},
            f"management-created-project-thread:{message.message_id}",
        )

    def _create_project(self, message: ChannelReply, context_id: str) -> None:
        form = _parse_form(
            message.content,
            ("项目名称", "是否需要第一段会话"),
            "首轮对话提示词",
        )
        if not form["项目名称"]:
            raise ManagementUserError("项目名称不能为空。")
        choice = form["是否需要第一段会话"]
        if choice not in {"是", "否"}:
            raise ManagementUserError("“是否需要第一段会话”只能填写“是”或“否”。")
        if choice == "是" and not form["首轮对话提示词"].strip():
            raise ManagementUserError("选择“是”时，首轮对话提示词不能为空。")
        self._claim_write_form(context_id)
        try:
            project = self.project_registry.register(form["项目名称"])
        except ProjectRegistryError as exc:
            raise ManagementUserError(str(exc)) from exc
        created: Mapping[str, Any] = {}
        recognized: dict[str, Any] | None = None
        required = ("list_projects", "create_thread") if choice == "是" else ("list_projects",)
        session = self._open_desktop(required)
        try:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                project_payload = session.tools.list_projects(session.source_thread_id)
                raw_projects = project_payload.get("projects")
                recognized = next(
                    (item for item in raw_projects if isinstance(item, dict) and item.get("projectId") == project.project_id),
                    None,
                ) if isinstance(raw_projects, list) else None
                if recognized is not None:
                    break
                time.sleep(0.25)
            if choice == "是" and recognized is not None:
                created = session.tools.create_thread(
                    session.source_thread_id,
                    form["首轮对话提示词"],
                    {
                        "type": "project",
                        "projectId": project.project_id,
                        "environment": {"type": "worktree" if bool(recognized.get("isGitRepository")) else "local"},
                    },
                    call_tag=f"management-create-project-{message.message_id[:12]}",
                )
        finally:
            session.tools.close()
        self._register_created_auto_monitor(created)
        if recognized is None:
            details = (
                "项目已安全登记，但当前运行的 Codex Desktop 尚未热加载该项目。"
                "为避免把首轮提示词错误建成个人会话，本次没有提交提示词；"
                "请重启 Codex Desktop 后从飞书重新查询项目并新建项目会话。"
            )
        elif choice == "是":
            details = "项目和首个会话均已创建，首轮提示词已原样提交。"
        else:
            details = "项目已创建；按你的选择，没有创建首个会话。"
        self._respond(
            f"执行结果：{details}\n项目名称：{project.name}\n目录：{project.root_paths[0]}",
            "management_created",
            {"project_id": project.project_id, "created": dict(created)},
            f"management-created-project:{message.message_id}",
        )


__all__ = [
    "CodexManagementController",
    "ManagementUserError",
    "PAGE_SIZE",
    "TOP_LEVEL_COMMANDS",
]
