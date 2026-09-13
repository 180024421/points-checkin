"""兼容明文 JSON 的本地安全存储（Windows DPAPI）。"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

from . import vault

MAGIC = b"CSDPAPI1"

# 后台线程（Trae 自动采集 / 调度）与 UI 线程可能同时写同一文件，
# 用进程内锁 + 独立临时名避免互相踩，偶发杀软占用则重试。
_SAVE_LOCK = threading.Lock()


def is_encrypted(path: str | Path) -> bool:
    target = Path(path)
    try:
        return target.read_bytes().startswith(MAGIC)
    except Exception:
        return False


def save_json(path: str | Path, value: Any) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    last_exc: Exception | None = None
    with _SAVE_LOCK:
        for attempt in range(3):
            try:
                vault.dump(value, temp)
                os.replace(temp, target)
                return target
            except PermissionError as exc:  # 杀软/占用瞬时锁，重试
                last_exc = exc
                time.sleep(0.05 * (attempt + 1))
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                break
    raise last_exc if last_exc else OSError(f"写入失败: {target}")


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
