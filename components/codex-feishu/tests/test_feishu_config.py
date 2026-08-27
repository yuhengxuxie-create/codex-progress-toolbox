"""飞书主后端的配置校验测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from progress_wx.config import ConfigError, load_config


def _write_config(tmp_path: Path, *, open_id: str = "ou_owner", secret: bool = True) -> Path:
    if secret:
        (tmp_path / "secret.dpapi").write_bytes(b"encrypted")
    path = tmp_path / "config.yaml"
    path.write_text(
        f"""
codex:
  home: "{tmp_path.as_posix()}"
monitor:
  ids: [thread-1]
messaging:
  backend: feishu
  require_quote: true
  secret_file: hmac.key
  pending_ttl_hours: 72
feishu:
  app_id: cli_test123
  app_secret_file: secret.dpapi
  target_open_id: {open_id}
  connect_timeout_seconds: 30
service:
  retry_delays: [1, 2, 4, 8, 16]
summary:
  mode: codex_final
""",
        encoding="utf-8",
    )
    return path


def test_feishu_config_does_not_require_wechat_accounts(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path))
    config.validate_ready()
    assert config.messaging.backend == "feishu"
    assert config.feishu.target_open_id == "ou_owner"
    assert config.wechat.tool_wechat_id == ""


def test_feishu_config_requires_exact_open_id_and_dpapi_file(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path, open_id="nickname"))
    with pytest.raises(ConfigError, match="open_id"):
        config.validate_ready()

    missing = load_config(_write_config(tmp_path, secret=False, open_id="ou_owner2"))
    # 删除上一轮可能创建的测试密钥，确保覆盖缺失分支。
    missing.feishu.app_secret_file.unlink(missing_ok=True)
    with pytest.raises(ConfigError, match="App Secret"):
        missing.validate_ready()

