from __future__ import annotations

import json

import progress_wx.slash_commands as slash_commands

from progress_wx.slash_commands import (
    OFFICIAL_SLASH_CAPABILITIES,
    SLASH_COMMAND_ENTRY_ALIASES,
    build_confirmation_card,
    build_slash_catalog_card,
    build_slash_operations_card,
    parse_slash_command,
    slash_card_action,
    slash_card_action_fingerprint,
    slash_card_action_fingerprints,
    slash_card_command,
)


def _walk(node: object):
    if isinstance(node, dict):
        yield node
        for child in node.values():
            yield from _walk(child)
    elif isinstance(node, list):
        for child in node:
            yield from _walk(child)


def test_official_catalog_has_every_supported_command_once() -> None:
    commands = [item.command for item in OFFICIAL_SLASH_CAPABILITIES]
    assert commands == [
        "/approve", "/cloud", "/cloud-environment", "/compact", "/fast",
        "/feedback", "/fork", "/goal", "/ide-context", "/init", "/local",
        "/mcp", "/memories", "/model", "/pet", "/personality", "/plan",
        "/project", "/reasoning", "/review", "/skill", "/skills", "/side", "/status", "/task",
        "/worktree",
    ]
    assert len(commands) == len(set(commands)) == 26
    assert all(item.disabled_reason for item in OFFICIAL_SLASH_CAPABILITIES if item.mode == "disabled")
    assert all(type(item.executable) is bool for item in OFFICIAL_SLASH_CAPABILITIES)
    assert all(item.usage for item in OFFICIAL_SLASH_CAPABILITIES)
    assert all(
        item.unavailable_reason
        for item in OFFICIAL_SLASH_CAPABILITIES
        if not item.executable
    )


def test_catalog_aliases_are_exact_top_level_commands() -> None:
    assert SLASH_COMMAND_ENTRY_ALIASES == ("查看指令列表", "斜杠指令", "/commands")
    for alias in SLASH_COMMAND_ENTRY_ALIASES:
        parsed = parse_slash_command(alias)
        assert parsed is not None and parsed.kind == "catalog"
        paged = parse_slash_command(f"{alias} page 2")
        assert paged is not None and (paged.kind, paged.argument) == (
            "catalog_page",
            "2",
        )
    assert parse_slash_command(" /commands ") is None


def test_slash_parser_is_line_start_only_and_rejects_unknown_tokens() -> None:
    for value in (
        "请执行 /status",
        "前缀\n/commands",
        "说明 /goal 保持目标",
        "/commands\n",
        "/unknown",
        "/rpc thread/delete",
    ):
        assert parse_slash_command(value) is None
    assert parse_slash_command("/commands page 5") is not None
    assert parse_slash_command("/commands page 6") is None


def test_parser_is_typed_and_never_accepts_arbitrary_rpc() -> None:
    assert parse_slash_command("/mcp").kind == "mcp_list"
    assert parse_slash_command("/compact confirm").kind == "compact_start"
    assert parse_slash_command("/model gpt-5.6-sol").argument == "gpt-5.6-sol"
    assert parse_slash_command("/personality pragmatic").argument == "pragmatic"
    assert parse_slash_command("/reasoning xhigh").argument == "xhigh"
    assert parse_slash_command("/memories enabled confirm").kind == "memories_set"
    assert parse_slash_command("/feedback").kind == "unavailable"
    assert parse_slash_command("/pet").kind == "unavailable"
    assert parse_slash_command("/rpc thread/delete") is None
    assert parse_slash_command("/personality evil") is None
    assert parse_slash_command("/model ../../secret") is None


def test_fast_parser_obeys_catalog_execution_gate(monkeypatch) -> None:
    capability = next(
        item for item in OFFICIAL_SLASH_CAPABILITIES if item.command == "/fast"
    )
    assert capability.executable is False

    unavailable = [
        parse_slash_command("/fast"),
        parse_slash_command("/fast confirm"),
        parse_slash_command(slash_card_command(slash_card_action("fast"))),
        parse_slash_command(
            slash_card_command(slash_card_action("fast_confirm"))
        ),
    ]
    assert all(item is not None and item.kind == "unavailable" for item in unavailable)
    assert {(item.argument, item.secondary) for item in unavailable if item is not None} == {
        ("/fast", capability.unavailable_reason)
    }
    disabled_detail = next(
        node["content"]
        for node in _walk(build_slash_catalog_card(1))
        if node.get("tag") == "markdown"
        and str(node.get("content") or "").startswith("**/fast｜")
    )
    assert "可执行：否" in disabled_detail

    # A future verified capability revision must update the directory and the
    # request/confirmation parser together; neither side may get ahead of the
    # other.
    enabled = slash_commands.SlashCapability(
        "/fast", "快速模式", "测试能力", "remote", "/fast", "", True
    )
    monkeypatch.setattr(
        slash_commands,
        "OFFICIAL_SLASH_CAPABILITIES",
        tuple(
            enabled if item.command == "/fast" else item
            for item in OFFICIAL_SLASH_CAPABILITIES
        ),
    )
    monkeypatch.setitem(
        slash_commands._BY_COMMAND,
        "/fast",
        enabled,
    )
    enabled_detail = next(
        node["content"]
        for node in _walk(build_slash_catalog_card(1))
        if node.get("tag") == "markdown"
        and str(node.get("content") or "").startswith("**/fast｜")
    )
    assert "可执行：是" in enabled_detail
    assert parse_slash_command("/fast").kind == "fast_request"
    assert parse_slash_command("/fast confirm").kind == "fast_toggle"


def test_catalog_pages_show_all_commands_and_exact_disabled_reasons() -> None:
    encoded = "\n".join(json.dumps(build_slash_catalog_card(page), ensure_ascii=False) for page in range(1, 6))
    for capability in OFFICIAL_SLASH_CAPABILITIES:
        assert capability.command in encoded
        if capability.disabled_reason:
            assert capability.disabled_reason in encoded
    assert "官方 26 项" in encoded
    assert "用法：" in encoded and "可执行：" in encoded
    assert "threadId" not in encoded and "cwd" not in encoded and "SKILL.md" not in encoded


def test_card_actions_only_return_fixed_commands_without_identity() -> None:
    cards = [build_slash_operations_card("合成会话"), build_confirmation_card("合成会话", "compact", "压缩当前上下文？")]
    for card in cards:
        encoded = json.dumps(card, ensure_ascii=False)
        assert "threadId" not in encoded and "cwd" not in encoded
        assert slash_card_action_fingerprints(card)
    action = slash_card_action("mcp")
    assert slash_card_command(action) == "/mcp"
    assert slash_card_action_fingerprint(action)
    assert slash_card_command({**action, "thread_id": "forged"}) is None


def test_catalog_navigation_round_trips_through_typed_parser() -> None:
    page1 = build_slash_catalog_card(1)
    next_value = next(
        node["value"]
        for node in _walk(page1)
        if node.get("tag") == "button" and node["text"]["content"] == "下一页"
    )
    command = slash_card_command(next_value)
    parsed = parse_slash_command(command)
    assert parsed is not None and (parsed.kind, parsed.argument) == ("catalog_page", "2")
    page2 = build_slash_catalog_card(int(parsed.argument))
    previous_value = next(
        node["value"]
        for node in _walk(page2)
        if node.get("tag") == "button" and node["text"]["content"] == "上一页"
    )
    assert parse_slash_command(slash_card_command(previous_value)).argument == "1"
    assert parse_slash_command("/commands page 0") is None
    assert parse_slash_command("/commands page 01") is None
    assert parse_slash_command("/commands page 2 extra") is None


def test_settings_form_is_strict() -> None:
    assert slash_card_command(
        slash_card_action("settings_form"),
        {"setting_kind": "model", "setting_value": "gpt-5.6-sol"},
    ) == "/model gpt-5.6-sol"
    assert slash_card_command(
        slash_card_action("settings_form"),
        {"setting_kind": "cwd", "setting_value": r"D:\\secret"},
    ) is None
