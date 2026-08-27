"""安装、诊断、运行和一键启停命令。"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import importlib.util
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Sequence

import yaml

from .codex_app_tools import DesktopAppToolsClient
from .approval_bridge import ApprovalBridge, permission_hook_result
from .codex_gateway import (
    CodexGatewayError,
    active_shared_websocket_url,
    authorize_gateway_launch,
    gateway_healthy,
    recover_owned_gateway_launch,
    register_shared_desktop,
    request_gateway_stop,
    run_gateway,
    shared_desktop_running,
    verified_gateway_state,
    verified_gateway_running,
)
from .codex_rpc import CodexAppServer, validate_loopback_websocket_url
from .codex_store import CodexStore, StorePaths
from .config import DEFAULT_CONFIG_PATH, PROJECT_ROOT, ConfigError, load_config
from .feishu import FeishuMessageChannel, discover_feishu_open_id
from .installer import install_notify, uninstall_notify
from .hooks_installer import install_permission_hook, uninstall_permission_hook
from .logging_utils import configure_logging
from .process_control import (
    InstanceError,
    acquire_instance,
    clear_stop_request,
    instance_running,
    read_pid_file,
    release_instance,
    request_stop,
    stop_requested_for,
)
from .retry import RetryPolicy, call_with_retry
from .service import ProgressService, snapshot_to_event
from .secrets import DpapiSecretStore
from .state import CorrelationCodec, StateStore
from .uia_probe import probe_tool_window
from .usage import USAGE_IMAGE_FOOTER, feishu_usage_images
from .wechat import WechatService, WxAutoX4Adapter


def _configure_console() -> None:
    """Windows 默认代码页不能表示全部 Unicode，统一为 UTF-8 且不因单字崩溃。"""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def _cell(value: object, limit: int = 160) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _desktop_project_assignments(codex_home: str | os.PathLike[str]) -> dict[str, tuple[str, str]]:
    """读取 Codex Desktop 明确保存的任务项目分配；绝不从工作目录推断项目。"""

    state_path = Path(codex_home).expanduser() / ".codex-global-state.json"
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    projects = payload.get("local-projects")
    assignments = payload.get("thread-project-assignments")
    if not isinstance(projects, dict) or not isinstance(assignments, dict):
        return {}
    result: dict[str, tuple[str, str]] = {}
    for thread_id, assignment in assignments.items():
        if not isinstance(thread_id, str) or not isinstance(assignment, dict):
            continue
        if assignment.get("projectKind") != "local":
            continue
        project_id = assignment.get("projectId")
        project = projects.get(project_id) if isinstance(project_id, str) else None
        project_name = project.get("name") if isinstance(project, dict) else None
        if isinstance(project_id, str) and isinstance(project_name, str) and project_name.strip():
            result[thread_id] = (project_id, project_name.strip())
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="progress-wx", description="Codex 进度通知服务")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="config.yaml 路径")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate", help="校验配置和本地数据库")
    threads = sub.add_parser("list-threads", help="列出可精确复制的 Codex 对话标识")
    threads.add_argument("--json", action="store_true")
    configure_monitor = sub.add_parser(
        "configure-monitor", help="从本机 Codex 对话列表选择一个或多个精确监控 ID"
    )
    configure_monitor.add_argument(
        "--force", action="store_true", help="即使已有有效选择器也重新选择"
    )
    monitor_list = sub.add_parser("monitor-list", help="列出统一监测注册表")
    monitor_list.add_argument("--json", action="store_true", help="只向 stdout 输出 JSON")
    monitor_add = sub.add_parser("monitor-add", help="永久手动监测一个精确 Codex 任务")
    monitor_add.add_argument("--thread-id", required=True)
    monitor_add.add_argument("--json", action="store_true", help="只向 stdout 输出 JSON")
    monitor_remove = sub.add_parser("monitor-remove", help="明确移除并抑制自动恢复")
    monitor_remove.add_argument("--thread-id", required=True)
    monitor_remove.add_argument("--json", action="store_true", help="只向 stdout 输出 JSON")
    monitor_settings = sub.add_parser(
        "monitor-settings", help="查询或设置自动监测全局开关"
    )
    monitor_settings.add_argument(
        "--auto-enabled",
        choices=("true", "false"),
        help="设置自动监测；省略时只读查询",
    )
    monitor_settings.add_argument(
        "--json", action="store_true", help="只向 stdout 输出 JSON"
    )
    sub.add_parser("install-notify", help="安全安装 Codex notify 包装器")
    sub.add_parser("install-permission-hook", help="安装用户全局飞书审批 Hook")
    sub.add_parser("uninstall-permission-hook", help="只移除本工具的全局飞书审批 Hook")
    sub.add_parser("permission-hook", help=argparse.SUPPRESS)
    sub.add_parser("uninstall-notify", help="恢复安装前的 Codex notify")
    sub.add_parser("run", help="前台运行服务")
    sub.add_parser("start", help="在当前用户交互会话后台启动")
    stop = sub.add_parser("stop", help="请求服务正常停止，不强杀进程")
    stop.add_argument("--timeout", type=float, default=30)
    sub.add_parser("status", help="显示运行状态和队列统计")
    gateway_run = sub.add_parser("gateway-run", help=argparse.SUPPRESS)
    gateway_run.add_argument("--launch-token", required=True, help=argparse.SUPPRESS)
    gateway_start = sub.add_parser("gateway-start", help=argparse.SUPPRESS)
    gateway_start.add_argument("--launch-token", required=True, help=argparse.SUPPRESS)
    gateway_stop = sub.add_parser(
        "gateway-stop", help="在 Desktop 退出后协作停止共享 app-server"
    )
    gateway_stop.add_argument("--expected-pid", type=int, help=argparse.SUPPRESS)
    gateway_stop.add_argument(
        "--expected-creation-time", type=int, help=argparse.SUPPRESS
    )
    gateway_stop.add_argument("--expected-launch-token", help=argparse.SUPPRESS)
    gateway_status = sub.add_parser("gateway-status", help="只读显示共享 Codex 状态")
    gateway_status.add_argument("--expected-pid", type=int, help=argparse.SUPPRESS)
    gateway_status.add_argument(
        "--expected-creation-time", type=int, help=argparse.SUPPRESS
    )
    gateway_status.add_argument("--expected-launch-token", help=argparse.SUPPRESS)
    gateway_recover = sub.add_parser("gateway-recover-owned", help=argparse.SUPPRESS)
    gateway_recover.add_argument(
        "--expected-launch-token", required=True, help=argparse.SUPPRESS
    )
    gateway_recover.add_argument(
        "--expected-pid-file", required=True, help=argparse.SUPPRESS
    )
    gateway_recover.add_argument(
        "--expected-state-file", required=True, help=argparse.SUPPRESS
    )
    gateway_recover.add_argument(
        "--expected-websocket-url", required=True, help=argparse.SUPPRESS
    )
    register_desktop = sub.add_parser("register-shared-desktop", help=argparse.SUPPRESS)
    register_desktop.add_argument("--pid", type=int, required=True)
    register_desktop.add_argument(
        "--install-location",
        type=Path,
        required=True,
        help="Get-AppxPackage 返回的当前 Codex 包安装目录",
    )
    register_desktop.add_argument(
        "--not-before-filetime",
        type=int,
        required=True,
        help="AppsFolder 激活前记录的 Windows UTC FILETIME",
    )
    register_desktop.add_argument("--expected-gateway-pid", type=int, required=True)
    register_desktop.add_argument(
        "--expected-gateway-creation-time", type=int, required=True
    )
    register_desktop.add_argument("--expected-gateway-launch-token")
    baseline = sub.add_parser(
        "baseline-pre-activation-hooks",
        help="首次生产启用前，忽略停用期 notify 与最新结构化终态",
    )
    baseline.add_argument(
        "--expected-count",
        type=int,
        required=True,
        help="刚刚由 status 只读观察到的 pending_hook_events 数量",
    )
    discard_replies = sub.add_parser(
        "discard-stale-pending-replies",
        help="停机维护时按精确数量与年龄丢弃陈旧普通回复",
    )
    discard_replies.add_argument("--expected-count", type=int, required=True)
    discard_replies.add_argument("--older-than-seconds", type=int, required=True)
    resolve = sub.add_parser("resolve-uncertain", help="人工解决结果未知的远程回复")
    resolve.add_argument("code", help="原通知第一行的完整 PCWX 编号")
    resolve.add_argument(
        "outcome",
        choices=("delivered", "not-delivered"),
        help="已确认投递，或已确认未投递并允许重新排队",
    )
    sub.add_parser("doctor", help="检查 app-server 和消息渠道依赖，不发送消息")
    configure_feishu = sub.add_parser(
        "configure-feishu", help="安全保存飞书 App ID/Secret，不在命令行暴露 Secret"
    )
    configure_feishu.add_argument("--app-id", help="飞书自建应用的 cli_ App ID")
    configure_feishu.add_argument(
        "--secret-stdin",
        action="store_true",
        help="从标准输入读取一行 Secret；默认使用隐藏输入",
    )
    pair_feishu = sub.add_parser(
        "pair-feishu", help="等待手机发送一次性绑定码并写入唯一 open_id 白名单"
    )
    pair_feishu.add_argument("--timeout", type=float, default=180)
    test_feishu = sub.add_parser("test-feishu", help="向已绑定用户发送飞书测试消息")
    test_feishu.add_argument("--text", default="进度通知：飞书发送链路测试成功。")
    test_feishu.add_argument(
        "--usage-guide",
        action="store_true",
        help="发送六页可直接预览的课堂图片及文字版提示",
    )
    free_probe = sub.add_parser("probe-free-wechat", help="只读探测指定小号的免费 UIA 能力")
    free_probe.add_argument(
        "--nickname",
        help="工具小号当前精确昵称；省略时从 config.yaml 安全读取",
    )
    free_probe.add_argument(
        "--single-visible-window",
        action="store_true",
        help="精确标题不可用时，仅核验唯一已展开的微信主窗口",
    )
    free_probe.add_argument(
        "--diagnostic-unverified-identity",
        action="store_true",
        help="仅在用户确认单账号时读取非内容结构；不能作为生产身份凭据",
    )
    sub.add_parser("verify-wechat", help="核验工具小号与白名单好友，不监听、不发送")
    test_wechat = sub.add_parser("test-wechat", help="向已配置好友真实发送一条测试消息")
    test_wechat.add_argument("--text", default="进度通知：微信发送链路测试成功。")
    return parser


def _config(args: argparse.Namespace, *, ready: bool = True):
    config = load_config(args.config)
    if ready:
        config.validate_ready()
    return config


def _update_yaml_scalar(path: Path, section: str, key: str, value: str) -> None:
    """只替换指定 YAML 二级标量，保留其余注释、顺序和用户配置。"""

    text = path.read_text(encoding="utf-8-sig")
    lines = text.splitlines(keepends=True)
    section_pattern = re.compile(rf"^{re.escape(section)}:\s*(?:#.*)?$")
    key_pattern = re.compile(rf"^(\s{{2}}{re.escape(key)}\s*:)\s*.*?(\r?\n)?$")
    in_section = False
    matches: list[int] = []
    for index, line in enumerate(lines):
        body = line.rstrip("\r\n")
        if body and not body[0].isspace():
            in_section = bool(section_pattern.fullmatch(body))
            continue
        if in_section and key_pattern.fullmatch(line):
            matches.append(index)
    if len(matches) != 1:
        raise ConfigError(f"无法唯一定位 {section}.{key}，拒绝改写 config.yaml")
    index = matches[0]
    newline = "\r\n" if lines[index].endswith("\r\n") else "\n"
    lines[index] = f"  {key}: {json.dumps(value, ensure_ascii=False)}{newline}"
    payload = "".join(lines)
    parsed = yaml.safe_load(payload)
    if not isinstance(parsed, dict) or parsed.get(section, {}).get(key) != value:
        raise ConfigError(f"更新后的 {section}.{key} 校验失败")
    descriptor, temporary = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _update_yaml_sequences(
    path: Path, section: str, updates: dict[str, list[str]]
) -> None:
    """在一次原子替换中更新多个 YAML 列表，避免热加载观察到中间态。"""

    lines = path.read_text(encoding="utf-8-sig").splitlines(keepends=True)
    section_pattern = re.compile(rf"^{re.escape(section)}:\s*(?:#.*)?$")
    for key, values in updates.items():
        key_pattern = re.compile(rf"^\s{{2}}{re.escape(key)}\s*:\s*.*?(?:\r?\n)?$")
        in_section = False
        matches: list[int] = []
        for index, line in enumerate(lines):
            body = line.rstrip("\r\n")
            if body and not body[0].isspace():
                in_section = bool(section_pattern.fullmatch(body))
                continue
            if in_section and key_pattern.fullmatch(line):
                matches.append(index)
        if len(matches) != 1:
            raise ConfigError(f"无法唯一定位 {section}.{key}，拒绝改写 config.yaml")
        start = matches[0]
        end = start + 1
        while end < len(lines):
            body = lines[end].rstrip("\r\n")
            if body and not body.startswith("    "):
                break
            end += 1
        newline = "\r\n" if lines[start].endswith("\r\n") else "\n"
        if values:
            replacement = [f"  {key}:{newline}"] + [
                f"    - {json.dumps(str(value), ensure_ascii=False)}{newline}"
                for value in values
            ]
        else:
            replacement = [f"  {key}: []{newline}"]
        lines = lines[:start] + replacement + lines[end:]
    payload = "".join(lines)
    parsed = yaml.safe_load(payload)
    if not isinstance(parsed, dict) or any(
        parsed.get(section, {}).get(key) != values for key, values in updates.items()
    ):
        raise ConfigError(f"更新后的 {section} 监控选择器校验失败")
    descriptor, temporary = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _require_service_backend(config) -> None:
    """在创建 PID、日志或后台进程前拒绝不完整的生产后端。"""

    messaging = getattr(config, "messaging", config.wechat)
    if messaging.backend == "feishu":
        if importlib.util.find_spec("lark_channel") is None:
            raise ConfigError("当前专用 Python 尚未安装 lark-channel-sdk")
        if not config.feishu.app_secret_file.is_file():
            raise ConfigError("飞书 App Secret 尚未安全保存")
        return
    if messaging.backend == "probe_only":
        raise ConfigError(
            "微信后端处于 probe_only：只允许只读探针，禁止启动后台服务"
        )
    if messaging.backend != "wxautox4":
        raise ConfigError("fake 消息后端只能由自动测试显式注入")


def _validate(args: argparse.Namespace) -> int:
    # “validate”是只读诊断入口；未配置凭证时应给出完整清单，而不是在第一项
    # 上抛栈退出。真正启动仍会再次执行严格的 validate_ready。
    config = _config(args, ready=False)
    paths = StorePaths.from_codex_home(config.codex.home)
    errors = []
    try:
        config.validate_ready()
    except ConfigError as exc:
        errors.append(str(exc))
    if not paths.state_db.is_file():
        errors.append(f"缺少 {paths.state_db}")
    if not paths.history_db.is_file():
        errors.append(f"缺少 {paths.history_db}")
    if config.messaging.backend == "feishu" and importlib.util.find_spec("lark_channel") is None:
        errors.append("当前 Python 未安装 lark-channel-sdk")
    if config.messaging.backend == "wxautox4" and importlib.util.find_spec("wxautox4") is None:
        errors.append("当前 Python 未安装 wxautox4（核心代码可测试，但真实微信不可用）")
    if config.messaging.backend == "probe_only":
        if importlib.util.find_spec("uiautomation") is None:
            errors.append("当前 Python 未安装免费只读探针依赖 uiautomation")
        errors.append("微信后端处于 probe_only：服务被安全禁用，仅允许只读能力探针")
    if errors:
        print("配置已解析，但尚未就绪：")
        for error in errors:
            print(f"- {error}")
        return 2
    print("配置、Codex 数据库和消息渠道依赖均已就绪。")
    return 0


def _list_threads(args: argparse.Namespace) -> int:
    config = _config(args, ready=False)
    store = CodexStore(paths=StorePaths.from_codex_home(config.codex.home))
    records = store.select_threads(include_archived=True)
    projects = _desktop_project_assignments(config.codex.home)
    if args.json:
        print(json.dumps([
            {
                "id": item.thread_id,
                "title": item.title,
                "cwd": item.cwd,
                "archived": item.archived,
                "updated_at_ms": item.updated_at_ms,
                "thread_source": item.thread_source,
                "project_id": projects.get(item.thread_id, (None, None))[0],
                "project_name": projects.get(item.thread_id, (None, None))[1],
            }
            for item in records
        ], ensure_ascii=False, indent=2))
    else:
        print("ID\t标题\t归属\t工作目录\t已归档")
        for item in records:
            project_name = projects.get(item.thread_id, (None, "个人对话"))[1]
            print(
                f"{item.thread_id}\t{_cell(item.title)}\t{_cell(project_name)}\t"
                f"{_cell(item.cwd, 260)}\t{item.archived}"
            )
    return 0


def _configure_monitor(args: argparse.Namespace) -> int:
    """用序号选择结构化 thread ID；不接受标题模糊搜索。"""

    config = _config(args, ready=False)
    selectors = config.codex.selectors
    configured_values = (*selectors.ids, *selectors.titles, *selectors.paths)
    has_placeholder = any(value.startswith("请替换") for value in configured_values)
    if selectors.configured() and not has_placeholder and not args.force:
        print("已保留现有精确监控选择器；如需重选请使用 configure-monitor --force。")
        return 0

    store = CodexStore(paths=StorePaths.from_codex_home(config.codex.home))
    records = store.select_threads(include_archived=False)[:50]
    store.require_readable("配置 Codex 监控对象")
    if not records:
        raise ConfigError("当前 Windows 用户下没有可选择的未归档 Codex 对话")
    print("请选择要监控的 Codex 对话（可输入多个序号，用逗号分隔）：")
    for index, record in enumerate(records, 1):
        print(f"{index:>2}. {_cell(record.title or record.thread_id, 80)} | {_cell(record.cwd, 120)}")
    answer = input("序号（例如 1 或 1,3；直接回车选择 1）：").strip() or "1"
    tokens = [item for item in re.split(r"[,，\s]+", answer) if item]
    if not tokens or any(not token.isdigit() for token in tokens):
        raise ConfigError("监控对象只能输入列表中的数字序号")
    indices = [int(token) for token in tokens]
    if any(index < 1 or index > len(records) for index in indices):
        raise ConfigError("监控对象序号超出当前列表范围")
    selected_ids: list[str] = []
    for index in indices:
        thread_id = records[index - 1].thread_id
        if thread_id not in selected_ids:
            selected_ids.append(thread_id)
    _update_yaml_sequences(
        config.path,
        "monitor",
        {"ids": selected_ids, "titles": [], "paths": []},
    )
    print(f"已写入 {len(selected_ids)} 个精确 Codex 对话 ID。")
    return 0


def _monitor_runtime(args: argparse.Namespace):
    config = _config(args, ready=False)
    state = StateStore(config.service.database)
    codex = CodexStore(paths=StorePaths.from_codex_home(config.codex.home))
    records = codex.select_threads(include_archived=True)
    codex.require_readable("读取 Codex 监测目录")
    top_level = {
        item.thread_id: item for item in records if item.thread_source != "subagent"
    }
    return config, state, codex, top_level


def _monitor_list(args: argparse.Namespace) -> int:
    config, state, _codex, records = _monitor_runtime(args)
    try:
        projects = _desktop_project_assignments(config.codex.home)
        items = []
        for subscription in state.monitor_subscriptions():
            thread_id = str(subscription["thread_id"])
            record = records.get(thread_id)
            project = projects.get(thread_id)
            group = project[1] if project is not None else "个人会话"
            title = (
                (record.title or record.preview).strip()
                if record is not None
                else ""
            ) or f"任务 {thread_id[:8]}"
            items.append(
                {
                    "thread_id": thread_id,
                    "title": title,
                    "group": group,
                    "project": group,
                    "origin": subscription["origin"],
                    "last_activity_at": subscription["last_activity_at"],
                    "expires_at": subscription["expires_at"],
                }
            )
        payload = {"schema_version": 1, "items": items}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        else:
            print(f"当前监测 {len(items)} 个任务：")
            for item in items:
                expiry = "永久" if item["expires_at"] is None else str(item["expires_at"])
                print(
                    f"- {item['title']} | {item['group']} | {item['origin']} | "
                    f"到期={expiry} | {item['thread_id']}"
                )
        return 0
    finally:
        state.close()


def _monitor_add(args: argparse.Namespace) -> int:
    _config_value, state, _codex, records = _monitor_runtime(args)
    try:
        thread_id = str(args.thread_id or "").strip()
        record = records.get(thread_id)
        if record is None:
            raise ConfigError("任务不存在、不可见或属于内部子任务")
        activity = int((record.updated_at_ms or record.created_at_ms or int(time.time()) * 1000) // 1000)
        state.add_manual_monitor(thread_id, last_activity_at=activity)
        payload = {
            "schema_version": 1,
            "success": True,
            "action": "add",
            "thread_id": thread_id,
            "origin": "manual",
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        else:
            print(f"已永久手动监测：{record.title or record.preview or thread_id}")
        return 0
    finally:
        state.close()


def _monitor_remove(args: argparse.Namespace) -> int:
    _config_value, state, _codex, _records = _monitor_runtime(args)
    try:
        thread_id = str(args.thread_id or "").strip()
        removed = state.remove_monitor(thread_id)
        payload = {
            "schema_version": 1,
            "success": True,
            "action": "remove",
            "thread_id": thread_id,
            "removed": removed,
            "suppressed": True,
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        else:
            print(f"已移除并抑制自动恢复：{thread_id}")
        return 0
    finally:
        state.close()


def _monitor_settings(args: argparse.Namespace) -> int:
    config = _config(args, ready=False)
    state = StateStore(config.service.database)
    try:
        raw_enabled = getattr(args, "auto_enabled", None)
        if raw_enabled is None:
            settings = state.auto_monitoring_settings()
            payload = {
                "schema_version": 1,
                "auto_monitoring_enabled": settings["auto_monitoring_enabled"],
                "effective_at": settings["effective_at"],
            }
        else:
            settings = state.set_auto_monitoring_enabled(raw_enabled == "true")
            payload = {
                "schema_version": 1,
                "auto_monitoring_enabled": settings["auto_monitoring_enabled"],
                "changed": settings["changed"],
                "effective_at": settings["effective_at"],
            }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        else:
            status = "开启" if payload["auto_monitoring_enabled"] else "关闭"
            changed = payload.get("changed")
            suffix = "" if changed is None else ("（已变更）" if changed else "（未变化）")
            print(f"自动监测：{status}{suffix}")
            print(f"生效时间：{payload['effective_at']}")
            if not payload["auto_monitoring_enabled"]:
                print("已有自动项保留至原到期时间；手动长期监测不受影响。")
        return 0
    finally:
        state.close()


def _install_notify(args: argparse.Namespace) -> int:
    config = _config(args, ready=False)
    CorrelationCodec.create_secret_file(config.messaging.secret_file)
    result = install_notify(
        python_executable=sys.executable,
        codex_home_path=config.codex.home,
        progress_config_path=config.path,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _install_permission_hook(args: argparse.Namespace) -> int:
    config = _config(args, ready=False)
    CorrelationCodec.create_secret_file(config.messaging.secret_file)
    result = install_permission_hook(
        hooks_file=config.codex.home / "hooks.json",
        python_executable=Path(sys.executable),
        entry_script=PROJECT_ROOT / "progress-wx.py",
        config_file=config.path,
        timeout_seconds=int(config.codex.reply_timeout_seconds) + 60,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _permission_hook(args: argparse.Namespace) -> int:
    config = _config(args, ready=False)
    try:
        payload = json.load(sys.stdin)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError("PermissionRequest hook 输入不是有效 JSON") from exc
    if not isinstance(payload, dict):
        raise ConfigError("PermissionRequest hook 输入不是对象")
    event_name = str(
        payload.get("hook_event_name") or payload.get("hookEventName") or ""
    ).strip()
    if event_name != "PermissionRequest":
        return 0
    bridge = ApprovalBridge(
        config.service.database.parent / "approval-bridge",
        config.messaging.secret_file,
    )
    result = permission_hook_result(
        payload,
        bridge=bridge,
        timeout_seconds=int(config.codex.reply_timeout_seconds),
        rules_file=config.codex.home / "rules" / "feishu-approved.rules",
        codex_command=config.codex.command,
    )
    if result is not None:
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


def _uninstall_permission_hook(args: argparse.Namespace) -> int:
    config = _config(args, ready=False)
    result = uninstall_permission_hook(hooks_file=config.codex.home / "hooks.json")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _uninstall_notify(args: argparse.Namespace) -> int:
    config = _config(args, ready=False)
    result = uninstall_notify(codex_home_path=config.codex.home)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result.get("status") == "externally-modified" else 0


def _run(args: argparse.Namespace) -> int:
    config = _config(args)
    _require_service_backend(config)
    logger = configure_logging(config.service.log_dir, config.service.log_retention_days)
    # 单实例锁内已在发布 PID 前清理旧停止文件，避免吞掉新到达的停止请求。
    pid_state = acquire_instance(config.service.pid_file, config.path)
    service = ProgressService(config.path)

    def watch_stop() -> None:
        while not service.stop_event.wait(0.5):
            if stop_requested_for(config.service.pid_file, pid_state):
                service.request_stop()
                return

    watcher = threading.Thread(target=watch_stop, name="progress-wx-stop-watcher", daemon=True)
    watcher.start()
    try:
        logger.info("服务启动，PID=%d", os.getpid())
        return service.run()
    finally:
        try:
            clear_stop_request(config.service.pid_file, pid_state)
        finally:
            release_instance(config.service.pid_file, pid_state)
        logger.info("服务已停止")


def _background_python() -> Path:
    executable = Path(sys.executable).resolve()
    if os.name == "nt":
        pythonw = executable.with_name("pythonw.exe")
        if pythonw.is_file():
            return pythonw
    return executable


def _start(args: argparse.Namespace) -> int:
    config = _config(args)
    _require_service_backend(config)
    if instance_running(config.service.pid_file):
        state = read_pid_file(config.service.pid_file)
        print(f"服务已经运行，PID={state['pid'] if state else '?'}")
        return 0
    command = [_background_python(), PROJECT_ROOT / "progress-wx.py", "--config", config.path, "run"]
    startupinfo = None
    flags = 0
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    process = subprocess.Popen(
        [os.fspath(item) for item in command],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        close_fds=True,
        startupinfo=startupinfo,
        creationflags=flags,
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if instance_running(config.service.pid_file):
            state = read_pid_file(config.service.pid_file)
            print(f"服务已启动，PID={state['pid'] if state else process.pid}")
            return 0
        if process.poll() is not None:
            print("服务启动失败；请运行前台命令查看 logs。", file=sys.stderr)
            return 1
        time.sleep(0.25)
    print("等待服务启动超时；请检查 logs。", file=sys.stderr)
    return 1


def _stop(args: argparse.Namespace) -> int:
    config = _config(args, ready=False)
    if not request_stop(config.service.pid_file):
        print("服务未运行。")
        return 0
    deadline = time.monotonic() + max(1, args.timeout)
    while time.monotonic() < deadline:
        if not instance_running(config.service.pid_file):
            print("服务已正常停止。")
            return 0
        time.sleep(0.25)
    print("服务未在时限内退出；未执行强杀，请查看日志。", file=sys.stderr)
    return 2


def _status(args: argparse.Namespace) -> int:
    config = _config(args, ready=False)
    state = read_pid_file(config.service.pid_file)
    running = instance_running(config.service.pid_file) if state else False
    result: dict[str, object] = {"running": running, "pid": state.get("pid") if state else None}
    if config.service.database.is_file():
        store = StateStore(config.service.database)
        try:
            result["state"] = store.stats()
            result["state"]["pending_hook_events"] = store.pending_hook_count()
        finally:
            store.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if running else 1


def _gateway_run(args: argparse.Namespace) -> int:
    """网关监督进程入口；不启动 Desktop 或微信服务。"""

    config = _config(args, ready=False)
    configure_logging(config.service.log_dir, config.service.log_retention_days)
    return run_gateway(
        command=config.codex.command,
        websocket_url=config.codex.shared_websocket_url,
        pid_file=config.codex.gateway_pid_file,
        config_path=config.path,
        launch_token=args.launch_token,
    )


def _gateway_startup_detail(path: Path) -> str:
    """读取后台启动器最后一条安全诊断，不把归属令牌回显给用户。"""

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "未能读取后台启动诊断"
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if not lines:
        return "后台进程没有留下诊断输出"
    detail = re.sub(r"\b[0-9a-fA-F]{64}\b", "<redacted>", lines[-1])
    return _cell(detail, limit=360)


def _gateway_start(args: argparse.Namespace) -> int:
    config = _config(args, ready=False)
    pid_file = config.codex.gateway_pid_file
    if verified_gateway_running(pid_file):
        if not gateway_healthy(config.codex.shared_websocket_url):
            raise CodexGatewayError("gateway 进程存在但 /readyz 失败；拒绝重复启动")
        state = read_pid_file(pid_file)
        print(
            json.dumps(
                {
                    "running": True,
                    "healthy": True,
                    "started_by_request": False,
                    "pid": state.get("pid") if state else None,
                    "creation_time": state.get("creation_time") if state else None,
                },
                ensure_ascii=False,
            )
        )
        return 0
    launch_token = args.launch_token
    if not isinstance(launch_token, str) or len(launch_token) < 32:
        raise CodexGatewayError("gateway 启动归属令牌无效")
    launch_token_sha256 = hashlib.sha256(launch_token.encode("utf-8")).hexdigest()
    command = [
        _background_python(),
        PROJECT_ROOT / "progress-wx.py",
        "--config",
        config.path,
        "gateway-run",
        "--launch-token",
        launch_token,
    ]
    startupinfo = None
    flags = 0
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
    diagnostic_path = pid_file.parent / "codex-gateway-startup.log"
    child: subprocess.Popen[bytes] | None = None
    try:
        # 授权发布也必须位于恢复边界内，避免异常窗口遗留无人接管的授权。
        authorize_gateway_launch(pid_file, launch_token)
        diagnostic_path.parent.mkdir(parents=True, exist_ok=True)
        # 后台入口若在发布 PID 前失败，主日志可能尚未来得及记录；保留本次启动输出，
        # 同时持有 Popen 句柄，以便立即识别退出而不是让用户盲等超时。
        with diagnostic_path.open("wb") as diagnostic_stream:
            child = subprocess.Popen(
                [os.fspath(item) for item in command],
                stdin=subprocess.DEVNULL,
                stdout=diagnostic_stream,
                stderr=subprocess.STDOUT,
                shell=False,
                close_fds=True,
                startupinfo=startupinfo,
                creationflags=flags,
            )
        # 子进程内部还要取得最长 30 秒的世代互斥锁，再等待 app-server 就绪；
        # 外层必须覆盖完整窗口，避免健康进程因 20 秒竞争超时被错误回滚。
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            exit_code = child.poll()
            if exit_code is not None:
                detail = _gateway_startup_detail(diagnostic_path)
                raise CodexGatewayError(
                    f"共享 Codex gateway 后台进程提前退出，code={exit_code}；{detail}"
                )
            if verified_gateway_running(pid_file) and gateway_healthy(
                config.codex.shared_websocket_url
            ):
                state = read_pid_file(pid_file)
                started_by_request = bool(
                    state and state.get("launch_token_sha256") == launch_token_sha256
                )
                print(
                    json.dumps(
                        {
                            "running": True,
                            "healthy": True,
                            "started_by_request": started_by_request,
                            "pid": state.get("pid") if state else None,
                            "creation_time": state.get("creation_time") if state else None,
                        },
                        ensure_ascii=False,
                    )
                )
                return 0
            time.sleep(0.1)
        detail = _gateway_startup_detail(diagnostic_path)
        raise CodexGatewayError(
            f"等待共享 Codex gateway 启动超时（60 秒）；{detail}"
        )
    except BaseException:
        try:
            recover_owned_gateway_launch(
                pid_file=pid_file,
                state_file=config.codex.shared_desktop_state_file,
                websocket_url=config.codex.shared_websocket_url,
                launch_token=launch_token,
            )
        except (CodexGatewayError, InstanceError, OSError, RuntimeError) as cleanup_error:
            raise CodexGatewayError(
                "gateway 启动未完成，且本代授权恢复尚未确认；已保留恢复状态"
            ) from cleanup_error
        raise


def _gateway_recover_owned(args: argparse.Namespace) -> int:
    """仅恢复 nonce 精确匹配的 v4 启动授权/网关世代。"""

    config = _config(args, ready=False)
    expected_pid_file = Path(args.expected_pid_file)
    expected_state_file = Path(args.expected_state_file)
    if not expected_pid_file.is_absolute() or not expected_state_file.is_absolute():
        raise CodexGatewayError("v4 恢复上下文必须使用绝对状态路径")
    if (
        os.path.normcase(os.fspath(expected_pid_file.resolve()))
        != os.path.normcase(os.fspath(config.codex.gateway_pid_file.resolve()))
        or os.path.normcase(os.fspath(expected_state_file.resolve()))
        != os.path.normcase(os.fspath(config.codex.shared_desktop_state_file.resolve()))
        or validate_loopback_websocket_url(args.expected_websocket_url)
        != validate_loopback_websocket_url(config.codex.shared_websocket_url)
    ):
        raise CodexGatewayError("v4 恢复上下文与当前配置不一致；已保留旧世代状态")
    result = recover_owned_gateway_launch(
        pid_file=config.codex.gateway_pid_file,
        state_file=config.codex.shared_desktop_state_file,
        websocket_url=config.codex.shared_websocket_url,
        launch_token=args.expected_launch_token,
    )
    print(json.dumps(dict(result), ensure_ascii=False))
    return 0 if result.get("resolved") is True else 2


def _gateway_stop(args: argparse.Namespace) -> int:
    config = _config(args, ready=False)
    if not request_gateway_stop(
        config.codex.gateway_pid_file,
        config.codex.shared_desktop_state_file,
        config.codex.shared_websocket_url,
        expected_pid=args.expected_pid,
        expected_creation_time=args.expected_creation_time,
        expected_launch_token=args.expected_launch_token,
    ):
        print("共享 Codex gateway 未运行。")
        return 0
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if not instance_running(config.codex.gateway_pid_file):
            print("共享 Codex gateway 已正常停止。")
            return 0
        time.sleep(0.1)
    raise CodexGatewayError("gateway 未在时限内退出；未强杀进程")


def _gateway_status(args: argparse.Namespace) -> int:
    config = _config(args, ready=False)
    running = verified_gateway_running(config.codex.gateway_pid_file)
    state = None
    if running:
        state = verified_gateway_state(
            config.codex.gateway_pid_file,
            expected_pid=getattr(args, "expected_pid", None),
            expected_creation_time=getattr(args, "expected_creation_time", None),
            expected_launch_token=getattr(args, "expected_launch_token", None),
        )
    healthy = running and gateway_healthy(config.codex.shared_websocket_url)
    desktop = shared_desktop_running(config.codex.shared_desktop_state_file)
    result = {
        "running": running,
        "healthy": healthy,
        "desktop_shared": desktop,
        "websocket_url": config.codex.shared_websocket_url,
        "gateway_pid_file": os.fspath(config.codex.gateway_pid_file.resolve()),
        "shared_desktop_state_file": os.fspath(
            config.codex.shared_desktop_state_file.resolve()
        ),
        "pid": state.get("pid") if state else None,
        "creation_time": state.get("creation_time") if state else None,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if running and healthy else 1


def _register_shared_desktop(args: argparse.Namespace) -> int:
    config = _config(args, ready=False)
    register_shared_desktop(
        desktop_pid=args.pid,
        websocket_url=config.codex.shared_websocket_url,
        gateway_pid_file=config.codex.gateway_pid_file,
        state_file=config.codex.shared_desktop_state_file,
        install_location=args.install_location,
        not_before_filetime=args.not_before_filetime,
        expected_gateway_pid=args.expected_gateway_pid,
        expected_gateway_creation_time=args.expected_gateway_creation_time,
        expected_gateway_launch_token=args.expected_gateway_launch_token,
    )
    print("已登记经过 TCP 验证的共享 Codex Desktop；未启动微信服务。")
    return 0


def _selected_monitor_records(config, codex_store: CodexStore):
    """按配置的三类精确选择器收集启用时监控对象。"""

    selected = {}
    for thread_id in config.codex.selectors.ids:
        record = codex_store.get_thread(thread_id)
        codex_store.require_readable(f"选择 Codex thread {thread_id}")
        if record is None:
            raise ConfigError(f"配置的 Codex 监控 ID 不存在：{thread_id}")
        selected[record.thread_id] = record
    for title in config.codex.selectors.titles:
        records = codex_store.select_threads(title=title)
        codex_store.require_readable(f"按标题选择 Codex thread {title}")
        if not records:
            raise ConfigError(f"配置的 Codex 监控标题不存在：{title}")
        selected.update((record.thread_id, record) for record in records)
    for cwd in config.codex.selectors.paths:
        records = codex_store.select_threads(cwd=cwd)
        codex_store.require_readable(f"按路径选择 Codex thread {cwd}")
        if not records:
            raise ConfigError(f"配置的 Codex 监控路径不存在：{cwd}")
        selected.update((record.thread_id, record) for record in records)
    return selected


def _selected_terminal_events(config):
    """捕获命令入口时已经结束的最新轮次，固定启用历史分界线。"""

    codex_store = CodexStore(paths=StorePaths.from_codex_home(config.codex.home))
    selected = _selected_monitor_records(config, codex_store)
    events = []
    for thread_id in selected:
        snapshot = codex_store.snapshot(thread_id)
        snapshot.require_readable()
        event = snapshot_to_event(snapshot)
        if event is not None:
            events.append(event)
    return tuple(events)


def _baseline_selected_terminal_turns(events, store: StateStore) -> int:
    """把已捕获的终态轮次登记为历史，不补发启用前旧通知。"""

    changed = 0
    for event in events:
        if not store.was_processed(event.dedupe_key):
            store.mark_processed(event.dedupe_key)
            changed += 1
    return changed


def _baseline_pre_activation_hooks(args: argparse.Namespace) -> int:
    """仅供首次生产启用向导建立 notify 与 SQLite 历史基线。"""

    config = _config(args, ready=False)
    if instance_running(config.service.pid_file):
        raise InstanceError("请先停止服务，再建立启用前 hook 基线")
    if args.expected_count < 0:
        raise ConfigError("expected-count 不能为负数")
    # 先固定 SQLite 终态快照，再原子核对 hook 数量；快照之后结束的新轮次
    # 不会被登记为历史，服务启动后仍会正常通知。
    terminal_events = _selected_terminal_events(config)
    store = StateStore(config.service.database)
    try:
        changed = store.baseline_pending_hooks(args.expected_count)
        terminal_turns = _baseline_selected_terminal_turns(terminal_events, store)
    finally:
        store.close()
    print(
        json.dumps(
            {
                "baselined_pre_activation_hooks": changed,
                "baselined_pre_activation_terminal_turns": terminal_turns,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _resolve_uncertain(args: argparse.Namespace) -> int:
    config = _config(args, ready=False)
    if instance_running(config.service.pid_file):
        raise InstanceError("请先停止服务，再解决结果未知的回复")
    codec = CorrelationCodec.from_file(config.messaging.secret_file)
    code = str(args.code).strip().upper()
    if not codec.valid(code):
        raise ConfigError("PCWX 编号签名无效")
    store = StateStore(config.service.database)
    try:
        changed = store.resolve_uncertain_reply(
            code,
            delivered=args.outcome == "delivered",
        )
    finally:
        store.close()
    if not changed:
        raise ConfigError("没有找到与该编号对应的结果未知回复")
    if args.outcome == "delivered":
        print("已标记为已投递；该正文不会再次提交。")
    else:
        print("已确认未投递；下次启动会重新排队一次。")
    return 0


def _discard_stale_pending_replies(args: argparse.Namespace) -> int:
    """只处理未进入非幂等提交阶段的陈旧回复，且不读取/输出正文。"""

    config = _config(args, ready=False)
    if instance_running(config.service.pid_file):
        raise InstanceError("请先停止服务，再丢弃陈旧回复")
    store = StateStore(config.service.database)
    try:
        changed = store.discard_stale_pending_turn_replies(
            args.expected_count,
            older_than_seconds=args.older_than_seconds,
        )
    finally:
        store.close()
    print(json.dumps({"discarded_stale_pending_replies": changed}, ensure_ascii=False))
    return 0


def _configure_feishu(args: argparse.Namespace) -> int:
    """隐藏读取 App Secret，使用当前 Windows 用户 DPAPI 保存。"""

    config = _config(args, ready=False)
    # 已由安装者预填或上次配置成功时直接复用 App ID；它不是秘密，避免远控
    # 环境下重复复制。命令行 --app-id 仍可显式覆盖，更换应用时不会猜测。
    configured_app_id = (
        config.feishu.app_id
        if re.fullmatch(r"cli_[A-Za-z0-9]+", config.feishu.app_id)
        else ""
    )
    app_id = str(
        getattr(args, "app_id", None)
        or configured_app_id
        or input("请输入飞书 App ID（cli_ 开头）：")
    ).strip()
    if not re.fullmatch(r"cli_[A-Za-z0-9]+", app_id):
        raise ConfigError("飞书 App ID 格式无效，应以 cli_ 开头")
    if args.secret_stdin:
        app_secret = sys.stdin.readline().rstrip("\r\n")
    else:
        app_secret = getpass.getpass("请输入飞书 App Secret（输入不会显示）：")
    if len(app_secret.strip()) < 8:
        raise ConfigError("飞书 App Secret 为空或长度异常")
    DpapiSecretStore(config.feishu.app_secret_file).save(app_secret.strip())
    _update_yaml_scalar(config.path, "feishu", "app_id", app_id)
    print("App ID 已写入配置，App Secret 已由当前 Windows 用户 DPAPI 加密保存。")
    print("下一步：发布飞书应用并启用长连接后，运行“一键绑定手机飞书”。")
    return 0


def _pair_feishu(args: argparse.Namespace) -> int:
    """用一次性正文把手机用户 open_id 精确写入白名单。"""

    config = _config(args, ready=False)
    if not re.fullmatch(r"cli_[A-Za-z0-9]+", config.feishu.app_id):
        raise ConfigError("请先运行飞书配置，保存有效 App ID")
    app_secret = DpapiSecretStore(config.feishu.app_secret_file).load()
    if not app_secret:
        raise ConfigError("请先运行飞书配置，安全保存 App Secret")
    timeout = float(args.timeout)
    if not 30 <= timeout <= 600:
        raise ConfigError("绑定等待时间必须介于 30 到 600 秒")
    # 64 位一次性随机量兼顾手机复制便利与短时开放绑定窗口的抗猜测能力。
    pairing_code = "PCPAIR-" + secrets.token_hex(8).upper()
    print("请在手机飞书中打开“进度通知”机器人，并原样发送下面的一次性绑定码：")
    print(pairing_code, flush=True)
    open_id = discover_feishu_open_id(
        app_id=config.feishu.app_id,
        app_secret=app_secret,
        pairing_code=pairing_code,
        timeout_seconds=timeout,
    )
    _update_yaml_scalar(config.path, "feishu", "target_open_id", open_id)
    print("手机飞书已绑定；只有该用户的私聊引用回复会被处理。")
    return 0


def _test_feishu(args: argparse.Namespace) -> int:
    """连接官方长连接并向唯一白名单用户发送一条真实测试消息。"""

    config = _config(args)
    if config.messaging.backend != "feishu":
        raise ConfigError("当前 messaging.backend 不是 feishu")
    app_secret = DpapiSecretStore(config.feishu.app_secret_file).load()
    if not app_secret:
        raise ConfigError("飞书 App Secret 无法读取")
    errors: list[BaseException] = []
    channel = FeishuMessageChannel(
        app_id=config.feishu.app_id,
        app_secret=app_secret,
        target_open_id=config.feishu.target_open_id,
        connect_timeout_seconds=config.feishu.connect_timeout_seconds,
        max_attempts=config.service.max_attempts,
        retry_delays=config.service.retry_delays,
        error_handler=errors.append,
    )
    # 测试命令也必须沿用生产服务的有限重试边界；FeishuMessageChannel 的
    # 首次连接不在内部重试，故这里仅包一层，避免嵌套后放大为 25 次。
    policy = RetryPolicy(config.service.max_attempts, config.service.retry_delays)
    idempotency_key = f"manual-test:{time.time_ns()}"
    usage_guide = bool(getattr(args, "usage_guide", False))
    image_message_ids: list[str] = []
    footer_message_id: str | tuple[str, ...] | None = None
    try:
        call_with_retry(
            "飞书测试连接",
            lambda: channel.start(lambda _reply: None),
            policy,
            sleep=time.sleep,
        )
        message_id = None
        if not usage_guide:
            message_id = call_with_retry(
                "飞书测试消息发送",
                lambda: channel.send_text(
                    args.text,
                    idempotency_key=idempotency_key,
                ),
                policy,
                sleep=time.sleep,
            )
        if usage_guide:
            for index, (_name, data) in enumerate(feishu_usage_images(), start=1):
                result = call_with_retry(
                    f"飞书课堂图片 {index} 发送",
                    lambda data=data, index=index: channel.send_image(
                        data,
                        idempotency_key=f"{idempotency_key}:usage-image:{index}",
                    ),
                    policy,
                    sleep=time.sleep,
                )
                values = (result,) if isinstance(result, str) else tuple(result or ())
                image_message_ids.extend(str(item) for item in values)
            footer_message_id = call_with_retry(
                "飞书课堂提示发送",
                lambda: channel.send_text(
                    USAGE_IMAGE_FOOTER,
                    idempotency_key=f"{idempotency_key}:usage-footer",
                ),
                policy,
                sleep=time.sleep,
            )
    finally:
        # 连接失败、发送失败或异常退出都必须释放 WebSocket 线程与 SDK。
        channel.stop()
    if errors:
        raise RuntimeError("飞书测试期间连接发生异常") from errors[0]
    output: dict[str, object] = {"sent": True}
    if usage_guide:
        output["image_message_ids"] = image_message_ids
        output["footer_message_id"] = footer_message_id
    else:
        output["message_id"] = message_id
    print(json.dumps(output, ensure_ascii=False))
    return 0


def _doctor(args: argparse.Namespace) -> int:
    # doctor 允许在凭证尚未配置时运行，只做本地 Codex 通道与依赖诊断。
    config = _config(args, ready=False)
    result: dict[str, object] = {}
    if config.codex.reply_transport == "desktop_app_tools":
        session = DesktopAppToolsClient(config.codex.desktop_log_dir).open_verified()
        try:
            result["codex_desktop_tool"] = "send_message_to_thread"
            result["codex_desktop_tool_verified"] = True
            result["codex_transport"] = "desktop_app_tools_named_pipe"
        finally:
            session.close()
    else:
        websocket_url = None
        if config.codex.reply_transport == "shared_websocket":
            websocket_url = active_shared_websocket_url(
                websocket_url=config.codex.shared_websocket_url,
                gateway_pid_file=config.codex.gateway_pid_file,
                state_file=config.codex.shared_desktop_state_file,
            )
        with CodexAppServer(
            config.codex.command,
            timeout_seconds=30,
            websocket_url=websocket_url,
        ) as rpc:
            result["codex_app_server"] = sorted(rpc.initialize().keys())
            result["codex_transport"] = rpc.transport
    result["codex_reply_transport"] = config.codex.reply_transport
    result["messaging_backend"] = config.messaging.backend
    if config.messaging.backend == "feishu":
        package_present = importlib.util.find_spec("lark_channel") is not None
        app_id_configured = bool(re.fullmatch(r"cli_[A-Za-z0-9]+", config.feishu.app_id))
        target_configured = bool(
            re.fullmatch(r"ou_[A-Za-z0-9_-]+", config.feishu.target_open_id)
        )
        secret_present = config.feishu.app_secret_file.is_file()
        result["lark_channel_package_present"] = package_present
        result["app_id_configured"] = app_id_configured
        result["app_secret_dpapi_present"] = secret_present
        result["target_open_id_configured"] = target_configured
        result["feishu_network_connection_opened"] = False
        backend_ready = (
            package_present and app_id_configured and secret_present and target_configured
        )
    else:
        package_present = importlib.util.find_spec("wxautox4") is not None
        result["wxautox4_package_present"] = package_present
        result["wxautox4_activation_checked"] = False
        result["wechat_client_created"] = False
        result["free_probe_dependency_present"] = (
            importlib.util.find_spec("uiautomation") is not None
        )
        backend_ready = config.messaging.backend == "wxautox4" and package_present
    result["service_backend_dependency_ready"] = backend_ready
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if backend_ready else 2


def _probe_free_wechat(args: argparse.Namespace) -> int:
    """运行不读取消息正文、不操作窗口的开源 UIA 能力探针。"""

    nickname = args.nickname
    if not nickname:
        nickname = _config(args, ready=False).wechat.tool_account_nickname
    result = probe_tool_window(
        nickname,
        single_visible_window=args.single_visible_window,
        diagnostic_unverified_identity=args.diagnostic_unverified_identity,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _test_wechat(args: argparse.Namespace) -> int:
    config = _config(args)
    service = WechatService(
        WxAutoX4Adapter(account_nickname=config.wechat.tool_account_nickname),
        tool_wechat_id=config.wechat.tool_wechat_id,
        chat_name=config.wechat.target_chat,
        target_wechat_id=config.wechat.target_wechat_id,
    )
    service.start(lambda _message: None)
    try:
        service.send_text(args.text)
    finally:
        service.stop()
    print("测试消息已发送。")
    return 0


def _verify_wechat(args: argparse.Namespace) -> int:
    """只读核验发送端账号和唯一联系人，不注册监听器。"""

    config = _config(args)
    adapter = WxAutoX4Adapter(account_nickname=config.wechat.tool_account_nickname)
    if adapter.verify_account(config.wechat.tool_wechat_id) is not True:
        raise ConfigError("当前绑定窗口不是配置的工具小号")
    if not adapter.is_online():
        raise ConfigError("工具小号当前不在线")
    if adapter.verify_friend(
        config.wechat.target_chat,
        config.wechat.target_wechat_id,
    ) is not True:
        raise ConfigError("唯一联系人白名单校验失败")
    print("微信身份校验通过：工具小号与唯一联系人均精确匹配；未监听、未发送消息。")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    _configure_console()
    args = _parser().parse_args(argv)
    actions = {
        "validate": _validate,
        "list-threads": _list_threads,
        "configure-monitor": _configure_monitor,
        "monitor-list": _monitor_list,
        "monitor-add": _monitor_add,
        "monitor-remove": _monitor_remove,
        "monitor-settings": _monitor_settings,
        "install-notify": _install_notify,
        "install-permission-hook": _install_permission_hook,
        "permission-hook": _permission_hook,
        "uninstall-permission-hook": _uninstall_permission_hook,
        "uninstall-notify": _uninstall_notify,
        "run": _run,
        "start": _start,
        "stop": _stop,
        "status": _status,
        "gateway-run": _gateway_run,
        "gateway-start": _gateway_start,
        "gateway-recover-owned": _gateway_recover_owned,
        "gateway-stop": _gateway_stop,
        "gateway-status": _gateway_status,
        "register-shared-desktop": _register_shared_desktop,
        "baseline-pre-activation-hooks": _baseline_pre_activation_hooks,
        "discard-stale-pending-replies": _discard_stale_pending_replies,
        "resolve-uncertain": _resolve_uncertain,
        "doctor": _doctor,
        "configure-feishu": _configure_feishu,
        "pair-feishu": _pair_feishu,
        "test-feishu": _test_feishu,
        "probe-free-wechat": _probe_free_wechat,
        "verify-wechat": _verify_wechat,
        "test-wechat": _test_wechat,
    }
    try:
        return actions[args.command](args)
    except (ConfigError, InstanceError, CodexGatewayError, RuntimeError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
