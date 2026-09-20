# -*- coding: utf-8 -*-
"""redact 的行为锁定：凭证不允许出现在落盘 / 上屏的字符串里。"""

from checkin_tool.redact import brief, is_sensitive_key, mask_text, redact


def test_redact_masks_sensitive_keys_recursively():
    payload = {
        "data": {"accessToken": "a" * 40, "list": [{"refresh_token": "b" * 40, "keep": 1}]},
        "code": 200,
    }
    out = redact(payload)
    assert out["data"]["accessToken"] == "***"
    assert out["data"]["list"][0]["refresh_token"] == "***"
    assert out["data"]["list"][0]["keep"] == 1
    # code 是业务码，不能被当成卡密屏蔽掉
    assert out["code"] == 200


def test_is_sensitive_key_matches_substrings_but_not_plain_code():
    assert is_sensitive_key("Access-Token")
    assert is_sensitive_key("private_key_pem")
    assert not is_sensitive_key("code")
    assert not is_sensitive_key("statusCode")


def test_mask_text_handles_kv_jwt_and_long_blobs():
    text = 'failed token=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abcdef body="' + ("x" * 60) + '"'
    masked = mask_text(text, 300)
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in masked
    assert "x" * 60 not in masked
    assert "token=" in masked  # 键名保留，便于排查
    assert "***" in masked


def test_brief_accepts_any_payload_and_caps_length():
    class Weird:
        pass

    assert len(brief({"a": Weird(), "token": "s" * 50}, 80)) <= 80
    assert brief(None) == "null"
    assert "ok" in brief({"message": "ok"})
    assert "***" in brief({"data": {"refreshToken": "r" * 40}})
