from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import _bootstrap

from progress_notify.config import AppConfig, load_config
from progress_notify.models import AgentTurnComplete
from progress_notify.runner import _resolve_title


class FailingAppServerClient:
    def get_thread_name(self, _thread_id: str) -> str:
        raise PermissionError("simulated packaged-app access denial")


class WorkingAppServerClient:
    def get_thread_name(self, _thread_id: str) -> str:
        return "App Server 标题"


class TitleResolutionTests(unittest.TestCase):
    @staticmethod
    def _config(path: Path, title_overrides: dict[str, str]) -> AppConfig:
        _bootstrap.write_config(
            path,
            codex={
                "command": "never-start-real-codex",
                "title_overrides": title_overrides,
                "request_timeout_seconds": 2,
            },
        )
        return load_config(path)

    def test_app_server_precedes_every_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(
                Path(directory) / "config.json",
                {"thr_selected": "配置覆盖标题"},
            )
            event = AgentTurnComplete("thr_selected", thread_title="事件标题")
            with patch(
                "progress_notify.runner.read_indexed_thread_name"
            ) as read_index:
                resolved = _resolve_title(event, config, WorkingAppServerClient())

        self.assertEqual(resolved.thread_title, "App Server 标题")
        read_index.assert_not_called()

    def test_local_index_is_used_when_app_server_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(Path(directory) / "config.json", {})
            event = AgentTurnComplete("thr_selected")
            with patch(
                "progress_notify.runner.read_indexed_thread_name",
                return_value="本地索引标题",
            ) as read_index:
                resolved = _resolve_title(event, config, FailingAppServerClient())

        self.assertEqual(resolved.thread_title, "本地索引标题")
        read_index.assert_called_once_with("thr_selected")

    def test_override_precedes_event_and_local_index(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(
                Path(directory) / "config.json",
                {"thr_selected": "配置覆盖标题"},
            )
            event = AgentTurnComplete("thr_selected", thread_title="事件标题")
            with patch(
                "progress_notify.runner.read_indexed_thread_name"
            ) as read_index:
                resolved = _resolve_title(event, config, FailingAppServerClient())

        self.assertEqual(resolved.thread_title, "配置覆盖标题")
        read_index.assert_not_called()

    def test_event_title_precedes_local_index(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(Path(directory) / "config.json", {})
            event = AgentTurnComplete("thr_selected", thread_title="事件标题")
            with patch(
                "progress_notify.runner.read_indexed_thread_name"
            ) as read_index:
                resolved = _resolve_title(event, config, FailingAppServerClient())

        self.assertEqual(resolved.thread_title, "事件标题")
        read_index.assert_not_called()

    def test_id_fallback_remains_when_every_title_source_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(Path(directory) / "config.json", {})
            event = AgentTurnComplete("thr_selected")
            with patch(
                "progress_notify.runner.read_indexed_thread_name",
                return_value=None,
            ):
                resolved = _resolve_title(event, config, FailingAppServerClient())

        self.assertEqual(resolved.thread_title, "未命名对话（thr_selected）")


if __name__ == "__main__":
    unittest.main()
