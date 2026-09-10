# -*- coding: utf-8 -*-
"""run-jane 代跑 API 客户端（卡密 + 设备指纹）。"""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .license_client import auth_headers, license_cfg_from_settings, load_cache
from .settings import load_settings


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
        try:
            data = json.loads(text)
        except Exception:
            return {"ok": False, "message": text[:200] or f"HTTP {exc.code}"}
    except URLError as exc:
        return {"ok": False, "message": f"网络失败: {exc}"}
    except Exception as exc:
        return {"ok": False, "message": str(exc)}

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
        },
    )


def list_server_accounts() -> dict[str, Any]:
    return _post("/accounts/list", {})


def set_enabled(server_account_id: int | str, enabled: bool) -> dict[str, Any]:
    return _post("/accounts/set-enabled", {"id": server_account_id, "enabled": enabled})


def delete_server_account(server_account_id: int | str) -> dict[str, Any]:
    return _post("/accounts/delete", {"id": server_account_id})


def today_runs() -> dict[str, Any]:
    return _post("/runs/today", {})


def run_now_server() -> dict[str, Any]:
    return _post("/runs/run-now", {})
