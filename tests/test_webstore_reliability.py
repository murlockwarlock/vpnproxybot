from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.services.provisioning_issues import build_internal_access_error
from webstore import notification_outbox, routes
from webstore.models import Base, WebNotificationOutbox, WebOrder


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_retryable_failure_keeps_paid_order_visible_and_schedules_retry(monkeypatch):
    monkeypatch.setattr(routes, "_notify_admins_webstore", AsyncMock())
    monkeypatch.setattr(routes, "_notify_linked_webstore_user", AsyncMock())
    order = WebOrder(
        order_id="retry-1",
        tariff_key="vpn",
        tariff_label="VPN",
        days=30,
        amount_rub=100,
        status="paid",
        fulfillment_attempts=1,
    )
    issue = build_internal_access_error(
        provider="adapt",
        code="adapt_api_503",
        status=503,
        admin_message="provider unavailable",
    )

    await routes._mark_order_failed(order, issue)

    assert order.status == "paid"
    assert order.next_fulfillment_retry_at is not None
    assert "Оплата получена" in order.failure_message
    assert order.failure_code == "adapt_api_503"


@pytest.mark.asyncio
async def test_adapt_renew_retry_reconciles_without_second_mutation(monkeypatch, session_factory):
    adapt_uuid = "770fa622-a4bd-63f6-c938-668877662222"
    old_end = datetime.utcnow() + timedelta(days=5)
    new_end = old_end + timedelta(days=30)
    async with session_factory() as session:
        primary = WebOrder(
            order_id="primary",
            profile_token="profile",
            tariff_key="adapt-old",
            tariff_label="Adapt",
            days=30,
            amount_rub=100,
            status="delivered",
            marzban_username=f"adapt_{adapt_uuid}",
            subscription_url=f"https://darimiru.ru/vpnbot/adapt-sub/{adapt_uuid}",
            access_expires_at=old_end,
        )
        retry = WebOrder(
            order_id="renew-retry",
            profile_token="profile",
            tariff_key="adapt-old",
            tariff_label="Adapt",
            days=30,
            amount_rub=100,
            status="paid",
            purchase_action="renew",
            target_order_id="primary",
            fulfillment_attempts=2,
            target_snapshot_expires_at=old_end,
        )
        session.add_all([primary, retry])
        await session.commit()

        api = AsyncMock()
        api.get_status.return_value = {
            "plan_uuid": "plan-old",
            "end_date": new_end.isoformat(),
            "devices": 5,
        }
        api.list_plans.return_value = [{"uuid": "plan-old", "devices": 5}]
        monkeypatch.setattr(routes, "AdaptAPI", lambda: api)
        monkeypatch.setattr(
            routes,
            "get_store_tariffs_by_key",
            lambda: {"adapt-old": {"adapt_plan_uuid": "plan-old", "provider": "adapt"}},
        )

        await routes._fulfill_adapt_order(session, retry, "plan-old")

        assert retry.status == "delivered"
        assert retry.marzban_username == f"adapt_{adapt_uuid}"
        api.renew_subscription.assert_not_awaited()


@pytest.mark.asyncio
async def test_notification_outbox_retries_and_persists(monkeypatch, session_factory):
    monkeypatch.setattr(notification_outbox, "async_session", session_factory)
    monkeypatch.setattr(notification_outbox.settings, "admin_bot_token", "123:token")
    sender = AsyncMock(side_effect=[0, 1])
    monkeypatch.setattr(notification_outbox, "send_telegram_notifications", sender)

    created = await notification_outbox.enqueue_notifications(
        event="order_failed",
        dedupe_prefix="order:42:failed",
        recipient_ids=[1001],
        text="Оплата получена, выдача задержана",
    )
    assert created == 1
    assert await notification_outbox.process_notification_outbox() == 0

    async with session_factory() as session:
        row = await session.scalar(select(WebNotificationOutbox))
        assert row.status == "pending"
        assert row.attempts == 1
        row.next_attempt_at = datetime.utcnow() - timedelta(seconds=1)
        await session.commit()

    assert await notification_outbox.process_notification_outbox() == 1
    async with session_factory() as session:
        row = await session.scalar(select(WebNotificationOutbox))
        assert row.status == "sent"
        assert row.attempts == 2


def test_all_staff_recipients_are_deduplicated(monkeypatch):
    monkeypatch.setattr(notification_outbox.settings, "admin_ids", [1, 2])
    monkeypatch.setattr(notification_outbox.settings, "owner_ids", [2, 3])
    monkeypatch.setattr(notification_outbox.settings, "support_agent_ids", [3, 4])
    assert notification_outbox.all_staff_recipient_ids() == {1, 2, 3, 4}
