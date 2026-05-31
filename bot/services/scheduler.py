"""Subscription scheduler - warnings, expiry handling, follow-ups, recurring reminders."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import aiohttp
from aiogram.exceptions import TelegramRetryAfter
from sqlalchemy import and_, exists, select
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
)
from bot.config import settings
from bot.services.backup_service import create_and_send_backup
from bot.services import vpn_manager
from bot.keyboards.client import renew_kb
from bot.services.balance_service import credit_user_balance, debit_user_balance, get_daily_charge_rub, next_charge_datetime
from bot.services.notifications import notify_admins_issue, notify_expired, notify_expiring
from bot.services.recurring_retry import (
    MAX_PAYMENT_ATTEMPTS,
    can_retry_now,
    get_next_retry_at,
)
from bot.services.payment_logger import plog
from bot.services.provisioning_issues import AccessProvisionError
from bot.services.subscription_semantics import is_demo_subscription_row, paid_access_clause
from bot.services.subscription_service import create_or_extend_balance_subscription, create_or_extend_balance_adapt_subscription
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
    if settings.webstore_public_enabled and settings.vhq_partner_api_key.strip():
        scheduler.add_job(
            check_vhq_balance,
            "interval",
            hours=12,
            args=[bot],
            id="check_vhq_balance",
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


async def check_payment_integrity(bot) -> None:
    """Alert admins if a completed non-topup payment is not linked to access."""
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
            amount_rub = payment.amount / 100.0 if payment.currency == "RUB" else float(payment.amount)
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


def _paid_warning_text(sub: Subscription, days: int) -> str:
    day_word = "день" if days == 1 else ("дня" if days in (2, 3, 4) else "дней")
    return (
        f"⚠️ <b>Подписка скоро закончится</b>\n\n"
        f"Доступ перестанет работать через <b>{days} {day_word}</b>.\n"
        f"Дата окончания: <b>{_format_date(sub.expires_at)}</b>\n\n"
        f"Чтобы не потерять доступ, продлите подписку заранее."
    )


def _demo_warning_text(sub: Subscription) -> str:
    return (
        f"⏳ <b>Демо-доступ скоро закончится</b>\n\n"
        f"До окончания осталось меньше <b>3 часов</b>.\n"
        f"Дата окончания: <b>{_format_date(sub.expires_at)}</b>\n\n"
        f"Если хотите сохранить доступ, оформите тариф."
    )


def _expired_text(sub: Subscription) -> str:
    if _is_demo_subscription(sub):
        return (
            f"🔴 <b>Демо-доступ завершен</b>\n\n"
            f"Демо-ключ отключен {_format_date(sub.expires_at)}.\n"
            f"Чтобы снова получить доступ, оформите платный тариф."
        )

    return (
        f"🔴 <b>Подписка завершена</b>\n\n"
        f"Ключ отключен {_format_date(sub.expires_at)}.\n"
        f"Чтобы восстановить доступ, продлите подписку."
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
                sent = await notify_expired(bot, sub.user.telegram_id, _expired_text(sub), reply_markup=renew_kb())
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
                for code, days, lower_bound, upper_bound in PAID_WARNING_WINDOWS:
                    if lower_bound < remaining <= upper_bound:
                        notification_code = code
                        text = _paid_warning_text(sub, days)
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
                .where(and_(paid_sub.user_id == User.id, paid_sub.client_name.notlike("%_demo")))
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
                        demo_sub.client_name.like("%_demo"),
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
    import hashlib

    from bot.services.payment_service import create_recurring_yookassa_payment
    from bot.services.subscription_service import create_or_extend_paid_access
    from bot.utils.texts import (
        RECURRING_CHARGE_DISABLED,
        RECURRING_CHARGE_FAILED_1,
        RECURRING_CHARGE_FAILED_2,
        RECURRING_CHARGE_SUCCESS,
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

            # Only charge if subscription expired or about to expire
            if sub and sub.status == SubStatus.ACTIVE and sub.expires_at > now:
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
                    for admin_id in settings.admin_ids:
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

            # Build idempotence key
            idempotence_key = hashlib.md5(
                f"recurring:{user.id}:{profile.id}:{tariff.id}:"
                f"{tariff.price_rub:.2f}:{profile.payment_attempt_count}:"
                f"{(profile.last_payment_attempt or now).isoformat()}".encode()
            ).hexdigest()

            res, payment_id, yk_status = await create_recurring_yookassa_payment(
                payment_method_id=profile.provider_payment_method_id,
                amount_rub=float(tariff.price_rub),
                description=f"Автопродление {tariff.label}",
                user_id=user.id,
                tariff_id=tariff.id,
                idempotence_key=idempotence_key,
            )

            payment_label = profile.payment_method_label or profile.provider
            user_info = fmt_user(user.telegram_id, user.username, user.full_name)

            if res == "succeeded":
                # Extend subscription
                try:
                    new_sub, vpn_key = await create_or_extend_paid_access(
                        session,
                        user=user,
                        tariff=tariff,
                        platform=sub.platform if sub else Platform.ANDROID,
                    )
                except AccessProvisionError as issue:
                    profile.is_active = False
                    session.add(Payment(
                        user_id=user.id,
                        subscription_id=sub.id if sub else None,
                        amount=tariff.price_rub * 100,
                        currency="RUB",
                        method=PaymentMethod.YOOKASSA,
                        status=PaymentStatus.COMPLETED,
                        provider_payment_id=payment_id,
                        telegram_chat_id=user.telegram_id,
                    ))
                    await session.commit()
                    plog(
                        "ОШИБКА_ВЫДАЧИ",
                        provider=issue.provider.upper(),
                        user_id=user.telegram_id,
                        tariff=tariff.label,
                        method="autopay",
                        code=issue.code,
                        status=issue.status or "",
                        PayId=payment_id or "",
                    )
                    try:
                        support_line = (
                            f"\nЕсли вопрос срочный, напишите в поддержку {settings.support_username}."
                            if settings.support_username else ""
                        )
                        await bot.send_message(
                            user.telegram_id,
                            f"❌ {issue.client_message}\nАвтопродление временно приостановлено.{support_line}",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
                    await notify_admins_issue(
                        bot,
                        title="Ошибка автопродления после успешного списания",
                        details=[
                            f"Пользователь: {user_info}",
                            f"Тариф: {tariff.label}",
                            f"Сумма: {tariff.price_rub}₽",
                            f"Провайдер: {issue.provider}",
                            f"Код: {issue.code}",
                            f"HTTP статус: {issue.status or '—'}",
                            f"PayId: {payment_id or '—'}",
                            f"Причина: {issue.admin_message}",
                        ],
                    )
                    logger.error(
                        "Recurring payment charged but delivery failed: user=%s tariff=%s payment=%s issue=%s",
                        user.telegram_id,
                        tariff.label,
                        payment_id,
                        issue.admin_message,
                    )
                    continue
                profile.payment_attempt_count = 0
                profile.last_payment_attempt = None
                profile.last_charge_at = now
                if new_sub:
                    profile.subscription_id = new_sub.id
                    profile.next_charge_at = new_sub.expires_at

                    # Record payment
                    session.add(Payment(
                        user_id=user.id,
                        subscription_id=new_sub.id,
                        amount=tariff.price_rub * 100,
                        currency="RUB",
                        method=PaymentMethod.YOOKASSA,
                        status=PaymentStatus.COMPLETED,
                        provider_payment_id=payment_id,
                        telegram_chat_id=user.telegram_id,
                    ))

                await session.commit()
                charged += 1

                expires_str = _format_date(new_sub.expires_at) if new_sub else "—"
                try:
                    await bot.send_message(
                        user.telegram_id,
                        RECURRING_CHARGE_SUCCESS.format(
                            expires=expires_str,
                            payment_method=payment_label,
                            amount=tariff.price_rub,
                        ),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

                for admin_id in settings.admin_ids:
                    try:
                        await bot.send_message(
                            admin_id,
                            f"🔔 Автопродление (YooKassa)\n"
                            f"Пользователь: {user_info}\n"
                            f"Тариф: {tariff.label}\n"
                            f"Сумма: {tariff.price_rub}₽\n"
                            f"До: {expires_str}",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass

                plog("ПРОДЛЕНИЕ", provider="Yookassa", user_id=user.telegram_id,
                     tariff=tariff.label, amount=f"{tariff.price_rub:.2f}",
                     PayId=payment_id or "")
                logger.info(
                    "Recurring charge succeeded: user=%s, tariff=%s, payment=%s",
                    user.telegram_id, tariff.label, payment_id,
                )

            elif res == "deactivate":
                profile.is_active = False
                profile.consent_granted = False
                await session.commit()

                try:
                    await bot.send_message(
                        user.telegram_id,
                        RECURRING_DEACTIVATED,
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
                for admin_id in settings.admin_ids:
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
                    for admin_id in settings.admin_ids:
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

                    for admin_id in settings.admin_ids:
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


async def check_webstore_health(bot) -> None:
    """Check public webstore availability from the bot process and alert on transitions."""
    base_url = settings.webstore_api_base_url.strip().rstrip("/")
    if not (settings.webstore_public_enabled and base_url):
        return

    health_url = f"{base_url}/buy"
    now = datetime.utcnow()
    ok = False
    reason = ""

    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.get(health_url, allow_redirects=True) as resp:
                if 200 <= resp.status < 400:
                    ok = True
                else:
                    reason = f"HTTP {resp.status}"
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"

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
                f"Время UTC: {now.isoformat(timespec='seconds')}",
            ],
        )
