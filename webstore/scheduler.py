"""Background jobs for web-only balance mode."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import and_, nulls_first, or_, select

from webstore.database import async_session
from webstore.models import WebBalanceAccount, WebBalanceTopUp, WebBalanceTransfer, WebOrder, WebProfileLink
from webstore.routes import (
    _acquire_profile_lock,
    _assert_profile_lock,
    _apply_order_bonus_spend,
    _refund_order_reserved_bonus_if_needed,
    _finalize_order_cashback,
    _build_local_balance_status,
    _disable_web_balance_autodebit_after_tariff_purchase,
    _ensure_web_access_until,
    _fetch_web_balance_history,
    _get_latest_web_access_order,
    _get_or_create_web_balance_account,
    _get_web_daily_charge_rub,
    _migrate_web_balance_to_telegram,
    _next_charge_datetime,
    _notify_admins_webstore,
    _notify_bot_order_delivered,
    _notify_linked_webstore_user,
    _sync_referral_credit_for_order,
    _serialize_web_balance_history,
    _add_web_balance_transaction,
    attempt_fulfill_order,
    reconcile_yookassa_web_order,
    reconcile_yookassa_web_topup,
)
from webstore.notification_outbox import process_notification_outbox

logger = logging.getLogger(__name__)

scheduler: AsyncIOScheduler | None = None


async def process_web_balance_daily_charges() -> None:
    daily_rate = await _get_web_daily_charge_rub()
    async with async_session() as session:
        profiles = (await session.execute(
            select(WebBalanceAccount.profile_token)
            .where(WebBalanceAccount.balance_mode_enabled == 1)
            .where(WebBalanceAccount.balance_autodebit_enabled == 1)
            .where(WebBalanceAccount.next_daily_charge_at.is_not(None))
            .where(WebBalanceAccount.next_daily_charge_at <= datetime.utcnow())
        )).scalars().all()

    for profile_token in profiles:
        async with _acquire_profile_lock(profile_token):
            async with async_session() as session:
                async with session.begin():
                    now = datetime.utcnow()
                    account = await session.scalar(
                        select(WebBalanceAccount)
                        .where(WebBalanceAccount.profile_token == profile_token)
                        .where(WebBalanceAccount.balance_mode_enabled == 1)
                        .where(WebBalanceAccount.balance_autodebit_enabled == 1)
                        .where(WebBalanceAccount.next_daily_charge_at.is_not(None))
                        .where(WebBalanceAccount.next_daily_charge_at <= now)
                    )
                    if not account:
                        continue
                    _assert_profile_lock(profile_token)
                    current_balance = round(float(account.balance_rub or 0), 2)

                    if current_balance < daily_rate and account.balance_grace_until and account.balance_grace_until <= now:
                        account.balance_mode_enabled = 0
                        account.balance_autodebit_enabled = 0
                        account.balance_grace_until = None
                        account.next_daily_charge_at = None
                        account.balance_warning_for_charge_at = None
                        account.updated_at = now
                        continue

                    order = await _get_latest_web_access_order(session, account.profile_token)
                    if not order:
                        account.balance_mode_enabled = 0
                        account.balance_autodebit_enabled = 0
                        account.balance_grace_until = None
                        account.next_daily_charge_at = None
                        account.balance_warning_for_charge_at = None
                        account.updated_at = now
                        continue

                    charge_at = account.next_daily_charge_at or now
                    next_charge_at = _next_charge_datetime(charge_at + timedelta(minutes=1))
                    charge_amount = int(round(daily_rate))
                    _add_web_balance_transaction(
                        session,
                        account,
                        amount_rub=charge_amount,
                        direction="debit",
                        kind="daily_charge",
                        description="Ежедневное списание",
                        source_id=charge_at.isoformat(),
                    )
                    ok = await _ensure_web_access_until(order, next_charge_at)
                    _assert_profile_lock(profile_token)
                    if not ok:
                        _add_web_balance_transaction(
                            session,
                            account,
                            amount_rub=charge_amount,
                            direction="credit",
                            kind="refund",
                            description="Возврат: не удалось продлить доступ",
                            source_id=charge_at.isoformat(),
                        )
                        account.balance_mode_enabled = 0
                        account.balance_autodebit_enabled = 0
                        account.balance_grace_until = None
                        account.next_daily_charge_at = None
                        account.balance_warning_for_charge_at = None
                        account.updated_at = now
                        continue

                    account.last_daily_charge_at = now
                    account.next_daily_charge_at = next_charge_at
                    account.balance_warning_for_charge_at = None
                    account.updated_at = now

                    if round(float(account.balance_rub or 0), 2) < 0:
                        if not account.balance_grace_until:
                            account.balance_grace_until = account.next_daily_charge_at
                    else:
                        account.balance_grace_until = None

                    _assert_profile_lock(profile_token)


async def warn_web_balance_due_soon() -> None:
    now = datetime.utcnow()
    warning_window_end = now + timedelta(hours=24)

    async with async_session() as session:
        daily_rate = await _get_web_daily_charge_rub()
        result = await session.execute(
            select(WebBalanceAccount)
            .where(WebBalanceAccount.balance_mode_enabled == 1)
            .where(WebBalanceAccount.balance_autodebit_enabled == 1)
            .where(WebBalanceAccount.next_daily_charge_at.is_not(None))
            .where(WebBalanceAccount.next_daily_charge_at > now)
            .where(WebBalanceAccount.next_daily_charge_at <= warning_window_end)
        )
        accounts = result.scalars().all()
        changed = False
        for account in accounts:
            next_charge_at = account.next_daily_charge_at
            if not next_charge_at:
                continue
            if account.balance_warning_for_charge_at == next_charge_at:
                continue
            if round(float(account.balance_rub or 0), 2) >= daily_rate:
                continue
            account.balance_warning_for_charge_at = next_charge_at
            account.updated_at = now
            changed = True
        if changed:
            await session.commit()


async def reconcile_pending_yookassa_orders() -> None:
    """Recover web orders whose YooKassa success webhook was lost."""
    async with async_session() as session:
        rows = (
            await session.execute(
                select(WebOrder.order_id, WebOrder.yookassa_payment_id)
                .where(
                    or_(
                        WebOrder.status == "pending",
                        and_(
                            WebOrder.status == "canceled",
                            WebOrder.cancellation_confirmed.is_(False),
                        ),
                    )
                )
                .where(WebOrder.yookassa_payment_id.is_not(None))
                .order_by(
                    nulls_first(WebOrder.last_reconciled_at.asc()),
                    WebOrder.created_at.desc(),
                )
                .limit(50)
            )
        ).all()

    for order_id, payment_id in rows:
        if not payment_id:
            continue
        try:
            status = await reconcile_yookassa_web_order(
                order_id,
                payment_id,
                source="scheduler",
            )
            if status not in {"provider_pending", "provider_waiting_for_capture"}:
                logger.info(
                    "Web YooKassa reconciliation order_id=%s payment_id=%s status=%s",
                    order_id,
                    payment_id,
                    status,
                )
        except Exception as exc:
            logger.exception(
                "Unexpected web YooKassa reconciliation failure order_id=%s payment_id=%s: %s",
                order_id,
                payment_id,
                exc,
            )


async def reconcile_pending_yookassa_topups() -> None:
    """Recover YooKassa web balance top-ups whose success webhook was lost."""
    async with async_session() as session:
        rows = (
            await session.execute(
                select(WebBalanceTopUp.topup_id, WebBalanceTopUp.yookassa_payment_id)
                .where(WebBalanceTopUp.status.in_(("pending", "paid")))
                .where(WebBalanceTopUp.yookassa_payment_id.is_not(None))
                .order_by(
                    nulls_first(WebBalanceTopUp.last_reconciled_at.asc()),
                    WebBalanceTopUp.created_at.desc(),
                )
                .limit(50)
            )
        ).all()

    for topup_id, payment_id in rows:
        if not payment_id:
            continue
        try:
            status = await reconcile_yookassa_web_topup(
                topup_id,
                payment_id,
                source="scheduler",
            )
            if status not in {"provider_pending", "provider_waiting_for_capture"}:
                logger.info(
                    "Web YooKassa topup reconciliation topup_id=%s payment_id=%s status=%s",
                    topup_id,
                    payment_id,
                    status,
                )
        except Exception as exc:
            logger.exception(
                "Unexpected web YooKassa topup reconciliation failure topup_id=%s payment_id=%s: %s",
                topup_id,
                payment_id,
                exc,
            )


async def expire_pending_orders() -> None:
    """Cancel only stale orders that never obtained a YooKassa payment id.

    Orders with a provider payment id are reconciled against YooKassa instead of
    being guessed/canceled locally.
    """
    now = datetime.utcnow()
    cutoff = now - timedelta(hours=24)
    async with async_session() as session:
        result = await session.execute(
            select(WebOrder.order_id, WebOrder.profile_token)
            .where(WebOrder.status == "pending")
            .where(WebOrder.created_at <= cutoff)
            .where(WebOrder.yookassa_payment_id.is_(None))
        )
        candidates = result.all()

    expired_count = 0
    for order_id, profile_token in candidates:
        lock_key = profile_token or f"web-order:{order_id}"
        async with _acquire_profile_lock(lock_key, timeout=10.0):
            async with async_session() as session:
                order = await session.scalar(
                    select(WebOrder).where(WebOrder.order_id == order_id)
                )
                if not order or order.status != "pending" or order.yookassa_payment_id:
                    continue
                order.status = "canceled"
                order.cancellation_confirmed = True
                await _refund_order_reserved_bonus_if_needed(
                    session,
                    order,
                    reason="expired_pending_without_provider",
                )
                await session.commit()
                expired_count += 1
    if expired_count:
        logger.info("Expired %d stale pending orders without provider payment", expired_count)


async def retry_paid_web_orders() -> None:
    """Retry paid-but-undelivered orders with tracked, idempotent attempts."""
    now = datetime.utcnow()
    async with async_session() as session:
        result = await session.execute(
            select(WebOrder)
            .where(WebOrder.status == "paid")
            .where(
                or_(
                    WebOrder.next_fulfillment_retry_at <= now,
                    and_(
                        WebOrder.next_fulfillment_retry_at.is_(None),
                        WebOrder.paid_at <= now - timedelta(minutes=1),
                    ),
                )
            )
            .order_by(WebOrder.next_fulfillment_retry_at.asc())
            .limit(20)
        )
        orders = result.scalars().all()

    for order_ref in orders:
        profile_lock_key = order_ref.profile_token or f"web-order:{order_ref.order_id}"
        try:
            async with _acquire_profile_lock(profile_lock_key, timeout=10.0):
                async with async_session() as session:
                    order = await session.scalar(
                        select(WebOrder).where(WebOrder.order_id == order_ref.order_id)
                    )
                    if not order or order.status != "paid":
                        continue
                    if order.next_fulfillment_retry_at and order.next_fulfillment_retry_at > datetime.utcnow():
                        continue
                    # Claim before the provider call. A crashed worker becomes due
                    # again, while overlapping scheduler runs cannot double-issue it.
                    order.next_fulfillment_retry_at = datetime.utcnow() + timedelta(minutes=15)
                    await session.commit()
                    await attempt_fulfill_order(session, order)
                    if order.status == "delivered":
                        await _disable_web_balance_autodebit_after_tariff_purchase(session, order)
                        await _apply_order_bonus_spend(session, order)
                        await session.commit()
                        bot_result = await _notify_bot_order_delivered(order, session=session)
                        await _finalize_order_cashback(session, order, bot_result)
                        await session.commit()
                        await _notify_admins_webstore(order, "delivered")
                        if not bot_result.get("delivered"):
                            await _notify_linked_webstore_user(order, "delivered")
                        await _sync_referral_credit_for_order(order.order_id)
                    else:
                        await session.commit()
                    logger.info(
                        "Web fulfillment retry order_id=%s attempt=%s status=%s next=%s",
                        order.order_id,
                        order.fulfillment_attempts,
                        order.status,
                        order.next_fulfillment_retry_at,
                    )
        except TimeoutError:
            logger.warning(
                "Could not acquire profile lock for retrying order %s, skipping attempt",
                order_ref.order_id,
            )
            continue


async def retry_pending_cashback_orders() -> None:
    """Retry cashback synchronization for delivered orders with pending cashback."""
    async with async_session() as session:
        result = await session.execute(
            select(WebOrder)
            .where(
                WebOrder.status == "delivered",
                WebOrder.cashback_status == "pending",
            )
            .order_by(WebOrder.id.asc())
            .limit(20)
        )
        orders = result.scalars().all()
        for order in orders:
            try:
                bot_result = await _notify_bot_order_delivered(order, session=session, cashback_only=True)
                await _finalize_order_cashback(session, order, bot_result)
                await session.commit()
            except Exception as exc:
                await session.rollback()
                logger.error("Error retrying cashback for order %s: %s", order.order_id, exc)


async def retry_pending_balance_transfers() -> None:
    """Retry pending web balance migrations to Telegram."""
    now = datetime.utcnow()
    # Backoff: only retry transfers that were not attempted in the last 2 minutes, max 10 per tick
    cutoff = now - timedelta(minutes=2)
    async with async_session() as session:
        transfers = (await session.execute(
            select(WebBalanceTransfer.transfer_id, WebBalanceTransfer.profile_token, WebBalanceTransfer.telegram_id)
            .where(
                WebBalanceTransfer.status == "pending",
                or_(
                    WebBalanceTransfer.last_attempt_at.is_(None),
                    WebBalanceTransfer.last_attempt_at <= cutoff,
                ),
            )
            .order_by(WebBalanceTransfer.id.asc())
            .limit(10)
        )).all()
    for transfer_id, profile_token, telegram_id in transfers:
        try:
            async with async_session() as session:
                # Retry strictly using the existing transfer and its immutable telegram_id
                await _migrate_web_balance_to_telegram(
                    session, profile_token, telegram_id, reconcile_existing_only=True,
                )
        except Exception as exc:
            logger.warning("Error retrying pending balance transfer %s: %s", transfer_id, exc)

    await reconcile_linked_web_balances()


async def reconcile_linked_web_balances() -> None:
    async with async_session() as session:
        profiles = (await session.execute(
            select(WebBalanceAccount.profile_token)
            .join(WebProfileLink, WebProfileLink.profile_token == WebBalanceAccount.profile_token)
            .where(WebBalanceAccount.balance_rub > 0)
            .where(~select(WebBalanceTransfer.id).where(
                WebBalanceTransfer.profile_token == WebBalanceAccount.profile_token,
                WebBalanceTransfer.status == "pending",
            ).exists())
        )).scalars().all()
    for profile_token in profiles:
        try:
            async with _acquire_profile_lock(profile_token):
                async with async_session() as session:
                    link = await session.get(WebProfileLink, profile_token)
                    if link:
                        await _migrate_web_balance_to_telegram(session, profile_token, int(link.telegram_id))
        except Exception as exc:
            logger.warning("Error reconciling linked web balance %s: %s", profile_token, exc)


def start_scheduler() -> None:
    global scheduler
    if scheduler and scheduler.running:
        return
    scheduler = AsyncIOScheduler()
    scheduler.add_job(process_web_balance_daily_charges, "interval", minutes=5, id="process_web_balance_daily_charges")
    scheduler.add_job(warn_web_balance_due_soon, "interval", hours=1, id="warn_web_balance_due_soon")
    scheduler.add_job(
        reconcile_pending_yookassa_orders,
        "interval",
        minutes=1,
        id="reconcile_pending_yookassa_orders",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        reconcile_pending_yookassa_topups,
        "interval",
        minutes=1,
        id="reconcile_pending_yookassa_topups",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(expire_pending_orders, "interval", minutes=15, id="expire_pending_orders")
    scheduler.add_job(
        retry_paid_web_orders,
        "interval",
        minutes=1,
        id="retry_paid_web_orders",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        retry_pending_cashback_orders,
        "interval",
        minutes=1,
        id="retry_pending_cashback_orders",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        retry_pending_balance_transfers,
        "interval",
        minutes=1,
        id="retry_pending_balance_transfers",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        process_notification_outbox,
        "interval",
        minutes=1,
        id="process_notification_outbox",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    logger.info("Webstore scheduler started")


def stop_scheduler() -> None:
    global scheduler
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Webstore scheduler stopped")
