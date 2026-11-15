from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.config import settings
from bot.handlers.admin import _build_unified_search_page
from bot.services.adapt_api import AdaptAPIError
from webstore import routes
from webstore.models import Base, WebOrder


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture(autouse=True)
def mock_tariffs(monkeypatch):
    test_tariffs = {
        "basic-30": {
            "key": "basic-30",
            "title": "Базовый 30 дней",
            "price_rub": 260,
            "days": 30,
            "provider": "adapt",
            "adapt_plan_uuid": "1fa99c54-43e2-490b-9d18-d66f7b1017c4",
            "device_limit": 10,
        },
        "demo": {
            "key": "demo",
            "title": "Тестовый период",
            "price_rub": 0,
            "days": 3,
            "provider": "adapt",
            "adapt_plan_uuid": "demo-plan-uuid",
            "device_limit": 1,
        },
    }
    monkeypatch.setattr(routes, "get_store_tariffs_by_key", lambda: test_tariffs)
    monkeypatch.setattr(routes, "get_store_tariffs", lambda: list(test_tariffs.values()))
    monkeypatch.setattr(settings, "adapt_demo_plan_uuid", "demo-plan-uuid")
    monkeypatch.setattr(routes, "_notify_admins_webstore", AsyncMock())
    monkeypatch.setattr(routes, "_notify_linked_webstore_user", AsyncMock())


def _adapt_renew_order(order_id: str, adapt_uuid: str, plan_uuid: str, expiry: datetime) -> WebOrder:
    return WebOrder(
        order_id=order_id,
        profile_token=f"profile-{order_id}",
        tariff_key="basic-30",
        tariff_label="Базовый",
        days=30,
        amount_rub=260,
        status="paid",
        purchase_action="renew",
        target_order_id=f"uuid:{adapt_uuid}",
        target_snapshot_plan_uuid=plan_uuid,
        target_snapshot_expires_at=expiry,
    )


def _adapt_upgrade_order(
    order_id: str, adapt_uuid: str, baseline_plan_uuid: str, expiry: datetime
) -> WebOrder:
    return WebOrder(
        order_id=order_id,
        profile_token=f"profile-{order_id}",
        tariff_key="basic-30",
        tariff_label="Базовый",
        days=30,
        amount_rub=260,
        status="paid",
        purchase_action="upgrade",
        target_order_id=f"uuid:{adapt_uuid}",
        target_snapshot_plan_uuid=baseline_plan_uuid,
        target_snapshot_expires_at=expiry,
    )


@pytest.mark.asyncio
async def test_a_current_incident_same_paid_plan_renews_and_replays_safely(session_factory, monkeypatch):
    """Test A: Current incident same paid plan -> renew action, renew_subscription called exactly 1 time,

    upgrade_subscription 0 times, expiry extended, replay 0 second mutation.
    """
    adapt_uuid = "e51c42a3-745d-4316-8894-aedeb4926965"
    old_expiry = datetime(2026, 11, 11, 6, 6, 22)
    new_expiry = datetime(2026, 12, 12, 6, 6, 22)
    plan_uuid = "1fa99c54-43e2-490b-9d18-d66f7b1017c4"

    async with session_factory() as session:
        # Previous delivered paid order that originated from a demo upgrade
        primary = WebOrder(
            order_id="prev-paid-order",
            profile_token="profile-1",
            tariff_key="basic-30",
            tariff_label="Базовый",
            days=30,
            amount_rub=260,
            status="delivered",
            purchase_action="upgrade",
            target_snapshot_plan_uuid="demo-plan-uuid",  # Old historical snapshot
            marzban_username=f"adapt_{adapt_uuid}",
            subscription_url=f"https://darimiru.xyz/vpnbot/adapt-sub/{adapt_uuid}",
            access_expires_at=old_expiry,
        )
        # New renewal order
        new_order = WebOrder(
            order_id="renew-order-314",
            profile_token="profile-1",
            tariff_key="basic-30",
            tariff_label="Базовый",
            days=30,
            amount_rub=260,
            status="paid",
            purchase_action="renew",
            target_order_id="prev-paid-order",
            target_snapshot_plan_uuid=plan_uuid,
            target_snapshot_expires_at=old_expiry,
            fulfillment_attempts=1,
        )
        session.add_all([primary, new_order])
        await session.commit()

        # Step 1: Verify current plan is identified from paid tariff, not historical snapshot
        resolved_plan = routes._web_order_current_adapt_plan_uuid(primary)
        assert resolved_plan == plan_uuid, f"Expected {plan_uuid}, got {resolved_plan}"

        # Step 2: Setup Adapt mock
        api = AsyncMock()
        api.get_status.side_effect = [
            # Before mutation: reports baseline
            {"plan_uuid": plan_uuid, "end_date": old_expiry.isoformat(), "devices": 10},
            # After mutation (verification): reports extended expiry
            {"plan_uuid": plan_uuid, "end_date": new_expiry.isoformat(), "devices": 10},
        ]
        api.renew_subscription.return_value = {
            "plan_uuid": plan_uuid,
            "end_date": new_expiry.isoformat(),
            "devices": 10,
        }
        monkeypatch.setattr(routes, "AdaptAPI", lambda: api)

        # Step 3: Fulfill
        await routes._fulfill_adapt_order(session, new_order, plan_uuid)

        # Assertions
        assert new_order.status == "delivered"
        assert new_order.purchase_action == "renew"
        assert new_order.access_expires_at == new_expiry
        api.renew_subscription.assert_awaited_once_with(adapt_uuid)
        api.upgrade_subscription.assert_not_awaited()

        # Step 4: Replay (subsequent reconciliation pass) -> must NOT mutate again (0 second POST)
        api.get_status.side_effect = [
            {"plan_uuid": plan_uuid, "end_date": new_expiry.isoformat(), "devices": 10},
        ]
        api.renew_subscription.reset_mock()

        replay_order = WebOrder(
            order_id="renew-order-replay",
            profile_token="profile-1",
            tariff_key="basic-30",
            tariff_label="Базовый",
            days=30,
            amount_rub=260,
            status="paid",
            purchase_action="renew",
            target_order_id="prev-paid-order",
            target_snapshot_plan_uuid=plan_uuid,
            target_snapshot_expires_at=old_expiry,
            fulfillment_attempts=2,
        )
        session.add(replay_order)
        await session.commit()

        await routes._fulfill_adapt_order(session, replay_order, plan_uuid)
        assert replay_order.status == "delivered"
        api.renew_subscription.assert_not_awaited()
        api.upgrade_subscription.assert_not_awaited()


@pytest.mark.asyncio
async def test_b_historical_demo_snapshot_does_not_misclassify_paid_order(session_factory):
    """Test B: Historical demo snapshot in previous delivered paid order does not misclassify current paid plan."""
    plan_uuid = "1fa99c54-43e2-490b-9d18-d66f7b1017c4"
    order = WebOrder(
        order_id="historical-paid-order",
        profile_token="profile-b",
        tariff_key="basic-30",
        tariff_label="Базовый 30 дней",
        days=30,
        amount_rub=260,
        status="delivered",
        purchase_action="upgrade",
        target_snapshot_plan_uuid="demo-plan-uuid",  # Historical artifact from demo upgrade
    )

    # 1. _web_order_current_adapt_plan_uuid must return the paid plan UUID
    current_uuid = routes._web_order_current_adapt_plan_uuid(order)
    assert current_uuid == plan_uuid

    # 2. _serialize_orders must serialize as paid (not trial)
    serialized = routes._serialize_orders([order])
    assert len(serialized) == 1
    assert serialized[0]["is_trial"] is False
    assert serialized[0]["adapt_plan_uuid"] == plan_uuid


@pytest.mark.asyncio
async def test_c_same_plan_alone_cannot_falsely_prove_fulfillment(session_factory, monkeypatch):
    """Test C: Same plan alone cannot falsely prove fulfillment (same plan + same expiry -> does NOT deliver)."""
    adapt_uuid = "e51c42a3-745d-4316-8894-aedeb4926965"
    old_expiry = datetime(2026, 11, 11, 6, 6, 22)
    plan_uuid = "1fa99c54-43e2-490b-9d18-d66f7b1017c4"

    async with session_factory() as session:
        # Misclassified or unextended upgrade attempt where plan already matches baseline
        order = WebOrder(
            order_id="unextended-upgrade",
            profile_token="profile-c",
            tariff_key="basic-30",
            tariff_label="Базовый",
            days=30,
            amount_rub=260,
            status="paid",
            purchase_action="upgrade",
            target_order_id=f"uuid:{adapt_uuid}",
            target_snapshot_plan_uuid=plan_uuid,
            target_snapshot_expires_at=old_expiry,
            fulfillment_attempts=1,
        )
        session.add(order)
        await session.commit()

        api = AsyncMock()
        # Upstream status shows SAME plan and UNEXTENDED expiry
        api.get_status.return_value = {
            "plan_uuid": plan_uuid,
            "end_date": old_expiry.isoformat(),
            "devices": 10,
        }
        monkeypatch.setattr(routes, "AdaptAPI", lambda: api)

        await routes._fulfill_adapt_order(session, order, plan_uuid)

        # Must fail closed: order NOT delivered
        assert order.status == "failed"
        assert order.failure_code == "adapt_upgrade_same_plan_unextended"
        api.renew_subscription.assert_not_awaited()
        api.upgrade_subscription.assert_not_awaited()


@pytest.mark.asyncio
async def test_upgrade_not_enough_days_fails_closed_without_composite_fallback(
    session_factory, monkeypatch
):
    """A near-expiry upgrade must not hide a custom renewal behind a second upgrade POST."""
    adapt_uuid = "e51c42a3-745d-4316-8894-aedeb4926965"
    baseline_plan = "old-plan-uuid"
    target_plan = "1fa99c54-43e2-490b-9d18-d66f7b1017c4"
    baseline_expiry = datetime(2027, 1, 11, 6, 6, 22)
    admin_notify_mock = AsyncMock()
    monkeypatch.setattr(routes, "_notify_admins_webstore", admin_notify_mock)

    api = AsyncMock()
    api.get_status.return_value = {
        "plan_uuid": baseline_plan,
        "end_date": baseline_expiry.isoformat(),
        "devices": 3,
    }
    api.upgrade_subscription.side_effect = AdaptAPIError(
        "Not enough days remaining to upgrade", status=400
    )
    monkeypatch.setattr(routes, "AdaptAPI", lambda: api)

    async with session_factory() as session:
        order = _adapt_upgrade_order(
            "upgrade-not-enough-days", adapt_uuid, baseline_plan, baseline_expiry
        )
        session.add(order)
        await session.commit()

        await routes.attempt_fulfill_order(session, order)

        assert order.status == "failed"
        assert order.failure_code == "adapt_upgrade_reactivation_required"
        assert order.adapt_mutation_state == routes._ADAPT_MUTATION_FAILED_CLOSED
        assert order.next_fulfillment_retry_at is None
        assert api.upgrade_subscription.await_count == 1
        api.renew_subscription_custom.assert_not_awaited()
        admin_notify_mock.assert_awaited_once()

        await routes.attempt_fulfill_order(session, order)
        assert api.upgrade_subscription.await_count == 1
        api.renew_subscription_custom.assert_not_awaited()
        assert admin_notify_mock.await_count == 1


@pytest.mark.asyncio
async def test_upgrade_intermediate_expiry_change_fails_closed_without_recovery_post(
    session_factory, monkeypatch
):
    """An old initial-inflight row with A/E2 is not an exact safe-retry baseline A/E."""
    adapt_uuid = "770fa622-a4bd-63f6-c938-668877664447"
    baseline_plan = "old-plan-uuid"
    target_plan = "1fa99c54-43e2-490b-9d18-d66f7b1017c4"
    baseline_expiry = datetime(2027, 1, 11, 6, 6, 22)
    intermediate_expiry = datetime(2027, 2, 10, 6, 6, 22)
    admin_notify_mock = AsyncMock()
    monkeypatch.setattr(routes, "_notify_admins_webstore", admin_notify_mock)

    api = AsyncMock()
    api.get_status.return_value = {
        "plan_uuid": baseline_plan,
        "end_date": intermediate_expiry.isoformat(),
        "devices": 3,
    }
    monkeypatch.setattr(routes, "AdaptAPI", lambda: api)

    async with session_factory() as session:
        order = _adapt_upgrade_order(
            "upgrade-intermediate-expiry", adapt_uuid, baseline_plan, baseline_expiry
        )
        order.adapt_mutation_state = routes._ADAPT_MUTATION_INITIAL_INFLIGHT
        order.fulfillment_attempts = 1
        session.add(order)
        await session.commit()

        await routes.attempt_fulfill_order(session, order)

        assert order.status == "failed"
        assert order.failure_code == "adapt_upgrade_reactivation_required"
        assert order.adapt_mutation_state == routes._ADAPT_MUTATION_FAILED_CLOSED
        assert order.next_fulfillment_retry_at is None
        api.upgrade_subscription.assert_not_awaited()
        api.renew_subscription_custom.assert_not_awaited()
        admin_notify_mock.assert_awaited_once()

        await routes.attempt_fulfill_order(session, order)
        api.upgrade_subscription.assert_not_awaited()
        api.renew_subscription_custom.assert_not_awaited()
        assert admin_notify_mock.await_count == 1


@pytest.mark.asyncio
async def test_same_plan_renew_without_expiry_extension_is_not_delivered(session_factory, monkeypatch):
    adapt_uuid = "e51c42a3-745d-4316-8894-aedeb4926965"
    expiry = datetime(2026, 11, 11, 6, 6, 22)
    plan_uuid = "1fa99c54-43e2-490b-9d18-d66f7b1017c4"
    order = _adapt_renew_order("renew-unchanged-expiry", adapt_uuid, plan_uuid, expiry)

    async with session_factory() as session:
        session.add(order)
        await session.commit()
        api = AsyncMock()
        api.get_status.return_value = {
            "plan_uuid": plan_uuid,
            "end_date": expiry.isoformat(),
            "devices": 10,
        }
        api.renew_subscription.return_value = {
            "plan_uuid": plan_uuid,
            "end_date": expiry.isoformat(),
            "devices": 10,
        }
        monkeypatch.setattr(routes, "AdaptAPI", lambda: api)

        await routes._fulfill_adapt_order(session, order, plan_uuid)

        assert order.status == "paid"
        assert order.failure_code == "adapt_renew_same_expiry"
        api.renew_subscription.assert_awaited_once_with(adapt_uuid)


@pytest.mark.asyncio
async def test_stale_local_upgrade_snapshot_cannot_false_deliver_first_attempt(
    session_factory, monkeypatch
):
    adapt_uuid = "770fa622-a4bd-63f6-c938-668877664447"
    stale_plan = "old-plan-uuid"
    authoritative_plan = "1fa99c54-43e2-490b-9d18-d66f7b1017c4"
    expiry = datetime(2027, 1, 11, 6, 6, 22)
    api = AsyncMock()
    api.get_status.return_value = {
        "plan_uuid": authoritative_plan,
        "end_date": expiry.isoformat(),
        "devices": 5,
    }
    monkeypatch.setattr(routes, "AdaptAPI", lambda: api)

    async with session_factory() as session:
        order = _adapt_upgrade_order(
            "upgrade-stale-local-snapshot",
            adapt_uuid,
            stale_plan,
            expiry,
        )
        session.add(order)
        await session.commit()

        await routes._fulfill_adapt_order(session, order, authoritative_plan)

        assert order.target_snapshot_plan_uuid == authoritative_plan
        assert order.adapt_mutation_state is None
        assert order.status == "failed"
        assert order.failure_code == "adapt_upgrade_same_plan_unextended"
        api.upgrade_subscription.assert_not_awaited()


@pytest.mark.asyncio
async def test_plan_changing_upgrade_target_without_expiry_extension_delivers_after_restart(
    session_factory, monkeypatch
):
    adapt_uuid = "770fa622-a4bd-63f6-c938-668877664444"
    baseline_plan = "old-plan-uuid"
    target_plan = "1fa99c54-43e2-490b-9d18-d66f7b1017c4"
    expiry = datetime(2027, 1, 11, 6, 6, 22)
    admin_notify_mock = AsyncMock()
    monkeypatch.setattr(routes, "_notify_admins_webstore", admin_notify_mock)

    api = AsyncMock()
    api.get_status.side_effect = [
        {"plan_uuid": baseline_plan, "end_date": expiry.isoformat(), "devices": 3},
        {"plan_uuid": target_plan, "end_date": expiry.isoformat(), "devices": 5},
    ]
    api.upgrade_subscription.side_effect = AdaptAPIError("response lost", status=503)
    monkeypatch.setattr(routes, "AdaptAPI", lambda: api)

    async with session_factory() as session:
        session.add(_adapt_upgrade_order("upgrade-plan-change-target", adapt_uuid, baseline_plan, expiry))
        await session.commit()
        order = await session.scalar(
            select(WebOrder).where(WebOrder.order_id == "upgrade-plan-change-target")
        )
        await routes.attempt_fulfill_order(session, order)
        assert order.status == "paid"
        assert order.adapt_mutation_state == routes._ADAPT_MUTATION_INITIAL_INFLIGHT
        assert order.target_snapshot_plan_uuid == baseline_plan
        assert order.target_snapshot_expires_at == expiry
        assert api.upgrade_subscription.await_count == 1

    async with session_factory() as session:
        order = await session.scalar(
            select(WebOrder).where(WebOrder.order_id == "upgrade-plan-change-target")
        )
        order.next_fulfillment_retry_at = None
        await routes.attempt_fulfill_order(session, order)
        assert order.status == "delivered"
        assert order.adapt_mutation_state == routes._ADAPT_MUTATION_CONFIRMED
        assert order.access_expires_at == expiry
        assert api.upgrade_subscription.await_count == 1
        admin_notify_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_plan_changing_upgrade_baseline_allows_one_safe_retry(session_factory, monkeypatch):
    adapt_uuid = "770fa622-a4bd-63f6-c938-668877664445"
    baseline_plan = "old-plan-uuid"
    target_plan = "1fa99c54-43e2-490b-9d18-d66f7b1017c4"
    expiry = datetime(2027, 1, 11, 6, 6, 22)
    api = AsyncMock()
    api.get_status.side_effect = [
        {"plan_uuid": baseline_plan, "end_date": expiry.isoformat(), "devices": 3},
        {"plan_uuid": baseline_plan, "end_date": expiry.isoformat(), "devices": 3},
        {"plan_uuid": target_plan, "end_date": expiry.isoformat(), "devices": 5},
    ]
    api.upgrade_subscription.side_effect = [
        AdaptAPIError("response lost", status=503),
        {"plan_uuid": target_plan, "end_date": expiry.isoformat(), "devices": 5},
    ]
    monkeypatch.setattr(routes, "AdaptAPI", lambda: api)

    async with session_factory() as session:
        order = _adapt_upgrade_order("upgrade-plan-change-retry", adapt_uuid, baseline_plan, expiry)
        session.add(order)
        await session.commit()
        for _ in range(2):
            order.next_fulfillment_retry_at = None
            await routes.attempt_fulfill_order(session, order)
        assert order.status == "delivered"
        assert order.adapt_mutation_state == routes._ADAPT_MUTATION_CONFIRMED
        assert api.upgrade_subscription.await_count == 2


@pytest.mark.asyncio
async def test_plan_changing_upgrade_safe_retry_baseline_fails_closed_without_third_post(
    session_factory, monkeypatch
):
    adapt_uuid = "770fa622-a4bd-63f6-c938-668877664446"
    baseline_plan = "old-plan-uuid"
    target_plan = "1fa99c54-43e2-490b-9d18-d66f7b1017c4"
    expiry = datetime(2027, 1, 11, 6, 6, 22)
    admin_notify_mock = AsyncMock()
    monkeypatch.setattr(routes, "_notify_admins_webstore", admin_notify_mock)
    api = AsyncMock()
    api.get_status.return_value = {
        "plan_uuid": baseline_plan,
        "end_date": expiry.isoformat(),
        "devices": 3,
    }
    api.upgrade_subscription.side_effect = AdaptAPIError("response lost", status=503)
    monkeypatch.setattr(routes, "AdaptAPI", lambda: api)

    async with session_factory() as session:
        order = _adapt_upgrade_order("upgrade-plan-change-exhausted", adapt_uuid, baseline_plan, expiry)
        session.add(order)
        await session.commit()
        for _ in range(3):
            order.next_fulfillment_retry_at = None
            await routes.attempt_fulfill_order(session, order)
        assert order.status == "failed"
        assert order.adapt_mutation_state == routes._ADAPT_MUTATION_FAILED_CLOSED
        assert api.upgrade_subscription.await_count == 2
        admin_notify_mock.assert_awaited_once()

        await routes.attempt_fulfill_order(session, order)
        assert api.upgrade_subscription.await_count == 2
        assert admin_notify_mock.await_count == 1


@pytest.mark.asyncio
async def test_d_ambiguous_renew_response_recovers_without_second_post(session_factory, monkeypatch):
    """Test D: Ambiguous renew response (POST applied upstream, response timeout/503) -> next retry GET shows expiry > baseline -> 0 second POST, delivered."""
    adapt_uuid = "e51c42a3-745d-4316-8894-aedeb4926965"
    old_expiry = datetime(2026, 11, 11, 6, 6, 22)
    new_expiry = datetime(2026, 12, 12, 6, 6, 22)
    plan_uuid = "1fa99c54-43e2-490b-9d18-d66f7b1017c4"

    async with session_factory() as session:
        order = WebOrder(
            order_id="ambiguous-renew",
            profile_token="profile-d",
            tariff_key="basic-30",
            tariff_label="Базовый",
            days=30,
            amount_rub=260,
            status="paid",
            purchase_action="renew",
            target_order_id=f"uuid:{adapt_uuid}",
            target_snapshot_plan_uuid=plan_uuid,
            target_snapshot_expires_at=old_expiry,
            fulfillment_attempts=1,
        )
        session.add(order)
        await session.commit()

        api = AsyncMock()
        # Attempt 1: get_status returns old_expiry, renew_subscription raises 503
        api.get_status.return_value = {
            "plan_uuid": plan_uuid,
            "end_date": old_expiry.isoformat(),
            "devices": 10,
        }
        api.renew_subscription.side_effect = AdaptAPIError("503 Service Unavailable", status=503)
        monkeypatch.setattr(routes, "AdaptAPI", lambda: api)

        await routes._fulfill_adapt_order(session, order, plan_uuid)

        # Attempt 1 outcome: order marked retryable ('paid'), durable snapshot intact
        assert order.status == "paid"
        assert order.failure_code == "adapt_renew_503"
        assert order.target_snapshot_expires_at == old_expiry
        assert api.renew_subscription.await_count == 1

        # Attempt 2 (Next retry pass):
        # Upstream actually applied the renewal! GET returns extended expiry
        api.get_status.return_value = {
            "plan_uuid": plan_uuid,
            "end_date": new_expiry.isoformat(),
            "devices": 10,
        }
        api.renew_subscription.reset_mock()
        order.fulfillment_attempts = 2

        await routes._fulfill_adapt_order(session, order, plan_uuid)

        # Outcome: delivered with 0 additional POST calls!
        assert order.status == "delivered"
        assert order.access_expires_at == new_expiry
        api.renew_subscription.assert_not_awaited()


@pytest.mark.asyncio
async def test_e_unapplied_renew_safe_single_retry_post(session_factory, monkeypatch):
    """Test E: Renew POST did not apply (GET proves baseline) -> 1 safe retry POST -> GET proves expiry > A -> delivered."""
    adapt_uuid = "e51c42a3-745d-4316-8894-aedeb4926965"
    old_expiry = datetime(2026, 11, 11, 6, 6, 22)
    new_expiry = datetime(2026, 12, 12, 6, 6, 22)
    plan_uuid = "1fa99c54-43e2-490b-9d18-d66f7b1017c4"

    async with session_factory() as session:
        order = WebOrder(
            order_id="unapplied-renew",
            profile_token="profile-e",
            tariff_key="basic-30",
            tariff_label="Базовый",
            days=30,
            amount_rub=260,
            status="paid",
            purchase_action="renew",
            target_order_id=f"uuid:{adapt_uuid}",
            target_snapshot_plan_uuid=plan_uuid,
            target_snapshot_expires_at=old_expiry,
            fulfillment_attempts=1,
        )
        session.add(order)
        await session.commit()

        api = AsyncMock()
        # Attempt 1: renew_subscription raises TimeoutError
        api.get_status.return_value = {
            "plan_uuid": plan_uuid,
            "end_date": old_expiry.isoformat(),
            "devices": 10,
        }
        api.renew_subscription.side_effect = TimeoutError("Timed out")
        monkeypatch.setattr(routes, "AdaptAPI", lambda: api)

        await routes._fulfill_adapt_order(session, order, plan_uuid)
        assert order.status == "paid"
        assert order.failure_code == "adapt_renew_timeout"
        assert api.renew_subscription.await_count == 1

        # Attempt 2 (Next retry pass):
        # GET proves baseline (not extended upstream)
        api.renew_subscription.reset_mock()
        api.get_status.side_effect = [
            # Check before mutation: baseline
            {"plan_uuid": plan_uuid, "end_date": old_expiry.isoformat(), "devices": 10},
            # Check after mutation: extended
            {"plan_uuid": plan_uuid, "end_date": new_expiry.isoformat(), "devices": 10},
        ]
        api.renew_subscription.side_effect = None
        api.renew_subscription.return_value = {
            "plan_uuid": plan_uuid,
            "end_date": new_expiry.isoformat(),
            "devices": 10,
        }
        order.fulfillment_attempts = 2

        await routes._fulfill_adapt_order(session, order, plan_uuid)

        # Outcome: exactly 1 safe retry POST, verified, delivered!
        assert order.status == "delivered"
        assert order.access_expires_at == new_expiry
        assert api.renew_subscription.await_count == 1


@pytest.mark.asyncio
async def test_f_get_outage_executes_zero_mutation_posts_and_recovers(session_factory, monkeypatch):
    """Test F: GET outage (503/timeout on GET) -> 0 renew POSTs, 0 upgrade POSTs, order remains recoverable, recovers once GET works."""
    adapt_uuid = "e51c42a3-745d-4316-8894-aedeb4926965"
    old_expiry = datetime(2026, 11, 11, 6, 6, 22)
    new_expiry = datetime(2026, 12, 12, 6, 6, 22)
    plan_uuid = "1fa99c54-43e2-490b-9d18-d66f7b1017c4"

    async with session_factory() as session:
        order = WebOrder(
            order_id="get-outage-order",
            profile_token="profile-f",
            tariff_key="basic-30",
            tariff_label="Базовый",
            days=30,
            amount_rub=260,
            status="paid",
            purchase_action="renew",
            target_order_id=f"uuid:{adapt_uuid}",
            target_snapshot_plan_uuid=plan_uuid,
            target_snapshot_expires_at=old_expiry,
            fulfillment_attempts=1,
        )
        session.add(order)
        await session.commit()

        api = AsyncMock()
        # Attempt 1: GET outage
        api.get_status.side_effect = AdaptAPIError("503 Outage", status=503)
        monkeypatch.setattr(routes, "AdaptAPI", lambda: api)

        await routes._fulfill_adapt_order(session, order, plan_uuid)

        # Assert zero mutations during GET outage
        assert order.status == "paid"  # retryable
        api.renew_subscription.assert_not_awaited()
        api.upgrade_subscription.assert_not_awaited()

        # Attempt 2: GET recovers
        api.get_status.side_effect = [
            {"plan_uuid": plan_uuid, "end_date": old_expiry.isoformat(), "devices": 10},
            {"plan_uuid": plan_uuid, "end_date": new_expiry.isoformat(), "devices": 10},
        ]
        api.renew_subscription.return_value = {
            "plan_uuid": plan_uuid,
            "end_date": new_expiry.isoformat(),
            "devices": 10,
        }
        order.fulfillment_attempts = 2

        await routes._fulfill_adapt_order(session, order, plan_uuid)

        # Outcome: delivered
        assert order.status == "delivered"
        api.renew_subscription.assert_awaited_once_with(adapt_uuid)


@pytest.mark.asyncio
async def test_ambiguous_post_get_outage_then_target_keeps_one_post_across_restart(session_factory, monkeypatch):
    adapt_uuid = "e51c42a3-745d-4316-8894-aedeb4926965"
    expiry_a = datetime(2026, 11, 11, 6, 6, 22)
    expiry_b = datetime(2026, 12, 12, 6, 6, 22)
    plan_uuid = "1fa99c54-43e2-490b-9d18-d66f7b1017c4"
    order_id = "ambiguous-outage-target"

    api = AsyncMock()
    api.get_status.side_effect = [
        {"plan_uuid": plan_uuid, "end_date": expiry_a.isoformat(), "devices": 10},
        AdaptAPIError("503 Outage", status=503),
        {"plan_uuid": plan_uuid, "end_date": expiry_b.isoformat(), "devices": 10},
    ]
    api.renew_subscription.side_effect = AdaptAPIError("response lost", status=503)
    monkeypatch.setattr(routes, "AdaptAPI", lambda: api)

    async with session_factory() as session:
        session.add(_adapt_renew_order(order_id, adapt_uuid, plan_uuid, expiry_a))
        await session.commit()
        order = await session.scalar(
            select(WebOrder).where(WebOrder.order_id == order_id)
        )
        await routes.attempt_fulfill_order(session, order)
        assert order.status == "paid"
        assert order.adapt_mutation_state == routes._ADAPT_MUTATION_INITIAL_INFLIGHT
        assert api.renew_subscription.await_count == 1

    async with session_factory() as session:
        order = await session.scalar(
            select(WebOrder).where(WebOrder.order_id == order_id)
        )
        order.next_fulfillment_retry_at = None
        await routes.attempt_fulfill_order(session, order)
        assert order.status == "paid"
        assert order.adapt_mutation_state == routes._ADAPT_MUTATION_INITIAL_INFLIGHT
        assert api.renew_subscription.await_count == 1

    async with session_factory() as session:
        order = await session.scalar(
            select(WebOrder).where(WebOrder.order_id == order_id)
        )
        order.next_fulfillment_retry_at = None
        await routes.attempt_fulfill_order(session, order)
        assert order.status == "delivered"
        assert order.target_snapshot_expires_at == expiry_a
        assert api.renew_subscription.await_count == 1


@pytest.mark.asyncio
async def test_ambiguous_post_get_outage_baseline_allows_one_safe_retry(session_factory, monkeypatch):
    adapt_uuid = "e51c42a3-745d-4316-8894-aedeb4926965"
    expiry_a = datetime(2026, 11, 11, 6, 6, 22)
    expiry_b = datetime(2026, 12, 12, 6, 6, 22)
    plan_uuid = "1fa99c54-43e2-490b-9d18-d66f7b1017c4"

    api = AsyncMock()
    api.get_status.side_effect = [
        {"plan_uuid": plan_uuid, "end_date": expiry_a.isoformat(), "devices": 10},
        AdaptAPIError("503 Outage", status=503),
        {"plan_uuid": plan_uuid, "end_date": expiry_a.isoformat(), "devices": 10},
        {"plan_uuid": plan_uuid, "end_date": expiry_b.isoformat(), "devices": 10},
    ]
    api.renew_subscription.side_effect = [
        AdaptAPIError("response lost", status=503),
        {"plan_uuid": plan_uuid, "end_date": expiry_b.isoformat(), "devices": 10},
    ]
    monkeypatch.setattr(routes, "AdaptAPI", lambda: api)

    async with session_factory() as session:
        order = _adapt_renew_order("ambiguous-outage-baseline", adapt_uuid, plan_uuid, expiry_a)
        session.add(order)
        await session.commit()
        for _ in range(3):
            order.next_fulfillment_retry_at = None
            await routes.attempt_fulfill_order(session, order)
        assert order.status == "delivered"
        assert order.adapt_mutation_state == routes._ADAPT_MUTATION_CONFIRMED
        assert api.renew_subscription.await_count == 2


@pytest.mark.asyncio
async def test_safe_recovery_ambiguous_target_allows_no_third_post(session_factory, monkeypatch):
    adapt_uuid = "e51c42a3-745d-4316-8894-aedeb4926965"
    expiry_a = datetime(2026, 11, 11, 6, 6, 22)
    expiry_b = datetime(2026, 12, 12, 6, 6, 22)
    plan_uuid = "1fa99c54-43e2-490b-9d18-d66f7b1017c4"

    api = AsyncMock()
    api.get_status.side_effect = [
        {"plan_uuid": plan_uuid, "end_date": expiry_a.isoformat(), "devices": 10},
        {"plan_uuid": plan_uuid, "end_date": expiry_a.isoformat(), "devices": 10},
        {"plan_uuid": plan_uuid, "end_date": expiry_b.isoformat(), "devices": 10},
    ]
    api.renew_subscription.side_effect = [
        AdaptAPIError("response lost", status=503),
        AdaptAPIError("response lost", status=503),
    ]
    monkeypatch.setattr(routes, "AdaptAPI", lambda: api)

    async with session_factory() as session:
        order = _adapt_renew_order("safe-retry-target", adapt_uuid, plan_uuid, expiry_a)
        session.add(order)
        await session.commit()
        for _ in range(3):
            order.next_fulfillment_retry_at = None
            await routes.attempt_fulfill_order(session, order)
        assert order.status == "delivered"
        assert api.renew_subscription.await_count == 2


@pytest.mark.asyncio
async def test_safe_recovery_ambiguous_baseline_fails_closed_without_third_post(session_factory, monkeypatch):
    adapt_uuid = "e51c42a3-745d-4316-8894-aedeb4926965"
    expiry_a = datetime(2026, 11, 11, 6, 6, 22)
    plan_uuid = "1fa99c54-43e2-490b-9d18-d66f7b1017c4"
    admin_notify_mock = AsyncMock()
    monkeypatch.setattr(routes, "_notify_admins_webstore", admin_notify_mock)

    api = AsyncMock()
    api.get_status.return_value = {
        "plan_uuid": plan_uuid,
        "end_date": expiry_a.isoformat(),
        "devices": 10,
    }
    api.renew_subscription.side_effect = [
        AdaptAPIError("response lost", status=503),
        AdaptAPIError("response lost", status=503),
    ]
    monkeypatch.setattr(routes, "AdaptAPI", lambda: api)

    async with session_factory() as session:
        order = _adapt_renew_order("safe-retry-baseline", adapt_uuid, plan_uuid, expiry_a)
        session.add(order)
        await session.commit()
        for _ in range(3):
            order.next_fulfillment_retry_at = None
            await routes.attempt_fulfill_order(session, order)
        assert order.status == "failed"
        assert order.adapt_mutation_state == routes._ADAPT_MUTATION_FAILED_CLOSED
        assert api.renew_subscription.await_count == 2
        assert admin_notify_mock.await_count == 1

        await routes.attempt_fulfill_order(session, order)
        assert api.renew_subscription.await_count == 2
        assert admin_notify_mock.await_count == 1


@pytest.mark.asyncio
async def test_safe_recovery_get_outage_keeps_post_count_bounded_then_targets(session_factory, monkeypatch):
    adapt_uuid = "e51c42a3-745d-4316-8894-aedeb4926965"
    expiry_a = datetime(2026, 11, 11, 6, 6, 22)
    expiry_b = datetime(2026, 12, 12, 6, 6, 22)
    plan_uuid = "1fa99c54-43e2-490b-9d18-d66f7b1017c4"
    admin_notify_mock = AsyncMock()
    monkeypatch.setattr(routes, "_notify_admins_webstore", admin_notify_mock)

    api = AsyncMock()
    api.get_status.side_effect = [
        {"plan_uuid": plan_uuid, "end_date": expiry_a.isoformat(), "devices": 10},
        {"plan_uuid": plan_uuid, "end_date": expiry_a.isoformat(), "devices": 10},
        AdaptAPIError("503 Outage", status=503),
        {"plan_uuid": plan_uuid, "end_date": expiry_b.isoformat(), "devices": 10},
    ]
    api.renew_subscription.side_effect = [
        AdaptAPIError("response lost", status=503),
        AdaptAPIError("response lost", status=503),
    ]
    monkeypatch.setattr(routes, "AdaptAPI", lambda: api)

    async with session_factory() as session:
        order = _adapt_renew_order("safe-retry-outage", adapt_uuid, plan_uuid, expiry_a)
        session.add(order)
        await session.commit()
        for _ in range(4):
            order.next_fulfillment_retry_at = None
            await routes.attempt_fulfill_order(session, order)
        assert order.status == "delivered"
        assert api.renew_subscription.await_count == 2
        admin_notify_mock.assert_not_awaited()


def test_admin_unified_search_deduplicates_identical_name_and_contact():
    """Test that when a web client has name == contact (e.g. phone number),

    the contact is not printed redundantly.
    """
    results = [
        {
            "kind": "web",
            "name": "+79090906556",
            "contact": "+79090906556",
            "profile_token": "75cbba5aa33f16e909075b10f9caeb25",
        }
    ]
    text, markup = _build_unified_search_page(results, query="+79090906556", page=1)

    # Title has "+79090906556"
    assert "1. +79090906556" in text
    # Redundant line "├ +79090906556" must NOT appear
    assert "├ +79090906556" not in text
    # Replaced by profile token preview
    assert "├ Профиль: <code>75cbba5aa33f…</code>" in text


@pytest.mark.asyncio
async def test_stale_local_baseline_authoritative_renew(session_factory, monkeypatch):
    """Test Blocker 1: Local expiry = A, Adapt authoritative pre-mutation expiry = B (B > A).

    First fulfillment must update baseline to B, call renew_subscription exactly once,
    receive C (C > B), and deliver C. Must NOT mistakenly treat B > A as already_applied with 0 renew POSTs.
    """
    adapt_uuid = "e51c42a3-745d-4316-8894-aedeb4926965"
    expiry_a = datetime(2026, 10, 10, 6, 6, 22)  # Stale local expiry A
    expiry_b = datetime(2026, 11, 11, 6, 6, 22)  # Adapt authoritative expiry B (B > A)
    expiry_c = datetime(2026, 12, 12, 6, 6, 22)  # Extended post-renew expiry C (C > B)
    plan_uuid = "1fa99c54-43e2-490b-9d18-d66f7b1017c4"

    async with session_factory() as session:
        primary = WebOrder(
            order_id="order-stale-local",
            profile_token="profile-stale",
            tariff_key="basic-30",
            tariff_label="Базовый",
            days=30,
            amount_rub=260,
            status="delivered",
            purchase_action="buy",
            marzban_username=f"adapt_{adapt_uuid}",
            subscription_url=f"https://darimiru.xyz/vpnbot/adapt-sub/{adapt_uuid}",
            access_expires_at=expiry_a,
        )
        new_order = WebOrder(
            order_id="renew-order-stale-baseline",
            profile_token="profile-stale",
            tariff_key="basic-30",
            tariff_label="Базовый",
            days=30,
            amount_rub=260,
            status="paid",
            purchase_action="renew",
            target_order_id="order-stale-local",
            target_snapshot_plan_uuid=plan_uuid,
            target_snapshot_expires_at=expiry_a,  # Local order initialized with stale A
            fulfillment_attempts=0,
        )
        session.add_all([primary, new_order])
        await session.commit()

        api = AsyncMock()
        api.get_status.side_effect = [
            # 1. Authoritative pre-mutation GET: returns B (B > A)
            {"plan_uuid": plan_uuid, "end_date": expiry_b.isoformat(), "devices": 10},
            # 2. Authoritative post-mutation GET: returns C (C > B)
            {"plan_uuid": plan_uuid, "end_date": expiry_c.isoformat(), "devices": 10},
        ]
        api.renew_subscription.return_value = {
            "plan_uuid": plan_uuid,
            "end_date": expiry_c.isoformat(),
            "devices": 10,
        }
        monkeypatch.setattr(routes, "AdaptAPI", lambda: api)

        # Run attempt_fulfill_order
        await routes.attempt_fulfill_order(session, new_order)

        # Verify outcome
        assert new_order.status == "delivered"
        # Baseline was updated to authoritative B before mutation
        assert new_order.target_snapshot_expires_at == expiry_b
        # Final access expiry delivered as C
        assert new_order.access_expires_at == expiry_c
        # renew_subscription called EXACTLY once! Not skipped (not 0 calls)!
        api.renew_subscription.assert_awaited_once_with(adapt_uuid)
        api.upgrade_subscription.assert_not_awaited()


@pytest.mark.asyncio
async def test_transient_503_does_not_alert_staff(session_factory, monkeypatch):
    """Test Blocker 2: Transient 503 from supplier within retry window must NOT alert staff.

    Client delayed message is allowed. When next attempt recovers, delivered, staff failure alerts == 0.
    """
    adapt_uuid = "e51c42a3-745d-4316-8894-aedeb4926965"
    old_expiry = datetime(2026, 11, 11, 6, 6, 22)
    new_expiry = datetime(2026, 12, 12, 6, 6, 22)
    plan_uuid = "1fa99c54-43e2-490b-9d18-d66f7b1017c4"

    admin_notify_mock = AsyncMock()
    customer_notify_mock = AsyncMock()
    monkeypatch.setattr(routes, "_notify_admins_webstore", admin_notify_mock)
    monkeypatch.setattr(routes, "_notify_linked_webstore_user", customer_notify_mock)

    async with session_factory() as session:
        order = WebOrder(
            order_id="order-transient-503",
            profile_token="profile-transient",
            tariff_key="basic-30",
            tariff_label="Базовый",
            days=30,
            amount_rub=260,
            status="paid",
            purchase_action="renew",
            target_order_id=f"uuid:{adapt_uuid}",
            target_snapshot_plan_uuid=plan_uuid,
            target_snapshot_expires_at=old_expiry,
            fulfillment_attempts=0,
        )
        session.add(order)
        await session.commit()

        api = AsyncMock()
        # Preflight GET returns old_expiry, renew POST fails with 503
        api.get_status.return_value = {
            "plan_uuid": plan_uuid,
            "end_date": old_expiry.isoformat(),
            "devices": 10,
        }
        api.renew_subscription.side_effect = AdaptAPIError("503 Service Unavailable", status=503)
        monkeypatch.setattr(routes, "AdaptAPI", lambda: api)

        # Attempt 1
        await routes.attempt_fulfill_order(session, order)

        # Order remains retryable ('paid'), next retry scheduled
        assert order.status == "paid"
        assert order.fulfillment_attempts == 1
        assert order.next_fulfillment_retry_at is not None
        # Staff failure alert MUST NOT be called!
        admin_notify_mock.assert_not_awaited()
        # Customer delayed notification is called
        customer_notify_mock.assert_awaited_with(order, "delayed")

        # Attempt 2: Recovered
        admin_notify_mock.reset_mock()
        customer_notify_mock.reset_mock()
        api.renew_subscription.side_effect = None
        api.renew_subscription.return_value = {
            "plan_uuid": plan_uuid,
            "end_date": new_expiry.isoformat(),
            "devices": 10,
        }
        api.get_status.side_effect = [
            {"plan_uuid": plan_uuid, "end_date": old_expiry.isoformat(), "devices": 10},
            {"plan_uuid": plan_uuid, "end_date": new_expiry.isoformat(), "devices": 10},
        ]
        order.next_fulfillment_retry_at = None

        await routes.attempt_fulfill_order(session, order)

        assert order.status == "delivered"
        assert order.access_expires_at == new_expiry
        # No staff failure alerts
        admin_notify_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_exhausted_recovery_alerts_once(session_factory, monkeypatch):
    """A failed safe recovery retry fails closed without a third supplier POST.

    Must alert staff EXACTLY once when exhausted, status='failed', next_fulfillment_retry_at=None.
    Replay must result in 0 duplicate staff alerts.
    """
    adapt_uuid = "e51c42a3-745d-4316-8894-aedeb4926965"
    old_expiry = datetime(2026, 11, 11, 6, 6, 22)
    plan_uuid = "1fa99c54-43e2-490b-9d18-d66f7b1017c4"

    admin_notify_mock = AsyncMock()
    monkeypatch.setattr(routes, "_notify_admins_webstore", admin_notify_mock)

    async with session_factory() as session:
        order = WebOrder(
            order_id="order-exhausted",
            profile_token="profile-exhausted",
            tariff_key="basic-30",
            tariff_label="Базовый",
            days=30,
            amount_rub=260,
            status="paid",
            purchase_action="renew",
            target_order_id=f"uuid:{adapt_uuid}",
            target_snapshot_plan_uuid=plan_uuid,
            target_snapshot_expires_at=old_expiry,
            fulfillment_attempts=0,
        )
        session.add(order)
        await session.commit()

        api = AsyncMock()
        api.get_status.return_value = {
            "plan_uuid": plan_uuid,
            "end_date": old_expiry.isoformat(),
            "devices": 10,
        }
        api.renew_subscription.side_effect = AdaptAPIError("503 Service Unavailable", status=503)
        monkeypatch.setattr(routes, "AdaptAPI", lambda: api)

        # Initial mutation and the one safe recovery retry are both ambiguous.
        for attempt in range(1, 3):
            order.next_fulfillment_retry_at = None
            await routes.attempt_fulfill_order(session, order)
            assert order.status == "paid"
            assert order.fulfillment_attempts == attempt
            assert order.next_fulfillment_retry_at is not None
            admin_notify_mock.assert_not_awaited()

        # The next authoritative baseline proves that the recovery retry did not apply.
        order.next_fulfillment_retry_at = None
        await routes.attempt_fulfill_order(session, order)
        assert order.status == "failed"
        assert order.fulfillment_attempts == 3
        assert order.next_fulfillment_retry_at is None
        assert api.renew_subscription.await_count == 2
        # Exactly 1 staff failure alert
        assert admin_notify_mock.await_count == 1

        # Replay: attempt_fulfill_order called again on already-failed order
        api.renew_subscription.reset_mock()
        await routes.attempt_fulfill_order(session, order)
        # 0 duplicate staff alerts, 0 extra renew calls
        assert admin_notify_mock.await_count == 1
        api.renew_subscription.assert_not_awaited()


@pytest.mark.asyncio
async def test_contradictory_state_fails_closed_and_alerts_once(session_factory, monkeypatch):
    """Test: Contradictory supplier state (baseline=A, target=B, supplier reports C)

    fails closed with 0 mutations, exactly 1 staff alert, and replay emits 0 duplicate alerts.
    """
    adapt_uuid = "e51c42a3-745d-4316-8894-aedeb4926965"
    old_expiry = datetime(2026, 11, 11, 6, 6, 22)
    plan_a = "demo-plan-uuid"
    plan_b = "1fa99c54-43e2-490b-9d18-d66f7b1017c4"
    plan_c = "unexpected-third-plan"

    admin_notify_mock = AsyncMock()
    monkeypatch.setattr(routes, "_notify_admins_webstore", admin_notify_mock)

    async with session_factory() as session:
        order = WebOrder(
            order_id="order-contradictory",
            profile_token="profile-contradictory",
            tariff_key="basic-30",
            tariff_label="Базовый",
            days=30,
            amount_rub=260,
            status="paid",
            purchase_action="upgrade",
            target_order_id=f"uuid:{adapt_uuid}",
            target_snapshot_plan_uuid=plan_a,
            target_snapshot_expires_at=old_expiry,
            fulfillment_attempts=0,
        )
        session.add(order)
        await session.commit()

        api = AsyncMock()
        # Upstream Adapt unexpectedly reports plan C (neither A nor B)
        api.get_status.return_value = {
            "plan_uuid": plan_c,
            "end_date": old_expiry.isoformat(),
            "devices": 10,
        }
        monkeypatch.setattr(routes, "AdaptAPI", lambda: api)

        # Run attempt_fulfill_order
        await routes.attempt_fulfill_order(session, order)

        # Fails closed immediately
        assert order.status == "failed"
        assert order.failure_code == "adapt_upgrade_plan_contradictory"
        assert order.next_fulfillment_retry_at is None
        # 0 mutations attempted
        api.renew_subscription.assert_not_awaited()
        api.upgrade_subscription.assert_not_awaited()
        # Exactly 1 staff alert
        assert admin_notify_mock.await_count == 1

        # Replay: attempt_fulfill_order called again
        await routes.attempt_fulfill_order(session, order)
        # 0 duplicate staff alerts, 0 extra mutation calls
        assert admin_notify_mock.await_count == 1
        api.renew_subscription.assert_not_awaited()
        api.upgrade_subscription.assert_not_awaited()

