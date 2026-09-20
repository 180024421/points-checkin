# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

hidden = collect_submodules("checkin_tool")

a = Analysis(
    ["run_gui.py"],
    pathex=["."],
    binaries=[],
    datas=[
        ("checkin_tool/ui/index.html", "ui"),
        ("checkin_tool/ui/icon.png", "ui"),
        ("checkin_tool/ui/icon.ico", "ui"),
    ],
    hiddenimports=hidden + ["webview", "bottle"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # pywebview 的 guilib 会 try-import 各后端（含 PyQt5.QtWebEngineWidgets），
    # 打包机装了 PyQt5 就会被整族收进 EXE（十几 MB 死重）。Windows 上实际跑的是
    # EdgeChromium，把这些显式排掉，同时把后端锁死在系统 WebView2。
    excludes=["PyQt5", "PyQt6", "PySide2", "PySide6"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="CheckinTool",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon="assets/icon.ico",
)
