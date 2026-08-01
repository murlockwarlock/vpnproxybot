"""Normalize device activity returned by the supported VPN backends."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any


def _as_text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def parse_activity_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        try:
            parsed = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        if raw.isdigit():
            return parse_activity_datetime(int(raw))
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_activity(value: Any) -> str:
    parsed = parse_activity_datetime(value)
    if parsed is None:
        return "не зафиксирована"
    msk = timezone(timedelta(hours=3))
    return f"{parsed.astimezone(msk).strftime('%d.%m.%Y %H:%M')} МСК"


def describe_user_agent(user_agent: Any) -> str:
    """Return a short OS label without exposing a raw user-agent to the client."""
    raw = _as_text(user_agent)
    if not raw:
        return "не определена"

    normalized = raw.lower()
    if "android" in normalized:
        version = re.search(r"android[ /]([0-9.]+)", raw, re.IGNORECASE)
        prefix = "Android TV" if "android tv" in normalized else "Android"
        return f"{prefix} {version.group(1)}" if version else prefix
    if any(marker in normalized for marker in ("iphone", "ipad", "ios")):
        version = re.search(r"(?:cpu (?:iphone )?os|ios)[ /]([0-9_\.]+)", raw, re.IGNORECASE)
        return f"iOS {version.group(1).replace('_', '.')}" if version else "iOS"
    if "windows" in normalized:
        return "Windows"
    if any(marker in normalized for marker in ("macintosh", "mac os", "macos")):
        return "macOS"
    if "linux" in normalized:
        return "Linux"
    return "не определена"


def adapt_device_details(device: dict[str, Any], index: int) -> dict[str, str]:
    device_id = _as_text(device.get("id") or device.get("device_id"))
    model = _as_text(device.get("device_model") or device.get("model"))
    name = _as_text(device.get("name") or device.get("client_name") or model)
    if not name:
        name = f"Устройство {index}"

    device_os = _as_text(device.get("device_os") or device.get("os"))
    os_version = _as_text(device.get("os_version"))
    os_label = " ".join(part for part in (device_os, os_version) if part) or "не определена"

    return {
        "id": device_id,
        "name": name,
        "model": model,
        "os": os_label,
        "last_activity": format_activity(
            device.get("last_seen") or device.get("last_login") or device.get("online_at")
        ),
        "ip": _as_text(
            device.get("ip_address") or device.get("last_ip") or device.get("ip")
        ) or "не определён",
    }
