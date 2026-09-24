# -*- coding: utf-8 -*-
"""新增后端能力：备份轮转 / 恢复白名单 / 跨天趋势 / CSV 导出。

沿用既有测试的做法——把 ``account_store`` 的文件常量指到 ``tmp_path``，
并把服务器聚合数据打桩成空，只验本机这一侧的逻辑。
"""
from __future__ import annotations

import zipfile
from datetime import datetime, timedelta

from checkin_tool import account_store, backup, report_export


def test_rotate_keeps_only_recent_backups(tmp_path, monkeypatch):
    monkeypatch.setattr(account_store, "data_root", lambda: tmp_path)
    d = backup.backup_dir()
    d.mkdir(parents=True, exist_ok=True)
    for i in range(15):
        (d / f"checkintool_backup_{20260101000000 + i:014d}.zip").write_bytes(b"x")

    removed = backup._rotate(keep=10)
    remain = sorted(p.name for p in d.glob("checkintool_backup_*.zip"))
    assert len(removed) == 5
    assert len(remain) == 10
    # 删的是最旧的，留下时间戳最大的 10 份
    assert "checkintool_backup_20260101000000.zip" not in remain
    assert "checkintool_backup_20260101000014.zip" in remain


def test_create_backup_writes_zip(tmp_path, monkeypatch):
    monkeypatch.setattr(account_store, "data_root", lambda: tmp_path)
    acc = tmp_path / "accounts.json"
    acc.write_text('{"accounts": []}', encoding="utf-8")
    monkeypatch.setattr(account_store, "ACCOUNTS_FILE", acc)

    out = backup.create_backup(keep=10, log=lambda _m: None)
    assert out["ok"] is True
    assert zipfile.is_zipfile(out["path"])


def test_restore_skips_non_allowlist_and_huge(tmp_path, monkeypatch):
    monkeypatch.setattr(account_store, "data_root", lambda: tmp_path)
    src = tmp_path / "bak.zip"
    big = b"a" * (backup.RESTORE_MAX_MEMBER_BYTES + 10)
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("accounts.json", '{"accounts": []}')
        zf.writestr("settings.json", "{}")
        zf.writestr("device_id.txt", "secret")           # 不在白名单：应跳过
        zf.writestr("evil.exe", big)                     # 不在白名单：应跳过
        zf.writestr("run_log.json", '{"runs": []}')

    out = backup.restore_from_backup(str(src), log=lambda _m: None)
    assert out["ok"] is True
    assert set(out["restored"]) == {"accounts.json", "settings.json", "run_log.json"}
    assert "device_id.txt" in out["skipped"]
    assert "evil.exe(过大)" in out["skipped"] or "evil.exe" in out["skipped"]
    assert (tmp_path / "accounts.json").exists()
    assert not (tmp_path / "device_id.txt").exists()


def _today(offset_days: int = 0) -> str:
    return (datetime.now() - timedelta(days=offset_days)).strftime("%Y-%m-%d")


def test_history_stats_daily_trend_and_success_rate(tmp_path, monkeypatch):
    monkeypatch.setattr(account_store, "RUN_LOG_FILE", tmp_path / "run_log.json")
    monkeypatch.setattr(account_store, "_aggregated_runs_cached", lambda: [])
    monkeypatch.setattr(account_store, "load_accounts", lambda **_k: [])

    # 今天：a 成功、b 失败、c 未开放（未开放不进成功率分母）
    account_store.append_run_log({"account_id": "a", "ok": True, "day": _today()})
    account_store.append_run_log({"account_id": "b", "ok": False, "message": "网络错", "day": _today()})
    account_store.append_run_log({"account_id": "c", "ok": False, "skipped": True, "day": _today()})
    # 昨天：a、b 都成功
    account_store.append_run_log({"account_id": "a", "ok": True, "day": _today(1)})
    account_store.append_run_log({"account_id": "b", "ok": True, "day": _today(1)})

    stats = account_store.history_stats(days=7)
    by_day = {d["day"]: d for d in stats["days"]}
    today = by_day[_today()]
    assert today["signed"] == 1
    assert today["failed"] == 1
    assert today["notopen"] == 1
    assert today["attempted"] == 2  # 未开放不算分母
    assert today["success_rate"] == 0.5
    yest = by_day[_today(1)]
    assert yest["signed"] == 2 and yest["failed"] == 0
    # b 在窗口内失败过一次 → 进问题账号榜
    assert any(p["account_id"] == "b" and p["failed"] == 1 for p in stats["problem_accounts"])


def test_history_stats_latest_per_account_per_day(tmp_path, monkeypatch):
    monkeypatch.setattr(account_store, "RUN_LOG_FILE", tmp_path / "run_log.json")
    monkeypatch.setattr(account_store, "_aggregated_runs_cached", lambda: [])
    monkeypatch.setattr(account_store, "load_accounts", lambda **_k: [])

    # 同一账号当天先失败、后补签成功（append 是最新插到队首）→ 只认最新那条成功
    account_store.append_run_log({"account_id": "a", "ok": False, "message": "早期失败", "day": _today()})
    account_store.append_run_log({"account_id": "a", "ok": True, "day": _today()})
    stats = account_store.history_stats(days=3)
    today = stats["days"][0]
    assert today["signed"] == 1 and today["failed"] == 0


def test_export_csv(tmp_path, monkeypatch):
    monkeypatch.setattr(account_store, "RUN_LOG_FILE", tmp_path / "run_log.json")
    monkeypatch.setattr(account_store, "CREDIT_HISTORY_FILE", tmp_path / "credit_history.json")
    monkeypatch.setattr(account_store, "ACCOUNTS_FILE", tmp_path / "accounts.json")
    monkeypatch.setattr(account_store, "_aggregated_runs_cached", lambda: [])
    monkeypatch.setattr(account_store, "load_accounts", lambda **_k: [])

    account_store.append_run_log({"account_id": "a", "provider": "workbuddy", "label": "小号A", "ok": True, "credits": 5})
    account_store.append_credit_history({"account_id": "a", "provider": "workbuddy", "ok": True, "credits": 105, "streak": 3})

    runs_csv = tmp_path / "runs.csv"
    n = report_export.export_run_logs_csv(runs_csv)
    assert n == 1
    head = runs_csv.read_text(encoding="utf-8-sig").splitlines()[0]
    assert head.startswith("day,at,account_id,label,provider,ok")

    credit_csv = tmp_path / "credit.csv"
    n2 = report_export.export_credit_history_csv(credit_csv)
    assert n2 == 1
    assert "105" in credit_csv.read_text(encoding="utf-8-sig")
