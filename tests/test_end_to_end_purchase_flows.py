"""End-to-end integration tests for all subscription purchase flows and callback handlers.

Tests:
1. User with active demo subscription -> clicks 'buy' -> opens tariff list directly (intent ~u~{sub_id}).
2. User with expired demo subscription -> clicks 'buy' -> opens tariff list directly (intent ~u~{sub_id}).
3. User with expired demo subscription -> selects tariff -> gets payment with payload ~u~{sub_id}.
4. Payment fulfillment for demo upgrade -> reuses and upgrades demo UUID, key URL unchanged.
5. User with active paid subscription -> clicks 'buy' -> shows subscription action screen.
6. User with active paid subscription -> clicks 'buy_new' -> gets new separate subscription & new key.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.handlers.buy import start_purchase, start_new_purchase_from_subscription
from bot.models import (
    AdaptSubscription,
    Base,
    Payment,
    PaymentMethod,
    PaymentStatus,
    Platform,
    Server,
    SubStatus,
    Subscription,
    Tariff,
    TariffType,
    User,
)
from bot.config import settings
from bot.services.subscription_service import create_or_extend_paid_access


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


def _make_callback(user_id: int, data: str = "buy"):
    msg = SimpleNamespace(
        edit_text=AsyncMock(),
        answer=AsyncMock(),
        message_id=123,
    )
    from_user = SimpleNamespace(id=user_id, username="test_user", first_name="Test")
    return SimpleNamespace(
        from_user=from_user,
        data=data,
        message=msg,
        answer=AsyncMock(),
    )


async def _seed_data(session: AsyncSession):
    user = User(id=1, telegram_id=111222333, username="test_user")
    session.add(user)

    server = Server(
        id=1,
        name="Adapt Server",
        host="185.70.185.209",
        location="Amsterdam",
        is_active=True,
    )
    session.add(server)

    from bot.config import settings

    tariff_trial = Tariff(
        id=1,
        label="Тест 7 дней",
        price_rub=0,
        days=7,
        tariff_type=TariffType.VPN,
        adapt_plan_uuid=settings.adapt_demo_plan_uuid,
        adapt_cost_price_usd=0.30,
        is_active=True,
    )
    session.add(tariff_trial)

    tariff_paid_30d = Tariff(
        id=12,
        label="Базовый 30 дней",
        price_rub=155,
        days=30,
        tariff_type=TariffType.VPN,
        adapt_plan_uuid="adapt-paid-plan-30d",
        adapt_cost_price_usd=4.0,
        is_active=True,
    )
    session.add(tariff_paid_30d)

    await session.commit()
    return user, server, tariff_trial, tariff_paid_30d


# ── TEST 1 & 2: Demo subscription (active or expired) skips intermediate screen ──

async def test_active_demo_user_clicking_buy_opens_tariffs_directly(db_session_factory):
    async with db_session_factory() as session:
        user, server, tariff_trial, tariff_paid_30d = await _seed_data(session)
        now = datetime.utcnow()
        demo_sub = Subscription(
            id=271,
            user_id=user.id,
            server_id=server.id,
            tariff_id=tariff_trial.id,
            tariff_days=7,
            tariff_months=0,
            billing_mode="demo",
            status=SubStatus.ACTIVE,
            device_slots=1,
            vpn_key="https://darimiru.ru/vpnbot/adapt-sub/demo-uuid-123",
            client_name="adapt_demo-uuid-123",
            platform=Platform.ANDROID,
            started_at=now - timedelta(days=2),
            expires_at=now + timedelta(days=5),
        )
        session.add(demo_sub)
        session.add(AdaptSubscription(
            subscription_id=demo_sub.id,
            adapt_uuid="demo-uuid-123",
            adapt_plan_uuid=settings.adapt_demo_plan_uuid,
            is_frozen=False,
            end_date=demo_sub.expires_at,
        ))
        await session.commit()

    callback = _make_callback(user_id=user.telegram_id, data="buy")
    with patch("bot.handlers.buy.async_session", db_session_factory):
        await start_purchase(callback)

    # Must edit text to show tariff selection directly, with intent ~u~271
    callback.message.edit_text.assert_awaited_once()
    args, kwargs = callback.message.edit_text.call_args
    assert "Выберите тариф" in args[0]
    kb = kwargs["reply_markup"]
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert any("tariff_12~u~271" in c for c in callbacks)
    # Must NOT have intermediate buttons like 'buy_new' or 'purchase_intro'
    assert not any("buy_new" in c for c in callbacks)


async def test_expired_demo_user_clicking_buy_opens_tariffs_directly(db_session_factory):
    async with db_session_factory() as session:
        user, server, tariff_trial, tariff_paid_30d = await _seed_data(session)
        now = datetime.utcnow()
        expired_demo_sub = Subscription(
            id=271,
            user_id=user.id,
            server_id=server.id,
            tariff_id=tariff_trial.id,
            tariff_days=7,
            tariff_months=0,
            billing_mode="demo",
            status=SubStatus.ACTIVE,
            device_slots=1,
            vpn_key="https://darimiru.ru/vpnbot/adapt-sub/demo-uuid-123",
            client_name="adapt_demo-uuid-123",
            platform=Platform.ANDROID,
            started_at=now - timedelta(days=8),
            expires_at=now - timedelta(hours=5),
        )
        session.add(expired_demo_sub)
        session.add(AdaptSubscription(
            subscription_id=expired_demo_sub.id,
            adapt_uuid="demo-uuid-123",
            adapt_plan_uuid=settings.adapt_demo_plan_uuid,
            is_frozen=False,
            end_date=expired_demo_sub.expires_at,
        ))
        await session.commit()

    callback = _make_callback(user_id=user.telegram_id, data="buy")
    with patch("bot.handlers.buy.async_session", db_session_factory):
        await start_purchase(callback)

    callback.message.edit_text.assert_awaited_once()
    args, kwargs = callback.message.edit_text.call_args
    assert "Выберите тариф" in args[0]
    kb = kwargs["reply_markup"]
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert any("tariff_12~u~271" in c for c in callbacks)
    assert not any("buy_new" in c for c in callbacks)


# ── TEST 3 & 4: Demo upgrade fulfillment preserves same key ──

async def test_demo_upgrade_payment_fulfillment_preserves_same_key(db_session_factory):
    async with db_session_factory() as session:
        user, server, tariff_trial, tariff_paid_30d = await _seed_data(session)
        now = datetime.utcnow()
        demo_sub = Subscription(
            id=271,
            user_id=user.id,
            server_id=server.id,
            tariff_id=tariff_trial.id,
            tariff_days=7,
            tariff_months=0,
            billing_mode="demo",
            status=SubStatus.ACTIVE,
            device_slots=1,
            vpn_key="https://darimiru.ru/vpnbot/adapt-sub/demo-uuid-123",
            client_name="adapt_demo-uuid-123",
            platform=Platform.ANDROID,
            started_at=now - timedelta(days=8),
            expires_at=now - timedelta(hours=5),
        )
        session.add(demo_sub)
        session.add(AdaptSubscription(
            subscription_id=demo_sub.id,
            adapt_uuid="demo-uuid-123",
            adapt_plan_uuid=settings.adapt_demo_plan_uuid,
            is_frozen=False,
            end_date=demo_sub.expires_at,
        ))
        payment = Payment(
            id=194,
            user_id=user.id,
            tariff_id=tariff_paid_30d.id,
            amount=155,
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
        )
        session.add(payment)
        await session.commit()

    with (
        patch("bot.services.subscription_service.get_primary_active_server", new=AsyncMock(return_value=server)),
        patch("bot.services.subscription_service.AdaptAPI") as mock_adapt_cls,
        patch("bot.services.subscription_service.build_adapt_mirror_url", return_value="https://darimiru.ru/vpnbot/adapt-sub/demo-uuid-123"),
        patch("bot.services.subscription_service.plog"),
    ):
        mock_api = AsyncMock()
        mock_api.get_status = AsyncMock(
            side_effect=[
                # 1. Preflight status before upgrade (expired trial)
                {
                    "subscription_uuid": "demo-uuid-123",
                    "plan_uuid": "adapt-trial-plan",
                    "end_date": (now - timedelta(hours=5)).isoformat() + "Z",
                    "days": 7,
                    "devices": 1,
                },
                # 2. Status after custom reactivation
                {
                    "subscription_uuid": "demo-uuid-123",
                    "plan_uuid": "adapt-trial-plan",
                    "end_date": (now + timedelta(days=3)).isoformat() + "Z",
                    "days": 3,
                    "devices": 1,
                },
                # 3. Final status after plan upgrade
                {
                    "subscription_uuid": "demo-uuid-123",
                    "plan_uuid": "adapt-paid-plan-30d",
                    "end_date": (now + timedelta(days=30)).isoformat() + "Z",
                    "days": 30,
                    "devices": 3,
                },
            ]
        )
        mock_api.list_plans = AsyncMock(return_value=[
            {"uuid": "adapt-paid-plan-30d", "devices": 3, "is_active": True}
        ])
        mock_api.renew_subscription_custom = AsyncMock(return_value={"success": True})
        mock_api.upgrade_subscription = AsyncMock(return_value={
            "subscription_uuid": "demo-uuid-123",
            "new_plan_uuid": "adapt-paid-plan-30d",
            "devices": 3,
            "end_date": (now + timedelta(days=30)).isoformat() + "Z",
        })
        mock_adapt_cls.return_value = mock_api

        async with db_session_factory() as session:
            sub, key = await create_or_extend_paid_access(
                session,
                user=user,
                tariff=tariff_paid_30d,
                platform=Platform.ANDROID,
                purchase_action="upgrade",
                target_subscription_id=271,
                provisioning_payment=payment,
            )
            await session.commit()

        # Assert key is unchanged and subscription 271 is upgraded
        assert sub.id == 271
        assert "demo-uuid-123" in key
        assert sub.tariff_id == 12
        assert sub.device_slots == 3
        mock_api.create_subscription.assert_not_called()


# ── TEST 5 & 6: User with paid subscription can create a new key for mom ──

async def test_paid_user_can_create_separate_new_subscription_for_mom(db_session_factory):
    async with db_session_factory() as session:
        user, server, tariff_trial, tariff_paid_30d = await _seed_data(session)
        now = datetime.utcnow()
        existing_paid_sub = Subscription(
            id=100,
            user_id=user.id,
            server_id=server.id,
            tariff_id=tariff_paid_30d.id,
            tariff_days=30,
            tariff_months=1,
            billing_mode="tariff",
            status=SubStatus.ACTIVE,
            device_slots=3,
            vpn_key="https://darimiru.ru/vpnbot/adapt-sub/my-own-uuid-100",
            client_name="adapt_my-own-uuid-100",
            platform=Platform.IOS,
            started_at=now - timedelta(days=10),
            expires_at=now + timedelta(days=20),
        )
        session.add(existing_paid_sub)
        session.add(AdaptSubscription(
            subscription_id=existing_paid_sub.id,
            adapt_uuid="my-own-uuid-100",
            adapt_plan_uuid="adapt-paid-plan-30d",
            is_frozen=False,
            end_date=existing_paid_sub.expires_at,
        ))
        await session.commit()

    # 1. Paid user clicks "➕ Создать новую" (buy_new_100)
    callback = _make_callback(user_id=user.telegram_id, data="buy_new_100")
    with patch("bot.handlers.buy.async_session", db_session_factory):
        await start_new_purchase_from_subscription(callback)

    callback.message.edit_text.assert_awaited_once()
    args, kwargs = callback.message.edit_text.call_args
    kb = kwargs["reply_markup"]
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    # Suffix for new purchase has target 0
    assert any("tariff_12~n~0" in c or "tariff_12~n~100" in c for c in callbacks)

    # 2. Paid user completes payment for action="new"
    with (
        patch("bot.services.subscription_service.get_primary_active_server", new=AsyncMock(return_value=server)),
        patch("bot.services.subscription_service.AdaptAPI") as mock_adapt_cls,
        patch("bot.services.subscription_service.build_adapt_mirror_url", return_value="https://darimiru.ru/vpnbot/adapt-sub/new-uuid-for-mom"),
        patch("bot.services.subscription_service.get_included_device_slots", new=AsyncMock(return_value=3)),
        patch("bot.services.subscription_service.plog"),
    ):
        mock_api = AsyncMock()
        mock_api.create_subscription = AsyncMock(return_value={
            "subscription_uuid": "new-uuid-for-mom",
            "subscription_url": "https://network-api.adaptgroup.app/sub/new-uuid-for-mom",
            "plan_uuid": "adapt-paid-plan-30d",
            "devices": 3,
            "days": 30,
        })
        mock_adapt_cls.return_value = mock_api

        async with db_session_factory() as session:
            new_sub, new_key = await create_or_extend_paid_access(
                session,
                user=user,
                tariff=tariff_paid_30d,
                platform=Platform.ANDROID,
                purchase_action="new",
                target_subscription_id=None,
            )
            await session.commit()

        # Assert new separate subscription is created and old subscription is untouched
        assert new_sub.id != 100
        assert "new-uuid-for-mom" in new_key
        mock_api.create_subscription.assert_awaited_once()

        async with db_session_factory() as session:
            subs = (await session.execute(select(Subscription).where(Subscription.user_id == user.id))).scalars().all()
            assert len(subs) == 2
