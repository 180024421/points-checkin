# -*- coding: utf-8 -*-
"""WorkBuddy 国际版（CodeBuddy 国际版）每日签到适配器。

与 CN 版（``workbuddy``）**后端同构**：同样的 ``/v2/billing/meter`` 路径、同样的
请求形状（POST + body ``{}`` + ``Authorization: Bearer`` + ``X-User-Id``），所以 HTTP
核心直接复用 ``workbuddy``，本模块只把 base host 换成国际版实测地址
``copilot.tencent.com``（实测：未带 token 返回 401 而非 404，路径存在）。

与 CN 的关键差异：国际版本机登录态
``%LOCALAPPDATA%\\CodeBuddyExtension\\Data\\Public\\auth\\workbuddy-desktop-ai.info``
里的 accessToken 被 ``$wbEncrypted`` AES-GCM 信封本地加密（CN 是明文），
所以无法像 CN 那样自动采集明文 token。一期只支持**手工粘贴 Bearer**：
把 ``{provider, access_token, uid}`` 写进账号库即可，签到/查积分链路完全打通。
"""

from __future__ import annotations

from typing import Any

from .base import CheckinResult
from . import workbuddy

PROVIDER = "workbuddy_intl"
BASE_INTL = "https://copilot.tencent.com"

# 供后续做自动采集（破解 $wbEncrypted 信封）时定位登录态文件；一期不自动采集
AUTH_FILENAME = "workbuddy-desktop-ai.info"


def checkin_from_blob(token_blob: dict[str, Any]) -> CheckinResult:
    return workbuddy.checkin_from_blob(token_blob, base_url=BASE_INTL, provider=PROVIDER)


def query_from_blob(token_blob: dict[str, Any]) -> dict[str, Any]:
    return workbuddy.query_from_blob(token_blob, base_url=BASE_INTL)
