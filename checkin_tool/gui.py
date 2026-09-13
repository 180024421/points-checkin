# -*- coding: utf-8 -*-
"""积分签到工具 GUI — 参考 Cursor 工具原生简约布局。

标题栏 + Notebook + 底部固定日志 + 状态条；不走炫酷皮肤。
"""

from __future__ import annotations

import threading
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, scrolledtext, simpledialog, ttk
from typing import Any

from . import __version__, account_store, autostart, credential_store, server_client
from .adapters import traework, workbuddy
from .license_client import check_status, ensure_licensed, redeem
from .login import login_by_id
from .scheduler import DailyScheduler, refresh_account_credits, run_local_all
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
        self.refresh_license()
        self.refresh_accounts()
        self.refresh_today()
        self.refresh_credits_view()
        if self.settings.get("auto_schedule", True):
            self.scheduler.start()
            self.append_log("已启动本机日签调度（早晚双窗口）")
        self.after(2000, self._tick_refresh)
        self._setup_tray()
        self._tick_clock()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        top = ttk.Frame(self, padding=(12, 10, 12, 4))
        top.pack(fill="x")
        ttk.Label(top, text="积分签到工具", font=("Segoe UI", 14, "bold")).pack(side="left")
        ttk.Label(top, text=f"v{__version__}", foreground="gray").pack(side="left", padx=(8, 0))
        ttk.Button(top, text="刷新", width=8, command=self._refresh_all).pack(side="right")

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
        ttk.Checkbutton(opt, text="开机自启", variable=self.var_autostart).pack(anchor="w")
        ttk.Checkbutton(opt, text="启用早晚调度", variable=self.var_auto).pack(anchor="w")
        ttk.Checkbutton(opt, text="启用晚间补漏", variable=self.var_evening).pack(anchor="w")

        btns = ttk.Frame(self.tab_license)
        btns.pack(fill="x")
        ttk.Button(btns, text="保存/激活", width=14, command=self.on_redeem).pack(side="left", padx=2)
        ttk.Button(btns, text="刷新授权", width=14, command=self.refresh_license).pack(side="left", padx=2)
        ttk.Button(btns, text="清理跑批日志", width=14, command=self.clear_run_logs).pack(side="left", padx=2)

    def _refresh_all(self) -> None:
        self.refresh_license()
        self.refresh_accounts()
        self.refresh_today()
        self.refresh_credits_view()
        self.append_log("已刷新")

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

    def _tick_refresh(self) -> None:
        try:
            self.refresh_today()
            self.refresh_accounts()
        except Exception:
            pass
        self.after(15000, self._tick_refresh)

    def _selected_account_id(self) -> str | None:
        sel = self.tree.selection()
        if not sel:
            return None
        values = self.tree.item(sel[0], "values")
        if not values:
            return None
        return str(values[-1])  # id 在末列

    def refresh_license(self) -> None:
        self.settings["license_base_url"] = self.var_base.get().strip()
        save_settings(self.settings)
        result = check_status(self.settings, force_online=True)
        text = f"valid={result.get('valid')}  {result.get('message')}"
        self.lbl_license.configure(text="授权：" + text)
        if self.lbl_license_bar is not None:
            valid = bool(result.get("valid"))
            self.lbl_license_bar.configure(
                text=("● 已授权" if valid else "○ 未授权"),
                foreground=("#2e8b57" if valid else "gray"),
            )
        self.append_log("授权: " + text)

    def on_redeem(self) -> None:
        self.settings["license_base_url"] = self.var_base.get().strip()
        self.settings["card_code"] = self.var_card.get().strip()
        self.settings["autostart"] = bool(self.var_autostart.get())
        self.settings["auto_schedule"] = bool(self.var_auto.get())
        self.settings["evening_schedule"] = bool(self.var_evening.get())
        save_settings(self.settings)
        try:
            autostart.set_enabled(bool(self.var_autostart.get()))
        except Exception as exc:
            self.append_log(f"开机自启设置失败: {exc}")
        if self.var_card.get().strip():
            result = redeem(self.settings, self.var_card.get())
            messagebox.showinfo("激活", result.get("message") or str(result))
        if self.settings.get("auto_schedule"):
            self.scheduler.start()
        else:
            self.scheduler.stop()
        self.refresh_license()

    def refresh_accounts(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        today = account_store.today_run_map()
        for row in account_store.load_accounts():
            view = account_store.public_account_view(row, today)
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
        board = account_store.today_board()
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
        auth, err = workbuddy.load_local_auth(self.settings.get("workbuddy_auth_path") or None)
        if err or not auth:
            messagebox.showerror("采集失败", err or "无登录态")
            return
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
        self.append_log(f"已采集 WorkBuddy：{auth.get('nickname') or auth.get('uid')}")
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
            if self.settings.get("traework_ug_api_base"):
                auth["ug_api_base"] = self.settings["traework_ug_api_base"]
            uid = str(auth.get("user_id") or "")
            if uid and tags.get(uid):
                auth["user_tag"] = tags[uid]
            account_store.upsert_account(
                {
                    "provider": "traework",
                    "label": uid or auth.get("nickname") or "traework",
                    "identity": uid or auth.get("auth_key") or "traework",
                    "run_mode": "local",
                    "enabled": True,
                    "token_blob": auth,
                    "last_error": "",
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

    def set_mode(self, mode: str) -> None:
        account_id = self._selected_account_id()
        if not account_id:
            messagebox.showinfo("提示", "请先选中账号")
            return
        accounts = account_store.load_accounts()
        for row in accounts:
            if str(row.get("id")) == account_id:
                row["run_mode"] = mode
                break
        account_store.save_accounts(accounts)
        self.refresh_accounts()

    def delete_selected(self) -> None:
        account_id = self._selected_account_id()
        if not account_id:
            return
        if messagebox.askyesno("确认", "删除本地账号记录？"):
            account_store.delete_account(account_id)
            self.refresh_accounts()
            self.refresh_today()

    def run_now(self) -> None:
        def worker() -> None:
            results = run_local_all(require_license=True, log=self.append_log)
            self.after(0, self.refresh_accounts)
            self.after(0, self.refresh_today)
            self.after(0, self.refresh_credits_view)
            self.append_log(f"本机签到完成，共 {len(results)} 条")

        threading.Thread(target=worker, daemon=True).start()

    def refresh_all_credits(self) -> None:
        def worker() -> None:
            for account in account_store.load_accounts():
                if not account.get("enabled", True):
                    continue
                info = refresh_account_credits(account)
                self.append_log(
                    f"积分查询 {account.get('provider')}/{account.get('label')}: "
                    f"{info.get('message') or info}"
                )
            self.after(0, self.refresh_accounts)
            self.after(0, self.refresh_credits_view)

        threading.Thread(target=worker, daemon=True).start()

    def upload_delegate(self) -> None:
        ok, msg = ensure_licensed(self.settings, force_online=True)
        if not ok:
            messagebox.showerror("卡密", msg)
            return
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
        if self._tray:
            try:
                self._tray.stop()
            except Exception:
                pass
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
