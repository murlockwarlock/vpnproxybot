"""Routing helpers for tariffs and subscriptions backed by VHQ."""

from __future__ import annotations

from typing import Any

VHQ_CLIENT_PREFIX = "vhq_"


def _normalize_label(value: str | None) -> str:
    return " ".join((value or "").strip().lower().split())


def _normalize_price(value: Any) -> int:
    try:
        return int(round(float(value or 0)))
    except (TypeError, ValueError):
        return 0


def get_vhq_spec_for_tariff(tariff: Any) -> dict[str, Any] | None:
    """Return VHQ purchase parameters for a bot tariff, or None."""
    # If this tariff has an Adapt UUID, it must NOT be routed to VHQ.
    if getattr(tariff, "adapt_plan_uuid", None):
        return None

    explicit_tier = _normalize_label(getattr(tariff, "vhq_tier", ""))
    days = int(getattr(tariff, "days", 0) or 0)
    if explicit_tier in {"lite", "basic"} and days > 0:
        return {"tier": explicit_tier, "days": days}

    label = _normalize_label(getattr(tariff, "label", ""))
    price_rub = _normalize_price(getattr(tariff, "price_rub", 0))

    # Legacy price/duration pairs from the earlier darimiru rollout.
    # Keep them first so old rows remain compatible.
    if days == 1 and price_rub in {1, 10}:
        return {"tier": "lite", "days": 1}
    if days == 7 and price_rub == 59:
        return {"tier": "lite", "days": 7}
    if days == 30 and price_rub == 149:
        return {"tier": "lite", "days": 30}

    # VHQ in the bot must be explicit/legacy only. New bot tariffs default to
    # Marzban unless they carry an Adapt UUID.
    if label in {"vip 7 дней", "базовый 7 дней", "базовый (7 дней)"} and days == 7:
        return {"tier": "lite", "days": 7}
    if label in {
        "премиум (1 месяц)",
        "premium (1 month)",
    } and days == 30 and price_rub == 399:
        return {"tier": "basic", "days": 30}
    return None


def get_vhq_spec_for_store_tariff(tariff: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return VHQ purchase parameters for a webstore tariff, or None."""
    if not tariff or tariff.get("provider") != "vhq":
        return None

    tier = str(tariff.get("vhq_tier", "")).strip().lower()
    days = int(tariff.get("days", 0) or 0)
    if not tier or days <= 0:
        return None
    return {"tier": tier, "days": days}


def is_vhq_tariff(tariff: Any) -> bool:
    return get_vhq_spec_for_tariff(tariff) is not None


def is_vhq_subscription(subscription: Any) -> bool:
    client_name = str(getattr(subscription, "client_name", "") or "")
    return client_name.startswith(VHQ_CLIENT_PREFIX)
