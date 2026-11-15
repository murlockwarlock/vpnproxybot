from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.config import settings
from bot.handlers import admin as admin_handler
from bot.handlers import profile as profile_handler
from bot.models import AdaptSubscription, Base, Platform, Server, SubStatus, Subscription, Tariff, User
from bot.services import guide_service, subscription_service


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


class _State:
    def __init__(self, data: dict):
        self.data = data

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **values):
        self.data.update(values)

    async def clear(self):
        self.data.clear()


def _make_callback(data: str, user_id: int = 9001):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id, first_name="Admin", username="admin"),
        message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
        answer=AsyncMock(),
        bot=SimpleNamespace(send_message=AsyncMock()),
    )


async def _seed_adapt_subscription(
    session,
    *,
    tariff: Tariff,
    adapt_uuid: str,
    status: SubStatus,
    billing_mode: str,
    expires_at: datetime,
    additional_tariffs: list[Tariff] | None = None,
):
    user = User(telegram_id=180001, username="adapt_demo_user", full_name="Adapt Demo")
    server = Server(name="Adapt test", host="127.0.0.1", api_url="https://vpn.test", location="Test")
    session.add_all([user, server, tariff, *(additional_tariffs or [])])
    await session.flush()

    subscription = Subscription(
        user_id=user.id,
        server_id=server.id,
        tariff_id=tariff.id,
        tariff_months=tariff.days // 30,
        tariff_days=tariff.days,
        billing_mode=billing_mode,
        status=status,
        device_slots=3,
        vpn_key=f"https://darimiru.ru/vpnbot/adapt-sub/{adapt_uuid}",
        client_name=f"adapt_{adapt_uuid}",
        platform=Platform.ANDROID,
        expires_at=expires_at,
    )
    session.add(subscription)
    await session.flush()

    adapt_subscription = AdaptSubscription(
        subscription_id=subscription.id,
        adapt_uuid=adapt_uuid,
        adapt_plan_uuid=tariff.adapt_plan_uuid,
        end_date=expires_at,
    )
    session.add(adapt_subscription)
    await session.commit()
    return user, server, subscription, adapt_subscription


@pytest.mark.asyncio
async def test_admin_demo_upgrade_lists_paid_adapt_tariffs_without_provider_cost(
    db_session_factory,
    monkeypatch,
):
    demo_plan_uuid = "adapt-demo-plan"
    monkeypatch.setattr(settings, "adapt_demo_plan_uuid", demo_plan_uuid)
    monkeypatch.setattr(admin_handler, "_is_admin", lambda user_id: True)
    monkeypatch.setattr(admin_handler, "async_session", db_session_factory)

    demo_tariff = Tariff(
        label="Демо",
        days=3,
        price_rub=0,
        adapt_plan_uuid=demo_plan_uuid,
        adapt_cost_price_usd=None,
    )
    paid_basic = Tariff(
        label="Базовый",
        days=30,
        price_rub=200,
        adapt_plan_uuid="adapt-paid-basic",
        adapt_cost_price_usd=None,
    )
    paid_plus = Tariff(
        label="Расширенный",
        days=60,
        price_rub=350,
        adapt_plan_uuid="adapt-paid-plus",
        adapt_cost_price_usd=None,
    )

    async with db_session_factory() as session:
        user, _, subscription, _ = await _seed_adapt_subscription(
            session,
            tariff=demo_tariff,
            adapt_uuid="demo-subscription",
            status=SubStatus.ACTIVE,
            billing_mode="demo",
            expires_at=datetime.utcnow() + timedelta(days=2),
            additional_tariffs=[paid_basic, paid_plus],
        )

    callback = _make_callback(f"adm_key_action_upgrade_{subscription.id}")
    state = _State({"manual_key_user": user.telegram_id, "manual_key_back_callback": "adm_back"})
    await admin_handler.admin_gen_key_action(callback, state)

    buttons = [
        button
        for row in callback.message.edit_text.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    callbacks = {button.callback_data for button in buttons if button.callback_data}
    assert f"adm_key_tariff_{paid_basic.id}" in callbacks
    assert f"adm_key_tariff_{paid_plus.id}" in callbacks
    assert f"adm_key_tariff_{demo_tariff.id}" not in callbacks
    callback.answer.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_admin_manual_renew_action_presents_only_current_tariff(
    db_session_factory,
    monkeypatch,
):
    monkeypatch.setattr(admin_handler, "_is_admin", lambda user_id: True)
    monkeypatch.setattr(admin_handler, "async_session", db_session_factory)

    paid_basic = Tariff(
        label="Базовый 30 дней",
        days=30,
        price_rub=200,
        adapt_plan_uuid="adapt-paid-basic",
        adapt_cost_price_usd=None,
    )
    other_tariff = Tariff(
        label="Другой тариф",
        days=60,
        price_rub=400,
        adapt_plan_uuid="adapt-paid-other",
        adapt_cost_price_usd=None,
    )

    async with db_session_factory() as session:
        user, _, subscription, _ = await _seed_adapt_subscription(
            session,
            tariff=paid_basic,
            adapt_uuid="paid-subscription-renew",
            status=SubStatus.ACTIVE,
            billing_mode="tariff",
            expires_at=datetime.utcnow() + timedelta(days=10),
            additional_tariffs=[other_tariff],
        )

    callback = _make_callback(f"adm_key_action_renew_{subscription.id}")
    state = _State({"manual_key_user": user.telegram_id, "manual_key_back_callback": "adm_back"})
    await admin_handler.admin_gen_key_action(callback, state)

    buttons = [
        button
        for row in callback.message.edit_text.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    callbacks = {button.callback_data for button in buttons if button.callback_data}
    assert f"adm_key_tariff_{paid_basic.id}" in callbacks
    assert f"adm_key_tariff_{other_tariff.id}" not in callbacks
    callback.answer.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_admin_active_paid_upgrade_remains_provider_cost_strict(
    db_session_factory,
    monkeypatch,
):
    monkeypatch.setattr(settings, "adapt_demo_plan_uuid", "adapt-demo-plan")
    monkeypatch.setattr(admin_handler, "_is_admin", lambda user_id: True)
    monkeypatch.setattr(admin_handler, "async_session", db_session_factory)
    monkeypatch.setattr(
        "bot.services.adapt_api.AdaptAPI.list_plans",
        AsyncMock(
            return_value=[
                {"uuid": "adapt-current", "days": 30, "price_usd": "2.0", "devices": 1, "is_trial": False, "is_active": True},
                {"uuid": "adapt-valid-upgrade", "days": 30, "price_usd": "4.0", "devices": 1, "is_trial": False, "is_active": True},
            ]
        ),
    )

    current_tariff = Tariff(
        label="Текущий",
        days=30,
        price_rub=150,
        adapt_plan_uuid="adapt-current",
        adapt_cost_price_usd=2.0,
    )
    missing_cost_tariff = Tariff(
        label="Без себестоимости",
        days=30,
        price_rub=250,
        adapt_plan_uuid="adapt-missing-cost",
        adapt_cost_price_usd=None,
    )
    valid_tariff = Tariff(
        label="Доступный апгрейд",
        days=30,
        price_rub=300,
        adapt_plan_uuid="adapt-valid-upgrade",
        adapt_cost_price_usd=4.0,
    )

    async with db_session_factory() as session:
        user, _, subscription, _ = await _seed_adapt_subscription(
            session,
            tariff=current_tariff,
            adapt_uuid="paid-subscription",
            status=SubStatus.ACTIVE,
            billing_mode="tariff",
            expires_at=datetime.utcnow() + timedelta(days=10),
            additional_tariffs=[missing_cost_tariff, valid_tariff],
        )

    callback = _make_callback(f"adm_key_action_upgrade_{subscription.id}")
    state = _State({"manual_key_user": user.telegram_id, "manual_key_back_callback": "adm_back"})
    await admin_handler.admin_gen_key_action(callback, state)

    buttons = [
        button
        for row in callback.message.edit_text.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    callbacks = {button.callback_data for button in buttons if button.callback_data}
    assert f"adm_key_tariff_{valid_tariff.id}" in callbacks
    assert f"adm_key_tariff_{missing_cost_tariff.id}" not in callbacks


@pytest.mark.asyncio
async def test_active_paid_customer_upgrade_uses_provider_plans_for_ui_and_quote(
    db_session_factory,
    monkeypatch,
):
    from bot.handlers import buy as buy_handler
    from bot.services.purchase_intent import get_purchase_price_rub

    demo_plan_uuid = "adapt-demo-plan"
    current_plan_uuid = "adapt-current-plan"
    higher_plan_uuid = "adapt-higher-plan"
    monkeypatch.setattr(settings, "adapt_demo_plan_uuid", demo_plan_uuid)
    monkeypatch.setattr(buy_handler, "async_session", db_session_factory)
    monkeypatch.setattr(settings, "is_admin", lambda user_id: False)
    monkeypatch.setattr(buy_handler, "_get_stars_enabled", AsyncMock(return_value=False))
    provider_plans = [
        {
            "uuid": current_plan_uuid,
            "days": 10,
            "price_usd": "1.00",
            "devices": 1,
            "is_trial": False,
            "is_active": True,
        },
        {
            "uuid": higher_plan_uuid,
            "days": 10,
            "price_usd": "0.90",
            "devices": 1,
            "is_trial": False,
            "is_active": True,
        },
        {
            "uuid": demo_plan_uuid,
            "days": 3,
            "price_usd": "0.01",
            "devices": 1,
            "is_trial": True,
            "is_active": True,
        },
    ]
    provider_list = AsyncMock(return_value=provider_plans)
    monkeypatch.setattr("bot.services.adapt_api.AdaptAPI.list_plans", provider_list)

    current_tariff = Tariff(
        label="Текущий тариф",
        days=10,
        price_rub=100,
        adapt_plan_uuid=current_plan_uuid,
        adapt_cost_price_usd=None,
    )
    higher_tariff = Tariff(
        label="Тариф выше",
        days=10,
        price_rub=220,
        adapt_plan_uuid=higher_plan_uuid,
        adapt_cost_price_usd=None,
    )
    demo_tariff = Tariff(
        label="Демо",
        days=3,
        price_rub=0,
        adapt_plan_uuid=demo_plan_uuid,
        adapt_cost_price_usd=None,
    )
    async with db_session_factory() as session:
        user, _, subscription, _ = await _seed_adapt_subscription(
            session,
            tariff=current_tariff,
            adapt_uuid="active-paid-uuid",
            status=SubStatus.ACTIVE,
            billing_mode="tariff",
            expires_at=datetime.utcnow() + timedelta(days=5),
            additional_tariffs=[higher_tariff, demo_tariff],
        )
        subscription_id = subscription.id
        higher_tariff_id = higher_tariff.id
        current_tariff_id = current_tariff.id
        demo_tariff_id = demo_tariff.id

    targets = await buy_handler._purchase_targets(user.telegram_id, upgrade_only=True)
    assert [item.id for item in targets] == [subscription_id]

    return_callback = _make_callback(
        f"purchase_return_{subscription_id}", user_id=user.telegram_id
    )
    await buy_handler.return_to_purchase_subscription(return_callback)
    card_buttons = [
        button
        for row in return_callback.message.edit_text.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert any(
        button.text == "↑ Улучшить"
        and button.callback_data == f"purchase_upgrade_{subscription_id}"
        for button in card_buttons
    )

    upgrade_callback = _make_callback("purchase_upgrade", user_id=user.telegram_id)
    await buy_handler._open_upgrade_target(upgrade_callback, subscription_id)
    upgrade_buttons = [
        button
        for row in upgrade_callback.message.edit_text.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    upgrade_callbacks = {button.callback_data for button in upgrade_buttons}
    assert f"tariff_{higher_tariff_id}~u~{subscription_id}" in upgrade_callbacks
    assert f"tariff_{current_tariff_id}~u~{subscription_id}" not in upgrade_callbacks
    assert f"tariff_{demo_tariff_id}~u~{subscription_id}" not in upgrade_callbacks

    async with db_session_factory() as session:
        user = await session.scalar(select(User).where(User.telegram_id == 180001))
        higher_tariff = await session.get(Tariff, higher_tariff_id)
        quote = await get_purchase_price_rub(
            session,
            user=user,
            tariff=higher_tariff,
            action="upgrade",
            target_subscription_id=subscription_id,
        )
        with pytest.raises(ValueError, match="уже на выбранном тарифе"):
            await get_purchase_price_rub(
                session,
                user=user,
                tariff=await session.get(Tariff, current_tariff_id),
                action="upgrade",
                target_subscription_id=subscription_id,
            )
        with pytest.raises(ValueError, match="недоступен для улучшения"):
            await get_purchase_price_rub(
                session,
                user=user,
                tariff=await session.get(Tariff, demo_tariff_id),
                action="upgrade",
                target_subscription_id=subscription_id,
            )

    assert quote > 0
    assert provider_list.await_count >= 1


@pytest.mark.asyncio
async def test_adapt_subscription_card_actions_reuse_purchase_callbacks(
    db_session_factory,
    monkeypatch,
):
    from bot.handlers import buy as buy_handler

    current_plan_uuid = "adapt-profile-current"
    higher_plan_uuid = "adapt-profile-higher"
    monkeypatch.setattr(profile_handler, "async_session", db_session_factory)
    monkeypatch.setattr(buy_handler, "async_session", db_session_factory)
    monkeypatch.setattr(profile_handler, "fetch_linked_web_profile", AsyncMock(return_value=None))
    monkeypatch.setattr(profile_handler, "sync_user_profile", AsyncMock())
    monkeypatch.setattr(settings, "is_admin", lambda user_id: False)
    monkeypatch.setattr(buy_handler, "_get_stars_enabled", AsyncMock(return_value=False))
    monkeypatch.setattr(
        "bot.services.adapt_api.AdaptAPI.list_plans",
        AsyncMock(
            return_value=[
                {
                    "uuid": current_plan_uuid,
                    "days": 10,
                    "price_usd": "1.00",
                    "devices": 1,
                    "is_trial": False,
                    "is_active": True,
                },
                {
                    "uuid": higher_plan_uuid,
                    "days": 10,
                    "price_usd": "0.90",
                    "devices": 1,
                    "is_trial": False,
                    "is_active": True,
                },
            ]
        ),
    )

    current_tariff = Tariff(
        label="Текущий тариф",
        days=10,
        price_rub=100,
        adapt_plan_uuid=current_plan_uuid,
        adapt_cost_price_usd=None,
    )
    higher_tariff = Tariff(
        label="Тариф выше",
        days=10,
        price_rub=220,
        adapt_plan_uuid=higher_plan_uuid,
        adapt_cost_price_usd=None,
    )
    async with db_session_factory() as session:
        user, _, subscription, _ = await _seed_adapt_subscription(
            session,
            tariff=current_tariff,
            adapt_uuid="profile-active-paid-uuid",
            status=SubStatus.ACTIVE,
            billing_mode="tariff",
            expires_at=datetime.utcnow() + timedelta(days=5),
            additional_tariffs=[higher_tariff],
        )

    text, markup = await profile_handler._build_keys_page(user.telegram_id)
    callbacks = {
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    }
    assert f"purchase_renew_{subscription.id}" in callbacks
    assert f"purchase_upgrade_{subscription.id}" in callbacks
    assert f"buy_new_{subscription.id}" in callbacks
    assert "Мои ключи" not in text


@pytest.mark.asyncio
async def test_active_paid_adapt_upgrade_preserves_existing_uuid(
    db_session_factory,
    monkeypatch,
):
    current_plan_uuid = "adapt-fulfillment-current"
    higher_plan_uuid = "adapt-fulfillment-higher"
    adapt_uuid = "active-paid-fulfillment-uuid"
    current_tariff = Tariff(
        label="Текущий тариф",
        days=10,
        price_rub=100,
        adapt_plan_uuid=current_plan_uuid,
        adapt_cost_price_usd=None,
    )
    higher_tariff = Tariff(
        label="Тариф выше",
        days=10,
        price_rub=220,
        adapt_plan_uuid=higher_plan_uuid,
        adapt_cost_price_usd=None,
    )
    async with db_session_factory() as session:
        user, _, subscription, _ = await _seed_adapt_subscription(
            session,
            tariff=current_tariff,
            adapt_uuid=adapt_uuid,
            status=SubStatus.ACTIVE,
            billing_mode="tariff",
            expires_at=datetime.utcnow() + timedelta(days=5),
            additional_tariffs=[higher_tariff],
        )
        subscription_id = subscription.id
        higher_tariff_id = higher_tariff.id

        api = SimpleNamespace(
            get_status=AsyncMock(
                side_effect=[
                    {
                        "plan_uuid": current_plan_uuid,
                        "is_active": True,
                        "is_frozen": False,
                        "devices": 3,
                        "end_date": (datetime.utcnow() + timedelta(days=5)).isoformat(),
                    },
                    {
                        "plan_uuid": higher_plan_uuid,
                        "is_active": True,
                        "is_frozen": False,
                        "devices": 5,
                        "end_date": (datetime.utcnow() + timedelta(days=10)).isoformat(),
                    },
                ]
            ),
            upgrade_subscription=AsyncMock(return_value={"success": True}),
        )
        monkeypatch.setattr(subscription_service, "AdaptAPI", lambda: api)
        monkeypatch.setattr(
            subscription_service,
            "build_adapt_mirror_url",
            lambda uuid: f"https://darimiru.ru/vpnbot/adapt-sub/{uuid}",
        )

        result_sub, result_url = await subscription_service.create_or_extend_paid_access(
            session,
            user=user,
            tariff=higher_tariff,
            platform=Platform.ANDROID,
            purchase_action="upgrade",
            target_subscription_id=subscription_id,
        )
        await session.commit()

    async with db_session_factory() as session:
        stored = await session.get(Subscription, subscription_id)
        adapt_record = await session.scalar(
            select(AdaptSubscription).where(AdaptSubscription.subscription_id == subscription_id)
        )

    assert result_sub.id == subscription_id
    assert adapt_uuid in result_url
    assert stored.tariff_id == higher_tariff_id
    assert stored.client_name == f"adapt_{adapt_uuid}"
    assert adapt_record.adapt_uuid == adapt_uuid
    assert adapt_record.adapt_plan_uuid == higher_plan_uuid
    api.upgrade_subscription.assert_awaited_once_with(adapt_uuid, higher_plan_uuid)


def test_active_adapt_demo_with_key_appears_in_my_keys(monkeypatch):
    demo_plan_uuid = "adapt-demo-plan"
    monkeypatch.setattr(settings, "adapt_demo_plan_uuid", demo_plan_uuid)
    subscription = SimpleNamespace(
        id=180,
        status=SimpleNamespace(value="active"),
        billing_mode="demo",
        client_name="adapt_demo-subscription",
        vpn_key="https://darimiru.ru/vpnbot/adapt-sub/demo-subscription",
        expires_at=datetime.utcnow() + timedelta(days=2),
        adapt_sub=SimpleNamespace(adapt_plan_uuid=demo_plan_uuid),
    )

    result = profile_handler._profile_vpn_subscriptions([subscription])

    assert [item.id for item in result] == [180]


def test_expired_adapt_demo_is_not_treated_as_paid_or_renewable(monkeypatch):
    demo_plan_uuid = "adapt-demo-plan"
    monkeypatch.setattr(settings, "adapt_demo_plan_uuid", demo_plan_uuid)
    expired_demo = SimpleNamespace(
        id=180,
        status=SimpleNamespace(value="expired"),
        billing_mode="demo",
        client_name="adapt_demo-subscription",
        vpn_key="https://darimiru.ru/vpnbot/adapt-sub/demo-subscription",
        expires_at=datetime.utcnow() - timedelta(days=1),
        adapt_sub=SimpleNamespace(adapt_plan_uuid=demo_plan_uuid),
    )
    active_paid = SimpleNamespace(
        id=181,
        status=SimpleNamespace(value="active"),
        billing_mode="tariff",
        client_name="adapt_paid-active",
        vpn_key="https://darimiru.ru/vpnbot/adapt-sub/paid-active",
        expires_at=datetime.utcnow() + timedelta(days=1),
        adapt_sub=SimpleNamespace(adapt_plan_uuid="adapt-paid"),
    )
    expired_paid = SimpleNamespace(
        id=182,
        status=SimpleNamespace(value="expired"),
        billing_mode="tariff",
        client_name="adapt_paid-expired",
        vpn_key="https://darimiru.ru/vpnbot/adapt-sub/paid-expired",
        expires_at=datetime.utcnow() - timedelta(days=1),
        adapt_sub=SimpleNamespace(adapt_plan_uuid="adapt-paid"),
    )

    result = profile_handler._profile_vpn_subscriptions([expired_demo, active_paid, expired_paid])

    assert {item.id for item in result} == {181, 182}


@pytest.mark.asyncio
async def test_admin_demo_upgrade_fulfillment_preserves_adapt_uuid(
    db_session_factory,
    monkeypatch,
):
    demo_plan_uuid = "adapt-demo-plan"
    paid_plan_uuid = "adapt-paid-plan"
    adapt_uuid = "demo-subscription-uuid"
    monkeypatch.setattr(settings, "adapt_demo_plan_uuid", demo_plan_uuid)
    monkeypatch.setattr(admin_handler, "_is_admin", lambda user_id: True)
    monkeypatch.setattr(admin_handler, "async_session", db_session_factory)

    demo_tariff = Tariff(
        label="Демо",
        days=3,
        price_rub=0,
        adapt_plan_uuid=demo_plan_uuid,
        adapt_cost_price_usd=None,
    )
    paid_tariff = Tariff(
        label="Платный",
        days=30,
        price_rub=200,
        adapt_plan_uuid=paid_plan_uuid,
        adapt_cost_price_usd=None,
    )
    async with db_session_factory() as session:
        user, _, subscription, _ = await _seed_adapt_subscription(
            session,
            tariff=demo_tariff,
            adapt_uuid=adapt_uuid,
            status=SubStatus.ACTIVE,
            billing_mode="demo",
            expires_at=datetime.utcnow() + timedelta(days=2),
            additional_tariffs=[paid_tariff],
        )
        subscription_id = subscription.id
        paid_tariff_id = paid_tariff.id

    new_end_date = datetime.utcnow() + timedelta(days=30)
    api = SimpleNamespace(
        get_status=AsyncMock(
            side_effect=[
                {
                    "plan_uuid": demo_plan_uuid,
                    "end_date": (datetime.utcnow() + timedelta(days=2)).isoformat() + "Z",
                    "devices": 3,
                    "is_frozen": False,
                },
                {
                    "plan_uuid": paid_plan_uuid,
                    "end_date": new_end_date.isoformat() + "Z",
                    "devices": 3,
                    "is_frozen": False,
                },
            ]
        ),
        upgrade_subscription=AsyncMock(return_value={"success": True}),
    )
    monkeypatch.setattr(subscription_service, "AdaptAPI", lambda: api)
    monkeypatch.setattr(subscription_service, "get_included_device_slots", AsyncMock(return_value=3))
    monkeypatch.setattr(
        subscription_service,
        "build_adapt_mirror_url",
        lambda uuid: f"https://darimiru.ru/vpnbot/adapt-sub/{uuid}",
    )
    monkeypatch.setattr(guide_service, "send_guide", AsyncMock())

    callback = _make_callback(f"adm_key_tariff_{paid_tariff_id}")
    state = _State({
        "manual_key_user": user.telegram_id,
        "manual_purchase_action": "upgrade",
        "manual_target_subscription_id": subscription_id,
    })
    await admin_handler.admin_gen_key_tariff(callback, state)

    async with db_session_factory() as session:
        updated_subscription = await session.get(Subscription, subscription_id)
        adapt_record = await session.scalar(
            select(AdaptSubscription).where(AdaptSubscription.subscription_id == subscription_id)
        )

    assert updated_subscription.tariff_id == paid_tariff_id
    assert updated_subscription.client_name == f"adapt_{adapt_uuid}"
    assert adapt_record.adapt_uuid == adapt_uuid
    assert adapt_record.adapt_plan_uuid == paid_plan_uuid
    api.upgrade_subscription.assert_awaited_once_with(adapt_uuid, paid_plan_uuid)


@pytest.mark.asyncio
async def test_admin_manual_new_for_expired_paid_adapt_creates_distinct_subscription(
    db_session_factory,
    monkeypatch,
):
    old_plan_uuid = "adapt-old-paid-plan"
    new_plan_uuid = "adapt-new-paid-plan"
    old_adapt_uuid = "old-expired-subscription-uuid"
    new_adapt_uuid = "new-distinct-subscription-uuid"

    monkeypatch.setattr(admin_handler, "_is_admin", lambda user_id: True)
    monkeypatch.setattr(admin_handler, "async_session", db_session_factory)

    old_paid_tariff = Tariff(
        label="Старый платный 30 дн",
        days=30,
        price_rub=200,
        adapt_plan_uuid=old_plan_uuid,
        adapt_cost_price_usd=None,
    )
    new_paid_tariff = Tariff(
        label="Новый платный 30 дн",
        days=30,
        price_rub=300,
        adapt_plan_uuid=new_plan_uuid,
        adapt_cost_price_usd=None,
    )

    async with db_session_factory() as session:
        user, server, old_sub, old_adapt = await _seed_adapt_subscription(
            session,
            tariff=old_paid_tariff,
            adapt_uuid=old_adapt_uuid,
            status=SubStatus.EXPIRED,
            billing_mode="tariff",
            expires_at=datetime.utcnow() - timedelta(days=5),
            additional_tariffs=[new_paid_tariff],
        )
        user_tg_id = user.telegram_id
        old_sub_id = old_sub.id
        new_tariff_id = new_paid_tariff.id

    api = SimpleNamespace(
        create_subscription=AsyncMock(
            return_value={
                "subscription_uuid": new_adapt_uuid,
                "subscription_url": f"https://upstream.test/{new_adapt_uuid}",
                "days": 30,
                "devices": 3,
            }
        ),
    )
    monkeypatch.setattr(subscription_service, "AdaptAPI", lambda: api)
    monkeypatch.setattr(subscription_service, "get_included_device_slots", AsyncMock(return_value=3))
    monkeypatch.setattr(
        subscription_service,
        "build_adapt_mirror_url",
        lambda uuid: f"https://darimiru.ru/vpnbot/adapt-sub/{uuid}",
    )
    monkeypatch.setattr(guide_service, "send_guide", AsyncMock())

    # 1. Admin chooses "➕ Создать новую подписку" via adm_key_action_new_0
    action_callback = _make_callback("adm_key_action_new_0")
    state = _State({
        "manual_key_user": user_tg_id,
        "manual_key_back_callback": "adm_back",
    })
    await admin_handler.admin_gen_key_action(action_callback, state)

    state_data = await state.get_data()
    assert state_data.get("manual_purchase_action") == "new"
    assert state_data.get("manual_target_subscription_id") is None

    action_buttons = [
        button
        for row in action_callback.message.edit_text.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    action_callbacks = {button.callback_data for button in action_buttons if button.callback_data}
    assert f"adm_key_tariff_{new_tariff_id}" in action_callbacks

    # 2. Admin selects a paid Adapt tariff via adm_key_tariff_{id}
    tariff_callback = _make_callback(f"adm_key_tariff_{new_tariff_id}")
    await admin_handler.admin_gen_key_tariff(tariff_callback, state)

    # Assertions
    api.create_subscription.assert_awaited_once_with(
        new_plan_uuid,
        external_user_id=str(user_tg_id),
    )

    async with db_session_factory() as session:
        all_subs = (
            await session.execute(
                select(Subscription)
                .where(Subscription.user_id == user.id)
                .order_by(Subscription.id)
            )
        ).scalars().all()
        assert len(all_subs) == 2

        # Old subscription remains untouched
        assert all_subs[0].id == old_sub_id
        assert all_subs[0].status == SubStatus.EXPIRED
        assert all_subs[0].client_name == f"adapt_{old_adapt_uuid}"
        assert all_subs[0].vpn_key == f"https://darimiru.ru/vpnbot/adapt-sub/{old_adapt_uuid}"

        old_adapt_rec = await session.scalar(
            select(AdaptSubscription).where(AdaptSubscription.subscription_id == old_sub_id)
        )
        assert old_adapt_rec.adapt_uuid == old_adapt_uuid
        assert old_adapt_rec.adapt_plan_uuid == old_plan_uuid

        # New subscription row created with new distinct UUID
        new_sub = all_subs[1]
        assert new_sub.id != old_sub_id
        assert new_sub.tariff_id == new_tariff_id
        assert new_sub.client_name == f"adapt_{new_adapt_uuid}"
        assert new_sub.vpn_key == f"https://darimiru.ru/vpnbot/adapt-sub/{new_adapt_uuid}"

        new_adapt_rec = await session.scalar(
            select(AdaptSubscription).where(AdaptSubscription.subscription_id == new_sub.id)
        )
        assert new_adapt_rec is not None
        assert new_adapt_rec.adapt_uuid == new_adapt_uuid
        assert new_adapt_rec.adapt_plan_uuid == new_plan_uuid

    # Confirm admin completion message
    edit_text_call = tariff_callback.message.edit_text.await_args
    assert "✅ <b>Ключ выдан</b>" in edit_text_call.args[0]
    tariff_callback.answer.assert_awaited_once_with()
