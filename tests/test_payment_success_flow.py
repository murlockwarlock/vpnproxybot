from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.handlers import payment as payment_handler
from bot.models import (
    Base,
    BalanceTopUp,
    BalanceTransaction,
    Payment,
    PaymentMethod,
    PaymentStatus,
    Platform,
    ProxyAccount,
    RecurringPaymentProfile,
    Server,
    SubStatus,
    Subscription,
    Tariff,
    TariffType,
    User,
)
from bot.services.provisioning_issues import AccessProvisionError


pytestmark = pytest.mark.asyncio


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


def _make_payment_message(
    *,
    user_id: int,
    payload: str,
    charge_id: str,
    total_amount: int = 1000,
    currency: str = "XTR",
):
    bot = SimpleNamespace(send_message=AsyncMock())
    from_user = SimpleNamespace(
        id=user_id,
        username="buyer",
        first_name="Buyer",
        full_name="Buyer Example",
    )
    successful_payment = SimpleNamespace(
        invoice_payload=payload,
        total_amount=total_amount,
        currency=currency,
        telegram_payment_charge_id=charge_id,
    )
    return SimpleNamespace(
        from_user=from_user,
        successful_payment=successful_payment,
        bot=bot,
        answer=AsyncMock(),
    )


async def test_successful_stars_payment_is_persisted_even_if_provisioning_fails(monkeypatch, db_session_factory):
    async with db_session_factory() as session:
        user = User(telegram_id=10001, username="buyer", full_name="Buyer")
        tariff = Tariff(
            days=30,
            label="Тест 1000⭐",
            price_rub=1000,
            price_stars=1000,
            tariff_type=TariffType.VPN,
            is_active=True,
        )
        session.add_all([user, tariff])
        await session.commit()
        await session.refresh(user)
        await session.refresh(tariff)

    monkeypatch.setattr(payment_handler, "async_session", db_session_factory)
    monkeypatch.setattr(payment_handler, "is_vhq_tariff", lambda _tariff: False)
    monkeypatch.setattr(payment_handler, "_notify_delivery_issue", AsyncMock())

    async def _raise_issue(*args, **kwargs):
        raise AccessProvisionError(
            provider="marzban",
            code="test_failure",
            client_message="Оплата прошла, но выдача задержалась.",
            admin_message="Synthetic failure",
        )

    monkeypatch.setattr(payment_handler, "create_or_extend_paid_access", _raise_issue)

    message = _make_payment_message(
        user_id=10001,
        payload=f"{tariff.id}_android_0",
        charge_id="charge_test_1",
    )

    await payment_handler.process_successful_payment(message)

    async with db_session_factory() as session:
        payment = await session.scalar(
            select(Payment).where(Payment.provider_payment_id == "charge_test_1")
        )

    assert payment is not None
    assert payment.user_id == user.id
    assert payment.method == PaymentMethod.STARS
    assert payment.status == PaymentStatus.COMPLETED
    assert payment.subscription_id is None
    message.bot.send_message.assert_awaited_once()


async def test_duplicate_successful_payment_is_ignored(monkeypatch, db_session_factory):
    async with db_session_factory() as session:
        user = User(telegram_id=10002, username="buyer2", full_name="Buyer 2")
        payment = Payment(
            user_id=1,
            amount=1000,
            currency="XTR",
            method=PaymentMethod.STARS,
            status=PaymentStatus.COMPLETED,
            provider_payment_id="charge_duplicate",
        )
        session.add(user)
        await session.flush()
        payment.user_id = user.id
        session.add(payment)
        await session.commit()

    monkeypatch.setattr(payment_handler, "async_session", db_session_factory)
    create_access = AsyncMock()
    monkeypatch.setattr(payment_handler, "create_or_extend_paid_access", create_access)

    message = _make_payment_message(
        user_id=10002,
        payload="1_android_0",
        charge_id="charge_duplicate",
    )

    await payment_handler.process_successful_payment(message)

    create_access.assert_not_awaited()
    message.answer.assert_awaited_once()
    assert "уже обработан" in message.answer.await_args.args[0]


async def test_successful_stars_payment_delivers_key_and_guide(monkeypatch, db_session_factory):
    async with db_session_factory() as session:
        user = User(telegram_id=10003, username="buyer3", full_name="Buyer 3")
        server = Server(name="NL", host="192.0.2.10", location="Netherlands")
        tariff = Tariff(
            days=30,
            label="Тест 30 дней",
            price_rub=95,
            price_stars=950,
            tariff_type=TariffType.VPN,
            is_active=True,
        )
        session.add_all([user, server, tariff])
        await session.commit()
        await session.refresh(user)
        await session.refresh(server)
        await session.refresh(tariff)

    monkeypatch.setattr(payment_handler, "async_session", db_session_factory)
    monkeypatch.setattr(payment_handler, "is_vhq_tariff", lambda _tariff: False)
    monkeypatch.setattr(payment_handler, "_notify_delivery_issue", AsyncMock())
    monkeypatch.setattr(payment_handler, "log_referral_payment", AsyncMock())
    monkeypatch.setattr(payment_handler, "credit_referral", AsyncMock())
    monkeypatch.setattr(payment_handler, "notify_admins_payment", AsyncMock())

    from bot.services import guide_service

    send_guide = AsyncMock()
    monkeypatch.setattr(guide_service, "send_guide", send_guide)

    async def _create_access(session, user, tariff, platform, bonus_days, **_kwargs):
        subscription = Subscription(
            user_id=user.id,
            server_id=server.id,
            tariff_months=1,
            tariff_days=tariff.days,
            status=SubStatus.ACTIVE,
            device_slots=3,
            vpn_key="https://loonapie.xyz/s/test-token",
            client_name="tg10003_1",
            platform=platform,
            expires_at=__import__("datetime").datetime.utcnow() + __import__("datetime").timedelta(days=30),
        )
        session.add(subscription)
        await session.flush()
        return subscription, "https://loonapie.xyz/s/test-token"

    monkeypatch.setattr(payment_handler, "create_or_extend_paid_access", _create_access)

    message = _make_payment_message(
        user_id=10003,
        payload=f"{tariff.id}_android_0",
        charge_id="charge_success_1",
        total_amount=950,
    )

    await payment_handler.process_successful_payment(message)

    async with db_session_factory() as session:
        payment = await session.scalar(
            select(Payment).where(Payment.provider_payment_id == "charge_success_1")
        )

    assert payment is not None
    assert payment.status == PaymentStatus.COMPLETED
    assert payment.subscription_id is not None
    message.answer.assert_not_awaited()
    user_messages = [
        call.args[1]
        for call in message.bot.send_message.await_args_list
        if call.args[0] == message.from_user.id
    ]
    assert len(user_messages) == 2
    first_text, second_text = user_messages
    assert "Подписка готова" in first_text
    assert "https://loonapie.xyz/s/test-token" not in first_text
    assert second_text == "<code>https://loonapie.xyz/s/test-token</code>"
    send_guide.assert_awaited_once()
    assert send_guide.await_args.args[1] == message.from_user.id
    assert send_guide.await_args.args[2] == Platform.ANDROID


async def test_successful_telegram_pay_deferred_platform_creates_subscription(monkeypatch, db_session_factory):
    async with db_session_factory() as session:
        user = User(
            telegram_id=10031,
            username="buyer31",
            full_name="Buyer 31",
            platform=Platform.IOS,
        )
        server = Server(name="NL", host="192.0.2.10", location="Netherlands")
        tariff = Tariff(
            days=30,
            label="1 месяц",
            price_rub=99,
            tariff_type=TariffType.VPN,
            is_active=True,
        )
        session.add_all([user, server, tariff])
        await session.commit()
        await session.refresh(user)
        await session.refresh(server)
        await session.refresh(tariff)

    monkeypatch.setattr(payment_handler, "async_session", db_session_factory)
    monkeypatch.setattr(payment_handler, "is_vhq_tariff", lambda _tariff: False)
    monkeypatch.setattr(payment_handler, "_notify_delivery_issue", AsyncMock())
    monkeypatch.setattr(payment_handler, "log_referral_payment", AsyncMock())
    monkeypatch.setattr(payment_handler, "credit_referral", AsyncMock())
    monkeypatch.setattr(payment_handler, "notify_admins_payment", AsyncMock())

    async def _create_access(session, user, tariff, platform, bonus_days, **_kwargs):
        subscription = Subscription(
            user_id=user.id,
            server_id=server.id,
            tariff_months=1,
            tariff_days=tariff.days,
            status=SubStatus.ACTIVE,
            device_slots=3,
            vpn_key="https://loonapie.xyz/s/deferred-token",
            client_name="tg10031_1",
            platform=platform,
            expires_at=__import__("datetime").datetime.utcnow() + __import__("datetime").timedelta(days=30),
        )
        session.add(subscription)
        await session.flush()
        return subscription, "https://loonapie.xyz/s/deferred-token"

    monkeypatch.setattr(payment_handler, "create_or_extend_paid_access", _create_access)

    message = _make_payment_message(
        user_id=10031,
        payload=f"{tariff.id}_deferred_0.0",
        charge_id="charge_deferred_1",
        total_amount=9900,
        currency="RUB",
    )

    await payment_handler.process_successful_payment(message)

    async with db_session_factory() as session:
        payment = await session.scalar(
            select(Payment).where(Payment.provider_payment_id == "charge_deferred_1")
        )

    assert payment is not None
    assert payment.status == PaymentStatus.COMPLETED
    assert payment.subscription_id is not None
    message.answer.assert_awaited_once()
    assert "Выберите устройство" in message.answer.await_args.args[0]


async def test_telegram_pay_topup_marks_pending_topup_completed_and_notifies_admins(monkeypatch, db_session_factory):
    async with db_session_factory() as session:
        user = User(telegram_id=10004, username="buyer4", full_name="Buyer 4", balance_rub=0.0)
        session.add(user)
        await session.flush()
        topup = BalanceTopUp(
            user_id=user.id,
            telegram_id=user.telegram_id,
            amount_rub=500.0,
            provider="telegram",
            status="pending",
            provider_payment_id="11",
        )
        session.add(topup)
        await session.commit()
        await session.refresh(user)
        await session.refresh(topup)

    monkeypatch.setattr(payment_handler, "async_session", db_session_factory)
    notify_admins = AsyncMock()
    monkeypatch.setattr(payment_handler, "notify_admins_payment", notify_admins)

    message = _make_payment_message(
        user_id=10004,
        payload=f"topup_500_{topup.id}",
        charge_id="charge_topup_1",
        total_amount=50000,
        currency="RUB",
    )

    await payment_handler.process_successful_payment(message)

    async with db_session_factory() as session:
        refreshed_user = await session.get(User, user.id)
        refreshed_topup = await session.get(BalanceTopUp, topup.id)
        payment = await session.scalar(
            select(Payment).where(Payment.provider_payment_id == "charge_topup_1")
        )
        balance_tx = await session.scalar(
            select(BalanceTransaction).where(BalanceTransaction.user_id == user.id)
        )

    assert refreshed_user.balance_rub == 500.0
    assert refreshed_topup.status == "completed"
    assert refreshed_topup.provider_payment_id == "charge_topup_1"
    assert refreshed_topup.completed_at is not None
    assert payment is not None
    assert payment.method == PaymentMethod.TELEGRAM
    assert payment.status == PaymentStatus.COMPLETED
    assert balance_tx is not None
    assert balance_tx.amount_rub == 500.0
    notify_admins.assert_awaited_once()
    assert "Баланс пополнен" in message.answer.await_args.args[0]

async def test_device_payment_reuses_existing_subscription_key(monkeypatch, db_session_factory):
    from datetime import datetime, timedelta

    async with db_session_factory() as session:
        user = User(telegram_id=10004, username="devicebuyer", full_name="Device Buyer")
        server = Server(name="NL", host="192.0.2.10", location="Netherlands")
        session.add_all([user, server])
        await session.commit()
        await session.refresh(user)
        await session.refresh(server)
        sub = Subscription(
            user_id=user.id,
            server_id=server.id,
            tariff_months=1,
            tariff_days=30,
            status=SubStatus.ACTIVE,
            device_slots=1,
            vpn_key="https://loonapie.xyz/s/existing-token",
            client_name="tg10004_1",
            platform=Platform.ANDROID,
            expires_at=datetime.utcnow() + timedelta(days=30),
        )
        session.add(sub)
        await session.commit()
        await session.refresh(sub)

    monkeypatch.setattr(payment_handler, "async_session", db_session_factory)
    monkeypatch.setattr(payment_handler.vpn_manager, "generate_key", AsyncMock(side_effect=AssertionError("must not generate a new key")))
    monkeypatch.setattr("bot.services.payment_service.credit_referral", AsyncMock())
    monkeypatch.setattr("bot.services.payment_service.log_referral_payment", AsyncMock())
    monkeypatch.setattr("bot.services.payment_service.credit_partner", AsyncMock())
    monkeypatch.setattr(payment_handler, "notify_admins_payment", AsyncMock())

    message = _make_payment_message(
        user_id=10004,
        payload=f"dev_{sub.id}",
        charge_id="charge_device_same_key",
    )

    await payment_handler.process_device_payment(message, f"dev_{sub.id}")

    async with db_session_factory() as session:
        refreshed = await session.get(Subscription, sub.id)
        proxy_count = len((await session.execute(select(ProxyAccount))).scalars().all())
        payment = await session.scalar(select(Payment).where(Payment.provider_payment_id == "charge_device_same_key"))

    assert refreshed.device_slots == 2
    assert proxy_count == 0
    assert payment.subscription_id == sub.id
    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert "https://loonapie.xyz/s/existing-token" in text
    assert "тот же ключ" in text


async def test_auto_retry_failed_provisionings(monkeypatch, db_session_factory):
    async with db_session_factory() as session:
        user = User(telegram_id=10005, username="retrybuyer", full_name="Retry Buyer")
        server = Server(name="NL", host="192.0.2.10", location="Netherlands")
        tariff = Tariff(
            days=30,
            label="Retry Tariff",
            price_rub=99,
            price_stars=990,
            tariff_type=TariffType.VPN,
            is_active=True,
        )
        session.add_all([user, server, tariff])
        await session.commit()
        await session.refresh(user)
        await session.refresh(server)
        await session.refresh(tariff)

        payment = Payment(
            user_id=user.id,
            subscription_id=None,
            amount=9900,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            provider_payment_id="yookassa_retry_test_1",
            tariff_id=tariff.id,
            platform="android~u~42",
        )
        session.add(payment)
        await session.commit()
        await session.refresh(payment)

    from bot.services import scheduler
    monkeypatch.setattr(scheduler, "async_session", db_session_factory)
    monkeypatch.setattr(payment_handler, "async_session", db_session_factory)
    
    bot = SimpleNamespace(send_message=AsyncMock())
    
    from bot.services import guide_service
    send_guide = AsyncMock()
    monkeypatch.setattr(guide_service, "send_guide", send_guide)
    
    from bot.services import payment_service
    credit_referral = AsyncMock()
    log_referral_payment = AsyncMock()
    credit_partner = AsyncMock()
    monkeypatch.setattr(payment_service, "credit_referral", credit_referral)
    monkeypatch.setattr(payment_service, "log_referral_payment", log_referral_payment)
    monkeypatch.setattr(payment_service, "credit_partner", credit_partner)

    from bot.services.cluster import LeaseManager
    monkeypatch.setattr(LeaseManager, "acquire_or_renew", AsyncMock(return_value=True))
    monkeypatch.setattr(LeaseManager, "release", AsyncMock())

    retry_intent = {}

    async def _create_access(session, user, tariff, platform, bonus_days, **kwargs):
        retry_intent.update(kwargs)
        subscription = Subscription(
            user_id=user.id,
            server_id=server.id,
            tariff_months=1,
            tariff_days=tariff.days,
            status=SubStatus.ACTIVE,
            device_slots=3,
            vpn_key="https://loonapie.xyz/s/retry-token",
            client_name="tg10005_1",
            platform=platform,
            expires_at=__import__("datetime").datetime.utcnow() + __import__("datetime").timedelta(days=30),
        )
        session.add(subscription)
        await session.flush()
        return subscription, "https://loonapie.xyz/s/retry-token"

    from bot.services import subscription_service
    monkeypatch.setattr(subscription_service, "create_or_extend_paid_access", _create_access)

    await scheduler.auto_retry_failed_provisionings(bot)

    async with db_session_factory() as session:
        refreshed_payment = await session.get(Payment, payment.id)
        assert refreshed_payment.subscription_id is not None
        
        subscription = await session.get(Subscription, refreshed_payment.subscription_id)
        assert subscription is not None
        assert subscription.vpn_key == "https://loonapie.xyz/s/retry-token"
        assert subscription.user_id == user.id

    assert bot.send_message.called
    assert send_guide.called
    assert credit_referral.called
    assert retry_intent["purchase_action"] == "upgrade"
    assert retry_intent["target_subscription_id"] == 42
    assert retry_intent["provisioning_payment"].id == payment.id


async def test_recurring_charge_renews_exact_linked_subscription(monkeypatch, db_session_factory):
    from bot.services import payment_service, scheduler, subscription_service

    async with db_session_factory() as session:
        user = User(telegram_id=10006, username="recurring", full_name="Recurring")
        server = Server(
            name="recurring-server",
            host="127.0.0.1",
            location="Test",
        )
        tariff = Tariff(
            days=30,
            label="VPN 5 устройств",
            price_rub=950,
            price_stars=950,
            tariff_type=TariffType.VPN,
            is_active=True,
        )
        session.add_all([user, server, tariff])
        await session.flush()
        unrelated = Subscription(
            user_id=user.id,
            server_id=server.id,
            tariff_months=1,
            tariff_days=30,
            status=SubStatus.EXPIRED,
            device_slots=3,
            client_name="unrelated",
            platform=Platform.ANDROID,
            expires_at=datetime.utcnow() - timedelta(days=2),
        )
        target = Subscription(
            user_id=user.id,
            server_id=server.id,
            tariff_months=1,
            tariff_days=30,
            status=SubStatus.EXPIRED,
            device_slots=5,
            client_name="target",
            platform=Platform.IOS,
            expires_at=datetime.utcnow() - timedelta(days=1),
        )
        session.add_all([unrelated, target])
        await session.flush()
        profile = RecurringPaymentProfile(
            user_id=user.id,
            subscription_id=target.id,
            tariff_id=tariff.id,
            provider="yookassa",
            provider_payment_method_id="pm-test",
            is_active=True,
            consent_granted=True,
            next_charge_at=target.expires_at,
        )
        session.add(profile)
        await session.commit()
        target_id = target.id

    monkeypatch.setattr(scheduler, "async_session", db_session_factory)
    monkeypatch.setattr(scheduler.settings, "recurring_payments_enabled", True)
    monkeypatch.setattr(scheduler.settings, "admin_ids", [])
    charge = AsyncMock(return_value=("succeeded", "payment-recurring", "succeeded"))
    monkeypatch.setattr(payment_service, "create_recurring_yookassa_payment", charge)
    access_kwargs = {}

    async def _renew(session, *, user, tariff, platform, **kwargs):
        access_kwargs.update(kwargs)
        subscription = await session.get(Subscription, target_id)
        subscription.status = SubStatus.ACTIVE
        subscription.expires_at = datetime.utcnow() + timedelta(days=30)
        return subscription, "stable-key"

    monkeypatch.setattr(subscription_service, "create_or_extend_paid_access", _renew)
    bot = SimpleNamespace(send_message=AsyncMock())

    await scheduler.process_recurring_charges(bot)

    assert access_kwargs == {
        "purchase_action": "renew",
        "target_subscription_id": target_id,
    }
    charge.assert_awaited_once()


async def test_recurring_profile_without_subscription_is_disabled_before_charge(
    monkeypatch,
    db_session_factory,
):
    from bot.services import payment_service, scheduler

    async with db_session_factory() as session:
        user = User(telegram_id=10007, username="orphan", full_name="Orphan")
        tariff = Tariff(
            days=30,
            label="VPN",
            price_rub=950,
            price_stars=950,
            tariff_type=TariffType.VPN,
            is_active=True,
        )
        session.add_all([user, tariff])
        await session.flush()
        profile = RecurringPaymentProfile(
            user_id=user.id,
            tariff_id=tariff.id,
            provider="yookassa",
            provider_payment_method_id="pm-orphan",
            is_active=True,
            consent_granted=True,
            next_charge_at=datetime.utcnow(),
        )
        session.add(profile)
        await session.commit()
        profile_id = profile.id

    monkeypatch.setattr(scheduler, "async_session", db_session_factory)
    monkeypatch.setattr(scheduler.settings, "recurring_payments_enabled", True)
    charge = AsyncMock()
    monkeypatch.setattr(payment_service, "create_recurring_yookassa_payment", charge)

    await scheduler.process_recurring_charges(SimpleNamespace(send_message=AsyncMock()))

    charge.assert_not_awaited()
    async with db_session_factory() as session:
        refreshed = await session.get(RecurringPaymentProfile, profile_id)
        assert refreshed.is_active is False
        assert refreshed.consent_granted is False
