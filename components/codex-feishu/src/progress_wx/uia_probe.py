"""无需商业授权的微信 UI Automation 只读能力探针。

默认只按精确窗口标题查找工具小号；用户明确启用单窗口模式时，探针会先
确认系统中恰好只有一个已展开的 Weixin.exe 主窗口，再只读取该根窗口的
Name 做昵称精确核验。之后仅把控件的 ClassName 与 AutomationId 同固定
白名单比较。它不读取消息正文，不点击、不输入、不切换页面，也不能替代
真实引用回复验收。
"""

from __future__ import annotations

import ctypes
import importlib
import sys
from collections import deque
from ctypes import wintypes
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable


WEIXIN_WINDOW_CLASS = "Qt51514QWindowIcon"
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
KNOWN_CLASS_NAMES = frozenset(
    {
        "mmui::MainWindow",
        "mmui::MainTabBar",
        "mmui::ChatMasterView",
        "mmui::ChatMessagePage",
        "mmui::XSplitterView",
        "mmui::XValidatorTextEdit",
    }
)
KNOWN_AUTOMATION_IDS = frozenset(
    {
        "main_tabbar",
        "search_list",
        "chat_input_field",
    }
)


class UiaProbeError(RuntimeError):
    """只读探针无法安全完成。"""


@dataclass(frozen=True)
class UiaStructure:
    """不含昵称和消息正文的结构化 UIA 结果。"""

    control_count: int
    class_names: tuple[str, ...]
    automation_ids: tuple[str, ...]
    truncated: bool


def evaluate_structure(structure: UiaStructure) -> dict[str, bool]:
    """根据非内容字段判断当前微信是否至少公开基础控件树。"""

    if structure.truncated:
        # 遍历不完整时不能把局部命中误报为能力已证明。
        return {
            "main_window": False,
            "session_panel": False,
            "chat_page": False,
            "search_box": False,
            "chat_input": False,
        }
    classes = set(structure.class_names)
    automation_ids = set(structure.automation_ids)
    return {
        "main_window": "mmui::MainWindow" in classes,
        "session_panel": "mmui::ChatMasterView" in classes,
        "chat_page": "mmui::ChatMessagePage" in classes,
        "search_box": "mmui::XValidatorTextEdit" in classes,
        "chat_input": "chat_input_field" in automation_ids,
    }


def _exact_window_handles(title: str) -> list[int]:
    """用 Win32 精确标题查询，避免枚举并读取其他账号的窗口标题。"""

    user32 = ctypes.windll.user32
    user32.FindWindowExW.argtypes = (
        wintypes.HWND,
        wintypes.HWND,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
    )
    user32.FindWindowExW.restype = wintypes.HWND
    handles: list[int] = []
    after = wintypes.HWND(0)
    while True:
        hwnd = user32.FindWindowExW(
            wintypes.HWND(0),
            after,
            WEIXIN_WINDOW_CLASS,
            title,
        )
        if not hwnd:
            return handles
        handles.append(int(hwnd))
        after = hwnd


def _class_window_handles() -> list[int]:
    """只按固定窗口类枚举顶层句柄，明确不读取任何窗口标题。"""

    user32 = ctypes.windll.user32
    user32.FindWindowExW.argtypes = (
        wintypes.HWND,
        wintypes.HWND,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
    )
    user32.FindWindowExW.restype = wintypes.HWND
    handles: list[int] = []
    after = wintypes.HWND(0)
    while True:
        hwnd = user32.FindWindowExW(
            wintypes.HWND(0),
            after,
            WEIXIN_WINDOW_CLASS,
            None,
        )
        if not hwnd:
            return handles
        handles.append(int(hwnd))
        after = hwnd


def _is_expanded_window(hwnd: int) -> bool:
    """判断顶层窗口是否可见且未最小化；不改变窗口状态。"""

    user32 = ctypes.windll.user32
    user32.IsWindow.argtypes = (wintypes.HWND,)
    user32.IsWindow.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsIconic.argtypes = (wintypes.HWND,)
    user32.IsIconic.restype = wintypes.BOOL
    handle = wintypes.HWND(hwnd)
    return bool(
        user32.IsWindow(handle)
        and user32.IsWindowVisible(handle)
        and not user32.IsIconic(handle)
    )


def _select_single_expanded_weixin_handle(
    handles: Iterable[int],
    *,
    is_expanded: Callable[[int], bool],
    process_image_name: Callable[[int], str],
) -> int:
    """从非内容句柄信息中严格选择唯一已展开的微信窗口。"""

    candidates: list[int] = []
    for hwnd in handles:
        if not is_expanded(hwnd):
            continue
        if process_image_name(hwnd).casefold() == "weixin.exe":
            candidates.append(hwnd)
    if len(candidates) != 1:
        raise UiaProbeError(
            f"已展开的微信主窗口数量应为 1，当前为 {len(candidates)}；"
            "请只展开工具小号主窗口，并最小化其他微信主窗口"
        )
    return candidates[0]


def _process_image_name(hwnd: int) -> str:
    """取得窗口所属进程文件名，仅用于拒绝同类名的非微信窗口。"""

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.IsWindow.argtypes = (wintypes.HWND,)
    user32.IsWindow.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = (
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    )
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    if not user32.IsWindow(wintypes.HWND(hwnd)):
        raise UiaProbeError("工具小号窗口句柄已失效")
    pid = wintypes.DWORD()
    thread_id = user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    if not thread_id or not pid.value:
        raise UiaProbeError("无法只读核验窗口所属进程 ID")
    process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not process:
        raise UiaProbeError("无法只读核验窗口所属进程")
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
            raise UiaProbeError("无法只读核验窗口所属进程路径")
        return Path(buffer.value).name
    finally:
        kernel32.CloseHandle(process)


def _read_structure(root: Any, *, max_depth: int = 8, max_controls: int = 2500) -> UiaStructure:
    """遍历非内容 UIA 属性；明确禁止读取 Control.Name。"""

    queue: deque[tuple[Any, int]] = deque([(root, 0)])
    classes: set[str] = set()
    automation_ids: set[str] = set()
    count = 0
    truncated = False
    while queue:
        control, depth = queue.popleft()
        count += 1
        if count > max_controls:
            truncated = True
            break
        try:
            class_name = str(control.ClassName or "")
            automation_id = str(control.AutomationId or "")
        except Exception as exc:
            raise UiaProbeError(f"读取 UIA 结构失败：{type(exc).__name__}") from exc
        # 只保存预先白名单的结构标识；第三方 Provider 的未知字段绝不输出。
        if class_name in KNOWN_CLASS_NAMES:
            classes.add(class_name)
        if automation_id in KNOWN_AUTOMATION_IDS:
            automation_ids.add(automation_id)
        if depth >= max_depth:
            continue
        try:
            children: Iterable[Any] = control.GetChildren()
        except Exception as exc:
            raise UiaProbeError(f"枚举 UIA 子控件失败：{type(exc).__name__}") from exc
        queue.extend((child, depth + 1) for child in children)
    return UiaStructure(
        control_count=count,
        class_names=tuple(sorted(classes)),
        automation_ids=tuple(sorted(automation_ids)),
        truncated=truncated,
    )


def _verify_root_nickname(root: Any, expected_nickname: str) -> None:
    """仅在唯一候选已确定后读取根 Name；错误中绝不泄露观察值。"""

    try:
        observed = str(root.Name or "")
    except Exception as exc:
        raise UiaProbeError(f"无法读取唯一候选根窗口身份：{type(exc).__name__}") from exc
    if observed != expected_nickname:
        raise UiaProbeError(
            "唯一候选根窗口未通过配置昵称的精确身份核验；"
            "观察到的昵称未保存、未输出，也没有读取聊天内容"
        )


def probe_tool_window(
    account_nickname: str,
    *,
    single_visible_window: bool = False,
    diagnostic_unverified_identity: bool = False,
) -> dict[str, object]:
    """只读探测工具小号；单窗口模式必须由调用方明确启用。"""

    if sys.platform != "win32":
        raise UiaProbeError("该探针只支持 Windows")
    nickname = str(account_nickname or "").strip()
    if not nickname:
        raise UiaProbeError("工具小号昵称不能为空")
    if diagnostic_unverified_identity and not single_visible_window:
        raise UiaProbeError("身份未验证诊断只能与显式单窗口模式一起使用")
    if single_visible_window:
        hwnd = _select_single_expanded_weixin_handle(
            _class_window_handles(),
            is_expanded=_is_expanded_window,
            process_image_name=_process_image_name,
        )
        selection_mode = "single_expanded_window"
    else:
        handles = _exact_window_handles(nickname)
        if len(handles) != 1:
            raise UiaProbeError(
                f"精确昵称对应的微信窗口数量应为 1，当前为 {len(handles)}；"
                "请只打开工具小号窗口并确认昵称，或显式启用单窗口模式"
            )
        hwnd = handles[0]
        if _process_image_name(hwnd).casefold() != "weixin.exe":
            raise UiaProbeError("精确标题匹配到的窗口不属于 Weixin.exe")
        selection_mode = "exact_title"
    user32 = ctypes.windll.user32
    user32.IsWindow.argtypes = (wintypes.HWND,)
    user32.IsWindow.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    user32.IsWindowVisible.restype = wintypes.BOOL
    if not user32.IsWindow(wintypes.HWND(hwnd)):
        raise UiaProbeError("工具小号窗口句柄已失效")
    if not user32.IsWindowVisible(wintypes.HWND(hwnd)):
        raise UiaProbeError("工具小号窗口当前不可见；请恢复该窗口后重试")
    try:
        auto = importlib.import_module("uiautomation")
    except ImportError as exc:
        raise UiaProbeError("未安装开源依赖 uiautomation") from exc
    try:
        root = auto.ControlFromHandle(hwnd)
    except Exception as exc:
        raise UiaProbeError(f"无法连接 UIA Provider：{type(exc).__name__}") from exc
    if root is None:
        raise UiaProbeError("微信窗口未公开 UIA Provider")
    if single_visible_window:
        rechecked_hwnd = _select_single_expanded_weixin_handle(
            _class_window_handles(),
            is_expanded=_is_expanded_window,
            process_image_name=_process_image_name,
        )
        if rechecked_hwnd != hwnd:
            raise UiaProbeError("唯一微信窗口在核验期间发生变化，已安全停止")
        if diagnostic_unverified_identity:
            # 用户确认单账号时只看非内容结构；不把该确认冒充生产身份凭据。
            account_nickname_verified = False
            identity_basis = "user_confirmed_single_account_diagnostic"
        else:
            # 只有句柄、展开状态、进程名和唯一性均通过后才读取根 Name。
            _verify_root_nickname(root, nickname)
            account_nickname_verified = True
            identity_basis = "root_name_exact"
    else:
        account_nickname_verified = True
        identity_basis = "exact_window_title"
    structure = _read_structure(root)
    capabilities = evaluate_structure(structure)
    return {
        "window_selection": selection_mode,
        "selected_window_count": 1,
        "window_visible": True,
        "process_verified": True,
        "account_nickname_verified": account_nickname_verified,
        "identity_basis": identity_basis,
        "structure": asdict(structure),
        "capabilities": capabilities,
        # 结构探针不读取消息，因此永远不能单独宣称 Quote 能力已通过。
        "quote_reply_verified": False,
        "production_ready": False,
    }


__all__ = [
    "UiaProbeError",
    "UiaStructure",
    "evaluate_structure",
    "probe_tool_window",
]
