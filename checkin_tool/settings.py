# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path
from typing import Any

from .license_client import DEFAULT_APP_KEY, DEFAULT_BASE_URL, data_root
from .secure_storage import load_json, save_json

SETTINGS_FILE = data_root() / "settings.json"

GAP_SEC_MIN = 0
GAP_SEC_MAX = 600
GAP_SEC_DEFAULT = (20, 60)

# 设置项区间 → 默认值：网页端 collectSettings 与 tkinter 端各写一份钳制
# 就会漂移（一边把 0 当「没填」退回默认值，另一边允许 0）。两条前端都调这里。
_INT_FIELDS: dict[str, tuple[int, int, int]] = {
    "auto_sync_minutes": (1, 240, 5),
    "schedule_hour": (0, 23, 9),
    "schedule_minute": (0, 59, 10),
    "evening_hour": (0, 23, 20),
    "evening_minute": (0, 59, 0),
    "run_gap_min_sec": (GAP_SEC_MIN, GAP_SEC_MAX, GAP_SEC_DEFAULT[0]),
    "run_gap_max_sec": (GAP_SEC_MIN, GAP_SEC_MAX, GAP_SEC_DEFAULT[1]),
    "credit_low_threshold": (0, 1_000_000, 100),
    "backup_keep_count": (1, 100, 10),
}


def parse_int_field(raw: Any, key: str) -> int:
    """按 ``_INT_FIELDS`` 的区间读一个输入框的值。

    空 / 非数字回退默认值；``0`` 是合法值（间隔 0 秒、阈值 0 = 关闭提醒），
    不能用 ``Number(v) || default`` 那类写法把它吃掉。
    """
    low, high, default = _INT_FIELDS[key]
    text = str(raw if raw is not None else "").strip()
    if not text:
        return default
    try:
        value = int(float(text))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def ordered_gap(lo_raw: Any, hi_raw: Any) -> tuple[int, int]:
    """账号间随机间隔：先各自钳制，再把填反了的区间换回来。

    跑批时 ``scheduler._run_gap`` 也会兜底，但落盘就存反区间的话，
    另一个前端回显时会显示成「最小 60 秒、最大 20 秒」这种鬼样子。
    """
    low = parse_int_field(lo_raw, "run_gap_min_sec")
    high = parse_int_field(hi_raw, "run_gap_max_sec")
    return (low, high) if low <= high else (high, low)


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
        # 账号间随机签到间隔：原来硬编码 0.8~1.8 秒，等于所有号在同一秒内连续问供应商，
        # 风控特征明显；给一个可配的区间，默认 20~60 秒。
        "run_gap_min_sec": 20,
        "run_gap_max_sec": 60,
        # 积分低于该值时列表橙色提醒（仅 WorkBuddy 有积分口径）
        "credit_low_threshold": 100,
        # 备份轮转：本机备份目录只保留最近多少份，超出删最旧（凭证绑机器，仅同机可恢复）
        "backup_keep_count": 10,
        "auto_schedule": True,
        # 当天所有签到窗口都过完时（机器 20 点后才开机、或窗口设在凌晨），
        # 启动后补跑一次；关掉就只在窗口内跑。
        "catchup_on_start": True,
        # 打开程序即同步，之后定时拉服务器状态与本机账号积分（界面 30 秒重读视图）
        "auto_sync": True,
        "auto_sync_minutes": 5,
        # 默认开：这台机器曾经连续三天在签到窗口之后才开机，而自启是关的，
        # 结果谁都没签、界面上还显示一切正常。
        "autostart": True,
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
