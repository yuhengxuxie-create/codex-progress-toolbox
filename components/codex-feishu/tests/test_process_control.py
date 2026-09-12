from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path
import json
import os
import socket
from types import SimpleNamespace

import pytest

import progress_wx.process_control as process_control

from progress_wx.process_control import (
    InstanceError,
    acquire_instance,
    channel_health_path,
    clear_stop_request,
    instance_running,
    loopback_tcp_client_pids,
    process_has_loopback_tcp_connection,
    process_liveness,
    process_session_id,
    read_channel_health,
    release_instance,
    request_stop,
    stop_requested_for,
    stop_request_path,
    write_channel_health,
)


class _FakeWinApiCall:
    def __init__(self, implementation):
        self._implementation = implementation
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self._implementation(*args)


class _FakeKernel32:
    def __init__(self, *, exit_code: int, exit_code_succeeds: bool = True) -> None:
        self.closed_handles: list[int] = []
        self.OpenProcess = _FakeWinApiCall(lambda *_args: 123)
        self.CloseHandle = _FakeWinApiCall(self._close_handle)
        self.GetExitCodeProcess = _FakeWinApiCall(self._get_exit_code)
        self.GetProcessTimes = _FakeWinApiCall(lambda *_args: 0)
        self.QueryFullProcessImageNameW = _FakeWinApiCall(lambda *_args: 0)
        self._exit_code = exit_code
        self._exit_code_succeeds = exit_code_succeeds

    def _close_handle(self, handle: int) -> int:
        self.closed_handles.append(int(handle))
        return 1

    def _get_exit_code(self, _handle: int, result) -> int:
        if not self._exit_code_succeeds:
            return 0
        ctypes.cast(result, ctypes.POINTER(wintypes.DWORD)).contents.value = (
            self._exit_code
        )
        return 1


def test_single_instance_and_cooperative_stop(tmp_path: Path) -> None:
    pid_file = tmp_path / "service.pid"
    state = acquire_instance(pid_file, tmp_path / "config.yaml")
    try:
        assert instance_running(pid_file)
        assert request_stop(pid_file)
        assert stop_request_path(pid_file).is_file()
        request = json.loads(stop_request_path(pid_file).read_text(encoding="utf-8"))
        assert request["version"] == 1
        assert request["pid"] == state["pid"]
        assert request["creation_time"] == state["creation_time"]
        assert stop_requested_for(pid_file, state)
    finally:
        release_instance(pid_file, state)
    assert not pid_file.exists()


def test_channel_health_is_atomic_and_bound_to_exact_process_generation(
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "service.pid"
    state = acquire_instance(
        pid_file,
        tmp_path / "config.yaml",
        metadata={"channel_health_schema_version": 1},
    )
    try:
        assert write_channel_health(
            pid_file,
            {
                "state": "offline",
                "ever_connected": True,
                "consecutive_failures": 3,
                "last_failure_class": "transient",
                "last_failure_type": "OSError",
                "retry_in_seconds": 7.5,
                "last_transition_at": 10.0,
            },
            now=20.0,
        )
        payload = read_channel_health(pid_file, instance_state=state)
        assert payload is not None
        assert payload["schema_version"] == 1
        assert payload["pid"] == state["pid"]
        assert payload["creation_time"] == state["creation_time"]
        assert payload["channel_state"] == "offline"
        assert payload["online"] is False
        assert payload["ever_connected"] is True
        assert payload["next_retry_at"] == 27.5
        assert not list(tmp_path.glob("*.tmp"))

        tampered = dict(payload)
        tampered["creation_time"] = int(state["creation_time"]) + 1
        channel_health_path(pid_file).write_text(
            json.dumps(tampered), encoding="utf-8"
        )
        assert read_channel_health(pid_file, instance_state=state) is None
    finally:
        channel_health_path(pid_file).unlink(missing_ok=True)
        release_instance(pid_file, state)


def test_new_service_generation_removes_stale_channel_health(tmp_path: Path) -> None:
    pid_file = tmp_path / "service.pid"
    channel_health_path(pid_file).write_text("{}", encoding="utf-8")
    state = acquire_instance(pid_file, tmp_path / "config.yaml")
    try:
        assert not channel_health_path(pid_file).exists()
    finally:
        release_instance(pid_file, state)


def test_pid_publish_is_atomic_and_cleans_failed_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "atomic-service.pid"

    def fail_replace(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        source_path, target_path = Path(source), Path(target)
        assert source_path.parent == pid_file.parent
        assert target_path == pid_file
        assert json.loads(source_path.read_text(encoding="utf-8"))["pid"] == os.getpid()
        raise OSError("simulated atomic replace failure")

    monkeypatch.setattr(process_control.os, "replace", fail_replace)
    with pytest.raises(InstanceError, match="原子发布"):
        acquire_instance(pid_file, tmp_path / "config.yaml")

    assert not pid_file.exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_stale_stop_request_never_matches_new_generation(tmp_path: Path) -> None:
    pid_file = tmp_path / "service.pid"
    state = acquire_instance(pid_file, tmp_path / "config.yaml")
    try:
        stale = {
            "version": 1,
            "pid": state["pid"],
            "creation_time": int(state["creation_time"]) + 1,
            "requested_at": 1,
        }
        stop_request_path(pid_file).write_text(json.dumps(stale), encoding="utf-8")
        assert stop_requested_for(pid_file, state) is False
        assert clear_stop_request(pid_file, state) is False
        assert stop_request_path(pid_file).is_file()
    finally:
        stop_request_path(pid_file).unlink(missing_ok=True)
        release_instance(pid_file, state)


def test_expected_generation_never_stops_replacement(tmp_path: Path) -> None:
    pid_file = tmp_path / "service.pid"
    state = acquire_instance(pid_file, tmp_path / "config.yaml")
    try:
        replacement = dict(state)
        replacement["creation_time"] = int(state["creation_time"]) + 1
        assert request_stop(pid_file, expected_state=replacement) is False
        assert not stop_request_path(pid_file).exists()
    finally:
        release_instance(pid_file, state)


def test_instance_metadata_cannot_override_generation(tmp_path: Path) -> None:
    with pytest.raises(InstanceError, match="保留字段"):
        acquire_instance(
            tmp_path / "service.pid",
            tmp_path / "config.yaml",
            metadata={"pid": 123},
        )


def test_pid_file_without_creation_time_is_not_trusted_on_windows(tmp_path: Path) -> None:
    if os.name != "nt":
        return
    pid_file = tmp_path / "legacy.pid"
    pid_file.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    assert instance_running(pid_file) is False


def test_mutex_still_blocks_when_pid_file_is_removed(tmp_path: Path) -> None:
    pid_file = tmp_path / "service.pid"
    state = acquire_instance(pid_file, tmp_path / "config.yaml")
    try:
        pid_file.unlink()
        with pytest.raises(InstanceError, match="单实例锁"):
            acquire_instance(pid_file, tmp_path / "config.yaml")
    finally:
        release_instance(pid_file, state)


def test_process_identity_helpers_distinguish_current_process() -> None:
    assert process_liveness(os.getpid()) == "running"
    assert process_liveness(0) == "absent"
    assert process_session_id(os.getpid()) is not None


@pytest.mark.parametrize(
    ("exit_code", "succeeds", "expected"),
    [
        (259, True, True),
        (0, True, False),
        (0, False, None),
    ],
)
def test_handle_activity_uses_windows_exit_code(
    exit_code: int, succeeds: bool, expected: bool | None
) -> None:
    kernel32 = _FakeKernel32(exit_code=exit_code, exit_code_succeeds=succeeds)
    assert process_control._handle_is_active(kernel32, 123) is expected


def test_queryable_exited_handle_is_not_treated_as_a_live_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name != "nt":
        pytest.skip("Windows process-handle semantics")
    kernel32 = _FakeKernel32(exit_code=0)
    monkeypatch.setattr(
        process_control.ctypes, "windll", SimpleNamespace(kernel32=kernel32)
    )
    monkeypatch.setattr(process_control.ctypes, "WinDLL", lambda *_args, **_kwargs: kernel32)

    assert process_control.process_creation_time(98765) is None
    assert process_control.process_image_path(98765) is None
    assert process_control.process_liveness(98765) == "absent"
    assert kernel32.closed_handles == [123, 123, 123]


def test_unreadable_exit_code_is_not_reported_as_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name != "nt":
        pytest.skip("Windows process-handle semantics")
    kernel32 = _FakeKernel32(exit_code=0, exit_code_succeeds=False)
    monkeypatch.setattr(process_control.ctypes, "WinDLL", lambda *_args, **_kwargs: kernel32)

    assert process_control.process_liveness(98765) == "unknown"
    assert kernel32.closed_handles == [123]


def test_windows_tcp_owner_proof_uses_actual_loopback_connection() -> None:
    if os.name != "nt":
        return
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = int(server.getsockname()[1])
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.connect(("127.0.0.1", port))
            accepted, _address = server.accept()
            with accepted:
                assert process_has_loopback_tcp_connection(os.getpid(), port)
                assert os.getpid() in loopback_tcp_client_pids(port)
