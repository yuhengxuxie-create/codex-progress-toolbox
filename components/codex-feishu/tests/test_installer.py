from __future__ import annotations

import json
from pathlib import Path

import pytest

from progress_wx import installer


def test_replace_notify_preserves_other_toml() -> None:
    source = 'model = "gpt-5"\nnotify = ["old.exe", "--flag"]\n\n[features]\nweb = true\n'
    updated = installer._replace_notify(source, ["python.exe", "hook.py"])
    assert 'notify = [ "python.exe", "hook.py" ]' in updated
    assert '[features]\nweb = true' in updated


def test_install_and_uninstall_restore_original(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    hook = project / "progress-wx-hook.py"
    hook.write_text("pass\n", encoding="utf-8")
    progress_config = project / "config.yaml"
    progress_config.write_text("version: 1\n", encoding="utf-8")
    codex = tmp_path / "codex"
    codex.mkdir()
    config_path = codex / "config.toml"
    original = ['old.exe', '--keep']
    config_path.write_text('notify = ["old.exe", "--keep"]\nmodel = "gpt-5"\n', encoding="utf-8")
    monkeypatch.setattr(installer, "PROJECT_ROOT", project)
    monkeypatch.setattr(installer, "INSTALL_STATE_PATH", project / ".state" / "install-state.json")
    result = installer.install_notify(
        python_executable=Path(__import__("sys").executable),
        codex_home_path=codex,
        progress_config_path=progress_config,
    )
    assert result["changed"] is True
    state = json.loads(installer.INSTALL_STATE_PATH.read_text(encoding="utf-8"))
    assert state["original_notify"] == original
    restored = installer.uninstall_notify(codex_home_path=codex)
    assert restored["restored_notify"] == original
    assert 'notify = [ "old.exe", "--keep" ]' in config_path.read_text(encoding="utf-8")


def test_prepared_install_is_recovered_without_guessing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "progress-wx-hook.py").write_text("pass\n", encoding="utf-8")
    progress_config = project / "config.yaml"
    progress_config.write_text("version: 1\n", encoding="utf-8")
    codex = tmp_path / "codex"
    codex.mkdir()
    config_path = codex / "config.toml"
    config_path.write_text('notify = ["old.exe"]\n', encoding="utf-8")
    state_path = project / ".state" / "install-state.json"
    monkeypatch.setattr(installer, "PROJECT_ROOT", project)
    monkeypatch.setattr(installer, "INSTALL_STATE_PATH", state_path)
    desired = installer.desired_notify(Path(__import__("sys").executable), progress_config)
    state_path.parent.mkdir()
    state_path.write_text(
        json.dumps(
            {
                "version": 2,
                "phase": "prepared",
                "config_path": str(config_path),
                "installed_notify": desired,
                "had_original_notify": True,
                "original_notify": ["old.exe"],
                "backup_path": None,
            }
        ),
        encoding="utf-8",
    )
    result = installer.install_notify(
        python_executable=Path(__import__("sys").executable),
        codex_home_path=codex,
        progress_config_path=progress_config,
    )
    assert result["status"] == "recovered-install"
    assert json.loads(state_path.read_text(encoding="utf-8"))["phase"] == "installed"


def test_malformed_state_dict_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_path = tmp_path / "install-state.json"
    state_path.write_text('{"version": 2}', encoding="utf-8")
    monkeypatch.setattr(installer, "INSTALL_STATE_PATH", state_path)
    with pytest.raises(installer.InstallError):
        installer._load_state()


def test_legacy_notify_command_is_upgraded_and_remains_uninstallable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "progress-wx-hook.py").write_text("pass\n", encoding="utf-8")
    progress_config = project / "custom.yaml"
    progress_config.write_text("version: 1\n", encoding="utf-8")
    codex = tmp_path / "codex"
    codex.mkdir()
    config_path = codex / "config.toml"
    python = Path(__import__("sys").executable).resolve()
    legacy = [str(python), str((project / "progress-wx-hook.py").resolve())]
    config_path.write_text(installer._notify_line(legacy), encoding="utf-8")
    state_path = project / ".state" / "install-state.json"
    state_path.parent.mkdir()
    state_path.write_text(
        json.dumps(
            {
                "version": 2,
                "phase": "installed",
                "config_path": str(config_path),
                "installed_notify": legacy,
                "had_original_notify": True,
                "original_notify": ["old.exe", "--keep"],
                "backup_path": None,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(installer, "PROJECT_ROOT", project)
    monkeypatch.setattr(installer, "INSTALL_STATE_PATH", state_path)

    result = installer.install_notify(
        python_executable=python,
        codex_home_path=codex,
        progress_config_path=progress_config,
    )
    desired = installer.desired_notify(python, progress_config)
    assert result["status"] == "upgraded-notify-command"
    assert json.loads(state_path.read_text(encoding="utf-8"))["installed_notify"] == desired
    assert installer._read_config(config_path)[1]["notify"] == desired

    restored = installer.uninstall_notify(codex_home_path=codex)
    assert restored["restored_notify"] == ["old.exe", "--keep"]
    assert installer._read_config(config_path)[1]["notify"] == ["old.exe", "--keep"]


def test_uninstall_restores_child_inside_current_outer_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """外部 wrapper 重新包裹后，卸载只替换 child，不重复嵌套 wrapper。"""

    project = tmp_path / "project"
    project.mkdir()
    (project / "progress-wx-hook.py").write_text("pass\n", encoding="utf-8")
    progress_config = project / "config.yaml"
    progress_config.write_text("version: 1\n", encoding="utf-8")
    codex = tmp_path / "codex"
    codex.mkdir()
    config_path = codex / "config.toml"
    old = ["old.exe", "--keep"]
    prefix = ["computer-use.exe", "turn-ended", "--mode", "desktop"]
    original_wrapper = [
        *prefix,
        "--previous-notify",
        json.dumps(old, ensure_ascii=False),
    ]
    config_path.write_text(installer._notify_line(original_wrapper), encoding="utf-8")
    monkeypatch.setattr(installer, "PROJECT_ROOT", project)
    monkeypatch.setattr(installer, "INSTALL_STATE_PATH", project / ".state" / "install-state.json")

    python = Path(__import__("sys").executable).resolve()
    installer.install_notify(
        python_executable=python,
        codex_home_path=codex,
        progress_config_path=progress_config,
    )
    installed = installer.desired_notify(python, progress_config)
    current_wrapper = [
        *prefix,
        "--previous-notify",
        json.dumps(installed, ensure_ascii=False),
    ]
    # 模拟外部集成在安装后重新包裹顶层 notify。
    config_path.write_text(installer._notify_line(current_wrapper), encoding="utf-8")

    restored = installer.uninstall_notify(codex_home_path=codex)
    assert restored["status"] == "uninstalled-restored-wrapper"
    assert restored["restored_notify"] == original_wrapper
    assert installer._read_config(config_path)[1]["notify"] == original_wrapper


def test_uninstall_rejects_ambiguous_wrapper_prefix_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """当前 wrapper 前缀变化时不能猜测用户想保留哪一层。"""

    project = tmp_path / "project"
    project.mkdir()
    (project / "progress-wx-hook.py").write_text("pass\n", encoding="utf-8")
    progress_config = project / "config.yaml"
    progress_config.write_text("version: 1\n", encoding="utf-8")
    codex = tmp_path / "codex"
    codex.mkdir()
    config_path = codex / "config.toml"
    old = ["old.exe"]
    original_prefix = ["computer-use.exe", "turn-ended"]
    original_wrapper = [
        *original_prefix,
        "--previous-notify",
        json.dumps(old),
    ]
    config_path.write_text(installer._notify_line(original_wrapper), encoding="utf-8")
    state_path = project / ".state" / "install-state.json"
    monkeypatch.setattr(installer, "PROJECT_ROOT", project)
    monkeypatch.setattr(installer, "INSTALL_STATE_PATH", state_path)
    python = Path(__import__("sys").executable).resolve()
    installer.install_notify(
        python_executable=python,
        codex_home_path=codex,
        progress_config_path=progress_config,
    )
    installed = installer.desired_notify(python, progress_config)
    changed_prefix = ["computer-use.exe", "turn-ended", "--other-mode"]
    config_path.write_text(
        installer._notify_line(
            [*changed_prefix, "--previous-notify", json.dumps(installed)]
        ),
        encoding="utf-8",
    )
    before = config_path.read_text(encoding="utf-8")

    with pytest.raises(installer.InstallError):
        installer.uninstall_notify(codex_home_path=codex)

    assert config_path.read_text(encoding="utf-8") == before
    assert state_path.exists()


def test_uninstall_rejects_wrapper_when_saved_original_is_not_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """只有一侧可解析 previous-notify 时不得把未知命令当成 child。"""

    project = tmp_path / "project"
    project.mkdir()
    (project / "progress-wx-hook.py").write_text("pass\n", encoding="utf-8")
    progress_config = project / "config.yaml"
    progress_config.write_text("version: 1\n", encoding="utf-8")
    codex = tmp_path / "codex"
    codex.mkdir()
    config_path = codex / "config.toml"
    old = ["old.exe"]
    config_path.write_text(installer._notify_line(old), encoding="utf-8")
    state_path = project / ".state" / "install-state.json"
    monkeypatch.setattr(installer, "PROJECT_ROOT", project)
    monkeypatch.setattr(installer, "INSTALL_STATE_PATH", state_path)
    python = Path(__import__("sys").executable).resolve()
    installer.install_notify(
        python_executable=python,
        codex_home_path=codex,
        progress_config_path=progress_config,
    )
    installed = installer.desired_notify(python, progress_config)
    prefix = ["computer-use.exe", "turn-ended"]
    config_path.write_text(
        installer._notify_line(
            [*prefix, "--previous-notify", json.dumps(installed)]
        ),
        encoding="utf-8",
    )

    with pytest.raises(installer.InstallError, match="不是可验证"):
        installer.uninstall_notify(codex_home_path=codex)


def test_previous_notify_wrapper_rejects_non_trailing_or_non_argv_json() -> None:
    """wrapper 解析器必须拒绝尾部标记不严格或 JSON 类型错误的 argv。"""

    prefix = ["wrapper.exe", "turn-ended"]
    assert installer._previous_notify_wrapper(
        [*prefix, "--previous-notify", '["old.exe"]']
    ) == (prefix, ["old.exe"])
    assert installer._previous_notify_wrapper(
        [*prefix, "--previous-notify", '["old.exe"]', "extra"]
    ) is None
    assert installer._previous_notify_wrapper(
        [*prefix, "--previous-notify", '{"command":"old.exe"}']
    ) is None
    assert installer._previous_notify_wrapper(
        [*prefix, "--previous-notify", '["old.exe", ""]']
    ) is None
