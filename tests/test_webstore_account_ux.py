from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiohttp import web
from sqlalchemy import select

from test_webstore_telegram_linking import db_engine, db_session_factory, patched_webstore, _Request
from webstore import otp_auth, routes
from webstore.models import WebAccount, WebBalanceAccount, WebBalanceTransaction, WebOrder
from bot.services.adapt_subscription_proxy import inject_adapt_devices_manager

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def account_server(patched_webstore, monkeypatch):
    monkeypatch.setattr(routes, "_fetch_public_balance_config", AsyncMock(return_value={"daily_charge_rub": 3.17, "referral_commission_percent": 10}))
    monkeypatch.setattr(routes, "_get_web_daily_charge_rub", AsyncMock(return_value=3.17))
    monkeypatch.setattr(routes.settings, "site_logo_path", str(routes.TEMPLATES_DIR.parent / "assets/internet-accelerator-logo.svg"))
    monkeypatch.setattr(routes.settings, "demo_key_enabled", True)
    monkeypatch.setattr(routes.settings, "adapt_demo_enabled", True)
    monkeypatch.setattr(routes.AdaptAPI, "create_subscription", AsyncMock(return_value={"uuid": "demo-provider-id", "subscription_url": "https://example.test/sub/demo"}))
    monkeypatch.setattr(routes, "build_adapt_mirror_url", lambda uuid: "https://example.test/sub/" + uuid)
    app = web.Application()
    routes.setup_routes(app)
    runner = web.AppRunner(app, shutdown_timeout=0.1)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    yield url, patched_webstore
    await runner.cleanup()


async def register_account():
    from unittest.mock import patch
    sender = AsyncMock()
    with patch.object(routes, "send_email", sender):
        response = await routes.handle_auth_register(_Request(_json_body={"contact": "new@example.com", "password": "safe-password", "password2": "safe-password"}))
        assert response.status == 200
        raw = sender.await_args.args[2].split("#")[-1]
        response = await routes.handle_email_token(_Request(_json_body={"token": raw}, path="/api/store/auth/confirm"))
        assert response.status == 200
    response = await routes.handle_auth_login(_Request(_json_body={"contact": "new@example.com", "password": "safe-password"}))
    token = json.loads(response.text)["token"]
    return token, {routes._AUTH_COOKIE: response.cookies[routes._AUTH_COOKIE].value}


async def confirm_and_login(page, sender, email):
    raw = sender.await_args.args[2].split("#")[-1]
    result = await page.evaluate("""async ({raw, email}) => {
        const confirm = await fetch('/api/store/auth/confirm', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({token:raw})});
        if (!confirm.ok) return {confirmed:false};
        return await (await fetch('/api/store/auth/login', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({contact:email,password:'safe-password'})})).json();
    }""", {"raw": raw, "email": email})
    assert result.get("token")
    await page.evaluate("token => { if (typeof saveProfileToken === 'function') saveProfileToken(token); }", result["token"])
    return result


async def test_zero_orders_session_referral_and_access(patched_webstore, monkeypatch):
    monkeypatch.setattr(routes, "_get_web_daily_charge_rub", AsyncMock(return_value=3.17))
    token, cookies = await register_account()
    for _ in range(2):
        response = await routes.handle_profile_orders(_Request(query={"token": token}, cookies=cookies))
        assert response.status == 200
        data = json.loads(response.text)
        assert data["orders"] == [] and data["contact"] == "new@example.com"
        assert data["referral"]["site_url"].endswith("/buy?ref=" + data["referral"]["ref_code"])
        assert data["balance"]["balance_rub"] == 0
        assert data["balance_history"] == data["web_balance_history"] == []
        assert not data["balance"]["balance_mode_enabled"]
        assert not data["balance"]["can_manage_daily_billing"]
    cookie_only = await routes.handle_profile_orders(_Request(cookies=cookies))
    assert cookie_only.status == 200
    assert json.loads(cookie_only.text)["referral"]["site_url"] == data["referral"]["site_url"]
    assert (await routes.handle_profile_link_init(_Request(_json_body={"token": token}, cookies=cookies))).status == 200
    for invalid in [token, "nonexistent"]:
        response = await routes.handle_profile_orders(_Request(query={"token": invalid}))
        assert response.status in (401, 404)
    foreign = await routes.handle_profile_orders(_Request(query={"token": token}, cookies={routes._AUTH_COOKIE: "different-session"}))
    assert foreign.status == 403 and routes._AUTH_COOKIE not in foreign.cookies
    assert (await routes.handle_profile_link_init(_Request(_json_body={"token": token}))).status == 401
    assert (await routes.handle_create_profile(_Request(_json_body={"contact": "new@example.com"}))).status == 401
    assert (await routes.handle_create_balance_topup(_Request(_json_body={"token": token, "amount_rub": 100}))).status == 401
    assert (await routes.handle_create_balance_topup(_Request(_json_body={"token": "unknown", "amount_rub": 100}))).status == 401
    response = await routes.handle_auth_logout(_Request(cookies=cookies))
    assert response.cookies[routes._AUTH_COOKIE]["max-age"] == "0"
    denied = await routes.handle_auth_login(_Request(_json_body={"contact": "new@example.com", "password": "incorrect"}))
    assert denied.status == 401 and routes._AUTH_COOKIE not in denied.cookies
    login = await routes.handle_auth_login(_Request(_json_body={"contact": "new@example.com", "password": "safe-password"}))
    assert login.status == 200 and json.loads(login.text)["token"] == token
    assert not json.loads((await routes.handle_auth_me(_Request(cookies=cookies))).text)["authenticated"]
    async with patched_webstore() as session:
        assert (await session.execute(select(WebOrder))).scalars().all() == []
        assert (await session.get(WebBalanceAccount, token)).ref_code == data["referral"]["ref_code"]


@pytest.mark.parametrize("kind,balance", [("demo", 0), ("demo", 100), ("delivered", 100), ("empty", 0)])
async def test_public_daily_enable_is_denied(patched_webstore, monkeypatch, kind, balance):
    monkeypatch.setattr(routes, "_get_web_daily_charge_rub", AsyncMock(return_value=3.17))
    token, cookies = await register_account()
    async with patched_webstore() as session:
        account = await session.get(WebBalanceAccount, token)
        account.balance_rub = balance
        if kind != "empty":
            session.add(WebOrder(order_id="daily-order", profile_token=token, contact="new@example.com", tariff_key="demo" if kind == "demo" else "vpn_30", tariff_label=kind, status=kind, days=3, amount_rub=0 if kind == "demo" else 100))
        await session.commit()
    response = await routes.handle_balance_toggle(_Request(_json_body={"token": token, "enabled": True}, cookies=cookies))
    assert response.status == 403
    async with patched_webstore() as session:
        account = await session.get(WebBalanceAccount, token)
        assert account.balance_rub == balance
        assert not account.balance_mode_enabled and not account.balance_autodebit_enabled
        assert (await session.execute(select(WebBalanceTransaction))).scalars().all() == []


@pytest_asyncio.fixture
async def browser():
    playwright = pytest.importorskip("playwright.async_api")
    async with playwright.async_playwright() as runtime:
        try:
            instance = await runtime.chromium.launch()
        except playwright.Error:
            try:
                instance = await runtime.chromium.launch(channel="chrome")
            except playwright.Error:
                pytest.skip("Install Playwright Chromium or Chrome to run browser checks")
        yield instance
        await instance.close()


async def test_account_browser_journey(account_server, browser, tmp_path, monkeypatch):
    from playwright.async_api import expect

    url, factory = account_server
    sender = AsyncMock()
    monkeypatch.setattr(otp_auth, "send_email", sender)
    context = await browser.new_context(permissions=["clipboard-read", "clipboard-write"])
    page = await context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    await page.goto(url + "/buy")
    await expect(page.locator("#heroAccountActions")).to_be_visible()
    assert await page.locator("#heroAccountActions a[href='/profile']").count() == 1
    assert await page.locator("#heroAccountActions a[href='/profile#telegram']").count() == 1
    assert await page.locator("#heroAccountActions a[href='/buy#referral-program']").count() == 1
    for width in [320, 360, 390, 768, 1024, 1440]:
        await page.set_viewport_size({"width": width, "height": 900})
        assert await page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth"), ("store", width)
    await page.locator("#registerTopbarBtn").click()
    await page.locator("#authContactInput").fill("browser@example.com")
    await page.locator("#authPasswordInput").fill("safe-password")
    await page.locator("#authPassword2Input").fill("safe-password")
    await expect(page.locator("#authPasswordInput")).to_have_value("safe-password")
    await expect(page.locator("#authPassword2Input")).to_have_value("safe-password")
    async with page.expect_response("**/api/store/auth/otp/start") as registration:
        await page.locator("#authSubmitBtn").click()
    registration_response = await registration.value
    assert registration_response.status == 200, await registration_response.text()
    await page.wait_for_url("**/email-confirm?flow=otp")
    await page.locator("#code").fill(__import__("re").search(r"Ваш код: (\d{6})", sender.await_args.args[2]).group(1))
    await page.locator("#verifyButton").click()
    await page.wait_for_url("**/profile")
    await page.goto(url + "/profile")
    await expect(page.locator("#ordersList")).to_contain_text("У вас пока нет подписок")
    await expect(page.locator("#lookupSection")).to_be_hidden()
    await expect(page.locator("#referralBox")).to_be_visible()
    referral = await page.locator("#referralLinkText").inner_text()
    await page.locator("#referralCopyBtn").click()
    assert await page.evaluate("navigator.clipboard.readText()") == referral
    await expect(page.locator("#toast")).to_have_text("Ссылка скопирована")
    token = (await page.evaluate("fetch('/api/store/auth/me').then(r => r.json())"))["profile_token"]
    assert not await page.locator("#balanceToggleBtn").is_visible()
    artifact_dir = Path(os.environ.get("VPNBOT_UI_ARTIFACT_DIR", tmp_path))
    artifact_dir.mkdir(parents=True, exist_ok=True)
    for state in ["empty", "demo", "demo-funded", "paid", "expired", "daily"]:
        if state == "demo":
            await page.get_by_role("link", name="Попробовать бесплатно", exact=True).click()
            await expect(page.locator("#demoSuccessContent")).to_be_visible()
            await page.goto(url + "/profile")
        elif state == "demo-funded":
            async with factory() as session:
                account = await session.get(WebBalanceAccount, token)
                account.balance_rub = 100
                await session.commit()
            await page.reload()
        elif state in ("paid", "expired"):
            async with factory() as session:
                if state == "paid":
                    session.add(WebOrder(order_id="paid-ui", profile_token=token, contact="browser@example.com", tariff_key="vpn_30", tariff_label="Оплаченный тариф", days=30, amount_rub=300, status="delivered", subscription_url="https://example.test/" + "x" * 180, access_expires_at=datetime.utcnow() + timedelta(days=30)))
                else:
                    order = await session.scalar(select(WebOrder).where(WebOrder.order_id == "paid-ui"))
                    order.access_expires_at = datetime.utcnow() - timedelta(days=1)
                await session.commit()
            await page.reload()
        elif state == "daily":
            async with factory() as session:
                account = await session.get(WebBalanceAccount, token)
                account.balance_mode_enabled = account.balance_autodebit_enabled = 1
                account.balance_rub = 100
                await session.commit()
            await page.reload()
            await expect(page.locator("#balanceToggleBtn")).to_have_count(0)
            await expect(page.locator("#balanceMeta")).to_contain_text("Ежедневные списания включены")
        await expect(page.locator("#referralBox")).to_be_visible()
        assert await page.locator("#referralLinkText").inner_text() == referral
        for width in [320, 360, 390, 768, 1024, 1440]:
            await page.set_viewport_size({"width": width, "height": 900})
            assert await page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth"), (state, width)
            await page.screenshot(path=str(artifact_dir / f"profile-{state}-{width}.png"), full_page=True, animations="disabled")
        await expect(page.locator("#balanceToggleBtn")).to_have_count(0)
        if state == "daily":
            async with factory() as session:
                account = await session.get(WebBalanceAccount, token)
                assert account.balance_mode_enabled and account.balance_autodebit_enabled
    for failure in [500, 404, "network"]:
        async def fail_request(route):
            if failure == "network":
                await route.abort()
            else:
                await route.fulfill(status=failure, json={"error": "temporary"})
        await page.route("**/api/store/profile-orders?*", fail_request)
        await page.reload()
        await expect(page.locator("#profileLoadError")).to_be_visible()
        assert (await page.evaluate("fetch('/api/store/auth/me').then(r => r.json())"))["authenticated"]
        await page.unroute("**/api/store/profile-orders?*")
        await page.locator("#profileLoadError").get_by_role("button", name="Повторить").click()
        await expect(page.locator("#referralBox")).to_be_visible()
        await expect(page.locator("#profileLoadError")).to_be_hidden()
    await page.get_by_role("button", name="Войти в другой профиль").click()
    await expect(page.locator("#lookupSection")).to_be_visible()
    for width in [320, 360, 390, 768, 1024, 1440]:
        await page.set_viewport_size({"width": width, "height": 900})
        assert await page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth"), ("login", width)
        await page.screenshot(path=str(artifact_dir / f"profile-login-{width}.png"), full_page=True, animations="disabled")
    await page.locator("#lookupContact").fill("browser@example.com")
    await page.locator("#lookupPassword").fill("wrong-password")
    await page.locator("#lookupBtn").click()
    await expect(page.locator("#lookupError")).to_have_text("Неверный контакт или пароль")
    await expect(page.locator("#ordersSection")).to_be_hidden()
    await page.locator("#lookupPassword").fill("safe-password")
    await page.locator("#lookupBtn").click()
    await expect(page.locator("#referralBox")).to_be_visible()
    assert not errors
    await context.close()


async def test_subscription_quick_actions_respect_anonymous_and_linked_context(account_server, browser):
    from playwright.async_api import expect

    url, _ = account_server
    injected = inject_adapt_devices_manager(
        b'''<!doctype html><html><head><style></style></head><body>
        <div class="main"><div class="card"><div class="card-label">\xd0\xa3\xd1\x81\xd1\x82\xd1\x80\xd0\xbe\xd0\xb9\xd1\x81\xd1\x82\xd0\xb2\xd0\xb0</div></div></div>
        <div class="footer"></div></body></html>''',
        "12345678-1234-1234-1234-123456789abc",
        [],
    ).decode()

    async def serve_subscription(route):
        await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=injected)

    context = await browser.new_context(permissions=["clipboard-read", "clipboard-write"])
    page = await context.new_page()
    await page.route("**/subscription", serve_subscription)
    await page.goto(url + "/subscription")
    await page.route(
        "**/api/store/auth/me",
        lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps({"authenticated": False})),
    )
    await page.reload()
    await expect(page.locator("#quick-actions-card")).to_be_visible()
    await expect(page.locator("#quickReferralAnonymous")).to_be_visible()
    await expect(page.locator("#quickReferralAuthenticated")).to_be_hidden()
    assert await page.locator("#quickReferralLink").inner_text() == ""

    await page.unroute("**/api/store/auth/me")
    await page.route(
        "**/api/store/auth/me",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"authenticated": True, "profile_token": "session-token"}),
        ),
    )
    await page.route(
        "**/api/store/profile-orders",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "telegram_link": None,
                "referral": {"site_url": "https://example.test/buy?ref=abc"},
            }),
        ),
    )
    await page.reload()
    await expect(page.locator("#quickTelegramNote")).to_be_visible()
    await expect(page.locator("#quickTelegramStatus")).to_be_hidden()
    await expect(page.locator("#quickReferralAuthenticated")).to_be_visible()
    await expect(page.locator("#quickReferralAnonymous")).to_be_hidden()
    await expect(page.locator("#quickReferralLink")).to_have_text("https://example.test/buy?ref=abc")
    assert await page.evaluate("Boolean(document.querySelector('#quick-actions-card').compareDocumentPosition(document.querySelector('.card-label')) & Node.DOCUMENT_POSITION_FOLLOWING)")
    for width in [320, 360, 390, 768, 1024, 1440]:
        await page.set_viewport_size({"width": width, "height": 900})
        assert await page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth"), width
        await expect(page.locator("#quick-actions-card")).to_be_visible()
    await page.locator("#quickReferralCopy").click()
    await expect(page.locator("#quickReferralCopyStatus")).to_have_text("Ссылка скопирована")

    await page.unroute("**/api/store/profile-orders")
    await page.route(
        "**/api/store/profile-orders",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "telegram_link": {"telegram_username": "linked"},
                "referral": {"site_url": "https://example.test/buy?ref=abc"},
            }),
        ),
    )
    await page.reload()
    await expect(page.locator("#quickTelegramStatus")).to_be_visible()
    await expect(page.locator("#quickTelegramAction")).to_have_text("Telegram подключён")
    await expect(page.locator("#quickTelegramNote")).to_be_hidden()
    await context.close()


async def test_referral_credit_and_topup_retries_are_idempotent(patched_webstore, monkeypatch):
    import asyncio
    from webstore.models import WebBalanceTopUp

    monkeypatch.setattr(routes, "_fetch_public_balance_config", AsyncMock(return_value={"referral_commission_percent": 10}))
    token, cookies = await register_account()
    async with patched_webstore() as session:
        account = await session.get(WebBalanceAccount, token)
        code = account.ref_code
        session.add(WebOrder(order_id="friend-paid", profile_token="friend", contact="friend@example.com", tariff_key="vpn_30", tariff_label="Платный", days=30, amount_rub=300, original_amount_rub=300, status="delivered", ref_source=code))
        session.add(WebBalanceTopUp(topup_id="topup-retry", profile_token=token, telegram_id=0, amount_rub=100, status="paid"))
        await session.commit()
    referrals = await asyncio.gather(*(routes._credit_local_web_referral("friend-paid") for _ in range(2)))
    topups = await asyncio.gather(*(routes._credit_local_balance_topup("topup-retry") for _ in range(2)))
    assert {item["status"] for item in referrals} == {"credited", "already_credited"}
    assert {item["status"] for item in topups} == {"credited", "already_credited"}
    async with patched_webstore() as session:
        account = await session.get(WebBalanceAccount, token)
        assert account.ref_code == code and account.balance_rub == 130
        assert len((await session.execute(select(WebBalanceTransaction))).scalars().all()) == 2
        assert not account.balance_mode_enabled and not account.balance_autodebit_enabled


async def test_zero_order_browser_telegram_link(account_server, browser, monkeypatch):
    from urllib.parse import parse_qs, urlparse
    from playwright.async_api import expect
    from webstore.models import WebProfileLink

    url, factory = account_server
    sender = AsyncMock()
    monkeypatch.setattr(routes, "send_email", sender)
    monkeypatch.setattr(routes, "_fetch_balance_profile", AsyncMock(return_value={"balance_rub": 0, "balance_mode_enabled": False, "balance_autodebit_enabled": False}))
    monkeypatch.setattr(routes, "_fetch_balance_history", AsyncMock(return_value=[]))
    context = await browser.new_context()
    page = await context.new_page()
    await page.goto(url + "/buy")
    await page.evaluate("""async () => (await fetch('/api/store/auth/register', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({contact:'link-browser@example.com',password:'safe-password',password2:'safe-password'})})).json()""")
    result = await confirm_and_login(page, sender, "link-browser@example.com")
    await page.goto(url + "/profile")
    await expect(page.locator("#referralBox")).to_be_visible()
    referral = await page.locator("#referralLinkText").inner_text()
    await page.route("https://t.me/**", lambda route: route.fulfill(body="Telegram transport boundary"))
    await page.locator("#telegramLinkBtn").click()
    await page.wait_for_url("https://t.me/**")
    code = parse_qs(urlparse(page.url).query)["start"][0]
    assert code.startswith("web_")
    response = await routes.handle_internal_telegram_link_claim(_Request(headers={"X-Internal-Secret": "test-bridge-secret"}, _json_body={"code": code[4:], "telegram_id": 321, "telegram_username": "linked_user"}))
    assert response.status == 200
    await page.goto(url + "/profile")
    await expect(page.locator("#telegramStatus")).to_have_text("Привязано: @linked_user")
    await expect(page.locator("#referralBox")).to_be_visible()
    assert await page.locator("#referralLinkText").inner_text() == referral
    await expect(page.locator("#balanceToggleBtn")).to_be_hidden()
    async with factory() as session:
        assert (await session.get(WebProfileLink, result["token"])).telegram_id == 321
        assert (await session.execute(select(WebOrder))).scalars().all() == []
        assert not (await session.get(WebBalanceAccount, result["token"])).balance_autodebit_enabled
    await context.close()


async def test_referral_browser_checkout_preserves_account_code(account_server, browser, monkeypatch):
    import sys
    from playwright.async_api import expect
    from test_webstore_flows import _FakeYooKassa

    url, factory = account_server
    sender = AsyncMock()
    monkeypatch.setattr(routes, "send_email", sender)
    monkeypatch.setattr(otp_auth, "send_email", sender)
    token, cookies = await register_account()
    async with factory() as session:
        inviter_code = (await session.get(WebBalanceAccount, token)).ref_code
    monkeypatch.setitem(sys.modules, "yookassa", _FakeYooKassa())
    monkeypatch.setattr(routes.settings, "yookassa_shop_id", "test-shop")
    monkeypatch.setattr(routes.settings, "yookassa_secret_key", "test-secret")
    context = await browser.new_context()
    page = await context.new_page()
    await page.goto(url + "/buy?ref=" + inviter_code)
    await page.locator("#registerTopbarBtn").click()
    await page.locator("#authContactInput").fill("buyer-browser@example.com")
    await page.locator("#authPasswordInput").fill("safe-password")
    await page.locator("#authPassword2Input").fill("safe-password")
    await expect(page.locator("#authPasswordInput")).to_have_value("safe-password")
    await expect(page.locator("#authPassword2Input")).to_have_value("safe-password")
    async with page.expect_response("**/api/store/auth/otp/start") as registration:
        await page.locator("#authSubmitBtn").click()
    registration_response = await registration.value
    assert registration_response.status == 200, await registration_response.text()
    await page.wait_for_url("**/email-confirm?flow=otp")
    await page.locator("#code").fill(__import__("re").search(r"Ваш код: (\d{6})", sender.await_args.args[2]).group(1))
    await page.locator("#verifyButton").click()
    await page.wait_for_url("**/buy?ref=" + inviter_code)
    buyer = await page.evaluate("fetch('/api/store/auth/me').then(r => r.json())")
    async with factory() as session:
        original_code = (await session.get(WebBalanceAccount, buyer["profile_token"])).ref_code
    await page.route("https://pay.test/**", lambda route: route.fulfill(body="Payment provider boundary"))
    async with page.expect_response("**/api/store/create-order") as purchase:
        await page.locator(".buy-btn[data-key]").first.click()
    response = await purchase.value
    assert response.status == 200, await response.text()
    assert response.request.post_data_json["ref"] == inviter_code
    await page.wait_for_url("https://pay.test/**")
    async with factory() as session:
        order = await session.scalar(select(WebOrder).where(WebOrder.profile_token == buyer["profile_token"]))
        assert order.ref_source == inviter_code and order.profile_token == buyer["profile_token"]
        assert order.status == "pending"
        account = await session.get(WebBalanceAccount, buyer["profile_token"])
        assert account.ref_code == original_code
        assert not account.balance_mode_enabled and not account.balance_autodebit_enabled
    await page.goto(url + "/profile")
    await expect(page.locator("#referralBox")).to_be_visible()
    await page.locator("#topupAmount").fill("100")
    async with page.expect_response("**/api/store/create-balance-topup") as payment:
        await page.locator("#topupBtn").click()
    assert (await payment.value).status == 200
    await page.wait_for_url("https://pay.test/**")
    from webstore.models import WebBalanceTopUp
    async with factory() as session:
        topup = await session.scalar(select(WebBalanceTopUp).where(WebBalanceTopUp.profile_token == buyer["profile_token"]))
        topup_id = topup.topup_id
    await page.goto(url + "/profile?topup_id=" + topup_id)
    await expect(page.locator("#topupStatus")).to_contain_text("Пополнение обрабатывается")
    callback = _Request(_json_body={"event": "payment.succeeded", "object": {"id": "yk_web_1", "metadata": {"source": "webstore_balance_topup", "topup_id": topup_id}}})
    assert (await routes.handle_yookassa_webhook(callback)).status == 200
    assert (await routes.handle_yookassa_webhook(callback)).status == 200
    await expect(page.locator("#balanceAmount")).to_have_text("100.00 ₽", timeout=10000)
    await expect(page.locator("#topupStatus")).to_have_text("Баланс пополнен")
    await expect(page.locator("#balanceToggleBtn")).to_be_hidden()
    async with factory() as session:
        account = await session.get(WebBalanceAccount, buyer["profile_token"])
        assert account.ref_code == original_code and account.balance_rub == 100
        assert len((await session.execute(select(WebBalanceTransaction))).scalars().all()) == 1
    await context.close()


async def test_linked_account_can_disable_existing_local_daily_mode(patched_webstore, monkeypatch):
    from webstore.models import WebProfileLink

    monkeypatch.setattr(routes, "_get_web_daily_charge_rub", AsyncMock(return_value=3.17))
    monkeypatch.setattr(routes, "_fetch_balance_profile", AsyncMock(return_value={"balance_rub": 100, "balance_mode_enabled": False, "balance_autodebit_enabled": False}))
    monkeypatch.setattr(routes, "_fetch_balance_history", AsyncMock(return_value=[]))
    toggle = AsyncMock(return_value=({"balance_rub": 100, "balance_mode_enabled": False, "balance_autodebit_enabled": False}, 200))
    monkeypatch.setattr(routes, "_toggle_balance_mode", toggle)
    token, cookies = await register_account()
    async with patched_webstore() as session:
        account = await session.get(WebBalanceAccount, token)
        account.balance_mode_enabled = account.balance_autodebit_enabled = 1
        account.balance_rub = 50
        session.add(WebProfileLink(profile_token=token, telegram_id=123))
        await session.commit()
    response = await routes.handle_profile_orders(_Request(query={"token": token}, cookies=cookies))
    balance = json.loads(response.text)["balance"]
    assert not balance["can_manage_daily_billing"] and balance["balance_autodebit_enabled"]
    assert balance["status_text"] == "Ежедневные списания на сайте включены"
    response = await routes.handle_balance_toggle(_Request(_json_body={"token": token, "enabled": False}, cookies=cookies))
    assert response.status == 200
    toggle.assert_awaited_once_with(123, False)
    async with patched_webstore() as session:
        account = await session.get(WebBalanceAccount, token)
        assert not account.balance_mode_enabled and not account.balance_autodebit_enabled
        assert account.balance_rub == 50
    refreshed = json.loads((await routes.handle_profile_orders(_Request(query={"token": token}, cookies=cookies))).text)
    assert not refreshed["balance"]["can_manage_daily_billing"]
