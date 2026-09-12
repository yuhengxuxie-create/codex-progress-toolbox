from __future__ import annotations

import hashlib
import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from progress_wx.channel import ChannelAttachment, ChannelReply
from progress_wx.codex_management import CodexManagementController
from progress_wx.codex_store import ThreadRecord
from progress_wx.models import TurnEvent
from progress_wx.service import ProgressService
from progress_wx.state import CorrelationCodec, StateStore


class _CaptureSender:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []

    def __call__(self, text: str, key: str) -> tuple[str, ...]:
        message_id = f"om-control-{len(self.messages) + 1}"
        self.messages.append((message_id, text, key))
        return (message_id,)


class _DesktopTools:
    def __init__(self) -> None:
        self.created: list[tuple[str, dict[str, object]]] = []
        self.sent: list[tuple[str, str]] = []
        self.closed = 0

    def list_threads(self, _source: str, *, limit: int = 50, call_tag: str = ""):
        del limit, call_tag
        return {
            "pinnedThreads": [],
            "threads": [
                {
                    "id": "thread-personal",
                    "kind": "codex",
                    "hostId": "local",
                    "status": "idle",
                    "updatedAt": 100,
                    "title": "测试会话",
                    "summary": "测试会话",
                }
            ],
        }

    def create_thread(
        self,
        _source: str,
        prompt: str,
        target: dict[str, object],
        *,
        title: str = "",
        call_tag: str = "",
    ):
        del title, call_tag
        self.created.append((prompt, target))
        return {"threadId": f"created-{len(self.created)}", "hostId": "local"}

    def send_message(
        self,
        thread_id: str,
        prompt: str,
        *,
        call_tag: str,
        source_thread_id: str,
        host_id: str,
    ):
        del call_tag, source_thread_id, host_id
        self.sent.append((thread_id, prompt))
        return {"success": True}

    def close(self) -> None:
        self.closed += 1


class _DesktopClient:
    def __init__(self, tools: _DesktopTools) -> None:
        self.tools = tools

    def open_verified(self, *, required_tools: tuple[str, ...]):
        del required_tools
        return self.tools


class _CodexStore:
    def __init__(self) -> None:
        self.records = {
            "thread-personal": ThreadRecord(
                "thread-personal", "测试会话", r"D:\\Codex", 100, 1
            )
        }

    def get_thread(self, thread_id: str):
        return self.records.get(thread_id)


@dataclass
class _Harness:
    state: StateStore
    codec: CorrelationCodec
    sender: _CaptureSender
    tools: _DesktopTools
    controller: CodexManagementController
    service: ProgressService

    def close(self) -> None:
        self.state.close()


def _harness(tmp_path: Path) -> _Harness:
    state = StateStore(tmp_path / "state.sqlite")
    codec = CorrelationCodec(b"d" * 32)
    sender = _CaptureSender()
    tools = _DesktopTools()
    controller = CodexManagementController(
        store=state,
        codex_store=_CodexStore(),
        desktop_client=_DesktopClient(tools),
        project_registry=object(),
        source_thread_ids=("source-thread",),
        send_text=sender,
    )

    # This uses the production service class and its routing methods while
    # keeping the channel/configuration local and side-effect free.
    service = object.__new__(ProgressService)
    service.config = SimpleNamespace(
        messaging=SimpleNamespace(backend="feishu"),
        feishu=SimpleNamespace(target_open_id="ou_owner"),
        service=SimpleNamespace(database=tmp_path / "state.sqlite"),
    )
    service.store = state
    service.codec = codec
    # The service route only needs the signed StateStore mapping here.  Leave
    # its optional Codex store unset so the test does not invent a Desktop
    # record for the synthetic notification thread.
    service.codex_store = None
    service.management = controller
    service.channel = None
    service.user_reply_chain = None
    service.parent_recovery_thread = None
    service.stop_event = threading.Event()
    service.management_queue = queue.Queue()
    service.parent_recovery_queue = queue.Queue()
    service.reply_queue = queue.Queue()
    service.receipt_queue = queue.Queue()
    service._reply_schedule_lock = threading.Lock()
    service._scheduled_reply_codes = set()
    service._deferred_reply_codes = set()
    service._pending_server_replies = {}
    service.approval_bridge = None
    service._active_rpc_lock = threading.RLock()
    service._active_rpc = None
    service._fatal = None
    service._pending_lock = threading.RLock()
    return _Harness(state, codec, sender, tools, controller, service)


def _message(
    message_id: str,
    content: str,
    *,
    reply_to: str = "",
    source_kind: str = "message",
    chat_id: str = "oc_private",
    attachments: tuple[ChannelAttachment, ...] = (),
) -> ChannelReply:
    return ChannelReply(
        sender_id="ou_owner",
        content=content,
        reply_to_message_id=reply_to,
        message_id=message_id,
        chat_id=chat_id,
        source_kind=source_kind,
        attachments=attachments,
    )


def _drain_management(harness: _Harness) -> ChannelReply:
    message = harness.service.management_queue.get_nowait()
    harness.controller.handle(message)
    return message


def _seed_notification(
    harness: _Harness,
    *,
    thread_id: str = "thread-staged",
    parent_id: str = "om-staged-parent",
) -> None:
    event = TurnEvent(thread_id, "turn-staged", "completed", title="暂存测试")
    code = harness.codec.issue()
    harness.state.reserve_notification(event, code, "暂存测试通知", 72)
    harness.state.bind_channel_message(event.dedupe_key, parent_id)
    harness.state.mark_sent(event.dedupe_key)


def _seed_image(tmp_path: Path) -> ChannelAttachment:
    path = tmp_path / "feishu-media" / "staged.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = b"\x89PNG\r\n\x1a\ncommand-boundary"
    path.write_bytes(data)
    return ChannelAttachment(
        str(path.resolve()),
        "image/png",
        hashlib.sha256(data).hexdigest(),
        len(data),
    )


def test_service_and_controller_require_dot_for_natural_language_entries(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    try:
        legacy = _message("legacy-entry", "查询会话")
        assert harness.controller.accepts(legacy) is True
        assert harness.service._on_channel_reply(legacy) is False
        _drain_management(harness)
        assert "半角 ." in harness.sender.messages[-1][1]
        assert "没有执行操作" in harness.sender.messages[-1][1]
        assert not harness.tools.created

        current = _message("dot-entry", ".查询会话")
        assert harness.controller.accepts(current) is True
        assert harness.service._on_channel_reply(current) is False
        _drain_management(harness)
        assert "查询 Codex" in harness.sender.messages[-1][1]
        assert "半角 ." not in harness.sender.messages[-1][1]
    finally:
        harness.close()


def test_unknown_dot_is_claimed_and_never_becomes_codex_prompt(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    try:
        unknown = _message("unknown-dot", ".完全未知入口")
        assert harness.controller.accepts(unknown) is True
        assert harness.service._on_channel_reply(unknown) is False
        _drain_management(harness)
        assert "没有向 Codex 发送正文" in harness.sender.messages[-1][1]
        with pytest.raises(queue.Empty):
            harness.service.reply_queue.get_nowait()
    finally:
        harness.close()


@pytest.mark.parametrize(
    "content",
    [
        ".查询会话",
        "查询会话",
        ".新建个人会话",
        ".功能中心",
        "/status",
        "$skill-one 请求",
        ".未知",
        "..查询会话",
        ".取消 ",
        ".发送\n/多行点",
    ],
)
def test_control_candidates_do_not_consume_staged_image(
    tmp_path: Path, content: str
) -> None:
    harness = _harness(tmp_path)
    try:
        _seed_notification(harness)
        attachment = _seed_image(tmp_path)
        staged_message = _message(
            "image-stage", "", reply_to="om-staged-parent", attachments=(attachment,)
        )
        assert harness.service._on_channel_reply(staged_message) is False
        receipt = harness.service.receipt_queue.get_nowait()
        assert receipt.received is True
        before = harness.state.staged_image_reply("ou_owner", "oc_private")
        assert before is not None

        command = _message(f"control-{content}", content)
        assert harness.service._on_channel_reply(command) is False
        after = harness.state.staged_image_reply("ou_owner", "oc_private")
        assert after == before
        queued = harness.service.management_queue.get_nowait()
        assert queued.content == content
        assert queued.attachments == ()
        with pytest.raises(queue.Empty):
            harness.service.reply_queue.get_nowait()
    finally:
        harness.close()


@pytest.mark.parametrize("content", [".发送", ".取消"])
def test_stage_special_without_staged_image_is_explicit_and_never_codex(
    tmp_path: Path, content: str
) -> None:
    harness = _harness(tmp_path)
    try:
        message = _message(f"no-stage-{content}", content)
        assert harness.service._on_channel_reply(message) is False
        _drain_management(harness)
        assert "暂存图片" in harness.sender.messages[-1][1]
        assert "没有向 Codex 发送正文" in harness.sender.messages[-1][1]
        with pytest.raises(queue.Empty):
            harness.service.reply_queue.get_nowait()
    finally:
        harness.close()


def test_staged_image_only_explicit_send_and_cancel_are_the_only_consumers(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    try:
        _seed_notification(harness)
        attachment = _seed_image(tmp_path)
        assert harness.service._on_channel_reply(
            _message("stage-for-send", "", reply_to="om-staged-parent", attachments=(attachment,))
        ) is False
        harness.service.receipt_queue.get_nowait()
        assert harness.state.staged_image_reply("ou_owner", "oc_private") is not None

        assert harness.service._on_channel_reply(_message("send-staged", ".发送")) is True
        send_job = harness.service.reply_queue.get_nowait()
        assert send_job.thread_id == "thread-staged"
        assert harness.state.staged_image_reply("ou_owner", "oc_private") is None
        harness.service.receipt_queue.get_nowait()

        _seed_notification(
            harness, thread_id="thread-cancel", parent_id="om-cancel-parent"
        )
        attachment = _seed_image(tmp_path)
        assert harness.service._on_channel_reply(
            _message("stage-for-cancel", "", reply_to="om-cancel-parent", attachments=(attachment,))
        ) is False
        harness.service.receipt_queue.get_nowait()
        assert harness.state.staged_image_reply("ou_owner", "oc_private") is not None
        assert harness.service._on_channel_reply(_message("cancel-staged", ".取消")) is False
        assert harness.state.staged_image_reply("ou_owner", "oc_private") is None
        cancel_receipt = harness.service.receipt_queue.get_nowait()
        assert "没有向 Codex 发送" in cancel_receipt.details
    finally:
        harness.close()


def test_new_personal_plain_reply_is_submitted_byte_for_byte_in_context(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    try:
        entry = _message("new-personal-entry", ".新建个人会话")
        assert harness.service._on_channel_reply(entry) is False
        _drain_management(harness)
        form_message_id = harness.sender.messages[-1][0]
        context = harness.state.management_context_record_for_message(form_message_id)
        assert context is not None and context.context_kind == "new_personal_thread_form"

        prompt = "  保留前导空格\n以及换行和末尾空格  "
        reply = _message("new-personal-plain", prompt, reply_to=form_message_id)
        assert harness.controller.accepts(reply) is True
        assert harness.service._on_channel_reply(reply) is False
        _drain_management(harness)
        assert harness.tools.created == [(prompt.replace("\r\n", "\n"), {"type": "projectless"})]
    finally:
        harness.close()


def test_existing_thread_context_keeps_plain_body_as_codex_prompt(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    try:
        context_id = harness.state.create_management_context(
            "thread_overview",
            {"thread": {"id": "thread-personal", "title": "测试会话"}, "group": "个人会话"},
            sender_id="ou_owner",
            chat_id="oc_private",
        )
        harness.state.bind_management_messages(context_id, ("om-thread-overview",))
        prompt = "普通正文 /goal 不是远程控制"
        reply = _message("thread-plain", prompt, reply_to="om-thread-overview")
        assert harness.controller.accepts(reply) is True
        assert harness.service._on_channel_reply(reply) is False
        _drain_management(harness)
        assert harness.tools.sent == [("thread-personal", prompt)]
    finally:
        harness.close()


def test_bot_menu_source_executes_fixed_entry_but_same_text_message_is_legacy_hint(
    tmp_path: Path,
) -> None:
    (tmp_path / "ordinary").mkdir()
    menu = _harness(tmp_path)
    ordinary = _harness(tmp_path / "ordinary")
    try:
        menu_message = _message(
            "menu-query", "查询会话", source_kind="bot_menu", chat_id=""
        )
        assert menu.controller.accepts(menu_message) is True
        assert menu.service._on_channel_reply(menu_message) is False
        _drain_management(menu)
        assert menu.sender.messages
        assert "半角 ." not in menu.sender.messages[-1][1]
        assert "查询 Codex" in menu.sender.messages[-1][1]

        ordinary_message = _message("ordinary-query", "查询会话")
        assert ordinary.controller.accepts(ordinary_message) is True
        assert ordinary.service._on_channel_reply(ordinary_message) is False
        _drain_management(ordinary)
        assert "半角 ." in ordinary.sender.messages[-1][1]
        assert "没有执行操作" in ordinary.sender.messages[-1][1]
    finally:
        menu.close()
        ordinary.close()


@pytest.mark.parametrize("context_kind", ["thread_overview", "new_personal_thread_form"])
def test_unknown_dot_reply_to_context_never_becomes_prompt(
    tmp_path: Path, context_kind: str
) -> None:
    harness = _harness(tmp_path)
    try:
        if context_kind == "thread_overview":
            context_id = harness.state.create_management_context(
                context_kind,
                {"thread": {"id": "thread-personal", "title": "测试会话"}, "group": "个人会话"},
                sender_id="ou_owner",
                chat_id="oc_private",
            )
            parent_id = "om-unknown-overview"
        else:
            entry = _message("unknown-form-entry", ".新建个人会话")
            assert harness.service._on_channel_reply(entry) is False
            _drain_management(harness)
            parent_id = harness.sender.messages[-1][0]
            context_id = harness.state.management_context_record_for_message(parent_id).context_id
        harness.state.bind_management_messages(context_id, (parent_id,))
        unknown = _message(
            f"unknown-context-{context_kind}", ".未知上下文操作", reply_to=parent_id
        )
        assert harness.service._on_channel_reply(unknown) is False
        queued = harness.service.management_queue.get_nowait()
        assert queued.content == ".未知上下文操作"
        assert queued.attachments == ()
        harness.controller.handle(queued)
        assert not harness.tools.sent
        assert not harness.tools.created
        assert "没有向 Codex 发送正文" in harness.sender.messages[-1][1]
    finally:
        harness.close()
