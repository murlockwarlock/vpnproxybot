"""Tests for Adapt webstore fulfillment (webstore/routes.py)."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_order(tariff_key="adapt_30", days=30, order_id="order-test-1"):
    order = MagicMock()
    order.order_id = order_id
    order.tariff_key = tariff_key
    order.tariff_label = "Adapt 30 дней"
    order.days = days
    order.amount_rub = 199
    order.contact = "user@test.com"
    order.status = "paid"
    order.profile_token = "tok123"
    order.failure_message = None
    order.failure_reason = None
    return order


def _make_session_without_primary():
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
    return session


# ── _fulfill_adapt_order ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fulfill_adapt_order_success():
    from webstore.routes import _fulfill_adapt_order

    order = _make_order()
    adapt_uuid = "adapt-sub-uuid"

    with (
        patch("webstore.routes.AdaptAPI") as mock_cls,
        patch("webstore.routes.build_adapt_mirror_url", return_value="https://darimiru.ru/vpnbot/adapt-sub/adapt-sub-uuid"),
    ):
        mock_api = AsyncMock()
        mock_api.create_subscription = AsyncMock(return_value={
            "uuid": adapt_uuid,
            "subscription_url": f"https://network-api.adaptgroup.app/sub/{adapt_uuid}",
        })
        mock_cls.return_value = mock_api

        await _fulfill_adapt_order(_make_session_without_primary(), order, "plan-test-uuid")

    assert order.status == "delivered"
    assert order.marzban_username == f"adapt_{adapt_uuid}"
    assert "adapt-sub" in order.subscription_url
    assert order.delivered_at is not None
    assert order.access_expires_at is not None


@pytest.mark.asyncio
async def test_fulfill_adapt_order_uses_api_end_date():
    from webstore.routes import _fulfill_adapt_order

    order = _make_order(days=14)
    adapt_uuid = "adapt-sub-uuid"

    with (
        patch("webstore.routes.AdaptAPI") as mock_cls,
        patch("webstore.routes.build_adapt_mirror_url", return_value="https://darimiru.ru/vpnbot/adapt-sub/adapt-sub-uuid"),
    ):
        mock_api = AsyncMock()
        mock_api.create_subscription = AsyncMock(return_value={
            "uuid": adapt_uuid,
            "end_date": "2026-06-02T12:00:00Z",
            "days": 30,
        })
        mock_cls.return_value = mock_api

        await _fulfill_adapt_order(_make_session_without_primary(), order, "plan-test-uuid")

    assert order.status == "delivered"
    assert order.access_expires_at == datetime(2026, 6, 2, 12, 0, 0)


@pytest.mark.asyncio
async def test_fulfill_adapt_order_uses_api_days_when_end_date_missing():
    from webstore.routes import _fulfill_adapt_order

    order = _make_order(days=14)
    adapt_uuid = "adapt-sub-uuid"

    with (
        patch("webstore.routes.AdaptAPI") as mock_cls,
        patch("webstore.routes.build_adapt_mirror_url", return_value="https://darimiru.ru/vpnbot/adapt-sub/adapt-sub-uuid"),
        patch("webstore.routes.datetime") as mock_datetime,
    ):
        now = datetime(2026, 5, 19, 10, 0, 0)
        mock_datetime.utcnow.return_value = now
        mock_datetime.fromisoformat = datetime.fromisoformat
        mock_api = AsyncMock()
        mock_api.create_subscription = AsyncMock(return_value={
            "uuid": adapt_uuid,
            "days": 30,
        })
        mock_cls.return_value = mock_api

        await _fulfill_adapt_order(_make_session_without_primary(), order, "plan-test-uuid")

    assert order.status == "delivered"
    assert order.access_expires_at == datetime(2026, 6, 18, 10, 0, 0)


@pytest.mark.asyncio
async def test_fulfill_adapt_order_api_error():
    from webstore.routes import _fulfill_adapt_order
    from bot.services.adapt_api import AdaptAPIError

    order = _make_order()

    with patch("webstore.routes.AdaptAPI") as mock_cls:
        mock_api = AsyncMock()
        mock_api.create_subscription = AsyncMock(side_effect=AdaptAPIError("Quota exceeded", status=402))
        mock_cls.return_value = mock_api

        with patch("webstore.routes._mark_order_failed", new=AsyncMock()):
            await _fulfill_adapt_order(_make_session_without_primary(), order, "plan-uuid")

    # Status should not be changed to "delivered"
    assert order.status == "paid"


@pytest.mark.asyncio
async def test_fulfill_adapt_order_missing_uuid():
    from webstore.routes import _fulfill_adapt_order

    order = _make_order()

    with (
        patch("webstore.routes.AdaptAPI") as mock_cls,
        patch("webstore.routes._mark_order_failed", new=AsyncMock()),
    ):
        mock_api = AsyncMock()
        mock_api.create_subscription = AsyncMock(return_value={"plan_uuid": "ok"})  # no uuid
        mock_cls.return_value = mock_api

        await _fulfill_adapt_order(_make_session_without_primary(), order, "plan-uuid")

    assert order.status == "paid"


@pytest.mark.asyncio
async def test_fulfill_adapt_order_renews_existing_same_tariff():
    from webstore.routes import _fulfill_adapt_order

    adapt_uuid = "770fa622-a4bd-63f6-c938-668877662221"
    order = _make_order(tariff_key="adapt_30", order_id="renew-order")
    primary = _make_order(tariff_key="adapt_30", order_id="primary-order")
    primary.id = 10
    order.id = 11
    primary.marzban_username = f"adapt_{adapt_uuid}"
    primary.subscription_url = f"https://darimiru.ru/vpnbot/adapt-sub/{adapt_uuid}"
    order.purchase_action = "renew"
    order.target_order_id = primary.order_id

    session = AsyncMock()
    session.scalar = AsyncMock(return_value=primary)

    with patch("webstore.routes.AdaptAPI") as mock_cls:
        mock_api = AsyncMock()
        mock_api.renew_subscription = AsyncMock(return_value={"end_date": "2026-07-01T00:00:00Z"})
        mock_api.get_status = AsyncMock(side_effect=[
            {"plan_uuid": "plan-test-uuid", "end_date": "2026-06-01T00:00:00Z", "devices": 3},
            {"plan_uuid": "plan-test-uuid", "end_date": "2026-07-01T00:00:00Z", "devices": 3},
        ])
        mock_api.list_plans = AsyncMock(return_value=[{"uuid": "plan-test-uuid", "devices": 3}])
        mock_api.create_subscription = AsyncMock()
        mock_cls.return_value = mock_api

        await _fulfill_adapt_order(session, order, "plan-test-uuid")

    mock_api.renew_subscription.assert_awaited_once_with(adapt_uuid)
    mock_api.create_subscription.assert_not_called()
    assert order.status == "delivered"
    assert order.marzban_username == primary.marzban_username
    assert order.subscription_url == primary.subscription_url
    assert order.access_expires_at == datetime(2026, 7, 1, 0, 0, 0)


@pytest.mark.asyncio
async def test_fulfill_adapt_order_creates_new_for_different_tariff():
    from webstore.routes import _fulfill_adapt_order

    order = _make_order(tariff_key="adapt_30", order_id="new-plan-order")
    primary = _make_order(tariff_key="adapt_2", order_id="primary-order")
    primary.id = 10
    order.id = 11
    primary.marzban_username = "adapt_existing-uuid"
    primary.subscription_url = "https://darimiru.ru/vpnbot/adapt-sub/existing-uuid"

    session = AsyncMock()

    with (
        patch("webstore.routes._get_latest_web_access_order", new=AsyncMock(return_value=primary)),
        patch("webstore.routes.AdaptAPI") as mock_cls,
        patch("webstore.routes.build_adapt_mirror_url", return_value="https://darimiru.ru/vpnbot/adapt-sub/new-uuid"),
    ):
        mock_api = AsyncMock()
        mock_api.renew_subscription = AsyncMock()
        mock_api.create_subscription = AsyncMock(return_value={"subscription_uuid": "new-uuid"})
        mock_cls.return_value = mock_api

        await _fulfill_adapt_order(session, order, "plan-test-uuid")

    mock_api.renew_subscription.assert_not_called()
    mock_api.create_subscription.assert_awaited_once()
    assert order.status == "delivered"
    assert order.marzban_username == "adapt_new-uuid"


# ── _fulfill_order routing ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fulfill_order_dispatches_adapt():
    """_fulfill_order should call _fulfill_adapt_order when tariff has adapt_plan_uuid."""
    from webstore.routes import _fulfill_order

    order = _make_order(tariff_key="adapt_30")
    session = AsyncMock()

    with (
        patch("webstore.routes.get_store_tariffs_by_key", return_value={"adapt_30": {"adapt_plan_uuid": "plan-test-uuid", "key": "adapt_30"}}),
        patch("webstore.routes.get_vhq_spec_for_store_tariff", return_value=None),
        patch("webstore.routes._fulfill_adapt_order", new=AsyncMock()) as mock_adapt,
        patch("webstore.routes._fulfill_vhq_order", new=AsyncMock()) as mock_vhq,
    ):
        await _fulfill_order(session, order)

    mock_adapt.assert_awaited_once_with(session, order, "plan-test-uuid")
    mock_vhq.assert_not_awaited()


@pytest.mark.asyncio
async def test_fulfill_order_does_not_dispatch_adapt_for_marzban():
    """_fulfill_order should NOT call adapt for marzban tariff."""
    from webstore.routes import _fulfill_order

    order = _make_order(tariff_key="vpn_30")
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))

    tariff_cfg = {"key": "vpn_30", "provider": "marzban", "days": 30}

    with (
        patch("webstore.routes.get_store_tariffs_by_key", return_value={"vpn_30": tariff_cfg}),
        patch("webstore.routes.get_vhq_spec_for_store_tariff", return_value=None),
        patch("webstore.routes._fulfill_adapt_order", new=AsyncMock()) as mock_adapt,
        patch("webstore.routes.MarzbanClient") as mock_mc,
    ):
        mc_instance = AsyncMock()
        mc_instance.__aenter__ = AsyncMock(return_value=mc_instance)
        mc_instance.__aexit__ = AsyncMock(return_value=False)
        mc_instance.create_user = AsyncMock(return_value={"username": "web_order-test-1"})
        mc_instance.get_subscription_url = AsyncMock(return_value="https://marzban.test/sub/abc")
        mock_mc.return_value = mc_instance

        await _fulfill_order(session, order)

    mock_adapt.assert_not_awaited()


# ── Webstore config: adapt_plan_uuid in tariff dict ──────────────────────────

def test_tariff_config_supports_adapt_plan_uuid():
    """Verify tariff dicts can carry adapt_plan_uuid without error."""
    tariff = {
        "key": "adapt_30",
        "label": "Adapt 30 дней",
        "days": 30,
        "price_rub": 199,
        "description": "Подключение через Adapt",
        "badge": "",
        "provider": "adapt",
        "adapt_plan_uuid": "plan-test-uuid-abc",
    }
    assert tariff.get("adapt_plan_uuid") == "plan-test-uuid-abc"


def test_webstore_config_adapt_api_settings():
    """WebStoreSettings should have adapt_api_id and adapt_api_key."""
    import os
    os.environ["ADAPT_API_ID"] = "12"
    os.environ["ADAPT_API_KEY"] = "testkey"

    from webstore.config import WebStoreSettings
    s = WebStoreSettings()
    assert s.adapt_api_id == "12"
    assert s.adapt_api_key == "testkey"
