# -*- coding: utf-8 -*-
"""签到节奏与单号操作：账号间随机间隔、只跑一个号、调度状态灯。

这几条都是照着参考界面补的能力，重点是「别让界面把三种状态看成一种」：
自动签到关了 / 调度线程静默死了 / 正常运行，界面上必须能分辨。
"""

from datetime import datetime

from checkin_tool import account_store, scheduler, webview_app


def _base_monkey(monkeypatch, rows, sleep_calls):
    monkeypatch.setattr(scheduler, "ensure_licensed", lambda settings, **kw: (True, "ok"))
    monkeypatch.setattr(account_store, "load_accounts", lambda **kw: rows)
    monkeypatch.setattr(scheduler.time, "sleep", lambda s: sleep_calls.append(s))


def test_run_gap_falls_back_and_clamps():
    # 没配 / 脏值 → 退回 20~60，而不是 0（0 = 所有号挤在同一秒问供应商）
    assert 20 <= scheduler._run_gap({}) <= 60
    assert 20 <= scheduler._run_gap({"run_gap_min_sec": "abc", "run_gap_max_sec": None}) <= 60
    # 区间填反了要能自愈，不能算出负数 sleep
    assert 0 <= scheduler._run_gap({"run_gap_min_sec": 90, "run_gap_max_sec": 10}) <= 90
    # 上限钳到 600 秒：误填一个巨大的数不能把整轮跑批挂死几十分钟
    assert scheduler._run_gap({"run_gap_min_sec": 99999, "run_gap_max_sec": 999999}) == 600
    assert scheduler._run_gap({"run_gap_min_sec": -5, "run_gap_max_sec": -1}) == 0


def test_run_local_all_waits_between_accounts_only(monkeypatch):
    """间隔只存在于「号与号之间」：第一个号直接跑，最后一个号跑完不再白等。"""
    sleeps: list[float] = []
    rows = [
        {"id": "a", "provider": "workbuddy", "label": "A", "run_mode": "local"},
        {"id": "b", "provider": "workbuddy", "label": "B", "run_mode": "local"},
        {"id": "c", "provider": "workbuddy", "label": "C", "run_mode": "local"},
    ]
    _base_monkey(monkeypatch, rows, sleeps)
    monkeypatch.setattr(scheduler, "load_settings", lambda: {"run_gap_min_sec": 30, "run_gap_max_sec": 30})
    ran: list[str] = []
    monkeypatch.setattr(
        scheduler, "run_one_account", lambda account, *, log=None: ran.append(account["id"]) or {"ok": True}
    )

    scheduler.run_local_all(log=lambda _m: None)
    assert ran == ["a", "b", "c"]
    assert len(sleeps) == 2, "三个号只等两次"
    assert all(s == 30 for s in sleeps)


def test_run_local_all_single_account_skips_the_gap(monkeypatch):
    """行内「签到」只跑一个号：既不该跑别的号，也不该有任何等待。"""
    sleeps: list[float] = []
    rows = [
        {"id": "a", "provider": "workbuddy", "label": "A", "run_mode": "local"},
        {"id": "b", "provider": "workbuddy", "label": "B", "run_mode": "local"},
    ]
    _base_monkey(monkeypatch, rows, sleeps)
    monkeypatch.setattr(scheduler, "load_settings", lambda: {"run_gap_min_sec": 20, "run_gap_max_sec": 60})
    ran: list[str] = []
    monkeypatch.setattr(
        scheduler, "run_one_account", lambda account, *, log=None: ran.append(account["id"]) or {"ok": True}
    )

    out = scheduler.run_local_all(log=lambda _m: None, account_id="b")
    assert ran == ["b"] and len(out) == 1
    assert sleeps == []


def test_next_slot_points_at_the_upcoming_window(monkeypatch):
    class _Noon(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 20, 8, 0, 0)

    monkeypatch.setattr(scheduler, "datetime", _Noon)
    settings = {"schedule_hour": 9, "schedule_minute": 10, "evening_hour": 20, "evening_minute": 30}
    assert scheduler.DailyScheduler._next_slot(settings) == "早签 09:10"
    settings["schedule_hour"] = 7  # 早签已过 → 指向补漏
    assert scheduler.DailyScheduler._next_slot(settings) == "补漏 20:30"
    assert scheduler.DailyScheduler._next_slot(
        {"schedule_hour": 1, "schedule_minute": 0, "evening_schedule": False}
    ) == "今日窗口已过"
    # 脏配置不能把状态灯带崩
    assert scheduler.DailyScheduler._next_slot({"schedule_hour": "abc"}) == ""


def test_scheduler_status_reports_stopped_thread():
    s = scheduler.DailyScheduler(log=lambda _m: None)
    st = s.status()
    assert st["running"] is False, "没 start() 就是没在跑，界面要显示「已暂停」而不是「运行中」"
    assert "nextAt" in st


def test_new_schedule_settings_are_whitelisted_and_range_checked():
    validators = webview_app._SETTING_VALIDATORS
    for key in ("run_gap_min_sec", "run_gap_max_sec", "credit_low_threshold", "schedule_hour", "schedule_minute"):
        assert key in validators, f"{key} 不在白名单里，设置页保存会被静默丢掉"
    assert validators["run_gap_max_sec"](99999)[0] is False
    assert validators["schedule_hour"](24)[0] is False
    assert validators["schedule_minute"](59)[0] is True
    assert validators["credit_low_threshold"](0)[1] == 0, "0 = 关闭提醒，是合法值"
