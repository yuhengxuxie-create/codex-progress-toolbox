"""Command-line interface and Codex notify argv entrypoint."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.local.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="progress-notify",
        description="Send a notification after selected Codex turns complete.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate installation and configuration")
    validate.add_argument("--config", type=Path, default=None)
    validate.add_argument(
        "--installation-only",
        action="store_true",
        help="check runtime/imports without requiring delivery configuration",
    )

    threads = subparsers.add_parser("list-threads", help="list stored Codex threads")
    threads.add_argument("--limit", type=int, default=100)
    threads.add_argument("--json", action="store_true", dest="as_json")

    test = subparsers.add_parser("send-test", help="send one test notification")
    test.add_argument("--config", type=Path, default=None)
    test.add_argument("--dry-run", action="store_true")

    dry_run = subparsers.add_parser("dry-run", help="render an event without delivery")
    dry_run.add_argument("event_json", nargs="?", help="one agent-turn-complete JSON object")
    dry_run.add_argument("--config", type=Path, default=None)
    dry_run.add_argument("--thread-id", default=None)
    dry_run.add_argument("--message", default="进度通知 dry-run 测试。")

    install_parser = subparsers.add_parser("install", help="install the Codex notify wrapper")
    install_parser.add_argument("--codex-home", type=Path, default=None)
    install_parser.add_argument("--python", type=Path, default=None)

    uninstall_parser = subparsers.add_parser("uninstall", help="restore the previous notify command")
    uninstall_parser.add_argument("--codex-home", type=Path, default=None)

    return parser


def _selected_config(path: Path | None) -> Path:
    selected = path or os.environ.get("PROGRESS_NOTIFY_CONFIG") or DEFAULT_CONFIG_PATH
    return Path(selected).expanduser().resolve()


def _safe_error(label: str, exc: BaseException) -> None:
    # Exceptions from HTTP libraries can contain full endpoint URLs. Keep CLI
    # diagnostics useful without leaking payloads, credentials, or endpoints.
    detail = ""
    try:
        from .http_client import HttpRequestError

        if isinstance(exc, HttpRequestError):
            if exc.status_code is not None:
                detail = (
                    f"endpoint returned HTTP {exc.status_code}; "
                    f"attempts={exc.attempts}"
                )
            elif exc.retryable:
                detail = (
                    "endpoint unreachable or timed out; "
                    f"attempts={exc.attempts}; check that AstrBot is running "
                    "and the configured port is correct"
                )
            else:
                detail = str(exc)
    except Exception:
        detail = ""
    suffix = f": {detail}" if detail else ""
    print(f"{label}: {type(exc).__name__}{suffix}", file=sys.stderr)
    path = PROJECT_ROOT / ".state" / "progress-notify-errors.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(
                f"{datetime.now(timezone.utc).isoformat()} {label.split()[0]} "
                f"{type(exc).__name__}\n"
            )
    except OSError:
        pass


def _direct_notify(raw_argument: str) -> int:
    # Import only the preservation layer. It forwards the original command
    # before it imports runner/classifier/notifier modules.
    from .dispatcher import dispatch_json_argument

    try:
        dispatch_json_argument(raw_argument)
    except Exception as exc:
        _safe_error("progress-notify failed", exc)
        return 1
    return 0


def _validate(config_path: Path | None, installation_only: bool) -> int:
    checks: list[tuple[str, bool, str]] = []
    checks.append(
        (
            "Python >= 3.11",
            sys.version_info >= (3, 11),
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        )
    )
    entrypoint = PROJECT_ROOT / "progress-notify.py"
    checks.append(("入口脚本", entrypoint.is_file(), str(entrypoint)))
    try:
        from . import codex_client as _codex_client
        from . import dispatcher as _dispatcher
        from . import installer as _installer

        del _codex_client, _dispatcher, _installer
        checks.append(("集成模块", True, "可导入"))
    except Exception as exc:
        checks.append(("集成模块", False, type(exc).__name__))

    if not installation_only:
        try:
            from .config import load_config

            config = load_config(_selected_config(config_path))
            checks.append(("配置文件", True, "有效"))
            checks.append(("线程白名单", bool(config.thread_ids), f"{len(config.thread_ids)} 个精确 ID"))
            checks.append(("通知提供方", True, config.notification.provider))
            try:
                from .codex_client import CodexAppServerClient

                with CodexAppServerClient(
                    config.codex.command,
                    config.codex.request_timeout_seconds,
                ) as codex:
                    codex.list_threads(limit=1)
                checks.append(("Codex App Server", True, "握手成功"))
            except Exception as exc:
                checks.append(("Codex App Server", False, type(exc).__name__))
        except Exception as exc:
            checks.append(("配置文件", False, type(exc).__name__))

    for name, ok, detail in checks:
        print(f"[{'OK' if ok else 'FAIL'}] {name}: {detail}")
    return 0 if all(item[1] for item in checks) else 1


def _list_threads(limit: int, as_json: bool) -> int:
    from .codex_client import CodexAppServerClient

    command = os.environ.get("PROGRESS_CODEX_COMMAND", "codex").strip()
    if not command:
        raise ValueError("PROGRESS_CODEX_COMMAND cannot be empty")
    raw_timeout = os.environ.get("PROGRESS_CODEX_TIMEOUT_SECONDS", "10")
    try:
        timeout = float(raw_timeout)
    except ValueError as exc:
        raise ValueError("PROGRESS_CODEX_TIMEOUT_SECONDS must be a number") from exc
    if not 0.5 <= timeout <= 120:
        raise ValueError("PROGRESS_CODEX_TIMEOUT_SECONDS must be between 0.5 and 120")
    with CodexAppServerClient(
        command,
        timeout,
    ) as client:
        threads = client.list_threads(limit=limit)
    if as_json:
        public = [
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "preview": item.get("preview"),
                "updatedAt": item.get("updatedAt"),
            }
            for item in threads
        ]
        print(json.dumps(public, ensure_ascii=False, indent=2))
        return 0
    if not threads:
        print("未找到 Codex 对话。")
        return 0
    for item in threads:
        thread_id = item.get("id", "")
        title = item.get("name") or item.get("preview") or "未命名对话"
        title = " ".join(str(title).split())
        if len(title) > 120:
            title = title[:119].rstrip() + "…"
        print(f"{thread_id}\t{title}")
    return 0


def _send_test(config_path: Path | None, dry_run: bool) -> int:
    from .config import load_config
    from .formatting import beijing_now, format_notification
    from .models import AgentTurnComplete, ProgressReport
    from .notifiers import send_notification

    config = load_config(_selected_config(config_path))
    thread_id = sorted(config.thread_ids)[0]
    title = config.codex.title_overrides.get(thread_id) or "进度通知测试"
    event = AgentTurnComplete(
        thread_id=thread_id,
        thread_title=title,
        last_assistant_message="进度通知安装测试已完成。",
        turn_id="send-test",
    )
    report = ProgressReport("完成", "进度通知测试消息已生成，用于验证通知通道配置。")
    now = beijing_now()
    message = format_notification(event, report, now=now)
    if dry_run:
        print(message)
        return 0
    result = send_notification(config.notification, event, report, now=now)
    print(f"测试消息发送成功（{result.provider}, HTTP {result.status_code}）。")
    return 0


def _dry_run(
    config_path: Path | None,
    event_json: str | None,
    thread_id: str | None,
    message: str,
) -> int:
    from .config import load_config
    from .runner import handle_event

    selected = _selected_config(config_path)
    if event_json is None:
        config = load_config(selected)
        selected_thread = thread_id or sorted(config.thread_ids)[0]
        payload = {
            "type": "agent-turn-complete",
            "thread-id": selected_thread,
            "turn-id": "dry-run",
            "input-messages": ["dry-run"],
            "last-assistant-message": message,
            "cwd": str(PROJECT_ROOT),
        }
    else:
        value = json.loads(event_json)
        if not isinstance(value, dict):
            raise ValueError("event_json must be a JSON object")
        payload = value
    result = handle_event(payload, selected, dry_run=True)
    if result.outcome == "ignored":
        print(f"事件未处理：{result.reason}")
        return 2
    print(result.message)
    return 0


def _install(codex_home: Path | None, python_path: Path | None) -> int:
    from .installer import install

    result = install(
        PROJECT_ROOT,
        codex_home,
        python_path,
    )
    public = {
        "status": result.get("status"),
        "changed": bool(result.get("changed")),
        "config_path": result.get("config_path"),
        "backup_path": result.get("backup_path"),
        "preserved_existing_notify": bool(result.get("had_original_notify")),
    }
    print(json.dumps(public, ensure_ascii=False, indent=2))
    return 0


def _uninstall(codex_home: Path | None) -> int:
    from .installer import uninstall

    result = uninstall(PROJECT_ROOT, codex_home)
    public = {
        "status": result.get("status"),
        "changed": bool(result.get("changed")),
        "config_path": result.get("config_path"),
        "restored_previous_notify": result.get("status") == "uninstalled",
    }
    if result.get("message"):
        public["message"] = result["message"]
    print(json.dumps(public, ensure_ascii=False, indent=2))
    return 2 if result.get("status") == "externally-modified" else 0


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 1 and args[0].lstrip().startswith("{"):
        return _direct_notify(args[0])

    namespace = _parser().parse_args(args)
    try:
        if namespace.command == "validate":
            return _validate(namespace.config, namespace.installation_only)
        if namespace.command == "list-threads":
            return _list_threads(namespace.limit, namespace.as_json)
        if namespace.command == "send-test":
            return _send_test(namespace.config, namespace.dry_run)
        if namespace.command == "dry-run":
            return _dry_run(
                namespace.config,
                namespace.event_json,
                namespace.thread_id,
                namespace.message,
            )
        if namespace.command == "install":
            return _install(namespace.codex_home, namespace.python)
        if namespace.command == "uninstall":
            return _uninstall(namespace.codex_home)
    except Exception as exc:
        _safe_error(f"{namespace.command} failed", exc)
        return 1
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
