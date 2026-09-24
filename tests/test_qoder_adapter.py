# -*- coding: utf-8 -*-
"""Qoder 适配器：把「每天一条 CLAIM_BENEFIT」的领取语义钉死，不碰真实账号。"""
from __future__ import annotations

import time

from checkin_tool.adapters import qoder

NOW = time.time


def _campaign(campaign_id: str, action: str, status: str, start: float, end: float, amount: int = 100):
    c = {
        "campaignId": campaign_id,
        "actionType": action,
        "claimStatus": status,
        "startAt": start,
        "endAt": end,
    }
    if action == qoder.CLAIM_ACTION:
        c["benefit"] = {"kind": "CREDITS", "amount": amount}
    return c


def _payload(campaigns, claimable=False, show=True):
    return {"showCampaign": show, "claimable": claimable, "campaigns": campaigns}


def _patch_http(monkeypatch, responses):
    """responses: 每次 _http 调用弹一个 (status, payload)；GET/POST 按顺序取。"""
    calls: list[tuple[str, str]] = []

    def fake_http(method, url, token, *, timeout=20.0, body=None):
        calls.append((method, url))
        status, payload = responses[min(len(calls) - 1, len(responses) - 1)]
        return status, payload

    monkeypatch.setattr(qoder, "_http", fake_http)
    return calls


def test_query_maps_today_benefit_and_claimable(monkeypatch):
    day = _campaign("c-today", qoder.CLAIM_ACTION, "", NOW() - 60, NOW() + 3600, amount=100)
    _patch_http(monkeypatch, [(200, _payload([day], claimable=True))])
    info = qoder.query_status("dt-x", "u-1")
    assert info["ok"] is True
    assert info["today_checked_in"] is False
    assert info["claimable"] is True
    assert info["today_campaign_id"] == "c-today"
    assert info["today_credit"] == 100


def test_checkin_claims_daily_benefit_and_reports_credits(monkeypatch):
    unclaimed = _campaign("c-today", qoder.CLAIM_ACTION, "", NOW() - 60, NOW() + 3600, amount=100)
    claimed = _campaign("c-today", qoder.CLAIM_ACTION, qoder.CLAIMED_STATUS, NOW() - 60, NOW() + 3600, amount=100)
    calls = _patch_http(
        monkeypatch,
        [
            (200, _payload([unclaimed])),          # 初始查询
            (200, {"ok": True}),                    # POST claim
            (200, _payload([claimed])),             # 领后复查
        ],
    )
    result = qoder.checkin_with_token("dt-x", "u-1")
    assert result.ok is True
    assert result.provider == "qoder"
    assert result.already is False
    assert result.credits == 100
    assert ("POST", f"{qoder.BASE}{qoder.CAMPAIGNS_PATH}/c-today/claim") in calls


def test_checkin_reports_already_when_today_claimed(monkeypatch):
    claimed = _campaign("c-today", qoder.CLAIM_ACTION, qoder.CLAIMED_STATUS, NOW() - 60, NOW() + 3600, amount=100)
    calls = _patch_http(monkeypatch, [(200, _payload([claimed]))])
    result = qoder.checkin_with_token("dt-x", "u-1")
    assert result.ok is True
    assert result.already is True
    assert result.credits == 100
    # 已签到就不该再发领取 POST
    assert all(m == "GET" for m, _ in calls)


def test_checkin_skips_when_no_daily_campaign_window(monkeypatch):
    # 只有 VIEW_DETAILS，或 CLAIM_BENEFIT 还没到窗口 —— 都不算失败，也不标绿
    future = _campaign("c-next", qoder.CLAIM_ACTION, "", NOW() + 3600, NOW() + 7200)
    view = _campaign("c-view", "VIEW_DETAILS", qoder.CLAIMED_STATUS, NOW() - 60, NOW() + 3600)
    _patch_http(monkeypatch, [(200, _payload([future, view]))])
    result = qoder.checkin_with_token("dt-x", "u-1")
    assert result.ok is False
    assert result.skipped is True
    assert result.already is False


def test_claim_http_error_is_failure_retryable_on_5xx(monkeypatch):
    monkeypatch.setattr("checkin_tool.retry_util.time.sleep", lambda *_: None)
    unclaimed = _campaign("c-today", qoder.CLAIM_ACTION, "", NOW() - 60, NOW() + 3600, amount=100)
    _patch_http(
        monkeypatch,
        [
            (200, _payload([unclaimed])),
            (500, {"message": "server error"}),  # 领取 5xx
        ],
    )
    result = qoder.checkin_with_token("dt-x", "u-1")
    assert result.ok is False
    assert result.raw_summary.get("retryable") is True


def test_expired_blob_short_circuits_without_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("过期 token 不应触发网络")

    monkeypatch.setattr(qoder, "_http", boom)
    blob = {"access_token": "dt-x", "uid": "u-1", "expires_at": int((time.time() - 10) * 1000)}
    result = qoder.checkin_from_blob(blob)
    assert result.ok is False
    assert "过期" in result.message
