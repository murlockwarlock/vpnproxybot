"""Bridge helpers for syncing Telegram data with the web store."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import aiohttp

from bot.config import settings
from bot.models import MTProtoAccount, Subscription, User
from bot.services.subscription_service import _format_proxy_links
from bot.services.tariff_utils import format_subscription_duration
from bot.services.vhq_subscription_proxy import get_subscription_display_key

logger = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=8)


def _bridge_enabled() -> bool:
    return bool(settings.webstore_api_base_url and settings.webstore_bridge_secret)


def _headers() -> dict[str, str]:
    return {"X-Internal-Secret": settings.webstore_bridge_secret}


def serialize_profile_items(
    user: User,
    subscriptions: list[Subscription],
    mtproto_accounts: list[MTProtoAccount],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []

    for sub in subscriptions:
        location = sub.server.location if sub.server else "VPN"
        emoji = sub.server.country_emoji if sub.server else "🌍"
        title = f"{emoji} {location}"
        subtitle = format_subscription_duration(
            tariff_days=sub.tariff_days,
            tariff_months=sub.tariff_months,
        )
        items.append({
            "item_type": "vpn",
            "external_id": f"sub_{sub.id}",
            "title": title,
            "subtitle": subtitle,
            "key_value": get_subscription_display_key(sub),
            "status": sub.status.value,
            "device_slots": sub.device_slots,
            "expires_at": sub.expires_at.isoformat() if sub.expires_at else None,
        })

    for acc in mtproto_accounts:
        expires_at = None
        status = "active"
        if acc.subscription_id:
            linked_sub = next((sub for sub in subscriptions if sub.id == acc.subscription_id), None)
            if linked_sub:
                expires_at = linked_sub.expires_at.isoformat() if linked_sub.expires_at else None
                status = linked_sub.status.value
        items.append({
            "item_type": "mtproto",
            "external_id": f"mtproto_{acc.id}",
            "title": "Telegram-ускоритель",
            "subtitle": user.username or user.full_name or "Telegram",
            "key_value": _format_proxy_links(acc.secret),
            "status": status,
            "device_slots": None,
            "expires_at": expires_at,
        })

    return items


async def claim_web_link(
    code: str,
    user: User,
    subscriptions: list[Subscription],
    mtproto_accounts: list[MTProtoAccount],
) -> bool:
    if not _bridge_enabled() or not code:
        return False

    payload = {
        "code": code,
        "telegram_id": user.telegram_id,
        "telegram_username": user.username,
        "telegram_full_name": user.full_name,
        "items": serialize_profile_items(user, subscriptions, mtproto_accounts),
    }
    url = f"{settings.webstore_api_base_url.rstrip('/')}/api/store/internal/telegram-link-claim"
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as http:
            async with http.post(url, json=payload, headers=_headers()) as resp:
                return resp.status == 200
    except Exception as exc:
        logger.warning("Failed to claim web link: %s", exc)
        return False


async def claim_web_auth(
    code: str,
    user: User,
    subscriptions: list[Subscription],
    mtproto_accounts: list[MTProtoAccount],
) -> bool:
    if not _bridge_enabled() or not code:
        return False

    payload = {
        "code": code,
        "telegram_id": user.telegram_id,
        "telegram_username": user.username,
        "telegram_full_name": user.full_name,
        "items": serialize_profile_items(user, subscriptions, mtproto_accounts),
    }
    url = f"{settings.webstore_api_base_url.rstrip('/')}/api/store/internal/telegram-auth-claim"
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as http:
            async with http.post(url, json=payload, headers=_headers()) as resp:
                return resp.status == 200
    except Exception as exc:
        logger.warning("Failed to claim web auth: %s", exc)
        return False


async def sync_user_profile(
    user: User,
    subscriptions: list[Subscription],
    mtproto_accounts: list[MTProtoAccount],
) -> None:
    if not _bridge_enabled():
        return

    payload = {
        "telegram_id": user.telegram_id,
        "telegram_username": user.username,
        "telegram_full_name": user.full_name,
        "items": serialize_profile_items(user, subscriptions, mtproto_accounts),
        "synced_at": datetime.utcnow().isoformat(),
    }
    url = f"{settings.webstore_api_base_url.rstrip('/')}/api/store/internal/telegram-sync"
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as http:
            async with http.post(url, json=payload, headers=_headers()) as resp:
                if resp.status not in {200, 404}:
                    logger.warning("Unexpected webstore sync status: %s", resp.status)
    except Exception as exc:
        logger.warning("Failed to sync Telegram profile to webstore: %s", exc)


async def fetch_linked_web_profile(telegram_id: int) -> dict[str, Any] | None:
    if not _bridge_enabled():
        return None

    url = (
        f"{settings.webstore_api_base_url.rstrip('/')}/api/store/internal/web-profile"
        f"?telegram_id={telegram_id}"
    )
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as http:
            async with http.get(url, headers=_headers()) as resp:
                if resp.status == 404:
                    return None
                if resp.status != 200:
                    logger.warning("Unexpected web profile status: %s", resp.status)
                    return None
                return await resp.json()
    except Exception as exc:
        logger.warning("Failed to fetch linked web profile: %s", exc)
        return None
