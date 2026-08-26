from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tests import _bootstrap  # noqa: F401  # Adds src/ to sys.path.

from progress_notify.config import load_config
from progress_notify.formatting import format_notification, normalize_report
from progress_notify.models import (
    AgentTurnComplete,
    EventValidationError,
    ProgressReport,
    parse_agent_turn_complete,
)


class AgentTurnCompleteTests(unittest.TestCase):
    def test_parses_official_hyphenated_fields(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "agent_turn_complete.json"
        payload = json.loads(fixture.read_text(encoding="utf-8"))

        event = parse_agent_turn_complete(payload)

        self.assertEqual(event.thread_id, "thr_selected")
        self.assertEqual(event.turn_id, "turn_456")
        self.assertEqual(event.input_messages, ("请完成实现并验证。",))
        self.assertIn("人工验收", event.last_assistant_message)

    def test_rejects_non_completion_event(self) -> None:
        with self.assertRaises(EventValidationError):
            AgentTurnComplete.from_payload(
                {"type": "approval-requested", "thread-id": "thr_selected"}
            )


class ExactThreadFilterTests(unittest.TestCase):
    def test_example_config_supports_feishu_environment_variables(self) -> None:
        config = load_config(
            Path(__file__).parents[1] / "config.example.json",
            environ={
                "PROGRESS_THREAD_IDS": "thr_selected",
                "PROGRESS_NOTIFY_PROVIDER": "feishu",
                "PROGRESS_WEBHOOK_URL": (
                    "https://open.feishu.cn/open-apis/bot/v2/hook/"
                    "test-hook-id"
                ),
                "PROGRESS_FEISHU_SECRET": "test-signing-secret",
            },
        )
        _bootstrap.close_package_logging()

        self.assertEqual(config.notification.provider, "feishu")
        self.assertEqual(
            config.notification.feishu_signing_secret,
            "test-signing-secret",
        )

    def test_example_config_supports_astrbot_environment_variables(self) -> None:
        config = load_config(
            Path(__file__).parents[1] / "config.example.json",
            environ={
                "PROGRESS_THREAD_IDS": "thr_selected",
                "PROGRESS_NOTIFY_PROVIDER": "astrbot",
                "PROGRESS_WEBHOOK_URL": (
                    "http://127.0.0.1:6185/api/v1/im/message"
                ),
                "PROGRESS_WEBHOOK_AUTH_TYPE": "bearer",
                "PROGRESS_WEBHOOK_BEARER_TOKEN": "test-token",
                "PROGRESS_ASTRBOT_TARGET_UMO": (
                    "test-bot:FriendMessage:test-user"
                ),
                "PROGRESS_ALLOW_HTTP_LOCALHOST": "true",
            },
        )
        _bootstrap.close_package_logging()

        self.assertEqual(config.notification.provider, "astrbot")
        self.assertEqual(config.notification.auth_type, "bearer")
        self.assertEqual(
            config.notification.target_umo,
            "test-bot:FriendMessage:test-user",
        )

    def test_loads_astrbot_target_umo_and_bearer_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.local.json"
            config_path.write_text(
                json.dumps(
                    {
                        "thread_ids": ["thr_selected"],
                        "notification": {
                            "provider": "astrbot",
                            "webhook_url": "https://example.invalid/api/notify",
                            "target_umo": "aiocqhttp:FriendMessage:123456",
                            "auth_type": "bearer",
                            "bearer_token": "test-token",
                        },
                    }
                ),
                encoding="utf-8",
            )
            config = load_config(config_path, environ={})
            _bootstrap.close_package_logging()

        self.assertEqual(config.notification.provider, "astrbot")
        self.assertEqual(
            config.notification.target_umo, "aiocqhttp:FriendMessage:123456"
        )
        self.assertEqual(config.notification.auth_type, "bearer")

    def test_thread_filter_is_exact_and_case_sensitive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.local.json"
            config_path.write_text(
                json.dumps(
                    {
                        "thread_ids": "thr_12,Thread-ABC",
                        "notification": {
                            "provider": "generic",
                            "webhook_url": "https://example.invalid/hook",
                        },
                    }
                ),
                encoding="utf-8",
            )
            config = load_config(config_path, environ={})
            _bootstrap.close_package_logging()

        self.assertTrue(config.matches_thread("thr_12"))
        self.assertTrue(config.matches_thread("Thread-ABC"))
        self.assertFalse(config.matches_thread("thr_123"))
        self.assertFalse(config.matches_thread("thread-abc"))
        self.assertFalse(config.matches_thread("prefix-thr_12"))

    def test_message_text_never_selects_a_thread(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.local.json"
            path.write_text(
                json.dumps(
                    {
                        "thread_ids": ["thr_selected"],
                        "notification": {
                            "provider": "generic",
                            "webhook_url": "https://example.invalid/hook",
                        },
                    }
                ),
                encoding="utf-8",
            )
            config = load_config(path, environ={})
            _bootstrap.close_package_logging()

        event = AgentTurnComplete(
            thread_id="thr_other",
            last_assistant_message="文本里即使出现 thr_selected 也不能命中。",
        )
        self.assertFalse(config.matches_thread(event.thread_id))


class NotificationFormattingTests(unittest.TestCase):
    NOW = datetime(2026, 8, 20, 13, 8, 35, tzinfo=timezone.utc)

    def test_exactly_four_plain_text_lines_in_required_order(self) -> None:
        event = AgentTurnComplete(
            thread_id="thr_selected",
            thread_title="支付\n回调",
        )
        rendered = format_notification(
            event,
            ProgressReport("待人工测试", "自动测试已通过。\r\n请执行人工验收。"),
            now=self.NOW,
        )

        self.assertEqual(
            rendered.splitlines(),
            [
                "对话名称：支付 回调",
                "当前进度：待人工测试",
                "进度详情：自动测试已通过。 请执行人工验收。",
                "本条消息时间：2026-08-20 21:08:35（北京时间）",
            ],
        )
        self.assertNotIn("**", rendered)
        self.assertFalse(any(line.startswith("-") for line in rendered.splitlines()))

    def test_standard_detail_is_limited_to_50_characters(self) -> None:
        details = "私密详情" * 50
        rendered = format_notification(
            AgentTurnComplete("thr_selected", thread_title="长度测试"),
            ProgressReport("完成", details),
            now=self.NOW,
        )
        normalized = normalize_report(ProgressReport("完成", details))

        self.assertEqual(rendered.splitlines()[1], "当前进度：完成")
        self.assertEqual(len(normalized.details), 50)
        self.assertTrue(normalized.details.endswith("…"))
        self.assertEqual(rendered.splitlines()[2], f"进度详情：{normalized.details}")

    def test_detail_boundary_is_exactly_50_characters(self) -> None:
        exact = normalize_report(ProgressReport("完成", "详" * 50))
        overflow = normalize_report(ProgressReport("完成", "详" * 51))

        self.assertEqual(exact.details, "详" * 50)
        self.assertEqual(overflow.details, "详" * 49 + "…")
        self.assertEqual(len(overflow.details), 50)

    def test_custom_status_is_preserved_and_detail_is_limited(self) -> None:
        details = "无法归入六个固定状态；" + "详" * 150
        rendered = format_notification(
            AgentTurnComplete("thr_selected", thread_title="特殊状态"),
            ProgressReport("等待外部系统", details),
            now=self.NOW,
        )
        normalized = normalize_report(ProgressReport("等待外部系统", details))

        self.assertEqual(len(rendered.splitlines()), 4)
        self.assertIn("当前进度：等待外部系统", rendered)
        self.assertNotIn(details, rendered)
        self.assertEqual(len(normalized.details), 50)

    def test_legacy_unknown_marker_becomes_readable_fallback(self) -> None:
        rendered = format_notification(
            AgentTurnComplete("thr_selected", thread_title="未知状态"),
            ProgressReport("*/*", "无法判断当前情况。"),
            now=self.NOW,
        )

        self.assertIn("当前进度：情况未知", rendered)
        self.assertNotIn("*/*", rendered)

    def test_custom_status_cannot_inject_extra_lines_or_markdown(self) -> None:
        rendered = format_notification(
            AgentTurnComplete("thr_selected", thread_title="普通标题"),
            ProgressReport("**等待\n外部依赖**", "需要\r\n等待第三方结果。"),
            now=self.NOW,
        )

        self.assertEqual(len(rendered.splitlines()), 4)
        self.assertIn("当前进度：等待 外部依赖", rendered)
        self.assertIn("进度详情：需要 等待第三方结果。", rendered)
        self.assertNotIn("**", rendered)


if __name__ == "__main__":
    unittest.main()
