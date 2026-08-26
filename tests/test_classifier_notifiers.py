from __future__ import annotations

import base64
import hashlib
import hmac
import json
import unittest
from datetime import datetime, timezone
from uuid import UUID
from unittest.mock import patch

from tests import _bootstrap  # noqa: F401
from tests._http_server import RecordingHTTPServer

from progress_notify.classifier import ProgressClassifier
from progress_notify.config import ClassifierConfig, ConfigError, WebhookConfig
from progress_notify.http_client import JsonHttpClient
from progress_notify.models import AgentTurnComplete, ProgressReport
from progress_notify.notifiers import NotificationError, send_notification


class ResponsesClassifierTests(unittest.TestCase):
    def test_fake_responses_api_returns_structured_semantic_result(self) -> None:
        with RecordingHTTPServer() as server:
            server.queue_json(
                {
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": (
                                        '{"status_kind":"待人工测试",'
                                        '"custom_status":"",'
                                        '"details":"自动检查已通过，需要在真实环境验收。"}'
                                    ),
                                }
                            ],
                        }
                    ],
                }
            )
            client = JsonHttpClient(
                timeout_seconds=2,
                max_attempts=1,
                allow_http_localhost=True,
                initial_backoff_seconds=0,
            )
            classifier = ProgressClassifier(
                ClassifierConfig(
                    mode="openai",
                    api_key="test-api-key",
                    base_url=server.url("/v1"),
                    model="fake-model",
                    timeout_seconds=2,
                ),
                client=client,
            )

            report = classifier.classify(
                AgentTurnComplete(
                    "thr_selected",
                    thread_title="支付回调",
                    last_assistant_message="实现和自动检查完成，请进行真实环境验收。",
                )
            )

        self.assertEqual(report.status, "待人工测试")
        self.assertIn("真实环境", report.details)
        self.assertEqual(len(server.requests), 1)
        request = server.requests[0]
        self.assertEqual(request.path, "/v1/responses")
        self.assertEqual(request.headers.get("Authorization"), "Bearer test-api-key")
        self.assertEqual(request.json["max_output_tokens"], 1024)
        self.assertTrue(request.json["text"]["format"]["strict"])
        self.assertEqual(
            request.json["text"]["format"]["type"], "json_schema"
        )
        schema = request.json["text"]["format"]["schema"]
        self.assertIn("自定义", schema["properties"]["status_kind"]["enum"])
        self.assertEqual(
            set(schema["required"]), {"status_kind", "custom_status", "details"}
        )

    def test_output_limit_retries_once_with_larger_budget(self) -> None:
        with RecordingHTTPServer() as server:
            server.queue_json(
                {
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "output": [{"type": "reasoning", "content": []}],
                }
            )
            server.queue_json(
                {
                    "status": "completed",
                    "output_text": json.dumps(
                        {
                            "status_kind": "完成",
                            "custom_status": "",
                            "details": "扩大输出预算后已生成完整摘要。",
                        },
                        ensure_ascii=False,
                    ),
                }
            )
            classifier = ProgressClassifier(
                ClassifierConfig(
                    mode="openai",
                    api_key="test-api-key",
                    base_url=server.url("/v1"),
                    model="fake-model",
                    timeout_seconds=2,
                ),
                client=JsonHttpClient(
                    timeout_seconds=2,
                    max_attempts=1,
                    allow_http_localhost=True,
                    initial_backoff_seconds=0,
                ),
            )

            report = classifier.classify(
                AgentTurnComplete(
                    "thr_selected",
                    last_assistant_message="任务已经完成。",
                )
            )

        self.assertEqual(report.status, "完成")
        self.assertEqual(len(server.requests), 2)
        self.assertEqual(
            [request.json["max_output_tokens"] for request in server.requests],
            [1024, 2048],
        )

    def test_missing_api_key_returns_unknown_without_http(self) -> None:
        with RecordingHTTPServer() as server:
            classifier = ProgressClassifier(
                ClassifierConfig(
                    mode="auto",
                    api_key="",
                    base_url=server.url("/v1"),
                    model="fake-model",
                ),
                client=JsonHttpClient(
                    timeout_seconds=2,
                    max_attempts=1,
                    allow_http_localhost=True,
                ),
            )
            report = classifier.classify(
                AgentTurnComplete("thr_selected", last_assistant_message="任意文本")
            )

        self.assertEqual(report.status, "情况未知")
        self.assertEqual(report.details, "未配置分类服务，请打开 Codex 查看本轮结果。")
        self.assertEqual(server.requests, [])

    def test_custom_status_and_long_detail_are_bounded(self) -> None:
        with RecordingHTTPServer() as server:
            server.queue_json(
                {
                    "status": "completed",
                    "output_text": json.dumps(
                        {
                            "status_kind": "自定义",
                            "custom_status": "等待外部系统",
                            "details": "外部系统尚未返回结果，当前只能等待后续回调。" * 4,
                        },
                        ensure_ascii=False,
                    ),
                }
            )
            classifier = ProgressClassifier(
                ClassifierConfig(
                    mode="openai",
                    api_key="test-api-key",
                    base_url=server.url("/v1"),
                    model="fake-model",
                    timeout_seconds=2,
                ),
                client=JsonHttpClient(
                    timeout_seconds=2,
                    max_attempts=1,
                    allow_http_localhost=True,
                    initial_backoff_seconds=0,
                ),
            )
            report = classifier.classify(AgentTurnComplete("thr_selected"))

        self.assertEqual(report.status, "等待外部系统")
        self.assertLessEqual(len(report.details), 50)
        self.assertTrue(report.details.endswith("…"))


class LocalWebhookTests(unittest.TestCase):
    NOW = datetime(2026, 8, 20, 13, 8, 35, tzinfo=timezone.utc)

    @staticmethod
    def event() -> AgentTurnComplete:
        return AgentTurnComplete(
            "thr_selected",
            thread_title="支付回调",
            turn_id="turn_456",
        )

    def test_generic_webhook_posts_versioned_event_envelope(self) -> None:
        with RecordingHTTPServer() as server:
            server.queue_json({"accepted": True})
            config = WebhookConfig(
                provider="generic",
                webhook_url=server.url("/generic"),
                allow_http_localhost=True,
                timeout_seconds=2,
                max_attempts=1,
            )
            result = send_notification(
                config,
                self.event(),
                ProgressReport("完成", "实现与自动测试均已完成。"),
                now=self.NOW,
            )

        self.assertTrue(result.ok)
        self.assertEqual(len(server.requests), 1)
        request = server.requests[0]
        self.assertEqual(request.path, "/generic")
        self.assertEqual(
            set(request.json),
            {
                "schema_version",
                "event",
                "event_id",
                "conversation_id",
                "conversation_name",
                "progress",
                "details",
                "sent_at",
                "timezone",
                "text",
            },
        )
        self.assertEqual(request.json["schema_version"], "1.0")
        self.assertEqual(request.json["event"], "codex.turn.completed")
        self.assertEqual(request.json["conversation_id"], "thr_selected")
        self.assertEqual(request.json["conversation_name"], "支付回调")
        self.assertEqual(request.json["progress"], "完成")
        self.assertEqual(request.json["timezone"], "Asia/Shanghai")
        self.assertEqual(request.json["sent_at"], "2026-08-20T21:08:35+08:00")
        self.assertEqual(UUID(request.json["event_id"]).version, 5)
        self.assertEqual(len(request.json["text"].splitlines()), 4)
        self.assertTrue(request.json["text"].startswith("对话名称："))
        self.assertIn("当前进度：完成", request.json["text"])
        self.assertIn("进度详情：实现与自动测试均已完成。", request.json["text"])

    def test_generic_hmac_signs_timestamp_dot_raw_body(self) -> None:
        with RecordingHTTPServer() as server:
            server.queue_json({"accepted": True})
            config = WebhookConfig(
                provider="generic",
                webhook_url=server.url("/signed"),
                auth_type="hmac-sha256",
                hmac_secret="test-secret",
                allow_http_localhost=True,
                timeout_seconds=2,
                max_attempts=1,
            )
            client = JsonHttpClient(
                timeout_seconds=2,
                max_attempts=1,
                allow_http_localhost=True,
                initial_backoff_seconds=0,
                clock=lambda: 1_700_000_000,
            )
            send_notification(
                config,
                self.event(),
                ProgressReport("完成", "已完成。"),
                now=self.NOW,
                client=client,
            )

        request = server.requests[0]
        timestamp = request.headers["X-Progress-Timestamp"]
        expected = hmac.new(
            b"test-secret",
            timestamp.encode("ascii") + b"." + request.body,
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(timestamp, "1700000000")
        self.assertEqual(
            request.headers["X-Progress-Signature"], f"sha256={expected}"
        )

    def test_wecom_webhook_posts_markdown_v2_within_byte_limit(self) -> None:
        with RecordingHTTPServer() as server:
            server.queue_json({"errcode": 0, "errmsg": "ok"})
            config = WebhookConfig(
                provider="wecom",
                webhook_url=server.url("/wecom"),
                allow_http_localhost=True,
                timeout_seconds=2,
                max_attempts=1,
            )
            result = send_notification(
                config,
                self.event(),
                ProgressReport("*/*", "详" * 5000),
                now=self.NOW,
            )

        self.assertTrue(result.ok)
        self.assertEqual(len(server.requests), 1)
        payload = server.requests[0].json
        self.assertEqual(payload["msgtype"], "markdown_v2")
        self.assertEqual(set(payload), {"msgtype", "markdown_v2"})
        content = payload["markdown_v2"]["content"]
        self.assertEqual(len(content.splitlines()), 4)
        self.assertLessEqual(len(content.encode("utf-8")), 4096)
        self.assertIn("当前进度：情况未知", content)
        self.assertNotIn("*/*", content)
        self.assertLessEqual(len(content.splitlines()[2].removeprefix("进度详情：")), 50)
        self.assertNotIn("详" * 100, content)

    @patch("progress_notify.notifiers.time.time", return_value=1_700_000_000)
    def test_feishu_webhook_posts_signed_text_message(self, _clock) -> None:
        with RecordingHTTPServer() as server:
            server.queue_json({"code": 0, "msg": "success"})
            config = WebhookConfig(
                provider="feishu",
                webhook_url=server.url("/feishu"),
                feishu_signing_secret="feishu-test-secret",
                allow_http_localhost=True,
                timeout_seconds=2,
                max_attempts=1,
            )
            result = send_notification(
                config,
                self.event(),
                ProgressReport("完成", "实现与自动测试均已完成。"),
                now=self.NOW,
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.provider, "feishu")
        request = server.requests[0]
        self.assertEqual(request.path, "/feishu")
        self.assertEqual(request.json["msg_type"], "text")
        self.assertEqual(request.json["timestamp"], 1_700_000_000)
        expected = base64.b64encode(
            hmac.new(
                b"1700000000\nfeishu-test-secret",
                digestmod=hashlib.sha256,
            ).digest()
        ).decode("ascii")
        self.assertEqual(request.json["sign"], expected)
        content = request.json["content"]["text"]
        self.assertEqual(len(content.splitlines()), 4)
        self.assertIn("当前进度：完成", content)

    def test_feishu_accepts_legacy_success_response_without_signing(self) -> None:
        with RecordingHTTPServer() as server:
            server.queue_json({"StatusCode": 0, "StatusMessage": "success"})
            config = WebhookConfig(
                provider="lark",
                webhook_url=server.url("/feishu"),
                allow_http_localhost=True,
                timeout_seconds=2,
                max_attempts=1,
            )
            result = send_notification(
                config,
                self.event(),
                ProgressReport("完成", "已完成。"),
                now=self.NOW,
            )

        self.assertEqual(result.provider, "feishu")
        self.assertEqual(
            set(server.requests[0].json), {"msg_type", "content"}
        )

    def test_feishu_rejects_error_without_echoing_remote_message(self) -> None:
        with RecordingHTTPServer() as server:
            server.queue_json({"code": 19021, "msg": "secret upstream diagnostic"})
            config = WebhookConfig(
                provider="feishu",
                webhook_url=server.url("/feishu"),
                allow_http_localhost=True,
                timeout_seconds=2,
                max_attempts=1,
            )
            with self.assertRaisesRegex(
                NotificationError, "Feishu rejected the message"
            ) as raised:
                send_notification(
                    config,
                    self.event(),
                    ProgressReport("完成", "已完成。"),
                    now=self.NOW,
                )

        self.assertNotIn("secret upstream diagnostic", str(raised.exception))

    def test_feishu_rejects_transport_auth_modes(self) -> None:
        with self.assertRaisesRegex(
            ConfigError, "notification.auth_type must be none for feishu"
        ):
            WebhookConfig(
                provider="feishu",
                webhook_url="https://example.invalid/hook",
                auth_type="bearer",
                bearer_token="wrong-layer-token",
            )

    def test_feishu_rejects_nonofficial_production_endpoint(self) -> None:
        with self.assertRaisesRegex(ConfigError, "feishu webhook host"):
            WebhookConfig(
                provider="feishu",
                webhook_url="https://example.invalid/open-apis/bot/v2/hook/test",
            )

    def test_astrbot_posts_bearer_authenticated_four_line_message(self) -> None:
        with RecordingHTTPServer() as server:
            server.queue_json({"status": "ok"})
            config = WebhookConfig(
                provider="astrbot",
                webhook_url=server.url("/api/notify"),
                target_umo="aiocqhttp:FriendMessage:123456",
                auth_type="bearer",
                bearer_token="astrbot-test-token",
                allow_http_localhost=True,
                timeout_seconds=2,
                max_attempts=1,
            )
            result = send_notification(
                config,
                self.event(),
                ProgressReport("完成", "实现与自动测试均已完成。"),
                now=self.NOW,
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.provider, "astrbot")
        self.assertEqual(result.response_json, {"status": "ok"})
        self.assertEqual(len(server.requests), 1)
        request = server.requests[0]
        self.assertEqual(request.path, "/api/notify")
        self.assertEqual(
            request.headers.get("Authorization"), "Bearer astrbot-test-token"
        )
        self.assertEqual(
            set(request.json),
            {"umo", "message"},
        )
        self.assertEqual(
            request.json["umo"], "aiocqhttp:FriendMessage:123456"
        )
        self.assertEqual(len(request.json["message"].splitlines()), 4)
        self.assertTrue(request.json["message"].startswith("对话名称："))
        self.assertIn("当前进度：完成", request.json["message"])
        self.assertIn("进度详情：实现与自动测试均已完成。", request.json["message"])

    def test_astrbot_rejects_non_json_response_safely(self) -> None:
        with RecordingHTTPServer() as server:
            server.queue_text("secret upstream diagnostic")
            config = WebhookConfig(
                provider="astrbot",
                webhook_url=server.url("/api/notify"),
                target_umo="aiocqhttp:FriendMessage:123456",
                auth_type="bearer",
                bearer_token="astrbot-test-token",
                allow_http_localhost=True,
                timeout_seconds=2,
                max_attempts=1,
            )
            with self.assertRaisesRegex(
                NotificationError, "AstrBot returned an invalid JSON response"
            ) as raised:
                send_notification(
                    config,
                    self.event(),
                    ProgressReport("完成", "已完成。"),
                    now=self.NOW,
                )

        self.assertNotIn("secret upstream diagnostic", str(raised.exception))

    def test_astrbot_rejects_error_status_without_echoing_response(self) -> None:
        with RecordingHTTPServer() as server:
            server.queue_json(
                {"status": "error", "message": "secret upstream diagnostic"}
            )
            config = WebhookConfig(
                provider="astrbot",
                webhook_url=server.url("/api/notify"),
                target_umo="aiocqhttp:FriendMessage:123456",
                auth_type="bearer",
                bearer_token="astrbot-test-token",
                allow_http_localhost=True,
                timeout_seconds=2,
                max_attempts=1,
            )
            with self.assertRaisesRegex(
                NotificationError, "AstrBot rejected the message"
            ) as raised:
                send_notification(
                    config,
                    self.event(),
                    ProgressReport("完成", "已完成。"),
                    now=self.NOW,
                )

        self.assertNotIn("secret upstream diagnostic", str(raised.exception))

    def test_astrbot_requires_bearer_auth_and_target_umo(self) -> None:
        with self.assertRaisesRegex(
            ConfigError, "notification.auth_type must be bearer for astrbot"
        ):
            WebhookConfig(
                provider="astrbot",
                webhook_url="https://example.invalid/api/notify",
                target_umo="aiocqhttp:FriendMessage:123456",
            )
        with self.assertRaisesRegex(
            ConfigError, "notification.target_umo is required for astrbot"
        ):
            WebhookConfig(
                provider="astrbot",
                webhook_url="https://example.invalid/api/notify",
                auth_type="bearer",
                bearer_token="astrbot-test-token",
            )

    def test_astrbot_rejects_a_hand_composed_invalid_umo(self) -> None:
        with self.assertRaisesRegex(ConfigError, "copied exactly from AstrBot /sid"):
            WebhookConfig(
                provider="astrbot",
                webhook_url="https://example.invalid/api/notify",
                target_umo="123456",
                auth_type="bearer",
                bearer_token="astrbot-test-token",
            )
        with self.assertRaisesRegex(ConfigError, "unsupported AstrBot message type"):
            WebhookConfig(
                provider="astrbot",
                webhook_url="https://example.invalid/api/notify",
                target_umo="bot:private:123456",
                auth_type="bearer",
                bearer_token="astrbot-test-token",
            )

    def test_astrbot_strips_sid_display_brackets_from_umo(self) -> None:
        config = WebhookConfig(
            provider="astrbot",
            webhook_url="https://example.invalid/api/notify",
            target_umo="「default:FriendMessage:123456」",
            auth_type="bearer",
            bearer_token="astrbot-test-token",
        )

        self.assertEqual(config.target_umo, "default:FriendMessage:123456")

    def test_astrbot_rejects_unmatched_sid_display_brackets(self) -> None:
        with self.assertRaisesRegex(ConfigError, "unmatched AstrBot display brackets"):
            WebhookConfig(
                provider="astrbot",
                webhook_url="https://example.invalid/api/notify",
                target_umo="「default:FriendMessage:123456",
                auth_type="bearer",
                bearer_token="astrbot-test-token",
            )


if __name__ == "__main__":
    unittest.main()
