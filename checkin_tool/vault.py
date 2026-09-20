"""本地敏感数据（token / 快照）的加密存储。

Windows 上使用 DPAPI（CryptProtectData）：密钥绑定到当前 Windows 用户账户，
换个用户或换台机器就解不开，即使文件被拷走也是一串废数据。
其他平台降级为「明文 + 600 权限」，并打印警告。
"""

from __future__ import annotations

import json
import os
import platform
import stat
from pathlib import Path
from typing import Any

SYSTEM = platform.system()
_MAGIC = b"CSDPAPI1"  # 加密文件头，用于区分明文/密文

# 加密失败 / 读到明文时留下的痕迹：EXE 无控制台，print 用户看不到，
# 必须落到数据目录里，否则"以为加密了其实是明文"永远不会被发现。
_WARNING_FILE = "STORAGE_WARNING.txt"
_DEGRADED: set[str] = set()


def is_secure() -> bool:
    return SYSTEM == "Windows"


def degraded_paths() -> list[str]:
    return sorted(_DEGRADED)


def warning_path(root: str | Path) -> Path:
    return Path(root).expanduser() / _WARNING_FILE


def startup_warnings(root: str | Path) -> list[str]:
    """启动时给界面看的存储告警（历史上是否明文落盘过）。"""
    target = warning_path(root)
    try:
        if not target.exists():
            return []
        lines = [ln.strip() for ln in target.read_text(encoding="utf-8").splitlines() if ln.strip()]
        return [f"本地敏感数据曾明文落盘：{lines[-1]}"]
    except OSError:
        return []


def note_degraded(path: str | Path, reason: str) -> None:
    target = Path(path).expanduser()
    _DEGRADED.add(str(target))
    try:
        from datetime import datetime

        marker = warning_path(target.parent)
        line = (
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
            f"{target.name} 未加密存储：{reason}\n"
        )
        # 只保留最近记录，避免异常反复触发时把告警文件写大
        try:
            size = marker.stat().st_size
        except OSError:
            size = 0
        keep = marker.read_text(encoding="utf-8")[-2000:] if 0 < size <= 64 * 1024 else ""
        marker.write_text(keep + line, encoding="utf-8")
    except Exception:  # noqa: BLE001 - 告警本身绝不能影响业务写入
        pass


# --------------------------------------------------------------------------
# Windows DPAPI
# --------------------------------------------------------------------------

def _dpapi_protect(data: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()

    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise OSError("CryptProtectData 调用失败")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _dpapi_unprotect(blob: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(blob, len(blob))
    blob_in = DATA_BLOB(len(blob), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()

    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise OSError("CryptUnprotectData 调用失败（文件来自另一台机器或另一个用户？）")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


# --------------------------------------------------------------------------
# 对外接口
# --------------------------------------------------------------------------

def dump(obj: Any, path: str | Path) -> Path:
    """把对象写入文件，尽可能加密。"""
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")

    if is_secure():
        try:
            payload = _MAGIC + _dpapi_protect(raw)
            path.write_bytes(payload)
            return path
        except Exception as e:  # noqa: BLE001 - 加密失败不能阻断主流程，降级为明文
            print(f"[warn] DPAPI 加密失败，改为明文保存：{e}")
            note_degraded(path, f"DPAPI 加密失败（{e}）")

    path.write_bytes(raw)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 600
    except Exception:
        pass
    return path


def load(path: str | Path) -> Any:
    """读取由 dump 写入的文件，自动识别明文/密文。"""
    path = Path(path).expanduser()
    raw = path.read_bytes()

    if raw.startswith(_MAGIC):
        raw = _dpapi_unprotect(raw[len(_MAGIC):])
    elif is_secure():
        print(f"[warn] {path} 未加密存储，读取后将自动迁移为加密格式")
        note_degraded(path, "读到明文文件（将在下次保存时加密迁移）")

    return json.loads(raw.decode("utf-8"))
