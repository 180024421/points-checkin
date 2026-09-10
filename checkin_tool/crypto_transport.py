"""run-jane 敏感接口的混合加密传输。

请求使用 RSA-OAEP(SHA-256/MGF1-SHA-256) 包裹随机 AES-256 key，
业务 JSON 使用 AES-GCM 加密。模块不会把明文凭据、密文或服务端原始响应写入日志。
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class CryptoTransportError(RuntimeError):
    """对调用方仅暴露不含敏感内容的通用错误。"""


class _KeyRejectedError(CryptoTransportError):
    pass


@dataclass(frozen=True)
class PublicKeyInfo:
    version: str
    key_id: str
    key: rsa.RSAPublicKey


_KEY_CACHE: dict[str, PublicKeyInfo] = {}
_KEY_CACHE_LOCK = threading.Lock()


def _looks_like_key_error(text: str) -> bool:
    marker = text.lower()
    return any(
        word in marker
        for word in (
            "keyid", "key_id", "key-id", "key id", "public key",
            "unknown key", "invalid key", "公钥", "密钥",
        )
    )


def public_key_url_for(url: str) -> str:
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        raise CryptoTransportError("安全服务地址无效")
    return urlunsplit((parts.scheme, parts.netloc, "/api/crypto/public-key", "", ""))


def parse_public_key_response(payload: Any) -> PublicKeyInfo:
    """解析 GET /api/crypto/public-key 的包装响应。"""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise CryptoTransportError("公钥响应格式无效")
    version = str(data.get("version") or "").strip()
    key_id = str(data.get("keyId") or "").strip()
    encoded = str(data.get("publicKey") or "").strip()
    if not version or not key_id or not encoded:
        raise CryptoTransportError("公钥响应字段不完整")
    try:
        loaded = serialization.load_der_public_key(base64.b64decode(encoded, validate=True))
    except Exception as exc:
        raise CryptoTransportError("服务端公钥无效") from exc
    if not isinstance(loaded, rsa.RSAPublicKey):
        raise CryptoTransportError("服务端公钥类型无效")
    return PublicKeyInfo(version=version, key_id=key_id, key=loaded)


def _read_json(req: Request, timeout: float, *, purpose: str) -> dict[str, Any]:
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except HTTPError as exc:
        body = b""
        try:
            body = exc.read()
        except Exception:
            pass
        marker = body.decode("utf-8", errors="ignore")
        # 敏感接口即使返回 4xx，正文仍是可认证的加密信封。保留该信封交给
        # secure_json_request 解密，避免把“密码错误”等正常业务结果误报成网络错误。
        try:
            value = json.loads(marker)
            encrypted = value if isinstance(value, dict) else {}
            if isinstance(encrypted.get("data"), dict):
                encrypted = encrypted["data"]
            if encrypted.get("iv") and encrypted.get("ciphertext"):
                return value
        except Exception:
            pass
        if exc.code in (409, 410) or _looks_like_key_error(marker):
            raise _KeyRejectedError("服务端密钥已更新") from exc
        raise CryptoTransportError(f"{purpose}失败（HTTP {exc.code}）") from exc
    except URLError as exc:
        raise CryptoTransportError(f"{purpose}网络失败") from exc
    except Exception as exc:
        raise CryptoTransportError(f"{purpose}失败") from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise CryptoTransportError(f"{purpose}响应格式无效") from exc
    if not isinstance(value, dict):
        raise CryptoTransportError(f"{purpose}响应格式无效")
    return value


def get_public_key(
    endpoint_url: str,
    timeout: float,
    *,
    force_refresh: bool = False,
) -> PublicKeyInfo:
    key_url = public_key_url_for(endpoint_url)
    with _KEY_CACHE_LOCK:
        if not force_refresh and key_url in _KEY_CACHE:
            return _KEY_CACHE[key_url]
    req = Request(
        key_url,
        headers={"Accept": "application/json", "User-Agent": "CursorTool/1.3.0"},
        method="GET",
    )
    info = parse_public_key_response(_read_json(req, timeout, purpose="获取公钥"))
    with _KEY_CACHE_LOCK:
        _KEY_CACHE[key_url] = info
    return info


def clear_public_key_cache(endpoint_url: str | None = None) -> None:
    with _KEY_CACHE_LOCK:
        if endpoint_url is None:
            _KEY_CACHE.clear()
        else:
            _KEY_CACHE.pop(public_key_url_for(endpoint_url), None)


def _aad(
    version: str,
    key_id: str,
    timestamp: int,
    request_id: str,
    scope: str,
    *,
    response: bool = False,
) -> bytes:
    text = f"{version}|{key_id}|{timestamp}|{request_id}|{scope}"
    if response:
        text += "|response"
    return text.encode("utf-8")


def build_request_envelope(
    payload: Any,
    scope: str,
    key_info: PublicKeyInfo,
) -> tuple[dict[str, Any], bytes]:
    if not scope:
        raise CryptoTransportError("安全请求 scope 不能为空")
    aes_key = AESGCM.generate_key(bit_length=256)
    iv = os.urandom(12)
    timestamp = int(time.time() * 1000)
    request_id = str(uuid.uuid4())
    plaintext = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    ciphertext = AESGCM(aes_key).encrypt(
        iv,
        plaintext,
        _aad(key_info.version, key_info.key_id, timestamp, request_id, scope),
    )
    encrypted_key = key_info.key.encrypt(
        aes_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    envelope = {
        "version": key_info.version,
        "keyId": key_info.key_id,
        "timestamp": timestamp,
        "requestId": request_id,
        "scope": scope,
        "iv": base64.b64encode(iv).decode("ascii"),
        "encryptedKey": base64.b64encode(encrypted_key).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }
    return envelope, aes_key


def decrypt_response_envelope(
    envelope: dict[str, Any],
    aes_key: bytes,
    *,
    version: str,
    key_id: str,
    timestamp: int,
    request_id: str,
    scope: str,
) -> Any:
    """解密响应；响应 ciphertext 按协议已包含 GCM tag。"""
    try:
        iv = base64.b64decode(str(envelope["iv"]), validate=True)
        ciphertext = base64.b64decode(str(envelope["ciphertext"]), validate=True)
        plaintext = AESGCM(aes_key).decrypt(
            iv,
            ciphertext,
            _aad(version, key_id, timestamp, request_id, scope, response=True),
        )
        return json.loads(plaintext.decode("utf-8"))
    except Exception as exc:
        raise CryptoTransportError("安全响应校验失败") from exc


def _encrypted_part(response: dict[str, Any]) -> dict[str, Any] | None:
    if "iv" in response and "ciphertext" in response:
        return response
    data = response.get("data")
    if isinstance(data, dict) and "iv" in data and "ciphertext" in data:
        return data
    return None


def secure_json_request(
    url: str,
    payload: Any,
    scope: str,
    timeout: float = 20.0,
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """向敏感接口发送加密信封；keyId 失效时刷新公钥并仅重试一次。"""
    for attempt in range(2):
        key_info = get_public_key(url, timeout, force_refresh=attempt == 1)
        envelope, aes_key = build_request_envelope(payload, scope, key_info)
        raw = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
        request_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "CursorTool/1.3.0",
        }
        request_headers.update(headers or {})
        req = Request(url, data=raw, headers=request_headers, method="POST")
        try:
            response = _read_json(req, timeout, purpose="安全请求")
            encrypted = _encrypted_part(response)
            if encrypted is None:
                marker = json.dumps(response, ensure_ascii=False)
                if _looks_like_key_error(marker):
                    raise _KeyRejectedError("服务端密钥已更新")
                raise CryptoTransportError("安全响应格式无效")
            decoded = decrypt_response_envelope(
                encrypted,
                aes_key,
                version=key_info.version,
                key_id=key_info.key_id,
                timestamp=int(envelope["timestamp"]),
                request_id=str(envelope["requestId"]),
                scope=scope,
            )
            if not isinstance(decoded, dict):
                raise CryptoTransportError("安全响应格式无效")
            return decoded
        except _KeyRejectedError:
            clear_public_key_cache(url)
            if attempt == 0:
                continue
            raise CryptoTransportError("服务端密钥校验失败")
    raise CryptoTransportError("安全请求失败")
