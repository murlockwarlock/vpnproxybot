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
        mock_api = AsyncMock()
        mock_api.get_status = AsyncMock(return_value={
            "plan_uuid": tariff.adapt_plan_uuid,
            "end_date": existing.expires_at.isoformat(),
        })
        mock_adapt_cls.return_value = mock_api
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
async def test_create_adapt_paid_subscription_upgrades_without_second_renew():
    from bot.services.subscription_service import _create_adapt_paid_subscription

    session = AsyncMock()
    user = _make_user()
    tariff = _make_tariff(adapt_plan_uuid="new-plan-uuid", days=30, label="Базовый • 30 дн • 3📱")
    tariff.price_rub = 155
    server = _make_server()

    existing = MagicMock()
    existing.id = 77
    existing.device_slots = 3
    existing.expires_at = datetime.utcnow() + timedelta(days=5)

    adapt_record = MagicMock()
    adapt_record.adapt_uuid = "sub-uuid-existing"
    adapt_record.adapt_plan_uuid = "old-plan-uuid"
    adapt_record.end_date = datetime.utcnow() + timedelta(days=5)

    with (
        patch("bot.services.subscription_service.get_primary_active_server", new=AsyncMock(return_value=server)),
        patch("bot.services.subscription_service._adapt_subscription_by_id", new=AsyncMock(return_value=(existing, adapt_record))),
        patch("bot.services.subscription_service.AdaptAPI") as mock_adapt_cls,
        patch("bot.services.subscription_service.renew_adapt_subscription", new=AsyncMock(return_value=True)) as mock_renew,
        patch("bot.services.subscription_service.build_adapt_mirror_url", return_value="https://darimiru.ru/vpnbot/adapt-sub/sub-uuid-existing"),
        patch("bot.services.subscription_service.plog"),
    ):
        mock_api = AsyncMock()
        new_end = datetime.utcnow() + timedelta(days=30)
        mock_api.upgrade_subscription = AsyncMock(return_value={"success": True, "devices": 5})
        mock_api.get_status = AsyncMock(return_value={"end_date": new_end.isoformat(), "devices": 5})
        mock_adapt_cls.return_value = mock_api

        sub, key = await _create_adapt_paid_subscription(
            session, user=user, tariff=tariff, platform=MagicMock(),
            purchase_action="upgrade", target_subscription_id=existing.id,
        )

    mock_api.upgrade_subscription.assert_awaited_once_with("sub-uuid-existing", "new-plan-uuid")
    mock_renew.assert_not_awaited()
    assert sub is existing
    assert key == "https://darimiru.ru/vpnbot/adapt-sub/sub-uuid-existing"
    assert existing.tariff_id == tariff.id
    assert adapt_record.adapt_plan_uuid == "new-plan-uuid"
    assert existing.device_slots == 5


@pytest.mark.asyncio
async def test_adapt_upgrade_does_not_mutate_when_preflight_status_fails():
    from bot.services.subscription_service import _create_adapt_paid_subscription

    session = AsyncMock()
    user = _make_user()
    tariff = _make_tariff(adapt_plan_uuid="new-plan-uuid", days=30)
    server = _make_server()
    old_end = datetime.utcnow() + timedelta(days=2)
    existing = MagicMock(id=78, device_slots=3, expires_at=old_end)
    adapt_record = MagicMock(
        adapt_uuid="sub-uuid-existing",
        adapt_plan_uuid="old-plan-uuid",
        end_date=old_end,
    )

    with (
        patch("bot.services.subscription_service.get_primary_active_server", new=AsyncMock(return_value=server)),
        patch("bot.services.subscription_service._adapt_subscription_by_id", new=AsyncMock(return_value=(existing, adapt_record))),
        patch("bot.services.subscription_service.AdaptAPI") as mock_adapt_cls,
        patch("bot.services.subscription_service.build_adapt_mirror_url", return_value="stable-key"),
        patch("bot.services.subscription_service.plog"),
    ):
        mock_api = AsyncMock()
        mock_api.upgrade_subscription = AsyncMock(return_value={"success": True})
        mock_api.get_status = AsyncMock(side_effect=RuntimeError("temporary status failure"))
        mock_api.list_plans = AsyncMock(return_value=[{"uuid": "new-plan-uuid", "devices": 5}])
        mock_adapt_cls.return_value = mock_api

        with pytest.raises(Exception, match="Adapt upgrade failed"):
            await _create_adapt_paid_subscription(
                session,
                user=user,
                tariff=tariff,
                platform=MagicMock(),
                purchase_action="upgrade",
                target_subscription_id=existing.id,
            )

    mock_api.upgrade_subscription.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_new_adapt_purchase_never_reuses_existing_uuid():
    from bot.services.subscription_service import _create_adapt_paid_subscription

    session = AsyncMock()
    user = _make_user()
    tariff = _make_tariff()
    server = _make_server()
    with (
        patch("bot.services.subscription_service.get_primary_active_server", new=AsyncMock(return_value=server)),
        patch("bot.services.subscription_service._latest_adapt_subscription", new=AsyncMock()) as latest,
        patch("bot.services.subscription_service._adapt_create_new_subscription", new=AsyncMock(return_value=(MagicMock(), "new-key"))) as create_new,
    ):
        _, key = await _create_adapt_paid_subscription(
            session, user=user, tariff=tariff, platform=MagicMock(), purchase_action="new"
        )

    latest.assert_not_awaited()
    create_new.assert_awaited_once()
    assert key == "new-key"


@pytest.mark.asyncio
async def test_retry_reconciles_paid_renew_without_second_renew():
    from bot.services.subscription_service import _create_adapt_paid_subscription

    session = AsyncMock()
    user = _make_user()
    tariff = _make_tariff()
    server = _make_server()
    baseline = datetime.utcnow() + timedelta(days=5)
    provider_end = baseline + timedelta(days=30)
    existing = MagicMock(id=90, expires_at=baseline, device_slots=3)
    adapt_record = MagicMock(
        adapt_uuid="sub-safe-retry",
        adapt_plan_uuid=tariff.adapt_plan_uuid,
        end_date=baseline,
    )
    payment = MagicMock(
        id=501,
        provisioning_baseline_expires_at=baseline,
        provisioning_baseline_plan_uuid=tariff.adapt_plan_uuid,
        provisioning_failure_code="adapt_renew_failed",
    )

    with (
        patch("bot.services.subscription_service.get_primary_active_server", new=AsyncMock(return_value=server)),
        patch("bot.services.subscription_service._adapt_subscription_by_id", new=AsyncMock(return_value=(existing, adapt_record))),
        patch("bot.services.subscription_service.AdaptAPI") as api_cls,
        patch("bot.services.subscription_service.renew_adapt_subscription", new=AsyncMock()) as renew,
        patch("bot.services.subscription_service.build_adapt_mirror_url", return_value="stable-key"),
        patch("bot.services.subscription_service.plog"),
    ):
        api = AsyncMock()
        api.get_status.return_value = {
            "plan_uuid": tariff.adapt_plan_uuid,
            "end_date": provider_end.isoformat(),
            "devices": 5,
        }
        api_cls.return_value = api
        result, _ = await _create_adapt_paid_subscription(
            session,
            user=user,
            tariff=tariff,
            platform=MagicMock(),
            purchase_action="renew",
            target_subscription_id=existing.id,
            provisioning_payment=payment,
        )

    renew.assert_not_awaited()
    assert result is existing
    assert existing.expires_at == provider_end


@pytest.mark.asyncio
async def test_ambiguous_new_create_waits_for_webhook_instead_of_duplicating():
    from bot.services.provisioning_issues import AccessProvisionError
    from bot.services.subscription_service import _adapt_create_new_subscription

    payment = MagicMock(
        id=777,
        provisioning_operation_id="tgpay_777",
        provisioning_failure_code="adapt_runtime",
    )
    with patch("bot.services.subscription_service.AdaptAPI") as api_cls:
        with pytest.raises(AccessProvisionError, match="Waiting for Adapt subs.created webhook"):
            await _adapt_create_new_subscription(
                AsyncMock(),
                user=_make_user(),
                tariff=_make_tariff(),
                platform=MagicMock(),
                server=_make_server(),
                provisioning_payment=payment,
            )
    api_cls.return_value.create_subscription.assert_not_called()
