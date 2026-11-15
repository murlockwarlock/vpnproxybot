"""Tests for renewing expired subscriptions with any active tariff (user and admin flows)."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import pytest_asyncio
from sqlalchemy import select

from bot.models import (
    Base,
    AdaptSubscription,
    Platform,
    Server,
    Subscription,
    SubStatus,
    Tariff,
    TariffType,
    User,
)


@pytest_asyncio.fixture
async def sqlite_engine():
    """Create in-memory SQLite engine with all tables."""
    from sqlalchemy.ext.asyncio import create_async_engine
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.mark.asyncio
async def test_expired_subscription_get_purchase_price_allows_fewer_devices(sqlite_engine):
    """An expired 3-device subscription must be allowed to quote and purchase a 1-device tariff at full price."""
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from bot.services.purchase_intent import get_purchase_price_rub

    session_factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with session_factory() as session:
        user = User(telegram_id=469203658, full_name="Customer", platform=Platform.ANDROID)
        server = Server(name="NL-1", host="127.0.0.1", location="Amsterdam")
        session.add_all([user, server])
        await session.commit()
        await session.refresh(user)
        await session.refresh(server)

        # Current tariff: 14 days, 3 devices (plan-14d-3dev)
        tariff_14d_3dev = Tariff(
            days=14,
            label="Базовый • 14 дн • 3📱• 18 Гб⚡️",
            price_rub=90,
            tariff_type=TariffType.VPN,
            is_active=True,
            adapt_plan_uuid="plan-14d-3dev",
        )
        # Target tariff: 30 days, 1 device (plan-30d-1dev) - FEWER devices!
        tariff_30d_1dev = Tariff(
            days=30,
            label="Базовый • 30 дн • 1📱• 31 Гб⚡️",
            price_rub=125,
            tariff_type=TariffType.VPN,
            is_active=True,
            adapt_plan_uuid="plan-30d-1dev",
        )
        session.add_all([tariff_14d_3dev, tariff_30d_1dev])
        await session.commit()

        # Expired subscription #298
        sub = Subscription(
            user_id=user.id,
            server_id=server.id,
            tariff_id=tariff_14d_3dev.id,
            tariff_months=1,
            tariff_days=14,
            device_slots=3,
            platform=Platform.ANDROID,
            status=SubStatus.EXPIRED,
            vpn_key="https://darimiru.ru/vpnbot/adapt-sub/67c38c64-3c4e-4b8a-8b9d-a8b9da4de1f8",
            client_name="adapt_67c38c64-3c4e-4b8a-8b9d-a8b9da4de1f8",
            expires_at=datetime.utcnow() - timedelta(days=4),
        )
        session.add(sub)
        await session.commit()
        await session.refresh(sub)

        adapt_sub = AdaptSubscription(
            subscription_id=sub.id,
            adapt_uuid="67c38c64-3c4e-4b8a-8b9d-a8b9da4de1f8",
            adapt_plan_uuid="plan-14d-3dev",
            end_date=datetime.utcnow() - timedelta(days=4),
            is_frozen=False,
        )
        session.add(adapt_sub)
        await session.commit()

        # 1. Test get_purchase_price_rub with action="upgrade"
        price_upgrade = await get_purchase_price_rub(
            session,
            user=user,
            tariff=tariff_30d_1dev,
            action="upgrade",
            target_subscription_id=sub.id,
        )
        assert price_upgrade == 125

        # 2. Test get_purchase_price_rub with action="renew" to a different plan
        price_renew = await get_purchase_price_rub(
            session,
            user=user,
            tariff=tariff_30d_1dev,
            action="renew",
            target_subscription_id=sub.id,
        )
        assert price_renew == 125


@pytest.mark.asyncio
async def test_admin_gen_key_shows_single_renew_and_all_tariffs_for_expired_sub(sqlite_engine):
    """Admin key flow must show a single '↻ Продлить' for expired sub, which lists all active tariffs."""
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from bot.handlers.admin import admin_gen_key_action, _build_manual_key_tariff_kb

    session_factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with session_factory() as session:
        user = User(telegram_id=469203658, full_name="Customer", platform=Platform.ANDROID)
        server = Server(name="NL-1", host="127.0.0.1", location="Amsterdam")
        session.add_all([user, server])
        await session.commit()
        await session.refresh(user)
        await session.refresh(server)

        t14_3 = Tariff(
            days=14, label="Базовый • 14 дн • 3📱• 18 Гб⚡️", price_rub=90,
            tariff_type=TariffType.VPN, is_active=True, adapt_plan_uuid="plan-14d-3dev",
        )
        t30_1 = Tariff(
            days=30, label="Базовый • 30 дн • 1📱• 31 Гб⚡️", price_rub=125,
            tariff_type=TariffType.VPN, is_active=True, adapt_plan_uuid="plan-30d-1dev",
        )
        t30_3 = Tariff(
            days=30, label="🔰Базовый • 30 дн • 3📱• 40 Гб⚡️", price_rub=155,
            tariff_type=TariffType.VPN, is_active=True, adapt_plan_uuid="plan-30d-3dev",
        )
        session.add_all([t14_3, t30_1, t30_3])
        await session.commit()

        sub = Subscription(
            user_id=user.id, server_id=server.id, tariff_id=t14_3.id,
            tariff_months=1, tariff_days=14, device_slots=3,
            platform=Platform.ANDROID, status=SubStatus.EXPIRED,
            vpn_key="https://darimiru.ru/vpnbot/adapt-sub/uuid-123",
            client_name="adapt_uuid-123",
            expires_at=datetime.utcnow() - timedelta(days=2),
        )
        session.add(sub)
        await session.commit()
        await session.refresh(sub)

        adapt_sub = AdaptSubscription(
            subscription_id=sub.id,
            adapt_uuid="uuid-123",
            adapt_plan_uuid="plan-14d-3dev",
            end_date=datetime.utcnow() - timedelta(days=2),
        )
        session.add(adapt_sub)
        await session.commit()
        sub_id = sub.id

    # Test _build_manual_key_tariff_kb renders devices cleanly
    kb_builder = _build_manual_key_tariff_kb([t14_3, t30_1, t30_3], back_callback="back")
    markup = kb_builder.as_markup()
    button_texts = [btn.text for row in markup.inline_keyboard for btn in row]
    assert any("1 мес. • 1📱 | 125₽" in txt for txt in button_texts)
    assert any("1 мес. • 3📱 | 155₽" in txt for txt in button_texts)
    assert any("14 дней • 3📱 | 90₽" in txt for txt in button_texts)

    # Test admin_gen_key_action when clicking adm_key_action_renew_{sub_id}
    callback = MagicMock()
    callback.from_user.id = 999999999
    callback.data = f"adm_key_action_renew_{sub_id}"
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()
    callback.answer = AsyncMock()

    mock_state = MagicMock()
    mock_state.get_data = AsyncMock(return_value={
        "manual_key_user": 469203658,
        "manual_key_back_callback": "adm_usr_key_469203658",
    })
    mock_state.update_data = AsyncMock()

    with patch("bot.handlers.admin._is_admin", return_value=True), \
         patch("bot.handlers.admin.async_session", session_factory):
        await admin_gen_key_action(callback, mock_state)

    callback.message.edit_text.assert_awaited_once()
    edit_call = callback.message.edit_text.await_args
    assert "Выберите тариф для продления:" in edit_call.args[0]
    shown_kb = edit_call.kwargs["reply_markup"]
    shown_callbacks = [b.callback_data for row in shown_kb.inline_keyboard for b in row]

    # Verify all 3 tariffs (including 1-device 30-day tariff!) are available
    assert f"adm_key_tariff_{t14_3.id}" in shown_callbacks
    assert f"adm_key_tariff_{t30_1.id}" in shown_callbacks
    assert f"adm_key_tariff_{t30_3.id}" in shown_callbacks


@pytest.mark.asyncio
async def test_adapt_paid_subscription_renew_switches_to_upgrade_when_plan_differs(sqlite_engine):
    """When renew is called for an expired subscription with a different plan, it must reactivate and upgrade on Adapt."""
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from bot.services.subscription_service import _create_adapt_paid_subscription

    session_factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with session_factory() as session:
        user = User(telegram_id=469203658, full_name="Customer", platform=Platform.ANDROID)
        server = Server(name="NL-1", host="127.0.0.1", location="Amsterdam")
        session.add_all([user, server])
        await session.commit()
        await session.refresh(user)
        await session.refresh(server)

        t14_3 = Tariff(
            days=14, label="Базовый • 14 дн • 3📱", price_rub=90,
            tariff_type=TariffType.VPN, is_active=True, adapt_plan_uuid="plan-14d-3dev",
        )
        t30_1 = Tariff(
            days=30, label="Базовый • 30 дн • 1📱", price_rub=125,
            tariff_type=TariffType.VPN, is_active=True, adapt_plan_uuid="plan-30d-1dev",
        )
        session.add_all([t14_3, t30_1])
        await session.commit()
        await session.refresh(t30_1)

        expired_date = datetime.utcnow() - timedelta(days=3)
        sub = Subscription(
            user_id=user.id, server_id=server.id, tariff_id=t14_3.id,
            tariff_months=1, tariff_days=14, device_slots=3,
            platform=Platform.ANDROID, status=SubStatus.EXPIRED,
            vpn_key="https://darimiru.ru/vpnbot/adapt-sub/test-uuid-999",
            client_name="adapt_test-uuid-999",
            expires_at=expired_date,
        )
        session.add(sub)
        await session.commit()
        await session.refresh(sub)

        adapt_sub = AdaptSubscription(
            subscription_id=sub.id,
            adapt_uuid="test-uuid-999",
            adapt_plan_uuid="plan-14d-3dev",
            end_date=expired_date,
            is_frozen=False,
        )
        session.add(adapt_sub)
        await session.commit()

        reactivated_end = datetime.utcnow() + timedelta(days=3)
        new_end = datetime.utcnow() + timedelta(days=30)

        mock_api = AsyncMock()
        mock_api.get_status.side_effect = [
            {"plan_uuid": "plan-14d-3dev", "end_date": expired_date.isoformat(), "devices": 3},
            {"plan_uuid": "plan-14d-3dev", "end_date": reactivated_end.isoformat(), "devices": 3},
            {"plan_uuid": "plan-30d-1dev", "end_date": new_end.isoformat(), "devices": 1},
        ]
        mock_api.list_plans.return_value = [{"uuid": "plan-30d-1dev", "devices": 1}]
        mock_api.renew_subscription_custom.return_value = {"end_date": reactivated_end.isoformat()}
        mock_api.upgrade_subscription.return_value = {"success": True}

        with patch("bot.services.subscription_service.AdaptAPI", return_value=mock_api):
            result_sub, key = await _create_adapt_paid_subscription(
                session,
                user=user,
                tariff=t30_1,
                platform=Platform.ANDROID,
                purchase_action="renew",
                target_subscription_id=sub.id,
            )

        # Verify that because plan differed on an expired subscription, it reactivated and upgraded!
        mock_api.renew_subscription_custom.assert_awaited_once_with("test-uuid-999", 3)
        mock_api.upgrade_subscription.assert_awaited_once_with("test-uuid-999", "plan-30d-1dev")

        assert result_sub.id == sub.id
        assert result_sub.status == SubStatus.ACTIVE
        assert result_sub.tariff_id == t30_1.id
        assert result_sub.device_slots == 1
        assert result_sub.expires_at == new_end
        assert adapt_sub.adapt_plan_uuid == "plan-30d-1dev"
