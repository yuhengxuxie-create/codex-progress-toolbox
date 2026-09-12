from __future__ import annotations

from typing import Any

import pytest

from progress_wx.codex_rpc import CodexRPCError
from progress_wx.remote_control import (
    AppServerRemoteControl,
    CURRENT_THREAD_CLEAR_COMMAND,
    CURRENT_THREAD_CLEAR_LABEL,
    CURRENT_THREAD_SWITCH_COMMAND,
    CURRENT_THREAD_SWITCH_LABEL,
    CURRENT_THREAD_VIEW_COMMAND,
    CURRENT_THREAD_VIEW_LABEL,
    RemoteWriteUnavailable,
    SkillSnapshot,
    VALIDATED_REMOTE_WRITE_CAPABILITIES,
    build_goal_set_form_card,
    build_goal_clear_confirmation_card,
    build_plan_start_form_card,
    build_remote_control_card,
    build_remote_control_entry_card,
    build_skills_card,
    parse_remote_command,
    remote_control_action,
    remote_control_action_fingerprint,
    remote_control_card_action_fingerprints,
    remote_control_command,
)


# Adapter tests exercise the full protocol allowlist, including methods that
# production deliberately keeps disabled until they have independent evidence.
SYNTHETIC_WRITE_CAPABILITIES = frozenset(
    {
        "goal_set",
        "goal_clear",
        "plan_start",
        "skill_start",
        "settings_update",
        "memories_set",
        "compact_start",
        "fork_start",
        "review_start",
        "feedback_upload",
    }
)


def test_production_write_capabilities_are_a_strict_audited_subset() -> None:
    assert VALIDATED_REMOTE_WRITE_CAPABILITIES == {
        "goal_set",
        "goal_clear",
        "plan_start",
        "skill_start",
        "settings_update",
        "compact_start",
    }
    assert "memories_set" not in VALIDATED_REMOTE_WRITE_CAPABILITIES
    assert "feedback_upload" not in VALIDATED_REMOTE_WRITE_CAPABILITIES


def _card_tags(value: object, tag: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if value.get("tag") == tag:
            found.append(value)
        for child in value.values():
            found.extend(_card_tags(child, tag))
    elif isinstance(value, list):
        for child in value:
            found.extend(_card_tags(child, tag))
    return found


class FakeRPC:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.closed = 0
        self.thread_active = False
        self.skills_enabled = True

    def initialize(self):
        self.calls.append(("initialize", None))
        return {"protocol": 1}

    def resume_thread(self, thread_id: str):
        self.calls.append(("thread/resume", thread_id))
        return {"result": {"thread": {"id": thread_id}}}

    def read_thread(self, thread_id: str, *, include_turns: bool):
        self.calls.append(("thread/read", (thread_id, include_turns)))
        return {
            "result": {
                "thread": {
                    "id": thread_id,
                    "cwd": r"D:\Workspace",
                    "status": {"type": "active" if self.thread_active else "idle"},
                    "turns": (
                        [{"id": "turn-live", "status": "inProgress"}]
                        if self.thread_active
                        else []
                    ),
                },
                "model": "gpt-current",
                "reasoningEffort": "medium",
                "serviceTier": None,
            }
        }

    def request(self, method: str, params: dict, *, before_send=None):
        if before_send is not None:
            before_send()
        self.calls.append((method, params))
        if method == "thread/goal/get":
            return {
                "result": {
                    "goal": {
                        "objective": "保持测试通过",
                        "status": "active",
                        "tokenBudget": 9000,
                        "tokensUsed": 123,
                    }
                }
            }
        if method == "thread/goal/set":
            return {"result": {"goal": {"objective": params["objective"]}}}
        if method == "thread/goal/clear":
            return {"result": {}}
        if method == "collaborationMode/list":
            return {
                "result": {
                    "data": [
                        {"name": "Default", "mode": "default", "model": None,
                         "reasoning_effort": None},
                        {
                            "name": "Plan",
                            "mode": "plan",
                            "model": None,
                            "reasoning_effort": "medium",
                        },
                    ]
                }
            }
        if method == "skills/list":
            return {
                "result": {
                    "data": [
                        {
                            "cwd": r"D:\Workspace",
                            "skills": [
                                {
                                    "name": "skill-one",
                                    "path": r"C:\skills\one\SKILL.md",
                                    "description": "第一项",
                                    "enabled": self.skills_enabled,
                                    "interface": {"displayName": "技能一"},
                                },
                                {
                                    "name": "disabled",
                                    "path": r"C:\skills\off\SKILL.md",
                                    "enabled": False,
                                },
                            ],
                        }
                    ]
                }
            }
        raise AssertionError(method)

    def request_with_notification(
        self,
        method: str,
        params: dict,
        *,
        notification_method: str,
        notification_matches,
        before_send=None,
    ):
        if before_send is not None:
            before_send()
        self.calls.append((method, params))
        from types import SimpleNamespace

        notification_params = {
            "threadId": "thread-1",
            "threadSettings": {
                "model": params.get("model", "gpt-current"),
                "effort": params.get("effort", "medium"),
                "personality": params.get("personality", "pragmatic"),
                "serviceTier": params.get("serviceTier"),
            },
        }
        assert notification_method == "thread/settings/updated"
        assert notification_matches(notification_params)
        return {"result": {}}, SimpleNamespace(params=notification_params)

    def start_turn(
        self, thread_id: str, message=None, *, input=None, before_send=None, **options
    ):
        if before_send is not None:
            before_send()
        self.calls.append(
            (
                "turn/start",
                {
                    "thread_id": thread_id,
                    "message": message,
                    "input": input,
                    **options,
                },
            )
        )
        return {"result": {"turn": {"id": "turn-new", "status": "inProgress"}}}

    def close(self) -> None:
        self.closed += 1


def test_remote_text_parser_has_exact_scoped_commands() -> None:
    assert parse_remote_command("/goal").kind == "goal_get"
    assert parse_remote_command("/goal 保持测试通过").argument == "保持测试通过"
    assert parse_remote_command("/goal clear").kind == "goal_clear_request"
    assert parse_remote_command("/plan 先给出方案").kind == "plan_start"
    assert parse_remote_command("/skills").kind == "skills_list"
    assert parse_remote_command("/skills page 3").argument == "3"
    assert parse_remote_command("/skills page 0") is None
    skill = parse_remote_command("/skill skill-one 检查代码")
    assert skill is not None
    assert (skill.kind, skill.skill_name, skill.argument) == (
        "skill_start",
        "skill-one",
        "检查代码",
    )
    assert parse_remote_command("$skill-one 检查代码") == skill
    assert parse_remote_command("/goal\x00") is None
    assert parse_remote_command("/skill ../bad 请求") is None
    assert parse_remote_command("/plan\x00bad") is None


def test_remote_control_cards_split_the_two_forms_into_single_form_children() -> None:
    main = build_remote_control_card("合成会话")
    goal = build_goal_set_form_card("合成会话")
    plan = build_plan_start_form_card("合成会话")

    assert _card_tags(main, "form") == []
    assert [item["name"] for item in _card_tags(goal, "form")] == [
        "goal_set_form"
    ]
    assert [item["name"] for item in _card_tags(plan, "form")] == [
        "plan_start_form"
    ]
    for form_card in (goal, plan):
        assert "schema" not in form_card
        assert "body" not in form_card
        submit = _card_tags(form_card, "button")[0]
        assert submit["action_type"] == "form_submit"
        assert "form_action_type" not in submit
    assert len(remote_control_card_action_fingerprints(main)) == 2
    assert len(remote_control_card_action_fingerprints(goal)) == 1
    assert len(remote_control_card_action_fingerprints(plan)) == 1

    assert remote_control_command(remote_control_action("goal_set_open")) == (
        "设置 Goal"
    )
    assert remote_control_command(remote_control_action("plan_start_open")) == (
        "启动 Plan"
    )
    assert "Skills" in str(main)
    assert "/ 指令列表" in str(main)
    assert "设置 Goal" not in str(main)
    assert "启动 Plan" not in str(main)
    assert remote_control_command(remote_control_action("slash_catalog")) == "/commands"


def test_skills_with_options_use_classic_form_submit_protocol() -> None:
    card = build_skills_card(
        "合成会话",
        (
            SkillSnapshot(
                name="skill-one",
                display_name="Skill One",
                path="C:/skills/skill-one/SKILL.md",
            ),
        ),
    )
    assert "schema" not in card
    assert "body" not in card
    forms = _card_tags(card, "form")
    assert len(forms) == 1
    submit = next(
        item
        for item in _card_tags(card, "button")
        if item.get("name") == "skill_start_form"
    )
    assert submit["action_type"] == "form_submit"
    assert "form_action_type" not in submit


def test_current_thread_commands_keep_exact_text_aliases_and_short_card_labels() -> None:
    assert (
        CURRENT_THREAD_VIEW_COMMAND,
        CURRENT_THREAD_SWITCH_COMMAND,
        CURRENT_THREAD_CLEAR_COMMAND,
    ) == ("查看当前会话", "切换当前会话", "清除当前会话")
    assert (
        CURRENT_THREAD_VIEW_LABEL,
        CURRENT_THREAD_SWITCH_LABEL,
        CURRENT_THREAD_CLEAR_LABEL,
    ) == ("当前会话", "切换会话", "清除会话绑定")
    assert remote_control_command(remote_control_action("binding_view")) == (
        CURRENT_THREAD_VIEW_COMMAND
    )
    assert remote_control_command(remote_control_action("binding_switch")) == (
        CURRENT_THREAD_SWITCH_COMMAND
    )
    assert remote_control_command(remote_control_action("binding_clear")) == (
        CURRENT_THREAD_CLEAR_COMMAND
    )
    card = build_remote_control_entry_card()
    encoded = str(card)
    assert "查看当前会话" in encoded
    assert "切换当前会话" in encoded
    assert "清除当前会话" in encoded
    assert "当前会话" in encoded
    assert "切换会话" in encoded
    assert "清除会话绑定" in encoded


def test_old_remote_control_form_actions_remain_strictly_compatible() -> None:
    goal_action = remote_control_action("goal_set_form")
    plan_action = remote_control_action("plan_start_form")
    assert remote_control_command(goal_action) is None
    assert remote_control_command(plan_action) is None
    assert remote_control_command(
        goal_action, {"goal_objective": "旧卡仍可提交"}
    ) == "/goal 旧卡仍可提交"
    assert remote_control_command(
        plan_action, {"plan_task": "旧卡仍可规划"}
    ) == "/plan 旧卡仍可规划"


def test_remote_card_actions_are_strict_and_do_not_carry_target_identity() -> None:
    cards = [
        build_remote_control_entry_card(),
        build_remote_control_card("合成会话"),
        build_goal_clear_confirmation_card("合成会话"),
        build_skills_card(
            "合成会话",
            (SkillSnapshot("skill-one", r"C:\private\SKILL.md", display_name="技能一"),),
        ),
    ]
    for card in cards:
        encoded = str(card)
        assert "thread-" not in encoded
        assert "C:\\private" not in encoded
        assert remote_control_card_action_fingerprints(card)
    action = remote_control_action("goal_get")
    assert remote_control_command(action) == "/goal"
    assert remote_control_action_fingerprint(action)
    assert remote_control_command({**action, "thread_id": "forged"}) is None
    page_action = remote_control_action("skills_page:2")
    assert remote_control_command(page_action) == "/skills page 2"
    assert remote_control_action_fingerprint(page_action)


def test_skills_card_pages_and_refresh_actions_are_exact() -> None:
    skills = tuple(
        SkillSnapshot(f"skill-{index}", rf"C:\skills\{index}\SKILL.md")
        for index in range(12)
    )
    card = build_skills_card(
        "合成会话", skills, page=2, page_size=10, refreshed_at="2026-09-01 20:00:00"
    )
    encoded = str(card)
    assert "skill-10" in encoded and "skill-11" in encoded
    assert "skill-0" not in encoded
    assert "2026-09-01 20:00:00" in encoded
    fingerprints = remote_control_card_action_fingerprints(card)
    assert len(fingerprints) == 3  # 表单提交 + 上一页 + 刷新
    assert remote_control_command(remote_control_action("skills_page:1")) == "/skills page 1"
    assert remote_control_command(remote_control_action("skills_refresh")) == "/skills page 1"


def test_form_actions_validate_goal_plan_and_skill_values() -> None:
    assert remote_control_command(
        remote_control_action("goal_set_form"),
        {"goal_objective": "完成本地候选"},
    ) == "/goal 完成本地候选"
    assert remote_control_command(
        remote_control_action("plan_start_form"),
        {"plan_task": "规划下一步"},
    ) == "/plan 规划下一步"
    assert remote_control_command(
        remote_control_action("skill_start_form"),
        {"skill_name": "skill-one", "skill_request": "检查实现"},
    ) == "/skill skill-one 检查实现"
    assert remote_control_command(
        remote_control_action("skill_start_form"),
        {"skill_name": "../../secret", "skill_request": "读取"},
    ) is None


def test_app_server_adapter_reads_without_resuming_and_uses_official_calls() -> None:
    rpc = FakeRPC()
    remote = AppServerRemoteControl(
        lambda: rpc,  # type: ignore[arg-type]
        write_capabilities=SYNTHETIC_WRITE_CAPABILITIES,
    )
    with remote.prepare("thread-1") as session:
        assert session.goal().objective == "保持测试通过"
        session.set_goal("新的目标")
        session.clear_goal()
        assert session.is_active() is False
        mode = session.plan_mode()
        assert mode == {
            "mode": "plan",
            "settings": {
                "model": "gpt-current",
                "reasoning_effort": "medium",
                "developer_instructions": None,
            },
        }
        session.start_plan("只生成计划", mode)
        skills = session.skills(force_reload=True)
        assert [item.name for item in skills] == ["skill-one"]
        session.start_skill(skills[0], "检查实现")
    assert rpc.calls[:2] == [
        ("initialize", None),
        ("thread/read", ("thread-1", True)),
    ]
    assert all(method != "thread/resume" for method, _params in rpc.calls)
    goal_set = next(params for method, params in rpc.calls if method == "thread/goal/set")
    assert goal_set == {"threadId": "thread-1", "objective": "新的目标"}
    started = [params for method, params in rpc.calls if method == "turn/start"]
    assert started[0]["collaborationMode"] == mode
    assert started[1]["input"] == [
        {"type": "text", "text": "$skill-one 检查实现"},
        {
            "type": "skill",
            "name": "skill-one",
            "path": r"C:\skills\one\SKILL.md",
        },
    ]
    assert rpc.closed == 1


def test_plan_mode_converts_catalog_mask_and_uses_unique_official_default_model() -> None:
    class DefaultModelRPC(FakeRPC):
        def read_thread(self, thread_id: str, *, include_turns: bool):
            response = super().read_thread(thread_id, include_turns=include_turns)
            response["result"].pop("model", None)
            return response

        def request(self, method: str, params: dict, *, before_send=None):
            if method == "model/list":
                if before_send is not None:
                    before_send()
                self.calls.append((method, params))
                return {
                    "result": {
                        "data": [
                            {"model": "gpt-other", "isDefault": False},
                            {"model": "gpt-default", "isDefault": True},
                        ]
                    }
                }
            return super().request(method, params, before_send=before_send)

    rpc = DefaultModelRPC()
    with AppServerRemoteControl(lambda: rpc).prepare("thread-1") as session:  # type: ignore[arg-type]
        assert session.plan_mode() == {
            "mode": "plan",
            "settings": {
                "model": "gpt-default",
                "reasoning_effort": "medium",
                "developer_instructions": None,
            },
        }
    assert ("model/list", {"includeHidden": False, "limit": 100}) in rpc.calls


def test_remote_writes_are_disabled_by_default_until_real_capability_validation() -> None:
    rpc = FakeRPC()
    with AppServerRemoteControl(lambda: rpc).prepare("thread-1") as session:  # type: ignore[arg-type]
        with pytest.raises(RemoteWriteUnavailable, match="没有提交"):
            session.set_goal("不能默认写入")
    assert all(method != "thread/goal/set" for method, _params in rpc.calls)


def test_every_unverified_write_rejects_before_before_send_and_rpc_submission() -> None:
    rpc = FakeRPC()
    before_send_calls: list[str] = []
    remote = AppServerRemoteControl(lambda: rpc)  # type: ignore[arg-type]

    with remote.prepare("thread-1") as session:
        operations = (
            ("goal_set", lambda: session.set_goal(
                "不应提交", before_send=lambda: before_send_calls.append("goal_set")
            )),
            ("goal_clear", lambda: session.clear_goal(
                before_send=lambda: before_send_calls.append("goal_clear")
            )),
            ("plan_start", lambda: session.start_plan(
                "不应提交", {"id": "plan"},
                before_send=lambda: before_send_calls.append("plan_start"),
            )),
            ("skill_start", lambda: session.start_skill(
                SkillSnapshot("skill-one", r"C:\skills\one\SKILL.md"),
                "不应提交",
                before_send=lambda: before_send_calls.append("skill_start"),
            )),
            ("settings_update", lambda: session.update_setting(
                "model", "gpt-synthetic",
                before_send=lambda: before_send_calls.append("settings_update"),
            )),
            ("memories_set", lambda: session.set_memory_mode(
                "enabled", before_send=lambda: before_send_calls.append("memories_set")
            )),
            ("compact_start", lambda: session.compact(
                before_send=lambda: before_send_calls.append("compact_start")
            )),
            ("fork_start", lambda: session.fork(
                before_send=lambda: before_send_calls.append("fork_start")
            )),
            ("review_start", lambda: session.review_uncommitted(
                before_send=lambda: before_send_calls.append("review_start")
            )),
            ("feedback_upload", lambda: session.upload_feedback(
                "synthetic", "不应提交",
                before_send=lambda: before_send_calls.append("feedback_upload"),
            )),
        )
        for capability, operation in operations:
            with pytest.raises(RemoteWriteUnavailable, match="没有提交"):
                operation()
            assert capability not in before_send_calls

    assert before_send_calls == []
    assert rpc.calls == [
        ("initialize", None),
        ("thread/read", ("thread-1", True)),
    ]


def test_remote_prepare_never_competes_for_active_writer() -> None:
    class WriterOwnedRPC(FakeRPC):
        def resume_thread(self, _thread_id: str):
            raise AssertionError("远控只读准备不得调用 thread/resume")

    rpc = WriterOwnedRPC()
    rpc.thread_active = True
    with AppServerRemoteControl(lambda: rpc).prepare("thread-1") as session:  # type: ignore[arg-type]
        assert session.goal() is not None
        assert session.is_active() is True
    assert rpc.closed == 1


def test_remote_prepare_closes_child_when_thread_identity_is_unverified() -> None:
    class WrongThreadRPC(FakeRPC):
        def read_thread(self, thread_id: str, *, include_turns: bool):
            response = super().read_thread(thread_id, include_turns=include_turns)
            response["result"]["thread"]["id"] = "thread-other"
            return response

    rpc = WrongThreadRPC()
    remote = AppServerRemoteControl(lambda: rpc)  # type: ignore[arg-type]
    with pytest.raises(CodexRPCError, match="目标会话身份"):
        remote.prepare("thread-1")
    assert rpc.closed == 1
    # 锁也必须释放，下一次使用可继续建立连接。
    healthy = FakeRPC()
    remote._rpc_factory = lambda: healthy  # type: ignore[assignment]
    with remote.prepare("thread-1"):
        pass
    assert healthy.closed == 1


def test_adapter_rejects_ambiguous_plan_or_unverifiable_cwd() -> None:
    class BadRPC(FakeRPC):
        def request(self, method: str, params: dict):
            if method == "collaborationMode/list":
                return {
                    "result": {
                        "data": [
                            {"name": "Plan", "id": "a"},
                            {"name": "plan", "id": "b"},
                        ]
                    }
                }
            return super().request(method, params)

    rpc = BadRPC()
    with AppServerRemoteControl(lambda: rpc).prepare("thread-1") as session:  # type: ignore[arg-type]
        with pytest.raises(CodexRPCError, match="唯一"):
            session.plan_mode()


def test_setting_notification_requires_present_field_even_for_null() -> None:
    rpc = FakeRPC()
    with AppServerRemoteControl(
        lambda: rpc,  # type: ignore[arg-type]
        write_capabilities={"settings_update"},
    ).prepare("thread-1") as session:
        settings = session.update_setting("serviceTier", None)
        assert "serviceTier" in settings and settings["serviceTier"] is None

    class MissingFieldRPC(FakeRPC):
        def request_with_notification(
            self,
            method: str,
            params: dict,
            *,
            notification_method: str,
            notification_matches,
            before_send=None,
        ):
            if before_send is not None:
                before_send()
            missing = {
                "threadId": "thread-1",
                "threadSettings": {"model": "gpt-current"},
            }
            wrong_thread = {
                "threadId": "thread-other",
                "threadSettings": {"serviceTier": None},
            }
            wrong_value = {
                "threadId": "thread-1",
                "threadSettings": {"serviceTier": "not-default"},
            }
            assert notification_matches(missing) is False
            assert notification_matches(wrong_thread) is False
            assert notification_matches(wrong_value) is False
            raise CodexRPCError("no matching settings notification")

    with AppServerRemoteControl(
        lambda: MissingFieldRPC(),  # type: ignore[arg-type]
        write_capabilities={"settings_update"},
    ).prepare("thread-1") as session:
        with pytest.raises(CodexRPCError, match="matching"):
            session.update_setting("serviceTier", None)


def test_skills_are_force_reloaded_and_isolated_to_exact_thread_cwd() -> None:
    class MultiCwdRPC(FakeRPC):
        def __init__(self) -> None:
            super().__init__()
            self.target_path = r"C:\skills\one\SKILL.md"
            self.target_enabled = True

        def request(self, method: str, params: dict, *, before_send=None):
            if method != "skills/list":
                return super().request(method, params, before_send=before_send)
            assert params == {"cwds": [r"D:\Workspace"], "forceReload": True}
            self.calls.append((method, params))
            return {
                "result": {
                    "data": [
                        {
                            "cwd": r"D:\Other",
                            "skills": [
                                {
                                    "name": "foreign-skill",
                                    "path": r"C:\skills\foreign\SKILL.md",
                                    "enabled": True,
                                }
                            ],
                        },
                        {
                            "cwd": r"D:\Workspace",
                            "skills": [
                                {
                                    "name": "skill-one",
                                    "path": self.target_path,
                                    "enabled": self.target_enabled,
                                }
                            ],
                        },
                    ]
                }
            }

    rpc = MultiCwdRPC()
    with AppServerRemoteControl(lambda: rpc).prepare("thread-1") as session:  # type: ignore[arg-type]
        first = session.skills(force_reload=True)
        assert [(item.name, item.path) for item in first] == [
            ("skill-one", r"C:\skills\one\SKILL.md")
        ]
        rpc.target_path = r"C:\skills\moved\SKILL.md"
        second = session.skills(force_reload=True)
        assert [(item.name, item.path) for item in second] == [
            ("skill-one", r"C:\skills\moved\SKILL.md")
        ]
        rpc.target_enabled = False
        assert session.skills(force_reload=True) == ()
