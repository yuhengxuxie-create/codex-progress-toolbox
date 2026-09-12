"""Shared, naturally wrapping text instructions for both Feishu card schemas."""
from typing import Any, Sequence


TEXT_INSTRUCTION_HEADING = "也可以通过发送文字\n来进行功能的使用："


def text_instruction_blocks(lines: Sequence[str], *, legacy: bool = False) -> list[dict[str, Any]]:
    """Keep the introduction separate from readable instructions, without fixed sizing."""
    heading = "\n".join(f"**{line}**" for line in TEXT_INSTRUCTION_HEADING.splitlines())
    contents = (heading, "\n".join(lines))
    if legacy:
        return [{"tag": "div", "text": {"tag": "lark_md", "content": content}} for content in contents]
    return [{"tag": "markdown", "content": content} for content in contents]
