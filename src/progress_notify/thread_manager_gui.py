"""Tkinter desktop UI for selecting monitored Codex threads."""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk
from typing import Any, Mapping

from .codex_client import CodexAppServerClient
from .thread_manager import (
    DEFAULT_CONFIG_PATH,
    ConfigConflictError,
    ThreadManagerError,
    ThreadManagerState,
    ThreadRecord,
    USER_FACING_THREAD_SOURCE_KINDS,
    build_thread_catalog,
    load_codex_project_index,
    load_thread_manager_state,
    normalize_thread_ids,
    save_thread_ids,
)


WINDOW_TITLE = "Codex 监控会话管理器"


def desktop_environment() -> dict[str, str]:
    """Return process variables plus missing persisted Windows user variables."""

    result = dict(os.environ)
    if os.name != "nt":
        return result
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            index = 0
            while True:
                try:
                    name, value, _kind = winreg.EnumValue(key, index)
                except OSError:
                    break
                index += 1
                if isinstance(name, str) and isinstance(value, str):
                    result.setdefault(name, value)
    except OSError:
        pass
    return result


def _shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _format_updated(value: float | None) -> str:
    if value is None:
        return "—"
    timestamp = value
    while timestamp > 40_000_000_000:
        timestamp /= 1000
    try:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return "—"


class ThreadManagerApp:
    """Thin Tk event layer over the testable thread-manager core."""

    def __init__(
        self,
        root: tk.Tk,
        config_path: Path,
        environ: Mapping[str, str],
    ) -> None:
        self.root = root
        self.config_path = config_path
        self.environ = dict(environ)
        self.state: ThreadManagerState | None = None
        self.selected_ids: list[str] = []
        self.original_ids: tuple[str, ...] = ()
        self.catalog: dict[str, ThreadRecord] = {}
        self.active_payloads: list[Mapping[str, Any]] = []
        self.archived_payloads: list[Mapping[str, Any]] = []
        self.available_iids: dict[str, str] = {}
        self.monitored_iids: dict[str, str] = {}
        self.events: queue.Queue[tuple[str, int, object]] = queue.Queue()
        self.fetch_generation = 0
        self.fetching = False
        self.closed = False
        self.fetch_cancel: threading.Event | None = None
        self.fetch_client: CodexAppServerClient | None = None
        self.fetch_client_lock = threading.Lock()
        self.last_selected_tree: ttk.Treeview | None = None

        self.search_var = tk.StringVar()
        self.status_var = tk.StringVar(value="正在读取配置…")
        self.config_var = tk.StringVar(value=str(config_path))

        self._configure_window()
        self._build_interface()
        self.search_var.trace_add("write", lambda *_args: self._render())
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._poll_events)
        if self._load_configuration(show_error=True):
            self._refresh_threads()

    def _configure_window(self) -> None:
        self.root.title(WINDOW_TITLE)
        self.root.geometry("1120x690")
        self.root.minsize(900, 560)
        self.root.option_add("*Font", ("Microsoft YaHei UI", 9))
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Header.TLabel", font=("Microsoft YaHei UI", 16, "bold"))
        style.configure("Hint.TLabel", foreground="#5b6472")
        style.configure("Status.TLabel", foreground="#344054")

    def _build_interface(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)

        header = ttk.Frame(self.root, padding=(18, 16, 18, 8))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="管理需要接收进度通知的会话", style="Header.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            header,
            text="项目会话按项目名称分组；一次性对话单独归类。保存只修改 thread_ids，并自动备份原配置。",
            style="Hint.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.reload_button = ttk.Button(
            header, text="重新读取配置", command=self._reload_configuration
        )
        self.reload_button.grid(row=0, column=1, rowspan=2, padx=(12, 0))

        toolbar = ttk.Frame(self.root, padding=(18, 4, 18, 10))
        toolbar.grid(row=1, column=0, sticky="ew")
        toolbar.columnconfigure(1, weight=1)
        ttk.Label(toolbar, text="搜索：").grid(row=0, column=0, sticky="w")
        search = ttk.Entry(toolbar, textvariable=self.search_var)
        search.grid(row=0, column=1, sticky="ew", padx=(0, 10))
        search.bind("<Escape>", lambda _event: self.search_var.set(""))
        self.refresh_button = ttk.Button(
            toolbar, text="刷新会话列表", command=self._refresh_threads
        )
        self.refresh_button.grid(row=0, column=2)
        ttk.Button(toolbar, text="手动添加 ID", command=self._manual_add).grid(
            row=0, column=3, padx=(8, 0)
        )

        body = ttk.Frame(self.root, padding=(18, 0, 18, 8))
        body.grid(row=2, column=0, sticky="nsew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(2, weight=1)

        self.available_group = ttk.LabelFrame(body, text="可添加的会话")
        self.available_group.grid(row=0, column=0, sticky="nsew")
        self.available_group.rowconfigure(0, weight=1)
        self.available_group.columnconfigure(0, weight=1)
        self.available_tree = self._make_tree(self.available_group)
        self.available_tree.bind("<Double-1>", self._double_click_add)
        self.available_tree.bind(
            "<<TreeviewSelect>>",
            lambda _event: self._on_tree_selection(self.available_tree),
        )

        controls = ttk.Frame(body, padding=(12, 0))
        controls.grid(row=0, column=1, sticky="ns")
        controls.rowconfigure(0, weight=1)
        controls.rowconfigure(3, weight=1)
        self.add_button = ttk.Button(controls, text="添加  →", command=self._add_selected)
        self.add_button.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        self.remove_button = ttk.Button(
            controls, text="←  删除", command=self._remove_selected
        )
        self.remove_button.grid(row=2, column=0, sticky="ew")

        self.monitored_group = ttk.LabelFrame(body, text="正在监控")
        self.monitored_group.grid(row=0, column=2, sticky="nsew")
        self.monitored_group.rowconfigure(0, weight=1)
        self.monitored_group.columnconfigure(0, weight=1)
        self.monitored_tree = self._make_tree(self.monitored_group)
        self.monitored_tree.bind("<Double-1>", self._double_click_remove)
        self.monitored_tree.bind(
            "<<TreeviewSelect>>",
            lambda _event: self._on_tree_selection(self.monitored_tree),
        )

        footer = ttk.Frame(self.root, padding=(18, 6, 18, 16))
        footer.grid(row=3, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, textvariable=self.status_var, style="Status.TLabel").grid(
            row=0, column=0, columnspan=4, sticky="ew", pady=(0, 7)
        )
        ttk.Label(footer, text="配置：", style="Hint.TLabel").grid(row=1, column=0, sticky="w")
        ttk.Label(footer, textvariable=self.config_var, style="Hint.TLabel").grid(
            row=2, column=0, sticky="w"
        )
        ttk.Button(footer, text="复制所选 ID", command=self._copy_selected_id).grid(
            row=1, column=1, rowspan=2, padx=(8, 0)
        )
        ttk.Button(footer, text="关闭", command=self._on_close).grid(
            row=1, column=2, rowspan=2, padx=(8, 0)
        )
        self.save_button = ttk.Button(footer, text="保存配置", command=self._save)
        self.save_button.grid(row=1, column=3, rowspan=2, padx=(8, 0))
        self._update_buttons()

    def _make_tree(self, parent: ttk.LabelFrame) -> ttk.Treeview:
        columns = ("title", "updated", "state", "id")
        tree = ttk.Treeview(
            parent,
            columns=columns,
            show=("tree", "headings"),
            selectmode="extended",
        )
        tree.heading("#0", text="分类 / 项目")
        tree.heading("title", text="会话名称")
        tree.heading("updated", text="更新时间")
        tree.heading("state", text="状态")
        tree.heading("id", text="完整会话 ID")
        tree.column("#0", width=170, minwidth=120, stretch=True)
        tree.column("title", width=235, minwidth=140, stretch=True)
        tree.column("updated", width=125, minwidth=110, stretch=False)
        tree.column("state", width=95, minwidth=80, stretch=False)
        tree.column("id", width=245, minwidth=160, stretch=True)
        vertical = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
        horizontal = ttk.Scrollbar(parent, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        tree.tag_configure("muted", foreground="#707784")
        tree.tag_configure(
            "group",
            background="#eef2f6",
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        tree.tag_configure(
            "project",
            foreground="#274c77",
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        return tree

    def _load_configuration(self, *, show_error: bool) -> bool:
        try:
            state = load_thread_manager_state(self.config_path, environ=self.environ)
        except ThreadManagerError as exc:
            self.state = None
            self.status_var.set(str(exc))
            self._update_buttons()
            if show_error:
                messagebox.showerror("无法读取配置", str(exc), parent=self.root)
            return False
        self.state = state
        self.config_path = state.path
        self.config_var.set(str(state.path))
        self.selected_ids = list(state.thread_ids)
        self.original_ids = state.thread_ids
        self._rebuild_catalog()
        self.status_var.set(
            f"已读取配置：当前监控 {len(self.selected_ids)} 个会话。正在获取会话名称…"
        )
        self._render()
        return True

    def _rebuild_catalog(self) -> None:
        overrides = self.state.title_overrides if self.state else {}
        project_index = load_codex_project_index(self.environ.get("CODEX_HOME") or None)
        records = build_thread_catalog(
            self.selected_ids,
            self.active_payloads,
            self.archived_payloads,
            title_overrides=overrides,
            project_index=project_index,
        )
        self.catalog = {record.thread_id: record for record in records}

    def _refresh_threads(self) -> None:
        if self.fetching or self.state is None:
            return
        self.fetching = True
        self.fetch_generation += 1
        generation = self.fetch_generation
        cancel = threading.Event()
        self.fetch_cancel = cancel
        command = self.state.codex_command
        timeout = self.state.codex_timeout_seconds
        self.refresh_button.configure(state="disabled")
        self.reload_button.configure(state="disabled")
        self.status_var.set("正在从 Codex 获取会话列表…")

        def worker() -> None:
            client = CodexAppServerClient(command, timeout)
            with self.fetch_client_lock:
                if cancel.is_set():
                    return
                self.fetch_client = client
            try:
                with client:
                    active = client.list_threads(
                        limit=1000,
                        archived=False,
                        source_kinds=USER_FACING_THREAD_SOURCE_KINDS,
                    )
                    if cancel.is_set():
                        return
                    archived = client.list_threads(
                        limit=1000,
                        archived=True,
                        source_kinds=USER_FACING_THREAD_SOURCE_KINDS,
                    )
                if not cancel.is_set():
                    self.events.put(("threads", generation, (active, archived)))
            except Exception as exc:
                if not cancel.is_set():
                    self.events.put(("error", generation, exc))
            finally:
                with self.fetch_client_lock:
                    if self.fetch_client is client:
                        self.fetch_client = None

        threading.Thread(
            target=worker,
            name="progress-notify-thread-manager-refresh",
            daemon=True,
        ).start()

    def _poll_events(self) -> None:
        if self.closed:
            return
        try:
            while True:
                kind, generation, payload = self.events.get_nowait()
                if generation != self.fetch_generation:
                    continue
                self.fetching = False
                self.refresh_button.configure(state="normal")
                self.reload_button.configure(state="normal")
                if kind == "threads":
                    active, archived = payload  # type: ignore[misc]
                    self.active_payloads = list(active)
                    self.archived_payloads = list(archived)
                    self._rebuild_catalog()
                    self._render()
                    project_keys = {
                        record.project_key
                        for record in self.catalog.values()
                        if record.conversation_type == "project" and record.project_key
                    }
                    project_threads = sum(
                        record.conversation_type == "project"
                        for record in self.catalog.values()
                    )
                    one_time_threads = sum(
                        record.conversation_type == "one_time"
                        for record in self.catalog.values()
                    )
                    self.status_var.set(
                        f"已加载 {len(self.catalog)} 个用户会话：{len(project_keys)} 个项目中的 "
                        f"{project_threads} 个项目对话，另有 {one_time_threads} 个一次性对话；"
                        f"右侧监控 {len(self.selected_ids)} 个。"
                    )
                else:
                    error = payload
                    self.status_var.set(
                        "会话列表刷新失败；已监控列表仍然保留，可以继续手动添加或保存。"
                    )
                    messagebox.showwarning(
                        "刷新失败",
                        f"无法读取 Codex 会话列表（{type(error).__name__}）。\n"
                        "当前配置没有被修改。",
                        parent=self.root,
                    )
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def _render(self) -> None:
        selected = set(self.selected_ids)
        query = " ".join(self.search_var.get().casefold().split())
        available_records = [
            record
            for record in self.catalog.values()
            if record.thread_id not in selected
            and (
                not query
                or query in record.thread_id.casefold()
                or query in record.title.casefold()
                or query in record.preview.casefold()
            )
        ]

        self.available_tree.delete(*self.available_tree.get_children())
        self.available_iids.clear()
        self._insert_grouped_records(
            self.available_tree,
            self.available_iids,
            available_records,
            prefix="available",
            monitored=False,
        )

        self.monitored_tree.delete(*self.monitored_tree.get_children())
        self.monitored_iids.clear()
        monitored_records = [
            self.catalog.get(thread_id)
            or ThreadRecord(thread_id=thread_id, title="未命名会话", available=False)
            for thread_id in self.selected_ids
        ]
        self._insert_grouped_records(
            self.monitored_tree,
            self.monitored_iids,
            monitored_records,
            prefix="monitored",
            monitored=True,
        )

        self.available_group.configure(text=f"可添加的会话（{len(available_records)}）")
        self.monitored_group.configure(text=f"正在监控（{len(self.selected_ids)}）")
        self._update_buttons()
        self._update_title()

    def _insert_grouped_records(
        self,
        tree: ttk.Treeview,
        iid_map: dict[str, str],
        records: list[ThreadRecord],
        *,
        prefix: str,
        monitored: bool,
    ) -> None:
        project_groups: dict[str, list[ThreadRecord]] = {}
        one_time: list[ThreadRecord] = []
        unclassified: list[ThreadRecord] = []
        unknown: list[ThreadRecord] = []
        for record in records:
            if record.conversation_type == "project" and (
                record.project_key or record.project_path
            ):
                key = record.project_key or os.path.normcase(record.project_path)
                project_groups.setdefault(key, []).append(record)
            elif record.conversation_type == "one_time":
                one_time.append(record)
            elif record.conversation_type == "unclassified":
                unclassified.append(record)
            else:
                unknown.append(record)

        row_index = 0

        def insert_record(parent: str, record: ThreadRecord) -> None:
            nonlocal row_index
            iid = f"{prefix}-thread-{row_index}"
            row_index += 1
            iid_map[iid] = record.thread_id
            if monitored:
                if not record.available:
                    state = "本地未找到"
                elif record.archived:
                    state = "已归档"
                else:
                    state = "监控中"
            else:
                state = "已归档" if record.archived else "可添加"
            tags = ("muted",) if record.archived or not record.available else ()
            tree.insert(
                parent,
                "end",
                iid=iid,
                text="",
                values=(
                    _shorten(record.title, 100),
                    _format_updated(record.updated_at),
                    state,
                    record.thread_id,
                ),
                tags=tags,
            )

        if project_groups:
            count = sum(len(group) for group in project_groups.values())
            root_iid = f"{prefix}-group-projects"
            tree.insert(
                "",
                "end",
                iid=root_iid,
                text=f"项目对话（{count}）",
                open=True,
                tags=("group",),
            )
            name_counts: dict[str, int] = {}
            for group in project_groups.values():
                name = (group[0].project_name or "未命名项目").casefold()
                name_counts[name] = name_counts.get(name, 0) + 1
            ordered_groups = sorted(
                project_groups.items(),
                key=lambda item: (
                    (item[1][0].project_name or "未命名项目").casefold(),
                    item[0].casefold(),
                ),
            )
            for project_index, (_project_key, group) in enumerate(ordered_groups):
                name = group[0].project_name or "未命名项目"
                project_path = group[0].project_path
                label = name
                if name_counts[name.casefold()] > 1:
                    label = f"{name} — {Path(project_path).parent.name}"
                project_iid = f"{prefix}-project-{project_index}"
                tree.insert(
                    root_iid,
                    "end",
                    iid=project_iid,
                    text=f"{label}（{len(group)}）",
                    open=True,
                    tags=("project",),
                )
                for record in group:
                    insert_record(project_iid, record)

        if one_time:
            one_time_iid = f"{prefix}-group-one-time"
            tree.insert(
                "",
                "end",
                iid=one_time_iid,
                text=f"一次性对话（{len(one_time)}）",
                open=True,
                tags=("group",),
            )
            for record in one_time:
                insert_record(one_time_iid, record)

        if unclassified:
            unclassified_iid = f"{prefix}-group-unclassified"
            tree.insert(
                "",
                "end",
                iid=unclassified_iid,
                text=f"未归类工作区（{len(unclassified)}）",
                open=True,
                tags=("group",),
            )
            for record in unclassified:
                insert_record(unclassified_iid, record)

        if unknown:
            unknown_iid = f"{prefix}-group-unknown"
            tree.insert(
                "",
                "end",
                iid=unknown_iid,
                text=f"来源未识别（{len(unknown)}）",
                open=True,
                tags=("group",),
            )
            for record in unknown:
                insert_record(unknown_iid, record)

    def _update_buttons(self) -> None:
        can_add = (
            any(iid in self.available_iids for iid in self.available_tree.selection())
            if hasattr(self, "available_tree")
            else False
        )
        can_remove = (
            any(iid in self.monitored_iids for iid in self.monitored_tree.selection())
            if hasattr(self, "monitored_tree")
            else False
        )
        if hasattr(self, "add_button"):
            self.add_button.configure(state="normal" if can_add else "disabled")
            self.remove_button.configure(state="normal" if can_remove else "disabled")
            self.save_button.configure(state="normal" if self.state else "disabled")

    def _on_tree_selection(self, tree: ttk.Treeview) -> None:
        self.last_selected_tree = tree
        self._update_buttons()

    def _is_dirty(self) -> bool:
        return set(self.selected_ids) != set(self.original_ids)

    def _update_title(self) -> None:
        self.root.title(("* " if self._is_dirty() else "") + WINDOW_TITLE)

    def _add_selected(self) -> None:
        additions = [
            self.available_iids[iid]
            for iid in self.available_tree.selection()
            if iid in self.available_iids
        ]
        selected = set(self.selected_ids)
        for thread_id in additions:
            if thread_id not in selected:
                self.selected_ids.append(thread_id)
                selected.add(thread_id)
        if additions:
            self.status_var.set("选择已更新；点击“保存配置”后生效。")
        self._render()

    def _remove_selected(self) -> None:
        removals = {
            self.monitored_iids[iid]
            for iid in self.monitored_tree.selection()
            if iid in self.monitored_iids
        }
        if removals:
            self.selected_ids = [item for item in self.selected_ids if item not in removals]
            self.status_var.set("选择已更新；点击“保存配置”后生效。")
        self._render()

    def _double_click_add(self, event: tk.Event[tk.Misc]) -> None:
        iid = self.available_tree.identify_row(event.y)
        if iid in self.available_iids:
            self.available_tree.selection_set(iid)
            self._add_selected()

    def _double_click_remove(self, event: tk.Event[tk.Misc]) -> None:
        iid = self.monitored_tree.identify_row(event.y)
        if iid in self.monitored_iids:
            self.monitored_tree.selection_set(iid)
            self._remove_selected()

    def _manual_add(self) -> None:
        raw = simpledialog.askstring(
            "手动添加会话 ID",
            "粘贴一个或多个完整会话 ID。多个 ID 可用逗号或换行分隔：",
            parent=self.root,
        )
        if raw is None:
            return
        try:
            additions = normalize_thread_ids(raw.replace("\r", "\n").replace("\n", ","))
        except ThreadManagerError as exc:
            messagebox.showerror("ID 无效", str(exc), parent=self.root)
            return
        selected = set(self.selected_ids)
        for thread_id in additions:
            if thread_id not in self.catalog:
                self.catalog[thread_id] = ThreadRecord(
                    thread_id=thread_id,
                    title="手动添加的会话",
                    available=False,
                )
            if thread_id not in selected:
                self.selected_ids.append(thread_id)
                selected.add(thread_id)
        self.status_var.set("已手动加入会话；点击“保存配置”后生效。")
        self._render()

    def _copy_selected_id(self) -> None:
        tree = self.last_selected_tree or self.monitored_tree
        mapping = self.monitored_iids if tree is self.monitored_tree else self.available_iids
        ids = [mapping[iid] for iid in tree.selection() if iid in mapping]
        if not ids:
            messagebox.showinfo("复制会话 ID", "请先在任一列表中选择会话。", parent=self.root)
            return
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(ids))
        self.status_var.set(f"已复制 {len(ids)} 个完整会话 ID。")

    def _save(self) -> None:
        if self.state is None:
            return
        try:
            result = save_thread_ids(
                self.state.path,
                self.selected_ids,
                expected_digest=self.state.digest,
                environ=self.environ,
            )
            state = load_thread_manager_state(result.path, environ=self.environ)
        except ConfigConflictError as exc:
            messagebox.showwarning("配置已变化", str(exc), parent=self.root)
            self.status_var.set(str(exc))
            return
        except ThreadManagerError as exc:
            messagebox.showerror("保存失败", str(exc), parent=self.root)
            self.status_var.set(str(exc))
            return

        self.state = state
        self.selected_ids = list(state.thread_ids)
        self.original_ids = state.thread_ids
        self._rebuild_catalog()
        self._render()
        self.status_var.set(
            f"保存成功：正在监控 {len(state.thread_ids)} 个会话；之后完成的轮次立即按新列表判断。"
        )
        messagebox.showinfo(
            "保存成功",
            f"正在监控 {len(state.thread_ids)} 个会话。\n\n"
            f"原配置备份：\n{result.backup_path}\n\n"
            "无需重启监测隧道。",
            parent=self.root,
        )

    def _reload_configuration(self) -> None:
        if self._is_dirty() and not messagebox.askyesno(
            "放弃未保存更改？",
            "重新读取会放弃当前尚未保存的添加或删除，是否继续？",
            parent=self.root,
        ):
            return
        self._cancel_refresh()
        if self._load_configuration(show_error=True):
            self._refresh_threads()

    def _cancel_refresh(self) -> None:
        self.fetch_generation += 1
        if self.fetch_cancel is not None:
            self.fetch_cancel.set()
        with self.fetch_client_lock:
            client = self.fetch_client
        if client is not None:
            client.close()
        self.fetching = False
        if not self.closed:
            self.refresh_button.configure(state="normal")
            self.reload_button.configure(state="normal")

    def _on_close(self) -> None:
        if self._is_dirty() and not messagebox.askyesno(
            "尚未保存",
            "当前添加或删除尚未保存，仍要关闭吗？",
            parent=self.root,
        ):
            return
        self.closed = True
        self._cancel_refresh()
        self.root.destroy()


def main(config_path: str | os.PathLike[str] | None = None) -> int:
    environ = desktop_environment()
    selected = config_path or environ.get("PROGRESS_NOTIFY_CONFIG") or DEFAULT_CONFIG_PATH
    root = tk.Tk()

    def report_callback_exception(
        exception_type: type[BaseException],
        _exception: BaseException,
        _traceback: object,
    ) -> None:
        messagebox.showerror(
            "界面发生错误",
            f"操作未完成（{exception_type.__name__}）。配置文件不会被静默覆盖。",
            parent=root,
        )

    root.report_callback_exception = report_callback_exception  # type: ignore[method-assign]
    ThreadManagerApp(root, Path(selected).expanduser().resolve(), environ)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
