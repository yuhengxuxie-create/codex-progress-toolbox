"""Public API for Codex Progress Notify.

This module must stay lightweight.  The CLI's direct Codex notify entrypoint
imports :mod:`progress_notify.dispatcher` first so it can forward any previously
configured notify command before loading our classifier or webhook stack.  Lazy
PEP 562 exports preserve a convenient public API without breaking that recovery
guarantee if an optional runtime module is damaged.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any


__version__ = "1.2.0"

_EXPORTS: dict[str, tuple[str, str]] = {
    "AGENT_TURN_COMPLETE": ("models", "AGENT_TURN_COMPLETE"),
    "ALL_CLASSIFIER_STATUSES": ("models", "ALL_CLASSIFIER_STATUSES"),
    "STANDARD_STATUSES": ("models", "STANDARD_STATUSES"),
    "AgentTurnComplete": ("models", "AgentTurnComplete"),
    "DeliveryResult": ("models", "DeliveryResult"),
    "EventValidationError": ("models", "EventValidationError"),
    "ProgressReport": ("models", "ProgressReport"),
    "ProgressStatus": ("models", "ProgressStatus"),
    "parse_agent_turn_complete": ("models", "parse_agent_turn_complete"),
    "AppConfig": ("config", "AppConfig"),
    "ClassifierConfig": ("config", "ClassifierConfig"),
    "CodexConfig": ("config", "CodexConfig"),
    "ConfigError": ("config", "ConfigError"),
    "WebhookConfig": ("config", "WebhookConfig"),
    "expand_placeholders": ("config", "expand_placeholders"),
    "load_config": ("config", "load_config"),
    "BEIJING_TIMEZONE": ("formatting", "BEIJING_TIMEZONE"),
    "DETAILS_MAX_CHARS": ("formatting", "DETAILS_MAX_CHARS"),
    "STANDARD_DETAILS_MAX_CHARS": ("formatting", "STANDARD_DETAILS_MAX_CHARS"),
    "STATUS_MAX_CHARS": ("formatting", "STATUS_MAX_CHARS"),
    "WECOM_MARKDOWN_MAX_BYTES": ("formatting", "WECOM_MARKDOWN_MAX_BYTES"),
    "beijing_now": ("formatting", "beijing_now"),
    "format_notification": ("formatting", "format_notification"),
    "format_notification_limited": ("formatting", "format_notification_limited"),
    "close_logging": ("logging_utils", "close_logging"),
    "configure_logging": ("logging_utils", "configure_logging"),
    "get_logger": ("logging_utils", "get_logger"),
    "ProgressClassifier": ("classifier", "ProgressClassifier"),
    "classify_event": ("classifier", "classify_event"),
    "AstrBotWebhookNotifier": ("notifiers", "AstrBotWebhookNotifier"),
    "FeishuWebhookNotifier": ("notifiers", "FeishuWebhookNotifier"),
    "GenericWebhookNotifier": ("notifiers", "GenericWebhookNotifier"),
    "NotificationError": ("notifiers", "NotificationError"),
    "WeComWebhookNotifier": ("notifiers", "WeComWebhookNotifier"),
    "WebhookNotifier": ("notifiers", "WebhookNotifier"),
    "create_notifier": ("notifiers", "create_notifier"),
    "send_notification": ("notifiers", "send_notification"),
}

__all__ = ["__version__", *_EXPORTS]


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = target
    value = getattr(import_module(f".{module_name}", __name__), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
