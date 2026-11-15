from __future__ import annotations

import types
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.models import (
    Base,
    BalanceTopUp,
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
)
from bot.services import payment_service
from bot.services.money import (
    kopecks_to_rub_string,
    rub_to_kopecks,
)


@pytest_asyncio.fixture
async def payment_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


def test_canonical_rub_conversion_handles_float_traps() -> None:
    assert rub_to_kopecks("109.84") == 10984
    assert rub_to_kopecks(289.15999999999997) == 28916
    assert rub_to_kopecks(0.1 + 0.2) == 30
    assert rub_to_kopecks(112.50) == 11250
    assert rub_to_kopecks(155) == 15500
    assert rub_to_kopecks(10) == 1000
    assert rub_to_kopecks(399 - 109.84) == 28916
    assert rub_to_kopecks(399) - rub_to_kopecks("109.84") == 28916
    assert kopecks_to_rub_string(28916) == "289.16"


class _FakePaymentApi:
    last_call = None

    @classmethod
    def create(cls, payload, idempotence_key):
        cls.last_call = (payload, idempotence_key)
        return types.SimpleNamespace(
            id="yk_test_payment",
            confirmation=types.SimpleNamespace(confirmation_url="https://pay.test/confirm"),
        )


class _FakeYooKassa(types.SimpleNamespace):
    Configuration = types.SimpleNamespace(account_id=None, secret_key=None)
    Payment = _FakePaymentApi


@pytest.mark.asyncio
async def test_standard_create_uses_canonical_amount_and_stable_key(monkeypatch):
    monkeypatch.setattr(payment_service.settings, "yookassa_shop_id", "shop")
    monkeypatch.setattr(payment_service.settings, "yookassa_secret_key", "secret")
    monkeypatch.setitem(__import__("sys").modules, "yookassa", _FakeYooKassa())

    await payment_service.create_yookassa_payment(
        user_id=1,
        tariff_id=2,
        platform="deferred~r~327",
        chat_id=3,
        return_url="https://example.com/return",
        tariff_label="Премиум",
        price_kopecks=28916,
        idempotence_key="bot-payment-317",
        metadata={"payment_id": "317"},
    )

    payload, key = _FakePaymentApi.last_call
    assert payload["amount"] == {"value": "289.16", "currency": "RUB"}
    assert key == "bot-payment-317"


@pytest.mark.asyncio
async def test_recurring_create_replay_keeps_same_key_after_ambiguous_result(monkeypatch):
    calls = []

    class _RecurringPaymentApi:
        @classmethod
        def create(cls, payload, idempotence_key):
            calls.append((payload, idempotence_key))
            if len(calls) == 1:
                raise TimeoutError("response lost after provider accepted request")
            return types.SimpleNamespace(id="yk_recurring_payment", status="succeeded")

    class _RecurringYooKassa(types.SimpleNamespace):
        Configuration = types.SimpleNamespace(account_id=None, secret_key=None)
        Payment = _RecurringPaymentApi

    monkeypatch.setattr(payment_service.settings, "yookassa_shop_id", "shop")
    monkeypatch.setattr(payment_service.settings, "yookassa_secret_key", "secret")
    monkeypatch.setitem(__import__("sys").modules, "yookassa", _RecurringYooKassa())
    exceptions_module = types.ModuleType("yookassa.domain.exceptions")
    for name in ("BadRequestError", "ForbiddenError", "InternalServerError", "TooManyRequestsError", "UnauthorizedError"):
        setattr(exceptions_module, name, type(name, (Exception,), {}))
    domain_module = types.ModuleType("yookassa.domain")
    domain_module.exceptions = exceptions_module
    monkeypatch.setitem(__import__("sys").modules, "yookassa.domain", domain_module)
    monkeypatch.setitem(__import__("sys").modules, "yookassa.domain.exceptions", exceptions_module)

    key = "recurring-profile-7-1735689600"
    first = await payment_service.create_recurring_yookassa_payment(
        payment_method_id="pm_saved",
        amount_rub=289.16,
        amount_kopecks=28916,
        description="Автопродление Премиум",
        user_id=1,
        tariff_id=2,
        idempotence_key=key,
    )
    second = await payment_service.create_recurring_yookassa_payment(
        payment_method_id="pm_saved",
        amount_rub=289.16,
        amount_kopecks=28916,
        description="Автопродление Премиум",
        user_id=1,
        tariff_id=2,
        idempotence_key=key,
    )

    assert first[0] == "provider_error"
    assert second[0] == "succeeded"
    assert calls[0][1] == calls[1][1] == key
    assert calls[0][0] == calls[1][0]
    assert calls[0][0]["amount"] == {"value": "289.16", "currency": "RUB"}


def test_payment_safety_static_guard_has_no_float_truncation_in_yookassa_paths():
    from scripts.check_payments_safety import scan_payment_safety

    violations = scan_payment_safety()
    assert not [item for item in violations if "int(" in item or "float(provider" in item]


@pytest.mark.asyncio
async def test_device_create_reuses_canonical_payment_operation(monkeypatch, payment_db):
    from bot.config import settings
    from bot.handlers.devices import bot_send_yookassa

    monkeypatch.setattr(settings, "yookassa_shop_id", "shop")
    monkeypatch.setattr(settings, "yookassa_secret_key", "secret")
    monkeypatch.setattr("bot.handlers.payment.async_session", payment_db)
    create_mock = AsyncMock(return_value=("https://pay.test/device", "yk_device"))
    monkeypatch.setattr("bot.handlers.devices.async_session", payment_db)
    monkeypatch.setattr("bot.handlers.devices.create_yookassa_payment", create_mock)

    callback = types.SimpleNamespace(
        from_user=types.SimpleNamespace(id=701),
        message=types.SimpleNamespace(
            chat=types.SimpleNamespace(id=702),
            edit_text=types.SimpleNamespace(),
            answer=types.SimpleNamespace(),
        ),
        answer=types.SimpleNamespace(),
    )
    callback.message.edit_text = AsyncMock()
    callback.message.answer = AsyncMock()
    callback.answer = AsyncMock()

    await bot_send_yookassa(callback, "Дополнительное устройство", "Слот", "dev_8", 50, 1, 8)
    await bot_send_yookassa(callback, "Дополнительное устройство", "Слот", "dev_8", 50, 1, 8)

    assert create_mock.await_count == 2
    first_kwargs = create_mock.await_args_list[0].kwargs
    second_kwargs = create_mock.await_args_list[1].kwargs
    assert first_kwargs["price_kopecks"] == second_kwargs["price_kopecks"] == 5000
    assert first_kwargs["idempotence_key"] == second_kwargs["idempotence_key"] == "bot-payment-1"
    async with payment_db() as session:
        payment = await session.get(Payment, 1)
        assert payment.amount == 5000
        assert payment.provider_idempotence_key == "bot-payment-1"


@pytest.mark.asyncio
async def test_topup_create_reuses_existing_stable_key(monkeypatch, payment_db):
    from bot.config import settings
    from bot.handlers.payment import initiate_yookassa_topup

    calls = []

    class _TopupPaymentApi:
        @classmethod
        def create(cls, payload, idempotence_key):
            calls.append((payload, idempotence_key))
            return types.SimpleNamespace(
                id="yk_topup",
                confirmation=types.SimpleNamespace(confirmation_url="https://pay.test/topup"),
            )

    class _TopupYooKassa(types.SimpleNamespace):
        Configuration = types.SimpleNamespace(account_id=None, secret_key=None)
        Payment = _TopupPaymentApi

    monkeypatch.setattr(settings, "yookassa_shop_id", "shop")
    monkeypatch.setattr(settings, "yookassa_secret_key", "secret")
    monkeypatch.setattr("bot.handlers.payment.async_session", payment_db)
    monkeypatch.setitem(__import__("sys").modules, "yookassa", _TopupYooKassa())

    callback = types.SimpleNamespace(
        data="paytopup_yookassa_250",
        from_user=types.SimpleNamespace(id=801, username="topup", full_name="Top Up"),
        message=types.SimpleNamespace(
            chat=types.SimpleNamespace(id=802),
            answer=AsyncMock(),
        ),
        answer=AsyncMock(),
    )

    await initiate_yookassa_topup(callback)
    await initiate_yookassa_topup(callback)

    assert len(calls) == 2
    assert calls[0][1] == calls[1][1] == "balance-topup-1"
    assert calls[0][0]["amount"] == calls[1][0]["amount"] == {"value": "250.00", "currency": "RUB"}
    async with payment_db() as session:
        topup = await session.get(BalanceTopUp, 1)
        assert topup.amount_kopecks == 25000
        assert topup.provider_idempotence_key == "balance-topup-1"


@pytest.mark.asyncio
async def test_old_canceled_checkout_allows_new_standard_device_and_topup_operations(monkeypatch, payment_db):
    from bot.config import settings
    from bot.handlers.devices import bot_send_yookassa
    from bot.handlers.payment import initiate_yookassa_payment, initiate_yookassa_topup

    monkeypatch.setattr(settings, "yookassa_shop_id", "shop")
    monkeypatch.setattr(settings, "yookassa_secret_key", "secret")
    monkeypatch.setattr("bot.handlers.payment.async_session", payment_db)
    async with payment_db() as session:
        user = User(id=1, telegram_id=91001, username="stale", full_name="Stale")
        server = Server(id=1, name="stale-server", host="vpn.test", location="test")
        tariff = Tariff(
            id=1,
            label="Baza",
            price_rub=100,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
        )
        sub = Subscription(
            id=1,
            user_id=1,
            server_id=1,
            tariff_id=1,
            tariff_months=1,
            tariff_days=30,
            status=SubStatus.ACTIVE,
            platform=Platform.ANDROID,
            client_name="sub-1",
            expires_at=datetime.utcnow() + timedelta(days=30),
        )
        old_standard = Payment(
            id=1,
            user_id=1,
            tariff_id=1,
            platform="android",
            amount=10000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_old_standard",
            provider_idempotence_key="bot-payment-1",
            provider_status="canceled",
            provider_paid=False,
            created_at=datetime.utcnow() - timedelta(hours=25),
        )
        old_device = Payment(
            id=2,
            user_id=1,
            subscription_id=1,
            platform="device",
            amount=5000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_old_device",
            provider_idempotence_key="bot-payment-2",
            provider_status="canceled",
            provider_paid=False,
            created_at=datetime.utcnow() - timedelta(hours=25),
        )
        old_topup = BalanceTopUp(
            id=3,
            user_id=1,
            telegram_id=91001,
            amount_rub=250,
            amount_kopecks=25000,
            provider="yookassa",
            status="pending",
            provider_payment_id="3",
            provider_idempotence_key="balance-topup-3",
            provider_status="canceled",
            provider_paid=False,
            created_at=datetime.utcnow() - timedelta(hours=25),
        )
        session.add_all([user, server, tariff, sub, old_standard, old_device, old_topup])
        await session.commit()

    create_standard = AsyncMock(return_value=("https://pay.test/new-standard", "yk_new_standard"))
    monkeypatch.setattr("bot.handlers.payment.create_yookassa_payment", create_standard)
    monkeypatch.setattr("bot.handlers.payment._get_tariff", AsyncMock(return_value=tariff))
    monkeypatch.setattr("bot.handlers.payment._ensure_intro_basic_available", AsyncMock(return_value=True))
    standard_callback = types.SimpleNamespace(
        data="pay_yookassa_1_android_0",
        from_user=types.SimpleNamespace(id=91001, username="stale", full_name="Stale"),
        message=types.SimpleNamespace(chat=types.SimpleNamespace(id=91001), answer=AsyncMock()),
        answer=AsyncMock(),
    )
    await initiate_yookassa_payment(standard_callback)
    assert create_standard.await_args.kwargs["idempotence_key"] == "bot-payment-3"

    create_device = AsyncMock(return_value=("https://pay.test/new-device", "yk_new_device"))
    monkeypatch.setattr("bot.handlers.devices.async_session", payment_db)
    monkeypatch.setattr("bot.handlers.devices.create_yookassa_payment", create_device)
    device_callback = types.SimpleNamespace(
        from_user=types.SimpleNamespace(id=91001),
        message=types.SimpleNamespace(chat=types.SimpleNamespace(id=91001), edit_text=AsyncMock(), answer=AsyncMock()),
        answer=AsyncMock(),
    )
    await bot_send_yookassa(device_callback, "Device", "Slot", "dev", 50, 1, 1)
    assert create_device.await_args.kwargs["idempotence_key"] == "bot-payment-4"

    calls = []
    class _TopupApi:
        @classmethod
        def create(cls, payload, key):
            calls.append((payload, key))
            return types.SimpleNamespace(
                id="yk_new_topup",
                confirmation=types.SimpleNamespace(confirmation_url="https://pay.test/new-topup"),
            )
    class _TopupYooKassa(types.SimpleNamespace):
        Configuration = types.SimpleNamespace(account_id=None, secret_key=None)
        Payment = _TopupApi
    monkeypatch.setitem(__import__("sys").modules, "yookassa", _TopupYooKassa())
    monkeypatch.setattr("bot.handlers.payment.async_session", payment_db)
    topup_callback = types.SimpleNamespace(
        data="paytopup_yookassa_250",
        from_user=types.SimpleNamespace(id=91001, username="stale", full_name="Stale"),
        message=types.SimpleNamespace(chat=types.SimpleNamespace(id=91001), answer=AsyncMock()),
        answer=AsyncMock(),
    )
    await initiate_yookassa_topup(topup_callback)
    assert calls[0][1] == "balance-topup-4"

    async with payment_db() as session:
        assert (await session.get(Payment, 1)).status == PaymentStatus.FAILED
        assert (await session.get(Payment, 2)).status == PaymentStatus.FAILED
        assert (await session.get(BalanceTopUp, 3)).status == "canceled"
        assert (await session.get(Payment, 3)).provider_idempotence_key == "bot-payment-3"
        assert (await session.get(Payment, 4)).provider_idempotence_key == "bot-payment-4"


@pytest.mark.asyncio
async def test_old_unknown_checkout_fails_closed_for_standard_device_and_topup(monkeypatch, payment_db):
    from bot.config import settings
    from bot.handlers.devices import bot_send_yookassa
    from bot.handlers.payment import initiate_yookassa_payment, initiate_yookassa_topup

    monkeypatch.setattr(settings, "yookassa_shop_id", "shop")
    monkeypatch.setattr(settings, "yookassa_secret_key", "secret")
    monkeypatch.setattr("bot.handlers.payment.async_session", payment_db)
    async with payment_db() as session:
        user = User(id=1, telegram_id=91002, username="unknown", full_name="Unknown")
        server = Server(id=1, name="unknown-server", host="vpn.test", location="test")
        tariff = Tariff(id=1, label="Baza", price_rub=100, price_stars=0, days=30, is_active=True, tariff_type=TariffType.VPN)
        sub = Subscription(id=1, user_id=1, server_id=1, tariff_id=1, tariff_months=1, tariff_days=30, status=SubStatus.ACTIVE, platform=Platform.ANDROID, client_name="sub-1", expires_at=datetime.utcnow() + timedelta(days=30))
        session.add_all([user, server, tariff, sub,
                         Payment(id=1, user_id=1, tariff_id=1, platform="android", amount=10000, currency="RUB", method=PaymentMethod.YOOKASSA, status=PaymentStatus.PENDING, provider_idempotence_key="bot-payment-1", created_at=datetime.utcnow() - timedelta(hours=25)),
                         Payment(id=2, user_id=1, subscription_id=1, platform="device", amount=5000, currency="RUB", method=PaymentMethod.YOOKASSA, status=PaymentStatus.PENDING, provider_idempotence_key="bot-payment-2", created_at=datetime.utcnow() - timedelta(hours=25)),
                         BalanceTopUp(id=3, user_id=1, telegram_id=91002, amount_rub=250, amount_kopecks=25000, provider="yookassa", status="pending", provider_idempotence_key="balance-topup-3", created_at=datetime.utcnow() - timedelta(hours=25))])
        await session.commit()

    standard_create = AsyncMock()
    monkeypatch.setattr("bot.handlers.payment.create_yookassa_payment", standard_create)
    monkeypatch.setattr("bot.handlers.payment._get_tariff", AsyncMock(return_value=tariff))
    monkeypatch.setattr("bot.handlers.payment._ensure_intro_basic_available", AsyncMock(return_value=True))
    callback = types.SimpleNamespace(data="pay_yookassa_1_android_0", from_user=types.SimpleNamespace(id=91002), message=types.SimpleNamespace(chat=types.SimpleNamespace(id=91002), answer=AsyncMock()), answer=AsyncMock())
    await initiate_yookassa_payment(callback)
    device_callback = types.SimpleNamespace(from_user=types.SimpleNamespace(id=91002), message=types.SimpleNamespace(chat=types.SimpleNamespace(id=91002), edit_text=AsyncMock(), answer=AsyncMock()), answer=AsyncMock())
    monkeypatch.setattr("bot.handlers.devices.async_session", payment_db)
    monkeypatch.setattr("bot.handlers.devices.create_yookassa_payment", AsyncMock())
    await bot_send_yookassa(device_callback, "Device", "Slot", "dev", 50, 1, 1)
    topup_callback = types.SimpleNamespace(data="paytopup_yookassa_250", from_user=types.SimpleNamespace(id=91002, username="unknown", full_name="Unknown"), message=types.SimpleNamespace(chat=types.SimpleNamespace(id=91002), answer=AsyncMock()), answer=AsyncMock())
    monkeypatch.setattr("bot.handlers.payment.async_session", payment_db)
    await initiate_yookassa_topup(topup_callback)

    assert standard_create.await_count == 0
    async with payment_db() as session:
        assert (await session.scalar(__import__("sqlalchemy").select(Payment).where(Payment.id == 1))).provisioning_failure_code == "yookassa_create_ambiguous_manual_review"
        assert (await session.scalar(__import__("sqlalchemy").select(Payment).where(Payment.id == 2))).provisioning_failure_code == "yookassa_create_ambiguous_manual_review"
        assert (await session.get(BalanceTopUp, 3)).status == "pending"
