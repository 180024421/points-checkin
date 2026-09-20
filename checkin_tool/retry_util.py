# -*- coding: utf-8 -*-
"""通用重试：网络失败 / Trae 9074 等。

等待时间按指数增长并封顶，再乘随机抖动（避免多账号同时重试打同一接口）；
``max_total_sec`` 是总预算——签到是串行跑账号的，单个账号无限重试会把后面
的账号饿死。
"""

from __future__ import annotations

import random
import time
from typing import Callable, TypeVar

T = TypeVar("T")


def backoff_wait(attempt: int, *, min_wait: float, max_wait: float, factor: float = 2.0) -> float:
    """第 attempt 次失败后的等待秒数（1 起）。"""
    raw = min_wait * (factor ** max(0, attempt - 1))
    capped = min(max_wait, max(min_wait, raw))
    return random.uniform(capped * 0.6, capped)


def retry_call(
    fn: Callable[[], T],
    *,
    retries: int = 8,
    min_wait: float = 2.0,
    max_wait: float = 30.0,
    max_total_sec: float | None = None,
    should_retry: Callable[[T], bool] | None = None,
    on_retry: Callable[[int, T | BaseException], None] | None = None,
) -> T:
    last: T | BaseException | None = None
    started = time.monotonic()
    for attempt in range(1, max(1, retries) + 1):
        budget_left = max_total_sec is None or (started + max_total_sec - time.monotonic()) > 0
        try:
            result = fn()
            last = result
            if should_retry and should_retry(result) and attempt < retries and budget_left:
                if on_retry:
                    on_retry(attempt, result)
                time.sleep(backoff_wait(attempt, min_wait=min_wait, max_wait=max_wait))
                continue
            return result
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt >= retries or not budget_left:
                raise
            if on_retry:
                on_retry(attempt, exc)
            time.sleep(backoff_wait(attempt, min_wait=min_wait, max_wait=max_wait))
    if isinstance(last, BaseException):
        raise last
    return last  # type: ignore[return-value]
