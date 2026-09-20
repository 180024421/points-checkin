# -*- coding: utf-8 -*-
"""重试预算：指数退避 + 封顶 + 总时长上限（签到是串行跑账号的）。"""

import time

import pytest

from checkin_tool.retry_util import backoff_wait, retry_call


def test_backoff_grows_exponentially_and_is_capped():
    samples = [backoff_wait(i, min_wait=1, max_wait=10) for i in range(1, 8)]
    assert samples[0] <= 1.0
    assert samples[-1] <= 10.0
    # 上限之上不再增长
    assert backoff_wait(50, min_wait=1, max_wait=10) <= 10.0
    assert all(s >= 0.6 for s in samples)


def test_retry_stops_when_total_budget_is_spent():
    calls = {"n": 0}

    def always_retry():
        calls["n"] += 1
        return "retry"

    started = time.monotonic()
    out = retry_call(
        always_retry,
        retries=50,
        min_wait=0.05,
        max_wait=0.05,
        max_total_sec=0.3,
        should_retry=lambda r: r == "retry",
    )
    assert out == "retry"
    assert calls["n"] < 50, "超出预算后必须提前收手"
    assert time.monotonic() - started < 2


def test_exception_path_respects_retries_and_budget():
    attempts = {"n": 0}

    def boom():
        attempts["n"] += 1
        raise OSError("connection refused")

    with pytest.raises(OSError):
        retry_call(boom, retries=3, min_wait=0.01, max_wait=0.02)
    assert attempts["n"] == 3


def test_success_short_circuits_and_on_retry_reports():
    seen: list[int] = []
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        return "ok" if calls["n"] >= 2 else "retry"

    out = retry_call(
        flaky,
        retries=5,
        min_wait=0.01,
        max_wait=0.01,
        should_retry=lambda r: r == "retry",
        on_retry=lambda attempt, _res: seen.append(attempt),
    )
    assert out == "ok" and seen == [1]
