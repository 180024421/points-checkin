# -*- coding: utf-8 -*-
"""原生（tkinter）回退前端冒烟：真窗口起来，逐页切换 + 点按钮 + 截图。

界面类改动最怕「代码看着对、窗口一开就崩」。tkinter 没有可注入假数据的桥，
所以这里直接把真窗口拉起来跑一遍：建窗 → 等后台取数 → 每页 raise + 重绘 →
把行内按钮真点一次（后端换成记账假件）→ 抓窗口位图存到系统临时目录。

检查跑在 ``after`` 回调里（也就是真实 mainloop 内）：主循环没跑起来时，
后台线程投递的 ``after(0, 渲染)`` 会直接 ``RuntimeError: main thread is not in main loop``，
那样什么都渲染不出来。

用法：``python scripts/native_ui_smoke.py``（Windows，需要桌面会话）。
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import time
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from checkin_tool import delegate  # noqa: E402
from checkin_tool import gui as gui_mod  # noqa: E402
from checkin_tool.gui import PAGES, CheckinApp  # noqa: E402

PAGES_ORDER = [key for key, _ in PAGES]


def _grab(app: CheckinApp, name: str, out_dir: pathlib.Path) -> str:
    try:
        from PIL import ImageGrab
    except Exception:  # noqa: BLE001 - 没装 Pillow 只影响截图，不影响冒烟结论
        return ""
    app.update_idletasks()
    x, y = app.winfo_rootx(), app.winfo_rooty()
    w, h = app.winfo_width(), app.winfo_height()
    if w < 50 or h < 50:
        return ""
    image = ImageGrab.grab(bbox=(x, y, x + w, y + h))
    path = out_dir / f"native-{name}.png"
    image.save(path)
    return str(path)


def _settle(app: CheckinApp, ms: int = 300) -> None:
    """在 mainloop 里等一小段时间：后台线程的 after(0, 渲染) 要落回主线程。

    只转 ``update()`` 不再 ``after(..., app.update)``：把 update 再排进事件队列
    会形成递归泵，实测能把整个脚本卡死。
    """
    deadline = time.monotonic() + ms / 1000.0
    while time.monotonic() < deadline:
        app.update()
        time.sleep(0.02)


def interaction_checks(app: CheckinApp, problems: list[str]) -> None:
    """把行内按钮真点一遍，但把后端调用换成记账用的假件。

    只验「点这个按钮到底调了谁、带的是哪个账号 ID」——真签到 / 真上传会去打
    供应商接口，冒烟脚本不该有这种副作用。
    """
    calls: list[Any] = []

    class FakeBox:
        def showinfo(self, title, msg):  # noqa: ANN001, ARG002
            calls.append(("msg", title, str(msg)))

        def showerror(self, title, msg):  # noqa: ANN001, ARG002
            calls.append(("err", title, str(msg)))

        def showwarning(self, title, msg):  # noqa: ANN001, ARG002
            calls.append(("warn", title, str(msg)))

        def askyesno(self, *a, **k):  # noqa: ANN002, ARG002
            calls.append(("ask", a))
            return False

    keep = {
        "messagebox": gui_mod.messagebox,
        "run_local_all": gui_mod.run_local_all,
        "refresh_account_credits": gui_mod.refresh_account_credits,
        "ensure_licensed": gui_mod.ensure_licensed,
        "upload": delegate.upload_delegate_accounts,
        "replace": delegate.replace_server_account,
    }

    def fake_run(**kwargs):  # noqa: ANN003
        calls.append(("run_local_all", dict(kwargs)))
        return []

    def fake_refresh(account, **_k):  # noqa: ANN001
        calls.append(("refresh", str(account.get("id"))))
        return {"ok": True, "credits": 1}

    def fake_upload(rows, **k):  # noqa: ANN001
        calls.append(("upload", [str(r.get("id")) for r in rows], k.get("task_enabled")))
        return len(rows)

    def fake_replace(old, new, **_k):  # noqa: ANN001
        calls.append(("replace", old, new))
        return {"ok": True}

    gui_mod.messagebox = FakeBox()  # type: ignore[assignment]
    gui_mod.ensure_licensed = lambda *a, **k: (True, "mock")
    gui_mod.run_local_all = fake_run
    gui_mod.refresh_account_credits = fake_refresh
    delegate.upload_delegate_accounts = fake_upload  # type: ignore[assignment]
    delegate.replace_server_account = fake_replace  # type: ignore[assignment]

    try:
        ids = list(app.tree.get_children())
        first = str(ids[0])
        app.tree.selection_set(first)
        _settle(app, 120)  # <<TreeviewSelect>> 是虚拟事件，要过一轮事件循环才回调
        view = app._selected_view()  # noqa: SLF001
        if (view or {}).get("id") != first:
            problems.append(f"选中行没映射到账号：iid={first} 视图={view}")
        print(f"选中提示={app.lbl_selection.cget('text')!r}")

        calls.clear()
        app.run_one()
        _settle(app)
        run_calls = [c for c in calls if c[0] == "run_local_all"]
        print(f"行内签到 → {run_calls}")
        if not run_calls or run_calls[-1][1].get("account_id") != first:
            problems.append("行内「签到」没有按选中账号跑（或压根没调 run_local_all）")

        stopped = next((str(v.get("id")) for v in app._views if not v.get("enabled", True)), "")  # noqa: SLF001
        if stopped:
            calls.clear()
            app.tree.selection_set(stopped)
            app.run_one()
            _settle(app)
            print(f"已停用账号点签到 → {[c[0] for c in calls]}")
            if any(c[0] == "run_local_all" for c in calls):
                problems.append("已停用账号仍然被派去签到")

        calls.clear()
        app.tree.selection_set(first)
        app.refresh_one_credit()
        _settle(app, 700)
        print(f"行内刷新 → {[c for c in calls if c[0] == 'refresh']}")
        if not any(c[0] == "refresh" for c in calls):
            problems.append("行内「刷新」没调到 refresh_account_credits")

        calls.clear()
        app.upload_delegate(one=True)
        _settle(app, 500)
        print(f"行内上传 → {[c for c in calls if c[0] == 'upload']}")
        if not any(c[0] == "upload" for c in calls):
            problems.append("行内「上传」没走 delegate.upload_delegate_accounts")

        calls.clear()
        # 真点保存按钮，但不写真文件、不启动同步线程：只验「按了之后落盘的键名对不对」
        written: list[dict[str, Any]] = []
        keep_save = gui_mod.save_settings
        keep_start, keep_soon = app.auto_sync.start, app.auto_sync.sync_soon
        keep_sched_start = app.scheduler.start
        gui_mod.save_settings = lambda payload: written.append(dict(payload))
        app.auto_sync.start = lambda: None
        app.auto_sync.sync_soon = lambda: None
        app.scheduler.start = lambda: None
        try:
            app.save_checkin_settings()
        finally:
            gui_mod.save_settings = keep_save
            app.auto_sync.start, app.auto_sync.sync_soon = keep_start, keep_soon
            app.scheduler.start = keep_sched_start
        print(
            f"保存签到设置后输入框={app.var_gap_min.get()}~{app.var_gap_max.get()} "
            f"阈值={app.var_low_credit.get()} 提示={app.lbl_checkin_tip.cget('text')!r}"
        )
        saved = written[-1] if written else {}
        for key in ("run_gap_min_sec", "run_gap_max_sec", "credit_low_threshold", "schedule_hour"):
            if key not in saved:
                problems.append(f"保存后落盘缺 {key}：写盘键名漂了")
        if saved.get("run_gap_min_sec", 0) > saved.get("run_gap_max_sec", 0):
            problems.append("落盘的间隔区间是反的：钳制/交换规则没生效")
    finally:
        gui_mod.messagebox = keep["messagebox"]  # type: ignore[assignment]
        gui_mod.run_local_all = keep["run_local_all"]
        gui_mod.refresh_account_credits = keep["refresh_account_credits"]
        gui_mod.ensure_licensed = keep["ensure_licensed"]
        delegate.upload_delegate_accounts = keep["upload"]  # type: ignore[assignment]
        delegate.replace_server_account = keep["replace"]  # type: ignore[assignment]


def run_checks(app: CheckinApp, out_dir: pathlib.Path) -> None:
    problems: list[str] = []
    shots: dict[str, str] = {}
    app.deiconify()
    app.lift()

    for key in PAGES_ORDER:
        app.show_page(key)
        for _ in range(4):
            app.update()
        page = app._pages.get(key)  # noqa: SLF001 - 冒烟脚本就是来摸内部状态的
        if page is None:
            problems.append(f"页面 {key} 不存在")
            continue
        if page.winfo_ismapped() != 1:
            problems.append(f"页面 {key} 没被 raise 到前台")
        shots[key] = _grab(app, key, out_dir)

    app.show_page("accounts")
    interaction_checks(app, problems)
    app.show_page("accounts")

    rows = list(app.tree.get_children())
    values: list[Any] = [app.tree.item(r, "values") for r in rows]
    stats = [lbl.cget("text") for lbl in app._stat_widgets.get("accounts", [])]  # noqa: SLF001
    print(f"账号行数={len(rows)} 侧栏状态={app.lbl_sched_state.cget('text')!r}")
    print(f"{app.lbl_side_sched_next.cget('text')!r} / {app.lbl_side_today_done.cget('text')!r}")
    print(f"统计卡={stats}")
    for row in values[:3]:
        print(f"行={row}")
    if not rows:
        problems.append("账号列表为空：确认本机确实没有账号，还是取数没回到主线程")
    if any(str(s) in ("", "—") for s in stats):
        problems.append(f"统计卡没算出来：{stats}")

    app.set_provider_filter("workbuddy")
    wb_rows = len(app.tree.get_children())
    wb_stats = [lbl.cget("text") for lbl in app._stat_widgets["accounts"]]  # noqa: SLF001
    print(f"筛选 WB 行数={wb_rows} 统计卡={wb_stats}")
    if wb_rows > len(rows):
        problems.append("筛选后行数比全部还多")
    shots["accounts-wb"] = _grab(app, "accounts-wb", out_dir)
    app.set_provider_filter("")

    print(
        "签到设置输入框="
        f"{app.var_sched_hour.get()}:{app.var_sched_min.get()} "
        f"间隔 {app.var_gap_min.get()}~{app.var_gap_max.get()} "
        f"阈值 {app.var_low_credit.get()} 成长任务 {app.var_wb_mode.get()}"
    )
    print(f"今日看板={app.lbl_today.cget('text')!r}")
    print(f"额度={app.lbl_account_quota.cget('text')!r}")
    print(f"日志行数={int(app.log.index('end-1c').split('.')[0]) - 1 if app.log else 0}")

    for name, path in shots.items():
        print(f"截图 {name}: {path or '未生成（缺 Pillow 或窗口未映射）'}")
    if problems:
        print("问题：")
        for item in problems:
            print(f"  - {item}")
    else:
        print("原生前端冒烟通过")
    app._smoke_problems = problems  # noqa: SLF001
    app._force_quit()  # noqa: SLF001 - 销毁窗口即结束 mainloop


def main() -> int:
    out_dir = pathlib.Path(tempfile.gettempdir()) / "checkin_native_preview"
    out_dir.mkdir(parents=True, exist_ok=True)
    app = CheckinApp()
    app._smoke_problems = []  # noqa: SLF001
    # 2 秒足够三轮后台取数回主线程（账号 / 看板 / 积分记录各自一个线程）
    app.after(2000, lambda: run_checks(app, out_dir))
    # 兜底看门狗：任何一步卡住都别让窗口挂着不退出
    app.after(60000, lambda: (print("看门狗：60 秒没跑完，强制收尾"), app._force_quit()))  # noqa: SLF001
    app.mainloop()
    return 1 if getattr(app, "_smoke_problems", []) else 0


if __name__ == "__main__":
    raise SystemExit(main())
