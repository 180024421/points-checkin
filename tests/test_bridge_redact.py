# -*- coding: utf-8 -*-
"""桥接层回给界面的数据里不能有票据：DevTools / 注入 JS 拿到的就是它。"""

from checkin_tool.webview_app import CheckinApi


def test_strip_secrets_covers_nested_license():
    stripped = CheckinApi._strip_secrets(
        {
            "ok": True,
            "valid": True,
            "ticket": "tk-" + "x" * 40,
            "license": {"valid": True, "expireAt": "2026-10-01", "ticket": "tk-" + "x" * 40, "primaryCard": "C-1"},
        }
    )
    blob = str(stripped)
    assert "tk-" not in blob and "C-1" not in blob
    assert stripped["license"]["expireAt"] == "2026-10-01", "额度/到期这些展示字段要保留"


def test_strip_secrets_tolerates_missing_license():
    assert CheckinApi._strip_secrets("not a dict") == {}
    assert CheckinApi._strip_secrets({"message": "ok"}) == {"message": "ok"}
