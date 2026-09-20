# -*- coding: utf-8 -*-
"""坐席额度三态：已知 / 0（到期）/ 未知，以及入库被拦时的结构化返回。

这里守的是「按号数收费」最容易伤人的一条边界：额度取不到 ≠ 不限，
也 ≠ 停用已有账号。两头的事故都真实发生过 ——
旧实现把「未知」当「不限」，一次授权抖动就把闸门全打开；
反过来若把「未知」当「拒绝一切」，断网用户连已经挂好的号都重新打开不了。
"""

import time
from datetime import datetime, timedelta, timezone

import pytest

from checkin_tool import account_store, license_client, server_client


@pytest.fixture(autouse=True)
def _no_side_effects(monkeypatch, tmp_path):
    """额度解析路径上的告警会写真实日志文件，测试里一律吃掉。"""
    account_store.invalidate_server_cache()
    account_store.invalidate_entitlement_cache()
    account_store._ent_last_error = None
    monkeypatch.setattr(account_store, "ACCOUNTS_FILE", tmp_path / "accounts.json")
    monkeypatch.setattr(account_store, "append_live_log", lambda *a, **kw: None)
    monkeypatch.setattr(account_store, "refresh_entitlement", lambda **kw: {})
    monkeypatch.setattr(account_store.server_client, "list_server_accounts", lambda: [])
    # 开发机真实授权缓存里有 accountLimit（=10），不隔离的话「额度未知」的用例
    # 会读到真实兜底值而被判成已知，测试结果随机器状态漂移。
    monkeypatch.setattr(license_client, "load_cache", lambda: {})
    yield
    account_store.invalidate_server_cache()
    account_store.invalidate_entitlement_cache()
    account_store._ent_last_error = None


def _ent(**data):
    """把代挂权益写进进程内缓存（等价于「服务端刚回过这个包」）。"""
    account_store._ent_cache = (time.monotonic(), data)


def test_quota_zero_is_a_real_quota_not_unlimited():
    # /entitlement/info 的 quota 服务端永远给数字（行缺失时兜底 default=10），
    # 所以 0 是真额度 = 坐席全部到期。这里必须原样保留，不能被抹成 None。
    assert server_client.normalize_entitlement({"quota": 0})["quota"] == 0
    # 负数才是「未配置」
    assert server_client.normalize_entitlement({"quota": -1})["quota"] is None
    assert server_client.normalize_entitlement({})["quota"] is None


def test_resolve_account_quota_three_states(monkeypatch):
    # 1) 服务端已知（含 0）
    _ent(quota=0)
    assert account_store.resolve_account_quota() == {"limit": 0, "known": True, "source": "server"}
    _ent(quota=3)
    assert account_store.resolve_account_quota()["limit"] == 3
    # 字符串数字也要吃进来（服务端偶尔按 varchar 回）
    _ent(quota="5")
    assert account_store.resolve_account_quota() == {"limit": 5, "known": True, "source": "server"}

    # 2) 服务端不可达 → 授权缓存里的 accountLimit 兜底（语义是设备座位数，只能兜底）
    account_store.invalidate_entitlement_cache()
    monkeypatch.setattr(license_client, "load_cache", lambda: {"accountLimit": 2})
    out = account_store.resolve_account_quota()
    assert out == {"limit": 2, "known": True, "source": "cache"}

    # 3) 两边都没有 → known=False，且绝不是「不限」
    account_store.invalidate_entitlement_cache()
    monkeypatch.setattr(license_client, "load_cache", lambda: {})
    out = account_store.resolve_account_quota()
    assert out == {"limit": None, "known": False, "source": ""}


def test_resolve_quota_does_not_reach_the_network(monkeypatch):
    # 入库路径在 _IO_LOCK 内读额度，这里必须是纯缓存读
    _ent(quota=7)

    def boom():
        raise AssertionError("读额度不能打 HTTP")

    monkeypatch.setattr(account_store.server_client, "entitlement_info", boom)
    assert account_store.resolve_account_quota()["limit"] == 7


@pytest.mark.parametrize("raw", ["", None, "abc", -1, True, False])
def test_garbage_account_limit_is_unknown_not_zero(raw, monkeypatch):
    account_store.invalidate_entitlement_cache()
    monkeypatch.setattr(license_client, "load_cache", lambda: {"accountLimit": raw})
    assert account_store.resolve_account_quota()["known"] is False


def test_new_enabled_account_rejected_when_quota_unknown():
    # 用户决定：额度未知 → 拒新增，但已有账号不受影响
    _ent()  # 有缓存但没有 quota 字段
    out = account_store.try_upsert_account({"provider": "workbuddy", "label": "A", "enabled": True})
    assert out["ok"] is False and out["code"] == "QUOTA_UNKNOWN"
    assert out["seats"] is None and out["used"] == 0
    # 「已有账号不受影响」的另一半：停用状态的新行（备份/回滚）照常入库
    assert account_store.try_upsert_account(
        {"provider": "workbuddy", "label": "B", "enabled": False}
    )["ok"] is True


def test_existing_account_can_be_reenabled_when_quota_unknown():
    # 未知额度下重新打开一条已有记录：这是「不阻断已有」的关键路径
    _ent(quota=1)
    ok = account_store.try_upsert_account({"provider": "workbuddy", "label": "A", "enabled": True})
    assert ok["ok"] is True
    account_id = ok["account"]["id"]
    assert account_store.try_upsert_account(
        {"id": account_id, "provider": "workbuddy", "label": "A", "enabled": False}
    )["ok"] is True
    account_store.invalidate_entitlement_cache()
    _ent()  # 现在额度未知
    assert account_store.try_upsert_account(
        {"id": account_id, "provider": "workbuddy", "label": "A", "enabled": True}
    )["ok"] is True


def test_quota_exceeded_carries_structured_seats_and_used():
    _ent(quota=1)
    first = account_store.try_upsert_account({"provider": "workbuddy", "label": "A", "enabled": True})
    out = account_store.try_upsert_account({"provider": "workbuddy", "label": "B", "enabled": True})
    assert out["ok"] is False and out["code"] == "QUOTA_EXCEEDED"
    assert out["seats"] == 1 and out["used"] == 1
    assert first["account"]["id"]
    # 界面拿到的是结构化字段，不需要 parse 中文句子
    assert isinstance(account_store.QuotaBlocked("X", "y"), ValueError)


def test_quota_zero_blocks_every_new_mount():
    _ent(quota=0)
    out = account_store.try_upsert_account({"provider": "traework", "label": "A", "enabled": True})
    assert out["ok"] is False and out["code"] == "QUOTA_EXCEEDED" and out["seats"] == 0


def test_try_upsert_converts_store_errors_into_results(monkeypatch):
    def boom(_account):
        raise RuntimeError("disk full")

    monkeypatch.setattr(account_store, "upsert_account", boom)
    out = account_store.try_upsert_account({"provider": "workbuddy", "label": "A"})
    assert out["ok"] is False and out["code"] == "STORE_FAILED" and "disk full" in out["message"]


def test_batch_capture_continues_after_quota_block():
    """批量采集里一条被拦不能中断整批：否则剩下的号 token 拿到了却没入库。"""
    _ent(quota=2)
    results = [
        account_store.try_upsert_account({"provider": "workbuddy", "label": f"A{i}", "enabled": True})
        for i in range(4)
    ]
    assert [r["ok"] for r in results] == [True, True, False, False]
    rows = account_store.load_accounts(include_server=False)
    assert {r["label"] for r in rows} == {"A0", "A1"}


def test_usage_exposes_quota_state_and_expiry():
    expire = datetime.now(timezone.utc) + timedelta(days=9)
    _ent(quota=None, timeUnlimited=False, expireAt=int(expire.timestamp() * 1000), contactVerified=True)
    # 只验字段渲染，账号视图直接传进来（额度未知时新增本来就会被拒，另有用例覆盖）
    usage = account_store.get_account_usage(
        accounts=[{"id": "a", "enabled": True}, {"id": "b", "enabled": False}]
    )
    # 服务端没给 quota、缓存里也没有 accountLimit → 额度未知，界面要显示「未知」而不是「不限」
    assert usage["quotaKnown"] is False and usage["quotaSource"] == "" and usage["limit"] is None
    assert usage["used"] == 1
    assert usage["timeUnlimited"] is False
    assert usage["expireDaysLeft"] in (8, 9), usage["expireDaysLeft"]
    assert usage["expireAtIso"].startswith("20")
