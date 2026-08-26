from __future__ import annotations

import sys
import unittest
from pathlib import Path

from tests import _bootstrap  # noqa: F401

from progress_notify.codex_client import CodexAppServerClient


class CodexAppServerClientTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "fake_codex_app_server.py"
        self.command = [sys.executable, str(fixture)]

    def test_reads_exact_thread_name_without_real_codex(self) -> None:
        with CodexAppServerClient(self.command, timeout_seconds=2) as client:
            name = client.get_thread_name("thr_exact")

        self.assertEqual(name, "标题-thr_exact")

    def test_follows_list_cursor(self) -> None:
        with CodexAppServerClient(self.command, timeout_seconds=2) as client:
            threads = client.list_threads(limit=10)

        self.assertEqual([thread["id"] for thread in threads], ["thr_a", "thr_b"])

    def test_passes_explicit_project_source_kinds(self) -> None:
        with CodexAppServerClient(self.command, timeout_seconds=2) as client:
            threads = client.list_threads(limit=10, source_kinds=("appServer",))

        self.assertEqual([thread["id"] for thread in threads], ["thr_project"])
        self.assertEqual(threads[0]["cwd"], "/workspace/project")


if __name__ == "__main__":
    unittest.main()
