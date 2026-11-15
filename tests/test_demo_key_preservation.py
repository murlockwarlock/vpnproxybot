from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.handlers import buy as buy_handler
from bot.config import settings
from bot.models import AdaptSubscription, Base, Platform, Server, Subscription, SubStatus, Tariff, TariffType, User
from bot.services.purchase_intent import get_purchase_price_rub

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


async def test_purchase_targets_keeps_demo_subscription_without_provider_metadata(db_session_factory):
    async with db_session_factory() as session:
        user = User(telegram_id=200100, full_name="Demo User")
        server = Server(name="adapt-srv", host="127.0.0.1", location="Test")
        demo_tariff = Tariff(
            days=7,
            label="Демо",
            price_rub=45,
            tariff_type=TariffType.VPN,
            adapt_plan_uuid=settings.adapt_demo_plan_uuid,
            adapt_cost_price_usd=None,
        )
        session.add_all([user, server, demo_tariff])
        await session.flush()

        demo_sub = Subscription(
            user_id=user.id,
            server_id=server.id,
            tariff_id=demo_tariff.id,
            tariff_months=0,
            tariff_days=7,
            billing_mode="demo",
            status=SubStatus.EXPIRED,
            device_slots=1,
            vpn_key="https://example.test/demo-key",
            client_name="adapt_demo_12345",
            platform=Platform.ANDROID,
            expires_at=datetime.utcnow() - timedelta(hours=1),
        )
        session.add(demo_sub)
        await session.flush()
        session.add(AdaptSubscription(
            subscription_id=demo_sub.id,
            adapt_uuid="demo-adapt-uuid",
            adapt_plan_uuid=settings.adapt_demo_plan_uuid,
            end_date=demo_sub.expires_at,
        ))
        await session.commit()

    with (
        patch("bot.handlers.buy.async_session", db_session_factory),
        patch("bot.handlers.buy.settings.is_admin", return_value=False),
    ):
        targets = await buy_handler._purchase_targets(200100)
        has_active_paid = await buy_handler._has_active_paid_subscription(200100)

    assert [target.id for target in targets] == [demo_sub.id]
    assert targets[0].adapt_current_tariff.id == demo_tariff.id
    assert targets[0].adapt_sub.adapt_uuid == "demo-adapt-uuid"
    assert has_active_paid is False

    from bot.keyboards.client import purchase_subscription_kb

    markup = purchase_subscription_kb(
        demo_sub.id,
        position=0,
        total=1,
        show_upgrade=False,
        show_new=has_active_paid,
    )
    assert not any(
        button.callback_data == f"buy_new_{demo_sub.id}"
        for row in markup.inline_keyboard
        for button in row
    )


async def test_expired_paid_subscription_without_provider_metadata_stays_selectable(db_session_factory):
    async with db_session_factory() as session:
        user = User(telegram_id=200103, full_name="Expired Paid User")
        server = Server(name="adapt-srv", host="127.0.0.1", location="Test")
        tariff = Tariff(
            days=30,
            label="Платный тариф",
            price_rub=200,
            tariff_type=TariffType.VPN,
            adapt_plan_uuid="paid-plan-without-cost",
            adapt_cost_price_usd=None,
        )
        session.add_all([user, server, tariff])
        await session.flush()
        subscription = Subscription(
            user_id=user.id,
            server_id=server.id,
            tariff_id=tariff.id,
            tariff_months=1,
            tariff_days=30,
            billing_mode="tariff",
            status=SubStatus.EXPIRED,
            device_slots=1,
            vpn_key="https://example.test/paid-key",
            client_name="adapt_expired_paid",
            platform=Platform.ANDROID,
            expires_at=datetime.utcnow() - timedelta(hours=1),
        )
        session.add(subscription)
        await session.flush()
        session.add(AdaptSubscription(
            subscription_id=subscription.id,
            adapt_uuid="expired-paid-adapt-uuid",
            adapt_plan_uuid=tariff.adapt_plan_uuid,
            end_date=subscription.expires_at,
        ))
        await session.commit()

    with patch("bot.handlers.buy.async_session", db_session_factory):
        targets = await buy_handler._purchase_targets(user.telegram_id)
        has_active_paid = await buy_handler._has_active_paid_subscription(user.telegram_id)

    assert [target.id for target in targets] == [subscription.id]
    assert targets[0].adapt_sub.adapt_uuid == "expired-paid-adapt-uuid"
    assert has_active_paid is False

    from bot.keyboards.client import purchase_subscription_kb

    markup = purchase_subscription_kb(
        subscription.id,
        position=0,
        total=1,
        show_upgrade=False,
        show_new=has_active_paid,
    )
    assert not any(
        button.callback_data == f"buy_new_{subscription.id}"
        for row in markup.inline_keyboard
        for button in row
    )


async def test_active_paid_subscription_without_provider_metadata_keeps_explicit_new(db_session_factory):
    async with db_session_factory() as session:
        user = User(telegram_id=200104, full_name="Active Paid User")
        server = Server(name="adapt-srv", host="127.0.0.1", location="Test")
        tariff = Tariff(
            days=30,
            label="Платный тариф",
            price_rub=200,
            tariff_type=TariffType.VPN,
            adapt_plan_uuid="active-paid-plan-without-cost",
            adapt_cost_price_usd=None,
        )
        session.add_all([user, server, tariff])
        await session.flush()
        subscription = Subscription(
            user_id=user.id,
            server_id=server.id,
            tariff_id=tariff.id,
            tariff_months=1,
            tariff_days=30,
            billing_mode="tariff",
            status=SubStatus.ACTIVE,
            device_slots=1,
            vpn_key="https://example.test/paid-key",
            client_name="adapt_active_paid",
            platform=Platform.ANDROID,
            expires_at=datetime.utcnow() + timedelta(days=10),
        )
        session.add(subscription)
        await session.flush()
        session.add(AdaptSubscription(
            subscription_id=subscription.id,
            adapt_uuid="active-paid-adapt-uuid",
            adapt_plan_uuid=tariff.adapt_plan_uuid,
            end_date=subscription.expires_at,
        ))
        await session.commit()

    with patch("bot.handlers.buy.async_session", db_session_factory):
        targets = await buy_handler._purchase_targets(user.telegram_id)
        has_active_paid = await buy_handler._has_active_paid_subscription(user.telegram_id)

    assert [target.id for target in targets] == [subscription.id]
    assert targets[0].adapt_sub.adapt_uuid == "active-paid-adapt-uuid"
    assert has_active_paid is True

    from bot.keyboards.client import purchase_subscription_kb

    markup = purchase_subscription_kb(
        subscription.id,
        position=0,
        total=1,
        show_upgrade=False,
        show_new=has_active_paid,
    )
    assert any(
        button.callback_data == f"buy_new_{subscription.id}"
        for row in markup.inline_keyboard
        for button in row
    )


async def test_get_purchase_price_rub_allows_demo_subscription(db_session_factory):
    async with db_session_factory() as session:
        user = User(telegram_id=200101, full_name="Demo User 2")
        server = Server(name="adapt-srv", host="127.0.0.1", location="Test")
        paid_tariff = Tariff(
            days=30,
            label="30 дней",
            price_rub=125,
            price_stars=0,
            tariff_type=TariffType.VPN,
            is_active=True,
            adapt_plan_uuid="paid-plan-uuid",
            adapt_cost_price_usd=None,
        )
        demo_tariff = Tariff(
            days=3,
            label="Тестовый доступ",
            price_rub=0,
            price_stars=0,
            tariff_type=TariffType.VPN,
            is_active=True,
            adapt_plan_uuid=settings.adapt_demo_plan_uuid,
            adapt_cost_price_usd=None,
        )
        session.add_all([user, server, paid_tariff, demo_tariff])
        await session.flush()

        demo_sub = Subscription(
            user_id=user.id,
            server_id=server.id,
            tariff_id=demo_tariff.id,
            tariff_months=0,
            tariff_days=7,
            billing_mode="demo",
            status=SubStatus.EXPIRED,
            device_slots=1,
            vpn_key="https://example.test/demo-key",
            client_name="adapt_demo_67890",
            platform=Platform.ANDROID,
            expires_at=datetime.utcnow() - timedelta(hours=1),
        )
        session.add(demo_sub)
        await session.flush()
        adapt_record = AdaptSubscription(
            subscription_id=demo_sub.id,
            adapt_uuid="demo-adapt-uuid",
            adapt_plan_uuid=settings.adapt_demo_plan_uuid,
            end_date=demo_sub.expires_at,
        )
        session.add(adapt_record)
        await session.commit()

        price = await get_purchase_price_rub(
            session,
            user=user,
            tariff=paid_tariff,
            action="upgrade",
            target_subscription_id=demo_sub.id,
        )
        assert price == 125
        assert adapt_record.adapt_uuid == "demo-adapt-uuid"


async def test_get_purchase_price_rub_allows_paid_subscription_renewal(db_session_factory):
    async with db_session_factory() as session:
        user = User(telegram_id=200102, full_name="Paid User")
        server = Server(name="adapt-srv", host="127.0.0.1", location="Test")
        paid_tariff = Tariff(
            days=30,
            label="30 дней",
            price_rub=125,
            price_stars=0,
            tariff_type=TariffType.VPN,
            is_active=True,
            adapt_plan_uuid="paid-plan-uuid",
            adapt_cost_price_usd=None,
        )
        session.add_all([user, server, paid_tariff])
        await session.flush()

        paid_sub = Subscription(
            user_id=user.id,
            server_id=server.id,
            tariff_id=paid_tariff.id,
            tariff_months=1,
            tariff_days=30,
            billing_mode="tariff",
            status=SubStatus.ACTIVE,
            device_slots=1,
            vpn_key="https://example.test/paid-key",
            client_name="adapt_paid_12345",
            platform=Platform.ANDROID,
            expires_at=datetime.utcnow() + timedelta(days=5),
        )
        session.add(paid_sub)
        await session.commit()

        price = await get_purchase_price_rub(
            session,
            user=user,
            tariff=paid_tariff,
            action="renew",
            target_subscription_id=paid_sub.id,
        )
        assert price == 125
