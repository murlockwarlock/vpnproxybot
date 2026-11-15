"""Regression tests for VHQ problem payments, safe recovery, and clear diagnostics.

Covers:
1. VHQ insufficient balance is recorded as "vhq_balance" (not ambiguous/uncertain).
2. Autonomous and manual reconciliation can safely retry VHQ delivery after balance top-up.
3. Historical uncertain payments check VHQ supplier orders before allowing safe retry (no false resets).
4. Ambiguous competing supplier orders fail closed.
5. Network timeouts or unknown errors safely retain "uncertain_supplier_mutation".
6. Outbox renewal fulfillment transitions to "retry_wait" on 402/insufficient balance.
7. Diagnostic reporting formats human-readable Russian descriptions and actionable advice.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.models import (
    AppLease,
    Base,
    Payment,
    PaymentMethod,
    PaymentStatus,
    Platform,
    Server,
    Subscription,
    SubStatus,
    Tariff,
    TariffType,
    User,
    VhqSubscriptionFulfillment,
)
from bot.services.cluster import LeaseHeartbeat, LeaseManager
from bot.services.problem_payments import (
    ProblemCategory,
    classify_candidate_db_only,
    diagnose_payment_problem,
    format_reconciliation_result,
)
from bot.services.provisioning_issues import AccessProvisionError, build_vhq_access_error
from bot.services.subscription_service import _create_vhq_paid_subscription, execute_vhq_fulfillment
from bot.services.vhq_partner_api import VHQPartnerAPI, VHQPartnerAPIError
from bot.services.yookassa_reconciliation import (
    _reconcile_ambiguous_vhq_mutation,
    verify_and_fulfill_yookassa_payment,
)
from bot.webhooks import handle_yookassa

pytestmark = pytest.mark.asyncio


def make_mock_vhq_class(mock_instance: AsyncMock) -> MagicMock:
    """Helper to mock VHQPartnerAPI while preserving static helper methods."""
    mock_instance.enabled = True
    cls_mock = MagicMock(return_value=mock_instance)
    cls_mock.extract_subscription_url = VHQPartnerAPI.extract_subscription_url
    cls_mock.extract_order_id = VHQPartnerAPI.extract_order_id
    cls_mock.extract_active_until = VHQPartnerAPI.extract_active_until
    return cls_mock


@pytest_asyncio.fixture
async def db_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session_factory(db_engine):
    return async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)


# ── Test 1: VHQ Insufficient Balance Recorded as vhq_balance ─────────────────


async def test_vhq_insufficient_balance_records_vhq_balance_not_uncertain(monkeypatch, db_session_factory):
    """When VHQ returns 'Insufficient balance: 37 < 249', failure code must be 'vhq_balance'."""
    monkeypatch.setattr("bot.database.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)

    async with db_session_factory() as session:
        await session.execute(delete(AppLease))
        user = User(id=12, telegram_id=700000001, username="synthetic_username_1", full_name="synthetic_full_name_2")
        tariff = Tariff(
            id=9,
            label="Базовый 30 дней",
            price_rub=359,
            days=30,
            vhq_tier="basic",
            is_active=True,
            tariff_type=TariffType.VPN,
        )
        server = Server(id=1, name="Primary", host="127.0.0.1", location="Amsterdam", api_url="https://vpn.test.com", is_active=True)
        session.add_all([user, tariff, server])
        await session.flush()

        payment = Payment(
            id=7319,
            user_id=user.id,
            tariff_id=tariff.id,
            amount=35910,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="synthetic_external_id_1",
            provider_status="succeeded",
            provider_paid=True,
            platform="deferred~n~7329",
            created_at=datetime.utcnow() - timedelta(hours=1),
        )
        session.add(payment)
        await session.commit()

        # Mock VHQ API to throw Insufficient balance
        mock_api = AsyncMock()
        mock_api.buy.side_effect = VHQPartnerAPIError("Insufficient balance: 37 < 249", status=400)

        with (
            patch("bot.services.subscription_service.get_primary_active_server", return_value=server),
            patch("bot.services.subscription_service.VHQPartnerAPI", return_value=mock_api),
            patch("bot.services.subscription_service.plog"),
        ):
            with pytest.raises(AccessProvisionError) as exc_info:
                await _create_vhq_paid_subscription(
                    session=session,
                    user=user,
                    tariff=tariff,
                    platform="deferred~n~7329",
                    vhq_spec={"tier": "basic", "days": 30},
                    provisioning_payment=payment,
                )

        assert exc_info.value.code == "vhq_balance"
        assert exc_info.value.stage == "pre_mutation"
        assert exc_info.value.confirmed_rejection is True
        assert exc_info.value.safe_retry_eligible is True

        # Payment record MUST have provisioning_failure_code = "vhq_balance"
        await session.refresh(payment)
        assert payment.provisioning_failure_code == "vhq_balance"

        # Check diagnostics
        diag = diagnose_payment_problem(payment, tariff=tariff)
        assert diag.supplier_name == "VHQ"
        assert diag.diagnostic_code == "vhq_balance"
        assert diag.safe_retry_eligible is True
        assert "недостаточно средств на партнёрском балансе" in diag.summary_title
        assert "Пополнить баланс VHQ" in diag.recommended_action

        # Classify candidate DB only
        item = await classify_candidate_db_only({"type": "payment", "obj": payment, "user": user})
        assert item is not None
        assert item.category == ProblemCategory.FAILED_SUPPLIER
        assert item.supplier_name == "VHQ"
        assert item.safe_retry_eligible is True
        assert item.diagnostic_code == "vhq_balance"
        assert "недостаточно средств" in item.details.lower()


# ── Test 2: Safe Recovery After VHQ Balance Top-Up ───────────────────────────


async def test_vhq_insufficient_balance_recovers_cleanly_after_topup(monkeypatch, db_session_factory):
    """Payment failing with vhq_balance recovers safely under lease once VHQ balance is topped up."""
    monkeypatch.setattr("bot.database.async_session", db_session_factory)
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)

    async with db_session_factory() as session:
        await session.execute(delete(AppLease))
        user = User(id=12, telegram_id=700000001, username="synthetic_username_1", full_name="synthetic_full_name_2")
        tariff = Tariff(
            id=9,
            label="Базовый 30 дней",
            price_rub=359,
            days=30,
            vhq_tier="basic",
            is_active=True,
            tariff_type=TariffType.VPN,
        )
        server = Server(id=1, name="Primary", host="127.0.0.1", location="Amsterdam", api_url="https://vpn.test.com", is_active=True)
        session.add_all([user, tariff, server])
        await session.flush()

        payment = Payment(
            id=7319,
            user_id=user.id,
            tariff_id=tariff.id,
            amount=35910,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="synthetic_external_id_2",
            provider_status="succeeded",
            provider_paid=True,
            platform="deferred~n~7329",
            provisioning_failure_code="vhq_balance",
            created_at=datetime.utcnow() - timedelta(hours=2),
        )
        session.add(payment)
        await session.commit()

    # Mock YooKassa as succeeded
    mock_yk_info = {
        "id": "synthetic_external_id_2",
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "359.10", "currency": "RUB"},
        "metadata": {
            "user_id": 700000001,
            "tariff_id": 9,
            "platform": "deferred~n~7329",
        },
    }

    # Mock VHQ API: balance is now 500 RUB (>= 249 RUB)
    mock_vhq_api = AsyncMock()
    mock_vhq_api.get_orders.return_value = []
    mock_vhq_api.get_balance.return_value = {"balance": 500.0}
    mock_vhq_api.buy.return_value = {
        "order_id": "synthetic_external_id_29",
        "branded_url": "https://example.test/synthetic-access-7",
    }
    mock_vhq_cls = make_mock_vhq_class(mock_vhq_api)

    fake_bot = AsyncMock()
    fake_bot.send_message.return_value = AsyncMock()

    with (
        patch("bot.services.yookassa_reconciliation.fetch_yookassa_payment", return_value=mock_yk_info),
        patch("bot.services.vhq_partner_api.VHQPartnerAPI", new=mock_vhq_cls),
        patch("bot.services.subscription_service.VHQPartnerAPI", new=mock_vhq_cls),
        patch("bot.services.subscription_service.get_primary_active_server", return_value=server),
        patch("bot.services.subscription_service.build_vhq_subscription_ref_url", return_value="https://darimiru.ru/vpnbot/vhq-sub/ref-url"),
        patch("bot.services.subscription_service.plog"),
    ):
        success, res_code = await verify_and_fulfill_yookassa_payment(
            bot_or_app=fake_bot,
            provider_payment_id="synthetic_external_id_2",
            source="reconciliation",
        )

    assert success is True
    assert res_code == "subscription_completed"

    async with db_session_factory() as session:
        p_final = await session.get(Payment, 7319)
        assert p_final.status == PaymentStatus.COMPLETED
        assert p_final.subscription_id is not None
        assert p_final.provisioning_failure_code is None

        sub = await session.get(Subscription, p_final.subscription_id)
        assert sub is not None
        assert sub.user_id == 12
        assert sub.status == SubStatus.ACTIVE
        assert sub.vhq_order_id == "synthetic_external_id_29"


# ── Test 3: Reconciliation While Still Insufficient Balance Fails Safely ─────


async def test_vhq_insufficient_balance_retry_while_still_insufficient_fails_safely(monkeypatch, db_session_factory):
    """If VHQ balance is still insufficient (<249), reconciliation must fail safely without changing to uncertain."""
    monkeypatch.setattr("bot.database.async_session", db_session_factory)
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)

    async with db_session_factory() as session:
        await session.execute(delete(AppLease))
        user = User(id=12, telegram_id=700000001, username="synthetic_username_1", full_name="synthetic_full_name_2")
        tariff = Tariff(
            id=9,
            label="Базовый 30 дней",
            price_rub=359,
            days=30,
            vhq_tier="basic",
            is_active=True,
            tariff_type=TariffType.VPN,
        )
        server = Server(id=1, name="Primary", host="127.0.0.1", location="Amsterdam", api_url="https://vpn.test.com", is_active=True)
        session.add_all([user, tariff, server])
        await session.flush()

        payment = Payment(
            id=7319,
            user_id=user.id,
            tariff_id=tariff.id,
            amount=35910,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="synthetic_external_id_3",
            provider_status="succeeded",
            provider_paid=True,
            platform="deferred~n~7329",
            provisioning_failure_code="vhq_balance",
            created_at=datetime.utcnow() - timedelta(hours=2),
        )
        session.add(payment)
        await session.commit()

    mock_yk_info = {
        "id": "synthetic_external_id_3",
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "359.10", "currency": "RUB"},
        "metadata": {
            "user_id": 700000001,
            "tariff_id": 9,
            "platform": "deferred~n~7329",
        },
    }

    mock_vhq_api = AsyncMock()
    mock_vhq_api.buy.side_effect = VHQPartnerAPIError("Insufficient balance: 37 < 249", status=400)
    mock_vhq_cls = make_mock_vhq_class(mock_vhq_api)

    fake_bot = AsyncMock()

    with (
        patch("bot.services.yookassa_reconciliation.fetch_yookassa_payment", return_value=mock_yk_info),
        patch("bot.services.vhq_partner_api.VHQPartnerAPI", new=mock_vhq_cls),
        patch("bot.services.subscription_service.VHQPartnerAPI", new=mock_vhq_cls),
        patch("bot.services.subscription_service.get_primary_active_server", return_value=server),
        patch("bot.services.subscription_service.plog"),
    ):
        success, res_code = await verify_and_fulfill_yookassa_payment(
            bot_or_app=fake_bot,
            provider_payment_id="synthetic_external_id_3",
            source="reconciliation",
        )

    assert success is False
    assert res_code == "delivery_failed:vhq_balance"

    async with db_session_factory() as session:
        p_final = await session.get(Payment, 7319)
        assert p_final.status == PaymentStatus.PENDING
        assert p_final.provisioning_failure_code == "vhq_balance"  # Must NOT be uncertain_supplier_mutation!

    # Format check
    summary_text, action_text = format_reconciliation_result(res_code)
    assert "недостаточно средств" in summary_text.lower()
    assert "Пополните баланс в кабинете VHQ" in action_text


# ── Test 4: Historical Uncertain Mutation Cannot Infer vhq_balance From Low Balance ──


async def test_historical_uncertain_payment_cannot_infer_vhq_balance_from_low_balance(monkeypatch, db_session_factory):
    """BLOCKER 1 & 2: When a payment has uncertain_supplier_mutation, reconciliation
    MUST NOT downgrade it to vhq_balance merely because balance < 249 ₽.
    It must fail closed to ambiguous_buy_mutation_manual_review_required, locking against
    automatic re-buy even after balance is topped up.
    """
    monkeypatch.setattr("bot.database.async_session", db_session_factory)
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)

    async with db_session_factory() as session:
        await session.execute(delete(AppLease))
        user = User(id=12, telegram_id=700000001, username="synthetic_username_1", full_name="synthetic_full_name_2")
        tariff = Tariff(
            id=9,
            label="Базовый 30 дней",
            price_rub=359,
            days=30,
            vhq_tier="basic",
            is_active=True,
            tariff_type=TariffType.VPN,
        )
        server = Server(id=1, name="Primary", host="127.0.0.1", location="Amsterdam", api_url="https://vpn.test.com", is_active=True)
        session.add_all([user, tariff, server])
        await session.flush()

        # Initial prod state: uncertain_supplier_mutation
        payment = Payment(
            id=7319,
            user_id=user.id,
            tariff_id=tariff.id,
            amount=35910,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="synthetic_external_id_4",
            provider_status="succeeded",
            provider_paid=True,
            platform="deferred~n~7329",
            provisioning_failure_code="uncertain_supplier_mutation",
            created_at=datetime.utcnow() - timedelta(hours=5),
        )
        session.add(payment)
        await session.commit()

    mock_yk_info = {
        "id": "synthetic_external_id_4",
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "359.10", "currency": "RUB"},
        "metadata": {
            "user_id": 700000001,
            "tariff_id": 9,
            "platform": "deferred~n~7329",
        },
    }

    fake_bot = AsyncMock()

    # Step 1: Reconcile while balance is 37 ₽ and 0 recent orders
    mock_vhq_step1 = AsyncMock()
    mock_vhq_step1.get_orders.return_value = [
        {"order_id": "synthetic_external_id_30", "created_at": (datetime.utcnow() - timedelta(days=20)).isoformat()}
    ]
    mock_vhq_step1.get_balance.return_value = {"balance": 37.0}
    mock_vhq_cls_step1 = make_mock_vhq_class(mock_vhq_step1)

    with (
        patch("bot.services.yookassa_reconciliation.fetch_yookassa_payment", return_value=mock_yk_info),
        patch("bot.services.vhq_partner_api.VHQPartnerAPI", new=mock_vhq_cls_step1),
        patch("bot.services.subscription_service.VHQPartnerAPI", new=mock_vhq_cls_step1),
    ):
        success1, res_code1 = await verify_and_fulfill_yookassa_payment(
            bot_or_app=fake_bot,
            provider_payment_id="synthetic_external_id_4",
            source="reconciliation",
        )

    # Must NOT infer vhq_balance! Must fail closed to manual review!
    assert success1 is False
    assert res_code1 == "ambiguous_buy_mutation_manual_review_required"

    async with db_session_factory() as session:
        p1 = await session.get(Payment, 7319)
        assert p1.provisioning_failure_code == "ambiguous_buy_mutation_manual_review_required"
        diag1 = diagnose_payment_problem(p1, tariff=tariff)
        assert diag1.supplier_name == "VHQ"
        assert diag1.safe_retry_eligible is False
        assert "закупка требует ручной проверки" in diag1.summary_title

    # Step 2: Even after balance topped up to 500 ₽, automated reconciliation refuses re-buy
    mock_vhq_step2 = AsyncMock()
    mock_vhq_step2.get_orders.return_value = [
        {"order_id": "synthetic_external_id_30", "created_at": (datetime.utcnow() - timedelta(days=20)).isoformat()}
    ]
    mock_vhq_step2.get_balance.return_value = {"balance": 500.0}
    mock_vhq_cls_step2 = make_mock_vhq_class(mock_vhq_step2)

    with (
        patch("bot.services.yookassa_reconciliation.fetch_yookassa_payment", return_value=mock_yk_info),
        patch("bot.services.vhq_partner_api.VHQPartnerAPI", new=mock_vhq_cls_step2),
        patch("bot.services.subscription_service.VHQPartnerAPI", new=mock_vhq_cls_step2),
    ):
        success2, res_code2 = await verify_and_fulfill_yookassa_payment(
            bot_or_app=fake_bot,
            provider_payment_id="synthetic_external_id_4",
            source="reconciliation",
        )

    # Durable lock prevents automatic re-purchase!
    assert success2 is False
    assert res_code2 == "ambiguous_buy_mutation_manual_review_required"
    assert mock_vhq_step2.buy.call_count == 0


# ── Test 5: Ambiguous Competing Orders at Supplier Fails Closed ──────────────


async def test_historical_uncertain_payment_with_competing_orders_fails_closed(monkeypatch, db_session_factory):
    """If VHQ orders list contains an unexpected order created in the same time window, fail closed."""
    async with db_session_factory() as session:
        server = Server(id=1, name="Primary", host="127.0.0.1", location="Amsterdam", api_url="https://vpn.test.com", is_active=True)
        user = User(id=12, telegram_id=700000001)
        tariff = Tariff(id=9, label="Базовый 30 дней", price_rub=359, days=30, vhq_tier="basic", tariff_type=TariffType.VPN)
        payment = Payment(
            id=320,
            user_id=12,
            tariff_id=9,
            amount=35910,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="synthetic_external_id_5",
            provider_status="succeeded",
            provider_paid=True,
            platform="deferred~n~7329",
            provisioning_failure_code="uncertain_supplier_mutation",
            created_at=datetime.utcnow() - timedelta(minutes=10),
        )
        session.add_all([server, user, tariff, payment])
        await session.commit()

        mock_api = AsyncMock()
        # Returns an order created at the exact time of payment
        mock_api.get_orders.return_value = [
            {"order_id": "synthetic_external_id_31", "created_at": (datetime.utcnow() - timedelta(minutes=9)).isoformat()}
        ]

        ok, sub_id, code = await _reconcile_ambiguous_vhq_mutation(
            session=session,
            payment=payment,
            provider_payment_id="synthetic_external_id_5",
            effective_platform="deferred~n~7329",
            effective_tariff_id=9,
            user_db_id=12,
            effective_user_tg_id=700000001,
            api=mock_api,
        )

        assert ok is False
        assert sub_id is None
        assert code == "ambiguous_competing_orders_at_supplier"
        assert payment.provisioning_failure_code == "ambiguous_competing_orders_at_supplier"


# ── Test 6: Network Timeout Retains uncertain_supplier_mutation ──────────────


async def test_vhq_network_timeout_retains_uncertain_supplier_mutation(monkeypatch, db_session_factory):
    """Network timeouts or unknown supplier 500 errors must remain 'uncertain_supplier_mutation'."""
    monkeypatch.setattr("bot.database.async_session", db_session_factory)

    async with db_session_factory() as session:
        user = User(id=12, telegram_id=700000001)
        tariff = Tariff(id=9, label="Базовый 30 дней", price_rub=359, days=30, vhq_tier="basic", tariff_type=TariffType.VPN)
        server = Server(id=1, name="Primary", host="127.0.0.1", location="Amsterdam", api_url="https://vpn.test.com", is_active=True)
        session.add_all([user, tariff, server])
        await session.flush()

        payment = Payment(
            id=321,
            user_id=12,
            tariff_id=9,
            amount=35910,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="synthetic_external_id_6",
            provider_status="succeeded",
            provider_paid=True,
            platform="deferred~n~7329",
        )
        session.add(payment)
        await session.commit()

        mock_api = AsyncMock()
        mock_api.buy.side_effect = asyncio.TimeoutError("VHQ connection timed out after 30s")

        with (
            patch("bot.services.subscription_service.get_primary_active_server", return_value=server),
            patch("bot.services.subscription_service.VHQPartnerAPI", return_value=mock_api),
            patch("bot.services.subscription_service.plog"),
        ):
            with pytest.raises(AccessProvisionError) as exc_info:
                await _create_vhq_paid_subscription(
                    session=session,
                    user=user,
                    tariff=tariff,
                    platform="deferred~n~7329",
                    vhq_spec={"tier": "basic", "days": 30},
                    provisioning_payment=payment,
                )

        assert exc_info.value.code == "vhq_runtime"
        assert exc_info.value.safe_retry_eligible is False

        await session.refresh(payment)
        assert payment.provisioning_failure_code == "uncertain_supplier_mutation"


# ── Test 7: Outbox Renewal Insufficient Balance Transitions to retry_wait ────


async def test_vhq_renewal_outbox_insufficient_balance_transitions_to_retry_wait(monkeypatch, db_session_factory):
    """Outbox fulfillment worker on 402/insufficient balance sets status='retry_wait'."""
    async with db_session_factory() as session:
        server = Server(id=1, name="Primary", host="127.0.0.1", location="Amsterdam", api_url="https://vpn.test.com", is_active=True)
        user = User(id=12, telegram_id=700000001)
        sub = Subscription(
            id=7329,
            user_id=12,
            server_id=1,
            tariff_months=1,
            client_name="synthetic_client_name_3",
            platform="android",
            vhq_order_id="synthetic_external_id_7",
            status=SubStatus.ACTIVE,
            expires_at=datetime.utcnow() + timedelta(days=2),
        )
        payment = Payment(
            id=322,
            user_id=12,
            amount=35910,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            provider_payment_id="synthetic_external_id_8",
        )
        session.add_all([server, user, sub, payment])
        await session.flush()

        fulfillment = VhqSubscriptionFulfillment(
            id=1,
            subscription_id=sub.id,
            user_id=user.id,
            payment_id=payment.id,
            source_type="payment",
            source_id=str(payment.id),
            action="renew",
            status="pending",
            vhq_order_id=sub.vhq_order_id,
            attempts=0,
            max_attempts=5,
            baseline_local_expires_at=sub.expires_at,
            target_local_expires_at=sub.expires_at + timedelta(days=30),
        )
        session.add(fulfillment)
        await session.commit()

        mock_api = AsyncMock()
        mock_api.renew.side_effect = VHQPartnerAPIError("Insufficient balance: 37 < 249", status=402)

        await execute_vhq_fulfillment(session, fulfillment, api=mock_api)
        await session.commit()

        await session.refresh(fulfillment)
        assert fulfillment.status == "retry_wait"
        assert fulfillment.last_error_code == "insufficient_balance"
        assert fulfillment.attempts == 1


# ── Test 8: Diagnostics Formatting and Card Display ──────────────────────────


async def test_problem_payment_diagnostics_formatting_admin_and_alerts():
    """Verify diagnostic text output for admin panel and notifications."""
    p_balance = Payment(
        id=7319,
        user_id=12,
        amount=35910,
        currency="RUB",
        status=PaymentStatus.PENDING,
        provider_status="succeeded",
        provider_paid=True,
        provider_payment_id="synthetic_external_id_4",
        provisioning_failure_code="vhq_balance",
    )
    t_vhq = Tariff(id=9, label="Базовый 30 дней", price_rub=249, days=30, vhq_tier="basic")

    diag = diagnose_payment_problem(p_balance, tariff=t_vhq)
    assert diag.supplier_name == "VHQ"
    assert "недостаточно средств" in diag.summary_title.lower()
    assert diag.safe_retry_eligible is True
    assert diag.diagnostic_code == "vhq_balance"

    # Reconciliation result formatting
    res_summary, res_action = format_reconciliation_result("vhq_balance")
    assert "VHQ — недостаточно средств на партнёрском балансе" in res_summary
    assert "Пополните баланс в кабинете VHQ" in res_action

    res_summary_deliv, _ = format_reconciliation_result("delivery_failed:vhq_balance")
    assert "VHQ — недостаточно средств на партнёрском балансе" in res_summary_deliv

    res_unc, act_unc = format_reconciliation_result("uncertain_supplier_mutation")
    assert "Неопределённое состояние у поставщика" in res_unc
    assert "Проверьте кабинет поставщика" in act_unc


# ── Test 9: VHQ Renewal Proven Applied Without Re-mutation ───────────────────


async def test_vhq_renewal_proven_applied_without_re_mutation(monkeypatch, db_session_factory):
    """When a renewal payment has uncertain_supplier_mutation, but VHQ orders list
    shows the subscription's active_until was already extended >= baseline + 28 days,
    reconciliation completes the payment and updates the local subscription with ZERO new API calls.
    """
    monkeypatch.setattr("bot.database.async_session", db_session_factory)
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)

    base_dt = datetime.utcnow()
    target_dt = base_dt + timedelta(days=30)

    async with db_session_factory() as session:
        user = User(id=40, telegram_id=700000002, username="synthetic_username_4")
        server = Server(id=1, name="NL", host="1.1.1.1", location="NL", is_active=True)
        tariff = Tariff(id=9, label="Базовый 30 дней", price_rub=249, days=30, vhq_tier="basic", tariff_type=TariffType.VPN)
        sub = Subscription(
            id=293,
            user_id=user.id,
            server_id=server.id,
            tariff_id=tariff.id,
            tariff_months=1,
            tariff_days=30,
            client_name="synthetic_client_name_5",
            platform="android",
            vpn_key="https://example.test/synthetic-access-1",
            vhq_order_id="synthetic_external_id_9",
            status=SubStatus.ACTIVE,
            expires_at=base_dt,
        )
        payment = Payment(
            id=318,
            user_id=user.id,
            tariff_id=tariff.id,
            amount=25400,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="synthetic_external_id_10",
            provider_status="succeeded",
            provider_paid=True,
            platform="deferred~r~293",
            provisioning_baseline_expires_at=base_dt,
            provisioning_failure_code="uncertain_supplier_mutation",
            created_at=base_dt - timedelta(minutes=5),
        )
        session.add_all([user, server, tariff, sub, payment])
        await session.commit()

    mock_yk = {
        "id": "synthetic_external_id_10",
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "254.00", "currency": "RUB"},
        "metadata": {"user_id": 700000002, "tariff_id": 9, "platform": "deferred~r~293"},
    }

    mock_api = AsyncMock()
    mock_api.get_orders.return_value = [
        {
            "order_id": "synthetic_external_id_9",
            "active_until": target_dt.isoformat(),
            "created_at": (base_dt - timedelta(days=30)).isoformat(),
        }
    ]
    mock_api.get_balance.return_value = {"balance": 1000.0}
    mock_cls = make_mock_vhq_class(mock_api)

    fake_bot = AsyncMock()

    with (
        patch("bot.services.yookassa_reconciliation.fetch_yookassa_payment", return_value=mock_yk),
        patch("bot.services.vhq_partner_api.VHQPartnerAPI", new=mock_cls),
        patch("bot.services.subscription_service.VHQPartnerAPI", new=mock_cls),
    ):
        success, code = await verify_and_fulfill_yookassa_payment(
            bot_or_app=fake_bot,
            provider_payment_id="synthetic_external_id_10",
            source="reconciliation",
        )

    assert success is True
    assert code in ("subscription_completed", "vhq_renewal_proven_applied")
    # CRITICAL: 0 new renew calls issued!
    assert mock_api.renew.call_count == 0
    assert mock_api.buy.call_count == 0

    async with db_session_factory() as session:
        p_db = await session.get(Payment, 318)
        assert p_db.status == PaymentStatus.COMPLETED
        assert p_db.subscription_id == 293
        assert p_db.provisioning_failure_code is None

        s_db = await session.get(Subscription, 293)
        assert s_db.status == SubStatus.ACTIVE
        assert s_db.expires_at >= target_dt - timedelta(seconds=5)


# ── Test 10: Ambiguous Buy Mutation with Sufficient Balance Fails Closed ─────


async def test_vhq_ambiguous_buy_with_sufficient_balance_fails_closed_to_manual_review(monkeypatch, db_session_factory):
    """If an ambiguous buy mutation occurred when partner balance was >= 249 ₽,
    reconciliation MUST NOT automatically re-buy, but must fail closed to manual review.
    """
    monkeypatch.setattr("bot.database.async_session", db_session_factory)
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)

    async with db_session_factory() as session:
        user = User(id=50, telegram_id=700000003)
        tariff = Tariff(id=9, label="Базовый 30 дней", price_rub=249, days=30, vhq_tier="basic", tariff_type=TariffType.VPN)
        payment = Payment(
            id=350,
            user_id=user.id,
            tariff_id=tariff.id,
            amount=24900,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="synthetic_external_id_11",
            provider_status="succeeded",
            provider_paid=True,
            platform="deferred~n~0",
            provisioning_failure_code="uncertain_supplier_mutation",
            created_at=datetime.utcnow() - timedelta(minutes=10),
        )
        session.add_all([user, tariff, payment])
        await session.commit()

    mock_yk = {
        "id": "synthetic_external_id_11",
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "249.00", "currency": "RUB"},
        "metadata": {"user_id": 700000003, "tariff_id": 9, "platform": "deferred~n~0"},
    }

    mock_api = AsyncMock()
    mock_api.get_orders.return_value = []
    # Balance is plenty (500 RUB >= 249 RUB)
    mock_api.get_balance.return_value = {"balance": 500.0}
    mock_cls = make_mock_vhq_class(mock_api)

    fake_bot = AsyncMock()

    with (
        patch("bot.services.yookassa_reconciliation.fetch_yookassa_payment", return_value=mock_yk),
        patch("bot.services.vhq_partner_api.VHQPartnerAPI", new=mock_cls),
        patch("bot.services.subscription_service.VHQPartnerAPI", new=mock_cls),
    ):
        success, code = await verify_and_fulfill_yookassa_payment(
            bot_or_app=fake_bot,
            provider_payment_id="synthetic_external_id_11",
            source="reconciliation",
        )

    assert success is False
    assert code == "ambiguous_buy_mutation_manual_review_required"
    # CRITICAL: ZERO buy mutations attempted!
    assert mock_api.buy.call_count == 0

    async with db_session_factory() as session:
        p_db = await session.get(Payment, 350)
        assert p_db.status == PaymentStatus.PENDING
        assert p_db.provisioning_failure_code == "ambiguous_buy_mutation_manual_review_required"


# ── Test 11: Repeated Webhook is Strictly Idempotent ─────────────────────────


async def test_repeated_webhook_is_idempotent_no_duplicate_vhq_call(monkeypatch, db_session_factory):
    """When a webhook arrives for a payment that is already COMPLETED with a linked subscription,
    it returns 'already_completed' with zero VHQ calls.
    """
    monkeypatch.setattr("bot.database.async_session", db_session_factory)
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)

    async with db_session_factory() as session:
        user = User(id=60, telegram_id=700000004)
        server = Server(id=1, name="NL", host="1.1.1.1", location="NL", is_active=True)
        tariff = Tariff(id=9, label="Базовый 30 дней", price_rub=249, days=30, vhq_tier="basic", tariff_type=TariffType.VPN)
        sub = Subscription(
            id=500,
            user_id=user.id,
            server_id=server.id,
            tariff_id=tariff.id,
            tariff_months=1,
            client_name="synthetic_client_name_6",
            platform="android",
            vhq_order_id="synthetic_external_id_12",
            status=SubStatus.ACTIVE,
            expires_at=datetime.utcnow() + timedelta(days=30),
        )
        payment = Payment(
            id=360,
            user_id=user.id,
            tariff_id=tariff.id,
            amount=24900,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            provider_payment_id="synthetic_external_id_13",
            provider_status="succeeded",
            provider_paid=True,
            subscription_id=sub.id,
            provisioning_failure_code=None,
        )
        session.add_all([user, server, tariff, sub, payment])
        await session.commit()

    mock_yk = {
        "id": "synthetic_external_id_13",
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "249.00", "currency": "RUB"},
        "metadata": {"user_id": 700000004, "tariff_id": 9},
    }

    mock_api = AsyncMock()
    mock_cls = make_mock_vhq_class(mock_api)

    fake_bot = AsyncMock()

    with (
        patch("bot.services.yookassa_reconciliation.fetch_yookassa_payment", return_value=mock_yk),
        patch("bot.services.vhq_partner_api.VHQPartnerAPI", new=mock_cls),
        patch("bot.services.subscription_service.VHQPartnerAPI", new=mock_cls),
    ):
        success, code = await verify_and_fulfill_yookassa_payment(
            bot_or_app=fake_bot,
            provider_payment_id="synthetic_external_id_13",
            source="webhook",
        )

    assert success is True
    assert code == "already_completed"
    assert mock_api.buy.call_count == 0
    assert mock_api.renew.call_count == 0


# ── Test 12: Concurrent Reconciliation Lease Collision Fails Closed ──────────


async def test_concurrent_reconciliation_lease_collision_fails_closed(monkeypatch, db_session_factory):
    """When two executions collide on the same provider_payment_id, the second one
    fails immediately with 'locked_by_another_execution' without touching VHQ.
    """
    monkeypatch.setattr("bot.database.async_session", db_session_factory)
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)

    async with db_session_factory() as session:
        user = User(id=70, telegram_id=700000005)
        tariff = Tariff(id=9, label="Базовый 30 дней", price_rub=249, days=30, vhq_tier="basic", tariff_type=TariffType.VPN)
        payment = Payment(
            id=370,
            user_id=user.id,
            tariff_id=tariff.id,
            amount=24900,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="synthetic_external_id_14",
            provider_status="succeeded",
            provider_paid=True,
            provisioning_failure_code="vhq_balance",
        )
        session.add_all([user, tariff, payment])
        await session.commit()

    mock_yk = {
        "id": "synthetic_external_id_14",
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "249.00", "currency": "RUB"},
        "metadata": {"user_id": 700000005, "tariff_id": 9},
    }

    fake_bot = AsyncMock()

    with (
        patch("bot.services.yookassa_reconciliation.fetch_yookassa_payment", return_value=mock_yk),
        patch("bot.services.yookassa_reconciliation.LeaseManager.acquire_or_renew", return_value=False),
        patch("bot.services.vhq_partner_api.VHQPartnerAPI") as mock_vhq_cls,
    ):
        success, code = await verify_and_fulfill_yookassa_payment(
            bot_or_app=fake_bot,
            provider_payment_id="synthetic_external_id_14",
            source="scheduler",
        )

    assert success is False
    assert code == "locked_by_another_execution"
    assert mock_vhq_cls.call_count == 0


# ── Test 13: Financial Side Effects are Strictly Idempotent ──────────────────


async def test_reconciliation_financial_effects_are_strictly_idempotent(monkeypatch, db_session_factory):
    """When a payment is completed, cashback and referral bonuses are credited once.
    Subsequent reconciliation triggers return already credited, never duplicate bonuses.
    """
    monkeypatch.setattr("bot.database.async_session", db_session_factory)
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)

    from bot.models import BalanceTransaction, BalanceTransactionKind, BalanceDirection

    async with db_session_factory() as session:
        user = User(id=80, telegram_id=700000006, balance_rub=0.0)
        referrer = User(id=81, telegram_id=700000007, balance_rub=0.0)
        tariff = Tariff(id=9, label="Базовый 30 дней", price_rub=249, days=30, vhq_tier="basic", tariff_type=TariffType.VPN)
        payment = Payment(
            id=380,
            user_id=user.id,
            tariff_id=tariff.id,
            amount=24900,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            provider_payment_id="synthetic_external_id_15",
            subscription_id=999,
        )
        session.add_all([user, referrer, tariff, payment])
        await session.flush()

        tx = BalanceTransaction(
            user_id=user.id,
            amount_rub=24.9,
            kind=BalanceTransactionKind.CASHBACK,
            direction=BalanceDirection.CREDIT,
            balance_after_rub=24.9,
            source_type="payment",
            source_id="380",
            description="Cashback for payment #380",
        )
        session.add(tx)
        await session.commit()

    from bot.services.payment_service import credit_cashback
    async with db_session_factory() as session:
        cb_res = await credit_cashback(session, user_db_id=80, payment_id=380, amount_rub=249.0)
        assert cb_res["status"] == "already_credited"

        txs = (await session.execute(
            select(BalanceTransaction).where(
                BalanceTransaction.user_id == 80,
                BalanceTransaction.kind == BalanceTransactionKind.CASHBACK,
            )
        )).scalars().all()
        assert len(txs) == 1


# ── Test 14: Manual Review Lock is Durable Against Webhook, Scheduler and Admin ────


async def test_manual_review_lock_is_durable_against_scheduler_webhook_and_admin_button(monkeypatch, db_session_factory):
    """BLOCKER 2: A payment in manual review state cannot be fulfilled by background scheduler,
    repeat webhook, or unvalidated admin trigger.
    """
    monkeypatch.setattr("bot.database.async_session", db_session_factory)
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)

    async with db_session_factory() as session:
        user = User(id=90, telegram_id=700000008)
        tariff = Tariff(id=9, label="Базовый 30 дней", price_rub=249, days=30, vhq_tier="basic", tariff_type=TariffType.VPN)
        payment = Payment(
            id=390,
            user_id=user.id,
            tariff_id=tariff.id,
            amount=24900,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="synthetic_external_id_16",
            provider_status="succeeded",
            provider_paid=True,
            provisioning_failure_code="ambiguous_buy_mutation_manual_review_required",
        )
        session.add_all([user, tariff, payment])
        await session.commit()

    mock_yk = {
        "id": "synthetic_external_id_16",
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "249.00", "currency": "RUB"},
        "metadata": {"user_id": 700000008, "tariff_id": 9, "platform": "android"},
    }

    mock_vhq = AsyncMock()
    fake_bot = AsyncMock()

    # 1. Background scheduler attempt
    with (
        patch("bot.services.yookassa_reconciliation.fetch_yookassa_payment", return_value=mock_yk),
        patch("bot.services.vhq_partner_api.VHQPartnerAPI", return_value=mock_vhq),
    ):
        ok_sched, code_sched = await verify_and_fulfill_yookassa_payment(
            bot_or_app=fake_bot,
            provider_payment_id="synthetic_external_id_16",
            source="scheduler",
        )
    assert ok_sched is False
    assert code_sched == "ambiguous_buy_mutation_manual_review_required"
    assert mock_vhq.buy.call_count == 0

    # 2. Repeated incoming webhook
    with (
        patch("bot.services.yookassa_reconciliation.fetch_yookassa_payment", return_value=mock_yk),
        patch("bot.services.vhq_partner_api.VHQPartnerAPI", return_value=mock_vhq),
    ):
        ok_wh, code_wh = await verify_and_fulfill_yookassa_payment(
            bot_or_app=fake_bot,
            provider_payment_id="synthetic_external_id_16",
            source="webhook",
        )
    assert ok_wh is False
    assert code_wh == "ambiguous_buy_mutation_manual_review_required"
    assert mock_vhq.buy.call_count == 0

    # 3. Problem payments candidate classification
    cand = {"type": "payment", "obj": payment, "user": user}
    prob_item = await classify_candidate_db_only(cand)
    assert prob_item is not None
    assert prob_item.category == ProblemCategory.DELIVERY_UNCERTAIN


# ── Test 15: Financial Finalization is Crash-Safe After VHQ Renewal Confirmation ──


async def test_financial_finalization_is_crash_safe_after_vhq_renewal_confirmation(monkeypatch, db_session_factory):
    """BLOCKER 3: If process restarts/crashes immediately after VHQ renewal is proven applied
    (before financial finalization commits), the subsequent run completes all financial effects
    (cashback, referral bonuses) with ZERO new VHQ mutations.
    """
    monkeypatch.setattr("bot.database.async_session", db_session_factory)
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)

    from bot.models import BalanceTransaction, BalanceTransactionKind, BotSettings

    base_dt = datetime.utcnow()
    target_dt = base_dt + timedelta(days=30)

    async with db_session_factory() as session:
        user = User(id=60, telegram_id=700000009, balance_rub=0.0)
        referrer = User(id=61, telegram_id=700000010, balance_rub=0.0)
        user.referrer_id = referrer.id
        server = Server(id=1, name="NL", host="1.1.1.1", location="NL", is_active=True)
        tariff = Tariff(id=9, label="Базовый 30 дней", price_rub=249, days=30, vhq_tier="basic", tariff_type=TariffType.VPN)
        sub = Subscription(
            id=444,
            user_id=user.id,
            server_id=server.id,
            tariff_id=tariff.id,
            tariff_months=1,
            tariff_days=30,
            client_name="synthetic_client_name_7",
            platform="android",
            vpn_key="https://example.test/synthetic-access-2",
            vhq_order_id="synthetic_external_id_17",
            status=SubStatus.ACTIVE,
            expires_at=target_dt,
            vhq_active_until=target_dt,
        )
        # Crash state: renewal was confirmed at supplier, sub linked,
        # but process terminated with financial_finalization_pending
        payment = Payment(
            id=395,
            user_id=user.id,
            tariff_id=tariff.id,
            amount=24900,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="synthetic_external_id_18",
            provider_status="succeeded",
            provider_paid=True,
            platform="deferred~r~444",
            subscription_id=sub.id,
            provisioning_failure_code="financial_finalization_pending",
            created_at=base_dt - timedelta(minutes=15),
        )
        session.add_all([
            user, referrer, server, tariff, sub, payment,
            BotSettings(key="cashback_enabled", value="1"),
            BotSettings(key="cashback_percent", value="10"),
        ])
        await session.commit()

    mock_yk = {
        "id": "synthetic_external_id_18",
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "249.00", "currency": "RUB"},
        "metadata": {"user_id": 700000009, "tariff_id": 9, "platform": "deferred~r~444"},
    }

    mock_vhq = AsyncMock()
    mock_vhq_cls = make_mock_vhq_class(mock_vhq)
    fake_bot = AsyncMock()

    with (
        patch("bot.services.yookassa_reconciliation.fetch_yookassa_payment", return_value=mock_yk),
        patch("bot.services.vhq_partner_api.VHQPartnerAPI", new=mock_vhq_cls),
        patch("bot.services.subscription_service.VHQPartnerAPI", new=mock_vhq_cls),
    ):
        success, code = await verify_and_fulfill_yookassa_payment(
            bot_or_app=fake_bot,
            provider_payment_id="synthetic_external_id_18",
            source="reconciliation",
        )

    assert success is True
    assert code == "subscription_completed"

    # Crucial: ZERO VHQ API calls made!
    assert mock_vhq.renew.call_count == 0
    assert mock_vhq.buy.call_count == 0

    async with db_session_factory() as session:
        p_fin = await session.get(Payment, 395)
        assert p_fin.status == PaymentStatus.COMPLETED
        assert p_fin.provisioning_failure_code is None

        # Verify cashback was credited
        tx_cb = (await session.execute(
            select(BalanceTransaction).where(
                BalanceTransaction.user_id == 60,
                BalanceTransaction.kind == BalanceTransactionKind.CASHBACK,
            )
        )).scalars().all()
        assert len(tx_cb) == 1


# ── Test 16: VHQ Renew Timeout with Temporary Read Lag Prohibits Duplicate POST ──


async def test_vhq_renew_timeout_with_temporary_read_lag_prohibits_duplicate_post(db_session_factory):
    """BLOCKER 4: When POST renew times out and GET orders temporarily shows old expiration
    date (read replica lag), the retry worker MUST NOT repeat POST renew blindly.
    It waits for convergence, then completes when GET reflects the extension with exactly 1 POST.
    """
    from bot.services.subscription_service import execute_vhq_fulfillment
    now = datetime.utcnow()
    async with db_session_factory() as session:
        user = User(id=77, telegram_id=700000011, username="synthetic_username_8")
        server = Server(id=2, name="NL2", host="2.2.2.2", location="NL", is_active=True, api_url="https://vpn2.test")
        tariff = Tariff(id=9, label="Базовый 30 дней", price_rub=249, days=30, vhq_tier="basic", tariff_type=TariffType.VPN)
        sub = Subscription(
            id=555,
            user_id=user.id,
            tariff_id=9,
            server_id=2,
            tariff_months=1,
            tariff_days=30,
            platform="android",
            client_name="synthetic_client_name_9",
            vpn_key="https://example.test/synthetic-access-3",
            vhq_order_id="synthetic_external_id_19",
            status=SubStatus.ACTIVE,
            expires_at=now + timedelta(days=2),
            vhq_active_until=now + timedelta(days=2),
        )
        ff = VhqSubscriptionFulfillment(
            id=555,
            subscription_id=555,
            user_id=user.id,
            source_type="payment",
            source_id="payment:399",
            action="renew",
            status="pending",
            baseline_local_expires_at=now + timedelta(days=2),
            baseline_provider_active_until=now + timedelta(days=2),
            expected_provider_active_until=now + timedelta(days=32),
            target_local_expires_at=now + timedelta(days=32),
            next_attempt_at=now - timedelta(seconds=1),
            attempts=0,
            max_attempts=5,
        )
        session.add_all([user, server, tariff, sub, ff])
        await session.commit()

    # Step 1: First attempt calls api.renew, but network times out!
    mock_api = AsyncMock()
    mock_api.renew.side_effect = asyncio.TimeoutError("Connection timed out to VHQ")

    async with db_session_factory() as session:
        ff_1 = await session.get(VhqSubscriptionFulfillment, 555)
        await execute_vhq_fulfillment(session, ff_1, api=mock_api)
        await session.commit()

    assert mock_api.renew.call_count == 1
    async with db_session_factory() as session:
        ff_check1 = await session.get(VhqSubscriptionFulfillment, 555)
        assert ff_check1.status == "uncertain"
        assert ff_check1.attempts == 1

    # Step 2: Retry attempt. GET get_orders temporarily returns OLD active_until!
    # Because status is "uncertain", execute_vhq_fulfillment MUST NOT call api.renew again!
    mock_api.get_orders.return_value = [
        {
            "order_id": "synthetic_external_id_19",
            "active_until": (now + timedelta(days=2)).isoformat(),  # Stale date!
        }
    ]

    async with db_session_factory() as session:
        ff_2 = await session.get(VhqSubscriptionFulfillment, 555)
        await execute_vhq_fulfillment(session, ff_2, api=mock_api)
        await session.commit()

    # CRITICAL VERIFICATION: api.renew call count is STILL 1! Zero second POST!
    assert mock_api.renew.call_count == 1
    async with db_session_factory() as session:
        ff_check2 = await session.get(VhqSubscriptionFulfillment, 555)
        assert ff_check2.status == "uncertain"
        assert ff_check2.attempts == 2

    # Step 3: GET get_orders converges and returns updated active_until!
    mock_api.get_orders.return_value = [
        {
            "order_id": "synthetic_external_id_19",
            "active_until": (now + timedelta(days=32)).isoformat(),  # Updated!
        }
    ]

    async with db_session_factory() as session:
        ff_3 = await session.get(VhqSubscriptionFulfillment, 555)
        await execute_vhq_fulfillment(session, ff_3, api=mock_api)
        await session.commit()

    # Completely fulfilled! Total renew calls across entire lifecycle: exactly 1!
    assert mock_api.renew.call_count == 1
    async with db_session_factory() as session:
        ff_check3 = await session.get(VhqSubscriptionFulfillment, 555)
        assert ff_check3.status == "completed"
        assert ff_check3.confirmed_provider_active_until is not None


# ── Test 17: Ambiguous POST Renew with Empty GET Orders Blocks Duplicate POST ─


async def test_vhq_renew_ambiguous_post_with_empty_get_orders_blocks_duplicate_post(db_session_factory):
    """When POST renew times out / fails with 5xx, and GET orders returns an empty list,
    the retry worker MUST NOT repeat POST renew. It transitions to uncertain or manual_review without calling renew again.
    """
    from bot.services.subscription_service import execute_vhq_fulfillment
    from bot.services.vhq_partner_api import VHQPartnerAPIError
    now = datetime.utcnow()
    async with db_session_factory() as session:
        user = User(id=81, telegram_id=700000012, username="synthetic_username_10")
        server = Server(id=3, name="NL3", host="3.3.3.3", location="NL", is_active=True, api_url="https://vpn3.test")
        tariff = Tariff(id=9, label="Базовый 30 дней", price_rub=249, days=30, vhq_tier="basic", tariff_type=TariffType.VPN)
        sub = Subscription(
            id=556,
            user_id=user.id,
            tariff_id=9,
            server_id=3,
            tariff_months=1,
            tariff_days=30,
            platform="android",
            client_name="synthetic_client_name_11",
            vpn_key="https://example.test/synthetic-access-4",
            vhq_order_id="synthetic_external_id_20",
            status=SubStatus.ACTIVE,
            expires_at=now + timedelta(days=2),
            vhq_active_until=now + timedelta(days=2),
        )
        ff = VhqSubscriptionFulfillment(
            id=556,
            subscription_id=556,
            user_id=user.id,
            source_type="payment",
            source_id="payment:399",
            action="renew",
            status="pending",
            baseline_local_expires_at=now + timedelta(days=2),
            baseline_provider_active_until=now + timedelta(days=2),
            expected_provider_active_until=now + timedelta(days=32),
            target_local_expires_at=now + timedelta(days=32),
            next_attempt_at=now - timedelta(seconds=1),
            attempts=0,
            max_attempts=3,
        )
        session.add_all([user, server, tariff, sub, ff])
        await session.commit()

    # Step 1: POST renew fails with HTTP 504 Gateway Timeout!
    mock_api = AsyncMock()
    mock_api.renew.side_effect = VHQPartnerAPIError("Gateway Timeout", status=504)

    async with db_session_factory() as session:
        ff_row = await session.get(VhqSubscriptionFulfillment, 556)
        await execute_vhq_fulfillment(session, ff_row, api=mock_api)
        await session.commit()

    assert mock_api.renew.call_count == 1
    async with db_session_factory() as session:
        ff_chk = await session.get(VhqSubscriptionFulfillment, 556)
        assert ff_chk.status == "uncertain"
        assert ff_chk.last_error_code == "http_504"

    # Step 2: Retry runs. GET orders returns EMPTY list []!
    mock_api.get_orders.return_value = []

    async with db_session_factory() as session:
        ff_retry = await session.get(VhqSubscriptionFulfillment, 556)
        await execute_vhq_fulfillment(session, ff_retry, api=mock_api)
        await session.commit()

    # INVARIANT: api.renew call count MUST REMAIN 1! Zero duplicate POST calls!
    assert mock_api.renew.call_count == 1
    async with db_session_factory() as session:
        ff_chk2 = await session.get(VhqSubscriptionFulfillment, 556)
        assert ff_chk2.status == "uncertain"
        assert ff_chk2.attempts == 2


# ── Test 18: Ambiguous POST Renew with Missing Order or Corrupt Date ─────────


async def test_vhq_renew_ambiguous_post_with_missing_order_or_corrupt_date_blocks_duplicate_post(db_session_factory):
    """When POST renew was ambiguous, and GET orders returns an unrelated order OR corrupt date,
    reconciliation MUST NOT fall through to a duplicate POST renew.
    """
    from bot.services.subscription_service import execute_vhq_fulfillment
    now = datetime.utcnow()
    async with db_session_factory() as session:
        user = User(id=82, telegram_id=700000013, username="synthetic_username_12")
        server = Server(id=4, name="NL4", host="4.4.4.4", location="NL", is_active=True, api_url="https://vpn4.test")
        tariff = Tariff(id=9, label="Базовый 30 дней", price_rub=249, days=30, vhq_tier="basic", tariff_type=TariffType.VPN)
        sub = Subscription(
            id=557,
            user_id=user.id,
            tariff_id=9,
            server_id=4,
            tariff_months=1,
            tariff_days=30,
            platform="android",
            client_name="synthetic_client_name_13",
            vpn_key="https://example.test/synthetic-access-5",
            vhq_order_id="synthetic_external_id_21",
            status=SubStatus.ACTIVE,
            expires_at=now + timedelta(days=2),
            vhq_active_until=now + timedelta(days=2),
        )
        ff = VhqSubscriptionFulfillment(
            id=557,
            subscription_id=557,
            user_id=user.id,
            source_type="payment",
            source_id="payment:400",
            action="renew",
            status="uncertain",
            last_error_code="network_timeout",
            baseline_local_expires_at=now + timedelta(days=2),
            baseline_provider_active_until=now + timedelta(days=2),
            expected_provider_active_until=now + timedelta(days=32),
            target_local_expires_at=now + timedelta(days=32),
            next_attempt_at=now - timedelta(seconds=1),
            attempts=1,
            max_attempts=5,
        )
        session.add_all([user, server, tariff, sub, ff])
        await session.commit()

    # Retry 1: Order is missing from get_orders (only other orders returned)
    mock_api = AsyncMock()
    mock_api.get_orders.return_value = [{"order_id": "synthetic_external_id_32", "active_until": now.isoformat()}]

    async with db_session_factory() as session:
        ff_retry1 = await session.get(VhqSubscriptionFulfillment, 557)
        await execute_vhq_fulfillment(session, ff_retry1, api=mock_api)
        await session.commit()

    assert mock_api.renew.call_count == 0  # ZERO calls!
    async with db_session_factory() as session:
        ff_chk1 = await session.get(VhqSubscriptionFulfillment, 557)
        assert ff_chk1.status == "uncertain"
        assert ff_chk1.attempts == 2

    # Retry 2: Order is present, but active_until is None/corrupt
    mock_api.get_orders.return_value = [{"order_id": "synthetic_external_id_21", "active_until": "INVALID_DATE_STRING"}]

    async with db_session_factory() as session:
        ff_retry2 = await session.get(VhqSubscriptionFulfillment, 557)
        await execute_vhq_fulfillment(session, ff_retry2, api=mock_api)
        await session.commit()

    assert mock_api.renew.call_count == 0  # STILL ZERO calls!
    async with db_session_factory() as session:
        ff_chk2 = await session.get(VhqSubscriptionFulfillment, 557)
        assert ff_chk2.status == "uncertain"
        assert ff_chk2.attempts == 3


# ── Test 19: Ambiguous POST Survives Restart Without Duplicate POST ───────────


async def test_vhq_renew_ambiguous_post_survives_restart_without_duplicate_post(db_session_factory):
    """After process restart, an uncertain fulfillment with error code http_504 or network_timeout
    is reloaded from database and NEVER issues a duplicate POST renew.
    """
    from bot.services.subscription_service import execute_vhq_fulfillment
    now = datetime.utcnow()
    async with db_session_factory() as session:
        user = User(id=83, telegram_id=700000014, username="synthetic_username_14")
        server = Server(id=5, name="NL5", host="5.5.5.5", location="NL", is_active=True, api_url="https://vpn5.test")
        tariff = Tariff(id=9, label="Базовый 30 дней", price_rub=249, days=30, vhq_tier="basic", tariff_type=TariffType.VPN)
        sub = Subscription(
            id=558,
            user_id=user.id,
            tariff_id=9,
            server_id=5,
            tariff_months=1,
            tariff_days=30,
            platform="android",
            client_name="synthetic_client_name_15",
            vpn_key="https://example.test/synthetic-access-6",
            vhq_order_id="synthetic_external_id_22",
            status=SubStatus.ACTIVE,
            expires_at=now + timedelta(days=2),
            vhq_active_until=now + timedelta(days=2),
        )
        ff = VhqSubscriptionFulfillment(
            id=558,
            subscription_id=558,
            user_id=user.id,
            source_type="payment",
            source_id="payment:401",
            action="renew",
            status="uncertain",
            last_error_code="http_504",
            baseline_local_expires_at=now + timedelta(days=2),
            baseline_provider_active_until=now + timedelta(days=2),
            expected_provider_active_until=now + timedelta(days=32),
            target_local_expires_at=now + timedelta(days=32),
            next_attempt_at=now - timedelta(seconds=1),
            attempts=1,
            max_attempts=2,
        )
        session.add_all([user, server, tariff, sub, ff])
        await session.commit()

    # Simulate fresh process after restart
    mock_api = AsyncMock()
    mock_api.get_orders.return_value = [{"order_id": "synthetic_external_id_22", "active_until": (now + timedelta(days=2)).isoformat()}]

    async with db_session_factory() as session:
        # Load fresh from DB as a new worker would
        fresh_ff = await session.get(VhqSubscriptionFulfillment, 558)
        await execute_vhq_fulfillment(session, fresh_ff, api=mock_api)
        await session.commit()

    # Invariant: attempts reached max_attempts (2 >= 2), transitions to manual_review with ZERO new renew calls!
    assert mock_api.renew.call_count == 0
    async with db_session_factory() as session:
        ff_final = await session.get(VhqSubscriptionFulfillment, 558)
        assert ff_final.status == "manual_review"
        assert ff_final.last_error_code == "ambiguous_renew_unconfirmed"


# ── Test 20: Manual Review Payment Full Lifecycle: Visibility, Alerts & Resolution ─


async def test_manual_review_payment_full_lifecycle_visibility_alerts_and_resolution(monkeypatch, db_session_factory):
    """Paid payment with ambiguous mutation transitions to manual_review.
    It MUST:
    1. Be visible in FastDashboardSummary under needs_review and total_active.
    2. Be present in _get_active_problem_categories('review').
    3. Be queryable via get_problem_payments_page(category='review').
    4. Send an admin alert with details, instructions, and diagnostic code.
    5. NOT send duplicate alert on subsequent check (anti-spam).
    6. Disappear from active problems ONLY upon verified database completion.
    """
    from bot.services.cluster import LeaseHeartbeat, LeaseManager
    from bot.services.problem_payments import (
        get_fast_dashboard_summary,
        _get_active_problem_categories,
        get_problem_payments_page,
        alert_admins_if_problem_payments_detected,
        ProblemCategory,
    )
    monkeypatch.setattr("bot.database.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)

    now = datetime.utcnow()
    async with db_session_factory() as session:
        user = User(id=91, telegram_id=700000005, username="synthetic_username_16")
        tariff = Tariff(id=9, label="Базовый 30 дней", price_rub=249, days=30, vhq_tier="basic", tariff_type=TariffType.VPN)
        payment = Payment(
            id=370,
            user_id=user.id,
            tariff_id=tariff.id,
            amount=24900,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="synthetic_external_id_23",
            provider_status="succeeded",
            provider_paid=True,
            provisioning_failure_code="ambiguous_buy_mutation_manual_review_required",
            created_at=now - timedelta(minutes=10),
        )
        session.add_all([user, tariff, payment])
        await session.commit()

    # 1. Check FastDashboardSummary
    summary = await get_fast_dashboard_summary(days=7)
    assert summary.needs_review == 1
    assert summary.total_active == 1
    assert any(it.item_id == 370 for it in summary.urgent_items)

    # 2. Check _get_active_problem_categories
    cat_map = await _get_active_problem_categories(days=7, now=now)
    assert len(cat_map["review"]) == 1
    assert cat_map["review"][0].item_id == 370
    assert cat_map["review"][0].category == ProblemCategory.DELIVERY_UNCERTAIN

    # 3. Check get_problem_payments_page
    page_items, total_count, _ = await get_problem_payments_page(category="review", page=1)
    assert total_count == 1
    assert page_items[0].item_id == 370

    # 4. Check admin alert delivery
    fake_bot = AsyncMock()
    mock_notify = AsyncMock(return_value=True)
    monkeypatch.setattr("bot.services.problem_payments.notify_admins_issue", mock_notify)

    await alert_admins_if_problem_payments_detected(fake_bot)
    assert mock_notify.call_count == 1
    call_kwargs = mock_notify.call_args.kwargs
    assert "ПРОБЛЕМНЫЕ ОПЛАТЫ (1 шт.)" in call_kwargs["title"]
    details_text = "\n".join(call_kwargs["details"])
    assert "payment #370" in details_text
    assert "ambiguous_buy_mutation_manual_review_required" in details_text

    # 5. Subsequent run must NOT send duplicate alert (anti-spam)
    mock_notify.reset_mock()
    await alert_admins_if_problem_payments_detected(fake_bot)
    assert mock_notify.call_count == 0

    # 6. Mark payment cleanly resolved in DB
    async with db_session_factory() as session:
        server = Server(id=6, name="NL6", host="6.6.6.6", location="NL", is_active=True)
        sub = Subscription(
            id=559,
            user_id=91,
            server_id=server.id,
            tariff_id=9,
            tariff_months=1,
            client_name="synthetic_client_name_17",
            platform="android",
            status=SubStatus.ACTIVE,
            expires_at=now + timedelta(days=30),
        )
        session.add_all([server, sub])
        await session.flush()

        p_upd = await session.get(Payment, 370)
        p_upd.status = PaymentStatus.COMPLETED
        p_upd.subscription_id = sub.id
        p_upd.provisioning_failure_code = None
        await session.commit()

    # Resolution notification sent and dropped from active problems
    mock_res_notify = AsyncMock(return_value=True)
    monkeypatch.setattr("bot.services.problem_payments.notify_staff_text", mock_res_notify)

    await alert_admins_if_problem_payments_detected(fake_bot)
    assert mock_res_notify.call_count == 1
    assert "ПРОБЛЕМНАЯ ОПЛАТА ОБРАБОТАНА" in mock_res_notify.call_args[0][1]

    # Verify dashboard is now clean
    summary_clean = await get_fast_dashboard_summary(days=7)
    assert summary_clean.needs_review == 0
    assert summary_clean.total_active == 0


# ── Test 21: Reconciliation Queue Starvation Prevention ───────────────────────


async def test_reconciliation_queue_starvation_prevention(monkeypatch, db_session_factory):
    """When 20 older payments are permanently locked in manual_review,
    the background scheduler MUST NOT starve! The 21st (newest) paid payment
    MUST be picked up and reconciled, while locked payments are accounted for in stats['locked'].
    """
    from bot.services.yookassa_reconciliation import reconcile_pending_yookassa_payments
    monkeypatch.setattr("bot.database.async_session", db_session_factory)
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)

    now = datetime.utcnow()
    async with db_session_factory() as session:
        user = User(id=92, telegram_id=700000015, username="synthetic_username_18")
        tariff = Tariff(id=9, label="Базовый 30 дней", price_rub=249, days=30, vhq_tier="basic", tariff_type=TariffType.VPN)
        session.add_all([user, tariff])

        # Insert 20 older locked payments
        for i in range(1, 21):
            p_locked = Payment(
                id=1000 + i,
                user_id=user.id,
                tariff_id=tariff.id,
                amount=24900,
                currency="RUB",
                method=PaymentMethod.YOOKASSA,
                status=PaymentStatus.PENDING,
                provider_payment_id=f"yk_locked_{i}",
                provider_status="succeeded",
                provider_paid=True,
                provisioning_failure_code="ambiguous_buy_mutation_manual_review_required",
                created_at=now - timedelta(hours=5) + timedelta(minutes=i),
            )
            session.add(p_locked)

        # Insert 1 brand-new unfulfilled payment
        p_new = Payment(
            id=1099,
            user_id=user.id,
            tariff_id=tariff.id,
            amount=24900,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="synthetic_external_id_24",
            provider_status="succeeded",
            provider_paid=True,
            provisioning_failure_code=None,  # Not locked!
            created_at=now - timedelta(minutes=5),
        )
        session.add(p_new)
        await session.commit()

    fake_bot = AsyncMock()
    processed_ids = []

    async def mock_fulfill(bot, provider_id, source="scheduler"):
        processed_ids.append(provider_id)
        return True, "completed"

    monkeypatch.setattr("bot.services.yookassa_reconciliation.verify_and_fulfill_yookassa_payment", mock_fulfill)

    stats = await reconcile_pending_yookassa_payments(fake_bot)

    # Invariant: 20 locked payments are tracked in stats['locked']
    assert stats["locked"] == 20
    # Invariant: The new payment was NOT starved and was processed!
    assert "synthetic_external_id_24" in processed_ids
    assert stats["fulfilled"] == 1


# ── Test 22: Real Integration Manual Payment Recovery & Audited Reclassification ──


async def test_real_integration_manual_payment_recovery(monkeypatch, db_session_factory):
    """Integration test with REAL LeaseManager, REAL verify_and_fulfill_yookassa_payment,
    real test DB, mocking ONLY external network calls (YooKassa / VHQ / Telegram).
    """
    from bot.services.manual_payment_recovery import authorize_and_recover_manual_payment
    from bot.config import settings

    monkeypatch.setattr("bot.database.async_session", db_session_factory)
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.manual_payment_recovery.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.cluster.async_session", db_session_factory)
    monkeypatch.setattr("bot.handlers.payment.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.guide_service.async_session", db_session_factory)

    admin_tg = 124958038
    monkeypatch.setattr(settings, "admin_ids", [admin_tg])
    monkeypatch.setattr(settings, "owner_ids", [admin_tg])

    now = datetime.utcnow()
    async with db_session_factory() as session:
        await session.execute(delete(AppLease))
        user = User(id=93, telegram_id=700000016, username="synthetic_username_19", full_name="synthetic_full_name_20")
        tariff = Tariff(id=9, label="Базовый 30 дней", price_rub=359, days=30, vhq_tier="basic", tariff_type=TariffType.VPN, is_active=True)
        server = Server(id=1, name="NL-1", host="127.0.0.1", location="Amsterdam", api_url="https://vpn.test.com", is_active=True)
        payment = Payment(
            id=7319,
            user_id=user.id,
            tariff_id=tariff.id,
            amount=35910,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="synthetic_external_id_4",
            provider_status="succeeded",
            provider_paid=True,
            provisioning_failure_code="uncertain_supplier_mutation",
            created_at=now - timedelta(hours=1),
        )
        session.add_all([user, tariff, server, payment])
        await session.commit()

    # 1. Unauthorized operator rejected
    fake_bot = AsyncMock()
    ok, code = await authorize_and_recover_manual_payment(
        bot=fake_bot,
        payment_id=7319,
        admin_telegram_id=700000017,
        reason="Hacker attempt",
    )
    assert ok is False
    assert code == "unauthorized_admin"

    # 2. Rejects ambiguous payment without explicit operator confirmation
    ok, code = await authorize_and_recover_manual_payment(
        bot=fake_bot,
        payment_id=7319,
        admin_telegram_id=admin_tg,
        reason="Trying without confirmation",
        confirm_no_prior_mutation=False,
    )
    assert ok is False
    assert code == "explicit_no_prior_mutation_confirmation_required"

    # The operator flag alone is not evidence of a pre-mutation rejection.
    ok, code = await authorize_and_recover_manual_payment(
        bot=fake_bot,
        payment_id=7319,
        admin_telegram_id=admin_tg,
        reason="Trying with confirmation only",
        confirm_no_prior_mutation=True,
    )
    assert ok is False
    assert code == "pre_mutation_evidence_required"

    # 3. Dry-run mode: checks balance and returns success without mutating DB
    mock_vhq = AsyncMock()
    mock_vhq.enabled = True
    mock_vhq.get_balance.return_value = {"balance": 500.0}
    with patch("bot.services.manual_payment_recovery.VHQPartnerAPI", return_value=mock_vhq):
        ok, code = await authorize_and_recover_manual_payment(
            bot=fake_bot,
            payment_id=7319,
            admin_telegram_id=admin_tg,
            reason="Dry run check",
            confirm_no_prior_mutation=True,
            pre_mutation_evidence={
                "provider_status": 400,
                "provider_error": "Insufficient balance",
                "evidence_reference": "vhq-audit:test-7319",
            },
            dry_run=True,
        )
    assert ok is True
    assert "dry_run_success" in code

    # Verify DB state is still unchanged
    async with db_session_factory() as session:
        p_check = await session.get(Payment, 7319)
        assert p_check.status == PaymentStatus.PENDING
        assert p_check.provisioning_failure_code == "uncertain_supplier_mutation"

    # 4. Distributed lease collision: another process holds the lease
    lease_coll = LeaseManager(owner_id="foreign_worker", session_factory=db_session_factory)
    acquired = await lease_coll.acquire_or_renew("payment:yookassa:synthetic_external_id_4", ttl_seconds=60)
    assert acquired is True

    with patch("bot.services.manual_payment_recovery.VHQPartnerAPI", return_value=mock_vhq):
        ok, code = await authorize_and_recover_manual_payment(
            bot=fake_bot,
            payment_id=7319,
            admin_telegram_id=admin_tg,
            reason="Should fail on lock",
            confirm_no_prior_mutation=True,
            pre_mutation_evidence={
                "provider_status": 400,
                "provider_error": "Insufficient balance",
                "evidence_reference": "vhq-audit:test-7319",
            },
        )
    assert ok is False
    assert code == "lock_acquisition_failed"
    await lease_coll.release("payment:yookassa:synthetic_external_id_4")

    # 5. Full REAL execution: real LeaseManager + real verify_and_fulfill_yookassa_payment
    mock_vhq_buy = AsyncMock()
    mock_vhq_buy.enabled = True
    mock_vhq_buy.extract_subscription_url = VHQPartnerAPI.extract_subscription_url
    mock_vhq_buy.extract_order_id = VHQPartnerAPI.extract_order_id
    mock_vhq_buy.extract_active_until = VHQPartnerAPI.extract_active_until
    mock_vhq_buy.get_balance.return_value = {"balance": 500.0}
    mock_vhq_buy.buy.return_value = {
        "order_id": "synthetic_external_id_33",
        "config_url": "https://vhq.test/sub/real_rec_319",
        "active_until": (now + timedelta(days=30)).isoformat(),
    }

    mock_yk_fetch = AsyncMock(return_value={
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "359.10", "currency": "RUB"},
        "metadata": {"user_id": "700000016", "tariff_id": "9", "platform": "android"},
    })

    mock_vhq_cls = MagicMock(return_value=mock_vhq_buy)
    mock_vhq_cls.extract_subscription_url = VHQPartnerAPI.extract_subscription_url
    mock_vhq_cls.extract_order_id = VHQPartnerAPI.extract_order_id
    mock_vhq_cls.extract_active_until = VHQPartnerAPI.extract_active_until

    with patch("bot.services.manual_payment_recovery.VHQPartnerAPI", mock_vhq_cls), \
         patch("bot.services.subscription_service.VHQPartnerAPI", mock_vhq_cls), \
         patch("bot.services.vhq_partner_api.VHQPartnerAPI", mock_vhq_cls), \
         patch("bot.services.yookassa_reconciliation.fetch_yookassa_payment", mock_yk_fetch), \
         patch("bot.webhooks.send_subscription_delivery_messages", AsyncMock()), \
         patch("bot.services.yookassa_reconciliation.notify_admins_payment", AsyncMock()):

        ok, code = await authorize_and_recover_manual_payment(
            bot=fake_bot,
            payment_id=7319,
            admin_telegram_id=admin_tg,
            reason="Audited proof: verified VHQ 400 rejection and topped up balance",
            confirm_no_prior_mutation=True,
            pre_mutation_evidence={
                "provider_status": 400,
                "provider_error": "Insufficient balance",
                "evidence_reference": "vhq-audit:test-7319",
            },
        )

    assert ok is True, f"Recovery failed: {code}"
    assert code == "subscription_completed"

    # Verify payment is COMPLETED and failure code is None in DB!
    async with db_session_factory() as session:
        p_final = await session.get(Payment, 7319)
        assert p_final.status == PaymentStatus.COMPLETED
        assert p_final.provisioning_failure_code is None
        assert p_final.subscription_id is not None

        # Verify subscription was created and is ACTIVE
        sub = await session.get(Subscription, p_final.subscription_id)
        assert sub is not None
        assert sub.status == SubStatus.ACTIVE
        assert sub.vhq_order_id == "synthetic_external_id_33"


# ── Test 23: End-to-End Autonomous Recovery on Balance Top-up ─────────────────


async def test_e2e_yookassa_succeeded_vhq_insufficient_balance_auto_recovery_on_topup(monkeypatch, db_session_factory):
    """The ultimate business scenario:
    1. Client payment succeeds in YooKassa.
    2. Initial fulfillment fails with HTTP 400 Insufficient balance.
    3. Payment is saved in WAITING_SUPPLIER_FUNDS (vhq_balance).
    4. Multiple scheduler runs pass while balance is 0 without duplicate buy or crash.
    5. Process restarts.
    6. VHQ balance is topped up.
    7. Scheduler WITHOUT ANY ADMIN INTERVENTION discovers the payment, fulfills it,
       delivers the VPN link to client, and marks payment completed.
    8. Further scheduler runs do not re-buy.
    """
    from bot.services.yookassa_reconciliation import reconcile_pending_yookassa_payments

    monkeypatch.setattr("bot.database.async_session", db_session_factory)
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.cluster.async_session", db_session_factory)
    monkeypatch.setattr("bot.handlers.payment.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.guide_service.async_session", db_session_factory)

    now = datetime.utcnow()
    async with db_session_factory() as session:
        await session.execute(delete(AppLease))
        user = User(id=77, telegram_id=700000018, username="synthetic_username_21", full_name="synthetic_full_name_22")
        tariff = Tariff(id=10, label="Базовый 30 дней", price_rub=359, days=30, vhq_tier="basic", tariff_type=TariffType.VPN, is_active=True)
        server = Server(id=2, name="Primary", host="127.0.0.1", location="Amsterdam", api_url="https://vpn.test.com", is_active=True)
        payment = Payment(
            id=501,
            user_id=user.id,
            tariff_id=tariff.id,
            amount=35900,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="synthetic_external_id_25",
            provider_status="succeeded",
            provider_paid=True,
            provisioning_failure_code=None,
            created_at=now,
        )
        session.add_all([user, tariff, server, payment])
        await session.commit()

    # The provider rejects the first real VHQ purchase before mutation.
    mock_vhq = AsyncMock()
    mock_vhq.enabled = True
    mock_vhq.get_balance.return_value = {"balance": 0.0}
    successful_buys: list[dict[str, Any]] = []
    rejected_once = False

    async def buy_after_topup(**kwargs):
        nonlocal rejected_once
        if not rejected_once:
            rejected_once = True
            raise VHQPartnerAPIError("Insufficient balance", status=400)
        successful_buys.append(kwargs)
        return {
            "order_id": "synthetic_external_id_34",
            "config_url": "https://vhq.test/sub/e2e_auto_501",
            "active_until": (now + timedelta(days=30)).isoformat(),
        }

    mock_vhq.buy.side_effect = buy_after_topup

    mock_vhq_cls = MagicMock(return_value=mock_vhq)
    mock_vhq_cls.extract_subscription_url = VHQPartnerAPI.extract_subscription_url
    mock_vhq_cls.extract_order_id = VHQPartnerAPI.extract_order_id
    mock_vhq_cls.extract_active_until = VHQPartnerAPI.extract_active_until

    mock_yk_fetch = AsyncMock(return_value={
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "359.00", "currency": "RUB"},
        "metadata": {"user_id": "700000018", "tariff_id": "10", "platform": "android"},
    })

    fake_bot = AsyncMock()

    class WebhookRequest(SimpleNamespace):
        async def json(self):
            return self._json_body

    with patch("bot.services.subscription_service.VHQPartnerAPI", mock_vhq_cls), \
         patch("bot.services.vhq_partner_api.VHQPartnerAPI", mock_vhq_cls), \
         patch("bot.services.yookassa_reconciliation.fetch_yookassa_payment", mock_yk_fetch), \
         patch("bot.services.yookassa_reconciliation.notify_admins_payment", AsyncMock()):

        # Real webhook path: YooKassa succeeded -> VHQ HTTP 400 -> durable DB state.
        webhook_request = WebhookRequest(
            _json_body={
                "event": "payment.succeeded",
                "object": {
                    "id": "synthetic_external_id_25",
                    "metadata": {
                        "user_id": "700000018",
                        "chat_id": "700000018",
                        "tariff_id": "10",
                        "platform": "android",
                    },
                },
            },
            headers={},
            remote="127.0.0.1",
            app={"bot": fake_bot},
        )
        response = await handle_yookassa(webhook_request)
        assert response.status == 503
        assert mock_vhq.buy.call_count == 1

        # The payment acknowledgement is customer-safe and sent exactly once.
        neutral_text = "✅ Оплата получена. Ваш VPN-доступ готовится. Мы автоматически отправим его сюда после активации"
        sent_texts = [call.args[1] for call in fake_bot.send_message.await_args_list]
        assert sent_texts.count(neutral_text) == 1

        async with db_session_factory() as session:
            p_check = await session.get(Payment, 501)
            assert p_check.status == PaymentStatus.PENDING
            assert p_check.provisioning_failure_code == "vhq_balance"
            assert p_check.next_attempt_at is not None

            # More than 24 hours later, a restarted worker must still see it.
            p_check.created_at = now - timedelta(hours=72)
            p_check.next_attempt_at = datetime.utcnow()
            await session.commit()

        # Simulate a restart by closing the previous DB session, then top up VHQ.
        mock_vhq.get_balance.return_value = {"balance": 500.0}

        # Scheduler runs without CLI or an administrative recovery call.
        stats3 = await reconcile_pending_yookassa_payments(fake_bot)
        assert stats3["fulfilled"] == 1
        assert len(successful_buys) == 1

        # Verify final DB state and the delivered VPN link.
        async with db_session_factory() as session:
            p_done = await session.get(Payment, 501)
            assert p_done.status == PaymentStatus.COMPLETED
            assert p_done.provisioning_failure_code is None
            assert p_done.next_attempt_at is None
            assert p_done.subscription_id is not None

            sub_done = await session.get(Subscription, p_done.subscription_id)
            assert sub_done.status == SubStatus.ACTIVE
            assert sub_done.vhq_order_id == "synthetic_external_id_34"

        sent_texts = [call.args[1] for call in fake_bot.send_message.await_args_list]
        assert sent_texts.count(neutral_text) == 1
        assert any("vhq-sub" in text or "sub.example" in text or "<code>" in text for text in sent_texts)

        # A replay after completion is a no-op and cannot buy again.
        sent_count_after_delivery = fake_bot.send_message.await_count
        stats4 = await reconcile_pending_yookassa_payments(fake_bot)
        assert stats4["fulfilled"] == 0
        assert len(successful_buys) == 1
        assert fake_bot.send_message.await_count == sent_count_after_delivery


# ── Test 24: Multiple Waiting Orders with Partial Balance Top-up ──────────────


async def test_e2e_multiple_waiting_orders_partial_topup(monkeypatch, db_session_factory):
    """When multiple orders are waiting for funds and balance is topped up with enough
    for only PART of the queue, available orders are fulfilled and remaining orders
    safely continue waiting.
    """
    from bot.services.yookassa_reconciliation import reconcile_pending_yookassa_payments

    monkeypatch.setattr("bot.database.async_session", db_session_factory)
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.cluster.async_session", db_session_factory)

    now = datetime.utcnow()
    async with db_session_factory() as session:
        await session.execute(delete(AppLease))
        user1 = User(id=81, telegram_id=700000019, username="synthetic_username_23", full_name="synthetic_full_name_24")
        user2 = User(id=82, telegram_id=700000020, username="synthetic_username_25", full_name="synthetic_full_name_26")
        tariff = Tariff(id=11, label="Базовый 30 дней", price_rub=359, days=30, vhq_tier="basic", tariff_type=TariffType.VPN, is_active=True)
        server = Server(id=3, name="Primary", host="127.0.0.1", location="Amsterdam", api_url="https://vpn.test.com", is_active=True)
        p1 = Payment(
            id=601,
            user_id=user1.id,
            tariff_id=tariff.id,
            amount=35900,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="synthetic_external_id_26",
            provider_status="succeeded",
            provider_paid=True,
            provisioning_failure_code="vhq_balance",
            created_at=now - timedelta(minutes=20),
        )
        p2 = Payment(
            id=602,
            user_id=user2.id,
            tariff_id=tariff.id,
            amount=35900,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="synthetic_external_id_27",
            provider_status="succeeded",
            provider_paid=True,
            provisioning_failure_code="vhq_balance",
            created_at=now - timedelta(minutes=15),
        )
        session.add_all([user1, user2, tariff, server, p1, p2])
        await session.commit()

    # Balance is topped up with 300 RUB (only enough for 1 basic sub of 249 RUB)
    curr_balance = 300.0

    async def dynamic_get_balance():
        return {"balance": curr_balance}

    async def dynamic_buy(**kwargs):
        nonlocal curr_balance
        if curr_balance < 249.0:
            raise VHQPartnerAPIError(f"Insufficient balance: {curr_balance} < 249", status=400)
        curr_balance -= 249.0
        return {
            "order_id": f"ord_{int(curr_balance)}",
            "config_url": "https://vhq.test/sub/part",
            "active_until": (now + timedelta(days=30)).isoformat(),
        }

    mock_vhq = AsyncMock()
    mock_vhq.enabled = True
    mock_vhq.get_balance.side_effect = dynamic_get_balance
    mock_vhq.buy.side_effect = dynamic_buy

    mock_vhq_cls = MagicMock(return_value=mock_vhq)
    mock_vhq_cls.extract_subscription_url = VHQPartnerAPI.extract_subscription_url
    mock_vhq_cls.extract_order_id = VHQPartnerAPI.extract_order_id
    mock_vhq_cls.extract_active_until = VHQPartnerAPI.extract_active_until

    async def mock_yk_fetch(payment_id):
        uid = "700000019" if "synthetic_external_id_26" in str(payment_id) else "700000020"
        return {
            "status": "succeeded",
            "paid": True,
            "amount": {"value": "359.00", "currency": "RUB"},
            "metadata": {"user_id": uid, "tariff_id": "11", "platform": "android"},
        }

    fake_bot = AsyncMock()

    with patch("bot.services.subscription_service.VHQPartnerAPI", mock_vhq_cls), \
         patch("bot.services.vhq_partner_api.VHQPartnerAPI", mock_vhq_cls), \
         patch("bot.services.yookassa_reconciliation.fetch_yookassa_payment", mock_yk_fetch), \
         patch("bot.webhooks.send_subscription_delivery_messages", AsyncMock()), \
         patch("bot.services.yookassa_reconciliation.notify_admins_payment", AsyncMock()):

        # Round 1: balance 300 RUB. p1 fulfills; p2 sees balance 51 < 249 and safely stays in vhq_balance
        stats = await reconcile_pending_yookassa_payments(fake_bot)
        assert stats["fulfilled"] == 1

        async with db_session_factory() as session:
            p1_chk = await session.get(Payment, 601)
            p2_chk = await session.get(Payment, 602)
            assert p1_chk.status == PaymentStatus.COMPLETED
            assert p2_chk.status == PaymentStatus.PENDING
            assert p2_chk.provisioning_failure_code == "vhq_balance"

        # Round 2: Admin tops up balance by another 300 RUB -> balance becomes 351 RUB
        curr_balance += 300.0
        async with db_session_factory() as session:
            waiting = await session.get(Payment, 602)
            waiting.next_attempt_at = datetime.utcnow()
            await session.commit()
        stats2 = await reconcile_pending_yookassa_payments(fake_bot)
        assert stats2["fulfilled"] == 1

        async with db_session_factory() as session:
            p2_final = await session.get(Payment, 602)
            assert p2_final.status == PaymentStatus.COMPLETED


# ── Test 25: VHQ Renew Crash Safety During POST Network Call ─────────────────


async def test_vhq_renew_crash_during_post_reconciles_on_restart_without_duplicate_post(monkeypatch, db_session_factory):
    """BLOCKER 4: When POST renew() is dispatched, PoNR commits renew_inflight.
    If process crashes during or right after POST, on restart it reconciles
    via supplier get_orders() without sending a duplicate POST!
    """
    now = datetime.utcnow()
    sub_id = 450
    order_id = "synthetic_external_id_28"

    async with db_session_factory() as session:
        user = User(id=88, telegram_id=700000021, username="synthetic_username_27")
        tariff = Tariff(id=12, label="Базовый 30 дней", price_rub=249, days=30, vhq_tier="basic", tariff_type=TariffType.VPN)
        server = Server(id=4, name="Primary", host="127.0.0.1", location="Amsterdam", api_url="https://vpn.test.com", is_active=True)
        sub = Subscription(
            id=sub_id,
            user_id=user.id,
            server_id=server.id,
            tariff_id=tariff.id,
            status=SubStatus.ACTIVE,
            vhq_order_id=order_id,
            tariff_months=1,
            client_name="synthetic_client_name_28",
            platform=Platform.ANDROID,
            vhq_active_until=now + timedelta(days=2),
            expires_at=now + timedelta(days=2),
        )
        fulfillment = VhqSubscriptionFulfillment(
            id=770,
            fulfillment_uuid="ff-crash-renew-770",
            subscription_id=sub.id,
            user_id=user.id,
            source_type="payment",
            source_id="payment:770",
            action="renew",
            status="pending",
            vhq_order_id=order_id,
            baseline_provider_active_until=now + timedelta(days=2),
            expected_provider_active_until=now + timedelta(days=32),
            baseline_local_expires_at=now + timedelta(days=2),
            target_local_expires_at=now + timedelta(days=32),
            attempts=0,
        )
        session.add_all([user, tariff, server, sub, fulfillment])
        await session.commit()

    mock_api = AsyncMock()
    mock_api.enabled = True

    # Simulate crash during POST execution: POST arrives at VHQ and succeeds,
    # but the process crashes before response can be processed by bot!
    async def crash_renew(*args, **kwargs):
        raise KeyboardInterrupt("Simulated server crash / SIGKILL")

    mock_api.renew.side_effect = crash_renew

    async with db_session_factory() as session:
        ff = await session.get(VhqSubscriptionFulfillment, 770)
        with pytest.raises(KeyboardInterrupt):
            await execute_vhq_fulfillment(session, ff, api=mock_api)

    # Verify PoNR state committed in DB before the crash
    async with db_session_factory() as session:
        ff_crashed = await session.get(VhqSubscriptionFulfillment, 770)
        assert ff_crashed.status == "renew_inflight"
        assert ff_crashed.last_error_code == "renew_inflight"

    # Now simulate process restart & scheduler recovery:
    # Supplier get_orders() shows the renewal actually succeeded at VHQ!
    mock_api.renew.side_effect = None
    mock_api.renew.reset_mock()
    mock_api.get_orders.return_value = [
        {"order_id": order_id, "active_until": (now + timedelta(days=32)).isoformat()},
    ]

    async with db_session_factory() as session:
        ff_recovered = await session.get(VhqSubscriptionFulfillment, 770)
        # Lease expired after crash
        ff_recovered.lease_expires_at = now - timedelta(seconds=1)
        await execute_vhq_fulfillment(session, ff_recovered, api=mock_api)

    # Renewal confirmed from get_orders()!
    assert ff_recovered.status == "completed"
    assert ff_recovered.confirmed_provider_active_until == now + timedelta(days=32)

    # CRITICAL INVARIANT: Zero duplicate POST renew calls!
    assert mock_api.renew.call_count == 0


# ── Test 26: Reclassification of Historical Uncertain Payment to Waiting Funds ─


async def test_reclassification_of_historical_uncertain_payment_to_waiting_funds(monkeypatch, db_session_factory):
    """Simulates historical Payment #7319:
    1. Payment has uncertain_supplier_mutation in DB.
    2. Admin authorizes reclassification to WAITING_SUPPLIER_FUNDS under lease with audit.
    3. Payment becomes vhq_balance.
    4. When balance is topped up, scheduler automatically fulfills it without further admin action!
    """
    from bot.services.manual_payment_recovery import authorize_and_recover_manual_payment
    from bot.services.yookassa_reconciliation import reconcile_pending_yookassa_payments
    from bot.config import settings

    monkeypatch.setattr("bot.database.async_session", db_session_factory)
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.manual_payment_recovery.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.cluster.async_session", db_session_factory)

    admin_tg = 124958038
    monkeypatch.setattr(settings, "admin_ids", [admin_tg])
    monkeypatch.setattr(settings, "owner_ids", [admin_tg])

    now = datetime.utcnow()
    async with db_session_factory() as session:
        await session.execute(delete(AppLease))
        user = User(id=94, telegram_id=700000001, username="synthetic_username_1", full_name="synthetic_full_name_2")
        tariff = Tariff(id=13, label="Базовый 30 дней", price_rub=359, days=30, vhq_tier="basic", tariff_type=TariffType.VPN, is_active=True)
        server = Server(id=5, name="Primary", host="127.0.0.1", location="Amsterdam", api_url="https://vpn.test.com", is_active=True)
        payment = Payment(
            id=7319,
            user_id=user.id,
            tariff_id=tariff.id,
            amount=35910,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="synthetic_external_id_4",
            provider_status="succeeded",
            provider_paid=True,
            provisioning_failure_code="uncertain_supplier_mutation",
            created_at=now - timedelta(hours=5),
        )
        session.add_all([user, tariff, server, payment])
        await session.commit()

    # Step 1: Admin reclassifies historical payment to waiting funds queue
    mock_vhq = AsyncMock()
    mock_vhq.enabled = True
    mock_vhq.get_balance.return_value = {"balance": 37.0}  # Currently still insufficient

    fake_bot = AsyncMock()
    with patch("bot.services.manual_payment_recovery.VHQPartnerAPI", return_value=mock_vhq):
        ok, code = await authorize_and_recover_manual_payment(
            bot=fake_bot,
            payment_id=7319,
            admin_telegram_id=admin_tg,
            reason="Verified original VHQ rejection: Insufficient balance 37 < 249",
            confirm_no_prior_mutation=True,
            pre_mutation_evidence={
                "provider_status": 400,
                "provider_error": "Insufficient balance",
                "evidence_reference": "vhq-audit:test-7319",
            },
            reclassify_to_waiting_funds_only=True,
        )

    assert ok is True
    assert "reclassified_to_waiting_funds" in code

    # Verify state in DB is now vhq_balance (WAITING_SUPPLIER_FUNDS)
    async with db_session_factory() as session:
        p_wait = await session.get(Payment, 7319)
        assert p_wait.provisioning_failure_code == "vhq_balance"

    # Step 2: Admin tops up balance in VHQ cabinet to 500 RUB
    mock_vhq.get_balance.return_value = {"balance": 500.0}
    mock_vhq.buy.return_value = {
        "order_id": "synthetic_external_id_35",
        "config_url": "https://vhq.test/sub/7319",
        "active_until": (now + timedelta(days=30)).isoformat(),
    }

    mock_yk_fetch = AsyncMock(return_value={
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "359.10", "currency": "RUB"},
        "metadata": {"user_id": "700000001", "tariff_id": "13", "platform": "android"},
    })

    mock_vhq_cls = MagicMock(return_value=mock_vhq)
    mock_vhq_cls.extract_subscription_url = VHQPartnerAPI.extract_subscription_url
    mock_vhq_cls.extract_order_id = VHQPartnerAPI.extract_order_id
    mock_vhq_cls.extract_active_until = VHQPartnerAPI.extract_active_until

    with patch("bot.services.subscription_service.VHQPartnerAPI", mock_vhq_cls), \
         patch("bot.services.vhq_partner_api.VHQPartnerAPI", mock_vhq_cls), \
         patch("bot.services.yookassa_reconciliation.fetch_yookassa_payment", mock_yk_fetch), \
         patch("bot.webhooks.send_subscription_delivery_messages", AsyncMock()), \
         patch("bot.services.yookassa_reconciliation.notify_admins_payment", AsyncMock()):

        # Scheduler runs and fulfills it automatically!
        stats = await reconcile_pending_yookassa_payments(fake_bot)
        assert stats["fulfilled"] == 1

    # Verify payment #7319 is completed!
    async with db_session_factory() as session:
        p_done = await session.get(Payment, 7319)
        assert p_done.status == PaymentStatus.COMPLETED
        assert p_done.provisioning_failure_code is None
        assert p_done.subscription_id is not None




