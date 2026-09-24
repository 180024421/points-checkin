# -*- coding: utf-8 -*-
"""把积分历史 / 跑批记录导出成 CSV，供「导出」按钮与外部表格分析使用。

用 ``utf-8-sig``（带 BOM）：Excel 打开中文不乱码。字段取当前数据里稳定存在的列，
缺失留空，不因为某条记录少个字段就抛错。
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from . import account_store

_CREDIT_COLUMNS = ["day", "at", "account_id", "label", "provider", "credits", "streak", "ok", "message"]
_RUN_COLUMNS = ["day", "at", "account_id", "label", "provider", "ok", "already", "skipped", "credits", "streak", "message"]


def _account_labels() -> dict[str, dict[str, Any]]:
    try:
        rows = account_store.load_accounts()
    except Exception:  # noqa: BLE001 - 取不到标签不影响导出
        return {}
    out: dict[str, dict[str, Any]] = {}
    for acc in rows:
        aid = str(acc.get("id") or "")
        if aid:
            out[aid] = acc
    return out


def _write(path: Path, columns: list[str], records: list[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for rec in records:
            writer.writerow({col: rec.get(col, "") for col in columns})
    return len(records)


def export_credit_history_csv(path: str | Path, *, limit: int = account_store.MAX_CREDIT_HISTORY_ITEMS) -> int:
    labels = _account_labels()
    records: list[dict[str, Any]] = []
    for item in account_store.load_credit_history(limit=limit):
        aid = str(item.get("account_id") or "")
        acc = labels.get(aid, {})
        records.append(
            {
                "day": item.get("day") or "",
                "at": item.get("at") or "",
                "account_id": aid,
                "label": acc.get("label") or acc.get("id") or aid,
                "provider": item.get("provider") or acc.get("provider") or "",
                "credits": item.get("credits", ""),
                "streak": item.get("streak", ""),
                "ok": "1" if item.get("ok") else "0",
                "message": item.get("message") or "",
            }
        )
    return _write(Path(path), _CREDIT_COLUMNS, records)


def export_run_logs_csv(path: str | Path, *, limit: int = account_store.MAX_RUN_LOGS) -> int:
    labels = _account_labels()
    records: list[dict[str, Any]] = []
    for row in account_store.load_run_logs(limit=limit):
        aid = str(row.get("account_id") or "")
        acc = labels.get(aid, {})
        records.append(
            {
                "day": row.get("day") or "",
                "at": row.get("at") or "",
                "account_id": aid,
                "label": row.get("label") or acc.get("label") or aid,
                "provider": row.get("provider") or acc.get("provider") or "",
                "ok": "1" if row.get("ok") else "0",
                "already": "1" if row.get("already") else "0",
                "skipped": "1" if row.get("skipped") else "0",
                "credits": row.get("credits", ""),
                "streak": row.get("streak", ""),
                "message": row.get("message") or "",
            }
        )
    return _write(Path(path), _RUN_COLUMNS, records)
