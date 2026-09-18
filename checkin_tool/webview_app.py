# -*- coding: utf-8 -*-
"""PyWebView 桌面壳：简约 HTML 界面（对齐 Cursor 工具交付形态）。"""

from __future__ import annotations

import json
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
import zipfile

from . import __version__, account_store, autostart, credential_store, server_client
from .adapters import traework, workbuddy
from .license_client import check_status, clear_cache, ensure_licensed, redeem
from .license_guard import LicenseGuard
from .login import login_by_id
from .scheduler import (
    DailyScheduler,
    refresh_account_credits,
    refresh_server_credentials,
    run_local_all,
    run_workbuddy_tasks,
    run_workbuddy_tasks_all,
)
from .settings import load_settings, save_settings
from .traework_watcher import TraeWorkAutoCapture


def _resource_path(*parts: str) -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        base = Path(sys._MEIPASS)  # type: ignore[attr-defined]
    else:
        base = Path(__file__).resolve().parent
    return base.joinpath(*parts)


class CheckinApi:
    def __init__(self) -> None:
        self.settings = load_settings()
        self._logs: list[str] = []
        self._lock = threading.Lock()
        self.scheduler = DailyScheduler(log=self._append_log)
        if self.settings.get("auto_schedule", True):
            self.scheduler.start()
            self._append_log("已启动本机日签调度")
        # Trae CN 只在本机保留"最后登录的那一个"账号，切号即覆盖 → 必须实时抓
        self.traework_watch = TraeWorkAutoCapture(
            self._on_traework_captured,
            log=self._append_log,
            interval=float(self.settings.get("traework_watch_interval") or 3),
            get_user_dir=lambda: self.settings.get("traework_user_dir") or "",
        )
        if self.settings.get("traework_auto_capture", True):
            self.traework_watch.start()
            self._append_log("已启用 Trae CN 登录态自动捕获（登录后无需手动采集）")
        # 授权守卫：定时在线校验，卡密失效立即踢出登录
        self._revoked: dict[str, Any] | None = None
        check_interval = float(self.settings.get("license_check_interval") or 300)
        self.license_guard = LicenseGuard(
            get_settings=lambda: self.settings,
            on_revoked=self._on_license_revoked,
            log=self._append_log,
            interval_sec=check_interval,
        )
        self.license_guard.start()
        self._append_log(
            f"已启用授权守卫（每 {int(check_interval / 60) or 5} 分钟在线校验，卡密失效自动退出登录）"
        )

    def _on_license_revoked(self, reason: str) -> None:
        """卡密失效：踢出登录 —— 停后台任务 + 清本机票据 + 前端弹回激活门禁。"""
        self._revoked = {"reason": reason, "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        for name, stop in (
            ("自动签到调度", self.scheduler.stop),
            ("Trae 登录态捕获", self.traework_watch.stop),
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

    def _append_log(self, msg: str) -> None:
        text = str(msg).rstrip()
        account_store.append_live_log(text)
        with self._lock:
            self._logs.append(text)
            self._logs = self._logs[-400:]

    def version(self) -> dict[str, Any]:
        return {"ok": True, "version": __version__}

    def get_bootstrap(self) -> dict[str, Any]:
        license_info = check_status(self.settings, force_online=False)
        return {
            "ok": True,
            "version": __version__,
            "settings": {
                "license_base_url": self.settings.get("license_base_url") or "",
                "card_code": self.settings.get("card_code") or "",
                "autostart": bool(self.settings.get("autostart")),
                "auto_schedule": bool(self.settings.get("auto_schedule", True)),
                "evening_schedule": bool(self.settings.get("evening_schedule", True)),
                "traework_auto_capture": bool(self.settings.get("traework_auto_capture", True)),
                "traework_user_dir": self.settings.get("traework_user_dir") or "",
                "workbuddy_task_mode": self.settings.get("workbuddy_task_mode") or "off",
                "workbuddy_chat_tasks": bool(self.settings.get("workbuddy_chat_tasks", True)),
            },
            "traework_watch": self.traework_watch.status(),
            "license": license_info,
            "revoked": self._revoked,  # 非空 = 卡密已失效被踢出，前端弹回门禁并提示
            "accountUsage": account_store.get_account_usage(),
            "licenseGuard": self.license_guard.status(),
            "board": account_store.today_board(),
            "accounts": self.list_accounts().get("accounts") or [],
            "logs": list(reversed(account_store.load_live_logs(120))),

            "credentials": [
                credential_store.public_credential_view(r) for r in credential_store.load_credentials()
            ],
        }

    def backup_data(self) -> dict[str, Any]:
        """备份所有数据文件到一个zip文件。"""
        try:
            backup_dir = account_store.data_root() / "backup"
            backup_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            backup_filename = f"checkintool_backup_{timestamp}.zip"
            backup_path = backup_dir / backup_filename

            files_to_backup = [
                account_store.ACCOUNTS_FILE,
                account_store.RUN_LOG_FILE,
                account_store.LIVE_LOG_FILE,
                account_store.CREDIT_HISTORY_FILE,
                license_client.LICENSE_CACHE,
                license_client.DEVICE_ID_FILE,
            ]

            with zipfile.ZipFile(backup_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for file_path in files_to_backup:
                    if file_path.exists():
                        zipf.write(file_path, arcname=file_path.name)
            
            self._append_log(f"数据已备份到: {backup_path}")
            return {"ok": True, "message": f"数据已备份到: {backup_path}", "path": str(backup_path)}
        except Exception as exc:
            self._append_log(f"数据备份失败: {exc}")
            return {"ok": False, "message": f"数据备份失败: {exc}"}

    def restore_data(self, backup_file_path: str) -> dict[str, Any]:
        """从备份文件恢复数据。"""
        try:
            backup_path = Path(backup_file_path)
            if not backup_path.is_file():
                return {"ok": False, "message": "备份文件不存在。"}
            
            data_root_path = account_store.data_root()

            with zipfile.ZipFile(backup_path, 'r') as zipf:
                for member in zipf.namelist():
                    # 确保只解压到数据根目录，防止路径遍历攻击
                    member_path = Path(data_root_path) / Path(member).name
                    # 避免恢复license_cache.json，因为它可能包含敏感信息且在恢复后应该重新验证
                    # 避免恢复device_id.txt，因为它与设备绑定，恢复后可能导致设备数统计问题
                    if member_path.name in ["license_cache.json", "device_id.txt"]:
                        self._append_log(f"跳过恢复敏感文件: {member_path.name}")
                        continue
                    
                    # 提取文件，目标路径是data_root_path
                    # zipfile.extract() 默认会将文件提取到当前工作目录，这里需要指定path参数
                    # 为了避免路径遍历漏洞，我们只提取文件名，并确保目标路径在data_root_path内
                    extracted_file_path = data_root_path / Path(member).name
                    with open(extracted_file_path, "wb") as outfile:
                        outfile.write(zipf.read(member))
            
            self._append_log(f"数据已从 {backup_file_path} 恢复。请重启应用以使更改生效。")
            return {"ok": True, "message": "数据已恢复。请重启应用以使更改生效。"}
        except Exception as exc:
            self._append_log(f"数据恢复失败: {exc}")
            return {"ok": False, "message": f"数据恢复失败: {exc}"}

    def poll_logs(self) -> dict[str, Any]:
        with self._lock:
            lines = list(self._logs)
            self._logs.clear()
        return {"ok": True, "lines": lines}

    def list_accounts(self) -> dict[str, Any]:
        today = account_store.today_run_map()
        rows = [account_store.public_account_view(a, today) for a in account_store.load_accounts()]
        return {"ok": True, "accounts": rows}

    def today_board(self) -> dict[str, Any]:
        return {"ok": True, "board": account_store.today_board()}

    def credit_history(self, account_id: str | None = None) -> dict[str, Any]:
        """获取积分历史，支持按账号ID筛选。"""
        return {"ok": True, "items": account_store.load_credit_history(account_id=account_id, limit=200)}

    def run_logs(self, account_id: str | None = None) -> dict[str, Any]:
        """获取运行日志，支持按账号ID筛选。"""
        return {"ok": True, "items": account_store.load_run_logs(account_id=account_id, limit=200)}

    def list_credentials(self) -> dict[str, Any]:
        return {
            "ok": True,
            "credentials": [
                credential_store.public_credential_view(r) for r in credential_store.load_credentials()
            ],
        }

    def refresh_license(self) -> dict[str, Any]:
        result = check_status(self.settings, force_online=True)
        self._append_log(f"授权: valid={result.get('valid')} {result.get('message')}")
        return {"ok": True, "license": result, "revoked": self._revoked}

    def license_check_now(self) -> dict[str, Any]:
        """手动触发一次授权校验（含踢出判定），供前端「立即校验」按钮使用。"""
        out = self.license_guard.check_now()
        return {"ok": True, **out, "revoked": self._revoked}

    def license_guard_status(self) -> dict[str, Any]:
        return {"ok": True, "guard": self.license_guard.status(), "revoked": self._revoked}

    def account_usage(self) -> dict[str, Any]:
        """套餐与账号用量：{limit, used, remain, planLabel, expireAt}。"""
        return {"ok": True, "usage": account_store.get_account_usage()}

    def save_settings(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        for key in (
            "license_base_url",
            "card_code",
            "autostart",
            "auto_schedule",
            "evening_schedule",
            "traework_auto_capture",
            "traework_user_dir",
            "traework_ug_api_base",
            "workbuddy_task_mode",
            "workbuddy_chat_tasks",
        ):
            if key in payload:
                self.settings[key] = payload[key]
        save_settings(self.settings)
        try:
            autostart.set_enabled(bool(self.settings.get("autostart")))
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"开机自启失败: {exc}")
        if self.settings.get("auto_schedule"):
            self.scheduler.start()
        else:
            self.scheduler.stop()
        # Trae 自动捕获开关即时生效
        if self.settings.get("traework_auto_capture", True):
            if not self.traework_watch.running:
                self.traework_watch.start()
                self._append_log("已启用 Trae CN 登录态自动捕获")
        else:
            self.traework_watch.stop()
        return {"ok": True, "message": "设置已保存"}

    def redeem(self, card_code: str = "") -> dict[str, Any]:
        code = (card_code or self.settings.get("card_code") or "").strip()
        if not code:
            return {"ok": False, "message": "请填写卡密"}
        self.settings["card_code"] = code
        save_settings(self.settings)
        result = redeem(self.settings, code)
        self._append_log(f"激活: {result.get('message') or result}")
        return {"ok": bool(result.get("ok") or result.get("valid")), "result": result, "message": result.get("message")}

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
            upserted_account = account_store.upsert_account(account_data)
            labels.append(auth.get("nickname") or auth.get("uid"))
            
            # 获取替换建议
            if not replacement_suggestion: # 只取第一个账号的建议
                suggestion = self._get_replacement_suggestion(upserted_account)
                if suggestion:
                    replacement_suggestion = suggestion

        self._append_log(f"已采集 WorkBuddy {len(auths)} 个账号：{', '.join(str(x) for x in labels)}")
        
        result = {"ok": True, "message": f"采集成功（{len(auths)} 个账号）", "count": len(auths)}
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
                auth["user_tag"] = tags[uid]
                account_data["token_blob"]["user_tag"] = tags[uid]
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
            upserted_account = account_store.upsert_account(account_data)
            stored += 1
            identities.append(str(auth.get("user_id") or auth.get("auth_key") or "traework"))
            saved.append(f"{uid or '未知'}({auth.get('user_region') or '?'})" + (f" {note}" if note else ""))

            # 获取替换建议
            if not replacement_suggestion: # 只取第一个账号的建议
                suggestion = self._get_replacement_suggestion(upserted_account)
                if suggestion:
                    replacement_suggestion = suggestion

        result = {
            "stored": stored,
            "enabled_count": enabled_count,
            "need_token": need_token,
            "saved": saved,
            "identities": identities,
        }
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
        self._append_log("已手工导入 TraeWork token")
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
            self._bg(lambda: login_by_id(str(row["id"]), headed=True, log=self._append_log))
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

        self._bg(worker)
        return {"ok": True, "message": f"已启动 {len(ids)} 条登录"}

    def delete_credential(self, credential_id: str = "") -> dict[str, Any]:
        if not credential_store.delete_credential(str(credential_id)):
            return {"ok": False, "message": "未找到账密"}
        return {"ok": True, "message": "已删除"}

    def set_account_mode(self, account_id: str = "", mode: str = "local") -> dict[str, Any]:
        accounts = account_store.load_accounts()
        found = False
        for row in accounts:
            if str(row.get("id")) == str(account_id):
                row["run_mode"] = mode
                found = True
                break
        if not found:
            return {"ok": False, "message": "未找到账号"}
        account_store.save_accounts(accounts)
        return {"ok": True, "message": f"已设为 {mode}"}

    def delete_account(self, account_id: str = "") -> dict[str, Any]:
        # 首先加载所有账号，找到要删除的账号以便获取其 run_mode 和 server_account_id
        all_accounts = account_store.load_accounts()
        account_to_delete = next((a for a in all_accounts if str(a.get("id")) == str(account_id)), None)

        if not account_to_delete:
            return {"ok": False, "message": "未找到账号"}

        # 删除本地账号
        if not account_store.delete_account(str(account_id)):
            return {"ok": False, "message": "删除本地账号失败"}

        # 如果是服务器代跑账号，则尝试从服务器删除
        if account_to_delete.get("run_mode") == "server":
            server_id = account_to_delete.get("id") # 这里的id就是server_account_id
            if server_id:
                try:
                    server_del_result = server_client.delete_server_account(server_id)
                    if not server_del_result.get("ok"):
                        self._append_log(f"删除服务器代跑账号 {account_id} 失败: {server_del_result.get('message')}")
                        # 即使服务器删除失败，本地也已删除，可以根据需求决定是否回滚或仅记录日志
                        return {"ok": False, "message": f"本地账号已删除，但删除服务器账号失败: {server_del_result.get('message')}"}
                    else:
                        self._append_log(f"服务器代跑账号 {account_id} 已删除。")
                except Exception as exc:
                    self._append_log(f"调用服务器删除接口异常: {exc}")
                    return {"ok": False, "message": f"本地账号已删除，但调用服务器删除接口异常: {exc}"}

        return {"ok": True, "message": "已删除"}

    def run_local_now(self) -> dict[str, Any]:
        self._append_log("开始本机签到…")

        def worker() -> None:
            results = run_local_all(require_license=True, log=self._append_log)
            self._append_log(f"本机签到完成，共 {len(results)} 条")

        self._bg(worker)
        return {"ok": True, "message": "已开始本机签到"}

    def refresh_credits(self) -> dict[str, Any]:
        def worker() -> None:
            for account in account_store.load_accounts():
                if not account.get("enabled", True):
                    continue
                info = refresh_account_credits(account)
                self._append_log(
                    f"积分查询 {account.get('provider')}/{account.get('label')}: "
                    f"{info.get('message') or info}"
                )

        self._bg(worker)
        return {"ok": True, "message": "正在刷新积分"}

    def upload_delegate(self, account_id: str = "") -> dict[str, Any]:
        ok, msg = ensure_licensed(self.settings, force_online=True)
        if not ok:
            return {"ok": False, "message": msg}
        accounts = account_store.load_accounts()
        targets = [a for a in accounts if (not account_id or str(a.get("id")) == str(account_id))]

        def worker() -> None:
            for account in targets:
                blob = account.get("token_blob") or {}
                if not blob.get("token") and not blob.get("access_token"):
                    self._append_log(f"跳过无 token 账号 {account.get('id')}")
                    continue
                account["run_mode"] = "server"
                if str(account.get("provider")) == "workbuddy":
                    # 告诉服务器：这个账号代跑时要不要顺带做成长任务
                    account["task_enabled"] = self.settings.get("workbuddy_task_mode") == "server"
                account_store.upsert_account(account)
                result = server_client.sync_server_blob(account, log=self._append_log, force=True)
                if not result:
                    self._append_log(f"上传代跑 {account.get('provider')}: 无可用凭证，已跳过")

        self._bg(worker)
        return {"ok": True, "message": "正在上传代跑"}

    def sync_server_credentials(self) -> dict[str, Any]:
        """手动触发一次「代跑凭证保鲜」：本机续期后回传服务器。"""
        ok, msg = ensure_licensed(self.settings, force_online=False)
        if not ok:
            return {"ok": False, "message": msg}

        def worker() -> None:
            results = refresh_server_credentials(log=self._append_log)
            if not results:
                self._append_log("没有处于代跑模式的账号")
                return
            synced = sum(1 for r in results if r.get("synced"))
            refreshed = sum(1 for r in results if r.get("token_refreshed"))
            self._append_log(
                f"代跑凭证同步完成：检查 {len(results)} 个账号，刷新 token {refreshed} 个，回传 {synced} 个"
            )

        self._bg(worker)
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
            if not results:
                self._append_log("没有可执行的 WorkBuddy 账号（需为本机模式且已启用）")
                return
            done = sum(1 for r in results if r.get("ok"))
            self._append_log(f"成长任务执行完成：{done}/{len(results)} 个账号")

        self._bg(worker)
        return {"ok": True, "message": "正在执行 WorkBuddy 成长任务"}

    def replace_server_account(self, old_account_id: str, new_account_id: str) -> dict[str, Any]:
        self._append_log(f"尝试更换服务器代跑账号：旧账号ID={old_account_id}, 新账号ID={new_account_id}")
        
        # 1. 查找并验证旧账号
        all_accounts = account_store.load_accounts()
        old_account = next((a for a in all_accounts if str(a.get("id")) == str(old_account_id)), None)
        if not old_account:
            return {"ok": False, "message": f"未找到旧账号 {old_account_id}"}
        if old_account.get("run_mode") != "server":
            return {"ok": False, "message": f"旧账号 {old_account_id} 不是服务器代跑模式，无法更换"}

        # 2. 查找并验证新账号
        new_account = next((a for a in all_accounts if str(a.get("id")) == str(new_account_id)), None)
        if not new_account:
            return {"ok": False, "message": f"未找到新账号 {new_account_id}"}
        if new_account.get("run_mode") == "server":
            return {"ok": False, "message": f"新账号 {new_account_id} 已是服务器代跑模式，请选择本地账号进行更换"}
        
        # 3. 删除服务器上的旧账号
        try:
            self._append_log(f"正在删除服务器上的旧账号: {old_account_id}")
            server_del_result = server_client.delete_server_account(old_account_id)
            if not server_del_result.get("ok"):
                self._append_log(f"删除服务器旧账号 {old_account_id} 失败: {server_del_result.get('message')}")
                return {"ok": False, "message": f"删除服务器旧账号失败: {server_del_result.get('message')}"}
        except Exception as exc:
            self._append_log(f"调用服务器删除旧账号接口异常: {exc}")
            return {"ok": False, "message": f"调用服务器删除旧账号接口异常: {exc}"}

        # 4. 上传新的账号到服务器并更新本地状态
        try:
            self._append_log(f"正在上传新账号 {new_account_id} 到服务器")
            # 将新账号设置为服务器模式
            new_account["run_mode"] = "server"
            # 更新本地 account_store，因为它现在是服务器模式了
            account_store.upsert_account(new_account)
            
            # 同步到服务器
            server_sync_result = server_client.sync_server_blob(new_account, log=self._append_log, force=True)
            if not server_sync_result or not server_sync_result.get("ok"):
                # 如果同步失败，尝试回滚本地账号为本地模式 (可选，取决于业务逻辑)
                new_account["run_mode"] = "local"
                account_store.upsert_account(new_account)
                self._append_log(f"新账号 {new_account_id} 上传服务器失败: {server_sync_result.get('message') if server_sync_result else '未知错误'}")
                return {"ok": False, "message": f"新账号上传服务器失败: {server_sync_result.get('message') if server_sync_result else '未知错误'}"}
            
            self._append_log(f"账号 {old_account_id} 已成功更换为 {new_account_id} 并上传至服务器。")
            return {"ok": True, "message": f"账号 {old_account_id} 已成功更换为 {new_account_id}"}

        except Exception as exc:
            self._append_log(f"上传新账号到服务器异常: {exc}")
            # 同样，如果上传失败，尝试回滚本地账号为本地模式
            new_account["run_mode"] = "local"
            account_store.upsert_account(new_account)
            return {"ok": False, "message": f"上传新账号到服务器异常: {exc}"}

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

        self._bg(worker)
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
        threading.Thread(target=fn, daemon=True).start()


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
        width=980,
        height=720,
        min_size=(820, 600),
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
