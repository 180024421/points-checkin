# -*- coding: utf-8 -*-
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from .license_client import data_root
from .secure_storage import load_json, save_json
from .license_client import check_status # Import check_status
from .license_client import device_fingerprint # Import device_fingerprint
from .run_jane_api import report_license_usage # Import report_license_usage
from . import server_client # Import server_client

ACCOUNTS_FILE = data_root() / "accounts.json"
RUN_LOG_FILE = data_root() / "run_log.json"
LIVE_LOG_FILE = data_root() / "live_log.json"
CREDIT_HISTORY_FILE = data_root() / "credit_history.json"

# Constants for log and history limits
MAX_RUN_LOGS = 1000
MAX_LIVE_LOGS = 800
MAX_CREDIT_HISTORY_ITEMS = 2000

DEFAULT_RUN_LOGS_LIMIT = 200
DEFAULT_LIVE_LOGS_LIMIT = 300
DEFAULT_CREDIT_HISTORY_LIMIT = 200

# UI 线程与后台线程（自动采集 / 调度）都会读改写这些文件，
# 锁保证 read-modify-write 不丢数据（例如两个账号同时入库）。
_IO_LOCK = threading.RLock()


def _enabled_count(rows: list[dict[str, Any]]) -> int:
    return sum(1 for r in rows if r.get("enabled", True))

def _report_enabled_accounts_to_run_jane(accounts: list[dict[str, Any]]) -> None:
    enabled_now = _enabled_count(accounts)
    fp = device_fingerprint()
    if not fp:
        print("Warning: device fingerprint not available, cannot report usage.")
        return
    try:
        report_license_usage(fp, enabled_now)
        print(f"Reported {enabled_now} enabled accounts to run-jane.")
    except Exception as e:
        print(f"Error reporting enabled accounts to run-jane: {e}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def get_account_limit() -> int | None:
    """可代挂账号数上限（按账号数计费）。

    直接读授权缓存（由 license_client.check_status 写入，离线可用）；
    不再用空 settings 发起请求（旧实现传 {}，会走默认服务地址导致判断失真）。
    None = 不限（未取到额度时不阻断用户）。
    """
    try:
        from .license_client import load_cache

        limit = load_cache().get("accountLimit")
        if limit is None:
            return None
        try:
            limit = int(limit)
        except ValueError as e:
            # Log the error for debugging, but still return None as per original logic
            print(f"Error converting accountLimit to int: {limit}, {e}")
            return None
        return limit if limit > 0 else None
    except Exception as e:
        # Log unexpected errors
        print(f"Unexpected error in get_account_limit: {e}")
        return None


def get_account_usage() -> dict[str, Any]:
    """供界面展示：{limit, used, remain, planLabel, expireAt}。"""
    limit = get_account_limit()
    used = 0
    plan_label = ""
    expire_at = None
    try:
        from .license_client import load_cache

        cache = load_cache()
        plan_label = str(cache.get("accountPlanLabel") or cache.get("planLabel") or "")
        expire_at = cache.get("expireAt")
        raw_used = cache.get("accountUsed")
        if raw_used is not None:
            try:
                used = int(raw_used)
            except ValueError as e:
                print(f"Error converting accountUsed to int: {raw_used}, {e}")
                # Keep used as 0 as per original logic if conversion fails
    except Exception as e:
        print(f"Unexpected error loading license cache in get_account_usage: {e}")
    
    if used <= 0:
        try:
            used = sum(1 for a in load_accounts() if a.get("enabled", True))
        except Exception as e:
            print(f"Error calculating account usage from loaded accounts: {e}")
            used = 0
    return {
        "limit": limit,
        "used": used,
        "remain": None if limit is None else max(limit - used, 0),
        "planLabel": plan_label,
        "expireAt": expire_at,
    }



def _today_local() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def load_accounts() -> list[dict[str, Any]]:
    with _IO_LOCK:
        local_accounts_data = load_json(ACCOUNTS_FILE, {"accounts": []})
        local_accounts = local_accounts_data.get("accounts", [])

        server_accounts = server_client.list_server_accounts() # 从服务端获取代跑账户

        # 合并账户，以服务端账户为准
        all_accounts_map: dict[str, dict[str, Any]] = {}
        for acc in local_accounts:
            all_accounts_map[str(acc.get("id"))] = acc
        for acc in server_accounts:
            all_accounts_map[str(acc.get("id"))] = acc
        
        return list(all_accounts_map.values())



def save_accounts(accounts: list[dict[str, Any]]) -> None:
    save_json(ACCOUNTS_FILE, {"accounts": accounts, "updatedAt": _now()})


def account_identity_key(account: dict[str, Any]) -> str:
    provider = str(account.get("provider") or "").strip()
    blob = account.get("token_blob") if isinstance(account.get("token_blob"), dict) else {}
    uid = str(
        account.get("identity")
        or blob.get("uid")
        or blob.get("user_id")
        or blob.get("nickname")
        or account.get("label")
        or account.get("id")
        or ""
    ).strip()
    return f"{provider}:{uid}"


def upsert_account(account: dict[str, Any]) -> dict[str, Any]:
    with _IO_LOCK:
        return _upsert_account(account)


def _upsert_account(account: dict[str, Any]) -> dict[str, Any]:
    accounts = load_accounts()
    identity = account_identity_key(account)
    account["identity"] = identity.split(":", 1)[-1] if ":" in identity else identity
    account["updatedAt"] = _now()
    if "run_mode" not in account:
        account["run_mode"] = "local"
    if "createdAt" not in account:
        account["createdAt"] = _now()

    # prefer match by explicit id, else by provider+identity (多号采集不互相覆盖错误对象)
    account_id = str(account.get("id") or "").strip()
    idx = -1
    if account_id:
        for i, row in enumerate(accounts):
            if str(row.get("id")) == account_id:
                idx = i
                break
    if idx < 0:
        for i, row in enumerate(accounts):
            if account_identity_key(row) == identity and identity.endswith(":") is False and identity:
                # empty identity after provider: skip
                if identity.split(":", 1)[-1]:
                    idx = i
                    account_id = str(row.get("id"))
                    break
    if not account_id:
        account_id = str(uuid.uuid4())
    account["id"] = account_id

    def _enabled_count(rows: list[dict[str, Any]]) -> int:
        return sum(1 for r in rows if r.get("enabled", True))

    account_limit = get_account_limit()
    if idx >= 0:
        was_enabled = bool(accounts[idx].get("enabled", True))
        merged = {**accounts[idx], **account}
        accounts[idx] = merged
        account = merged
        # 从禁用切回启用也要占用额度（额度按「启用中的账号数」计）
        if not was_enabled and bool(merged.get("enabled", True)) and account_limit is not None:
            enabled_now = _enabled_count(accounts)
            if enabled_now > account_limit:
                raise ValueError(
                    f"超出账号数量上限：启用后将有 {enabled_now} 个，"
                    f"当前套餐仅支持 {account_limit} 个（升级套餐可增加）"
                )
    else:
        if account_limit is not None:
            enabled_now = _enabled_count(accounts)
            if enabled_now >= account_limit:
                raise ValueError(
                    f"超出账号数量上限：当前已启用 {enabled_now} 个，"
                    f"套餐上限 {account_limit} 个（升级套餐或先停用部分账号）"
                )
        accounts.append(account)
    
    # Ensure tags is a list
    if "tags" not in account or not isinstance(account["tags"], list):
        account["tags"] = []

    # Handle user_tag from token_blob if present
    token_blob = account.get("token_blob")
    if isinstance(token_blob, dict):
        user_tag_from_blob = token_blob.get("user_tag")
        if user_tag_from_blob and isinstance(user_tag_from_blob, str) and user_tag_from_blob not in account["tags"]:
            account["tags"].append(user_tag_from_blob)
    
    save_accounts(accounts)
    _report_enabled_accounts_to_run_jane(accounts) # Report changes to run-jane
    return account


def delete_account(account_id: str) -> bool:
    with _IO_LOCK:
        accounts = load_accounts()
        new_rows = [a for a in accounts if str(a.get("id")) != account_id]
        if len(new_rows) == len(accounts):
            return False
        save_accounts(new_rows)
        _report_enabled_accounts_to_run_jane(new_rows) # Report changes to run-jane
        return True


def public_account_view(account: dict[str, Any], today_map: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    blob = account.get("token_blob") if isinstance(account.get("token_blob"), dict) else {}
    today = (today_map or {}).get(str(account.get("id")), {})
    expires_at = blob.get("expires_at") or blob.get("expiresAt")
    expired = False
    if isinstance(expires_at, (int, float)) and expires_at > 0:
        expired = expires_at < (datetime.now().timestamp() * 1000 + 5 * 60 * 1000)
    return {
        "id": account.get("id"),
        "provider": account.get("provider"),
        "label": account.get("label") or blob.get("nickname") or blob.get("user_id") or blob.get("uid") or account.get("id"),
        "run_mode": account.get("run_mode") or "local",
        "enabled": bool(account.get("enabled", True)),
        "last_ok_at": account.get("last_ok_at"),
        "last_error": account.get("last_error"),
        "last_credits": account.get("last_credits"),
        "last_streak": account.get("last_streak"),
        "token_hint": blob.get("token_hint") or "(已保存)",
        "token_expired": expired,
        "today_status": today.get("status") or "未跑",
        "today_credits": today.get("credits"),
        "today_message": today.get("message") or "",
        "updatedAt": account.get("updatedAt"),
    }


def append_run_log(entry: dict[str, Any]) -> None:
    with _IO_LOCK:
        data = load_json(RUN_LOG_FILE, {"runs": []})
        runs = data.get("runs") if isinstance(data, dict) else []
        if not isinstance(runs, list):
            runs = []
        entry = {
            **entry,
            "at": entry.get("at") or _now(),
            "day": entry.get("day") or _today_local(),
        }
        runs.insert(0, entry)
        runs = runs[:MAX_RUN_LOGS]
        save_json(RUN_LOG_FILE, {"runs": runs})


def load_run_logs(account_id: str | None = None, limit: int = DEFAULT_RUN_LOGS_LIMIT) -> list[dict[str, Any]]:
    with _IO_LOCK:
        data = load_json(RUN_LOG_FILE, {"runs": []})
        runs = data.get("runs", [])
        if account_id:
            runs = [x for x in runs if str(x.get("account_id")) == str(account_id)]
        return runs[:limit]


def clear_run_logs() -> None:
    save_json(RUN_LOG_FILE, {"runs": [], "clearedAt": _now()})


def append_live_log(message: str) -> None:
    with _IO_LOCK:
        data = load_json(LIVE_LOG_FILE, {"lines": []})
        lines = data.get("lines") if isinstance(data, dict) else []
        if not isinstance(lines, list):
            lines = []
        lines.insert(0, {"at": datetime.now().strftime("%H:%M:%S"), "message": message})
        lines = lines[:MAX_LIVE_LOGS]
        save_json(LIVE_LOG_FILE, {"lines": lines})


def load_live_logs(limit: int = DEFAULT_LIVE_LOGS_LIMIT) -> list[dict[str, Any]]:
    with _IO_LOCK:
        data = load_json(LIVE_LOG_FILE, {"lines": []})
        lines = data.get("lines", [])
        return lines[:limit]


def clear_live_logs() -> None:
    save_json(LIVE_LOG_FILE, {"lines": [], "clearedAt": _now()})


def append_credit_history(entry: dict[str, Any]) -> None:
    with _IO_LOCK:
        data = load_json(CREDIT_HISTORY_FILE, {"items": []})
        items = data.get("items") if isinstance(data, dict) else []
        if not isinstance(items, list):
            items = []
        items.insert(0, {**entry, "at": entry.get("at") or _now(), "day": entry.get("day") or _today_local()})
        items = items[:MAX_CREDIT_HISTORY_ITEMS]
        save_json(CREDIT_HISTORY_FILE, {"items": items})


def load_credit_history(account_id: str | None = None, limit: int = DEFAULT_CREDIT_HISTORY_LIMIT) -> list[dict[str, Any]]:
    with _IO_LOCK:
        data = load_json(CREDIT_HISTORY_FILE, {"items": []})
        items = data.get("items", [])
        if account_id:
            items = [x for x in items if str(x.get("account_id")) == str(account_id)]
        return items[:limit]


def clear_credit_history() -> None:
    save_json(CREDIT_HISTORY_FILE, {"items": [], "clearedAt": _now()})


def today_run_map() -> dict[str, dict[str, Any]]:
    """account_id -> {status, credits, message, ok} for local day."""
    day = _today_local()
    out: dict[str, dict[str, Any]] = {}

    # 加载本地运行日志
    for row in load_run_logs(limit=MAX_RUN_LOGS):
        row_day = str(row.get("day") or "")
        if not row_day:
            at = str(row.get("at") or "")
            row_day = at[:10] if len(at) >= 10 else ""
        if row_day != day:
            continue
        aid = str(row.get("account_id") or "")
        if not aid or aid in out:
            continue
        out[aid] = _process_run_log_entry(row)
    
    # 加载服务器端运行日志并合并，服务器端数据优先
    server_runs = server_client.fetch_aggregated_checkin_data()
    for row in server_runs:
        row_day = str(row.get("day") or "")
        if not row_day:
            at = str(row.get("at") or "")
            row_day = at[:10] if len(at) >= 10 else ""
        if row_day != day:
            continue
        aid = str(row.get("account_id") or "")
        if not aid:
            continue
        out[aid] = _process_run_log_entry(row) # 服务器数据覆盖本地数据

    return out

def _process_run_log_entry(row: dict[str, Any]) -> dict[str, Any]:
    ok = bool(row.get("ok"))
    already = bool(row.get("already"))
    if ok and already:
        status = "已签(之前)"
    elif ok:
        status = "已跑成功"
    else:
        status = "失败"
    return {
        "status": status,
        "credits": row.get("credits"),
        "message": row.get("message") or "",
        "ok": ok,
        "already": already,
        "at": row.get("at"),
        "run_mode": row.get("run_mode") or "local", # 记录运行模式
    }


def today_board() -> dict[str, Any]:
    accounts = load_accounts()
    today = today_run_map()
    done: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for acc in accounts:
        if not acc.get("enabled", True):
            continue
        view = public_account_view(acc, today)
        st = view.get("today_status") or "未跑"
        if st.startswith("已"):
            done.append(view)
        elif st == "失败":
            failed.append(view)
        else:
            pending.append(view)
    return {
        "day": _today_local(),
        "done": done,
        "pending": pending,
        "failed": failed,
        "done_count": len(done),
        "pending_count": len(pending),
        "failed_count": len(failed),
        "total_enabled": len(done) + len(pending) + len(failed),
    }
