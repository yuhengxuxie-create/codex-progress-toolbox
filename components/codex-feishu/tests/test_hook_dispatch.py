from __future__ import annotations

import json
from pathlib import Path

from progress_wx import hook_dispatch
from progress_wx.state import StateStore


def test_main_uses_explicit_custom_config(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "custom.yaml"
    database = tmp_path / "custom.sqlite"
    config_path.write_text(
        "service:\n  database: custom.sqlite\n",
        encoding="utf-8",
    )
    forwarded: list[str] = []
    monkeypatch.setattr(
        hook_dispatch,
        "forward_original",
        lambda raw: forwarded.append(raw) or True,
    )
    raw = json.dumps(
        {
            "type": "agent-turn-complete",
            "thread-id": "thread-custom",
            "turn-id": "turn-custom",
            "last-assistant-message": "结构化完成摘要",
        }
    )

    assert hook_dispatch.main(["--config", str(config_path), raw]) == 0
    # Codex/宿主重试同一 notify 时应当是幂等成功，不能重复创建队列事件。
    assert hook_dispatch.main(["--config", str(config_path), raw]) == 0
    assert forwarded == [raw, raw]
    store = StateStore(database)
    try:
        assert store.stats()["hook_events"] == 1
        pending = store.pending_hook_payloads()
        assert pending == [
            (
                "thread-custom:turn-custom:completed",
                {
                    "type": "agent-turn-complete",
                    "thread-id": "thread-custom",
                    "turn-id": "turn-custom",
                    "last-assistant-message": "结构化完成摘要",
                },
            )
        ]
    finally:
        store.close()


def test_main_rejects_malformed_or_non_completion_notify_without_queue(
    tmp_path: Path, monkeypatch
) -> None:
    """非法 JSON、非对象和非完成事件不能污染指定状态库。"""

    config_path = tmp_path / "custom.yaml"
    database = tmp_path / "custom.sqlite"
    config_path.write_text(
        f"service:\n  database: {database.as_posix()}\n",
        encoding="utf-8",
    )
    forwarded: list[str] = []
    errors: list[str] = []
    monkeypatch.setattr(
        hook_dispatch,
        "forward_original",
        lambda raw: forwarded.append(raw) or False,
    )
    monkeypatch.setattr(hook_dispatch, "_record_error", errors.append)

    invalid_inputs = [
        "not-json",
        "[]",
        json.dumps({"type": "turn-ended", "thread-id": "t", "turn-id": "u"}),
        json.dumps({"type": "agent-turn-complete", "thread-id": "t"}),
    ]
    for raw in invalid_inputs:
        assert hook_dispatch.main(["--config", str(config_path), raw]) == 1

    assert forwarded == invalid_inputs
    assert errors
    if database.exists():
        store = StateStore(database)
        try:
            assert store.stats()["hook_events"] == 0
        finally:
            store.close()


def test_previous_notify_wrapper_requires_strict_trailing_json_argv() -> None:
    command = [
        "wrapper.exe",
        "turn-ended",
        "--previous-notify",
        '["python.exe", "old.py"]',
    ]
    assert hook_dispatch._previous_notify_wrapper(command) == (
        ["wrapper.exe", "turn-ended"],
        ["python.exe", "old.py"],
    )
    assert hook_dispatch._previous_notify_wrapper([*command, "extra"]) is None
    assert hook_dispatch._previous_notify_wrapper(
        ["wrapper.exe", "--previous-notify", "{}"]
    ) is None


def test_original_notify_skips_duplicate_outer_wrapper(
    tmp_path: Path, monkeypatch
) -> None:
    installed = ["python.exe", "new-hook.py", "--config", "new.yaml"]
    older = ["python.exe", "old-hook.py"]
    wrapper = ["computer-use.exe", "turn-ended"]
    codex_config = tmp_path / "config.toml"
    codex_config.write_text(
        "notify = "
        + json.dumps(
            [*wrapper, "--previous-notify", json.dumps(installed)],
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    install_state = tmp_path / "install-state.json"
    install_state.write_text(
        json.dumps(
            {
                "config_path": str(codex_config),
                "installed_notify": installed,
                "original_notify": [
                    *wrapper,
                    "--previous-notify",
                    json.dumps(older),
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(hook_dispatch, "INSTALL_STATE_PATH", install_state)

    assert hook_dispatch._load_original_notify() == older


def test_original_notify_keeps_different_outer_wrapper(
    tmp_path: Path, monkeypatch
) -> None:
    installed = ["python.exe", "new-hook.py"]
    original = ["wrapper-v1.exe", "--previous-notify", '["old.exe"]']
    codex_config = tmp_path / "config.toml"
    codex_config.write_text(
        "notify = "
        + json.dumps(
            ["wrapper-v2.exe", "--previous-notify", json.dumps(installed)]
        )
        + "\n",
        encoding="utf-8",
    )
    install_state = tmp_path / "install-state.json"
    install_state.write_text(
        json.dumps(
            {
                "config_path": str(codex_config),
                "installed_notify": installed,
                "original_notify": original,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(hook_dispatch, "INSTALL_STATE_PATH", install_state)

    assert hook_dispatch._load_original_notify() == original
