from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.handlers import admin as admin_handler
from bot.models import Base, User, Subscription, SubStatus, Server, Platform
from bot.services.adapt_routing import is_adapt_subscription
from bot.services.vhq_routing import is_vhq_subscription

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
async def db_session(db_session_factory):
    async with db_session_factory() as session:
        yield session


def _make_callback(user_id: int, data: str):
    bot = SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(username="testbot")),
        send_message=AsyncMock(),
    )
    message = SimpleNamespace(
        edit_text=AsyncMock(),
        answer=AsyncMock(),
        answer_document=AsyncMock(),
        chat=SimpleNamespace(id=user_id),
    )
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id, first_name="Alice", username="alice"),
        data=data,
        bot=bot,
        message=message,
        answer=AsyncMock(),
    )


async def test_admin_stats_overview_categorization(monkeypatch, db_session, db_session_factory):
    # Setup test data
    server = Server(name="NL", host="127.0.0.1", location="NL", is_active=True)
    db_session.add(server)
    await db_session.commit()
    await db_session.refresh(server)

    user = User(telegram_id=12345, username="testadmin", full_name="Admin User")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    # Add active and expired subscriptions
    subs = [
        # Marzban paid
        Subscription(
            user_id=user.id,
            server_id=server.id,
            status=SubStatus.ACTIVE,
            billing_mode="tariff",
            client_name="botuser1_paid",
            vpn_key="key1",
            device_slots=1,
            tariff_months=1,
            tariff_days=0,
            platform=Platform.ANDROID,
            expires_at=datetime.utcnow(),
        ),
        # Marzban demo
        Subscription(
            user_id=user.id,
            server_id=server.id,
            status=SubStatus.ACTIVE,
            billing_mode="demo",
            client_name="botuser2_demo",
            vpn_key="key2",
            device_slots=1,
            tariff_months=0,
            tariff_days=3,
            platform=Platform.ANDROID,
            expires_at=datetime.utcnow(),
        ),
        # Adapt paid
        Subscription(
            user_id=user.id,
            server_id=server.id,
            status=SubStatus.ACTIVE,
            billing_mode="tariff",
            client_name="adapt_paid",
            vpn_key="key3",
            device_slots=1,
            tariff_months=1,
            tariff_days=0,
            platform=Platform.ANDROID,
            expires_at=datetime.utcnow(),
        ),
        # Adapt demo
        Subscription(
            user_id=user.id,
            server_id=server.id,
            status=SubStatus.ACTIVE,
            billing_mode="demo",
            client_name="adapt_demo",
            vpn_key="key4",
            device_slots=1,
            tariff_months=0,
            tariff_days=3,
            platform=Platform.ANDROID,
            expires_at=datetime.utcnow(),
        ),
        # VHQ paid
        Subscription(
            user_id=user.id,
            server_id=server.id,
            status=SubStatus.ACTIVE,
            billing_mode="tariff",
            client_name="vhq_paid",
            vpn_key="key5",
            device_slots=1,
            tariff_months=1,
            tariff_days=0,
            platform=Platform.ANDROID,
            expires_at=datetime.utcnow(),
        ),
        # VHQ demo
        Subscription(
            user_id=user.id,
            server_id=server.id,
            status=SubStatus.ACTIVE,
            billing_mode="demo",
            client_name="vhq_demo",
            vpn_key="key6",
            device_slots=1,
            tariff_months=0,
            tariff_days=3,
            platform=Platform.ANDROID,
            expires_at=datetime.utcnow(),
        ),
        # Expired
        Subscription(
            user_id=user.id,
            server_id=server.id,
            status=SubStatus.EXPIRED,
            billing_mode="tariff",
            client_name="expired_sub",
            vpn_key="key7",
            device_slots=1,
            tariff_months=1,
            tariff_days=0,
            platform=Platform.ANDROID,
            expires_at=datetime.utcnow(),
        ),
    ]
    db_session.add_all(subs)
    await db_session.commit()

    monkeypatch.setattr(admin_handler, "async_session", db_session_factory)
    monkeypatch.setattr(admin_handler, "_is_admin", lambda _uid: True)

    callback = _make_callback(12345, "adm_stats_overview")
    await admin_handler.admin_stats_overview(callback)

    callback.message.edit_text.assert_awaited_once()
    text = callback.message.edit_text.await_args.args[0]

    # Verify counts in stats text
    assert "Всего юзеров: <b>1</b>" in text
    assert "Активных подписок: <b>6</b>" in text
    assert "Marzban: <b>1</b> (демо: <b>1</b>)" in text
    assert "Adapt: <b>1</b> (демо: <b>1</b>)" in text
    assert "VHQ: <b>1</b> (демо: <b>1</b>)" in text
    assert "Истёкших: <b>1</b>" in text
