"""Hot-path dispatcher that preserves an existing Codex notify integration."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .installer import PROJECT_ROOT, get_install_state


@dataclass(frozen=True, slots=True)
class ForwardResult:
    attempted: bool
    started: bool
    reason: str = ""


def _same_command(left: list[str], right: list[str]) -> bool:
    if os.name == "nt":
        return [os.path.normcase(os.path.normpath(item)) for item in left] == [
            os.path.normcase(os.path.normpath(item)) for item in right
        ]
    return left == right


def _write_dispatch_error(
    reason: str,
    project_root: str | os.PathLike[str] | None,
) -> None:
    root = Path(project_root).resolve() if project_root else PROJECT_ROOT
    path = root / ".state" / "dispatcher-errors.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{datetime.now(timezone.utc).isoformat()} {reason}\n")
    except OSError:
        pass


def _launch_argv(original: list[str]) -> list[str]:
    if os.name != "nt":
        return original
    executable = shutil.which(original[0]) or original[0]
    suffix = Path(executable).suffix.casefold()
    command = [executable, *original[1:]]
    if suffix in {".cmd", ".bat"}:
        return [os.environ.get("COMSPEC") or "cmd.exe", "/d", "/c", *command]
    if suffix == ".ps1":
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if shell:
            return [shell, "-NoProfile", "-File", *command]
    return command


def forward_original_notify(
    raw_json_argument: str,
    *,
    project_root: str | os.PathLike[str] | None = None,
    state_path: str | os.PathLike[str] | None = None,
) -> ForwardResult:
    """Start the pre-install notify command and return without waiting for it."""

    try:
        state = get_install_state(project_root, state_path=state_path)
    except Exception:
        _write_dispatch_error("state-unavailable", project_root)
        return ForwardResult(False, False, "state-unavailable")
    if state is None or not state.get("had_original_notify"):
        return ForwardResult(False, False, "no-original-notify")
    original = state.get("original_notify")
    installed = state.get("installed_notify")
    if not isinstance(original, list) or not all(
        isinstance(part, str) and part for part in original
    ):
        return ForwardResult(False, False, "invalid-original-notify")
    if isinstance(installed, list) and all(isinstance(part, str) for part in installed):
        if _same_command(original, installed):
            return ForwardResult(False, False, "recursive-original-notify")

    startupinfo = None
    creationflags = 0
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.Popen(
            [*_launch_argv(original), raw_json_argument],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            startupinfo=startupinfo,
            creationflags=creationflags,
        )
    except OSError:
        _write_dispatch_error("original-notify-start-failed", project_root)
        return ForwardResult(True, False, "original-notify-start-failed")
    return ForwardResult(True, True)


def dispatch_json_argument(
    raw_json_argument: str,
    config_path: str | os.PathLike[str] | None = None,
    *,
    dry_run: bool = False,
    project_root: str | os.PathLike[str] | None = None,
    state_path: str | os.PathLike[str] | None = None,
):
    """Forward first, then import and invoke the progress-notification stack."""

    forward_original_notify(
        raw_json_argument,
        project_root=project_root,
        state_path=state_path,
    )
    # Do not move this import to module scope: a broken optional integration
    # must never prevent codex-computer-use's turn-ended command from starting.
    from .runner import handle_event

    try:
        payload = json.loads(raw_json_argument)
    except json.JSONDecodeError as exc:
        raise ValueError("Codex notify argument is not valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("Codex notify argument must be a JSON object")
    return handle_event(payload, config_path, dry_run=dry_run)


def dispatch_event(
    payload: Mapping[str, Any],
    config_path: str | os.PathLike[str] | None = None,
    *,
    dry_run: bool = False,
):
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return dispatch_json_argument(raw, config_path, dry_run=dry_run)
