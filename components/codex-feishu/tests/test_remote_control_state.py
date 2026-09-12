"""schema19 远程 Codex 写动作状态机的合成回归。"""

from __future__ import annotations

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from progress_wx.state import SCHEMA_VERSION, REMOTE_CONTROL_ACTIONS, StateError, StateStore


def _context(store: StateStore, *, name: str = "ctx") -> str:
    return store.create_management_context(
        "remote_control",
        {"selector": name},
        sender_id="ou_owner",
        chat_id="oc_private",
    )


def _set_schema(path: Path, version: int, *, drop_remote: bool = False) -> None:
    connection = sqlite3.connect(path)
    try:
        if drop_remote:
            connection.execute("DROP TABLE IF EXISTS remote_control_actions")
        connection.execute(
            "UPDATE meta SET value=? WHERE key='schema_version'", (str(version),)
        )
        connection.commit()
    finally:
        connection.close()


def _file_state(path: Path) -> tuple[str, int, bool, str | None]:
    wal = Path(f"{path}-wal")
    return (
        hashlib.sha256(path.read_bytes()).hexdigest(),
        path.stat().st_mtime_ns,
        wal.exists(),
        hashlib.sha256(wal.read_bytes()).hexdigest() if wal.exists() else None,
    )


def test_schema18_migrates_remote_actions_without_touching_legacy_actions(
    tmp_path: Path,
) -> None:
    path = tmp_path / "schema18.sqlite"
    store = StateStore(path)
    context_id = _context(store)
    assert store.begin_management_context_action(
        context_id, "raw", "legacy-inbound"
    ).status == "claimed"
    store.close()
    _set_schema(path, 18, drop_remote=True)

    migrated = StateStore(path)
    try:
        assert migrated._connection.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0] == str(SCHEMA_VERSION)
        columns = {
            row[1]
            for row in migrated._connection.execute(
                "PRAGMA table_info(remote_control_actions)"
            )
        }
        assert {
            "context_id", "action", "request_hash", "inbound_message_id",
            "created_at", "updated_at", "attempt_count", "claimed_at",
            "submitted_at", "succeeded_at", "rejected_at", "uncertain_at",
            "result_json", "last_error_code",
        } <= columns
        legacy = migrated.management_context_action(context_id, "raw")
        assert legacy is not None and legacy["claimed_at"] is not None
        assert migrated.stats()["remote_control_actions"] == 0
        # schema18 -> 19 creates the expanded typed CHECK in one migration;
        # every action must be insertable without changing the table shape.
        for index, action in enumerate(sorted(REMOTE_CONTROL_ACTIONS), start=1):
            request_hash = hashlib.sha256(
                f"schema19:{action}".encode("utf-8")
            ).hexdigest()
            assert migrated.begin_remote_control_action(
                context_id,
                action,
                request_hash,
                f"migration-{index}",
                now=100 + index,
            ).status == "claimed"
            assert migrated.release_remote_control_action(
                context_id,
                action,
                request_hash,
                "migration_check",
                now=200 + index,
            ) is True
        assert migrated.stats()["remote_control_actions"] == len(REMOTE_CONTROL_ACTIONS)
    finally:
        migrated.close()


def test_schema19_migrates_current_thread_binding_and_keeps_owner_chat_isolated(
    tmp_path: Path,
) -> None:
    path = tmp_path / "schema19-current-thread.sqlite"
    store = StateStore(path)
    store.close()
    _set_schema(path, 19)
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TABLE management_current_bindings")
        connection.commit()
    finally:
        connection.close()

    migrated = StateStore(path)
    try:
        assert migrated._connection.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0] == str(SCHEMA_VERSION)
        columns = {
            row[1]
            for row in migrated._connection.execute(
                "PRAGMA table_info(management_current_bindings)"
            )
        }
        assert {
            "sender_id", "chat_id", "thread_id", "display_title",
            "created_at", "updated_at", "expires_at",
        } <= columns

        migrated.set_current_thread(
            "ou_owner", "oc_private", "thread-a", "安全标题", now=100
        )
        migrated.set_current_thread(
            "ou_owner", "oc_other", "thread-b", "另一个标题", now=100
        )
        migrated.set_current_thread(
            "ou_other", "oc_private", "thread-c", "第三个标题", now=100
        )
        assert migrated.current_thread("ou_owner", "oc_private", now=101)["thread_id"] == "thread-a"
        assert migrated.current_thread("ou_owner", "oc_other", now=101)["thread_id"] == "thread-b"
        assert migrated.current_thread("ou_other", "oc_private", now=101)["thread_id"] == "thread-c"
        assert migrated.clear_current_thread("ou_owner", "oc_private") is True
        assert migrated.current_thread("ou_owner", "oc_private", now=101) is None
        assert migrated.current_thread("ou_owner", "oc_other", now=101) is not None
    finally:
        migrated.close()


def test_pending_target_selection_is_atomically_consumed_once(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "target-selection.sqlite")
    try:
        context_id = store.create_management_context(
            "remote_control_menu",
            {"selection_mode": "remote_control", "pending_command": "/goal"},
            sender_id="ou_owner",
            chat_id="oc_private",
            now=100,
        )
        assert store.claim_management_target_selection(context_id, now=101) is True
        assert store.claim_management_target_selection(context_id, now=102) is False
        # A derived project-list chain uses a separate marker on the same
        # root context: the range choice and the pending command are distinct
        # one-shot stages, while duplicates at either stage are rejected.
        assert (
            store.claim_management_target_selection(
                context_id, marker="pending", now=103
            )
            is True
        )
        assert (
            store.claim_management_target_selection(
                context_id, marker="pending", now=104
            )
            is False
        )
        record = store.management_context_record_for_message("missing", now=102)
        assert record is None
        payload = store._connection.execute(
            "SELECT payload_json FROM management_contexts WHERE context_id=?",
            (context_id,),
        ).fetchone()[0]
        assert "_target_selection_claimed_at" in payload
        assert "_pending_command_claimed_at" in payload
        assert "/goal" in payload
    finally:
        store.close()


def test_remote_action_state_machine_reject_retry_and_terminal_boundaries(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "remote.sqlite")
    context_id = _context(store)
    request_hash = "A" * 64
    try:
        assert REMOTE_CONTROL_ACTIONS == {
            "goal_set", "goal_clear", "plan_start", "skill_start",
            "compact_start", "fork_start", "review_start", "feedback_upload",
            "model_set", "personality_set", "reasoning_set", "fast_toggle",
            "memories_set", "monitor_auto_set",
        }
        claimed = store.begin_remote_control_action(
            context_id, "goal_set", request_hash, "in-1", now=10
        )
        assert claimed.status == "claimed"
        assert claimed.attempt_count == 1
        # Success cannot be recorded before the official write boundary.
        assert store.complete_remote_control_action(
            context_id, "goal_set", request_hash, result={"ok": True}, now=11
        ) is False
        assert store.mark_remote_control_action_submitted(
            context_id, "goal_set", request_hash, now=12
        ) is True
        assert store.complete_remote_control_action(
            context_id, "goal_set", request_hash, result={"ok": True}, now=13
        ) is True
        row = store.remote_control_action(context_id, "goal_set", request_hash)
        assert row is not None
        assert row["state"] == "succeeded"
        assert row["result_json"] == '{"ok":true}'
        assert store.begin_remote_control_action(
            context_id, "goal_set", request_hash, "in-duplicate", now=14
        ).status == "succeeded"

        rejected_hash = "b" * 64
        assert store.begin_remote_control_action(
            context_id, "goal_clear", rejected_hash, "in-reject", now=20
        ).status == "claimed"
        assert store.release_remote_control_action(
            context_id, "goal_clear", rejected_hash, "official_rejected", now=21
        ) is True
        retry = store.begin_remote_control_action(
            context_id, "goal_clear", rejected_hash, "in-retry", now=22
        )
        assert retry.status == "claimed" and retry.attempt_count == 2
        assert store.mark_remote_control_action_submitted(
            context_id, "goal_clear", rejected_hash, now=23
        ) is True
        # A generic exception after submission is not a safe retry.
        assert store.release_remote_control_action(
            context_id, "goal_clear", rejected_hash, "unsafe", now=24
        ) is False
        assert store.mark_remote_control_action_uncertain(
            context_id, "goal_clear", rejected_hash, "result_unknown", now=25
        ) is True
        assert store.begin_remote_control_action(
            context_id, "goal_clear", rejected_hash, "in-unknown-replay", now=26
        ).status == "uncertain"
    finally:
        store.close()


@pytest.mark.parametrize("action", sorted(REMOTE_CONTROL_ACTIONS))
def test_every_typed_remote_action_has_success_and_unknown_boundaries(
    tmp_path: Path, action: str
) -> None:
    """每个官方写动作都经过同一原子 claim/submit/终态门禁。"""

    store = StateStore(tmp_path / f"{action}.sqlite")
    success_context = _context(store, name=f"{action}-success")
    unknown_context = _context(store, name=f"{action}-unknown")
    success_hash = hashlib.sha256(f"{action}:success".encode()).hexdigest()
    unknown_hash = hashlib.sha256(f"{action}:unknown".encode()).hexdigest()
    try:
        claimed = store.begin_remote_control_action(
            success_context, action, success_hash, "in-success", now=10
        )
        assert claimed.status == "claimed"
        assert store.complete_remote_control_action(
            success_context, action, success_hash, result={"ok": True}, now=11
        ) is False
        assert store.mark_remote_control_action_submitted(
            success_context, action, success_hash, now=12
        ) is True
        assert store.complete_remote_control_action(
            success_context, action, success_hash, result={"ok": True}, now=13
        ) is True
        assert store.remote_control_action(
            success_context, action, success_hash
        )["state"] == "succeeded"
        assert store.begin_remote_control_action(
            success_context, action, success_hash, "in-duplicate", now=14
        ).status == "succeeded"

        assert store.begin_remote_control_action(
            unknown_context, action, unknown_hash, "in-unknown", now=20
        ).status == "claimed"
        assert store.mark_remote_control_action_submitted(
            unknown_context, action, unknown_hash, now=21
        ) is True
        assert store.mark_remote_control_action_uncertain(
            unknown_context, action, unknown_hash, "result_unknown", now=22
        ) is True
        assert store.begin_remote_control_action(
            unknown_context, action, unknown_hash, "in-replay", now=23
        ).status == "uncertain"
    finally:
        store.close()


def test_remote_action_claim_is_atomic_across_store_connections(tmp_path: Path) -> None:
    path = tmp_path / "concurrent.sqlite"
    owner = StateStore(path)
    context_id = _context(owner)
    owner.close()
    stores = [StateStore(path), StateStore(path)]
    try:
        def begin(index: int):
            return stores[index].begin_remote_control_action(
                context_id,
                "plan_start",
                "c" * 64,
                f"in-concurrent-{index}",
                now=100,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(begin, range(2)))
        assert [item.status for item in results].count("claimed") == 1
        assert [item.status for item in results].count("busy") == 1
        row = stores[0].remote_control_action(context_id, "plan_start", "c" * 64)
        assert row is not None and row["attempt_count"] == 1
    finally:
        for store in stores:
            store.close()


def test_remote_action_restart_releases_before_submit_and_freezes_after_submit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "restart.sqlite"
    store = StateStore(path)
    before_context = _context(store, name="before")
    after_context = _context(store, name="after")
    before_hash = "d" * 64
    after_hash = "e" * 64
    assert store.begin_remote_control_action(
        before_context, "skill_start", before_hash, "in-before", now=200
    ).status == "claimed"
    assert store.begin_remote_control_action(
        after_context, "skill_start", after_hash, "in-after", now=200
    ).status == "claimed"
    assert store.mark_remote_control_action_submitted(
        after_context, "skill_start", after_hash, now=201
    ) is True
    store.close()

    reopened = StateStore(path)
    try:
        assert reopened.recover_interrupted_remote_control_actions(now=300) == {
            "unsubmitted_released": 1,
            "submitted_uncertain": 1,
        }
        before = reopened.remote_control_action(
            before_context, "skill_start", before_hash
        )
        after = reopened.remote_control_action(after_context, "skill_start", after_hash)
        assert before is not None and before["state"] == "rejected"
        assert after is not None and after["state"] == "uncertain"
        assert reopened.begin_remote_control_action(
            before_context, "skill_start", before_hash, "in-before-retry", now=301
        ).status == "claimed"
        assert reopened.begin_remote_control_action(
            after_context, "skill_start", after_hash, "in-after-retry", now=301
        ).status == "uncertain"
    finally:
        reopened.close()


def test_remote_action_rejects_invalid_action_hash_and_result(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "invalid.sqlite")
    context_id = _context(store)
    try:
        with pytest.raises(ValueError, match="remote control action"):
            store.begin_remote_control_action(context_id, "raw", "f" * 64, "in")
        with pytest.raises(ValueError, match="request_hash"):
            store.begin_remote_control_action(context_id, "goal_set", "bad", "in")
        with pytest.raises(ValueError, match="合法 JSON"):
            store._remote_result_json("not-json")
        with pytest.raises(ValueError, match="result 和 result_json"):
            store.complete_remote_control_action(
                context_id,
                "goal_set",
                "f" * 64,
                result={"ok": True},
                result_json="{}",
            )
        with pytest.raises(sqlite3.IntegrityError):
            store._connection.execute(
                """
                INSERT INTO remote_control_actions(
                    context_id, action, request_hash, inbound_message_id,
                    created_at, updated_at, attempt_count
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (context_id, "goal_set", "not-a-hash", "in", 1, 1, 0),
            )
    finally:
        store.close()


def test_schema18_read_only_reports_zero_without_creating_remote_table(
    tmp_path: Path,
) -> None:
    path = tmp_path / "readonly18.sqlite"
    StateStore(path).close()
    _set_schema(path, 18, drop_remote=True)
    before = _file_state(path)
    state = StateStore.open_read_only(path)
    try:
        assert state.stats()["remote_control_actions"] == 0
        assert state.stats()["remote_control_pending"] == 0
    finally:
        state.close()
    assert _file_state(path) == before
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='remote_control_actions'"
        ).fetchone() is None
    finally:
        connection.close()


def test_schema19_read_only_rejects_missing_remote_table_without_repair(
    tmp_path: Path,
) -> None:
    path = tmp_path / "missing-remote.sqlite"
    StateStore(path).close()
    _set_schema(path, 19, drop_remote=True)
    before = _file_state(path)
    with pytest.raises(StateError, match="remote_control_actions"):
        StateStore.open_read_only(path)
    assert _file_state(path) == before
