# -*- coding: utf-8 -*-
"""账号库读放大：服务器往返必须有 TTL 缓存，且不能占用文件 IO 锁。"""

import time
from datetime import datetime

from checkin_tool import account_store, secure_storage


def _reset_caches():
    account_store.invalidate_server_cache()


def test_server_accounts_are_cached_and_invalidate_on_demand(tmp_path, monkeypatch):
    monkeypatch.setattr(account_store, "ACCOUNTS_FILE", tmp_path / "accounts.json")
    account_store.save_accounts([{"id": "local1", "provider": "workbuddy", "label": "L"}])
    calls = {"n": 0}

    def fake_list():
        calls["n"] += 1
        # 关键约束：HTTP 发起时不能持锁，否则调度线程会阻塞 UI 的读写
        assert not account_store._IO_LOCK._is_owned(), "服务器请求不能在 _IO_LOCK 内发起"
        return [{"id": "srv1", "provider": "workbuddy", "label": "S", "run_mode": "server"}]

    monkeypatch.setattr(account_store.server_client, "list_server_accounts", fake_list)
    _reset_caches()
    try:
        first = account_store.load_accounts()
        second = account_store.load_accounts()
        assert calls["n"] == 1, "TTL 内重复读不应再打服务器"
        assert {r["id"] for r in first} == {"local1", "srv:srv1"}
        assert first == second
        # 代跑记录只能活在合并视图里，绝不能被写进本地账号库（否则服务器删了本地还留着幽灵行）
        assert account_store.load_accounts(include_server=False)[0]["id"] == "local1"

        account_store.load_accounts(include_server=False)
        assert calls["n"] == 1

        _reset_caches()
        account_store.load_accounts()
        assert calls["n"] == 2
    finally:
        _reset_caches()


def test_aggregated_run_data_is_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(account_store, "RUN_LOG_FILE", tmp_path / "run_log.json")
    calls = {"n": 0}

    def fake_fetch():
        calls["n"] += 1
        return []

    monkeypatch.setattr(account_store.server_client, "fetch_aggregated_checkin_data", fake_fetch)
    _reset_caches()
    try:
        account_store.today_run_map()
        account_store.today_run_map()
        assert calls["n"] == 1
        _reset_caches()
        account_store.today_run_map()
        assert calls["n"] == 2
    finally:
        _reset_caches()


def test_server_failure_falls_back_to_last_good_view(tmp_path, monkeypatch):
    monkeypatch.setattr(account_store, "ACCOUNTS_FILE", tmp_path / "accounts.json")
    monkeypatch.setattr(account_store, "_SERVER_TTL_SEC", 0.0)  # 每次都真的去拉
    account_store.save_accounts([])
    state = {"fail": False}

    def fake_list():
        if state["fail"]:
            raise RuntimeError("boom token=" + "s" * 40)
        return [{"id": "srv1", "provider": "workbuddy", "run_mode": "server"}]

    logs: list[str] = []
    monkeypatch.setattr(account_store.server_client, "list_server_accounts", fake_list)
    monkeypatch.setattr(account_store, "append_live_log", logs.append)
    _reset_caches()
    try:
        assert {r["id"] for r in account_store.load_accounts()} == {"srv:srv1"}
        state["fail"] = True
        rows = account_store.load_accounts()
        assert {r["id"] for r in rows} == {"srv:srv1"}, "拉取失败要退回上次结果，而不是把代跑账号清空"
        assert logs, "失败要留痕"
        assert "s" * 40 not in "".join(logs)
    finally:
        _reset_caches()


def test_server_rows_are_never_persisted_locally(tmp_path, monkeypatch):
    """任何写入路径都把代跑行挡在 accounts.json 外面。"""
    monkeypatch.setattr(account_store, "ACCOUNTS_FILE", tmp_path / "accounts.json")
    account_store.save_accounts(
        [
            {"id": "local1", "provider": "workbuddy"},
            {"id": "srv:srv1", "provider": "workbuddy", "source": "server", "server_account_id": "srv1"},
        ]
    )
    assert {r["id"] for r in account_store.load_accounts(include_server=False)} == {"local1"}


def _isolate_live_log(monkeypatch, tmp_path):
    """实时日志是进程内缓冲的，测试之间必须各用一份文件和状态。"""
    log_file = tmp_path / "live_log.json"
    monkeypatch.setattr(account_store, "LIVE_LOG_FILE", log_file)
    monkeypatch.setattr(account_store, "_live_buf", None)
    monkeypatch.setattr(account_store, "_live_dirty", False)
    monkeypatch.setattr(account_store, "_live_flushed_at", time.monotonic())
    return log_file


def test_live_log_writes_are_buffered_until_flush(monkeypatch, tmp_path):
    """整份 live_log 是 DPAPI 加密的：每写一行就重加密一遍会把 IO 锁变成界面瓶颈。"""
    log_file = _isolate_live_log(monkeypatch, tmp_path)
    account_store.append_live_log("第一条")
    account_store.append_live_log("第二条")
    assert not log_file.exists(), "未到一个落盘周期前不该写盘"

    lines = account_store.load_live_logs(10)  # 读内存缓冲，界面照常立刻能看到
    assert [l["message"] for l in lines] == ["第二条", "第一条"]

    account_store.flush_live_logs()
    assert log_file.exists(), "退出前 flush 必须把缓冲写下去"
    account_store._live_buf = None  # 强制下次从盘上读
    assert [l["message"] for l in account_store.load_live_logs(10)] == ["第二条", "第一条"]


def test_live_log_tail_flushes_on_read(monkeypatch, tmp_path):
    """集中打完一小段日志后进程闲置：读一次也该把尾巴带下去，不能等退出才落盘。"""
    log_file = _isolate_live_log(monkeypatch, tmp_path)
    monkeypatch.setattr(account_store, "_live_flushed_at", time.monotonic() - 10)
    account_store.append_live_log("第一条")  # 已过周期 → 落盘
    account_store.append_live_log("第二条")  # 未到周期 → 留在缓冲
    on_disk = secure_storage.load_json(log_file, {"lines": []})["lines"]
    assert [l["message"] for l in on_disk] == ["第一条"]

    monkeypatch.setattr(account_store, "_live_flushed_at", time.monotonic() - 10)
    assert [l["message"] for l in account_store.load_live_logs(5)] == ["第二条", "第一条"]
    on_disk = secure_storage.load_json(log_file, {"lines": []})["lines"]
    assert [l["message"] for l in on_disk] == ["第二条", "第一条"]


def test_live_log_entry_carries_local_date(monkeypatch, tmp_path):
    """只存时:分:秒的话跨天日志会混成一列，重放时更是分不清哪天。"""
    _isolate_live_log(monkeypatch, tmp_path)
    account_store.append_live_log("今天的")
    at = account_store.load_live_logs(1)[0]["at"]
    parsed = datetime.fromisoformat(at)
    assert parsed.date() == datetime.now().date()
    assert parsed.tzinfo is None, "存本地时间，前端不用再做时区换算"


def test_live_log_is_capped_and_clearable(monkeypatch, tmp_path):
    _isolate_live_log(monkeypatch, tmp_path)
    for i in range(account_store.MAX_LIVE_LOGS + 50):
        account_store.append_live_log(f"line {i}")
    assert len(account_store.load_live_logs(10_000)) == account_store.MAX_LIVE_LOGS
    account_store.clear_live_logs()
    assert account_store.load_live_logs(10) == []
    account_store.flush_live_logs()  # 清空后不该把缓冲又写回去
    assert account_store.load_live_logs(10) == []


def test_board_and_usage_reuse_preloaded_rows(monkeypatch, tmp_path):
    """一次界面刷新只合并一次账号：board/usage 都接受传进来的合并视图。"""
    merges: list[int] = []
    monkeypatch.setattr(account_store, "ACCOUNTS_FILE", tmp_path / "accounts.json")

    def counting_load(**_kw):
        merges.append(1)
        return []

    monkeypatch.setattr(account_store, "load_accounts", counting_load)
    monkeypatch.setattr(account_store, "today_run_map", lambda: {})
    monkeypatch.setattr(account_store, "public_account_view", lambda a, _t: {**a, "today_status": "未跑"})
    monkeypatch.setattr(account_store, "refresh_entitlement", lambda **_kw: {})
    monkeypatch.setattr(account_store, "get_entitlement", lambda: {})
    monkeypatch.setattr(account_store, "get_account_limit", lambda: 3)

    board = account_store.today_board(accounts=[{"id": "a", "enabled": True}], today={})
    usage = account_store.get_account_usage(accounts=[{"id": "a", "enabled": True}, {"id": "b", "enabled": False}])
    assert merges == [], "传了 accounts 就不该再合并一遍"
    assert board["pending_count"] == 1 and board["total_enabled"] == 1
    assert usage["used"] == 1 and usage["limit"] == 3
