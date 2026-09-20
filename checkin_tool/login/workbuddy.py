# -*- coding: utf-8 -*-
"""WorkBuddy 账密登录：优先探测官方/OneID API，失败再 Playwright。"""

from __future__ import annotations

from typing import Any

from .browser import attach_token_sniffer, ensure_playwright, fill_login_form
from .http import post_json as _post_json, walk_dicts
from .types import LoginResult

# 经验路径：腾讯 OneID 邮箱登录；多数场景仍需浏览器态，失败即回退 Playwright
API_CANDIDATES = [
    ("https://api.account.tencent.com/authz/v1/authn/email/login", {"email": "{user}", "password": "{pass}"}),
    ("https://api.account.tencentid.com/authz/v1/authn/email/login", {"email": "{user}", "password": "{pass}"}),
    ("https://www.codebuddy.cn/v2/user/login", {"account": "{user}", "password": "{pass}"}),
    ("https://copilot.tencent.com/v2/user/login", {"account": "{user}", "password": "{pass}"}),
]

LOGIN_PAGES = [
    "https://www.codebuddy.cn/login",
    "https://www.workbuddy.cn/login",
    "https://copilot.tencent.com/login",
]


def _token_from_api_body(body: Any) -> dict[str, Any] | None:
    if not isinstance(body, dict):
        return None
    token = uid = nickname = None
    expires = None
    for node in walk_dicts(body):
        for k, v in node.items():
            lk = str(k).lower()
            if not isinstance(v, str):
                if isinstance(v, (int, float)) and not isinstance(v, bool) and lk in ("expiresat", "expires_at"):
                    expires = v
                continue
            if lk in ("accesstoken", "access_token", "token") and len(v) > 20:
                token = token or v
            if lk in ("uid", "userid", "user_id") and v:
                uid = uid or v
            if lk in ("nickname", "name") and v:
                nickname = nickname or v
    if not token:
        return None
    return {
        "provider": "workbuddy",
        "access_token": token,
        "token": token,
        "uid": str(uid or ""),
        "nickname": str(nickname or ""),
        "expires_at": expires or 0,
        "token_hint": "账密登录取得（内容已隐藏）",
        "login_source": "api",
    }


def try_api_login(username: str, password: str) -> LoginResult:
    notes: list[str] = []
    for url, tmpl in API_CANDIDATES:
        payload = {k: str(v).replace("{user}", username).replace("{pass}", password) for k, v in tmpl.items()}
        status, body = _post_json(url, payload)
        blob = _token_from_api_body(body)
        if blob and blob.get("access_token"):
            if not blob.get("uid"):
                blob["uid"] = username
            return LoginResult(ok=True, provider="workbuddy", method="api", token_blob=blob, message=f"API 登录成功 {url}")
        notes.append(f"{url} -> {status}")
    return LoginResult(
        ok=False,
        provider="workbuddy",
        method="api",
        message="官方账密 API 不可用或需验证码/会话态：" + "; ".join(notes[:4]),
    )


def try_playwright_login(
    username: str,
    password: str,
    *,
    headed: bool = True,
    timeout_ms: int = 180_000,
) -> LoginResult:
    try:
        sync_playwright = ensure_playwright()
    except RuntimeError as exc:
        return LoginResult(ok=False, provider="workbuddy", method="playwright", message=str(exc), needs_manual=True)

    sniffed: list[dict[str, Any]] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context()
        page = context.new_page()
        attach_token_sniffer(page, sniffed)
        last_err = ""
        for url in LOGIN_PAGES:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                fill_login_form(page, username, password)
                import time

                end = time.time() + (timeout_ms / 1000.0)
                while time.time() < end:
                    for item in sniffed:
                        token = item.get("access_token") or item.get("token")
                        if token and len(str(token)) > 20:
                            uid = item.get("uid") or item.get("user_id") or username
                            blob = {
                                "provider": "workbuddy",
                                "access_token": str(token),
                                "token": str(token),
                                "uid": str(uid),
                                "nickname": str(item.get("nickname") or ""),
                                "expires_at": item.get("expires_at") or 0,
                                "token_hint": "Playwright 登录取得（内容已隐藏）",
                                "login_source": "playwright",
                            }
                            browser.close()
                            return LoginResult(
                                ok=True,
                                provider="workbuddy",
                                method="playwright",
                                token_blob=blob,
                                message=f"浏览器登录成功（{url}）",
                            )
                    page.wait_for_timeout(800)
                last_err = f"超时未捕获 token：{url}"
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
                continue
        browser.close()
    return LoginResult(
        ok=False,
        provider="workbuddy",
        method="playwright",
        message=last_err or "浏览器登录失败（可能需扫码/验证码，请改用客户端自助登录后采集）",
        needs_manual=True,
    )


def login(username: str, password: str, *, headed: bool = True) -> LoginResult:
    api = try_api_login(username, password)
    if api.ok:
        return api
    pw = try_playwright_login(username, password, headed=headed)
    if pw.ok:
        pw.message = f"{api.message} → {pw.message}"
        return pw
    return LoginResult(
        ok=False,
        provider="workbuddy",
        method=pw.method or "playwright",
        message=f"{api.message}；{pw.message}",
        needs_manual=True,
    )
