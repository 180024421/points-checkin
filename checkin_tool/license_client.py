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

from .run_jane_api import get_license_status, lookup_card_code_with_usage
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


def _stable_machine_id() -> str:
    """本机稳定标识：Windows MachineGuid → 物理 MAC；都取不到时返回空字符串。

    用于保证重装 / 升级 / 删除数据目录后仍是“同一台设备”，
    避免服务端把同一台机器重复计入设备数（maxDevices）。
    """
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography"
            ) as key:
                value = winreg.QueryValueEx(key, "MachineGuid")[0]
                text = str(value).strip()
                if text:
                    return text
        except Exception:
            pass
    try:
        mac = uuid.getnode()
        if mac and not (mac >> 40) & 0x01:  # 非随机 MAC
            return uuid.UUID(int=mac).hex[-12:]
    except Exception:
        pass
    return ""


def _restored_device_id() -> str:
    for directory in _backup_dirs():
        path = directory / "device_id.txt"
        if not path.exists():
            continue
        try:
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        except Exception:
            continue
    return ""


def _mirror_device_id(mid: str) -> None:
    for directory in _backup_dirs():
        try:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "device_id.txt").write_text(mid + "\n", encoding="utf-8")
        except Exception:
            continue


def device_fingerprint() -> str:
    DEVICE_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
    mid = ""
    if DEVICE_ID_FILE.exists():
        try:
            mid = DEVICE_ID_FILE.read_text(encoding="utf-8").strip()
        except Exception:
            mid = ""
    if not mid:
        # 1) 异地备份（重装后恢复原设备身份，不额外占用设备数）
        # 2) 本机稳定标识（MachineGuid / MAC）
        # 3) 随机 UUID（兜底）
        mid = _restored_device_id() or _stable_machine_id() or str(uuid.uuid4())
        try:
            DEVICE_ID_FILE.write_text(mid + "\n", encoding="utf-8")
        except Exception:
            pass
        _mirror_device_id(mid)
    host = platform.node() or socket.gethostname() or "host"
    user = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
    system = platform.system() or "OS"
    return f"{mid}|{system}|{host}|{user}"


def device_name() -> str:
    return f"{platform.node() or 'PC'} ({platform.system()})"


def _backup_dirs() -> list[Path]:
    """异地备份目录：重装或删除数据目录后仍保留。"""
    dirs: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(Path(local) / "CheckinTool")
    try:
        dirs.append(Path.home() / ".checkintool")
    except Exception:
        pass
    return dirs


def _backup_files() -> list[Path]:
    """卡密/票据的异地备份位置，保证重装或删除数据目录后仍可恢复。"""
    return [d / "license_cache.json" for d in _backup_dirs()]


def _restore_cache_from_backup() -> dict[str, Any]:
    for path in _backup_files():
        if not path.exists():
            continue
        try:
            data = secure_load_json(path, {})
        except Exception:
            continue
        if isinstance(data, dict) and data:
            try:  # 恢复回主目录
                LICENSE_CACHE.parent.mkdir(parents=True, exist_ok=True)
                secure_save_json(LICENSE_CACHE, data)
            except Exception:
                pass
            return data
    return {}


def load_cache() -> dict[str, Any]:
    if LICENSE_CACHE.exists():
        try:
            data = secure_load_json(LICENSE_CACHE, {})
            if isinstance(data, dict) and data:
                return data
        except Exception:
            pass
    return _restore_cache_from_backup()


def save_cache(data: dict[str, Any]) -> None:
    LICENSE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    secure_save_json(LICENSE_CACHE, data)
    for path in _backup_files():  # 异地镜像，重装后自动找回
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            secure_save_json(path, data)
        except Exception:
            continue


def clear_cache() -> None:
    for path in [LICENSE_CACHE, *_backup_files()]:
        if path.exists():
            try:
                path.unlink()
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
        server_message = ""
        try:
            body_text = exc.read().decode("utf-8", errors="ignore")
            value = json.loads(body_text)
            if isinstance(value, dict) and (value.get("message") or value.get("msg")):
                server_message = value.get("message") or value.get("msg")
        except Exception:
            pass
        
        # Prioritize server's message, otherwise provide a user-friendly HTTP error
        user_message = server_message or f"服务器响应错误（HTTP {exc.code}），请稍后再试。"
        return {"code": exc.code, "message": user_message}
    except URLError as exc:
        return {"code": -1, "message": f"无法连接到授权服务器，请检查网络连接: {exc.reason}"}
    except Exception as exc:
        return {"code": -1, "message": f"授权服务通信异常，请联系技术支持。详情: {str(exc)}"}


def _post_json(url: str, body: dict[str, Any], timeout: float, scope: str, prefer_crypto: bool) -> dict[str, Any]:
    # 服务端敏感接口已强制要求加密信封，始终优先走加密通道，失败时回退明文以兼容旧服务端。
    try:
        from .crypto_transport import CryptoTransportError, secure_json_request

        return secure_json_request(url, body, scope, timeout)
    except Exception as exc:
        plain = _post_plain(url, body, timeout)
        if prefer_crypto:
            if plain.get("code") in (200, 0, "200", "0") or isinstance(plain.get("data"), dict):
                return plain
            return {"code": -1, "message": f"与授权服务器安全通信失败，请检查网络环境或联系技术支持。详情: {exc}"}
        return plain


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
        # 按账号数计费：服务端下发可代挂账号数 / 已用 / 套餐名
        "accountLimit": data.get("accountLimit"),
        "accountUsed": data.get("accountUsed"),
        "accountPlanLabel": data.get("accountPlanLabel") or "",
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

    # Fetch gateway license status for account limits
    gateway_license_data: dict[str, Any] = {}
    try:
        gateway_resp = get_license_status(fp)
        # Assuming gateway_resp already contains the relevant fields like tokenQuota and tokenUsed
        if isinstance(gateway_resp, dict):
            gateway_license_data = gateway_resp
    except Exception as exc:
        print(f"Failed to fetch gateway license status: {exc}") # Log the error, but don't block
        # Continue with app-license data if gateway license fetch fails

    # Merge gateway license data into current data, prioritizing gateway for account limits
    if gateway_license_data:
        data["accountLimit"] = gateway_license_data.get("tokenQuota")
        data["accountUsed"] = gateway_license_data.get("tokenUsed")

    # Fetch card code usage limit if a card code is configured
    configured_card_code = settings.get("card_code")
    if configured_card_code:
        try:
            card_code_lookup_resp = lookup_card_code_with_usage(configured_card_code)
            if card_code_lookup_resp and card_code_lookup_resp.get("usageLimit") is not None:
                data["accountLimit"] = card_code_lookup_resp["usageLimit"]
                # You might also want to add cardCode status or expiry to data if needed
                # For now, we only care about usageLimit
        except Exception as exc:
            print(f"Failed to fetch card code usage limit: {exc}") # Log the error

    # Optionally, update message or validity based on gateway license if needed
    # For now, keep app-license's validity and message as primary for overall license status

    if not ok:
        # 票据失效（过期 / 设备变更 / 重装）：用已保存的卡密静默重新激活
        renewed = _auto_renew(cfg, cache, fp, public_cfg)
        if renewed:
            return renewed
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
    if cache.get("primaryCard"):
        cached["primaryCard"] = cache["primaryCard"]  # 保卡密，供下次自动续期
    save_cache(cached)
    return {
        "ok": True,
        "valid": bool(data.get("valid")),
        "message": data.get("message") or msg or ("授权有效" if data.get("valid") else "未激活"),
        "license": cached,
        "deviceFingerprint": fp,
        "cfg": public_cfg,
    }


def _auto_renew(
    cfg: dict[str, Any],
    cache: dict[str, Any],
    fp: str,
    public_cfg: dict[str, Any],
) -> dict[str, Any] | None:
    """用本地已保存的卡密重新激活；成功返回 check_status 风格结果，否则 None。"""
    card = str(cache.get("primaryCard") or "").strip()
    if not card:
        return None
    try:
        result = redeem(
            {
                "license_base_url": cfg["base_url"],
                "license_app_key": cfg["app_key"],
                "license_timeout": cfg["timeout"],
                "prefer_crypto": cfg["prefer_crypto"],
            },
            card,
        )
    except Exception:
        return None
    if not result.get("ok"):
        return None
    license_cache = result.get("license") or load_cache()
    return {
        "ok": True,
        "valid": bool(license_cache.get("valid")),
        "renewed": True,
        "message": result.get("message") or "已用本地卡密自动续期",
        "license": license_cache,
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
    cached["primaryCard"] = code  # 记住卡密，后续自动续期，无需再次填写
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
