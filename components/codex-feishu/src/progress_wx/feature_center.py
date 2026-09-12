"""飞书功能中心的纯卡片构造器。

这个模块只描述稳定的功能入口，不绑定具体 Codex 会话。目标会话、工作目录、
技能路径和用户内容都必须由上层在收到动作后，依据持久上下文重新解析；它们
不能进入功能中心卡片或卡片动作值。
"""

from __future__ import annotations

from .card_text_hints import text_instruction_blocks

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any, Mapping, Sequence


FEATURE_CENTER_ENTRY_COMMAND = "功能中心"
FEATURE_CENTER_MENU_EVENT_KEY = "progress_wx_feature_center"
FEATURE_CENTER_CARD_NAMESPACE = "progress_wx.feature_center"
FEATURE_CENTER_CARD_VERSION = 1
FEATURE_OPERATION_CARD_NAMESPACE = "progress_wx.feature_operation"
FEATURE_OPERATION_CARD_VERSION = 1

_FEATURE_OPERATION_COMMANDS = {
    "monitor_refresh": "监测设置",
    "monitor_enable_request": "开启自动监测",
    "monitor_disable_request": "关闭自动监测",
    "monitor_enable_confirm": "确认开启自动监测",
    "monitor_disable_confirm": "确认关闭自动监测",
}


@dataclass(frozen=True, slots=True)
class FeatureEntry:
    """功能中心中的一个稳定入口。

    ``action`` 是唯一放进卡片 value 的标识。``enabled`` 和
    ``disabled_reason`` 只用于展示当前能力状态；它们不携带目标身份。
    """

    action: str
    label: str
    description: str
    fallback: str
    enabled: bool = True
    disabled_reason: str = ""

    def __post_init__(self) -> None:
        for field_name in ("action", "label", "description", "fallback"):
            value = getattr(self, field_name)
            if type(value) is not str or not value.strip():
                raise ValueError(f"功能入口 {field_name} 必须是非空文本")
        if any(character in self.action for character in ("/", "\\", " ", "\n", "\r")):
            raise ValueError("功能入口 action 必须是稳定的单一键")
        if type(self.enabled) is not bool:
            raise ValueError("功能入口 enabled 必须是布尔值")
        if type(self.disabled_reason) is not str:
            raise ValueError("功能入口 disabled_reason 必须是文本")
        if not self.enabled and not self.disabled_reason.strip():
            raise ValueError("不可用功能必须说明 disabled_reason")


@dataclass(frozen=True, slots=True)
class FeatureSection:
    """功能中心的一个分区。"""

    key: str
    title: str
    description: str
    entries: tuple[FeatureEntry, ...]

    def __post_init__(self) -> None:
        for field_name in ("key", "title", "description"):
            value = getattr(self, field_name)
            if type(value) is not str or not value.strip():
                raise ValueError(f"功能分区 {field_name} 必须是非空文本")
        if not isinstance(self.entries, tuple):
            raise ValueError("功能分区 entries 必须是 tuple")
        seen: set[str] = set()
        for entry in self.entries:
            if not isinstance(entry, FeatureEntry):
                raise ValueError("功能分区只能包含 FeatureEntry")
            if entry.action in seen:
                raise ValueError(f"功能入口 action 重复：{entry.action}")
            seen.add(entry.action)


def _entry(
    action: str,
    label: str,
    description: str,
    fallback: str,
) -> FeatureEntry:
    return FeatureEntry(action, label, description, fallback)


def _display_direct_command(command: str) -> str:
    """为纯文字说明显示普通入口的半角句点，不改变协议值。"""

    return command if command.startswith((".", "/", "$")) else f".{command}"


DEFAULT_FEATURE_SECTIONS: tuple[FeatureSection, ...] = (
    FeatureSection(
        key="sessions",
        title="会话",
        description="查看、搜索、创建或继续 Codex 会话。",
        entries=(
            _entry("session_query", "查询会话", "先看项目或个人会话列表。", "查询会话"),
            _entry("session_search", "搜索会话", "按名称、描述或活动时间查找会话。", "搜索会话"),
            _entry("project_list", "项目会话", "打开项目会话列表。", "查询项目列表"),
            _entry("personal_list", "个人会话", "打开个人会话列表。", "查询个人会话"),
            _entry("project_create", "新建项目", "注册一个新的 Codex 项目。", "新建项目"),
            _entry("project_task_create", "项目新会话", "选择已有项目和运行方式后创建会话。", "/project"),
            _entry("personal_create", "新建个人会话", "创建一个新的个人会话。", "新建个人会话"),
        ),
    ),
    FeatureSection(
        key="monitoring",
        title="进度监测",
        description="查看监测状态，或管理需要持续关注的会话。",
        entries=(
            _entry("monitor_list", "查看监测", "查看正在监测的项目和个人会话。", "查询监测列表"),
            _entry("monitor_add", "添加监测", "从会话列表中选择一个会话加入监测。", "添加监测任务"),
            _entry("monitor_remove", "移除监测", "从监测列表中选择一个会话移除。", "移除监测任务"),
            _entry("monitor_settings", "监测设置", "查看或调整自动监测开关。", "监测设置"),
            _entry("reset_alert_status", "预警状态", "只读查看重置预警运行状态。", "重置预警状态"),
            _entry("reset_alert_recent", "最近预警", "只读查看已经触发的最近预警。", "最近预警"),
        ),
    ),
    FeatureSection(
        key="codex",
        title="指令使用",
        description="直接浏览个人/全局 Skills 和斜杠指令说明，无需先选择会话。",
        entries=(
            _entry("codex_skills", "Skills", "查看已启用的个人/全局技能；浏览不执行。", "/skills"),
            _entry("codex_slash", "斜杠指令", "直接查看指令用途、支持情况和使用方法。", "/commands"),
        ),
    ),
    FeatureSection(
        key="account_help",
        title="账户与帮助",
        description="查看额度，或了解飞书机器人的使用方法。",
        entries=(
            _entry("quota", "剩余额度", "查看 Codex 每周额度和剩余重置卡。", "查询剩余额度"),
            _entry("usage_guide", "使用说明", "查看图文版使用说明。", "使用说明"),
            _entry("usage_text", "文字版使用说明", "需要时查看纯文字版说明。", "文字版使用说明"),
        ),
    ),
)


def _all_entries(sections: Sequence[FeatureSection]) -> tuple[FeatureEntry, ...]:
    entries: list[FeatureEntry] = []
    seen: set[str] = set()
    for section in sections:
        if not isinstance(section, FeatureSection):
            raise ValueError("sections 只能包含 FeatureSection")
        for entry in section.entries:
            if entry.action in seen:
                raise ValueError(f"功能入口 action 重复：{entry.action}")
            seen.add(entry.action)
            entries.append(entry)
    return tuple(entries)


def feature_center_action(action: str) -> dict[str, object]:
    """生成不含目标身份的严格功能中心动作值。"""

    if type(action) is not str or not action or action not in {
        entry.action for entry in _all_entries(DEFAULT_FEATURE_SECTIONS) if entry.enabled
    }:
        raise ValueError("未知的功能中心动作")
    return {
        "namespace": FEATURE_CENTER_CARD_NAMESPACE,
        "version": FEATURE_CENTER_CARD_VERSION,
        "action": action,
    }


def _known_actions(sections: Sequence[FeatureSection]) -> frozenset[str]:
    # 不可用入口可以继续出现在卡片中解释原因，但服务端永不接受它的动作值。
    # 这样旧卡或伪造 payload 也不能绕过显示层的灰态。
    return frozenset(entry.action for entry in _all_entries(sections) if entry.enabled)


def parse_feature_center_action(
    value: object,
    *,
    sections: Sequence[FeatureSection] = DEFAULT_FEATURE_SECTIONS,
) -> str | None:
    """严格解析动作值，只返回稳定 action key。"""

    if not isinstance(value, Mapping):
        return None
    if set(value) != {"namespace", "version", "action"}:
        return None
    if value.get("namespace") != FEATURE_CENTER_CARD_NAMESPACE:
        return None
    if type(value.get("version")) is not int or value.get("version") != FEATURE_CENTER_CARD_VERSION:
        return None
    action = value.get("action")
    if type(action) is not str or action not in _known_actions(sections):
        return None
    return action


def feature_center_action_command(value: object) -> str | None:
    """把启用入口的严格动作值映射回同一真实文字命令。"""

    action = parse_feature_center_action(value)
    if action is None:
        return None
    entry = next(
        item
        for item in _all_entries(DEFAULT_FEATURE_SECTIONS)
        if item.action == action and item.enabled
    )
    return entry.fallback


def feature_center_direct_commands(
    sections: Sequence[FeatureSection] = DEFAULT_FEATURE_SECTIONS,
) -> dict[str, str]:
    """Return enabled user-facing labels mapped to their canonical command.

    The visible labels are deliberately accepted as normal top-level text so
    typing a feature name opens only that feature instead of another menu.
    Duplicate labels fail closed rather than silently choosing one route.
    """

    result: dict[str, str] = {}
    for entry in _all_entries(sections):
        if not entry.enabled:
            continue
        if entry.label in result:
            raise ValueError(f"功能入口 label 重复：{entry.label}")
        result[entry.label] = entry.fallback
    return result


def feature_center_action_fingerprint(
    value: object,
    *,
    sections: Sequence[FeatureSection] = DEFAULT_FEATURE_SECTIONS,
) -> str | None:
    """为卡片动作生成稳定指纹；上层仍须叠加入站 event_id 做幂等。"""

    if parse_feature_center_action(value, sections=sections) is None:
        return None
    canonical = json.dumps(
        dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def feature_operation_action(action: str) -> dict[str, object]:
    """生成不含状态值、目标身份或用户内容的二级功能动作。"""

    if type(action) is not str or action not in _FEATURE_OPERATION_COMMANDS:
        raise ValueError("未知的功能操作")
    return {
        "namespace": FEATURE_OPERATION_CARD_NAMESPACE,
        "version": FEATURE_OPERATION_CARD_VERSION,
        "action": action,
    }


def parse_feature_operation_action(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    if set(value) != {"namespace", "version", "action"}:
        return None
    if value.get("namespace") != FEATURE_OPERATION_CARD_NAMESPACE:
        return None
    if (
        type(value.get("version")) is not int
        or value.get("version") != FEATURE_OPERATION_CARD_VERSION
    ):
        return None
    action = value.get("action")
    if type(action) is not str or action not in _FEATURE_OPERATION_COMMANDS:
        return None
    return action


def feature_operation_action_command(value: object) -> str | None:
    action = parse_feature_operation_action(value)
    return _FEATURE_OPERATION_COMMANDS.get(action) if action is not None else None


def feature_operation_action_fingerprint(value: object) -> str | None:
    if parse_feature_operation_action(value) is None:
        return None
    canonical = json.dumps(
        dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _button(entry: FeatureEntry) -> dict[str, Any]:
    label = entry.label if entry.enabled else f"{entry.label}（暂不可用）"
    result = {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": "primary" if entry.enabled else "default",
        "value": {
            "namespace": FEATURE_CENTER_CARD_NAMESPACE,
            "version": FEATURE_CENTER_CARD_VERSION,
            "action": entry.action,
        },
    }
    if not entry.enabled:
        result["disabled"] = True
    return result


def _card(title: str, subtitle: str, elements: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": title},
            "subtitle": {"tag": "plain_text", "content": subtitle},
        },
        "body": {"elements": [dict(element) for element in elements]},
    }


def build_monitor_settings_card(
    enabled: bool,
    effective_at: int | None,
    *,
    confirm: bool | None = None,
) -> dict[str, Any]:
    """构造自动监测设置卡；写操作必须先经过独立确认卡。"""

    if type(enabled) is not bool:
        raise ValueError("enabled 必须是布尔值")
    if effective_at is not None and type(effective_at) is not int:
        raise ValueError("effective_at 必须是整数或 None")
    state_text = "已开启" if enabled else "已关闭"
    effective_text = (
        datetime.fromtimestamp(
            effective_at, tz=timezone(timedelta(hours=8))
        ).strftime("%Y-%m-%d %H:%M:%S")
        if effective_at is not None
        else "暂无变更记录"
    )
    elements: list[Mapping[str, Any]] = [
        {
            "tag": "markdown",
            "content": (
                f"**自动监测：**{state_text}\n"
                f"**最后生效时间：**{effective_text}\n\n"
                "关闭只停止自动发现和自动加入；现有自动项按原到期规则退出，"
                "手动长期监测及通知不受影响。"
            ),
        }
    ]
    if confirm is None:
        target_action = (
            "monitor_disable_request" if enabled else "monitor_enable_request"
        )
        target_label = "关闭自动监测" if enabled else "开启自动监测"
        elements.append(
            {
                "tag": "column_set",
                "columns": [
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "elements": [
                            {
                                "tag": "button",
                                "text": {"tag": "plain_text", "content": target_label},
                                "type": "primary",
                                "value": feature_operation_action(target_action),
                            }
                        ],
                    },
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "elements": [
                            {
                                "tag": "button",
                                "text": {"tag": "plain_text", "content": "刷新状态"},
                                "value": feature_operation_action("monitor_refresh"),
                            }
                        ],
                    },
                ],
            }
        )
        subtitle = "查看当前状态，修改前会再次确认"
    else:
        action = "monitor_enable_confirm" if confirm else "monitor_disable_confirm"
        label = "确认开启" if confirm else "确认关闭"
        elements.extend(
            (
                {
                    "tag": "markdown",
                    "content": f"请确认：将自动监测设置为{'开启' if confirm else '关闭'}。",
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": label},
                    "type": "danger" if not confirm else "primary",
                    "value": feature_operation_action(action),
                },
            )
        )
        subtitle = "一次性确认；未确认不会修改"
    return _card("进度监测设置", subtitle, elements)


def feature_operation_card_action_fingerprints(
    card: Mapping[str, Any],
) -> tuple[str, ...]:
    found: list[str] = []

    def visit(node: object) -> None:
        if isinstance(node, Mapping):
            if node.get("tag") == "button":
                fingerprint = feature_operation_action_fingerprint(node.get("value"))
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


def build_feature_center_card(
    sections: Sequence[FeatureSection] = DEFAULT_FEATURE_SECTIONS,
    *,
    title: str = "Codex 功能中心",
    subtitle: str = "选择一个入口开始",
) -> dict[str, Any]:
    """构造固定入口卡片。

    卡片只含分区描述、入口文案和稳定动作值。``sections`` 可以由上层根据
    实时能力状态替换，但不得在 ``FeatureEntry`` 文本中放入会话身份或路径。
    """

    if type(title) is not str or not title.strip():
        raise ValueError("功能中心卡片 title 必须是非空文本")
    if type(subtitle) is not str or not subtitle.strip():
        raise ValueError("功能中心卡片 subtitle 必须是非空文本")
    normalized = tuple(sections)
    _all_entries(normalized)
    elements: list[Mapping[str, Any]] = []
    for section in normalized:
        elements.append(
            {
                "tag": "markdown",
                "content": f"**{section.title}**\n{section.description}",
            }
        )
        entries = section.entries
        for start in range(0, len(entries), 2):
            row = entries[start : start + 2]
            columns: list[Mapping[str, Any]] = []
            for entry in row:
                column_elements: list[Mapping[str, Any]] = [_button(entry)]
                if not entry.enabled:
                    column_elements.append(
                        {
                            "tag": "markdown",
                            "content": f"原因：{entry.disabled_reason}",
                        }
                    )
                columns.append(
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "elements": column_elements,
                    }
                )
            elements.append({"tag": "column_set", "columns": columns})
    elements.extend(
        text_instruction_blocks(
            [
                '直接发送，例如：',
                '• .查询会话',
                '• .新建个人会话',
                '• .项目新会话',
                '• .Skills',
                '• .查看指令列表',
            ],
            legacy=False,
        )
    )
    return _card(title, subtitle, elements)


def feature_center_text_fallback(
    sections: Sequence[FeatureSection] = DEFAULT_FEATURE_SECTIONS,
) -> str:
    """返回卡片不可用时发送的纯文字功能入口说明。"""

    normalized = tuple(sections)
    _all_entries(normalized)
    lines = [
        "功能中心：",
        "请直接发送下面任一命令：",
        "斜杠目录别名：.查看指令列表、.斜杠指令、.Skills、.项目新会话、/commands、/skills、/project",
    ]
    for section in normalized:
        lines.append("")
        lines.append(f"【{section.title}】")
        for entry in section.entries:
            if entry.enabled:
                lines.append(
                    f"- {_display_direct_command(entry.fallback)}：{entry.description}"
                )
            else:
                lines.append(
                    f"- {_display_direct_command(entry.fallback)}：暂不可用（{entry.disabled_reason}）"
                )
    return "\n".join(lines)


def feature_center_card_action_fingerprints(
    card: Mapping[str, Any],
    *,
    sections: Sequence[FeatureSection] = DEFAULT_FEATURE_SECTIONS,
) -> tuple[str, ...]:
    """提取卡片中所有合法入口指纹，用于出站卡片上下文绑定。"""

    found: list[str] = []

    def visit(node: object) -> None:
        if isinstance(node, Mapping):
            if node.get("tag") == "button":
                fingerprint = feature_center_action_fingerprint(
                    node.get("value"), sections=sections
                )
                if fingerprint:
                    found.append(fingerprint)
            for child in node.values():
                visit(child)
        elif isinstance(node, Sequence) and not isinstance(node, (str, bytes, bytearray)):
            for child in node:
                visit(child)

    visit(card)
    return tuple(dict.fromkeys(found))


# 与现有模块的命名风格保持可发现性；两者是同一个纯构造器，不形成第二套语义。
build_feature_center_entry_card = build_feature_center_card
feature_center_command = feature_center_text_fallback


__all__ = [
    "DEFAULT_FEATURE_SECTIONS",
    "FEATURE_CENTER_CARD_NAMESPACE",
    "FEATURE_CENTER_CARD_VERSION",
    "FEATURE_CENTER_ENTRY_COMMAND",
    "FEATURE_CENTER_MENU_EVENT_KEY",
    "FEATURE_OPERATION_CARD_NAMESPACE",
    "FEATURE_OPERATION_CARD_VERSION",
    "FeatureEntry",
    "FeatureSection",
    "build_feature_center_card",
    "build_feature_center_entry_card",
    "build_monitor_settings_card",
    "feature_center_action",
    "feature_center_action_fingerprint",
    "feature_center_action_command",
    "feature_center_direct_commands",
    "feature_center_card_action_fingerprints",
    "feature_center_command",
    "feature_center_text_fallback",
    "feature_operation_action",
    "feature_operation_action_command",
    "feature_operation_action_fingerprint",
    "feature_operation_card_action_fingerprints",
    "parse_feature_center_action",
    "parse_feature_operation_action",
]
