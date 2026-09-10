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
from .scheduler import DailyScheduler, refresh_account_credits, run_local_all
from .settings import load_settings, save_settings


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
            },
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
        auth, err = workbuddy.load_local_auth(self.settings.get("workbuddy_auth_path") or None)
        if err or not auth:
            return {"ok": False, "message": err or "无登录态"}
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
        self._append_log(f"已采集 WorkBuddy：{auth.get('nickname') or auth.get('uid')}")
        return {"ok": True, "message": "采集成功"}

    def capture_traework(self) -> dict[str, Any]:
        ok, msg = ensure_licensed(self.settings, force_online=True)
        if not ok:
            return {"ok": False, "message": msg}
        auth, err = traework.load_local_auth(self.settings.get("traework_user_dir") or None)
        if not auth:
            return {"ok": False, "message": err or "无登录态"}
        note = ""
        if not auth.get("token"):
            note = "设备头已读到，但 token 需粘贴"
        if self.settings.get("traework_ug_api_base"):
            auth["ug_api_base"] = self.settings["traework_ug_api_base"]
        account_store.upsert_account(
            {
                "provider": "traework",
                "label": auth.get("user_id") or "traework",
                "identity": auth.get("user_id") or auth.get("auth_key") or "traework",
                "run_mode": "local",
                "enabled": bool(auth.get("token")),
                "token_blob": auth,
                "last_error": "" if auth.get("token") else (err or "缺少 token"),
            }
        )
        self._append_log("已采集 TraeWork" + (f"（{note}）" if note else ""))
        return {"ok": True, "message": note or "采集成功", "need_token": not bool(auth.get("token"))}

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
                account_store.upsert_account(account)
                result = server_client.upsert_server_account(account)
                self._append_log(f"上传代跑 {account.get('provider')}: {result.get('message') or result}")

        self._bg(worker)
        return {"ok": True, "message": "正在上传代跑"}

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
    window_kwargs = dict(
        title=f"积分签到工具  v{__version__}",
        url=url,
        width=920,
        height=680,
        min_size=(760, 560),
        resizable=True,
        text_select=True,
        js_api=api,
    )
    if icon_path.exists():
        window_kwargs["icon"] = str(icon_path)
    webview.create_window(**window_kwargs)
    webview.start(debug=False)


if __name__ == "__main__":
    main()
