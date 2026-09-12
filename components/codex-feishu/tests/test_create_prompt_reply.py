"""Offline coverage for direct prompt replies to new-thread prompts."""

from __future__ import annotations

from pathlib import Path

import pytest

from progress_wx.channel import ChannelReply
from progress_wx.codex_management import (
    CodexManagementController,
    ManagementUserError,
)
from progress_wx.state import StateStore


class FakeDesktopTools:
    def __init__(self, *, is_git: bool = True) -> None:
        self.is_git = is_git
        self.created: list[tuple[str, dict, str]] = []
        self.sent: list[tuple[str, str, str, str, str]] = []
        self.closed = 0

    def list_threads(self, _source: str, *, limit: int = 50, call_tag: str = ""):
        del limit, call_tag
        return {"pinnedThreads": [], "threads": []}

    def list_projects(self, _source: str):
        return {
            "projects": [
                {
                    "projectId": "project-1",
                    "label": "A01",
                    "name": "飞书机器人",
                    "isGitRepository": self.is_git,
                }
            ]
        }

    def create_thread(
        self,
        _source: str,
        prompt: str,
        target: dict,
        *,
        title: str = "",
        call_tag: str = "",
    ):
        del title
        self.created.append((prompt, target, call_tag))
        return {"threadId": f"created-{len(self.created)}"}

    def send_message(
        self,
        thread_id: str,
        prompt: str,
        *,
        call_tag: str,
        source_thread_id: str,
        host_id: str,
    ):
        self.sent.append((thread_id, prompt, call_tag, source_thread_id, host_id))
        return {"success": True}

    def close(self) -> None:
        self.closed += 1


class FakeDesktopClient:
    def __init__(self, tools: FakeDesktopTools) -> None:
        self.tools = tools

    def open_verified(self, *, required_tools: tuple[str, ...]):
        del required_tools
        return self.tools


class FakeProjectRegistry:
    pass


class Sender:
    def __init__(self, prefix: str = "out") -> None:
        self.prefix = prefix
        self.messages: list[tuple[str, str, str]] = []

    def __call__(self, text: str, key: str) -> tuple[str, ...]:
        message_id = f"{self.prefix}-{len(self.messages) + 1}"
        self.messages.append((message_id, text, key))
        return (message_id,)


def _message(message_id: str, content: str, *, reply_to: str = "") -> ChannelReply:
    return ChannelReply(
        sender_id="ou_owner",
        content=content,
        reply_to_message_id=reply_to,
        message_id=message_id,
        chat_id="oc_private",
    )


def _controller(
    state: StateStore,
    tools: FakeDesktopTools,
    sender: Sender,
) -> CodexManagementController:
    return CodexManagementController(
        store=state,
        codex_store=object(),
        desktop_client=FakeDesktopClient(tools),
        project_registry=FakeProjectRegistry(),
        source_thread_ids=("source-thread",),
        send_text=sender,
    )


def test_personal_prompt_accepts_plain_body_and_replay_is_durable(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite"
    state = StateStore(database)
    try:
        first_tools = FakeDesktopTools()
        first_sender = Sender("first")
        first = _controller(state, first_tools, first_sender)
        first.handle(_message("open-personal", ".新建个人会话"))
        prompt_id = first_sender.messages[-1][0]
        assert first_sender.messages[-1][1] == (
            "操作类型：新建 Codex 个人会话\n\n"
            "请回复这条消息，直接写下要做的事并发送。"
        )
        context = state.management_context_record_for_message(prompt_id)
        assert context is not None
        assert context.context_kind == "new_personal_thread_form"
        assert context.payload["prompt_input"] == "direct_or_form"
        assert state.management_context_exists_for_message(prompt_id)
    finally:
        state.close()

    reopened = StateStore(database)
    try:
        second_tools = FakeDesktopTools()
        second = _controller(reopened, second_tools, Sender("second"))
        prompt = "直接作为首轮提示词\n保留换行"
        second.handle(_message("personal-prompt", prompt, reply_to=prompt_id))
        assert second_tools.created == [
            (
                prompt,
                {"type": "projectless"},
                "management-create-personal-personal-pro",
            )
        ]
        assert reopened.management_inbound_status(
            "personal-prompt", sender_id="ou_owner", content=prompt
        ) == "accepted"

        # A new Feishu delivery id for the same durable parent is also one-shot.
        with pytest.raises(ManagementUserError, match="已经提交过"):
            second.handle(_message("personal-replay", prompt, reply_to=prompt_id))
        assert len(second_tools.created) == 1
    finally:
        reopened.close()


def test_selected_project_accepts_plain_prompt_with_auto_environment(
    tmp_path: Path,
) -> None:
    state = StateStore(tmp_path / "state.sqlite")
    try:
        tools = FakeDesktopTools(is_git=True)
        sender = Sender()
        controller = _controller(state, tools, sender)
        controller._new_project_thread_form(
            {
                "project_id": "project-1",
                "name": "飞书机器人",
                "label": "A01",
            },
            "project-entry",
            environment_mode="auto",
        )
        prompt_id = sender.messages[-1][0]
        assert "已选项目：飞书机器人" in sender.messages[-1][1]
        assert "请回复这条消息，直接写下要做的事并发送。" in sender.messages[-1][1]
        assert "首轮对话提示词：" not in sender.messages[-1][1]

        controller.handle(_message("project-prompt", "创建一个离线测试", reply_to=prompt_id))
        assert tools.created == [
            (
                "创建一个离线测试",
                {
                    "type": "project",
                    "projectId": "project-1",
                    "environment": {"type": "worktree"},
                },
                "management-create-project-thread-project-prom",
            )
        ]
    finally:
        state.close()


def test_project_selection_stage_reopens_with_real_parent_binding(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite"
    state = StateStore(database)
    try:
        first_tools = FakeDesktopTools(is_git=False)
        first_sender = Sender("first")
        first = _controller(state, first_tools, first_sender)
        first._new_project_thread_form(
            None,
            "project-entry",
            projects=[
                {
                    "project_id": "project-1",
                    "name": "飞书机器人",
                    "label": "A01",
                }
            ],
            environment_mode="auto",
        )
        parent_id = first_sender.messages[-1][0]
        first.handle(_message("select-project", "A01", reply_to=parent_id))
        selected_id = first_sender.messages[-1][0]
        selected_context = state.management_context_record_for_message(selected_id)
        assert selected_context is not None
        assert selected_context.sender_id == "ou_owner"
        assert selected_context.chat_id == "oc_private"
    finally:
        state.close()

    reopened = StateStore(database)
    try:
        tools = FakeDesktopTools(is_git=False)
        controller = _controller(reopened, tools, Sender("reopened"))
        controller.handle(
            _message("project-prompt-after-restart", "重启后继续正文", reply_to=selected_id)
        )
        assert tools.created[0][0] == "重启后继续正文"
        assert tools.created[0][1]["environment"] == {"type": "local"}
    finally:
        reopened.close()


def test_unselected_project_and_choose_environment_reject_plain_body(
    tmp_path: Path,
) -> None:
    state = StateStore(tmp_path / "state.sqlite")
    try:
        tools = FakeDesktopTools(is_git=False)
        sender = Sender()
        controller = _controller(state, tools, sender)
        project = {
            "project_id": "project-1",
            "name": "飞书机器人",
            "label": "A01",
        }

        controller._new_project_thread_form(
            None,
            "unselected-entry",
            projects=[project],
            environment_mode="auto",
        )
        unselected_id = sender.messages[-1][0]
        assert "项目名称：" not in sender.messages[-1][1]
        with pytest.raises(ManagementUserError, match="猜测项目"):
            controller.handle(_message("unselected-prompt", "不要猜项目", reply_to=unselected_id))
        assert tools.created == []
        controller.handle(_message("select-project", "A01", reply_to=unselected_id))
        selected_id = sender.messages[-1][0]
        selected_context = state.management_context_record_for_message(selected_id)
        assert selected_context is not None
        assert selected_context.payload["prompt_input"] == "direct_or_form"
        controller.handle(
            _message("selected-prompt", "已明确选定项目", reply_to=selected_id)
        )
        assert tools.created[-1][1]["environment"] == {"type": "local"}

        controller._new_project_thread_form(
            project,
            "choose-entry",
            environment_mode="choose",
        )
        choose_id = sender.messages[-1][0]
        assert "运行方式：" not in sender.messages[-1][1]
        with pytest.raises(ManagementUserError, match="运行方式"):
            controller.handle(_message("choose-prompt", "不要猜运行方式", reply_to=choose_id))
        controller.handle(_message("choose-local", "本地", reply_to=choose_id))
        local_prompt_id = sender.messages[-1][0]
        local_context = state.management_context_record_for_message(local_prompt_id)
        assert local_context is not None
        assert local_context.payload["prompt_input"] == "direct_or_form"
        controller.handle(
            _message("choose-local-prompt", "已明确选定本地", reply_to=local_prompt_id)
        )
        assert tools.created[-1][1]["environment"] == {"type": "local"}

        # The old labelled form remains the explicit path when routing is not
        # fully selected; it still chooses the project and environment.
        controller._new_project_thread_form(
            project,
            "legacy-entry",
            environment_mode="choose",
        )
        legacy_id = sender.messages[-1][0]
        controller.handle(
            _message(
                "choose-form",
                "项目名称：飞书机器人\n运行方式：本地\n首轮对话提示词：保留旧模板",
                reply_to=legacy_id,
            )
        )
        assert tools.created[-1][0] == "保留旧模板"
        assert tools.created[-1][1]["environment"] == {"type": "local"}
    finally:
        state.close()


def test_thread_overview_plain_reply_stays_ordinary_continuation(
    tmp_path: Path,
) -> None:
    state = StateStore(tmp_path / "state.sqlite")
    try:
        tools = FakeDesktopTools()
        sender = Sender()
        controller = _controller(state, tools, sender)
        context_id = state.create_management_context(
            "thread_overview",
            {"thread": {"id": "thread-1", "title": "现有会话"}, "group": "个人会话"},
            sender_id="ou_owner",
            chat_id="oc_private",
        )
        state.bind_management_messages(context_id, ("existing-overview",))

        controller.handle(
            _message("ordinary-reply", "继续现有会话", reply_to="existing-overview")
        )
        assert tools.sent == [
            (
                "thread-1",
                "继续现有会话",
                "management-send-ordinary-rep",
                "source-thread",
                "",
            )
        ]
        assert tools.created == []
    finally:
        state.close()


def test_previous_static_legacy_templates_still_round_trip_after_prompt_ui_change(
    tmp_path: Path,
) -> None:
    state = StateStore(tmp_path / "state.sqlite")
    try:
        tools = FakeDesktopTools(is_git=True)
        sender = Sender()
        controller = _controller(state, tools, sender)

        controller._new_personal_form("personal-entry")
        personal_id = sender.messages[-1][0]
        personal_form = (
            "操作类型：新建 Codex 个人会话\n\n"
            "填写说明：复制整段，然后只在冒号后面填上想要的内容就可以了哦。\n"
            "首轮对话提示词：旧模板个人提示词"
        )
        controller.handle(
            _message("personal-form", personal_form, reply_to=personal_id)
        )

        controller._new_project_thread_form(
            {
                "project_id": "project-1",
                "name": "飞书机器人",
                "label": "A01",
            },
            "project-entry",
            environment_mode="auto",
        )
        project_id = sender.messages[-1][0]
        project_form = (
            "操作类型：在项目“飞书机器人”中新建会话\n\n"
            "填写说明：复制整段，然后只在冒号后面填上想要的内容就可以了哦。\n"
            "“自动”会让 Git 项目使用工作树，其他项目使用本地目录。\n"
            "项目名称：飞书机器人\n"
            "运行方式：自动\n"
            "首轮对话提示词：旧模板项目提示词"
        )
        controller.handle(_message("project-form", project_form, reply_to=project_id))

        assert [item[0] for item in tools.created] == [
            "旧模板个人提示词",
            "旧模板项目提示词",
        ]
        assert tools.created[-1][1]["environment"] == {"type": "worktree"}
    finally:
        state.close()
