"""codex_rpc 的 JSONL 握手、调用、事件和关闭测试。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from progress_wx.codex_rpc import (  # noqa: E402
    CodexAppServer,
    CodexRPCUnhandledRequest,
    CodexRPCTimeout,
    ServerRequest,
    TurnCompletedEvent,
    command_argv,
    validate_loopback_websocket_url,
)


class CodexRPCTests(unittest.TestCase):
    def test_websocket_url_is_strictly_ipv4_loopback(self) -> None:
        self.assertEqual(
            validate_loopback_websocket_url("ws://127.0.0.1:6230/"),
            "ws://127.0.0.1:6230",
        )
        for value in (
            "ws://0.0.0.0:6230",
            "ws://localhost:6230",
            "wss://127.0.0.1:6230",
            "ws://127.0.0.1:80",
            "ws://user@127.0.0.1:6230",
            "ws://127.0.0.1:6230/private",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_loopback_websocket_url(value)

    def test_websocket_transport_uses_one_json_message_per_frame(self) -> None:
        class FakeWebSocket:
            def __init__(self) -> None:
                self.received: queue.Queue[str] = queue.Queue()
                self.sent: list[str] = []
                self.closed = False

            def settimeout(self, _value: object) -> None:
                return None

            def send(self, payload: str) -> None:
                self.sent.append(payload)
                message = json.loads(payload)
                method = message.get("method")
                if method == "initialize":
                    self.received.put(json.dumps({"id": message["id"], "result": {}}))
                elif method == "thread/read":
                    self.received.put(
                        json.dumps(
                            {
                                "id": message["id"],
                                "result": {
                                    "thread": {
                                        "id": "thread-1",
                                        "status": {"type": "active"},
                                        "turns": [
                                            {"id": "turn-old", "status": "completed"},
                                            {"id": "turn-live", "status": "inProgress"},
                                        ],
                                    }
                                },
                            }
                        )
                    )

            def recv(self) -> str:
                value = self.received.get(timeout=2)
                return value

            def close(self) -> None:
                self.closed = True
                self.received.put("")

        fake = FakeWebSocket()
        calls: list[dict[str, object]] = []

        def factory(url: str, **options: object) -> FakeWebSocket:
            calls.append({"url": url, **options})
            return fake

        client = CodexAppServer(
            websocket_url="ws://127.0.0.1:6230",
            websocket_factory=factory,
            timeout_seconds=2,
        )
        try:
            self.assertEqual(client.transport, "websocket")
            response = client.read_thread("thread-1", include_turns=True)
            self.assertEqual(client.active_turn_id(response), "turn-live")
            self.assertTrue(all(not frame.endswith("\n") for frame in fake.sent))
            self.assertEqual(calls[0]["url"], "ws://127.0.0.1:6230")
            self.assertEqual(calls[0]["http_proxy_host"], None)
            # 官方 listener 会拒绝任何 Origin；本机客户端必须显式禁止该头。
            self.assertIs(calls[0]["suppress_origin"], True)
        finally:
            client.close()
        self.assertTrue(fake.closed)

    def test_active_turn_id_rejects_ambiguous_or_incomplete_data(self) -> None:
        self.assertEqual(
            CodexAppServer.active_turn_id(
                {"result": {"thread": {"turns": [{"id": "done", "status": "completed"}]}}}
            ),
            "",
        )
        with self.assertRaisesRegex(Exception, "多个活动"):
            CodexAppServer.active_turn_id(
                {
                    "result": {
                        "thread": {
                            "turns": [
                                {"id": "a", "status": "inProgress"},
                                {"id": "b", "status": "inProgress"},
                            ]
                        }
                    }
                }
            )

    def test_command_is_an_argument_array_and_app_server_is_appended(self) -> None:
        self.assertEqual(command_argv(["codex"]), ["codex", "app-server"])
        self.assertEqual(command_argv(["codex", "app-server"]), ["codex", "app-server"])
        self.assertEqual(command_argv("codex --color never"), ["codex", "--color", "never", "app-server"])

    @unittest.skipUnless(os.name == "nt", "Windows shim path parsing")
    def test_command_accepts_unquoted_absolute_path_with_spaces(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            shim = Path(directory) / "folder with spaces" / "codex.cmd"
            shim.parent.mkdir()
            shim.write_text("@echo off\n", encoding="utf-8")
            self.assertEqual(command_argv(os.fspath(shim)), [os.fspath(shim), "app-server"])

    def test_turn_completed_event_uses_explicit_protocol_fields(self) -> None:
        event = TurnCompletedEvent.from_message(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-1", "status": "failed"},
                },
            }
        )
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.thread_id, "thread-1")
        self.assertEqual(event.turn_id, "turn-1")
        self.assertEqual(event.status, "failed")
        self.assertTrue(event.is_terminal)
        self.assertIsNone(
            TurnCompletedEvent.from_message(
                {"method": "turn/completed", "params": {"status": "completed"}}
            )
        )

    def test_stdio_handshake_resume_start_and_completed_event(self) -> None:
        # 这个短脚本模拟 JSONL App Server；app-server 参数由客户端自动追加。
        server_script = r'''
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        print(json.dumps({"id": request_id, "result": {"protocol": 1}}), flush=True)
    elif method == "thread/resume":
        print(json.dumps({"id": request_id, "result": {"threadId": message["params"]["threadId"]}}), flush=True)
    elif method == "turn/start":
        thread_id = message["params"]["threadId"]
        print(json.dumps({"id": request_id, "result": {"turnId": "turn-1"}}), flush=True)
        print(json.dumps({"method": "turn/completed", "params": {"threadId": thread_id, "turn": {"id": "turn-1", "status": "completed"}}}), flush=True)
'''
        client = CodexAppServer(
            [sys.executable, "-u", "-c", server_script], timeout_seconds=2
        )
        try:
            self.assertTrue(client.is_running is False)
            self.assertEqual(client.initialize()["protocol"], 1)
            resumed = client.resume_thread("thread-1")
            self.assertEqual(resumed["result"]["threadId"], "thread-1")
            started = client.start_turn("thread-1", "继续")
            self.assertEqual(started["result"]["turnId"], "turn-1")
            event = client.listen_turn_completed("thread-1", timeout_seconds=1)
            self.assertEqual(event.status, "completed")
            self.assertTrue(event.is_terminal)
        finally:
            client.close()

    def test_turn_steer_requires_expected_turn_id_and_uses_structured_input(self) -> None:
        server_script = r'''
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        print(json.dumps({"id": message["id"], "result": {}}), flush=True)
    elif method == "turn/steer":
        params = message["params"]
        assert params == {
            "threadId": "thread-1",
            "expectedTurnId": "turn-active",
            "input": [{"type": "text", "text": "extra input"}],
        }
        print(json.dumps({"id": message["id"], "result": {"turnId": "turn-active"}}), flush=True)
'''
        client = CodexAppServer(
            [sys.executable, "-u", "-c", server_script], timeout_seconds=2
        )
        try:
            response = client.steer_turn("thread-1", "turn-active", "extra input")
            self.assertEqual(response["result"]["turnId"], "turn-active")
            with self.assertRaises(ValueError):
                client.steer_turn("thread-1", "", "不能发送")
        finally:
            client.close()
            client.close()
        self.assertFalse(client.is_running)

    def test_timeout_is_bounded(self) -> None:
        server_script = r'''
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    if message.get("method") == "initialize":
        print(json.dumps({"id": message.get("id"), "result": {}}), flush=True)
'''
        client = CodexAppServer(
            [sys.executable, "-u", "-c", server_script], timeout_seconds=1
        )
        try:
            client.start()
            with self.assertRaises(CodexRPCTimeout):
                client.listen_turn_completed(timeout_seconds=0.05)
        finally:
            client.close()

    def test_server_request_is_answered_on_the_same_connection(self) -> None:
        server_script = r'''
import json, sys
thread_id = "thread-1"
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        print(json.dumps({"id": request_id, "result": {}}), flush=True)
    elif method == "thread/resume":
        print(json.dumps({"id": request_id, "result": {"threadId": thread_id}}), flush=True)
    elif method == "turn/start":
        print(json.dumps({"id": request_id, "result": {"turnId": "turn-1"}}), flush=True)
        print(json.dumps({
            "id": "approval-1",
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": thread_id, "turnId": "turn-1", "reason": "测试"},
        }), flush=True)
    elif request_id == "approval-1" and message.get("result") == {"decision": "accept"}:
        print(json.dumps({
            "method": "turn/completed",
            "params": {"threadId": thread_id, "turn": {"id": "turn-1", "status": "completed"}},
        }), flush=True)
'''
        client = CodexAppServer(
            [sys.executable, "-u", "-c", server_script], timeout_seconds=2
        )
        try:
            client.resume_thread("thread-1")
            client.start_turn("thread-1", "继续")
            request = client.listen_event("thread-1", timeout_seconds=1)
            self.assertIsInstance(request, ServerRequest)
            assert isinstance(request, ServerRequest)
            self.assertEqual(request.method, "item/commandExecution/requestApproval")
            client.respond(request.request_id, {"decision": "accept"})
            completed = client.listen_turn_completed("thread-1", timeout_seconds=1)
            self.assertEqual(completed.status.value, "completed")
        finally:
            client.close()

    def test_server_request_can_precede_turn_start_response(self) -> None:
        server_script = r'''
import json, sys
start_request_id = None
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        print(json.dumps({"id": request_id, "result": {}}), flush=True)
    elif method == "turn/start":
        start_request_id = request_id
        print(json.dumps({
            "id": "approval-before-response",
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "thread-1", "turnId": "turn-early"},
        }), flush=True)
    elif request_id == "approval-before-response":
        print(json.dumps({"id": start_request_id, "result": {"turnId": "turn-early"}}), flush=True)
        print(json.dumps({
            "method": "turn/completed",
            "params": {"threadId": "thread-1", "turn": {"id": "turn-early", "status": "completed"}},
        }), flush=True)
'''
        client = CodexAppServer(
            [sys.executable, "-u", "-c", server_script], timeout_seconds=2
        )
        handled: list[str] = []
        try:
            def approve(request: ServerRequest) -> None:
                handled.append(request.method)
                client.respond(request.request_id, {"decision": "accept"})

            response = client.start_turn(
                "thread-1",
                "继续",
                on_server_request=approve,
            )
            self.assertEqual(response["result"]["turnId"], "turn-early")
            self.assertEqual(handled, ["item/commandExecution/requestApproval"])
            completed = client.listen_turn_completed(
                "thread-1", turn_id="turn-early", timeout_seconds=1
            )
            self.assertTrue(completed.is_terminal)
        finally:
            client.close()

    def test_completed_only_api_fails_fast_on_server_request(self) -> None:
        server_script = r'''
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    if message.get("method") == "initialize":
        print(json.dumps({"id": message["id"], "result": {}}), flush=True)
    elif message.get("method") == "turn/start":
        print(json.dumps({"id": message["id"], "result": {}}), flush=True)
        print(json.dumps({
            "id": 99,
            "method": "item/fileChange/requestApproval",
            "params": {"threadId": "thread-1", "turnId": "turn-1"},
        }), flush=True)
'''
        client = CodexAppServer(
            [sys.executable, "-u", "-c", server_script], timeout_seconds=2
        )
        try:
            client.start_turn("thread-1", "修改")
            with self.assertRaises(CodexRPCUnhandledRequest):
                client.listen_turn_completed("thread-1", timeout_seconds=1)
        finally:
            client.close()

    def test_event_filter_binds_both_thread_and_turn(self) -> None:
        server_script = r'''
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    if message.get("method") == "initialize":
        print(json.dumps({"id": message["id"], "result": {}}), flush=True)
        print(json.dumps({
            "method": "turn/completed",
            "params": {"threadId": "thread-1", "turn": {"id": "old-turn", "status": "completed"}},
        }), flush=True)
        print(json.dumps({
            "method": "turn/completed",
            "params": {"threadId": "thread-1", "turn": {"id": "new-turn", "status": "failed"}},
        }), flush=True)
'''
        client = CodexAppServer(
            [sys.executable, "-u", "-c", server_script], timeout_seconds=2
        )
        try:
            event = client.listen_event(
                "thread-1", turn_id="new-turn", timeout_seconds=1
            )
            self.assertIsInstance(event, TurnCompletedEvent)
            assert isinstance(event, TurnCompletedEvent)
            self.assertEqual(event.turn_id, "new-turn")
            old = client.listen_event(
                "thread-1", turn_id="old-turn", timeout_seconds=1
            )
            self.assertIsInstance(old, TurnCompletedEvent)
        finally:
            client.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
