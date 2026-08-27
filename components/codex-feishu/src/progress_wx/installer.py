"""原子安装 Codex ``notify`` 包装器，并完整保留原配置。"""

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

from .config import DEFAULT_CONFIG_PATH
from .locking import InterprocessMutex, LockUnavailable


PROJECT_ROOT = Path(__file__).resolve().parents[2]
INSTALL_STATE_PATH = PROJECT_ROOT / ".state" / "install-state.json"


class InstallError(RuntimeError):
    """检测到可能覆盖用户配置时拒绝安装。"""


def _strict_command(value: Any) -> list[str] | None:
    """只接受非空字符串 argv，拒绝把损坏状态传给启动器或配置恢复。"""

    if not isinstance(value, list) or not value:
        return None
    if not all(isinstance(item, str) and item for item in value):
        return None
    return list(value)


def _previous_notify_wrapper(command: list[str]) -> tuple[list[str], list[str]] | None:
    """严格解析末尾的 ``--previous-notify <JSON argv>`` 包装参数。

    该格式由外部 Codex 集成使用。标记必须只出现为倒数第二个参数，且
    JSON 必须解码为非空字符串 argv；任何多余参数或嵌套类型都视为不安全。
    """

    try:
        marker = command.index("--previous-notify")
    except ValueError:
        return None
    if marker < 1 or marker + 2 != len(command):
        return None
    try:
        previous = _strict_command(json.loads(command[marker + 1]))
    except (json.JSONDecodeError, TypeError):
        return None
    if previous is None:
        return None
    return command[:marker], previous


def _normalized_command(command: list[str]) -> tuple[str, ...]:
    """按 Windows 路径规则规范化 argv，用于严格比较已知命令链。"""

    return tuple(os.path.normcase(os.path.normpath(item)) for item in command)


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()


def desired_notify(
    python_executable: str | os.PathLike[str] | None = None,
    progress_config_path: str | os.PathLike[str] | None = None,
) -> list[str]:
    python = Path(python_executable or sys.executable).resolve()
    entrypoint = (PROJECT_ROOT / "progress-wx-hook.py").resolve()
    progress_config = Path(progress_config_path or DEFAULT_CONFIG_PATH).expanduser().resolve()
    if not python.is_file() or not entrypoint.is_file() or not progress_config.is_file():
        raise InstallError("Python、progress-wx-hook.py 或进度通知配置不存在")
    return [str(python), str(entrypoint), "--config", str(progress_config)]


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _assignment_key(line: str) -> tuple[str | None, int]:
    """扫描顶层简单赋值，忽略字符串内的等号。"""

    quote = ""
    escaped = False
    for index, character in enumerate(line):
        if escaped:
            escaped = False
        elif quote and character == "\\" and quote == '"':
            escaped = True
        elif quote:
            if character == quote:
                quote = ""
        elif character in {'"', "'"}:
            quote = character
        elif character == "#":
            break
        elif character == "=":
            key = line[:index].strip().strip('"\'')
            return key or None, index + 1
    return None, -1


def _array_end(text: str, start: int) -> int:
    depth = 0
    began = False
    quote = ""
    escaped = False
    comment = False
    for index in range(start, len(text)):
        character = text[index]
        if comment:
            if character in "\r\n":
                comment = False
            continue
        if escaped:
            escaped = False
        elif quote and character == "\\" and quote == '"':
            escaped = True
        elif quote:
            if character == quote:
                quote = ""
        elif character in {'"', "'"}:
            quote = character
        elif character == "#":
            comment = True
        elif character == "[":
            depth += 1
            began = True
        elif character == "]" and began:
            depth -= 1
            if depth == 0:
                newline = text.find("\n", index)
                return len(text) if newline < 0 else newline + 1
    raise InstallError("无法定位顶层 notify 数组的末尾")


def _notify_span(text: str) -> tuple[int, int] | None:
    offset = 0
    in_table = False
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith("["):
            in_table = True
        if not in_table:
            key, value_start = _assignment_key(line)
            if key == "notify":
                return offset, _array_end(text, offset + value_start)
        offset += len(line)
    return None


def _first_table(text: str) -> int:
    offset = 0
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("["):
            return offset
        offset += len(line)
    return len(text)


def _notify_line(command: list[str]) -> str:
    return "notify = [ " + ", ".join(json.dumps(item, ensure_ascii=False) for item in command) + " ]\n"


def _replace_notify(text: str, command: list[str] | None) -> str:
    span = _notify_span(text)
    replacement = "" if command is None else _notify_line(command)
    if span:
        return text[: span[0]] + replacement + text[span[1] :]
    if command is None:
        return text
    index = _first_table(text)
    prefix, suffix = text[:index], text[index:]
    if prefix and not prefix.endswith(("\n", "\r")):
        prefix += "\n"
    if prefix.strip() and not prefix.endswith("\n\n"):
        replacement += "\n"
    return prefix + replacement + suffix


def _read_config(path: Path) -> tuple[str, dict[str, Any]]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return "", {}
    try:
        text = raw.decode("utf-8-sig")
        value = tomllib.loads(text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise InstallError(f"Codex config.toml 无法安全解析：{exc}") from exc
    return text, value


def _current_notify(parsed: dict[str, Any]) -> tuple[bool, list[str] | None]:
    if "notify" not in parsed:
        return False, None
    value = _strict_command(parsed["notify"])
    if value is None:
        raise InstallError("顶层 notify 必须是非空字符串数组")
    return True, value


def _load_state() -> dict[str, Any] | None:
    try:
        value = json.loads(INSTALL_STATE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError("安装状态损坏，拒绝覆盖 Codex 配置") from exc
    if not isinstance(value, dict):
        raise InstallError("安装状态格式错误")
    required = {
        "config_path": str,
        "installed_notify": list,
        "had_original_notify": bool,
    }
    if any(not isinstance(value.get(key), kind) for key, kind in required.items()):
        raise InstallError("安装状态缺少必需字段")
    if not value["config_path"] or not value["installed_notify"] or not all(
        isinstance(item, str) and item for item in value["installed_notify"]
    ):
        raise InstallError("安装状态的路径或 notify 无效")
    original = value.get("original_notify")
    if value["had_original_notify"]:
        if not isinstance(original, list) or not all(
            isinstance(item, str) and item for item in original
        ):
            raise InstallError("安装状态的原 notify 无效")
    elif original is not None:
        raise InstallError("安装状态的原 notify 标记不一致")
    phase = value.get("phase", "installed")
    if phase not in {"prepared", "installed"}:
        raise InstallError("安装状态 phase 无效")
    previous = value.get("previous_installed_notify")
    if previous is not None and (
        phase != "prepared"
        or not isinstance(previous, list)
        or not previous
        or not all(isinstance(item, str) and item for item in previous)
    ):
        raise InstallError("安装状态的迁移前 notify 无效")
    value["phase"] = phase
    return value


def _write_state(state: dict[str, Any]) -> None:
    _atomic_write(
        INSTALL_STATE_PATH,
        (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def _finish_state(state: dict[str, Any]) -> None:
    state["version"] = 3
    state["phase"] = "installed"
    state.pop("previous_installed_notify", None)
    _write_state(state)


def _is_legacy_command_upgrade(previous: list[str], desired: list[str]) -> bool:
    """只允许把本工具旧的两参数命令升级为带配置路径的新命令。"""

    return (
        len(previous) == 2
        and len(desired) == 4
        and previous == desired[:2]
        and desired[2] == "--config"
    )


def _restore_wrapped_notify(
    current: list[str], original: list[str] | None, installed: list[str]
) -> list[str] | None:
    """在可证明安全时恢复外部 wrapper 的旧 child。

    安装后某个集成可能把本工具包成 ``prefix --previous-notify JSON``。只有
    当前 wrapper 的 child 精确指向本工具、保存的原命令也是同一 prefix 的
    严格末尾 JSON wrapper，才允许保留当前外层并替换 child；其余情况全部拒绝
    猜测，返回 ``None`` 让卸载报告外部修改或直接抛出歧义错误。
    """

    current_wrapper = _previous_notify_wrapper(current)
    if current_wrapper is None:
        # 当前命令不是可验证的 wrapper，不能据此恢复任何配置。
        return None
    current_prefix, current_child = current_wrapper
    if _normalized_command(current_child) != _normalized_command(installed):
        # 这是其它程序的修改，不是本工具留下的可识别包装。
        return None
    original_wrapper = _previous_notify_wrapper(original) if original is not None else None
    if original_wrapper is None:
        raise InstallError("当前 notify 包含本工具 wrapper，但原 notify 不是可验证的 previous-notify wrapper")
    original_prefix, original_child = original_wrapper
    if _normalized_command(current_prefix) != _normalized_command(original_prefix):
        raise InstallError("当前 notify 的外层 wrapper 已变化；拒绝猜测卸载目标")
    if _normalized_command(original_child) == _normalized_command(installed):
        raise InstallError("原 notify 的 child 仍指向本工具；拒绝形成递归 wrapper")

    restored = [
        *current_prefix,
        "--previous-notify",
        # 保留安装记录中的 JSON 文本，避免无意义地改写外部 wrapper 的格式。
        original[-1],
    ]
    checked = _previous_notify_wrapper(restored)
    if checked is None or _normalized_command(checked[0]) != _normalized_command(current_prefix):
        raise InstallError("恢复 wrapper 的 notify 参数无法通过严格校验")
    if _normalized_command(checked[1]) != _normalized_command(original_child):
        raise InstallError("恢复 wrapper 的 child 与安装记录不一致")
    return restored


def _install_notify_unlocked(
    *,
    python_executable: str | os.PathLike[str] | None = None,
    codex_home_path: str | os.PathLike[str] | None = None,
    progress_config_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """保存备份后安装；已有安装状态与当前配置不一致时立即停止。"""

    config_path = (Path(codex_home_path).expanduser().resolve() if codex_home_path else codex_home()) / "config.toml"
    desired = desired_notify(python_executable, progress_config_path)
    text, parsed = _read_config(config_path)
    had_original, original = _current_notify(parsed)
    state = _load_state()
    if state:
        state_config = Path(state["config_path"]).resolve()
        if state_config != config_path:
            raise InstallError("安装状态属于不同 Codex home；拒绝覆盖")
        if state["installed_notify"] != desired:
            previous = state["installed_notify"]
            if (
                state["phase"] != "installed"
                or parsed.get("notify") != previous
                or not _is_legacy_command_upgrade(previous, desired)
            ):
                raise InstallError("安装状态属于不同 Python 或进度通知配置；拒绝覆盖")
            # 先记录可恢复迁移意图，再替换 Codex 配置，最后提交状态。
            state["version"] = 3
            state["phase"] = "prepared"
            state["previous_installed_notify"] = previous
            state["installed_notify"] = desired
            state["upgraded_at"] = datetime.now(timezone.utc).isoformat()
            _write_state(state)
            upgraded_text = _replace_notify(text, desired)
            if tomllib.loads(upgraded_text).get("notify") != desired:
                raise InstallError("升级生成的 notify 无效")
            _atomic_write(config_path, upgraded_text.encode("utf-8"))
            _finish_state(state)
            return {"changed": True, "status": "upgraded-notify-command", **state}
        if parsed.get("notify") == desired:
            if state["phase"] == "prepared":
                upgraded = "previous_installed_notify" in state
                _finish_state(state)
                status = "recovered-notify-upgrade" if upgraded else "recovered-installed"
                return {"changed": False, "status": status, **state}
            if state.get("version") != 3:
                _finish_state(state)
                return {"changed": False, "status": "upgraded-install-state", **state}
            return {"changed": False, "status": "already-installed", **state}
        if state["phase"] == "prepared":
            had_current, current = _current_notify(parsed)
            previous = state.get("previous_installed_notify")
            expected_matches = (
                had_current and current == previous
                if previous is not None
                else had_current == state["had_original_notify"]
                and current == state.get("original_notify")
            )
            if not expected_matches:
                raise InstallError("安装准备阶段的 config.toml 已被外部修改；拒绝覆盖")
            resumed_text = _replace_notify(text, desired)
            if tomllib.loads(resumed_text).get("notify") != desired:
                raise InstallError("恢复安装生成的 notify 无效")
            _atomic_write(config_path, resumed_text.encode("utf-8"))
            upgraded = previous is not None
            _finish_state(state)
            status = "recovered-notify-upgrade" if upgraded else "recovered-install"
            return {"changed": True, "status": status, **state}
        raise InstallError("工具已安装但 config.toml 被外部修改；请先人工核对")
    if original == desired:
        raise InstallError("config.toml 已指向本工具但缺少恢复状态；拒绝猜测原配置")

    backup: Path | None = None
    if config_path.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = config_path.with_name(f"{config_path.name}.progress-wx.{stamp}.bak")
        shutil.copy2(config_path, backup)
    new_text = _replace_notify(text, desired)
    try:
        validated = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as exc:
        raise InstallError(f"生成后的 TOML 无效：{exc}") from exc
    if validated.get("notify") != desired:
        raise InstallError("生成后的 notify 与预期不一致")
    state = {
        "version": 3,
        "phase": "prepared",
        "config_path": str(config_path),
        "installed_notify": desired,
        "had_original_notify": had_original,
        "original_notify": original,
        "backup_path": str(backup) if backup else None,
        "installed_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_state(state)
    _atomic_write(config_path, new_text.encode("utf-8"))
    _finish_state(state)
    return {"changed": True, "status": "installed", **state}


def _uninstall_notify_unlocked(*, codex_home_path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """仅当当前 notify 仍等于本工具时恢复原值。"""

    state = _load_state()
    if not state:
        return {"changed": False, "status": "not-installed"}
    state_config_path = Path(state["config_path"]).resolve()
    config_path = Path(codex_home_path).expanduser().resolve() / "config.toml" if codex_home_path else state_config_path
    if config_path != state_config_path:
        raise InstallError("卸载参数的 Codex home 与安装状态不一致")
    text, parsed = _read_config(config_path)
    installed = state.get("installed_notify")
    if state["phase"] == "prepared" and parsed.get("notify") != installed:
        had_current, current = _current_notify(parsed)
        previous = state.get("previous_installed_notify")
        if previous is not None and had_current and current == previous:
            original = state.get("original_notify") if state.get("had_original_notify") else None
            new_text = _replace_notify(text, original)
            validated = tomllib.loads(new_text)
            if original is None and "notify" in validated:
                raise InstallError("取消迁移后 notify 未被移除")
            if original is not None and validated.get("notify") != original:
                raise InstallError("取消迁移后未恢复原 notify")
            _atomic_write(config_path, new_text.encode("utf-8"))
            INSTALL_STATE_PATH.unlink(missing_ok=True)
            return {
                "changed": True,
                "status": "uninstalled-prepared-upgrade",
                "restored_notify": original,
            }
        if had_current == state["had_original_notify"] and current == state.get("original_notify"):
            INSTALL_STATE_PATH.unlink(missing_ok=True)
            return {"changed": False, "status": "aborted-prepared-install"}
    if parsed.get("notify") != installed:
        if state["phase"] == "installed":
            had_current, current = _current_notify(parsed)
            if had_current and current is not None:
                original = state.get("original_notify") if state.get("had_original_notify") else None
                restored_wrapper = _restore_wrapped_notify(current, original, installed)
                if restored_wrapper is not None:
                    new_text = _replace_notify(text, restored_wrapper)
                    validated = tomllib.loads(new_text)
                    if validated.get("notify") != restored_wrapper:
                        raise InstallError("卸载后 wrapper notify 未通过严格校验")
                    _atomic_write(config_path, new_text.encode("utf-8"))
                    INSTALL_STATE_PATH.unlink(missing_ok=True)
                    return {
                        "changed": True,
                        "status": "uninstalled-restored-wrapper",
                        "restored_notify": restored_wrapper,
                    }
        return {"changed": False, "status": "externally-modified", "config_path": str(config_path)}
    original = state.get("original_notify") if state.get("had_original_notify") else None
    new_text = _replace_notify(text, original)
    validated = tomllib.loads(new_text)
    if original is None and "notify" in validated:
        raise InstallError("卸载后 notify 未被移除")
    if original is not None and validated.get("notify") != original:
        raise InstallError("卸载后未恢复原 notify")
    _atomic_write(config_path, new_text.encode("utf-8"))
    INSTALL_STATE_PATH.unlink(missing_ok=True)
    return {"changed": True, "status": "uninstalled", "restored_notify": original}


def _installer_mutex(codex_home_path: str | os.PathLike[str] | None) -> InterprocessMutex:
    home = Path(codex_home_path).expanduser().resolve() if codex_home_path else codex_home()
    return InterprocessMutex(f"installer:{os.path.normcase(str(home / 'config.toml'))}")


def install_notify(
    *,
    python_executable: str | os.PathLike[str] | None = None,
    codex_home_path: str | os.PathLike[str] | None = None,
    progress_config_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """跨进程串行执行安装，防止状态文件与 Codex 配置交错写入。"""

    try:
        with _installer_mutex(codex_home_path):
            return _install_notify_unlocked(
                python_executable=python_executable,
                codex_home_path=codex_home_path,
                progress_config_path=progress_config_path,
            )
    except LockUnavailable as exc:
        raise InstallError("另一个安装或卸载操作正在进行") from exc


def uninstall_notify(*, codex_home_path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """跨进程串行恢复安装前的顶层 notify。"""

    try:
        with _installer_mutex(codex_home_path):
            return _uninstall_notify_unlocked(codex_home_path=codex_home_path)
    except LockUnavailable as exc:
        raise InstallError("另一个安装或卸载操作正在进行") from exc
