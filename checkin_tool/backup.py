# -*- coding: utf-8 -*-
"""本机备份 / 恢复核心：打包数据文件、白名单恢复、按份数轮转。

PyWebView 与 tkinter 两端共用这一份，避免「备份只在某一端有、两端的
白名单/上限各写一份会漂移」。凭证与设备身份绑定「当前用户 + 机器」，
换机解不开，因此这里只保证**同机**备份恢复，不做换机迁移承诺。
"""

from __future__ import annotations

import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from . import account_store, license_client
from .settings import settings_path

BACKUP_PREFIX = "checkintool_backup_"
BACKUP_SUFFIX = ".zip"

# 默认保留最近多少份备份；超出即删最旧。可由设置项 backup_keep_count 覆盖。
DEFAULT_KEEP = 10
_KEEP_FLOOR, _KEEP_CEIL = 1, 100

# 备份里允许恢复的文件名（其余一律忽略）：避免恶意 zip 往数据目录写任意文件。
# license_cache.json / device_id.txt 含票据与设备身份，刻意不参与恢复。
RESTORE_ALLOWLIST = {
    "accounts.json",
    "run_log.json",
    "live_log.json",
    "credit_history.json",
    "settings.json",
}
RESTORE_MAX_MEMBER_BYTES = 20 * 1024 * 1024


def backup_dir() -> Path:
    return account_store.data_root() / "backup"


def _backup_files() -> list[Path]:
    return [
        account_store.ACCOUNTS_FILE,
        account_store.RUN_LOG_FILE,
        account_store.LIVE_LOG_FILE,
        account_store.CREDIT_HISTORY_FILE,
        settings_path(),
        license_client.LICENSE_CACHE,
        license_client.DEVICE_ID_FILE,
    ]


def normalize_keep(raw: Any) -> int:
    try:
        value = int(float(str(raw if raw is not None else "").strip()))
    except (TypeError, ValueError):
        return DEFAULT_KEEP
    return max(_KEEP_FLOOR, min(_KEEP_CEIL, value))


def _rotate(keep: int, log: Any = None) -> list[str]:
    """保留最近 ``keep`` 份，删除更早的。返回被删掉的文件名。"""
    d = backup_dir()
    backups = sorted(
        (p for p in d.glob(BACKUP_PREFIX + "*" + BACKUP_SUFFIX) if p.is_file()),
        key=lambda p: p.name,
    )
    # 文件名带时间戳，按名字排序即按时间排序
    excess = len(backups) - max(1, keep)
    removed: list[str] = []
    for old in backups[: max(0, excess)]:
        try:
            old.unlink()
            removed.append(old.name)
        except OSError as exc:
            if log:
                log(f"清理旧备份失败 {old.name}: {exc}")
    return removed


def create_backup(keep: int = DEFAULT_KEEP, log: Any = None) -> dict[str, Any]:
    """打包数据文件到带时间戳的 zip，并把备份份数轮转到最近 ``keep`` 份。"""
    try:
        d = backup_dir()
        d.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        backup_path = d / f"{BACKUP_PREFIX}{timestamp}{BACKUP_SUFFIX}"

        with zipfile.ZipFile(backup_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            for file_path in _backup_files():
                if file_path.exists():
                    zipf.write(file_path, arcname=file_path.name)

        removed = _rotate(normalize_keep(keep), log=log)
        msg = f"数据已备份到: {backup_path}"
        if removed:
            msg += f"（清理旧备份 {len(removed)} 份）"
        if log:
            log(msg)
        return {"ok": True, "message": msg, "path": str(backup_path), "removed": removed}
    except Exception as exc:  # noqa: BLE001 - 备份失败只回报，不掀调用方
        if log:
            log(f"数据备份失败: {exc}")
        return {"ok": False, "message": f"数据备份失败: {exc}"}


def restore_from_backup(backup_file_path: str, log: Any = None) -> dict[str, Any]:
    """从备份 zip 恢复数据：只认白名单文件名，过大与越界一律跳过。"""
    try:
        backup_path = Path(str(backup_file_path or "").strip())
        if not backup_path.is_file():
            return {"ok": False, "message": "备份文件不存在。"}

        data_root_path = account_store.data_root()
        restored: list[str] = []
        skipped: list[str] = []
        with zipfile.ZipFile(backup_path, "r") as zipf:
            for member in zipf.infolist():
                if member.is_dir():
                    continue
                name = Path(member.filename).name  # 只取文件名，天然免疫路径穿越
                if name not in RESTORE_ALLOWLIST:
                    skipped.append(name)
                    continue
                if member.file_size > RESTORE_MAX_MEMBER_BYTES:
                    skipped.append(f"{name}(过大)")
                    continue
                with zipf.open(member) as src, open(data_root_path / name, "wb") as outfile:
                    outfile.write(src.read())
                restored.append(name)

        if not restored:
            return {"ok": False, "message": "备份中没有任何可恢复的数据文件。"}
        msg = (
            f"数据已从 {backup_path.name} 恢复：{', '.join(restored)}"
            + (f"；已忽略 {', '.join(skipped)}" if skipped else "")
            + "。请重启应用以使更改生效。"
        )
        if log:
            log(msg)
        return {
            "ok": True,
            "message": "数据已恢复。请重启应用以使更改生效。",
            "restored": restored,
            "skipped": skipped,
        }
    except Exception as exc:  # noqa: BLE001
        if log:
            log(f"数据恢复失败: {exc}")
        return {"ok": False, "message": f"数据恢复失败: {exc}"}
