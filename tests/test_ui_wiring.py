# -*- coding: utf-8 -*-
"""界面 id 与脚本引用对账。

这类崩法是静默的：HTML 里把 ``acc-list`` 改成 ``acc-body``，JS 忘跟着改，
``$('acc-list').onclick`` 在 boot() 里抛 TypeError，整屏脚本一起停摆，
而 Python 侧的测试全绿。所以这里只盯一件事——脚本引用的 id 必须真的存在。
"""

import re
from pathlib import Path

UI_FILE = Path(__file__).resolve().parent.parent / "checkin_tool" / "ui" / "index.html"


def _parts() -> tuple[str, str]:
    src = UI_FILE.read_text(encoding="utf-8")
    scripts = re.findall(r"<script>(.*?)</script>", src, re.S)
    assert scripts, "界面里找不到内联脚本，正则锚点变了"
    return src[: src.index("<script>")], scripts[-1]


def _markup_ids() -> set[str]:
    markup, _ = _parts()
    return set(re.findall(r'\bid="([^"]+)"', markup))


def test_every_dollar_id_reference_exists():
    _, script = _parts()
    ids = _markup_ids()
    refs = set(re.findall(r"\$\('([A-Za-z0-9_ -]+)'\)", script))
    assert len(refs) > 30, f"只取到 {len(refs)} 个引用，说明正则失效而不是真的没引用"
    missing = sorted(r for r in refs if r not in ids)
    assert not missing, f"脚本里这些 id 在 HTML 中不存在：{missing}"


def test_nav_tabs_all_have_a_page():
    """侧栏点一个不存在的 page → classList of null，切页整块失效。"""
    markup, _ = _parts()
    tabs = set(re.findall(r'data-tab="([^"]+)"', markup))
    assert tabs, "没找到导航项"
    pages = set(re.findall(r'id="page-([^"]+)"', markup))
    assert tabs <= pages, f"导航指向不存在的页面：{sorted(tabs - pages)}"


def test_schedule_setting_fields_are_both_filled_and_saved():
    """新增设置键必须「回填 + 提交」两头都有，缺回填就是每次打开都变默认值。"""
    _, script = _parts()
    ids = _markup_ids()
    fields = {
        "set-sched-hour", "set-sched-min", "set-ev-hour", "set-ev-min",
        "set-gap-min", "set-gap-max", "set-low-credit",
        "set-auto", "set-evening", "set-wb-mode", "set-wb-chat",
        "set-auto-sync", "set-sync-minutes", "set-autostart", "set-trae-capture",
    }
    assert fields <= ids, f"HTML 里缺这些设置控件：{sorted(fields - ids)}"
    apply_body = script[script.index("function applySettings") : script.index("function applyTraeWatch")]
    collect_body = script[script.index("function collectSettings") : script.index("function applyTraeWatch")]
    unfilled = sorted(f for f in fields if f"'{f}'" not in apply_body)
    unsaved = sorted(f for f in fields if f not in collect_body)
    assert not unfilled, f"这些控件不会被回填：{unfilled}"
    assert not unsaved, f"这些控件不在保存范围内：{unsaved}"


def test_provider_filter_values_match_the_scheduler_dispatch():
    """筛选值写错不会报错，只会永远筛出空列表：拿调度器真正分支用的字符串对账。"""
    markup, _ = _parts()
    filters = set(re.findall(r'data-pf="([^"]*)"', markup))
    assert "" in filters, "缺少「全部」这一档"
    scheduler_src = (UI_FILE.parent.parent / "scheduler.py").read_text(encoding="utf-8")
    handled = set(re.findall(r'provider == "([^"]+)"', scheduler_src))
    assert handled, "调度器里找不到 provider 分支，锚点变了"
    unknown = sorted((filters - {""}) - handled)
    assert not unknown, f"筛选器里有调度器不认识的 provider：{unknown}"
