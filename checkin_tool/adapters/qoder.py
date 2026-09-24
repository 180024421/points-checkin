# -*- coding: utf-8 -*-
"""Qoder 每日签到（成长活动 claim）适配器。

实测契约（本机登录态 + 抓包 openapi.qoder.sh 验证，非猜测）：
- 状态：``GET /sash/api/v1/me/campaigns``，头 ``Authorization: Bearer <dt-token>``
  + ``Cosy-ClientType: 10``。返回 ``claimable`` 与 ``campaigns[]``。
- 每天新出一条 ``actionType:"CLAIM_BENEFIT"`` 活动（``benefit.kind=CREDITS,
  amount=100``，窗口约 24h），``claimStatus`` 从空 → ``CLAIMED`` 即为「今天已签」。
  另有一条常驻 ``VIEW_DETAILS`` 活动（打开外链用），不参与签到。
- 领取：``POST /sash/api/v1/me/campaigns/{campaignId}/claim``（iframe 通过
  postMessage 让原生端代发，所以桌面日志里看不到这条 POST）。

登录态 ``%APPDATA%\\com.qoder.app.stable\\auth.v1.dat`` 用 Chromium OSCrypt v10
（AES-256-GCM + DPAPI 主密钥）本地加密，token 约一个月寿命，可本机解密采集。
"""
from __future__ import annotations

import base64
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..redact import brief, mask_text
from ..retry_util import retry_call
from .base import CheckinResult, mask_secret

BASE = "https://openapi.qoder.sh"
CAMPAIGNS_PATH = "/sash/api/v1/me/campaigns"
CLAIM_SUFFIX = "/claim"
# 签到即「领当天那条 CLAIM_BENEFIT」，VIEW_DETAILS 只是开外链，不参与
CLAIM_ACTION = "CLAIM_BENEFIT"
CLAIMED_STATUS = "CLAIMED"
COSY_VERSION = "0.3.4"

PROVIDER = "qoder"


def _appdata() -> Path:
    return Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))


def _data_dir() -> Path:
    return _appdata() / "com.qoder.app.stable"


def _dpapi_unprotect(blob: bytes) -> bytes | None:
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(blob, len(blob))
    blob_in = DATA_BLOB(len(blob), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        return None
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _oscrypt_key(data_dir: Path) -> bytes | None:
    local_state = data_dir / "Local State"
    if not local_state.exists():
        return None
    data = json.loads(local_state.read_text(encoding="utf-8"))
    enc = data.get("os_crypt", {}).get("encrypted_key")
    if not enc:
        return None
    raw = base64.b64decode(enc)
    if raw[:5] != b"DPAPI":
        return None
    return _dpapi_unprotect(raw[5:])


def _decrypt_auth_v1(dat_path: Path, key: bytes) -> dict[str, Any]:
    blob = dat_path.read_bytes()
    if blob[:3] != b"v10":
        raise ValueError(f"auth.v1.dat 头部不是 v10：{blob[:3]!r}")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    plain = AESGCM(key).decrypt(blob[3:15], blob[15:], None)
    return json.loads(plain.decode("utf-8"))


def _iso_to_ms(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str) or not value:
        return 0
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0
    return int(dt.timestamp() * 1000)


def load_local_auth(data_dir: str | Path | None = None) -> tuple[dict[str, Any] | None, str | None]:
    """解密本机 Qoder 登录态，返回可直接入库的 token_blob（不含明文落盘）。"""
    directory = Path(data_dir) if data_dir else _data_dir()
    dat = directory / "auth.v1.dat"
    if not dat.exists():
        return None, f"找不到 Qoder 登录态：{dat}"
    try:
        key = _oscrypt_key(directory)
        if not key:
            return None, "无法取得本机解密主密钥（非 Windows 或 DPAPI 失败）"
        auth = _decrypt_auth_v1(dat, key)
    except Exception as exc:  # noqa: BLE001 - 采集失败要变成界面能读懂的一句话，不能抛
        return None, f"登录态解密失败：{mask_text(exc, 120)}"
    token = str(auth.get("token") or "").strip()
    uid = str((auth.get("user") or {}).get("id") or "").strip()
    if not token or not uid:
        return None, "登录态缺少 token 或 user.id"
    expires_ms = _iso_to_ms(auth.get("expiresAt"))
    if expires_ms and expires_ms < (time.time() * 1000 + 5 * 60 * 1000):
        return None, "登录态已过期，请重新打开一次 Qoder 登录后再采集"
    user = auth.get("user") or {}
    return {
        "provider": PROVIDER,
        "access_token": token,
        "token": token,
        "refresh_token": auth.get("refreshToken") or "",
        "uid": uid,
        "nickname": str(user.get("name") or user.get("email") or ""),
        "expires_at": expires_ms,
        "token_hint": mask_secret(token),
    }, None


def _http(method: str, url: str, token: str, *, timeout: float = 20.0, body: bytes | None = None) -> tuple[int, dict[str, Any]]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Cosy-ClientType": "10",
        "Cosy-Version": COSY_VERSION,
        "User-Agent": "Qoder",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = Request(url, method=method, data=body, headers=headers)
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.status, _load_json(resp.read())
    except HTTPError as exc:
        return exc.code, _load_json(exc.read().decode("utf-8", errors="ignore"))
    except URLError as exc:
        return -1, {"msg": mask_text(f"网络失败: {exc}", 200)}
    except Exception as exc:  # noqa: BLE001
        return -1, {"msg": mask_text(exc, 200)}


def _load_json(raw: Any) -> dict[str, Any]:
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="ignore")
    try:
        data = json.loads(raw)
    except Exception:
        return {"msg": brief(str(raw), 200)}
    return data if isinstance(data, dict) else {"data": data}


def _campaign_field(c: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in c and c[k] not in (None, ""):
            return c[k]
    return default


def _today_benefit(campaigns: list[dict[str, Any]], now: float) -> tuple[dict[str, Any] | None, int]:
    """返回「当天那条 CLAIM_BENEFIT」及其积分；找不到给 (None, 0)。"""
    for c in campaigns:
        if str(c.get("actionType") or "") != CLAIM_ACTION:
            continue
        start = _campaign_field(c, "startAt", default=0) or 0
        end = _campaign_field(c, "endAt", default=0) or 0
        if start <= now <= end:
            benefit = c.get("benefit") if isinstance(c.get("benefit"), dict) else {}
            amount = benefit.get("amount")
            return c, int(amount) if isinstance(amount, (int, float)) else 0
    return None, 0


def query_status(token: str, uid: str, *, timeout: float = 20.0) -> dict[str, Any]:
    status, resp = _http("GET", f"{BASE}{CAMPAIGNS_PATH}", token, timeout=timeout)
    campaigns = resp.get("campaigns") if isinstance(resp.get("campaigns"), list) else []
    now = time.time()
    today, amount = _today_benefit(campaigns, now)
    checked_in = bool(today) and str(today.get("claimStatus") or "") == CLAIMED_STATUS
    claimable_now = bool(today) and not checked_in
    return {
        "ok": status == 200,
        "http": status,
        "code": resp.get("code"),
        "message": resp.get("msg") or resp.get("message") or "",
        "show_campaign": bool(resp.get("showCampaign")),
        "claimable": bool(resp.get("claimable")) or claimable_now,
        "today_checked_in": checked_in,
        "today_campaign_id": str((today or {}).get("campaignId") or ""),
        "today_credit": amount if checked_in else (amount if claimable_now else 0),
        "credits": amount,
        "raw": {"campaigns": campaigns},
    }


def _claim_campaign(token: str, campaign_id: str, *, timeout: float = 20.0) -> tuple[int, dict[str, Any]]:
    url = f"{BASE}{CAMPAIGNS_PATH}/{campaign_id}{CLAIM_SUFFIX}"
    return _http("POST", url, token, timeout=timeout, body=b"{}")


def checkin_with_token(token: str, uid: str, *, timeout: float = 20.0) -> CheckinResult:
    def _once() -> CheckinResult:
        info = query_status(token, uid, timeout=timeout)
        if not info.get("ok"):
            msg = info.get("message") or f"HTTP {info.get('http')}"
            http = info.get("http") or 0
            return CheckinResult(
                ok=False,
                provider=PROVIDER,
                message=f"查询活动状态失败：{mask_text(msg, 160)}",
                raw_summary={"http": http, "retryable": http < 0 or http >= 500},
            )
        campaign_id = info.get("today_campaign_id") or ""
        amount = int(info.get("today_credit") or 0)
        if info.get("today_checked_in"):
            return CheckinResult(
                ok=True,
                provider=PROVIDER,
                already=True,
                credits=amount or None,
                message=f"今天已签到，获得 {amount} 积分",
                raw_summary={"claimed": True},
            )
        if not campaign_id:
            # 当天没有可领的活动（未开始/已结束）：中性态，别标绿也别算失败
            return CheckinResult(
                ok=False,
                provider=PROVIDER,
                skipped=True,
                message="当前没有可领取的每日活动，未领到积分",
                raw_summary={"no_active_campaign": True},
            )
        status2, resp2 = _claim_campaign(token, campaign_id, timeout=timeout)
        if status2 == 200:
            refreshed = query_status(token, uid, timeout=timeout)
            got = int(refreshed.get("today_credit") or amount or 0)
            if refreshed.get("today_checked_in") or refreshed.get("ok"):
                return CheckinResult(
                    ok=True,
                    provider=PROVIDER,
                    credits=got or None,
                    message=f"签到成功，获得 {got} 积分",
                    raw_summary={"claimed": True},
                )
            return CheckinResult(
                ok=True,
                provider=PROVIDER,
                credits=got or None,
                message=f"领取请求已发出（获得 {got} 积分）",
                raw_summary={"claim_sent": True},
            )
        already = status2 in (409,) or str(resp2.get("code") or "").upper() in ("ALREADY_CLAIMED", "CLAIMED")
        if already:
            return CheckinResult(
                ok=True,
                provider=PROVIDER,
                already=True,
                credits=amount or None,
                message=mask_text(str(resp2.get("message") or resp2.get("msg") or "今天已签到"), 160),
                raw_summary={"http": status2},
            )
        return CheckinResult(
            ok=False,
            provider=PROVIDER,
            message=f"领取失败：HTTP {status2} / {brief(resp2.get('message') or resp2.get('msg') or resp2, 160)}",
            raw_summary={"http": status2, "retryable": status2 < 0 or status2 >= 500},
        )

    def _should_retry(result: CheckinResult) -> bool:
        if result.ok or result.skipped:
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


def checkin_from_blob(token_blob: dict[str, Any]) -> CheckinResult:
    token = str(token_blob.get("access_token") or token_blob.get("token") or "").strip()
    uid = str(token_blob.get("uid") or "").strip()
    if not token or not uid:
        return CheckinResult(ok=False, provider=PROVIDER, message="token_blob 缺少 access_token/uid")
    expires = token_blob.get("expires_at") or token_blob.get("expiresAt")
    if isinstance(expires, (int, float)) and expires > 0 and expires < (time.time() * 1000 + 5 * 60 * 1000):
        return CheckinResult(ok=False, provider=PROVIDER, message="Token 已过期，请重新登录 Qoder 后采集")
    return checkin_with_token(token, uid)


def query_from_blob(token_blob: dict[str, Any]) -> dict[str, Any]:
    token = str(token_blob.get("access_token") or token_blob.get("token") or "").strip()
    uid = str(token_blob.get("uid") or "").strip()
    if not token or not uid:
        return {"ok": False, "message": "缺少 token/uid"}
    return query_status(token, uid)
