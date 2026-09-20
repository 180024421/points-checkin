# -*- coding: utf-8 -*-
"""传输策略与明文回退：中间人不能靠干扰加密握手把卡密降级成明文请求。"""

import io
from urllib.error import HTTPError, URLError

import pytest

from checkin_tool import crypto_transport, license_client
from checkin_tool.license_client import (
    _is_https,
    _post_json,
    _post_plain,
    _unwrap,
    insecure_transport_allowed,
    public_license_view,
    ticket_still_valid,
    transport_guard,
)

HTTP_URL = "http://127.0.0.1:8080/api/app-license/demo/redeem"
HTTPS_URL = "https://license.example.com/api/app-license/demo/redeem"


def test_transport_guard_only_blocks_plaintext_when_disallowed():
    assert transport_guard({"base_url": "http://a", "allow_insecure_transport": False}).startswith("授权服务地址是明文")
    assert transport_guard({"base_url": "http://a", "allow_insecure_transport": True}) == ""
    assert transport_guard({"base_url": "https://a", "allow_insecure_transport": False}) == ""
    assert transport_guard({"base_url": "", "allow_insecure_transport": False}) == ""
    # 默认允许：当前授权服务器还没有 TLS，默认收紧会直接把产品锁死
    assert insecure_transport_allowed({}) is True
    assert insecure_transport_allowed({"allow_insecure_transport": False}) is False
    assert _is_https("HTTPS://a") and not _is_https("http://a")


@pytest.fixture
def plain_spy(monkeypatch):
    calls: list[dict] = []

    def fake_plain(url, body, timeout):
        calls.append({"url": url, "body": body})
        return {"code": 200, "data": {"valid": True}}

    monkeypatch.setattr(license_client, "_post_plain", fake_plain)
    return calls


def _break_crypto(monkeypatch, exc: Exception) -> None:
    def boom(*args, **kwargs):
        raise exc

    monkeypatch.setattr(crypto_transport, "secure_json_request", boom)


def test_crypto_failure_does_not_downgrade_over_http(plain_spy, monkeypatch):
    _break_crypto(monkeypatch, OSError("ssl handshake failed"))
    res = _post_json(HTTP_URL, {"cardCode": "x"}, 5.0, "redeem", {"prefer_crypto": True})
    assert plain_spy == [], "明文 HTTP 下不允许降级"
    assert res.get("cryptoError") is True
    assert res.get("networkError") is False


def test_crypto_network_failure_is_flagged_as_network(plain_spy, monkeypatch):
    _break_crypto(monkeypatch, OSError("connection refused"))
    res = _post_json(HTTP_URL, {}, 5.0, "status", {"prefer_crypto": True})
    assert res.get("networkError") is True
    assert plain_spy == []


def test_downgrade_allowed_over_https_or_by_setting(plain_spy, monkeypatch):
    _break_crypto(monkeypatch, OSError("ssl handshake failed"))
    assert _post_json(HTTPS_URL, {}, 5.0, "status", {"prefer_crypto": True}).get("code") == 200
    cfg = {"prefer_crypto": True, "allow_plain_fallback": True}
    assert _post_json(HTTP_URL, {}, 5.0, "status", cfg).get("code") == 200
    assert len(plain_spy) == 2


def test_prefer_crypto_keeps_plain_business_failure(plain_spy, monkeypatch):
    _break_crypto(monkeypatch, OSError("handshake"))

    def fake_plain(url, body, timeout):
        return {"code": 4001, "message": "卡密已被使用", "networkError": False}

    monkeypatch.setattr(license_client, "_post_plain", fake_plain)
    res = _post_json(HTTP_URL, {}, 5.0, "redeem", {"prefer_crypto": True, "allow_plain_fallback": True})
    assert res.get("code") == 4001  # 服务端明确应答，不能被包成网络错误
    assert res.get("networkError") is False


def test_post_plain_distinguishes_http_error_from_network_error(monkeypatch):
    def raise_url_error(req, timeout=None, **kwargs):
        raise URLError("Name or service not known")

    monkeypatch.setattr(license_client, "urlopen", raise_url_error)
    res = _post_plain(HTTP_URL, {}, 1.0)
    assert res["networkError"] is True and res["code"] == -1

    def raise_http_error(req, timeout=None, **kwargs):
        raise HTTPError(HTTP_URL, 403, "Forbidden", {}, io.BytesIO(b'{"message":"ticket expired"}'))

    monkeypatch.setattr(license_client, "urlopen", raise_http_error)
    res = _post_plain(HTTP_URL, {}, 1.0)
    assert res["networkError"] is False
    assert res["message"] == "ticket expired"
    assert res["code"] == 403

    def raise_plain(req, timeout=None, **kwargs):
        raise RuntimeError("token=abc123def456 leaked")

    monkeypatch.setattr(license_client, "urlopen", raise_plain)
    res = _post_plain(HTTP_URL, {}, 1.0)
    assert res["networkError"] is True
    assert "abc123def456" not in res["message"]


def test_unwrap_accepts_bare_and_enveloped_payloads():
    assert _unwrap({"code": 200, "data": {"valid": True}}) == (True, "", {"valid": True})
    assert _unwrap({"valid": True, "ticket": "t"})[0] is True
    ok, msg, _ = _unwrap({"code": 500})
    assert ok is False and "code=500" in msg
    assert _unwrap("not a dict") == (False, "响应无效", {})


def test_public_license_view_strips_ticket_and_card():
    view = public_license_view({"valid": True, "ticket": "t" * 30, "primaryCard": "c" * 20, "accountLimit": 3})
    assert view == {"valid": True, "accountLimit": 3}
    assert public_license_view(None) == {}


def test_ticket_still_valid_reads_expiry_without_network():
    assert ticket_still_valid({}) is False
    assert ticket_still_valid({"valid": True, "timeUnlimited": True}) is True
    assert ticket_still_valid({"valid": True, "expireAt": "2000-01-01T00:00:00+00:00"}) is False
    assert ticket_still_valid(
        {"valid": True, "expireAt": "2999-01-01T00:00:00+00:00"}
    ) is True
    assert ticket_still_valid(
        {"valid": True, "expireAt": None, "ticketExpireAt": "2999-01-01T00:00:00+00:00", "ticket": "t"}
    ) is True
