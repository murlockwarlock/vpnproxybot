"""Bridge helpers for syncing Telegram data with the web store."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import aiohttp

from bot.config import settings
from sqlalchemy import select

from bot.database import async_session
from bot.models import AdaptSubscription, MTProtoAccount, Platform, SubStatus, Subscription, Tariff, User
from bot.services.adapt_api import AdaptAPI
from bot.services.adapt_routing import build_adapt_client_name
from bot.services.adapt_subscription_proxy import build_adapt_mirror_url
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
        client_name = str(sub.client_name or "")
        provider = "adapt" if client_name.startswith("adapt_") else (
            "vhq" if client_name.startswith("vhq_") else "marzban"
        )
        tariff = getattr(sub, "tariff", None)
        items.append({
            "item_type": "vpn",
            "external_id": f"sub_{sub.id}",
            "title": title,
            "subtitle": subtitle,
            "key_value": get_subscription_display_key(sub),
            "provider": provider,
            "adapt_plan_uuid": str(getattr(tariff, "adapt_plan_uuid", "") or ""),
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


def _web_adapt_uuid(order: dict[str, Any]) -> str | None:
    import re
    direct = str(order.get("adapt_uuid") or "").strip()
    if re.fullmatch(r"[0-9a-f-]{36}", direct, re.IGNORECASE):
        return direct
    raw = str(order.get("raw_subscription_url") or order.get("subscription_url") or "")
    match = re.search(r"/(?:adapt-sub|sub)/([0-9a-f-]{36})", raw, re.IGNORECASE)
    return match.group(1) if match else None


def _parse_web_datetime(value: Any):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo:
            from datetime import timezone
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    except ValueError:
        return None


async def sync_linked_web_subscriptions(telegram_id: int, profile: dict[str, Any] | None) -> bool:
    """Import/update web Adapt UUIDs in the bot DB so bot/admin can manage them."""
    if not profile:
        return False
    candidates: dict[str, dict[str, Any]] = {}
    for order in profile.get("orders") or []:
        adapt_uuid = _web_adapt_uuid(order)
        if order.get("status") == "delivered" and order.get("provider") == "adapt" and adapt_uuid:
            previous = candidates.get(adapt_uuid)
            if not previous or str(order.get("created_at") or "") > str(previous.get("created_at") or ""):
                candidates[adapt_uuid] = order
    if not candidates:
        return False

    changed = False
    async with async_session() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        if not user:
            return False
        from bot.services.subscription_service import get_primary_active_server
        server = await get_primary_active_server(session)
        if not server:
            return False

        api = AdaptAPI()
        plans_cache: list[dict[str, Any]] | None = None
        for adapt_uuid, order in candidates.items():
            record = await session.scalar(
                select(AdaptSubscription).where(AdaptSubscription.adapt_uuid == adapt_uuid)
            )
            subscription = await session.get(Subscription, record.subscription_id) if record else None
            if subscription and subscription.user_id != user.id:
                logger.error("Refusing web Adapt UUID reassignment: uuid=%s old_user=%s new_user=%s", adapt_uuid, subscription.user_id, user.id)
                continue

            if not api.enabled:
                logger.warning("Cannot import web Adapt subscription %s: API is disabled", adapt_uuid)
                continue
            try:
                status = await api.get_status(adapt_uuid)
            except Exception as exc:
                logger.warning("Failed to refresh imported web Adapt subscription %s: %s", adapt_uuid, exc)
                continue
            plan_uuid = str(status.get("plan_uuid") or order.get("adapt_plan_uuid") or "").strip()
            tariff = await session.scalar(
                select(Tariff).where(Tariff.adapt_plan_uuid == plan_uuid).order_by(Tariff.id.desc()).limit(1)
            ) if plan_uuid else None
            expires_at = _parse_web_datetime(status.get("end_date") or order.get("access_expires_at"))
            if not expires_at:
                continue
            actual_devices = status.get("devices")
            if actual_devices is None and plan_uuid:
                try:
                    if plans_cache is None:
                        plans_cache = await api.list_plans()
                    plan = next(
                        (
                            item for item in plans_cache
                            if str(item.get("uuid") or item.get("plan_uuid") or "").strip() == plan_uuid
                        ),
                        None,
                    )
                    actual_devices = plan.get("devices") if plan else None
                except Exception as exc:
                    logger.warning("Failed to resolve Adapt device limit for %s: %s", adapt_uuid, exc)
            if actual_devices is None:
                logger.warning("Cannot import web Adapt subscription %s: provider device limit is missing", adapt_uuid)
                continue
            devices = int(actual_devices)
            public_url = str(order.get("subscription_url") or "").strip() or build_adapt_mirror_url(adapt_uuid)

            if not subscription:
                subscription = Subscription(
                    user_id=user.id,
                    server_id=server.id,
                    tariff_months=int(order.get("days") or 0) // 30,
                    tariff_days=int(order.get("days") or 0),
                    billing_mode="tariff",
                    status=SubStatus.ACTIVE,
                    device_slots=devices,
                    vpn_key=public_url,
                    client_name=build_adapt_client_name(adapt_uuid),
                    platform=user.platform or Platform.ANDROID,
                    expires_at=expires_at,
                    tariff_id=tariff.id if tariff else None,
                )
                session.add(subscription)
                await session.flush()
                record = AdaptSubscription(
                    subscription_id=subscription.id,
                    adapt_uuid=adapt_uuid,
                    adapt_plan_uuid=plan_uuid,
                    end_date=expires_at,
                    traffic_limit_bytes=status.get("traffic_limit_bytes"),
                )
                session.add(record)
            else:
                subscription.server_id = server.id
                subscription.status = SubStatus.ACTIVE if expires_at > datetime.utcnow() else SubStatus.EXPIRED
                subscription.device_slots = devices
                subscription.vpn_key = public_url
                subscription.expires_at = expires_at
                if tariff:
                    subscription.tariff_id = tariff.id
                    subscription.tariff_days = tariff.days
                    subscription.tariff_months = tariff.days // 30
                record.adapt_plan_uuid = plan_uuid or record.adapt_plan_uuid
                record.end_date = expires_at
                record.traffic_limit_bytes = status.get("traffic_limit_bytes")
            changed = True
        if changed:
            await session.commit()
    return changed
