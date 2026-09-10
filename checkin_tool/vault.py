"""本地敏感数据（token / 快照）的加密存储。

Windows 上使用 DPAPI（CryptProtectData）：密钥绑定到当前 Windows 用户账户，
换个用户或换台机器就解不开，即使文件被拷走也是一串废数据。
其他平台降级为「明文 + 600 权限」，并打印警告。
"""

from __future__ import annotations

import base64
import json
import os
import platform
import stat
from pathlib import Path
from typing import Any

SYSTEM = platform.system()
_MAGIC = b"CSDPAPI1"  # 加密文件头，用于区分明文/密文


def is_secure() -> bool:
    return SYSTEM == "Windows"


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
        except Exception as e:  # 加密失败不能阻断主流程，降级为明文
            print(f"[warn] DPAPI 加密失败，改为明文保存：{e}")

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
        print(f"[warn] {path} 未加密存储，建议迁移到本机生成的文件")

    return json.loads(raw.decode("utf-8"))
