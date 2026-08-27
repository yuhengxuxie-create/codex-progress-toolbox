from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from progress_wx.channel import ChannelAttachment, ChannelReply
from progress_wx.codex_app_tools import DesktopAppToolsError
from progress_wx.codex_management import (
    CodexManagementController,
    ManagementUserError,
    _latest_final,
    _parse_form,
)
from progress_wx.codex_projects import (
    CodexProjectRegistry,
    LocalProject,
    ProjectRegistrySnapshot,
)
from progress_wx.codex_store import ThreadRecord, ThreadStatus, TurnRecord
from progress_wx.models import GeneratedImageArtifact
from progress_wx.state import StateStore


class FakeCodexStore:
    def __init__(self, records: list[ThreadRecord]) -> None:
        self.records = records
        self.turns: dict[str, TurnRecord] = {}

    def select_threads(self, *, include_archived: bool = False):
        del include_archived
        return list(self.records)

    def require_readable(self, _operation: str) -> None:
        return None

    def get_thread(self, thread_id: str):
        return next((item for item in self.records if item.thread_id == thread_id), None)

    def latest_turn(self, thread_id: str):
        return self.turns.get(thread_id)


class FakeRegistry:
    def __init__(self) -> None:
        self.project = LocalProject("project-1", "飞书机器人", (r"D:\Bot",), 1, 1)
        self.registered_names: list[str] = []
        self.extra_assignments: dict[str, str] = {}

    def snapshot(self) -> ProjectRegistrySnapshot:
        return ProjectRegistrySnapshot(
            (self.project,),
            {"thread-project": "project-1", **self.extra_assignments},
            frozenset({"thread-personal"}),
        )

    def register(self, name: str) -> LocalProject:
        self.registered_names.append(name)
        self.project = LocalProject("project-1", name, (r"D:\Bot",), 1, 1)
        return self.project


class FakeDesktopTools:
    def __init__(self) -> None:
        self.sent_prompts: list[tuple[str, str, str, str]] = []
        self.created: list[tuple[str, dict, str]] = []
        self.closed = 0

    def list_threads(self, _source: str, *, limit: int = 50, call_tag: str = ""):
        del limit, call_tag
        return {
            "pinnedThreads": [],
            "threads": [
                {
                    "id": "thread-project",
                    "kind": "codex",
                    "projectId": "project-1",
                    "hostId": "local",
                    "status": "idle",
                    "updatedAt": 200,
                    "title": "项目会话",
                    "summary": "已完成飞书路由和分页。",
                },
                {
                    "id": "thread-personal",
                    "kind": "codex",
                    "projectId": None,
                    "hostId": "local",
                    "status": "idle",
                    "updatedAt": 100,
                    "title": "个人会话",
                    "summary": "个人任务概览。",
                },
            ],
        }

    def list_projects(self, _source: str):
        return {
            "projects": [
                {
                    "projectId": "project-1",
                    "projectKind": "local",
                    "label": "飞书机器人",
                    "path": r"D:\Bot",
                    "hostId": "local",
                    "isGitRepository": False,
                }
            ]
        }

    def read_thread(self, _source: str, thread_id: str, **_kwargs):
        assert thread_id == "thread-project"
        return {
            "thread": {"id": thread_id, "status": {"type": "idle"}},
            "turns": [
                {
                    "items": [
                        {
                            "type": "agentMessage",
                            "phase": "final_answer",
                            "text": "完整测试通过，服务已经启动。",
                        }
                    ]
                }
            ],
        }

    def send_message(
        self,
        thread_id: str,
        prompt: str,
        *,
        call_tag: str,
        source_thread_id: str,
        host_id: str,
    ):
        self.sent_prompts.append((thread_id, prompt, source_thread_id, host_id))
        return {"success": True, "callTag": call_tag}

    def create_thread(
        self,
        _source: str,
        prompt: str,
        target: dict,
        *,
        title: str = "",
        call_tag: str = "",
    ):
        del call_tag
        self.created.append((prompt, target, title))
        return {"threadId": f"created-{len(self.created)}", "hostId": "local"}

    def close(self) -> None:
        self.closed += 1


class FakeDesktopClient:
    def __init__(self, tools: FakeDesktopTools) -> None:
        self.tools = tools
        self.required: list[tuple[str, ...]] = []

    def open_verified(self, *, required_tools: tuple[str, ...]):
        self.required.append(required_tools)
        return self.tools


class CaptureSender:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []

    def __call__(self, text: str, key: str) -> tuple[str, ...]:
        message_id = f"om_{len(self.messages) + 1}"
        self.messages.append((message_id, text, key))
        return (message_id,)


class CaptureFileSender:
    def __init__(self) -> None:
        self.files: list[tuple[str, bytes, str, str]] = []

    def __call__(self, data: bytes, file_name: str, key: str) -> tuple[str, ...]:
        message_id = f"om_image_{len(self.files) + 1}"
        self.files.append((message_id, data, file_name, key))
        return (message_id,)


class CaptureImageSender:
    def __init__(self) -> None:
        self.images: list[tuple[str, bytes, str]] = []

    def __call__(self, data: bytes, key: str) -> tuple[str, ...]:
        message_id = f"om_image_{len(self.images) + 1}"
        self.images.append((message_id, data, key))
        return (message_id,)


def _message(
    message_id: str,
    content: str,
    *,
    reply_to: str = "",
    attachments: tuple[ChannelAttachment, ...] = (),
) -> ChannelReply:
    return ChannelReply(
        sender_id="ou_owner",
        content=content,
        reply_to_message_id=reply_to,
        message_id=message_id,
        chat_id="oc_private",
        attachments=attachments,
    )


def _controller(
    tmp_path: Path,
    *,
    image_sender: CaptureImageSender | None = None,
    file_sender: CaptureFileSender | None = None,
    account_reader=None,
):
    state = StateStore(tmp_path / "state.sqlite")
    tools = FakeDesktopTools()
    sender = CaptureSender()
    records = [
        ThreadRecord("thread-project", "项目会话", r"D:\Bot", 200000, 1, preview="项目预览"),
        ThreadRecord("thread-personal", "个人会话", r"C:\Personal", 100000, 1, preview="个人预览"),
    ]
    controller = CodexManagementController(
        store=state,
        codex_store=FakeCodexStore(records),
        desktop_client=FakeDesktopClient(tools),
        project_registry=FakeRegistry(),
        source_thread_ids=("source-thread",),
        send_text=sender,
        send_image=image_sender,
        send_file=file_sender,
        account_reader=account_reader,
    )
    return controller, state, tools, sender


def test_quota_is_an_unquoted_top_level_command(tmp_path: Path) -> None:
    from progress_wx.codex_account import AccountRateLimits, RateLimitWindow

    class FakeAccountReader:
        def read(self):
            return AccountRateLimits(
                (
                    RateLimitWindow(
                        "codex", "Codex", "每周额度", 12, 88, 10080, 1_800_000_000
                    ),
                ),
                0,
                (),
            )

    controller, state, tools, sender = _controller(
        tmp_path, account_reader=FakeAccountReader()
    )
    try:
        message = _message("quota-1", "查询剩余额度")
        assert controller.accepts(message) is True
        controller.handle(message)
        assert sender.messages[-1][1] == "Codex 每周额度：88%\n剩余重置卡：0 张"
        assert tools.closed == 0
    finally:
        state.close()


def test_project_history_context_selects_and_continues_exact_prompt(tmp_path: Path) -> None:
    controller, state, tools, sender = _controller(tmp_path)
    try:
        controller.handle(_message("in-1", "查询项目列表"))
        assert "A01｜飞书机器人｜1 个会话" in sender.messages[-1][1]

        controller.handle(_message("in-2", "展开A01", reply_to="om_1"))
        assert "a01｜项目会话" in sender.messages[-1][1]

        controller.handle(_message("in-3", "选定a01", reply_to="om_2"))
        overview = sender.messages[-1][1]
        assert "整体概览：已完成飞书路由和分页。" in overview
        assert "最后一轮结果：完整测试通过，服务已经启动。" in overview

        exact_prompt = "  第一行\n第二行  "
        controller.handle(_message("in-4", exact_prompt, reply_to="om_3"))
        assert tools.sent_prompts == [
            ("thread-project", exact_prompt, "source-thread", "local")
        ]
        assert "提交状态：正文已原样送达" in sender.messages[-1][1]

        # 回复较早的项目列表消息仍使用当时的不可变上下文。
        controller.handle(_message("in-5", "展开A01", reply_to="om_1"))
        assert "项目名称：A01｜飞书机器人" in sender.messages[-1][1]
    finally:
        state.close()


def test_selected_session_falls_back_to_local_turn_when_desktop_detail_is_unreadable(
    tmp_path: Path,
) -> None:
    controller, state, tools, sender = _controller(tmp_path)
    controller.codex_store.turns["thread-project"] = TurnRecord(
        thread_id="thread-project",
        turn_id="turn-local-final",
        status=ThreadStatus.COMPLETED,
        final_message="本地结构化最终结果仍可正常展示。",
    )

    def unreadable_detail(*_args, **_kwargs):
        raise DesktopAppToolsError("Codex app tool request failed")

    tools.read_thread = unreadable_detail
    try:
        controller.handle(_message("in-1", "查询项目列表"))
        controller.handle(_message("in-2", "展开A01", reply_to="om_1"))
        controller.handle(_message("in-3", "选定a01", reply_to="om_2"))

        overview = sender.messages[-1][1]
        assert "会话名称：项目会话" in overview
        assert "整体概览：已完成飞书路由和分页。" in overview
        assert "最后一轮结果：本地结构化最终结果仍可正常展示。" in overview
        assert "Codex Desktop 当前无法完成" not in overview

        controller.handle(_message("in-4", "继续执行下一步", reply_to="om_3"))
        assert tools.sent_prompts == [
            ("thread-project", "继续执行下一步", "source-thread", "local")
        ]
    finally:
        state.close()


def test_selected_session_sends_latest_generated_image_and_binds_same_context(
    tmp_path: Path,
) -> None:
    image_sender = CaptureImageSender()
    controller, state, tools, sender = _controller(tmp_path, image_sender=image_sender)
    image_path = tmp_path / "item-image.png"
    original = b"\x89PNG\r\n\x1a\nexact-session-overview-image"
    image_path.write_bytes(original)
    artifact = GeneratedImageArtifact(
        item_id="item-image",
        path=str(image_path),
        mime_type="image/png",
        sha256=hashlib.sha256(original).hexdigest(),
        size=len(original),
        file_name=image_path.name,
    )
    controller.codex_store.turns["thread-project"] = TurnRecord(
        thread_id="thread-project",
        turn_id="turn-image",
        status=ThreadStatus.COMPLETED,
        generated_images=(artifact,),
    )
    try:
        controller.handle(_message("in-1", "查询项目列表"))
        controller.handle(_message("in-2", "展开A01", reply_to="om_1"))
        controller.handle(_message("in-3", "选定a01", reply_to="om_2"))

        assert "最近生成图片：1 张将在下方直接展示。" in sender.messages[-1][1]
        assert image_sender.images == [
            (
                "om_image_1",
                original,
                "management-overview:in-3:thread-project:image:item-image:"
                + artifact.sha256,
            )
        ]
        overview_context = state.management_context_for_message("om_3")
        assert overview_context is not None
        assert state.management_context_for_message("om_image_1") == overview_context

        controller.handle(
            _message("in-4", "请继续处理这张图", reply_to="om_image_1")
        )
        assert tools.sent_prompts == [
            ("thread-project", "请继续处理这张图", "source-thread", "local")
        ]
    finally:
        state.close()


def test_selected_session_does_not_send_image_from_plain_text_path(tmp_path: Path) -> None:
    image_sender = CaptureImageSender()
    controller, state, tools, sender = _controller(tmp_path, image_sender=image_sender)
    tools.read_thread = lambda *_args, **_kwargs: {
        "thread": {"id": "thread-project", "status": {"type": "idle"}},
        "turns": [
            {
                "items": [
                    {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "打开图片 C:/Users/example/generated_images/not-structured.png",
                    }
                ]
            }
        ],
    }
    try:
        controller.handle(_message("in-1", "查询项目列表"))
        controller.handle(_message("in-2", "展开A01", reply_to="om_1"))
        controller.handle(_message("in-3", "选定a01", reply_to="om_2"))

        assert "not-structured.png" in sender.messages[-1][1]
        assert image_sender.images == []
        assert "最近生成图片" not in sender.messages[-1][1]
    finally:
        state.close()


def test_personal_query_excludes_threads_with_registry_assignment(tmp_path: Path) -> None:
    controller, state, _tools, sender = _controller(tmp_path)
    try:
        controller.handle(_message("in-1", "查询个人会话"))
        text = sender.messages[-1][1]
        assert "p01｜个人会话" in text
        assert "项目会话" not in text
    finally:
        state.close()


def test_catalog_merges_full_local_history_missing_from_desktop_limit(tmp_path: Path) -> None:
    controller, state, _tools, sender = _controller(tmp_path)
    controller.codex_store.records.extend(
        [
            ThreadRecord(
                "thread-old-personal",
                "第 51 条个人会话",
                r"C:\OldPersonal",
                90_000,
                1,
                preview="旧个人会话",
            ),
            ThreadRecord(
                "thread-old-project",
                "第 51 条项目会话",
                r"D:\Bot",
                80_000,
                1,
                preview="旧项目会话",
            ),
        ]
    )
    controller.project_registry.extra_assignments["thread-old-project"] = "project-1"
    try:
        controller.handle(_message("in-1", "查询个人会话"))
        personal = sender.messages[-1][1]
        assert "第 51 条个人会话" in personal
        assert "第 51 条项目会话" not in personal

        controller.handle(_message("in-2", "查询项目列表"))
        assert "A01｜飞书机器人｜2 个会话" in sender.messages[-1][1]
        controller.handle(_message("in-3", "展开A01", reply_to="om_2"))
        project_threads = sender.messages[-1][1]
        assert "第 51 条项目会话" in project_threads
        assert "第 51 条个人会话" not in project_threads
    finally:
        state.close()


def test_catalog_excludes_internal_subagents_and_uses_project_path_fallback(
    tmp_path: Path,
) -> None:
    controller, state, _tools, sender = _controller(tmp_path)
    controller.codex_store.records.extend(
        [
            ThreadRecord(
                "thread-subagent",
                "",
                r"\\?\D:\Bot",
                300_000,
                1,
                thread_source="subagent",
            ),
            ThreadRecord(
                "thread-legacy-project",
                "旧版项目会话",
                r"\\?\D:\Bot\src",
                250_000,
                1,
                thread_source="user",
            ),
        ]
    )
    try:
        controller.handle(_message("in-1", "查询个人会话"))
        personal = sender.messages[-1][1]
        assert "旧版项目会话" not in personal
        assert "未命名会话" not in personal

        controller.handle(_message("in-2", "查询项目列表"))
        assert "A01｜飞书机器人｜2 个会话" in sender.messages[-1][1]
        controller.handle(_message("in-3", "展开A01", reply_to="om_2"))
        project_threads = sender.messages[-1][1]
        assert "旧版项目会话" in project_threads
        assert "未命名会话" not in project_threads
    finally:
        state.close()


def test_feishu_monitor_add_list_remove_uses_stable_snapshot(tmp_path: Path) -> None:
    controller, state, _tools, sender = _controller(tmp_path)
    try:
        controller.handle(_message("in-1", "查询个人会话"))
        assert "p01｜个人会话" in sender.messages[-1][1]

        controller.handle(_message("in-2", "添加监测p01", reply_to="om_1"))
        subscriptions = state.monitor_subscriptions()
        assert [(item["thread_id"], item["origin"]) for item in subscriptions] == [
            ("thread-personal", "manual")
        ]

        controller.handle(_message("in-3", "查询监测列表"))
        assert "m01｜个人会话｜个人会话｜手动永久" in sender.messages[-1][1]

        controller.handle(_message("in-4", "移除m01", reply_to="om_3"))
        assert state.monitor_subscriptions() == []
        assert state.discover_auto_monitor(
            "thread-personal", last_activity_at=int(__import__("time").time())
        ) is False
    finally:
        state.close()


def test_feishu_created_thread_is_immediately_auto_monitored(tmp_path: Path) -> None:
    controller, state, _tools, _sender = _controller(tmp_path)
    try:
        controller.handle(_message("in-1", "新建个人会话"))
        controller.handle(
            _message("in-2", "首轮对话提示词：测试自动监测", reply_to="om_1")
        )
        subscriptions = state.monitor_subscriptions()
        assert [(item["thread_id"], item["origin"]) for item in subscriptions] == [
            ("created-1", "auto")
        ]
    finally:
        state.close()


def test_usage_command_sends_six_preview_images_then_text_version_hint(
    tmp_path: Path,
) -> None:
    image_sender = CaptureImageSender()
    controller, state, tools, sender = _controller(
        tmp_path, image_sender=image_sender
    )
    try:
        controller.handle(_message("in-1", "使用说明"))
        assert [text for _, text, _ in sender.messages] == [
            "以上为使用说明，如果想要文字版使用说明，请发送“文字版使用说明”哦"
        ]
        assert sender.messages[0][2] == "management-usage-footer:in-1:1.5.0"
        assert len(image_sender.images) == 6
        assert all(
            data.startswith(b"\x89PNG\r\n\x1a\n")
            for _, data, _ in image_sender.images
        )
        assert [key for _, _, key in image_sender.images] == [
            f"management-usage:in-1:image:{index}:1.5.0"
            for index in range(1, 7)
        ]
        usage_context = state.management_context_for_message("om_image_1")
        assert usage_context is not None
        for index in range(1, 7):
            assert state.management_context_for_message(f"om_image_{index}") == usage_context
        assert state.management_context_for_message("om_1") == usage_context
        assert tools.closed == 0
    finally:
        state.close()


def test_text_usage_command_returns_versioned_complete_guide_without_desktop(
    tmp_path: Path,
) -> None:
    controller, state, tools, sender = _controller(tmp_path)
    try:
        controller.handle(_message("in-1", "文字版使用说明"))
        guide = sender.messages[-1][1]
        assert "版本：1.5.0" in guide
        assert "重要提醒：" in guide
        assert "查看会话：" in guide
        assert "新建对话：" in guide
        assert "继续已有对话：" in guide
        assert "发送图片：" in guide
        assert "管理进度监测：" in guide
        assert "查询监测列表" in guide
        assert "复制整段，然后只在冒号后面填上想要的内容" in guide
        assert "一定要引用回复对应的机器人消息" in guide
        assert "暂存有效期为10分钟" in guide
        assert "“.发送”" in guide
        assert "“.取消”" in guide
        assert "\n\n查看会话：\n\n" in guide
        assert "`" not in guide
        assert "#" not in guide
        assert tools.closed == 0
    finally:
        state.close()


def test_quoted_verified_image_is_sent_to_exact_selected_codex_thread(
    tmp_path: Path,
) -> None:
    controller, state, tools, _sender = _controller(tmp_path)
    image_path = tmp_path / "feishu-image.jpg"
    image_path.write_bytes(b"image")
    attachment = ChannelAttachment(
        str(image_path.resolve()), "image/jpeg", "c" * 64, image_path.stat().st_size
    )
    try:
        controller.handle(_message("in-1", "查询项目列表"))
        controller.handle(_message("in-2", "展开A01", reply_to="om_1"))
        controller.handle(_message("in-3", "选定a01", reply_to="om_2"))
        controller.handle(
            _message("in-4", "", reply_to="om_3", attachments=(attachment,))
        )
        assert len(tools.sent_prompts) == 1
        thread_id, prompt, source, host = tools.sent_prompts[0]
        assert (thread_id, source, host) == ("thread-project", "source-thread", "local")
        assert "用户通过飞书发送了以下图片" in prompt
        assert str(image_path.resolve()) in prompt
    finally:
        state.close()


def test_personal_page_compacts_long_title_but_preserves_snapshot(tmp_path: Path) -> None:
    controller, state, _tools, sender = _controller(tmp_path)
    long_title = "第一行\n" + ("很长的历史标题" * 20)
    try:
        controller._send_personal_page(
            [{"id": "long-thread", "title": long_title, "projectId": None}],
            1,
            "long-title-test",
        )
        text = sender.messages[-1][1]
        assert "p01｜第一行 很长的历史标题" in text
        assert "…" in text
        assert len(text) < 200

        context = state.management_context_for_message("om_1")
        assert context is not None
        assert context[0] == "personal_list"
        assert context[1]["threads"][0]["title"] == long_title
    finally:
        state.close()


def test_overview_compacts_long_title_but_preserves_thread_context(tmp_path: Path) -> None:
    controller, state, _tools, sender = _controller(tmp_path)
    long_title = "超长会话标题" * 30
    thread = {
        "id": "thread-project",
        "title": long_title,
        "hostId": "local",
        "status": "idle",
        "summary": "整体概览",
        "updatedAt": 100,
    }
    try:
        controller._send_overview(thread, "飞书机器人", "overview-title-test")
        text = sender.messages[-1][1]
        assert "会话名称：超长会话标题" in text
        assert "…" in text.splitlines()[0]
        assert len(text.splitlines()[0]) < 100

        context = state.management_context_for_message("om_1")
        assert context is not None
        assert context[0] == "thread_overview"
        assert context[1]["thread"]["title"] == long_title
    finally:
        state.close()


def test_personal_form_preserves_multiline_prompt_after_structural_newline(
    tmp_path: Path,
) -> None:
    controller, state, tools, sender = _controller(tmp_path)
    try:
        controller.handle(_message("in-1", "新建个人会话"))
        assert "会话名称：" not in sender.messages[-1][1]
        form = "首轮对话提示词：\n第一行\n  第二行  "
        controller.handle(_message("in-2", form, reply_to="om_1"))
        assert tools.created == [
            ("第一行\n  第二行  ", {"type": "projectless"}, "")
        ]
        assert "个人会话已创建" in sender.messages[-1][1]
    finally:
        state.close()


def test_project_list_can_create_thread_by_immutable_project_label(tmp_path: Path) -> None:
    controller, state, tools, sender = _controller(tmp_path)
    try:
        controller.handle(_message("in-1", "查询项目列表"))
        assert "新建项目会话" in sender.messages[-1][1]

        controller.handle(_message("in-2", "新建项目会话", reply_to="om_1"))
        form = sender.messages[-1][1]
        assert "项目名称：\n" in form
        assert "会话名称：" not in form

        controller.handle(
            _message(
                "in-3",
                "项目名称：A01\n首轮对话提示词：\n逐字保留\n  第二行  ",
                reply_to="om_2",
            )
        )
        assert tools.created == [
            (
                "逐字保留\n  第二行  ",
                {
                    "type": "project",
                    "projectId": "project-1",
                    "environment": {"type": "local"},
                },
                "",
            )
        ]
        assert "项目名称：飞书机器人" in sender.messages[-1][1]
    finally:
        state.close()


def test_creation_entry_forms_match_mobile_minimum_fields(tmp_path: Path) -> None:
    controller, state, _tools, sender = _controller(tmp_path)
    try:
        controller.handle(_message("in-1", "新建项目"))
        project_form = sender.messages[-1][1]
        assert "项目名称：\n" in project_form
        assert "是否需要第一段会话：\n" in project_form
        assert "首轮对话提示词：" in project_form
        assert "会话名称：" not in project_form

        controller.handle(_message("in-2", "查询项目列表"))
        controller.handle(_message("in-3", "展开A01", reply_to="om_2"))
        controller.handle(_message("in-4", "新建项目会话", reply_to="om_3"))
        expanded_form = sender.messages[-1][1]
        assert "项目名称：飞书机器人" in expanded_form
        assert "会话名称：" not in expanded_form
    finally:
        state.close()


def test_new_project_preserves_name_and_first_prompt_without_thread_title(tmp_path: Path) -> None:
    controller, state, tools, sender = _controller(tmp_path)
    registry = controller.project_registry
    try:
        controller.handle(_message("in-1", "新建项目"))
        controller.handle(
            _message(
                "in-2",
                "项目名称：飞书机器人\n是否需要第一段会话：是\n"
                "首轮对话提示词：\n第一行\n  第二行  ",
                reply_to="om_1",
            )
        )
        assert registry.registered_names == ["飞书机器人"]
        assert tools.created == [
            (
                "第一行\n  第二行  ",
                {
                    "type": "project",
                    "projectId": "project-1",
                    "environment": {"type": "local"},
                },
                "",
            )
        ]
        assert "项目和首个会话均已创建" in sender.messages[-1][1]
    finally:
        state.close()


def test_all_management_lists_page_at_twenty_with_stable_global_labels(tmp_path: Path) -> None:
    controller, state, _tools, sender = _controller(tmp_path)
    projects = [
        {
            "label": f"A{index:02d}",
            "name": f"项目{index}",
            "thread_count": 0,
            "project_id": f"project-{index}",
        }
        for index in range(1, 22)
    ]
    threads = [
        {"id": f"thread-{index}", "title": f"会话{index}", "hostId": "local"}
        for index in range(1, 22)
    ]
    try:
        controller._send_project_page(projects, 1, "project-pages")
        first_projects = sender.messages[-1][1]
        assert "A20｜项目20" in first_projects
        assert "A21｜项目21" not in first_projects
        controller.handle(_message("page-projects", "第2页", reply_to="om_1"))
        second_projects = sender.messages[-1][1]
        assert "A21｜项目21" in second_projects
        assert "A20｜项目20" not in second_projects

        controller._send_project_threads(projects[0], threads, 1, "thread-pages")
        first_threads = sender.messages[-1][1]
        assert "a20｜会话20" in first_threads
        assert "a21｜会话21" not in first_threads
        controller.handle(_message("page-threads", "第2页", reply_to="om_3"))
        second_threads = sender.messages[-1][1]
        assert "a21｜会话21" in second_threads
        assert "a20｜会话20" not in second_threads

        controller._send_personal_page(threads, 1, "personal-pages")
        first_personal = sender.messages[-1][1]
        assert "p20｜会话20" in first_personal
        assert "p21｜会话21" not in first_personal
        controller.handle(_message("page-personal", "第2页", reply_to="om_5"))
        second_personal = sender.messages[-1][1]
        assert "p21｜会话21" in second_personal
        assert "p20｜会话20" not in second_personal
    finally:
        state.close()


def test_form_parser_rejects_unknown_header_and_keeps_prompt_body() -> None:
    parsed = _parse_form(
        "会话名称：测试\n首轮对话提示词：一\n二  ",
        ("会话名称",),
        "首轮对话提示词",
    )
    assert parsed == {"会话名称": "测试", "首轮对话提示词": "一\n二  "}


def test_form_parser_accepts_copied_personal_form_preamble() -> None:
    parsed = _parse_form(
        "新建 Codex 个人会话\n\n"
        "请回复本消息并保留字段名：\n"
        "首轮对话提示词：歪歪歪，你在吗",
        (),
        "首轮对话提示词",
    )

    assert parsed == {"首轮对话提示词": "歪歪歪，你在吗"}


def test_form_parser_accepts_copied_dynamic_project_thread_preamble() -> None:
    parsed = _parse_form(
        "在“FeiShuBOT”中新建 Codex 会话\n\n"
        "请只填写下面字段并回复本消息；不要添加标题或说明：\n"
        "项目名称：FeiShuBOT\n"
        "首轮对话提示词：第一行\n第二行  ",
        ("项目名称",),
        "首轮对话提示词",
    )

    assert parsed == {
        "项目名称": "FeiShuBOT",
        "首轮对话提示词": "第一行\n第二行  ",
    }


def test_form_parser_names_the_first_unknown_line() -> None:
    with pytest.raises(ManagementUserError, match="测试测试"):
        _parse_form(
            "新建 Codex 个人会话\n测试测试\n首轮对话提示词：歪歪歪，你在吗",
            (),
            "首轮对话提示词",
        )


def test_latest_final_skips_active_commentary_and_uses_completed_result() -> None:
    payload = {
        "turns": [
            {
                "status": "inProgress",
                "items": [
                    {"type": "agentMessage", "phase": "commentary", "text": "正在处理中"}
                ],
            },
            {
                "status": "completed",
                "items": [
                    {"type": "agentMessage", "phase": "commentary", "text": "旧进度"},
                    {"type": "agentMessage", "phase": "final_answer", "text": "最终结果"},
                ],
            },
        ]
    }

    assert _latest_final(payload) == "最终结果"


def test_project_registry_uses_codex_opener_and_is_idempotent(
    tmp_path: Path,
) -> None:
    state_file = tmp_path / ".codex-global-state.json"
    original = {
        "local-projects": {},
        "project-order": [],
        "thread-project-assignments": {"old": {"projectKind": "local", "projectId": "missing"}},
        "unrelated": {"keep": "exact"},
    }
    state_file.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
    opened: list[Path] = []

    def fake_opener(root: Path) -> None:
        opened.append(root)
        state = json.loads(state_file.read_text(encoding="utf-8"))
        state["local-projects"]["project-created"] = {
            "id": "project-created",
            "name": root.name,
            "rootPaths": [str(root)],
            "createdAt": 100,
            "updatedAt": 100,
        }
        state["project-order"].insert(0, "project-created")
        state_file.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    registry = CodexProjectRegistry(
        state_file, tmp_path / "managed", opener=fake_opener, recognition_timeout=1
    )

    first = registry.register("飞书项目")
    second = registry.register("飞书项目")

    assert second.project_id == first.project_id
    assert first.name == "飞书项目"
    assert Path(first.root_paths[0]).is_dir()
    assert Path(first.root_paths[0]).name == "飞书项目"
    assert opened == [Path(first.root_paths[0])]
    updated = json.loads(state_file.read_text(encoding="utf-8"))
    assert updated["unrelated"] == {"keep": "exact"}
    assert updated["project-order"] == [first.project_id]


def test_project_registry_rejects_name_that_codex_cannot_keep_exact(tmp_path: Path) -> None:
    state_file = tmp_path / ".codex-global-state.json"
    state_file.write_text(
        json.dumps({"local-projects": {}, "project-order": []}), encoding="utf-8"
    )
    registry = CodexProjectRegistry(
        state_file, tmp_path / "managed", opener=lambda _root: None, recognition_timeout=0.1
    )
    try:
        registry.register("A/B")
    except Exception as exc:
        assert "合法的 Windows 文件夹名" in str(exc)
    else:
        raise AssertionError("非法名称不应被静默改写")
