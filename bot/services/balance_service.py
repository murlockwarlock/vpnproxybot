"""Helpers for the unified user balance and its ledger."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

from bot.models import BalanceDirection, BalanceTransaction, BalanceTransactionKind, BotSettings, User

_MSK = timezone(timedelta(hours=3))
_DEFAULT_DAILY_RATE = (Decimal("95") / Decimal("30")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def get_user_balance(user: User | None) -> float:
    if not user:
        return 0.0
    return round(float(user.balance_rub or 0.0), 2)


def get_default_daily_charge_rub() -> float:
    return float(_DEFAULT_DAILY_RATE)


async def get_daily_charge_rub(session) -> float:
    row = await session.get(BotSettings, "daily_charge_rub")
    if row and row.value:
        try:
            value = round(float(row.value), 2)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    return get_default_daily_charge_rub()


def next_charge_datetime(now: datetime | None = None) -> datetime:
    current = now or datetime.utcnow().replace(tzinfo=timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local = current.astimezone(_MSK)
    candidate = local.replace(hour=5, minute=0, second=0, microsecond=0)
    if local >= candidate:
        candidate = candidate + timedelta(days=1)
    return candidate.astimezone(timezone.utc).replace(tzinfo=None)


def credit_user_balance(
    session,
    user: User,
    amount_rub: float,
    kind: BalanceTransactionKind,
    description: str,
    source_type: str | None = None,
    source_id: str | None = None,
) -> float:
    amount = round(float(amount_rub), 2)
    if amount <= 0:
        return get_user_balance(user)

    user.balance_rub = round(get_user_balance(user) + amount, 2)
    user.balance_warning_for_charge_at = None
    session.add(
        BalanceTransaction(
            user_id=user.id,
            kind=kind,
            direction=BalanceDirection.CREDIT,
            amount_rub=amount,
            balance_after_rub=user.balance_rub,
            description=description,
            source_type=source_type,
            source_id=source_id,
        )
    )
    return user.balance_rub


def debit_user_balance(
    session,
    user: User,
    amount_rub: float,
    kind: BalanceTransactionKind,
    description: str,
    source_type: str | None = None,
    source_id: str | None = None,
) -> float:
    amount = round(float(amount_rub), 2)
    if amount <= 0:
        return get_user_balance(user)

    user.balance_rub = round(get_user_balance(user) - amount, 2)
    session.add(
        BalanceTransaction(
            user_id=user.id,
            kind=kind,
            direction=BalanceDirection.DEBIT,
            amount_rub=amount,
            balance_after_rub=user.balance_rub,
            description=description,
            source_type=source_type,
            source_id=source_id,
        )
    )
    return user.balance_rub
