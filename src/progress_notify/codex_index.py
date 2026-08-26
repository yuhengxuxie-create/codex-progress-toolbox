"""Best-effort, read-only access to Codex's local thread title index."""

from __future__ import annotations

import json
import os
from pathlib import Path


def default_codex_home() -> Path:
    """Return the Codex data directory used by the current process."""

    value = os.environ.get("CODEX_HOME")
    return Path(value).expanduser() if value else Path.home() / ".codex"


def read_indexed_thread_name(
    thread_id: str,
    codex_home: str | os.PathLike[str] | None = None,
) -> str | None:
    """Return the latest exact-ID title from ``session_index.jsonl``.

    The index is a local implementation detail rather than the primary Codex
    API, so every filesystem, decoding, and record-level failure degrades to
    ``None``. A concurrently appended partial JSONL line is therefore harmless.
    """

    if not isinstance(thread_id, str) or not thread_id:
        raise ValueError("thread_id must be a non-empty string")

    root = (
        Path(codex_home).expanduser()
        if codex_home is not None
        else default_codex_home()
    )
    index_path = root / "session_index.jsonl"
    latest_name: str | None = None

    try:
        with index_path.open("r", encoding="utf-8-sig") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(record, dict) or record.get("id") != thread_id:
                    continue
                value = record.get("thread_name")
                candidate = value.strip() if isinstance(value, str) else ""
                if candidate:
                    latest_name = candidate
    except (OSError, UnicodeError):
        return None

    return latest_name
