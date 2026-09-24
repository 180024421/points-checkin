# -*- coding: utf-8 -*-
"""跑批的诚实度：没得可领既不算成功也不算异常；网络抖动要换 base；错误体不带凭证。"""

from urllib.error import HTTPError

from checkin_tool import account_store, scheduler
from checkin_tool.adapters import traework, workbuddy
from checkin_tool.adapters.base import CheckinResult


def _no_retry(monkeypatch, module):
    """retry_call 的等待是 5 秒起步，离线测试只跑一次就够。"""
    monkeypatch.setattr(module, "retry_call", lambda fn, **_kw: fn())


def _traework_blob(**over):
    blob = {
        "token": "atk",
        "machine_id": "m-1",
        "device_id": "d-1",
        "ug_api_base": "https://a.invalid",
        "ug_api_bases": ["https://b.invalid"],
    }
    blob.update(over)
    return blob


def test_traework_not_open_is_neither_success_nor_failure(monkeypatch):
    """签到入口没开放：不能标绿（今天其实什么都没领到），也不该标红刷屏。"""
    _no_retry(monkeypatch, traework)
    monkeypatch.setattr(
        traework, "_api_post", lambda url, headers, timeout=30.0, data=None: (200, {"code": 0, "data": {"enable": False}})
    )
    res = traework.checkin_with_blob(_traework_blob())
    assert res.ok is False and res.already is False
    assert res.skipped is True
    assert "未开放" in res.message


def test_skipped_result_clears_error_without_marking_done(monkeypatch):
    row = {"id": "a", "last_error": "上次网络失败"}
    monkeypatch.setattr(
        account_store, "update_account", lambda _id, mutate: (row.update(mutate(row) or {}), True)[1]
    )
    monkeypatch.setattr(account_store, "append_credit_history", lambda *_a, **_kw: None)
    scheduler._update_account_after_run(
        "a", CheckinResult(ok=False, provider="workbuddy", skipped=True, message="没有活动")
    )
    assert row["last_error"] == ""
    assert "last_ok_at" not in row, "没领到积分不能写成今天已签到"


def test_run_log_entry_skipped_is_its_own_status():
    assert account_store._process_run_log_entry({"ok": False, "skipped": True})["status"] == "未开放"
    assert account_store._process_run_log_entry({"ok": False})["status"] == "失败"


def test_traework_network_failure_falls_through_to_next_base(monkeypatch):
    """主域名抖一下不能整轮放弃：备用 base 一次都没被访问过就报失败。"""
    _no_retry(monkeypatch, traework)
    seen: list[str] = []

    def fake_post(url, headers, timeout=30.0, data=None):
        seen.append(url)
        if "a.invalid" in url:
            return -1, {"msg": "网络失败: timed out"}
        return 200, {"code": 0, "data": {"enable": True, "checked_in": True, "credits": 120}}

    monkeypatch.setattr(traework, "_api_post", fake_post)
    res = traework.checkin_with_blob(_traework_blob())
    assert len(seen) == 2 and "b.invalid" in seen[1]
    assert res.ok is True and res.credits == 120


def test_traework_all_bases_down_stays_retryable(monkeypatch):
    _no_retry(monkeypatch, traework)
    monkeypatch.setattr(traework, "_api_post", lambda *a, **kw: (-1, {"msg": "网络失败"}))
    res = traework.checkin_with_blob(_traework_blob())
    assert res.ok is False
    assert res.raw_summary.get("retryable") is True, "全 base 连不上必须还能重试"


def test_traework_claim_without_business_code_is_not_success(monkeypatch):
    """200 但不是业务响应（网关 HTML 页）以前算签到成功，界面就此不再重试。"""
    _no_retry(monkeypatch, traework)

    def fake_post(url, headers, timeout=30.0, data=None):
        if url.endswith(traework.STATUS_PATH):
            return 200, {"code": 0, "data": {"enable": True, "checked_in": False}}
        return 200, {"raw": "<html>502 Bad Gateway</html>"}

    monkeypatch.setattr(traework, "_api_post", fake_post)
    res = traework.checkin_with_blob(_traework_blob())
    assert res.ok is False
    assert res.raw_summary.get("claimed") is None


def test_traework_missing_credits_stays_unknown(monkeypatch):
    _no_retry(monkeypatch, traework)

    def fake_post(url, headers, timeout=30.0, data=None):
        if url.endswith(traework.STATUS_PATH):
            return 200, {"code": 0, "data": {"enable": True, "checked_in": False}}
        return 200, {"code": 0}

    monkeypatch.setattr(traework, "_api_post", fake_post)
    res = traework.checkin_with_blob(_traework_blob())
    assert res.ok is True
    assert res.credits is None, "响应没给积分就填 200 是编数据"


def test_workbuddy_no_campaign_is_skipped(monkeypatch):
    _no_retry(monkeypatch, workbuddy)
    monkeypatch.setattr(
        workbuddy, "_api_post", lambda url, token, uid, timeout=20.0: (200, {"code": 0, "data": {"active": False}})
    )
    res = workbuddy.checkin_with_token("tok", "uid")
    assert res.ok is False and res.already is False and res.skipped is True
    assert "没有进行中的签到活动" in res.message


def test_workbuddy_error_body_keeps_credentials_out(monkeypatch):
    class _Body:
        def read(self):
            return b'bad gateway access_token="abcdef1234567890"'

    err = HTTPError("https://x.invalid", 502, "Bad Gateway", {}, _Body())
    monkeypatch.setattr(workbuddy, "urlopen", lambda req, timeout=None: (_ for _ in ()).throw(err))
    status, resp = workbuddy._api_post("https://x.invalid", "tok", "uid")
    assert status == 502
    assert "abcdef1234567890" not in str(resp), "错误响应体原文会随 last_error 落盘"


def test_workbuddy_status_failure_does_not_dump_whole_dict(monkeypatch):
    _no_retry(monkeypatch, workbuddy)
    monkeypatch.setattr(
        workbuddy,
        "_api_post",
        lambda url, token, uid, timeout=20.0: (200, {"code": 500, "msg": "内部错误 token='zzz'"}),
    )
    res = workbuddy.checkin_with_token("tok", "uid")
    assert res.ok is False
    assert "raw" not in res.message and "{'ok'" not in res.message


def test_run_local_all_yields_to_running_vendor_task(monkeypatch):
    """定时跑批和自动同步/手动刷新用的是同一批 token，并行只会互相覆盖回写。"""
    assert scheduler.try_acquire_credit_slot()
    ran: list[str] = []
    try:
        monkeypatch.setattr(scheduler, "run_one_account", lambda account, **_kw: ran.append(account["id"]))
        out = scheduler.run_local_all(require_license=False, log=lambda _m: None)
    finally:
        scheduler.release_credit_slot()
    assert ran == []
    assert out[0]["busy"] is True


def test_run_local_all_releases_the_slot(monkeypatch):
    monkeypatch.setattr(scheduler, "load_settings", lambda: {})
    monkeypatch.setattr(account_store, "load_accounts", lambda **kw: [])
    assert scheduler.run_local_all(require_license=False, log=lambda _m: None) == []
    assert scheduler.credit_slot_busy() is False


def test_maybe_run_slot_retries_after_yielding(monkeypatch):
    """让路不算这个窗口跑过了，否则一次撞上同步就整天不签。"""
    from datetime import datetime

    s = scheduler.DailyScheduler(log=lambda _m: None)
    monkeypatch.setattr(scheduler, "run_local_all", lambda **kw: [{"ok": False, "busy": True, "message": "忙"}])
    now = datetime.now()
    day = now.strftime("%Y-%m-%d")
    s._maybe_run_slot({}, now, day, "morning", now.hour, now.minute)
    assert s._slot_key(day, "morning") not in s._fired
