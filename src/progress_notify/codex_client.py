"""Minimal stdio client for the stable Codex App Server API.

The client intentionally uses a fresh subprocess for a short group of requests.
That keeps the notify hook stateless and, more importantly, guarantees that a
timed-out App Server is reaped instead of being left behind.
"""

from __future__ import annotations

import json
import os
import queue
import shlex
import shutil
import subprocess
import threading
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any


class CodexAppServerError(RuntimeError):
    """Raised when the Codex App Server cannot satisfy a request."""


_EOF = object()
def _command_argv(command: str | Sequence[str]) -> list[str]:
    if isinstance(command, str):
        command = command.strip()
        if not command:
            raise ValueError("Codex command cannot be empty")
        # An absolute executable path can contain spaces and needs no shell
        # parsing. Environment expansion is useful in shared configurations.
        expanded = os.path.expandvars(os.path.expanduser(command))
        if os.path.isfile(expanded):
            argv = [expanded]
        else:
            argv = shlex.split(expanded, posix=os.name != "nt")
            if os.name == "nt":
                argv = [
                    part[1:-1]
                    if len(part) >= 2 and part[0] == part[-1] and part[0] in {'"', "'"}
                    else part
                    for part in argv
                ]
    else:
        argv = [os.fspath(part) for part in command]
        if not argv or any(not part for part in argv):
            raise ValueError("Codex command cannot be empty")

    resolved = shutil.which(argv[0])
    if resolved:
        argv[0] = resolved
    suffix = Path(argv[0]).suffix.lower()
    if os.name == "nt" and suffix == ".ps1":
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if not shell:
            raise CodexAppServerError("PowerShell is required to run the configured Codex command")
        argv = [shell, "-NoProfile", "-File", *argv]
    elif os.name == "nt" and suffix in {".cmd", ".bat"}:
        # CreateProcess cannot execute batch files directly (WinError 5). Keep
        # shell interpretation constrained to this already-resolved executable;
        # request arguments remain separate and are quoted by subprocess.
        command_processor = os.environ.get("COMSPEC") or "cmd.exe"
        argv = [command_processor, "/d", "/c", *argv]
    return argv


class CodexAppServerClient:
    """Synchronous JSON-lines client with bounded reads and deterministic cleanup."""

    def __init__(
        self,
        command: str | Sequence[str] = "codex",
        timeout_seconds: float = 10.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        self.command = command
        self.timeout_seconds = float(timeout_seconds)
        self._process: subprocess.Popen[str] | None = None
        self._messages: queue.Queue[object] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._next_id = 1

    def __enter__(self) -> "CodexAppServerClient":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def start(self) -> None:
        if self._process is not None:
            return
        # A client may be explicitly closed and reused. Do not let the prior
        # reader's EOF sentinel poison the next connection.
        self._messages = queue.Queue()
        self._next_id = 1
        argv = _command_argv(self.command)
        if not (len(argv) >= 2 and argv[-1] == "app-server"):
            argv.append("app-server")
        startupinfo = None
        creationflags = 0
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._process = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                startupinfo=startupinfo,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise CodexAppServerError(
                f"Unable to start Codex App Server ({argv[0]}): {exc}"
            ) from exc

        self._reader = threading.Thread(
            target=self._read_stdout,
            name="progress-notify-codex-reader",
            daemon=True,
        )
        self._reader.start()
        try:
            response = self._request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "progress_notify",
                        "title": "Codex Progress Notify",
                        "version": "1.0.0",
                    }
                },
            )
            if "result" not in response:
                raise CodexAppServerError("Codex initialize returned no result")
            self._notify("initialized", {})
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=2.0)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    process.wait(timeout=2.0)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        if process.stdout is not None:
            try:
                process.stdout.close()
            except OSError:
                pass

    def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            self._messages.put(_EOF)
            return
        try:
            for line in process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(message, dict):
                    self._messages.put(message)
        finally:
            self._messages.put(_EOF)

    def _write(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.poll() is not None:
            raise CodexAppServerError("Codex App Server is not running")
        try:
            process.stdin.write(
                json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
            )
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise CodexAppServerError("Codex App Server closed its input") from exc

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"method": method, "params": params})

    def _request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        message: dict[str, Any] = {"method": method, "id": request_id}
        if params is not None:
            message["params"] = params
        self._write(message)

        deadline = time.monotonic() + self.timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexAppServerError(
                    f"Timed out after {self.timeout_seconds:g}s waiting for {method}"
                )
            try:
                response = self._messages.get(timeout=remaining)
            except queue.Empty as exc:
                raise CodexAppServerError(
                    f"Timed out after {self.timeout_seconds:g}s waiting for {method}"
                ) from exc
            if response is _EOF:
                code = self._process.poll() if self._process is not None else None
                raise CodexAppServerError(
                    f"Codex App Server exited while handling {method} (code {code})"
                )
            assert isinstance(response, dict)
            if response.get("id") != request_id:
                # Notifications do not carry an id. There is only one outstanding
                # request in this client, so unrelated messages can be discarded.
                continue
            error = response.get("error")
            if error is not None:
                raise CodexAppServerError(
                    f"Codex App Server {method} failed: "
                    + json.dumps(error, ensure_ascii=False)
                )
            return response

    def read_thread(self, thread_id: str, include_turns: bool = False) -> dict[str, Any]:
        if not isinstance(thread_id, str) or not thread_id:
            raise ValueError("thread_id must be a non-empty string")
        self.start()
        response = self._request(
            "thread/read",
            {"threadId": thread_id, "includeTurns": bool(include_turns)},
        )
        thread = response.get("result", {}).get("thread")
        if not isinstance(thread, dict):
            raise CodexAppServerError("thread/read returned no thread")
        return thread

    def get_thread_name(self, thread_id: str) -> str | None:
        name = self.read_thread(thread_id, include_turns=False).get("name")
        return name if isinstance(name, str) and name.strip() else None

    def list_threads(
        self,
        *,
        archived: bool = False,
        limit: int = 100,
        source_kinds: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        self.start()
        remaining = limit
        cursor: str | None = None
        threads: list[dict[str, Any]] = []
        while remaining > 0:
            params: dict[str, Any] = {
                "cursor": cursor,
                "limit": min(remaining, 100),
                "sortKey": "updated_at",
                "sortDirection": "desc",
                "archived": archived,
            }
            if source_kinds is not None:
                params["sourceKinds"] = list(source_kinds)
            response = self._request("thread/list", params)
            result = response.get("result", {})
            data = result.get("data")
            if not isinstance(data, list):
                raise CodexAppServerError("thread/list returned invalid data")
            page = [item for item in data if isinstance(item, dict)]
            threads.extend(page)
            remaining = limit - len(threads)
            next_cursor = result.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor or not page:
                break
            cursor = next_cursor
        return threads[:limit]


def read_thread_name(
    thread_id: str,
    command: str | Sequence[str] = "codex",
    timeout_seconds: float = 10.0,
) -> str | None:
    """Convenience helper for the notify hot path."""

    with CodexAppServerClient(command, timeout_seconds) as client:
        return client.get_thread_name(thread_id)


def list_codex_threads(
    command: str | Sequence[str] = "codex",
    timeout_seconds: float = 10.0,
    limit: int = 100,
) -> list[dict[str, Any]]:
    with CodexAppServerClient(command, timeout_seconds) as client:
        return client.list_threads(limit=limit)
