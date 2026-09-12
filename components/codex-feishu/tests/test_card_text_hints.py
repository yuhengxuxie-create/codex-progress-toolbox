import pytest

from progress_wx import feature_center as f, session_query_card as q, remote_control as r
from progress_wx.card_text_hints import TEXT_INSTRUCTION_HEADING


def representative_cards(f=f, q=q, r=r):
    return {
        "feature_center": f.build_feature_center_card(),
        "query": q.build_session_query_card(),
        "projects": q.build_project_list_card([], page=1, pages=1, total=0),
        "threads": q.build_thread_list_card([], page=1, pages=1, total=0),
        "search": q.build_session_search_form_card(),
        "overview": q.build_thread_overview_card(title="合成任务", facts=(), sections=(), monitor_status="未监测"),
        "reply": q.build_thread_reply_form_card("合成任务"),
        "entry": r.build_remote_control_entry_card(),
        "goal": r.build_goal_set_form_card("合成任务"),
        "plan": r.build_plan_start_form_card("合成任务"),
        "skills": r.build_skills_card("合成任务", ()),
    }


def content(item):
    return item.get("content", item.get("text", {}).get("content", ""))


@pytest.mark.parametrize("name,card", representative_cards().items())
def test_intro_and_commands_are_separate_readable_blocks(name, card):
    elements = card.get("body", card)["elements"]
    intros = [i for i, item in enumerate(elements) if TEXT_INSTRUCTION_HEADING in content(item).replace("**", "")]
    assert len(intros) == 1, name
    heading = content(elements[intros[0]])
    body = content(elements[intros[0]+1])
    assert heading == "**也可以通过发送文字**\n**来进行功能的使用：**"
    assert "<font" not in heading
    assert body and "<font" not in body and "也可以" not in body
    assert "下方" not in body and " / " not in body
    assert all(len(line) <= 28 for line in body.splitlines())
    assert "height" not in elements[intros[0]] and "height" not in elements[intros[0]+1]
    if name == "feature_center":
        assert "查询会话" in body and "查看指令列表" in body
    if name == "search":
        assert "会话最后活动时间：" in body
