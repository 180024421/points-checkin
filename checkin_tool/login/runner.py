# -*- coding: utf-8 -*-
"""统一登录编排：读账密 → 登录 → 写入账号库。"""

from __future__ import annotations

from typing import Any, Callable

from .. import account_store, credential_store
from ..redact import mask_text
from . import traework as tw_login
from . import workbuddy as wb_login
from .types import LoginResult

LogFn = Callable[[str], None]


def login_credential(cred: dict[str, Any], *, headed: bool = True, log: LogFn | None = None) -> LoginResult:
    provider = str(cred.get("provider") or "").strip().lower()
    username = str(cred.get("username") or "").strip()
    password = str(cred.get("password") or "")
    log = log or (lambda _m: None)
    log(f"开始统一登录 {provider} / {username} …")

    if provider == "workbuddy":
        result = wb_login.login(username, password, headed=headed)
    elif provider == "traework":
        result = tw_login.login(username, password, headed=headed)
    else:
        result = LoginResult(ok=False, provider=provider or "unknown", message="未知 provider")

    # 登录失败消息可能带回调 URL / 响应片段，落盘前先兜底脱敏
    if not result.ok:
        result.message = mask_text(result.message, 200)

    linked_id = None
    blocked_note = ""  # 登录成功但额度拦住没入库时，把原因带进账密记录
    if result.ok and result.token_blob:
        blob = dict(result.token_blob)
        label = (
            blob.get("nickname")
            or blob.get("uid")
            or blob.get("user_id")
            or cred.get("label")
            or username
        )
        identity = blob.get("uid") or blob.get("user_id") or username
        stored = account_store.try_upsert_account(
            {
                "provider": provider,
                "label": label,
                "identity": identity,
                "run_mode": "local",
                "enabled": True,
                "token_blob": blob,
                "last_error": "",
                "credential_id": cred.get("id"),
            }
        )
        if stored.get("ok"):
            linked_id = str((stored.get("account") or {}).get("id"))
            log(f"登录成功 [{result.method}] → 账号入库 {label}")
        else:
            # 登录本身是成功的（token 真拿到了），只是这个号挂不进额度。
            # 绝不能让 QuotaBlocked 从这里冒出去：它跑在后台登录线程里，
            # 一冒出来整批登录中途 abort，前面已入库的账号就成了没人管的脏数据。
            blocked_note = str(stored.get("message") or "账号未入库")
            log(f"登录成功 [{result.method}]，但账号未入库：{blocked_note}")
    else:
        log(f"登录失败：{result.message}")

    if cred.get("id"):
        credential_store.mark_login_result(
            str(cred["id"]),
            ok=result.ok,
            method=result.method,
            error=blocked_note if result.ok else result.message,
            linked_account_id=linked_id,
        )
    return result


def login_by_id(credential_id: str, *, headed: bool = True, log: LogFn | None = None) -> LoginResult:
    for row in credential_store.load_credentials():
        if str(row.get("id")) == str(credential_id):
            return login_credential(row, headed=headed, log=log)
    return LoginResult(ok=False, provider="", message="找不到账密记录")


def login_all(*, provider: str | None = None, headed: bool = True, log: LogFn | None = None) -> list[LoginResult]:
    results: list[LoginResult] = []
    for row in credential_store.load_credentials():
        if provider and str(row.get("provider")) != provider:
            continue
        results.append(login_credential(row, headed=headed, log=log))
    return results
