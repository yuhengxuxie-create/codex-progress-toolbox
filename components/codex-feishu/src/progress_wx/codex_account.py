"""Read and format Codex account rate limits from the official App Server API."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Callable, Mapping

from .codex_rpc import CodexAppServer, CodexRPCError


class CodexAccountError(RuntimeError):
    """The official account endpoint did not return a trustworthy snapshot."""


@dataclass(frozen=True, slots=True)
class RateLimitWindow:
    bucket_id: str
    bucket_name: str
    window_name: str
    used_percent: float
    available_percent: float
    duration_minutes: int | None
    resets_at: int | None


@dataclass(frozen=True, slots=True)
class ResetCredit:
    credit_id: str
    title: str
    status: str
    expires_at: int | None


@dataclass(frozen=True, slots=True)
class AccountRateLimits:
    windows: tuple[RateLimitWindow, ...]
    reset_credit_count: int | None
    reset_credits: tuple[ResetCredit, ...]


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _integer(value: Any) -> int | None:
    number = _number(value)
    if number is None:
        return None
    result = int(number)
    return result if result >= 0 else None


def _window_label(duration_minutes: int | None, fallback: str) -> str:
    if duration_minutes is None or duration_minutes <= 0:
        return fallback
    if duration_minutes % 10080 == 0:
        weeks = duration_minutes // 10080
        return "每周额度" if weeks == 1 else f"每 {weeks} 周额度"
    if duration_minutes % 1440 == 0:
        days = duration_minutes // 1440
        return "每日额度" if days == 1 else f"每 {days} 天额度"
    if duration_minutes % 60 == 0:
        hours = duration_minutes // 60
        return f"{hours} 小时额度"
    return f"{duration_minutes} 分钟额度"


def _parse_window(
    bucket_id: str,
    bucket_name: str,
    fallback_name: str,
    payload: Any,
) -> RateLimitWindow | None:
    if not isinstance(payload, Mapping):
        return None
    used = _number(payload.get("usedPercent"))
    if used is None:
        return None
    used = min(100.0, max(0.0, used))
    duration = _integer(payload.get("windowDurationMins"))
    resets_at = _integer(payload.get("resetsAt"))
    return RateLimitWindow(
        bucket_id=bucket_id,
        bucket_name=bucket_name,
        window_name=_window_label(duration, fallback_name),
        used_percent=used,
        available_percent=100.0 - used,
        duration_minutes=duration,
        resets_at=resets_at,
    )


def parse_rate_limits_response(response: Mapping[str, Any]) -> AccountRateLimits:
    result = response.get("result")
    if not isinstance(result, Mapping):
        raise CodexAccountError("官方额度响应缺少 result")

    raw_by_id = result.get("rateLimitsByLimitId")
    buckets: list[tuple[str, Mapping[str, Any]]] = []
    if isinstance(raw_by_id, Mapping):
        codex_bucket = raw_by_id.get("codex")
        if isinstance(codex_bucket, Mapping):
            buckets.append(("codex", codex_bucket))
    if not buckets:
        legacy = result.get("rateLimits")
        if isinstance(legacy, Mapping):
            buckets.append(("codex", legacy))

    windows: list[RateLimitWindow] = []
    for bucket_id, bucket in buckets:
        bucket_name = str(bucket.get("limitName") or "").strip()
        if not bucket_name:
            bucket_name = "Codex" if bucket_id == "codex" else bucket_id
        for key, fallback in (("primary", "主要额度"), ("secondary", "额外额度")):
            parsed = _parse_window(
                bucket_id, bucket_name, fallback, bucket.get(key)
            )
            if parsed is not None:
                windows.append(parsed)
    if not windows:
        raise CodexAccountError("官方当前没有返回可用的 Codex 额度窗口")

    credit_count: int | None = None
    credits: list[ResetCredit] = []
    reset_payload = result.get("rateLimitResetCredits")
    if isinstance(reset_payload, Mapping):
        credit_count = _integer(reset_payload.get("availableCount"))
        raw_credits = reset_payload.get("credits")
        if isinstance(raw_credits, list):
            for item in raw_credits:
                if not isinstance(item, Mapping):
                    continue
                status = str(item.get("status") or "").strip()
                if status and status.casefold() not in {"available", "active", "unused"}:
                    continue
                credits.append(
                    ResetCredit(
                        credit_id=str(item.get("id") or "").strip(),
                        title=str(item.get("title") or "额外重置卡").strip(),
                        status=status,
                        expires_at=_integer(item.get("expiresAt")),
                    )
                )
    return AccountRateLimits(tuple(windows), credit_count, tuple(credits))


class CodexAccountReader:
    """One-shot reader so every Feishu query uses a fresh official snapshot."""

    def __init__(
        self,
        command: str,
        *,
        timeout_seconds: float = 20.0,
        rpc_factory: Callable[[], CodexAppServer] | None = None,
    ) -> None:
        self.command = command
        self.timeout_seconds = float(timeout_seconds)
        self._rpc_factory = rpc_factory

    def read(self) -> AccountRateLimits:
        rpc = (
            self._rpc_factory()
            if self._rpc_factory is not None
            else CodexAppServer(
                self.command,
                timeout_seconds=self.timeout_seconds,
                client_name="progress_wx_account",
            )
        )
        try:
            response = rpc.request(
                "account/rateLimits/read",
                timeout_seconds=self.timeout_seconds,
            )
            return parse_rate_limits_response(response)
        except CodexAccountError:
            raise
        except (CodexRPCError, OSError, TimeoutError) as exc:
            raise CodexAccountError("暂时无法从 Codex 官方服务读取额度") from exc
        finally:
            rpc.close()


def _percent(value: float) -> str:
    rounded = round(value, 1)
    return f"{int(rounded)}%" if rounded.is_integer() else f"{rounded:.1f}%"


def format_rate_limits(snapshot: AccountRateLimits) -> str:
    weekly = next(
        (item for item in snapshot.windows if item.duration_minutes == 7 * 24 * 60),
        None,
    )
    if weekly is None:
        raise CodexAccountError("官方当前没有返回 Codex 每周额度")
    lines = [f"Codex 每周额度：{_percent(weekly.available_percent)}"]
    count = snapshot.reset_credit_count
    if count is None:
        lines.append("剩余重置卡：官方当前未返回，无法确认")
    else:
        lines.append(f"剩余重置卡：{count} 张")
    return "\n".join(lines)


__all__ = [
    "AccountRateLimits",
    "CodexAccountError",
    "CodexAccountReader",
    "RateLimitWindow",
    "ResetCredit",
    "format_rate_limits",
    "parse_rate_limits_response",
]
