from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.handlers import admin as admin_handler
from bot.models import AdaptSubscription, Base, Platform, Server, SubStatus, Subscription, Tariff, User
from webstore.models import WebOrder
from webstore.routes import _sync_bot_db_subscription_on_fulfillment


def _make_callback(data: str, user_id: int = 9001):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id, first_name="Admin", username="admin"),
        message=SimpleNamespace(
            edit_text=AsyncMock(),
            answer=AsyncMock(),
        ),
        answer=AsyncMock(),
        bot=SimpleNamespace(send_message=AsyncMock()),
    )


@pytest_asyncio.fixture
async def db_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.mark.asyncio
async def test_admin_user_subs_self_heals_stale_expiration_date_from_adapt_api(
    db_session_factory, monkeypatch
):
    """When DB has stale/expired date (e.g. 17.09) but Adapt API has renewed date (e.g. 18.10),

    admin_user_subs must self-heal the DB record and display the true Adapt expiration date.
    """
    adapt_uuid = "3093bb7e-632d-4ea9-8ffa-afbd0a6a85a9"
    stale_date = datetime(2026, 9, 17, 14, 28, 37)
    live_adapt_end_date = datetime(2026, 10, 18, 14, 28, 37)

    async with db_session_factory() as session:
        user = User(telegram_id=124958038, full_name="Дмитрий Чураков", platform=Platform.ANDROID)
        server = Server(name="Test Srv", host="127.0.0.1", api_url="https://vpn.test", location="Test", country_emoji="🇳🇱", is_active=True)
        tariff = Tariff(
            label="Базовый • 30 дн • 3 📱 • 40 Гб ⚡️",
            price_rub=155,
            days=30,
            device_count=3,
            is_active=True,
            adapt_plan_uuid="303c4b1c-ecfc-4346-b6f8-31c0cad27646",
        )
        session.add_all([user, server, tariff])
        await session.flush()

        sub = Subscription(
            user_id=user.id,
            server_id=server.id,
            tariff_id=tariff.id,
            tariff_days=30,
            tariff_months=1,
            device_slots=3,
            status=SubStatus.EXPIRED,  # Stale status in local DB
            vpn_key=f"https://darimiru.ru/vpnbot/adapt-sub/{adapt_uuid}",
            client_name=f"adapt_{adapt_uuid}",
            platform=Platform.ANDROID,
            started_at=datetime(2026, 8, 17, 14, 28, 37),
            expires_at=stale_date,  # Stale date in local DB
            billing_mode="tariff",
        )
        session.add(sub)
        await session.flush()

        adapt_rec = AdaptSubscription(
            subscription_id=sub.id,
            adapt_uuid=adapt_uuid,
            adapt_plan_uuid=tariff.adapt_plan_uuid,
            end_date=stale_date,
            traffic_limit_bytes=43271795507,
        )
        session.add(adapt_rec)
        await session.commit()
        sub_id = sub.id

    monkeypatch.setattr(admin_handler, "async_session", db_session_factory)
    monkeypatch.setattr(admin_handler, "_is_admin", lambda uid: True)

    mock_devices = [{"device_id": 1}, {"device_id": 2}]
    mock_status = {
        "end_date": live_adapt_end_date.isoformat(),
        "used_traffic_bytes": 1428000000,
        "traffic_limit_bytes": 86543591014,  # ~81 GB
        "devices": 2,
    }

    mock_adapt_api = SimpleNamespace(
        get_devices=AsyncMock(return_value=mock_devices),
        get_status=AsyncMock(return_value=mock_status),
    )
    monkeypatch.setattr("bot.services.adapt_api.AdaptAPI", lambda: mock_adapt_api)

    callback = _make_callback(f"adm_usr_subs_124958038_1")
    await admin_handler.admin_user_subs(callback)

    # 1. Verify card text rendered to admin contains the LIVE Adapt date (18-10-2026) and ACTIVE status
    edit_call = callback.message.edit_text.await_args
    card_text = edit_call.args[0]
    assert "18-10-2026" in card_text
    assert "17-09-2026" not in card_text
    assert "🟢 активна" in card_text
    assert "⚡️Трафик: <b>1.33</b> из <b>81</b> Гб" in card_text

    # 2. Verify database was self-healed
    async with db_session_factory() as session:
        updated_sub = await session.get(Subscription, sub_id)
        assert updated_sub.expires_at == live_adapt_end_date
        assert updated_sub.status == SubStatus.ACTIVE

        updated_adapt = await session.scalar(
            select(AdaptSubscription).where(AdaptSubscription.subscription_id == sub_id)
        )
        assert updated_adapt.end_date == live_adapt_end_date
        assert updated_adapt.traffic_limit_bytes == 86543591014


def test_sync_bot_db_subscription_on_fulfillment(tmp_path, monkeypatch):
    """When webstore fulfills a renewal for an Adapt key,

    it must directly update the bot database subscription expiration date and status.
    """
    bot_db_file = tmp_path / "darimiru_bot.db"
    with sqlite3.connect(str(bot_db_file)) as con:
        con.execute(
            """
            CREATE TABLE subscriptions (
                id INTEGER PRIMARY KEY,
                user_id INTEGER,
                server_id INTEGER,
                tariff_months INTEGER,
                status VARCHAR(9),
                device_slots INTEGER,
                vpn_key TEXT,
                client_name VARCHAR(64),
                platform VARCHAR(10),
                started_at DATETIME,
                expires_at DATETIME,
                billing_mode VARCHAR(16),
                tariff_days INTEGER,
                tariff_id INTEGER
            )
            """
        )
        con.execute(
            """
            CREATE TABLE adapt_subscriptions (
                id INTEGER PRIMARY KEY,
                subscription_id INTEGER,
                adapt_uuid VARCHAR(64),
                adapt_plan_uuid VARCHAR(64),
                is_frozen INTEGER,
                frozen_at DATETIME,
                end_date DATETIME,
                traffic_limit_bytes INTEGER,
                created_at DATETIME
            )
            """
        )
        # Seed stale subscription (expired 17-09-2026)
        con.execute(
            """
            INSERT INTO subscriptions (id, user_id, server_id, tariff_months, status, device_slots, vpn_key, client_name, platform, started_at, expires_at, billing_mode, tariff_days, tariff_id)
            VALUES (273, 184, 1, 1, 'EXPIRED', 3, 'https://darimiru.ru/vpnbot/adapt-sub/3093bb7e-632d-4ea9-8ffa-afbd0a6a85a9', 'adapt_3093bb7e-632d-4ea9-8ffa-afbd0a6a85a9', 'ANDROID', '2026-08-17 14:28:37', '2026-09-17 14:28:37.000000', 'tariff', 30, 12)
            """
        )
        con.execute(
            """
            INSERT INTO adapt_subscriptions (id, subscription_id, adapt_uuid, adapt_plan_uuid, is_frozen, end_date, traffic_limit_bytes)
            VALUES (95, 273, '3093bb7e-632d-4ea9-8ffa-afbd0a6a85a9', '303c4b1c-ecfc-4346-b6f8-31c0cad27646', 0, '2026-09-17 14:28:37.000000', 43271795507)
            """
        )
        con.commit()

    monkeypatch.setattr("webstore.config._bot_db_path", lambda: bot_db_file)

    new_end_date = datetime(2026, 10, 18, 14, 28, 37)
    order = SimpleNamespace(
        order_id="web_order_123",
        tariff_key="basic_30_12",
        tariff_label="Базовый 30 дн",
        days=30,
        amount_rub=155,
        status="delivered",
        marzban_username="adapt_3093bb7e-632d-4ea9-8ffa-afbd0a6a85a9",
        subscription_url="https://darimiru.ru/vpnbot/adapt-sub/3093bb7e-632d-4ea9-8ffa-afbd0a6a85a9",
        target_order_id="uuid:3093bb7e-632d-4ea9-8ffa-afbd0a6a85a9",
        purchase_action="renew",
        access_expires_at=new_end_date,
    )

    _sync_bot_db_subscription_on_fulfillment(order)

    # Verify bot DB updated immediately
    with sqlite3.connect(str(bot_db_file)) as con:
        con.row_factory = sqlite3.Row
        sub = con.execute("SELECT * FROM subscriptions WHERE id = 273").fetchone()
        assert sub["status"] == "ACTIVE"
        assert "2026-10-18 14:28:37" in sub["expires_at"]

        adapt_sub = con.execute("SELECT * FROM adapt_subscriptions WHERE subscription_id = 273").fetchone()
        assert "2026-10-18 14:28:37" in adapt_sub["end_date"]
