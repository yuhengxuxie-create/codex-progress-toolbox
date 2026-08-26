from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests import _bootstrap  # noqa: F401
from tests._http_server import RecordingHTTPServer

from progress_notify.http_client import JsonHttpClient
from progress_notify.runner import handle_event


class FakeAppServerClient:
    def __init__(self, title: str = "App Server 标题") -> None:
        self.title = title
        self.thread_ids: list[str] = []

    def get_thread_name(self, thread_id: str) -> str:
        self.thread_ids.append(thread_id)
        return self.title


class DryRunEndToEndTests(unittest.TestCase):
    def write_config(self, path: Path, server: RecordingHTTPServer) -> None:
        path.write_text(
            json.dumps(
                {
                    "thread_ids": ["thr_selected"],
                    "notification": {
                        "provider": "generic",
                        "webhook_url": server.url("/must-not-send"),
                        "allow_http_localhost": True,
                        "timeout_seconds": 2,
                        "max_attempts": 1,
                    },
                    "classifier": {
                        "mode": "openai",
                        "api_key": "fake-key",
                        "base_url": server.url("/v1"),
                        "model": "fake-model",
                        "timeout_seconds": 2,
                    },
                    "codex": {
                        "command": "never-start-real-codex",
                        "title_overrides": {"thr_selected": "配置兜底标题"},
                        "request_timeout_seconds": 2,
                    },
                    "log_file": ".state/e2e.log",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def test_selected_thread_dry_run_classifies_but_never_posts_webhook(self) -> None:
        with tempfile.TemporaryDirectory() as directory, RecordingHTTPServer() as server:
            config_path = Path(directory) / "config.local.json"
            self.write_config(config_path, server)
            server.queue_json(
                {
                    "status": "completed",
                    "output_text": (
                        '{"status_kind":"完成",'
                        '"custom_status":"",'
                        '"details":"代码、文档与自动化测试均已完成。"}'
                    ),
                }
            )
            http_client = JsonHttpClient(
                timeout_seconds=2,
                max_attempts=1,
                allow_http_localhost=True,
                initial_backoff_seconds=0,
            )
            app_server = FakeAppServerClient("真实对话标题")

            result = handle_event(
                {
                    "type": "agent-turn-complete",
                    "thread-id": "thr_selected",
                    "turn-id": "turn_e2e",
                    "last-assistant-message": "所有实现和验证已经完成。",
                    "input-messages": ["请完成这个工具。"],
                },
                config_path,
                dry_run=True,
                client=http_client,
                app_server_client=app_server,
            )

            paths = [request.path for request in server.requests]
            _bootstrap.close_package_logging()

        self.assertEqual(result.outcome, "dry-run")
        self.assertFalse(result.sent)
        self.assertEqual(result.report.status, "完成")
        self.assertEqual(app_server.thread_ids, ["thr_selected"])
        self.assertIn("对话名称：真实对话标题", result.message)
        self.assertIn("当前进度：完成", result.message)
        self.assertIn("进度详情：代码、文档与自动化测试均已完成。", result.message)
        self.assertEqual(len(result.message.splitlines()), 4)
        self.assertEqual(paths, ["/v1/responses"])
        self.assertNotIn("/must-not-send", paths)

    def test_unselected_thread_stops_before_title_classification_and_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as directory, RecordingHTTPServer() as server:
            config_path = Path(directory) / "config.local.json"
            self.write_config(config_path, server)
            app_server = FakeAppServerClient()

            result = handle_event(
                {
                    "type": "agent-turn-complete",
                    "thread-id": "thr_other",
                    "last-assistant-message": "消息提到 thr_selected 也不能命中。",
                },
                config_path,
                dry_run=True,
                app_server_client=app_server,
            )
            _bootstrap.close_package_logging()

        self.assertEqual(result.outcome, "ignored")
        self.assertEqual(result.reason, "thread-not-allowlisted")
        self.assertEqual(app_server.thread_ids, [])
        self.assertEqual(server.requests, [])


if __name__ == "__main__":
    unittest.main()
