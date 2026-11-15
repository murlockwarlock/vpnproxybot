"""Focused regression tests for darimiru.xyz Adapt subscription delivery."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import pytest

from bot.config import Settings
from bot.services.adapt_routing import (
    get_adapt_uuid_from_subscription,
    is_adapt_subscription,
)
from bot.services.adapt_subscription_proxy import (
    build_adapt_mirror_url,
    get_adapt_display_key,
)
from bot.services.vhq_subscription_proxy import get_subscription_display_key
from bot.services.webstore_bridge import serialize_profile_items


def test_adapt_mirror_url_defaults_to_darimiru_xyz():
    """Adapt mirror URL must use https://darimiru.xyz by default."""
    uuid = "bcbccd61-c8e3-4e1e-a7aa-5ead9d8c2c55"
    url = build_adapt_mirror_url(uuid)
    assert url == f"https://darimiru.xyz/vpnbot/adapt-sub/{uuid}"


def test_adapt_mirror_url_respects_custom_base():
    """Adapt mirror URL respects custom ADAPT_SUBSCRIPTION_BASE_URL if configured."""
    uuid = "bcbccd61-c8e3-4e1e-a7aa-5ead9d8c2c55"
    with patch("bot.services.adapt_subscription_proxy.settings") as mock_settings:
        mock_settings.adapt_subscription_base_url = "https://custom-proxy.example.com"
        mock_settings.webhook_path_prefix = "/vpnbot"
        url = build_adapt_mirror_url(uuid)
    assert url == f"https://custom-proxy.example.com/vpnbot/adapt-sub/{uuid}"


def test_legacy_darimiru_ru_key_does_not_override_preferred_xyz():
    """Stored darimiru.ru URL must not override the new preferred darimiru.xyz delivery URL."""
    uuid = "bcbccd61-c8e3-4e1e-a7aa-5ead9d8c2c55"
    legacy_url = f"https://darimiru.ru/vpnbot/adapt-sub/{uuid}"
    sub = SimpleNamespace(
        id=307,
        client_name=f"adapt_{uuid}",
        vpn_key=legacy_url,
    )

    # 1. Extraction of UUID
    assert get_adapt_uuid_from_subscription(sub) == uuid
    assert is_adapt_subscription(sub) is True

    # 2. Display key helper must return darimiru.xyz
    display_key = get_subscription_display_key(sub)
    assert display_key == f"https://darimiru.xyz/vpnbot/adapt-sub/{uuid}"
    assert "darimiru.ru" not in display_key


def test_vhq_subscription_display_key_untouched():
    """VHQ subscriptions must NOT be converted to Adapt or darimiru.xyz URLs."""
    sub = SimpleNamespace(
        id=99,
        client_name="vhq_user_99",
        vpn_key="https://vhq-upstream.example.com/sub/token123",
        vhq_order_id="vhq-order-1",
    )
    assert is_adapt_subscription(sub) is False
    display_key = get_subscription_display_key(sub)
    assert "darimiru.xyz" not in display_key
    assert "vhq" in display_key or "sub" in display_key


def test_marzban_subscription_display_key_untouched():
    """Marzban subscriptions must retain their stored vpn_key unchanged."""
    marzban_key = "https://darimiru.ru/sub/marzban_token_abc"
    sub = SimpleNamespace(
        id=50,
        client_name="marzban_user_50",
        vpn_key=marzban_key,
        vhq_order_id=None,
    )
    assert is_adapt_subscription(sub) is False
    display_key = get_subscription_display_key(sub)
    assert display_key == marzban_key


def test_webstore_bridge_presents_darimiru_xyz_for_adapt():
    """Webstore bridge items must present darimiru.xyz for Adapt subscriptions when key is generated."""
    uuid = "bcbccd61-c8e3-4e1e-a7aa-5ead9d8c2c55"
    user = SimpleNamespace(
        id=207,
        telegram_id=295220793,
        username="test_user",
        full_name="Test User",
    )
    sub = SimpleNamespace(
        id=307,
        user_id=207,
        server=None,
        tariff_days=30,
        tariff_months=1,
        tariff=None,
        client_name=f"adapt_{uuid}",
        vpn_key=None,
        status=SimpleNamespace(value="active"),
        device_slots=3,
        expires_at=None,
    )
    adapt_record = SimpleNamespace(
        subscription_id=307,
        adapt_uuid=uuid,
        adapt_plan_uuid="303c4b1c-ecfc-4346-b6f8-31c0cad27646",
    )
    items = serialize_profile_items(
        user=user,
        subscriptions=[sub],
        mtproto_accounts=[],
        adapt_map={307: adapt_record},
    )
    assert len(items) == 1
    assert items[0]["key_value"] == f"https://darimiru.xyz/vpnbot/adapt-sub/{uuid}"
