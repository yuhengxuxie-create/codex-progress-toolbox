"""免费微信 UIA 探针的纯逻辑测试；不访问桌面微信。"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from src.progress_wx.uia_probe import (
    UiaProbeError,
    UiaStructure,
    _read_structure,
    _select_single_expanded_weixin_handle,
    _verify_root_nickname,
    evaluate_structure,
)


@dataclass
class FakeControl:
    """只提供非内容属性；若实现误读 Name，测试对象不会提供该字段。"""

    ClassName: str = ""
    AutomationId: str = ""
    children: list["FakeControl"] = field(default_factory=list)

    def GetChildren(self):
        return self.children


def test_structure_probe_uses_only_non_content_properties() -> None:
    root = FakeControl(
        ClassName="mmui::MainWindow",
        children=[
            FakeControl(ClassName="mmui::ChatMasterView"),
            FakeControl(ClassName="mmui::ChatMessagePage"),
            FakeControl(ClassName="mmui::XValidatorTextEdit"),
            FakeControl(AutomationId="chat_input_field"),
            FakeControl(ClassName="不会输出的未知类", AutomationId="不会输出的未知标识"),
        ],
    )

    structure = _read_structure(root)

    assert structure.control_count == 6
    assert "不会输出的未知类" not in structure.class_names
    assert "不会输出的未知标识" not in structure.automation_ids
    assert evaluate_structure(structure) == {
        "main_window": True,
        "session_panel": True,
        "chat_page": True,
        "search_box": True,
        "chat_input": True,
    }


def test_incomplete_tree_is_never_treated_as_ready() -> None:
    structure = UiaStructure(
        control_count=1,
        class_names=("Qt51514QWindowIcon",),
        automation_ids=(),
        truncated=False,
    )
    assert not any(evaluate_structure(structure).values())


def test_truncated_tree_never_reports_partial_capabilities() -> None:
    structure = UiaStructure(
        control_count=2501,
        class_names=("mmui::MainWindow", "mmui::ChatMasterView"),
        automation_ids=("chat_input_field",),
        truncated=True,
    )
    assert not any(evaluate_structure(structure).values())


def test_single_window_selector_uses_only_expanded_weixin_process() -> None:
    """选择阶段只使用句柄状态和进程名，不接触任何 UIA Name。"""

    process_names = {10: "Other.exe", 20: "Weixin.exe", 30: "Weixin.exe"}
    selected = _select_single_expanded_weixin_handle(
        [10, 20, 30],
        is_expanded=lambda hwnd: hwnd != 30,
        process_image_name=process_names.__getitem__,
    )
    assert selected == 20


@pytest.mark.parametrize(
    "expanded, expected_count",
    [({10: False, 20: False}, 0), ({10: True, 20: True}, 2)],
)
def test_single_window_selector_fails_closed_on_non_unique_candidate(
    expanded: dict[int, bool],
    expected_count: int,
) -> None:
    with pytest.raises(UiaProbeError, match=rf"当前为 {expected_count}"):
        _select_single_expanded_weixin_handle(
            expanded,
            is_expanded=expanded.__getitem__,
            process_image_name=lambda _hwnd: "Weixin.exe",
        )


@pytest.mark.parametrize(
    "observed",
    ["工具小号 ", "工具小號", "不应出现在错误中的其他账号"],
)
def test_root_nickname_exact_match_and_redacted_mismatch(observed: str) -> None:
    class Root:
        Name = "工具小号"

    _verify_root_nickname(Root(), "工具小号")
    Root.Name = observed
    with pytest.raises(UiaProbeError) as caught:
        _verify_root_nickname(Root(), "工具小号")
    assert observed not in str(caught.value)
