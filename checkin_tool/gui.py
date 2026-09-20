# -*- coding: utf-8 -*-
"""积分签到工具 原生窗口（tkinter 回退前端）。

与 pywebview 端 ``ui/index.html`` 保持同一套信息架构：左侧导航 +
概览 / 账号管理 / 今日签到 / 签到记录 / 签到设置 / 系统设置 / 使用说明，
平台筛选、统计卡口径、行内操作和设置项都一一对应。

两条前端只负责「取参数 + 回显」，跑批与写操作都在共用模块里：
``scheduler.run_local_all`` / ``scheduler.refresh_account_credits`` /
``delegate.upload_delegate_accounts`` / ``delegate.replace_server_account`` /
``settings.ordered_gap``。界面各写一套钳制规则是漂移的开始（网页端曾经把
``0`` 当成「没填」退回默认值），所以这里一律走 settings 里那份。
"""

from __future__ import annotations

import os
import threading
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, scrolledtext, simpledialog, ttk
from typing import Any, Callable

from . import (
    __version__,
    account_store,
    autostart,
    credential_store,
    delegate,
    server_client,
    vault,
)
from .adapters import traework, workbuddy
from .license_client import check_status, data_root, ensure_licensed, redeem
from .login import login_by_id
from .redact import mask_text
from .scheduler import (
    AutoSyncer,
    DailyScheduler,
    clamp_sync_minutes,
    credit_slot_busy,
    refresh_account_credits,
    refresh_server_credentials,
    release_credit_slot,
    run_local_all,
    run_workbuddy_tasks_all,
    try_acquire_credit_slot,
)
from .settings import load_settings, parse_int_field, save_settings

# 与 index.html 的 data-pf / PROVIDER_LABEL 一一对应；口径不一致会出现
# 「网页筛出 6 个、原生筛出 5 个」这种没法解释的差值。
PROVIDER_LABEL = {"traework": "TRAE", "workbuddy": "WB"}
PROVIDER_FILTERS: tuple[tuple[str, str], ...] = (
    ("", "全部"),
    ("traework", "TRAE"),
    ("workbuddy", "WB"),
)
PAGES: tuple[tuple[str, str], ...] = (
    ("home", "概览"),
    ("accounts", "账号管理"),
    ("today", "今日签到"),
    ("credits", "签到记录"),
    ("checkin", "签到设置"),
    ("settings", "系统设置"),
    ("help", "使用说明"),
)
NAV_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("导航", ("home", "accounts", "today", "credits")),
    ("配置", ("checkin", "settings", "help")),
)

COL_BG = "#ffffff"
COL_SIDE = "#f5f6f8"
COL_LINE = "#dfe2e7"
COL_MUTED = "#6b7280"
COL_TEXT = "#1f2328"
COL_OK = "#1a7f37"
COL_BAD = "#c0392b"
COL_WARN = "#b26a00"
COL_NAV_ACTIVE = "#e6edfb"
COL_NAV_HOVER = "#eceff4"


def provider_label(provider: Any) -> str:
    return PROVIDER_LABEL.get(str(provider or "").lower(), str(provider or "其他"))


def classify_today(status: Any) -> str:
    """今日状态 → ok / bad / 其它（待跑）。与网页端 stCls 同一判据。"""
    text = str(status or "未跑")
    if text == "失败":
        return "bad"
    return "ok" if text.startswith("已") else ""


def account_stats(rows: list[dict[str, Any]]) -> tuple[int, int, int, int]:
    """账号总数 / 今日已签 / 今日待签 / 异常·过期。与 ``renderAccStats`` 同口径。"""
    done = 0
    bad = 0
    for row in rows:
        if classify_today(row.get("today_status")) == "ok":
            done += 1
        if row.get("last_error") or row.get("token_expired") or str(
            row.get("today_status")
        ) == "失败" or not row.get("enabled", True):
            bad += 1
    return len(rows), done, len(rows) - done, bad


def local_account(row: dict[str, Any]) -> bool:
    """这一行本机跑不跑：只在服务器存在的记录、以及标成代跑的行都不跑。"""
    return row.get("source") != "server" and str(row.get("run_mode") or "local") != "server"


def low_credit(row: dict[str, Any], threshold: int) -> bool:
    """积分橙色提醒：只对 WorkBuddy 有效，且积分确实取到过（None 不当 0）。"""
    if str(row.get("provider")) != "workbuddy" or threshold <= 0:
        return False
    raw = row.get("last_credits")
    if raw is None:
        return False
    try:
        return float(raw) < threshold
    except (TypeError, ValueError):
        # 积分列偶尔是 "1,234" 这种带分隔的字符串：宁可不下结论也别把整表刷崩
        return False


def result_text(row: dict[str, Any]) -> str:
    return (
        row.get("last_error")
        or row.get("today_message")
        or (f"最近成功 {str(row.get('last_ok_at'))[5:16]}" if row.get("last_ok_at") else "—")
    )


def checkin_settings_payload(values: dict[str, Any]) -> dict[str, Any]:
    """从输入框原始值算出「签到设置」要落盘的字段（纯函数，便于测试）。

    ``values`` 用 var 名索引；空值 / 脏值退回默认，``0`` 保留为合法值。
    """
    gap_min, gap_max = gap_range(values.get("gap_min"), values.get("gap_max"))
    return {
        "auto_schedule": bool(values.get("auto")),
        "schedule_hour": parse_int_field(values.get("sched_hour"), "schedule_hour"),
        "schedule_minute": parse_int_field(values.get("sched_min"), "schedule_minute"),
        "evening_schedule": bool(values.get("evening")),
        "evening_hour": parse_int_field(values.get("ev_hour"), "evening_hour"),
        "evening_minute": parse_int_field(values.get("ev_min"), "evening_minute"),
        "run_gap_min_sec": gap_min,
        "run_gap_max_sec": gap_max,
        "credit_low_threshold": parse_int_field(values.get("low_credit"), "credit_low_threshold"),
        "workbuddy_task_mode": str(values.get("wb_mode") or "off"),
        "workbuddy_chat_tasks": bool(values.get("wb_chat")),
        "auto_sync": bool(values.get("auto_sync")),
        "auto_sync_minutes": clamp_sync_minutes(values.get("sync_minutes")),
    }


def gap_range(lo_raw: Any, hi_raw: Any) -> tuple[int, int]:
    low = parse_int_field(lo_raw, "run_gap_min_sec")
    high = parse_int_field(hi_raw, "run_gap_max_sec")
    return (low, high) if low <= high else (high, low)


class CheckinApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"积分签到工具  v{__version__}")
        self.geometry("1120x760")
        self.minsize(940, 620)
        self.settings = load_settings()
        self.scheduler = DailyScheduler(log=self.append_log)
        self._tray = None
        # 签到/批量登录在直接问供应商，用 _vendor_busy 让自动同步让路；
        # 必须在建界面之前建：按钮回调和后台线程都可能先摸到它。
        self._vendor_busy = threading.Event()
        self.log: scrolledtext.ScrolledText | None = None
        self.lbl_license_bar: ttk.Label | None = None
        self.lbl_time: ttk.Label | None = None
        # 列表数据只在后台线程取，主线程渲染时读这份缓存（含被筛掉的行）
        self._views: list[dict[str, Any]] = []
        self._provider_filter = ""
        self._page = "home"
        self._pages: dict[str, ttk.Frame] = {}
        self._nav: dict[str, tk.Label] = {}
        # 额度三态 / 统计卡上的每个数字都要能被两处（概览 + 账号页）同时刷新
        self._stat_widgets: dict[str, list[ttk.Label]] = {}

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
        self._refresh_scheduler_status()
        self.after(2000, self._tick_refresh)
        # 状态/积分自动同步：启动几秒后拉一次，之后按设置里的间隔查积分
        self.auto_sync = AutoSyncer(
            get_settings=lambda: self.settings,
            is_busy=self._vendor_busy.is_set,
            log=self.append_log,
        )
        if self.settings.get("auto_sync", True):
            self.auto_sync.start()
        self._setup_tray()
        self._tick_clock()

    # ------------------------------------------------------------------ UI 外壳
    def _build_ui(self) -> None:
        style = ttk.Style(self)
        style.configure("Card.TLabelframe", padding=8)
        style.configure("Card.TLabelframe.Label", font=("Segoe UI", 10, "bold"))
        style.configure("StatValue.TLabel", font=("Segoe UI", 17, "bold"), background=COL_BG)
        style.configure("StatKey.TLabel", foreground=COL_MUTED, background=COL_BG)
        style.configure("Muted.TLabel", foreground=COL_MUTED)
        style.configure("Ok.TLabel", foreground=COL_OK)
        style.configure("Bad.TLabel", foreground=COL_BAD)
        style.configure("Head.TLabel", font=("Segoe UI", 13, "bold"))

        top = ttk.Frame(self, padding=(14, 10, 14, 6))
        top.pack(side="top", fill="x")
        ttk.Label(top, text="积分签到工具", style="Head.TLabel").pack(side="left")
        ttk.Label(top, text=f"v{__version__} · 原生版", foreground=COL_MUTED).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(top, text="立即全部签到", command=self.run_now).pack(side="right", padx=(6, 0))
        self.btn_refresh_all = ttk.Button(top, text="同步", command=self._refresh_all)
        self.btn_refresh_all.pack(side="right", padx=(6, 0))

        status = ttk.Frame(self, padding=(10, 2, 10, 6))
        status.pack(side="bottom", fill="x")
        self.lbl_time = ttk.Label(status, text="", foreground=COL_MUTED)
        self.lbl_time.pack(side="left")
        self.lbl_license_bar = ttk.Label(status, text="授权未检查", foreground=COL_MUTED)
        self.lbl_license_bar.pack(side="right")

        log_fr = ttk.Frame(self, padding=(10, 0, 10, 4))
        log_fr.pack(side="bottom", fill="x")
        bar = ttk.Frame(log_fr)
        bar.pack(fill="x")
        ttk.Label(bar, text="日志", anchor="w").pack(side="left")
        ttk.Button(bar, text="清理", width=6, command=self.clear_live_logs).pack(side="right")
        self.log = scrolledtext.ScrolledText(
            log_fr, height=7, state="disabled", font=("Consolas", 9), wrap="word"
        )
        self.log.pack(fill="both")

        body = tk.Frame(self, bg=COL_BG)
        body.pack(side="top", fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        self._build_sidebar(body)
        host = tk.Frame(body, bg=COL_BG)
        host.grid(row=0, column=1, sticky="nsew")
        host.columnconfigure(0, weight=1)
        host.rowconfigure(0, weight=1)
        self._page_host = host

        self._build_home()
        self._build_accounts()
        self._build_today()
        self._build_credits()
        self._build_checkin()
        self._build_settings()
        self._build_help()
        self.show_page("home")

        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_sidebar(self, parent: tk.Frame) -> None:
        """左侧导航：与 index.html 的 .sidebar 同构（导航分组 + 平台筛选 + 状态脚注）。

        布局用 pack 而不是 grid：状态脚注要贴底，靠 ``pack(side="bottom")``
        先占位即可，不必给导航项猜行号。
        """
        side = tk.Frame(
            parent, bg=COL_SIDE, width=186, highlightbackground=COL_LINE, highlightthickness=1
        )
        side.grid(row=0, column=0, sticky="ns")
        side.grid_propagate(False)

        foot = tk.Frame(side, bg=COL_SIDE)
        foot.pack(side="bottom", fill="x", padx=12, pady=(0, 12))
        self.lbl_sched_state = tk.Label(
            foot, text="● 状态检测中", bg=COL_SIDE, fg=COL_MUTED,
            font=("Segoe UI", 9), anchor="w", justify="left",
        )
        self.lbl_sched_state.pack(anchor="w")
        for key, text in (
            ("sched_next", "下次签到 —"),
            ("today_done", "今日已签 —"),
            ("lic", "授权：未检查"),
        ):
            lbl = tk.Label(foot, text=text, bg=COL_SIDE, fg=COL_MUTED,
                           font=("Segoe UI", 9), anchor="w", justify="left", wraplength=160)
            lbl.pack(anchor="w", pady=(2, 0))
            setattr(self, f"lbl_side_{key}", lbl)
        tk.Label(foot, text=f"版本 v{__version__}", bg=COL_SIDE, fg=COL_MUTED,
                 font=("Segoe UI", 8)).pack(anchor="w", pady=(6, 0))

        for caption, keys in NAV_GROUPS:
            tk.Label(side, text=caption, bg=COL_SIDE, fg=COL_MUTED,
                     font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=12, pady=(12, 2))
            for key in keys:
                title = next(t for k, t in PAGES if k == key)
                lbl = tk.Label(
                    side,
                    text=f"    {title}",
                    anchor="w",
                    bg=COL_SIDE,
                    fg=COL_TEXT,
                    font=("Segoe UI", 10),
                    cursor="hand2",
                )
                lbl.pack(fill="x", pady=1)
                lbl.bind("<Button-1>", lambda _e, k=key: self.show_page(k))
                lbl.bind("<Enter>", lambda _e, k=key: self._hover_nav(k, True))
                lbl.bind("<Leave>", lambda _e, k=key: self._hover_nav(k, False))
                self._nav[key] = lbl

        tk.Label(side, text="平台筛选", bg=COL_SIDE, fg=COL_MUTED,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=12, pady=(14, 2))
        seg = tk.Frame(side, bg=COL_SIDE)
        seg.pack(anchor="w", padx=10)
        self._filter_lbls: dict[str, tk.Label] = {}
        for value, text in PROVIDER_FILTERS:
            lbl = tk.Label(
                seg,
                text=text,
                anchor="center",
                width=5,
                bg=COL_BG,
                fg=COL_MUTED,
                font=("Segoe UI", 9),
                highlightbackground=COL_LINE,
                highlightthickness=1,
                cursor="hand2",
                padx=6,
                pady=3,
            )
            lbl.pack(side="left", padx=1)
            lbl.bind("<Button-1>", lambda _e, v=value: self.set_provider_filter(v))
            self._filter_lbls[value] = lbl
        self._apply_nav_style()

    def _hover_nav(self, key: str, entering: bool) -> None:
        lbl = self._nav.get(key)
        if lbl is None or key == self._page:
            return
        lbl.configure(background=COL_NAV_HOVER if entering else COL_SIDE)

    def _apply_nav_style(self) -> None:
        for key, lbl in self._nav.items():
            active = key == self._page
            lbl.configure(
                background=COL_NAV_ACTIVE if active else COL_SIDE,
                foreground="#1b4dbb" if active else COL_TEXT,
                font=("Segoe UI", 10, "bold" if active else "normal"),
            )
        for value, lbl in self._filter_lbls.items():
            active = value == self._provider_filter
            lbl.configure(
                background=COL_NAV_ACTIVE if active else COL_BG,
                foreground="#1b4dbb" if active else COL_MUTED,
            )

    def show_page(self, key: str) -> None:
        self._page = key
        self._pages[key].tkraise()
        self._apply_nav_style()

    def set_provider_filter(self, value: str) -> None:
        """侧栏平台筛选：只影响账号页的列表与统计卡，数据源仍是同一份缓存。"""
        self._provider_filter = value
        self._apply_nav_style()
        self._render_accounts()
        if value:
            self.show_page("accounts")

    def _visible_views(self) -> list[dict[str, Any]]:
        if not self._provider_filter:
            return list(self._views)
        return [v for v in self._views if str(v.get("provider") or "").lower() == self._provider_filter]

    def _make_page(self, key: str) -> ttk.Frame:
        page = ttk.Frame(self._page_host, padding=(14, 10, 14, 10))
        page.grid(row=0, column=0, sticky="nsew")
        self._pages[key] = page
        return page

    def _stat_cards(self, parent: ttk.Frame, group: str) -> None:
        """四个统计卡（账号总数 / 今日已签 / 今日待签 / 异常·过期）。"""
        box = ttk.Frame(parent)
        box.pack(fill="x", pady=(0, 8))
        titles = ("账号总数", "今日已签", "今日待签", "异常 / 过期")
        cards: list[ttk.Label] = []
        for idx, title in enumerate(titles):
            card = tk.Frame(
                box, bg=COL_BG, highlightbackground=COL_LINE, highlightthickness=1, padx=12, pady=8
            )
            card.grid(row=0, column=idx, sticky="nsew", padx=(0 if idx == 0 else 6, 0))
            box.columnconfigure(idx, weight=1)
            ttk.Label(card, text=title, style="StatKey.TLabel").pack(anchor="w")
            value = ttk.Label(card, text="—", style="StatValue.TLabel")
            value.pack(anchor="w")
            cards.append(value)
        self._stat_widgets[group] = cards

    def _set_stats(self, group: str, rows: list[dict[str, Any]]) -> None:
        total, done, pending, bad = account_stats(rows)
        values = (str(total), str(done), str(pending), str(bad))
        colors = (COL_TEXT, COL_OK, COL_TEXT, COL_BAD if bad else COL_TEXT)
        for lbl, text, color in zip(self._stat_widgets.get(group, []), values, colors):
            lbl.configure(text=text, foreground=color)
        if group == "accounts":
            self.lbl_side_today_done.configure(text=f"今日已签 {done} / {total}")

    # ------------------------------------------------------------------ 各页面
    def _build_home(self) -> None:
        page = self._make_page("home")
        self._stat_cards(page, "home")

        status = ttk.LabelFrame(page, text="今日与授权", style="Card.TLabelframe")
        status.pack(fill="x", pady=(0, 8))
        self.lbl_today = ttk.Label(status, text="…", foreground=COL_TEXT, wraplength=880,
                                   justify="left")
        self.lbl_today.pack(anchor="w")
        self.lbl_license = ttk.Label(status, text="授权：未检查", foreground=COL_TEXT,
                                     wraplength=880, justify="left")
        self.lbl_license.pack(anchor="w", pady=(6, 0))
        self.lbl_account_quota = ttk.Label(status, text="账号额度：未检查", foreground=COL_TEXT,
                                           wraplength=880, justify="left")
        self.lbl_account_quota.pack(anchor="w", pady=(6, 0))
        self.lbl_sched_home = ttk.Label(status, text="调度：检测中", foreground=COL_MUTED,
                                        wraplength=880, justify="left")
        self.lbl_sched_home.pack(anchor="w", pady=(6, 0))

        actions = ttk.LabelFrame(page, text="常用操作", style="Card.TLabelframe")
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
        row2 = ttk.Frame(actions)
        row2.pack(fill="x", pady=(2, 0))
        for text, cmd in [
            ("同步代跑凭证", self.sync_server_credentials),
            ("执行成长任务", self.run_wb_tasks),
            ("账号管理", lambda: self.show_page("accounts")),
            ("签到设置", lambda: self.show_page("checkin")),
        ]:
            ttk.Button(row2, text=text, width=14, command=cmd).pack(side="left", padx=2)

        tip = ttk.LabelFrame(page, text="上手三步", style="Card.TLabelframe")
        tip.pack(fill="x")
        ttk.Label(
            tip,
            text=(
                "① 官方客户端登录后到「账号管理」从客户端采集 / 粘贴 token\n"
                "② 或导入账密后统一登录（优先 API，失败再开浏览器）\n"
                "③ 「签到设置」里定好每日签到时间与账号间隔，挂着即可\n\n"
                "账密仅本机加密保存；代跑只上传 token。"
            ),
            foreground=COL_MUTED,
            justify="left",
        ).pack(anchor="w")

    def _build_accounts(self) -> None:
        page = self._make_page("accounts")
        self._stat_cards(page, "accounts")

        ops = ttk.LabelFrame(page, text="导入与跑批", style="Card.TLabelframe")
        ops.pack(fill="x", pady=(0, 8))
        r1 = ttk.Frame(ops)
        r1.pack(fill="x", pady=2)
        for text, cmd in [
            ("从客户端导入 WorkBuddy", self.capture_workbuddy),
            ("从客户端导入 TraeWork", self.capture_traework),
            ("账密导入", self.import_credentials),
            ("统一登录", self.unified_login_all),
            ("粘贴 Trae token", self.paste_trae_token),
        ]:
            ttk.Button(r1, text=text, command=cmd).pack(side="left", padx=2)
        r2 = ttk.Frame(ops)
        r2.pack(fill="x", pady=(4, 0))
        for text, cmd in [
            ("立即全部签到", self.run_now),
            ("上传代跑", self.upload_delegate),
            ("同步代跑凭证", self.sync_server_credentials),
            ("执行成长任务", self.run_wb_tasks),
            ("管理账密", self.manage_credentials),
            ("刷新全部积分", self.refresh_all_credits),
        ]:
            ttk.Button(r2, text=text, command=cmd).pack(side="left", padx=2)
        self.lbl_acc_hint = ttk.Label(
            ops,
            text="账号之间按「签到设置」里的随机间隔逐个执行，避免同一秒并发打供应商被风控；"
                 "积分低于阈值的账号在列表里标橙。",
            foreground=COL_MUTED,
            wraplength=880,
            justify="left",
        )
        self.lbl_acc_hint.pack(anchor="w", pady=(6, 0))

        # 行操作条排在表格之前：表格是 fill+expand 的那个，把别的控件排在它后面
        # 等于让 12 行的自然高度把操作条挤出窗口（截图里直接看不见）。
        self.lbl_selection = ttk.Label(page, text="未选中账号", foreground=COL_MUTED)
        self.lbl_selection.pack(anchor="w")
        row_ops = ttk.Frame(page)
        row_ops.pack(fill="x", pady=(2, 6))
        ttk.Label(row_ops, text="选中行：", foreground=COL_MUTED).pack(side="left")
        for text, cmd in [
            ("签到", lambda: self.run_one()),
            ("刷新", lambda: self.refresh_one_credit()),
            ("上传", lambda: self.upload_delegate(one=True)),
            ("本机", lambda: self.set_mode("local")),
            ("代跑", lambda: self.set_mode("server")),
            ("更换", self.replace_selected),
            ("删除", self.delete_selected),
        ]:
            ttk.Button(row_ops, text=text, width=6, command=cmd).pack(side="left", padx=2)
        ttk.Label(
            row_ops,
            text="（双击行 = 签到；右键行 = 同样的操作）",
            foreground=COL_MUTED,
        ).pack(side="left", padx=(8, 0))

        list_box = ttk.LabelFrame(page, text="账号列表", style="Card.TLabelframe")
        list_box.pack(fill="both", expand=True)
        # 账号 ID 不再占一列：它既挤掉「最近结果」又没人读得下去，
        # 改放 Treeview 的 iid 里（选中行即拿到 ID），界面上只在选中提示里出现。
        cols = ("provider", "label", "credits", "streak", "today", "token", "result", "mode")
        headers = {
            "provider": "平台",
            "label": "账号",
            "credits": "积分余额",
            "streak": "连签",
            "today": "今日状态",
            "token": "Token",
            "result": "最近结果",
            "mode": "模式",
        }
        widths = {
            # 1120 窗口 - 侧栏 186 - 页面内边距 28 - 卡片内边距 20 ≈ 880 可用。
            # Treeview 没有横向滚动条兜底，撑宽的两列（账号 / 最近结果）一旦把
            # 定宽列挤出可视区，末列「模式」就会被裁掉，所以定宽列合计留到 560 以内。
            "provider": 58,
            "label": 150,
            "credits": 66,
            "streak": 44,
            "today": 78,
            "token": 118,
            "result": 250,
            "mode": 56,
        }
        self.tree = ttk.Treeview(list_box, columns=cols, show="headings", height=9)
        for tag, color in (
            ("ok", COL_OK),
            ("bad", COL_BAD),
            ("warn", COL_WARN),
            ("muted", COL_MUTED),
        ):
            self.tree.tag_configure(tag, foreground=color)
        for c in cols:
            self.tree.heading(c, text=headers[c])
            self.tree.column(
                c, width=widths[c], stretch=(c in ("label", "result")),
                anchor="e" if c in ("credits", "streak") else "w",
            )
        self.tree.pack(fill="both", expand=True)
        self.lbl_empty = ttk.Label(list_box, text="", foreground=COL_MUTED)
        self.lbl_empty.pack(anchor="w")
        self.tree.bind("<Double-1>", lambda _e: self.run_one())
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._on_select())
        menu = tk.Menu(self.tree, tearoff=0)
        for text, cmd, key in (
            ("立即签到", lambda: self.run_one(), "checkin"),
            ("刷新积分", lambda: self.refresh_one_credit(), "refresh"),
            ("上传代跑", lambda: self.upload_delegate(one=True), "upload"),
            ("设为本机跑", lambda: self.set_mode("local"), "local"),
            ("设为服务器代跑", lambda: self.set_mode("server"), "server"),
            ("更换代跑账号", self.replace_selected, "replace"),
            ("——", None, None),
            ("删除账号", self.delete_selected, "del"),
        ):
            if cmd is None:
                menu.add_separator()
                continue
            menu.add_command(label=text, command=cmd)
        self.tree.bind("<Button-3>", lambda e: self._popup_menu(e, menu))


    def _popup_menu(self, event: tk.Event, menu: tk.Menu) -> None:
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
            menu.post(event.x_root, event.y_root)

    def _build_today(self) -> None:
        page = self._make_page("today")
        bar = ttk.Frame(page)
        bar.pack(fill="x")
        self.lbl_today_tab = ttk.Label(bar, text="今日统计")
        self.lbl_today_tab.pack(side="left")
        ttk.Button(bar, text="刷新", width=8, command=self.refresh_today).pack(side="right")

        paned = ttk.Panedwindow(page, orient=tk.HORIZONTAL)
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
        page = self._make_page("credits")
        bar = ttk.Frame(page)
        bar.pack(fill="x")
        ttk.Button(bar, text="刷新", width=8, command=self.refresh_credits_view).pack(
            side="left", padx=2
        )
        ttk.Button(bar, text="清理", width=8, command=self.clear_credits).pack(side="left", padx=2)
        ttk.Label(bar, text="只看某个号：", foreground=COL_MUTED).pack(side="left", padx=(12, 2))
        self.var_credit_filter = tk.StringVar(value="所有账号")
        self.cmb_credit_filter = ttk.Combobox(
            bar, textvariable=self.var_credit_filter, width=26, state="readonly",
            values=["所有账号"],
        )
        self.cmb_credit_filter.pack(side="left")
        self.cmb_credit_filter.bind("<<ComboboxSelected>>", lambda _e: self._render_credits())
        cols = ("at", "provider", "account_id", "credits", "streak", "ok", "message")
        headers = {
            "at": "时间",
            "provider": "平台",
            "account_id": "账号ID",
            "credits": "积分",
            "streak": "连签",
            "ok": "成功",
            "message": "说明",
        }
        self.credit_tree = ttk.Treeview(page, columns=cols, show="headings", height=16)
        self.credit_tree.tag_configure("ok", foreground=COL_OK)
        self.credit_tree.tag_configure("bad", foreground=COL_BAD)
        for c in cols:
            self.credit_tree.heading(c, text=headers[c])
            self.credit_tree.column(
                c, width=90 if c != "message" else 260, stretch=(c == "message"),
                anchor="e" if c in ("credits", "streak") else "w",
            )
        self.credit_tree.pack(fill="both", expand=True, pady=(6, 0))
        self._credit_rows: list[dict[str, Any]] = []
        self._credit_options: dict[str, str] = {"所有账号": ""}

    def _build_checkin(self) -> None:
        page = self._make_page("checkin")
        s = self.settings

        box = ttk.LabelFrame(page, text="自动签到", style="Card.TLabelframe")
        box.pack(fill="x", pady=(0, 8))
        self.var_auto = tk.BooleanVar(value=bool(s.get("auto_schedule", True)))
        ttk.Checkbutton(box, text="开启每日自动签到", variable=self.var_auto).pack(anchor="w")
        row = ttk.Frame(box)
        row.pack(fill="x", pady=(6, 0))
        ttk.Label(row, text="每日签到时间").pack(side="left")
        self.var_sched_hour = tk.StringVar(value=str(s.get("schedule_hour", 9)))
        self.var_sched_min = tk.StringVar(value=str(s.get("schedule_minute", 10)))
        ttk.Spinbox(row, from_=0, to=23, width=4, textvariable=self.var_sched_hour).pack(
            side="left", padx=(6, 2)
        )
        ttk.Label(row, text=":").pack(side="left")
        ttk.Spinbox(row, from_=0, to=59, width=4, textvariable=self.var_sched_min).pack(
            side="left", padx=(2, 6)
        )
        ttk.Label(row, text="错过会自动补签", foreground=COL_MUTED).pack(side="left")
        row2 = ttk.Frame(box)
        row2.pack(fill="x", pady=(6, 0))
        self.var_evening = tk.BooleanVar(value=bool(s.get("evening_schedule", True)))
        ttk.Checkbutton(row2, text="晚间补漏", variable=self.var_evening).pack(side="left")
        self.var_ev_hour = tk.StringVar(value=str(s.get("evening_hour", 20)))
        self.var_ev_min = tk.StringVar(value=str(s.get("evening_minute", 0)))
        ttk.Spinbox(row2, from_=0, to=23, width=4, textvariable=self.var_ev_hour).pack(
            side="left", padx=(8, 2)
        )
        ttk.Label(row2, text=":").pack(side="left")
        ttk.Spinbox(row2, from_=0, to=59, width=4, textvariable=self.var_ev_min).pack(
            side="left", padx=(2, 6)
        )
        ttk.Label(row2, text="到点仍未签的账号再跑一次", foreground=COL_MUTED).pack(side="left")
        ttk.Label(
            box,
            text="实际执行时间在设定值之后 0~2 分钟内随机触发一次，避开整点；窗口内没赶上还会在晚窗补。",
            foreground=COL_MUTED,
            wraplength=860,
            justify="left",
        ).pack(anchor="w", pady=(6, 0))

        pace = ttk.LabelFrame(page, text="节奏与提醒", style="Card.TLabelframe")
        pace.pack(fill="x", pady=(0, 8))
        row3 = ttk.Frame(pace)
        row3.pack(fill="x", pady=2)
        ttk.Label(row3, text="账号间随机签到间隔").pack(side="left")
        self.var_gap_min = tk.StringVar(value=str(s.get("run_gap_min_sec", 20)))
        self.var_gap_max = tk.StringVar(value=str(s.get("run_gap_max_sec", 60)))
        ttk.Spinbox(row3, from_=0, to=600, width=5, textvariable=self.var_gap_min).pack(
            side="left", padx=(6, 2)
        )
        ttk.Label(row3, text="秒 ~").pack(side="left")
        ttk.Spinbox(row3, from_=0, to=600, width=5, textvariable=self.var_gap_max).pack(
            side="left", padx=(2, 4)
        )
        ttk.Label(row3, text="秒（降低风控风险）", foreground=COL_MUTED).pack(side="left")
        row4 = ttk.Frame(pace)
        row4.pack(fill="x", pady=2)
        ttk.Label(row4, text="积分不足提醒阈值").pack(side="left")
        self.var_low_credit = tk.StringVar(value=str(s.get("credit_low_threshold", 100)))
        ttk.Spinbox(row4, from_=0, to=1000000, increment=50, width=8,
                    textvariable=self.var_low_credit).pack(side="left", padx=(6, 4))
        ttk.Label(row4, text="积分（低于该值列表标橙，仅 WorkBuddy 有效；0 = 关闭）",
                  foreground=COL_MUTED).pack(side="left")
        ttk.Label(
            pace,
            text="13 个账号按 20~60 秒间隔跑一轮约 2~12 分钟；只求快可以设成 0~0 秒，但会失去错峰保护。",
            foreground=COL_MUTED,
            wraplength=860,
            justify="left",
        ).pack(anchor="w", pady=(4, 0))

        tasks = ttk.LabelFrame(page, text="成长任务与状态同步", style="Card.TLabelframe")
        tasks.pack(fill="x")
        row5 = ttk.Frame(tasks)
        row5.pack(fill="x", pady=2)
        ttk.Label(row5, text="WorkBuddy 成长任务").pack(side="left")
        self.var_wb_mode = tk.StringVar(value=str(s.get("workbuddy_task_mode") or "off"))
        ttk.Combobox(
            row5,
            textvariable=self.var_wb_mode,
            values=["off", "local", "server"],
            state="readonly",
            width=8,
        ).pack(side="left", padx=6)
        self.var_wb_chat = tk.BooleanVar(value=bool(s.get("workbuddy_chat_tasks", True)))
        ttk.Checkbutton(row5, text="含 AI 对话任务", variable=self.var_wb_chat).pack(side="left")
        ttk.Label(
            tasks,
            text="成长任务：召唤专家、模板、聊天、抽奖、盲盒、派猫猫旅行、连签兑换等，只补未完成项，"
                 "重复运行不会重复领取。选「服务器代跑」后需重新点一次「上传代跑」才会生效。",
            foreground=COL_MUTED,
            wraplength=860,
            justify="left",
        ).pack(anchor="w", pady=(4, 0))
        row6 = ttk.Frame(tasks)
        row6.pack(fill="x", pady=(6, 0))
        self.var_auto_sync = tk.BooleanVar(value=bool(s.get("auto_sync", True)))
        ttk.Checkbutton(row6, text="自动同步状态与积分", variable=self.var_auto_sync).pack(
            side="left"
        )
        ttk.Label(row6, text="同步间隔（分钟）").pack(side="left", padx=(14, 4))
        self.var_sync_minutes = tk.StringVar(value=str(s.get("auto_sync_minutes") or 5))
        ttk.Spinbox(row6, from_=1, to=240, width=5, textvariable=self.var_sync_minutes).pack(
            side="left"
        )
        ttk.Label(
            tasks,
            text="开启后程序启动几秒内先同步一次，之后每隔设定间隔自动查积分；「哪些号跑了」走我们自己的服务器，"
                 "界面每 15 秒自动重读，不用手点。",
            foreground=COL_MUTED,
            wraplength=860,
            justify="left",
        ).pack(anchor="w", pady=(4, 0))
        # 回显走一遍同一套钳制：设置文件里存着脏值时，输入框不该显示 99 点
        self._fill_checkin_vars(self.settings)
        btns = ttk.Frame(tasks)
        btns.pack(fill="x", pady=(8, 0))
        self.btn_save_checkin = ttk.Button(btns, text="保存设置", width=12,
                                           command=self.save_checkin_settings)
        self.btn_save_checkin.pack(side="left")
        self.lbl_checkin_tip = ttk.Label(btns, text="", foreground=COL_MUTED)
        self.lbl_checkin_tip.pack(side="left", padx=8)

    def _build_settings(self) -> None:
        page = self._make_page("settings")
        s = self.settings

        frm = ttk.LabelFrame(page, text="授权", style="Card.TLabelframe")
        frm.pack(fill="x", pady=(0, 8))
        ttk.Label(frm, text="授权服务").grid(row=0, column=0, sticky="w")
        self.var_base = tk.StringVar(value=str(s.get("license_base_url") or ""))
        ttk.Entry(frm, textvariable=self.var_base, width=64).grid(
            row=0, column=1, sticky="ew", pady=3, padx=(8, 0)
        )
        ttk.Label(frm, text="卡密").grid(row=1, column=0, sticky="w")
        self.var_card = tk.StringVar(value=str(s.get("card_code") or ""))
        ttk.Entry(frm, textvariable=self.var_card, show="*", width=64).grid(
            row=1, column=1, sticky="ew", pady=3, padx=(8, 0)
        )
        frm.columnconfigure(1, weight=1)
        btns = ttk.Frame(frm)
        btns.grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.btn_redeem = ttk.Button(btns, text="保存并激活", width=12, command=self.on_redeem)
        self.btn_redeem.pack(side="left", padx=2)
        self.btn_refresh_license = ttk.Button(btns, text="刷新授权", width=12,
                                              command=self.refresh_license)
        self.btn_refresh_license.pack(side="left", padx=2)
        self.lbl_lic_detail = ttk.Label(btns, text="", foreground=COL_MUTED)
        self.lbl_lic_detail.pack(side="left", padx=8)

        opt = ttk.LabelFrame(page, text="本机选项", style="Card.TLabelframe")
        opt.pack(fill="x", pady=(0, 8))
        self.var_autostart = tk.BooleanVar(value=bool(s.get("autostart")))
        self.var_trae_capture = tk.BooleanVar(value=bool(s.get("traework_auto_capture", True)))
        self.var_tray = tk.BooleanVar(value=bool(s.get("minimize_to_tray", True)))
        ttk.Checkbutton(opt, text="开机自启", variable=self.var_autostart).pack(anchor="w")
        ttk.Checkbutton(opt, text="自动捕获 Trae CN 登录（登录后直接入库）",
                        variable=self.var_trae_capture).pack(anchor="w")
        ttk.Label(
            opt,
            text="Trae CN 在本机只保留「最后登录的那一个」账号，切号会覆盖旧登录态；开启后每次登录都会自动入库，无需手动采集。",
            foreground=COL_MUTED,
            wraplength=860,
            justify="left",
        ).pack(anchor="w")
        ttk.Checkbutton(opt, text="关闭窗口时最小化到托盘", variable=self.var_tray).pack(anchor="w")
        ttk.Button(opt, text="保存本机选项", width=14, command=self.save_system_settings).pack(
            anchor="w", pady=(6, 0)
        )

        data = ttk.LabelFrame(page, text="数据维护", style="Card.TLabelframe")
        data.pack(fill="x")
        drow = ttk.Frame(data)
        drow.pack(fill="x")
        for text, cmd in [
            ("清理实时日志", self.clear_live_logs),
            ("清理跑批日志", self.clear_run_logs),
            ("清理积分记录", self.clear_credits),
            ("打开数据目录", self.open_data_dir),
        ]:
            ttk.Button(drow, text=text, width=14, command=cmd).pack(side="left", padx=2)
        ttk.Label(
            data,
            text=f"数据目录：{data_root()}",
            foreground=COL_MUTED,
        ).pack(anchor="w", pady=(6, 0))

    def _build_help(self) -> None:
        page = self._make_page("help")
        body = ttk.LabelFrame(page, text="使用说明", style="Card.TLabelframe")
        body.pack(fill="both", expand=True)
        text = scrolledtext.ScrolledText(
            body, wrap="word", font=("Segoe UI", 10), bg=COL_BG, relief="flat", padx=10, pady=8
        )
        text.pack(fill="both", expand=True)
        text.insert(
            "1.0",
            "一、把账号弄进来（任选其一）\n"
            "  · 从客户端导入：先在官方客户端登录一次，再点「从客户端导入 WorkBuddy / TraeWork」\n"
            "  · 账密导入 + 统一登录：优先走 API 登录，失败时才开一次浏览器\n"
            "  · 粘贴 Trae token：从抓包工具复制 cloudide / Bearer token\n\n"
            "二、定节奏（签到设置）\n"
            "  · 每日签到时间 + 晚间补漏：错过会自动补，实际触发再随机 0~2 分钟\n"
            "  · 账号间随机间隔：默认 20~60 秒，避免同一秒并发问供应商被风控\n"
            "  · 积分不足提醒阈值：低于该值的 WorkBuddy 账号列表标橙，0 表示关闭\n\n"
            "三、挂着就行\n"
            "  · 左侧状态灯显示调度是否在跑、下一个窗口几点；「今日签到」看已跑/未跑/失败\n"
            "  · 行内「签到 / 刷新」只操作选中的那一个号，不会惊动其它账号\n\n"
            "四、本机跑还是服务器代跑\n"
            "  · 本机模式：程序得开着，token 不外传\n"
            "  · 代跑模式：token 上传服务器，本机可以关机；换号用行内「更换」\n"
            "  · 代跑账号的 token 会由「同步代跑凭证」自动续期回传，不必重新采集\n\n"
            "五、账号数量与授权\n"
            "  · 坐席按卡密授权的账号数计；额度用满时新增会被拒绝，已挂的号不受影响\n"
            "  · 卡密到期进入宽限期：跑批暂停，续卡或买卡后自动恢复\n\n"
            "账密与 token 只在本机加密保存（Windows DPAPI）；DPAPI 不可用时启动会给出警告。\n",
        )
        text.configure(state="disabled")

    # ------------------------------------------------------------------ 任务 / 日志
    def _manual_sync_task(self) -> Callable[[], None]:
        """「同步」「刷新积分」共用同一条同步路径：同一批 token 不被并发使用。"""

        def task() -> None:
            if self._vendor_busy.is_set() or credit_slot_busy():
                self.append_log("签到/登录正在执行，请等待其完成后再同步")
                return
            try:
                result = self.auto_sync.sync_once(reason="手动")
            except Exception as exc:  # noqa: BLE001
                self.append_log(f"同步失败：{mask_text(exc, 160)}")
                return
            if not result.get("ok"):
                self.append_log(result.get("message") or "同步失败")

        return task

    def _refresh_all(self) -> None:
        """同步 = 真的去拉一次（服务器状态 + 本机积分），不只是重读本地文件。"""

        self._run_task_in_background(
            self._manual_sync_task(), [self.btn_refresh_all], "正在同步…", on_done=self._refresh_views
        )
        self.refresh_license()

    def _tick_clock(self) -> None:
        if self.lbl_time is not None:
            self.lbl_time.configure(text=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        self.after(1000, self._tick_clock)

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
                self.after(0, lambda s=status_message: self.lbl_license_bar.config(text=s, foreground=COL_MUTED))

            try:
                task_func()
            finally:
                if on_done:
                    self.after(0, on_done)
                for btn in buttons_to_disable:
                    self.after(0, lambda b=btn: b.config(state="enabled"))
                if self.lbl_license_bar and status_message:
                    self.after(0, lambda: self.lbl_license_bar.config(text="", foreground=COL_MUTED))

        threading.Thread(target=worker, daemon=True).start()

    def _tick_refresh(self) -> None:
        try:
            self.refresh_today()
            self.refresh_accounts()
            self._refresh_scheduler_status()
        except Exception as e:
            self.append_log(f"后台刷新异常: {e}")
        self.after(15000, self._tick_refresh)

    def _refresh_scheduler_status(self) -> None:
        """状态灯：「已暂停」和「调度线程静默死了」必须长得不一样。"""
        try:
            status = self.scheduler.status()
        except Exception as exc:  # noqa: BLE001 - 状态读取不能反过来把界面带崩
            self.append_log(f"读取调度状态失败：{mask_text(exc, 120)}")
            return
        if not status.get("running"):
            text, color, tip = "● 调度已停止", COL_BAD, "调度线程没在跑：重启程序或检查日志"
        elif not status.get("enabled"):
            text, color, tip = "● 已暂停（自动签到关闭）", COL_WARN, "在「签到设置」里开启每日自动签到"
        else:
            text, color, tip = "● 运行中", COL_OK, ""
        next_at = status.get("nextAt") or "—"
        self.lbl_sched_state.configure(text=text, foreground=color)
        self.lbl_side_sched_next.configure(text=f"下次签到 {next_at}")
        self.lbl_sched_home.configure(
            text=f"调度：{text.lstrip('● ')} · 下次签到 {next_at}" + (f"（{tip}）" if tip else ""),
            foreground=color if color != COL_OK else COL_MUTED,
        )

    def _selected_view(self) -> dict[str, Any] | None:
        """选中行的账号：行 iid 就是账号 ID（见 ``_render_accounts``）。"""
        sel = self.tree.selection()
        if not sel:
            return None
        account_id = str(sel[0])
        return next((v for v in self._views if str(v.get("id")) == account_id), None)

    def _on_select(self) -> None:
        view = self._selected_view()
        if not view:
            self.lbl_selection.configure(text="未选中账号")
            return
        self.lbl_selection.configure(
            text=f"已选中：{provider_label(view.get('provider'))} · "
                 f"{view.get('label') or view.get('id')}（{view.get('id')}）"
        )

    # ------------------------------------------------------------------ 授权 / 额度
    def refresh_license(self, *, _from_thread: bool = False) -> None:
        if not _from_thread:
            self._run_task_in_background(
                lambda: self.refresh_license(_from_thread=True),
                [self.btn_refresh_all],
                "正在刷新授权状态...",
            )
            return

        self.settings["license_base_url"] = self.var_base.get().strip()
        save_settings(self.settings)
        result = check_status(self.settings, force_online=True)
        text = f"valid={result.get('valid')}  {result.get('message')}"
        self.after(0, lambda: self.lbl_license.configure(text="授权：" + text))
        self.after(0, lambda: self.lbl_lic_detail.configure(text=text))
        if self.lbl_license_bar is not None:
            valid = bool(result.get("valid"))
            self.after(0, lambda: self.lbl_side_lic.configure(text=f"授权：{'有效' if valid else '无效'}"))
            self.after(0, lambda: self.lbl_license_bar.configure(
                text=("● 已授权" if valid else "○ 未授权"),
                foreground=(COL_OK if valid else COL_MUTED),
            ))
        self.append_log("授权: " + text)

        # Update account quota display（额度用满时给出升级引导）
        usage = account_store.get_account_usage()
        used = usage.get("used") or 0
        limit = usage.get("limit")
        plan = usage.get("planLabel") or ""
        if limit is None:
            # 新口径：limit=None 只剩「额度未知」这一种含义（离线 / 卡密要重新激活），
            # 不再当「不限」。旧文案会让用户以为能随便挂，回联网才发现新增被拒。
            quota_text = f"{used}/额度未知（离线，暂时无法新增账号）"
        else:
            if limit == 0:
                # quota=0 是新口径下的真额度：坐席已全部到期（不是「不限」）。
                # 必须给出路，否则用户以为程序坏了。
                detail = "坐席已全部到期，续卡或买卡后即可恢复"
            elif used >= limit:
                detail = "额度已满，升级套餐可挂载更多"
            else:
                detail = f"剩余 {usage.get('remain')}"
            quota_text = f"{used}/{limit}（{detail}）"
        if plan:
            quota_text += f" - {plan}"
        if usage.get("contactVerified") is False:
            # 邮箱只决定「异常时收不收邮件」，不是代跑前置条件（plan 3.7）
            quota_text += " · 可选：绑邮箱后异常能收到邮件"

        self.after(0, lambda: self.lbl_account_quota.configure(text="账号额度：" + quota_text))

    def on_redeem(self, *, _from_thread: bool = False) -> None:
        if not _from_thread:
            self._run_task_in_background(
                lambda: self.on_redeem(_from_thread=True),
                [self.btn_redeem, self.btn_refresh_license],
                "正在保存并激活授权...",
            )
            return

        self.settings["license_base_url"] = self.var_base.get().strip()
        self.settings["card_code"] = self.var_card.get().strip()
        self.settings["autostart"] = bool(self.var_autostart.get())
        self.settings["traework_auto_capture"] = bool(self.var_trae_capture.get())
        self.settings["minimize_to_tray"] = bool(self.var_tray.get())
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

    # ------------------------------------------------------------------ 设置保存
    def _checkin_values(self) -> dict[str, Any]:
        return {
            "auto": self.var_auto.get(),
            "sched_hour": self.var_sched_hour.get(),
            "sched_min": self.var_sched_min.get(),
            "evening": self.var_evening.get(),
            "ev_hour": self.var_ev_hour.get(),
            "ev_min": self.var_ev_min.get(),
            "gap_min": self.var_gap_min.get(),
            "gap_max": self.var_gap_max.get(),
            "low_credit": self.var_low_credit.get(),
            "wb_mode": self.var_wb_mode.get(),
            "wb_chat": self.var_wb_chat.get(),
            "auto_sync": self.var_auto_sync.get(),
            "sync_minutes": self.var_sync_minutes.get(),
        }

    def save_checkin_settings(self) -> None:
        updates = checkin_settings_payload(self._checkin_values())
        self.settings.update(updates)
        save_settings(self.settings)
        # 输入框回显成钳制后的值：填反的区间、越界的数字当场看到结果
        self._fill_checkin_vars(self.settings)
        if updates["auto_schedule"]:
            self.scheduler.start()
        else:
            self.scheduler.stop()
        if updates["auto_sync"]:
            was_running = self.auto_sync.status().get("running")
            self.auto_sync.start()
            if not was_running:
                self.append_log("已启用状态/积分自动同步")
            self.auto_sync.sync_soon()
        else:
            self.auto_sync.stop()
        self._refresh_scheduler_status()
        gap = f"{updates['run_gap_min_sec']}~{updates['run_gap_max_sec']} 秒"
        tip = f"已保存 · 间隔 {gap} · 阈值 {updates['credit_low_threshold']}"
        self.lbl_checkin_tip.configure(text=tip, foreground=COL_OK)
        self.append_log(
            f"签到设置已保存：自动签到={'开' if updates['auto_schedule'] else '关'}，"
            f"账号间隔 {gap}，阈值 {updates['credit_low_threshold']}"
        )

    def _fill_checkin_vars(self, settings: dict[str, Any]) -> None:
        self.var_auto.set(bool(settings.get("auto_schedule", True)))
        self.var_sched_hour.set(str(settings.get("schedule_hour", 9)))
        self.var_sched_min.set(str(settings.get("schedule_minute", 10)))
        self.var_evening.set(bool(settings.get("evening_schedule", True)))
        self.var_ev_hour.set(str(settings.get("evening_hour", 20)))
        self.var_ev_min.set(str(settings.get("evening_minute", 0)))
        self.var_gap_min.set(str(settings.get("run_gap_min_sec", 20)))
        self.var_gap_max.set(str(settings.get("run_gap_max_sec", 60)))
        self.var_low_credit.set(str(settings.get("credit_low_threshold", 100)))
        self.var_wb_mode.set(str(settings.get("workbuddy_task_mode") or "off"))
        self.var_wb_chat.set(bool(settings.get("workbuddy_chat_tasks", True)))
        self.var_auto_sync.set(bool(settings.get("auto_sync", True)))
        self.var_sync_minutes.set(str(settings.get("auto_sync_minutes") or 5))

    def save_system_settings(self) -> None:
        updates = {
            "autostart": bool(self.var_autostart.get()),
            "traework_auto_capture": bool(self.var_trae_capture.get()),
            "minimize_to_tray": bool(self.var_tray.get()),
        }
        self.settings.update(updates)
        save_settings(self.settings)
        try:
            autostart.set_enabled(updates["autostart"])
        except Exception as exc:
            self.append_log(f"开机自启设置失败: {exc}")
        self.append_log("本机选项已保存")
        if not updates["autostart"]:
            self.append_log("已关闭开机自启")

    def open_data_dir(self) -> None:
        try:
            os.startfile(str(data_root()))  # noqa: S606 - Windows 专用回退前端
        except Exception as exc:  # noqa: BLE001 - 打不开目录不算大事
            self.append_log(f"打开数据目录失败：{mask_text(exc, 120)}")

    # ------------------------------------------------------------------ 列表渲染
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

    def _render_accounts(self, views: list[dict[str, Any]] | None = None) -> None:
        if views is not None:
            self._views = views
        rows = self._visible_views()
        threshold = parse_int_field(self.settings.get("credit_low_threshold"), "credit_low_threshold")
        self._set_stats("accounts", rows)
        self._set_stats("home", self._views)
        for item in self.tree.get_children():
            self.tree.delete(item)
        for view in rows:
            status = str(view.get("today_status") or "未跑")
            mode = (
                "服务器"
                if view.get("source") == "server"
                else ("代跑" if str(view.get("run_mode")) == "server" else "本机")
            )
            tags = tuple(
                t
                for t in (
                    classify_today(status),
                    "bad" if view.get("last_error") or view.get("token_expired") else "",
                    "warn" if low_credit(view, threshold) else "",
                    "" if view.get("enabled", True) else "muted",
                )
                if t
            )
            self.tree.insert(
                "",
                tk.END,
                iid=str(view.get("id")),
                tags=tags,
                values=(
                    provider_label(view.get("provider")),
                    f"{view.get('label') or view.get('id')}"
                    + ("  · 仅服务器" if view.get("source") == "server" else ""),
                    "" if view.get("last_credits") is None else view.get("last_credits"),
                    "" if view.get("last_streak") is None else view.get("last_streak"),
                    status if view.get("enabled", True) else f"{status}（已停用）",
                    "已过期" if view.get("token_expired") else (view.get("token_hint") or "—"),
                    result_text(view),
                    mode,
                ),
            )
        total_all = len(self._views)
        if not rows:
            self.lbl_empty.configure(
                text="暂无账号，请先导入或从客户端采集"
                if not total_all
                else "该平台下暂无账号，点左侧「全部」看其它平台"
            )
        else:
            self.lbl_empty.configure(text=f"共 {len(rows)} 个账号" + ("" if not self._provider_filter
                                                                     else f"（已按 {provider_label(self._provider_filter)} 筛选）"))

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
                    f"{provider_label(v.get('provider'))} | {v.get('label')} | {v.get('today_status')} | "
                    f"积分={v.get('today_credits') if v.get('today_credits') is not None else v.get('last_credits')}",
                )

    def refresh_credits_view(self) -> None:
        """取数放后台：签到记录是一次全量读盘，主线程读会把界面卡住。"""

        def worker() -> None:
            try:
                rows = account_store.load_credit_history(limit=300)
            except Exception as exc:  # noqa: BLE001
                self.append_log(f"读取积分记录失败：{mask_text(exc, 160)}")
                return
            self.after(0, lambda r=rows: self._apply_credit_rows(r))

        threading.Thread(target=worker, daemon=True).start()

    def _credit_option(self, view: dict[str, Any]) -> str:
        return f"{provider_label(view.get('provider'))} - {view.get('label') or view.get('id')}"

    def _apply_credit_rows(self, rows: list[dict[str, Any]]) -> None:
        """记录列表 + 「只看某个号」下拉。

        选项按账号列表（``self._views``）来出，不从历史记录里凑：历史条目只有
        account_id，凑出来的下拉会少掉从没查过积分的号，与网页端口径不一致。
        """
        self._credit_rows = rows
        options = {"所有账号": ""}
        for view in self._views:
            options[self._credit_option(view)] = str(view.get("id"))
        self._credit_options = options
        self.cmb_credit_filter.configure(values=list(options))
        if self.var_credit_filter.get() not in options:
            self.var_credit_filter.set("所有账号")
        self._render_credits()

    def _render_credits(self) -> None:
        only_id = self._credit_options.get(self.var_credit_filter.get(), "")
        for item in self.credit_tree.get_children():
            self.credit_tree.delete(item)
        for row in self._credit_rows:
            if only_id and str(row.get("account_id")) != only_id:
                continue
            ok = row.get("ok")
            self.credit_tree.insert(
                "",
                tk.END,
                tags=(() if ok is None else (("ok",) if ok else ("bad",))),
                values=(
                    row.get("at"),
                    provider_label(row.get("provider")),
                    row.get("account_id"),
                    row.get("credits") if row.get("credits") is not None else "",
                    row.get("streak") if row.get("streak") is not None else "",
                    "成功" if ok else ("—" if ok is None else "失败"),
                    mask_text(row.get("message") or "", 160),
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

    # ------------------------------------------------------------------ 账号行内操作
    def _require_selection(self) -> dict[str, Any] | None:
        view = self._selected_view()
        if not view:
            messagebox.showinfo("提示", "请先在账号列表里选中一个账号")
        return view

    def run_one(self) -> None:
        """行内「签到」：只跑选中的那一个号，不惊动其它账号。"""
        view = self._require_selection()
        if not view:
            return
        account_id = str(view.get("id"))
        if view.get("source") == "server":
            messagebox.showinfo("签到", "该记录只在服务器上，本机没有凭证，跑不了")
            return
        if str(view.get("run_mode") or "local") != "local":
            messagebox.showinfo("签到", "该账号是服务器代跑模式，本机不跑")
            return
        if not view.get("enabled", True):
            messagebox.showinfo("签到", "该账号已停用，先启用再签到")
            return
        if self._vendor_busy.is_set():
            self.append_log("已有签到/登录在执行，请等待完成")
            return

        label = f"本机签到 {view.get('label') or account_id}"

        def worker() -> None:
            self._vendor_busy.set()
            try:
                results = run_local_all(
                    require_license=True, log=self.append_log, account_id=account_id
                )
                self.append_log(f"{label}完成，共 {len(results)} 条")
            finally:
                self._vendor_busy.clear()
                self.after(0, self._refresh_views)

        threading.Thread(target=worker, daemon=True).start()
        self.append_log(f"开始{label}…")

    def refresh_one_credit(self) -> None:
        """行内「刷新」：查一个号的积分，和自动同步抢同一个串行槽。"""
        view = self._require_selection()
        if not view:
            return
        account = next(
            (a for a in account_store.load_accounts() if str(a.get("id")) == str(view.get("id"))),
            None,
        )
        if account is None:
            messagebox.showinfo("刷新", "找不到该账号，可能已删除")
            return
        blob = account.get("token_blob") if isinstance(account.get("token_blob"), dict) else {}
        if not (blob.get("token") or blob.get("access_token")):
            messagebox.showinfo("刷新", "本机没有该账号的凭证（仅服务器代跑记录），查不了积分")
            return
        if self._vendor_busy.is_set() or credit_slot_busy():
            messagebox.showinfo("刷新", "已有任务在执行（签到/登录/同步），请等待完成")
            return
        label = str(view.get("label") or view.get("id"))

        def worker() -> None:
            if not try_acquire_credit_slot():
                self.append_log(f"积分刷新跳过 {label}：已有同步任务在执行")
                return
            try:
                info = refresh_account_credits(account)
            except Exception as exc:  # noqa: BLE001 - 后台线程里没人接异常
                self.append_log(f"积分刷新失败 {label}：{mask_text(exc, 160)}")
                return
            finally:
                release_credit_slot()
            if info.get("ok"):
                self.append_log(
                    f"积分刷新 {label}："
                    f"{info.get('credits') if info.get('credits') is not None else '-'}"
                    + (f" · 连签 {info.get('streak')}" if info.get("streak") is not None else "")
                )

        threading.Thread(target=worker, daemon=True).start()

    def set_mode(self, mode: str) -> None:
        view = self._require_selection()
        if not view:
            return
        account_id = str(view.get("id"))
        result = account_store.set_run_mode(account_id, mode)
        self.append_log(str(result.get("message") or ""))
        if not result.get("ok"):
            messagebox.showerror("模式", result.get("message") or "失败")
        if mode == "server":
            self._bind_hint()
        self.refresh_accounts()

    def delete_selected(self) -> None:
        view = self._require_selection()
        if not view:
            return
        account_id = str(view.get("id"))
        if not messagebox.askyesno("确认", "删除该账号？服务器上的代跑记录也会一并删除。"):
            return
        result = account_store.delete_account_with_server(account_id, log=self.append_log)
        if not result.get("ok"):
            messagebox.showerror("删除", result.get("message") or "删除失败")
        self.refresh_accounts()
        self.refresh_today()

    def replace_selected(self) -> None:
        """行内「更换」：服务器上的代跑记录换成另一个本机账号（不重复占坐席）。"""
        view = self._require_selection()
        if not view:
            return
        if str(view.get("run_mode")) != "server":
            messagebox.showinfo("更换", "只有「代跑 / 服务器」模式的账号需要更换")
            return
        candidates = [v for v in self._views if local_account(v) and v.get("enabled", True)]
        if not candidates:
            messagebox.showinfo("更换", "没有可用的本机账号可以替换")
            return
        pick = self._pick_account_dialog(candidates, f"用哪个账号替换「{view.get('label') or view.get('id')}」")
        if not pick:
            return
        self._bind_hint()

        def worker() -> None:
            result = delegate.replace_server_account(
                str(view.get("id")), str(pick.get("id")), log=self.append_log
            )
            tip = result.get("message") or ("更换完成" if result.get("ok") else "更换失败")
            self.after(0, lambda t=tip, ok=bool(result.get("ok")): (
                messagebox.showinfo("更换", t) if ok else messagebox.showerror("更换", t)
            ))
            self.after(0, self._refresh_views)

        threading.Thread(target=worker, daemon=True).start()

    def _pick_account_dialog(
        self, candidates: list[dict[str, Any]], title: str
    ) -> dict[str, Any] | None:
        """通用「选一个账号」对话框，返回选中的视图（含 label/id）。"""
        win = tk.Toplevel(self)
        win.title(title)
        win.geometry("460x320")
        win.transient(self)
        box = ttk.Frame(win, padding=10)
        box.pack(fill="both", expand=True)
        ttk.Label(box, text=title).pack(anchor="w")
        lst = tk.Listbox(box, font=("Segoe UI", 10))
        lst.pack(fill="both", expand=True, pady=6)
        for v in candidates:
            lst.insert(
                tk.END,
                f"{provider_label(v.get('provider'))} · {v.get('label') or v.get('id')} · {v.get('id')}",
            )
        out: dict[str, Any] | None = None

        def confirm() -> None:
            nonlocal out
            idx = lst.curselection()
            if not idx:
                messagebox.showinfo("提示", "请先选中一个账号", parent=win)
                return
            out = candidates[int(idx[0])]
            win.destroy()

        btns = ttk.Frame(box)
        btns.pack(fill="x")
        ttk.Button(btns, text="确定", width=10, command=confirm).pack(side="right", padx=2)
        ttk.Button(btns, text="取消", width=10, command=win.destroy).pack(side="right")
        lst.bind("<Double-1>", lambda _e: confirm())
        self.wait_window(win)
        return out

    # ------------------------------------------------------------------ credentials / capture
    def import_credentials(self) -> None:
        ok, msg = ensure_licensed(self.settings, force_online=True)
        if not ok:
            messagebox.showerror("卡密", msg)
            return
        win = tk.Toplevel(self)
        win.title("导入账密")
        win.geometry("480x360")
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
        ttk.Label(frm, text="批量（每行 provider,username,password[,label]）", foreground=COL_MUTED).grid(
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
            "provider": "平台",
            "username": "账号",
            "last_login_ok": "登录",
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
            self._vendor_busy.set()
            try:
                for cid in credential_ids:
                    try:
                        result = login_by_id(cid, headed=True, log=self.append_log)
                        if result.needs_manual and not result.ok:
                            self.append_log("→ 请改用「采集」或「粘贴 Trae token」")
                    except Exception as exc:  # noqa: BLE001
                        self.append_log(f"统一登录异常：{exc}")
            finally:
                self._vendor_busy.clear()
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
        quota_blocked: list[str] = []
        saved: list[str] = []
        for auth in auths:
            label = str(auth.get("nickname") or auth.get("uid") or "workbuddy")
            stored = account_store.try_upsert_account(
                {
                    "provider": "workbuddy",
                    "label": label,
                    "identity": auth.get("uid"),
                    "run_mode": "local",
                    "enabled": True,
                    "token_blob": auth,
                    "last_error": "",
                }
            )
            if not stored.get("ok"):
                # 批量采集：额度拦住一个号不能掀翻整批，剩下的照常入库
                quota_blocked.append(f"{label}：{stored.get('message')}")
                continue
            saved.append(label)
        if quota_blocked:
            self.append_log(f"额度不足，{len(quota_blocked)} 个账号未入库：{quota_blocked[0]}")
        self.append_log(f"已入库 WorkBuddy {len(saved)} 个账号：" + "、".join(saved))
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
        stored = account_store.try_upsert_account(
            {
                "provider": "traework",
                "label": blob.get("user_id") or "traework-manual",
                "identity": blob.get("user_id") or blob["token"][-12:],
                "run_mode": "local",
                "enabled": True,
                "token_blob": blob,
            }
        )
        if not stored.get("ok"):
            # 单条导入：直接把额度/存储原因弹窗告知，不能让异常冒出来打断按钮回调
            self.append_log(f"TraeWork token 未入库：{stored.get('message')}")
            messagebox.showwarning("未入库", str(stored.get("message") or "账号入库失败"))
            self.refresh_accounts()
            return
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
        quota_blocked: list[str] = []  # 「未入库」而不是「跳过」：token 已解密成功，只是挂不进额度
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
            stored = account_store.try_upsert_account(
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
            if not stored.get("ok"):
                # 注意：这里不能复用 blocked —— 上面那个 blocked 是「受限区域」
                quota_blocked.append(f"{uid or '未知'}：{stored.get('message')}")
                continue
            saved.append(uid or "未知")
        known = traework.known_user_ids()
        collected = {str(a.get("user_id") or "") for a in todo}
        missing = [u for u in known if u not in collected]
        if quota_blocked:
            # 与 pywebview 端同一句口径：报「未入库」而不是笼统的「失败」
            self.append_log(f"额度不足，{len(quota_blocked)} 个账号未入库：{quota_blocked[0]}")
        self.append_log(
            f"已入库 TraeWork {len(saved)} 个账号：{', '.join(saved)}"
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

    # ------------------------------------------------------------------ 跑批入口
    def run_now(self) -> None:
        """「立即全部签到」：账号之间按设置里的随机间隔逐个执行。"""
        if self._vendor_busy.is_set():
            self.append_log("已有签到/登录在执行，请等待完成")
            return

        def worker() -> None:
            self._vendor_busy.set()  # 自动同步让路，别在签到时并发问同一个 token
            try:
                results = run_local_all(require_license=True, log=self.append_log)
                self.append_log(f"本机签到完成，共 {len(results)} 条")
            finally:
                self._vendor_busy.clear()
                self.after(0, self._refresh_views)
                self.after(0, self._refresh_scheduler_status)

        threading.Thread(target=worker, daemon=True).start()

    def refresh_all_credits(self) -> None:
        """「刷新积分」与「同步」同一条路径：共用串行槽和限速，
        否则两个入口能对同一批账号并发问供应商。"""

        self._run_task_in_background(
            self._manual_sync_task(), [self.btn_refresh_all], "正在刷新积分…", on_done=self._refresh_views
        )

    def _refresh_views(self) -> None:
        """重读界面：必须在同步任务结束后再调，提前读只会显示同步前的旧数据。"""
        self.refresh_accounts()
        self.refresh_today()
        self.refresh_credits_view()

    def upload_delegate(self, one: bool = False) -> None:
        ok, msg = ensure_licensed(self.settings, force_online=True)
        if not ok:
            messagebox.showerror("卡密", msg)
            return
        self._bind_hint()
        account_id = ""
        if one:
            view = self._require_selection()
            if not view:
                return
            account_id = str(view.get("id"))
        accounts = account_store.load_accounts()
        targets = [a for a in accounts if (not account_id or str(a.get("id")) == account_id)]
        task_enabled = self.settings.get("workbuddy_task_mode") == "server"

        def worker() -> None:
            uploaded = delegate.upload_delegate_accounts(
                targets, task_enabled=task_enabled, log=self.append_log
            )
            self.append_log(f"代跑上传完成：成功推送 {uploaded} 个账号")
            self.after(0, self.refresh_accounts)

        threading.Thread(target=worker, daemon=True).start()

    def sync_server_credentials(self) -> None:
        """手动触发一次「代跑凭证保鲜」：本机续期后回传服务器。"""
        ok, msg = ensure_licensed(self.settings, force_online=False)
        if not ok:
            messagebox.showerror("卡密", msg)
            return

        def worker() -> None:
            results = refresh_server_credentials(log=self.append_log)
            if not results:
                self.append_log("没有处于代跑模式的账号")
                return
            synced = sum(1 for r in results if r.get("synced"))
            refreshed = sum(1 for r in results if r.get("token_refreshed"))
            self.append_log(
                f"代跑凭证同步完成：检查 {len(results)} 个账号，刷新 token {refreshed} 个，回传 {synced} 个"
            )

        threading.Thread(target=worker, daemon=True).start()

    def run_wb_tasks(self) -> None:
        """立即执行 WorkBuddy 成长任务（本机模式；代跑模式下由服务器执行）。"""
        ok, msg = ensure_licensed(self.settings, force_online=False)
        if not ok:
            messagebox.showerror("卡密", msg)
            return
        if str(self.settings.get("workbuddy_task_mode") or "off") == "server":
            messagebox.showinfo(
                "成长任务", "当前是「服务器代跑」模式，任务由服务器在每日代跑时执行，本机不重复跑"
            )
            return

        def worker() -> None:
            results = run_workbuddy_tasks_all(log=self.append_log, force=True)
            if not results:
                self.append_log("没有可执行的 WorkBuddy 账号（需为本机模式且已启用）")
                return
            done = sum(1 for r in results if r.get("ok"))
            self.append_log(f"成长任务执行完成：{done}/{len(results)} 个账号")
            self.after(0, self._refresh_views)

        threading.Thread(target=worker, daemon=True).start()

    def run_server_now(self) -> None:
        def worker() -> None:
            result = server_client.run_now_server()
            self.append_log(f"服务器代跑触发: {result}")
            runs = server_client.today_runs()
            self.append_log(f"代跑今日结果: {runs}")
            self.after(0, self.refresh_today)

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------ 托盘 / 退出
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
