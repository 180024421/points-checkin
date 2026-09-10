from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class CheckinResult:
    ok: bool
    provider: str
    already: bool = False
    credits: int | None = None
    streak: int | None = None
    message: str = ""
    raw_summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def mask_secret(_value: str | None) -> str:
    return "已加载（内容已隐藏）" if _value else "(无)"
