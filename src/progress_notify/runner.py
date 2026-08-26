"""End-to-end handling for one already-forwarded Codex notify event."""

from __future__ import annotations

import os
import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from .classifier import classify_event
from .codex_client import CodexAppServerClient
from .codex_index import read_indexed_thread_name
from .config import AppConfig, load_config
from .formatting import beijing_now, format_notification
from .logging_utils import close_logging, configure_logging
from .models import AgentTurnComplete, DeliveryResult, ProgressReport, parse_agent_turn_complete
from .notifiers import send_notification


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.local.json"


def _thread_ref(thread_id: str) -> str:
    return hashlib.sha256(thread_id.encode("utf-8", errors="replace")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class RunResult:
    outcome: str
    reason: str = ""
    message: str = ""
    event: AgentTurnComplete | None = None
    report: ProgressReport | None = None
    delivery: DeliveryResult | None = None

    @property
    def sent(self) -> bool:
        return self.outcome == "sent"


def resolve_config_path(path: str | os.PathLike[str] | None = None) -> Path:
    selected = path or os.environ.get("PROGRESS_NOTIFY_CONFIG") or DEFAULT_CONFIG_PATH
    return Path(selected).expanduser().resolve()


def _resolve_title(
    event: AgentTurnComplete,
    config: AppConfig,
    app_server_client: CodexAppServerClient | None = None,
) -> AgentTurnComplete:
    name: str | None = None
    try:
        if app_server_client is not None:
            name = app_server_client.get_thread_name(event.thread_id)
        else:
            with CodexAppServerClient(
                config.codex.command,
                config.codex.request_timeout_seconds,
            ) as client:
                name = client.get_thread_name(event.thread_id)
    except Exception:
        # Notification delivery remains useful when the local App Server is
        # unavailable. Remaining title sources are local, read-only fallbacks.
        name = None

    title = (
        name
        or config.codex.title_overrides.get(event.thread_id)
        or event.thread_title
        or read_indexed_thread_name(event.thread_id)
        or f"未命名对话（{event.thread_id}）"
    )
    return replace(event, thread_title=title)


def handle_event(
    payload: Mapping[str, Any],
    config_path: str | os.PathLike[str] | None = None,
    *,
    dry_run: bool = False,
    client: Any = None,
    app_server_client: CodexAppServerClient | None = None,
) -> RunResult:
    """Validate, whitelist, classify, format, and optionally deliver one event.

    Forwarding the pre-existing Codex notify command is deliberately performed
    by :mod:`progress_notify.dispatcher` *before this module is imported*.
    """

    # Unsupported lifecycle events need neither local configuration nor a log
    # file. This also keeps the exact event gate operational during setup.
    if not isinstance(payload, Mapping) or payload.get("type") != "agent-turn-complete":
        return RunResult("ignored", "unsupported-event-type")

    config = load_config(resolve_config_path(config_path))
    secrets = [
        config.classifier.api_key,
        config.notification.bearer_token,
        config.notification.hmac_secret,
        config.notification.feishu_signing_secret,
        config.notification.webhook_url,
        *config.notification.headers.values(),
    ]
    try:
        logger = configure_logging(config.log_file, secrets=secrets)
    except OSError:
        logger = configure_logging(None, secrets=secrets)
        logger.warning("Unable to open configured log file; using standard error")
    try:
        return _handle_loaded_event(
            payload,
            config,
            logger,
            dry_run=dry_run,
            client=client,
            app_server_client=app_server_client,
        )
    finally:
        close_logging(logger)


def _handle_loaded_event(
    payload: Mapping[str, Any],
    config: AppConfig,
    logger: Any,
    *,
    dry_run: bool,
    client: Any,
    app_server_client: CodexAppServerClient | None,
) -> RunResult:
    try:
        event = parse_agent_turn_complete(payload)
    except Exception as exc:
        logger.error("event failed stage=parse error=%s", type(exc).__name__)
        raise
    thread_ref = _thread_ref(event.thread_id)
    if not config.matches_thread(event.thread_id):
        logger.info(
            "event ignored reason=thread-not-allowlisted thread_ref=%s",
            thread_ref,
        )
        return RunResult("ignored", "thread-not-allowlisted", event=event)

    try:
        event = _resolve_title(event, config, app_server_client)
        report = classify_event(event, config.classifier, client=client)
        now = beijing_now()
        message = format_notification(event, report, now=now)
        if dry_run:
            logger.info("event dry-run thread_ref=%s", thread_ref)
            return RunResult(
                "dry-run",
                "delivery-disabled",
                message=message,
                event=event,
                report=report,
            )

        delivery = send_notification(
            config.notification,
            event,
            report,
            now=now,
            client=client,
        )
    except Exception as exc:
        logger.error(
            "event failed stage=processing thread_ref=%s error=%s",
            thread_ref,
            type(exc).__name__,
        )
        raise
    logger.info(
        "event sent thread_ref=%s provider=%s status_code=%s attempts=%s",
        thread_ref,
        delivery.provider,
        delivery.status_code,
        delivery.attempts,
    )
    return RunResult(
        "sent",
        message=message,
        event=event,
        report=report,
        delivery=delivery,
    )


def dry_run_event(
    payload: Mapping[str, Any],
    config_path: str | os.PathLike[str] | None = None,
    *,
    client: Any = None,
    app_server_client: CodexAppServerClient | None = None,
) -> RunResult:
    return handle_event(
        payload,
        config_path,
        dry_run=True,
        client=client,
        app_server_client=app_server_client,
    )
