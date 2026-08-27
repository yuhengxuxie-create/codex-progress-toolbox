"""共享 Codex app-server 的本机回环生命周期与 Desktop 绑定标记。"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from contextlib import contextmanager
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

from .codex_rpc import _launch_argv, command_argv, validate_loopback_websocket_url
from .locking import InterprocessMutex, LockUnavailable
from .process_control import (
    InstanceError,
    acquire_instance,
    clear_stop_request,
    instance_running,
    process_creation_time,
    process_has_loopback_tcp_connection,
    process_image_path,
    process_package_full_name,
    process_liveness,
    process_session_id,
    loopback_tcp_client_pids,
    read_pid_file,
    release_instance,
    request_stop,
    stop_requested_for,
)


LOGGER = logging.getLogger(__name__)


class CodexGatewayError(RuntimeError):
    """共享网关状态损坏、启动失败或 Desktop 绑定不可信。"""


def _health_url(websocket_url: str) -> str:
    url = validate_loopback_websocket_url(websocket_url)
    parts = urlsplit(url)
    return f"http://127.0.0.1:{parts.port}/readyz"


def gateway_healthy(websocket_url: str, *, timeout_seconds: float = 0.5) -> bool:
    """不经系统代理访问 app-server 官方 ``/readyz``。"""

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds 必须大于 0")
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(_health_url(websocket_url), timeout=timeout_seconds) as response:
            return 200 <= int(response.status) < 300
    except (OSError, HTTPError, URLError, TimeoutError):
        return False


def _gateway_argv(command: str, websocket_url: str) -> list[str]:
    base = command_argv(command)
    # npm 的 codex.ps1/cmd 会再生成 Node 和原生子进程，终止 shim 容易遗留监听器。
    # 显式配置 shim 时，优先在该 shim 同目录的 npm 包树内解析原生二进制；
    # 未配置路径时才按 PATH 的固定顺序查找，绝不扫描其它磁盘目录。
    if os.name == "nt":
        native = _npm_native_codex(None if base[0].casefold() == "codex" else base[0])
        if native:
            base = [native, *base[1:]]
        elif base[0].casefold() == "codex":
            resolved = shutil.which("codex")
            if resolved:
                resolved_parts = tuple(part.casefold() for part in Path(resolved).parts)
                is_desktop_package = "windowsapps" in resolved_parts and any(
                    part.startswith("openai.codex_") for part in resolved_parts
                )
                if is_desktop_package:
                    raise CodexGatewayError(
                        "codex.command 命中了受保护的 Codex Desktop；"
                        "请配置可执行的 Codex CLI shim 绝对路径"
                    )
    return [
        *_launch_argv(base),
        "--listen",
        validate_loopback_websocket_url(websocket_url),
    ]


def _npm_native_codex(executable: str | None = None) -> str | None:
    """从指定 npm shim 的封闭包树解析唯一原生二进制。

    ``executable`` 通常来自配置中的第一项。绝对路径按原样使用，避免
    ``shutil.which`` 把受保护的 WindowsApps Desktop 可执行文件误当成 CLI。
    只有未指定路径时才查找 ``codex.cmd``/``codex``，且后续只检查 npm
    包的固定目录层级，不做宽泛磁盘扫描。
    """

    shim: str | None = executable
    if shim is None:
        shim = shutil.which("codex.cmd") or shutil.which("codex")
    elif not os.path.isabs(shim):
        shim = shutil.which(shim) or shim
    if not shim:
        return None
    shim_path = Path(shim)
    if not shim_path.is_file() or shim_path.suffix.casefold() not in {
        ".cmd",
        ".bat",
        ".ps1",
    }:
        # WindowsApps 中的 codex.exe 是 Desktop 包入口，不是可安全复用的
        # npm CLI；没有 shim 就保持 fail-closed，由调用方报告配置问题。
        return None
    root = shim_path.resolve().parent
    # npm 的两种合法布局：包作为 @openai/codex 的嵌套依赖，或作为根依赖。
    package_roots = (
        root / "node_modules" / "@openai" / "codex" / "node_modules" / "@openai",
        root / "node_modules" / "@openai",
    )
    matches: set[Path] = set()
    for package_root in package_roots:
        for package in package_root.glob("codex-win32-*"):
            # 只接受 npm 包声明的 vendor/<target>/bin/codex.exe 位置。
            candidate = package / "vendor"
            for target in candidate.glob("*/bin/codex.exe"):
                if target.is_file():
                    matches.add(target.resolve())
    if len(matches) != 1:
        return None
    return os.fspath(next(iter(matches)))


class _JobBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("per_process_user_time_limit", ctypes.c_longlong),
        ("per_job_user_time_limit", ctypes.c_longlong),
        ("limit_flags", wintypes.DWORD),
        ("minimum_working_set_size", ctypes.c_size_t),
        ("maximum_working_set_size", ctypes.c_size_t),
        ("active_process_limit", wintypes.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority_class", wintypes.DWORD),
        ("scheduling_class", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("read_operation_count", ctypes.c_ulonglong),
        ("write_operation_count", ctypes.c_ulonglong),
        ("other_operation_count", ctypes.c_ulonglong),
        ("read_transfer_count", ctypes.c_ulonglong),
        ("write_transfer_count", ctypes.c_ulonglong),
        ("other_transfer_count", ctypes.c_ulonglong),
    ]


class _JobExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("basic_limit_information", _JobBasicLimitInformation),
        ("io_info", _IoCounters),
        ("process_memory_limit", ctypes.c_size_t),
        ("job_memory_limit", ctypes.c_size_t),
        ("peak_process_memory_used", ctypes.c_size_t),
        ("peak_job_memory_used", ctypes.c_size_t),
    ]


class _WindowsProcessJob:
    """关闭句柄时精确回收本监督器创建的全部子孙进程。"""

    def __init__(self) -> None:
        self.handle: int | None = None
        if os.name != "nt":
            return
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise CodexGatewayError("无法创建 Windows gateway Job Object")
        info = _JobExtendedLimitInformation()
        info.basic_limit_information.limit_flags = 0x00002000
        ok = kernel32.SetInformationJobObject(
            handle, 9, ctypes.byref(info), ctypes.sizeof(info)
        )
        if not ok:
            kernel32.CloseHandle(handle)
            raise CodexGatewayError("无法设置 gateway Job Object")
        self.handle = int(handle)

    def assign(self, process: subprocess.Popen[str]) -> None:
        if self.handle is None:
            return
        ok = ctypes.windll.kernel32.AssignProcessToJobObject(
            wintypes.HANDLE(self.handle), wintypes.HANDLE(process._handle)  # type: ignore[attr-defined]
        )
        if not ok:
            raise CodexGatewayError("无法把 gateway 进程绑定到专属 Job Object")

    def close(self) -> None:
        handle, self.handle = self.handle, None
        if handle is not None:
            ctypes.windll.kernel32.CloseHandle(wintypes.HANDLE(handle))


def _log_stderr(stream: Any) -> None:
    """把子进程诊断写入七天轮转日志，不让管道阻塞。"""

    if stream is None:
        return
    try:
        for line in stream:
            text = " ".join(str(line).split())
            if text:
                LOGGER.warning("Codex gateway: %s", text[:1000])
    except (OSError, ValueError):
        return


def gateway_launch_authorization_path(pid_file: Path) -> Path:
    """返回网关启动授权文件；它只保存 nonce 哈希，不保存原始令牌。"""

    return pid_file.with_name(pid_file.name + ".launch")


def _launch_token_sha256(launch_token: str) -> str:
    if not isinstance(launch_token, str) or len(launch_token) < 32:
        raise CodexGatewayError("gateway 启动归属令牌无效")
    return hashlib.sha256(launch_token.encode("utf-8")).hexdigest()


def _acquire_gateway_launch_mutex(
    pid_file: Path, *, timeout_seconds: float = 30.0
) -> InterprocessMutex:
    """串行化“消费启动授权发布 PID”和“撤销尚未消费的授权”。"""

    identity = f"gateway-launch:{os.path.normcase(os.fspath(pid_file.resolve()))}"
    deadline = time.monotonic() + timeout_seconds
    while True:
        mutex = InterprocessMutex(identity, global_scope=True)
        try:
            mutex.acquire()
            return mutex
        except LockUnavailable as exc:
            mutex.close()
            if time.monotonic() >= deadline:
                raise CodexGatewayError("gateway 启动授权正被另一进程处理") from exc
            time.sleep(0.01)


def _read_gateway_launch_authorization(pid_file: Path) -> Mapping[str, Any] | None:
    path = gateway_launch_authorization_path(pid_file)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise CodexGatewayError("gateway 启动授权状态损坏") from exc
    version = value.get("version") if isinstance(value, Mapping) else None
    token_hash = value.get("launch_token_sha256") if isinstance(value, Mapping) else None
    project_root = value.get("project_root") if isinstance(value, Mapping) else None
    created_at = value.get("created_at") if isinstance(value, Mapping) else None
    if (
        type(version) is not int
        or version != 1
        or not isinstance(token_hash, str)
        or len(token_hash) != 64
        or any(character not in "0123456789abcdef" for character in token_hash)
        or not isinstance(project_root, str)
        or not project_root.strip()
        or type(created_at) is not int
        or created_at <= 0
    ):
        raise CodexGatewayError("gateway 启动授权格式无效")
    return value


def _authorization_matches(
    authorization: Mapping[str, Any] | None,
    *,
    launch_token_sha256: str,
) -> bool:
    if authorization is None:
        return False
    project_root = Path(__file__).resolve().parents[2]
    return (
        authorization.get("launch_token_sha256") == launch_token_sha256
        and _normalized_path(authorization.get("project_root"))
        == _normalized_path(project_root)
    )


def authorize_gateway_launch(pid_file: Path, launch_token: str) -> Mapping[str, Any]:
    """在派生监督器前发布一次性授权；已有授权时失败关闭。"""

    token_hash = _launch_token_sha256(launch_token)
    path = gateway_launch_authorization_path(pid_file)
    mutex = _acquire_gateway_launch_mutex(pid_file)
    try:
        if _read_gateway_launch_authorization(pid_file) is not None:
            raise CodexGatewayError("已有未完成的 gateway 启动授权")
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "launch_token_sha256": token_hash,
            "project_root": os.fspath(Path(__file__).resolve().parents[2]),
            "created_at": int(time.time()),
        }
        # 正式授权文件只能由同目录完整临时文件原子替换而来；断电不会留下半截 JSON。
        _atomic_json(path, payload)
        return payload
    finally:
        mutex.close()


def _acquire_authorized_gateway_instance(
    *,
    pid_file: Path,
    config_path: Path,
    launch_token: str,
) -> dict[str, Any]:
    """只有仍持有本代授权的监督器才能发布 PID 状态。"""

    token_hash = _launch_token_sha256(launch_token)
    authorization_path = gateway_launch_authorization_path(pid_file)
    mutex = _acquire_gateway_launch_mutex(pid_file)
    state: dict[str, Any] | None = None
    try:
        authorization = _read_gateway_launch_authorization(pid_file)
        if not _authorization_matches(
            authorization, launch_token_sha256=token_hash
        ):
            raise CodexGatewayError("gateway 启动授权已撤销或不属于本代")
        state = acquire_instance(
            pid_file,
            config_path,
            metadata={"launch_token_sha256": token_hash},
        )
        try:
            authorization_path.unlink()
        except OSError as exc:
            release_instance(pid_file, state)
            state = None
            raise CodexGatewayError("无法消费 gateway 启动授权，已撤销 PID 发布") from exc
        return state
    finally:
        mutex.close()


def _same_gateway_generation(
    state: Mapping[str, Any] | None, expected: Mapping[str, Any]
) -> bool:
    if state is None:
        return False
    pid = state.get("pid")
    creation_time = state.get("creation_time")
    return (
        type(pid) is int
        and pid > 0
        and type(creation_time) is int
        and creation_time > 0
        and pid == expected.get("pid")
        and creation_time == expected.get("creation_time")
    )


def recover_owned_gateway_launch(
    *,
    pid_file: Path,
    state_file: Path,
    websocket_url: str,
    launch_token: str,
    stop_timeout_seconds: float = 20.0,
) -> Mapping[str, Any]:
    """原子撤销待启动授权，或协作停止已经发布的精确归属世代。"""

    if stop_timeout_seconds <= 0:
        raise ValueError("stop_timeout_seconds 必须大于 0")
    token_hash = _launch_token_sha256(launch_token)
    authorization_path = gateway_launch_authorization_path(pid_file)
    expected_state: Mapping[str, Any] | None = None
    mutex = _acquire_gateway_launch_mutex(pid_file)
    try:
        authorization = _read_gateway_launch_authorization(pid_file)
        authorization_owned = _authorization_matches(
            authorization, launch_token_sha256=token_hash
        )
        if authorization is not None and not authorization_owned:
            # 在读取/停止 PID 世代前统一拒绝异世代授权，绝不清理或影响其它启动者。
            raise CodexGatewayError(
                "发现另一世代的 gateway 启动授权；拒绝删除、停止或宣称本代已恢复"
            )
        gateway = read_pid_file(pid_file)
        if gateway is None:
            if authorization_owned:
                authorization_path.unlink()
                return {"resolved": True, "outcome": "pending-authorization-cancelled"}
            # 没有本代授权时，迟到的 gateway-run 无法再发布 PID。
            return {"resolved": True, "outcome": "no-owned-launch"}

        project_root = gateway.get("project_root")
        if (
            gateway.get("launch_token_sha256") != token_hash
            or not isinstance(project_root, str)
            or _normalized_path(project_root)
            != _normalized_path(Path(__file__).resolve().parents[2])
        ):
            if authorization_owned:
                authorization_path.unlink()
            return {"resolved": True, "outcome": "replacement-generation-preserved"}

        pid = gateway.get("pid")
        creation_time = gateway.get("creation_time")
        if (
            type(pid) is not int
            or pid <= 0
            or type(creation_time) is not int
            or creation_time <= 0
        ):
            raise CodexGatewayError("本代 gateway PID 世代字段无效")
        liveness = process_liveness(pid)
        if liveness == "unknown":
            raise CodexGatewayError("无法确认本代 gateway 进程是否仍存活")
        actual_creation_time = process_creation_time(pid) if liveness == "running" else None
        if liveness == "absent" or actual_creation_time != creation_time:
            if authorization_owned:
                authorization_path.unlink()
            return {"resolved": True, "outcome": "owned-generation-already-exited"}

        expected_state = verified_gateway_state(
            pid_file,
            expected_pid=pid,
            expected_creation_time=creation_time,
            expected_launch_token=launch_token,
        )
        if authorization_owned:
            # PID 已发布时也撤销可能残留的同代授权，阻止重复迟到进程。
            authorization_path.unlink()
        requested = request_gateway_stop(
            pid_file,
            state_file,
            websocket_url,
            expected_pid=pid,
            expected_creation_time=creation_time,
            expected_launch_token=launch_token,
        )
        if not requested:
            current = read_pid_file(pid_file)
            if not _same_gateway_generation(current, expected_state):
                return {"resolved": True, "outcome": "owned-generation-exited"}
            raise CodexGatewayError("本代 gateway 未确认停止，也未接受停止请求")
    finally:
        mutex.close()

    deadline = time.monotonic() + stop_timeout_seconds
    while time.monotonic() < deadline:
        current = read_pid_file(pid_file)
        if not _same_gateway_generation(current, expected_state):
            return {"resolved": True, "outcome": "owned-generation-stopped"}
        assert current is not None
        pid = current["pid"]
        liveness = process_liveness(pid)
        if liveness == "absent":
            return {"resolved": True, "outcome": "owned-generation-stopped"}
        if liveness == "unknown":
            raise CodexGatewayError("等待停止时无法确认本代 gateway 存活状态")
        if process_creation_time(pid) != current.get("creation_time"):
            return {"resolved": True, "outcome": "owned-generation-stopped"}
        time.sleep(0.1)
    raise CodexGatewayError("本代 gateway 未在时限内退出；已保留恢复状态")


def run_gateway(
    *,
    command: str,
    websocket_url: str,
    pid_file: Path,
    config_path: Path,
    launch_token: str,
    ready_timeout_seconds: float = 15.0,
) -> int:
    """前台监督一个 WebSocket app-server；只终止自己创建的精确子进程。"""

    url = validate_loopback_websocket_url(websocket_url)
    if not isinstance(launch_token, str) or len(launch_token) < 32:
        raise CodexGatewayError("gateway 启动归属令牌无效")
    state = _acquire_authorized_gateway_instance(
        pid_file=pid_file,
        config_path=config_path,
        launch_token=launch_token,
    )
    child: subprocess.Popen[str] | None = None
    stderr_reader: threading.Thread | None = None
    process_job: _WindowsProcessJob | None = None
    try:
        process_job = _WindowsProcessJob()
        startupinfo = None
        creationflags = 0
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        child = subprocess.Popen(
            _gateway_argv(command, url),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            close_fds=True,
            startupinfo=startupinfo,
            creationflags=creationflags,
        )
        process_job.assign(child)
        stderr_reader = threading.Thread(
            target=_log_stderr,
            args=(child.stderr,),
            name="progress-wx-codex-gateway-stderr",
            daemon=True,
        )
        stderr_reader.start()
        deadline = time.monotonic() + ready_timeout_seconds
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise CodexGatewayError(
                    f"Codex gateway 启动前退出，code={child.returncode}"
                )
            if gateway_healthy(url):
                LOGGER.info("Codex gateway 已就绪：127.0.0.1:%d", urlsplit(url).port)
                break
            time.sleep(0.1)
        else:
            raise CodexGatewayError("等待 Codex gateway /readyz 超时")

        while True:
            code = child.poll()
            if code is not None:
                raise CodexGatewayError(f"Codex gateway 意外退出，code={code}")
            if stop_requested_for(pid_file, state):
                # 停止标记存在期间，受支持的 Desktop 登记入口会拒绝新连接。
                # 再要求持续 1 秒没有任何回环客户端，缩小外部客户端晚到窗口。
                quiescent = True
                port = urlsplit(url).port
                if port is None:  # pragma: no cover - URL 校验器已保证端口存在
                    raise CodexGatewayError("共享 Codex URL 缺少端口")
                for index in range(5):
                    try:
                        if loopback_tcp_client_pids(port):
                            quiescent = False
                            break
                    except OSError:
                        quiescent = False
                        break
                    if index < 4:
                        time.sleep(0.25)
                if quiescent:
                    return 0
                clear_stop_request(pid_file, state)
                LOGGER.warning("共享 gateway 停止时检测到回环客户端；已取消本次停止")
            time.sleep(0.5)
    finally:
        if child is not None:
            try:
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=5)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                # 只对本函数刚创建且仍持有句柄的子进程使用最终回收。
                try:
                    if child.poll() is None:
                        child.kill()
                except (OSError, ValueError):
                    pass
                try:
                    child.wait(timeout=2)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    pass
        # Windows Job Object 在这里同步回收 shim 可能创建的全部子孙。
        if process_job is not None:
            process_job.close()
        if stderr_reader is not None:
            stderr_reader.join(timeout=1)
        try:
            clear_stop_request(pid_file, state)
        finally:
            release_instance(pid_file, state)


def _read_json(path: Path) -> Mapping[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise CodexGatewayError(f"共享 Codex 状态文件损坏：{path}") from exc
    if not isinstance(value, Mapping):
        raise CodexGatewayError(f"共享 Codex 状态文件格式错误：{path}")
    return value


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    """把完整 JSON 刷盘后原子发布，异常时只清理本次唯一临时文件。"""

    payload = (
        json.dumps(dict(value), ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _normalized_path(value: str | os.PathLike[str]) -> str:
    """不访问文件系统地规范绝对路径，供进程身份精确比较。"""

    return os.path.normcase(os.path.abspath(os.fspath(value)))


def _program_files_directory() -> Path:
    """从 Windows Known Folder API 取系统 Program Files，不信任调用者环境变量。"""

    if os.name != "nt":
        return Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    buffer = ctypes.create_unicode_buffer(32768)
    shell32 = ctypes.windll.shell32
    shell32.SHGetFolderPathW.argtypes = (
        wintypes.HWND,
        ctypes.c_int,
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
    )
    shell32.SHGetFolderPathW.restype = ctypes.c_long
    # CSIDL_PROGRAM_FILES；由系统 API 返回，避免环境变量被调用者临时替换。
    result = shell32.SHGetFolderPathW(None, 0x0026, None, 0, buffer)
    if result != 0 or not buffer.value:
        raise CodexGatewayError("无法从 Windows 系统 API 获取 Program Files")
    return Path(buffer.value)


@contextmanager
def _shared_desktop_lock(state_file: Path):
    """串行登记与清除，避免检查旧标记后误删并发写入的新标记。"""

    identity = f"shared-desktop:{os.path.normcase(os.fspath(state_file.resolve()))}"
    mutex = InterprocessMutex(identity)
    try:
        mutex.acquire()
    except LockUnavailable as exc:
        raise CodexGatewayError("共享 Codex Desktop 状态正被另一进程修改") from exc
    try:
        yield
    finally:
        mutex.close()


def verified_gateway_running(pid_file: Path) -> bool:
    """验证监督器 PID 世代、映像和项目根；存活但身份不明时失败关闭。"""

    gateway = read_pid_file(pid_file)
    if gateway is None:
        return False
    if not instance_running(pid_file):
        return False
    pid = gateway.get("pid")
    expected_image = gateway.get("image_path")
    expected_project = gateway.get("project_root")
    actual_image = process_image_path(pid) if isinstance(pid, int) else None
    project_root = Path(__file__).resolve().parents[2]
    if (
        not isinstance(expected_image, str)
        or not expected_image.strip()
        or actual_image is None
        or _normalized_path(expected_image) != _normalized_path(actual_image)
        or not isinstance(expected_project, str)
        or _normalized_path(expected_project) != _normalized_path(project_root)
    ):
        raise CodexGatewayError("共享 Codex gateway 进程身份不匹配")
    return True


def _verified_gateway_state(pid_file: Path) -> Mapping[str, Any]:
    if not verified_gateway_running(pid_file):
        raise CodexGatewayError("共享 Codex gateway 未运行")
    gateway = read_pid_file(pid_file)
    if gateway is None:  # pragma: no cover - 已由上面的原子状态检查保证
        raise CodexGatewayError("共享 Codex gateway 状态消失")
    return gateway


def _assert_expected_gateway(
    gateway: Mapping[str, Any],
    *,
    expected_pid: int | None,
    expected_creation_time: int | None,
    expected_launch_token: str | None,
) -> None:
    """把调用方观察到的 gateway 身份持续绑定到同一进程世代。"""

    if (expected_pid is None) != (expected_creation_time is None):
        raise CodexGatewayError("预期 gateway PID 与创建时间必须同时提供")
    if expected_pid is not None and (
        gateway.get("pid") != expected_pid
        or gateway.get("creation_time") != expected_creation_time
    ):
        raise CodexGatewayError("共享 gateway 已换代；拒绝使用替代实例")
    if expected_launch_token is not None:
        if len(expected_launch_token) < 32:
            raise CodexGatewayError("预期 gateway 启动归属令牌无效")
        expected_sha256 = hashlib.sha256(
            expected_launch_token.encode("utf-8")
        ).hexdigest()
        if gateway.get("launch_token_sha256") != expected_sha256:
            raise CodexGatewayError("共享 gateway 启动归属不匹配；拒绝使用替代实例")


def verified_gateway_state(
    pid_file: Path,
    *,
    expected_pid: int | None = None,
    expected_creation_time: int | None = None,
    expected_launch_token: str | None = None,
) -> Mapping[str, Any]:
    """返回已验证且符合调用方预期的 gateway 状态。"""

    gateway = _verified_gateway_state(pid_file)
    _assert_expected_gateway(
        gateway,
        expected_pid=expected_pid,
        expected_creation_time=expected_creation_time,
        expected_launch_token=expected_launch_token,
    )
    return gateway


def register_shared_desktop(
    *,
    desktop_pid: int,
    websocket_url: str,
    gateway_pid_file: Path,
    state_file: Path,
    install_location: Path,
    not_before_filetime: int,
    expected_gateway_pid: int | None = None,
    expected_gateway_creation_time: int | None = None,
    expected_gateway_launch_token: str | None = None,
) -> Mapping[str, Any]:
    """在启动脚本验证 TCP 连接后，绑定 Desktop 与网关的进程世代。"""

    if desktop_pid <= 0:
        raise CodexGatewayError("Desktop PID 必须大于 0")
    if not isinstance(not_before_filetime, int) or not_before_filetime < 0:
        raise CodexGatewayError("Desktop 激活时间边界无效")
    if os.name == "nt" and not_before_filetime <= 0:
        raise CodexGatewayError("Desktop 激活时间边界必须是 Windows FILETIME")
    url = validate_loopback_websocket_url(websocket_url)
    port = urlsplit(url).port
    if port is None:  # pragma: no cover - URL 校验器已保证端口存在
        raise CodexGatewayError("共享 Codex URL 缺少端口")
    with _shared_desktop_lock(state_file):
        gateway = verified_gateway_state(
            gateway_pid_file,
            expected_pid=expected_gateway_pid,
            expected_creation_time=expected_gateway_creation_time,
            expected_launch_token=expected_gateway_launch_token,
        )
        if stop_requested_for(gateway_pid_file, gateway):
            raise CodexGatewayError("共享 Codex gateway 正在停止，拒绝登记 Desktop")
        if not gateway_healthy(url):
            raise CodexGatewayError("共享 Codex gateway /readyz 未就绪")
        desktop_creation = process_creation_time(desktop_pid)
        if desktop_creation is None:
            raise CodexGatewayError("无法确认 Codex Desktop 进程仍在运行")
        if desktop_creation < not_before_filetime:
            raise CodexGatewayError("Codex Desktop 早于本次 AppsFolder 激活边界")
        current_session = process_session_id(os.getpid())
        desktop_session = process_session_id(desktop_pid)
        if current_session is None or desktop_session != current_session:
            raise CodexGatewayError("Codex Desktop 不在当前 Windows 会话")
        image = process_image_path(desktop_pid)
        install_root = Path(install_location)
        if not install_root.is_absolute():
            raise CodexGatewayError("Codex Desktop 安装目录必须是绝对路径")
        program_files = _program_files_directory()
        windows_apps = program_files / "WindowsApps"
        try:
            protected_common = os.path.commonpath(
                (_normalized_path(install_root), _normalized_path(windows_apps))
            )
        except ValueError as exc:
            raise CodexGatewayError("Codex Desktop 安装目录不在受保护包目录") from exc
        if (
            protected_common != _normalized_path(windows_apps)
            or not install_root.name.casefold().startswith("openai.codex_")
        ):
            raise CodexGatewayError("Codex Desktop 安装目录不在受保护的 OpenAI.Codex 包目录")
        package_full_name = process_package_full_name(desktop_pid)
        if (
            not isinstance(package_full_name, str)
            or install_root.name.casefold() != package_full_name.casefold()
        ):
            raise CodexGatewayError("Codex Desktop 实际 AppX 包身份与安装目录不一致")
        expected_image = install_root / "app" / "ChatGPT.exe"
        if (
            image is None
            or image.name.casefold() != "chatgpt.exe"
            or _normalized_path(image) != _normalized_path(expected_image)
        ):
            raise CodexGatewayError("指定 PID 不是当前 Codex 包安装目录中的 ChatGPT.exe")
        try:
            connected = process_has_loopback_tcp_connection(desktop_pid, port)
        except OSError as exc:
            raise CodexGatewayError("无法读取 Desktop 的回环 TCP owner 状态") from exc
        if not connected:
            raise CodexGatewayError("Codex Desktop 未连接当前共享 gateway")
        value: dict[str, Any] = {
            "version": 3,
            "websocket_url": url,
            "gateway_pid": gateway["pid"],
            "gateway_creation_time": gateway.get("creation_time"),
            "desktop_pid": int(desktop_pid),
            "desktop_creation_time": desktop_creation,
            "desktop_image_path": os.fspath(image),
            "desktop_install_location": os.fspath(install_root),
            "desktop_package_full_name": package_full_name,
            "desktop_session_id": desktop_session,
            "activation_not_before_filetime": int(not_before_filetime),
            "registered_at": int(time.time()),
        }
        _atomic_json(state_file, value)
        return value


def _shared_desktop_running_state(state: Mapping[str, Any]) -> bool:
    """校验已读取标记；仅在能证明目标世代已消失时返回 False。"""

    if state.get("version") != 3:
        raise CodexGatewayError("共享 Codex Desktop 标记版本不受信任")
    pid = state.get("desktop_pid")
    expected_creation = state.get("desktop_creation_time")
    expected_image = state.get("desktop_image_path")
    install_location = state.get("desktop_install_location")
    package_full_name = state.get("desktop_package_full_name")
    expected_session = state.get("desktop_session_id")
    activation_boundary = state.get("activation_not_before_filetime")
    if (
        not isinstance(pid, int)
        or not isinstance(expected_creation, int)
        or not isinstance(expected_image, str)
        or not isinstance(install_location, str)
        or not isinstance(package_full_name, str)
        or not isinstance(expected_session, int)
        or not isinstance(activation_boundary, int)
        or expected_creation < activation_boundary
    ):
        raise CodexGatewayError("共享 Codex Desktop 标记缺少进程身份")
    actual_creation = process_creation_time(pid)
    if actual_creation is None:
        liveness = process_liveness(pid)
        if liveness == "absent":
            return False
        raise CodexGatewayError("无法确认共享 Codex Desktop 是否仍在运行")
    if actual_creation != expected_creation:
        return False
    actual_session = process_session_id(pid)
    if actual_session is None:
        raise CodexGatewayError("无法确认共享 Codex Desktop 会话")
    if actual_session != expected_session:
        raise CodexGatewayError("共享 Codex Desktop 会话已变化")
    actual_image = process_image_path(pid)
    actual_package_full_name = process_package_full_name(pid)
    required_image = Path(install_location) / "app" / "ChatGPT.exe"
    if (
        actual_image is None
        or actual_package_full_name is None
        or actual_package_full_name.casefold() != package_full_name.casefold()
        or Path(install_location).name.casefold() != package_full_name.casefold()
        or _normalized_path(actual_image) != _normalized_path(expected_image)
        or _normalized_path(actual_image) != _normalized_path(required_image)
    ):
        raise CodexGatewayError("共享 Codex Desktop 进程映像不匹配")
    state_url = state.get("websocket_url")
    if not isinstance(state_url, str):
        raise CodexGatewayError("共享 Codex Desktop 标记缺少 WebSocket URL")
    port = urlsplit(validate_loopback_websocket_url(state_url)).port
    if port is None:  # pragma: no cover - URL 校验器已保证端口存在
        raise CodexGatewayError("共享 Codex Desktop 标记缺少端口")
    try:
        connected = process_has_loopback_tcp_connection(pid, port)
    except OSError as exc:
        raise CodexGatewayError("无法复核 Desktop 的回环 TCP owner 状态") from exc
    if not connected:
        raise CodexGatewayError("共享 Codex Desktop 已断开回环 gateway")
    return True


def shared_desktop_running(state_file: Path) -> bool:
    state = _read_json(state_file)
    if state is None:
        return False
    return _shared_desktop_running_state(state)


def active_shared_websocket_url(
    *,
    websocket_url: str,
    gateway_pid_file: Path,
    state_file: Path,
) -> str | None:
    """仅在 gateway 与 Desktop 两个进程世代都精确匹配时返回共享端点。"""

    state = _read_json(state_file)
    if state is None:
        return None
    url = validate_loopback_websocket_url(websocket_url)
    if state.get("version") != 3 or state.get("websocket_url") != url:
        raise CodexGatewayError("共享 Codex 标记与当前配置不一致；拒绝回退")
    try:
        gateway = _verified_gateway_state(gateway_pid_file)
    except CodexGatewayError as exc:
        raise CodexGatewayError(
            "共享 Codex gateway 已停止或身份失配；拒绝回退到独立 stdio"
        ) from exc
    if (
        state.get("gateway_pid") != gateway.get("pid")
        or state.get("gateway_creation_time") != gateway.get("creation_time")
    ):
        raise CodexGatewayError("共享 Codex gateway 进程世代不匹配；拒绝回退")
    if not shared_desktop_running(state_file):
        raise CodexGatewayError("共享 Codex Desktop 已退出；请先停止共享网关")
    if not gateway_healthy(url):
        raise CodexGatewayError("共享 Codex gateway 健康检查失败")
    return url


def clear_shared_desktop_state(state_file: Path) -> bool:
    """只在已绑定 Desktop 确认退出后清除标记。"""

    with _shared_desktop_lock(state_file):
        return _clear_shared_desktop_state_locked(state_file)


def _clear_shared_desktop_state_locked(state_file: Path) -> bool:
    state = _read_json(state_file)
    if state is None:
        return False
    if _shared_desktop_running_state(state):
        raise CodexGatewayError("Codex Desktop 仍在共享网关中；请先正常退出 Desktop")
    state_file.unlink()
    return True


def request_gateway_stop(
    pid_file: Path,
    state_file: Path,
    websocket_url: str,
    *,
    expected_pid: int | None = None,
    expected_creation_time: int | None = None,
    expected_launch_token: str | None = None,
) -> bool:
    """Desktop 仍在线时拒绝切断其 app-server。"""

    # 清标记与写停止请求共用一把锁；登记方会拒绝已存在停止请求的 gateway。
    with _shared_desktop_lock(state_file):
        _clear_shared_desktop_state_locked(state_file)
        if not verified_gateway_running(pid_file):
            return False
        gateway = verified_gateway_state(
            pid_file,
            expected_pid=expected_pid,
            expected_creation_time=expected_creation_time,
            expected_launch_token=expected_launch_token,
        )
        port = urlsplit(validate_loopback_websocket_url(websocket_url)).port
        if port is None:  # pragma: no cover - URL 校验器已保证端口存在
            raise CodexGatewayError("共享 Codex URL 缺少端口")
        try:
            clients = loopback_tcp_client_pids(port)
        except OSError as exc:
            raise CodexGatewayError("无法确认共享 gateway 是否仍有回环客户端") from exc
        if clients:
            raise CodexGatewayError("共享 gateway 仍有回环客户端；拒绝停止")
        return request_stop(pid_file, expected_state=gateway)


__all__ = [
    "CodexGatewayError",
    "active_shared_websocket_url",
    "authorize_gateway_launch",
    "clear_shared_desktop_state",
    "gateway_launch_authorization_path",
    "gateway_healthy",
    "recover_owned_gateway_launch",
    "register_shared_desktop",
    "request_gateway_stop",
    "run_gateway",
    "shared_desktop_running",
    "verified_gateway_state",
    "verified_gateway_running",
]
