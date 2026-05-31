from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.models import Base, Platform, Server, SubStatus, Subscription, Tariff, TariffType, User
from bot.services import subscription_service
from bot.services.subscription_semantics import is_demo_subscription_row


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


async def test_paid_tariff_reuses_demo_key_and_disables_balance_autodebit(
    monkeypatch,
    db_session_factory,
):
    now = datetime.utcnow()
    async with db_session_factory() as session:
        user = User(
            telegram_id=1283224097,
            username="VViktoriaIA",
            full_name="Victoria",
            balance_rub=900.0,
            balance_mode_enabled=True,
            balance_autodebit_enabled=True,
            next_daily_charge_at=now + timedelta(hours=12),
            balance_grace_until=now + timedelta(days=1),
            balance_warning_for_charge_at=now + timedelta(hours=12),
        )
        server = Server(
            name="NL",
            host="72.56.71.124",
            api_url="https://panel.example",
            location="Netherlands",
            is_active=True,
        )
        tariff = Tariff(
            days=30,
            label="1 месяц",
            price_rub=99,
            price_stars=0,
            tariff_type=TariffType.VPN,
            is_active=True,
        )
        session.add_all([user, server, tariff])
        await session.flush()

        demo_expires = now + timedelta(days=1)
        subscription = Subscription(
            user_id=user.id,
            server_id=server.id,
            tariff_months=0,
            tariff_days=0,
            billing_mode="balance",
            status=SubStatus.ACTIVE,
            device_slots=1,
            vpn_key="old-key",
            client_name="moms_tg1283224097_demo",
            platform=Platform.ANDROID,
            expires_at=demo_expires,
        )
        session.add(subscription)
        await session.commit()
        await session.refresh(user)
        await session.refresh(tariff)
        await session.refresh(subscription)

        ensure_marzban_user = AsyncMock(return_value="same-key")
        monkeypatch.setattr(subscription_service, "_ensure_marzban_user", ensure_marzban_user)

        sub, key = await subscription_service.create_or_extend_paid_subscription(
            session,
            user=user,
            tariff=tariff,
            platform=Platform.IOS,
        )
        await session.commit()

        assert sub is subscription
        assert key == "same-key"
        assert sub.client_name == "moms_tg1283224097_demo"
        assert sub.billing_mode == "tariff"
        assert sub.status == SubStatus.ACTIVE
        assert sub.tariff_days == 30
        assert sub.expires_at >= demo_expires + timedelta(days=30) - timedelta(seconds=1)
        assert user.balance_mode_enabled is False
        assert user.balance_autodebit_enabled is False
        assert user.next_daily_charge_at is None
        assert user.balance_grace_until is None
        assert user.balance_warning_for_charge_at is None
        assert is_demo_subscription_row(sub) is False
        ensure_marzban_user.assert_awaited_once()


async def test_plain_demo_key_is_still_demo(db_session_factory):
    now = datetime.utcnow()
    async with db_session_factory() as session:
        user = User(telegram_id=20001, username="demo", full_name="Demo")
        server = Server(name="NL", host="72.56.71.124", location="Netherlands", is_active=True)
        session.add_all([user, server])
        await session.flush()
        subscription = Subscription(
            user_id=user.id,
            server_id=server.id,
            tariff_months=0,
            tariff_days=3,
            status=SubStatus.ACTIVE,
            device_slots=1,
            vpn_key="demo-key",
            client_name="moms_tg20001_demo",
            platform=Platform.ANDROID,
            expires_at=now + timedelta(days=3),
        )
        session.add(subscription)
        await session.flush()

        assert is_demo_subscription_row(subscription) is True
