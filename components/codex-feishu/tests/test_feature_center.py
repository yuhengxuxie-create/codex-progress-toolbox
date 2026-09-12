from __future__ import annotations

import json

import pytest

from progress_wx.feature_center import (
    DEFAULT_FEATURE_SECTIONS,
    FEATURE_CENTER_CARD_NAMESPACE,
    FEATURE_CENTER_CARD_VERSION,
    FeatureEntry,
    FeatureSection,
    build_feature_center_card,
    build_monitor_settings_card,
    feature_center_action,
    feature_center_action_command,
    feature_center_action_fingerprint,
    feature_center_card_action_fingerprints,
    feature_center_direct_commands,
    feature_center_text_fallback,
    feature_operation_action,
    feature_operation_action_command,
    feature_operation_action_fingerprint,
    feature_operation_card_action_fingerprints,
    parse_feature_operation_action,
    parse_feature_center_action,
)


def _walk(node: object):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def test_default_center_has_four_user_facing_sections_and_stable_actions() -> None:
    assert [section.key for section in DEFAULT_FEATURE_SECTIONS] == [
        "sessions",
        "monitoring",
        "codex",
        "account_help",
    ]
    actions = [
        entry.action
        for section in DEFAULT_FEATURE_SECTIONS
        for entry in section.entries
    ]
    assert len(actions) == len(set(actions))
    assert {entry.label for section in DEFAULT_FEATURE_SECTIONS for entry in section.entries} >= {
        "查询会话",
        "搜索会话",
        "查看监测",
        "Skills",
        "斜杠指令",
        "剩余额度",
        "使用说明",
    }


def test_card_contains_only_safe_action_values_and_no_target_identity() -> None:
    card = build_feature_center_card()
    assert card["schema"] == "2.0"
    assert card["config"] == {"update_multi": True}
    buttons = [node for node in _walk(card) if node.get("tag") == "button"]
    assert len(buttons) >= 4
    for button in buttons:
        value = button["value"]
        assert set(value) == {"namespace", "version", "action"}
        assert value["namespace"] == FEATURE_CENTER_CARD_NAMESPACE
        assert value["version"] == FEATURE_CENTER_CARD_VERSION
        if button.get("disabled") is True:
            assert parse_feature_center_action(value) is None
        else:
            assert parse_feature_center_action(value) == value["action"]
        assert "thread" not in json.dumps(value).lower()
        assert "cwd" not in json.dumps(value).lower()
        assert "path" not in json.dumps(value).lower()
    encoded = json.dumps(card, ensure_ascii=False)
    assert "thread_id" not in encoded
    assert "工作目录" not in encoded
    assert "SKILL.md" not in encoded
    assert len(feature_center_card_action_fingerprints(card)) == len(
        [button for button in buttons if button.get("disabled") is not True]
    )


def test_text_fallback_explains_all_sections_and_safe_commands() -> None:
    text = feature_center_text_fallback()
    for title in ("会话", "进度监测", "指令使用", "账户与帮助"):
        assert f"【{title}】" in text
    for command in ("查询会话", "搜索会话", "查询监测列表", "/skills", "查询剩余额度", "使用说明"):
        assert command in text
    for alias in ("查看指令列表", "斜杠指令", "/commands"):
        assert alias in text
    assert "thread_id" not in text
    assert "SKILL.md" not in text


def test_action_parser_is_exact_and_rejects_forged_or_unknown_values() -> None:
    action = feature_center_action("session_query")
    assert parse_feature_center_action(action) == "session_query"
    assert feature_center_action_command(action) == "查询会话"
    assert feature_center_action_fingerprint(action)
    assert parse_feature_center_action({**action, "thread_id": "forged"}) is None
    assert parse_feature_center_action({**action, "action": "unknown"}) is None
    assert parse_feature_center_action({**action, "version": "1"}) is None
    assert parse_feature_center_action({"namespace": FEATURE_CENTER_CARD_NAMESPACE}) is None
    assert feature_center_action_fingerprint({**action, "cwd": r"D:\secret"}) is None
    with pytest.raises(ValueError, match="未知"):
        feature_center_action("unknown")


def test_unavailable_entry_is_visible_with_reason_but_keeps_safe_action() -> None:
    entry = FeatureEntry(
        "future_feature",
        "未来功能",
        "等待官方能力接入。",
        "未来功能",
        enabled=False,
        disabled_reason="当前桥接端没有该官方接口",
    )
    sections = (
        FeatureSection("one", "测试分区", "合成能力状态。", (entry,)),
    )
    card = build_feature_center_card(sections)
    encoded = json.dumps(card, ensure_ascii=False)
    assert "未来功能（暂不可用）" in encoded
    assert "当前桥接端没有该官方接口" in encoded
    button = next(node for node in _walk(card) if node.get("tag") == "button")
    assert button["disabled"] is True
    entry_value = button["value"]
    assert parse_feature_center_action(entry_value, sections=sections) is None
    assert feature_center_action_fingerprint(entry_value, sections=sections) is None
    assert feature_center_text_fallback(sections).endswith(
        "未来功能：暂不可用（当前桥接端没有该官方接口）"
    )


def test_default_fallbacks_match_real_top_level_commands() -> None:
    from progress_wx.codex_management import LEGACY_TEXT_COMMANDS, TOP_LEVEL_COMMANDS

    enabled = {
        entry.fallback
        for section in DEFAULT_FEATURE_SECTIONS
        for entry in section.entries
        if entry.enabled
    }
    # Internal canonical values still serve existing buttons/callbacks.  Test
    # the actual rendered direct instructions separately, without rewriting
    # message inputs inside a helper.
    scoped = {"/skills"}
    assert enabled - scoped <= TOP_LEVEL_COMMANDS | LEGACY_TEXT_COMMANDS
    rendered = {
        line[2:].split("：", 1)[0]
        for line in feature_center_text_fallback().splitlines()
        if line.startswith("- ") and "暂不可用" not in line
    }
    assert len(rendered) == len(enabled)
    assert rendered - scoped <= TOP_LEVEL_COMMANDS
    assert "新建项目" in enabled
    assert "新建项目会话" not in enabled
    assert "/project" in enabled
    assert any(
        entry.action == "monitor_settings" and entry.enabled
        for section in DEFAULT_FEATURE_SECTIONS
        for entry in section.entries
    )
    entries = {
        entry.action: entry
        for section in DEFAULT_FEATURE_SECTIONS
        for entry in section.entries
    }
    assert entries["project_create"].fallback == "新建项目"
    assert entries["project_task_create"].fallback == "/project"
    assert entries["session_query"].fallback == "查询会话"
    assert set(entry.action for entry in DEFAULT_FEATURE_SECTIONS[2].entries) == {
        "codex_skills",
        "codex_slash",
    }
    direct = feature_center_direct_commands()
    assert direct["查询会话"] == "查询会话"
    assert direct["项目会话"] == "查询项目列表"
    assert direct["Skills"] == "/skills"
    assert "Goal" not in direct and "Plan 模式" not in direct


def test_monitor_settings_card_uses_beijing_time_and_strict_two_step_actions() -> None:
    card = build_monitor_settings_card(True, 1_788_000_000)
    encoded = json.dumps(card, ensure_ascii=False)
    assert "1788000000" not in encoded
    assert "2026-08-" in encoded
    assert "关闭自动监测" in encoded
    assert len(feature_operation_card_action_fingerprints(card)) == 2
    request = feature_operation_action("monitor_disable_request")
    assert parse_feature_operation_action(request) == "monitor_disable_request"
    assert feature_operation_action_command(request) == "关闭自动监测"
    assert feature_operation_action_fingerprint(request)
    assert parse_feature_operation_action({**request, "desired": False}) is None

    confirm = build_monitor_settings_card(True, None, confirm=False)
    confirm_encoded = json.dumps(confirm, ensure_ascii=False)
    assert "暂无变更记录" in confirm_encoded
    assert feature_operation_action_command(
        feature_operation_action("monitor_disable_confirm")
    ) == "确认关闭自动监测"
    assert len(feature_operation_card_action_fingerprints(confirm)) == 1


def test_duplicate_actions_and_invalid_entries_fail_closed() -> None:
    first = FeatureEntry("same", "一", "一", "一")
    second = FeatureEntry("same", "二", "二", "二")
    with pytest.raises(ValueError, match="重复"):
        FeatureSection("dup", "重复", "重复", (first, second))
    with pytest.raises(ValueError, match="tuple"):
        FeatureSection("bad", "坏", "坏", [first])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="不可用"):
        FeatureEntry("off", "不可用", "说明", "不可用", enabled=False)
    with pytest.raises(ValueError, match="稳定"):
        FeatureEntry("bad/key", "坏", "说明", "坏")


def test_custom_sections_keep_actions_scoped_and_fingerprints_deterministic() -> None:
    one = FeatureEntry("one", "一", "第一项", "一")
    two = FeatureEntry("two", "二", "第二项", "二")
    sections = (FeatureSection("custom", "自定义", "说明", (one, two)),)
    card = build_feature_center_card(sections, title="自定义功能中心", subtitle="合成")
    values = [node["value"] for node in _walk(card) if node.get("tag") == "button"]
    assert [parse_feature_center_action(value, sections=sections) for value in values] == [
        "one",
        "two",
    ]
    assert parse_feature_center_action(values[0]) is None
    assert feature_center_card_action_fingerprints(card, sections=sections) == tuple(
        feature_center_action_fingerprint(value, sections=sections) for value in values
    )
