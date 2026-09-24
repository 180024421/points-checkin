# -*- coding: utf-8 -*-
"""WorkBuddy 国际版（CodeBuddy 国际版）签到发现探针。

为什么要单独一个探针：国际版和 CN 版**后端同构**，但本机登录态的落盘方式不同，
在真正写适配器之前必须先把三件事抓准，而不是照抄 CN 猜一遍：

  1. 登录态文件在哪、字段什么形状；
  2. 真正的签到接口 host 与路径（本机日志实测是 copilot.tencent.com，
     而登录态里的 ``auth.domain`` 只是账号域，别被 www.workbuddy.ai 带偏）；
  3. 拿到一个**可用**的 Bearer：国际版把 accessToken 用 ``$wbEncrypted``
     信封在本地加密了（CN 是明文），所以自动采集需要先解决解密；本探针同时
     支持"手工给 token /从客户端日志里抓 token"，先验证端到端契约。

用法（Windows，需本机装了 WorkBuddy AI 并登录过）：
    python scripts/probe_workbuddy_intl.py                 # 只看登录态 + 路由是否存在（无副作用）
    python scripts/probe_workbuddy_intl.py --from-log       # 尝试从客户端 renderer.log 抓一个明文 Bearer 并查状态
    python scripts/probe_workbuddy_intl.py --token <BEARER> # 用手工抓到的 Bearer 查状态
    python scripts/probe_workbuddy_intl.py --token <BEARER> --claim   # 显式 --claim 才真的领一次积分

默认不带 --claim 时只做只读查询，不会替账号领积分。
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# 实测：国际版客户端把签到请求打到 copilot.tencent.com，路径与 CN 完全一致
BASE_URLS = [
    "https://copilot.tencent.com",
    "https://www.workbuddy.ai",  # 备选：登录态里的 auth.domain，万一路由也在这台网关上
]
STATUS_PATH = "/v2/billing/meter/checkin-activity-status"
CHECKIN_PATH = "/v2/billing/meter/daily-checkin"

LOG_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "WorkBuddy" / "logs"
AUTH_GLOBS = ["workbuddy-desktop-ai*.info"]


def _short(s: Any, n: int = 10, tail: int = 4) -> str:
    s = "" if s is None else str(s)
    return s[:n] + "…" + s[-tail:] if len(s) > n + tail + 1 else s


def find_auth_file() -> Path | None:
    root = Path(os.environ.get("LOCALAPPDATA", "")) / "CodeBuddyExtension" / "Data" / "Public" / "auth"
    if not root.exists():
        return None
    files: list[Path] = []
    for g in AUTH_GLOBS:
        files.extend(root.glob(g))
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def describe_auth(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    auth = data.get("auth") or {}
    account = data.get("account") or {}

    def is_envelope(v: Any) -> bool:
        return isinstance(v, dict) and v.get("$wbEncrypted") == 1

    at = auth.get("accessToken")
    info: dict[str, Any] = {
        "path": str(path),
        "uid": account.get("uid"),
        "uin": account.get("uin"),
        "tokenType": auth.get("tokenType"),
        "domain": auth.get("domain"),
        "scope": _short(auth.get("scope"), 24, 6),
        "expiresAt": auth.get("expiresAt"),
        "accessToken_is_envelope": is_envelope(at),
        "refreshToken_is_envelope": is_envelope(auth.get("refreshToken")),
        "plaintext_token": None,
    }
    if isinstance(at, str):
        info["plaintext_token"] = at
    return info


def token_from_log() -> tuple[str | None, str | None]:
    """从客户端日志里刮一个**与签到接口同段出现**的明文 Bearer。

    该客户端会把出网请求（含 Authorization 头）记进 main.log / renderer.log。
    但日志里有多种服务的 Bearer，只有紧邻 ``billing/meter`` 那条才是签到用的，
    所以只在同一小段里同时出现"签到 URL"和"Bearer"时才取，避免抓错 token。
    这也是临时便利，不是长期方案：日志会滚动、token 会轮换，正式适配器不应依赖它。
    """
    if not LOG_DIR.exists():
        return None, f"找不到日志目录：{LOG_DIR}"
    bearer = re.compile(r"Bearer\s+([A-Za-z0-9._\-]{20,})")
    marker = re.compile(r"billing/meter|checkin", re.I)
    best: str | None = None
    best_mtime = 0.0
    for fn in sorted(LOG_DIR.glob("*.log")):
        try:
            text = fn.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for m in bearer.finditer(text):
            window = text[max(0, m.start() - 240): m.end() + 240]
            if not marker.search(window):
                continue  # 这段不是签到请求，别抓错
            if fn.stat().st_mtime >= best_mtime:
                best_mtime = fn.stat().st_mtime
                best = m.group(1)
    if not best:
        return None, "日志里没抓到与签到接口同段的 Bearer（版本可能不再记录明文，请用 --token 手工提供）"
    return best.strip(), None


def post(url: str, token: str | None, uid: str | None, timeout: float = 20.0) -> tuple[int, dict[str, Any]]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "CheckinTool/1.0",
    }
    if token:
        headers["Authorization"] = token if token.lower().startswith("bearer ") else f"Bearer {token}"
    if uid:
        headers["X-User-Id"] = str(uid)
    req = Request(url, method="POST", data=b"{}", headers=headers)
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        try:
            return exc.code, json.loads(body)
        except Exception:
            return exc.code, {"_html": body[:160]}
    except URLError as exc:
        return -1, {"_err": f"网络失败: {exc}"}
    except Exception as exc:
        return -1, {"_err": str(exc)}


def probe_routes() -> None:
    print("== 路由存在性探测（不带 token，无副作用）==")
    for base in BASE_URLS:
        code, resp = post(base + STATUS_PATH, None, None)
        hint = "401=路由存在只是要鉴权 / 404=路径不对 / -1=域名或网络不通"
        body = resp.get("_html") or resp.get("_err") or resp
        print(f"  {base}{STATUS_PATH} -> HTTP {code}  ({hint})")
        print(f"      body: {_short(json.dumps(body, ensure_ascii=False), 60, 8)}")


def main() -> int:
    ap = argparse.ArgumentParser(description="WorkBuddy 国际版签到发现探针")
    ap.add_argument("--token", help="手工抓到的 Bearer（可带或不带前缀 'Bearer '）")
    ap.add_argument("--uid", help="配合 --token 使用的 X-User-Id；缺省从登录态里取")
    ap.add_argument("--from-log", action="store_true", help="尝试从客户端 renderer.log 刮一个明文 Bearer")
    ap.add_argument("--claim", action="store_true", help="真的领一次积分（默认只读查询，不领）")
    args = ap.parse_args()

    auth_path = find_auth_file()
    info: dict[str, Any] = {}
    if auth_path:
        try:
            info = describe_auth(auth_path)
            print("== 登录态 ==")
            print(json.dumps(info, ensure_ascii=False, indent=2))
        except Exception as exc:
            print("AUTH_PARSE_FAIL", exc)
    else:
        print("没找到 workbuddy-desktop-ai*.info（确认已安装并登录 WorkBuddy AI）")

    token = args.token
    if not token and args.from_log:
        token, err = token_from_log()
        print("== 从日志抓 token ==", ("OK " + _short(token)) if token else ("FAIL " + str(err)))

    uid = args.uid or info.get("uid")

    probe_routes()

    if not token:
        print("\n没有可用 Bearer，仅完成路由探测。")
        if info.get("accessToken_is_envelope"):
            print("登录态 accessToken 是 $wbEncrypted 信封（本机加密），无法直接当 Bearer 用。")
            print("下一步二选一：")
            print("  A) 手工/日志取 token：--from-log 或 --token <从客户端 DevTools 网络面板复制的 Bearer>")
            print("  B) 在适配器里实现 $wbEncrypted 信封解密（需从 app.asar 还原密钥派生，较易碎）")
        return 0

    print("\n== 用 token 查签到状态（只读）==")
    for base in BASE_URLS:
        code, resp = post(base + STATUS_PATH, token, uid)
        print(f"  {base}{STATUS_PATH} -> HTTP {code}")
        print("      " + json.dumps(resp, ensure_ascii=False)[:400])
        if code == 200 and isinstance(resp.get("data"), dict):
            print(f"      命中可用 host：{base}")
            if args.claim:
                print("\n== --claim：真的领一次 daily-checkin ==")
                c2, r2 = post(base + CHECKIN_PATH, token, uid)
                print(f"  {base}{CHECKIN_PATH} -> HTTP {c2}")
                print("      " + json.dumps(r2, ensure_ascii=False)[:400])
            else:
                print("  （加 --claim 才会真的领积分）")
            return 0 if code == 200 else 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
