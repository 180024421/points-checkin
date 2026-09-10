# -*- coding: utf-8 -*-
"""TraeWork 账密登录：GetLoginGuidance API → OAuth/Playwright 回退。"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from ..adapters import traework as traework_adapter
from .browser import attach_token_sniffer, ensure_playwright, fill_login_form
from .types import LoginResult

_SSL = ssl.create_default_context()

# 从 Trae 桌面端 main.js 提取的默认 ClientID
CLIENT_ID_TRAE = "ono9krqynydwx5"
CLIENT_ID_SOLO = "en1oxy7wnw8j9n"

GUIDANCE_URLS = [
    "https://api.trae.com.cn/cloudide/api/v3/trae/GetLoginGuidance",
    "https://api.trae.com.cn/cloudide/api/v3/trae/GetLoginGuidanceForBytedance",
]

API_HOSTS = [
    "https://api.trae.com.cn",
    "https://www.marscode.cn",
    "https://www.trae.com.cn",
]


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _pkce() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _post_json(url: str, payload: dict[str, Any], timeout: float = 20.0) -> tuple[int, dict[str, Any] | str]:
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
        with urlopen(req, timeout=timeout, context=_SSL) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, raw
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, raw
    except URLError as exc:
        return -1, f"network:{exc}"
    except Exception as exc:  # noqa: BLE001
        return -1, str(exc)


def get_login_host() -> tuple[str | None, str]:
    notes: list[str] = []
    for url in GUIDANCE_URLS:
        status, body = _post_json(url, {})
        if isinstance(body, dict):
            result = body.get("Result") if isinstance(body.get("Result"), dict) else {}
            host = result.get("LoginHost") or result.get("loginHost")
            if host:
                if not str(host).startswith("http"):
                    host = "https://" + str(host).lstrip("/")
                return str(host).rstrip("/"), f"GetLoginGuidance OK ({url})"
        notes.append(f"{url}->{status}")
    return "https://www.trae.cn", "GetLoginGuidance 回退默认 www.trae.cn；" + "; ".join(notes[:2])


def try_api_password_login(username: str, password: str) -> LoginResult:
    """Trae 无公开纯账密换 token 接口；此步仅探测并明确失败原因。"""
    host, note = get_login_host()
    # 探测是否存在未文档化的 password grant（预期失败）
    probes = [
        f"{host}/cloudide/api/v1/users/login/local",
        f"{host}/trae/api/v3/oauth/password",
        "https://api.trae.com.cn/trae/api/v3/oauth/password",
    ]
    notes = [note]
    for url in probes:
        status, body = _post_json(url, {"username": username, "password": password, "email": username})
        if isinstance(body, dict):
            # unlikely success path
            token = None
            stack = [body]
            while stack:
                cur = stack.pop()
                if not isinstance(cur, dict):
                    continue
                for k, v in cur.items():
                    if isinstance(v, dict):
                        stack.append(v)
                    elif isinstance(v, str) and k.lower() in ("token", "accesstoken", "access_token") and len(v) > 20:
                        token = v
            if token:
                headers = traework_adapter.load_device_headers()
                blob = {
                    "provider": "traework",
                    "token": token,
                    "access_token": token,
                    "user_id": username,
                    "machine_id": headers.get("X-Machine-Id") or "",
                    "device_id": headers.get("X-Device-Id") or "",
                    "token_hint": "API 登录取得（内容已隐藏）",
                    "login_source": "api",
                }
                return LoginResult(ok=True, provider="traework", method="api", token_blob=blob, message=f"API 登录成功 {url}")
        notes.append(f"{url}->{status}")
    return LoginResult(
        ok=False,
        provider="traework",
        method="api",
        message="无可用纯账密 API（Trae 为 OAuth）：" + "; ".join(notes[:4]),
    )


class _AuthCodeServer:
    def __init__(self) -> None:
        self.code: str | None = None
        self.raw_query: str = ""
        self._httpd: HTTPServer | None = None
        self.port = 0

    def start(self) -> int:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path.rstrip("/") not in ("/authorize", "/callback", "/"):
                    self.send_response(404)
                    self.end_headers()
                    return
                qs = parse_qs(parsed.query)
                owner.raw_query = parsed.query
                code = (qs.get("code") or qs.get("AuthCode") or qs.get("auth_code") or [None])[0]
                if code:
                    owner.code = code
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<html><body>Login OK. You can close this window.</body></html>")

            def log_message(self, format, *args):  # noqa: A003
                return

        self._httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self._httpd.server_address[1])
        t = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        t.start()
        return self.port

    def stop(self) -> None:
        if self._httpd:
            try:
                self._httpd.shutdown()
            except Exception:
                pass


def _exchange_token(api_base: str, auth_code: str, code_verifier: str, device: dict[str, str]) -> dict[str, Any] | None:
    payload = {
        "ClientID": CLIENT_ID_TRAE,
        "AuthCode": auth_code,
        "CodeVerifier": code_verifier,
        "DeviceInfo": {
            "DeviceID": device.get("device_id") or secrets.token_hex(16),
            "MachineID": device.get("machine_id") or secrets.token_hex(16),
            "PlatformCode": "Windows",
        },
        "IDEVersion": "CheckinTool/1.0",
    }
    for base in [api_base, *API_HOSTS]:
        url = f"{base.rstrip('/')}/trae/api/v3/oauth/ExchangeToken"
        status, body = _post_json(url, payload, timeout=30)
        if not isinstance(body, dict):
            continue
        # walk for token fields
        token = refresh = user_id = None
        stack = [body]
        while stack:
            cur = stack.pop()
            if not isinstance(cur, dict):
                continue
            for k, v in cur.items():
                lk = str(k).lower()
                if isinstance(v, dict):
                    stack.append(v)
                elif isinstance(v, str):
                    if lk in ("token", "accesstoken", "access_token", "cloudidetoken") and len(v) > 20:
                        token = token or v
                    if lk in ("refreshtoken", "refresh_token"):
                        refresh = refresh or v
                    if lk in ("userid", "user_id", "uid") and v:
                        user_id = user_id or v
        if token:
            return {
                "token": token,
                "refresh_token": refresh or "",
                "user_id": user_id or "",
                "api_base": base,
                "http": status,
            }
    return None


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
        return LoginResult(ok=False, provider="traework", method="playwright", message=str(exc), needs_manual=True)

    headers = traework_adapter.load_device_headers()
    device = {
        "machine_id": headers.get("X-Machine-Id") or secrets.token_hex(16),
        "device_id": headers.get("X-Device-Id") or secrets.token_hex(16),
    }
    login_host, host_note = get_login_host()
    verifier, challenge = _pkce()
    server = _AuthCodeServer()
    port = server.start()
    callback = f"http://127.0.0.1:{port}/authorize"
    auth_url = (
        f"{login_host}/authorization?login_version=1&auth_from=trae&login_channel=native_ide"
        f"&plugin_version=checkin-tool-1.0&auth_type=local&client_id={CLIENT_ID_TRAE}"
        f"&redirect=0&login_trace_id={secrets.token_hex(8)}"
        f"&auth_callback_url={callback}"
        f"&machine_id={device['machine_id']}&device_id={device['device_id']}"
        f"&code_challenge={challenge}&code_challenge_method=S256"
    )

    sniffed: list[dict[str, Any]] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not headed)
            context = browser.new_context()
            page = context.new_page()
            attach_token_sniffer(page, sniffed)
            page.goto(auth_url, wait_until="domcontentloaded", timeout=60_000)
            fill_login_form(page, username, password)

            end = time.time() + (timeout_ms / 1000.0)
            auth_code = None
            direct_token = None
            while time.time() < end:
                if server.code:
                    auth_code = server.code
                    break
                for item in sniffed:
                    if item.get("auth_code"):
                        auth_code = item["auth_code"]
                        break
                    tok = item.get("access_token") or item.get("token")
                    if tok and len(str(tok)) > 20:
                        direct_token = str(tok)
                        break
                if auth_code or direct_token:
                    break
                page.wait_for_timeout(800)
            browser.close()
    finally:
        server.stop()

    if direct_token:
        blob = {
            "provider": "traework",
            "token": direct_token,
            "access_token": direct_token,
            "user_id": username,
            "machine_id": device["machine_id"],
            "device_id": device["device_id"],
            "token_hint": "Playwright 登录取得（内容已隐藏）",
            "login_source": "playwright",
        }
        return LoginResult(
            ok=True,
            provider="traework",
            method="playwright",
            token_blob=blob,
            message=f"{host_note}；浏览器直接捕获 token",
        )

    if not auth_code:
        return LoginResult(
            ok=False,
            provider="traework",
            method="playwright",
            message=f"{host_note}；未拿到 AuthCode（可能需验证码/扫码）。请改用「粘贴 Trae token」或客户端自助登录。",
            needs_manual=True,
        )

    exchanged = _exchange_token("https://api.trae.com.cn", auth_code, verifier, device)
    if not exchanged:
        return LoginResult(
            ok=False,
            provider="traework",
            method="playwright",
            message=f"拿到 AuthCode 但 ExchangeToken 失败。可改用粘贴 token。code={auth_code[:8]}…",
            needs_manual=True,
        )

    blob = {
        "provider": "traework",
        "token": exchanged["token"],
        "access_token": exchanged["token"],
        "refresh_token": exchanged.get("refresh_token") or "",
        "user_id": exchanged.get("user_id") or username,
        "machine_id": device["machine_id"],
        "device_id": device["device_id"],
        "ug_api_base": exchanged.get("api_base") or "",
        "token_hint": "OAuth 交换取得（内容已隐藏）",
        "login_source": "playwright_oauth",
    }
    return LoginResult(
        ok=True,
        provider="traework",
        method="playwright",
        token_blob=blob,
        message=f"{host_note}；OAuth ExchangeToken 成功",
    )


def login(username: str, password: str, *, headed: bool = True) -> LoginResult:
    api = try_api_password_login(username, password)
    if api.ok:
        return api
    pw = try_playwright_login(username, password, headed=headed)
    if pw.ok:
        pw.message = f"{api.message} → {pw.message}"
        return pw
    return LoginResult(
        ok=False,
        provider="traework",
        method=pw.method or "playwright",
        message=f"{api.message}；{pw.message}",
        needs_manual=True,
    )
