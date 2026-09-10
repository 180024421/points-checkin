# -*- coding: utf-8 -*-
"""账密本地安全存储（DPAPI）。仅存本机，默认不上传服务器。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from .license_client import data_root
from .secure_storage import load_json, save_json

CREDENTIALS_FILE = data_root() / "credentials.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_credentials() -> list[dict[str, Any]]:
    data = load_json(CREDENTIALS_FILE, {"credentials": []})
    rows = data.get("credentials") if isinstance(data, dict) else []
    return rows if isinstance(rows, list) else []


def save_credentials(rows: list[dict[str, Any]]) -> None:
    save_json(CREDENTIALS_FILE, {"credentials": rows, "updatedAt": _now()})


def upsert_credential(
    *,
    provider: str,
    username: str,
    password: str,
    label: str = "",
    credential_id: str | None = None,
) -> dict[str, Any]:
    provider = str(provider or "").strip().lower()
    username = str(username or "").strip()
    password = str(password or "")
    if provider not in ("workbuddy", "traework"):
        raise ValueError("provider 必须是 workbuddy 或 traework")
    if not username or not password:
        raise ValueError("账号和密码不能为空")

    rows = load_credentials()
    idx = -1
    if credential_id:
        for i, row in enumerate(rows):
            if str(row.get("id")) == str(credential_id):
                idx = i
                break
    if idx < 0:
        for i, row in enumerate(rows):
            if str(row.get("provider")) == provider and str(row.get("username")) == username:
                idx = i
                credential_id = str(row.get("id"))
                break
    if not credential_id:
        credential_id = str(uuid.uuid4())

    row = {
        "id": credential_id,
        "provider": provider,
        "username": username,
        "password": password,
        "label": label or username,
        "updatedAt": _now(),
        "createdAt": rows[idx].get("createdAt") if idx >= 0 else _now(),
        "last_login_at": rows[idx].get("last_login_at") if idx >= 0 else None,
        "last_login_ok": rows[idx].get("last_login_ok") if idx >= 0 else None,
        "last_login_method": rows[idx].get("last_login_method") if idx >= 0 else None,
        "last_login_error": rows[idx].get("last_login_error") if idx >= 0 else "",
        "linked_account_id": rows[idx].get("linked_account_id") if idx >= 0 else None,
    }
    if idx >= 0:
        rows[idx] = {**rows[idx], **row}
        row = rows[idx]
    else:
        rows.append(row)
    save_credentials(rows)
    return row


def delete_credential(credential_id: str) -> bool:
    rows = load_credentials()
    new_rows = [r for r in rows if str(r.get("id")) != str(credential_id)]
    if len(new_rows) == len(rows):
        return False
    save_credentials(new_rows)
    return True


def mark_login_result(
    credential_id: str,
    *,
    ok: bool,
    method: str,
    error: str = "",
    linked_account_id: str | None = None,
) -> None:
    rows = load_credentials()
    for i, row in enumerate(rows):
        if str(row.get("id")) != str(credential_id):
            continue
        row = {
            **row,
            "last_login_at": _now(),
            "last_login_ok": bool(ok),
            "last_login_method": method,
            "last_login_error": error or "",
            "updatedAt": _now(),
        }
        if linked_account_id:
            row["linked_account_id"] = linked_account_id
        rows[i] = row
        save_credentials(rows)
        return


def public_credential_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "provider": row.get("provider"),
        "label": row.get("label") or row.get("username"),
        "username": row.get("username"),
        "has_password": bool(row.get("password")),
        "last_login_at": row.get("last_login_at"),
        "last_login_ok": row.get("last_login_ok"),
        "last_login_method": row.get("last_login_method"),
        "last_login_error": row.get("last_login_error") or "",
        "linked_account_id": row.get("linked_account_id"),
        "updatedAt": row.get("updatedAt"),
    }
