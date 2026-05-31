"""Tests that inactive tariffs cannot be purchased through cached callbacks."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.models import Base, Tariff, TariffType


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
