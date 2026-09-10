# -*- coding: utf-8 -*-
"""对接 run-jane app-license：兑换 / 查状态（明文 JSON，兼容加密通道）。"""

from __future__ import annotations

import json
import os
import platform
import socket
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .secure_storage import load_json as secure_load_json, save_json as secure_save_json

DEFAULT_APP_KEY = "points-checkin"
DEFAULT_BASE_URL = "http://111.229.202.251"
DEFAULT_TIMEOUT = 15.0


def _data_root() -> Path:
    if getattr(sys, "frozen", False):
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
        root = base / "CheckinTool"
        root.mkdir(parents=True, exist_ok=True)
        return root
    return Path(__file__).resolve().parent.parent / "data"


def data_root() -> Path:
    root = _data_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


LICENSE_CACHE = data_root() / "license_cache.json"
DEVICE_ID_FILE = data_root() / "device_id.txt"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        text = str(s).strip().replace("Z", "+00:00")
        if " " in text and "T" not in text[:20]:
            text = text.replace(" ", "T", 1)
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def device_fingerprint() -> str:
    DEVICE_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
    mid = ""
    if DEVICE_ID_FILE.exists():
        try:
            mid = DEVICE_ID_FILE.read_text(encoding="utf-8").strip()
        except Exception:
            mid = ""
    if not mid:
        mid = str(uuid.uuid4())
        try:
            DEVICE_ID_FILE.write_text(mid + "\n", encoding="utf-8")
        except Exception:
            pass
    host = platform.node() or socket.gethostname() or "host"
    user = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
    system = platform.system() or "OS"
    return f"{mid}|{system}|{host}|{user}"


def device_name() -> str:
    return f"{platform.node() or 'PC'} ({platform.system()})"


def load_cache() -> dict[str, Any]:
    if not LICENSE_CACHE.exists():
        return {}
    try:
        data = secure_load_json(LICENSE_CACHE, {})
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_cache(data: dict[str, Any]) -> None:
    LICENSE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    secure_save_json(LICENSE_CACHE, data)


def clear_cache() -> None:
    if LICENSE_CACHE.exists():
        try:
            LICENSE_CACHE.unlink()
        except Exception:
            pass


def _normalize_base(url: str) -> str:
    u = (url or "").strip().rstrip("/")
    if not u:
        return ""
    if u.endswith("/api"):
        return u[:-4]
    return u


def license_cfg_from_settings(settings: dict[str, Any]) -> dict[str, Any]:
    base = (
        (settings.get("license_base_url") or "").strip()
        or os.environ.get("CHECKIN_LICENSE_URL", "").strip()
        or DEFAULT_BASE_URL
    )
    app_key = (
        (settings.get("license_app_key") or "").strip()
        or os.environ.get("CHECKIN_APP_KEY", "").strip()
        or DEFAULT_APP_KEY
    )
    return {
        "base_url": _normalize_base(base),
        "app_key": app_key,
        "require_license": True,
        "timeout": float(settings.get("license_timeout") or DEFAULT_TIMEOUT),
        "prefer_crypto": bool(settings.get("prefer_crypto", False)),
    }


def _endpoint(base: str, app_key: str, action: str) -> str:
    return f"{base}/api/app-license/{app_key}/{action}"


def _unwrap(resp: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    if not isinstance(resp, dict):
        return False, "响应无效", {}
    code = resp.get("code")
    if code is None and (
        "valid" in resp or "ticket" in resp or "timeUnlimited" in resp or "planLabel" in resp
    ):
        return True, str(resp.get("message") or ""), resp
    msg = str(resp.get("message") or resp.get("msg") or "")
    data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
    ok = code in (200, 0, "200", "0")
    if not ok and not msg:
        msg = f"请求失败 code={code}"
    return ok, msg, data


def _post_plain(url: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = Request(
        url,
        data=raw,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "CheckinTool/1.0",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            return payload if isinstance(payload, dict) else {"code": -1, "message": "响应无效"}
    except HTTPError as exc:
        body_text = ""
        try:
            body_text = exc.read().decode("utf-8", errors="ignore")
            value = json.loads(body_text)
            if isinstance(value, dict):
                return value
        except Exception:
            pass
        return {"code": exc.code, "message": body_text[:200] or f"HTTP {exc.code}"}
    except URLError as exc:
        return {"code": -1, "message": f"网络失败: {exc}"}
    except Exception as exc:
        return {"code": -1, "message": str(exc)}


def _post_json(url: str, body: dict[str, Any], timeout: float, scope: str, prefer_crypto: bool) -> dict[str, Any]:
    if prefer_crypto:
        try:
            from .crypto_transport import CryptoTransportError, secure_json_request

            return secure_json_request(url, body, scope, timeout)
        except Exception as exc:
            # 回退明文
            plain = _post_plain(url, body, timeout)
            if plain.get("code") in (200, 0, "200", "0") or isinstance(plain.get("data"), dict):
                return plain
            return {"code": -1, "message": f"加密通道失败且明文失败: {exc}"}
    return _post_plain(url, body, timeout)


def _cache_from_data(data: dict[str, Any], app_key: str) -> dict[str, Any]:
    return {
        "app_key": app_key,
        "valid": bool(data.get("valid")),
        "expireAt": data.get("expireAt"),
        "timeUnlimited": bool(data.get("timeUnlimited")),
        "planLabel": data.get("planLabel") or "",
        "message": data.get("message") or "",
        "deviceCount": data.get("deviceCount"),
        "maxDevices": data.get("maxDevices"),
        "ticket": data.get("ticket") or "",
        "ticketExpireAt": data.get("ticketExpireAt"),
        "updatedAt": _now_iso(),
    }


def ticket_still_valid(cache: dict[str, Any] | None = None) -> bool:
    cache = cache or load_cache()
    if not cache.get("valid"):
        return False
    if cache.get("timeUnlimited"):
        return True
    exp = _parse_iso(cache.get("expireAt"))
    if exp and exp > datetime.now(timezone.utc):
        return True
    texp = cache.get("ticketExpireAt")
    if isinstance(texp, (int, float)) and texp > datetime.now(timezone.utc).timestamp() * 1000:
        return bool(cache.get("ticket"))
    texp_dt = _parse_iso(str(texp) if texp else None)
    if texp_dt and texp_dt > datetime.now(timezone.utc) and cache.get("ticket"):
        return True
    return False


def check_status(settings: dict[str, Any], *, force_online: bool = False) -> dict[str, Any]:
    cfg = license_cfg_from_settings(settings)
    cache = load_cache()
    fp = device_fingerprint()
    public_cfg = {"base_url": cfg["base_url"], "app_key": cfg["app_key"], "require_license": True}

    if not cfg["base_url"]:
        return {
            "ok": False,
            "valid": False,
            "message": "未配置授权服务地址 license_base_url",
            "deviceFingerprint": fp,
            "cfg": public_cfg,
        }

    if not force_online and ticket_still_valid(cache):
        return {
            "ok": True,
            "valid": True,
            "offline": True,
            "message": cache.get("message") or "本地票据有效",
            "license": cache,
            "deviceFingerprint": fp,
            "cfg": public_cfg,
        }

    body: dict[str, Any] = {"deviceFingerprint": fp}
    if cache.get("ticket"):
        body["ticket"] = cache["ticket"]

    resp = _post_json(
        _endpoint(cfg["base_url"], cfg["app_key"], "status"),
        body,
        cfg["timeout"],
        "app-license.status",
        cfg["prefer_crypto"],
    )
    ok, msg, data = _unwrap(resp)
    if not ok:
        if ticket_still_valid(cache):
            return {
                "ok": True,
                "valid": True,
                "offline": True,
                "message": f"在线校验失败，使用本地票据：{msg}",
                "license": cache,
                "deviceFingerprint": fp,
                "cfg": public_cfg,
            }
        return {
            "ok": False,
            "valid": False,
            "message": msg or "授权无效",
            "raw": resp,
            "deviceFingerprint": fp,
            "cfg": public_cfg,
            "license": cache,
        }

    cached = _cache_from_data(data, cfg["app_key"])
    save_cache(cached)
    return {
        "ok": True,
        "valid": bool(data.get("valid")),
        "message": data.get("message") or msg or ("授权有效" if data.get("valid") else "未激活"),
        "license": cached,
        "deviceFingerprint": fp,
        "cfg": public_cfg,
    }


def redeem(settings: dict[str, Any], card_code: str) -> dict[str, Any]:
    cfg = license_cfg_from_settings(settings)
    code = (card_code or "").strip()
    if not code:
        return {"ok": False, "message": "请输入卡密"}
    if not cfg["base_url"]:
        return {"ok": False, "message": "未配置授权服务地址"}

    fp = device_fingerprint()
    resp = _post_json(
        _endpoint(cfg["base_url"], cfg["app_key"], "redeem"),
        {
            "cardCode": code,
            "deviceFingerprint": fp,
            "deviceName": device_name(),
        },
        cfg["timeout"],
        "app-license.redeem",
        cfg["prefer_crypto"],
    )
    ok, msg, data = _unwrap(resp)
    if not ok:
        return {"ok": False, "message": msg or "兑换失败", "raw": resp}

    cached = _cache_from_data(data, cfg["app_key"])
    if "valid" not in data:
        cached["valid"] = True
    save_cache(cached)
    return {
        "ok": True,
        "valid": bool(cached.get("valid")),
        "message": data.get("message") or "兑换成功",
        "license": cached,
        "deviceFingerprint": fp,
    }


def ensure_licensed(settings: dict[str, Any], *, force_online: bool = True) -> tuple[bool, str]:
    result = check_status(settings, force_online=force_online)
    if result.get("valid"):
        return True, result.get("message") or "ok"
    return False, result.get("message") or "请先激活卡密"


def auth_headers(settings: dict[str, Any]) -> dict[str, Any]:
    """供代跑 API 使用的卡密/设备字段。"""
    cache = load_cache()
    return {
        "deviceFingerprint": device_fingerprint(),
        "deviceName": device_name(),
        "ticket": cache.get("ticket") or "",
        "cardCode": str(settings.get("card_code") or cache.get("primaryCard") or "").strip(),
    }
