"""Codex 完成通知热路径：先转发现有 notify，再写入本地队列。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import DEFAULT_CONFIG_PATH, load_config
from .installer import (
    INSTALL_STATE_PATH,
    _normalized_command,
    _previous_notify_wrapper,
)
from .state import StateStore


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _record_error(message: str) -> None:
    """热路径失败时只写最小日志，避免反过来阻塞 Codex。"""

    try:
        now = datetime.now(timezone.utc)
        log_dir = PROJECT_ROOT / "logs"
        path = log_dir / f"hook-errors.{now:%Y-%m-%d}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{now.isoformat()} {message}\n")
        cutoff = time.time() - timedelta(days=7).total_seconds()
        for candidate in log_dir.glob("hook-errors.????-??-??.log"):
            try:
                if candidate.stat().st_mtime < cutoff:
                    candidate.unlink()
            except OSError:
                pass
    except OSError:
        pass


def _load_install_state() -> dict[str, Any] | None:
    try:
        data = json.loads(INSTALL_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _command_list(value: Any) -> list[str] | None:
    """只接受不含空参数的 argv 数组，避免把损坏状态交给进程启动器。"""

    if not isinstance(value, list) or not value:
        return None
    if not all(isinstance(item, str) and item for item in value):
        return None
    return list(value)


def _current_codex_notify(state: dict[str, Any]) -> list[str] | None:
    """只读取安装状态指向的 Codex 顶层 notify。"""

    config_value = state.get("config_path")
    if not isinstance(config_value, str) or not config_value:
        return None
    try:
        parsed = tomllib.loads(Path(config_value).read_text(encoding="utf-8-sig"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    return _command_list(parsed.get("notify"))


def _load_original_notify() -> list[str] | None:
    state = _load_install_state()
    if state is None:
        return None
    original = _command_list(state.get("original_notify"))
    installed = _command_list(state.get("installed_notify"))
    if original is None:
        return None

    # 外部集成可能在本工具安装后重新包裹顶层 notify。例如 current 与
    # original 都是 computer-use 包装器，但 current 的子命令是本工具。
    # 此时再次执行完整 original 会让同一外层动作运行两遍；仅当前缀和子命令
    # 都严格匹配时，跳过重复外层并继续转发原先保存的更早通知器。
    current = _current_codex_notify(state)
    current_wrapper = _previous_notify_wrapper(current) if current else None
    original_wrapper = _previous_notify_wrapper(original)
    if current_wrapper and original_wrapper and installed:
        current_prefix, current_previous = current_wrapper
        original_prefix, original_previous = original_wrapper
        if (
            _normalized_command(current_prefix) == _normalized_command(original_prefix)
            and _normalized_command(current_previous) == _normalized_command(installed)
        ):
            return original_previous
    return original


def _windows_launch_command(command: list[str]) -> list[str]:
    if os.name != "nt":
        return command
    executable = shutil.which(command[0]) or command[0]
    suffix = Path(executable).suffix.casefold()
    if suffix in {".cmd", ".bat"}:
        return [os.environ.get("COMSPEC") or "cmd.exe", "/d", "/c", executable, *command[1:]]
    if suffix == ".ps1":
        shell = shutil.which("pwsh") or shutil.which("powershell") or "powershell.exe"
        return [shell, "-NoProfile", "-File", executable, *command[1:]]
    return [executable, *command[1:]]


def forward_original(raw_argument: str) -> bool:
    """异步转发安装前的 notify，确保其它 Codex 集成不被截断。"""

    original = _load_original_notify()
    if not original:
        return False
    installed_prefix = [
        str(Path(sys.executable).resolve()),
        str((PROJECT_ROOT / "progress-wx-hook.py").resolve()),
    ]
    if [os.path.normcase(item) for item in original[:2]] == [
        os.path.normcase(item) for item in installed_prefix
    ]:
        _record_error("拒绝递归调用原 notify")
        return False
    startupinfo = None
    flags = 0
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.Popen(
            [*_windows_launch_command(original), raw_argument],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            startupinfo=startupinfo,
            creationflags=flags,
        )
        return True
    except OSError as exc:
        _record_error(f"原 notify 启动失败：{type(exc).__name__}")
        return False


def enqueue(raw_argument: str, config_path: Path = DEFAULT_CONFIG_PATH) -> bool:
    try:
        payload: Any = json.loads(raw_argument)
        if not isinstance(payload, dict):
            raise ValueError("根节点不是对象")
        config = load_config(config_path)
        store = StateStore(config.service.database)
        try:
            # ``INSERT OR IGNORE`` 会把 Codex 重试送达的同一轮当作幂等成功；
            # 即使本次没有新增行，也不能让 notify 看到失败码并误以为入队失败。
            # 真正的 JSON、配置或协议错误仍会由异常路径返回 False。
            store.enqueue_hook_payload(payload)
            return True
        finally:
            store.close()
    except Exception as exc:
        _record_error(f"事件入队失败：{type(exc).__name__}: {exc}")
        return False


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) == 1:
        # 兼容升级前的两段式 notify 命令。
        config_path = DEFAULT_CONFIG_PATH
        raw = arguments[0]
    elif len(arguments) == 3 and arguments[0] == "--config":
        config_path = Path(arguments[1]).expanduser().resolve()
        raw = arguments[2]
    else:
        _record_error("notify 参数数量错误")
        return 2
    forward_original(raw)
    return 0 if enqueue(raw, config_path) else 1
