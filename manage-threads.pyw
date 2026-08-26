"""Windowed entry point for the monitored-thread manager."""

from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

def _show_fatal_startup_error(error: BaseException) -> None:
    message = (
        "会话管理器无法启动。\n\n"
        f"错误类型：{type(error).__name__}\n"
        "项目文件和配置不会因此被修改。"
    )
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(  # type: ignore[attr-defined]
                None,
                message,
                "Codex 监控会话管理器",
                0x10,
            )
            return
        except Exception:
            pass
    print(message, file=sys.stderr)


try:
    from progress_notify.thread_manager_gui import main  # noqa: E402

    raise SystemExit(main(PROJECT_ROOT / "config.local.json"))
except SystemExit:
    raise
except BaseException as exc:
    _show_fatal_startup_error(exc)
    raise SystemExit(1) from None
