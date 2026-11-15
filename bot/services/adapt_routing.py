"""Routing helpers for tariffs and subscriptions backed by Adapt Group API."""

from __future__ import annotations

import re
from typing import Any

ADAPT_CLIENT_PREFIX = "adapt_"
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_ADAPT_URL_UUID_RE = re.compile(
    r"/(?:adapt-sub|sub)/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
    re.IGNORECASE,
)


def is_adapt_tariff(tariff: Any) -> bool:
    """Return True if this tariff should be fulfilled via Adapt API."""
    return bool(getattr(tariff, "adapt_plan_uuid", None))


def get_adapt_uuid_from_subscription(subscription: Any) -> str | None:
    """Extract the canonical Adapt subscription UUID from a Subscription or AdaptSubscription object."""
    if subscription is None:
        return None

    # 1. Direct adapt_uuid attribute (e.g. AdaptSubscription model or dict/wrapper)
    direct_uuid = str(getattr(subscription, "adapt_uuid", "") or "").strip()
    if direct_uuid and _UUID_RE.match(direct_uuid):
        return direct_uuid

    # 2. From client_name: adapt_{uuid}
    client_name = str(getattr(subscription, "client_name", "") or "").strip()
    if client_name.startswith(ADAPT_CLIENT_PREFIX):
        raw_uuid = client_name[len(ADAPT_CLIENT_PREFIX):].strip()
        if raw_uuid and _UUID_RE.match(raw_uuid):
            return raw_uuid

    # 3. From attached relationship (e.g. subscription.adapt_sub)
    adapt_sub = getattr(subscription, "adapt_sub", None)
    if adapt_sub is not None:
        sub_uuid = str(getattr(adapt_sub, "adapt_uuid", "") or "").strip()
        if sub_uuid and _UUID_RE.match(sub_uuid):
            return sub_uuid

    # 4. From vpn_key URL if present
    vpn_key = str(getattr(subscription, "vpn_key", "") or "").strip()
    if vpn_key:
        match = _ADAPT_URL_UUID_RE.search(vpn_key)
        if match:
            return match.group(1)

    return None


def is_adapt_subscription(subscription: Any) -> bool:
    """Return True if this subscription is backed by Adapt API."""
    if subscription is None:
        return False
    client_name = str(getattr(subscription, "client_name", "") or "")
    if client_name.startswith(ADAPT_CLIENT_PREFIX):
        return True
    return get_adapt_uuid_from_subscription(subscription) is not None


def build_adapt_client_name(adapt_uuid: str) -> str:
    """Build the client_name stored in Subscription for an Adapt subscription."""
    return f"{ADAPT_CLIENT_PREFIX}{adapt_uuid}"[:64]

