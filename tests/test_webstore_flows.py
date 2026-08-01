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
        api.get_status = AsyncMock(return_value={"end_date": expires_at.isoformat(), "devices": 5})
        api.renew_subscription = AsyncMock()
        monkeypatch.setattr(webstore_routes, "AdaptAPI", lambda: api)

        await webstore_routes._fulfill_adapt_order(session, upgrade, "new-plan-uuid")

        assert upgrade.status == "delivered"
        assert upgrade.subscription_url == primary.subscription_url
        assert upgrade.marzban_username == primary.marzban_username
        api.upgrade_subscription.assert_awaited_once_with(adapt_uuid, "new-plan-uuid")
        api.renew_subscription.assert_not_awaited()


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
        api.get_status = AsyncMock(return_value={"end_date": new_end.isoformat(), "devices": 5})
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


async def test_web_profile_enriches_legacy_telegram_adapt_item(monkeypatch):
    adapt_uuid = "770fa622-a4bd-63f6-c938-668877662224"
    plan_uuid = "661f9511-f3ac-52e5-b827-557766552223"
    expires_at = datetime.utcnow() + timedelta(days=20)
    item = WebTelegramItem(
        profile_token="legacy-profile",
        telegram_id=123456,
        item_type="vpn",
        external_id="sub_9",
        title="VPN",
        key_value=f"https://test/adapt-sub/{adapt_uuid}",
        status="active",
    )
    api = AsyncMock()
    api.enabled = True
    api.get_status = AsyncMock(return_value={
        "plan_uuid": plan_uuid,
        "devices": 5,
        "end_date": expires_at.isoformat(),
    })
    monkeypatch.setattr(webstore_routes, "AdaptAPI", lambda: api)
    session = AsyncMock()

    await webstore_routes._enrich_legacy_telegram_adapt_items(session, [item])

    assert item.provider == "adapt"
    assert item.adapt_plan_uuid == plan_uuid
    assert item.device_slots == 5
    session.flush.assert_awaited_once()


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
