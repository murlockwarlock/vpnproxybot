from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.handlers import admin as admin_handler
from bot.models import Base, User, Subscription, SubStatus, Server, Platform, Payment, PaymentMethod, PaymentStatus
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
    assert "Пользователи:</b> <b>1</b>" in text
    assert "Активные доступы: 6" in text
    assert "Marzban: <b>2</b> (из них демо: <b>1</b>)" in text
    assert "Adapt: <b>2</b> (из них демо: <b>1</b>)" in text
    assert "VHQ: <b>2</b> (из них демо: <b>1</b>)" in text
    assert "Истёкших: 1" in text


async def test_admin_stats_unified_with_webstore(monkeypatch, db_session, db_session_factory):
    server = Server(name="NL", host="127.0.0.1", location="NL", is_active=True)
    db_session.add(server)
    user = User(telegram_id=12345, username="testadmin", full_name="Admin User")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    await db_session.refresh(server)

    sub = Subscription(
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
    )
    db_session.add(sub)
    await db_session.commit()

    monkeypatch.setattr(admin_handler, "async_session", db_session_factory)
    monkeypatch.setattr(admin_handler, "_is_admin", lambda _uid: True)

    mock_web_data = {
        "revenue": {"today": 100, "w7": 1200, "w30": 5000, "all": 11000},
        "status_counts": {"pending": 0, "delivered": 67, "canceled": 65, "demo": 65},
        "total_profiles": 112,
        "referrals": {"orders": 28, "credited_rub": 450},
        "conversion": {"total_30d": 45, "paid_30d": 21, "demo_total": 65, "demo_converted": 9},
        "active": {"paid": 20, "demo": 2, "expired": 43},
        "churn": {"ever_paid": 28, "still_active": 20, "churned": 8},
    }
    monkeypatch.setattr(admin_handler, "_fetch_webstore_stats_dict", AsyncMock(return_value=mock_web_data))

    callback = _make_callback(12345, "adm_stats_overview")
    await admin_handler.admin_stats_overview(callback)

    callback.message.edit_text.assert_awaited_once()
    text = callback.message.edit_text.await_args.args[0]

    assert "Статистика сервиса" in text
    assert "Клиенты (всего: 113)" in text
    assert "Telegram: <b>1</b>" in text
    assert "Веб-сайт: <b>112</b>" in text
    assert "Активные доступы: 23</b> (ТГ: <b>1</b> · Сайт: <b>22</b>)" in text
    assert "В Telegram: <b>1</b> (платных: <b>1</b> · демо: <b>0</b>)" in text
    assert "На Сайте: <b>22</b> (платных: <b>20</b> · демо: <b>2</b>)" in text
    assert "Истёкших: 43</b> (ТГ: <b>0</b> · Сайт: <b>43</b>)" in text
    assert "Успешных оплат: <b>67</b> (ТГ: <b>0</b> · Сайт: <b>67</b>)" in text
    assert "За всё время: <b>11,000 ₽</b> (Сайт: 11,000 ₽)" in text
    assert "Демо → Покупка: 13.8%" in text
    assert "Отток клиентов: 28.6%" in text


async def test_admin_stats_revenue_unified(monkeypatch, db_session, db_session_factory):
    monkeypatch.setattr(admin_handler, "async_session", db_session_factory)
    monkeypatch.setattr(admin_handler, "_is_admin", lambda _uid: True)

    mock_web_data = {
        "revenue": {"today": 250, "w7": 1500, "w30": 6000, "all": 12000},
        "status_counts": {"delivered": 40},
    }
    monkeypatch.setattr(admin_handler, "_fetch_webstore_stats_dict", AsyncMock(return_value=mock_web_data))

    callback = _make_callback(12345, "adm_stats_revenue")
    await admin_handler.admin_stats_revenue(callback)

    callback.message.edit_text.assert_awaited_once()
    text = callback.message.edit_text.await_args.args[0]

    assert "Выручка" in text
    assert "250 ₽" in text
    assert "1,500 ₽" in text
    assert "6,000 ₽" in text
    assert "12,000 ₽" in text
    assert "Оплат за всё время:</b> <b>40</b>" in text


async def test_admin_stats_funnel_and_churn_combined(monkeypatch, db_session, db_session_factory):
    server = Server(name="NL2", host="127.0.0.2", location="NL", is_active=True)
    db_session.add(server)

    # User 1: Took demo, then paid, currently active
    u1 = User(telegram_id=101, username="user1", full_name="User One")
    # User 2: Took demo, did not pay
    u2 = User(telegram_id=102, username="user2", full_name="User Two")
    # User 3: Paid directly, expired (churned)
    u3 = User(telegram_id=103, username="user3", full_name="User Three")
    db_session.add_all([u1, u2, u3])
    await db_session.commit()
    for u in [u1, u2, u3]:
        await db_session.refresh(u)
    await db_session.refresh(server)

    subs = [
        # u1 demo sub
        Subscription(
            user_id=u1.id, server_id=server.id, status=SubStatus.EXPIRED, billing_mode="demo",
            client_name="u1_demo", vpn_key="k1", device_slots=1, tariff_months=0, tariff_days=3,
            platform=Platform.ANDROID, expires_at=datetime.utcnow() - timedelta(days=10),
        ),
        # u1 active paid sub
        Subscription(
            user_id=u1.id, server_id=server.id, status=SubStatus.ACTIVE, billing_mode="tariff",
            client_name="u1_paid", vpn_key="k2", device_slots=1, tariff_months=1, tariff_days=0,
            platform=Platform.ANDROID, expires_at=datetime.utcnow() + timedelta(days=20),
        ),
        # u2 demo sub (expired, no paid sub)
        Subscription(
            user_id=u2.id, server_id=server.id, status=SubStatus.EXPIRED, billing_mode="demo",
            client_name="u2_demo", vpn_key="k3", device_slots=1, tariff_months=0, tariff_days=3,
            platform=Platform.ANDROID, expires_at=datetime.utcnow() - timedelta(days=5),
        ),
        # u3 expired paid sub
        Subscription(
            user_id=u3.id, server_id=server.id, status=SubStatus.EXPIRED, billing_mode="tariff",
            client_name="u3_paid", vpn_key="k4", device_slots=1, tariff_months=1, tariff_days=0,
            platform=Platform.ANDROID, expires_at=datetime.utcnow() - timedelta(days=2),
        ),
    ]
    db_session.add_all(subs)

    payments = [
        Payment(user_id=u1.id, amount=30000, currency="RUB", method=PaymentMethod.YOOKASSA, status=PaymentStatus.COMPLETED),
        Payment(user_id=u3.id, amount=30000, currency="RUB", method=PaymentMethod.YOOKASSA, status=PaymentStatus.COMPLETED),
    ]
    db_session.add_all(payments)
    await db_session.commit()

    monkeypatch.setattr(admin_handler, "async_session", db_session_factory)
    monkeypatch.setattr(admin_handler, "_is_admin", lambda _uid: True)

    mock_web_data = {
        "revenue": {"today": 0, "w7": 1000, "w30": 3000, "all": 5000},
        "status_counts": {"delivered": 20, "demo": 10},
        "total_profiles": 50,
        "referrals": {"orders": 5, "credited_rub": 100},
        "conversion": {"total_30d": 15, "paid_30d": 10, "demo_total": 10, "demo_converted": 4},
        "active": {"paid": 15, "demo": 2, "expired": 10},
        "churn": {"ever_paid": 20, "still_active": 15, "churned": 5},
    }
    monkeypatch.setattr(admin_handler, "_fetch_webstore_stats_dict", AsyncMock(return_value=mock_web_data))

    callback = _make_callback(12345, "adm_stats_overview")
    await admin_handler.admin_stats_overview(callback)

    callback.message.edit_text.assert_awaited_once()
    text = callback.message.edit_text.await_args.args[0]

    # Total clients: 3 in bot + 50 web = 53
    assert "Клиенты (всего: 53)" in text
    # Active accesses: 1 in bot + 17 web (15 paid + 2 demo) = 18
    assert "Активные доступы: 18</b> (ТГ: <b>1</b> · Сайт: <b>17</b>)" in text
    assert "В Telegram: <b>1</b> (платных: <b>1</b> · демо: <b>0</b>)" in text
    assert "На Сайте: <b>17</b> (платных: <b>15</b> · демо: <b>2</b>)" in text
    # Expired: 3 in bot + 10 web = 13
    assert "Истёкших: 13</b> (ТГ: <b>3</b> · Сайт: <b>10</b>)" in text
    # Revenue: 600 in bot + 5000 in web = 5600
    assert "5,600 ₽</b> (ТГ: 600 ₽ · Сайт: 5,000 ₽)" in text
    # Demo conversion: 1 bot + 4 web = 5 out of 12 (2 bot + 10 web) = 41.7%
    assert "Демо → Покупка: 41.7%</b> (5 из 12)" in text
    assert "В боте: <b>50.0%</b> (1 из 2)" in text
    assert "На сайте: <b>40.0%</b> (4 из 10)" in text
    # Churn: 1 bot + 5 web = 6 out of 22 (2 bot + 20 web) = 27.3%
    assert "Отток клиентов: 27.3%</b> (6 не продлили из 22)" in text
    assert "В боте: <b>50.0%</b> (1 из 2)" in text
    assert "На сайте: <b>25.0%</b> (5 из 20)" in text
