# -*- coding: utf-8 -*-
"""Playwright 通用助手。"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from .http import walk_dicts


def ensure_playwright():
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "未安装 playwright。请执行：pip install playwright && playwright install chromium"
        ) from exc
    return sync_playwright


def fill_login_form(page, username: str, password: str) -> bool:
    """尽量填充账密表单，返回是否找到密码框。"""
    user_selectors = [
        'input[type="email"]',
        'input[name="email"]',
        'input[name="username"]',
        'input[name="account"]',
        'input[name="phone"]',
        'input[placeholder*="邮箱"]',
        'input[placeholder*="手机"]',
        'input[placeholder*="账号"]',
        'input[placeholder*="email" i]',
        'input[placeholder*="phone" i]',
        'input[type="text"]',
    ]
    pass_selectors = [
        'input[type="password"]',
        'input[name="password"]',
        'input[placeholder*="密码"]',
        'input[placeholder*="password" i]',
    ]
    filled_user = False
    for sel in user_selectors:
        loc = page.locator(sel).first
        try:
            if loc.count() > 0 and loc.is_visible(timeout=800):
                loc.fill(username)
                filled_user = True
                break
        except Exception:
            continue
    filled_pass = False
    for sel in pass_selectors:
        loc = page.locator(sel).first
        try:
            if loc.count() > 0 and loc.is_visible(timeout=800):
                loc.fill(password)
                filled_pass = True
                break
        except Exception:
            continue
    if filled_pass:
        for sel in [
            'button[type="submit"]',
            'button:has-text("登录")',
            'button:has-text("登 录")',
            'button:has-text("Sign in")',
            'button:has-text("Login")',
            'text=登录',
        ]:
            try:
                btn = page.locator(sel).first
                if btn.count() > 0 and btn.is_visible(timeout=500):
                    btn.click(timeout=2000)
                    break
            except Exception:
                continue
    return filled_user and filled_pass


def extract_tokens_from_text(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if not text:
        return out
    try:
        data = json.loads(text)
    except Exception:
        data = None
    if isinstance(data, dict):
        for node in walk_dicts(data):
            for k, v in node.items():
                lk = str(k).lower()
                if not isinstance(v, (str, int, float)):
                    continue
                if lk in ("accesstoken", "access_token", "token", "cloudtoken", "id_token") and len(str(v)) > 20:
                    out.setdefault("token", str(v))
                    if "access" in lk:
                        out["access_token"] = str(v)
                if lk in ("refreshtoken", "refresh_token") and len(str(v)) > 10:
                    out["refresh_token"] = str(v)
                if lk in ("uid", "userid", "user_id", "openid") and str(v):
                    out.setdefault("uid", str(v))
                    out.setdefault("user_id", str(v))
                if lk in ("nickname", "name", "username") and str(v):
                    out.setdefault("nickname", str(v))
                if lk in ("expiresat", "expires_at", "expiredat") and v:
                    out["expires_at"] = v
    # auth code in query-like text
    m = re.search(r"(?:[?&#]|^)(?:code|auth_code|AuthCode)=([A-Za-z0-9._~-]{8,})", text)
    if m:
        out["auth_code"] = m.group(1)
    return out


# 一次登录会命中几十个响应，逐个把 body 塞进 bucket 会无上限增长
_MAX_SNIFFED = 60
_SNIFF_MAX_BODY_CHARS = 500_000


def attach_token_sniffer(page, bucket: list[dict[str, Any]]) -> Callable[[], None]:
    def _on_response(resp) -> None:
        try:
            try:
                ctype = str((resp.headers or {}).get("content-type", "")).lower()
            except Exception:
                return
            # 只嗅探文本类响应：图片/字体/二进制既不含 token，
            # resp.text() 还会把它们整份解码出来白吃内存。
            if not any(marker in ctype for marker in ("json", "javascript", "text", "xml")):
                return
            body = resp.text()
        except Exception:  # noqa: BLE001 - 响应体可能已随跳转丢弃
            return
        if not body or len(body) > _SNIFF_MAX_BODY_CHARS:
            return
        try:
            found = extract_tokens_from_text(body)
        except Exception:  # noqa: BLE001
            return
        if found:
            found["_url"] = str(resp.url)[:200]
            bucket.append(found)
            if len(bucket) > _MAX_SNIFFED:
                del bucket[: len(bucket) - _MAX_SNIFFED]

    page.on("response", _on_response)

    def detach() -> None:
        try:
            page.remove_listener("response", _on_response)
        except Exception:  # noqa: BLE001 - 页面已关或旧版本无该 API
            pass

    return detach
