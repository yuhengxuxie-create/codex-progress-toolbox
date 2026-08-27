"""Codex App Server 的最小 stdio JSONL / 回环 WebSocket 客户端。

客户端只通过 ``subprocess.Popen`` 的 stdin/stdout 与一个明确的
``codex app-server`` 子进程通信，不使用 ``Popen(shell=True)``，也不拼接用户输入
到命令字符串；仅当配置入口本身是 Windows 脚本 shim 时显式调用对应解释器。它负责握手、恢复线程、启动新一轮、
向本连接持有的活动轮次追加输入，并把
``turn/completed`` 通知与审批/人工输入请求转换成结构化事件。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import queue
import shlex
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from types import MappingProxyType
from typing import Any, TextIO
from urllib.parse import urlsplit

from .codex_store import ThreadStatus


class CodexRPCError(RuntimeError):
    """App Server 启动、JSON-RPC 或协议错误。"""


class CodexRPCTimeout(CodexRPCError):
    """等待响应或生命周期事件超时。"""


class CodexRPCClosed(CodexRPCError):
    """App Server 已退出或连接已经关闭。"""


class CodexRPCUnhandledRequest(CodexRPCError):
    """调用方使用仅等待完成事件的接口，却收到必须答复的服务端请求。"""

    def __init__(self, request: "ServerRequest") -> None:
        super().__init__(f"Codex App Server 请求尚未处理：{request.method}")
        self.request = request


@dataclass(frozen=True, slots=True)
class TurnCompletedEvent:
    """从 ``turn/completed`` 通知提取的显式轮次状态。"""

    thread_id: str
    turn_id: str
    status: ThreadStatus
    raw_status: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            ThreadStatus.COMPLETED,
            ThreadStatus.INTERRUPTED,
            ThreadStatus.FAILED,
        }

    @classmethod
    def from_message(cls, message: Mapping[str, Any]) -> "TurnCompletedEvent | None":
        """仅解析方法名完全等于 ``turn/completed`` 的 JSON-RPC 通知。"""

        if message.get("method") != "turn/completed":
            return None
        params = message.get("params")
        if not isinstance(params, Mapping):
            return None

        turn = params.get("turn")
        turn_mapping = turn if isinstance(turn, Mapping) else {}
        thread_id = _first_text(params, "threadId", "thread_id") or _first_text(
            turn_mapping, "threadId", "thread_id"
        )
        turn_id = _first_text(params, "turnId", "turn_id") or _first_text(
            turn_mapping, "id", "turnId", "turn_id"
        )
        raw_status_value = _first_value(turn_mapping, "status")
        if raw_status_value is None:
            raw_status_value = _first_value(params, "status")
        raw_status = str(raw_status_value).strip() if raw_status_value is not None else ""
        if not thread_id or not raw_status:
            return None
        return cls(
            thread_id=thread_id,
            turn_id=turn_id,
            status=_status_from_wire(raw_status),
            raw_status=raw_status,
            raw=MappingProxyType(dict(message)),
        )


@dataclass(frozen=True, slots=True)
class ServerRequest:
    """Codex 通过同一 JSON-RPC 连接发起、必须由客户端回答的请求。"""

    request_id: str | int
    method: str
    params: Mapping[str, Any]
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def thread_id(self) -> str:
        return _first_text(self.params, "threadId", "thread_id", "conversationId")

    @property
    def turn_id(self) -> str:
        return _first_text(self.params, "turnId", "turn_id")

    @classmethod
    def from_message(cls, message: Mapping[str, Any]) -> "ServerRequest | None":
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params")
        if (
            not isinstance(method, str)
            or not method.strip()
            or isinstance(request_id, bool)
            or not isinstance(request_id, (str, int))
            or not isinstance(params, Mapping)
        ):
            return None
        return cls(
            request_id=request_id,
            method=method.strip(),
            params=MappingProxyType(dict(params)),
            raw=MappingProxyType(dict(message)),
        )


_EOF = object()


def _first_value(value: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in value:
            return value[key]
    return None


def _first_text(value: Mapping[str, Any], *keys: str) -> str:
    raw = _first_value(value, *keys)
    return raw.strip() if isinstance(raw, str) else ""


def _status_from_wire(value: str) -> ThreadStatus:
    """把协议状态字段映射到受控集合，不读取任何消息文本。"""

    aliases = {
        "completed": ThreadStatus.COMPLETED,
        "interrupted": ThreadStatus.INTERRUPTED,
        "failed": ThreadStatus.FAILED,
        "inprogress": ThreadStatus.IN_PROGRESS,
        "in_progress": ThreadStatus.IN_PROGRESS,
        "in-progress": ThreadStatus.IN_PROGRESS,
    }
    return aliases.get(value.strip().casefold(), ThreadStatus.UNKNOWN)


def command_argv(command: str | Sequence[str] = "codex") -> list[str]:
    """把可执行命令解析为参数数组，并补上 ``app-server`` 子命令。"""

    if isinstance(command, str):
        if not command.strip():
            raise ValueError("Codex command 不能为空")
        # 这里只做参数解析，不交给 shell 执行；Windows 下去除成对引号。
        text = command.strip()
        # 配置中可直接填写带空格的绝对 shim 路径；先走这条无歧义路径，
        # 避免 Windows 的 shlex 把路径拆成多个参数。带额外参数的命令仍
        # 使用下方受控的参数解析，并建议调用方为路径保留成对引号。
        direct = text
        if len(direct) >= 2 and direct[0] == direct[-1] and direct[0] in {'"', "'"}:
            direct = direct[1:-1]
        if os.name == "nt" and Path(direct).is_file():
            argv = [direct]
        else:
            argv = shlex.split(text, posix=os.name != "nt")
        if os.name == "nt":
            argv = [
                item[1:-1]
                if len(item) >= 2 and item[0] == item[-1] and item[0] in {'"', "'"}
                else item
                for item in argv
            ]
    else:
        argv = [os.fspath(item) for item in command]
    if not argv or any(not item for item in argv):
        raise ValueError("Codex command 不能为空")
    if argv[-1] != "app-server":
        argv.append("app-server")
    return argv


def validate_loopback_websocket_url(value: str) -> str:
    """规范化并限制到 IPv4 回环，拒绝代理可达或含凭据的地址。"""

    if not isinstance(value, str) or not value.strip():
        raise ValueError("WebSocket URL 不能为空")
    text = value.strip()
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError as exc:
        raise ValueError("WebSocket URL 端口格式无效") from exc
    if (
        parts.scheme.casefold() != "ws"
        or parts.hostname != "127.0.0.1"
        or port is None
        or not 1024 <= port <= 65535
        or parts.username is not None
        or parts.password is not None
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
    ):
        raise ValueError("WebSocket URL 只能是 ws://127.0.0.1:1024-65535/")
    return f"ws://127.0.0.1:{port}"


def _launch_argv(argv: Sequence[str]) -> list[str]:
    """为 Windows 的脚本 shim 生成显式参数数组，仍保持 ``shell=False``。"""

    result = list(argv)
    resolved = shutil.which(result[0]) or result[0]
    result[0] = resolved
    suffix = Path(resolved).suffix.casefold()
    if os.name == "nt" and suffix == ".ps1":
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if not powershell:
            raise CodexRPCError("运行 codex.ps1 需要 PowerShell")
        return [powershell, "-NoProfile", "-File", *result]
    if os.name == "nt" and suffix in {".cmd", ".bat"}:
        command_processor = os.environ.get("COMSPEC") or shutil.which("cmd.exe")
        if not command_processor:
            raise CodexRPCError("运行 codex.cmd 需要 cmd.exe")
        # 这是显式启动的 cmd.exe 子进程，不是 Popen(shell=True)。
        return [command_processor, "/d", "/c", *result]
    return result


class CodexAppServer:
    """同步 JSON-RPC 客户端，后台线程持续读取 JSONL 事件。"""

    def __init__(
        self,
        command: str | Sequence[str] = "codex",
        *,
        timeout_seconds: float = 10.0,
        client_name: str = "progress_wx",
        client_version: str = "0.1.0",
        max_line_bytes: int = 4 * 1024 * 1024,
        popen_factory: Callable[..., Any] = subprocess.Popen,
        websocket_url: str | None = None,
        websocket_factory: Callable[..., Any] | None = None,
        on_turn_completed: Callable[[TurnCompletedEvent], None] | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        if max_line_bytes <= 0:
            raise ValueError("max_line_bytes 必须大于 0")
        self.command = command
        self.timeout_seconds = float(timeout_seconds)
        self.client_name = client_name
        self.client_version = client_version
        self.max_line_bytes = int(max_line_bytes)
        self._popen = popen_factory
        self.websocket_url = (
            validate_loopback_websocket_url(websocket_url)
            if websocket_url is not None
            else None
        )
        self._websocket_factory = websocket_factory
        self._callback = on_turn_completed
        self._process: Any | None = None
        self._websocket: Any | None = None
        self._reader: threading.Thread | None = None
        self._incoming: queue.Queue[object] = queue.Queue()
        self._events: queue.Queue[object] = queue.Queue()
        self._next_id = 1
        self._write_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._closed = threading.Event()
        self._initialized = False
        self._initialize_result: Mapping[str, Any] = {}

    def __enter__(self) -> "CodexAppServer":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def process(self) -> Any | None:
        """只读暴露子进程对象，便于健康检查和测试。"""

        return self._process

    @property
    def is_running(self) -> bool:
        if self.websocket_url is not None:
            return self._websocket is not None and not self._closed.is_set()
        process = self._process
        if process is None:
            return False
        try:
            return process.poll() is None
        except (AttributeError, OSError):
            return False

    @property
    def transport(self) -> str:
        """返回当前配置的传输名称，不泄露端口之外的信息。"""

        return "websocket" if self.websocket_url is not None else "stdio"

    def _start_process(self) -> None:
        argv = _launch_argv(command_argv(self.command))
        startupinfo = None
        creationflags = 0
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            # shell=False 明确写出安全边界；事件数据永远不会成为命令字符串。
            process = self._popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                shell=False,
                startupinfo=startupinfo,
                creationflags=creationflags,
            )
        except (OSError, TypeError) as exc:
            raise CodexRPCError(f"无法启动 Codex App Server: {type(exc).__name__}") from exc
        self._process = process
        self._closed.clear()
        self._incoming = queue.Queue()
        self._events = queue.Queue()
        self._reader = threading.Thread(
            target=self._read_stdout,
            name="progress-wx-codex-reader",
            daemon=True,
        )
        self._reader.start()

    def _start_websocket(self) -> None:
        """连接已由本工具管理的回环 app-server；本客户端不拥有其进程。"""

        assert self.websocket_url is not None
        factory = self._websocket_factory
        if factory is None:
            try:
                import websocket  # type: ignore[import-not-found]
            except ImportError as exc:
                raise CodexRPCError(
                    "共享 Codex 需要 websocket-client；请重新运行安装脚本"
                ) from exc
            factory = websocket.create_connection
        try:
            connection = factory(
                self.websocket_url,
                timeout=self.timeout_seconds,
                enable_multithread=True,
                http_proxy_host=None,
                http_no_proxy=["127.0.0.1"],
                suppress_origin=True,
            )
            settimeout = getattr(connection, "settimeout", None)
            if callable(settimeout):
                settimeout(None)
        except (OSError, TypeError, ValueError) as exc:
            raise CodexRPCError(
                f"无法连接共享 Codex App Server: {type(exc).__name__}"
            ) from exc
        except Exception as exc:
            # websocket-client 使用自己的异常层次；不把 URL 以外的细节写入错误。
            raise CodexRPCError(
                f"无法连接共享 Codex App Server: {type(exc).__name__}"
            ) from exc
        self._websocket = connection
        self._closed.clear()
        self._incoming = queue.Queue()
        self._events = queue.Queue()
        self._reader = threading.Thread(
            target=self._read_websocket,
            name="progress-wx-codex-websocket-reader",
            daemon=True,
        )
        self._reader.start()

    def start(self) -> None:
        """启动进程并完成 initialize/initialized 握手。"""

        if self.is_running and self._initialized:
            return
        if (
            (self._process is not None or self._websocket is not None)
            and not self.is_running
        ):
            self.close()
        if self.websocket_url is None:
            self._start_process()
        else:
            self._start_websocket()
        try:
            response = self._request(
                "initialize",
                {
                    "clientInfo": {
                        "name": self.client_name,
                        "title": "进度通知",
                        "version": self.client_version,
                    }
                },
            )
            if not isinstance(response.get("result"), Mapping):
                raise CodexRPCError("Codex initialize 响应缺少 result")
            self._notify("initialized", {})
            self._initialize_result = MappingProxyType(dict(response["result"]))
            self._initialized = True
        except BaseException:
            self.close()
            raise

    def initialize(self) -> Mapping[str, Any]:
        """显式执行握手并返回 initialize 的 result。"""

        self.start()
        return self._initialize_result

    def close(self) -> None:
        """关闭本连接；只回收本客户端自己启动的 stdio 子进程。"""

        process, self._process = self._process, None
        websocket, self._websocket = self._websocket, None
        self._initialized = False
        self._initialize_result = {}
        self._closed.set()
        if websocket is not None:
            try:
                websocket.close()
            except Exception:
                pass
        if process is None:
            reader = self._reader
            if reader is not None and reader is not threading.current_thread():
                reader.join(timeout=min(2.0, self.timeout_seconds))
            self._reader = None
            self._incoming.put(_EOF)
            self._events.put(_EOF)
            return
        stdin = getattr(process, "stdin", None)
        if stdin is not None:
            try:
                stdin.close()
            except (OSError, ValueError):
                pass
        try:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=min(2.0, self.timeout_seconds))
        except (OSError, ValueError, AttributeError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except (OSError, ValueError, AttributeError):
                pass
            try:
                process.wait(timeout=2.0)
            except (OSError, ValueError, subprocess.TimeoutExpired, AttributeError):
                pass
        stdout = getattr(process, "stdout", None)
        if stdout is not None:
            try:
                stdout.close()
            except (OSError, ValueError):
                pass
        reader = self._reader
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=min(2.0, self.timeout_seconds))
        self._reader = None
        # 如果伪造进程没有触发 EOF，主动唤醒等待者。
        self._incoming.put(_EOF)
        self._events.put(_EOF)

    def _read_stdout(self) -> None:
        process = self._process
        stdout: TextIO | None = getattr(process, "stdout", None) if process else None
        if stdout is None:
            self._incoming.put(_EOF)
            self._events.put(_EOF)
            return
        try:
            for raw_line in stdout:
                if isinstance(raw_line, bytes):
                    line = raw_line.decode("utf-8", errors="replace")
                    line_size = len(raw_line)
                else:
                    line = str(raw_line)
                    line_size = len(line.encode("utf-8", errors="replace"))
                if line_size > self.max_line_bytes:
                    # 丢弃过大的单行，避免异常输出耗尽内存。
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except (TypeError, json.JSONDecodeError):
                    # 非 JSON 的诊断输出不是协议事件，安全忽略。
                    continue
                if not isinstance(message, dict):
                    continue
                self._route_message(message)
        except (OSError, ValueError):
            # 关闭 stdout 时，Windows 可能在迭代器上抛出句柄错误；统一走 EOF。
            pass
        finally:
            self._incoming.put(_EOF)
            self._events.put(_EOF)

    def _read_websocket(self) -> None:
        connection = self._websocket
        if connection is None:
            self._incoming.put(_EOF)
            self._events.put(_EOF)
            return
        try:
            while not self._closed.is_set():
                raw = connection.recv()
                if raw is None or raw == "":
                    break
                if isinstance(raw, bytes):
                    if len(raw) > self.max_line_bytes:
                        continue
                    text = raw.decode("utf-8", errors="strict")
                else:
                    text = str(raw)
                    if len(text.encode("utf-8", errors="strict")) > self.max_line_bytes:
                        continue
                try:
                    message = json.loads(text)
                except (TypeError, UnicodeError, json.JSONDecodeError):
                    continue
                if isinstance(message, dict):
                    self._route_message(message)
        except Exception:
            # close() 与连接中止都统一转成 EOF；业务层据此 fail-stop。
            pass
        finally:
            self._closed.set()
            self._incoming.put(_EOF)
            self._events.put(_EOF)

    def _route_message(self, message: dict[str, Any]) -> None:
        """把两种传输收到的同一协议消息路由到有界业务入口。"""

        event = TurnCompletedEvent.from_message(message)
        if event is not None:
            self._events.put(event)
            if self._callback is not None:
                try:
                    self._callback(event)
                except Exception:
                    # 回调属于业务层，不能杀死协议读取线程。
                    pass
            return
        server_request = ServerRequest.from_message(message)
        if server_request is not None:
            self._events.put(server_request)
            return
        # 只有客户端请求的响应进入响应队列。普通通知不应在长轮次中积压。
        if "id" in message and "method" not in message:
            self._incoming.put(message)

    @staticmethod
    def _json_line(message: Mapping[str, Any]) -> str:
        try:
            return json.dumps(
                dict(message), ensure_ascii=False, separators=(",", ":")
            ) + "\n"
        except (TypeError, ValueError) as exc:
            raise CodexRPCError("JSON-RPC 请求不可序列化") from exc

    def _write(self, message: Mapping[str, Any]) -> None:
        if self.websocket_url is not None:
            connection = self._websocket
            if connection is None or not self.is_running:
                raise CodexRPCClosed("共享 Codex App Server 未连接")
            payload = self._json_line(message).rstrip("\n")
            with self._write_lock:
                try:
                    connection.send(payload)
                except Exception as exc:
                    raise CodexRPCClosed("共享 Codex App Server 连接已关闭") from exc
            return
        process = self._process
        stdin = getattr(process, "stdin", None) if process is not None else None
        if process is None or stdin is None or not self.is_running:
            raise CodexRPCClosed("Codex App Server 未运行")
        line = self._json_line(message)
        with self._write_lock:
            try:
                stdin.write(line)
                stdin.flush()
            except TypeError:
                # 测试替身可能使用二进制 stdin；真实 Popen 使用 text=True。
                try:
                    stdin.write(line.encode("utf-8"))
                    stdin.flush()
                except (BrokenPipeError, OSError, ValueError) as exc:
                    raise CodexRPCClosed("Codex App Server 输入已关闭") from exc
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise CodexRPCClosed("Codex App Server 输入已关闭") from exc

    def _notify(self, method: str, params: Mapping[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": dict(params)})

    def _request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
        on_server_request: Callable[[ServerRequest], None] | None = None,
    ) -> dict[str, Any]:
        if not self.is_running:
            raise CodexRPCClosed("Codex App Server 未启动")
        timeout = self.timeout_seconds if timeout_seconds is None else float(timeout_seconds)
        if timeout <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        with self._request_lock:
            request_id = self._next_id
            self._next_id += 1
            message: dict[str, Any] = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
            }
            if params is not None:
                message["params"] = dict(params)
            self._write(message)
            deadline = time.monotonic() + timeout
            deferred: list[TurnCompletedEvent] = []
            try:
                while True:
                    # 处理远程审批可能耗时；回调返回后先接收已排队的响应，再判超时。
                    try:
                        item = self._incoming.get_nowait()
                    except queue.Empty:
                        item = None
                    if item is _EOF:
                        raise CodexRPCClosed(f"Codex App Server 在处理 {method} 时退出")
                    if isinstance(item, dict):
                        if item.get("id") != request_id:
                            # 单请求锁保证这里只可能是已超时旧请求的迟到响应。
                            continue
                        error = item.get("error")
                        if error is not None:
                            raise CodexRPCError(f"Codex App Server {method} 返回 JSON-RPC error")
                        return item

                    try:
                        event_item = self._events.get_nowait()
                    except queue.Empty:
                        event_item = None
                    if event_item is _EOF:
                        raise CodexRPCClosed(f"Codex App Server 在处理 {method} 时退出")
                    if isinstance(event_item, ServerRequest):
                        if on_server_request is None:
                            raise CodexRPCUnhandledRequest(event_item)
                        on_server_request(event_item)
                        continue
                    if isinstance(event_item, TurnCompletedEvent):
                        deferred.append(event_item)
                        continue

                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise CodexRPCTimeout(f"等待 {method} 响应超时")
                    try:
                        item = self._incoming.get(timeout=min(0.05, remaining))
                    except queue.Empty:
                        continue
                    if item is _EOF:
                        raise CodexRPCClosed(f"Codex App Server 在处理 {method} 时退出")
                    if not isinstance(item, dict) or item.get("id") != request_id:
                        continue
                    error = item.get("error")
                    if error is not None:
                        raise CodexRPCError(f"Codex App Server {method} 返回 JSON-RPC error")
                    return item
            finally:
                for deferred_event in deferred:
                    self._events.put(deferred_event)

    def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
        on_server_request: Callable[[ServerRequest], None] | None = None,
    ) -> dict[str, Any]:
        """发送一个请求；首次使用时自动启动并握手。"""

        self.start()
        return self._request(
            method,
            params,
            timeout_seconds=timeout_seconds,
            on_server_request=on_server_request,
        )

    def respond(self, request_id: str | int, result: Mapping[str, Any]) -> None:
        """在原连接上回答 Codex 发起的 JSON-RPC 请求；响应只写入一次。"""

        if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
            raise ValueError("request_id 必须是字符串或整数")
        if not isinstance(result, Mapping):
            raise TypeError("result 必须是映射")
        self._write({"jsonrpc": "2.0", "id": request_id, "result": dict(result)})

    def resume_thread(
        self,
        thread_id: str,
        *,
        timeout_seconds: float | None = None,
        on_server_request: Callable[[ServerRequest], None] | None = None,
        **options: Any,
    ) -> dict[str, Any]:
        """调用官方 ``thread/resume``，只把 thread id 作为结构化参数发送。"""

        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError("thread_id 不能为空")
        params = dict(options)
        params["threadId"] = thread_id
        return self.request(
            "thread/resume",
            params,
            timeout_seconds=timeout_seconds,
            on_server_request=on_server_request,
        )

    resume = resume_thread

    def read_thread(
        self,
        thread_id: str,
        *,
        include_turns: bool = False,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """调用官方 ``thread/read``，不恢复线程、不订阅事件。"""

        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError("thread_id 不能为空")
        return self.request(
            "thread/read",
            {"threadId": thread_id, "includeTurns": bool(include_turns)},
            timeout_seconds=timeout_seconds,
        )

    @staticmethod
    def active_turn_id(response: Mapping[str, Any]) -> str:
        """只接受 ``thread/read`` 中唯一、显式 ``inProgress`` 的 turn。

        返回空字符串表示没有活动轮次；协议结构不完整或出现多个活动轮次时抛错，
        绝不通过数组最后一项或聊天文本猜测。
        """

        result = response.get("result")
        thread = result.get("thread") if isinstance(result, Mapping) else None
        if not isinstance(thread, Mapping):
            raise CodexRPCError("thread/read 响应缺少 thread")
        turns = thread.get("turns")
        if not isinstance(turns, list):
            raise CodexRPCError("thread/read 响应缺少 turns")
        active: list[str] = []
        for turn in turns:
            if not isinstance(turn, Mapping):
                continue
            status = _first_text(turn, "status").casefold().replace("_", "")
            if status != "inprogress":
                continue
            turn_id = _first_text(turn, "id", "turnId", "turn_id")
            if not turn_id:
                raise CodexRPCError("活动 turn 缺少 id")
            active.append(turn_id)
        if len(active) > 1:
            raise CodexRPCError("thread/read 返回多个活动 turn，拒绝猜测")
        return active[0] if active else ""

    @staticmethod
    def _normalize_input(value: Any) -> list[Any]:
        if isinstance(value, str):
            return [{"type": "text", "text": value}]
        if isinstance(value, Mapping):
            return [dict(value)]
        if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
            return list(value)
        raise TypeError("turn 输入必须是字符串、对象或数组")

    def start_turn(
        self,
        thread_id: str,
        message: Any = None,
        *,
        input: Any = None,
        timeout_seconds: float | None = None,
        on_server_request: Callable[[ServerRequest], None] | None = None,
        **options: Any,
    ) -> dict[str, Any]:
        """调用 ``turn/start``；字符串会变成标准 text 输入对象。"""

        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError("thread_id 不能为空")
        if message is not None and input is not None:
            raise ValueError("message 与 input 只能提供一个")
        selected = input if input is not None else message
        if selected is None:
            raise ValueError("turn 输入不能为空")
        params = dict(options)
        params["threadId"] = thread_id
        params["input"] = self._normalize_input(selected)
        return self.request(
            "turn/start",
            params,
            timeout_seconds=timeout_seconds,
            on_server_request=on_server_request,
        )

    turn_start = start_turn

    def steer_turn(
        self,
        thread_id: str,
        expected_turn_id: str,
        message: Any = None,
        *,
        input: Any = None,
        timeout_seconds: float | None = None,
        on_server_request: Callable[[ServerRequest], None] | None = None,
    ) -> dict[str, Any]:
        """调用官方 ``turn/steer``，只追加到精确匹配的活动轮次。

        ``expectedTurnId`` 是强制安全门；调用方不能省略或退回“当前任意轮次”。
        该方法只适用于本客户端所连接 app-server 持有的活动轮次，不能跨进程
        接管 Codex Desktop 私有 stdio 中的轮次。
        """

        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError("thread_id 不能为空")
        if not isinstance(expected_turn_id, str) or not expected_turn_id.strip():
            raise ValueError("expected_turn_id 不能为空")
        if message is not None and input is not None:
            raise ValueError("message 与 input 只能提供一个")
        selected = input if input is not None else message
        if selected is None:
            raise ValueError("steer 输入不能为空")
        return self.request(
            "turn/steer",
            {
                "threadId": thread_id,
                "expectedTurnId": expected_turn_id,
                "input": self._normalize_input(selected),
            },
            timeout_seconds=timeout_seconds,
            on_server_request=on_server_request,
        )

    turn_steer = steer_turn

    def listen_event(
        self,
        thread_id: str | None = None,
        *,
        turn_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> TurnCompletedEvent | ServerRequest:
        """等待完成事件或服务端请求；其它线程事件会保留给后续调用。"""

        self.start()
        timeout = self.timeout_seconds if timeout_seconds is None else float(timeout_seconds)
        if timeout <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        deadline = time.monotonic() + timeout
        deferred: list[TurnCompletedEvent | ServerRequest] = []
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CodexRPCTimeout("等待 Codex 生命周期事件超时")
                try:
                    item = self._events.get(timeout=remaining)
                except queue.Empty as exc:
                    raise CodexRPCTimeout("等待 Codex 生命周期事件超时") from exc
                if item is _EOF:
                    raise CodexRPCClosed("Codex App Server 已关闭")
                if not isinstance(item, (TurnCompletedEvent, ServerRequest)):
                    continue
                item_thread_id = item.thread_id
                if thread_id is not None and item_thread_id and item_thread_id != thread_id:
                    deferred.append(item)
                    continue
                item_turn_id = item.turn_id
                if turn_id is not None and item_turn_id and item_turn_id != turn_id:
                    deferred.append(item)
                    continue
                return item
        finally:
            for item in deferred:
                self._events.put(item)

    def listen_turn_completed(
        self,
        thread_id: str | None = None,
        *,
        turn_id: str | None = None,
        timeout_seconds: float | None = None,
        callback: Callable[[TurnCompletedEvent], None] | None = None,
    ) -> TurnCompletedEvent:
        """等待下一个目标线程的 ``turn/completed`` 事件。"""

        self.start()
        timeout = self.timeout_seconds if timeout_seconds is None else float(timeout_seconds)
        if timeout <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexRPCTimeout("等待 turn/completed 事件超时")
            item = self.listen_event(
                thread_id,
                turn_id=turn_id,
                timeout_seconds=remaining,
            )
            if isinstance(item, ServerRequest):
                raise CodexRPCUnhandledRequest(item)
            if callback is not None:
                callback(item)
            return item

    wait_for_turn_completed = listen_turn_completed

    def iter_turn_completed(
        self,
        *,
        thread_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> Iterable[TurnCompletedEvent]:
        """迭代生命周期事件，适合常驻监控循环。"""

        self.start()
        while True:
            event = self.listen_turn_completed(
                thread_id=thread_id, timeout_seconds=timeout_seconds
            )
            yield event


__all__ = [
    "CodexAppServer",
    "CodexRPCClosed",
    "CodexRPCError",
    "CodexRPCUnhandledRequest",
    "CodexRPCTimeout",
    "ServerRequest",
    "TurnCompletedEvent",
    "command_argv",
    "validate_loopback_websocket_url",
]
