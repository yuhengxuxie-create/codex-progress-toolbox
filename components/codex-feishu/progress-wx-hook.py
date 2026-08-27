#!/usr/bin/env python
"""Codex ``notify`` 的极简入口；由安装脚本写入用户 config.toml。"""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from progress_wx.hook_dispatch import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())

