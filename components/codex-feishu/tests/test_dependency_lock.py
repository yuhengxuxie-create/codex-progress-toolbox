from __future__ import annotations

import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCKED_REQUIREMENT = re.compile(
    r"^[A-Za-z0-9_.-]+==[^\s]+ --hash=sha256:[0-9a-f]{64}$"
)


def _effective_lines(path: Path) -> list[str]:
    """只返回会影响 pip 的行，避免注释干扰锁文件校验。"""

    return [
        line.strip()
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_core_runtime_is_exactly_pinned_and_hashed() -> None:
    """核心依赖不得在离线部署时临时解析版本。"""

    lines = _effective_lines(PROJECT_ROOT / "requirements-core.txt")
    assert all(LOCKED_REQUIREMENT.fullmatch(line) for line in lines)
    names = {line.split("==", 1)[0].lower() for line in lines}
    assert names == {"pyyaml", "websocket-client"}


def test_feishu_lock_includes_core_and_hashes_every_package() -> None:
    """飞书锁必须形成包含共享 Codex 依赖的完整闭包。"""

    lines = _effective_lines(PROJECT_ROOT / "requirements-feishu.txt")
    assert lines[0] == "-r requirements-core.txt"
    assert all(LOCKED_REQUIREMENT.fullmatch(line) for line in lines[1:])
    assert any(line.startswith("lark-channel-sdk==") for line in lines)


def test_installer_uses_locked_cache_without_editable_build() -> None:
    """安装脚本不得为了可编辑包隐式下载构建环境。"""

    script = (PROJECT_ROOT / "scripts" / "install.ps1").read_text(
        encoding="utf-8-sig"
    )
    assert "pip download" in script
    assert "--require-hashes" in script
    assert "--no-index" in script
    assert "--editable" not in script
