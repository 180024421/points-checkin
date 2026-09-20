# -*- coding: utf-8 -*-
"""调度线程的存活底线：单账号异常不外溢、日志不双写、签名失败不炸线程。"""

import threading
from datetime import datetime

from checkin_tool import account_store, scheduler
from checkin_tool.adapters import traework


def _fake_sleep(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)


def test_log_writes_live_log_exactly_once_per_sink(monkeypatch):
    """两个界面的日志出口自己会落盘，_log 再写一遍就是每条存两份。"""
    written: list[str] = []
    monkeypatch.setattr(account_store, "append_live_log", written.append)

    def ui_sink(msg: str) -> None:  # 模拟 gui.append_log / CheckinApi._append_log
        account_store.append_live_log(msg)

    scheduler._log(ui_sink, "一条日志")
    assert written == ["一条日志"], "有 sink 时不能再自己落一次盘"
    scheduler._log(None, "没有 sink")
    assert written == ["一条日志", "没有 sink"], "没有 sink 时才由 _log 兜底落盘"


def test_run_local_all_keeps_going_when_one_account_blow_up(monkeypatch):
    _fake_sleep(monkeypatch)
    monkeypatch.setattr(scheduler, "load_settings", lambda: {})
    monkeypatch.setattr(scheduler, "ensure_licensed", lambda settings, **kw: (True, "ok"))
    monkeypatch.setattr(
        account_store,
        "load_accounts",
        lambda **kw: [
            {"id": "boom", "provider": "traework", "label": "B", "run_mode": "local"},
            {"id": "fine", "provider": "workbuddy", "label": "F", "run_mode": "local"},
        ],
    )
    ran: list[str] = []

    def fake_run(account, *, log=None):
        ran.append(str(account["id"]))
        if account["id"] == "boom":
            raise RuntimeError("UnsupportedAlgorithm: RSA key cannot do ECDSA")
        return {"ok": True, "message": "签到成功"}

    monkeypatch.setattr(scheduler, "run_one_account", fake_run)
    results = scheduler.run_local_all(log=lambda _m: None)
    assert ran == ["boom", "fine"], "一个账号抛错不能带走整批"
    assert results[0]["ok"] is False and "B 执行异常" in results[0]["message"]
    assert results[1]["ok"] is True


def test_run_local_all_masks_exception_text(monkeypatch):
    _fake_sleep(monkeypatch)
    monkeypatch.setattr(scheduler, "load_settings", lambda: {})
    monkeypatch.setattr(scheduler, "ensure_licensed", lambda settings, **kw: (True, "ok"))
    monkeypatch.setattr(
        account_store,
        "load_accounts",
        lambda **kw: [{"id": "a", "provider": "workbuddy", "label": "A", "run_mode": "local"}],
    )

    def blow_up(account, *, log=None):
        raise RuntimeError("token refresh_token=abcdef123456 failed")

    logs: list[str] = []
    monkeypatch.setattr(scheduler, "run_one_account", blow_up)
    out = scheduler.run_local_all(log=logs.append)
    assert "abcdef123456" not in out[0]["message"], "异常原文里的凭证不能进日志和 last_error"


def test_maybe_run_slot_fires_once_even_when_batch_raises(monkeypatch):
    """整批炸了也要记下这个窗口跑过：否则每 20 秒重放一次，反复打供应商。"""
    s = scheduler.DailyScheduler(log=lambda _m: None)
    monkeypatch.setattr(scheduler, "run_local_all", lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    now = datetime.now()
    s._maybe_run_slot({}, now, now.strftime("%Y-%m-%d"), "morning", now.hour, now.minute)
    assert s._slot_key(now.strftime("%Y-%m-%d"), "morning") in s._fired


def test_scheduler_loop_survives_tick_exception(monkeypatch):
    s = scheduler.DailyScheduler(log=lambda _m: None)
    calls = {"n": 0}

    def tick():
        calls["n"] += 1
        s._stop.set()  # 让循环跑一轮就退
        raise OSError("CryptUnprotectData 调用失败")

    monkeypatch.setattr(s, "_tick", tick)
    s._loop()  # 不该把异常抛出来
    assert calls["n"] == 1


def test_scheduler_loop_keeps_thread_alive_across_ticks(monkeypatch):
    """线程活着才有下一次签到：坏一轮不等于永久停摆。"""
    s = scheduler.DailyScheduler(log=lambda _m: None)
    ticks = {"n": 0}

    def tick():
        ticks["n"] += 1
        if ticks["n"] < 3:
            raise RuntimeError("账号库读不了")
        s._stop.set()  # 第三轮正常跑完后才停，证明前两轮的空异常没杀死线程

    monkeypatch.setattr(s, "_tick", tick)
    monkeypatch.setattr(s._stop, "wait", lambda _s: True)
    t = threading.Thread(target=s._loop, daemon=True)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive()
    assert ticks["n"] >= 3, "前几轮抛错后循环还得继续，直到 stop"


def test_refresh_with_rsa_device_key_returns_error_inst_of_raising():
    """设备私钥是采集来的，类型不合就只能续期失败，不能把异常抛穿到调度线程。"""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    pub = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    blob = {
        "refresh_token": "rt",
        "private_key_pem": priv,
        "public_key_pem": pub,
        "ug_api_base": "https://example.invalid",
        "token": "atk",
    }
    new_blob, err = traework.refresh_traework_token(blob, timeout=0.1)
    assert new_blob is None
    assert err and "签名" in err


def _task_writeback(monkeypatch, result: dict):
    """跑一次成长任务，返回真正写回本地那一行的字段（走 update_account 的 patch）。"""
    row: dict = {"id": "wb-1", "provider": "workbuddy", "label": "W"}
    monkeypatch.setattr(scheduler.workbuddy_tasks, "run_daily_tasks", lambda blob, **kw: dict(result))
    monkeypatch.setattr(account_store, "append_run_log", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        account_store,
        "update_account",
        lambda account_id, mutate: (row.update(mutate(row) or {}), True)[1],
    )
    scheduler.run_workbuddy_tasks(dict(row), log=lambda _m: None, settings={}, force=True)
    return row


def test_failed_task_run_does_not_consume_today(monkeypatch):
    """失败不能把「今天已跑」写掉：否则早窗炸一次，晚窗就不重试了。"""
    row = _task_writeback(monkeypatch, {"ok": False, "message": "任务列表拉取失败：HTTP 401", "done": 0, "total": 0})
    assert "last_task_day" not in row, "没跑成就不能占掉今天"
    assert "401" in row["last_error"]


def test_successful_task_run_marks_today_and_clears_error(monkeypatch):
    row = _task_writeback(
        monkeypatch,
        {"ok": True, "message": "成长任务完成 3/3", "done": 3, "total": 3, "rest": [], "failed": []},
    )
    assert row["last_task_day"] == datetime.now().strftime("%Y-%m-%d")
    assert row["last_task_done"] == 3
    assert row["last_error"] == "", "跑成后要清掉上一次的错误，界面别再挂旧红字"
