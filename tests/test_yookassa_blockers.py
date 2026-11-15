from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.config import settings
from bot.models import (
    AppLease,
    AdaptSubscription,
    Base,
    Payment,
    PaymentMethod,
    PaymentStatus,
    Platform,
    RecurringPaymentProfile,
    Server,
    SubStatus,
    Subscription,
    Tariff,
    TariffType,
    User,
    VhqSubscriptionFulfillment,
)
from bot.services.yookassa_reconciliation import (
    reconcile_pending_yookassa_payments,
    verify_and_fulfill_yookassa_payment,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def blocker_db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_active_vhq_renewal_per_sub "
                "ON vhq_subscription_fulfillments (subscription_id) "
                "WHERE status IN ('pending', 'processing', 'retry_wait', 'uncertain', 'buy_inflight')"
            )
        )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("bot.services.scheduler.async_session", factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", factory)
    monkeypatch.setattr("bot.services.cluster.async_session", factory)
    monkeypatch.setattr("bot.webhooks.async_session", factory)
    monkeypatch.setattr("bot.services.problem_payments.async_session", factory)
    yield factory
    await engine.dispose()


def _server(server_id: int) -> Server:
    return Server(
        id=server_id,
        name=f"blocker-server-{server_id}",
        host="vpn.test",
        location="test",
        api_url="https://vpn.test",
        is_active=True,
    )


async def _seed_recurring_cycle(factory, *, profile_id: int, tariff_id: int, sub_id: int, tg_id: int):
    now = datetime.utcnow()
    async with factory() as session:
        user = User(id=profile_id, telegram_id=tg_id, username=f"u{profile_id}")
        tariff = Tariff(
            id=tariff_id,
            label="Baza Pro",
            price_rub=399,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            vhq_tier="basic",
        )
        sub = Subscription(
            id=sub_id,
            user_id=profile_id,
            tariff_id=tariff_id,
            server_id=profile_id,
            tariff_months=1,
            tariff_days=30,
            status=SubStatus.EXPIRED,
            platform=Platform.ANDROID,
            client_name=f"vhq_ord_{sub_id}",
            vpn_key=f"https://sub.vhq.test/{sub_id}",
            vhq_order_id=f"ord_{sub_id}",
            expires_at=now - timedelta(days=1),
            vhq_active_until=now - timedelta(days=1),
        )
        profile = RecurringPaymentProfile(
            id=profile_id,
            user_id=profile_id,
            subscription_id=sub_id,
            tariff_id=tariff_id,
            provider="yookassa",
            provider_payment_method_id=f"pm_{profile_id}",
            is_active=True,
            consent_granted=True,
            next_charge_at=sub.expires_at,
        )
        session.add_all([user, tariff, _server(profile_id), sub, profile])
        await session.commit()
    return now


async def _seed_adapt_recurring_cycle(factory, *, profile_id: int, tariff_id: int, sub_id: int, tg_id: int):
    now = datetime.utcnow()
    baseline = now - timedelta(days=1)
    adapt_uuid = f"adapt-recurring-{sub_id}"
    plan_uuid = "adapt-paid-plan-30d"
    async with factory() as session:
        user = User(id=profile_id, telegram_id=tg_id, username=f"adapt_u{profile_id}")
        tariff = Tariff(
            id=tariff_id,
            label="Adapt Pro",
            price_rub=399,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            adapt_plan_uuid=plan_uuid,
        )
        sub = Subscription(
            id=sub_id,
            user_id=profile_id,
            tariff_id=tariff_id,
            server_id=profile_id,
            tariff_months=1,
            tariff_days=30,
            status=SubStatus.EXPIRED,
            platform=Platform.ANDROID,
            client_name=f"adapt_{adapt_uuid}",
            vpn_key=f"https://sub.adapt.test/{adapt_uuid}",
            expires_at=baseline,
        )
        adapt = AdaptSubscription(
            subscription_id=sub_id,
            adapt_uuid=adapt_uuid,
            adapt_plan_uuid=plan_uuid,
            end_date=baseline,
        )
        profile = RecurringPaymentProfile(
            id=profile_id,
            user_id=profile_id,
            subscription_id=sub_id,
            tariff_id=tariff_id,
            provider="yookassa",
            provider_payment_method_id=f"pm_{profile_id}",
            is_active=True,
            consent_granted=True,
            next_charge_at=baseline,
        )
        session.add_all([user, tariff, _server(profile_id), sub, adapt, profile])
        await session.commit()
    return now, baseline, adapt_uuid, plan_uuid


async def test_recurring_provider_success_crash_boundary_resumes_same_paid_operation(blocker_db, monkeypatch):
    now = await _seed_recurring_cycle(blocker_db, profile_id=1, tariff_id=1001, sub_id=1001, tg_id=71001)
    charge = AsyncMock(return_value=("succeeded", "yk_crash_boundary", "succeeded"))
    first_attempt = True
    entitlement_calls = []

    async def _access(session, *, user, tariff, platform, **kwargs):
        nonlocal first_attempt
        entitlement_calls.append(kwargs.get("provisioning_payment").id)
        if first_attempt:
            first_attempt = False
            raise RuntimeError("crash after provider truth commit")
        sub = await session.get(Subscription, 1001)
        sub.status = SubStatus.ACTIVE
        sub.expires_at = now + timedelta(days=30)
        sub.vhq_active_until = now + timedelta(days=30)
        return sub, sub.vpn_key

    async def _provider_get(_provider_payment_id):
        async with blocker_db() as session:
            profile = await session.get(RecurringPaymentProfile, 1)
            operation_key = profile.recurring_operation_key
        return {
            "id": "yk_crash_boundary",
            "status": "succeeded",
            "paid": True,
            "amount": {"value": "399.00", "currency": "RUB"},
            "metadata": {
                "user_id": "71001",
                "tariff_id": "1001",
                "platform": "deferred~r~1001",
                "recurring": "true",
                "recurring_profile_id": "1",
                "sub_id": "1001",
                "operation_key": operation_key,
            },
        }

    with (
        patch.object(settings, "recurring_payments_enabled", True),
        patch("bot.services.payment_service.create_recurring_yookassa_payment", new=charge),
        patch("bot.services.yookassa_reconciliation.fetch_yookassa_payment", new=_provider_get),
        patch("bot.webhooks.create_or_extend_paid_access", new=_access),
    ):
        from bot.services.scheduler import process_recurring_charges

        await process_recurring_charges(SimpleNamespace(send_message=AsyncMock()))

    async with blocker_db() as session:
        payment = await session.scalar(select(Payment).where(Payment.provider_payment_id == "yk_crash_boundary"))
        profile = await session.get(RecurringPaymentProfile, 1)
        assert payment.status == PaymentStatus.PENDING
        assert payment.provider_status == "succeeded"
        assert payment.provider_paid is True
        assert payment.subscription_id is None
        first_key = profile.recurring_operation_key

    from bot.services.problem_payments import classify_candidate_db_only, ProblemCategory
    async with blocker_db() as session:
        unresolved_payment = await session.get(Payment, payment.id)
        unresolved_user = await session.get(User, unresolved_payment.user_id)
    unresolved_item = await classify_candidate_db_only(
        {"type": "payment", "obj": unresolved_payment, "user": unresolved_user}
    )
    assert unresolved_item is not None
    assert unresolved_item.category != ProblemCategory.PROCESSED

    with (
        patch.object(settings, "recurring_payments_enabled", True),
        patch("bot.services.payment_service.create_recurring_yookassa_payment", new=charge),
        patch("bot.services.yookassa_reconciliation.fetch_yookassa_payment", new=_provider_get),
        patch("bot.webhooks.create_or_extend_paid_access", new=_access),
    ):
        await process_recurring_charges(SimpleNamespace(send_message=AsyncMock()))

    async with blocker_db() as session:
        payment = await session.scalar(select(Payment).where(Payment.provider_payment_id == "yk_crash_boundary"))
        assert payment.status == PaymentStatus.COMPLETED
        assert payment.subscription_id == 1001
        assert payment.provisioning_failure_code is None
        profile = await session.get(RecurringPaymentProfile, 1)
        assert profile.recurring_operation_key is None
    assert charge.await_count == 2
    assert charge.await_args_list[0].kwargs["idempotence_key"] == first_key
    assert charge.await_args_list[1].kwargs["idempotence_key"] == first_key
    assert entitlement_calls == [1, 1]


async def test_recurring_pending_then_authenticated_success_reconciles_before_scheduler(blocker_db, monkeypatch):
    await _seed_recurring_cycle(blocker_db, profile_id=2, tariff_id=1002, sub_id=1002, tg_id=71002)
    charge = AsyncMock(return_value=("pending", "yk_pending_recurring", "pending"))
    bot = SimpleNamespace(send_message=AsyncMock())

    with (
        patch.object(settings, "recurring_payments_enabled", True),
        patch("bot.services.payment_service.create_recurring_yookassa_payment", new=charge),
    ):
        from bot.services.scheduler import process_recurring_charges

        await process_recurring_charges(bot)

    async with blocker_db() as session:
        payment = await session.scalar(select(Payment).where(Payment.provider_payment_id == "yk_pending_recurring"))
        profile = await session.get(RecurringPaymentProfile, 2)
        assert payment.status == PaymentStatus.PENDING
        assert profile.recurring_operation_key
        operation_key = profile.recurring_operation_key
        payment.created_at = datetime.utcnow() - timedelta(minutes=5)
        await session.commit()

    expected_expiry = datetime.utcnow() + timedelta(days=30, minutes=1)
    api = SimpleNamespace(
        renew=AsyncMock(
            return_value={
                "status": "scheduled",
                "active_until": expected_expiry.isoformat() + "Z",
                "order_id": "ord_1002",
            }
        ),
        get_orders=AsyncMock(return_value=[]),
    )
    delivery = AsyncMock(return_value=(True, None))
    issue_alert = AsyncMock()
    provider_payment = {
        "id": "yk_pending_recurring",
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "399.00", "currency": "RUB"},
        "metadata": {
            "user_id": "71002",
            "tariff_id": "1002",
            "platform": "deferred~r~1002",
            "recurring": "true",
            "recurring_profile_id": "2",
            "sub_id": "1002",
            "operation_key": operation_key,
        },
    }

    with (
        patch("bot.services.yookassa_reconciliation.fetch_yookassa_payment", new=AsyncMock(return_value=provider_payment)),
        patch("bot.services.subscription_service.VHQPartnerAPI", lambda: api),
        patch("bot.webhooks.send_subscription_delivery_messages", new=delivery),
        patch("bot.services.yookassa_reconciliation.notify_admins_issue", new=issue_alert),
    ):
        stats = await reconcile_pending_yookassa_payments(bot)

    assert stats["fulfilled"] == 1
    api.renew.assert_awaited_once()
    issue_alert.assert_not_awaited()
    delivery.assert_awaited_once()

    async with blocker_db() as session:
        payment = await session.scalar(select(Payment).where(Payment.provider_payment_id == "yk_pending_recurring"))
        sub = await session.get(Subscription, 1002)
        profile = await session.get(RecurringPaymentProfile, 2)
        assert payment.status == PaymentStatus.COMPLETED
        assert payment.subscription_id == 1002
        assert payment.provisioning_failure_code is None
        assert sub.expires_at > datetime.utcnow()
        assert profile.recurring_operation_key is None

    # The fifteen-minute recurring scheduler sees the completed entitlement and
    # must not create another operation or supplier renewal.
    with (
        patch.object(settings, "recurring_payments_enabled", True),
        patch("bot.services.payment_service.create_recurring_yookassa_payment", new=AsyncMock()),
    ):
        from bot.services.scheduler import process_recurring_charges

        await process_recurring_charges(bot)
    assert api.renew.await_count == 1


async def test_recurring_adapt_scheduler_and_reconciliation_share_payment_lease(blocker_db):
    now, baseline, adapt_uuid, plan_uuid = await _seed_adapt_recurring_cycle(
        blocker_db,
        profile_id=6,
        tariff_id=1006,
        sub_id=1006,
        tg_id=71006,
    )
    charge = AsyncMock(
        side_effect=[
            ("pending", "yk_adapt_race", "pending"),
            ("succeeded", "yk_adapt_race", "succeeded"),
        ]
    )
    bot = SimpleNamespace(send_message=AsyncMock())

    with (
        patch.object(settings, "recurring_payments_enabled", True),
        patch("bot.services.payment_service.create_recurring_yookassa_payment", new=charge),
    ):
        from bot.services.scheduler import process_recurring_charges

        await process_recurring_charges(bot)

    async with blocker_db() as session:
        payment = await session.scalar(select(Payment).where(Payment.provider_payment_id == "yk_adapt_race"))
        profile = await session.get(RecurringPaymentProfile, 6)
        payment.created_at = datetime.utcnow() - timedelta(minutes=5)
        profile.last_payment_attempt = datetime.utcnow() - timedelta(hours=3)
        operation_key = profile.recurring_operation_key
        await session.commit()

    renewed = False
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release_first = asyncio.Event()
    renew_calls = []
    expected_expiry = baseline + timedelta(days=30)

    async def _get_status(_self, subscription_uuid):
        return {
            "plan_uuid": plan_uuid,
            "end_date": (expected_expiry if renewed else baseline).isoformat() + "Z",
            "devices": 1,
            "is_frozen": False,
        }

    async def _renew(_self, subscription_uuid):
        nonlocal renewed
        renew_calls.append(subscription_uuid)
        if len(renew_calls) == 1:
            first_started.set()
            await release_first.wait()
            renewed = True
        else:
            second_started.set()
            renewed = True
        return {"status": "renewed", "end_date": expected_expiry.isoformat() + "Z"}

    provider_payment = {
        "id": "yk_adapt_race",
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "399.00", "currency": "RUB"},
        "metadata": {
            "user_id": "71006",
            "tariff_id": "1006",
            "platform": "deferred~r~1006",
            "recurring": "true",
            "recurring_profile_id": "6",
            "sub_id": "1006",
            "operation_key": operation_key,
        },
    }

    from bot.services.yookassa_reconciliation import reconcile_pending_yookassa_payments
    delivery = AsyncMock(return_value=(True, None))

    with (
        patch.object(settings, "recurring_payments_enabled", True),
        patch("bot.services.payment_service.create_recurring_yookassa_payment", new=charge),
        patch("bot.services.yookassa_reconciliation.fetch_yookassa_payment", new=AsyncMock(return_value=provider_payment)),
        patch("bot.services.subscription_service.AdaptAPI.get_status", new=_get_status),
        patch("bot.services.subscription_service.AdaptAPI.renew_subscription", new=_renew),
        patch("bot.webhooks.send_subscription_delivery_messages", new=delivery),
    ):
        scheduler_task = asyncio.create_task(process_recurring_charges(bot))
        await asyncio.wait_for(first_started.wait(), timeout=1)
        reconcile_task = asyncio.create_task(reconcile_pending_yookassa_payments(bot))
        try:
            await asyncio.wait_for(second_started.wait(), timeout=0.25)
        except asyncio.TimeoutError:
            pass
        release_first.set()
        await asyncio.gather(scheduler_task, reconcile_task)

    assert len(renew_calls) == 1
    assert second_started.is_set() is False
    assert delivery.await_count == 1

    async with blocker_db() as session:
        payment_rows = (await session.scalars(select(Payment).where(Payment.provider_payment_id == "yk_adapt_race"))).all()
        assert len(payment_rows) == 1
        payment = payment_rows[0]
        sub = await session.get(Subscription, 1006)
        profile = await session.get(RecurringPaymentProfile, 6)
        adapt_rows = (await session.scalars(select(AdaptSubscription).where(AdaptSubscription.subscription_id == 1006))).all()
        assert len(adapt_rows) == 1
        adapt = adapt_rows[0]
        assert payment.status == PaymentStatus.COMPLETED
        assert payment.subscription_id == 1006
        assert sub.vpn_key == f"https://sub.adapt.test/{adapt_uuid}"
        assert sub.expires_at >= expected_expiry
        assert adapt.end_date >= expected_expiry
        assert profile.recurring_operation_key is None
        assert profile.last_charge_at is not None

    with (
        patch.object(settings, "recurring_payments_enabled", True),
        patch("bot.services.payment_service.create_recurring_yookassa_payment", new=charge),
        patch("bot.services.yookassa_reconciliation.fetch_yookassa_payment", new=AsyncMock(return_value=provider_payment)),
        patch("bot.webhooks.send_subscription_delivery_messages", new=delivery),
    ):
        await reconcile_pending_yookassa_payments(bot)
        await process_recurring_charges(bot)

    assert len(renew_calls) == 1
    assert delivery.await_count == 1
    assert charge.await_count == 2
    assert all(call.kwargs["idempotence_key"] == operation_key for call in charge.await_args_list)
    recurring_success_messages = [
        call.args[1]
        for call in bot.send_message.await_args_list
        if len(call.args) > 1 and "Подписка продлена автоматически" in call.args[1]
    ]
    assert len(recurring_success_messages) == 1


async def test_reconciliation_keeps_old_provider_paid_pending_payment_in_scan(blocker_db, monkeypatch):
    now = await _seed_recurring_cycle(blocker_db, profile_id=4, tariff_id=1004, sub_id=1004, tg_id=71004)
    async with blocker_db() as session:
        payment = Payment(
            user_id=4,
            subscription_id=None,
            tariff_id=1004,
            amount=39900,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_old_paid_1004",
            provider_idempotence_key="bot-payment-old-paid-1004",
            provider_status="succeeded",
            provider_paid=True,
            provider_amount_kopecks=39900,
            provider_currency="RUB",
            provider_verified_at=now - timedelta(days=2),
            provisioning_failure_code="supplier_fulfillment_pending",
            created_at=now - timedelta(days=2),
            platform="deferred~r~1004",
        )
        session.add(payment)
        await session.commit()

    verify = AsyncMock(return_value=(True, "subscription_completed"))
    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.verify_and_fulfill_yookassa_payment",
        verify,
    )
    stats = await reconcile_pending_yookassa_payments(SimpleNamespace(send_message=AsyncMock()))

    assert stats["fulfilled"] == 1
    verify.assert_awaited_once()
    assert verify.await_args.args[1] == "yk_old_paid_1004"


async def test_recurring_payment_without_recurring_metadata_fails_closed(blocker_db, monkeypatch):
    await _seed_recurring_cycle(blocker_db, profile_id=5, tariff_id=1005, sub_id=1005, tg_id=71005)
    charge = AsyncMock(return_value=("pending", "yk_missing_recurring_meta", "pending"))
    with (
        patch.object(settings, "recurring_payments_enabled", True),
        patch("bot.services.payment_service.create_recurring_yookassa_payment", new=charge),
    ):
        from bot.services.scheduler import process_recurring_charges

        await process_recurring_charges(SimpleNamespace(send_message=AsyncMock()))

    async with blocker_db() as session:
        payment = await session.scalar(
            select(Payment).where(Payment.provider_payment_id == "yk_missing_recurring_meta")
        )
        payment.created_at = datetime.utcnow() - timedelta(minutes=5)
        await session.commit()

    provider_payment = {
        "id": "yk_missing_recurring_meta",
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "399.00", "currency": "RUB"},
        "metadata": {
            "user_id": "71005",
            "tariff_id": "1005",
            "platform": "deferred~r~1005",
        },
    }
    with patch(
        "bot.services.yookassa_reconciliation.fetch_yookassa_payment",
        new=AsyncMock(return_value=provider_payment),
    ):
        ok, code = await verify_and_fulfill_yookassa_payment(
            SimpleNamespace(send_message=AsyncMock()),
            "yk_missing_recurring_meta",
            source="synthetic",
        )

    assert ok is False
    assert code == "missing_recurring_metadata"
    async with blocker_db() as session:
        payment = await session.scalar(
            select(Payment).where(Payment.provider_payment_id == "yk_missing_recurring_meta")
        )
        assert payment.provider_paid is True
        assert payment.provisioning_failure_code == "yookassa_missing_recurring_metadata"


async def test_recurring_terminal_failure_gets_new_key_but_ambiguous_retry_reuses_it(blocker_db):
    now = await _seed_recurring_cycle(blocker_db, profile_id=3, tariff_id=1003, sub_id=1003, tg_id=71003)
    responses = [
        ("failed", "yk_failed_k1", "canceled"),
        ("provider_error", None, None),
        ("succeeded", "yk_succeeded_k2", "succeeded"),
    ]
    charge = AsyncMock(side_effect=responses)

    async def _access(session, *, user, tariff, platform, **kwargs):
        sub = await session.get(Subscription, 1003)
        sub.status = SubStatus.ACTIVE
        sub.expires_at = now + timedelta(days=30)
        return sub, sub.vpn_key

    async def _provider_get(provider_payment_id):
        async with blocker_db() as session:
            profile = await session.get(RecurringPaymentProfile, 3)
            operation_key = profile.recurring_operation_key
        return {
            "id": provider_payment_id,
            "status": "succeeded",
            "paid": True,
            "amount": {"value": "399.00", "currency": "RUB"},
            "metadata": {
                "user_id": "71003",
                "tariff_id": "1003",
                "platform": "deferred~r~1003",
                "recurring": "true",
                "recurring_profile_id": "3",
                "sub_id": "1003",
                "operation_key": operation_key,
            },
        }

    from bot.services.scheduler import process_recurring_charges

    with (
        patch.object(settings, "recurring_payments_enabled", True),
        patch("bot.services.payment_service.create_recurring_yookassa_payment", new=charge),
        patch("bot.webhooks.create_or_extend_paid_access", new=_access),
        patch("bot.services.yookassa_reconciliation.fetch_yookassa_payment", new=_provider_get),
    ):
        await process_recurring_charges(SimpleNamespace(send_message=AsyncMock()))

    async with blocker_db() as session:
        profile = await session.get(RecurringPaymentProfile, 3)
        first_key = profile.recurring_operation_key
        assert first_key is None
        profile.last_payment_attempt = datetime.utcnow() - timedelta(hours=3)
        await session.commit()

    with (
        patch.object(settings, "recurring_payments_enabled", True),
        patch("bot.services.payment_service.create_recurring_yookassa_payment", new=charge),
        patch("bot.webhooks.create_or_extend_paid_access", new=_access),
        patch("bot.services.yookassa_reconciliation.fetch_yookassa_payment", new=_provider_get),
    ):
        await process_recurring_charges(SimpleNamespace(send_message=AsyncMock()))

    async with blocker_db() as session:
        profile = await session.get(RecurringPaymentProfile, 3)
        second_key = profile.recurring_operation_key
        assert second_key
        profile.last_payment_attempt = datetime.utcnow() - timedelta(hours=3)
        await session.commit()

    with (
        patch.object(settings, "recurring_payments_enabled", True),
        patch("bot.services.payment_service.create_recurring_yookassa_payment", new=charge),
        patch("bot.webhooks.create_or_extend_paid_access", new=_access),
        patch("bot.services.yookassa_reconciliation.fetch_yookassa_payment", new=_provider_get),
    ):
        await process_recurring_charges(SimpleNamespace(send_message=AsyncMock()))

    assert charge.await_count == 3
    keys = [call.kwargs["idempotence_key"] for call in charge.await_args_list]
    assert keys[0] != keys[1]
    assert keys[1] == keys[2]

    async with blocker_db() as session:
        payments = (await session.scalars(select(Payment).where(Payment.user_id == 3))).all()
        assert len(payments) == 2
        successful = next(p for p in payments if p.provider_payment_id == "yk_succeeded_k2")
        assert successful.status == PaymentStatus.COMPLETED
        assert successful.subscription_id == 1003
