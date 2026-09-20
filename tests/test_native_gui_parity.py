# -*- coding: utf-8 -*-
"""两条前端（pywebview 的 index.html 与 tkinter 的 gui.py）不得各说各话。

界面重复本身是没办法的（一个给用户看、一个是无 WebView 环境下的回退），
但「口径」重复一定会出事：同一个筛选条件在两边数量不一样、tkinter 写出的
设置项被网页端白名单拒收、积分阈值一边生效一边不生效。这里全部按文本比对钉住。
"""

from __future__ import annotations

import pathlib
import re

from checkin_tool import gui, webview_app

HTML = (pathlib.Path(gui.__file__).parent / "ui" / "index.html").read_text(encoding="utf-8")


def _attr_values(pattern: str) -> set[str]:
    return set(re.findall(pattern, HTML))


def test_provider_filter_options_are_the_same_three():
    """侧栏筛选项必须等于网页端 data-pf，且都是 scheduler 真认识的 provider。"""
    assert {value for value, _ in gui.PROVIDER_FILTERS} == _attr_values(r'data-pf="([^"]*)"')
    dispatched = set(re.findall(r'provider\s*==\s*"([a-z_]+)"', pathlib.Path(gui.__file__).with_name("scheduler.py").read_text(encoding="utf-8")))
    assert {value for value, _ in gui.PROVIDER_FILTERS} - {""} <= dispatched


def test_nav_pages_match_the_webview_tabs():
    keys = [key for key, _ in gui.PAGES]
    assert set(keys) == _attr_values(r'data-tab="([^"]+)"')
    # 分组里漏一个页面 = 那条页在原生端永远进不去
    assert sorted(k for _, group in gui.NAV_GROUPS for k in group) == sorted(keys)


def test_account_table_shows_every_webview_column_but_ops():
    """列口径对齐；「操作」在 tkinter 里是行选中后的按钮条 + 右键菜单，不是单元格。"""
    headers = set(re.findall(r"<th[^>]*>([^<]+)</th>", HTML[HTML.index('id="acc-table"'):HTML.index('id="acc-body"')]))
    assert headers == {"平台", "账号", "积分余额", "连签", "今日状态", "Token", "最近结果", "模式", "操作"}
    gui_headers = {"平台", "账号", "积分余额", "连签", "今日状态", "Token", "最近结果", "模式"}
    assert gui_headers <= headers


def test_provider_labels_agree():
    assert gui.PROVIDER_LABEL == {
        "traework": "TRAE",
        "workbuddy": "WB",
    }
    # 网页端筛选按钮上的文案（TRAE / WB）必须与原生端一致
    for value, text in gui.PROVIDER_FILTERS:
        if value:
            assert text in HTML


def test_checkin_payload_keys_are_all_whitelisted_by_the_webview():
    """tkinter 直接写盘，网页端走白名单；两边键集不一致时，网页端会「保存成功但没存」。"""
    payload = gui.checkin_settings_payload(
        {
            "auto": True,
            "sched_hour": "9",
            "sched_min": "10",
            "evening": True,
            "ev_hour": "20",
            "ev_min": "0",
            "gap_min": "20",
            "gap_max": "60",
            "low_credit": "100",
            "wb_mode": "local",
            "wb_chat": True,
            "auto_sync": True,
            "sync_minutes": "5",
        }
    )
    assert set(payload) == {
        "auto_schedule",
        "schedule_hour",
        "schedule_minute",
        "evening_schedule",
        "evening_hour",
        "evening_minute",
        "run_gap_min_sec",
        "run_gap_max_sec",
        "credit_low_threshold",
        "workbuddy_task_mode",
        "workbuddy_chat_tasks",
        "auto_sync",
        "auto_sync_minutes",
    }
    assert set(payload) <= set(webview_app._SETTING_VALIDATORS)
    for key, value in payload.items():
        ok, _ = webview_app._SETTING_VALIDATORS[key](value)
        assert ok, f"网页端拒收 {key}={value!r}：两条前端的取值口径已经漂移"


def test_zero_is_a_real_value_and_reversed_gap_gets_swapped():
    """0 秒间隔 / 0 阈值都是合法选择；填反的区间落盘前必须换回来。"""
    zero = gui.checkin_settings_payload({"gap_min": "0", "gap_max": "0", "low_credit": "0"})
    assert zero["run_gap_min_sec"] == zero["run_gap_max_sec"] == 0
    assert zero["credit_low_threshold"] == 0

    swapped = gui.checkin_settings_payload({"gap_min": "90", "gap_max": "30"})
    assert (swapped["run_gap_min_sec"], swapped["run_gap_max_sec"]) == (30, 90)

    blank = gui.checkin_settings_payload({"gap_min": "", "gap_max": "abc", "sched_hour": ""})
    assert (blank["run_gap_min_sec"], blank["run_gap_max_sec"]) == (20, 60)
    assert blank["schedule_hour"] == 9

    dirty = gui.checkin_settings_payload({"gap_max": "99999", "sched_hour": "-3"})
    assert dirty["run_gap_max_sec"] == 600
    assert dirty["schedule_hour"] == 0


def test_webview_save_settings_orders_the_gap_the_same_way():
    """网页端保存时也得走同一条区间整理规则，否则一边存 30~90、一边存 90~30。"""
    from checkin_tool.settings import ordered_gap

    assert ordered_gap("90", "30") == (30, 90)
    assert ordered_gap("0", "0") == (0, 0)
    assert ordered_gap("", None) == (20, 60)


def test_stat_counters_use_the_same_classification_as_the_web_page():
    rows = [
        {"today_status": "已签到", "enabled": True, "last_error": "", "token_expired": False},
        {"today_status": "未跑", "enabled": True, "last_error": "", "token_expired": False},
        {"today_status": "失败", "enabled": True, "last_error": "boom", "token_expired": False},
        {"today_status": "未跑", "enabled": False, "last_error": "", "token_expired": True},
    ]
    # 一行同时命中「失败 + 报错」「停用 + 过期」也只算一次异常
    assert gui.account_stats(rows) == (4, 1, 3, 2)


def test_low_credit_flag_only_fires_for_workbuddy_with_real_numbers():
    assert gui.low_credit({"provider": "workbuddy", "last_credits": 40}, 100) is True
    assert gui.low_credit({"provider": "traework", "last_credits": 40}, 100) is False
    assert gui.low_credit({"provider": "workbuddy", "last_credits": None}, 100) is False
    assert gui.low_credit({"provider": "workbuddy", "last_credits": 40}, 0) is False
    assert gui.low_credit({"provider": "workbuddy", "last_credits": "1,234"}, 100) is False


def test_local_account_rule_matches_the_web_view_row_buttons():
    """行内「签到」在什么情况下出现：只在服务器存在的记录、代跑行都不给本机按钮。"""
    assert gui.local_account({"source": "local", "run_mode": "local"}) is True
    assert gui.local_account({"source": "local", "run_mode": "server"}) is False
    assert gui.local_account({"source": "server", "run_mode": "local"}) is False


def test_native_entry_points_reuse_the_shared_backend():
    """原生端不许自己实现上传/更换：必须调 delegate，否则修一处漏一处。"""
    source = pathlib.Path(gui.__file__).read_text(encoding="utf-8")
    assert "delegate.upload_delegate_accounts(" in source
    assert "delegate.replace_server_account(" in source
    assert "account_store.try_upsert_account(" in source  # 入库仍走带额度守卫的那条路
    # 单号签到必须真的把 account_id 传下去，否则行内「签到」会惊动全部账号
    assert re.search(r"run_local_all\([^)]*account_id=account_id", source, re.S)
    assert "refresh_account_credits(account)" in source
