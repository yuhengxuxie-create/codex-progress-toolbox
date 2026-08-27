"""Install the user-global PermissionRequest hook without replacing other hooks."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Mapping


class HooksInstallError(RuntimeError):
    pass


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"hooks": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HooksInstallError("现有 hooks.json 无法安全解析") from exc
    if not isinstance(payload, dict):
        raise HooksInstallError("现有 hooks.json 顶层不是对象")
    hooks = payload.get("hooks")
    if hooks is None:
        payload["hooks"] = {}
    elif not isinstance(hooks, dict):
        raise HooksInstallError("现有 hooks.json 的 hooks 字段不是对象")
    return payload


def _is_ours(entry: Any) -> bool:
    if not isinstance(entry, Mapping):
        return False
    hooks = entry.get("hooks")
    if not isinstance(hooks, list):
        return False
    return any(
        isinstance(item, Mapping)
        and "progress-wx.py" in str(item.get("command") or "").casefold()
        and "permission-hook" in str(item.get("command") or "").casefold()
        for item in hooks
    )


def _write(path: Path, payload: Mapping[str, Any]) -> None:
    rendered = json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def install_permission_hook(
    *,
    hooks_file: Path,
    python_executable: Path,
    entry_script: Path,
    config_file: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    path = Path(hooks_file).expanduser().resolve()
    payload = _read(path)
    hooks = payload["hooks"]
    assert isinstance(hooks, dict)
    existing = hooks.get("PermissionRequest")
    if existing is not None and not isinstance(existing, list):
        raise HooksInstallError("现有 PermissionRequest Hook 不是列表，拒绝覆盖")
    entries = list(existing) if isinstance(existing, list) else []
    entries = [item for item in entries if not _is_ours(item)]
    command = subprocess.list2cmdline(
        [
            str(Path(python_executable).resolve()),
            str(Path(entry_script).resolve()),
            "--config",
            str(Path(config_file).resolve()),
            "permission-hook",
        ]
    )
    entry = {
        "matcher": "*",
        "hooks": [
            {
                "type": "command",
                "command": command,
                "timeout": int(timeout_seconds),
                "statusMessage": "等待飞书审批",
            }
        ],
    }
    entries.append(entry)
    hooks["PermissionRequest"] = entries
    _write(path, payload)
    return {
        "schema_version": 1,
        "installed": True,
        "hooks_file": str(path),
        "timeout_seconds": int(timeout_seconds),
    }


def uninstall_permission_hook(*, hooks_file: Path) -> dict[str, Any]:
    path = Path(hooks_file).expanduser().resolve()
    payload = _read(path)
    hooks = payload["hooks"]
    assert isinstance(hooks, dict)
    existing = hooks.get("PermissionRequest")
    if existing is not None and not isinstance(existing, list):
        raise HooksInstallError("现有 PermissionRequest Hook 不是列表，拒绝修改")
    entries = list(existing) if isinstance(existing, list) else []
    remaining = [item for item in entries if not _is_ours(item)]
    removed = len(entries) - len(remaining)
    if remaining:
        hooks["PermissionRequest"] = remaining
    else:
        hooks.pop("PermissionRequest", None)
    if removed:
        _write(path, payload)
    return {
        "schema_version": 1,
        "removed": removed,
        "hooks_file": str(path),
    }


__all__ = [
    "HooksInstallError",
    "install_permission_hook",
    "uninstall_permission_hook",
]
