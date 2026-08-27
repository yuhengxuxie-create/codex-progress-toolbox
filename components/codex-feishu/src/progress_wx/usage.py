"""飞书内置使用说明；完整版由同仓库版本化文档维护。"""

from __future__ import annotations

from pathlib import Path


USAGE_VERSION = "1.5.0"
USAGE_IMAGE_FOOTER = (
    "以上为使用说明，如果想要文字版使用说明，请发送“文字版使用说明”哦"
)
_GUIDE_PATH = Path(__file__).resolve().parents[2] / "docs" / "FEISHU_USAGE.md"
_GUIDE_IMAGE_DIR = _GUIDE_PATH.parent / "assets" / "feishu-usage-classroom"
_GUIDE_IMAGE_NAMES = (
    "01-important-reminder.png",
    "02-view-conversations.png",
    "03-create-conversations.png",
    "04-continue-conversations.png",
    "05-send-images.png",
    "06-manage-monitoring.png",
)


def feishu_usage_text() -> str:
    """读取版本化说明并转换为适合飞书富文本的纯文本。"""

    try:
        markdown = _GUIDE_PATH.read_text(encoding="utf-8")
    except OSError:
        return (
            f"版本：{USAGE_VERSION}\n"
            "执行结果：本机完整使用说明暂不可读。\n"
            "操作说明：请在本机查看 docs/FEISHU_USAGE.md。"
        )
    lines: list[str] = []
    for raw in markdown.splitlines():
        line = raw.strip()
        if line.startswith("# "):
            continue
        if not line:
            if lines and lines[-1]:
                lines.append("")
            continue
        if line.startswith("- `"):
            line = "- " + line[2:].replace("`", "")
        lines.append(line.rstrip())
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def feishu_usage_images() -> tuple[tuple[str, bytes], ...]:
    """读取随使用说明发送的版本化课堂图片，顺序即飞书展示顺序。"""

    return tuple(
        (name, (_GUIDE_IMAGE_DIR / name).read_bytes())
        for name in _GUIDE_IMAGE_NAMES
    )


__all__ = [
    "USAGE_IMAGE_FOOTER",
    "USAGE_VERSION",
    "feishu_usage_images",
    "feishu_usage_text",
]
