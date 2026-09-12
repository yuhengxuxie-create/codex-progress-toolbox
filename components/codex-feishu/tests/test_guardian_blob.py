from concurrent.futures import ThreadPoolExecutor
import hashlib
import multiprocessing
import os
from pathlib import Path
import threading
import time

import pytest

from progress_wx.guardian_blob import GuardianBlobPayloadRejectedError, GuardianBlobs
import progress_wx.guardian_blob as guardian_blob


def _put_from_process(root: str, data: bytes, output) -> None:
    try:
        output.put(("ok", GuardianBlobs(root).put(data)))
    except BaseException as exc:  # pragma: no cover - surfaced by parent assert
        output.put(("error", type(exc).__name__, str(exc)))


def test_put_and_load_returns_verified_json_reference(tmp_path: Path) -> None:
    store = GuardianBlobs(tmp_path / "media")
    data = b"media payload" * 100_000

    reference = store.put(data)

    assert reference == {
        "name": f"{hashlib.sha256(data).hexdigest()}.blob",
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }
    assert store.put(data) == reference
    assert store.load(reference) == data
    assert list(store.root.glob("*.blob")) == [store.root / reference["name"]]


def test_empty_payload_is_a_typed_rejection(tmp_path: Path) -> None:
    store = GuardianBlobs(tmp_path / "media")

    with pytest.raises(GuardianBlobPayloadRejectedError) as captured:
        store.put(b"")

    assert captured.value.reason == "empty"
    assert captured.value.size == 0
    assert captured.value.limit == guardian_blob.MAX_BLOB_SIZE
    assert list(store.root.glob("*.blob")) == []


def test_documented_file_boundary_is_30_decimal_megabytes(monkeypatch, tmp_path: Path) -> None:
    assert guardian_blob.MAX_BLOB_SIZE == 30 * 1000 * 1000
    monkeypatch.setattr(guardian_blob, "MAX_BLOB_SIZE", 8)
    store = GuardianBlobs(tmp_path / "media")

    assert store.load(store.put(b"12345678")) == b"12345678"
    with pytest.raises(GuardianBlobPayloadRejectedError) as captured:
        store.put(b"123456789")
    assert captured.value.reason == "too_large"
    assert captured.value.size == 9


def test_load_rejects_path_traversal_wrong_hash_and_symlink(tmp_path: Path) -> None:
    store = GuardianBlobs(tmp_path / "media")
    data = b"safe payload"
    reference = store.put(data)

    with pytest.raises(ValueError):
        store.load({**reference, "name": "../outside.blob"})
    with pytest.raises(ValueError):
        store.load(
            {
                **reference,
                "name": f"{'0' * 64}.blob",
            }
        )

    target = store.root / reference["name"]
    outside = tmp_path / "outside.bin"
    outside.write_bytes(data)
    target.unlink()
    try:
        target.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(ValueError):
        store.load(reference)


def test_load_rejects_hash_and_size_tampering(tmp_path: Path) -> None:
    store = GuardianBlobs(tmp_path / "media")
    data = b"original payload"
    reference = store.put(data)
    target = store.root / reference["name"]
    target.write_bytes(b"tampered payload")

    with pytest.raises(ValueError, match="integrity"):
        store.load(reference)
    with pytest.raises(ValueError, match="integrity"):
        store.load({**reference, "size": len(data) + 1})


def test_put_and_load_enforce_single_blob_limit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(guardian_blob, "MAX_BLOB_SIZE", 8)
    store = GuardianBlobs(tmp_path / "media")

    with pytest.raises(ValueError, match="single-file"):
        store.put(b"123456789")

    data = b"12345678"
    reference = store.put(data)
    (store.root / reference["name"]).write_bytes(b"123456789")
    with pytest.raises(ValueError, match="exceeds single-file"):
        store.load(reference)


def test_quota_counts_blobs_and_pending_files(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(guardian_blob, "MAX_TOTAL_BYTES", 5)
    store = GuardianBlobs(tmp_path / "media")
    store.put(b"12345")
    with pytest.raises(BufferError, match="byte limit"):
        store.put(b"6")

    files_root = tmp_path / "files"
    monkeypatch.setattr(guardian_blob, "MAX_TOTAL_BYTES", 256)
    monkeypatch.setattr(guardian_blob, "MAX_BLOB_FILES", 1)
    files_root.mkdir()
    (files_root / ".guardian-blob-manual.pending").write_bytes(b"pending")
    with pytest.raises(BufferError, match="file limit"):
        GuardianBlobs(files_root).put(b"new")


def test_same_hash_is_idempotent_across_threads(tmp_path: Path) -> None:
    root = tmp_path / "media"
    data = b"concurrent payload" * 100_000

    def write_one(_index: int):
        return GuardianBlobs(root).put(data)

    with ThreadPoolExecutor(max_workers=8) as executor:
        references = list(executor.map(write_one, range(16)))

    assert all(reference == references[0] for reference in references)
    assert len(list(root.glob("*.blob"))) == 1
    assert GuardianBlobs(root).load(references[0]) == data


def test_repeated_concurrent_first_lock_creation_is_serialized(tmp_path: Path) -> None:
    """Independent instances survive a fresh Windows lock file each round."""

    data = b"first-lock payload" * 10_000
    for round_index in range(8):
        root = tmp_path / f"fresh-{round_index}" / "media"
        root.mkdir(parents=True)
        stores = [GuardianBlobs(root) for _ in range(12)]
        # Force every round through lock-file initialization without racing
        # GuardianBlobs.__init__'s private-directory setup.
        (root / guardian_blob._LOCK_NAME).unlink(missing_ok=True)
        barrier = threading.Barrier(len(stores))

        def write_one(store: GuardianBlobs):
            barrier.wait(timeout=5)
            return store.put(data)

        with ThreadPoolExecutor(max_workers=len(stores)) as executor:
            references = list(executor.map(write_one, stores))
        assert all(reference == references[0] for reference in references)
        assert len(list(root.glob("*.blob"))) == 1


def test_same_hash_is_serialized_across_processes(tmp_path: Path) -> None:
    root = tmp_path / "media"
    data = b"cross process payload" * 75_000
    context = multiprocessing.get_context("spawn")
    output = context.Queue()
    processes = [
        context.Process(target=_put_from_process, args=(str(root), data, output))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    results = [output.get(timeout=20) for _ in processes]
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0

    assert all(result[0] == "ok" for result in results), results
    assert len(list(root.glob("*.blob"))) == 1


def test_cleanup_keeps_referenced_and_unknown_files_and_honors_terminal_names(
    tmp_path: Path,
) -> None:
    store = GuardianBlobs(tmp_path / "media")
    referenced = store.put(b"referenced")
    orphan = store.put(b"orphan")
    now = time.time() - guardian_blob.BLOB_RETENTION_SECONDS - 1
    os.utime(store.root / orphan["name"], (now, now))
    os.utime(store.root / referenced["name"], (now, now))

    deleted = store.cleanup(
        [referenced["name"], "unknown-reference.blob"],
        terminal_names=[referenced["name"]],
    )
    assert deleted == [orphan["name"]]
    assert (store.root / referenced["name"]).exists()

    # A terminal hint cannot remove a blob that still has a pending/uncertain
    # reference; the caller's complete reference set owns that decision.
    assert store.cleanup([referenced["name"]], terminal_names=[referenced["name"]]) == []
    assert store.cleanup([], terminal_names=[referenced["name"]]) == [referenced["name"]]
