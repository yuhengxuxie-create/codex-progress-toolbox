"""当前 Codex 会话绑定的隔离状态层回归测试。

这些测试只操作 pytest 临时目录中的合成 SQLite 文件，不读取生产配置，
也不调用 Feishu/Codex 服务或命令行入口。
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from progress_wx.state import (
    SCHEMA_VERSION,
    ManagementContextRecord,
    StateError,
    StateStore,
)


def _set_schema_version(
    path: Path,
    version: int,
    *,
    drop_current_bindings: bool = False,
) -> None:
    connection = sqlite3.connect(path)
    try:
        if drop_current_bindings:
            connection.execute("DROP TABLE IF EXISTS management_current_bindings")
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


def test_schema19_migrates_current_binding_table_without_touching_legacy_context(
    tmp_path: Path,
) -> None:
    path = tmp_path / "schema19-migration.sqlite"
    store = StateStore(path)
    context_id = store.create_management_context(
        "menu", {"item": "status"}, sender_id="owner-a", chat_id="chat-a",
        ttl_days=30, now=100,
    )
    store.bind_management_messages(context_id, ["message-a"], now=101)
    store.close()

    # schema19 has no current-thread table.  This is a synthetic downgrade;
    # no production database or migration command is involved.
    _set_schema_version(path, 19, drop_current_bindings=True)

    migrated = StateStore(path)
    try:
        version = migrated._connection.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0]
        assert version == str(SCHEMA_VERSION)
        columns = {
            str(row[1])
            for row in migrated._connection.execute(
                "PRAGMA table_info(management_current_bindings)"
            )
        }
        assert columns == {
            "sender_id", "chat_id", "thread_id", "display_title",
            "created_at", "updated_at", "expires_at",
        }
        record = migrated.management_context_record_for_message(
            "message-a", now=100
        )
        assert record is not None
        assert record.created_at == 100
        assert record.expires_at == 100 + 30 * 86_400
        assert migrated.current_thread("owner-a", "chat-a", now=100) is None

        migrated.set_current_thread(
            "owner-a", "chat-a", "thread-a", "A safe title", now=200
        )
        assert migrated.current_thread("owner-a", "chat-a", now=200) == {
            "thread_id": "thread-a",
            "title": "A safe title",
            "created_at": 200,
            "updated_at": 200,
            "expires_at": 200 + 30 * 86_400,
        }
    finally:
        migrated.close()


def test_current_thread_isolated_by_owner_and_chat_and_switch_is_atomic(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "isolation.sqlite")
    try:
        store.set_current_thread(
            "owner-a", "chat-a", "thread-a", "First title", now=100
        )
        assert store.current_thread("owner-b", "chat-a", now=100) is None
        assert store.current_thread("owner-a", "chat-b", now=100) is None
        assert store.clear_current_thread("owner-b", "chat-a") is False
        assert store.current_thread("owner-a", "chat-a", now=100)["thread_id"] == "thread-a"

        store.set_current_thread(
            "owner-a", "chat-a", "thread-b", "Second title", now=200
        )
        switched = store.current_thread("owner-a", "chat-a", now=200)
        assert switched == {
            "thread_id": "thread-b",
            "title": "Second title",
            # created_at is the binding row's original timestamp; switching
            # updates the target/expiry without changing row identity.
            "created_at": 100,
            "updated_at": 200,
            "expires_at": 200 + 30 * 86_400,
        }
        assert store.clear_current_thread(
            "owner-a", "chat-a", expected_thread_id="thread-a"
        ) is False
        assert store.current_thread("owner-a", "chat-a", now=200) == switched
        assert store.clear_current_thread(
            "owner-a", "chat-a", expected_thread_id="thread-b"
        ) is True
        assert store.clear_current_thread("owner-a", "chat-a") is False
        assert store.current_thread("owner-a", "chat-a", now=200) is None
    finally:
        store.close()


def test_current_thread_ttl_fails_closed_at_expiry_boundary(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "ttl.sqlite")
    try:
        store.set_current_thread(
            "owner-a", "chat-a", "thread-a", "Title", ttl_days=1, now=100
        )
        assert store.current_thread("owner-a", "chat-a", now=100 + 86_400 - 1)
        assert store.current_thread("owner-a", "chat-a", now=100 + 86_400) is None
        # Expiry is read-time fail-closed; clear can still remove the stale row.
        assert store.clear_current_thread("owner-a", "chat-a") is True
    finally:
        store.close()


def test_current_thread_restart_persists_only_safe_display_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "restart.sqlite"
    store = StateStore(path)
    store.set_current_thread(
        "owner-a", "chat-a", "thread-a", r"D:\Private\rollout", now=100
    )
    assert store.stats()["management_current_bindings"] == 1
    store.close()

    restarted = StateStore(path)
    try:
        binding = restarted.current_thread("owner-a", "chat-a", now=100)
        assert binding is not None
        assert binding["thread_id"] == "thread-a"
        # Path-like titles are dropped rather than persisted or returned.
        assert binding["title"] == ""
        assert restarted.stats()["management_current_bindings"] == 1
        columns = {
            str(row[1])
            for row in restarted._connection.execute(
                "PRAGMA table_info(management_current_bindings)"
            )
        }
        assert not {"path", "cwd", "group", "project"} & columns
    finally:
        restarted.close()


def test_management_context_record_keeps_backward_constructor_and_timestamps(
    tmp_path: Path,
) -> None:
    legacy = ManagementContextRecord("context", "menu", {}, "owner", "chat")
    assert legacy.created_at == 0
    assert legacy.expires_at > 0

    store = StateStore(tmp_path / "context-timestamps.sqlite")
    try:
        context_id = store.create_management_context(
            "menu", {"item": "status"}, sender_id="owner", chat_id="chat",
            ttl_days=2, now=1_000,
        )
        store.bind_management_messages(context_id, ["message"], now=1_001)
        record = store.management_context_record_for_message("message", now=1_000)
        assert record is not None
        assert record.created_at == 1_000
        assert record.expires_at == 1_000 + 2 * 86_400
    finally:
        store.close()


def test_schema19_read_only_binding_api_fails_closed_without_repair(tmp_path: Path) -> None:
    path = tmp_path / "schema19-read-only.sqlite"
    store = StateStore(path)
    store.close()
    _set_schema_version(path, 19, drop_current_bindings=True)
    before = _file_state(path)

    read_only = StateStore.open_read_only(path)
    try:
        assert read_only.current_thread("owner-a", "chat-a", now=100) is None
        assert read_only.stats()["management_current_bindings"] == 0
        with pytest.raises(StateError, match="只读"):
            read_only.set_current_thread("owner-a", "chat-a", "thread-a", now=100)
        with pytest.raises(StateError, match="只读"):
            read_only.clear_current_thread("owner-a", "chat-a")
    finally:
        read_only.close()

    assert _file_state(path) == before
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='management_current_bindings'"
        ).fetchone() is None
    finally:
        connection.close()


def test_schema20_read_only_binding_api_reads_without_writes(tmp_path: Path) -> None:
    path = tmp_path / "schema20-read-only.sqlite"
    store = StateStore(path)
    store.set_current_thread("owner-a", "chat-a", "thread-a", "Title", now=100)
    store.close()
    before = _file_state(path)

    read_only = StateStore.open_read_only(path)
    try:
        assert read_only.current_thread("owner-a", "chat-a", now=100)["thread_id"] == "thread-a"
        with pytest.raises(StateError, match="只读"):
            read_only.set_current_thread("owner-a", "chat-a", "thread-b", now=200)
        with pytest.raises(StateError, match="只读"):
            read_only.clear_current_thread("owner-a", "chat-a")
    finally:
        read_only.close()

    assert _file_state(path) == before


def test_future_schema_read_only_rejects_without_writing(tmp_path: Path) -> None:
    path = tmp_path / "future-read-only.sqlite"
    store = StateStore(path)
    store.close()
    _set_schema_version(path, SCHEMA_VERSION + 1)
    before = _file_state(path)

    with pytest.raises(StateError, match="高于"):
        StateStore.open_read_only(path)

    assert _file_state(path) == before
