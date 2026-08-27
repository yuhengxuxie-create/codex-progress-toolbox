"""共享 Codex gateway 的进程世代与 fail-closed 标记测试。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import socket

import pytest

import progress_wx.codex_gateway as codex_gateway
from progress_wx.locking import InterprocessMutex
from progress_wx.codex_gateway import (
    CodexGatewayError,
    _gateway_argv,
    _npm_native_codex,
    active_shared_websocket_url,
    authorize_gateway_launch,
    clear_shared_desktop_state,
    gateway_launch_authorization_path,
    recover_owned_gateway_launch,
    register_shared_desktop,
    request_gateway_stop,
    verified_gateway_running,
)
from progress_wx.process_control import (
    process_creation_time,
    process_image_path,
    release_instance,
)
from progress_wx.process_control import request_stop, stop_request_path


URL = "ws://127.0.0.1:6230"
INSTALL_ROOT = Path(r"C:\Program Files\WindowsApps\OpenAI.Codex_test")
DESKTOP_IMAGE = INSTALL_ROOT / "app" / "ChatGPT.exe"
NOT_BEFORE = max(0, int(process_creation_time(os.getpid()) or 0) - 1)


@pytest.fixture(autouse=True)
def _mock_codex_package_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """单元测试进程不是 AppX；登记测试显式模拟真实包身份。"""

    monkeypatch.setattr(
        "progress_wx.codex_gateway.process_package_full_name",
        lambda _pid: INSTALL_ROOT.name,
    )


def _gateway_pid_file(path: Path) -> Path:
    pid_file = path / "gateway.pid"
    pid_file.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "creation_time": process_creation_time(os.getpid()),
                "image_path": os.fspath(process_image_path(os.getpid())),
                "project_root": os.fspath(Path(__file__).resolve().parents[1]),
            }
        ),
        encoding="utf-8",
    )
    return pid_file


def test_explicit_npm_shim_resolves_only_its_native_package_tree(tmp_path: Path) -> None:
    """显式 shim 只允许绑定同目录 npm 包中的唯一原生 CLI。"""

    shim = tmp_path / "bin with spaces" / "codex.cmd"
    native = (
        shim.parent
        / "node_modules"
        / "@openai"
        / "codex"
        / "node_modules"
        / "@openai"
        / "codex-win32-test"
        / "vendor"
        / "x86_64-pc-windows-msvc"
        / "bin"
        / "codex.exe"
    )
    shim.parent.mkdir(parents=True)
    native.parent.mkdir(parents=True)
    shim.write_text("@echo off\n", encoding="utf-8")
    native.write_text("native\n", encoding="utf-8")

    assert _npm_native_codex(os.fspath(shim)) == os.fspath(native.resolve())
    assert _gateway_argv(os.fspath(shim), URL) == [
        os.fspath(native.resolve()),
        "app-server",
        "--listen",
        URL,
    ]


def test_default_codex_does_not_treat_desktop_windowsapps_exe_as_npm_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PATH 命中的 Desktop EXE 不得触发包树或外部目录扫描。"""

    desktop = r"C:\Program Files\WindowsApps\OpenAI.Codex_test\app\resources\codex.EXE"

    def fake_which(name: str) -> str | None:
        return desktop if name == "codex" else None

    monkeypatch.setattr(codex_gateway.shutil, "which", fake_which)
    assert _npm_native_codex() is None
    with pytest.raises(
        codex_gateway.CodexGatewayError,
        match="命中了受保护的 Codex Desktop",
    ):
        _gateway_argv("codex", URL)


def test_gateway_launch_authorization_is_consumed_before_pid_publish(
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "authorized-gateway.pid"
    token = "1" * 64
    authorization = authorize_gateway_launch(pid_file, token)
    authorization_path = gateway_launch_authorization_path(pid_file)
    assert authorization["launch_token_sha256"] == hashlib.sha256(
        token.encode("utf-8")
    ).hexdigest()
    assert token not in authorization_path.read_text(encoding="utf-8")

    state = codex_gateway._acquire_authorized_gateway_instance(
        pid_file=pid_file,
        config_path=tmp_path / "config.yaml",
        launch_token=token,
    )
    try:
        assert pid_file.is_file()
        assert not authorization_path.exists()
        assert state["launch_token_sha256"] == authorization["launch_token_sha256"]
    finally:
        release_instance(pid_file, state)


def test_gateway_launch_authorization_publish_is_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "atomic-authorization.pid"
    authorization_path = gateway_launch_authorization_path(pid_file)
    real_replace = os.replace
    observed: list[tuple[Path, Path]] = []

    def inspect_replace(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        source_path, target_path = Path(source), Path(target)
        assert source_path.parent == authorization_path.parent
        assert target_path == authorization_path
        assert json.loads(source_path.read_text(encoding="utf-8"))["version"] == 1
        observed.append((source_path, target_path))
        real_replace(source_path, target_path)

    monkeypatch.setattr(codex_gateway.os, "replace", inspect_replace)
    authorize_gateway_launch(pid_file, "a" * 64)

    assert observed
    assert authorization_path.is_file()
    assert not list(tmp_path.glob("*.tmp"))


def test_gateway_launch_authorization_failed_replace_leaves_no_partial_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "failed-authorization.pid"

    def fail_replace(_source: object, _target: object) -> None:
        raise OSError("simulated atomic replace failure")

    monkeypatch.setattr(codex_gateway.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated"):
        authorize_gateway_launch(pid_file, "b" * 64)

    assert not gateway_launch_authorization_path(pid_file).exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_gateway_launch_mutex_uses_cross_session_namespace() -> None:
    mutex = InterprocessMutex("gateway-launch:test", global_scope=True)
    assert mutex._name.startswith("Global\\ProgressCheckingWX-")


def test_recovery_cancels_pending_authorization_and_rejects_late_gateway(
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "pending-gateway.pid"
    token = "2" * 64
    authorize_gateway_launch(pid_file, token)

    result = recover_owned_gateway_launch(
        pid_file=pid_file,
        state_file=tmp_path / "shared.json",
        websocket_url=URL,
        launch_token=token,
        stop_timeout_seconds=0.1,
    )
    assert result == {
        "resolved": True,
        "outcome": "pending-authorization-cancelled",
    }
    assert not gateway_launch_authorization_path(pid_file).exists()
    with pytest.raises(CodexGatewayError, match="授权已撤销"):
        codex_gateway._acquire_authorized_gateway_instance(
            pid_file=pid_file,
            config_path=tmp_path / "config.yaml",
            launch_token=token,
        )
    assert not pid_file.exists()


def test_recovery_preserves_other_generation_authorization(tmp_path: Path) -> None:
    pid_file = tmp_path / "other-generation.pid"
    authorize_gateway_launch(pid_file, "3" * 64)
    authorization_path = gateway_launch_authorization_path(pid_file)

    with pytest.raises(CodexGatewayError, match="另一世代"):
        recover_owned_gateway_launch(
            pid_file=pid_file,
            state_file=tmp_path / "shared.json",
            websocket_url=URL,
            launch_token="4" * 64,
            stop_timeout_seconds=0.1,
        )
    assert authorization_path.is_file()


def test_recovery_rejects_other_authorization_before_touching_owned_pid(
    tmp_path: Path,
) -> None:
    pid_file = _gateway_pid_file(tmp_path)
    owned_token = "5" * 64
    other_token = "6" * 64
    gateway = json.loads(pid_file.read_text(encoding="utf-8"))
    gateway["launch_token_sha256"] = hashlib.sha256(
        owned_token.encode("utf-8")
    ).hexdigest()
    pid_file.write_text(json.dumps(gateway), encoding="utf-8")
    authorize_gateway_launch(pid_file, other_token)

    with pytest.raises(CodexGatewayError, match="另一世代"):
        recover_owned_gateway_launch(
            pid_file=pid_file,
            state_file=tmp_path / "shared.json",
            websocket_url=URL,
            launch_token=owned_token,
            stop_timeout_seconds=0.1,
        )

    assert gateway_launch_authorization_path(pid_file).is_file()
    assert not stop_request_path(pid_file).exists()


def test_verified_desktop_and_gateway_activate_shared_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = _gateway_pid_file(tmp_path)
    state_file = tmp_path / "shared.json"
    monkeypatch.setattr("progress_wx.codex_gateway.gateway_healthy", lambda *_a, **_k: True)
    monkeypatch.setattr(
        "progress_wx.codex_gateway.process_image_path",
        lambda _pid: DESKTOP_IMAGE,
    )
    monkeypatch.setattr(
        "progress_wx.codex_gateway.process_session_id", lambda _pid: 7
    )
    monkeypatch.setattr(
        "progress_wx.codex_gateway.process_has_loopback_tcp_connection",
        lambda _pid, _port: True,
    )
    gateway = json.loads(pid_file.read_text(encoding="utf-8"))
    gateway["image_path"] = os.fspath(DESKTOP_IMAGE)
    pid_file.write_text(json.dumps(gateway), encoding="utf-8")

    register_shared_desktop(
        desktop_pid=os.getpid(),
        websocket_url=URL,
        gateway_pid_file=pid_file,
        state_file=state_file,
        install_location=INSTALL_ROOT,
        not_before_filetime=NOT_BEFORE,
    )

    assert (
        active_shared_websocket_url(
            websocket_url=URL,
            gateway_pid_file=pid_file,
            state_file=state_file,
        )
        == URL
    )
    with pytest.raises(CodexGatewayError, match="仍在共享"):
        request_gateway_stop(pid_file, state_file, URL)


def test_stale_desktop_marker_fails_closed_then_can_be_cleared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = _gateway_pid_file(tmp_path)
    state_file = tmp_path / "shared.json"
    monkeypatch.setattr("progress_wx.codex_gateway.gateway_healthy", lambda *_a, **_k: True)
    monkeypatch.setattr(
        "progress_wx.codex_gateway.process_image_path",
        lambda _pid: DESKTOP_IMAGE,
    )
    monkeypatch.setattr(
        "progress_wx.codex_gateway.process_session_id", lambda _pid: 7
    )
    monkeypatch.setattr(
        "progress_wx.codex_gateway.process_has_loopback_tcp_connection",
        lambda _pid, _port: True,
    )
    gateway = json.loads(pid_file.read_text(encoding="utf-8"))
    gateway["image_path"] = os.fspath(DESKTOP_IMAGE)
    pid_file.write_text(json.dumps(gateway), encoding="utf-8")
    value = dict(
        register_shared_desktop(
            desktop_pid=os.getpid(),
            websocket_url=URL,
            gateway_pid_file=pid_file,
            state_file=state_file,
            install_location=INSTALL_ROOT,
            not_before_filetime=NOT_BEFORE,
        )
    )
    value["desktop_creation_time"] = int(value["desktop_creation_time"]) + 1
    state_file.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(CodexGatewayError, match="Desktop 已退出"):
        active_shared_websocket_url(
            websocket_url=URL,
            gateway_pid_file=pid_file,
            state_file=state_file,
        )
    assert clear_shared_desktop_state(state_file) is True
    assert not state_file.exists()


def test_corrupt_marker_never_falls_back(tmp_path: Path) -> None:
    state_file = tmp_path / "shared.json"
    state_file.write_text("not-json", encoding="utf-8")
    with pytest.raises(CodexGatewayError, match="损坏"):
        active_shared_websocket_url(
            websocket_url=URL,
            gateway_pid_file=tmp_path / "missing.pid",
            state_file=state_file,
        )


def test_wrong_desktop_install_location_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = _gateway_pid_file(tmp_path)
    state_file = tmp_path / "shared.json"
    monkeypatch.setattr("progress_wx.codex_gateway.gateway_healthy", lambda *_a, **_k: True)
    monkeypatch.setattr(
        "progress_wx.codex_gateway.process_image_path", lambda _pid: DESKTOP_IMAGE
    )
    monkeypatch.setattr(
        "progress_wx.codex_gateway.process_session_id", lambda _pid: 7
    )
    monkeypatch.setattr(
        "progress_wx.codex_gateway.process_has_loopback_tcp_connection",
        lambda _pid, _port: True,
    )
    gateway = json.loads(pid_file.read_text(encoding="utf-8"))
    gateway["image_path"] = os.fspath(DESKTOP_IMAGE)
    pid_file.write_text(json.dumps(gateway), encoding="utf-8")

    with pytest.raises(CodexGatewayError, match="受保护"):
        register_shared_desktop(
            desktop_pid=os.getpid(),
            websocket_url=URL,
            gateway_pid_file=pid_file,
            state_file=state_file,
            install_location=Path(r"D:\Fake\OpenAI.Codex_test"),
            not_before_filetime=NOT_BEFORE,
        )


def test_wrong_appx_package_identity_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = _gateway_pid_file(tmp_path)
    state_file = tmp_path / "shared.json"
    monkeypatch.setattr("progress_wx.codex_gateway.gateway_healthy", lambda *_a, **_k: True)
    monkeypatch.setattr(
        "progress_wx.codex_gateway.process_image_path", lambda _pid: DESKTOP_IMAGE
    )
    monkeypatch.setattr("progress_wx.codex_gateway.process_session_id", lambda _pid: 7)
    monkeypatch.setattr(
        "progress_wx.codex_gateway.process_package_full_name",
        lambda _pid: "OpenAI.Codex_other",
    )
    gateway = json.loads(pid_file.read_text(encoding="utf-8"))
    gateway["image_path"] = os.fspath(DESKTOP_IMAGE)
    pid_file.write_text(json.dumps(gateway), encoding="utf-8")

    with pytest.raises(CodexGatewayError, match="实际 AppX 包身份"):
        register_shared_desktop(
            desktop_pid=os.getpid(),
            websocket_url=URL,
            gateway_pid_file=pid_file,
            state_file=state_file,
            install_location=INSTALL_ROOT,
            not_before_filetime=NOT_BEFORE,
        )


def test_live_gateway_with_changed_image_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = _gateway_pid_file(tmp_path)
    monkeypatch.setattr(
        "progress_wx.codex_gateway.process_image_path",
        lambda _pid: Path(r"D:\Other\python.exe"),
    )
    with pytest.raises(CodexGatewayError, match="进程身份不匹配"):
        verified_gateway_running(pid_file)


def test_unknown_desktop_liveness_never_clears_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = _gateway_pid_file(tmp_path)
    state_file = tmp_path / "shared.json"
    monkeypatch.setattr("progress_wx.codex_gateway.gateway_healthy", lambda *_a, **_k: True)
    monkeypatch.setattr(
        "progress_wx.codex_gateway.process_image_path", lambda _pid: DESKTOP_IMAGE
    )
    monkeypatch.setattr(
        "progress_wx.codex_gateway.process_session_id", lambda _pid: 7
    )
    monkeypatch.setattr(
        "progress_wx.codex_gateway.process_has_loopback_tcp_connection",
        lambda _pid, _port: True,
    )
    gateway = json.loads(pid_file.read_text(encoding="utf-8"))
    gateway["image_path"] = os.fspath(DESKTOP_IMAGE)
    pid_file.write_text(json.dumps(gateway), encoding="utf-8")
    register_shared_desktop(
        desktop_pid=os.getpid(),
        websocket_url=URL,
        gateway_pid_file=pid_file,
        state_file=state_file,
        install_location=INSTALL_ROOT,
        not_before_filetime=NOT_BEFORE,
    )
    monkeypatch.setattr(
        "progress_wx.codex_gateway.process_creation_time", lambda _pid: None
    )
    monkeypatch.setattr(
        "progress_wx.codex_gateway.process_liveness", lambda _pid: "unknown"
    )

    with pytest.raises(CodexGatewayError, match="无法确认"):
        clear_shared_desktop_state(state_file)
    assert state_file.is_file()


def test_register_requires_direct_tcp_owner_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = _gateway_pid_file(tmp_path)
    state_file = tmp_path / "shared.json"
    monkeypatch.setattr("progress_wx.codex_gateway.gateway_healthy", lambda *_a, **_k: True)
    monkeypatch.setattr(
        "progress_wx.codex_gateway.process_image_path", lambda _pid: DESKTOP_IMAGE
    )
    monkeypatch.setattr(
        "progress_wx.codex_gateway.process_session_id", lambda _pid: 7
    )
    monkeypatch.setattr(
        "progress_wx.codex_gateway.process_has_loopback_tcp_connection",
        lambda _pid, _port: False,
    )
    gateway = json.loads(pid_file.read_text(encoding="utf-8"))
    gateway["image_path"] = os.fspath(DESKTOP_IMAGE)
    pid_file.write_text(json.dumps(gateway), encoding="utf-8")

    with pytest.raises(CodexGatewayError, match="未连接当前"):
        register_shared_desktop(
            desktop_pid=os.getpid(),
            websocket_url=URL,
            gateway_pid_file=pid_file,
            state_file=state_file,
            install_location=INSTALL_ROOT,
            not_before_filetime=NOT_BEFORE,
        )
    assert not state_file.exists()


def test_active_shared_url_rechecks_desktop_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = _gateway_pid_file(tmp_path)
    state_file = tmp_path / "shared.json"
    connected = {"value": True}
    monkeypatch.setattr("progress_wx.codex_gateway.gateway_healthy", lambda *_a, **_k: True)
    monkeypatch.setattr(
        "progress_wx.codex_gateway.process_image_path", lambda _pid: DESKTOP_IMAGE
    )
    monkeypatch.setattr(
        "progress_wx.codex_gateway.process_session_id", lambda _pid: 7
    )
    monkeypatch.setattr(
        "progress_wx.codex_gateway.process_has_loopback_tcp_connection",
        lambda _pid, _port: connected["value"],
    )
    gateway = json.loads(pid_file.read_text(encoding="utf-8"))
    gateway["image_path"] = os.fspath(DESKTOP_IMAGE)
    pid_file.write_text(json.dumps(gateway), encoding="utf-8")
    register_shared_desktop(
        desktop_pid=os.getpid(),
        websocket_url=URL,
        gateway_pid_file=pid_file,
        state_file=state_file,
        install_location=INSTALL_ROOT,
        not_before_filetime=NOT_BEFORE,
    )
    connected["value"] = False

    with pytest.raises(CodexGatewayError, match="已断开"):
        active_shared_websocket_url(
            websocket_url=URL,
            gateway_pid_file=pid_file,
            state_file=state_file,
        )


def test_register_rejects_gateway_already_stopping(tmp_path: Path) -> None:
    pid_file = _gateway_pid_file(tmp_path)
    assert request_stop(pid_file)
    with pytest.raises(CodexGatewayError, match="正在停止"):
        register_shared_desktop(
            desktop_pid=os.getpid(),
            websocket_url=URL,
            gateway_pid_file=pid_file,
            state_file=tmp_path / "shared.json",
            install_location=INSTALL_ROOT,
            not_before_filetime=NOT_BEFORE,
        )


def test_register_rejects_desktop_older_than_activation_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = _gateway_pid_file(tmp_path)
    monkeypatch.setattr("progress_wx.codex_gateway.gateway_healthy", lambda *_a, **_k: True)
    monkeypatch.setattr(
        "progress_wx.codex_gateway.process_image_path", lambda _pid: DESKTOP_IMAGE
    )
    gateway = json.loads(pid_file.read_text(encoding="utf-8"))
    gateway["image_path"] = os.fspath(DESKTOP_IMAGE)
    pid_file.write_text(json.dumps(gateway), encoding="utf-8")
    creation = int(process_creation_time(os.getpid()) or 0)

    with pytest.raises(CodexGatewayError, match="早于本次"):
        register_shared_desktop(
            desktop_pid=os.getpid(),
            websocket_url=URL,
            gateway_pid_file=pid_file,
            state_file=tmp_path / "shared.json",
            install_location=INSTALL_ROOT,
            not_before_filetime=creation + 1,
        )


def test_gateway_stop_rejects_unregistered_loopback_client(tmp_path: Path) -> None:
    if os.name != "nt":
        return
    pid_file = _gateway_pid_file(tmp_path)
    state_file = tmp_path / "shared.json"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = int(server.getsockname()[1])
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.connect(("127.0.0.1", port))
            accepted, _address = server.accept()
            with accepted:
                with pytest.raises(CodexGatewayError, match="仍有回环客户端"):
                    request_gateway_stop(
                        pid_file,
                        state_file,
                        f"ws://127.0.0.1:{port}",
                    )
    assert not stop_request_path(pid_file).exists()


def test_gateway_stop_rejects_wrong_launch_ownership(tmp_path: Path) -> None:
    pid_file = _gateway_pid_file(tmp_path)
    gateway = json.loads(pid_file.read_text(encoding="utf-8"))
    gateway["launch_token_sha256"] = hashlib.sha256(b"owned-token-" + b"a" * 32).hexdigest()
    pid_file.write_text(json.dumps(gateway), encoding="utf-8")

    with pytest.raises(CodexGatewayError, match="启动归属不匹配"):
        request_gateway_stop(
            pid_file,
            tmp_path / "shared.json",
            URL,
            expected_launch_token="wrong-token-" + "b" * 32,
        )
    assert not stop_request_path(pid_file).exists()
