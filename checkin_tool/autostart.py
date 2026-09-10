# -*- coding: utf-8 -*-
"""开机自启（当前用户 Run 注册表）。"""

from __future__ import annotations

import os
import sys
from pathlib import Path


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "CheckinTool"


def _exe_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    root = Path(__file__).resolve().parents[1]
    py = sys.executable
    return f'"{py}" "{root / "run_gui.py"}"'


def is_enabled() -> bool:
    if os.name != "nt":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, VALUE_NAME)
            return True
    except OSError:
        return False


def set_enabled(enabled: bool) -> None:
    if os.name != "nt":
        raise RuntimeError("仅支持 Windows 开机自启")
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, _exe_command())
        else:
            try:
                winreg.DeleteValue(key, VALUE_NAME)
            except FileNotFoundError:
                pass
