# -*- coding: utf-8 -*-
"""PyWebView 桌面壳：简约 HTML 界面（对齐 Cursor 工具交付形态）。"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Any, Callable

from . import __version__, account_store, autostart, credential_store, server_client
from .adapters import traework, workbuddy
from .license_client import check_status, ensure_licensed, redeem
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

    def stop_background(self) -> None:
        try:
            self.traework_watch.stop()
        except Exception:
            pass
        try:
            self.scheduler.stop()
        except Exception:
            pass

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
            "board": account_store.today_board(),
            "accounts": self.list_accounts().get("accounts") or [],
            "logs": list(reversed(account_store.load_live_logs(120))),
            "credits": account_store.load_credit_history(limit=80),
            "credentials": [
                credential_store.public_credential_view(r) for r in credential_store.load_credentials()
            ],
        }

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

    def credit_history(self) -> dict[str, Any]:
        return {"ok": True, "items": account_store.load_credit_history(limit=200)}

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
        return {"ok": True, "license": result}

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
            labels.append(auth.get("nickname") or auth.get("uid"))
        self._append_log(f"已采集 WorkBuddy {len(auths)} 个账号：{', '.join(str(x) for x in labels)}")
        return {"ok": True, "message": f"采集成功（{len(auths)} 个账号）", "count": len(auths)}

    def _store_traework_auths(self, auths: list[dict[str, Any]]) -> dict[str, Any]:
        """把 TraeWork 登录态写入账号库（按 userId 累加，不会覆盖已采账号）。"""
        tags = traework.user_tags()
        saved: list[str] = []
        identities: list[str] = []
        stored = 0
        need_token = False
        enabled_count = 0
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
            if auth.get("needs_manual_token") and not auth.get("token"):
                continue  # 无 token 的占位项不入库，避免账号列表出现空账号
            account_store.upsert_account(
                {
                    "provider": "traework",
                    "label": auth.get("user_id") or auth.get("nickname") or "traework",
                    "identity": auth.get("user_id") or auth.get("auth_key") or "traework",
                    "run_mode": "local",
                    "enabled": bool(auth.get("token")) and not blocked,
                    "token_blob": auth,
                    "last_error": note,
                }
            )
            stored += 1
            identities.append(str(auth.get("user_id") or auth.get("auth_key") or "traework"))
            saved.append(f"{uid or '未知'}({auth.get('user_region') or '?'})" + (f" {note}" if note else ""))
        return {
            "stored": stored,
            "enabled_count": enabled_count,
            "need_token": need_token,
            "saved": saved,
            "identities": identities,
        }

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
        if not account_store.delete_account(str(account_id)):
            return {"ok": False, "message": "未找到账号"}
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
