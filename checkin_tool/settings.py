# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path
from typing import Any

from .license_client import DEFAULT_APP_KEY, DEFAULT_BASE_URL, data_root
from .secure_storage import load_json, save_json

SETTINGS_FILE = data_root() / "settings.json"


def default_settings() -> dict[str, Any]:
    return {
        "license_base_url": DEFAULT_BASE_URL,
        "license_app_key": DEFAULT_APP_KEY,
        "license_timeout": 15,
        "prefer_crypto": False,
        # 授权/代跑服务地址：默认允许明文 HTTP（当前服务端尚无 TLS），
        # 服务端配好证书后置 False 即强制 https。
        "allow_insecure_transport": True,
        # 加密信封失败时是否允许退回明文请求，默认禁止（防降级）
        "allow_plain_fallback": False,
        "license_check_interval": 300,
        "card_code": "",
        "schedule_hour": 9,
        "schedule_minute": 10,
        "evening_schedule": True,
        "evening_hour": 20,
        "evening_minute": 0,
        "schedule_jitter_sec": 120,
        "schedule_window_sec": 7200,
        "auto_schedule": True,
        # 打开程序即同步，之后定时拉服务器状态与本机账号积分（界面 30 秒重读视图）
        "auto_sync": True,
        "auto_sync_minutes": 5,
        "autostart": False,
        "minimize_to_tray": True,
        "workbuddy_auth_path": "",
        "traework_user_dir": "",
        "traework_ug_api_base": "",
        # Trae CN 只保留最后登录的那个账号，切号即覆盖 → 默认开启实时捕获
        "traework_auto_capture": True,
        "traework_watch_interval": 3,
        # WorkBuddy 成长中心任务：off=不做 / local=本机做 / server=服务器代跑
        "workbuddy_task_mode": "off",
        # 是否执行需要真实 AI 对话的任务（和AI聊天5次 / GLM对话 / 专家团 / 夜猫子）
        "workbuddy_chat_tasks": True,
    }


def load_settings() -> dict[str, Any]:
    data = default_settings()
    try:
        stored = load_json(SETTINGS_FILE, {})
        if isinstance(stored, dict):
            data.update(stored)
    except Exception:
        pass
    return data


def save_settings(settings: dict[str, Any]) -> None:
    merged = default_settings()
    merged.update(settings or {})
    save_json(SETTINGS_FILE, merged)


def settings_path() -> Path:
    return SETTINGS_FILE
