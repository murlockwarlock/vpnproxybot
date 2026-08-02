"""Purchase intent encoding and Adapt-style retail upgrade quotes."""

from __future__ import annotations

import math
from datetime import datetime


VALID_ACTIONS = {"auto", "new", "renew", "upgrade"}
_ACTION_CODES = {"new": "n", "renew": "r", "upgrade": "u"}
_CODE_ACTIONS = {value: key for key, value in _ACTION_CODES.items()}
MIN_PURCHASE_PRICE_RUB = 10


def effective_expired_adapt_action(
    action: str,
    *,
    current_plan_uuid: str | None,
    selected_plan_uuid: str | None,
    expires_at: datetime | None,
    now: datetime | None = None,
) -> str:
    """Treat selecting the same plan on an expired tariff screen as renew."""
    current = now or datetime.utcnow()
    if (
        action == "upgrade"
        and expires_at
        and expires_at <= current
        and str(current_plan_uuid or "").strip()
        and str(current_plan_uuid or "").strip() == str(selected_plan_uuid or "").strip()
    ):
        return "renew"
    return action


def encode_intent(value: str, action: str, target_subscription_id: int | None = None) -> str:
    """Append a compact operation context to a callback/platform value."""
    action = action if action in _ACTION_CODES else "new"
    target = int(target_subscription_id or 0)
    return f"{value}~{_ACTION_CODES[action]}~{target}"


def decode_intent(value: str) -> tuple[str, str, int | None]:
    """Return (plain value, action, target subscription id)."""
    parts = str(value or "").rsplit("~", 2)
    if len(parts) != 3 or parts[1] not in _CODE_ACTIONS:
        return str(value or ""), "auto", None
    try:
        target = int(parts[2]) or None
    except ValueError:
        return str(value or ""), "auto", None
    return parts[0], _CODE_ACTIONS[parts[1]], target


def calculate_upgrade_price_rub(
    *,
    current_price_rub: float,
    current_days: int,
    new_price_rub: float,
    expires_at: datetime,
    now: datetime | None = None,
) -> int:
    """Calculate the current Adapt replacement-plan formula in retail RUB.

    remaining_value = old_plan_price / old_plan_days * remaining_days
    upgrade_price = new_plan_price - remaining_value
    """
    current = now or datetime.utcnow()
    if current_days <= 0 or expires_at <= current:
        return max(0, math.ceil(float(new_price_rub)))
    remaining_days = (expires_at - current).total_seconds() / 86400
    remaining_value = float(current_price_rub) / current_days * remaining_days
    return max(0, math.ceil(float(new_price_rub) - remaining_value))


async def get_purchase_price_rub(session, *, user, tariff, action: str, target_subscription_id: int | None) -> int:
    """Return and validate the customer-facing price for a purchase intent."""
    from bot.models import Subscription, Tariff
    from bot.services.adapt_routing import is_adapt_subscription

    if action == "new" or action == "auto":
        return int(tariff.price_rub)
    if not user:
        raise ValueError("Пользователь не найден")
    target = await session.get(Subscription, int(target_subscription_id or 0))
    current_tariff = await session.get(Tariff, target.tariff_id) if target and target.tariff_id else None
    if not target or target.user_id != user.id or not current_tariff:
        raise ValueError("Выбранная подписка не найдена")
    if (
        not is_adapt_subscription(target)
        or not current_tariff.adapt_plan_uuid
        or not tariff.adapt_plan_uuid
    ):
        raise ValueError(
            "Для этой подписки продление или улучшение недоступно. Создайте новую подписку."
        )
    if action == "renew":
        if current_tariff.id != tariff.id:
            raise ValueError("Для продления выберите текущий тариф")
        return int(tariff.price_rub)
    if not current_tariff.adapt_plan_uuid or not tariff.adapt_plan_uuid:
        raise ValueError("Для этой подписки улучшение сейчас недоступно")
    if current_tariff.adapt_plan_uuid == tariff.adapt_plan_uuid:
        raise ValueError("Эта подписка уже на выбранном тарифе")
    if target.expires_at <= datetime.utcnow():
        # Adapt upgrades active subscriptions only.  Fulfillment first
        # reactivates an expired UUID for the provider minimum and immediately
        # replaces its plan, so the customer pays the full selected tariff.
        return int(tariff.price_rub)
    if int(tariff.price_rub) <= int(current_tariff.price_rub):
        raise ValueError("Улучшение доступно только на более дорогой тариф")
    price = calculate_upgrade_price_rub(
        current_price_rub=float(current_tariff.price_rub),
        current_days=int(current_tariff.days),
        new_price_rub=float(tariff.price_rub),
        expires_at=target.expires_at,
    )
    if price <= 0:
        raise ValueError("Переход на этот тариф сейчас невозможен")
    return max(MIN_PURCHASE_PRICE_RUB, price)
