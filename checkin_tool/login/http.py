# -*- coding: utf-8 -*-
"""登录层共用的 JSON POST 与响应遍历。

workbuddy 与 traework 原先各抄一份 _post_json（只有默认超时不同），
一处修好另一处照样踩坑，故收敛到这里。TLS 使用默认校验上下文。
"""

from __future__ import annotations

import json
import ssl
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SSL_CONTEXT = ssl.create_default_context()
DEFAULT_TIMEOUT = 12.0


def post_json(
    url: str,
    payload: dict[str, Any],
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[int, dict[str, Any] | str]:
    """返回 (HTTP 状态码, 解析后的 JSON 或原始文本)；网络失败返回 (-1, "network:...")。"""
    data = json.dumps(payload).encode("utf-8")
    req = Request(
        url,
        method="POST",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "CheckinTool/1.0",
        },
    )
    try:
        with urlopen(req, timeout=timeout, context=SSL_CONTEXT) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, _maybe_json(raw)
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        return exc.code, _maybe_json(raw)
    except URLError as exc:
        return -1, f"network:{exc}"
    except Exception as exc:  # noqa: BLE001 - 登录探测失败要变成备注文本，不能抛
        return -1, str(exc)


def _maybe_json(raw: str) -> dict[str, Any] | str:
    try:
        value = json.loads(raw)
    except Exception:
        return raw
    return value if isinstance(value, dict) else raw


def walk_dicts(obj: Any) -> Iterator[dict[str, Any]]:
    """深度遍历响应体里的所有 dict（含 list 内的 dict），不重复访问。"""
    seen: set[int] = set()
    stack: list[Any] = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if id(cur) in seen:
                continue
            seen.add(id(cur))
            yield cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
