"""Purchase intent encoding and Adapt-style retail upgrade quotes."""

from __future__ import annotations

import logging
import math
from datetime import datetime


VALID_ACTIONS = {"auto", "new", "renew", "upgrade"}
_ACTION_CODES = {"new": "n", "renew": "r", "upgrade": "u"}
_CODE_ACTIONS = {value: key for key, value in _ACTION_CODES.items()}
MIN_PURCHASE_PRICE_RUB = 10
logger = logging.getLogger(__name__)


async def get_paid_adapt_upgrade_plans(session, current_plan, target_plan):
    """Resolve authoritative provider plan metadata from /plans/list for paid upgrades.

    Both current and target plans must exist in the authoritative provider plan list,
    be active and non-trial, and contain positive cost, duration, and device metadata.
    Local Tariff attributes must not bypass provider /plans/list.
    """
    from bot.services.subscription_semantics import (
        get_adapt_plan_provider_metadata,
        has_adapt_provider_metadata,
        get_adapt_plan_devices,
    )

    current_uuid = get_adapt_plan_provider_metadata(current_plan)[0]
    target_uuid = get_adapt_plan_provider_metadata(target_plan)[0]
    if not current_uuid or not target_uuid or current_uuid == target_uuid:
        return None

    cache = getattr(session, "info", None)
    cache_key = "adapt_provider_plans"
    provider_plans = cache.get(cache_key) if isinstance(cache, dict) else None
    if provider_plans is None:
        from bot.services.adapt_api import AdaptAPI

        try:
            raw_plans = await AdaptAPI().list_plans()
        except Exception as exc:
            logger.warning("Adapt provider plan list unavailable: %s", type(exc).__name__)
            raw_plans = []
        provider_plans = {}
        for plan in raw_plans or []:
            if not isinstance(plan, dict):
                continue
            plan_uuid = str(plan.get("uuid") or plan.get("plan_uuid") or "").strip()
            if plan_uuid:
                provider_plans[plan_uuid.casefold()] = plan
        if isinstance(cache, dict):
            cache[cache_key] = provider_plans

    current_provider_plan = provider_plans.get(current_uuid.casefold())
    target_provider_plan = provider_plans.get(target_uuid.casefold())
    if not current_provider_plan or not target_provider_plan:
        return None
    if (
        current_provider_plan.get("is_active") is False
        or target_provider_plan.get("is_active") is False
        or current_provider_plan.get("is_trial") is True
        or target_provider_plan.get("is_trial") is True
        or not has_adapt_provider_metadata(current_provider_plan)
        or not has_adapt_provider_metadata(target_provider_plan)
        or not get_adapt_plan_devices(current_provider_plan)
        or not get_adapt_plan_devices(target_provider_plan)
    ):
        return None
    return current_provider_plan, target_provider_plan


get_active_paid_adapt_upgrade_plans = get_paid_adapt_upgrade_plans


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
    from bot.config import settings
    from bot.models import Subscription, Tariff, AdaptSubscription
    from sqlalchemy import select
    from bot.services.adapt_routing import is_adapt_subscription
    from bot.services.subscription_semantics import (
        has_adapt_plan_metadata,
        get_adapt_plan_tariff,
        is_adapt_customer_upgrade_eligible,
        is_explicit_demo_subscription,
        is_adapt_trial_subscription,
        is_adapt_trial_tariff,
    )

    if action == "new" or action == "auto":
        return int(tariff.price_rub)
    if not user:
        raise ValueError("Пользователь не найден")
    target = await session.get(Subscription, int(target_subscription_id or 0))
    if not target or target.user_id != user.id:
        raise ValueError("Выбранная подписка не найдена")
    from bot.services.vhq_routing import get_vhq_spec_for_tariff, is_vhq_subscription

    if is_vhq_subscription(target):
        if target.status.value not in {"active", "expired"}:
            raise ValueError("Подписка недоступна для продления")
        spec = get_vhq_spec_for_tariff(tariff)
        if action != "renew" or not spec or spec["tier"] != "basic" or spec["days"] != 30:
            raise ValueError("Для подписки База Про доступно только продление на 30 дней.")
        if not tariff.is_active or tariff.is_admin_only and not settings.is_admin(user.telegram_id):
            raise ValueError("Выбранный тариф недоступен для продления")
        if int(tariff.price_rub or 0) <= 0:
            raise ValueError("Выбранный тариф недоступен для продления")
        return int(tariff.price_rub)
    if not is_adapt_subscription(target) or not tariff.adapt_plan_uuid:
        raise ValueError(
            "Для этой подписки продление или улучшение недоступно. Создайте новую подписку."
        )
    adapt_sub = None
    if hasattr(session, "scalar"):
        adapt_sub = await session.scalar(
            select(AdaptSubscription).where(AdaptSubscription.subscription_id == target.id).limit(1)
        )
    current_plan_uuid = (
        (getattr(adapt_sub, "adapt_plan_uuid", None) or "").strip()
        or (getattr(target, "tariff", None) and getattr(target.tariff, "adapt_plan_uuid", None) or "").strip()
    )
    current_tariff = None
    if current_plan_uuid:
        if hasattr(session, "scalar"):
            current_tariff = await get_adapt_plan_tariff(session, current_plan_uuid)
    if not current_tariff and not current_plan_uuid and target.tariff_id:
        current_tariff = await session.get(Tariff, target.tariff_id)
    if not current_plan_uuid:
        if not current_tariff:
            raise ValueError("Выбранная подписка не найдена")
        current_plan_uuid = str(getattr(current_tariff, "adapt_plan_uuid", None) or "").strip()
    selected_plan_uuid = (tariff.adapt_plan_uuid or "").strip()
    if action == "renew":
        if not has_adapt_plan_metadata(tariff) or not selected_plan_uuid:
            raise ValueError("Выбранный тариф недоступен для продления. Обратитесь в поддержку.")
        is_target_expired = bool(
            (target.expires_at and target.expires_at <= datetime.utcnow())
            or target.status.value == "expired"
        )
        if current_plan_uuid and current_plan_uuid != selected_plan_uuid and not is_target_expired:
            raise ValueError("Для продления выберите текущий тариф")
        if not current_plan_uuid and (not current_tariff or current_tariff.id != tariff.id) and not is_target_expired:
            raise ValueError("Для продления выберите текущий тариф")
        if int(tariff.price_rub or 0) <= 0:
            raise ValueError("Выбранный тариф недоступен для продления. Обратитесь в поддержку.")
        return int(tariff.price_rub)
    if not current_plan_uuid or not selected_plan_uuid:
        raise ValueError("Для этой подписки улучшение сейчас недоступно")
    if current_plan_uuid == selected_plan_uuid:
        raise ValueError("Эта подписка уже на выбранном тарифе")
    if is_adapt_trial_tariff(tariff):
        raise ValueError("Выбранный тариф недоступен для улучшения. Обратитесь в поддержку.")

    is_expired = bool(target.expires_at and target.expires_at <= datetime.utcnow())
    is_trial = is_adapt_trial_subscription(target, adapt_sub=adapt_sub)
    is_legacy_demo = is_expired and is_explicit_demo_subscription(target)
    demo_plan_uuid = str(settings.adapt_demo_plan_uuid or "").strip()
    if is_trial or is_legacy_demo or (
        demo_plan_uuid and current_plan_uuid.casefold() == demo_plan_uuid.casefold()
    ):
        if not has_adapt_plan_metadata(tariff) or int(tariff.price_rub or 0) <= 0:
            raise ValueError("Выбранный тариф недоступен для улучшения. Обратитесь в поддержку.")
        return int(tariff.price_rub)

    if not current_tariff or not has_adapt_plan_metadata(current_tariff) or not has_adapt_plan_metadata(tariff) or int(tariff.price_rub or 0) <= 0:
        raise ValueError("Улучшение доступно только на более дорогой тариф")

    if is_expired:
        return int(tariff.price_rub)

    provider_plans = await get_paid_adapt_upgrade_plans(
        session,
        current_tariff,
        tariff,
    )
    if not provider_plans or not is_adapt_customer_upgrade_eligible(
        provider_plans[0],
        provider_plans[1],
        expires_at=target.expires_at,
    ):
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
