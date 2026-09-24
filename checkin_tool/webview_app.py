# -*- coding: utf-8 -*-
"""PyWebView 桌面壳：简约 HTML 界面（对齐 Cursor 工具交付形态）。"""

from __future__ import annotations

import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import (
    __version__,
    account_store,
    autostart,
    backup,
    credential_store,
    delegate,
    license_client,
    report_export,
    server_client,
    vault,
)
from .adapters import qoder, traework, workbuddy
from .license_client import (
    check_status,
    clear_cache,
    ensure_licensed,
    public_license_view,
    redeem,
)
from .license_guard import LicenseGuard
from .login import login_by_id
from .redact import mask_text
from .scheduler import (
    AutoSyncer,
    DailyScheduler,
    credit_slot_busy,
    refresh_account_credits,
    refresh_server_credentials,
    release_credit_slot,
    run_local_all,
    run_workbuddy_tasks,
    run_workbuddy_tasks_all,
    try_acquire_credit_slot,
)
from .settings import load_settings, ordered_gap, save_settings
from .traework_watcher import TraeWorkAutoCapture


def _resource_path(*parts: str) -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        base = Path(sys._MEIPASS)  # type: ignore[attr-defined]
    else:
        base = Path(__file__).resolve().parent
    return base.joinpath(*parts)


def _num(value: Any, default: float) -> float:
    """设置项可能来自前端 / 手工改过的配置文件，不能因脏值让程序起不来。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if number != number or number in (float("inf"), float("-inf")):
        return float(default)
    return number


def _v_text(value: Any) -> tuple[bool, Any]:
    return True, str(value or "").strip()


def _v_bool(value: Any) -> tuple[bool, Any]:
    return True, bool(value)


def _v_http_url(value: Any) -> tuple[bool, Any]:
    """授权服务地址由前端任意填写，而卡密 / ticket 会发往该地址，必须限死协议。"""
    text = str(value or "").strip().rstrip("/")
    if not text:
        return True, ""
    if not text.lower().startswith(("http://", "https://")):
        return False, "授权服务地址必须以 http:// 或 https:// 开头"
    if len(text) > 200:
        return False, "授权服务地址过长"
    return True, text


def _v_choice(*allowed: str):
    def check(value: Any) -> tuple[bool, Any]:
        text = str(value or "").strip()
        if text not in allowed:
            return False, f"取值只能是 {'/'.join(allowed)}"
        return True, text

    return check


def _v_int_range(low: int, high: int):
    def check(value: Any) -> tuple[bool, Any]:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return False, f"只能是 {low}~{high} 的整数"
        if not low <= number <= high:
            return False, f"只能是 {low}~{high}"
        return True, number

    return check


# 前端可改的设置项：白名单 + 逐值校验（此前只白名单 key、不校验 value）
_SETTING_VALIDATORS: dict[str, Callable[[Any], tuple[bool, Any]]] = {
    "license_base_url": _v_http_url,
    "card_code": _v_text,
    "autostart": _v_bool,
    "auto_schedule": _v_bool,
    "catchup_on_start": _v_bool,
    "evening_schedule": _v_bool,
    "traework_auto_capture": _v_bool,
    "auto_sync": _v_bool,
    "auto_sync_minutes": _v_int_range(1, 240),
    "schedule_hour": _v_int_range(0, 23),
    "schedule_minute": _v_int_range(0, 59),
    "evening_hour": _v_int_range(0, 23),
    "evening_minute": _v_int_range(0, 59),
    # 号与号之间的随机等待：上限 600 秒，误填一个巨大的数不能把整轮跑批挂死
    "run_gap_min_sec": _v_int_range(0, 600),
    "run_gap_max_sec": _v_int_range(0, 600),
    "credit_low_threshold": _v_int_range(0, 1000000),
    "backup_keep_count": _v_int_range(1, 100),
    "traework_user_dir": _v_text,
    "traework_ug_api_base": _v_http_url,
    "workbuddy_task_mode": _v_choice("off", "local", "server"),
    "workbuddy_chat_tasks": _v_bool,
    "allow_insecure_transport": _v_bool,
    "allow_plain_fallback": _v_bool,
}


class CheckinApi:
    def __init__(self) -> None:
        self.settings = load_settings()
        self._logs: list[str] = []
        self._lock = threading.Lock()
        # settings 会被 UI 线程（JS api）、授权守卫线程、调度线程同时读写
        self._settings_lock = threading.RLock()
        self._busy: set[str] = set()
        self._busy_lock = threading.Lock()
        # 注册表条目是设置的派生物：exe 挪过目录后 Run 里还是旧路径，Windows 静默不启动，
        # 而界面上的勾仍然亮着。启动时对着当前路径核一遍（源码态内部直接返回空串）。
        message = autostart.repair_on_startup(self.settings)
        if message:
            self._append_log(message)
        self.scheduler = DailyScheduler(log=self._append_log)
        if self.settings.get("auto_schedule", True):
            self.scheduler.start()
            self._append_log("已启动本机日签调度")
        # Trae CN 只在本机保留"最后登录的那一个"账号，切号即覆盖 → 必须实时抓
        self.traework_watch = TraeWorkAutoCapture(
            self._on_traework_captured,
            log=self._append_log,
            interval=_num(self.settings.get("traework_watch_interval"), 3.0),
            get_user_dir=lambda: self._setting("traework_user_dir", ""),
        )
        if self.settings.get("traework_auto_capture", True):
            self.traework_watch.start()
            self._append_log("已启用 Trae CN 登录态自动捕获（登录后无需手动采集）")
        # 授权守卫：定时在线校验，卡密失效立即踢出登录
        self._revoked: dict[str, Any] | None = None
        check_interval = max(30.0, _num(self.settings.get("license_check_interval"), 300.0))
        self.license_guard = LicenseGuard(
            get_settings=self._settings_snapshot,
            on_revoked=self._on_license_revoked,
            log=self._append_log,
            interval_sec=check_interval,
        )
        self.license_guard.start()
        self._append_log(
            f"已启用授权守卫（每 {int(check_interval / 60) or 5} 分钟在线校验，卡密失效自动退出登录）"
        )
        if str(self._setting("license_base_url", "")).lower().startswith("http://"):
            self._append_log(
                "[!] 授权/代跑服务当前走明文 HTTP，tokenBlob 与卡密在链路上可被同网段窥探；"
                "服务端配好证书后请设置 allow_insecure_transport=false 强制 https"
            )
        # 状态/积分自动同步：启动几秒后先拉一次，之后按间隔定时拉，界面不用手点
        self.auto_sync = AutoSyncer(
            get_settings=self._settings_snapshot,
            is_busy=self._job_running,
            log=self._append_log,
        )
        if self.settings.get("auto_sync", True):
            self.auto_sync.start()
        for warning in vault.startup_warnings(license_client.data_root()):
            self._append_log(f"[!] {warning}")

    # ------------------------------------------------------------ 设置读写
    def _setting(self, key: str, default: Any = None) -> Any:
        with self._settings_lock:
            return self.settings.get(key, default)

    def _settings_snapshot(self) -> dict[str, Any]:
        with self._settings_lock:
            return dict(self.settings)

    def _set_settings(self, updates: dict[str, Any]) -> None:
        with self._settings_lock:
            self.settings.update(updates)

    # ------------------------------------------------------------ 授权门禁
    def _sync_revoked_state(self, license_info: dict[str, Any] | None) -> None:
        """卡密重新有效后清掉 revoked 标记，否则前端会一直停在激活门禁上。"""
        if not self._revoked:
            return
        if isinstance(license_info, dict) and license_info.get("valid"):
            self._restore_after_redeem("检测到授权已恢复")

    def _restore_after_redeem(self, reason: str) -> None:
        was_revoked = self._revoked is not None
        self._revoked = None
        self.license_guard.reset()
        # 刚激活就该马上同步：启动时未授权会把本轮间隔的时间戳烧掉，不重置要等满间隔
        self.auto_sync.sync_soon()
        if not was_revoked:
            return
        if self._setting("auto_schedule", True):
            self.scheduler.start()
        if self._setting("traework_auto_capture", True):
            self.traework_watch.start()
        if self._setting("auto_sync", True):
            self.auto_sync.start()
        self._append_log(f"授权已恢复（{reason}），后台校验与调度已重启")

    def _on_license_revoked(self, reason: str) -> None:
        """卡密失效：踢出登录 —— 停后台任务 + 清本机票据 + 前端弹回激活门禁。"""
        self._revoked = {"reason": reason, "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        for name, stop in (
            ("自动签到调度", self.scheduler.stop),
            ("Trae 登录态捕获", self.traework_watch.stop),
            ("状态积分同步", self.auto_sync.stop),
        ):
            try:
                stop()
                self._append_log(f"已停止{name}")
            except Exception as e:
                self._append_log(f"Error stopping task {name}: {e}")
        try:
            clear_cache()  # 清掉本机（含两处镜像）票据，后续任何操作都会被门禁拦截
            self._append_log("已清除本机授权票据")
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"[!] 清除授权票据失败：{exc}")
        self._append_log(f"请重新输入有效卡密后继续使用（原因：{reason}）")

    def stop_background(self) -> None:
        try:
            self.auto_sync.stop()
        except Exception as e:
            self._append_log(f"Error stopping service: {e}")
        try:
            self.license_guard.stop()
        except Exception as e:
            self._append_log(f"Error stopping service: {e}")
        try:
            self.traework_watch.stop()
        except Exception as e:
            self._append_log(f"Error stopping service: {e}")
        try:
            self.scheduler.stop()
        except Exception as e:
            self._append_log(f"Error stopping service: {e}")
        try:
            account_store.flush_live_logs()  # 实时日志是缓冲落盘的，退出前补一次
        except Exception as e:
            print(f"flush live log failed: {e}")

    def _append_log(self, msg: str) -> None:
        # 日志会落盘并在前端渲染：适配器/服务端的错误消息里可能带 token，统一过一道脱敏
        text = mask_text(str(msg).rstrip(), 1000)
        account_store.append_live_log(text)
        with self._lock:
            self._logs.append(text)
            self._logs = self._logs[-400:]

    @staticmethod
    def _strip_secrets(result: dict[str, Any]) -> dict[str, Any]:
        """bridge 返回值统一去掉 ticket / 卡密：前端只用得到 valid / 额度 / 到期时间。

        顶层和嵌套的 ``license``（服务端 cache 原样）都要过一遍——票据落到渲染进程
        就等于交给任何能打开 DevTools 或注入 JS 的人。
        """
        if not isinstance(result, dict):
            return {}
        out = {k: v for k, v in result.items() if k not in ("ticket", "primaryCard")}
        if isinstance(out.get("license"), dict):
            out["license"] = public_license_view(out["license"])
        return out

    def get_bootstrap(self) -> dict[str, Any]:
        license_info = self._strip_secrets(check_status(self.settings, force_online=False))
        self._sync_revoked_state(license_info.get("license"))
        # 账号合并视图（本地 + 服务器代跑）和今日跑批映射各算一次，下面三块共用：
        # 以前一次刷新要合并三遍，界面每 30 秒就白做两次全量合并。
        merged = account_store.load_accounts()
        run_map = account_store.today_run_map()
        account_rows = [account_store.public_account_view(a, run_map) for a in merged]
        return {
            "ok": True,
            "version": __version__,
            "settings": {
                "license_base_url": self.settings.get("license_base_url") or "",
                "card_code": self.settings.get("card_code") or "",
                "autostart": bool(self.settings.get("autostart")),
                "auto_schedule": bool(self.settings.get("auto_schedule", True)),
                "catchup_on_start": bool(self.settings.get("catchup_on_start", True)),
                "evening_schedule": bool(self.settings.get("evening_schedule", True)),
                "traework_auto_capture": bool(self.settings.get("traework_auto_capture", True)),
                "auto_sync": bool(self.settings.get("auto_sync", True)),
                "auto_sync_minutes": int(self.settings.get("auto_sync_minutes") or 5),
                "schedule_hour": int(self._setting("schedule_hour", 9) or 0),
                "schedule_minute": int(self._setting("schedule_minute", 10) or 0),
                "evening_hour": int(self._setting("evening_hour", 20) or 0),
                "evening_minute": int(self._setting("evening_minute", 0) or 0),
                "run_gap_min_sec": int(self._setting("run_gap_min_sec", 20) or 0),
                "run_gap_max_sec": int(self._setting("run_gap_max_sec", 60) or 0),
                "credit_low_threshold": int(self._setting("credit_low_threshold", 100) or 0),
                "backup_keep_count": int(self._setting("backup_keep_count", 10) or 10),
                "traework_user_dir": self.settings.get("traework_user_dir") or "",
                "workbuddy_task_mode": self.settings.get("workbuddy_task_mode") or "off",
                "workbuddy_chat_tasks": bool(self.settings.get("workbuddy_chat_tasks", True)),
            },
            "traework_watch": self.traework_watch.status(),
            "scheduler": self.scheduler.status(),
            "license": license_info,
            "revoked": self._revoked,  # 非空 = 卡密已失效被踢出，前端弹回门禁并提示
            "accountUsage": account_store.get_account_usage(accounts=merged),
            "licenseGuard": self.license_guard.status(),
            "sync": {
                **self.auto_sync.status(),
                "busy": self._job_running() or self.auto_sync.syncing(),
            },
            "board": account_store.today_board(accounts=merged, today=run_map),
            "accounts": account_rows,
            "logs": list(reversed(account_store.load_live_logs(120))),

            "credentials": [
                credential_store.public_credential_view(r) for r in credential_store.load_credentials()
            ],
        }

    def backup_data(self) -> dict[str, Any]:
        """备份所有数据文件到一个 zip，并轮转到最近 N 份。"""
        keep = int(self._setting("backup_keep_count", backup.DEFAULT_KEEP) or backup.DEFAULT_KEEP)
        return backup.create_backup(keep=keep, log=self._append_log)

    def restore_data(self, backup_file_path: str) -> dict[str, Any]:
        """从备份文件恢复数据。"""
        out = backup.restore_from_backup(backup_file_path, log=self._append_log)
        if out.get("ok"):
            # 重新载入内存副本：否则下一次保存设置会把刚恢复的文件覆盖回旧配置
            with self._settings_lock:
                self.settings = load_settings()
        return out

    def list_backups(self) -> dict[str, Any]:
        d = backup.backup_dir()
        files = sorted(d.glob(backup.BACKUP_PREFIX + "*" + backup.BACKUP_SUFFIX), key=lambda p: p.name, reverse=True)
        return {"ok": True, "dir": str(d), "files": [str(p) for p in files]}

    def export_csv(self, kind: str) -> dict[str, Any]:
        """把积分历史 / 跑批记录导出成 CSV，落到数据目录下的 export/。"""
        try:
            kind = str(kind or "").strip().lower()
            out_dir = account_store.data_root() / "export"
            out_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d%H%M%S")
            if kind == "credit":
                path = out_dir / f"credit_history_{stamp}.csv"
                rows = report_export.export_credit_history_csv(path)
            elif kind == "runs":
                path = out_dir / f"run_history_{stamp}.csv"
                rows = report_export.export_run_logs_csv(path)
            else:
                return {"ok": False, "message": "kind 只能是 credit 或 runs"}
            self._append_log(f"已导出 {rows} 行到 {path}")
            return {"ok": True, "message": f"已导出 {rows} 行", "path": str(path), "rows": rows}
        except Exception as exc:  # noqa: BLE001 - 导出失败只回报
            self._append_log(f"导出失败: {exc}")
            return {"ok": False, "message": f"导出失败: {exc}"}

    def poll_logs(self) -> dict[str, Any]:
        with self._lock:
            lines = list(self._logs)
            self._logs.clear()
        return {"ok": True, "lines": lines}

    def list_accounts(self) -> dict[str, Any]:
        today = account_store.today_run_map()
        rows = [account_store.public_account_view(a, today) for a in account_store.load_accounts()]
        return {"ok": True, "accounts": rows}

    def credit_history(self, account_id: str | None = None) -> dict[str, Any]:
        """获取积分历史，支持按账号ID筛选。"""
        return {"ok": True, "items": account_store.load_credit_history(account_id=account_id, limit=200)}

    def get_history(self, days: int = 14) -> dict[str, Any]:
        """跨天跑批趋势：近 N 天每日成功率 + 期间老失败的账号。"""
        try:
            span = int(days)
        except (TypeError, ValueError):
            span = 14
        return {"ok": True, **account_store.history_stats(days=span)}

    def license_check_now(self) -> dict[str, Any]:
        """手动触发一次授权校验（含踢出判定），供前端「立即校验」按钮使用。"""
        out = self.license_guard.check_now()
        if out.get("valid"):
            self._restore_after_redeem("手动校验通过")
        return {"ok": True, **out, "revoked": self._revoked}

    def save_settings(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        updates: dict[str, Any] = {}
        for key, checker in _SETTING_VALIDATORS.items():
            if key not in payload:
                continue
            ok, value_or_msg = checker(payload[key])
            if not ok:
                return {"ok": False, "message": str(value_or_msg)}
            updates[key] = value_or_msg
        if not updates:
            return {"ok": True, "message": "无改动"}
        # 区间填反了（最小 60 / 最大 20）在跑批时能被 _run_gap 兜住，但落盘就该是有序的：
        # 另一个前端回显时不认得「反区间」，会照原样显示。规则放在 settings 里和 tkinter 共用。
        if "run_gap_min_sec" in updates or "run_gap_max_sec" in updates:
            merged = {**self._settings_snapshot(), **updates}
            updates["run_gap_min_sec"], updates["run_gap_max_sec"] = ordered_gap(
                merged.get("run_gap_min_sec"), merged.get("run_gap_max_sec")
            )
        self._set_settings(updates)
        with self._settings_lock:
            snapshot = dict(self.settings)
        save_settings(snapshot)
        if "autostart" in updates:
            message = autostart.apply_toggle(bool(snapshot.get("autostart")))
            if message:
                self._append_log(message)
        if snapshot.get("auto_schedule"):
            self.scheduler.start()
        else:
            self.scheduler.stop()
        # Trae 自动捕获开关即时生效
        if snapshot.get("traework_auto_capture", True):
            if not self.traework_watch.running:
                self.traework_watch.start()
                self._append_log("已启用 Trae CN 登录态自动捕获")
        else:
            self.traework_watch.stop()
        # 自动同步开关即时生效
        if snapshot.get("auto_sync", True):
            was_running = self.auto_sync.status().get("running")
            self.auto_sync.start()
            if not was_running:
                self._append_log("已启用状态/积分自动同步")
            # 线程还活着时 start() 是 no-op，间隔计时器不会复位；刚改过设置就应当马上看到结果
            self.auto_sync.sync_soon()
        else:
            self.auto_sync.stop()
        return {"ok": True, "message": "设置已保存"}

    def redeem(self, card_code: str = "") -> dict[str, Any]:
        code = str(card_code or self._setting("card_code") or "").strip()
        if not code:
            return {"ok": False, "message": "请填写卡密"}
        self._set_settings({"card_code": code})
        snapshot = self._settings_snapshot()
        save_settings(snapshot)
        result = redeem(snapshot, code)
        license_cache = result.get("license") or {}
        activated = bool(result.get("valid") or license_cache.get("valid"))
        # 只记 message：把整个 result 打进日志会连 ticket 一起落盘
        self._append_log(f"激活: {result.get('message') or ('成功' if activated else '失败')}")
        if activated:
            self._restore_after_redeem("卡密激活成功")
            # 换卡即换额度：代挂额度按卡密/卡种配置，必须重新拉一次
            account_store.invalidate_entitlement_cache()
            account_store.refresh_entitlement(force=True)
        return {
            "ok": bool(result.get("ok") or activated),
            "result": self._strip_secrets(result),
            "message": result.get("message"),
            "valid": activated,
        }

    # ------------------------------------------------------------ 代挂额度 / 联系邮箱
    def _bind_hint(self) -> str:
        """未绑定联系邮箱时的一句引导。

        **软引导**：只记日志、只把文案带回给界面，绝不中断刚完成的操作
        （见 ``account_store.contact_notice``）。用 ``force=False`` 走权益缓存的 TTL，
        一次引导不该多打一趟 HTTP。
        """
        try:
            notice = account_store.contact_notice(force=False) or ""
        except Exception as exc:  # noqa: BLE001 - 引导本身出错不能影响主流程
            self._append_log(f"读取邮箱绑定状态失败：{exc}")
            return ""
        if notice:
            self._append_log(notice)
        return notice

    def bind_contact(self, email: str = "") -> dict[str, Any]:
        ok, msg = ensure_licensed(self.settings, force_online=False)
        if not ok:
            return {"ok": False, "message": msg}
        result = server_client.bind_contact(str(email or ""))
        self._append_log(f"绑定联系邮箱：{result.get('message') or ('已发送验证码' if result.get('ok') else '失败')}")
        return {
            "ok": bool(result.get("ok")),
            "needCode": bool(result.get("ok")),
            "message": result.get("message") or ("验证码已发送到邮箱，请查收" if result.get("ok") else "绑定失败"),
        }

    def verify_contact(self, code: str = "") -> dict[str, Any]:
        ok, msg = ensure_licensed(self.settings, force_online=False)
        if not ok:
            return {"ok": False, "message": msg}
        result = server_client.verify_contact(str(code or ""))
        self._append_log(f"邮箱验证：{result.get('message') or ('成功' if result.get('ok') else '失败')}")
        return {
            "ok": bool(result.get("ok")),
            "message": result.get("message") or ("邮箱已验证" if result.get("ok") else "验证码不正确或已过期"),
        }

    def capture_workbuddy(self) -> dict[str, Any]:
        ok, msg = ensure_licensed(self.settings, force_online=True)
        if not ok:
            return {"ok": False, "message": msg}
        custom_path = self.settings.get("workbuddy_auth_path")
        if custom_path:
            auth, err = workbuddy.load_local_auth(custom_path)
            auths = [auth] if auth else []
        else:
            auths, err = workbuddy.load_all_local_auths()
        if not auths:
            return {"ok": False, "message": err or "无登录态"}
        labels = []
        blocked: list[str] = []
        replacement_suggestion = None # 初始化建议
        for auth in auths:
            account_data = {
                "provider": "workbuddy",
                "label": auth.get("nickname") or auth.get("uid"),
                "identity": auth.get("uid"),
                "run_mode": "local",
                "enabled": True,
                "token_blob": auth,
                "last_error": "",
            }
            stored = account_store.try_upsert_account(account_data)
            if not stored.get("ok"):
                # 额度拦住的是「这一个」账号：记下来继续采下一个，
                # 抛出去会让整批采集中途 abort、已入库的一半没人收尾
                blocked.append(str(auth.get("nickname") or auth.get("uid")))
                self._append_log(f"未入库 {blocked[-1]}：{stored.get('message')}")
                continue
            labels.append(auth.get("nickname") or auth.get("uid"))
            
            # 获取替换建议
            if not replacement_suggestion: # 只取第一个账号的建议
                suggestion = self._get_replacement_suggestion(stored.get("account") or {})
                if suggestion:
                    replacement_suggestion = suggestion

        self._append_log(f"已采集 WorkBuddy {len(labels)} 个账号：{', '.join(str(x) for x in labels)}")
        
        result = {"ok": True, "message": f"采集成功（{len(labels)} 个账号）", "count": len(labels)}
        if blocked:
            result["blocked"] = len(blocked)
            result["message"] += f"，{len(blocked)} 个未入库（额度不足）"
        if replacement_suggestion:
            result["replacement_suggestion"] = replacement_suggestion
        return result
    def _store_traework_auths(self, auths: list[dict[str, Any]]) -> dict[str, Any]:
        """把 TraeWork 登录态写入账号库（按 userId 累加，不会覆盖已采账号）。"""
        tags = traework.user_tags()
        saved: list[str] = []
        identities: list[str] = []
        stored = 0
        need_token = False
        enabled_count = 0
        quota_blocked: list[str] = []  # 额度拦下的号（逐条收集，不中断整批采集）
        replacement_suggestion = None # 初始化建议
        for auth in auths:
            blocked = traework._blocked_region(auth) if auth.get("token") else ""
            if blocked:
                note = f"区域 {blocked}，签到仅支持 CN 区，已停用"
            elif not auth.get("token"):
                note = "设备头已读到，但 token 需粘贴"
                need_token = True
            else:
                note = ""
                enabled_count += 1
            if self.settings.get("traework_ug_api_base"):
                auth["ug_api_base"] = self.settings["traework_ug_api_base"]
            uid = str(auth.get("user_id") or "")
            if uid and tags.get(uid):
                # token_blob 就是 auth 本身，标签写在这里即可随账号一起入库
                auth["user_tag"] = tags[uid]
            if auth.get("needs_manual_token") and not auth.get("token"):
                continue  # 无 token 的占位项不入库，避免账号列表出现空账号
            
            account_data = {
                "provider": "traework",
                "label": auth.get("user_id") or auth.get("nickname") or "traework",
                "identity": auth.get("user_id") or auth.get("auth_key") or "traework",
                "run_mode": "local",
                "enabled": bool(auth.get("token")) and not blocked,
                "token_blob": auth,
                "last_error": note,
            }
            stored_result = account_store.try_upsert_account(account_data)
            if not stored_result.get("ok"):
                # 额度只拦住这一个号：记一笔继续采下一个（`blocked` 这个名字已被
                # 「区域受限」占用，这里用 quota_blocked）
                quota_blocked.append(f"{uid or '未知'}：{stored_result.get('message')}")
                continue
            stored += 1
            identities.append(str(auth.get("user_id") or auth.get("auth_key") or "traework"))
            saved.append(f"{uid or '未知'}({auth.get('user_region') or '?'})" + (f" {note}" if note else ""))

            # 获取替换建议
            if not replacement_suggestion: # 只取第一个账号的建议
                suggestion = self._get_replacement_suggestion(stored_result.get("account") or {})
                if suggestion:
                    replacement_suggestion = suggestion

        result = {
            "stored": stored,
            "enabled_count": enabled_count,
            "need_token": need_token,
            "saved": saved,
            "identities": identities,
            "quota_blocked": quota_blocked,
        }
        if quota_blocked:
            self._append_log(
                f"额度不足，{len(quota_blocked)} 个账号未入库：{quota_blocked[0]}"
            )
        if replacement_suggestion:
            result["replacement_suggestion"] = replacement_suggestion
        return result

    def _on_traework_captured(self, auths: list[dict[str, Any]], reason: str) -> None:
        """实时捕获回调：Trae 登录/切号后立即落库。"""
        ok, msg = ensure_licensed(self.settings, force_online=False)
        if not ok:
            return
        stat = self._store_traework_auths(auths)
        if stat["stored"]:
            self._append_log(
                f"[Trae自动采集] {reason} → 已入库 {stat['stored']} 个账号：{', '.join(stat['saved'])}"
            )
            # 代跑账号：刚拿到的就是最新 token，顺带回传服务器
            self._sync_server_for(stat["identities"])

    def _sync_server_for(self, identities: list[str], *, force: bool = False) -> None:
        """把这些账号里处于「代跑模式」的最新凭证回传服务器。"""
        wanted = {str(i) for i in (identities or []) if i}
        if not wanted:
            return
        for account in account_store.load_accounts():
            if str(account.get("run_mode") or "local") != "server":
                continue
            if str(account.get("identity") or "") not in wanted:
                continue
            try:
                server_client.sync_server_blob(account, log=self._append_log, force=force)
            except Exception as exc:  # noqa: BLE001
                self._append_log(f"代跑凭证同步异常：{exc}")

    def capture_traework(self) -> dict[str, Any]:
        ok, msg = ensure_licensed(self.settings, force_online=True)
        if not ok:
            return {"ok": False, "message": msg}
        custom_dir = self.settings.get("traework_user_dir")
        auths, err = traework.load_all_local_auths(custom_dir or None)
        if not auths:
            return {"ok": False, "message": err or "无登录态"}

        known = traework.known_user_ids()
        collected = {str(a.get("user_id") or "") for a in auths if a.get("user_id")}
        missing = [uid for uid in known if uid not in collected]

        stat = self._store_traework_auths(auths)
        if not stat["stored"]:
            return {"ok": False, "message": err or "未找到可解密的 Trae 登录态，请先登录 Trae CN 桌面端"}
        self._append_log(f"已采集 TraeWork {stat['stored']} 个账号：{', '.join(stat['saved'])}")
        self._sync_server_for(stat["identities"])
        if missing:
            self._append_log(
                f"本机还登录过 {len(missing)} 个账号但只剩记录、无 token：{', '.join(missing)}；"
                f"Trae 只保留最后登录的那个账号，历史 token 已被覆盖，需重新登录一次由自动捕获入库"
            )
        return {
            "ok": True,
            "message": (
                f"采集成功（{stat['stored']} 个账号，{stat['enabled_count']} 个可签到）"
                + (
                    f"；本机还检测到 {len(missing)} 个曾登录但当前无登录态的账号"
                    f"（{', '.join(missing)}）：其 token 已被 Trae 覆盖、无法补采，"
                    "请在 Trae CN 里重新登录一次这些账号，自动捕获会即时入库"
                    if missing
                    else ""
                )
            ),
            "count": stat["stored"],
            "missing": missing,
            "need_token": stat["need_token"],
        }

    def diagnose_traework(self) -> dict[str, Any]:
        """Trae CN 登录态体检：目录、键、可解密情况、缺哪个账号。"""
        try:
            report = traework.diagnose_local_auth(self.settings.get("traework_user_dir") or None)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"体检失败: {exc}", "details": [str(exc)]}
        details = list(report.get("details") or [])
        watch = self.traework_watch.status()
        details.append(
            f"自动捕获：{'运行中' if watch['running'] else '已停止'}，"
            f"累计入库 {watch['captured']} 次"
            + (f"，最近 {watch['last_at']}：{watch['last_message']}" if watch.get("last_at") else "")
        )
        self._append_log(
            f"[Trae体检] 数据目录 {len(report.get('dirs') or [])} 个；"
            f"可提取 {len(report.get('collected') or [])} 个账号；"
            f"仅剩记录（token 已被覆盖） {len(report.get('missing') or [])} 个"
        )
        if report.get("missing"):
            self._append_log(f"[Trae体检] 已被覆盖的账号：{', '.join(report['missing'])}")
        return {
            "ok": bool(report.get("ok")),
            "message": (
                f"可提取 {len(report.get('collected') or [])} 个账号；"
                f"{len(report.get('missing') or [])} 个历史账号只剩记录（token 已被覆盖）"
            ),
            "report": report,
            "watch": watch,
            "details": details,
        }

    def traework_watch_status(self) -> dict[str, Any]:
        return {"ok": True, "watch": self.traework_watch.status()}

    def notices(self) -> dict[str, Any]:
        """拉取服务器未读通知（账号异常 + 运营公告），前端据此弹窗。"""
        try:
            res = server_client.fetch_notices()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "items": [], "message": str(exc)}
        data = res.get("data")
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            items = []
        return {"ok": bool(res.get("ok")), "items": items, "message": res.get("message") or ""}

    def ack_notices(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """标记通知已读（之后不再弹窗）。"""
        payload = payload or {}
        keys = [str(k) for k in (payload.get("keys") or [])]
        all_read = bool(payload.get("all"))
        try:
            res = server_client.ack_notices(keys, all_read=all_read)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": str(exc)}
        if res.get("ok") and (keys or all_read):
            self._append_log(f"已读通知 {len(keys) if keys else '全部'}")
        return {"ok": bool(res.get("ok")), "message": res.get("message") or "", "data": res.get("data")}

    def paste_trae_token(self, token: str = "", user_id: str = "") -> dict[str, Any]:
        ok, msg = ensure_licensed(self.settings, force_online=True)
        if not ok:
            return {"ok": False, "message": msg}
        token = (token or "").strip()
        if not token:
            return {"ok": False, "message": "token 不能为空"}
        headers = traework.load_device_headers()
        auth, _ = traework.load_local_auth(self.settings.get("traework_user_dir") or None)
        blob = {
            "provider": "traework",
            "token": token,
            "access_token": token,
            "machine_id": (auth or {}).get("machine_id") or headers.get("X-Machine-Id") or "",
            "device_id": (auth or {}).get("device_id") or headers.get("X-Device-Id") or "",
            "user_id": (user_id or "").strip() or (auth or {}).get("user_id") or "",
            "ug_api_base": self.settings.get("traework_ug_api_base") or "",
            "token_hint": "已手工粘贴（内容已隐藏）",
        }
        if not blob["machine_id"] or not blob["device_id"]:
            return {"ok": False, "message": "缺少设备头，请先打开一次 TraeWork"}
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
            # 单条手工导入：把额度原因（code/seats/used）原样交给界面，由它引导升档，
            # 而不是抛异常让 JS 侧收到一句「未处理的异常」。
            return dict(stored)
        self._append_log("已手工导入 TraeWork token")
        return {"ok": True, "message": "已导入"}

    def paste_workbuddy_intl_token(self, token: str = "", uid: str = "") -> dict[str, Any]:
        """手工导入 WorkBuddy 国际版 Bearer token。

        国际版本机登录态（workbuddy-desktop-ai.info）里的 accessToken 被
        ``$wbEncrypted`` 信封加密，无法像 CN 那样自动采集，只能由用户从客户端
        的 ``/v2/billing/meter/*`` 请求里复制 Authorization 头和 X-User-Id 粘进来。
        """
        ok, msg = ensure_licensed(self.settings, force_online=True)
        if not ok:
            return {"ok": False, "message": msg}
        token = (token or "").strip()
        uid = (uid or "").strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        if not token:
            return {"ok": False, "message": "token 不能为空"}
        if not uid:
            return {"ok": False, "message": "国际版需要 uid（X-User-Id），不能为空"}
        blob = {
            "provider": "workbuddy_intl",
            "access_token": token,
            "token": token,
            "uid": uid,
            "token_hint": "已手工粘贴（内容已隐藏）",
        }
        stored = account_store.try_upsert_account(
            {
                "provider": "workbuddy_intl",
                "label": uid,
                "identity": uid,
                "run_mode": "local",
                "enabled": True,
                "token_blob": blob,
            }
        )
        if not stored.get("ok"):
            return dict(stored)
        self._append_log("已手工导入 WorkBuddy 国际版 token")
        return {"ok": True, "message": "已导入"}

    def capture_qoder(self) -> dict[str, Any]:
        """从本机 Qoder 客户端解密登录态并入库（auth.v1.dat，OSCrypt v10）。"""
        ok, msg = ensure_licensed(self.settings, force_online=True)
        if not ok:
            return {"ok": False, "message": msg}
        auth, err = qoder.load_local_auth()
        if not auth:
            return {"ok": False, "message": err or "无登录态"}
        label = str(auth.get("nickname") or auth.get("uid") or "qoder")
        stored = account_store.try_upsert_account(
            {
                "provider": "qoder",
                "label": label,
                "identity": auth.get("uid"),
                "run_mode": "local",
                "enabled": True,
                "token_blob": auth,
                "last_error": "",
            }
        )
        if not stored.get("ok"):
            self._append_log(f"Qoder 未入库：{stored.get('message')}")
            return dict(stored)
        self._append_log(f"已入库 Qoder：{label}")
        suggestion = self._get_replacement_suggestion(stored.get("account") or {})
        result = {"ok": True, "message": f"已导入 {label}", "count": 1}
        if suggestion:
            result["replacement_suggestion"] = suggestion
        return result

    def paste_qoder_token(self, token: str = "", uid: str = "") -> dict[str, Any]:
        """手工导入 Qoder Bearer token（本机解密失败时的兜底，如非 Windows）。"""
        ok, msg = ensure_licensed(self.settings, force_online=True)
        if not ok:
            return {"ok": False, "message": msg}
        token = (token or "").strip()
        uid = (uid or "").strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        if not token:
            return {"ok": False, "message": "token 不能为空"}
        if not uid:
            return {"ok": False, "message": "Qoder 需要 uid（user.id），不能为空"}
        blob = {
            "provider": "qoder",
            "access_token": token,
            "token": token,
            "uid": uid,
            "token_hint": "已手工粘贴（内容已隐藏）",
        }
        stored = account_store.try_upsert_account(
            {
                "provider": "qoder",
                "label": uid,
                "identity": uid,
                "run_mode": "local",
                "enabled": True,
                "token_blob": blob,
            }
        )
        if not stored.get("ok"):
            return dict(stored)
        self._append_log("已手工导入 Qoder token")
        return {"ok": True, "message": "已导入"}

    def import_credential(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        ok, msg = ensure_licensed(self.settings, force_online=True)
        if not ok:
            return {"ok": False, "message": msg}
        payload = payload or {}
        try:
            row = credential_store.upsert_credential(
                provider=str(payload.get("provider") or ""),
                username=str(payload.get("username") or ""),
                password=str(payload.get("password") or ""),
                label=str(payload.get("label") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": str(exc)}
        self._append_log(f"已导入账密 {row.get('provider')}/{row.get('username')}")
        login_now = bool(payload.get("login_now", True))
        if login_now:
            busy = self._start_job(
                "账密登录",
                lambda: login_by_id(str(row["id"]), headed=True, log=self._append_log),
            )
            if busy:
                return busy
            return {"ok": True, "message": "已保存，正在后台登录…", "id": row.get("id")}
        return {"ok": True, "message": "已保存", "id": row.get("id")}

    def import_credentials_batch(self, text: str = "") -> dict[str, Any]:
        ok, msg = ensure_licensed(self.settings, force_online=True)
        if not ok:
            return {"ok": False, "message": msg}
        ids: list[str] = []
        try:
            for line in (text or "").splitlines():
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
                ids.append(str(row["id"]))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": str(exc)}
        self._append_log(f"批量导入账密 {len(ids)} 条")
        return {"ok": True, "ids": ids, "message": f"已导入 {len(ids)} 条"}

    def unified_login(self, credential_id: str = "") -> dict[str, Any]:
        ok, msg = ensure_licensed(self.settings, force_online=True)
        if not ok:
            return {"ok": False, "message": msg}
        ids: list[str]
        if credential_id:
            ids = [str(credential_id)]
        else:
            ids = [str(r["id"]) for r in credential_store.load_credentials() if r.get("id")]
        if not ids:
            return {"ok": False, "message": "没有可登录的账密"}
        self._append_log(f"后台统一登录启动，共 {len(ids)} 条…")

        def worker() -> None:
            for cid in ids:
                try:
                    result = login_by_id(cid, headed=True, log=self._append_log)
                    if result.needs_manual and not result.ok:
                        self._append_log("→ 请改用采集或粘贴 token")
                except Exception as exc:  # noqa: BLE001
                    self._append_log(f"统一登录异常：{exc}")

        busy = self._start_job("账密统一登录", worker)
        if busy:
            return busy
        return {"ok": True, "message": f"已启动 {len(ids)} 条登录"}

    def delete_credential(self, credential_id: str = "") -> dict[str, Any]:
        if not credential_store.delete_credential(str(credential_id)):
            return {"ok": False, "message": "未找到账密"}
        return {"ok": True, "message": "已删除"}

    def set_account_mode(self, account_id: str = "", mode: str = "local") -> dict[str, Any]:
        result = account_store.set_run_mode(account_id, mode)
        if result.get("ok"):
            self._append_log(str(result.get("message") or ""))
        if str(mode or "").strip() == "server":
            notice = self._bind_hint()
            if notice:
                result["notice"] = notice
        return result

    def delete_account(self, account_id: str = "") -> dict[str, Any]:
        result = account_store.delete_account_with_server(account_id, log=self._append_log)
        if not result.get("ok"):
            self._append_log(str(result.get("message") or ""))
        return result

    def _find_account(self, account_id: str) -> dict[str, Any] | None:
        if not account_id:
            return None
        return next(
            (a for a in account_store.load_accounts() if str(a.get("id")) == str(account_id)), None
        )

    def run_local_now(self, account_id: str = "") -> dict[str, Any]:
        """本机签到。``account_id`` 非空时只跑那一个号（列表行内「签到」）。"""
        label = "本机签到"
        if account_id:
            account = self._find_account(account_id)
            if account is None:
                return {"ok": False, "message": "找不到该账号，可能已删除"}
            if str(account.get("run_mode") or "local") != "local":
                return {"ok": False, "message": "该账号是服务器代跑模式，本机不跑"}
            if not account.get("enabled", True):
                return {"ok": False, "message": "该账号已停用，先启用再签到"}
            label = f"本机签到 {account.get('label') or account.get('id')}"
        # 定时调度那一轮也在用同一批 token，它不占界面的任务名，只能看全局槽
        if credit_slot_busy():
            return {"ok": False, "busy": True, "message": "已有签到/同步任务在跑，请等待完成"}
        self._append_log(f"开始{label}…")

        def worker() -> None:
            results = run_local_all(
                require_license=True, log=self._append_log, account_id=str(account_id or "")
            )
            self._append_log(f"{label}完成，共 {len(results)} 条")

        # 和整批签到共用任务名：同一批 token 并发问供应商只会换来限流
        busy = self._start_job("本机签到", worker)
        if busy:
            return busy
        return {"ok": True, "message": f"已开始{label}"}

    def refresh_account(self, account_id: str = "") -> dict[str, Any]:
        """单个账号刷新积分（列表行内「刷新」）。"""
        account = self._find_account(account_id)
        if account is None:
            return {"ok": False, "message": "找不到该账号，可能已删除"}
        blob = account.get("token_blob") if isinstance(account.get("token_blob"), dict) else {}
        if not (blob.get("token") or blob.get("access_token")):
            return {"ok": False, "message": "本机没有该账号的凭证（仅服务器代跑记录），查不了积分"}
        if self._job_running() or credit_slot_busy():
            return {"ok": False, "busy": True, "message": "已有任务在执行（签到/登录/同步），请等待完成"}
        label = str(account.get("label") or account.get("id"))

        def worker() -> None:
            # 抢全局积分查询槽：自动同步那一路也在问同一个号，并发只会被供应商限流
            if not try_acquire_credit_slot():
                self._append_log(f"积分刷新跳过 {label}：已有同步任务在执行")
                return
            try:
                info = refresh_account_credits(account)
            except Exception as exc:  # noqa: BLE001 - 后台线程里没人接异常
                self._append_log(f"积分刷新失败 {label}：{mask_text(exc, 160)}")
                return
            finally:
                release_credit_slot()
            if info.get("ok"):
                self._append_log(
                    f"积分刷新 {label}：{info.get('credits') if info.get('credits') is not None else '-'}"
                    + (f" · 连签 {info.get('streak')}" if info.get("streak") is not None else "")
                )

        busy = self._start_job("积分刷新", worker)
        if busy:
            return busy
        return {"ok": True, "message": f"正在刷新 {label} 的积分"}

    def sync_now(self) -> dict[str, Any]:
        """「立即同步」：拉一次服务器代跑状态 + 查本机账号积分。"""

        return self._start_sync("同步")

    def refresh_credits(self) -> dict[str, Any]:
        """「刷新积分」与「立即同步」同一条路径：共用任务名和串行槽，
        否则两个入口能对同一批账号并发问供应商。"""

        return self._start_sync("积分刷新")

    def _start_sync(self, label: str) -> dict[str, Any]:
        # 手动同步和签到/批量登录都要用同一批 token，和自动同步一样让路，不并行
        if self._job_running() or credit_slot_busy():
            return {"ok": False, "busy": True, "message": "已有任务在执行（签到/登录/同步），请等待完成"}

        def worker() -> None:
            result = self.auto_sync.sync_once(reason="手动")
            if not result.get("ok"):
                self._append_log(result.get("message") or "同步失败")

        busy = self._start_job("同步", worker)
        if busy:
            return busy
        return {"ok": True, "message": f"正在{label}…"}

    def upload_delegate(self, account_id: str = "") -> dict[str, Any]:
        ok, msg = ensure_licensed(self.settings, force_online=True)
        if not ok:
            return {"ok": False, "message": msg}
        notice = self._bind_hint()
        accounts = account_store.load_accounts()
        targets = [a for a in accounts if (not account_id or str(a.get("id")) == str(account_id))]

        def worker() -> None:
            delegate.upload_delegate_accounts(
                targets,
                task_enabled=self._settings_snapshot().get("workbuddy_task_mode") == "server",
                log=self._append_log,
            )

        busy = self._start_job("代跑上传", worker)
        if busy:
            return busy
        out: dict[str, Any] = {"ok": True, "message": "正在上传代跑"}
        if notice:
            out["notice"] = notice
        return out

    def sync_server_credentials(self) -> dict[str, Any]:
        """手动触发一次「代跑凭证保鲜」：本机续期后回传服务器。"""
        ok, msg = ensure_licensed(self.settings, force_online=False)
        if not ok:
            return {"ok": False, "message": msg}

        def worker() -> None:
            results = refresh_server_credentials(log=self._append_log)
            if results and all(r.get("busy") for r in results):
                return  # 让路原因已经写进调度日志
            if not results:
                self._append_log("没有处于代跑模式的账号")
                return
            synced = sum(1 for r in results if r.get("synced"))
            refreshed = sum(1 for r in results if r.get("token_refreshed"))
            self._append_log(
                f"代跑凭证同步完成：检查 {len(results)} 个账号，刷新 token {refreshed} 个，回传 {synced} 个"
            )

        busy = self._start_job("代跑凭证同步", worker)
        if busy:
            return busy
        return {"ok": True, "message": "正在同步代跑凭证"}

    def run_workbuddy_tasks(self, account_id: str = "") -> dict[str, Any]:
        """立即执行 WorkBuddy 成长中心任务（本机模式）。"""
        ok, msg = ensure_licensed(self.settings, force_online=False)
        if not ok:
            return {"ok": False, "message": msg}
        mode = self.settings.get("workbuddy_task_mode") or "off"
        if mode == "server":
            return {
                "ok": False,
                "message": "当前是「服务器代跑」模式，任务由服务器在每日代跑时执行，本机不重复跑",
            }

        def worker() -> None:
            if account_id:
                accounts = account_store.load_accounts()
                target = next(
                    (a for a in accounts if str(a.get("id")) == str(account_id)), None
                )
                if not target:
                    self._append_log("未找到该账号")
                    return
                result = run_workbuddy_tasks(target, log=self._append_log, force=True)
                self._append_log(result.get("message") or str(result))
                return
            results = run_workbuddy_tasks_all(log=self._append_log, force=True)
            if results and all(r.get("busy") for r in results):
                return  # 供应商槽被签到/同步占着，原因已在调度日志里
            if not results:
                self._append_log("没有可执行的 WorkBuddy 账号（需为本机模式且已启用）")
                return
            done = sum(1 for r in results if r.get("ok"))
            self._append_log(f"成长任务执行完成：{done}/{len(results)} 个账号")

        busy = self._start_job("WorkBuddy 成长任务", worker)
        if busy:
            return busy
        return {"ok": True, "message": "正在执行 WorkBuddy 成长任务"}

    def replace_server_account(self, old_account_id: str, new_account_id: str) -> dict[str, Any]:
        """更换服务器代跑账号：实现与 tkinter 端共用 ``checkin_tool.delegate``。"""

        self._bind_hint()
        return delegate.replace_server_account(
            old_account_id, new_account_id, log=self._append_log
        )

    def _get_replacement_suggestion(self, new_local_account: dict[str, Any]) -> dict[str, Any] | None:
        """
        根据新入库的本地账号，查找是否存在同服务商的、且处于服务器代跑模式的旧账号，
        并返回替换建议。
        """
        if new_local_account.get("run_mode") == "server":
            return None # 新账号已经是服务器模式，不需要替换建议

        all_accounts = account_store.load_accounts()
        new_provider = new_local_account.get("provider")
        new_identity = account_store.account_identity_key(new_local_account) # 使用 identity key 进行匹配

        # 查找是否存在与新账号同服务商且同 identity key 的服务器代跑账号
        # 这里的逻辑是：如果有一个旧的服务器账号与新账号“身份”相同，就提示替换
        for acc in all_accounts:
            if acc.get("run_mode") == "server" and \
               acc.get("provider") == new_provider and \
               account_store.account_identity_key(acc) == new_identity and \
               str(acc.get("id")) != str(new_local_account.get("id")): # 确保不是同一个账号ID
                return {
                    "old_account_id": str(acc.get("id")),
                    "old_account_label": str(acc.get("label") or acc.get("id")),
                    "new_account_id": str(new_local_account.get("id")),
                    "new_account_label": str(new_local_account.get("label") or new_local_account.get("id")),
                    "provider": new_provider
                }
        return None

    def run_server_now(self) -> dict[str, Any]:
        def worker() -> None:
            result = server_client.run_now_server()
            self._append_log(f"服务器代跑触发: {result}")
            runs = server_client.today_runs()
            self._append_log(f"代跑今日结果: {runs}")

        busy = self._start_job("服务器代跑", worker)
        if busy:
            return busy
        return {"ok": True, "message": "已触发服务器代跑"}

    def clear_live_logs(self) -> dict[str, Any]:
        account_store.clear_live_logs()
        with self._lock:
            self._logs.clear()
        return {"ok": True}

    def clear_run_logs(self) -> dict[str, Any]:
        account_store.clear_run_logs()
        return {"ok": True}

    def clear_credits(self) -> dict[str, Any]:
        account_store.clear_credit_history()
        return {"ok": True}

    def _bg(self, fn: Callable[[], None]) -> None:
        def run() -> None:
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 - 线程里抛异常会静默消失，表现为"点了没反应"
                self._append_log(f"后台任务异常：{exc}")

        threading.Thread(target=run, daemon=True).start()

    def _job_running(self) -> bool:
        with self._busy_lock:
            return bool(self._busy)

    def _start_job(self, name: str, fn: Callable[[], None]) -> dict[str, Any] | None:
        """同名任务只允许一个在跑；返回非 None 表示已有一个在执行。"""
        with self._busy_lock:
            if name in self._busy:
                return {"ok": False, "message": f"{name}仍在执行中，请等待完成"}
            self._busy.add(name)

        def wrapped() -> None:
            try:
                fn()
            finally:
                with self._busy_lock:
                    self._busy.discard(name)

        self._bg(wrapped)
        return None


def main() -> None:
    try:
        import webview
    except ImportError as exc:
        raise SystemExit(
            "缺少 pywebview。请执行：pip install pywebview\n"
            "或使用备用界面：python -m checkin_tool --native"
        ) from exc

    api = CheckinApi()
    html_path = _resource_path("ui", "index.html")
    if not html_path.exists():
        raise SystemExit(f"找不到界面文件：{html_path}")
    icon_path = _resource_path("ui", "icon.ico")
    if not icon_path.exists():
        icon_path = _resource_path("ui", "icon.png")
    url = html_path.resolve().as_uri()
    # 注意：icon 是 webview.start() 的参数，create_window() 不接受，
    # 传错会直接 TypeError 导致启动失败。
    webview.create_window(
        title=f"积分签到工具  v{__version__}",
        url=url,
        # 侧栏占了 ~200px，账号表 9 列要 ~900px 才不横向滚；
        # 980 宽的默认窗口一进「账号管理」就得拖着横向滚动条看操作列。
        width=1180,
        height=780,
        min_size=(980, 640),
        resizable=True,
        text_select=True,
        background_color="#eef3fa",
        js_api=api,
    )
    start_kwargs: dict[str, Any] = {"debug": "--debug" in sys.argv}
    if icon_path.exists():
        start_kwargs["icon"] = str(icon_path)
    try:
        try:
            webview.start(**start_kwargs)
        except TypeError:  # 旧版本 pywebview 不支持 icon 参数
            start_kwargs.pop("icon", None)
            webview.start(**start_kwargs)
    finally:
        api.stop_background()


if __name__ == "__main__":
    main()
