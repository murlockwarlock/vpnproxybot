"""Tests that inactive tariffs cannot be purchased through cached callbacks."""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.models import AdaptSubscription, Base, Platform, Server, Subscription, SubStatus, Tariff, TariffType, User


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
async def inactive_tariff(db_session_factory):
    async with db_session_factory() as session:
        tariff = Tariff(
            days=365,
            label="1 год",
            price_rub=995,
            price_stars=0,
            tariff_type=TariffType.VPN,
            is_active=False,
        )
        session.add(tariff)
        await session.commit()
        await session.refresh(tariff)
        return tariff


def _make_callback(tariff_id: int, user_id: int = 100500) -> SimpleNamespace:
    message = SimpleNamespace(
        edit_text=AsyncMock(),
        chat=SimpleNamespace(id=user_id),
    )
    return SimpleNamespace(
        data=f"tariff_{tariff_id}",
        from_user=SimpleNamespace(id=user_id, username="tester"),
        message=message,
        answer=AsyncMock(),
        bot=AsyncMock(),
    )


class _FrozenCallback:
    """Small CallbackQuery stand-in that fails on the mutation rejected by aiogram 3."""

    def __init__(self, data: str, user_id: int = 100500):
        self._data = data
        self.from_user = SimpleNamespace(id=user_id, username="tester")
        self.message = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
        self.answer = AsyncMock()
        self.bot = AsyncMock()

    @property
    def data(self) -> str:
        return self._data

    @data.setter
    def data(self, value: str) -> None:
        raise AssertionError(f"CallbackQuery data must not be mutated: {value}")


async def test_select_tariff_rejects_inactive(db_session_factory, inactive_tariff):
    """select_tariff handler must reject an inactive tariff with show_alert."""
    from bot.handlers import buy as buy_handler

    callback = _make_callback(inactive_tariff.id)

    with patch("bot.handlers.buy.async_session", db_session_factory):
        await buy_handler.select_tariff(callback)

    callback.answer.assert_awaited_once()
    call_kwargs = callback.answer.await_args
    assert call_kwargs.kwargs.get("show_alert") is True
    assert "недоступен" in call_kwargs.args[0]
    # Must NOT proceed to platform selection
    callback.message.edit_text.assert_not_awaited()


async def test_select_tariff_rejects_hidden_tariff_for_non_admin(db_session_factory):
    """A cached/direct callback must not expose an admin-only tariff."""
    from bot.handlers import buy as buy_handler

    async with db_session_factory() as session:
        tariff = Tariff(
            days=30,
            label="Скрытый",
            price_rub=999,
            price_stars=0,
            tariff_type=TariffType.VPN,
            is_active=True,
            is_admin_only=True,
        )
        session.add(tariff)
        await session.commit()
        await session.refresh(tariff)
        tariff_id = tariff.id

    callback = _make_callback(tariff_id)
    with patch("bot.handlers.buy.async_session", db_session_factory):
        await buy_handler.select_tariff(callback)

    callback.answer.assert_awaited_once_with("Этот тариф больше недоступен", show_alert=True)
    callback.message.edit_text.assert_not_awaited()


async def test_upgrade_menu_hides_admin_only_tariffs(db_session_factory):
    """The upgrade keyboard for a regular user must contain only public tariffs."""
    from bot.handlers import buy as buy_handler

    async with db_session_factory() as session:
        user = User(telegram_id=100500, full_name="User")
        server = Server(name="test", host="127.0.0.1", location="Test")
        current = Tariff(
            days=30, label="Current", price_rub=100, price_stars=0,
            tariff_type=TariffType.VPN, is_active=True, is_admin_only=False,
            adapt_plan_uuid="plan-current",
        )
        hidden = Tariff(
            days=30, label="Hidden", price_rub=200, price_stars=0,
            tariff_type=TariffType.VPN, is_active=True, is_admin_only=True,
            adapt_plan_uuid="plan-hidden",
        )
        public = Tariff(
            days=30, label="Public", price_rub=300, price_stars=0,
            tariff_type=TariffType.VPN, is_active=True, is_admin_only=False,
            adapt_plan_uuid="plan-public",
        )
        session.add_all([user, server, current, hidden, public])
        await session.flush()
        subscription = Subscription(
            user_id=user.id,
            server_id=server.id,
            tariff_id=current.id,
            tariff_months=1,
            tariff_days=30,
            status=SubStatus.ACTIVE,
            vpn_key="https://example.test/sub",
            client_name="adapt_test",
            platform=Platform.ANDROID,
            expires_at=datetime.utcnow() + timedelta(days=10),
        )
        session.add(subscription)
        await session.commit()
        await session.refresh(subscription)
        ids = subscription.id, hidden.id, public.id

    sub_id, hidden_id, public_id = ids
    callback = _make_callback(current.id)
    callback.data = f"purchase_upgrade_{sub_id}"
    with (
        patch("bot.handlers.buy.async_session", db_session_factory),
        patch("bot.handlers.buy.settings.is_admin", return_value=False),
        patch("bot.handlers.buy._get_stars_enabled", new_callable=AsyncMock, return_value=False),
    ):
        await buy_handler.choose_upgrade_target(callback)

    markup = callback.message.edit_text.await_args.kwargs["reply_markup"]
    callbacks = [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    ]
    assert any(str(public_id) in value for value in callbacks)
    assert all(str(hidden_id) not in value for value in callbacks)
    button_texts = [
        button.text
        for row in markup.inline_keyboard
        for button in row
    ]
    assert "Public - 267₽" in button_texts
    text = callback.message.edit_text.await_args.args[0]
    assert "Текущий тариф: <b>Current</b>" in text
    assert "Стоимость: <b>100 ₽</b>" in text
    assert "Использовано:" in text
    assert "Остаточная стоимость:" in text


async def test_expired_subscription_tariff_choice_includes_same_and_cheaper_tariff(db_session_factory):
    from bot.handlers import buy as buy_handler

    async with db_session_factory() as session:
        user = User(telegram_id=100502, full_name="User")
        server = Server(name="test", host="127.0.0.1", location="Test")
        current = Tariff(
            days=365, label="Год", price_rub=995, price_stars=0,
            tariff_type=TariffType.VPN, is_active=True,
            adapt_plan_uuid="plan-annual",
        )
        cheaper = Tariff(
            days=30, label="Месяц", price_rub=155, price_stars=0,
            tariff_type=TariffType.VPN, is_active=True,
            adapt_plan_uuid="plan-monthly",
        )
        impossible = Tariff(
            days=7, label="Минимальный", price_rub=50, price_stars=0,
            tariff_type=TariffType.VPN, is_active=True,
            adapt_plan_uuid="plan-impossible",
        )
        session.add_all([user, server, current, cheaper, impossible])
        await session.flush()
        subscription = Subscription(
            user_id=user.id, server_id=server.id, tariff_id=current.id,
            tariff_months=12, tariff_days=365, status=SubStatus.EXPIRED,
            vpn_key="https://example.test/sub", client_name="adapt_expired",
            platform=Platform.ANDROID,
            expires_at=datetime.utcnow() - timedelta(days=1),
        )
        session.add(subscription)
        await session.commit()
        sub_id = subscription.id
        current_id = current.id
        cheaper_id = cheaper.id
        impossible_id = impossible.id

    callback = _make_callback(current.id, user_id=100502)
    callback.data = f"purchase_upgrade_{sub_id}"
    with (
        patch("bot.handlers.buy.async_session", db_session_factory),
        patch("bot.handlers.buy.settings.is_admin", return_value=False),
        patch("bot.handlers.buy._get_stars_enabled", new_callable=AsyncMock, return_value=False),
    ):
        await buy_handler._open_expired_tariff_choice(callback, sub_id)

    markup = callback.message.edit_text.await_args.kwargs["reply_markup"]
    callbacks = [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    ]
    assert f"tariff_{current_id}~u~{sub_id}" in callbacks
    assert f"tariff_{cheaper_id}~u~{sub_id}" in callbacks
    assert f"tariff_{impossible_id}~u~{sub_id}" in callbacks


async def test_expired_paid_renew_button_opens_all_tariffs(db_session_factory):
    from bot.handlers import buy as buy_handler

    async with db_session_factory() as session:
        user = User(telegram_id=100504, full_name="User")
        server = Server(name="test", host="127.0.0.1", location="Test")
        tariff = Tariff(
            days=30, label="Месяц", price_rub=155, price_stars=0,
            tariff_type=TariffType.VPN, is_active=True,
            adapt_plan_uuid="paid-plan",
        )
        session.add_all([user, server, tariff])
        await session.flush()
        subscription = Subscription(
            user_id=user.id, server_id=server.id, tariff_id=tariff.id,
            tariff_months=1, tariff_days=30, status=SubStatus.EXPIRED,
            vpn_key="https://example.test/sub", client_name="adapt_expired",
            platform=Platform.ANDROID,
            expires_at=datetime.utcnow() - timedelta(days=1),
        )
        session.add(subscription)
        await session.commit()
        sub_id = subscription.id

    callback = _FrozenCallback(f"purchase_renew_{sub_id}", user_id=100504)
    open_tariffs = AsyncMock()
    with (
        patch("bot.handlers.buy.async_session", db_session_factory),
        patch("bot.handlers.buy._open_expired_tariff_choice", open_tariffs),
    ):
        await buy_handler.choose_renew_target(callback)

    open_tariffs.assert_awaited_once_with(callback, sub_id)


async def test_purchase_carousel_contains_only_public_adapt_subscriptions(db_session_factory):
    from bot.handlers import buy as buy_handler

    async with db_session_factory() as session:
        user = User(telegram_id=100500, full_name="User")
        server = Server(name="test", host="127.0.0.1", location="Test")
        adapt = Tariff(
            days=30, label="Adapt", price_rub=100, price_stars=0,
            tariff_type=TariffType.VPN, is_active=True, is_admin_only=False,
            adapt_plan_uuid="adapt-plan",
        )
        marzban = Tariff(
            days=30, label="Marzban", price_rub=100, price_stars=0,
            tariff_type=TariffType.VPN, is_active=True, is_admin_only=False,
        )
        hidden = Tariff(
            days=30, label="Hidden", price_rub=100, price_stars=0,
            tariff_type=TariffType.VPN, is_active=True, is_admin_only=True,
            adapt_plan_uuid="hidden-plan",
        )
        session.add_all([user, server, adapt, marzban, hidden])
        await session.flush()
        rows = [
            Subscription(
                user_id=user.id, server_id=server.id, tariff_id=adapt.id,
                tariff_months=1, tariff_days=30, status=SubStatus.ACTIVE,
                vpn_key="https://example.test/adapt", client_name="adapt_public",
                platform=Platform.ANDROID, expires_at=datetime.utcnow() + timedelta(days=10),
            ),
            Subscription(
                user_id=user.id, server_id=server.id, tariff_id=marzban.id,
                tariff_months=1, tariff_days=30, status=SubStatus.ACTIVE,
                vpn_key="https://example.test/marzban", client_name="local_key",
                platform=Platform.ANDROID, expires_at=datetime.utcnow() + timedelta(days=10),
            ),
            Subscription(
                user_id=user.id, server_id=server.id, tariff_id=hidden.id,
                tariff_months=1, tariff_days=30, status=SubStatus.ACTIVE,
                vpn_key="https://example.test/hidden", client_name="adapt_hidden",
                platform=Platform.ANDROID, expires_at=datetime.utcnow() + timedelta(days=10),
            ),
        ]
        session.add_all(rows)
        await session.commit()
        public_id = rows[0].id

    with (
        patch("bot.handlers.buy.async_session", db_session_factory),
        patch("bot.handlers.buy.settings.is_admin", return_value=False),
    ):
        targets = await buy_handler._purchase_targets(100500)

    assert [target.id for target in targets] == [public_id]


async def test_purchase_targets_do_not_call_adapt_before_payment(db_session_factory):
    from bot.handlers import buy as buy_handler

    provider_end = datetime.utcnow() + timedelta(days=120)
    async with db_session_factory() as session:
        user = User(telegram_id=100501, full_name="User")
        server = Server(name="adapt-test", host="127.0.0.1", location="Test")
        tariff_3 = Tariff(
            days=90, label="90 дней • 3 устройства", price_rub=405, price_stars=0,
            tariff_type=TariffType.VPN, is_active=True, adapt_plan_uuid="plan-3",
        )
        tariff_5 = Tariff(
            days=90, label="90 дней • 5 устройств", price_rub=485, price_stars=0,
            tariff_type=TariffType.VPN, is_active=True, adapt_plan_uuid="plan-5",
        )
        session.add_all([user, server, tariff_3, tariff_5])
        await session.flush()
        subscription = Subscription(
            user_id=user.id, server_id=server.id, tariff_id=tariff_5.id,
            tariff_months=3, tariff_days=90, status=SubStatus.ACTIVE, device_slots=5,
            vpn_key="https://example.test/adapt", client_name="adapt_stale",
            platform=Platform.ANDROID, expires_at=provider_end,
        )
        session.add(subscription)
        await session.flush()
        session.add(AdaptSubscription(
            subscription_id=subscription.id,
            adapt_uuid="adapt-subscription-uuid",
            adapt_plan_uuid="plan-5",
            end_date=provider_end,
        ))
        await session.commit()
        subscription_id = subscription.id
        tariff_5_id = tariff_5.id

    with (
        patch("bot.handlers.buy.async_session", db_session_factory),
        patch("bot.handlers.buy.settings.is_admin", return_value=False),
    ):
        targets = await buy_handler._purchase_targets(100501)

    assert [target.id for target in targets] == [subscription_id]
    assert targets[0].tariff_id == tariff_5_id
    assert targets[0].device_slots == 5
    async with db_session_factory() as session:
        stored = await session.get(Subscription, subscription_id)
        adapt = await session.scalar(
            select(AdaptSubscription).where(AdaptSubscription.subscription_id == subscription_id)
        )
        assert stored.tariff_id == tariff_5_id
        assert stored.device_slots == 5
        assert adapt.adapt_plan_uuid == "plan-5"


async def test_purchase_targets_hide_separate_upgrade_for_expired_adapt_subscription(db_session_factory):
    from bot.handlers import buy as buy_handler

    expired_end = datetime.utcnow() - timedelta(days=2)
    async with db_session_factory() as session:
        user = User(telegram_id=100503, full_name="User")
        server = Server(name="adapt-expired", host="127.0.0.1", location="Test")
        annual = Tariff(
            days=365, label="Год", price_rub=995, price_stars=0,
            tariff_type=TariffType.VPN, is_active=True,
            adapt_plan_uuid="plan-annual",
        )
        monthly = Tariff(
            days=30, label="Месяц", price_rub=155, price_stars=0,
            tariff_type=TariffType.VPN, is_active=True,
            adapt_plan_uuid="plan-monthly",
        )
        session.add_all([user, server, annual, monthly])
        await session.flush()
        subscription = Subscription(
            user_id=user.id, server_id=server.id, tariff_id=annual.id,
            tariff_months=12, tariff_days=365, status=SubStatus.EXPIRED,
            vpn_key="https://example.test/adapt", client_name="adapt_expired",
            platform=Platform.ANDROID, expires_at=expired_end,
        )
        session.add(subscription)
        await session.flush()
        session.add(AdaptSubscription(
            subscription_id=subscription.id,
            adapt_uuid="expired-adapt-uuid",
            adapt_plan_uuid="plan-annual",
            end_date=expired_end,
        ))
        await session.commit()
        subscription_id = subscription.id

    with (
        patch("bot.handlers.buy.async_session", db_session_factory),
        patch("bot.handlers.buy.settings.is_admin", return_value=False),
    ):
        targets = await buy_handler._purchase_targets(100503, upgrade_only=True)

    assert targets == []


async def test_renew_target_does_not_mutate_frozen_callback(db_session_factory):
    """The exact production failure must not recur with aiogram's frozen model."""
    from bot.handlers import buy as buy_handler

    async with db_session_factory() as session:
        user = User(telegram_id=100500, full_name="User")
        server = Server(name="test", host="127.0.0.1", location="Test")
        tariff = Tariff(
            days=30, label="Премиум • 30 дн", price_rub=399, price_stars=0,
            tariff_type=TariffType.VPN, is_active=True, adapt_plan_uuid="paid-plan",
        )
        session.add_all([user, server, tariff])
        await session.flush()
        subscription = Subscription(
            user_id=user.id, server_id=server.id, tariff_id=tariff.id,
            tariff_months=1, tariff_days=30, status=SubStatus.ACTIVE,
            vpn_key="https://example.test/sub", client_name="adapt_test",
            platform=Platform.ANDROID, expires_at=datetime.utcnow() + timedelta(days=10),
        )
        session.add(subscription)
        await session.commit()
        sub_id = subscription.id
        tariff_id = tariff.id

    callback = _FrozenCallback(f"purchase_renew_{sub_id}")
    open_tariff = AsyncMock()
    with (
        patch("bot.handlers.buy.async_session", db_session_factory),
        patch("bot.handlers.buy._select_tariff_token", open_tariff),
    ):
        await buy_handler.choose_renew_target(callback)

    open_tariff.assert_awaited_once_with(callback, f"{tariff_id}~r~{sub_id}")
    assert callback.data == f"purchase_renew_{sub_id}"


async def test_purchase_carousel_shows_url_and_does_not_mutate_callback():
    from bot.handlers import buy as buy_handler

    sub = SimpleNamespace(
        id=243,
        status=SimpleNamespace(value="active"),
        tariff=SimpleNamespace(days=90, device_count=5),
        tariff_days=90,
        device_slots=5,
        expires_at=datetime(2026, 10, 11),
        vpn_key="https://example.test/sub?a=1&b=2",
        client_name="adapt_test",
    )
    callback = _FrozenCallback("purchase_browse_0")
    with patch(
        "bot.handlers.buy._purchase_targets",
        new_callable=AsyncMock,
        side_effect=[[sub], []],
    ):
        await buy_handler.browse_purchase_subscriptions(callback)

    edit = callback.message.edit_text.await_args
    assert "Подписка 1/1" in edit.args[0]
    assert "https://example.test/sub?a=1&amp;b=2" in edit.args[0]
    assert callback.data == "purchase_browse_0"
    assert "↑ Улучшить" not in [
        button.text for row in edit.kwargs["reply_markup"].inline_keyboard for button in row
    ]


async def test_purchase_carousel_ignores_unchanged_message_and_acknowledges_button():
    from aiogram.exceptions import TelegramBadRequest
    from bot.handlers import buy as buy_handler

    sub = SimpleNamespace(
        id=243,
        status=SimpleNamespace(value="active"),
        tariff=SimpleNamespace(days=90, device_count=5),
        tariff_days=90,
        device_slots=5,
        expires_at=datetime(2026, 10, 11),
        vpn_key="https://example.test/sub",
        client_name="adapt_test",
    )
    callback = _FrozenCallback("purchase_browse_0")
    callback.message.edit_text.side_effect = TelegramBadRequest(
        method=SimpleNamespace(),
        message="Bad Request: message is not modified",
    )
    with patch(
        "bot.handlers.buy._purchase_targets",
        new_callable=AsyncMock,
        side_effect=[[sub], []],
    ):
        await buy_handler.browse_purchase_subscriptions(callback)

    callback.answer.assert_awaited_once()


async def test_single_unpaid_trial_opens_paid_tariffs_immediately():
    from bot.handlers import buy as buy_handler

    trial = SimpleNamespace(
        id=199,
        billing_mode="tariff",
        tariff=SimpleNamespace(days=7, adapt_plan_uuid="trial-plan"),
    )
    callback = _FrozenCallback("buy")
    open_trial = AsyncMock()
    with (
        patch("bot.handlers.buy._has_non_vpn_tariffs", new_callable=AsyncMock, return_value=False),
        patch("bot.handlers.buy._purchase_targets", new_callable=AsyncMock, return_value=[trial]),
        patch("bot.handlers.buy._has_completed_payment", new_callable=AsyncMock, return_value=False),
        patch("bot.handlers.buy._open_renew_target", open_trial),
    ):
        await buy_handler.start_purchase(callback)

    open_trial.assert_awaited_once_with(callback, 199)
    callback.message.edit_text.assert_not_awaited()


async def test_single_expired_subscription_shows_renew_or_create_choice():
    from bot.handlers import buy as buy_handler

    expired = SimpleNamespace(
        id=244,
        billing_mode="tariff",
        expires_at=datetime.utcnow() - timedelta(days=2),
        status=SimpleNamespace(value="expired"),
        tariff=SimpleNamespace(days=30, device_count=3, adapt_plan_uuid="expired-plan"),
        tariff_days=30,
        device_slots=3,
        vpn_key="https://example.test/expired",
        client_name="adapt_expired",
    )
    callback = _FrozenCallback("buy")
    with (
        patch("bot.handlers.buy._has_non_vpn_tariffs", new_callable=AsyncMock, return_value=False),
        patch("bot.handlers.buy._purchase_targets", new_callable=AsyncMock, return_value=[expired]),
    ):
        await buy_handler.start_purchase(callback)

    edit = callback.message.edit_text.await_args
    assert "Подписка 1/1" in edit.args[0]
    texts = [
        button.text
        for row in edit.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert "↻ Продлить" in texts
    assert "➕ Создать новую" in texts
    assert "↑ Улучшить" not in texts


async def test_tariff_back_returns_to_exact_subscription_card():
    from bot.handlers import buy as buy_handler

    first = SimpleNamespace(
        id=243,
        billing_mode="tariff",
        expires_at=datetime.utcnow() + timedelta(days=10),
        status=SimpleNamespace(value="active"),
        tariff=SimpleNamespace(days=30, device_count=3, adapt_plan_uuid="first-plan"),
        tariff_days=30,
        device_slots=3,
        vpn_key="https://example.test/first",
        client_name="adapt_first",
    )
    second = SimpleNamespace(
        id=244,
        billing_mode="tariff",
        expires_at=datetime.utcnow() - timedelta(days=2),
        status=SimpleNamespace(value="expired"),
        tariff=SimpleNamespace(days=90, device_count=5, adapt_plan_uuid="second-plan"),
        tariff_days=90,
        device_slots=5,
        vpn_key="https://example.test/second",
        client_name="adapt_second",
    )
    callback = _FrozenCallback("purchase_return_244")
    targets = AsyncMock(side_effect=[[first, second], []])
    with patch("bot.handlers.buy._purchase_targets", targets):
        await buy_handler.return_to_purchase_subscription(callback)

    edit = callback.message.edit_text.await_args
    assert "Подписка 2/2" in edit.args[0]
    assert "https://example.test/second" in edit.args[0]
    texts = [
        button.text
        for row in edit.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert "↻ Продлить" in texts
    assert "➕ Создать новую" in texts
    assert "↑ Улучшить" not in texts


async def test_payment_back_keeps_purchase_context():
    from bot.handlers.buy import _tariff_payment_back_callback

    assert _tariff_payment_back_callback(
        tariff_type=TariffType.VPN,
        has_product_types=False,
        requested_purchase_action="upgrade",
        target_subscription_id=244,
    ) == "purchase_tariffs_244"
    assert _tariff_payment_back_callback(
        tariff_type=TariffType.VPN,
        has_product_types=False,
        requested_purchase_action="renew",
        target_subscription_id=244,
    ) == "purchase_return_244"
    assert _tariff_payment_back_callback(
        tariff_type=TariffType.VPN,
        has_product_types=False,
        requested_purchase_action="new",
        target_subscription_id=244,
    ) == "buy_new_244"
    assert _tariff_payment_back_callback(
        tariff_type=TariffType.BOTH,
        has_product_types=True,
        requested_purchase_action="new",
        target_subscription_id=244,
    ) == "ptype_both~244"


async def test_paid_trial_user_sees_purchase_intro_before_carousel():
    from bot.handlers import buy as buy_handler

    trial = SimpleNamespace(
        id=199,
        billing_mode="tariff",
        tariff=SimpleNamespace(days=7, adapt_plan_uuid="trial-plan"),
    )
    callback = _FrozenCallback("buy")
    with (
        patch("bot.handlers.buy._has_non_vpn_tariffs", new_callable=AsyncMock, return_value=False),
        patch("bot.handlers.buy._purchase_targets", new_callable=AsyncMock, return_value=[trial]),
        patch("bot.handlers.buy._has_completed_payment", new_callable=AsyncMock, return_value=True),
    ):
        await buy_handler.start_purchase(callback)

    edit = callback.message.edit_text.await_args
    assert "Оплата подписки" in edit.args[0]
    assert edit.kwargs["reply_markup"].inline_keyboard[0][0].text == "Продолжить"


async def test_trial_renewal_shows_public_paid_plans_with_upgrade_intent(db_session_factory):
    from bot.handlers import buy as buy_handler

    async with db_session_factory() as session:
        user = User(telegram_id=100500, full_name="User")
        server = Server(name="test", host="127.0.0.1", location="Test")
        trial = Tariff(
            days=7, label="Базовый • 7 дн • 1📱", price_rub=45, price_stars=0,
            tariff_type=TariffType.VPN, is_active=True, adapt_plan_uuid="trial-plan",
        )
        public = Tariff(
            days=14, label="Базовый • 14 дн • 1📱", price_rub=75, price_stars=0,
            tariff_type=TariffType.VPN, is_active=True, adapt_plan_uuid="public-plan",
        )
        hidden = Tariff(
            days=14, label="Скрытый", price_rub=90, price_stars=0,
            tariff_type=TariffType.VPN, is_active=True, is_admin_only=True,
            adapt_plan_uuid="hidden-plan",
        )
        session.add_all([user, server, trial, public, hidden])
        await session.flush()
        subscription = Subscription(
            user_id=user.id, server_id=server.id, tariff_id=trial.id,
            tariff_months=0, tariff_days=7, status=SubStatus.ACTIVE,
            vpn_key="https://example.test/sub", client_name="adapt_trial",
            platform=Platform.ANDROID, expires_at=datetime.utcnow() - timedelta(days=1),
        )
        session.add(subscription)
        await session.commit()
        sub_id, public_id, hidden_id = subscription.id, public.id, hidden.id

    callback = _FrozenCallback(f"purchase_renew_{sub_id}")
    with (
        patch("bot.handlers.buy.async_session", db_session_factory),
        patch("bot.handlers.buy.settings.is_admin", return_value=False),
        patch("bot.handlers.buy._get_stars_enabled", new_callable=AsyncMock, return_value=False),
    ):
        await buy_handler.choose_renew_target(callback)

    edit = callback.message.edit_text.await_args
    assert edit.args[0] == "Выберите тариф для продления:"
    callbacks = [
        button.callback_data
        for row in edit.kwargs["reply_markup"].inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith("tariff_")
    ]
    assert f"tariff_{public_id}~u~{sub_id}" in callbacks
    assert all(str(hidden_id) not in value for value in callbacks)
    assert callback.data == f"purchase_renew_{sub_id}"


async def test_subscription_count_text_is_explicit():
    from bot.handlers.buy import _subscription_count_text

    assert _subscription_count_text(2, "renew") == (
        "У вас найдено 2 подписки. Выберите, какую хотите продлить."
    )


async def test_stars_payment_rejects_inactive(db_session_factory, inactive_tariff):
    """Stars payment handler must reject an inactive tariff."""
    from bot.handlers import payment as payment_handler

    callback = _make_callback(inactive_tariff.id)
    callback.data = f"pay_stars_{inactive_tariff.id}_ios_0"

    async def fake_get_tariff(tid):
        async with db_session_factory() as s:
            return await s.get(Tariff, tid)

    with patch("bot.handlers.payment._get_tariff", fake_get_tariff):
        await payment_handler.initiate_stars_payment(callback)

    callback.answer.assert_awaited_once()
    call_kwargs = callback.answer.await_args
    assert call_kwargs.kwargs.get("show_alert") is True
    assert "недоступен" in call_kwargs.args[0]


async def test_telegram_pay_rejects_inactive(db_session_factory, inactive_tariff):
    """Telegram Pay handler must reject an inactive tariff."""
    from bot.handlers import payment as payment_handler
    from bot.config import settings

    callback = _make_callback(inactive_tariff.id)
    callback.data = f"pay_telegram_{inactive_tariff.id}_ios_0"

    async def fake_get_tariff(tid):
        async with db_session_factory() as s:
            return await s.get(Tariff, tid)

    with (
        patch("bot.handlers.payment._get_tariff", fake_get_tariff),
        patch.object(settings, "telegram_payment_provider_token", "test_token"),
    ):
        await payment_handler.initiate_telegram_payment(callback)

    callback.answer.assert_awaited_once()
    call_kwargs = callback.answer.await_args
    assert call_kwargs.kwargs.get("show_alert") is True
    assert "недоступен" in call_kwargs.args[0]


async def test_balance_pay_rejects_inactive(db_session_factory, inactive_tariff):
    """Balance payment handler must reject an inactive tariff."""
    from bot.handlers import payment as payment_handler

    callback = _make_callback(inactive_tariff.id)
    callback.data = f"pay_balance_{inactive_tariff.id}_ios_0"

    async def fake_get_tariff(tid):
        async with db_session_factory() as s:
            return await s.get(Tariff, tid)

    with patch("bot.handlers.payment._get_tariff", fake_get_tariff):
        await payment_handler.initiate_balance_payment(callback)

    callback.answer.assert_awaited_once()
    call_kwargs = callback.answer.await_args
    assert call_kwargs.kwargs.get("show_alert") is True
    assert "недоступен" in call_kwargs.args[0]
