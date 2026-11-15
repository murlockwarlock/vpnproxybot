from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from webstore import routes, scheduler
from webstore.models import Base, WebBalanceAccount, WebBalanceTopUp, WebBalanceTransaction, WebOrder
from bot.services.vhq_partner_api import VHQPartnerAPI, VHQPartnerAPIError


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def web_db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    monkeypatch.setattr(routes, "async_session", factory)
    monkeypatch.setattr(scheduler, "async_session", factory)
    monkeypatch.setattr("webstore.notification_outbox.async_session", factory)

    yield factory

    await engine.dispose()


async def test_scheduler_recovers_succeeded_order_when_webhook_is_lost(
    monkeypatch, web_db
):
    async with web_db() as session:
        session.add(
            WebOrder(
                order_id="lost-webhook",
                contact="client@example.com",
                tariff_key="vpn_30",
                tariff_label="30 дней",
                days=30,
                amount_rub=155,
                original_amount_rub=155,
                status="pending",
                profile_token="profile-lost-webhook",
                yookassa_payment_id="yk_lost_1",
                created_at=datetime.utcnow() - timedelta(minutes=5),
            )
        )
        await session.commit()

    monkeypatch.setattr(
        routes,
        "fetch_yookassa_payment",
        AsyncMock(
            return_value=SimpleNamespace(
                payment_id="yk_lost_1",
                status="succeeded",
                paid=True,
                amount_rub=Decimal("155.00"),
                currency="RUB",
                metadata={"source": "webstore", "order_id": "lost-webhook"},
            )
        ),
    )

    async def deliver(_session, order):
        order.status = "delivered"
        order.subscription_url = "https://example.test/sub/recovered"
        order.delivered_at = datetime.utcnow()

    monkeypatch.setattr(routes, "attempt_fulfill_order", AsyncMock(side_effect=deliver))
    monkeypatch.setattr(routes, "_notify_admins_webstore", AsyncMock())
    monkeypatch.setattr(
        routes, "_notify_bot_order_delivered", AsyncMock(return_value={"delivered": True})
    )
    monkeypatch.setattr(routes, "_finalize_order_cashback", AsyncMock())
    monkeypatch.setattr(
        routes, "_disable_web_balance_autodebit_after_tariff_purchase", AsyncMock()
    )
    monkeypatch.setattr(routes, "_apply_order_bonus_spend", AsyncMock())
    monkeypatch.setattr(routes, "_sync_referral_credit_for_order", AsyncMock())

    await scheduler.reconcile_pending_yookassa_orders()

    async with web_db() as session:
        order = await session.scalar(
            select(WebOrder).where(WebOrder.order_id == "lost-webhook")
        )
        assert order.status == "delivered"
        assert order.paid_at is not None
        assert order.subscription_url == "https://example.test/sub/recovered"


async def test_scheduler_uses_provider_canceled_as_authoritative_state(
    monkeypatch, web_db
):
    async with web_db() as session:
        session.add(
            WebOrder(
                order_id="provider-canceled",
                contact="client@example.com",
                tariff_key="vpn_30",
                tariff_label="30 дней",
                days=30,
                amount_rub=155,
                original_amount_rub=155,
                status="pending",
                profile_token="profile-provider-canceled",
                yookassa_payment_id="yk_cancel_1",
                created_at=datetime.utcnow() - timedelta(hours=25),
            )
        )
        await session.commit()

    monkeypatch.setattr(
        routes,
        "fetch_yookassa_payment",
        AsyncMock(
            return_value=SimpleNamespace(
                payment_id="yk_cancel_1",
                status="canceled",
                paid=False,
                amount_rub=Decimal("155.00"),
                currency="RUB",
                metadata={"source": "webstore", "order_id": "provider-canceled"},
            )
        ),
    )

    await scheduler.reconcile_pending_yookassa_orders()

    async with web_db() as session:
        order = await session.scalar(
            select(WebOrder).where(WebOrder.order_id == "provider-canceled")
        )
        assert order.status == "canceled"


async def test_expiration_never_guesses_state_for_provider_backed_order(web_db):
    old = datetime.utcnow() - timedelta(hours=25)
    async with web_db() as session:
        session.add_all(
            [
                WebOrder(
                    order_id="provider-backed",
                    contact="client@example.com",
                    tariff_key="vpn_30",
                    tariff_label="30 дней",
                    days=30,
                    amount_rub=155,
                    original_amount_rub=155,
                    status="pending",
                    profile_token="profile-provider-backed",
                    yookassa_payment_id="yk_pending_1",
                    created_at=old,
                ),
                WebOrder(
                    order_id="no-provider-payment",
                    contact="client2@example.com",
                    tariff_key="vpn_30",
                    tariff_label="30 дней",
                    days=30,
                    amount_rub=155,
                    original_amount_rub=155,
                    status="pending",
                    profile_token="profile-no-payment",
                    created_at=old,
                ),
            ]
        )
        await session.commit()

    await scheduler.expire_pending_orders()

    async with web_db() as session:
        provider_backed = await session.scalar(
            select(WebOrder).where(WebOrder.order_id == "provider-backed")
        )
        no_provider = await session.scalar(
            select(WebOrder).where(WebOrder.order_id == "no-provider-payment")
        )
        assert provider_backed.status == "pending"
        assert no_provider.status == "canceled"


async def test_scheduler_recovers_succeeded_topup_when_webhook_is_lost(
    monkeypatch, web_db
):
    async with web_db() as session:
        session.add(
            WebBalanceAccount(
                profile_token="lost-topup-profile",
                contact="client@example.com",
                ref_code="lost-topup-ref",
                balance_rub=0,
            )
        )
        session.add(
            WebBalanceTopUp(
                topup_id="lost-topup",
                profile_token="lost-topup-profile",
                telegram_id=0,
                amount_rub=100,
                status="pending",
                yookassa_payment_id="yk_lost_topup_1",
                created_at=datetime.utcnow() - timedelta(minutes=5),
            )
        )
        await session.commit()

    monkeypatch.setattr(
        routes,
        "fetch_yookassa_payment",
        AsyncMock(
            return_value=SimpleNamespace(
                payment_id="yk_lost_topup_1",
                status="succeeded",
                paid=True,
                amount_rub=Decimal("100.00"),
                currency="RUB",
                metadata={
                    "source": "webstore_balance_topup",
                    "topup_id": "lost-topup",
                },
            )
        ),
    )

    await scheduler.reconcile_pending_yookassa_topups()

    async with web_db() as session:
        account = await session.get(WebBalanceAccount, "lost-topup-profile")
        topup = await session.scalar(
            select(WebBalanceTopUp).where(WebBalanceTopUp.topup_id == "lost-topup")
        )
        tx = await session.scalar(
            select(WebBalanceTransaction).where(
                WebBalanceTransaction.source_id == "lost-topup"
            )
        )
        assert account.balance_rub == 100
        assert topup.status == "completed"
        assert topup.completed_at is not None
        assert tx is not None
        assert tx.amount_rub == 100


async def test_local_cancellation_does_not_hide_paid_order_from_recovery(
    monkeypatch, web_db
):
    """BLOCKER regression test: A local cancellation on an order with yookassa_payment_id

    must NOT permanently exclude it from reconciliation if YooKassa confirmed payment.
    """
    now = datetime.utcnow()
    async with web_db() as session:
        session.add(
            WebOrder(
                order_id="cancelled-then-paid",
                contact="customer@example.com",
                tariff_key="vpn_30",
                tariff_label="30 дней",
                days=30,
                amount_rub=250,
                original_amount_rub=250,
                status="pending",
                profile_token="prof-cancel-recover",
                yookassa_payment_id="yk_cancelled_then_paid_1",
                created_at=now - timedelta(minutes=5),
            )
        )
        await session.commit()

    # 1. Simulate local cancellation by customer
    class _FakeRequest:
        async def json(self):
            return {"order_id": "cancelled-then-paid", "token": "prof-cancel-recover"}
        cookies = {}
        headers = {}

    resp = await routes.handle_cancel_order(_FakeRequest())
    assert resp.status == 200

    async with web_db() as session:
        order = await session.scalar(
            select(WebOrder).where(WebOrder.order_id == "cancelled-then-paid")
        )
        assert order.status == "canceled"
        assert order.cancellation_confirmed is False

    # 2. YooKassa succeeded and paid
    monkeypatch.setattr(
        routes,
        "fetch_yookassa_payment",
        AsyncMock(
            return_value=SimpleNamespace(
                payment_id="yk_cancelled_then_paid_1",
                status="succeeded",
                paid=True,
                amount_rub=Decimal("250.00"),
                currency="RUB",
                metadata={"source": "webstore", "order_id": "cancelled-then-paid"},
            )
        ),
    )

    fulfill_count = 0

    async def deliver(_session, order_obj):
        nonlocal fulfill_count
        fulfill_count += 1
        order_obj.status = "delivered"
        order_obj.subscription_url = "https://example.test/sub/recovered-from-cancel"
        order_obj.delivered_at = datetime.utcnow()

    monkeypatch.setattr(routes, "attempt_fulfill_order", AsyncMock(side_effect=deliver))
    monkeypatch.setattr(routes, "_notify_admins_webstore", AsyncMock())
    monkeypatch.setattr(routes, "_notify_bot_order_delivered", AsyncMock(return_value={"delivered": True}))
    monkeypatch.setattr(routes, "_finalize_order_cashback", AsyncMock())
    monkeypatch.setattr(routes, "_disable_web_balance_autodebit_after_tariff_purchase", AsyncMock())
    monkeypatch.setattr(routes, "_apply_order_bonus_spend", AsyncMock())
    monkeypatch.setattr(routes, "_sync_referral_credit_for_order", AsyncMock())

    # 3. Scheduled reconciliation recovers the order
    await scheduler.reconcile_pending_yookassa_orders()

    async with web_db() as session:
        order = await session.scalar(
            select(WebOrder).where(WebOrder.order_id == "cancelled-then-paid")
        )
        assert order.status == "delivered"
        assert order.paid_at is not None
        assert order.cancellation_confirmed is False
        assert order.subscription_url == "https://example.test/sub/recovered-from-cancel"

    assert fulfill_count == 1


async def test_provider_confirmed_cancellation_stops_reconciliation(
    monkeypatch, web_db
):
    """When YooKassa confirms the payment is canceled, the order status stays canceled,

    cancellation_confirmed becomes True, and the order is never checked again.
    """
    now = datetime.utcnow()
    async with web_db() as session:
        session.add(
            WebOrder(
                order_id="confirmed-cancel-order",
                contact="client@example.com",
                tariff_key="vpn_30",
                tariff_label="30 дней",
                days=30,
                amount_rub=250,
                original_amount_rub=250,
                status="pending",
                profile_token="prof-confirmed-cancel",
                yookassa_payment_id="yk_prov_cancel_1",
                created_at=now - timedelta(minutes=10),
            )
        )
        await session.commit()

    # 1. Customer cancels locally
    class _FakeRequest:
        async def json(self):
            return {"order_id": "confirmed-cancel-order", "token": "prof-confirmed-cancel"}
        cookies = {}
        headers = {}

    await routes.handle_cancel_order(_FakeRequest())

    # 2. YooKassa confirms canceled
    monkeypatch.setattr(
        routes,
        "fetch_yookassa_payment",
        AsyncMock(
            return_value=SimpleNamespace(
                payment_id="yk_prov_cancel_1",
                status="canceled",
                paid=False,
                amount_rub=Decimal("250.00"),
                currency="RUB",
                metadata={"source": "webstore", "order_id": "confirmed-cancel-order"},
            )
        ),
    )

    fulfill_mock = AsyncMock()
    monkeypatch.setattr(routes, "attempt_fulfill_order", fulfill_mock)

    await scheduler.reconcile_pending_yookassa_orders()

    async with web_db() as session:
        order = await session.scalar(
            select(WebOrder).where(WebOrder.order_id == "confirmed-cancel-order")
        )
        assert order.status == "canceled"
        assert order.cancellation_confirmed is True
        assert order.paid_at is None
        assert order.last_reconciled_at is not None

    assert fulfill_mock.call_count == 0

    # 3. Subsequent scheduler runs ignore this order completely
    fetch_mock = AsyncMock()
    monkeypatch.setattr(routes, "fetch_yookassa_payment", fetch_mock)
    await scheduler.reconcile_pending_yookassa_orders()
    assert fetch_mock.call_count == 0


async def test_historical_canceled_orders_are_never_reconciled(
    monkeypatch, web_db
):
    """Historical canceled orders with cancellation_confirmed=True (like historical order #60)

    must never be picked up or processed by reconciliation.
    """
    now = datetime.utcnow()
    async with web_db() as session:
        session.add(
            WebOrder(
                order_id="historical-order-60",
                contact="hist@example.com",
                tariff_key="vpn_30",
                tariff_label="30 дней",
                days=30,
                amount_rub=100,
                original_amount_rub=100,
                status="canceled",
                cancellation_confirmed=True,  # Set by migration
                profile_token="prof-historical",
                yookassa_payment_id="yk_hist_60",
                created_at=now - timedelta(days=180),
            )
        )
        await session.commit()

    fetch_mock = AsyncMock()
    monkeypatch.setattr(routes, "fetch_yookassa_payment", fetch_mock)

    await scheduler.reconcile_pending_yookassa_orders()

    # Historical order was never touched
    assert fetch_mock.call_count == 0

    async with web_db() as session:
        order = await session.scalar(
            select(WebOrder).where(WebOrder.order_id == "historical-order-60")
        )
        assert order.status == "canceled"
        assert order.cancellation_confirmed is True


async def test_attempt_fulfill_order_is_protected_by_profile_lock(web_db):
    """Under concurrent fulfillment attempts, attempt_fulfill_order executes exactly once."""
    import asyncio

    now = datetime.utcnow()
    async with web_db() as session:
        session.add(
            WebOrder(
                order_id="concurrent-order-1",
                contact="client@example.com",
                tariff_key="vpn_30",
                tariff_label="30 дней",
                days=30,
                amount_rub=250,
                status="paid",
                paid_at=now,
                profile_token="prof-concurrent-race",
                yookassa_payment_id="yk_conc_1",
                created_at=now,
            )
        )
        await session.commit()

    fulfill_call_count = 0

    async def mock_fulfill(session, order_obj):
        nonlocal fulfill_call_count
        fulfill_call_count += 1
        await asyncio.sleep(0.05)
        order_obj.status = "delivered"
        order_obj.subscription_url = "https://example.test/sub/race"
        order_obj.delivered_at = datetime.utcnow()
        await session.commit()

    with patch.object(routes, "_fulfill_order", side_effect=mock_fulfill):
        async with web_db() as session1, web_db() as session2:
            order1 = await session1.scalar(
                select(WebOrder).where(WebOrder.order_id == "concurrent-order-1")
            )
            order2 = await session2.scalar(
                select(WebOrder).where(WebOrder.order_id == "concurrent-order-1")
            )

            # Simultaneous fulfillment calls
            await asyncio.gather(
                routes.attempt_fulfill_order(session1, order1),
                routes.attempt_fulfill_order(session2, order2),
            )

    assert fulfill_call_count == 1

    async with web_db() as session:
        final_order = await session.scalar(
            select(WebOrder).where(WebOrder.order_id == "concurrent-order-1")
        )
        assert final_order.status == "delivered"


async def test_scheduler_starvation_prevention_prioritizes_fresh_orders(
    monkeypatch, web_db
):
    """Fresh orders without last_reconciled_at are prioritized over 60 older pending orders."""
    now = datetime.utcnow()
    async with web_db() as session:
        # 60 older orders that were already reconciled previously
        for i in range(60):
            session.add(
                WebOrder(
                    order_id=f"older-order-{i}",
                    tariff_key="vpn_30",
                    tariff_label="30 дней",
                    days=30,
                    amount_rub=100,
                    status="pending",
                    profile_token=f"prof-older-{i}",
                    yookassa_payment_id=f"yk_older_{i}",
                    created_at=now - timedelta(hours=2, minutes=i),
                    last_reconciled_at=now - timedelta(minutes=10),
                )
            )
        # 1 brand new order created 30 seconds ago (never reconciled)
        session.add(
            WebOrder(
                order_id="brand-new-order",
                tariff_key="vpn_30",
                tariff_label="30 дней",
                days=30,
                amount_rub=100,
                status="pending",
                profile_token="prof-brand-new",
                yookassa_payment_id="yk_brand_new_1",
                created_at=now - timedelta(seconds=30),
                last_reconciled_at=None,
            )
        )
        await session.commit()

    reconciled_order_ids = []

    async def fake_reconcile(order_id, payment_id, source="scheduler"):
        reconciled_order_ids.append(order_id)
        return "provider_pending"

    monkeypatch.setattr(scheduler, "reconcile_yookassa_web_order", fake_reconcile)

    await scheduler.reconcile_pending_yookassa_orders()

    # The batch limit is 50. Even though there were 60 older orders,
    # the brand-new order MUST be included and in fact processed first!
    assert "brand-new-order" in reconciled_order_ids
    assert reconciled_order_ids[0] == "brand-new-order"
    assert len(reconciled_order_ids) == 50


async def test_yookassa_verification_timeout_leaves_order_recoverable(
    monkeypatch, web_db
):
    """When YooKassa verification times out, order remains in its current state

    with updated last_reconciled_at, and is not terminally failed.
    """
    from webstore.yookassa_verify import YooKassaVerificationError

    now = datetime.utcnow()
    async with web_db() as session:
        session.add(
            WebOrder(
                order_id="timeout-order",
                contact="client@example.com",
                tariff_key="vpn_30",
                tariff_label="30 дней",
                days=30,
                amount_rub=155,
                status="pending",
                profile_token="prof-timeout",
                yookassa_payment_id="yk_timeout_1",
                created_at=now - timedelta(minutes=5),
                last_reconciled_at=None,
            )
        )
        await session.commit()

    monkeypatch.setattr(
        routes,
        "fetch_yookassa_payment",
        AsyncMock(side_effect=YooKassaVerificationError("YooKassa payment lookup timed out for yk_timeout_1")),
    )

    status = await routes.reconcile_yookassa_web_order("timeout-order", "yk_timeout_1")
    assert status == "provider_unavailable"

    async with web_db() as session:
        order = await session.scalar(
            select(WebOrder).where(WebOrder.order_id == "timeout-order")
        )
        # Order is NOT failed, still pending and recoverable
        assert order.status == "pending"
        assert order.last_reconciled_at is not None
        assert order.next_fulfillment_retry_at is None


async def test_fetch_yookassa_payment_timeout_and_error_mapping(monkeypatch):
    """fetch_yookassa_payment translates network timeouts and HTTP errors into YooKassaVerificationError."""
    import aiohttp
    from aiohttp import web
    from aiohttp.test_utils import RawTestServer
    from webstore.config import settings
    from webstore.yookassa_verify import fetch_yookassa_payment, YooKassaVerificationError

    monkeypatch.setattr(settings, "yookassa_shop_id", "test_shop")
    monkeypatch.setattr(settings, "yookassa_secret_key", "test_secret")

    # 1. Missing credentials
    monkeypatch.setattr(settings, "yookassa_shop_id", "")
    with pytest.raises(YooKassaVerificationError, match="not configured"):
        await fetch_yookassa_payment("yk_1")

    monkeypatch.setattr(settings, "yookassa_shop_id", "test_shop")

    # 2. Timeout handling
    class HangingServer:
        async def __call__(self, request):
            await asyncio.sleep(1.0)
            return web.Response(text="ok")

    async def mock_get(*args, **kwargs):
        raise asyncio.TimeoutError("Timeout connecting")

    class MockSession:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass
        def get(self, *args, **kwargs):
            raise asyncio.TimeoutError("lookup timed out")

    monkeypatch.setattr(aiohttp, "ClientSession", MockSession)
    with pytest.raises(YooKassaVerificationError, match="timed out"):
        await fetch_yookassa_payment("yk_timeout_test")

    # 3. Successful snapshot parsing
    class SuccessSession:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass
        def get(self, *args, **kwargs):
            class MockResponse:
                status = 200
                async def __aenter__(self):
                    return self
                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    pass
                async def json(self):
                    return {
                        "id": "yk_success_1",
                        "status": "succeeded",
                        "paid": True,
                        "amount": {"value": "299.00", "currency": "RUB"},
                        "metadata": {"source": "webstore", "order_id": "ord_success_1"},
                    }
            return MockResponse()

    monkeypatch.setattr(aiohttp, "ClientSession", SuccessSession)
    snap = await fetch_yookassa_payment("yk_success_1")
    assert snap.payment_id == "yk_success_1"
    assert snap.status == "succeeded"
    assert snap.paid is True
    assert snap.amount_rub == Decimal("299.00")
    assert snap.currency == "RUB"
    assert snap.metadata == {"source": "webstore", "order_id": "ord_success_1"}

    # 4. HTTP 404 handling
    class NotFoundSession:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass
        def get(self, *args, **kwargs):
            class MockResponse:
                status = 404
                async def __aenter__(self):
                    return self
                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    pass
            return MockResponse()

    monkeypatch.setattr(aiohttp, "ClientSession", NotFoundSession)
    with pytest.raises(YooKassaVerificationError, match="was not returned"):
        await fetch_yookassa_payment("yk_404")

    # 5. HTTP 500 / 502 handling
    class ServerErrorSession:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass
        def get(self, *args, **kwargs):
            class MockResponse:
                status = 502
                async def __aenter__(self):
                    return self
                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    pass
            return MockResponse()

    monkeypatch.setattr(aiohttp, "ClientSession", ServerErrorSession)
    with pytest.raises(YooKassaVerificationError, match="returned HTTP 502"):
        await fetch_yookassa_payment("yk_502")

    # 6. Malformed JSON handling
    class BadJsonSession:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass
        def get(self, *args, **kwargs):
            class MockResponse:
                status = 200
                async def __aenter__(self):
                    return self
                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    pass
                async def json(self):
                    raise ValueError("Expecting value: line 1 column 1 (char 0)")
            return MockResponse()

    monkeypatch.setattr(aiohttp, "ClientSession", BadJsonSession)
    with pytest.raises(YooKassaVerificationError, match="failed to parse YooKassa response"):
        await fetch_yookassa_payment("yk_bad_json")


# ── VHQ NEW Buy Crash-Safety & Leases ──────────────────────────────────────────

async def test_webstore_vhq_new_buy_durable_marker_and_crash_recovery_prevents_duplicate_buy(
    monkeypatch, web_db
):
    """VHQ NEW buy writes durable buy_inflight before calling api.buy().

    If the process crashes after buy, the next fulfillment/retry attempt
    detects stale_buy_inflight and refuses to repeat api.buy().
    """
    from bot.services.vhq_partner_api import VHQPartnerAPI
    now = datetime.utcnow()
    async with web_db() as session:
        session.add(
            WebOrder(
                order_id="vhq-crash-new-buy",
                contact="vhq_buyer@example.com",
                tariff_key="baza_pro_30",
                tariff_label="Baza Pro 30",
                days=30,
                amount_rub=200,
                original_amount_rub=200,
                status="paid",
                purchase_action="new",
                profile_token="prof-vhq-crash-test",
                yookassa_payment_id="yk_vhq_crash_1",
            )
        )
        await session.commit()

    marker_observed_in_db = False

    async def mock_buy_inspecting_db(*args, **kwargs):
        nonlocal marker_observed_in_db
        # Query DB via independent session to verify buy_inflight was committed BEFORE buy
        async with web_db() as check_session:
            order_in_db = await check_session.scalar(
                select(WebOrder).where(WebOrder.order_id == "vhq-crash-new-buy")
            )
            if (
                order_in_db.failure_code == "buy_inflight"
                and order_in_db.next_fulfillment_retry_at is not None
            ):
                marker_observed_in_db = True
        return {
            "order_id": "vhq_supp_order_999",
            "config_url": "https://sub.vhq-connect.xyz/real_key_999",
            "active_until": (now + timedelta(days=30)).isoformat(),
        }

    mock_api = AsyncMock()
    mock_api.buy = AsyncMock(side_effect=mock_buy_inspecting_db)
    mock_api_cls = MagicMock(return_value=mock_api)
    mock_api_cls.extract_active_until = VHQPartnerAPI.extract_active_until
    mock_api_cls.extract_subscription_url = VHQPartnerAPI.extract_subscription_url
    mock_api_cls.extract_order_id = VHQPartnerAPI.extract_order_id

    monkeypatch.setattr(
        routes,
        "get_store_tariffs_by_key",
        lambda: {"baza_pro_30": {"provider": "vhq", "vhq_tier": "basic", "days": 30}},
    )
    monkeypatch.setattr(routes, "VHQPartnerAPI", mock_api_cls)
    admin_notify_mock = AsyncMock()
    monkeypatch.setattr(routes, "_notify_admins_webstore", admin_notify_mock)

    # 1. Run first fulfillment attempt
    async with web_db() as session:
        order = await session.scalar(
            select(WebOrder).where(WebOrder.order_id == "vhq-crash-new-buy")
        )
        await routes.attempt_fulfill_order(session, order)
        await session.commit()

    # Durable marker was committed before api.buy() executed
    assert marker_observed_in_db is True
    assert mock_api.buy.call_count == 1

    # 2. Simulate process crash:
    # Server died after api.buy() succeeded, leaving order in status='paid' with buy_inflight
    # and expired lease.
    async with web_db() as session:
        order = await session.scalar(
            select(WebOrder).where(WebOrder.order_id == "vhq-crash-new-buy")
        )
        order.status = "paid"
        order.failure_code = "buy_inflight"
        order.failure_reason = "New VHQ buy initiated"
        order.next_fulfillment_retry_at = datetime.utcnow() - timedelta(minutes=1)
        await session.commit()

    # 3. Next fulfillment / retry runs after crash
    retry_api = AsyncMock()
    retry_api.buy = AsyncMock()
    retry_cls = MagicMock(return_value=retry_api)
    retry_cls.extract_active_until = VHQPartnerAPI.extract_active_until
    retry_cls.extract_subscription_url = VHQPartnerAPI.extract_subscription_url
    retry_cls.extract_order_id = VHQPartnerAPI.extract_order_id
    monkeypatch.setattr(routes, "VHQPartnerAPI", retry_cls)

    async with web_db() as session:
        order = await session.scalar(
            select(WebOrder).where(WebOrder.order_id == "vhq-crash-new-buy")
        )
        await routes.attempt_fulfill_order(session, order)
        await session.commit()

    # CRITICAL: api.buy MUST NOT be called again!
    assert retry_api.buy.call_count == 0

    # Order must transition to failed-safe / manual review state
    async with web_db() as session:
        order = await session.scalar(
            select(WebOrder).where(WebOrder.order_id == "vhq-crash-new-buy")
        )
        assert order.status == "failed"
        assert order.failure_code == "stale_buy_inflight"
        assert order.next_fulfillment_retry_at is None

    assert admin_notify_mock.call_count >= 1


async def test_webstore_vhq_new_buy_live_inflight_is_left_untouched(
    monkeypatch, web_db
):
    """When an order has an active buy_inflight lease, fulfillment skips it without calling buy."""
    now = datetime.utcnow()
    async with web_db() as session:
        session.add(
            WebOrder(
                order_id="vhq-live-lease",
                contact="vhq_live@example.com",
                tariff_key="baza_pro_30",
                tariff_label="Baza Pro 30",
                days=30,
                amount_rub=200,
                status="paid",
                purchase_action="new",
                profile_token="prof-vhq-live",
                failure_code="buy_inflight",
                failure_reason="New VHQ buy initiated",
                next_fulfillment_retry_at=now + timedelta(minutes=1),
            )
        )
        await session.commit()

    mock_api = AsyncMock()
    mock_api.buy = AsyncMock()
    monkeypatch.setattr(routes, "VHQPartnerAPI", MagicMock(return_value=mock_api))

    async with web_db() as session:
        order = await session.scalar(
            select(WebOrder).where(WebOrder.order_id == "vhq-live-lease")
        )
        await routes.attempt_fulfill_order(session, order)
        await session.commit()

    assert mock_api.buy.call_count == 0
    async with web_db() as session:
        order = await session.scalar(
            select(WebOrder).where(WebOrder.order_id == "vhq-live-lease")
        )
        assert order.status == "paid"
        assert order.failure_code == "buy_inflight"


async def test_webstore_vhq_new_buy_network_timeout_fails_closed(
    monkeypatch, web_db
):
    """Network timeout during VHQ new buy fails closed with no automated retry."""
    from bot.services.vhq_partner_api import VHQPartnerAPI
    async with web_db() as session:
        session.add(
            WebOrder(
                order_id="vhq-net-timeout",
                contact="vhq_timeout@example.com",
                tariff_key="baza_pro_30",
                tariff_label="Baza Pro 30",
                days=30,
                amount_rub=200,
                status="paid",
                purchase_action="new",
                profile_token="prof-vhq-timeout",
            )
        )
        await session.commit()

    mock_api = AsyncMock()
    mock_api.buy = AsyncMock(side_effect=asyncio.TimeoutError("VHQ connection timed out"))
    mock_api_cls = MagicMock(return_value=mock_api)
    monkeypatch.setattr(routes, "VHQPartnerAPI", mock_api_cls)
    monkeypatch.setattr(
        routes,
        "get_store_tariffs_by_key",
        lambda: {"baza_pro_30": {"provider": "vhq", "vhq_tier": "basic", "days": 30}},
    )
    admin_notify_mock = AsyncMock()
    monkeypatch.setattr(routes, "_notify_admins_webstore", admin_notify_mock)

    async with web_db() as session:
        order = await session.scalar(
            select(WebOrder).where(WebOrder.order_id == "vhq-net-timeout")
        )
        await routes.attempt_fulfill_order(session, order)
        await session.commit()

    async with web_db() as session:
        order = await session.scalar(
            select(WebOrder).where(WebOrder.order_id == "vhq-net-timeout")
        )
        assert order.status == "failed"
        assert order.failure_code in ("vhq_buy_outcome_unknown", "ambiguous_buy_network_error")
        assert order.next_fulfillment_retry_at is None

    assert admin_notify_mock.call_count >= 1


# ── Cancel Concurrency Race Protection ────────────────────────────────────────

async def test_customer_cancel_cannot_overwrite_delivered_order_concurrent_race(
    monkeypatch, web_db
):
    """Customer cancel racing with payment reconciliation/fulfillment cannot overwrite delivered state."""
    async with web_db() as session:
        session.add(
            WebOrder(
                order_id="race-cancel-customer",
                contact="race_cust@example.com",
                tariff_key="vpn_30",
                tariff_label="30 дней",
                days=30,
                amount_rub=250,
                original_amount_rub=250,
                status="pending",
                profile_token="prof-race-cust",
                yookassa_payment_id="yk_race_cust_1",
            )
        )
        await session.commit()

    # Simulate race: Task B (reconciliation/webhook) obtains lock and delivers order
    async with routes._acquire_profile_lock("prof-race-cust"):
        async with web_db() as session:
            order = await session.scalar(
                select(WebOrder).where(WebOrder.order_id == "race-cancel-customer")
            )
            order.status = "delivered"
            order.delivered_at = datetime.utcnow()
            await session.commit()

    # Now Task A (customer cancel) runs after delivery
    class _FakeRequest:
        async def json(self):
            return {"order_id": "race-cancel-customer", "token": "prof-race-cust"}
        cookies = {}
        headers = {}

    resp = await routes.handle_cancel_order(_FakeRequest())
    # Cancel must be rejected because status is delivered, not pending
    assert resp.status == 400

    async with web_db() as session:
        order = await session.scalar(
            select(WebOrder).where(WebOrder.order_id == "race-cancel-customer")
        )
        assert order.status == "delivered"


async def test_admin_cancel_cannot_overwrite_delivered_order_concurrent_race(
    monkeypatch, web_db
):
    """Admin cancel racing with payment reconciliation/fulfillment cannot overwrite delivered state."""
    from webstore.config import settings
    monkeypatch.setattr(settings, "bridge_shared_secret", "test-secret")

    async with web_db() as session:
        session.add(
            WebOrder(
                order_id="race-cancel-admin",
                contact="race_admin@example.com",
                tariff_key="vpn_30",
                tariff_label="30 дней",
                days=30,
                amount_rub=250,
                original_amount_rub=250,
                status="pending",
                profile_token="prof-race-admin",
                yookassa_payment_id="yk_race_admin_1",
            )
        )
        await session.commit()

    # Deliver order first
    async with web_db() as session:
        order = await session.scalar(
            select(WebOrder).where(WebOrder.order_id == "race-cancel-admin")
        )
        order.status = "delivered"
        order.delivered_at = datetime.utcnow()
        await session.commit()

    class _AdminRequest:
        headers = {"X-Internal-Secret": "test-secret"}
        async def json(self):
            return {"order_id": "race-cancel-admin", "action": "cancel"}

    resp = await routes.handle_internal_admin_order_action(_AdminRequest())
    assert resp.status == 409

    async with web_db() as session:
        order = await session.scalar(
            select(WebOrder).where(WebOrder.order_id == "race-cancel-admin")
        )
        assert order.status == "delivered"


# ── Migration Historical Cutoff Test ──────────────────────────────────────────

async def test_migration_historical_cutoff_protects_legacy_and_allows_new_canceled(tmp_path):
    """Legacy migration:

    - Canceled orders on or before 2026-11-03 11:55:00 UTC are marked cancellation_confirmed = 1.
    - Canceled orders after the cutoff remain cancellation_confirmed = 0 (recoverable).
    - Repeated init_db() calls do not alter newly canceled orders.
    """
    from sqlalchemy.pool import NullPool
    from webstore.database import init_db
    import sqlite3

    db_file = tmp_path / "legacy_test.db"
    test_db_url = f"sqlite+aiosqlite:///{db_file}"
    temp_engine = create_async_engine(test_db_url, echo=False, poolclass=NullPool)

    # 1. Create full tables and insert test orders via ORM
    async with temp_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    temp_session_factory = async_sessionmaker(temp_engine, class_=AsyncSession, expire_on_commit=False)
    async with temp_session_factory() as session:
        session.add_all([
            WebOrder(
                order_id="hist-order-before",
                tariff_key="vpn_30",
                tariff_label="30 дней",
                days=30,
                amount_rub=100,
                status="canceled",
                yookassa_payment_id="yk_old",
                created_at=datetime(2026, 11, 3, 10, 0, 0),
            ),
            WebOrder(
                order_id="order-after-cutoff",
                tariff_key="vpn_30",
                tariff_label="30 дней",
                days=30,
                amount_rub=200,
                status="canceled",
                yookassa_payment_id="yk_new",
                created_at=datetime(2026, 11, 3, 12, 0, 0),
            ),
        ])
        await session.commit()

    # 2. Drop cancellation_confirmed to simulate legacy schema before PR #27 migration
    async with temp_engine.begin() as conn:
        await conn.execute(text("ALTER TABLE web_orders DROP COLUMN cancellation_confirmed"))

    # 3. Configure webstore.database to point to our test SQLite database
    import webstore.database as db_mod
    orig_engine = db_mod.engine
    orig_session = db_mod.async_session
    orig_db_url = db_mod._db_url

    test_engine = create_async_engine(test_db_url, echo=False, poolclass=NullPool)
    test_session = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    db_mod.engine = test_engine
    db_mod.async_session = test_session

    try:
        # First migration run
        await init_db()

        # Check DB states
        conn = sqlite3.connect(str(db_file))
        cursor = conn.cursor()
        cursor.execute("SELECT order_id, cancellation_confirmed FROM web_orders ORDER BY id")
        rows = dict(cursor.fetchall())
        conn.close()

        # Order before cutoff must be confirmed canceled (legacy freeze)
        assert rows["hist-order-before"] == 1
        # Order after cutoff must NOT be confirmed canceled (recoverable)
        assert rows["order-after-cutoff"] == 0

        # Second migration run (idempotency check)
        await init_db()

        conn = sqlite3.connect(str(db_file))
        cursor = conn.cursor()
        cursor.execute("SELECT order_id, cancellation_confirmed FROM web_orders ORDER BY id")
        rows_second = dict(cursor.fetchall())
        conn.close()

        assert rows_second["hist-order-before"] == 1
        assert rows_second["order-after-cutoff"] == 0
    finally:
        await temp_engine.dispose()
        await test_engine.dispose()
        db_mod.engine = orig_engine
        db_mod.async_session = orig_session
        db_mod._db_url = orig_db_url


# ── Blocker 1 & 2 Corrective Tests: Fail-closed Admin Retry & Fail-closed Migration ──

@pytest.mark.parametrize("failure_code", [
    "ambiguous_buy_network_error",
    "stale_buy_inflight",
    "buy_missing_data",
    "vhq_buy_outcome_unknown",
])
async def test_admin_retry_blocked_for_non_retryable_vhq_buy_codes(
    monkeypatch, web_db, failure_code
):
    """When a VHQ order has a non-retryable failure code (ambiguous/stale buy outcome),

    1. _serialize_admin_order marks can_retry = False.
    2. Admin retry action returns HTTP 409 and never calls api.buy.
    """
    from webstore.config import settings
    monkeypatch.setattr(settings, "bridge_shared_secret", "test-secret")

    now = datetime.utcnow()
    async with web_db() as session:
        order = WebOrder(
            id=9501,
            order_id=f"WEB_RETRY_BLOCKED_{failure_code}",
            tariff_key="baza_pro_30",
            tariff_label="Baza Pro 30",
            days=30,
            amount_rub=100,
            status="failed",
            purchase_action="new",
            paid_at=now - timedelta(minutes=10),
            failure_code=failure_code,
            failure_reason="Non-retryable buy failure",
            profile_token=f"prof-{failure_code}",
        )
        session.add(order)
        await session.commit()
        await session.refresh(order)

        # 1. Check UI serialization: can_retry MUST be False!
        serialized = routes._serialize_admin_order(order)
        assert serialized["can_retry"] is False

    # 2. Check admin action endpoint: returns 409 and never calls api.buy
    mock_api = AsyncMock()
    mock_api.buy = AsyncMock()
    mock_api_cls = MagicMock(return_value=mock_api)

    class _AdminRequest:
        headers = {"X-Internal-Secret": "test-secret"}
        async def json(self):
            return {"order_id": f"WEB_RETRY_BLOCKED_{failure_code}", "action": "retry"}

    with (
        patch("webstore.routes.get_store_tariffs_by_key", return_value={"baza_pro_30": {"provider": "vhq", "vhq_tier": "basic", "days": 30}}),
        patch("webstore.routes.VHQPartnerAPI", mock_api_cls),
    ):
        resp = await routes.handle_internal_admin_order_action(_AdminRequest())

    assert resp.status == 409
    data = json.loads(resp.body.decode("utf-8"))
    assert "Повтор закупки заблокирован" in data.get("error", "")

    # CRITICAL INVARIANT: api.buy() MUST NOT be called!
    assert mock_api.buy.call_count == 0

    # Ensure DB record state was not modified into live fulfillment
    async with web_db() as session:
        order_check = await session.get(WebOrder, 9501)
        assert order_check.status == "failed"
        assert order_check.failure_code == failure_code


async def test_attempt_fulfill_order_refuses_non_retryable_failure_codes(web_db):
    """attempt_fulfill_order refuses to invoke supplier buy if order has a non-retryable failure code."""
    async with web_db() as session:
        order = WebOrder(
            id=9502,
            order_id="WEB_DIRECT_NON_RETRYABLE",
            tariff_key="baza_pro_30",
            tariff_label="Baza Pro 30",
            days=30,
            amount_rub=100,
            status="failed",
            purchase_action="new",
            failure_code="ambiguous_buy_network_error",
            profile_token="prof-direct-refuse",
        )
        session.add(order)
        await session.commit()

    mock_api = AsyncMock()
    mock_api.buy = AsyncMock()
    mock_api_cls = MagicMock(return_value=mock_api)

    async with web_db() as session:
        order = await session.get(WebOrder, 9502)
        with (
            patch("webstore.routes.get_store_tariffs_by_key", return_value={"baza_pro_30": {"provider": "vhq", "vhq_tier": "basic", "days": 30}}),
            patch("webstore.routes.VHQPartnerAPI", mock_api_cls),
        ):
            await routes.attempt_fulfill_order(session, order)
            await session.commit()

    assert mock_api.buy.call_count == 0
    async with web_db() as session:
        order_check = await session.get(WebOrder, 9502)
        assert order_check.status == "failed"
        assert order_check.failure_code == "ambiguous_buy_network_error"


async def test_migration_fail_closed_if_backfill_fails(tmp_path):
    """If backfill UPDATE fails during init_db(), the exception is NOT swallowed and startup aborts."""
    import contextlib
    from sqlalchemy.pool import NullPool
    from webstore.database import init_db
    import webstore.database as db_mod

    db_file = tmp_path / "fail_backfill.db"
    test_db_url = f"sqlite+aiosqlite:///{db_file}"
    temp_engine = create_async_engine(test_db_url, echo=False, poolclass=NullPool)

    # 1. Create tables and drop cancellation_confirmed to simulate legacy schema
    async with temp_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("ALTER TABLE web_orders DROP COLUMN cancellation_confirmed"))

    orig_engine = db_mod.engine
    orig_session = db_mod.async_session
    orig_db_url = db_mod._db_url

    class FailingBackfillConn:
        def __init__(self, real_conn):
            self.real_conn = real_conn

        async def execute(self, statement, *args, **kwargs):
            stmt_str = str(statement)
            if "UPDATE web_orders" in stmt_str and "cancellation_confirmed = 1" in stmt_str:
                raise RuntimeError("Simulated backfill disk/IO failure")
            return await self.real_conn.execute(statement, *args, **kwargs)

        async def run_sync(self, fn, *args, **kwargs):
            return await self.real_conn.run_sync(fn, *args, **kwargs)

        async def scalar(self, statement, *args, **kwargs):
            return await self.real_conn.scalar(statement, *args, **kwargs)

    class MockEngine:
        def __init__(self, real_engine):
            self.real_engine = real_engine

        @contextlib.asynccontextmanager
        async def begin(self):
            async with self.real_engine.begin() as real_conn:
                yield FailingBackfillConn(real_conn)

    db_mod.engine = MockEngine(temp_engine)
    db_mod.async_session = async_sessionmaker(temp_engine, class_=AsyncSession, expire_on_commit=False)

    try:
        with pytest.raises(RuntimeError, match="Simulated backfill disk/IO failure"):
            await init_db()
    finally:
        await temp_engine.dispose()
        db_mod.engine = orig_engine
        db_mod.async_session = orig_session
        db_mod._db_url = orig_db_url


async def test_migration_fail_closed_postcondition_verification_detects_unconfirmed_legacy(tmp_path):
    """If an unconfirmed legacy canceled order remains after migration, init_db() raises RuntimeError."""
    import contextlib
    from sqlalchemy.pool import NullPool
    from webstore.database import init_db
    import webstore.database as db_mod

    db_file = tmp_path / "postcondition_fail.db"
    test_db_url = f"sqlite+aiosqlite:///{db_file}"
    temp_engine = create_async_engine(test_db_url, echo=False, poolclass=NullPool)

    async with temp_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Insert a legacy canceled order before cutoff but with cancellation_confirmed = 0
    temp_session_factory = async_sessionmaker(temp_engine, class_=AsyncSession, expire_on_commit=False)
    async with temp_session_factory() as session:
        session.add(
            WebOrder(
                order_id="legacy-unprotected",
                tariff_key="vpn_30",
                tariff_label="30 дней",
                days=30,
                amount_rub=100,
                status="canceled",
                cancellation_confirmed=False,
                created_at=datetime(2026, 11, 2, 10, 0, 0),
            )
        )
        await session.commit()

    orig_engine = db_mod.engine
    orig_session = db_mod.async_session
    orig_db_url = db_mod._db_url

    class NoopUpdateConn:
        def __init__(self, real_conn):
            self.real_conn = real_conn

        async def execute(self, statement, *args, **kwargs):
            stmt_str = str(statement)
            if "UPDATE web_orders" in stmt_str and "cancellation_confirmed = 1" in stmt_str:
                return None
            return await self.real_conn.execute(statement, *args, **kwargs)

        async def run_sync(self, fn, *args, **kwargs):
            return await self.real_conn.run_sync(fn, *args, **kwargs)

        async def scalar(self, statement, *args, **kwargs):
            return await self.real_conn.scalar(statement, *args, **kwargs)

    class MockEngine:
        def __init__(self, real_engine):
            self.real_engine = real_engine

        @contextlib.asynccontextmanager
        async def begin(self):
            async with self.real_engine.begin() as real_conn:
                yield NoopUpdateConn(real_conn)

    db_mod.engine = MockEngine(temp_engine)
    db_mod.async_session = async_sessionmaker(temp_engine, class_=AsyncSession, expire_on_commit=False)

    try:
        with pytest.raises(RuntimeError, match="Historical cancellation safety verification failed"):
            await init_db()
    finally:
        await temp_engine.dispose()
        db_mod.engine = orig_engine
        db_mod.async_session = orig_session
        db_mod._db_url = orig_db_url


async def test_concurrent_gather_race_cancel_vs_payment_reconciliation(monkeypatch, web_db):
    """True concurrency test using asyncio.Event and asyncio.gather:

    Reconcile task (payment succeeded in YooKassa) races with Customer Cancel task.
    Proves:
    1. Both tasks execute concurrently under asyncio.gather.
    2. Exactly one authoritative outcome is reached.
    3. Even if cancel acquires lock first, the subsequent reconciliation detects payment.succeeded
       and safely recovers/delivers access.
    4. If reconciliation delivers first, cancel is safely rejected with HTTP 400.
    5. In no situation is a paid order wiped out or unfulfilled.
    """
    now = datetime.utcnow()
    async with web_db() as session:
        order = WebOrder(
            order_id="gather-race-order-1",
            contact="race_client@example.com",
            tariff_key="vpn_30",
            tariff_label="30 дней",
            days=30,
            amount_rub=150,
            status="pending",
            profile_token="prof-gather-race-1",
            yookassa_payment_id="yk_gather_1",
            created_at=now - timedelta(minutes=2),
            cancellation_confirmed=False,
        )
        session.add(order)
        await session.commit()

    reconcile_started = asyncio.Event()
    cancel_attempted = asyncio.Event()

    async def fake_verify(pid):
        reconcile_started.set()
        await cancel_attempted.wait()
        return SimpleNamespace(
            payment_id="yk_gather_1",
            status="succeeded",
            paid=True,
            amount_rub=Decimal("150.00"),
            currency="RUB",
            metadata={"source": "webstore", "order_id": "gather-race-order-1"},
        )

    monkeypatch.setattr(routes, "fetch_yookassa_payment", fake_verify)

    async def fake_fulfill(session, ord_obj):
        ord_obj.status = "delivered"
        ord_obj.delivered_at = datetime.utcnow()
        ord_obj.subscription_url = "https://sub.vhq-connect.xyz/gather-key"

    monkeypatch.setattr(routes, "attempt_fulfill_order", fake_fulfill)
    monkeypatch.setattr(routes, "_notify_bot_order_delivered", AsyncMock(return_value={"delivered": True}))
    monkeypatch.setattr(routes, "_notify_admins_webstore", AsyncMock())

    class _CancelRequest:
        async def json(self):
            return {"order_id": "gather-race-order-1", "token": "prof-gather-race-1"}
        cookies = {}
        headers = {}

    async def run_cancel_after_reconcile_start():
        await reconcile_started.wait()
        cancel_attempted.set()
        return await routes.handle_cancel_order(_CancelRequest())

    cancel_resp, reconcile_status = await asyncio.gather(
        run_cancel_after_reconcile_start(),
        routes.reconcile_yookassa_web_order("gather-race-order-1", "yk_gather_1", source="webhook"),
    )

    # In either scheduling order:
    # Final order MUST be delivered because YooKassa actually succeeded!
    async with web_db() as session:
        final_order = await session.scalar(
            select(WebOrder).where(WebOrder.order_id == "gather-race-order-1")
        )
        assert final_order.status == "delivered"
        assert final_order.subscription_url is not None
        assert final_order.cancellation_confirmed is False

    assert cancel_resp.status in (200, 400)
    assert reconcile_status in ("delivered", "already_delivered")


# ── VHQ Buy Protection Regression Tests (Tests A, B, C, D) ───────────────────

async def test_vhq_buy_http200_invalid_json_fails_closed_to_unknown_and_blocks_retry(
    monkeypatch, web_db
):
    """Test A: HTTP 200 + invalid JSON body through VHQPartnerAPI.

    Verifies:
    1. Order becomes failed with failure_code="vhq_buy_outcome_unknown".
    2. Admin Retry returns HTTP 409.
    3. Second api.buy call count is 0.
    """
    from webstore.config import settings
    monkeypatch.setattr(settings, "bridge_shared_secret", "test-secret")
    monkeypatch.setattr(settings, "vhq_partner_api_key", "test-vhq-key")

    now = datetime.utcnow()
    async with web_db() as session:
        order = WebOrder(
            id=9601,
            order_id="WEB_TEST_A_INVALID_JSON",
            tariff_key="baza_pro_30",
            tariff_label="Baza Pro 30",
            days=30,
            amount_rub=100,
            status="paid",
            purchase_action="new",
            paid_at=now,
            profile_token="prof-test-a",
        )
        session.add(order)
        await session.commit()

    # VHQPartnerAPI throws VHQPartnerAPIError on HTTP 200 with invalid JSON body
    mock_api = AsyncMock()
    mock_api.enabled = True
    mock_api.buy = AsyncMock(side_effect=VHQPartnerAPIError("<html>Bad Gateway Proxy</html>", status=200))
    mock_api_cls = MagicMock(return_value=mock_api)

    with (
        patch("webstore.routes.get_store_tariffs_by_key", return_value={"baza_pro_30": {"provider": "vhq", "vhq_tier": "basic", "days": 30}}),
        patch("webstore.routes.VHQPartnerAPI", mock_api_cls),
        patch("webstore.routes._notify_admins_webstore", new=AsyncMock()),
    ):
        async with web_db() as session:
            db_order = await session.get(WebOrder, 9601)
            await routes.attempt_fulfill_order(session, db_order)
            await session.commit()

    async with web_db() as session:
        order_check = await session.get(WebOrder, 9601)
        assert order_check.status == "failed"
        assert order_check.failure_code == "vhq_buy_outcome_unknown"
        assert order_check.next_fulfillment_retry_at is None
        assert routes._serialize_admin_order(order_check)["can_retry"] is False

    # Now attempt Admin Retry: must return 409 and not call buy() a second time!
    class _AdminRequest:
        headers = {"X-Internal-Secret": "test-secret"}
        async def json(self):
            return {"order_id": "WEB_TEST_A_INVALID_JSON", "action": "retry"}

    retry_api = AsyncMock()
    retry_api.enabled = True
    retry_api.buy = AsyncMock()
    retry_api_cls = MagicMock(return_value=retry_api)

    with (
        patch("webstore.routes.get_store_tariffs_by_key", return_value={"baza_pro_30": {"provider": "vhq", "vhq_tier": "basic", "days": 30}}),
        patch("webstore.routes.VHQPartnerAPI", retry_api_cls),
    ):
        resp = await routes.handle_internal_admin_order_action(_AdminRequest())

    assert resp.status == 409
    data = json.loads(resp.body.decode("utf-8"))
    assert "Повтор закупки заблокирован" in data.get("error", "")
    assert retry_api.buy.call_count == 0


async def test_vhq_buy_http502_bad_gateway_fails_closed_to_unknown_and_blocks_retry(
    monkeypatch, web_db
):
    """Test B: HTTP 502 Bad Gateway through VHQPartnerAPI.

    Verifies:
    1. Order becomes failed with failure_code="vhq_buy_outcome_unknown".
    2. Admin Retry returns HTTP 409.
    3. Second api.buy call count is 0.
    """
    from webstore.config import settings
    monkeypatch.setattr(settings, "bridge_shared_secret", "test-secret")
    monkeypatch.setattr(settings, "vhq_partner_api_key", "test-vhq-key")

    now = datetime.utcnow()
    async with web_db() as session:
        order = WebOrder(
            id=9602,
            order_id="WEB_TEST_B_HTTP_502",
            tariff_key="baza_pro_30",
            tariff_label="Baza Pro 30",
            days=30,
            amount_rub=100,
            status="paid",
            purchase_action="new",
            paid_at=now,
            profile_token="prof-test-b",
        )
        session.add(order)
        await session.commit()

    mock_api = AsyncMock()
    mock_api.enabled = True
    mock_api.buy = AsyncMock(side_effect=VHQPartnerAPIError("Bad Gateway", status=502))
    mock_api_cls = MagicMock(return_value=mock_api)

    with (
        patch("webstore.routes.get_store_tariffs_by_key", return_value={"baza_pro_30": {"provider": "vhq", "vhq_tier": "basic", "days": 30}}),
        patch("webstore.routes.VHQPartnerAPI", mock_api_cls),
        patch("webstore.routes._notify_admins_webstore", new=AsyncMock()),
    ):
        async with web_db() as session:
            db_order = await session.get(WebOrder, 9602)
            await routes.attempt_fulfill_order(session, db_order)
            await session.commit()

    async with web_db() as session:
        order_check = await session.get(WebOrder, 9602)
        assert order_check.status == "failed"
        assert order_check.failure_code == "vhq_buy_outcome_unknown"
        assert order_check.next_fulfillment_retry_at is None
        assert routes._serialize_admin_order(order_check)["can_retry"] is False

    class _AdminRequest:
        headers = {"X-Internal-Secret": "test-secret"}
        async def json(self):
            return {"order_id": "WEB_TEST_B_HTTP_502", "action": "retry"}

    retry_api = AsyncMock()
    retry_api.enabled = True
    retry_api.buy = AsyncMock()
    retry_api_cls = MagicMock(return_value=retry_api)

    with (
        patch("webstore.routes.get_store_tariffs_by_key", return_value={"baza_pro_30": {"provider": "vhq", "vhq_tier": "basic", "days": 30}}),
        patch("webstore.routes.VHQPartnerAPI", retry_api_cls),
    ):
        resp = await routes.handle_internal_admin_order_action(_AdminRequest())

    assert resp.status == 409
    data = json.loads(resp.body.decode("utf-8"))
    assert "Повтор закупки заблокирован" in data.get("error", "")
    assert retry_api.buy.call_count == 0


async def test_vhq_buy_missing_identifiers_fails_closed_to_unknown_and_blocks_retry(
    monkeypatch, web_db
):
    """Test C: HTTP 200 with missing identifiers {"status": "ok"}.

    Verifies:
    1. Order becomes failed with failure_code="vhq_buy_outcome_unknown".
    2. Admin Retry returns HTTP 409.
    3. Second api.buy call count is 0.
    """
    from webstore.config import settings
    monkeypatch.setattr(settings, "bridge_shared_secret", "test-secret")
    monkeypatch.setattr(settings, "vhq_partner_api_key", "test-vhq-key")

    now = datetime.utcnow()
    async with web_db() as session:
        order = WebOrder(
            id=9603,
            order_id="WEB_TEST_C_MISSING_DATA",
            tariff_key="baza_pro_30",
            tariff_label="Baza Pro 30",
            days=30,
            amount_rub=100,
            status="paid",
            purchase_action="new",
            paid_at=now,
            profile_token="prof-test-c",
        )
        session.add(order)
        await session.commit()

    mock_api = AsyncMock()
    mock_api.enabled = True
    mock_api.buy = AsyncMock(return_value={"status": "ok"})
    mock_api_cls = MagicMock(return_value=mock_api)
    mock_api_cls.extract_subscription_url = VHQPartnerAPI.extract_subscription_url
    mock_api_cls.extract_order_id = VHQPartnerAPI.extract_order_id
    mock_api_cls.extract_active_until = VHQPartnerAPI.extract_active_until

    with (
        patch("webstore.routes.get_store_tariffs_by_key", return_value={"baza_pro_30": {"provider": "vhq", "vhq_tier": "basic", "days": 30}}),
        patch("webstore.routes.VHQPartnerAPI", mock_api_cls),
        patch("webstore.routes._notify_admins_webstore", new=AsyncMock()),
    ):
        async with web_db() as session:
            db_order = await session.get(WebOrder, 9603)
            await routes.attempt_fulfill_order(session, db_order)
            await session.commit()

    async with web_db() as session:
        order_check = await session.get(WebOrder, 9603)
        assert order_check.status == "failed"
        assert order_check.failure_code == "vhq_buy_outcome_unknown"
        assert order_check.next_fulfillment_retry_at is None
        assert routes._serialize_admin_order(order_check)["can_retry"] is False

    class _AdminRequest:
        headers = {"X-Internal-Secret": "test-secret"}
        async def json(self):
            return {"order_id": "WEB_TEST_C_MISSING_DATA", "action": "retry"}

    retry_api = AsyncMock()
    retry_api.enabled = True
    retry_api.buy = AsyncMock()
    retry_api_cls = MagicMock(return_value=retry_api)

    with (
        patch("webstore.routes.get_store_tariffs_by_key", return_value={"baza_pro_30": {"provider": "vhq", "vhq_tier": "basic", "days": 30}}),
        patch("webstore.routes.VHQPartnerAPI", retry_api_cls),
    ):
        resp = await routes.handle_internal_admin_order_action(_AdminRequest())

    assert resp.status == 409
    data = json.loads(resp.body.decode("utf-8"))
    assert "Повтор закупки заблокирован" in data.get("error", "")
    assert retry_api.buy.call_count == 0


async def test_vhq_buy_preflight_validation_failures_remain_safely_retryable(
    monkeypatch, web_db
):
    """Test D: Local preflight failures (unconfigured API key or unsupported spec).

    Verifies:
    1. api.buy call count is 0.
    2. Failure codes are vhq_not_configured / vhq_unsupported_spec (not vhq_buy_outcome_unknown).
    3. _serialize_admin_order(order)["can_retry"] is True.
    """
    from webstore.config import settings
    monkeypatch.setattr(settings, "bridge_shared_secret", "test-secret")

    now = datetime.utcnow()
    # Case D1: Unconfigured API key
    monkeypatch.setattr(settings, "vhq_partner_api_key", "")

    async with web_db() as session:
        order1 = WebOrder(
            id=9604,
            order_id="WEB_TEST_D1_UNCONFIGURED",
            tariff_key="baza_pro_30",
            tariff_label="Baza Pro 30",
            days=30,
            amount_rub=100,
            status="paid",
            purchase_action="new",
            paid_at=now,
            profile_token="prof-test-d1",
        )
        session.add(order1)
        await session.commit()

    mock_api1 = AsyncMock()
    mock_api1.enabled = False
    mock_api1.buy = AsyncMock()
    mock_api1_cls = MagicMock(return_value=mock_api1)

    with (
        patch("webstore.routes.get_store_tariffs_by_key", return_value={"baza_pro_30": {"provider": "vhq", "vhq_tier": "basic", "days": 30}}),
        patch("webstore.routes.VHQPartnerAPI", mock_api1_cls),
        patch("webstore.routes._notify_admins_webstore", new=AsyncMock()),
    ):
        async with web_db() as session:
            db_order1 = await session.get(WebOrder, 9604)
            await routes.attempt_fulfill_order(session, db_order1)
            await session.commit()

    assert mock_api1.buy.call_count == 0
    async with web_db() as session:
        order1_check = await session.get(WebOrder, 9604)
        assert order1_check.status == "failed"
        assert order1_check.failure_code == "vhq_not_configured"
        assert order1_check.failure_code != "vhq_buy_outcome_unknown"
        assert routes._serialize_admin_order(order1_check)["can_retry"] is True

    # Case D2: Unsupported spec (tier="unsupported", days=999)
    monkeypatch.setattr(settings, "vhq_partner_api_key", "test-vhq-key")

    async with web_db() as session:
        order2 = WebOrder(
            id=9605,
            order_id="WEB_TEST_D2_UNSUPPORTED_SPEC",
            tariff_key="baza_custom",
            tariff_label="Baza Custom",
            days=999,
            amount_rub=500,
            status="paid",
            purchase_action="new",
            paid_at=now,
            profile_token="prof-test-d2",
        )
        session.add(order2)
        await session.commit()

    mock_api2 = AsyncMock()
    mock_api2.enabled = True
    mock_api2.buy = AsyncMock()
    mock_api2_cls = MagicMock(return_value=mock_api2)

    with (
        patch("webstore.routes.get_store_tariffs_by_key", return_value={"baza_custom": {"provider": "vhq", "vhq_tier": "unsupported", "days": 999}}),
        patch("webstore.routes.VHQPartnerAPI", mock_api2_cls),
        patch("webstore.routes._notify_admins_webstore", new=AsyncMock()),
    ):
        async with web_db() as session:
            db_order2 = await session.get(WebOrder, 9605)
            await routes.attempt_fulfill_order(session, db_order2)
            await session.commit()

    assert mock_api2.buy.call_count == 0
    async with web_db() as session:
        order2_check = await session.get(WebOrder, 9605)
        assert order2_check.status == "failed"
        assert order2_check.failure_code == "vhq_unsupported_spec"
        assert order2_check.failure_code != "vhq_buy_outcome_unknown"
        assert routes._serialize_admin_order(order2_check)["can_retry"] is True


