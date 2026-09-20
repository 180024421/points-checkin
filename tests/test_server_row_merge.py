# -*- coding: utf-8 -*-
"""合并视图：服务器代跑记录必须并进本机那一行，且永远不落盘成幽灵账号。"""

from checkin_tool import account_store


def _setup(tmp_path, monkeypatch, server_rows):
    monkeypatch.setattr(account_store, "ACCOUNTS_FILE", tmp_path / "accounts.json")
    monkeypatch.setattr(account_store, "_server_accounts_cached", lambda: list(server_rows))
    account_store._purged_server_rows = 0
    return server_rows


def _drop_on_server_delete(monkeypatch, server_rows, deleted):
    """删服务端记录成功后，让假服务器列表也跟着少一条（真环境就是如此）。"""

    def fake_delete(sid):
        deleted.append(sid)
        server_rows[:] = [r for r in server_rows if str(r.get("server_account_id")) != str(sid)]
        return {"ok": True}

    monkeypatch.setattr(account_store.server_client, "delete_server_account", fake_delete)


def _server_row(**over):
    """按 _normalize_server_account 的产物形状造数据（服务端自增 id + 打标）。"""
    row = {
        "id": "12",
        "server_account_id": "12",
        "provider": "workbuddy",
        "label": "S",
        "run_mode": "server",
        "source": "server",
        "enabled": True,
        "token_blob": {},
    }
    row.update(over)
    return row


def test_server_record_merges_into_local_twin_by_client_account_id(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, [_server_row(client_account_id="local-uuid")])
    account_store.save_accounts([{"id": "local-uuid", "provider": "workbuddy", "label": "L", "token_blob": {"uid": "u1"}}])

    rows = account_store.load_accounts()
    assert [r["id"] for r in rows] == ["local-uuid"], "同一个代挂账号只能出现一行"
    assert rows[0]["run_mode"] == "server"
    assert rows[0]["server_account_id"] == "12"
    assert rows[0]["token_blob"] == {"uid": "u1"}, "本机凭证不能被服务器的空 blob 覆盖"

    account_store.save_accounts(rows)
    stored = account_store.load_accounts(include_server=False)
    assert len(stored) == 1 and stored[0]["id"] == "local-uuid"
    assert stored[0]["server_account_id"] == "12", "代跑关系要留在本机行上，重启后还能认出来"


def test_server_record_matches_local_by_identity_without_client_account_id(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, [_server_row(identity="u1")])
    account_store.save_accounts(
        [{"id": "local-uuid", "provider": "workbuddy", "label": "L", "token_blob": {"uid": "u1"}}]
    )
    assert [r["id"] for r in account_store.load_accounts()] == ["local-uuid"]


def test_unmatched_server_row_is_a_read_only_orphan(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, [_server_row()])
    account_store.save_accounts([])

    rows = account_store.load_accounts()
    assert [r["id"] for r in rows] == ["srv:12"]
    assert rows[0]["source"] == "server"
    account_store.save_accounts(rows)
    assert account_store.load_accounts(include_server=False) == [], "别机上传的记录不能写进本机账号库"


def test_ghost_rows_from_old_versions_are_purged(tmp_path, monkeypatch):
    # 旧版本把整条服务器记录（自增 id）写进了 accounts.json，服务端删号后就留下幽灵
    _setup(tmp_path, monkeypatch, [])
    account_store.save_accounts([{"id": "local-uuid", "provider": "workbuddy"}])
    account_store.save_json(
        account_store.ACCOUNTS_FILE,
        {
            "accounts": [
                {"id": "local-uuid", "provider": "workbuddy"},
                {"id": "12", "provider": "workbuddy", "run_mode": "server"},
            ]
        },
    )

    assert [r["id"] for r in account_store.load_accounts()] == ["local-uuid"]
    account_store.save_accounts(account_store.load_accounts())
    assert [r["id"] for r in account_store.load_accounts(include_server=False)] == ["local-uuid"]


def test_delete_with_server_uses_server_id_not_local_uuid(tmp_path, monkeypatch):
    server_rows = _setup(tmp_path, monkeypatch, [_server_row(client_account_id="local-uuid")])
    account_store.save_accounts([{"id": "local-uuid", "provider": "workbuddy", "run_mode": "server"}])
    deleted = []
    _drop_on_server_delete(monkeypatch, server_rows, deleted)

    result = account_store.delete_account_with_server("local-uuid")
    assert result["ok"] is True
    assert deleted == ["12"], "必须用服务端自增 id 删，本机 uuid 服务端查不到"
    assert account_store.load_accounts() == []


def test_delete_pure_server_row_only_touches_server(tmp_path, monkeypatch):
    server_rows = _setup(tmp_path, monkeypatch, [_server_row()])
    account_store.save_accounts([])
    calls = []
    _drop_on_server_delete(monkeypatch, server_rows, calls)

    assert account_store.delete_account_with_server("srv:12")["ok"] is True
    assert calls == ["12"]
    assert account_store.load_accounts() == []


def test_delete_local_only_account_never_calls_server(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, [])
    account_store.save_accounts([{"id": "local-uuid", "provider": "workbuddy", "run_mode": "local"}])

    def boom(_sid):
        raise AssertionError("本机账号不该触发服务器删除")

    monkeypatch.setattr(account_store.server_client, "delete_server_account", boom)
    assert account_store.delete_account_with_server("local-uuid")["ok"] is True


def test_delete_reports_server_failure_instead_of_claiming_success(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, [_server_row(client_account_id="local-uuid")])
    account_store.save_accounts([{"id": "local-uuid", "provider": "workbuddy", "run_mode": "server"}])
    monkeypatch.setattr(
        account_store.server_client, "delete_server_account", lambda _sid: {"ok": False, "message": "网络超时"}
    )
    result = account_store.delete_account_with_server("local-uuid")
    assert result["ok"] is False
    assert "网络超时" in result["message"]


def test_set_run_mode_rejects_pure_server_row(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, [_server_row()])
    account_store.save_accounts([])
    result = account_store.set_run_mode("srv:12", "local")
    assert result["ok"] is False
    assert "只存在于服务器" in result["message"]


def test_set_run_mode_back_to_local_says_server_record_remains(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, [_server_row(client_account_id="local-uuid")])
    account_store.save_accounts([{"id": "local-uuid", "provider": "workbuddy", "run_mode": "server"}])

    assert account_store.set_run_mode("local-uuid", "local")["ok"] is True
    stored = account_store.load_accounts(include_server=False)[0]
    assert stored["run_mode"] == "local"
    assert "server_account_id" not in stored, "合并视图带来的字段不该被写回本地文件"
    # 服务器仍在代跑，所以视图里它依旧是代跑状态，界面文案不能骗人
    result = account_store.set_run_mode("local-uuid", "local")
    assert "服务器" in result["message"]
    assert account_store.load_accounts()[0]["run_mode"] == "server"


def test_set_run_mode_rejects_unknown_mode(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, [])
    account_store.save_accounts([{"id": "local-uuid", "provider": "workbuddy"}])
    assert account_store.set_run_mode("local-uuid", "cloud")["ok"] is False


def test_public_view_marks_server_only_row(monkeypatch):
    view = account_store.public_account_view(
        {"id": "srv:12", "server_account_id": "12", "provider": "workbuddy", "source": "server", "run_mode": "server"}
    )
    assert view["source"] == "server"
    assert view["server_account_id"] == "12"
    assert view["token_hint"] == "(仅服务器)", "本机没有凭证，不能显示成「已保存」"


def test_credit_refresh_skips_server_only_row(monkeypatch):
    """仅服务器的行本机没有凭证，不能拿空 blob 去打供应商接口。"""
    from checkin_tool import scheduler

    def boom(_blob):
        raise AssertionError("不该用空 token 查询积分")

    monkeypatch.setattr(scheduler.workbuddy, "query_from_blob", boom)
    result = scheduler.refresh_account_credits(
        {"id": "srv:12", "provider": "workbuddy", "source": "server", "run_mode": "server", "token_blob": {}}
    )
    assert result["ok"] is False
    assert "仅存在于服务器" in result["message"]


def test_update_account_only_touches_its_own_fields(tmp_path, monkeypatch):
    """账号库整表覆盖的老写法会让后写者拿旧快照抹掉别人刚写的字段。"""
    _setup(tmp_path, monkeypatch, [])
    account_store.save_accounts(
        [{"id": "a1", "provider": "workbuddy", "last_credits": 0, "token_blob": {"token": "old"}}]
    )

    assert account_store.update_account("a1", lambda _row: {"last_credits": 88}) is True
    assert account_store.update_account("a1", lambda _row: {"token_blob": {"token": "new"}}) is True

    row = account_store.load_accounts(include_server=False)[0]
    assert row["last_credits"] == 88 and row["token_blob"]["token"] == "new"


def test_update_account_sees_current_row_and_skips_empty_patch(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, [])
    account_store.save_accounts([{"id": "a1", "provider": "workbuddy", "last_credits": 7}])

    seen = {}

    def mutate(row):
        seen["credits"] = row.get("last_credits")
        return {}  # 空 patch = 不改

    assert account_store.update_account("a1", mutate) is False
    assert seen["credits"] == 7, "mutate 收到的必须是盘上的当前行"
    assert account_store.update_account("ghost", lambda _r: {"last_credits": 1}) is False
    assert account_store.load_accounts(include_server=False)[0]["last_credits"] == 7
