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
    excludes=[],
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
