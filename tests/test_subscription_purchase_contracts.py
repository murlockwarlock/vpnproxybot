from datetime import datetime, timedelta
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from test_adapt_web_renew_and_devices import (
    _FakeYooKassaPaymentApi, _Request, bot_engine, bot_session_factory,
    patched_env, web_engine, web_session_factory,
)
from bot.handlers import buy, payment as payment_handler
from bot.models import AdaptSubscription, Payment, PaymentStatus, Platform, Server, Subscription, SubStatus, Tariff, User, VhqSubscriptionFulfillment
from bot.services import subscription_service, webstore_bridge
from bot.services.purchase_intent import get_purchase_price_rub
from bot.services.provisioning_issues import AccessProvisionError
from bot.services.vhq_subscription_proxy import get_subscription_display_key
from webstore import routes
from webstore.models import WebOrder, WebTelegramItem

pytestmark = pytest.mark.asyncio
UUID = "67676767-6767-6767-6767-676767676767"


@pytest.fixture
def frozen_now(monkeypatch):
    now = datetime.utcnow()

    class FixedDatetime(datetime):
        @classmethod
        def utcnow(cls):
            return now

    monkeypatch.setattr(routes, "datetime", FixedDatetime)
    monkeypatch.setattr("bot.services.purchase_intent.datetime", FixedDatetime)
    return now


async def seed_web_target(sessions, now, *, days=15.25):
    async with sessions() as session:
        session.add(WebOrder(
            order_id="current-order", contact="owner@example.com", profile_token="owner-profile",
            tariff_key="adapt_basic", tariff_label="Базовый", days=30, amount_rub=200,
            status="delivered", subscription_url=f"https://example.test/adapt-sub/{UUID}",
            target_snapshot_plan_uuid="adapt-plan-basic-111",
            access_expires_at=now + timedelta(days=days),
        ))
        await session.commit()


@pytest.mark.parametrize(("days", "historical", "target_days", "expected"), [
    (15.25, True, 30, 299), (45.25, False, 30, 99), (15.25, False, 90, 299),
])
async def test_profile_quote_and_checkout_share_authoritative_price(
    patched_env, monkeypatch, frozen_now, days, historical, target_days, expected,
):
    all_tariffs = routes.get_store_tariffs()
    current, target = all_tariffs[1:]
    target["days"] = target_days
    metadata = {t["adapt_plan_uuid"]: t for t in all_tariffs}
    monkeypatch.setattr(routes, "get_adapt_plan_metadata_by_uuid", metadata.get)
    plans = await routes.AdaptAPI().list_plans()
    plans[2]["days"] = target_days
    if historical:
        monkeypatch.setattr(routes, "get_store_tariffs", lambda: [target])
        monkeypatch.setattr(routes, "get_store_tariffs_by_key", lambda: {target["key"]: target})
    await seed_web_target(patched_env, frozen_now, days=days)
    lookup = await routes.handle_quick_renew_lookup(_Request(
        _json_body={"target_order_id": "current-order", "expires_at": "2000-01-01", "expired": True},
        cookies={routes._AUTH_COOKIE: "owner-profile"},
    ))
    assert lookup.status == 200
    options = json.loads(lookup.text)
    assert options["is_expired"] is False
    quoted = next(t for t in options["upgrade_tariffs"] if t["key"] == target["key"])
    assert quoted["payable_price_rub"] == expected
    response = await routes.handle_create_order(_Request(
        _json_body={"tariff_key": target["key"], "purchase_action": "upgrade",
                    "target_order_id": "current-order", "contact": "owner@example.com"},
        cookies={routes._AUTH_COOKIE: "owner-profile"},
    ))
    assert response.status == 200
    assert _FakeYooKassaPaymentApi.last_params["amount"]["value"] == f"{expected:.2f}"
    async with patched_env() as session:
        order = await session.scalar(select(WebOrder).where(WebOrder.order_id == json.loads(response.text)["order_id"]))
        assert order.amount_rub == expected
        assert order.target_snapshot_expires_at == frozen_now + timedelta(days=days)


@pytest.mark.parametrize("target", ["current-order", "order:1", "tgitem:1", f"tg:{UUID}"])
@pytest.mark.parametrize(("token", "status"), [("", 401), ("foreign-profile", 404)])
async def test_profile_quotes_do_not_disclose_foreign_subscription(patched_env, frozen_now, target, token, status):
    await seed_web_target(patched_env, frozen_now)
    async with patched_env() as session:
        session.add(WebTelegramItem(
            id=1, profile_token="owner-profile", telegram_id=123, item_type="vpn", external_id=UUID,
            title="Базовый", key_value=f"https://example.test/adapt-sub/{UUID}",
            provider="adapt", adapt_plan_uuid="adapt-plan-basic-111",
        ))
        await session.commit()
    response = await routes.handle_quick_renew_lookup(_Request(
        _json_body={"target_order_id": target}, cookies={routes._AUTH_COOKIE: token},
    ))
    assert response.status == status
    assert "upgrade_tariffs" not in json.loads(response.text)


@pytest.mark.parametrize("target", ["current-order", "tgitem:1", f"tg:{UUID}"])
async def test_profile_quote_uses_the_same_owned_snapshot_as_checkout(patched_env, monkeypatch, frozen_now, target):
    metadata = {t["adapt_plan_uuid"]: t for t in routes.get_store_tariffs()}
    monkeypatch.setattr(routes, "get_adapt_plan_metadata_by_uuid", metadata.get)
    await seed_web_target(patched_env, frozen_now)
    async with patched_env() as session:
        session.add_all([
            WebTelegramItem(
                id=1, profile_token="owner-profile", telegram_id=123, item_type="vpn", external_id=UUID,
                title="Базовый", key_value=f"https://example.test/adapt-sub/{UUID}",
                provider="adapt", adapt_plan_uuid="adapt-plan-basic-111",
                expires_at=frozen_now + timedelta(days=10.25),
            ),
            WebOrder(
                order_id="foreign-latest", contact="foreign@example.com", profile_token="foreign-profile",
                tariff_key="adapt_pro", tariff_label="Другой снимок", days=30, amount_rub=400,
                status="delivered", subscription_url=f"https://example.test/adapt-sub/{UUID}",
                target_snapshot_plan_uuid="adapt-plan-pro-222", access_expires_at=frozen_now + timedelta(days=30),
            ),
        ])
        await session.commit()
    request = _Request(_json_body={"target_order_id": target}, cookies={routes._AUTH_COOKIE: "owner-profile"})
    response = await routes.handle_quick_renew_lookup(request)
    assert response.status == 200
    quoted = next(t for t in json.loads(response.text)["upgrade_tariffs"] if t["key"] == "adapt_pro")
    assert quoted["payable_price_rub"] == (299 if target == "current-order" else 332)
    request._json_body.update(tariff_key="adapt_pro", purchase_action="upgrade", contact="owner@example.com")
    response = await routes.handle_create_order(request)
    assert response.status == 200
    assert _FakeYooKassaPaymentApi.last_params["amount"]["value"] == f'{quoted["payable_price_rub"]:.2f}'


async def test_vhq_web_profile_offers_only_supported_renewal(patched_env, monkeypatch, frozen_now):
    tariffs = [
        {"key": "vhq_basic", "label": "База Про", "price_rub": 399, "days": 30, "provider": "vhq", "vhq_tier": "basic"},
        {"key": "vhq_trial", "label": "Пробный", "price_rub": 10, "days": 1, "provider": "vhq", "vhq_tier": "lite"},
    ]
    monkeypatch.setattr(routes, "get_store_tariffs", lambda: tariffs)
    monkeypatch.setattr(routes, "get_store_tariffs_by_key", lambda: {t["key"]: t for t in tariffs})
    async with patched_env() as session:
        session.add(WebOrder(
            order_id="vhq-current", contact="owner@example.com", profile_token="owner-profile",
            tariff_key="vhq_basic", tariff_label="База Про", days=30, amount_rub=399,
            status="delivered", vhq_order_id="supplier-current", subscription_url="https://example.test/vhq-sub/stable",
            access_expires_at=frozen_now + timedelta(days=10),
        ))
        await session.commit()
    request = _Request(_json_body={"target_order_id": "vhq-current"}, cookies={routes._AUTH_COOKIE: "owner-profile"})
    response = await routes.handle_quick_renew_lookup(request)
    assert response.status == 200
    data = json.loads(response.text)
    assert [t["key"] for t in data["renew_tariffs"]] == ["vhq_basic"]
    assert data["renew_tariffs"][0]["payable_price_rub"] == 399
    assert data["upgrade_tariffs"] == []
    request._json_body.update(tariff_key="vhq_basic", purchase_action="renew", contact="owner@example.com")
    response = await routes.handle_create_order(request)
    assert response.status == 200
    assert _FakeYooKassaPaymentApi.last_params["amount"]["value"] == "399.00"


async def seed_bot_vhq(sessions):
    async with sessions() as session:
        user = User(telegram_id=1004, full_name="Owner", platform=Platform.WINDOWS)
        server = Server(id=1, name="S1", host="1.1.1.1", location="Test", is_active=True)
        tariff = Tariff(id=12, label="База Про", price_rub=399, days=30, vhq_tier="basic", is_active=True)
        session.add_all([user, server, tariff])
        await session.flush()
        session.add(Subscription(
            id=504, user_id=user.id, server_id=server.id, tariff_id=tariff.id,
            tariff_months=1, tariff_days=30, billing_mode="tariff", client_name="vhq_order_existing",
            vpn_key="https://supplier.test/existing", platform=Platform.WINDOWS,
            status=SubStatus.ACTIVE, expires_at=datetime.utcnow() + timedelta(days=15),
        ))
        await session.commit()


async def test_vhq_bot_renew_button_opens_payment_with_same_target(bot_session_factory, monkeypatch):
    await seed_bot_vhq(bot_session_factory)
    monkeypatch.setattr(buy, "async_session", bot_session_factory)
    callback = SimpleNamespace(from_user=SimpleNamespace(id=1004), answer=AsyncMock(),
                               message=SimpleNamespace(edit_text=AsyncMock()))
    await buy._open_renew_target(callback, 504)
    args = callback.message.edit_text.await_args
    assert args is not None
    assert "399₽" in args.args[0]
    buttons = [b for row in args.kwargs["reply_markup"].inline_keyboard for b in row]
    assert any("~r~504" in (b.callback_data or "") for b in buttons)


async def test_vhq_balance_payment_extends_original_subscription_and_preserves_public_url(bot_session_factory, monkeypatch):
    from bot.config import settings

    await seed_bot_vhq(bot_session_factory)
    monkeypatch.setattr(payment_handler, "async_session", bot_session_factory)
    monkeypatch.setattr(settings, "vhq_subscription_proxy_base_url", "https://example.test")
    monkeypatch.setattr(settings, "vhq_subscription_proxy_secret", "test-stable-reference")
    monkeypatch.setattr(payment_handler, "_send_subscription_key_for_platform", AsyncMock())
    monkeypatch.setattr(payment_handler, "notify_admins_payment", AsyncMock())
    monkeypatch.setattr("bot.services.payment_service.credit_partner", AsyncMock())
    monkeypatch.setattr("bot.services.payment_service.credit_cashback", AsyncMock())
    async with bot_session_factory() as session:
        user = await session.scalar(select(User).where(User.telegram_id == 1004))
        user.balance_rub = 1000
        sub = await session.get(Subscription, 504)
        before_url = get_subscription_display_key(sub)
        previous_end = sub.expires_at
        sub.vhq_order_id = "supplier-existing"
        sub.vhq_active_until = previous_end
        await session.commit()
    renew = AsyncMock(return_value={"order_id": "supplier-existing", "status": "renewed",
                                   "active_until": (previous_end + timedelta(days=30)).isoformat()})
    monkeypatch.setattr(subscription_service.VHQPartnerAPI, "renew", renew)
    create = AsyncMock()
    monkeypatch.setattr(subscription_service.VHQPartnerAPI, "buy", create)
    callback = SimpleNamespace(
        data="pay_balance_12_windows~r~504_0", bot=SimpleNamespace(send_message=AsyncMock()),
        from_user=SimpleNamespace(id=1004, full_name="Owner", username="owner"), answer=AsyncMock(),
        message=SimpleNamespace(answer=AsyncMock(), chat=SimpleNamespace(id=1004)),
    )
    await payment_handler.initiate_balance_payment(callback)
    assert callback.message.answer.await_args.args[0] == "✅ Оплата с баланса успешна!"
    renew.assert_awaited_once_with(order_id="supplier-existing")
    create.assert_not_awaited()
    async with bot_session_factory() as session:
        user = await session.scalar(select(User).where(User.telegram_id == 1004))
        assert user.balance_rub == 601
        sub = await session.get(Subscription, 504)
        assert sub.expires_at == previous_end + timedelta(days=30)
        assert get_subscription_display_key(sub) == before_url
        payments = (await session.scalars(select(Payment))).all()
        assert len(payments) == 1
        assert payments[0].amount == 39900
        assert payments[0].status == PaymentStatus.COMPLETED
        assert payments[0].subscription_id == 504
        assert len((await session.scalars(select(Subscription))).all()) == 1
        fulfillment = await session.scalar(select(VhqSubscriptionFulfillment))
        assert fulfillment.status == "completed"


@pytest.mark.parametrize("target", [504, 999])
async def test_vhq_invalid_explicit_target_never_creates_replacement(bot_session_factory, monkeypatch, target):
    await seed_bot_vhq(bot_session_factory)
    create = AsyncMock()
    monkeypatch.setattr(subscription_service.VHQPartnerAPI, "buy", create)
    async with bot_session_factory() as session:
        user = User(id=999, telegram_id=999)
        tariff = await session.get(Tariff, 12)
        with pytest.raises(AccessProvisionError) as error:
            await subscription_service.create_or_extend_paid_access(
                session, user=user, tariff=tariff, platform=Platform.WINDOWS,
                purchase_action="renew", target_subscription_id=target,
            )
        assert error.value.code == "invalid_renewal_target"
    create.assert_not_awaited()


@pytest.mark.parametrize("variant", ["foreign", "adapt", "upgrade", "lite", "inactive", "admin_only", "zero", "wrong_state"])
async def test_vhq_renew_quote_rejects_invalid_target_or_tariff(bot_session_factory, variant):
    await seed_bot_vhq(bot_session_factory)
    async with bot_session_factory() as session:
        user = await session.scalar(select(User).where(User.telegram_id == 1004))
        sub = await session.get(Subscription, 504)
        tariff = await session.get(Tariff, 12)
        action = "renew"
        if variant == "foreign":
            user = User(id=999, telegram_id=999)
        elif variant == "adapt":
            tariff.adapt_plan_uuid = "adapt-plan"
        elif variant == "upgrade":
            action = "upgrade"
        elif variant == "lite":
            tariff.vhq_tier, tariff.days = "lite", 7
        elif variant == "inactive":
            tariff.is_active = False
        elif variant == "admin_only":
            tariff.is_admin_only = True
        elif variant == "zero":
            tariff.price_rub = 0
        else:
            sub.status = SubStatus.CANCELLED
        with pytest.raises(ValueError):
            await get_purchase_price_rub(session, user=user, tariff=tariff, action=action, target_subscription_id=504)


@pytest.mark.parametrize("action", ["renew", "upgrade"])
@pytest.mark.parametrize("original_base", ["https://network-api.adaptgroup.app/sub", "https://legacy.test/adapt-sub"])
async def test_adapt_paid_operation_preserves_exact_url_and_bridge(
    bot_session_factory, monkeypatch, action, original_base,
):
    original = f"{original_base}/{UUID}"
    end = datetime.utcnow() + timedelta(days=30)
    plan = "adapt-pro" if action == "upgrade" else "adapt-basic"
    status = {"uuid": UUID, "plan_uuid": plan, "devices": 1, "is_active": True, "end_date": end.isoformat()}
    monkeypatch.setattr(subscription_service.AdaptAPI, "renew_subscription", AsyncMock(return_value=status))
    monkeypatch.setattr(subscription_service.AdaptAPI, "upgrade_subscription", AsyncMock(return_value=status))
    previous_status = {**status, "plan_uuid": "adapt-basic", "end_date": (datetime.utcnow() + timedelta(days=15)).isoformat()}
    monkeypatch.setattr(subscription_service.AdaptAPI, "get_status", AsyncMock(side_effect=[previous_status, status, status]))
    monkeypatch.setattr(subscription_service.AdaptAPI, "list_plans", AsyncMock(return_value=[
        {"uuid": "adapt-basic", "days": 30, "price_usd": "1.00", "devices": 1, "is_active": True},
        {"uuid": "adapt-pro", "days": 30, "price_usd": "2.00", "devices": 1, "is_active": True},
    ]))
    create = AsyncMock()
    monkeypatch.setattr(subscription_service.AdaptAPI, "create_subscription", create)
    async with bot_session_factory() as session:
        user = User(telegram_id=1003, full_name="Owner")
        server = Server(id=1, name="S1", host="1.1.1.1", location="Test", is_active=True)
        current = Tariff(id=12, label="Basic", price_rub=200, days=30, adapt_plan_uuid="adapt-basic")
        target = Tariff(id=13, label="Pro", price_rub=400, days=30, adapt_plan_uuid="adapt-pro")
        session.add_all([user, server, current, target])
        await session.flush()
        sub = Subscription(id=503, user_id=user.id, server_id=1, tariff_id=12, tariff_months=1, tariff_days=30,
                           billing_mode="tariff", client_name=f"adapt_{UUID}", vpn_key=original,
                           platform=Platform.WINDOWS, status=SubStatus.ACTIVE,
                           expires_at=datetime.utcnow() + timedelta(days=15))
        sub.server, sub.tariff = server, current
        session.add(sub)
        await session.flush()
        record = AdaptSubscription(subscription_id=503, adapt_uuid=UUID, adapt_plan_uuid="adapt-basic", end_date=sub.expires_at)
        session.add(record)
        await session.commit()
        result, url = await subscription_service.create_or_extend_paid_access(
            session, user=user, tariff=target if action == "upgrade" else current,
            platform=Platform.WINDOWS, purchase_action=action, target_subscription_id=503,
        )
        assert url == original
        assert result.id == 503
        assert webstore_bridge.serialize_profile_items(user, [result], [], {503: record})[0]["key_value"] == original
        await session.commit()
    async with bot_session_factory() as session:
        assert (await session.get(Subscription, 503)).vpn_key == original
    create.assert_not_awaited()


async def test_profile_upgrade_browser_uses_server_options_across_timezones_and_widths(
    patched_env, monkeypatch, frozen_now, tmp_path,
):
    from aiohttp import web

    playwright = pytest.importorskip("playwright.async_api")
    current, target = routes.get_store_tariffs()[1:]
    current["description"] = target["description"] = ""
    catalog = [current, target]
    monkeypatch.setattr(routes, "get_store_tariffs", lambda: catalog)
    monkeypatch.setattr(routes, "get_store_tariffs_by_key", lambda: {t["key"]: t for t in catalog})
    monkeypatch.setattr(routes, "get_adapt_plan_metadata_by_uuid", {t["adapt_plan_uuid"]: t for t in [current, target]}.get)
    monkeypatch.setattr(routes, "_fetch_public_balance_config", AsyncMock(return_value={}))
    monkeypatch.setattr(routes, "_get_web_daily_charge_rub", AsyncMock(return_value=3))
    plans = await routes.AdaptAPI().list_plans()
    await seed_web_target(patched_env, frozen_now)
    app = web.Application()
    routes.setup_routes(app)
    app.router.add_get("/buy", routes.handle_store_page)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        async with playwright.async_playwright() as runtime:
            try:
                browser = await runtime.chromium.launch(executable_path=os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE"))
            except playwright.Error as exc:
                if "Executable doesn't exist" in str(exc):
                    pytest.skip("Playwright Chromium is not installed")
                raise
            try:
                for scenario, days, historical, target_days, expected in [
                    ("historical", 15.25, True, 30, 299),
                    ("accumulated", 45.25, False, 30, 99),
                    ("normal", 15.25, False, 30, 299),
                    ("longer", 15.25, False, 90, 299),
                ]:
                    target["days"] = plans[2]["days"] = target_days
                    catalog = [target] if historical else [current, target]
                    async with patched_env() as session:
                        order = await session.scalar(select(WebOrder).where(WebOrder.order_id == "current-order"))
                        order.access_expires_at = frozen_now + timedelta(days=days)
                        await session.commit()
                    for timezone in ("UTC", "Asia/Almaty"):
                        context = await browser.new_context(timezone_id=timezone)
                        await context.add_cookies([{"name": routes._AUTH_COOKIE, "value": "owner-profile", "url": base}])
                        page = await context.new_page()
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))

                        async def network(route):
                            if route.request.url.startswith(base):
                                await route.continue_()
                            elif route.request.url == "https://pay.test/redirect":
                                await route.fulfill(status=200, body="Test payment")
                            else:
                                await route.abort()

                        await page.route("**/*", network)
                        for width in (320, 360, 390, 768, 1024, 1440):
                            await page.set_viewport_size({"width": width, "height": 900})
                            await page.goto(f"{base}/profile?token=owner-profile", wait_until="domcontentloaded")
                            await page.get_by_role("link", name="↑ Улучшить", exact=True).click()
                            card = page.locator("#tariffCards .tariff-card").filter(has=page.locator('[data-key="adapt_pro"]'))
                            await playwright.expect(card.locator(".tariff-price")).to_have_text(f"{expected} ₽")
                            await playwright.expect(page.locator("#upgradeSummary")).to_be_visible()
                            if await page.locator("#cookieAcceptBtn").is_visible():
                                await playwright.expect(page.locator("#supportFab")).to_be_hidden()
                                await page.locator("#cookieAcceptBtn").click()
                                await playwright.expect(page.locator("#supportFab")).to_be_visible()
                            assert await page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
                            await page.locator("#upgradeSummary").scroll_into_view_if_needed()
                            await page.screenshot(path=str(tmp_path / f"{scenario}-{timezone.replace('/', '-')}-{width}.png"))
                        await page.reload()
                        await playwright.expect(card.locator(".tariff-price")).to_have_text(f"{expected} ₽")
                        await card.locator(".buy-btn").click()
                        await page.wait_for_url("https://pay.test/redirect")
                        assert _FakeYooKassaPaymentApi.last_params["amount"]["value"] == f"{expected:.2f}"
                        assert not errors
                        await context.close()
                page = await browser.new_page()
                await page.route("**/*", network)
                await page.goto(f"{base}/buy?action=upgrade&target_order_id=current-order", wait_until="domcontentloaded")
                await playwright.expect(page.locator("#tariffCards .buy-btn")).to_have_count(0)
                await playwright.expect(page.locator("#tariffCards")).to_contain_text("Сначала войдите")
                await playwright.expect(page.get_by_role("button", name="Повторить", exact=True)).to_be_visible()
                await page.context.add_cookies([{"name": routes._AUTH_COOKIE, "value": "owner-profile", "url": base}])
                await page.get_by_role("button", name="Повторить", exact=True).click()
                await playwright.expect(page.locator("#tariffCards .tariff-price")).to_have_text("299 ₽")
                monkeypatch.setattr(routes.AdaptAPI, "list_plans", AsyncMock(side_effect=TimeoutError("provider unavailable")))
                await page.reload(wait_until="domcontentloaded")
                await playwright.expect(page.locator("#tariffCards")).to_contain_text("нет подходящих тарифов")
                await playwright.expect(page.locator("#tariffCards .buy-btn")).to_have_count(0)
            finally:
                await browser.close()
    finally:
        await runner.cleanup()
