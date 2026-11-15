from unittest.mock import AsyncMock

import pytest
from test_webstore_account_ux import account_server, browser
from test_webstore_telegram_linking import db_engine, db_session_factory, patched_webstore
from webstore import otp_auth, routes
from webstore.models import WebAccount

pytestmark = pytest.mark.asyncio


async def test_existing_registration_explains_recovery_in_browser(account_server, browser, monkeypatch):
    from playwright.async_api import expect

    url, factory = account_server
    sender = AsyncMock()
    monkeypatch.setattr(otp_auth, "send_email", sender)
    async with factory() as session:
        session.add(WebAccount(contact="already@example.com", profile_token="already", password_hash=routes._hash_password("old-password")))
        await session.commit()
    context = await browser.new_context()
    page = await context.new_page()
    await page.goto(url + "/buy")
    await page.locator("#registerTopbarBtn").click()
    await page.locator("#authContactInput").fill("already@example.com")
    await page.locator("#authPasswordInput").fill("new-password")
    await page.locator("#authPassword2Input").fill("new-password")
    await page.locator("#authSubmitBtn").click()
    await page.wait_for_url("**/email-confirm?flow=otp")
    code = __import__("re").search(r"Ваш код: (\d{6})", sender.await_args.args[2]).group(1)
    await page.locator("#code").fill(code)
    async with page.expect_response("**/api/store/auth/otp/verify") as verified:
        await page.locator("#verifyButton").click()
    assert (await verified.value).status == 409
    await expect(page.locator("#error")).to_contain_text("уже зарегистрирован")
    await expect(page.locator("#changeEmail")).to_have_attribute("href", "/profile#forgot")
    await expect(page.locator("#changeEmail")).to_have_text("Восстановить пароль")
    await context.close()


async def test_recovery_rate_limit_shows_countdown(account_server, browser):
    from playwright.async_api import expect

    url, _ = account_server
    context = await browser.new_context()
    page = await context.new_page()
    await page.goto(url + "/profile#forgot")
    await page.locator("#forgotEmail").fill("rate-limit@example.com")
    await page.route("**/api/store/auth/otp/start", lambda route: route.fulfill(
        status=429, json={"error": "Подождите перед повторной отправкой", "retry_after": 2}
    ))
    await page.locator("#forgotSubmit").click()
    await expect(page.locator("#forgotError")).to_contain_text("Подождите перед повторной отправкой: 0:")
    await expect(page.locator("#forgotSubmit")).to_contain_text("Повторить через 0:")
    await expect(page.locator("#forgotSubmit")).to_be_enabled(timeout=4000)
    await context.close()


async def test_email_journey_in_browser(account_server, browser, monkeypatch):
    from playwright.async_api import expect

    url, _ = account_server
    sender = AsyncMock()
    monkeypatch.setattr(routes, "send_email", sender)
    monkeypatch.setattr(otp_auth, "send_email", sender)
    context = await browser.new_context()
    page = await context.new_page()
    async def check_layout(name):
        for width in [320, 360, 390, 768, 1024, 1440]:
            await page.set_viewport_size({"width": width, "height": 900})
            assert await page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth"), (name, width)

    await page.goto(url + "/buy")
    await page.locator("#registerTopbarBtn").click()
    await check_layout("registration")
    await page.locator("#authContactInput").fill("browser@example.com")
    await page.locator("#authPasswordInput").fill("password-one")
    await page.locator("#authPassword2Input").fill("password-one")
    await page.locator("#authSubmitBtn").click()
    await page.wait_for_url("**/email-confirm?flow=otp")
    await check_layout("confirm")
    await page.reload()
    await expect(page.locator("#email")).to_have_text("browser@example.com")
    code = __import__("re").search(r"Ваш код: (\d{6})", sender.await_args.args[2]).group(1)
    await expect(page.locator("#resendButton")).to_be_disabled()
    await page.locator("#code").fill("000000" if code != "000000" else "000001")
    async with page.expect_response("**/api/store/auth/otp/verify") as incorrect:
        await page.locator("#verifyButton").click()
    assert (await incorrect.value).status == 400
    await expect(page.locator("#error")).to_contain_text("Неверный")
    await page.locator("#code").fill(code)
    await page.locator("#verifyButton").click()
    await page.wait_for_url("**/profile")
    await expect(page.locator("#ordersSection")).to_be_visible()
    await page.goto(url + "/buy")
    await page.goto(url + "/profile#forgot")
    await expect(page.locator("#forgotForm")).to_be_visible()
    await page.goto(url + "/profile")
    await expect(page.locator("#ordersSection")).to_be_visible()
    await page.get_by_role("button", name="Добавить или изменить email").click()
    await check_layout("profile-email")
    await page.locator("#newEmail").fill("new-browser@example.com")
    await page.locator("#emailPassword").fill("password-one")
    await page.locator("#emailForm button").click()
    await expect(page.locator("#emailFormMessage")).to_contain_text("Проверьте почту")
    token = sender.await_args.args[2].split("#")[-1]
    await page.goto(url + "/email-confirm#" + token)
    await page.locator("#submit").click()
    await expect(page.locator("#message")).to_contain_text("Готово")
    await page.goto(url + "/profile")
    await page.get_by_role("button", name="Войти в другой профиль").click()
    await page.get_by_role("button", name="Забыли пароль?").click()
    await check_layout("forgot")
    await page.locator("#forgotEmail").fill("new-browser@example.com")
    await page.locator("#forgotForm button").click()
    await page.wait_for_url("**/password-reset?flow=otp")
    await check_layout("reset")
    code = __import__("re").search(r"Ваш код: (\d{6})", sender.await_args.args[2]).group(1)
    await page.locator("#code").fill(code)
    await page.locator("#verifyButton").click()
    await page.locator("#password").fill("password-two")
    await page.locator("#password2").fill("password-two")
    await page.locator("#passwordButton").click()
    await page.wait_for_url("**/profile")
    await expect(page.locator("#ordersSection")).to_be_visible()
    await page.get_by_role("button", name="Войти в другой профиль").click()
    await page.locator("#lookupContact").fill("new-browser@example.com")
    await page.locator("#lookupPassword").fill("password-two")
    await page.locator("#lookupBtn").click()
    await expect(page.locator("#ordersSection")).to_be_visible()
    await page.reload()
    await expect(page.locator("#ordersSection")).to_be_visible()
    await context.close()


async def test_phone_account_confirmed_email_and_stale_profile_link(account_server, browser, monkeypatch):
    import os
    import sys
    from playwright.async_api import expect
    from sqlalchemy import select
    from test_webstore_flows import _FakeYooKassa, _FakeYooKassaPaymentApi
    from webstore.models import WebAccount, WebBalanceAccount, WebOrder

    url, factory = account_server
    sender = AsyncMock()
    monkeypatch.setattr(routes, "send_email", sender)
    monkeypatch.setitem(sys.modules, "yookassa", _FakeYooKassa())
    monkeypatch.setattr(routes.settings, "yookassa_shop_id", "test-shop")
    monkeypatch.setattr(routes.settings, "yookassa_secret_key", "test-secret")
    monkeypatch.setattr(routes, "_fulfill_order", AsyncMock(side_effect=AssertionError("Unexpected fulfillment")))
    async with factory() as session:
        session.add(WebAccount(contact="+71234567890", profile_token="phone-profile", password_hash=routes._hash_password("password-one")))
        session.add(WebBalanceAccount(profile_token="phone-profile", contact="+71234567890", balance_rub=1, ref_code="phone-ref"))
        session.add(WebAccount(contact="foreign@example.com", profile_token="foreign-profile", password_hash=routes._hash_password("foreign-password")))
        await session.commit()
    context = await browser.new_context()
    page = await context.new_page()
    await page.goto(url + "/profile")
    await page.locator("#lookupContact").fill("+71234567890")
    await page.locator("#lookupPassword").fill("password-one")
    await page.locator("#lookupBtn").click()
    await expect(page.locator("#ordersSection")).to_be_visible()
    await page.get_by_role("button", name="Добавить или изменить email").click()
    await page.locator("#newEmail").fill("confirmed@example.com")
    await page.locator("#emailPassword").fill("password-one")
    await page.locator("#emailForm button").click()
    await expect(page.locator("#emailFormMessage")).to_contain_text("Проверьте почту")
    token = sender.await_args.args[2].split("#")[-1]
    await page.goto(url + "/email-confirm#" + token)
    await page.locator("#submit").click()
    await expect(page.locator("#message")).to_contain_text("Готово")
    await page.goto(url + "/profile?token=foreign-profile")
    await expect(page.locator("#emailBanner")).to_have_text("confirmed@example.com")
    await expect(page.locator("#referralLinkText")).to_contain_text("phone-ref")
    for width in [320, 360, 390, 768, 1024, 1440]:
        await page.set_viewport_size({"width": width, "height": 900})
        assert await page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        artifact_dir = os.getenv("VPNBOT_UI_ARTIFACT_DIR")
        if artifact_dir:
            await page.screenshot(path=f"{artifact_dir}/profile-{width}.png", full_page=True, animations="disabled")
    result = await page.evaluate("fetch('/api/store/profile-orders?token=foreign-profile').then(r => r.json())")
    assert result["referral"]["ref_code"] == "phone-ref"
    assert await page.evaluate("getActiveProfileToken()") == "phone-profile"
    await page.reload()
    await expect(page.locator("#emailBanner")).to_have_text("confirmed@example.com")
    await page.route("https://t.me/**", lambda route: route.fulfill(body="Telegram transport boundary"))
    async with page.expect_response("**/api/store/profile-link-init") as link_request:
        await page.locator("#telegramLinkBtn").click()
    link_response = await link_request.value
    assert link_response.status == 200
    assert link_response.request.post_data_json["token"] == "phone-profile"
    await page.goto(url + "/buy")
    await page.route("https://pay.test/**", lambda route: route.fulfill(body="Payment provider boundary"))
    async with page.expect_response("**/api/store/create-order") as purchase:
        await page.locator(".buy-btn[data-key]").first.click()
    response = await purchase.value
    assert response.status == 200, await response.text()
    await page.wait_for_url("https://pay.test/**")
    assert _FakeYooKassaPaymentApi.last_params["receipt"]["customer"]["email"] == "confirmed@example.com"
    async with factory() as session:
        order = await session.scalar(select(WebOrder).where(WebOrder.profile_token == "phone-profile"))
        account = await session.scalar(select(WebAccount).where(WebAccount.profile_token == "phone-profile"))
        balance = await session.get(WebBalanceAccount, "phone-profile")
        assert (order.contact, order.email) == ("confirmed@example.com", "confirmed@example.com")
        assert account.contact == "+71234567890"
        assert (balance.balance_rub, balance.ref_code) == (1, "phone-ref")
    await context.close()


async def test_legacy_email_reset_in_browser(account_server, browser, monkeypatch):
    from playwright.async_api import expect
    from webstore.models import WebAccount

    url, factory = account_server
    sender = AsyncMock()
    monkeypatch.setattr(otp_auth, "send_email", sender)
    async with factory() as session:
        session.add(WebAccount(contact="legacy@example.com", profile_token="legacy-email", password_hash=routes._hash_password("old-password")))
        await session.commit()
    context = await browser.new_context()
    page = await context.new_page()
    await page.goto(url + "/profile")
    await page.get_by_role("button", name="Забыли пароль?").click()
    await page.locator("#forgotEmail").fill("legacy@example.com")
    await page.locator("#forgotForm button").click()
    await page.wait_for_url("**/password-reset?flow=otp")
    code = __import__("re").search(r"Ваш код: (\d{6})", sender.await_args.args[2]).group(1)
    await page.locator("#code").fill(code)
    await page.locator("#verifyButton").click()
    await expect(page.locator("#passwordStep")).to_be_visible()
    await page.reload()
    await page.locator("#password").fill("new-password")
    await page.locator("#password2").fill("new-password")
    await page.locator("#passwordButton").click()
    await page.wait_for_url("**/profile")
    await page.get_by_role("button", name="Войти в другой профиль").click()
    await page.locator("#lookupContact").fill("legacy@example.com")
    await page.locator("#lookupPassword").fill("new-password")
    await page.locator("#lookupBtn").click()
    await expect(page.locator("#ordersSection")).to_be_visible()
    await expect(page.locator("#emailBanner")).to_have_text("legacy@example.com")
    await page.reload()
    await expect(page.locator("#ordersSection")).to_be_visible()
    await context.close()


async def test_auth_modal_modes_and_recovery_navigation(account_server, browser, monkeypatch):
    import os
    from playwright.async_api import expect

    url, _ = account_server
    monkeypatch.setattr(otp_auth, 'send_email', AsyncMock())
    context = await browser.new_context(permissions=['clipboard-read', 'clipboard-write'])
    page = await context.new_page()
    await page.goto(url + '/buy')
    artifact_dir = os.getenv('VPNBOT_UI_ARTIFACT_DIR')
    for width in [320, 360, 390, 768, 1024, 1440]:
        await page.set_viewport_size({'width': width, 'height': 900})
        assert await page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth')
        if artifact_dir:
            await page.locator('.step-card').first.screenshot(path=f'{artifact_dir}/store-step-{width}.png', animations='disabled')
    await page.locator('#registerTopbarBtn').click()
    for width in [320, 360, 390, 768, 1024, 1440]:
        await page.set_viewport_size({'width': width, 'height': 900})
        await expect(page.locator('#authTitle')).to_have_text('Регистрация')
        await expect(page.locator('#authContactInput')).to_have_attribute('placeholder', 'Email')
        await expect(page.locator('#authPassword2Input')).to_be_visible()
        await expect(page.locator('#authForgotLink')).to_be_hidden()
        await expect(page.locator('#authNote')).to_contain_text('код для подтверждения email')
        await expect(page.locator('#authNote')).to_contain_text('чек придёт на email')
        assert await page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth')
        boxes = [await page.locator(selector).bounding_box() for selector in ('#authContactInput', '#authPasswordInput', '#authPassword2Input', '#authSubmitBtn')]
        assert all(box and box['x'] >= 0 and box['x'] + box['width'] <= width for box in boxes)
        assert all(first['y'] + first['height'] <= second['y'] for first, second in zip(boxes, boxes[1:]))
        if artifact_dir:
            await page.screenshot(path=f'{artifact_dir}/register-{width}.png', animations='disabled')
        await page.locator('#authSwitchLink').click()
        await expect(page.locator('#authTitle')).to_have_text('Войти в личный кабинет')
        await expect(page.locator('#authForgotLink')).to_be_visible()
        await expect(page.locator('#authPassword2Input')).to_be_hidden()
        await expect(page.locator('#authNote')).not_to_contain_text('код для подтверждения')
        assert await page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth')
        if artifact_dir:
            await page.screenshot(path=f'{artifact_dir}/login-{width}.png', animations='disabled')
        await page.locator('#authSwitchLink').click()
    await page.set_viewport_size({'width': 320, 'height': 568})
    box = await page.locator('#authOverlay .demo-modal').bounding_box()
    assert box and box['y'] >= 0 and box['y'] + box['height'] <= 568
    if artifact_dir:
        await page.screenshot(path=f'{artifact_dir}/register-320x568.png', animations='disabled')
    await page.locator('#authSwitchLink').click()
    box = await page.locator('#authOverlay .demo-modal').bounding_box()
    assert box and box['y'] >= 0 and box['y'] + box['height'] <= 568
    if artifact_dir:
        await page.screenshot(path=f'{artifact_dir}/login-320x568.png', animations='disabled')
    await page.locator('#authContactInput').fill('+71234567890')
    await page.locator('#authSwitchLink').click()
    await expect(page.locator('#authContactInput')).to_have_value('')
    await page.locator('#authSwitchLink').click()
    await page.locator('#authForgotLink').click()
    await page.wait_for_url('**/profile#forgot')
    await expect(page.locator('#forgotForm')).to_be_visible()
    await expect(page.locator('#forgotForm h3')).to_have_text('Восстановление пароля')
    await expect(page.locator('#forgotToggle')).to_be_hidden()
    await expect(page.locator('#forgotEmail')).to_be_focused()
    for width in [320, 360, 390, 768, 1024, 1440]:
        await page.set_viewport_size({'width': width, 'height': 900})
        assert await page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth')
        if artifact_dir:
            await page.screenshot(path=f'{artifact_dir}/recovery-{width}.png', full_page=True, animations='disabled')
    await page.locator('#forgotForm button').click()
    await expect(page.locator('#forgotError')).to_have_text('Введите email')
    await page.locator('#forgotEmail').fill('invalid')
    await page.locator('#forgotForm button').click()
    await expect(page.locator('#forgotError')).to_have_text('Введите email')
    await page.locator('#forgotEmail').fill('unknown@example.com')
    await page.route('**/api/store/auth/otp/start', lambda route: route.fulfill(status=500, json={'error': 'unavailable'}))
    await page.locator('#forgotForm button').click()
    await expect(page.locator('#forgotError')).to_contain_text('unavailable')
    await page.unroute('**/api/store/auth/otp/start')
    async with page.expect_response('**/api/store/auth/otp/start') as recovery:
        await page.locator('#forgotForm button').click()
    assert (await recovery.value).status == 200
    await page.wait_for_url('**/password-reset?flow=otp')
    await expect(page.locator('#codeForm')).to_be_visible()
    subscription = 'https://example.test/subscription/' + 'x' * 180
    referral = 'https://example.test/buy?ref=' + 'y' * 100
    await page.route('**/api/store/profile-orders?*', lambda route: route.fulfill(status=200, json={
        'referral': {'site_url': referral},
    }))
    await page.route('**/api/store/order-status?*', lambda route: route.fulfill(status=200, json={
        'status': 'delivered', 'subscription_url': subscription, 'profile_token': 'test-profile',
        'tariff_label': 'Тариф', 'days': 30,
    }))
    await page.goto(url + '/pay/success?order_id=test-order')
    await expect(page.locator('#stateSuccess')).to_be_visible()
    await expect(page.locator('#inviteBox')).to_be_visible()
    await expect(page.locator('#stateSuccess')).to_contain_text('Ключ также доступен в личном кабинете, а чек придёт на email')
    for width in [320, 360, 390, 768, 1024, 1440]:
        await page.set_viewport_size({'width': width, 'height': 900})
        assert await page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth')
        for text_selector, button_selector in [('#keyUrlText', '#keyUrl .copy-btn'), ('#inviteLinkText', '#inviteCopyBtn')]:
            text_box = await page.locator(text_selector).bounding_box()
            button_box = await page.locator(button_selector).bounding_box()
            assert text_box and button_box
            assert text_box['x'] + text_box['width'] <= button_box['x'] or text_box['y'] + text_box['height'] <= button_box['y']
            assert button_box['x'] >= 0 and button_box['x'] + button_box['width'] <= width
        if artifact_dir:
            await page.screenshot(path=f'{artifact_dir}/success-{width}.png', full_page=True, animations='disabled')
    await page.locator('#keyUrl .copy-btn').click()
    await expect(page.locator('#copyStatus')).to_have_text('Ссылка скопирована!')
    assert await page.evaluate('navigator.clipboard.readText()') == subscription
    await page.locator('#inviteCopyBtn').click()
    await expect(page.locator('#inviteCopyBtn')).to_have_text('Скопировано')
    assert await page.evaluate('navigator.clipboard.readText()') == referral
    await context.close()
