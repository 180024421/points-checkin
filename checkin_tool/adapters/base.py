from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class CheckinResult:
    ok: bool
    provider: str
    already: bool = False
    # 「没得可领」的中性态（活动未开始 / 签到入口未开放）：既不是成功也不是失败，
    # 记成成功会把这格标绿不再补跑，记成失败会让统计卡整天挂着异常。
    skipped: bool = False
    credits: int | None = None
    streak: int | None = None
    message: str = ""
    raw_summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def mask_secret(_value: str | None) -> str:
    return "已加载（内容已隐藏）" if _value else "(无)"
