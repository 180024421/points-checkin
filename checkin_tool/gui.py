# -*- coding: utf-8 -*-
"""积分签到工具 GUI — 参考 Cursor 工具原生简约布局。

标题栏 + Notebook + 底部固定日志 + 状态条；不走炫酷皮肤。
"""

from __future__ import annotations

import threading
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, scrolledtext, simpledialog, ttk
from typing import Any, Callable

from . import __version__, account_store, autostart, credential_store, server_client, vault
from .adapters import traework, workbuddy
from .license_client import check_status, data_root, ensure_licensed, redeem
from .login import login_by_id
from .redact import mask_text
from .scheduler import AutoSyncer, DailyScheduler, clamp_sync_minutes, run_local_all
from .settings import load_settings, save_settings


class CheckinApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"积分签到工具  v{__version__}")
        self.geometry("720x640")
        self.minsize(640, 560)
        self.settings = load_settings()
        self.scheduler = DailyScheduler(log=self.append_log)
        self._tray = None
        self.log: scrolledtext.ScrolledText | None = None
        self.lbl_license_bar: ttk.Label | None = None
        self.lbl_time: ttk.Label | None = None

        self._build_ui()
        # DPAPI 不可用时凭证是明文落盘的，必须让用户看见
        for warning in vault.startup_warnings(data_root()):
            self.append_log(f"[警告] {warning}")
        self.after(0, self.refresh_license)
        self.refresh_accounts()
        self.refresh_today()
        self.refresh_credits_view()
        if self.settings.get("auto_schedule", True):
            self.scheduler.start()
            self.append_log("已启动本机日签调度（早晚双窗口）")
        self.after(2000, self._tick_refresh)
        # 状态/积分自动同步：启动几秒后拉一次，之后按设置里的间隔查积分。
        # 签到/批量登录在直接问供应商，用 _vendor_busy 让它们独占，同步自动让路。
        self._vendor_busy = threading.Event()
        self.auto_sync = AutoSyncer(
            get_settings=lambda: self.settings,
            is_busy=self._vendor_busy.is_set,
            log=self.append_log,
        )
        if self.settings.get("auto_sync", True):
            self.auto_sync.start()
        self._setup_tray()
        self._tick_clock()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        top = ttk.Frame(self, padding=(12, 10, 12, 4))
        top.pack(fill="x")
        ttk.Label(top, text="积分签到工具", font=("Segoe UI", 14, "bold")).pack(side="left")
        ttk.Label(top, text=f"v{__version__}", foreground="gray").pack(side="left", padx=(8, 0))
        self.btn_refresh_all = ttk.Button(top, text="刷新", width=8, command=self._refresh_all)
        self.btn_refresh_all.pack(side="right")

        nb = ttk.Notebook(self, padding=(10, 0, 10, 6))
        nb.pack(fill="both", expand=True)

        self.tab_home = ttk.Frame(nb, padding=10)
        self.tab_accounts = ttk.Frame(nb, padding=10)
        self.tab_today = ttk.Frame(nb, padding=10)
        self.tab_credits = ttk.Frame(nb, padding=10)
        self.tab_license = ttk.Frame(nb, padding=10)
        nb.add(self.tab_home, text="  概览  ")
        nb.add(self.tab_accounts, text="  账号  ")
        nb.add(self.tab_today, text="  今日  ")
        nb.add(self.tab_credits, text="  积分  ")
        nb.add(self.tab_license, text="  设置  ")

        self._build_home()
        self._build_accounts()
        self._build_today()
        self._build_credits()
        self._build_license()

        log_fr = ttk.Frame(self, padding=(10, 0, 10, 4))
        log_fr.pack(fill="both")
        bar = ttk.Frame(log_fr)
        bar.pack(fill="x")
        ttk.Label(bar, text="日志", anchor="w").pack(side="left")
        ttk.Button(bar, text="清理", width=8, command=self.clear_live_logs).pack(side="right")
        self.log = scrolledtext.ScrolledText(
            log_fr, height=8, state="disabled", font=("Consolas", 9), wrap="word"
        )
        self.log.pack(fill="both", expand=True)

        bottom = ttk.Frame(self, padding=(10, 2, 10, 8))
        bottom.pack(fill="x")
        self.lbl_time = ttk.Label(bottom, text="", foreground="gray")
        self.lbl_time.pack(side="left")
        self.lbl_license_bar = ttk.Label(bottom, text="授权未检查", foreground="gray")
        self.lbl_license_bar.pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_home(self) -> None:
        status = ttk.LabelFrame(self.tab_home, text="今日状态", padding=8)
        status.pack(fill="x", pady=(0, 8))
        self.lbl_today = ttk.Label(status, text="…", foreground="#333")
        self.lbl_today.pack(anchor="w")
        self.lbl_license = ttk.Label(status, text="授权：未检查", foreground="#333", wraplength=640)
        self.lbl_license.pack(anchor="w", pady=(6, 0))
        self.lbl_account_quota = ttk.Label(status, text="账号额度：未检查", foreground="#333", wraplength=640)
        self.lbl_account_quota.pack(anchor="w", pady=(6, 0))

        actions = ttk.LabelFrame(self.tab_home, text="常用操作", padding=8)
        actions.pack(fill="x", pady=(0, 8))
        row1 = ttk.Frame(actions)
        row1.pack(fill="x", pady=2)
        for text, cmd in [
            ("本机立即签到", self.run_now),
            ("刷新积分", self.refresh_all_credits),
            ("刷新看板", self.refresh_today),
            ("服务器代跑", self.run_server_now),
        ]:
            ttk.Button(row1, text=text, width=14, command=cmd).pack(side="left", padx=2)

        tip = ttk.LabelFrame(self.tab_home, text="说明", padding=8)
        tip.pack(fill="x")
        ttk.Label(
            tip,
            text=(
                "① 官方客户端登录后到「账号」页采集 / 粘贴 token\n"
                "② 或导入账密后统一登录（优先 API，失败再开浏览器）\n"
                "账密仅本机加密保存；代跑只上传 token。"
            ),
            foreground="gray",
            justify="left",
        ).pack(anchor="w")

    def _build_accounts(self) -> None:
        ops = ttk.LabelFrame(self.tab_accounts, text="入池", padding=8)
        ops.pack(fill="x", pady=(0, 6))
        r1 = ttk.Frame(ops)
        r1.pack(fill="x", pady=2)
        for text, cmd in [
            ("导入账密", self.import_credentials),
            ("统一登录", self.unified_login_all),
            ("管理账密", self.manage_credentials),
            ("采集 WorkBuddy", self.capture_workbuddy),
            ("采集 TraeWork", self.capture_traework),
            ("粘贴 Trae token", self.paste_trae_token),
        ]:
            ttk.Button(r1, text=text, width=14, command=cmd).pack(side="left", padx=2)

        r2 = ttk.Frame(ops)
        r2.pack(fill="x", pady=2)
        for text, cmd in [
            ("本机签到", self.run_now),
            ("上传代跑", self.upload_delegate),
            ("设为本机", lambda: self.set_mode("local")),
            ("设为代跑", lambda: self.set_mode("server")),
            ("删除所选", self.delete_selected),
            ("刷新列表", self.refresh_accounts),
        ]:
            ttk.Button(r2, text=text, width=14, command=cmd).pack(side="left", padx=2)

        cols = (
            "provider",
            "label",
            "run_mode",
            "today_status",
            "last_credits",
            "last_streak",
            "token_expired",
            "last_error",
            "id",
        )
        self.tree = ttk.Treeview(self.tab_accounts, columns=cols, show="headings", height=12)
        headers = {
            "provider": "产品",
            "label": "账号",
            "run_mode": "模式",
            "today_status": "今日",
            "last_credits": "积分",
            "last_streak": "连签",
            "token_expired": "过期",
            "last_error": "错误",
            "id": "ID",
        }
        widths = {
            "provider": 90,
            "label": 120,
            "run_mode": 70,
            "today_status": 80,
            "last_credits": 60,
            "last_streak": 50,
            "token_expired": 50,
            "last_error": 160,
            "id": 70,
        }
        for c in cols:
            self.tree.heading(c, text=headers.get(c, c))
            self.tree.column(c, width=widths.get(c, 80), stretch=(c in ("label", "last_error")))
        self.tree.pack(fill="both", expand=True, pady=(4, 0))

    def _build_today(self) -> None:
        bar = ttk.Frame(self.tab_today)
        bar.pack(fill="x")
        self.lbl_today_tab = ttk.Label(bar, text="今日统计")
        self.lbl_today_tab.pack(side="left")
        ttk.Button(bar, text="刷新", width=8, command=self.refresh_today).pack(side="right")

        paned = ttk.Panedwindow(self.tab_today, orient=tk.HORIZONTAL)
        paned.pack(fill="both", expand=True, pady=6)
        f1, self.lst_done = self._make_labeled_list(paned, "已跑")
        f2, self.lst_pending = self._make_labeled_list(paned, "未跑")
        f3, self.lst_failed = self._make_labeled_list(paned, "失败")
        paned.add(f1)
        paned.add(f2)
        paned.add(f3)

    def _make_labeled_list(self, parent: tk.Widget, title: str) -> tuple[ttk.Frame, tk.Listbox]:
        frm = ttk.Frame(parent, padding=4)
        ttk.Label(frm, text=title).pack(anchor="w")
        lst = tk.Listbox(frm, font=("Consolas", 9))
        lst.pack(fill="both", expand=True)
        return frm, lst

    def _build_credits(self) -> None:
        bar = ttk.Frame(self.tab_credits)
        bar.pack(fill="x")
        ttk.Button(bar, text="刷新", width=8, command=self.refresh_credits_view).pack(side="left", padx=2)
        ttk.Button(bar, text="清理", width=8, command=self.clear_credits).pack(side="left", padx=2)
        cols = ("at", "provider", "account_id", "credits", "streak", "ok", "message")
        self.credit_tree = ttk.Treeview(self.tab_credits, columns=cols, show="headings", height=14)
        headers = {
            "at": "时间",
            "provider": "产品",
            "account_id": "账号ID",
            "credits": "积分",
            "streak": "连签",
            "ok": "成功",
            "message": "说明",
        }
        for c in cols:
            self.credit_tree.heading(c, text=headers.get(c, c))
            self.credit_tree.column(c, width=90 if c != "message" else 180, stretch=(c == "message"))
        self.credit_tree.pack(fill="both", expand=True, pady=(6, 0))

    def _build_license(self) -> None:
        frm = ttk.LabelFrame(self.tab_license, text="授权", padding=8)
        frm.pack(fill="x", pady=(0, 8))
        ttk.Label(frm, text="授权服务").grid(row=0, column=0, sticky="w")
        self.var_base = tk.StringVar(value=str(self.settings.get("license_base_url") or ""))
        ttk.Entry(frm, textvariable=self.var_base).grid(row=0, column=1, sticky="ew", pady=3, padx=(8, 0))
        ttk.Label(frm, text="卡密").grid(row=1, column=0, sticky="w")
        self.var_card = tk.StringVar(value=str(self.settings.get("card_code") or ""))
        ttk.Entry(frm, textvariable=self.var_card, show="*").grid(row=1, column=1, sticky="ew", pady=3, padx=(8, 0))
        frm.columnconfigure(1, weight=1)

        opt = ttk.LabelFrame(self.tab_license, text="本机选项", padding=8)
        opt.pack(fill="x", pady=(0, 8))
        self.var_autostart = tk.BooleanVar(value=bool(self.settings.get("autostart")))
        self.var_evening = tk.BooleanVar(value=bool(self.settings.get("evening_schedule", True)))
        self.var_auto = tk.BooleanVar(value=bool(self.settings.get("auto_schedule", True)))
        self.var_auto_sync = tk.BooleanVar(value=bool(self.settings.get("auto_sync", True)))
        self.var_sync_minutes = tk.StringVar(value=str(self.settings.get("auto_sync_minutes") or 5))
        ttk.Checkbutton(opt, text="开机自启", variable=self.var_autostart).pack(anchor="w")
        ttk.Checkbutton(opt, text="启用早晚调度", variable=self.var_auto).pack(anchor="w")
        ttk.Checkbutton(opt, text="启用晚间补漏", variable=self.var_evening).pack(anchor="w")
        ttk.Checkbutton(opt, text="自动同步服务器状态与积分", variable=self.var_auto_sync).pack(anchor="w")
        sync_row = ttk.Frame(opt)
        sync_row.pack(anchor="w", pady=(4, 0))
        ttk.Label(sync_row, text="同步间隔（分钟）").pack(side="left")
        ttk.Spinbox(
            sync_row, from_=1, to=240, increment=1, width=6, textvariable=self.var_sync_minutes
        ).pack(side="left", padx=(6, 0))

        btns = ttk.Frame(self.tab_license)
        btns.pack(fill="x")
        self.btn_redeem = ttk.Button(btns, text="保存/激活", width=14, command=self.on_redeem)
        self.btn_redeem.pack(side="left", padx=2)
        self.btn_refresh_license = ttk.Button(btns, text="刷新授权", width=14, command=self.refresh_license)
        self.btn_refresh_license.pack(side="left", padx=2)
        ttk.Button(btns, text="清理跑批日志", width=14, command=self.clear_run_logs).pack(side="left", padx=2)

    def _manual_sync_task(self) -> Callable[[], None]:
        """「刷新」「刷新积分」共用同一条同步路径：同一批 token 不被并发使用。"""

        def task() -> None:
            if self._vendor_busy.is_set():
                self.append_log("签到/登录正在执行，请等待其完成后再同步")
                return
            try:
                result = self.auto_sync.sync_once(reason="手动")
            except Exception as exc:  # noqa: BLE001
                self.append_log(f"同步失败：{exc}")
                return
            if not result.get("ok"):
                self.append_log(result.get("message") or "同步失败")

        return task

    def _refresh_all(self) -> None:
        """刷新 = 真的去同步一次（服务器状态 + 本机积分），不只是重读本地文件。"""

        self._run_task_in_background(
            self._manual_sync_task(), [self.btn_refresh_all], "正在同步…", on_done=self._refresh_views
        )
        self.refresh_license()

    def _tick_clock(self) -> None:
        if self.lbl_time is not None:
            self.lbl_time.configure(text=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        self.after(1000, self._tick_clock)

    # ------------------------------------------------------------------ log / refresh
    def append_log(self, msg: str) -> None:
        account_store.append_live_log(msg)
        line = msg.rstrip()

        def _append() -> None:
            if self.log is None:
                return
            self.log.configure(state="normal")
            self.log.insert(tk.END, line + "\n")
            self.log.see(tk.END)
            self.log.configure(state="disabled")

        self.after(0, _append)

    def _run_task_in_background(
        self,
        task_func: Callable[[], Any],
        buttons_to_disable: list[ttk.Button],
        status_message: str = "",
        on_done: Callable[[], None] | None = None,
    ) -> None:
        """后台跑 ``task_func``；``on_done`` 在任务结束后回到主线程执行。

        同步/签到这类要改数据的任务必须用 ``on_done`` 再重读界面，
        否则主线程在任务刚派出去时就抢先渲染了一遍旧数据。
        """

        def worker() -> None:
            for btn in buttons_to_disable:
                self.after(0, lambda b=btn: b.config(state="disabled"))
            if self.lbl_license_bar and status_message:
                self.after(0, lambda s=status_message: self.lbl_license_bar.config(text=s, foreground="gray"))

            try:
                task_func()
            finally:
                if on_done:
                    self.after(0, on_done)
                for btn in buttons_to_disable:
                    self.after(0, lambda b=btn: b.config(state="enabled"))
                if self.lbl_license_bar and status_message:
                    self.after(0, lambda: self.lbl_license_bar.config(text="", foreground="gray")) # Clear status

        threading.Thread(target=worker, daemon=True).start()

    def _tick_refresh(self) -> None:
        try:
            self.refresh_today()
            self.refresh_accounts()
        except Exception as e:
            self.append_log(f"后台刷新异常: {e}")
        self.after(15000, self._tick_refresh)

    def _selected_account_id(self) -> str | None:
        sel = self.tree.selection()
        if not sel:
            return None
        values = self.tree.item(sel[0], "values")
        if not values:
            return None
        return str(values[-1])  # id 在末列

    def refresh_license(self, *, _from_thread: bool = False) -> None:
        if not _from_thread:
            self._run_task_in_background(lambda: self.refresh_license(_from_thread=True), [self.btn_refresh_all], "正在刷新授权状态...")
            return

        self.settings["license_base_url"] = self.var_base.get().strip()
        save_settings(self.settings)
        result = check_status(self.settings, force_online=True)
        text = f"valid={result.get('valid')}  {result.get('message')}"
        self.after(0, lambda:
            self.lbl_license.configure(text="授权：" + text)
        )
        if self.lbl_license_bar is not None:
            valid = bool(result.get("valid"))
            self.after(0, lambda:
                self.lbl_license_bar.configure(
                    text=("● 已授权" if valid else "○ 未授权"),
                    foreground=("#2e8b57" if valid else "gray"),
                )
            )
        self.append_log("授权: " + text)

        # Update account quota display（额度用满时给出升级引导）
        usage = account_store.get_account_usage()
        used = usage.get("used") or 0
        limit = usage.get("limit")
        plan = usage.get("planLabel") or ""
        if limit is None:
            quota_text = f"{used}/不限"
        else:
            detail = "已满，升级套餐可挂载更多" if used >= limit else f"剩余 {usage.get('remain')}"
            quota_text = f"{used}/{limit}（{detail}）"
        if plan:
            quota_text += f" - {plan}"
        if usage.get("contactVerified") is False:
            # 服务端要求代跑前绑定联系邮箱，这里提前提示，别等上传时才报错
            quota_text += " · 代跑邮箱未绑定"

        self.after(0, lambda:
            self.lbl_account_quota.configure(text="账号额度：" + quota_text)
        )

    def on_redeem(self, *, _from_thread: bool = False) -> None:
        if not _from_thread:
            self._run_task_in_background(lambda: self.on_redeem(_from_thread=True), [self.btn_redeem, self.btn_refresh_license], "正在保存并激活授权...")
            return

        self.settings["license_base_url"] = self.var_base.get().strip()
        self.settings["card_code"] = self.var_card.get().strip()
        self.settings["autostart"] = bool(self.var_autostart.get())
        self.settings["auto_schedule"] = bool(self.var_auto.get())
        self.settings["evening_schedule"] = bool(self.var_evening.get())
        self.settings["auto_sync"] = bool(self.var_auto_sync.get())
        self.settings["auto_sync_minutes"] = clamp_sync_minutes(self.var_sync_minutes.get().strip())
        save_settings(self.settings)
        try:
            autostart.set_enabled(bool(self.var_autostart.get()))
        except Exception as exc:
            # except 结束时会解绑 exc，lambda 里直接引用它等于弹窗时才 NameError，先落成普通变量
            tip = f"开机自启设置失败: {exc}"
            self.append_log(tip)
            self.after(0, lambda t=tip: messagebox.showerror("设置失败", t))
        if self.var_card.get().strip():
            result = redeem(self.settings, self.var_card.get())
            # 只取 message：str(result) 里含 ticket，弹窗会把票据显示在屏幕上
            activated = bool(result.get("valid") or result.get("ok"))
            tip = result.get("message") or ("激活成功" if activated else "激活失败")
            self.after(0, lambda: messagebox.showinfo("激活", tip))
        else:
            activated = False
        if self.settings.get("auto_schedule"):
            self.scheduler.start()
        else:
            self.scheduler.stop()
        if self.settings.get("auto_sync", True):
            was_running = self.auto_sync.status().get("running")
            self.auto_sync.start()
            # 线程还活着时 start() 不重置间隔；激活/改完设置都应该马上同步一次
            if activated or not was_running:
                self.auto_sync.sync_soon()
        else:
            self.auto_sync.stop()
        self.after(0, self.refresh_license)

    def refresh_accounts(self) -> None:
        """取数放后台线程：代跑账号和签到记录都要打服务器，HTTP 不能让 Tk 主循环等。"""

        def worker() -> None:
            try:
                today = account_store.today_run_map()
                views = [
                    account_store.public_account_view(row, today)
                    for row in account_store.load_accounts()
                ]
            except Exception as exc:  # noqa: BLE001
                self.append_log(f"刷新账号列表失败：{mask_text(exc, 160)}")
                return
            self.after(0, lambda v=views: self._render_accounts(v))

        threading.Thread(target=worker, daemon=True).start()

    def _render_accounts(self, views: list[dict[str, Any]]) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        for view in views:
            self.tree.insert(
                "",
                tk.END,
                values=(
                    view.get("provider"),
                    view.get("label"),
                    view.get("run_mode"),
                    view.get("today_status"),
                    view.get("last_credits") if view.get("last_credits") is not None else "",
                    view.get("last_streak") if view.get("last_streak") is not None else "",
                    "是" if view.get("token_expired") else "",
                    view.get("last_error") or "",
                    view.get("id"),
                ),
            )

    def refresh_today(self) -> None:
        def worker() -> None:
            try:
                board = account_store.today_board()
            except Exception as exc:  # noqa: BLE001
                self.append_log(f"刷新今日看板失败：{mask_text(exc, 160)}")
                return
            self.after(0, lambda b=board: self._render_today(b))

        threading.Thread(target=worker, daemon=True).start()

    def _render_today(self, board: dict[str, Any]) -> None:
        summary = (
            f"{board['day']}  已跑 {board['done_count']} / "
            f"未跑 {board['pending_count']} / 失败 {board['failed_count']} "
            f"（启用 {board['total_enabled']}）"
        )
        self.lbl_today.configure(text=summary)
        if hasattr(self, "lbl_today_tab"):
            self.lbl_today_tab.configure(text=summary)
        for lst, rows in (
            (self.lst_done, board["done"]),
            (self.lst_pending, board["pending"]),
            (self.lst_failed, board["failed"]),
        ):
            lst.delete(0, tk.END)
            for v in rows:
                lst.insert(
                    tk.END,
                    f"{v.get('provider')} | {v.get('label')} | {v.get('today_status')} | "
                    f"积分={v.get('today_credits') if v.get('today_credits') is not None else v.get('last_credits')}",
                )

    def refresh_credits_view(self) -> None:
        for item in self.credit_tree.get_children():
            self.credit_tree.delete(item)
        for row in account_store.load_credit_history(limit=300):
            self.credit_tree.insert(
                "",
                tk.END,
                values=(
                    row.get("at"),
                    row.get("provider"),
                    row.get("account_id"),
                    row.get("credits") if row.get("credits") is not None else "",
                    row.get("streak") if row.get("streak") is not None else "",
                    row.get("ok"),
                    row.get("message") or "",
                ),
            )

    def clear_live_logs(self) -> None:
        if messagebox.askyesno("确认", "清理实时日志？"):
            account_store.clear_live_logs()
            if self.log is not None:
                self.log.configure(state="normal")
                self.log.delete("1.0", tk.END)
                self.log.configure(state="disabled")

    def clear_run_logs(self) -> None:
        if messagebox.askyesno("确认", "清理签到跑批日志？（今日看板会受影响）"):
            account_store.clear_run_logs()
            self.refresh_today()
            self.refresh_accounts()

    def clear_credits(self) -> None:
        if messagebox.askyesno("确认", "清理积分记录？"):
            account_store.clear_credit_history()
            self.refresh_credits_view()

    # ------------------------------------------------------------------ credentials / capture
    def import_credentials(self) -> None:
        ok, msg = ensure_licensed(self.settings, force_online=True)
        if not ok:
            messagebox.showerror("卡密", msg)
            return
        win = tk.Toplevel(self)
        win.title("导入账密")
        win.geometry("480x340")
        win.transient(self)
        frm = ttk.Frame(win, padding=10)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="产品").grid(row=0, column=0, sticky="w")
        var_provider = tk.StringVar(value="workbuddy")
        ttk.Combobox(
            frm, textvariable=var_provider, values=["workbuddy", "traework"], state="readonly", width=28
        ).grid(row=0, column=1, sticky="w", pady=3)
        ttk.Label(frm, text="账号").grid(row=1, column=0, sticky="w")
        ent_user = ttk.Entry(frm, width=36)
        ent_user.grid(row=1, column=1, sticky="w", pady=3)
        ttk.Label(frm, text="密码").grid(row=2, column=0, sticky="w")
        ent_pass = ttk.Entry(frm, width=36, show="*")
        ent_pass.grid(row=2, column=1, sticky="w", pady=3)
        ttk.Label(frm, text="备注").grid(row=3, column=0, sticky="w")
        ent_label = ttk.Entry(frm, width=36)
        ent_label.grid(row=3, column=1, sticky="w", pady=3)
        var_login_now = tk.BooleanVar(value=True)
        ttk.Checkbutton(frm, text="保存后立即登录", variable=var_login_now).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=6
        )
        ttk.Label(frm, text="批量（每行 provider,username,password[,label]）", foreground="gray").grid(
            row=5, column=0, columnspan=2, sticky="w"
        )
        txt_batch = tk.Text(frm, height=5, width=52, font=("Consolas", 9))
        txt_batch.grid(row=6, column=0, columnspan=2, sticky="ew", pady=4)

        def _save() -> None:
            saved_ids: list[str] = []
            batch = txt_batch.get("1.0", tk.END).strip()
            try:
                if batch:
                    for line in batch.splitlines():
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        parts = [p.strip() for p in line.split(",")]
                        if len(parts) < 3:
                            raise ValueError(f"行格式错误：{line}")
                        row = credential_store.upsert_credential(
                            provider=parts[0],
                            username=parts[1],
                            password=parts[2],
                            label=parts[3] if len(parts) > 3 else "",
                        )
                        saved_ids.append(str(row["id"]))
                else:
                    row = credential_store.upsert_credential(
                        provider=var_provider.get(),
                        username=ent_user.get(),
                        password=ent_pass.get(),
                        label=ent_label.get(),
                    )
                    saved_ids.append(str(row["id"]))
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("导入失败", str(exc), parent=win)
                return
            self.append_log(f"已导入 {len(saved_ids)} 条账密")
            win.destroy()
            if var_login_now.get() and saved_ids:
                self._run_unified_login(saved_ids)

        ttk.Button(frm, text="保存", width=12, command=_save).grid(row=7, column=1, sticky="e", pady=8)

    def manage_credentials(self) -> None:
        win = tk.Toplevel(self)
        win.title("已存账密")
        win.geometry("680x320")
        win.transient(self)
        cols = ("provider", "username", "last_login_ok", "last_login_method", "last_login_error", "id")
        tree = ttk.Treeview(win, columns=cols, show="headings", height=10)
        headers = {
            "provider": "产品",
            "username": "账号",
            "last_login_ok": "成功",
            "last_login_method": "方式",
            "last_login_error": "错误",
            "id": "ID",
        }
        for c in cols:
            tree.heading(c, text=headers.get(c, c))
            tree.column(c, width=90 if c != "last_login_error" else 180, stretch=(c == "last_login_error"))
        tree.pack(fill="both", expand=True, padx=8, pady=8)

        def refresh() -> None:
            tree.delete(*tree.get_children())
            for row in credential_store.load_credentials():
                view = credential_store.public_credential_view(row)
                tree.insert(
                    "",
                    tk.END,
                    values=(
                        view.get("provider"),
                        view.get("username"),
                        view.get("last_login_ok"),
                        view.get("last_login_method") or "",
                        (view.get("last_login_error") or "")[:80],
                        view.get("id"),
                    ),
                )

        def login_selected() -> None:
            sel = tree.selection()
            if not sel:
                messagebox.showinfo("提示", "请先选中账密", parent=win)
                return
            cid = str(tree.item(sel[0], "values")[-1])
            self._run_unified_login([cid])

        def delete_selected() -> None:
            sel = tree.selection()
            if not sel:
                return
            cid = str(tree.item(sel[0], "values")[-1])
            if messagebox.askyesno("确认", "删除该账密？", parent=win):
                credential_store.delete_credential(cid)
                refresh()

        bar = ttk.Frame(win, padding=8)
        bar.pack(fill="x")
        ttk.Button(bar, text="刷新", width=10, command=refresh).pack(side="left", padx=2)
        ttk.Button(bar, text="登录所选", width=10, command=login_selected).pack(side="left", padx=2)
        ttk.Button(bar, text="删除所选", width=10, command=delete_selected).pack(side="left", padx=2)
        refresh()

    def unified_login_all(self) -> None:
        ok, msg = ensure_licensed(self.settings, force_online=True)
        if not ok:
            messagebox.showerror("卡密", msg)
            return
        rows = credential_store.load_credentials()
        if not rows:
            messagebox.showinfo("提示", "还没有导入账密")
            return
        if not messagebox.askyesno("确认", f"对 {len(rows)} 条账密统一登录？"):
            return
        self._run_unified_login([str(r["id"]) for r in rows if r.get("id")])

    def _run_unified_login(self, credential_ids: list[str]) -> None:
        def worker() -> None:
            for cid in credential_ids:
                try:
                    result = login_by_id(cid, headed=True, log=self.append_log)
                    if result.needs_manual and not result.ok:
                        self.append_log("→ 请改用「采集」或「粘贴 Trae token」")
                except Exception as exc:  # noqa: BLE001
                    self.append_log(f"统一登录异常：{exc}")
            self.after(0, self.refresh_accounts)
            self.after(0, self.refresh_today)

        threading.Thread(target=worker, daemon=True).start()
        self.append_log(f"后台统一登录启动，共 {len(credential_ids)} 条…")

    def capture_workbuddy(self) -> None:
        ok, msg = ensure_licensed(self.settings, force_online=True)
        if not ok:
            messagebox.showerror("卡密", msg)
            return
        custom_path = self.settings.get("workbuddy_auth_path")
        if custom_path:
            auth, err = workbuddy.load_local_auth(custom_path)
            auths = [auth] if auth else []
        else:
            auths, err = workbuddy.load_all_local_auths()
        if not auths:
            messagebox.showerror("采集失败", err or "无登录态")
            return
        for auth in auths:
            account_store.upsert_account(
                {
                    "provider": "workbuddy",
                    "label": auth.get("nickname") or auth.get("uid"),
                    "identity": auth.get("uid"),
                    "run_mode": "local",
                    "enabled": True,
                    "token_blob": auth,
                    "last_error": "",
                }
            )
        self.append_log(
            f"已采集 WorkBuddy {len(auths)} 个账号："
            + "、".join(str(a.get("nickname") or a.get("uid")) for a in auths)
        )
        self.refresh_accounts()
        self.refresh_today()

    def paste_trae_token(self) -> None:
        ok, msg = ensure_licensed(self.settings, force_online=True)
        if not ok:
            messagebox.showerror("卡密", msg)
            return
        token = simpledialog.askstring("TraeWork token", "粘贴 cloudide / Bearer token：", show="*")
        if not token or not token.strip():
            return
        headers = traework.load_device_headers()
        auth, _ = traework.load_local_auth(self.settings.get("traework_user_dir") or None)
        user_id = simpledialog.askstring("可选", "账号标识（user_id，可空）：") or ""
        blob = {
            "provider": "traework",
            "token": token.strip(),
            "access_token": token.strip(),
            "machine_id": (auth or {}).get("machine_id") or headers.get("X-Machine-Id") or "",
            "device_id": (auth or {}).get("device_id") or headers.get("X-Device-Id") or "",
            "user_id": user_id or (auth or {}).get("user_id") or "",
            "ug_api_base": self.settings.get("traework_ug_api_base") or "",
            "token_hint": "已手工粘贴（内容已隐藏）",
        }
        if not blob["machine_id"] or not blob["device_id"]:
            messagebox.showerror("缺少设备头", "请先打开一次 TraeWork 桌面端")
            return
        account_store.upsert_account(
            {
                "provider": "traework",
                "label": blob.get("user_id") or "traework-manual",
                "identity": blob.get("user_id") or blob["token"][-12:],
                "run_mode": "local",
                "enabled": True,
                "token_blob": blob,
            }
        )
        self.append_log("已手工导入 TraeWork token")
        self.refresh_accounts()

    def capture_traework(self) -> None:
        ok, msg = ensure_licensed(self.settings, force_online=True)
        if not ok:
            messagebox.showerror("卡密", msg)
            return
        auths, err = traework.load_all_local_auths(self.settings.get("traework_user_dir") or None)
        todo = [a for a in auths if a.get("token")]
        if not todo:
            messagebox.showwarning(
                "无法自动解密 token",
                (err or "未找到可解密的 iCubeAuthInfo")
                + "\n请在 Trae CN 里登录一次（登录瞬间会自动入库），或使用「粘贴 Trae token」。",
            )
            return
        tags = traework.user_tags()
        saved: list[str] = []
        for auth in todo:
            blocked = traework._blocked_region(auth)
            if blocked:
                self.append_log(f"跳过 {auth.get('user_id')}：区域 {blocked}，签到仅支持 CN 区")
            if self.settings.get("traework_ug_api_base"):
                auth["ug_api_base"] = self.settings["traework_ug_api_base"]
            uid = str(auth.get("user_id") or "")
            if uid and tags.get(uid):
                # 下面 upsert 的 token_blob 就是 auth，标签写在这里即可
                auth["user_tag"] = tags[uid]
            account_store.upsert_account(
                {
                    "provider": "traework",
                    "label": uid or auth.get("nickname") or "traework",
                    "identity": uid or auth.get("auth_key") or "traework",
                    "run_mode": "local",
                    "enabled": not blocked,
                    "token_blob": auth,
                    "last_error": f"区域 {blocked}，签到仅支持 CN 区，已停用" if blocked else "",
                }
            )
            saved.append(uid or "未知")
        known = traework.known_user_ids()
        collected = {str(a.get("user_id") or "") for a in todo}
        missing = [u for u in known if u not in collected]
        self.append_log(
            f"已采集 TraeWork {len(todo)} 个账号：{', '.join(saved)}"
            + (f"；另有 {len(missing)} 个历史账号 token 已被覆盖，需重新登录一次" if missing else "")
        )
        self.refresh_accounts()

    # ------------------------------------------------------- 代挂额度 / 联系邮箱
    def _bind_contact_flow(self) -> bool:
        """弹窗引导「绑定邮箱 → 输入验证码」，返回是否绑定成功。**可跳过**。"""
        email = simpledialog.askstring(
            "联系邮箱",
            "绑定联系邮箱后，账号异常时除站内提醒外还能收到邮件通知（不绑定也能代跑）：",
            parent=self,
        )
        if not email:
            return False
        result = server_client.bind_contact(email)
        if not result.get("ok"):
            messagebox.showerror("绑定失败", result.get("message") or "服务器拒绝了绑定请求")
            return False
        code = simpledialog.askstring("邮箱验证码", "验证码已发送到该邮箱，请输入：", parent=self)
        if not code:
            return False
        checked = server_client.verify_contact(code)
        if not checked.get("ok"):
            messagebox.showerror("验证失败", checked.get("message") or "验证码不正确或已过期")
            return False
        messagebox.showinfo("完成", "联系邮箱已绑定")
        return True

    def _bind_hint(self) -> None:
        """未绑定联系邮箱时引导一次（见 ``account_store.contact_notice``）。

        软引导：只记一句提示并询问是否现在绑定，**无论用户选什么都不中断代跑** ——
        服务端早已把「先绑邮箱」的硬校验删掉（run-jane c1f0d49），本机不该再造门槛。
        """
        try:
            notice = account_store.contact_notice(force=False)
        except Exception as exc:  # noqa: BLE001 - 引导本身出错不能影响主流程
            self.append_log(f"读取邮箱绑定状态失败：{exc}")
            return
        if not notice:
            return
        self.append_log(notice)
        if messagebox.askyesno("联系邮箱", notice + "\n\n现在要绑定吗？"):
            self._bind_contact_flow()

    def set_mode(self, mode: str) -> None:
        account_id = self._selected_account_id()
        if not account_id:
            messagebox.showinfo("提示", "请先选中账号")
            return
        result = account_store.set_run_mode(account_id, mode)
        self.append_log(str(result.get("message") or ""))
        if not result.get("ok"):
            messagebox.showerror("模式", result.get("message") or "失败")
        if mode == "server":
            self._bind_hint()
        self.refresh_accounts()

    def delete_selected(self) -> None:
        account_id = self._selected_account_id()
        if not account_id:
            return
        if not messagebox.askyesno("确认", "删除该账号？服务器上的代跑记录也会一并删除。"):
            return
        result = account_store.delete_account_with_server(account_id, log=self.append_log)
        if not result.get("ok"):
            messagebox.showerror("删除", result.get("message") or "删除失败")
        self.refresh_accounts()
        self.refresh_today()

    def run_now(self) -> None:
        def worker() -> None:
            self._vendor_busy.set()  # 自动同步让路，别在签到时并发查同一个 token
            try:
                results = run_local_all(require_license=True, log=self.append_log)
                self.append_log(f"本机签到完成，共 {len(results)} 条")
            finally:
                self._vendor_busy.clear()
                self.after(0, self.refresh_accounts)
                self.after(0, self.refresh_today)
                self.after(0, self.refresh_credits_view)

        threading.Thread(target=worker, daemon=True).start()

    def refresh_all_credits(self) -> None:
        """「刷新积分」与「刷新」同一条路径：共用串行槽和限速，
        否则两个入口能对同一批账号并发问供应商。"""

        self._run_task_in_background(
            self._manual_sync_task(), [self.btn_refresh_all], "正在刷新积分…", on_done=self._refresh_views
        )

    def _refresh_views(self) -> None:
        """重读界面：必须在同步任务结束后再调，提前读只会显示同步前的旧数据。"""
        self.refresh_accounts()
        self.refresh_today()
        self.refresh_credits_view()

    def upload_delegate(self) -> None:
        ok, msg = ensure_licensed(self.settings, force_online=True)
        if not ok:
            messagebox.showerror("卡密", msg)
            return
        self._bind_hint()
        account_id = self._selected_account_id()
        accounts = account_store.load_accounts()
        targets = [a for a in accounts if (not account_id or str(a.get("id")) == account_id)]

        def worker() -> None:
            for account in targets:
                blob = account.get("token_blob") or {}
                if not blob.get("token") and not blob.get("access_token"):
                    self.append_log(f"跳过无 token 账号 {account.get('id')}")
                    continue
                account["run_mode"] = "server"
                if str(account.get("provider")) == "workbuddy":
                    # 告诉服务器：这个账号代跑时要不要顺带做成长任务
                    account["task_enabled"] = self.settings.get("workbuddy_task_mode") == "server"
                account_store.upsert_account(account)
                result = server_client.sync_server_blob(account, log=self.append_log, force=True)
                if not result:
                    self.append_log(f"上传代跑 {account.get('provider')}: 无可用凭证，已跳过")
            self.after(0, self.refresh_accounts)

        threading.Thread(target=worker, daemon=True).start()

    def run_server_now(self) -> None:
        def worker() -> None:
            result = server_client.run_now_server()
            self.append_log(f"服务器代跑触发: {result}")
            runs = server_client.today_runs()
            self.append_log(f"代跑今日结果: {runs}")
            self.after(0, self.refresh_today)

        threading.Thread(target=worker, daemon=True).start()

    def _setup_tray(self) -> None:
        if not self.settings.get("minimize_to_tray", True):
            return
        try:
            import pystray
            from PIL import Image, ImageDraw
        except Exception:
            self.append_log("未安装 pystray/Pillow，托盘不可用")
            return

        image = Image.new("RGB", (64, 64), color=(80, 80, 80))
        draw = ImageDraw.Draw(image)
        draw.rectangle((18, 18, 46, 46), fill=(240, 240, 240))

        def on_show(icon, item):  # noqa: ARG001
            self.after(0, self.deiconify)

        def on_run(icon, item):  # noqa: ARG001
            self.after(0, self.run_now)

        def on_quit(icon, item):  # noqa: ARG001
            icon.stop()
            self.after(0, self._force_quit)

        menu = pystray.Menu(
            pystray.MenuItem("显示窗口", on_show),
            pystray.MenuItem("立即签到", on_run),
            pystray.MenuItem("退出", on_quit),
        )
        self._tray = pystray.Icon("CheckinTool", image, "积分签到工具", menu)
        threading.Thread(target=self._tray.run, daemon=True).start()

    def _force_quit(self) -> None:
        self.scheduler.stop()
        self.auto_sync.stop()
        try:
            account_store.flush_live_logs()  # 实时日志缓冲落盘，退出前补一次
        except Exception as e:
            print(f"Error flushing live log: {e}")
        if self._tray:
            try:
                self._tray.stop()
            except Exception as e:
                print(f"Error stopping tray icon: {e}")
        self.destroy()

    def on_close(self) -> None:
        if self.settings.get("minimize_to_tray", True) and self._tray is not None:
            self.withdraw()
            self.append_log("已最小化到托盘")
            return
        self._force_quit()


def main() -> None:
    app = CheckinApp()
    app.mainloop()


if __name__ == "__main__":
    main()
