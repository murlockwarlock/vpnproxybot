from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from webstore import routes as webstore_routes


async def test_store_upgrade_price_uses_server_quotes():
    html = webstore_routes._render(
        "store.html",
        tariffs=[{"key": "test", "price_rub": 100, "days": 30}],
        MIN_PURCHASE_PRICE_RUB=10,
    )

    assert "t.payable_price_rub" in html
    assert "Остаток действующей подписки учтён" in html
    assert "upgradeResidual" not in html
    assert "{{MIN_PURCHASE_PRICE_RUB}}" not in html


from webstore.models import Base, WebBalanceAccount, WebBalanceTopUp, WebBalanceTransaction, WebOrder, WebProfileLink, WebSession, WebTelegramItem

pytestmark = pytest.mark.asyncio


class _Request(SimpleNamespace):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if not hasattr(self, "headers") or self.headers is None:
            self.headers = {}
        if not hasattr(self, "cookies") or self.cookies is None:
            self.cookies = {}
        if not hasattr(self, "remote"):
            self.remote = "127.0.0.1"

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
            id="yk_web_1",
            confirmation=SimpleNamespace(confirmation_url="https://pay.test/redirect"),
        )

    @classmethod
    def find_one(cls, payment_id):
        params = cls.last_params or {}
        amount = params.get("amount", {"value": "0.00", "currency": "RUB"})
        return SimpleNamespace(
            id=payment_id,
            status="succeeded",
            paid=True,
            amount=SimpleNamespace(
                value=amount.get("value", "0.00"),
                currency=amount.get("currency", "RUB"),
            ),
            metadata=params.get("metadata", {}),
        )


class _FakeYooKassa(SimpleNamespace):
    Configuration = SimpleNamespace(account_id=None, secret_key=None)
    Payment = _FakeYooKassaPaymentApi


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
async def patched_webstore(monkeypatch, db_session_factory):
    monkeypatch.setattr(webstore_routes, "async_session", db_session_factory)
    monkeypatch.setattr(webstore_routes.settings, "subscription_base_url", "https://loonapie.xyz")
    monkeypatch.setattr(webstore_routes.settings, "public_webstore_base_url", "https://loonapie.xyz")
    monkeypatch.setattr(webstore_routes.settings, "bot_url", "https://t.me/uskoritelinternetabot")
    monkeypatch.setattr(webstore_routes.settings, "site_name", "Ускоритель интернета")
    monkeypatch.setattr(webstore_routes.settings, "bridge_shared_secret", "bridge-secret")
    monkeypatch.setattr(webstore_routes.settings, "telegram_link_ttl_minutes", 15)
    return db_session_factory


async def test_create_profile_returns_ref_link_and_demo_url(monkeypatch, patched_webstore):
    import hashlib
    maybe_demo = AsyncMock(return_value="https://loonapie.xyz/s/demo-token")
    monkeypatch.setattr(webstore_routes, "_maybe_issue_web_demo_key", maybe_demo)
    monkeypatch.setattr(webstore_routes.settings, "demo_key_days", 3)

    unauth_resp = await webstore_routes.handle_create_profile(
        _Request(_json_body={"contact": "@user"}, cookies={}, headers={}, remote="127.0.0.1")
    )
    assert unauth_resp.status == 401

    profile_token = webstore_routes._make_profile_token("@user")
    session_secret = "valid-session-secret-for-demo"
    async with patched_webstore() as session:
        session.add(WebProfileLink(profile_token=profile_token, contact="@user", telegram_id=777001))
        session.add(
            WebSession(
                token_hash=hashlib.sha256(session_secret.encode()).hexdigest(),
                profile_token=profile_token,
                method="telegram",
                expires_at=datetime.utcnow() + timedelta(hours=1),
            )
        )
        await session.commit()

    request = _Request(
        _json_body={"token": profile_token},
        cookies={webstore_routes._AUTH_COOKIE: session_secret},
        headers={},
        remote="127.0.0.1",
    )

    response = await webstore_routes.handle_create_profile(request)
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["ref_url"].startswith("https://loonapie.xyz/buy?ref=")
    assert payload["demo_subscription_url"] == "https://loonapie.xyz/s/demo-token"
    assert payload["demo_days"] == 3

    async with patched_webstore() as session:
        account = await session.get(WebBalanceAccount, payload["token"])
        assert account is not None
        assert account.contact == "@user"
        assert account.ref_code == payload["ref_code"]


async def test_create_order_creates_pending_order_and_yookassa_payment(monkeypatch, patched_webstore):
    monkeypatch.setitem(sys.modules, "yookassa", _FakeYooKassa())
    monkeypatch.setattr(webstore_routes.settings, "yookassa_shop_id", "shop")
    monkeypatch.setattr(webstore_routes.settings, "yookassa_secret_key", "secret")

    request = _Request(
        _json_body={"tariff_key": "vpn_30", "contact": "user@example.com"},
        cookies={},
        headers={"X-Real-IP": "1.2.3.4"},
        remote="127.0.0.1",
    )

    response = await webstore_routes.handle_create_order(request)
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["redirect_url"] == "https://pay.test/redirect"
    assert _FakeYooKassaPaymentApi.last_params["description"] == "Ускоритель интернета — Лайт (1 месяц)"

    async with patched_webstore() as session:
        order = await session.scalar(select(WebOrder).where(WebOrder.order_id == payload["order_id"]))
        assert order is not None
        assert order.status == "pending"
        assert order.yookassa_payment_id == "yk_web_1"
        assert order.contact == "user@example.com"
        assert order.profile_token is not None


async def test_telegram_auth_claim_and_internal_profile_flow(monkeypatch, patched_webstore):
    migrate_balance = AsyncMock()
    monkeypatch.setattr(webstore_routes, "_migrate_web_balance_to_telegram", migrate_balance)

    init_request = _Request(_json_body={}, cookies={}, headers={}, remote="127.0.0.1")
    init_response = await webstore_routes.handle_telegram_auth_init(init_request)
    init_payload = json.loads(init_response.text)
    code = init_payload["code"]

    pending_response = await webstore_routes.handle_telegram_auth_status(
        _Request(_json_body={}, cookies={}, headers={}, query={"code": code}, remote="127.0.0.1")
    )
    assert json.loads(pending_response.text)["status"] == "pending"

    claim_request = _Request(
        _json_body={
            "code": code,
            "telegram_id": 123456,
            "telegram_username": "loonapie_user",
            "telegram_full_name": "Loonapie User",
            "items": [
                {
                    "item_type": "vpn",
                    "external_id": "sub-1",
                    "title": "Доступ",
                    "subtitle": "Активен",
                    "key_value": "https://loonapie.xyz/s/test-token",
                    "expires_at": (datetime.utcnow() + timedelta(days=30)).isoformat(),
                }
            ],
        },
        cookies={},
        headers={"X-Internal-Secret": "bridge-secret"},
        remote="127.0.0.1",
    )
    claim_response = await webstore_routes.handle_internal_telegram_auth_claim(claim_request)
    claim_payload = json.loads(claim_response.text)
    profile_token = claim_payload["profile_token"]

    async with patched_webstore() as session:
        session.add(
            WebOrder(
                order_id="order123",
                contact="@loonapie_user",
                tariff_key="vpn_30",
                tariff_label="Лайт (1 месяц)",
                days=30,
                amount_rub=95,
                original_amount_rub=95,
                bonus_applied_rub=0,
                status="delivered",
                profile_token=profile_token,
                subscription_url="https://loonapie.xyz/s/test-token",
            )
        )
        await session.commit()

    done_response = await webstore_routes.handle_telegram_auth_status(
        _Request(_json_body={}, cookies={}, headers={}, query={"code": code}, remote="127.0.0.1")
    )
    done_payload = json.loads(done_response.text)
    assert done_payload == {"status": "completed", "token": profile_token}

    login_response = await webstore_routes.handle_profile_page(
        _Request(_json_body={}, cookies={}, headers={}, query={"login": code}, remote="127.0.0.1")
    )
    assert login_response.status == 200
    assert webstore_routes._AUTH_COOKIE not in login_response.cookies
    cookie = done_response.cookies[webstore_routes._AUTH_COOKIE].value
    me = await webstore_routes.handle_auth_me(_Request(cookies={webstore_routes._AUTH_COOKIE: cookie}))
    assert json.loads(me.text)["profile_token"] == profile_token

    sync_request = _Request(
        _json_body={
            "telegram_id": 123456,
            "telegram_username": "loonapie_sync",
            "telegram_full_name": "Synced User",
            "items": [
                {
                    "item_type": "vpn",
                    "external_id": "sub-2",
                    "title": "Ключ",
                    "subtitle": "Синхронизирован",
                    "key_value": "https://loonapie.xyz/s/updated-token",
                }
            ],
        },
        cookies={},
        headers={"X-Internal-Secret": "bridge-secret"},
        remote="127.0.0.1",
    )
    sync_response = await webstore_routes.handle_internal_telegram_sync(sync_request)
    assert json.loads(sync_response.text) == {"ok": True}

    profile_response = await webstore_routes.handle_internal_web_profile(
        _Request(
            _json_body={},
            cookies={},
            headers={"X-Internal-Secret": "bridge-secret"},
            query={"telegram_id": "123456"},
            remote="127.0.0.1",
        )
    )
    profile_payload = json.loads(profile_response.text)

    assert profile_payload["contact"] is None
    assert profile_payload["orders"][0]["subscription_url"] == "https://loonapie.xyz/s/test-token"

    async with patched_webstore() as session:
        link = await session.scalar(select(WebProfileLink).where(WebProfileLink.telegram_id == 123456))
        items = (await session.execute(select(WebTelegramItem).where(WebTelegramItem.profile_token == profile_token))).scalars().all()
        assert link.telegram_username == "loonapie_sync"
        assert len(items) == 1
        assert items[0].key_value == "https://loonapie.xyz/s/updated-token"

    migrate_balance.assert_awaited_once()


async def test_yookassa_webhook_marks_order_delivered(monkeypatch, patched_webstore):
    async with patched_webstore() as session:
        order = WebOrder(
            order_id="ord-success",
            contact="user@example.com",
            tariff_key="vpn_30",
            tariff_label="Лайт (1 месяц)",
            days=30,
            amount_rub=95,
            original_amount_rub=95,
            bonus_applied_rub=0,
            status="pending",
            profile_token="profile-token",
        )
        session.add(order)
        await session.commit()

    async def _fulfill(_session, order):
        order.status = "delivered"
        order.subscription_url = "https://loonapie.xyz/s/final-token"
        order.delivered_at = datetime.utcnow()

    notify_admins = AsyncMock()
    apply_bonus = AsyncMock()
    sync_referral = AsyncMock()
    monkeypatch.setattr(webstore_routes, "_notify_admins_webstore", notify_admins)
    monkeypatch.setattr(webstore_routes, "_fulfill_order", _fulfill)
    monkeypatch.setattr(webstore_routes, "_apply_order_bonus_spend", apply_bonus)
    monkeypatch.setattr(webstore_routes, "_sync_referral_credit_for_order", sync_referral)
    monkeypatch.setattr(
        webstore_routes,
        "fetch_yookassa_payment",
        AsyncMock(
            return_value=SimpleNamespace(
                payment_id="yk_paid_1",
                status="succeeded",
                paid=True,
                amount_rub=Decimal("95.00"),
                currency="RUB",
                metadata={"source": "webstore", "order_id": "ord-success"},
            )
        ),
    )

    request = _Request(
        _json_body={
            "event": "payment.succeeded",
            "object": {
                "id": "yk_paid_1",
                "metadata": {"source": "webstore", "order_id": "ord-success"},
            },
        },
        cookies={},
        headers={},
        remote="127.0.0.1",
    )

    response = await webstore_routes.handle_yookassa_webhook(request)
    assert response.text == "OK"

    async with patched_webstore() as session:
        order = await session.scalar(select(WebOrder).where(WebOrder.order_id == "ord-success"))
        assert order.status == "delivered"
        assert order.yookassa_payment_id == "yk_paid_1"
        assert order.subscription_url == "https://loonapie.xyz/s/final-token"
        assert order.paid_at is not None

    assert notify_admins.await_count == 2
    apply_bonus.assert_awaited_once()
    sync_referral.assert_awaited_once_with("ord-success")


async def test_yookassa_webhook_rejects_unverified_callback(monkeypatch, patched_webstore):
    async with patched_webstore() as session:
        session.add(
            WebOrder(
                order_id="ord-forged",
                contact="user@example.com",
                tariff_key="vpn_30",
                tariff_label="Лайт (1 месяц)",
                days=30,
                amount_rub=95,
                original_amount_rub=95,
                bonus_applied_rub=0,
                status="pending",
                profile_token="profile-forged",
                yookassa_payment_id="yk_forged_1",
            )
        )
        await session.commit()

    fulfill = AsyncMock()
    monkeypatch.setattr(webstore_routes, "attempt_fulfill_order", fulfill)
    monkeypatch.setattr(
        webstore_routes,
        "fetch_yookassa_payment",
        AsyncMock(
            return_value=SimpleNamespace(
                payment_id="yk_forged_1",
                status="succeeded",
                paid=True,
                amount_rub=Decimal("1.00"),
                currency="RUB",
                metadata={"source": "webstore", "order_id": "ord-forged"},
            )
        ),
    )

    response = await webstore_routes.handle_yookassa_webhook(
        _Request(
            _json_body={
                "event": "payment.succeeded",
                "object": {
                    "id": "yk_forged_1",
                    "metadata": {"source": "webstore", "order_id": "ord-forged"},
                },
            },
            cookies={},
            headers={},
            remote="127.0.0.1",
        )
    )

    assert response.status == 200
    fulfill.assert_not_awaited()
    async with patched_webstore() as session:
        order = await session.scalar(
            select(WebOrder).where(WebOrder.order_id == "ord-forged")
        )
        assert order.status == "pending"
        assert order.paid_at is None


async def test_yookassa_topup_webhook_credits_only_verified_payment(monkeypatch, patched_webstore):
    async with patched_webstore() as session:
        session.add(WebBalanceAccount(
            profile_token="topup-profile",
            contact="user@example.com",
            ref_code="topup-ref",
            balance_rub=0,
        ))
        session.add(WebBalanceTopUp(
            topup_id="topup-verified",
            profile_token="topup-profile",
            telegram_id=0,
            amount_rub=100,
            status="pending",
            yookassa_payment_id="yk_topup_1",
        ))
        await session.commit()

    monkeypatch.setattr(
        webstore_routes,
        "fetch_yookassa_payment",
        AsyncMock(return_value=SimpleNamespace(
            payment_id="yk_topup_1",
            status="succeeded",
            paid=True,
            amount_rub=Decimal("100.00"),
            currency="RUB",
            metadata={"source": "webstore_balance_topup", "topup_id": "topup-verified"},
        )),
    )

    response = await webstore_routes.handle_yookassa_webhook(_Request(
        _json_body={
            "event": "payment.succeeded",
            "object": {
                "id": "yk_topup_1",
                "metadata": {"source": "webstore_balance_topup", "topup_id": "topup-verified"},
            },
        },
        cookies={},
        headers={},
        remote="127.0.0.1",
    ))

    assert response.status == 200
    async with patched_webstore() as session:
        account = await session.get(WebBalanceAccount, "topup-profile")
        topup = await session.scalar(
            select(WebBalanceTopUp).where(WebBalanceTopUp.topup_id == "topup-verified")
        )
        tx = await session.scalar(
            select(WebBalanceTransaction).where(
                WebBalanceTransaction.source_id == "topup-verified"
            )
        )
        assert account.balance_rub == 100
        assert topup.status == "completed"
        assert tx is not None
        assert tx.amount_rub == 100


async def test_device_slot_order_reuses_existing_web_subscription_key(monkeypatch, patched_webstore):
    class FailingMarzbanClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("device slot must not create a new Marzban user")

    monkeypatch.setattr(webstore_routes, "MarzbanClient", FailingMarzbanClient)
    expires_at = datetime.utcnow() + timedelta(days=30)

    async with patched_webstore() as session:
        primary = WebOrder(
            order_id="primary-web",
            contact="user@example.com",
            tariff_key="vpn_30",
            tariff_label="Лайт (1 месяц)",
            days=30,
            amount_rub=99,
            status="delivered",
            profile_token="profile-token",
            marzban_username="web_primary",
            subscription_url="https://loonapie.xyz/s/primary-token",
            access_expires_at=expires_at,
            delivered_at=datetime.utcnow(),
        )
        device = WebOrder(
            order_id="device-web",
            contact="user@example.com",
            tariff_key="device_slot",
            tariff_label="Доп. устройство",
            days=0,
            amount_rub=100,
            status="pending",
            profile_token="profile-token",
            access_expires_at=expires_at,
        )
        session.add_all([primary, device])
        await session.flush()

        await webstore_routes._fulfill_device_slot_order(session, device)

        assert device.status == "delivered"
        assert device.marzban_username == "web_primary"
        assert device.subscription_url == "https://loonapie.xyz/s/primary-token"
        assert device.access_expires_at == expires_at


async def test_device_slot_order_reuses_linked_telegram_key_and_increments_slots(monkeypatch, patched_webstore):
    class FailingMarzbanClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("device slot must not create a new Marzban user")

    monkeypatch.setattr(webstore_routes, "MarzbanClient", FailingMarzbanClient)
    expires_at = datetime.utcnow() + timedelta(days=30)

    async with patched_webstore() as session:
        tg_item = WebTelegramItem(
            profile_token="profile-token",
            telegram_id=123456,
            item_type="vpn",
            external_id="sub_7",
            title="VPN",
            key_value="https://loonapie.xyz/s/telegram-token",
            status="active",
            device_slots=3,
            expires_at=expires_at,
        )
        device = WebOrder(
            order_id="device-tg",
            contact="@user",
            tariff_key="device_slot",
            tariff_label="Доп. устройство",
            days=0,
            amount_rub=100,
            status="pending",
            profile_token="profile-token",
            access_expires_at=expires_at,
        )
        session.add_all([tg_item, device])
        await session.flush()

        await webstore_routes._fulfill_device_slot_order(session, device)

        assert device.status == "delivered"
        assert device.marzban_username == "sub_7"
        assert device.subscription_url == "https://loonapie.xyz/s/telegram-token"
        assert tg_item.device_slots == 4


async def test_device_slot_order_rejects_adapt_subscription(monkeypatch, patched_webstore):
    monkeypatch.setattr(webstore_routes, "_notify_admins_webstore", AsyncMock())
    expires_at = datetime.utcnow() + timedelta(days=30)

    async with patched_webstore() as session:
        primary = WebOrder(
            order_id="adapt-parent",
            contact="user@example.com",
            tariff_key="basic_30",
            tariff_label="Базовый 3 устройства",
            days=30,
            amount_rub=249,
            status="delivered",
            profile_token="adapt-profile",
            marzban_username="adapt_770fa622-a4bd-63f6-c938-668877662222",
            subscription_url="https://test/adapt-sub/770fa622-a4bd-63f6-c938-668877662222",
            access_expires_at=expires_at,
            delivered_at=datetime.utcnow(),
        )
        device = WebOrder(
            order_id="adapt-device",
            contact="user@example.com",
            tariff_key="device_slot",
            tariff_label="Доп. устройство",
            days=0,
            amount_rub=100,
            status="paid",
            profile_token="adapt-profile",
        )
        session.add_all([primary, device])
        await session.flush()

        await webstore_routes._fulfill_device_slot_order(session, device)

        assert device.status == "failed"
        assert "улучшение тарифа" in device.failure_message
        assert device.subscription_url is None


async def test_web_adapt_upgrade_reuses_uuid_and_does_not_renew(monkeypatch, patched_webstore):
    adapt_uuid = "770fa622-a4bd-63f6-c938-668877662222"
    expires_at = datetime.utcnow() + timedelta(days=30)
    async with patched_webstore() as session:
        primary = WebOrder(
            order_id="adapt-old", contact="user@example.com", tariff_key="basic_30",
            tariff_label="Базовый 3 устройства", days=30, amount_rub=249,
            status="delivered", profile_token="adapt-profile",
            marzban_username=f"adapt_{adapt_uuid}", subscription_url=f"https://test/adapt-sub/{adapt_uuid}",
            access_expires_at=datetime.utcnow() + timedelta(days=10), delivered_at=datetime.utcnow(),
        )
        upgrade = WebOrder(
            order_id="adapt-upgrade", contact="user@example.com", tariff_key="basic_30_5",
            tariff_label="Базовый 5 устройств", days=30, amount_rub=100,
            status="paid", profile_token="adapt-profile", purchase_action="upgrade",
            target_order_id="adapt-old",
        )
        session.add_all([primary, upgrade])
        await session.flush()

        api = AsyncMock()
        api.upgrade_subscription = AsyncMock(return_value={"success": True, "devices": 5})
        api.get_status = AsyncMock(side_effect=[
            {"plan_uuid": "old-plan-uuid", "end_date": primary.access_expires_at.isoformat(), "devices": 3},
            {"plan_uuid": "new-plan-uuid", "end_date": expires_at.isoformat(), "devices": 5},
        ])
        api.list_plans = AsyncMock(return_value=[{"uuid": "new-plan-uuid", "devices": 5}])
        api.renew_subscription = AsyncMock()
        monkeypatch.setattr(webstore_routes, "AdaptAPI", lambda: api)

        await webstore_routes._fulfill_adapt_order(session, upgrade, "new-plan-uuid")

        assert upgrade.status == "delivered"
        assert upgrade.subscription_url == primary.subscription_url
        assert upgrade.marzban_username == primary.marzban_username
        api.upgrade_subscription.assert_awaited_once_with(adapt_uuid, "new-plan-uuid")
        api.renew_subscription.assert_not_awaited()


async def test_web_adapt_upgrade_not_enough_days_fails_closed(
    monkeypatch, patched_webstore
):
    adapt_uuid = "770fa622-a4bd-63f6-c938-668877662228"
    expires_at = datetime.utcnow() + timedelta(minutes=10)
    async with patched_webstore() as session:
        primary = WebOrder(
            order_id="adapt-almost-expired", contact="user@example.com", tariff_key="basic-old",
            tariff_label="14 дней", days=14, amount_rub=90,
            status="delivered", profile_token="adapt-profile-near-expire",
            marzban_username=f"adapt_{adapt_uuid}",
            subscription_url=f"https://darimiru.ru/vpnbot/adapt-sub/{adapt_uuid}",
            access_expires_at=expires_at,
            created_at=datetime.utcnow() - timedelta(days=13),
            delivered_at=datetime.utcnow() - timedelta(days=13),
        )
        upgrade = WebOrder(
            order_id="adapt-upgrade-near-expire", contact="user@example.com", tariff_key="monthly-new",
            tariff_label="30 дней", days=30, amount_rub=155,
            status="paid", profile_token="adapt-profile-near-expire",
            purchase_action="upgrade",
            target_order_id="adapt-almost-expired",
        )
        session.add_all([primary, upgrade])
        await session.flush()

        api = AsyncMock()
        from bot.services.adapt_api import AdaptAPIError
        api.upgrade_subscription = AsyncMock(
            side_effect=AdaptAPIError("Not enough days remaining to upgrade", status=400)
        )
        api.renew_subscription_custom = AsyncMock(return_value={"success": True})
        api.get_status = AsyncMock(side_effect=[
            {"plan_uuid": "old-plan-uuid", "end_date": expires_at.isoformat(), "devices": 3},
        ])
        api.list_plans = AsyncMock(return_value=[{"uuid": "new-plan-uuid", "devices": 3}])
        api.renew_subscription = AsyncMock()
        admin_notify_mock = AsyncMock()
        monkeypatch.setattr(webstore_routes, "_notify_admins_webstore", admin_notify_mock)
        monkeypatch.setattr(webstore_routes, "AdaptAPI", lambda: api)

        await webstore_routes._fulfill_adapt_order(session, upgrade, "new-plan-uuid")

        assert upgrade.status == "failed"
        assert upgrade.failure_code == "adapt_upgrade_reactivation_required"
        assert upgrade.adapt_mutation_state == webstore_routes._ADAPT_MUTATION_FAILED_CLOSED
        assert upgrade.next_fulfillment_retry_at is None
        assert api.upgrade_subscription.await_count == 1
        api.renew_subscription_custom.assert_not_awaited()
        admin_notify_mock.assert_awaited_once()


async def test_web_expired_adapt_upgrade_reactivates_then_changes_to_cheaper_plan(
    monkeypatch, patched_webstore
):
    adapt_uuid = "770fa622-a4bd-63f6-c938-668877662229"
    expired_end = datetime.utcnow() - timedelta(days=2)
    reactivated_end = datetime.utcnow() + timedelta(days=1)
    upgraded_end = datetime.utcnow() + timedelta(days=30)
    async with patched_webstore() as session:
        primary = WebOrder(
            order_id="adapt-expired-old", contact="user@example.com", tariff_key="annual-old",
            tariff_label="Год", days=365, amount_rub=995,
            status="delivered", profile_token="adapt-expired-profile",
            marzban_username=f"adapt_{adapt_uuid}",
            subscription_url=f"https://test/adapt-sub/{adapt_uuid}",
            access_expires_at=expired_end, delivered_at=datetime.utcnow(),
        )
        upgrade = WebOrder(
            order_id="adapt-expired-upgrade", contact="user@example.com", tariff_key="monthly-new",
            tariff_label="Месяц", days=30, amount_rub=155,
            status="paid", profile_token="adapt-expired-profile", purchase_action="upgrade",
            target_order_id=primary.order_id,
            target_snapshot_plan_uuid="annual-plan",
            target_snapshot_expires_at=expired_end,
        )
        session.add_all([primary, upgrade])
        await session.flush()

        api = AsyncMock()
        api.get_status.side_effect = [
            {"plan_uuid": "annual-plan", "end_date": expired_end.isoformat(), "devices": 5},
            {"plan_uuid": "annual-plan", "end_date": reactivated_end.isoformat(), "devices": 5},
            {"plan_uuid": "monthly-plan", "end_date": upgraded_end.isoformat(), "devices": 1},
        ]
        api.list_plans.return_value = [{"uuid": "monthly-plan", "devices": 1}]
        api.renew_subscription_custom.return_value = {"end_date": reactivated_end.isoformat()}
        api.upgrade_subscription.return_value = {"success": True}
        monkeypatch.setattr(webstore_routes, "AdaptAPI", lambda: api)

        await webstore_routes._fulfill_adapt_order(session, upgrade, "monthly-plan")

        assert upgrade.status == "delivered"
        assert upgrade.subscription_url == primary.subscription_url
        assert upgrade.access_expires_at == upgraded_end
        api.renew_subscription_custom.assert_awaited_once_with(adapt_uuid, 3)
        api.upgrade_subscription.assert_awaited_once_with(adapt_uuid, "monthly-plan")


async def test_web_expired_upgrade_retry_skips_second_reactivation(
    monkeypatch, patched_webstore
):
    adapt_uuid = "770fa622-a4bd-63f6-c938-668877662230"
    expired_end = datetime.utcnow() - timedelta(days=2)
    reactivated_end = datetime.utcnow() + timedelta(days=1)
    upgraded_end = datetime.utcnow() + timedelta(days=30)
    async with patched_webstore() as session:
        primary = WebOrder(
            order_id="adapt-retry-old", contact="user@example.com", tariff_key="annual-old",
            tariff_label="Год", days=365, amount_rub=995,
            status="delivered", profile_token="adapt-retry-profile",
            marzban_username=f"adapt_{adapt_uuid}",
            subscription_url=f"https://test/adapt-sub/{adapt_uuid}",
            access_expires_at=reactivated_end, delivered_at=datetime.utcnow(),
        )
        retry = WebOrder(
            order_id="adapt-retry-upgrade", contact="user@example.com", tariff_key="monthly-new",
            tariff_label="Месяц", days=30, amount_rub=155,
            status="paid", profile_token="adapt-retry-profile", purchase_action="upgrade",
            target_order_id=primary.order_id, fulfillment_attempts=2,
            target_snapshot_plan_uuid="annual-plan",
            target_snapshot_expires_at=expired_end,
        )
        session.add_all([primary, retry])
        await session.flush()

        api = AsyncMock()
        api.get_status.side_effect = [
            {"plan_uuid": "annual-plan", "end_date": reactivated_end.isoformat(), "devices": 5},
            {"plan_uuid": "monthly-plan", "end_date": upgraded_end.isoformat(), "devices": 1},
        ]
        api.list_plans.return_value = [{"uuid": "monthly-plan", "devices": 1}]
        api.upgrade_subscription.return_value = {"success": True}
        monkeypatch.setattr(webstore_routes, "AdaptAPI", lambda: api)

        await webstore_routes._fulfill_adapt_order(session, retry, "monthly-plan")

        assert retry.status == "delivered"
        api.renew_subscription_custom.assert_not_awaited()
        api.upgrade_subscription.assert_awaited_once_with(adapt_uuid, "monthly-plan")


async def test_web_adapt_upgrade_accepts_linked_telegram_target(monkeypatch, patched_webstore):
    adapt_uuid = "770fa622-a4bd-63f6-c938-668877662223"
    old_plan_uuid = "661f9511-f3ac-52e5-b827-557766551111"
    new_plan_uuid = "661f9511-f3ac-52e5-b827-557766552222"
    new_end = datetime.utcnow() + timedelta(days=30)

    async with patched_webstore() as session:
        tg_item = WebTelegramItem(
            profile_token="linked-profile",
            telegram_id=123456,
            item_type="vpn",
            external_id="sub_8",
            title="VPN",
            key_value=f"https://test/adapt-sub/{adapt_uuid}",
            provider="adapt",
            adapt_plan_uuid=old_plan_uuid,
            status="active",
            device_slots=3,
            expires_at=datetime.utcnow() + timedelta(days=10),
        )
        session.add(tg_item)
        await session.flush()
        upgrade = WebOrder(
            order_id="adapt-tg-upgrade",
            contact="user@example.com",
            tariff_key="basic_30_5",
            tariff_label="Базовый 5 устройств",
            days=30,
            amount_rub=100,
            status="paid",
            profile_token="linked-profile",
            purchase_action="upgrade",
            target_order_id=f"tg:{adapt_uuid}",
        )
        session.add(upgrade)
        await session.flush()

        api = AsyncMock()
        api.upgrade_subscription = AsyncMock(return_value={"success": True, "devices": 5})
        api.get_status = AsyncMock(side_effect=[
            {"plan_uuid": old_plan_uuid, "end_date": tg_item.expires_at.isoformat(), "devices": 3},
            {"plan_uuid": new_plan_uuid, "end_date": new_end.isoformat(), "devices": 5},
        ])
        api.list_plans = AsyncMock(return_value=[{"uuid": new_plan_uuid, "devices": 5}])
        api.renew_subscription = AsyncMock()
        monkeypatch.setattr(webstore_routes, "AdaptAPI", lambda: api)

        await webstore_routes._fulfill_adapt_order(session, upgrade, new_plan_uuid)

        assert upgrade.status == "delivered"
        assert upgrade.subscription_url == tg_item.key_value
        assert upgrade.marzban_username == f"adapt_{adapt_uuid}"
        assert tg_item.adapt_plan_uuid == new_plan_uuid
        assert tg_item.device_slots == 5
        api.upgrade_subscription.assert_awaited_once_with(adapt_uuid, new_plan_uuid)
        api.renew_subscription.assert_not_awaited()


async def test_web_adapt_upgrade_is_not_delivered_when_provider_keeps_old_plan(monkeypatch, patched_webstore):
    adapt_uuid = "770fa622-a4bd-63f6-c938-668877662225"
    old_plan_uuid = "old-plan-uuid"
    new_plan_uuid = "new-plan-uuid"
    old_end = datetime.utcnow() + timedelta(days=10)

    async with patched_webstore() as session:
        primary = WebOrder(
            order_id="adapt-old-mismatch", contact="user@example.com", tariff_key="basic_30",
            tariff_label="Базовый 3 устройства", days=30, amount_rub=249,
            status="delivered", profile_token="adapt-mismatch-profile",
            marzban_username=f"adapt_{adapt_uuid}", subscription_url=f"https://test/adapt-sub/{adapt_uuid}",
            access_expires_at=old_end, delivered_at=datetime.utcnow(),
        )
        upgrade = WebOrder(
            order_id="adapt-upgrade-mismatch", contact="user@example.com", tariff_key="basic_30_5",
            tariff_label="Базовый 5 устройств", days=30, amount_rub=100,
            status="paid", profile_token="adapt-mismatch-profile", purchase_action="upgrade",
            target_order_id=primary.order_id,
        )
        session.add_all([primary, upgrade])
        await session.flush()

        api = AsyncMock()
        api.get_status.side_effect = [
            {"plan_uuid": old_plan_uuid, "end_date": old_end.isoformat(), "devices": 3},
            {"plan_uuid": old_plan_uuid, "end_date": old_end.isoformat(), "devices": 3},
        ]
        api.list_plans.return_value = [{"uuid": new_plan_uuid, "devices": 5}]
        api.upgrade_subscription.return_value = {"success": True}
        monkeypatch.setattr(webstore_routes, "AdaptAPI", lambda: api)
        monkeypatch.setattr(webstore_routes, "_notify_admins_webstore", AsyncMock())
        monkeypatch.setattr(webstore_routes, "_notify_linked_webstore_user", AsyncMock())

        await webstore_routes._fulfill_adapt_order(session, upgrade, new_plan_uuid)

        assert upgrade.status == "failed"
        assert upgrade.failure_code == "adapt_result_mismatch"
        assert upgrade.subscription_url is None
        api.upgrade_subscription.assert_awaited_once_with(adapt_uuid, new_plan_uuid)




async def test_web_profile_marks_expired_subscription_upgradeable_to_lower_cost_tariff(monkeypatch):
    annual = {
        "key": "annual", "adapt_plan_uuid": "annual-plan",
        "price_rub": 995, "days": 365, "adapt_cost_price_usd": 12.0,  # ~0.0328/day
    }
    monthly = {
        "key": "monthly", "adapt_plan_uuid": "monthly-plan",
        "price_rub": 155, "days": 30, "adapt_cost_price_usd": 2.0,
    }
    lower_cost = {
        "key": "super_cheap", "adapt_plan_uuid": "cheap-plan",
        "price_rub": 50, "days": 30, "adapt_cost_price_usd": 0.5,
    }
    monkeypatch.setattr(webstore_routes, "get_store_tariffs", lambda: [annual, monthly])
    monkeypatch.setattr(
        webstore_routes,
        "get_store_tariffs_by_key",
        lambda: {"annual": annual, "monthly": monthly, "super_cheap": lower_cost},
    )
    order = WebOrder(
        order_id="expired-profile-order",
        tariff_key="annual",
        tariff_label="Год",
        days=365,
        amount_rub=995,
        status="delivered",
        marzban_username="adapt_770fa622-a4bd-63f6-c938-668877662231",
        subscription_url="https://test/adapt-sub/770fa622-a4bd-63f6-c938-668877662231",
        access_expires_at=datetime.utcnow() - timedelta(days=1),
    )

    payload = webstore_routes._serialize_orders([order])[0]
    assert payload["can_upgrade"] is True

    # The lower-cost plan still has a positive provider upgrade quote.
    monkeypatch.setattr(webstore_routes, "get_store_tariffs", lambda: [annual, lower_cost])
    payload2 = webstore_routes._serialize_orders([order])[0]
    assert payload2["can_upgrade"] is True


async def test_web_upgrade_option_uses_actual_remaining_term(monkeypatch):
    current = {
        "key": "current",
        "adapt_plan_uuid": "current-plan",
        "price_rub": 100,
        "days": 10,
        "adapt_cost_price_usd": 1.0,
    }
    target = {
        "key": "target",
        "adapt_plan_uuid": "target-plan",
        "price_rub": 50,
        "days": 2,
        "adapt_cost_price_usd": 0.35,
    }
    monkeypatch.setattr(webstore_routes, "get_store_tariffs", lambda: [current, target])
    monkeypatch.setattr(
        webstore_routes,
        "get_store_tariffs_by_key",
        lambda: {"current": current, "target": target},
    )
    monkeypatch.setattr(webstore_routes, "get_adapt_plan_metadata_by_key", lambda key: None)

    active_order = WebOrder(
        order_id="active-term-order",
        tariff_key="current",
        tariff_label="Текущий",
        days=10,
        amount_rub=100,
        status="delivered",
        subscription_url="https://test/adapt-sub/active-term",
        marzban_username="adapt_active-term",
        access_expires_at=datetime.utcnow() + timedelta(days=5),
    )
    expired_order = WebOrder(
        order_id="expired-term-order",
        tariff_key="current",
        tariff_label="Текущий",
        days=10,
        amount_rub=100,
        status="delivered",
        subscription_url="https://test/adapt-sub/expired-term",
        marzban_username="adapt_expired-term",
        access_expires_at=datetime.utcnow() - timedelta(days=1),
    )

    active_payload, expired_payload = webstore_routes._serialize_orders(
        [active_order, expired_order]
    )

    assert active_payload["can_upgrade"] is False
    assert expired_payload["can_upgrade"] is True


async def test_admin_client_search_finds_telegram_username_and_subscription_key(patched_webstore):
    async with patched_webstore() as session:
        session.add(WebProfileLink(
            profile_token="profile-search",
            contact="89150000000",
            telegram_id=123456789,
            telegram_username="search_user",
            telegram_full_name="Search User",
        ))
        session.add(WebTelegramItem(
            profile_token="profile-search",
            telegram_id=123456789,
            item_type="vpn",
            external_id="adapt-search-uuid",
            title="VPN",
            key_value="https://example.test/subscription/search-key",
        ))
        await session.commit()

    headers = {"X-Internal-Secret": "bridge-secret"}
    username_response = await webstore_routes.handle_internal_admin_client_lookup(
        _Request(query={"q": "@search_user"}, headers=headers)
    )
    key_response = await webstore_routes.handle_internal_admin_client_lookup(
        _Request(query={"q": "search-key"}, headers=headers)
    )

    assert username_response.status == 200
    assert json.loads(username_response.text)["profiles"][0]["profile_token"] == "profile-search"
    assert key_response.status == 200
    assert json.loads(key_response.text)["profiles"][0]["telegram"]["id"] == "123456789"


async def test_admin_order_actions_enforce_payment_state(monkeypatch, patched_webstore):
    notify_admins = AsyncMock()
    notify_customer = AsyncMock(return_value=True)
    sync_referral = AsyncMock()
    monkeypatch.setattr(webstore_routes, "_notify_admins_webstore", notify_admins)
    monkeypatch.setattr(webstore_routes, "_notify_linked_webstore_user", notify_customer)
    monkeypatch.setattr(webstore_routes, "_sync_referral_credit_for_order", sync_referral)
    async with patched_webstore() as session:
        unpaid = WebOrder(
            order_id="admin-unpaid",
            contact="unpaid@example.com",
            tariff_key="vpn_30",
            tariff_label="30 дней",
            days=30,
            amount_rub=100,
            status="pending",
        )
        paid = WebOrder(
            order_id="admin-paid",
            contact="paid@example.com",
            tariff_key="vpn_30",
            tariff_label="30 дней",
            days=30,
            amount_rub=100,
            status="paid",
            paid_at=datetime.utcnow(),
        )
        session.add_all([unpaid, paid])
        await session.commit()

    headers = {"X-Internal-Secret": "bridge-secret"}
    forbidden_retry = await webstore_routes.handle_internal_admin_order_action(
        _Request(_json_body={"order_id": "admin-unpaid", "action": "retry"}, headers=headers)
    )
    assert forbidden_retry.status == 409

    async def _deliver(_session, order):
        order.fulfillment_attempts += 1
        order.status = "delivered"
        order.delivered_at = datetime.utcnow()

    monkeypatch.setattr(webstore_routes, "attempt_fulfill_order", _deliver)
    retry_response = await webstore_routes.handle_internal_admin_order_action(
        _Request(_json_body={"order_id": "admin-paid", "action": "retry"}, headers=headers)
    )
    retry_payload = json.loads(retry_response.text)
    assert retry_response.status == 200
    assert retry_payload["before"]["status"] == "paid"
    assert retry_payload["order"]["status"] == "delivered"
    notify_admins.assert_awaited_once()
    notify_customer.assert_awaited_once()
    sync_referral.assert_awaited_once_with("admin-paid")

    cancel_paid = await webstore_routes.handle_internal_admin_order_action(
        _Request(_json_body={"order_id": "admin-paid", "action": "cancel"}, headers=headers)
    )
    assert cancel_paid.status == 409

    cancel_unpaid = await webstore_routes.handle_internal_admin_order_action(
        _Request(_json_body={"order_id": "admin-unpaid", "action": "cancel"}, headers=headers)
    )
    assert cancel_unpaid.status == 200
    assert json.loads(cancel_unpaid.text)["order"]["status"] == "canceled"


async def test_admin_retry_keeps_delivered_access_when_notification_fails(monkeypatch, patched_webstore):
    async with patched_webstore() as session:
        session.add(WebOrder(
            order_id="admin-notify-failure",
            contact="client@example.com",
            tariff_key="vpn_30",
            tariff_label="30 дней",
            days=30,
            amount_rub=100,
            status="paid",
            paid_at=datetime.utcnow(),
        ))
        await session.commit()

    async def _deliver(_session, order):
        order.fulfillment_attempts += 1
        order.status = "delivered"
        order.subscription_url = "https://example.test/sub/client"
        order.delivered_at = datetime.utcnow()

    monkeypatch.setattr(webstore_routes, "attempt_fulfill_order", _deliver)
    monkeypatch.setattr(
        webstore_routes, "_notify_admins_webstore",
        AsyncMock(side_effect=RuntimeError("telegram unavailable")),
    )
    monkeypatch.setattr(webstore_routes, "_notify_linked_webstore_user", AsyncMock(return_value=True))
    monkeypatch.setattr(webstore_routes, "_sync_referral_credit_for_order", AsyncMock())

    response = await webstore_routes.handle_internal_admin_order_action(
        _Request(
            _json_body={"order_id": "admin-notify-failure", "action": "retry"},
            headers={"X-Internal-Secret": "bridge-secret"},
        )
    )
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["order"]["status"] == "delivered"
    assert payload["warnings"]
    async with patched_webstore() as session:
        stored = await session.scalar(select(WebOrder).where(WebOrder.order_id == "admin-notify-failure"))
        assert stored.status == "delivered"
        assert stored.subscription_url == "https://example.test/sub/client"


@pytest.mark.asyncio
async def test_quick_renew_create_order_without_auth(monkeypatch, patched_webstore):
    monkeypatch.setitem(sys.modules, "yookassa", _FakeYooKassa())
    monkeypatch.setattr(webstore_routes.settings, "yookassa_shop_id", "test-shop")
    monkeypatch.setattr(webstore_routes.settings, "yookassa_secret_key", "test-secret")
    monkeypatch.setattr(
        webstore_routes, "get_store_tariffs",
        lambda: [
            {"key": "1m", "label": "1 месяц", "adapt_plan_uuid": "plan-1m", "price_rub": 300, "days": 30, "devices": 1, "adapt_cost_price_usd": 1.0, "is_active": True},
            {"key": "3m", "label": "3 месяца", "adapt_plan_uuid": "plan-3m", "price_rub": 800, "days": 90, "devices": 1, "adapt_cost_price_usd": 4.0, "is_active": True},
        ],
    )
    monkeypatch.setattr(
        webstore_routes, "get_store_tariffs_by_key",
        lambda: {
            "1m": {"key": "1m", "label": "1 месяц", "adapt_plan_uuid": "plan-1m", "price_rub": 300, "days": 30, "devices": 1, "adapt_cost_price_usd": 1.0, "is_active": True},
            "3m": {"key": "3m", "label": "3 месяца", "adapt_plan_uuid": "plan-3m", "price_rub": 800, "days": 90, "devices": 1, "adapt_cost_price_usd": 4.0, "is_active": True},
        },
    )

    class MockAdaptAPI:
        async def get_status(self, adapt_uuid):
            return {
                "uuid": adapt_uuid,
                "plan_uuid": "plan-1m",
                "is_active": True,
                "end_date": (datetime.utcnow() + timedelta(days=5)).isoformat(),
            }

    monkeypatch.setattr(webstore_routes, "AdaptAPI", MockAdaptAPI)

    request = _Request(
        _json_body={
            "tariff_key": "1m",
            "purchase_action": "renew",
            "target_order_id": "uuid:05a19dc5-a84f-4568-9994-ad9d8fb61354",
            "contact": "customer@example.com",
        },
    )
    response = await webstore_routes.handle_create_order(request)
    assert response.status == 200
    payload = json.loads(response.text)
    assert payload["redirect_url"] == "https://pay.test/redirect"

    async with patched_webstore() as session:
        created = await session.scalar(select(WebOrder).where(WebOrder.email == "customer@example.com"))
        assert created is not None
        assert created.target_order_id == "uuid:05a19dc5-a84f-4568-9994-ad9d8fb61354"
        assert created.purchase_action == "renew"


async def test_demo_upgrade_order_allows_missing_wholesale_metadata(
    monkeypatch, patched_webstore
):
    monkeypatch.setitem(sys.modules, "yookassa", _FakeYooKassa())
    monkeypatch.setattr(webstore_routes.settings, "yookassa_shop_id", "test-shop")
    monkeypatch.setattr(webstore_routes.settings, "yookassa_secret_key", "test-secret")
    demo_uuid = "05a19dc5-a84f-4568-9994-ad9d8fb61354"
    demo_plan_uuid = webstore_routes.settings.adapt_demo_plan_uuid
    tariffs = [
        {
            "key": "demo",
            "label": "Тестовый доступ",
            "adapt_plan_uuid": demo_plan_uuid,
            "adapt_cost_price_usd": None,
            "price_rub": 45,
            "days": 7,
            "provider": "adapt",
        },
        {
            "key": "paid",
            "label": "Платный тариф",
            "adapt_plan_uuid": "paid-plan-uuid",
            "adapt_cost_price_usd": None,
            "price_rub": 200,
            "days": 30,
            "provider": "adapt",
        },
    ]
    monkeypatch.setattr(webstore_routes, "get_store_tariffs", lambda: tariffs)
    monkeypatch.setattr(
        webstore_routes,
        "get_store_tariffs_by_key",
        lambda: {tariff["key"]: tariff for tariff in tariffs},
    )

    async with patched_webstore() as session:
        session.add(
            WebOrder(
                order_id="demo-order",
                contact="customer@example.com",
                tariff_key="demo",
                tariff_label="Тестовый доступ",
                days=7,
                amount_rub=45,
                status="demo",
                profile_token="demo-profile",
                marzban_username=f"adapt_{demo_uuid}",
                subscription_url=f"https://test/adapt-sub/{demo_uuid}",
                access_expires_at=datetime.utcnow() + timedelta(days=5),
                target_snapshot_plan_uuid=demo_plan_uuid,
            )
        )
        import hashlib
        session.add(
            WebSession(
                token_hash=hashlib.sha256(b"demo-session-secret").hexdigest(),
                profile_token="demo-profile",
                method="telegram",
                expires_at=datetime.utcnow() + timedelta(hours=1),
            )
        )
        await session.commit()

    request = _Request(
        _json_body={
            "tariff_key": "paid",
            "purchase_action": "upgrade",
            "target_order_id": "order:demo-order",
            "contact": "customer@example.com",
        },
        cookies={webstore_routes._AUTH_COOKIE: "demo-session-secret"},
        headers={"X-Real-IP": "127.0.0.1"},
        remote="127.0.0.1",
    )
    response = await webstore_routes.handle_create_order(request)

    assert response.status == 200
    payload = json.loads(response.text)
    assert payload["redirect_url"] == "https://pay.test/redirect"

    async with patched_webstore() as session:
        created = await session.scalar(
            select(WebOrder).where(WebOrder.order_id == payload["order_id"])
        )
        assert created is not None
        assert created.purchase_action == "upgrade"
        assert created.amount_rub == 200
        assert created.target_order_id == f"uuid:{demo_uuid}"
        assert created.target_snapshot_plan_uuid == demo_plan_uuid


async def test_apply_order_cashback_credits_buyer_balance(monkeypatch, patched_webstore):
    async with patched_webstore() as session:
        account = WebBalanceAccount(profile_token="cb_token", ref_code="ref_cb_1", balance_rub=0)
        session.add(account)
        order = WebOrder(
            order_id="cb_order_1",
            contact="user@test.com",
            tariff_key="basic",
            tariff_label="Базовый",
            days=30,
            amount_rub=155,
            status="delivered",
            profile_token="cb_token",
        )
        session.add(order)
        await session.commit()

        cashback = await webstore_routes._apply_order_cashback(session, order)
        assert cashback == 16  # 10% of 155 = 15.5 -> 16
        await session.commit()

        await session.refresh(account)
        assert account.balance_rub == 16

        tx = await session.scalar(
            select(WebBalanceTransaction).where(
                WebBalanceTransaction.profile_token == "cb_token",
                WebBalanceTransaction.kind == "cashback",
            )
        )
        assert tx is not None
        assert tx.amount_rub == 16
        assert tx.source_id == "cb_order_1"
        assert "Кешбэк 10%" in tx.description


async def test_apply_order_cashback_skips_when_no_real_money_paid(monkeypatch, patched_webstore):
    async with patched_webstore() as session:
        account = WebBalanceAccount(profile_token="cb_token2", ref_code="ref_cb_2", balance_rub=100)
        session.add(account)
        order = WebOrder(
            order_id="cb_order_2",
            contact="user2@test.com",
            tariff_key="basic",
            tariff_label="Базовый",
            days=30,
            amount_rub=0,
            status="delivered",
            profile_token="cb_token2",
        )
        session.add(order)
        await session.commit()

        cashback = await webstore_routes._apply_order_cashback(session, order)
        assert cashback == 0
        await session.commit()

        await session.refresh(account)
        assert account.balance_rub == 100

