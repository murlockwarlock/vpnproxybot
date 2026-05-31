from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from webstore.models import Base, WebBalanceAccount, WebOrder, WebProfileLink
from webstore.routes import _disable_web_balance_autodebit_after_tariff_purchase


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def test_webstore_tariff_purchase_disables_daily_balance_autodebit(db_session_factory):
    now = datetime.utcnow()
    async with db_session_factory() as session:
        account = WebBalanceAccount(
            profile_token="profile-token",
            contact="@buyer",
            ref_code="ref123",
            balance_rub=1000,
            balance_mode_enabled=1,
            balance_autodebit_enabled=1,
            balance_grace_until=now + timedelta(days=1),
            next_daily_charge_at=now + timedelta(hours=6),
            balance_warning_for_charge_at=now + timedelta(hours=6),
        )
        order = WebOrder(
            order_id="ord-fixed-tariff",
            contact="@buyer",
            tariff_key="vpn_30",
            tariff_label="Лайт (1 месяц)",
            days=30,
            amount_rub=99,
            status="delivered",
            profile_token="profile-token",
            access_expires_at=now + timedelta(days=30),
        )
        session.add_all([account, order])
        await session.flush()

        await _disable_web_balance_autodebit_after_tariff_purchase(session, order)

        assert account.balance_mode_enabled == 0
        assert account.balance_autodebit_enabled == 0
        assert account.next_daily_charge_at is None
        assert account.balance_grace_until is None
        assert account.balance_warning_for_charge_at is None


async def test_webstore_tariff_purchase_disables_linked_bot_balance_mode(db_session_factory, monkeypatch):
    calls: list[tuple[int, bool]] = []

    async def fake_toggle_balance_mode(telegram_id: int, enabled: bool):
        calls.append((telegram_id, enabled))
        return {"ok": True}, 200

    monkeypatch.setattr("webstore.routes._toggle_balance_mode", fake_toggle_balance_mode)

    now = datetime.utcnow()
    async with db_session_factory() as session:
        link = WebProfileLink(
            profile_token="profile-token",
            contact="@buyer",
            telegram_id=1283224097,
            telegram_username="VViktoriaIA",
        )
        order = WebOrder(
            order_id="ord-linked-fixed-tariff",
            contact="@buyer",
            tariff_key="vpn_30",
            tariff_label="Лайт (1 месяц)",
            days=30,
            amount_rub=99,
            status="delivered",
            profile_token="profile-token",
            access_expires_at=now + timedelta(days=30),
        )
        session.add_all([link, order])
        await session.flush()

        await _disable_web_balance_autodebit_after_tariff_purchase(session, order)

        assert calls == [(1283224097, False)]


async def test_webstore_device_slot_purchase_keeps_daily_balance_autodebit(db_session_factory):
    now = datetime.utcnow()
    async with db_session_factory() as session:
        account = WebBalanceAccount(
            profile_token="profile-token",
            contact="@buyer",
            ref_code="ref123",
            balance_rub=1000,
            balance_mode_enabled=1,
            balance_autodebit_enabled=1,
            next_daily_charge_at=now + timedelta(hours=6),
        )
        order = WebOrder(
            order_id="ord-device-slot",
            contact="@buyer",
            tariff_key="device_slot",
            tariff_label="Дополнительное устройство",
            days=30,
            amount_rub=50,
            status="delivered",
            profile_token="profile-token",
        )
        session.add_all([account, order])
        await session.flush()

        await _disable_web_balance_autodebit_after_tariff_purchase(session, order)

        assert account.balance_mode_enabled == 1
        assert account.balance_autodebit_enabled == 1
        assert account.next_daily_charge_at is not None
