from __future__ import annotations

import json
from pathlib import Path
import threading

from progress_wx.approval_bridge import (
    ApprovalBridge,
    permission_hook_result,
    persist_execpolicy_rule,
)
from progress_wx.hooks_installer import install_permission_hook, uninstall_permission_hook
from progress_wx.state import CorrelationCodec


def _bridge(tmp_path: Path) -> ApprovalBridge:
    secret = tmp_path / "hmac.key"
    CorrelationCodec.create_secret_file(secret)
    return ApprovalBridge(tmp_path / "bridge", secret)


def _event() -> dict:
    return {
        "hook_event_name": "PermissionRequest",
        "session_id": "thread-1",
        "turn_id": "turn-1",
        "cwd": r"D:\Work",
        "model": "gpt-test",
        "tool_name": "exec_command",
        "permission_mode": "on-request",
        "tool_input": {
            "cmd": "git fetch origin",
            "prefix_rule": ["git", "fetch"],
        },
    }


def test_signed_request_and_response_roundtrip(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path)
    request = bridge.submit(_event(), timeout_seconds=30)
    assert bridge.pending() == (request,)
    assert request.reusable_prefix == ("git", "fetch")
    bridge.respond(request.request_id, "allow_similar")
    assert bridge.wait_for_response(request) == "allow_similar"
    bridge.complete(request)
    assert bridge.pending() == ()


def test_hook_returns_official_allow_shape(tmp_path: Path, monkeypatch) -> None:
    bridge = _bridge(tmp_path)
    saved: list[tuple[str, ...]] = []

    def fake_persist(_path, prefix, *, codex_command):
        del codex_command
        saved.append(tuple(prefix))
        return True

    monkeypatch.setattr("progress_wx.approval_bridge.persist_execpolicy_rule", fake_persist)
    result: list[object] = []

    def run_hook() -> None:
        result.append(
            permission_hook_result(
                _event(),
                bridge=bridge,
                timeout_seconds=30,
                rules_file=tmp_path / "rules" / "approved.rules",
                codex_command="codex",
            )
        )

    thread = threading.Thread(target=run_hook)
    thread.start()
    while not bridge.pending():
        thread.join(0.01)
    request = bridge.pending()[0]
    bridge.respond(request.request_id, "allow_similar")
    thread.join(5)
    assert not thread.is_alive()
    assert saved == [("git", "fetch")]
    assert result == [
        {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {"behavior": "allow"},
            }
        }
    ]


def test_execpolicy_rule_is_validated_before_replace(tmp_path: Path, monkeypatch) -> None:
    class Completed:
        returncode = 0
        stdout = json.dumps(
            {
                "matchedRules": [
                    {
                        "prefixRuleMatch": {
                            "matchedPrefix": ["git", "fetch"],
                            "decision": "allow",
                        }
                    }
                ],
                "decision": "allow",
            }
        )
        stderr = ""

    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        return Completed()

    monkeypatch.setattr("progress_wx.approval_bridge.subprocess.run", fake_run)
    rules = tmp_path / "approved.rules"
    changed = persist_execpolicy_rule(rules, ("git", "fetch"), codex_command="codex")
    assert changed is True
    assert 'prefix_rule(pattern=["git", "fetch"], decision="allow")' in rules.read_text(
        encoding="utf-8"
    )
    assert calls[0][-2:] == ["git", "fetch"]
    assert persist_execpolicy_rule(
        rules, ("git", "fetch"), codex_command="codex"
    ) is False


def test_hook_installer_preserves_stop_hook(tmp_path: Path) -> None:
    hooks = tmp_path / "hooks.json"
    hooks.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {"type": "command", "command": "existing-stop.ps1"}
                            ]
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    result = install_permission_hook(
        hooks_file=hooks,
        python_executable=tmp_path / "python.exe",
        entry_script=tmp_path / "progress-wx.py",
        config_file=tmp_path / "config.yaml",
        timeout_seconds=3600,
    )
    payload = json.loads(hooks.read_text(encoding="utf-8"))
    assert payload["hooks"]["Stop"][0]["hooks"][0]["command"] == "existing-stop.ps1"
    assert len(payload["hooks"]["PermissionRequest"]) == 1
    assert "permission-hook" in payload["hooks"]["PermissionRequest"][0]["hooks"][0]["command"]
    assert result["installed"] is True
    removed = uninstall_permission_hook(hooks_file=hooks)
    after = json.loads(hooks.read_text(encoding="utf-8"))
    assert removed["removed"] == 1
    assert "PermissionRequest" not in after["hooks"]
    assert after["hooks"]["Stop"][0]["hooks"][0]["command"] == "existing-stop.ps1"
