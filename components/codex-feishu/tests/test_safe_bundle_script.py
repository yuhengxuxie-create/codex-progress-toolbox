from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_safe_bundle_is_allowlist_based_and_excludes_local_state() -> None:
    """分享脚本必须先白名单复制，不能把项目整树压缩后再猜测删秘密。"""

    script = (PROJECT_ROOT / "scripts" / "export-safe-bundle.ps1").read_text(
        encoding="utf-8-sig"
    )
    assert "$TopLevelNames" in script
    assert "foreach ($DirectoryName in @('docs', 'scripts', 'src', 'tests'))" in script
    assert "Copy-Item -LiteralPath $ProjectFull" not in script
    for private_name in ("config.yaml", ".secrets", ".state", "logs", "*.dpapi", "*.key"):
        assert private_name in script
    assert "ReparsePoint" in script
    assert "Compress-Archive" in script


def test_safe_bundle_visible_entry_keeps_result_window_open() -> None:
    command = (PROJECT_ROOT / "一键生成安全分享包.cmd").read_text(
        encoding="utf-8-sig"
    )
    assert "export-safe-bundle.ps1" in command
    assert "pause" in command.lower()
