"""Subscription scheduler - warnings, expiry handling, follow-ups, recurring reminders."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import aiohttp
import uuid
from aiogram.exceptions import TelegramRetryAfter
from sqlalchemy import and_, exists, or_, select, update
from sqlalchemy.orm import aliased, selectinload
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from bot.database import async_session
from bot.models import (
    BalanceTopUp,
    BalanceTransactionKind,
    BotSettings,
    FollowUpCampaign,
    FollowUpLog,
    MTProtoAccount,
    Payment,
    PaymentMethod,
    PaymentStatus,
    Platform,
    ProxyAccount,
    RecurringNotificationLog,
    RecurringPaymentProfile,
    Server,
    SubStatus,
    Subscription,
    SubscriptionNotificationLog,
    Tariff,
    User,
    VhqSubscriptionFulfillment,
    ManualSubscriptionOperation,
)
from bot.config import settings
from bot.services.backup_service import create_and_send_backup
from bot.services import vpn_manager
from bot.keyboards.client import renew_kb
from bot.services.balance_service import credit_user_balance, debit_user_balance, get_daily_charge_rub, next_charge_datetime
from bot.services.notifications import notify_admins_issue, notify_expired, notify_expiring, notify_staff_text
from bot.services.recurring_retry import (
    MAX_PAYMENT_ATTEMPTS,
    can_retry_now,
    get_next_retry_at,
)
from bot.services.payment_logger import plog
from bot.services.money import kopecks_to_rub_float, rub_to_kopecks
from bot.services.purchase_intent import encode_intent
from bot.services.provisioning_issues import AccessProvisionError
from bot.services.payment_notification_outbox import process_payment_notification_outbox
from bot.services.subscription_semantics import is_demo_subscription_row, paid_access_clause
from bot.services.subscription_service import (
    _claim_vhq_fulfillment_for_execution,
    create_or_extend_balance_subscription,
    create_or_extend_balance_adapt_subscription,
    execute_vhq_fulfillment,
)
from bot.services.adapt_api import AdaptAPI, AdaptAPIError
from bot.services.vhq_partner_api import VHQPartnerAPI, VHQPartnerAPIError

logger = logging.getLogger(__name__)


scheduler: AsyncIOScheduler | None = None

DEMO_WARNING_WINDOW = timedelta(hours=3)
AUTOPAY_WARNING_WINDOW = timedelta(days=2, hours=1)
AUTOPAY_WARNING_CODES: tuple[tuple[str, timedelta, timedelta], ...] = (
    ("charge_2d", timedelta(days=1, hours=1), timedelta(days=2, hours=1)),
    ("charge_1d", timedelta(hours=1), timedelta(days=1, hours=1)),
    ("charge_24h", timedelta(seconds=0), timedelta(hours=1)),  # kept for backward compat
)
PAID_WARNING_WINDOWS: tuple[tuple[str, int, timedelta, timedelta], ...] = (
    ("paid_3d", 3, timedelta(days=2), timedelta(days=3)),
    ("paid_2d", 2, timedelta(days=1), timedelta(days=2)),
    ("paid_1d", 1, timedelta(seconds=0), timedelta(days=1)),
)


def setup_scheduler(bot):
    """Set up periodic jobs."""
    global scheduler
    from bot.services.health_check import check_server_health

    if scheduler and scheduler.running:
        return

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        check_expirations,
        "interval",
        minutes=15,
        args=[bot],
        id="check_expirations",
        replace_existing=True,
    )
    scheduler.add_job(
        send_expiry_warnings,
        "interval",
        minutes=30,
        args=[bot],
        id="send_expiry_warnings",
        replace_existing=True,
    )
    scheduler.add_job(
        send_recurring_charge_warnings,
        "interval",
        hours=1,
        args=[bot],
        id="send_recurring_charge_warnings",
        replace_existing=True,
    )
    scheduler.add_job(
        process_recurring_charges,
        "interval",
        minutes=15,
        args=[bot],
        id="process_recurring_charges",
        replace_existing=True,
    )
    scheduler.add_job(
        process_balance_daily_charges,
        "interval",
        minutes=15,
        args=[bot],
        id="process_balance_daily_charges",
        replace_existing=True,
    )
    scheduler.add_job(
        send_balance_low_warnings,
        "interval",
        hours=1,
        args=[bot],
        id="send_balance_low_warnings",
        replace_existing=True,
    )
    if settings.webstore_public_enabled and settings.webstore_api_base_url.strip():
        scheduler.add_job(
            check_webstore_health,
            "interval",
            minutes=5,
            args=[bot],
            id="check_webstore_health",
            replace_existing=True,
        )
    scheduler.add_job(
        send_followup_mailings,
        "cron",
        hour=12,
        minute=0,
        args=[bot],
        id="send_followup_mailings",
        replace_existing=True,
    )
    scheduler.add_job(
        check_server_health,
        "interval",
        minutes=10,
        args=[bot],
        id="check_server_health",
        replace_existing=True,
    )
    scheduler.add_job(
        check_payment_integrity,
        "interval",
        minutes=10,
        args=[bot],
        id="check_payment_integrity",
        replace_existing=True,
    )
    scheduler.add_job(
        check_overdue_pending_payments,
        "interval",
        minutes=10,
        args=[bot],
        id="check_overdue_pending_payments",
        replace_existing=True,
    )
    from bot.services.yookassa_reconciliation import reconcile_pending_yookassa_payments
    scheduler.add_job(
        reconcile_pending_yookassa_payments,
        "interval",
        minutes=3,
        args=[bot],
        id="reconcile_pending_yookassa_payments",
        replace_existing=True,
    )
    scheduler.add_job(
        process_payment_notification_outbox,
        "interval",
        seconds=30,
        args=[bot],
        id="process_payment_notification_outbox",
        replace_existing=True,
    )
    if settings.webstore_public_enabled and settings.vhq_partner_api_key.strip():
        scheduler.add_job(
            check_vhq_balance,
            "interval",
            hours=12,
            args=[bot],
            id="check_vhq_balance",
            replace_existing=True,
        )
    scheduler.add_job(
        process_vhq_subscription_fulfillments,
        "interval",
        seconds=30,
        args=[bot],
        id="process_vhq_subscription_fulfillments",
        replace_existing=True,
    )
    if AdaptAPI().enabled:
        scheduler.add_job(
            check_adapt_balance,
            "interval",
            hours=12,
            args=[bot],
            id="check_adapt_balance",
            replace_existing=True,
        )
    if settings.run_daily_backup:
        scheduler.add_job(
            create_and_send_backup,
            "cron",
            hour=settings.backup_hour,
            minute=settings.backup_minute,
            args=[bot],
            id="create_daily_backup",
            replace_existing=True,
        )
    scheduler.start()
    logger.info(
        "Scheduler started: expirations every 15 min, warnings every 30 min, "
        "recurring reminders hourly, balance charges every 15 min, balance warnings hourly, follow-up daily at 12:00, health checks every 10 min, "
        "webstore health checks %s, VHQ balance checks %s, daily backups %s",
        "enabled" if settings.webstore_public_enabled and settings.webstore_api_base_url.strip() else "disabled",
        "enabled" if settings.webstore_public_enabled and settings.vhq_partner_api_key.strip() else "disabled",
        "enabled" if settings.run_daily_backup else "disabled",
    )


async def auto_retry_failed_provisionings(bot) -> None:
    """Attempt to provision subscriptions for completed payments without subscription."""
    from bot.services.cluster import LeaseManager, LeaseHeartbeat
    from bot.models import Tariff, Platform, TariffType, RobokassaPayment, Subscription
    from bot.services.subscription_service import create_or_extend_paid_access, create_mtproto_subscription, get_primary_active_server
    from bot.services.provisioning_issues import AccessProvisionError
    from bot.services.device_slots import get_included_device_slots
    from bot.services.payment_service import credit_referral, log_referral_payment, credit_partner
    from bot.handlers.payment import (
        _delivery_platform_kb,
        _is_deferred_platform,
        _platform_from_str,
        _purchase_access_kwargs,
        _send_subscription_key_for_platform,
    )
    from bot.services.vhq_routing import is_vhq_tariff
    
    # We retry payments from the last 2 days
    cutoff = datetime.utcnow() - timedelta(days=2)

    async with async_session() as session:
        completed_topup_exists = (
            select(BalanceTopUp.id)
            .where(BalanceTopUp.provider_payment_id == Payment.provider_payment_id)
            .where(BalanceTopUp.status == "completed")
            .exists()
        )
        result = await session.execute(
            select(Payment.id)
            .where(
                or_(
                    and_(Payment.status == PaymentStatus.COMPLETED, Payment.subscription_id.is_(None)),
                    and_(
                        Payment.method == PaymentMethod.YOOKASSA,
                        Payment.provisioning_failure_code == "financial_finalization_pending",
                    ),
                )
            )
            .where(Payment.created_at >= cutoff)
            .where(~completed_topup_exists)
            .order_by(Payment.created_at.asc())
        )
        payment_ids = result.scalars().all()
    if not payment_ids:
        return

    for pid in payment_ids:
        async with async_session() as session:
            payment = await session.get(Payment, pid)
            if not payment:
                continue
            is_unlinked_completed = payment.status == PaymentStatus.COMPLETED and not payment.subscription_id
            is_yk_fin_pending = (
                payment.method == PaymentMethod.YOOKASSA
                and payment.provisioning_failure_code == "financial_finalization_pending"
            )
            if not (is_unlinked_completed or is_yk_fin_pending):
                continue

            if payment.method == PaymentMethod.YOOKASSA:
                if not payment.provider_payment_id:
                    logger.warning(
                        "Auto-retry payment ID=%s: YooKassa payment missing provider_payment_id; skipping unverified provisioning",
                        payment.id,
                    )
                    continue

                # Route YooKassa payments through authenticated verification/reconciliation pipeline.
                # No lock is acquired here to avoid nested lease acquisition,
                # as verify_and_fulfill_yookassa_payment manages its own lease and LeaseHeartbeat.
                from bot.services.yookassa_reconciliation import verify_and_fulfill_yookassa_payment
                await verify_and_fulfill_yookassa_payment(bot, payment.provider_payment_id, source="auto_retry")
                continue

            from uuid import uuid4
            owner_token = f"{settings.instance_id}:auto_retry:{uuid4().hex[:8]}"
            lease_manager = LeaseManager(owner_token)
            lock_name = f"payment_retry:{payment.id}"

            acquired = await lease_manager.acquire_or_renew(lock_name, ttl_seconds=600)
            if not acquired:
                continue

            heartbeat = LeaseHeartbeat(lease_manager, lock_name, ttl_seconds=30)
            try:
                heartbeat.start()

                # Safety check: respect same supplier-operation safety state as YooKassa reconciler
                if payment.provisioning_failure_code in ("mutation_in_flight", "uncertain_supplier_mutation"):
                    if payment.subscription_id:
                        subscription = await session.get(Subscription, payment.subscription_id)
                        logger.info("Auto-retry payment ID=%s recovering with existing subscription ID=%s", payment.id, payment.subscription_id)
                    else:
                        logger.warning(
                            "Payment ID=%s has unresolved supplier mutation (%s); refusing duplicate mutation in auto-retry",
                            payment.id, payment.provisioning_failure_code,
                        )
                        continue

                user = await session.get(User, payment.user_id)
                if not user:
                    logger.error("Auto-retry payment ID=%s: user not found", payment.id)
                    continue

                tariff = None
                if payment.tariff_id:
                    tariff = await session.get(Tariff, payment.tariff_id)
                
                if not tariff and payment.method.value == "robokassa" and payment.provider_payment_id and payment.provider_payment_id.isdigit():
                    rp = await session.get(RobokassaPayment, int(payment.provider_payment_id))
                    if rp:
                        tariff = await session.get(Tariff, rp.tariff_id)

                if not tariff:
                    # Fallback matching by price
                    amount_val = payment.amount
                    if payment.currency == "XTR":
                        stmt = select(Tariff).where(Tariff.price_stars == amount_val)
                    else:
                        stmt = select(Tariff).where(Tariff.price_rub == kopecks_to_rub_float(amount_val))
                    
                    tariffs = (await session.execute(stmt)).scalars().all()
                    if len(tariffs) == 1:
                        tariff = tariffs[0]
                    elif len(tariffs) > 1:
                        active_tariffs = [t for t in tariffs if t.is_active]
                        tariff = active_tariffs[0] if len(active_tariffs) == 1 else tariffs[0]

                if not tariff:
                    logger.warning("Auto-retry payment ID=%s: could not determine tariff", payment.id)
                    continue

                platform_str = payment.platform
                if not platform_str and payment.method.value == "robokassa" and payment.provider_payment_id and payment.provider_payment_id.isdigit():
                    rp = await session.get(RobokassaPayment, int(payment.provider_payment_id))
                    if rp:
                        platform_str = rp.platform

                platform = None
                if platform_str:
                    platform = _platform_from_str(platform_str)
                else:
                    platform = user.platform or Platform.ANDROID

                logger.info(
                    "Auto-retry provisioning for payment ID=%s, User=%s (%s), Tariff=%s, Platform=%s",
                    payment.id, user.id, user.telegram_id, tariff.label, platform
                )

                is_tg_proxy_only = tariff.tariff_type == TariffType.TG_PROXY
                is_both = tariff.tariff_type == TariffType.BOTH

                saved_platform = user.platform if platform_str and _is_deferred_platform(platform_str) and user.platform and not is_tg_proxy_only else None
                needs_platform_choice = bool(
                    platform_str
                    and _is_deferred_platform(platform_str)
                    and not is_tg_proxy_only
                )
                delivery_platform = saved_platform or platform

                if user.platform is None and not is_tg_proxy_only and not (platform_str and _is_deferred_platform(platform_str)):
                    user.platform = platform

                subscription = None
                vpn_key = None
                proxy_link = None

                if not is_tg_proxy_only and not payment.subscription_id:
                    bonus = 0 if is_vhq_tariff(tariff) else (user.bonus_days or 0)
                    if bonus > 0:
                        user.bonus_days = 0

                    # Persist durable in-flight marker BEFORE supplier mutation
                    payment.provisioning_failure_code = "mutation_in_flight"
                    await session.commit()

                    delivery_issue = None
                    try:
                        subscription, vpn_key = await create_or_extend_paid_access(
                            session,
                            user=user,
                            tariff=tariff,
                            platform=delivery_platform,
                            bonus_days=bonus,
                            provisioning_payment=payment,
                            **_purchase_access_kwargs(platform_str or ""),
                        )
                    except AccessProvisionError as e:
                        delivery_issue = e.code or "delivery_failed"
                        logger.error(
                            "AccessProvisionError in auto-retry for payment ID=%s: %s",
                            payment.id, e.admin_message
                        )
                    except Exception as e:
                        delivery_issue = "delivery_failed"
                        logger.error(
                            "Unexpected error in auto-retry for payment ID=%s: %s",
                            payment.id, e
                        )

                    # Fencing check after supplier provisioning: if lease expired while supplier call was in flight, do NOT proceed
                    lease_is_held = (not heartbeat.lease_lost) and await lease_manager.is_owned_by_me(lock_name)
                    if not lease_is_held:
                        logger.error("Lease %s lost/expired during auto-retry provisioning for payment ID=%s", lock_name, payment.id)
                        async with async_session() as sess:
                            p = await sess.get(Payment, payment.id)
                            if p and p.status == PaymentStatus.COMPLETED and not p.subscription_id:
                                p.provisioning_failure_code = "uncertain_supplier_mutation"
                                if subscription:
                                    p.subscription_id = subscription.id
                                await sess.commit()
                        continue

                    if delivery_issue or not subscription or not vpn_key:
                        fail_code = delivery_issue or "provisioning_failed"
                        logger.error("Auto-retry for payment ID=%s failed: %s", payment.id, fail_code)
                        if payment.provisioning_failure_code == "mutation_in_flight":
                            payment.provisioning_failure_code = fail_code
                            await session.commit()
                        continue
                elif payment.subscription_id:
                    subscription = await session.get(Subscription, payment.subscription_id)

                if is_tg_proxy_only or is_both:
                    if is_tg_proxy_only and not subscription:
                        server = await get_primary_active_server(session)
                        if not server:
                            logger.error("Auto-retry payment ID=%s: no active proxy server found", payment.id)
                            continue
                        
                        now = datetime.utcnow()
                        expires_at = now + timedelta(days=tariff.days)
                        client_name = f"mtproto_tg{user.telegram_id}"
                        included_slots = await get_included_device_slots(session)
                        subscription = Subscription(
                            user_id=user.id,
                            server_id=server.id,
                            tariff_months=tariff.days // 30,
                            tariff_days=tariff.days,
                            vpn_key=None,
                            client_name=client_name,
                            platform=Platform.ANDROID,
                            device_slots=included_slots,
                            expires_at=expires_at,
                        )
                        session.add(subscription)
                        await session.flush()

                    mtproto_account, proxy_link = await create_mtproto_subscription(
                        session, user=user, tariff=tariff, subscription=subscription
                    )
                    if not mtproto_account and is_tg_proxy_only:
                        logger.error("Auto-retry payment ID=%s: failed to create MTProto account", payment.id)
                        continue

                payment.subscription_id = subscription.id if subscription else None
                payment.provisioning_failure_code = None
                await session.flush()

                amount_rub = (
                    float(tariff.price_rub)
                    if payment.currency == "XTR"
                    else kopecks_to_rub_float(payment.amount)
                )
                try:
                    await credit_referral(session, user.id, payment.id, amount_rub, bot=bot)
                    await log_referral_payment(session, user.id, amount_rub, bot=bot, payment_id=payment.id)
                except Exception as e:
                    logger.error("Error crediting referral in auto-retry: %s", e)

                if user.partner_id:
                    try:
                        await credit_partner(session, user.id, payment.id, amount_rub, bot=bot)
                    except Exception as e:
                        logger.error("Error crediting partner in auto-retry: %s", e)

                try:
                    from bot.services.payment_service import credit_cashback
                    await credit_cashback(session, user.id, payment.id, amount_rub, bot=bot)
                except Exception as e:
                    logger.error("Error crediting cashback in auto-retry: %s", e)

                plog(
                    "ОПЛАТА_АВТО_РЕЗОЛВ",
                    provider=payment.method.value,
                    user_id=user.telegram_id,
                    amount=f"{amount_rub:.2f}",
                    tariff=tariff.label,
                    payment_id=payment.id,
                )
                
                await session.commit()
                logger.info(
                    "Auto-retry provisioning succeeded and committed for payment ID=%s, subscription ID=%s",
                    payment.id, subscription.id if subscription else None
                )

                if vpn_key and subscription:
                    key_delivered = False
                    try:
                        delivered = await _send_subscription_key_for_platform(
                            bot=bot,
                            chat_id=user.telegram_id,
                            telegram_id=user.telegram_id,
                            subscription_id=subscription.id,
                            platform=delivery_platform,
                        )
                        if not delivered:
                            logger.error("Failed to deliver key in auto-retry for sub ID=%s", subscription.id)
                        else:
                            key_delivered = True
                    except Exception as exc:
                        logger.error("Failed to deliver key via telegram in auto-retry: %s", exc)

                    if key_delivered and needs_platform_choice:
                        try:
                            from bot.utils.texts import SELECT_DEVICE_AFTER_PAYMENT
                            await bot.send_message(
                                user.telegram_id,
                                SELECT_DEVICE_AFTER_PAYMENT,
                                parse_mode="HTML",
                                reply_markup=_delivery_platform_kb(subscription.id),
                            )
                        except Exception as exc:
                            logger.warning("Failed to send auxiliary platform selector in auto-retry: %s", exc)

                if proxy_link and subscription:
                    try:
                        from bot.utils.texts import MTPROTO_KEY_DELIVERED
                        expires_str = subscription.expires_at.strftime("%d.%m.%Y") if subscription.expires_at else "N/A"
                        await bot.send_message(
                            user.telegram_id,
                            MTPROTO_KEY_DELIVERED.format(proxy_links=proxy_link, expires=expires_str),
                            parse_mode="HTML",
                            disable_web_page_preview=True,
                        )
                    except Exception as exc:
                        logger.error("Failed to deliver proxy link in auto-retry: %s", exc)

                try:
                    async with async_session() as wa_session:
                        wa_enabled_row = await wa_session.get(BotSettings, "whatsapp_proxy_enabled")
                        wa_host_row = await wa_session.get(BotSettings, "whatsapp_proxy_host")
                    if wa_enabled_row and wa_enabled_row.value == "1" and wa_host_row and wa_host_row.value:
                        from bot.utils.texts import WHATSAPP_PROXY_BONUS
                        await bot.send_message(
                            user.telegram_id,
                            WHATSAPP_PROXY_BONUS.format(proxy_host=wa_host_row.value),
                            parse_mode="HTML",
                        )
                except Exception as exc:
                    logger.warning("Failed to deliver WhatsApp proxy bonus in auto-retry: %s", exc)

                admin_text = (
                    "🤖 <b>Авто-выдача доступа после сбоя!</b>\n\n"
                    f"Пользователь: {user.full_name or ''} (<code>{user.telegram_id}</code>)\n"
                    f"Тариф: {tariff.label}\n"
                    f"Платформа: {delivery_platform.value if hasattr(delivery_platform, 'value') else str(delivery_platform)}\n"
                    f"Платеж ID: {payment.id}\n"
                    f"Способ: {payment.method.value}\n"
                    f"Доступ успешно выдан авто-попыткой!"
                )
                await notify_staff_text(bot, admin_text)

            except Exception as e:
                logger.error("Error in auto-retry loop for payment ID=%s: %s", payment.id, e)
            finally:
                await heartbeat.stop()
                await lease_manager.release(lock_name)


async def check_payment_integrity(bot) -> None:
    """Alert admins if a completed non-topup payment is not linked to access."""
    try:
        await auto_retry_failed_provisionings(bot)
    except Exception as e:
        logger.error("Error running auto_retry_failed_provisionings: %s", e)

    cutoff = datetime.utcnow() - timedelta(days=30)

    async with async_session() as session:
        completed_topup_exists = (
            select(BalanceTopUp.id)
            .where(BalanceTopUp.provider_payment_id == Payment.provider_payment_id)
            .where(BalanceTopUp.status == "completed")
            .exists()
        )
        result = await session.execute(
            select(Payment, User)
            .join(User, User.id == Payment.user_id)
            .where(Payment.status == PaymentStatus.COMPLETED)
            .where(Payment.subscription_id.is_(None))
            .where(Payment.created_at >= cutoff)
            .where(~completed_topup_exists)
            .order_by(Payment.created_at.desc())
            .limit(10)
        )
        rows = result.all()
        ids = ",".join(str(payment.id) for payment, _user in rows)
        marker = await session.get(BotSettings, "payment_integrity_alert_ids")

        if not rows:
            if marker and marker.value:
                marker.value = ""
                await session.commit()
            return
        if marker and marker.value == ids:
            return

        details = []
        for payment, user in rows:
            amount_rub = (
                kopecks_to_rub_float(payment.amount)
                if payment.currency == "RUB"
                else float(payment.amount)
            )
            username = f"@{user.username}" if user.username else "—"
            details.append(
                f"payment_id={payment.id}, user={user.telegram_id} {username}, "
                f"amount={amount_rub:.2f} {payment.currency}, method={payment.method.value}, "
                f"created_at={payment.created_at}, provider_id={payment.provider_payment_id or '—'}"
            )

        await notify_admins_issue(
            bot,
            title="Оплата прошла, но подписка не привязана",
            details=details,
        )

        if marker:
            marker.value = ids
        else:
            session.add(BotSettings(key="payment_integrity_alert_ids", value=ids))
        await session.commit()


async def check_overdue_pending_payments(bot) -> None:
    """Always-runnable periodic audit for overdue pending payments and balance topups.

    Verifies bounded candidates against YooKassa and alerts admins without spam.
    Ensures long-lived unresolved payments remain visibly tracked until resolved.
    """
    from bot.services.problem_payments import (
        alert_admins_if_problem_payments_detected,
        refresh_unsnapshotted_yookassa_state,
    )
    try:
        await refresh_unsnapshotted_yookassa_state()
        await alert_admins_if_problem_payments_detected(bot)
    except Exception as exc:
        logger.error("Error in check_overdue_pending_payments audit: %s", exc)


async def send_balance_low_warnings(bot):
    """Warn once per charge cycle when the current balance only covers about one more day."""
    now = datetime.utcnow()
    warning_window_end = now + timedelta(hours=24)

    async with async_session() as session:
        daily_rate = await get_daily_charge_rub(session)
        result = await session.execute(
            select(User)
            .where(User.balance_mode_enabled == True)  # noqa: E712
            .where(User.balance_autodebit_enabled == True)  # noqa: E712
            .where(User.balance_grace_until.is_(None))
            .where(User.next_daily_charge_at.is_not(None))
            .where(User.next_daily_charge_at > now)
            .where(User.next_daily_charge_at <= warning_window_end)
        )
        users = result.scalars().all()

        for user in users:
            next_charge_at = user.next_daily_charge_at
            if not next_charge_at:
                continue
            if user.balance_warning_for_charge_at == next_charge_at:
                continue

            current_balance = round(float(user.balance_rub or 0.0), 2)
            if current_balance >= round(daily_rate * 2, 2):
                continue

            if current_balance < daily_rate:
                text = (
                    "⚠️ <b>Баланс закончится завтра</b>\n\n"
                    f"Следующее списание: <b>{_format_date(next_charge_at)}</b>\n"
                    "На ближайшем списании баланс уйдёт в минус.\n\n"
                    "Пополните баланс сегодня, чтобы доступ не остановился."
                )
            else:
                text = (
                    "⚠️ <b>Баланс закончится завтра</b>\n\n"
                    f"Следующее списание: <b>{_format_date(next_charge_at)}</b>\n"
                    "По текущему остатку денег хватит только до ближайшего списания.\n\n"
                    "Пополните баланс сегодня, чтобы доступ не остановился."
                )

            try:
                await bot.send_message(user.telegram_id, text, parse_mode="HTML")
                user.balance_warning_for_charge_at = next_charge_at
            except Exception:
                pass

        await session.commit()


async def process_balance_daily_charges(bot):
    """Run daily balance debits for users who enabled balance mode."""
    now = datetime.utcnow()

    async with async_session() as session:
        global_daily_rate = await get_daily_charge_rub(session)
        result = await session.execute(
            select(User)
            .where(User.balance_mode_enabled == True)  # noqa: E712
            .where(User.balance_autodebit_enabled == True)  # noqa: E712
            .where(User.next_daily_charge_at.is_not(None))
            .where(User.next_daily_charge_at <= now)
        )
        users = result.scalars().all()

        for user in users:
            # Skip if user has an active fixed-tariff subscription (billing_mode='tariff')
            # with meaningful time remaining — they already paid for the period.
            tariff_sub_result = await session.execute(
                select(Subscription)
                .where(Subscription.user_id == user.id)
                .where(Subscription.billing_mode == "tariff")
                .where(Subscription.status == SubStatus.ACTIVE)
                .where(Subscription.expires_at > now + timedelta(hours=20))
                .limit(1)
            )
            if tariff_sub_result.scalar_one_or_none():
                continue

            # Determine daily rate: per-tariff if user chose one, else global
            charge_tariff: Tariff | None = None
            if user.daily_charge_tariff_id:
                charge_tariff = await session.get(Tariff, user.daily_charge_tariff_id)
            daily_rate = (
                round(float(charge_tariff.price_rub) / max(charge_tariff.days, 1), 2)
                if charge_tariff and charge_tariff.days > 0
                else global_daily_rate
            )

            current_balance = round(float(user.balance_rub or 0.0), 2)

            if current_balance < daily_rate and user.balance_grace_until and user.balance_grace_until <= now:
                user.balance_mode_enabled = False
                user.balance_autodebit_enabled = False
                user.balance_grace_until = None
                user.next_daily_charge_at = None
                user.balance_warning_for_charge_at = None
                try:
                    await bot.send_message(
                        user.telegram_id,
                        "🔴 <b>Ежедневные списания остановлены</b>\n\n"
                        "Баланс не был пополнен вовремя.\n"
                        "Пополните баланс и включите списания снова.",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
                continue

            charge_at = user.next_daily_charge_at or now
            next_charge_at = next_charge_datetime(charge_at + timedelta(minutes=1))
            debit_user_balance(
                session,
                user,
                daily_rate,
                BalanceTransactionKind.DAILY_CHARGE,
                "Ежедневное списание",
                source_type="daily_charge",
                source_id=charge_at.isoformat(),
            )

            # Dispatch to the correct provider
            if charge_tariff and getattr(charge_tariff, "adapt_plan_uuid", None):
                sub, vpn_key = await create_or_extend_balance_adapt_subscription(
                    session,
                    user=user,
                    tariff=charge_tariff,
                    platform=user.platform or Platform.ANDROID,
                    expires_at=next_charge_at,
                )
            else:
                sub, vpn_key = await create_or_extend_balance_subscription(
                    session,
                    user=user,
                    platform=user.platform or Platform.ANDROID,
                    expires_at=next_charge_at,
                )
            if not sub or not vpn_key:
                credit_user_balance(
                    session,
                    user,
                    daily_rate,
                    BalanceTransactionKind.REFUND,
                    "Возврат: не удалось продлить доступ",
                    source_type="daily_charge_refund",
                    source_id=charge_at.isoformat(),
                )
                user.balance_mode_enabled = False
                user.balance_autodebit_enabled = False
                user.balance_grace_until = None
                user.next_daily_charge_at = None
                user.balance_warning_for_charge_at = None
                try:
                    await bot.send_message(
                        user.telegram_id,
                        "🔴 <b>Не удалось продлить доступ с баланса</b>\n\n"
                        "Списания остановлены. Попробуйте включить их снова позже.",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
                continue

            user.last_daily_charge_at = now
            user.next_daily_charge_at = next_charge_at
            user.balance_warning_for_charge_at = None

            if round(float(user.balance_rub or 0.0), 2) < 0:
                if not user.balance_grace_until:
                    user.balance_grace_until = user.next_daily_charge_at
                    try:
                        await bot.send_message(
                            user.telegram_id,
                            "⚠️ <b>Баланс ушёл в минус</b>\n\n"
                            "Это последний день льготного периода.\n"
                            "Пополните баланс до следующего списания, чтобы режим не остановился.",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
            else:
                user.balance_grace_until = None

        await session.commit()


def stop_scheduler() -> None:
    """Stop scheduler on leadership loss or graceful shutdown."""
    global scheduler
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
    scheduler = None


def _is_demo_subscription(sub: Subscription) -> bool:
    return is_demo_subscription_row(sub)


_MSK = timezone(timedelta(hours=3))


def _format_date(dt: datetime) -> str:
    """Format datetime in Moscow time."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_MSK).strftime("%d.%m.%Y %H:%M МСК")


def _format_balance_line(user_balance: float, custom_template: str = "") -> str:
    if user_balance <= 0:
        return ""
    if custom_template:
        try:
            return "\n\n" + custom_template.format(balance=f"{user_balance:.2f}")
        except Exception:
            pass
    return f"\n\n💰 У вас на балансе <b>{user_balance:.2f} ₽</b> — вы можете использовать их для оплаты продления!"


def _paid_warning_text(sub: Subscription, days: int, user_balance: float = 0.0, custom_balance_template: str = "") -> str:
    day_word = "день" if days == 1 else ("дня" if days in (2, 3, 4) else "дней")
    balance_line = _format_balance_line(user_balance, custom_balance_template)
    return (
        f"⚠️ <b>Подписка скоро закончится</b>\n\n"
        f"Доступ перестанет работать через <b>{days} {day_word}</b>.\n"
        f"Дата окончания: <b>{_format_date(sub.expires_at)}</b>\n\n"
        f"Чтобы не потерять доступ, продлите подписку заранее."
        f"{balance_line}"
    )


def _demo_warning_text(sub: Subscription) -> str:
    return (
        f"⏳ <b>Демо-доступ скоро закончится</b>\n\n"
        f"До окончания осталось меньше <b>3 часов</b>.\n"
        f"Дата окончания: <b>{_format_date(sub.expires_at)}</b>\n\n"
        f"Если хотите сохранить доступ, оформите тариф."
    )


def _expired_text(sub: Subscription, user_balance: float = 0.0, custom_balance_template: str = "") -> str:
    balance_line = _format_balance_line(user_balance, custom_balance_template)

    if _is_demo_subscription(sub):
        return (
            f"🔴 <b>Демо-доступ завершен</b>\n\n"
            f"Демо-ключ отключен {_format_date(sub.expires_at)}.\n"
            f"Чтобы снова получить доступ, оформите платный тариф."
            f"{balance_line}"
        )

    return (
        f"🔴 <b>Подписка завершена</b>\n\n"
        f"Ключ отключен {_format_date(sub.expires_at)}.\n"
        f"Чтобы восстановить доступ, продлите подписку."
        f"{balance_line}"
    )


def _recurring_warning_text(
    profile: RecurringPaymentProfile,
    subscription: Subscription | None,
) -> str:
    payment_label = profile.payment_method_label or profile.provider
    expires_line = ""
    if subscription and subscription.expires_at:
        expires_line = f"\nТекущий доступ действует до: <b>{_format_date(subscription.expires_at)}</b>"
    return (
        f"💳 <b>Скоро автосписание</b>\n\n"
        f"Запланирована попытка списания по автопродлению.\n"
        f"Когда: <b>{_format_date(profile.next_charge_at)}</b>\n"
        f"Способ оплаты: <b>{payment_label}</b>{expires_line}\n\n"
        f"Если хотите отключить автопродление, сделайте это заранее."
    )


async def _has_subscription_notification(
    session,
    subscription_id: int,
    notification_code: str,
) -> bool:
    existing = await session.scalar(
        select(SubscriptionNotificationLog.id)
        .where(SubscriptionNotificationLog.subscription_id == subscription_id)
        .where(SubscriptionNotificationLog.notification_code == notification_code)
        .limit(1)
    )
    return existing is not None


async def _has_recurring_notification(
    session,
    recurring_profile_id: int,
    notification_code: str,
) -> bool:
    existing = await session.scalar(
        select(RecurringNotificationLog.id)
        .where(RecurringNotificationLog.recurring_profile_id == recurring_profile_id)
        .where(RecurringNotificationLog.notification_code == notification_code)
        .limit(1)
    )
    return existing is not None


async def _latest_paid_subscription_ids(session, now: datetime) -> dict[int, int]:
    result = await session.execute(
        select(Subscription)
        .where(Subscription.status == SubStatus.ACTIVE)
        .where(Subscription.expires_at > now)
        .where(paid_access_clause(Subscription))
        .order_by(Subscription.user_id, Subscription.expires_at.desc(), Subscription.id.desc())
    )
    latest: dict[int, int] = {}
    for sub in result.scalars():
        latest.setdefault(sub.user_id, sub.id)
    return latest


async def _has_future_paid_access(
    session,
    user_id: int,
    now: datetime,
    exclude_subscription_id: int | None = None,
) -> bool:
    stmt = (
        select(Subscription.id)
        .where(Subscription.user_id == user_id)
        .where(Subscription.status == SubStatus.ACTIVE)
        .where(Subscription.expires_at > now)
        .where(paid_access_clause(Subscription))
    )
    if exclude_subscription_id is not None:
        stmt = stmt.where(Subscription.id != exclude_subscription_id)
    existing = await session.scalar(stmt.limit(1))
    return existing is not None


async def _revoke_single_key(
    session,
    server_id: int,
    client_name: str,
    revoked_targets: set[tuple[int, str]],
    decrement_clients: bool = True,
) -> bool:
    target = (server_id, client_name)
    if target in revoked_targets:
        return False

    server = await session.get(Server, server_id)
    if not server:
        return False

    revoked = await vpn_manager.revoke_key(server=server, client_name=client_name)
    if revoked:
        revoked_targets.add(target)
        if decrement_clients:
            server.current_clients = max(0, server.current_clients - 1)
    else:
        logger.warning("Failed to revoke key %s on server %s", client_name, server_id)
    return revoked


async def _disable_single_key(
    session,
    server_id: int,
    client_name: str,
    disabled_targets: set[tuple[int, str]],
    decrement_clients: bool = True,
) -> bool:
    target = (server_id, client_name)
    if target in disabled_targets:
        return False

    server = await session.get(Server, server_id)
    if not server:
        return False

    disabled = await vpn_manager.disable_key(server=server, client_name=client_name)
    if disabled:
        disabled_targets.add(target)
        if decrement_clients:
            server.current_clients = max(0, server.current_clients - 1)
    else:
        logger.warning("Failed to disable key %s on server %s", client_name, server_id)
    return disabled


async def _disable_paid_access(
    session,
    sub: Subscription,
    disabled_targets: set[tuple[int, str]],
) -> bool:
    proxy_result = await session.execute(
        select(ProxyAccount)
        .where(ProxyAccount.user_id == sub.user_id)
        .where(ProxyAccount.marzban_username.notlike("%_demo"))
    )
    proxies = proxy_result.scalars().all()

    targets = {(proxy.server_id, proxy.marzban_username) for proxy in proxies}
    targets.add((sub.server_id, sub.client_name))

    disabled_any = False
    for server_id, client_name in sorted(targets):
        disabled = await _disable_single_key(
            session,
            server_id,
            client_name,
            disabled_targets,
            decrement_clients=False,
        )
        disabled_any = disabled_any or disabled

    server = await session.get(Server, sub.server_id)
    if server:
        server.current_clients = max(0, server.current_clients - 1)

    return disabled_any


async def check_expirations(bot):
    """Find expired subscriptions, revoke keys when access truly ends, update statuses."""
    async with async_session() as session:
        now = datetime.utcnow()
        result = await session.execute(
            select(Subscription)
            .options(selectinload(Subscription.user), selectinload(Subscription.server))
            .where(Subscription.status == SubStatus.ACTIVE)
            .where(Subscription.expires_at <= now)
            .order_by(Subscription.expires_at, Subscription.id)
        )
        expired_subs = result.scalars().all()

        revoked_targets: set[tuple[int, str]] = set()
        revoked_paid_users: set[int] = set()
        processed = 0

        for sub in expired_subs:
            access_ended = False

            if (
                sub.billing_mode == "balance"
                and sub.user
                and sub.user.balance_mode_enabled
                and sub.user.balance_autodebit_enabled
                and sub.user.next_daily_charge_at
                and now - timedelta(minutes=30) <= sub.user.next_daily_charge_at <= now
            ):
                continue

            if _is_demo_subscription(sub):
                # Disable (not delete) the Marzban user so that if the user later
                # purchases, _ensure_marzban_user can re-activate the same user and
                # preserve the subscription URL. Deleting the user would cause a new
                # token to be generated on recreation, breaking the installed link.
                access_ended = await _disable_single_key(
                    session,
                    sub.server_id,
                    sub.client_name,
                    revoked_targets,
                )
            elif sub.user_id not in revoked_paid_users:
                has_future_paid = await _has_future_paid_access(
                    session,
                    sub.user_id,
                    now,
                    exclude_subscription_id=sub.id,
                )
                if not has_future_paid:
                    access_ended = await _disable_paid_access(session, sub, revoked_targets)
                    revoked_paid_users.add(sub.user_id)

            # Deactivate MTProto secrets linked to this subscription
            mtproto_result = await session.execute(
                select(MTProtoAccount)
                .where(MTProtoAccount.subscription_id == sub.id)
                .where(MTProtoAccount.is_active == True)  # noqa: E712
            )
            for mtproto_acc in mtproto_result.scalars():
                try:
                    from bot.services.mtproto_manager import remove_secret
                    removed = await remove_secret(mtproto_acc.label)
                    if removed:
                        mtproto_acc.is_active = False
                        logger.info("Deactivated MTProto secret %s for user %s", mtproto_acc.label, sub.user_id)
                except Exception as e:
                    logger.error("Failed to deactivate MTProto secret %s: %s", mtproto_acc.label, e)

            sub.status = SubStatus.EXPIRED
            processed += 1

            should_notify = access_ended
            if _is_demo_subscription(sub):
                should_notify = should_notify and not await _has_future_paid_access(session, sub.user_id, now)

            if should_notify and sub.user and not await _has_subscription_notification(session, sub.id, "expired"):
                custom_bal_row = await session.get(BotSettings, "balance_reminder_text")
                custom_bal_tpl = custom_bal_row.value if (custom_bal_row and custom_bal_row.value) else ""
                user_balance = float(getattr(sub.user, "balance_rub", 0.0) or 0.0)
                sent = await notify_expired(
                    bot,
                    sub.user.telegram_id,
                    _expired_text(sub, user_balance=user_balance, custom_balance_template=custom_bal_tpl),
                    reply_markup=renew_kb(),
                )
                if sent:
                    session.add(
                        SubscriptionNotificationLog(
                            subscription_id=sub.id,
                            notification_code="expired",
                        )
                    )

            logger.info("Subscription %s expired for user %s", sub.id, sub.user_id)

        await session.commit()
        if processed:
            logger.info("Processed %s expired subscriptions", processed)


async def send_expiry_warnings(bot):
    """Send demo warning 3 hours before end and paid warnings 3/2/1 days before end."""
    async with async_session() as session:
        now = datetime.utcnow()
        latest_paid_sub_ids = await _latest_paid_subscription_ids(session, now)
        active_paid_users = set(latest_paid_sub_ids)
        custom_bal_row = await session.get(BotSettings, "balance_reminder_text")
        custom_bal_tpl = custom_bal_row.value if (custom_bal_row and custom_bal_row.value) else ""

        result = await session.execute(
            select(Subscription)
            .options(selectinload(Subscription.user), selectinload(Subscription.server))
            .where(Subscription.status == SubStatus.ACTIVE)
            .where(Subscription.expires_at > now)
            .where(Subscription.expires_at <= now + timedelta(days=3))
            .order_by(Subscription.expires_at, Subscription.id)
        )
        subs = result.scalars().all()

        sent_count = 0

        for sub in subs:
            if not sub.user:
                continue

            remaining = sub.expires_at - now
            notification_code: str | None = None
            text: str | None = None

            if _is_demo_subscription(sub):
                if sub.user_id in active_paid_users:
                    continue
                if timedelta(0) < remaining <= DEMO_WARNING_WINDOW:
                    notification_code = "demo_3h"
                    text = _demo_warning_text(sub)
            else:
                if latest_paid_sub_ids.get(sub.user_id) != sub.id:
                    continue
                user_balance = float(getattr(sub.user, "balance_rub", 0.0) or 0.0)
                for code, days, lower_bound, upper_bound in PAID_WARNING_WINDOWS:
                    if lower_bound < remaining <= upper_bound:
                        notification_code = code
                        text = _paid_warning_text(sub, days, user_balance=user_balance, custom_balance_template=custom_bal_tpl)
                        break

            if not notification_code or not text:
                continue

            if await _has_subscription_notification(session, sub.id, notification_code):
                continue

            sent = await notify_expiring(bot, sub.user.telegram_id, text, reply_markup=renew_kb())
            if not sent:
                continue

            session.add(
                SubscriptionNotificationLog(
                    subscription_id=sub.id,
                    notification_code=notification_code,
                )
            )
            sent_count += 1

        await session.commit()
        if sent_count:
            logger.info("Sent %s subscription warning notifications", sent_count)


async def send_recurring_charge_warnings(bot):
    """Warn users about upcoming recurring charges when consent is enabled."""
    if not settings.recurring_payments_enabled:
        return

    async with async_session() as session:
        now = datetime.utcnow()
        result = await session.execute(
            select(RecurringPaymentProfile, User, Subscription)
            .join(User, User.id == RecurringPaymentProfile.user_id)
            .outerjoin(Subscription, Subscription.id == RecurringPaymentProfile.subscription_id)
            .where(RecurringPaymentProfile.is_active == True)  # noqa: E712
            .where(RecurringPaymentProfile.consent_granted == True)  # noqa: E712
            .where(RecurringPaymentProfile.next_charge_at.is_not(None))
            .where(RecurringPaymentProfile.next_charge_at > now)
            .where(RecurringPaymentProfile.next_charge_at <= now + AUTOPAY_WARNING_WINDOW)
        )
        rows = result.all()

        sent_count = 0

        for profile, user, subscription in rows:
            remaining = profile.next_charge_at - now
            notification_code = None
            for code, lower, upper in AUTOPAY_WARNING_CODES:
                if lower < remaining <= upper:
                    notification_code = code
                    break
            if not notification_code:
                continue

            if await _has_recurring_notification(session, profile.id, notification_code):
                continue

            text = _recurring_warning_text(profile, subscription)
            sent = await notify_expiring(bot, user.telegram_id, text)
            if not sent:
                continue

            session.add(
                RecurringNotificationLog(
                    recurring_profile_id=profile.id,
                    notification_code=notification_code,
                )
            )
            sent_count += 1

        await session.commit()
        if sent_count:
            logger.info("Sent %s recurring charge reminders", sent_count)


async def send_followup_mailings(bot):
    """Send follow-up messages from all enabled campaigns to eligible demo users."""
    async with async_session() as session:
        campaigns = (await session.execute(
            select(FollowUpCampaign).where(FollowUpCampaign.is_enabled == True)  # noqa: E712
        )).scalars().all()

        if not campaigns:
            return

        total_sent = 0
        for campaign in campaigns:
            if not campaign.text:
                continue

            cutoff = datetime.utcnow() - timedelta(days=campaign.days_after_demo)

            demo_sub = aliased(Subscription)
            paid_sub = aliased(Subscription)

            has_paid_sub = exists(
                select(1)
                .select_from(paid_sub)
                .where(and_(paid_sub.user_id == User.id, paid_sub.billing_mode != "demo"))
            )
            already_notified = exists(
                select(1)
                .select_from(FollowUpLog)
                .where(
                    FollowUpLog.user_id == User.id,
                    FollowUpLog.campaign_id == campaign.id,
                )
            )

            result = await session.execute(
                select(User.telegram_id, User.id)
                .join(
                    demo_sub,
                    and_(
                        demo_sub.user_id == User.id,
                        demo_sub.billing_mode == "demo",
                        demo_sub.started_at <= cutoff,
                    ),
                )
                .where(~has_paid_sub)
                .where(~already_notified)
                .distinct()
            )
            targets = result.all()

            sent = 0
            from bot.handlers.mailing import build_user_kb
            reply_markup = build_user_kb(campaign.buttons_json)

            for tg_id, user_db_id in targets:
                try:
                    if campaign.media_file_id and campaign.media_type:
                        send_fn = bot.send_photo if campaign.media_type == "photo" else bot.send_video
                        await send_fn(tg_id, campaign.media_file_id, caption=campaign.text, parse_mode="HTML", reply_markup=reply_markup)
                    else:
                        await bot.send_message(tg_id, campaign.text, parse_mode="HTML", reply_markup=reply_markup)

                    async with async_session() as s2:
                        s2.add(FollowUpLog(user_id=user_db_id, campaign_id=campaign.id))
                        await s2.commit()
                    sent += 1
                except TelegramRetryAfter as exc:
                    await asyncio.sleep(exc.retry_after)
                    try:
                        if campaign.media_file_id and campaign.media_type:
                            send_fn = bot.send_photo if campaign.media_type == "photo" else bot.send_video
                            await send_fn(tg_id, campaign.media_file_id, caption=campaign.text, parse_mode="HTML", reply_markup=reply_markup)
                        else:
                            await bot.send_message(tg_id, campaign.text, parse_mode="HTML", reply_markup=reply_markup)
                        async with async_session() as s2:
                            s2.add(FollowUpLog(user_id=user_db_id, campaign_id=campaign.id))
                            await s2.commit()
                        sent += 1
                    except Exception:
                        pass
                except Exception as e:
                    logger.warning("Follow-up campaign %s send failed for %s: %s", campaign.name, tg_id, e)
                await asyncio.sleep(0.05)

            if sent:
                logger.info("Follow-up campaign '%s' sent to %s users", campaign.name, sent)
            total_sent += sent


async def process_recurring_charges(bot):
    """Attempt recurring YooKassa charges for profiles due for renewal."""
    if not settings.recurring_payments_enabled:
        logger.info("Recurring payments are disabled; skipping scheduler job.")
        return

    from bot.services.payment_service import create_recurring_yookassa_payment
    from bot.utils.texts import (
        RECURRING_CHARGE_DISABLED,
        RECURRING_CHARGE_FAILED_1,
        RECURRING_CHARGE_FAILED_2,
        RECURRING_DEACTIVATED,
        RECURRING_PROVIDER_ERROR,
        fmt_user,
    )

    async with async_session() as session:
        now = datetime.utcnow()

        result = await session.execute(
            select(RecurringPaymentProfile)
            .options(
                selectinload(RecurringPaymentProfile.user),
                selectinload(RecurringPaymentProfile.subscription),
                selectinload(RecurringPaymentProfile.tariff),
            )
            .where(RecurringPaymentProfile.is_active == True)  # noqa: E712
            .where(RecurringPaymentProfile.consent_granted == True)  # noqa: E712
            .where(RecurringPaymentProfile.provider_payment_method_id.is_not(None))
        )
        profiles = result.scalars().all()

        charged = 0
        for profile in profiles:
            user = profile.user
            tariff = profile.tariff
            sub = profile.subscription

            if not user or not tariff:
                continue

            if not sub:
                profile.is_active = False
                profile.consent_granted = False
                await session.commit()
                plog(
                    "АВТОПРОДЛ_ОТКЛ",
                    user_id=user.telegram_id,
                    причина="подписка_не_найдена",
                    tariff=tariff.label,
                )
                logger.warning(
                    "Recurring profile %s disabled before charge: subscription is missing",
                    profile.id,
                )
                continue

            if not tariff.is_active or tariff.is_admin_only:
                plog(
                    "АВТОПРОДЛ_ПРОПУСК",
                    user_id=user.telegram_id,
                    reason="тариф_неактивен",
                    tariff=tariff.label,
                    is_active=tariff.is_active,
                    is_admin_only=tariff.is_admin_only,
                )
                profile.is_active = False
                profile.consent_granted = False
                await session.commit()
                continue

            # Only charge if subscription expired or about to expire
            if sub and sub.status == SubStatus.ACTIVE and sub.expires_at > now:
                continue

            user_info = fmt_user(user.telegram_id, user.username, user.full_name)

            # Blocker 1: Do not charge again if an open paid renewal cycle is already in progress
            active_ff = await session.scalar(
                select(VhqSubscriptionFulfillment)
                .where(
                    (VhqSubscriptionFulfillment.subscription_id == sub.id)
                    | (VhqSubscriptionFulfillment.recurring_profile_id == profile.id)
                )
                .where(
                    VhqSubscriptionFulfillment.status.in_([
                        "pending",
                        "processing",
                        "retry_wait",
                        "uncertain",
                        "buy_inflight",
                        "renew_inflight",
                        "manual_review",
                    ])
                )
                .order_by(VhqSubscriptionFulfillment.id.desc())
                .limit(1)
            )
            if active_ff:
                if active_ff.status == "manual_review":
                    profile.is_active = False
                    profile.consent_granted = False
                    await session.commit()
                    logger.warning(
                        "Recurring profile %s paused: active fulfillment %s is in manual_review",
                        profile.id, active_ff.id,
                    )
                    await notify_admins_issue(
                        bot,
                        title="Автопродление приостановлено (требуется ручная проверка)",
                        details=[
                            f"Пользователь: {user_info}",
                            f"Тариф: {tariff.label}",
                            f"Fulfillment ID: {active_ff.id}",
                            f"Статус: manual_review",
                            f"Код ошибки: {active_ff.last_error_code or '—'}",
                        ],
                    )
                    continue

                has_paid_or_completed_payment = False
                if active_ff.payment_id:
                    pmt = await session.get(Payment, active_ff.payment_id)
                    if pmt and (
                        pmt.status == PaymentStatus.COMPLETED
                        or pmt.provider_paid is True
                        or pmt.provider_status in ("succeeded", "pending", "waiting_for_capture")
                    ):
                        has_paid_or_completed_payment = True

                if has_paid_or_completed_payment:
                    logger.info(
                        "Skipping recurring charge for profile %s: fulfillment %s is %s with completed payment %s",
                        profile.id, active_ff.id, active_ff.status, active_ff.payment_id,
                    )
                    continue

            operation_amount_kopecks = profile.recurring_operation_amount_kopecks
            operation_description = profile.recurring_operation_description
            operation_payment_method_id = profile.recurring_operation_payment_method_id
            if profile.recurring_operation_key is None:
                cycle_marker = profile.next_charge_at or sub.expires_at or now
                cycle_token = int(cycle_marker.timestamp())
                generation = int(profile.recurring_operation_generation or 0) + 1
                profile.recurring_operation_generation = generation
                profile.recurring_operation_key = f"recurring-profile-{profile.id}-{generation}-{cycle_token}"[:64]
                operation_amount_kopecks = rub_to_kopecks(tariff.price_rub)
                operation_description = f"Автопродление {tariff.label}"
                profile.recurring_operation_amount_kopecks = operation_amount_kopecks
                profile.recurring_operation_description = operation_description
                operation_payment_method_id = profile.provider_payment_method_id
                profile.recurring_operation_payment_method_id = operation_payment_method_id
                profile.recurring_operation_started_at = now
                await session.commit()
            else:
                if operation_amount_kopecks is None:
                    operation_amount_kopecks = rub_to_kopecks(tariff.price_rub)
                    profile.recurring_operation_amount_kopecks = operation_amount_kopecks
                if not operation_description:
                    operation_description = f"Автопродление {tariff.label}"
                    profile.recurring_operation_description = operation_description
                if not operation_payment_method_id:
                    operation_payment_method_id = profile.provider_payment_method_id
                    profile.recurring_operation_payment_method_id = operation_payment_method_id
                await session.commit()

            # Never roll an unknown POST outcome into a fresh operation after
            # YooKassa's idempotence window. Stop for operator reconciliation.
            if (
                profile.recurring_operation_started_at
                and now - profile.recurring_operation_started_at >= timedelta(hours=23)
            ):
                profile.is_active = False
                profile.consent_granted = False
                await session.commit()
                await notify_admins_issue(
                    bot,
                    title="Автопродление остановлено для проверки",
                    details=[
                        f"Профиль: {profile.id}",
                        f"Пользователь: {user.telegram_id}",
                        "Неизвестный результат операции старше 23 часов; новый ключ не создан.",
                    ],
                )
                continue

            # Check retry policy
            if not can_retry_now(
                profile.payment_attempt_count,
                profile.last_payment_attempt,
                now,
            ):
                # All attempts exhausted — disable
                if profile.payment_attempt_count >= MAX_PAYMENT_ATTEMPTS:
                    profile.is_active = False
                    profile.consent_granted = False
                    await session.commit()
                    plog("АВТОПРОДЛ_ОТКЛ", user_id=user.telegram_id,
                         причина="3_попытки", tariff=tariff.label)
                    try:
                        await bot.send_message(
                            user.telegram_id,
                            RECURRING_CHARGE_DISABLED,
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
                    for admin_id in settings.notification_recipient_ids:
                        try:
                            await bot.send_message(
                                admin_id,
                                f"🔴 Автопродление отключено (3 неудачи)\n"
                                f"Пользователь: {fmt_user(user.telegram_id, user.username, user.full_name)}\n"
                                f"Тариф: {tariff.label}",
                                parse_mode="HTML",
                            )
                        except Exception:
                            pass
                continue

            renewal_platform = encode_intent("deferred", "renew", sub.id)
            # Persist the recurring billing intent before the provider POST.
            # This gives an ambiguous create a durable local operation to
            # reconcile and guarantees retries reuse the same key and payload.
            operation_payment = await session.scalar(
                select(Payment).where(
                    Payment.provider_idempotence_key == profile.recurring_operation_key,
                    Payment.method == PaymentMethod.YOOKASSA,
                ).limit(1)
            )
            if not operation_payment:
                operation_payment = Payment(
                    user_id=user.id,
                    subscription_id=None,
                    tariff_id=tariff.id,
                    amount=operation_amount_kopecks,
                    currency="RUB",
                    method=PaymentMethod.YOOKASSA,
                    status=PaymentStatus.PENDING,
                    provider_payment_id=None,
                    provider_idempotence_key=profile.recurring_operation_key,
                    telegram_chat_id=user.telegram_id,
                    purchase_kopecks=operation_amount_kopecks,
                    discount_kopecks=0,
                    platform=renewal_platform,
                    provisioning_failure_code="recurring_create_pending",
                )
                session.add(operation_payment)
                await session.flush()
                await session.commit()

            res, payment_id, yk_status = await create_recurring_yookassa_payment(
                payment_method_id=operation_payment_method_id,
                amount_rub=kopecks_to_rub_float(operation_amount_kopecks),
                amount_kopecks=operation_amount_kopecks,
                description=operation_description,
                user_id=user.id,
                tariff_id=tariff.id,
                idempotence_key=profile.recurring_operation_key,
                telegram_user_id=user.telegram_id,
                platform=renewal_platform,
                subscription_id=sub.id,
                recurring_profile_id=profile.id,
            )

            if payment_id:
                profile.recurring_operation_provider_payment_id = payment_id
                await session.commit()

            if payment_id and res != "succeeded":
                existing_recurring_payment = await session.scalar(
                    select(Payment).where(Payment.provider_payment_id == payment_id).limit(1)
                )
                if not existing_recurring_payment:
                    existing_recurring_payment = operation_payment
                existing_recurring_payment.provider_payment_id = payment_id
                existing_recurring_payment.status = PaymentStatus.PENDING if res == "pending" else PaymentStatus.FAILED
                existing_recurring_payment.provider_status = yk_status or res
                existing_recurring_payment.provider_paid = False
                existing_recurring_payment.provider_amount_kopecks = operation_amount_kopecks
                existing_recurring_payment.provider_currency = "RUB"
                existing_recurring_payment.provider_verified_at = now
                existing_recurring_payment.provisioning_failure_code = (
                    "recurring_provider_pending" if res == "pending" else "recurring_provider_failed"
                )
                await session.commit()

            payment_label = profile.payment_method_label or profile.provider

            if res == "succeeded":
                # Ensure durable Payment entity exists BEFORE provisioning
                if not payment_id:
                    operation_payment.provider_status = yk_status or "succeeded"
                    operation_payment.provider_paid = True
                    operation_payment.provider_amount_kopecks = operation_amount_kopecks
                    operation_payment.provider_currency = "RUB"
                    operation_payment.provider_verified_at = now
                    operation_payment.provisioning_failure_code = "recurring_missing_provider_id"
                    await session.commit()
                    logger.error(
                        "Recurring YooKassa create returned succeeded without a provider payment ID: profile=%s",
                        profile.id,
                    )
                    continue

                payment = await session.scalar(
                    select(Payment).where(Payment.provider_payment_id == payment_id).limit(1)
                )
                if not payment:
                    payment = operation_payment
                    payment.provider_payment_id = payment_id
                else:
                    payment.status = PaymentStatus.PENDING
                    payment.provider_idempotence_key = payment.provider_idempotence_key or profile.recurring_operation_key
                    payment.amount = operation_amount_kopecks
                    payment.platform = payment.platform or renewal_platform

                payment.status = PaymentStatus.PENDING
                payment.amount = operation_amount_kopecks
                payment.platform = payment.platform or renewal_platform
                payment.provider_status = yk_status or "succeeded"
                payment.provider_paid = True
                payment.provider_amount_kopecks = operation_amount_kopecks
                payment.provider_currency = "RUB"
                payment.provider_verified_at = now

                payment.provisioning_failure_code = "recurring_fulfillment_pending"

                # Provider success is durable before any supplier mutation or
                # fulfillment validation begins.
                await session.commit()

                # Route every recurring supplier operation through the same
                # authenticated verification and payment lease used by
                # webhook/reconciliation workers.  The provider-success
                # snapshot above is durable before this call; this pipeline is
                # the only owner allowed to mutate the supplier.
                from bot.services.yookassa_reconciliation import verify_and_fulfill_yookassa_payment

                fulfilled, fulfillment_code = await verify_and_fulfill_yookassa_payment(
                    bot,
                    payment_id,
                    source="recurring_scheduler",
                )
                if not fulfilled:
                    logger.info(
                        "Recurring payment %s remains unresolved after canonical fulfillment: %s",
                        payment_id,
                        fulfillment_code,
                    )
                    continue

                # VHQ background fulfillment returns success only for the
                # durable pending state; its worker owns final completion.
                if fulfillment_code == "supplier_fulfillment_pending":
                    continue

                await session.refresh(payment)
                await session.refresh(profile)
                if payment.status != PaymentStatus.COMPLETED:
                    continue

                charged += 1
                logger.info(
                    "Recurring charge fulfilled through canonical payment pipeline: user=%s tariff=%s payment=%s",
                    user.telegram_id,
                    tariff.label,
                    payment_id,
                )

            elif res == "deactivate":
                profile.is_active = False
                profile.consent_granted = False
                profile.recurring_operation_key = None
                profile.recurring_operation_amount_kopecks = None
                profile.recurring_operation_description = None
                profile.recurring_operation_payment_method_id = None
                profile.recurring_operation_provider_payment_id = None
                profile.recurring_operation_started_at = None
                await session.commit()

                try:
                    await bot.send_message(
                        user.telegram_id,
                        RECURRING_DEACTIVATED,
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
                for admin_id in settings.notification_recipient_ids:
                    try:
                        await bot.send_message(
                            admin_id,
                            f"🚫 Автопродление отключено (отказ провайдера)\n"
                            f"Пользователь: {user_info}\nПровайдер: YooKassa",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass

                plog("АВТОПРОДЛ_ОТКЛ", provider="Yookassa",
                     user_id=user.telegram_id, причина="отказ_провайдера")
                logger.warning(
                    "Recurring deactivated (payment method gone): user=%s",
                    user.telegram_id,
                )

            elif res == "provider_error":
                # Don't count as attempt — provider issue
                profile.last_payment_attempt = now
                await session.commit()

                next_retry = get_next_retry_at(
                    profile.payment_attempt_count, profile.last_payment_attempt
                )
                next_retry_str = _format_date(next_retry) if next_retry else "ближайшее время"
                try:
                    await bot.send_message(
                        user.telegram_id,
                        RECURRING_PROVIDER_ERROR.format(next_retry=next_retry_str),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

                plog("ОШИБКА_ПРОВАЙДЕРА", provider="Yookassa",
                     user_id=user.telegram_id, tariff=tariff.label)
                logger.warning(
                    "Recurring provider error (not counted): user=%s",
                    user.telegram_id,
                )

            else:
                # failed / pending — count as attempt
                attempt_num = profile.payment_attempt_count + 1
                profile.payment_attempt_count = attempt_num
                profile.last_payment_attempt = now
                if res == "failed":
                    profile.recurring_operation_key = None
                    profile.recurring_operation_amount_kopecks = None
                    profile.recurring_operation_description = None
                    profile.recurring_operation_payment_method_id = None
                    profile.recurring_operation_provider_payment_id = None
                    profile.recurring_operation_started_at = None
                plog("ОШИБКА_СПИСАНИЯ", provider="Yookassa",
                     user_id=user.telegram_id, попытка=f"{attempt_num}/{MAX_PAYMENT_ATTEMPTS}",
                     tariff=tariff.label, amount=f"{tariff.price_rub:.2f}")

                if attempt_num >= MAX_PAYMENT_ATTEMPTS:
                    profile.is_active = False
                    profile.consent_granted = False
                    await session.commit()
                    try:
                        await bot.send_message(
                            user.telegram_id,
                            RECURRING_CHARGE_DISABLED,
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
                    for admin_id in settings.notification_recipient_ids:
                        try:
                            await bot.send_message(
                                admin_id,
                                f"🔴 Автопродление отключено (3 неудачи)\n"
                                f"Пользователь: {user_info}\nТариф: {tariff.label}",
                                parse_mode="HTML",
                            )
                        except Exception:
                            pass
                else:
                    await session.commit()
                    next_retry = get_next_retry_at(
                        profile.payment_attempt_count, profile.last_payment_attempt
                    )
                    next_retry_str = _format_date(next_retry) if next_retry else "ближайшее время"

                    if attempt_num == 1:
                        msg = RECURRING_CHARGE_FAILED_1.format(
                            payment_method=payment_label,
                            next_retry=next_retry_str,
                        )
                    else:
                        msg = RECURRING_CHARGE_FAILED_2.format(
                            payment_method=payment_label,
                            next_retry=next_retry_str,
                        )

                    try:
                        await bot.send_message(
                            user.telegram_id, msg, parse_mode="HTML",
                        )
                    except Exception:
                        pass

                    for admin_id in settings.notification_recipient_ids:
                        try:
                            await bot.send_message(
                                admin_id,
                                f"⚠️ Ошибка автосписания [{attempt_num}/{MAX_PAYMENT_ATTEMPTS}]\n"
                                f"Пользователь: {user_info}\n"
                                f"Тариф: {tariff.label}\nСумма: {tariff.price_rub}₽\n"
                                f"Провайдер: YooKassa",
                                parse_mode="HTML",
                            )
                        except Exception:
                            pass

                logger.warning(
                    "Recurring charge failed: user=%s, attempt=%s/%s, payment=%s",
                    user.telegram_id, attempt_num, MAX_PAYMENT_ATTEMPTS, payment_id,
                )

        if charged:
            logger.info("Processed %s successful recurring charges", charged)


async def check_vhq_balance(bot) -> None:
    """Check VHQ partner balance and notify admins when it gets low."""
    if not (settings.webstore_public_enabled and settings.vhq_partner_api_key.strip()):
        return

    try:
        data = await VHQPartnerAPI(
            api_key=settings.vhq_partner_api_key,
            base_url=settings.vhq_partner_api_url,
        ).get_balance()
        balance = float(data.get("balance") or 0)
        currency = str(data.get("currency") or "RUB")
        plog("VHQ_БАЛАНС", provider="VHQ", balance=balance, currency=currency)
        logger.info("VHQ balance check complete: balance=%s currency=%s", balance, currency)
    except VHQPartnerAPIError as exc:
        logger.error("VHQ balance check failed: status=%s error=%s", exc.status, exc)
        await notify_admins_issue(
            bot,
            title="Ошибка проверки баланса VHQ",
            details=[
                f"HTTP статус: {exc.status or '—'}",
                f"Причина: {exc}",
            ],
        )
        return
    except Exception as exc:
        logger.error("Unexpected VHQ balance check failure: %s", exc)
        await notify_admins_issue(
            bot,
            title="Ошибка проверки баланса VHQ",
            details=[f"Причина: {exc}"],
        )
        return

    if balance < 200:
        await notify_admins_issue(
            bot,
            title="Низкий баланс VHQ",
            details=[
                f"Текущий баланс: {balance:.0f} {currency}",
                "Нужно пополнить баланс VHQ, иначе новые VHQ-тарифы не будут выдаваться.",
            ],
        )


async def check_adapt_balance(bot) -> None:
    """Check Adapt Group partner balance and notify admins when low or on error."""
    api = AdaptAPI()
    if not api.enabled:
        return

    try:
        data = await api.get_balance()
        balance = float(data.get("balance") or 0)
        currency = str(data.get("currency") or "USD")
        plog("ADAPT_БАЛАНС", provider="Adapt", balance=balance, currency=currency)
        logger.info("Adapt balance check complete: balance=%s currency=%s", balance, currency)
    except AdaptAPIError as exc:
        logger.error("Adapt balance check failed: status=%s error=%s", exc.status, exc)
        await notify_admins_issue(
            bot,
            title="Ошибка проверки баланса Adapt",
            details=[
                f"HTTP статус: {exc.status or '—'}",
                f"Причина: {exc}",
            ],
        )
        return
    except Exception as exc:
        logger.error("Unexpected Adapt balance check failure: %s", exc)
        await notify_admins_issue(
            bot,
            title="Ошибка проверки баланса Adapt",
            details=[f"Причина: {exc}"],
        )
        return

    if balance < settings.adapt_min_balance:
        await notify_admins_issue(
            bot,
            title="Низкий баланс Adapt",
            details=[
                f"Текущий баланс: {balance:.2f} {currency}",
                "Нужно пополнить баланс Adapt, иначе новые подписки Adapt не смогут выдаваться.",
            ],
        )


async def check_webstore_health(bot) -> None:
    """Check public webstore availability from the bot process and alert on transitions."""
    base_url = settings.webstore_api_base_url.strip().rstrip("/")
    if not (settings.webstore_public_enabled and base_url):
        return

    health_url = f"{base_url}/buy"
    now = datetime.utcnow()
    ok = False
    reason = ""

    max_attempts = 3
    timeout = aiohttp.ClientTimeout(total=15, connect=5, sock_read=10)

    async with aiohttp.ClientSession(timeout=timeout) as http:
        for attempt in range(1, max_attempts + 1):
            try:
                async with http.get(health_url, allow_redirects=True) as resp:
                    if 200 <= resp.status < 400:
                        ok = True
                        reason = ""
                        break
                    else:
                        reason = f"HTTP {resp.status}"
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"

            if attempt < max_attempts:
                await asyncio.sleep(2)

    async with async_session() as session:
        key = "webstore_health_status"
        row = await session.get(BotSettings, key)
        previous = row.value if row else "unknown"
        current = "ok" if ok else "down"
        if row:
            row.value = current
        else:
            session.add(BotSettings(key=key, value=current))

        last_reason_key = "webstore_health_last_reason"
        reason_row = await session.get(BotSettings, last_reason_key)
        if reason_row:
            reason_row.value = reason[:500]
        else:
            session.add(BotSettings(key=last_reason_key, value=reason[:500]))

        await session.commit()

    if ok and previous == "down":
        await notify_admins_issue(
            bot,
            title="Webstore восстановился",
            details=[
                f"URL: {health_url}",
                f"Время UTC: {now.isoformat(timespec='seconds')}",
            ],
        )
    elif not ok and previous != "down":
        await notify_admins_issue(
            bot,
            title="Webstore недоступен",
            details=[
                f"URL: {health_url}",
                f"Причина: {reason or 'unknown'}",
                f"Попыток: {max_attempts}",
                f"Время UTC: {now.isoformat(timespec='seconds')}",
            ],
        )


async def process_vhq_subscription_fulfillments(bot) -> None:
    """Background worker processing pending, retry_wait, uncertain VHQ fulfillments and re-claiming stale processing leases."""
    now = datetime.utcnow()

    async with async_session() as session:
        query = (
            select(VhqSubscriptionFulfillment.id)
            .where(
                or_(
                    and_(
                        VhqSubscriptionFulfillment.status.in_(("pending", "retry_wait", "uncertain")),
                        or_(
                            VhqSubscriptionFulfillment.next_attempt_at.is_(None),
                            VhqSubscriptionFulfillment.next_attempt_at <= now,
                        ),
                    ),
                    and_(
                        VhqSubscriptionFulfillment.status == "processing",
                        or_(
                            VhqSubscriptionFulfillment.lease_expires_at.is_(None),
                            VhqSubscriptionFulfillment.lease_expires_at <= now,
                        ),
                    ),
                    and_(
                        VhqSubscriptionFulfillment.status.in_(("buy_inflight", "renew_inflight")),
                        or_(
                            VhqSubscriptionFulfillment.lease_expires_at.is_(None),
                            VhqSubscriptionFulfillment.lease_expires_at <= now,
                        ),
                    ),
                )
            )
            .order_by(VhqSubscriptionFulfillment.id.asc())
            .limit(10)
        )
        candidate_ids = (await session.scalars(query)).all()

    for cid in candidate_ids:
        has_completed_oneoff_payment = False
        async with async_session() as session:
            ff_quick = await session.get(VhqSubscriptionFulfillment, cid)
            if not ff_quick:
                continue

            # Crash safety: detect stale buy_inflight after crash and never reissue buy!
            if ff_quick.status == "buy_inflight":
                if ff_quick.lease_expires_at and ff_quick.lease_expires_at > datetime.utcnow():
                    logger.info(
                        "Fulfillment %s has active buy_inflight lease until %s. Leaving untouched.",
                        cid,
                        ff_quick.lease_expires_at,
                    )
                    continue
                ff_quick.status = "manual_review"
                ff_quick.lease_token = None
                ff_quick.lease_expires_at = None
                ff_quick.last_error_code = "stale_buy_inflight"
                ff_quick.last_error_message = (
                    "Crash recovery: stale buy_inflight detected. Duplicate buy prohibited."
                )
                if ff_quick.manual_operation_id:
                    manual_op = await session.get(ManualSubscriptionOperation, ff_quick.manual_operation_id)
                    if manual_op:
                        manual_op.status = "manual_review"
                await session.commit()
                logger.critical("Fulfillment %s had stale buy_inflight. Moved to manual_review.", cid)
                await notify_admins_issue(
                    bot,
                    title="VHQ Fallback Buy: обнаружен stale buy_inflight (manual_review)",
                    details=[
                        f"Fulfillment ID: {cid}",
                        f"Subscription ID: {ff_quick.subscription_id}",
                        f"Ошибка: stale buy_inflight после сбоя процесса",
                    ],
                )
                continue

            if ff_quick.status == "renew_inflight":
                if ff_quick.lease_expires_at and ff_quick.lease_expires_at > datetime.utcnow():
                    logger.info(
                        "Fulfillment %s has active renew_inflight lease until %s. Leaving untouched.",
                        cid,
                        ff_quick.lease_expires_at,
                    )
                    continue

            if not await _claim_vhq_fulfillment_for_execution(session, ff_quick):
                continue

            fulfillment = ff_quick
            token = fulfillment.lease_token
            if not fulfillment or not token:
                continue

            try:
                await execute_vhq_fulfillment(
                    session,
                    fulfillment,
                    lease_token=token,
                )
                if fulfillment.status == "completed":
                    # 1. Update manual operation if linked
                    if fulfillment.manual_operation_id:
                        manual_op = await session.get(ManualSubscriptionOperation, fulfillment.manual_operation_id)
                        if manual_op:
                            manual_op.status = "completed"
                            manual_op.completed_at = datetime.utcnow()
                            if (
                                fulfillment.baseline_local_expires_at
                                and fulfillment.baseline_local_expires_at <= datetime.utcnow()
                            ):
                                target_sub = await session.get(Subscription, fulfillment.subscription_id)
                                target_user = await session.get(User, fulfillment.user_id)
                                if target_sub and target_user and bot:
                                    try:
                                        from bot.utils.texts import MANUAL_KEY_DELIVERED
                                        from bot.services.vhq_subscription_proxy import build_vhq_subscription_ref_url
                                        exp_str = _format_date(target_sub.expires_at)
                                        customer_url = build_vhq_subscription_ref_url(target_sub.id) or target_sub.vpn_key
                                        key_display = customer_url if len(customer_url) <= 200 else customer_url[:200] + "..."
                                        await bot.send_message(
                                            target_user.telegram_id,
                                            MANUAL_KEY_DELIVERED.format(key=key_display, expires=exp_str),
                                            parse_mode="HTML",
                                        )
                                    except Exception as exc:
                                        logger.warning("Failed to send background manual renew notification: %s", exc)

                    # 2. Finalize payment if linked
                    if fulfillment.payment_id:
                        payment = await session.get(Payment, fulfillment.payment_id)
                        if payment:
                            payment.subscription_id = fulfillment.subscription_id
                            # Provider truth is already durable before this
                            # worker runs.  Business completion is only
                            # terminal after the fulfillment reached
                            # ``completed``.
                            payment.status = PaymentStatus.COMPLETED
                            payment.provisioning_failure_code = (
                                None
                                if fulfillment.recurring_profile_id
                                else "financial_finalization_pending"
                            )
                        if not fulfillment.recurring_profile_id:
                            has_completed_oneoff_payment = True

                    # 3. Finalize recurring payment profile ONLY if fulfillment has a linked recurring_profile_id
                    if fulfillment.recurring_profile_id:
                        rec_profile = await session.get(RecurringPaymentProfile, fulfillment.recurring_profile_id)
                        if rec_profile and fulfillment.target_local_expires_at:
                            rec_profile.payment_attempt_count = 0
                            rec_profile.last_payment_attempt = None
                            rec_profile.next_charge_at = fulfillment.target_local_expires_at
                            rec_profile.last_charge_at = datetime.utcnow()
                            rec_profile.recurring_operation_key = None
                            rec_profile.recurring_operation_amount_kopecks = None
                            rec_profile.recurring_operation_description = None
                            rec_profile.recurring_operation_payment_method_id = None
                            rec_profile.recurring_operation_provider_payment_id = None
                            rec_profile.recurring_operation_started_at = None
                            rec_user = await session.get(User, fulfillment.user_id)
                            if rec_user and bot:
                                try:
                                    from bot.utils.texts import RECURRING_CHARGE_SUCCESS
                                    exp_str = _format_date(fulfillment.target_local_expires_at)
                                    payment_label = rec_profile.payment_method_label or rec_profile.provider
                                    rec_tariff = await session.get(Tariff, rec_profile.tariff_id) if rec_profile.tariff_id else None
                                    amount_val = rec_tariff.price_rub if rec_tariff else 0
                                    await bot.send_message(
                                        rec_user.telegram_id,
                                        RECURRING_CHARGE_SUCCESS.format(
                                            expires=exp_str,
                                            payment_method=payment_label,
                                            amount=amount_val,
                                        ),
                                        parse_mode="HTML",
                                    )
                                except Exception as exc:
                                    logger.warning("Failed to send recurring success after worker fulfillment: %s", exc)
                elif fulfillment.status in ("manual_review", "failed"):
                    if fulfillment.manual_operation_id:
                        manual_op = await session.get(ManualSubscriptionOperation, fulfillment.manual_operation_id)
                        if manual_op:
                            manual_op.status = fulfillment.status
            except Exception as exc:
                logger.exception("Error executing VHQ fulfillment %s: %s", cid, exc)
                fulfillment.status = "uncertain"
                fulfillment.next_attempt_at = datetime.utcnow() + timedelta(seconds=30)
                fulfillment.last_error_code = "worker_exception"
                fulfillment.last_error_message = str(exc)
            finally:
                if fulfillment.status != "processing":
                    fulfillment.lease_token = None
                    fulfillment.lease_expires_at = None
                await session.commit()

            if fulfillment.status == "manual_review":
                try:
                    await notify_admins_issue(
                        bot,
                        title="Требуется ручная проверка VHQ продления",
                        details=[
                            f"Fulfillment ID: {fulfillment.id}",
                            f"Subscription ID: {fulfillment.subscription_id}",
                            f"Action: {fulfillment.action}",
                            f"Source: {fulfillment.source_type}:{fulfillment.source_id}",
                            f"Код ошибки: {fulfillment.last_error_code or '—'}",
                            f"Ошибка: {fulfillment.last_error_message or '—'}",
                        ],
                    )
                except Exception as exc:
                    logger.warning("Failed to notify admins of VHQ manual_review: %s", exc)

            # Trigger canonical payment finalizer once for completed one-off payments
            if has_completed_oneoff_payment and bot:
                await auto_retry_failed_provisionings(bot)

