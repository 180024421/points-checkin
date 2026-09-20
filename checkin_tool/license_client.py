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

from .redact import mask_text, redact as _redact_secrets
from .secure_storage import load_json as secure_load_json, save_json as secure_save_json

DEFAULT_APP_KEY = "points-checkin"
DEFAULT_BASE_URL = "http://111.229.202.251"
DEFAULT_TIMEOUT = 15.0


def _is_https(url: str) -> bool:
    return str(url or "").strip().lower().startswith("https://")


def insecure_transport_allowed(settings: dict[str, Any] | None) -> bool:
    """是否允许走明文 HTTP 与授权/代跑服务通信。

    默认 True 只为兼容当前尚无 TLS 的授权服务器；一旦服务端配好证书，
    把 allow_insecure_transport 设为 False 即可强制 https。
    """
    return bool((settings or {}).get("allow_insecure_transport", True))


def transport_guard(cfg: dict[str, Any]) -> str:
    """返回错误消息；空字符串表示放行。"""
    base = str(cfg.get("base_url") or "")
    if base and not _is_https(base) and not cfg.get("allow_insecure_transport", True):
        return (
            "授权服务地址是明文 HTTP，已按设置禁止不安全传输。"
            "请改用 https 地址，或在设置中临时允许 allow_insecure_transport。"
        )
    return ""


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


_MILLIS_THRESHOLD = 1e11  # > 1e11 视为毫秒；秒级 epoch 现在约 1.7e9，不会误判


def _epoch_to_dt(num: float) -> datetime:
    return datetime.fromtimestamp(num / 1000.0 if num > _MILLIS_THRESHOLD else num, timezone.utc)


def _parse_iso(s: Any) -> datetime | None:
    """解析到期时间，三种线格式都要吃：

    1. ISO 字符串（``2026-10-14T23:25:44``）—— src 那代 DTO 直出。
    2. 空格分隔（``2026-10-14 23:25:44``）—— 服务端实发形态。
    3. epoch 数字（``1764000000000``）—— 线上那代返回 Map 的 DTO 把 ``expireAt``
       以 ``java.util.Date`` 直出，Jackson 序列化成毫秒数字。旧实现只认字符串，
       于是到期时间一律解析成 None：时长轴在客户端等于不存在，
       ``ticket_still_valid`` 也少一条判据（只能靠 ticketExpireAt 兜）。
    """
    if s is None or s == "":
        return None
    if isinstance(s, (int, float)) and not isinstance(s, bool):
        return _epoch_to_dt(float(s))
    try:
        text = str(s).strip().replace("Z", "+00:00")
        if text.lstrip("-").isdigit():
            return _epoch_to_dt(float(text))
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
        "allow_insecure_transport": bool(settings.get("allow_insecure_transport", True)),
        # 加密通道不可用时是否允许退回明文请求。默认关闭：明文回退等于给
        # 中间人一个「让请求降级」的开关，卡密与 ticket 都会裸奔。
        "allow_plain_fallback": bool(settings.get("allow_plain_fallback", False)),
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
        # 拿到了 HTTP 状态码 = 服务端明确应答，与断网区别对待（授权守卫据此判断是否踢出）
        return {"code": exc.code, "message": user_message, "networkError": False}
    except URLError as exc:
        return {
            "code": -1,
            "message": f"无法连接到授权服务器，请检查网络连接: {mask_text(exc.reason)}",
            "networkError": True,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "code": -1,
            "message": f"授权服务通信异常，请联系技术支持。详情: {mask_text(exc)}",
            "networkError": True,
        }


def _post_json(
    url: str,
    body: dict[str, Any],
    timeout: float,
    scope: str,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """敏感接口优先走加密信封。

    加密失败时只在「TLS 通道下」或「显式 allow_plain_fallback」时回退明文，
    避免中间人只要干扰加密握手就能把卡密 / ticket 降级成裸明文请求。
    """
    cfg = cfg or {}
    prefer_crypto = bool(cfg.get("prefer_crypto"))
    allow_plain = bool(cfg.get("allow_plain_fallback")) or _is_https(url)
    try:
        from .crypto_transport import secure_json_request

        return secure_json_request(url, body, scope, timeout)
    except Exception as exc:  # noqa: BLE001
        if not allow_plain:
            return {
                "code": -1,
                "cryptoError": True,
                "networkError": _looks_like_network_error(exc),
                "message": (
                    "与授权服务器建立加密通道失败，已拒绝降级为明文请求。"
                    f"请检查网络环境或联系技术支持。详情: {mask_text(exc, 160)}"
                ),
            }
        plain = _post_plain(url, body, timeout)
        if not prefer_crypto or not plain.get("networkError"):
            # 明文请求真的到了服务端：业务码和消息（例如「卡密已被使用」）必须原样
            # 返回。包成「安全通信失败」会让界面提示和踢出判定都丢掉真实原因。
            return plain
        return {
            "code": -1,
            "message": f"与授权服务器安全通信失败，请检查网络环境或联系技术支持。详情: {mask_text(exc, 160)}",
            "networkError": _looks_like_network_error(exc),
        }


def _looks_like_network_error(exc: BaseException | str) -> bool:
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "timed out", "timeout", "unreachable", "getaddrinfo",
            "connection", "refused", "reset by peer", "网络失败",
        )
    )


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


def public_license_view(data: dict[str, Any] | None) -> dict[str, Any]:
    """给界面用的授权摘要：去掉 ticket / 卡密，前端不需要也不该拿到它们。"""
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k not in ("ticket", "primaryCard")}


def ticket_still_valid(cache: dict[str, Any] | None = None) -> bool:
    cache = load_cache() if cache is None else cache
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

    blocked_msg = transport_guard(cfg)
    if blocked_msg:
        return {
            "ok": False,
            "valid": False,
            "message": blocked_msg,
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
        cfg,
    )
    ok, msg, data = _unwrap(resp)

    # 额度字段一律以 app-license 下发为准。
    # 历史实现曾把大帅网关的 tokenQuota/tokenUsed（Token 用量）和卡密 usageLimit（使用次数）
    # 覆盖到 accountLimit 上，二者都不是「账号数」，会让额度显示成荒谬数字，已移除。

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
            "raw": _redact_secrets(resp),
            "networkError": bool(resp.get("networkError")),
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
    blocked_msg = transport_guard(cfg)
    if blocked_msg:
        return {"ok": False, "message": blocked_msg}

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
        cfg,
    )
    ok, msg, data = _unwrap(resp)
    if not ok:
        return {
            "ok": False,
            "message": msg or "兑换失败",
            "raw": _redact_secrets(resp),
            "networkError": bool(resp.get("networkError")),
        }

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
