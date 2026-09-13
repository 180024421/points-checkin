# -*- coding: utf-8 -*-
"""TraeWork（TRAE SOLO / Trae CN Work）每日签到适配器。

官方桌面端走：
  POST {ugApi}/trae/api/v2/ug/checkin_credits/status
  POST {ugApi}/trae/api/v2/ug/checkin_credits/claim

本机登录态优先从 iCubeAuthInfo（Electron safeStorage）提取；若无法解密，
允许用户把已登录客户端采集到的 token_blob 写入账号库。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ..retry_util import retry_call
from .base import CheckinResult, mask_secret

STATUS_PATH = "/trae/api/v2/ug/checkin_credits/status"
CLAIM_PATH = "/trae/api/v2/ug/checkin_credits/claim"
EXCHANGE_PATH = "/trae/api/v3/oauth/ExchangeToken"

# product.json -> iCubeApp.authConfig.SOLO.stable（TRAE SOLO CN 即 SOLO 模式稳定版）
CLIENT_ID = "en1oxy7wnw8j9n"
IDE_VERSION = "1.0.0"
# token 剩余有效期不足该秒数时，签到前自动用 refreshToken 续期
REFRESH_THRESHOLD_SEC = 6 * 3600

# CN 常见 ugApi；可被 settings / token_blob 覆盖
DEFAULT_UG_API_BASES = [
    "https://www.marscode.cn",
    "https://api.trae.com.cn",
    "https://www.trae.com.cn",
]

APP_DATA_CANDIDATES = [
    "TRAE SOLO",
    "Trae CN",
    "TRAE SOLO CN",
    "Trae",
]


def _appdata() -> Path:
    return Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))


def discover_user_dirs() -> list[Path]:
    root = _appdata()
    found: list[Path] = []
    for name in APP_DATA_CANDIDATES:
        p = root / name / "User" / "globalStorage"
        if p.exists():
            found.append(p)
    return found


def _dpapi_unprotect(blob: bytes) -> bytes | None:
    if os.name != "nt":
        return None
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
        return None
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


# ---- Trae byteCrypto（iCubeAuthInfo storage.json 混淆格式） ----
# 还原自 TRAE SOLO out/main.js 的 out-build/vs/base/common/byteCrypto.js：
# 头部 6 字节魔数 + 32 字节随机密钥材料 + AES-128-CBC(SHA512 派生 key/iv) 密文，
# 明文前 64 字节为 SHA-512(明文) 校验头。
_TRAE_MAGIC_AES = bytes([116, 99, 5, 16, 0, 0])  # 'tc' + 0x05 0x10 0x00 0x00
_TRAE_MAGIC_AES_PRIVATE = bytes([18, 57, 32, 32, 2, 3])
_TRAE_KIE = bytes.fromhex(
    "52096ad53036a538bf40a39e81f3d7fb"
    "7ce339829b2fff87348e4344c4dee9cb"
    "547b9432a6c2233dee4c950b42fac34e"
    "082ea16628d924b2765ba2496d8bd125"
)
_TRAE_DIE = bytes(
    [31, 221, 168, 51, 136, 7, 199, 49, 177, 18, 16, 89, 39, 128, 236, 95,
     96, 81, 127, 169, 25, 181, 74, 13, 45, 229, 122, 159, 147, 201, 156, 239,
     160, 224, 59, 77, 174, 42, 245, 176, 200, 235, 187, 60, 131, 83, 153, 97,
     23, 43, 4, 126, 186, 119, 214, 38, 225, 105, 20, 99, 85, 33, 12, 125]
)
_TRAE_CIE = bytes(
    [191, 192, 216, 250, 122, 246, 220, 97, 31, 254, 98, 27, 8, 72, 71, 176,
     135, 99, 96, 18, 127, 101, 203, 104, 211, 102, 191, 125, 37, 72, 150, 156,
     51, 229, 121, 35, 17, 153, 141, 177, 110, 131, 150, 128, 172, 255, 254, 6,
     18, 140, 55, 62, 236, 249, 135, 64, 135, 12, 117, 4, 89, 149, 168, 209]
)
_TRAE_EIE = bytes(
    [246, 204, 26, 232, 232, 70, 129, 109, 223, 146, 169, 242, 23, 241, 105, 145,
     50, 196, 165, 42, 254, 120, 3, 54, 244, 207, 209, 85, 53, 6, 138, 106,
     175, 148, 31, 204, 186, 186, 165, 182, 87, 142, 49, 10, 39, 110, 26, 154,
     86, 56, 173, 125, 18, 64, 198, 225, 99, 99, 83, 82, 191, 134, 76, 170]
)


def _decrypt_trae_bytecrypto(raw: bytes) -> bytes | None:
    """解密 Trae byteCrypto v1/v2 格式；非该格式返回 None。"""
    if raw[:6] == _TRAE_MAGIC_AES:
        xor_table = bytes(a ^ b for a, b in zip(_TRAE_KIE, _TRAE_DIE))
    elif raw[:6] == _TRAE_MAGIC_AES_PRIVATE:
        xor_table = bytes(a ^ b for a, b in zip(_TRAE_CIE, _TRAE_EIE))
    else:
        return None
    key_material = raw[6:38]
    if len(key_material) != 32:
        return None
    derived = bytearray(128)
    derived[0:64] = hashlib.sha512(key_material).digest()
    derived[64:128] = xor_table
    derived[0:64] = hashlib.sha512(bytes(derived)).digest()
    aes_key = bytes(derived[0:16])
    iv = bytes(derived[16:32])
    ciphertext = raw[38:]
    if not ciphertext or len(ciphertext) % 16:
        return None
    decryptor = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).decryptor()
    plain = decryptor.update(ciphertext) + decryptor.finalize()
    pad = plain[-1] if plain else 0
    if 1 <= pad <= 16:  # 去 PKCS#7 填充（WebCrypto 解密自动完成）
        plain = plain[:-pad]
    if len(plain) <= 64 or hashlib.sha512(plain[64:]).digest() != plain[:64]:
        return None
    return plain[64:]


def _try_decrypt_auth_value(value: str) -> Any | None:
    # plaintext json
    try:
        return json.loads(value)
    except Exception:
        pass
    try:
        raw = base64.b64decode(value)
    except Exception:
        return None
    # Trae byteCrypto（storage.json 中 iCubeAuthInfo 的主要格式）
    try:
        plain = _decrypt_trae_bytecrypto(raw)
    except Exception:
        plain = None
    if plain:
        try:
            return json.loads(plain.decode("utf-8"))
        except Exception:
            return None
    plain = _dpapi_unprotect(raw)
    if plain:
        try:
            return json.loads(plain.decode("utf-8"))
        except Exception:
            try:
                return json.loads(plain.decode("utf-8", errors="ignore"))
            except Exception:
                return None
    # Chromium OSCrypt v10 prefix
    if raw.startswith(b"v10") or raw.startswith(b"v11"):
        return None
    return None


def _read_storage_json(gs: Path) -> dict[str, Any]:
    path = gs / "storage.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _extract_token_from_obj(obj: Any) -> dict[str, Any] | None:
    if not isinstance(obj, dict):
        return None
    token = (
        obj.get("token")
        or obj.get("accessToken")
        or obj.get("access_token")
        or (obj.get("cloudToken") if isinstance(obj.get("cloudToken"), str) else None)
    )
    if not token and isinstance(obj.get("auth"), dict):
        token = obj["auth"].get("token") or obj["auth"].get("accessToken")
    user_id = obj.get("userId") or obj.get("user_id") or obj.get("uid")
    if not user_id and isinstance(obj.get("account"), dict):
        user_id = obj["account"].get("userId") or obj["account"].get("id")
    user_region = obj.get("userRegion")
    if isinstance(user_region, dict):
        user_region = user_region.get("region") or user_region.get("userRegion")
    scope = None
    if isinstance(obj.get("account"), dict):
        scope = obj["account"].get("scope")
    refresh_token = obj.get("refreshToken") or obj.get("refresh_token")
    expired_at = obj.get("expiredAt") or obj.get("expiresAt") or obj.get("expired_at")
    refresh_expired_at = obj.get("refreshExpiredAt") or obj.get("refreshExpiredAt")
    if token:
        return {
            "token": str(token),
            "user_id": str(user_id or ""),
            "user_region": str(user_region or ""),
            "scope": str(scope or ""),
            "host": str(obj.get("host") or ""),
            "refresh_token": str(refresh_token or ""),
            "token_expired_at": str(expired_at or ""),
            "refresh_expired_at": str(refresh_expired_at or ""),
        }
    return None


def _device_key_pairs(storage: dict[str, Any]) -> dict[str, dict[str, str]]:
    """iCubeAuthInfo://icube-dc:<userId> -> {私钥, 公钥}（该键按账号分键存储）。"""
    pairs: dict[str, dict[str, str]] = {}
    for key, value in storage.items():
        k = str(key)
        if not k.startswith("iCubeAuthInfo://icube-dc:") or not isinstance(value, str):
            continue
        obj = _try_decrypt_auth_value(value)
        if isinstance(obj, dict) and obj.get("privateKeyPEM") and obj.get("publicKeyPEM"):
            uid = k.split("icube-dc:", 1)[-1].strip()
            pairs[uid] = {
                "private_key_pem": str(obj["privateKeyPEM"]),
                "public_key_pem": str(obj["publicKeyPEM"]),
            }
    return pairs


def _extract_device_keys(storage: dict[str, Any], user_id: str | None = None) -> dict[str, str]:
    """取出与账号匹配的设备 RSA 密钥对（用于 refreshToken 续期签名）。

    dc 键以 userId 结尾，必须与账号一一对应：配错密钥会导致续期验签失败。
    优先精确匹配 user_id；只有一个时直接用它；都没有则回退任意一个。
    """
    pairs = _device_key_pairs(storage)
    uid = str(user_id or "").strip()
    if uid and uid in pairs:
        return pairs[uid]
    if len(pairs) == 1:
        return next(iter(pairs.values()))
    if uid:
        # 实测 dc 键后缀多为设备号（与账号 userId 不同）：同目录内任一密钥对均可用
        for k, v in pairs.items():
            if uid in k or k in uid:
                return v
    # 密钥对是设备级的（同客户端目录内通用），多个时也取一个兜底，避免误报"缺私钥"
    return next(iter(pairs.values())) if pairs else {}


def _identity_from_server_data(storage: dict[str, Any]) -> dict[str, str]:
    """iCubeServerData://* 为明文 JSON，取 identityStr 作为账号标识/备注。"""
    for key, value in storage.items():
        if not str(key).startswith("iCubeServerData://"):
            continue
        node = value
        if isinstance(value, str):
            try:
                node = json.loads(value)
            except Exception:
                continue
        ent = node.get("entitlementInfo") if isinstance(node, dict) else None
        if isinstance(ent, dict):
            ident = str(ent.get("identityStr") or "").strip()
            if ident:
                return {"nickname": ident, "membership": ident}
    return {}


def load_device_headers(gs: Path | None = None) -> dict[str, str]:
    dirs = [gs] if gs else discover_user_dirs()
    headers: dict[str, str] = {}
    for d in dirs:
        if not d:
            continue
        storage = _read_storage_json(d)
        machine = str(storage.get("telemetry.machineId") or "").strip()
        device = str(storage.get("telemetry.devDeviceId") or "").strip()
        if machine:
            headers["X-Machine-Id"] = machine
            headers["x-machine-id"] = machine
        if device:
            headers["X-Device-Id"] = device
            headers["x-device-id"] = device
        if headers:
            return headers
    return headers


def _blob_from_extracted(
    extracted: dict[str, Any],
    device_headers: dict[str, str],
    source_path: str,
    key: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    blob = {
        "provider": "traework",
        "access_token": extracted["token"],
        "token": extracted["token"],
        "user_id": extracted.get("user_id") or "",
        "user_region": extracted.get("user_region") or "",
        "scope": extracted.get("scope") or "",
        "machine_id": device_headers.get("X-Machine-Id") or "",
        "device_id": device_headers.get("X-Device-Id") or "",
        "ug_api_base": extracted.get("host") or "",
        "source_path": source_path,
        "token_hint": mask_secret(extracted["token"]),
        "auth_key": key,
    }
    for f in ("refresh_token", "token_expired_at", "refresh_expired_at"):
        if extracted.get(f):
            blob[f] = extracted[f]
    if isinstance(extra, dict):
        blob.update(extra)
    return blob


def user_tags(user_dir: str | Path | None = None) -> dict[str, str]:
    """本机登录过的 userId -> userTag（用于识别账号、提示未采集的账号）。"""
    dirs = [Path(user_dir)] if user_dir else discover_user_dirs()
    tags: dict[str, str] = {}
    for gs in dirs:
        storage = _read_storage_json(gs)
        value = storage.get("iCubeAuthInfo://usertag")
        if not isinstance(value, str):
            continue
        obj = _try_decrypt_auth_value(value)
        if isinstance(obj, dict):
            for uid, tag in obj.items():
                uid_s = str(uid).strip()
                if uid_s and uid_s.isdigit():
                    tags.setdefault(uid_s, str(tag or ""))
    return tags


def known_user_ids(user_dir: str | Path | None = None) -> list[str]:
    """本机曾经登录过的所有 userId（含已被覆盖、只剩记录的）。"""
    return sorted(user_tags(user_dir).keys())


_AUTH_KEY_PREFIX = "iCubeAuthInfo://"
_DEVICE_KEY_PREFIX = "iCubeAuthInfo://icube-dc:"
_USER_TAG_KEY = "iCubeAuthInfo://usertag"
# 只保存“当前账号”的固定键（登录/切号时被整条覆盖）
CURRENT_AUTH_KEY = "iCubeAuthInfo://icube.cloudide"


def _is_token_key(key: str) -> bool:
    """判断该 iCubeAuthInfo 键是否可能存放访问令牌（排除 usertag / 设备密钥）。"""
    return key.startswith(_AUTH_KEY_PREFIX) and key != _USER_TAG_KEY and not key.startswith(_DEVICE_KEY_PREFIX)


def _auths_from_storage(gs: Path) -> tuple[list[dict[str, Any]], str | None]:
    """从单个目录的 storage.json 提取**全部**可解密的登录态。

    注意：Trae CN 用固定键 ``iCubeAuthInfo://icube.cloudide`` 只保存当前登录账号，
    切号即覆盖；因此本函数把该目录所有 token 键都扫一遍（含历史残留键），
    尽量多捞。当前键排在最前。
    """
    storage = _read_storage_json(gs)
    if not storage:
        return [], None
    device_headers = load_device_headers(gs)
    server_hint = _identity_from_server_data(storage)
    dc_uids = list(_device_key_pairs(storage).keys())
    keys = [k for k in storage if _is_token_key(str(k))]
    keys.sort(key=lambda k: 0 if str(k) == CURRENT_AUTH_KEY else 1)

    out: list[dict[str, Any]] = []
    last_err = ""
    for key in keys:
        value = storage.get(key)
        if not isinstance(value, str):
            continue
        obj = _try_decrypt_auth_value(value)
        extracted = _extract_token_from_obj(obj) if obj else None
        if not extracted or not extracted.get("token"):
            last_err = f"无法解密 {key}（可能是 Electron safeStorage，需本机会话用户）"
            continue
        if not extracted.get("user_id") and len(dc_uids) == 1:
            extracted["user_id"] = dc_uids[0]
        keys_blob = _extract_device_keys(storage, extracted.get("user_id") or "")
        out.append(
            _blob_from_extracted(
                extracted, device_headers, str(gs / "storage.json"), str(key), {**keys_blob, **server_hint}
            )
        )
    return out, last_err


def _auths_from_vscdb(gs: Path) -> tuple[list[dict[str, Any]], str | None]:
    """state.vscdb 兜底提取登录态（可能存有一份历史副本）。"""
    db = gs / "state.vscdb"
    if not db.exists():
        return [], ""
    device_headers = load_device_headers(gs)
    try:
        conn = sqlite3.connect(str(db))
        rows = conn.execute(
            "SELECT key, value FROM ItemTable WHERE key LIKE 'iCubeAuthInfo%'"
        ).fetchall()
        conn.close()
    except Exception as exc:
        return [], f"读取 state.vscdb 失败: {exc}"
    storage_like = {str(k): v for k, v in rows}
    server_hint = _identity_from_server_data(storage_like)
    dc_uids = list(_device_key_pairs(storage_like).keys())
    out: list[dict[str, Any]] = []
    for key, value in rows:
        if not isinstance(value, str) or not _is_token_key(str(key)):
            continue
        obj = _try_decrypt_auth_value(value)
        extracted = _extract_token_from_obj(obj) if obj else None
        if not extracted or not extracted.get("token"):
            continue
        if not extracted.get("user_id") and len(dc_uids) == 1:
            extracted["user_id"] = dc_uids[0]
        keys_blob = _extract_device_keys(storage_like, extracted.get("user_id") or "")
        out.append(
            _blob_from_extracted(
                extracted, device_headers, str(db), str(key), {**keys_blob, **server_hint}
            )
        )
    return out, ""


def _mtime(path: str) -> float:
    try:
        return Path(path).stat().st_mtime
    except Exception:
        return 0.0


def _sort_key(auth: dict[str, Any]) -> tuple[int, float]:
    """CN 区优先；同区域取登录态文件最新（即最近登录）的。"""
    region = str(auth.get("user_region") or "").strip().upper()
    cn_first = 0 if (not region or region in ("CN", "CHINA")) else 1
    return (cn_first, _mtime(str(auth.get("source_path") or "")))


def _best_of(cur: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """同一 userId 多处出现时，取信息更全、登录态文件更新的那份。"""
    cur_score = (1 if cur.get("token") else 0, _mtime(str(cur.get("source_path") or "")))
    new_score = (1 if new.get("token") else 0, _mtime(str(new.get("source_path") or "")))
    return new if new_score >= cur_score else cur


def load_all_local_auths(
    user_dir: str | Path | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """扫描所有 Trae 数据目录，返回全部登录态（按 user_id 去重）。

    排序：CN 区优先，其次登录态文件最新（最近登录的客户端）。
    """
    dirs = [Path(user_dir)] if user_dir else discover_user_dirs()
    if not dirs:
        return [], "未找到 Trae/TRAE SOLO 用户数据目录，请先安装并登录 TraeWork 桌面端"

    by_key: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for gs in dirs:
        for fn in (_auths_from_storage, _auths_from_vscdb):
            auths, err = fn(gs)
            if err:
                errors.append(err)
            for auth in auths:
                uid = str(auth.get("user_id") or "").strip()
                key = uid or f"anon:{auth.get('auth_key') or auth.get('token_hint') or len(by_key)}"
                by_key[key] = _best_of(by_key[key], auth) if key in by_key else auth

    found = sorted(by_key.values(), key=_sort_key)
    if found:
        return found, None
    headers = load_device_headers()
    fallback = {
        "provider": "traework",
        "access_token": "",
        "token": "",
        "user_id": "",
        "machine_id": headers.get("X-Machine-Id") or "",
        "device_id": headers.get("X-Device-Id") or "",
        "ug_api_base": "",
        "needs_manual_token": True,
        "token_hint": "(无)",
    }
    err_text = (errors[0] if errors else "未找到可解密的 iCubeAuthInfo")
    return [fallback], err_text + "；可在账号库手工粘贴 token（并保留 machine_id/device_id）"


def storage_signature(user_dir: str | Path | None = None) -> dict[str, tuple[int, int]]:
    """登录态文件指纹（mtime_ns, size）；供实时监听判断是否需要重新提取。"""
    dirs = [Path(user_dir)] if user_dir else discover_user_dirs()
    sig: dict[str, tuple[int, int]] = {}
    for gs in dirs:
        for name in ("storage.json", "state.vscdb"):
            p = gs / name
            try:
                st = p.stat()
            except Exception:
                continue
            sig[str(p)] = (st.st_mtime_ns, st.st_size)
    return sig


def diagnose_local_auth(user_dir: str | Path | None = None) -> dict[str, Any]:
    """体检本机 Trae 登录态：为什么采不到 / 少采了哪个账号。"""
    dirs = [Path(user_dir)] if user_dir else discover_user_dirs()
    details: list[str] = []
    if not dirs:
        return {
            "ok": False,
            "details": [
                "未找到任何 Trae 数据目录（应为 %APPDATA%\\Trae CN\\User\\globalStorage）",
                "请先安装并登录 Trae CN 桌面端",
            ],
            "dirs": [],
        }
    details.append(f"发现 {len(dirs)} 个 Trae 数据目录")
    keys_report: list[dict[str, Any]] = []
    for gs in dirs:
        storage = _read_storage_json(gs)
        details.append(f"[{gs.parent.parent.name}] {gs}")
        if not storage:
            details.append("  storage.json 不存在或无法解析")
            continue
        for key, value in storage.items():
            k = str(key)
            item = {"dir": str(gs), "key": k, "decryptable": None}
            if k == _USER_TAG_KEY:
                tags = _try_decrypt_auth_value(value) if isinstance(value, str) else None
                item["decryptable"] = True
                item["kind"] = "usertag"
                item["user_ids"] = sorted(str(u) for u in tags) if isinstance(tags, dict) else []
                details.append(f"  usertag: 本机登录过 {len(item['user_ids'])} 个账号 {item['user_ids']}")
            elif k.startswith(_DEVICE_KEY_PREFIX):
                item["decryptable"] = bool(_extract_device_keys(storage, k.split("icube-dc:", 1)[-1]))
                item["kind"] = "device-key"
                details.append(f"  {k}: 设备私钥{'可解密' if item['decryptable'] else '不可解密'}")
            elif _is_token_key(k):
                obj = _try_decrypt_auth_value(value) if isinstance(value, str) else None
                ext = _extract_token_from_obj(obj) if obj else None
                item["decryptable"] = bool(ext and ext.get("token"))
                item["kind"] = "token" + ("(当前键)" if k == CURRENT_AUTH_KEY else "")
                item["user_id"] = (ext or {}).get("user_id")
                details.append(
                    f"  {k} [{item['kind']}]: "
                    + (f"已解密 userId={item['user_id']}" if item["decryptable"] else "无法解密（客户端未运行/已换代）")
                )
            elif k.startswith("iCubeServerData://"):
                item["decryptable"] = True
                item["kind"] = "server-data(明文)"
            keys_report.append(item)

    auths, err = load_all_local_auths(user_dir)
    with_token = [a for a in auths if a.get("token")]
    known = known_user_ids(user_dir)
    collected = {str(a.get("user_id") or "") for a in with_token}
    missing = [u for u in known if u not in collected]
    details.append(f"可提取登录态：{len(with_token)} 个 {sorted(collected)}")
    if err:
        details.append(f"提示：{err}")
    if missing:
        details.append(
            f"以下 {len(missing)} 个账号本机只剩记录、没有 token：{missing}；"
            "Trae 只保留最后登录的那个账号，历史 token 已被覆盖，无法事后补采"
        )
    return {
        "ok": bool(with_token),
        "dirs": [str(d) for d in dirs],
        "keys": keys_report,
        "collected": sorted(collected),
        "missing": missing,
        "details": details,
    }


def load_local_auth(user_dir: str | Path | None = None) -> tuple[dict[str, Any] | None, str | None]:
    """返回当前最合适的登录态：CN 区优先，其次最近登录的客户端。"""
    auths, err = load_all_local_auths(user_dir)
    if not auths:
        return None, err
    return auths[0], (err if not auths[0].get("token") else None)


def _parse_iso_ms(value: str | None) -> float | None:
    """解析 token 有效期：支持 ISO 字符串，也支持纯数字（秒 / 毫秒时间戳）。"""
    if not value:
        return None
    s = str(value).strip()
    if s.isdigit():
        n = int(s)
        return n / 1000.0 if n > 1e12 else float(n)
    s = s.rstrip("Z").replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            import datetime as _dt

            dt = _dt.datetime.strptime(s[:26], fmt)
            return dt.replace(tzinfo=_dt.timezone.utc).timestamp()
        except Exception:
            continue
    return None


def reload_blob_from_local(blob: dict[str, Any]) -> dict[str, Any] | None:
    """回读本机 storage 中该账号最新 token（Trae 客户端打开/运行时后台自动刷新并写回）。

    若本机存在更新的登录态，返回更新后的 blob；否则返回 None。
    """
    uid = str(blob.get("user_id") or "").strip()
    if not uid:
        return None
    for gs in discover_user_dirs():
        storage = _read_storage_json(gs)
        for key, value in storage.items():
            if not str(key).startswith("iCubeAuthInfo://") or not isinstance(value, str):
                continue
            obj = _try_decrypt_auth_value(value)
            if not isinstance(obj, dict):
                continue
            ext = _extract_token_from_obj(obj)
            if not ext or ext.get("user_id") != uid or not ext.get("token"):
                continue
            new = dict(blob)
            new["access_token"] = ext["token"]
            new["token"] = ext["token"]
            new["token_hint"] = mask_secret(ext["token"])
            if ext.get("refresh_token"):
                new["refresh_token"] = ext["refresh_token"]
            if ext.get("token_expired_at"):
                new["token_expired_at"] = ext["token_expired_at"]
            if ext.get("host"):
                new["ug_api_base"] = ext["host"]
            return new
    return None


def prepare_checkin_blob(
    blob: dict[str, Any], *, settings: dict | None = None
) -> tuple[dict[str, Any], str]:
    """签到前确保 token 有效（自动续期）。

    策略：
      1) 回读本机 Trae 已自动刷新的最新 token（最可靠，覆盖大多数场景）；
      2) 否则尝试用 refreshToken + 设备私钥协议续期兜底；
    返回 (blob, note)，note 为空表示无需续期或已是最新。
    """
    blob = dict(blob)
    if settings and settings.get("traework_ug_api_base") and not blob.get("ug_api_base"):
        blob["ug_api_base"] = str(settings["traework_ug_api_base"]).strip().rstrip("/")
    if _blocked_region(blob):
        return blob, ""  # 非 CN 区不续期
    note = ""
    if token_needs_refresh(blob):
        local = reload_blob_from_local(blob)
        if local and not token_needs_refresh(local):
            blob = local
            note = "已从本机 Trae 登录态刷新 token"
        else:
            new_blob, err = refresh_traework_token(blob)
            if new_blob:
                blob = new_blob
                note = "已使用 refreshToken 自动续期"
            elif err:
                note = (
                    f"续期失败（{err}）；"
                    "若长时间未打开 Trae，请先打开一次客户端（会自动刷新登录态）再重新采集"
                )
    return blob, note


def token_needs_refresh(blob: dict[str, Any], *, now: float | None = None) -> bool:
    """token 即将过期（或已过期）则 True。"""
    if not blob.get("refresh_token"):
        return False
    exp = _parse_iso_ms(str(blob.get("token_expired_at") or ""))
    if exp is None:
        return False
    now = time.time() if now is None else now
    return exp - now <= REFRESH_THRESHOLD_SEC


def refresh_traework_token(blob: dict[str, Any], *, timeout: float = 30.0) -> tuple[dict[str, Any] | None, str | None]:
    """用 refreshToken + 设备私钥签名调用 ExchangeToken 续期。

    成功返回更新后的 blob（含新 token / refreshToken / expiredAt），失败返回 (None, err)。
    设备密钥对和 refreshToken 在采集时一并存入 blob，因此无需重新登录即可续期。
    """
    refresh_token = str(blob.get("refresh_token") or "").strip()
    priv = str(blob.get("private_key_pem") or "").strip()
    pub = str(blob.get("public_key_pem") or "").strip()
    base = str(blob.get("ug_api_base") or "").strip().rstrip("/")
    access = str(blob.get("access_token") or blob.get("token") or "").strip()
    if not (refresh_token and priv and pub and base):
        return None, "缺少 refreshToken / 设备私钥 / ugApi，无法自动续期（请重新采集）"
    try:
        pkey = serialization.load_pem_private_key(priv.encode("utf-8"), password=None)
    except Exception as exc:
        return None, f"私钥解析失败: {exc}"

    import socket
    import time

    ts = int(time.time())
    nonce = os.urandom(16).hex()
    message = f"POST {EXCHANGE_PATH} {CLIENT_ID} {refresh_token} {ts} {nonce}".encode("utf-8")
    sig = pkey.sign(message, ec.ECDSA(hashes.SHA256()))
    signature = base64.b64encode(sig).decode("utf-8")

    device_info = {
        "DeviceID": str(blob.get("device_id") or ""),
        "MachineID": str(blob.get("machine_id") or ""),
        "PlatformCode": "SOLO_Lite",
        "DeviceType": "PC",
        "DeviceName": socket.gethostname(),
        "DeviceModel": "",
        "ClientVersion": IDE_VERSION,
        "DevicePublicKey": pub,
        "DeviceBrand": "",
        "DeviceCPU": "",
    }
    payload = {
        "ClientID": CLIENT_ID,
        "ClientSecret": "",
        "RefreshToken": refresh_token,
        "DeviceInfo": device_info,
        "DeviceProof": {"Signature": signature, "Timestamp": ts, "Nonce": nonce},
        "IDEVersion": IDE_VERSION,
    }
    headers = {"Content-Type": "application/json", "x-cloudide-token": access}
    http, resp = _api_post(f"{base}{EXCHANGE_PATH}", headers, timeout=timeout, data=payload)
    if http != 200 or not isinstance(resp, dict):
        return None, f"续期请求失败 HTTP {http}: {str(resp)[:200]}"

    # 响应可能嵌套在 Result / data 中
    data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
    code = resp.get("code")
    if code not in (0, None, "0") and isinstance(resp.get("Result"), dict):
        data = resp["Result"]
    new_token = (
        data.get("token")
        or data.get("accessToken")
        or data.get("access_token")
    )
    if not new_token:
        return None, f"续期响应缺少 token: {str(resp)[:200]}"
    new_blob = dict(blob)
    new_blob["access_token"] = str(new_token)
    new_blob["token"] = str(new_token)
    new_blob["token_hint"] = mask_secret(str(new_token))
    new_blob["refresh_token"] = str(data.get("refreshToken") or data.get("refresh_token") or refresh_token)
    if data.get("expiredAt") or data.get("expiresAt"):
        new_blob["token_expired_at"] = str(data.get("expiredAt") or data.get("expiresAt"))
    if data.get("refreshExpiredAt") or data.get("refreshExpiredAt"):
        new_blob["refresh_expired_at"] = str(data.get("refreshExpiredAt") or data.get("refreshExpiredAt"))
    return new_blob, None


def _api_post(
    url: str,
    headers: dict[str, str],
    timeout: float = 30.0,
    data: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    body = b"{}" if data is None else json.dumps(data).encode("utf-8")
    req = Request(
        url,
        method="POST",
        data=body,
        headers=headers,
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, {"raw": raw[:300]}
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        try:
            return exc.code, json.loads(body)
        except Exception:
            return exc.code, {"msg": body[:200]}
    except URLError as exc:
        return -1, {"msg": f"网络失败: {exc}"}
    except Exception as exc:
        return -1, {"msg": str(exc)}


def _build_headers(token_blob: dict[str, Any]) -> dict[str, str]:
    token = str(token_blob.get("access_token") or token_blob.get("token") or "").strip()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "CheckinTool/1.0",
        # 官方客户端使用 Cloud-IDE-JWT 方案（mixAuthorization），Bearer 会被判为未认证(1001)
        "Authorization": f"Cloud-IDE-JWT {token}",
        "x-cloudide-token": token,
        "X-Cloudide-Token": token,
    }
    machine = str(token_blob.get("machine_id") or "").strip()
    device = str(token_blob.get("device_id") or "").strip()
    if machine:
        headers["X-Machine-Id"] = machine
        headers["x-machine-id"] = machine
    if device:
        headers["X-Device-Id"] = device
        headers["x-device-id"] = device
    user_id = str(token_blob.get("user_id") or "").strip()
    if user_id:
        headers["X-User-Id"] = user_id
        headers["x-user-id"] = user_id
    region = str(token_blob.get("user_region") or "").strip()
    if region:
        headers["X-User-Region"] = region
    return headers


def _candidate_bases(token_blob: dict[str, Any]) -> list[str]:
    bases: list[str] = []
    custom = str(token_blob.get("ug_api_base") or "").strip().rstrip("/")
    if custom:
        bases.append(custom)
    for key in ("ug_api_bases", "host_bases"):
        extra = token_blob.get(key)
        if isinstance(extra, list):
            for b in extra:
                b = str(b or "").strip().rstrip("/")
                if b and b not in bases:
                    bases.append(b)
    for b in DEFAULT_UG_API_BASES:
        if b not in bases:
            bases.append(b)
    return bases


def _blocked_region(token_blob: dict[str, Any]) -> str:
    """签到积分仅对 CN 区账号开放；返回非 CN 区域名，CN/未知返回空。"""
    region = str(token_blob.get("user_region") or token_blob.get("userRegion") or "").strip().upper()
    if region and region not in ("CN", "CHINA", "CN-NORTH"):
        return region
    return ""


def query_from_blob(token_blob: dict[str, Any], *, timeout: float = 30.0) -> dict[str, Any]:
    token = str(token_blob.get("access_token") or token_blob.get("token") or "").strip()
    if not token:
        return {"ok": False, "message": "缺少 token"}
    region = _blocked_region(token_blob)
    if region:
        return {"ok": False, "message": f"账号区域为 {region}，签到积分仅支持 CN 区账号", "region": region}
    local_headers = load_device_headers()
    token_blob = {
        **token_blob,
        "machine_id": token_blob.get("machine_id") or local_headers.get("X-Machine-Id") or "",
        "device_id": token_blob.get("device_id") or local_headers.get("X-Device-Id") or "",
    }
    headers = _build_headers(token_blob)
    for base in _candidate_bases(token_blob):
        http, resp = _api_post(f"{base}{STATUS_PATH}", headers, timeout=timeout)
        data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
        if http == 200 and isinstance(data, dict):
            return {
                "ok": True,
                "base": base,
                "enable": data.get("enable"),
                "today_checked_in": data.get("checked_in"),
                "credits": data.get("credits"),
                "message": "",
                "raw": data,
            }
    return {"ok": False, "message": "查询失败"}


def checkin_with_blob(token_blob: dict[str, Any], *, timeout: float = 30.0) -> CheckinResult:
    token = str(token_blob.get("access_token") or token_blob.get("token") or "").strip()
    if not token:
        return CheckinResult(ok=False, provider="traework", message="缺少 TraeWork token")
    region = _blocked_region(token_blob)
    if region:
        return CheckinResult(
            ok=False,
            provider="traework",
            message=f"账号区域为 {region}，签到积分仅支持 CN 区账号",
            raw_summary={"region": region, "retryable": False},
        )
    if not token_blob.get("machine_id") or not token_blob.get("device_id"):
        local_headers = load_device_headers()
        token_blob = {
            **token_blob,
            "machine_id": token_blob.get("machine_id") or local_headers.get("X-Machine-Id") or "",
            "device_id": token_blob.get("device_id") or local_headers.get("X-Device-Id") or "",
        }
    if not token_blob.get("machine_id") or not token_blob.get("device_id"):
        return CheckinResult(
            ok=False,
            provider="traework",
            message="缺少 X-Machine-Id / X-Device-Id（请先打开过 TraeWork 桌面端）",
        )

    def _once() -> CheckinResult:
        headers = _build_headers(token_blob)
        last_err = "所有 ugApi 均失败"
        for base in _candidate_bases(token_blob):
            status_url = f"{base}{STATUS_PATH}"
            claim_url = f"{base}{CLAIM_PATH}"
            http, resp = _api_post(status_url, headers, timeout=timeout)
            if http < 0:
                last_err = str(resp.get("msg") or last_err)
                return CheckinResult(
                    ok=False,
                    provider="traework",
                    message=last_err,
                    raw_summary={"retryable": True, "base": base},
                )
            data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
            if not isinstance(data, dict):
                last_err = f"{base} 响应无效 HTTP {http}"
                continue

            enable = data.get("enable")
            checked_in = data.get("checked_in")
            credits = data.get("credits")
            code = data.get("code") if "code" in data else resp.get("code")

            if code in (9004, "9004"):
                last_err = f"{base} 参数错误(9004)，请检查设备头/token"
                continue
            if code in (9074, "9074"):
                return CheckinResult(
                    ok=False,
                    provider="traework",
                    message="服务器繁忙(9074)，请稍后重试",
                    raw_summary={"base": base, "code": 9074, "retryable": True},
                )

            if http == 200 and enable is False:
                return CheckinResult(
                    ok=True,
                    provider="traework",
                    already=True,
                    message=f"签到未开放（{base}）",
                    raw_summary={"base": base, "enable": False},
                )

            if http == 200 and checked_in is True:
                return CheckinResult(
                    ok=True,
                    provider="traework",
                    already=True,
                    credits=int(credits) if isinstance(credits, (int, float)) else 200,
                    message=f"今天已签到（{base}）",
                    raw_summary={"base": base, "checked_in": True},
                )

            if http == 200 and (checked_in is False or enable is True or "checked_in" in data):
                http2, resp2 = _api_post(claim_url, headers, timeout=timeout)
                data2 = resp2.get("data") if isinstance(resp2.get("data"), dict) else resp2
                code2 = data2.get("code") if isinstance(data2, dict) and "code" in data2 else resp2.get("code")
                if http2 == 200 and (
                    code2 in (0, None, "0")
                    or (isinstance(data2, dict) and data2.get("checked_in") is not False)
                ):
                    got = None
                    if isinstance(data2, dict):
                        got = data2.get("credits") or data2.get("credit")
                    return CheckinResult(
                        ok=True,
                        provider="traework",
                        credits=int(got) if isinstance(got, (int, float)) else 200,
                        message=f"签到成功（{base}）",
                        raw_summary={"base": base, "claimed": True},
                    )
                if code2 in (9074, "9074") or http2 < 0:
                    return CheckinResult(
                        ok=False,
                        provider="traework",
                        message="服务器繁忙/网络失败，将重试",
                        raw_summary={"base": base, "code": code2, "retryable": True},
                    )
                last_err = f"领取失败 HTTP {http2} / {resp2}"
                continue

            last_err = f"{base} HTTP {http} / {str(resp)[:160]}"
        return CheckinResult(ok=False, provider="traework", message=last_err)

    def _should_retry(result: CheckinResult) -> bool:
        return (not result.ok) and bool((result.raw_summary or {}).get("retryable"))

    return retry_call(_once, retries=10, min_wait=15, max_wait=30, should_retry=_should_retry)


def checkin_from_local(user_dir: str | Path | None = None) -> CheckinResult:
    auth, err = load_local_auth(user_dir)
    if not auth or not auth.get("token"):
        return CheckinResult(ok=False, provider="traework", message=err or "无登录态")
    return checkin_with_blob(auth)


def checkin_from_blob(token_blob: dict[str, Any]) -> CheckinResult:
    return checkin_with_blob(token_blob)
