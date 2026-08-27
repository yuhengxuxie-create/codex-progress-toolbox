"""不接触真实飞书或 Codex 的完整双向链路集成测试。"""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys
import threading
import time

from progress_wx.codex_store import ThreadRecord, ThreadSnapshot, ThreadStatus
from progress_wx.channel import ChannelReply
from progress_wx.config import load_config
from progress_wx.models import ProgressReport
from progress_wx.service import ProgressService
from progress_wx.state import CorrelationCodec, StateStore


class IntegrationChannel:
    """记录出站文本及平台 message_id，并允许等待下一条消息。"""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.message_ids: list[str] = []
        self._condition = threading.Condition()

    def send_text(self, text: str, *, idempotency_key: str) -> str:
        assert idempotency_key
        with self._condition:
            self.messages.append(text)
            message_id = f"om-e2e-{len(self.messages)}"
            self.message_ids.append(message_id)
            self._condition.notify_all()
            return message_id

    def is_online(self) -> bool:
        return True

    def wait_for_count(self, count: int, timeout: float = 5) -> None:
        deadline = time.monotonic() + timeout
        with self._condition:
            while len(self.messages) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"等待第 {count} 条渠道消息超时")
                self._condition.wait(remaining)


class IntegrationCodexStore:
    """只提供服务编排所需的结构化对话元数据。"""

    def __init__(self) -> None:
        self.thread = ThreadRecord("thread-e2e", title="离线端到端", cwd="D:/e2e")

    def get_thread(self, thread_id: str):
        return self.thread if thread_id == self.thread.thread_id else None

    def select_threads(self, *, title=None, cwd=None, **_kwargs):
        if title is not None and title != self.thread.title:
            return []
        if cwd is not None and cwd != self.thread.cwd:
            return []
        return [self.thread]

    def snapshot(self, _thread_id: str):
        return ThreadSnapshot(self.thread, None, ThreadStatus.UNKNOWN, True, True)

    def require_readable(self, _operation: str = ""):
        # 集成替身没有真实 SQLite；真实 CodexStore 会在这里提升结构化读取错误。
        return None

    def status(self, _thread_id: str):
        return ThreadStatus.COMPLETED


class IntegrationSummarizer:
    def summarize(self, event, *, wait=None):
        del wait
        return ProgressReport("完成", f"已处理结构化轮次 {event.turn_id}")


def _fake_app_server_script() -> str:
    """返回与 codex_rpc 子进程测试一致的 JSONL 替身脚本。"""

    return r'''
import json
from pathlib import Path
import sys

# Codex App Server 的 JSONL 协议是 UTF-8；Windows Python 替身不得依赖本机代码页。
sys.stdin.reconfigure(encoding="utf-8", errors="strict")
sys.stdout.reconfigure(encoding="utf-8", errors="strict")
log_path = Path(sys.argv[1])

def record(value):
    with log_path.open("a", encoding="utf-8") as handle:
        # 诊断日志必须能保存任意 JSON 字符，包括 Windows 管道中的代理码。
        handle.write(json.dumps(value, ensure_ascii=True) + "\n")

for line in sys.stdin:
    message = json.loads(line)
    try:
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            print(json.dumps({"id": request_id, "result": {"protocol": "e2e"}}), flush=True)
        elif method == "thread/resume":
            print(json.dumps({"id": request_id, "result": {"threadId": message["params"]["threadId"]}}), flush=True)
        elif method == "turn/start":
            record({"turn_input": message["params"]["input"]})
            print(json.dumps({"id": request_id, "result": {"turnId": "turn-from-channel"}}), flush=True)
            print(json.dumps({
                "id": "approval-e2e",
                "method": "item/commandExecution/requestApproval",
                "params": {
                    "threadId": "thread-e2e",
                    "turnId": "turn-from-channel",
                    "reason": "离线集成测试",
                },
            }), flush=True)
        elif request_id == "approval-e2e":
            record({"approval": message.get("result")})
            print(json.dumps({
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-e2e",
                    "turn": {"id": "turn-from-channel", "status": "completed"},
                },
            }), flush=True)
    except Exception as exc:
        record({"fake_error": type(exc).__name__, "detail": str(exc), "message": message})
        raise
'''


def _wait_until(predicate, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("等待离线端到端状态变化超时")
        time.sleep(0.01)


def test_hook_quote_approval_and_completion_end_to_end(tmp_path: Path) -> None:
    server_log = tmp_path / "fake_app_server.jsonl"
    server_script = _fake_app_server_script()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
codex:
  home: "{tmp_path.as_posix()}"
  command: codex
  reply_timeout_seconds: 30
monitor:
  ids: [thread-e2e]
messaging:
  backend: fake
  require_quote: true
  secret_file: "{(tmp_path / 'secret').as_posix()}"
service:
  max_attempts: 5
  retry_delays: [0, 0, 0, 0, 0]
  database: "{(tmp_path / 'state.sqlite').as_posix()}"
  log_dir: "{(tmp_path / 'logs').as_posix()}"
summary:
  mode: codex_final
""",
        encoding="utf-8",
    )
    loaded = load_config(config_path)
    loaded.validate_ready()
    command = (sys.executable, "-u", "-c", server_script, str(server_log))
    config = replace(loaded, codex=replace(loaded.codex, command=command))

    store = StateStore(config.service.database)
    codec = CorrelationCodec(b"z" * 32)
    channel = IntegrationChannel()
    service = ProgressService(config.path)
    service.config = config
    service.store = store
    service.codec = codec
    service.channel = channel
    service.codex_store = IntegrationCodexStore()  # type: ignore[assignment]
    service.summarizer = IntegrationSummarizer()  # type: ignore[assignment]

    store.enqueue_hook_payload(
        {
            "type": "agent-turn-complete",
            "thread-id": "thread-e2e",
            "turn-id": "turn-initial",
            "last-assistant-message": "第一轮结构化结果",
        }
    )
    service._poll_once(config)
    channel.wait_for_count(1)
    assert "对话名称：离线端到端" in channel.messages[0]
    assert "本条消息时间：" in channel.messages[0]

    service._on_channel_reply(
        ChannelReply(
            sender_id="integration-user",
            content="继续开发",
            reply_to_message_id=channel.message_ids[0],
            message_id="reply-1",
            chat_id="integration-chat",
        )
    )
    queued_job = service.reply_queue.get_nowait()
    assert queued_job is not None
    assert [ord(character) for character in queued_job.reply_text] == [32487, 32493, 24320, 21457]
    service.reply_queue.put(queued_job)
    service.reply_thread = threading.Thread(target=service._reply_worker, daemon=True)
    service.reply_thread.start()
    _wait_until(lambda: len(channel.messages) >= 2 or service._fatal is not None)
    diagnostic = server_log.read_text(encoding="utf-8") if server_log.exists() else "<no fake log>"
    assert service._fatal is None, diagnostic
    assert "当前进度：待审批" in channel.messages[1]
    service._on_channel_reply(
        ChannelReply(
            sender_id="integration-user",
            content="A",
            reply_to_message_id=channel.message_ids[1],
            message_id="reply-2",
            chat_id="integration-chat",
        )
    )

    def turn_finished() -> bool:
        with service._active_rpc_lock:
            return service._active_rpc is None and server_log.exists()

    _wait_until(turn_finished)
    records = [json.loads(line) for line in server_log.read_text(encoding="utf-8").splitlines()]
    assert records == [
        {"turn_input": [{"type": "text", "text": "继续开发"}]},
        {"approval": {"decision": "accept"}},
    ]
    assert service._fatal is None

    store.enqueue_hook_payload(
        {
            "type": "agent-turn-complete",
            "thread-id": "thread-e2e",
            "turn-id": "turn-from-channel",
            "last-assistant-message": "消息渠道发起轮次完成",
        }
    )
    service._poll_once(config)
    channel.wait_for_count(3)
    assert "已处理结构化轮次 turn-from-channel" in channel.messages[2]

    service.stop_event.set()
    service.reply_queue.put(None)
    service.reply_thread.join(timeout=2)
    assert not service.reply_thread.is_alive()
    store.close()
