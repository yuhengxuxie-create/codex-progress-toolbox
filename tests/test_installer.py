from __future__ import annotations

import json
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import _bootstrap  # noqa: F401

from progress_notify.installer import install, uninstall
from progress_notify.dispatcher import forward_original_notify


class InstallerRecoveryTests(unittest.TestCase):
    def make_layout(self, base: Path) -> tuple[Path, Path, Path]:
        project = base / "project"
        codex_home = base / "codex-home"
        project.mkdir()
        codex_home.mkdir()
        (project / "progress-notify.py").write_text(
            "raise SystemExit(0)\n", encoding="utf-8"
        )
        config = codex_home / "config.toml"
        config.write_text(
            '# 用户原配置\nnotify = ["python", "legacy-notify.py"]\n'
            'model = "gpt-test"\n\n[features]\nhooks = true\n',
            encoding="utf-8",
        )
        return project, codex_home, config

    def test_install_is_idempotent_and_uninstall_restores_original_notify(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, codex_home, config = self.make_layout(Path(directory))

            first = install(
                project_root=project,
                codex_home=codex_home,
                python_executable=sys.executable,
            )
            second = install(
                project_root=project,
                codex_home=codex_home,
                python_executable=sys.executable,
            )

            installed = tomllib.loads(config.read_text(encoding="utf-8"))
            state = json.loads(
                (project / ".state" / "install-state.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(first["changed"])
            self.assertFalse(second["changed"])
            self.assertEqual(state["original_notify"], ["python", "legacy-notify.py"])
            self.assertEqual(installed["notify"], state["installed_notify"])
            self.assertTrue(Path(state["backup_path"]).is_file())

            removed = uninstall(project_root=project, codex_home=codex_home)
            restored_text = config.read_text(encoding="utf-8")
            restored = tomllib.loads(restored_text)

        self.assertTrue(removed["changed"])
        self.assertEqual(restored["notify"], ["python", "legacy-notify.py"])
        self.assertEqual(restored["model"], "gpt-test")
        self.assertTrue(restored["features"]["hooks"])
        self.assertIn("# 用户原配置", restored_text)

    def test_uninstall_does_not_overwrite_a_later_user_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, codex_home, config = self.make_layout(Path(directory))
            install(
                project_root=project,
                codex_home=codex_home,
                python_executable=sys.executable,
            )
            config.write_text(
                'notify = ["python", "new-user-choice.py"]\n', encoding="utf-8"
            )

            result = uninstall(project_root=project, codex_home=codex_home)
            current = tomllib.loads(config.read_text(encoding="utf-8"))

        self.assertFalse(result["changed"])
        self.assertEqual(result["status"], "externally-modified")
        self.assertEqual(current["notify"], ["python", "new-user-choice.py"])

    def test_existing_notify_is_forwarded_with_original_json_argument(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, codex_home, _config = self.make_layout(Path(directory))
            install(
                project_root=project,
                codex_home=codex_home,
                python_executable=sys.executable,
            )
            raw_event = '{"type":"agent-turn-complete","thread-id":"thr_1"}'

            with patch("progress_notify.dispatcher.subprocess.Popen") as popen:
                result = forward_original_notify(raw_event, project_root=project)

        self.assertTrue(result.attempted)
        self.assertTrue(result.started)
        command = popen.call_args.args[0]
        self.assertTrue(Path(command[0]).name.casefold().startswith("python"))
        self.assertEqual(command[1], "legacy-notify.py")
        self.assertEqual(command[-1], raw_event)


if __name__ == "__main__":
    unittest.main()
