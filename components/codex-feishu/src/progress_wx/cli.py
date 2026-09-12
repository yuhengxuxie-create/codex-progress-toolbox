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
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

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
from .codex_store import (
    CodexStore,
    CodexStoreReadError,
    StorePaths,
    ThreadRecord,
    public_thread_title,
    thread_title_recovery_hash,
)
from .codex_projects import CodexProjectRegistry
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
    read_channel_health,
    read_pid_file,
    release_instance,
    request_stop,
    stop_requested_for,
)
from .retry import RetryPolicy, call_with_retry
from .reset_alert import BEIJING, next_check_at as reset_next_check_at
from .service import ProgressService, snapshot_to_event
from .secrets import DpapiSecretStore
from .state import CorrelationCodec, StateError, StateStore
from .session_search import (
    AtomicProgressFile,
    LunaSemanticJudge,
    SearchRequest,
    SessionSearchCancelled,
    build_session_search_engine,
)
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
    session_search = sub.add_parser(
        "session-search", help="从 UTF-8 JSON 请求搜索全部用户 Codex 会话"
    )
    session_search.add_argument(
        "--request-file",
        required=True,
        help="UTF-8 request JSON 文件；使用 - 从 stdin 读取",
    )
    session_search.add_argument(
        "--progress-file",
        required=True,
        type=Path,
        help="原子写入、由调用方轮询的 progress JSON 文件",
    )
    session_search.add_argument(
        "--cancel-file",
        type=Path,
        help="可选；调用方创建该文件后，搜索会在下一安全检查点取消",
    )
    session_search.add_argument(
        "--cache-mode",
        choices=("persistent", "ephemeral"),
        default="persistent",
        help=(
            "缓存模式：persistent 写入生产缓存（默认）；ephemeral 使用临时状态副本，"
            "适合 UI/联调验收且不修改生产状态"
        ),
    )
    session_search.add_argument(
        "--json", action="store_true", help="兼容显式 JSON 模式；成功 stdout 始终仅 JSON"
    )
    repair_titles = sub.add_parser(
        "repair-thread-titles",
        help="一次性恢复缺失的历史会话标题；列表刷新本身不会调用模型",
    )
    repair_titles.add_argument(
        "--max-model-calls",
        type=int,
        choices=(1, 2, 3),
        default=3,
        help="本次最多发起的 Luna 批次，单批最多6条（默认3）",
    )
    repair_titles.add_argument(
        "--json", action="store_true", help="只向 stdout 输出不含标题正文的 JSON"
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
    sub.add_parser("guardian-run", help=argparse.SUPPRESS)
    worker = sub.add_parser("worker-run", help=argparse.SUPPRESS)
    worker.add_argument("--guardian-token", required=True)
    guardian_status_parser = sub.add_parser("guardian-status", help="只读显示通信守护和业务状态")
    guardian_status_parser.add_argument("--json", action="store_true")
    guardian_stop_parser = sub.add_parser("guardian-stop", help="完整退出业务和远程救援")
    guardian_stop_parser.add_argument("--timeout", type=int, default=30)
    maintenance = sub.add_parser("guardian-maintenance", help="受控进入或退出升级维护")
    maintenance_choice = maintenance.add_mutually_exclusive_group(required=True)
    maintenance_choice.add_argument("--enter", action="store_true")
    maintenance_choice.add_argument("--leave", action="store_true")
    maintenance.add_argument("--shutdown-guardian", action="store_true")
    maintenance.add_argument("--timeout", type=int, default=30)
    recover = sub.add_parser("guardian-recover", help="按精确实例身份恢复通信守护")
    recover.add_argument("--expected-pid", type=int, required=True)
    recover.add_argument("--expected-creation-time", type=int, required=True)
    recover.add_argument("--reason", choices=("crashed","unresponsive"), required=True)
    worker_recover = sub.add_parser("worker-recover", help="本机按精确身份恢复无响应业务，保留不确定投递")
    worker_recover.add_argument("--expected-pid", type=int, required=True)
    worker_recover.add_argument("--expected-creation-time", type=int, required=True)
    reset_alert_status = sub.add_parser(
        "reset-alert-status", help="只读显示 Codex 重置预警运行状态"
    )
    reset_alert_status.add_argument(
        "--json", action="store_true", help="成功时 stdout 仅输出 schema 1 JSON"
    )
    reset_alert_latest = sub.add_parser(
        "reset-alert-latest", help="只读显示最近的 Codex 重置预警事件"
    )
    reset_alert_latest.add_argument("--limit", type=int, default=10)
    reset_alert_latest.add_argument(
        "--json", action="store_true", help="成功时 stdout 仅输出 schema 1 JSON"
    )
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
    artifact_status = sub.add_parser("artifact-status",help="只读查看成果文件投递状态")
    artifact_status.add_argument("--json",action="store_true")
    artifact_status.add_argument("--thread-id",default="")
    artifact_status.add_argument("--offset",type=int,default=0)
    artifact_status.add_argument("--limit",type=int,default=100)
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
        help="发送四页可直接预览的课堂图片及文字版提示",
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


def _open_read_only_state(path: Path) -> StateStore:
    """打开现有状态库的硬只读连接；只读命令不得触发迁移。"""

    return StateStore.open_read_only(path)


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
    else:
        try:
            # Codex 的 state_5.sqlite 不是本服务状态库，不能套用 FeiShuBOT
            # 的 meta/schema17 校验；由 CodexStore 以其自己的只读语义读取。
            codex_store = CodexStore(paths=paths)
            codex_store.select_threads(include_archived=True)
            codex_store.require_readable("Codex 状态只读检查")
        except (CodexStoreReadError, OSError) as exc:
            errors.append(f"Codex 状态只读检查失败：{exc}")
    service = getattr(config, "service", None)
    service_database = getattr(service, "database", None)
    if service_database is not None:
        service_database = Path(service_database).expanduser().resolve()
        if not service_database.is_file():
            errors.append(f"缺少 {service_database}")
        else:
            try:
                state = _open_read_only_state(service_database)
            except (StateError, OSError) as exc:
                errors.append(f"FeiShuBOT 状态库只读检查失败：{exc}")
            else:
                state.close()
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
    records = [
        item
        for item in store.select_threads(include_archived=True)
        if item.thread_source != "subagent"
    ]
    store.require_readable("列出用户 Codex 会话")
    state = _open_read_only_state(config.service.database)
    try:
        projects = _desktop_project_assignments(config.codex.home)
        items = []
        for record in records:
            title, title_origin = _thread_display_title(state, record)
            items.append(
                {
                    "id": record.thread_id,
                    "title": title,
                    "title_origin": title_origin,
                    "cwd": record.cwd,
                    "archived": record.archived,
                    "updated_at_ms": record.updated_at_ms,
                    "thread_source": record.thread_source,
                    "project_id": projects.get(record.thread_id, (None, None))[0],
                    "project_name": projects.get(record.thread_id, (None, None))[1],
                }
            )
        if args.json:
            print(json.dumps(items, ensure_ascii=False, indent=2))
        else:
            print("ID\t标题\t归属\t工作目录\t已归档")
            for item in items:
                project_name = item["project_name"] or "个人对话"
                print(
                    f"{item['id']}\t{_cell(item['title'])}\t{_cell(project_name)}\t"
                    f"{_cell(item['cwd'], 260)}\t{item['archived']}"
                )
        return 0
    finally:
        state.close()


def _thread_display_title(
    state: StateStore, record: ThreadRecord
) -> tuple[str, str]:
    cached = state.thread_title_recovery(
        record.thread_id, thread_title_recovery_hash(record)
    )
    recovered = str(cached.get("display_title") or "") if cached else ""
    return public_thread_title(record, recovered)


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


def _monitor_runtime(args: argparse.Namespace, *, read_only: bool = False):
    config = _config(args, ready=False)
    state = (
        _open_read_only_state(config.service.database)
        if read_only
        else StateStore(config.service.database)
    )
    codex = CodexStore(paths=StorePaths.from_codex_home(config.codex.home))
    records = codex.select_threads(include_archived=True)
    codex.require_readable("读取 Codex 监测目录")
    top_level = {
        item.thread_id: item for item in records if item.thread_source != "subagent"
    }
    return config, state, codex, top_level


def _monitor_list(args: argparse.Namespace) -> int:
    config, state, _codex, records = _monitor_runtime(args, read_only=True)
    try:
        projects = _desktop_project_assignments(config.codex.home)
        items = []
        for subscription in state.monitor_subscriptions():
            thread_id = str(subscription["thread_id"])
            record = records.get(thread_id)
            project = projects.get(thread_id)
            group = project[1] if project is not None else "个人会话"
            if record is not None:
                title, title_origin = _thread_display_title(state, record)
            else:
                title = f"任务 {thread_id[:8]}"
                title_origin = "missing_record"
            items.append(
                {
                    "thread_id": thread_id,
                    "title": title,
                    "title_origin": title_origin,
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
            title, _title_origin = _thread_display_title(state, record)
            print(f"已永久手动监测：{title}")
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
    raw_enabled = getattr(args, "auto_enabled", None)
    state = (
        _open_read_only_state(config.service.database)
        if raw_enabled is None
        else StateStore(config.service.database)
    )
    try:
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


class _SessionSearchProgress:
    """让每个进度快照都明确当前缓存隔离语义，不改变 schema v1。"""

    def __init__(self, sink: AtomicProgressFile, cache_mode: str):
        self._sink = sink
        self._prefix = (
            "【隔离临时缓存】" if cache_mode == "ephemeral" else "【生产持久缓存】"
        )

    def write(self, phase: str, current: int, total: int, message: str) -> None:
        self._sink.write(phase, current, total, f"{self._prefix}{message}")


def _session_search(args: argparse.Namespace) -> int:
    """稳定 CLI：请求正文不进入进程命令行，成功 stdout 只有一个 JSON。"""

    cache_mode = str(getattr(args, "cache_mode", "persistent") or "persistent")
    progress = _SessionSearchProgress(
        AtomicProgressFile(args.progress_file), cache_mode
    )
    progress.write("starting", 0, 0, "正在校验会话搜索请求")
    try:
        if str(args.request_file) == "-":
            binary_stdin = getattr(sys.stdin, "buffer", None)
            if binary_stdin is not None:
                raw = binary_stdin.read(65_537)
                if len(raw) > 65_536:
                    raise ConfigError("session-search 请求超过 64 KiB 上限")
                raw_text = raw.decode("utf-8-sig")
            else:
                # 测试替身或少数嵌入环境可能没有 buffer；这条兼容路径仍要求
                # 调用方交付已正确解码的 Unicode 文本。
                raw_text = sys.stdin.read(65_537)
                if len(raw_text.encode("utf-8")) > 65_536:
                    raise ConfigError("session-search 请求超过 64 KiB 上限")
        else:
            request_path = Path(args.request_file).expanduser().resolve()
            raw = request_path.read_bytes()
            if len(raw) > 65_536:
                raise ConfigError("session-search 请求超过 64 KiB 上限")
            raw_text = raw.decode("utf-8-sig")
        parsed = json.loads(raw_text)
        if not isinstance(parsed, dict):
            raise ConfigError("session-search 请求 JSON 根节点必须是对象")
        request = SearchRequest.from_mapping(parsed)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        progress.write("failed", 0, 0, "搜索请求无效")
        raise ConfigError(f"session-search 请求无效：{exc}") from exc

    config = _config(args)
    temporary: tempfile.TemporaryDirectory[str] | None = None
    state_path = config.service.database
    try:
        if cache_mode == "ephemeral":
            temporary = tempfile.TemporaryDirectory(
                prefix="progress-wx-session-search-state-"
            )
            state_path = Path(temporary.name) / "isolated-state.sqlite"
            source_path = Path(config.service.database).expanduser().resolve()
            # 临时隔离搜索只从生产库读取；正确转义路径并在没有活动 WAL 时
            # 使用 immutable，避免 SQLite 为这条“只读”连接创建 -shm/-wal。
            source_uri = f"{source_path.as_uri()}?mode=ro"
            if not Path(f"{source_path}-wal").exists():
                source_uri += "&immutable=1"
            source = sqlite3.connect(source_uri, uri=True, timeout=10)
            destination = sqlite3.connect(state_path)
            try:
                source.backup(destination)
            finally:
                destination.close()
                source.close()
            progress.write(
                "starting",
                0,
                0,
                "生产状态已只读复制，后续不会修改生产状态",
            )
        state = StateStore(state_path)
    except BaseException:
        if temporary is not None:
            temporary.cleanup()
        raise
    try:
        engine = build_session_search_engine(
            state=state,
            codex_store=CodexStore(config.codex.home),
            codex_home=config.codex.home,
            summary_config=config.summary,
            codex_command=config.summary.codex_command or config.codex.command,
            project_registry=CodexProjectRegistry(
                config.codex.home / ".codex-global-state.json",
                config.codex.managed_project_root,
            ),
            retry_policy=RetryPolicy(
                config.service.max_attempts,
                config.service.retry_delays,
            ),
        )
        try:
            result = engine.search(
                request,
                progress=progress,
                cancel_file=args.cancel_file,
            )
        except SessionSearchCancelled as exc:
            progress.write("cancelled", 0, 0, "搜索已取消")
            print(f"错误：{exc}", file=sys.stderr)
            return 3
        except BaseException:
            progress.write("failed", 0, 0, "搜索失败；未返回不可信候选")
            raise
        print(json.dumps(result.to_dict(), ensure_ascii=False, separators=(",", ":")))
        return 0
    finally:
        state.close()
        if temporary is not None:
            temporary.cleanup()


def _repair_thread_titles(args: argparse.Namespace) -> int:
    """显式、受限地恢复历史异常标题；不会由列表/服务轮询隐式触发。"""

    config = _config(args)
    state = StateStore(config.service.database)
    try:
        engine = build_session_search_engine(
            state=state,
            codex_store=CodexStore(config.codex.home),
            codex_home=config.codex.home,
            summary_config=config.summary,
            codex_command=config.summary.codex_command or config.codex.command,
            project_registry=CodexProjectRegistry(
                config.codex.home / ".codex-global-state.json",
                config.codex.managed_project_root,
            ),
            retry_policy=RetryPolicy(1, (0.0,)),
        )
        # 维护命令以“实际进程尝试”为硬上限；单个批次失败不会嵌套重试并
        # 悄悄超过用户可预期的额度。
        engine.judge = LunaSemanticJudge(
            config.summary.codex_command or config.codex.command,
            timeout_seconds=config.summary.timeout_seconds,
            retry_policy=RetryPolicy(1, (0.0,)),
        )
        result = engine.repair_missing_titles(
            max_model_calls=int(args.max_model_calls)
        )
        if args.json:
            print(json.dumps(dict(result), ensure_ascii=False, separators=(",", ":")))
        else:
            print(
                "标题异常总数={total_anomalies}，已缓存={already_recovered}，"
                "本次恢复={recovered}，剩余={remaining}，Luna调用={model_call_count}".format(
                    **result
                )
            )
        return 0 if int(result["remaining"]) == 0 else 3
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
    if config.messaging.backend == "feishu" and getattr(args,"command","") != "worker-run":
        from .guardian import control
        return control(config,"start")
    guardian_token = getattr(args,"guardian_token",None)
    if config.messaging.backend == "feishu":
        from .guardian import control_root
        from .guardian_store import GuardianStore
        authorization = GuardianStore(control_root(config),readonly=True)
        try:
            if not guardian_token or authorization.get("worker_token") != guardian_token or authorization.get("desired_state") != "running":
                raise RuntimeError("worker_launch_not_authorized")
        finally:
            authorization.close()
    logger = configure_logging(config.service.log_dir, config.service.log_retention_days)
    # 单实例锁内已在发布 PID 前清理旧停止文件，避免吞掉新到达的停止请求。
    pid_state = acquire_instance(
        config.service.pid_file,
        config.path,
        metadata={"channel_health_schema_version": 1},
    )
    service = ProgressService(config.path)
    service.guardian_generation = guardian_token

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
    if config.messaging.backend == "feishu":
        from .guardian import control
        result = control(config,"start")
        print("服务已就绪。" if result == 0 else "服务未就绪，请检查 guardian-status。")
        return result
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
    if getattr(getattr(config,"messaging",None),"backend",None) == "feishu":
        from .guardian import control
        return control(config,"stop",timeout=max(1,args.timeout))
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
    health = (
        read_channel_health(config.service.pid_file, instance_state=state)
        if running and state is not None
        else None
    )
    raw_channel_state = str((health or {}).get("channel_state") or "unknown")
    ever_connected = bool((health or {}).get("ever_connected"))
    if not running:
        service_state = "stopped"
        raw_channel_state = "stopped"
    elif health is None:
        service_state = (
            "connecting"
            if state is not None and state.get("channel_health_schema_version") == 1
            else "running"
        )
    elif raw_channel_state == "online":
        service_state = "running"
    elif ever_connected:
        service_state = "reconnecting"
    else:
        service_state = "connecting"
    result: dict[str, object] = {
        "schema_version": 1,
        "running": running,
        "pid": state.get("pid") if running and state else None,
        "service_state": service_state,
        "channel": {
            "state": raw_channel_state,
            "online": bool((health or {}).get("online")),
            "ever_connected": ever_connected,
            "consecutive_failures": int(
                (health or {}).get("consecutive_failures") or 0
            ),
            "last_failure_class": str(
                (health or {}).get("last_failure_class") or ""
            ),
            "last_failure_type": str(
                (health or {}).get("last_failure_type") or ""
            ),
            "next_retry_at": _reset_alert_time(
                (health or {}).get("next_retry_at")
            ),
            "updated_at": _reset_alert_time((health or {}).get("updated_at")),
        },
    }
    if config.service.database.is_file():
        store = _open_read_only_state(config.service.database)
        try:
            result["state"] = store.stats()
            result["state"]["pending_hook_events"] = store.pending_hook_count()
        finally:
            store.close()
    from .guardian import guardian_status
    guardian_view = guardian_status(config)
    result["guardian"] = guardian_view["guardian"]
    result["worker"] = guardian_view["worker"]
    result["desired_state"] = guardian_view["desired_state"]
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if running else 1


def _guardian_command(args: argparse.Namespace) -> int:
    from .guardian import control, guardian_status, run_guardian, recover_guardian
    config = _config(args,ready=False)
    if args.command == "worker-recover":
        from .guardian import recover_worker
        result=recover_worker(config,args.expected_pid,args.expected_creation_time)
        print(json.dumps(guardian_status(config),ensure_ascii=False))
        return result
    if args.command == "guardian-status":
        print(json.dumps(guardian_status(config),ensure_ascii=False))
        return 0
    if args.command == "guardian-run":
        from .guardian import control_root
        from .guardian_store import private_directory
        private_directory(control_root(config))
        configure_logging(control_root(config)/"logs",7)
        return run_guardian(config)
    if args.command == "guardian-recover":
        result = recover_guardian(config,args.expected_pid,args.expected_creation_time,args.reason)
        print(json.dumps(guardian_status(config),ensure_ascii=False))
        return result
    if args.command == "guardian-stop":
        return control(config,"exit",timeout=max(1,args.timeout))
    return control(config,"enter" if args.enter else "leave",timeout=max(1,args.timeout),shutdown_guardian=args.shutdown_guardian)


def _reset_alert_time(value: object) -> str | None:
    if value is None:
        return None
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return None
    try:
        return datetime.fromtimestamp(timestamp, BEIJING).isoformat(timespec="seconds")
    except (ValueError, OverflowError, OSError):
        return None


def _x_endpoint_status(cursor: Mapping[str, Any], *, now: int) -> dict[str, Any]:
    endpoints = {}
    for key in ('syndication', 'oembed', 'x_parent'):
        value = (cursor.get('x_endpoints') or {}).get(key, {})
        until = int(value.get('cooldown_until') or 0)
        unrepresentable = bool(value.get('wait_unrepresentable')) or bool(until and _reset_alert_time(until) is None)
        cooling = unrepresentable or until > now
        error = str(value.get('last_error') or '')
        state = ('cooldown' if cooling else 'retry_due' if error == 'source_http_429' else
                 'error' if error else 'available' if value.get('last_success_at') else 'never')
        next_attempt = None
        if value and not unrepresentable:
            try:
                next_attempt = _reset_alert_time(reset_next_check_at(max(now + 1, until) - 1))
            except (ValueError, OverflowError, OSError):
                unrepresentable = True
        endpoints[key] = {'state': state,
            'retry_not_before': _reset_alert_time(until) if until and not unrepresentable else None,
            'next_attempt_at': next_attempt, 'last_attempt_at': _reset_alert_time(value.get('last_attempt_at')),
            'last_success_at': _reset_alert_time(value.get('last_success_at')),
            'consecutive_429': int(value.get('consecutive_429') or 0),
            'cooldown_basis': str(value.get('cooldown_basis') or ''),
            'last_error_code': error or None, 'retry_unrepresentable': unrepresentable}
    fallback = cursor.get('forecast_discovery') or {}
    verification = cursor.get('official_verification') or {}
    verified = int(verification.get('verified_count') or 0)
    attempted = bool(verification.get('attempted'))
    return {'x_endpoint_states': endpoints,
        'fallback_discovery': {'source': 'forecast',
            'state': ('unknown' if not fallback else 'available' if fallback.get('success') else 'unavailable'),
            'candidate_count': fallback.get('candidate_count'),
            'checked_at': _reset_alert_time(fallback.get('checked_at'))},
        'official_verification': {
            'state': ('unknown' if not verification else 'verified' if verification.get('live_verified_count') else
                      'cached' if verified else 'unavailable' if verification.get('error_code') else 'not_needed'),
            'attempted': attempted, 'verified_count': verification.get('verified_count'),
            'live_verified_count': verification.get('live_verified_count'),
            'last_error_code': verification.get('error_code') or None}}


def _reset_alert_status(args: argparse.Namespace) -> int:
    config = _config(args, ready=False)
    store = _open_read_only_state(config.service.database)
    try:
        status = store.reset_alert_status()
    finally:
        store.close()
    sources = []
    for item in status.get("sources", []):
        cursor = json.loads(str(item.get("cursor_json") or "{}"))
        sources.append(
            {
                "source": str(item.get("source_id") or ""),
                "health": str(item.get("health") or "never"),
                "last_check_at": _reset_alert_time(item.get("last_attempt_at")),
                "last_success_at": _reset_alert_time(item.get("last_success_at")),
                "last_item_at": _reset_alert_time(item.get("last_item_at")),
                "baseline_ready": item.get("baseline_completed_at") is not None,
                "last_error_code": item.get("last_error_code"),
                "coverage": "degraded" if item.get("health") != "ok" or cursor.get("syndication_error") else "available",
                "discovery_error_code": cursor.get("syndication_error"),
                **(_x_endpoint_status(cursor, now=int(time.time()))
                   if item.get('source_id') == 'x_thsottiaux' else {}),
            }
        )
    available = bool(status.get("available"))
    payload = {
        "schema_version": 1,
        "available": available,
        "enabled": bool(config.reset_alert.enabled and status.get("enabled")),
        "can_alert": bool(
            available
            and config.reset_alert.enabled
            and status.get("enabled")
            and status.get("worker_running")
            and any(item["health"] == "ok" and item["source"] != "forecast" for item in sources)
        ),
        "worker_running": bool(status.get("worker_running")),
        "worker_started_at": _reset_alert_time(status.get("worker_started_at")),
        "worker_heartbeat_at": _reset_alert_time(status.get("worker_heartbeat_at")),
        "worker_stopped_at": _reset_alert_time(status.get("worker_stopped_at")),
        "state": str(status.get("state") or "unknown"),
        "timezone": "UTC+08:00",
        "check_hours": list(range(8, 24)),
        "last_check_at": _reset_alert_time(status.get("last_attempt_at")),
        "last_success_at": _reset_alert_time(status.get("last_success_at")),
        "next_check_at": _reset_alert_time(status.get("next_check_at")),
        "window_start_at": _reset_alert_time(status.get("window_start_at")),
        "window_end_at": _reset_alert_time(status.get("window_end_at")),
        "pending": int(status.get("pending") or 0),
        "uncertain": int(status.get("uncertain") or 0),
        "last_error_code": status.get("last_error_code"),
        "source_states": sources,
        "coverage": "available" if sources and all(item["coverage"] == "available" for item in sources) else "degraded",
    }
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0


def _reset_alert_delivery_contract(raw: dict[str, object]) -> dict[str, object]:
    delivery = dict(raw)
    state = str(delivery.get("state") or "pending")
    terminal = state in {"delivered", "rejected", "uncertain", "expired"}
    if state == "delivered":
        consumer_state = "delivered"
    elif state in {"rejected", "expired"}:
        consumer_state = "failed"
    elif state == "uncertain":
        consumer_state = "needs_attention"
    else:
        consumer_state = "wait"
    delivery.update(
        {
            "terminal": terminal,
            "consumable": terminal,
            "consumer_state": consumer_state,
        }
    )
    return delivery


def _reset_alert_latest(args: argparse.Namespace) -> int:
    if not 1 <= int(args.limit) <= 100:
        raise ConfigError("reset-alert-latest --limit 必须介于 1 和 100")
    config = _config(args, ready=False)
    store = _open_read_only_state(config.service.database)
    try:
        status = store.reset_alert_status()
        items = store.latest_reset_alerts(limit=int(args.limit))
    finally:
        store.close()

    now = int(time.time())
    payload = {
        "schema_version": 1,
        "available": bool(status.get("available")),
        "items": [
            {
                "event_id": item["event_key"],
                "event_key": item["event_key"],
                "level": item["level"],
                "evidence": item["evidence"],
                "window": item["window"],
                "advice": item["advice"],
                "created_at": _reset_alert_time(item["created_at"]),
                "expires_at": _reset_alert_time(item["expires_at"]),
                "notified_at": _reset_alert_time(item["notified_at"]),
                **_reset_alert_notification_contract(item, available=bool(status.get("available")), now=now),
                "delivery": _reset_alert_delivery_contract(item["delivery"]),
            }
            for item in items
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0


def _reset_alert_notification_contract(item: dict[str, object], *, available: bool, now: int) -> dict[str, object]:
    parts = str(item.get("event_key") or "").split(":")
    phase = parts[1] if len(parts) == 3 and parts[1] in {
        "upcoming", "announced_available", "watch"
    } else "legacy"
    try:
        valid = item.get("level") in {"A", "B"} and int(item.get("expires_at") or 0) > 0
        active = valid and int(item["expires_at"]) > now
    except (TypeError, ValueError):
        valid = active = False
    reason = "unavailable" if not available else "invalid" if not valid else "active" if active else "expired"
    return {"phase": phase, "notification_eligible": reason == "active", "eligibility_reason": reason}


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
    from .guardian import control_root
    if instance_running(control_root(config)/"guardian.pid") or instance_running(config.service.pid_file):
        raise ConfigError("请先完整退出机器人后再重新绑定，避免两个长连接竞争消息。")
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
    from .guardian import control_root
    if instance_running(control_root(config)/"guardian.pid"):
        from .guardian_channel import GuardianChannel
        channel = GuardianChannel(config,"manual-test",passive=True)
    elif instance_running(config.service.pid_file):
        raise ConfigError("旧业务服务仍运行，请通过受控升级切换守护后再测试，不能另开长连接。")
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


def _artifact_status(args):
    config=_config(args,ready=False)
    from .state import StateStore
    store=StateStore(config.service.database,mode="ro",migrate=False)
    try:
        with store._lock:
            tables={r[0] for r in store._connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'artifact_file_deliveries' not in tables:
                result={'available':False,'reason':'schema_before_artifact_delivery','rows':[]}
            else:
                where=' WHERE thread_id=?' if args.thread_id else ''
                params=(args.thread_id,) if args.thread_id else ()
                total=store._connection.execute('SELECT COUNT(*) FROM artifact_file_deliveries'+where,params).fetchone()[0]
                rows=store._connection.execute('SELECT delivery_id,thread_id,turn_id,file_name,media_kind,sha256,size,state,reason,attempts,notice_state,notice_reason,notice_key FROM artifact_file_deliveries'+where+' ORDER BY created,ordinal LIMIT ? OFFSET ?',(*params,max(1,min(args.limit,500)),max(0,args.offset))).fetchall()
                result={'available':True,'total':total,'offset':max(0,args.offset),'rows':[dict(row) for row in rows]}
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0
    finally:
        store.close()


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
        "session-search": _session_search,
        "repair-thread-titles": _repair_thread_titles,
        "install-notify": _install_notify,
        "install-permission-hook": _install_permission_hook,
        "permission-hook": _permission_hook,
        "uninstall-permission-hook": _uninstall_permission_hook,
        "uninstall-notify": _uninstall_notify,
        "run": _run,
        "worker-run": _run,
        "guardian-run": _guardian_command,
        "guardian-status": _guardian_command,
        "guardian-stop": _guardian_command,
        "guardian-maintenance": _guardian_command,
        "guardian-recover": _guardian_command,
        "worker-recover": _guardian_command,
        "start": _start,
        "stop": _stop,
        "status": _status,
        "reset-alert-status": _reset_alert_status,
        "reset-alert-latest": _reset_alert_latest,
        "gateway-run": _gateway_run,
        "gateway-start": _gateway_start,
        "gateway-recover-owned": _gateway_recover_owned,
        "gateway-stop": _gateway_stop,
        "gateway-status": _gateway_status,
        "register-shared-desktop": _register_shared_desktop,
        "baseline-pre-activation-hooks": _baseline_pre_activation_hooks,
        "discard-stale-pending-replies": _discard_stale_pending_replies,
        "resolve-uncertain": _resolve_uncertain,
        "artifact-status": _artifact_status,
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
