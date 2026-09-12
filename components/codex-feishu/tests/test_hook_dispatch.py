from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from progress_wx import hook_dispatch
from progress_wx import state as state_module
from progress_wx.state import SCHEMA_VERSION, StateError, enqueue_hook_payload_only


def _prepare_hook_database(path: Path, version: int) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            f"""
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO meta(key, value) VALUES('schema_version', '{version}');
            CREATE TABLE hook_events(
                event_key TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                consumed_at INTEGER
            );
            CREATE TABLE hook_sentinel(value TEXT NOT NULL);
            INSERT INTO hook_sentinel(value) VALUES('unchanged');
            """
        )
        connection.commit()
    finally:
        connection.close()


def test_main_uses_explicit_custom_config(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "custom.yaml"
    database = tmp_path / "custom.sqlite"
    config_path.write_text(
        "service:\n  database: custom.sqlite\n",
        encoding="utf-8",
    )
    _prepare_hook_database(database, 15)
    forwarded: list[str] = []
    monkeypatch.setattr(
        hook_dispatch,
        "forward_original",
        lambda raw: forwarded.append(raw) or True,
    )
    raw = json.dumps(
        {
            "type": "agent-turn-complete",
            "thread-id": "thread-custom",
            "turn-id": "turn-custom",
            "last-assistant-message": "结构化完成摘要",
        }
    )

    assert hook_dispatch.main(["--config", str(config_path), raw]) == 0
    # Codex/宿主重试同一 notify 时应当是幂等成功，不能重复创建队列事件。
    assert hook_dispatch.main(["--config", str(config_path), raw]) == 0
    assert forwarded == [raw, raw]
    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone() == ("15",)
        row = connection.execute(
            "SELECT event_key, payload_json FROM hook_events"
        ).fetchone()
        assert row is not None
        assert row[0] == "thread-custom:turn-custom:completed"
        assert json.loads(row[1]) == {
            "type": "agent-turn-complete",
            "thread-id": "thread-custom",
            "turn-id": "turn-custom",
            "last-assistant-message": "结构化完成摘要",
        }
        assert connection.execute("SELECT value FROM hook_sentinel").fetchone() == (
            "unchanged",
        )
    finally:
        connection.close()


def test_main_rejects_malformed_or_non_completion_notify_without_queue(
    tmp_path: Path, monkeypatch
) -> None:
    """非法 JSON、非对象和非完成事件不能污染指定状态库。"""

    config_path = tmp_path / "custom.yaml"
    database = tmp_path / "custom.sqlite"
    config_path.write_text(
        f"service:\n  database: {database.as_posix()}\n",
        encoding="utf-8",
    )
    forwarded: list[str] = []
    errors: list[str] = []
    monkeypatch.setattr(
        hook_dispatch,
        "forward_original",
        lambda raw: forwarded.append(raw) or False,
    )
    monkeypatch.setattr(hook_dispatch, "_record_error", errors.append)

    invalid_inputs = [
        "not-json",
        "[]",
        json.dumps({"type": "turn-ended", "thread-id": "t", "turn-id": "u"}),
        json.dumps({"type": "agent-turn-complete", "thread-id": "t"}),
    ]
    for raw in invalid_inputs:
        assert hook_dispatch.main(["--config", str(config_path), raw]) == 1

    assert forwarded == invalid_inputs
    assert errors
    assert not database.exists()


@pytest.mark.parametrize("version", [15, 16])
def test_enqueue_only_accepts_existing_compatible_schema_without_migration(
    tmp_path: Path, version: int
) -> None:
    database = tmp_path / f"schema-{version}.sqlite"
    _prepare_hook_database(database, version)
    before = sqlite3.connect(database)
    try:
        schema_before = before.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
    finally:
        before.close()

    payload = {
        "type": "agent-turn-complete",
        "thread-id": "thread-compatible",
        "turn-id": f"turn-{version}",
    }
    assert enqueue_hook_payload_only(database, payload, now=1234)
    assert not enqueue_hook_payload_only(database, payload, now=5678)

    after = sqlite3.connect(database)
    try:
        assert after.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone() == (str(version),)
        assert after.execute(
            "SELECT created_at, COUNT(*) FROM hook_events"
        ).fetchone() == (1234, 1)
        assert after.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall() == schema_before
    finally:
        after.close()


def test_enqueue_only_rejects_missing_bad_or_future_database_without_repair(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.sqlite"
    payload = {
        "type": "agent-turn-complete",
        "thread-id": "thread",
        "turn-id": "turn",
    }
    with pytest.raises(StateError, match="尚未由服务初始化"):
        enqueue_hook_payload_only(missing, payload)
    assert not missing.exists()

    bad = tmp_path / "bad.sqlite"
    connection = sqlite3.connect(bad)
    connection.executescript(
        "CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);"
        "INSERT INTO meta VALUES('schema_version','16');"
        "CREATE TABLE hook_events(event_key TEXT PRIMARY KEY,payload_json TEXT NOT NULL,created_at INTEGER NOT NULL);"
    )
    connection.close()
    with pytest.raises(StateError, match="缺少字段"):
        enqueue_hook_payload_only(bad, payload)
    check = sqlite3.connect(bad)
    try:
        assert {
            row[1] for row in check.execute("PRAGMA table_info(hook_events)")
        } == {"event_key", "payload_json", "created_at"}
    finally:
        check.close()

    future = tmp_path / "future.sqlite"
    _prepare_hook_database(future, SCHEMA_VERSION + 1)
    with pytest.raises(StateError, match="高于本程序支持"):
        enqueue_hook_payload_only(future, payload)


def test_enqueue_only_waits_for_short_write_lock_without_migrating(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "locked.sqlite"
    _prepare_hook_database(database, 16)
    monkeypatch.setattr(state_module, "_SQLITE_LOCK_WAIT_SECONDS", 1.0)
    blocker = sqlite3.connect(database)
    blocker.execute("BEGIN IMMEDIATE")
    result: list[object] = []

    def invoke() -> None:
        try:
            result.append(
                enqueue_hook_payload_only(
                    database,
                    {
                        "type": "agent-turn-complete",
                        "thread-id": "thread-lock",
                        "turn-id": "turn-lock",
                    },
                )
            )
        except BaseException as exc:  # pragma: no cover - assertion reports exact failure
            result.append(exc)

    worker = threading.Thread(target=invoke)
    worker.start()
    time.sleep(0.1)
    blocker.rollback()
    blocker.close()
    worker.join(timeout=2)
    assert result == [True]


def test_previous_notify_wrapper_requires_strict_trailing_json_argv() -> None:
    command = [
        "wrapper.exe",
        "turn-ended",
        "--previous-notify",
        '["python.exe", "old.py"]',
    ]
    assert hook_dispatch._previous_notify_wrapper(command) == (
        ["wrapper.exe", "turn-ended"],
        ["python.exe", "old.py"],
    )
    assert hook_dispatch._previous_notify_wrapper([*command, "extra"]) is None
    assert hook_dispatch._previous_notify_wrapper(
        ["wrapper.exe", "--previous-notify", "{}"]
    ) is None


def test_original_notify_skips_duplicate_outer_wrapper(
    tmp_path: Path, monkeypatch
) -> None:
    installed = ["python.exe", "new-hook.py", "--config", "new.yaml"]
    older = ["python.exe", "old-hook.py"]
    wrapper = ["computer-use.exe", "turn-ended"]
    codex_config = tmp_path / "config.toml"
    codex_config.write_text(
        "notify = "
        + json.dumps(
            [*wrapper, "--previous-notify", json.dumps(installed)],
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    install_state = tmp_path / "install-state.json"
    install_state.write_text(
        json.dumps(
            {
                "config_path": str(codex_config),
                "installed_notify": installed,
                "original_notify": [
                    *wrapper,
                    "--previous-notify",
                    json.dumps(older),
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(hook_dispatch, "INSTALL_STATE_PATH", install_state)

    assert hook_dispatch._load_original_notify() == older


def test_original_notify_keeps_different_outer_wrapper(
    tmp_path: Path, monkeypatch
) -> None:
    installed = ["python.exe", "new-hook.py"]
    original = ["wrapper-v1.exe", "--previous-notify", '["old.exe"]']
    codex_config = tmp_path / "config.toml"
    codex_config.write_text(
        "notify = "
        + json.dumps(
            ["wrapper-v2.exe", "--previous-notify", json.dumps(installed)]
        )
        + "\n",
        encoding="utf-8",
    )
    install_state = tmp_path / "install-state.json"
    install_state.write_text(
        json.dumps(
            {
                "config_path": str(codex_config),
                "installed_notify": installed,
                "original_notify": original,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(hook_dispatch, "INSTALL_STATE_PATH", install_state)

    assert hook_dispatch._load_original_notify() == original
