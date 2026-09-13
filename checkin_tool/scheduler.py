# -*- coding: utf-8 -*-
"""本机签到执行与早晚双窗口调度。"""

from __future__ import annotations

import random
import threading
import time
from datetime import datetime
from typing import Any, Callable

from . import account_store, server_client
from .adapters import traework, workbuddy, workbuddy_tasks
from .license_client import ensure_licensed
from .settings import load_settings

# 代跑凭证保鲜间隔：服务端不会自己续期，本机定期把刷新后的 token 回传
SERVER_SYNC_INTERVAL_SEC = 6 * 3600

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
        new_blob, renew_note = traework.prepare_checkin_blob(blob, settings=settings)
        if renew_note:
            log(f"[traework] {renew_note}") if log else None
        if renew_note and new_blob is not blob:
            blob = new_blob
            account["token_blob"] = new_blob
            accounts = account_store.load_accounts()
            for row in accounts:
                if str(row.get("id")) == str(account.get("id")):
                    row["token_blob"] = new_blob
                    cur = (row.get("last_error") or "").strip()
                    row["last_error"] = (cur + " | " + renew_note).strip(" |") if cur else renew_note
                    break
            account_store.save_accounts(accounts)
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


def run_workbuddy_tasks(
    account: dict[str, Any],
    *,
    log: LogFn | None = None,
    settings: dict[str, Any] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """跑一个 WorkBuddy 账号的成长中心任务（幂等：已完成/已领奖自动跳过）。

    只处理 provider=workbuddy 的账号；默认同一天只跑一次（早晚两个窗口只会执行一次），
    force=True 可用于手动「立即执行」。
    """
    settings = settings or load_settings()
    provider = str(account.get("provider") or "")
    label = account.get("label") or account.get("id")
    if provider != "workbuddy":
        return {"ok": False, "message": f"{label} 不是 WorkBuddy 账号，无成长任务"}
    blob = account.get("token_blob") if isinstance(account.get("token_blob"), dict) else {}
    today = datetime.now().strftime("%Y-%m-%d")
    if not force and str(account.get("last_task_day") or "") == today:
        return {"ok": True, "message": f"{label} 今天已跑过成长任务", "skipped": True}

    _log(log, f"[成长任务] {label} 开始 ...")
    result = workbuddy_tasks.run_daily_tasks(
        blob, chat_tasks=bool(settings.get("workbuddy_chat_tasks", True))
    )
    for line in result.get("lines") or []:
        _log(log, line)

    accounts = account_store.load_accounts()
    for row in accounts:
        if str(row.get("id")) != str(account.get("id")):
            continue
        row["last_task_day"] = today
        row["last_task_at"] = datetime.now().isoformat(timespec="seconds")
        if result.get("ok"):
            row["last_task_done"] = result.get("done")
            row["last_task_total"] = result.get("total")
            row["last_task_rest"] = result.get("rest") or []
            row["last_error"] = ""
        else:
            row["last_error"] = result.get("message") or "成长任务失败"
        break
    account_store.save_accounts(accounts)
    account_store.append_run_log(
        {
            "account_id": account.get("id"),
            "provider": provider,
            "label": label,
            "ok": bool(result.get("ok")),
            "credits": None,
            "streak": None,
            "message": result.get("message") or "成长任务",
            "mode": "local",
            "task": True,
        }
    )
    return result


def run_workbuddy_tasks_all(
    *, log: LogFn | None = None, force: bool = False
) -> list[dict[str, Any]]:
    """对所有本机模式的 WorkBuddy 账号跑成长任务。"""
    settings = load_settings()
    results: list[dict[str, Any]] = []
    for account in account_store.load_accounts():
        if not account.get("enabled", True):
            continue
        if str(account.get("run_mode") or "local") != "local":
            continue
        if str(account.get("provider") or "") != "workbuddy":
            continue
        results.append(run_workbuddy_tasks(account, log=log, settings=settings, force=force))
        time.sleep(0.5)
    return results


def refresh_server_credentials(*, log: LogFn | None = None) -> list[dict[str, Any]]:
    """代跑凭证保鲜：本机把 token 续期成功后自动回传服务器。

    服务端 worker 只会用上传时的 access_token 打接口，不续期；
    因此本机定期（或每次登录重新采集后）把最新 tokenBlob 推上去，
    代跑才不会因为 token 到期而连续失败。返回每个代跑账号的处理结果。
    """
    settings = load_settings()
    results: list[dict[str, Any]] = []
    for account in account_store.load_accounts():
        if str(account.get("run_mode") or "local") != "server":
            continue
        if not account.get("enabled", True):
            continue
        provider = str(account.get("provider") or "")
        label = account.get("label") or account.get("id")
        blob = account.get("token_blob") if isinstance(account.get("token_blob"), dict) else {}
        changed = False
        if provider == "traework" and blob.get("refresh_token"):
            new_blob, note = traework.prepare_checkin_blob(blob, settings=settings)
            old_token = str(blob.get("token") or blob.get("access_token") or "")
            new_token = str(new_blob.get("token") or new_blob.get("access_token") or "")
            if new_token and new_token != old_token:
                account["token_blob"] = new_blob
                changed = True
                _log(log, f"[代跑凭证] {label} 已刷新 token，将同步服务器")
            elif note and "失败" in note:
                _log(log, f"[代跑凭证] {label} 续期失败：{note}")
        result = server_client.sync_server_blob(account, log=log)
        results.append(
            {
                "account_id": account.get("id"),
                "label": label,
                "provider": provider,
                "token_refreshed": changed,
                "synced": bool(result and result.get("ok")),
                "message": (result or {}).get("message") or ("已是最新" if result is None else ""),
            }
        )
    return results


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
        # WorkBuddy 成长中心任务（在本机签到之后顺带做，幂等）
        if settings.get("workbuddy_task_mode") == "local" and str(account.get("provider")) == "workbuddy":
            try:
                run_workbuddy_tasks(account, log=log, settings=settings)
            except Exception as exc:  # noqa: BLE001
                _log(log, f"[成长任务] 异常: {exc}")
    return results


class DailyScheduler:
    def __init__(self, log: LogFn | None = None) -> None:
        self._log = log
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fired: set[str] = set()
        self._last_server_sync = 0.0

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
            # 代跑凭证保鲜：启动先跑一次，之后每 6 小时一次
            if time.time() - self._last_server_sync >= SERVER_SYNC_INTERVAL_SEC:
                self._last_server_sync = time.time()
                try:
                    refresh_server_credentials(log=self._log)
                except Exception as exc:  # noqa: BLE001
                    _log(self._log, f"[代跑凭证] 同步异常: {exc}")
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
