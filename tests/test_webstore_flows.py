from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from webstore import routes as webstore_routes
from webstore.models import Base, WebBalanceAccount, WebOrder, WebProfileLink, WebTelegramItem

pytestmark = pytest.mark.asyncio


class _Request(SimpleNamespace):
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
    monkeypatch.setattr(webstore_routes.settings, "bot_url", "https://t.me/uskoritelinternetabot")
    monkeypatch.setattr(webstore_routes.settings, "site_name", "Ускоритель интернета")
    monkeypatch.setattr(webstore_routes.settings, "bridge_shared_secret", "bridge-secret")
    monkeypatch.setattr(webstore_routes.settings, "telegram_link_ttl_minutes", 15)
    return db_session_factory


async def test_create_profile_returns_ref_link_and_demo_url(monkeypatch, patched_webstore):
    maybe_demo = AsyncMock(return_value="https://loonapie.xyz/s/demo-token")
    monkeypatch.setattr(webstore_routes, "_maybe_issue_web_demo_key", maybe_demo)
    monkeypatch.setattr(webstore_routes.settings, "demo_key_days", 3)

    request = _Request(
        _json_body={"contact": "@user"},
        cookies={},
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
    assert login_response.status == 302
    assert login_response.headers["Location"] == "/profile"
    assert login_response.cookies["webstore_profile_token"].value == profile_token

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

    async def _fulfill(order):
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
