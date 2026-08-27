"""codex_store 的本地临时 SQLite 测试。"""

from __future__ import annotations

import sqlite3
import hashlib
import json
import shutil
import sys
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from progress_wx.codex_store import (  # noqa: E402
    CodexStore,
    CodexStoreReadError,
    StorePaths,
    ThreadStatus,
)


class CodexStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.state = root / "state_5.sqlite"
        self.history = root / "thread_history_1.sqlite"
        self.session_index = root / "session_index.jsonl"
        connection = sqlite3.connect(self.state)
        try:
            connection.executescript(
                """
                CREATE TABLE threads (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    name TEXT,
                    cwd TEXT NOT NULL,
                    updated_at_ms INTEGER,
                    created_at_ms INTEGER,
                    archived INTEGER NOT NULL DEFAULT 0,
                    preview TEXT,
                    source TEXT,
                    thread_source TEXT,
                    rollout_path TEXT
                );
                INSERT INTO threads
                    (id, title, name, cwd, updated_at_ms, created_at_ms, archived)
                VALUES
                    ('thread-a', '支付回调', '', 'D:/repo/a', 3000, 1000, 0),
                    ('thread-b', '其他对话', '', 'D:/repo/b', 2000, 1000, 0),
                    ('thread-c', '有最终答复', '', 'D:/repo/c', 1500, 1000, 0),
                    ('thread-archived', '旧对话', '', 'D:/repo/c', 1000, 900, 1);
                UPDATE threads SET thread_source = 'user' WHERE id = 'thread-a';
                UPDATE threads SET thread_source = 'subagent' WHERE id = 'thread-b';
                """
            )
        finally:
            connection.close()
        connection = sqlite3.connect(self.history)
        try:
            connection.executescript(
                """
                CREATE TABLE thread_turns (
                    thread_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    rollout_ordinal INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    error_json TEXT,
                    started_at INTEGER,
                    completed_at INTEGER,
                    duration_ms INTEGER,
                    final_agent_item_id TEXT
                );
                INSERT INTO thread_turns
                    (thread_id, turn_id, rollout_ordinal, status, started_at, completed_at, final_agent_item_id)
                VALUES
                    ('thread-a', 'turn-completed', 1, 'completed', 100, 200, NULL),
                    ('thread-a', 'turn-in-progress', 2, 'inProgress', 300, NULL, NULL),
                    ('thread-b', 'turn-failed', 1, 'failed', 100, 150, NULL),
                    ('thread-c', 'turn-final', 1, 'completed', 100, 200, 'item-final'),
                    ('thread-archived', 'turn-interrupted', 1, 'interrupted', 100, 120, NULL);

                CREATE TABLE thread_items (
                    thread_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    rollout_ordinal INTEGER,
                    created_at_ms INTEGER,
                    item_json TEXT,
                    item_type TEXT,
                    updated_at_ordinal INTEGER
                );
                INSERT INTO thread_items
                    (thread_id, turn_id, item_id, item_json, item_type)
                VALUES
                    ('thread-c', 'turn-final', 'item-final',
                     '{"type":"agentMessage","id":"item-final","text":"结构化最终答复","phase":"final_answer"}',
                     'agentMessage');
                """
            )
        finally:
            connection.close()
        self.session_index.write_text(
            '{"id":"thread-b","thread_name":"Codex 侧栏短标题"}\n',
            encoding="utf-8",
        )
        self.store = CodexStore(
            paths=StorePaths(self.state, self.history, self.session_index), timeout_seconds=0.2
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_exact_thread_id_title_and_cwd_selection(self) -> None:
        self.assertEqual(
            [item.thread_id for item in self.store.select_threads(thread_id="thread-a")],
            ["thread-a"],
        )
        self.assertEqual(
            [item.thread_id for item in self.store.select_threads(title="支付回调")],
            ["thread-a"],
        )
        self.assertEqual(
            [item.thread_id for item in self.store.select_threads(cwd="D:/repo/a")],
            ["thread-a"],
        )
        # 相似前缀和大小写不同的值不能命中。
        self.assertEqual(self.store.select_threads(thread_id="thread"), [])
        self.assertEqual(self.store.select_threads(thread_id="THREAD-A"), [])
        self.assertEqual(self.store.select_threads(title="支付"), [])
        self.assertEqual(self.store.select_threads(cwd="D:/repo"), [])

    def test_thread_source_is_read_from_the_authoritative_column(self) -> None:
        by_id = {
            item.thread_id: item.thread_source
            for item in self.store.select_threads(include_archived=True)
        }
        self.assertEqual(by_id["thread-a"], "user")
        self.assertEqual(by_id["thread-b"], "subagent")

    def test_session_index_title_matches_codex_sidebar(self) -> None:
        match = self.store.select_threads(thread_id="thread-b")
        self.assertEqual(match[0].title, "Codex 侧栏短标题")

    def test_archived_filter_and_latest_status_are_explicit_fields(self) -> None:
        self.assertEqual(
            [item.thread_id for item in self.store.select_threads()],
            ["thread-a", "thread-b", "thread-c"],
        )
        self.assertEqual(
            [item.thread_id for item in self.store.select_threads(include_archived=True)],
            ["thread-a", "thread-b", "thread-c", "thread-archived"],
        )
        snapshot = self.store.snapshot("thread-a")
        self.assertEqual(snapshot.status, ThreadStatus.IN_PROGRESS)
        self.assertEqual(snapshot.latest_turn.turn_id, "turn-in-progress")
        self.assertEqual(self.store.status("thread-b"), ThreadStatus.FAILED)
        self.assertEqual(
            self.store.snapshot("thread-archived").status,
            ThreadStatus.INTERRUPTED,
        )

    def test_completed_turn_projects_exact_structured_final_answer(self) -> None:
        snapshot = self.store.snapshot("thread-c")
        self.assertEqual(snapshot.status, ThreadStatus.COMPLETED)
        self.assertEqual(snapshot.latest_turn.final_agent_item_id, "item-final")
        self.assertEqual(snapshot.latest_turn.final_message, "结构化最终答复")
        self.assertEqual(snapshot.errors, ())

    def test_completed_turn_projects_only_same_turn_generated_image_original(self) -> None:
        image_dir = Path(self.temp_dir.name) / "generated_images" / "thread-c"
        image_dir.mkdir(parents=True)
        image_path = image_dir / "item-image.png"
        image_bytes = b"\x89PNG\r\n\x1a\n" + b"original-codex-image"
        image_path.write_bytes(image_bytes)
        connection = sqlite3.connect(self.history)
        try:
            connection.execute(
                "INSERT INTO thread_items "
                "(thread_id, turn_id, item_id, rollout_ordinal, item_json, item_type) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "thread-c",
                    "turn-final",
                    "item-image",
                    2,
                    json.dumps(
                        {
                            "type": "imageGeneration",
                            "id": "item-image",
                            "status": "completed",
                            "savedPath": str(image_path),
                        }
                    ),
                    "imageGeneration",
                ),
            )
            connection.commit()
        finally:
            connection.close()

        snapshot = self.store.snapshot("thread-c")
        self.assertEqual(len(snapshot.latest_turn.generated_images), 1)
        artifact = snapshot.latest_turn.generated_images[0]
        self.assertEqual(artifact.item_id, "item-image")
        self.assertEqual(artifact.path, str(image_path.resolve()))
        self.assertEqual(artifact.mime_type, "image/png")
        self.assertEqual(artifact.size, len(image_bytes))
        self.assertEqual(artifact.sha256, hashlib.sha256(image_bytes).hexdigest())

    def test_generated_image_rejects_path_outside_codex_generated_root(self) -> None:
        outside = Path(self.temp_dir.name) / "item-outside.png"
        outside.write_bytes(b"\x89PNG\r\n\x1a\nnot-authorized")
        connection = sqlite3.connect(self.history)
        try:
            connection.execute(
                "INSERT INTO thread_items "
                "(thread_id, turn_id, item_id, rollout_ordinal, item_json, item_type) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "thread-c",
                    "turn-final",
                    "item-outside",
                    2,
                    json.dumps(
                        {
                            "type": "imageGeneration",
                            "id": "item-outside",
                            "status": "completed",
                            "savedPath": str(outside),
                        }
                    ),
                    "imageGeneration",
                ),
            )
            connection.commit()
        finally:
            connection.close()

        snapshot = self.store.snapshot("thread-c")
        self.assertEqual(snapshot.latest_turn.generated_images, ())

    def test_legacy_message_final_answer_remains_compatible(self) -> None:
        connection = sqlite3.connect(self.history)
        try:
            connection.execute(
                "INSERT INTO thread_turns "
                "(thread_id, turn_id, rollout_ordinal, status, started_at, completed_at, final_agent_item_id) "
                "VALUES (?, ?, ?, 'completed', ?, ?, ?)",
                ("thread-legacy", "turn-legacy", 10, 100, 200, "item-legacy"),
            )
            connection.execute(
                "INSERT INTO thread_items "
                "(thread_id, turn_id, item_id, item_json, item_type) VALUES (?, ?, ?, ?, ?)",
                (
                    "thread-legacy",
                    "turn-legacy",
                    "item-legacy",
                    '{"type":"message","role":"assistant","phase":"final_answer","text":"旧格式最终答复"}',
                    "agentMessage",
                ),
            )
            connection.commit()
        finally:
            connection.close()

        snapshot = self.store.snapshot("thread-legacy")
        self.assertEqual(snapshot.latest_turn.final_message, "旧格式最终答复")
        self.assertEqual(snapshot.errors, ())

    def test_old_thread_falls_back_to_incremental_structured_rollout(self) -> None:
        sessions = Path(self.temp_dir.name) / "sessions" / "2026" / "08" / "20"
        sessions.mkdir(parents=True)
        rollout = sessions / "rollout-old-thread.jsonl"
        rollout.write_text(
            '{"timestamp":"2026-08-25T05:00:00Z","type":"event_msg",'
            '"payload":{"type":"task_complete","turn_id":"turn-rollout-1",'
            '"last_agent_message":"自动续跑最终答复","started_at":100,'
            '"completed_at":200,"duration_ms":100000}}\n',
            encoding="utf-8",
        )
        connection = sqlite3.connect(self.state)
        try:
            connection.execute(
                "INSERT INTO threads "
                "(id, title, name, cwd, updated_at_ms, created_at_ms, archived, rollout_path) "
                "VALUES (?, ?, '', ?, ?, ?, 0, ?)",
                (
                    "thread-rollout",
                    "旧自动任务",
                    "D:/repo/old",
                    200000,
                    100000,
                    str(rollout),
                ),
            )
            connection.commit()
        finally:
            connection.close()

        first = self.store.snapshot("thread-rollout")
        self.assertEqual(first.status, ThreadStatus.COMPLETED)
        self.assertEqual(first.latest_turn.turn_id, "turn-rollout-1")
        self.assertEqual(first.latest_turn.final_message, "自动续跑最终答复")
        self.assertEqual(first.latest_turn.raw["source"], "codex-rollout")

        with rollout.open("a", encoding="utf-8") as handle:
            handle.write(
                '{"timestamp":"2026-08-25T05:01:00Z","type":"event_msg",'
                '"payload":{"type":"turn_aborted","turn_id":"turn-rollout-2",'
                '"started_at":300,"completed_at":400,"duration_ms":100000}}\n'
            )
        second = self.store.snapshot("thread-rollout")
        self.assertEqual(second.status, ThreadStatus.INTERRUPTED)
        self.assertEqual(second.latest_turn.turn_id, "turn-rollout-2")
        self.assertEqual(second.latest_turn.final_message, "")

    def test_rollout_fallback_rejects_paths_outside_codex_sessions(self) -> None:
        outside = Path(self.temp_dir.name).parent / "outside-rollout.jsonl"
        outside.write_text(
            '{"type":"event_msg","payload":{"type":"task_complete",'
            '"turn_id":"outside","completed_at":999}}\n',
            encoding="utf-8",
        )
        self.addCleanup(outside.unlink, missing_ok=True)
        connection = sqlite3.connect(self.state)
        try:
            connection.execute(
                "INSERT INTO threads "
                "(id, title, name, cwd, updated_at_ms, created_at_ms, archived, rollout_path) "
                "VALUES ('thread-outside', '越界', '', 'D:/repo', 999000, 1, 0, ?)",
                (str(outside),),
            )
            connection.commit()
        finally:
            connection.close()
        snapshot = self.store.snapshot("thread-outside")
        self.assertEqual(snapshot.status, ThreadStatus.UNKNOWN)
        self.assertIsNone(snapshot.latest_turn)
        self.assertEqual(snapshot.errors, ())

    def test_in_progress_turn_never_projects_final_answer_pointer(self) -> None:
        connection = sqlite3.connect(self.history)
        try:
            connection.execute(
                "INSERT INTO thread_turns "
                "(thread_id, turn_id, rollout_ordinal, status, started_at, final_agent_item_id) "
                "VALUES (?, ?, ?, 'inProgress', ?, ?)",
                ("thread-in-progress-pointer", "turn-live", 999, 999, "item-live"),
            )
            connection.execute(
                "INSERT INTO thread_items "
                "(thread_id, turn_id, item_id, item_json, item_type) VALUES (?, ?, ?, ?, ?)",
                (
                    "thread-in-progress-pointer",
                    "turn-live",
                    "item-live",
                    '{"type":"message","role":"assistant","phase":"final_answer","text":"不应提前投影"}',
                    "agentMessage",
                ),
            )
            connection.commit()
        finally:
            connection.close()

        snapshot = self.store.snapshot("thread-in-progress-pointer")
        self.assertEqual(snapshot.status, ThreadStatus.IN_PROGRESS)
        self.assertEqual(snapshot.latest_turn.final_message, "")
        self.assertEqual(snapshot.errors, ())

    def test_final_answer_projection_rejects_non_strict_item_shapes(self) -> None:
        connection = sqlite3.connect(self.history)
        try:
            invalid_rows = (
                ("thread-invalid-type", "item-1", "toolResult", '{"type":"message","role":"assistant","phase":"final_answer","text":"不应使用"}'),
                ("thread-invalid-type-field", "item-2", "agentMessage", '{"type":"not-message","role":"assistant","phase":"final_answer","text":"不应使用"}'),
                ("thread-invalid-role", "item-3", "agentMessage", '{"type":"message","role":"user","phase":"final_answer","text":"不应使用"}'),
                ("thread-invalid-phase", "item-4", "agentMessage", '{"type":"message","role":"assistant","phase":"commentary","text":"不应使用"}'),
                ("thread-invalid-text", "item-5", "agentMessage", '{"type":"message","role":"assistant","phase":"final_answer","text":"   "}'),
                ("thread-invalid-embedded-id", "item-6", "agentMessage", '{"type":"agentMessage","id":"other-item","phase":"final_answer","text":"不应使用"}'),
                ("thread-invalid-role-shape", "item-7", "agentMessage", '{"type":"agentMessage","role":[],"phase":"final_answer","text":"不应使用"}'),
            )
            for index, (thread_id, item_id, item_type, item_json) in enumerate(invalid_rows, start=1):
                connection.execute(
                    "INSERT INTO thread_turns "
                    "(thread_id, turn_id, rollout_ordinal, status, started_at, completed_at, final_agent_item_id) "
                    "VALUES (?, ?, ?, 'completed', ?, ?, ?)",
                    (thread_id, f"turn-{index}", 100 + index, 100 + index, 200 + index, item_id),
                )
                connection.execute(
                    "INSERT INTO thread_items "
                    "(thread_id, turn_id, item_id, item_json, item_type) VALUES (?, ?, ?, ?, ?)",
                    (thread_id, f"turn-{index}", item_id, item_json, item_type),
                )
            connection.commit()
        finally:
            connection.close()

        invalid_suffixes = (
            "type",
            "type-field",
            "role",
            "phase",
            "text",
            "embedded-id",
            "role-shape",
        )
        for suffix in invalid_suffixes:
            snapshot = self.store.snapshot(f"thread-invalid-{suffix}")
            self.assertEqual(snapshot.status, ThreadStatus.COMPLETED)
            self.assertEqual(snapshot.latest_turn.final_message, "")
            self.assertEqual(snapshot.errors, ())

    def test_missing_final_item_table_or_columns_are_compatible_empty(self) -> None:
        missing_table = Path(self.temp_dir.name) / "missing-items.sqlite"
        connection = sqlite3.connect(missing_table)
        try:
            connection.execute(
                "CREATE TABLE thread_turns ("
                "thread_id TEXT, turn_id TEXT, status TEXT, started_at INTEGER, "
                "completed_at INTEGER, final_agent_item_id TEXT)"
            )
            connection.execute(
                "INSERT INTO thread_turns VALUES (?, ?, 'completed', 1, 2, ?)",
                ("thread-c", "turn-missing-table", "item-missing"),
            )
            connection.commit()
        finally:
            connection.close()
        store = CodexStore(state_db=self.state, history_db=missing_table)
        snapshot = store.snapshot("thread-c")
        self.assertEqual(snapshot.status, ThreadStatus.COMPLETED)
        self.assertEqual(snapshot.latest_turn.final_message, "")
        self.assertEqual(snapshot.errors, ())

        missing_column = Path(self.temp_dir.name) / "missing-item-json.sqlite"
        connection = sqlite3.connect(missing_column)
        try:
            connection.executescript(
                """
                CREATE TABLE thread_turns (
                    thread_id TEXT, turn_id TEXT, status TEXT,
                    started_at INTEGER, completed_at INTEGER, final_agent_item_id TEXT
                );
                CREATE TABLE thread_items (
                    thread_id TEXT, turn_id TEXT, item_id TEXT, item_type TEXT
                );
                """
            )
            connection.execute(
                "INSERT INTO thread_turns VALUES (?, ?, 'completed', 1, 2, ?)",
                ("thread-c", "turn-missing-column", "item-missing"),
            )
            connection.commit()
        finally:
            connection.close()
        store = CodexStore(state_db=self.state, history_db=missing_column)
        snapshot = store.snapshot("thread-c")
        self.assertEqual(snapshot.status, ThreadStatus.COMPLETED)
        self.assertEqual(snapshot.latest_turn.final_message, "")
        self.assertEqual(snapshot.errors, ())

    def test_unknown_status_is_not_inferred_from_error_text(self) -> None:
        connection = sqlite3.connect(self.history)
        try:
            connection.execute(
                "INSERT INTO thread_turns "
                "(thread_id, turn_id, rollout_ordinal, status, error_json, started_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "thread-b",
                    "turn-unknown",
                    2,
                    "futureStatus",
                    '{"message":"failed but this is not the status field"}',
                    400,
                ),
            )
            connection.commit()
        finally:
            connection.close()
        snapshot = self.store.snapshot("thread-b")
        self.assertEqual(snapshot.status, ThreadStatus.UNKNOWN)
        self.assertEqual(snapshot.latest_turn.turn_id, "turn-unknown")

    def test_missing_or_invalid_databases_fail_closed(self) -> None:
        missing = CodexStore(
            state_db=Path(self.temp_dir.name) / "missing.sqlite",
            history_db=Path(self.temp_dir.name) / "missing-history.sqlite",
        )
        self.assertEqual(missing.select_threads(), [])
        self.assertEqual(missing.snapshot("thread-a").status, ThreadStatus.UNKNOWN)
        self.assertTrue(missing.last_errors)

        invalid = Path(self.temp_dir.name) / "invalid.sqlite"
        invalid.write_text("not a sqlite database", encoding="utf-8")
        invalid_store = CodexStore(state_db=invalid, history_db=invalid)
        self.assertEqual(invalid_store.select_threads(), [])
        self.assertEqual(invalid_store.snapshot("thread-a").status, ThreadStatus.UNKNOWN)

    def test_read_errors_are_distinct_from_healthy_unknown_or_missing_thread(self) -> None:
        # 两个数据库可读，但选择器指向不存在的 thread：这是合法空状态，不应
        # 被误判为数据库异常。
        missing_thread = self.store.snapshot("thread-does-not-exist")
        self.assertEqual(missing_thread.status, ThreadStatus.UNKNOWN)
        self.assertEqual(missing_thread.errors, ())
        self.assertTrue(missing_thread.readable)
        self.assertIs(missing_thread.require_readable(), missing_thread)

        broken = CodexStore(
            state_db=Path(self.temp_dir.name) / "missing-state.sqlite",
            history_db=Path(self.temp_dir.name) / "missing-history.sqlite",
        )
        broken_snapshot = broken.snapshot("thread-a")
        self.assertEqual(broken_snapshot.status, ThreadStatus.UNKNOWN)
        self.assertFalse(broken_snapshot.readable)
        with self.assertRaises(CodexStoreReadError) as raised:
            broken_snapshot.require_readable()
        self.assertEqual(raised.exception.errors, ("state:missing", "history:missing"))

        with self.assertRaises(CodexStoreReadError):
            broken.status("thread-a")

    def test_read_only_uri_does_not_create_missing_files(self) -> None:
        state = Path(self.temp_dir.name) / "not-created-state.sqlite"
        history = Path(self.temp_dir.name) / "not-created-history.sqlite"
        CodexStore(state_db=state, history_db=history).snapshot("x")
        self.assertFalse(state.exists())
        self.assertFalse(history.exists())

    def test_read_only_uri_escapes_hash_percent_and_spaces(self) -> None:
        special = Path(self.temp_dir.name) / "Codex #100% data"
        special.mkdir()
        state = special / "state_5.sqlite"
        history = special / "thread_history_1.sqlite"
        shutil.copy2(self.state, state)
        shutil.copy2(self.history, history)
        store = CodexStore(state_db=state, history_db=history)
        self.assertEqual(store.status("thread-b"), ThreadStatus.FAILED)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
