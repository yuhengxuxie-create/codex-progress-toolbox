"""Generic JSON, Feishu, WeCom, and AstrBot webhook notification adapters."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from datetime import datetime
from typing import Any, Mapping, Protocol
from uuid import NAMESPACE_URL, uuid5

from .config import WebhookConfig
from .formatting import (
    BEIJING_TIMEZONE,
    WECOM_MARKDOWN_MAX_BYTES,
    beijing_now,
    format_notification,
    format_notification_limited,
    normalize_report,
)
from .http_client import HttpResponse, JsonHttpClient
from .logging_utils import get_logger, redact_url
from .models import AgentTurnComplete, DeliveryResult, ProgressReport


_LOGGER = get_logger()


class NotificationError(RuntimeError):
    """Raised when a webhook does not accept a notification."""


class _JsonClient(Protocol):
    def post_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        headers: Mapping[str, str] | None = None,
        auth_type: str = "none",
        bearer_token: str = "",
        hmac_secret: str = "",
    ) -> HttpResponse: ...


class WebhookNotifier:
    """Shared bounded transport for concrete webhook payload formats."""

    provider = "generic"

    def __init__(
        self,
        config: WebhookConfig,
        client: _JsonClient | None = None,
    ) -> None:
        self.config = config
        self.client: _JsonClient = client or JsonHttpClient(
            timeout_seconds=config.timeout_seconds,
            max_attempts=config.max_attempts,
            allow_http_localhost=config.allow_http_localhost,
        )

    def _post(self, payload: Mapping[str, Any]) -> HttpResponse:
        _LOGGER.debug(
            "Sending %s webhook to %s",
            self.provider,
            redact_url(self.config.webhook_url),
        )
        try:
            return self.client.post_json(
                self.config.webhook_url,
                payload,
                headers=self.config.headers,
                auth_type=self.config.auth_type,
                bearer_token=self.config.bearer_token,
                hmac_secret=self.config.hmac_secret,
            )
        except Exception as exc:
            _LOGGER.warning(
                "Webhook delivery failed provider=%s error=%s",
                self.provider,
                type(exc).__name__,
            )
            raise

    @staticmethod
    def _optional_json(response: HttpResponse) -> Mapping[str, Any] | None:
        if not response.body:
            return None
        try:
            data = response.json()
        except Exception:
            return None
        return data if isinstance(data, Mapping) else None

    def send(
        self,
        event: AgentTurnComplete,
        report: ProgressReport,
        *,
        now: datetime | None = None,
    ) -> DeliveryResult:
        raise NotImplementedError


class GenericWebhookNotifier(WebhookNotifier):
    """Send a stable event envelope containing the four-line plain-text reminder."""

    provider = "generic"

    def send(
        self,
        event: AgentTurnComplete,
        report: ProgressReport,
        *,
        now: datetime | None = None,
    ) -> DeliveryResult:
        if now is None:
            sent_at = beijing_now()
        elif now.tzinfo is None:
            sent_at = now.replace(tzinfo=BEIJING_TIMEZONE)
        else:
            sent_at = now.astimezone(BEIJING_TIMEZONE)
        normalized = normalize_report(report)
        content = format_notification(event, normalized, sent_at)
        event_id = str(
            uuid5(
                NAMESPACE_URL,
                f"codex-progress-notify:{event.thread_id}:{event.turn_id}",
            )
        )
        response = self._post(
            {
                "schema_version": "1.0",
                "event": "codex.turn.completed",
                "event_id": event_id,
                "conversation_id": event.thread_id,
                "conversation_name": event.display_title,
                "progress": normalized.status,
                "details": normalized.details,
                "sent_at": sent_at.isoformat(timespec="seconds"),
                "timezone": "Asia/Shanghai",
                "text": content,
            }
        )
        _LOGGER.info(
            "Webhook accepted provider=%s status=%d attempts=%d",
            self.provider,
            response.status_code,
            response.attempts,
        )
        return DeliveryResult(
            provider=self.provider,
            status_code=response.status_code,
            attempts=response.attempts,
            response_json=self._optional_json(response),
        )


class FeishuWebhookNotifier(WebhookNotifier):
    """Send a plain-text message through a Feishu custom bot webhook."""

    provider = "feishu"

    @staticmethod
    def _sign(timestamp: int, secret: str) -> str:
        key = f"{timestamp}\n{secret}".encode("utf-8")
        digest = hmac.new(key, digestmod=hashlib.sha256).digest()
        return base64.b64encode(digest).decode("ascii")

    def send(
        self,
        event: AgentTurnComplete,
        report: ProgressReport,
        *,
        now: datetime | None = None,
    ) -> DeliveryResult:
        content = format_notification(event, report, now)
        payload: dict[str, Any] = {
            "msg_type": "text",
            "content": {"text": content},
        }
        if self.config.feishu_signing_secret:
            timestamp = int(time.time())
            payload["timestamp"] = timestamp
            payload["sign"] = self._sign(
                timestamp, self.config.feishu_signing_secret
            )

        response = self._post(payload)
        try:
            data = response.json()
        except Exception as exc:
            raise NotificationError("Feishu returned an invalid JSON response") from exc
        if not isinstance(data, Mapping):
            _LOGGER.warning("Webhook response invalid provider=%s", self.provider)
            raise NotificationError("Feishu returned an invalid response object")

        if "code" in data:
            accepted = data.get("code") == 0
            result_type = type(data.get("code")).__name__
        elif "StatusCode" in data:
            accepted = data.get("StatusCode") == 0
            result_type = type(data.get("StatusCode")).__name__
        else:
            _LOGGER.warning("Webhook response invalid provider=%s", self.provider)
            raise NotificationError("Feishu returned an unrecognized response object")
        if not accepted:
            _LOGGER.warning(
                "Webhook rejected provider=%s result_type=%s",
                self.provider,
                result_type,
            )
            raise NotificationError("Feishu rejected the message")

        _LOGGER.info(
            "Webhook accepted provider=%s status=%d attempts=%d",
            self.provider,
            response.status_code,
            response.attempts,
        )
        return DeliveryResult(
            provider=self.provider,
            status_code=response.status_code,
            attempts=response.attempts,
            response_json=data,
        )


class WeComWebhookNotifier(WebhookNotifier):
    """Send a WeCom group-robot ``markdown_v2`` message."""

    provider = "wecom"

    def send(
        self,
        event: AgentTurnComplete,
        report: ProgressReport,
        *,
        now: datetime | None = None,
    ) -> DeliveryResult:
        try:
            content = format_notification_limited(
                event,
                report,
                max_utf8_bytes=WECOM_MARKDOWN_MAX_BYTES,
                now=now,
            )
        except ValueError as exc:
            raise NotificationError(str(exc)) from exc
        response = self._post(
            {"msgtype": "markdown_v2", "markdown_v2": {"content": content}}
        )
        try:
            data = response.json()
        except Exception as exc:
            raise NotificationError("WeCom returned an invalid JSON response") from exc
        if not isinstance(data, Mapping):
            _LOGGER.warning("Webhook response invalid provider=%s", self.provider)
            raise NotificationError("WeCom returned an invalid response object")
        errcode = data.get("errcode")
        if errcode != 0:
            # errmsg can echo remote/request data, so it is intentionally not
            # included in logs or the public exception.
            _LOGGER.warning(
                "Webhook rejected provider=%s errcode_type=%s",
                self.provider,
                type(errcode).__name__,
            )
            raise NotificationError(f"WeCom rejected the message (errcode={errcode!r})")
        _LOGGER.info(
            "Webhook accepted provider=%s status=%d attempts=%d",
            self.provider,
            response.status_code,
            response.attempts,
        )
        return DeliveryResult(
            provider=self.provider,
            status_code=response.status_code,
            attempts=response.attempts,
            response_json=data,
        )


class AstrBotWebhookNotifier(WebhookNotifier):
    """Send a four-line message through AstrBot's authenticated push API."""

    provider = "astrbot"

    def send(
        self,
        event: AgentTurnComplete,
        report: ProgressReport,
        *,
        now: datetime | None = None,
    ) -> DeliveryResult:
        content = format_notification(event, report, now)
        response = self._post(
            {"umo": self.config.target_umo, "message": content}
        )
        try:
            data = response.json()
        except Exception as exc:
            raise NotificationError("AstrBot returned an invalid JSON response") from exc
        if not isinstance(data, Mapping):
            _LOGGER.warning("Webhook response invalid provider=%s", self.provider)
            raise NotificationError("AstrBot returned an invalid response object")
        if data.get("status") != "ok":
            # Remote response fields can contain request data or implementation
            # details, so neither logs nor the public exception echo them.
            _LOGGER.warning("Webhook rejected provider=%s", self.provider)
            raise NotificationError("AstrBot rejected the message")
        _LOGGER.info(
            "Webhook accepted provider=%s status=%d attempts=%d",
            self.provider,
            response.status_code,
            response.attempts,
        )
        return DeliveryResult(
            provider=self.provider,
            status_code=response.status_code,
            attempts=response.attempts,
            response_json=data,
        )


def create_notifier(
    config: WebhookConfig,
    client: _JsonClient | None = None,
) -> WebhookNotifier:
    if config.provider == "astrbot":
        return AstrBotWebhookNotifier(config, client)
    if config.provider == "feishu":
        return FeishuWebhookNotifier(config, client)
    if config.provider == "wecom":
        return WeComWebhookNotifier(config, client)
    if config.provider == "generic":
        return GenericWebhookNotifier(config, client)
    raise NotificationError(f"unsupported notification provider: {config.provider!r}")


def send_notification(
    config: WebhookConfig,
    event: AgentTurnComplete,
    report: ProgressReport,
    *,
    now: datetime | None = None,
    client: _JsonClient | None = None,
) -> DeliveryResult:
    """Create the configured adapter and send one completed-turn message."""

    return create_notifier(config, client).send(event, report, now=now)
