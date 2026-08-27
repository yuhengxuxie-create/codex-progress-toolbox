from __future__ import annotations

from pathlib import Path

import pytest

from progress_wx.codex_app_tools import (
    DesktopAppToolsClient,
    DesktopAppToolsError,
    DesktopAppToolsResultUnknown,
)


class FakePipe:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = list(responses)
        self.requests: list[dict] = []
        self.closed = False

    def request(self, payload: dict) -> dict:
        self.requests.append(payload)
        return self.responses.pop(0)

    def close(self) -> None:
        self.closed = True


def _write_app_log(root: Path, pipe_name: str) -> None:
    path = root / "2026" / "08" / "24" / "codex-desktop-test-123-t0-i1.log"
    path.parent.mkdir(parents=True)
    path.write_text(
        "2026-08-24T00:00:00Z info [dynamic-app-tools-native-pipe] "
        f"dynamic_app_tools_listening pipePath=\\\\.\\pipe\\{pipe_name}\n",
        encoding="utf-8",
    )


def test_verified_pipe_lists_tool_then_sends_without_model_override(tmp_path: Path) -> None:
    pipe_name = "codex-browser-use-11111111-2222-3333-4444-555555555555"
    _write_app_log(tmp_path, pipe_name)
    fake = FakePipe(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "tools": [
                        {
                            "namespace": "codex_app",
                            "name": "send_message_to_thread",
                        }
                    ]
                },
            },
            {"jsonrpc": "2.0", "id": 2, "result": {"content": []}},
        ]
    )
    seen: list[str] = []

    def connect(path: str, _connect_timeout: float, _response_timeout: float):
        seen.append(path)
        return fake

    session = DesktopAppToolsClient(tmp_path, connector=connect).open_verified()
    session.send_message("thread-1", "继续", call_tag="PCWX-TEST")
    session.close()

    assert seen == [rf"\\.\pipe\{pipe_name}"]
    call = fake.requests[1]
    assert call["method"] == "tools/call"
    assert call["params"]["namespace"] == "codex_app"
    assert call["params"]["tool"] == "send_message_to_thread"
    assert call["params"]["arguments"] == {
        "threadId": "thread-1",
        "prompt": "继续",
    }
    assert "model" not in call["params"]["arguments"]
    assert "thinking" not in call["params"]["arguments"]
    assert fake.closed is True


def test_call_error_after_submit_is_result_unknown(tmp_path: Path) -> None:
    pipe_name = "codex-browser-use-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    _write_app_log(tmp_path, pipe_name)
    fake = FakePipe(
        [
            {
                "id": 1,
                "result": {
                    "tools": [
                        {
                            "namespace": "codex_app",
                            "name": "send_message_to_thread",
                        }
                    ]
                },
            },
            {"id": 2, "error": {"code": -32000, "message": "failed"}},
        ]
    )
    session = DesktopAppToolsClient(
        tmp_path,
        connector=lambda *_args: fake,
    ).open_verified()

    with pytest.raises(DesktopAppToolsResultUnknown):
        session.send_message("thread-1", "不要重复", call_tag="PCWX-TEST")
    session.close()


def test_log_parser_rejects_unrelated_named_pipe(tmp_path: Path) -> None:
    _write_app_log(tmp_path, "unrelated-pipe")

    with pytest.raises(Exception, match="没有应用工具管道"):
        DesktopAppToolsClient(tmp_path, connector=lambda *_args: None).open_verified()


def test_verified_read_tools_decode_list_and_wait_payloads(tmp_path: Path) -> None:
    pipe_name = "codex-browser-use-12345678-1234-1234-1234-123456789abc"
    _write_app_log(tmp_path, pipe_name)
    fake = FakePipe(
        [
            {
                "id": 1,
                "result": {
                    "tools": [
                        {"namespace": "codex_app", "name": "list_threads"},
                        {"namespace": "codex_app", "name": "wait_threads"},
                    ]
                },
            },
            {
                "id": 2,
                "result": {
                    "success": True,
                    "contentItems": [
                        {
                            "type": "inputText",
                            "text": '{"threads":[{"id":"source"}],"pinnedThreads":[]}',
                        }
                    ],
                },
            },
            {
                "id": 3,
                "result": {
                    "success": True,
                    "contentItems": [
                        {
                            "type": "inputText",
                            "text": '{"timedOut":true,"wake":null,"polls":[]}',
                        }
                    ],
                },
            },
        ]
    )
    session = DesktopAppToolsClient(
        tmp_path,
        connector=lambda *_args: fake,
    ).open_verified(required_tools=("list_threads", "wait_threads"))

    assert session.list_threads("target", limit=20)["threads"][0]["id"] == "source"
    assert session.wait_threads(
        "source",
        [{"threadId": "target", "hostId": "local", "afterCursor": "cursor-1"}],
        timeout_ms=10_000,
    )["timedOut"] is True

    assert fake.requests[1]["params"]["arguments"] == {"limit": 20}
    assert fake.requests[2]["params"]["arguments"] == {
        "targets": [
            {
                "threadId": "target",
                "hostId": "local",
                "afterCursor": "cursor-1",
            }
        ],
        "timeoutMs": 10_000,
    }
    assert fake.requests[2]["params"]["threadId"] == "source"
    session.close()


def test_read_tool_rejects_non_json_content_without_treating_it_as_write_unknown(
    tmp_path: Path,
) -> None:
    pipe_name = "codex-browser-use-fedcba98-4321-4321-4321-cba987654321"
    _write_app_log(tmp_path, pipe_name)
    fake = FakePipe(
        [
            {
                "id": 1,
                "result": {
                    "tools": [
                        {"namespace": "codex_app", "name": "list_threads"},
                    ]
                },
            },
            {
                "id": 2,
                "result": {
                    "success": True,
                    "contentItems": [{"type": "inputText", "text": "not-json"}],
                },
            },
        ]
    )
    session = DesktopAppToolsClient(
        tmp_path,
        connector=lambda *_args: fake,
    ).open_verified(required_tools=("list_threads",))

    with pytest.raises(DesktopAppToolsError, match="不是有效 JSON"):
        session.list_threads("thread-1")
    session.close()


def test_management_tools_use_exact_desktop_arguments(tmp_path: Path) -> None:
    pipe_name = "codex-browser-use-99999999-8888-7777-6666-555555555555"
    _write_app_log(tmp_path, pipe_name)

    def content(payload: str) -> dict:
        return {
            "success": True,
            "contentItems": [{"type": "inputText", "text": payload}],
        }

    fake = FakePipe(
        [
            {
                "id": 1,
                "result": {
                    "tools": [
                        {"namespace": "codex_app", "name": "list_projects"},
                        {"namespace": "codex_app", "name": "read_thread"},
                        {"namespace": "codex_app", "name": "create_thread"},
                    ]
                },
            },
            {"id": 2, "result": content('{"projects":[]}')},
            {"id": 3, "result": content('{"thread":{"id":"target"},"turns":[]}')},
            {"id": 4, "result": content('{"threadId":"created","hostId":"local"}')},
        ]
    )
    session = DesktopAppToolsClient(
        tmp_path, connector=lambda *_args: fake
    ).open_verified(required_tools=("list_projects", "read_thread", "create_thread"))

    assert session.list_projects("source")["projects"] == []
    assert session.read_thread(
        "source", "target", host_id="local", turn_limit=3
    )["thread"]["id"] == "target"
    assert session.create_thread(
        "source",
        "逐字提示词",
        {"type": "projectless"},
        title="新会话",
    )["threadId"] == "created"

    assert fake.requests[1]["params"]["arguments"] == {}
    assert fake.requests[2]["params"]["arguments"] == {
        "threadId": "target",
        "turnLimit": 3,
        "includeOutputs": False,
        "maxOutputCharsPerItem": 4000,
        "hostId": "local",
    }
    create_args = fake.requests[3]["params"]["arguments"]
    assert create_args == {
        "prompt": "逐字提示词",
        "target": {"type": "projectless"},
        "title": "新会话",
    }
    assert "model" not in create_args
    assert "thinking" not in create_args
    session.close()
