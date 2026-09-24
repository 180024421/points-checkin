# -*- coding: utf-8 -*-
"""Qoder 签到/活动只读探测。

目的：在写适配器之前，先用本机真实登录态回答两个决定性问题——
1) Qoder 的「签到」到底是可重复的每日签到，还是限时运营活动（campaign）；
2) 领取（claim）接口在哪、请求形状是什么。

本脚本默认只读：解密本机 auth.v1.dat，对 campaigns 接口发 GET。
**不做任何 claim / POST 状态变更**，除非显式 --claim（且不默认调用）。

Chromium OSCrypt v10：auth.v1.dat = b"v10" + nonce(12B) + AES-256-GCM 密文；
AES 密钥 = DPAPI(Local State.os_crypt.encrypted_key 去掉 b"DPAPI" 前缀)。
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import json
import os
import sys
import urllib.request
from ctypes import wintypes
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

OPENAPI_BASE = "https://openapi.qoder.sh"
CAMPAIGNS_PATH = "/sash/api/v1/me/campaigns"


def _appdata() -> Path:
    return Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))


def _qoder_dir() -> Path:
    return _appdata() / "com.qoder.app.stable"


def _dpapi_unprotect(blob: bytes) -> bytes | None:
    if os.name != "nt":
        return None

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(blob, len(blob))
    blob_in = DATA_BLOB(len(blob), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        return None
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _oscrypt_key() -> bytes | None:
    local_state = _qoder_dir() / "Local State"
    data = json.loads(local_state.read_text(encoding="utf-8"))
    enc = data.get("os_crypt", {}).get("encrypted_key")
    if not enc:
        return None
    raw = base64.b64decode(enc)
    if raw[:5] != b"DPAPI":
        return None
    return _dpapi_unprotect(raw[5:])


def decrypt_auth() -> dict | None:
    key = _oscrypt_key()
    if not key:
        print("[!] 无法取得 OSCrypt 主密钥（非 Windows 或 DPAPI 失败）")
        return None
    dat = _qoder_dir() / "auth.v1.dat"
    blob = dat.read_bytes()
    if blob[:3] != b"v10":
        print(f"[!] auth.v1.dat 头部不是 v10，实际={blob[:3]!r}")
        return None
    nonce = blob[3:15]
    ct = blob[15:]
    plain = AESGCM(key).decrypt(nonce, ct, None)
    return json.loads(plain.decode("utf-8"))


def _mask(v: str, keep: int = 6) -> str:
    if not isinstance(v, str) or len(v) <= keep:
        return "<short>"
    return v[:keep] + "…" + f"(len={len(v)})"


def show_auth(auth: dict) -> None:
    tok = auth.get("token") or ""
    print("== auth.v1.dat（脱敏）==")
    print(f"  schemaVersion : {auth.get('schemaVersion')}")
    print(f"  token         : {_mask(tok)}")
    print(f"  refreshToken  : {_mask(str(auth.get('refreshToken') or ''))}")
    print(f"  expiresAt     : {auth.get('expiresAt')}")
    u = auth.get("user") or {}
    print(f"  user.id       : {u.get('id')}")
    print(f"  user.name     : {u.get('name')}")
    print(f"  user.email    : {u.get('email')}")
    print(f"  顶层键         : {sorted(auth.keys())}")


def http_get(url: str, token: str, cosy_version: str) -> tuple[int, str]:
    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Cosy-ClientType": "10",
            "Cosy-Version": cosy_version,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return -1, repr(e)


def _guess_cosy_version() -> str:
    # 从本地日志里找 Cosy-Version，找不到就给个占位（服务端一般不强校验该头）
    logs = _qoder_dir() / "logs"
    if logs.exists():
        for p in sorted(logs.rglob("*.log"), key=lambda x: x.stat().st_mtime, reverse=True)[:5]:
            try:
                txt = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            idx = txt.find("Cosy-Version")
            if idx != -1:
                seg = txt[idx : idx + 80]
                # 尝试提取引号内的版本串
                for q in ('"', "'"):
                    if q in seg:
                        after = seg.split(q)
                        for cand in after[1:]:
                            if cand and cand[0] != ":" and len(cand) > 3 and "ersion" not in cand:
                                return cand
    return ""


def probe_campaigns(token: str) -> None:
    cosy = _guess_cosy_version()
    url = OPENAPI_BASE + CAMPAIGNS_PATH
    status, body = http_get(url, token, cosy)
    print(f"\n== GET {CAMPAIGNS_PATH} -> {status} (Cosy-Version={'<set>' if cosy else '<empty>'}) ==")
    try:
        data = json.loads(body)
    except Exception:
        print(body[:2000])
        return
    print(json.dumps(data, ensure_ascii=False, indent=2)[:6000])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--claim", action="store_true", help="（默认关闭）执行领取，需另行确认接口")
    args = ap.parse_args()
    auth = decrypt_auth()
    if not auth:
        return 1
    show_auth(auth)
    tok = auth.get("token") or ""
    if not tok:
        print("[!] 无 token，跳过网络探测")
        return 2
    probe_campaigns(tok)
    if args.claim:
        print("\n[!] --claim 未实现：claim 接口尚未定位，禁止盲发状态变更。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
