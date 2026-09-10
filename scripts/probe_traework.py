# -*- coding: utf-8 -*-
"""TraeWork 签到探针。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from checkin_tool.adapters import traework


def main() -> int:
    auth, err = traework.load_local_auth()
    print("LOAD", "err=", err)
    if auth:
        safe = {k: v for k, v in auth.items() if k not in {"token", "access_token"}}
        print(json.dumps(safe, ensure_ascii=False, indent=2))
    if not auth or not auth.get("token"):
        print("NO_TOKEN — 请确认 TraeWork 已登录；若仍失败需手工补 token")
        return 2
    result = traework.checkin_from_blob(auth)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
