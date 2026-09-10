# -*- coding: utf-8 -*-
"""WorkBuddy 签到探针。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from checkin_tool.adapters import workbuddy


def main() -> int:
    auth, err = workbuddy.load_local_auth()
    if err or not auth:
        print("LOAD_FAIL", err)
        return 2
    print("LOAD_OK", auth.get("nickname") or auth.get("uid"), auth.get("token_hint"))
    result = workbuddy.checkin_from_blob(auth)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
