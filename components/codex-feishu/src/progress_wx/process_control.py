"""无需强杀的单实例与停止请求协议。"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
import socket
import struct
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

from .locking import InterprocessMutex, LockUnavailable


class InstanceError(RuntimeError):
    """已有实例或 PID 状态无法安全判断。"""


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]


def process_creation_time(pid: int) -> int | None:
    """读取 Windows FILETIME；用于避免 PID 重用后误认或误停其它进程。"""

    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return None
        return 0
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
    )
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return None
    try:
        creation, exit_time, kernel, user = _FileTime(), _FileTime(), _FileTime(), _FileTime()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None
        return (creation.high << 32) | creation.low
    finally:
        kernel32.CloseHandle(handle)


def process_image_path(pid: int) -> Path | None:
    """读取精确进程映像路径；共享 Desktop 登记时用于拒绝任意 PID。"""

    if pid <= 0:
        return None
    if os.name != "nt":
        try:
            return Path(f"/proc/{pid}/exe").resolve(strict=True)
        except OSError:
            return None
    kernel32 = ctypes.windll.kernel32
    kernel32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return None
    try:
        capacity = 32768
        buffer = ctypes.create_unicode_buffer(capacity)
        size = wintypes.DWORD(capacity)
        if not kernel32.QueryFullProcessImageNameW(
            handle, 0, buffer, ctypes.byref(size)
        ):
            return None
        return Path(buffer.value)
    finally:
        kernel32.CloseHandle(handle)


def process_package_full_name(pid: int) -> str | None:
    """读取目标进程的真实 AppX PackageFullName；非打包进程返回 ``None``。"""

    if pid <= 0 or os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    try:
        get_package_full_name = kernel32.GetPackageFullName
    except AttributeError:
        return None
    get_package_full_name.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_uint32),
        wintypes.LPWSTR,
    )
    get_package_full_name.restype = ctypes.c_long
    handle = kernel32.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return None
    try:
        length = ctypes.c_uint32(0)
        result = int(get_package_full_name(handle, ctypes.byref(length), None))
        # ERROR_INSUFFICIENT_BUFFER 表示已取得所需字符数；15700 表示无包身份。
        if result == 15700:
            return None
        if result != 122 or length.value <= 1:
            return None
        buffer = ctypes.create_unicode_buffer(length.value)
        result = int(
            get_package_full_name(handle, ctypes.byref(length), buffer)
        )
        if result != 0 or not buffer.value:
            return None
        return buffer.value
    finally:
        kernel32.CloseHandle(handle)


def process_liveness(pid: int) -> str:
    """返回 ``running/absent/unknown``；权限失败不能伪装成进程已退出。"""

    if pid <= 0:
        return "absent"
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return "absent"
        except (PermissionError, OSError):
            return "unknown"
        return "running"
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    ctypes.set_last_error(0)
    handle = kernel32.OpenProcess(0x1000, False, int(pid))
    if handle:
        kernel32.CloseHandle(handle)
        return "running"
    error = ctypes.get_last_error()
    # ERROR_INVALID_PARAMETER 表示该 PID 不存在；ACCESS_DENIED 等必须视为未知。
    return "absent" if error == 87 else "unknown"


def process_session_id(pid: int) -> int | None:
    """读取 Windows 会话 ID；共享 Desktop 必须与登记 CLI 位于同一会话。"""

    if pid <= 0:
        return None
    if os.name != "nt":
        return 0 if process_liveness(pid) == "running" else None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.ProcessIdToSessionId.argtypes = (
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.ProcessIdToSessionId.restype = wintypes.BOOL
    session = wintypes.DWORD()
    if not kernel32.ProcessIdToSessionId(int(pid), ctypes.byref(session)):
        return None
    return int(session.value)


class _MibTcpRowOwnerPid(ctypes.Structure):
    _fields_ = [
        ("state", wintypes.DWORD),
        ("local_address", wintypes.DWORD),
        ("local_port", wintypes.DWORD),
        ("remote_address", wintypes.DWORD),
        ("remote_port", wintypes.DWORD),
        ("owning_pid", wintypes.DWORD),
    ]


def loopback_tcp_client_pids(remote_port: int) -> set[int]:
    """返回正连接指定回环服务端口的 Windows TCP 客户端 PID。"""

    if not 1 <= remote_port <= 65535:
        return set()
    if os.name != "nt":
        # 非 Windows 仅用于开发测试；生产项目不会走此分支。
        return set()
    iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
    iphlpapi.GetExtendedTcpTable.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.ULONG),
        wintypes.BOOL,
        wintypes.ULONG,
        ctypes.c_int,
        wintypes.ULONG,
    )
    iphlpapi.GetExtendedTcpTable.restype = wintypes.DWORD
    size = wintypes.ULONG(0)
    result = iphlpapi.GetExtendedTcpTable(None, ctypes.byref(size), False, 2, 5, 0)
    if result not in (0, 122):
        raise OSError(int(result), "读取 Windows TCP owner 表大小失败")
    buffer = ctypes.create_string_buffer(max(int(size.value), ctypes.sizeof(wintypes.DWORD)))
    result = iphlpapi.GetExtendedTcpTable(
        buffer, ctypes.byref(size), False, 2, 5, 0
    )
    if result != 0:
        raise OSError(int(result), "读取 Windows TCP owner 表失败")
    count = ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD)).contents.value
    row_size = ctypes.sizeof(_MibTcpRowOwnerPid)
    offset = ctypes.sizeof(wintypes.DWORD)
    if offset + int(count) * row_size > len(buffer):
        raise OSError("Windows TCP owner 表长度异常")
    clients: set[int] = set()
    for index in range(int(count)):
        row = _MibTcpRowOwnerPid.from_buffer_copy(buffer, offset + index * row_size)
        if int(row.state) != 5:
            continue
        address = socket.inet_ntoa(struct.pack("<I", int(row.remote_address)))
        port = socket.ntohs(int(row.remote_port) & 0xFFFF)
        if address == "127.0.0.1" and port == int(remote_port):
            clients.add(int(row.owning_pid))
    return clients


def process_has_loopback_tcp_connection(pid: int, remote_port: int) -> bool:
    """用 Windows TCP owner 表证明指定 PID 正连接目标回环端口。"""

    if pid <= 0:
        return False
    return int(pid) in loopback_tcp_client_pids(remote_port)


def read_pid_file(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise InstanceError(f"PID 文件损坏：{path}") from exc
    pid = value.get("pid") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
    ):
        raise InstanceError(f"PID 文件格式错误：{path}")
    return value


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    """把完整 JSON 刷盘后原子发布，避免断电留下损坏的正式状态文件。"""

    data = (
        json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _state_process_running(state: Mapping[str, Any]) -> bool:
    """只按 PID 与创建时间判断一个已读取状态，避免 PID 重用。"""

    pid = state.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    actual = process_creation_time(pid)
    expected = state.get("creation_time")
    if actual is None:
        return False
    if os.name == "nt":
        # 缺失创建时间的旧/损坏 PID 文件无法抵御 PID 重用，必须视为失效。
        return isinstance(expected, int) and expected > 0 and actual == expected
    return expected == actual


def instance_running(path: Path) -> bool:
    state = read_pid_file(path)
    return state is not None and _state_process_running(state)


def _same_generation(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """PID 与创建时间必须同时相同；布尔值不能冒充整数。"""

    left_pid = left.get("pid")
    right_pid = right.get("pid")
    left_creation = left.get("creation_time")
    right_creation = right.get("creation_time")
    return (
        isinstance(left_pid, int)
        and not isinstance(left_pid, bool)
        and left_pid > 0
        and isinstance(left_creation, int)
        and not isinstance(left_creation, bool)
        and isinstance(right_pid, int)
        and not isinstance(right_pid, bool)
        and right_pid > 0
        and isinstance(right_creation, int)
        and not isinstance(right_creation, bool)
        and left_pid == right_pid
        and left_creation == right_creation
    )


def _acquire_stop_mutex(pid_file: Path, timeout_seconds: float = 2.0) -> InterprocessMutex:
    """短时串行化停止标记的发布、读取与清理。"""

    identity = f"stop:{os.path.normcase(str(pid_file.resolve()))}"
    deadline = time.monotonic() + timeout_seconds
    while True:
        mutex = InterprocessMutex(identity)
        try:
            mutex.acquire()
            return mutex
        except LockUnavailable as exc:
            mutex.close()
            if time.monotonic() >= deadline:
                raise InstanceError("停止请求状态正被另一进程更新") from exc
            time.sleep(0.01)


def acquire_instance(
    path: Path,
    config_path: Path,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    path = path.resolve()
    mutex = InterprocessMutex(f"service:{os.path.normcase(str(path))}")
    try:
        mutex.acquire()
    except LockUnavailable as exc:
        raise InstanceError("另一个服务实例持有单实例锁") from exc
    stop_mutex: InterprocessMutex | None = None
    try:
        # 新世代的 PID 发布与旧停止标记清理必须是同一个短事务。
        stop_mutex = _acquire_stop_mutex(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = read_pid_file(path)
        if existing is not None:
            if instance_running(path):
                raise InstanceError(f"服务已经运行，PID={existing['pid']}")
            path.unlink(missing_ok=True)
        # 必须在发布新 PID 前删除旧停止标记；此时 request_stop 尚不能命中新实例。
        stop_request_path(path).unlink(missing_ok=True)
        pid = os.getpid()
        image = process_image_path(pid)
        if image is None:
            raise InstanceError("无法读取当前进程映像路径，拒绝发布 PID 状态")
        creation_time = process_creation_time(pid)
        if not isinstance(creation_time, int) or isinstance(creation_time, bool):
            raise InstanceError("无法读取当前进程创建时间，拒绝发布 PID 状态")
        state: dict[str, Any] = {
            "pid": pid,
            "creation_time": creation_time,
            "image_path": os.fspath(image),
            "config_path": str(config_path.resolve()),
            "project_root": str(Path(__file__).resolve().parents[2]),
            "started_at": int(time.time()),
        }
        if metadata:
            reserved = set(state) | {"_mutex"}
            overlap = reserved.intersection(metadata)
            if overlap:
                raise InstanceError(
                    "实例元数据试图覆盖保留字段：" + ", ".join(sorted(overlap))
                )
            state.update(dict(metadata))
        try:
            # PID 状态必须一次性可见；崩溃时不能向恢复器暴露半截 JSON。
            _atomic_json(path, state)
        except OSError as exc:
            raise InstanceError("无法原子发布 PID 文件") from exc
        # 运行时对象只保留在内存返回值中，不写入 PID 文件。
        state["_mutex"] = mutex
        return state
    except BaseException:
        mutex.close()
        raise
    finally:
        if stop_mutex is not None:
            stop_mutex.close()


def release_instance(path: Path, state: dict[str, Any]) -> None:
    mutex = state.get("_mutex")
    try:
        try:
            current = read_pid_file(path)
        except InstanceError:
            return
        if current and current.get("pid") == state.get("pid") and current.get("creation_time") == state.get("creation_time"):
            path.unlink(missing_ok=True)
    finally:
        if isinstance(mutex, InterprocessMutex):
            mutex.close()


def stop_request_path(pid_file: Path) -> Path:
    return pid_file.with_name(pid_file.name + ".stop")


def _read_stop_request(path: Path) -> Mapping[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        # 旧版纯文本或损坏标记绝不能被解释成“停止当前世代”。
        return None
    except OSError as exc:
        raise InstanceError(f"无法读取停止请求：{path}") from exc
    if not isinstance(value, Mapping) or value.get("version") != 1:
        return None
    return value


def _write_stop_request(path: Path, payload: Mapping[str, Any]) -> None:
    _atomic_json(path, payload)


def request_stop(
    pid_file: Path,
    *,
    expected_state: Mapping[str, Any] | None = None,
) -> bool:
    """向当前精确进程世代发布协作停止请求，不会命中随后启动的新世代。"""

    mutex = _acquire_stop_mutex(pid_file)
    try:
        state = read_pid_file(pid_file)
        if state is None or not _state_process_running(state):
            return False
        if expected_state is not None and not _same_generation(state, expected_state):
            return False
        payload = {
            "version": 1,
            "pid": state["pid"],
            "creation_time": state.get("creation_time"),
            "requested_at": int(time.time()),
        }
        path = stop_request_path(pid_file)
        _write_stop_request(path, payload)
        current = read_pid_file(pid_file)
        if (
            current is not None
            and _same_generation(current, state)
            and _state_process_running(current)
        ):
            return True
        # 目标在发布期间退出；仅清除仍是本次 payload 的标记。
        if _read_stop_request(path) == payload:
            path.unlink(missing_ok=True)
        return False
    finally:
        mutex.close()


def stop_requested_for(pid_file: Path, state: Mapping[str, Any]) -> bool:
    """仅当停止标记精确绑定给 ``state`` 的进程世代时返回真。"""

    mutex = _acquire_stop_mutex(pid_file)
    try:
        request = _read_stop_request(stop_request_path(pid_file))
        return request is not None and _same_generation(request, state)
    finally:
        mutex.close()


def clear_stop_request(pid_file: Path, state: Mapping[str, Any]) -> bool:
    """只删除属于指定世代的停止标记，绝不清理另一代请求。"""

    mutex = _acquire_stop_mutex(pid_file)
    try:
        path = stop_request_path(pid_file)
        request = _read_stop_request(path)
        if request is None or not _same_generation(request, state):
            return False
        path.unlink(missing_ok=True)
        return True
    finally:
        mutex.close()
