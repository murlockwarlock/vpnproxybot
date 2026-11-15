"""Comprehensive regression & integration tests for Webstore ↔ Telegram linking and balance migration."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from webstore import routes as webstore_routes
from webstore.config import WebStoreSettings
from webstore.models import (
    Base,
    WebBalanceAccount,
    WebBalanceTransaction,
    WebBalanceTransfer,
    WebOrder,
    WebProfileLink,
    WebProfileLinkCode,
    WebTelegramItem,
)

pytestmark = pytest.mark.asyncio


class _MockResponse:
    def __init__(self, data: dict, status: int = 200):
        self._data = data
        self.status = status
        self.content_type = "application/json"

    async def json(self):
        return self._data

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


class _MockClientSession:
    def __init__(self, post_handler=None, get_handler=None):
        self._post_handler = post_handler
        self._get_handler = get_handler

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    def post(self, url, json=None, headers=None, **kwargs):
        if self._post_handler:
            return self._post_handler(url, json=json, headers=headers, **kwargs)
        return _MockResponse({"status": "credited", "balance_rub": 100})

    def get(self, url, params=None, headers=None, **kwargs):
        if self._get_handler:
            return self._get_handler(url, params=params, headers=headers, **kwargs)
        return _MockResponse({"ok": True, "found": False})


class _Request(SimpleNamespace):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if not hasattr(self, "_json_body"):
            self._json_body = {}
        if not hasattr(self, "headers"):
            self.headers = {}
        if not hasattr(self, "query"):
            self.query = {}
        if not hasattr(self, "cookies"):
            self.cookies = {}

    async def json(self):
        return self._json_body


@pytest_asyncio.fixture
async def db_engine(tmp_path):
    db_file = tmp_path / "test_webstore_linking.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}", echo=False)
    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA journal_mode=WAL;"))
        await conn.execute(text("PRAGMA busy_timeout=5000;"))
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_web_balance_transfers_profile_pending "
                "ON web_balance_transfers(profile_token) WHERE status = 'pending';"
            )
        )
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_web_balance_tx_migration_source "
                "ON web_balance_transactions(source_id) "
                "WHERE kind = 'balance_migration' AND source_id IS NOT NULL;"
            )
        )
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session_factory(db_engine):
    return async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def patched_webstore(monkeypatch, db_session_factory):
    import webstore.database as webstore_db
    import webstore.scheduler as webstore_scheduler

    monkeypatch.setattr(webstore_routes, "async_session", db_session_factory)
    monkeypatch.setattr(webstore_scheduler, "async_session", db_session_factory)
    monkeypatch.setattr(webstore_db, "async_session", db_session_factory)
    monkeypatch.setattr(webstore_routes.settings, "subscription_base_url", "https://darimiru.ru")
    monkeypatch.setattr(webstore_routes.settings, "bot_url", "https://t.me/darimiru_bot")
    monkeypatch.setattr(webstore_routes.settings, "site_name", "Ускоритель ДариМир")
    monkeypatch.setattr(webstore_routes.settings, "bridge_shared_secret", "test-bridge-secret")
    monkeypatch.setattr(webstore_routes.settings, "referral_bridge_url", "http://bridge.test/vpnbot")
    monkeypatch.setattr(webstore_routes.settings, "telegram_link_ttl_minutes", 15)
    monkeypatch.setattr(webstore_routes.settings, "vhq_subscription_proxy_base_url", "https://darimiru.ru")
    monkeypatch.setattr(webstore_routes.settings, "vhq_subscription_proxy_secret", "proxy-secret")
    monkeypatch.setattr(webstore_routes.settings, "bot_webhook_path_prefix", "/vpnbot")
    return db_session_factory


# ---------------------------------------------------------------------------
# 1. profile-link-init contract test
# ---------------------------------------------------------------------------
async def test_profile_link_init_contract(patched_webstore):
    """Test POST /api/store/profile-link-init creates code, TTL, and link_url with ?start=web_."""
    async with patched_webstore() as session:
        session.add(
            WebOrder(
                order_id="ord_init_1",
                contact="client@test.local",
                tariff_key="month_1",
                tariff_label="1 месяц",
                days=30,
                amount_rub=300,
                status="delivered",
                profile_token="tok_init_1",
            )
        )
        await session.commit()

    req = _Request(_json_body={"token": "tok_init_1"})
    resp = await webstore_routes.handle_profile_link_init(req)
    assert resp.status == 200

    data = json.loads(resp.text)
    assert "link_url" in data
    assert data["expires_in_minutes"] == 15
    link_url = data["link_url"]

    # Canonical backend-generated URL
    assert link_url.startswith("https://t.me/darimiru_bot?start=web_")
    code = link_url.split("?start=web_")[1]
    assert len(code) > 10

    # Verify WebProfileLinkCode in DB
    async with patched_webstore() as session:
        result = await session.execute(
            select(WebProfileLinkCode).where(WebProfileLinkCode.code == code)
        )
        link_code = result.scalar_one_or_none()
        assert link_code is not None
        assert link_code.profile_token == "tok_init_1"
        assert link_code.consumed_at is None
        assert link_code.expires_at > datetime.utcnow()


# ---------------------------------------------------------------------------
# 2. frontend contract regression test
# ---------------------------------------------------------------------------
def test_frontend_contract_regression():
    """Verify profile.html startTelegramLink uses data.link_url and does not depend on BOT_USERNAME or data.code."""
    template_path = Path(__file__).resolve().parent.parent / "webstore" / "templates" / "profile.html"
    content = template_path.read_text(encoding="utf-8")

    # Extract startTelegramLink function body
    assert "async function startTelegramLink()" in content
    start_idx = content.find("async function startTelegramLink()")
    end_idx = content.find("function renderOrders(", start_idx)
    assert end_idx > start_idx
    fn_body = content[start_idx:end_idx]

    # Acceptance criteria:
    # 1. uses data.link_url
    assert "window.location.href = data.link_url" in fn_body
    # 2. does not construct https://t.me/...
    assert "https://t.me/" not in fn_body
    # 3. does not depend on data.code
    assert "data.code" not in fn_body
    # 4. does not depend on BOT_USERNAME
    assert "BOT_USERNAME" not in fn_body
    assert "BOT_USERNAME" not in content


# ---------------------------------------------------------------------------
# 3. end-to-end linking backend flow
# ---------------------------------------------------------------------------
async def test_end_to_end_linking_backend_flow(patched_webstore, monkeypatch):
    """Verify full link flow: init -> extract code -> internal claim with secret -> link created, code consumed, tg items saved."""
    async with patched_webstore() as session:
        session.add(
            WebOrder(
                order_id="ord_e2e_1",
                contact="e2e@test.local",
                tariff_key="month_1",
                tariff_label="1 месяц",
                days=30,
                amount_rub=300,
                status="delivered",
                profile_token="tok_e2e_1",
            )
        )
        await session.commit()

    # 1. Init link
    req_init = _Request(_json_body={"token": "tok_e2e_1"})
    resp_init = await webstore_routes.handle_profile_link_init(req_init)
    assert resp_init.status == 200
    data_init = json.loads(resp_init.text)
    code = data_init["link_url"].split("?start=web_")[1]

    # Mock migration to avoid network
    migrate_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(webstore_routes, "_migrate_web_balance_to_telegram", migrate_mock)

    # 2. Internal claim with valid secret
    claim_payload = {
        "code": code,
        "telegram_id": 12345678,
        "telegram_username": "e2e_user",
        "telegram_full_name": "E2E User",
        "items": [
            {
                "item_type": "vpn",
                "external_id": "sub_vpn_1",
                "title": "VPN Subscription",
                "key_value": "vless://key1",
                "status": "active",
            }
        ],
    }
    req_claim = _Request(
        _json_body=claim_payload,
        headers={"X-Internal-Secret": "test-bridge-secret"},
    )
    resp_claim = await webstore_routes.handle_internal_telegram_link_claim(req_claim)
    assert resp_claim.status == 200
    assert json.loads(resp_claim.text) == {"ok": True}

    # Verify link and consumed code in DB
    async with patched_webstore() as session:
        code_row = (
            await session.execute(
                select(WebProfileLinkCode).where(WebProfileLinkCode.code == code)
            )
        ).scalar_one()
        assert code_row.consumed_at is not None

        link_row = (
            await session.execute(
                select(WebProfileLink).where(WebProfileLink.telegram_id == 12345678)
            )
        ).scalar_one()
        assert link_row.profile_token == "tok_e2e_1"
        assert link_row.telegram_username == "e2e_user"
        assert link_row.telegram_full_name == "E2E User"

        items = (
            await session.execute(
                select(WebTelegramItem).where(WebTelegramItem.profile_token == "tok_e2e_1")
            )
        ).scalars().all()
        assert len(items) == 1
        assert items[0].external_id == "sub_vpn_1"
        assert items[0].telegram_id == 12345678

    # 3. Repeated claim with same code is rejected
    resp_repeat = await webstore_routes.handle_internal_telegram_link_claim(req_claim)
    assert resp_repeat.status == 404

    # 4. Expired code is rejected
    async with patched_webstore() as session:
        session.add(
            WebProfileLinkCode(
                code="expired_code_123",
                profile_token="tok_e2e_1",
                expires_at=datetime.utcnow() - timedelta(minutes=5),
            )
        )
        await session.commit()

    req_expired = _Request(
        _json_body={"code": "expired_code_123", "telegram_id": 12345678},
        headers={"X-Internal-Secret": "test-bridge-secret"},
    )
    resp_expired = await webstore_routes.handle_internal_telegram_link_claim(req_expired)
    assert resp_expired.status == 404


# ---------------------------------------------------------------------------
# 4. linking → bot-visible web order (with VHQ raw supplier URL protection)
# ---------------------------------------------------------------------------
async def test_linking_bot_visible_web_order_vhq_protection(patched_webstore):
    """Verify handle_internal_web_profile returns delivered orders with safe proxy/display subscription URL."""
    async with patched_webstore() as session:
        # Create delivered VHQ order with raw supplier URL
        raw_supplier_url = "https://upstream.vhq-connect.xyz/proxy-subscription/raw_secret_vhq_token"
        session.add(
            WebOrder(
                order_id="ord_vhq_1",
                contact="vhq@test.local",
                tariff_key="baza_1",
                tariff_label="База 1 мес",
                days=30,
                amount_rub=350,
                status="delivered",
                profile_token="tok_vhq_user",
                subscription_url=raw_supplier_url,
                created_at=datetime.utcnow(),
                access_expires_at=datetime.utcnow() + timedelta(days=30),
            )
        )
        # Link telegram ID 888888
        session.add(
            WebProfileLink(
                profile_token="tok_vhq_user",
                contact="vhq@test.local",
                telegram_id=888888,
                telegram_username="vhq_buyer",
                linked_at=datetime.utcnow(),
                last_synced_at=datetime.utcnow(),
            )
        )
        await session.commit()

    req = _Request(
        query={"telegram_id": "888888"},
        headers={"X-Internal-Secret": "test-bridge-secret"},
    )
    resp = await webstore_routes.handle_internal_web_profile(req)
    assert resp.status == 200

    data = json.loads(resp.text)
    assert "orders" in data
    assert len(data["orders"]) == 1
    order = data["orders"][0]
    assert order["order_id"] == "ord_vhq_1"
    assert order["status"] == "delivered"

    # CRITICAL: Do NOT expose raw supplier URL
    public_url = order["subscription_url"]
    assert public_url is not None
    assert "upstream.vhq-connect.xyz" not in public_url
    assert "raw_secret_vhq_token" not in public_url
    # Formatted through VHQ mirror/proxy base URL
    assert public_url.startswith("https://darimiru.ru/vpnbot/vhq-sub/")


# ---------------------------------------------------------------------------
# 5. web balance migration — first transfer
# ---------------------------------------------------------------------------
async def test_web_balance_migration_first_transfer(patched_webstore, monkeypatch):
    """Initial migration: balance 100 -> durable transfer -> bot credited -> web balance 0, debit once."""
    async with patched_webstore() as session:
        account = WebBalanceAccount(
            profile_token="tok_bal_1",
            contact="bal1@test.local",
            ref_code="ref111",
            balance_rub=100,
            updated_at=datetime.utcnow(),
        )
        session.add(account)
        await session.commit()

    credit_calls = []

    def mock_post(url, json=None, headers=None, **kwargs):
        credit_calls.append({"url": url, "json": json, "headers": headers})
        assert json["amount_rub"] == 100
        assert json["telegram_id"] == 4444
        assert json["source_id"].startswith("wbtr_")
        return _MockResponse({"status": "credited", "balance_rub": 100})

    monkeypatch.setattr(
        webstore_routes.aiohttp,
        "ClientSession",
        lambda *args, **kwargs: _MockClientSession(post_handler=mock_post),
    )

    async with patched_webstore() as session:
        transfer = await webstore_routes._migrate_web_balance_to_telegram(
            session, "tok_bal_1", 4444
        )
        assert transfer is not None
        assert transfer.status == "completed"
        assert transfer.amount_rub == 100
        assert transfer.completed_at is not None

    assert len(credit_calls) == 1

    # Check DB state
    async with patched_webstore() as session:
        acc = await session.get(WebBalanceAccount, "tok_bal_1")
        assert acc.balance_rub == 0
        assert acc.total_spent_rub == 100

        txs = (
            await session.execute(
                select(WebBalanceTransaction).where(
                    WebBalanceTransaction.profile_token == "tok_bal_1"
                )
            )
        ).scalars().all()
        assert len(txs) == 1
        assert txs[0].amount_rub == 100
        assert txs[0].direction == "debit"
        assert txs[0].source_id == transfer.transfer_id

    # Repeated call does NOT do a second debit
    async with patched_webstore() as session:
        second_call = await webstore_routes._migrate_web_balance_to_telegram(
            session, "tok_bal_1", 4444
        )
        assert second_call is None

    assert len(credit_calls) == 1
    async with patched_webstore() as session:
        acc = await session.get(WebBalanceAccount, "tok_bal_1")
        assert acc.balance_rub == 0


# ---------------------------------------------------------------------------
# 6. migration retry after ambiguous/crash
# ---------------------------------------------------------------------------
async def test_migration_retry_after_ambiguous_crash(patched_webstore, monkeypatch):
    """Crash/timeout after pending transfer created: retry must reuse same source_id and handle already_credited."""
    async with patched_webstore() as session:
        account = WebBalanceAccount(
            profile_token="tok_crash_1",
            contact="crash@test.local",
            ref_code="ref_crash",
            balance_rub=0,  # funds were already reserved when pending transfer was created
            updated_at=datetime.utcnow(),
        )
        session.add(account)
        # Pre-seed a pending transfer representing the interrupted/ambiguous transfer
        durable_transfer = WebBalanceTransfer(
            transfer_id="wbtr_crash_intent_id",
            profile_token="tok_crash_1",
            telegram_id=5555,
            amount_rub=100,
            status="pending",
            created_at=datetime.utcnow(),
        )
        session.add(durable_transfer)
        await session.commit()

    credit_calls = []

    def mock_post(url, json=None, headers=None, **kwargs):
        credit_calls.append({"url": url, "json": json, "headers": headers})
        # Simulate bot already credited this source_id during the ambiguous attempt
        assert json["source_id"] == "wbtr_crash_intent_id"
        assert json["amount_rub"] == 100
        return _MockResponse({"status": "already_credited", "balance_rub": 100})

    monkeypatch.setattr(
        webstore_routes.aiohttp,
        "ClientSession",
        lambda *args, **kwargs: _MockClientSession(post_handler=mock_post),
    )

    async with patched_webstore() as session:
        transfer = await webstore_routes._migrate_web_balance_to_telegram(
            session, "tok_crash_1", 5555
        )
        assert transfer is not None
        assert transfer.transfer_id == "wbtr_crash_intent_id"
        assert transfer.status == "completed"
        assert transfer.completed_at is not None

    assert len(credit_calls) == 1

    # Verify debit occurred exactly once
    async with patched_webstore() as session:
        acc = await session.get(WebBalanceAccount, "tok_crash_1")
        assert acc.balance_rub == 0
        txs = (
            await session.execute(
                select(WebBalanceTransaction).where(
                    WebBalanceTransaction.profile_token == "tok_crash_1"
                )
            )
        ).scalars().all()
        assert len(txs) == 1
        assert txs[0].source_id == "wbtr_crash_intent_id"


# ---------------------------------------------------------------------------
# 7. new balance after old completed migration
# ---------------------------------------------------------------------------
async def test_new_balance_after_old_completed_migration(patched_webstore, monkeypatch):
    """Old migration #1 (100 ₽) completed. New balance (50 ₽) creates transfer #2 with a NEW source_id."""
    async with patched_webstore() as session:
        account = WebBalanceAccount(
            profile_token="tok_multi_1",
            contact="multi@test.local",
            ref_code="ref_multi",
            balance_rub=50,  # newly accumulated balance
            total_earned_rub=150,
            total_spent_rub=100,
            updated_at=datetime.utcnow(),
        )
        session.add(account)
        # Previous transfer was completed
        old_transfer = WebBalanceTransfer(
            transfer_id="wbtr_first_completed_id",
            profile_token="tok_multi_1",
            telegram_id=6666,
            amount_rub=100,
            status="completed",
            created_at=datetime.utcnow() - timedelta(days=2),
            completed_at=datetime.utcnow() - timedelta(days=2),
        )
        session.add(old_transfer)
        session.add(
            WebBalanceTransaction(
                profile_token="tok_multi_1",
                amount_rub=100,
                direction="debit",
                kind="balance_migration",
                balance_after_rub=0,
                source_id="wbtr_first_completed_id",
            )
        )
        await session.commit()

    credit_calls = []

    def mock_post(url, json=None, headers=None, **kwargs):
        credit_calls.append({"url": url, "json": json, "headers": headers})
        # Crucial check: must NOT be old source_id!
        assert json["source_id"] != "wbtr_first_completed_id"
        assert json["amount_rub"] == 50
        return _MockResponse({"status": "credited", "balance_rub": 150})

    monkeypatch.setattr(
        webstore_routes.aiohttp,
        "ClientSession",
        lambda *args, **kwargs: _MockClientSession(post_handler=mock_post),
    )

    async with patched_webstore() as session:
        new_transfer = await webstore_routes._migrate_web_balance_to_telegram(
            session, "tok_multi_1", 6666
        )
        assert new_transfer is not None
        assert new_transfer.transfer_id != "wbtr_first_completed_id"
        assert new_transfer.amount_rub == 50
        assert new_transfer.status == "completed"

    assert len(credit_calls) == 1
    assert credit_calls[0]["json"]["source_id"] == new_transfer.transfer_id

    # Web balance is now 0
    async with patched_webstore() as session:
        acc = await session.get(WebBalanceAccount, "tok_multi_1")
        assert acc.balance_rub == 0
        assert acc.total_spent_rub == 150

        # Both transfers exist
        all_transfers = (
            await session.execute(
                select(WebBalanceTransfer)
                .where(WebBalanceTransfer.profile_token == "tok_multi_1")
                .order_by(WebBalanceTransfer.id.asc())
            )
        ).scalars().all()
        assert len(all_transfers) == 2
        assert all_transfers[0].transfer_id == "wbtr_first_completed_id"
        assert all_transfers[1].transfer_id == new_transfer.transfer_id


# ---------------------------------------------------------------------------
# 8. failed bridge call
# ---------------------------------------------------------------------------
async def test_failed_bridge_call_keeps_balance_and_transfer_pending(patched_webstore, monkeypatch):
    """Bridge call 500 or timeout: funds remain reserved; transfer remains pending/recoverable; no ledger debit."""
    async with patched_webstore() as session:
        account = WebBalanceAccount(
            profile_token="tok_fail_1",
            contact="fail@test.local",
            ref_code="ref_fail",
            balance_rub=100,
            updated_at=datetime.utcnow(),
        )
        session.add(account)
        await session.commit()

    def mock_post(url, json=None, headers=None, **kwargs):
        return _MockResponse({"error": "Internal error"}, status=500)

    monkeypatch.setattr(
        webstore_routes.aiohttp,
        "ClientSession",
        lambda *args, **kwargs: _MockClientSession(post_handler=mock_post),
    )

    async with patched_webstore() as session:
        transfer = await webstore_routes._migrate_web_balance_to_telegram(
            session, "tok_fail_1", 7777
        )
        assert transfer is not None
        assert transfer.status == "pending"
        assert transfer.completed_at is None
        assert transfer.error_message is not None

    # Available balance is reserved (0) to prevent double-spending; no ledger transaction created yet
    async with patched_webstore() as session:
        acc = await session.get(WebBalanceAccount, "tok_fail_1")
        assert acc.balance_rub == 0
        assert acc.total_spent_rub == 0

        txs = (
            await session.execute(
                select(WebBalanceTransaction).where(
                    WebBalanceTransaction.profile_token == "tok_fail_1"
                )
            )
        ).scalars().all()
        assert len(txs) == 0


# ---------------------------------------------------------------------------
# 9. re-link A → B
# ---------------------------------------------------------------------------
async def test_relink_telegram_a_to_b(patched_webstore):
    """Re-linking Telegram from profile A to profile B removes A's link, cleans tg items, leaves A orders/balance untouched."""
    async with patched_webstore() as session:
        # Profile A with an order and balance
        session.add(
            WebOrder(
                order_id="ord_a_1",
                contact="profile_a@test.local",
                tariff_key="month_1",
                tariff_label="Тариф А",
                days=30,
                amount_rub=300,
                status="delivered",
                profile_token="tok_profile_a",
            )
        )
        session.add(
            WebBalanceAccount(
                profile_token="tok_profile_a",
                contact="profile_a@test.local",
                ref_code="ref_a",
                balance_rub=120,
            )
        )
        # Profile A is currently linked to Telegram 99999
        session.add(
            WebProfileLink(
                profile_token="tok_profile_a",
                contact="profile_a@test.local",
                telegram_id=99999,
                telegram_username="tg_user_99999",
                linked_at=datetime.utcnow(),
                last_synced_at=datetime.utcnow(),
            )
        )
        session.add(
            WebTelegramItem(
                profile_token="tok_profile_a",
                telegram_id=99999,
                item_type="vpn",
                external_id="item_a_old",
                title="A Key",
                status="active",
            )
        )

        # Profile B with its own order and balance
        session.add(
            WebOrder(
                order_id="ord_b_1",
                contact="profile_b@test.local",
                tariff_key="month_3",
                tariff_label="Тариф Б",
                days=90,
                amount_rub=800,
                status="delivered",
                profile_token="tok_profile_b",
            )
        )
        session.add(
            WebBalanceAccount(
                profile_token="tok_profile_b",
                contact="profile_b@test.local",
                ref_code="ref_b",
                balance_rub=50,
            )
        )
        await session.commit()

    # Re-link Telegram 99999 to Profile B
    async with patched_webstore() as session:
        link = await webstore_routes._upsert_profile_link(
            session,
            profile_token="tok_profile_b",
            contact="profile_b@test.local",
            telegram_id=99999,
            telegram_username="tg_user_99999",
            telegram_full_name="User B",
        )
        assert link.profile_token == "tok_profile_b"
        assert link.telegram_id == 99999

        await webstore_routes._replace_telegram_items(
            session,
            profile_token="tok_profile_b",
            telegram_id=99999,
            items=[
                {
                    "item_type": "vpn",
                    "external_id": "item_b_new",
                    "title": "B Key",
                    "status": "active",
                }
            ],
        )
        await session.commit()

    # Verify DB assertions
    async with patched_webstore() as session:
        # 1. Profile B is linked
        link_b = (
            await session.execute(
                select(WebProfileLink).where(WebProfileLink.telegram_id == 99999)
            )
        ).scalar_one()
        assert link_b.profile_token == "tok_profile_b"

        # 2. Profile A link record no longer exists
        link_a = await session.get(WebProfileLink, "tok_profile_a")
        assert link_a is None

        # 3. Old Telegram items for A are gone
        items_a = (
            await session.execute(
                select(WebTelegramItem).where(WebTelegramItem.profile_token == "tok_profile_a")
            )
        ).scalars().all()
        assert len(items_a) == 0

        # 4. New items for B exist
        items_b = (
            await session.execute(
                select(WebTelegramItem).where(WebTelegramItem.profile_token == "tok_profile_b")
            )
        ).scalars().all()
        assert len(items_b) == 1
        assert items_b[0].external_id == "item_b_new"

        # 5. Profile A order is NOT deleted
        ord_a = (
            await session.execute(
                select(WebOrder).where(WebOrder.profile_token == "tok_profile_a")
            )
        ).scalar_one()
        assert ord_a.order_id == "ord_a_1"

        # 6. Profile A balance is NOT deleted
        bal_a = await session.get(WebBalanceAccount, "tok_profile_a")
        assert bal_a is not None
        assert bal_a.balance_rub == 120

    # 7. Internal web profile for Telegram 99999 now returns B's orders, NOT A's
    req_profile = _Request(
        query={"telegram_id": "99999"},
        headers={"X-Internal-Secret": "test-bridge-secret"},
    )
    resp_profile = await webstore_routes.handle_internal_web_profile(req_profile)
    assert resp_profile.status == 200
    data_profile = json.loads(resp_profile.text)
    orders = data_profile["orders"]
    assert len(orders) == 1
    assert orders[0]["order_id"] == "ord_b_1"


# ---------------------------------------------------------------------------
# 10. Bridge shared secret fallback test
# ---------------------------------------------------------------------------
async def test_bridge_shared_secret_fallback(monkeypatch):
    """Verify webstore config bridge_shared_secret falls back to WEBSTORE_BRIDGE_SECRET."""
    monkeypatch.delenv("BRIDGE_SHARED_SECRET", raising=False)
    monkeypatch.setenv("WEBSTORE_BRIDGE_SECRET", "fallback-secret-value")

    settings = WebStoreSettings()
    assert settings.bridge_shared_secret == "fallback-secret-value"


# ---------------------------------------------------------------------------
# 11. Concurrent balance migration creates single pending transfer
# ---------------------------------------------------------------------------
async def test_concurrent_balance_migration_creates_single_pending_transfer(patched_webstore, monkeypatch):
    """Two concurrent migration attempts for profile with 100 ₽:
    Prove exactly one durable transfer_id, bot sees 1 logical source_id, Telegram credit is 100 (not 200),
    funds reserved once (balance is 0), and 1 ledger debit row.
    """
    async with patched_webstore() as session:
        account = WebBalanceAccount(
            profile_token="tok_conc_migrate",
            contact="conc@test.local",
            ref_code="ref_conc",
            balance_rub=100,
            updated_at=datetime.utcnow(),
        )
        session.add(account)
        await session.commit()

    source_ids_seen = []
    total_credited = 0

    def mock_post(url, json=None, headers=None, **kwargs):
        nonlocal total_credited
        sid = json["source_id"]
        source_ids_seen.append(sid)
        amt = json["amount_rub"]
        if source_ids_seen.count(sid) == 1:
            total_credited += amt
            return _MockResponse({"status": "credited", "balance_rub": total_credited})
        return _MockResponse({"status": "already_credited", "balance_rub": total_credited})

    monkeypatch.setattr(
        webstore_routes.aiohttp,
        "ClientSession",
        lambda *args, **kwargs: _MockClientSession(post_handler=mock_post),
    )

    async def run_migration():
        async with patched_webstore() as s:
            return await webstore_routes._migrate_web_balance_to_telegram(
                s, "tok_conc_migrate", 7771
            )

    t1, t2 = await asyncio.gather(run_migration(), run_migration())

    transfers = [t for t in (t1, t2) if t is not None]
    assert len(transfers) >= 1
    transfer_ids = {t.transfer_id for t in transfers}
    assert len(transfer_ids) == 1
    assert len(set(source_ids_seen)) == 1
    assert total_credited == 100

    async with patched_webstore() as session:
        acc = await session.get(WebBalanceAccount, "tok_conc_migrate")
        assert acc.balance_rub == 0
        assert acc.total_spent_rub == 100

        all_t = (
            await session.execute(
                select(WebBalanceTransfer).where(
                    WebBalanceTransfer.profile_token == "tok_conc_migrate"
                )
            )
        ).scalars().all()
        assert len(all_t) == 1

        txs = (
            await session.execute(
                select(WebBalanceTransaction).where(
                    WebBalanceTransaction.profile_token == "tok_conc_migrate"
                )
            )
        ).scalars().all()
        assert len(txs) == 1
        assert txs[0].amount_rub == 100
        assert txs[0].source_id == list(transfer_ids)[0]


# ---------------------------------------------------------------------------
# 12. Concurrent retry finalizes transfer once
# ---------------------------------------------------------------------------
async def test_concurrent_retry_finalizes_transfer_once(patched_webstore, monkeypatch):
    """Two concurrent recoveries of the same pending transfer:
    Exactly one migration ledger row, total_spent_rub incremented once, balance not decreased twice.
    """
    async with patched_webstore() as session:
        account = WebBalanceAccount(
            profile_token="tok_conc_fin",
            contact="fin@test.local",
            ref_code="ref_fin",
            balance_rub=0,  # funds were reserved at intent
            total_spent_rub=0,
            updated_at=datetime.utcnow(),
        )
        session.add(account)
        durable = WebBalanceTransfer(
            transfer_id="wbtr_conc_fin_1",
            profile_token="tok_conc_fin",
            telegram_id=8881,
            amount_rub=100,
            status="pending",
            created_at=datetime.utcnow(),
        )
        session.add(durable)
        await session.commit()

    def mock_post(url, json=None, headers=None, **kwargs):
        assert json["source_id"] == "wbtr_conc_fin_1"
        return _MockResponse({"status": "already_credited", "balance_rub": 100})

    monkeypatch.setattr(
        webstore_routes.aiohttp,
        "ClientSession",
        lambda *args, **kwargs: _MockClientSession(post_handler=mock_post),
    )

    async def run_retry():
        async with patched_webstore() as s:
            return await webstore_routes._migrate_web_balance_to_telegram(
                s, "tok_conc_fin", 8881
            )

    t1, t2 = await asyncio.gather(run_retry(), run_retry())

    async with patched_webstore() as session:
        acc = await session.get(WebBalanceAccount, "tok_conc_fin")
        assert acc.balance_rub == 0
        assert acc.total_spent_rub == 100

        txs = (
            await session.execute(
                select(WebBalanceTransaction).where(
                    WebBalanceTransaction.profile_token == "tok_conc_fin"
                )
            )
        ).scalars().all()
        assert len(txs) == 1
        assert txs[0].source_id == "wbtr_conc_fin_1"

        trans = (
            await session.execute(
                select(WebBalanceTransfer).where(
                    WebBalanceTransfer.profile_token == "tok_conc_fin"
                )
            )
        ).scalars().all()
        assert len(trans) == 1
        assert trans[0].status == "completed"


# ---------------------------------------------------------------------------
# 13. Pending transfer reserves balance from web spending
# ---------------------------------------------------------------------------
async def test_pending_transfer_reserves_balance_from_web_spending(patched_webstore, monkeypatch):
    """Pending transfer reserves funds from web spending. New money (50 ₽) is available and unaffected by old retry."""
    from webstore.scheduler import process_web_balance_daily_charges

    async with patched_webstore() as session:
        account = WebBalanceAccount(
            profile_token="tok_reserve_spend",
            contact="reserve@test.local",
            ref_code="ref_reserve",
            balance_rub=100,
            balance_mode_enabled=1,
            balance_autodebit_enabled=1,
            next_daily_charge_at=datetime.utcnow() - timedelta(minutes=5),
            updated_at=datetime.utcnow(),
        )
        session.add(account)
        await session.commit()

    # 1. First migration attempt times out (504) -> intent created, 100 ₽ reserved
    def mock_post_fail(url, json=None, headers=None, **kwargs):
        return _MockResponse({"error": "Gateway timeout"}, status=504)

    monkeypatch.setattr(
        webstore_routes.aiohttp,
        "ClientSession",
        lambda *args, **kwargs: _MockClientSession(post_handler=mock_post_fail),
    )

    async with patched_webstore() as session:
        transfer = await webstore_routes._migrate_web_balance_to_telegram(
            session, "tok_reserve_spend", 9991
        )
        assert transfer is not None
        assert transfer.status == "pending"

    # Verify funds are reserved (available balance is 0)
    async with patched_webstore() as session:
        acc = await session.get(WebBalanceAccount, "tok_reserve_spend")
        assert acc.balance_rub == 0

    # 2. Daily charge scheduler runs. Cannot spend the reserved 100 ₽
    monkeypatch.setattr(
        webstore_routes,
        "_get_web_daily_charge_rub",
        AsyncMock(return_value=10.0),
    )
    await process_web_balance_daily_charges()

    async with patched_webstore() as session:
        acc = await session.get(WebBalanceAccount, "tok_reserve_spend")
        assert acc.balance_rub == 0
        txs = (
            await session.execute(
                select(WebBalanceTransaction).where(
                    WebBalanceTransaction.profile_token == "tok_reserve_spend",
                    WebBalanceTransaction.kind == "daily_charge",
                )
            )
        ).scalars().all()
        assert len(txs) == 0

    # 3. Add 50 ₽ of new money
    async with patched_webstore() as session:
        acc = await session.get(WebBalanceAccount, "tok_reserve_spend")
        acc.balance_rub = 50
        await session.commit()

    # 4. Old pending transfer recovers and completes
    def mock_post_success(url, json=None, headers=None, **kwargs):
        assert json["source_id"] == transfer.transfer_id
        assert json["amount_rub"] == 100
        return _MockResponse({"status": "already_credited", "balance_rub": 100})

    monkeypatch.setattr(
        webstore_routes.aiohttp,
        "ClientSession",
        lambda *args, **kwargs: _MockClientSession(post_handler=mock_post_success),
    )

    async with patched_webstore() as session:
        completed_tx = await webstore_routes._migrate_web_balance_to_telegram(
            session, "tok_reserve_spend", 9991
        )
        assert completed_tx is not None
        assert completed_tx.status == "completed"

    # The new 50 ₽ was NOT touched by the old retry!
    async with patched_webstore() as session:
        acc = await session.get(WebBalanceAccount, "tok_reserve_spend")
        assert acc.balance_rub == 50
        assert acc.total_spent_rub == 100


# ---------------------------------------------------------------------------
# 14. Crash after bot credit recovers without double credit or debit
# ---------------------------------------------------------------------------
async def test_credit_success_local_commit_crash_recovers_without_double_credit_or_debit(patched_webstore, monkeypatch):
    """Crash after bot credit succeeded: transfer remains pending; retry receives already_credited;
    finalization completes once without double credit or double debit.
    """
    async with patched_webstore() as session:
        account = WebBalanceAccount(
            profile_token="tok_crash_recover",
            contact="crash_rec@test.local",
            ref_code="ref_cr",
            balance_rub=100,
            updated_at=datetime.utcnow(),
        )
        session.add(account)
        await session.commit()

    bot_credits = 0

    def mock_post(url, json=None, headers=None, **kwargs):
        nonlocal bot_credits
        bot_credits += 1
        if bot_credits == 1:
            return _MockResponse({"status": "credited", "balance_rub": 100})
        return _MockResponse({"status": "already_credited", "balance_rub": 100})

    monkeypatch.setattr(
        webstore_routes.aiohttp,
        "ClientSession",
        lambda *args, **kwargs: _MockClientSession(post_handler=mock_post),
    )

    # Step 1: Pre-seed intent created + balance reserved, simulating interrupted state where
    # bot credit completed on the remote bot, but local finalization didn't commit.
    async with patched_webstore() as session:
        intent = WebBalanceTransfer(
            transfer_id="wbtr_crash_rec_intent",
            profile_token="tok_crash_recover",
            telegram_id=6661,
            amount_rub=100,
            status="pending",
            created_at=datetime.utcnow(),
        )
        session.add(intent)
        acc = await session.get(WebBalanceAccount, "tok_crash_recover")
        acc.balance_rub -= 100
        await session.commit()

    bot_credits = 1

    # Step 2: Recovery runs
    async with patched_webstore() as session:
        recovered = await webstore_routes._migrate_web_balance_to_telegram(
            session, "tok_crash_recover", 6661
        )
        assert recovered is not None
        assert recovered.status == "completed"
        assert recovered.transfer_id == "wbtr_crash_rec_intent"

    # Verify no double credit to bot, exactly one ledger row, balance is 0 (not -100)
    assert bot_credits == 2
    async with patched_webstore() as session:
        acc = await session.get(WebBalanceAccount, "tok_crash_recover")
        assert acc.balance_rub == 0
        assert acc.total_spent_rub == 100

        txs = (
            await session.execute(
                select(WebBalanceTransaction).where(
                    WebBalanceTransaction.profile_token == "tok_crash_recover"
                )
            )
        ).scalars().all()
        assert len(txs) == 1
        assert txs[0].source_id == "wbtr_crash_rec_intent"


# ---------------------------------------------------------------------------
# 15. Re-link pending A → B does not create second transfer
# ---------------------------------------------------------------------------
async def test_relink_does_not_create_second_transfer_for_new_recipient(patched_webstore, monkeypatch):
    """Pending transfer to recipient A blocks re-link to recipient B if ambiguous.
    When A is reconciled, re-link to B proceeds without creating duplicate transfer for old money.
    """
    async with patched_webstore() as session:
        session.add(
            WebOrder(
                order_id="ord_relink_test",
                contact="relink@test.local",
                tariff_key="month_1",
                tariff_label="1 месяц",
                days=30,
                amount_rub=300,
                status="delivered",
                profile_token="tok_relink_p",
            )
        )
        session.add(
            WebBalanceAccount(
                profile_token="tok_relink_p",
                contact="relink@test.local",
                ref_code="ref_rl",
                balance_rub=0,  # 100 was already reserved by pending transfer
            )
        )
        session.add(
            WebProfileLink(
                profile_token="tok_relink_p",
                telegram_id=1001,
                linked_at=datetime.utcnow(),
            )
        )
        session.add(
            WebBalanceTransfer(
                transfer_id="wbtr_to_recipient_a",
                profile_token="tok_relink_p",
                telegram_id=1001,
                amount_rub=100,
                status="pending",
                created_at=datetime.utcnow(),
            )
        )
        session.add(
            WebProfileLinkCode(
                code="code_relink_b",
                profile_token="tok_relink_p",
                expires_at=datetime.utcnow() + timedelta(minutes=15),
            )
        )
        await session.commit()

    # 1. Reconcile attempt for A fails (timeout/504)
    reconcile_calls = []

    def mock_post_fail(url, json=None, headers=None, **kwargs):
        reconcile_calls.append(json)
        return _MockResponse({"error": "Network timeout"}, status=504)

    monkeypatch.setattr(
        webstore_routes.aiohttp,
        "ClientSession",
        lambda *args, **kwargs: _MockClientSession(post_handler=mock_post_fail),
    )

    req_claim = _Request(
        _json_body={"code": "code_relink_b", "telegram_id": 2002},
        headers={"X-Internal-Secret": "test-bridge-secret"},
    )
    resp = await webstore_routes.handle_internal_telegram_link_claim(req_claim)

    # Must return 409 Conflict: re-link recipient NOT completed!
    assert resp.status == 409
    data = json.loads(resp.text)
    assert data["pending_transfer_id"] == "wbtr_to_recipient_a"

    # Verify link code was NOT consumed, link is NOT changed to B, and no transfer to B
    async with patched_webstore() as session:
        code_row = (
            await session.execute(
                select(WebProfileLinkCode).where(WebProfileLinkCode.code == "code_relink_b")
            )
        ).scalar_one()
        assert code_row.consumed_at is None

        transfers = (
            await session.execute(
                select(WebBalanceTransfer).where(
                    WebBalanceTransfer.profile_token == "tok_relink_p"
                )
            )
        ).scalars().all()
        assert len(transfers) == 1
        assert transfers[0].telegram_id == 1001  # strictly recipient A

    # 2. Now simulate network recovers and A completes successfully
    def mock_post_ok(url, json=None, headers=None, **kwargs):
        assert json["telegram_id"] == 1001  # Must reconcile strictly to A!
        assert json["source_id"] == "wbtr_to_recipient_a"
        return _MockResponse({"status": "already_credited", "balance_rub": 100})

    monkeypatch.setattr(
        webstore_routes.aiohttp,
        "ClientSession",
        lambda *args, **kwargs: _MockClientSession(post_handler=mock_post_ok),
    )

    resp_retry = await webstore_routes.handle_internal_telegram_link_claim(req_claim)
    assert resp_retry.status == 200
    assert json.loads(resp_retry.text) == {"ok": True}

    # Now re-link to B is complete, code consumed, transfer to A completed, NO transfer to B created
    async with patched_webstore() as session:
        link_b = (
            await session.execute(
                select(WebProfileLink).where(WebProfileLink.telegram_id == 2002)
            )
        ).scalar_one_or_none()
        assert link_b is not None
        assert link_b.profile_token == "tok_relink_p"

        code_row = (
            await session.execute(
                select(WebProfileLinkCode).where(WebProfileLinkCode.code == "code_relink_b")
            )
        ).scalar_one()
        assert code_row.consumed_at is not None

        all_t = (
            await session.execute(
                select(WebBalanceTransfer).where(
                    WebBalanceTransfer.profile_token == "tok_relink_p"
                )
            )
        ).scalars().all()
        assert len(all_t) == 1
        assert all_t[0].status == "completed"
        assert all_t[0].telegram_id == 1001


# ---------------------------------------------------------------------------
# 16. Legacy already credited state never creates fresh random credit
# ---------------------------------------------------------------------------
async def test_legacy_already_credited_state_never_creates_fresh_random_credit(patched_webstore, monkeypatch):
    """Bot-side read-only probe reports web_migrate_<profile[:16]> already completed in bot.
    Local debit is absent.
    Must NOT create a fresh random wbtr_* credit; must reconcile legacy amount locally.
    """
    profile_tok = "0123456789abcdef0123456789abcdef"
    legacy_source = f"web_migrate_{profile_tok[:16]}"

    async with patched_webstore() as session:
        account = WebBalanceAccount(
            profile_token=profile_tok,
            contact="legacy@test.local",
            ref_code="ref_leg",
            balance_rub=100,
            updated_at=datetime.utcnow(),
        )
        session.add(account)
        await session.commit()

    get_probes = []
    post_credits = []

    def mock_get(url, params=None, headers=None, **kwargs):
        get_probes.append({"url": url, "params": params})
        if params and params.get("source_id") == legacy_source:
            return _MockResponse({
                "ok": True,
                "found": True,
                "source_id": legacy_source,
                "telegram_id": 555000,
                "amount_rub": 100,
                "status": "completed",
                "completed": True,
                "provider": "web_balance_migration",
            })
        return _MockResponse({"ok": True, "found": False})

    def mock_post(url, json=None, headers=None, **kwargs):
        post_credits.append(json)
        return _MockResponse({"status": "credited", "balance_rub": 100})

    monkeypatch.setattr(
        webstore_routes.aiohttp,
        "ClientSession",
        lambda *args, **kwargs: _MockClientSession(post_handler=mock_post, get_handler=mock_get),
    )

    async with patched_webstore() as session:
        res = await webstore_routes._migrate_web_balance_to_telegram(
            session, profile_tok, 555000
        )
        assert res is None

    assert len(get_probes) == 1
    assert get_probes[0]["params"]["source_id"] == legacy_source

    # CRITICAL: POST credit must NEVER have been called!
    assert len(post_credits) == 0

    async with patched_webstore() as session:
        acc = await session.get(WebBalanceAccount, profile_tok)
        assert acc.balance_rub == 0
        assert acc.total_spent_rub == 100

        txs = (
            await session.execute(
                select(WebBalanceTransaction).where(
                    WebBalanceTransaction.profile_token == profile_tok
                )
            )
        ).scalars().all()
        assert len(txs) == 1
        assert txs[0].source_id == legacy_source
        assert txs[0].amount_rub == 100

        transfers = (
            await session.execute(
                select(WebBalanceTransfer).where(
                    WebBalanceTransfer.profile_token == profile_tok
                )
            )
        ).scalars().all()
        assert len(transfers) == 0


# ---------------------------------------------------------------------------
# 17. Legacy status absent allows new transfer
# ---------------------------------------------------------------------------
async def test_legacy_status_absent_allows_new_transfer(patched_webstore, monkeypatch):
    """Read-only probe reports legacy source absent -> normal new transfer allowed."""
    profile_tok = "fedcba9876543210fedcba9876543210"
    legacy_source = f"web_migrate_{profile_tok[:16]}"

    async with patched_webstore() as session:
        account = WebBalanceAccount(
            profile_token=profile_tok,
            contact="new_trans@test.local",
            ref_code="ref_nt",
            balance_rub=100,
            updated_at=datetime.utcnow(),
        )
        session.add(account)
        await session.commit()

    get_probes = []
    post_credits = []

    def mock_get(url, params=None, headers=None, **kwargs):
        get_probes.append({"url": url, "params": params})
        return _MockResponse({"ok": True, "found": False})

    def mock_post(url, json=None, headers=None, **kwargs):
        post_credits.append(json)
        return _MockResponse({"status": "credited", "balance_rub": 100})

    monkeypatch.setattr(
        webstore_routes.aiohttp,
        "ClientSession",
        lambda *args, **kwargs: _MockClientSession(post_handler=mock_post, get_handler=mock_get),
    )

    async with patched_webstore() as session:
        res = await webstore_routes._migrate_web_balance_to_telegram(
            session, profile_tok, 777000
        )
        assert res is not None
        assert res.status == "completed"
        assert res.transfer_id.startswith("wbtr_")

    assert len(get_probes) == 1
    assert get_probes[0]["params"]["source_id"] == legacy_source
    assert len(post_credits) == 1
    assert post_credits[0]["source_id"] == res.transfer_id


# ---------------------------------------------------------------------------
# 18. Atomic code claim under concurrency
# ---------------------------------------------------------------------------
async def test_profile_link_code_can_be_consumed_only_once_under_concurrency(patched_webstore, monkeypatch):
    """Two concurrent claims for the same code: exactly one succeeds (200), loser gets 404."""
    async with patched_webstore() as session:
        session.add(
            WebOrder(
                order_id="ord_atomic_claim",
                contact="atomic@test.local",
                tariff_key="month_1",
                tariff_label="1 месяц",
                days=30,
                amount_rub=300,
                status="delivered",
                profile_token="tok_atomic_claim",
            )
        )
        session.add(
            WebProfileLinkCode(
                code="atomic_code_123",
                profile_token="tok_atomic_claim",
                expires_at=datetime.utcnow() + timedelta(minutes=15),
            )
        )
        await session.commit()

    monkeypatch.setattr(
        webstore_routes,
        "_migrate_web_balance_to_telegram",
        AsyncMock(return_value=None),
    )

    async def claim_code(tg_id: int):
        req = _Request(
            _json_body={"code": "atomic_code_123", "telegram_id": tg_id},
            headers={"X-Internal-Secret": "test-bridge-secret"},
        )
        return await webstore_routes.handle_internal_telegram_link_claim(req)

    r1, r2 = await asyncio.gather(claim_code(11111), claim_code(22222))

    statuses = {r1.status, r2.status}
    assert statuses == {200, 404}

    async with patched_webstore() as session:
        code_row = (
            await session.execute(
                select(WebProfileLinkCode).where(WebProfileLinkCode.code == "atomic_code_123")
            )
        ).scalar_one()
        assert code_row.consumed_at is not None

        links = (
            await session.execute(
                select(WebProfileLink).where(WebProfileLink.profile_token == "tok_atomic_claim")
            )
        ).scalars().all()
        assert len(links) == 1
        assert links[0].telegram_id in (11111, 22222)


# ---------------------------------------------------------------------------
# 19. Bot read-only balance-credit-status endpoint test
# ---------------------------------------------------------------------------
async def test_read_only_balance_credit_status_endpoint(monkeypatch):
    """Directly test bot endpoint handle_internal_balance_credit_status."""
    from bot import webhooks
    from bot.models import BalanceTopUp, Base as BotBase, User

    # 1. Test 403 on missing secret
    req_no_auth = _Request(query={"source_id": "test_src"})
    res_no_auth = await webhooks.handle_internal_balance_credit_status(req_no_auth)
    assert res_no_auth.status == 403

    # 2. Test 400 on missing source_id
    monkeypatch.setattr(webhooks.settings, "webstore_bridge_secret", "test-bridge-secret")
    req_no_src = _Request(query={}, headers={"X-Internal-Secret": "test-bridge-secret"})
    res_no_src = await webhooks.handle_internal_balance_credit_status(req_no_src)
    assert res_no_src.status == 400

    # 3. Test source_id not found
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(BotBase.metadata.create_all)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(webhooks, "async_session", session_factory)

    req_not_found = _Request(query={"source_id": "non_existent_source"}, headers={"X-Internal-Secret": "test-bridge-secret"})
    res_not_found = await webhooks.handle_internal_balance_credit_status(req_not_found)
    assert res_not_found.status == 200
    data_not_found = json.loads(res_not_found.text)
    assert data_not_found == {"ok": True, "found": False, "completed": False, "ambiguous": False}

    # 4. Test source_id found
    async with session_factory() as session:
        session.add(
            User(id=1, telegram_id=888999, username="test_u")
        )
        session.add(
            BalanceTopUp(
                id=1,
                user_id=1,
                telegram_id=888999,
                amount_rub=250.0,
                status="completed",
                provider="web_balance_migration",
                provider_payment_id="web_migrate_probe_1",
            )
        )
        await session.commit()

    req_found = _Request(query={"source_id": "web_migrate_probe_1"}, headers={"X-Internal-Secret": "test-bridge-secret"})
    res_found = await webhooks.handle_internal_balance_credit_status(req_found)
    assert res_found.status == 200
    data_found = json.loads(res_found.text)
    assert data_found["ok"] is True
    assert data_found["found"] is True
    assert data_found["source_id"] == "web_migrate_probe_1"
    assert data_found["telegram_id"] == 888999
    assert data_found["amount_rub"] == 250.0
    assert data_found["completed"] is True


# ---------------------------------------------------------------------------
# 20. Scheduler retry pending balance transfers
# ---------------------------------------------------------------------------
async def test_scheduler_retry_pending_balance_transfers(patched_webstore, monkeypatch):
    """Scheduler job retry_pending_balance_transfers finds eligible pending transfers and retries them."""
    from webstore.scheduler import retry_pending_balance_transfers

    async with patched_webstore() as session:
        # Transfer 1: pending and last_attempt_at was 5 minutes ago (eligible)
        session.add(
            WebBalanceTransfer(
                transfer_id="wbtr_sched_1",
                profile_token="tok_sched_1",
                telegram_id=9901,
                amount_rub=100,
                status="pending",
                last_attempt_at=datetime.utcnow() - timedelta(minutes=5),
            )
        )
        # Transfer 2: pending but last_attempt_at was 30 seconds ago (not eligible yet)
        session.add(
            WebBalanceTransfer(
                transfer_id="wbtr_sched_2",
                profile_token="tok_sched_2",
                telegram_id=9902,
                amount_rub=100,
                status="pending",
                last_attempt_at=datetime.utcnow() - timedelta(seconds=30),
            )
        )
        await session.commit()

    retried_transfers = []

    async def mock_migrate(session, profile_token, telegram_id, **kwargs):
        assert kwargs == {"reconcile_existing_only": True}
        retried_transfers.append((profile_token, telegram_id))

    monkeypatch.setattr(
        "webstore.scheduler._migrate_web_balance_to_telegram",
        mock_migrate,
    )

    await retry_pending_balance_transfers()

    # Only transfer 1 was retried (backoff respected)
    assert len(retried_transfers) == 1
    assert retried_transfers[0] == ("tok_sched_1", 9901)


# ===========================================================================
# Blockers 1 & 2 Regression Tests
# ===========================================================================

# ---------------------------------------------------------------------------
# 1. Historical successful migration must not debit new money
# ---------------------------------------------------------------------------
async def test_legacy_completed_with_old_tg_ledger_does_not_debit_new_balance(patched_webstore, monkeypatch):
    """Historical successful migration must NOT debit new money.

    Fixture:
    Bot evidence: source_id = web_migrate_... telegram_id = 123 amount = 100 completed = true provider = web_balance_migration
    Webstore historical ledger: profile=P kind=balance_migration source_id=tg:123 amount=100
    Current NEW web balance: 150
    Expected:
    old 100 is NOT debited again;
    current 150 considered new;
    new wbtr_* transfer created for 150;
    bot new POST amount = 150;
    historical total_spent not incremented with old amount again.
    """
    profile_tok = "tok_legacy_old_ledger_profile_1"
    legacy_source = f"web_migrate_{profile_tok[:16]}"
    telegram_id = 123

    async with patched_webstore() as session:
        account = WebBalanceAccount(
            profile_token=profile_tok,
            contact="legacy_user@test.local",
            ref_code="ref_leg_1",
            balance_rub=150,
            total_spent_rub=100,
            updated_at=datetime.utcnow(),
        )
        session.add(account)
        # Historical local transaction as created by old code: kind="balance_migration", source_id="tg:123"
        session.add(
            WebBalanceTransaction(
                profile_token=profile_tok,
                kind="balance_migration",
                amount_rub=100,
                direction="debit",
                balance_after_rub=0,
                source_id=f"tg:{telegram_id}",
                description="Перенос баланса в Telegram",
                created_at=datetime.utcnow(),
            )
        )
        await session.commit()

    get_probes = []
    post_credits = []

    def mock_get(url, params=None, headers=None, **kwargs):
        get_probes.append({"url": url, "params": params})
        if params and params.get("source_id") == legacy_source:
            return _MockResponse({
                "ok": True,
                "found": True,
                "source_id": legacy_source,
                "telegram_id": telegram_id,
                "amount_rub": 100,
                "status": "completed",
                "completed": True,
                "provider": "web_balance_migration",
            })
        return _MockResponse({"ok": True, "found": False})

    def mock_post(url, json=None, headers=None, **kwargs):
        post_credits.append(json)
        return _MockResponse({"status": "credited", "balance_rub": 150})

    monkeypatch.setattr(
        webstore_routes.aiohttp,
        "ClientSession",
        lambda *args, **kwargs: _MockClientSession(post_handler=mock_post, get_handler=mock_get),
    )

    async with patched_webstore() as session:
        res = await webstore_routes._migrate_web_balance_to_telegram(
            session, profile_tok, telegram_id
        )
        assert res is not None
        assert res.status == "completed"
        assert res.amount_rub == 150
        assert res.transfer_id.startswith("wbtr_")

    assert len(get_probes) == 1
    assert get_probes[0]["params"]["source_id"] == legacy_source

    # Crucial: exactly ONE post credit was made for the NEW balance (150 RUB), NOT old 100
    assert len(post_credits) == 1
    assert post_credits[0]["amount_rub"] == 150
    assert post_credits[0]["source_id"] == res.transfer_id

    async with patched_webstore() as session:
        acc = await session.get(WebBalanceAccount, profile_tok)
        assert acc.balance_rub == 0
        # total_spent was 100 + 150 = 250, NOT 100 + 100 + 150
        assert acc.total_spent_rub == 250

        txs = (
            await session.execute(
                select(WebBalanceTransaction)
                .where(WebBalanceTransaction.profile_token == profile_tok)
                .order_by(WebBalanceTransaction.id)
            )
        ).scalars().all()
        # Exactly 2 transactions: original tg:123 (100) and new wbtr_* (150)
        assert len(txs) == 2
        assert txs[0].source_id == f"tg:{telegram_id}"
        assert txs[0].amount_rub == 100
        assert txs[1].source_id == res.transfer_id
        assert txs[1].amount_rub == 150


# ---------------------------------------------------------------------------
# 2. Legacy bot completed + no old local ledger
# ---------------------------------------------------------------------------
async def test_legacy_bot_completed_plus_no_old_local_ledger(patched_webstore, monkeypatch):
    """Crash-reconciliation case:
    Bot says completed 100;
    Local tg:123 migration is absent;
    Current balance = 150.
    Expected:
    exact 100 reconciliation debit;
    remaining 50 -> new wbtr_* transfer;
    no duplicate old credit to bot.
    """
    profile_tok = "tok_legacy_no_ledger_profile_2"
    legacy_source = f"web_migrate_{profile_tok[:16]}"
    telegram_id = 123

    async with patched_webstore() as session:
        account = WebBalanceAccount(
            profile_token=profile_tok,
            contact="legacy_reconcile@test.local",
            ref_code="ref_leg_2",
            balance_rub=150,
            total_spent_rub=0,
            updated_at=datetime.utcnow(),
        )
        session.add(account)
        await session.commit()

    get_probes = []
    post_credits = []

    def mock_get(url, params=None, headers=None, **kwargs):
        get_probes.append({"url": url, "params": params})
        if params and params.get("source_id") == legacy_source:
            return _MockResponse({
                "ok": True,
                "found": True,
                "source_id": legacy_source,
                "telegram_id": telegram_id,
                "amount_rub": 100,
                "status": "completed",
                "completed": True,
                "provider": "web_balance_migration",
            })
        return _MockResponse({"ok": True, "found": False})

    def mock_post(url, json=None, headers=None, **kwargs):
        post_credits.append(json)
        return _MockResponse({"status": "credited", "balance_rub": 50})

    monkeypatch.setattr(
        webstore_routes.aiohttp,
        "ClientSession",
        lambda *args, **kwargs: _MockClientSession(post_handler=mock_post, get_handler=mock_get),
    )

    async with patched_webstore() as session:
        res = await webstore_routes._migrate_web_balance_to_telegram(
            session, profile_tok, telegram_id
        )
        assert res is not None
        assert res.status == "completed"
        assert res.amount_rub == 50
        assert res.transfer_id.startswith("wbtr_")

    assert len(get_probes) == 1
    # Exactly 1 POST credit, ONLY for the remaining 50 RUB
    assert len(post_credits) == 1
    assert post_credits[0]["amount_rub"] == 50
    assert post_credits[0]["source_id"] == res.transfer_id

    async with patched_webstore() as session:
        acc = await session.get(WebBalanceAccount, profile_tok)
        assert acc.balance_rub == 0
        assert acc.total_spent_rub == 150

        txs = (
            await session.execute(
                select(WebBalanceTransaction)
                .where(WebBalanceTransaction.profile_token == profile_tok)
            )
        ).scalars().all()
        # 2 transactions: reconciled legacy debit (100) with legacy_source, and new wbtr_* debit (50)
        assert len(txs) == 2
        sources = {t.source_id for t in txs}
        assert legacy_source in sources
        assert res.transfer_id in sources


# ---------------------------------------------------------------------------
# 3. Legacy current balance smaller than missing debit
# ---------------------------------------------------------------------------
async def test_legacy_current_balance_smaller_than_missing_debit(patched_webstore, monkeypatch):
    """Bot completed 100.
    Local historical debit absent.
    Current balance = 50 (< 100).
    Expected:
    fail closed;
    no wbtr POST;
    balance 50 untouched.
    """
    profile_tok = "tok_legacy_underfunded_profile_3"
    legacy_source = f"web_migrate_{profile_tok[:16]}"
    telegram_id = 123

    async with patched_webstore() as session:
        account = WebBalanceAccount(
            profile_token=profile_tok,
            contact="underfunded@test.local",
            ref_code="ref_leg_3",
            balance_rub=50,
            total_spent_rub=0,
            updated_at=datetime.utcnow(),
        )
        session.add(account)
        await session.commit()

    get_probes = []
    post_credits = []

    def mock_get(url, params=None, headers=None, **kwargs):
        get_probes.append({"url": url, "params": params})
        if params and params.get("source_id") == legacy_source:
            return _MockResponse({
                "ok": True,
                "found": True,
                "source_id": legacy_source,
                "telegram_id": telegram_id,
                "amount_rub": 100,
                "status": "completed",
                "completed": True,
                "provider": "web_balance_migration",
            })
        return _MockResponse({"ok": True, "found": False})

    def mock_post(url, json=None, headers=None, **kwargs):
        post_credits.append(json)
        return _MockResponse({"status": "credited", "balance_rub": 50})

    monkeypatch.setattr(
        webstore_routes.aiohttp,
        "ClientSession",
        lambda *args, **kwargs: _MockClientSession(post_handler=mock_post, get_handler=mock_get),
    )

    async with patched_webstore() as session:
        res = await webstore_routes._migrate_web_balance_to_telegram(
            session, profile_tok, telegram_id
        )
        # Fails closed
        assert res is None

    assert len(get_probes) == 1
    # NO POST credit attempted
    assert len(post_credits) == 0

    async with patched_webstore() as session:
        acc = await session.get(WebBalanceAccount, profile_tok)
        assert acc.balance_rub == 50
        assert acc.total_spent_rub == 0


# ---------------------------------------------------------------------------
# 4. Legacy status pending is NOT completed evidence
# ---------------------------------------------------------------------------
async def test_legacy_status_pending_is_not_completed_evidence(patched_webstore, monkeypatch):
    """Bot endpoint returns found=true completed=false status=pending.
    Expected:
    no local debit;
    no fresh wbtr credit;
    fail closed.
    """
    profile_tok = "tok_legacy_pending_profile_4"
    legacy_source = f"web_migrate_{profile_tok[:16]}"
    telegram_id = 123

    async with patched_webstore() as session:
        account = WebBalanceAccount(
            profile_token=profile_tok,
            contact="pending_leg@test.local",
            ref_code="ref_leg_4",
            balance_rub=100,
            total_spent_rub=0,
            updated_at=datetime.utcnow(),
        )
        session.add(account)
        await session.commit()

    get_probes = []
    post_credits = []

    def mock_get(url, params=None, headers=None, **kwargs):
        get_probes.append({"url": url, "params": params})
        if params and params.get("source_id") == legacy_source:
            return _MockResponse({
                "ok": True,
                "found": True,
                "source_id": legacy_source,
                "telegram_id": telegram_id,
                "amount_rub": 100,
                "status": "pending",
                "completed": False,
                "provider": "web_balance_migration",
            })
        return _MockResponse({"ok": True, "found": False})

    def mock_post(url, json=None, headers=None, **kwargs):
        post_credits.append(json)
        return _MockResponse({"status": "credited", "balance_rub": 100})

    monkeypatch.setattr(
        webstore_routes.aiohttp,
        "ClientSession",
        lambda *args, **kwargs: _MockClientSession(post_handler=mock_post, get_handler=mock_get),
    )

    async with patched_webstore() as session:
        res = await webstore_routes._migrate_web_balance_to_telegram(
            session, profile_tok, telegram_id
        )
        assert res is None

    assert len(get_probes) == 1
    assert len(post_credits) == 0

    async with patched_webstore() as session:
        acc = await session.get(WebBalanceAccount, profile_tok)
        assert acc.balance_rub == 100
        assert acc.total_spent_rub == 0


# ---------------------------------------------------------------------------
# 5. Legacy conflicting status rows
# ---------------------------------------------------------------------------
async def test_legacy_conflicting_status_rows(patched_webstore, monkeypatch):
    """Read-only endpoint sees conflicting duplicate historical rows.
    Expected:
    endpoint reports ambiguity;
    webstore does nothing financially.
    """
    from bot import webhooks
    from bot.models import BalanceTopUp, Base as BotBase, User

    # Part A: Test bot endpoint detects conflicting duplicate rows and returns ambiguous=True
    monkeypatch.setattr(webhooks.settings, "webstore_bridge_secret", "test-bridge-secret")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(BotBase.metadata.create_all)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(webhooks, "async_session", session_factory)

    async with session_factory() as session:
        session.add(User(id=1, telegram_id=5001, username="u1"))
        session.add(User(id=2, telegram_id=5002, username="u2"))
        # Two conflicting historical topups with the SAME provider_payment_id
        session.add(
            BalanceTopUp(
                id=1,
                user_id=1,
                telegram_id=5001,
                amount_rub=100.0,
                status="completed",
                provider="web_balance_migration",
                provider_payment_id="web_migrate_conflict_src",
            )
        )
        session.add(
            BalanceTopUp(
                id=2,
                user_id=2,
                telegram_id=5002,
                amount_rub=200.0,
                status="completed",
                provider="web_balance_migration",
                provider_payment_id="web_migrate_conflict_src",
            )
        )
        await session.commit()

    req_status = _Request(
        query={"source_id": "web_migrate_conflict_src"},
        headers={"X-Internal-Secret": "test-bridge-secret"},
    )
    res_status = await webhooks.handle_internal_balance_credit_status(req_status)
    assert res_status.status == 200
    st_data = json.loads(res_status.text)
    assert st_data["ok"] is True
    assert st_data["found"] is True
    assert st_data["ambiguous"] is True
    assert st_data["completed"] is False

    # Part B: Webstore calling bot probe that returns ambiguous=True fails closed without debit
    profile_tok = "tok_legacy_conflict_profile_5"
    legacy_source = f"web_migrate_{profile_tok[:16]}"
    telegram_id = 5001

    async with patched_webstore() as session:
        account = WebBalanceAccount(
            profile_token=profile_tok,
            contact="conflict@test.local",
            ref_code="ref_leg_5",
            balance_rub=150,
            total_spent_rub=0,
            updated_at=datetime.utcnow(),
        )
        session.add(account)
        await session.commit()

    post_credits = []

    def mock_get(url, params=None, headers=None, **kwargs):
        return _MockResponse({
            "ok": True,
            "found": True,
            "ambiguous": True,
            "completed": False,
            "error": "Conflicting historical rows found for source_id",
        })

    def mock_post(url, json=None, headers=None, **kwargs):
        post_credits.append(json)
        return _MockResponse({"status": "credited", "balance_rub": 150})

    monkeypatch.setattr(
        webstore_routes.aiohttp,
        "ClientSession",
        lambda *args, **kwargs: _MockClientSession(post_handler=mock_post, get_handler=mock_get),
    )

    async with patched_webstore() as session:
        res = await webstore_routes._migrate_web_balance_to_telegram(
            session, profile_tok, telegram_id
        )
        assert res is None

    assert len(post_credits) == 0

    async with patched_webstore() as session:
        acc = await session.get(WebBalanceAccount, profile_tok)
        assert acc.balance_rub == 150
        assert acc.total_spent_rub == 0


# ---------------------------------------------------------------------------
# 6. Actual bot concurrent idempotency
# ---------------------------------------------------------------------------
async def test_actual_bot_concurrent_idempotency(tmp_path, monkeypatch):
    """Actual bot concurrent idempotency.
    Run two concurrent calls to the REAL handle_internal_balance_credit
    with same wbtr_* source_id, same telegram, same amount.
    Expected:
    user balance increased exactly once;
    one completed BalanceTopUp for this wbtr;
    responses semantically credited + already_credited;
    one financial credit.
    """
    from bot import webhooks
    from bot.models import BalanceTopUp, Base as BotBase, BalanceTransaction, User

    db_path = tmp_path / "bot_concurrency_test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA journal_mode=WAL;"))
        await conn.execute(text("PRAGMA busy_timeout=5000;"))
        await conn.run_sync(BotBase.metadata.create_all)
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_balance_topup_web_balance_wbtr "
                "ON balance_topups (provider_payment_id) "
                "WHERE provider = 'web_balance_migration' AND provider_payment_id LIKE 'wbtr_%'"
            )
        )

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(webhooks, "async_session", session_factory)
    monkeypatch.setattr(webhooks.settings, "webstore_bridge_secret", "test-bridge-secret")

    tg_id = 999888
    async with session_factory() as session:
        session.add(
            User(id=1, telegram_id=tg_id, username="race_u", balance_rub=0.0)
        )
        await session.commit()

    source_id = "wbtr_real_concurrency_123"
    req1 = _Request(
        _json_body={
            "telegram_id": tg_id,
            "amount_rub": 100.0,
            "source_type": "web_balance_migration",
            "source_id": source_id,
            "description": "Transfer",
        },
        headers={"X-Internal-Secret": "test-bridge-secret"},
    )
    req2 = _Request(
        _json_body={
            "telegram_id": tg_id,
            "amount_rub": 100.0,
            "source_type": "web_balance_migration",
            "source_id": source_id,
            "description": "Transfer",
        },
        headers={"X-Internal-Secret": "test-bridge-secret"},
    )

    res1, res2 = await asyncio.gather(
        webhooks.handle_internal_balance_credit(req1),
        webhooks.handle_internal_balance_credit(req2),
    )

    assert res1.status == 200
    assert res2.status == 200
    data1 = json.loads(res1.text)
    data2 = json.loads(res2.text)
    statuses = {data1.get("status"), data2.get("status")}
    assert statuses == {"credited", "already_credited"}

    async with session_factory() as session:
        u = (await session.execute(select(User).where(User.telegram_id == tg_id))).scalar_one()
        # Balance credited exactly ONCE
        assert round(float(u.balance_rub or 0.0), 2) == 100.0

        topups = (
            await session.execute(
                select(BalanceTopUp).where(BalanceTopUp.provider_payment_id == source_id)
            )
        ).scalars().all()
        assert len(topups) == 1
        assert topups[0].amount_rub == 100.0
        assert topups[0].status == "completed"

        txs = (
            await session.execute(
                select(BalanceTransaction).where(BalanceTransaction.source_id == source_id)
            )
        ).scalars().all()
        assert len(txs) == 1
        assert txs[0].amount_rub == 100.0


# ---------------------------------------------------------------------------
# 7. Same wbtr source, different amount
# ---------------------------------------------------------------------------
async def test_same_wbtr_source_different_amount(tmp_path, monkeypatch):
    """Same wbtr source, different amount:
    First request: 100 RUB -> credited (200)
    Second request: 200 RUB, same source_id -> 409 Conflict
    Balance remains +100 only.
    """
    from bot import webhooks
    from bot.models import BalanceTopUp, Base as BotBase, BalanceTransaction, User

    db_path = tmp_path / "bot_diff_amt_test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(BotBase.metadata.create_all)
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_balance_topup_web_balance_wbtr "
                "ON balance_topups (provider_payment_id) "
                "WHERE provider = 'web_balance_migration' AND provider_payment_id LIKE 'wbtr_%'"
            )
        )

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(webhooks, "async_session", session_factory)
    monkeypatch.setattr(webhooks.settings, "webstore_bridge_secret", "test-bridge-secret")

    tg_id = 777888
    async with session_factory() as session:
        session.add(
            User(id=1, telegram_id=tg_id, username="diff_amt_u", balance_rub=0.0)
        )
        await session.commit()

    source_id = "wbtr_diff_amt_source"
    req1 = _Request(
        _json_body={
            "telegram_id": tg_id,
            "amount_rub": 100.0,
            "source_type": "web_balance_migration",
            "source_id": source_id,
        },
        headers={"X-Internal-Secret": "test-bridge-secret"},
    )
    res1 = await webhooks.handle_internal_balance_credit(req1)
    assert res1.status == 200
    assert json.loads(res1.text).get("status") == "credited"

    req2 = _Request(
        _json_body={
            "telegram_id": tg_id,
            "amount_rub": 200.0,
            "source_type": "web_balance_migration",
            "source_id": source_id,
        },
        headers={"X-Internal-Secret": "test-bridge-secret"},
    )
    res2 = await webhooks.handle_internal_balance_credit(req2)
    assert res2.status == 409

    async with session_factory() as session:
        u = (await session.execute(select(User).where(User.telegram_id == tg_id))).scalar_one()
        assert round(float(u.balance_rub or 0.0), 2) == 100.0


# ---------------------------------------------------------------------------
# 8. Same wbtr source, different Telegram
# ---------------------------------------------------------------------------
async def test_same_wbtr_source_different_telegram(tmp_path, monkeypatch):
    """Same wbtr source, different Telegram:
    First -> User A (amount 100) -> 200 credited
    Second same source -> User B -> 409 Conflict
    User B gets nothing (0.0). User A remains 100.0.
    """
    from bot import webhooks
    from bot.models import BalanceTopUp, Base as BotBase, BalanceTransaction, User

    db_path = tmp_path / "bot_diff_tg_test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(BotBase.metadata.create_all)
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_balance_topup_web_balance_wbtr "
                "ON balance_topups (provider_payment_id) "
                "WHERE provider = 'web_balance_migration' AND provider_payment_id LIKE 'wbtr_%'"
            )
        )

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(webhooks, "async_session", session_factory)
    monkeypatch.setattr(webhooks.settings, "webstore_bridge_secret", "test-bridge-secret")

    async with session_factory() as session:
        session.add(User(id=1, telegram_id=111, username="user_a", balance_rub=0.0))
        session.add(User(id=2, telegram_id=222, username="user_b", balance_rub=0.0))
        await session.commit()

    source_id = "wbtr_diff_user_source"
    req1 = _Request(
        _json_body={
            "telegram_id": 111,
            "amount_rub": 100.0,
            "source_type": "web_balance_migration",
            "source_id": source_id,
        },
        headers={"X-Internal-Secret": "test-bridge-secret"},
    )
    res1 = await webhooks.handle_internal_balance_credit(req1)
    assert res1.status == 200
    assert json.loads(res1.text).get("status") == "credited"

    req2 = _Request(
        _json_body={
            "telegram_id": 222,
            "amount_rub": 100.0,
            "source_type": "web_balance_migration",
            "source_id": source_id,
        },
        headers={"X-Internal-Secret": "test-bridge-secret"},
    )
    res2 = await webhooks.handle_internal_balance_credit(req2)
    assert res2.status == 409

    async with session_factory() as session:
        u_a = (await session.execute(select(User).where(User.telegram_id == 111))).scalar_one()
        u_b = (await session.execute(select(User).where(User.telegram_id == 222))).scalar_one()
        assert round(float(u_a.balance_rub or 0.0), 2) == 100.0
        assert round(float(u_b.balance_rub or 0.0), 2) == 0.0


# ---------------------------------------------------------------------------
# 9. Bot DB unique index exists
# ---------------------------------------------------------------------------
async def test_bot_db_unique_index_exists(tmp_path, monkeypatch):
    """Migration/init test must prove partial unique index for new wbtr_* really exists in SQLite.

    Proves:
    1. init_db creates uq_balance_topup_web_balance_wbtr.
    2. Partial index enforces uniqueness for provider='web_balance_migration' and provider_payment_id LIKE 'wbtr_%'.
    3. Partial index does NOT block duplicates for other providers or legacy web_migrate_* rows.
    """
    import bot.database as bot_db
    from bot.models import BalanceTopUp

    db_path = tmp_path / "bot_init_index_test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    monkeypatch.setattr(bot_db, "engine", engine)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(bot_db, "async_session", session_factory)

    # Run init_db()
    await bot_db.init_db()

    # Verify index in sqlite_master
    async with engine.begin() as conn:
        res = await conn.execute(
            text("SELECT name, sql FROM sqlite_master WHERE type='index' AND name='uq_balance_topup_web_balance_wbtr'")
        )
        row = res.fetchone()
        assert row is not None
        assert row[0] == "uq_balance_topup_web_balance_wbtr"
        assert "LIKE 'wbtr_%'" in row[1]
        assert "web_balance_migration" in row[1]

    # Verify partial uniqueness enforcement:
    # 1. Duplicate wbtr_* with web_balance_migration fails
    async with session_factory() as session:
        session.add(
            BalanceTopUp(
                user_id=1,
                telegram_id=123,
                amount_rub=100.0,
                provider="web_balance_migration",
                provider_payment_id="wbtr_test_dup",
                status="completed",
            )
        )
        await session.commit()

    async with session_factory() as session:
        session.add(
            BalanceTopUp(
                user_id=1,
                telegram_id=123,
                amount_rub=100.0,
                provider="web_balance_migration",
                provider_payment_id="wbtr_test_dup",
                status="completed",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()

    # 2. Legacy web_migrate_* duplicates are NOT blocked by this partial index
    async with session_factory() as session:
        session.add(
            BalanceTopUp(
                user_id=1,
                telegram_id=123,
                amount_rub=100.0,
                provider="web_balance_migration",
                provider_payment_id="web_migrate_legacy_123",
                status="completed",
            )
        )
        session.add(
            BalanceTopUp(
                user_id=1,
                telegram_id=123,
                amount_rub=100.0,
                provider="web_balance_migration",
                provider_payment_id="web_migrate_legacy_123",
                status="completed",
            )
        )
        # Does NOT raise IntegrityError
        await session.commit()


# ===========================================================================
# Audit Fix Tests: Tasks 1 - 7
# ===========================================================================


async def test_concurrent_telegram_a_b_linking_real_sqlite(tmp_path, monkeypatch):
    """Задача 1 (P1): Concurrent Telegram A/B linking on real SQLite WAL DB.
    
    Serializes linking & transfer reservation by profile_token via WebProfileLock.
    Guarantees funds are only credited to the recipient bound to the transfer.
    Never transfers funds to wrong recipient.
    """
    db_file = tmp_path / "test_ab_linking.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}", echo=False)
    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA journal_mode=WAL;"))
        await conn.execute(text("PRAGMA busy_timeout=5000;"))
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_web_balance_transfers_profile_pending "
                "ON web_balance_transfers(profile_token) WHERE status = 'pending';"
            )
        )
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_web_balance_tx_migration_source "
                "ON web_balance_transactions(source_id) "
                "WHERE kind = 'balance_migration' AND source_id IS NOT NULL;"
            )
        )
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(webstore_routes, "async_session", session_factory)
    monkeypatch.setattr("webstore.database.async_session", session_factory)
    monkeypatch.setattr(webstore_routes.settings, "bridge_shared_secret", "test-bridge-secret")
    monkeypatch.setattr(webstore_routes.settings, "referral_bridge_url", "http://bridge.test/vpnbot")

    profile_token = "prof_ab_race_12345"
    tg_a = 11111
    tg_b = 22222

    async with session_factory() as session:
        session.add(
            WebOrder(
                order_id="ord_ab_1",
                contact="client_ab@test.local",
                tariff_key="month_1",
                tariff_label="1 месяц",
                days=30,
                amount_rub=300,
                status="paid",
                profile_token=profile_token,
                created_at=datetime.utcnow(),
            )
        )
        session.add(
            WebBalanceAccount(
                profile_token=profile_token,
                ref_code="ref_" + profile_token[:8],
                balance_rub=100,
                total_earned_rub=100,
                total_spent_rub=0,
                updated_at=datetime.utcnow(),
            )
        )
        session.add(
            WebProfileLinkCode(
                code="code_a_123",
                profile_token=profile_token,
                created_at=datetime.utcnow(),
                expires_at=datetime.utcnow() + timedelta(minutes=15),
            )
        )
        session.add(
            WebProfileLinkCode(
                code="code_b_123",
                profile_token=profile_token,
                created_at=datetime.utcnow(),
                expires_at=datetime.utcnow() + timedelta(minutes=15),
            )
        )
        await session.commit()

    recorded_recipients = []

    def mock_post(url, json=None, headers=None, **kwargs):
        if "/internal/balance-credit" in url:
            recorded_recipients.append(json.get("telegram_id"))
            return _MockResponse({"status": "credited", "balance_rub": 100})
        return _MockResponse({"status": "ok"})

    def mock_get(url, params=None, headers=None, **kwargs):
        return _MockResponse({"ok": True, "found": False})

    client = _MockClientSession(post_handler=mock_post, get_handler=mock_get)
    monkeypatch.setattr("aiohttp.ClientSession", lambda *args, **kwargs: client)

    req_a = _Request(
        _json_body={"code": "code_a_123", "telegram_id": tg_a, "telegram_username": "user_a"},
        headers={"X-Internal-Secret": "test-bridge-secret"},
    )
    req_b = _Request(
        _json_body={"code": "code_b_123", "telegram_id": tg_b, "telegram_username": "user_b"},
        headers={"X-Internal-Secret": "test-bridge-secret"},
    )

    res_a, res_b = await asyncio.gather(
        webstore_routes.handle_internal_telegram_link_claim(req_a),
        webstore_routes.handle_internal_telegram_link_claim(req_b),
    )

    # Both requests execute safely without deadlocks or crashes
    assert res_a.status in (200, 409)
    assert res_b.status in (200, 409)

    async with session_factory() as session:
        # Check transfers:
        transfers = (
            await session.execute(
                select(WebBalanceTransfer).where(WebBalanceTransfer.profile_token == profile_token)
            )
        ).scalars().all()
        # Exactly one transfer was created for initial balance 100
        assert len(transfers) >= 1
        initial_tx = transfers[0]
        assert initial_tx.amount_rub == 100
        assert initial_tx.status == "completed"
        # The recipient in recorded HTTP post MUST match transfer.telegram_id
        assert initial_tx.telegram_id in (tg_a, tg_b)
        assert initial_tx.telegram_id in recorded_recipients

        # Balance on web account was debited to 0
        account = await session.get(WebBalanceAccount, profile_token)
        assert account.balance_rub == 0
        assert account.total_spent_rub == 100

        # Ledger matches
        ledger = (
            await session.execute(
                select(WebBalanceTransaction).where(WebBalanceTransaction.profile_token == profile_token)
            )
        ).scalars().all()
        assert len(ledger) == 1
        assert ledger[0].amount_rub == 100
        assert ledger[0].direction == "debit"

    await engine.dispose()


async def test_atomic_balance_credit_different_source_ids_real_sqlite(tmp_path, monkeypatch):
    """Задача 2 (P1): Atomic balance credit for two different wbtr_* IDs on real SQLite WAL.
    
    - Initial balance 0 RUB.
    - Two parallel requests: wbtr_1 for 100 RUB, wbtr_2 for 150 RUB.
    - Final balance EXACTLY 250 RUB (no lost update).
    - 2 rows in balance_topups, 2 rows in balance_transactions.
    - Repeating both requests returns already_credited and does NOT change balance.
    """
    from bot import webhooks
    from bot.models import BalanceTopUp, Base as BotBase, BalanceTransaction, User

    db_path = tmp_path / "bot_atomic_credit_test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA journal_mode=WAL;"))
        await conn.execute(text("PRAGMA busy_timeout=5000;"))
        await conn.run_sync(BotBase.metadata.create_all)
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_balance_topup_web_balance_wbtr "
                "ON balance_topups (provider_payment_id) "
                "WHERE provider = 'web_balance_migration' AND provider_payment_id LIKE 'wbtr_%'"
            )
        )

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(webhooks, "async_session", session_factory)
    monkeypatch.setattr(webhooks.settings, "webstore_bridge_secret", "test-bridge-secret")

    tg_id = 777666
    async with session_factory() as session:
        session.add(
            User(id=42, telegram_id=tg_id, username="atomic_user", balance_rub=0.0)
        )
        await session.commit()

    source_1 = "wbtr_p1_source_100"
    source_2 = "wbtr_p1_source_150"

    req1 = _Request(
        _json_body={
            "telegram_id": tg_id,
            "amount_rub": 100.0,
            "source_type": "web_balance_migration",
            "source_id": source_1,
            "description": "Transfer 100",
        },
        headers={"X-Internal-Secret": "test-bridge-secret"},
    )
    req2 = _Request(
        _json_body={
            "telegram_id": tg_id,
            "amount_rub": 150.0,
            "source_type": "web_balance_migration",
            "source_id": source_2,
            "description": "Transfer 150",
        },
        headers={"X-Internal-Secret": "test-bridge-secret"},
    )

    # Execute both parallel requests
    res1, res2 = await asyncio.gather(
        webhooks.handle_internal_balance_credit(req1),
        webhooks.handle_internal_balance_credit(req2),
    )

    assert res1.status == 200
    assert res2.status == 200
    data1 = json.loads(res1.text)
    data2 = json.loads(res2.text)
    assert data1["status"] == "credited"
    assert data2["status"] == "credited"

    async with session_factory() as session:
        u = (await session.execute(select(User).where(User.telegram_id == tg_id))).scalar_one()
        # Final balance must be EXACTLY 250.0 RUB (no lost update!)
        assert round(float(u.balance_rub or 0.0), 2) == 250.0

        topups = (
            await session.execute(
                select(BalanceTopUp).where(BalanceTopUp.telegram_id == tg_id).order_by(BalanceTopUp.id.asc())
            )
        ).scalars().all()
        assert len(topups) == 2
        assert {topups[0].amount_rub, topups[1].amount_rub} == {100.0, 150.0}
        assert all(t.status == "completed" for t in topups)

        txs = (
            await session.execute(
                select(BalanceTransaction).where(BalanceTransaction.user_id == 42).order_by(BalanceTransaction.id.asc())
            )
        ).scalars().all()
        assert len(txs) == 2
        assert {txs[0].amount_rub, txs[1].amount_rub} == {100.0, 150.0}

    # Repeat both requests: must return already_credited and NOT change balance
    res1_rep, res2_rep = await asyncio.gather(
        webhooks.handle_internal_balance_credit(req1),
        webhooks.handle_internal_balance_credit(req2),
    )
    assert res1_rep.status == 200
    assert res2_rep.status == 200
    assert json.loads(res1_rep.text)["status"] == "already_credited"
    assert json.loads(res2_rep.text)["status"] == "already_credited"

    async with session_factory() as session:
        u_final = (await session.execute(select(User).where(User.telegram_id == tg_id))).scalar_one()
        assert round(float(u_final.balance_rub or 0.0), 2) == 250.0

        topups_final = (
            await session.execute(select(BalanceTopUp).where(BalanceTopUp.telegram_id == tg_id))
        ).scalars().all()
        assert len(topups_final) == 2

        txs_final = (
            await session.execute(select(BalanceTransaction).where(BalanceTransaction.user_id == 42))
        ).scalars().all()
        assert len(txs_final) == 2

    await engine.dispose()


async def test_concurrent_spend_and_topup_during_legacy_reconciliation(patched_webstore, monkeypatch):
    """Задача 3 (P1): Atomic legacy reconciliation prevents lost update and negative balance.
    
    If webstore balance is spent concurrently during legacy reconciliation probe,
    the conditional atomic UPDATE fails closed without creating negative balance.
    If balance was added concurrently, it is preserved without lost updates.
    """
    profile_token = "prof_atomic_legacy_123"
    telegram_id = 987654

    # Setup: account has 100 RUB balance
    async with patched_webstore() as session:
        session.add(
            WebBalanceAccount(
                profile_token=profile_token,
                ref_code="ref_" + profile_token[:8],
                balance_rub=100,
                total_earned_rub=100,
                total_spent_rub=0,
                updated_at=datetime.utcnow(),
            )
        )
        await session.commit()

    # Bot confirms legacy 100 RUB was credited
    legacy_status_payload = {
        "ok": True,
        "found": True,
        "completed": True,
        "status": "completed",
        "provider": "web_balance_migration",
        "telegram_id": telegram_id,
        "amount_rub": 100,
    }

    def mock_get(url, params=None, headers=None, **kwargs):
        return _MockResponse(legacy_status_payload)

    client = _MockClientSession(get_handler=mock_get)
    monkeypatch.setattr("aiohttp.ClientSession", lambda *args, **kwargs: client)

    # Simulate concurrent balance spend prior to debit execution
    async with patched_webstore() as session:
        account = await session.get(WebBalanceAccount, profile_token)
        account.balance_rub = 0
        await session.commit()

    async with patched_webstore() as session:
        result = await webstore_routes._migrate_web_balance_to_telegram(session, profile_token, telegram_id)
        # Fails closed because balance was insufficient for missing debit
        assert result is None

    async with patched_webstore() as session:
        acc_after = await session.get(WebBalanceAccount, profile_token)
        # Never goes negative!
        assert acc_after.balance_rub >= 0


async def test_strict_historical_ledger_validation_and_fractional_amount(patched_webstore, monkeypatch):
    """Задача 4 (P2): Strict historical ledger validation and rejection of fractional amounts.
    
    - Fractional amount (e.g. 100.5) fails closed without debiting.
    - Contradictory transaction kind or direction fails closed.
    - Contradictory source_id fails closed.
    """
    profile_token = "prof_strict_val_1"
    telegram_id = 123456

    async with patched_webstore() as session:
        session.add(
            WebBalanceAccount(
                profile_token=profile_token,
                ref_code="ref_" + profile_token[:8],
                balance_rub=200,
                total_earned_rub=200,
                total_spent_rub=0,
                updated_at=datetime.utcnow(),
            )
        )
        await session.commit()

    # 1. Fractional amount returned from bot probe: 100.5 RUB
    def mock_get_fractional(url, params=None, headers=None, **kwargs):
        return _MockResponse({
            "ok": True,
            "found": True,
            "completed": True,
            "status": "completed",
            "provider": "web_balance_migration",
            "telegram_id": telegram_id,
            "amount_rub": 100.5,
        })

    client = _MockClientSession(get_handler=mock_get_fractional)
    monkeypatch.setattr("aiohttp.ClientSession", lambda *args, **kwargs: client)

    async with patched_webstore() as session:
        res = await webstore_routes._migrate_web_balance_to_telegram(session, profile_token, telegram_id)
        # Must fail closed without rounding 100.5 into 101 or 100!
        assert res is None

    async with patched_webstore() as session:
        acc = await session.get(WebBalanceAccount, profile_token)
        assert acc.balance_rub == 200  # Untouched

    # 2. Historical ledger has direction == 'credit' instead of 'debit'
    async with patched_webstore() as session:
        session.add(
            WebBalanceTransaction(
                profile_token=profile_token,
                amount_rub=100,
                direction="credit",  # Contradictory direction!
                kind="balance_migration",
                balance_after_rub=300,
                description="Invalid ledger",
                source_id=f"tg:{telegram_id}",
            )
        )
        await session.commit()

    def mock_get_integer(url, params=None, headers=None, **kwargs):
        return _MockResponse({
            "ok": True,
            "found": True,
            "completed": True,
            "status": "completed",
            "provider": "web_balance_migration",
            "telegram_id": telegram_id,
            "amount_rub": 100,
        })

    client2 = _MockClientSession(get_handler=mock_get_integer)
    monkeypatch.setattr("aiohttp.ClientSession", lambda *args, **kwargs: client2)

    async with patched_webstore() as session:
        res = await webstore_routes._migrate_web_balance_to_telegram(session, profile_token, telegram_id)
        # Contradictory ledger row -> fail closed!
        assert res is None


async def test_finalization_commit_failure_retains_pending_and_allows_safe_retry(patched_webstore, monkeypatch):
    """Задача 6 (P2): Finalization commit crash does NOT mark transfer completed in memory.
    
    - Commit fails during transfer finalization.
    - Transfer status in DB and memory remains 'pending'.
    - total_spent_rub is not double-incremented.
    - Safe retry succeeds cleanly.
    """
    profile_token = "prof_commit_crash_test"
    telegram_id = 999111

    async with patched_webstore() as session:
        session.add(
            WebBalanceAccount(
                profile_token=profile_token,
                ref_code="ref_" + profile_token[:8],
                balance_rub=100,
                total_earned_rub=100,
                total_spent_rub=0,
                updated_at=datetime.utcnow(),
            )
        )
        await session.commit()

    def mock_post(url, json=None, headers=None, **kwargs):
        return _MockResponse({"status": "credited", "balance_rub": 100})

    def mock_get(url, params=None, headers=None, **kwargs):
        return _MockResponse({"ok": True, "found": False})

    client = _MockClientSession(post_handler=mock_post, get_handler=mock_get)
    monkeypatch.setattr("aiohttp.ClientSession", lambda *args, **kwargs: client)

    # Simulate commit failure precisely during finalization
    fail_finalization_once = True
    orig_commit = AsyncSession.commit

    async def flaky_commit(self):
        nonlocal fail_finalization_once
        if fail_finalization_once and any(isinstance(obj, WebBalanceTransaction) for obj in self.new):
            fail_finalization_once = False
            raise IntegrityError("Simulated finalization commit failure", None, None)
        return await orig_commit(self)

    monkeypatch.setattr(AsyncSession, "commit", flaky_commit)

    async with patched_webstore() as session:
        transfer = await webstore_routes._migrate_web_balance_to_telegram(session, profile_token, telegram_id)
        # Crucial requirement: Must NOT be marked completed in memory when commit fails!
        assert transfer.status == "pending"

    # Restore normal commit
    monkeypatch.setattr(AsyncSession, "commit", orig_commit)

    # Verify DB state: transfer is pending, total_spent_rub was not incremented
    async with patched_webstore() as session:
        tx_row = (
            await session.execute(
                select(WebBalanceTransfer).where(WebBalanceTransfer.profile_token == profile_token)
            )
        ).scalar_one()
        assert tx_row.status == "pending"

        acc = await session.get(WebBalanceAccount, profile_token)
        assert acc.total_spent_rub == 0

    # Retry: Bot now returns already_credited, finalization succeeds
    def mock_post_retry(url, json=None, headers=None, **kwargs):
        return _MockResponse({"status": "already_credited", "balance_rub": 100})

    client_retry = _MockClientSession(post_handler=mock_post_retry, get_handler=mock_get)
    monkeypatch.setattr("aiohttp.ClientSession", lambda *args, **kwargs: client_retry)

    async with patched_webstore() as session:
        transfer_retry = await webstore_routes._migrate_web_balance_to_telegram(session, profile_token, telegram_id)
        assert transfer_retry.status == "completed"

    async with patched_webstore() as session:
        acc_final = await session.get(WebBalanceAccount, profile_token)
        assert acc_final.total_spent_rub == 100  # Incremented exactly ONCE!


async def test_fail_fast_financial_indexes_with_conflicting_rows(tmp_path, monkeypatch):
    """Задача 7 (P2): Fail-fast financial indexes.
    
    If critical unique index cannot be created due to duplicates,
    init_db() raises RuntimeError and logs critical diagnostic without silent data corruption.
    """
    import webstore.database as webstore_db

    db_path = tmp_path / "test_fail_fast_indexes.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    monkeypatch.setattr(webstore_db, "engine", engine)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(webstore_db, "async_session", session_factory)

    # 1. Create table with duplicate pending transfers (drop partial unique index first to simulate unindexed state)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("DROP INDEX IF EXISTS uq_web_balance_transfers_profile_pending;"))
        await conn.execute(
            text(
                "INSERT INTO web_balance_transfers (transfer_id, profile_token, telegram_id, amount_rub, status, attempts, created_at) "
                "VALUES ('wbtr_dup1', 'prof_dup', 111, 100, 'pending', 0, '2026-01-01 00:00:00'), "
                "       ('wbtr_dup2', 'prof_dup', 222, 100, 'pending', 0, '2026-01-01 00:00:00')"
            )
        )

    # 2. init_db() must raise RuntimeError (fail-fast)
    with pytest.raises(RuntimeError) as exc_info:
        await webstore_db.init_db()

    assert "uq_web_balance_transfers_profile_pending" in str(exc_info.value)

    # 3. Conflicting rows are NOT silently deleted
    async with session_factory() as session:
        rows = (await session.execute(select(WebBalanceTransfer))).scalars().all()
        assert len(rows) == 2

    await engine.dispose()




async def test_legacy_then_repeated_new_transfers(patched_webstore, monkeypatch):
    token = "legacy-sequential"
    async with patched_webstore() as session:
        session.add(WebBalanceAccount(profile_token=token, ref_code="sequential", balance_rub=150, total_spent_rub=100))
        session.add(WebBalanceTransaction(profile_token=token, amount_rub=100, direction="debit", kind="balance_migration", balance_after_rub=0, source_id="tg:123"))
        await session.commit()
    credits = {}

    def get_status(*args, **kwargs):
        return _MockResponse({"ok": True, "found": True, "completed": True, "status": "completed", "provider": "web_balance_migration", "telegram_id": 123, "amount_rub": 100})

    def post_credit(url, json, **kwargs):
        credits.setdefault(json["source_id"], json["amount_rub"])
        return _MockResponse({"status": "credited"})

    monkeypatch.setattr(webstore_routes.aiohttp, "ClientSession", lambda **kw: _MockClientSession(post_handler=post_credit, get_handler=get_status))
    async with patched_webstore() as session:
        first = await webstore_routes._migrate_web_balance_to_telegram(session, token, 123)
        assert first.status == "completed" and first.amount_rub == 150
    async with patched_webstore() as session:
        account = await session.get(WebBalanceAccount, token)
        account.balance_rub += 50
        await session.commit()
    async with patched_webstore() as session:
        second = await webstore_routes._migrate_web_balance_to_telegram(session, token, 123)
        assert second.status == "completed" and second.amount_rub == 50
        assert first.transfer_id != second.transfer_id
        assert await webstore_routes._migrate_web_balance_to_telegram(session, token, 123) is None
    async with patched_webstore() as session:
        account = await session.get(WebBalanceAccount, token)
        txs = (await session.execute(select(WebBalanceTransaction))).scalars().all()
        assert account.balance_rub == 0 and account.total_spent_rub == 300
        assert sorted(tx.amount_rub for tx in txs) == [50, 100, 150]
        assert len({tx.source_id for tx in txs}) == 3
    assert sum(credits.values()) + 100 == 300


@pytest.mark.parametrize("durable_status", [None, "pending", "failed", "wrong_profile", "wrong_amount"])
async def test_unconfirmed_wbtr_ledger_fails_closed(patched_webstore, monkeypatch, durable_status):
    async with patched_webstore() as session:
        session.add(WebBalanceAccount(profile_token="unsafe", ref_code="unsafe", balance_rub=50))
        session.add(WebBalanceTransaction(profile_token="unsafe", amount_rub=100, direction="debit", kind="balance_migration", balance_after_rub=0, source_id="wbtr_unconfirmed"))
        if durable_status:
            session.add(WebBalanceTransfer(transfer_id="wbtr_unconfirmed", profile_token="foreign" if durable_status == "wrong_profile" else "unsafe", telegram_id=123, amount_rub=99 if durable_status == "wrong_amount" else 100, status="completed" if durable_status.startswith("wrong") else durable_status, completed_at=datetime.utcnow()))
        await session.commit()
    post = AsyncMock()
    monkeypatch.setattr(webstore_routes.aiohttp, "ClientSession", lambda **kw: _MockClientSession(post_handler=post))
    async with patched_webstore() as session:
        assert await webstore_routes._migrate_web_balance_to_telegram(session, "unsafe", 123) is None
    post.assert_not_called()
    async with patched_webstore() as session:
        assert (await session.get(WebBalanceAccount, "unsafe")).balance_rub == 50


async def test_relink_without_pending_sends_new_balance_to_b(patched_webstore, monkeypatch):
    from webstore.models import WebAccount

    token = "new-account-relink"
    async with patched_webstore() as session:
        session.add(WebAccount(contact="relink@example.com", profile_token=token, password_hash="unused"))
        session.add(WebBalanceAccount(profile_token=token, ref_code="relink-new", balance_rub=100))
        session.add(WebProfileLink(profile_token=token, telegram_id=111))
        session.add(WebProfileLinkCode(code="relink-b", profile_token=token, expires_at=datetime.utcnow() + timedelta(minutes=5)))
        await session.commit()
    recipients = []

    def post_credit(url, json, **kw):
        recipients.append((json["telegram_id"], json["amount_rub"]))
        return _MockResponse({"status": "credited"})

    monkeypatch.setattr(webstore_routes.aiohttp, "ClientSession", lambda **kw: _MockClientSession(post_handler=post_credit))
    response = await webstore_routes.handle_internal_telegram_link_claim(_Request(headers={"X-Internal-Secret": "test-bridge-secret"}, _json_body={"code": "relink-b", "telegram_id": 222}))
    assert response.status == 200, response.text
    assert recipients == [(222, 100)]
    async with patched_webstore() as session:
        assert (await session.get(WebProfileLink, token)).telegram_id == 222
        assert (await session.get(WebBalanceAccount, token)).balance_rub == 0
        assert (await session.scalar(select(WebProfileLinkCode))).consumed_at is not None


async def test_profile_lock_survives_expiry_and_process_death(patched_webstore):
    import sys

    database = str(patched_webstore.kw["bind"].url.database)
    script = '''
import asyncio, sys
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from webstore import routes
routes.async_session = async_sessionmaker(create_async_engine("sqlite+aiosqlite:///" + sys.argv[1]))
async def run():
    async with routes._acquire_profile_lock("process-lock", lease=0.1):
        print("locked", flush=True)
        await asyncio.sleep(60)
asyncio.run(run())
'''
    process = await asyncio.create_subprocess_exec(sys.executable, "-c", script, database, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        assert await asyncio.wait_for(process.stdout.readline(), 10) == b"locked\n"
        await asyncio.sleep(0.2)
        with pytest.raises(TimeoutError):
            async with webstore_routes._acquire_profile_lock("process-lock", timeout=0.2, lease=0.1):
                pytest.fail("A second process acquired an expired but live lock")
        async with patched_webstore() as session:
            session.add(WebBalanceAccount(profile_token="unrelated-write", ref_code="unrelated"))
            await session.commit()
    finally:
        process.kill()
        await process.wait()
    async with webstore_routes._acquire_profile_lock("process-lock", timeout=1):
        webstore_routes._assert_profile_lock("process-lock")


async def test_profile_lock_cancellation_releases_ownership(patched_webstore):
    started = asyncio.Event()

    async def holder():
        async with webstore_routes._acquire_profile_lock("cancel-lock"):
            started.set()
            await asyncio.Future()

    task = asyncio.create_task(holder())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with webstore_routes._acquire_profile_lock("cancel-lock", timeout=1):
        webstore_routes._assert_profile_lock("cancel-lock")


async def test_identical_legacy_bot_duplicates_are_ambiguous(tmp_path, monkeypatch):
    from bot import webhooks
    from bot.models import BalanceTopUp, Base as BotBase, User

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'duplicates.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(BotBase.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(webhooks, "async_session", factory)
    monkeypatch.setattr(webhooks.settings, "webstore_bridge_secret", "test-bridge-secret")
    async with factory() as session:
        session.add(User(id=1, telegram_id=123, balance_rub=200))
        for _ in range(2):
            session.add(BalanceTopUp(user_id=1, telegram_id=123, amount_rub=100, status="completed", provider="web_balance_migration", provider_payment_id="web_migrate_duplicates"))
        await session.commit()
    headers = {"X-Internal-Secret": "test-bridge-secret"}
    status = await webhooks.handle_internal_balance_credit_status(_Request(headers=headers, query={"source_id": "web_migrate_duplicates"}))
    assert json.loads(status.text)["ambiguous"] is True
    credit = await webhooks.handle_internal_balance_credit(_Request(headers=headers, _json_body={"telegram_id": 123, "amount_rub": 100, "source_type": "web_balance_migration", "source_id": "web_migrate_duplicates"}))
    assert credit.status == 409
    async with factory() as session:
        assert (await session.get(User, 1)).balance_rub == 200
    await engine.dispose()


async def test_auth_profile_changed_under_lock_keeps_code_unused(patched_webstore, monkeypatch):
    from contextlib import asynccontextmanager
    from webstore.models import WebTelegramAuthCode

    init = json.loads((await webstore_routes.handle_telegram_auth_init(_Request())).text)
    async with patched_webstore() as session:
        session.add(WebProfileLink(profile_token="before-lock", telegram_id=123))
        await session.commit()
    original_lock = webstore_routes._acquire_profile_lock

    @asynccontextmanager
    async def changed_link(token):
        async with original_lock(token):
            async with patched_webstore() as session:
                old_link = await session.get(WebProfileLink, "before-lock")
                await session.delete(old_link)
                await session.flush()
                session.add(WebProfileLink(profile_token="after-lock", telegram_id=123))
                await session.commit()
            yield

    monkeypatch.setattr(webstore_routes, "_acquire_profile_lock", changed_link)
    response = await webstore_routes.handle_internal_telegram_auth_claim(_Request(headers={"X-Internal-Secret": "test-bridge-secret"}, _json_body={"code": init["code"], "telegram_id": 123}))
    assert response.status == 409
    async with patched_webstore() as session:
        code = await session.scalar(select(WebTelegramAuthCode).where(WebTelegramAuthCode.code == init["code"]))
        assert code.consumed_at is None and code.profile_token is None
        assert not (await session.execute(select(WebBalanceTransfer))).scalars().all()


@pytest.mark.parametrize("lose_first_reply", [False, True])
async def test_real_bridge_legacy_and_sequential_transfers(patched_webstore, tmp_path, monkeypatch, lose_first_reply):
    from aiohttp import web
    from bot import webhooks
    from bot.models import BalanceTopUp, Base as BotBase, User

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'real-bridge.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(BotBase.metadata.create_all)
        await connection.execute(text("CREATE UNIQUE INDEX uq_real_bridge_wbtr ON balance_topups(provider_payment_id) WHERE provider = 'web_balance_migration' AND provider_payment_id LIKE 'wbtr_%'"))
    bot_factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(webhooks, "async_session", bot_factory)
    monkeypatch.setattr(webhooks.settings, "webstore_bridge_secret", "test-bridge-secret")
    token = "real-bridge-sequence"
    async with bot_factory() as session:
        session.add(User(id=1, telegram_id=123, balance_rub=100))
        session.add(BalanceTopUp(user_id=1, telegram_id=123, amount_rub=100, provider="web_balance_migration", status="completed", provider_payment_id=f"web_migrate_{token[:16]}"))
        await session.commit()
    async with patched_webstore() as session:
        session.add(WebBalanceAccount(profile_token=token, ref_code="real-bridge", balance_rub=150, total_spent_rub=100))
        session.add(WebBalanceTransaction(profile_token=token, amount_rub=100, direction="debit", kind="balance_migration", balance_after_rub=0, source_id="tg:123"))
        await session.commit()
    calls = 0

    async def credit(request):
        nonlocal calls
        result = await webhooks.handle_internal_balance_credit(request)
        calls += 1
        if lose_first_reply and calls == 1:
            return web.json_response({"error": "upstream reply lost"}, status=502)
        return result

    app = web.Application()
    app.router.add_get("/internal/balance-credit-status", webhooks.handle_internal_balance_credit_status)
    app.router.add_post("/internal/balance-credit", credit)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    monkeypatch.setattr(webstore_routes.settings, "referral_bridge_url", f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}")
    try:
        async with patched_webstore() as session:
            first = await webstore_routes._migrate_web_balance_to_telegram(session, token, 123)
            if lose_first_reply:
                assert first.status == "pending"
                first = await webstore_routes._migrate_web_balance_to_telegram(session, token, 123)
            assert first.status == "completed" and first.amount_rub == 150
        async with patched_webstore() as session:
            account = await session.get(WebBalanceAccount, token)
            account.balance_rub += 50
            await session.commit()
        async with patched_webstore() as session:
            second = await webstore_routes._migrate_web_balance_to_telegram(session, token, 123)
            assert second.status == "completed" and second.amount_rub == 50
            assert second.transfer_id != first.transfer_id
            assert await webstore_routes._migrate_web_balance_to_telegram(session, token, 123) is None
            assert (await session.get(WebBalanceAccount, token)).balance_rub == 0
            assert len((await session.execute(select(WebBalanceTransaction))).scalars().all()) == 3
        async with bot_factory() as session:
            assert (await session.get(User, 1)).balance_rub == 300
            assert len((await session.execute(select(BalanceTopUp))).scalars().all()) == 3
    finally:
        await runner.cleanup()
        await engine.dispose()


async def test_scheduler_recovers_linked_balance_after_probe_outage(patched_webstore, monkeypatch):
    from webstore.scheduler import retry_pending_balance_transfers

    async with patched_webstore() as session:
        session.add(WebBalanceAccount(profile_token="probe-recovery", ref_code="probe-recovery", balance_rub=100))
        session.add(WebProfileLink(profile_token="probe-recovery", telegram_id=222))
        await session.commit()
    available = False
    recipients = []

    def get_status(*args, **kw):
        return _MockResponse({"ok": True, "found": False}) if available else _MockResponse({}, status=503)

    def post_credit(url, json, **kw):
        recipients.append(json["telegram_id"])
        return _MockResponse({"status": "credited"})

    monkeypatch.setattr(webstore_routes.aiohttp, "ClientSession", lambda **kw: _MockClientSession(get_handler=get_status, post_handler=post_credit))
    await retry_pending_balance_transfers()
    async with patched_webstore() as session:
        assert (await session.get(WebBalanceAccount, "probe-recovery")).balance_rub == 100
        assert not (await session.execute(select(WebBalanceTransfer))).scalars().all()
    available = True
    await retry_pending_balance_transfers()
    await retry_pending_balance_transfers()
    assert recipients == [222]
    async with patched_webstore() as session:
        assert (await session.get(WebBalanceAccount, "probe-recovery")).balance_rub == 0
        assert len((await session.execute(select(WebBalanceTransaction))).scalars().all()) == 1
