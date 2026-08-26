from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import _bootstrap  # noqa: F401

from progress_notify.codex_index import read_indexed_thread_name


class CodexThreadIndexTests(unittest.TestCase):
    def test_reads_latest_exact_id_and_ignores_malformed_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            index_path = Path(directory) / "session_index.jsonl"
            rows = [
                json.dumps(
                    {"id": "thr_exact", "thread_name": "旧标题"},
                    ensure_ascii=False,
                ),
                "{not-json",
                json.dumps(
                    {"id": "thr_exact-extra", "thread_name": "相似 ID 标题"},
                    ensure_ascii=False,
                ),
                json.dumps(
                    {"id": "THR_EXACT", "thread_name": "不同大小写标题"},
                    ensure_ascii=False,
                ),
                json.dumps(
                    {"id": "thr_exact", "thread_name": " 新标题 "},
                    ensure_ascii=False,
                ),
                '{"id":"thr_exact"',
            ]
            index_path.write_text("\n".join(rows), encoding="utf-8")

            with patch.dict(os.environ, {"CODEX_HOME": directory}):
                name = read_indexed_thread_name("thr_exact")

        self.assertEqual(name, "新标题")

    def test_invalid_or_blank_records_preserve_the_last_valid_title(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            index_path = Path(directory) / "session_index.jsonl"
            index_path.write_text(
                "\n".join(
                    [
                        json.dumps({"id": "thr_exact", "thread_name": "旧标题"}),
                        json.dumps({"id": "thr_exact", "thread_name": "   "}),
                        json.dumps({"id": "thr_exact", "thread_name": 42}),
                        json.dumps({"id": "thr_exact"}),
                    ]
                ),
                encoding="utf-8",
            )

            name = read_indexed_thread_name("thr_exact", directory)

        self.assertEqual(name, "旧标题")

    def test_missing_index_or_unknown_id_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(read_indexed_thread_name("thr_missing", directory))

            index_path = Path(directory) / "session_index.jsonl"
            index_path.write_text(
                json.dumps({"id": "thr_other", "thread_name": "其他标题"}),
                encoding="utf-8",
            )
            self.assertIsNone(read_indexed_thread_name("thr_missing", directory))

    def test_rejects_empty_thread_id(self) -> None:
        with self.assertRaises(ValueError):
            read_indexed_thread_name("")


if __name__ == "__main__":
    unittest.main()
