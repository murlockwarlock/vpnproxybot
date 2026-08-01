"""Background jobs for web-only balance mode."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import and_, or_, select

from webstore.database import async_session
from webstore.models import WebBalanceAccount, WebOrder
from webstore.routes import (
    _apply_order_bonus_spend,
    _build_local_balance_status,
    _disable_web_balance_autodebit_after_tariff_purchase,
    _ensure_web_access_until,
    _fetch_web_balance_history,
    _get_latest_web_access_order,
    _get_or_create_web_balance_account,
    _get_web_daily_charge_rub,
    _next_charge_datetime,
    _notify_admins_webstore,
    _notify_linked_webstore_user,
    _sync_referral_credit_for_order,
    _serialize_web_balance_history,
    _add_web_balance_transaction,
    attempt_fulfill_order,
)
from webstore.notification_outbox import process_notification_outbox

logger = logging.getLogger(__name__)

scheduler: AsyncIOScheduler | None = None


async def process_web_balance_daily_charges() -> None:
    now = datetime.utcnow()

    async with async_session() as session:
        daily_rate = await _get_web_daily_charge_rub()
        result = await session.execute(
            select(WebBalanceAccount)
            .where(WebBalanceAccount.balance_mode_enabled == 1)
            .where(WebBalanceAccount.balance_autodebit_enabled == 1)
            .where(WebBalanceAccount.next_daily_charge_at.is_not(None))
            .where(WebBalanceAccount.next_daily_charge_at <= now)
        )
        accounts = result.scalars().all()

        for account in accounts:
            account = await _get_or_create_web_balance_account(session, account.profile_token, account.contact)
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

        await session.commit()


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


async def expire_pending_orders() -> None:
    """Cancel pending web orders older than 24 hours."""
    now = datetime.utcnow()
    cutoff = now - timedelta(hours=24)
    async with async_session() as session:
        result = await session.execute(
            select(WebOrder)
            .where(WebOrder.status == "pending")
            .where(WebOrder.created_at <= cutoff)
        )
        orders = result.scalars().all()
        if orders:
            for order in orders:
                order.status = "canceled"
            await session.commit()
            logger.info("Expired %d stale pending orders", len(orders))


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
        for order in orders:
            # Claim before the provider call.  A crashed worker becomes due
            # again, while overlapping scheduler runs cannot double-issue it.
            order.next_fulfillment_retry_at = now + timedelta(minutes=15)
            await session.commit()
            await attempt_fulfill_order(session, order)
            if order.status == "delivered":
                await _disable_web_balance_autodebit_after_tariff_purchase(session, order)
                await _apply_order_bonus_spend(session, order)
            await session.commit()
            if order.status == "delivered":
                await _notify_admins_webstore(order, "delivered")
                await _notify_linked_webstore_user(order, "delivered")
                await _sync_referral_credit_for_order(order.order_id)
            logger.info(
                "Web fulfillment retry order_id=%s attempt=%s status=%s next=%s",
                order.order_id,
                order.fulfillment_attempts,
                order.status,
                order.next_fulfillment_retry_at,
            )


def start_scheduler() -> None:
    global scheduler
    if scheduler and scheduler.running:
        return
    scheduler = AsyncIOScheduler()
    scheduler.add_job(process_web_balance_daily_charges, "interval", minutes=5, id="process_web_balance_daily_charges")
    scheduler.add_job(warn_web_balance_due_soon, "interval", hours=1, id="warn_web_balance_due_soon")
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
