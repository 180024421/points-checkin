# -*- coding: utf-8 -*-
"""run-jane 代跑 API 客户端（卡密 + 设备指纹）。

另外负责「代跑凭证保鲜」：服务端 worker 只用上传时的 access_token 打接口，
不会自己续期；所以本机一旦把 token 刷新成功，就要把最新 tokenBlob 回传，
否则到期后服务端会一直签到失败。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import account_store
from .license_client import auth_headers, license_cfg_from_settings, load_cache
from .settings import load_settings

LogFn = Callable[[str], None]


def _post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    settings = load_settings()
    cfg = license_cfg_from_settings(settings)
    base = cfg["base_url"]
    if not base:
        return {"ok": False, "message": "未配置授权服务地址"}
    url = f"{base}/api/points-checkin{path}"
    payload = {**auth_headers(settings), **body}
    # ensure ticket present
    cache = load_cache()
    if cache.get("ticket") and not payload.get("ticket"):
        payload["ticket"] = cache["ticket"]

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
        text = exc.read().decode("utf-8", errors="ignore")
        server_message = ""
        try:
            data = json.loads(text)
            # Prioritize server's message from the JSON response
            if isinstance(data, dict) and (data.get("message") or data.get("msg")):
                server_message = data.get("message") or data.get("msg")
        except Exception:
            pass # Failed to parse JSON error, fall back to generic message

        # If server_message is available, use it. Otherwise, provide a user-friendly HTTP error.
        user_message = server_message or f"服务器响应错误（HTTP {exc.code}），请稍后再试。"
        return {"ok": False, "message": user_message}
    except URLError as exc:
        return {"ok": False, "message": f"无法连接到服务器，请检查网络连接: {exc.reason}"}
    except Exception as exc:
        return {"ok": False, "message": f"服务器通信异常，请联系技术支持。详情: {str(exc)}"}

    if not isinstance(data, dict):
        return {"ok": False, "message": "响应无效"}
    code = data.get("code")
    ok = code in (200, 0, "200", "0", None) and data.get("ok") is not False
    message = data.get("message") or data.get("msg") or ""
    result = data.get("data") if isinstance(data.get("data"), dict) else data.get("data")
    return {"ok": bool(ok), "message": message, "data": result, "raw": data}


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
            account_store.upsert_account(account)
        if log:
            log(f"代跑凭证已同步服务器：{label}")
    elif log:
        log(f"代跑凭证同步失败（{label}）：{result.get('message')}")
    return result


def list_server_accounts() -> list[dict[str, Any]]:
    resp = _post("/accounts/list", {})
    if resp.get("ok") and isinstance(resp.get("data"), list):
        server_accounts = resp["data"]
        for account in server_accounts:
            account["run_mode"] = "server"  # 标记为服务器代跑账户
        return server_accounts
    return []


def set_enabled(server_account_id: int | str, enabled: bool) -> dict[str, Any]:
    return _post("/accounts/set-enabled", {"id": server_account_id, "enabled": enabled})


def delete_server_account(server_account_id: int | str) -> dict[str, Any]:
    return _post("/accounts/delete", {"id": server_account_id})


def today_runs() -> dict[str, Any]:
    return _post("/runs/today", {})

def fetch_aggregated_checkin_data() -> list[dict[str, Any]]:
    """获取所有账户（包括本地和服务器代跑）的聚合签到数据"""
    resp = _post("/checkin-data/aggregated", {})
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
