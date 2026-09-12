"""codex_store 的本地临时 SQLite 测试。"""

from __future__ import annotations

import sqlite3
import hashlib
import json
import shutil
import sys
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
from typing import Any
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from progress_wx.codex_store import (  # noqa: E402
    CodexStore,
    CodexStoreReadError,
    StorePaths,
    ThreadRecord,
    ThreadStatus,
    TurnRecord,
    _resolve_thread_title,
    independent_thread_title,
    public_thread_title,
    prompt_derived_thread_title,
    thread_title_recovery_hash,
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
                UPDATE threads SET preview = '其他对话' WHERE id = 'thread-b';
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

    def _add_rollout_thread(
        self,
        thread_id: str,
        content: bytes,
        *,
        archived: bool = False,
    ) -> Path:
        sessions = Path(self.temp_dir.name) / "sessions" / "2026" / "09" / "04"
        sessions.mkdir(parents=True, exist_ok=True)
        rollout = sessions / f"rollout-{thread_id}.jsonl"
        rollout.write_bytes(content)
        connection = sqlite3.connect(self.state)
        try:
            connection.execute(
                "INSERT INTO threads "
                "(id, title, name, cwd, updated_at_ms, created_at_ms, archived, rollout_path) "
                "VALUES (?, ?, '', ?, ?, ?, ?, ?)",
                (
                    thread_id,
                    "合成历史任务",
                    "D:/repo/synthetic",
                    2_000,
                    1_000,
                    int(archived),
                    str(rollout),
                ),
            )
            connection.commit()
        finally:
            connection.close()
        return rollout

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
        self.assertEqual(match[0].title_source, "session_index_title")

    def test_title_lifecycle_prefers_manual_then_sqlite_then_independent_index(self) -> None:
        connection = sqlite3.connect(self.state)
        try:
            connection.executemany(
                "INSERT INTO threads "
                "(id, title, name, cwd, updated_at_ms, created_at_ms, archived, preview, thread_source) "
                "VALUES (?, ?, ?, 'D:/repo/title', 4000, 1000, 0, ?, 'user')",
                [
                    ("thread-auto-late", "请修复合成服务并验证结果。", "", "请修复合成服务并验证结果。"),
                    ("thread-sqlite-new", "修复合成服务", "", "请修复合成服务并验证结果。"),
                    ("thread-manual", "自动生成标题", "人工重命名", "请完成合成任务。"),
                    (
                        "thread-damaged",
                        "请检查历史合成脚本为什么失效并修复，还要完成测试并说明最终结果。",
                        "",
                        "请检查历史合成脚本为什么失效并修复，还要完成测试并说明最终结果。",
                    ),
                ],
            )
            connection.commit()
        finally:
            connection.close()
        self.session_index.write_text(
            "\n".join(
                [
                    '{"id":"thread-auto-late","thread_name":"修复合成服务"}',
                    '{"id":"thread-sqlite-new","thread_name":"请修复合成服务并验证结果。"}',
                    '{"id":"thread-manual","thread_name":"自动生成标题"}',
                    '{"id":"thread-damaged","thread_name":"请检查历史合成脚本为什么失效并修复，还要完成测试…"}',
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        records = {
            item.thread_id: item
            for item in self.store.select_threads(include_archived=True)
        }
        self.assertEqual(
            (records["thread-auto-late"].title, records["thread-auto-late"].title_source),
            ("修复合成服务", "session_index_title"),
        )
        self.assertEqual(
            (records["thread-sqlite-new"].title, records["thread-sqlite-new"].title_source),
            ("修复合成服务", "sqlite_title"),
        )
        self.assertEqual(
            (records["thread-manual"].title, records["thread-manual"].title_source),
            ("人工重命名", "manual_name"),
        )
        self.assertEqual(records["thread-damaged"].title_source, "prompt_fallback")
        self.assertTrue(
            prompt_derived_thread_title(
                records["thread-damaged"].title, records["thread-damaged"]
            )
        )

    def test_append_only_session_index_updates_one_thread_without_duplication(self) -> None:
        connection = sqlite3.connect(self.state)
        try:
            connection.execute(
                "INSERT INTO threads "
                "(id, title, name, cwd, updated_at_ms, created_at_ms, archived, preview, thread_source) "
                "VALUES ('thread-async', '请修复异步任务。', '', 'D:/repo/async', 5000, 1000, 0, "
                "'请修复异步任务。', 'user')"
            )
            connection.commit()
        finally:
            connection.close()
        self.session_index.write_text(
            '{"id":"thread-async","thread_name":"请修复异步任务。"}\n'
            '{"id":"thread-async","thread_name":"修复异步任务"}\n',
            encoding="utf-8",
        )

        matches = self.store.select_threads(thread_id="thread-async")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].title, "修复异步任务")
        self.assertEqual(matches[0].title_source, "session_index_title")

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

    def test_latest_completed_result_turn_ignores_later_failure_and_empty_completion(self) -> None:
        connection = sqlite3.connect(self.history)
        try:
            connection.executemany(
                "INSERT INTO thread_turns "
                "(thread_id, turn_id, rollout_ordinal, status, started_at, completed_at, final_agent_item_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    ("thread-c", "turn-empty", 2, "completed", 210, 220, None),
                    ("thread-c", "turn-failed-later", 3, "failed", 230, 240, None),
                ],
            )
            connection.commit()
        finally:
            connection.close()
        turn = self.store.latest_completed_result_turn("thread-c")
        self.assertIsNotNone(turn)
        self.assertEqual(turn.turn_id, "turn-final")
        self.assertEqual(turn.completed_at, 200)
        self.assertEqual(turn.final_message, "结构化最终答复")

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

    def test_rollout_cursor_uses_explicit_time_for_乱序_and_old_appends(self) -> None:
        """追加顺序乱序时，最新终态和最新结果都不能回退。"""

        content = b"\n".join(
            (
                b'{"type":"event_msg","payload":{"type":"task_complete",'
                b'"turn_id":"turn-newest","last_agent_message":"newest",'
                b'"completed_at":600,"started_at":500}}',
                b'{"type":"event_msg","payload":{"type":"turn_aborted",'
                b'"turn_id":"turn-older","completed_at":500,"started_at":400}}',
                b'{"type":"event_msg","payload":{"type":"task_complete",'
                b'"turn_id":"turn-middle","last_agent_message":"middle",'
                b'"completed_at":550,"started_at":450}}',
            )
        ) + b"\n"
        rollout = self._add_rollout_thread("thread-rollout-out-of-order", content)

        first = self.store.snapshot("thread-rollout-out-of-order")
        self.assertIsNotNone(first.latest_turn)
        assert first.latest_turn is not None
        self.assertEqual(first.latest_turn.turn_id, "turn-newest")
        self.assertEqual(first.latest_turn.final_message, "newest")
        completed = self.store.latest_completed_result_turn("thread-rollout-out-of-order")
        self.assertIsNotNone(completed)
        assert completed is not None
        self.assertEqual(completed.turn_id, "turn-newest")
        self.assertEqual(completed.final_message, "newest")
        self.store.require_readable("initial out-of-order rollout")

        # 旧终态、同一 turn 的较早冲突终态，以及无时间终态都在后面追加；
        # 游标必须复用同轮安全合并并保持已有明确最新记录。
        with rollout.open("ab") as handle:
            handle.write(
                b'{"type":"event_msg","payload":{"type":"task_complete",'
                b'"turn_id":"turn-old-appended","last_agent_message":"old",'
                b'"completed_at":400,"started_at":300}}\n'
                b'{"type":"event_msg","payload":{"type":"turn_aborted",'
                b'"turn_id":"turn-newest","completed_at":500,"started_at":450}}\n'
                b'{"type":"event_msg","payload":{"type":"turn_aborted",'
                b'"turn_id":"turn-no-time"}}\n'
            )

        second = self.store.snapshot("thread-rollout-out-of-order")
        self.assertIsNotNone(second.latest_turn)
        assert second.latest_turn is not None
        self.assertEqual(second.latest_turn.turn_id, "turn-newest")
        self.assertEqual(second.latest_turn.final_message, "newest")
        completed = self.store.latest_completed_result_turn("thread-rollout-out-of-order")
        self.assertIsNotNone(completed)
        assert completed is not None
        self.assertEqual(completed.turn_id, "turn-newest")
        self.assertEqual(completed.final_message, "newest")
        self.store.require_readable("appended old rollout")

    def test_rollout_cursor_detects_same_inode_rewrite_that_grows(self) -> None:
        """A rewrite must rewind even when the inode is retained and size grows."""

        filler = b'{"type":"unrelated"}\n' * 8_000
        old_event = (
            b'{"type":"event_msg","payload":{"type":"task_complete",'
            b'"turn_id":"turn-rewrite-old","completed_at":100,'
            b'"last_agent_message":"old result"}}\n'
        )
        new_event = (
            b'{"type":"event_msg","payload":{"type":"task_complete",'
            b'"turn_id":"turn-rewrite-new","completed_at":200,'
            b'"last_agent_message":"new result"}}\n'
        )
        rollout = self._add_rollout_thread(
            "thread-rewrite", filler + old_event
        )
        first = self.store.snapshot("thread-rewrite")
        self.assertEqual(first.latest_turn.turn_id, "turn-rewrite-old")
        before = rollout.stat()

        # Truncate/write through the same descriptor: this preserves the inode
        # while changing bytes well past a small prefix sampling window.
        rewritten = filler + new_event + (b'{"type":"unrelated-new"}\n' * 2_000)
        with rollout.open("r+b") as handle:
            handle.truncate(0)
            handle.write(rewritten)
            handle.flush()
        after = rollout.stat()
        self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))
        self.assertGreater(after.st_size, before.st_size)

        second = self.store.snapshot("thread-rewrite")
        self.assertEqual(second.latest_turn.turn_id, "turn-rewrite-new")
        self.assertEqual(second.latest_turn.final_message, "new result")

    def test_same_turn_no_timestamp_uses_rollout_event_order(self) -> None:
        """A later completed event updates an earlier interrupted event."""

        interrupted = (
            b'{"type":"event_msg","payload":{"type":"turn_aborted",'
            b'"turn_id":"turn-no-time-state"}}\n'
        )
        completed = (
            b'{"type":"event_msg","payload":{"type":"task_complete",'
            b'"turn_id":"turn-no-time-state","last_agent_message":"completed"}}\n'
        )
        rollout = self._add_rollout_thread("thread-no-time-state", interrupted)
        first = self.store.snapshot("thread-no-time-state")
        self.assertEqual(first.status, ThreadStatus.INTERRUPTED)
        with rollout.open("ab") as handle:
            handle.write(completed)
        second = self.store.snapshot("thread-no-time-state")
        self.assertEqual(second.status, ThreadStatus.COMPLETED)
        self.assertEqual(second.latest_turn.final_message, "completed")

    def test_same_turn_no_timestamp_promotes_history_in_progress(self) -> None:
        """History ordinals and rollout line numbers must not be compared."""

        rollout = self._add_rollout_thread(
            "thread-no-time-history",
            (
                b'{"type":"event_msg","payload":{"type":"task_complete",'
                b'"turn_id":"turn-no-time-history",'
                b'"last_agent_message":"completed from rollout"}}\n'
            ),
        )
        del rollout
        connection = sqlite3.connect(self.history)
        try:
            connection.execute(
                "INSERT INTO thread_turns "
                "(thread_id, turn_id, rollout_ordinal, status, started_at, completed_at) "
                "VALUES (?, ?, ?, 'inProgress', NULL, NULL)",
                ("thread-no-time-history", "turn-no-time-history", 99),
            )
            connection.commit()
        finally:
            connection.close()

        snapshot = self.store.snapshot("thread-no-time-history")
        self.assertEqual(snapshot.status, ThreadStatus.COMPLETED)
        self.assertIsNotNone(snapshot.latest_turn)
        assert snapshot.latest_turn is not None
        self.assertEqual(snapshot.latest_turn.turn_id, "turn-no-time-history")
        self.assertEqual(snapshot.latest_turn.final_message, "completed from rollout")
        self.store.require_readable("same-turn no-time history merge")

    def test_same_turn_newer_timestamp_updates_completed_result(self) -> None:
        """A newer same-turn result is not hidden by equal completeness ranking."""

        old = (
            b'{"type":"event_msg","payload":{"type":"task_complete",'
            b'"turn_id":"turn-result-update","completed_at":100,'
            b'"last_agent_message":"old result"}}\n'
        )
        new = (
            b'{"type":"event_msg","payload":{"type":"task_complete",'
            b'"turn_id":"turn-result-update","completed_at":200,'
            b'"last_agent_message":"new result"}}\n'
        )
        rollout = self._add_rollout_thread("thread-result-update", old)
        self.assertEqual(
            self.store.snapshot("thread-result-update").latest_turn.final_message,
            "old result",
        )
        with rollout.open("ab") as handle:
            handle.write(new)
        second = self.store.snapshot("thread-result-update")
        self.assertEqual(second.latest_turn.status, ThreadStatus.COMPLETED)
        self.assertEqual(second.latest_turn.final_message, "new result")

    def test_rollout_outer_timestamp_is_used_when_payload_time_is_missing(self) -> None:
        """Envelope time supplies ordering evidence for sparse terminal payloads."""

        old = json.dumps(
            {
                "timestamp": "2026-09-04T00:00:01Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "turn-envelope-time",
                    "last_agent_message": "envelope old",
                },
            }
        ).encode("utf-8") + b"\n"
        new = json.dumps(
            {
                "timestamp": "2026-09-04T00:00:02Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "turn-envelope-time",
                    "last_agent_message": "envelope new",
                },
            }
        ).encode("utf-8") + b"\n"
        rollout = self._add_rollout_thread("thread-envelope-time", old)
        first = self.store.snapshot("thread-envelope-time")
        self.assertIsNotNone(first.latest_turn.completed_at)
        self.assertEqual(first.latest_turn.final_message, "envelope old")
        with rollout.open("ab") as handle:
            handle.write(new)
        second = self.store.snapshot("thread-envelope-time")
        self.assertEqual(second.latest_turn.final_message, "envelope new")
        self.assertGreater(
            second.latest_turn.completed_at,
            first.latest_turn.completed_at,
        )

    def test_cross_turn_missing_timestamps_follow_file_event_order(self) -> None:
        """Missing timestamps do not permanently pin the first observed turn."""

        first_turn = (
            b'{"type":"event_msg","payload":{"type":"turn_aborted",'
            b'"turn_id":"turn-file-first"}}\n'
        )
        second_turn = (
            b'{"type":"event_msg","payload":{"type":"task_complete",'
            b'"turn_id":"turn-file-second","last_agent_message":"second"}}\n'
        )
        rollout = self._add_rollout_thread("thread-file-order", first_turn)
        self.assertEqual(
            self.store.snapshot("thread-file-order").latest_turn.turn_id,
            "turn-file-first",
        )
        with rollout.open("ab") as handle:
            handle.write(second_turn)
        second = self.store.snapshot("thread-file-order")
        self.assertEqual(second.latest_turn.turn_id, "turn-file-second")

    def test_read_errors_are_isolated_between_concurrent_queries(self) -> None:
        """One query's error list cannot poison another thread's snapshot."""

        barrier = threading.Barrier(2)
        original_open = self.store._open
        snapshots: dict[str, Any] = {}
        failures: list[BaseException] = []

        def open_for_thread(path, label):
            if label == "state":
                barrier.wait(timeout=5)
                if threading.current_thread().name == "broken":
                    self.store._errors().append("synthetic:broken")
                    return None
            return original_open(path, label)

        self.store._open = open_for_thread  # type: ignore[method-assign]

        def query() -> None:
            try:
                snapshots[threading.current_thread().name] = self.store.snapshot(
                    "thread-a"
                )
            except BaseException as exc:  # pragma: no cover - diagnostic guard
                failures.append(exc)

        workers = [
            threading.Thread(target=query, name="broken"),
            threading.Thread(target=query, name="healthy"),
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=5)
        self.assertFalse(failures)
        self.assertEqual(snapshots["broken"].errors, ("synthetic:broken",))
        self.assertEqual(snapshots["healthy"].errors, ())
        self.assertEqual(self.store.last_errors, ())

    def test_rollout_cursor_isolated_for_threads_sharing_a_path(self) -> None:
        """A shared/reused path cannot reuse another thread's cursor state."""

        sessions = Path(self.temp_dir.name) / "sessions" / "2026" / "09" / "04"
        sessions.mkdir(parents=True, exist_ok=True)
        rollout = sessions / "rollout-shared.jsonl"
        rollout.write_text(
            json.dumps(
                {
                    "thread_id": "thread-shared-a",
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": "turn-shared-a",
                        "last_agent_message": "A result",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        connection = sqlite3.connect(self.state)
        try:
            for thread_id in ("thread-shared-a", "thread-shared-b"):
                connection.execute(
                    "INSERT INTO threads "
                    "(id, title, name, cwd, updated_at_ms, created_at_ms, archived, rollout_path) "
                    "VALUES (?, '共享路径', '', 'D:/repo/shared', 2, 1, 0, ?)",
                    (thread_id, str(rollout)),
                )
            connection.commit()
        finally:
            connection.close()

        first = self.store.snapshot("thread-shared-a")
        self.assertEqual(first.latest_turn.turn_id, "turn-shared-a")
        with rollout.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "thread_id": "thread-shared-b",
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "turn_id": "turn-shared-b",
                            "last_agent_message": "B result",
                        },
                    }
                )
                + "\n"
            )

        second = self.store.snapshot("thread-shared-b")
        self.assertEqual(second.latest_turn.turn_id, "turn-shared-b")
        still_a = self.store.snapshot("thread-shared-a")
        self.assertEqual(still_a.latest_turn.turn_id, "turn-shared-a")
        path_key = self.store._comparison_path(rollout)
        self.assertIn((path_key, "thread-shared-a"), self.store._rollout_cursors)
        self.assertIn((path_key, "thread-shared-b"), self.store._rollout_cursors)

    def test_shared_rollout_without_thread_id_fails_closed_without_body(self) -> None:
        sessions = Path(self.temp_dir.name) / "sessions" / "2026" / "09" / "04"
        sessions.mkdir(parents=True, exist_ok=True)
        secret = "SHARED-ROLLOUT-PRIVATE-BODY"
        rollout = sessions / "rollout-shared-unscoped.jsonl"
        rollout.write_text(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": "turn-unscoped",
                        "last_agent_message": secret,
                    },
                }
            )
            + "\n"
            + json.dumps(
                {
                    "thread_id": "thread-unscoped-a",
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": "turn-after-ambiguous",
                        "last_agent_message": "must not expose after ambiguity",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        connection = sqlite3.connect(self.state)
        try:
            for thread_id in ("thread-unscoped-a", "thread-unscoped-b"):
                connection.execute(
                    "INSERT INTO threads "
                    "(id, title, name, cwd, updated_at_ms, created_at_ms, archived, rollout_path) "
                    "VALUES (?, '共享未标记路径', '', 'D:/repo/shared', 2, 1, 0, ?)",
                    (thread_id, str(rollout)),
                )
            connection.commit()
        finally:
            connection.close()

        for thread_id in ("thread-unscoped-a", "thread-unscoped-b"):
            with self.subTest(thread_id=thread_id):
                snapshot = self.store.snapshot(thread_id)
                self.assertIsNone(snapshot.latest_turn)
                self.assertEqual(snapshot.status, ThreadStatus.UNKNOWN)
                self.assertEqual(snapshot.errors, ("rollout:thread-ambiguous",))
                self.assertNotIn(secret, repr(snapshot))
                with self.assertRaises(CodexStoreReadError) as raised:
                    snapshot.require_readable()
                self.assertNotIn(secret, str(raised.exception))

                # Exact fallback must enforce the same ownership boundary.
                self.assertIsNone(self.store.get_turn(thread_id, "turn-unscoped"))
                with self.assertRaises(CodexStoreReadError) as raised:
                    self.store.require_readable("shared unscoped exact turn")
                self.assertEqual(
                    raised.exception.errors, ("rollout:thread-ambiguous",)
                )
                self.assertNotIn(secret, str(raised.exception))

    def test_get_turn_recovers_only_exact_rollout_turn_when_history_lags(self) -> None:
        self._add_rollout_thread(
            "thread-exact-rollout",
            (
                b'{"type":"event_msg","payload":{"type":"task_complete",'
                b'"turn_id":"turn-requested","last_agent_message":"requested raw"}}\n'
                b'{"type":"event_msg","payload":{"type":"task_complete",'
                b'"turn_id":"turn-newer","last_agent_message":"newer raw"}}\n'
            ),
            archived=True,
        )

        recovered = self.store.get_turn("thread-exact-rollout", "turn-requested")

        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertEqual(recovered.turn_id, "turn-requested")
        self.assertEqual(recovered.final_message, "requested raw")
        self.assertEqual(recovered.raw, {"source": "codex-rollout", "event_type": "task_complete"})
        self.store.require_readable("recover exact rollout turn")

        # A fresh process/store must recover the same immutable identity; it must
        # never substitute the newer terminal event from the same rollout.
        reopened = CodexStore(
            paths=StorePaths(self.state, self.history, self.session_index),
            timeout_seconds=0.2,
        )
        after_restart = reopened.get_turn("thread-exact-rollout", "turn-requested")
        self.assertIsNotNone(after_restart)
        assert after_restart is not None
        self.assertEqual(after_restart.turn_id, "turn-requested")
        self.assertEqual(after_restart.final_message, "requested raw")
        reopened.require_readable("recover exact rollout turn after restart")

    def test_get_turn_prefers_exact_history_projection_over_rollout(self) -> None:
        sessions = Path(self.temp_dir.name) / "sessions" / "2026" / "09" / "04"
        sessions.mkdir(parents=True, exist_ok=True)
        rollout = sessions / "rollout-thread-c.jsonl"
        rollout.write_text(
            '{"type":"event_msg","payload":{"type":"task_complete",'
            '"turn_id":"turn-final","last_agent_message":"rollout copy"}}\n',
            encoding="utf-8",
        )
        connection = sqlite3.connect(self.state)
        try:
            connection.execute(
                "UPDATE threads SET rollout_path=? WHERE id='thread-c'",
                (str(rollout),),
            )
            connection.commit()
        finally:
            connection.close()

        recovered = self.store.get_turn("thread-c", "turn-final")

        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertEqual(recovered.final_message, "结构化最终答复")
        self.assertNotEqual(recovered.raw.get("source"), "codex-rollout")

    def test_get_turn_does_not_hide_history_read_errors_with_rollout(self) -> None:
        self._add_rollout_thread(
            "thread-history-error",
            (
                b'{"type":"event_msg","payload":{"type":"task_complete",'
                b'"turn_id":"turn-target","last_agent_message":"must not leak"}}\n'
            ),
        )
        connection = sqlite3.connect(self.history)
        try:
            connection.execute("DROP TABLE thread_turns")
            connection.commit()
        finally:
            connection.close()

        self.assertIsNone(self.store.get_turn("thread-history-error", "turn-target"))
        with self.assertRaises(CodexStoreReadError) as raised:
            self.store.require_readable("exact history error")
        self.assertIn("history:schema", raised.exception.errors)
        self.assertNotIn("must not leak", str(raised.exception))

    def test_get_turn_rollout_rejects_partial_and_ambiguous_terminals(self) -> None:
        cases = {
            "partial": (
                b'{"type":"event_msg","payload":{"type":"task_complete",'
                b'"turn_id":"turn-target","last_agent_message":"complete"}}\n'
                b'{"type":"event_msg","payload":{"type":"task_complete"}'
            ),
            "ambiguous": (
                b'{"type":"event_msg","payload":{"type":"task_complete",'
                b'"turn_id":"turn-target","last_agent_message":"first"}}\n'
                b'{"type":"event_msg","payload":{"type":"turn_aborted",'
                b'"turn_id":"turn-target"}}\n'
            ),
        }
        expected_error = {
            "partial": "rollout:partial-line",
            "ambiguous": "rollout:turn-ambiguous",
        }
        for label, payload in cases.items():
            with self.subTest(label=label):
                thread_id = f"thread-{label}"
                self._add_rollout_thread(thread_id, payload)
                self.assertIsNone(self.store.get_turn(thread_id, "turn-target"))
                with self.assertRaises(CodexStoreReadError) as raised:
                    self.store.require_readable(f"reject {label}")
                self.assertEqual(raised.exception.errors, (expected_error[label],))
                self.assertNotIn(thread_id, str(raised.exception))

    def test_get_turn_rollout_rejects_untrusted_path_without_disclosing_it(self) -> None:
        outside = Path(self.temp_dir.name).parent / "outside-exact-rollout.jsonl"
        outside.write_text(
            '{"type":"event_msg","payload":{"type":"task_complete",'
            '"turn_id":"turn-target","last_agent_message":"private"}}\n',
            encoding="utf-8",
        )
        self.addCleanup(outside.unlink, missing_ok=True)
        connection = sqlite3.connect(self.state)
        try:
            connection.execute(
                "INSERT INTO threads "
                "(id, title, name, cwd, updated_at_ms, created_at_ms, archived, rollout_path) "
                "VALUES ('thread-untrusted', '合成任务', '', 'D:/repo', 2, 1, 0, ?)",
                (str(outside),),
            )
            connection.commit()
        finally:
            connection.close()

        self.assertIsNone(self.store.get_turn("thread-untrusted", "turn-target"))
        with self.assertRaises(CodexStoreReadError) as raised:
            self.store.require_readable("reject untrusted rollout")
        self.assertEqual(raised.exception.errors, ("rollout:path",))
        self.assertNotIn(str(outside), str(raised.exception))
        self.assertNotIn("private", str(raised.exception))

    def test_latest_readers_merge_stale_history_and_multiple_rollout_results(self) -> None:
        sessions = Path(self.temp_dir.name) / "sessions" / "2026" / "09" / "04"
        sessions.mkdir(parents=True, exist_ok=True)
        rollout = sessions / "rollout-thread-c-lagging-history.jsonl"
        rollout.write_text(
            "".join(
                (
                    '{"type":"event_msg","payload":{"type":"task_complete",'
                    '"turn_id":"turn-rollout-old","last_agent_message":"older rollout",'
                    '"started_at":300,"completed_at":400}}\n',
                    '{"type":"event_msg","payload":{"type":"task_complete",'
                    '"turn_id":"turn-rollout-new","last_agent_message":"newest completed",'
                    '"started_at":500,"completed_at":600}}\n',
                    '{"type":"event_msg","payload":{"type":"turn_aborted",'
                    '"turn_id":"turn-rollout-aborted","started_at":700,"completed_at":800}}\n',
                )
            ),
            encoding="utf-8",
        )
        connection = sqlite3.connect(self.state)
        try:
            connection.execute(
                "UPDATE threads SET rollout_path=?, updated_at_ms=? WHERE id='thread-c'",
                (str(rollout), 800_000),
            )
            connection.commit()
        finally:
            connection.close()

        latest = self.store.latest_turn("thread-c")
        self.store.require_readable("latest turn")
        terminal = self.store.latest_terminal_turn("thread-c")
        self.store.require_readable("latest terminal turn")
        completed = self.store.latest_completed_result_turn("thread-c")
        self.store.require_readable("latest completed result turn")
        exact_old = self.store.get_turn("thread-c", "turn-rollout-old")
        self.store.require_readable("exact older rollout turn")

        self.assertIsNotNone(latest)
        self.assertIsNotNone(terminal)
        self.assertIsNotNone(completed)
        self.assertIsNotNone(exact_old)
        assert latest is not None and terminal is not None
        assert completed is not None and exact_old is not None
        self.assertEqual(latest.turn_id, "turn-rollout-aborted")
        self.assertEqual(terminal.turn_id, "turn-rollout-aborted")
        self.assertEqual(completed.turn_id, "turn-rollout-new")
        self.assertEqual(completed.final_message, "newest completed")
        self.assertEqual(exact_old.turn_id, "turn-rollout-old")
        self.assertEqual(exact_old.final_message, "older rollout")

    def test_snapshot_reads_rollout_when_updated_at_is_stale_equal_or_near_history(self) -> None:
        """目录时间不是 rollout 水位；三种边界都必须发现新增终态。"""

        sessions = Path(self.temp_dir.name) / "sessions" / "2026" / "09" / "04"
        sessions.mkdir(parents=True, exist_ok=True)
        base = 1_700_000_000_000
        cases = {
            "stale": base - 5_000,
            "equal": base,
            "near": base + 1_500,
        }
        connection = sqlite3.connect(self.state)
        history_connection = sqlite3.connect(self.history)
        try:
            for label, updated_at_ms in cases.items():
                thread_id = f"thread-snapshot-{label}"
                history_turn_id = f"turn-history-{label}"
                rollout_turn_id = f"turn-rollout-{label}"
                rollout = sessions / f"rollout-{thread_id}.jsonl"
                rollout.write_text(
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "task_complete",
                                "turn_id": rollout_turn_id,
                                "last_agent_message": f"rollout result {label}",
                                "started_at": base + 5_000,
                                "completed_at": base + 10_000,
                            },
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                connection.execute(
                    "INSERT INTO threads "
                    "(id, title, name, cwd, updated_at_ms, created_at_ms, archived, rollout_path) "
                    "VALUES (?, ?, '', 'D:/repo/synthetic', ?, ?, 0, ?)",
                    (
                        thread_id,
                        f"合成快照 {label}",
                        updated_at_ms,
                        base - 10_000,
                        str(rollout),
                    ),
                )
                history_connection.execute(
                    "INSERT INTO thread_turns "
                    "(thread_id, turn_id, rollout_ordinal, status, started_at, completed_at) "
                    "VALUES (?, ?, 1, 'completed', ?, ?)",
                    (thread_id, history_turn_id, base - 20_000, base),
                )
            connection.commit()
            history_connection.commit()
        finally:
            connection.close()
            history_connection.close()

        for label in cases:
            with self.subTest(label=label):
                snapshot = self.store.snapshot(f"thread-snapshot-{label}")
                self.assertEqual(snapshot.status, ThreadStatus.COMPLETED)
                self.assertIsNotNone(snapshot.latest_turn)
                assert snapshot.latest_turn is not None
                self.assertEqual(snapshot.latest_turn.turn_id, f"turn-rollout-{label}")
                self.assertEqual(snapshot.latest_turn.final_message, f"rollout result {label}")
                self.store.require_readable(f"snapshot {label}")

                # 第二次读取只检查相同游标，不重扫未变化的 rollout 文件。
                cursor_key = (
                    self.store._comparison_path(
                        sessions / f"rollout-thread-snapshot-{label}.jsonl"
                    ),
                    f"thread-snapshot-{label}",
                )
                cursor = self.store._rollout_cursors[cursor_key]
                offset = cursor.offset
                again = self.store.snapshot(f"thread-snapshot-{label}")
                self.assertEqual(again.latest_turn.turn_id, f"turn-rollout-{label}")
                self.assertEqual(self.store._rollout_cursors[cursor_key].offset, offset)

    def test_snapshot_promotes_same_turn_rollout_completion_but_get_turn_stays_exact_history(self) -> None:
        """snapshot 可推进滞后的同轮状态；精确 get_turn 仍不替换已有 history。"""

        sessions = Path(self.temp_dir.name) / "sessions" / "2026" / "09" / "04"
        sessions.mkdir(parents=True, exist_ok=True)
        thread_id = "thread-same-turn-lag"
        turn_id = "turn-same-turn"
        base = 1_700_000_100_000
        rollout = sessions / f"rollout-{thread_id}.jsonl"
        rollout.write_text(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": turn_id,
                        "last_agent_message": "同轮已完成的 rollout 结果",
                        "started_at": base + 100,
                        "completed_at": base + 1_000,
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        connection = sqlite3.connect(self.state)
        history_connection = sqlite3.connect(self.history)
        try:
            connection.execute(
                "INSERT INTO threads "
                "(id, title, name, cwd, updated_at_ms, created_at_ms, archived, rollout_path) "
                "VALUES (?, '同轮滞后合成任务', '', 'D:/repo/synthetic', ?, ?, 0, ?)",
                (thread_id, base, base - 10_000, str(rollout)),
            )
            history_connection.execute(
                "INSERT INTO thread_turns "
                "(thread_id, turn_id, rollout_ordinal, status, started_at, completed_at) "
                "VALUES (?, ?, 1, 'inProgress', ?, NULL)",
                (thread_id, turn_id, base),
            )
            connection.commit()
            history_connection.commit()
        finally:
            connection.close()
            history_connection.close()

        snapshot = self.store.snapshot(thread_id)
        self.assertEqual(snapshot.status, ThreadStatus.COMPLETED)
        self.assertIsNotNone(snapshot.latest_turn)
        assert snapshot.latest_turn is not None
        self.assertEqual(snapshot.latest_turn.turn_id, turn_id)
        self.assertEqual(snapshot.latest_turn.final_message, "同轮已完成的 rollout 结果")
        self.store.require_readable("same-turn snapshot")

        # 精确动作已在 history 中存在时，不能因 snapshot 的合并逻辑改写
        # get_turn 的不可变 history 优先语义。
        exact = self.store.get_turn(thread_id, turn_id)
        self.assertIsNotNone(exact)
        assert exact is not None
        self.assertEqual(exact.status, ThreadStatus.IN_PROGRESS)
        self.assertEqual(exact.final_message, "")
        self.store.require_readable("same-turn exact history")

    def test_same_turn_conflicting_terminal_states_use_explicit_time_without_mixing(self) -> None:
        """旧空 completed 不能压过较晚失败/中断，时间不明时必须保守。"""

        base = 1_700_000_200_000
        history = TurnRecord(
            thread_id="thread-conflicting-terminal",
            turn_id="turn-conflicting-terminal",
            status=ThreadStatus.COMPLETED,
            completed_at=base,
            final_message="",
            raw={"source": "history"},
        )
        for status in (ThreadStatus.INTERRUPTED, ThreadStatus.FAILED):
            with self.subTest(status=status):
                later = TurnRecord(
                    thread_id=history.thread_id,
                    turn_id=history.turn_id,
                    status=status,
                    completed_at=base + 1_000,
                    error_json="structured failure",
                    final_message="",
                    raw={"source": "rollout"},
                )
                merged = CodexStore._newer_turn(history, later)
                self.assertIs(merged, later)
                self.assertEqual(merged.status, status)
                self.assertEqual(merged.error_json, "structured failure")

        # completed_at 相同或缺失时不能借状态等级猜测，也不能把另一份的
        # 时间、错误或结果拼到 history 上。
        for rollout_completed_at in (base, None):
            with self.subTest(rollout_completed_at=rollout_completed_at):
                conflicting = TurnRecord(
                    thread_id=history.thread_id,
                    turn_id=history.turn_id,
                    status=ThreadStatus.INTERRUPTED,
                    completed_at=rollout_completed_at,
                    error_json="must not be mixed",
                    raw={"source": "rollout"},
                )
                merged = CodexStore._newer_turn(history, conflicting)
                self.assertIs(merged, history)
                self.assertEqual(merged.status, ThreadStatus.COMPLETED)
                self.assertEqual(merged.completed_at, base)
                self.assertIsNone(merged.error_json)
                self.assertEqual(merged.raw, {"source": "history"})

        newer_history_failure = replace(
            history,
            status=ThreadStatus.FAILED,
            completed_at=base + 2_000,
            error_json="history failure",
        )
        older_rollout_success = replace(
            history,
            status=ThreadStatus.COMPLETED,
            completed_at=base + 1_000,
            final_message="stale success",
            raw={"source": "rollout"},
        )
        merged = CodexStore._newer_turn(newer_history_failure, older_rollout_success)
        self.assertIs(merged, newer_history_failure)
        self.assertEqual(merged.status, ThreadStatus.FAILED)
        self.assertEqual(merged.error_json, "history failure")
        self.assertEqual(merged.final_message, "")

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

    def test_independent_title_uses_preview_evidence_not_raw_title_equality(self) -> None:
        real = ThreadRecord(
            "real-title",
            title="修复自动归档工具",
            preview="请检查合成工具的归档逻辑并修复问题。",
            raw={
                "title": "修复自动归档工具",
                "name": "",
                "preview": "请检查合成工具的归档逻辑并修复问题。",
            },
        )
        self.assertFalse(prompt_derived_thread_title(real.title, real))
        self.assertEqual(independent_thread_title(real), "修复自动归档工具")

        prompt = (
            "请检查合成工具的归档逻辑并修复问题，完成后给出验证结果，"
            "同时整理测试步骤和后续使用说明。"
        )
        prompt_title = prompt[:28] + "…"
        derived = ThreadRecord(
            "prompt-title",
            title=prompt_title,
            preview=prompt,
            raw={"title": prompt, "name": "", "preview": prompt},
        )
        self.assertTrue(prompt_derived_thread_title(prompt_title, derived))
        self.assertEqual(independent_thread_title(derived), "")

        renamed = ThreadRecord(
            "renamed",
            title=prompt_title,
            preview=prompt,
            raw={"title": prompt, "name": "用户确认的会话名", "preview": prompt},
        )
        self.assertEqual(independent_thread_title(renamed), "用户确认的会话名")

    def test_prompt_wrapper_containment_uses_independent_session_index_title(self) -> None:
        preview = "请完成跨端工具的设计、实现、测试和使用说明。" * 20
        wrapped = f"/goal {preview} 请持续执行直到完成。"
        record = ThreadRecord(
            "wrapped-prompt",
            title=wrapped,
            preview=preview,
            raw={"title": wrapped, "name": "", "preview": preview},
        )
        self.assertTrue(prompt_derived_thread_title(wrapped, record))
        self.assertEqual(independent_thread_title(record), "")

        resolved, source = _resolve_thread_title(
            name="",
            sqlite_title=wrapped,
            session_title="开发跨端屏幕共享工具",
            preview=preview,
        )
        self.assertEqual(resolved, "开发跨端屏幕共享工具")
        self.assertEqual(source, "session_index_title")

    def test_public_title_prefers_real_title_then_hash_bound_recovery(self) -> None:
        prompt = "请分析并修复旧工具，验证完成后整理说明。" * 8
        damaged = ThreadRecord(
            "damaged-thread",
            title=prompt,
            preview=prompt,
            updated_at_ms=1_700_000_000_000,
            raw={"title": prompt, "name": "", "preview": prompt},
            title_source="prompt_fallback",
        )
        first_hash = thread_title_recovery_hash(damaged)
        title, source = public_thread_title(damaged, "修复历史自动化工具")
        self.assertEqual((title, source), ("修复历史自动化工具", "recovered_summary"))
        self.assertNotIn(prompt[:20], title)
        changed = replace(damaged, updated_at_ms=1_700_000_100_000)
        self.assertNotEqual(first_hash, thread_title_recovery_hash(changed))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
