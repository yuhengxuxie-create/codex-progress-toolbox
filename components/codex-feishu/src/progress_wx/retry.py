"""有上限的递增间隔重试；耗尽后由服务触发警报并停止。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, TypeVar


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 5
    delays: tuple[float, ...] = (1, 2, 4, 8, 16)

    def __post_init__(self) -> None:
        if not 1 <= self.max_attempts <= 5:
            raise ValueError("max_attempts 必须介于 1 和 5")
        if len(self.delays) < self.max_attempts:
            raise ValueError("delays 数量不能少于 max_attempts")


class RetryExhausted(RuntimeError):
    """同一操作在有限次数后仍失败。"""

    def __init__(self, operation: str, attempts: int, last_error: BaseException):
        super().__init__(f"{operation} 连续失败 {attempts} 次：{last_error}")
        self.operation = operation
        self.attempts = attempts
        self.last_error = last_error


def call_with_retry(
    operation: str,
    function: Callable[[], T],
    policy: RetryPolicy,
    *,
    sleep: Callable[[float], None] = time.sleep,
    on_failure: Callable[[int, BaseException], None] | None = None,
    should_retry: Callable[[BaseException], bool] | None = None,
) -> T:
    """最多执行五次；无论异常文字是否变化都不会无限循环。"""

    last_error: BaseException | None = None
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return function()
        except (KeyboardInterrupt, SystemExit):
            raise
        except RetryExhausted:
            # 上层操作可能组合了自己的五次重试。直接透传耗尽异常，避免嵌套后
            # 把“最多五次”意外放大为 25 次。
            raise
        except BaseException as exc:
            last_error = exc
            if on_failure:
                on_failure(attempt, exc)
            if should_retry is not None and not should_retry(exc):
                raise
            if attempt < policy.max_attempts:
                sleep(policy.delays[attempt - 1])
    assert last_error is not None
    raise RetryExhausted(operation, policy.max_attempts, last_error)
