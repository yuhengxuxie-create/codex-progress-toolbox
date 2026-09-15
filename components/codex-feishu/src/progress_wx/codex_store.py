"""Codex 本地结构化状态的只读访问层。

本模块优先读取 Codex 自己维护的两个 SQLite 数据库，不打开写事务，也不解析
助手文本来猜测状态。旧任务缺少历史投影时，只在 CODEX_HOME/sessions 边界内
增量读取 rollout 的显式 task_complete/turn_aborted 事件。线程选择只使用 thread
id、标题和工作目录的精确相等。由于 Codex 的内部 schema 可能随版本演进，查询
前会检查表和列；数据库不存在、损坏或暂时不可读时保留类型化错误，由上层进入
有限重试和停机，不会静默伪装成正常 ``unknown``。
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
import math
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
import sqlite3
import threading
from typing import Any, Iterable, Mapping

from .models import GeneratedImageArtifact, NotificationContext
from .delivered_files import discover_delivered_files
from .file_validation import read_verified_file


_GENERATED_IMAGE_MAX_BYTES = 30 * 1024 * 1024
_GENERATED_IMAGE_FORMATS = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
_NOTIFICATION_REQUEST_MAX_CHARS = 1_200
_NOTIFICATION_ROLLOUT_MAX_BYTES = 2 * 1024 * 1024
_NOTIFICATION_ROLLOUT_MAX_LINES = 8_000


def _is_reparse_point(path: Path) -> bool:
    """Return whether *path* is a symlink/junction-like filesystem object."""

    try:
        stat = path.stat(follow_symlinks=False)
    except (OSError, TypeError):
        return True
    attributes = int(getattr(stat, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return path.is_symlink() or bool(attributes & reparse_flag)


def read_generated_image_bytes(artifact: GeneratedImageArtifact) -> bytes:
    """发送前重读并核对摘要，关闭提取与上传之间的替换窗口。"""

    path = Path(artifact.path)
    try:
        # ``GeneratedImageArtifact`` 只会由同轮结构化 imageGeneration 产生，
        # 这里仍重新核对目录形状、重解析点、扩展名与文件头，避免持久 outbox
        # 在稍后/重启后发送时被本地路径替换。
        parts = tuple(part.casefold() for part in path.parts)
        generated_index = parts.index("generated_images")
        if generated_index + 2 != len(parts) - 1:
            raise ValueError("生成图片不在认可的单层任务目录")
        for candidate in (
            Path(*path.parts[: generated_index + 1]),
            Path(*path.parts[: generated_index + 2]),
            path,
        ):
            if _is_reparse_point(candidate):
                raise ValueError("生成图片路径包含重解析点")
        resolved = path.resolve(strict=True)
        expected_parent = Path(*path.parts[: generated_index + 2]).resolve(strict=True)
        if resolved.parent != expected_parent or not resolved.is_file():
            raise ValueError("生成图片路径边界已变化")
        expected_mime = _GENERATED_IMAGE_FORMATS.get(resolved.suffix.casefold())
        if expected_mime != artifact.mime_type:
            raise ValueError("生成图片扩展名与 MIME 不一致")
        data = read_verified_file(path, _GENERATED_IMAGE_MAX_BYTES, expected_sha256=artifact.sha256)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("生成图片原文件已不可读") from exc
    if not CodexStore._image_header_matches(artifact.mime_type, data[:16]):
        raise ValueError("生成图片文件头与 MIME 不一致")
    if len(data) != artifact.size:
        raise ValueError("生成图片原文件大小已变化")
    if hashlib.sha256(data).hexdigest() != artifact.sha256:
        raise ValueError("生成图片原文件摘要已变化")
    return data


class ThreadStatus(StrEnum):
    """Codex ``thread_turns.status`` 的受控状态集合。"""

    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    CANCELLED = "cancelled"
    IN_PROGRESS = "inProgress"
    UNKNOWN = "unknown"


class CodexStoreReadError(RuntimeError):
    """Codex 结构化状态读取失败；调用方必须停止或进入有限重试。"""

    def __init__(self, operation: str, errors: Iterable[str]):
        self.operation = str(operation)
        self.errors = tuple(str(item) for item in errors if str(item))
        detail = ", ".join(self.errors) or "unknown"
        super().__init__(f"{self.operation}失败：{detail}")


@dataclass(frozen=True, slots=True)
class StorePaths:
    """两个 Codex 状态库的路径。"""

    state_db: Path
    history_db: Path
    session_index: Path | None = None

    @classmethod
    def from_codex_home(cls, codex_home: str | os.PathLike[str] | None = None) -> "StorePaths":
        root = Path(codex_home).expanduser() if codex_home else Path.home() / ".codex"
        return cls(
            state_db=(root / "state_5.sqlite").resolve(),
            history_db=(root / "thread_history_1.sqlite").resolve(),
            session_index=(root / "session_index.jsonl").resolve(),
        )


@dataclass(frozen=True, slots=True)
class ThreadRecord:
    """从 ``state_5.sqlite.threads`` 读取的最小线程元数据。"""

    thread_id: str
    title: str = ""
    cwd: str = ""
    updated_at_ms: int | None = None
    created_at_ms: int | None = None
    archived: bool = False
    preview: str = ""
    source: str = ""
    thread_source: str = ""
    rollout_path: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)
    title_source: str = ""

    @property
    def id(self) -> str:
        """兼容调用方常用的 ``id`` 命名。"""

        return self.thread_id

    @property
    def name(self) -> str:
        """兼容 App Server 的 ``name`` 命名。"""

        return self.title


@dataclass(frozen=True, slots=True)
class TurnRecord:
    """从 ``thread_history_1.sqlite.thread_turns`` 读取的一轮状态。"""

    thread_id: str
    turn_id: str
    status: ThreadStatus
    rollout_ordinal: int | None = None
    started_at: int | None = None
    completed_at: int | None = None
    duration_ms: int | None = None
    error_json: str | None = None
    # Codex 在 thread_turns 中提供的最终 assistant item 指针。只有通过该
    # 精确指针读取到严格结构化的 final_answer 时，才会填充 final_message。
    final_agent_item_id: str = ""
    final_message: str = ""
    generated_images: tuple[GeneratedImageArtifact, ...] = ()
    delivered_files: tuple[Any, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)
    final_answer_parts: tuple[str, ...] = ()

    @property
    def in_progress(self) -> bool:
        return self.status is ThreadStatus.IN_PROGRESS


@dataclass(slots=True)
class _RolloutCursor:
    """单个 append-only rollout 的增量读取位置与最新结构化终态。"""

    identity: tuple[int, int]
    offset: int = 0
    line_number: int = 0
    mtime_ns: int = -1
    file_size: int = -1
    # SHA-256 of exactly the bytes already consumed.  ``mtime`` and size are
    # useful hints, but neither detects an in-place rewrite that keeps the same
    # inode and grows the file.
    prefix_digest: str = ""
    latest_turn: TurnRecord | None = None
    latest_completed_result: TurnRecord | None = None
    # A shared path with an unscoped terminal event cannot be attributed to one
    # thread safely.  Keep the fail-closed decision across unchanged polls.
    ambiguous_thread_event: bool = False


@dataclass(frozen=True, slots=True)
class ThreadSnapshot:
    """一个线程的元数据和最新一轮状态。"""

    thread: ThreadRecord | None
    latest_turn: TurnRecord | None
    status: ThreadStatus = ThreadStatus.UNKNOWN
    state_available: bool = False
    history_available: bool = False
    errors: tuple[str, ...] = ()

    @property
    def thread_id(self) -> str:
        return self.thread.thread_id if self.thread else ""

    @property
    def title(self) -> str:
        return self.thread.title if self.thread else ""

    @property
    def cwd(self) -> str:
        return self.thread.cwd if self.thread else ""

    @property
    def turn(self) -> TurnRecord | None:
        """兼容调用方使用 ``snapshot.turn`` 的简写。"""

        return self.latest_turn

    @property
    def available(self) -> bool:
        """至少有一个数据库可读；线程不存在仍可能是历史库延迟投影。"""

        return self.state_available or self.history_available

    @property
    def readable(self) -> bool:
        """底层查询没有错误；healthy 但不存在的 thread 仍然是可读快照。"""

        return not self.errors

    def require_readable(self) -> "ThreadSnapshot":
        """把底层读错误显式抛出，避免上层把它误当成正常 ``unknown``。"""

        if self.errors:
            raise CodexStoreReadError(
                f"读取 Codex thread {self.thread_id or '<unknown>'} 状态",
                self.errors,
            )
        return self


def _readonly_uri(path: Path) -> str:
    """构造 SQLite ``mode=ro`` URI，避免不存在时创建数据库。"""

    # as_uri 会正确转义 Windows 合法路径中的空格、# 和 %，避免被 SQLite
    # 当作 URI fragment 或百分号转义序列解释。
    return f"{path.resolve(strict=False).as_uri()}?mode=ro"


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _title_comparison_text(value: object) -> str:
    """归一化标题/preview，仅用于判断两者是否来自同一段提示词。

    Codex 的历史投影有时会给 SQLite ``title`` 套上一层命令前缀、引号或
    其它很短的包装，而 ``preview`` 保存包装内的完整首轮内容。这里不能只做
    严格相等，也不能只为 ``/goal`` 写特例；否则任何相似包装都会再次漏判。
    """

    text = " ".join(_as_text(value).split())
    if text.startswith("《") and text.endswith("》"):
        text = text[1:-1].strip()
    return text


def _title_is_preview_derived(value: object, preview: object) -> bool:
    """以可核验文本关系识别首轮提示词的等值、截断或薄包装副本。"""

    title = _title_comparison_text(value)
    source = _title_comparison_text(preview)
    if not title or not source:
        return False
    if title == source:
        return True

    prefix = title
    if prefix.endswith("…"):
        prefix = prefix[:-1].rstrip()
    elif prefix.endswith("..."):
        prefix = prefix[:-3].rstrip()
    if len(prefix) >= 24 and len(source) > len(prefix) and source.startswith(prefix):
        return True

    # 历史异常会把几乎完整的 preview 包在 ``/goal`` 等很短的外壳里。
    # 只在较长文本且长度非常接近时接受包含关系，避免把正常短标题（恰好
    # 出现在提示词里）误判为提示词副本。
    shorter, longer = sorted((title, source), key=len)
    if len(shorter) >= 24 and shorter in longer:
        return len(shorter) / max(1, len(longer)) >= 0.80
    return False


def prompt_derived_thread_title(value: object, record: ThreadRecord) -> bool:
    """标题是否只是 SQLite 首轮提示词/preview 的派生副本。"""

    title = _title_comparison_text(value)
    if not title:
        return False
    if record.title_source == "session_index_name" and _as_text(value) == record.title:
        return False
    explicit_name = _title_comparison_text(record.raw.get("name"))
    if explicit_name and title == explicit_name:
        return False
    # raw.title 既可能是用户重命名/系统生成的真实短标题，也可能被 Codex
    # 填成首轮提示词，不能仅因“等于 title 字段”就判定为提示词。只有与
    # preview（当前已知的首轮内容来源）等值或构成截断前缀时才有直接证据。
    for raw in (record.raw.get("preview"), record.preview):
        if _title_is_preview_derived(title, raw):
            return True
    return False


def independent_thread_title(
    record: ThreadRecord, *preferred_titles: object
) -> str:
    """返回独立真实标题；首轮提示词派生值不冒充会话名称。"""

    explicit_name = _as_text(record.raw.get("name"))
    # 本地结构化生命周期已经按 manual > SQLite > session_index 解析；调用方
    # 提供的 Desktop 快照只用于本地尚无独立标题时补位，不能反向覆盖新值。
    for candidate in (explicit_name, record.title):
        title = _as_text(candidate)
        if title and not prompt_derived_thread_title(title, record):
            return title
    # Callers provide only structured Desktop list_threads title/name fields,
    # never summaries, search snippets or raw prompt text inferred as a name.
    for candidate in preferred_titles:
        title = _as_text(candidate)
        if title:
            return title
    return ""


def _resolve_thread_title(
    *, name: object, sqlite_title: object, session_title: object, preview: object
) -> tuple[str, str]:
    """按 Codex 标题生命周期选择当前名称，并保留可测试的来源。

    ``threads.name`` 是人工重命名；``threads.title`` 是当前结构化自动标题。
    ``session_index`` 是追加式侧栏索引，只在 SQLite 仍为首轮提示词回退时
    补充已异步生成的独立标题。两处都没有独立标题时保留紧凑的提示词回退，
    供上层明确识别为历史损坏，而不是伪造一条 Codex 标题。
    """

    explicit_name = _as_text(name)
    raw_title = _as_text(sqlite_title)
    indexed_title = _as_text(session_title)
    raw_preview = _as_text(preview)
    if explicit_name:
        return explicit_name, "manual_name"
    if raw_title and not _title_is_preview_derived(raw_title, raw_preview):
        return raw_title, "sqlite_title"
    # An explicitly indexed name equal to a request is still a name.
    # Do not infer the same provenance for an unindexed SQLite preview.
    if indexed_title and indexed_title == raw_preview:
        return indexed_title, "session_index_name"
    if indexed_title and not _title_is_preview_derived(indexed_title, raw_preview):
        return indexed_title, "session_index_title"
    if indexed_title:
        return indexed_title, "prompt_fallback"
    if raw_title:
        return raw_title, "prompt_fallback"
    if raw_preview:
        return raw_preview, "preview_fallback"
    return "", "missing"


def thread_title_recovery_hash(record: ThreadRecord) -> str:
    """为异常恢复名生成不含正文的稳定内容版本键。"""

    payload = {
        "thread_id": record.thread_id,
        "title": record.title,
        "title_source": record.title_source,
        "preview": record.preview,
        "updated_at_ms": record.updated_at_ms,
        "rollout_path": record.rollout_path,
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def public_thread_title(
    record: ThreadRecord,
    recovered_title: object = "",
) -> tuple[str, str]:
    """返回所有飞书/CLI列表共享的安全展示名及来源。

    恢复名只用于已确认没有人工/自动/索引标题的历史异常，不写回 Codex
    元数据；任何真实独立标题一出现都会立即覆盖它。
    """

    title = independent_thread_title(record)
    if title:
        source = (
            "codex_manual"
            if record.title_source == "manual_name"
            else "codex_generated"
        )
        return title, source
    recovered = _as_text(recovered_title)
    if recovered and not prompt_derived_thread_title(recovered, record):
        return recovered, "recovered_summary"
    suffix = record.thread_id[:8] or "unknown"
    return f"历史会话（标题尚未恢复·{suffix}）", "unavailable"


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _event_time(value: Any) -> int | None:
    """Parse a rollout timestamp without treating arbitrary text as a time.

    Codex payloads normally use Unix seconds/milliseconds.  The outer rollout
    envelope has also used RFC-3339 strings, so accept that one explicit shape
    and normalize it to milliseconds.  ``_time_key`` below handles the unit
    comparison for both forms.
    """

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) else None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        pass
    try:
        numeric = float(text)
    except (TypeError, ValueError):
        numeric = None
    if numeric is not None:
        return int(numeric) if math.isfinite(numeric) else None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _pick_value(row: Mapping[str, Any], *names: str) -> Any:
    """按列名别名读取值，不对值做模糊匹配。"""

    lowered = {str(key).casefold(): key for key in row.keys()}
    for name in names:
        actual = lowered.get(name.casefold())
        if actual is not None:
            return row[actual]
    return None


def _normalize_status(value: Any) -> ThreadStatus:
    """只接受完整状态字段，不从任何自然语言内容推断状态。"""

    if not isinstance(value, str):
        return ThreadStatus.UNKNOWN
    # 这些是数据库字段的完整拼写兼容项，不是对消息内容做关键词扫描。
    aliases = {
        "completed": ThreadStatus.COMPLETED,
        "interrupted": ThreadStatus.INTERRUPTED,
        "failed": ThreadStatus.FAILED,
        "cancelled": ThreadStatus.CANCELLED,
        "canceled": ThreadStatus.CANCELLED,
        "inprogress": ThreadStatus.IN_PROGRESS,
        "in_progress": ThreadStatus.IN_PROGRESS,
        "in-progress": ThreadStatus.IN_PROGRESS,
    }
    return aliases.get(value.strip().casefold(), ThreadStatus.UNKNOWN)


def _time_key(value: int | None) -> int:
    """把秒级和毫秒级时间转换为可比较的整数。"""

    if value is None:
        return -1
    # 当前 Codex 使用 Unix 秒；旧/未来 schema 可能使用毫秒。
    return value * 1000 if abs(value) < 10_000_000_000 else value


class CodexStore:
    """安全、低开销的 Codex 状态查询器。

    每次公开查询都使用短生命周期的只读连接，避免长期持有 Codex 的 WAL 文件
    和锁。SQLite 结果不缓存；仅为旧任务的 append-only rollout 保留字节游标，
    避免结构化历史投影缺失时每轮重复扫描大文件。
    """

    def __init__(
        self,
        paths: StorePaths | str | os.PathLike[str] | None = None,
        *,
        codex_home: str | os.PathLike[str] | None = None,
        state_db: str | os.PathLike[str] | None = None,
        history_db: str | os.PathLike[str] | None = None,
        state_path: str | os.PathLike[str] | None = None,
        history_path: str | os.PathLike[str] | None = None,
        timeout_seconds: float = 0.5,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        if isinstance(paths, StorePaths):
            selected = paths
        elif paths is not None:
            # 允许直接传入 ``Path.home()/'.codex'``，便于和项目配置对象衔接。
            selected = StorePaths.from_codex_home(paths)
        else:
            selected = StorePaths.from_codex_home(codex_home)
        selected_state = state_db or state_path
        selected_history = history_db or history_path
        self.paths = StorePaths(
            state_db=Path(selected_state).expanduser().resolve()
            if selected_state
            else selected.state_db,
            history_db=Path(selected_history).expanduser().resolve()
            if selected_history
            else selected.history_db,
            session_index=selected.session_index,
        )
        self.timeout_seconds = float(timeout_seconds)
        # A store is shared by the service's monitor and request handlers.  A
        # process-wide error list lets one failing call poison an unrelated
        # concurrent call; keep the current query's list in thread-local state.
        self._query_state = threading.local()
        self._rollout_cursors: dict[tuple[str, str], _RolloutCursor] = {}
        self._rollout_lock = threading.RLock()

    def _errors(self) -> list[str]:
        errors = getattr(self._query_state, "errors", None)
        if errors is None:
            errors = []
            self._query_state.errors = errors
        return errors

    @property
    def last_errors(self) -> tuple[str, ...]:
        """最近一次查询遇到的类型化错误，不包含 SQL 或用户内容。"""

        return tuple(self._errors())

    def require_readable(self, operation: str = "读取 Codex 状态") -> None:
        """显式抛出最近一次查询错误；正常空结果和 unknown 不会触发。"""

        errors = tuple(self._errors())
        if errors:
            raise CodexStoreReadError(operation, errors)

    def _begin_query(self) -> None:
        self._query_state.errors = []
        self._query_state.shared_rollout_paths = set()

    def _open(self, path: Path, label: str) -> sqlite3.Connection | None:
        if not path.is_file():
            self._errors().append(f"{label}:missing")
            return None
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                _readonly_uri(path),
                uri=True,
                timeout=self.timeout_seconds,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            # mode=ro 已禁止写入；query_only 进一步表达并保护意图。
            connection.execute("PRAGMA query_only = ON")
            return connection
        except (OSError, sqlite3.Error):
            # PRAGMA 或 schema 校验失败时，连接可能已经创建，必须立即释放。
            if connection is not None:
                try:
                    connection.close()
                except (OSError, sqlite3.Error):
                    pass
            self._errors().append(f"{label}:unavailable")
            return None

    @staticmethod
    def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
        # 表名来自固定候选集合，不接受用户输入，因此不会形成 SQL 注入点。
        cursor: sqlite3.Cursor | None = None
        try:
            cursor = connection.execute(f'PRAGMA table_info("{table}")')
            return {str(row[1]) for row in cursor.fetchall()}
        except sqlite3.Error:
            # 缺表时 SQLite 的 PRAGMA 会返回空集合；真正的数据库读取错误
            # 必须向上抛出，由调用方记录并进入 fail-closed 重试熔断路径。
            raise
        finally:
            if cursor is not None:
                cursor.close()

    @staticmethod
    def _find_table(
        connection: sqlite3.Connection, candidates: Iterable[str]
    ) -> tuple[str, set[str]] | None:
        for table in candidates:
            columns = CodexStore._table_columns(connection, table)
            if columns:
                return table, columns
        return None

    @staticmethod
    def _select_columns(
        columns: set[str], aliases: Mapping[str, tuple[str, ...]]
    ) -> tuple[list[str], dict[str, str]]:
        """返回可用的原列名和 canonical->原列名映射。"""

        by_folded = {column.casefold(): column for column in columns}
        selected: list[str] = []
        mapping: dict[str, str] = {}
        for canonical, candidates in aliases.items():
            for candidate in candidates:
                actual = by_folded.get(candidate.casefold())
                if actual is not None:
                    mapping[canonical] = actual
                    if actual not in selected:
                        selected.append(actual)
                    break
        return selected, mapping

    @contextmanager
    def metadata_batch(self):
        """Reuse directory reads within one poll, never turns or rollout data."""
        previous = getattr(self._query_state, 'metadata_batch', None)
        self._query_state.metadata_batch = {}
        try:
            yield
        finally:
            self._query_state.metadata_batch = previous

    def _read_threads(self, *, prepare_rollout_ownership: bool = True):
        cache = getattr(self._query_state, 'metadata_batch', None)
        if cache is None:
            return self._read_threads_uncached(prepare_rollout_ownership=prepare_rollout_ownership)
        key = bool(prepare_rollout_ownership)
        if key not in cache:
            before = len(self._errors())
            records, available = self._read_threads_uncached(prepare_rollout_ownership=key)
            cache[key] = (records, available, tuple(self._errors()[before:]),
                          frozenset(getattr(self._query_state, 'shared_rollout_paths', set())))
            return list(records), available
        records, available, errors, shared = cache[key]
        self._errors().extend(errors)
        self._query_state.shared_rollout_paths = set(shared)
        return list(records), available

    def _read_threads_uncached(
        self, *, prepare_rollout_ownership: bool = True
    ) -> tuple[list[ThreadRecord], bool]:
        connection = self._open(self.paths.state_db, "state")
        if connection is None:
            return [], False
        try:
            table_info = self._find_table(connection, ("threads", "thread"))
            if table_info is None:
                self._errors().append("state:schema")
                return [], True
            table, columns = table_info
            aliases = {
                "id": ("id", "thread_id", "threadId"),
                "title": ("title",),
                "name": ("name",),
                "cwd": ("cwd", "working_directory", "workdir"),
                "updated_at_ms": ("updated_at_ms", "updatedAtMs"),
                "updated_at": ("updated_at", "updatedAt"),
                "created_at_ms": ("created_at_ms", "createdAtMs"),
                "created_at": ("created_at", "createdAt"),
                "archived": ("archived",),
                "preview": ("preview",),
                "source": ("source",),
                "thread_source": ("thread_source", "threadSource"),
                "rollout_path": ("rollout_path", "rolloutPath"),
            }
            selected, _mapping = self._select_columns(columns, aliases)
            if "id" not in _mapping:
                self._errors().append("state:thread-id-column")
                return [], True
            quoted = ", ".join(f'"{column}"' for column in selected)
            rows = connection.execute(f'SELECT {quoted} FROM "{table}"').fetchall()
            # Codex 的人工名称、结构化自动标题与旧 session_index 分属不同
            # 生命周期。追加式索引不能无条件覆盖 SQLite 中较新的真实标题。
            session_names = self._read_session_names()
            result: list[ThreadRecord] = []
            for row in rows:
                raw = dict(row)
                thread_id = _as_text(_pick_value(raw, "id", "thread_id", "threadId"))
                if not thread_id:
                    continue
                title, title_source = _resolve_thread_title(
                    name=_pick_value(raw, "name"),
                    sqlite_title=_pick_value(raw, "title"),
                    session_title=session_names.get(thread_id, ""),
                    preview=_pick_value(raw, "preview"),
                )
                result.append(
                    ThreadRecord(
                        thread_id=thread_id,
                        title=title,
                        cwd=_as_text(
                            _pick_value(raw, "cwd", "working_directory", "workdir")
                        ),
                        updated_at_ms=_as_int(
                            _pick_value(raw, "updated_at_ms", "updatedAtMs")
                        )
                        or _as_int(_pick_value(raw, "updated_at", "updatedAt")),
                        created_at_ms=_as_int(
                            _pick_value(raw, "created_at_ms", "createdAtMs")
                        )
                        or _as_int(_pick_value(raw, "created_at", "createdAt")),
                        archived=bool(_as_int(_pick_value(raw, "archived")) or 0),
                        preview=_as_text(_pick_value(raw, "preview")),
                        source=_as_text(_pick_value(raw, "source")),
                        thread_source=_as_text(
                            _pick_value(raw, "thread_source", "threadSource")
                        ),
                        rollout_path=_as_text(
                            _pick_value(raw, "rollout_path", "rolloutPath")
                        ),
                        raw=raw,
                        title_source=title_source,
                    )
                )
            # Directory-only selection does not read rollout files. Actual
            # rollout readers keep the default and rebuild ownership every time.
            if not prepare_rollout_ownership:
                return result, True
            owners: dict[str, set[str]] = {}
            for record in result:
                path_key = self._rollout_path_key(record.rollout_path)
                if path_key:
                    owners.setdefault(path_key, set()).add(record.thread_id)
            self._query_state.shared_rollout_paths = {
                path_key for path_key, thread_ids in owners.items() if len(thread_ids) > 1
            }
            return result, True
        except (OSError, sqlite3.Error):
            self._errors().append("state:read")
            return [], True
        finally:
            connection.close()

    def _read_session_names(self) -> dict[str, str]:
        """只读加载 Codex Desktop 维护的任务短标题索引。"""

        path = self.paths.session_index
        if path is None or not path.is_file():
            return {}
        result: dict[str, str] = {}
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(payload, dict):
                        continue
                    thread_id = _as_text(payload.get("id"))
                    thread_name = _as_text(payload.get("thread_name"))
                    if thread_id and thread_name:
                        result[thread_id] = thread_name
        except (OSError, UnicodeError):
            return {}
        return result

    @staticmethod
    def _row_to_turn(row: Mapping[str, Any]) -> TurnRecord | None:
        thread_id = _as_text(_pick_value(row, "thread_id", "threadId"))
        if not thread_id:
            return None
        return TurnRecord(
            thread_id=thread_id,
            turn_id=_as_text(_pick_value(row, "turn_id", "turnId", "id")),
            status=_normalize_status(_pick_value(row, "status")),
            rollout_ordinal=_as_int(
                _pick_value(row, "rollout_ordinal", "rolloutOrdinal", "ordinal")
            ),
            started_at=_as_int(_pick_value(row, "started_at", "startedAt")),
            completed_at=_as_int(_pick_value(row, "completed_at", "completedAt")),
            duration_ms=_as_int(_pick_value(row, "duration_ms", "durationMs")),
            error_json=(
                str(_pick_value(row, "error_json", "errorJson"))
                if _pick_value(row, "error_json", "errorJson") is not None
                else None
            ),
            final_agent_item_id=_as_text(
                _pick_value(row, "final_agent_item_id", "finalAgentItemId")
            ),
            raw=dict(row),
        )

    def _read_final_messages(
        self,
        connection: sqlite3.Connection,
        turn: TurnRecord,
    ) -> tuple[str, ...]:
        """以最终 item 指针为锚，读取同轮有序的正式答复材料。

        这里刻意不扫描文本、不寻找“最后一条消息”，也不读取 rollout 或大日志。
        旧版本没有 ``thread_items`` 或相关列时返回空元组；精确 SQL 读取失败
        则记录类型化错误，使外层服务停止并告警。
        """

        item_id = turn.final_agent_item_id.strip()
        if turn.status not in {
            ThreadStatus.COMPLETED,
            ThreadStatus.FAILED,
            ThreadStatus.INTERRUPTED,
        } or not item_id:
            return ()
        table_info = self._find_table(connection, ("thread_items",))
        if table_info is None:
            # 旧 Codex schema 没有投影表：兼容为空，不把它伪装成读取错误。
            return ()
        table, columns = table_info
        aliases = {
            "thread_id": ("thread_id", "threadId"),
            "turn_id": ("turn_id", "turnId"),
            "item_id": ("item_id", "itemId", "id"),
            "item_type": ("item_type", "itemType"),
            "item_json": ("item_json", "itemJson"),
        }
        selected, mapping = self._select_columns(columns, aliases)
        required = ("thread_id", "turn_id", "item_id", "item_type", "item_json")
        if any(name not in mapping for name in required):
            # schema 演进时缺列只意味着没有可用的最终答复投影。
            return ()
        try:
            row = connection.execute(
                (
                    f'SELECT "{mapping["item_type"]}", "{mapping["item_json"]}" '
                    f'FROM "{table}" '
                    f'WHERE "{mapping["thread_id"]}" = ? '
                    f'AND "{mapping["turn_id"]}" = ? '
                    f'AND "{mapping["item_id"]}" = ? LIMIT 1'
                ),
                (turn.thread_id, turn.turn_id, item_id),
            ).fetchone()
        except (OSError, sqlite3.Error):
            self._errors().append("history:final-message-read")
            return ()
        if row is None:
            return ()
        item_type = _as_text(row[0])
        if item_type != "agentMessage":
            return ()
        raw_json = row[1]
        if not isinstance(raw_json, str) or not raw_json.strip():
            return ()
        try:
            item = json.loads(raw_json)
        except (TypeError, json.JSONDecodeError):
            return ()
        if not isinstance(item, Mapping):
            return ()
        # Codex Desktop 当前把最终答复保存为 ``agentMessage``，旧版本曾使用
        # ``message`` + ``role=assistant``。两种结构都必须依赖 thread_turns 的
        # 精确 final_agent_item_id 指针，并且只接受 final_answer 阶段；不能退化
        # 成扫描“最后一条看起来像助手消息”的模糊逻辑。
        item_kind = item.get("type")
        role = item.get("role")
        current_shape = item_kind == "agentMessage" and (
            role is None or role == "assistant"
        )
        legacy_shape = item_kind == "message" and role == "assistant"
        embedded_id = item.get("id")
        if (
            not (current_shape or legacy_shape)
            or item.get("phase") != "final_answer"
            or (embedded_id is not None and embedded_id != item_id)
        ):
            return ()
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            return ()
        anchor_text = text.strip()
        if "rollout_ordinal" not in columns:
            return (anchor_text,)
        try:
            anchor = connection.execute(
                f'SELECT rollout_ordinal FROM "{table}" WHERE "{mapping["thread_id"]}"=? AND "{mapping["turn_id"]}"=? AND "{mapping["item_id"]}"=?',
                (turn.thread_id, turn.turn_id, item_id),
            ).fetchone()
            if anchor is None or not isinstance(anchor[0], int):
                return (anchor_text,)
            rows = connection.execute(
                f'SELECT "{mapping["item_id"]}", "{mapping["item_type"]}", "{mapping["item_json"]}", rollout_ordinal FROM "{table}" '
                f'WHERE "{mapping["thread_id"]}"=? AND "{mapping["turn_id"]}"=? AND "{mapping["item_type"]}"=? AND rollout_ordinal<=? '
                'ORDER BY rollout_ordinal LIMIT 257',
                (turn.thread_id, turn.turn_id, "agentMessage", anchor[0]),
            ).fetchall()
        except (OSError, sqlite3.Error):
            self._errors().append("history:final-material-read")
            return ()
        if len(rows)>256:
            self._errors().append("history:final-material-limit")
            return ()
        parts=[]
        for rid, kind, raw, ordinal in rows:
            try:
                value=json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(value, Mapping) or value.get("phase")!="final_answer":
                continue
            valid=(value.get("type")=="agentMessage" and value.get("role") in (None,"assistant")) or (value.get("type")=="message" and value.get("role")=="assistant")
            if not valid or value.get("id",rid)!=rid:
                continue
            if any(value.get(k) not in (None,expected) for k,expected in (("thread_id",turn.thread_id),("threadId",turn.thread_id),("turn_id",turn.turn_id),("turnId",turn.turn_id))):
                continue
            body=value.get("text")
            if isinstance(body,str) and body.strip():
                parts.append((rid,body.strip()))
        if not parts or parts[-1][0]!=item_id:
            self._errors().append("history:final-material-anchor")
            return ()
        if sum(len(body) for _,body in parts)>2*1024*1024:
            self._errors().append("history:final-material-limit")
            return ()
        return tuple(body for _,body in parts)

    @staticmethod
    def _answer_material(parts: tuple[str, ...]) -> str:
        if len(parts)<=1:
            return parts[0] if parts else ""
        return "\n\n".join(f"【本轮正式答复 {i}，按时间先后】\n{part}" for i,part in enumerate(parts,1))

    @staticmethod
    def _image_header_matches(mime_type: str, header: bytes) -> bool:
        if mime_type == "image/png":
            return header.startswith(b"\x89PNG\r\n\x1a\n")
        if mime_type == "image/jpeg":
            return header.startswith(b"\xff\xd8\xff")
        if mime_type == "image/webp":
            return len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP"
        return False

    def _validated_generated_image(
        self,
        turn: TurnRecord,
        item_id: str,
        item: Mapping[str, Any],
    ) -> GeneratedImageArtifact | None:
        """把 Codex 图片投影收窄为 generated_images 中的同轮原始文件。"""

        if (
            item.get("type") != "imageGeneration"
            or item.get("status") != "completed"
            or item.get("id") != item_id
        ):
            return None
        saved_path = item.get("savedPath")
        if not isinstance(saved_path, str) or not saved_path.strip():
            return None
        try:
            root = (
                self.paths.state_db.parent / "generated_images" / turn.thread_id
            ).resolve(strict=True)
            candidate = Path(saved_path).expanduser().resolve(strict=True)
            root_key = self._comparison_path(root)
            candidate_key = self._comparison_path(candidate)
            if os.path.commonpath((root_key, candidate_key)) != root_key:
                return None
            if self._comparison_path(candidate.parent) != root_key:
                return None
            if not candidate.is_file() or candidate.stem != item_id:
                return None
            mime_type = _GENERATED_IMAGE_FORMATS.get(candidate.suffix.casefold())
            if mime_type is None:
                return None
            stat = candidate.stat()
            size = int(stat.st_size)
            if size <= 0 or size > _GENERATED_IMAGE_MAX_BYTES:
                return None
            digest = hashlib.sha256()
            with candidate.open("rb") as handle:
                header = handle.read(16)
                if not self._image_header_matches(mime_type, header):
                    return None
                digest.update(header)
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except (OSError, RuntimeError, ValueError):
            return None
        return GeneratedImageArtifact(
            item_id=item_id,
            path=str(candidate),
            mime_type=mime_type,
            sha256=digest.hexdigest(),
            size=size,
            file_name=candidate.name,
        )

    def _read_generated_images(
        self,
        connection: sqlite3.Connection,
        turn: TurnRecord,
    ) -> tuple[GeneratedImageArtifact, ...]:
        """精确读取同一轮的完成态 imageGeneration 投影。"""

        if turn.status not in {
            ThreadStatus.COMPLETED,
            ThreadStatus.FAILED,
            ThreadStatus.INTERRUPTED,
        }:
            return ()
        table_info = self._find_table(connection, ("thread_items",))
        if table_info is None:
            return ()
        table, columns = table_info
        aliases = {
            "thread_id": ("thread_id", "threadId"),
            "turn_id": ("turn_id", "turnId"),
            "item_id": ("item_id", "itemId", "id"),
            "item_type": ("item_type", "itemType"),
            "item_json": ("item_json", "itemJson"),
            "rollout_ordinal": ("rollout_ordinal", "rolloutOrdinal", "ordinal"),
        }
        _selected, mapping = self._select_columns(columns, aliases)
        required = ("thread_id", "turn_id", "item_id", "item_type", "item_json")
        if any(name not in mapping for name in required):
            return ()
        order = (
            f' ORDER BY "{mapping["rollout_ordinal"]}" ASC'
            if "rollout_ordinal" in mapping
            else ""
        )
        try:
            rows = connection.execute(
                (
                    f'SELECT "{mapping["item_id"]}", "{mapping["item_json"]}" '
                    f'FROM "{table}" '
                    f'WHERE "{mapping["thread_id"]}" = ? '
                    f'AND "{mapping["turn_id"]}" = ? '
                    f'AND "{mapping["item_type"]}" = ?{order}'
                ),
                (turn.thread_id, turn.turn_id, "imageGeneration"),
            ).fetchall()
        except (OSError, sqlite3.Error):
            self._errors().append("history:generated-image-read")
            return ()
        images: list[GeneratedImageArtifact] = []
        for row in rows:
            item_id = _as_text(row[0])
            raw_json = row[1]
            if not item_id or not isinstance(raw_json, str):
                continue
            try:
                item = json.loads(raw_json)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(item, Mapping):
                continue
            artifact = self._validated_generated_image(turn, item_id, item)
            if artifact is not None:
                images.append(artifact)
        return tuple(images)

    def _read_delivered_files(self, connection, turn, final_message):
        items = []
        if final_message:
            items.append({"type":"agentMessage", "id":turn.final_agent_item_id,
                          "phase":"final_answer", "text":final_message})
        info = self._find_table(connection, ("thread_items",))
        if info and {"thread_id","turn_id","item_type","item_json"}.issubset(info[1]):
            # Cursor iteration retains all actual resources without a silent cap.
            for row in connection.execute("SELECT item_json FROM thread_items WHERE thread_id=? AND turn_id=? AND item_type='mcpToolCall' ORDER BY rowid", (turn.thread_id,turn.turn_id)):
                try:
                    item=json.loads(row[0])
                except (TypeError, ValueError):
                    continue
                if isinstance(item,dict):
                    items.append(item)
        return tuple(discover_delivered_files(items, turn_id=turn.turn_id, inspect_content=False).candidates)

    def _read_turns(
        self, thread_id: str, *, project_all_results: bool = False
    ) -> tuple[list[TurnRecord], bool]:
        connection = self._open(self.paths.history_db, "history")
        if connection is None:
            return [], False
        try:
            table_info = self._find_table(
                connection, ("thread_turns", "turns", "thread_status")
            )
            if table_info is None:
                self._errors().append("history:schema")
                return [], True
            table, columns = table_info
            aliases = {
                "thread_id": ("thread_id", "threadId"),
                "turn_id": ("turn_id", "turnId", "id"),
                "rollout_ordinal": ("rollout_ordinal", "rolloutOrdinal", "ordinal"),
                "status": ("status",),
                "error_json": ("error_json", "errorJson"),
                "started_at": ("started_at", "startedAt"),
                "completed_at": ("completed_at", "completedAt"),
                "duration_ms": ("duration_ms", "durationMs"),
                "final_agent_item_id": ("final_agent_item_id", "finalAgentItemId"),
            }
            selected, mapping = self._select_columns(columns, aliases)
            if "thread_id" not in mapping or "status" not in mapping:
                self._errors().append("history:turn-columns")
                return [], True
            quoted = ", ".join(f'"{column}"' for column in selected)
            thread_column = mapping["thread_id"]
            rows = connection.execute(
                f'SELECT {quoted} FROM "{table}" WHERE "{thread_column}" = ?',
                (thread_id,),
            ).fetchall()
            result = []
            for row in rows:
                turn = self._row_to_turn(dict(row))
                if turn is not None:
                    result.append(turn)
            result.sort(
                key=lambda turn: (
                    _time_key(turn.completed_at or turn.started_at),
                    turn.rollout_ordinal if turn.rollout_ordinal is not None else -1,
                    turn.turn_id,
                ),
                reverse=True,
            )
            indexes = range(len(result)) if project_all_results else range(min(1, len(result)))
            for index in indexes:
                latest = result[index]
                final_parts = self._read_final_messages(connection, latest)
                final_message = self._answer_material(final_parts)
                generated_images = self._read_generated_images(connection, latest)
                delivered_files = self._read_delivered_files(connection, latest, final_message)
                if final_message or generated_images or delivered_files:
                    result[index] = TurnRecord(
                        thread_id=latest.thread_id,
                        turn_id=latest.turn_id,
                        status=latest.status,
                        rollout_ordinal=latest.rollout_ordinal,
                        started_at=latest.started_at,
                        completed_at=latest.completed_at,
                        duration_ms=latest.duration_ms,
                        error_json=latest.error_json,
                        final_agent_item_id=latest.final_agent_item_id,
                        final_message=final_message,
                        final_answer_parts=final_parts,
                        generated_images=generated_images,
                        delivered_files=delivered_files,
                        raw=latest.raw,
                    )
            return result, True
        except (OSError, sqlite3.Error):
            self._errors().append("history:read")
            return [], True
        finally:
            connection.close()

    @staticmethod
    def _comparison_path(path: Path) -> str:
        """归一化 Windows extended-length 前缀，供目录边界比较使用。"""

        value = os.path.normcase(os.path.abspath(os.fspath(path)))
        if os.name == "nt":
            lowered = value.casefold()
            if lowered.startswith("\\\\?\\unc\\"):
                value = "\\\\" + value[8:]
            elif lowered.startswith("\\\\?\\"):
                value = value[4:]
        return os.path.normcase(os.path.normpath(value))

    @classmethod
    def _rollout_path_key(cls, raw_path: object) -> str:
        """Return a comparison key for state rows that point to a rollout."""

        value = _as_text(raw_path)
        if not value:
            return ""
        try:
            return cls._comparison_path(Path(value).expanduser().resolve(strict=False))
        except (OSError, RuntimeError, ValueError):
            return ""

    def _validated_rollout_path(self, thread: ThreadRecord) -> Path | None:
        """只允许读取当前 CODEX_HOME/sessions 内的普通 rollout 文件。"""

        raw_path = thread.rollout_path.strip()
        if not raw_path:
            return None
        try:
            sessions_root = (self.paths.state_db.parent / "sessions").resolve()
            candidate = Path(raw_path).expanduser().resolve(strict=True)
            root_key = self._comparison_path(sessions_root)
            candidate_key = self._comparison_path(candidate)
            if os.path.commonpath((root_key, candidate_key)) != root_key:
                return None
            if not candidate.is_file():
                return None
        except (OSError, RuntimeError, ValueError):
            return None
        return candidate

    def _shared_rollout_path(self, path: Path) -> bool:
        shared_paths = getattr(self._query_state, "shared_rollout_paths", set())
        return self._comparison_path(path) in shared_paths

    @staticmethod
    def _rollout_event_thread_id(
        payload: Mapping[str, Any], event: Mapping[str, Any] | None = None
    ) -> str:
        """Read the optional explicit owner carried by a rollout event."""

        event = event if isinstance(event, Mapping) else {}
        return _as_text(
            _pick_value(payload, "thread_id", "threadId", "thread-id")
        ) or _as_text(
            _pick_value(event, "thread_id", "threadId", "thread-id")
        )

    @staticmethod
    def _rollout_terminal_event(
        thread_id: str,
        payload: Mapping[str, Any],
        line_number: int,
        event: Mapping[str, Any] | None = None,
    ) -> TurnRecord | None:
        """把 Codex 显式终态事件映射为 TurnRecord；不检查任何正文关键词。

        The path is trusted for the owning thread, but some rollout variants
        include an explicit thread/session id in the envelope.  Honour that
        identity when present so a shared/reused path cannot attribute a
        terminal event to a different thread.  The envelope timestamp is a
        real event timestamp, not a heuristic based on file mtime.
        """

        event = event if isinstance(event, Mapping) else {}
        event_thread_id = CodexStore._rollout_event_thread_id(payload, event)
        if event_thread_id and event_thread_id != thread_id:
            return None

        event_type = _as_text(payload.get("type"))
        status = {
            "task_complete": ThreadStatus.COMPLETED,
            "turn_aborted": ThreadStatus.INTERRUPTED,
        }.get(event_type)
        turn_id = _as_text(_pick_value(payload, "turn_id", "turnId"))
        if status is None or not turn_id:
            return None
        final_message = ""
        if status is ThreadStatus.COMPLETED:
            value = payload.get("last_agent_message")
            if isinstance(value, str):
                final_message = value.strip()
        completed_at = _event_time(
            _pick_value(payload, "completed_at", "completedAt")
        )
        # Payload time is authoritative.  If it is absent, the envelope's
        # timestamp is the only explicit event-time evidence available.
        if completed_at is None:
            completed_at = _event_time(
                _pick_value(event, "timestamp", "time", "created_at", "createdAt")
            )
        return TurnRecord(
            thread_id=thread_id,
            turn_id=turn_id,
            status=status,
            rollout_ordinal=line_number,
            started_at=_event_time(_pick_value(payload, "started_at", "startedAt")),
            completed_at=completed_at,
            duration_ms=_as_int(_pick_value(payload, "duration_ms", "durationMs")),
            final_message=final_message,
            delivered_files=discover_delivered_files([{ "type":"agentMessage", "id":"rollout-final", "phase":"final_answer", "text":final_message}], turn_id=turn_id, inspect_content=False).candidates,
            raw={"source": "codex-rollout", "event_type": event_type},
        )

    @staticmethod
    def _rollout_prefix_digest(path: Path, length: int) -> str | None:
        """Hash exactly the already-consumed prefix of a rollout file.

        Reading the prefix is deliberately conservative: an append-only cursor
        is reusable only when every byte it previously consumed is unchanged.
        This catches same-inode rewrites that happen to increase (or preserve)
        the file size, including rewrites beyond a small fixed sampling window.
        """

        if length < 0:
            return None
        digest = hashlib.sha256()
        remaining = int(length)
        try:
            with path.open("rb") as handle:
                while remaining:
                    chunk = handle.read(min(1024 * 1024, remaining))
                    if not chunk:
                        return None
                    digest.update(chunk)
                    remaining -= len(chunk)
        except OSError:
            return None
        return digest.hexdigest()

    @staticmethod
    def _new_rollout_cursor(identity: tuple[int, int]) -> _RolloutCursor:
        return _RolloutCursor(
            identity=identity,
            prefix_digest=hashlib.sha256(b"").hexdigest(),
        )

    def _read_rollout_latest_turn(self, thread: ThreadRecord) -> TurnRecord | None:
        """增量读取旧任务 rollout 中最新的显式完成/中断事件。"""

        path = self._validated_rollout_path(thread)
        if path is None:
            return None
        try:
            file_stat = path.stat()
        except OSError:
            return None
        # A path may be reused by another thread.  Cursor state and file
        # position are therefore never shared solely by pathname.
        key = (self._comparison_path(path), thread.thread_id)
        identity = (int(file_stat.st_dev), int(file_stat.st_ino))
        shared_path = self._shared_rollout_path(path)
        with self._rollout_lock:
            cursor = self._rollout_cursors.get(key)
            reset = bool(
                cursor is None
                or cursor.identity != identity
                or int(file_stat.st_size) < cursor.offset
            )
            if not reset and cursor is not None and cursor.ambiguous_thread_event:
                if shared_path:
                    self._errors().append("rollout:thread-ambiguous")
                    return None
                # State ownership changed: re-read the file now that an
                # unscoped event can be attributed to the sole remaining owner.
                reset = True
            if (
                not reset
                and cursor is not None
                and int(file_stat.st_size) == cursor.file_size
                and int(file_stat.st_mtime_ns) == cursor.mtime_ns
            ):
                # A completely unchanged file is the common monitor-poll
                # path.  Do not hash a large consumed prefix on every poll;
                # content validation is required when size/mtime indicates a
                # possible rewrite or append.
                return cursor.latest_turn
            if not reset and cursor is not None:
                # mtime is only a cheap hint.  The content anchor is the
                # authority, so a rewrite with unchanged mtime is detected too.
                prefix = self._rollout_prefix_digest(path, cursor.offset)
                if prefix is None:
                    return cursor.latest_turn
                reset = prefix != cursor.prefix_digest
            if reset:
                cursor = self._new_rollout_cursor(identity)
                self._rollout_cursors[key] = cursor
            assert cursor is not None
            if int(file_stat.st_size) == cursor.offset:
                cursor.file_size = int(file_stat.st_size)
                cursor.mtime_ns = int(file_stat.st_mtime_ns)
                return cursor.latest_turn

            # Keep a stable fallback in case the writer replaces or rewrites
            # the file while it is being scanned.  Parsed records are not
            # committed as a cursor state until the old prefix is revalidated.
            original_offset = cursor.offset
            original_prefix_digest = cursor.prefix_digest
            original_latest = cursor.latest_turn
            try:
                with path.open("rb") as handle:
                    handle.seek(cursor.offset)
                    while True:
                        line_start = handle.tell()
                        raw_line = handle.readline()
                        if not raw_line:
                            break
                        # Codex 可能正在追加最后一行；保留起点，下轮再完整读取。
                        if not raw_line.endswith(b"\n"):
                            handle.seek(line_start)
                            break
                        cursor.offset = handle.tell()
                        cursor.line_number += 1
                        try:
                            item = json.loads(raw_line.decode("utf-8"))
                        except (UnicodeError, json.JSONDecodeError):
                            continue
                        if not isinstance(item, Mapping) or item.get("type") != "event_msg":
                            continue
                        payload = item.get("payload")
                        if not isinstance(payload, Mapping):
                            continue
                        terminal = self._rollout_terminal_event(
                            thread.thread_id, payload, cursor.line_number, item
                        )
                        if terminal is not None:
                            if shared_path and not self._rollout_event_thread_id(
                                payload, item
                            ):
                                cursor.ambiguous_thread_event = True
                                cursor.latest_turn = None
                                cursor.latest_completed_result = None
                                if "rollout:thread-ambiguous" not in self._errors():
                                    self._errors().append("rollout:thread-ambiguous")
                                continue
                            if cursor.ambiguous_thread_event:
                                # Once any unscoped terminal was observed, no
                                # later scoped result from the same shared file
                                # may be exposed through this cursor either.
                                continue
                            cursor.latest_turn = self._prefer_rollout_turn(
                                cursor.latest_turn, terminal
                            )
                            if (
                                terminal.status is ThreadStatus.COMPLETED
                                and terminal.final_message.strip()
                            ):
                                cursor.latest_completed_result = self._prefer_rollout_turn(
                                    cursor.latest_completed_result, terminal
                                )
            except OSError:
                return cursor.latest_turn

            try:
                after_stat = path.stat()
            except OSError:
                return cursor.latest_turn
            if (
                (int(after_stat.st_dev), int(after_stat.st_ino)) != identity
                or int(after_stat.st_size) < cursor.offset
            ):
                self._rollout_cursors[key] = self._new_rollout_cursor(
                    (int(after_stat.st_dev), int(after_stat.st_ino))
                )
                return original_latest
            # Verify bytes that were already consumed before this call.  A
            # normal append keeps them identical; an in-place rewrite during
            # the scan invalidates all records parsed from the old descriptor.
            old_prefix = self._rollout_prefix_digest(path, original_offset)
            if old_prefix != original_prefix_digest:
                self._rollout_cursors[key] = self._new_rollout_cursor(identity)
                return original_latest
            consumed_prefix = self._rollout_prefix_digest(path, cursor.offset)
            if consumed_prefix is None:
                self._rollout_cursors[key] = self._new_rollout_cursor(identity)
                return original_latest
            cursor.prefix_digest = consumed_prefix
            cursor.file_size = int(after_stat.st_size)
            cursor.mtime_ns = int(after_stat.st_mtime_ns)
            return cursor.latest_turn

    @staticmethod
    def _turn_time(turn: TurnRecord) -> int:
        """返回轮次的显式完成时间，缺失时才退到开始时间。"""

        value = turn.completed_at
        if value is None:
            value = turn.started_at
        return _time_key(value)

    @staticmethod
    def _turn_sequence(turn: TurnRecord) -> int:
        """Return strict rollout/file order when the event carries one."""

        value = turn.rollout_ordinal
        return int(value) if value is not None else -1

    @staticmethod
    def _compare_turn_evidence(current: TurnRecord, candidate: TurnRecord) -> int:
        """Compare candidate recency: explicit time first, sequence as tie-break.

        A lower explicit time can never overwrite a higher one, even when it was
        appended later.  When time is absent (or equal), the sequence is usable
        only within one evidence domain: history ordinals and rollout line
        numbers are unrelated scales.  If neither source establishes an order,
        return zero and let the caller keep the conservative history result.
        """

        current_value = current.completed_at
        if current_value is None:
            current_value = current.started_at
        candidate_value = candidate.completed_at
        if candidate_value is None:
            candidate_value = candidate.started_at
        current_time = _time_key(current_value) if current_value is not None else None
        candidate_time = _time_key(candidate_value) if candidate_value is not None else None
        current_sequence = CodexStore._turn_sequence(current)
        candidate_sequence = CodexStore._turn_sequence(candidate)
        same_evidence_domain = (
            (current.raw.get("source") == "codex-rollout")
            == (candidate.raw.get("source") == "codex-rollout")
        )
        if current_time is not None and candidate_time is not None:
            if candidate_time != current_time:
                return 1 if candidate_time > current_time else -1
        elif current_time is not None:
            # An un-timestamped later append is not enough evidence to move a
            # timestamped current turn backwards/forwards.  This is the
            # conservative boundary between explicit business time and an
            # event whose only ordering evidence is local file position.
            return -1
        elif candidate_time is not None:
            return 1
        elif (
            same_evidence_domain
            and current_sequence >= 0
            and candidate_sequence >= 0
        ):
            if candidate_sequence != current_sequence:
                return 1 if candidate_sequence > current_sequence else -1
        if same_evidence_domain and current_sequence != candidate_sequence:
            if candidate_sequence < 0:
                return -1
            if current_sequence < 0:
                return 1
            return 1 if candidate_sequence > current_sequence else -1
        return 0

    @staticmethod
    def _prefer_rollout_turn(
        current: TurnRecord | None,
        candidate: TurnRecord,
    ) -> TurnRecord:
        """以显式时间稳定保留 rollout 游标中的最新终态。

        rollout 是追加文件，但追加顺序不等于业务时间顺序。已有带时间记录
        不能被后追加的旧记录或无时间记录覆盖；同一 turn 交给
        ``_newer_turn`` 处理状态/结果完整度，跨 turn 的同时间候选则保留
        先观察到的记录，避免在重读和增量读取之间来回跳变。
        """

        if current is None:
            return candidate
        if current.turn_id == candidate.turn_id:
            merged = CodexStore._newer_turn(current, candidate)
            # current 非空且 candidate 为同一 turn，因此这里不会返回 None。
            return merged if merged is not None else current
        return (
            candidate
            if CodexStore._compare_turn_evidence(current, candidate) > 0
            else current
        )

    def _read_rollout_latest_completed_result(
        self, thread: ThreadRecord
    ) -> TurnRecord | None:
        """读取 rollout 中最新一轮带最终答复的显式完成事件。"""

        path = self._validated_rollout_path(thread)
        if path is None:
            return None
        # 复用同一个增量游标推进文件；该调用返回的是最新任意终态，而这里
        # 从游标读取最新的 completed+result，避免后续失败/中断遮住上一条结果。
        self._read_rollout_latest_turn(thread)
        key = (self._comparison_path(path), thread.thread_id)
        with self._rollout_lock:
            cursor = self._rollout_cursors.get(key)
            return cursor.latest_completed_result if cursor is not None else None

    @staticmethod
    def _newer_turn(
        history_turn: TurnRecord | None,
        rollout_turn: TurnRecord | None,
    ) -> TurnRecord | None:
        """按显式轮次时间和同轮完整度合并 history 与 rollout。

        history 是结构化投影的首选来源，但它可能暂时落后于 append-only
        rollout。相同 ``turn_id`` 不能只看来源优先级：例如 history 仍是
        ``inProgress``、rollout 已写入 ``task_complete`` 时，必须让已结束的
        轮次胜出；反之若 history 已有最终答复/生成图片，而 rollout 只有终态
        标记，则保留 history 的更完整结构化结果。``get_turn`` 不经过此方法，
        因而其冻结的精确 history 优先语义不变。
        """

        if rollout_turn is None:
            return history_turn
        if history_turn is None:
            return rollout_turn
        if history_turn.turn_id == rollout_turn.turn_id:
            terminal_statuses = {
                ThreadStatus.COMPLETED,
                ThreadStatus.FAILED,
                ThreadStatus.INTERRUPTED,
                ThreadStatus.CANCELLED,
            }
            history_terminal = history_turn.status in terminal_statuses
            rollout_terminal = rollout_turn.status in terminal_statuses
            evidence = CodexStore._compare_turn_evidence(
                history_turn, rollout_turn
            )
            if history_terminal != rollout_terminal:
                # A newer explicit event wins even if its state is a terminal
                # correction of an older terminal projection.  If no ordering
                # evidence exists, terminal remains the conservative choice.
                if evidence > 0:
                    return rollout_turn
                if evidence < 0:
                    return history_turn
                return history_turn if history_terminal else rollout_turn
            if history_terminal and rollout_terminal and (
                history_turn.status is not rollout_turn.status
            ):
                # Different terminal states must never be selected by a
                # status ranking.  Explicit time or strict event sequence is
                # required; an equal/unknown pair stays on history.
                if evidence > 0:
                    return rollout_turn
                if evidence < 0:
                    return history_turn
                return history_turn
            # Same-state records can carry a newer final answer.  Prefer the
            # newer source when time/order says so, while still filling fields
            # absent from that source from the older structured projection.
            if evidence > 0:
                return CodexStore._merge_same_turn(
                    history_turn, rollout_turn, preferred=rollout_turn
                )
            if evidence < 0:
                return CodexStore._merge_same_turn(
                    history_turn, rollout_turn, preferred=history_turn
                )
            return CodexStore._merge_same_turn(history_turn, rollout_turn)
        return (
            rollout_turn
            if CodexStore._compare_turn_evidence(history_turn, rollout_turn) > 0
            else history_turn
        )

    @staticmethod
    def _turn_completeness(turn: TurnRecord) -> tuple[int, ...]:
        """返回只由结构化字段组成的同一轮完整度排序键。"""

        status_rank = {
            ThreadStatus.UNKNOWN: 0,
            ThreadStatus.IN_PROGRESS: 1,
            ThreadStatus.CANCELLED: 2,
            ThreadStatus.INTERRUPTED: 2,
            ThreadStatus.FAILED: 2,
            ThreadStatus.COMPLETED: 3,
        }.get(turn.status, 0)
        return (
            status_rank,
            1 if turn.final_message.strip() else 0,
            len(turn.generated_images),
            1 if turn.final_agent_item_id.strip() else 0,
            1 if turn.completed_at is not None else 0,
            1 if turn.started_at is not None else 0,
            1 if turn.duration_ms is not None else 0,
            1 if turn.error_json else 0,
        )

    @staticmethod
    def _merge_same_turn(
        history_turn: TurnRecord,
        rollout_turn: TurnRecord,
        *,
        preferred: TurnRecord | None = None,
    ) -> TurnRecord:
        """安全合并同一 turn 的两份投影，优先保留更完整来源。

        终态、错误和 raw 只来自胜出的来源，避免把不同状态拼成矛盾记录；
        只有胜出来源缺失的稳定结果字段/时间字段才从另一份投影补齐。这样
        rollout 的完成事件可以推进滞后的 history，而 history 的 final item
        与图片投影也不会被 rollout 的简化记录覆盖。
        """

        if preferred is history_turn:
            primary, secondary = history_turn, rollout_turn
        elif preferred is rollout_turn:
            primary, secondary = rollout_turn, history_turn
        else:
            history_quality = CodexStore._turn_completeness(history_turn)
            rollout_quality = CodexStore._turn_completeness(rollout_turn)
            primary, secondary = (
                (history_turn, rollout_turn)
                if history_quality >= rollout_quality
                else (rollout_turn, history_turn)
            )
        material_parts = primary.final_answer_parts
        if (not material_parts and secondary.final_answer_parts
                and primary.final_message == secondary.final_answer_parts[-1]):
            material_parts = secondary.final_answer_parts
        return TurnRecord(
            thread_id=primary.thread_id,
            turn_id=primary.turn_id,
            status=primary.status,
            rollout_ordinal=(
                primary.rollout_ordinal
                if primary.rollout_ordinal is not None
                else secondary.rollout_ordinal
            ),
            started_at=(
                primary.started_at
                if primary.started_at is not None
                else secondary.started_at
            ),
            completed_at=(
                primary.completed_at
                if primary.completed_at is not None
                else secondary.completed_at
            ),
            duration_ms=(
                primary.duration_ms
                if primary.duration_ms is not None
                else secondary.duration_ms
            ),
            error_json=primary.error_json,
            final_agent_item_id=(
                primary.final_agent_item_id
                if primary.final_agent_item_id.strip()
                else secondary.final_agent_item_id
            ),
            final_answer_parts=material_parts,
            final_message=(
                CodexStore._answer_material(material_parts) if material_parts else
                primary.final_message
                if primary.final_message.strip()
                else secondary.final_message
            ),
            generated_images=primary.generated_images or secondary.generated_images,
            delivered_files=primary.delivered_files or secondary.delivered_files,
            raw=primary.raw,
        )

    def _read_rollout_turn_exact(
        self,
        thread: ThreadRecord,
        turn_id: str,
    ) -> TurnRecord | None:
        """从受信 rollout 精确恢复一轮显式终态。

        该路径只服务已经冻结 ``thread_id + turn_id`` 的持久通知。它不会使用
        最新一轮、时间、正文或标题做近似回退；读取期间文件变化、末行未写完、
        完整行损坏，或同一 turn 出现多个终态时均保留类型化错误并 fail closed。
        """

        normalized_turn = _as_text(turn_id)
        if not normalized_turn:
            return None
        path = self._validated_rollout_path(thread)
        if path is None:
            if thread.rollout_path.strip():
                self._errors().append("rollout:path")
            return None
        try:
            before = path.stat()
            matches: list[TurnRecord] = []
            shared_path = self._shared_rollout_path(path)
            with path.open("rb") as handle:
                line_number = 0
                while True:
                    raw_line = handle.readline()
                    if not raw_line:
                        break
                    line_number += 1
                    # Codex 可能正在追加最后一行。精确历史恢复不能把半行当成
                    # “目标不存在”，否则会永久冻结错误结论；留给上层退避重试。
                    if not raw_line.endswith(b"\n"):
                        self._errors().append("rollout:partial-line")
                        return None
                    try:
                        item = json.loads(raw_line.decode("utf-8"))
                    except (UnicodeError, json.JSONDecodeError):
                        self._errors().append("rollout:json")
                        return None
                    if not isinstance(item, Mapping) or item.get("type") != "event_msg":
                        continue
                    payload = item.get("payload")
                    if not isinstance(payload, Mapping):
                        continue
                    terminal = self._rollout_terminal_event(
                        thread.thread_id,
                        payload,
                        line_number,
                        item,
                    )
                    if terminal is not None and shared_path and not self._rollout_event_thread_id(
                        payload, item
                    ):
                        self._errors().append("rollout:thread-ambiguous")
                        return None
                    if terminal is not None and terminal.turn_id == normalized_turn:
                        matches.append(terminal)
            after = path.stat()
        except OSError:
            self._errors().append("rollout:read")
            return None

        before_identity = (
            int(before.st_dev),
            int(before.st_ino),
            int(before.st_size),
            int(before.st_mtime_ns),
        )
        after_identity = (
            int(after.st_dev),
            int(after.st_ino),
            int(after.st_size),
            int(after.st_mtime_ns),
        )
        if after_identity != before_identity:
            self._errors().append("rollout:changed-during-read")
            return None
        if len(matches) > 1:
            self._errors().append("rollout:turn-ambiguous")
            return None
        return matches[0] if matches else None

    def select_threads(
        self,
        *,
        thread_id: str | None = None,
        title: str | None = None,
        cwd: str | None = None,
        include_archived: bool = False,
    ) -> list[ThreadRecord]:
        """按 id、标题、cwd 做精确选择；多个条件同时提供时取交集。"""

        self._begin_query()
        records, _available = self._read_threads(prepare_rollout_ownership=False)
        result: list[ThreadRecord] = []
        for record in records:
            if not include_archived and record.archived:
                continue
            # 这里刻意使用 ``==``，不使用 LIKE、contains、前缀或正则。
            if thread_id is not None and record.thread_id != thread_id:
                continue
            if title is not None and record.title != title:
                continue
            if cwd is not None and record.cwd != cwd:
                continue
            result.append(record)
        result.sort(
            key=lambda record: (
                _time_key(record.updated_at_ms),
                record.thread_id,
            ),
            reverse=True,
        )
        return result

    # 下列别名让监控层可以使用更自然的命名，同时保持同一套精确语义。
    find_threads = select_threads
    find = select_threads
    select = select_threads
    query_threads = select_threads

    def get_thread(
        self, thread_id: str, *, include_archived: bool = True
    ) -> ThreadRecord | None:
        matches = self.select_threads(
            thread_id=thread_id, include_archived=include_archived
        )
        return matches[0] if matches else None

    def latest_turn(self, thread_id: str) -> TurnRecord | None:
        """读取指定 thread 的最新一轮，不从文本内容推断状态。"""

        self._begin_query()
        turns, _available = self._read_turns(thread_id)
        history_latest = turns[0] if turns else None
        threads, _state_available = self._read_threads()
        thread = next((item for item in threads if item.thread_id == thread_id), None)
        rollout_latest = self._read_rollout_latest_turn(thread) if thread is not None else None
        return self._newer_turn(history_latest, rollout_latest)

    def get_turn(self, thread_id: str, turn_id: str) -> TurnRecord | None:
        """按不可变 thread/turn 身份读取一轮，供持久通知恢复精确重建。"""

        normalized_thread = str(thread_id or "").strip()
        normalized_turn = str(turn_id or "").strip()
        if not normalized_thread or not normalized_turn:
            return None
        self._begin_query()
        turns, _available = self._read_turns(
            normalized_thread,
            project_all_results=True,
        )
        # history 本身出现任何读取/投影错误时不能用 rollout 掩盖；调用方会
        # 通过 require_readable 进入可重试路径，而不是误报“原文已清理”。
        if self._errors():
            return None
        exact = next(
            (item for item in turns if item.turn_id == normalized_turn),
            None,
        )
        if exact is not None:
            return exact

        # Codex 的历史投影可能清理较早 turn，而 state 仍保留该 thread 的受信
        # rollout_path。只在精确 history 缺项且读取健康时启用 exact-turn 回退。
        threads, _state_available = self._read_threads()
        if self._errors():
            return None
        matching_threads = [
            item for item in threads if item.thread_id == normalized_thread
        ]
        if len(matching_threads) > 1:
            self._errors().append("state:thread-ambiguous")
            return None
        if not matching_threads:
            return None
        return self._read_rollout_turn_exact(matching_threads[0], normalized_turn)

    @staticmethod
    def _notification_payload_turn_id(
        item: Mapping[str, Any], payload: Mapping[str, Any]
    ) -> str:
        """Read an explicit rollout turn id without guessing from text."""

        for source in (payload, item):
            for key in ("turn_id", "turnId", "turn-id"):
                value = source.get(key)
                if value is not None and str(value).strip():
                    return str(value).strip()
        return ""

    @staticmethod
    def _notification_payload_text(value: object) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, Mapping):
            for key in ("text", "input_text", "output_text", "message"):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate
            return ""
        if not isinstance(value, list):
            return ""
        parts: list[str] = []
        for item in value:
            if not isinstance(item, Mapping):
                continue
            for key in ("text", "input_text", "output_text"):
                candidate = item.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    parts.append(candidate)
                    break
        return "\n".join(parts)

    @staticmethod
    def _notification_context_text(value: object) -> str:
        text = CodexStore._notification_payload_text(value).replace("\x00", "").strip()
        # Attached runtime/governance metadata is not the user's request.
        for tag in ('recommended_plugins', 'environment_context', 'INSTRUCTIONS'):
            text = re.sub(r'<'+tag+r'\b[^>]*>.*?</'+tag+r'>', '', text, flags=re.S)
        if text.lstrip().startswith('# AGENTS.md instructions'):
            marker = re.search(r'^#+\s*(?:My request|用户请求)\s*:', text, re.M)
            text = text[marker.end():] if marker else ''
        text = "\n".join(" ".join(line.split()) for line in text.splitlines())
        if len(text) > _NOTIFICATION_REQUEST_MAX_CHARS:
            text = text[: _NOTIFICATION_REQUEST_MAX_CHARS - 1].rstrip() + "…"
        return text

    def _exact_notification_request(
        self, thread_id: str, turn_id: str, verified_reply_texts: tuple[str, ...]
    ) -> tuple[str, str]:
        """Read exact history items; tool-shaped input additionally needs local delivery proof."""
        connection = self._open(self.paths.history_db, 'history')
        if connection is None:
            return '', ''
        try:
            columns = self._table_columns(connection, 'thread_turns')
            items = self._table_columns(connection, 'thread_items')
            if not {'thread_id','turn_id','item_id','item_json','item_type','rollout_ordinal'} <= items:
                return '', ''
            first = None
            if {'thread_id','turn_id','first_user_item_id'} <= columns:
                row=connection.execute('SELECT first_user_item_id FROM thread_turns WHERE thread_id=? AND turn_id=?',(thread_id,turn_id)).fetchone()
                first=row[0] if row else None
            requests=[]
            alignments=set()
            if first:
                row=connection.execute('SELECT item_type,item_json,rollout_ordinal FROM thread_items WHERE thread_id=? AND turn_id=? AND item_id=?',(thread_id,turn_id,first)).fetchone()
                if row and row[0]=='userMessage':
                    try:value=json.loads(row[1])
                    except (TypeError,json.JSONDecodeError):value=None
                    if (isinstance(value,Mapping) and value.get('type')=='userMessage'
                            and value.get('role') in (None,'user') and value.get('id',first)==first):
                        user_rows=connection.execute("SELECT item_id,item_json,rollout_ordinal FROM thread_items WHERE thread_id=? AND turn_id=? AND item_type='userMessage' AND rollout_ordinal>=? ORDER BY rollout_ordinal LIMIT 129",(thread_id,turn_id,row[2])).fetchall() if isinstance(row[2],int) else [(first,row[1],-1)]
                        if len(user_rows)>128:return '', ''
                        for uid,raw,ordinal in user_rows:
                            try:user=json.loads(raw)
                            except (TypeError,json.JSONDecodeError):continue
                            if not isinstance(user,Mapping) or user.get('type')!='userMessage' or user.get('id',uid)!=uid or user.get('role') not in (None,'user'):
                                continue
                            if any(user.get(k) not in (None,expected) for k,expected in (('thread_id',thread_id),('threadId',thread_id),('turn_id',turn_id),('turnId',turn_id))):
                                continue
                            text=self._notification_context_text(user.get('content'))
                            if text:
                                requests.append((ordinal,text))
                                alignments.add('history_user_item')
            rows=connection.execute("SELECT item_id,item_json,rollout_ordinal FROM thread_items WHERE thread_id=? AND turn_id=? AND item_type='functionCallOutput' ORDER BY rollout_ordinal LIMIT 129",(thread_id,turn_id)).fetchall() if verified_reply_texts else []
            if len(rows)>128:return '', ''
            for iid,raw,ordinal in rows:
                try:value=json.loads(raw)
                except (TypeError,json.JSONDecodeError):continue
                if not isinstance(value,Mapping) or value.get('type')!='functionCallOutput' or value.get('id',iid)!=iid:
                    continue
                if value.get('namespace')!='codex_app' or value.get('name')!='send_message_to_thread':
                    continue
                output=value.get('output')
                if not isinstance(output,str) or len(output)>64000 or '<!' in output:
                    continue
                try:envelope=ET.fromstring(output)
                except ET.ParseError:continue
                if (envelope.tag!='codex_delegation' or envelope.attrib
                        or [child.tag for child in envelope]!=['source_thread_id','input']):
                    continue
                source,input_node=list(envelope)
                if source.text!=thread_id or source.attrib or input_node.attrib or len(source) or len(input_node):
                    continue
                raw_request=(input_node.text or '').strip()
                if raw_request not in verified_reply_texts:
                    continue
                text=self._notification_context_text(raw_request)
                if text and isinstance(ordinal,int):
                    requests.append((ordinal,text))
                    alignments.add('reply_verified')
            if not requests:return '', ''
            requests.sort(key=lambda pair:pair[0])
            budget=max(1,(_NOTIFICATION_REQUEST_MAX_CHARS-len(requests))//len(requests))
            bodies=[text if len(text)<=budget else text[:max(0,budget-1)]+'…' for _,text in requests]
            return '\n'.join(bodies), '+'.join(sorted(alignments))
        except (OSError,sqlite3.Error):
            self._errors().append('history:notification-request-read')
            return '', ''
        finally:
            connection.close()

    def notification_context(
        self, thread_id: str, turn_id: str, *, verified_reply_texts: tuple[str, ...] = ()
    ) -> NotificationContext:
        """Read exact-turn user request and verifiable task state.

        Only the trusted rollout path already associated with the exact thread
        is scanned, within a byte/line bound.  User messages are selected by
        their position before the requested turn's explicit terminal/event
        marker; no title or assistant text is used as a request guess.
        Missing or ambiguous evidence yields an empty request and a state
        marker so the policy model can remain conservative.
        """

        normalized_thread = str(thread_id or "").strip()
        normalized_turn = str(turn_id or "").strip()
        if not normalized_thread or not normalized_turn:
            return NotificationContext(task_state="exact_turn_identity_missing")
        target = self.get_turn(normalized_thread, normalized_turn)
        thread = self.get_thread(normalized_thread, include_archived=True)
        target_status = target.status.value if target is not None else "unknown"
        latest = self.latest_turn(normalized_thread)
        latest_status = latest.status.value if latest is not None else "unknown"
        archived = bool(thread.archived) if thread is not None else False
        state_parts = (
            f"turn_status={target_status}",
            f"latest_status={latest_status}",
            f"archived={'true' if archived else 'false'}",
            f"exact_turn={'true' if target is not None else 'false'}",
        )
        if target is not None:
            request, alignment = self._exact_notification_request(normalized_thread, normalized_turn, verified_reply_texts)
            if request:
                return NotificationContext(user_request=request,task_state='; '.join((*state_parts,'request_alignment='+alignment)))
        if thread is None:
            return NotificationContext(task_state="; ".join(state_parts))
        path = self._validated_rollout_path(thread)
        if path is None:
            return NotificationContext(task_state="; ".join(state_parts))

        user_messages: list[tuple[int, str, str]] = []
        target_line: int | None = None
        read_bytes = 0
        truncated = False
        try:
            with path.open("rb") as handle:
                for line_number, raw_line in enumerate(handle, start=1):
                    if line_number > _NOTIFICATION_ROLLOUT_MAX_LINES:
                        truncated = True
                        break
                    read_bytes += len(raw_line)
                    if read_bytes > _NOTIFICATION_ROLLOUT_MAX_BYTES:
                        truncated = True
                        break
                    try:
                        item = json.loads(raw_line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if not isinstance(item, Mapping):
                        continue
                    payload = item.get("payload")
                    if not isinstance(payload, Mapping):
                        continue
                    item_turn = self._notification_payload_turn_id(item, payload)
                    if item_turn == normalized_turn:
                        target_line = line_number
                    envelope = str(item.get("type") or "")
                    payload_type = str(payload.get("type") or "")
                    if envelope == "event_msg" and payload_type == "user_message":
                        text = self._notification_context_text(payload.get("message"))
                        if text:
                            user_messages.append((line_number, item_turn, text))
                    elif envelope == "response_item" and payload_type == "message":
                        if str(payload.get("role") or "") != "user":
                            continue
                        text = self._notification_context_text(
                            payload.get("content")
                        )
                        if text:
                            user_messages.append((line_number, item_turn, text))
        except OSError:
            self._errors().append("rollout:notification-context-read")
            return NotificationContext(task_state="; ".join(state_parts))

        eligible = [
            item for item in user_messages
            if target_line is None or item[0] <= target_line
        ]
        # An explicit turn id on a user event is stronger than positional
        # fallback and prevents a later continuation from becoming the request.
        exact = [item for item in eligible if item[1] == normalized_turn]
        if exact:
            request = exact[-1][2]
        elif not truncated and target_line is None and len(user_messages) == 1:
            # A single user message is an unambiguous legacy rollout shape;
            # multiple messages without a target marker are not safe to guess.
            request = user_messages[0][2]
        else:
            request = ""
        if truncated:
            state_parts += ('request_scan=truncated',)
        if target_line is None and request:
            state_parts += ("request_alignment=positional",)
        elif request:
            state_parts += ("request_alignment=exact_or_ordered",)
        else:
            state_parts += ("request_alignment=missing",)
        return NotificationContext(
            user_request=request,
            task_state="; ".join(state_parts),
        )

    def latest_terminal_turn(self, thread_id: str) -> TurnRecord | None:
        """读取最后一个已经结束的 turn；活动中的新 turn 不会遮住它。"""

        self._begin_query()
        turns, _available = self._read_turns(thread_id)
        history_terminal = None
        for turn in turns:
            if turn.status in {
                ThreadStatus.COMPLETED,
                ThreadStatus.FAILED,
                ThreadStatus.INTERRUPTED,
                ThreadStatus.CANCELLED,
            }:
                history_terminal = turn
                break
        threads, _state_available = self._read_threads()
        thread = next((item for item in threads if item.thread_id == thread_id), None)
        rollout_terminal = self._read_rollout_latest_turn(thread) if thread is not None else None
        if rollout_terminal is not None and rollout_terminal.status not in {
            ThreadStatus.COMPLETED,
            ThreadStatus.FAILED,
            ThreadStatus.INTERRUPTED,
            ThreadStatus.CANCELLED,
        }:
            rollout_terminal = None
        return self._newer_turn(history_terminal, rollout_terminal)

    def latest_completed_result_turn(self, thread_id: str) -> TurnRecord | None:
        """读取最后一个真正完成且产出可展示结果的 turn。

        搜索结果的“最后活动时间”必须对应产生最后结果的 completedAt；失败、
        中断或没有最终答复的空完成轮次不能冒充这项时间。
        """

        self._begin_query()
        turns, _available = self._read_turns(thread_id, project_all_results=True)
        history_completed = None
        for turn in turns:
            if turn.status is ThreadStatus.COMPLETED and (
                turn.final_message.strip() or turn.generated_images
            ):
                history_completed = turn
                break
        threads, _state_available = self._read_threads()
        thread = next((item for item in threads if item.thread_id == thread_id), None)
        rollout_completed = (
            self._read_rollout_latest_completed_result(thread)
            if thread is not None
            else None
        )
        return self._newer_turn(history_completed, rollout_completed)

    get_latest_turn = latest_turn
    get_latest_terminal_turn = latest_terminal_turn
    get_latest_completed_result_turn = latest_completed_result_turn

    def snapshot(
        self, thread_id: str, *, include_archived: bool = True
    ) -> ThreadSnapshot:
        """合并线程元数据、历史库与旧任务 rollout 的最新结构化状态。"""

        self._begin_query()
        threads, state_available = self._read_threads()
        thread = next(
            (
                item
                for item in threads
                if item.thread_id == thread_id
                and (include_archived or not item.archived)
            ),
            None,
        )
        turns, history_available = self._read_turns(thread_id)
        latest = turns[0] if turns else None
        # ``threads.updated_at_ms`` 是目录元数据，不是 rollout 游标的可靠
        # 水位：历史投影和目录更新时间可能相同、落后，或只相差不到两秒。
        # 对有受信 rollout_path 的线程始终推进同一个增量游标；无变化时只做
        # stat/offset 检查，不会重复扫描文件。统一交给 _newer_turn 按显式
        # turn 时间及同轮完整度合并，不能因为目录时间门槛漏掉新终态。
        rollout_latest = (
            self._read_rollout_latest_turn(thread) if thread is not None else None
        )
        latest = self._newer_turn(latest, rollout_latest)
        status = latest.status if latest is not None else ThreadStatus.UNKNOWN
        return ThreadSnapshot(
            thread=thread,
            latest_turn=latest,
            status=status,
            state_available=state_available,
            history_available=history_available,
            errors=tuple(self._errors()),
        )

    get_snapshot = snapshot
    read_snapshot = snapshot

    def status(self, thread_id: str) -> ThreadStatus:
        """返回最新轮次的显式状态；无数据时返回 ``unknown``。"""

        return self.snapshot(thread_id).require_readable().status

    get_status = status


__all__ = [
    "CodexStore",
    "CodexStoreReadError",
    "read_generated_image_bytes",
    "StorePaths",
    "ThreadRecord",
    "ThreadSnapshot",
    "ThreadStatus",
    "TurnRecord",
]
