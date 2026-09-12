"""Exact local file validation shared by discovery and delivery capture."""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
import stat
from typing import Mapping

IDENTITY_FIELDS = ("st_dev", "st_ino", "st_size", "st_mtime_ns")

def identity(value):
    return {key: int(getattr(value, key)) for key in IDENTITY_FIELDS}

def inspect_local_file(path: Path, max_bytes: int):
    path = Path(path)
    if max_bytes < 0:
        raise ValueError("invalid_max_bytes")
    if not path.is_absolute() or "\x00" in str(path):
        raise ValueError("invalid_absolute_path")
    # Check the complete lexical chain without resolving away a link first.
    current = None
    for part in (*reversed(path.parents), path):
        current = part.lstat()
        if stat.S_ISLNK(current.st_mode) or int(getattr(current, "st_file_attributes", 0)) & 0x400:
            raise ValueError("reparse_path")
    if not stat.S_ISREG(current.st_mode):
        raise ValueError("not_file")
    if current.st_size > max_bytes:
        raise ValueError("size_limit")
    if current.st_size <= 0:
        raise ValueError("empty_file")
    return current

def read_verified_file(path: Path, max_bytes: int, *, expected: Mapping | None = None,
                       expected_sha256: str = "") -> bytes:
    path = Path(path)
    before = inspect_local_file(path, max_bytes)
    if expected and any(int(getattr(before, key)) != int(value) for key, value in expected.items()):
        raise ValueError("source_changed_before_snapshot")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if identity(opened) != identity(before):
            raise ValueError("source_changed_before_snapshot")
        data = stream.read(max_bytes + 1)
        after = os.fstat(stream.fileno())
    if identity(after) != identity(opened) or len(data) != opened.st_size:
        raise ValueError("source_changed_during_snapshot")
    if identity(inspect_local_file(path, max_bytes)) != identity(opened):
        raise ValueError("source_changed_during_snapshot")
    if expected_sha256 and hashlib.sha256(data).hexdigest() != expected_sha256:
        raise ValueError("source_changed_before_snapshot")
    return data

def failure_code(error: Exception) -> str:
    if isinstance(error, FileNotFoundError):
        return "missing"
    if isinstance(error, PermissionError):
        return "local_permission_denied"
    if isinstance(error, ValueError):
        return str(error) or "invalid_file"
    return "local_unreadable"
