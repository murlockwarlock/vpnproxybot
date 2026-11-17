from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from test_webstore_telegram_linking import db_engine, db_session_factory, patched_webstore, _Request
from webstore import routes
from webstore.models import (
    WebAccount,
    WebBalanceAccount,
    WebOrder,
    WebProfileLink,
    WebSession,
    WebTelegramItem,
)

pytestmark = pytest.mark.asyncio


class _FakeYooKassaPaymentApi:
    @classmethod
    def create(cls, params, idempotence_key):
        return SimpleNamespace(
            id="yk_sec_test",
            confirmation=SimpleNamespace(confirmation_url="https://pay.test/redirect"),
        )


class _FakeYooKassa(SimpleNamespace):
    Configuration = SimpleNamespace(account_id=None, secret_key=None)
    Payment = _FakeYooKassaPaymentApi


async def test_session_is_random_hashed_and_revoked(patched_webstore):
    async with patched_webstore() as session:
        session.add(WebAccount(contact="123456", profile_token="profile-id", password_hash=routes._hash_password("safe-password")))
        await session.commit()
    response = await routes.handle_auth_login(_Request(_json_body={"contact": "123456", "password": "safe-password"}))
    assert response.status == 200
    cookie = response.cookies[routes._AUTH_COOKIE]
    secret = cookie.value
    assert secret != "profile-id" and cookie["httponly"] and cookie["secure"] and cookie["samesite"] == "Lax"
    async with patched_webstore() as session:
        stored = await session.scalar(select(WebSession))
        assert stored.token_hash == hashlib.sha256(secret.encode()).hexdigest()
    assert (await routes.handle_profile_orders(_Request(query={"token": "profile-id"}, cookies={routes._AUTH_COOKIE: "profile-id"}))).status == 403
    assert json.loads((await routes.handle_auth_me(_Request(cookies={routes._AUTH_COOKIE: secret}))).text)["authenticated"]
    await routes.handle_auth_logout(_Request(cookies={routes._AUTH_COOKIE: secret}))
    assert not json.loads((await routes.handle_auth_me(_Request(cookies={routes._AUTH_COOKIE: secret}))).text)["authenticated"]


async def test_unauthenticated_create_profile_cannot_steal_token_by_email_or_telegram_link(patched_webstore):
    """P0-4: POST /api/store/create-profile must reject unauthenticated contact/token even without WebAccount."""
    victim_email = "victim-guest@example.com"
    victim_token = routes._make_profile_token(victim_email)
    tg_token = "tg-linked-profile-token"

    async with patched_webstore() as session:
        session.add(
            WebOrder(
                order_id="victim_paid_1",
                contact=victim_email,
                email=victim_email,
                profile_token=victim_token,
                tariff_key="vpn_30",
                tariff_label="Базовый",
                days=30,
                amount_rub=299,
                status="delivered",
                subscription_url="https://secret.test/sub/victim-1",
            )
        )
        session.add(
            WebBalanceAccount(
                profile_token=victim_token,
                contact=victim_email,
                balance_rub=450,
                ref_code="ref_victim_1",
            )
        )
        session.add(
            WebProfileLink(
                profile_token=tg_token,
                contact="@victim_tg",
                telegram_id=888777666,
            )
        )
        await session.commit()

    # 1. Raw email probe
    r1 = await routes.handle_create_profile(_Request(_json_body={"contact": victim_email}))
    assert r1.status == 401
    assert victim_token not in r1.text

    # 2. Direct token probe for passwordless guest profile
    r2 = await routes.handle_create_profile(_Request(_json_body={"token": victim_token}))
    assert r2.status == 401
    assert "ref_victim_1" not in r2.text

    # 3. Direct token probe for Telegram-linked profile without WebAccount
    r3 = await routes.handle_create_profile(_Request(_json_body={"token": tg_token}))
    assert r3.status == 401


async def test_passwordless_and_telegram_profiles_reject_unauthenticated_access_and_forged_legacy_cookie(
    patched_webstore, monkeypatch
):
    """P0-4: Knowing profile_token or forging webstore_profile_token cookie cannot read keys, link TG, or spend balance."""
    monkeypatch.setitem(sys.modules, "yookassa", _FakeYooKassa())
    monkeypatch.setattr(routes.settings, "yookassa_shop_id", "shop")
    monkeypatch.setattr(routes.settings, "yookassa_secret_key", "secret")

    victim_email = "passwordless@example.com"
    victim_token = routes._make_profile_token(victim_email)
    owned_uuid = "11112222-3333-4444-5555-666677778888"

    async with patched_webstore() as session:
        session.add(
            WebOrder(
                id=501,
                order_id="ord_victim_501",
                contact=victim_email,
                email=victim_email,
                profile_token=victim_token,
                tariff_key="vpn_30",
                tariff_label="Базовый",
                days=30,
                amount_rub=300,
                status="delivered",
                purchase_action="new",
                marzban_username=f"adapt_{owned_uuid}",
                subscription_url=f"https://darimiru.xyz/vpnbot/adapt-sub/{owned_uuid}",
                access_expires_at=datetime.utcnow() + timedelta(days=25),
            )
        )
        session.add(
            WebTelegramItem(
                profile_token=victim_token,
                telegram_id=999111222,
                item_type="vpn",
                external_id="tg_sub_1",
                title="TG Key",
                key_value="vless://secret-tg-key",
                status="active",
            )
        )
        session.add(
            WebBalanceAccount(
                profile_token=victim_token,
                contact=victim_email,
                balance_rub=500,
                ref_code="ref_pwless",
            )
        )
        await session.commit()

    forged_legacy = {"webstore_profile_token": victim_token}

    # Unauthenticated query and forged legacy cookie must BOTH fail on profile-orders
    resp_unauth = await routes.handle_profile_orders(_Request(query={"token": victim_token}))
    assert resp_unauth.status == 401
    assert owned_uuid not in resp_unauth.text
    assert "secret-tg-key" not in resp_unauth.text

    resp_legacy = await routes.handle_profile_orders(
        _Request(query={"token": victim_token}, cookies=forged_legacy)
    )
    assert resp_legacy.status == 401
    assert owned_uuid not in resp_legacy.text

    # auth/me must NOT authenticate from legacy cookie
    me_legacy = json.loads((await routes.handle_auth_me(_Request(cookies=forged_legacy))).text)
    assert me_legacy["authenticated"] is False

    # Telegram link hijack attempt must fail
    link_resp = await routes.handle_profile_link_init(
        _Request(_json_body={"token": victim_token}, cookies=forged_legacy)
    )
    assert link_resp.status == 401

    # Balance topup & toggle must fail
    topup_resp = await routes.handle_create_balance_topup(
        _Request(_json_body={"token": victim_token, "amount_rub": 100}, cookies=forged_legacy)
    )
    assert topup_resp.status == 401

    toggle_resp = await routes.handle_balance_toggle(
        _Request(_json_body={"token": victim_token, "enabled": False}, cookies=forged_legacy)
    )
    assert toggle_resp.status == 401

    # Device list & delete must fail
    dev_list = await routes.handle_devices_list(
        _Request(query={"target": "order:501"}, cookies=forged_legacy)
    )
    assert dev_list.status == 401

    dev_del = await routes.handle_device_delete(
        _Request(_json_body={"device_target": "order:501", "device_id": 1}, cookies=forged_legacy)
    )
    assert dev_del.status == 401

    # Spending victim's passwordless balance via contact or forged legacy cookie must fail!
    spend_resp = await routes.handle_create_order(
        _Request(
            _json_body={"tariff_key": "vpn_30", "contact": victim_email, "use_balance": True},
            cookies=forged_legacy,
        )
    )
    assert spend_resp.status == 401
    async with patched_webstore() as session:
        assert (await session.get(WebBalanceAccount, victim_token)).balance_rub == 500


async def test_unpaid_guest_order_never_leaks_profile_token_or_unlocks_lookup(
    patched_webstore, monkeypatch
):
    """P0-4: Unpaid guest checkout with victim's email must not leak victim's profile_token
    via /api/store/order-status or /api/store/profile-lookup, and must isolate guest orders.
    """
    monkeypatch.setitem(sys.modules, "yookassa", _FakeYooKassa())
    monkeypatch.setattr(routes.settings, "yookassa_shop_id", "shop")
    monkeypatch.setattr(routes.settings, "yookassa_secret_key", "secret")

    victim_email = "existing-buyer@example.com"
    victim_token = routes._make_profile_token(victim_email)

    async with patched_webstore() as session:
        session.add(
            WebOrder(
                order_id="legit_paid_order_1",
                contact=victim_email,
                email=victim_email,
                profile_token=victim_token,
                tariff_key="vpn_30",
                tariff_label="Базовый",
                days=30,
                amount_rub=299,
                status="delivered",
                subscription_url="https://secret.test/victim-key",
            )
        )
        await session.commit()

    # Attacker creates an unpaid guest order with victim's email
    create_resp = await routes.handle_create_order(
        _Request(_json_body={"tariff_key": "vpn_30", "contact": victim_email})
    )
    assert create_resp.status == 200
    attacker_order_id = json.loads(create_resp.text)["order_id"]

    # 1. Order is isolated from victim's profile_token!
    async with patched_webstore() as session:
        attacker_order = await session.scalar(
            select(WebOrder).where(WebOrder.order_id == attacker_order_id)
        )
        assert attacker_order.profile_token != victim_token
        assert attacker_order.profile_token == f"guest_{attacker_order_id}"

    # 2. Polling order-status on pending/unpaid order never leaks profile_token or subscription_url
    status_resp = await routes.handle_order_status(_Request(query={"order_id": attacker_order_id}))
    status_data = json.loads(status_resp.text)
    assert status_data["status"] == "pending"
    assert status_data["profile_token"] is None
    assert status_data["subscription_url"] is None

    # 3. Attacker cannot use their unpaid order_id in /api/store/profile-lookup to access victim's profile
    lookup_resp = await routes.handle_profile_lookup(
        _Request(_json_body={"contact": victim_email, "order_id": attacker_order_id})
    )
    assert lookup_resp.status == 404


async def test_legitimate_first_time_guest_checkout_and_order_lookup_mint_real_session(
    patched_webstore, monkeypatch
):
    """Legitimate guest buyer receives their key on /pay/success and a real WebSession via checkout or order lookup."""
    monkeypatch.setitem(sys.modules, "yookassa", _FakeYooKassa())
    monkeypatch.setattr(routes.settings, "yookassa_shop_id", "shop")
    monkeypatch.setattr(routes.settings, "yookassa_secret_key", "secret")
    monkeypatch.setattr(routes, "_get_web_daily_charge_rub", lambda: _async_val(3.17))

    guest_email = "fresh-guest@example.com"
    create_resp = await routes.handle_create_order(
        _Request(_json_body={"tariff_key": "vpn_30", "contact": guest_email})
    )
    assert create_resp.status == 200
    guest_order_id = json.loads(create_resp.text)["order_id"]

    # Simulate payment delivery completing
    async with patched_webstore() as session:
        order = await session.scalar(select(WebOrder).where(WebOrder.order_id == guest_order_id))
        order.status = "delivered"
        order.subscription_url = "https://secret.test/fresh-guest-key"
        order.delivered_at = datetime.utcnow()
        await session.commit()

    # 1. Guest polls /api/store/order-status on /pay/success -> receives key + HttpOnly WebSession cookie!
    status_resp = await routes.handle_order_status(_Request(query={"order_id": guest_order_id}))
    assert status_resp.status == 200
    status_data = json.loads(status_resp.text)
    assert status_data["subscription_url"] == "https://secret.test/fresh-guest-key"
    assert status_data["profile_token"] == routes._make_profile_token(guest_email)
    session_cookie = status_resp.cookies[routes._AUTH_COOKIE].value

    # Guest can now open /profile using that WebSession cookie
    prof_resp = await routes.handle_profile_orders(
        _Request(cookies={routes._AUTH_COOKIE: session_cookie})
    )
    assert prof_resp.status == 200
    prof_data = json.loads(prof_resp.text)
    assert len(prof_data["orders"]) == 1
    assert prof_data["orders"][0]["order_id"] == guest_order_id

    # 2. If guest later clears cookies and uses /api/store/profile-lookup with (email + delivered order_id),
    # they receive a fresh WebSession cookie (while protected WebAccount profiles refuse order-id lookup).
    lookup_resp = await routes.handle_profile_lookup(
        _Request(_json_body={"contact": guest_email, "order_id": guest_order_id})
    )
    assert lookup_resp.status == 200
    lookup_cookie = lookup_resp.cookies[routes._AUTH_COOKIE].value
    assert (
        await routes.handle_profile_orders(_Request(cookies={routes._AUTH_COOKIE: lookup_cookie}))
    ).status == 200


async def test_unauthenticated_quick_renew_with_protected_contact_succeeds_without_exposing_profile(
    patched_webstore, monkeypatch
):
    """Unauthenticated Quick Renew entering a registered WebAccount email must not crash or leak the account profile."""
    monkeypatch.setitem(sys.modules, "yookassa", _FakeYooKassa())
    monkeypatch.setattr(routes.settings, "yookassa_shop_id", "shop")
    monkeypatch.setattr(routes.settings, "yookassa_secret_key", "secret")
    tariff_1m = {
        "key": "1m",
        "label": "1 месяц",
        "adapt_plan_uuid": "plan-1m",
        "price_rub": 300,
        "days": 30,
        "devices": 1,
        "adapt_cost_price_usd": 1.0,
        "is_active": True,
    }
    monkeypatch.setattr(routes, "get_store_tariffs", lambda: [tariff_1m])
    monkeypatch.setattr(routes, "get_store_tariffs_by_key", lambda: {"1m": tariff_1m})

    class _MockAdapt:
        async def get_status(self, adapt_uuid):
            return {
                "uuid": adapt_uuid,
                "plan_uuid": "plan-1m",
                "is_active": True,
                "end_date": (datetime.utcnow() + timedelta(days=5)).isoformat(),
            }

    monkeypatch.setattr(routes, "AdaptAPI", _MockAdapt)

    victim_email = "registered-owner@example.com"
    victim_profile = routes._make_profile_token(victim_email)
    async with patched_webstore() as session:
        session.add(
            WebAccount(
                contact=victim_email,
                profile_token=victim_profile,
                password_hash=routes._hash_password("strong-pass"),
            )
        )
        session.add(
            WebBalanceAccount(
                profile_token=victim_profile,
                contact=victim_email,
                balance_rub=500,
                ref_code="ref-reg-owner",
            )
        )
        await session.commit()

    resp = await routes.handle_create_order(
        _Request(
            _json_body={
                "tariff_key": "1m",
                "purchase_action": "renew",
                "target_order_id": "uuid:05a19dc5-a84f-4568-9994-ad9d8fb61354",
                "contact": victim_email,
            }
        )
    )
    assert resp.status == 200
    qr_order_id = json.loads(resp.text)["order_id"]

    async with patched_webstore() as session:
        qr_order = await session.scalar(select(WebOrder).where(WebOrder.order_id == qr_order_id))
        assert qr_order is not None
        assert qr_order.profile_token is None
        assert (await session.get(WebBalanceAccount, victim_profile)).balance_rub == 500
        qr_order.status = "delivered"
        qr_order.subscription_url = "https://secret.test/renewed-key"
        qr_order.delivered_at = datetime.utcnow()
        await session.commit()

    status_resp = await routes.handle_order_status(_Request(query={"order_id": qr_order_id}))
    assert status_resp.status == 200
    status_data = json.loads(status_resp.text)
    assert "05a19dc5-a84f-4568-9994-ad9d8fb61354" in (status_data["subscription_url"] or "")
    assert status_data["profile_token"] is None
    assert routes._AUTH_COOKIE not in status_resp.cookies


async def _async_val(val):
    return val
