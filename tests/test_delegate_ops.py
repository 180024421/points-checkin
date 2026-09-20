# -*- coding: utf-8 -*-
"""``checkin_tool.delegate`` —— 两条前端共用的代跑写操作。

这段逻辑原来只长在 webview 的 API 类里，没有测试也没有第二处调用；抽成共用模块后
最要紧的两条性质必须钉住：批量上传一条失败不掀翻整批；更换账号时服务器一旦删掉旧记录，
本机状态一定要收尾（新账号上传失败要退回本机模式）。
"""

from __future__ import annotations

from typing import Any

import pytest

from checkin_tool import delegate


class FakeStore:
    def __init__(self, rows: list[dict[str, Any]], fail_for: set[str] | None = None):
        self.rows = {str(r["id"]): dict(r) for r in rows}
        self.fail_for = fail_for or set()
        self.writes: list[str] = []

    def load_accounts(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.rows.values()]

    def try_upsert_account(self, account: dict[str, Any]) -> dict[str, Any]:
        account_id = str(account.get("id"))
        self.writes.append(account_id)
        if account_id in self.fail_for:
            return {"ok": False, "code": "SEATS_EXHAUSTED", "message": "坐席已用满"}
        self.rows[account_id] = dict(account)
        return {"ok": True}


class FakeServer:
    def __init__(self, *, delete_ok: bool = True, sync_ok: bool = True, has_token: bool = True):
        self.delete_ok = delete_ok
        self.sync_ok = sync_ok
        self.has_token = has_token
        self.deleted: list[str] = []
        self.synced: list[str] = []

    def delete_server_account(self, server_account_id: Any) -> dict[str, Any]:
        self.deleted.append(str(server_account_id))
        return {"ok": self.delete_ok, "message": "" if self.delete_ok else "服务器说不存在"}

    def sync_server_blob(self, account: dict[str, Any], *, log=None, force: bool = False):  # noqa: ANN001
        account_id = str(account.get("id"))
        self.synced.append(account_id)
        if not self.has_token:
            return None
        return {"ok": self.sync_ok, "message": "" if self.sync_ok else "上传被拒"}


@pytest.fixture
def patched(monkeypatch):
    def _patch(rows, *, store_fail: set[str] | None = None, **server_kwargs):
        store = FakeStore(rows, store_fail)
        server = FakeServer(**server_kwargs)
        monkeypatch.setattr(delegate.account_store, "load_accounts", store.load_accounts)
        monkeypatch.setattr(delegate.account_store, "try_upsert_account", store.try_upsert_account)
        monkeypatch.setattr(delegate.server_client, "delete_server_account", server.delete_server_account)
        monkeypatch.setattr(delegate.server_client, "sync_server_blob", server.sync_server_blob)
        return store, server

    return _patch


def _account(account_id: str, **over) -> dict[str, Any]:  # noqa: ANN003
    row: dict[str, Any] = {
        "id": account_id,
        "provider": "workbuddy",
        "label": f"号{account_id}",
        "run_mode": "local",
        "enabled": True,
        "token_blob": {"token": "t-" + account_id},
    }
    row.update(over)
    return row


def test_upload_marks_server_and_sets_task_flag(patched):
    store, server = patched([_account("a"), _account("b")])
    uploaded = delegate.upload_delegate_accounts(store.load_accounts(), task_enabled=True, log=None)
    assert uploaded == 2
    assert store.rows["a"]["run_mode"] == "server"
    assert store.rows["a"]["task_enabled"] is True
    assert server.synced == ["a", "b"]


def test_upload_skips_tokenless_row_and_keeps_the_batch_going(patched):
    rows = [
        _account("a", token_blob={}),
        _account("b"),
        _account("c"),
    ]
    store, server = patched(rows, store_fail={"b"})
    logs: list[str] = []
    uploaded = delegate.upload_delegate_accounts(rows, task_enabled=False, log=logs.append)
    # 无 token 的跳过、额度拦住的只跳过它自己，后面的账号照样推上去
    assert uploaded == 1
    assert server.synced == ["c"]
    assert any("无 token" in m for m in logs)
    assert any("坐席已用满" in m for m in logs)


def test_workbuddy_task_flag_only_written_for_workbuddy(patched):
    rows = [_account("a", provider="traework"), _account("b")]
    store, _ = patched(rows)
    delegate.upload_delegate_accounts(rows, task_enabled=True, log=None)
    assert "task_enabled" not in store.rows["a"]
    assert store.rows["b"]["task_enabled"] is True


def test_replace_requires_server_mode_old_and_local_new(patched):
    rows = [_account("old", run_mode="local"), _account("new")]
    store, server = patched(rows)
    out = delegate.replace_server_account("old", "new", log=None)
    assert out["ok"] is False and "不是服务器代跑模式" in out["message"]
    assert server.deleted == [] and store.writes == []

    rows = [_account("old", run_mode="server", server_account_id=77), _account("new", run_mode="server")]
    store, server = patched(rows)
    out = delegate.replace_server_account("old", "new", log=None)
    assert out["ok"] is False and "已是服务器代跑模式" in out["message"]
    assert server.deleted == []


def test_replace_refuses_without_server_id(patched):
    rows = [_account("old", run_mode="server"), _account("new")]
    store, server = patched(rows)
    out = delegate.replace_server_account("old", "new", log=None)
    assert out["ok"] is False and "没有服务器代跑记录 id" in out["message"]
    assert server.deleted == []


def test_replace_rolls_back_local_mode_when_upload_fails(patched):
    rows = [_account("old", run_mode="server", server_account_id=77), _account("new")]
    store, server = patched(rows, sync_ok=False)
    out = delegate.replace_server_account("old", "new", log=None)
    assert out["ok"] is False and "上传服务器失败" in out["message"]
    # 关键收尾：新账号退回本机，否则界面显示在代跑而服务器根本没有
    assert store.rows["new"]["run_mode"] == "local"


def test_replace_rolls_back_when_no_token_available(patched):
    rows = [_account("old", run_mode="server", server_account_id=77), _account("new")]
    store, _ = patched(rows, has_token=False)
    out = delegate.replace_server_account("old", "new", log=None)
    assert out["ok"] is False and "没有可用 token" in out["message"]
    assert store.rows["new"]["run_mode"] == "local"


def test_replace_success_moves_both_sides(patched):
    rows = [
        _account("old", run_mode="server", server_account_id=77),
        _account("new", provider="workbuddy"),
    ]
    store, server = patched(rows)
    out = delegate.replace_server_account("old", "new", log=None)
    assert out["ok"] is True
    assert server.deleted == ["77"] and server.synced == ["new"]
    assert store.rows["new"]["run_mode"] == "server"
    # 旧记录不再代跑：本机那行改回本机并摘掉服务端 id
    assert store.rows["old"]["run_mode"] == "local"
    assert "server_account_id" not in store.rows["old"]


def test_replace_stops_when_server_delete_fails(patched):
    rows = [_account("old", run_mode="server", server_account_id=77), _account("new")]
    store, server = patched(rows, delete_ok=False)
    out = delegate.replace_server_account("old", "new", log=None)
    assert out["ok"] is False and "删除服务器旧账号失败" in out["message"]
    # 旧记录还在服务器上：本机两行都不许动
    assert server.synced == []
    assert store.rows["new"]["run_mode"] == "local"
    assert store.rows["old"]["run_mode"] == "server"
