from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.models import Base as BotBase, AdaptSubscription, Server, Subscription, Tariff, User
from bot.services import webstore_bridge
from webstore import routes as webstore_routes
from webstore.models import Base as WebBase, WebOrder, WebProfileLink, WebTelegramItem

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
async def web_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(WebBase.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(WebBase.metadata.drop_all)
    await engine.dispose()


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
async def web_session_factory(web_engine):
    return async_sessionmaker(web_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def bot_session_factory(bot_engine):
    return async_sessionmaker(bot_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def patched_env(monkeypatch, web_session_factory, bot_session_factory):
    monkeypatch.setattr(webstore_routes, "async_session", web_session_factory)
    monkeypatch.setattr(webstore_bridge, "async_session", bot_session_factory)
    monkeypatch.setitem(sys.modules, "yookassa", _FakeYooKassa())
    monkeypatch.setattr(webstore_routes.settings, "yookassa_shop_id", "shop")
    monkeypatch.setattr(webstore_routes.settings, "yookassa_secret_key", "secret")
    monkeypatch.setattr(webstore_routes.settings, "adapt_demo_plan_uuid", "00cce2fe-ee55-4c0d-8bfa-9e6e47cd99a4")
    monkeypatch.setattr(webstore_routes.settings, "subscription_base_url", "https://darimiru.ru/vpnbot")

    mock_tariffs = [
        {
            "key": "adapt_7d",
            "label": "Тест 7 дней",
            "days": 7,
            "price_rub": 75,
            "provider": "adapt",
            "adapt_plan_uuid": "adapt-plan-7d-paid",
            "adapt_cost_price_usd": 0.50,
            "device_limit": 1,
        },
        {
            "key": "adapt_basic",
            "label": "Базовый 1 мес",
            "days": 30,
            "price_rub": 200,
            "provider": "adapt",
            "adapt_plan_uuid": "adapt-plan-basic-111",
            "adapt_cost_price_usd": 1.00,
            "device_limit": 1,
        },
        {
            "key": "adapt_pro",
            "label": "Продвинутый 1 мес",
            "days": 30,
            "price_rub": 400,
            "provider": "adapt",
            "adapt_plan_uuid": "adapt-plan-pro-222",
            "adapt_cost_price_usd": 2.00,
            "device_limit": 3,
        },
    ]
    monkeypatch.setattr(webstore_routes, "get_store_tariffs", lambda: mock_tariffs)
    monkeypatch.setattr(
        webstore_routes,
        "get_store_tariffs_by_key",
        lambda: {t["key"]: t for t in mock_tariffs},
    )
    monkeypatch.setattr(
        webstore_routes,
        "get_adapt_plan_metadata_by_uuid",
        lambda uuid: {
            "adapt_plan_uuid": uuid,
            "adapt_cost_price_usd": 0.03,
            "price_usd": 0.03,
            "days": 3,
            "price_rub": 0,
        } if uuid == webstore_routes.settings.adapt_demo_plan_uuid else None,
    )
    provider_plans = [
        {"uuid": "adapt-plan-7d-paid", "days": 7, "price_usd": "0.50", "devices": 1, "is_trial": False, "is_active": True},
        {"uuid": "adapt-plan-basic-111", "days": 30, "price_usd": "1.00", "devices": 1, "is_trial": False, "is_active": True},
        {"uuid": "adapt-plan-pro-222", "days": 30, "price_usd": "2.00", "devices": 3, "is_trial": False, "is_active": True},
        {"uuid": "00cce2fe-ee55-4c0d-8bfa-9e6e47cd99a4", "days": 3, "price_usd": "0.03", "devices": 1, "is_trial": True, "is_active": True},
    ]
    monkeypatch.setattr(
        "bot.services.adapt_api.AdaptAPI.list_plans",
        AsyncMock(return_value=provider_plans),
    )
    monkeypatch.setattr(
        "webstore.routes.AdaptAPI.list_plans",
        AsyncMock(return_value=provider_plans),
    )
    return web_session_factory


async def test_profile_render_performs_zero_adapt_api_calls(patched_env, monkeypatch):
    profile_token = "prof_token_zero_calls"
    adapt_uuid_1 = "aaaa1111-2222-3333-4444-555555555555"
    adapt_uuid_2 = "bbbb1111-2222-3333-4444-555555555555"

    async with patched_env() as session:
        tg_item1 = WebTelegramItem(
            profile_token=profile_token,
            telegram_id=123456,
            item_type="vpn",
            external_id="sub_1",
            title="Adapt 1",
            key_value=f"https://darimiru.ru/vpnbot/adapt-sub/{adapt_uuid_1}",
            provider="adapt",
            adapt_plan_uuid="adapt-plan-basic-111",
            status="active",
            expires_at=datetime.utcnow() + timedelta(days=20),
        )
        tg_item2 = WebTelegramItem(
            profile_token=profile_token,
            telegram_id=123456,
            item_type="vpn",
            external_id="sub_2",
            title="Adapt 2",
            key_value=f"https://darimiru.ru/vpnbot/adapt-sub/{adapt_uuid_2}",
            provider="adapt",
            adapt_plan_uuid="adapt-plan-pro-222",
            status="active",
            expires_at=datetime.utcnow() + timedelta(days=25),
        )
        session.add_all([tg_item1, tg_item2])
        await session.commit()

    mock_get_status = AsyncMock(side_effect=RuntimeError("get_status must NOT be called on profile render"))
    mock_list_plans = AsyncMock(side_effect=RuntimeError("list_plans must NOT be called on profile render"))
    mock_get_devices = AsyncMock(side_effect=RuntimeError("get_devices must NOT be called on profile render"))

    monkeypatch.setattr("webstore.routes.AdaptAPI.get_status", mock_get_status)
    monkeypatch.setattr("webstore.routes.AdaptAPI.list_plans", mock_list_plans)
    monkeypatch.setattr("webstore.routes.AdaptAPI.get_devices", mock_get_devices)

    req = _Request(
        query={"token": profile_token},
        cookies={webstore_routes._AUTH_COOKIE: profile_token},
    )

    resp = await webstore_routes.handle_profile_orders(req)
    assert resp.status == 200
    data = json.loads(resp.text)
    assert len(data["telegram_items"]) == 2
    mock_get_status.assert_not_called()
    mock_list_plans.assert_not_called()
    mock_get_devices.assert_not_called()


async def test_paid_7day_adapt_tariff_is_not_demo_and_remains_visible_and_renews(patched_env, monkeypatch):
    adapt_uuid = "77777777-7777-7777-7777-777777777777"
    profile_token = "prof_token_7day_paid"

    async with patched_env() as session:
        order = WebOrder(
            id=77,
            order_id="order_7d",
            profile_token=profile_token,
            tariff_key="adapt_7d",
            tariff_label="Тест 7 дней",
            days=7,
            amount_rub=75,
            status="delivered",
            subscription_url=f"https://darimiru.ru/vpnbot/adapt-sub/{adapt_uuid}",
            marzban_username=f"adapt_{adapt_uuid}",
            access_expires_at=datetime.utcnow() + timedelta(days=5),
        )
        session.add(order)
        await session.commit()

    req_qr = _Request(_json_body={"query": f"https://darimiru.ru/vpnbot/adapt-sub/{adapt_uuid}"})
    resp_qr = await webstore_routes.handle_quick_renew_lookup(req_qr)
    assert resp_qr.status == 200
    data_qr = json.loads(resp_qr.text)
    assert data_qr["is_trial"] is False
    assert any(t["key"] == "adapt_7d" for t in data_qr["renew_tariffs"])

    req_order = _Request(
        _json_body={
            "tariff_key": "adapt_7d",
            "purchase_action": "renew",
            "target_order_id": "order_7d",
            "contact": "user7d@example.com",
        },
        cookies={webstore_routes._AUTH_COOKIE: profile_token},
    )
    resp_order = await webstore_routes.handle_create_order(req_order)
    assert resp_order.status == 200
    data_order = json.loads(resp_order.text)

    async with patched_env() as session:
        created = await session.scalar(select(WebOrder).where(WebOrder.order_id == data_order["order_id"]))
        assert created is not None
        assert created.purchase_action == "renew"
        assert created.original_amount_rub == 75


async def test_server_canonicalizes_action_to_upgrade_when_selected_plan_differs(patched_env, monkeypatch):
    adapt_uuid = "88888888-8888-8888-8888-888888888888"
    profile_token = "prof_token_diff_plan"

    async with patched_env() as session:
        order = WebOrder(
            id=88,
            order_id="order_basic_88",
            profile_token=profile_token,
            tariff_key="adapt_basic",
            tariff_label="Базовый 1 мес",
            days=30,
            amount_rub=200,
            status="delivered",
            subscription_url=f"https://darimiru.ru/vpnbot/adapt-sub/{adapt_uuid}",
            marzban_username=f"adapt_{adapt_uuid}",
            access_expires_at=datetime.utcnow() + timedelta(days=20),
        )
        session.add(order)
        await session.commit()

    req = _Request(
        _json_body={
            "tariff_key": "adapt_pro",
            "purchase_action": "renew",
            "target_order_id": "order_basic_88",
            "contact": "upgrade_user@example.com",
        },
        cookies={webstore_routes._AUTH_COOKIE: profile_token},
    )

    resp = await webstore_routes.handle_create_order(req)
    assert resp.status == 200
    data = json.loads(resp.text)

    async with patched_env() as session:
        created = await session.scalar(select(WebOrder).where(WebOrder.order_id == data["order_id"]))
        assert created is not None
        assert created.purchase_action == "upgrade"


async def test_raw_quick_renew_does_not_grant_device_management_rights(patched_env, monkeypatch):
    foreign_adapt_uuid = "99999999-9999-9999-9999-999999999999"
    payer_token = "prof_token_payer"

    async with patched_env() as session:
        order = WebOrder(
            id=123,
            order_id="order_payer_123",
            profile_token=payer_token,
            tariff_key="adapt_basic",
            tariff_label="Базовый 1 мес",
            days=30,
            amount_rub=200,
            status="delivered",
            purchase_action="renew",
            target_order_id=f"uuid:{foreign_adapt_uuid}",
            subscription_url=f"https://darimiru.ru/vpnbot/adapt-sub/{foreign_adapt_uuid}",
            marzban_username=f"adapt_{foreign_adapt_uuid}",
            access_expires_at=datetime.utcnow() + timedelta(days=30),
        )
        session.add(order)
        await session.commit()

    mock_get_devices = AsyncMock()
    mock_delete_device = AsyncMock()
    monkeypatch.setattr("webstore.routes.AdaptAPI.get_devices", mock_get_devices)
    monkeypatch.setattr("webstore.routes.AdaptAPI.delete_device", mock_delete_device)

    req_orders = _Request(
        query={"token": payer_token},
        cookies={webstore_routes._AUTH_COOKIE: payer_token},
    )
    resp_orders = await webstore_routes.handle_profile_orders(req_orders)
    data_orders = json.loads(resp_orders.text)
    assert len(data_orders["orders"]) == 1
    assert data_orders["orders"][0]["device_target"] is None

    req_dev_list = _Request(
        query={"target": "order:123"},
        cookies={webstore_routes._AUTH_COOKIE: payer_token},
    )
    resp_dev_list = await webstore_routes.handle_devices_list(req_dev_list)
    assert resp_dev_list.status == 404
    mock_get_devices.assert_not_called()

    req_dev_del = _Request(
        _json_body={"device_target": "order:123", "device_id": 1},
        cookies={webstore_routes._AUTH_COOKIE: payer_token},
    )
    resp_dev_del = await webstore_routes.handle_device_delete(req_dev_del)
    assert resp_dev_del.status == 404
    mock_delete_device.assert_not_called()


async def test_genuinely_owned_web_subscription_grants_device_management_rights(patched_env, monkeypatch):
    owned_adapt_uuid = "12121212-3434-5656-7878-909090909090"
    owner_token = "prof_token_real_owner"

    async with patched_env() as session:
        root_order = WebOrder(
            id=200,
            order_id="order_owner_200",
            profile_token=owner_token,
            tariff_key="adapt_pro",
            tariff_label="Продвинутый",
            days=30,
            amount_rub=400,
            status="delivered",
            purchase_action="new",
            subscription_url=f"https://darimiru.ru/vpnbot/adapt-sub/{owned_adapt_uuid}",
            marzban_username=f"adapt_{owned_adapt_uuid}",
            access_expires_at=datetime.utcnow() + timedelta(days=30),
        )
        session.add(root_order)
        await session.commit()

    monkeypatch.setattr("webstore.routes.AdaptAPI.get_devices", AsyncMock(return_value=[{"id": 1, "name": "Phone"}]))
    monkeypatch.setattr("webstore.routes.AdaptAPI.get_status", AsyncMock(return_value={"devices": 3}))

    req_orders = _Request(
        query={"token": owner_token},
        cookies={webstore_routes._AUTH_COOKIE: owner_token},
    )
    resp_orders = await webstore_routes.handle_profile_orders(req_orders)
    data_orders = json.loads(resp_orders.text)
    assert data_orders["orders"][0]["device_target"] == "order:200"

    req_dev = _Request(
        query={"target": "order:200"},
        cookies={webstore_routes._AUTH_COOKIE: owner_token},
    )
    resp_dev = await webstore_routes.handle_devices_list(req_dev)
    assert resp_dev.status == 200
    data_dev = json.loads(resp_dev.text)
    assert data_dev["ok"] is True
    assert len(data_dev["devices"]) == 1


async def test_bridge_fallback_for_legacy_subscription_without_adaptsubscription_row(bot_session_factory):
    async with bot_session_factory() as session:
        user = User(telegram_id=654321, username="legacyuser", full_name="Legacy User")
        tariff = Tariff(id=2, label="Basic", price_rub=200, days=30, adapt_plan_uuid="adapt-plan-basic-111")
        server = Server(id=2, name="Server 2", host="2.3.4.5", location="Frankfurt", country_emoji="🇩🇪")
        session.add_all([user, tariff, server])
        await session.flush()

        legacy_sub = Subscription(
            id=201,
            user_id=user.id,
            tariff_id=tariff.id,
            tariff_months=1,
            platform="ios",
            server_id=server.id,
            client_name="adapt_11112222-3333-4444-5555-666677778888",
            vpn_key="https://darimiru.ru/vpnbot/adapt-sub/11112222-3333-4444-5555-666677778888",
            tariff_days=30,
            expires_at=datetime.utcnow() + timedelta(days=30),
        )
        session.add(legacy_sub)
        await session.commit()

        adapt_map = await webstore_bridge.load_adapt_subscriptions_map(session, [201])
        assert len(adapt_map) == 0

        items = webstore_bridge.serialize_profile_items(user, [legacy_sub], [], adapt_map=adapt_map)
        assert len(items) == 1
        assert "11112222-3333-4444-5555-666677778888" in items[0]["key_value"]
        assert items[0]["adapt_plan_uuid"] == "adapt-plan-basic-111"


async def test_active_weborder_shows_device_management_in_html():
    html = (Path(__file__).parents[1] / "webstore" / "templates" / "profile.html").read_text()
    assert "${deviceBtn}" in html


async def test_quick_renew_unknown_provider_fallback_plan_fails_closed(patched_env, monkeypatch):
    unknown_uuid = "34343434-3434-3434-3434-343434343434"

    mock_status = {
        "plan_uuid": "unknown-non-store-plan-uuid",
        "devices": 1,
        "is_active": True,
        "end_date": (datetime.utcnow() + timedelta(days=10)).isoformat(),
    }
    monkeypatch.setattr("webstore.routes.AdaptAPI.get_status", AsyncMock(return_value=mock_status))

    req = _Request(_json_body={"query": f"https://darimiru.ru/vpnbot/adapt-sub/{unknown_uuid}"})
    resp = await webstore_routes.handle_quick_renew_lookup(req)
    assert resp.status == 400
    data = json.loads(resp.text)
    assert "недоступно" in data["error"]


@pytest.mark.parametrize("source", ["provider", "web", "telegram"])
@pytest.mark.parametrize(
    ("remaining_days", "target_price", "is_trial", "expected_price"),
    [
        (15, 400, False, 300),
        (15.25, 400, False, 299),
        (-1, 400, False, 400),
        (15, 105, False, 10),
        (15, 400, True, 400),
        (None, 400, False, 400),
    ],
)
async def test_quick_upgrade_quote_matches_order_and_payment(
    patched_env, monkeypatch, source, remaining_days, target_price, is_trial, expected_price,
):
    fixed_now = datetime(2026, 10, 21, 12)

    class FixedDatetime(datetime):
        @classmethod
        def utcnow(cls):
            return fixed_now

    monkeypatch.setattr(webstore_routes, "datetime", FixedDatetime)
    monkeypatch.setattr("bot.services.purchase_intent.datetime", FixedDatetime)
    tariffs = webstore_routes.get_store_tariffs()
    tariffs[-1]["price_rub"] = target_price
    current_uuid = (
        webstore_routes.settings.adapt_demo_plan_uuid if is_trial else "adapt-plan-basic-111"
    )
    metadata = {t["adapt_plan_uuid"]: t for t in tariffs}
    metadata[current_uuid] = metadata.get(current_uuid) or {
        "adapt_plan_uuid": current_uuid, "days": 3, "price_rub": 0,
    }
    monkeypatch.setattr(webstore_routes, "get_adapt_plan_metadata_by_uuid", metadata.get)
    adapt_uuid = "67676767-6767-6767-6767-676767676767"
    status = {
        "plan_uuid": current_uuid, "is_active": True,
        "end_date": (fixed_now + timedelta(days=remaining_days)).isoformat()
        if remaining_days is not None else None,
    }
    get_status = AsyncMock(return_value=status)
    monkeypatch.setattr(webstore_routes.AdaptAPI, "get_status", get_status)
    if source != "provider":
        expires_at = fixed_now + timedelta(days=remaining_days) if remaining_days is not None else None
        async with patched_env() as session:
            if source == "web":
                session.add(WebOrder(
                    order_id="current-subscription", contact="original@example.com",
                    tariff_key="demo" if is_trial else "adapt_basic",
                    tariff_label="Текущий тариф", days=3 if is_trial else 30,
                    amount_rub=0 if is_trial else 200,
                    status="demo" if is_trial else "delivered",
                    subscription_url=f"https://darimiru.ru/vpnbot/adapt-sub/{adapt_uuid}",
                    target_snapshot_plan_uuid=current_uuid, access_expires_at=expires_at,
                ))
            else:
                session.add(WebTelegramItem(
                    profile_token="original-profile", telegram_id=123456,
                    item_type="vpn", external_id=adapt_uuid, title="Текущий тариф",
                    key_value=f"https://darimiru.ru/vpnbot/adapt-sub/{adapt_uuid}",
                    provider="adapt", adapt_plan_uuid=current_uuid,
                    status="active", expires_at=expires_at,
                ))
            await session.commit()
    lookup = await webstore_routes.handle_quick_renew_lookup(
        _Request(_json_body={"query": adapt_uuid})
    )
    assert lookup.status == 200
    data = json.loads(lookup.text)
    option = next(t for t in data["upgrade_tariffs"] if t["key"] == "adapt_pro")
    assert option["price_rub"] == target_price
    assert option["payable_price_rub"] == expected_price
    if not is_trial:
        assert data["renew_tariffs"][0]["payable_price_rub"] == 200

    response = await webstore_routes.handle_create_order(_Request(_json_body={
        "tariff_key": "adapt_pro", "purchase_action": "upgrade",
        "target_order_id": f"uuid:{adapt_uuid}", "contact": "quote@example.com",
        "payable_price_rub": 1,
    }))
    assert response.status == 200
    assert json.loads(response.text)["redirect_url"] == "https://pay.test/redirect"
    assert _FakeYooKassaPaymentApi.last_params["amount"]["value"] == f"{expected_price:.2f}"
    assert _FakeYooKassaPaymentApi.last_params["receipt"]["items"][0]["amount"]["value"] == f"{expected_price:.2f}"
    async with patched_env() as session:
        order = await session.scalar(select(WebOrder).where(WebOrder.contact == "quote@example.com"))
        assert order.amount_rub == expected_price
        assert order.original_amount_rub == expected_price
        assert order.purchase_action == "upgrade"
        assert order.target_order_id == f"uuid:{adapt_uuid}"
    if source != "provider":
        get_status.assert_not_awaited()


async def test_quick_upgrade_nonpositive_retail_quote_is_not_offered(patched_env, monkeypatch):
    tariffs = webstore_routes.get_store_tariffs()
    tariffs[-1]["price_rub"] = 50
    monkeypatch.setattr(
        webstore_routes, "get_adapt_plan_metadata_by_uuid",
        {t["adapt_plan_uuid"]: t for t in tariffs}.get,
    )
    adapt_uuid = "67676767-6767-6767-6767-676767676767"
    monkeypatch.setattr(webstore_routes.AdaptAPI, "get_status", AsyncMock(return_value={
        "plan_uuid": "adapt-plan-basic-111", "is_active": True,
        "end_date": (datetime.utcnow() + timedelta(days=15)).isoformat(),
    }))
    response = await webstore_routes.handle_quick_renew_lookup(
        _Request(_json_body={"query": adapt_uuid})
    )
    assert response.status == 200
    assert json.loads(response.text)["upgrade_tariffs"] == []
    response = await webstore_routes.handle_create_order(_Request(_json_body={
        "tariff_key": "adapt_pro", "purchase_action": "upgrade",
        "target_order_id": f"uuid:{adapt_uuid}", "contact": "quote@example.com",
    }))
    assert response.status == 400
    async with patched_env() as session:
        assert await session.scalar(select(WebOrder.id)) is None


async def test_quick_upgrade_browser_shows_quote_and_keeps_price_on_one_line(patched_env, monkeypatch):
    from aiohttp import web

    playwright = pytest.importorskip("playwright.async_api")
    fixed_now = datetime.utcnow()

    class FixedDatetime(datetime):
        @classmethod
        def utcnow(cls):
            return fixed_now

    monkeypatch.setattr(webstore_routes, "datetime", FixedDatetime)
    tariffs = webstore_routes.get_store_tariffs()
    tariffs[-1]["label"] = "Базовый • 30 дн • 3 устройства • 238 ГБ"
    monkeypatch.setattr(
        webstore_routes, "get_adapt_plan_metadata_by_uuid",
        {t["adapt_plan_uuid"]: t for t in tariffs}.get,
    )
    monkeypatch.setattr(webstore_routes, "_fetch_public_balance_config", AsyncMock(return_value={}))
    monkeypatch.setattr(webstore_routes.AdaptAPI, "get_status", AsyncMock(return_value={
        "plan_uuid": "adapt-plan-basic-111", "is_active": True,
        "end_date": (fixed_now + timedelta(days=15)).isoformat(),
    }))
    app = web.Application()
    app.router.add_get("/", webstore_routes.handle_store_page)
    app.router.add_post("/api/store/quick-renew/lookup", webstore_routes.handle_quick_renew_lookup)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        async with playwright.async_playwright() as runtime:
            try:
                browser = await runtime.chromium.launch(
                    executable_path=os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE"),
                )
            except playwright.Error as exc:
                if "Executable doesn't exist" in str(exc):
                    pytest.skip("Playwright Chromium is not installed")
                raise
            try:
                page = await browser.new_page()

                async def route_request(route):
                    if route.request.url.startswith(base):
                        await route.continue_()
                    else:
                        await route.abort()

                await page.route("**/*", route_request)
                for width in (320, 360, 390, 768, 1024, 1440):
                    await page.set_viewport_size({"width": width, "height": 900})
                    await page.goto(base)
                    await page.locator("#quickRenewTopbarBtn").click()
                    await page.locator("#quickRenewQueryInput").fill("67676767-6767-6767-6767-676767676767")
                    await page.locator("#quickRenewFindBtn").click()
                    await playwright.expect(page.locator("#quickRenewPayBtn")).to_have_text("Оплатить 200 ₽")
                    await page.locator("#quickRenewTabUpgrade").click()
                    await playwright.expect(page.locator("#quickTariffsList .quick-price")).to_have_text("300 ₽")
                    await playwright.expect(page.locator("#quickRenewPayBtn")).to_have_text("Оплатить 300 ₽")
                    await playwright.expect(page.locator("#quickRenewUpgradeNote")).to_be_visible()
                    assert await page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
                    assert await page.locator(".quick-tariff-card").evaluate("""card => {
                        const text = card.firstElementChild.getBoundingClientRect();
                        const price = card.lastElementChild.getBoundingClientRect();
                        return text.right + 8 <= price.left && price.height < 28
                            && card.scrollWidth <= card.clientWidth;
                    }""")
                    await page.locator("#quickRenewTabRenew").click()
                    await playwright.expect(page.locator("#quickRenewPayBtn")).to_have_text("Оплатить 200 ₽")
                    await playwright.expect(page.locator("#quickRenewUpgradeNote")).to_be_hidden()
            finally:
                await browser.close()
    finally:
        await runner.cleanup()


async def test_quick_renew_expired_legacy_plan_returns_paid_options(patched_env, monkeypatch):
    adapt_uuid = "67676767-6767-6767-6767-676767676767"
    legacy_plan_uuid = "legacy-expired-plan-uuid"
    tariffs = [
        {
            "key": "legacy_paid_7d",
            "label": "Базовый 7 дней",
            "days": 7,
            "price_rub": 55,
            "provider": "adapt",
            "adapt_plan_uuid": "legacy-paid-7d",
            "adapt_cost_price_usd": None,
            "device_limit": 1,
        },
        {
            "key": "legacy_paid_30d",
            "label": "Базовый 30 дней",
            "days": 30,
            "price_rub": 200,
            "provider": "adapt",
            "adapt_plan_uuid": "legacy-paid-30d",
            "adapt_cost_price_usd": None,
            "device_limit": 1,
        },
    ]
    monkeypatch.setattr(webstore_routes, "get_store_tariffs", lambda: tariffs)
    monkeypatch.setattr(
        webstore_routes,
        "get_adapt_plan_metadata_by_uuid",
        lambda uuid: {
            "adapt_plan_uuid": legacy_plan_uuid,
            "adapt_cost_price_usd": None,
            "days": 7,
            "price_rub": 55,
        } if uuid == legacy_plan_uuid else None,
    )
    monkeypatch.setattr(
        "webstore.routes.AdaptAPI.get_status",
        AsyncMock(
            return_value={
                "plan_uuid": legacy_plan_uuid,
                "devices": 1,
                "is_active": False,
                "end_date": (datetime.utcnow() - timedelta(days=2)).isoformat(),
            }
        ),
    )

    monkeypatch.setattr(
        "bot.services.adapt_api.AdaptAPI.list_plans",
        AsyncMock(
            return_value=[
                {"uuid": legacy_plan_uuid, "days": 7, "price_usd": "0.50", "devices": 1, "is_trial": False, "is_active": True},
                {"uuid": "legacy-paid-7d", "days": 7, "price_usd": "0.60", "devices": 1, "is_trial": False, "is_active": True},
                {"uuid": "legacy-paid-30d", "days": 30, "price_usd": "2.00", "devices": 1, "is_trial": False, "is_active": True},
            ]
        ),
    )

    response = await webstore_routes.handle_quick_renew_lookup(
        _Request(_json_body={"query": f"https://darimiru.ru/vpnbot/adapt-sub/{adapt_uuid}"})
    )
    data = json.loads(response.text)
    assert response.status == 200
    assert data["is_trial"] is False
    assert data["is_expired"] is True
    assert data["renew_tariffs"] == []
    assert {item["key"] for item in data["upgrade_tariffs"]} == {
        "legacy_paid_7d",
        "legacy_paid_30d",
    }
    assert data["unavailable_message"] is None


async def test_quick_renew_locally_known_unknown_plan_fails_closed(patched_env):
    adapt_uuid = "56565656-5656-5656-5656-565656565656"

    async with patched_env() as session:
        order = WebOrder(
            order_id="order_unknown_plan",
            tariff_key="custom_legacy_unsupported",
            tariff_label="Legacy",
            days=30,
            amount_rub=100,
            status="delivered",
            subscription_url=f"https://darimiru.ru/vpnbot/adapt-sub/{adapt_uuid}",
            marzban_username=f"adapt_{adapt_uuid}",
            access_expires_at=datetime.utcnow() + timedelta(days=10),
        )
        session.add(order)
        await session.commit()

    req = _Request(_json_body={"query": f"https://darimiru.ru/vpnbot/adapt-sub/{adapt_uuid}"})
    resp = await webstore_routes.handle_quick_renew_lookup(req)
    assert resp.status == 400
    data = json.loads(resp.text)
    assert "недоступно" in data["error"]


async def test_targeting_historical_weborder_canonicalizes_to_latest_plan(patched_env, monkeypatch):
    adapt_uuid = "78787878-7878-7878-7878-787878787878"
    profile_token = "prof_token_stale_target"

    async with patched_env() as session:
        order1 = WebOrder(
            id=10,
            order_id="order_old_basic",
            profile_token=profile_token,
            tariff_key="adapt_basic",
            tariff_label="Базовый 1 мес",
            days=30,
            amount_rub=200,
            status="delivered",
            subscription_url=f"https://darimiru.ru/vpnbot/adapt-sub/{adapt_uuid}",
            marzban_username=f"adapt_{adapt_uuid}",
            access_expires_at=datetime.utcnow() + timedelta(days=10),
        )
        order2 = WebOrder(
            id=20,
            order_id="order_new_pro",
            profile_token=profile_token,
            tariff_key="adapt_pro",
            tariff_label="Продвинутый 1 мес",
            days=30,
            amount_rub=400,
            status="delivered",
            subscription_url=f"https://darimiru.ru/vpnbot/adapt-sub/{adapt_uuid}",
            marzban_username=f"adapt_{adapt_uuid}",
            access_expires_at=datetime.utcnow() + timedelta(days=25),
        )
        session.add_all([order1, order2])
        await session.commit()

    req = _Request(
        _json_body={
            "tariff_key": "adapt_pro",
            "purchase_action": "renew",
            "target_order_id": "order_old_basic",
            "contact": "stale@example.com",
        },
        cookies={webstore_routes._AUTH_COOKIE: profile_token},
    )

    resp = await webstore_routes.handle_create_order(req)
    assert resp.status == 200
    data = json.loads(resp.text)

    async with patched_env() as session:
        created = await session.scalar(select(WebOrder).where(WebOrder.order_id == data["order_id"]))
        assert created is not None
        assert created.target_snapshot_plan_uuid == "adapt-plan-pro-222"
        assert created.purchase_action == "renew"
        assert created.original_amount_rub == 400


async def test_demo_with_new_request_upgrades_same_uuid_without_creating_new(patched_env, monkeypatch):
    demo_uuid = "00001111-2222-3333-4444-555555555555"
    profile_token = "prof_token_demo_upgrade"

    async with patched_env() as session:
        demo_order = WebOrder(
            id=101,
            order_id="order_demo_101",
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
        _json_body={
            "tariff_key": "adapt_basic",
            "purchase_action": "new",
            "contact": "demouser@example.com",
        },
        cookies={webstore_routes._AUTH_COOKIE: profile_token},
    )

    resp = await webstore_routes.handle_create_order(req)
    assert resp.status == 200
    data = json.loads(resp.text)

    async with patched_env() as session:
        created = await session.scalar(select(WebOrder).where(WebOrder.order_id == data["order_id"]))
        assert created is not None
        assert created.target_order_id == f"uuid:{demo_uuid}"
        assert created.purchase_action == "upgrade"
        assert created.target_snapshot_plan_uuid == "00cce2fe-ee55-4c0d-8bfa-9e6e47cd99a4"


async def test_historical_demo_plan_uses_retail_and_preserves_uuid(patched_env, monkeypatch):
    adapt_uuid = "00002222-3333-4444-5555-666666666666"
    legacy_plan_uuid = "legacy-demo-plan-uuid"
    paid_plan_uuid = "paid-plan-for-legacy-demo"
    profile_token = "prof_token_historical_demo"
    tariffs = [
        {
            "key": "legacy_paid",
            "label": "Базовый 30 дней",
            "days": 30,
            "price_rub": 275,
            "provider": "adapt",
            "adapt_plan_uuid": paid_plan_uuid,
            "adapt_cost_price_usd": None,
            "device_limit": 1,
        },
    ]
    monkeypatch.setattr(webstore_routes, "get_store_tariffs", lambda: tariffs)
    monkeypatch.setattr(
        webstore_routes,
        "get_store_tariffs_by_key",
        lambda: {tariff["key"]: tariff for tariff in tariffs},
    )
    monkeypatch.setattr(
        webstore_routes,
        "get_adapt_plan_metadata_by_uuid",
        lambda uuid: {
            "adapt_plan_uuid": legacy_plan_uuid,
            "adapt_cost_price_usd": None,
            "days": 7,
            "price_rub": 45,
        } if uuid == legacy_plan_uuid else None,
    )

    async with patched_env() as session:
        demo_order = WebOrder(
            id=105,
            order_id="order_historical_demo",
            profile_token=profile_token,
            tariff_key="demo",
            tariff_label="Тестовый доступ",
            days=7,
            amount_rub=45,
            status="demo",
            subscription_url=f"https://darimiru.ru/vpnbot/adapt-sub/{adapt_uuid}",
            marzban_username=f"adapt_{adapt_uuid}",
            access_expires_at=datetime.utcnow() - timedelta(days=2),
            target_snapshot_plan_uuid=legacy_plan_uuid,
        )
        session.add(demo_order)
        await session.commit()

    lookup = await webstore_routes.handle_quick_renew_lookup(
        _Request(_json_body={"query": f"https://darimiru.ru/vpnbot/adapt-sub/{adapt_uuid}"})
    )
    lookup_data = json.loads(lookup.text)
    assert lookup.status == 200
    assert lookup_data["is_trial"] is True
    assert any(item["key"] == "legacy_paid" for item in lookup_data["upgrade_tariffs"])

    response = await webstore_routes.handle_create_order(
        _Request(
            _json_body={
                "tariff_key": "legacy_paid",
                "purchase_action": "upgrade",
                "target_order_id": "order:order_historical_demo",
                "contact": "historical@example.com",
            },
            cookies={webstore_routes._AUTH_COOKIE: profile_token},
        )
    )
    assert response.status == 200
    data = json.loads(response.text)

    async with patched_env() as session:
        created = await session.scalar(select(WebOrder).where(WebOrder.order_id == data["order_id"]))
        assert created is not None
        assert created.amount_rub == 275
        assert created.original_amount_rub == 275
        assert created.purchase_action == "upgrade"
        assert created.target_order_id == f"uuid:{adapt_uuid}"
        assert created.target_snapshot_plan_uuid == legacy_plan_uuid


async def test_expired_paid_with_crafted_new_request_is_rejected(patched_env):
    adapt_uuid = "11112222-3333-4444-5555-666677778888"
    profile_token = "prof_token_expired_paid"

    async with patched_env() as session:
        expired_order = WebOrder(
            id=102,
            order_id="order_exp_102",
            profile_token=profile_token,
            tariff_key="adapt_basic",
            tariff_label="Базовый",
            days=30,
            amount_rub=200,
            status="delivered",
            subscription_url=f"https://darimiru.ru/vpnbot/adapt-sub/{adapt_uuid}",
            marzban_username=f"adapt_{adapt_uuid}",
            access_expires_at=datetime.utcnow() - timedelta(days=5),
        )
        session.add(expired_order)
        await session.commit()

    req = _Request(
        _json_body={
            "tariff_key": "adapt_basic",
            "purchase_action": "new",
            "contact": "expireduser@example.com",
        },
        cookies={webstore_routes._AUTH_COOKIE: profile_token},
    )

    resp = await webstore_routes.handle_create_order(req)
    assert resp.status == 200
    data = json.loads(resp.text)
    async with patched_env() as session:
        created = await session.scalar(select(WebOrder).where(WebOrder.order_id == data["order_id"]))
        assert created is not None
        assert created.purchase_action == "new"
        assert created.target_order_id is None


async def test_active_paid_with_explicit_new_creates_separate_order(patched_env):
    active_uuid = "aaaa1111-2222-3333-4444-555555555555"
    profile_token = "prof_token_active_paid"

    async with patched_env() as session:
        active_order = WebOrder(
            id=103,
            order_id="order_act_103",
            profile_token=profile_token,
            tariff_key="adapt_basic",
            tariff_label="Базовый",
            days=30,
            amount_rub=200,
            status="delivered",
            subscription_url=f"https://darimiru.ru/vpnbot/adapt-sub/{active_uuid}",
            marzban_username=f"adapt_{active_uuid}",
            access_expires_at=datetime.utcnow() + timedelta(days=20),
        )
        session.add(active_order)
        await session.commit()

    req = _Request(
        _json_body={
            "tariff_key": "adapt_pro",
            "purchase_action": "new",
            "contact": "activeuser@example.com",
        },
        cookies={webstore_routes._AUTH_COOKIE: profile_token},
    )

    resp = await webstore_routes.handle_create_order(req)
    assert resp.status == 200
    data = json.loads(resp.text)

    async with patched_env() as session:
        created = await session.scalar(select(WebOrder).where(WebOrder.order_id == data["order_id"]))
        assert created is not None
        assert created.purchase_action == "new"
        assert created.target_order_id is None


async def test_renew_and_upgrade_preserve_original_uuid_and_url_on_fulfillment(patched_env, monkeypatch):
    target_uuid = "bbbb1111-2222-3333-4444-555555555555"
    profile_token = "prof_token_preserve"

    async with patched_env() as session:
        base_order = WebOrder(
            id=104,
            order_id="order_base_104",
            profile_token=profile_token,
            tariff_key="adapt_basic",
            tariff_label="Базовый",
            days=30,
            amount_rub=200,
            status="delivered",
            subscription_url=f"https://darimiru.ru/vpnbot/adapt-sub/{target_uuid}",
            marzban_username=f"adapt_{target_uuid}",
            access_expires_at=datetime.utcnow() - timedelta(days=2),  # Expired
        )
        session.add(base_order)
        await session.commit()

    mock_renew = AsyncMock(return_value={"uuid": target_uuid, "end_date": (datetime.utcnow() + timedelta(days=30)).isoformat()})
    mock_custom_renew = AsyncMock(return_value={"uuid": target_uuid})
    mock_upgrade = AsyncMock(return_value={"uuid": target_uuid})
    mock_get_status = AsyncMock(side_effect=[
        {
            "uuid": target_uuid,
            "plan_uuid": "adapt-plan-basic-111",
            "devices": 1,
            "is_active": False,
            "end_date": (datetime.utcnow() - timedelta(days=2)).isoformat(),
        },
        {
            "uuid": target_uuid,
            "plan_uuid": "adapt-plan-basic-111",
            "devices": 1,
            "is_active": True,
            "end_date": (datetime.utcnow() + timedelta(days=3)).isoformat(),
        },
        {
            "uuid": target_uuid,
            "plan_uuid": "adapt-plan-pro-222",
            "devices": 3,
            "is_active": True,
            "end_date": (datetime.utcnow() + timedelta(days=30)).isoformat(),
        },
    ])
    mock_list_plans = AsyncMock(return_value=[
        {"uuid": "adapt-plan-basic-111", "days": 30, "price_usd": "1.00", "devices": 1, "is_active": True},
        {"uuid": "adapt-plan-pro-222", "days": 30, "price_usd": "2.00", "devices": 3, "is_active": True},
    ])

    monkeypatch.setattr("webstore.routes.AdaptAPI.renew_subscription", mock_renew)
    monkeypatch.setattr("webstore.routes.AdaptAPI.renew_subscription_custom", mock_custom_renew)
    monkeypatch.setattr("webstore.routes.AdaptAPI.upgrade_subscription", mock_upgrade)
    monkeypatch.setattr("webstore.routes.AdaptAPI.get_status", mock_get_status)
    monkeypatch.setattr("webstore.routes.AdaptAPI.list_plans", mock_list_plans)

    req = _Request(
        _json_body={
            "tariff_key": "adapt_pro",
            "purchase_action": "upgrade",
            "target_order_id": "order_base_104",
            "contact": "preserve@example.com",
        },
        cookies={webstore_routes._AUTH_COOKIE: profile_token},
    )
    resp = await webstore_routes.handle_create_order(req)
    assert resp.status == 200
    data = json.loads(resp.text)

    async with patched_env() as session:
        order = await session.scalar(select(WebOrder).where(WebOrder.order_id == data["order_id"]))
        assert order is not None
        await webstore_routes._fulfill_adapt_order(session, order, "adapt-plan-pro-222")
        await session.commit()

        delivered = await session.scalar(select(WebOrder).where(WebOrder.order_id == data["order_id"]))
        assert delivered.status == "delivered"
        assert target_uuid in delivered.subscription_url
        assert delivered.marzban_username == f"adapt_{target_uuid}"


async def test_known_telegram_adapt_renewal_succeeds_without_adapt_api_call(patched_env, monkeypatch):
    adapt_uuid = "11111111-2222-3333-4444-555555555555"
    profile_token = "prof_token_tg_user"

    async with patched_env() as session:
        tg_item = WebTelegramItem(
            profile_token=profile_token,
            telegram_id=123456,
            item_type="vpn",
            external_id="sub_10",
            title="Adapt VPN",
            key_value=f"https://darimiru.ru/vpnbot/adapt-sub/{adapt_uuid}",
            provider="adapt",
            adapt_plan_uuid="adapt-plan-basic-111",
            status="active",
            expires_at=datetime.utcnow() + timedelta(days=20),
        )
        session.add(tg_item)
        await session.commit()

    mock_get_status = AsyncMock(side_effect=RuntimeError("Adapt API should NOT be called during known checkout!"))
    monkeypatch.setattr("webstore.routes.AdaptAPI.get_status", mock_get_status)

    req = _Request(
        _json_body={
            "tariff_key": "adapt_basic",
            "purchase_action": "renew",
            "target_order_id": f"tg:{adapt_uuid}",
            "contact": "tguser@example.com",
        },
        cookies={webstore_routes._AUTH_COOKIE: profile_token},
    )

    resp = await webstore_routes.handle_create_order(req)
    assert resp.status == 200
    data = json.loads(resp.text)
    assert "redirect_url" in data
    mock_get_status.assert_not_called()

    async with patched_env() as session:
        order = await session.scalar(select(WebOrder).where(WebOrder.order_id == data["order_id"]))
        assert order is not None
        assert order.target_order_id == f"uuid:{adapt_uuid}"
        assert order.target_snapshot_plan_uuid == "adapt-plan-basic-111"
        assert order.purchase_action == "renew"


async def test_unknown_non_demo_plan_fails_closed(patched_env):
    adapt_uuid = "44444444-5555-6666-7777-888888888888"
    profile_token = "prof_token_unknown_plan"

    async with patched_env() as session:
        tg_item = WebTelegramItem(
            profile_token=profile_token,
            telegram_id=123456,
            item_type="vpn",
            external_id="sub_55",
            title="Неизвестный VPN",
            key_value=f"https://darimiru.ru/vpnbot/adapt-sub/{adapt_uuid}",
            provider="adapt",
            adapt_plan_uuid="unknown-legacy-plan-999",
            status="active",
            expires_at=datetime.utcnow() + timedelta(days=10),
        )
        session.add(tg_item)
        await session.commit()

    req = _Request(
        _json_body={
            "tariff_key": "adapt_pro",
            "purchase_action": "upgrade",
            "target_order_id": f"tg:{adapt_uuid}",
            "contact": "unknown@example.com",
        },
        cookies={webstore_routes._AUTH_COOKIE: profile_token},
    )

    resp = await webstore_routes.handle_create_order(req)
    assert resp.status == 400
    data = json.loads(resp.text)
    assert "недоступно" in data["error"]
