"""Tests that inactive tariffs cannot be purchased through cached callbacks."""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.models import Base, Platform, Server, Subscription, SubStatus, Tariff, TariffType, User


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


@pytest_asyncio.fixture
async def inactive_tariff(db_session_factory):
    async with db_session_factory() as session:
        tariff = Tariff(
            days=365,
            label="1 год",
            price_rub=995,
            price_stars=0,
            tariff_type=TariffType.VPN,
            is_active=False,
        )
        session.add(tariff)
        await session.commit()
        await session.refresh(tariff)
        return tariff


def _make_callback(tariff_id: int, user_id: int = 100500) -> SimpleNamespace:
    message = SimpleNamespace(
        edit_text=AsyncMock(),
        chat=SimpleNamespace(id=user_id),
    )
    return SimpleNamespace(
        data=f"tariff_{tariff_id}",
        from_user=SimpleNamespace(id=user_id, username="tester"),
        message=message,
        answer=AsyncMock(),
        bot=AsyncMock(),
    )


async def test_select_tariff_rejects_inactive(db_session_factory, inactive_tariff):
    """select_tariff handler must reject an inactive tariff with show_alert."""
    from bot.handlers import buy as buy_handler

    callback = _make_callback(inactive_tariff.id)

    with patch("bot.handlers.buy.async_session", db_session_factory):
        await buy_handler.select_tariff(callback)

    callback.answer.assert_awaited_once()
    call_kwargs = callback.answer.await_args
    assert call_kwargs.kwargs.get("show_alert") is True
    assert "недоступен" in call_kwargs.args[0]
    # Must NOT proceed to platform selection
    callback.message.edit_text.assert_not_awaited()


async def test_select_tariff_rejects_hidden_tariff_for_non_admin(db_session_factory):
    """A cached/direct callback must not expose an admin-only tariff."""
    from bot.handlers import buy as buy_handler

    async with db_session_factory() as session:
        tariff = Tariff(
            days=30,
            label="Скрытый",
            price_rub=999,
            price_stars=0,
            tariff_type=TariffType.VPN,
            is_active=True,
            is_admin_only=True,
        )
        session.add(tariff)
        await session.commit()
        await session.refresh(tariff)
        tariff_id = tariff.id

    callback = _make_callback(tariff_id)
    with patch("bot.handlers.buy.async_session", db_session_factory):
        await buy_handler.select_tariff(callback)

    callback.answer.assert_awaited_once_with("Этот тариф больше недоступен", show_alert=True)
    callback.message.edit_text.assert_not_awaited()


async def test_upgrade_menu_hides_admin_only_tariffs(db_session_factory):
    """The upgrade keyboard for a regular user must contain only public tariffs."""
    from bot.handlers import buy as buy_handler

    async with db_session_factory() as session:
        user = User(telegram_id=100500, full_name="User")
        server = Server(name="test", host="127.0.0.1", location="Test")
        current = Tariff(
            days=30, label="Current", price_rub=100, price_stars=0,
            tariff_type=TariffType.VPN, is_active=True, is_admin_only=False,
            adapt_plan_uuid="plan-current",
        )
        hidden = Tariff(
            days=30, label="Hidden", price_rub=200, price_stars=0,
            tariff_type=TariffType.VPN, is_active=True, is_admin_only=True,
            adapt_plan_uuid="plan-hidden",
        )
        public = Tariff(
            days=30, label="Public", price_rub=300, price_stars=0,
            tariff_type=TariffType.VPN, is_active=True, is_admin_only=False,
            adapt_plan_uuid="plan-public",
        )
        session.add_all([user, server, current, hidden, public])
        await session.flush()
        subscription = Subscription(
            user_id=user.id,
            server_id=server.id,
            tariff_id=current.id,
            tariff_months=1,
            tariff_days=30,
            status=SubStatus.ACTIVE,
            vpn_key="https://example.test/sub",
            client_name="adapt_test",
            platform=Platform.ANDROID,
            expires_at=datetime.utcnow() + timedelta(days=10),
        )
        session.add(subscription)
        await session.commit()
        await session.refresh(subscription)
        ids = subscription.id, hidden.id, public.id

    sub_id, hidden_id, public_id = ids
    callback = _make_callback(current.id)
    callback.data = f"purchase_upgrade_{sub_id}"
    with (
        patch("bot.handlers.buy.async_session", db_session_factory),
        patch("bot.handlers.buy.settings.is_admin", return_value=False),
        patch("bot.handlers.buy._get_stars_enabled", new_callable=AsyncMock, return_value=False),
    ):
        await buy_handler.choose_upgrade_target(callback)

    markup = callback.message.edit_text.await_args.kwargs["reply_markup"]
    callbacks = [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    ]
    assert any(str(public_id) in value for value in callbacks)
    assert all(str(hidden_id) not in value for value in callbacks)


async def test_stars_payment_rejects_inactive(db_session_factory, inactive_tariff):
    """Stars payment handler must reject an inactive tariff."""
    from bot.handlers import payment as payment_handler

    callback = _make_callback(inactive_tariff.id)
    callback.data = f"pay_stars_{inactive_tariff.id}_ios_0"

    async def fake_get_tariff(tid):
        async with db_session_factory() as s:
            return await s.get(Tariff, tid)

    with patch("bot.handlers.payment._get_tariff", fake_get_tariff):
        await payment_handler.initiate_stars_payment(callback)

    callback.answer.assert_awaited_once()
    call_kwargs = callback.answer.await_args
    assert call_kwargs.kwargs.get("show_alert") is True
    assert "недоступен" in call_kwargs.args[0]


async def test_telegram_pay_rejects_inactive(db_session_factory, inactive_tariff):
    """Telegram Pay handler must reject an inactive tariff."""
    from bot.handlers import payment as payment_handler
    from bot.config import settings

    callback = _make_callback(inactive_tariff.id)
    callback.data = f"pay_telegram_{inactive_tariff.id}_ios_0"

    async def fake_get_tariff(tid):
        async with db_session_factory() as s:
            return await s.get(Tariff, tid)

    with (
        patch("bot.handlers.payment._get_tariff", fake_get_tariff),
        patch.object(settings, "telegram_payment_provider_token", "test_token"),
    ):
        await payment_handler.initiate_telegram_payment(callback)

    callback.answer.assert_awaited_once()
    call_kwargs = callback.answer.await_args
    assert call_kwargs.kwargs.get("show_alert") is True
    assert "недоступен" in call_kwargs.args[0]


async def test_balance_pay_rejects_inactive(db_session_factory, inactive_tariff):
    """Balance payment handler must reject an inactive tariff."""
    from bot.handlers import payment as payment_handler

    callback = _make_callback(inactive_tariff.id)
    callback.data = f"pay_balance_{inactive_tariff.id}_ios_0"

    async def fake_get_tariff(tid):
        async with db_session_factory() as s:
            return await s.get(Tariff, tid)

    with patch("bot.handlers.payment._get_tariff", fake_get_tariff):
        await payment_handler.initiate_balance_payment(callback)

    callback.answer.assert_awaited_once()
    call_kwargs = callback.answer.await_args
    assert call_kwargs.kwargs.get("show_alert") is True
    assert "недоступен" in call_kwargs.args[0]
