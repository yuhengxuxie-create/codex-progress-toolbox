"""通过飞书精确命令管理 Codex Desktop 项目和会话。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
import math
import ntpath
from pathlib import Path
import re
import time
from typing import Any, Callable, Mapping, Sequence

from .channel import ChannelReply, codex_prompt_for_reply
from .codex_account import (
    CodexAccountError,
    CodexAccountReader,
    format_rate_limits,
)
from .codex_app_tools import (
    DesktopAppToolsClient,
    DesktopAppToolsError,
    DesktopAppToolsUnavailable,
    VerifiedDesktopAppTools,
)
from .codex_projects import CodexProjectRegistry, LocalProject, ProjectRegistryError
from .codex_store import CodexStore, ThreadRecord, read_generated_image_bytes
from .models import GeneratedImageArtifact
from .state import StateStore
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
LOGGER = logging.getLogger("progress_wx.codex_management")
TOP_LEVEL_COMMANDS = frozenset({
    "查询项目列表",
    "查询个人会话",
    "新建项目",
    "新建个人会话",
    "查询监测列表",
    "添加监测任务",
    "移除监测任务",
    "使用说明",
    "文字版使用说明",
    "查询剩余额度",
})
_PAGE = re.compile(r"^第([1-9][0-9]*)页$")
_EXPAND_PROJECT = re.compile(r"^展开(A[0-9]{2,})$")
_SELECT_PROJECT_THREAD = re.compile(r"^选定(a[0-9]{2,})$")
_SELECT_PERSONAL_THREAD = re.compile(r"^选定(p[0-9]{2,})$")
_ADD_PROJECT_THREAD_MONITOR = re.compile(r"^添加监测(a[0-9]{2,})$")
_ADD_PERSONAL_THREAD_MONITOR = re.compile(r"^添加监测(p[0-9]{2,})$")
_REMOVE_MONITOR = re.compile(r"^移除(m[0-9]{2,})$")


class ManagementUserError(ValueError):
    """用户输入与所回复消息的上下文不匹配，可修正后重试。"""


@dataclass(frozen=True, slots=True)
class _DesktopSession:
    tools: VerifiedDesktopAppTools
    source_thread_id: str
    listing: Mapping[str, Any]


def _text(value: Any) -> str:
    return str(value or "").strip()


def _timestamp(value: Any) -> int:
    try:
        number = int(float(value or 0))
    except (TypeError, ValueError):
        return 0
    return number // 1000 if number > 10_000_000_000 else number


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
}


def _is_form_preamble_line(line: str) -> bool:
    cleaned = _clean_line(line)
    if cleaned in _FORM_PREAMBLE_LINES:
        return True
    if cleaned.startswith(("操作类型：", "填写说明：")):
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


def _compact(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


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
        send_image: Callable[[bytes, str], tuple[str, ...]] | None = None,
        send_file: Callable[[bytes, str, str], tuple[str, ...]] | None = None,
        account_reader: CodexAccountReader | None = None,
        context_ttl_days: int | None = None,
    ) -> None:
        self.store = store
        self.codex_store = codex_store
        self.desktop_client = desktop_client
        self.project_registry = project_registry
        self.source_thread_ids = tuple(dict.fromkeys(_text(item) for item in source_thread_ids if _text(item)))
        self.send_text = send_text
        self.send_image = send_image
        self.send_file = send_file
        self.account_reader = account_reader
        self.context_ttl_days = (
            None if context_ttl_days is None else int(context_ttl_days)
        )
        if not self.source_thread_ids:
            raise ValueError("source_thread_ids 不能为空")

    def accepts(self, message: ChannelReply) -> bool:
        if not message.reply_to_message_id:
            return message.content in TOP_LEVEL_COMMANDS
        return self.store.management_context_for_message(message.reply_to_message_id) is not None

    def handle(self, message: ChannelReply) -> None:
        if not self.store.reserve_management_inbound(
            message.message_id, message.sender_id, message.content
        ):
            return
        try:
            if message.reply_to_message_id:
                context = self.store.management_context_for_message(message.reply_to_message_id)
                if context is None:
                    raise ManagementUserError("这条机器人消息的操作上下文已过期，请重新查询。")
                self._handle_context(message, context[0], context[1])
            else:
                self._handle_top(message)
            self.store.complete_management_inbound(message.message_id)
        except BaseException:
            raise

    def send_user_error(self, message: ChannelReply, details: str) -> None:
        context = self.store.management_context_for_message(message.reply_to_message_id)
        kind, payload = context if context is not None else ("management_error", {})
        self._respond(
            f"没有执行。{details}\n\n请修改后继续回复原消息，或重新发送入口命令。",
            kind,
            payload,
            f"management-error:{message.message_id}",
        )

    def send_system_error(self, message: ChannelReply) -> None:
        self._respond(
            "Codex Desktop 当前无法完成这项操作；没有创建会话，也没有发送提示词。"
            "请确认 Codex 桌面端正在运行后重新发送。",
            "management_error",
            {},
            f"management-system-error:{message.message_id}",
        )

    def _respond(
        self,
        text: str,
        kind: str,
        payload: Mapping[str, Any],
        key: str,
    ) -> str:
        context_id = self.store.create_management_context(
            kind, payload, ttl_days=self.context_ttl_days
        )
        message_ids = self.send_text(text, key)
        if not message_ids:
            raise DesktopAppToolsError("飞书渠道未返回可绑定的 message_id")
        self.store.bind_management_messages(context_id, message_ids)
        return context_id

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
            if not _text(item.get("title")):
                item["title"] = record.title or record.preview
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

    def _handle_top(self, message: ChannelReply) -> None:
        command = message.content
        if command == "查询剩余额度":
            if self.account_reader is None:
                raise ManagementUserError("当前版本尚未启用额度查询。")
            try:
                snapshot = self.account_reader.read()
            except CodexAccountError:
                self._respond(
                    "当前未能从 Codex 官方服务读取额度。没有使用缓存数据，也没有估算；"
                    "请稍后重新发送“查询剩余额度”。",
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
        self, projects: Sequence[Mapping[str, Any]], page: int, request_id: str
    ) -> None:
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
        self._respond(
            "\n".join(lines),
            "project_list",
            {"projects": list(projects), "page": page},
            f"management-project-list:{request_id}:{page}",
        )

    def _send_personal_page(
        self, threads: Sequence[Mapping[str, Any]], page: int, request_id: str
    ) -> None:
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
        self._respond(
            "\n".join(lines),
            "personal_list",
            {"threads": snapshot, "page": page},
            f"management-personal-list:{request_id}:{page}",
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
            "- 添加任务：发送入口命令“添加监测任务”",
        ))
        self._respond(
            "\n".join(lines),
            "monitor_list",
            {"items": snapshot, "page": page},
            f"management-monitor-list:{request_id}:{page}",
        )

    def _handle_context(
        self, message: ChannelReply, kind: str, payload: Mapping[str, Any]
    ) -> None:
        content = message.content.strip()
        if kind == "project_list":
            self._handle_project_list(message, payload, content)
        elif kind == "project_threads":
            self._handle_project_threads(message, payload, content)
        elif kind == "personal_list":
            self._handle_personal_list(message, payload, content)
        elif kind == "monitor_list":
            self._handle_monitor_list(message, payload, content)
        elif kind == "thread_overview":
            self._continue_thread(message, payload)
        elif kind == "new_project_form":
            self._create_project(message)
        elif kind == "new_project_thread_form":
            self._create_project_thread(message, payload)
        elif kind == "new_personal_thread_form":
            self._create_personal_thread(message)
        elif kind == "monitor_add_form":
            self._apply_monitor_form(message, "add")
        elif kind == "monitor_remove_form":
            self._apply_monitor_form(message, "remove")
        else:
            raise ManagementUserError("这条消息不支持继续操作，请重新发送入口命令。")

    def _handle_project_list(
        self, message: ChannelReply, payload: Mapping[str, Any], content: str
    ) -> None:
        projects = payload.get("projects")
        if not isinstance(projects, list):
            raise ManagementUserError("项目快照已损坏，请重新查询。")
        page_match = _PAGE.fullmatch(content)
        if page_match:
            self._send_project_page(projects, int(page_match.group(1)), message.message_id)
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
        session = self._open_desktop(())
        try:
            catalog, _registry = self._catalog(session.listing)
            threads = [item for item in catalog if item.get("projectId") == project.get("project_id")]
        finally:
            session.tools.close()
        self._send_project_threads(project, threads, 1, message.message_id)

    def _send_project_threads(
        self,
        project: Mapping[str, Any],
        threads: Sequence[Mapping[str, Any]],
        page: int,
        request_id: str,
    ) -> None:
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
        self._respond(
            "\n".join(lines),
            "project_threads",
            {"project": dict(project), "threads": snapshot, "page": page},
            f"management-project-threads:{request_id}:{page}",
        )

    def _handle_project_threads(
        self, message: ChannelReply, payload: Mapping[str, Any], content: str
    ) -> None:
        project = payload.get("project")
        threads = payload.get("threads")
        if not isinstance(project, dict) or not isinstance(threads, list):
            raise ManagementUserError("项目会话快照已损坏，请重新查询。")
        page_match = _PAGE.fullmatch(content)
        if page_match:
            self._send_project_threads(project, threads, int(page_match.group(1)), message.message_id)
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
        self._send_overview(thread, _text(project.get("name")), message.message_id)

    def _handle_personal_list(
        self, message: ChannelReply, payload: Mapping[str, Any], content: str
    ) -> None:
        threads = payload.get("threads")
        if not isinstance(threads, list):
            raise ManagementUserError("个人会话快照已损坏，请重新查询。")
        page_match = _PAGE.fullmatch(content)
        if page_match:
            # 已有快照带 label，重新分页时保留稳定标号。
            start, end, pages = _page_bounds(len(threads), int(page_match.group(1)))
            page = int(page_match.group(1))
            lines = [
                "列表类型：Codex 个人会话",
                f"页码：第 {page}/{pages} 页",
                f"总数：{len(threads)} 个",
                "",
                "会话列表：",
            ]
            lines.extend(
                f"{item['label']}｜{_compact(_text(item.get('title')) or '未命名会话', LIST_TITLE_MAX_CHARS)}"
                for item in threads[start:end] if isinstance(item, dict)
            )
            lines.extend((
                "",
                "操作说明：",
                "- 选择会话：回复“选定p01”",
                "- 永久监测：回复“添加监测p01”",
                "- 翻页：回复“第2页”",
                "- 也可回复“新建个人会话”",
            ))
            self._respond("\n".join(lines), "personal_list", {"threads": threads, "page": page}, f"management-personal-page:{message.message_id}:{page}")
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
        self._send_overview(thread, "个人会话", message.message_id)

    def _send_overview(self, thread: Mapping[str, Any], group: str, request_id: str) -> None:
        thread_id = _text(thread.get("id"))
        latest_turn = self.codex_store.latest_turn(thread_id)
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
        local_final = latest_turn.final_message if latest_turn is not None else ""
        latest = _compact(_latest_final(detail) or local_final, 500)
        updated = _timestamp(thread.get("updatedAt"))
        updated_text = datetime.fromtimestamp(updated).strftime("%Y-%m-%d %H:%M") if updated else "未知"
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
        if self.send_image is not None and latest_turn is not None:
            for artifact in latest_turn.generated_images:
                try:
                    data = read_generated_image_bytes(artifact)
                except ValueError:
                    skipped_images += 1
                    LOGGER.warning(
                        "跳过已变化或不可读的会话概览图片 thread=%s turn=%s item=%s",
                        latest_turn.thread_id,
                        latest_turn.turn_id,
                        artifact.item_id,
                    )
                else:
                    prepared_images.append((artifact, data))
        lines = [
            f"会话名称：{_compact(_text(thread.get('title')) or '新建会话', OVERVIEW_TITLE_MAX_CHARS)}",
            f"归属：{group}",
            f"状态：{_text(thread.get('status')) or detail_status or '未知'}",
            f"最近更新：{updated_text}",
            f"监测状态：{monitor_status}",
            "",
            f"整体概览：{overall or 'Codex 暂未提供概览。'}",
            "",
            f"最后一轮结果：{latest or '最近轮次暂无可展示的最终答复。'}",
        ]
        if prepared_images:
            lines.extend(("", f"最近生成图片：{len(prepared_images)} 张将在下方直接展示。"))
        if skipped_images:
            lines.extend(("", f"最近生成图片：{skipped_images} 张原图已失效，未发送。"))
        lines.extend((
            "",
            "操作说明：回复文字可续聊；回复“添加监测”或“移除监测”可管理本任务。",
        ))
        stored = dict(thread)
        stored.pop("summary", None)
        context_id = self._respond(
            "\n".join(lines),
            "thread_overview",
            {"thread": stored, "group": group},
            f"management-overview:{request_id}:{thread.get('id')}",
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
                "课堂图片暂时不可读；可发送“文字版使用说明”查看文字内容。",
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

    def _continue_thread(self, message: ChannelReply, payload: Mapping[str, Any]) -> None:
        thread = payload.get("thread")
        if not isinstance(thread, dict) or not _text(thread.get("id")):
            raise ManagementUserError("会话上下文已损坏，请重新查询并选定。")
        if message.attachment_error:
            raise ManagementUserError(message.attachment_error)
        command = message.content.strip()
        if command == "添加监测":
            self._add_manual_monitor(thread, message, "thread_overview", payload)
            return
        if command == "移除监测":
            self.store.remove_monitor(_text(thread.get("id")))
            self._respond(
                "执行结果：已移除监测\n"
                f"会话名称：{_compact(_text(thread.get('title')) or '新建会话', OVERVIEW_TITLE_MAX_CHARS)}\n"
                "提交状态：已写入抑制，除非你手动重新添加，否则不会被自动发现恢复。",
                "thread_overview",
                payload,
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
            payload,
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

    def _apply_monitor_form(self, message: ChannelReply, action: str) -> None:
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
            self.store.add_manual_monitor(thread_id, last_activity_at=activity)
            result = "已添加永久手动监测"
        else:
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
            "填写说明：复制整段，然后只在冒号后面填上想要的内容就可以了哦。\n"
            "首轮对话提示词：",
            "new_personal_thread_form",
            {},
            f"management-new-personal-form:{request_id}",
        )

    def _new_project_thread_form(
        self,
        project: Mapping[str, Any] | None,
        request_id: str,
        *,
        projects: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        selected = dict(project) if project is not None else None
        project_name = _text(selected.get("name")) if selected is not None else ""
        heading = (
            f"在项目“{project_name}”中新建会话"
            if selected is not None
            else "新建 Codex 项目会话"
        )
        payload: dict[str, Any] = (
            {"project": selected}
            if selected is not None
            else {"projects": [dict(item) for item in projects]}
        )
        self._respond(
            f"操作类型：{heading}\n\n"
            "填写说明：复制整段，然后只在冒号后面填上想要的内容就可以了哦。\n"
            f"项目名称：{project_name}\n"
            "首轮对话提示词：",
            "new_project_thread_form",
            payload,
            f"management-new-project-thread-form:{request_id}",
        )

    def _create_personal_thread(self, message: ChannelReply) -> None:
        form = _parse_form(message.content, (), "首轮对话提示词")
        if not form["首轮对话提示词"].strip():
            raise ManagementUserError("首轮对话提示词不能为空。")
        session = self._open_desktop(("create_thread",))
        try:
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
            "操作说明：稍后可通过“查询个人会话”找到它。",
            "management_created",
            {"created": created},
            f"management-created-personal:{message.message_id}",
        )

    def _create_project_thread(
        self, message: ChannelReply, payload: Mapping[str, Any]
    ) -> None:
        form = _parse_form(message.content, ("项目名称",), "首轮对话提示词")
        project_value = form["项目名称"]
        project = payload.get("project")
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
            environment = {"type": "worktree" if bool(current.get("isGitRepository")) else "local"}
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
            f"任务 ID：{created_id or '正在由 Codex 分配'}\n"
            "提交状态：标题将由 Codex 根据首轮对话自动生成；首轮提示词已原样提交。",
            "management_created",
            {"created": created},
            f"management-created-project-thread:{message.message_id}",
        )

    def _create_project(self, message: ChannelReply) -> None:
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
