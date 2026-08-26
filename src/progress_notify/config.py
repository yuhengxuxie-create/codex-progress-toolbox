"""Configuration loading with portable environment-variable placeholders."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlsplit


DEFAULT_CONFIG_PATH = Path("config.local.json")


class ConfigError(ValueError):
    """Raised when the local configuration is malformed or unsafe."""


def _expand_string(template: str, environ: Mapping[str, str]) -> str:
    """Expand ``${VAR}`` and ``${VAR:-default}`` without shell evaluation.

    An unset plain placeholder expands to an empty string.  This deliberately
    permits optional secrets such as ``OPENAI_API_KEY`` to be absent; the
    classifier then uses its bounded, human-readable unknown-state fallback.
    """

    result: list[str] = []
    cursor = 0
    while cursor < len(template):
        start = template.find("${", cursor)
        if start < 0:
            result.append(template[cursor:])
            break
        result.append(template[cursor:start])
        # A default may itself be a JSON object (the shipped config uses
        # ``${HEADERS:-{}}``), so locate the placeholder's closing brace while
        # respecting nested braces and JSON-style quoted strings.
        end = start + 2
        nested_braces = 0
        quote = ""
        escaped = False
        while end < len(template):
            character = template[end]
            if escaped:
                escaped = False
            elif character == "\\" and quote:
                escaped = True
            elif quote:
                if character == quote:
                    quote = ""
            elif character in {'"', "'"}:
                quote = character
            elif character == "{":
                nested_braces += 1
            elif character == "}":
                if nested_braces == 0:
                    break
                nested_braces -= 1
            end += 1
        if end < 0:
            raise ConfigError("unterminated environment placeholder")
        if end >= len(template):
            raise ConfigError("unterminated environment placeholder")

        expression = template[start + 2 : end]
        if ":-" in expression:
            name, default = expression.split(":-", 1)
            value = environ.get(name, "")
            result.append(value if value else default)
        else:
            name = expression
            result.append(environ.get(name, ""))

        if not name or not (name[0].isalpha() or name[0] == "_"):
            raise ConfigError(f"invalid environment variable name: {name!r}")
        if not all(character.isalnum() or character == "_" for character in name):
            raise ConfigError(f"invalid environment variable name: {name!r}")
        cursor = end + 1
    return "".join(result)


def expand_placeholders(value: Any, environ: Mapping[str, str] | None = None) -> Any:
    """Recursively expand supported placeholders in JSON-compatible values."""

    source = os.environ if environ is None else environ
    if isinstance(value, str):
        return _expand_string(value, source)
    if isinstance(value, list):
        return [expand_placeholders(item, source) for item in value]
    if isinstance(value, dict):
        return {
            str(key): expand_placeholders(item, source) for key, item in value.items()
        }
    return value


def _as_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"{label} must be a JSON object")
    return value


def _as_str(value: Any, label: str, *, default: str = "") -> str:
    if value is None:
        return default
    if not isinstance(value, (str, int, float, bool)):
        raise ConfigError(f"{label} must be a string")
    return str(value).strip()


def _as_bool(value: Any, label: str, *, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ConfigError(f"{label} must be true or false")


def _as_float(
    value: Any,
    label: str,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    if value is None or value == "":
        result = default
    else:
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{label} must be a number") from exc
    if not minimum <= result <= maximum:
        raise ConfigError(f"{label} must be between {minimum} and {maximum}")
    return result


def _as_int(
    value: Any,
    label: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    if value is None or value == "":
        result = default
    else:
        try:
            result = int(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{label} must be an integer") from exc
    if not minimum <= result <= maximum:
        raise ConfigError(f"{label} must be between {minimum} and {maximum}")
    return result


def _parse_json_object(value: Any, label: str) -> dict[str, str]:
    if value is None or value == "":
        return {}
    parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{label} must contain a JSON object") from exc
    if not isinstance(parsed, Mapping):
        raise ConfigError(f"{label} must be a JSON object")
    result: dict[str, str] = {}
    for raw_key, raw_value in parsed.items():
        key = str(raw_key).strip()
        if not key:
            raise ConfigError(f"{label} contains an empty key")
        if isinstance(raw_value, (dict, list)):
            raise ConfigError(f"{label}.{key} must be a scalar value")
        result[key] = str(raw_value)
    return result


def _parse_thread_ids(value: Any) -> frozenset[str]:
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                value = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ConfigError("thread_ids contains invalid JSON") from exc
        else:
            value = text.split(",") if text else []
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise ConfigError("thread_ids must be an array or comma-separated string")

    result: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError("thread_ids entries must be non-empty strings")
        result.add(item.strip())
    if not result:
        raise ConfigError("thread_ids must contain at least one exact Codex thread ID")
    return frozenset(result)


def _validate_astrbot_umo(value: str) -> str:
    """Validate AstrBot's stable ``platform:type:session`` string shape."""

    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ConfigError("notification.target_umo contains a control character")
    # `/sid` renders the copyable value as ``UMO: 「platform:type:session」``.
    # Treat those display brackets as presentation, not as part of the UMO.
    if value.startswith("「") and value.endswith("」"):
        value = value[1:-1].strip()
    if "「" in value or "」" in value:
        raise ConfigError(
            "notification.target_umo contains unmatched AstrBot display brackets"
        )
    parts = value.split(":", 2)
    if len(parts) != 3 or not all(part.strip() for part in parts):
        raise ConfigError(
            "notification.target_umo must be copied exactly from AstrBot /sid"
        )
    if parts[1] not in {"FriendMessage", "GroupMessage", "OtherMessage"}:
        raise ConfigError(
            "notification.target_umo has an unsupported AstrBot message type"
        )
    return value


@dataclass(frozen=True, slots=True)
class WebhookConfig:
    provider: str
    webhook_url: str
    auth_type: str = "none"
    bearer_token: str = ""
    hmac_secret: str = ""
    feishu_signing_secret: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)
    allow_http_localhost: bool = False
    timeout_seconds: float = 10.0
    max_attempts: int = 3
    target_umo: str = ""

    def __post_init__(self) -> None:
        provider = self.provider.strip().casefold()
        if provider in {"wechat", "wechat-work", "wework", "wecom_robot"}:
            provider = "wecom"
        if provider in {"lark", "feishu_robot"}:
            provider = "feishu"
        if provider not in {"astrbot", "feishu", "generic", "wecom"}:
            raise ConfigError(
                "notification.provider must be astrbot, feishu, generic, or wecom"
            )
        auth_type = self.auth_type.strip().casefold() or "none"
        if auth_type == "hmac":
            auth_type = "hmac-sha256"
        if auth_type not in {"none", "bearer", "hmac-sha256"}:
            raise ConfigError(
                "notification.auth_type must be none, bearer, or hmac-sha256"
            )
        target_umo = self.target_umo.strip()
        if provider == "astrbot" and auth_type != "bearer":
            raise ConfigError("notification.auth_type must be bearer for astrbot")
        if provider == "feishu" and auth_type != "none":
            raise ConfigError("notification.auth_type must be none for feishu")
        if provider == "astrbot" and not target_umo:
            raise ConfigError("notification.target_umo is required for astrbot")
        if provider == "astrbot":
            target_umo = _validate_astrbot_umo(target_umo)
        if auth_type == "bearer" and not self.bearer_token:
            raise ConfigError("notification.bearer_token is required for bearer auth")
        if auth_type == "hmac-sha256" and not self.hmac_secret:
            raise ConfigError("notification.hmac_secret is required for hmac auth")
        if not self.webhook_url:
            raise ConfigError("notification.webhook_url is required")
        if not 0.5 <= float(self.timeout_seconds) <= 120.0:
            raise ConfigError("notification.timeout_seconds must be between 0.5 and 120")
        if not 1 <= int(self.max_attempts) <= 5:
            raise ConfigError("notification.max_attempts must be between 1 and 5")
        from .http_client import HttpRequestError, validate_outbound_url

        try:
            validate_outbound_url(
                self.webhook_url,
                allow_http_localhost=self.allow_http_localhost,
            )
        except HttpRequestError as exc:
            raise ConfigError(str(exc)) from exc
        if provider == "feishu":
            parts = urlsplit(self.webhook_url)
            hostname = (parts.hostname or "").casefold()
            official_host = hostname in {"open.feishu.cn", "open.larksuite.com"}
            local_test_host = self.allow_http_localhost and hostname in {
                "127.0.0.1",
                "localhost",
                "::1",
            }
            if not official_host and not local_test_host:
                raise ConfigError(
                    "feishu webhook host must be open.feishu.cn or open.larksuite.com"
                )
            if official_host and not parts.path.startswith(
                "/open-apis/bot/v2/hook/"
            ):
                raise ConfigError("feishu webhook path is not a custom-bot hook")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "target_umo", target_umo)
        object.__setattr__(self, "auth_type", auth_type)
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))

    @property
    def url(self) -> str:
        """Compatibility alias for callers that use the concise name."""

        return self.webhook_url


@dataclass(frozen=True, slots=True)
class ClassifierConfig:
    mode: str = "auto"
    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-5-mini"
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        mode = self.mode.strip().casefold() or "auto"
        if mode not in {"auto", "openai", "disabled"}:
            raise ConfigError("classifier.mode must be auto, openai, or disabled")
        if not self.base_url:
            raise ConfigError("classifier.base_url is required")
        if not self.model:
            raise ConfigError("classifier.model is required")
        if not 1.0 <= float(self.timeout_seconds) <= 180.0:
            raise ConfigError("classifier.timeout_seconds must be between 1 and 180")
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "base_url", self.base_url.rstrip("/"))


@dataclass(frozen=True, slots=True)
class CodexConfig:
    command: str = "codex"
    title_overrides: Mapping[str, str] = field(default_factory=dict)
    request_timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        if not self.command:
            raise ConfigError("codex.command is required")
        if not 0.5 <= float(self.request_timeout_seconds) <= 120.0:
            raise ConfigError(
                "codex.request_timeout_seconds must be between 0.5 and 120"
            )
        object.__setattr__(
            self, "title_overrides", MappingProxyType(dict(self.title_overrides))
        )

    @property
    def timeout_seconds(self) -> float:
        return self.request_timeout_seconds


@dataclass(frozen=True, slots=True)
class AppConfig:
    thread_ids: frozenset[str]
    notification: WebhookConfig
    classifier: ClassifierConfig = field(default_factory=ClassifierConfig)
    codex: CodexConfig = field(default_factory=CodexConfig)
    log_file: Path = Path(".state/progress-notify.log")

    def matches_thread(self, thread_id: str) -> bool:
        """Return an exact, case-sensitive set membership result."""

        return thread_id in self.thread_ids


def _build_config(data: Mapping[str, Any], *, config_dir: Path) -> AppConfig:
    notification_data = _as_mapping(data.get("notification"), "notification")
    classifier_data = _as_mapping(data.get("classifier"), "classifier")
    codex_data = _as_mapping(data.get("codex"), "codex")

    headers_value = notification_data.get(
        "headers", notification_data.get("headers_json", {})
    )
    notification = WebhookConfig(
        provider=_as_str(
            notification_data.get("provider"),
            "notification.provider",
            default="generic",
        ),
        webhook_url=_as_str(
            notification_data.get("webhook_url"), "notification.webhook_url"
        ),
        target_umo=_as_str(
            notification_data.get("target_umo"), "notification.target_umo"
        ),
        auth_type=_as_str(
            notification_data.get("auth_type"),
            "notification.auth_type",
            default="none",
        ),
        bearer_token=_as_str(
            notification_data.get("bearer_token"), "notification.bearer_token"
        ),
        hmac_secret=_as_str(
            notification_data.get("hmac_secret"), "notification.hmac_secret"
        ),
        feishu_signing_secret=_as_str(
            notification_data.get("feishu_signing_secret"),
            "notification.feishu_signing_secret",
        ),
        headers=_parse_json_object(headers_value, "notification.headers"),
        allow_http_localhost=_as_bool(
            notification_data.get("allow_http_localhost"),
            "notification.allow_http_localhost",
        ),
        timeout_seconds=_as_float(
            notification_data.get("timeout_seconds"),
            "notification.timeout_seconds",
            default=10.0,
            minimum=0.5,
            maximum=120.0,
        ),
        max_attempts=_as_int(
            notification_data.get("max_attempts"),
            "notification.max_attempts",
            default=3,
            minimum=1,
            maximum=5,
        ),
    )

    classifier = ClassifierConfig(
        mode=_as_str(
            classifier_data.get("mode"), "classifier.mode", default="auto"
        ),
        api_key=_as_str(classifier_data.get("api_key"), "classifier.api_key"),
        base_url=_as_str(
            classifier_data.get("base_url"),
            "classifier.base_url",
            default="https://api.openai.com/v1",
        ),
        model=_as_str(
            classifier_data.get("model"),
            "classifier.model",
            default="gpt-5-mini",
        ),
        timeout_seconds=_as_float(
            classifier_data.get("timeout_seconds"),
            "classifier.timeout_seconds",
            default=30.0,
            minimum=1.0,
            maximum=180.0,
        ),
    )

    title_overrides_value = codex_data.get(
        "title_overrides", codex_data.get("title_overrides_json", {})
    )
    codex = CodexConfig(
        command=_as_str(codex_data.get("command"), "codex.command", default="codex"),
        title_overrides=_parse_json_object(
            title_overrides_value, "codex.title_overrides"
        ),
        request_timeout_seconds=_as_float(
            codex_data.get("request_timeout_seconds"),
            "codex.request_timeout_seconds",
            default=10.0,
            minimum=0.5,
            maximum=120.0,
        ),
    )

    log_value = _as_str(
        data.get("log_file"), "log_file", default=".state/progress-notify.log"
    )
    log_file = Path(log_value).expanduser()
    if not log_file.is_absolute():
        log_file = config_dir / log_file

    return AppConfig(
        thread_ids=_parse_thread_ids(data.get("thread_ids")),
        notification=notification,
        classifier=classifier,
        codex=codex,
        log_file=log_file.resolve(),
    )


def load_config(
    path: str | os.PathLike[str] = DEFAULT_CONFIG_PATH,
    environ: Mapping[str, str] | None = None,
) -> AppConfig:
    """Load and validate a portable ``config.local.json`` file."""

    config_path = Path(path).expanduser().resolve()
    try:
        raw_text = config_path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ConfigError(f"cannot read config file: {config_path}") from exc
    try:
        raw_data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"invalid JSON in config file at line {exc.lineno}, column {exc.colno}"
        ) from exc
    if not isinstance(raw_data, Mapping):
        raise ConfigError("configuration root must be a JSON object")

    expanded = expand_placeholders(raw_data, environ)
    return _build_config(expanded, config_dir=config_path.parent)
