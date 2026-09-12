"""Independent acceptance checks for history projection and artifact capture.

These tests stay outside the production integration modules so the root task
can apply service/codex-store fixes without merging a test fixture into them.
All files contain synthetic data only and no channel/network is contacted.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "src"))

from progress_wx.codex_store import (  # noqa: E402
    CodexStore,
    StorePaths,
    ThreadSnapshot,
    ThreadStatus,
    ThreadRecord,
    TurnRecord,
)
from progress_wx.delivered_files import discover_delivered_files  # noqa: E402
from progress_wx.file_delivery import FileDeliveryQueue  # noqa: E402
from progress_wx.models import ProgressReport, TurnEvent  # noqa: E402
from progress_wx.service import ProgressService, snapshot_to_event  # noqa: E402
from progress_wx.state import StateStore  # noqa: E402


def _local_delivery(path: Path, *, turn_id: str = "turn-1"):
    result = discover_delivered_files(
        [
            {
                "type": "agentMessage",
                "id": "item-final",
                "turn_id": turn_id,
                "phase": "final_answer",
                "text": f"交付文件：<{path}>",
            }
        ],
        turn_id=turn_id,
        final_agent_item_id="item-final",
        inspect_content=False,
    )
    assert len(result.ready) == 1
    return result.ready[0]


def _create_history_databases(tmp_path: Path) -> tuple[Path, Path]:
    state = tmp_path / "state.sqlite"
    history = tmp_path / "thread_history.sqlite"
    with sqlite3.connect(state) as connection:
        connection.executescript(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY, title TEXT, name TEXT, cwd TEXT,
                updated_at_ms INTEGER, created_at_ms INTEGER,
                archived INTEGER DEFAULT 0, rollout_path TEXT
            );
            INSERT INTO threads
                (id,title,name,cwd,updated_at_ms,created_at_ms,archived)
            VALUES ('thread-history','synthetic history','','D:/synthetic',2000,1000,0);
            """
        )
    with sqlite3.connect(history) as connection:
        connection.executescript(
            """
            CREATE TABLE thread_turns (
                thread_id TEXT, turn_id TEXT, rollout_ordinal INTEGER,
                status TEXT, error_json TEXT, started_at INTEGER,
                completed_at INTEGER, duration_ms INTEGER,
                final_agent_item_id TEXT
            );
            CREATE TABLE thread_items (
                thread_id TEXT, turn_id TEXT, item_id TEXT,
                rollout_ordinal INTEGER, created_at_ms INTEGER,
                item_json TEXT, item_type TEXT, updated_at_ordinal INTEGER
            );
            """
        )
    return state, history


def test_history_uses_exact_final_item_and_same_turn_completed_resource(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "history-output.data"
    artifact.write_bytes(b"history artifact")
    tool_artifact = tmp_path / "tool-output.data"
    tool_artifact.write_bytes(b"tool artifact")
    commentary_artifact = tmp_path / "commentary-must-not-send.data"
    commentary_artifact.write_bytes(b"commentary")
    state, history = _create_history_databases(tmp_path)
    final_item = {
        "type": "agentMessage",
        "id": "item-final",
        "phase": "final_answer",
        "text": f"交付文件：<{artifact}>",
    }
    commentary_item = {
        "type": "agentMessage",
        "id": "item-commentary",
        "phase": "commentary",
        "text": f"交付文件：<{commentary_artifact}>",
    }
    tool_item = {
        "type": "mcpToolCall",
        "id": "tool-item",
        "status": "completed",
        "error": None,
        "result": {
            "isError": False,
            "content": [{"type": "resource_link", "uri": tool_artifact.as_uri()}],
        },
    }
    with sqlite3.connect(history) as connection:
        connection.execute(
            "INSERT INTO thread_turns VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "thread-history",
                "turn-1",
                1,
                "completed",
                None,
                100,
                200,
                None,
                "item-final",
            ),
        )
        connection.executemany(
            "INSERT INTO thread_items"
            " (thread_id,turn_id,item_id,item_json,item_type) VALUES (?,?,?,?,?)",
            [
                (
                    "thread-history",
                    "turn-1",
                    "item-final",
                    json.dumps(final_item),
                    "agentMessage",
                ),
                (
                    "thread-history",
                    "turn-1",
                    "item-commentary",
                    json.dumps(commentary_item),
                    "agentMessage",
                ),
                (
                    "thread-history",
                    "turn-1",
                    "tool-item",
                    json.dumps(tool_item),
                    "mcpToolCall",
                ),
            ],
        )

    store = CodexStore(
        paths=StorePaths(state, history, tmp_path / "session-index.jsonl")
    )
    snapshot = store.snapshot("thread-history")
    assert snapshot.errors == ()
    assert snapshot.latest_turn is not None
    delivered = snapshot.latest_turn.delivered_files
    assert {candidate.path for candidate in delivered} == {artifact, tool_artifact}
    assert commentary_artifact not in {candidate.path for candidate in delivered}
    assert all(candidate.provenance.get("inspection") == "stat" for candidate in delivered)


def test_valid_rollout_task_complete_projects_delivery_without_history_rows(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "sessions" / "2026" / "09" / "rollout-output.data"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"rollout artifact")
    rollout = artifact.parent / "rollout-thread-rollout.jsonl"
    rollout.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "turn-rollout",
                    "completed_at": 2_000,
                    "last_agent_message": f"交付文件：<{artifact}>",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    state, history = _create_history_databases(tmp_path)
    with sqlite3.connect(state) as connection:
        connection.execute(
            "INSERT INTO threads"
            " (id,title,name,cwd,updated_at_ms,created_at_ms,archived,rollout_path)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                "thread-rollout",
                "synthetic rollout",
                "",
                "D:/synthetic",
                2_000,
                1_000,
                0,
                str(rollout),
            ),
        )
    store = CodexStore(
        paths=StorePaths(state, history, tmp_path / "session-index.jsonl")
    )
    snapshot = store.snapshot("thread-rollout")
    assert snapshot.latest_turn is not None
    assert snapshot.latest_turn.raw["source"] == "codex-rollout"
    assert len(snapshot.latest_turn.delivered_files) == 1
    assert snapshot.latest_turn.delivered_files[0].path == artifact
    assert snapshot.latest_turn.delivered_files[0].sha256 == ""


class _SilentStore:
    def __init__(self) -> None:
        self.processed: list[str] = []

    def was_processed(self, event_key: str) -> bool:
        return event_key in self.processed

    def mark_processed(self, event_key: str) -> None:
        self.processed.append(event_key)


class _RecordingArtifactQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[TurnEvent, tuple[object, ...]]] = []

    def reserve(self, event: TurnEvent, candidates: tuple[object, ...]) -> None:
        self.calls.append((event, tuple(candidates)))


def test_silent_policy_does_not_drop_already_reserved_artifact(tmp_path: Path) -> None:
    artifact = tmp_path / "silent-policy-output.bin"
    artifact.write_bytes(b"artifact")
    candidate = _local_delivery(artifact)
    event = TurnEvent(
        "thread-silent",
        "turn-silent",
        "completed",
        final_message="本轮完成。",
        delivered_files=(candidate,),
    )
    store = _SilentStore()
    queue = _RecordingArtifactQueue()
    service = ProgressService.__new__(ProgressService)
    service.config = SimpleNamespace(
        messaging=SimpleNamespace(backend="feishu"),
        service=SimpleNamespace(max_attempts=1, retry_delays=(0,)),
    )
    service.store = store
    service.codec = object()
    service.channel = object()
    service.summarizer = object()
    service._public_event_title = lambda current: current
    service._artifact_queue = lambda: queue
    service._notification_policy_context = lambda _event: object()
    service._summarize_with_policy_context = lambda _event, _context: ProgressReport(
        "*/*", "静默", notification_reason="silent"
    )
    service._policy = lambda: SimpleNamespace(max_attempts=1, delays=(0,))
    service._retry_sleep = lambda _delay: None
    service._on_retry = lambda _operation: None

    service._send_event(event)

    assert len(queue.calls) == 1
    assert queue.calls[0][1] == (candidate,)
    assert store.processed == [event.dedupe_key]


class _OfflineChannel:
    def is_online(self) -> bool:
        return False


def test_first_start_suppresses_old_history_but_accepts_new_turn(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.sqlite")
    queue = FileDeliveryQueue(
        state,
        tmp_path / "artifact-snapshots",
        _OfflineChannel(),
        lambda *_args: None,
        start=False,
    )
    try:
        artifact = tmp_path / "old-or-new.bin"
        artifact.write_bytes(b"artifact")
        candidate = _local_delivery(artifact)
        old_event = TurnEvent(
            "thread-history",
            "turn-old",
            "completed",
            completed_at=int(queue.enabled_at) - 1,
            delivered_files=(candidate,),
        )
        queue.reserve(old_event, (candidate,))
        assert queue._rows("SELECT * FROM artifact_file_deliveries") == []

        new_event = TurnEvent(
            "thread-history",
            "turn-new",
            "completed",
            completed_at=int(queue.enabled_at) + 1,
            delivered_files=(candidate,),
        )
        queue.reserve(new_event, (candidate,))
        rows = queue._rows("SELECT * FROM artifact_file_deliveries")
        assert len(rows) == 1
        assert rows[0]["state"] == "capture_pending"
    finally:
        queue.stop()
        state.close()


def test_main_poll_reservation_does_not_read_slow_artifact_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = StateStore(tmp_path / "state.sqlite")
    queue = FileDeliveryQueue(
        state,
        tmp_path / "artifact-snapshots",
        _OfflineChannel(),
        lambda *_args: None,
        start=False,
    )
    artifact = tmp_path / "slow-output.bin"
    artifact.write_bytes(b"artifact")
    candidate = _local_delivery(artifact)
    original_open = Path.open

    def fail_if_content_is_read(self: Path, *args, **kwargs):
        raise AssertionError("main-poll reservation read artifact content")

    monkeypatch.setattr(Path, "open", fail_if_content_is_read)
    started = time.monotonic()
    try:
        queue.reserve(
            TurnEvent(
                "thread-slow",
                "turn-slow",
                "completed",
                completed_at=int(queue.enabled_at) + 1,
                delivered_files=(candidate,),
            ),
            (candidate,),
        )
    finally:
        monkeypatch.setattr(Path, "open", original_open)
        queue.stop()
        state.close()
    assert time.monotonic() - started < 1.0


def test_completed_tool_only_snapshot_is_required_to_project_event() -> None:
    artifact = object()
    turn = TurnRecord(
        thread_id="thread-tool-only",
        turn_id="turn-tool-only",
        status=ThreadStatus.COMPLETED,
        completed_at=2_000,
        delivered_files=(artifact,),
    )
    snapshot = ThreadSnapshot(
        thread=ThreadRecord("thread-tool-only", title="tool-only"),
        status=ThreadStatus.COMPLETED,
        latest_turn=turn,
    )
    event = snapshot_to_event(snapshot)
    assert event is not None
    assert event.delivered_files == (artifact,)
