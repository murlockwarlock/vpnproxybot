from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import expect

from test_webstore_account_ux import account_server, browser
from test_support_account_recovery import configure_fake_yookassa, telegram_login_body
from test_webstore_telegram_linking import db_engine, db_session_factory, patched_webstore
from webstore import otp_auth, routes
from webstore.models import WebAccount, WebBalanceAccount, WebOrder

pytestmark = pytest.mark.asyncio


async def test_old_phone_recovery_support_journey_in_browser(account_server, browser, monkeypatch):
    url, factory = account_server
    monkeypatch.setattr(routes.settings, "support_admin_password", "support-browser-password")
    monkeypatch.setattr(routes.settings, "admin_bot_token", "test-bot-token")
    monkeypatch.setattr(routes.settings, "support_agent_ids", [123456789])
    monkeypatch.setattr(routes.settings, "telegram_bot_name", "darimiru_bot")
    configure_fake_yookassa(monkeypatch, customer=None)
    monkeypatch.setattr(routes, "_notify_support_agents", AsyncMock())
    monkeypatch.setattr(routes, "_notify_support_agents_message", AsyncMock())
    sender = AsyncMock()
    monkeypatch.setattr(otp_auth, "send_email", sender)
    async with factory() as session:
        session.add(WebAccount(contact="+71234567890", profile_token="legacy-phone", password_hash=routes._hash_password("old-password")))
        session.add(WebBalanceAccount(profile_token="legacy-phone", contact="+71234567890", balance_rub=321, ref_code="legacy-ref"))
        session.add(WebOrder(
            order_id="order-legacy-1",
            profile_token="legacy-phone",
            contact="+71234567890",
            tariff_key="vpn_30",
            tariff_label="VPN 30",
            days=30,
            amount_rub=499,
            status="delivered",
            yookassa_payment_id="payment-legacy-1",
            paid_at=datetime.utcnow(),
            subscription_url="https://vpn.example/sub/legacy-1",
        ))
        await session.commit()

    context = await browser.new_context()
    page = await context.new_page()
    await page.goto(url + "/profile")
    await page.locator("#lookupContact").fill("+71234567890")
    await page.locator("#lookupPassword").fill("old-password")
    await page.locator("#lookupBtn").click()
    await expect(page.locator("#ordersSection")).to_be_visible()
    await expect(page.locator("#accountProtectionCard")).to_be_visible()
    await page.locator("#accountProtectionCard button").click()
    await expect(page.locator("#emailForm")).to_be_visible()

    await page.get_by_role("button", name="Войти в другой профиль").click()
    await page.get_by_role("button", name="Забыли пароль?").click()
    await page.get_by_role("link", name="Восстановить через поддержку").click()
    await page.wait_for_url("**/support?flow=recovery")
    for width in (320, 390):
        await page.set_viewport_size({"width": width, "height": 900})
        assert await page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    await page.locator("#recoveryContact").fill("+7 123 456 7890")
    await page.locator("#recoveryReference").fill("order-legacy-1")
    await page.locator("#recoveryEmail").fill("recovery@example.com")
    await page.locator("#recoveryDetails").fill("Дата и сумма платежа известны поддержке")
    await page.get_by_role("button", name="Создать обращение").click()
    await expect(page.locator("#chatScreen")).to_be_visible()
    ticket_token = await page.evaluate("localStorage.getItem('support_recovery_ticket_token')")
    assert ticket_token

    admin_page = await context.new_page()
    await admin_page.goto(url + "/support/admin")
    assert await admin_page.locator('[data-telegram-login="darimiru_bot"]').count() == 1
    await admin_page.locator("#adminPassword").fill("support-browser-password")
    await admin_page.get_by_role("button", name="Войти").click()
    await expect(admin_page.locator("#mainScreen")).to_be_visible()
    for width in (320, 390):
        await admin_page.set_viewport_size({"width": width, "height": 900})
        assert await admin_page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    await expect(admin_page.locator(".ticket-item")).to_contain_text("+7 123 456 7890")
    await admin_page.locator(".ticket-item").first.click()
    await expect(admin_page.locator("#recoveryPanel")).to_have_class(__import__("re").compile(r".*active.*"))
    await expect(admin_page.locator("#recoveryPanel .recovery-approve")).to_be_hidden()
    await expect(admin_page.locator("#recoveryMessage")).to_contain_text("Telegram")

    await context.clear_cookies()
    telegram_body = telegram_login_body()
    telegram_token = await admin_page.evaluate("""async body => {
        const response = await fetch('/api/support/admin/login', {
          method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
        });
        return await response.json();
    }""", telegram_body)
    assert telegram_token["ok"] is True and telegram_token["can_approve_recovery"] is True
    await admin_page.evaluate("token => localStorage.setItem('support_agent_token', token)", telegram_token["token"])
    await admin_page.reload()
    await expect(admin_page.locator("#mainScreen")).to_be_visible()
    await expect(admin_page.locator(".ticket-item").first).to_contain_text("+7 123 456 7890")
    await admin_page.locator(".ticket-item").first.click()
    await expect(admin_page.locator("#recoveryPanel")).to_have_class(__import__("re").compile(r".*active.*"))
    await admin_page.locator("#recoveryVerificationNote").fill("Платёж найден у провайдера, статус succeeded и сумма совпадает.")
    await admin_page.get_by_role("button", name="Подтвердить владельца").click()
    await expect(admin_page.locator("#recoveryMessage")).to_contain_text("контакт плательщика")
    await expect(admin_page.get_by_role("button", name="Одобрить после ручной проверки")).to_be_visible()
    await admin_page.locator("#recoveryManualBasis").select_option("account_history")
    await admin_page.locator("#recoveryVerificationNote").fill("История заказов и активная подписка сопоставлены с обращением клиента.")
    await admin_page.get_by_role("button", name="Одобрить после ручной проверки").click()
    await expect(admin_page.locator("#recoveryMessage")).to_contain_text("Владелец подтверждён")

    await expect(page.locator("#recoveryOtpButton")).to_be_visible(timeout=8000)
    await page.locator("#recoveryOtpButton").click()
    await page.wait_for_url("**/password-reset?flow=otp&support=1")
    code = __import__("re").search(r"Ваш код: (\d{6})", sender.await_args.args[2]).group(1)
    await page.locator("#code").fill(code)
    await page.locator("#verifyButton").click()
    await page.locator("#password").fill("new-password")
    await page.locator("#password2").fill("new-password")
    await page.locator("#passwordButton").click()
    await page.wait_for_url("**/profile")
    await expect(page.locator("#ordersSection")).to_be_visible()
    await expect(page.locator("#emailBanner")).to_have_text("recovery@example.com")
    await expect(page.locator("#accountProtectionCard")).to_be_hidden()
    await expect(page.locator("#referralLinkText")).to_contain_text("legacy-ref")
    await admin_page.close()
    await context.close()
