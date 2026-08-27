"""进度通知命令行入口；所有实际命令统一转交给 CLI 模块。"""

from .cli import main


if __name__ == "__main__":
    raise SystemExit(main())
