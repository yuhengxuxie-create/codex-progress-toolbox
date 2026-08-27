"""集中加载并严格校验 ``config.yaml``。"""

from __future__ import annotations

import os
import re
from urllib.parse import urlsplit
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from .codex_app_tools import default_codex_desktop_log_dir


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(ValueError):
    """配置不完整、不安全或类型错误。"""


def _expand(value: Any, environ: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            return environ.get(name) or (default if default is not None else "")
        return _ENV_PATTERN.sub(replace, value)
    if isinstance(value, list):
        return [_expand(item, environ) for item in value]
    if isinstance(value, dict):
        return {str(key): _expand(item, environ) for key, item in value.items()}
    return value


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"{label} 必须是映射")
    return value


def _shared_ws_url(value: Any) -> str:
    """只接受固定 IPv4 回环 WebSocket，避免误绑定局域网或公网。"""

    text = str(value or "ws://127.0.0.1:6230").strip()
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError as exc:
        raise ConfigError("codex.shared_websocket_url 端口格式无效") from exc
    if (
        parts.scheme.casefold() != "ws"
        or parts.hostname != "127.0.0.1"
        or port is None
        or not 1024 <= port <= 65535
        or parts.username is not None
        or parts.password is not None
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
    ):
        raise ConfigError(
            "codex.shared_websocket_url 只能是 ws://127.0.0.1:1024-65535/"
        )
    return f"ws://127.0.0.1:{port}"


def _strings(value: Any, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ConfigError(f"{label} 必须是字符串数组")
    result = tuple(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))
    return result


def _number(value: Any, label: str, default: float, low: float, high: float) -> float:
    if value in (None, ""):
        result = default
    else:
        if isinstance(value, bool):
            raise ConfigError(f"{label} 必须是数字，不能使用布尔值")
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{label} 必须是数字") from exc
    if not low <= result <= high:
        raise ConfigError(f"{label} 必须介于 {low} 和 {high} 之间")
    return result


def _integer(value: Any, label: str, default: int, low: int, high: int) -> int:
    result = int(_number(value, label, default, low, high))
    if value not in (None, "") and float(value) != result:
        raise ConfigError(f"{label} 必须是整数")
    return result


def _boolean(value: Any, label: str, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.casefold() in {"true", "yes", "1", "on"}:
        return True
    if isinstance(value, str) and value.casefold() in {"false", "no", "0", "off"}:
        return False
    raise ConfigError(f"{label} 必须是布尔值")


def _path(value: Any, config_dir: Path, default: str) -> Path:
    result = Path(str(value or default)).expanduser()
    return (result if result.is_absolute() else config_dir / result).resolve()


@dataclass(frozen=True, slots=True)
class ThreadSelectors:
    """精确选择器；不支持正则或模糊关键词。"""

    ids: tuple[str, ...] = ()
    titles: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()

    def configured(self) -> bool:
        return bool(self.ids or self.titles or self.paths)


@dataclass(frozen=True, slots=True)
class CodexConfig:
    home: Path
    command: str = "codex"
    managed_project_root: Path = Path.home() / "Documents" / "Codex" / "Projects"
    # 旧配置未声明时保持 stdio；新安装模板显式启用 Desktop 官方应用工具。
    reply_transport: str = "stdio"
    desktop_log_dir: Path = field(default_factory=default_codex_desktop_log_dir)
    shared_websocket_url: str = "ws://127.0.0.1:6230"
    gateway_pid_file: Path = PROJECT_ROOT / ".state" / "codex-gateway.pid"
    shared_desktop_state_file: Path = (
        PROJECT_ROOT / ".state" / "codex-shared-desktop.json"
    )
    reply_timeout_seconds: float = 86400.0
    selectors: ThreadSelectors = field(default_factory=ThreadSelectors)


@dataclass(frozen=True, slots=True)
class WeChatConfig:
    backend: str
    tool_account_nickname: str
    tool_wechat_id: str
    target_chat: str
    target_wechat_id: str
    require_quote: bool
    secret_file: Path
    pending_ttl_hours: int


@dataclass(frozen=True, slots=True)
class MessagingConfig:
    """与具体平台无关的消息渠道安全配置。"""

    backend: str
    require_quote: bool
    secret_file: Path
    pending_ttl_hours: int


@dataclass(frozen=True, slots=True)
class FeishuConfig:
    """飞书企业自建应用机器人的最小连接配置。"""

    app_id: str
    app_secret_file: Path
    target_open_id: str
    connect_timeout_seconds: float


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    poll_seconds: float
    max_attempts: int
    retry_delays: tuple[float, ...]
    log_retention_days: int
    database: Path
    log_dir: Path
    pid_file: Path


@dataclass(frozen=True, slots=True)
class SummaryConfig:
    mode: str
    endpoint: str
    model: str
    api_key_env: str
    min_interval_seconds: float
    codex_command: str = "codex"
    reasoning_effort: str = "low"
    timeout_seconds: float = 120.0
    max_input_chars: int = 12000


@dataclass(frozen=True, slots=True)
class AppConfig:
    path: Path
    codex: CodexConfig
    messaging: MessagingConfig
    feishu: FeishuConfig
    wechat: WeChatConfig
    service: ServiceConfig
    summary: SummaryConfig

    def validate_ready(self) -> None:
        if not self.codex.selectors.configured():
            raise ConfigError("monitor.ids、monitor.titles 或 monitor.paths 至少配置一项")
        if not self.messaging.require_quote:
            raise ConfigError(
                "messaging.require_quote 必须为 true；会话正文必须回复本工具的结构化消息"
            )
        placeholders = (*self.codex.selectors.ids, *self.codex.selectors.titles, *self.codex.selectors.paths)
        if any(value.startswith("请替换") for value in placeholders):
            raise ConfigError("monitor 中仍有“请替换”占位值")
        if self.messaging.backend == "feishu":
            if not re.fullmatch(r"cli_[A-Za-z0-9]+", self.feishu.app_id):
                raise ConfigError("feishu.app_id 必须是 cli_ 开头的飞书应用 ID")
            if not re.fullmatch(r"ou_[A-Za-z0-9_-]+", self.feishu.target_open_id):
                raise ConfigError("feishu.target_open_id 必须是 ou_ 开头的唯一用户 open_id")
            if not self.feishu.app_secret_file.is_file():
                raise ConfigError(
                    "飞书 App Secret 尚未安全保存；请先运行“一键配置飞书”"
                )
            return
        if self.messaging.backend in {"wxautox4", "probe_only"}:
            if not self.wechat.target_chat:
                raise ConfigError("wechat.target_chat 不能为空")
            if not self.wechat.tool_account_nickname:
                raise ConfigError("wechat.tool_account_nickname 不能为空；双开时必须显式选择工具小号")
            if not self.wechat.tool_wechat_id:
                raise ConfigError("wechat.tool_wechat_id 不能为空；启动前必须核验工具小号")
            if not self.wechat.target_wechat_id:
                raise ConfigError("wechat.target_wechat_id 不能为空；生产模式按微信号唯一校验")
            if self.wechat.tool_wechat_id == self.wechat.target_wechat_id:
                raise ConfigError("wechat.tool_wechat_id 与 target_wechat_id 必须是两个不同账号")
            wechat_values = (
                self.wechat.tool_account_nickname,
                self.wechat.tool_wechat_id,
                self.wechat.target_chat,
                self.wechat.target_wechat_id,
            )
            if any(value.startswith("请替换") for value in wechat_values):
                raise ConfigError("wechat 中仍有“请替换”占位值")


def load_config(path: str | os.PathLike[str] = DEFAULT_CONFIG_PATH, *, environ: Mapping[str, str] | None = None) -> AppConfig:
    """读取 YAML，并展开 ``${NAME:-default}`` 环境变量占位符。"""

    config_path = Path(path).expanduser().resolve()
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8-sig")) or {}
    except OSError as exc:
        raise ConfigError(f"无法读取配置：{config_path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML 格式错误：{exc}") from exc
    if not isinstance(data, Mapping):
        raise ConfigError("配置根节点必须是映射")
    root = _expand(data, os.environ if environ is None else environ)
    config_dir = config_path.parent
    codex_data = _mapping(root.get("codex"), "codex")
    monitor_data = _mapping(root.get("monitor"), "monitor")
    messaging_data = _mapping(root.get("messaging"), "messaging")
    feishu_data = _mapping(root.get("feishu"), "feishu")
    wechat_data = _mapping(root.get("wechat"), "wechat")
    service_data = _mapping(root.get("service"), "service")
    summary_data = _mapping(root.get("summary"), "summary")

    home = _path(codex_data.get("home"), config_dir, str(Path.home() / ".codex"))
    selectors = ThreadSelectors(
        ids=_strings(monitor_data.get("ids"), "monitor.ids"),
        titles=_strings(monitor_data.get("titles"), "monitor.titles"),
        paths=_strings(monitor_data.get("paths"), "monitor.paths"),
    )
    codex = CodexConfig(
        home=home,
        command=str(codex_data.get("command") or "codex").strip(),
        managed_project_root=_path(
            codex_data.get("managed_project_root"),
            config_dir,
            str(Path.home() / "Documents" / "Codex" / "Projects"),
        ),
        reply_transport=str(
            codex_data.get("reply_transport") or "stdio"
        ).strip().casefold(),
        desktop_log_dir=_path(
            codex_data.get("desktop_log_dir"),
            config_dir,
            str(default_codex_desktop_log_dir()),
        ),
        shared_websocket_url=_shared_ws_url(
            codex_data.get("shared_websocket_url")
        ),
        gateway_pid_file=_path(
            codex_data.get("gateway_pid_file"),
            config_dir,
            ".state/codex-gateway.pid",
        ),
        shared_desktop_state_file=_path(
            codex_data.get("shared_desktop_state_file"),
            config_dir,
            ".state/codex-shared-desktop.json",
        ),
        reply_timeout_seconds=_number(codex_data.get("reply_timeout_seconds"), "codex.reply_timeout_seconds", 86400, 30, 86400),
        selectors=selectors,
    )
    if codex.reply_transport not in {
        "desktop_app_tools",
        "stdio",
        "shared_websocket",
    }:
        raise ConfigError(
            "codex.reply_transport 仅支持 desktop_app_tools、stdio 或 shared_websocket"
        )
    backend = str(
        messaging_data.get("backend") or wechat_data.get("backend") or "feishu"
    ).strip().casefold()
    if backend not in {"feishu", "wxautox4", "probe_only", "fake"}:
        raise ConfigError(
            "messaging.backend 仅支持 feishu、wxautox4、probe_only（fake 仅供测试）"
        )
    require_quote = _boolean(
        messaging_data.get("require_quote", wechat_data.get("require_quote")),
        "messaging.require_quote",
        True,
    )
    secret_file = _path(
        messaging_data.get("secret_file", wechat_data.get("secret_file")),
        config_dir,
        ".secrets/hmac.key",
    )
    pending_ttl_hours = _integer(
        messaging_data.get("pending_ttl_hours", wechat_data.get("pending_ttl_hours")),
        "messaging.pending_ttl_hours",
        72,
        1,
        720,
    )
    messaging = MessagingConfig(
        backend=backend,
        require_quote=require_quote,
        secret_file=secret_file,
        pending_ttl_hours=pending_ttl_hours,
    )
    feishu = FeishuConfig(
        app_id=str(feishu_data.get("app_id") or "").strip(),
        app_secret_file=_path(
            feishu_data.get("app_secret_file"),
            config_dir,
            ".secrets/feishu-app-secret.dpapi",
        ),
        target_open_id=str(feishu_data.get("target_open_id") or "").strip(),
        connect_timeout_seconds=_number(
            feishu_data.get("connect_timeout_seconds"),
            "feishu.connect_timeout_seconds",
            30,
            5,
            120,
        ),
    )
    wechat = WeChatConfig(
        backend=backend,
        tool_account_nickname=str(wechat_data.get("tool_account_nickname") or "").strip(),
        tool_wechat_id=str(wechat_data.get("tool_wechat_id") or "").strip(),
        target_chat=str(wechat_data.get("target_chat") or "").strip(),
        target_wechat_id=str(wechat_data.get("target_wechat_id") or "").strip(),
        require_quote=require_quote,
        secret_file=secret_file,
        pending_ttl_hours=pending_ttl_hours,
    )
    attempts = _integer(service_data.get("max_attempts"), "service.max_attempts", 5, 1, 5)
    raw_delays = service_data.get("retry_delays", [1, 2, 4, 8, 16])
    if not isinstance(raw_delays, list) or len(raw_delays) < attempts:
        raise ConfigError("service.retry_delays 必须至少包含 max_attempts 个间隔")
    delays = tuple(_number(item, "service.retry_delays[]", 1, 0, 300) for item in raw_delays[:attempts])
    if backend != "fake" and any(
        later <= earlier for earlier, later in zip(delays, delays[1:])
    ):
        raise ConfigError("生产模式的 service.retry_delays 必须严格递增")
    service = ServiceConfig(
        poll_seconds=_number(service_data.get("poll_seconds"), "service.poll_seconds", 2, 0.5, 60),
        max_attempts=attempts,
        retry_delays=delays,
        log_retention_days=_integer(service_data.get("log_retention_days"), "service.log_retention_days", 7, 1, 90),
        database=_path(service_data.get("database"), config_dir, ".state/progress-wx.sqlite"),
        log_dir=_path(service_data.get("log_dir"), config_dir, "logs"),
        pid_file=_path(service_data.get("pid_file"), config_dir, ".state/progress-wx.pid"),
    )
    mode = str(summary_data.get("mode") or "codex_final").strip().casefold()
    if mode not in {"codex_final", "codex_cli", "openai_compatible", "disabled"}:
        raise ConfigError(
            "summary.mode 必须是 codex_final、codex_cli、openai_compatible 或 disabled"
        )
    reasoning_effort = str(
        summary_data.get("reasoning_effort") or "low"
    ).strip().casefold()
    if reasoning_effort not in {"none", "low", "medium", "high"}:
        raise ConfigError("summary.reasoning_effort 仅支持 none、low、medium 或 high")
    summary = SummaryConfig(
        mode=mode,
        endpoint=str(summary_data.get("endpoint") or "").strip(),
        model=str(summary_data.get("model") or "").strip(),
        api_key_env=str(summary_data.get("api_key_env") or "OPENAI_API_KEY").strip(),
        min_interval_seconds=_number(summary_data.get("min_interval_seconds"), "summary.min_interval_seconds", 60, 0, 86400),
        codex_command=str(summary_data.get("codex_command") or "codex").strip(),
        reasoning_effort=reasoning_effort,
        timeout_seconds=_number(
            summary_data.get("timeout_seconds"),
            "summary.timeout_seconds",
            120,
            30,
            300,
        ),
        max_input_chars=_integer(
            summary_data.get("max_input_chars"),
            "summary.max_input_chars",
            12000,
            1000,
            50000,
        ),
    )
    if mode == "openai_compatible" and (not summary.endpoint or not summary.model):
        raise ConfigError("openai_compatible 摘要必须配置 endpoint 和 model")
    if mode == "codex_cli" and (not summary.codex_command or not summary.model):
        raise ConfigError("codex_cli 摘要必须配置 codex_command 和 model")
    if mode == "openai_compatible":
        parts = urlsplit(summary.endpoint)
        loopback = (parts.hostname or "").casefold() in {"localhost", "127.0.0.1", "::1"}
        if parts.scheme not in {"http", "https"} or (parts.scheme == "http" and not loopback):
            raise ConfigError("摘要 endpoint 仅允许 HTTPS，或 HTTP 回环地址")
    return AppConfig(config_path, codex, messaging, feishu, wechat, service, summary)


class ReloadingConfig:
    """按文件修改时间动态重载配置；未变化时不解析 YAML。"""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path).resolve()
        self._mtime_ns = -1
        self._value: AppConfig | None = None

    def get(self) -> AppConfig:
        mtime = self.path.stat().st_mtime_ns
        if self._value is None or mtime != self._mtime_ns:
            value = load_config(self.path)
            value.validate_ready()
            self._value, self._mtime_ns = value, mtime
        return self._value
