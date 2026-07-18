"""Tests for Adapt-related subscription_service functions."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_user(id=1, telegram_id=123456):
    u = MagicMock()
    u.id = id
    u.telegram_id = telegram_id
    return u


def _make_tariff(adapt_plan_uuid="plan-test-uuid", days=30, label="Test 30d"):
    t = MagicMock()
    t.adapt_plan_uuid = adapt_plan_uuid
    t.days = days
    t.label = label
    t.id = 42
    return t


def _make_server():
    s = MagicMock()
    s.id = 1
    s.api_url = "https://vpn.test.com"
    return s


def _adapt_api_response(adapt_uuid="sub-uuid-001", days=30, devices=3):
    return {
        "subscription_uuid": adapt_uuid,
        "subscription_url": f"https://network-api.adaptgroup.app/sub/{adapt_uuid}",
        "plan_uuid": "plan-test-uuid",
        "end_date": (datetime.utcnow() + timedelta(days=days)).isoformat() + "Z",
        "days": days,
        "devices": devices,
        "traffic_limit_bytes": None,
        "balance": 5.0,
    }


# ── is_adapt_tariff routing ───────────────────────────────────────────────────

def test_is_adapt_tariff_positive():
    from bot.services.adapt_routing import is_adapt_tariff
    tariff = _make_tariff(adapt_plan_uuid="uuid-xxx")
    assert is_adapt_tariff(tariff) is True


def test_is_adapt_tariff_negative():
    from bot.services.adapt_routing import is_adapt_tariff
    tariff = _make_tariff(adapt_plan_uuid=None)
    assert is_adapt_tariff(tariff) is False


# ── _create_adapt_paid_subscription ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_adapt_paid_subscription_success():
    from bot.services.subscription_service import _create_adapt_paid_subscription
    from bot.models import Subscription, AdaptSubscription

    session = AsyncMock()
    session.flush = AsyncMock()
    session.add = MagicMock()

    user = _make_user()
    tariff = _make_tariff()
    server = _make_server()

    api_resp = _adapt_api_response()

    with (
        patch("bot.services.subscription_service.get_primary_active_server", new=AsyncMock(return_value=server)),
        patch("bot.services.subscription_service._latest_adapt_subscription", new=AsyncMock(return_value=(None, None))),
        patch("bot.services.subscription_service.AdaptAPI") as mock_adapt_cls,
        patch("bot.services.subscription_service.build_adapt_mirror_url", return_value="https://darimiru.ru/vpnbot/adapt-sub/sub-uuid-001"),
        patch("bot.services.subscription_service.get_included_device_slots", new=AsyncMock(return_value=3)),
        patch("bot.services.subscription_service.plog"),
    ):
        mock_api = AsyncMock()
        mock_api.create_subscription = AsyncMock(return_value=api_resp)
        mock_adapt_cls.return_value = mock_api

        sub, key = await _create_adapt_paid_subscription(
            session, user=user, tariff=tariff, platform=MagicMock()
        )

    assert sub is not None
    assert key is not None
    assert "adapt-sub" in key
    # Verify AdaptSubscription was added
    add_calls = [call[0][0] for call in session.add.call_args_list]
    adapt_records = [c for c in add_calls if hasattr(c, "adapt_uuid")]
    assert len(adapt_records) == 1
    assert adapt_records[0].adapt_uuid == "sub-uuid-001"


@pytest.mark.asyncio
async def test_create_adapt_paid_subscription_no_server():
    from bot.services.subscription_service import _create_adapt_paid_subscription
    from bot.services.provisioning_issues import AccessProvisionError

    session = AsyncMock()
    user = _make_user()
    tariff = _make_tariff()

    with (
        patch("bot.services.subscription_service.get_primary_active_server", new=AsyncMock(return_value=None)),
        patch("bot.services.subscription_service.plog"),
    ):
        with pytest.raises(AccessProvisionError):
            await _create_adapt_paid_subscription(
                session, user=user, tariff=tariff, platform=MagicMock()
            )


@pytest.mark.asyncio
async def test_create_adapt_paid_subscription_api_error():
    from bot.services.subscription_service import _create_adapt_paid_subscription
    from bot.services.adapt_api import AdaptAPIError
    from bot.services.provisioning_issues import AccessProvisionError

    session = AsyncMock()
    user = _make_user()
    tariff = _make_tariff()
    server = _make_server()

    with (
        patch("bot.services.subscription_service.get_primary_active_server", new=AsyncMock(return_value=server)),
        patch("bot.services.subscription_service._latest_adapt_subscription", new=AsyncMock(return_value=(None, None))),
        patch("bot.services.subscription_service.AdaptAPI") as mock_adapt_cls,
        patch("bot.services.subscription_service.plog"),
    ):
        mock_api = AsyncMock()
        mock_api.create_subscription = AsyncMock(side_effect=AdaptAPIError("Rate limited", status=429))
        mock_adapt_cls.return_value = mock_api

        with pytest.raises(AccessProvisionError):
            await _create_adapt_paid_subscription(
                session, user=user, tariff=tariff, platform=MagicMock()
            )


@pytest.mark.asyncio
async def test_create_adapt_paid_subscription_missing_uuid():
    from bot.services.subscription_service import _create_adapt_paid_subscription
    from bot.services.provisioning_issues import AccessProvisionError

    session = AsyncMock()
    user = _make_user()
    tariff = _make_tariff()
    server = _make_server()

    incomplete_response = {"plan_uuid": "plan-1"}  # no subscription_uuid

    with (
        patch("bot.services.subscription_service.get_primary_active_server", new=AsyncMock(return_value=server)),
        patch("bot.services.subscription_service._latest_adapt_subscription", new=AsyncMock(return_value=(None, None))),
        patch("bot.services.subscription_service.AdaptAPI") as mock_adapt_cls,
        patch("bot.services.subscription_service.plog"),
    ):
        mock_api = AsyncMock()
        mock_api.create_subscription = AsyncMock(return_value=incomplete_response)
        mock_adapt_cls.return_value = mock_api

        with pytest.raises(AccessProvisionError):
            await _create_adapt_paid_subscription(
                session, user=user, tariff=tariff, platform=MagicMock()
            )


@pytest.mark.asyncio
async def test_create_adapt_paid_subscription_renews_existing_for_same_plan():
    from bot.services.subscription_service import _create_adapt_paid_subscription

    session = AsyncMock()
    user = _make_user()
    tariff = _make_tariff()
    server = _make_server()

    existing = MagicMock()
    existing.id = 77
    existing.expires_at = datetime.utcnow() + timedelta(days=5)
    adapt_record = MagicMock()
    adapt_record.adapt_uuid = "sub-uuid-existing"
    adapt_record.adapt_plan_uuid = tariff.adapt_plan_uuid
    adapt_record.end_date = datetime.utcnow() + timedelta(days=35)

    with (
        patch("bot.services.subscription_service.get_primary_active_server", new=AsyncMock(return_value=server)),
        patch(
            "bot.services.subscription_service._latest_adapt_subscription",
            new=AsyncMock(return_value=(existing, adapt_record)),
        ),
        patch("bot.services.subscription_service.renew_adapt_subscription", new=AsyncMock(return_value=True)) as mock_renew,
        patch("bot.services.subscription_service.AdaptAPI") as mock_adapt_cls,
        patch(
            "bot.services.subscription_service.build_adapt_mirror_url",
            return_value="https://darimiru.ru/vpnbot/adapt-sub/sub-uuid-existing",
        ),
        patch("bot.services.subscription_service.plog"),
    ):
        sub, key = await _create_adapt_paid_subscription(
            session, user=user, tariff=tariff, platform=MagicMock()
        )

    mock_renew.assert_awaited_once_with(session, adapt_record=adapt_record, tariff_days=tariff.days)
    mock_adapt_cls.return_value.create_subscription.assert_not_called()
    assert sub is existing
    assert key == "https://darimiru.ru/vpnbot/adapt-sub/sub-uuid-existing"
    assert existing.vpn_key == key
    assert existing.tariff_id == tariff.id


# ── renew_adapt_subscription ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_renew_adapt_subscription_success():
    from bot.services.subscription_service import renew_adapt_subscription
    from bot.models import AdaptSubscription

    session = AsyncMock()
    adapt_record = MagicMock()
    adapt_record.adapt_uuid = "sub-uuid-001"
    adapt_record.is_frozen = False
    adapt_record.frozen_at = None
    adapt_record.end_date = None

    renew_response = {"subscription_uuid": "sub-uuid-001", "end_date": "2026-09-01T00:00:00Z"}

    with patch("bot.services.subscription_service.AdaptAPI") as mock_cls:
        mock_api = AsyncMock()
        mock_api.renew_subscription = AsyncMock(return_value=renew_response)
        mock_cls.return_value = mock_api

        result = await renew_adapt_subscription(session, adapt_record=adapt_record, tariff_days=30)

    assert result is True
    assert adapt_record.is_frozen is False


@pytest.mark.asyncio
async def test_renew_adapt_subscription_unfreezes_first():
    from bot.services.subscription_service import renew_adapt_subscription

    session = AsyncMock()
    adapt_record = MagicMock()
    adapt_record.adapt_uuid = "sub-uuid-frozen"
    adapt_record.is_frozen = True
    adapt_record.frozen_at = datetime.utcnow()
    adapt_record.end_date = None

    unfreeze_response = {"end_date": "2026-09-01T00:00:00Z"}
    renew_response = {"end_date": "2026-10-01T00:00:00Z"}

    with patch("bot.services.subscription_service.AdaptAPI") as mock_cls:
        mock_api = AsyncMock()
        mock_api.unfreeze_subscription = AsyncMock(return_value=unfreeze_response)
        mock_api.renew_subscription = AsyncMock(return_value=renew_response)
        mock_cls.return_value = mock_api

        result = await renew_adapt_subscription(session, adapt_record=adapt_record, tariff_days=30)

    assert result is True
    mock_api.unfreeze_subscription.assert_awaited_once_with("sub-uuid-frozen")
    mock_api.renew_subscription.assert_awaited_once_with("sub-uuid-frozen")
    assert adapt_record.is_frozen is False
    assert adapt_record.frozen_at is None


@pytest.mark.asyncio
async def test_renew_adapt_subscription_api_error_returns_false():
    from bot.services.subscription_service import renew_adapt_subscription
    from bot.services.adapt_api import AdaptAPIError

    session = AsyncMock()
    adapt_record = MagicMock()
    adapt_record.adapt_uuid = "sub-uuid-001"
    adapt_record.is_frozen = False

    with patch("bot.services.subscription_service.AdaptAPI") as mock_cls:
        mock_api = AsyncMock()
        mock_api.renew_subscription = AsyncMock(side_effect=AdaptAPIError("Server error", status=500))
        mock_cls.return_value = mock_api

        result = await renew_adapt_subscription(session, adapt_record=adapt_record, tariff_days=30)

    assert result is False


# ── create_or_extend_paid_access routing ─────────────────────────────────────

@pytest.mark.asyncio
async def test_create_or_extend_paid_access_dispatches_to_adapt():
    from bot.services.subscription_service import create_or_extend_paid_access

    session = AsyncMock()
    user = _make_user()
    tariff = _make_tariff(adapt_plan_uuid="plan-xyz")

    fake_sub = MagicMock()
    fake_key = "https://darimiru.ru/vpnbot/adapt-sub/some-uuid"

    with patch(
        "bot.services.subscription_service._create_adapt_paid_subscription",
        new=AsyncMock(return_value=(fake_sub, fake_key)),
    ) as mock_adapt:
        sub, key = await create_or_extend_paid_access(
            session, user=user, tariff=tariff, platform=MagicMock()
        )

    mock_adapt.assert_awaited_once()
    assert sub is fake_sub
    assert key == fake_key


@pytest.mark.asyncio
async def test_create_or_extend_paid_access_skips_adapt_for_marzban():
    from bot.services.subscription_service import create_or_extend_paid_access

    session = AsyncMock()
    user = _make_user()
    # Marzban tariff (no adapt_plan_uuid, no vhq_plan_uuid)
    tariff = MagicMock()
    tariff.adapt_plan_uuid = None
    tariff.vhq_plan_uuid = None
    tariff.days = 30
    tariff.label = "Marzban 30d"
    tariff.id = 1

    fake_sub = MagicMock()

    with (
        patch("bot.services.subscription_service._create_adapt_paid_subscription", new=AsyncMock()) as mock_adapt,
        patch("bot.services.subscription_service.create_or_extend_paid_subscription", new=AsyncMock(return_value=(fake_sub, "marzban_key"))) as mock_marzban,
        patch("bot.services.subscription_service.get_vhq_spec_for_tariff", return_value=None),
    ):
        sub, key = await create_or_extend_paid_access(
            session, user=user, tariff=tariff, platform=MagicMock()
        )

    mock_adapt.assert_not_awaited()
    mock_marzban.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_adapt_paid_subscription_custom_renew_same_devices():
    from bot.services.subscription_service import _create_adapt_paid_subscription

    session = AsyncMock()
    user = _make_user()
    tariff = _make_tariff(adapt_plan_uuid="new-plan-uuid", days=14, label="Базовый • 3📱")
    tariff.price_rub = 100
    server = _make_server()

    existing = MagicMock()
    existing.id = 77
    existing.device_slots = 3
    existing.expires_at = datetime.utcnow() + timedelta(days=5)

    adapt_record = MagicMock()
    adapt_record.adapt_uuid = "sub-uuid-existing"
    adapt_record.adapt_plan_uuid = "old-plan-uuid"
    adapt_record.end_date = datetime.utcnow() + timedelta(days=5)

    custom_renew_resp = {
        "end_date": (datetime.utcnow() + timedelta(days=14)).isoformat() + "Z"
    }

    with (
        patch("bot.services.subscription_service.get_primary_active_server", new=AsyncMock(return_value=server)),
        patch("bot.services.subscription_service._latest_adapt_subscription", new=AsyncMock(return_value=(existing, adapt_record))),
        patch("bot.services.subscription_service.AdaptAPI") as mock_adapt_cls,
        patch("bot.services.subscription_service.build_adapt_mirror_url", return_value="https://darimiru.ru/vpnbot/adapt-sub/sub-uuid-existing"),
        patch("bot.services.subscription_service.plog"),
    ):
        mock_api = AsyncMock()
        mock_api.list_plans = AsyncMock(return_value=[
            {"uuid": "old-plan-uuid", "days": 30, "price_usd": "0.5078", "devices": 3},
            {"uuid": "new-plan-uuid", "days": 14, "price_usd": "0.2772", "devices": 3},
        ])
        mock_api.renew_subscription_custom = AsyncMock(return_value=custom_renew_resp)
        mock_adapt_cls.return_value = mock_api

        sub, key = await _create_adapt_paid_subscription(
            session, user=user, tariff=tariff, platform=MagicMock()
        )

    mock_api.renew_subscription_custom.assert_awaited_once_with("sub-uuid-existing", 14)
    assert sub is existing
    assert key == "https://darimiru.ru/vpnbot/adapt-sub/sub-uuid-existing"
    assert existing.vpn_key == key
    assert existing.tariff_id == tariff.id
    assert adapt_record.adapt_plan_uuid == "new-plan-uuid"


@pytest.mark.asyncio
async def test_create_adapt_paid_subscription_custom_renew_expired():
    from bot.services.subscription_service import _create_adapt_paid_subscription

    session = AsyncMock()
    user = _make_user()
    tariff = _make_tariff(adapt_plan_uuid="new-plan-uuid", days=14, label="Базовый • 3📱")
    tariff.price_rub = 100
    server = _make_server()

    existing = MagicMock()
    existing.id = 77
    existing.device_slots = 3
    existing.expires_at = datetime.utcnow() - timedelta(days=2) # Expired

    adapt_record = MagicMock()
    adapt_record.adapt_uuid = "sub-uuid-existing"
    adapt_record.adapt_plan_uuid = "old-plan-uuid"
    adapt_record.end_date = datetime.utcnow() - timedelta(days=2) # Expired

    custom_renew_resp = {
        "end_date": (datetime.utcnow() + timedelta(days=14)).isoformat() + "Z"
    }

    with (
        patch("bot.services.subscription_service.get_primary_active_server", new=AsyncMock(return_value=server)),
        patch("bot.services.subscription_service._latest_adapt_subscription", new=AsyncMock(return_value=(existing, adapt_record))),
        patch("bot.services.subscription_service.AdaptAPI") as mock_adapt_cls,
        patch("bot.services.subscription_service.build_adapt_mirror_url", return_value="https://darimiru.ru/vpnbot/adapt-sub/sub-uuid-existing"),
        patch("bot.services.subscription_service.plog"),
    ):
        mock_api = AsyncMock()
        mock_api.list_plans = AsyncMock(return_value=[
            {"uuid": "old-plan-uuid", "days": 30, "price_usd": "0.5078", "devices": 3},
            {"uuid": "new-plan-uuid", "days": 14, "price_usd": "0.2772", "devices": 3},
        ])
        mock_api.renew_subscription_custom = AsyncMock(return_value=custom_renew_resp)
        mock_adapt_cls.return_value = mock_api

        sub, key = await _create_adapt_paid_subscription(
            session, user=user, tariff=tariff, platform=MagicMock()
        )

    mock_api.renew_subscription_custom.assert_awaited_once_with("sub-uuid-existing", 14)
    assert sub is existing
    assert key == "https://darimiru.ru/vpnbot/adapt-sub/sub-uuid-existing"
    assert existing.vpn_key == key
    assert existing.tariff_id == tariff.id
    assert adapt_record.adapt_plan_uuid == "new-plan-uuid"


@pytest.mark.asyncio
async def test_create_adapt_paid_subscription_fallback_unprofitable_plan_change():
    from bot.services.subscription_service import _create_adapt_paid_subscription

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    user = _make_user()
    tariff = _make_tariff(adapt_plan_uuid="new-plan-uuid", days=30, label="Базовый • 3📱")
    tariff.price_rub = 125  # Rubles
    server = _make_server()

    existing = MagicMock()
    existing.id = 77
    existing.device_slots = 3
    existing.expires_at = datetime.utcnow() - timedelta(days=2) # Expired

    adapt_record = MagicMock()
    adapt_record.adapt_uuid = "sub-uuid-existing"
    adapt_record.adapt_plan_uuid = "old-plan-uuid"
    adapt_record.end_date = datetime.utcnow() - timedelta(days=2) # Expired

    api_resp = _adapt_api_response(adapt_uuid="sub-uuid-new-fresh", days=30, devices=3)

    with (
        patch("bot.services.subscription_service.get_primary_active_server", new=AsyncMock(return_value=server)),
        patch("bot.services.subscription_service._latest_adapt_subscription", new=AsyncMock(return_value=(existing, adapt_record))),
        patch("bot.services.subscription_service.AdaptAPI") as mock_adapt_cls,
        patch("bot.services.subscription_service.build_adapt_mirror_url", return_value="https://darimiru.ru/vpnbot/adapt-sub/sub-uuid-new-fresh"),
        patch("bot.services.subscription_service.get_included_device_slots", new=AsyncMock(return_value=3)),
        patch("bot.services.subscription_service.plog"),
    ):
        mock_api = AsyncMock()
        mock_api.list_plans = AsyncMock(return_value=[
            {"uuid": "old-plan-uuid", "days": 14, "price_usd": "0.50", "devices": 3},
            {"uuid": "new-plan-uuid", "days": 30, "price_usd": "0.20", "devices": 3},
        ])
        mock_api.create_subscription = AsyncMock(return_value=api_resp)
        mock_adapt_cls.return_value = mock_api

        sub, key = await _create_adapt_paid_subscription(
            session, user=user, tariff=tariff, platform=MagicMock()
        )

    mock_api.create_subscription.assert_awaited_once_with("new-plan-uuid", external_user_id="123456")
    mock_api.renew_subscription_custom.assert_not_called()
    assert sub is not None
    assert sub is not existing
    assert key == "https://darimiru.ru/vpnbot/adapt-sub/sub-uuid-new-fresh"


@pytest.mark.asyncio
async def test_create_adapt_paid_subscription_fallback_device_mismatch():
    from bot.services.subscription_service import _create_adapt_paid_subscription

    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    user = _make_user()
    tariff = _make_tariff(adapt_plan_uuid="new-plan-3devices-uuid", days=30, label="Базовый • 3📱")
    tariff.price_rub = 155
    server = _make_server()

    existing = MagicMock()
    existing.id = 77
    existing.device_slots = 1 # Mismatch (current is 1, new is 3)
    existing.expires_at = datetime.utcnow() + timedelta(days=5)

    adapt_record = MagicMock()
    adapt_record.adapt_uuid = "sub-uuid-existing"
    adapt_record.adapt_plan_uuid = "old-plan-1device-uuid"
    adapt_record.end_date = datetime.utcnow() + timedelta(days=5)

    # Devices count is different, pricing doesn't check
    session.scalar = AsyncMock()

    api_resp = _adapt_api_response(adapt_uuid="sub-uuid-new-fresh", days=30, devices=3)

    with (
        patch("bot.services.subscription_service.get_primary_active_server", new=AsyncMock(return_value=server)),
        patch("bot.services.subscription_service._latest_adapt_subscription", new=AsyncMock(return_value=(existing, adapt_record))),
        patch("bot.services.subscription_service.AdaptAPI") as mock_adapt_cls,
        patch("bot.services.subscription_service.build_adapt_mirror_url", return_value="https://darimiru.ru/vpnbot/adapt-sub/sub-uuid-new-fresh"),
        patch("bot.services.subscription_service.get_included_device_slots", new=AsyncMock(return_value=3)),
        patch("bot.services.subscription_service.plog"),
    ):
        mock_api = AsyncMock()
        mock_api.create_subscription = AsyncMock(return_value=api_resp)
        mock_adapt_cls.return_value = mock_api

        sub, key = await _create_adapt_paid_subscription(
            session, user=user, tariff=tariff, platform=MagicMock()
        )

    mock_api.create_subscription.assert_awaited_once_with("new-plan-3devices-uuid", external_user_id="123456")
    mock_api.renew_subscription_custom.assert_not_called()
    assert sub is not None
    assert sub is not existing
    assert key == "https://darimiru.ru/vpnbot/adapt-sub/sub-uuid-new-fresh"

