# -*- coding: utf-8 -*-
"""代挂额度以服务端 /entitlement/info 为准，且取额度的 HTTP 不能落在 IO 锁里。"""

import json
import time

import pytest

from checkin_tool import account_store, license_client, server_client


def _reset():
    account_store.invalidate_server_cache()
    account_store.invalidate_entitlement_cache()
    account_store._ent_last_error = None


def test_normalize_entitlement_tolerates_field_names():
    live = server_client.normalize_entitlement({
        "quota": 10, "used": 2, "contactEmail": "a@b.com", "contactVerified": False,
        "licenseActive": True, "timeUnlimited": True, "expireAt": None,
    })
    assert live["quota"] == 10
    assert live["contactVerified"] is False
    assert live["contactEmail"] == "a@b.com"
    # 服务端字段改名时也不能抛
    other = server_client.normalize_entitlement({"accountQuota": "3", "contact_verified": "1"})
    assert other["quota"] == 3
    assert other["contactVerified"] is True
    # 0 / 负数 = 未配置额度，与「不限」同样处理
    assert server_client.normalize_entitlement({"quota": 0})["quota"] is None
    assert server_client.normalize_entitlement({})["quota"] is None
    assert server_client.normalize_entitlement({})["contactVerified"] is None


def test_account_limit_prefers_entitlement_over_license(monkeypatch):
    _reset()
    monkeypatch.setattr(license_client, "load_cache", lambda: {"accountLimit": 1})
    try:
        # 服务端额度取不到时才有 accountLimit 兜底（它是设备座位数）
        assert account_store.get_account_limit() == 1
        monkeypatch.setattr(account_store, "_ent_cache", (time.monotonic(), {"quota": 10}))
        assert account_store.get_account_limit() == 10
    finally:
        _reset()


def test_contact_binding_unknown_when_offline():
    _reset()
    try:
        assert account_store.contact_binding() == {"verified": None, "email": ""}
    finally:
        _reset()


def test_refresh_entitlement_caches_and_warns_once(monkeypatch):
    _reset()
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise RuntimeError("connection refused")

    logs: list[str] = []
    monkeypatch.setattr(account_store.server_client, "entitlement_info", boom)
    monkeypatch.setattr(account_store, "append_live_log", logs.append)
    try:
        assert account_store.refresh_entitlement() == {}
        assert account_store.refresh_entitlement() == {}
        assert calls["n"] == 1, "失败也要推进缓存时间戳，断网时不能每次读都重打 HTTP"
        assert len(logs) == 1, "同样的失败不能反复刷日志"
    finally:
        _reset()


def test_upsert_enforces_entitlement_quota_without_http_under_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(account_store, "ACCOUNTS_FILE", tmp_path / "accounts.json")
    _reset()

    def guard():
        assert not account_store._IO_LOCK._is_owned(), "入库路径上的 HTTP 不能在 _IO_LOCK 内发起"
        return True

    def fake_list():
        guard()
        return []

    def fake_info():
        guard()
        return {"ok": True, "message": "", "data": {"quota": 2, "contactVerified": True}}

    monkeypatch.setattr(account_store.server_client, "list_server_accounts", fake_list)
    monkeypatch.setattr(account_store.server_client, "entitlement_info", fake_info)
    try:
        account_store.upsert_account({"provider": "workbuddy", "label": "A", "enabled": True})
        account_store.upsert_account({"provider": "workbuddy", "label": "B", "enabled": True})
        with pytest.raises(ValueError) as exc:
            account_store.upsert_account({"provider": "workbuddy", "label": "C", "enabled": True})
        assert "2" in str(exc.value)
        # 停用账号不占额度，仍可入库
        account_store.upsert_account({"provider": "workbuddy", "label": "C", "enabled": False})
        assert {r["label"] for r in account_store.load_accounts(include_server=False)} == {"A", "B", "C"}
    finally:
        _reset()


class _FakeResp:
    def __init__(self, payload: dict) -> None:
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


def test_bind_and_verify_requests_hit_server_and_invalidate_cache(monkeypatch):
    sent: dict[str, object] = {}

    def fake_urlopen(req, timeout=None):  # noqa: ARG001
        sent["url"] = req.full_url
        sent["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp({"code": 200, "message": "SUCCESS", "data": {"ok": True}})

    invalidated = {"n": 0}
    monkeypatch.setattr(server_client, "urlopen", fake_urlopen)
    monkeypatch.setattr(server_client, "load_settings", lambda: {})
    monkeypatch.setattr(server_client, "license_cfg_from_settings", lambda s: {"base_url": "http://x", "timeout": 5})
    monkeypatch.setattr(server_client, "transport_guard", lambda cfg: None)
    monkeypatch.setattr(server_client, "auth_headers", lambda s: {"appKey": "points-checkin"})
    monkeypatch.setattr(server_client, "load_cache", lambda: {"ticket": "tk-123456"})
    monkeypatch.setattr(account_store, "invalidate_entitlement_cache", lambda: invalidated.__setitem__("n", invalidated["n"] + 1))
    monkeypatch.setattr(account_store, "invalidate_server_cache", lambda: None)

    assert server_client.bind_contact(" a@b.com ")["ok"] is True
    assert sent["url"].endswith("/api/points-checkin/entitlement/bind")
    assert sent["body"]["contactEmail"] == "a@b.com"
    # 票据只能待在 body 里：进了 URL 就会被 nginx 访问日志记走
    assert sent["body"]["ticket"] == "tk-123456"
    assert "ticket" not in str(sent["url"]) and "a@b.com" not in str(sent["url"]).split("?", 1)[-1]

    assert server_client.verify_contact("123456")["ok"] is True
    assert sent["url"].endswith("/api/points-checkin/entitlement/verify")
    assert sent["body"]["verifyCode"] == "123456"
    # 绑定/验证成功后必须让缓存失效，否则界面还会停在「未绑定」
    assert invalidated["n"] == 2


def test_entitlement_info_normalizes_response(monkeypatch):
    def fake_urlopen(req, timeout=None):  # noqa: ARG001
        return _FakeResp({
            "code": 200,
            "message": "SUCCESS",
            "data": {"quota": 10, "used": 0, "contactEmail": None, "contactVerified": False},
        })

    monkeypatch.setattr(server_client, "urlopen", fake_urlopen)
    monkeypatch.setattr(server_client, "load_settings", lambda: {})
    monkeypatch.setattr(server_client, "license_cfg_from_settings", lambda s: {"base_url": "http://x", "timeout": 5})
    monkeypatch.setattr(server_client, "transport_guard", lambda cfg: None)
    monkeypatch.setattr(server_client, "auth_headers", lambda s: {})
    monkeypatch.setattr(server_client, "load_cache", lambda: {})
    monkeypatch.setattr(account_store, "invalidate_server_cache", lambda: None)
    monkeypatch.setattr(account_store, "invalidate_entitlement_cache", lambda: None)

    out = server_client.entitlement_info()
    assert out["ok"] is True
    assert out["data"]["quota"] == 10
    assert out["data"]["contactVerified"] is False
    assert out["data"]["contactEmail"] == ""


def test_contact_gate_blocks_only_on_explicit_unbound(monkeypatch):
    _reset()
    monkeypatch.setattr(account_store, "refresh_entitlement", lambda **kw: {})
    try:
        # 未知（离线 / 服务端没返回该字段）不能阻断用户
        assert account_store.contact_gate(force=False) is None
        monkeypatch.setattr(account_store, "_ent_cache", (time.monotonic(), {"contactVerified": True}))
        assert account_store.contact_gate(force=False) is None
        monkeypatch.setattr(account_store, "_ent_cache", (time.monotonic(), {"contactVerified": False}))
        assert "邮箱" in (account_store.contact_gate(force=False) or "")
    finally:
        _reset()


def test_bind_contact_rejects_bad_email_before_hitting_network(monkeypatch):
    def fail_post(*a, **kw):  # noqa: ARG001
        raise AssertionError("邮箱格式不对时不应该打服务器")

    monkeypatch.setattr(server_client, "_post", fail_post)
    assert server_client.bind_contact("not-an-email")["ok"] is False
    assert server_client.bind_contact("")["ok"] is False
    assert server_client.verify_contact("  ")["ok"] is False
