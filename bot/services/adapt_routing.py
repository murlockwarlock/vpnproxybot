"""Routing helpers for tariffs and subscriptions backed by Adapt Group API."""

from __future__ import annotations

from typing import Any

ADAPT_CLIENT_PREFIX = "adapt_"


def is_adapt_tariff(tariff: Any) -> bool:
    """Return True if this tariff should be fulfilled via Adapt API."""
    return bool(getattr(tariff, "adapt_plan_uuid", None))


def is_adapt_subscription(subscription: Any) -> bool:
    """Return True if this subscription was created via Adapt API."""
    return str(getattr(subscription, "client_name", "") or "").startswith(
        ADAPT_CLIENT_PREFIX
    )


def get_adapt_uuid_from_subscription(subscription: Any) -> str | None:
    """Extract the Adapt subscription UUID from a bot Subscription record."""
    client_name = str(getattr(subscription, "client_name", "") or "")
    if client_name.startswith(ADAPT_CLIENT_PREFIX):
        uuid = client_name[len(ADAPT_CLIENT_PREFIX):]
        return uuid if uuid else None
    return None


def build_adapt_client_name(adapt_uuid: str) -> str:
    """Build the client_name stored in Subscription for an Adapt subscription."""
    return f"{ADAPT_CLIENT_PREFIX}{adapt_uuid}"[:64]
