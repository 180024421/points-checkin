"""兼容明文 JSON 的本地安全存储（Windows DPAPI）。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import vault

MAGIC = b"CSDPAPI1"


def is_encrypted(path: str | Path) -> bool:
    target = Path(path)
    try:
        return target.read_bytes().startswith(MAGIC)
    except Exception:
        return False


def save_json(path: str | Path, value: Any) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    vault.dump(value, temp)
    os.replace(temp, target)
    return target


def load_json(
    path: str | Path,
    default: Any,
    *,
    migrate_plaintext: bool = True,
) -> Any:
    target = Path(path)
    if not target.exists():
        return default
    was_encrypted = is_encrypted(target)
    value = vault.load(target)
    if migrate_plaintext and vault.is_secure() and not was_encrypted:
        save_json(target, value)
    return value
