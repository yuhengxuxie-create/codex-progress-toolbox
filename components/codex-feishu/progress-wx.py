#!/usr/bin/env python
"""无需安装包也能运行的本地命令入口。"""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from progress_wx.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())

