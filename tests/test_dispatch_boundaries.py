from __future__ import annotations

import builtins
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import _bootstrap

from progress_notify.dispatcher import dispatch_json_argument


class ImportBoundaryTests(unittest.TestCase):
    def test_importing_cli_does_not_load_classifier_or_notifiers(self) -> None:
        code = (
            "import json, sys; import progress_notify.cli; "
            "print(json.dumps(sorted(name for name in sys.modules "
            "if name in {'progress_notify.classifier','progress_notify.notifiers'})))"
        )
        environment = os.environ.copy()
        existing = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = str(_bootstrap.SRC) + (
            os.pathsep + existing if existing else ""
        )

        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=_bootstrap.ROOT,
            env=environment,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), [])

    def test_safe_error_explains_unreachable_http_endpoint_without_url(self) -> None:
        from progress_notify.cli import _safe_error
        from progress_notify.http_client import HttpRequestError

        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            with patch("progress_notify.cli.PROJECT_ROOT", Path(directory)):
                with contextlib.redirect_stderr(output):
                    _safe_error(
                        "send-test failed",
                        HttpRequestError(
                            "secret https://example.invalid/hook?token=value",
                            attempts=3,
                            retryable=True,
                        ),
                    )

        diagnostic = output.getvalue()
        self.assertIn("endpoint unreachable or timed out", diagnostic)
        self.assertIn("attempts=3", diagnostic)
        self.assertNotIn("example.invalid", diagnostic)
        self.assertNotIn("token=value", diagnostic)

    def test_original_notify_starts_before_runner_import_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            state_path = project / ".state" / "install-state.json"
            state_path.parent.mkdir()
            state_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "had_original_notify": True,
                        "original_notify": ["python", "legacy-notify.py"],
                        "installed_notify": ["python", "progress-notify.py"],
                    }
                ),
                encoding="utf-8",
            )
            raw_event = (
                '{"type":"agent-turn-complete","thread-id":"thr_selected"}'
            )
            real_import = builtins.__import__

            def fail_runner_import(
                name: str,
                globals_: object = None,
                locals_: object = None,
                fromlist: object = (),
                level: int = 0,
            ):
                if name == "runner" and level == 1:
                    raise ImportError("simulated runner import failure")
                if name == "progress_notify.runner":
                    raise ImportError("simulated runner import failure")
                return real_import(name, globals_, locals_, fromlist, level)

            with patch("progress_notify.dispatcher.subprocess.Popen") as popen:
                with patch("builtins.__import__", side_effect=fail_runner_import):
                    with self.assertRaises(ImportError):
                        dispatch_json_argument(raw_event, project_root=project)

        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0][-1], raw_event)


if __name__ == "__main__":
    unittest.main()
