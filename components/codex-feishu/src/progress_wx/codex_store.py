"""Codex 本地结构化状态的只读访问层。

本模块优先读取 Codex 自己维护的两个 SQLite 数据库，不打开写事务，也不解析
助手文本来猜测状态。旧任务缺少历史投影时，只在 CODEX_HOME/sessions 边界内
增量读取 rollout 的显式 task_complete/turn_aborted 事件。线程选择只使用 thread
id、标题和工作目录的精确相等。由于 Codex 的内部 schema 可能随版本演进，查询
前会检查表和列；数据库不存在、损坏或暂时不可读时保留类型化错误，由上层进入
有限重试和停机，不会静默伪装成正常 ``unknown``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any, Iterable, Mapping

from .models import GeneratedImageArtifact


_GENERATED_IMAGE_MAX_BYTES = 30 * 1024 * 1024
_GENERATED_IMAGE_FORMATS = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def read_generated_image_bytes(artifact: GeneratedImageArtifact) -> bytes:
    """发送前重读并核对摘要，关闭提取与上传之间的替换窗口。"""

    path = Path(artifact.path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ValueError("生成图片原文件已不可读") from exc
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
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

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
    latest_turn: TurnRecord | None = None


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


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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
        self._last_errors: list[str] = []
        self._rollout_cursors: dict[str, _RolloutCursor] = {}
        self._rollout_lock = threading.RLock()

    @property
    def last_errors(self) -> tuple[str, ...]:
        """最近一次查询遇到的类型化错误，不包含 SQL 或用户内容。"""

        return tuple(self._last_errors)

    def require_readable(self, operation: str = "读取 Codex 状态") -> None:
        """显式抛出最近一次查询错误；正常空结果和 unknown 不会触发。"""

        if self._last_errors:
            raise CodexStoreReadError(operation, self._last_errors)

    def _begin_query(self) -> None:
        self._last_errors = []

    def _open(self, path: Path, label: str) -> sqlite3.Connection | None:
        if not path.is_file():
            self._last_errors.append(f"{label}:missing")
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
            self._last_errors.append(f"{label}:unavailable")
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

    def _read_threads(self) -> tuple[list[ThreadRecord], bool]:
        connection = self._open(self.paths.state_db, "state")
        if connection is None:
            return [], False
        try:
            table_info = self._find_table(connection, ("threads", "thread"))
            if table_info is None:
                self._last_errors.append("state:schema")
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
                self._last_errors.append("state:thread-id-column")
                return [], True
            quoted = ", ".join(f'"{column}"' for column in selected)
            rows = connection.execute(f'SELECT {quoted} FROM "{table}"').fetchall()
            # Codex Desktop 侧栏的短标题以 session_index.thread_name 为准。
            # 旧任务在 SQLite 中可能没有 name，此时 title 是整段首条提示词。
            session_names = self._read_session_names()
            result: list[ThreadRecord] = []
            for row in rows:
                raw = dict(row)
                thread_id = _as_text(_pick_value(raw, "id", "thread_id", "threadId"))
                if not thread_id:
                    continue
                title = (
                    session_names.get(thread_id, "")
                    or _as_text(_pick_value(raw, "name"))
                    or _as_text(_pick_value(raw, "title"))
                    or _as_text(_pick_value(raw, "preview"))
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
                    )
                )
            return result, True
        except (OSError, sqlite3.Error):
            self._last_errors.append("state:read")
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

    def _read_final_message(
        self,
        connection: sqlite3.Connection,
        turn: TurnRecord,
    ) -> str:
        """按 Codex 的最终 item 指针读取 assistant 最终答复。

        这里刻意不扫描文本、不寻找“最后一条消息”，也不读取 rollout 或大日志。
        旧版本没有 ``thread_items`` 或相关列时返回空字符串；精确 SQL 读取失败
        则记录类型化错误，使外层服务停止并告警。
        """

        item_id = turn.final_agent_item_id.strip()
        if turn.status not in {
            ThreadStatus.COMPLETED,
            ThreadStatus.FAILED,
            ThreadStatus.INTERRUPTED,
        } or not item_id:
            return ""
        table_info = self._find_table(connection, ("thread_items",))
        if table_info is None:
            # 旧 Codex schema 没有投影表：兼容为空，不把它伪装成读取错误。
            return ""
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
            return ""
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
            self._last_errors.append("history:final-message-read")
            return ""
        if row is None:
            return ""
        item_type = _as_text(row[0])
        if item_type != "agentMessage":
            return ""
        raw_json = row[1]
        if not isinstance(raw_json, str) or not raw_json.strip():
            return ""
        try:
            item = json.loads(raw_json)
        except (TypeError, json.JSONDecodeError):
            return ""
        if not isinstance(item, Mapping):
            return ""
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
            return ""
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            return ""
        return text.strip()

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
            self._last_errors.append("history:generated-image-read")
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

    def _read_turns(self, thread_id: str) -> tuple[list[TurnRecord], bool]:
        connection = self._open(self.paths.history_db, "history")
        if connection is None:
            return [], False
        try:
            table_info = self._find_table(
                connection, ("thread_turns", "turns", "thread_status")
            )
            if table_info is None:
                self._last_errors.append("history:schema")
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
                self._last_errors.append("history:turn-columns")
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
            if result:
                latest = result[0]
                final_message = self._read_final_message(connection, latest)
                generated_images = self._read_generated_images(connection, latest)
                if final_message or generated_images:
                    result[0] = TurnRecord(
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
                        generated_images=generated_images,
                        raw=latest.raw,
                    )
            return result, True
        except (OSError, sqlite3.Error):
            self._last_errors.append("history:read")
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

    @staticmethod
    def _rollout_terminal_event(
        thread_id: str,
        payload: Mapping[str, Any],
        line_number: int,
    ) -> TurnRecord | None:
        """把 Codex 显式终态事件映射为 TurnRecord；不检查任何正文关键词。"""

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
        return TurnRecord(
            thread_id=thread_id,
            turn_id=turn_id,
            status=status,
            rollout_ordinal=line_number,
            started_at=_as_int(_pick_value(payload, "started_at", "startedAt")),
            completed_at=_as_int(_pick_value(payload, "completed_at", "completedAt")),
            duration_ms=_as_int(_pick_value(payload, "duration_ms", "durationMs")),
            final_message=final_message,
            raw={"source": "codex-rollout", "event_type": event_type},
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
        key = self._comparison_path(path)
        identity = (int(file_stat.st_dev), int(file_stat.st_ino))
        with self._rollout_lock:
            cursor = self._rollout_cursors.get(key)
            rewritten_same_size = bool(
                cursor is not None
                and cursor.mtime_ns != int(file_stat.st_mtime_ns)
                and int(file_stat.st_size) == cursor.offset
            )
            if (
                cursor is None
                or cursor.identity != identity
                or int(file_stat.st_size) < cursor.offset
                or rewritten_same_size
            ):
                cursor = _RolloutCursor(identity=identity)
                self._rollout_cursors[key] = cursor
            if int(file_stat.st_size) == cursor.offset:
                cursor.mtime_ns = int(file_stat.st_mtime_ns)
                return cursor.latest_turn
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
                            thread.thread_id, payload, cursor.line_number
                        )
                        if terminal is not None:
                            cursor.latest_turn = terminal
            except OSError:
                return cursor.latest_turn
            cursor.mtime_ns = int(file_stat.st_mtime_ns)
            return cursor.latest_turn

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
        records, _available = self._read_threads()
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
        return turns[0] if turns else None

    get_latest_turn = latest_turn

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
        history_time = _time_key(latest.completed_at or latest.started_at) if latest else -1
        thread_time = _time_key(thread.updated_at_ms) if thread is not None else -1
        needs_rollout = bool(
            thread is not None
            and (latest is None or thread_time > history_time + 2_000)
        )
        rollout_latest = self._read_rollout_latest_turn(thread) if needs_rollout else None
        if rollout_latest is not None and (
            latest is None
            or _time_key(rollout_latest.completed_at or rollout_latest.started_at)
            > history_time
        ):
            latest = rollout_latest
        status = latest.status if latest is not None else ThreadStatus.UNKNOWN
        return ThreadSnapshot(
            thread=thread,
            latest_turn=latest,
            status=status,
            state_available=state_available,
            history_available=history_available,
            errors=tuple(self._last_errors),
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
