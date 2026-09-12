from __future__ import annotations

from .card_text_hints import text_instruction_blocks

from contextlib import AbstractContextManager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import unicodedata
from typing import Any, Callable, Iterable, Mapping, Sequence

from .codex_rpc import CodexAppServer, CodexRPCError


class SkillsListError(CodexRPCError):
    """A fixed, safe diagnosis; never contains raw provider text or paths."""


REMOTE_CONTROL_ENTRY_COMMAND = "远程控制"
REMOTE_CONTROL_MENU_EVENT_KEY = "progress_wx_remote_control"
REMOTE_CONTROL_CARD_NAMESPACE = "progress_wx.remote_control"
REMOTE_CONTROL_CARD_VERSION = 1
# These are local routing controls.  They never cross the Codex App Server
# boundary; they only inspect or replace the owner/chat-scoped binding.  Keep
# the command values as the exact text users send; card copy has its own short
# labels below so presentation never silently changes the routing contract.
CURRENT_THREAD_VIEW_COMMAND = "查看当前会话"
CURRENT_THREAD_SWITCH_COMMAND = "切换当前会话"
CURRENT_THREAD_CLEAR_COMMAND = "清除当前会话"
CURRENT_THREAD_COMMANDS = frozenset(
    {
        CURRENT_THREAD_VIEW_COMMAND,
        CURRENT_THREAD_SWITCH_COMMAND,
        CURRENT_THREAD_CLEAR_COMMAND,
    }
)
CURRENT_THREAD_VIEW_LABEL = "当前会话"
CURRENT_THREAD_SWITCH_LABEL = "切换会话"
CURRENT_THREAD_CLEAR_LABEL = "清除会话绑定"

REMOTE_WRITE_ACTIONS = frozenset(
    {"goal_set", "goal_clear", "plan_start", "skill_start"}
)
REMOTE_METHOD_WRITE_CAPABILITIES = frozenset(
    {
        "goal_set",
        "goal_clear",
        "plan_start",
        "skill_start",
        "settings_update",
        "memories_set",
        "compact_start",
        "fork_start",
        "review_start",
        "feedback_upload",
    }
)

# Production may only opt into methods with real current-Desktop evidence.
# Keep this separate from the protocol-wide allowlist above: the latter guards
# spelling, while this set is the audited deployment policy.
VALIDATED_REMOTE_WRITE_CAPABILITIES = frozenset(
    {
        "goal_set",
        "goal_clear",
        "plan_start",
        "skill_start",
        "settings_update",
        "compact_start",
    }
)


class RemoteWriteUnavailable(CodexRPCError):
    """官方写方法尚未在无 writer 抢占路径上通过真实能力验证。"""

_SKILL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


@dataclass(frozen=True, slots=True)
class RemoteCommand:
    kind: str
    argument: str = ""
    skill_name: str = ""

    @property
    def is_write(self) -> bool:
        return self.kind in REMOTE_WRITE_ACTIONS

    def request_hash(self) -> str:
        canonical = json.dumps(
            {
                "kind": self.kind,
                "argument": self.argument,
                "skill_name": self.skill_name,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class GoalSnapshot:
    objective: str
    status: str = ""
    token_budget: int | None = None
    tokens_used: int | None = None
    time_used_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class SkillSnapshot:
    name: str
    path: str
    description: str = ""
    display_name: str = ""


@dataclass(frozen=True, slots=True)
class ModelSnapshot:
    model_id: str
    display_name: str
    efforts: tuple[str, ...]
    supports_personality: bool
    default_service_tier: str | None
    service_tiers: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True, slots=True)
class McpServerSnapshot:
    name: str
    auth_status: str
    runtime_status: str
    tool_count: int
    resource_count: int
    template_count: int


@dataclass(frozen=True, slots=True)
class ThreadRuntimeSnapshot:
    model: str
    effort: str
    service_tier: str | None
    status: str


def _has_disallowed_control(value: str, *, multiline: bool) -> bool:
    allowed = {"\t", "\r", "\n"} if multiline else set()
    return any(
        character not in allowed and unicodedata.category(character) == "Cc"
        for character in value
    )


def parse_remote_command(value: object) -> RemoteCommand | None:
    if type(value) is not str:
        return None
    # A slash command is a single first-line command.  Do not normalize away
    # leading whitespace or accept a later-line ``/goal`` hidden in a normal
    # prompt; multiline text belongs to the Codex prompt/form paths.
    if "\r" in value or "\n" in value:
        return None
    text = value.rstrip()
    if text == "/goal":
        return RemoteCommand("goal_get")
    # This token is only valid after an owner-bound clear confirmation card;
    # direct parsing must never reinterpret it as a new Goal objective.
    if text == "/goal clear confirm":
        return None
    if text.startswith("/goal "):
        objective = text[6:].strip()
        if objective.casefold() in {"clear", "清除", "清空"}:
            return RemoteCommand("goal_clear_request")
        if (
            not objective
            or len(objective) > 4000
            or _has_disallowed_control(objective, multiline=True)
        ):
            return None
        return RemoteCommand("goal_set", objective)
    if text.startswith("/plan "):
        task = text[6:].strip()
        if (
            not task
            or len(task) > 4000
            or _has_disallowed_control(task, multiline=True)
        ):
            return None
        return RemoteCommand("plan_start", task)
    if text == "/skills":
        return RemoteCommand("skills_list", "1")
    skills_page = re.fullmatch(r"/skills page ([1-9][0-9]*)", text)
    if skills_page:
        return RemoteCommand("skills_list", skills_page.group(1))
    if text.startswith("/skill "):
        remainder = text[7:].strip()
        name, separator, request = remainder.partition(" ")
        request = request.strip()
        if (
            not separator
            or _SKILL_NAME.fullmatch(name) is None
            or not request
            or len(request) > 4000
            or _has_disallowed_control(request, multiline=True)
        ):
            return None
        return RemoteCommand("skill_start", request, name)
    if text.startswith("$"):
        marker, separator, request = text[1:].partition(" ")
        request = request.strip()
        if (
            not separator
            or _SKILL_NAME.fullmatch(marker) is None
            or not request
            or len(request) > 4000
            or _has_disallowed_control(request, multiline=True)
        ):
            return None
        return RemoteCommand("skill_start", request, marker)
    return None


def confirmed_goal_clear_command() -> RemoteCommand:
    """仅供通过 owner-bound 确认卡验证后的控制器调用。"""

    return RemoteCommand("goal_clear")


def _strict_action(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    if value.get("namespace") != REMOTE_CONTROL_CARD_NAMESPACE:
        return None
    if type(value.get("version")) is not int or value.get("version") != REMOTE_CONTROL_CARD_VERSION:
        return None
    action = value.get("action")
    return action if type(action) is str and action else None


_FIXED_ACTION_COMMANDS = {
    "select_project": "选择项目会话",
    "select_personal": "选择个人会话",
    "goal_get": "/goal",
    # The controller card is navigation-only.  These actions open a new
    # single-form card; the existing submit actions remain separate so an old
    # form callback is never mistaken for menu navigation.
    "goal_set_open": "设置 Goal",
    "goal_set_form": "",
    "goal_clear_request": "/goal clear",
    "goal_clear_confirm": "/goal clear confirm",
    "plan_start_open": "启动 Plan",
    "plan_start_form": "",
    "skills_list": "/skills",
    "slash_catalog": "/commands",
    "skills_refresh": "/skills page 1",
    "skill_start_form": "",
    "slash_operations": "斜杠控制",
    "binding_view": CURRENT_THREAD_VIEW_COMMAND,
    "binding_switch": CURRENT_THREAD_SWITCH_COMMAND,
    "binding_clear": CURRENT_THREAD_CLEAR_COMMAND,
}


def remote_control_action(action: str) -> dict[str, object]:
    if action not in _FIXED_ACTION_COMMANDS and re.fullmatch(
        r"skills_page:[1-9][0-9]*", action
    ) is None:
        raise ValueError("未知的远程控制卡片动作")
    return {
        "namespace": REMOTE_CONTROL_CARD_NAMESPACE,
        "version": REMOTE_CONTROL_CARD_VERSION,
        "action": action,
    }


def remote_control_command(value: object, form_value: object = None) -> str | None:
    action = _strict_action(value)
    if not isinstance(value, Mapping):
        return None
    if set(value) != {"namespace", "version", "action"}:
        return None
    page_match = re.fullmatch(r"skills_page:([1-9][0-9]*)", action or "")
    if page_match:
        return (
            f"/skills page {page_match.group(1)}"
            if form_value in (None, {})
            else None
        )
    if action not in _FIXED_ACTION_COMMANDS:
        return None
    if action not in {"goal_set_form", "plan_start_form", "skill_start_form"}:
        if form_value not in (None, {}):
            return None
        return _FIXED_ACTION_COMMANDS[action]
    if not isinstance(form_value, Mapping):
        return None
    if action == "goal_set_form":
        if set(form_value) != {"goal_objective"}:
            return None
        objective = form_value.get("goal_objective")
        return (
            f"/goal {objective.strip()}"
            if type(objective) is str
            and 0 < len(objective.strip()) <= 4000
            and not _has_disallowed_control(objective, multiline=True)
            else None
        )
    if action == "plan_start_form":
        if set(form_value) != {"plan_task"}:
            return None
        task = form_value.get("plan_task")
        return (
            f"/plan {task.strip()}"
            if type(task) is str
            and 0 < len(task.strip()) <= 4000
            and not _has_disallowed_control(task, multiline=True)
            else None
        )
    if set(form_value) != {"skill_name", "skill_request"}:
        return None
    name = form_value.get("skill_name")
    request = form_value.get("skill_request")
    if (
        type(name) is not str
        or _SKILL_NAME.fullmatch(name) is None
        or type(request) is not str
        or not request.strip()
        or len(request.strip()) > 4000
        or _has_disallowed_control(request, multiline=True)
    ):
        return None
    return f"/skill {name} {request.strip()}"


def remote_control_action_fingerprint(value: object) -> str | None:
    action = _strict_action(value)
    if action not in _FIXED_ACTION_COMMANDS and re.fullmatch(
        r"skills_page:[1-9][0-9]*", action or ""
    ) is None:
        return None
    if remote_control_command(value) is None and action not in {
        "goal_set_form",
        "plan_start_form",
        "skill_start_form",
    }:
        return None
    canonical = json.dumps(
        dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def remote_control_card_action_fingerprints(
    card: Mapping[str, Any],
) -> tuple[str, ...]:
    found: list[str] = []

    def visit(node: object) -> None:
        if isinstance(node, Mapping):
            if node.get("tag") == "button":
                fingerprint = remote_control_action_fingerprint(node.get("value"))
                if fingerprint:
                    found.append(fingerprint)
            for child in node.values():
                visit(child)
        elif isinstance(node, Sequence) and not isinstance(
            node, (str, bytes, bytearray)
        ):
            for child in node:
                visit(child)

    visit(card)
    return tuple(dict.fromkeys(found))


def _button(label: str, action: str, *, primary: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": "primary" if primary else "default",
        "value": remote_control_action(action),
    }
    return result


def _form_submit(label: str, action: str) -> dict[str, Any]:
    return {
        **_button(label, action, primary=True),
        "name": action,
        "action_type": "form_submit",
    }


def _input(name: str, label: str, placeholder: str, max_length: int) -> dict[str, Any]:
    return {
        "tag": "input",
        "name": name,
        "placeholder": {"tag": "plain_text", "content": placeholder},
        "max_length": max_length,
        "label": {"tag": "plain_text", "content": label},
        "label_position": "top",
    }


def _card(title: str, subtitle: str, elements: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": title},
            "subtitle": {"tag": "plain_text", "content": subtitle},
        },
        "body": {"elements": [dict(item) for item in elements]},
    }


def _legacy_markdown(content: str) -> dict[str, Any]:
    return {
        "tag": "div",
        "text": {"tag": "lark_md", "content": content},
    }


def _form_card(
    title: str,
    subtitle: str,
    elements: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """构造可在飞书移动端真实提交 form_value 的经典交互卡。"""

    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": [
            _legacy_markdown(str(subtitle or "已选会话")),
            *[dict(item) for item in elements],
        ],
    }


def build_remote_control_entry_card() -> dict[str, Any]:
    return _card(
        "指令使用",
        "先选择要操作的会话",
        [
            {
                "tag": "markdown",
                "content": "目标只会从当前 Codex 项目或个人会话快照中选择。",
            },
            {
                "tag": "column_set",
                "columns": [
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "elements": [_button("项目会话", "select_project", primary=True)],
                    },
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "elements": [_button("个人会话", "select_personal")],
                    },
                ],
            },
            _button("更多指令", "slash_operations"),
            {
                "tag": "column_set",
                "columns": [
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "elements": [_button(CURRENT_THREAD_VIEW_LABEL, "binding_view")],
                    },
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "elements": [_button(CURRENT_THREAD_SWITCH_LABEL, "binding_switch")],
                    },
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "elements": [_button(CURRENT_THREAD_CLEAR_LABEL, "binding_clear")],
                    },
                ],
            },
            *text_instruction_blocks(['先回复本卡片，再发送：', '• 选择项目会话', '• 选择个人会话', '• 查看当前会话', '• 切换当前会话', '• 清除当前会话'], legacy=False),
        ],
    )


def build_remote_control_card(title: str) -> dict[str, Any]:
    return _card(
        "指令使用",
        str(title or "已选会话"),
        [
            {
                "tag": "markdown",
                "content": "查看当前会话可用的工具；其他指令可直接在聊天框输入。",
            },
            {
                "tag": "column_set",
                "columns": [
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "elements": [_button("Skills", "skills_list", primary=True)],
                    },
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "elements": [_button("/ 指令列表", "slash_catalog")],
                    },
                ],
            },
            {
                "tag": "markdown",
                "content": (
                    "<font color='grey'>例如：/goal、/plan 任务、/skills、"
                    "/skill 技能名 请求、$技能名 请求；完整清单请点“/ 指令列表”。</font>"
                ),
            },
        ],
    )


def build_goal_set_form_card(title: str) -> dict[str, Any]:
    """构造只包含 Goal 表单的远程控制子卡。"""

    return _form_card(
        "设置 Goal",
        str(title or "已选会话"),
        [
            _legacy_markdown(
                "为当前会话设置正式持久目标；提交前仍会校验目标会话上下文。"
            ),
            {
                "tag": "form",
                "name": "goal_set_form",
                "elements": [
                    _input("goal_objective", "设置 Goal", "输入新的长期目标", 1000),
                    _form_submit("设置 Goal", "goal_set_form"),
                ],
            },
            *text_instruction_blocks(['回复本卡片发送：', '/goal 你的长期目标'], legacy=True),
        ],
    )


def build_plan_start_form_card(title: str) -> dict[str, Any]:
    """构造只包含 Plan 表单的远程控制子卡。"""

    return _form_card(
        "启动 Plan",
        str(title or "已选会话"),
        [
            _legacy_markdown(
                "为当前空闲会话启动官方 Plan 协作模式；忙碌会话不会提交。"
            ),
            {
                "tag": "form",
                "name": "plan_start_form",
                "elements": [
                    _input("plan_task", "Plan 模式", "输入需要规划的任务", 1000),
                    _form_submit("开始规划", "plan_start_form"),
                ],
            },
            *text_instruction_blocks(['回复本卡片发送：', '/plan 需要规划的任务'], legacy=True),
        ],
    )


def build_goal_clear_confirmation_card(title: str) -> dict[str, Any]:
    return _card(
        "确认清除 Goal",
        str(title or "已选会话"),
        [
            {
                "tag": "markdown",
                "content": "这会清除 Codex 正式持久目标，但不会删除会话。",
            },
            {
                **_button("确认清除", "goal_clear_confirm", primary=True),
                "confirm": {
                    "title": {"tag": "plain_text", "content": "再次确认"},
                    "text": {
                        "tag": "plain_text",
                        "content": "确定清除这个会话的正式 Goal 吗？",
                    },
                },
            },
            {
                "tag": "markdown",
                "content": "<font color='grey'>若不清除，直接关闭卡片即可。</font>",
            },
        ],
    )


def build_skills_card(
    title: str,
    skills: Sequence[SkillSnapshot],
    *,
    page: int = 1,
    page_size: int = 10,
    refreshed_at: str = "",
    browse_only: bool = False,
) -> dict[str, Any]:
    if type(page) is not int or page < 1:
        raise ValueError("Skills 页码必须是正整数")
    if type(page_size) is not int or not 1 <= page_size <= 20:
        raise ValueError("Skills page_size 必须介于 1 和 20")
    pages = max(1, (len(skills) + page_size - 1) // page_size)
    if page > pages:
        raise ValueError("Skills 页码超出范围")
    start = (page - 1) * page_size
    visible = skills[start : start + page_size]
    options = [
        {
            "text": {
                "tag": "plain_text",
                "content": skill.display_name or skill.name,
            },
            "value": skill.name,
        }
        for skill in visible
    ]
    elements: list[Mapping[str, Any]] = [
        {
            "tag": "markdown",
            "content": (
                (f"个人/全局已启用 Skills：**{len(skills)}** 个（不含项目专属）。\n" if browse_only else f"当前工作目录可用 **{len(skills)}** 个已启用 Skills。\n")
                + f"第 {page}/{pages} 页｜本页 {len(options)} 个"
                + (f"\n刷新时间：{refreshed_at}" if refreshed_at else "")
            ),
        }
    ]
    if options and browse_only:
        for skill in visible:
            elements.append({"tag": "markdown", "content": f"**{skill.display_name or skill.name}** (`{skill.name}`)\n{skill.description[:500]}"})
    elif options:
        elements.append(
            {
                "tag": "form",
                "name": "skill_start_form",
                "elements": [
                    {
                        "tag": "select_static",
                        "name": "skill_name",
                        "placeholder": {"tag": "plain_text", "content": "选择 Skill"},
                        "options": options,
                    },
                    _input("skill_request", "具体要求", "输入希望 Skill 完成的任务", 1000),
                    _form_submit("调用 Skill", "skill_start_form"),
                ],
            }
        )
    else:
        elements.append({"tag": "markdown", "content": "此范围当前没有已启用的 Skill。"})
    navigation: list[Mapping[str, Any]] = []
    if page > 1:
        navigation.append(_button("上一页", f"skills_page:{page - 1}"))
    navigation.append(_button("刷新技能", "skills_refresh", primary=True))
    if page < pages:
        navigation.append(_button("下一页", f"skills_page:{page + 1}"))
    elements.append(
        {
            "tag": "column_set",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [button],
                }
                for button in navigation
            ],
        }
    )
    elements.extend(text_instruction_blocks(['回复本卡片，任选一种写法：', '• /skill 技能名 请求', '• $技能名 请求'], legacy=False))
    if options and not browse_only:
        legacy_elements: list[Mapping[str, Any]] = []
        for item in elements:
            if item.get("tag") == "markdown":
                legacy_elements.append(_legacy_markdown(str(item.get("content") or "")))
            elif item.get("tag") == "column_set":
                buttons = [
                    column.get("elements", [None])[0]
                    for column in item.get("columns", [])
                    if isinstance(column, Mapping)
                    and isinstance(column.get("elements"), Sequence)
                    and column.get("elements")
                    and isinstance(column.get("elements")[0], Mapping)
                ]
                legacy_elements.append({"tag": "action", "actions": buttons})
            else:
                legacy_elements.append(item)
        return _form_card(
            "Codex Skills", str(title or "已选会话"), legacy_elements
        )
    return _card("Codex Skills", str(title or "已选会话"), elements)


class PreparedRemoteSession(AbstractContextManager["PreparedRemoteSession"]):
    def __init__(
        self,
        rpc: CodexAppServer,
        thread_id: str,
        write_capabilities: frozenset[str],
        release: Callable[[], None],
    ) -> None:
        self.rpc = rpc
        self.thread_id = thread_id
        self._write_capabilities = write_capabilities
        self._release = release
        self._closed = False

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self.rpc.close()
            finally:
                self._release()

    @staticmethod
    def _result(response: Mapping[str, Any]) -> Mapping[str, Any]:
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise CodexRPCError("Codex App Server 响应缺少 result")
        return result

    def read_thread(self) -> Mapping[str, Any]:
        return self.rpc.read_thread(self.thread_id, include_turns=True)

    def _require_write(self, capability: str) -> None:
        if capability not in self._write_capabilities:
            raise RemoteWriteUnavailable(
                "该官方写方法尚未通过不争抢 Desktop writer 的真实能力验证；"
                "本次没有提交"
            )

    def is_active(self) -> bool:
        return CodexAppServer.thread_is_active(self.read_thread())

    def cwd(self) -> str:
        result = self._result(self.read_thread())
        thread = result.get("thread")
        if not isinstance(thread, Mapping):
            raise CodexRPCError("thread/read 响应缺少 thread")
        cwd = str(thread.get("cwd") or "").strip()
        if not cwd:
            raise CodexRPCError("目标会话没有可验证的工作目录")
        return cwd

    def goal(self) -> GoalSnapshot | None:
        result = self._result(
            self.rpc.request("thread/goal/get", {"threadId": self.thread_id})
        )
        raw = result.get("goal")
        if raw is None:
            return None
        if not isinstance(raw, Mapping):
            raise CodexRPCError("thread/goal/get 响应中的 goal 无效")
        objective = str(raw.get("objective") or "").strip()
        if not objective:
            return None

        def optional_int(key: str) -> int | None:
            value = raw.get(key)
            return value if type(value) is int and value >= 0 else None

        return GoalSnapshot(
            objective=objective,
            status=str(raw.get("status") or ""),
            token_budget=optional_int("tokenBudget"),
            tokens_used=optional_int("tokensUsed"),
            time_used_seconds=optional_int("timeUsedSeconds"),
        )

    def set_goal(
        self, objective: str, *, before_send: Callable[[], None] | None = None
    ) -> Mapping[str, Any]:
        self._require_write("goal_set")
        return self._result(
            self.rpc.request(
                "thread/goal/set",
                {"threadId": self.thread_id, "objective": objective},
                before_send=before_send,
            )
        )

    def clear_goal(
        self, *, before_send: Callable[[], None] | None = None
    ) -> Mapping[str, Any]:
        self._require_write("goal_clear")
        return self._result(
            self.rpc.request(
                "thread/goal/clear",
                {"threadId": self.thread_id},
                before_send=before_send,
            )
        )

    def plan_mode(self) -> Mapping[str, Any]:
        result = self._result(self.rpc.request("collaborationMode/list", {}))
        raw_modes = (
            result.get("data")
            if isinstance(result.get("data"), list)
            else result.get("modes")
            if isinstance(result.get("modes"), list)
            else result.get("collaborationModes")
        )
        if not isinstance(raw_modes, list):
            raise CodexRPCError("collaborationMode/list 响应缺少模式列表")
        matches: list[Mapping[str, Any]] = []
        for raw in raw_modes:
            if not isinstance(raw, Mapping):
                continue
            candidates = (
                raw.get("name"),
                raw.get("id"),
                raw.get("mode"),
                raw.get("displayName"),
            )
            if any(str(value or "").strip().casefold() == "plan" for value in candidates):
                matches.append(raw)
        if len(matches) != 1:
            raise CodexRPCError("无法唯一识别 Codex 官方 Plan 协作模式")
        mask = matches[0]
        mode = str(mask.get("mode") or "").strip()
        if not mode:
            raise CodexRPCError("Codex 官方 Plan 模式缺少 mode")

        # collaborationMode/list 返回的是目录 mask，不是 turn/start
        # 接受的 CollaborationMode。后者要求完整 settings.model；
        # 优先使用 thread/read 提供的当前模型，缺失时只取
        # model/list 中唯一 isDefault，绝不猜测或写死型号。
        thread_result = self._result(self.read_thread())
        model = str(mask.get("model") or thread_result.get("model") or "").strip()
        if not model:
            model_result = self._result(
                self.rpc.request("model/list", {"includeHidden": False, "limit": 100})
            )
            rows = model_result.get("data")
            defaults = [
                row
                for row in rows
                if isinstance(row, Mapping)
                and row.get("isDefault") is True
                and str(row.get("model") or row.get("id") or "").strip()
            ] if isinstance(rows, list) else []
            if len(defaults) != 1:
                raise CodexRPCError("无法唯一确定 Plan 模式的官方默认模型")
            model = str(defaults[0].get("model") or defaults[0].get("id")).strip()
        effort = mask.get("reasoning_effort")
        return {
            "mode": mode,
            "settings": {
                "model": model,
                "reasoning_effort": effort,
                "developer_instructions": None,
            },
        }

    def skills(self, *, force_reload: bool, cwd: str | None = None, global_only: bool = False) -> tuple[SkillSnapshot, ...]:
        cwd = self.cwd() if cwd is None else cwd
        result = self._result(
            self.rpc.request(
                "skills/list",
                {"cwds": [cwd], "forceReload": bool(force_reload)},
            )
        )
        data = result.get("data")
        if not isinstance(data, list):
            raise SkillsListError("官方 Skills 响应缺少列表数据。")
        matching = [
            entry
            for entry in data
            if isinstance(entry, Mapping) and os.path.normcase(os.path.normpath(str(entry.get("cwd") or ""))) == os.path.normcase(os.path.normpath(cwd))
        ]
        if len(matching) != 1:
            raise SkillsListError("官方 Skills 响应未唯一匹配请求范围。")
        if matching[0].get("errors"):
            raise SkillsListError("Codex 报告技能加载错误，列表可能不完整；请在桌面端检查技能配置。")
        raw_skills = matching[0].get("skills")
        if not isinstance(raw_skills, list):
            raise SkillsListError("官方 Skills 响应缺少技能条目。")
        snapshots: list[SkillSnapshot] = []
        seen: set[str] = set()
        for raw in raw_skills:
            if not isinstance(raw, Mapping) or raw.get("enabled") is not True:
                continue
            if global_only and raw.get("scope") not in {"user", "system", "admin"}:
                continue
            name = str(raw.get("name") or "").strip()
            path = str(raw.get("path") or raw.get("skillPath") or "").strip()
            if _SKILL_NAME.fullmatch(name) is None or not path or name in seen:
                continue
            interface = raw.get("interface")
            display_name = (
                str(interface.get("displayName") or "").strip()
                if isinstance(interface, Mapping)
                else ""
            )
            snapshots.append(
                SkillSnapshot(
                    name=name,
                    path=path,
                    description=str(raw.get("description") or "").strip(),
                    display_name=display_name,
                )
            )
            seen.add(name)
        return tuple(snapshots)

    def runtime_snapshot(self) -> ThreadRuntimeSnapshot:
        # 远控连接绝不能先 thread/resume 抢占 Desktop 的活动 writer；运行态
        # 每次都以官方 thread/read 的最新只读响应为准。
        result = self._result(self.read_thread())
        thread = result.get("thread")
        status_raw = thread.get("status") if isinstance(thread, Mapping) else ""
        status = (
            str(status_raw.get("type") or "")
            if isinstance(status_raw, Mapping)
            else str(status_raw or "")
        )
        # New thread/read exposes persisted settings on thread; older builds
        # used result-level fields. Explicit null remains unavailable.
        fields = dict(result)
        if isinstance(thread, Mapping):
            fields.update(thread)
        return ThreadRuntimeSnapshot(
            model=str(fields.get("model") or "").strip(),
            effort=str(fields.get("reasoningEffort") or "").strip(),
            service_tier=(
                str(fields.get("serviceTier")).strip()
                if fields.get("serviceTier") is not None
                else None
            ),
            status=status,
        )

    def models(self) -> tuple[ModelSnapshot, ...]:
        result = self._result(
            self.rpc.request("model/list", {"includeHidden": False, "limit": 100})
        )
        data = result.get("data")
        if not isinstance(data, list):
            raise CodexRPCError("model/list 响应缺少 data")
        snapshots: list[ModelSnapshot] = []
        seen: set[str] = set()
        for raw in data:
            if not isinstance(raw, Mapping) or raw.get("hidden") is True:
                continue
            model_id = str(raw.get("model") or raw.get("id") or "").strip()
            if not model_id or model_id in seen:
                continue
            effort_rows = raw.get("supportedReasoningEfforts")
            efforts = tuple(
                str(item.get("reasoningEffort") or "").strip()
                for item in effort_rows
                if isinstance(item, Mapping)
                and str(item.get("reasoningEffort") or "").strip()
            ) if isinstance(effort_rows, list) else ()
            tier_rows = raw.get("serviceTiers")
            tiers: list[tuple[str, str, str]] = []
            if isinstance(tier_rows, list):
                for item in tier_rows:
                    if not isinstance(item, Mapping):
                        continue
                    tier_id = str(item.get("id") or "").strip()
                    if tier_id:
                        tiers.append((
                            tier_id,
                            str(item.get("name") or tier_id).strip(),
                            str(item.get("description") or "").strip(),
                        ))
            default_tier = raw.get("defaultServiceTier")
            snapshots.append(
                ModelSnapshot(
                    model_id=model_id,
                    display_name=str(raw.get("displayName") or model_id).strip(),
                    efforts=efforts,
                    supports_personality=raw.get("supportsPersonality") is True,
                    default_service_tier=(
                        str(default_tier).strip() if default_tier is not None else None
                    ),
                    service_tiers=tuple(tiers),
                )
            )
            seen.add(model_id)
        return tuple(snapshots)

    def mcp_servers(
        self, *, cursor: str | None = None, limit: int = 20
    ) -> tuple[tuple[McpServerSnapshot, ...], str | None]:
        params: dict[str, Any] = {
            "threadId": self.thread_id,
            "detail": "toolsAndAuthOnly",
            "limit": int(limit),
        }
        if cursor:
            params["cursor"] = cursor
        result = self._result(self.rpc.request("mcpServerStatus/list", params))
        data = result.get("data")
        if not isinstance(data, list):
            raise CodexRPCError("mcpServerStatus/list 响应缺少 data")
        rows: list[McpServerSnapshot] = []
        for raw in data:
            if not isinstance(raw, Mapping):
                continue
            name = str(raw.get("name") or "").strip()
            tools = raw.get("tools")
            resources = raw.get("resources")
            templates = raw.get("resourceTemplates")
            if name:
                rows.append(
                    McpServerSnapshot(
                        name=name,
                        auth_status=str(raw.get("authStatus") or "unknown"),
                        runtime_status=str(raw.get("runtimeStatus") or "unknown"),
                        tool_count=len(tools) if isinstance(tools, Mapping) else 0,
                        resource_count=len(resources) if isinstance(resources, list) else 0,
                        template_count=len(templates) if isinstance(templates, list) else 0,
                    )
                )
        next_cursor = result.get("nextCursor")
        return tuple(rows), str(next_cursor) if isinstance(next_cursor, str) else None

    def update_setting(
        self,
        field: str,
        value: str | None,
        *,
        before_send: Callable[[], None] | None = None,
    ) -> Mapping[str, Any]:
        if field not in {"model", "effort", "personality", "serviceTier"}:
            raise ValueError("不允许修改该 Codex 设置字段")
        self._require_write("settings_update")
        params = {"threadId": self.thread_id, field: value}

        def matches(notification: Mapping[str, Any]) -> bool:
            if str(notification.get("threadId") or "") != self.thread_id:
                return False
            settings = notification.get("threadSettings")
            return (
                isinstance(settings, Mapping)
                and field in settings
                and settings[field] == value
            )

        _response, notification = self.rpc.request_with_notification(
            "thread/settings/update",
            params,
            notification_method="thread/settings/updated",
            notification_matches=matches,
            before_send=before_send,
        )
        settings = notification.params.get("threadSettings")
        if not isinstance(settings, Mapping):
            raise CodexRPCError("thread/settings/updated 缺少 threadSettings")
        return dict(settings)

    def set_memory_mode(
        self, mode: str, *, before_send: Callable[[], None] | None = None
    ) -> Mapping[str, Any]:
        if mode not in {"enabled", "disabled"}:
            raise ValueError("memory mode 只能是 enabled 或 disabled")
        self._require_write("memories_set")
        return self._result(
            self.rpc.request(
                "thread/memoryMode/set",
                {"threadId": self.thread_id, "mode": mode},
                before_send=before_send,
            )
        )

    def compact(
        self, *, before_send: Callable[[], None] | None = None
    ) -> Mapping[str, Any]:
        self._require_write("compact_start")
        return self._result(
            self.rpc.request(
                "thread/compact/start",
                {"threadId": self.thread_id},
                before_send=before_send,
            )
        )

    def fork(
        self, *, before_send: Callable[[], None] | None = None
    ) -> Mapping[str, Any]:
        self._require_write("fork_start")
        return self._result(
            self.rpc.request(
                "thread/fork",
                {"threadId": self.thread_id, "ephemeral": False},
                before_send=before_send,
            )
        )

    def review_uncommitted(
        self, *, before_send: Callable[[], None] | None = None
    ) -> Mapping[str, Any]:
        self._require_write("review_start")
        return self._result(
            self.rpc.request(
                "review/start",
                {
                    "threadId": self.thread_id,
                    "delivery": "inline",
                    "target": {"type": "uncommittedChanges"},
                },
                before_send=before_send,
            )
        )

    def upload_feedback(
        self,
        classification: str,
        reason: str,
        *,
        before_send: Callable[[], None] | None = None,
    ) -> Mapping[str, Any]:
        self._require_write("feedback_upload")
        return self._result(
            self.rpc.request(
                "feedback/upload",
                {
                    "classification": classification,
                    "reason": reason,
                    "includeLogs": False,
                    "threadId": self.thread_id,
                },
                before_send=before_send,
            )
        )

    def start_plan(
        self,
        task: str,
        mode: Mapping[str, Any],
        *,
        before_send: Callable[[], None] | None = None,
    ) -> Mapping[str, Any]:
        self._require_write("plan_start")
        return self._result(
            self.rpc.start_turn(
                self.thread_id,
                task,
                collaborationMode=dict(mode),
                before_send=before_send,
            )
        )

    def start_skill(
        self,
        skill: SkillSnapshot,
        request: str,
        *,
        before_send: Callable[[], None] | None = None,
    ) -> Mapping[str, Any]:
        self._require_write("skill_start")
        text = f"${skill.name} {request}"
        return self._result(
            self.rpc.start_turn(
                self.thread_id,
                input=[
                    {"type": "text", "text": text},
                    {"type": "skill", "name": skill.name, "path": skill.path},
                ],
                before_send=before_send,
            )
        )


class AppServerRemoteControl:
    """为每次远程操作建立独立、短生命周期的官方 App Server 连接。"""

    def __init__(
        self,
        rpc_factory: Callable[[], CodexAppServer],
        *,
        write_capabilities: Iterable[str] = (),
    ) -> None:
        self._rpc_factory = rpc_factory
        normalized = frozenset(str(item) for item in write_capabilities)
        if not normalized <= REMOTE_METHOD_WRITE_CAPABILITIES:
            raise ValueError("包含未知的远程写能力")
        self._write_capabilities = normalized
        self._operation_lock = threading.Lock()

    def global_skills(self) -> tuple[SkillSnapshot, ...]:
        """Read user/system/admin skills without selecting or loading a thread."""
        rpc = self._rpc_factory()
        try:
            rpc.initialize()
            reader = PreparedRemoteSession(rpc, "", frozenset(), lambda: None)
            return reader.skills(force_reload=True, cwd=str(Path.home()), global_only=True)
        finally:
            rpc.close()

    def prepare(self, thread_id: str) -> PreparedRemoteSession:
        if type(thread_id) is not str or not thread_id.strip():
            raise ValueError("thread_id 不能为空")
        if not self._operation_lock.acquire(timeout=10.0):
            raise CodexRPCError("另一项 Codex 远程操作正在建立连接")
        rpc = self._rpc_factory()
        try:
            rpc.initialize()
            opened = rpc.read_thread(thread_id, include_turns=True)
            result = opened.get("result")
            thread = result.get("thread") if isinstance(result, Mapping) else None
            if (
                not isinstance(thread, Mapping)
                or str(thread.get("id") or "").strip() != thread_id.strip()
            ):
                raise CodexRPCError("thread/read 未返回目标会话身份")
        except BaseException:
            rpc.close()
            self._operation_lock.release()
            raise
        return PreparedRemoteSession(
            rpc,
            thread_id,
            self._write_capabilities,
            self._operation_lock.release,
        )


__all__ = [
    "AppServerRemoteControl",
    "GoalSnapshot",
    "McpServerSnapshot",
    "ModelSnapshot",
    "PreparedRemoteSession",
    "REMOTE_CONTROL_CARD_NAMESPACE",
    "REMOTE_CONTROL_CARD_VERSION",
    "REMOTE_CONTROL_ENTRY_COMMAND",
    "REMOTE_CONTROL_MENU_EVENT_KEY",
    "CURRENT_THREAD_VIEW_COMMAND",
    "CURRENT_THREAD_SWITCH_COMMAND",
    "CURRENT_THREAD_CLEAR_COMMAND",
    "CURRENT_THREAD_COMMANDS",
    "CURRENT_THREAD_VIEW_LABEL",
    "CURRENT_THREAD_SWITCH_LABEL",
    "CURRENT_THREAD_CLEAR_LABEL",
    "REMOTE_METHOD_WRITE_CAPABILITIES",
    "VALIDATED_REMOTE_WRITE_CAPABILITIES",
    "REMOTE_WRITE_ACTIONS",
    "RemoteCommand",
    "RemoteWriteUnavailable",
    "SkillSnapshot",
    "ThreadRuntimeSnapshot",
    "build_goal_set_form_card",
    "build_goal_clear_confirmation_card",
    "build_plan_start_form_card",
    "build_remote_control_card",
    "build_remote_control_entry_card",
    "build_skills_card",
    "confirmed_goal_clear_command",
    "parse_remote_command",
    "remote_control_action",
    "remote_control_action_fingerprint",
    "remote_control_card_action_fingerprints",
    "remote_control_command",
]
