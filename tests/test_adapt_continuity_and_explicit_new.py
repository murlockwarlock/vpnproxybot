from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.models import (
    Base as BotBase,
    AdaptSubscription,
    Payment,
    PaymentMethod,
    PaymentStatus,
    Platform,
    Server,
    Subscription,
    SubStatus,
    Tariff,
    User,
)
from bot.config import settings
from bot.services import subscription_service, webstore_bridge
from bot.services.purchase_intent import get_purchase_price_rub
from bot.services.subscription_semantics import (
    is_adapt_trial_subscription,
    is_adapt_trial_tariff,
    is_adapt_customer_upgrade_eligible,
)
from bot.services.provisioning_issues import AccessProvisionError
from bot.handlers import buy as buy_handler
from webstore import routes as webstore_routes
from webstore.models import Base as WebBase, WebOrder

pytestmark = pytest.mark.asyncio


class _Request(SimpleNamespace):
    def __init__(self, _json_body=None, cookies=None, query=None, headers=None, remote="127.0.0.1"):
        super().__init__(
            _json_body=_json_body or {},
            cookies=cookies or {},
            query=query or {},
            headers=headers or {},
            remote=remote,
        )

    async def json(self):
        return self._json_body


class _FakeYooKassaPaymentApi:
    last_params = None
    last_idempotence_key = None

    @classmethod
    def create(cls, params, idempotence_key):
        cls.last_params = params
        cls.last_idempotence_key = idempotence_key
        return SimpleNamespace(
            id="yk_adapt_test",
            confirmation=SimpleNamespace(confirmation_url="https://pay.test/redirect"),
        )


class _FakeYooKassa(SimpleNamespace):
    Configuration = SimpleNamespace(account_id=None, secret_key=None)
    Payment = _FakeYooKassaPaymentApi


@pytest_asyncio.fixture
async def bot_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(BotBase.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(BotBase.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def bot_session_factory(bot_engine):
    return async_sessionmaker(bot_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def web_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(WebBase.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(WebBase.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def web_session_factory(web_engine):
    return async_sessionmaker(web_engine, class_=AsyncSession, expire_on_commit=False)


# ==============================================================================
# Telegram Flow Continuity & Explicit-New Tests
# ==============================================================================


async def test_telegram_single_demo_flow_is_one_step_to_paid_tariffs(bot_session_factory, monkeypatch):
    demo_uuid = "11111111-aaaa-bbbb-cccc-000000000000"
    monkeypatch.setattr(buy_handler, "async_session", bot_session_factory)

    async with bot_session_factory() as session:
        user = User(telegram_id=2000, username="singledemo", full_name="Single Demo")
        server = Server(id=1, name="S1", host="1.1.1.1", location="Frankfurt", is_active=True)
        paid_tariff = Tariff(id=10, label="Basic 30d", price_rub=200, days=30, is_active=True, adapt_plan_uuid="adapt-plan-paid-30d", adapt_cost_price_usd=4.0)
        demo_tariff = Tariff(id=9, label="Тестовый доступ", price_rub=0, days=3, is_active=True, adapt_plan_uuid=settings.adapt_demo_plan_uuid, adapt_cost_price_usd=0.30)
        session.add_all([user, server, paid_tariff, demo_tariff])
        await session.flush()

        demo_sub = Subscription(
            id=500,
            user_id=user.id,
            server_id=server.id,
            tariff_months=0,
            tariff_days=3,
            tariff_id=demo_tariff.id,
            billing_mode="demo",
            client_name=f"adapt_{demo_uuid}",
            vpn_key=f"https://darimiru.ru/vpnbot/adapt-sub/{demo_uuid}",
            platform=Platform.IOS,
            status=SubStatus.ACTIVE,
            expires_at=datetime.utcnow() + timedelta(days=2),
        )
        session.add(demo_sub)
        await session.flush()

        adapt_sub = AdaptSubscription(
            subscription_id=demo_sub.id,
            adapt_uuid=demo_uuid,
            adapt_plan_uuid=settings.adapt_demo_plan_uuid,
            end_date=demo_sub.expires_at,
        )
        session.add(adapt_sub)
        await session.commit()

    edit_text_mock = AsyncMock()
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=2000),
        data="buy",
        message=SimpleNamespace(edit_text=edit_text_mock),
        answer=AsyncMock(),
    )

    await buy_handler.start_purchase(callback)

    edit_text_mock.assert_called_once()
    args, kwargs = edit_text_mock.call_args
    assert "Выберите тариф" in args[0]
    markup = kwargs["reply_markup"]
    buttons = [b for row in markup.inline_keyboard for b in row]
    assert any("~u~500" in (b.callback_data or "") for b in buttons)
    assert not any("buy_new" in (b.callback_data or "") for b in buttons)


async def test_expired_paid_telegram_shows_renew_and_upgrade(bot_session_factory, monkeypatch):
    monkeypatch.setattr(buy_handler, "async_session", bot_session_factory)
    mock_plans = [
        {"uuid": "expired-paid-plan", "days": 14, "price_usd": "1.00", "devices": 1, "is_trial": False, "is_active": True},
        {"uuid": "expired-paid-target-plan", "days": 30, "price_usd": "3.00", "devices": 1, "is_trial": False, "is_active": True},
    ]
    monkeypatch.setattr("bot.services.adapt_api.AdaptAPI.list_plans", AsyncMock(return_value=mock_plans))

    async with bot_session_factory() as session:
        user = User(id=610, telegram_id=610001, username="expired_paid")
        server = Server(id=610, name="Expired paid server", host="1.1.1.1", location="Frankfurt")
        current_tariff = Tariff(
            id=610,
            label="Базовый 14 дней",
            days=14,
            price_rub=75,
            is_active=True,
            adapt_plan_uuid="expired-paid-plan",
            adapt_cost_price_usd=None,
        )
        target_tariff = Tariff(
            id=611,
            label="Базовый 30 дней",
            days=30,
            price_rub=200,
            is_active=True,
            adapt_plan_uuid="expired-paid-target-plan",
            adapt_cost_price_usd=None,
        )
        sub = Subscription(
            id=610,
            user_id=user.id,
            server_id=server.id,
            tariff_id=current_tariff.id,
            tariff_months=0,
            tariff_days=current_tariff.days,
            billing_mode="tariff",
            status=SubStatus.EXPIRED,
            vpn_key="https://darimiru.ru/vpnbot/adapt-sub/expired-paid-uuid",
            client_name="adapt_expired_paid",
            platform=Platform.ANDROID,
            expires_at=datetime.utcnow() - timedelta(days=14),
        )
        adapt_sub = AdaptSubscription(
            subscription_id=sub.id,
            adapt_uuid="expired-paid-uuid",
            adapt_plan_uuid=current_tariff.adapt_plan_uuid,
            end_date=sub.expires_at,
        )
        session.add_all([user, server, current_tariff, target_tariff, sub, adapt_sub])
        await session.commit()

    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=user.telegram_id),
        data="buy",
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )
    await buy_handler.start_purchase(callback)

    buttons = [
        button
        for row in callback.message.edit_text.call_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    button_texts = {button.text for button in buttons}
    assert "↻ Продлить" in button_texts
    assert "↑ Улучшить" in button_texts
    assert "➕ Создать новую" in button_texts

    upgrade_callback = SimpleNamespace(
        from_user=SimpleNamespace(id=user.telegram_id),
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )
    await buy_handler._open_upgrade_target(upgrade_callback, sub.id)
    upgrade_buttons = [
        button
        for row in upgrade_callback.message.edit_text.call_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert any(
        button.callback_data == f"tariff_{target_tariff.id}~u~{sub.id}"
        for button in upgrade_buttons
    )


async def test_telegram_active_demo_no_new_upgrades_same_uuid(bot_session_factory, monkeypatch):
    demo_uuid = "11111111-aaaa-bbbb-cccc-000000000001"
    async with bot_session_factory() as session:
        user = User(telegram_id=1001, username="demo_user", full_name="Demo User")
        server = Server(id=1, name="S1", host="1.1.1.1", location="Frankfurt", is_active=True)
        paid_tariff = Tariff(id=10, label="Basic 30d", price_rub=200, days=30, adapt_plan_uuid="adapt-plan-paid-30d", adapt_cost_price_usd=4.0)
        demo_tariff = Tariff(id=9, label="Тестовый доступ", price_rub=0, days=3, is_active=True, adapt_plan_uuid=settings.adapt_demo_plan_uuid, adapt_cost_price_usd=0.30)
        session.add_all([user, server, paid_tariff, demo_tariff])
        await session.flush()

        demo_sub = Subscription(
            id=501,
            user_id=user.id,
            server_id=server.id,
            tariff_months=0,
            tariff_days=3,
            tariff_id=demo_tariff.id,
            billing_mode="demo",
            client_name=f"adapt_{demo_uuid}",
            vpn_key=f"https://darimiru.ru/vpnbot/adapt-sub/{demo_uuid}",
            platform=Platform.IOS,
            status=SubStatus.ACTIVE,
            expires_at=datetime.utcnow() + timedelta(days=2),
        )
        session.add(demo_sub)
        await session.flush()

        adapt_sub = AdaptSubscription(
            subscription_id=demo_sub.id,
            adapt_uuid=demo_uuid,
            adapt_plan_uuid=settings.adapt_demo_plan_uuid,
            end_date=demo_sub.expires_at,
        )
        session.add(adapt_sub)
        await session.commit()

        mock_upgrade = AsyncMock(return_value={"uuid": demo_uuid, "end_date": (datetime.utcnow() + timedelta(days=30)).isoformat()})
        mock_get_status = AsyncMock(side_effect=[
            {"uuid": demo_uuid, "plan_uuid": settings.adapt_demo_plan_uuid, "devices": 1, "is_active": True, "end_date": (datetime.utcnow() + timedelta(days=2)).isoformat()},
            {"uuid": demo_uuid, "plan_uuid": "adapt-plan-paid-30d", "devices": 1, "is_active": True, "end_date": (datetime.utcnow() + timedelta(days=30)).isoformat()},
        ])
        mock_list_plans = AsyncMock(return_value=[{"uuid": "adapt-plan-paid-30d", "devices": 1, "is_active": True}])
        mock_create = AsyncMock()

        monkeypatch.setattr("bot.services.subscription_service.AdaptAPI.upgrade_subscription", mock_upgrade)
        monkeypatch.setattr("bot.services.subscription_service.AdaptAPI.get_status", mock_get_status)
        monkeypatch.setattr("bot.services.subscription_service.AdaptAPI.list_plans", mock_list_plans)
        monkeypatch.setattr("bot.services.subscription_service.AdaptAPI.create_subscription", mock_create)

        res_sub, res_url = await subscription_service.create_or_extend_paid_access(
            session,
            user=user,
            tariff=paid_tariff,
            platform=Platform.IOS,
            purchase_action="new",
        )

        mock_create.assert_not_called()
        mock_upgrade.assert_called_once_with(demo_uuid, "adapt-plan-paid-30d")
        assert res_sub.id == demo_sub.id
        assert demo_uuid in res_url


async def test_telegram_expired_demo_no_new_reactivates_and_upgrades_same_uuid(bot_session_factory, monkeypatch):
    demo_uuid = "22222222-aaaa-bbbb-cccc-000000000002"
    async with bot_session_factory() as session:
        user = User(telegram_id=1002, username="exp_demo_user", full_name="Exp Demo User")
        server = Server(id=1, name="S1", host="1.1.1.1", location="Frankfurt", is_active=True)
        paid_tariff = Tariff(id=11, label="Pro 30d", price_rub=400, days=30, adapt_plan_uuid="adapt-plan-pro-30d", adapt_cost_price_usd=4.0)
        demo_tariff = Tariff(id=9, label="Тестовый доступ", price_rub=0, days=3, is_active=True, adapt_plan_uuid=settings.adapt_demo_plan_uuid, adapt_cost_price_usd=0.30)
        session.add_all([user, server, paid_tariff, demo_tariff])
        await session.flush()

        demo_sub = Subscription(
            id=502,
            user_id=user.id,
            server_id=server.id,
            tariff_months=0,
            tariff_days=3,
            tariff_id=demo_tariff.id,
            billing_mode="demo",
            client_name=f"adapt_{demo_uuid}",
            vpn_key=f"https://darimiru.ru/vpnbot/adapt-sub/{demo_uuid}",
            platform=Platform.ANDROID,
            status=SubStatus.EXPIRED,
            expires_at=datetime.utcnow() - timedelta(days=1),
        )
        session.add(demo_sub)
        await session.flush()

        adapt_sub = AdaptSubscription(
            subscription_id=demo_sub.id,
            adapt_uuid=demo_uuid,
            adapt_plan_uuid=settings.adapt_demo_plan_uuid,
            end_date=demo_sub.expires_at,
        )
        session.add(adapt_sub)
        await session.commit()

        mock_renew_custom = AsyncMock(return_value={"uuid": demo_uuid})
        mock_upgrade = AsyncMock(return_value={"uuid": demo_uuid})
        mock_get_status = AsyncMock(side_effect=[
            {"uuid": demo_uuid, "plan_uuid": settings.adapt_demo_plan_uuid, "devices": 1, "is_active": False, "end_date": (datetime.utcnow() - timedelta(days=1)).isoformat()},
            {"uuid": demo_uuid, "plan_uuid": settings.adapt_demo_plan_uuid, "devices": 1, "is_active": True, "end_date": (datetime.utcnow() + timedelta(days=1)).isoformat()},
            {"uuid": demo_uuid, "plan_uuid": "adapt-plan-pro-30d", "devices": 3, "is_active": True, "end_date": (datetime.utcnow() + timedelta(days=30)).isoformat()},
            {"uuid": demo_uuid, "plan_uuid": "adapt-plan-pro-30d", "devices": 3, "is_active": True, "end_date": (datetime.utcnow() + timedelta(days=30)).isoformat()},
        ])
        mock_list_plans = AsyncMock(return_value=[{"uuid": "adapt-plan-pro-30d", "devices": 3, "is_active": True}])
        mock_create = AsyncMock()

        monkeypatch.setattr("bot.services.subscription_service.AdaptAPI.renew_subscription_custom", mock_renew_custom)
        monkeypatch.setattr("bot.services.subscription_service.AdaptAPI.upgrade_subscription", mock_upgrade)
        monkeypatch.setattr("bot.services.subscription_service.AdaptAPI.get_status", mock_get_status)
        monkeypatch.setattr("bot.services.subscription_service.AdaptAPI.list_plans", mock_list_plans)
        monkeypatch.setattr("bot.services.subscription_service.AdaptAPI.create_subscription", mock_create)

        res_sub, res_url = await subscription_service.create_or_extend_paid_access(
            session,
            user=user,
            tariff=paid_tariff,
            platform=Platform.ANDROID,
            purchase_action="new",
        )

        mock_create.assert_not_called()
        mock_renew_custom.assert_called_once()
        mock_upgrade.assert_called_once_with(demo_uuid, "adapt-plan-pro-30d")
        assert res_sub.id == demo_sub.id
        assert demo_uuid in res_url


async def test_telegram_expired_paid_no_new_renews_same_uuid(bot_session_factory, monkeypatch):
    paid_uuid = "33333333-aaaa-bbbb-cccc-000000000003"
    async with bot_session_factory() as session:
        user = User(telegram_id=1003, username="exp_paid_user", full_name="Exp Paid User")
        server = Server(id=1, name="S1", host="1.1.1.1", location="Frankfurt", is_active=True)
        paid_tariff = Tariff(id=12, label="Basic 30d", price_rub=200, days=30, adapt_plan_uuid="adapt-plan-basic-30d")
        session.add_all([user, server, paid_tariff])
        await session.flush()

        paid_sub = Subscription(
            id=503,
            user_id=user.id,
            server_id=server.id,
            tariff_id=paid_tariff.id,
            tariff_months=1,
            tariff_days=30,
            billing_mode="tariff",
            client_name=f"adapt_{paid_uuid}",
            vpn_key=f"https://darimiru.ru/vpnbot/adapt-sub/{paid_uuid}",
            platform=Platform.WINDOWS,
            status=SubStatus.EXPIRED,
            expires_at=datetime.utcnow() - timedelta(days=5),
        )
        session.add(paid_sub)
        await session.flush()

        adapt_sub = AdaptSubscription(
            subscription_id=paid_sub.id,
            adapt_uuid=paid_uuid,
            adapt_plan_uuid="adapt-plan-basic-30d",
            end_date=paid_sub.expires_at,
        )
        session.add(adapt_sub)
        await session.commit()

        mock_renew = AsyncMock(return_value={"uuid": paid_uuid, "end_date": (datetime.utcnow() + timedelta(days=30)).isoformat()})
        mock_get_status = AsyncMock(return_value={"uuid": paid_uuid, "plan_uuid": "adapt-plan-basic-30d", "devices": 1, "is_active": True, "end_date": (datetime.utcnow() + timedelta(days=30)).isoformat()})
        mock_list_plans = AsyncMock(return_value=[{"uuid": "adapt-plan-basic-30d", "devices": 1, "is_active": True}])
        mock_create = AsyncMock()

        monkeypatch.setattr("bot.services.subscription_service.AdaptAPI.renew_subscription", mock_renew)
        monkeypatch.setattr("bot.services.subscription_service.AdaptAPI.get_status", mock_get_status)
        monkeypatch.setattr("bot.services.subscription_service.AdaptAPI.list_plans", mock_list_plans)
        monkeypatch.setattr("bot.services.subscription_service.AdaptAPI.create_subscription", mock_create)

        res_sub, res_url = await subscription_service.create_or_extend_paid_access(
            session,
            user=user,
            tariff=paid_tariff,
            platform=Platform.WINDOWS,
            purchase_action="renew",
            target_subscription_id=paid_sub.id,
        )

        mock_create.assert_not_called()
        mock_renew.assert_called_once_with(paid_uuid)
        assert res_sub.id == paid_sub.id
        assert paid_uuid in res_url


async def test_telegram_active_paid_allows_explicit_new_distinct_uuid(bot_session_factory, monkeypatch):
    existing_uuid = "44444444-aaaa-bbbb-cccc-000000000004"
    brand_new_uuid = "55555555-aaaa-bbbb-cccc-000000000005"
    async with bot_session_factory() as session:
        user = User(telegram_id=1004, username="active_paid_user", full_name="Active Paid User")
        server = Server(id=1, name="S1", host="1.1.1.1", location="Frankfurt", is_active=True)
        paid_tariff = Tariff(id=13, label="Basic 30d", price_rub=200, days=30, adapt_plan_uuid="adapt-plan-basic-30d")
        session.add_all([user, server, paid_tariff])
        await session.flush()

        active_sub = Subscription(
            id=504,
            user_id=user.id,
            server_id=server.id,
            tariff_id=paid_tariff.id,
            tariff_months=1,
            tariff_days=30,
            billing_mode="tariff",
            client_name=f"adapt_{existing_uuid}",
            vpn_key=f"https://darimiru.ru/vpnbot/adapt-sub/{existing_uuid}",
            platform=Platform.MAC,
            status=SubStatus.ACTIVE,
            expires_at=datetime.utcnow() + timedelta(days=20),
        )
        session.add(active_sub)
        await session.flush()

        adapt_sub = AdaptSubscription(
            subscription_id=active_sub.id,
            adapt_uuid=existing_uuid,
            adapt_plan_uuid="adapt-plan-basic-30d",
            end_date=active_sub.expires_at,
        )
        session.add(adapt_sub)
        await session.commit()

        mock_create = AsyncMock(return_value={
            "subscription_uuid": brand_new_uuid,
            "subscription_url": f"https://darimiru.ru/vpnbot/adapt-sub/{brand_new_uuid}",
            "days": 30,
            "devices": 1,
        })
        monkeypatch.setattr("bot.services.subscription_service.AdaptAPI.create_subscription", mock_create)

        res_sub, res_url = await subscription_service.create_or_extend_paid_access(
            session,
            user=user,
            tariff=paid_tariff,
            platform=Platform.MAC,
            purchase_action="new",
        )

        mock_create.assert_called_once()
        assert res_sub.id != active_sub.id
        assert brand_new_uuid in res_url


async def test_active_paid_7day_adapt_subscription_semantics_and_continuity(bot_session_factory, monkeypatch):
    paid_7d_uuid = "77777777-7777-7777-7777-777777777777"
    brand_new_uuid = "88888888-8888-8888-8888-888888888888"
    monkeypatch.setattr(buy_handler, "async_session", bot_session_factory)
    mock_plans = [
        {"uuid": "adapt-plan-paid-7d", "days": 7, "price_usd": "0.50", "devices": 1, "is_trial": False, "is_active": True},
        {"uuid": "adapt-plan-paid-30d", "days": 30, "price_usd": "3.00", "devices": 1, "is_trial": False, "is_active": True},
    ]
    monkeypatch.setattr("bot.services.adapt_api.AdaptAPI.list_plans", AsyncMock(return_value=mock_plans))

    async with bot_session_factory() as session:
        user = User(telegram_id=3007, username="paid7d_user", full_name="Paid 7d User")
        server = Server(id=1, name="S1", host="1.1.1.1", location="Frankfurt", is_active=True)
        paid_7d_tariff = Tariff(
            id=77,
            label="Лайт 7 дней",
            price_rub=49,
            days=7,
            is_active=True,
            adapt_plan_uuid="adapt-plan-paid-7d",
            adapt_cost_price_usd=0.50,
        )
        higher_tariff = Tariff(
            id=78,
            label="Базовый 30 дней",
            price_rub=200,
            days=30,
            is_active=True,
            adapt_plan_uuid="adapt-plan-paid-30d",
            adapt_cost_price_usd=3.00,
        )
        session.add_all([user, server, paid_7d_tariff, higher_tariff])
        await session.flush()

        paid_7d_sub = Subscription(
            id=707,
            user_id=user.id,
            server_id=server.id,
            tariff_id=paid_7d_tariff.id,
            tariff_months=0,
            tariff_days=7,
            billing_mode="tariff",
            client_name=f"adapt_{paid_7d_uuid}",
            vpn_key=f"https://darimiru.ru/vpnbot/adapt-sub/{paid_7d_uuid}",
            platform=Platform.IOS,
            status=SubStatus.ACTIVE,
            expires_at=datetime.utcnow() + timedelta(days=5),
        )
        session.add(paid_7d_sub)
        await session.flush()

        adapt_sub = AdaptSubscription(
            subscription_id=paid_7d_sub.id,
            adapt_uuid=paid_7d_uuid,
            adapt_plan_uuid="adapt-plan-paid-7d",
            end_date=paid_7d_sub.expires_at,
        )
        session.add(adapt_sub)
        await session.commit()

        # 1. Test predicates: NOT trial
        assert not is_adapt_trial_tariff(paid_7d_tariff)
        assert not is_adapt_trial_subscription(paid_7d_sub)

        # 2. Test active paid detection: Counts as active paid
        has_active_paid = await buy_handler._has_active_paid_subscription(3007)
        assert has_active_paid is True

        # 3. Test renewal price: returns full 7-day price
        renew_price = await get_purchase_price_rub(
            session,
            user=user,
            tariff=paid_7d_tariff,
            action="renew",
            target_subscription_id=707,
        )
        assert renew_price == 49

        # 4. Test upgrade price: normal paid upgrade pricing (not demo full price)
        upgrade_price = await get_purchase_price_rub(
            session,
            user=user,
            tariff=higher_tariff,
            action="upgrade",
            target_subscription_id=707,
        )
        assert upgrade_price > 0
        assert upgrade_price < 200  # Remaining days deducted

        # 5. Allows explicit new subscription
        mock_create = AsyncMock(return_value={
            "subscription_uuid": brand_new_uuid,
            "subscription_url": f"https://darimiru.ru/vpnbot/adapt-sub/{brand_new_uuid}",
            "days": 7,
            "devices": 1,
        })
        monkeypatch.setattr("bot.services.subscription_service.AdaptAPI.create_subscription", mock_create)

        res_sub, res_url = await subscription_service.create_or_extend_paid_access(
            session,
            user=user,
            tariff=paid_7d_tariff,
            platform=Platform.IOS,
            purchase_action="new",
        )
        mock_create.assert_called_once()
        assert res_sub.id != paid_7d_sub.id
        assert brand_new_uuid in res_url


async def test_telegram_historical_demo_plus_expired_paid_rejects_crafted_new(bot_session_factory, monkeypatch):
    demo_uuid = "11111111-0000-0000-0000-000000000001"
    expired_paid_uuid = "22222222-0000-0000-0000-000000000002"
    brand_new_uuid = "99999999-0000-0000-0000-000000000009"

    async with bot_session_factory() as session:
        user = User(telegram_id=4001, username="hist_user", full_name="Hist User")
        server = Server(id=1, name="S1", host="1.1.1.1", location="Frankfurt", is_active=True)
        paid_tariff = Tariff(id=10, label="Basic 30d", price_rub=200, days=30, is_active=True, adapt_plan_uuid="adapt-plan-paid-30d")
        session.add_all([user, server, paid_tariff])
        await session.flush()

        demo_sub = Subscription(
            id=801,
            user_id=user.id,
            server_id=server.id,
            tariff_months=0,
            tariff_days=3,
            billing_mode="demo",
            client_name=f"adapt_{demo_uuid}",
            vpn_key=f"https://darimiru.ru/vpnbot/adapt-sub/{demo_uuid}",
            platform=Platform.IOS,
            status=SubStatus.EXPIRED,
            expires_at=datetime.utcnow() - timedelta(days=20),
        )
        expired_paid_sub = Subscription(
            id=802,
            user_id=user.id,
            server_id=server.id,
            tariff_id=paid_tariff.id,
            tariff_months=1,
            tariff_days=30,
            billing_mode="tariff",
            client_name=f"adapt_{expired_paid_uuid}",
            vpn_key=f"https://darimiru.ru/vpnbot/adapt-sub/{expired_paid_uuid}",
            platform=Platform.IOS,
            status=SubStatus.EXPIRED,
            expires_at=datetime.utcnow() - timedelta(days=2),
        )
        session.add_all([demo_sub, expired_paid_sub])
        await session.flush()

        adapt_sub1 = AdaptSubscription(subscription_id=demo_sub.id, adapt_uuid=demo_uuid, adapt_plan_uuid="adapt-demo-plan-uuid", end_date=demo_sub.expires_at)
        adapt_sub2 = AdaptSubscription(subscription_id=expired_paid_sub.id, adapt_uuid=expired_paid_uuid, adapt_plan_uuid="adapt-plan-paid-30d", end_date=expired_paid_sub.expires_at)
        session.add_all([adapt_sub1, adapt_sub2])
        await session.commit()

        mock_create = AsyncMock(return_value={
            "subscription_uuid": brand_new_uuid,
            "subscription_url": f"https://darimiru.ru/vpnbot/adapt-sub/{brand_new_uuid}",
            "days": 30,
            "devices": 1,
        })
        monkeypatch.setattr("bot.services.subscription_service.AdaptAPI.create_subscription", mock_create)

        res_sub, res_url = await subscription_service.create_or_extend_paid_access(
            session,
            user=user,
            tariff=paid_tariff,
            platform=Platform.IOS,
            purchase_action="new",
        )
        mock_create.assert_called_once()
        assert res_sub.id != demo_sub.id
        assert res_sub.id != expired_paid_sub.id
        assert brand_new_uuid in res_url


async def test_telegram_multiple_expired_paid_rejects_crafted_new(bot_session_factory, monkeypatch):
    uuid1 = "33333333-0000-0000-0000-000000000001"
    uuid2 = "44444444-0000-0000-0000-000000000002"
    brand_new_uuid = "99999999-0000-0000-0000-000000000008"

    async with bot_session_factory() as session:
        user = User(telegram_id=4002, username="multi_exp_user", full_name="Multi Exp User")
        server = Server(id=1, name="S1", host="1.1.1.1", location="Frankfurt", is_active=True)
        paid_tariff = Tariff(id=10, label="Basic 30d", price_rub=200, days=30, is_active=True, adapt_plan_uuid="adapt-plan-paid-30d")
        session.add_all([user, server, paid_tariff])
        await session.flush()

        sub1 = Subscription(
            id=803,
            user_id=user.id,
            server_id=server.id,
            tariff_id=paid_tariff.id,
            tariff_months=1,
            tariff_days=30,
            billing_mode="tariff",
            client_name=f"adapt_{uuid1}",
            vpn_key=f"https://darimiru.ru/vpnbot/adapt-sub/{uuid1}",
            platform=Platform.IOS,
            status=SubStatus.EXPIRED,
            expires_at=datetime.utcnow() - timedelta(days=10),
        )
        sub2 = Subscription(
            id=804,
            user_id=user.id,
            server_id=server.id,
            tariff_id=paid_tariff.id,
            tariff_months=1,
            tariff_days=30,
            billing_mode="tariff",
            client_name=f"adapt_{uuid2}",
            vpn_key=f"https://darimiru.ru/vpnbot/adapt-sub/{uuid2}",
            platform=Platform.IOS,
            status=SubStatus.EXPIRED,
            expires_at=datetime.utcnow() - timedelta(days=5),
        )
        session.add_all([sub1, sub2])
        await session.flush()

        adapt_sub1 = AdaptSubscription(subscription_id=sub1.id, adapt_uuid=uuid1, adapt_plan_uuid="adapt-plan-paid-30d", end_date=sub1.expires_at)
        adapt_sub2 = AdaptSubscription(subscription_id=sub2.id, adapt_uuid=uuid2, adapt_plan_uuid="adapt-plan-paid-30d", end_date=sub2.expires_at)
        session.add_all([adapt_sub1, adapt_sub2])
        await session.commit()

        mock_create = AsyncMock(return_value={
            "subscription_uuid": brand_new_uuid,
            "subscription_url": f"https://darimiru.ru/vpnbot/adapt-sub/{brand_new_uuid}",
            "days": 30,
            "devices": 1,
        })
        monkeypatch.setattr("bot.services.subscription_service.AdaptAPI.create_subscription", mock_create)

        res_sub, res_url = await subscription_service.create_or_extend_paid_access(
            session,
            user=user,
            tariff=paid_tariff,
            platform=Platform.IOS,
            purchase_action="new",
        )
        mock_create.assert_called_once()
        assert res_sub.id != sub1.id
        assert res_sub.id != sub2.id
        assert brand_new_uuid in res_url


# ==============================================================================
# Web Flow Continuity & Explicit-New Tests
# ==============================================================================


async def test_web_active_demo_no_new_upgrades_same_uuid(web_session_factory, monkeypatch):
    demo_uuid = "66666666-aaaa-bbbb-cccc-000000000006"
    profile_token = "prof_web_active_demo"
    monkeypatch.setattr(webstore_routes, "async_session", web_session_factory)
    monkeypatch.setitem(sys.modules, "yookassa", _FakeYooKassa())
    monkeypatch.setattr(webstore_routes.settings, "yookassa_shop_id", "shop")
    monkeypatch.setattr(webstore_routes.settings, "yookassa_secret_key", "secret")
    monkeypatch.setattr(webstore_routes.settings, "adapt_demo_plan_uuid", "00cce2fe-ee55-4c0d-8bfa-9e6e47cd99a4")
    mock_tariffs = [
        {"key": "adapt_basic", "label": "Basic", "days": 30, "price_rub": 200, "provider": "adapt", "adapt_plan_uuid": "adapt-plan-basic-30d", "adapt_cost_price_usd": 4.0}
    ]
    monkeypatch.setattr(webstore_routes, "get_store_tariffs", lambda: mock_tariffs)
    monkeypatch.setattr(webstore_routes, "get_store_tariffs_by_key", lambda: {t["key"]: t for t in mock_tariffs})
    monkeypatch.setattr(
        webstore_routes,
        "get_adapt_plan_metadata_by_uuid",
        lambda uuid: {
            "adapt_plan_uuid": uuid,
            "adapt_cost_price_usd": 0.30,
            "price_usd": 0.30,
            "days": 3,
            "price_rub": 0,
        } if uuid == webstore_routes.settings.adapt_demo_plan_uuid else None,
    )

    async with web_session_factory() as session:
        demo_order = WebOrder(
            id=601,
            order_id="order_demo_601",
            profile_token=profile_token,
            tariff_key="demo",
            tariff_label="Тестовый доступ",
            days=3,
            amount_rub=0,
            status="demo",
            subscription_url=f"https://darimiru.ru/vpnbot/adapt-sub/{demo_uuid}",
            marzban_username=f"adapt_{demo_uuid}",
            access_expires_at=datetime.utcnow() + timedelta(days=2),
        )
        session.add(demo_order)
        await session.commit()

    req = _Request(
        _json_body={"tariff_key": "adapt_basic", "purchase_action": "new", "contact": "demo@example.com"},
        cookies={webstore_routes._AUTH_COOKIE: profile_token},
    )
    resp = await webstore_routes.handle_create_order(req)
    assert resp.status == 200
    data = json.loads(resp.text)

    async with web_session_factory() as session:
        created = await session.scalar(select(WebOrder).where(WebOrder.order_id == data["order_id"]))
        assert created is not None
        assert created.target_order_id == f"uuid:{demo_uuid}"
        assert created.purchase_action == "upgrade"


async def test_web_expired_demo_no_new_reactivates_and_upgrades_same_uuid(web_session_factory, monkeypatch):
    demo_uuid = "77777777-aaaa-bbbb-cccc-000000000007"
    profile_token = "prof_web_exp_demo"
    monkeypatch.setattr(webstore_routes, "async_session", web_session_factory)
    monkeypatch.setitem(sys.modules, "yookassa", _FakeYooKassa())
    monkeypatch.setattr(webstore_routes.settings, "yookassa_shop_id", "shop")
    monkeypatch.setattr(webstore_routes.settings, "yookassa_secret_key", "secret")
    monkeypatch.setattr(webstore_routes.settings, "adapt_demo_plan_uuid", "00cce2fe-ee55-4c0d-8bfa-9e6e47cd99a4")
    mock_tariffs = [
        {"key": "adapt_basic", "label": "Basic", "days": 30, "price_rub": 200, "provider": "adapt", "adapt_plan_uuid": "adapt-plan-basic-30d", "adapt_cost_price_usd": 4.0}
    ]
    monkeypatch.setattr(webstore_routes, "get_store_tariffs", lambda: mock_tariffs)
    monkeypatch.setattr(webstore_routes, "get_store_tariffs_by_key", lambda: {t["key"]: t for t in mock_tariffs})
    monkeypatch.setattr(
        webstore_routes,
        "get_adapt_plan_metadata_by_uuid",
        lambda uuid: {
            "adapt_plan_uuid": uuid,
            "adapt_cost_price_usd": 0.30,
            "price_usd": 0.30,
            "days": 3,
            "price_rub": 0,
        } if uuid == webstore_routes.settings.adapt_demo_plan_uuid else None,
    )

    async with web_session_factory() as session:
        demo_order = WebOrder(
            id=602,
            order_id="order_demo_602",
            profile_token=profile_token,
            tariff_key="demo",
            tariff_label="Тестовый доступ",
            days=3,
            amount_rub=0,
            status="demo",
            subscription_url=f"https://darimiru.ru/vpnbot/adapt-sub/{demo_uuid}",
            marzban_username=f"adapt_{demo_uuid}",
            access_expires_at=datetime.utcnow() - timedelta(days=2),
        )
        session.add(demo_order)
        await session.commit()

    req = _Request(
        _json_body={"tariff_key": "adapt_basic", "purchase_action": "new", "contact": "expdemo@example.com"},
        cookies={webstore_routes._AUTH_COOKIE: profile_token},
    )
    resp = await webstore_routes.handle_create_order(req)
    assert resp.status == 200
    data = json.loads(resp.text)

    async with web_session_factory() as session:
        created = await session.scalar(select(WebOrder).where(WebOrder.order_id == data["order_id"]))
        assert created is not None
        assert created.target_order_id == f"uuid:{demo_uuid}"
        assert created.purchase_action == "upgrade"


async def test_web_expired_paid_no_new_rejected_on_crafted_new(web_session_factory, monkeypatch):
    paid_uuid = "88888888-aaaa-bbbb-cccc-000000000008"
    profile_token = "prof_web_exp_paid"
    monkeypatch.setattr(webstore_routes, "async_session", web_session_factory)
    monkeypatch.setitem(sys.modules, "yookassa", _FakeYooKassa())
    monkeypatch.setattr(webstore_routes.settings, "yookassa_shop_id", "shop")
    monkeypatch.setattr(webstore_routes.settings, "yookassa_secret_key", "secret")
    mock_tariffs = [
        {"key": "adapt_basic", "label": "Basic", "days": 30, "price_rub": 200, "provider": "adapt", "adapt_plan_uuid": "adapt-plan-basic-30d", "adapt_cost_price_usd": 1.0}
    ]
    monkeypatch.setattr(webstore_routes, "get_store_tariffs", lambda: mock_tariffs)
    monkeypatch.setattr(webstore_routes, "get_store_tariffs_by_key", lambda: {t["key"]: t for t in mock_tariffs})

    async with web_session_factory() as session:
        expired_order = WebOrder(
            id=603,
            order_id="order_exp_603",
            profile_token=profile_token,
            tariff_key="adapt_basic",
            tariff_label="Базовый",
            days=30,
            amount_rub=200,
            status="delivered",
            subscription_url=f"https://darimiru.ru/vpnbot/adapt-sub/{paid_uuid}",
            marzban_username=f"adapt_{paid_uuid}",
            access_expires_at=datetime.utcnow() - timedelta(days=5),
        )
        session.add(expired_order)
        await session.commit()

    req = _Request(
        _json_body={"tariff_key": "adapt_basic", "purchase_action": "new", "contact": "exppaid@example.com"},
        cookies={webstore_routes._AUTH_COOKIE: profile_token},
    )
    resp = await webstore_routes.handle_create_order(req)
    assert resp.status == 200
    data = json.loads(resp.text)
    async with web_session_factory() as session:
        created = await session.scalar(select(WebOrder).where(WebOrder.order_id == data["order_id"]))
        assert created is not None
        assert created.purchase_action == "new"
        assert created.target_order_id is None


async def test_web_historical_demo_plus_expired_paid_rejects_crafted_new(web_session_factory, monkeypatch):
    demo_uuid = "11111111-2222-3333-4444-555555555555"
    paid_uuid = "66666666-7777-8888-9999-000000000000"
    profile_token = "prof_web_hist_demo_exp_paid"
    monkeypatch.setattr(webstore_routes, "async_session", web_session_factory)
    monkeypatch.setitem(sys.modules, "yookassa", _FakeYooKassa())
    monkeypatch.setattr(webstore_routes.settings, "yookassa_shop_id", "shop")
    monkeypatch.setattr(webstore_routes.settings, "yookassa_secret_key", "secret")
    mock_tariffs = [
        {"key": "adapt_basic", "label": "Basic", "days": 30, "price_rub": 200, "provider": "adapt", "adapt_plan_uuid": "adapt-plan-basic-30d"}
    ]
    monkeypatch.setattr(webstore_routes, "get_store_tariffs", lambda: mock_tariffs)
    monkeypatch.setattr(webstore_routes, "get_store_tariffs_by_key", lambda: {t["key"]: t for t in mock_tariffs})

    async with web_session_factory() as session:
        demo_order = WebOrder(
            id=605,
            order_id="order_demo_605",
            profile_token=profile_token,
            tariff_key="demo",
            tariff_label="Тестовый доступ",
            days=3,
            amount_rub=0,
            status="demo",
            subscription_url=f"https://darimiru.ru/vpnbot/adapt-sub/{demo_uuid}",
            marzban_username=f"adapt_{demo_uuid}",
            access_expires_at=datetime.utcnow() - timedelta(days=20),
        )
        expired_order = WebOrder(
            id=606,
            order_id="order_exp_606",
            profile_token=profile_token,
            tariff_key="adapt_basic",
            tariff_label="Базовый",
            days=30,
            amount_rub=200,
            status="delivered",
            subscription_url=f"https://darimiru.ru/vpnbot/adapt-sub/{paid_uuid}",
            marzban_username=f"adapt_{paid_uuid}",
            access_expires_at=datetime.utcnow() - timedelta(days=2),
        )
        session.add_all([demo_order, expired_order])
        await session.commit()

    req = _Request(
        _json_body={"tariff_key": "adapt_basic", "purchase_action": "new", "contact": "user@example.com"},
        cookies={webstore_routes._AUTH_COOKIE: profile_token},
    )
    resp = await webstore_routes.handle_create_order(req)
    assert resp.status == 200
    data = json.loads(resp.text)
    async with web_session_factory() as session:
        created = await session.scalar(select(WebOrder).where(WebOrder.order_id == data["order_id"]))
        assert created is not None
        assert created.purchase_action == "new"
        assert created.target_order_id is None


async def test_web_multiple_expired_paid_rejects_crafted_new(web_session_factory, monkeypatch):
    paid_uuid1 = "11111111-aaaa-bbbb-cccc-111111111111"
    paid_uuid2 = "22222222-aaaa-bbbb-cccc-222222222222"
    profile_token = "prof_web_multi_exp_paid"
    monkeypatch.setattr(webstore_routes, "async_session", web_session_factory)
    monkeypatch.setitem(sys.modules, "yookassa", _FakeYooKassa())
    monkeypatch.setattr(webstore_routes.settings, "yookassa_shop_id", "shop")
    monkeypatch.setattr(webstore_routes.settings, "yookassa_secret_key", "secret")
    mock_tariffs = [
        {"key": "adapt_basic", "label": "Basic", "days": 30, "price_rub": 200, "provider": "adapt", "adapt_plan_uuid": "adapt-plan-basic-30d"}
    ]
    monkeypatch.setattr(webstore_routes, "get_store_tariffs", lambda: mock_tariffs)
    monkeypatch.setattr(webstore_routes, "get_store_tariffs_by_key", lambda: {t["key"]: t for t in mock_tariffs})

    async with web_session_factory() as session:
        order1 = WebOrder(
            id=607,
            order_id="order_exp_607",
            profile_token=profile_token,
            tariff_key="adapt_basic",
            tariff_label="Базовый",
            days=30,
            amount_rub=200,
            status="delivered",
            subscription_url=f"https://darimiru.ru/vpnbot/adapt-sub/{paid_uuid1}",
            marzban_username=f"adapt_{paid_uuid1}",
            access_expires_at=datetime.utcnow() - timedelta(days=10),
        )
        order2 = WebOrder(
            id=608,
            order_id="order_exp_608",
            profile_token=profile_token,
            tariff_key="adapt_basic",
            tariff_label="Базовый",
            days=30,
            amount_rub=200,
            status="delivered",
            subscription_url=f"https://darimiru.ru/vpnbot/adapt-sub/{paid_uuid2}",
            marzban_username=f"adapt_{paid_uuid2}",
            access_expires_at=datetime.utcnow() - timedelta(days=5),
        )
        session.add_all([order1, order2])
        await session.commit()

    req = _Request(
        _json_body={"tariff_key": "adapt_basic", "purchase_action": "new", "contact": "user@example.com"},
        cookies={webstore_routes._AUTH_COOKIE: profile_token},
    )
    resp = await webstore_routes.handle_create_order(req)
    assert resp.status == 200
    data = json.loads(resp.text)
    async with web_session_factory() as session:
        created = await session.scalar(select(WebOrder).where(WebOrder.order_id == data["order_id"]))
        assert created is not None
        assert created.purchase_action == "new"
        assert created.target_order_id is None


async def test_web_active_paid_allows_explicit_new_distinct_order(web_session_factory, monkeypatch):
    paid_uuid = "99999999-aaaa-bbbb-cccc-000000000009"
    profile_token = "prof_web_active_paid"
    monkeypatch.setattr(webstore_routes, "async_session", web_session_factory)
    monkeypatch.setitem(sys.modules, "yookassa", _FakeYooKassa())
    monkeypatch.setattr(webstore_routes.settings, "yookassa_shop_id", "shop")
    monkeypatch.setattr(webstore_routes.settings, "yookassa_secret_key", "secret")
    mock_tariffs = [
        {"key": "adapt_basic", "label": "Basic", "days": 30, "price_rub": 200, "provider": "adapt", "adapt_plan_uuid": "adapt-plan-basic-30d"}
    ]
    monkeypatch.setattr(webstore_routes, "get_store_tariffs", lambda: mock_tariffs)
    monkeypatch.setattr(webstore_routes, "get_store_tariffs_by_key", lambda: {t["key"]: t for t in mock_tariffs})

    async with web_session_factory() as session:
        active_order = WebOrder(
            id=604,
            order_id="order_active_604",
            profile_token=profile_token,
            tariff_key="adapt_basic",
            tariff_label="Базовый",
            days=30,
            amount_rub=200,
            status="delivered",
            subscription_url=f"https://darimiru.ru/vpnbot/adapt-sub/{paid_uuid}",
            marzban_username=f"adapt_{paid_uuid}",
            access_expires_at=datetime.utcnow() + timedelta(days=20),
        )
        session.add(active_order)
        await session.commit()

    req = _Request(
        _json_body={"tariff_key": "adapt_basic", "purchase_action": "new", "contact": "active@example.com"},
        cookies={webstore_routes._AUTH_COOKIE: profile_token},
    )
    resp = await webstore_routes.handle_create_order(req)
    assert resp.status == 200
    data = json.loads(resp.text)

    async with web_session_factory() as session:
        created = await session.scalar(select(WebOrder).where(WebOrder.order_id == data["order_id"]))
        assert created is not None
        assert created.purchase_action == "new"
        assert created.target_order_id is None


async def test_adapt_subscription_discovery_db_error_fails_closed_and_does_not_call_create(bot_session_factory, monkeypatch):
    """If DB query fails during local Adapt subscription lookup, fail closed without calling create_subscription."""
    mock_create = AsyncMock()
    monkeypatch.setattr("bot.services.adapt_api.AdaptAPI.create_subscription", mock_create)
    monkeypatch.setattr("bot.services.subscription_service._user_adapt_subscriptions", AsyncMock(side_effect=RuntimeError("DB query failed")))

    async with bot_session_factory() as session:
        user = User(id=701, telegram_id=701001, username="db_error_user")
        server = Server(id=701, name="Adapt Srv", is_active=True, host="1.1.1.1", location="Frankfurt")
        tariff = Tariff(id=701, label="Paid 30", days=30, price_rub=200, adapt_plan_uuid="adapt-plan-paid-30d")
        session.add_all([user, server, tariff])
        await session.commit()

        with pytest.raises(AccessProvisionError) as exc_info:
            await subscription_service._create_adapt_paid_subscription(
                session,
                user=user,
                tariff=tariff,
                platform=Platform.IOS,
                purchase_action="new",
            )
        assert exc_info.value.code == "adapt_subscription_discovery_failed"
        mock_create.assert_not_called()


async def test_adapt_subscription_canonical_plan_mismatch_precedence():
    """AdaptSubscription plan UUID is authoritative over stale Tariff plan."""
    demo_plan_uuid = "demo-plan-uuid-1234"
    paid_plan_uuid = "paid-plan-uuid-5678"

    with patch("bot.config.settings.adapt_demo_plan_uuid", demo_plan_uuid):
        # 1. AdaptSubscription says PAID, stale Tariff says DEMO -> Result is PAID (not demo)
        sub1 = SimpleNamespace(
            id=1,
            billing_mode="paid",
            client_name="adapt_user_1",
            tariff=SimpleNamespace(adapt_plan_uuid=demo_plan_uuid, days=7),
        )
        adapt_sub1 = SimpleNamespace(adapt_plan_uuid=paid_plan_uuid)
        assert is_adapt_trial_subscription(sub1, adapt_sub=adapt_sub1) is False

        # 2. AdaptSubscription says DEMO, stale Tariff says PAID -> Result is DEMO
        sub2 = SimpleNamespace(
            id=2,
            billing_mode="paid",
            client_name="adapt_user_2",
            tariff=SimpleNamespace(adapt_plan_uuid=paid_plan_uuid, days=30),
        )
        adapt_sub2 = SimpleNamespace(adapt_plan_uuid=demo_plan_uuid)
        assert is_adapt_trial_subscription(sub2, adapt_sub=adapt_sub2) is True


async def test_adapt_fulfillment_frozen_same_plan_unfreezes_and_renews(bot_session_factory, monkeypatch):
    """A frozen subscription must be unfrozen before calling renew_subscription and re-frozen after."""
    adapt_uuid = "11111111-aaaa-bbbb-cccc-000000000702"
    plan_uuid = "plan-paid-30d"

    mock_unfreeze = AsyncMock(return_value={"success": True, "subscription_uuid": adapt_uuid})
    mock_renew = AsyncMock(return_value={"success": True, "subscription_uuid": adapt_uuid, "end_date": (datetime.utcnow() + timedelta(days=30)).isoformat()})
    mock_freeze = AsyncMock(return_value={"success": True, "subscription_uuid": adapt_uuid})
    mock_upgrade = AsyncMock()

    status_frozen = {
        "success": True,
        "subscription_uuid": adapt_uuid,
        "plan_uuid": plan_uuid,
        "is_active": False,
        "is_frozen": True,
        "devices": 2,
        "end_date": (datetime.utcnow() + timedelta(days=10)).isoformat(),
    }
    status_unfrozen = {
        "success": True,
        "subscription_uuid": adapt_uuid,
        "plan_uuid": plan_uuid,
        "is_active": True,
        "is_frozen": False,
        "devices": 2,
        "end_date": (datetime.utcnow() + timedelta(days=10)).isoformat(),
    }
    status_renewed = {
        "success": True,
        "subscription_uuid": adapt_uuid,
        "plan_uuid": plan_uuid,
        "is_active": True,
        "is_frozen": False,
        "devices": 2,
        "end_date": (datetime.utcnow() + timedelta(days=40)).isoformat(),
    }
    status_refrozen = {
        "success": True,
        "subscription_uuid": adapt_uuid,
        "plan_uuid": plan_uuid,
        "is_active": False,
        "is_frozen": True,
        "devices": 2,
        "end_date": (datetime.utcnow() + timedelta(days=40)).isoformat(),
    }

    mock_get_status = AsyncMock(side_effect=[status_frozen, status_unfrozen, status_renewed, status_unfrozen, status_refrozen])
    mock_list_plans = AsyncMock(return_value=[{"uuid": plan_uuid, "devices": 2, "is_active": True}])

    monkeypatch.setattr("bot.services.adapt_api.AdaptAPI.get_status", mock_get_status)
    monkeypatch.setattr("bot.services.adapt_api.AdaptAPI.list_plans", mock_list_plans)
    monkeypatch.setattr("bot.services.adapt_api.AdaptAPI.unfreeze_subscription", mock_unfreeze)
    monkeypatch.setattr("bot.services.adapt_api.AdaptAPI.renew_subscription", mock_renew)
    monkeypatch.setattr("bot.services.adapt_api.AdaptAPI.freeze_subscription", mock_freeze)
    monkeypatch.setattr("bot.services.adapt_api.AdaptAPI.upgrade_subscription", mock_upgrade)

    async with bot_session_factory() as session:
        user = User(id=702, telegram_id=702001, username="frozen_user")
        server = Server(id=702, name="Adapt Srv", is_active=True, host="1.1.1.1", location="Frankfurt")
        tariff = Tariff(id=702, label="Paid 30", days=30, price_rub=200, adapt_plan_uuid=plan_uuid)
        sub = Subscription(
            id=702,
            user_id=702,
            server_id=702,
            tariff_id=702,
            tariff_months=1,
            tariff_days=30,
            platform=Platform.IOS,
            status=SubStatus.ACTIVE,
            client_name="adapt_client_702",
            vpn_key=f"https://darimiru.ru/vpnbot/adapt-sub/{adapt_uuid}",
            expires_at=datetime.utcnow() + timedelta(days=10),
            device_slots=2,
        )
        adapt_sub = AdaptSubscription(
            subscription_id=702,
            adapt_uuid=adapt_uuid,
            adapt_plan_uuid=plan_uuid,
            end_date=datetime.utcnow() + timedelta(days=10),
            is_frozen=True,
        )
        session.add_all([user, server, tariff, sub, adapt_sub])
        await session.commit()

        result_sub, url = await subscription_service._create_adapt_paid_subscription(
            session,
            user=user,
            tariff=tariff,
            platform=Platform.IOS,
            purchase_action="renew",
            target_subscription_id=702,
        )
        assert result_sub.id == 702
        mock_unfreeze.assert_awaited_once_with(adapt_uuid)
        mock_renew.assert_awaited_once_with(adapt_uuid)
        mock_freeze.assert_awaited_once_with(adapt_uuid)
        assert adapt_sub.is_frozen is True
        mock_upgrade.assert_not_called()


async def test_web_fulfillment_frozen_upgrade_unfreezes_and_upgrades(web_session_factory, monkeypatch):
    """Web fulfillment for frozen subscription unfreezes before upgrading and re-freezes after."""
    adapt_uuid = "22222222-aaaa-bbbb-cccc-000000000703"
    old_plan_uuid = "plan-basic-30d"
    new_plan_uuid = "plan-pro-30d"

    mock_tariffs = [
        {"key": "adapt_basic", "label": "Basic", "days": 30, "price_rub": 200, "provider": "adapt", "adapt_plan_uuid": old_plan_uuid},
        {"key": "adapt_pro", "label": "Pro", "days": 30, "price_rub": 350, "provider": "adapt", "adapt_plan_uuid": new_plan_uuid},
    ]
    monkeypatch.setattr(webstore_routes, "get_store_tariffs", lambda: mock_tariffs)
    monkeypatch.setattr(webstore_routes, "get_store_tariffs_by_key", lambda: {t["key"]: t for t in mock_tariffs})

    mock_unfreeze = AsyncMock(return_value={"success": True, "subscription_uuid": adapt_uuid})
    mock_upgrade = AsyncMock(return_value={"success": True, "subscription_uuid": adapt_uuid, "devices": 3, "end_date": (datetime.utcnow() + timedelta(days=30)).isoformat()})
    mock_freeze = AsyncMock(return_value={"success": True, "subscription_uuid": adapt_uuid})
    mock_renew = AsyncMock()

    status_frozen = {
        "success": True,
        "subscription_uuid": adapt_uuid,
        "plan_uuid": old_plan_uuid,
        "is_active": False,
        "is_frozen": True,
        "devices": 1,
        "end_date": (datetime.utcnow() + timedelta(days=10)).isoformat(),
    }
    status_unfrozen = {
        "success": True,
        "subscription_uuid": adapt_uuid,
        "plan_uuid": old_plan_uuid,
        "is_active": True,
        "is_frozen": False,
        "devices": 1,
        "end_date": (datetime.utcnow() + timedelta(days=10)).isoformat(),
    }
    status_final = {
        "success": True,
        "subscription_uuid": adapt_uuid,
        "plan_uuid": new_plan_uuid,
        "is_active": True,
        "is_frozen": False,
        "devices": 3,
        "end_date": (datetime.utcnow() + timedelta(days=30)).isoformat(),
    }
    status_refrozen = {
        "success": True,
        "subscription_uuid": adapt_uuid,
        "plan_uuid": new_plan_uuid,
        "is_active": False,
        "is_frozen": True,
        "devices": 3,
        "end_date": (datetime.utcnow() + timedelta(days=30)).isoformat(),
    }

    mock_get_status = AsyncMock(side_effect=[status_frozen, status_unfrozen, status_final, status_unfrozen, status_refrozen])
    mock_list_plans = AsyncMock(return_value=[
        {"uuid": old_plan_uuid, "devices": 1, "is_active": True},
        {"uuid": new_plan_uuid, "devices": 3, "is_active": True},
    ])

    monkeypatch.setattr("webstore.routes.AdaptAPI.get_status", mock_get_status)
    monkeypatch.setattr("webstore.routes.AdaptAPI.list_plans", mock_list_plans)
    monkeypatch.setattr("webstore.routes.AdaptAPI.unfreeze_subscription", mock_unfreeze)
    monkeypatch.setattr("webstore.routes.AdaptAPI.freeze_subscription", mock_freeze)
    monkeypatch.setattr("webstore.routes.AdaptAPI.upgrade_subscription", mock_upgrade)
    monkeypatch.setattr("webstore.routes.AdaptAPI.renew_subscription", mock_renew)

    async with web_session_factory() as session:
        primary_order = WebOrder(
            id=703,
            order_id="order_frozen_primary_703",
            profile_token="prof_frozen_703",
            tariff_key="adapt_basic",
            tariff_label="Базовый",
            days=30,
            amount_rub=200,
            status="delivered",
            subscription_url=f"https://darimiru.ru/vpnbot/adapt-sub/{adapt_uuid}",
            marzban_username=f"adapt_{adapt_uuid}",
            access_expires_at=datetime.utcnow() + timedelta(days=10),
            purchase_action="new",
        )
        upgrade_order = WebOrder(
            id=704,
            order_id="order_frozen_upgrade_704",
            profile_token="prof_frozen_703",
            tariff_key="adapt_pro",
            tariff_label="Про",
            days=30,
            amount_rub=350,
            status="paid",
            paid_at=datetime.utcnow(),
            purchase_action="upgrade",
            target_order_id="order_frozen_primary_703",
            target_snapshot_plan_uuid=old_plan_uuid,
            target_snapshot_expires_at=datetime.utcnow() + timedelta(days=10),
        )
        session.add_all([primary_order, upgrade_order])
        await session.commit()

        await webstore_routes._fulfill_adapt_order(session, upgrade_order, new_plan_uuid)
        assert upgrade_order.status == "delivered"
        mock_unfreeze.assert_awaited_once_with(adapt_uuid)
        mock_upgrade.assert_awaited_once_with(adapt_uuid, new_plan_uuid)
        mock_freeze.assert_awaited_once_with(adapt_uuid)
        mock_renew.assert_not_called()


async def test_telegram_frozen_restore_retry_reconciles_without_second_mutation(bot_session_factory, monkeypatch):
    adapt_uuid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaa0707"
    old_plan_uuid = "plan-old-retry"
    new_plan_uuid = "plan-new-retry"
    baseline_end = datetime.utcnow() + timedelta(days=5)
    provider_end = datetime.utcnow() + timedelta(days=35)

    async with bot_session_factory() as session:
        user = User(id=707, telegram_id=707001, username="retry_user")
        server = Server(id=707, name="Retry server", host="1.1.1.1", location="Frankfurt")
        old_tariff = Tariff(
            id=707,
            label="Старый тариф",
            days=30,
            price_rub=200,
            adapt_plan_uuid=old_plan_uuid,
            adapt_cost_price_usd=1.0,
        )
        new_tariff = Tariff(
            id=708,
            label="Новый тариф",
            days=30,
            price_rub=350,
            adapt_plan_uuid=new_plan_uuid,
            adapt_cost_price_usd=2.0,
        )
        sub = Subscription(
            id=707,
            user_id=user.id,
            server_id=server.id,
            tariff_id=old_tariff.id,
            tariff_months=1,
            tariff_days=30,
            platform=Platform.IOS,
            status=SubStatus.ACTIVE,
            client_name=f"adapt_{adapt_uuid}",
            vpn_key=f"https://darimiru.ru/vpnbot/adapt-sub/{adapt_uuid}",
            expires_at=baseline_end,
            device_slots=1,
        )
        adapt_sub = AdaptSubscription(
            subscription_id=sub.id,
            adapt_uuid=adapt_uuid,
            adapt_plan_uuid=old_plan_uuid,
            end_date=baseline_end,
            is_frozen=True,
        )
        payment = Payment(
            id=707,
            user_id=user.id,
            subscription_id=sub.id,
            amount=350,
            currency="RUB",
            method=PaymentMethod.TELEGRAM,
            status=PaymentStatus.COMPLETED,
            provisioning_freeze_state="mutation_applied",
            provisioning_baseline_plan_uuid=old_plan_uuid,
            provisioning_baseline_expires_at=baseline_end,
        )
        session.add_all([user, server, old_tariff, new_tariff, sub, adapt_sub, payment])
        await session.commit()

    status_after_mutation = {
        "subscription_uuid": adapt_uuid,
        "plan_uuid": new_plan_uuid,
        "is_active": False,
        "is_frozen": True,
        "devices": 3,
        "end_date": provider_end.isoformat(),
    }
    mock_get_status = AsyncMock(side_effect=[status_after_mutation, status_after_mutation, status_after_mutation])
    mock_list_plans = AsyncMock(return_value=[
        {"uuid": new_plan_uuid, "devices": 3, "is_active": True},
    ])
    mock_unfreeze = AsyncMock()
    mock_renew = AsyncMock()
    mock_custom_renew = AsyncMock()
    mock_upgrade = AsyncMock()
    mock_freeze = AsyncMock()
    monkeypatch.setattr("bot.services.subscription_service.AdaptAPI.get_status", mock_get_status)
    monkeypatch.setattr("bot.services.subscription_service.AdaptAPI.list_plans", mock_list_plans)
    monkeypatch.setattr("bot.services.subscription_service.AdaptAPI.unfreeze_subscription", mock_unfreeze)
    monkeypatch.setattr("bot.services.subscription_service.AdaptAPI.renew_subscription", mock_renew)
    monkeypatch.setattr("bot.services.subscription_service.AdaptAPI.renew_subscription_custom", mock_custom_renew)
    monkeypatch.setattr("bot.services.subscription_service.AdaptAPI.upgrade_subscription", mock_upgrade)
    monkeypatch.setattr("bot.services.subscription_service.AdaptAPI.freeze_subscription", mock_freeze)

    async with bot_session_factory() as session:
        user = await session.get(User, 707)
        payment = await session.get(Payment, 707)
        result_sub, _ = await subscription_service._create_adapt_paid_subscription(
            session,
            user=user,
            tariff=await session.get(Tariff, 708),
            platform=Platform.IOS,
            purchase_action="upgrade",
            target_subscription_id=707,
            provisioning_payment=payment,
        )
        await session.refresh(payment)
        adapt_sub = await session.scalar(
            select(AdaptSubscription).where(AdaptSubscription.subscription_id == 707)
        )

    assert result_sub.id == 707
    mock_unfreeze.assert_not_called()
    mock_renew.assert_not_called()
    mock_custom_renew.assert_not_called()
    mock_upgrade.assert_not_called()
    mock_freeze.assert_not_called()
    assert payment.provisioning_freeze_state == "freeze_restored"
    assert adapt_sub.is_frozen is True


async def test_web_frozen_restore_retry_reconciles_without_second_mutation(web_session_factory, monkeypatch):
    adapt_uuid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbb0708"
    old_plan_uuid = "plan-old-web-retry"
    new_plan_uuid = "plan-new-web-retry"
    profile_token = "profile_web_retry"
    baseline_end = datetime.utcnow() + timedelta(days=5)

    mock_tariffs = [
        {
            "key": "adapt_old_retry",
            "label": "Старый тариф",
            "days": 30,
            "price_rub": 200,
            "provider": "adapt",
            "adapt_plan_uuid": old_plan_uuid,
            "adapt_cost_price_usd": 1.0,
        },
        {
            "key": "adapt_new_retry",
            "label": "Новый тариф",
            "days": 30,
            "price_rub": 350,
            "provider": "adapt",
            "adapt_plan_uuid": new_plan_uuid,
            "adapt_cost_price_usd": 2.0,
        },
    ]
    monkeypatch.setattr(webstore_routes, "get_store_tariffs", lambda: mock_tariffs)
    monkeypatch.setattr(webstore_routes, "get_store_tariffs_by_key", lambda: {
        tariff["key"]: tariff for tariff in mock_tariffs
    })
    async with web_session_factory() as session:
        primary = WebOrder(
            id=708,
            order_id="order_web_retry_primary",
            profile_token=profile_token,
            tariff_key="adapt_old_retry",
            tariff_label="Старый тариф",
            days=30,
            amount_rub=200,
            status="delivered",
            subscription_url=f"https://darimiru.ru/vpnbot/adapt-sub/{adapt_uuid}",
            marzban_username=f"adapt_{adapt_uuid}",
            access_expires_at=baseline_end,
        )
        order = WebOrder(
            id=709,
            order_id="order_web_retry_upgrade",
            profile_token=profile_token,
            tariff_key="adapt_new_retry",
            tariff_label="Новый тариф",
            days=30,
            amount_rub=350,
            status="paid",
            purchase_action="upgrade",
            target_order_id=primary.order_id,
            target_snapshot_plan_uuid=old_plan_uuid,
            target_snapshot_expires_at=baseline_end,
            target_snapshot_is_frozen=True,
            fulfillment_freeze_state="mutation_applied",
        )
        session.add_all([primary, order])
        await session.commit()

    status_after_mutation = {
        "subscription_uuid": adapt_uuid,
        "plan_uuid": new_plan_uuid,
        "is_active": False,
        "is_frozen": True,
        "devices": 3,
        "end_date": (datetime.utcnow() + timedelta(days=35)).isoformat(),
    }
    mock_get_status = AsyncMock(side_effect=[status_after_mutation, status_after_mutation])
    mock_list_plans = AsyncMock(return_value=[
        {"uuid": new_plan_uuid, "devices": 3, "is_active": True},
    ])
    mock_unfreeze = AsyncMock()
    mock_renew = AsyncMock()
    mock_custom_renew = AsyncMock()
    mock_upgrade = AsyncMock()
    mock_freeze = AsyncMock()
    monkeypatch.setattr("webstore.routes.AdaptAPI.get_status", mock_get_status)
    monkeypatch.setattr("webstore.routes.AdaptAPI.list_plans", mock_list_plans)
    monkeypatch.setattr("webstore.routes.AdaptAPI.unfreeze_subscription", mock_unfreeze)
    monkeypatch.setattr("webstore.routes.AdaptAPI.renew_subscription", mock_renew)
    monkeypatch.setattr("webstore.routes.AdaptAPI.renew_subscription_custom", mock_custom_renew)
    monkeypatch.setattr("webstore.routes.AdaptAPI.upgrade_subscription", mock_upgrade)
    monkeypatch.setattr("webstore.routes.AdaptAPI.freeze_subscription", mock_freeze)

    async with web_session_factory() as session:
        order = await session.scalar(
            select(WebOrder).where(WebOrder.order_id == "order_web_retry_upgrade")
        )
        await webstore_routes._fulfill_adapt_order(session, order, new_plan_uuid)
        await session.commit()
        await session.refresh(order)

    assert order.status == "delivered"
    assert order.fulfillment_freeze_state == "freeze_restored"
    mock_unfreeze.assert_not_called()
    mock_renew.assert_not_called()
    mock_custom_renew.assert_not_called()
    mock_upgrade.assert_not_called()
    mock_freeze.assert_not_called()


async def test_web_device_list_timeout_returns_504(web_session_factory, monkeypatch):
    """Interactive device list timeout returns 504 with a clear error."""
    import asyncio
    profile_token = "prof_device_timeout"
    adapt_uuid = "33333333-aaaa-bbbb-cccc-000000000705"

    mock_tariffs = [
        {"key": "adapt_basic", "label": "Basic", "days": 30, "price_rub": 200, "provider": "adapt", "adapt_plan_uuid": "plan-basic-30d"},
    ]
    monkeypatch.setattr(webstore_routes, "get_store_tariffs", lambda: mock_tariffs)
    monkeypatch.setattr(webstore_routes, "get_store_tariffs_by_key", lambda: {t["key"]: t for t in mock_tariffs})

    async def _slow_get_devices(*args, **kwargs):
        await asyncio.sleep(15)
        return []

    monkeypatch.setattr(webstore_routes, "async_session", web_session_factory)
    monkeypatch.setattr("webstore.routes.AdaptAPI.get_devices", _slow_get_devices)
    monkeypatch.setattr("webstore.routes.AdaptAPI.get_status", AsyncMock(return_value={"devices": 2}))

    async with web_session_factory() as session:
        order = WebOrder(
            id=705,
            order_id="order_dev_705",
            profile_token=profile_token,
            tariff_key="adapt_basic",
            tariff_label="Базовый",
            days=30,
            amount_rub=200,
            status="delivered",
            subscription_url=f"https://darimiru.ru/vpnbot/adapt-sub/{adapt_uuid}",
            marzban_username=f"adapt_{adapt_uuid}",
            access_expires_at=datetime.utcnow() + timedelta(days=20),
            purchase_action="new",
        )
        session.add(order)
        await session.commit()

    req = _Request(
        query={"target": "order:705"},
        cookies={webstore_routes._AUTH_COOKIE: profile_token},
    )
    resp = await webstore_routes.handle_devices_list(req)
    assert resp.status == 504
    data = json.loads(resp.text)
    assert "Время ожидания" in data["error"]


async def test_web_device_delete_timeout_returns_504_and_no_delete(web_session_factory, monkeypatch):
    """Interactive device delete timeout returns 504 and does not call delete."""
    import asyncio
    profile_token = "prof_device_del_timeout"
    adapt_uuid = "44444444-aaaa-bbbb-cccc-000000000706"
    mock_delete = AsyncMock()

    mock_tariffs = [
        {"key": "adapt_basic", "label": "Basic", "days": 30, "price_rub": 200, "provider": "adapt", "adapt_plan_uuid": "plan-basic-30d"},
    ]
    monkeypatch.setattr(webstore_routes, "get_store_tariffs", lambda: mock_tariffs)
    monkeypatch.setattr(webstore_routes, "get_store_tariffs_by_key", lambda: {t["key"]: t for t in mock_tariffs})

    async def _slow_get_devices(*args, **kwargs):
        await asyncio.sleep(15)
        return []

    monkeypatch.setattr(webstore_routes, "async_session", web_session_factory)
    monkeypatch.setattr("webstore.routes.AdaptAPI.get_devices", _slow_get_devices)
    monkeypatch.setattr("webstore.routes.AdaptAPI.delete_device", mock_delete)

    async with web_session_factory() as session:
        order = WebOrder(
            id=706,
            order_id="order_dev_del_706",
            profile_token=profile_token,
            tariff_key="adapt_basic",
            tariff_label="Базовый",
            days=30,
            amount_rub=200,
            status="delivered",
            subscription_url=f"https://darimiru.ru/vpnbot/adapt-sub/{adapt_uuid}",
            marzban_username=f"adapt_{adapt_uuid}",
            access_expires_at=datetime.utcnow() + timedelta(days=20),
            purchase_action="new",
        )
        session.add(order)
        await session.commit()

    req = _Request(
        _json_body={"target": "order:706", "device_id": 1},
        cookies={webstore_routes._AUTH_COOKIE: profile_token},
    )
    resp = await webstore_routes.handle_device_delete(req)
    assert resp.status == 504
    mock_delete.assert_not_called()


# ── Additional Provider Upgrade, Demo Authority & Frozen State Tests ─────────

def test_adapt_provider_upgrade_eligibility_uses_positive_provider_quote():
    """Verify Decimal-based provider eligibility uses a strictly positive quote."""
    from bot.services.subscription_semantics import is_adapt_provider_upgrade_eligible

    # 1. Positive provider quote -> eligible
    cur = {"adapt_plan_uuid": "plan-1", "adapt_cost_price_usd": "1.00", "days": 10}
    target_higher = {"adapt_plan_uuid": "plan-2", "adapt_cost_price_usd": "0.80", "days": 7}
    assert is_adapt_provider_upgrade_eligible(cur, target_higher) is True

    # 2. Lower provider cost with a positive quote -> eligible
    target_lower_cost = {"adapt_plan_uuid": "plan-3", "adapt_cost_price_usd": "0.35", "days": 7}
    assert is_adapt_provider_upgrade_eligible(cur, target_lower_cost) is True

    # 3. Same plan -> False (renew, not upgrade)
    same_plan = {"adapt_plan_uuid": "plan-1", "adapt_cost_price_usd": "1.00", "days": 10}
    assert is_adapt_provider_upgrade_eligible(cur, same_plan) is False

    # 4. Non-positive provider quote -> False
    target_nonpositive_quote = {"adapt_plan_uuid": "plan-4", "adapt_cost_price_usd": "0.25", "days": 2}
    assert is_adapt_provider_upgrade_eligible(cur, target_nonpositive_quote) is False

    # 5. Missing or invalid metadata -> fail closed (False)
    missing_cost = {"adapt_plan_uuid": "plan-5", "days": 10}
    missing_days = {"adapt_plan_uuid": "plan-6", "adapt_cost_price_usd": "1.00", "days": 0}
    assert is_adapt_provider_upgrade_eligible(cur, missing_cost) is False
    assert is_adapt_provider_upgrade_eligible(cur, missing_days) is False
    assert is_adapt_provider_upgrade_eligible(missing_cost, target_higher) is False

    demo_plan = {"adapt_plan_uuid": settings.adapt_demo_plan_uuid, "adapt_cost_price_usd": "0.00", "days": 3}
    assert is_adapt_provider_upgrade_eligible(demo_plan, target_higher) is False
    demo_with_metadata = {"adapt_plan_uuid": settings.adapt_demo_plan_uuid, "adapt_cost_price_usd": "0.30", "days": 3}
    assert is_adapt_provider_upgrade_eligible(demo_with_metadata, target_higher) is True
    assert is_adapt_provider_upgrade_eligible(demo_with_metadata, demo_with_metadata) is False


def test_demo_authority_mismatch_precedence():
    """Verify exact Adapt demo authority precedence."""
    from bot.services.subscription_semantics import is_adapt_trial_subscription, is_adapt_trial_tariff

    demo_uuid = settings.adapt_demo_plan_uuid

    # 1. Canonical paid AdaptSubscription + client_name ending _demo -> PAID
    sub_paid_name_demo = SimpleNamespace(
        id=1, client_name="adapt_client_demo", billing_mode="tariff", tariff_id=1,
    )
    adapt_sub_paid = SimpleNamespace(adapt_plan_uuid="paid-plan-uuid")
    assert is_adapt_trial_subscription(sub_paid_name_demo, adapt_sub=adapt_sub_paid) is False

    sub_missing_plan_demo = SimpleNamespace(
        id=5, client_name="adapt_client_demo", billing_mode="demo", tariff_id=1,
    )
    assert is_adapt_trial_subscription(sub_missing_plan_demo, adapt_sub=SimpleNamespace()) is False

    # 2. Canonical paid AdaptSubscription + billing_mode demo -> PAID for Adapt monetary logic
    sub_paid_mode_demo = SimpleNamespace(
        id=2, client_name="adapt_client_2", billing_mode="demo", tariff_id=1,
    )
    assert is_adapt_trial_subscription(sub_paid_mode_demo, adapt_sub=adapt_sub_paid) is False

    # 3. Canonical demo AdaptSubscription + stale paid Tariff -> DEMO
    sub_demo_stale_tariff = SimpleNamespace(
        id=3, client_name="adapt_client_3", billing_mode="tariff",
        tariff=SimpleNamespace(adapt_plan_uuid="paid-plan-uuid"),
    )
    adapt_sub_demo = SimpleNamespace(adapt_plan_uuid=demo_uuid)
    assert is_adapt_trial_subscription(sub_demo_stale_tariff, adapt_sub=adapt_sub_demo) is True

    # 4. Fallback to billing_mode == "demo" when AdaptSubscription is absent
    sub_no_adapt = SimpleNamespace(
        id=4, client_name="adapt_client_4", billing_mode="demo",
    )
    assert is_adapt_trial_subscription(sub_no_adapt, adapt_sub=None) is True

    # 5. 7-day paid tariff is NOT a trial tariff
    tariff_7d = SimpleNamespace(id=1, label="7 дней", days=7, adapt_plan_uuid="paid-7d-uuid")
    assert is_adapt_trial_tariff(tariff_7d) is False


async def test_telegram_flow_uses_canonical_paid_plan_over_stale_demo_fields(bot_session_factory, monkeypatch):
    monkeypatch.setattr(buy_handler, "async_session", bot_session_factory)
    async with bot_session_factory() as session:
        user = User(id=806, telegram_id=806001, username="canonical_paid")
        server = Server(id=806, name="Canonical server", host="1.1.1.1", location="Frankfurt")
        demo_tariff = Tariff(
            id=806,
            label="Тестовый доступ",
            days=3,
            price_rub=0,
            adapt_plan_uuid=settings.adapt_demo_plan_uuid,
            adapt_cost_price_usd=0.03,
        )
        paid_tariff = Tariff(
            id=807,
            label="Платный тариф",
            days=30,
            price_rub=200,
            adapt_plan_uuid="canonical-paid-plan",
            adapt_cost_price_usd=2.0,
        )
        sub = Subscription(
            id=806,
            user_id=user.id,
            server_id=server.id,
            tariff_id=demo_tariff.id,
            tariff_months=0,
            tariff_days=3,
            billing_mode="demo",
            client_name="adapt_canonical_demo",
            vpn_key="https://darimiru.ru/vpnbot/adapt-sub/canonical-paid-uuid",
            platform=Platform.IOS,
            status=SubStatus.ACTIVE,
            expires_at=datetime.utcnow() + timedelta(days=10),
        )
        adapt_sub = AdaptSubscription(
            subscription_id=sub.id,
            adapt_uuid="canonical-paid-uuid",
            adapt_plan_uuid=paid_tariff.adapt_plan_uuid,
            end_date=sub.expires_at,
        )
        session.add_all([user, server, demo_tariff, paid_tariff, sub, adapt_sub])
        await session.commit()

        targets = await buy_handler._purchase_targets(user.telegram_id)
        price = await get_purchase_price_rub(
            session,
            user=user,
            tariff=paid_tariff,
            action="renew",
            target_subscription_id=sub.id,
        )

    assert [target.id for target in targets] == [sub.id]
    assert targets[0].adapt_current_tariff.id == paid_tariff.id
    assert price == paid_tariff.price_rub


async def test_purchase_targets_keep_inactive_historical_adapt_current_plan(bot_session_factory, monkeypatch):
    monkeypatch.setattr(buy_handler, "async_session", bot_session_factory)
    mock_plans = [
        {"uuid": "historical-current-plan", "days": 30, "price_usd": "1.00", "devices": 1, "is_trial": False, "is_active": True},
        {"uuid": "historical-target-plan", "days": 30, "price_usd": "2.00", "devices": 1, "is_trial": False, "is_active": True},
    ]
    monkeypatch.setattr("bot.services.adapt_api.AdaptAPI.list_plans", AsyncMock(return_value=mock_plans))
    async with bot_session_factory() as session:
        user = User(id=808, telegram_id=808001, username="historical_plan")
        server = Server(id=808, name="Historical server", host="1.1.1.1", location="Frankfurt")
        current_tariff = Tariff(
            id=808,
            label="Архивный тариф",
            days=30,
            price_rub=200,
            is_active=False,
            adapt_plan_uuid="historical-current-plan",
            adapt_cost_price_usd=1.0,
        )
        target_tariff = Tariff(
            id=809,
            label="Доступный тариф",
            days=30,
            price_rub=350,
            is_active=True,
            adapt_plan_uuid="historical-target-plan",
            adapt_cost_price_usd=2.0,
        )
        sub = Subscription(
            id=808,
            user_id=user.id,
            server_id=server.id,
            tariff_id=current_tariff.id,
            tariff_months=1,
            tariff_days=30,
            billing_mode="tariff",
            client_name="adapt_historical",
            vpn_key="https://darimiru.ru/vpnbot/adapt-sub/historical-uuid",
            platform=Platform.ANDROID,
            status=SubStatus.ACTIVE,
            expires_at=datetime.utcnow() + timedelta(days=10),
        )
        adapt_sub = AdaptSubscription(
            subscription_id=sub.id,
            adapt_uuid="historical-uuid",
            adapt_plan_uuid=current_tariff.adapt_plan_uuid,
            end_date=sub.expires_at,
        )
        session.add_all([user, server, current_tariff, target_tariff, sub, adapt_sub])
        await session.commit()

    targets = await buy_handler._purchase_targets(user.telegram_id, upgrade_only=True)

    assert [target.id for target in targets] == [sub.id]
    assert targets[0].adapt_current_tariff.id == current_tariff.id


async def test_latest_adapt_subscription_strict_user_scoping(bot_session_factory):
    """_latest_adapt_subscription must never return another user's subscription."""
    from bot.services.subscription_service import _latest_adapt_subscription

    async with bot_session_factory() as session:
        user1 = User(id=801, telegram_id=801001, username="user1")
        user2 = User(id=802, telegram_id=802001, username="user2")
        server = Server(id=801, name="Adapt Srv", is_active=True, host="1.1.1.1", location="Frankfurt")
        sub1 = Subscription(
            id=801,
            user_id=801,
            server_id=801,
            tariff_months=1,
            tariff_days=30,
            platform=Platform.IOS,
            status=SubStatus.ACTIVE,
            client_name="adapt_client_801",
            vpn_key="https://darimiru.ru/vpnbot/adapt-sub/uuid-801",
            expires_at=datetime.utcnow() + timedelta(days=30),
        )
        adapt_sub1 = AdaptSubscription(
            subscription_id=801,
            adapt_uuid="uuid-801",
            adapt_plan_uuid="plan-801",
            end_date=datetime.utcnow() + timedelta(days=30),
        )
        session.add_all([user1, user2, server, sub1, adapt_sub1])
        await session.commit()

        # User 1 should find subscription 801
        found_sub1, found_adapt1 = await _latest_adapt_subscription(session, user_id=801)
        assert found_sub1 is not None
        assert found_sub1.id == 801

        # User 2 has NO subscriptions -> must return (None, None)
        found_sub2, found_adapt2 = await _latest_adapt_subscription(session, user_id=802)
        assert found_sub2 is None
        assert found_adapt2 is None


async def test_retail_rub_cannot_override_nonpositive_provider_quote(bot_session_factory, monkeypatch):
    """Retail price_rub cannot make a non-positive provider quote purchasable."""
    from bot.services.purchase_intent import get_purchase_price_rub

    plan_a_uuid = "plan-a-expensive-provider"
    plan_b_uuid = "plan-b-nonpositive-quote"

    async with bot_session_factory() as session:
        user = User(id=803, telegram_id=803001, username="user3")
        server = Server(id=803, name="Adapt Srv", is_active=True, host="1.1.1.1", location="Frankfurt")
        # Tariff A: cheap retail (100 RUB), with a $2.00 provider cost.
        tariff_a = Tariff(
            id=803, label="Tariff A", days=10, price_rub=100,
            adapt_plan_uuid=plan_a_uuid, adapt_cost_price_usd=2.0,
        )
        # Tariff B: expensive retail (500 RUB), with a $1.00 provider cost.
        tariff_b = Tariff(
            id=804, label="Tariff B", days=10, price_rub=500,
            adapt_plan_uuid=plan_b_uuid, adapt_cost_price_usd=0.99,
        )
        sub = Subscription(
            id=803,
            user_id=803,
            server_id=803,
            tariff_id=803,
            tariff_months=1,
            tariff_days=10,
            platform=Platform.IOS,
            status=SubStatus.ACTIVE,
            client_name="adapt_client_803",
            vpn_key="https://darimiru.ru/vpnbot/adapt-sub/uuid-803",
            expires_at=datetime.utcnow() + timedelta(days=5),
        )
        adapt_sub = AdaptSubscription(
            subscription_id=803,
            adapt_uuid="uuid-803",
            adapt_plan_uuid=plan_a_uuid,
            end_date=datetime.utcnow() + timedelta(days=5),
        )
        session.add_all([user, server, tariff_a, tariff_b, sub, adapt_sub])
        await session.commit()

        # Five remaining days make the provider upgrade quote negative.
        with pytest.raises(ValueError, match="Улучшение доступно только на более дорогой тариф"):
            await get_purchase_price_rub(
                session, user=user, tariff=tariff_b, action="upgrade",
                target_subscription_id=803,
            )


async def test_active_and_expired_nonpositive_provider_quote_rejected_before_payment(web_session_factory, monkeypatch):
    """Web checkout rejects a non-positive provider quote before payment creation."""
    adapt_uuid = "55555555-aaaa-bbbb-cccc-000000000805"
    old_plan_uuid = "plan-cur-expensive"
    target_plan_uuid = "plan-tgt-nonpositive-quote"

    mock_tariffs = [
        {"key": "tariff_cur", "label": "Current", "days": 10, "price_rub": 100, "provider": "adapt", "adapt_plan_uuid": old_plan_uuid, "adapt_cost_price_usd": 2.0},
        {"key": "tariff_target", "label": "Target", "days": 10, "price_rub": 500, "provider": "adapt", "adapt_plan_uuid": target_plan_uuid, "adapt_cost_price_usd": 0.5},
    ]
    monkeypatch.setattr(webstore_routes, "get_store_tariffs", lambda: mock_tariffs)
    monkeypatch.setattr(webstore_routes, "get_store_tariffs_by_key", lambda: {t["key"]: t for t in mock_tariffs})
    monkeypatch.setattr(webstore_routes, "async_session", web_session_factory)
    monkeypatch.setitem(sys.modules, "yookassa", _FakeYooKassa())
    monkeypatch.setattr(webstore_routes.settings, "yookassa_shop_id", "shop")
    monkeypatch.setattr(webstore_routes.settings, "yookassa_secret_key", "secret")

    # 1. Active subscription
    async with web_session_factory() as session:
        order_active = WebOrder(
            id=805,
            order_id="order_active_805",
            profile_token="prof_805",
            tariff_key="tariff_cur",
            tariff_label="Current",
            days=10,
            amount_rub=100,
            status="delivered",
            subscription_url=f"https://darimiru.ru/vpnbot/adapt-sub/{adapt_uuid}",
            marzban_username=f"adapt_{adapt_uuid}",
            access_expires_at=datetime.utcnow() + timedelta(days=5),
            purchase_action="new",
        )
        session.add(order_active)
        await session.commit()

    req_active = _Request(
        _json_body={
            "tariff_key": "tariff_target",
            "purchase_action": "upgrade",
            "target_order_id": "order_active_805",
        },
        cookies={webstore_routes._AUTH_COOKIE: "prof_805"},
    )
    resp_active = await webstore_routes.handle_create_order(req_active)
    assert resp_active.status == 400
    assert "Улучшение доступно только на более дорогой тариф" in json.loads(resp_active.text)["error"]

    # 2. Expired subscription
    adapt_uuid_exp = "55555555-aaaa-bbbb-cccc-000000000806"
    async with web_session_factory() as session:
        order_expired = WebOrder(
            id=806,
            order_id="order_expired_806",
            profile_token="prof_805",
            tariff_key="tariff_cur",
            tariff_label="Current",
            days=10,
            amount_rub=100,
            status="delivered",
            subscription_url=f"https://darimiru.ru/vpnbot/adapt-sub/{adapt_uuid_exp}",
            marzban_username=f"adapt_{adapt_uuid_exp}",
            access_expires_at=datetime.utcnow() - timedelta(days=5),
            purchase_action="new",
        )
        session.add(order_expired)
        await session.commit()

    req_expired = _Request(
        _json_body={
            "tariff_key": "tariff_target",
            "purchase_action": "upgrade",
            "target_order_id": "order_expired_806",
        },
        cookies={webstore_routes._AUTH_COOKIE: "prof_805"},
    )
    resp_expired = await webstore_routes.handle_create_order(req_expired)
    assert resp_expired.status == 400
    assert "Улучшение доступно только на более дорогой тариф" in json.loads(resp_expired.text)["error"]


async def test_known_checkout_zero_adapt_api_calls(web_session_factory, monkeypatch):
    """Known subscription checkout must not make any live Adapt API calls."""
    adapt_uuid = "66666666-aaaa-bbbb-cccc-000000000807"
    plan_uuid = "plan-known-30d"

    mock_tariffs = [
        {"key": "adapt_basic", "label": "Basic", "days": 30, "price_rub": 200, "provider": "adapt", "adapt_plan_uuid": plan_uuid, "adapt_cost_price_usd": 1.0},
    ]
    monkeypatch.setattr(webstore_routes, "get_store_tariffs", lambda: mock_tariffs)
    monkeypatch.setattr(webstore_routes, "get_store_tariffs_by_key", lambda: {t["key"]: t for t in mock_tariffs})
    monkeypatch.setattr(webstore_routes, "async_session", web_session_factory)
    monkeypatch.setitem(sys.modules, "yookassa", _FakeYooKassa())

    mock_get_status = AsyncMock()
    mock_list_plans = AsyncMock()
    monkeypatch.setattr("webstore.routes.AdaptAPI.get_status", mock_get_status)
    monkeypatch.setattr("webstore.routes.AdaptAPI.list_plans", mock_list_plans)

    async with web_session_factory() as session:
        order = WebOrder(
            id=807,
            order_id="order_known_807",
            profile_token="prof_known_807",
            tariff_key="adapt_basic",
            tariff_label="Basic",
            days=30,
            amount_rub=200,
            status="delivered",
            subscription_url=f"https://darimiru.ru/vpnbot/adapt-sub/{adapt_uuid}",
            marzban_username=f"adapt_{adapt_uuid}",
            access_expires_at=datetime.utcnow() + timedelta(days=20),
            purchase_action="new",
        )
        session.add(order)
        await session.commit()

    req = _Request(
        _json_body={
            "tariff_key": "adapt_basic",
            "purchase_action": "renew",
            "target_order_id": "order_known_807",
        },
        cookies={webstore_routes._AUTH_COOKIE: "prof_known_807"},
    )
    monkeypatch.setattr(webstore_routes.settings, "yookassa_shop_id", "test_shop")
    monkeypatch.setattr(webstore_routes.settings, "yookassa_secret_key", "test_key")

    resp = await webstore_routes.handle_create_order(req)
    assert resp.status == 200

    # Ensure ZERO Adapt API calls occurred during checkout
    mock_get_status.assert_not_called()
    mock_list_plans.assert_not_called()


# ==============================================================================
# Authoritative Provider Metadata & Device Entitlement Tests
# ==============================================================================


async def test_provider_metadata_is_authoritative_over_local_tariff_fields_for_device_entitlement(
    bot_session_factory, monkeypatch
):
    """Local tariffs with positive costs cannot bypass provider device count (5 devices -> 3 devices rejected)."""
    current_plan_uuid = "provider-7d-5dev"
    target_plan_uuid = "provider-7d-3dev"
    monkeypatch.setattr(buy_handler, "async_session", bot_session_factory)

    provider_plans = [
        {"uuid": current_plan_uuid, "days": 7, "price_usd": "0.50", "devices": 5, "is_trial": False, "is_active": True},
        {"uuid": target_plan_uuid, "days": 7, "price_usd": "1.00", "devices": 3, "is_trial": False, "is_active": True},
    ]
    monkeypatch.setattr("bot.services.adapt_api.AdaptAPI.list_plans", AsyncMock(return_value=provider_plans))

    async with bot_session_factory() as session:
        user = User(id=901, telegram_id=901001, username="device_user")
        server = Server(id=901, name="S1", host="1.1.1.1", location="Frankfurt", is_active=True)
        # Local tariffs have positive cost prices
        current_tariff = Tariff(
            id=901,
            label="7 дней 5 устройств",
            days=7,
            price_rub=75,
            is_active=True,
            adapt_plan_uuid=current_plan_uuid,
            adapt_cost_price_usd=0.50,
            device_count=5,
        )
        target_tariff = Tariff(
            id=902,
            label="7 дней 3 устройства",
            days=7,
            price_rub=120,
            is_active=True,
            adapt_plan_uuid=target_plan_uuid,
            adapt_cost_price_usd=1.00,
            device_count=3,
        )
        sub = Subscription(
            id=901,
            user_id=user.id,
            server_id=server.id,
            tariff_id=current_tariff.id,
            tariff_months=0,
            tariff_days=7,
            billing_mode="tariff",
            status=SubStatus.EXPIRED,
            vpn_key="https://darimiru.ru/vpnbot/adapt-sub/dev-uuid",
            client_name="adapt_dev_test",
            platform=Platform.IOS,
            expires_at=datetime.utcnow() - timedelta(days=2),
        )
        adapt_sub = AdaptSubscription(
            subscription_id=sub.id,
            adapt_uuid="dev-uuid",
            adapt_plan_uuid=current_plan_uuid,
            end_date=sub.expires_at,
        )
        session.add_all([user, server, current_tariff, target_tariff, sub, adapt_sub])
        await session.commit()

        # 1. Direct semantics check: target_devices (3) < current_devices (5) => False
        provider_pair = await get_purchase_price_rub.__globals__["get_paid_adapt_upgrade_plans"](
            session, current_tariff, target_tariff
        )
        assert provider_pair is not None
        assert is_adapt_customer_upgrade_eligible(provider_pair[0], provider_pair[1], expires_at=sub.expires_at) is False

        # 2. Quote validation fails
        with pytest.raises(ValueError, match="Улучшение доступно только на более дорогой тариф"):
            await get_purchase_price_rub(
                session,
                user=user,
                tariff=target_tariff,
                action="upgrade",
                target_subscription_id=sub.id,
            )

    # 3. Excluded from Telegram upgrade target UI
    upgrade_callback = SimpleNamespace(
        from_user=SimpleNamespace(id=user.telegram_id),
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )
    await buy_handler._open_upgrade_target(upgrade_callback, sub.id)
    buttons = [
        button
        for row in upgrade_callback.message.edit_text.call_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    callbacks = {b.callback_data for b in buttons if b.callback_data}
    assert f"tariff_{target_tariff.id}~u~{sub.id}" not in callbacks


async def test_provider_plan_missing_devices_fails_closed_for_upgrade(bot_session_factory, monkeypatch):
    """If provider /plans/list lacks device count, paid upgrade must fail closed."""
    current_plan_uuid = "provider-cur-nodev"
    target_plan_uuid = "provider-tgt-nodev"
    monkeypatch.setattr(buy_handler, "async_session", bot_session_factory)

    provider_plans = [
        {"uuid": current_plan_uuid, "days": 30, "price_usd": "1.00", "devices": 1, "is_trial": False, "is_active": True},
        {"uuid": target_plan_uuid, "days": 30, "price_usd": "3.00", "devices": None, "is_trial": False, "is_active": True},
    ]
    monkeypatch.setattr("bot.services.adapt_api.AdaptAPI.list_plans", AsyncMock(return_value=provider_plans))

    async with bot_session_factory() as session:
        user = User(id=902, telegram_id=902001, username="nodev_user")
        server = Server(id=902, name="S1", host="1.1.1.1", location="Frankfurt", is_active=True)
        current_tariff = Tariff(id=903, label="Cur", days=30, price_rub=100, is_active=True, adapt_plan_uuid=current_plan_uuid)
        target_tariff = Tariff(id=904, label="Tgt", days=30, price_rub=250, is_active=True, adapt_plan_uuid=target_plan_uuid)
        sub = Subscription(
            id=902,
            user_id=user.id,
            server_id=server.id,
            tariff_id=current_tariff.id,
            tariff_months=1,
            tariff_days=30,
            billing_mode="tariff",
            status=SubStatus.ACTIVE,
            vpn_key="https://darimiru.ru/vpnbot/adapt-sub/nodev-uuid",
            client_name="adapt_nodev",
            platform=Platform.IOS,
            expires_at=datetime.utcnow() + timedelta(days=10),
        )
        adapt_sub = AdaptSubscription(subscription_id=sub.id, adapt_uuid="nodev-uuid", adapt_plan_uuid=current_plan_uuid, end_date=sub.expires_at)
        session.add_all([user, server, current_tariff, target_tariff, sub, adapt_sub])
        await session.commit()

        # get_paid_adapt_upgrade_plans returns None because target is missing devices
        resolved = await get_purchase_price_rub.__globals__["get_paid_adapt_upgrade_plans"](session, current_tariff, target_tariff)
        assert resolved is None

        with pytest.raises(ValueError, match="Улучшение доступно только на более дорогой тариф"):
            await get_purchase_price_rub(
                session,
                user=user,
                tariff=target_tariff,
                action="upgrade",
                target_subscription_id=sub.id,
            )


async def test_provider_plan_missing_entirely_fails_closed_for_upgrade(bot_session_factory, monkeypatch):
    """If target plan is not in /plans/list, paid upgrade must fail closed."""
    current_plan_uuid = "provider-cur-exists"
    target_plan_uuid = "provider-tgt-missing"
    monkeypatch.setattr(buy_handler, "async_session", bot_session_factory)

    provider_plans = [
        {"uuid": current_plan_uuid, "days": 30, "price_usd": "1.00", "devices": 1, "is_trial": False, "is_active": True},
    ]
    monkeypatch.setattr("bot.services.adapt_api.AdaptAPI.list_plans", AsyncMock(return_value=provider_plans))

    async with bot_session_factory() as session:
        user = User(id=903, telegram_id=903001, username="missing_user")
        server = Server(id=903, name="S1", host="1.1.1.1", location="Frankfurt", is_active=True)
        current_tariff = Tariff(id=905, label="Cur", days=30, price_rub=100, is_active=True, adapt_plan_uuid=current_plan_uuid)
        target_tariff = Tariff(id=906, label="Tgt", days=30, price_rub=250, is_active=True, adapt_plan_uuid=target_plan_uuid)
        sub = Subscription(
            id=903,
            user_id=user.id,
            server_id=server.id,
            tariff_id=current_tariff.id,
            tariff_months=1,
            tariff_days=30,
            billing_mode="tariff",
            status=SubStatus.ACTIVE,
            vpn_key="https://darimiru.ru/vpnbot/adapt-sub/missing-uuid",
            client_name="adapt_missing",
            platform=Platform.IOS,
            expires_at=datetime.utcnow() + timedelta(days=10),
        )
        adapt_sub = AdaptSubscription(subscription_id=sub.id, adapt_uuid="missing-uuid", adapt_plan_uuid=current_plan_uuid, end_date=sub.expires_at)
        session.add_all([user, server, current_tariff, target_tariff, sub, adapt_sub])
        await session.commit()

        resolved = await get_purchase_price_rub.__globals__["get_paid_adapt_upgrade_plans"](session, current_tariff, target_tariff)
        assert resolved is None

        with pytest.raises(ValueError, match="Улучшение доступно только на более дорогой тариф"):
            await get_purchase_price_rub(
                session,
                user=user,
                tariff=target_tariff,
                action="upgrade",
                target_subscription_id=sub.id,
            )


async def test_same_plan_renew_succeeds_when_upgrade_provider_metadata_unavailable(
    bot_session_factory, monkeypatch
):
    """Same-plan renewal works reliably even when provider /plans/list is empty or unavailable."""
    plan_uuid = "provider-renew-plan"
    monkeypatch.setattr(buy_handler, "async_session", bot_session_factory)
    monkeypatch.setattr("bot.services.adapt_api.AdaptAPI.list_plans", AsyncMock(return_value=[]))

    async with bot_session_factory() as session:
        user = User(id=904, telegram_id=904001, username="renew_resilience_user")
        server = Server(id=904, name="S1", host="1.1.1.1", location="Frankfurt", is_active=True)
        tariff = Tariff(id=907, label="Basic 30d", days=30, price_rub=200, is_active=True, adapt_plan_uuid=plan_uuid)
        sub = Subscription(
            id=904,
            user_id=user.id,
            server_id=server.id,
            tariff_id=tariff.id,
            tariff_months=1,
            tariff_days=30,
            billing_mode="tariff",
            status=SubStatus.EXPIRED,
            vpn_key="https://darimiru.ru/vpnbot/adapt-sub/renew-uuid",
            client_name="adapt_renew_test",
            platform=Platform.IOS,
            expires_at=datetime.utcnow() - timedelta(days=3),
        )
        adapt_sub = AdaptSubscription(subscription_id=sub.id, adapt_uuid="renew-uuid", adapt_plan_uuid=plan_uuid, end_date=sub.expires_at)
        session.add_all([user, server, tariff, sub, adapt_sub])
        await session.commit()

        price = await get_purchase_price_rub(
            session,
            user=user,
            tariff=tariff,
            action="renew",
            target_subscription_id=sub.id,
        )
        assert price == 200
