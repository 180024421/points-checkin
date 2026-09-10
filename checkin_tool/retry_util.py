# -*- coding: utf-8 -*-
"""通用重试：网络失败 / Trae 9074 等。"""

from __future__ import annotations

import random
import time
from typing import Callable, TypeVar

T = TypeVar("T")


def retry_call(
    fn: Callable[[], T],
    *,
    retries: int = 8,
    min_wait: float = 15.0,
    max_wait: float = 30.0,
    should_retry: Callable[[T], bool] | None = None,
    on_retry: Callable[[int, T | BaseException], None] | None = None,
) -> T:
    last: T | BaseException | None = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            result = fn()
            last = result
            if should_retry and should_retry(result) and attempt < retries:
                wait = random.uniform(min_wait, max_wait)
                if on_retry:
                    on_retry(attempt, result)
                time.sleep(wait)
                continue
            return result
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt >= retries:
                raise
            wait = random.uniform(min_wait, max_wait)
            if on_retry:
                on_retry(attempt, exc)
            time.sleep(wait)
    if isinstance(last, BaseException):
        raise last
    return last  # type: ignore[return-value]
