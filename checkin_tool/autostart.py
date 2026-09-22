# -*- coding: utf-8 -*-
"""开机自启（当前用户 Run 注册表）。

注册表条目是「设置」的派生物，不是真相：exe 重装 / 挪目录后，Run 里留的仍是旧路径，
Windows 只会静默不启动，而界面复选框还亮着。所以打包态每次启动都对着当前路径核一遍
（`repair_on_startup`）。源码态一律不碰注册表——那时写进去的是 `python.exe run_gui.py`，
会把已装好的 exe 顶掉，开机后读的是仓库里的 data/ 目录，账号看起来就"没了"。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "CheckinTool"
# 用来判断某个值是不是本工具写的历史残留（exe 名 + 源码态入口脚本名）
_OWN_MARKERS = ("checkintool", "run_gui.py")


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def expected_command() -> str:
    """当前程序应该写进 Run 的那条命令。"""
    if _is_frozen():
        return f'"{sys.executable}"'
    root = Path(__file__).resolve().parents[1]
    return f'"{sys.executable}" "{root / "run_gui.py"}"'


def is_ours(stored: Any) -> bool:
    """这条 Run 值是不是本工具（含旧版本 / 源码态）写的。"""
    text = str(stored or "").lower()
    return any(marker in text for marker in _OWN_MARKERS)


def stored_command() -> str | None:
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
        return str(value)
    except OSError:
        return None


def is_enabled() -> bool:
    return stored_command() is not None


def write_entry() -> None:
    if os.name != "nt":
        raise RuntimeError("仅支持 Windows 开机自启")
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, expected_command())


def remove_entry() -> None:
    if os.name != "nt":
        raise RuntimeError("仅支持 Windows 开机自启")
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        try:
            winreg.DeleteValue(key, VALUE_NAME)
        except FileNotFoundError:
            pass


def apply_toggle(enabled: bool) -> str:
    """界面复选框的显式开关路径，返回一行给用户看的说明（不抛异常）。

    源码态直接拒绝：这时要写的命令是 ``python.exe run_gui.py``，会把已装好的 exe 条目顶掉，
    开机后读的是仓库里的 data/ 目录，账号看起来就"没了"。
    """
    if not _is_frozen():
        return "当前是源码运行，开机自启只在打包后的 exe 里生效"
    try:
        if enabled:
            write_entry()
            return "已开启开机自启"
        remove_entry()
        return "已关闭开机自启"
    except (OSError, RuntimeError) as exc:
        return f"开机自启设置失败: {exc}"


def plan_repair(
    settings: dict[str, Any],
    *,
    frozen: bool,
    stored: str | None,
    expected: str,
) -> str | None:
    """该往注册表写什么：``"write"`` / ``"remove"`` / ``None``（什么都不做）。"""
    if not frozen:
        return None
    if settings.get("autostart", True):
        return None if stored == expected else "write"
    return "remove" if is_ours(stored) else None


def repair_on_startup(settings: dict[str, Any]) -> str:
    """按当前设置对齐注册表，返回一行日志（无事可做时返回空串）。"""
    try:
        action = plan_repair(
            settings, frozen=_is_frozen(), stored=stored_command(), expected=expected_command()
        )
    except OSError as exc:  # 读注册表失败不该带崩启动
        return f"开机自启检查失败: {exc}"
    if not action:
        return ""
    try:
        if action == "write":
            write_entry()
            return "已对齐开机自启注册表（程序路径与之前不一致时会自动修正）"
        remove_entry()
        return "已按设置关闭开机自启"
    except (OSError, RuntimeError) as exc:
        return f"开机自启设置失败: {exc}"
