import hashlib
import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from test_webstore_telegram_linking import db_engine, db_session_factory, patched_webstore, _Request
from webstore import routes
from webstore.models import WebAccount, WebBalanceAccount, WebEmail, WebEmailToken, WebOrder, WebProfileLink

pytestmark = pytest.mark.asyncio


def body(response):
    return json.loads(response.text)


def request(data, path="/api/store/auth/confirm", cookies=None):
    return _Request(_json_body=data, path=path, cookies=cookies or {})


def last_token(mock):
    return mock.await_args.args[2].split("#")[-1]


async def test_registration_confirmation_and_reset(patched_webstore, monkeypatch):
    send = AsyncMock()
    monkeypatch.setattr(routes, "send_email", send)
    data = {"contact": "new@example.com", "password": "password-one", "password2": "password-one"}
    response = await routes.handle_auth_register(request(data))
    assert response.status == 200 and send.await_count == 1
    async with patched_webstore() as session:
        assert not await session.scalar(select(WebAccount))
        stored = await session.scalar(select(WebEmailToken))
        assert stored.token_hash == hashlib.sha256(last_token(send).encode()).hexdigest()
    assert (await routes.handle_auth_login(request(data))).status == 401
    raw = last_token(send)
    assert (await routes.handle_email_token(request({"token": raw}))).status == 200
    assert (await routes.handle_email_token(request({"token": raw}))).status == 400
    login = await routes.handle_auth_login(request(data))
    assert login.status == 200
    cookie = {routes._AUTH_COOKIE: login.cookies[routes._AUTH_COOKIE].value}
    second_login = await routes.handle_auth_login(request(data))
    second_cookie = {routes._AUTH_COOKIE: second_login.cookies[routes._AUTH_COOKIE].value}
    await routes.handle_forgot_password(request({"email": "new@example.com"}))
    reset = last_token(send)
    assert (await routes.handle_email_token(request({"token": reset, "password": "password-two"}, "/api/store/auth/reset"))).status == 200
    assert (await routes.handle_email_token(request({"token": reset, "password": "password-two"}, "/api/store/auth/reset"))).status == 400
    assert not body(await routes.handle_auth_me(request({}, cookies=cookie)))["authenticated"]
    assert not body(await routes.handle_auth_me(request({}, cookies=second_cookie)))["authenticated"]
    assert (await routes.handle_auth_login(request(data))).status == 401
    assert (await routes.handle_auth_login(request({**data, "password": "password-two"}))).status == 200
    assert body(await routes.handle_forgot_password(request({"email": "unknown@example.com"}))) == {"ok": True}


async def test_legacy_phone_email_keeps_related_data(patched_webstore, monkeypatch):
    send = AsyncMock()
    monkeypatch.setattr(routes, "send_email", send)
    async with patched_webstore() as session:
        session.add(WebAccount(contact="+71234567890", profile_token="legacy", password_hash=routes._hash_password("password-one")))
        session.add(WebBalanceAccount(profile_token="legacy", contact="+71234567890", balance_rub=150, ref_code="ref123"))
        session.add(WebOrder(order_id="old-order", profile_token="legacy", tariff_key="vpn", tariff_label="VPN", days=30, amount_rub=100, status="delivered"))
        session.add(WebProfileLink(profile_token="legacy", telegram_id=12345))
        await session.commit()
    assert body(await routes.handle_forgot_password(request({"email": "+71234567890"}))) == {"ok": True}
    assert send.await_count == 0
    login = await routes.handle_auth_login(request({"contact": "+71234567890", "password": "password-one"}))
    assert login.status == 200
    cookies = {routes._AUTH_COOKIE: login.cookies[routes._AUTH_COOKIE].value}
    assert (await routes.handle_email_change(request({"email": "owner@example.com", "password": "wrong"}, cookies=cookies))).status == 401
    assert (await routes.handle_email_change(request({"email": "owner@example.com", "password": "password-one"}, cookies=cookies))).status == 200
    assert (await routes.handle_email_token(request({"token": last_token(send)}))).status == 200
    async with patched_webstore() as session:
        assert (await session.get(WebEmail, "owner@example.com")).profile_token == "legacy"
        balance = await session.get(WebBalanceAccount, "legacy")
        assert (balance.balance_rub, balance.ref_code) == (150, "ref123")
        assert (await session.scalar(select(WebOrder))).order_id == "old-order"
        assert (await session.get(WebProfileLink, "legacy")).telegram_id == 12345
    assert body(await routes.handle_auth_me(request({}, cookies=cookies)))["contact"] == "owner@example.com"
    monkeypatch.setattr(routes, "_fetch_balance_profile", AsyncMock(return_value=None))
    monkeypatch.setattr(routes, "_fetch_balance_history", AsyncMock(return_value=[]))
    monkeypatch.setattr(routes, "_get_web_daily_charge_rub", AsyncMock(return_value=0))
    profile = await routes.handle_profile_orders(_Request(query={"token": "legacy"}, cookies=cookies))
    assert body(profile)["contact"] == "owner@example.com"
    assert body(profile)["orders"][0]["order_id"] == "old-order"
    assert (await routes.handle_auth_login(request({"contact": "owner@example.com", "password": "password-one"}))).status == 200
    assert (await routes.handle_auth_login(request({"contact": "+71234567890", "password": "password-one"}))).status == 401


async def test_invalid_and_expired_reset(patched_webstore):
    raw = "expired-token"
    async with patched_webstore() as session:
        session.add(WebEmailToken(token_hash=hashlib.sha256(raw.encode()).hexdigest(), purpose="reset", email="a@example.com", profile_token="abc", expires_at=datetime.utcnow() - timedelta(seconds=1)))
        await session.commit()
    for token in (raw, "unknown"):
        assert (await routes.handle_email_token(request({"token": token, "password": "password-two"}, "/api/store/auth/reset"))).status == 400


async def test_smtp_starttls_and_login(monkeypatch):
    from webstore import email_auth

    calls = []
    class Server:
        def __init__(self, host, port, timeout):
            calls.append(("connect", host, port))
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def ehlo(self):
            calls.append(("ehlo",))
        def starttls(self, context):
            assert context.check_hostname
            calls.append(("starttls",))
        def login(self, username, password):
            calls.append(("login", username, password))
        def send_message(self, message):
            calls.append(("send", message["To"], message.get_content()))
    for key, value in {"SMTP_HOST": "smtp.gmail.com", "SMTP_PORT": "587", "SMTP_USERNAME": "sender@example.com", "SMTP_APP_PASSWORD": "app-password", "SMTP_FROM": "sender@example.com"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(email_auth.smtplib, "SMTP", Server)
    await email_auth.send_email("recipient@example.com", "Subject", "message")
    assert [entry[0] for entry in calls] == ["connect", "ehlo", "starttls", "ehlo", "login", "send"]
    assert calls[-1][1:] == ("recipient@example.com", "message\n")


async def test_recent_telegram_session_can_add_email(patched_webstore, monkeypatch):
    send = AsyncMock()
    monkeypatch.setattr(routes, "send_email", send)
    async with patched_webstore() as session:
        session.add(WebAccount(contact="+71234567890", profile_token="legacy", password_hash=routes._hash_password("forgotten-password")))
        session.add(WebProfileLink(profile_token="legacy", telegram_id=12345))
        await session.commit()
    monkeypatch.setattr(routes, "_migrate_web_balance_to_telegram", AsyncMock())
    init = await routes.handle_telegram_auth_init(request({}))
    code = body(init)["code"]
    claim = _Request(headers={"X-Internal-Secret": "test-bridge-secret"}, _json_body={"code": code, "telegram_id": 12345})
    assert (await routes.handle_internal_telegram_auth_claim(claim)).status == 200
    login = await routes.handle_telegram_auth_status(_Request(query={"code": code}))
    assert body(login)["token"] == "legacy"
    assert (await routes.handle_telegram_auth_status(_Request(query={"code": code}))).status == 404
    cookies = {routes._AUTH_COOKIE: login.cookies[routes._AUTH_COOKIE].value}
    assert (await routes.handle_email_change(request({"email": "owner@example.com", "password": ""}, cookies=cookies))).status == 200
    assert (await routes.handle_email_token(request({"token": last_token(send)}))).status == 200
    await routes.handle_forgot_password(request({"email": "owner@example.com"}))
    assert (await routes.handle_email_token(request({"token": last_token(send), "password": "new-password"}, "/api/store/auth/reset"))).status == 200
    assert (await routes.handle_auth_login(request({"contact": "owner@example.com", "password": "new-password"}))).status == 200


async def test_email_uniqueness_and_mail_rate_limit(patched_webstore, monkeypatch):
    sender = AsyncMock()
    monkeypatch.setattr(routes, "send_email", sender)
    async with patched_webstore() as session:
        session.add_all([
            WebAccount(contact="phone-a", profile_token="a", password_hash=routes._hash_password("password-one")),
            WebAccount(contact="phone-b", profile_token="b", password_hash=routes._hash_password("password-one")),
            WebEmail(email="taken@example.com", profile_token="a"),
        ])
        await session.commit()
    login = await routes.handle_auth_login(request({"contact": "phone-b", "password": "password-one"}))
    cookies = {routes._AUTH_COOKIE: login.cookies[routes._AUTH_COOKIE].value}
    assert (await routes.handle_email_change(request({"email": "taken@example.com", "password": "password-one"}, cookies=cookies))).status == 409
    for _ in range(5):
        assert body(await routes.handle_forgot_password(request({"email": "taken@example.com"}))) == {"ok": True}
    assert sender.await_count == 3


async def test_profile_token_does_not_authorize_private_actions(patched_webstore):
    async with patched_webstore() as session:
        session.add(WebAccount(contact="owner@example.com", profile_token="secret-profile", password_hash=routes._hash_password("password-one")))
        session.add(WebOrder(order_id="order-1", profile_token="secret-profile", tariff_key="vpn", tariff_label="VPN", days=30, amount_rub=100, status="pending"))
        await session.commit()
    forged = {"webstore_profile_token": "secret-profile", routes._AUTH_COOKIE: "secret-profile"}
    assert not body(await routes.handle_auth_me(request({}, cookies=forged)))["authenticated"]
    assert (await routes.handle_profile_orders(_Request(query={"token": "secret-profile"}, cookies=forged))).status == 403
    assert (await routes.handle_profile_link_init(request({"token": "secret-profile"}, cookies=forged))).status == 403
    assert (await routes.handle_cancel_order(request({"token": "secret-profile", "order_id": "order-1"}, cookies=forged))).status == 403


async def test_forged_contact_cannot_spend_account_balance(patched_webstore):
    contact = "owner@example.com"
    profile = routes._make_profile_token(contact)
    async with patched_webstore() as session:
        session.add(WebAccount(contact=contact, profile_token=profile, password_hash=routes._hash_password("password-one")))
        session.add(WebBalanceAccount(profile_token=profile, contact=contact, balance_rub=1000, ref_code="ref-test"))
        await session.commit()
    response = await routes.handle_create_order(request({"tariff_key": "vpn_30", "contact": contact, "use_balance": True}))
    assert response.status == 401
    async with patched_webstore() as session:
        assert (await session.get(WebBalanceAccount, profile)).balance_rub == 1000
        assert not await session.scalar(select(WebOrder))


async def test_rate_limit_is_atomic(patched_webstore):
    import asyncio
    results = await asyncio.gather(*(routes._allow_auth_attempt("parallel", "same-client", 5, 60) for _ in range(15)))
    assert sum(results) == 5


async def test_telegram_verification_expires(patched_webstore, monkeypatch):
    from webstore.models import WebSession

    sender = AsyncMock()
    monkeypatch.setattr(routes, "send_email", sender)
    async with patched_webstore() as session:
        session.add(WebAccount(contact="old-phone", profile_token="old-profile", password_hash=routes._hash_password("password-one")))
        await session.commit()
    response = await routes._auth_response({"ok": True}, "old-profile", method="telegram")
    secret = response.cookies[routes._AUTH_COOKIE].value
    async with patched_webstore() as session:
        record = await session.get(WebSession, hashlib.sha256(secret.encode()).hexdigest())
        record.created_at = datetime.utcnow() - timedelta(minutes=11)
        await session.commit()
    response = await routes.handle_email_change(request({"email": "owner@example.com", "password": ""}, cookies={routes._AUTH_COOKIE: secret}))
    assert response.status == 401 and sender.await_count == 0


async def test_revoked_session_cannot_issue_email_change(patched_webstore, monkeypatch):
    from webstore.models import WebSession

    sender = AsyncMock()
    monkeypatch.setattr(routes, "send_email", sender)
    stored = routes._hash_password("password-one")
    async with patched_webstore() as session:
        session.add(WebAccount(contact="old-phone", profile_token="old-profile", password_hash=stored))
        await session.commit()
    login = await routes._auth_response({"ok": True}, "old-profile")
    cookie = login.cookies[routes._AUTH_COOKIE].value
    digest = hashlib.sha256(cookie.encode()).hexdigest()
    async with patched_webstore() as session:
        await session.delete(await session.get(WebSession, digest))
        await session.commit()
    await routes._send_auth_link("new@example.com", "change", profile_token="old-profile", session_hash=digest, expected_password_hash=stored)
    assert sender.await_count == 0
    async with patched_webstore() as session:
        assert not await session.scalar(select(WebEmailToken))


async def test_new_email_confirmation_invalidates_other_pending_changes(patched_webstore, monkeypatch):
    sender = AsyncMock()
    monkeypatch.setattr(routes, "send_email", sender)
    async with patched_webstore() as session:
        session.add(WebAccount(contact="old-phone", profile_token="profile", password_hash=routes._hash_password("password-one")))
        await session.commit()
    login = await routes.handle_auth_login(request({"contact": "old-phone", "password": "password-one"}))
    cookies = {routes._AUTH_COOKIE: login.cookies[routes._AUTH_COOKIE].value}
    await routes.handle_email_change(request({"email": "first@example.com", "password": "password-one"}, cookies=cookies))
    first = last_token(sender)
    await routes.handle_email_change(request({"email": "second@example.com", "password": "password-one"}, cookies=cookies))
    second = last_token(sender)
    assert (await routes.handle_email_token(request({"token": second}))).status == 200
    assert (await routes.handle_email_token(request({"token": first}))).status == 400


async def test_legacy_email_account_reset_and_old_address_after_change(patched_webstore, monkeypatch):
    send = AsyncMock()
    monkeypatch.setattr(routes, "send_email", send)
    profile = "legacy-email-profile"
    async with patched_webstore() as session:
        session.add(WebAccount(contact="legacy@example.com", profile_token=profile, password_hash=routes._hash_password("old-password")))
        session.add(WebBalanceAccount(profile_token=profile, contact="legacy@example.com", balance_rub=125, ref_code="legacy-ref"))
        session.add(WebOrder(order_id="legacy-order", profile_token=profile, contact="legacy@example.com", tariff_key="vpn", tariff_label="VPN", days=30, amount_rub=100, status="delivered"))
        session.add(WebProfileLink(profile_token=profile, telegram_id=23456))
        await session.commit()
    old_login = await routes.handle_auth_login(request({"contact": "legacy@example.com", "password": "old-password"}))
    cookies = {routes._AUTH_COOKIE: old_login.cookies[routes._AUTH_COOKIE].value}
    assert body(await routes.handle_forgot_password(request({"email": "LEGACY@example.com"}))) == {"ok": True}
    token = last_token(send)
    async with patched_webstore() as session:
        assert (await session.get(WebEmailToken, hashlib.sha256(token.encode()).hexdigest())).profile_token == profile
    assert (await routes.handle_email_token(request({"token": token, "password": "new-password"}, "/api/store/auth/reset"))).status == 200
    assert (await routes.handle_email_token(request({"token": token, "password": "new-password"}, "/api/store/auth/reset"))).status == 400
    assert not body(await routes.handle_auth_me(request({}, cookies=cookies)))["authenticated"]
    assert (await routes.handle_auth_login(request({"contact": "legacy@example.com", "password": "old-password"}))).status == 401
    login = await routes.handle_auth_login(request({"contact": "legacy@example.com", "password": "new-password"}))
    cookies = {routes._AUTH_COOKIE: login.cookies[routes._AUTH_COOKIE].value}
    async with patched_webstore() as session:
        assert (await session.get(WebEmail, "legacy@example.com")).profile_token == profile
        assert (await session.get(WebBalanceAccount, profile)).balance_rub == 125
        assert (await session.get(WebBalanceAccount, profile)).ref_code == "legacy-ref"
        assert (await session.scalar(select(WebOrder))).order_id == "legacy-order"
        assert (await session.get(WebProfileLink, profile)).telegram_id == 23456
    assert (await routes.handle_email_change(request({"email": "new@example.com", "password": "new-password"}, cookies=cookies))).status == 200
    assert (await routes.handle_email_token(request({"token": last_token(send)}))).status == 200
    count = send.await_count
    assert body(await routes.handle_forgot_password(request({"email": "legacy@example.com"}))) == {"ok": True}
    assert send.await_count == count
    assert (await routes.handle_auth_login(request({"contact": "new@example.com", "password": "new-password"}))).status == 200


async def test_old_reset_link_rejected_after_legacy_email_change(patched_webstore, monkeypatch):
    send = AsyncMock()
    monkeypatch.setattr(routes, "send_email", send)
    async with patched_webstore() as session:
        session.add(WebAccount(contact="legacy@example.com", profile_token="legacy", password_hash=routes._hash_password("old-password")))
        await session.commit()
    await routes.handle_forgot_password(request({"email": "legacy@example.com"}))
    old_reset = last_token(send)
    login = await routes.handle_auth_login(request({"contact": "legacy@example.com", "password": "old-password"}))
    cookies = {routes._AUTH_COOKIE: login.cookies[routes._AUTH_COOKIE].value}
    await routes.handle_email_change(request({"email": "changed@example.com", "password": "old-password"}, cookies=cookies))
    assert (await routes.handle_email_token(request({"token": last_token(send)}))).status == 200
    assert (await routes.handle_email_token(request({"token": old_reset, "password": "new-password"}, "/api/store/auth/reset"))).status == 400
    assert (await routes.handle_auth_login(request({"contact": "changed@example.com", "password": "old-password"}))).status == 200


async def test_device_purchase_uses_confirmed_email(patched_webstore, monkeypatch):
    import sys
    from test_webstore_flows import _FakeYooKassa, _FakeYooKassaPaymentApi

    monkeypatch.setitem(sys.modules, "yookassa", _FakeYooKassa())
    monkeypatch.setattr(routes.settings, "yookassa_shop_id", "test-shop")
    monkeypatch.setattr(routes.settings, "yookassa_secret_key", "test-secret")
    monkeypatch.setattr(routes, "_get_order_provider", lambda order: "marzban")
    async with patched_webstore() as session:
        session.add(WebAccount(contact="+71234567890", profile_token="legacy", password_hash=routes._hash_password("password-one")))
        session.add(WebEmail(email="new@example.com", profile_token="legacy"))
        session.add(WebOrder(order_id="old-order", profile_token="legacy", contact="old@example.com", email="old@example.com", tariff_key="vpn_30", tariff_label="VPN", days=30, amount_rub=100, status="delivered", marzban_username="existing-user", access_expires_at=datetime.utcnow() + timedelta(days=30)))
        await session.commit()
    login = await routes.handle_auth_login(request({"contact": "new@example.com", "password": "password-one"}))
    cookies = {routes._AUTH_COOKIE: login.cookies[routes._AUTH_COOKIE].value}
    response = await routes.handle_create_device_order(request({}, cookies=cookies))
    assert response.status == 200
    assert _FakeYooKassaPaymentApi.last_params["receipt"]["customer"]["email"] == "new@example.com"
    async with patched_webstore() as session:
        order = await session.scalar(select(WebOrder).where(WebOrder.tariff_key == "device_slot"))
        assert (order.profile_token, order.contact, order.email) == ("legacy", "new@example.com", "new@example.com")
        account = await session.scalar(select(WebAccount).where(WebAccount.profile_token == "legacy"))
        assert account.contact == "+71234567890"
