# -*- coding: utf-8 -*-
from checkin_tool.adapters.workbuddy import checkin_with_token
from checkin_tool.adapters.base import CheckinResult


def test_workbuddy_already_code_is_success(monkeypatch):
    calls = {"n": 0}

    def fake_post(url, token, uid, timeout=20.0):
        calls["n"] += 1
        if "checkin-activity-status" in url:
            return 200, {
                "code": 0,
                "data": {
                    "active": True,
                    "today_checked_in": False,
                    "streak_days": 3,
                    "daily_credit": 100,
                },
            }
        return 400, {"code": 10001, "msg": "今天已签到"}

    monkeypatch.setattr("checkin_tool.adapters.workbuddy._api_post", fake_post)
    result = checkin_with_token("tok", "uid")
    assert isinstance(result, CheckinResult)
    assert result.ok is True
    assert result.already is True
