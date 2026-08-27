from __future__ import annotations

from argparse import Namespace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from progress_wx import cli
from progress_wx.config import ConfigError, load_config
from progress_wx.retry import RetryExhausted
from progress_wx.state import StateStore


def _gateway_config(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        path=tmp_path / "config.yaml",
        codex=SimpleNamespace(
            gateway_pid_file=tmp_path / "gateway.pid",
            shared_desktop_state_file=tmp_path / "shared.json",
            shared_websocket_url="ws://127.0.0.1:6230",
        ),
    )


def test_gateway_start_requires_caller_owned_launch_token() -> None:
    with pytest.raises(SystemExit):
        cli._parser().parse_args(["gateway-start"])


def test_gateway_start_existing_instance_is_never_owned(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    config = _gateway_config(tmp_path)
    monkeypatch.setattr(cli, "_config", lambda _args, ready=False: config)
    monkeypatch.setattr(cli, "verified_gateway_running", lambda _path: True)
    monkeypatch.setattr(cli, "gateway_healthy", lambda _url: True)
    monkeypatch.setattr(cli, "authorize_gateway_launch", lambda *_args: None)
    monkeypatch.setattr(
        cli, "read_pid_file", lambda _path: {"pid": 42, "creation_time": 420}
    )
    monkeypatch.setattr(
        cli.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("已有 gateway 时不得创建进程")
        ),
    )

    assert cli._gateway_start(Namespace()) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {
        "running": True,
        "healthy": True,
        "started_by_request": False,
        "pid": 42,
        "creation_time": 420,
    }


@pytest.mark.parametrize(
    "published_token, expected_owned", [("a" * 64, True), ("b" * 64, False)]
)
def test_gateway_start_ownership_requires_exact_launch_token(
    monkeypatch,
    tmp_path: Path,
    capsys,
    published_token: str,
    expected_owned: bool,
) -> None:
    config = _gateway_config(tmp_path)
    running_checks = iter((False, True))
    launched: list[list[str]] = []
    monkeypatch.setattr(cli, "_config", lambda _args, ready=False: config)
    monkeypatch.setattr(
        cli, "verified_gateway_running", lambda _path: next(running_checks)
    )
    monkeypatch.setattr(cli, "gateway_healthy", lambda _url: True)
    monkeypatch.setattr(cli, "authorize_gateway_launch", lambda *_args: None)
    launch_token = "a" * 64
    monkeypatch.setattr(
        cli,
        "read_pid_file",
        lambda _path: {
            "pid": 43,
            "creation_time": 430,
            "launch_token_sha256": hashlib.sha256(
                published_token.encode("utf-8")
            ).hexdigest(),
        },
    )
    monkeypatch.setattr(
        cli.subprocess,
        "Popen",
        lambda command, **_kwargs: launched.append(list(command))
        or SimpleNamespace(poll=lambda: None),
    )

    assert cli._gateway_start(Namespace(launch_token=launch_token)) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["started_by_request"] is expected_owned
    assert launched and launched[0][-2:] == ["--launch-token", "a" * 64]


def test_gateway_start_failure_recovers_exact_launch_authorization(
    monkeypatch, tmp_path: Path
) -> None:
    config = _gateway_config(tmp_path)
    launch_token = "c" * 64
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(cli, "_config", lambda _args, ready=False: config)
    monkeypatch.setattr(cli, "verified_gateway_running", lambda _path: False)
    monkeypatch.setattr(
        cli,
        "authorize_gateway_launch",
        lambda _path, token: calls.append(("authorize", token)),
    )
    monkeypatch.setattr(
        cli.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("spawn failed")),
    )
    monkeypatch.setattr(
        cli,
        "recover_owned_gateway_launch",
        lambda **kwargs: calls.append(("recover", kwargs["launch_token"]))
        or {"resolved": True},
    )

    with pytest.raises(OSError, match="spawn failed"):
        cli._gateway_start(Namespace(launch_token=launch_token))
    assert calls == [("authorize", launch_token), ("recover", launch_token)]


def test_gateway_start_reports_early_child_failure_without_waiting_for_timeout(
    monkeypatch, tmp_path: Path
) -> None:
    config = _gateway_config(tmp_path)
    launch_token = "f" * 64
    calls: list[str] = []
    monkeypatch.setattr(cli, "_config", lambda _args, ready=False: config)
    monkeypatch.setattr(cli, "verified_gateway_running", lambda _path: False)
    monkeypatch.setattr(cli, "authorize_gateway_launch", lambda *_args: None)

    def fake_popen(_command, **kwargs):
        kwargs["stdout"].write("错误：拒绝访问\n".encode("utf-8"))
        kwargs["stdout"].flush()
        return SimpleNamespace(poll=lambda: 2)

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        cli,
        "recover_owned_gateway_launch",
        lambda **kwargs: calls.append(kwargs["launch_token"]) or {"resolved": True},
    )

    with pytest.raises(
        cli.CodexGatewayError,
        match=r"后台进程提前退出，code=2；错误：拒绝访问",
    ):
        cli._gateway_start(Namespace(launch_token=launch_token))
    assert calls == [launch_token]


def test_gateway_startup_detail_redacts_launch_token(tmp_path: Path) -> None:
    token = "a" * 64
    path = tmp_path / "gateway.log"
    path.write_text(f"错误：token={token}\n", encoding="utf-8")

    detail = cli._gateway_startup_detail(path)

    assert token not in detail
    assert "<redacted>" in detail


def test_gateway_recovery_requires_exact_bound_context(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    config = _gateway_config(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(cli, "_config", lambda _args, ready=False: config)
    monkeypatch.setattr(
        cli,
        "recover_owned_gateway_launch",
        lambda **kwargs: calls.append(kwargs["launch_token"])
        or {"resolved": True, "outcome": "no-owned-launch"},
    )
    args = Namespace(
        expected_launch_token="d" * 64,
        expected_pid_file=str(config.codex.gateway_pid_file.resolve()),
        expected_state_file=str(config.codex.shared_desktop_state_file.resolve()),
        expected_websocket_url="ws://127.0.0.1:6230/",
    )

    assert cli._gateway_recover_owned(args) == 0
    assert calls == ["d" * 64]
    assert json.loads(capsys.readouterr().out)["resolved"] is True


def test_gateway_recovery_rejects_changed_state_path(
    monkeypatch, tmp_path: Path
) -> None:
    config = _gateway_config(tmp_path)
    monkeypatch.setattr(cli, "_config", lambda _args, ready=False: config)
    monkeypatch.setattr(
        cli,
        "recover_owned_gateway_launch",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("上下文不一致时不得进入恢复原语")
        ),
    )
    args = Namespace(
        expected_launch_token="e" * 64,
        expected_pid_file=str((tmp_path / "old-gateway.pid").resolve()),
        expected_state_file=str(config.codex.shared_desktop_state_file.resolve()),
        expected_websocket_url="ws://127.0.0.1:6230/",
    )

    with pytest.raises(cli.CodexGatewayError, match="上下文与当前配置不一致"):
        cli._gateway_recover_owned(args)


def test_uninstall_external_modification_returns_failure(monkeypatch, tmp_path: Path) -> None:
    config = SimpleNamespace(codex=SimpleNamespace(home=tmp_path))
    monkeypatch.setattr(cli, "_config", lambda _args, ready=False: config)
    monkeypatch.setattr(
        cli,
        "uninstall_notify",
        lambda **_kwargs: {"changed": False, "status": "externally-modified"},
    )

    assert cli._uninstall_notify(Namespace()) == 2


def test_update_yaml_scalar_preserves_comments_and_other_sections(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "# 保留注释\nversion: 2\nfeishu:\n  app_id: \"\"\n  target_open_id: \"\"\nservice:\n  poll_seconds: 2\n",
        encoding="utf-8",
    )
    cli._update_yaml_scalar(path, "feishu", "target_open_id", "ou_owner")
    text = path.read_text(encoding="utf-8")
    assert "# 保留注释" in text
    assert "poll_seconds: 2" in text
    assert 'target_open_id: "ou_owner"' in text


def test_configure_monitor_selects_exact_ids_and_clears_other_selectors(
    monkeypatch, tmp_path: Path
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "# 保留注释\nmonitor:\n  ids:\n    - \"请替换为 Codex 对话 ID\"\n  titles:\n    - \"旧标题\"\n  paths:\n    - \"旧路径\"\nservice:\n  poll_seconds: 2\n",
        encoding="utf-8",
    )
    selectors = SimpleNamespace(
        ids=("请替换为 Codex 对话 ID",),
        titles=("旧标题",),
        paths=("旧路径",),
        configured=lambda: True,
    )
    config = SimpleNamespace(
        path=path,
        codex=SimpleNamespace(home=tmp_path, selectors=selectors),
    )
    records = [
        SimpleNamespace(thread_id="thread-1", title="任务一", cwd="D:/one"),
        SimpleNamespace(thread_id="thread-2", title="任务二", cwd="D:/two"),
    ]

    class Store:
        def __init__(self, *, paths):
            assert paths == "store-paths"

        def select_threads(self, *, include_archived):
            assert include_archived is False
            return records

        def require_readable(self, _operation):
            return None

    monkeypatch.setattr(cli, "_config", lambda _args, ready=False: config)
    monkeypatch.setattr(cli.StorePaths, "from_codex_home", lambda _home: "store-paths")
    monkeypatch.setattr(cli, "CodexStore", Store)
    monkeypatch.setattr("builtins.input", lambda _prompt: "2,1,2")

    assert cli._configure_monitor(Namespace(force=False)) == 0
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert parsed["monitor"] == {
        "ids": ["thread-2", "thread-1"],
        "titles": [],
        "paths": [],
    }
    assert "# 保留注释" in path.read_text(encoding="utf-8")
    assert "poll_seconds: 2" in path.read_text(encoding="utf-8")


def test_desktop_project_assignments_only_use_explicit_codex_metadata(tmp_path: Path) -> None:
    (tmp_path / ".codex-global-state.json").write_text(
        json.dumps(
            {
                "local-projects": {
                    "project-real": {"id": "project-real", "name": "真实项目"},
                },
                "thread-project-assignments": {
                    "thread-project": {"projectKind": "local", "projectId": "project-real"},
                    "thread-missing": {"projectKind": "local", "projectId": "missing"},
                    "thread-cloud": {"projectKind": "cloud", "projectId": "project-real"},
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    assert cli._desktop_project_assignments(tmp_path) == {
        "thread-project": ("project-real", "真实项目")
    }


def test_monitor_cli_json_contract_add_list_remove(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    database = tmp_path / "state.sqlite"
    config = SimpleNamespace(codex=SimpleNamespace(home=tmp_path))
    record = SimpleNamespace(
        thread_id="thread-1",
        title="飞书机器人开发",
        preview="",
        updated_at_ms=1_700_000_000_000,
        created_at_ms=None,
    )

    def runtime(_args):
        return config, StateStore(database), None, {"thread-1": record}

    monkeypatch.setattr(cli, "_monitor_runtime", runtime)
    monkeypatch.setattr(
        cli,
        "_desktop_project_assignments",
        lambda _home: {"thread-1": ("project-1", "FeiShuBOT")},
    )

    assert cli._monitor_add(Namespace(thread_id="thread-1", json=True)) == 0
    added = json.loads(capsys.readouterr().out)
    assert added == {
        "schema_version": 1,
        "success": True,
        "action": "add",
        "thread_id": "thread-1",
        "origin": "manual",
    }

    assert cli._monitor_list(Namespace(json=True)) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["schema_version"] == 1
    assert listed["items"] == [
        {
            "thread_id": "thread-1",
            "title": "飞书机器人开发",
            "group": "FeiShuBOT",
            "project": "FeiShuBOT",
            "origin": "manual",
            "last_activity_at": 1_700_000_000,
            "expires_at": None,
        }
    ]

    assert cli._monitor_remove(Namespace(thread_id="thread-1", json=True)) == 0
    removed = json.loads(capsys.readouterr().out)
    assert removed["removed"] is True
    assert removed["suppressed"] is True

    assert cli._monitor_list(Namespace(json=True)) == 0
    assert json.loads(capsys.readouterr().out) == {"schema_version": 1, "items": []}


def test_monitor_settings_cli_json_contract(monkeypatch, tmp_path: Path, capsys) -> None:
    database = tmp_path / "state.sqlite"
    config = SimpleNamespace(service=SimpleNamespace(database=database))
    monkeypatch.setattr(cli, "_config", lambda _args, ready=False: config)

    assert cli._monitor_settings(Namespace(auto_enabled=None, json=True)) == 0
    assert json.loads(capsys.readouterr().out) == {
        "schema_version": 1,
        "auto_monitoring_enabled": True,
        "effective_at": None,
    }

    monkeypatch.setattr(cli.time, "time", lambda: 1_700_000_000)
    assert cli._monitor_settings(Namespace(auto_enabled="false", json=True)) == 0
    assert json.loads(capsys.readouterr().out) == {
        "schema_version": 1,
        "auto_monitoring_enabled": False,
        "changed": True,
        "effective_at": 1_700_000_000,
    }
    assert cli._monitor_settings(Namespace(auto_enabled="false", json=True)) == 0
    assert json.loads(capsys.readouterr().out)["changed"] is False

    parsed = cli._parser().parse_args(
        ["monitor-settings", "--auto-enabled", "true", "--json"]
    )
    assert parsed.command == "monitor-settings"
    assert parsed.auto_enabled == "true"
    assert parsed.json is True


def test_configure_feishu_never_puts_secret_in_yaml(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "version: 2\nfeishu:\n  app_id: \"\"\n  app_secret_file: secret.dpapi\n  target_open_id: \"\"\n",
        encoding="utf-8",
    )
    config = load_config(path)
    saved: list[str] = []

    class Store:
        def __init__(self, secret_path):
            assert secret_path == config.feishu.app_secret_file

        def save(self, value):
            saved.append(value)

    monkeypatch.setattr(cli, "_config", lambda _args, ready=False: config)
    monkeypatch.setattr(cli, "DpapiSecretStore", Store)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: "super-secret-value")
    assert cli._configure_feishu(Namespace(app_id="cli_ABC123", secret_stdin=False)) == 0
    text = path.read_text(encoding="utf-8")
    assert saved == ["super-secret-value"]
    assert "super-secret-value" not in text
    assert 'app_id: "cli_ABC123"' in text


def test_configure_feishu_reuses_valid_configured_app_id(monkeypatch, tmp_path: Path) -> None:
    """已预填 App ID 时只需隐藏输入 Secret，不再要求远控复制 ID。"""

    path = tmp_path / "config.yaml"
    path.write_text(
        "version: 2\nfeishu:\n  app_id: \"cli_PREFILLED\"\n"
        "  app_secret_file: secret.dpapi\n  target_open_id: \"\"\n",
        encoding="utf-8",
    )
    config = load_config(path)
    saved: list[str] = []

    class Store:
        def __init__(self, secret_path):
            assert secret_path == config.feishu.app_secret_file

        def save(self, value):
            saved.append(value)

    monkeypatch.setattr(cli, "_config", lambda _args, ready=False: config)
    monkeypatch.setattr(cli, "DpapiSecretStore", Store)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: "local-secret")
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: (_ for _ in ()).throw(AssertionError("不得再次询问 App ID")),
    )

    assert cli._configure_feishu(Namespace(app_id=None, secret_stdin=False)) == 0
    assert saved == ["local-secret"]
    assert 'app_id: "cli_PREFILLED"' in path.read_text(encoding="utf-8")


def test_test_feishu_retries_each_operation_five_times_and_reuses_idempotency_key(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    """飞书测试命令应使用单层有限重试，发送重试必须复用同一幂等键。"""

    secret_path = tmp_path / "secret.dpapi"
    secret_path.write_bytes(b"protected")
    config = SimpleNamespace(
        messaging=SimpleNamespace(backend="feishu"),
        feishu=SimpleNamespace(
            app_id="cli_TEST",
            app_secret_file=secret_path,
            target_open_id="ou_target",
            connect_timeout_seconds=1.0,
        ),
        service=SimpleNamespace(max_attempts=5, retry_delays=(0, 0, 0, 0, 0)),
    )
    monkeypatch.setattr(cli, "_config", lambda _args: config)
    monkeypatch.setattr(cli.DpapiSecretStore, "load", lambda _self: "secret")
    starts = 0
    sends = 0
    keys: list[str] = []
    stops = 0

    class Channel:
        def __init__(self, **_kwargs):
            pass

        def start(self, _callback):
            nonlocal starts
            starts += 1
            if starts < 5:
                raise RuntimeError("temporary connect failure")

        def send_text(self, _text, *, idempotency_key):
            nonlocal sends
            sends += 1
            keys.append(idempotency_key)
            if sends < 5:
                raise RuntimeError("temporary send failure")
            return "om_test"

        def stop(self):
            nonlocal stops
            stops += 1

    monkeypatch.setattr(cli, "FeishuMessageChannel", Channel)
    monkeypatch.setattr(cli.time, "sleep", lambda _delay: None)

    assert cli._test_feishu(Namespace(text="test")) == 0
    assert starts == 5
    assert sends == 5
    assert len(set(keys)) == 1
    assert keys[0].startswith("manual-test:")
    assert stops == 1
    assert json.loads(capsys.readouterr().out) == {
        "sent": True,
        "message_id": "om_test",
    }


def test_test_feishu_usage_guide_sends_preview_images_then_hint(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    secret_path = tmp_path / "secret.dpapi"
    secret_path.write_bytes(b"protected")
    config = SimpleNamespace(
        messaging=SimpleNamespace(backend="feishu"),
        feishu=SimpleNamespace(
            app_id="cli_TEST",
            app_secret_file=secret_path,
            target_open_id="ou_target",
            connect_timeout_seconds=1.0,
        ),
        service=SimpleNamespace(max_attempts=1, retry_delays=(0,)),
    )
    monkeypatch.setattr(cli, "_config", lambda _args: config)
    monkeypatch.setattr(cli.DpapiSecretStore, "load", lambda _self: "secret")
    monkeypatch.setattr(
        cli,
        "feishu_usage_images",
        lambda: (("one.png", b"one"), ("two.png", b"two")),
    )
    texts: list[tuple[str, str]] = []
    images: list[tuple[bytes, str]] = []

    class Channel:
        def __init__(self, **_kwargs):
            pass

        def start(self, _callback):
            return None

        def send_text(self, text, *, idempotency_key):
            texts.append((text, idempotency_key))
            return "om_text"

        def send_image(self, data, *, idempotency_key):
            images.append((data, idempotency_key))
            return f"om_image_{len(images)}"

        def stop(self):
            return None

    monkeypatch.setattr(cli, "FeishuMessageChannel", Channel)

    assert cli._test_feishu(Namespace(text="ignored", usage_guide=True)) == 0
    assert [item[0] for item in texts] == [
        "以上为使用说明，如果想要文字版使用说明，请发送“文字版使用说明”哦"
    ]
    assert texts[0][1].endswith(":usage-footer")
    assert [item[0] for item in images] == [b"one", b"two"]
    assert images[0][1].endswith(":usage-image:1")
    assert images[1][1].endswith(":usage-image:2")
    assert json.loads(capsys.readouterr().out) == {
        "sent": True,
        "image_message_ids": ["om_image_1", "om_image_2"],
        "footer_message_id": "om_text",
    }


def test_test_feishu_stops_after_retry_exhaustion(monkeypatch, tmp_path: Path) -> None:
    """连接五次仍失败时不得发送，并且仍执行一次 stop 清理。"""

    secret_path = tmp_path / "secret.dpapi"
    secret_path.write_bytes(b"protected")
    config = SimpleNamespace(
        messaging=SimpleNamespace(backend="feishu"),
        feishu=SimpleNamespace(
            app_id="cli_TEST",
            app_secret_file=secret_path,
            target_open_id="ou_target",
            connect_timeout_seconds=1.0,
        ),
        service=SimpleNamespace(max_attempts=5, retry_delays=(0, 0, 0, 0, 0)),
    )
    monkeypatch.setattr(cli, "_config", lambda _args: config)
    monkeypatch.setattr(cli.DpapiSecretStore, "load", lambda _self: "secret")
    starts = 0
    stops = 0

    class Channel:
        def __init__(self, **_kwargs):
            pass

        def start(self, _callback):
            nonlocal starts
            starts += 1
            raise RuntimeError("connect failure")

        def send_text(self, _text, *, idempotency_key):
            raise AssertionError("连接失败后不得发送")

        def stop(self):
            nonlocal stops
            stops += 1

    monkeypatch.setattr(cli, "FeishuMessageChannel", Channel)
    monkeypatch.setattr(cli.time, "sleep", lambda _delay: None)

    with pytest.raises(RetryExhausted):
        cli._test_feishu(Namespace(text="test"))
    assert starts == 5
    assert stops == 1


def test_validate_reports_unready_config_without_throwing(monkeypatch, tmp_path: Path, capsys) -> None:
    class UnreadyConfig:
        codex = SimpleNamespace(home=tmp_path)
        messaging = SimpleNamespace(backend="feishu")

        @staticmethod
        def validate_ready() -> None:
            raise cli.ConfigError("飞书 App Secret 尚未安全保存")

    monkeypatch.setattr(cli, "_config", lambda _args, ready=False: UnreadyConfig())
    monkeypatch.setattr(
        cli.StorePaths,
        "from_codex_home",
        lambda _home: SimpleNamespace(
            state_db=tmp_path / "missing-state.sqlite",
            history_db=tmp_path / "missing-history.sqlite",
        ),
    )
    assert cli._validate(Namespace()) == 2
    output = capsys.readouterr().out
    assert "飞书 App Secret 尚未安全保存" in output
    assert "配置已解析，但尚未就绪" in output


def test_doctor_reports_missing_feishu_credentials_without_connecting_feishu(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    config = SimpleNamespace(
        codex=SimpleNamespace(
            command="codex",
            reply_transport="desktop_app_tools",
            desktop_log_dir=tmp_path / "Codex" / "Logs",
            shared_websocket_url="ws://127.0.0.1:6230",
            gateway_pid_file=tmp_path / "gateway.pid",
            shared_desktop_state_file=tmp_path / "desktop.json",
        ),
        messaging=SimpleNamespace(backend="feishu"),
        feishu=SimpleNamespace(
            app_id="",
            app_secret_file=tmp_path / "missing.dpapi",
            target_open_id="",
        ),
    )

    class Session:
        def close(self):
            pass

    class Client:
        def __init__(self, log_dir):
            assert log_dir == config.codex.desktop_log_dir

        def open_verified(self):
            return Session()

    monkeypatch.setattr(cli, "_config", lambda _args, ready=False: config)
    monkeypatch.setattr(cli, "DesktopAppToolsClient", Client)
    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda name: object() if name == "lark_channel" else None)

    assert cli._doctor(Namespace()) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["codex_desktop_tool_verified"] is True
    assert result["codex_transport"] == "desktop_app_tools_named_pipe"
    assert result["lark_channel_package_present"] is True
    assert result["app_id_configured"] is False
    assert result["app_secret_dpapi_present"] is False
    assert result["target_open_id_configured"] is False
    assert result["feishu_network_connection_opened"] is False


def test_verify_wechat_only_checks_exact_identities(monkeypatch) -> None:
    """微信预检不得注册监听或发送消息。"""

    calls: list[tuple] = []
    config = SimpleNamespace(
        wechat=SimpleNamespace(
            tool_account_nickname="通知小号",
            tool_wechat_id="wxid-tool",
            target_chat="唯一联系人",
            target_wechat_id="wxid-main",
        )
    )

    class Adapter:
        def verify_account(self, wechat_id):
            calls.append(("account", wechat_id))
            return True

        def is_online(self):
            calls.append(("online",))
            return True

        def verify_friend(self, chat_name, wechat_id):
            calls.append(("friend", chat_name, wechat_id))
            return True

    def create_adapter(*, account_nickname):
        calls.append(("create", account_nickname))
        return Adapter()

    monkeypatch.setattr(cli, "_config", lambda _args: config)
    monkeypatch.setattr(cli, "WxAutoX4Adapter", create_adapter)

    assert cli._verify_wechat(Namespace()) == 0
    assert calls == [
        ("create", "通知小号"),
        ("account", "wxid-tool"),
        ("online",),
        ("friend", "唯一联系人", "wxid-main"),
    ]


def test_free_probe_reads_nickname_from_config_and_passes_opt_in(monkeypatch) -> None:
    calls: list[tuple[str, bool]] = []
    config = SimpleNamespace(wechat=SimpleNamespace(tool_account_nickname="工具小号"))
    monkeypatch.setattr(cli, "_config", lambda _args, ready=False: config)
    monkeypatch.setattr(
        cli,
        "probe_tool_window",
        lambda nickname, *, single_visible_window, diagnostic_unverified_identity: calls.append(
            (nickname, single_visible_window, diagnostic_unverified_identity)
        ) or {"production_ready": False},
    )

    assert cli._probe_free_wechat(
        Namespace(
            nickname=None,
            single_visible_window=True,
            diagnostic_unverified_identity=True,
        )
    ) == 0
    assert calls == [("工具小号", True, True)]


def test_free_probe_explicit_nickname_does_not_load_config(monkeypatch) -> None:
    calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        cli,
        "_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("不应读取配置")),
    )
    monkeypatch.setattr(
        cli,
        "probe_tool_window",
        lambda nickname, *, single_visible_window, diagnostic_unverified_identity: calls.append(
            (nickname, single_visible_window, diagnostic_unverified_identity)
        ) or {},
    )

    assert cli._probe_free_wechat(
        Namespace(
            nickname="指定小号",
            single_visible_window=False,
            diagnostic_unverified_identity=False,
        )
    ) == 0
    assert calls == [("指定小号", False, False)]


def test_start_rejects_probe_only_before_spawning(monkeypatch) -> None:
    config = SimpleNamespace(wechat=SimpleNamespace(backend="probe_only"))
    monkeypatch.setattr(cli, "_config", lambda _args: config)
    monkeypatch.setattr(
        cli.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("probe_only 不得创建后台进程")
        ),
    )

    with pytest.raises(ConfigError, match="禁止启动"):
        cli._start(Namespace())


def test_run_rejects_probe_only_before_logging_or_pid(monkeypatch) -> None:
    config = SimpleNamespace(wechat=SimpleNamespace(backend="probe_only"))
    monkeypatch.setattr(cli, "_config", lambda _args: config)
    monkeypatch.setattr(
        cli,
        "configure_logging",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("probe_only 不得创建运行日志")
        ),
    )

    with pytest.raises(ConfigError, match="禁止启动"):
        cli._run(Namespace())


def test_pre_activation_baseline_requires_stopped_service(monkeypatch, tmp_path: Path) -> None:
    config = SimpleNamespace(
        service=SimpleNamespace(pid_file=tmp_path / "service.pid", database=tmp_path / "state.sqlite")
    )
    monkeypatch.setattr(cli, "_config", lambda _args, ready=False: config)
    monkeypatch.setattr(cli, "instance_running", lambda _path: True)
    monkeypatch.setattr(
        cli,
        "StateStore",
        lambda _path: (_ for _ in ()).throw(AssertionError("运行中不得打开状态库")),
    )

    with pytest.raises(cli.InstanceError, match="先停止服务"):
        cli._baseline_pre_activation_hooks(Namespace(expected_count=0))


def test_pre_activation_baseline_passes_exact_observed_count(monkeypatch, tmp_path: Path) -> None:
    calls: list[object] = []
    config = SimpleNamespace(
        service=SimpleNamespace(pid_file=tmp_path / "service.pid", database=tmp_path / "state.sqlite")
    )

    class Store:
        def __init__(self, path):
            calls.append(("open", path))

        def baseline_pending_hooks(self, expected_count):
            calls.append(("baseline", expected_count))
            return expected_count

        def close(self):
            calls.append(("close",))

    monkeypatch.setattr(cli, "_config", lambda _args, ready=False: config)
    monkeypatch.setattr(cli, "instance_running", lambda _path: False)
    monkeypatch.setattr(
        cli,
        "_selected_terminal_events",
        lambda _config: calls.append(("snapshot",)) or (),
    )
    monkeypatch.setattr(
        cli,
        "_baseline_selected_terminal_turns",
        lambda events, store: calls.append(("terminal", events, store)) or 0,
    )
    monkeypatch.setattr(cli, "StateStore", Store)

    assert cli._baseline_pre_activation_hooks(Namespace(expected_count=3)) == 0
    assert calls[:3] == [
        ("snapshot",),
        ("open", config.service.database),
        ("baseline", 3),
    ]
    assert calls[3][0:2] == ("terminal", ())
    assert isinstance(calls[3][2], Store)
    assert calls[4] == ("close",)


def test_selected_terminal_events_only_captures_existing_terminal_snapshots(
    monkeypatch, tmp_path: Path
) -> None:
    records = {
        "terminal": SimpleNamespace(thread_id="terminal"),
        "running": SimpleNamespace(thread_id="running"),
    }
    terminal_event = SimpleNamespace(dedupe_key="terminal:turn-1:completed")
    snapshots = {
        "terminal": SimpleNamespace(require_readable=lambda: None),
        "running": SimpleNamespace(require_readable=lambda: None),
    }
    config = SimpleNamespace(
        codex=SimpleNamespace(
            home=tmp_path,
            selectors=SimpleNamespace(ids=("terminal", "running"), titles=(), paths=()),
        )
    )

    class CodexStore:
        def __init__(self, *, paths):
            assert paths == "store-paths"

        def get_thread(self, thread_id):
            return records[thread_id]

        def require_readable(self, _operation):
            return None

        def snapshot(self, thread_id):
            return snapshots[thread_id]

    monkeypatch.setattr(cli.StorePaths, "from_codex_home", lambda _home: "store-paths")
    monkeypatch.setattr(cli, "CodexStore", CodexStore)
    monkeypatch.setattr(
        cli,
        "snapshot_to_event",
        lambda snapshot: terminal_event if snapshot is snapshots["terminal"] else None,
    )

    assert cli._selected_terminal_events(config) == (terminal_event,)


def test_baseline_selected_terminal_turns_is_idempotent() -> None:
    old_event = SimpleNamespace(dedupe_key="thread:old:completed")
    new_event = SimpleNamespace(dedupe_key="thread:new:failed")
    marked: list[str] = []

    class Store:
        @staticmethod
        def was_processed(event_key):
            return event_key == old_event.dedupe_key

        @staticmethod
        def mark_processed(event_key):
            marked.append(event_key)

    assert cli._baseline_selected_terminal_turns((old_event, new_event), Store()) == 1
    assert marked == [new_event.dedupe_key]
