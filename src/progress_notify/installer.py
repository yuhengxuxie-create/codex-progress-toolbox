"""Safe installation of the user-level Codex ``notify`` command."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATE_RELATIVE_PATH = Path(".state") / "install-state.json"
ENTRYPOINT_NAME = "progress-notify.py"


class InstallError(RuntimeError):
    """Raised when changing Codex configuration would be unsafe."""


def default_codex_home() -> Path:
    value = os.environ.get("CODEX_HOME")
    return Path(value).expanduser() if value else Path.home() / ".codex"


def default_state_path(project_root: str | os.PathLike[str] | None = None) -> Path:
    root = Path(project_root).resolve() if project_root else PROJECT_ROOT
    return root / STATE_RELATIVE_PATH


def installed_notify_command(
    project_root: str | os.PathLike[str] | None = None,
    python_executable: str | os.PathLike[str] | None = None,
) -> list[str]:
    root = Path(project_root).resolve() if project_root else PROJECT_ROOT
    entrypoint = root / ENTRYPOINT_NAME
    if not entrypoint.is_file():
        raise InstallError(f"Entrypoint not found: {entrypoint}")
    executable = Path(python_executable or sys.executable).resolve()
    if not executable.is_file():
        raise InstallError(f"Python executable not found: {executable}")
    return [str(executable), str(entrypoint.resolve())]


def _load_toml(path: Path) -> tuple[str, dict[str, Any]]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return "", {}
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InstallError(f"Codex config is not UTF-8: {path}") from exc
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise InstallError(f"Codex config is invalid TOML: {exc}") from exc
    return text, parsed


def _notify_value(parsed: dict[str, Any]) -> tuple[bool, list[str] | None]:
    if "notify" not in parsed:
        return False, None
    value = parsed["notify"]
    if not isinstance(value, list) or not all(isinstance(part, str) for part in value):
        raise InstallError("Top-level notify must be an array of strings")
    return True, list(value)


def _assignment_key(line: str) -> tuple[str | None, int]:
    """Return a simple top-level key and the index after its equals sign."""

    in_basic = False
    in_literal = False
    escaped = False
    for index, char in enumerate(line):
        if in_basic:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_basic = False
            continue
        if in_literal:
            if char == "'":
                in_literal = False
            continue
        if char == "#":
            return None, -1
        if char == '"':
            in_basic = True
            continue
        if char == "'":
            in_literal = True
            continue
        if char == "=":
            token = line[:index].strip()
            if token in {"notify", '"notify"', "'notify'"}:
                return "notify", index + 1
            return token or None, index + 1
    return None, -1


def _array_assignment_end(text: str, value_start: int) -> int:
    """Find the line end after a TOML array while respecting quoted strings."""

    depth = 0
    started = False
    quote: str | None = None
    escaped = False
    comment = False
    index = value_start
    while index < len(text):
        char = text[index]
        if comment:
            if char in "\r\n":
                comment = False
            index += 1
            continue
        if quote == '"':
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quote = None
            index += 1
            continue
        if quote == "'":
            if char == "'":
                quote = None
            index += 1
            continue
        if char == "#":
            comment = True
        elif char in {'"', "'"}:
            quote = char
        elif char == "[":
            started = True
            depth += 1
        elif char == "]" and started:
            depth -= 1
            if depth == 0:
                newline = text.find("\n", index)
                return len(text) if newline < 0 else newline + 1
        index += 1
    raise InstallError("Could not locate the end of top-level notify array")


def _scan_container_state(
    line: str,
    depth: int,
    multiline_quote: str | None,
) -> tuple[int, str | None]:
    """Track arrays/inline tables so a continuation ``]`` is not a table header."""

    index = 0
    quote = multiline_quote
    while index < len(line):
        if quote in {'"""', "'''"}:
            closing = line.find(quote, index)
            if closing < 0:
                return depth, quote
            # A backslash can escape a basic multiline quote. Counting the run
            # immediately before it is enough for structural scanning.
            if quote == '"""':
                slashes = 0
                cursor = closing - 1
                while cursor >= 0 and line[cursor] == "\\":
                    slashes += 1
                    cursor -= 1
                if slashes % 2:
                    index = closing + 1
                    continue
            quote = None
            index = closing + 3
            continue

        char = line[index]
        if char == "#":
            break
        triple = line[index : index + 3]
        if triple in {'"""', "'''"}:
            quote = triple
            index += 3
            continue
        if char in {'"', "'"}:
            delimiter = char
            index += 1
            escaped = False
            while index < len(line):
                current = line[index]
                if delimiter == '"' and escaped:
                    escaped = False
                elif delimiter == '"' and current == "\\":
                    escaped = True
                elif current == delimiter:
                    index += 1
                    break
                index += 1
            continue
        if char in "[{":
            depth += 1
        elif char in "]}" and depth > 0:
            depth -= 1
        index += 1
    return depth, quote


def _find_notify_span(text: str) -> tuple[int, int] | None:
    offset = 0
    depth = 0
    multiline_quote: str | None = None
    for line in text.splitlines(keepends=True):
        if depth == 0 and multiline_quote is None:
            stripped = line.lstrip()
            if stripped.startswith("["):
                break
            key, value_column = _assignment_key(line)
            if key == "notify":
                return offset, _array_assignment_end(text, offset + value_column)
        depth, multiline_quote = _scan_container_state(
            line,
            depth,
            multiline_quote,
        )
        offset += len(line)
    return None


def _first_table_offset(text: str) -> int:
    offset = 0
    depth = 0
    multiline_quote: str | None = None
    for line in text.splitlines(keepends=True):
        if depth == 0 and multiline_quote is None and line.lstrip().startswith("["):
            return offset
        depth, multiline_quote = _scan_container_state(
            line,
            depth,
            multiline_quote,
        )
        offset += len(line)
    return len(text)


def _toml_notify_line(command: list[str]) -> str:
    values = ", ".join(json.dumps(part, ensure_ascii=False) for part in command)
    return f"notify = [ {values} ]\n"


def _replace_notify(text: str, command: list[str] | None) -> str:
    span = _find_notify_span(text)
    replacement = "" if command is None else _toml_notify_line(command)
    if span is not None:
        return text[: span[0]] + replacement + text[span[1] :]
    if command is None:
        return text
    insert_at = _first_table_offset(text)
    prefix, suffix = text[:insert_at], text[insert_at:]
    if prefix and not prefix.endswith(("\n", "\r")):
        prefix += "\n"
    if prefix and prefix.strip() and not prefix.endswith("\n\n"):
        replacement += "\n"
    return prefix + replacement + suffix


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _write_state(path: Path, state: dict[str, Any]) -> None:
    payload = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _atomic_write_bytes(path, payload.encode("utf-8"))


def get_install_state(
    project_root: str | os.PathLike[str] | None = None,
    *,
    state_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any] | None:
    path = Path(state_path) if state_path else default_state_path(project_root)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError(f"Install state is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise InstallError(f"Install state is invalid: {path}")
    return value


def install(
    project_root: str | os.PathLike[str] | None = None,
    codex_home: str | os.PathLike[str] | None = None,
    python_executable: str | os.PathLike[str] | None = None,
    *,
    state_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Install the wrapper, preserving any existing notify command verbatim."""

    root = Path(project_root).resolve() if project_root else PROJECT_ROOT
    state_file = Path(state_path) if state_path else default_state_path(root)
    config_path = Path(codex_home).expanduser() / "config.toml" if codex_home else default_codex_home() / "config.toml"
    desired = installed_notify_command(root, python_executable)
    text, parsed = _load_toml(config_path)
    had_original, current = _notify_value(parsed)
    state = get_install_state(root, state_path=state_file)

    if state is not None:
        installed = state.get("installed_notify")
        if current == desired and installed == desired:
            return {"changed": False, "status": "already-installed", **state}
        raise InstallError(
            "An install state already exists but Codex notify was changed externally; "
            "refusing to overwrite it"
        )

    if current == desired:
        # Recover a missing state conservatively. There is no original command to
        # infer, so uninstall may only remove this exact wrapper.
        recovered = {
            "version": 1,
            "config_path": str(config_path.resolve()),
            "installed_notify": desired,
            "had_original_notify": False,
            "original_notify": None,
            "backup_path": None,
            "installed_at": datetime.now(timezone.utc).isoformat(),
            "recovered_state": True,
        }
        _write_state(state_file, recovered)
        return {"changed": False, "status": "already-installed", **recovered}

    config_path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_path: Path | None = None
    if config_path.exists():
        backup_path = config_path.with_name(
            f"{config_path.name}.progress-notify.{stamp}.bak"
        )
        shutil.copy2(config_path, backup_path)

    new_text = _replace_notify(text, desired)
    # Validate the full document before making it visible to Codex.
    try:
        validated = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as exc:
        raise InstallError(f"Generated Codex config is invalid: {exc}") from exc
    if validated.get("notify") != desired:
        raise InstallError("Generated Codex config did not contain the expected notify command")

    new_state = {
        "version": 1,
        "config_path": str(config_path.resolve()),
        "installed_notify": desired,
        "had_original_notify": had_original,
        "original_notify": current,
        "backup_path": str(backup_path.resolve()) if backup_path else None,
        "installed_at": datetime.now(timezone.utc).isoformat(),
    }
    # Persist recovery information first. A crash between these two atomic
    # replaces is safe: a subsequent install sees unchanged Codex config and can
    # be retried after removing the state, while the original value is retained.
    _write_state(state_file, new_state)
    try:
        _atomic_write_bytes(config_path, new_text.encode("utf-8"))
    except BaseException:
        try:
            state_file.unlink()
        except OSError:
            pass
        raise
    return {"changed": True, "status": "installed", **new_state}


def uninstall(
    project_root: str | os.PathLike[str] | None = None,
    codex_home: str | os.PathLike[str] | None = None,
    *,
    state_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Restore the prior notify only if Codex still points at this wrapper."""

    root = Path(project_root).resolve() if project_root else PROJECT_ROOT
    state_file = Path(state_path) if state_path else default_state_path(root)
    state = get_install_state(root, state_path=state_file)
    if state is None:
        return {"changed": False, "status": "not-installed"}

    recorded_config = state.get("config_path")
    config_path = (
        Path(codex_home).expanduser() / "config.toml"
        if codex_home
        else Path(recorded_config)
        if isinstance(recorded_config, str) and recorded_config
        else default_codex_home() / "config.toml"
    )
    text, parsed = _load_toml(config_path)
    _had_current, current = _notify_value(parsed)
    installed = state.get("installed_notify")
    if not isinstance(installed, list) or not all(isinstance(part, str) for part in installed):
        raise InstallError("Install state has no valid installed_notify value")
    if current != installed:
        return {
            "changed": False,
            "status": "externally-modified",
            "message": "Codex notify no longer points at this tool; configuration was not changed",
        }

    original = state.get("original_notify") if state.get("had_original_notify") else None
    if original is not None and (
        not isinstance(original, list) or not all(isinstance(part, str) for part in original)
    ):
        raise InstallError("Install state has an invalid original_notify value")
    new_text = _replace_notify(text, original)
    try:
        validated = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as exc:
        raise InstallError(f"Restored Codex config would be invalid: {exc}") from exc
    if original is None:
        if "notify" in validated:
            raise InstallError("Failed to remove installed notify command")
    elif validated.get("notify") != original:
        raise InstallError("Failed to restore original notify command")

    _atomic_write_bytes(config_path, new_text.encode("utf-8"))
    try:
        state_file.unlink()
    except FileNotFoundError:
        pass
    return {
        "changed": True,
        "status": "uninstalled",
        "restored_notify": original,
        "config_path": str(config_path.resolve()),
    }
