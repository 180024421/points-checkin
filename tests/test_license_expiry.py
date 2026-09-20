# -*- coding: utf-8 -*-
"""到期时间的三种线格式都必须能解析——线上那代 DTO 发的是毫秒数字。

服务端 `AppLicenseServiceImpl.status()` 返回 Map，`expireAt` 是裸 `java.util.Date`，
Jackson 序列化成 epoch-millis；src 那代 DTO 直出 ISO 字符串，另有些字段是
`"2026-10-14 23:25:44"` 这种空格分隔。旧解析器只认 ISO 字符串，于是**带时长的授权**
在客户端一律读成「没有到期时间」，时长轴等于不存在。
"""

from datetime import datetime, timedelta, timezone

from checkin_tool import license_client


def _ts(dt: datetime) -> float:
    return dt.timestamp()


def test_parses_iso_string_with_offset():
    dt = license_client._parse_iso("2026-10-14T23:25:44+08:00")
    assert dt is not None
    assert dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S") == "2026-10-14 15:25:44"


def test_parses_iso_string_without_tz_as_utc():
    dt = license_client._parse_iso("2026-10-14T23:25:44")
    assert dt is not None and dt.tzinfo is not None
    assert dt.utcoffset() == timedelta(0)


def test_parses_space_separated_form_server_actually_sends():
    dt = license_client._parse_iso("2026-10-14 23:25:44")
    assert dt is not None
    assert (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second) == (2026, 10, 14, 23, 25, 44)


def test_parses_epoch_millis_from_live_map_dto():
    """线上 Map 版 DTO：expireAt = 毫秒数字，旧实现 here 返回 None。"""
    target = datetime.now(timezone.utc) + timedelta(days=30)
    millis = int(_ts(target) * 1000)
    for raw in (millis, float(millis), str(millis)):
        dt = license_client._parse_iso(raw)
        assert dt is not None, f"{raw!r} 解析成 None，时长轴又丢了"
        assert abs((dt - target).total_seconds()) < 2, f"{raw!r} 解析结果偏了"


def test_parses_epoch_seconds_too():
    target = datetime.now(timezone.utc) + timedelta(days=7)
    dt = license_client._parse_iso(int(_ts(target)))
    assert dt is not None
    assert abs((dt - target).total_seconds()) < 2


def test_unparseable_input_degrades_to_none():
    for raw in (None, "", "  ", "not-a-date", False, True, "2026-13-45T99:99:99"):
        assert license_client._parse_iso(raw) is None, f"{raw!r} 不该被当成时间"
    # bool 不能当时间戳：True 会变成 1970-01-01，看起来"有效"但完全是错的


def test_ticket_still_valid_accepts_millis_expire_at():
    future = int((_ts(datetime.now(timezone.utc)) + 3600) * 1000)
    past = int((_ts(datetime.now(timezone.utc)) - 3600) * 1000)
    assert license_client.ticket_still_valid(
        {"valid": True, "timeUnlimited": False, "expireAt": future}
    ), "毫秒到期时间在未来 → 票据仍有效"
    assert not license_client.ticket_still_valid(
        {"valid": True, "timeUnlimited": False, "expireAt": past}
    ), "已过期且无有效 ticketExpireAt → 不能放行"
