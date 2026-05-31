from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.models import (
    Base,
    BalanceTopUp,
    BalanceTransaction,
    Payment,
    PaymentMethod,
    PaymentStatus,
    Platform,
    TariffType,
)
from bot.webhooks import handle_yookassa

pytestmark = pytest.mark.asyncio


class _Request(SimpleNamespace):
    async def json(self):
        return self._json_body


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


async def test_yookassa_webhook_stores_completed_payment(monkeypatch, db_session_factory):
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)

    async with db_session_factory() as session:
        from bot.models import Tariff, User

        user = User(telegram_id=777001, username="loonapie", full_name="Loonapie User")
        tariff = Tariff(
            id=51,
            label="Лайт",
            price_rub=95,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
        )
        session.add_all([user, tariff])
        await session.commit()
        user_id = user.id

    acquire_lock = AsyncMock(return_value=True)
    monkeypatch.setattr("bot.webhooks.lease_manager.acquire_or_renew", acquire_lock)
    monkeypatch.setattr("bot.webhooks._wait_for_completed_payment", AsyncMock(return_value=False))
    monkeypatch.setattr("bot.webhooks._process_and_deliver", AsyncMock(return_value=(user_id, 9001)))
    monkeypatch.setattr("bot.webhooks.credit_referral", AsyncMock())
    monkeypatch.setattr("bot.webhooks.log_referral_payment", AsyncMock())
    monkeypatch.setattr("bot.services.payment_service.credit_partner", AsyncMock())

    request = _Request(
        _json_body={
            "event": "payment.succeeded",
            "object": {
                "id": "yk_bot_123",
                "amount": {"value": "95.00"},
                "metadata": {
                    "user_id": "777001",
                    "chat_id": "777001",
                    "tariff_id": "51",
                    "platform": Platform.IOS.value,
                },
            },
        },
        headers={},
        remote="127.0.0.1",
        app={"bot": SimpleNamespace(send_message=AsyncMock())},
    )

    response = await handle_yookassa(request)

    assert response.text == "OK"
    acquire_lock.assert_awaited()

    async with db_session_factory() as session:
        payment = await session.scalar(
            select(Payment).where(Payment.provider_payment_id == "yk_bot_123")
        )
        assert payment is not None
        assert payment.status == PaymentStatus.COMPLETED
        assert payment.user_id == user_id
        assert payment.subscription_id == 9001
        assert payment.amount == 9500
        assert payment.currency == "RUB"
        assert payment.method == PaymentMethod.YOOKASSA


async def test_yookassa_balance_topup_notifies_admins_and_credits_balance(monkeypatch, db_session_factory):
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)

    async with db_session_factory() as session:
        from bot.models import User

        user = User(telegram_id=777002, username="balanceuser", full_name="Balance User")
        session.add(user)
        await session.commit()
        user_id = user.id

    acquire_lock = AsyncMock(return_value=True)
    notify_admins = AsyncMock()
    monkeypatch.setattr("bot.webhooks.lease_manager.acquire_or_renew", acquire_lock)
    monkeypatch.setattr("bot.webhooks._wait_for_completed_payment", AsyncMock(return_value=False))
    monkeypatch.setattr("bot.webhooks.notify_admins_payment", notify_admins)

    request = _Request(
        _json_body={
            "event": "payment.succeeded",
            "object": {
                "id": "yk_topup_123",
                "amount": {"value": "500.00"},
                "metadata": {
                    "user_id": "777002",
                    "chat_id": "777002",
                    "purpose": "balance_topup",
                },
            },
        },
        headers={},
        remote="127.0.0.1",
        app={"bot": SimpleNamespace(send_message=AsyncMock())},
    )

    response = await handle_yookassa(request)

    assert response.text == "OK"
    acquire_lock.assert_awaited()
    notify_admins.assert_awaited_once()

    async with db_session_factory() as session:
        payment = await session.scalar(
            select(Payment).where(Payment.provider_payment_id == "yk_topup_123")
        )
        topup = await session.scalar(
            select(BalanceTopUp).where(BalanceTopUp.provider_payment_id == "yk_topup_123")
        )
        balance_tx = await session.scalar(
            select(BalanceTransaction).where(BalanceTransaction.user_id == user_id)
        )
        refreshed_user = await session.get(User, user_id)

    assert payment is not None
    assert payment.status == PaymentStatus.COMPLETED
    assert payment.subscription_id is None
    assert payment.amount == 50000
    assert topup is not None
    assert topup.status == "completed"
    assert balance_tx is not None
    assert balance_tx.amount_rub == 500.0
    assert refreshed_user.balance_rub == 500.0
