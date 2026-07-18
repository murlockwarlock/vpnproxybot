"""Tests for VHQ-related subscription_service functions."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.models import Subscription, SubStatus

def _make_user(id=1, telegram_id=123456):
    u = MagicMock()
    u.id = id
    u.telegram_id = telegram_id
    return u

def _make_tariff(days=30, label="Premium"):
    t = MagicMock()
    t.days = days
    t.label = label
    t.id = 42
    return t

def _make_server():
    s = MagicMock()
    s.id = 1
    s.api_url = "https://vpn.test.com"
    return s

@pytest.mark.asyncio
async def test_create_vhq_paid_subscription_new_success():
    from bot.services.subscription_service import _create_vhq_paid_subscription

    session = AsyncMock()
    session.flush = AsyncMock()
    session.add = MagicMock()

    user = _make_user()
    tariff = _make_tariff()
    server = _make_server()

    api_resp = {
        "order_id": "vhq-order-111",
        "branded_url": "https://sub.vhq-connect.xyz/some-key",
    }

    vhq_spec = {"tier": "basic", "days": 30}

    # Simulate no existing subscription
    mock_execute_result = MagicMock()
    mock_execute_result.scalar_one_or_none = MagicMock(return_value=None)

    with (
        patch("bot.services.subscription_service.get_primary_active_server", new=AsyncMock(return_value=server)),
        patch("bot.services.subscription_service.VHQPartnerAPI") as mock_vhq_cls,
        patch("bot.services.subscription_service.build_vhq_subscription_ref_url", return_value="https://darimiru.ru/vpnbot/vhq-sub/ref-url"),
        patch("bot.services.subscription_service.get_included_device_slots", new=AsyncMock(return_value=3)),
        patch("bot.services.subscription_service.plog"),
    ):
        mock_api = AsyncMock()
        mock_api.buy = AsyncMock(return_value=api_resp)
        mock_vhq_cls.return_value = mock_api
        mock_vhq_cls.extract_subscription_url.side_effect = lambda data: data.get("branded_url") or data.get("config_url")
        session.execute = AsyncMock(return_value=mock_execute_result)

        sub, key = await _create_vhq_paid_subscription(
            session, user=user, tariff=tariff, platform=MagicMock(), vhq_spec=vhq_spec
        )

    assert sub is not None
    assert key == "https://darimiru.ru/vpnbot/vhq-sub/ref-url"
    
    # Verify Subscription was added
    add_calls = [call[0][0] for call in session.add.call_args_list]
    subs = [c for c in add_calls if isinstance(c, Subscription)]
    assert len(subs) == 1
    assert subs[0].vpn_key == "https://sub.vhq-connect.xyz/some-key"
    assert subs[0].client_name.startswith("vhq_")

@pytest.mark.asyncio
async def test_create_vhq_paid_subscription_reuses_existing():
    from bot.services.subscription_service import _create_vhq_paid_subscription

    session = AsyncMock()
    session.flush = AsyncMock()
    session.add = MagicMock()

    user = _make_user()
    tariff = _make_tariff()
    server = _make_server()

    # Pre-existing subscription
    existing_sub = Subscription(
        id=99,
        user_id=user.id,
        server_id=server.id,
        client_name="vhq_old_order",
        vpn_key="https://sub.vhq-connect.xyz/old-key",
        expires_at=datetime.utcnow() + timedelta(days=5),
        status=SubStatus.ACTIVE,
    )

    api_resp = {
        "order_id": "vhq-order-222",
        "branded_url": "https://sub.vhq-connect.xyz/new-key",
    }

    vhq_spec = {"tier": "basic", "days": 30}

    # Simulate existing subscription
    mock_execute_result = MagicMock()
    mock_execute_result.scalar_one_or_none = MagicMock(return_value=existing_sub)

    with (
        patch("bot.services.subscription_service.get_primary_active_server", new=AsyncMock(return_value=server)),
        patch("bot.services.subscription_service.VHQPartnerAPI") as mock_vhq_cls,
        patch("bot.services.subscription_service.build_vhq_subscription_ref_url", return_value="https://darimiru.ru/vpnbot/vhq-sub/ref-url-99"),
        patch("bot.services.subscription_service.get_included_device_slots", new=AsyncMock(return_value=3)),
        patch("bot.services.subscription_service.plog"),
    ):
        mock_api = AsyncMock()
        mock_api.buy = AsyncMock(return_value=api_resp)
        mock_vhq_cls.return_value = mock_api
        mock_vhq_cls.extract_subscription_url.side_effect = lambda data: data.get("branded_url") or data.get("config_url")
        session.execute = AsyncMock(return_value=mock_execute_result)

        sub, key = await _create_vhq_paid_subscription(
            session, user=user, tariff=tariff, platform=MagicMock(), vhq_spec=vhq_spec
        )

    assert sub is existing_sub
    assert key == "https://darimiru.ru/vpnbot/vhq-sub/ref-url-99"
    assert existing_sub.vpn_key == "https://sub.vhq-connect.xyz/new-key"
    assert existing_sub.status == SubStatus.ACTIVE
    
    # Verify no new Subscription was added to session
    add_calls = [call[0][0] for call in session.add.call_args_list]
    subs = [c for c in add_calls if isinstance(c, Subscription)]
    assert len(subs) == 0
