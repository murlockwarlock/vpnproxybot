from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.models import AdaptSubscription, Base, Server, Subscription, Tariff, TariffType, User
from bot.services import webstore_bridge


@pytest.mark.asyncio
async def test_web_adapt_subscription_is_imported_once_with_actual_device_limit(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    plan_uuid = "661f9511-f3ac-52e5-b827-557766551111"
    adapt_uuid = "770fa622-a4bd-63f6-c938-668877662222"
    expires = datetime.utcnow() + timedelta(days=30)

    async with sessions() as session:
        session.add_all([
            User(telegram_id=89154970647, username="web", full_name="Web User"),
            Server(name="placeholder", host="127.0.0.1", location="VPN", is_active=True),
            Tariff(days=30, label="Базовый 5 устройств", price_rub=500, tariff_type=TariffType.VPN,
                   is_active=True, adapt_plan_uuid=plan_uuid),
        ])
        await session.commit()

    monkeypatch.setattr(webstore_bridge, "async_session", sessions)
    api = AsyncMock()
    api.enabled = True
    api.get_status = AsyncMock(return_value={
        "plan_uuid": plan_uuid, "end_date": expires.isoformat(), "devices": 5,
    })
    monkeypatch.setattr(webstore_bridge, "AdaptAPI", lambda: api)
    profile = {"orders": [{
        "order_id": "web-1", "status": "delivered", "provider": "adapt",
        "days": 30, "created_at": datetime.utcnow().isoformat(),
        "adapt_plan_uuid": plan_uuid, "access_expires_at": expires.isoformat(),
        "subscription_url": f"https://darimiru.ru/vpnbot/adapt-sub/{adapt_uuid}",
        "raw_subscription_url": f"https://darimiru.ru/vpnbot/adapt-sub/{adapt_uuid}",
    }]}

    assert await webstore_bridge.sync_linked_web_subscriptions(89154970647, profile)
    assert await webstore_bridge.sync_linked_web_subscriptions(89154970647, profile)

    async with sessions() as session:
        subscriptions = (await session.execute(select(Subscription))).scalars().all()
        records = (await session.execute(select(AdaptSubscription))).scalars().all()
        assert len(subscriptions) == len(records) == 1
        assert subscriptions[0].device_slots == 5
        assert records[0].adapt_uuid == adapt_uuid
        assert records[0].adapt_plan_uuid == plan_uuid

    await engine.dispose()
