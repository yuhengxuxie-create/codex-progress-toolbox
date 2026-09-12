"""飞书“查询会话”卡片及其有限动作协议。"""

from __future__ import annotations

from .card_text_hints import text_instruction_blocks

import hashlib
import json
import re
import unicodedata
from typing import Any, Mapping, Sequence


SESSION_QUERY_ENTRY_COMMAND = "查询会话"
SESSION_QUERY_MENU_EVENT_KEY = "progress_wx_session_query"
SESSION_QUERY_CARD_NAMESPACE = "progress_wx.session_query"
SESSION_QUERY_CARD_VERSION = 1

_ACTION_COMMANDS = {
    "project_sessions": "查询项目列表",
    "personal_sessions": "查询个人会话",
    "search_sessions": "搜索会话",
    "new_project": "新建项目",
    "new_project_thread": "新建项目会话",
    "new_personal_thread": "新建个人会话",
    "thread_add_monitor": "添加监测",
    "thread_remove_monitor": "移除监测",
    "thread_raw": ".原文",
    "thread_archive": ".归档",
    "thread_remote_control": "远程控制",
    "thread_reply_open": "继续对话",
}

_FORM_ACTIONS = frozenset({"thread_reply_submit", "session_search_submit"})

_LABEL_ACTIONS = {
    "expand_project": (re.compile(r"^A[0-9]{2,}$"), "展开"),
    "select_project_thread": (re.compile(r"^a[0-9]{2,}$"), "选定"),
    "monitor_project_thread": (re.compile(r"^a[0-9]{2,}$"), "添加监测"),
    "select_personal_thread": (re.compile(r"^p[0-9]{2,}$"), "选定"),
    "monitor_personal_thread": (re.compile(r"^p[0-9]{2,}$"), "添加监测"),
}


def _strict_action_name(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    namespace = value.get("namespace")
    version = value.get("version")
    action = value.get("action")
    if type(namespace) is not str or namespace != SESSION_QUERY_CARD_NAMESPACE:
        return None
    if type(version) is not int or version != SESSION_QUERY_CARD_VERSION:
        return None
    if type(action) is not str or not action:
        return None
    return action


def _has_disallowed_control(value: str, *, multiline: bool) -> bool:
    allowed = {"\t", "\r", "\n"} if multiline else set()
    return any(
        character not in allowed and unicodedata.category(character) == "Cc"
        for character in value
    )


def _compact_label(value: object, limit: int = 42) -> str:
    text = " ".join(str(value or "").split()) or "未命名"
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def session_query_action(
    action: str,
    *,
    label: str | None = None,
    page: int | None = None,
) -> dict[str, object]:
    """构造不可扩权的卡片动作值。"""

    if type(action) is not str or not action:
        raise ValueError("会话查询卡片动作必须是非空字符串")
    if label is not None and type(label) is not str:
        raise ValueError("会话查询卡片标号必须是字符串")
    if page is not None and (type(page) is not int or page < 1):
        raise ValueError("会话查询卡片页码无效")

    value: dict[str, object] = {
        "namespace": SESSION_QUERY_CARD_NAMESPACE,
        "version": SESSION_QUERY_CARD_VERSION,
        "action": action,
    }
    if action in _ACTION_COMMANDS or action in _FORM_ACTIONS:
        if label is not None or page is not None:
            raise ValueError("固定动作不能携带额外参数")
        return value
    if action in _LABEL_ACTIONS:
        pattern, _prefix = _LABEL_ACTIONS[action]
        if label is None or pattern.fullmatch(label) is None or page is not None:
            raise ValueError("会话查询卡片标号无效")
        value["label"] = label
        return value
    if action == "list_page":
        if label is not None or page is None or page < 1:
            raise ValueError("会话查询卡片页码无效")
        value["page"] = page
        return value
    raise ValueError("未知的会话查询卡片动作")


def session_query_command(value: object) -> str | None:
    """只接受本版本生成的精确动作对象，并映射到既有文字命令。"""

    action = _strict_action_name(value)
    if action is None:
        return None
    if action in _ACTION_COMMANDS:
        if set(value) != {"namespace", "version", "action"}:
            return None
        return _ACTION_COMMANDS[action]
    if action in _LABEL_ACTIONS:
        if set(value) != {"namespace", "version", "action", "label"}:
            return None
        label = value.get("label")
        if type(label) is not str:
            return None
        pattern, prefix = _LABEL_ACTIONS[action]
        return f"{prefix}{label}" if pattern.fullmatch(label) else None
    if action == "list_page":
        if set(value) != {"namespace", "version", "action", "page"}:
            return None
        page = value.get("page")
        if type(page) is not int or page < 1:
            return None
        return f"第{page}页"
    return None


def session_query_form_command(
    value: object,
    form_value: object,
) -> str | None:
    """把受信表单值转换成既有文字协议；None 表示拒绝该回调。"""

    action = _strict_action_name(value)
    if action is None:
        return None
    if set(value) != {"namespace", "version", "action"}:
        return None
    if action not in _FORM_ACTIONS or not isinstance(form_value, Mapping):
        return None
    if action == "thread_reply_submit":
        if set(form_value) != {"thread_reply"}:
            return None
        content = form_value.get("thread_reply")
        if (
            type(content) is not str
            or len(content) > 4000
            or _has_disallowed_control(content, multiline=True)
        ):
            return None
        # Preserve the form value byte-for-byte.  In particular, a leading
        # space/newline before ``/`` or ``$`` is meaningful: it keeps the
        # submitted text in the ordinary prompt plane instead of promoting it
        # to a control command.  The downstream thread continuation performs
        # the whitespace-only check and sends ordinary prompt text unchanged.
        return content
    allowed = {"session_name", "session_description", "session_activity"}
    if not set(form_value).issubset(allowed):
        return None
    values: dict[str, str] = {}
    limits = {
        "session_name": 200,
        "session_description": 1000,
        "session_activity": 100,
    }
    for key in allowed:
        raw = form_value.get(key, "")
        if (
            type(raw) is not str
            or len(raw) > limits[key]
            or _has_disallowed_control(raw, multiline=False)
        ):
            return None
        values[key] = raw.strip()
    return (
        f"会话名称：{values['session_name']}\n"
        f"会话描述：{values['session_description']}\n"
        f"会话最后活动时间：{values['session_activity']}"
    )


def session_query_action_fingerprint(value: object) -> str | None:
    """返回受信动作值的规范指纹；无效或扩权动作返回 ``None``。"""

    action = _strict_action_name(value)
    if action is None or not isinstance(value, Mapping):
        return None
    valid = session_query_command(value) is not None or (
        action in _FORM_ACTIONS
        and set(value) == {"namespace", "version", "action"}
    )
    if not valid:
        return None
    canonical = json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def session_query_card_action_fingerprints(
    card: Mapping[str, Any],
) -> tuple[str, ...]:
    """冻结卡片实际包含的完整按钮动作，阻止同名参数动作被伪造。"""

    found: list[str] = []

    def visit(node: object) -> None:
        if isinstance(node, Mapping):
            if node.get("tag") == "button":
                fingerprint = session_query_action_fingerprint(node.get("value"))
                if fingerprint is not None:
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


def _button(
    label: str,
    action: str,
    *,
    style: str = "default",
    target_label: str | None = None,
    page: int | None = None,
) -> dict[str, Any]:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": style,
        "value": session_query_action(action, label=target_label, page=page),
    }


def _columns(*elements: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "tag": "column_set",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "elements": [dict(element)],
            }
            for element in elements
        ],
    }


def _plain_text_div(
    content: object,
    *,
    text_size: str | None = None,
    text_color: str | None = None,
) -> dict[str, Any]:
    """构造不依赖 Markdown 解析的 CardKit 原生文本块。"""

    text: dict[str, Any] = {"tag": "plain_text", "content": str(content or "")}
    if text_size is not None:
        text["text_size"] = text_size
    if text_color is not None:
        text["text_color"] = text_color
    return {
        "tag": "div",
        "text": text,
    }


def _fact_row(label: object, value: object) -> dict[str, Any]:
    """将概览元数据渲染为适合手机窄屏的原生标签/值双列行。"""

    label_text = str(label or "")
    if not label_text.strip():
        label_text = "信息"
    value_text = str(value or "")
    if not value_text.strip():
        value_text = "—"
    return {
        "tag": "column_set",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "top",
                "elements": [
                    _plain_text_div(
                        f"{label_text}：",
                        text_size="notation",
                        text_color="grey",
                    )
                ],
            },
            {
                "tag": "column",
                "width": "weighted",
                "weight": 4,
                "vertical_align": "top",
                "elements": [_plain_text_div(value_text)],
            },
        ],
    }


def _card(
    title: str,
    subtitle: str,
    elements: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "template": "purple",
            "title": {"tag": "plain_text", "content": title},
            "subtitle": {"tag": "plain_text", "content": subtitle},
        },
        "body": {"elements": [dict(item) for item in elements]},
    }


def _paging(page: int, pages: int) -> Mapping[str, Any] | None:
    buttons: list[Mapping[str, Any]] = []
    if page > 1:
        buttons.append(_button("上一页", "list_page", page=page - 1))
    if page < pages:
        buttons.append(_button("下一页", "list_page", page=page + 1))
    return _columns(*buttons) if buttons else None


def _input(
    name: str,
    label: str,
    placeholder: str,
    max_length: int,
) -> dict[str, Any]:
    if not 1 <= max_length <= 1000:
        raise ValueError("CardKit 2.0 input max_length 必须在 1 到 1000 之间")
    return {
        "tag": "input",
        "name": name,
        "placeholder": {"tag": "plain_text", "content": placeholder},
        "max_length": max_length,
        "label": {"tag": "plain_text", "content": label},
        "label_position": "top",
    }


def _form_submit(label: str, action: str) -> dict[str, Any]:
    return {
        **_button(label, action, style="primary"),
        "name": action,
        # 飞书输入表单的稳定回调协议仍是经典交互卡：按钮必须位于 form
        # 内并声明 action_type=form_submit。这个字段不能混入 CardKit 2.0
        # 外壳，否则服务端会以 230099 拒绝；所有表单由 _form_card 单独
        # 构造经典卡片，普通导航卡继续使用 _card/CardKit 2.0。
        "action_type": "form_submit",
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
    """构造可在飞书移动端真实回调 form_value 的经典交互卡。"""

    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": "purple",
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": [
            _legacy_markdown(f"**{_md_escape(subtitle)}**"),
            *[dict(item) for item in elements],
        ],
    }


def _md_escape(value: object) -> str:
    text = str(value or "")
    for source, replacement in (
        ("\\", "\\\\"),
        ("*", "\\*"),
        ("_", "\\_"),
        ("[", "\\["),
        ("<", "&lt;"),
        (">", "&gt;"),
    ):
        text = text.replace(source, replacement)
    return text


def build_session_query_card() -> dict[str, Any]:
    """返回 CardKit 2.0 查询入口；真实查询仍由既有控制器完成。"""

    return _card(
        "查询 Codex 会话",
        "选择查询方式",
        [
                {
                    "tag": "markdown",
                    "content": "项目和个人会话会直接列出；模糊搜索会打开现有搜索表单。",
                },
                _columns(
                    _button("项目会话", "project_sessions", style="primary"),
                    _button("个人会话", "personal_sessions"),
                ),
                _button("模糊搜索", "search_sessions"),
                *text_instruction_blocks(['直接发送以下任一内容：', '• .查询项目列表', '• .查询个人会话', '• .搜索会话'], legacy=False),
        ],
    )


def build_project_list_card(
    projects: Sequence[Mapping[str, Any]],
    *,
    page: int,
    pages: int,
    total: int,
) -> dict[str, Any]:
    elements: list[Mapping[str, Any]] = [
        {"tag": "markdown", "content": f"共 **{total}** 个项目；点击项目查看会话。"}
    ]
    if not projects:
        elements.append({"tag": "markdown", "content": "目前没有项目。"})
    for item in projects:
        label = str(item.get("label") or "")
        name = _compact_label(item.get("name") or "未命名项目")
        count = int(item.get("thread_count") or 0)
        elements.append(
            _button(
                f"{name}（{count} 个会话）",
                "expand_project",
                target_label=label,
                style="primary",
            )
        )
    paging = _paging(page, pages)
    if paging is not None:
        elements.append(paging)
    elements.append(
        _columns(
            _button("新建项目", "new_project"),
            _button("新建项目会话", "new_project_thread"),
        )
    )
    elements.extend(text_instruction_blocks(['先回复本卡片，再发送：', '• 展开A01', '• 第2页'], legacy=False))
    return _card("Codex 项目", f"第 {page}/{pages} 页", elements)


def build_thread_list_card(
    threads: Sequence[Mapping[str, Any]],
    *,
    page: int,
    pages: int,
    total: int,
    project_name: str | None = None,
) -> dict[str, Any]:
    is_project = project_name is not None
    title = f"{project_name} · 会话" if is_project else "Codex 个人会话"
    elements: list[Mapping[str, Any]] = [
        {"tag": "markdown", "content": f"共 **{total}** 个会话；点击名称查看详情。"}
    ]
    if not threads:
        elements.append({"tag": "markdown", "content": "目前没有会话。"})
    select_action = "select_project_thread" if is_project else "select_personal_thread"
    for item in threads:
        label = str(item.get("label") or "")
        thread_title = _compact_label(item.get("title") or "未命名会话")
        elements.append(
            _button(
                thread_title,
                select_action,
                target_label=label,
                style="primary",
            )
        )
    paging = _paging(page, pages)
    if paging is not None:
        elements.append(paging)
    elements.append(
        _button(
            "新建项目会话" if is_project else "新建个人会话",
            "new_project_thread" if is_project else "new_personal_thread",
        )
    )
    prefix = "a" if is_project else "p"
    elements.extend(text_instruction_blocks(['先回复本卡片，再发送：', f'• 选定{prefix}01', f'• 添加监测{prefix}01', '• 第2页'], legacy=False))
    return _card(title, f"第 {page}/{pages} 页", elements)


def build_session_search_form_card() -> dict[str, Any]:
    """构造与现有三字段文字搜索表单等价的卡片表单。"""

    return _form_card(
        "模糊搜索 Codex 会话",
        "记得多少填多少，三项都可留空",
        [
            _legacy_markdown(
                "名称、描述和时间会一起理解；填错位置也不会直接排除会话。"
            ),
            {
                "tag": "form",
                "name": "session_search_form",
                "elements": [
                    _input(
                        "session_name",
                        "会话名称",
                        "例如：医疗科技选题（可留空）",
                        200,
                    ),
                    _input(
                        "session_description",
                        "会话描述",
                        "输入你记得的主题、工作内容或关键词",
                        1000,
                    ),
                    _input(
                        "session_activity",
                        "最后活动时间",
                        "例如：今天、最近7天、2026年8月",
                        100,
                    ),
                    _form_submit("开始搜索", "session_search_submit"),
                ],
            },
            *text_instruction_blocks(['回复本卡片，复制并填写：', '会话名称：', '会话描述：', '会话最后活动时间：'], legacy=True),
        ],
    )


def build_thread_overview_card(
    *,
    title: str,
    facts: Sequence[tuple[str, str]],
    sections: Sequence[tuple[str, str]],
    monitor_status: str,
) -> dict[str, Any]:
    """构造可直接续聊的会话概览卡；上下文仍由出站 message_id 绑定。"""

    elements: list[Mapping[str, Any]] = []
    if facts:
        elements.extend(_fact_row(label, value) for label, value in facts)
    for label, content in sections:
        if not str(content or "").strip():
            continue
        elements.append(
            {
                "tag": "markdown",
                "content": f"**{_md_escape(label)}**\n{_md_escape(content)}",
            }
        )
    # CardKit 2.0 在移动端对“长概览 + 内嵌表单 + 多组独立按钮”的
    # 混合卡片不会稳定产生回调。概览只保留一个打开动作，真正提交使用
    # 单独的纯表单子卡；thread_id 继续只保存在服务端绑定上下文中。
    elements.append(_button("继续对话", "thread_reply_open", style="primary"))
    monitor_action = (
        "thread_add_monitor" if monitor_status == "未监测" else "thread_remove_monitor"
    )
    monitor_label = "添加监测" if monitor_status == "未监测" else "移除监测"
    elements.append(
        _columns(
            _button(monitor_label, monitor_action),
            _button("查看原文", "thread_raw"),
        )
    )
    elements.append(
        {
            **_button("归档会话", "thread_archive"),
            "confirm": {
                "title": {"tag": "plain_text", "content": "确认归档会话"},
                "text": {
                    "tag": "plain_text",
                    "content": "归档会改变 Codex 中该会话的状态，是否继续？",
                },
            },
        }
    )
    elements.append(_button("指令使用", "thread_remote_control", style="primary"))
    elements.extend(text_instruction_blocks(['回复本卡片发送新要求，或发送：', '• 添加监测', '• 移除监测', '• .原文', '• .归档', '• 指令使用'], legacy=False))
    return _card(_compact_label(title, 70), "会话概览与快捷操作", elements)


def build_thread_reply_form_card(title: str) -> dict[str, Any]:
    """构造只包含会话续聊表单的经典交互子卡。"""

    return _form_card(
        "继续 Codex 会话",
        _compact_label(title, 70),
        [
            _legacy_markdown("提交内容会进入刚才选定的同一个 Codex 会话。"),
            {
                "tag": "form",
                "name": "thread_reply_form",
                "elements": [
                    _input(
                        "thread_reply",
                        "下一步要求",
                        "输入普通要求，或以 /、$ 开头输入正式控制指令",
                        1000,
                    ),
                    _form_submit("发送到此会话", "thread_reply_submit"),
                ],
            },
            *text_instruction_blocks(['回复本卡片，发送文字或图片。'], legacy=True),
        ],
    )


def session_query_text_menu() -> str:
    return (
        "查询 Codex 会话\n\n"
        "请发送以下任一文字命令：\n"
        ".查询项目列表\n"
        ".查询个人会话\n"
        ".搜索会话"
    )


__all__ = [
    "SESSION_QUERY_CARD_NAMESPACE",
    "SESSION_QUERY_CARD_VERSION",
    "SESSION_QUERY_ENTRY_COMMAND",
    "SESSION_QUERY_MENU_EVENT_KEY",
    "build_session_query_card",
    "build_project_list_card",
    "build_session_search_form_card",
    "build_thread_list_card",
    "build_thread_overview_card",
    "build_thread_reply_form_card",
    "session_query_action",
    "session_query_action_fingerprint",
    "session_query_card_action_fingerprints",
    "session_query_command",
    "session_query_form_command",
    "session_query_text_menu",
]
