# -*- coding: utf-8 -*-
"""run-jane 代跑 API 客户端（卡密 + 设备指纹）。

另外负责「代跑凭证保鲜」：服务端 worker 只用上传时的 access_token 打接口，
不会自己续期；所以本机一旦把 token 刷新成功，就要把最新 tokenBlob 回传，
否则到期后服务端会一直签到失败。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from . import account_store
from .license_client import (
    auth_headers,
    device_fingerprint,
    license_cfg_from_settings,
    load_cache,
    transport_guard,
)
from .redact import mask_text
from .settings import load_settings

LogFn = Callable[[str], None]


def _post(path: str, body: dict[str, Any], *, params: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = load_settings()
    cfg = license_cfg_from_settings(settings)
    base = cfg["base_url"]
    if not base:
        return {"ok": False, "message": "未配置授权服务地址"}
    blocked_msg = transport_guard(cfg)
    if blocked_msg:
        return {"ok": False, "message": blocked_msg}
    url = f"{base}/api/points-checkin{path}"
    payload = {**auth_headers(settings), **body}
    # ensure ticket present
    cache = load_cache()
    if cache.get("ticket") and not payload.get("ticket"):
        payload["ticket"] = cache["ticket"]
    if params:
        # 少数接口只读 query 参数（不解析 JSON body）。这里只放设备指纹，
        # ticket / 卡密一律留在 body 里，避免落进 nginx 访问日志。
        url = f"{url}?{urlencode({k: v for k, v in params.items() if v})}"

    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
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
        with urlopen(req, timeout=cfg["timeout"]) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        server_message = ""
        try:
            data = json.loads(exc.read().decode("utf-8", errors="ignore"))
            # Prioritize server's message from the JSON response
            if isinstance(data, dict) and (data.get("message") or data.get("msg")):
                server_message = data.get("message") or data.get("msg")
        except Exception:
            pass # Failed to parse JSON error, fall back to generic message

        # If server_message is available, use it. Otherwise, provide a user-friendly HTTP error.
        user_message = server_message or f"服务器响应错误（HTTP {exc.code}），请稍后再试。"
        return {"ok": False, "message": user_message}
    except URLError as exc:
        return {"ok": False, "message": f"无法连接到服务器，请检查网络连接: {mask_text(exc.reason)}"}
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "message": f"服务器通信异常，请联系技术支持。详情: {mask_text(exc, 160)}",
        }

    if not isinstance(data, dict):
        return {"ok": False, "message": "响应无效"}
    code = data.get("code")
    ok = code in (200, 0, "200", "0", None) and data.get("ok") is not False
    message = data.get("message") or data.get("msg") or ""
    result = data.get("data") if isinstance(data.get("data"), dict) else data.get("data")
    if ok and (path.startswith("/accounts/") or path == "/runs/run-now"):
        # 只有「会改变服务器状态」的接口才失效缓存；读取类接口（/runs/today、
        # /checkin-data/aggregated）不能失效，否则缓存永远命中不了。
        account_store.invalidate_server_cache()
    if ok and path.startswith("/entitlement/"):
        account_store.invalidate_entitlement_cache()
    return {"ok": bool(ok), "message": message, "data": result, "raw": data}


def _pick(mapping: dict[str, Any], *keys: str) -> Any:
    """服务端字段名不保证稳定（补丁仓库里只有 controller，没有 service 实现），
    因此按候选名逐个取，取不到就是 None。"""
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "y", "on"):
        return True
    if text in ("0", "false", "no", "n", "off", ""):
        return False
    return None


def normalize_entitlement(data: dict[str, Any]) -> dict[str, Any]:
    quota = _as_int(_pick(data, "quota", "accountQuota", "account_quota", "account_limit", "accountLimit"))
    used = _as_int(_pick(data, "used", "accountUsed", "used_count", "usedCount"))
    return {
        # quota 服务端永远给数字（行缺失时兜底 default=10），所以 **0 是真额度**：
        # 坐席全部到期时 reconcileCapacity 会把 account_quota 反写成 0。
        # 旧实现在这里把 0 抹成 None（= 不限），于是本机显示「不限账号」，
        # 而服务端同期正把该授权的账号逐个停用 —— 两端口径必须一致。
        # 现网实测 8 条权益行 quota 全 > 0（最小 1），保留 0 不会误伤任何现有用户。
        # 负数仍按「未配置」处理。
        "quota": quota if quota is None or quota >= 0 else None,
        "used": used,
        "contactEmail": str(_pick(data, "contactEmail", "contact_email", "email") or ""),
        "contactVerified": _as_bool(_pick(data, "contactVerified", "contact_verified", "verified")),
        "licenseActive": _as_bool(_pick(data, "licenseActive", "license_active")),
        "expireAt": _pick(data, "expireAt", "expire_at"),
        "timeUnlimited": _as_bool(_pick(data, "timeUnlimited", "time_unlimited")),
    }


def entitlement_info() -> dict[str, Any]:
    """代挂额度 + 联系邮箱绑定状态。

    线上实测（2026-09）返回
    ``{quota, used, contactEmail, contactVerified, licenseActive, timeUnlimited, expireAt, boundAt}``，
    额度取自 ``checkin_entitlement``，是「可代挂账号数」的权威来源；
    授权状态里的 ``accountLimit`` 是设备座位数，语义不同，不能拿来当额度用。
    """
    resp = _post("/entitlement/info", {})
    if not resp.get("ok"):
        return {"ok": False, "message": resp.get("message") or "获取代挂额度失败"}
    data = resp.get("data")
    if not isinstance(data, dict):
        return {"ok": False, "message": "代挂额度响应格式无效"}
    return {"ok": True, "message": "", "data": normalize_entitlement(data)}


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def bind_contact(email: str) -> dict[str, Any]:
    """绑定联系邮箱（服务端下发验证码）。服务端要求代跑前必须绑定。"""
    email = str(email or "").strip()
    if not _EMAIL_RE.match(email):
        return {"ok": False, "message": "邮箱格式不正确"}
    return _post("/entitlement/bind", {"contactEmail": email})


def verify_contact(code: str) -> dict[str, Any]:
    code = str(code or "").strip()
    if not code:
        return {"ok": False, "message": "请输入邮箱验证码"}
    return _post("/entitlement/verify", {"verifyCode": code})


def upsert_server_account(account: dict[str, Any]) -> dict[str, Any]:
    blob = account.get("token_blob") if isinstance(account.get("token_blob"), dict) else {}
    return _post(
        "/accounts/upsert",
        {
            "provider": account.get("provider"),
            "accountLabel": account.get("label") or blob.get("nickname") or blob.get("uid") or "",
            "tokenBlob": blob,
            "enabled": bool(account.get("enabled", True)),
            "clientAccountId": account.get("id"),
            # WorkBuddy：服务器代跑时是否顺带执行成长中心任务
            "taskEnabled": bool(account.get("task_enabled")),
        },
    )


def token_fingerprint(account: dict[str, Any]) -> str:
    """当前 token 的指纹，用于判断相对上次上传是否有变化。"""
    blob = account.get("token_blob") if isinstance(account.get("token_blob"), dict) else {}
    return str(blob.get("token_hint") or blob.get("access_token") or blob.get("token") or "")


def sync_server_blob(
    account: dict[str, Any],
    *,
    log: LogFn | None = None,
    force: bool = False,
) -> dict[str, Any] | None:
    """代跑账号：本机 token 有更新就回传服务器，避免服务端 token 过期后一直失败。

    返回 None 表示无需同步（非代跑模式 / 无 token / token 与上次一致）。
    调用方传入的必须是账号库里的完整记录，同步成功后会把指纹回写落库。
    """
    if str(account.get("run_mode") or "local") != "server":
        return None
    blob = account.get("token_blob") if isinstance(account.get("token_blob"), dict) else {}
    if not (blob.get("token") or blob.get("access_token")):
        return None
    fp = token_fingerprint(account)
    if not force and fp and str(account.get("server_synced_hint") or "") == fp:
        return None

    label = account.get("label") or account.get("id")
    result = upsert_server_account(account)
    if result.get("ok"):
        account["server_synced_hint"] = fp
        account["server_synced_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if account.get("id"):
            # 回写同步戳失败不能掀翻整轮凭证保鲜（批量循环里逐条继续）
            stored = account_store.try_upsert_account(account)
            if not stored.get("ok") and log:
                log(f"同步标记写入失败（{label}）：{stored.get('message')}")
        if log:
            log(f"代跑凭证已同步服务器：{label}")
    elif log:
        log(f"代跑凭证同步失败（{label}）：{result.get('message')}")
    return result


def list_server_accounts() -> list[dict[str, Any]]:
    resp = _post("/accounts/list", {})
    if resp.get("ok") and isinstance(resp.get("data"), list):
        return [_normalize_server_account(row) for row in resp["data"] if isinstance(row, dict)]
    return []


# 服务端返回驼峰字段，本地账号记录用下划线；这里把两边对到一套键上，
# 合并视图才能认出「这条服务器记录就是本机那个账号」。
_SERVER_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "label": ("accountLabel", "account_label"),
    "identity": ("accountUid", "account_uid", "uid", "userId", "user_id"),
    "client_account_id": ("clientAccountId",),
    "server_account_id": ("boundAccountId", "bound_account_id"),
    "last_ok_at": ("lastOkAt",),
    "last_error": ("lastError",),
    "provider": ("providerName",),
}


def _normalize_server_account(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for snake, aliases in _SERVER_FIELD_ALIASES.items():
        if out.get(snake) is not None:
            continue
        for alias in aliases:
            if out.get(alias) is not None:
                out[snake] = out[alias]
                break
    if out.get("server_account_id") is None and out.get("id") is not None:
        # checkin_bound_account.id 是服务端自增大整数；本机 id 是 uuid，两者不会撞
        out["server_account_id"] = out["id"]
    blob = out.get("token_blob") or out.get("tokenBlob")
    out["token_blob"] = blob if isinstance(blob, dict) else {}
    out["id"] = str(out.get("id") or out.get("server_account_id") or "")
    if "enabled" in out:
        out["enabled"] = bool(out.get("enabled"))
    out["run_mode"] = "server"
    out["source"] = "server"  # 标记来源：这类行不允许写进本地 accounts.json
    return out


def delete_server_account(server_account_id: int | str) -> dict[str, Any]:
    return _post("/accounts/delete", {"id": server_account_id})


def today_runs() -> dict[str, Any]:
    return _post("/runs/today", {})

def fetch_aggregated_checkin_data() -> list[dict[str, Any]]:
    """获取所有账户（包括本地和服务器代跑）的聚合签到数据。

    注意：线上这个接口只认 query 里的 deviceFingerprint，JSON body 会被忽略
    （2026-09 实测：只发 body 会稳定返回 code=400「缺少 deviceFingerprint」，
    导致服务器端跑批记录一直合并不进今日看板），所以指纹要同时放到 URL 上。
    """
    resp = _post("/checkin-data/aggregated", {}, params={"deviceFingerprint": device_fingerprint()})
    if resp.get("ok") and isinstance(resp.get("data"), list):
        return resp["data"]
    return []



def run_now_server() -> dict[str, Any]:
    return _post("/runs/run-now", {})


def fetch_notices() -> dict[str, Any]:
    """拉取未读站内通知（账号异常 + 运营公告），供客户端弹窗提醒。"""
    return _post("/notices", {})


def ack_notices(keys: list[str] | None = None, *, all_read: bool = False) -> dict[str, Any]:
    """把通知标记为已读（之后不再弹窗）。"""
    return _post("/notices/ack", {"noticeKeys": [str(k) for k in (keys or [])], "all": bool(all_read)})
