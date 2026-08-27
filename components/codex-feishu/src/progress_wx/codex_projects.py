"""Codex Desktop 本地项目清单的最小、可回滚登记器。"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Callable, Mapping


class ProjectRegistryError(RuntimeError):
    """Codex 项目状态无法在不覆盖并发更新的前提下安全修改。"""


@dataclass(frozen=True, slots=True)
class LocalProject:
    project_id: str
    name: str
    root_paths: tuple[str, ...]
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class ProjectRegistrySnapshot:
    projects: tuple[LocalProject, ...]
    thread_assignments: Mapping[str, str]
    projectless_thread_ids: frozenset[str]


_INVALID_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def _safe_directory_name(name: str) -> str:
    value = _INVALID_PATH_CHARS.sub("_", name).rstrip(" .")
    if not value:
        value = "Codex项目"
    if value.upper() in _RESERVED_NAMES:
        value = f"_{value}"
    return value[:100].rstrip(" .") or "Codex项目"


def _load_state(path: Path) -> tuple[bytes, dict[str, Any]]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProjectRegistryError("无法读取 Codex 全局项目状态") from exc
    if not isinstance(payload, dict):
        raise ProjectRegistryError("Codex 全局项目状态根节点不是对象")
    return raw, payload


def _project_from_raw(project_id: str, value: Any) -> LocalProject | None:
    if not isinstance(value, dict):
        return None
    name = str(value.get("name") or "").strip()
    raw_paths = value.get("rootPaths")
    paths = tuple(
        str(item).strip()
        for item in raw_paths
        if str(item).strip()
    ) if isinstance(raw_paths, list) else ()
    if not project_id or not name or not paths:
        return None
    try:
        created = int(value.get("createdAt") or 0)
        updated = int(value.get("updatedAt") or created)
    except (TypeError, ValueError):
        created = updated = 0
    return LocalProject(project_id, name, paths, created, updated)


class CodexProjectRegistry:
    """读取项目状态，并通过 Codex 官方 Windows ``--open-project`` 注册。"""

    def __init__(
        self,
        state_file: Path,
        managed_root: Path,
        *,
        opener: Callable[[Path], None] | None = None,
        recognition_timeout: float = 10.0,
    ) -> None:
        self.state_file = state_file.expanduser().resolve()
        self.managed_root = managed_root.expanduser().resolve()
        self._opener = opener or self._open_with_codex
        self.recognition_timeout = float(recognition_timeout)
        if self.recognition_timeout <= 0:
            raise ValueError("recognition_timeout 必须大于 0")

    def snapshot(self) -> ProjectRegistrySnapshot:
        _raw, state = _load_state(self.state_file)
        raw_projects = state.get("local-projects")
        projects_by_id = raw_projects if isinstance(raw_projects, dict) else {}
        raw_order = state.get("project-order")
        order = [str(item) for item in raw_order] if isinstance(raw_order, list) else []
        seen: set[str] = set()
        projects: list[LocalProject] = []
        for project_id in (*order, *projects_by_id.keys()):
            project_id = str(project_id)
            if project_id in seen:
                continue
            seen.add(project_id)
            project = _project_from_raw(project_id, projects_by_id.get(project_id))
            if project is not None:
                projects.append(project)
        assignments: dict[str, str] = {}
        raw_assignments = state.get("thread-project-assignments")
        if isinstance(raw_assignments, dict):
            for thread_id, value in raw_assignments.items():
                if not isinstance(value, dict) or value.get("projectKind") != "local":
                    continue
                project_id = str(value.get("projectId") or "").strip()
                if project_id in projects_by_id:
                    assignments[str(thread_id)] = project_id
        raw_projectless = state.get("projectless-thread-ids")
        projectless = frozenset(
            str(item) for item in raw_projectless
        ) if isinstance(raw_projectless, list) else frozenset()
        return ProjectRegistrySnapshot(tuple(projects), assignments, projectless)

    def register(self, name: str) -> LocalProject:
        """创建同名目录并交给运行中的 Codex 注册；同名项目不重复创建。"""

        exact_name = str(name or "").strip()
        if not exact_name or len(exact_name) > 120 or any(ord(ch) < 32 for ch in exact_name):
            raise ProjectRegistryError("项目名称必须是 1 到 120 个可见字符")
        if _safe_directory_name(exact_name) != exact_name:
            raise ProjectRegistryError(
                "项目名称必须同时是合法的 Windows 文件夹名，不能包含 < > : \" / \\ | ? *，也不能以空格或句点结尾"
            )
        before = self.snapshot()
        for project in before.projects:
            if project.name == exact_name:
                return project
        root = self.managed_root / exact_name
        if root.exists():
            raise ProjectRegistryError("同名项目目录已存在，但尚未登记为 Codex 项目")
        try:
            root.mkdir(parents=True, exist_ok=False)
            self._opener(root)
            deadline = time.monotonic() + self.recognition_timeout
            expected = str(root)
            while time.monotonic() < deadline:
                for project in self.snapshot().projects:
                    if project.name == exact_name and expected in project.root_paths:
                        return project
                time.sleep(0.2)
        except BaseException:
            try:
                root.rmdir()
            except OSError:
                pass
            raise
        try:
            root.rmdir()
        except OSError:
            pass
        raise ProjectRegistryError("Codex Desktop 没有确认新项目，请确认桌面端正在运行")

    @staticmethod
    def _open_with_codex(root: Path) -> None:
        if os.name != "nt":
            raise ProjectRegistryError("Codex --open-project 当前只支持 Windows Desktop")
        try:
            import winreg

            key_path = r"Software\Classes\Directory\shell\OpenProjectInCodex\command"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                command = str(winreg.QueryValueEx(key, "")[0])
        except (OSError, ValueError) as exc:
            raise ProjectRegistryError("找不到 Codex 官方 OpenProjectInCodex 注册入口") from exc
        match = re.match(r'^"([^"]+\.exe)"\s+--open-project\s+"?%1"?$', command, re.I)
        if match is None:
            raise ProjectRegistryError("Codex OpenProjectInCodex 注册命令格式异常")
        executable = Path(match.group(1)).resolve()
        if not executable.is_file():
            raise ProjectRegistryError("Codex Desktop 主程序不存在")
        try:
            subprocess.Popen(
                [str(executable), "--open-project", str(root)],
                cwd=str(executable.parent),
                close_fds=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            raise ProjectRegistryError("无法调用 Codex --open-project") from exc


__all__ = [
    "CodexProjectRegistry",
    "LocalProject",
    "ProjectRegistryError",
    "ProjectRegistrySnapshot",
]
