from datetime import datetime, timezone

from bot.utils.device_info import (
    adapt_device_details,
    describe_user_agent,
    format_activity,
    parse_activity_datetime,
)


def test_adapt_device_details_uses_current_api_fields():
    info = adapt_device_details(
        {
            "id": 42,
            "name": "Телефон",
            "device_model": "Pixel 9",
            "device_os": "Android",
            "os_version": "15",
            "last_seen": "2026-08-01T12:30:00+00:00",
            "ip_address": "192.0.2.1",
        },
        1,
    )

    assert info == {
        "id": "42",
        "name": "Телефон",
        "model": "Pixel 9",
        "os": "Android 15",
        "last_activity": "01.08.2026 15:30 МСК",
        "ip": "192.0.2.1",
    }


def test_adapt_device_details_has_safe_fallbacks():
    info = adapt_device_details({}, 3)

    assert info["name"] == "Устройство 3"
    assert info["os"] == "не определена"
    assert info["last_activity"] == "не зафиксирована"
    assert info["ip"] == "не определён"


def test_describe_user_agent_detects_common_os_without_returning_raw_ua():
    assert describe_user_agent("Mozilla/5.0 (Linux; Android 15; Pixel 9)") == "Android 15"
    assert describe_user_agent("Mozilla/5.0 (iPhone; CPU iPhone OS 18_4 like Mac OS X)") == "iOS 18.4"
    assert describe_user_agent("Mozilla/5.0 (Windows NT 10.0; Win64; x64)") == "Windows"
    assert describe_user_agent("unknown-client/1.2 secret") == "не определена"


def test_parse_activity_datetime_accepts_iso_and_unix_milliseconds():
    expected = datetime(2026, 8, 1, 12, 30, tzinfo=timezone.utc)

    assert parse_activity_datetime("2026-08-01T12:30:00Z") == expected
    assert parse_activity_datetime(1785587400000) == expected
    assert format_activity(None) == "не зафиксирована"
