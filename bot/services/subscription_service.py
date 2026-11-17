"""Helpers for creating and renewing paid subscriptions without rotating links."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
from urllib.parse import urlparse
from sqlalchemy import and_, case, delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from bot.models import (
    AdaptSubscription,
    ManualSubscriptionOperation,
    MTProtoAccount,
    PaymentMethod,
    Platform,
    ProxyAccount,
    Server,
    SubStatus,
    Subscription,
    Tariff,
    TariffType,
    User,
    VhqSubscriptionFulfillment,
)
from bot.services import vpn_manager
from bot.services.adapt_api import ADAPT_MIN_CUSTOM_RENEW_DAYS, AdaptAPI, AdaptAPIError
from bot.services.adapt_routing import (
    ADAPT_CLIENT_PREFIX,
    build_adapt_client_name,
    is_adapt_subscription,
    is_adapt_tariff,
)
from bot.services.adapt_subscription_proxy import build_adapt_mirror_url
from bot.services.client_names import build_client_name
from bot.services.device_slots import get_included_device_slots
from bot.services.payment_logger import plog
from bot.services.provisioning_issues import (
    build_adapt_access_error,
    build_internal_access_error,
    build_vhq_access_error,
)
from bot.services.proxy_manager import MarzbanAPI, get_subscription_link
from bot.services.subscription_semantics import (
    demo_access_clause,
    is_adapt_trial_subscription,
    paid_access_clause,
)
from bot.services.vhq_partner_api import (
    VHQPartnerAPI,
    VHQPartnerAPIError,
    is_vhq_renewal_active_until_confirmed,
)
from bot.services.vhq_routing import VHQ_CLIENT_PREFIX, get_vhq_spec_for_tariff
from bot.services.vhq_subscription_proxy import build_vhq_subscription_ref_url

logger = logging.getLogger(__name__)

_VHQ_FULFILLMENT_LEASE_DURATION = timedelta(minutes=2)


def _disable_balance_autodebit_after_tariff_purchase(user: User) -> None:
    """A fixed tariff fully pays the period; daily balance debits must stop."""
    user.balance_mode_enabled = False
    user.balance_autodebit_enabled = False
    user.balance_grace_until = None
    user.next_daily_charge_at = None
    user.balance_warning_for_charge_at = None


async def get_primary_active_server(session) -> Server | None:
    result = await session.execute(
        select(Server).where(Server.is_active == True).order_by(Server.id).limit(1)  # noqa: E712
    )
    server = result.scalar_one_or_none()
    if server:
        return server
    # Fallback: if all servers marked inactive by health check, use any server
    # Key delivery must never be blocked — health check is for monitoring only
    logger.warning("No active servers found, falling back to any available server")
    result = await session.execute(
        select(Server).where(Server.api_url.isnot(None)).order_by(Server.id).limit(1)
    )
    return result.scalar_one_or_none()


async def _latest_paid_subscription(session, user_id: int) -> Subscription | None:
    result = await session.execute(
        select(Subscription)
        .where(Subscription.user_id == user_id)
        .where(paid_access_clause(Subscription))
        .order_by(Subscription.expires_at.desc(), Subscription.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _latest_balance_subscription(session, user_id: int) -> Subscription | None:
    result = await session.execute(
        select(Subscription)
        .where(Subscription.user_id == user_id)
        .where(paid_access_clause(Subscription))
        .where(Subscription.billing_mode == "balance")
        .order_by(Subscription.expires_at.desc(), Subscription.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _latest_demo_subscription(session, user_id: int) -> Subscription | None:
    result = await session.execute(
        select(Subscription)
        .where(Subscription.user_id == user_id)
        .where(demo_access_clause(Subscription))
        .order_by(Subscription.expires_at.desc(), Subscription.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _latest_adapt_subscription_for_plan(
    session,
    *,
    user_id: int,
    adapt_plan_uuid: str,
) -> tuple[Subscription | None, AdaptSubscription | None]:
    result = await session.execute(
        select(Subscription, AdaptSubscription)
        .join(AdaptSubscription, AdaptSubscription.subscription_id == Subscription.id)
        .where(Subscription.user_id == user_id)
        .where(Subscription.client_name.like(f"{ADAPT_CLIENT_PREFIX}%"))
        .where(AdaptSubscription.adapt_plan_uuid == adapt_plan_uuid)
        .order_by(Subscription.expires_at.desc(), Subscription.id.desc())
        .limit(1)
    )
    row = result.first()
    if not row:
        return None, None
    return row[0], row[1]


async def _latest_adapt_subscription(
    session,
    *,
    user_id: int,
) -> tuple[Subscription | None, AdaptSubscription | None]:
    result = await session.execute(
        select(Subscription, AdaptSubscription)
        .join(AdaptSubscription, AdaptSubscription.subscription_id == Subscription.id)
        .where(Subscription.user_id == user_id)
        .where(Subscription.client_name.like(f"{ADAPT_CLIENT_PREFIX}%"))
        .order_by(Subscription.expires_at.desc(), Subscription.id.desc())
        .limit(1)
    )
    if hasattr(result, "first"):
        raw_first = result.first()
        import inspect
        if inspect.isawaitable(raw_first):
            row = await raw_first
        else:
            row = raw_first
    else:
        row = None
    if not row:
        return None, None
    try:
        return row[0], row[1] if len(row) > 1 else None
    except (TypeError, IndexError):
        if isinstance(row, Subscription):
            return row, getattr(row, "adapt_sub", None) or getattr(row, "adapt_subscription", None)
        return None, None


async def _user_adapt_subscriptions(
    session,
    *,
    user_id: int,
) -> list[tuple[Subscription, AdaptSubscription | None]]:
    result = await session.execute(
        select(Subscription, AdaptSubscription)
        .outerjoin(AdaptSubscription, AdaptSubscription.subscription_id == Subscription.id)
        .options(selectinload(Subscription.tariff))
        .where(Subscription.user_id == user_id)
        .where(Subscription.client_name.like(f"{ADAPT_CLIENT_PREFIX}%"))
        .order_by(Subscription.expires_at.desc(), Subscription.id.desc())
    )
    if hasattr(result, "all"):
        raw_all = result.all()
        import inspect
        if inspect.isawaitable(raw_all):
            rows = await raw_all
        else:
            rows = raw_all
    else:
        rows = []
    out: list[tuple[Subscription, AdaptSubscription | None]] = []
    if rows:
        for row in rows:
            try:
                sub = row[0]
                adapt_sub = row[1] if len(row) > 1 else None
                out.append((sub, adapt_sub))
            except (TypeError, IndexError, KeyError):
                if isinstance(row, Subscription):
                    out.append((row, getattr(row, "adapt_sub", None) or getattr(row, "adapt_subscription", None)))
    return out


async def _adapt_subscription_by_id(
    session, *, user_id: int, subscription_id: int
) -> tuple[Subscription | None, AdaptSubscription | None]:
    result = await session.execute(
        select(Subscription, AdaptSubscription)
        .join(AdaptSubscription, AdaptSubscription.subscription_id == Subscription.id)
        .where(Subscription.id == subscription_id)
        .where(Subscription.user_id == user_id)
        .limit(1)
    )
    row = result.first()
    return (row[0], row[1]) if row else (None, None)


def _extract_devices_from_tariff(tariff: Tariff) -> int | None:
    import re
    if not tariff or not tariff.label:
        return None
    match = re.search(r"(\d+)\s*📱", tariff.label)
    if match:
        return int(match.group(1))
    return None


def _naive_utc(value) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


async def _ensure_marzban_user(server: Server, client_name: str, expires_at: datetime) -> str | None:
    expire_ts = int(expires_at.timestamp())
    try:
        async with MarzbanAPI(server) as api:
            user_data = await api.get_user(client_name)
            if user_data:
                updated = await api.update_user(
                    client_name,
                    expire=expire_ts,
                    status="active",
                )
                if not updated:
                    return None
            else:
                created = await api.create_user(
                    username=client_name,
                    expire=expire_ts,
                    status="active",
                )
                if not created:
                    return None

        return await get_subscription_link(server, client_name)
    except Exception as exc:
        logger.error("Failed to sync Marzban user %s on %s: %s", client_name, server.name, exc)
        return None


async def _upsert_primary_proxy_account(
    session,
    *,
    user_id: int,
    server_id: int,
    subscription_id: int,
    client_name: str,
    sub_url: str,
) -> None:
    result = await session.execute(
        select(ProxyAccount)
        .where(ProxyAccount.user_id == user_id)
        .where(ProxyAccount.marzban_username == client_name)
        .order_by(ProxyAccount.id.desc())
        .limit(1)
    )
    proxy = result.scalar_one_or_none()
    if proxy:
        proxy.server_id = server_id
        proxy.subscription_id = subscription_id
        proxy.sub_url = sub_url
        return

    session.add(
        ProxyAccount(
            user_id=user_id,
            server_id=server_id,
            subscription_id=subscription_id,
            marzban_username=client_name,
            sub_url=sub_url,
            device_limit=1,
        )
    )


async def _refresh_extra_device_links(
    session,
    *,
    user_id: int,
    subscription_id: int,
    primary_client_name: str,
    expires_at: datetime,
) -> None:
    result = await session.execute(
        select(ProxyAccount)
        .where(ProxyAccount.user_id == user_id)
        .where(ProxyAccount.subscription_id == subscription_id)
        .where(ProxyAccount.marzban_username != primary_client_name)
        .order_by(ProxyAccount.id)
    )
    proxies = result.scalars().all()

    for proxy in proxies:
        server = await session.get(Server, proxy.server_id)
        if not server:
            continue

        link = await _ensure_marzban_user(server, proxy.marzban_username, expires_at)
        if link:
            proxy.sub_url = link


async def create_or_extend_paid_subscription(
    session,
    *,
    user: User,
    tariff: Tariff,
    platform: Platform,
    bonus_days: int = 0,
    force_new: bool = False,
    target_subscription_id: int | None = None,
) -> tuple[Subscription | None, str | None]:
    """Reuse the user's primary paid subscription when possible to keep the same link."""
    now = datetime.utcnow()
    total_days = tariff.days + bonus_days
    if total_days <= 0:
        total_days = tariff.days

    logger.info(
        "Starting VPN subscription sync: user_id=%s telegram_id=%s tariff_id=%s tariff_label=%s platform=%s bonus_days=%s",
        user.id,
        user.telegram_id,
        getattr(tariff, "id", None),
        tariff.label,
        platform.value,
        bonus_days,
    )

    existing = None
    if not force_new and target_subscription_id:
        candidate = await session.get(Subscription, target_subscription_id)
        if candidate and candidate.user_id == user.id:
            existing = candidate
    elif not force_new:
        existing = await _latest_paid_subscription(session, user.id)
        if not existing:
            existing = await _latest_demo_subscription(session, user.id)

    if existing:
        server = await session.get(Server, existing.server_id)
        if not server:
            server = await get_primary_active_server(session)
        if not server:
            logger.error(
                "VPN subscription sync failed: no active server for existing subscription user_id=%s subscription_id=%s",
                user.id,
                existing.id,
            )
            return None, None

        was_inactive = existing.status != SubStatus.ACTIVE or existing.expires_at <= now
        base_expires = existing.expires_at if existing.expires_at > now else now
        expires_at = base_expires + timedelta(days=total_days)

        logger.info(
            "Reusing existing VPN subscription: user_id=%s subscription_id=%s server_id=%s was_inactive=%s new_expires_at=%s",
            user.id,
            existing.id,
            server.id,
            was_inactive,
            expires_at.isoformat(),
        )

        vpn_key = await _ensure_marzban_user(server, existing.client_name, expires_at)
        if not vpn_key:
            logger.error(
                "VPN subscription sync failed: Marzban user update returned no key user_id=%s subscription_id=%s server_id=%s",
                user.id,
                existing.id,
                server.id,
            )
            return None, None

        existing.server_id = server.id
        existing.tariff_months = tariff.days // 30
        existing.tariff_days = tariff.days
        existing.billing_mode = "tariff"
        existing.status = SubStatus.ACTIVE
        existing.platform = platform
        existing.expires_at = expires_at
        existing.vpn_key = vpn_key
        if tariff.id:
            existing.tariff_id = tariff.id
        _disable_balance_autodebit_after_tariff_purchase(user)

        await _upsert_primary_proxy_account(
            session,
            user_id=user.id,
            server_id=server.id,
            subscription_id=existing.id,
            client_name=existing.client_name,
            sub_url=vpn_key,
        )
        await _refresh_extra_device_links(
            session,
            user_id=user.id,
            subscription_id=existing.id,
            primary_client_name=existing.client_name,
            expires_at=expires_at,
        )

        if was_inactive:
            server.current_clients += 1

        logger.info(
            "VPN subscription sync completed: user_id=%s subscription_id=%s server_id=%s reused=true expires_at=%s",
            user.id,
            existing.id,
            server.id,
            expires_at.isoformat(),
        )
        return existing, vpn_key

    server = await get_primary_active_server(session)
    if not server:
        logger.error(
            "VPN subscription creation failed: no active server user_id=%s tariff_id=%s",
            user.id,
            getattr(tariff, "id", None),
        )
        return None, None

    expires_at = now + timedelta(days=total_days)
    client_name = build_client_name(user.telegram_id, slot=1)
    vpn_key = await vpn_manager.generate_key(
        server=server,
        client_name=client_name,
        expire=int(expires_at.timestamp()),
    )
    if not vpn_key or vpn_key.startswith("Error"):
        logger.error(
            "VPN subscription creation failed: generate_key returned invalid result user_id=%s server_id=%s client_name=%s result=%s",
            user.id,
            server.id,
            client_name,
            vpn_key,
        )
        return None, None

    included_slots = await get_included_device_slots(session)
    subscription = Subscription(
        user_id=user.id,
        server_id=server.id,
        tariff_months=tariff.days // 30,
        tariff_days=tariff.days,
        billing_mode="tariff",
        vpn_key=vpn_key,
        client_name=client_name,
        platform=platform,
        device_slots=included_slots,
        expires_at=expires_at,
        tariff_id=tariff.id if tariff.id else None,
    )
    session.add(subscription)
    await session.flush()
    _disable_balance_autodebit_after_tariff_purchase(user)

    await _upsert_primary_proxy_account(
        session,
        user_id=user.id,
        server_id=server.id,
        subscription_id=subscription.id,
        client_name=client_name,
        sub_url=vpn_key,
    )
    server.current_clients += 1

    logger.info(
        "VPN subscription created: user_id=%s subscription_id=%s server_id=%s client_name=%s expires_at=%s",
        user.id,
        subscription.id,
        server.id,
        client_name,
        expires_at.isoformat(),
    )
    return subscription, vpn_key


def _parse_iso_datetime(val: Any) -> datetime | None:
    if not val:
        return None
    if isinstance(val, datetime):
        return val.replace(tzinfo=None) if val.tzinfo else val
    raw = str(val).strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw[:19], fmt)
        except Exception:
            pass
    return None


def _extract_vhq_subscription_url(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    for key in ("config_url", "branded_url"):
        val = str(data.get(key, "")).strip()
        if val:
            return val
    return ""


def _extract_vhq_order_id(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    for key in ("order_id", "id", "orderId"):
        val = str(data.get(key, "")).strip()
        if val:
            return val
    return ""


def _match_supplier_order_by_key(
    orders_list: list[dict[str, Any]], vpn_key: str | None
) -> dict[str, Any] | None:
    """Find a supplier order whose branded_url or config_url matches the given vpn_key."""
    if not vpn_key or not isinstance(orders_list, list):
        return None
    key_clean = vpn_key.strip()
    if not key_clean:
        return None

    # 1. Exact match on branded_url or config_url
    for o in orders_list:
        if not isinstance(o, dict):
            continue
        for field in ("branded_url", "config_url"):
            val = str(o.get(field) or "").strip()
            if val and val == key_clean:
                return o

    # 2. Token match (last path segment of URL, min len 10)
    try:
        parsed_key = urlparse(key_clean)
        key_token = parsed_key.path.rstrip("/").split("/")[-1] if parsed_key.path else ""
        if key_token and len(key_token) >= 10:
            for o in orders_list:
                if not isinstance(o, dict):
                    continue
                for field in ("branded_url", "config_url"):
                    val = str(o.get(field) or "").strip()
                    if val:
                        val_token = urlparse(val).path.rstrip("/").split("/")[-1]
                        if val_token and val_token == key_token:
                            return o
    except Exception:
        pass

    return None


async def _claim_vhq_fulfillment_for_execution(
    session,
    fulfillment: VhqSubscriptionFulfillment,
    *,
    lease_token: str | None = None,
) -> bool:
    """Atomically make one worker the only owner allowed to reach supplier POST."""
    now = datetime.utcnow()
    token = lease_token or uuid.uuid4().hex

    if lease_token is not None:
        claim = (
            update(VhqSubscriptionFulfillment)
            .where(VhqSubscriptionFulfillment.id == fulfillment.id)
            .where(VhqSubscriptionFulfillment.status == "processing")
            .where(VhqSubscriptionFulfillment.lease_token == lease_token)
            .where(
                or_(
                    VhqSubscriptionFulfillment.lease_expires_at.is_(None),
                    VhqSubscriptionFulfillment.lease_expires_at > now,
                )
            )
            .values(lease_expires_at=now + _VHQ_FULFILLMENT_LEASE_DURATION)
        )
    else:
        preserved_error_code = case(
            (
                and_(
                    VhqSubscriptionFulfillment.last_error_code.is_(None),
                    VhqSubscriptionFulfillment.status.in_(
                        ("uncertain", "renew_inflight")
                    ),
                ),
                "ambiguous_renew",
            ),
            else_=VhqSubscriptionFulfillment.last_error_code,
        )
        claim = (
            update(VhqSubscriptionFulfillment)
            .where(VhqSubscriptionFulfillment.id == fulfillment.id)
            .where(
                or_(
                    and_(
                        VhqSubscriptionFulfillment.status.in_(
                            ("pending", "retry_wait", "uncertain")
                        ),
                    ),
                    and_(
                        VhqSubscriptionFulfillment.status.in_(
                            ("processing", "renew_inflight")
                        ),
                        or_(
                            VhqSubscriptionFulfillment.lease_expires_at.is_(None),
                            VhqSubscriptionFulfillment.lease_expires_at <= now,
                        ),
                    ),
                )
            )
            .values(
                status="processing",
                lease_token=token,
                lease_expires_at=now + _VHQ_FULFILLMENT_LEASE_DURATION,
                last_error_code=preserved_error_code,
            )
        )

    result = await session.execute(claim)
    if result.rowcount != 1:
        logger.info(
            "VHQ fulfillment %s is already owned by another worker; refusing supplier POST",
            fulfillment.id,
        )
        return False
    await session.commit()
    await session.refresh(fulfillment)
    return True


async def execute_vhq_fulfillment(
    session,
    fulfillment: VhqSubscriptionFulfillment,
    api: VHQPartnerAPI | None = None,
    *,
    lease_token: str | None = None,
) -> None:
    """Execute or reconcile a single VHQ subscription fulfillment."""
    if api is None:
        api = VHQPartnerAPI()
    now = datetime.utcnow()

    # Crash recovery check: if status is buy_inflight or renew_inflight, handle stale inflight leases safely
    if fulfillment.status == "buy_inflight":
        if fulfillment.lease_expires_at and fulfillment.lease_expires_at > now:
            logger.info(
                "Fulfillment %s has active buy_inflight lease until %s. Leaving untouched.",
                fulfillment.id,
                fulfillment.lease_expires_at,
            )
            return
        fulfillment.status = "manual_review"
        fulfillment.lease_token = None
        fulfillment.lease_expires_at = None
        fulfillment.last_error_code = "stale_buy_inflight"
        fulfillment.last_error_message = (
            "Crash recovery: stale buy_inflight detected. Duplicate buy prohibited."
        )
        logger.critical(
            "Fulfillment %s has stale buy_inflight. Moving to manual_review.",
            fulfillment.id,
        )
        await session.commit()
        return

    if fulfillment.status == "renew_inflight":
        if (
            fulfillment.lease_expires_at
            and fulfillment.lease_expires_at > now
            and lease_token != fulfillment.lease_token
        ):
            logger.info(
                "Fulfillment %s has active renew_inflight lease until %s. Leaving untouched.",
                fulfillment.id,
                fulfillment.lease_expires_at,
            )
            return
        logger.warning(
            "Fulfillment %s has expired renew_inflight lease. Reconciling via supplier state.",
            fulfillment.id,
        )

    prior_status = fulfillment.status
    prior_error_code = fulfillment.last_error_code

    if not await _claim_vhq_fulfillment_for_execution(
        session,
        fulfillment,
        lease_token=lease_token,
    ):
        return

    now = datetime.utcnow()

    fulfillment.last_attempt_at = now
    fulfillment.attempts += 1

    sub = await session.get(Subscription, fulfillment.subscription_id)
    if not sub:
        fulfillment.status = "failed"
        fulfillment.lease_token = None
        fulfillment.lease_expires_at = None
        fulfillment.last_error_code = "subscription_not_found"
        await session.commit()
        return

    renew_days = int(sub.tariff_days or 0)
    if renew_days <= 0 and sub.tariff_id:
        linked_tariff = await session.get(Tariff, sub.tariff_id)
        if linked_tariff and linked_tariff.days:
            renew_days = int(linked_tariff.days)
    if renew_days <= 0:
        renew_days = 30

    orders_list = None
    order_id = fulfillment.vhq_order_id or sub.vhq_order_id
    if not order_id and fulfillment.action == "renew":
        # Legacy subscription with NULL vhq_order_id:
        # Client_name is only an unverified legacy candidate. Do NOT use it blindly as truth.
        # Must attempt deterministic reconciliation from read-only supplier state.
        proven_order = None
        try:
            orders_resp = await api.get_orders()
            orders_list = (
                orders_resp
                if isinstance(orders_resp, list)
                else (orders_resp.get("orders", []) if isinstance(orders_resp, dict) else [])
            )
            proven_order = _match_supplier_order_by_key(orders_list, sub.vpn_key)
        except Exception as exc:
            logger.warning("Failed to reconcile legacy VHQ order for sub %s from supplier state: %s", sub.id, exc)

        if proven_order:
            proven_oid = str(proven_order.get("order_id") or proven_order.get("id") or "").strip()
            if proven_oid:
                order_id = proven_oid
                fulfillment.vhq_order_id = order_id
                sub.vhq_order_id = order_id

                has_existing_target = (
                    fulfillment.expected_provider_active_until is not None
                    or fulfillment.baseline_provider_active_until is not None
                )
                if not has_existing_target:
                    sup_active = _parse_iso_datetime(proven_order.get("active_until"))
                    if sup_active:
                        fulfillment.baseline_provider_active_until = sup_active
                        sub.vhq_active_until = sup_active
                        ref_time = sup_active if sup_active > now else now
                        fulfillment.expected_provider_active_until = ref_time + timedelta(days=renew_days)

                # Persist identity and baseline/expected targets durably before any POST renew!
                try:
                    await session.commit()
                except Exception as commit_exc:
                    logger.error(
                        "DB commit failed while persisting legacy identity for sub %s, fulfillment %s: %s",
                        sub.id, fulfillment.id, commit_exc,
                    )
                    await session.rollback()
                    raise commit_exc

                logger.info(
                    "Reconciled and committed legacy VHQ order_id for sub %s from supplier state: %s (baseline=%s, expected=%s)",
                    sub.id,
                    order_id,
                    fulfillment.baseline_provider_active_until,
                    fulfillment.expected_provider_active_until,
                )

        if not order_id:
            candidate = (
                sub.client_name.removeprefix(VHQ_CLIENT_PREFIX)
                if sub.client_name and sub.client_name.startswith(VHQ_CLIENT_PREFIX)
                else None
            )
            fulfillment.status = "manual_review"
            fulfillment.lease_token = None
            fulfillment.lease_expires_at = None
            fulfillment.last_error_code = "unverified_legacy_order"
            fulfillment.last_error_message = (
                f"Cannot prove current supplier order for legacy sub {sub.id}: "
                f"order_id is NULL and candidate {candidate} is not verified by current upstream key"
            )
            await session.commit()
            return

    # 0. Legacy supplier baseline: if unknown, snapshot from GET orders before first renew
    if (
        fulfillment.expected_provider_active_until is None
        and (fulfillment.baseline_provider_active_until is None or sub.vhq_active_until is None)
        and order_id
        and fulfillment.action == "renew"
        and fulfillment.attempts <= 1
    ):
        matched_base = None
        try:
            if orders_list is None:
                orders_resp = await api.get_orders()
                orders_list = orders_resp if isinstance(orders_resp, list) else (orders_resp.get("orders", []) if isinstance(orders_resp, dict) else [])
            matched_base = next(
                (o for o in orders_list if str(o.get("order_id") or o.get("id") or "").strip() == str(order_id).strip()),
                None,
            )
        except Exception as exc:
            logger.warning("Failed to fetch initial baseline for fulfillment %s from supplier state: %s", fulfillment.id, exc)

        if matched_base:
            base_sup_active = _parse_iso_datetime(matched_base.get("active_until"))
            if base_sup_active:
                fulfillment.baseline_provider_active_until = base_sup_active
                sub.vhq_active_until = base_sup_active
                ref_time = base_sup_active if base_sup_active > now else now
                fulfillment.expected_provider_active_until = ref_time + timedelta(days=renew_days)
                try:
                    await session.commit()
                except Exception as commit_exc:
                    logger.error(
                        "DB commit failed while persisting initial baseline for sub %s, fulfillment %s: %s",
                        sub.id, fulfillment.id, commit_exc,
                    )
                    await session.rollback()
                    raise commit_exc

                logger.info(
                    "Established supplier baseline for fulfillment %s, sub %s: baseline=%s, expected=%s",
                    fulfillment.id, sub.id, base_sup_active.isoformat(), fulfillment.expected_provider_active_until.isoformat(),
                )

    if fulfillment.expected_provider_active_until is None and fulfillment.baseline_provider_active_until:
        ref_time = fulfillment.baseline_provider_active_until if fulfillment.baseline_provider_active_until > now else now
        fulfillment.expected_provider_active_until = ref_time + timedelta(days=renew_days)
        try:
            await session.commit()
        except Exception as commit_exc:
            logger.error(
                "DB commit failed while persisting expected target for sub %s, fulfillment %s: %s",
                sub.id, fulfillment.id, commit_exc,
            )
            await session.rollback()
            raise commit_exc

    # 1. Reconciliation: if status was uncertain, inflight, or leased retry, inspect orders first
    effective_error_code = fulfillment.last_error_code or prior_error_code
    is_ambiguous_post_attempt = (
        prior_status in ("renew_inflight", "uncertain")
        or fulfillment.status in ("renew_inflight", "uncertain")
        or effective_error_code in (
            "renew_inflight",
            "network_timeout",
            "http_500",
            "http_502",
            "http_503",
            "http_504",
            "unexpected_error",
            "ambiguous_renew",
            "worker_exception",
        )
        or bool(effective_error_code and (
            effective_error_code.startswith("http_5")
            or effective_error_code.startswith("ambiguous_")
        ))
    )
    should_reconcile = (
        is_ambiguous_post_attempt
        or prior_status == "uncertain"
        or fulfillment.status == "uncertain"
        or fulfillment.attempts > 1
        or effective_error_code is not None
    )
    if should_reconcile and order_id:
        try:
            if orders_list is None:
                orders_resp = await api.get_orders()
                orders_list = orders_resp if isinstance(orders_resp, list) else (orders_resp.get("orders", []) if isinstance(orders_resp, dict) else [])
            matched = next(
                (o for o in orders_list if str(o.get("order_id") or o.get("id") or "").strip() == str(order_id).strip()),
                None,
            )
            if matched:
                sup_active = _parse_iso_datetime(matched.get("active_until"))
                if sup_active:
                    is_ok = False
                    if fulfillment.expected_provider_active_until:
                        is_ok = is_vhq_renewal_active_until_confirmed(
                            sup_active,
                            expected_active_until=fulfillment.expected_provider_active_until,
                            baseline_active_until=fulfillment.baseline_provider_active_until,
                        )
                    else:
                        fulfillment.status = "manual_review"
                        fulfillment.lease_token = None
                        fulfillment.lease_expires_at = None
                        fulfillment.last_error_code = "missing_baseline_unproven_renewal"
                        fulfillment.last_error_message = (
                            f"Order found in reconciliation but renewal cannot be proven without baseline: {matched}"
                        )
                        await session.commit()
                        return
                    if is_ok:
                        fulfillment.status = "completed"
                        fulfillment.lease_token = None
                        fulfillment.lease_expires_at = None
                        fulfillment.last_error_code = None
                        fulfillment.last_error_message = None
                        fulfillment.confirmed_provider_active_until = sup_active
                        fulfillment.completed_at = now
                        sub.vhq_active_until = sup_active
                        sub.expires_at = fulfillment.target_local_expires_at
                        sub.status = SubStatus.ACTIVE
                        logger.info(
                            "Reconciliation confirmed VHQ renew for fulfillment %s, sub %s until %s",
                            fulfillment.id, sub.id, sup_active.isoformat(),
                        )
                        await session.commit()
                        return
                    else:
                        # Order found, but active_until has not reached expected target!
                        # If a POST renew was already dispatched and its response was lost/ambiguous:
                        # Repeating POST renew blindly is strictly prohibited without idempotency key!
                        if is_ambiguous_post_attempt:
                            backoff_seconds = min(30 * (2 ** max(fulfillment.attempts - 1, 0)), 3600)
                            fulfillment.lease_token = None
                            fulfillment.lease_expires_at = None
                            if fulfillment.attempts >= fulfillment.max_attempts:
                                fulfillment.status = "manual_review"
                                fulfillment.last_error_code = "ambiguous_renew_unconfirmed"
                                fulfillment.last_error_message = (
                                    f"POST renew was dispatched, but active_until ({sup_active}) did not reach "
                                    f"expected {fulfillment.expected_provider_active_until} after {fulfillment.attempts} attempts. "
                                    f"Blind re-POST prohibited without idempotency key."
                                )
                                logger.critical(
                                    "Fulfillment %s: ambiguous renew unconfirmed after max attempts. Moving to manual_review.",
                                    fulfillment.id,
                                )
                            else:
                                fulfillment.status = "uncertain"
                                if not fulfillment.last_error_code:
                                    fulfillment.last_error_code = effective_error_code or "ambiguous_renew"
                                fulfillment.next_attempt_at = now + timedelta(seconds=backoff_seconds)
                                logger.warning(
                                    "Fulfillment %s: POST renew dispatched but active_until (%s) is still below expected (%s). "
                                    "Waiting for read convergence; skipping duplicate POST renew.",
                                    fulfillment.id, sup_active, fulfillment.expected_provider_active_until,
                                )
                            await session.commit()
                            return
                else:
                    # Matched, but active_until is unparseable or missing!
                    if is_ambiguous_post_attempt:
                        backoff_seconds = min(30 * (2 ** max(fulfillment.attempts - 1, 0)), 3600)
                        fulfillment.lease_token = None
                        fulfillment.lease_expires_at = None
                        if fulfillment.attempts >= fulfillment.max_attempts:
                            fulfillment.status = "manual_review"
                            fulfillment.last_error_code = "ambiguous_renew_invalid_date"
                            fulfillment.last_error_message = f"Order found in reconciliation but active_until is unparseable: {matched}"
                        else:
                            fulfillment.status = "uncertain"
                            if not fulfillment.last_error_code:
                                fulfillment.last_error_code = effective_error_code or "ambiguous_renew"
                            fulfillment.next_attempt_at = now + timedelta(seconds=backoff_seconds)
                        await session.commit()
                        return
            else:
                # matched is None: order not found in orders_list or orders_list is empty
                if is_ambiguous_post_attempt:
                    backoff_seconds = min(30 * (2 ** max(fulfillment.attempts - 1, 0)), 3600)
                    fulfillment.lease_token = None
                    fulfillment.lease_expires_at = None
                    if fulfillment.attempts >= fulfillment.max_attempts:
                        fulfillment.status = "manual_review"
                        fulfillment.last_error_code = "ambiguous_renew_order_missing"
                        fulfillment.last_error_message = f"POST renew dispatched, but order {order_id} not found in get_orders after {fulfillment.attempts} attempts"
                    else:
                        fulfillment.status = "uncertain"
                        if not fulfillment.last_error_code:
                            fulfillment.last_error_code = effective_error_code or "ambiguous_renew"
                        fulfillment.next_attempt_at = now + timedelta(seconds=backoff_seconds)
                    await session.commit()
                    return
        except Exception as exc:
            logger.warning("Reconciliation get_orders failed for fulfillment %s: %s", fulfillment.id, exc)
            fulfillment.lease_token = None
            fulfillment.lease_expires_at = None
            if fulfillment.attempts >= fulfillment.max_attempts:
                fulfillment.status = "manual_review"
                fulfillment.last_error_code = "max_attempts_exceeded"
                fulfillment.last_error_message = f"Max attempts reached during reconciliation: {exc}"
                await session.commit()
                return
            fulfillment.status = "uncertain"
            if not fulfillment.last_error_code and is_ambiguous_post_attempt:
                fulfillment.last_error_code = effective_error_code or "ambiguous_renew"
            backoff_seconds = min(30 * (2 ** max(fulfillment.attempts - 1, 0)), 3600)
            fulfillment.next_attempt_at = now + timedelta(seconds=backoff_seconds)
            await session.commit()
            return

    # Invariant: If an ambiguous POST was already sent, NEVER allow a duplicate POST!
    if is_ambiguous_post_attempt:
        logger.critical(
            "CRITICAL: Blind re-POST renew prohibited for fulfillment %s (status=%s, err=%s)",
            fulfillment.id, fulfillment.status, effective_error_code,
        )
        fulfillment.lease_token = None
        fulfillment.lease_expires_at = None
        backoff_seconds = min(30 * (2 ** max(fulfillment.attempts - 1, 0)), 3600)
        if fulfillment.attempts >= fulfillment.max_attempts:
            fulfillment.status = "manual_review"
            fulfillment.last_error_code = "ambiguous_renew_unconfirmed"
        else:
            fulfillment.status = "uncertain"
            if not fulfillment.last_error_code:
                fulfillment.last_error_code = effective_error_code or "ambiguous_renew"
            fulfillment.next_attempt_at = now + timedelta(seconds=backoff_seconds)
        await session.commit()
        return

    # 2. Renew action
    if fulfillment.action == "renew":
        backoff_seconds = min(30 * (2 ** max(fulfillment.attempts - 1, 0)), 3600)

        # Preflight check for insufficient balance waiting queue: abort before dispatching renew if still insufficient
        if fulfillment.last_error_code == "insufficient_balance":
            try:
                bal_data = await api.get_balance()
                c_bal = float(bal_data.get("balance", 0.0))
                # Basic 30d renew wholesale is 249 RUB
                if c_bal < 249.0:
                    fulfillment.status = "retry_wait"
                    fulfillment.lease_token = None
                    fulfillment.lease_expires_at = None
                    fulfillment.next_attempt_at = now + timedelta(seconds=60)
                    await session.commit()
                    return
            except Exception as bal_err:
                logger.warning("Preflight balance check for renew fulfillment %s failed: %s", fulfillment.id, bal_err)

        # PoNR: Commit durable in-flight state BEFORE non-idempotent network call!
        fulfillment.status = "renew_inflight"
        fulfillment.last_error_code = "renew_inflight"
        fulfillment.last_attempt_at = now
        fulfillment.lease_token = fulfillment.lease_token or uuid.uuid4().hex
        fulfillment.lease_expires_at = now + _VHQ_FULFILLMENT_LEASE_DURATION
        try:
            await session.commit()
        except Exception as commit_exc:
            await session.rollback()
            raise commit_exc

        try:
            response = await api.renew(order_id=order_id)
        except VHQPartnerAPIError as exc:
            logger.warning("VHQ renew API error fulfillment %s: status=%s msg=%s", fulfillment.id, exc.status, exc)
            fulfillment.lease_token = None
            fulfillment.lease_expires_at = None
            exc_msg = str(exc).lower()
            is_expired_too_long = (
                "expired too long ago" in exc_msg
                or "please buy a new one" in exc_msg
            )
            if exc.status == 409 and is_expired_too_long:
                fulfillment.action = "buy_fallback"
                fulfillment.status = "pending"
                fulfillment.last_error_code = "expired_too_long_ago"
                fulfillment.last_error_message = str(exc)
            elif exc.status == 409:
                fulfillment.status = "manual_review"
                fulfillment.last_error_code = "renew_409_wrong_product"
                fulfillment.last_error_message = str(exc)
                await session.commit()
                return
            elif exc.status in (401, 403):
                fulfillment.status = "manual_review"
                fulfillment.last_error_code = f"http_{exc.status}"
                fulfillment.last_error_message = str(exc)
                await session.commit()
                return
            elif exc.status == 402 or "insufficient balance" in exc_msg:
                fulfillment.last_error_code = "insufficient_balance"
                fulfillment.last_error_message = str(exc)
                if (
                    fulfillment.source_type == "manual_operation"
                    or fulfillment.manual_operation_id is not None
                ):
                    fulfillment.status = "manual_review"
                else:
                    # Confirmed pre-mutation funds rejection: keep in retry_wait indefinitely until balance top-up
                    fulfillment.status = "retry_wait"
                    fulfillment.next_attempt_at = now + timedelta(seconds=min(60, backoff_seconds))
                await session.commit()
                return
            elif exc.status in (400, 404):
                fulfillment.status = "manual_review"
                fulfillment.last_error_code = f"http_{exc.status}"
                fulfillment.last_error_message = str(exc)
                await session.commit()
                return
            elif exc.status in (500, 502, 503, 504) or (exc.status and 500 <= exc.status < 600):
                if fulfillment.attempts >= fulfillment.max_attempts:
                    fulfillment.status = "manual_review"
                    fulfillment.last_error_code = "max_attempts_exceeded"
                    fulfillment.last_error_message = f"Max attempts ({fulfillment.max_attempts}) reached on HTTP {exc.status}: {exc}"
                    await session.commit()
                    return
                fulfillment.status = "uncertain"
                fulfillment.next_attempt_at = now + timedelta(seconds=backoff_seconds)
                fulfillment.last_error_code = f"http_{exc.status}"
                fulfillment.last_error_message = str(exc)
                await session.commit()
                return
            else:
                if fulfillment.attempts >= fulfillment.max_attempts:
                    fulfillment.status = "manual_review"
                    fulfillment.last_error_code = "max_attempts_exceeded"
                    fulfillment.last_error_message = f"Max attempts reached on error: {exc}"
                    await session.commit()
                    return
                fulfillment.status = "uncertain"
                fulfillment.next_attempt_at = now + timedelta(seconds=backoff_seconds)
                fulfillment.last_error_code = f"http_{exc.status}" if exc.status else "ambiguous_renew"
                fulfillment.last_error_message = str(exc)
                await session.commit()
                return
        except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError) as exc:
            logger.warning("VHQ renew network error fulfillment %s: %s", fulfillment.id, exc)
            fulfillment.lease_token = None
            fulfillment.lease_expires_at = None
            if fulfillment.attempts >= fulfillment.max_attempts:
                fulfillment.status = "manual_review"
                fulfillment.last_error_code = "max_attempts_exceeded"
                fulfillment.last_error_message = f"Max attempts reached on network error: {exc}"
                await session.commit()
                return
            fulfillment.status = "uncertain"
            fulfillment.next_attempt_at = now + timedelta(seconds=backoff_seconds)
            fulfillment.last_error_code = "network_timeout"
            fulfillment.last_error_message = str(exc)
            await session.commit()
            return
        except Exception as exc:
            logger.exception("VHQ renew unexpected error fulfillment %s: %s", fulfillment.id, exc)
            fulfillment.lease_token = None
            fulfillment.lease_expires_at = None
            if fulfillment.attempts >= fulfillment.max_attempts:
                fulfillment.status = "manual_review"
                fulfillment.last_error_code = "max_attempts_exceeded"
                fulfillment.last_error_message = f"Max attempts reached on unexpected error: {exc}"
                await session.commit()
                return
            fulfillment.status = "uncertain"
            fulfillment.next_attempt_at = now + timedelta(seconds=backoff_seconds)
            fulfillment.last_error_code = "unexpected_error"
            fulfillment.last_error_message = str(exc)
            await session.commit()
            return
        else:
            fulfillment.lease_token = None
            fulfillment.lease_expires_at = None
            resp_active_until = _parse_iso_datetime(response.get("active_until"))
            fulfillment.provider_response_json = json.dumps(response, default=str)
            is_success = False
            if fulfillment.expected_provider_active_until:
                is_success = is_vhq_renewal_active_until_confirmed(
                    resp_active_until,
                    expected_active_until=fulfillment.expected_provider_active_until,
                    baseline_active_until=fulfillment.baseline_provider_active_until,
                )
            else:
                if bool(response.get("already_requested")):
                    fulfillment.status = "manual_review"
                    fulfillment.last_error_code = "missing_baseline_already_requested"
                    fulfillment.last_error_message = (
                        "Supplier returned already_requested=True without known baseline; cannot prove new renewal cycle"
                    )
                    logger.warning(
                        "Fulfillment %s: already_requested=True with unknown baseline -> manual_review",
                        fulfillment.id,
                    )
                    await session.commit()
                    return
                if response.get("status") in ("scheduled", "renewed") and resp_active_until and resp_active_until > now:
                    is_success = True
                else:
                    fulfillment.status = "manual_review"
                    fulfillment.last_error_code = "missing_baseline_ambiguous_renew"
                    fulfillment.last_error_message = (
                        f"Direct renew returned ambiguous result without baseline: status={response.get('status')} active_until={resp_active_until}"
                    )
                    await session.commit()
                    return

            if is_success:
                fulfillment.status = "completed"
                fulfillment.last_error_code = None
                fulfillment.last_error_message = None
                fulfillment.confirmed_provider_active_until = resp_active_until or fulfillment.expected_provider_active_until
                fulfillment.completed_at = now
                sub.vhq_active_until = fulfillment.confirmed_provider_active_until
                sub.expires_at = fulfillment.target_local_expires_at
                sub.status = SubStatus.ACTIVE
                await session.commit()
                return
            else:
                if fulfillment.attempts >= fulfillment.max_attempts:
                    fulfillment.status = "manual_review"
                    fulfillment.last_error_code = "max_attempts_exceeded"
                    fulfillment.last_error_message = f"Expected active_until {fulfillment.expected_provider_active_until} not confirmed after {fulfillment.attempts} attempts"
                    await session.commit()
                    return
                fulfillment.status = "uncertain"
                fulfillment.last_error_code = "ambiguous_renew"
                fulfillment.last_error_message = (
                    f"Unconfirmed VHQ renew response: status={response.get('status')} "
                    f"active_until={resp_active_until} expected={fulfillment.expected_provider_active_until}"
                )
                fulfillment.next_attempt_at = now + timedelta(seconds=backoff_seconds)
                await session.commit()
                return

    # 3. Fallback buy branch
    if fulfillment.action == "buy_fallback":
        # PoNR: Commit durable state before non-idempotent network call!
        fulfillment.status = "buy_inflight"
        fulfillment.last_attempt_at = now
        fulfillment.lease_token = fulfillment.lease_token or uuid.uuid4().hex
        fulfillment.lease_expires_at = now + _VHQ_FULFILLMENT_LEASE_DURATION
        try:
            await session.commit()
        except Exception as commit_exc:
            await session.rollback()
            raise commit_exc

        try:
            buy_resp = await api.buy(tier="basic", days=30)
        except VHQPartnerAPIError as exc:
            logger.warning("VHQ buy_fallback API error fulfillment %s: status=%s msg=%s", fulfillment.id, exc.status, exc)
            fulfillment.status = "manual_review"
            fulfillment.lease_token = None
            fulfillment.lease_expires_at = None
            fulfillment.last_error_code = f"buy_fallback_http_{exc.status}"
            fulfillment.last_error_message = str(exc)
            await session.commit()
            return
        except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError) as exc:
            logger.error("VHQ buy_fallback ambiguous network error fulfillment %s: %s", fulfillment.id, exc)
            fulfillment.status = "manual_review"
            fulfillment.lease_token = None
            fulfillment.lease_expires_at = None
            fulfillment.last_error_code = "ambiguous_buy_network_error"
            fulfillment.last_error_message = str(exc)
            await session.commit()
            return
        except Exception as exc:
            logger.exception("VHQ buy_fallback unexpected error fulfillment %s: %s", fulfillment.id, exc)
            fulfillment.status = "manual_review"
            fulfillment.lease_token = None
            fulfillment.lease_expires_at = None
            fulfillment.last_error_code = "ambiguous_buy_unexpected_error"
            fulfillment.last_error_message = str(exc)
            await session.commit()
            return

        new_upstream = _extract_vhq_subscription_url(buy_resp)
        new_order_id = _extract_vhq_order_id(buy_resp)
        new_active_until = _parse_iso_datetime(buy_resp.get("active_until"))
        if not new_upstream or not new_order_id:
            fulfillment.status = "manual_review"
            fulfillment.lease_token = None
            fulfillment.lease_expires_at = None
            fulfillment.last_error_code = "missing_buy_data"
            fulfillment.last_error_message = f"Missing upstream or order_id in {buy_resp}"
            await session.commit()
            return

        fulfillment.status = "completed"
        fulfillment.lease_token = None
        fulfillment.lease_expires_at = None
        fulfillment.confirmed_provider_active_until = new_active_until
        fulfillment.completed_at = now
        fulfillment.provider_response_json = json.dumps(buy_resp, default=str)
        sub.vhq_order_id = new_order_id
        sub.vpn_key = new_upstream
        sub.client_name = f"{VHQ_CLIENT_PREFIX}{new_order_id}"[:64]
        sub.vhq_active_until = new_active_until
        sub.expires_at = fulfillment.target_local_expires_at
        sub.status = SubStatus.ACTIVE
        await session.commit()
        return


async def create_or_extend_paid_access(
    session,
    *,
    user: User,
    tariff: Tariff,
    platform: Platform,
    bonus_days: int = 0,
    purchase_action: str = "auto",
    target_subscription_id: int | None = None,
    provisioning_payment=None,
    manual_operation=None,
    recurring_profile_id: int | None = None,
) -> tuple[Subscription | None, str | None]:
    if is_adapt_tariff(tariff):
        return await _create_adapt_paid_subscription(
            session,
            user=user,
            tariff=tariff,
            platform=platform,
            purchase_action=purchase_action,
            target_subscription_id=target_subscription_id,
            provisioning_payment=provisioning_payment,
        )
    vhq_spec = get_vhq_spec_for_tariff(tariff)
    if not vhq_spec:
        return await create_or_extend_paid_subscription(
            session,
            user=user,
            tariff=tariff,
            platform=platform,
            bonus_days=bonus_days,
            force_new=purchase_action == "new",
            target_subscription_id=target_subscription_id,
        )
    return await _create_vhq_paid_subscription(
        session,
        user=user,
        tariff=tariff,
        platform=platform,
        vhq_spec=vhq_spec,
        force_new=purchase_action == "new",
        target_subscription_id=target_subscription_id,
        provisioning_payment=provisioning_payment,
        manual_operation=manual_operation,
        recurring_profile_id=recurring_profile_id,
    )


async def _create_vhq_paid_subscription(
    session,
    *,
    user: User,
    tariff: Tariff,
    platform: Platform,
    vhq_spec: dict[str, int | str],
    force_new: bool = False,
    purchase_action: str = "auto",
    target_subscription_id: int | None = None,
    provisioning_payment=None,
    manual_operation=None,
    recurring_profile_id: int | None = None,
) -> tuple[Subscription | None, str | None]:
    if purchase_action == "new":
        force_new = True
    server = await get_primary_active_server(session)
    if not server:
        if provisioning_payment is not None:
            provisioning_payment.provisioning_failure_code = "missing_server"
            await session.commit()
        issue = build_internal_access_error(
            provider="vhq",
            code="missing_server",
            admin_message=(
                "No placeholder server available for VHQ subscription "
                f"user_id={user.id} telegram_id={user.telegram_id} tariff_id={getattr(tariff, 'id', None)}"
            ),
            client_message=(
                "Оплата прошла, но выдача доступа временно недоступна. "
                "Мы уже получили уведомление и разбираемся."
            ),
        )
        plog(
            "ОШИБКА_ВЫДАЧИ",
            provider="VHQ",
            user_id=user.telegram_id,
            tariff=tariff.label,
            code=issue.code,
            detail=issue.admin_message,
        )
        logger.error(issue.admin_message)
        raise issue

    # Check if this is a renewal of an existing VHQ subscription
    existing = None
    if not force_new and target_subscription_id:
        candidate = await session.get(Subscription, target_subscription_id)
        if candidate and candidate.user_id == user.id and candidate.client_name.startswith(VHQ_CLIENT_PREFIX):
            existing = candidate
        else:
            raise build_internal_access_error(
                provider="vhq", code="invalid_renewal_target",
                admin_message=f"Invalid VHQ renewal target user_id={user.id} subscription_id={target_subscription_id}",
                client_message="Выбранная подписка не найдена или не поддерживает продление.",
            )
    elif not force_new:
        result = await session.execute(
            select(Subscription)
            .where(Subscription.user_id == user.id)
            .where(Subscription.client_name.like(f"{VHQ_CLIENT_PREFIX}%"))
            .order_by(Subscription.expires_at.desc(), Subscription.id.desc())
            .limit(1)
        )
        existing = result.scalar_one_or_none()

    now = datetime.utcnow()

    # Case 1: Brand new subscription (buy flow)
    if not existing:
        try:
            order = await VHQPartnerAPI().buy(
                tier=str(vhq_spec["tier"]),
                days=int(vhq_spec["days"]),
            )
        except VHQPartnerAPIError as exc:
            issue = build_vhq_access_error(
                status=exc.status,
                message=str(exc),
                context=(
                    f"user_id={user.id} telegram_id={user.telegram_id} "
                    f"tariff_id={getattr(tariff, 'id', None)} days={vhq_spec['days']}"
                ),
            )
            if provisioning_payment is not None and issue.code in ("vhq_balance", "vhq_auth", "vhq_request_invalid"):
                provisioning_payment.provisioning_failure_code = issue.code
                await session.commit()
            plog(
                "ОШИБКА_ВЫДАЧИ",
                provider="VHQ",
                user_id=user.telegram_id,
                tariff=tariff.label,
                code=issue.code,
                status=exc.status or "",
                detail=issue.raw_message or str(exc),
            )
            logger.error(issue.admin_message)
            raise issue
        except Exception as exc:
            if provisioning_payment is not None:
                provisioning_payment.provisioning_failure_code = "uncertain_supplier_mutation"
                await session.commit()
            issue = build_internal_access_error(
                provider="vhq",
                code="vhq_runtime",
                admin_message=(
                    "Unexpected VHQ purchase error "
                    f"user_id={user.id} telegram_id={user.telegram_id} "
                    f"tariff_id={getattr(tariff, 'id', None)} error={exc}"
                ),
                raw_message=str(exc),
            )
            plog(
                "ОШИБКА_ВЫДАЧИ",
                provider="VHQ",
                user_id=user.telegram_id,
                tariff=tariff.label,
                code=issue.code,
                detail=issue.raw_message or str(exc),
            )
            logger.error(issue.admin_message)
            raise issue

        upstream_url = VHQPartnerAPI.extract_subscription_url(order)
        if not upstream_url:
            issue = build_vhq_access_error(
                status=None,
                message=f"missing subscription url response={order}",
                context=(
                    f"user_id={user.id} telegram_id={user.telegram_id} "
                    f"tariff_id={getattr(tariff, 'id', None)}"
                ),
            )
            plog(
                "ОШИБКА_ВЫДАЧИ",
                provider="VHQ",
                user_id=user.telegram_id,
                tariff=tariff.label,
                code=issue.code,
                detail=issue.raw_message or "missing subscription url",
            )
            logger.error(issue.admin_message)
            raise issue

        order_id = str(order.get("order_id", "")).strip() or f"{user.telegram_id}_{int(now.timestamp())}"
        client_name = f"{VHQ_CLIENT_PREFIX}{order_id}"[:64]
        included_slots = await get_included_device_slots(session)
        expires_at = now + timedelta(days=int(vhq_spec["days"]))
        active_until = _parse_iso_datetime(order.get("active_until"))

        subscription = Subscription(
            user_id=user.id,
            server_id=server.id,
            tariff_months=tariff.days // 30,
            tariff_days=tariff.days,
            billing_mode="tariff",
            vpn_key=upstream_url,
            client_name=client_name,
            platform=platform,
            device_slots=included_slots,
            expires_at=expires_at,
            tariff_id=tariff.id if tariff.id else None,
            vhq_order_id=order_id,
            vhq_active_until=active_until,
        )
        session.add(subscription)
        await session.flush()
        _disable_balance_autodebit_after_tariff_purchase(user)

        logger.info(
            "VHQ subscription created: user_id=%s subscription_id=%s tariff_id=%s order_id=%s expires_at=%s",
            user.id,
            subscription.id,
            getattr(tariff, "id", None),
            order.get("order_id"),
            expires_at.isoformat(),
        )
        plog(
            "ВЫДАЧА",
            provider="VHQ",
            user_id=user.telegram_id,
            tariff=tariff.label,
            order_id=order.get("order_id") or "",
            subscription_id=subscription.id,
        )
        return subscription, build_vhq_subscription_ref_url(subscription.id) or upstream_url

    # Case 2: Existing VHQ subscription -> RENEWAL via fulfillment outbox
    payment_id = None
    manual_op_id = None
    if provisioning_payment:
        source_type = "payment"
        source_id = f"payment:{provisioning_payment.id}"
        payment_id = provisioning_payment.id
    elif manual_operation:
        source_type = "manual_operation"
        source_id = f"manual:{manual_operation.id}"
        manual_op_id = manual_operation.id
    else:
        source_type = "adhoc"
        source_id = f"adhoc:{user.id}_{int(now.timestamp())}"

    # 1. Single source replay idempotency
    existing_ff = await session.scalar(
        select(VhqSubscriptionFulfillment).where(
            VhqSubscriptionFulfillment.source_type == source_type,
            VhqSubscriptionFulfillment.source_id == source_id,
        )
    )
    if existing_ff:
        logger.info(
            "VHQ fulfillment already exists for %s:%s (status=%s)",
            source_type, source_id, existing_ff.status,
        )
        if existing_ff.status == "completed":
            sub = await session.get(Subscription, existing_ff.subscription_id)
            return sub, build_vhq_subscription_ref_url(sub.id) if sub else existing.vpn_key
        elif existing_ff.status in ("pending", "processing", "retry_wait", "uncertain", "renew_inflight"):
            issue = build_internal_access_error(
                provider="vhq",
                code="supplier_fulfillment_pending",
                admin_message=f"VHQ fulfillment {existing_ff.id} is {existing_ff.status} for {source_type}:{source_id}",
                client_message="Оплата принята, продление выполняется у поставщика. Доступ будет обновлен автоматически.",
            )
            raise issue
        else:
            issue = build_internal_access_error(
                provider="vhq",
                code=existing_ff.last_error_code or "fulfillment_manual_review",
                admin_message=f"VHQ fulfillment {existing_ff.id} requires manual review: {existing_ff.last_error_message}",
                client_message="Ошибка продления у поставщика. Обратитесь в поддержку.",
            )
            raise issue

    # 2. Concurrency check: one active renewal cycle per subscription
    active_cycle = await session.scalar(
        select(VhqSubscriptionFulfillment).where(
            VhqSubscriptionFulfillment.subscription_id == existing.id,
            VhqSubscriptionFulfillment.status.in_(
                ("pending", "processing", "retry_wait", "uncertain", "buy_inflight", "renew_inflight")
            ),
        )
    )
    if active_cycle:
        conflict_ff = VhqSubscriptionFulfillment(
            fulfillment_uuid=str(uuid.uuid4()),
            subscription_id=existing.id,
            user_id=user.id,
            payment_id=payment_id,
            manual_operation_id=manual_op_id,
            recurring_profile_id=recurring_profile_id,
            source_type=source_type,
            source_id=source_id,
            action="renew",
            status="manual_review",
            vhq_order_id=existing.vhq_order_id,
            baseline_local_expires_at=existing.expires_at,
            target_local_expires_at=existing.expires_at,
            last_error_code="concurrent_renewal_conflict",
            last_error_message=(
                f"Concurrent renewal blocked: fulfillment {active_cycle.id} "
                f"is already {active_cycle.status} for subscription {existing.id}"
            ),
        )
        session.add(conflict_ff)
        await session.flush()
        issue = build_internal_access_error(
            provider="vhq",
            code="concurrent_renewal_conflict",
            admin_message=(
                f"Concurrent VHQ renewal blocked: subscription_id={existing.id} "
                f"payment_id={payment_id} active_cycle_id={active_cycle.id}"
            ),
            client_message=(
                "Предыдущее продление ещё обрабатывается. "
                "Оплата сохранена, мы завершим продление в ближайшее время."
            ),
        )
        logger.warning(issue.admin_message)
        raise issue

    # 3. Create fulfillment with baseline and expected targets
    base_expires = existing.expires_at if existing.expires_at > now else now
    target_local_expires = base_expires + timedelta(days=int(vhq_spec["days"]))
    baseline_provider = existing.vhq_active_until
    if baseline_provider:
        expected_provider = (baseline_provider if baseline_provider > now else now) + timedelta(days=int(vhq_spec["days"]))
    else:
        expected_provider = None

    order_id = existing.vhq_order_id

    fulfillment = VhqSubscriptionFulfillment(
        fulfillment_uuid=str(uuid.uuid4()),
        subscription_id=existing.id,
        user_id=user.id,
        payment_id=payment_id,
        manual_operation_id=manual_op_id,
        recurring_profile_id=recurring_profile_id,
        source_type=source_type,
        source_id=source_id,
        action="renew",
        status="pending",
        vhq_order_id=order_id,
        baseline_provider_active_until=baseline_provider,
        expected_provider_active_until=expected_provider,
        baseline_local_expires_at=existing.expires_at,
        target_local_expires_at=target_local_expires,
        attempts=0,
        next_attempt_at=now,
    )
    session.add(fulfillment)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        # 1. Re-query existing fulfillment by source identity
        existing_ff = await session.scalar(
            select(VhqSubscriptionFulfillment).where(
                VhqSubscriptionFulfillment.source_type == source_type,
                VhqSubscriptionFulfillment.source_id == source_id,
            )
        )
        if existing_ff:
            if existing_ff.status == "completed":
                sub = await session.get(Subscription, existing_ff.subscription_id)
                return sub, build_vhq_subscription_ref_url(sub.id) if sub else existing.vpn_key
            elif existing_ff.status in (
                "pending", "processing", "retry_wait", "uncertain", "buy_inflight", "renew_inflight"
            ):
                issue = build_internal_access_error(
                    provider="vhq",
                    code="supplier_fulfillment_pending",
                    admin_message=f"VHQ fulfillment {existing_ff.id} is already in flight ({existing_ff.status})",
                    client_message="Оплата принята, продление выполняется у поставщика. Доступ будет обновлен автоматически.",
                )
                raise issue
            else:
                issue = build_internal_access_error(
                    provider="vhq",
                    code=existing_ff.last_error_code or "fulfillment_manual_review",
                    admin_message=f"VHQ fulfillment {existing_ff.id} requires manual review: {existing_ff.last_error_message}",
                    client_message="Ошибка продления у поставщика. Обратитесь в поддержку.",
                )
                raise issue

        # 2. Only if source fulfillment does not exist, record unique conflict
        conflict_source_id = f"conflict:{source_id}_{uuid.uuid4().hex[:8]}"
        conflict_ff = VhqSubscriptionFulfillment(
            fulfillment_uuid=str(uuid.uuid4()),
            subscription_id=existing.id,
            user_id=user.id,
            payment_id=payment_id,
            manual_operation_id=manual_op_id,
            recurring_profile_id=recurring_profile_id,
            source_type=source_type,
            source_id=conflict_source_id,
            action="renew",
            status="manual_review",
            vhq_order_id=existing.vhq_order_id,
            baseline_local_expires_at=existing.expires_at,
            target_local_expires_at=existing.expires_at,
            last_error_code="concurrent_renewal_conflict",
            last_error_message=f"DB unique constraint violated: {exc}",
        )
        session.add(conflict_ff)
        await session.commit()
        issue = build_internal_access_error(
            provider="vhq",
            code="concurrent_renewal_conflict",
            admin_message=f"Concurrent VHQ renewal caught by index constraint: {exc}",
            client_message="Предыдущее продление ещё обрабатывается. Оплата сохранена.",
        )
        raise issue

    # 4. Attempt inline execution
    await execute_vhq_fulfillment(session, fulfillment)
    await session.commit()

    if fulfillment.status == "completed":
        existing.expires_at = fulfillment.target_local_expires_at
        existing.tariff_days = tariff.days
        existing.tariff_months = tariff.days // 30
        existing.status = SubStatus.ACTIVE
        existing.platform = platform
        if tariff.id:
            existing.tariff_id = tariff.id
        _disable_balance_autodebit_after_tariff_purchase(user)
        await session.commit()
        logger.info(
            "VHQ subscription renewed inline: user_id=%s subscription_id=%s expires_at=%s",
            user.id, existing.id, existing.expires_at.isoformat(),
        )
        plog(
            "ВЫДАЧА_ПРОДЛЕНИЕ",
            provider="VHQ",
            user_id=user.telegram_id,
            tariff=tariff.label,
            order_id=fulfillment.vhq_order_id or "",
            subscription_id=existing.id,
        )
        return existing, build_vhq_subscription_ref_url(existing.id) or existing.vpn_key
    elif fulfillment.status in ("manual_review", "failed"):
        issue = build_internal_access_error(
            provider="vhq",
            code=fulfillment.last_error_code or "fulfillment_failed",
            admin_message=f"VHQ renewal failed inline: {fulfillment.last_error_message}",
            client_message="Ошибка продления у поставщика. Обратитесь в поддержку.",
        )
        raise issue
    else:
        logger.info(
            "VHQ fulfillment queued for background processing: fulfillment_id=%s status=%s",
            fulfillment.id, fulfillment.status,
        )
        issue = build_internal_access_error(
            provider="vhq",
            code="supplier_fulfillment_pending",
            admin_message=f"VHQ fulfillment queued as {fulfillment.status} (id={fulfillment.id})",
            client_message="Оплата принята, продление выполняется у поставщика. Доступ будет обновлен автоматически.",
        )
        raise issue


async def _adapt_create_new_subscription(
    session,
    *,
    user: User,
    tariff: Tariff,
    platform: Platform,
    server: Server,
    provisioning_payment=None,
) -> tuple[Subscription | None, str | None]:
    """Create a brand-new Adapt subscription (new UUID + URL)."""
    plan_uuid = str(tariff.adapt_plan_uuid).strip()
    if provisioning_payment is not None:
        if not provisioning_payment.provisioning_operation_id:
            provisioning_payment.provisioning_operation_id = f"tgpay_{provisioning_payment.id}"
            await session.commit()
        external_user_id = provisioning_payment.provisioning_operation_id
        if (
            provisioning_payment.provisioning_failure_code
            and provisioning_payment.provisioning_failure_code not in {"adapt_api_402", "adapt_api_429", "mutation_in_flight"}
        ):
            raise build_internal_access_error(
                provider="adapt",
                code="adapt_create_awaiting_webhook",
                admin_message=(
                    "Waiting for Adapt subs.created webhook after ambiguous create "
                    f"payment_id={provisioning_payment.id} external_user_id={external_user_id}"
                ),
                client_message="Оплата получена. Проверяем создание доступа, чтобы не выдать второй ключ.",
            )
    else:
        external_user_id = str(user.telegram_id)
    try:
        order = await AdaptAPI().create_subscription(
            plan_uuid,
            external_user_id=external_user_id,
        )
    except AdaptAPIError as exc:
        failure_code = f"adapt_api_{exc.status or 'error'}"
        if provisioning_payment is not None:
            provisioning_payment.provisioning_failure_code = failure_code if exc.status in (402, 429) else "uncertain_supplier_mutation"
            await session.commit()
        if exc.status == 402:
            issue = build_adapt_access_error(
                status=exc.status,
                message=str(exc),
                context=(
                    f"user_id={user.id} telegram_id={user.telegram_id} "
                    f"tariff_id={getattr(tariff, 'id', None)} plan_uuid={plan_uuid}"
                ),
            )
        else:
            issue = build_adapt_access_error(
                status=exc.status,
                message=str(exc),
                code=failure_code,
                context=(
                    f"user_id={user.id} telegram_id={user.telegram_id} "
                    f"tariff_id={getattr(tariff, 'id', None)} plan_uuid={plan_uuid}"
                ),
            )
        plog(
            "ОШИБКА_ВЫДАЧИ",
            provider="Adapt",
            user_id=user.telegram_id,
            tariff=tariff.label,
            code=issue.code,
            status=exc.status or "",
            detail=str(exc),
        )
        logger.error(issue.admin_message)
        raise issue
    except Exception as exc:
        if provisioning_payment is not None:
            provisioning_payment.provisioning_failure_code = "uncertain_supplier_mutation"
            await session.commit()
        issue = build_internal_access_error(
            provider="adapt",
            code="adapt_runtime",
            admin_message=(
                f"Unexpected Adapt error user_id={user.id} telegram_id={user.telegram_id} "
                f"tariff_id={getattr(tariff, 'id', None)} error={exc}"
            ),
            raw_message=str(exc),
        )
        plog(
            "ОШИБКА_ВЫДАЧИ",
            provider="Adapt",
            user_id=user.telegram_id,
            tariff=tariff.label,
            code=issue.code,
            detail=str(exc),
        )
        logger.error(issue.admin_message)
        raise issue

    adapt_uuid = str(order.get("subscription_uuid", "")).strip()
    if not adapt_uuid:
        if provisioning_payment is not None:
            provisioning_payment.provisioning_failure_code = "uncertain_supplier_mutation"
            await session.commit()
        issue = build_internal_access_error(
            provider="adapt",
            code="adapt_missing_uuid",
            admin_message=(
                f"Adapt returned no subscription_uuid: response={order} "
                f"user_id={user.id} telegram_id={user.telegram_id}"
            ),
            client_message=(
                "Оплата прошла, но выдача доступа временно недоступна. "
                "Мы уже получили уведомление и разбираемся."
            ),
        )
        plog(
            "ОШИБКА_ВЫДАЧИ",
            provider="Adapt",
            user_id=user.telegram_id,
            tariff=tariff.label,
            code=issue.code,
            detail=issue.admin_message,
        )
        logger.error(issue.admin_message)
        raise issue

    now = datetime.utcnow()
    expires_at = now + timedelta(days=int(order.get("days") or tariff.days))
    client_name = build_adapt_client_name(adapt_uuid)
    included_slots = await get_included_device_slots(session)
    branded_url = build_adapt_mirror_url(adapt_uuid)
    upstream_url = str(order.get("subscription_url", "")).strip() or branded_url

    subscription = Subscription(
        user_id=user.id,
        server_id=server.id,
        tariff_months=tariff.days // 30,
        tariff_days=tariff.days,
        billing_mode="tariff",
        vpn_key=branded_url or upstream_url,
        client_name=client_name,
        platform=platform,
        device_slots=int(order.get("devices") or included_slots),
        expires_at=expires_at,
        tariff_id=tariff.id if tariff.id else None,
    )
    session.add(subscription)
    await session.flush()

    await session.execute(
        delete(AdaptSubscription).where(AdaptSubscription.subscription_id == subscription.id)
    )

    adapt_record = AdaptSubscription(
        subscription_id=subscription.id,
        adapt_uuid=adapt_uuid,
        adapt_plan_uuid=plan_uuid,
        end_date=expires_at,
        traffic_limit_bytes=order.get("traffic_limit_bytes"),
    )
    session.add(adapt_record)
    if provisioning_payment is not None:
        provisioning_payment.subscription_id = subscription.id
        if provisioning_payment.method == PaymentMethod.YOOKASSA:
            provisioning_payment.provisioning_failure_code = "financial_finalization_pending"
        else:
            provisioning_payment.provisioning_failure_code = None
    _disable_balance_autodebit_after_tariff_purchase(user)

    logger.info(
        "Adapt subscription created: user_id=%s subscription_id=%s adapt_uuid=%s plan_uuid=%s expires_at=%s",
        user.id,
        subscription.id,
        adapt_uuid,
        plan_uuid,
        expires_at.isoformat(),
    )
    plog(
        "ВЫДАЧА",
        provider="Adapt",
        user_id=user.telegram_id,
        tariff=tariff.label,
        adapt_uuid=adapt_uuid,
        subscription_id=subscription.id,
    )
    return subscription, branded_url or upstream_url


async def _create_adapt_paid_subscription(
    session,
    *,
    user: User,
    tariff: Tariff,
    platform: Platform,
    purchase_action: str = "auto",
    target_subscription_id: int | None = None,
    provisioning_payment=None,
) -> tuple[Subscription | None, str | None]:
    """Fulfill an explicit Adapt create, renew or upgrade operation.

    Adapt's current upgrade endpoint replaces the plan and starts its full term.
    It must not be followed by renew.  Legacy/auto purchases only renew an
    existing subscription on the exact same plan; otherwise a new UUID is made.
    """
    server = await get_primary_active_server(session)
    if not server:
        if provisioning_payment is not None:
            provisioning_payment.provisioning_failure_code = "missing_server"
            await session.commit()
        issue = build_internal_access_error(
            provider="adapt",
            code="missing_server",
            admin_message=(
                "No placeholder server available for Adapt subscription "
                f"user_id={user.id} telegram_id={user.telegram_id} tariff_id={getattr(tariff, 'id', None)}"
            ),
            client_message=(
                "Оплата прошла, но выдача доступа временно недоступна. "
                "Мы уже получили уведомление и разбираемся."
            ),
        )
        plog(
            "ОШИБКА_ВЫДАЧИ",
            provider="Adapt",
            user_id=user.telegram_id,
            tariff=tariff.label,
            code=issue.code,
            detail=issue.admin_message,
        )
        logger.error(issue.admin_message)
        raise issue

    plan_uuid = str(tariff.adapt_plan_uuid).strip()
    action = (purchase_action or "auto").strip().lower()

    # Query all user's Adapt subscriptions to enforce single-key continuity
    try:
        user_adapt_pairs = await _user_adapt_subscriptions(session, user_id=user.id)
    except Exception as exc:
        issue = build_internal_access_error(
            provider="adapt",
            code="adapt_subscription_discovery_failed",
            admin_message=f"Failed to query local Adapt subscriptions for user_id={user.id}: {exc}",
            client_message="Не удалось загрузить данные о ваших подписках. Пожалуйста, повторите попытку позже.",
            raw_message=str(exc),
        )
        plog(
            "ОШИБКА_ВЫДАЧИ",
            provider="Adapt",
            user_id=user.telegram_id,
            tariff=tariff.label,
            code=issue.code,
            detail=issue.admin_message,
        )
        logger.error(issue.admin_message)
        raise issue

    user_adapt_subs = [pair[0] for pair in user_adapt_pairs]
    adapt_records_by_sub_id = {pair[0].id: pair[1] for pair in user_adapt_pairs if pair[1]}

    existing = None
    adapt_record = None

    if action == "new":
        if len(user_adapt_subs) == 1 and is_adapt_trial_subscription(
            user_adapt_subs[0], adapt_sub=adapt_records_by_sub_id.get(user_adapt_subs[0].id)
        ):
            existing = user_adapt_subs[0]
            adapt_record = adapt_records_by_sub_id.get(existing.id)
            action = "upgrade"
        else:
            existing = None
            adapt_record = None
    else:
        if target_subscription_id:
            existing, adapt_record = await _adapt_subscription_by_id(
                session, user_id=user.id, subscription_id=target_subscription_id
            )
        elif len(user_adapt_subs) == 1:
            existing = user_adapt_subs[0]
            adapt_record = adapt_records_by_sub_id.get(existing.id)

        if not existing or not adapt_record:
            if len(user_adapt_subs) > 1:
                issue = build_internal_access_error(
                    provider="adapt",
                    code="adapt_ambiguous_target",
                    admin_message=f"Ambiguous target subscription for user_id={user.id} subs_count={len(user_adapt_subs)}",
                    client_message="У вас несколько подписок. Пожалуйста, выберите конкретную подписку для продления в профиле.",
                )
                raise issue

    if action == "new" or not existing or not adapt_record:
        if action in {"renew", "upgrade"}:
            issue = build_internal_access_error(
                provider="adapt",
                code="adapt_target_not_found",
                admin_message=(
                    "Adapt purchase target not found or does not belong to user "
                    f"user_id={user.id} target_subscription_id={target_subscription_id} action={action}"
                ),
                client_message="Выбранная подписка не найдена. Вернитесь в профиль и выберите её заново.",
            )
            raise issue
        return await _adapt_create_new_subscription(
            session,
            user=user,
            tariff=tariff,
            platform=platform,
            server=server,
            provisioning_payment=provisioning_payment,
        )

    # Adapt is authoritative.  A previous failed/legacy fulfillment may have
    # updated our tariff row without actually changing the provider plan.
    # Never decide between renew and upgrade from the local snapshot alone.
    api = AdaptAPI()
    try:
        status_before = await api.get_status(adapt_record.adapt_uuid)
    except Exception as exc:
        issue = build_internal_access_error(
            provider="adapt",
            code="adapt_status_before_purchase_failed",
            admin_message=(
                "Cannot safely verify Adapt state before paid operation "
                f"subscription_id={existing.id} adapt_uuid={adapt_record.adapt_uuid} "
                f"action={action}: {exc}"
            ),
            client_message=(
                "Оплата получена. Не удалось проверить подписку перед изменением. "
                "Мы уже получили уведомление и разберёмся."
            ),
            raw_message=str(exc),
        )
        raise issue

    freeze_state = getattr(provisioning_payment, "provisioning_freeze_state", None) if provisioning_payment else None
    if not freeze_state:
        was_originally_frozen = bool(status_before.get("is_frozen") is True or getattr(adapt_record, "is_frozen", False) is True)
        if was_originally_frozen:
            if provisioning_payment is not None:
                provisioning_payment.provisioning_freeze_state = "pending_unfreeze"
                await session.commit()
            freeze_state = "pending_unfreeze"
    else:
        was_originally_frozen = freeze_state in {"pending_unfreeze", "unfrozen_for_mutation", "mutation_applied", "freeze_restored"}

    if was_originally_frozen and freeze_state in {"pending_unfreeze", "unfrozen_for_mutation"}:
        if status_before.get("is_frozen") is True:
            try:
                logger.info(
                    "Unfreezing frozen Adapt subscription before fulfillment: subscription_id=%s adapt_uuid=%s",
                    existing.id,
                    adapt_record.adapt_uuid,
                )
                await api.unfreeze_subscription(adapt_record.adapt_uuid)
                status_before = await api.get_status(adapt_record.adapt_uuid)
                if status_before.get("is_frozen") is True:
                    raise RuntimeError("Adapt provider still reports subscription is frozen after unfreeze")
            except Exception as exc:
                issue = build_internal_access_error(
                    provider="adapt",
                    code="adapt_unfreeze_failed",
                    admin_message=(
                        f"Failed to unfreeze subscription before fulfillment "
                        f"subscription_id={existing.id} adapt_uuid={adapt_record.adapt_uuid}: {exc}"
                    ),
                    client_message=(
                        "Оплата получена. Не удалось разморозить подписку перед изменением. "
                        "Мы уже получили уведомление и разберёмся."
                    ),
                    raw_message=str(exc),
                )
                raise issue
        if provisioning_payment is not None:
            provisioning_payment.provisioning_freeze_state = "unfrozen_for_mutation"
            await session.commit()

    stored_plan_uuid = str(adapt_record.adapt_plan_uuid).strip()
    current_plan_uuid = str(status_before.get("plan_uuid") or stored_plan_uuid).strip()
    provider_end = _naive_utc(status_before.get("end_date")) if status_before.get("end_date") else None
    provider_devices = status_before.get("devices")
    if current_plan_uuid and current_plan_uuid != stored_plan_uuid:
        logger.warning(
            "Reconciling stale Adapt plan before purchase: subscription_id=%s stored_plan=%s provider_plan=%s",
            existing.id,
            stored_plan_uuid,
            current_plan_uuid,
        )
        adapt_record.adapt_plan_uuid = current_plan_uuid
        actual_tariff = await session.scalar(
            select(Tariff)
            .where(Tariff.adapt_plan_uuid == current_plan_uuid)
            .order_by(Tariff.id.desc())
            .limit(1)
        )
        if actual_tariff:
            existing.tariff_id = actual_tariff.id
            existing.tariff_days = actual_tariff.days
            existing.tariff_months = actual_tariff.days // 30
    if provider_end:
        existing.expires_at = provider_end
        adapt_record.end_date = provider_end
    if provider_devices is not None:
        existing.device_slots = int(provider_devices)
    if (current_plan_uuid and current_plan_uuid != stored_plan_uuid) or provider_end or provider_devices is not None:
        await session.commit()
    same_plan = current_plan_uuid == plan_uuid
    now = datetime.utcnow()

    if action == "renew" and same_plan and is_adapt_trial_subscription(existing, adapt_sub=adapt_record):
        issue = build_internal_access_error(
            provider="adapt",
            code="adapt_trial_requires_upgrade",
            admin_message=(
                "Refusing Adapt renew for introductory plan "
                f"subscription_id={existing.id} current_plan={current_plan_uuid}"
            ),
            client_message="Для продления тестовой подписки выберите платный тариф.",
        )
        raise issue

    if action == "renew" and not same_plan:
        is_expired = bool(
            (existing.expires_at and existing.expires_at <= now)
            or existing.status.value == "expired"
        )
        if is_adapt_trial_subscription(existing, adapt_sub=adapt_record) or is_expired:
            logger.info(
                "Adapt renew requested for %s sub %s with plan %s; switching action to upgrade",
                "expired" if is_expired else "trial",
                existing.id,
                plan_uuid,
            )
            action = "upgrade"
        else:
            issue = build_internal_access_error(
                provider="adapt",
                code="adapt_renew_plan_mismatch",
                admin_message=(
                    f"Refusing Adapt renew with a different plan "
                    f"subscription_id={existing.id} current_plan={current_plan_uuid} requested_plan={plan_uuid}"
                ),
                client_message="Выбранный тариф отличается от текущего. Для смены тарифа используйте улучшение.",
            )
            raise issue

    if action == "auto" and not same_plan:
        action = "upgrade"

    response: dict = {}
    if action == "upgrade":
        upgrade_already_applied = bool(
            same_plan
            and provisioning_payment is not None
            and (
                provisioning_payment.provisioning_freeze_state in {"mutation_applied", "freeze_restored"}
                or (
                    provisioning_payment.provisioning_baseline_plan_uuid
                    and str(provisioning_payment.provisioning_baseline_plan_uuid).strip() != plan_uuid
                )
            )
        )
        if same_plan and not upgrade_already_applied:
            issue = build_internal_access_error(
                provider="adapt",
                code="adapt_upgrade_same_plan",
                admin_message=f"Refusing same-plan Adapt upgrade subscription_id={existing.id}",
                client_message="Эта подписка уже использует выбранный тариф.",
            )
            raise issue
        try:
            status_plan_uuid = str(status_before.get("plan_uuid") or "").strip()
            status_end = _naive_utc(status_before.get("end_date")) if status_before.get("end_date") else None
            mutation_already_applied = bool(
                provisioning_payment is not None
                and provisioning_payment.provisioning_freeze_state in {"mutation_applied", "freeze_restored"}
            )
            is_retry = bool(
                provisioning_payment is not None
                and provisioning_payment.provisioning_baseline_plan_uuid is not None
            )
            if provisioning_payment is not None and not is_retry:
                provisioning_payment.provisioning_baseline_plan_uuid = status_plan_uuid or current_plan_uuid
                provisioning_payment.provisioning_baseline_expires_at = status_end or existing.expires_at
                await session.commit()
            baseline_plan_uuid = str(
                getattr(provisioning_payment, "provisioning_baseline_plan_uuid", None)
                or status_plan_uuid
                or current_plan_uuid
            ).strip()
            baseline_end = (
                getattr(provisioning_payment, "provisioning_baseline_expires_at", None)
                if provisioning_payment is not None
                else status_end
            )
            reactivation_already_applied = bool(
                is_retry
                and status_plan_uuid != plan_uuid
                and status_plan_uuid == baseline_plan_uuid
                and baseline_end
                and baseline_end <= now
                and status_end
                and status_end > baseline_end + timedelta(seconds=30)
            )
            if upgrade_already_applied or mutation_already_applied or (is_retry and status_plan_uuid == plan_uuid):
                response = status_before
                plog(
                    "ВЫДАЧА_СВЕРКА",
                    provider="Adapt",
                    user_id=user.telegram_id,
                    tariff=tariff.label,
                    adapt_uuid=adapt_record.adapt_uuid,
                    action="upgrade",
                )
            else:
                if status_end and status_end <= now and not reactivation_already_applied:
                    # Adapt upgrades active subscriptions only.  A minimum
                    # custom renewal makes an expired UUID active; its
                    # proportional cost is then credited by the upgrade
                    # formula.  This also permits moving to a cheaper plan.
                    await api.renew_subscription_custom(
                        adapt_record.adapt_uuid,
                        ADAPT_MIN_CUSTOM_RENEW_DAYS,
                    )
                    reactivated_status = await api.get_status(adapt_record.adapt_uuid)
                    reactivated_plan_uuid = str(
                        reactivated_status.get("plan_uuid") or ""
                    ).strip()
                    reactivated_end = (
                        _naive_utc(reactivated_status.get("end_date"))
                        if reactivated_status.get("end_date")
                        else None
                    )
                    if (
                        reactivated_plan_uuid != status_plan_uuid
                        or not reactivated_end
                        or reactivated_end <= now
                    ):
                        raise RuntimeError(
                            "Adapt custom renewal did not reactivate the expected plan"
                        )
                    existing.expires_at = reactivated_end
                    adapt_record.end_date = reactivated_end
                    plog(
                        "ВЫДАЧА_РЕАКТИВАЦИЯ",
                        provider="Adapt",
                        user_id=user.telegram_id,
                        tariff=tariff.label,
                        adapt_uuid=adapt_record.adapt_uuid,
                        action="upgrade",
                    )
                elif reactivation_already_applied:
                    plog(
                        "ВЫДАЧА_СВЕРКА",
                        provider="Adapt",
                        user_id=user.telegram_id,
                        tariff=tariff.label,
                        adapt_uuid=adapt_record.adapt_uuid,
                        action="upgrade_reactivation",
                    )
                try:
                    response = await api.upgrade_subscription(adapt_record.adapt_uuid, plan_uuid)
                except Exception as upgrade_err:
                    if "Not enough days" in str(upgrade_err):
                        logger.warning(
                            "Adapt upgrade requires renewal first: %s. Performing custom renewal...",
                            upgrade_err,
                        )
                        await api.renew_subscription_custom(
                            adapt_record.adapt_uuid, ADAPT_MIN_CUSTOM_RENEW_DAYS
                        )
                        response = await api.upgrade_subscription(adapt_record.adapt_uuid, plan_uuid)
                    else:
                        raise
            # The live API docs state that upgrade starts a full new plan term.
            # A successful HTTP response is not enough: verify the actual plan.
            status_response = await api.get_status(adapt_record.adapt_uuid)
            response = {
                **response,
                **{key: value for key, value in status_response.items() if value is not None},
            }
        except AdaptAPIError as exc:
            if provisioning_payment is not None:
                provisioning_payment.provisioning_failure_code = (
                    "adapt_api_402" if exc.status == 402 else "adapt_upgrade_failed"
                )
                await session.commit()
            if exc.status == 402:
                issue = build_adapt_access_error(
                    status=exc.status,
                    message=str(exc),
                    context=(
                        f"subscription_id={existing.id} adapt_uuid={adapt_record.adapt_uuid} "
                        f"new_plan_uuid={plan_uuid}"
                    ),
                )
            else:
                issue = build_adapt_access_error(
                    status=exc.status,
                    message=str(exc),
                    code="adapt_upgrade_failed",
                    context=(
                        f"subscription_id={existing.id} adapt_uuid={adapt_record.adapt_uuid} "
                        f"new_plan_uuid={plan_uuid}"
                    ),
                )
            raise issue
        except Exception as exc:
            if provisioning_payment is not None:
                provisioning_payment.provisioning_failure_code = "adapt_upgrade_failed"
                await session.commit()
            issue = build_internal_access_error(
                provider="adapt",
                code="adapt_upgrade_failed",
                admin_message=(
                    "Adapt upgrade failed "
                    f"subscription_id={existing.id} adapt_uuid={adapt_record.adapt_uuid} "
                    f"new_plan_uuid={plan_uuid}: {exc}"
                ),
                client_message="Оплата прошла, но улучшение подписки временно недоступно. Мы уже получили уведомление.",
                raw_message=str(exc),
            )
            raise issue
        if provisioning_payment is not None:
            provisioning_payment.provisioning_failure_code = None
        adapt_record.adapt_plan_uuid = plan_uuid
    else:
        is_retry = bool(
            provisioning_payment is not None
            and provisioning_payment.provisioning_baseline_expires_at is not None
        )
        if provisioning_payment is not None and not is_retry:
            provisioning_payment.provisioning_baseline_expires_at = provider_end or existing.expires_at or adapt_record.end_date
            provisioning_payment.provisioning_baseline_plan_uuid = str(status_before.get("plan_uuid") or current_plan_uuid)
            await session.commit()
        baseline_end = (
            provisioning_payment.provisioning_baseline_expires_at
            if provisioning_payment is not None
            else None
        )
        if (
            provisioning_payment is not None
            and provisioning_payment.provisioning_freeze_state in {"mutation_applied", "freeze_restored"}
        ) or (is_retry and provider_end and baseline_end and provider_end > baseline_end + timedelta(seconds=30)):
            response = status_before
            adapt_record.end_date = provider_end
            plog(
                "ВЫДАЧА_СВЕРКА",
                provider="Adapt",
                user_id=user.telegram_id,
                tariff=tariff.label,
                adapt_uuid=adapt_record.adapt_uuid,
                action="renew",
            )
        else:
            try:
                renewed = await renew_adapt_subscription(
                    session,
                    adapt_record=adapt_record,
                    tariff_days=tariff.days,
                    manage_freeze=False,
                    raise_api_errors=True,
                )
            except AdaptAPIError as exc:
                if provisioning_payment is not None:
                    provisioning_payment.provisioning_failure_code = (
                        "adapt_api_402" if exc.status == 402 else "adapt_renew_failed"
                    )
                    await session.commit()
                issue = build_adapt_access_error(
                    status=exc.status,
                    message=str(exc),
                    code="adapt_renew_failed",
                    context=(
                        f"subscription_id={existing.id} adapt_uuid={adapt_record.adapt_uuid} "
                        f"tariff_id={getattr(tariff, 'id', None)}"
                    ),
                )
                raise issue
            except Exception as exc:
                if provisioning_payment is not None:
                    provisioning_payment.provisioning_failure_code = "adapt_renew_failed"
                    await session.commit()
                issue = build_internal_access_error(
                    provider="adapt",
                    code="adapt_renew_failed",
                    admin_message=(
                        "Adapt renewal result is unknown "
                        f"subscription_id={existing.id} adapt_uuid={adapt_record.adapt_uuid}: {exc}"
                    ),
                    client_message=(
                        "Оплата получена, но результат продления не подтверждён. "
                        "Доступ не изменён автоматически."
                    ),
                    raw_message=str(exc),
                    stage="unknown",
                    confirmed_rejection=False,
                    safe_retry_eligible=False,
                )
                raise issue
            if renewed:
                try:
                    response = await api.get_status(adapt_record.adapt_uuid)
                except Exception as exc:
                    if provisioning_payment is not None:
                        provisioning_payment.provisioning_failure_code = "adapt_renew_failed"
                        await session.commit()
                    issue = build_internal_access_error(
                        provider="adapt",
                        code="adapt_renew_failed",
                        admin_message=(
                            "Adapt renew result could not be verified "
                            f"subscription_id={existing.id} adapt_uuid={adapt_record.adapt_uuid}: {exc}"
                        ),
                        client_message=(
                            "Оплата прошла, но результат продления пока не подтверждён. "
                            "Мы уже получили уведомление и проверяем подписку."
                        ),
                        raw_message=str(exc),
                    )
                    raise issue
            else:
                response = {}
        if provisioning_payment is not None:
            provisioning_payment.provisioning_failure_code = None

    if action != "upgrade" and not response:
        issue = build_internal_access_error(
            provider="adapt",
            code="adapt_renew_failed",
            admin_message=(
                "Adapt renewal failed for existing subscription "
                f"user_id={user.id} telegram_id={user.telegram_id} "
                f"subscription_id={existing.id} adapt_uuid={adapt_record.adapt_uuid} "
                f"tariff_id={getattr(tariff, 'id', None)} plan_uuid={plan_uuid}"
            ),
            client_message=(
                "Оплата прошла, но продление доступа временно недоступно. "
                "Мы уже получили уведомление и разбираемся."
            ),
        )
        plog(
            "ОШИБКА_ПРОДЛЕНИЯ",
            provider="Adapt",
            user_id=user.telegram_id,
            tariff=tariff.label,
            code=issue.code,
            detail=issue.admin_message,
        )
        logger.error(issue.admin_message)
        raise issue

    verified_plan_uuid = str(
        response.get("plan_uuid") or response.get("new_plan_uuid") or ""
    ).strip()
    verified_devices = response.get("devices")
    if verified_plan_uuid != plan_uuid or verified_devices is None:
        if verified_plan_uuid:
            adapt_record.adapt_plan_uuid = verified_plan_uuid
            actual_tariff = await session.scalar(
                select(Tariff)
                .where(Tariff.adapt_plan_uuid == verified_plan_uuid)
                .order_by(Tariff.id.desc())
                .limit(1)
            )
            if actual_tariff:
                existing.tariff_id = actual_tariff.id
                existing.tariff_days = actual_tariff.days
                existing.tariff_months = actual_tariff.days // 30
        if verified_devices is not None:
            existing.device_slots = int(verified_devices)
        if response.get("end_date"):
            verified_end = _naive_utc(response["end_date"])
            existing.expires_at = verified_end
            adapt_record.end_date = verified_end
        issue = build_internal_access_error(
            provider="adapt",
            code=f"adapt_{action}_failed",
            admin_message=(
                "Adapt paid operation returned an unexpected result "
                f"subscription_id={existing.id} action={action} "
                f"expected_plan={plan_uuid} actual_plan={verified_plan_uuid or 'missing'} "
                f"actual_devices={verified_devices}"
            ),
            client_message=(
                "Оплата получена, но провайдер не подтвердил выбранный тариф. "
                "Мы уже получили уведомление и разберёмся."
            ),
        )
        if provisioning_payment is not None:
            provisioning_payment.provisioning_failure_code = issue.code
        await session.commit()
        raise issue

    raw_end_date = response.get("end_date")
    if action != "upgrade" and not raw_end_date:
        raw_end_date = adapt_record.end_date
    expires_at = _naive_utc(raw_end_date) if raw_end_date else now + timedelta(days=tariff.days)
    branded_url = build_adapt_mirror_url(adapt_record.adapt_uuid)
    upstream_url = f"https://network-api.adaptgroup.app/sub/{adapt_record.adapt_uuid}"

    existing.server_id = server.id
    existing.tariff_months = tariff.days // 30
    existing.tariff_days = tariff.days
    existing.billing_mode = "tariff"
    existing.status = SubStatus.ACTIVE
    existing.platform = platform
    existing.expires_at = expires_at
    existing.vpn_key = existing.vpn_key or branded_url or upstream_url
    existing.client_name = build_adapt_client_name(adapt_record.adapt_uuid)
    existing.tariff_id = tariff.id if tariff.id else None
    existing.device_slots = int(verified_devices)
    adapt_record.adapt_plan_uuid = plan_uuid
    adapt_record.end_date = expires_at
    _disable_balance_autodebit_after_tariff_purchase(user)

    if provisioning_payment is not None and provisioning_payment.provisioning_freeze_state == "unfrozen_for_mutation":
        provisioning_payment.provisioning_freeze_state = "mutation_applied"
        await session.commit()

    if was_originally_frozen:
        try:
            restoration_status = await api.get_status(adapt_record.adapt_uuid)
            already_restored = restoration_status.get("is_frozen") is True
            if not already_restored:
                logger.info(
                    "Restoring frozen state for Adapt subscription after fulfillment: subscription_id=%s adapt_uuid=%s",
                    existing.id,
                    adapt_record.adapt_uuid,
                )
                await api.freeze_subscription(adapt_record.adapt_uuid)
                final_status = await api.get_status(adapt_record.adapt_uuid)
                if final_status.get("is_frozen") is not True:
                    raise RuntimeError("Adapt provider did not confirm frozen state after restoration")
            adapt_record.is_frozen = True
            adapt_record.frozen_at = adapt_record.frozen_at or datetime.utcnow()
            if provisioning_payment is not None:
                provisioning_payment.provisioning_freeze_state = "freeze_restored"
                await session.commit()
        except Exception as exc:
            if provisioning_payment is not None:
                provisioning_payment.provisioning_failure_code = "adapt_refreeze_failed"
                await session.commit()
            issue = build_internal_access_error(
                provider="adapt",
                code="adapt_refreeze_failed",
                admin_message=(
                    f"Adapt mutation succeeded but restoring freeze failed for subscription_id={existing.id} "
                    f"adapt_uuid={adapt_record.adapt_uuid}: {exc}"
                ),
                client_message=(
                    "Оплата получена и подписка продлена, но не удалось восстановить состояние заморозки. "
                    "Мы уже получили уведомление и восстановим заморозку."
                ),
                raw_message=str(exc),
            )
            raise issue

    logger.info(
        "Adapt subscription %s: user_id=%s subscription_id=%s adapt_uuid=%s plan_uuid=%s expires_at=%s devices=%s",
        "upgraded" if action == "upgrade" else "renewed",
        user.id,
        existing.id,
        adapt_record.adapt_uuid,
        plan_uuid,
        expires_at.isoformat(),
        existing.device_slots,
    )
    plog(
        "УЛУЧШЕНИЕ" if action == "upgrade" else "ПРОДЛЕНИЕ",
        provider="Adapt",
        user_id=user.telegram_id,
        tariff=tariff.label,
        adapt_uuid=adapt_record.adapt_uuid,
        subscription_id=existing.id,
    )
    return existing, branded_url or existing.vpn_key


async def create_adapt_demo_subscription(
    session,
    *,
    user: User,
    tariff: Tariff,
    platform: Platform,
) -> tuple[Subscription | None, str | None]:
    """Create a new demo subscription via the Adapt Group API."""
    server = await get_primary_active_server(session)
    if not server:
        return None, None

    plan_uuid = str(tariff.adapt_plan_uuid).strip()

    try:
        order = await AdaptAPI().create_subscription(
            plan_uuid,
            external_user_id=str(user.telegram_id),
        )
    except Exception as exc:
        logger.error(f"Failed to create Adapt demo subscription: {exc}")
        return None, None

    adapt_uuid = str(order.get("subscription_uuid", "")).strip()
    if not adapt_uuid:
        return None, None

    now = datetime.utcnow()
    expires_at = now + timedelta(days=int(order.get("days") or tariff.days))
    
    client_name = build_adapt_client_name(adapt_uuid)
    included_slots = await get_included_device_slots(session)
    branded_url = build_adapt_mirror_url(adapt_uuid)
    upstream_url = str(order.get("subscription_url", "")).strip() or branded_url

    subscription = Subscription(
        user_id=user.id,
        server_id=server.id,
        tariff_months=0,
        tariff_days=tariff.days,
        billing_mode="demo",
        vpn_key=branded_url or upstream_url,
        client_name=client_name,
        platform=platform,
        device_slots=int(order.get("devices") or included_slots),
        expires_at=expires_at,
        tariff_id=None,
    )
    session.add(subscription)
    await session.flush()

    adapt_record = AdaptSubscription(
        subscription_id=subscription.id,
        adapt_uuid=adapt_uuid,
        adapt_plan_uuid=plan_uuid,
        end_date=expires_at,
        traffic_limit_bytes=order.get("traffic_limit_bytes"),
    )
    session.add(adapt_record)

    logger.info(
        "Adapt DEMO subscription created: user_id=%s subscription_id=%s adapt_uuid=%s expires_at=%s",
        user.id,
        subscription.id,
        adapt_uuid,
        expires_at.isoformat(),
    )
    return subscription, branded_url or upstream_url


async def renew_adapt_subscription(
    session,
    *,
    adapt_record: "AdaptSubscription",
    tariff_days: int,
    manage_freeze: bool = True,
    raise_api_errors: bool = False,
) -> bool:
    """Renew an Adapt subscription (e.g. after user payment).

    When manage_freeze is True (for standalone callers outside paid fulfillment orchestration),
    it unfreezes before renew and restores freeze afterwards.
    When manage_freeze is False (called from _create_adapt_paid_subscription),
    fulfillment orchestration owns the freeze lifecycle.
    Returns True on success.
    """
    adapt_uuid = adapt_record.adapt_uuid
    api = AdaptAPI()
    was_frozen = bool(adapt_record.is_frozen)

    async def restore_frozen_state() -> None:
        status = await api.get_status(adapt_uuid)
        if status.get("is_frozen") is not True:
            await api.freeze_subscription(adapt_uuid)
            status = await api.get_status(adapt_uuid)
        if status.get("is_frozen") is not True:
            raise RuntimeError("Adapt provider did not confirm frozen state after restoration")
        adapt_record.is_frozen = True

    async def restore_after_failed_renew() -> None:
        if not manage_freeze or not was_frozen:
            return
        try:
            await restore_frozen_state()
        except Exception as restore_exc:
            logger.error(
                "Failed to restore frozen Adapt subscription after failed renew adapt_uuid=%s: %s",
                adapt_uuid,
                restore_exc,
            )

    try:
        if manage_freeze and was_frozen:
            status_before = await api.get_status(adapt_uuid)
            if status_before.get("is_frozen") is not False:
                await api.unfreeze_subscription(adapt_uuid)
            status_after_unfreeze = await api.get_status(adapt_uuid)
            if status_after_unfreeze.get("is_frozen") is not False:
                raise RuntimeError("Adapt provider still reports subscription is frozen after unfreeze")
        result = await api.renew_subscription(adapt_uuid)
        if manage_freeze and was_frozen:
            try:
                await restore_frozen_state()
            except Exception as restore_exc:
                adapt_record.is_frozen = False
                logger.error(
                    "Adapt renew succeeded but frozen state restoration is unresolved adapt_uuid=%s: %s",
                    adapt_uuid,
                    restore_exc,
                )
        if result.get("end_date"):
            from datetime import datetime as _dt
            try:
                parsed = _dt.fromisoformat(
                    str(result["end_date"]).replace("Z", "+00:00")
                )
                adapt_record.end_date = _naive_utc(parsed)
            except Exception:
                pass
        return True
    except AdaptAPIError as exc:
        await restore_after_failed_renew()
        logger.error(
            "Failed to renew Adapt subscription adapt_uuid=%s: %s (status=%s)",
            adapt_uuid,
            exc,
            exc.status,
        )
        if raise_api_errors or exc.status == 402:
            raise
        return False
    except Exception as exc:
        await restore_after_failed_renew()
        logger.error(
            "Failed to renew Adapt subscription adapt_uuid=%s: %s (status=%s)",
            adapt_uuid,
            exc,
            getattr(exc, "status", None),
        )
        if raise_api_errors:
            raise
        return False


async def create_or_extend_balance_subscription(
    session,
    *,
    user: User,
    platform: Platform,
    expires_at: datetime,
) -> tuple[Subscription | None, str | None]:
    """Create or extend a balance-managed subscription without mixing it with tariff renewals."""
    now = datetime.utcnow()
    existing = await _latest_balance_subscription(session, user.id)
    if not existing:
        existing = await _latest_paid_subscription(session, user.id)
    if not existing:
        existing = await _latest_demo_subscription(session, user.id)

    if existing:
        server = await session.get(Server, existing.server_id)
        if not server:
            server = await get_primary_active_server(session)
        if not server:
            return None, None

        was_inactive = existing.status != SubStatus.ACTIVE or existing.expires_at <= now
        vpn_key = await _ensure_marzban_user(server, existing.client_name, expires_at)
        if not vpn_key:
            return None, None

        existing.server_id = server.id
        # If subscription is an active fixed-tariff, don't overwrite billing_mode or expires_at —
        # the user already paid for the full period; balance top-ups must not shorten it.
        is_active_tariff = existing.billing_mode == "tariff" and existing.expires_at > now
        if not is_active_tariff:
            existing.tariff_months = 0
            existing.tariff_days = 0
            existing.billing_mode = "balance"
            existing.expires_at = expires_at
        existing.status = SubStatus.ACTIVE
        existing.platform = platform
        existing.vpn_key = vpn_key

        effective_expires = existing.expires_at if is_active_tariff else expires_at
        await _upsert_primary_proxy_account(
            session,
            user_id=user.id,
            server_id=server.id,
            subscription_id=existing.id,
            client_name=existing.client_name,
            sub_url=vpn_key,
        )
        await _refresh_extra_device_links(
            session,
            user_id=user.id,
            subscription_id=existing.id,
            primary_client_name=existing.client_name,
            expires_at=effective_expires,
        )
        if was_inactive:
            server.current_clients += 1
        return existing, vpn_key

    server = await get_primary_active_server(session)
    if not server:
        return None, None

    client_name = build_client_name(user.telegram_id, slot=1)
    vpn_key = await vpn_manager.generate_key(
        server=server,
        client_name=client_name,
        expire=int(expires_at.timestamp()),
    )
    if not vpn_key or vpn_key.startswith("Error"):
        return None, None

    included_slots = await get_included_device_slots(session)
    subscription = Subscription(
        user_id=user.id,
        server_id=server.id,
        tariff_months=0,
        tariff_days=0,
        billing_mode="balance",
        vpn_key=vpn_key,
        client_name=client_name,
        platform=platform,
        device_slots=included_slots,
        expires_at=expires_at,
    )
    session.add(subscription)
    await session.flush()

    await _upsert_primary_proxy_account(
        session,
        user_id=user.id,
        server_id=server.id,
        subscription_id=subscription.id,
        client_name=client_name,
        sub_url=vpn_key,
    )
    server.current_clients += 1
    return subscription, vpn_key


async def create_or_extend_balance_adapt_subscription(
    session,
    *,
    user: User,
    tariff: "Tariff",
    platform: Platform,
    expires_at: datetime,
) -> tuple["Subscription | None", str | None]:
    """Extend (or create) an Adapt subscription for daily-balance billing.

    Finds the user's most recent Adapt subscription for this tariff's plan and renews it.
    If the subscription is frozen it is unfrozen first.
    Returns (subscription, vpn_key_url) or (None, None) on failure.
    """
    from bot.models import AdaptSubscription as _AdaptSub

    # Find the user's Adapt subscription linked to this tariff (or any Adapt sub as fallback)
    adapt_subs_result = await session.execute(
        select(Subscription)
        .where(Subscription.user_id == user.id)
        .where(Subscription.client_name.like("adapt_%"))
        .order_by(Subscription.expires_at.desc())
    )
    adapt_subs = adapt_subs_result.scalars().all()

    # Prefer the sub linked to the same tariff
    existing = None
    for sub in adapt_subs:
        if sub.tariff_id == tariff.id:
            existing = sub
            break
    if not existing and adapt_subs:
        existing = adapt_subs[0]

    if not existing:
        return None, None

    adapt_rec_result = await session.execute(
        select(_AdaptSub).where(_AdaptSub.subscription_id == existing.id)
    )
    adapt_rec = adapt_rec_result.scalar_one_or_none()
    if not adapt_rec:
        return None, None

    try:
        renewed = await renew_adapt_subscription(session, adapt_record=adapt_rec, tariff_days=tariff.days)
    except AdaptAPIError as exc:
        if exc.status == 402:
            logger.warning("Adapt balance is insufficient for balance-mode renewal: %s", exc)
        return None, None
    if not renewed:
        return None, None

    existing.billing_mode = "balance"
    existing.status = SubStatus.ACTIVE
    existing.expires_at = expires_at
    existing.tariff_id = tariff.id

    return existing, existing.vpn_key or adapt_rec.adapt_uuid


def _format_proxy_links(secret: str) -> str:
    """Build formatted HTML string with all proxy server links."""
    from bot.services.mtproto_manager import build_all_proxy_links

    links = build_all_proxy_links(secret)
    if not links:
        return ""
    lines = []
    for label, link in links:
        lines.append(f'🔗 <a href="{link}">{label}</a>')
    return "\n".join(lines)


async def create_mtproto_subscription(
    session,
    *,
    user: User,
    tariff: Tariff,
    subscription: Subscription | None = None,
    restart_proxy: bool = True,
) -> tuple[MTProtoAccount | None, str | None]:
    """Create or reuse an MTProto proxy account for the user.

    Args:
        restart_proxy: If True (default), restart proxy after adding secret.
                       Set to False for bulk operations.

    Returns (account, formatted_proxy_links_html) or (None, None) on failure.
    """
    from bot.services.mtproto_manager import (
        add_secret,
        build_mtproto_label,
        generate_secret,
    )

    logger.info(
        "Starting MTProto subscription sync: user_id=%s telegram_id=%s tariff_id=%s tariff_label=%s subscription_id=%s restart_proxy=%s",
        user.id,
        user.telegram_id,
        getattr(tariff, "id", None),
        tariff.label,
        subscription.id if subscription else None,
        restart_proxy,
    )

    desired_label = build_mtproto_label(user.telegram_id)

    # Check for existing active MTProto account(s) for this bot/user.
    result = await session.execute(
        select(MTProtoAccount)
        .where(MTProtoAccount.user_id == user.id)
        .where(MTProtoAccount.is_active == True)  # noqa: E712
        .order_by(MTProtoAccount.id.desc())
    )
    active_accounts = result.scalars().all()
    existing = next((acc for acc in active_accounts if acc.label == desired_label), None)
    stale_accounts = [acc for acc in active_accounts if acc is not existing]

    if existing:
        if subscription:
            existing.subscription_id = subscription.id
        ok = await add_secret(existing.label, existing.secret, restart=restart_proxy)
        if not ok:
            logger.error(
                "MTProto subscription resync failed: user_id=%s account_id=%s label=%s restart_proxy=%s",
                user.id,
                existing.id,
                existing.label,
                restart_proxy,
            )
            return None, None
        for stale in stale_accounts:
            stale.is_active = False
        proxy_links = _format_proxy_links(existing.secret)
        logger.info(
            "Reusing existing MTProto account: user_id=%s account_id=%s subscription_id=%s label=%s stale_deactivated=%s",
            user.id,
            existing.id,
            existing.subscription_id,
            existing.label,
            len(stale_accounts),
        )
        return existing, proxy_links

    if active_accounts:
        # Migrate legacy non-namespaced rows to the current bot-specific label.
        migrated = active_accounts[0]
        previous_label = migrated.label
        previous_subscription_id = migrated.subscription_id
        migrated.label = desired_label
        if subscription:
            migrated.subscription_id = subscription.id
        ok = await add_secret(migrated.label, migrated.secret, restart=restart_proxy)
        if not ok:
            migrated.label = previous_label
            migrated.subscription_id = previous_subscription_id
            logger.error(
                "MTProto legacy migration failed: user_id=%s account_id=%s from_label=%s to_label=%s restart_proxy=%s",
                user.id,
                migrated.id,
                previous_label,
                desired_label,
                restart_proxy,
            )
            return None, None
        for stale in active_accounts[1:]:
            stale.is_active = False
        proxy_links = _format_proxy_links(migrated.secret)
        logger.info(
            "Migrated MTProto account label: user_id=%s account_id=%s from_label=%s to_label=%s stale_deactivated=%s",
            user.id,
            migrated.id,
            previous_label,
            desired_label,
            max(len(active_accounts) - 1, 0),
        )
        return migrated, proxy_links

    # Create new secret
    secret = generate_secret()
    label = desired_label

    ok = await add_secret(label, secret, restart=restart_proxy)
    if not ok:
        logger.error(
            "MTProto subscription creation failed: add_secret returned false user_id=%s label=%s restart_proxy=%s",
            user.id,
            label,
            restart_proxy,
        )
        return None, None

    account = MTProtoAccount(
        user_id=user.id,
        subscription_id=subscription.id if subscription else None,
        secret=secret,
        label=label,
        is_active=True,
    )
    session.add(account)
    await session.flush()

    proxy_links = _format_proxy_links(secret)
    logger.info(
        "MTProto subscription created: user_id=%s account_id=%s subscription_id=%s label=%s",
        user.id,
        account.id,
        account.subscription_id,
        label,
    )
    return account, proxy_links
