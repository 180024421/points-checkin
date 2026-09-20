# -*- coding: utf-8 -*-
"""把凭证从「会写进日志 / 落盘 / 显示给用户」的字符串里剔除。

用于错误消息：服务端返回体常带 access_token / refreshToken / 私钥，
原样拼进 last_error 会随 accounts.json 落盘并显示在前端。
"""

from __future__ import annotations

import json
import re
from typing import Any

_SENSITIVE_KEYS = {
    "token",
    "accesstoken",
    "access_token",
    "refreshtoken",
    "refresh_token",
    "idtoken",
    "id_token",
    "securitytoken",
    "sessiontoken",
    "authtoken",
    "auth_token",
    "authcode",
    "auth_code",
    "ticket",
    "cardcode",
    "card_code",
    "password",
    "passwd",
    "secret",
    "clientsecret",
    "client_secret",
    "code_verifier",
    "authorization",
    "cookie",
    "private_key",
    "private_key_pem",
    "privatekeypem",
    "devicecert",
}

_MASK = "***"

_KEY_VALUE_RE = re.compile(
    r"""(?ix)
    (?P<prefix>["']?(?P<key>[A-Za-z_][A-Za-z0-9_]*)["']?\s*[:=]\s*)
    (?P<q>["']?)(?P<val>[^\s"',;}\]]+)(?P=q)?
    """
)

# JWT / 长随机串：eyJ 开头或长度 >= 32 的 base64 样串
_BLOB_RE = re.compile(r"(?x) (?<![A-Za-z0-9_\-/.]) ((?:eyJ|eyJhbGci)[A-Za-z0-9_\-=.]{8,})")
_LONG_RE = re.compile(r"(?<![A-Za-z0-9_\-/.])([A-Za-z0-9_\-+/=.]{40,})(?![A-Za-z0-9_\-/.])")


def is_sensitive_key(key: Any) -> bool:
    text = str(key or "").strip().lower()
    if text in _SENSITIVE_KEYS:
        return True
    return any(marker in text for marker in ("token", "password", "secret", "privatekey", "private_key"))


def redact(value: Any) -> Any:
    """递归复制并把敏感字段的值替换为 ***。"""
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            out[k] = _MASK if is_sensitive_key(k) else redact(v)
        return out
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, tuple):
        return [redact(v) for v in value]
    return value


def mask_text(text: Any, limit: int = 200) -> str:
    """对只拿到原始字符串的场景做兜底脱敏。"""
    raw = text if isinstance(text, str) else str(text or "")
    if not raw:
        return ""

    def _kv(match: re.Match[str]) -> str:
        if is_sensitive_key(match.group("key")):
            return f"{match.group('prefix')}{_MASK}"
        return match.group(0)

    masked = _KEY_VALUE_RE.sub(_kv, raw)
    masked = _BLOB_RE.sub(_MASK, masked)
    masked = _LONG_RE.sub(_MASK, masked)
    return masked[:limit]


def brief(payload: Any, limit: int = 200) -> str:
    """把（可能含凭证的）响应体压成可安全外泄的简短字符串。"""
    if isinstance(payload, str):
        return mask_text(payload, limit)
    try:
        dumped = json.dumps(redact(payload), ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001 - 极端对象兜底
        dumped = str(payload)
    return mask_text(dumped, limit)
