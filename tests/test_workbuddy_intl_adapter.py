# -*- coding: utf-8 -*-
from checkin_tool.adapters import workbuddy, workbuddy_intl


def test_intl_routes_to_copilot_host_and_labels_provider(monkeypatch):
    seen_urls: list[str] = []

    def fake_post(url, token, uid, timeout=20.0):
        seen_urls.append(url)
        if "checkin-activity-status" in url:
            return 200, {
                "code": 0,
                "data": {
                    "active": True,
                    "today_checked_in": False,
                    "streak_days": 5,
                    "today_credit": 100,
                },
            }
        return 200, {"code": 0, "data": {}}

    monkeypatch.setattr(workbuddy, "_api_post", fake_post)
    blob = {"access_token": "tok", "uid": "u-1"}
    result = workbuddy_intl.checkin_from_blob(blob)

    assert result.ok is True
    assert result.provider == "workbuddy_intl"
    # 状态查询 + 领取，两条都打在 copilot.tencent.com（不是 CN 的 codebuddy.cn）
    assert seen_urls and all(workbuddy_intl.BASE_INTL in u for u in seen_urls)
    assert any("daily-checkin" in u for u in seen_urls)


def test_intl_query_uses_copilot_host(monkeypatch):
    captured: dict[str, str] = {}

    def fake_post(url, token, uid, timeout=20.0):
        captured["url"] = url
        return 200, {"code": 0, "data": {"active": True, "today_credit": 42, "streak_days": 2}}

    monkeypatch.setattr(workbuddy, "_api_post", fake_post)
    info = workbuddy_intl.query_from_blob({"access_token": "tok", "uid": "u-1"})

    assert info.get("ok") is True
    assert workbuddy_intl.BASE_INTL in captured["url"]
    assert info.get("today_credit") == 42
