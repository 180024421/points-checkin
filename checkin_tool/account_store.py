# -*- coding: utf-8 -*-
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from .license_client import data_root
from .secure_storage import load_json, save_json

ACCOUNTS_FILE = data_root() / "accounts.json"
RUN_LOG_FILE = data_root() / "run_log.json"
LIVE_LOG_FILE = data_root() / "live_log.json"
CREDIT_HISTORY_FILE = data_root() / "credit_history.json"

# UI 线程与后台线程（自动采集 / 调度）都会读改写这些文件，
# 锁保证 read-modify-write 不丢数据（例如两个账号同时入库）。
_IO_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_local() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def load_accounts() -> list[dict[str, Any]]:
    data = load_json(ACCOUNTS_FILE, {"accounts": []})
    accounts = data.get("accounts") if isinstance(data, dict) else []
    return accounts if isinstance(accounts, list) else []


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

    if idx >= 0:
        merged = {**accounts[idx], **account}
        accounts[idx] = merged
        account = merged
    else:
        accounts.append(account)
    save_accounts(accounts)
    return account


def delete_account(account_id: str) -> bool:
    with _IO_LOCK:
        accounts = load_accounts()
        new_rows = [a for a in accounts if str(a.get("id")) != account_id]
        if len(new_rows) == len(accounts):
            return False
        save_accounts(new_rows)
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
        runs = runs[:1000]
        save_json(RUN_LOG_FILE, {"runs": runs})


def load_run_logs(limit: int = 200) -> list[dict[str, Any]]:
    data = load_json(RUN_LOG_FILE, {"runs": []})
    runs = data.get("runs") if isinstance(data, dict) else []
    if not isinstance(runs, list):
        return []
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
        lines = lines[:800]
        save_json(LIVE_LOG_FILE, {"lines": lines})


def load_live_logs(limit: int = 300) -> list[dict[str, Any]]:
    data = load_json(LIVE_LOG_FILE, {"lines": []})
    lines = data.get("lines") if isinstance(data, dict) else []
    if not isinstance(lines, list):
        return []
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
        items = items[:2000]
        save_json(CREDIT_HISTORY_FILE, {"items": items})


def load_credit_history(account_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    data = load_json(CREDIT_HISTORY_FILE, {"items": []})
    items = data.get("items") if isinstance(data, dict) else []
    if not isinstance(items, list):
        return []
    if account_id:
        items = [x for x in items if str(x.get("account_id")) == str(account_id)]
    return items[:limit]


def clear_credit_history() -> None:
    save_json(CREDIT_HISTORY_FILE, {"items": [], "clearedAt": _now()})


def today_run_map() -> dict[str, dict[str, Any]]:
    """account_id -> {status, credits, message, ok} for local day."""
    day = _today_local()
    out: dict[str, dict[str, Any]] = {}
    for row in load_run_logs(limit=1000):
        row_day = str(row.get("day") or "")
        if not row_day:
            at = str(row.get("at") or "")
            row_day = at[:10] if len(at) >= 10 else ""
        if row_day != day:
            continue
        aid = str(row.get("account_id") or "")
        if not aid or aid in out:
            continue
        ok = bool(row.get("ok"))
        already = bool(row.get("already"))
        if ok and already:
            status = "已签(之前)"
        elif ok:
            status = "已跑成功"
        else:
            status = "失败"
        out[aid] = {
            "status": status,
            "credits": row.get("credits"),
            "message": row.get("message") or "",
            "ok": ok,
            "already": already,
            "at": row.get("at"),
        }
    return out


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
