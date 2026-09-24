# -*- coding: utf-8 -*-
"""WorkBuddy（CodeBuddy）每日签到适配器。"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..redact import brief, mask_text
from ..retry_util import retry_call
from .base import CheckinResult, mask_secret

# CN 版基址；国际版（workbuddy_intl）复用本模块 HTTP 核心，只换 base_url/provider
BASE_CN = "https://www.codebuddy.cn"
STATUS_PATH = "/v2/billing/meter/checkin-activity-status"
CHECKIN_PATH = "/v2/billing/meter/daily-checkin"
STATUS_URL = BASE_CN + STATUS_PATH
CHECKIN_URL = BASE_CN + CHECKIN_PATH

AUTH_CANDIDATES = [
    Path(os.environ.get("LOCALAPPDATA", ""))
    / "CodeBuddyExtension"
    / "Data"
    / "Public"
    / "auth"
    / "workbuddy-desktop.info",
]


def default_auth_path() -> Path:
    for path in AUTH_CANDIDATES:
        if path.exists():
            return path
    return AUTH_CANDIDATES[0]


def auth_dir() -> Path:
    return default_auth_path().parent


def _parse_auth_data(data: Any, source_path: str | Path) -> tuple[dict[str, Any] | None, str | None]:
    """解析登录态 JSON；返回 (auth, err)。"""
    if not isinstance(data, dict):
        return None, f"登录态格式无效：{source_path}"
    auth = data.get("auth") if isinstance(data.get("auth"), dict) else {}
    token = auth.get("accessToken")
    account = data.get("account") if isinstance(data.get("account"), dict) else {}
    uid = account.get("uid")
    if not token:
        return None, "登录态缺少 accessToken"
    # 国际版（workbuddy-desktop-ai.info）把 accessToken 存成 $wbEncrypted 信封，
    # 不能当 CN 的明文 token 用：这里挡掉，避免把一个坏账号混进 CN 列表
    if isinstance(token, dict):
        return None, "accessToken 是本机加密信封（疑似国际版），需另行解密或手工采集"
    if not uid:
        return None, "登录态缺少 uid"
    expires = auth.get("expiresAt", 0) or 0
    if expires and expires < (time.time() * 1000 + 5 * 60 * 1000):
        return None, "accessToken 已过期"
    return {
        "provider": "workbuddy",
        "access_token": token,
        "token": token,
        "uid": str(uid),
        "nickname": str(account.get("nickname") or ""),
        "expires_at": expires,
        "source_path": str(source_path),
        "token_hint": mask_secret(token),
    }, None


def load_all_local_auths(auth_dir_path: str | Path | None = None) -> tuple[list[dict[str, Any]], str | None]:
    """扫描 auth 目录下所有 workbuddy-desktop*.info（含历史备份），按 uid 去重，
    同一 uid 取 expiresAt 最新的一条未过期登录态。"""
    directory = Path(auth_dir_path) if auth_dir_path else auth_dir()
    if not directory.exists():
        return [], f"找不到登录态目录：{directory}"
    best: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for path in sorted(directory.glob("workbuddy-desktop*.info")):
        seen.add(str(path))
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        auth, _err = _parse_auth_data(data, path)
        if not auth:
            continue
        uid = auth["uid"]
        if uid not in best or (auth.get("expires_at") or 0) > (best[uid].get("expires_at") or 0):
            best[uid] = auth
    if not best:
        return [], "目录中没有有效的登录态文件，请先打开 WorkBuddy 登录"
    return sorted(best.values(), key=lambda a: a.get("uid") or ""), None


def load_local_auth(auth_path: str | Path | None = None) -> tuple[dict[str, Any] | None, str | None]:
    path = Path(auth_path) if auth_path else default_auth_path()
    if not path.exists():
        return None, f"找不到登录态文件：{path}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, f"登录态解析失败：{exc}"
    auth, err = _parse_auth_data(data, path)
    if not auth:
        if err == "accessToken 已过期":
            return None, "accessToken 即将过期，请先打开一次 WorkBuddy 刷新登录态"
        return None, err or "登录态无效"
    return auth, None


def _api_post(url: str, token: str, uid: str, timeout: float = 20.0) -> tuple[int, dict[str, Any]]:
    req = Request(
        url,
        method="POST",
        data=b"{}",
        headers={
            "Authorization": f"Bearer {token}",
            "X-User-Id": uid,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "CheckinTool/1.0",
        },
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        try:
            return exc.code, json.loads(body)
        except Exception:
            # 非 JSON 的错误页原样进 msg，会随 last_error 落盘并显示在界面上
            return exc.code, {"msg": brief(body, 200)}
    except URLError as exc:
        return -1, {"msg": mask_text(f"网络失败: {exc}", 200)}
    except Exception as exc:
        return -1, {"msg": mask_text(exc, 200)}


def query_status(token: str, uid: str, *, timeout: float = 20.0, base_url: str = BASE_CN) -> dict[str, Any]:
    status, resp = _api_post(f"{base_url}{STATUS_PATH}", token, uid, timeout=timeout)
    data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
    return {
        "ok": status == 200 and resp.get("code") == 0,
        "http": status,
        "code": resp.get("code"),
        "active": bool(data.get("active")),
        "today_checked_in": bool(data.get("today_checked_in")),
        "streak": data.get("streak_days"),
        "today_credit": data.get("today_credit"),
        "daily_credit": data.get("daily_credit"),
        "message": resp.get("msg") or "",
        "raw": data,
    }


def checkin_with_token(
    token: str,
    uid: str,
    *,
    timeout: float = 20.0,
    base_url: str = BASE_CN,
    provider: str = "workbuddy",
) -> CheckinResult:
    def _once() -> CheckinResult:
        status_info = query_status(token, uid, timeout=timeout, base_url=base_url)
        if not status_info.get("ok"):
            # 原来 `or status_info` 会把整个响应字典拼进 message，连着 raw 一起落盘
            msg = status_info.get("message") or f"HTTP {status_info.get('http')} / code={status_info.get('code')}"
            return CheckinResult(
                ok=False,
                provider=provider,
                message=f"查询签到状态失败：{mask_text(msg, 160)}",
                raw_summary={"http": status_info.get("http"), "code": status_info.get("code"), "retryable": status_info.get("http", 0) < 0},
            )
        if not status_info.get("active"):
            # 没有活动不等于「今天已签到」：记成成功会把这格标绿，晚窗也不再补跑
            return CheckinResult(
                ok=False,
                provider=provider,
                already=False,
                skipped=True,
                message="当前没有进行中的签到活动，未领到积分",
                raw_summary={"active": False},
            )
        streak = status_info.get("streak")
        today_got = status_info.get("today_credit")
        if status_info.get("today_checked_in"):
            return CheckinResult(
                ok=True,
                provider=provider,
                already=True,
                credits=int(today_got) if isinstance(today_got, (int, float)) else None,
                streak=int(streak) if isinstance(streak, (int, float)) else None,
                message=f"今天已签到，获得 {today_got or 0} 积分",
                raw_summary={"today_checked_in": True},
            )

        status2, resp2 = _api_post(f"{base_url}{CHECKIN_PATH}", token, uid, timeout=timeout)
        code2 = resp2.get("code")
        if status2 == 200 and code2 == 0:
            refreshed = query_status(token, uid, timeout=timeout, base_url=base_url)
            if refreshed.get("ok"):
                today_got = refreshed.get("today_credit", today_got)
                streak = refreshed.get("streak", streak)
            return CheckinResult(
                ok=True,
                provider=provider,
                credits=int(today_got) if isinstance(today_got, (int, float)) else None,
                streak=int(streak) if isinstance(streak, (int, float)) else None,
                message=f"签到成功，获得 {today_got or 0} 积分，连签 {streak or 0} 天",
                raw_summary={"claimed": True},
            )
        if code2 == 10001:
            return CheckinResult(
                ok=True,
                provider=provider,
                already=True,
                message=mask_text(str(resp2.get("msg") or "今天已签到"), 160),
                raw_summary={"code": 10001},
            )
        return CheckinResult(
            ok=False,
            provider=provider,
            message=f"签到失败：HTTP {status2} / code={code2} / {brief(resp2.get('msg') or resp2, 160)}",
            raw_summary={"http": status2, "code": code2, "retryable": status2 < 0},
        )

    def _should_retry(result: CheckinResult) -> bool:
        if result.ok:
            return False
        return bool((result.raw_summary or {}).get("retryable"))

    return retry_call(
        _once,
        retries=5,
        min_wait=3,
        max_wait=25,
        max_total_sec=90,
        should_retry=_should_retry,
    )


def checkin_from_local(auth_path: str | Path | None = None) -> CheckinResult:
    auth, err = load_local_auth(auth_path)
    if err or not auth:
        return CheckinResult(ok=False, provider="workbuddy", message=err or "无登录态")
    return checkin_with_token(auth["access_token"], auth["uid"])


def checkin_from_blob(
    token_blob: dict[str, Any], *, base_url: str = BASE_CN, provider: str = "workbuddy"
) -> CheckinResult:
    token = str(token_blob.get("access_token") or token_blob.get("token") or "").strip()
    uid = str(token_blob.get("uid") or "").strip()
    if not token or not uid:
        return CheckinResult(ok=False, provider=provider, message="token_blob 缺少 access_token/uid")
    expires = token_blob.get("expires_at") or token_blob.get("expiresAt")
    if isinstance(expires, (int, float)) and expires > 0 and expires < (time.time() * 1000 + 5 * 60 * 1000):
        return CheckinResult(ok=False, provider=provider, message="Token 已过期，请重新登录 WorkBuddy 后采集")
    return checkin_with_token(token, uid, base_url=base_url, provider=provider)


def query_from_blob(token_blob: dict[str, Any], *, base_url: str = BASE_CN) -> dict[str, Any]:
    token = str(token_blob.get("access_token") or token_blob.get("token") or "").strip()
    uid = str(token_blob.get("uid") or "").strip()
    if not token or not uid:
        return {"ok": False, "message": "缺少 token/uid"}
    return query_status(token, uid, base_url=base_url)
