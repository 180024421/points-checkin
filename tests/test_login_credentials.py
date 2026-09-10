# -*- coding: utf-8 -*-
from checkin_tool import credential_store
from checkin_tool.login.traework import get_login_host, try_api_password_login
from checkin_tool.login.workbuddy import try_api_login


def test_credential_upsert_and_public_view(tmp_path, monkeypatch):
    monkeypatch.setattr(credential_store, "CREDENTIALS_FILE", tmp_path / "credentials.json")
    row = credential_store.upsert_credential(
        provider="workbuddy",
        username="a@example.com",
        password="secret",
        label="A",
    )
    assert row["id"]
    views = [credential_store.public_credential_view(r) for r in credential_store.load_credentials()]
    assert views[0]["username"] == "a@example.com"
    assert views[0]["has_password"] is True
    assert "password" not in views[0]


def test_wb_api_login_fails_gracefully():
    r = try_api_login("probe@example.com", "invalid-password")
    assert r.ok is False
    assert r.provider == "workbuddy"
    assert r.method == "api"


def test_trae_guidance_and_api_probe():
    host, note = get_login_host()
    assert host.startswith("http")
    r = try_api_password_login("probe@example.com", "invalid")
    assert r.ok is False
    assert "OAuth" in r.message or "API" in r.message
