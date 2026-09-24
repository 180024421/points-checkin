"""启动补签：机器每天晚开机时，当天的签到窗口已经过完，得在启动后补跑一次。

场景来自实际事故：机器连续三天在 20:16~21:06 才开机，`schedule_window_sec` 7200
让 20:00 的补漏窗口在 22:00 关窗；一旦开机更晚（或窗口本身设得早），
当天就一次都不会跑，而界面只会安静地显示「今日窗口已过」。
"""
from __future__ import annotations

from datetime import datetime

from checkin_tool import scheduler


def at(hhmm: str) -> datetime:
    hour, minute = hhmm.split(":")
    return datetime(2026, 9, 22, int(hour), int(minute), 0)


DAY = "2026-09-22"
BASE = {
    "auto_schedule": True,
    "catchup_on_start": True,
    "schedule_hour": 9,
    "schedule_minute": 10,
    "evening_schedule": True,
    "evening_hour": 20,
    "evening_minute": 0,
    "schedule_window_sec": 7200,
}


def plan(settings: dict, now: datetime, fired=None):
    return scheduler.plan_startup_catchup({**BASE, **settings}, now, set(fired or []), DAY)


def test_returns_none_while_a_window_is_still_upcoming():
    # 21:00 开机：20:00 的窗口还开着（7200 秒到 22:00），交给正常调度，不该抢跑
    assert plan({}, at("21:00")) is None


def test_returns_none_before_the_morning_window():
    assert plan({}, at("08:00")) is None


def test_catches_up_after_the_last_window_closed():
    assert plan({}, at("22:05")) == "evening"


def test_evening_disabled_makes_morning_the_last_window():
    # 早签窗口 9:10 + 7200 秒缓冲到 11:10 关窗，11:30 才算真的错过
    assert plan({"evening_schedule": False}, at("11:30")) == "morning"


def test_skips_when_the_last_window_already_ran_today():
    assert plan({}, at("22:05"), [f"{DAY}:evening"]) is None


def test_catches_up_when_only_the_morning_window_ran():
    # 早签跑了、机器在补漏窗口前又关了 → 仍该补
    assert plan({}, at("22:05"), [f"{DAY}:morning"]) == "evening"


def test_runs_at_most_once_per_day():
    assert plan({}, at("22:05"), [f"{DAY}:启动补签"]) is None


def test_respects_the_master_switches():
    assert plan({"catchup_on_start": False}, at("22:05")) is None
    assert plan({"auto_schedule": False}, at("22:05")) is None


def test_dirty_window_falls_back_to_default_without_raising():
    assert plan({"schedule_window_sec": "abc"}, at("22:05")) == "evening"
    # 窗口拉到 1 小时：20:30 就已经算错过了
    assert plan({"schedule_window_sec": 600}, at("20:30")) == "evening"


def test_dirty_slot_times_fall_back_to_defaults():
    # 时刻被写歪时退回默认 9:10 / 20:00，而不是整条补签判断静默失效
    assert plan({"evening_hour": "abc", "evening_minute": ""}, at("22:05")) == "evening"
    assert plan({"evening_schedule": False, "schedule_hour": None}, at("11:30")) == "morning"


# --- 接线：方法必须真的只跑一轮，而且异常不许每 20 秒重放 ---


class Calls:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self, **kwargs) -> list:
        assert kwargs.get("require_license") is True, "补签必须走授权校验，不能绕过卡密"
        self.count += 1
        return []


def _scheduler(monkeypatch, batch):
    monkeypatch.setattr(scheduler, "run_local_all", batch)
    return scheduler.DailyScheduler(log=lambda _m: None)


def test_maybe_catchup_runs_exactly_one_batch(monkeypatch):
    calls = Calls()
    s = _scheduler(monkeypatch, calls)
    s._maybe_catchup_on_start(BASE, at("22:05"), DAY)
    s._maybe_catchup_on_start(BASE, at("22:05"), DAY)
    assert calls.count == 1
    assert f"{DAY}:启动补签" in s._fired


def test_maybe_catchup_stays_quiet_inside_a_window(monkeypatch):
    calls = Calls()
    s = _scheduler(monkeypatch, calls)
    s._maybe_catchup_on_start(BASE, at("21:00"), DAY)
    assert calls.count == 0


def test_maybe_catchup_does_not_replay_after_an_exception(monkeypatch):
    def boom(**_kwargs):
        raise RuntimeError("供应商炸了")

    s = _scheduler(monkeypatch, boom)
    s._maybe_catchup_on_start(BASE, at("22:05"), DAY)
    s._maybe_catchup_on_start(BASE, at("22:05"), DAY)  # 第二次不得再抛


def test_maybe_catchup_retries_when_the_vendor_slot_is_busy(monkeypatch):
    # 启动时自动同步常常正占着供应商槽；补签是一天唯一一次兜底，不能被一轮忙吃掉
    rounds = iter([[{"ok": False, "busy": True, "message": "已有同步任务在跑"}], []])

    calls = Calls()

    def batch(**kwargs):
        calls(**kwargs)
        return next(rounds)

    s = _scheduler(monkeypatch, batch)
    s._maybe_catchup_on_start(BASE, at("22:05"), DAY)
    assert calls.count == 1
    assert f"{DAY}:启动补签" not in s._fired, "让路不能算补签已跑过"
    s._maybe_catchup_on_start(BASE, at("22:05"), DAY)
    assert calls.count == 2
    assert f"{DAY}:启动补签" in s._fired


def test_tick_calls_the_catchup_after_the_regular_slots(monkeypatch):
    # _tick 里补签排在正常窗口之后：窗口内开机时正常槽位已经 fired，不会重复跑
    called: list[str] = []
    s = scheduler.DailyScheduler(log=lambda _m: None)
    monkeypatch.setattr(s, "_maybe_run_slot", lambda *_a, **_k: None)
    monkeypatch.setattr(s, "_maybe_catchup_on_start", lambda *_a: called.append("catchup"))
    monkeypatch.setattr(scheduler, "load_settings", lambda: dict(BASE))
    monkeypatch.setattr(s, "_last_server_sync", 9e15)  # 跳过代跑凭证刷新
    s._tick()
    assert called == ["catchup"]
