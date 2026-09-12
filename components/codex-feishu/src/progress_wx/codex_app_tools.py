"""Codex Desktop 动态应用工具的本地命名管道客户端。

本模块只读取 Codex Desktop 自己的日志来发现本机管道，并在调用工具前用
``tools/list`` 验明具体工具身份。生产路径只使用已验明的
``list_threads``、``wait_threads``、``send_message_to_thread`` 和
``set_thread_archived``；它不修改代理、环境变量、路由或 TUN，也不启动、停止或
接管 Codex 进程。
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import time
import uuid
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol


MAX_FRAME_BYTES = 8 * 1024 * 1024
_PIPE_NAME = re.compile(
    r"codex-browser-use-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_PIPE_LINE = re.compile(
    r"\[dynamic-app-tools-native-pipe\]\s+dynamic_app_tools_listening\s+"
    r"pipePath=(?P<path>\\\\\.\\pipe\\codex-browser-use-"
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)
_CODEX_CLI_LINE = re.compile(
    r"\bbrowser_use_runtime_paths_selected\b.*?\bcodexCliPath=(?P<path>.+?\\codex\.exe)\s+"
    r"codexCliPathSource=(?P<source>[^\s]+)"
)
_MAX_RUNTIME_LOG_BYTES = 32 * 1024 * 1024


class DesktopAppToolsError(RuntimeError):
    """Codex Desktop 应用工具调用失败。"""


class DesktopAppToolsUnavailable(DesktopAppToolsError):
    """尚未提交正文前，Desktop 或其工具管道不可用。"""


class DesktopAppToolsResultUnknown(DesktopAppToolsError):
    """正文已经进入写入阶段，但未取得可证明的结果。"""


class DesktopAppToolsNotSubmitted(DesktopAppToolsError):
    """工具调用在任何请求字节写入前失败，可以安全释放 claim。"""


class DesktopAppToolsRejected(DesktopAppToolsError):
    """Desktop 明确拒绝工具调用，已证明正文没有被接受。"""


class _FramePipe(Protocol):
    def request(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def close(self) -> None: ...


if os.name == "nt":
    _ULONG_PTR = ctypes.c_size_t

    class _OVERLAPPED(ctypes.Structure):
        _fields_ = [
            ("Internal", _ULONG_PTR),
            ("InternalHigh", _ULONG_PTR),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]


class _WindowsFramePipe:
    """使用 Windows Overlapped I/O 实现带硬超时的长度前缀 JSON 帧。"""

    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _OPEN_EXISTING = 3
    _FILE_FLAG_OVERLAPPED = 0x40000000
    _ERROR_IO_PENDING = 997
    _ERROR_MORE_DATA = 234
    _ERROR_SEM_TIMEOUT = 121
    _WAIT_TIMEOUT = 258
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    def __init__(self, pipe_path: str, *, connect_timeout: float, response_timeout: float):
        if os.name != "nt":
            raise DesktopAppToolsUnavailable("Desktop 应用工具命名管道仅支持 Windows")
        self.pipe_path = pipe_path
        self.response_timeout = max(0.1, float(response_timeout))
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_api()
        timeout_ms = max(1, min(int(connect_timeout * 1000), 2_147_483_647))
        if not self._kernel32.WaitNamedPipeW(pipe_path, timeout_ms):
            error = ctypes.get_last_error()
            raise DesktopAppToolsUnavailable(
                f"Codex Desktop 工具管道尚不可连接（Windows error {error}）"
            )
        handle = self._kernel32.CreateFileW(
            pipe_path,
            self._GENERIC_READ | self._GENERIC_WRITE,
            0,
            None,
            self._OPEN_EXISTING,
            self._FILE_FLAG_OVERLAPPED,
            None,
        )
        if handle == self._INVALID_HANDLE_VALUE:
            error = ctypes.get_last_error()
            raise DesktopAppToolsUnavailable(
                f"无法打开 Codex Desktop 工具管道（Windows error {error}）"
            )
        self._handle = handle

    def _configure_api(self) -> None:
        kernel32 = self._kernel32
        kernel32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
        kernel32.WaitNamedPipeW.restype = wintypes.BOOL
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CreateEventW.argtypes = [
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        kernel32.CreateEventW.restype = wintypes.HANDLE
        kernel32.ReadFile.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(_OVERLAPPED),
        ]
        kernel32.ReadFile.restype = wintypes.BOOL
        kernel32.WriteFile.argtypes = kernel32.ReadFile.argtypes
        kernel32.WriteFile.restype = wintypes.BOOL
        kernel32.GetOverlappedResultEx.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_OVERLAPPED),
            ctypes.POINTER(wintypes.DWORD),
            wintypes.DWORD,
            wintypes.BOOL,
        ]
        kernel32.GetOverlappedResultEx.restype = wintypes.BOOL
        kernel32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(_OVERLAPPED)]
        kernel32.CancelIoEx.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

    def _transfer(self, data: bytearray | bytes, *, write: bool) -> int:
        if self._handle is None:
            raise DesktopAppToolsUnavailable("Codex Desktop 工具管道已关闭")
        buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data) if write else (
            ctypes.c_ubyte * len(data)
        )()
        event = self._kernel32.CreateEventW(None, True, False, None)
        if not event:
            raise DesktopAppToolsUnavailable("无法创建命名管道等待事件")
        overlapped = _OVERLAPPED()
        overlapped.hEvent = event
        transferred = wintypes.DWORD(0)
        operation = self._kernel32.WriteFile if write else self._kernel32.ReadFile
        try:
            ok = operation(
                self._handle,
                ctypes.byref(buffer),
                len(data),
                ctypes.byref(transferred),
                ctypes.byref(overlapped),
            )
            if not ok:
                error = ctypes.get_last_error()
                if error != self._ERROR_IO_PENDING:
                    if not write and error == self._ERROR_MORE_DATA:
                        pass
                    else:
                        raise OSError(error, "命名管道 I/O 启动失败")
                else:
                    timeout_ms = max(
                        1,
                        min(int(self.response_timeout * 1000), 2_147_483_647),
                    )
                    ok = self._kernel32.GetOverlappedResultEx(
                        self._handle,
                        ctypes.byref(overlapped),
                        ctypes.byref(transferred),
                        timeout_ms,
                        True,
                    )
                    if not ok:
                        error = ctypes.get_last_error()
                        self._kernel32.CancelIoEx(
                            self._handle, ctypes.byref(overlapped)
                        )
                        if error in {self._ERROR_SEM_TIMEOUT, self._WAIT_TIMEOUT}:
                            raise TimeoutError("Codex Desktop 工具管道响应超时")
                        if not write and error == self._ERROR_MORE_DATA:
                            pass
                        else:
                            raise OSError(error, "命名管道 I/O 失败")
            count = int(transferred.value)
            if not write and count:
                data[:count] = bytes(buffer[:count])
            return count
        finally:
            self._kernel32.CloseHandle(event)

    def _write_all(self, data: bytes) -> None:
        offset = 0
        while offset < len(data):
            count = self._transfer(data[offset:], write=True)
            if count <= 0:
                raise OSError("Codex Desktop 工具管道写入了 0 字节")
            offset += count

    def _read_exactly(self, size: int) -> bytes:
        result = bytearray(size)
        offset = 0
        while offset < size:
            chunk = bytearray(size - offset)
            count = self._transfer(chunk, write=False)
            if count <= 0:
                raise EOFError("Codex Desktop 工具管道提前关闭")
            result[offset : offset + count] = chunk[:count]
            offset += count
        return bytes(result)

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._handle is None:
            raise DesktopAppToolsNotSubmitted("Codex Desktop 工具管道在写入前已关闭")
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if not encoded or len(encoded) > MAX_FRAME_BYTES:
            raise DesktopAppToolsNotSubmitted("Codex Desktop 工具请求大小无效")
        self._write_all(len(encoded).to_bytes(4, "little") + encoded)
        header = self._read_exactly(4)
        size = int.from_bytes(header, "little")
        if size <= 0 or size > MAX_FRAME_BYTES:
            raise DesktopAppToolsError("Codex Desktop 工具响应帧大小无效")
        try:
            response = json.loads(self._read_exactly(size).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DesktopAppToolsError("Codex Desktop 工具返回了无效 JSON") from exc
        if not isinstance(response, dict):
            raise DesktopAppToolsError("Codex Desktop 工具响应不是对象")
        return response

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            self._kernel32.CancelIoEx(handle, None)
            self._kernel32.CloseHandle(handle)


@dataclass(slots=True)
class VerifiedDesktopAppTools:
    """已在同一连接上验明工具身份、尚未提交正文的会话。"""

    pipe: _FramePipe
    tools: frozenset[str]
    source_pipe_path: str = ""
    _next_id: int = 2

    def _call(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        source_thread_id: str,
        call_tag: str,
        write: bool,
    ) -> dict[str, Any]:
        if tool not in self.tools:
            raise DesktopAppToolsUnavailable(f"Desktop 未验明 {tool} 工具")
        request_id = self._next_id
        self._next_id += 1
        try:
            response = self.pipe.request(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/call",
                    "params": {
                        "arguments": arguments,
                        "callId": f"progress-wx-{call_tag}-{uuid.uuid4().hex}",
                        "namespace": "codex_app",
                        "threadId": source_thread_id,
                        "turnId": f"progress-wx-{call_tag}",
                        "tool": tool,
                    },
                }
            )
        except (
            DesktopAppToolsNotSubmitted,
            DesktopAppToolsRejected,
            DesktopAppToolsResultUnknown,
            DesktopAppToolsUnavailable,
        ):
            raise
        except (OSError, EOFError, TimeoutError, DesktopAppToolsError) as exc:
            if write:
                raise DesktopAppToolsResultUnknown(
                    f"Desktop {tool} 工具提交后的传输结果未知"
                ) from exc
            raise
        if response.get("id") != request_id:
            error_type = DesktopAppToolsResultUnknown if write else DesktopAppToolsError
            raise error_type("Desktop 工具响应 ID 不一致")
        if "error" in response:
            error_type = DesktopAppToolsRejected if write else DesktopAppToolsError
            raise error_type(f"Desktop {tool} 工具明确返回调用错误")
        result = response.get("result")
        if (
            not isinstance(result, dict)
            or result.get("isError") is True
            or result.get("success") is False
        ):
            error_type = DesktopAppToolsRejected if write else DesktopAppToolsError
            raise error_type(f"Desktop {tool} 工具明确返回未接受结果")
        return result

    @staticmethod
    def _json_content(result: dict[str, Any], *, tool: str) -> dict[str, Any]:
        """严格提取 Desktop 动态工具返回的单个 JSON 文本对象。"""

        raw_items = result.get("contentItems", result.get("content"))
        if not isinstance(raw_items, list):
            raise DesktopAppToolsError(f"Desktop {tool} 工具缺少 contentItems")
        texts = [
            item.get("text")
            for item in raw_items
            if isinstance(item, dict)
            and item.get("type") in {"inputText", "text"}
            and isinstance(item.get("text"), str)
        ]
        if len(texts) != 1:
            raise DesktopAppToolsError(f"Desktop {tool} 工具没有返回唯一 JSON 正文")
        try:
            payload = json.loads(texts[0])
        except json.JSONDecodeError as exc:
            raise DesktopAppToolsError(f"Desktop {tool} 工具正文不是有效 JSON") from exc
        if not isinstance(payload, dict):
            raise DesktopAppToolsError(f"Desktop {tool} 工具 JSON 根节点不是对象")
        return payload

    def send_message(
        self,
        thread_id: str,
        prompt: str,
        *,
        call_tag: str,
        source_thread_id: str | None = None,
        host_id: str = "",
    ) -> dict[str, Any]:
        arguments = {"threadId": thread_id, "prompt": prompt}
        if host_id:
            arguments["hostId"] = host_id
        return self._call(
            "send_message_to_thread",
            arguments,
            source_thread_id=source_thread_id or thread_id,
            call_tag=call_tag,
            write=True,
        )

    def set_thread_archived(
        self,
        thread_id: str,
        *,
        archived: bool,
        source_thread_id: str,
        host_id: str = "",
        call_tag: str,
    ) -> dict[str, Any]:
        """通过 Codex Desktop 官方工具归档/恢复一个明确目标任务。"""

        target = str(thread_id or "").strip()
        source = str(source_thread_id or "").strip()
        if not target:
            raise ValueError("set_thread_archived thread_id 不能为空")
        if not source:
            raise ValueError("set_thread_archived source_thread_id 不能为空")
        if not isinstance(archived, bool):
            raise TypeError("set_thread_archived archived 必须是 bool")
        arguments: dict[str, Any] = {
            "threadId": target,
            "archived": archived,
        }
        host = str(host_id or "").strip()
        if host:
            arguments["hostId"] = host
        result = self._call(
            "set_thread_archived",
            arguments,
            source_thread_id=source,
            call_tag=call_tag,
            write=True,
        )
        if not (
            result.get("success") is True
            or isinstance(result.get("content"), list)
            or isinstance(result.get("contentItems"), list)
        ):
            raise DesktopAppToolsResultUnknown(
                "Desktop set_thread_archived 工具未返回可证明的成功结果"
            )
        return result

    def list_projects(
        self,
        source_thread_id: str,
        *,
        call_tag: str = "management-projects",
    ) -> dict[str, Any]:
        result = self._call(
            "list_projects",
            {},
            source_thread_id=source_thread_id,
            call_tag=call_tag,
            write=False,
        )
        return self._json_content(result, tool="list_projects")

    def read_thread(
        self,
        source_thread_id: str,
        thread_id: str,
        *,
        host_id: str = "",
        turn_limit: int = 10,
        include_outputs: bool = False,
        max_output_chars_per_item: int = 4000,
        call_tag: str = "management-read",
    ) -> dict[str, Any]:
        if not 1 <= int(turn_limit) <= 10:
            raise ValueError("read_thread turn_limit 必须介于 1 和 10")
        if not 0 <= int(max_output_chars_per_item) <= 20_000:
            raise ValueError("read_thread max_output_chars_per_item 必须介于 0 和 20000")
        arguments: dict[str, Any] = {
            "threadId": thread_id,
            "turnLimit": int(turn_limit),
            "includeOutputs": bool(include_outputs),
            "maxOutputCharsPerItem": int(max_output_chars_per_item),
        }
        if host_id:
            arguments["hostId"] = host_id
        result = self._call(
            "read_thread",
            arguments,
            source_thread_id=source_thread_id,
            call_tag=call_tag,
            write=False,
        )
        return self._json_content(result, tool="read_thread")

    def create_thread(
        self,
        source_thread_id: str,
        prompt: str,
        target: dict[str, Any],
        *,
        title: str = "",
        call_tag: str = "management-create",
    ) -> dict[str, Any]:
        if not prompt:
            raise ValueError("create_thread prompt 不能为空")
        arguments: dict[str, Any] = {"prompt": prompt, "target": target}
        if title:
            arguments["title"] = title
        result = self._call(
            "create_thread",
            arguments,
            source_thread_id=source_thread_id,
            call_tag=call_tag,
            write=True,
        )
        return self._json_content(result, tool="create_thread")

    def list_threads(
        self,
        source_thread_id: str,
        *,
        limit: int = 50,
        call_tag: str = "attention-list",
    ) -> dict[str, Any]:
        if not 1 <= int(limit) <= 50:
            raise ValueError("list_threads limit 必须介于 1 和 50")
        result = self._call(
            "list_threads",
            {"limit": int(limit)},
            source_thread_id=source_thread_id,
            call_tag=call_tag,
            write=False,
        )
        return self._json_content(result, tool="list_threads")

    def wait_threads(
        self,
        source_thread_id: str,
        targets: list[dict[str, str]],
        *,
        timeout_ms: int = 10_000,
        call_tag: str = "attention-wait",
    ) -> dict[str, Any]:
        if not 1 <= len(targets) <= 8:
            raise ValueError("wait_threads targets 必须包含 1 到 8 个任务")
        if not 0 <= int(timeout_ms) <= 120_000:
            raise ValueError("wait_threads timeout_ms 必须介于 0 和 120000")
        normalized: list[dict[str, str]] = []
        for target in targets:
            thread_id = str(target.get("threadId") or "").strip()
            if not thread_id or thread_id == source_thread_id:
                raise ValueError("wait_threads 目标不能为空且不能是调用来源任务")
            item = {"threadId": thread_id}
            for key in ("hostId", "afterCursor"):
                value = str(target.get(key) or "").strip()
                if value:
                    item[key] = value
            normalized.append(item)
        result = self._call(
            "wait_threads",
            {"targets": normalized, "timeoutMs": int(timeout_ms)},
            source_thread_id=source_thread_id,
            call_tag=call_tag,
            write=False,
        )
        return self._json_content(result, tool="wait_threads")

    def close(self) -> None:
        self.pipe.close()


class DesktopAppToolsClient:
    """发现并验证当前 Codex Desktop 的动态应用工具管道。"""

    def __init__(
        self,
        log_dir: Path,
        *,
        connect_timeout: float = 2.0,
        response_timeout: float = 30.0,
        connector: Callable[[str, float, float], _FramePipe] | None = None,
        live_pipe_names: Callable[[], list[str]] | None = None,
    ) -> None:
        self.log_dir = log_dir.expanduser().resolve()
        self.connect_timeout = float(connect_timeout)
        self.response_timeout = float(response_timeout)
        self._connector = connector or (
            lambda path, connect, response: _WindowsFramePipe(
                path,
                connect_timeout=connect,
                response_timeout=response,
            )
        )
        # 生产默认直接枚举当前 Windows 命名管道；使用测试 connector 时保持
        # 旧的纯日志夹具语义，测试可显式传入 live_pipe_names 覆盖。
        self._live_pipe_names = live_pipe_names or (
            (lambda: os.listdir(r"\\.\pipe")) if connector is None and os.name == "nt"
            else (lambda: [])
        )

    def _candidate_paths(self) -> list[str]:
        found: list[str] = []
        live_error: OSError | None = None
        try:
            for raw_name in self._live_pipe_names():
                name = str(raw_name)
                if _PIPE_NAME.fullmatch(name):
                    candidate = rf"\\.\pipe\{name}"
                    if candidate not in found:
                        found.append(candidate)
        except OSError as exc:
            # 日志仍可作为旧版 Desktop 或受限环境的发现回退；若两路都失败，
            # 下面会保留实时枚举异常为最终 cause。
            live_error = exc

        if not self.log_dir.is_dir():
            if found:
                return found
            raise DesktopAppToolsUnavailable("找不到 Codex Desktop 日志目录") from live_error
        try:
            files = sorted(
                self.log_dir.glob("**/codex-desktop-*-t0-*.log"),
                key=lambda item: item.stat().st_mtime_ns,
                reverse=True,
            )[:64]
        except OSError as exc:
            raise DesktopAppToolsUnavailable("无法枚举 Codex Desktop 日志") from exc
        for path in files:
            try:
                # 管道公布在启动日志前部；限制读取量，避免扫描长期大日志。
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    text = handle.read(256 * 1024)
            except OSError:
                continue
            matches = list(_PIPE_LINE.finditer(text))
            for match in reversed(matches):
                candidate = match.group("path")
                if candidate not in found:
                    found.append(candidate)
        if not found:
            raise DesktopAppToolsUnavailable(
                "Codex Desktop 日志中没有应用工具管道，实时枚举也没有有效候选"
            ) from live_error
        return found

    def _log_files(self) -> tuple[Path, ...]:
        if not self.log_dir.is_dir():
            raise DesktopAppToolsUnavailable("找不到 Codex Desktop 日志目录")
        try:
            return tuple(
                sorted(
                    self.log_dir.glob("**/codex-desktop-*-t0-*.log"),
                    key=lambda item: item.stat().st_mtime_ns,
                    reverse=True,
                )[:64]
            )
        except OSError as exc:
            raise DesktopAppToolsUnavailable("无法枚举 Codex Desktop 日志") from exc

    def discover_current_codex_cli(self) -> str:
        """从已验明的当前 Desktop 管道对应日志读取 bundled CLI 路径。"""

        session = self.open_verified(required_tools=("list_threads",))
        try:
            pipe_path = session.source_pipe_path
        finally:
            session.close()
        if not pipe_path:
            raise DesktopAppToolsUnavailable("已验证 Desktop 管道缺少来源身份")
        for path in self._log_files():
            matched_pipe = False
            cli_rows: list[tuple[str, str]] = []
            consumed = 0
            try:
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        consumed += len(line.encode("utf-8", errors="replace"))
                        if consumed > _MAX_RUNTIME_LOG_BYTES:
                            break
                        pipe_match = _PIPE_LINE.search(line)
                        if pipe_match and pipe_match.group("path") == pipe_path:
                            matched_pipe = True
                        cli_match = _CODEX_CLI_LINE.search(line)
                        if cli_match:
                            cli_rows.append(
                                (cli_match.group("path"), cli_match.group("source"))
                            )
            except OSError:
                continue
            if not matched_pipe:
                continue
            trusted = [value for value, source in cli_rows if source == "bundled-or-dev"]
            if trusted:
                return trusted[-1]
            raise DesktopAppToolsUnavailable(
                "当前 Codex Desktop 日志没有 bundled-or-dev CLI 身份"
            )
        raise DesktopAppToolsUnavailable(
            "无法把已验证 Desktop 管道关联到同一份运行日志"
        )

    def open_verified(
        self,
        *,
        required_tools: tuple[str, ...] = ("send_message_to_thread",),
    ) -> VerifiedDesktopAppTools:
        if not required_tools or any(not str(item).strip() for item in required_tools):
            raise ValueError("required_tools 不能为空")
        required = frozenset(required_tools)
        errors: list[BaseException] = []
        for pipe_path in self._candidate_paths():
            pipe: _FramePipe | None = None
            try:
                pipe = self._connector(
                    pipe_path,
                    self.connect_timeout,
                    self.response_timeout,
                )
                response = pipe.request(
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
                )
                if response.get("id") != 1 or "error" in response:
                    raise DesktopAppToolsUnavailable("Desktop 工具身份握手失败")
                tools = response.get("result", {}).get("tools", [])
                names = [
                    str(tool.get("name"))
                    for tool in tools
                    if isinstance(tool, dict)
                    and tool.get("namespace") == "codex_app"
                    and isinstance(tool.get("name"), str)
                ]
                available = frozenset(names)
                if any(names.count(name) != 1 for name in required) or not required <= available:
                    raise DesktopAppToolsUnavailable(
                        "Desktop 未公布全部且唯一的必需任务工具"
                    )
                return VerifiedDesktopAppTools(
                    pipe, available, source_pipe_path=pipe_path
                )
            except DesktopAppToolsUnavailable as exc:
                errors.append(exc)
                if pipe is not None:
                    pipe.close()
            except (OSError, EOFError, TimeoutError, DesktopAppToolsError) as exc:
                errors.append(exc)
                if pipe is not None:
                    pipe.close()
        raise DesktopAppToolsUnavailable(
            f"没有可验证的 Codex Desktop 工具管道（候选 {len(errors)} 个）"
        ) from (errors[-1] if errors else None)


def default_codex_desktop_log_dir() -> Path:
    """返回当前 Windows 用户的 Codex Desktop 日志目录。"""

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "Codex" / "Logs"
    return Path.home() / "AppData" / "Local" / "Codex" / "Logs"


__all__ = [
    "DesktopAppToolsClient",
    "DesktopAppToolsError",
    "DesktopAppToolsNotSubmitted",
    "DesktopAppToolsRejected",
    "DesktopAppToolsResultUnknown",
    "DesktopAppToolsUnavailable",
    "VerifiedDesktopAppTools",
    "default_codex_desktop_log_dir",
]
