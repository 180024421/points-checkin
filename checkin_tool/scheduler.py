# -*- coding: utf-8 -*-
"""本机签到执行与早晚双窗口调度。"""

from __future__ import annotations

import random
import threading
import time
from datetime import datetime
from typing import Any, Callable

from . import account_store
from .adapters import traework, workbuddy
from .license_client import ensure_licensed
from .settings import load_settings

LogFn = Callable[[str], None]


def _log(fn: LogFn | None, msg: str) -> None:
    account_store.append_live_log(msg)
    if fn:
        fn(msg)


def _update_account_after_run(account_id: str, result: Any) -> None:
    accounts = account_store.load_accounts()
    for row in accounts:
        if str(row.get("id")) != str(account_id):
            continue
        if result.ok:
            row["last_ok_at"] = datetime.now().isoformat(timespec="seconds")
            row["last_error"] = ""
            if result.credits is not None:
                row["last_credits"] = result.credits
            if result.streak is not None:
                row["last_streak"] = result.streak
        else:
            row["last_error"] = result.message
        break
    account_store.save_accounts(accounts)
    account_store.append_credit_history(
        {
            "account_id": account_id,
            "provider": getattr(result, "provider", ""),
            "ok": bool(result.ok),
            "already": bool(result.already),
            "credits": result.credits,
            "streak": result.streak,
            "message": result.message,
        }
    )


def run_one_account(account: dict[str, Any], *, log: LogFn | None = None) -> dict[str, Any]:
    provider = str(account.get("provider") or "").strip()
    blob = account.get("token_blob") if isinstance(account.get("token_blob"), dict) else {}
    label = account.get("label") or account.get("id")
    if provider == "workbuddy":
        result = workbuddy.checkin_from_blob(blob)
    elif provider == "traework":
        settings = load_settings()
        if settings.get("traework_ug_api_base") and not blob.get("ug_api_base"):
            blob = {**blob, "ug_api_base": settings["traework_ug_api_base"]}
        result = traework.checkin_from_blob(blob)
    else:
        return {"ok": False, "message": f"未知 provider: {provider}"}

    payload = result.to_dict()
    account_store.append_run_log(
        {
            "account_id": account.get("id"),
            "provider": provider,
            "label": label,
            "ok": result.ok,
            "already": result.already,
            "credits": result.credits,
            "streak": result.streak,
            "message": result.message,
            "mode": "local",
        }
    )
    _update_account_after_run(str(account.get("id")), result)
    return payload


def refresh_account_credits(account: dict[str, Any]) -> dict[str, Any]:
    provider = str(account.get("provider") or "").strip()
    blob = account.get("token_blob") if isinstance(account.get("token_blob"), dict) else {}
    if provider == "workbuddy":
        info = workbuddy.query_from_blob(blob)
    elif provider == "traework":
        info = traework.query_from_blob(blob)
    else:
        return {"ok": False, "message": "未知 provider"}
    if info.get("ok"):
        accounts = account_store.load_accounts()
        for row in accounts:
            if str(row.get("id")) == str(account.get("id")):
                credits = info.get("today_credit") if provider == "workbuddy" else info.get("credits")
                streak = info.get("streak")
                if credits is not None:
                    row["last_credits"] = credits
                if streak is not None:
                    row["last_streak"] = streak
                if info.get("today_checked_in"):
                    row["last_ok_at"] = row.get("last_ok_at") or datetime.now().isoformat(timespec="seconds")
                break
        account_store.save_accounts(accounts)
        account_store.append_credit_history(
            {
                "account_id": account.get("id"),
                "provider": provider,
                "ok": True,
                "credits": info.get("today_credit") if provider == "workbuddy" else info.get("credits"),
                "streak": info.get("streak"),
                "message": "查询积分状态",
                "query_only": True,
            }
        )
    return info


def run_local_all(*, require_license: bool = True, log: LogFn | None = None) -> list[dict[str, Any]]:
    settings = load_settings()
    if require_license:
        ok, msg = ensure_licensed(settings, force_online=True)
        if not ok:
            _log(log, f"卡密无效：{msg}")
            return [{"ok": False, "message": msg}]

    results: list[dict[str, Any]] = []
    for account in account_store.load_accounts():
        if not account.get("enabled", True):
            continue
        if str(account.get("run_mode") or "local") != "local":
            continue
        _log(log, f"签到 {account.get('provider')} / {account.get('label') or account.get('id')} ...")
        result = run_one_account(account, log=log)
        results.append(result)
        _log(log, result.get("message") or str(result))
        time.sleep(0.8 + random.random())
    return results


class DailyScheduler:
    def __init__(self, log: LogFn | None = None) -> None:
        self._log = log
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fired: set[str] = set()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="checkin-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _slot_key(self, day: str, slot: str) -> str:
        return f"{day}:{slot}"

    def _maybe_run_slot(self, settings: dict[str, Any], now: datetime, day: str, slot: str, hour: int, minute: int) -> None:
        key = self._slot_key(day, slot)
        if key in self._fired:
            return
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        window = int(settings.get("schedule_window_sec") or 7200)
        if now < target or (now - target).total_seconds() > window:
            return
        jitter = int(settings.get("schedule_jitter_sec") or 0)
        if jitter > 0:
            wait = random.randint(0, jitter)
            _log(self._log, f"[{slot}] 抖动等待 {wait}s")
            self._stop.wait(wait)
            if self._stop.is_set():
                return
        _log(self._log, f"[{slot}] 开始本机日签调度")
        run_local_all(require_license=True, log=self._log)
        self._fired.add(key)

    def _loop(self) -> None:
        while not self._stop.is_set():
            settings = load_settings()
            if not settings.get("auto_schedule", True):
                self._stop.wait(30)
                continue
            now = datetime.now()
            day = now.strftime("%Y-%m-%d")
            # morning
            self._maybe_run_slot(
                settings,
                now,
                day,
                "morning",
                int(settings.get("schedule_hour") or 9),
                int(settings.get("schedule_minute") or 10),
            )
            # evening补漏
            if settings.get("evening_schedule", True):
                self._maybe_run_slot(
                    settings,
                    now,
                    day,
                    "evening",
                    int(settings.get("evening_hour") or 20),
                    int(settings.get("evening_minute") or 0),
                )
            self._stop.wait(20)
