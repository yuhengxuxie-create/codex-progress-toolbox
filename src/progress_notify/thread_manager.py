"""Safe, testable core for the local monitored-thread manager.

The GUI must never serialize :class:`~progress_notify.config.AppConfig` back to
disk: doing that would expand secret environment placeholders.  This module
therefore keeps the original JSON document and changes only ``thread_ids``.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .config import ConfigError, expand_placeholders, load_config


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.local.json"
USER_FACING_THREAD_SOURCE_KINDS = ("cli", "vscode", "appServer", "unknown")


class ThreadManagerError(RuntimeError):
    """Base error for safe thread-manager operations."""


class ThreadSelectionError(ThreadManagerError):
    """Raised when a requested monitored-thread selection is invalid."""


class ConfigConflictError(ThreadManagerError):
    """Raised instead of overwriting a configuration changed elsewhere."""


@dataclass(frozen=True, slots=True)
class ThreadManagerState:
    """Validated manager state without notification credentials."""

    path: Path
    digest: str
    thread_ids: tuple[str, ...]
    codex_command: str
    codex_timeout_seconds: float
    title_overrides: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class ThreadRecord:
    """One Codex thread prepared for display by the GUI."""

    thread_id: str
    title: str
    preview: str = ""
    updated_at: float | None = None
    archived: bool = False
    available: bool = True
    conversation_type: str = "unknown"
    project_key: str = ""
    project_name: str = ""
    project_path: str = ""
    source_kind: str = ""


@dataclass(frozen=True, slots=True)
class ConversationLocation:
    """Classification derived from a thread's persisted working directory."""

    conversation_type: str
    project_key: str = ""
    project_name: str = ""
    project_path: str = ""


@dataclass(frozen=True, slots=True)
class CodexLocalProject:
    """Minimal non-secret subset of one Codex desktop local project."""

    project_id: str
    name: str
    root_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CodexProjectIndex:
    """Minimal thread-to-project mapping read from Codex desktop state."""

    projects: Mapping[str, CodexLocalProject]
    assignments: Mapping[str, str]
    projectless_thread_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class SaveResult:
    """Result of an atomic thread-ID update."""

    path: Path
    backup_path: Path
    digest: str
    thread_ids: tuple[str, ...]


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read_document(path: Path) -> tuple[bytes, dict[str, Any]]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ThreadManagerError(f"无法读取配置文件：{path}") from exc
    try:
        document = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ThreadManagerError("配置文件不是有效的 UTF-8 JSON。") from exc
    if not isinstance(document, dict):
        raise ThreadManagerError("配置文件根节点必须是 JSON 对象。")
    return raw, document


def normalize_thread_ids(value: Any) -> tuple[str, ...]:
    """Normalize supported thread-ID forms while preserving first-seen order."""

    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                value = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ThreadSelectionError("会话 ID 列表中的 JSON 无效。") from exc
        else:
            value = text.split(",") if text else []
    if isinstance(value, (set, frozenset)):
        value = sorted(value)
    if not isinstance(value, (list, tuple)):
        raise ThreadSelectionError("会话 ID 必须是列表或逗号分隔文本。")

    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ThreadSelectionError("会话 ID 不能为空，且必须是文本。")
        thread_id = item.strip()
        if thread_id not in seen:
            seen.add(thread_id)
            result.append(thread_id)
    if not result:
        raise ThreadSelectionError("至少需要保留一个监控会话。")
    return tuple(result)


def load_thread_manager_state(
    path: str | os.PathLike[str] = DEFAULT_CONFIG_PATH,
    *,
    environ: Mapping[str, str] | None = None,
) -> ThreadManagerState:
    """Load the effective thread selection and safe Codex connection settings."""

    config_path = Path(path).expanduser().resolve()
    raw, document = _read_document(config_path)
    try:
        config = load_config(config_path, environ=environ)
        expanded_ids = expand_placeholders(document.get("thread_ids"), environ)
        ordered_ids = normalize_thread_ids(expanded_ids)
    except (ConfigError, ThreadSelectionError) as exc:
        raise ThreadManagerError(f"配置校验失败：{exc}") from exc
    if frozenset(ordered_ids) != config.thread_ids:
        raise ThreadManagerError("会话 ID 的读取结果与配置校验结果不一致。")
    return ThreadManagerState(
        path=config_path,
        digest=_digest(raw),
        thread_ids=ordered_ids,
        codex_command=config.codex.command,
        codex_timeout_seconds=config.codex.request_timeout_seconds,
        title_overrides=MappingProxyType(dict(config.codex.title_overrides)),
    )


def _display_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())


_DATE_DIRECTORY = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


def _empty_project_index() -> CodexProjectIndex:
    return CodexProjectIndex(
        projects=MappingProxyType({}),
        assignments=MappingProxyType({}),
        projectless_thread_ids=frozenset(),
    )


def load_codex_project_index(
    codex_home: str | os.PathLike[str] | None = None,
) -> CodexProjectIndex:
    """Best-effort read of Codex desktop's explicit project assignment index.

    Only the three project-related keys are retained.  Other global UI state is
    intentionally ignored and never exposed to the GUI or logs.
    """

    root = (
        Path(codex_home).expanduser()
        if codex_home is not None
        else Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser()
    )
    path = root / ".codex-global-state.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return _empty_project_index()
    if not isinstance(document, Mapping):
        return _empty_project_index()

    projects: dict[str, CodexLocalProject] = {}
    raw_projects = document.get("local-projects")
    if isinstance(raw_projects, Mapping):
        for raw_key, raw_value in raw_projects.items():
            if not isinstance(raw_value, Mapping):
                continue
            raw_id = raw_value.get("id")
            project_id = (
                raw_id.strip()
                if isinstance(raw_id, str) and raw_id.strip()
                else str(raw_key).strip()
            )
            if not project_id:
                continue
            raw_roots = raw_value.get("rootPaths")
            roots = tuple(
                item.strip()
                for item in raw_roots
                if isinstance(item, str) and item.strip()
            ) if isinstance(raw_roots, list) else ()
            name = _display_text(raw_value.get("name"))
            if not name and roots:
                name = Path(roots[0]).name
            projects[project_id] = CodexLocalProject(
                project_id=project_id,
                name=name or "未命名项目",
                root_paths=roots,
            )

    assignments: dict[str, str] = {}
    raw_assignments = document.get("thread-project-assignments")
    if isinstance(raw_assignments, Mapping):
        for raw_thread_id, raw_assignment in raw_assignments.items():
            if not isinstance(raw_thread_id, str) or not raw_thread_id.strip():
                continue
            if not isinstance(raw_assignment, Mapping):
                continue
            raw_project_id = raw_assignment.get("projectId")
            if isinstance(raw_project_id, str) and raw_project_id.strip():
                assignments[raw_thread_id.strip()] = raw_project_id.strip()

    raw_projectless = document.get("projectless-thread-ids")
    projectless = frozenset(
        item.strip()
        for item in raw_projectless
        if isinstance(item, str) and item.strip()
    ) if isinstance(raw_projectless, list) else frozenset()
    return CodexProjectIndex(
        projects=MappingProxyType(projects),
        assignments=MappingProxyType(assignments),
        projectless_thread_ids=projectless,
    )


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath(
            (os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(root)))
        ) == os.path.normcase(os.path.abspath(root))
    except (OSError, ValueError):
        return False


def classify_conversation_location(
    cwd: object,
    *,
    user_home: str | os.PathLike[str] | None = None,
) -> ConversationLocation:
    """Fallback classification when Codex has no explicit project assignment.

    Codex desktop creates a fresh workspace beneath
    ``Documents/Codex/YYYY-MM-DD/<generated-name>`` for a chat started without
    a project.  Other working directories are shown as unclassified instead of
    being guessed to be projects.
    """

    if not isinstance(cwd, str) or not cwd.strip():
        return ConversationLocation("unknown")
    raw_path = cwd.strip()
    path = Path(raw_path).expanduser()
    parent = path.parent
    codex_root = parent.parent
    home = Path(user_home).expanduser() if user_home is not None else Path.home()
    is_generated_one_off = (
        bool(path.name)
        and _DATE_DIRECTORY.fullmatch(parent.name) is not None
        and codex_root.name.casefold() == "codex"
        and _path_is_within(path, home)
    )
    if is_generated_one_off:
        return ConversationLocation("one_time", project_path=str(path))
    workspace_name = path.name or path.anchor or raw_path
    return ConversationLocation(
        "unclassified",
        project_key=os.path.normcase(os.path.normpath(str(path))),
        project_name=workspace_name,
        project_path=str(path),
    )


def classify_thread_location(
    thread_id: str,
    cwd: object,
    *,
    ephemeral: object = False,
    project_index: CodexProjectIndex | None = None,
    user_home: str | os.PathLike[str] | None = None,
) -> ConversationLocation:
    """Use Codex's explicit UI mapping before conservative payload fallbacks."""

    index = project_index or _empty_project_index()
    project_id = index.assignments.get(thread_id)
    if project_id:
        project = index.projects.get(project_id)
        if project is not None:
            project_path = project.root_paths[0] if project.root_paths else ""
            return ConversationLocation(
                "project",
                project_key=f"project:{project_id}",
                project_name=project.name,
                project_path=project_path,
            )
        return ConversationLocation(
            "project",
            project_key=f"project:{project_id}",
            project_name="未知项目",
            project_path=cwd.strip() if isinstance(cwd, str) else "",
        )
    if thread_id in index.projectless_thread_ids or ephemeral is True:
        return ConversationLocation(
            "one_time",
            project_path=cwd.strip() if isinstance(cwd, str) else "",
        )
    return classify_conversation_location(cwd, user_home=user_home)


def _source_kind(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        if "subAgent" in value:
            return "subAgent"
        first = next(iter(value), "")
        return str(first)
    return ""


def _record_from_payload(
    payload: Mapping[str, Any],
    *,
    archived: bool,
    user_home: str | os.PathLike[str] | None = None,
    project_index: CodexProjectIndex | None = None,
) -> ThreadRecord | None:
    source_kind = _source_kind(payload.get("source"))
    if payload.get("parentThreadId") or source_kind.startswith("subAgent"):
        return None
    thread_id = payload.get("id")
    if not isinstance(thread_id, str) or not thread_id.strip():
        return None
    thread_id = thread_id.strip()
    name = _display_text(payload.get("name"))
    preview = _display_text(payload.get("preview"))
    title = name or preview or "未命名会话"
    raw_updated = payload.get("updatedAt")
    try:
        updated_at = (
            float(raw_updated)
            if isinstance(raw_updated, (int, float)) and not isinstance(raw_updated, bool)
            else None
        )
    except (OverflowError, TypeError, ValueError):
        updated_at = None
    if updated_at is not None and not math.isfinite(updated_at):
        updated_at = None
    location = classify_thread_location(
        thread_id,
        payload.get("cwd"),
        ephemeral=payload.get("ephemeral"),
        project_index=project_index,
        user_home=user_home,
    )
    return ThreadRecord(
        thread_id=thread_id,
        title=title,
        preview=preview,
        updated_at=updated_at,
        archived=archived,
        conversation_type=location.conversation_type,
        project_key=location.project_key,
        project_name=location.project_name,
        project_path=location.project_path,
        source_kind=source_kind,
    )


def build_thread_catalog(
    configured_ids: Iterable[str],
    active_threads: Iterable[Mapping[str, Any]],
    archived_threads: Iterable[Mapping[str, Any]] = (),
    *,
    title_overrides: Mapping[str, str] | None = None,
    user_home: str | os.PathLike[str] | None = None,
    project_index: CodexProjectIndex | None = None,
) -> tuple[ThreadRecord, ...]:
    """Merge App Server results with configured IDs that are not currently listed."""

    records: list[ThreadRecord] = []
    seen: set[str] = set()
    for archived, payloads in ((False, active_threads), (True, archived_threads)):
        for payload in payloads:
            if not isinstance(payload, Mapping):
                continue
            record = _record_from_payload(
                payload,
                archived=archived,
                user_home=user_home,
                project_index=project_index,
            )
            if record is None or record.thread_id in seen:
                continue
            seen.add(record.thread_id)
            records.append(record)

    overrides = title_overrides or {}
    for thread_id in configured_ids:
        if thread_id in seen:
            continue
        title = _display_text(overrides.get(thread_id)) or "未命名会话"
        records.append(
            ThreadRecord(
                thread_id=thread_id,
                title=title,
                available=False,
            )
        )
        seen.add(thread_id)
    return tuple(records)


def _write_exclusive(path: Path, data: bytes) -> None:
    try:
        with path.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise ThreadManagerError(f"无法写入文件：{path}") from exc


def save_thread_ids(
    path: str | os.PathLike[str],
    thread_ids: Iterable[str] | str,
    *,
    expected_digest: str,
    environ: Mapping[str, str] | None = None,
) -> SaveResult:
    """Atomically replace only ``thread_ids`` after conflict and full validation."""

    config_path = Path(path).expanduser().resolve()
    normalized = normalize_thread_ids(thread_ids)
    original_raw, document = _read_document(config_path)
    if _digest(original_raw) != expected_digest:
        raise ConfigConflictError("配置文件已被其他程序修改，请重新读取后再保存。")

    document["thread_ids"] = list(normalized)
    rendered = (
        json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    unique = uuid.uuid4().hex
    temporary_path = config_path.with_name(f".{config_path.name}.{unique}.tmp")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_path = config_path.with_name(
        f"{config_path.name}.before-thread-manager-{stamp}.bak"
    )

    try:
        _write_exclusive(temporary_path, rendered)
        try:
            load_config(temporary_path, environ=environ)
        except ConfigError as exc:
            raise ThreadManagerError(f"新配置校验失败，未写入原文件：{exc}") from exc

        latest_raw, _ = _read_document(config_path)
        if _digest(latest_raw) != expected_digest:
            raise ConfigConflictError("配置文件已被其他程序修改，请重新读取后再保存。")
        _write_exclusive(backup_path, original_raw)
        try:
            os.replace(temporary_path, config_path)
        except OSError as exc:
            raise ThreadManagerError("无法原子替换配置文件，原文件未被覆盖。") from exc
    finally:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass

    return SaveResult(
        path=config_path,
        backup_path=backup_path,
        digest=_digest(rendered),
        thread_ids=normalized,
    )
