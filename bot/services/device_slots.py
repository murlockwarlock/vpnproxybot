"""Helpers for included and maximum device slots per subscription."""

from __future__ import annotations

from bot.models import BotSettings


async def _get_setting(session, key: str, default: str) -> str:
    row = await session.get(BotSettings, key)
    return row.value if row and row.value else default


async def get_included_device_slots(session) -> int:
    raw = await _get_setting(session, "included_devices_per_sub", "3")
    try:
        value = int(raw)
    except ValueError:
        value = 3
    return max(1, value)


async def get_max_device_slots(session) -> int | None:
    included = await get_included_device_slots(session)
    raw = await _get_setting(session, "max_devices_per_sub", "0")
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value <= 0:
        return None
    return max(included, value)
