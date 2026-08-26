from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests import _bootstrap

from progress_notify.thread_manager import (
    ConfigConflictError,
    ThreadManagerError,
    USER_FACING_THREAD_SOURCE_KINDS,
    build_thread_catalog,
    classify_conversation_location,
    classify_thread_location,
    load_thread_manager_state,
    load_codex_project_index,
    normalize_thread_ids,
    save_thread_ids,
)


class ThreadManagerStateTests(unittest.TestCase):
    def test_user_facing_sources_include_desktop_projects_but_not_subagents(self) -> None:
        self.assertIn("appServer", USER_FACING_THREAD_SOURCE_KINDS)
        self.assertIn("vscode", USER_FACING_THREAD_SOURCE_KINDS)
        self.assertNotIn("subAgent", USER_FACING_THREAD_SOURCE_KINDS)

    def test_loads_scalar_placeholder_and_preserves_effective_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = _bootstrap.write_config(
                Path(directory) / "config.local.json",
                thread_ids="${TEST_THREAD_IDS}",
            )
            state = load_thread_manager_state(
                path,
                environ={"TEST_THREAD_IDS": "thr_b, thr_a,thr_b"},
            )

        self.assertEqual(state.thread_ids, ("thr_b", "thr_a"))

    def test_catalog_keeps_configured_thread_missing_from_server(self) -> None:
        records = build_thread_catalog(
            ("thr_live", "thr_missing"),
            (
                {
                    "id": "thr_live",
                    "name": " 在线\n会话 ",
                    "updatedAt": 123,
                },
            ),
            title_overrides={"thr_missing": "旧会话"},
        )

        self.assertEqual([record.thread_id for record in records], ["thr_live", "thr_missing"])
        self.assertEqual(records[0].title, "在线 会话")
        self.assertTrue(records[0].available)
        self.assertEqual(records[1].title, "旧会话")
        self.assertFalse(records[1].available)

    def test_catalog_deduplicates_active_and_archived_results(self) -> None:
        records = build_thread_catalog(
            ("thr_a",),
            ({"id": "thr_a", "name": "当前"},),
            (
                {"id": "thr_a", "name": "归档重复"},
                {"id": "thr_b", "preview": "归档预览"},
            ),
        )

        self.assertEqual([record.thread_id for record in records], ["thr_a", "thr_b"])
        self.assertFalse(records[0].archived)
        self.assertTrue(records[1].archived)

    def test_fallback_classifies_generated_workspace_but_does_not_guess_projects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            one_time = home / "Documents" / "Codex" / "2026-08-22" / "new-chat"
            project = Path(directory) / "projects" / "ScreenSharing"

            one_time_location = classify_conversation_location(one_time.as_posix(), user_home=home)
            project_location = classify_conversation_location(project.as_posix(), user_home=home)

        self.assertEqual(one_time_location.conversation_type, "one_time")
        self.assertEqual(one_time_location.project_name, "")
        self.assertEqual(project_location.conversation_type, "unclassified")
        self.assertEqual(project_location.project_name, "ScreenSharing")

    def test_catalog_records_project_metadata_and_excludes_children(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            codex_home = Path(directory) / "codex-home"
            codex_home.mkdir()
            project = Path(directory) / "projects" / "进度监控"
            one_time = home / "Documents" / "Codex" / "2026-08-22" / "quick-chat"
            (codex_home / ".codex-global-state.json").write_text(
                json.dumps(
                    {
                        "local-projects": {
                            "project-1": {
                                "id": "project-1",
                                "name": "进度监控项目",
                                "rootPaths": [str(project)],
                            }
                        },
                        "thread-project-assignments": {
                            "thr_project": {
                                "projectId": "project-1",
                                "projectKind": "local",
                            }
                        },
                        "projectless-thread-ids": ["thr_once"],
                        "unrelated-sensitive-ui-state": {"must": "be ignored"},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            project_index = load_codex_project_index(codex_home)
            records = build_thread_catalog(
                ("thr_project",),
                (
                    {
                        "id": "thr_project",
                        "name": "项目会话",
                        "cwd": str(project),
                        "source": "appServer",
                    },
                    {
                        "id": "thr_once",
                        "name": "临时会话",
                        "cwd": str(one_time),
                        "source": "vscode",
                    },
                    {
                        "id": "thr_child",
                        "name": "内部子代理",
                        "cwd": str(project),
                        "source": {"subAgent": {"thread_spawn": {}}},
                        "parentThreadId": "thr_project",
                    },
                ),
                user_home=home,
                project_index=project_index,
            )

        self.assertEqual([record.thread_id for record in records], ["thr_project", "thr_once"])
        self.assertEqual(records[0].conversation_type, "project")
        self.assertEqual(records[0].project_name, "进度监控项目")
        self.assertEqual(records[0].project_key, "project:project-1")
        self.assertEqual(records[0].source_kind, "appServer")
        self.assertEqual(records[1].conversation_type, "one_time")

    def test_missing_or_stale_project_state_has_safe_fallbacks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            codex_home.mkdir()
            (codex_home / ".codex-global-state.json").write_text(
                "not-json",
                encoding="utf-8",
            )
            empty_index = load_codex_project_index(codex_home)
            fallback = classify_thread_location(
                "thr_unknown",
                str(Path(directory) / "workspace"),
                project_index=empty_index,
            )

            stale_home = Path(directory) / "stale-home"
            stale_home.mkdir()
            (stale_home / ".codex-global-state.json").write_text(
                json.dumps(
                    {
                        "local-projects": {},
                        "thread-project-assignments": {
                            "thr_stale": {"projectId": "deleted-project"}
                        },
                        "projectless-thread-ids": [],
                    }
                ),
                encoding="utf-8",
            )
            stale_index = load_codex_project_index(stale_home)
            stale = classify_thread_location(
                "thr_stale",
                str(Path(directory) / "old-project"),
                project_index=stale_index,
            )

        self.assertEqual(fallback.conversation_type, "unclassified")
        self.assertEqual(stale.conversation_type, "project")
        self.assertEqual(stale.project_name, "未知项目")
        self.assertEqual(stale.project_key, "project:deleted-project")

    def test_invalid_updated_timestamp_does_not_break_the_catalog(self) -> None:
        records = build_thread_catalog(
            ("thr_large",),
            (
                {"id": "thr_large", "name": "极大值", "updatedAt": 10**10000},
                {"id": "thr_infinite", "name": "无穷值", "updatedAt": float("inf")},
            ),
        )

        self.assertEqual([record.thread_id for record in records], ["thr_large", "thr_infinite"])
        self.assertIsNone(records[0].updated_at)
        self.assertIsNone(records[1].updated_at)


class ThreadManagerSaveTests(unittest.TestCase):
    def test_save_only_changes_thread_ids_and_creates_exact_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = _bootstrap.write_config(Path(directory) / "config.local.json")
            document = json.loads(path.read_text(encoding="utf-8"))
            document["unknown_future_field"] = {"keep": "保持不变"}
            document["notification"]["headers_json"] = "{\"X-Test\":\"${TOKEN}\"}"
            path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
            original = path.read_bytes()
            state = load_thread_manager_state(path, environ={"TOKEN": "secret"})

            result = save_thread_ids(
                path,
                [" thr_two ", "thr_one", "thr_two"],
                expected_digest=state.digest,
                environ={"TOKEN": "secret"},
            )

            saved = json.loads(path.read_text(encoding="utf-8"))
            backup = result.backup_path.read_bytes()
            self.assertEqual(saved["thread_ids"], ["thr_two", "thr_one"])
            self.assertEqual(saved["unknown_future_field"], {"keep": "保持不变"})
            self.assertEqual(saved["notification"]["headers_json"], '{"X-Test":"${TOKEN}"}')
            self.assertNotIn("secret", path.read_text(encoding="utf-8"))
            self.assertEqual(backup, original)

    def test_external_change_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = _bootstrap.write_config(Path(directory) / "config.local.json")
            state = load_thread_manager_state(path)
            document = json.loads(path.read_text(encoding="utf-8"))
            document["external_change"] = True
            path.write_text(json.dumps(document), encoding="utf-8")

            with self.assertRaises(ConfigConflictError):
                save_thread_ids(
                    path,
                    ["thr_new"],
                    expected_digest=state.digest,
                )

            self.assertTrue(json.loads(path.read_text(encoding="utf-8"))["external_change"])
            self.assertEqual(list(Path(directory).glob("*.bak")), [])

    def test_empty_selection_is_rejected_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = _bootstrap.write_config(Path(directory) / "config.local.json")
            original = path.read_bytes()
            state = load_thread_manager_state(path)

            with self.assertRaises(ThreadManagerError):
                save_thread_ids(path, [], expected_digest=state.digest)

            self.assertEqual(path.read_bytes(), original)

    def test_normalizer_rejects_blank_and_deduplicates_exact_ids(self) -> None:
        self.assertEqual(normalize_thread_ids("A,b,A"), ("A", "b"))
        with self.assertRaises(ThreadManagerError):
            normalize_thread_ids("  ")


if __name__ == "__main__":
    unittest.main()
