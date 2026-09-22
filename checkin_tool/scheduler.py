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
from .redact import mask_text
from .settings import load_settings, parse_int_field

# 代跑凭证保鲜间隔：服务端不会自己续期，本机定期把刷新后的 token 回传
SERVER_SYNC_INTERVAL_SEC = 6 * 3600

LogFn = Callable[[str], None]


def _log(fn: LogFn | None, msg: str) -> None:
    """一条日志只落一次盘。

    两个界面的日志出口（``gui.append_log`` / ``CheckinApi._append_log``）自己就写
    实时日志，这里再写一遍会把每条调度日志存成双份，实时日志的有效容量直接减半。
    没有 sink 时才由这里兜底落盘。
    """
    if fn:
        fn(msg)
    else:
        account_store.append_live_log(msg)



# 积分查询/状态同步的全局串行槽：自动同步、手动「立即同步」、「刷新积分」三条路
# 都会逐个账号去问供应商，同名任务之外的互斥只能靠这把锁，否则同一个 token
# 会被并发拿去查两次（限流、风控，还会互相覆盖回写）。
_CREDIT_SLOT_LOCK = threading.Lock()
_CREDIT_SLOT_BUSY = False


def try_acquire_credit_slot() -> bool:
    global _CREDIT_SLOT_BUSY
    with _CREDIT_SLOT_LOCK:
        if _CREDIT_SLOT_BUSY:
            return False
        _CREDIT_SLOT_BUSY = True
        return True


def release_credit_slot() -> None:
    global _CREDIT_SLOT_BUSY
    with _CREDIT_SLOT_LOCK:
        _CREDIT_SLOT_BUSY = False


def credit_slot_busy() -> bool:
    with _CREDIT_SLOT_LOCK:
        return _CREDIT_SLOT_BUSY


def _update_account_after_run(account_id: str, result: Any) -> None:
    def patch(row: dict[str, Any]) -> dict[str, Any]:
        if result.ok:
            out: dict[str, Any] = {"last_ok_at": datetime.now().isoformat(timespec="seconds"), "last_error": ""}
            if result.credits is not None:
                out["last_credits"] = result.credits
            if result.streak is not None:
                out["last_streak"] = result.streak
            return out
        return {"last_error": result.message}

    account_store.update_account(account_id, patch)
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

            def patch(row: dict[str, Any]) -> dict[str, Any]:
                cur = (row.get("last_error") or "").strip()
                return {"token_blob": new_blob, "last_error": (cur + " | " + renew_note).strip(" |") if cur else renew_note}

            account_store.update_account(str(account.get("id")), patch)
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


def refresh_account_credits(
    account: dict[str, Any], *, skip_if_unchanged: bool = False
) -> dict[str, Any]:
    """查询一个账号的当前积分并回写。

    ``skip_if_unchanged``：自动同步用——积分没变就不往「积分记录」里堆，
    否则 5 分钟一条会把历史刷满（上限 2000 条，两天就滚动没了）。
    """
    provider = str(account.get("provider") or "").strip()
    if account.get("source") == "server":
        # 只在服务器存在的代跑记录（别机上传的）：本机没有凭证，查询和回写都没有意义
        return {"ok": False, "message": "该记录仅存在于服务器，本机无凭证可查"}
    blob = account.get("token_blob") if isinstance(account.get("token_blob"), dict) else {}
    if provider == "workbuddy":
        info = workbuddy.query_from_blob(blob)
    elif provider == "traework":
        info = traework.query_from_blob(blob)
    else:
        return {"ok": False, "message": "未知 provider"}
    if info.get("ok"):
        credits = info.get("today_credit") if provider == "workbuddy" else info.get("credits")
        streak = info.get("streak")
        checked_in_today = bool(info.get("today_checked_in"))
        changed = False

        def patch(row: dict[str, Any]) -> dict[str, Any]:
            # 和"盘上这一行"比，不是和调用方几分钟前拿到的快照比：否则 changed 会算错，
            # 积分记录要么白记一条、要么该记的没记。
            nonlocal changed
            changed = credits != row.get("last_credits") or streak != row.get("last_streak")
            out: dict[str, Any] = {}
            if credits is not None:
                out["last_credits"] = credits
            if streak is not None:
                out["last_streak"] = streak
            if checked_in_today and not row.get("last_ok_at"):
                out["last_ok_at"] = datetime.now().isoformat(timespec="seconds")
            return out

        account_store.update_account(str(account.get("id")), patch)
        info = {**info, "credits": credits, "changed": changed}
        if skip_if_unchanged and not changed:
            return info
        account_store.append_credit_history(
            {
                "account_id": account.get("id"),
                "provider": provider,
                "ok": True,
                "credits": credits,
                "streak": streak,
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

    def patch(row: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {"last_task_at": datetime.now().isoformat(timespec="seconds")}
        if result.get("ok"):
            # 只有真跑成才占掉「今天已跑」：失败必须留给下一个窗口重试
            out["last_task_day"] = today
            out["last_task_done"] = result.get("done")
            out["last_task_total"] = result.get("total")
            out["last_task_rest"] = result.get("rest") or []
            out["last_error"] = ""
        else:
            out["last_error"] = result.get("message") or "成长任务失败"
        return out

    account_store.update_account(str(account.get("id")), patch)
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
        try:
            results.append(run_workbuddy_tasks(account, log=log, settings=settings, force=force))
        except Exception as exc:  # noqa: BLE001 - 单账号异常不能带走整批任务
            _log(log, f"[成长任务] {account.get('label') or account.get('id')} 异常: {mask_text(exc, 160)}")
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
        try:
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
        except Exception as exc:  # noqa: BLE001 - 一个账号的续期/上传异常不能带走整批，也不能杀调度线程
            _log(log, f"[代跑凭证] {label} 异常: {mask_text(exc, 160)}")
            result = {"ok": False, "message": mask_text(exc, 160)}
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


def _run_gap(settings: dict[str, Any]) -> float:
    """账号之间的随机等待秒数。

    原来硬编码 0.8~1.8 秒：十几个号挤在两秒内连续问供应商，风控特征太明显。
    脏值一律钳制，上限 600 秒 —— 误填一个巨大的数不能把整轮跑批挂死。
    """
    try:
        low = int(settings.get("run_gap_min_sec"))
        high = int(settings.get("run_gap_max_sec"))
    except (TypeError, ValueError):
        low, high = 20, 60
    low = min(max(low, 0), 600)
    high = min(max(high, low), 600)
    return random.uniform(low, high) if high > low else float(low)


def run_local_all(
    *, require_license: bool = True, log: LogFn | None = None, account_id: str = ""
) -> list[dict[str, Any]]:
    """本机跑一轮签到。``account_id`` 非空时只跑那一个号（界面行内「签到」）。"""
    settings = load_settings()
    if require_license:
        ok, msg = ensure_licensed(settings, force_online=True)
        if not ok:
            _log(log, f"卡密无效：{msg}")
            return [{"ok": False, "message": msg}]

    rows = account_store.load_accounts()
    if account_id:
        rows = [r for r in rows if str(r.get("id")) == str(account_id)]
    results: list[dict[str, Any]] = []
    first = True
    for account in rows:
        if not account.get("enabled", True):
            continue
        if str(account.get("run_mode") or "local") != "local":
            continue
        # 间隔只加在「号与号之间」：第一个号直接跑，最后一个号跑完不再白等；
        # 每一对之间重新摇一次，整批共用一个随机数等于没随机。
        if not first:
            gap = _run_gap(settings)
            if gap > 0:
                time.sleep(gap)
        first = False
        _log(log, f"签到 {account.get('provider')} / {account.get('label') or account.get('id')} ...")
        label = account.get("label") or account.get("id") or account.get("provider")
        try:
            result = run_one_account(account, log=log)
        except Exception as exc:  # noqa: BLE001 - 一个账号炸了不能带走整批，更不能带走调度线程
            result = {"ok": False, "provider": account.get("provider"), "message": f"{label} 执行异常: {mask_text(exc, 160)}"}
        results.append(result)
        _log(log, result.get("message") or str(result))
        # WorkBuddy 成长中心任务（在本机签到之后顺带做，幂等）
        if settings.get("workbuddy_task_mode") == "local" and str(account.get("provider")) == "workbuddy":
            try:
                run_workbuddy_tasks(account, log=log, settings=settings)
            except Exception as exc:  # noqa: BLE001
                _log(log, f"[成长任务] 异常: {mask_text(exc, 160)}")
    return results


WINDOW_SEC_DEFAULT = 7200


def _window_sec(settings: dict[str, Any]) -> int:
    """窗口缓冲秒数：脏值退回默认。

    这里吃过 ``int("abc")`` 的亏——设置是能被前端写歪的东西，一旦抛异常，
    ``_maybe_run_slot`` 就永远进不到判断，整台机器的日签静默停摆。
    """
    try:
        return int(settings.get("schedule_window_sec") or WINDOW_SEC_DEFAULT)
    except (TypeError, ValueError):
        return WINDOW_SEC_DEFAULT


def slot_targets(settings: dict[str, Any]) -> list[tuple[str, int, int]]:
    """配置里的签到窗口，按时间排序：``(槽位名, 时, 分)``。脏值退回默认。"""
    slots: list[tuple[str, int, int]] = [
        (
            "morning",
            parse_int_field(settings.get("schedule_hour"), "schedule_hour"),
            parse_int_field(settings.get("schedule_minute"), "schedule_minute"),
        )
    ]
    if settings.get("evening_schedule", True):
        slots.append(
            (
                "evening",
                parse_int_field(settings.get("evening_hour"), "evening_hour"),
                parse_int_field(settings.get("evening_minute"), "evening_minute"),
            )
        )
    return sorted(slots, key=lambda item: (item[1], item[2]))


def plan_startup_catchup(
    settings: dict[str, Any], now: datetime, fired: set[str], day: str
) -> str | None:
    """当天所有窗口都过完了还没跑 → 返回要补的槽位名，否则 ``None``。

    窗口内的正常触发不归这里管（那是 `_maybe_run_slot`）。这里补的是「开机就晚了」这一类：
    机器 20 点后才有电、窗口缓冲期又已经过完，那种情况下 `_maybe_run_slot` 到死都不会命中，
    当天一次都不会签，而界面只会安静地显示「今日窗口已过」。
    """
    try:
        if not settings.get("auto_schedule", True) or not settings.get("catchup_on_start", True):
            return None
        window = _window_sec(settings)
        slot, hour, minute = slot_targets(settings)[-1]
        if f"{day}:启动补签" in fired or f"{day}:{slot}" in fired:
            return None
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    except (AttributeError, TypeError, ValueError, IndexError):
        return None
    return slot if (now - target).total_seconds() > window else None


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

    def status(self) -> dict[str, Any]:
        """界面状态灯：线程是否活着 + 自动签到是否开 + 下一个到点窗口。

        「已暂停」和「调度线程静默死了」以前在界面上长得一模一样（都显示一切正常），
        用户只能靠「今天怎么没签到」反推。
        """
        try:
            settings = load_settings()
        except Exception:  # noqa: BLE001 - 状态读取不能反过来把界面带崩
            settings = {}
        return {
            "enabled": bool(settings.get("auto_schedule", True)),
            "running": bool(self._thread and self._thread.is_alive()),
            "nextAt": self._next_slot(settings),
        }

    @staticmethod
    def _next_slot(settings: dict[str, Any]) -> str:
        """下一个还没过的签到窗口，纯展示用；脏配置退回默认值，越界的直接跳过。"""
        try:
            slots = [
                (
                    int(settings.get("schedule_hour") or 9),
                    int(settings.get("schedule_minute") or 10),
                    "早签",
                )
            ]
            if settings.get("evening_schedule", True):
                slots.append(
                    (
                        int(settings.get("evening_hour") or 20),
                        int(settings.get("evening_minute") or 0),
                        "补漏",
                    )
                )
        except (TypeError, ValueError):
            return ""
        now = datetime.now()
        for hour, minute, label in sorted(slots):
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                continue
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target > now:
                return f"{label} {target.strftime('%H:%M')}"
        return "今日窗口已过"

    def _slot_key(self, day: str, slot: str) -> str:
        return f"{day}:{slot}"

    def _maybe_run_slot(self, settings: dict[str, Any], now: datetime, day: str, slot: str, hour: int, minute: int) -> None:
        key = self._slot_key(day, slot)
        if key in self._fired:
            return
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        window = _window_sec(settings)
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
        try:
            run_local_all(require_license=True, log=self._log)
        except Exception as exc:  # noqa: BLE001 - 整批兜底：炸在这里会让调度线程结束，之后所有签到静默停摆
            _log(self._log, f"[{slot}] 调度执行异常: {mask_text(exc, 200)}")
        # 异常也算"这个窗口跑过了"：否则会每 20 秒重放一次，反复打供应商并刷满日志。
        # 真漏掉的号还有晚间补漏窗口兜底。
        self._fired.add(key)

    def _maybe_catchup_on_start(self, settings: dict[str, Any], now: datetime, day: str) -> None:
        """当天的窗口全过完了还没跑 → 启动后补一次。

        判断交给 `plan_startup_catchup`（纯函数，好测），这里只负责「一天最多一轮」和兜底。
        """
        slot = plan_startup_catchup(settings, now, self._fired, day)
        if not slot:
            return
        # 先占坑再跑：批量执行一旦抛异常，回到 _loop 时 20 秒后又会进到这里，
        # 不先记就变成每 20 秒重放一轮整批签到。
        self._fired.add(self._slot_key(day, "启动补签"))
        _log(self._log, f"[启动补签] 今天「{slot}」窗口已过，立即补跑一次")
        try:
            run_local_all(require_license=True, log=self._log)
        except Exception as exc:  # noqa: BLE001 - 补签失败不能带崩调度线程
            _log(self._log, f"[启动补签] 执行异常: {mask_text(exc, 200)}")

    def _loop(self) -> None:
        while not self._stop.is_set():
            # 循环体整体兜底：这个线程一死，早晚签到就静默停了，用户看不出来。
            # （设置读取 / 账号文件读取 / 解析都可能在循环里抛，见 vault.load 的 OSError）
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001
                _log(self._log, f"[调度] 轮次异常: {mask_text(exc, 200)}")
            self._stop.wait(20)

    def _tick(self) -> None:
        settings = load_settings()
        if not settings.get("auto_schedule", True):
            return  # 等下一轮由 _loop 的 20 秒间隔驱动
        # 代跑凭证保鲜：启动先跑一次，之后每 6 小时一次
        if time.time() - self._last_server_sync >= SERVER_SYNC_INTERVAL_SEC:
            self._last_server_sync = time.time()
            try:
                refresh_server_credentials(log=self._log)
            except Exception as exc:  # noqa: BLE001
                _log(self._log, f"[代跑凭证] 同步异常: {mask_text(exc, 200)}")
        now = datetime.now()
        day = now.strftime("%Y-%m-%d")
        # 窗口清单与「启动补签」共用 slot_targets，避免两处各写一份、口径慢慢漂开
        for slot, hour, minute in slot_targets(settings):
            self._maybe_run_slot(settings, now, day, slot, hour, minute)
        # 窗口内开机时上面的正常槽位已经把当天标记 fired 了，补签自然不会再抢；
        # 只有「开机就全过完了」才会真正跑。
        self._maybe_catchup_on_start(settings, now, day)


def clamp_sync_minutes(value: Any, default: int = 5) -> int:
    """同步间隔：留空 / 0 / 非数字都退回默认，其余夹到 1..240 分钟。"""
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return default
    if minutes == 0:
        return default
    return max(1, min(minutes, 240))


def _sync_minutes(settings: dict[str, Any]) -> int:
    return clamp_sync_minutes(settings.get("auto_sync_minutes"))


class AutoSyncer:
    """定时把「哪些号跑了 / 现在多少积分」拉到界面，省掉手点。

    视图数据（服务器代跑状态、跑批记录）走 ``account_store`` 的 15 秒 TTL 缓存，
    界面每 30 秒重读一次几乎零成本；积分得逐个问供应商，所以单独用更长的间隔
    （``auto_sync_minutes``），并让路给正在执行的手动任务。
    """

    TICK_SEC = 30.0
    FIRST_DELAY_SEC = 4.0

    def __init__(
        self,
        *,
        get_settings: Callable[[], dict[str, Any]],
        is_busy: Callable[[], bool] | None = None,
        log: LogFn | None = None,
    ) -> None:
        self._get_settings = get_settings
        self._is_busy = is_busy or (lambda: False)
        self._log = log
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        # 0 = 启动后第一次 tick 就同步，界面不至于先空着
        self._last_credit_sync = 0.0
        self._syncing = False
        self._last: dict[str, Any] = {"at": None, "message": "尚未同步"}

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        # 重新开启（含卡密失效后恢复）应当立刻拉一次，别等满一个间隔
        self._last_credit_sync = 0.0
        self._thread = threading.Thread(target=self._loop, name="auto-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def sync_soon(self) -> None:
        """让下一次 tick 立刻同步：激活卡密、刚打开开关时调用。"""
        self._last_credit_sync = 0.0

    def syncing(self) -> bool:
        return self._syncing

    def status(self) -> dict[str, Any]:
        settings = self._get_settings()
        with self._lock:
            last = dict(self._last)
        return {
            **last,
            "enabled": bool(settings.get("auto_sync", True)),
            "minutes": _sync_minutes(settings),
            "running": bool(self._thread and self._thread.is_alive()),
        }

    # ------------------------------------------------------------ 同步动作
    def sync_once(self, *, reason: str = "自动", query_credits: bool = True) -> dict[str, Any]:
        """拉一次：服务器状态 + 本机账号积分。阻塞，调用方放到线程里跑。

        全局只允许一份在跑（``try_acquire_credit_slot``）：自动同步、手动同步、
        刷新积分并发问同一个账号，只会换来供应商限流和互相覆盖的回写。
        """
        if not try_acquire_credit_slot():
            return {"ok": False, "busy": True, "message": "已有同步任务在执行，请等待完成"}
        self._syncing = True
        try:
            return self._run_sync(reason=reason, query_credits=query_credits)
        finally:
            self._syncing = False
            release_credit_slot()

    def _run_sync(self, *, reason: str, query_credits: bool) -> dict[str, Any]:
        account_store.invalidate_server_cache()
        rows = account_store.load_accounts()
        # today_board 顺带把服务器聚合的跑批记录拉回来；账号视图复用上面那份
        board = account_store.today_board(accounts=rows)
        checked = changed = failed = 0
        if query_credits:
            for row in rows:
                if self._stop.is_set():
                    break
                if not row.get("enabled", True):
                    continue
                blob = row.get("token_blob") if isinstance(row.get("token_blob"), dict) else {}
                if not (blob.get("token") or blob.get("access_token")):
                    continue  # 纯服务器代跑记录：本机没凭证，问不了
                info = refresh_account_credits(row, skip_if_unchanged=True)
                checked += 1
                if not info.get("ok"):
                    failed += 1
                elif info.get("changed"):
                    changed += 1
                time.sleep(0.3)
        summary = {
            "at": datetime.now().strftime("%H:%M:%S"),
            "reason": reason,
            "accounts": len(rows),
            "checked": checked,
            "changed": changed,
            "failed": failed,
            "done": board.get("done_count"),
            "pending": board.get("pending_count"),
            "runFailed": board.get("failed_count"),
            "message": (
                f"同步完成：今日已跑 {board.get('done_count', 0)}/{board.get('total_enabled', 0)}，"
                f"积分查询 {checked} 个"
                + (f"，{failed} 个失败" if failed else "")
                + (f"，{changed} 个有变化" if changed else "")
            ),
        }
        with self._lock:
            self._last = dict(summary)
        if changed or failed or reason != "自动":
            _log(self._log, f"[{reason}同步] {summary['message']}")
        return {"ok": True, **summary}

    def _due(self, settings: dict[str, Any]) -> bool:
        return time.time() - self._last_credit_sync >= _sync_minutes(settings) * 60

    def tick(self) -> bool:
        """判定并（该同步时）执行一次同步，返回是否真的同步了。"""
        settings = self._get_settings()
        if (
            not settings.get("auto_sync", True)
            or self._is_busy()
            or credit_slot_busy()  # 手动同步/刷新积分正在跑，让路
            or not self._due(settings)
        ):
            return False
        # 先记时间戳：这一次失败也不该下一轮重来，免得卡在网络坏的时候
        # （激活成功的时机由界面调 sync_soon() 补回来）
        self._last_credit_sync = time.time()
        ok, _msg = ensure_licensed(settings, force_online=False)
        if not ok:
            return False
        try:
            self.sync_once(reason="自动")
        except Exception as exc:  # noqa: BLE001 - 后台同步不能把线程搞没了
            _log(self._log, f"[自动同步] 异常: {exc}")
            return False
        return True

    def _loop(self) -> None:
        self._stop.wait(self.FIRST_DELAY_SEC)
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001
                _log(self._log, f"[自动同步] 轮次异常: {exc}")
            self._stop.wait(self.TICK_SEC)
