# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class LoginResult:
    ok: bool
    provider: str
    method: str = ""  # api | playwright | manual_needed
    token_blob: dict[str, Any] = field(default_factory=dict)
    message: str = ""
    needs_manual: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
