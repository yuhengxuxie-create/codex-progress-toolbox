from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from progress_wx import cli
from progress_wx.codex_store import ThreadRecord
from progress_wx.state import SCHEMA_VERSION, StateError, StateStore


def _initialized_database(path: Path) -> None:
    store = StateStore(path)
    store.close()


def _set_schema_version(
    path: Path,
    version: int,
    *,
    drop_schema17: bool = False,
    drop_schema18: bool = False,
    drop_schema20: bool = False,
) -> None:
    connection = sqlite3.connect(path)
    try:
        if drop_schema17:
            connection.execute("DROP TABLE notification_summary_deliveries")
        if drop_schema18:
            for table in (
                "reset_alert_deliveries",
                "reset_alert_events",
                "reset_alert_signals",
                "reset_alert_sources",
                "reset_alert_state",
            ):
                connection.execute(f"DROP TABLE {table}")
        if drop_schema20:
            connection.execute("DROP TABLE management_current_bindings")
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


def _make_schema16_database(path: Path) -> None:
    """构造仅含 schema16 表的合成状态库；不接触生产数据库。"""

    _initialized_database(path)
    _set_schema_version(path, 16, drop_schema17=True, drop_schema18=True)


def _make_compatible_database(path: Path, schema_version: int) -> None:
    _initialized_database(path)
    if schema_version == 16:
        _set_schema_version(path, 16, drop_schema17=True, drop_schema18=True)
    elif schema_version == 17:
        _set_schema_version(path, 17, drop_schema18=True)
    elif schema_version == 19:
        _set_schema_version(path, 19, drop_schema20=True)
    elif schema_version != SCHEMA_VERSION:
        raise AssertionError(f"unsupported synthetic schema {schema_version}")


def _assert_unchanged(path: Path, before: tuple[str, int, bool, str | None]) -> None:
    assert _file_state(path) == before


@pytest.mark.parametrize("schema_version", [16, 17, 19, SCHEMA_VERSION])
def test_status_compatible_schema_is_hard_read_only(
    monkeypatch, tmp_path: Path, capsys, schema_version: int
) -> None:
    path = tmp_path / f"status-{schema_version}.sqlite"
    _make_compatible_database(path, schema_version)
    before = _file_state(path)
    config = SimpleNamespace(
        service=SimpleNamespace(database=path, pid_file=tmp_path / "service.pid")
    )
    monkeypatch.setattr(cli, "_config", lambda _args, ready=False: config)
    monkeypatch.setattr(cli, "read_pid_file", lambda _path: None)

    assert cli._status(SimpleNamespace()) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["running"] is False
    assert payload["state"]["notification_summary_deliveries"] == 0
    assert payload["state"]["notification_media_deliveries"] == 0
    _assert_unchanged(path, before)


@pytest.mark.parametrize(
    (
        "instance_state",
        "running",
        "health",
        "expected_exit",
        "expected_service_state",
        "expected_channel_state",
    ),
    [
        (
            {"pid": 4101, "creation_time": 101, "channel_health_schema_version": 1},
            True,
            {
                "channel_state": "online",
                "online": True,
                "ever_connected": True,
                "consecutive_failures": 0,
                "updated_at": 1_800_000_000,
            },
            0,
            "running",
            "online",
        ),
        (
            {"pid": 4102, "creation_time": 102, "channel_health_schema_version": 1},
            True,
            {
                "channel_state": "connecting",
                "online": False,
                "ever_connected": False,
                "consecutive_failures": 1,
                "next_retry_at": 1_800_000_005,
            },
            0,
            "connecting",
            "connecting",
        ),
        (
            {"pid": 4103, "creation_time": 103, "channel_health_schema_version": 1},
            True,
            {
                "channel_state": "offline",
                "online": False,
                "ever_connected": True,
                "consecutive_failures": 3,
                "last_failure_class": "transient",
                "last_failure_type": "TimeoutError",
            },
            0,
            "reconnecting",
            "offline",
        ),
        (None, False, None, 1, "stopped", "stopped"),
    ],
)
def test_status_freezes_service_and_channel_state_contract(
    monkeypatch,
    tmp_path: Path,
    capsys,
    instance_state,
    running: bool,
    health,
    expected_exit: int,
    expected_service_state: str,
    expected_channel_state: str,
) -> None:
    path = tmp_path / "status-contract.sqlite"
    _initialized_database(path)
    before = _file_state(path)
    config = SimpleNamespace(
        service=SimpleNamespace(database=path, pid_file=tmp_path / "service.pid")
    )
    monkeypatch.setattr(cli, "_config", lambda _args, ready=False: config)
    monkeypatch.setattr(cli, "read_pid_file", lambda _path: instance_state)
    monkeypatch.setattr(cli, "instance_running", lambda _path: running)
    monkeypatch.setattr(
        cli,
        "read_channel_health",
        lambda _path, *, instance_state=None: health,
    )

    assert cli._status(SimpleNamespace()) == expected_exit
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["running"] is running
    assert payload["pid"] == (instance_state["pid"] if running else None)
    assert payload["service_state"] == expected_service_state
    assert payload["channel"]["state"] == expected_channel_state
    assert payload["channel"]["online"] is (expected_channel_state == "online")
    assert payload["channel"]["ever_connected"] is bool(
        health and health.get("ever_connected")
    )
    assert set(payload["channel"]) == {
        "state",
        "online",
        "ever_connected",
        "consecutive_failures",
        "last_failure_class",
        "last_failure_type",
        "next_retry_at",
        "updated_at",
    }
    _assert_unchanged(path, before)


@pytest.mark.parametrize(
    ("instance_state", "expected_service_state"),
    [
        (
            {"pid": 4201, "creation_time": 201, "channel_health_schema_version": 1},
            "connecting",
        ),
        ({"pid": 4202, "creation_time": 202}, "running"),
    ],
)
def test_status_missing_health_sidecar_distinguishes_new_and_legacy_pid_files(
    monkeypatch,
    tmp_path: Path,
    capsys,
    instance_state,
    expected_service_state: str,
) -> None:
    path = tmp_path / "status-missing-sidecar.sqlite"
    _initialized_database(path)
    before = _file_state(path)
    config = SimpleNamespace(
        service=SimpleNamespace(database=path, pid_file=tmp_path / "service.pid")
    )
    monkeypatch.setattr(cli, "_config", lambda _args, ready=False: config)
    monkeypatch.setattr(cli, "read_pid_file", lambda _path: instance_state)
    monkeypatch.setattr(cli, "instance_running", lambda _path: True)
    monkeypatch.setattr(
        cli,
        "read_channel_health",
        lambda _path, *, instance_state=None: None,
    )

    assert cli._status(SimpleNamespace()) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["service_state"] == expected_service_state
    assert payload["channel"]["state"] == "unknown"
    assert payload["channel"]["online"] is False
    _assert_unchanged(path, before)


@pytest.mark.parametrize("schema_version", [16, 17, 19, SCHEMA_VERSION])
def test_validate_compatible_schema_checks_without_migration(
    monkeypatch, tmp_path: Path, capsys, schema_version: int
) -> None:
    path = tmp_path / f"validate-{schema_version}.sqlite"
    _make_compatible_database(path, schema_version)
    codex_state = tmp_path / "codex-state.sqlite"
    codex_connection = sqlite3.connect(codex_state)
    try:
        codex_connection.execute(
            "CREATE TABLE threads(id TEXT PRIMARY KEY, title TEXT, preview TEXT)"
        )
        codex_connection.commit()
    finally:
        codex_connection.close()
    history = tmp_path / "history.sqlite"
    history.write_bytes(b"synthetic history")
    before = _file_state(path)
    codex_before = _file_state(codex_state)
    history_before = _file_state(history)

    class Config:
        codex = SimpleNamespace(home=tmp_path)
        service = SimpleNamespace(database=path)
        messaging = SimpleNamespace(backend="fake")

        @staticmethod
        def validate_ready() -> None:
            return None

    monkeypatch.setattr(cli, "_config", lambda _args, ready=False: Config())
    monkeypatch.setattr(
        cli.StorePaths,
        "from_codex_home",
        lambda _home: SimpleNamespace(
            state_db=codex_state, history_db=history, session_index=None
        ),
    )

    assert cli._validate(SimpleNamespace()) == 0
    assert "均已就绪" in capsys.readouterr().out
    _assert_unchanged(path, before)
    _assert_unchanged(codex_state, codex_before)
    _assert_unchanged(history, history_before)


def test_validate_rejects_service_database_without_meta_without_repair(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    service_database = tmp_path / "service-without-meta.sqlite"
    connection = sqlite3.connect(service_database)
    try:
        connection.execute("CREATE TABLE unrelated(value TEXT)")
        connection.commit()
    finally:
        connection.close()
    codex_state = tmp_path / "codex-state.sqlite"
    connection = sqlite3.connect(codex_state)
    try:
        connection.execute(
            "CREATE TABLE threads(id TEXT PRIMARY KEY, title TEXT, preview TEXT)"
        )
        connection.commit()
    finally:
        connection.close()
    history = tmp_path / "history.sqlite"
    history.write_bytes(b"synthetic history")
    service_before = _file_state(service_database)
    codex_before = _file_state(codex_state)

    class Config:
        codex = SimpleNamespace(home=tmp_path)
        service = SimpleNamespace(database=service_database)
        messaging = SimpleNamespace(backend="fake")

        @staticmethod
        def validate_ready() -> None:
            return None

    monkeypatch.setattr(cli, "_config", lambda _args, ready=False: Config())
    monkeypatch.setattr(
        cli.StorePaths,
        "from_codex_home",
        lambda _home: SimpleNamespace(
            state_db=codex_state, history_db=history, session_index=None
        ),
    )

    assert cli._validate(SimpleNamespace()) == 2
    assert "FeiShuBOT 状态库只读检查失败" in capsys.readouterr().out
    _assert_unchanged(service_database, service_before)
    _assert_unchanged(codex_state, codex_before)


@pytest.mark.parametrize("schema_version", [16, 17, 19, SCHEMA_VERSION])
def test_list_threads_compatible_schema_does_not_write_recovery_cache(
    monkeypatch, tmp_path: Path, capsys, schema_version: int
) -> None:
    path = tmp_path / f"list-{schema_version}.sqlite"
    _make_compatible_database(path, schema_version)
    before = _file_state(path)
    record = ThreadRecord(
        thread_id="synthetic-thread",
        title="合成会话标题",
        preview="合成预览",
        thread_source="user",
        raw={"title": "合成会话标题"},
        title_source="sqlite_title",
    )

    class Catalog:
        def select_threads(self, *, include_archived=False):
            assert include_archived is True
            return [record]

        def require_readable(self, _operation):
            return None

    config = SimpleNamespace(
        codex=SimpleNamespace(home=tmp_path),
        service=SimpleNamespace(database=path),
    )
    monkeypatch.setattr(cli, "_config", lambda _args, ready=False: config)
    monkeypatch.setattr(cli, "CodexStore", lambda **_kwargs: Catalog())
    monkeypatch.setattr(cli, "_desktop_project_assignments", lambda _home: {})

    assert cli._list_threads(SimpleNamespace(json=True)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["title"] == "合成会话标题"
    _assert_unchanged(path, before)


@pytest.mark.parametrize("schema_version", [16, 17, 19, SCHEMA_VERSION])
def test_monitor_list_compatible_schema_preserves_expired_rows_and_database(
    monkeypatch, tmp_path: Path, capsys, schema_version: int
) -> None:
    path = tmp_path / f"monitor-{schema_version}.sqlite"
    store = StateStore(path)
    store.add_manual_monitor("synthetic-thread", last_activity_at=1)
    store.close()
    if schema_version == 16:
        _set_schema_version(path, 16, drop_schema17=True, drop_schema18=True)
    elif schema_version == 17:
        _set_schema_version(path, 17, drop_schema18=True)
    before = _file_state(path)
    record = ThreadRecord(
        thread_id="synthetic-thread",
        title="合成监测会话",
        thread_source="user",
        raw={"title": "合成监测会话"},
        title_source="sqlite_title",
    )

    class Catalog:
        def select_threads(self, *, include_archived=False):
            assert include_archived is True
            return [record]

        def require_readable(self, _operation):
            return None

    config = SimpleNamespace(
        codex=SimpleNamespace(home=tmp_path),
        service=SimpleNamespace(database=path),
    )
    monkeypatch.setattr(cli, "_config", lambda _args, ready=False: config)
    monkeypatch.setattr(cli, "CodexStore", lambda **_kwargs: Catalog())
    monkeypatch.setattr(cli, "_desktop_project_assignments", lambda _home: {})

    assert cli._monitor_list(SimpleNamespace(json=True)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["items"][0]["thread_id"] == "synthetic-thread"
    _assert_unchanged(path, before)


@pytest.mark.parametrize("schema_version", [16, 17, 19, SCHEMA_VERSION])
def test_monitor_settings_query_compatible_schema_is_read_only(
    monkeypatch, tmp_path: Path, capsys, schema_version: int
) -> None:
    path = tmp_path / f"settings-{schema_version}.sqlite"
    _make_compatible_database(path, schema_version)
    before = _file_state(path)
    config = SimpleNamespace(service=SimpleNamespace(database=path))
    monkeypatch.setattr(cli, "_config", lambda _args, ready=False: config)

    assert cli._monitor_settings(SimpleNamespace(auto_enabled=None, json=True)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "schema_version": 1,
        "auto_monitoring_enabled": True,
        "effective_at": None,
    }
    _assert_unchanged(path, before)


def test_schema16_read_only_state_does_not_migrate_or_create_schema17_tables(
    tmp_path: Path,
) -> None:
    # 路径中的空格和 ``#`` 用于回归 Windows SQLite URI 的正确转义。
    path = tmp_path / "schema16 with #.sqlite"
    _initialized_database(path)
    _set_schema_version(path, 16, drop_schema17=True, drop_schema18=True)
    before = _file_state(path)

    state = StateStore.open_read_only(path)
    try:
        stats = state.stats()
        assert state.read_only is True
        assert stats["notification_summary_deliveries"] == 0
        assert stats["notification_media_deliveries"] == 0
        assert stats["notification_summary_pending"] == 0
        assert stats["notification_media_pending"] == 0
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            state._connection.execute("CREATE TABLE should_not_exist(value TEXT)")
    finally:
        state.close()

    assert _file_state(path) == before
    connection = sqlite3.connect(path)
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        connection.close()
    assert "notification_summary_deliveries" not in tables
    assert "notification_media_deliveries" in tables


def test_schema18_read_only_state_is_usable_without_writes(tmp_path: Path) -> None:
    path = tmp_path / "schema18.sqlite"
    _initialized_database(path)
    before = _file_state(path)

    state = StateStore.open_read_only(path)
    try:
        assert state.stats()["notification_summary_deliveries"] == 0
        assert state.pending_hook_count() == 0
    finally:
        state.close()

    assert _file_state(path) == before


def test_schema16_read_only_rejects_missing_schema15_media_outbox_without_repair(
    tmp_path: Path,
) -> None:
    path = tmp_path / "schema16-missing-media.sqlite"
    _make_schema16_database(path)
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TABLE notification_media_deliveries")
        connection.commit()
    finally:
        connection.close()
    before = _file_state(path)

    with pytest.raises(StateError, match="notification_media_deliveries"):
        StateStore.open_read_only(path)

    _assert_unchanged(path, before)


@pytest.mark.parametrize("mutation", ["future", "missing_table", "missing_column"])
def test_read_only_rejects_incompatible_schema_without_repair(
    tmp_path: Path, mutation: str
) -> None:
    path = tmp_path / f"bad-{mutation}.sqlite"
    _initialized_database(path)
    connection = sqlite3.connect(path)
    try:
        if mutation == "future":
            connection.execute(
                "UPDATE meta SET value=? WHERE key='schema_version'",
                (str(SCHEMA_VERSION + 1),),
            )
        elif mutation == "missing_table":
            connection.execute("DROP TABLE monitor_subscriptions")
        else:
            connection.execute(
                "ALTER TABLE monitor_subscriptions RENAME COLUMN origin TO origin_missing"
            )
        connection.commit()
    finally:
        connection.close()
    before = _file_state(path)

    with pytest.raises(StateError, match="(高于|缺少|不兼容)"):
        StateStore.open_read_only(path)

    assert _file_state(path) == before
