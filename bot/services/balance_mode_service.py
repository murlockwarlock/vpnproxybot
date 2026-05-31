"""Helpers for enabling/disabling balance-managed access."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from bot.models import BalanceTransactionKind, Platform, Subscription, SubStatus, User
from bot.services.balance_service import debit_user_balance, get_daily_charge_rub, get_user_balance, next_charge_datetime
from bot.services.subscription_service import create_or_extend_balance_subscription
from bot.services.subscription_semantics import paid_access_clause


async def get_active_paid_subscription(session, user_id: int) -> Subscription | None:
    result = await session.execute(
        select(Subscription)
        .where(Subscription.user_id == user_id)
        .where(paid_access_clause(Subscription))
        .where(Subscription.status == SubStatus.ACTIVE)
        .order_by(Subscription.expires_at.desc(), Subscription.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def enable_balance_mode(session, user: User) -> tuple[bool, str | None]:
    now = datetime.utcnow()
    active_sub = await get_active_paid_subscription(session, user.id)

    user.balance_mode_enabled = True
    user.balance_autodebit_enabled = True

    if active_sub and active_sub.billing_mode != "balance" and active_sub.expires_at > now:
        active_sub.billing_mode = "balance"
        user.next_daily_charge_at = active_sub.expires_at
        user.balance_grace_until = None
        user.balance_warning_for_charge_at = None
        return True, None

    daily_rate = await get_daily_charge_rub(session)
    if get_user_balance(user) < daily_rate:
        user.balance_mode_enabled = False
        user.balance_autodebit_enabled = False
        return False, "Недостаточно средств для запуска режима"

    expires_at = next_charge_datetime(now)
    debit_user_balance(
        session,
        user,
        daily_rate,
        kind=BalanceTransactionKind.DAILY_CHARGE,
        description="Ежедневное списание",
        source_type="daily_charge_start",
        source_id=expires_at.isoformat(),
    )
    await create_or_extend_balance_subscription(
        session,
        user=user,
        platform=user.platform or Platform.ANDROID,
        expires_at=expires_at,
    )
    user.next_daily_charge_at = expires_at
    user.last_daily_charge_at = now
    user.balance_grace_until = None
    user.balance_warning_for_charge_at = None
    return True, None


async def disable_balance_mode(session, user: User) -> tuple[bool, datetime | None]:
    now = datetime.utcnow()
    active_sub = await get_active_paid_subscription(session, user.id)
    access_until = None
    if active_sub and active_sub.billing_mode == "balance" and active_sub.expires_at > now:
        access_until = active_sub.expires_at

    user.balance_mode_enabled = False
    user.balance_autodebit_enabled = False
    user.balance_grace_until = None
    user.next_daily_charge_at = access_until
    user.balance_warning_for_charge_at = None
    return True, access_until
