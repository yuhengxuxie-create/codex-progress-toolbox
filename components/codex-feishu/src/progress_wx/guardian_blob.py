"""A bounded, private and content-addressed store for guardian media blobs.

The guardian and worker can access this directory at the same time.  Every
operation therefore takes both the in-process reentrant lock and a small
cross-process file lock before inspecting quota or changing a blob.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import threading
import time
from typing import Iterable, Iterator, Mapping
import uuid

from .guardian_store import private_directory


# Feishu's official file upload API says "not over 30 MB" and rejects empty
# files.  The API uses decimal MB in its published limit, so this is exactly
# 30,000,000 bytes rather than an unverified MiB conversion.
MAX_BLOB_SIZE = 30 * 1000 * 1000
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_BLOB_FILES = 128
BLOB_RETENTION_SECONDS = 24 * 60 * 60
_READ_CHUNK_SIZE = 1024 * 1024
_BLOB_NAME = re.compile(r"^[0-9a-f]{64}\.blob$")
_TEMP_NAME = re.compile(r"^\.guardian-blob-[0-9a-f-]+\.pending$")
_LOCK_NAME = ".guardian-blobs.lock"
_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.RLock] = {}


class GuardianBlobPayloadRejectedError(ValueError):
    """A media payload is empty or outside the documented file boundary."""

    def __init__(self, *, reason: str, size: int, limit: int) -> None:
        self.reason = str(reason)
        self.size = int(size)
        self.limit = int(limit)
        if self.reason == "empty":
            message = "guardian blob cannot be empty"
        else:
            message = "guardian blob exceeds single-file limit"
        super().__init__(message)


def _validate_payload_size(size: int) -> None:
    if size == 0:
        raise GuardianBlobPayloadRejectedError(
            reason="empty",
            size=size,
            limit=MAX_BLOB_SIZE,
        )
    if size > MAX_BLOB_SIZE:
        raise GuardianBlobPayloadRejectedError(
            reason="too_large",
            size=size,
            limit=MAX_BLOB_SIZE,
        )


def _process_lock_for(path: Path) -> threading.RLock:
    """Return one shared lock for all instances using the same lock file."""

    key = os.path.normcase(str(path.absolute()))
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PROCESS_LOCKS[key] = lock
        return lock


def _open_lock_handle(path: Path):
    """Open a lock file and atomically make its first byte available.

    ``msvcrt.locking`` cannot lock an empty file.  A normal text-mode
    ``write``/``flush`` sequence is also racy when independent instances first
    create the same file on Windows.  ``O_APPEND`` makes the one-byte
    initialization safe even if another process reaches this branch at the
    same time; duplicate bytes are harmless because only byte zero is locked.
    """

    flags = os.O_RDWR | os.O_CREAT | os.O_APPEND
    flags |= int(getattr(os, "O_BINARY", 0))
    fd = os.open(str(path), flags, 0o600)
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        return os.fdopen(fd, "r+b", closefd=True)
    except BaseException:
        os.close(fd)
        raise


def _is_reparse(path: Path) -> bool:
    """Return whether *path* is a symlink or an OS reparse point."""

    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
        stat = path.lstat()
    except FileNotFoundError:
        # A not-yet-created target is safe to inspect further; callers still
        # validate its parent/root before opening or replacing it.
        return False
    except OSError:
        # A path that cannot be inspected is unsafe to follow.
        return True
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(int(getattr(stat, "st_file_attributes", 0)) & reparse_flag)


class GuardianBlobs:
    """Bounded private content-addressed media storage.

    ``root`` is the private media directory itself.  References returned by
    :meth:`put` contain only a safe filename, digest and byte count, so they
    can be stored in the guardian's JSON/SQLite payload without exposing a
    local path.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root).absolute()
        private_directory(self.root)
        self._thread_lock = threading.RLock()
        self._lock_path = self.root / _LOCK_NAME

    @staticmethod
    def _normalise_names(values: Iterable[object] | None) -> set[str]:
        if values is None:
            return set()
        if isinstance(values, (str, bytes)):
            values = (values,)
        names: set[str] = set()
        for value in values:
            if isinstance(value, Mapping):
                value = value.get("name")
            if isinstance(value, str):
                names.add(value)
        return names

    def _check_root(self) -> Path:
        root = self.root
        if _is_reparse(root) or not root.is_dir():
            raise ValueError("guardian blob root is not a private directory")
        for parent in root.parents:
            if _is_reparse(parent):
                raise ValueError("guardian blob root contains a reparse path")
        return root.resolve()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Take the process lock while holding the same-process RLock."""

        with self._thread_lock, _process_lock_for(self._lock_path):
            self._check_root()
            if _is_reparse(self._lock_path) or (self._lock_path.exists() and not self._lock_path.is_file()):
                raise ValueError("guardian blob lock is not a regular private file")
            handle = _open_lock_handle(self._lock_path)
            locked = False
            try:
                if os.name == "nt":
                    import msvcrt

                    # _open_lock_handle guarantees that byte zero exists.
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    locked = True
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    locked = True
                yield
            finally:
                try:
                    if locked and os.name == "nt":
                        import msvcrt

                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    elif locked:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                finally:
                    handle.close()

    def _safe_path(self, name: str) -> Path:
        if not isinstance(name, str) or not _BLOB_NAME.fullmatch(name):
            raise ValueError("invalid guardian blob name")
        root = self._check_root()
        candidate = self.root / name
        if _is_reparse(candidate):
            raise ValueError("guardian blob path is a reparse link")
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(root):
            raise ValueError("guardian blob path escapes its root")
        return candidate

    def _entries_locked(self) -> list[Path]:
        """List private files while failing closed on unexpected children."""

        entries: list[Path] = []
        for entry in sorted(self.root.iterdir(), key=lambda item: item.name):
            if entry.name == _LOCK_NAME:
                continue
            if _is_reparse(entry):
                raise ValueError("guardian blob child is a reparse link")
            if not entry.is_file():
                raise ValueError("guardian blob directory contains a non-file")
            entries.append(entry)
        return entries

    def _usage_locked(self) -> tuple[int, int]:
        entries = self._entries_locked()
        total = 0
        for entry in entries:
            try:
                total += entry.stat().st_size
            except OSError as exc:
                raise ValueError("guardian blob child cannot be inspected") from exc
        return len(entries), total

    @staticmethod
    def _hash_file(path: Path, *, expected_size: int | None = None) -> tuple[int, str]:
        hasher = hashlib.sha256()
        size = 0
        try:
            with path.open("rb") as stream:
                while True:
                    chunk = stream.read(_READ_CHUNK_SIZE)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_BLOB_SIZE:
                        raise ValueError("guardian blob exceeds single-file limit")
                    hasher.update(chunk)
        except OSError as exc:
            raise FileNotFoundError("guardian blob cannot be read") from exc
        _validate_payload_size(size)
        if expected_size is not None and size != expected_size:
            raise ValueError("guardian blob size mismatch")
        return size, hasher.hexdigest()

    @staticmethod
    def _fsync_directory(root: Path) -> None:
        """Persist the directory rename where the platform permits it."""

        try:
            flags = os.O_RDONLY
            if hasattr(os, "O_DIRECTORY"):
                flags |= os.O_DIRECTORY
            fd = os.open(str(root), flags)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            # Windows does not support fsync on directory handles.  The blob
            # itself was fsynced before replace, which is the durable part.
            pass
        finally:
            os.close(fd)

    def put(self, data: bytes | bytearray | memoryview) -> dict[str, object]:
        """Atomically store bytes and return a portable JSON reference."""

        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("guardian blob data must be bytes-like")
        payload = bytes(data)
        size = len(payload)
        _validate_payload_size(size)
        digest = hashlib.sha256(payload).hexdigest()
        name = f"{digest}.blob"
        reference: dict[str, object] = {
            "name": name,
            "sha256": digest,
            "size": size,
        }

        with self._locked():
            target = self._safe_path(name)
            if target.exists() or target.is_symlink():
                if _is_reparse(target) or not target.is_file():
                    raise ValueError("guardian blob target is not a regular file")
                existing_size, existing_digest = self._hash_file(
                    target, expected_size=size
                )
                if existing_size != size or existing_digest != digest:
                    raise ValueError("guardian blob content conflict")
                return reference

            count, total = self._usage_locked()
            # The pending file is included in both limits until os.replace.
            if count + 1 > MAX_BLOB_FILES:
                raise BufferError("guardian blob file limit")
            if total + size > MAX_TOTAL_BYTES:
                raise BufferError("guardian blob byte limit")

            temporary = self.root / (
                f".guardian-blob-{uuid.uuid4().hex}.pending"
            )
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= int(getattr(os, "O_BINARY", 0))
            fd = os.open(str(temporary), flags, 0o600)
            try:
                with os.fdopen(fd, "wb", closefd=True) as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(str(temporary), str(target))
                self._fsync_directory(self.root)
            except BaseException:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
        return reference

    def load(self, reference: Mapping[str, object]) -> bytes:
        """Load one bounded byte buffer and verify it without a joined copy."""

        if not isinstance(reference, Mapping):
            raise ValueError("invalid guardian blob reference")
        name = reference.get("name")
        digest = reference.get("sha256")
        size = reference.get("size")
        if (
            not isinstance(name, str)
            or not isinstance(digest, str)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or size <= 0
            or size > MAX_BLOB_SIZE
            or name != f"{digest}.blob"
        ):
            if isinstance(size, int) and not isinstance(size, bool):
                _validate_payload_size(size)
            raise ValueError("invalid guardian blob reference")

        with self._locked():
            path = self._safe_path(name)
            if not path.exists() or not path.is_file():
                raise FileNotFoundError("guardian blob is missing")
            try:
                with path.open("rb") as stream:
                    data = stream.read(MAX_BLOB_SIZE + 1)
                    actual_size = len(data)
                    if actual_size > MAX_BLOB_SIZE:
                        raise ValueError("guardian blob exceeds single-file limit")
            except OSError as exc:
                raise FileNotFoundError("guardian blob cannot be read") from exc
            actual_digest = hashlib.sha256(data).hexdigest()
            if actual_size != size or actual_digest != digest:
                raise ValueError("guardian blob integrity mismatch")
            return data

    def cleanup(
        self,
        referenced_names: Iterable[object] | None,
        *,
        terminal_names: Iterable[object] | None = None,
    ) -> list[str]:
        """Delete only unreferenced old blobs or explicitly terminal blobs.

        ``referenced_names`` must include every pending/uncertain reference;
        an explicit terminal name is eligible immediately only when it is not
        present in that set.  Unknown filenames are retained for safety.
        """

        referenced = self._normalise_names(referenced_names)
        terminal = self._normalise_names(terminal_names)
        deleted: list[str] = []
        now = time.time()
        with self._locked():
            for path in self._entries_locked():
                if not _BLOB_NAME.fullmatch(path.name) and not _TEMP_NAME.fullmatch(
                    path.name
                ):
                    continue
                if _is_reparse(path):
                    raise ValueError("guardian blob cleanup encountered a reparse link")
                if path.name in referenced:
                    continue
                eligible_now = path.name in terminal and path.name not in referenced
                try:
                    old = now - path.stat().st_mtime >= BLOB_RETENTION_SECONDS
                except OSError:
                    continue
                if not eligible_now and not old:
                    continue
                path.unlink()
                deleted.append(path.name)
            if deleted:
                self._fsync_directory(self.root)
        return deleted


# Keep the descriptive name available to callers that use the media-store
# terminology while the guardian-facing API remains GuardianBlobs.
BoundedMediaStore = GuardianBlobs


__all__ = [
    "BLOB_RETENTION_SECONDS",
    "BoundedMediaStore",
    "GuardianBlobs",
    "GuardianBlobPayloadRejectedError",
    "MAX_BLOB_FILES",
    "MAX_BLOB_SIZE",
    "MAX_TOTAL_BYTES",
]
