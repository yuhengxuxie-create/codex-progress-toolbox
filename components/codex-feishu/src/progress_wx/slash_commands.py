"""Codex 官方斜杠命令的飞书展示、严格解析和卡片构造。

这个模块不执行任何 RPC。它只维护当前官方命令清单、精确远程能力分级，
并把安全的卡片动作转换成受控文字命令。目标 thread/cwd/path 永不进入卡片。
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Mapping, Sequence


SLASH_COMMAND_ENTRY_COMMAND = "斜杠指令"
# These are user-facing aliases for the same catalog entry.  Keep the
# canonical Chinese label for existing callers, but accept every alias at the
# parser boundary so a copied command can never fall through to a normal
# prompt.
SLASH_COMMAND_ENTRY_ALIASES: tuple[str, ...] = (
    "查看指令列表",
    SLASH_COMMAND_ENTRY_COMMAND,
    "/commands",
)
SLASH_COMMAND_CARD_NAMESPACE = "progress_wx.slash_commands"
SLASH_COMMAND_CARD_VERSION = 1
SLASH_PAGE_SIZE = 6


_UNVERIFIED_WRITE_REASON = (
    "当前官方写方法尚未通过不争抢 Desktop writer 的真实能力验证；"
    "本次不会提交，也不会改发普通提示词"
)
_CONTEXT_ONLY_REASON = "仅在对应审批消息上下文中可用，不能作为独立文字命令执行"


@dataclass(frozen=True, slots=True, init=False)
class SlashCapability:
    command: str
    label: str
    description: str
    mode: str
    usage: str
    disabled_reason: str = ""
    executable: bool | None = None
    write_unavailable_reason: str = ""

    def __init__(
        self,
        command: str,
        label: str,
        description: str,
        mode: str,
        usage: str | None = None,
        disabled_reason: str = "",
        executable: bool | None = None,
        write_unavailable_reason: str = "",
        *,
        syntax: str | None = None,
    ) -> None:
        """Build a catalog row.

        ``syntax=`` was the name used by the first local implementation.  It
        remains accepted for callers constructing synthetic rows, while the
        public field is now the clearer ``usage``.
        """

        if usage is None:
            usage = syntax
        elif syntax is not None and usage != syntax:
            raise ValueError("slash command usage 与 syntax 不一致")
        object.__setattr__(self, "command", command)
        object.__setattr__(self, "label", label)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "usage", usage)
        object.__setattr__(self, "disabled_reason", disabled_reason)
        object.__setattr__(self, "executable", executable)
        object.__setattr__(self, "write_unavailable_reason", write_unavailable_reason)
        self.__post_init__()

    def __post_init__(self) -> None:
        if (
            type(self.command) is not str
            or not self.command.startswith("/")
            or " " in self.command
            or "\t" in self.command
            or "\r" in self.command
            or "\n" in self.command
        ):
            raise ValueError("slash command 必须是单一 /name")
        if type(self.label) is not str or not self.label.strip():
            raise ValueError("slash command label 必须是非空文本")
        if type(self.description) is not str or not self.description.strip():
            raise ValueError("slash command description 必须是非空文本")
        if type(self.usage) is not str or not self.usage.strip():
            raise ValueError("slash command usage 必须是非空文本")
        if self.mode not in {"remote", "context", "existing", "disabled"}:
            raise ValueError("slash capability mode 无效")
        if type(self.disabled_reason) is not str:
            raise ValueError("slash capability disabled_reason 必须是文本")
        if type(self.executable) not in {bool, type(None)}:
            raise ValueError("slash capability executable 必须是布尔值")
        if self.executable is None:
            object.__setattr__(
                self,
                "executable",
                self.mode in {"remote", "existing"},
            )
        if type(self.write_unavailable_reason) is not str:
            raise ValueError("slash capability write_unavailable_reason 必须是文本")
        if self.mode in {"disabled", "context"} and self.executable:
            raise ValueError("disabled/context slash capability 不能标记为可执行")
        if not self.executable and not self.disabled_reason.strip():
            raise ValueError("不可远程执行的命令必须说明原因")

    @property
    def syntax(self) -> str:
        """Backward-compatible name used by older card callers."""

        return self.usage

    @property
    def unavailable_reason(self) -> str:
        """Reason for an unavailable command or an unavailable write form."""

        return self.disabled_reason or self.write_unavailable_reason

    @property
    def is_executable(self) -> bool:
        return bool(self.executable)


OFFICIAL_SLASH_CAPABILITIES: tuple[SlashCapability, ...] = (
    SlashCapability(
        "/approve", "批准重试", "批准真实 Guardian 拒绝事件后的重试。",
        "context", "/approve", _CONTEXT_ONLY_REASON, False,
    ),
    SlashCapability(
        "/cloud", "云端运行", "把任务交给 ChatGPT 云端环境。", "disabled",
        "/cloud", "当前本地 App Server 没有与桌面端 /cloud 等价的创建端点", False,
    ),
    SlashCapability(
        "/cloud-environment", "云端环境", "选择云端执行环境。", "disabled",
        "/cloud-environment", "当前只读环境接口不能安全选择并启动云任务", False,
    ),
    SlashCapability(
        "/compact", "压缩上下文", "调用 thread/compact/start 压缩当前会话。",
        "remote", "/compact", "", True,
    ),
    SlashCapability(
        "/fast", "快速模式", "从 model/list 实时识别 Fast tier 并切换。",
        "remote", "/fast", _UNVERIFIED_WRITE_REASON, False,
    ),
    SlashCapability(
        "/feedback", "提交反馈", "向 OpenAI 提交产品反馈。", "disabled", "/feedback",
        "当前官方协议未提供 classification 可选值目录，禁止猜测后上传", False,
    ),
    SlashCapability(
        "/fork", "复制会话", "通过 thread/fork 创建一个非临时副本。", "remote",
        "/fork", "持久副本与分页历史的完整生命周期尚未通过真实验证", False,
    ),
    SlashCapability(
        "/goal", "长期目标", "查看、设置或清除 Codex 正式 Goal。", "remote",
        "/goal [目标|clear]", "", True,
    ),
    SlashCapability(
        "/ide-context", "IDE 上下文", "切换桌面客户端 IDE 上下文注入。", "disabled",
        "/ide-context", "这是桌面客户端界面状态，当前协议没有等价线程方法", False,
    ),
    SlashCapability(
        "/init", "生成 AGENTS.md", "使用 Codex 当前内置模板生成说明文件。", "disabled",
        "/init", "当前协议不能读取版本同步的官方 /init 模板，禁止手写近似版本", False,
    ),
    SlashCapability(
        "/local", "本地项目", "进入现有本地项目新会话流程。", "existing", "/local", "", True,
    ),
    SlashCapability(
        "/mcp", "MCP 状态", "通过 mcpServerStatus/list 查看实时连接状态。", "remote",
        "/mcp", "", True,
    ),
    SlashCapability(
        "/memories", "记忆模式", "查看并设置当前会话的记忆模式。", "remote",
        "/memories [enabled|disabled]", "", True, _UNVERIFIED_WRITE_REASON,
    ),
    SlashCapability(
        "/model", "模型", "通过 model/list 查看并选择后续轮次模型。", "remote",
        "/model [模型 ID]", "", True,
    ),
    SlashCapability(
        "/pet", "桌面宠物", "控制 Codex 桌面端宠物。", "disabled", "/pet",
        "这是桌面客户端本地界面能力，App Server 没有等价端点", False,
    ),
    SlashCapability(
        "/personality", "个性", "查看并选择官方 personality 枚举。", "remote",
        "/personality [个性]", "", True,
    ),
    SlashCapability(
        "/plan", "规划模式", "用官方 collaboration mode 启动新一轮规划。", "remote",
        "/plan 任务", "", True,
    ),
    SlashCapability(
        "/project", "选择项目", "进入现有项目会话选择流程。", "existing", "/project", "", True,
    ),
    SlashCapability(
        "/reasoning", "推理强度", "查看并选择当前模型支持的 effort。", "remote",
        "/reasoning [effort]", "", True,
    ),
    SlashCapability(
        "/review", "代码审查", "通过 review/start 审查未提交改动。", "remote",
        "/review", "reviewThreadId 与真实活动 turn 的跟踪尚未完整验证", False,
    ),
    SlashCapability(
        "/skill", "调用 Skill", "按名称调用当前会话实时启用的 Skill。", "remote",
        "/skill 技能名 具体要求", "", True,
    ),
    SlashCapability(
        "/skills", "Skills 列表", "实时刷新并查看当前会话可用的 Skill。", "remote",
        "/skills [page 页码]", "", True,
    ),
    SlashCapability(
        "/side", "临时旁聊", "创建不进入历史的临时旁聊。", "disabled", "/side",
        "ephemeral fork 依赖持续 App Server 生命周期，当前短连接桥不能保证等价存活", False,
    ),
    SlashCapability(
        "/status", "会话状态", "读取线程状态、模型、推理强度和额度。", "remote",
        "/status", "", True,
    ),
    SlashCapability(
        "/task", "个人任务", "进入现有新建个人会话流程。", "existing", "/task", "", True,
    ),
    SlashCapability(
        "/worktree", "新建工作树", "从 Git 项目创建 Codex worktree 任务。", "existing",
        "/worktree", "", True,
    ),
)

_BY_COMMAND = {item.command: item for item in OFFICIAL_SLASH_CAPABILITIES}
if len(_BY_COMMAND) != len(OFFICIAL_SLASH_CAPABILITIES):
    raise RuntimeError("官方 slash 命令清单存在重复")

# Public aliases make the catalog discoverable without requiring callers to
# know the historical constant name.
OFFICIAL_SLASH_COMMANDS = OFFICIAL_SLASH_CAPABILITIES
SLASH_COMMAND_CATALOG = OFFICIAL_SLASH_CAPABILITIES
SLASH_COMMAND_ALIASES = SLASH_COMMAND_ENTRY_ALIASES

_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_EFFORT = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
_PERSONALITIES = frozenset({"none", "friendly", "pragmatic"})


@dataclass(frozen=True, slots=True)
class SlashCommand:
    kind: str
    argument: str = ""
    secondary: str = ""

    @property
    def is_write(self) -> bool:
        return self.kind in {
            "compact_start", "fast_toggle", "feedback_upload", "fork_start",
            "memories_set", "model_set", "personality_set", "reasoning_set",
            "review_start",
        }

    def request_hash(self) -> str:
        payload = json.dumps(
            {"kind": self.kind, "argument": self.argument, "secondary": self.secondary},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_slash_command(value: object) -> SlashCommand | None:
    if type(value) is not str:
        return None
    # A slash command occupies the whole incoming line.  Do not strip a
    # leading/trailing newline and accidentally accept a command embedded in
    # a multi-line prompt.
    if "\r" in value or "\n" in value:
        return None
    if value.startswith("/"):
        text = value.rstrip()
        if text != text.lstrip():
            return None
    elif value.lstrip().startswith("/"):
        return None
    else:
        text = value.strip()
    if text in SLASH_COMMAND_ENTRY_ALIASES:
        return SlashCommand("catalog")
    page_match = re.fullmatch(
        r"(?:查看指令列表|斜杠指令|/commands) page ([1-9][0-9]*)",
        text,
    )
    if page_match:
        page = int(page_match.group(1))
        page_count = (
            len(OFFICIAL_SLASH_CAPABILITIES) + SLASH_PAGE_SIZE - 1
        ) // SLASH_PAGE_SIZE
        return (
            SlashCommand("catalog_page", page_match.group(1))
            if page <= page_count
            else None
        )
    if text == "/mcp":
        return SlashCommand("mcp_list")
    if text == "/status":
        return SlashCommand("status_get")
    if text == "/plan":
        return SlashCommand("plan_form")
    if text == "/compact":
        return SlashCommand("compact_request")
    if text == "/compact confirm":
        return SlashCommand("compact_start")
    if text == "/fork":
        return SlashCommand("fork_request")
    if text == "/fork confirm":
        return SlashCommand("fork_start")
    if text == "/review":
        return SlashCommand("review_request")
    if text == "/review confirm":
        return SlashCommand("review_start")
    if text == "/model":
        return SlashCommand("model_list")
    if text.startswith("/model "):
        model = text[7:].strip()
        return SlashCommand("model_set", model) if _MODEL_ID.fullmatch(model) else None
    if text == "/personality":
        return SlashCommand("personality_list")
    if text.startswith("/personality "):
        personality = text[13:].strip().casefold()
        return SlashCommand("personality_set", personality) if personality in _PERSONALITIES else None
    if text == "/reasoning":
        return SlashCommand("reasoning_list")
    if text.startswith("/reasoning "):
        effort = text[11:].strip().casefold()
        return SlashCommand("reasoning_set", effort) if _EFFORT.fullmatch(effort) else None
    if text in {"/fast", "/fast confirm"}:
        # Keep the parser and the catalog's capability gate in lockstep.  A
        # disabled capability may remain visible in the directory, but it
        # must never reach either the request or confirmation write path.
        capability = _BY_COMMAND["/fast"]
        if not capability.is_executable:
            return SlashCommand(
                "unavailable",
                capability.command,
                capability.unavailable_reason,
            )
        return SlashCommand(
            "fast_request" if text == "/fast" else "fast_toggle"
        )
    if text == "/memories":
        return SlashCommand("memories_list")
    memory_match = re.fullmatch(r"/memories\s+(enabled|disabled)(?:\s+confirm)?", text, re.I)
    if memory_match:
        mode = memory_match.group(1).casefold()
        return SlashCommand(
            "memories_set" if text.casefold().endswith(" confirm") else "memories_request",
            mode,
        )
    # The command token is only recognized at the beginning of the complete
    # message.  In particular, do not turn ``please /status`` or an unknown
    # ``/rpc`` token into a normal prompt or a guessed RPC call.  Disabled and
    # context-only commands have no parameter grammar, so malformed variants
    # are rejected instead of being presented as executable.
    command_token, separator, remainder = text.partition(" ")
    capability = _BY_COMMAND.get(command_token)
    if capability is not None and capability.mode in {"disabled", "context"}:
        if separator and remainder.strip():
            return None
        return SlashCommand(
            "unavailable",
            capability.command,
            capability.unavailable_reason,
        )
    if text == "/project":
        return SlashCommand("existing_project")
    if text == "/task":
        return SlashCommand("existing_task")
    if text == "/local":
        return SlashCommand("existing_local")
    if text == "/worktree":
        return SlashCommand("existing_worktree")
    return None


_CARD_COMMANDS = {
    "catalog": SLASH_COMMAND_ENTRY_COMMAND,
    "catalog_1": "/commands page 1",
    "catalog_2": "/commands page 2",
    "catalog_3": "/commands page 3",
    "catalog_4": "/commands page 4",
    "catalog_5": "/commands page 5",
    "mcp": "/mcp",
    "status": "/status",
    "model": "/model",
    "personality": "/personality",
    "reasoning": "/reasoning",
    "memories": "/memories",
    "compact": "/compact",
    "compact_confirm": "/compact confirm",
    "fork": "/fork",
    "fork_confirm": "/fork confirm",
    "fast": "/fast",
    "fast_confirm": "/fast confirm",
    "review": "/review",
    "review_confirm": "/review confirm",
    "settings_form": "",
    "memory_enable": "/memories enabled",
    "memory_enable_confirm": "/memories enabled confirm",
    "memory_disable": "/memories disabled",
    "memory_disable_confirm": "/memories disabled confirm",
}


def slash_card_action(action: str) -> dict[str, object]:
    if action not in _CARD_COMMANDS:
        raise ValueError("未知 slash 卡片动作")
    return {"namespace": SLASH_COMMAND_CARD_NAMESPACE, "version": SLASH_COMMAND_CARD_VERSION, "action": action}


def slash_card_command(value: object, form_value: object = None) -> str | None:
    if not isinstance(value, Mapping) or set(value) != {"namespace", "version", "action"}:
        return None
    if value.get("namespace") != SLASH_COMMAND_CARD_NAMESPACE or value.get("version") != SLASH_COMMAND_CARD_VERSION:
        return None
    action = value.get("action")
    if type(action) is not str or action not in _CARD_COMMANDS:
        return None
    if action == "settings_form":
        if not isinstance(form_value, Mapping) or set(form_value) != {"setting_kind", "setting_value"}:
            return None
        kind, selected = form_value.get("setting_kind"), form_value.get("setting_value")
        if kind not in {"model", "personality", "reasoning"} or type(selected) is not str:
            return None
        return f"/{kind} {selected}"
    if form_value not in (None, {}):
        return None
    return _CARD_COMMANDS[action]


def slash_card_action_fingerprint(value: object) -> str | None:
    if slash_card_command(value) is None and not (
        isinstance(value, Mapping) and value.get("action") == "settings_form"
    ):
        return None
    canonical = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _button(label: str, action: str, *, primary: bool = False) -> dict[str, Any]:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": "primary" if primary else "default",
        "value": slash_card_action(action),
    }


def _card(title: str, subtitle: str, elements: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {"template": "blue", "title": {"tag": "plain_text", "content": title}, "subtitle": {"tag": "plain_text", "content": subtitle}},
        "body": {"elements": [dict(item) for item in elements]},
    }


def build_slash_catalog_card(page: int = 1) -> dict[str, Any]:
    pages = (len(OFFICIAL_SLASH_CAPABILITIES) + SLASH_PAGE_SIZE - 1) // SLASH_PAGE_SIZE
    if page < 1 or page > pages:
        raise ValueError("slash 页码超出范围")
    start = (page - 1) * SLASH_PAGE_SIZE
    items = OFFICIAL_SLASH_CAPABILITIES[start : start + SLASH_PAGE_SIZE]
    elements: list[Mapping[str, Any]] = []
    labels = {
        "remote": "远程能力",
        "existing": "复用现有流程",
        "context": "仅对应消息上下文",
        "disabled": "当前不可远程执行",
    }
    for item in items:
        executable_text = "是" if item.executable else "否"
        detail = (
            f"**{item.command}｜{item.label}**\n"
            f"用法：{item.usage}\n"
            f"{item.description}\n"
            f"可执行：{executable_text}\n"
            f"状态：{labels[item.mode]}"
        )
        if item.disabled_reason:
            detail += f"\n原因：{item.disabled_reason}"
        if item.write_unavailable_reason:
            detail += f"\n写入限制：{item.write_unavailable_reason}"
        elements.append({"tag": "markdown", "content": detail})
    nav: list[Mapping[str, Any]] = []
    if page > 1:
        nav.append(_button("上一页", f"catalog_{page - 1}"))
    if page < pages:
        nav.append(_button("下一页", f"catalog_{page + 1}", primary=True))
    if nav:
        elements.append({"tag": "column_set", "columns": [{"tag": "column", "width": "weighted", "weight": 1, "elements": [button]} for button in nav]})
    elements.append({"tag": "markdown", "content": "<font color='grey'>可执行命令仍会按目标会话、权限、确认和 exactly-once 门禁处理。</font>"})
    return _card(
        "Codex 斜杠指令",
        f"第 {page}/{pages} 页 · 官方 {len(OFFICIAL_SLASH_CAPABILITIES)} 项",
        elements,
    )


def build_slash_operations_card(title: str) -> dict[str, Any]:
    rows = [
        ("MCP 状态", "mcp", "会话状态", "status"),
        ("模型", "model", "个性", "personality"),
        ("推理强度", "reasoning", "记忆模式", "memories"),
        ("压缩上下文", "compact", "复制会话", "fork"),
        ("快速模式", "fast", "代码审查", "review"),
        ("全部指令", "catalog", "MCP 状态", "mcp"),
    ]
    elements: list[Mapping[str, Any]] = [
        {"tag": "markdown", "content": "所有动作都绑定当前选定会话；写操作会先确认。"}
    ]
    for left_label, left_action, right_label, right_action in rows:
        elements.append({
            "tag": "column_set",
            "columns": [
                {"tag": "column", "width": "weighted", "weight": 1, "elements": [_button(left_label, left_action, primary=left_action in {"mcp", "model"})]},
                {"tag": "column", "width": "weighted", "weight": 1, "elements": [_button(right_label, right_action)]},
            ],
        })
    return _card("Codex 控制", title or "已选会话", elements)


def build_confirmation_card(title: str, action: str, message: str) -> dict[str, Any]:
    confirm_action = {
        "compact": "compact_confirm", "fork": "fork_confirm", "fast": "fast_confirm",
        "review": "review_confirm", "memory_enable": "memory_enable_confirm",
        "memory_disable": "memory_disable_confirm",
    }.get(action)
    if confirm_action is None:
        raise ValueError("未知确认动作")
    return _card(
        "确认 Codex 操作",
        title or "已选会话",
        [
            {"tag": "markdown", "content": message},
            {**_button("确认执行", confirm_action, primary=True), "confirm": {"title": {"tag": "plain_text", "content": "再次确认"}, "text": {"tag": "plain_text", "content": message}}},
        ],
    )


def slash_card_action_fingerprints(card: Mapping[str, Any]) -> tuple[str, ...]:
    found: list[str] = []
    def visit(node: object) -> None:
        if isinstance(node, Mapping):
            if node.get("tag") == "button":
                fingerprint = slash_card_action_fingerprint(node.get("value"))
                if fingerprint:
                    found.append(fingerprint)
            for child in node.values():
                visit(child)
        elif isinstance(node, Sequence) and not isinstance(node, (str, bytes, bytearray)):
            for child in node:
                visit(child)
    visit(card)
    return tuple(dict.fromkeys(found))


__all__ = [
    "OFFICIAL_SLASH_CAPABILITIES", "OFFICIAL_SLASH_COMMANDS",
    "SLASH_COMMAND_CATALOG", "SLASH_COMMAND_ALIASES",
    "SLASH_COMMAND_ENTRY_ALIASES", "SLASH_COMMAND_CARD_NAMESPACE",
    "SLASH_COMMAND_CARD_VERSION", "SLASH_COMMAND_ENTRY_COMMAND", "SlashCapability",
    "SlashCommand", "build_confirmation_card", "build_slash_catalog_card",
    "build_slash_operations_card", "parse_slash_command", "slash_card_action",
    "slash_card_action_fingerprint", "slash_card_action_fingerprints",
    "slash_card_command",
]
