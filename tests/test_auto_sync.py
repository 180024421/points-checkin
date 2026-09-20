# -*- coding: utf-8 -*-
"""自动同步：启动即同步、按间隔查积分、让路给手动任务、失败与无凭证的处理。"""

import time

from checkin_tool import account_store, scheduler
from checkin_tool.scheduler import AutoSyncer


def _account(**over):
    row = {
        "id": "a1",
        "provider": "workbuddy",
        "label": "A",
        "enabled": True,
        "run_mode": "local",
        "token_blob": {"token": "tk-1"},
        "last_credits": 100,
        "last_streak": 3,
    }
    row.update(over)
    return row


def _patch_store(monkeypatch, rows, board=None):
    monkeypatch.setattr(account_store, "invalidate_server_cache", lambda: None)
    monkeypatch.setattr(
        account_store,
        "today_board",
        lambda **kw: dict(
            board
            or {
                "done_count": 1,
                "pending_count": 1,
                "failed_count": 0,
                "total_enabled": 2,
                "done": [],
                "pending": [],
                "failed": [],
            }
        ),
    )
    monkeypatch.setattr(account_store, "load_accounts", lambda **kw: [dict(r) for r in rows])
    monkeypatch.setattr(time, "sleep", lambda _s: None)  # 循环里的限速别真的等


def _syncer(settings=None, *, busy=False):
    merged = {"auto_sync": True, "auto_sync_minutes": 5}
    merged.update(settings or {})
    return AutoSyncer(get_settings=lambda: dict(merged), is_busy=lambda: busy)


def test_sync_only_queries_enabled_accounts_holding_credentials(monkeypatch):
    rows = [
        _account(),
        _account(id="off", enabled=False),
        _account(id="srv", run_mode="server", source="server", token_blob={}),
    ]
    _patch_store(monkeypatch, rows)
    seen: list[str] = []
    monkeypatch.setattr(
        scheduler,
        "refresh_account_credits",
        lambda acc, **kw: seen.append(acc["id"]) or {"ok": True, "changed": False},
    )
    out = _syncer().sync_once(reason="手动")
    assert seen == ["a1"], "停用账号和纯服务器代跑记录都不该被拿去问供应商"
    assert out["checked"] == 1 and out["changed"] == 0 and out["failed"] == 0


def test_sync_counts_failures_and_exposes_summary(monkeypatch):
    rows = [_account(), _account(id="a2", last_credits=7)]
    _patch_store(monkeypatch, rows)
    answers = [{"ok": False, "message": "token 过期"}, {"ok": True, "changed": True}]
    it = iter(answers)
    monkeypatch.setattr(scheduler, "refresh_account_credits", lambda acc, **kw: next(it))
    s = _syncer()
    out = s.sync_once()
    assert out["checked"] == 2 and out["failed"] == 1 and out["changed"] == 1
    assert out["done"] == 1 and out["pending"] == 1
    status = s.status()
    assert status["minutes"] == 5 and status["enabled"] is True
    assert "1 个失败" in status["message"] and "1 个有变化" in status["message"]


def test_tick_syncs_once_per_interval(monkeypatch):
    _patch_store(monkeypatch, [_account()])
    monkeypatch.setattr(scheduler, "refresh_account_credits", lambda acc, **kw: {"ok": True})
    monkeypatch.setattr(scheduler, "ensure_licensed", lambda settings, **kw: (True, "ok"))
    runs = []
    s = _syncer({"auto_sync_minutes": 5})
    monkeypatch.setattr(s, "sync_once", lambda **kw: runs.append(1) or {"ok": True})
    assert s.tick() is True, "启动后第一次 tick 就该同步，别让界面先空着"
    assert s.tick() is False, "间隔未到不能重复查积分"
    s._last_credit_sync = 0.0
    assert s.tick() is True
    assert len(runs) == 2


def test_tick_yields_to_manual_jobs_and_disabled_setting(monkeypatch):
    _patch_store(monkeypatch, [_account()])
    monkeypatch.setattr(scheduler, "ensure_licensed", lambda settings, **kw: (True, "ok"))
    busy = _syncer({}, busy=True)
    called = []
    monkeypatch.setattr(busy, "sync_once", lambda **kw: called.append(1))
    assert busy.tick() is False and not called, "有任务在跑时自动同步要让路"

    off = _syncer({"auto_sync": False})
    monkeypatch.setattr(off, "sync_once", lambda **kw: called.append(1))
    assert off.tick() is False and not called
    assert off.status()["enabled"] is False


def test_tick_does_not_hit_vendor_without_license(monkeypatch):
    _patch_store(monkeypatch, [_account()])
    monkeypatch.setattr(scheduler, "ensure_licensed", lambda settings, **kw: (False, "未激活"))
    s = _syncer()
    called = []
    monkeypatch.setattr(s, "sync_once", lambda **kw: called.append(1))
    assert s.tick() is False and not called


def test_auto_sync_stays_quiet_when_nothing_changed(monkeypatch):
    _patch_store(monkeypatch, [_account()])
    monkeypatch.setattr(scheduler, "refresh_account_credits", lambda acc, **kw: {"ok": True, "changed": False})
    logs: list[str] = []
    monkeypatch.setattr(account_store, "append_live_log", logs.append)
    s = _syncer()
    s.sync_once(reason="自动")  # 每 5 分钟一次，没变化就不刷日志
    assert logs == []
    s.sync_once(reason="手动")
    assert logs and "同步完成" in logs[-1]


def test_auto_sync_logs_failures(monkeypatch):
    _patch_store(monkeypatch, [_account()])
    monkeypatch.setattr(scheduler, "refresh_account_credits", lambda acc, **kw: {"ok": False, "message": "网络异常"})
    logs: list[str] = []
    monkeypatch.setattr(account_store, "append_live_log", logs.append)
    _syncer().sync_once(reason="自动")
    assert logs and "1 个失败" in logs[-1]


def test_credit_slot_serializes_overlapping_syncs(monkeypatch):
    """自动同步和手动刷新撞上时只能有一份在问供应商：同一个 token 不能被并发用两次。"""
    _patch_store(monkeypatch, [_account()])
    inner: list[dict] = []

    def fake_query(acc, **kw):
        inner.append(_syncer().sync_once(reason="手动"))
        return {"ok": True, "changed": False}

    monkeypatch.setattr(scheduler, "refresh_account_credits", fake_query)
    out = _syncer().sync_once(reason="自动")
    assert out["ok"] is True
    assert inner and inner[0].get("busy") is True, "第二份同步必须被串行槽挡掉"
    assert scheduler.credit_slot_busy() is False, "同步结束后必须把槽释放掉"


def test_tick_yields_when_manual_sync_holds_the_slot(monkeypatch):
    _patch_store(monkeypatch, [_account()])
    monkeypatch.setattr(scheduler, "ensure_licensed", lambda settings, **kw: (True, "ok"))
    assert scheduler.try_acquire_credit_slot() is True
    try:
        s = _syncer()
        called: list[int] = []
        monkeypatch.setattr(s, "sync_once", lambda **kw: called.append(1))
        assert s.tick() is False and not called, "手动同步在跑，自动同步要让路"
    finally:
        scheduler.release_credit_slot()


def test_sync_soon_makes_next_tick_fire(monkeypatch):
    """激活卡密/刚打开开关后要马上看到数据，不能等满一个间隔。"""
    _patch_store(monkeypatch, [_account()])
    monkeypatch.setattr(scheduler, "ensure_licensed", lambda settings, **kw: (True, "ok"))
    monkeypatch.setattr(scheduler, "refresh_account_credits", lambda acc, **kw: {"ok": True})
    s = _syncer({"auto_sync_minutes": 5})
    assert s.tick() is True
    assert s.tick() is False, "间隔未到不该重复查"
    s.sync_soon()
    assert s.tick() is True


def test_start_after_stop_resyncs_immediately(monkeypatch):
    _patch_store(monkeypatch, [_account()])
    monkeypatch.setattr(scheduler, "ensure_licensed", lambda settings, **kw: (True, "ok"))
    monkeypatch.setattr(scheduler, "refresh_account_credits", lambda acc, **kw: {"ok": True})
    s = _syncer({"auto_sync_minutes": 5})
    s._last_credit_sync = time.time()  # 上一轮刚跑完
    s.start()
    try:
        assert s._last_credit_sync == 0.0, "重新开启同步（含卡密恢复）应当立刻拉一次"
    finally:
        s.stop()


def test_unchanged_credits_do_not_append_history(monkeypatch):
    """积分没变化就别往「积分记录」里堆，否则历史两天就被刷满。"""
    row = _account()
    appended: list[dict] = []

    def fake_update(account_id, mutate):
        # 真实实现是锁内读-改-写本地行，这里把改动直接映回假行，changed 才算得准
        patch = mutate(row)
        row.update(patch)
        return bool(patch)

    monkeypatch.setattr(account_store, "update_account", fake_update)
    monkeypatch.setattr(account_store, "append_credit_history", appended.append)
    monkeypatch.setattr(
        scheduler.workbuddy, "query_from_blob", lambda _blob: {"ok": True, "today_credit": 100, "streak": 3}
    )
    info = scheduler.refresh_account_credits(row, skip_if_unchanged=True)
    assert info["changed"] is False
    assert appended == []

    monkeypatch.setattr(
        scheduler.workbuddy, "query_from_blob", lambda _blob: {"ok": True, "today_credit": 120, "streak": 3}
    )
    info = scheduler.refresh_account_credits(row, skip_if_unchanged=True)
    assert info["changed"] is True
    assert len(appended) == 1 and appended[0]["credits"] == 120


def test_sync_interval_is_clamped():
    assert scheduler._sync_minutes({}) == 5
    assert scheduler._sync_minutes({"auto_sync_minutes": 0}) == 5, "留空/0 视为默认，不是每 0 分钟"
    assert scheduler._sync_minutes({"auto_sync_minutes": "abc"}) == 5
    assert scheduler._sync_minutes({"auto_sync_minutes": 99999}) == 240
    assert scheduler._sync_minutes({"auto_sync_minutes": -3}) == 1
    # 界面上的间隔输入框走同一个口径，免得两边 clamp 不一致
    assert scheduler.clamp_sync_minutes("") == 5
    assert scheduler.clamp_sync_minutes(" 12 ") == 12
    assert scheduler.clamp_sync_minutes(None) == 5
