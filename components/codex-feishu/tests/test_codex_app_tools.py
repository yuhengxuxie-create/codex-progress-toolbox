from __future__ import annotations

from pathlib import Path

import pytest

from progress_wx.codex_app_tools import (
    DesktopAppToolsClient,
    DesktopAppToolsError,
    DesktopAppToolsNotSubmitted,
    DesktopAppToolsRejected,
    DesktopAppToolsResultUnknown,
    DesktopAppToolsUnavailable,
    VerifiedDesktopAppTools,
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


class FailingPipe(FakePipe):
    def __init__(self, error: BaseException) -> None:
        super().__init__(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "tools": [
                            {"namespace": "codex_app", "name": "list_threads"},
                            {
                                "namespace": "codex_app",
                                "name": "set_thread_archived",
                            },
                        ]
                    },
                }
            ]
        )
        self.error = error

    def request(self, payload: dict) -> dict:
        self.requests.append(payload)
        if len(self.requests) == 2:
            raise self.error
        return self.responses.pop(0)


def _write_app_log(root: Path, pipe_name: str) -> None:
    path = root / "2026" / "08" / "24" / "codex-desktop-test-123-t0-i1.log"
    path.parent.mkdir(parents=True)
    path.write_text(
        "2026-08-24T00:00:00Z info [dynamic-app-tools-native-pipe] "
        f"dynamic_app_tools_listening pipePath=\\\\.\\pipe\\{pipe_name}\n",
        encoding="utf-8",
    )


def _write_runtime_log(
    root: Path,
    pipe_name: str,
    cli_path: Path,
    *,
    source: str = "bundled-or-dev",
) -> None:
    path = root / "2026" / "09" / "01" / "codex-desktop-runtime-t0-i1.log"
    path.parent.mkdir(parents=True)
    path.write_text(
        "2026-09-01T00:00:00Z info [dynamic-app-tools-native-pipe] "
        f"dynamic_app_tools_listening pipePath=\\\\.\\pipe\\{pipe_name}\n"
        "2026-09-01T00:00:01Z info [BrowserUseThreadConfig] "
        "browser_use_runtime_paths_selected "
        f"codexCliPath={cli_path} codexCliPathSource={source} "
        "platform=win32\n",
        encoding="utf-8",
    )


def test_current_codex_cli_is_bound_to_verified_pipe_log(tmp_path: Path) -> None:
    pipe_name = "codex-browser-use-10101010-2020-3030-4040-505050505050"
    current = tmp_path / "OpenAI" / "Codex" / "bin" / "current" / "codex.exe"
    _write_runtime_log(tmp_path / "logs", pipe_name, current)
    fake = FakePipe(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "tools": [
                        {"namespace": "codex_app", "name": "list_threads"}
                    ]
                },
            }
        ]
    )
    client = DesktopAppToolsClient(
        tmp_path / "logs",
        connector=lambda *_args: fake,
    )

    assert client.discover_current_codex_cli() == str(current)
    assert fake.closed is True


def test_current_codex_cli_rejects_non_bundled_log_source(tmp_path: Path) -> None:
    pipe_name = "codex-browser-use-60606060-7070-8080-9090-a0a0a0a0a0a0"
    candidate = tmp_path / "outside" / "codex.exe"
    _write_runtime_log(
        tmp_path / "logs", pipe_name, candidate, source="environment-override"
    )
    fake = FakePipe(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "tools": [
                        {"namespace": "codex_app", "name": "list_threads"}
                    ]
                },
            }
        ]
    )
    with pytest.raises(DesktopAppToolsUnavailable, match="bundled-or-dev"):
        DesktopAppToolsClient(
            tmp_path / "logs", connector=lambda *_args: fake
        ).discover_current_codex_cli()


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


@pytest.mark.parametrize("host_id", ["local", ""])
def test_set_thread_archived_uses_distinct_source_and_target(
    tmp_path: Path, host_id: str
) -> None:
    pipe_name = "codex-browser-use-11111111-2222-3333-4444-555555555555"
    _write_app_log(tmp_path, pipe_name)
    fake = FakePipe(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "tools": [
                        {"namespace": "codex_app", "name": "list_threads"},
                        {
                            "namespace": "codex_app",
                            "name": "set_thread_archived",
                        },
                    ]
                },
            },
            {"jsonrpc": "2.0", "id": 2, "result": {"success": True}},
        ]
    )
    session = DesktopAppToolsClient(
        tmp_path,
        connector=lambda *_args: fake,
    ).open_verified(required_tools=("list_threads", "set_thread_archived"))

    try:
        assert session.set_thread_archived(
            "target-thread",
            archived=True,
            source_thread_id="source-thread",
            host_id=host_id,
            call_tag="PCWX-ARCHIVE",
        ) == {"success": True}
        call = fake.requests[1]
        assert call["method"] == "tools/call"
        assert call["params"]["namespace"] == "codex_app"
        assert call["params"]["tool"] == "set_thread_archived"
        assert call["params"]["threadId"] == "source-thread"
        assert call["params"]["turnId"] == "progress-wx-PCWX-ARCHIVE"
        expected_arguments = {
            "threadId": "target-thread",
            "archived": True,
        }
        if host_id:
            expected_arguments["hostId"] = host_id
        assert call["params"]["arguments"] == expected_arguments
        assert "model" not in call["params"]["arguments"]
        assert "thinking" not in call["params"]["arguments"]
    finally:
        session.close()


def test_set_thread_archived_validates_target_source_and_archived_type() -> None:
    fake = FakePipe([])
    session = VerifiedDesktopAppTools(
        fake,
        frozenset({"set_thread_archived"}),
    )

    with pytest.raises(ValueError, match="thread_id"):
        session.set_thread_archived(
            " ",
            archived=True,
            source_thread_id="source-thread",
            call_tag="PCWX-ARCHIVE",
        )
    with pytest.raises(ValueError, match="source_thread_id"):
        session.set_thread_archived(
            "target-thread",
            archived=True,
            source_thread_id=" ",
            call_tag="PCWX-ARCHIVE",
        )
    with pytest.raises(TypeError, match="archived"):
        session.set_thread_archived(
            "target-thread",
            archived=1,  # type: ignore[arg-type]
            source_thread_id="source-thread",
            call_tag="PCWX-ARCHIVE",
        )
    assert fake.requests == []


@pytest.mark.parametrize(
    "declared_tools",
    [
        [{"namespace": "codex_app", "name": "list_threads"}],
        [
            {"namespace": "codex_app", "name": "list_threads"},
            {"namespace": "codex_app", "name": "set_thread_archived"},
            {"namespace": "codex_app", "name": "set_thread_archived"},
        ],
    ],
    ids=["missing", "duplicate"],
)
def test_set_thread_archived_requires_one_verified_tool(
    tmp_path: Path, declared_tools: list[dict]
) -> None:
    pipe_name = "codex-browser-use-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    _write_app_log(tmp_path, pipe_name)
    fake = FakePipe(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"tools": declared_tools},
            }
        ]
    )

    with pytest.raises(DesktopAppToolsUnavailable):
        DesktopAppToolsClient(
            tmp_path,
            connector=lambda *_args: fake,
        ).open_verified(required_tools=("list_threads", "set_thread_archived"))
    assert fake.closed is True


def test_set_thread_archived_explicit_rejection_is_retryable(tmp_path: Path) -> None:
    pipe_name = "codex-browser-use-bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
    _write_app_log(tmp_path, pipe_name)
    fake = FakePipe(
        [
            {
                "id": 1,
                "result": {
                    "tools": [
                        {"namespace": "codex_app", "name": "list_threads"},
                        {
                            "namespace": "codex_app",
                            "name": "set_thread_archived",
                        },
                    ]
                },
            },
            {"id": 2, "error": {"code": -32000, "message": "rejected"}},
        ]
    )
    session = DesktopAppToolsClient(
        tmp_path,
        connector=lambda *_args: fake,
    ).open_verified(required_tools=("list_threads", "set_thread_archived"))

    try:
        with pytest.raises(DesktopAppToolsRejected):
            session.set_thread_archived(
                "target-thread",
                archived=True,
                source_thread_id="source-thread",
                call_tag="PCWX-ARCHIVE",
            )
    finally:
        session.close()


def test_set_thread_archived_requires_provable_success(tmp_path: Path) -> None:
    pipe_name = "codex-browser-use-cccccccc-dddd-eeee-ffff-000000000000"
    _write_app_log(tmp_path, pipe_name)
    fake = FakePipe(
        [
            {
                "id": 1,
                "result": {
                    "tools": [
                        {"namespace": "codex_app", "name": "list_threads"},
                        {
                            "namespace": "codex_app",
                            "name": "set_thread_archived",
                        },
                    ]
                },
            },
            {"id": 2, "result": {}},
        ]
    )
    session = DesktopAppToolsClient(
        tmp_path,
        connector=lambda *_args: fake,
    ).open_verified(required_tools=("list_threads", "set_thread_archived"))

    try:
        with pytest.raises(DesktopAppToolsResultUnknown):
            session.set_thread_archived(
                "target-thread",
                archived=True,
                source_thread_id="source-thread",
                call_tag="PCWX-ARCHIVE",
            )
    finally:
        session.close()


def test_set_thread_archived_accepts_content_list_success(tmp_path: Path) -> None:
    pipe_name = "codex-browser-use-dddddddd-eeee-ffff-0000-111111111111"
    _write_app_log(tmp_path, pipe_name)
    fake = FakePipe(
        [
            {
                "id": 1,
                "result": {
                    "tools": [
                        {"namespace": "codex_app", "name": "list_threads"},
                        {
                            "namespace": "codex_app",
                            "name": "set_thread_archived",
                        },
                    ]
                },
            },
            {"id": 2, "result": {"contentItems": []}},
        ]
    )
    session = DesktopAppToolsClient(
        tmp_path,
        connector=lambda *_args: fake,
    ).open_verified(required_tools=("list_threads", "set_thread_archived"))

    try:
        assert session.set_thread_archived(
            "target-thread",
            archived=True,
            source_thread_id="source-thread",
            call_tag="PCWX-ARCHIVE",
        ) == {"contentItems": []}
    finally:
        session.close()


def test_set_thread_archived_response_id_mismatch_is_unknown(tmp_path: Path) -> None:
    pipe_name = "codex-browser-use-eeeeeeee-ffff-0000-1111-222222222222"
    _write_app_log(tmp_path, pipe_name)
    fake = FakePipe(
        [
            {
                "id": 1,
                "result": {
                    "tools": [
                        {"namespace": "codex_app", "name": "list_threads"},
                        {
                            "namespace": "codex_app",
                            "name": "set_thread_archived",
                        },
                    ]
                },
            },
            {"id": 999, "result": {"success": True}},
        ]
    )
    session = DesktopAppToolsClient(
        tmp_path,
        connector=lambda *_args: fake,
    ).open_verified(required_tools=("list_threads", "set_thread_archived"))

    try:
        with pytest.raises(DesktopAppToolsResultUnknown):
            session.set_thread_archived(
                "target-thread",
                archived=True,
                source_thread_id="source-thread",
                call_tag="PCWX-ARCHIVE",
            )
    finally:
        session.close()


@pytest.mark.parametrize(
    "error",
    [
        OSError("pipe write/read failed"),
        EOFError("pipe closed"),
        TimeoutError("pipe timed out"),
        DesktopAppToolsError("malformed response"),
    ],
    ids=["os-error", "eof", "timeout", "desktop-error"],
)
def test_set_thread_archived_transport_errors_are_unknown(
    tmp_path: Path, error: BaseException
) -> None:
    pipe_name = "codex-browser-use-ffffffff-0000-1111-2222-333333333333"
    _write_app_log(tmp_path, pipe_name)
    fake = FailingPipe(error)
    session = DesktopAppToolsClient(
        tmp_path,
        connector=lambda *_args: fake,
    ).open_verified(required_tools=("list_threads", "set_thread_archived"))

    try:
        with pytest.raises(DesktopAppToolsResultUnknown) as caught:
            session.set_thread_archived(
                "target-thread",
                archived=True,
                source_thread_id="source-thread",
                call_tag="PCWX-ARCHIVE",
            )
        assert isinstance(caught.value.__cause__, type(error))
    finally:
        session.close()


def test_set_thread_archived_preserves_not_submitted(tmp_path: Path) -> None:
    pipe_name = "codex-browser-use-00000000-1111-2222-3333-444444444444"
    _write_app_log(tmp_path, pipe_name)
    fake = FailingPipe(DesktopAppToolsNotSubmitted("closed before write"))
    session = DesktopAppToolsClient(
        tmp_path,
        connector=lambda *_args: fake,
    ).open_verified(required_tools=("list_threads", "set_thread_archived"))

    try:
        with pytest.raises(DesktopAppToolsNotSubmitted):
            session.set_thread_archived(
                "target-thread",
                archived=True,
                source_thread_id="source-thread",
                call_tag="PCWX-ARCHIVE",
            )
    finally:
        session.close()


def test_live_pipe_discovery_precedes_stale_log_candidates(tmp_path: Path) -> None:
    stale_name = "codex-browser-use-00000000-1111-2222-3333-444444444444"
    live_name = "codex-browser-use-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    _write_app_log(tmp_path, stale_name)
    fake = FakePipe(
        [
            {
                "id": 1,
                "result": {
                    "tools": [
                        {"namespace": "codex_app", "name": "send_message_to_thread"}
                    ]
                },
            }
        ]
    )
    seen: list[str] = []

    def connect(path: str, _connect_timeout: float, _response_timeout: float):
        seen.append(path)
        assert path.endswith(live_name)
        return fake

    session = DesktopAppToolsClient(
        tmp_path,
        connector=connect,
        live_pipe_names=lambda: ["unrelated-pipe", live_name, live_name],
    ).open_verified()
    session.close()

    assert seen == [rf"\\.\pipe\{live_name}"]


def test_live_pipe_enumeration_failure_falls_back_to_log(tmp_path: Path) -> None:
    pipe_name = "codex-browser-use-12345678-1111-2222-3333-444444444444"
    _write_app_log(tmp_path, pipe_name)
    fake = FakePipe(
        [
            {
                "id": 1,
                "result": {
                    "tools": [
                        {"namespace": "codex_app", "name": "send_message_to_thread"}
                    ]
                },
            }
        ]
    )

    def fail_live_enumeration() -> list[str]:
        raise OSError("pipe namespace unavailable")

    session = DesktopAppToolsClient(
        tmp_path,
        connector=lambda *_args: fake,
        live_pipe_names=fail_live_enumeration,
    ).open_verified()
    session.close()
    assert fake.closed is True


def test_call_explicit_error_after_submit_is_rejected(tmp_path: Path) -> None:
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

    with pytest.raises(DesktopAppToolsRejected):
        session.send_message("thread-1", "不要重复", call_tag="PCWX-TEST")
    session.close()


def test_call_mismatched_response_id_after_submit_is_result_unknown(tmp_path: Path) -> None:
    pipe_name = "codex-browser-use-bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
    _write_app_log(tmp_path, pipe_name)
    fake = FakePipe(
        [
            {
                "id": 1,
                "result": {
                    "tools": [
                        {"namespace": "codex_app", "name": "send_message_to_thread"}
                    ]
                },
            },
            {"id": 999, "result": {"content": []}},
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
