"""Helpers for creating and renewing paid subscriptions without rotating links."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from bot.models import (
    AdaptSubscription,
    MTProtoAccount,
    Platform,
    ProxyAccount,
    Server,
    SubStatus,
    Subscription,
    Tariff,
    TariffType,
    User,
)
from bot.services import vpn_manager
from bot.services.adapt_api import ADAPT_MIN_CUSTOM_RENEW_DAYS, AdaptAPI, AdaptAPIError
from bot.services.adapt_routing import ADAPT_CLIENT_PREFIX, build_adapt_client_name, is_adapt_tariff
from bot.services.adapt_subscription_proxy import build_adapt_mirror_url
from bot.services.client_names import build_client_name
from bot.services.device_slots import get_included_device_slots
from bot.services.payment_logger import plog
from bot.services.provisioning_issues import (
    build_internal_access_error,
    build_vhq_access_error,
)
from bot.services.proxy_manager import MarzbanAPI, get_subscription_link
from bot.services.subscription_semantics import (
    demo_access_clause,
    is_adapt_trial_tariff,
    paid_access_clause,
)
from bot.services.vhq_partner_api import VHQPartnerAPI, VHQPartnerAPIError
from bot.services.vhq_routing import VHQ_CLIENT_PREFIX, get_vhq_spec_for_tariff
from bot.services.vhq_subscription_proxy import build_vhq_subscription_ref_url

logger = logging.getLogger(__name__)


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
    row = result.first()
    if not row:
        return None, None
    return row[0], row[1]


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
    )


async def _create_vhq_paid_subscription(
    session,
    *,
    user: User,
    tariff: Tariff,
    platform: Platform,
    vhq_spec: dict[str, int | str],
    force_new: bool = False,
    target_subscription_id: int | None = None,
) -> tuple[Subscription | None, str | None]:
    server = await get_primary_active_server(session)
    if not server:
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

    # Look for an existing VHQ subscription to reuse and keep the same subscription URL
    existing = None
    if not force_new and target_subscription_id:
        candidate = await session.get(Subscription, target_subscription_id)
        if candidate and candidate.user_id == user.id and candidate.client_name.startswith(VHQ_CLIENT_PREFIX):
            existing = candidate
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
    if existing:
        base_expires = existing.expires_at if existing.expires_at > now else now
        expires_at = base_expires + timedelta(days=int(vhq_spec["days"]))
    else:
        expires_at = now + timedelta(days=int(vhq_spec["days"]))

    if existing:
        existing.vpn_key = upstream_url
        existing.expires_at = expires_at
        existing.tariff_days = tariff.days
        existing.tariff_months = tariff.days // 30
        existing.status = SubStatus.ACTIVE
        existing.platform = platform
        if tariff.id:
            existing.tariff_id = tariff.id
        _disable_balance_autodebit_after_tariff_purchase(user)

        logger.info(
            "VHQ subscription updated: user_id=%s subscription_id=%s tariff_id=%s order_id=%s expires_at=%s",
            user.id,
            existing.id,
            getattr(tariff, 'id', None),
            order.get("order_id"),
            expires_at.isoformat(),
        )
        plog(
            "ВЫДАЧА_ПРОДЛЕНИЕ",
            provider="VHQ",
            user_id=user.telegram_id,
            tariff=tariff.label,
            order_id=order.get("order_id") or "",
            subscription_id=existing.id,
        )
        return existing, build_vhq_subscription_ref_url(existing.id) or upstream_url

    order_id = str(order.get("order_id", "")).strip() or f"{user.telegram_id}_{int(now.timestamp())}"
    client_name = f"{VHQ_CLIENT_PREFIX}{order_id}"[:64]
    included_slots = await get_included_device_slots(session)

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
            and provisioning_payment.provisioning_failure_code not in {"adapt_api_402", "adapt_api_429"}
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
            provisioning_payment.provisioning_failure_code = failure_code
            await session.commit()
        issue = build_internal_access_error(
            provider="adapt",
            code=failure_code,
            admin_message=(
                f"Adapt API error during subscription creation: {exc} "
                f"user_id={user.id} telegram_id={user.telegram_id} "
                f"tariff_id={getattr(tariff, 'id', None)} plan_uuid={plan_uuid} status={exc.status}"
            ),
            client_message=(
                "Оплата прошла, но выдача доступа временно недоступна. "
                "Мы уже получили уведомление и разбираемся."
            ),
            raw_message=str(exc),
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
            provisioning_payment.provisioning_failure_code = "adapt_runtime"
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

    adapt_record = AdaptSubscription(
        subscription_id=subscription.id,
        adapt_uuid=adapt_uuid,
        adapt_plan_uuid=plan_uuid,
        end_date=expires_at,
        traffic_limit_bytes=order.get("traffic_limit_bytes"),
    )
    session.add(adapt_record)
    if provisioning_payment is not None:
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
    existing = None
    adapt_record = None
    if target_subscription_id:
        existing, adapt_record = await _adapt_subscription_by_id(
            session, user_id=user.id, subscription_id=target_subscription_id
        )
    elif action == "auto":
        existing, adapt_record = await _latest_adapt_subscription(session, user_id=user.id)
        if adapt_record and str(adapt_record.adapt_plan_uuid).strip() != plan_uuid:
            existing, adapt_record = None, None

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
        available_plans = await api.list_plans()
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

    target_plan = next(
        (
            item for item in available_plans
            if str(item.get("uuid") or item.get("plan_uuid") or "").strip() == plan_uuid
        ),
        None,
    )
    expected_devices = target_plan.get("devices") if target_plan else None
    if (
        target_plan is None
        or expected_devices is None
        or target_plan.get("is_active") is False
    ):
        issue = build_internal_access_error(
            provider="adapt",
            code="adapt_target_plan_unavailable",
            admin_message=(
                "Adapt target plan is missing or has no device limit "
                f"subscription_id={existing.id} target_plan={plan_uuid}"
            ),
            client_message=(
                "Оплата получена, но выбранный тариф временно недоступен у провайдера. "
                "Мы уже получили уведомление и разберёмся."
            ),
        )
        raise issue

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
    same_plan = current_plan_uuid == plan_uuid
    now = datetime.utcnow()

    if action == "renew" and same_plan and is_adapt_trial_tariff(tariff):
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

    if action in {"auto", "renew"} and not same_plan:
        # Persist the provider truth before returning the paid-delivery issue,
        # otherwise the next UI render would continue to advertise the stale plan.
        await session.commit()
        issue = build_internal_access_error(
            provider="adapt",
            code="adapt_renew_plan_mismatch",
            admin_message=(
                "Refusing Adapt renew with a different plan "
                f"subscription_id={existing.id} current_plan={current_plan_uuid} requested_plan={plan_uuid}"
            ),
            client_message="Для продления выберите текущий тариф. Более дорогой тариф оформляется через «Улучшить».",
        )
        raise issue

    response: dict = {}
    if action == "upgrade":
        upgrade_already_applied = bool(
            same_plan
            and provisioning_payment is not None
            and provisioning_payment.provisioning_baseline_plan_uuid
            and str(provisioning_payment.provisioning_baseline_plan_uuid).strip() != plan_uuid
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
            if upgrade_already_applied or (is_retry and status_plan_uuid == plan_uuid):
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
                response = await api.upgrade_subscription(adapt_record.adapt_uuid, plan_uuid)
            # The live API docs state that upgrade starts a full new plan term.
            # A successful HTTP response is not enough: verify the actual plan.
            status_response = await api.get_status(adapt_record.adapt_uuid)
            response = {
                **response,
                **{key: value for key, value in status_response.items() if value is not None},
            }
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
        if is_retry and provider_end and baseline_end and provider_end > baseline_end + timedelta(seconds=30):
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
            renewed = await renew_adapt_subscription(
                session, adapt_record=adapt_record, tariff_days=tariff.days
            )
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
    if verified_plan_uuid != plan_uuid or verified_devices is None or int(verified_devices) != int(expected_devices):
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
                f"expected_devices={expected_devices} actual_devices={verified_devices}"
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
    existing.vpn_key = branded_url or upstream_url
    existing.client_name = build_adapt_client_name(adapt_record.adapt_uuid)
    existing.tariff_id = tariff.id if tariff.id else None
    existing.device_slots = int(verified_devices)
    adapt_record.adapt_plan_uuid = plan_uuid
    adapt_record.end_date = expires_at
    _disable_balance_autodebit_after_tariff_purchase(user)

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
    return existing, branded_url or upstream_url


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
) -> bool:
    """Renew an Adapt subscription (e.g. after user payment).

    If the subscription is frozen, unfreeze it first.
    Returns True on success.
    """
    from bot.models import AdaptSubscription as _AdaptSub  # noqa: F401 (local import to avoid circularity)
    adapt_uuid = adapt_record.adapt_uuid
    try:
        if adapt_record.is_frozen:
            await AdaptAPI().unfreeze_subscription(adapt_uuid)
            adapt_record.is_frozen = False
            adapt_record.frozen_at = None
        result = await AdaptAPI().renew_subscription(adapt_uuid)
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
        logger.error(
            "Failed to renew Adapt subscription adapt_uuid=%s: %s (status=%s)",
            adapt_uuid,
            exc,
            exc.status,
        )
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

    renewed = await renew_adapt_subscription(session, adapt_record=adapt_rec, tariff_days=tariff.days)
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
