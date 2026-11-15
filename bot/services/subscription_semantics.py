"""Shared predicates for demo vs paid subscription rows and Adapt provider plan semantics."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

ADAPT_MIN_CUSTOM_RENEW_DAYS = 3


def is_demo_subscription_row(sub) -> bool:
    """Return True only for a real demo row."""
    if not sub:
        return False
    mode = getattr(sub, "billing_mode", None)
    if isinstance(mode, str) and mode == "demo":
        return True
    client_name = getattr(sub, "client_name", None)
    if isinstance(client_name, str) and client_name.endswith("_demo"):
        return True
    return False


def is_explicit_demo_subscription(sub) -> bool:
    if not sub:
        return False
    if is_demo_subscription_row(sub):
        return True
    status = getattr(sub, "status", None)
    status_value = getattr(status, "value", status)
    if isinstance(status_value, str) and status_value.lower() == "demo":
        return True
    return getattr(sub, "tariff_key", None) == "demo"


def is_adapt_trial_tariff(tariff) -> bool:
    """Return True only if the tariff explicitly matches the configured Adapt demo plan UUID."""
    if not tariff or isinstance(tariff, (int, str)):
        return False
    from bot.config import settings
    demo_plan_uuid = (getattr(settings, "adapt_demo_plan_uuid", "") or "").strip().lower()
    if not demo_plan_uuid:
        return False
    plan = None
    if isinstance(tariff, dict):
        plan = tariff.get("adapt_plan_uuid")
    else:
        plan = getattr(tariff, "adapt_plan_uuid", None)
    if not isinstance(plan, str) or not plan.strip():
        return False
    return plan.strip().lower() == demo_plan_uuid


def is_adapt_trial_subscription(sub, adapt_sub=None) -> bool:
    """Recognise explicit demo subscriptions by canonical AdaptSubscription plan UUID or explicit billing_mode."""
    if not sub:
        return False

    from bot.config import settings
    demo_plan_uuid = (getattr(settings, "adapt_demo_plan_uuid", "") or "").strip().lower()

    # 1. Canonical AdaptSubscription record if provided or attached
    adapt_record = adapt_sub
    if adapt_record is None and hasattr(sub, "adapt_sub"):
        adapt_record = getattr(sub, "adapt_sub", None)
    elif adapt_record is None and hasattr(sub, "adapt_subscription"):
        adapt_record = getattr(sub, "adapt_subscription", None)

    if adapt_record is not None:
        plan = getattr(adapt_record, "adapt_plan_uuid", None)
        if isinstance(plan, str) and plan.strip():
            # Canonical AdaptSubscription plan UUID is strictly authoritative over tariff, name, billing_mode
            if not demo_plan_uuid:
                return False
            return plan.strip().lower() == demo_plan_uuid
        return False

    # 2. Persisted demo mode (billing_mode == "demo") when AdaptSubscription is absent
    mode = getattr(sub, "billing_mode", None)
    if isinstance(mode, str) and mode == "demo":
        return True

    # 3. Client name fallback only when AdaptSubscription is absent
    client_name = getattr(sub, "client_name", None)
    if isinstance(client_name, str) and client_name.endswith("_demo"):
        return True

    # 4. Fallback to tariff only when AdaptSubscription is absent
    if demo_plan_uuid:
        tariff = None
        if hasattr(sub, "__dict__") and "tariff" in sub.__dict__:
            tariff = sub.__dict__["tariff"]
        elif not hasattr(sub, "_sa_instance_state"):
            tariff = getattr(sub, "tariff", None)
        if tariff is not None and not isinstance(tariff, (int, str)):
            return is_adapt_trial_tariff(tariff)
    return False


def get_adapt_plan_provider_metadata(plan: Any) -> tuple[str | None, Decimal | None, int | None]:
    """Extract (plan_uuid, cost_price_usd, days) from Tariff, dict, or plan metadata."""
    if not plan:
        return None, None, None
    plan_uuid = None
    cost_usd = None
    days = None
    if isinstance(plan, dict):
        plan_uuid = str(
            plan.get("adapt_plan_uuid")
            or plan.get("uuid")
            or plan.get("plan_uuid")
            or ""
        ).strip()
        raw_cost = plan.get("adapt_cost_price_usd")
        if raw_cost is None:
            raw_cost = plan.get("price_usd")
        if raw_cost is not None:
            try:
                cost_usd = Decimal(str(raw_cost))
            except Exception:
                cost_usd = None
        raw_days = plan.get("days")
        if raw_days is not None:
            try:
                days = int(raw_days)
            except Exception:
                days = None
    else:
        plan_uuid = str(
            getattr(plan, "adapt_plan_uuid", None)
            or getattr(plan, "uuid", None)
            or ""
        ).strip()
        raw_cost = getattr(plan, "adapt_cost_price_usd", None)
        if raw_cost is None:
            raw_cost = getattr(plan, "price_usd", None)
        if raw_cost is not None:
            try:
                cost_usd = Decimal(str(raw_cost))
            except Exception:
                cost_usd = None
        raw_days = getattr(plan, "days", None)
        if raw_days is not None:
            try:
                days = int(raw_days)
            except Exception:
                days = None
    return plan_uuid or None, cost_usd, days


def has_adapt_provider_metadata(plan: Any) -> bool:
    plan_uuid, cost_usd, days = get_adapt_plan_provider_metadata(plan)
    return bool(
        plan_uuid
        and cost_usd is not None
        and cost_usd > Decimal("0")
        and days is not None
        and days > 0
    )


def has_adapt_plan_metadata(plan: Any) -> bool:
    plan_uuid, _, days = get_adapt_plan_provider_metadata(plan)
    return bool(plan_uuid and days is not None and days > 0)


async def get_adapt_plan_tariff(session, plan_uuid: str | None):
    if not plan_uuid:
        return None
    from sqlalchemy import select

    from bot.models import Tariff

    return await session.scalar(
        select(Tariff)
        .where(Tariff.adapt_plan_uuid == str(plan_uuid).strip())
        .order_by(Tariff.id.desc())
        .limit(1)
    )


def is_adapt_provider_upgrade_eligible(
    current_plan: Any,
    target_plan: Any,
    *,
    expires_at: datetime | None = None,
    now: datetime | None = None,
) -> bool:
    """Determine whether target_plan is an eligible provider upgrade from current_plan.

    Contract rules:
    - Same plan UUID is renew, never upgrade (returns False).
    - Both current and target plans must have authoritative local provider
      metadata with positive cost and duration.
    - The computed provider upgrade_price must be strictly positive:
      remaining_value = (current_cost_usd / current_days) * rem_days
      upgrade_price = target_cost_usd - remaining_value
      For active subscriptions, rem_days is the remaining term. For expired
      subscriptions, rem_days is ADAPT_MIN_CUSTOM_RENEW_DAYS.
    """
    current_uuid, current_cost, current_days = get_adapt_plan_provider_metadata(current_plan)
    target_uuid, target_cost, target_days = get_adapt_plan_provider_metadata(target_plan)

    if not current_uuid or not target_uuid:
        return False
    if current_uuid == target_uuid:
        return False

    if current_cost is None or current_cost <= Decimal("0") or not current_days or current_days <= 0:
        return False
    if target_cost is None or target_cost <= Decimal("0") or not target_days or target_days <= 0:
        return False

    current_time = now or datetime.utcnow()
    if expires_at is None or expires_at <= current_time:
        rem_days = Decimal(str(ADAPT_MIN_CUSTOM_RENEW_DAYS))
    else:
        rem_secs = Decimal(str(max(0.0, (expires_at - current_time).total_seconds())))
        rem_days = rem_secs / Decimal("86400")

    remaining_value = (current_cost / Decimal(current_days)) * rem_days
    upgrade_price = target_cost - remaining_value

    return upgrade_price > Decimal("0")


def get_adapt_plan_devices(plan: Any) -> int | None:
    """Extract positive device count from Tariff, dict, or provider plan metadata."""
    if not plan:
        return None
    raw = None
    if isinstance(plan, dict):
        raw = plan.get("devices")
        if raw is None:
            raw = plan.get("device_count")
        if raw is None:
            raw = plan.get("device_limit")
    else:
        raw = getattr(plan, "devices", None)
        if raw is None:
            raw = getattr(plan, "device_count", None)
        if raw is None:
            raw = getattr(plan, "device_limit", None)
    if raw is not None:
        try:
            val = int(raw)
            return val if val > 0 else None
        except (ValueError, TypeError):
            return None
    return None


def is_adapt_customer_upgrade_eligible(
    current_plan: Any,
    target_plan: Any,
    *,
    expires_at: datetime | None = None,
    is_demo: bool = False,
    now: datetime | None = None,
) -> bool:
    current_uuid, current_cost, current_days = get_adapt_plan_provider_metadata(current_plan)
    target_uuid, target_cost, target_days = get_adapt_plan_provider_metadata(target_plan)
    if not current_uuid or not target_uuid or current_uuid == target_uuid:
        return False
    if not target_days or target_days <= 0:
        return False
    if is_demo:
        return True
    if not current_days or current_days <= 0:
        return False

    # Device entitlement must fail closed:
    # Both current and target provider devices must be known and > 0,
    # and target_devices >= current_devices
    current_devices = get_adapt_plan_devices(current_plan)
    target_devices = get_adapt_plan_devices(target_plan)
    if current_devices is None or target_devices is None or current_devices <= 0 or target_devices <= 0:
        return False
    if target_devices < current_devices:
        return False

    current_time = now or datetime.utcnow()
    return is_adapt_provider_upgrade_eligible(
        current_plan,
        target_plan,
        expires_at=expires_at,
        now=current_time,
    )


def paid_access_clause(model):
    """SQLAlchemy clause for rows that should be treated as non-demo access."""
    return model.billing_mode != "demo"


def demo_access_clause(model):
    """SQLAlchemy clause for real demo rows."""
    return model.billing_mode == "demo"
