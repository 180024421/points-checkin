# -*- coding: utf-8 -*-
"""授权守卫：断网不能踢人，明确拒绝必须踢，重新激活必须能恢复。"""

from checkin_tool import license_client
from checkin_tool.license_guard import LicenseGuard


def _guard(monkeypatch, result: dict, *, interval: float = 3600.0):
    revoked: list[str] = []
    logs: list[str] = []
    monkeypatch.setattr(license_client, "check_status", lambda settings, *, force_online=False: dict(result))
    g = LicenseGuard(
        get_settings=lambda: {},
        on_revoked=revoked.append,
        log=logs.append,
        interval_sec=interval,
    )
    return g, revoked, logs


def test_valid_check_keeps_ticket_out_of_status(monkeypatch):
    """status() 会被宿主塞进渲染进程（get_bootstrap），票据绝不能跟着走。"""
    g, _revoked, _ = _guard(
        monkeypatch,
        {
            "ok": True,
            "valid": True,
            "message": "授权有效",
            "license": {"valid": True, "accountLimit": 3, "ticket": "tk-" + "s" * 40, "primaryCard": "CARD-1"},
        },
    )
    g.check_now()
    status = g.status()
    blob = str(status)
    assert "tk-" not in blob and "CARD-1" not in blob, "界面侧一律拿不到 ticket / 卡密"
    assert status["last"]["license"]["accountLimit"] == 3, "额度字段要留着，界面按它显示"
    assert g.get_account_limit() == 3


def test_network_error_does_not_kick(monkeypatch):
    g, revoked, logs = _guard(
        monkeypatch,
        {"ok": False, "valid": False, "message": "无法连接到授权服务器", "networkError": True, "raw": {"code": -1}},
    )
    out = g.check_now()
    assert out["revoked"] is False and out["valid"] is None
    assert revoked == []
    assert g.status()["netFail"] == 1
    assert "暂不踢出" in logs[-1]


def test_crypto_error_does_not_kick(monkeypatch):
    g, revoked, _ = _guard(
        monkeypatch,
        {"ok": False, "valid": False, "message": "加密通道失败", "cryptoError": True, "raw": {"code": -1}},
    )
    assert g.check_now()["revoked"] is False
    assert revoked == []


def test_explicit_server_rejection_kicks_once(monkeypatch):
    g, revoked, _ = _guard(
        monkeypatch,
        {"ok": True, "valid": False, "message": "卡密已到期", "raw": {"code": 4001}},
    )
    out = g.check_now()
    assert out == {"valid": False, "revoked": True, "message": "卡密已到期", "explicit": True}
    assert revoked == ["卡密已到期"]
    g.check_now()
    assert revoked == ["卡密已到期"], "重复踢出必须是幂等的"
    assert g._revoked is True


def test_valid_result_resets_failure_counter(monkeypatch):
    g, _, _ = _guard(monkeypatch, {"ok": True, "valid": False, "message": "x", "networkError": True})
    g.check_now()
    assert g.status()["netFail"] == 1
    monkeypatch.setattr(license_client, "check_status", lambda s, *, force_online=False: {"ok": True, "valid": True})
    g.check_now()
    assert g.status()["netFail"] == 0


def test_reset_clears_revoked_and_restarts_thread(monkeypatch):
    g, revoked, _ = _guard(monkeypatch, {"ok": True, "valid": False, "message": "卡密失效", "raw": {"code": 4001}})
    g.check_now()
    assert revoked and g._revoked is True

    g.reset()
    assert g._revoked is False
    assert g.status()["netFail"] == 0
    assert g._thread is not None and g._thread.is_alive(), "重新激活后后台校验要恢复"
    g.stop()


def test_loop_keeps_running_after_revoke(monkeypatch):
    """被踢出后线程不能退出，否则本进程再也不会恢复校验。"""
    g, _, _ = _guard(monkeypatch, {"ok": True, "valid": False, "message": "失效", "raw": {"code": 1}}, interval=0.01)
    g._revoked = True
    g.start()
    assert g._thread.is_alive()
    g.stop()
    g._thread.join(timeout=2)
    assert not g._thread.is_alive()
