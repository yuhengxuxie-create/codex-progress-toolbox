from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pytest

from progress_wx.channel import (
    ChannelAttachment,
    ChannelReply,
    MessageChannelPayloadRejectedError,
)
from progress_wx.codex_app_tools import (
    DesktopAppToolsError,
    DesktopAppToolsRejected,
    DesktopAppToolsResultUnknown,
)
from progress_wx.codex_management import (
    CodexManagementController,
    ManagementUserError,
    SEARCH_EMPTY_TITLE,
    _book_title,
    _latest_final,
    _parse_form,
)
from progress_wx.codex_rpc import CodexRPCRejected, CodexRPCTimeout
from progress_wx.codex_projects import (
    CodexProjectRegistry,
    LocalProject,
    ProjectRegistrySnapshot,
)
from progress_wx.codex_store import ThreadRecord, ThreadStatus, TurnRecord
from progress_wx.models import GeneratedImageArtifact
from progress_wx.session_search import SearchMatch, SearchResult
from progress_wx.session_query_card import (
    SESSION_QUERY_ENTRY_COMMAND,
    build_session_search_form_card,
    build_thread_overview_card,
    build_thread_reply_form_card,
    session_query_action,
    session_query_action_fingerprint,
    session_query_card_action_fingerprints,
    session_query_command,
    session_query_form_command,
)
from progress_wx.remote_control import (
    CURRENT_THREAD_CLEAR_COMMAND,
    CURRENT_THREAD_CLEAR_LABEL,
    CURRENT_THREAD_SWITCH_COMMAND,
    CURRENT_THREAD_SWITCH_LABEL,
    CURRENT_THREAD_VIEW_COMMAND,
    CURRENT_THREAD_VIEW_LABEL,
    GoalSnapshot,
    REMOTE_CONTROL_ENTRY_COMMAND,
    RemoteCommand,
    SkillSnapshot,
    build_goal_set_form_card,
    build_plan_start_form_card,
    parse_remote_command,
    remote_control_action,
    remote_control_action_fingerprint,
    remote_control_card_action_fingerprints,
)
from progress_wx.state import StateStore


class FakeCodexStore:
    def __init__(self, records: list[ThreadRecord]) -> None:
        self.records = records
        self.turns: dict[str, TurnRecord] = {}
        self.completed_turns: dict[str, TurnRecord] = {}

    def select_threads(self, *, include_archived: bool = False):
        del include_archived
        return list(self.records)

    def require_readable(self, _operation: str) -> None:
        return None

    def get_thread(self, thread_id: str):
        return next((item for item in self.records if item.thread_id == thread_id), None)

    def latest_turn(self, thread_id: str):
        return self.turns.get(thread_id)

    def latest_terminal_turn(self, thread_id: str):
        return self.turns.get(thread_id)

    def latest_completed_result_turn(self, thread_id: str):
        return self.completed_turns.get(thread_id) or self.turns.get(thread_id)


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
        self.extra_threads: list[dict] = []
        self.archive_calls: list[tuple[str, bool, str, str]] = []
        self.archive_error: BaseException | None = None
        self.close_error: BaseException | None = None
        self.is_git_repository = False

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
                *self.extra_threads,
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
                    "isGitRepository": self.is_git_repository,
                }
            ]
        }

    def read_thread(self, _source: str, thread_id: str, **_kwargs):
        assert thread_id in {"thread-project", "thread-personal"}
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

    def set_thread_archived(
        self,
        thread_id: str,
        *,
        archived: bool,
        source_thread_id: str,
        host_id: str = "",
        call_tag: str,
    ):
        del call_tag
        self.archive_calls.append((thread_id, archived, source_thread_id, host_id))
        if self.archive_error is not None:
            raise self.archive_error
        return {"success": True}

    def close(self) -> None:
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error


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


class CaptureCardSender:
    def __init__(self) -> None:
        self.cards: list[tuple[str, dict, str]] = []

    def __call__(self, card, key: str) -> tuple[str, ...]:
        message_id = f"om_card_{len(self.cards) + 1}"
        self.cards.append((message_id, dict(card), key))
        return (message_id,)


class RejectFormatCardSender:
    def __init__(self) -> None:
        self.calls: list[tuple[dict, str]] = []

    def __call__(self, card, key: str) -> tuple[str, ...]:
        self.calls.append((dict(card), key))
        raise MessageChannelPayloadRejectedError("synthetic card format rejection")


class RejectNthFormatCardSender(CaptureCardSender):
    def __init__(self, reject_on_call: int) -> None:
        super().__init__()
        self.reject_on_call = reject_on_call
        self.attempts: list[tuple[dict, str]] = []

    def __call__(self, card, key: str) -> tuple[str, ...]:
        self.attempts.append((dict(card), key))
        if len(self.attempts) == self.reject_on_call:
            raise MessageChannelPayloadRejectedError(
                "synthetic overview card format rejection"
            )
        return super().__call__(card, key)


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


class FakeRemoteSession:
    def __init__(self) -> None:
        self.goal_value: GoalSnapshot | None = GoalSnapshot("已有目标", "active")
        self.active_checks: list[bool] = []
        self.skills_value = (
            SkillSnapshot(
                "skill-one",
                r"C:\skills\one\SKILL.md",
                "合成技能",
                "技能一",
            ),
        )
        self.calls: list[tuple[str, object]] = []
        self.write_error: BaseException | None = None
        self.closed = 0

    def close(self) -> None:
        self.closed += 1

    def goal(self):
        self.calls.append(("goal", None))
        return self.goal_value

    def set_goal(self, objective: str, *, before_send=None):
        self.calls.append(("set_goal", objective))
        if before_send is not None:
            before_send()
        if self.write_error:
            raise self.write_error
        self.goal_value = GoalSnapshot(objective, "active")
        return {"goal": {"objective": objective}}

    def clear_goal(self, *, before_send=None):
        self.calls.append(("clear_goal", None))
        if before_send is not None:
            before_send()
        if self.write_error:
            raise self.write_error
        self.goal_value = None
        return {}

    def is_active(self) -> bool:
        value = self.active_checks.pop(0) if self.active_checks else False
        self.calls.append(("is_active", value))
        return value

    def plan_mode(self):
        self.calls.append(("plan_mode", None))
        return {"id": "plan", "name": "Plan"}

    def skills(self, *, force_reload: bool):
        self.calls.append(("skills", force_reload))
        return self.skills_value

    def start_plan(self, task: str, mode, *, before_send=None):
        self.calls.append(("start_plan", (task, mode)))
        if before_send is not None:
            before_send()
        if self.write_error:
            raise self.write_error
        return {"turn": {"id": "turn-plan", "status": "inProgress"}}

    def start_skill(self, skill, request: str, *, before_send=None):
        self.calls.append(("start_skill", (skill.name, skill.path, request)))
        if before_send is not None:
            before_send()
        if self.write_error:
            raise self.write_error
        return {"turn": {"id": "turn-skill", "status": "inProgress"}}


class FakeRemoteControl:
    def __init__(self, session: FakeRemoteSession | None = None) -> None:
        self.session = session or FakeRemoteSession()
        self.prepared: list[str] = []

    def prepare(self, thread_id: str):
        self.prepared.append(thread_id)
        return self.session


def _message(
    message_id: str,
    content: str,
    *,
    reply_to: str = "",
    attachments: tuple[ChannelAttachment, ...] = (),
    sender_id: str = "ou_owner",
    chat_id: str = "oc_private",
    source_kind: str = "message",
    action_name: str = "",
    action_fingerprint: str = "",
) -> ChannelReply:
    if source_kind == "card_action" and action_name and not action_fingerprint:
        try:
            action_fingerprint = (
                session_query_action_fingerprint(session_query_action(action_name)) or ""
            )
        except ValueError:
            try:
                action_fingerprint = (
                    remote_control_action_fingerprint(
                        remote_control_action(action_name)
                    )
                    or ""
                )
            except ValueError:
                action_fingerprint = ""
    return ChannelReply(
        sender_id=sender_id,
        content=content,
        reply_to_message_id=reply_to,
        message_id=message_id,
        chat_id=chat_id,
        attachments=attachments,
        source_kind=source_kind,
        action_name=action_name,
        action_fingerprint=action_fingerprint,
    )


def _controller(
    tmp_path: Path,
    *,
    image_sender: CaptureImageSender | None = None,
    file_sender: CaptureFileSender | None = None,
    card_sender: CaptureCardSender | None = None,
    account_reader=None,
    session_search=None,
    remote_control=None,
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
        send_card=card_sender,
        send_image=image_sender,
        send_file=file_sender,
        account_reader=account_reader,
        session_search=session_search,
        remote_control=remote_control,
    )
    return controller, state, tools, sender


def test_session_query_entry_sends_card_and_button_reuses_existing_command(
    tmp_path: Path,
) -> None:
    card_sender = CaptureCardSender()
    controller, state, tools, sender = _controller(
        tmp_path, card_sender=card_sender
    )
    try:
        entry = _message("query-menu-1", "." + SESSION_QUERY_ENTRY_COMMAND)
        assert controller.accepts(entry) is True
        controller.handle(entry)
        assert len(card_sender.cards) == 1
        card_message_id, card, key = card_sender.cards[0]
        assert card["schema"] == "2.0"
        assert key == "management-session-query-menu:query-menu-1"
        context = state.management_context_record_for_message(card_message_id)
        assert context is not None
        assert context.context_kind == "session_query_menu"
        assert context.sender_id == "ou_owner"
        assert context.chat_id == "oc_private"

        action = _message(
            "feishu-card-event-1",
            "查询个人会话",
            reply_to=card_message_id,
        )
        assert controller.accepts(action) is True
        controller.handle(action)
        assert len(card_sender.cards) == 2
        personal_message_id, personal_card, personal_key = card_sender.cards[-1]
        assert personal_card["header"]["title"]["content"] == "Codex 个人会话"
        assert personal_key == "management-personal-list:feishu-card-event-1:1"
        personal_context = state.management_context_record_for_message(
            personal_message_id
        )
        assert personal_context is not None
        assert personal_context.context_kind == "personal_list"
        assert tools.closed == 1
    finally:
        state.close()


def test_visible_feature_names_go_directly_to_only_the_requested_feature(
    tmp_path: Path,
) -> None:
    cards = CaptureCardSender()
    controller, state, _tools, _sender = _controller(tmp_path, card_sender=cards)
    try:
        controller.handle(_message("direct-query", ".查询会话"))
        assert cards.cards[-1][1]["header"]["title"]["content"] == "查询 Codex 会话"
        assert state.management_context_record_for_message(
            cards.cards[-1][0]
        ).context_kind == "session_query_menu"

        controller.handle(_message("direct-projects", ".项目会话"))
        assert cards.cards[-1][1]["header"]["title"]["content"] == "Codex 项目"
        assert state.management_context_record_for_message(
            cards.cards[-1][0]
        ).context_kind == "project_list"
    finally:
        state.close()


def test_remote_menu_reuses_personal_snapshot_and_binds_selected_thread(
    tmp_path: Path,
) -> None:
    cards = CaptureCardSender()
    remote = FakeRemoteControl()
    controller, state, tools, sender = _controller(
        tmp_path,
        card_sender=cards,
        remote_control=remote,
    )
    try:
        entry = _message("remote-menu-1", "." + REMOTE_CONTROL_ENTRY_COMMAND)
        assert controller.accepts(entry) is True
        controller.handle(entry)
        entry_id = cards.cards[-1][0]
        entry_context = state.management_context_record_for_message(entry_id)
        assert entry_context is not None
        assert entry_context.context_kind == "remote_control_menu"
        controller.handle(
            _message(
                "remote-select-personal",
                "选择个人会话",
                reply_to=entry_id,
                source_kind="card_action",
                action_name="select_personal",
            )
        )
        list_id = cards.cards[-1][0]
        list_context = state.management_context_record_for_message(list_id)
        assert list_context is not None
        assert list_context.context_kind == "personal_list"
        assert list_context.payload["selection_mode"] == "remote_control"
        controller.handle(
            _message(
                "remote-select-thread",
                "选定p01",
                reply_to=list_id,
                source_kind="card_action",
                action_name="select_personal_thread",
                action_fingerprint=(
                    session_query_action_fingerprint(
                        session_query_action(
                            "select_personal_thread", label="p01"
                        )
                    )
                    or ""
                ),
            )
        )
        remote_id = cards.cards[-1][0]
        remote_context = state.management_context_record_for_message(remote_id)
        assert remote_context is not None
        assert remote_context.context_kind == "remote_control"
        assert remote_context.payload["thread"]["id"] == "thread-personal"
        encoded_card = json.dumps(cards.cards[-1][1], ensure_ascii=False)
        assert "thread-personal" not in encoded_card
        assert r"C:\Personal" not in encoded_card
        assert remote.prepared == []
        assert [text for _message_id, text, _key in sender.messages] == [
            "已设为当前会话：《个人会话》"
        ]
    finally:
        state.close()


@pytest.mark.parametrize("entry", ["指令使用", "远程控制", "Codex管理", "Codex 管理"])
def test_plain_navigation_cards_are_reusable_but_pending_commands_stay_one_shot(
    tmp_path: Path, entry: str,
) -> None:
    cards = CaptureCardSender()
    remote = FakeRemoteControl()
    controller, state, _tools, _sender = _controller(
        tmp_path, card_sender=cards, remote_control=remote
    )
    try:
        controller.handle(_message("plain-query", ".查询会话"))
        query_id = cards.cards[-1][0]
        controller.handle(_message("plain-personal-1", "查询个人会话", reply_to=query_id))
        first_list_id = cards.cards[-1][0]
        controller.handle(_message("plain-personal-2", "查询个人会话", reply_to=query_id))
        second_list_id = cards.cards[-1][0]
        assert first_list_id != second_list_id

        # A normal list can be used for more than one deliberate click.  Each
        # click receives its own Feishu event id and creates its own result.
        controller.handle(_message("plain-select-1", "选定p01", reply_to=first_list_id))
        first_overview_id = cards.cards[-1][0]
        controller.handle(_message("plain-select-2", "选定p01", reply_to=first_list_id))
        second_overview_id = cards.cards[-1][0]
        assert first_overview_id != second_overview_id

        controller.handle(_message("plain-codex", f".{entry}"))
        manager_id = cards.cards[-1][0]
        manager_card = cards.cards[-1][1]
        assert "指令使用" in str(manager_card)
        assert "Skills" in str(manager_card)
        assert "/ 指令列表" in str(manager_card)
        assert "设置 Goal" not in str(manager_card)
        # Reusing the same manager card is allowed for read-only navigation.
        skills_action = remote_control_action("skills_list")
        fingerprint = remote_control_action_fingerprint(skills_action) or ""
        controller.handle(
            _message(
                "manager-skills-1",
                "/skills",
                reply_to=manager_id,
                source_kind="card_action",
                action_name="skills_list",
                action_fingerprint=fingerprint,
            )
        )
        controller.handle(
            _message(
                "manager-skills-2",
                "/skills",
                reply_to=manager_id,
                source_kind="card_action",
                action_name="skills_list",
                action_fingerprint=fingerprint,
            )
        )
        assert remote.prepared == ["thread-personal", "thread-personal"]
    finally:
        state.close()


def test_write_forms_validate_then_claim_once_across_distinct_feishu_events(
    tmp_path: Path,
) -> None:
    controller, state, tools, _sender = _controller(tmp_path)
    try:
        controller.handle(_message("personal-form-open", ".新建个人会话"))
        form_id = "om_1"

        # Invalid content does not consume the form, so the user can correct it.
        with pytest.raises(ManagementUserError, match="不能为空"):
            controller.handle(
                _message(
                    "personal-form-invalid",
                    "首轮对话提示词：",
                    reply_to=form_id,
                )
            )

        controller.handle(
            _message(
                "personal-form-submit-1",
                "首轮对话提示词：只创建一次",
                reply_to=form_id,
            )
        )
        assert len(tools.created) == 1

        with pytest.raises(ManagementUserError, match="已经提交过"):
            controller.handle(
                _message(
                    "personal-form-submit-2",
                    "首轮对话提示词：只创建一次",
                    reply_to=form_id,
                )
            )
        assert len(tools.created) == 1
    finally:
        state.close()


def test_goal_get_set_and_future_query_remains_official_default(
    tmp_path: Path,
) -> None:
    cards = CaptureCardSender()
    remote = FakeRemoteControl()
    controller, state, tools, sender = _controller(
        tmp_path,
        card_sender=cards,
        remote_control=remote,
    )
    try:
        controller.handle(_message("remote-entry", "." + REMOTE_CONTROL_ENTRY_COMMAND))
        menu_id = cards.cards[-1][0]
        controller.handle(
            _message(
                "remote-personal",
                "选择个人会话",
                reply_to=menu_id,
                source_kind="card_action",
                action_name="select_personal",
            )
        )
        list_id = cards.cards[-1][0]
        controller.handle(_message("remote-thread", "选定p01", reply_to=list_id))
        remote_id = cards.cards[-1][0]

        controller.handle(_message("goal-get-1", "/goal", reply_to=remote_id))
        assert "当前 Goal：已有目标" in sender.messages[-1][1]
        controller.handle(
            _message("goal-set-1", "/goal 新的长期目标", reply_to=remote_id)
        )
        assert "已设置正式 Goal" in sender.messages[-1][1]
        action = parse_remote_command("/goal 新的长期目标")
        assert action is not None
        row = state.remote_control_action(
            state.management_context_record_for_message(remote_id).context_id,
            "goal_set",
            action.request_hash(),
        )
        assert row is not None and row["state"] == "succeeded"

        controller.handle(_message("goal-set-duplicate", "/goal 新的长期目标", reply_to=remote_id))
        assert "已经成功执行" in sender.messages[-1][1]
        assert [call for call in remote.session.calls if call[0] == "set_goal"] == [
            ("set_goal", "新的长期目标")
        ]
        controller.handle(_message("goal-get-2", "/goal", reply_to=remote_id))
        assert "当前 Goal：新的长期目标" in sender.messages[-1][1]
    finally:
        state.close()


def test_unbound_slash_selects_once_persists_binding_and_reuses_target(
    tmp_path: Path,
) -> None:
    cards = CaptureCardSender()
    remote = FakeRemoteControl()
    controller, state, _tools, sender = _controller(
        tmp_path, card_sender=cards, remote_control=remote
    )
    try:
        command = _message("unbound-goal", "/goal")
        assert controller.accepts(command) is True
        controller.handle(command)
        selector_id = cards.cards[-1][0]
        selector_context = state.management_context_record_for_message(selector_id)
        assert selector_context is not None
        assert selector_context.payload["pending_command"] == "/goal"

        controller.handle(
            _message(
                "selector-personal",
                "选择个人会话",
                reply_to=selector_id,
                source_kind="card_action",
                action_name="select_personal",
            )
        )
        list_id = cards.cards[-1][0]
        # Replaying the original target-range card with a different inbound
        # event must not create a second child context that can later submit
        # the same pending slash command.
        child_card_count = len(cards.cards)
        with pytest.raises(ManagementUserError):
            controller.handle(
                _message(
                    "selector-personal-duplicate",
                    "选择个人会话",
                    reply_to=selector_id,
                    source_kind="card_action",
                    action_name="select_personal",
                )
            )
        assert len(cards.cards) == child_card_count
        controller.handle(_message("select-personal-thread", "选定p01", reply_to=list_id))
        remote_id = cards.cards[-1][0]
        assert state.current_thread("ou_owner", "oc_private") is not None
        assert state.current_thread("ou_owner", "oc_private")["thread_id"] == (
            "thread-personal"
        )
        assert "当前 Goal：已有目标" in sender.messages[-1][1]
        assert remote.prepared == ["thread-personal"]

        # The same list card can produce a distinct inbound event id.  Its
        # target-selection marker must prevent another context/RPC.
        card_count = len(cards.cards)
        with pytest.raises(ManagementUserError):
            controller.handle(
                _message("duplicate-selection", "选定p01", reply_to=list_id)
            )
        assert len(cards.cards) == card_count
        assert remote.prepared == ["thread-personal"]

        # A later direct slash uses the persisted binding and does not reopen
        # a target selector.
        controller.handle(_message("bound-goal", "/goal"))
        # A bound direct command creates a private routing context and returns
        # only the requested result; it does not add a noisy target card.
        assert len(cards.cards) == card_count
        assert remote.prepared == ["thread-personal", "thread-personal"]
        assert cards.cards[-1][0] != selector_id
        assert state.management_context_record_for_message(remote_id) is not None
    finally:
        state.close()


def test_current_binding_controls_are_isolated_and_fail_closed_for_stale_target(
    tmp_path: Path,
) -> None:
    cards = CaptureCardSender()
    remote = FakeRemoteControl()
    controller, state, _tools, sender = _controller(
        tmp_path, card_sender=cards, remote_control=remote
    )
    try:
        state.set_current_thread(
            "ou_owner",
            "oc_private",
            "thread-personal",
            "个人会话",
            now=int(time.time()),
        )
        controller.handle(_message("view-binding", "." + CURRENT_THREAD_VIEW_COMMAND))
        assert "个人会话" in sender.messages[-1][1]
        assert "thread-personal" not in sender.messages[-1][1]
        assert r"C:\Personal" not in sender.messages[-1][1]

        # Human-facing button labels are also accepted as text fallbacks.
        controller.handle(_message("view-binding-label", "." + CURRENT_THREAD_VIEW_LABEL))
        assert "当前会话：《个人会话》" in sender.messages[-1][1]

        controller.handle(_message("switch-binding", "." + CURRENT_THREAD_SWITCH_LABEL))
        switch_id = cards.cards[-1][0]
        switch_context = state.management_context_record_for_message(switch_id)
        assert switch_context is not None
        assert switch_context.payload["selection_mode"] == "binding_switch"

        controller.handle(
            _message(
                "switch-personal",
                "选择个人会话",
                reply_to=switch_id,
                source_kind="card_action",
                action_name="select_personal",
            )
        )
        switch_list_id = cards.cards[-1][0]
        controller.handle(_message("switch-thread", "选定p01", reply_to=switch_list_id))
        assert state.current_thread("ou_owner", "oc_private")["thread_id"] == (
            "thread-personal"
        )

        # Another owner/chat has no access to this binding.
        controller.handle(
            _message(
                "other-owner-view",
                "." + CURRENT_THREAD_VIEW_COMMAND,
                sender_id="ou_other",
                chat_id="oc_private",
            )
        )
        assert "没有绑定" in sender.messages[-1][1]

        controller.handle(_message("clear-binding", "." + CURRENT_THREAD_CLEAR_LABEL))
        assert state.current_thread("ou_owner", "oc_private") is None
        assert "已清除当前会话绑定" in sender.messages[-1][1]

        # A stale binding never falls back to a selector or opens an RPC.
        state.set_current_thread(
            "ou_owner",
            "oc_private",
            "missing-thread",
            now=int(time.time()),
        )
        prepared_before = list(remote.prepared)
        controller.handle(_message("stale-command", "/goal"))
        assert "不存在或已归档" in sender.messages[-1][1]
        assert remote.prepared == prepared_before
    finally:
        state.close()


def test_only_single_line_row_head_slash_enters_management_router(
    tmp_path: Path,
) -> None:
    controller, state, _tools, sender = _controller(tmp_path)
    try:
        assert controller.accepts(_message("unknown", "/not-a-command")) is True
        controller.handle(_message("unknown", "/not-a-command"))
        assert "无法识别或解析" in sender.messages[-1][1]
        assert controller.accepts(_message("embedded", "普通文字 /goal")) is False
        assert controller.accepts(_message("later-line", "普通文字\n/goal")) is False
        assert controller.accepts(_message("leading-space", " /goal")) is False
        assert controller.accepts(_message("malformed-multiline", "/goal 目标\n/plan 另一项")) is False
    finally:
        state.close()


def test_remote_control_buttons_open_owner_bound_single_form_cards(
    tmp_path: Path,
) -> None:
    cards = CaptureCardSender()
    remote = FakeRemoteControl()
    controller, state, _tools, sender = _controller(
        tmp_path, card_sender=cards, remote_control=remote
    )
    thread = {"id": "thread-personal", "title": "个人会话"}
    try:
        overview_context = state.create_management_context(
            "thread_overview",
            {"thread": thread, "group": "个人会话"},
            sender_id="ou_owner",
            chat_id="oc_private",
        )
        state.bind_management_messages(overview_context, ("om_overview_remote",))
        controller.handle(
            _message(
                "synthetic-main",
                REMOTE_CONTROL_ENTRY_COMMAND,
                reply_to="om_overview_remote",
            )
        )
        main_id = cards.cards[-1][0]
        assert _card_tags(cards.cards[-1][1], "form") == []

        # Simulate a real controller card sent before this UI was slimmed
        # down.  Its frozen action fingerprints must remain valid so already
        # delivered cards do not break after deployment.
        legacy_id = "om_legacy_remote_control"
        legacy_context = state.create_management_context(
            "remote_control",
            {
                "thread": thread,
                "group": "个人会话",
                "_card_source": "owner_dm",
                "_card_action_fingerprints": [
                    remote_control_action_fingerprint(
                        remote_control_action("goal_set_open")
                    ),
                    remote_control_action_fingerprint(
                        remote_control_action("plan_start_open")
                    ),
                ],
            },
            sender_id="ou_owner",
            chat_id="oc_private",
        )
        state.bind_management_messages(legacy_context, (legacy_id,))

        with pytest.raises(ManagementUserError):
            controller.handle(
                _message(
                    "goal-form-intruder",
                    "设置 Goal",
                    reply_to=legacy_id,
                    sender_id="ou_intruder",
                    chat_id="oc_other",
                    source_kind="card_action",
                    action_name="goal_set_open",
                )
            )

        controller.handle(
            _message(
                "goal-form-open",
                "设置 Goal",
                reply_to=legacy_id,
                source_kind="card_action",
                action_name="goal_set_open",
            )
        )
        goal_id, goal_card, _key = cards.cards[-1]
        assert [row["name"] for row in _card_tags(goal_card, "form")] == [
            "goal_set_form"
        ]
        goal_context = state.management_context_record_for_message(goal_id)
        assert goal_context is not None
        assert goal_context.context_kind == "remote_goal_set_form"
        assert (goal_context.sender_id, goal_context.chat_id) == (
            "ou_owner",
            "oc_private",
        )

        controller.handle(
            _message(
                "goal-form-submit",
                "/goal 单表单目标",
                reply_to=goal_id,
                source_kind="card_action",
                action_name="goal_set_form",
            )
        )
        assert "已设置正式 Goal" in sender.messages[-1][1]
        assert [call for call in remote.session.calls if call[0] == "set_goal"] == [
            ("set_goal", "单表单目标")
        ]

        controller.handle(
            _message(
                "plan-form-open",
                "启动 Plan",
                reply_to=legacy_id,
                source_kind="card_action",
                action_name="plan_start_open",
            )
        )
        plan_id, plan_card, _key = cards.cards[-1]
        assert [row["name"] for row in _card_tags(plan_card, "form")] == [
            "plan_start_form"
        ]
        plan_context = state.management_context_record_for_message(plan_id)
        assert plan_context is not None
        assert plan_context.context_kind == "remote_plan_start_form"
    finally:
        state.close()


def test_remote_form_230099_rejection_never_masquerades_as_success(
    tmp_path: Path,
) -> None:
    cards = RejectNthFormatCardSender(reject_on_call=2)
    remote = FakeRemoteControl()
    controller, state, _tools, sender = _controller(
        tmp_path, card_sender=cards, remote_control=remote
    )
    try:
        overview_context = state.create_management_context(
            "thread_overview",
            {
                "thread": {"id": "thread-personal", "title": "个人会话"},
                "group": "个人会话",
            },
            sender_id="ou_owner",
            chat_id="oc_private",
        )
        state.bind_management_messages(
            overview_context, ("om_overview_remote_230099",)
        )
        controller.handle(
            _message(
                "synthetic-230099-main",
                REMOTE_CONTROL_ENTRY_COMMAND,
                reply_to="om_overview_remote_230099",
            )
        )
        main_id = cards.cards[-1][0]
        legacy_id = "om_legacy_remote_control_230099"
        legacy_context = state.create_management_context(
            "remote_control",
            {
                "thread": {"id": "thread-personal", "title": "个人会话"},
                "group": "个人会话",
                "_card_source": "owner_dm",
                "_card_action_fingerprints": [
                    remote_control_action_fingerprint(
                        remote_control_action("goal_set_open")
                    )
                ],
            },
            sender_id="ou_owner",
            chat_id="oc_private",
        )
        state.bind_management_messages(legacy_context, (legacy_id,))
        controller.handle(
            _message(
                "synthetic-230099-open",
                "设置 Goal",
                reply_to=legacy_id,
                source_kind="card_action",
                action_name="goal_set_open",
            )
        )

        assert len(cards.attempts) == 2 and len(cards.cards) == 1
        assert len(sender.messages) == 1
        fallback_id, fallback_text, fallback_key = sender.messages[0]
        assert "无法显示“设置 Goal”卡片" in fallback_text
        assert "本次没有执行" in fallback_text
        assert "已设置正式 Goal" not in fallback_text
        assert fallback_key.endswith(":card-format-fallback")
        assert [call for call in remote.session.calls if call[0] == "set_goal"] == []
        fallback_context = state.management_context_record_for_message(fallback_id)
        assert fallback_context is not None
        assert fallback_context.context_kind == "remote_goal_set_form"
    finally:
        state.close()


def test_goal_clear_requires_separate_confirmation_context_and_is_once_only(
    tmp_path: Path,
) -> None:
    cards = CaptureCardSender()
    remote = FakeRemoteControl()
    controller, state, _tools, sender = _controller(
        tmp_path, card_sender=cards, remote_control=remote
    )
    try:
        # Directly create an owner-bound remote context without touching Desktop.
        context_id = state.create_management_context(
            "remote_control",
            {"thread": {"id": "thread-personal", "title": "个人会话"}, "group": "个人会话"},
            sender_id="ou_owner",
            chat_id="oc_private",
        )
        state.bind_management_messages(context_id, ("om_remote",))
        controller.handle(_message("clear-request", "/goal clear", reply_to="om_remote"))
        assert [call for call in remote.session.calls if call[0] == "clear_goal"] == []
        confirm_id = cards.cards[-1][0]
        confirm_context = state.management_context_record_for_message(confirm_id)
        assert confirm_context is not None
        assert confirm_context.context_kind == "remote_goal_clear_confirm"
        controller.handle(
            _message(
                "clear-confirm",
                "/goal clear confirm",
                reply_to=confirm_id,
                source_kind="card_action",
                action_name="goal_clear_confirm",
            )
        )
        assert "已清除正式 Goal" in sender.messages[-1][1]
        controller.handle(
            _message(
                "clear-confirm-again",
                "/goal clear confirm",
                reply_to=confirm_id,
                source_kind="card_action",
                action_name="goal_clear_confirm",
            )
        )
        assert "已经成功执行" in sender.messages[-1][1]
        assert [call for call in remote.session.calls if call[0] == "clear_goal"] == [
            ("clear_goal", None)
        ]
    finally:
        state.close()


def test_plan_busy_releases_before_submit_then_retry_succeeds(
    tmp_path: Path,
) -> None:
    remote_session = FakeRemoteSession()
    remote_session.active_checks = [True]
    remote = FakeRemoteControl(remote_session)
    controller, state, _tools, sender = _controller(
        tmp_path, remote_control=remote
    )
    try:
        context_id = state.create_management_context(
            "remote_control",
            {"thread": {"id": "thread-personal", "title": "个人会话"}, "group": "个人会话"},
            sender_id="ou_owner",
            chat_id="oc_private",
        )
        state.bind_management_messages(context_id, ("om_remote",))
        with pytest.raises(ManagementUserError, match="正在运行"):
            controller.handle(_message("plan-busy", "/plan 制定测试方案", reply_to="om_remote"))
        action = parse_remote_command("/plan 制定测试方案")
        assert action is not None
        row = state.remote_control_action(context_id, "plan_start", action.request_hash())
        assert row is not None and row["state"] == "rejected"

        remote_session.active_checks = [False, False]
        controller.handle(_message("plan-retry", "/plan 制定测试方案", reply_to="om_remote"))
        assert "Plan 模式已启动" in sender.messages[-1][1]
        row = state.remote_control_action(context_id, "plan_start", action.request_hash())
        assert row is not None and row["state"] == "succeeded"
        assert [call for call in remote_session.calls if call[0] == "start_plan"]
    finally:
        state.close()


def test_submitted_timeout_freezes_remote_action_and_never_replays(
    tmp_path: Path,
) -> None:
    remote_session = FakeRemoteSession()
    remote_session.write_error = CodexRPCTimeout("synthetic timeout")
    remote = FakeRemoteControl(remote_session)
    controller, state, _tools, sender = _controller(
        tmp_path, remote_control=remote
    )
    try:
        context_id = state.create_management_context(
            "remote_control",
            {"thread": {"id": "thread-personal", "title": "个人会话"}, "group": "个人会话"},
            sender_id="ou_owner",
            chat_id="oc_private",
        )
        state.bind_management_messages(context_id, ("om_remote",))
        controller.handle(_message("goal-timeout", "/goal 可能已写入", reply_to="om_remote"))
        assert "结果无法确认" in sender.messages[-1][1]
        action = parse_remote_command("/goal 可能已写入")
        assert action is not None
        row = state.remote_control_action(context_id, "goal_set", action.request_hash())
        assert row is not None and row["state"] == "uncertain"
        controller.handle(_message("goal-timeout-dup", "/goal 可能已写入", reply_to="om_remote"))
        assert "已冻结自动重试" in sender.messages[-1][1]
        assert len([call for call in remote_session.calls if call[0] == "set_goal"]) == 1
    finally:
        state.close()


def test_session_query_entry_has_plain_text_fallback_without_card_channel(
    tmp_path: Path,
) -> None:
    controller, state, _tools, sender = _controller(tmp_path)
    try:
        controller.handle(_message("query-menu-fallback", "." + SESSION_QUERY_ENTRY_COMMAND))
        assert "查询项目列表" in sender.messages[-1][1]
        assert "查询个人会话" in sender.messages[-1][1]
        assert "搜索会话" in sender.messages[-1][1]
    finally:
        state.close()


def test_input_forms_use_classic_submit_protocol_without_cardkit_v2_mixing() -> None:
    search_card = build_session_search_form_card()
    assert "schema" not in search_card
    assert "body" not in search_card
    assert isinstance(search_card.get("elements"), list)
    search_inputs = {
        str(item["name"]): item
        for item in _card_tags(search_card, "input")
    }
    assert {
        name: item["max_length"] for name, item in search_inputs.items()
    } == {
        "session_name": 200,
        "session_description": 1000,
        "session_activity": 100,
    }
    search_submit = next(
        item
        for item in _card_tags(search_card, "button")
        if item.get("name") == "session_search_submit"
    )
    assert search_submit["action_type"] == "form_submit"
    assert "form_action_type" not in search_submit

    overview_card = build_thread_overview_card(
        title="项目会话",
        facts=(("归属", "飞书机器人"),),
        sections=(("最后一轮结果", "阶段性结果。"),),
        monitor_status="未监测",
    )
    assert _card_tags(overview_card, "form") == []
    assert _card_tags(overview_card, "input") == []
    assert session_query_action("thread_reply_open") in _card_values(overview_card)

    reply_card = build_thread_reply_form_card("项目会话")
    assert "schema" not in reply_card
    assert "body" not in reply_card
    assert isinstance(reply_card.get("elements"), list)
    overview_submit = next(
        item
        for item in _card_tags(reply_card, "button")
        if item.get("name") == "thread_reply_submit"
    )
    assert overview_submit["action_type"] == "form_submit"
    assert "form_action_type" not in overview_submit
    overview_inputs = _card_tags(reply_card, "input")
    assert len(overview_inputs) == 1
    assert overview_inputs[0]["name"] == "thread_reply"
    assert overview_inputs[0]["max_length"] == 1000
    assert all(
        1 <= int(item["max_length"]) <= 1000
        for item in (*search_inputs.values(), *overview_inputs)
    )


def test_thread_overview_facts_use_native_mobile_label_value_rows() -> None:
    facts = (
        ("归属", "项目·一个很长但必须完整保留的项目名称"),
        ("状态", "cancelled"),
        ("最近更新", "2026-09-01 12:34（北京时间，附加说明仍需换行显示）"),
        ("监测状态", "未监测"),
        ("最后一轮状态", "cancelled"),
        ("最后一轮时间", ""),
    )
    card = build_thread_overview_card(
        title="移动端会话概览",
        facts=facts,
        sections=(("最后一轮结果", "保持既有正文分区。"),),
        monitor_status="未监测",
    )

    body = card["body"]["elements"]
    fact_rows = body[: len(facts)]
    assert [row["tag"] for row in fact_rows] == ["column_set"] * len(facts)
    assert all(len(row["columns"]) == 2 for row in fact_rows)
    assert all(
        [column["weight"] for column in row["columns"]] == [1, 4]
        and all(column["vertical_align"] == "top" for column in row["columns"])
        for row in fact_rows
    )

    visible_rows = [
        tuple(
            column["elements"][0]["text"]["content"]
            for column in row["columns"]
        )
        for row in fact_rows
    ]
    assert visible_rows == [
        ("归属：", "项目·一个很长但必须完整保留的项目名称"),
        ("状态：", "cancelled"),
        ("最近更新：", "2026-09-01 12:34（北京时间，附加说明仍需换行显示）"),
        ("监测状态：", "未监测"),
        ("最后一轮状态：", "cancelled"),
        ("最后一轮时间：", "—"),
    ]
    assert all(
        column["elements"][0]["text"]["tag"] == "plain_text"
        for row in fact_rows
        for column in row["columns"]
    )
    assert all(
        row["columns"][0]["elements"][0]["text"]["text_size"] == "notation"
        and row["columns"][0]["elements"][0]["text"]["text_color"] == "grey"
        for row in fact_rows
    )
    assert "**" not in json.dumps(fact_rows, ensure_ascii=False)


def test_explicit_card_format_rejection_falls_back_to_bound_text_once(
    tmp_path: Path,
) -> None:
    cards = RejectFormatCardSender()
    controller, state, _tools, sender = _controller(tmp_path, card_sender=cards)
    try:
        controller.handle(_message("query-menu-format-rejected", "." + SESSION_QUERY_ENTRY_COMMAND))
        assert len(cards.calls) == 1
        assert len(sender.messages) == 1
        message_id, text, key = sender.messages[0]
        assert "查询项目列表" in text
        assert key.endswith(":card-format-fallback")
        context = state.management_context_record_for_message(message_id)
        assert context is not None
        assert context.context_kind == "session_query_menu"
        assert context.sender_id == "ou_owner"
        assert context.chat_id == "oc_private"
    finally:
        state.close()


def test_bot_menu_card_first_action_binds_private_chat_and_rejects_wrong_action(
    tmp_path: Path,
) -> None:
    cards = CaptureCardSender()
    controller, state, tools, sender = _controller(tmp_path, card_sender=cards)
    try:
        controller.handle(
            _message(
                "menu-event-1",
                SESSION_QUERY_ENTRY_COMMAND,
                chat_id="",
                source_kind="bot_menu",
            )
        )
        card_id, _card, _key = cards.cards[-1]
        before = state.management_context_record_for_message(card_id)
        assert before is not None
        assert before.sender_id == "ou_owner"
        assert before.chat_id == ""
        assert before.payload["_card_source"] == "owner_open_id_direct"

        with pytest.raises(ManagementUserError, match="无法验证来源"):
            controller.handle(
                _message(
                    "menu-wrong-action",
                    "继续",
                    reply_to=card_id,
                    source_kind="card_action",
                    action_name="thread_reply_submit",
                )
            )

        controller.handle(
            _message(
                "menu-valid-action",
                "查询个人会话",
                reply_to=card_id,
                source_kind="card_action",
                action_name="personal_sessions",
            )
        )
        after = state.management_context_record_for_message(card_id)
        assert after is not None
        assert after.chat_id == "oc_private"
        assert cards.cards[-1][1]["header"]["title"]["content"] == "Codex 个人会话"
        assert sender.messages == []
        assert tools.closed == 1
    finally:
        state.close()


def test_card_action_cannot_target_plain_text_fallback_context(tmp_path: Path) -> None:
    controller, state, _tools, sender = _controller(tmp_path)
    try:
        controller.handle(_message("plain-menu", "." + SESSION_QUERY_ENTRY_COMMAND))
        plain_message_id = sender.messages[-1][0]
        with pytest.raises(ManagementUserError, match="无法验证来源"):
            controller.handle(
                _message(
                    "forged-card-action",
                    "查询个人会话",
                    reply_to=plain_message_id,
                    source_kind="card_action",
                    action_name="personal_sessions",
                )
            )
    finally:
        state.close()


def test_session_query_action_protocol_is_strict() -> None:
    assert session_query_command(
        {
            "namespace": "progress_wx.session_query",
            "version": 1,
            "action": "search_sessions",
        }
    ) == "搜索会话"
    assert session_query_command({"action": "search_sessions"}) is None
    assert session_query_command(
        {
            "namespace": "progress_wx.session_query",
            "version": True,
            "action": "search_sessions",
        }
    ) is None
    assert session_query_command(
        {
            "namespace": "progress_wx.session_query",
            "version": 1,
            "action": ["search_sessions"],
        }
    ) is None
    assert session_query_command(
        {
            "namespace": "progress_wx.session_query",
            "version": 1,
            "action": "search_sessions",
            "unexpected": "archive",
        }
    ) is None
    assert session_query_command(
        session_query_action("expand_project", label="A01")
    ) == "展开A01"
    assert session_query_command(
        session_query_action("select_personal_thread", label="p20")
    ) == "选定p20"
    assert session_query_command(
        session_query_action("monitor_project_thread", label="a03")
    ) == "添加监测a03"
    assert session_query_command(
        session_query_action("list_page", page=2)
    ) == "第2页"
    assert session_query_command(
        {
            **session_query_action("list_page", page=2),
            "label": "p01",
        }
    ) is None
    with pytest.raises(ValueError):
        session_query_action("expand_project", label="p01")
    with pytest.raises(ValueError):
        session_query_action("list_page", page=True)


def test_session_query_form_protocol_is_strict() -> None:
    reply_action = session_query_action("thread_reply_submit")
    assert session_query_form_command(
        reply_action,
        {"thread_reply": "  继续完成回归测试  "},
    ) == "  继续完成回归测试  "
    assert session_query_form_command(reply_action, {"thread_reply": ""}) == ""
    assert session_query_form_command(
        reply_action,
        {"thread_reply": "继续", "thread_id": "thread-other"},
    ) is None
    assert session_query_form_command(
        reply_action,
        {"thread_reply": "x" * 4001},
    ) is None
    assert session_query_form_command(
        reply_action,
        {"thread_reply": "继续\x00执行"},
    ) is None
    assert session_query_form_command(
        reply_action,
        {"thread_reply": "第一行\n第二行"},
    ) == "第一行\n第二行"

    search_action = session_query_action("session_search_submit")
    assert session_query_form_command(
        search_action,
        {
            "session_name": "  医疗科技选题 ",
            "session_description": " 找昨天讨论过的机器人 ",
            "session_activity": " 最近7天 ",
        },
    ) == (
        "会话名称：医疗科技选题\n"
        "会话描述：找昨天讨论过的机器人\n"
        "会话最后活动时间：最近7天"
    )
    assert session_query_form_command(
        search_action,
        {"session_description": "只记得这一项"},
    ) == (
        "会话名称：\n"
        "会话描述：只记得这一项\n"
        "会话最后活动时间："
    )
    assert session_query_form_command(search_action, {}) == (
        "会话名称：\n"
        "会话描述：\n"
        "会话最后活动时间："
    )
    assert session_query_form_command(
        search_action,
        {"session_description": "线索", "thread_id": "thread-other"},
    ) is None
    assert session_query_form_command(
        search_action,
        {"session_description": ["不是字符串"]},
    ) is None
    assert session_query_form_command(
        search_action,
        {"session_description": "伪造字段\n会话最后活动时间：全部"},
    ) is None
    assert session_query_form_command(
        search_action,
        {"session_name": "标题\x00尾部"},
    ) is None


@pytest.mark.parametrize(
    "content",
    (
        " /goal 这只是普通提示词",
        "\n/plan 这也是普通提示词",
        "\r\n/goal 这仍然是普通提示词",
        "/goal 第一行只是正文\n第二行继续说明",
        "  $skill-one 这仍然是普通提示词",
    ),
)
def test_thread_reply_form_preserves_leading_control_like_text_as_prompt(
    tmp_path: Path,
    content: str,
) -> None:
    """A form value with leading whitespace must not enter the control plane."""

    cards = CaptureCardSender()
    remote_session = FakeRemoteSession()
    remote = FakeRemoteControl(remote_session)
    controller, state, tools, _sender = _controller(
        tmp_path, card_sender=cards, remote_control=remote
    )
    try:
        controller._active_owner = _message("leading-owner", "查询个人会话")
        controller._send_overview(
            {
                "id": "thread-project",
                "title": "项目会话",
                "hostId": "local",
                "status": "idle",
                "summary": "合成结果。",
                "updatedAt": 100,
            },
            "飞书机器人",
            f"leading-overview-{hash(content)}",
        )
        controller._active_owner = None
        overview_id = cards.cards[-1][0]
        controller.handle(
            _message(
                f"leading-open-{hash(content)}",
                "继续对话",
                reply_to=overview_id,
                source_kind="card_action",
                action_name="thread_reply_open",
            )
        )
        reply_card_id = cards.cards[-1][0]
        form_command = session_query_form_command(
            session_query_action("thread_reply_submit"),
            {"thread_reply": content},
        )
        assert form_command == content
        controller.handle(
            _message(
                f"leading-submit-{hash(content)}",
                form_command,
                reply_to=reply_card_id,
                source_kind="card_action",
                action_name="thread_reply_submit",
            )
        )
        assert tools.sent_prompts == [
            ("thread-project", content, "source-thread", "local")
        ]
        assert remote.prepared == []
    finally:
        state.close()


def _card_tags(value: object, tag: str) -> list[dict]:
    found: list[dict] = []
    if isinstance(value, dict):
        if value.get("tag") == tag:
            found.append(value)
        for child in value.values():
            found.extend(_card_tags(child, tag))
    elif isinstance(value, list):
        for child in value:
            found.extend(_card_tags(child, tag))
    return found


def _card_values(value: object) -> list[dict]:
    found: list[dict] = []
    if isinstance(value, dict):
        if value.get("tag") == "button" and isinstance(value.get("value"), dict):
            found.append(value["value"])
        for child in value.values():
            found.extend(_card_values(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_card_values(child))
    return found


def _card_button_texts(value: object) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        if value.get("tag") == "button" and isinstance(value.get("text"), dict):
            found.append(str(value["text"].get("content") or ""))
        for child in value.values():
            found.extend(_card_button_texts(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_card_button_texts(child))
    return found


def test_query_result_cards_keep_frozen_context_and_chain_clicks(tmp_path: Path) -> None:
    card_sender = CaptureCardSender()
    controller, state, tools, sender = _controller(
        tmp_path, card_sender=card_sender
    )
    try:
        controller.handle(_message("project-card-entry", ".查询项目列表"))
        project_message_id, project_card, _key = card_sender.cards[-1]
        project_values = _card_values(project_card)
        assert session_query_action("expand_project", label="A01") in project_values
        assert session_query_action("new_project") in project_values
        project_context = state.management_context_record_for_message(
            project_message_id
        )
        assert project_context is not None
        assert project_context.context_kind == "project_list"
        assert project_context.payload["projects"][0]["project_id"] == "project-1"

        forged = session_query_action("expand_project", label="A99")
        with pytest.raises(ManagementUserError, match="无法验证来源"):
            controller.handle(
                _message(
                    "project-card-forged-expand",
                    "展开A99",
                    reply_to=project_message_id,
                    source_kind="card_action",
                    action_name="expand_project",
                    action_fingerprint=session_query_action_fingerprint(forged) or "",
                )
            )

        expand_a01 = session_query_action("expand_project", label="A01")
        controller.handle(
            _message(
                "project-card-expand",
                "展开A01",
                reply_to=project_message_id,
                source_kind="card_action",
                action_name="expand_project",
                action_fingerprint=session_query_action_fingerprint(expand_a01) or "",
            )
        )
        thread_message_id, thread_card, _key = card_sender.cards[-1]
        thread_values = _card_values(thread_card)
        assert session_query_action(
            "select_project_thread", label="a01"
        ) in thread_values
        assert session_query_action(
            "monitor_project_thread", label="a01"
        ) not in thread_values
        assert "项目会话" in _card_button_texts(thread_card)
        assert not any(
            text.startswith("a01") for text in _card_button_texts(thread_card)
        )
        assert "监测" not in _card_button_texts(thread_card)
        thread_context = state.management_context_record_for_message(thread_message_id)
        assert thread_context is not None
        assert thread_context.context_kind == "project_threads"
        assert thread_context.payload["threads"][0]["id"] == "thread-project"
        assert sender.messages == []
        assert tools.closed == 2
    finally:
        state.close()


def test_project_thread_overview_format_rejection_falls_back_with_frozen_context(
    tmp_path: Path,
) -> None:
    cards = RejectNthFormatCardSender(reject_on_call=3)
    controller, state, _tools, sender = _controller(tmp_path, card_sender=cards)
    raw_final = "该项目会话的稳定最终原文"
    completed = TurnRecord(
        thread_id="thread-project",
        turn_id="turn-project-frozen",
        status=ThreadStatus.COMPLETED,
        completed_at=300000,
        final_message=raw_final,
    )
    controller.codex_store.turns["thread-project"] = completed
    controller.codex_store.completed_turns["thread-project"] = completed
    try:
        controller.handle(_message("project-fallback-entry", ".查询项目列表"))
        project_message_id = cards.cards[-1][0]
        expand = session_query_action("expand_project", label="A01")
        controller.handle(
            _message(
                "project-fallback-expand",
                "展开A01",
                reply_to=project_message_id,
                source_kind="card_action",
                action_name="expand_project",
                action_fingerprint=session_query_action_fingerprint(expand) or "",
            )
        )
        thread_message_id = cards.cards[-1][0]
        select = session_query_action("select_project_thread", label="a01")
        controller.handle(
            _message(
                "project-fallback-select",
                "选定a01",
                reply_to=thread_message_id,
                source_kind="card_action",
                action_name="select_project_thread",
                action_fingerprint=session_query_action_fingerprint(select) or "",
            )
        )

        assert len(cards.attempts) == 3
        assert len(cards.cards) == 2
        assert len(sender.messages) == 1
        fallback_id, fallback_text, fallback_key = sender.messages[0]
        assert "会话名称：项目会话" in fallback_text
        assert "最后一轮结果：该项目会话的稳定最终原文" in fallback_text
        assert "Codex Desktop 当前无法完成" not in fallback_text
        assert fallback_key.endswith(":card-format-fallback")

        context = state.management_context_record_for_message(fallback_id)
        assert context is not None
        assert context.context_kind == "thread_overview"
        assert context.payload["thread"]["id"] == "thread-project"
        assert context.payload["group"] == "飞书机器人"
        snapshot = context.payload["query_snapshot"]
        assert snapshot["turn_id"] == "turn-project-frozen"
        assert snapshot["raw_final"] == raw_final
        assert snapshot["raw_sha256"] == hashlib.sha256(
            raw_final.encode("utf-8")
        ).hexdigest()
        assert len(snapshot["snapshot_key"]) == 64
        inbound = state._connection.execute(
            "SELECT completed_at FROM management_inbound_messages WHERE message_id=?",
            ("project-fallback-select",),
        ).fetchone()
        assert inbound is not None
        assert inbound["completed_at"] is not None
    finally:
        state.close()


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
        message = _message("quota-1", ".查询剩余额度")
        assert controller.accepts(message) is True
        controller.handle(message)
        assert sender.messages[-1][1] == "Codex 每周额度：88%\n剩余重置卡：0 张"
        assert tools.closed == 0
    finally:
        state.close()


def test_project_history_context_selects_and_continues_exact_prompt(tmp_path: Path) -> None:
    controller, state, tools, sender = _controller(tmp_path)
    try:
        controller.handle(_message("in-1", ".查询项目列表"))
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
        controller.handle(_message("in-1", ".查询项目列表"))
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
    image_path = tmp_path / "generated_images" / "thread-project" / "item-image.png"
    image_path.parent.mkdir(parents=True)
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
        controller.handle(_message("in-1", ".查询项目列表"))
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
        controller.handle(_message("in-1", ".查询项目列表"))
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
        controller.handle(_message("in-1", ".查询个人会话"))
        text = sender.messages[-1][1]
        assert "p01｜个人会话" in text
        assert "项目会话" not in text

        controller.handle(_message("in-2", "选定p01", reply_to="om_1"))
        overview = sender.messages[-1][1]
        blocks = overview.split("\n\n")
        assert len(blocks) == 5
        assert blocks[0] == "已设为当前会话：《个人会话》"
        identity = blocks[1].splitlines()
        assert identity[:3] == [
            "会话名称：个人会话",
            "归属：个人会话",
            "状态：idle",
        ]
        assert identity[3].startswith("最近更新：")
        assert identity[4] == "监测状态：未监测"
        assert blocks[2] == "整体概览：个人任务概览。"
        assert blocks[3] == "最后一轮结果：完整测试通过，服务已经启动。"
        assert blocks[4].splitlines() == [
            "操作说明：",
            "- 继续会话：直接回复本消息并发送文字",
            "- 管理监测：回复“添加监测”或“移除监测”",
            "- 查看本次原文：回复“.原文”（本次查询限一次）",
            "- 归档该会话：回复“.归档”（本次查询限一次）",
        ]
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
        controller.handle(_message("in-1", ".查询个人会话"))
        personal = sender.messages[-1][1]
        assert "第 51 条个人会话" in personal
        assert "第 51 条项目会话" not in personal

        controller.handle(_message("in-2", ".查询项目列表"))
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
        controller.handle(_message("in-1", ".查询个人会话"))
        personal = sender.messages[-1][1]
        assert "旧版项目会话" not in personal
        assert "未命名会话" not in personal

        controller.handle(_message("in-2", ".查询项目列表"))
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
        controller.handle(_message("in-1", ".查询个人会话"))
        assert "p01｜个人会话" in sender.messages[-1][1]

        controller.handle(_message("in-2", "添加监测p01", reply_to="om_1"))
        subscriptions = state.monitor_subscriptions()
        assert [(item["thread_id"], item["origin"]) for item in subscriptions] == [
            ("thread-personal", "manual")
        ]

        controller.handle(_message("in-3", ".查询监测列表"))
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
        controller.handle(_message("in-1", ".新建个人会话"))
        controller.handle(
            _message("in-2", "首轮对话提示词：测试自动监测", reply_to="om_1")
        )
        subscriptions = state.monitor_subscriptions()
        assert [(item["thread_id"], item["origin"]) for item in subscriptions] == [
            ("created-1", "auto")
        ]
    finally:
        state.close()


def test_usage_command_sends_four_preview_images_then_text_version_hint(
    tmp_path: Path,
) -> None:
    image_sender = CaptureImageSender()
    controller, state, tools, sender = _controller(
        tmp_path, image_sender=image_sender
    )
    try:
        controller.handle(_message("in-1", ".使用说明"))
        assert [text for _, text, _ in sender.messages] == [
                "以上为使用说明，如果想要文字版使用说明，请发送“.文字版使用说明”哦"
        ]
        assert sender.messages[0][2] == "management-usage-footer:in-1:2026-09-08"
        assert len(image_sender.images) == 4
        from progress_wx.usage import feishu_usage_images
        assert [data for _, data, _ in image_sender.images] == [
            data for _, data in feishu_usage_images()
        ]
        assert all(
            data.startswith(b"\x89PNG\r\n\x1a\n")
            for _, data, _ in image_sender.images
        )
        assert [key for _, _, key in image_sender.images] == [
            f"management-usage:in-1:image:{index}:2026-09-08"
            for index in range(1, 5)
        ]
        usage_context = state.management_context_for_message("om_image_1")
        assert usage_context is not None
        for index in range(1, 5):
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
        controller.handle(_message("in-1", ".文字版使用说明"))
        guide = sender.messages[-1][1]
        assert "说明修订日期：2026-09-09" in guide
        assert "重要提醒：" in guide
        assert "查看会话：" in guide
        assert "新建对话：" in guide
        assert "继续已有对话：" in guide
        assert "发送图片：" in guide
        assert "管理进度监测：" in guide
        assert "查询监测列表" in guide
        assert "直接回复新建提示并输入要做的事即可" in guide
        assert "不必复制模板或添加字段名" in guide
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
        controller.handle(_message("in-1", ".查询项目列表"))
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


def _search_match(index: int, *, score: float = 0.8) -> SearchMatch:
    return SearchMatch(
        thread_id=f"search-thread-{index}",
        title=f"搜索候选 {index}",
        description=f"候选 {index} 的大白话说明",
        last_result=f"候选 {index} 已完成最后一轮工作",
        last_activity_at_beijing=f"2026-08-{20 + index:02d} 10:00",
        score=score,
        confidence="high" if score >= 0.8 else "medium",
        classification="strong_match" if score >= 0.8 else "possible_match",
        reason=f"第 {index} 项包含用户描述的流程证据",
        project_id="project-1",
        project_name="飞书机器人",
        archived=False,
        host_id="local",
        monitor={"monitored": False, "origin": None, "expires_at": None},
        snapshot_turn_id=f"search-turn-{index}",
        snapshot_content_hash=hashlib.sha256(
            f"search-content-{index}".encode("utf-8")
        ).hexdigest(),
        raw_final_snapshot=f"搜索候选 {index} 的完整最终答复原文",
    )


class FakeSessionSearch:
    def __init__(
        self,
        results,
        *,
        last_result: str = "搜索引擎生成的同款最后结果摘要",
        last_result_at: int = 1_777_777_777,
    ):
        self.results = list(results)
        self.requests = []
        self.last_result = last_result
        self.last_result_at = last_result_at

    def search(self, request):
        self.requests.append(request)
        if not self.results:
            raise AssertionError("没有为这次搜索准备结果")
        return self.results.pop(0)

    def last_result_for_thread(self, _thread_id):
        return self.last_result, self.last_result_at


class FakeOverviewSnapshotSearch:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def result_snapshot_for_thread(self, _thread_id):
        return self.snapshot


def test_session_search_card_form_reuses_search_and_thread_reply_controllers(
    tmp_path: Path,
) -> None:
    search = FakeSessionSearch([_search_result("found", [_search_match(1)])])
    cards = CaptureCardSender()
    controller, state, tools, sender = _controller(
        tmp_path,
        card_sender=cards,
        session_search=search,
    )
    try:
        controller.handle(_message("search-card-entry", ".搜索会话"))
        search_card_id, search_card, _key = cards.cards[-1]
        assert search_card["header"]["title"]["content"] == "模糊搜索 Codex 会话"
        assert {item.get("name") for item in _card_tags(search_card, "input")} == {
            "session_name",
            "session_description",
            "session_activity",
        }
        search_forms = _card_tags(search_card, "form")
        assert [item.get("name") for item in search_forms] == ["session_search_form"]
        assert "thread_id" not in json.dumps(search_card, ensure_ascii=False)
        search_context = state.management_context_record_for_message(search_card_id)
        assert search_context is not None
        assert search_context.context_kind == "session_search_form"
        assert search_context.payload["_card_source"] == "owner_dm"
        assert search_context.payload["_card_action_fingerprints"] == list(
            session_query_card_action_fingerprints(search_card)
        )

        controller.handle(
            _message(
                "search-card-submit",
                "会话名称：记错的标题\n"
                "会话描述：真正的语义线索\n"
                "会话最后活动时间：这几天",
                reply_to=search_card_id,
                source_kind="card_action",
                action_name="session_search_submit",
            )
        )
        assert search.requests[0].name == "记错的标题"
        assert search.requests[0].description == "真正的语义线索"
        assert search.requests[0].last_activity == "这几天"

        overview_card_id, overview_card, _key = cards.cards[-1]
        assert overview_card["header"]["title"]["content"] == "搜索候选 1"
        assert _card_tags(overview_card, "input") == []
        assert session_query_action("thread_reply_open") in _card_values(
            overview_card
        )
        overview_context = state.management_context_record_for_message(
            overview_card_id
        )
        assert overview_context is not None
        assert overview_context.context_kind == "thread_overview"
        assert overview_context.payload["thread"]["id"] == "search-thread-1"
        assert session_query_action_fingerprint(
            session_query_action("thread_reply_open")
        ) in overview_context.payload["_card_action_fingerprints"]
        assert "thread_id" not in json.dumps(overview_card, ensure_ascii=False)
        assert "search-thread-1" not in json.dumps(overview_card, ensure_ascii=False)

        controller.handle(
            _message(
                "overview-card-open",
                "继续对话",
                reply_to=overview_card_id,
                source_kind="card_action",
                action_name="thread_reply_open",
            )
        )
        reply_card_id, reply_card, _key = cards.cards[-1]
        assert reply_card["header"]["title"]["content"] == "继续 Codex 会话"
        assert [item.get("name") for item in _card_tags(reply_card, "input")] == [
            "thread_reply"
        ]
        assert session_query_action("thread_reply_submit") in _card_values(reply_card)
        assert "thread_id" not in json.dumps(reply_card, ensure_ascii=False)
        assert "search-thread-1" not in json.dumps(reply_card, ensure_ascii=False)
        reply_context = state.management_context_record_for_message(reply_card_id)
        assert reply_context is not None
        assert reply_context.context_kind == "thread_reply_form"
        assert reply_context.payload["thread"]["id"] == "search-thread-1"

        controller.handle(
            _message(
                "overview-card-submit",
                "继续把这个方案收尾",
                reply_to=reply_card_id,
                source_kind="card_action",
                action_name="thread_reply_submit",
            )
        )
        assert tools.sent_prompts == [
            (
                "search-thread-1",
                "继续把这个方案收尾",
                "source-thread",
                "local",
            )
        ]
        assert "执行结果：消息已发送" in sender.messages[-1][1]
        assert "提交状态：正文已原样送达" in sender.messages[-1][1]
    finally:
        state.close()


def test_regular_session_overview_is_a_bound_reply_form_card(tmp_path: Path) -> None:
    cards = CaptureCardSender()
    controller, state, tools, sender = _controller(tmp_path, card_sender=cards)
    try:
        controller._active_owner = _message("overview-owner", "查询个人会话")
        controller._send_overview(
            {
                "id": "thread-project",
                "title": "项目会话",
                "hostId": "local",
                "status": "idle",
                "summary": "已经完成阶段性工作。",
                "updatedAt": 100,
            },
            "飞书机器人",
            "regular-overview-card",
        )
        controller._active_owner = None
        overview_card_id, overview_card, _key = cards.cards[-1]
        assert overview_card["header"]["title"]["content"] == "项目会话"
        assert _card_tags(overview_card, "input") == []
        assert session_query_action("thread_reply_open") in _card_values(overview_card)
        context = state.management_context_record_for_message(overview_card_id)
        assert context is not None
        assert context.context_kind == "thread_overview"
        assert context.payload["thread"]["id"] == "thread-project"
        assert "thread_id" not in json.dumps(overview_card, ensure_ascii=False)
        assert "thread-project" not in json.dumps(overview_card, ensure_ascii=False)

        controller.handle(
            _message(
                "regular-overview-open",
                "继续对话",
                reply_to=overview_card_id,
                source_kind="card_action",
                action_name="thread_reply_open",
            )
        )
        reply_card_id, reply_card, _key = cards.cards[-1]
        assert [item.get("name") for item in _card_tags(reply_card, "input")] == [
            "thread_reply"
        ]
        reply_context = state.management_context_record_for_message(reply_card_id)
        assert reply_context is not None
        assert reply_context.context_kind == "thread_reply_form"
        assert reply_context.payload["thread"]["id"] == "thread-project"

        with pytest.raises(ManagementUserError, match="发送内容不能为空"):
            controller.handle(
                _message(
                    "regular-overview-empty",
                    "",
                    reply_to=reply_card_id,
                    source_kind="card_action",
                    action_name="thread_reply_submit",
                )
            )
        assert tools.sent_prompts == []

        controller.handle(
            _message(
                "regular-overview-submit",
                "开始下一步",
                reply_to=reply_card_id,
                source_kind="card_action",
                action_name="thread_reply_submit",
            )
        )
        assert tools.sent_prompts == [
            ("thread-project", "开始下一步", "source-thread", "local")
        ]
        assert "执行结果：消息已发送" in sender.messages[-1][1]
        assert "提交状态：正文已原样送达" in sender.messages[-1][1]
    finally:
        state.close()


def test_old_embedded_overview_form_remains_backward_compatible_and_idempotent(
    tmp_path: Path,
) -> None:
    controller, state, tools, sender = _controller(tmp_path)
    try:
        submit = session_query_action("thread_reply_submit")
        fingerprint = session_query_action_fingerprint(submit)
        assert fingerprint is not None
        context_id = state.create_management_context(
            "thread_overview",
            {
                "thread": {
                    "id": "thread-project",
                    "title": "项目会话",
                    "hostId": "local",
                },
                "group": "飞书机器人",
                "_card_source": "owner_dm",
                "_card_action_fingerprints": [fingerprint],
            },
            sender_id="ou_owner",
            chat_id="oc_private",
        )
        state.bind_management_messages(context_id, ("om_old_overview",))
        old_submit = _message(
            "old-overview-submit",
            "继续旧卡中的任务",
            reply_to="om_old_overview",
            source_kind="card_action",
            action_name="thread_reply_submit",
        )
        controller.handle(old_submit)
        controller.handle(old_submit)
        assert tools.sent_prompts == [
            ("thread-project", "继续旧卡中的任务", "source-thread", "local")
        ]
        assert "执行结果：消息已发送" in sender.messages[-1][1]
    finally:
        state.close()


@pytest.mark.parametrize(
    ("command", "call_name"),
    (
        ("/goal 回复我：你好", "set_goal"),
        ("/plan 规划下一步", "start_plan"),
        ("$skill-one 检查当前实现", "start_skill"),
    ),
)
def test_dedicated_thread_reply_form_routes_control_commands_to_official_plane(
    tmp_path: Path,
    command: str,
    call_name: str,
) -> None:
    cards = CaptureCardSender()
    remote_session = FakeRemoteSession()
    remote = FakeRemoteControl(remote_session)
    controller, state, tools, _sender = _controller(
        tmp_path, card_sender=cards, remote_control=remote
    )
    try:
        controller._active_owner = _message("overview-owner", "查询个人会话")
        controller._send_overview(
            {
                "id": "thread-project",
                "title": "项目会话",
                "hostId": "local",
                "status": "idle",
                "summary": "合成结果。",
                "updatedAt": 100,
            },
            "飞书机器人",
            f"control-overview-{call_name}",
        )
        controller._active_owner = None
        overview_id = cards.cards[-1][0]
        controller.handle(
            _message(
                f"open-{call_name}",
                "继续对话",
                reply_to=overview_id,
                source_kind="card_action",
                action_name="thread_reply_open",
            )
        )
        reply_card_id = cards.cards[-1][0]
        controller.handle(
            _message(
                f"submit-{call_name}",
                command,
                reply_to=reply_card_id,
                source_kind="card_action",
                action_name="thread_reply_submit",
            )
        )
        assert remote.prepared == ["thread-project"]
        assert any(call[0] == call_name for call in remote_session.calls)
        assert tools.sent_prompts == []
    finally:
        state.close()


def _search_result(status: str, matches=(), *, scope="recent_30d", next_scope="recent_180d"):
    can_expand = next_scope is not None
    return SearchResult(
        search_id=f"search-{scope}",
        status=status,
        scope=scope,
        scope_label={
            "recent_30d": "最近30天",
            "recent_180d": "最近180天",
            "all": "全部用户会话（含归档）",
        }[scope],
        can_expand=can_expand,
        next_scope=next_scope,
        cost_warning="扩大后预计检查 20 个会话，最多调用 2 轮 Luna。"
        if can_expand
        else "已经检查全部用户会话，不能再扩大范围。",
        matches=tuple(matches),
        examined_count=8,
        semantic_candidate_count=4,
        model_call_count=1,
        warnings=(),
    )


def test_session_search_top_command_returns_copyable_form_and_requires_quote(tmp_path: Path) -> None:
    search = FakeSessionSearch([_search_result("found", [_search_match(1)])])
    controller, state, _tools, sender = _controller(tmp_path, session_search=search)
    try:
        controller.handle(_message("search-in-1", ".搜索会话"))
        form_message_id, form, _key = sender.messages[-1]
        assert form == (
            "搜索 Codex 会话\n\n"
            "请回复本消息并保留字段名：\n"
            "会话名称：\n"
            "会话描述：\n"
            "会话最后活动时间："
        )
        # 未引用表单的正文不会被当作搜索提交。
        assert controller.accepts(_message("search-in-2", "会话名称：任意\n会话描述：线索\n会话最后活动时间：")) is False

        controller.handle(
            _message(
                "search-in-3",
                "会话名称：记错的标题\n会话描述：真正的语义线索\n会话最后活动时间：这几天",
                reply_to=form_message_id,
            )
        )
        assert search.requests[0].name == "记错的标题"
        assert search.requests[0].description == "真正的语义线索"
        assert search.requests[0].last_activity == "这几天"
        overview = sender.messages[-1][1]
        assert "会话名称：《搜索候选 1》" in overview
        assert "会话最后一轮结果：候选 1 已完成最后一轮工作" in overview
        assert "会话最后活动时间：2026-08-21 10:00" in overview
    finally:
        state.close()


def test_session_search_ambiguous_pages_three_and_flip_context_isolated(tmp_path: Path) -> None:
    matches = [_search_match(index, score=0.79 - index / 100) for index in range(1, 6)]
    search = FakeSessionSearch([_search_result("ambiguous", matches)])
    controller, state, _tools, sender = _controller(tmp_path, session_search=search)
    try:
        controller.handle(_message("search-list-1", ".搜索会话"))
        controller.handle(
            _message(
                "search-list-2",
                "会话名称：\n会话描述：流程线索\n会话最后活动时间：",
                reply_to="om_1",
            )
        )
        first_page_id, first_page, _key = sender.messages[-1]
        assert "页码：第 1/2 页" in first_page
        assert "1｜《搜索候选 1》" in first_page
        assert "3｜《搜索候选 3》" in first_page
        assert "4｜《搜索候选 4》" not in first_page
        assert first_page.split("\n\n") == [
            "列表类型：会话搜索候选\n搜索范围：最近30天\n页码：第 1/2 页\n总数：5 个",
            "会话列表：",
            "1｜《搜索候选 1》\n匹配度：78%\n匹配说明：第 1 项包含用户描述的流程证据",
            "2｜《搜索候选 2》\n匹配度：77%\n匹配说明：第 2 项包含用户描述的流程证据",
            "3｜《搜索候选 3》\n匹配度：76%\n匹配说明：第 3 项包含用户描述的流程证据",
            "操作说明：\n- 选择会话：回复“选择1”\n- 下一页：回复“翻页”",
        ]
        assert controller.accepts(_message("unquoted-flip", "翻页")) is False

        controller.handle(_message("search-list-3", "翻页", reply_to=first_page_id))
        second_page_id, second_page, _key = sender.messages[-1]
        assert "页码：第 2/2 页" in second_page
        assert "4｜《搜索候选 4》" in second_page
        assert "5｜《搜索候选 5》" in second_page
        assert "已到最后一页" in second_page

        controller.handle(_message("search-list-4", "翻页", reply_to=second_page_id))
        end_message_id, end_text, _key = sender.messages[-1]
        assert "已经到最后一页" in end_text

        controller.handle(_message("search-list-5", "选择4", reply_to=end_message_id))
        selected = sender.messages[-1][1]
        assert selected.split("\n\n") == [
            "会话名称：《搜索候选 4》\n归属：飞书机器人\n监测状态：未监测",
            "会话描述：候选 4 的大白话说明",
            "会话最后一轮结果：候选 4 已完成最后一轮工作\n"
            "会话最后活动时间：2026-08-24 10:00",
            "匹配说明：第 4 项包含用户描述的流程证据",
            "操作说明：\n- 继续会话：直接回复本消息并发送文字\n"
            "- 管理监测：回复“添加监测”或“移除监测”\n"
            "- 查看本次原文：回复“.原文”（本次查询限一次）\n"
            "- 归档该会话：回复“.归档”（本次查询限一次）",
        ]
    finally:
        state.close()


def test_session_search_unique_and_selected_match_share_exact_detail_layout(
    tmp_path: Path,
) -> None:
    match = _search_match(1)
    found = FakeSessionSearch([_search_result("found", [match])])
    ambiguous = FakeSessionSearch([_search_result("ambiguous", [match])])
    found_controller, found_state, _tools, found_sender = _controller(
        tmp_path / "found", session_search=found
    )
    selected_controller, selected_state, _tools, selected_sender = _controller(
        tmp_path / "selected", session_search=ambiguous
    )
    try:
        found_controller.handle(_message("found-1", ".搜索会话"))
        found_controller.handle(
            _message(
                "found-2",
                "会话名称：\n会话描述：线索\n会话最后活动时间：",
                reply_to="om_1",
            )
        )

        selected_controller.handle(_message("selected-1", ".搜索会话"))
        selected_controller.handle(
            _message(
                "selected-2",
                "会话名称：\n会话描述：线索\n会话最后活动时间：",
                reply_to="om_1",
            )
        )
        selected_controller.handle(
            _message("selected-3", "选择1", reply_to="om_2")
        )

        assert found_sender.messages[-1][1] == selected_sender.messages[-1][1]
    finally:
        found_state.close()
        selected_state.close()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("已有标题", "《已有标题》"),
        ("《已有标题》", "《已有标题》"),
        ('修复"D:\\合成目录\\工具.bat"', '《修复"D:\\合成目录\\工具.bat"》'),
        ("", f"《{SEARCH_EMPTY_TITLE}》"),
    ],
)
def test_search_title_bookmarks_are_display_only_and_never_duplicated(
    raw: str, expected: str
) -> None:
    assert _book_title(raw, 80) == expected


def test_search_title_long_value_compacts_inside_bookmarks() -> None:
    rendered = _book_title("很长的合成会话名称" * 20, 48)
    assert rendered.startswith("《很长的合成会话名称")
    assert rendered.endswith("…》")
    assert len(rendered[1:-1]) == 48


def test_search_display_refreshes_renamed_title_without_mutating_snapshot_or_schema(
    tmp_path: Path,
) -> None:
    controller, state, tools, sender = _controller(tmp_path)
    item = _search_match(1).to_dict()
    item.update({"thread_id": "thread-project", "title": "缓存中的旧标题"})
    result = _search_result("ambiguous", [SearchMatch(**item)]).to_dict()
    frozen = json.loads(json.dumps(result, ensure_ascii=False))
    try:
        controller._send_search_page(result, 1, "renamed-page")
        assert "1｜《项目会话》" in sender.messages[-1][1]
        controller._send_search_match(item, "renamed-detail")
        assert "会话名称：《项目会话》" in sender.messages[-1][1]
        assert result == frozen
        assert item["title"] == "缓存中的旧标题"
        assert tools.closed == 2
    finally:
        state.close()


def test_catalog_and_search_refresh_never_let_stale_desktop_title_override_sqlite(
    tmp_path: Path,
) -> None:
    controller, state, tools, sender = _controller(tmp_path)
    prompt = "请修复合成目录里的自动任务并完成验证。"
    controller.codex_store.records.append(
        ThreadRecord(
            "thread-sqlite-new",
            "修复合成自动任务",
            r"C:\Personal",
            310000,
            1,
            preview=prompt,
            raw={"title": "修复合成自动任务", "name": "", "preview": prompt},
            title_source="sqlite_title",
        )
    )
    tools.extra_threads.append(
        {
            "id": "thread-sqlite-new",
            "kind": "codex",
            "title": prompt[:25] + "…",
            "projectId": None,
        }
    )
    item = _search_match(1).to_dict()
    item.update({"thread_id": "thread-sqlite-new", "title": "缓存中的旧名称"})
    try:
        controller.handle(_message("catalog-title-1", ".查询个人会话"))
        assert "修复合成自动任务" in sender.messages[-1][1]
        assert prompt[:25] not in sender.messages[-1][1]

        controller._send_search_match(item, "search-title-refresh")
        assert "会话名称：《修复合成自动任务》" in sender.messages[-1][1]
        assert "当前名称为内容概括" not in sender.messages[-1][1]
    finally:
        state.close()


def test_search_display_uses_generated_title_for_prompt_metadata_and_keeps_explicit_path_name(
    tmp_path: Path,
) -> None:
    controller, state, tools, sender = _controller(tmp_path)
    prompt = '请检查"D:\\合成目录\\自动任务.bat"为什么无法继续执行，并修好后告诉我。'
    prompt_excerpt = prompt[:30] + "…"
    explicit_path_title = '修复"D:\\合成目录\\命名工具.bat"'
    controller.codex_store.records.extend(
        [
            ThreadRecord(
                "thread-prompt-derived",
                prompt_excerpt,
                r"D:\合成目录",
                300000,
                1,
                preview=prompt,
                raw={"title": prompt, "name": "", "preview": prompt},
            ),
            ThreadRecord(
                "thread-explicit-path",
                explicit_path_title,
                r"D:\合成目录",
                299000,
                1,
                preview=prompt,
                raw={
                    "title": prompt,
                    "name": explicit_path_title,
                    "preview": prompt,
                },
            ),
        ]
    )
    tools.extra_threads.extend(
        [
            {
                "id": "thread-prompt-derived",
                "kind": "codex",
                "title": prompt_excerpt,
            },
            {
                "id": "thread-explicit-path",
                "kind": "codex",
                "title": f"《{explicit_path_title}》",
            },
        ]
    )
    prompt_item = _search_match(1).to_dict()
    prompt_item.update(
        {
            "thread_id": "thread-prompt-derived",
            "title": "修复自动创建任务脚本",
            "title_origin": "recovered_summary",
        }
    )
    explicit_item = _search_match(2).to_dict()
    explicit_item.update(
        {"thread_id": "thread-explicit-path", "title": "缓存旧标题"}
    )
    result = _search_result(
        "ambiguous",
        [SearchMatch(**prompt_item), SearchMatch(**explicit_item)],
    ).to_dict()
    try:
        controller._send_search_page(result, 1, "source-page")
        page = sender.messages[-1][1]
        assert "1｜《修复自动创建任务脚本》" in page
        assert "Codex 当前没有可用的独立标题，当前名称为内容概括" in page
        assert "在 Codex 中重命名后会自动更新" in page
        assert f"2｜《{explicit_path_title}》" in page
        assert f"《《{explicit_path_title}》》" not in page

        controller._send_search_match(prompt_item, "source-detail")
        assert "会话名称：《修复自动创建任务脚本》" in sender.messages[-1][1]
        assert "Codex 当前没有可用的独立标题，当前名称为内容概括" in sender.messages[-1][1]
        assert "在 Codex 中重命名后会自动更新" in sender.messages[-1][1]
        controller._send_search_match(explicit_item, "explicit-detail")
        assert f"会话名称：《{explicit_path_title}》" in sender.messages[-1][1]
        assert "当前名称为内容概括" not in sender.messages[-1][1]
    finally:
        state.close()


def test_historic_recovered_title_is_immediately_replaced_after_manual_rename(
    tmp_path: Path,
) -> None:
    controller, state, _tools, sender = _controller(tmp_path)
    prompt = "请处理一段足够长的合成首轮要求，并验证全部结果后再告诉我。"
    record = ThreadRecord(
        "thread-historic-repair",
        prompt,
        r"D:\合成目录",
        300000,
        1,
        preview=prompt,
        raw={"title": prompt, "name": "", "preview": prompt},
        title_source="prompt_fallback",
    )
    controller.codex_store.records.append(record)
    item = _search_match(1).to_dict()
    item.update(
        {
            "thread_id": record.thread_id,
            "title": "恢复性内容概括",
            "title_origin": "recovered_summary",
        }
    )
    try:
        controller._send_search_match(item, "historic-before-rename")
        assert "会话名称：《恢复性内容概括》" in sender.messages[-1][1]
        assert "当前名称为内容概括" in sender.messages[-1][1]

        controller.codex_store.records[-1] = ThreadRecord(
            record.thread_id,
            "用户确认的真实会话名",
            record.cwd,
            record.updated_at_ms + 1,
            record.created_at_ms,
            preview=prompt,
            raw={
                "title": prompt,
                "name": "用户确认的真实会话名",
                "preview": prompt,
            },
            title_source="manual_name",
        )
        controller._send_search_match(item, "historic-after-rename")
        assert "会话名称：《用户确认的真实会话名》" in sender.messages[-1][1]
        assert "恢复性内容概括" not in sender.messages[-1][1]
        assert "当前名称为内容概括" not in sender.messages[-1][1]
    finally:
        state.close()


def test_old_search_context_never_trusts_unverifiable_prompt_title(
    tmp_path: Path,
) -> None:
    controller, state, _tools, sender = _controller(tmp_path)
    item = _search_match(1).to_dict()
    item.update(
        {
            "thread_id": "missing-old-thread",
            "title": "请把这一整段历史首轮要求继续当作标题展示给用户",
        }
    )
    result = _search_result("ambiguous", [SearchMatch(**item)]).to_dict()
    try:
        with pytest.raises(ManagementUserError, match="历史搜索结果来自旧版"):
            controller._send_search_page(
                result,
                1,
                "old-context",
                trust_result_titles=False,
            )
        assert not sender.messages
    finally:
        state.close()


def test_session_search_detail_keeps_links_commands_and_empty_fallback_readable(
    tmp_path: Path,
) -> None:
    controller, state, _tools, sender = _controller(tmp_path)
    item = _search_match(1).to_dict()
    item.update({
        "description": "参考 [本地说明](https://example.invalid/help) 后继续处理。",
        "last_result": "请引用本消息回复 `选择1`，不要改动命令文字。" + "结果" * 260,
        "reason": "",
    })
    try:
        controller._send_search_match(item, "synthetic-layout")
        detail = sender.messages[-1][1]
        assert "[本地说明](https://example.invalid/help)" in detail
        assert "`选择1`" in detail
        assert "会话最后一轮结果：" in detail
        assert "…" in detail
        assert "匹配说明：暂无补充说明。" in detail
        assert detail.count("\n\n") == 4
    finally:
        state.close()


def test_session_search_expands_only_after_exact_quoted_confirmation(tmp_path: Path) -> None:
    search = FakeSessionSearch(
        [
            _search_result("not_found"),
            _search_result("not_found", scope="recent_180d", next_scope="all"),
            _search_result("not_found", scope="all", next_scope=None),
        ]
    )
    controller, state, _tools, sender = _controller(tmp_path, session_search=search)
    try:
        controller.handle(_message("expand-1", ".搜索会话"))
        controller.handle(
            _message(
                "expand-2",
                "会话名称：\n会话描述：很久以前的任务\n会话最后活动时间：",
                reply_to="om_1",
            )
        )
        not_found_id, not_found, _key = sender.messages[-1]
        assert "没有找到" in not_found
        assert "当前检索范围：最近30天" in not_found
        assert "确认增加搜索范围" in not_found

        with pytest.raises(ManagementUserError, match="确认增加搜索范围"):
            controller.handle(_message("expand-wrong", "扩大一下", reply_to=not_found_id))
        assert len(search.requests) == 1

        # 仍引用原始结果消息时，精确确认才会真正扩大。
        controller.handle(_message("expand-3", "确认增加搜索范围", reply_to=not_found_id))
        next_id, next_text, _key = sender.messages[-1]
        assert search.requests[-1].scope == "recent_180d"
        assert "当前检索范围：最近180天" in next_text

        controller.handle(_message("expand-4", "确认增加搜索范围", reply_to=next_id))
        final_id, final_text, _key = sender.messages[-1]
        assert search.requests[-1].scope == "all"
        assert "已经检查全部用户会话" in final_text

        with pytest.raises(ManagementUserError, match="不能继续扩大范围"):
            controller.handle(_message("expand-5", "确认增加搜索范围", reply_to=final_id))
        assert len(search.requests) == 3
    finally:
        state.close()


def test_existing_overview_uses_search_summary_but_thread_updated_at(tmp_path: Path) -> None:
    search = FakeSessionSearch([])
    controller, state, _tools, sender = _controller(tmp_path, session_search=search)
    try:
        controller.handle(_message("overview-1", ".查询项目列表"))
        controller.handle(_message("overview-2", "展开A01", reply_to="om_1"))
        controller.handle(_message("overview-3", "选定a01", reply_to="om_2"))
        overview = sender.messages[-1][1]
        assert "最后一轮结果：搜索引擎生成的同款最后结果摘要" in overview
        assert "最近更新：1970-01-01 08:03" in overview
    finally:
        state.close()


def test_overview_separates_active_latest_from_old_completed_snapshot(
    tmp_path: Path,
) -> None:
    current = 1_777_777_778
    old = 1_700_000_000
    old_raw = "旧完成轮次完整原文"
    search = FakeOverviewSnapshotSearch(
        {
            "summary": "旧完成轮次摘要",
            "completed_at": old,
            "turn_id": "turn-old-completed",
            "content_hash": "old-content-hash",
            "raw_final": old_raw,
            "raw_sha256": hashlib.sha256(old_raw.encode()).hexdigest(),
        }
    )
    controller, state, _tools, sender = _controller(
        tmp_path, session_search=search
    )
    controller.codex_store.turns["thread-personal"] = TurnRecord(
        thread_id="thread-personal",
        turn_id="turn-active",
        status=ThreadStatus.IN_PROGRESS,
        started_at=current,
    )
    controller.codex_store.completed_turns["thread-personal"] = TurnRecord(
        thread_id="thread-personal",
        turn_id="turn-old-completed",
        status=ThreadStatus.COMPLETED,
        completed_at=old,
        final_message=old_raw,
    )
    try:
        controller._send_overview(
            {
                "id": "thread-personal",
                "title": "个人会话",
                "hostId": "local",
                "status": "idle",
                "updatedAt": current,
                "summary": "当前任务仍在处理。",
            },
            "个人会话",
            "synthetic-active",
        )
        overview_id, overview, _ = sender.messages[-1]
        assert "状态：active" in overview
        assert "最近更新：2026-05-03 11:09" in overview
        assert "最后一轮状态：active" in overview
        assert "最后一轮时间：2026-05-03 11:09" in overview
        assert "最后一轮结果：最近轮次暂无可展示的最终答复。" in overview
        assert "最近完成结果：旧完成轮次摘要" in overview
        assert old_raw not in overview
        record = state.management_context_record_for_message(overview_id)
        assert record is not None
        snapshot = record.payload["query_snapshot"]
        assert snapshot["turn_id"] == "turn-old-completed"
        assert snapshot["raw_final"] == old_raw
        assert snapshot["raw_sha256"] == hashlib.sha256(old_raw.encode()).hexdigest()
    finally:
        state.close()


@pytest.mark.parametrize(
    ("latest_status", "status_label"),
    ((ThreadStatus.FAILED, "failed"), (ThreadStatus.CANCELLED, "cancelled")),
)
def test_overview_latest_failed_or_cancelled_without_completed_result_stays_empty(
    tmp_path: Path, latest_status: ThreadStatus, status_label: str
) -> None:
    current = 1_777_777_778
    search = FakeSessionSearch(
        [], last_result="不应冒充最新轮次的摘要", last_result_at=1_700_000_000
    )
    controller, state, _tools, sender = _controller(
        tmp_path, session_search=search
    )
    controller.codex_store.turns["thread-personal"] = TurnRecord(
        thread_id="thread-personal",
        turn_id="turn-failed",
        status=latest_status,
        completed_at=current,
        final_message="",
    )
    try:
        controller._send_overview(
            {
                "id": "thread-personal",
                "title": "个人会话",
                "hostId": "local",
                "status": "idle",
                "updatedAt": current,
                "summary": "本轮失败。",
            },
            "个人会话",
            "synthetic-failed",
        )
        overview_id, overview, _ = sender.messages[-1]
        assert f"状态：{status_label}" in overview
        assert "最近更新：2026-05-03 11:09" in overview
        assert f"最后一轮状态：{status_label}" in overview
        assert "最后一轮结果：最近轮次暂无可展示的最终答复。" in overview
        assert "不应冒充最新轮次的摘要" not in overview
        assert "最近完成结果：" not in overview
        record = state.management_context_record_for_message(overview_id)
        assert record is not None
        assert record.payload["query_snapshot"] == {}
    finally:
        state.close()


def test_overview_completed_turn_remains_the_last_result(
    tmp_path: Path,
) -> None:
    current = 1_777_777_778
    search = FakeSessionSearch(
        [], last_result="当前完成轮次摘要", last_result_at=current
    )
    controller, state, _tools, sender = _controller(
        tmp_path, session_search=search
    )
    current_raw = "当前完成轮次完整原文"
    controller.codex_store.turns["thread-personal"] = TurnRecord(
        thread_id="thread-personal",
        turn_id="turn-current-completed",
        status=ThreadStatus.COMPLETED,
        completed_at=current,
        final_message=current_raw,
    )
    try:
        controller._send_overview(
            {
                "id": "thread-personal",
                "title": "个人会话",
                "hostId": "local",
                "status": "idle",
                "updatedAt": current,
                "summary": "本轮已完成。",
            },
            "个人会话",
            "synthetic-completed",
        )
        overview_id, overview, _ = sender.messages[-1]
        assert "状态：completed" in overview
        assert "最后一轮状态：completed" in overview
        assert "最后一轮结果：当前完成轮次摘要" in overview
        assert "最近完成结果：" not in overview
        assert current_raw not in overview
        record = state.management_context_record_for_message(overview_id)
        assert record is not None
        assert record.payload["query_snapshot"]["turn_id"] == "turn-current-completed"
        assert record.payload["query_snapshot"]["raw_final"] == current_raw
    finally:
        state.close()


def test_personal_form_preserves_multiline_prompt_after_structural_newline(
    tmp_path: Path,
) -> None:
    controller, state, tools, sender = _controller(tmp_path)
    try:
        controller.handle(_message("in-1", ".新建个人会话"))
        assert "会话名称：" not in sender.messages[-1][1]
        form = "首轮对话提示词：\n第一行\n  第二行  "
        controller.handle(_message("in-2", form, reply_to="om_1"))
        assert tools.created == [
            ("第一行\n  第二行  ", {"type": "projectless"}, "")
        ]
        assert "个人会话已创建" in sender.messages[-1][1]
    finally:
        state.close()


def test_overview_raw_is_frozen_chunked_redacted_and_archive_is_independent(
    tmp_path: Path,
) -> None:
    search = FakeSessionSearch([])
    controller, state, tools, sender = _controller(
        tmp_path, session_search=search
    )
    raw_final = "完整最终答复第一段。\n\n完整最终答复第二段。"
    controller.codex_store.turns["thread-personal"] = TurnRecord(
        thread_id="thread-personal",
        turn_id="turn-frozen-1",
        status=ThreadStatus.COMPLETED,
        final_message=raw_final,
        completed_at=1_777_777_777_000,
    )
    try:
        controller.handle(_message("raw-1", ".查询个人会话"))
        controller.handle(_message("raw-2", "选定p01", reply_to="om_1"))
        overview_id = sender.messages[-1][0]
        overview = sender.messages[-1][1]
        assert "最后一轮结果：搜索引擎生成的同款最后结果摘要" in overview
        assert raw_final not in overview
        record = state.management_context_record_for_message(overview_id)
        assert record is not None
        assert record.sender_id == "ou_owner"
        assert record.chat_id == "oc_private"
        assert record.payload["query_snapshot"]["turn_id"] == "turn-frozen-1"
        assert record.payload["query_snapshot"]["raw_final"] == raw_final

        raw_sends: list[tuple[str, str]] = []

        def split_raw(text: str, key: str) -> tuple[str, ...]:
            raw_sends.append((text, key))
            return ("om_raw_chunk_1", "om_raw_chunk_2")

        controller.send_text = split_raw
        controller.handle(_message("raw-3", ".原文", reply_to=overview_id))
        assert len(raw_sends) == 1
        assert raw_final in raw_sends[0][0]
        assert tools.sent_prompts == []
        raw_action = state.management_context_action(record.context_id, "raw")
        assert raw_action is not None and raw_action["succeeded_at"] is not None
        assert json.loads(raw_action["result_message_ids_json"]) == [
            "om_raw_chunk_1",
            "om_raw_chunk_2",
        ]
        for message_id in ("om_raw_chunk_1", "om_raw_chunk_2", overview_id):
            rebound = state.management_context_record_for_message(message_id)
            assert rebound is not None
            assert rebound.context_id == record.context_id
            assert rebound.payload["query_snapshot"]["raw_final"] == ""
            assert rebound.payload["query_snapshot"]["turn_id"] == "turn-frozen-1"

        controller.send_text = sender
        controller.handle(_message("raw-4", ".原文", reply_to=overview_id))
        assert "该次查询的“.原文”已使用" in sender.messages[-1][1]

        controller.handle(_message("raw-5", ".归档", reply_to="om_raw_chunk_2"))
        assert tools.archive_calls == [
            ("thread-personal", True, "source-thread", "local")
        ]
        archive_action = state.management_context_action(record.context_id, "archive")
        assert archive_action is not None and archive_action["succeeded_at"] is not None

        # 同一会话的新查询拥有新的 snapshot_key，仍默认展示总结，且动作次数独立。
        controller.handle(_message("raw-6", ".查询个人会话"))
        next_list_id = sender.messages[-1][0]
        controller.handle(_message("raw-7", "选定p01", reply_to=next_list_id))
        next_overview_id, next_overview, _ = sender.messages[-1]
        assert "最后一轮结果：搜索引擎生成的同款最后结果摘要" in next_overview
        assert raw_final not in next_overview
        next_record = state.management_context_record_for_message(next_overview_id)
        assert next_record is not None
        assert next_record.context_id != record.context_id
        assert next_record.payload["query_snapshot"]["raw_final"] == raw_final
    finally:
        state.close()


def test_user_error_reuses_query_context_without_copying_raw_payload(
    tmp_path: Path,
) -> None:
    search = FakeSessionSearch([])
    controller, state, _tools, sender = _controller(
        tmp_path, session_search=search
    )
    raw_final = "只应保存于原查询上下文的冻结原文"
    controller.codex_store.turns["thread-personal"] = TurnRecord(
        thread_id="thread-personal",
        turn_id="turn-error-context",
        status=ThreadStatus.COMPLETED,
        final_message=raw_final,
    )
    try:
        controller.handle(_message("error-1", ".查询个人会话"))
        controller.handle(_message("error-2", "选定p01", reply_to="om_1"))
        overview_id = sender.messages[-1][0]
        overview_record = state.management_context_record_for_message(overview_id)
        assert overview_record is not None
        before = state.stats()["management_contexts"]

        controller.send_user_error(
            _message("error-3", "格式错误", reply_to=overview_id),
            "示例错误",
        )
        error_reply_id = sender.messages[-1][0]
        assert state.stats()["management_contexts"] == before
        rebound = state.management_context_record_for_message(error_reply_id)
        assert rebound is not None
        assert rebound.context_id == overview_record.context_id
        assert rebound.payload["query_snapshot"]["raw_final"] == raw_final

        controller.handle(_message("error-4", ".原文", reply_to=error_reply_id))
        assert raw_final in sender.messages[-1][1]
        redacted = state.management_context_record_for_message(overview_id)
        assert redacted is not None
        assert redacted.payload["query_snapshot"]["raw_final"] == ""
    finally:
        state.close()


def test_raw_without_stable_final_does_not_consume_action(tmp_path: Path) -> None:
    search = FakeSessionSearch([])
    controller, state, _tools, sender = _controller(
        tmp_path, session_search=search
    )
    try:
        controller.handle(_message("empty-1", ".查询个人会话"))
        controller.handle(_message("empty-2", "选定p01", reply_to="om_1"))
        overview_id = sender.messages[-1][0]
        record = state.management_context_record_for_message(overview_id)
        assert record is not None
        controller.handle(_message("empty-3", ".原文", reply_to=overview_id))
        assert "没有消耗“.原文”机会" in sender.messages[-1][1]
        assert state.management_context_action(record.context_id, "raw") is None
    finally:
        state.close()


def test_ownerless_legacy_overview_rejects_actions_but_keeps_normal_continue(
    tmp_path: Path,
) -> None:
    controller, state, tools, sender = _controller(tmp_path)
    raw = "旧查询冻结原文"
    context_id = state.create_management_context(
        "thread_overview",
        {
            "thread": {
                "id": "thread-personal",
                "title": "个人会话",
                "hostId": "local",
                "archived": False,
            },
            "group": "个人会话",
            "query_snapshot": {
                "turn_id": "legacy-turn",
                "content_hash": "legacy-content",
                "raw_final": raw,
                "raw_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                "snapshot_key": hashlib.sha256(b"legacy-query").hexdigest(),
            },
        },
    )
    state.bind_management_messages(context_id, ("om_legacy",))
    try:
        controller.handle(_message("legacy-1", ".原文", reply_to="om_legacy"))
        assert "安全绑定升级前" in sender.messages[-1][1]
        assert state.management_context_action(context_id, "raw") is None
        controller.handle(_message("legacy-2", ".归档", reply_to="om_legacy"))
        assert "安全绑定升级前" in sender.messages[-1][1]
        assert state.management_context_action(context_id, "archive") is None
        assert tools.archive_calls == []

        controller.handle(_message("legacy-3", "添加监测", reply_to="om_legacy"))
        subscriptions = {
            item["thread_id"]: item for item in state.monitor_subscriptions()
        }
        assert subscriptions["thread-personal"]["origin"] == "manual"

        controller.handle(_message("legacy-4", "继续处理", reply_to="om_legacy"))
        assert tools.sent_prompts == [
            ("thread-personal", "继续处理", "source-thread", "local")
        ]
    finally:
        state.close()


def test_management_owner_mismatch_is_rejected_before_action(tmp_path: Path) -> None:
    controller, state, tools, sender = _controller(tmp_path)
    controller.codex_store.turns["thread-personal"] = TurnRecord(
        thread_id="thread-personal",
        turn_id="turn-owner",
        status=ThreadStatus.COMPLETED,
        final_message="所有者原文",
    )
    try:
        controller.handle(_message("owner-1", ".查询个人会话"))
        controller.handle(_message("owner-2", "选定p01", reply_to="om_1"))
        overview_id = sender.messages[-1][0]
        original = state.management_context_record_for_message(overview_id)
        assert original is not None
        attacker = _message(
            "owner-3",
            ".归档",
            reply_to=overview_id,
            sender_id="ou_other",
        )
        with pytest.raises(ManagementUserError, match="不属于当前发送者"):
            controller.handle(attacker)
        controller.send_user_error(attacker, "这条查询结果不属于当前发送者")
        error_reply_id = sender.messages[-1][0]
        rebound = state.management_context_record_for_message(error_reply_id)
        assert rebound is not None
        assert rebound.context_id == original.context_id
        assert rebound.sender_id == "ou_owner"
        with pytest.raises(ManagementUserError, match="不属于当前发送者"):
            controller.handle(
                _message(
                    "owner-4",
                    ".归档",
                    reply_to=error_reply_id,
                    sender_id="ou_other",
                )
            )
        assert tools.archive_calls == []
    finally:
        state.close()


def test_archive_rejected_can_retry_but_unknown_is_frozen(tmp_path: Path) -> None:
    controller, state, tools, sender = _controller(tmp_path)
    controller.codex_store.turns["thread-personal"] = TurnRecord(
        thread_id="thread-personal",
        turn_id="turn-archive",
        status=ThreadStatus.COMPLETED,
        final_message="归档测试原文",
    )
    try:
        controller.handle(_message("archive-1", ".查询个人会话"))
        controller.handle(_message("archive-2", "选定p01", reply_to="om_1"))
        first_overview = sender.messages[-1][0]
        tools.archive_error = DesktopAppToolsRejected("explicit reject")
        controller.handle(_message("archive-3", ".归档", reply_to=first_overview))
        assert "没有消耗，可稍后重试" in sender.messages[-1][1]
        tools.archive_error = None
        controller.handle(_message("archive-4", ".归档", reply_to=first_overview))
        assert "官方工具已确认接受" in sender.messages[-1][1]
        assert len(tools.archive_calls) == 2

        controller.handle(_message("archive-5", ".查询个人会话"))
        second_list = sender.messages[-1][0]
        controller.handle(_message("archive-6", "选定p01", reply_to=second_list))
        second_overview = sender.messages[-1][0]
        tools.archive_error = DesktopAppToolsResultUnknown("unknown")
        controller.handle(_message("archive-7", ".归档", reply_to=second_overview))
        assert "归档结果无法确认" in sender.messages[-1][1]
        calls_after_unknown = len(tools.archive_calls)
        controller.handle(_message("archive-8", ".归档", reply_to=second_overview))
        assert "结果无法确认" in sender.messages[-1][1]
        assert len(tools.archive_calls) == calls_after_unknown
    finally:
        state.close()


def test_archive_confirmed_success_is_not_lost_when_local_pipe_close_fails(
    tmp_path: Path,
) -> None:
    controller, state, tools, sender = _controller(tmp_path)
    controller.codex_store.turns["thread-personal"] = TurnRecord(
        thread_id="thread-personal",
        turn_id="turn-archive-close",
        status=ThreadStatus.COMPLETED,
        final_message="归档关闭测试原文",
    )
    try:
        controller.handle(_message("archive-close-1", ".查询个人会话"))
        controller.handle(
            _message("archive-close-2", "选定p01", reply_to="om_1")
        )
        overview_id = sender.messages[-1][0]
        record = state.management_context_record_for_message(overview_id)
        assert record is not None
        tools.close_error = OSError("local pipe close failed")
        controller.handle(
            _message("archive-close-3", ".归档", reply_to=overview_id)
        )
        action = state.management_context_action(record.context_id, "archive")
        assert action is not None and action["succeeded_at"] is not None
        assert action["uncertain_at"] is None
        assert "官方工具已确认接受" in sender.messages[-1][1]
        assert len(tools.archive_calls) == 1
    finally:
        state.close()


def test_corrupt_raw_snapshot_is_rejected_before_send_or_consume(
    tmp_path: Path,
) -> None:
    controller, state, tools, sender = _controller(tmp_path)
    raw = "不应发出的损坏快照原文"
    context_id = state.create_management_context(
        "thread_overview",
        {
            "thread": {
                "id": "thread-personal",
                "title": "个人会话",
                "hostId": "local",
                "archived": False,
            },
            "group": "个人会话",
            "query_snapshot": {
                "turn_id": "turn-corrupt",
                "content_hash": "not-a-content-hash",
                "raw_final": raw,
                "raw_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                "snapshot_key": "not-a-snapshot-key",
            },
        },
        sender_id="ou_owner",
        chat_id="oc_private",
        ttl_days=30,
    )
    state.bind_management_messages(context_id, ("om_corrupt_raw",))
    try:
        with pytest.raises(ManagementUserError, match="快照标识已损坏"):
            controller.handle(
                _message("corrupt-raw-1", ".原文", reply_to="om_corrupt_raw")
            )
        assert state.management_context_action(context_id, "raw") is None
        assert raw not in "\n".join(text for _, text, _ in sender.messages)
        assert tools.sent_prompts == []
    finally:
        state.close()


def test_raw_send_unknown_is_frozen_and_never_blindly_retried(
    tmp_path: Path,
) -> None:
    search = FakeSessionSearch([])
    controller, state, tools, sender = _controller(
        tmp_path, session_search=search
    )
    controller.codex_store.turns["thread-personal"] = TurnRecord(
        thread_id="thread-personal",
        turn_id="turn-raw-unknown",
        status=ThreadStatus.COMPLETED,
        final_message="结果未知边界原文",
    )
    attempts: list[str] = []
    try:
        controller.handle(_message("raw-unknown-1", ".查询个人会话"))
        controller.handle(
            _message("raw-unknown-2", "选定p01", reply_to="om_1")
        )
        overview_id = sender.messages[-1][0]
        record = state.management_context_record_for_message(overview_id)
        assert record is not None

        def unknown_send(_text: str, key: str) -> tuple[str, ...]:
            attempts.append(key)
            raise RuntimeError("send result unknown")

        controller.send_text = unknown_send
        with pytest.raises(RuntimeError, match="send result unknown"):
            controller.handle(
                _message("raw-unknown-3", ".原文", reply_to=overview_id)
            )
        action = state.management_context_action(record.context_id, "raw")
        assert action is not None and action["uncertain_at"] is not None
        assert len(attempts) == 1

        controller.send_text = sender
        controller.handle(
            _message("raw-unknown-4", ".原文", reply_to=overview_id)
        )
        assert "结果无法确认" in sender.messages[-1][1]
        assert len(attempts) == 1
        assert tools.sent_prompts == []
    finally:
        state.close()


def test_unique_search_match_freezes_raw_privately_and_public_schema_omits_it(
    tmp_path: Path,
) -> None:
    match = _search_match(1, score=0.9)
    assert "raw_final" not in json.dumps(match.to_dict(), ensure_ascii=False)
    search = FakeSessionSearch([_search_result("found", [match])])
    controller, state, tools, sender = _controller(
        tmp_path, session_search=search
    )
    try:
        controller.handle(_message("search-raw-1", ".搜索会话"))
        controller.handle(
            _message(
                "search-raw-2",
                "会话名称：候选\n会话描述：\n会话最后活动时间：",
                reply_to="om_1",
            )
        )
        overview_id = sender.messages[-1][0]
        controller.handle(_message("search-raw-3", ".原文", reply_to=overview_id))
        assert "搜索候选 1 的完整最终答复原文" in sender.messages[-1][1]
        assert tools.sent_prompts == []
    finally:
        state.close()


def test_ambiguous_search_selection_uses_the_selected_frozen_raw_snapshot(
    tmp_path: Path,
) -> None:
    first = _search_match(1, score=0.88)
    second = _search_match(2, score=0.84)
    search = FakeSessionSearch([_search_result("ambiguous", [first, second])])
    controller, state, tools, sender = _controller(
        tmp_path, session_search=search
    )
    try:
        controller.handle(_message("search-select-1", ".搜索会话"))
        form_id = sender.messages[-1][0]
        controller.handle(
            _message(
                "search-select-2",
                "会话名称：候选\n会话描述：\n会话最后活动时间：",
                reply_to=form_id,
            )
        )
        page_id = sender.messages[-1][0]
        controller.handle(
            _message("search-select-3", "选择2", reply_to=page_id)
        )
        selected_id = sender.messages[-1][0]
        controller.handle(
            _message("search-select-4", ".原文", reply_to=selected_id)
        )
        raw_reply = sender.messages[-1][1]
        assert "搜索候选 2 的完整最终答复原文" in raw_reply
        assert "搜索候选 1 的完整最终答复原文" not in raw_reply
        assert tools.sent_prompts == []
    finally:
        state.close()


def test_project_list_can_create_thread_by_immutable_project_label(tmp_path: Path) -> None:
    controller, state, tools, sender = _controller(tmp_path)
    try:
        controller.handle(_message("in-1", ".查询项目列表"))
        assert "新建项目会话" in sender.messages[-1][1]

        controller.handle(_message("in-2", "新建项目会话", reply_to="om_1"))
        form = sender.messages[-1][1]
        assert "请回复项目编号" in form
        assert "首轮对话提示词：" not in form
        assert "会话名称：" not in form

        controller.handle(
            _message(
                "in-3",
                "项目名称：A01\n运行方式：自动\n首轮对话提示词：\n逐字保留\n  第二行  ",
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


def test_monitor_settings_requires_owner_confirmation_and_is_exactly_once(
    tmp_path: Path,
) -> None:
    cards = CaptureCardSender()
    controller, state, _tools, sender = _controller(tmp_path, card_sender=cards)
    try:
        controller.handle(_message("settings-open", ".监测设置"))
        settings_id, settings_card, _key = cards.cards[-1]
        settings_context = state.management_context_record_for_message(settings_id)
        assert settings_context is not None
        assert settings_context.context_kind == "monitor_settings"
        assert "178" not in json.dumps(settings_card, ensure_ascii=False)

        with pytest.raises(ManagementUserError):
            controller.handle(
                _message(
                    "settings-intruder",
                    "关闭自动监测",
                    reply_to=settings_id,
                    sender_id="ou_intruder",
                    chat_id="oc_other",
                )
            )

        controller.handle(
            _message("settings-request", "关闭自动监测", reply_to=settings_id)
        )
        confirm_id, _confirm_card, _key = cards.cards[-1]
        confirm_context = state.management_context_record_for_message(confirm_id)
        assert confirm_context is not None
        assert confirm_context.payload["desired"] is False

        # 旧的状态卡不能直接冒充确认卡。
        with pytest.raises(ManagementUserError, match="确认卡状态"):
            controller.handle(
                _message(
                    "settings-old-card-confirm",
                    "确认关闭自动监测",
                    reply_to=settings_id,
                )
            )

        controller.handle(
            _message(
                "settings-confirm",
                "确认关闭自动监测",
                reply_to=confirm_id,
            )
        )
        assert state.auto_monitoring_settings()["auto_monitoring_enabled"] is False
        command = RemoteCommand("monitor_auto_set", "false")
        row = state.remote_control_action(
            confirm_context.context_id,
            command.kind,
            command.request_hash(),
        )
        assert row is not None and row["state"] == "succeeded"
        result = json.loads(row["result_json"])
        assert result["changed"] is True

        before_messages = len(sender.messages)
        controller.handle(
            _message(
                "settings-confirm-repeat",
                "确认关闭自动监测",
                reply_to=confirm_id,
            )
        )
        assert len(sender.messages) == before_messages + 1
        assert "已经成功执行" in sender.messages[-1][1]

        # 同值请求是明确的无变化结果，不建立新的写动作。
        controller.handle(_message("settings-noop", ".关闭自动监测"))
        assert "没有重复写入" in sender.messages[-1][1]
    finally:
        state.close()


def test_monitor_settings_post_submit_failure_is_frozen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cards = CaptureCardSender()
    controller, state, _tools, sender = _controller(tmp_path, card_sender=cards)
    original = StateStore.set_auto_monitoring_enabled
    try:
        controller.handle(_message("settings-open-2", ".监测设置"))
        settings_id = cards.cards[-1][0]
        controller.handle(
            _message("settings-request-2", "关闭自动监测", reply_to=settings_id)
        )
        confirm_id = cards.cards[-1][0]
        context = state.management_context_record_for_message(confirm_id)
        assert context is not None

        def write_then_fail(self, enabled: bool, *, now=None):
            original(self, enabled, now=now)
            raise OSError("synthetic post-submit failure")

        monkeypatch.setattr(StateStore, "set_auto_monitoring_enabled", write_then_fail)
        controller.handle(
            _message(
                "settings-confirm-2",
                "确认关闭自动监测",
                reply_to=confirm_id,
            )
        )
        command = RemoteCommand("monitor_auto_set", "false")
        row = state.remote_control_action(
            context.context_id, command.kind, command.request_hash()
        )
        assert row is not None and row["state"] == "uncertain"
        assert "冻结自动重试" in sender.messages[-1][1]
    finally:
        state.close()


def test_monitor_settings_pre_submit_failure_releases_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cards = CaptureCardSender()
    controller, state, _tools, _sender = _controller(tmp_path, card_sender=cards)
    try:
        controller.handle(_message("settings-open-pre", ".监测设置"))
        settings_id = cards.cards[-1][0]
        controller.handle(
            _message("settings-request-pre", "关闭自动监测", reply_to=settings_id)
        )
        confirm_id = cards.cards[-1][0]
        context = state.management_context_record_for_message(confirm_id)
        assert context is not None

        monkeypatch.setattr(
            StateStore,
            "mark_remote_control_action_submitted",
            lambda self, context_id, action_kind, request_hash: False,
        )
        with pytest.raises(ManagementUserError, match="写入前失败"):
            controller.handle(
                _message(
                    "settings-confirm-pre",
                    "确认关闭自动监测",
                    reply_to=confirm_id,
                )
            )

        assert state.auto_monitoring_settings()["auto_monitoring_enabled"] is True
        command = RemoteCommand("monitor_auto_set", "false")
        row = state.remote_control_action(
            context.context_id, command.kind, command.request_hash()
        )
        assert row is not None and row["state"] == "rejected"
    finally:
        state.close()


def test_skills_open_page_refresh_and_submit_always_use_fresh_runtime_list(
    tmp_path: Path,
) -> None:
    cards = CaptureCardSender()
    remote_session = FakeRemoteSession()
    remote = FakeRemoteControl(remote_session)
    controller, state, _tools, sender = _controller(
        tmp_path, card_sender=cards, remote_control=remote
    )
    try:
        context_id = state.create_management_context(
            "remote_control",
            {"thread": {"id": "thread-personal", "title": "个人会话"}, "group": "个人会话"},
            sender_id="ou_owner",
            chat_id="oc_private",
        )
        state.bind_management_messages(context_id, ("om_remote_skills",))

        controller.handle(
            _message("skills-open", "/skills", reply_to="om_remote_skills")
        )
        first_card_id, first_card, _key = cards.cards[-1]
        assert remote_session.calls[-1] == ("skills", True)
        assert "skill-one" in json.dumps(first_card, ensure_ascii=False)

        remote_session.skills_value = tuple(
            SkillSnapshot(f"skill-{index:02d}", rf"C:\skills\{index}\SKILL.md")
            for index in range(12)
        )
        controller.handle(
            _message("skills-page", "/skills page 2", reply_to=first_card_id)
        )
        page_card_id, page_card, _key = cards.cards[-1]
        assert remote_session.calls[-1] == ("skills", True)
        encoded_page = json.dumps(page_card, ensure_ascii=False)
        assert "skill-10" in encoded_page and "skill-00" not in encoded_page

        # 旧卡里选择的名字在提交前会重新取列表，路径变化后只使用新路径。
        remote_session.skills_value = (
            SkillSnapshot("skill-one", r"C:\skills\new\SKILL.md"),
        )
        controller.handle(
            _message(
                "skills-submit",
                "/skill skill-one 使用最新实现",
                reply_to=first_card_id,
            )
        )
        skill_calls = [call for call in remote_session.calls if call[0] == "start_skill"]
        assert skill_calls[-1][1][1] == r"C:\skills\new\SKILL.md"
        assert ("skills", True) in remote_session.calls

        # 已删除/禁用后的旧卡不能再执行；使用另一页上下文验证独立动作状态。
        remote_session.skills_value = ()
        with pytest.raises(ManagementUserError, match="未启用|已变化"):
            controller.handle(
                _message(
                    "skills-disabled-submit",
                    "/skill skill-10 不应执行",
                    reply_to=page_card_id,
                )
            )
        assert len([call for call in remote_session.calls if call[0] == "start_skill"]) == 1
        assert sender.messages[-1][1].startswith("执行结果：Skill 已启动")
    finally:
        state.close()


def test_reset_alert_feature_entries_are_read_only_for_alert_tables(
    tmp_path: Path,
) -> None:
    controller, state, _tools, sender = _controller(tmp_path)
    try:
        state.reserve_reset_alert_event(
            event_key="synthetic-event",
            level="A",
            evidence="合成证据",
            window_text="合成窗口",
            advice="合成建议",
            source_ids=("synthetic",),
            fingerprint="synthetic-fingerprint",
            expires_at=2_000_000_000,
            now=1_900_000_000,
        )

        def alert_snapshot():
            return {
                table: [tuple(row) for row in state._connection.execute(f"SELECT * FROM {table}")]
                for table in (
                    "reset_alert_state",
                    "reset_alert_sources",
                    "reset_alert_signals",
                    "reset_alert_events",
                    "reset_alert_deliveries",
                )
            }

        before = alert_snapshot()
        controller.handle(_message("reset-status", ".重置预警状态"))
        assert "本次仅读取现有状态" in sender.messages[-1][1]
        controller.handle(_message("reset-recent", ".最近预警"))
        assert "A 级" in sender.messages[-1][1]
        assert "合成证据" in sender.messages[-1][1]
        assert alert_snapshot() == before
    finally:
        state.close()


@pytest.mark.parametrize(
    ("command", "mode_text", "is_git", "expected_environment"),
    (
        ("/local", "本地", False, "local"),
        ("/project", "本地", False, "local"),
        ("/worktree", "工作树", True, "worktree"),
    ),
)
def test_project_local_and_worktree_commands_keep_distinct_environments(
    tmp_path: Path,
    command: str,
    mode_text: str,
    is_git: bool,
    expected_environment: str,
) -> None:
    controller, state, tools, sender = _controller(tmp_path)
    tools.is_git_repository = is_git
    try:
        controller.handle(_message(f"open-{expected_environment}", command))
        project_list_id = sender.messages[-1][0]
        controller.handle(
            _message(
                f"choose-{expected_environment}",
                "展开A01",
                reply_to=project_list_id,
            )
        )
        form_id = sender.messages[-1][0]
        form = sender.messages[-1][1]
        if command == "/project":
            assert "请回复“本地”或“工作树”" in form
            assert "首轮对话提示词：" not in form
        else:
            assert f"运行方式：{mode_text}" in form
        controller.handle(
            _message(
                f"create-{expected_environment}",
                f"项目名称：飞书机器人\n运行方式：{mode_text}\n"
                "首轮对话提示词：\n合成请求",
                reply_to=form_id,
            )
        )
        assert tools.created[-1][1]["environment"] == {
            "type": expected_environment
        }
    finally:
        state.close()


def test_creation_entry_forms_match_mobile_minimum_fields(tmp_path: Path) -> None:
    controller, state, _tools, sender = _controller(tmp_path)
    try:
        controller.handle(_message("in-1", ".新建项目"))
        project_form = sender.messages[-1][1]
        assert "项目名称：\n" in project_form
        assert "是否需要第一段会话：\n" in project_form
        assert "首轮对话提示词：" in project_form
        assert "会话名称：" not in project_form

        controller.handle(_message("in-2", ".查询项目列表"))
        controller.handle(_message("in-3", "展开A01", reply_to="om_2"))
        controller.handle(_message("in-4", "新建项目会话", reply_to="om_3"))
        expanded_form = sender.messages[-1][1]
        assert "已选项目：飞书机器人" in expanded_form
        assert "请回复这条消息，直接写下要做的事并发送。" in expanded_form
        assert "会话名称：" not in expanded_form
    finally:
        state.close()


def test_new_project_preserves_name_and_first_prompt_without_thread_title(tmp_path: Path) -> None:
    controller, state, tools, sender = _controller(tmp_path)
    registry = controller.project_registry
    try:
        controller.handle(_message("in-1", ".新建项目"))
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


@pytest.mark.parametrize("entry", ["指令使用", "远程控制", "Codex管理", "Codex 管理"])
def test_instruction_label_aliases_keep_quoted_overview_and_tools_routing(tmp_path: Path, entry: str) -> None:
    cards = CaptureCardSender()
    controller, state, _tools, _sender = _controller(
        tmp_path, card_sender=cards, remote_control=FakeRemoteControl()
    )
    try:
        controller.handle(_message("label-personal", ".查询个人会话"))
        list_id = cards.cards[-1][0]
        controller.handle(_message("label-select", "选定p01", reply_to=list_id))
        overview_id, overview = cards.cards[-1][:2]
        assert "指令使用" in str(overview)
        assert "远程控制" not in str(overview)
        controller.handle(_message("label-open", entry, reply_to=overview_id))
        manager_id, manager = cards.cards[-1][:2]
        assert "指令使用" in str(manager)
        assert "Codex 管理" not in str(manager)
        controller.handle(_message("label-reopen", entry, reply_to=manager_id))
        assert "指令使用" in str(cards.cards[-1][1])
    finally:
        state.close()
