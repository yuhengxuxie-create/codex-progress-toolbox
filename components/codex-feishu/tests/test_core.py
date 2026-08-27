from __future__ import annotations

from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sqlite3

import pytest

from progress_wx.config import ConfigError, ReloadingConfig, load_config
from progress_wx.formatting import (
    BEIJING_TZ,
    STANDARD_DETAILS_MAX_CHARS,
    format_notification,
)
from progress_wx.models import ProgressReport, ProgressStatus, TurnEvent, structural_report
from progress_wx.retry import RetryExhausted, RetryPolicy, call_with_retry
from progress_wx.state import CorrelationCodec, StateError, StateStore


def test_structural_status_never_reads_message_keywords() -> None:
    event = TurnEvent("thread-1", "turn-1", "completed", final_message="阻塞 完成 待审批")
    assert structural_report(event).status == "*/*"


def test_notification_fields_and_remote_reading_limit() -> None:
    event = TurnEvent("thread-1", "turn-1", "failed", title="示例")
    report = ProgressReport(
        ProgressStatus.BLOCKED,
        "甲" * (STANDARD_DETAILS_MAX_CHARS + 50),
    )
    text = format_notification(
        event,
        report,
        "PCWX-ABCDEFGH-0123456789AB",
        now=datetime(2026, 8, 23, 12, 34, 56, tzinfo=BEIJING_TZ),
    )
    lines = text.split("\n\n")
    assert lines[0] == "对话名称：示例"
    assert lines[1] == "当前进度：阻塞"
    assert STANDARD_DETAILS_MAX_CHARS == 600
    assert len(lines[2]) == STANDARD_DETAILS_MAX_CHARS
    assert "进度详情：" not in text
    assert lines[3] == "本条消息时间：2026-08-23 12:34:56（北京时间）"


def test_unknown_details_are_not_limited() -> None:
    event = TurnEvent("thread-1", "turn-1", "completed")
    text = format_notification(event, ProgressReport("*/*", "乙" * 200), "PCWX-X-Y")
    assert "乙" * 200 in text


def test_custom_status_and_details_are_preserved() -> None:
    event = TurnEvent("thread-1", "turn-1", "completed")
    text = format_notification(event, ProgressReport("等待外部依赖", "丙" * 200))
    assert "当前进度：等待外部依赖" in text
    assert "丙" * 200 in text


def test_multiline_details_preserve_bullet_layout() -> None:
    event = TurnEvent("thread-1", "turn-1", "completed")
    text = format_notification(
        event,
        ProgressReport("*/*", "- 123\n\n* 321\n• 213"),
    )
    assert "\n\n- 123\n- 321\n- 213\n\n" in text
    assert "进度详情：" not in text
    assert "- 123 - 321" not in text


def test_standard_status_keeps_information_rich_summary_over_100_chars() -> None:
    event = TurnEvent("thread-1", "turn-1", "completed")
    details = (
        "本轮完成：\n- 调研并整理飞书机器人菜单和交互卡片能力。\n"
        "关键结果：\n- 菜单事件可绑定固定 command_id。\n"
        "- 复杂参数可通过交互卡片提交。\n"
        "- 精准执行仍需本地命令注册表和结果验证。\n"
        "剩余事项：\n- 尚未接入菜单回调。\n"
        "需要你处理：\n- 无需。"
    )

    text = format_notification(event, ProgressReport("完成", details))

    assert len(details) > 100
    assert details in text
    assert not details.endswith("…")


def test_overlong_custom_status_falls_back_to_unknown() -> None:
    report = ProgressReport("甲" * 21, "详细说明")
    assert report.status == "*/*"


def test_legacy_channel_can_include_visible_reply_code() -> None:
    event = TurnEvent("thread-1", "turn-1", "completed")
    text = format_notification(
        event,
        ProgressReport("*/*", "说明"),
        "PCWX-ABCDEFGH-0123456789AB",
        include_reply_code=True,
    )
    assert text.split("\n\n")[-1] == "回复编号：PCWX-ABCDEFGH-0123456789AB"


def test_signed_code_is_one_time_and_bound_to_thread(tmp_path: Path) -> None:
    codec = CorrelationCodec(b"s" * 32)
    store = StateStore(tmp_path / "state.sqlite")
    event = TurnEvent("thread-1", "turn-1", "completed")
    code = codec.issue()
    message = format_notification(event, structural_report(event), code)
    store.reserve_notification(event, code, message, 72)
    store.mark_sent(event.dedupe_key)
    assert store.consume_reply(code, "message-1", codec, reply_text="继续") == (
        "thread-1",
        "turn-1",
        "turn",
    )
    assert store.consume_reply(code, "message-2", codec, reply_text="继续") is None
    assert store.consume_reply(code[:-1] + "0", "message-3", codec, reply_text="继续") is None
    assert store.pending_turn_replies() == [(code, "thread-1", "继续", "message-1")]
    assert store.claim_turn_reply(code) is True
    assert store.uncertain_turn_replies() == [code]
    assert store.resolve_uncertain_reply(code, delivered=False) is True
    assert store.pending_turn_replies() == [(code, "thread-1", "继续", "message-1")]
    assert store.claim_turn_reply(code) is True
    store.mark_reply_delivered(code)
    assert store.uncertain_turn_replies() == []
    store.close()


def test_stale_pending_reply_discard_is_exact_age_gated_and_clears_body(
    tmp_path: Path,
) -> None:
    codec = CorrelationCodec(b"x" * 32)
    store = StateStore(tmp_path / "state.sqlite")
    event = TurnEvent("thread-1", "turn-stale", "completed")
    code = codec.issue()
    store.reserve_notification(event, code, "通知", 72)
    store.mark_sent(event.dedupe_key)
    assert store.consume_reply(
        code,
        "message-stale",
        codec,
        reply_text="陈旧测试正文",
        now=1_000,
    ) is not None

    with pytest.raises(StateError, match="数量或年龄"):
        store.discard_stale_pending_turn_replies(
            2,
            older_than_seconds=500,
            now=2_000,
        )
    with pytest.raises(StateError, match="数量或年龄"):
        store.discard_stale_pending_turn_replies(
            1,
            older_than_seconds=1_500,
            now=2_000,
        )
    assert store.discard_stale_pending_turn_replies(
        1,
        older_than_seconds=500,
        now=2_000,
    ) == 1
    assert store.pending_turn_replies() == []
    row = store._connection.execute(
        "SELECT discarded_at, reply_text FROM notifications WHERE code=?",
        (code,),
    ).fetchone()
    assert tuple(row) == (2_000, None)
    store.close()


def test_secret_file_is_created_once_and_readable(tmp_path: Path) -> None:
    path = tmp_path / "private" / "hmac.key"
    CorrelationCodec.create_secret_file(path)
    original = path.read_bytes()
    CorrelationCodec.create_secret_file(path)
    assert path.read_bytes() == original
    assert CorrelationCodec.from_file(path).valid(CorrelationCodec.from_file(path).issue())


def test_future_state_schema_is_rejected_without_downgrade(tmp_path: Path) -> None:
    path = tmp_path / "future.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO meta(key, value) VALUES('schema_version', '999')")
    connection.commit()
    connection.close()

    with pytest.raises(StateError, match="拒绝降级"):
        StateStore(path)

    reopened = sqlite3.connect(path)
    try:
        assert reopened.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0] == "999"
    finally:
        reopened.close()


@pytest.mark.parametrize("payload", ["{坏 JSON", "[]"])
def test_corrupt_hook_payload_is_never_silently_consumed(
    tmp_path: Path,
    payload: str,
) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    with store._connection:
        store._connection.execute(
            "INSERT INTO hook_events(event_key, payload_json, created_at) VALUES(?,?,?)",
            ("thread-1:turn-1:completed", payload, 1),
        )

    with pytest.raises(StateError, match="拒绝静默消费"):
        store.pending_hook_payloads()
    row = store._connection.execute(
        "SELECT consumed_at FROM hook_events WHERE event_key=?",
        ("thread-1:turn-1:completed",),
    ).fetchone()
    assert row[0] is None
    store.close()


def test_pre_activation_baseline_is_atomic_and_requires_exact_count(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    for index in range(2):
        assert store.enqueue_hook_payload(
            {
                "type": "agent-turn-complete",
                "thread-id": "thread-1",
                "turn-id": f"turn-{index}",
            }
        )

    assert store.pending_hook_count() == 2
    with pytest.raises(StateError, match="数量在确认期间发生变化"):
        store.baseline_pending_hooks(1)
    assert store.pending_hook_count() == 2

    assert store.baseline_pending_hooks(2) == 2
    assert store.pending_hook_count() == 0
    assert store.pending_hook_payloads() == []
    store.close()


def test_pre_activation_baseline_rejects_negative_count(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    with pytest.raises(ValueError, match="不能为负数"):
        store.baseline_pending_hooks(-1)
    store.close()


def test_old_state_schema_migration_is_serialized(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta(key, value) VALUES('schema_version', '1');
        CREATE TABLE hook_events (
            event_key TEXT PRIMARY KEY, payload_json TEXT NOT NULL,
            created_at INTEGER NOT NULL, consumed_at INTEGER
        );
        CREATE TABLE notifications (
            event_key TEXT PRIMARY KEY, code TEXT NOT NULL UNIQUE,
            thread_id TEXT NOT NULL, turn_id TEXT NOT NULL,
            message_text TEXT NOT NULL, created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL, sent_at INTEGER, consumed_at INTEGER,
            reply_fingerprint TEXT UNIQUE
        );
        CREATE TABLE processed_turns (
            event_key TEXT PRIMARY KEY, processed_at INTEGER NOT NULL
        );
        """
    )
    connection.close()

    def migrate() -> None:
        store = StateStore(path)
        store.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda _index: migrate(), range(2)))

    migrated = sqlite3.connect(path)
    try:
        columns = {row[1] for row in migrated.execute("PRAGMA table_info(notifications)")}
        version = migrated.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0]
        tables = {
            row[0]
            for row in migrated.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        migrated.close()
    assert {
        "reply_kind",
        "reply_text",
        "claimed_at",
        "delivered_at",
        "channel_message_id",
        "discarded_at",
    } <= columns
    assert version == "10"
    assert {
        "management_contexts",
        "management_message_ids",
        "management_inbound_messages",
        "staged_image_replies",
        "monitor_subscriptions",
        "monitor_suppressions",
    } <= tables


def test_staged_image_reply_append_replace_expire_and_clear(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    first = {
        "path": str(tmp_path / "first.png"),
        "mime_type": "image/png",
        "sha256": "a" * 64,
        "size": 123,
    }
    second = {
        "path": str(tmp_path / "second.jpg"),
        "mime_type": "image/jpeg",
        "sha256": "b" * 64,
        "size": 456,
    }
    assert store.stage_image_reply(
        sender_id="ou_owner",
        chat_id="oc_private",
        reply_to_message_id="om_notice",
        source_message_id="om_image_1",
        attachments=(first,),
        ttl_seconds=600,
        now=100,
    ) == (1, False, 700)
    assert store.stage_image_reply(
        sender_id="ou_owner",
        chat_id="oc_private",
        reply_to_message_id="om_notice",
        source_message_id="om_image_2",
        attachments=(second,),
        ttl_seconds=600,
        now=110,
    ) == (2, False, 710)
    staged = store.staged_image_reply("ou_owner", "oc_private", now=120)
    assert staged is not None
    assert staged["reply_to_message_id"] == "om_notice"
    assert staged["attachments"] == (first, second)
    assert staged["source_message_ids"] == ("om_image_1", "om_image_2")

    assert store.stage_image_reply(
        sender_id="ou_owner",
        chat_id="oc_private",
        reply_to_message_id="om_other_task",
        source_message_id="om_image_3",
        attachments=(first,),
        ttl_seconds=600,
        now=130,
    ) == (1, True, 730)
    replaced = store.staged_image_reply("ou_owner", "oc_private", now=140)
    assert replaced is not None
    assert replaced["reply_to_message_id"] == "om_other_task"
    assert replaced["source_message_ids"] == ("om_image_3",)
    assert store.clear_staged_image_reply("ou_owner", "oc_private") is True
    assert store.clear_staged_image_reply("ou_owner", "oc_private") is False

    store.stage_image_reply(
        sender_id="ou_owner",
        chat_id="oc_private",
        reply_to_message_id="om_notice",
        source_message_id="om_image_4",
        attachments=(first,),
        ttl_seconds=10,
        now=200,
    )
    assert store.staged_image_reply("ou_owner", "oc_private", now=211) is None
    store.close()


def test_v7_management_context_expiry_is_migrated_to_permanent(tmp_path: Path) -> None:
    path = tmp_path / "v7.sqlite"
    store = StateStore(path)
    context_id = store.create_management_context(
        "project_list", {"projects": [{"label": "A01"}]}, ttl_days=1, now=100
    )
    store.bind_management_messages(context_id, ("om_old",), now=100)
    store.close()

    connection = sqlite3.connect(path)
    connection.execute("UPDATE meta SET value='7' WHERE key='schema_version'")
    connection.execute(
        "UPDATE management_contexts SET expires_at=200 WHERE context_id=?",
        (context_id,),
    )
    connection.commit()
    connection.close()

    migrated = StateStore(path)
    try:
        assert migrated.management_context_for_message("om_old", now=4_000_000_000) == (
            "project_list",
            {"projects": [{"label": "A01"}]},
        )
        expiry = migrated._connection.execute(
            "SELECT expires_at FROM management_contexts WHERE context_id=?",
            (context_id,),
        ).fetchone()[0]
        assert expiry == 9_223_372_036_854_775_807
    finally:
        migrated.close()


def test_monitor_manual_auto_expiry_and_suppression_lifecycle(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    try:
        assert store.discover_auto_monitor(
            "auto-thread", last_activity_at=1_000, now=1_100, ttl_seconds=300
        ) is True
        assert store.monitor_subscriptions(now=1_100) == [
            {
                "thread_id": "auto-thread",
                "origin": "auto",
                "added_at": 1_100,
                "last_activity_at": 1_000,
                "expires_at": 1_300,
            }
        ]

        store.add_manual_monitor("auto-thread", last_activity_at=1_200, now=1_200)
        manual = store.monitor_subscriptions(now=50_000)
        assert manual[0]["origin"] == "manual"
        assert manual[0]["expires_at"] is None

        assert store.remove_monitor("auto-thread", now=50_001) is True
        assert store.monitor_subscriptions(now=50_001) == []
        assert store.discover_auto_monitor(
            "auto-thread", last_activity_at=50_001, now=50_001
        ) is False
        assert store.ensure_legacy_manual_monitor(
            "auto-thread", last_activity_at=50_001, now=50_001
        ) is False

        store.add_manual_monitor("auto-thread", last_activity_at=50_002, now=50_002)
        restored = store.monitor_subscriptions(now=9_999_999)
        assert restored[0]["origin"] == "manual"
    finally:
        store.close()


def test_expired_auto_monitor_is_pruned_but_manual_monitor_is_permanent(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    try:
        store.discover_auto_monitor(
            "expired-auto", last_activity_at=1_000, now=1_000, ttl_seconds=300
        )
        store.add_manual_monitor("manual", last_activity_at=1_000, now=1_000)
        assert [item["thread_id"] for item in store.monitor_subscriptions(now=1_301)] == [
            "manual"
        ]
    finally:
        store.close()


def test_auto_monitoring_setting_stops_discovery_but_retains_existing_items(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    try:
        assert store.auto_monitoring_settings() == {
            "auto_monitoring_enabled": True,
            "effective_at": None,
        }
        assert store.discover_auto_monitor(
            "existing-auto", last_activity_at=1_000, now=1_000, ttl_seconds=300
        ) is True
        store.add_manual_monitor("manual", last_activity_at=1_000, now=1_000)

        assert store.set_auto_monitoring_enabled(False, now=1_100) == {
            "auto_monitoring_enabled": False,
            "changed": True,
            "effective_at": 1_100,
        }
        assert store.set_auto_monitoring_enabled(False, now=1_200) == {
            "auto_monitoring_enabled": False,
            "changed": False,
            "effective_at": 1_100,
        }
        assert store.discover_auto_monitor(
            "new-auto", last_activity_at=1_200, now=1_200, ttl_seconds=300
        ) is False
        assert store.discover_auto_monitor(
            "existing-auto", last_activity_at=1_200, now=1_200, ttl_seconds=300
        ) is False
        assert store.discover_auto_monitor(
            "manual", last_activity_at=1_250, now=1_250, ttl_seconds=300
        ) is False
        current = {
            item["thread_id"]: item for item in store.monitor_subscriptions(now=1_250)
        }
        assert set(current) == {"existing-auto", "manual"}
        assert current["existing-auto"]["expires_at"] == 1_300
        assert current["manual"]["last_activity_at"] == 1_250
        assert current["manual"]["expires_at"] is None
        assert [
            item["thread_id"] for item in store.monitor_subscriptions(now=1_301)
        ] == ["manual"]

        assert store.set_auto_monitoring_enabled(True, now=1_400) == {
            "auto_monitoring_enabled": True,
            "changed": True,
            "effective_at": 1_400,
        }
        assert store.discover_auto_monitor(
            "new-auto", last_activity_at=1_400, now=1_400, ttl_seconds=300
        ) is True
    finally:
        store.close()


def test_retry_stops_after_exactly_five_attempts() -> None:
    calls: list[int] = []
    sleeps: list[float] = []

    def fail() -> None:
        calls.append(1)
        raise OSError("offline")

    with pytest.raises(RetryExhausted) as captured:
        call_with_retry("微信发送", fail, RetryPolicy(), sleep=sleeps.append)
    assert len(calls) == 5
    assert sleeps == [1, 2, 4, 8]
    assert captured.value.attempts == 5


def test_retry_predicate_stops_permanent_error_after_one_attempt() -> None:
    calls: list[int] = []
    failures: list[int] = []

    def fail() -> None:
        calls.append(1)
        raise PermissionError("permanent")

    with pytest.raises(PermissionError):
        call_with_retry(
            "资源上传",
            fail,
            RetryPolicy(),
            sleep=lambda _delay: pytest.fail("永久错误不应退避重试"),
            on_failure=lambda attempt, _error: failures.append(attempt),
            should_retry=lambda _error: False,
        )
    assert calls == [1]
    assert failures == [1]


def test_nested_retry_does_not_expand_five_attempt_limit() -> None:
    calls: list[int] = []

    def inner() -> None:
        def fail() -> None:
            calls.append(1)
            raise OSError("offline")

        call_with_retry("内层", fail, RetryPolicy(), sleep=lambda _delay: None)

    with pytest.raises(RetryExhausted):
        call_with_retry("外层", inner, RetryPolicy(), sleep=lambda _delay: None)
    assert len(calls) == 5


def test_yaml_config_and_environment_expansion(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
codex:
  home: "${TEST_CODEX_HOME}"
monitor:
  ids: ["thread-1"]
wechat:
  backend: fake
  tool_account_nickname: 通知小号
  tool_wechat_id: wxid-tool
  target_chat: 主号
  target_wechat_id: wxid-main
service:
  max_attempts: 5
  retry_delays: [0, 0, 0, 0, 0]
summary:
  mode: codex_final
""",
        encoding="utf-8",
    )
    config = load_config(config_path, environ={"TEST_CODEX_HOME": str(tmp_path / "codex")})
    config.validate_ready()
    assert config.codex.home == (tmp_path / "codex").resolve()
    assert config.codex.reply_transport == "stdio"
    assert config.codex.reply_timeout_seconds == 86400
    assert config.service.poll_seconds == 2


def test_codex_reply_transport_rejects_unknown_value(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "codex: {reply_transport: desktop_magic}\nmonitor: {ids: [thread-1]}\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="reply_transport"):
        load_config(path)


def test_config_file_change_hot_reloads_monitor_selectors(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"

    def write(thread_id: str) -> None:
        path.write_text(
            f"""
monitor: {{ids: [{thread_id}]}}
wechat:
  backend: fake
  tool_account_nickname: 通知小号
  tool_wechat_id: wxid-tool
  target_chat: 主号
  target_wechat_id: wxid-main
service: {{max_attempts: 5, retry_delays: [0, 0, 0, 0, 0]}}
""",
            encoding="utf-8",
        )

    write("thread-before")
    source = ReloadingConfig(path)
    assert source.get().codex.selectors.ids == ("thread-before",)
    previous_mtime = path.stat().st_mtime_ns
    write("thread-after")
    # 显式推进 mtime，避免极快文件系统写入落在同一时间戳。
    os.utime(path, ns=(previous_mtime + 1_000_000, previous_mtime + 1_000_000))
    assert source.get().codex.selectors.ids == ("thread-after",)


def test_plain_http_summary_is_limited_to_loopback(tmp_path: Path) -> None:
    base = """
monitor: {ids: [thread-1]}
wechat: {backend: fake, tool_account_nickname: 通知小号, tool_wechat_id: wxid-tool, target_chat: 主号, target_wechat_id: wxid-main}
service: {max_attempts: 5, retry_delays: [0, 0, 0, 0, 0]}
summary:
  mode: openai_compatible
  endpoint: http://example.com/v1
  model: local-model
"""
    path = tmp_path / "config.yaml"
    path.write_text(base, encoding="utf-8")
    from progress_wx.config import ConfigError

    with pytest.raises(ConfigError):
        load_config(path)
    path.write_text(base.replace("example.com", "127.0.0.1:11434"), encoding="utf-8")
    assert load_config(path).summary.endpoint == "http://127.0.0.1:11434/v1"


def test_production_requires_quote_and_increasing_retry_delays(tmp_path: Path) -> None:
    from progress_wx.config import ConfigError

    path = tmp_path / "config.yaml"
    base = """
monitor: {ids: [thread-1]}
wechat:
  backend: wxautox4
  tool_account_nickname: 通知小号
  tool_wechat_id: wxid-tool
  target_chat: 主号
  target_wechat_id: wxid-main
  require_quote: false
service: {max_attempts: 5, retry_delays: [1, 2, 4, 8, 16]}
"""
    path.write_text(base, encoding="utf-8")
    config = load_config(path)
    with pytest.raises(ConfigError):
        config.validate_ready()
    path.write_text(
        base.replace("require_quote: false", "require_quote: true").replace(
            "[1, 2, 4, 8, 16]", "[1, 1, 2, 3, 4]"
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_config(path)


def test_probe_only_backend_is_a_valid_fail_closed_configuration(tmp_path: Path) -> None:
    """无商业授权时允许保存真实身份配置，但服务层会保持禁用。"""

    path = tmp_path / "config.yaml"
    path.write_text(
        """
monitor: {ids: [thread-1]}
wechat:
  backend: probe_only
  tool_account_nickname: 通知小号
  tool_wechat_id: wxid-tool
  target_chat: 主号
  target_wechat_id: wxid-main
  require_quote: true
service: {max_attempts: 5, retry_delays: [1, 2, 4, 8, 16]}
""",
        encoding="utf-8",
    )

    config = load_config(path)
    config.validate_ready()
    assert config.wechat.backend == "probe_only"
