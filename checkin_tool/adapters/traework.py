# -*- coding: utf-8 -*-
"""TraeWork（TRAE SOLO / Trae CN Work）每日签到适配器。

官方桌面端走：
  POST {ugApi}/trae/api/v2/ug/checkin_credits/status
  POST {ugApi}/trae/api/v2/ug/checkin_credits/claim

本机登录态优先从 iCubeAuthInfo（Electron safeStorage）提取；若无法解密，
允许用户把已登录客户端采集到的 token_blob 写入账号库。
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..retry_util import retry_call
from .base import CheckinResult, mask_secret

STATUS_PATH = "/trae/api/v2/ug/checkin_credits/status"
CLAIM_PATH = "/trae/api/v2/ug/checkin_credits/claim"

# CN 常见 ugApi；可被 settings / token_blob 覆盖
DEFAULT_UG_API_BASES = [
    "https://www.marscode.cn",
    "https://api.trae.com.cn",
    "https://www.trae.com.cn",
]

APP_DATA_CANDIDATES = [
    "TRAE SOLO",
    "Trae CN",
    "TRAE SOLO CN",
    "Trae",
]


def _appdata() -> Path:
    return Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))


def discover_user_dirs() -> list[Path]:
    root = _appdata()
    found: list[Path] = []
    for name in APP_DATA_CANDIDATES:
        p = root / name / "User" / "globalStorage"
        if p.exists():
            found.append(p)
    return found


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


def _try_decrypt_auth_value(value: str) -> Any | None:
    # plaintext json
    try:
        return json.loads(value)
    except Exception:
        pass
    try:
        raw = base64.b64decode(value)
    except Exception:
        return None
    plain = _dpapi_unprotect(raw)
    if plain:
        try:
            return json.loads(plain.decode("utf-8"))
        except Exception:
            try:
                return json.loads(plain.decode("utf-8", errors="ignore"))
            except Exception:
                return None
    # Chromium OSCrypt v10 prefix
    if raw.startswith(b"v10") or raw.startswith(b"v11"):
        return None
    return None


def _read_storage_json(gs: Path) -> dict[str, Any]:
    path = gs / "storage.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _extract_token_from_obj(obj: Any) -> dict[str, Any] | None:
    if not isinstance(obj, dict):
        return None
    token = (
        obj.get("token")
        or obj.get("accessToken")
        or obj.get("access_token")
        or (obj.get("cloudToken") if isinstance(obj.get("cloudToken"), str) else None)
    )
    if not token and isinstance(obj.get("auth"), dict):
        token = obj["auth"].get("token") or obj["auth"].get("accessToken")
    user_id = obj.get("userId") or obj.get("user_id") or obj.get("uid")
    if not user_id and isinstance(obj.get("account"), dict):
        user_id = obj["account"].get("userId") or obj["account"].get("id")
    user_region = obj.get("userRegion")
    if isinstance(user_region, dict):
        user_region = user_region.get("region") or user_region.get("userRegion")
    scope = None
    if isinstance(obj.get("account"), dict):
        scope = obj["account"].get("scope")
    if token:
        return {
            "token": str(token),
            "user_id": str(user_id or ""),
            "user_region": str(user_region or ""),
            "scope": str(scope or ""),
        }
    return None


def load_device_headers(gs: Path | None = None) -> dict[str, str]:
    dirs = [gs] if gs else discover_user_dirs()
    headers: dict[str, str] = {}
    for d in dirs:
        if not d:
            continue
        storage = _read_storage_json(d)
        machine = str(storage.get("telemetry.machineId") or "").strip()
        device = str(storage.get("telemetry.devDeviceId") or "").strip()
        if machine:
            headers["X-Machine-Id"] = machine
            headers["x-machine-id"] = machine
        if device:
            headers["X-Device-Id"] = device
            headers["x-device-id"] = device
        if headers:
            return headers
    return headers


def load_local_auth(user_dir: str | Path | None = None) -> tuple[dict[str, Any] | None, str | None]:
    dirs = [Path(user_dir)] if user_dir else discover_user_dirs()
    if not dirs:
        return None, "未找到 Trae/TRAE SOLO 用户数据目录，请先安装并登录 TraeWork 桌面端"

    last_err = "未找到可解密的 iCubeAuthInfo"
    for gs in dirs:
        storage = _read_storage_json(gs)
        device_headers = load_device_headers(gs)
        # storage.json auth blobs
        for key, value in storage.items():
            if not str(key).startswith("iCubeAuthInfo://"):
                continue
            if not isinstance(value, str):
                continue
            obj = _try_decrypt_auth_value(value)
            extracted = _extract_token_from_obj(obj) if obj else None
            if extracted and extracted.get("token"):
                return {
                    "provider": "traework",
                    "access_token": extracted["token"],
                    "token": extracted["token"],
                    "user_id": extracted.get("user_id") or "",
                    "user_region": extracted.get("user_region") or "",
                    "scope": extracted.get("scope") or "",
                    "machine_id": device_headers.get("X-Machine-Id") or "",
                    "device_id": device_headers.get("X-Device-Id") or "",
                    "ug_api_base": "",
                    "source_path": str(gs / "storage.json"),
                    "token_hint": mask_secret(extracted["token"]),
                    "auth_key": key,
                }, None
            last_err = f"无法解密 {key}（可能是 Electron safeStorage，需本机会话用户）"

        # state.vscdb fallback
        db = gs / "state.vscdb"
        if db.exists():
            try:
                conn = sqlite3.connect(str(db))
                rows = conn.execute(
                    "SELECT key, value FROM ItemTable WHERE key LIKE 'iCubeAuthInfo%'"
                ).fetchall()
                conn.close()
                for key, value in rows:
                    if not isinstance(value, str):
                        continue
                    obj = _try_decrypt_auth_value(value)
                    extracted = _extract_token_from_obj(obj) if obj else None
                    if extracted and extracted.get("token"):
                        return {
                            "provider": "traework",
                            "access_token": extracted["token"],
                            "token": extracted["token"],
                            "user_id": extracted.get("user_id") or "",
                            "user_region": extracted.get("user_region") or "",
                            "scope": extracted.get("scope") or "",
                            "machine_id": device_headers.get("X-Machine-Id") or "",
                            "device_id": device_headers.get("X-Device-Id") or "",
                            "ug_api_base": "",
                            "source_path": str(db),
                            "token_hint": mask_secret(extracted["token"]),
                            "auth_key": key,
                        }, None
            except Exception as exc:
                last_err = f"读取 state.vscdb 失败: {exc}"

    # 仍返回设备头，便于手工补 token
    headers = load_device_headers()
    return {
        "provider": "traework",
        "access_token": "",
        "token": "",
        "user_id": "",
        "machine_id": headers.get("X-Machine-Id") or "",
        "device_id": headers.get("X-Device-Id") or "",
        "ug_api_base": "",
        "needs_manual_token": True,
        "token_hint": "(无)",
    }, last_err + "；可在账号库手工粘贴 token（并保留 machine_id/device_id）"


def _api_post(url: str, headers: dict[str, str], timeout: float = 30.0) -> tuple[int, dict[str, Any]]:
    req = Request(
        url,
        method="POST",
        data=b"{}",
        headers=headers,
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, {"raw": raw[:300]}
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        try:
            return exc.code, json.loads(body)
        except Exception:
            return exc.code, {"msg": body[:200]}
    except URLError as exc:
        return -1, {"msg": f"网络失败: {exc}"}
    except Exception as exc:
        return -1, {"msg": str(exc)}


def _build_headers(token_blob: dict[str, Any]) -> dict[str, str]:
    token = str(token_blob.get("access_token") or token_blob.get("token") or "").strip()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "CheckinTool/1.0",
        "Authorization": f"Bearer {token}",
        "x-cloudide-token": token,
        "X-Cloudide-Token": token,
    }
    machine = str(token_blob.get("machine_id") or "").strip()
    device = str(token_blob.get("device_id") or "").strip()
    if machine:
        headers["X-Machine-Id"] = machine
        headers["x-machine-id"] = machine
    if device:
        headers["X-Device-Id"] = device
        headers["x-device-id"] = device
    user_id = str(token_blob.get("user_id") or "").strip()
    if user_id:
        headers["X-User-Id"] = user_id
        headers["x-user-id"] = user_id
    region = str(token_blob.get("user_region") or "").strip()
    if region:
        headers["X-User-Region"] = region
    return headers


def _candidate_bases(token_blob: dict[str, Any]) -> list[str]:
    bases: list[str] = []
    custom = str(token_blob.get("ug_api_base") or "").strip().rstrip("/")
    if custom:
        bases.append(custom)
    for b in DEFAULT_UG_API_BASES:
        if b not in bases:
            bases.append(b)
    return bases


def query_from_blob(token_blob: dict[str, Any], *, timeout: float = 30.0) -> dict[str, Any]:
    token = str(token_blob.get("access_token") or token_blob.get("token") or "").strip()
    if not token:
        return {"ok": False, "message": "缺少 token"}
    local_headers = load_device_headers()
    token_blob = {
        **token_blob,
        "machine_id": token_blob.get("machine_id") or local_headers.get("X-Machine-Id") or "",
        "device_id": token_blob.get("device_id") or local_headers.get("X-Device-Id") or "",
    }
    headers = _build_headers(token_blob)
    for base in _candidate_bases(token_blob):
        http, resp = _api_post(f"{base}{STATUS_PATH}", headers, timeout=timeout)
        data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
        if http == 200 and isinstance(data, dict):
            return {
                "ok": True,
                "base": base,
                "enable": data.get("enable"),
                "today_checked_in": data.get("checked_in"),
                "credits": data.get("credits"),
                "message": "",
                "raw": data,
            }
    return {"ok": False, "message": "查询失败"}


def checkin_with_blob(token_blob: dict[str, Any], *, timeout: float = 30.0) -> CheckinResult:
    token = str(token_blob.get("access_token") or token_blob.get("token") or "").strip()
    if not token:
        return CheckinResult(ok=False, provider="traework", message="缺少 TraeWork token")
    if not token_blob.get("machine_id") or not token_blob.get("device_id"):
        local_headers = load_device_headers()
        token_blob = {
            **token_blob,
            "machine_id": token_blob.get("machine_id") or local_headers.get("X-Machine-Id") or "",
            "device_id": token_blob.get("device_id") or local_headers.get("X-Device-Id") or "",
        }
    if not token_blob.get("machine_id") or not token_blob.get("device_id"):
        return CheckinResult(
            ok=False,
            provider="traework",
            message="缺少 X-Machine-Id / X-Device-Id（请先打开过 TraeWork 桌面端）",
        )

    def _once() -> CheckinResult:
        headers = _build_headers(token_blob)
        last_err = "所有 ugApi 均失败"
        for base in _candidate_bases(token_blob):
            status_url = f"{base}{STATUS_PATH}"
            claim_url = f"{base}{CLAIM_PATH}"
            http, resp = _api_post(status_url, headers, timeout=timeout)
            if http < 0:
                last_err = str(resp.get("msg") or last_err)
                return CheckinResult(
                    ok=False,
                    provider="traework",
                    message=last_err,
                    raw_summary={"retryable": True, "base": base},
                )
            data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
            if not isinstance(data, dict):
                last_err = f"{base} 响应无效 HTTP {http}"
                continue

            enable = data.get("enable")
            checked_in = data.get("checked_in")
            credits = data.get("credits")
            code = data.get("code") if "code" in data else resp.get("code")

            if code in (9004, "9004"):
                last_err = f"{base} 参数错误(9004)，请检查设备头/token"
                continue
            if code in (9074, "9074"):
                return CheckinResult(
                    ok=False,
                    provider="traework",
                    message="服务器繁忙(9074)，请稍后重试",
                    raw_summary={"base": base, "code": 9074, "retryable": True},
                )

            if http == 200 and enable is False:
                return CheckinResult(
                    ok=True,
                    provider="traework",
                    already=True,
                    message=f"签到未开放（{base}）",
                    raw_summary={"base": base, "enable": False},
                )

            if http == 200 and checked_in is True:
                return CheckinResult(
                    ok=True,
                    provider="traework",
                    already=True,
                    credits=int(credits) if isinstance(credits, (int, float)) else 200,
                    message=f"今天已签到（{base}）",
                    raw_summary={"base": base, "checked_in": True},
                )

            if http == 200 and (checked_in is False or enable is True or "checked_in" in data):
                http2, resp2 = _api_post(claim_url, headers, timeout=timeout)
                data2 = resp2.get("data") if isinstance(resp2.get("data"), dict) else resp2
                code2 = data2.get("code") if isinstance(data2, dict) and "code" in data2 else resp2.get("code")
                if http2 == 200 and (
                    code2 in (0, None, "0")
                    or (isinstance(data2, dict) and data2.get("checked_in") is not False)
                ):
                    got = None
                    if isinstance(data2, dict):
                        got = data2.get("credits") or data2.get("credit")
                    return CheckinResult(
                        ok=True,
                        provider="traework",
                        credits=int(got) if isinstance(got, (int, float)) else 200,
                        message=f"签到成功（{base}）",
                        raw_summary={"base": base, "claimed": True},
                    )
                if code2 in (9074, "9074") or http2 < 0:
                    return CheckinResult(
                        ok=False,
                        provider="traework",
                        message="服务器繁忙/网络失败，将重试",
                        raw_summary={"base": base, "code": code2, "retryable": True},
                    )
                last_err = f"领取失败 HTTP {http2} / {resp2}"
                continue

            last_err = f"{base} HTTP {http} / {str(resp)[:160]}"
        return CheckinResult(ok=False, provider="traework", message=last_err)

    def _should_retry(result: CheckinResult) -> bool:
        return (not result.ok) and bool((result.raw_summary or {}).get("retryable"))

    return retry_call(_once, retries=10, min_wait=15, max_wait=30, should_retry=_should_retry)


def checkin_from_local(user_dir: str | Path | None = None) -> CheckinResult:
    auth, err = load_local_auth(user_dir)
    if not auth or not auth.get("token"):
        return CheckinResult(ok=False, provider="traework", message=err or "无登录态")
    return checkin_with_blob(auth)


def checkin_from_blob(token_blob: dict[str, Any]) -> CheckinResult:
    return checkin_with_blob(token_blob)
