"""Comprehensive regression tests for Webstore -> Telegram Cashback Routing.

Covers Flows A through L:
A. Linked TG web renewal (TG balance credited, web balance untouched, notification sent)
B. Anonymous quick renewal using Adapt UUID (no WebProfileLink, owner resolved by UUID)
C. Duplicate callback (exactly one TG cashback transaction, no duplicate push)
D. _DELIVERED_WEB_ORDERS already contains order_id but cashback transaction is absent (cashback still credited)
E. Legacy order with existing web cashback (no TG credit, no second web credit, money untouched)
F. Pure web user, bot returns matched=False (WebBalanceAccount cashback works)
G. Bot bridge timeout/network error (NO web cashback fallback, remains pending, retry succeeds)
H. Cashback disabled (matched TG order -> no TG credit and no web fallback)
I. amount_rub=0 (no cashback, not_eligible)
J. Partial balance payment (cashback calculated from amount_rub only)
K. Two concurrent/repeated callbacks (durable DB idempotency prevents double credit)
L. Scheduler retry of pending cashback orders
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.models import (
    Base as BotBase,
    AdaptSubscription,
    BalanceTransaction,
    BalanceTransactionKind,
    BotSettings,
    Platform,
    Server,
    Subscription,
    SubStatus,
    User,
)
from bot.services.payment_service import credit_cashback
from bot.webhooks import handle_internal_web_order_delivered, _DELIVERED_WEB_ORDERS
from webstore.models import (
    Base as WebBase,
    WebBalanceAccount,
    WebBalanceTransaction,
    WebOrder,
    WebProfileLink,
)
from webstore.routes import _finalize_order_cashback, _get_order_adapt_uuid, _notify_bot_order_delivered, settings as web_settings
from webstore.scheduler import retry_pending_cashback_orders


@pytest_asyncio.fixture
async def bot_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(BotBase.metadata.create_all)
        # Create partial unique index
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_balance_tx_web_cashback "
                "ON balance_transactions (source_type, source_id) "
                "WHERE kind = 'CASHBACK' AND source_type = 'web_order_cashback' AND source_id IS NOT NULL;"
            )
        )
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def web_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(WebBase.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def bot_session(bot_engine):
    maker = async_sessionmaker(bot_engine, expire_on_commit=False)
    async with maker() as session:
        # Default bot settings: cashback enabled 10%
        session.add(BotSettings(key="cashback_enabled", value="1"))
        session.add(BotSettings(key="cashback_percent", value="10"))
        await session.commit()
    return maker


@pytest_asyncio.fixture
async def web_session(web_engine):
    maker = async_sessionmaker(web_engine, expire_on_commit=False)
    return maker


def _make_mock_bot():
    bot = AsyncMock()
    bot.send_message = AsyncMock()
    return bot


def _make_web_order(**kwargs) -> WebOrder:
    defaults = {
        "tariff_key": "basic",
        "tariff_label": "Базовый • 30 дн",
        "days": 30,
        "amount_rub": 100,
        "status": "delivered",
        "cashback_status": "pending",
    }
    defaults.update(kwargs)
    return WebOrder(**defaults)


def _make_subscription(**kwargs) -> Subscription:
    defaults = {
        "tariff_months": 1,
        "platform": Platform.ANDROID,
        "status": SubStatus.ACTIVE,
        "expires_at": datetime.utcnow() + timedelta(days=30),
    }
    defaults.update(kwargs)
    return Subscription(**defaults)


def _make_user(**kwargs) -> User:
    defaults = {
        "platform": Platform.ANDROID,
        "balance_rub": 0.0,
    }
    defaults.update(kwargs)
    return User(**defaults)


def _make_adapt_subscription(**kwargs) -> AdaptSubscription:
    defaults = {
        "adapt_plan_uuid": "plan-1",
    }
    defaults.update(kwargs)
    return AdaptSubscription(**defaults)


# ---------------------------------------------------------------------------
# Flow A: Linked TG web renewal
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_flow_a_linked_tg_web_renewal(bot_session, web_session):
    mock_bot = _make_mock_bot()
    _DELIVERED_WEB_ORDERS.clear()

    # 1. Setup bot DB: user + subscription + adapt record
    async with bot_session() as b_session:
        user = _make_user(telegram_id=111001, username="test_user_a")
        b_session.add(user)
        server = Server(name="NL-A", host="1.1.1.1", location="NL")
        b_session.add(server)
        await b_session.commit()
        await b_session.refresh(user)
        await b_session.refresh(server)

        sub = _make_subscription(
            user_id=user.id,
            server_id=server.id,
            client_name="adapt_uuid-flow-a",
            expires_at=datetime.utcnow() + timedelta(days=5),
        )
        b_session.add(sub)
        await b_session.commit()
        await b_session.refresh(sub)

        adapt_sub = _make_adapt_subscription(
            subscription_id=sub.id,
            adapt_uuid="uuid-flow-a",
            end_date=datetime.utcnow() + timedelta(days=5),
        )
        b_session.add(adapt_sub)
        await b_session.commit()

    # 2. Setup web DB: web order
    async with web_session() as w_session:
        order = _make_web_order(
            order_id="order_flow_a",
            amount_rub=200.0,
            original_amount_rub=200.0,
            marzban_username="adapt_uuid-flow-a",
        )
        w_session.add(order)
        await w_session.commit()

    # 3. Simulate bot webhook call
    req = MagicMock()
    req.app = {"bot": mock_bot}
    req.headers = {"X-Internal-Secret": "secret"}
    req.json = AsyncMock(return_value={
        "order_id": "order_flow_a",
        "adapt_uuid": "uuid-flow-a",
        "marzban_username": "adapt_uuid-flow-a",
        "amount_rub": 200.0,
        "cashback_eligible": True,
    })

    with patch("bot.webhooks._verify_internal_secret", return_value=True), \
         patch("bot.webhooks.async_session", bot_session):
        resp = await handle_internal_web_order_delivered(req)

    import json
    data = json.loads(resp.text)
    assert data["ok"] is True
    assert data["matched"] is True
    assert data["cashback_status"] == "credited"
    assert data["cashback_rub"] == 20.0

    # 4. Finalize cashback on webstore side
    async with web_session() as w_session:
        db_order = await w_session.scalar(select(WebOrder).where(WebOrder.order_id == "order_flow_a"))
        status = await _finalize_order_cashback(w_session, db_order, data)
        await w_session.commit()
        assert status == "telegram_credited"
        assert db_order.cashback_status == "telegram_credited"
        assert db_order.cashback_amount_rub == 20

        # Verify WebBalanceAccount did NOT receive cashback
        web_txs = (await w_session.execute(select(WebBalanceTransaction))).scalars().all()
        assert len(web_txs) == 0

    # 5. Verify Bot DB state
    async with bot_session() as b_session:
        db_user = await b_session.get(User, user.id)
        assert float(db_user.balance_rub) == 20.0

        tx = await b_session.scalar(
            select(BalanceTransaction).where(BalanceTransaction.user_id == user.id)
        )
        assert tx is not None
        assert tx.kind == BalanceTransactionKind.CASHBACK
        assert float(tx.amount_rub) == 20.0
        assert tx.source_type == "web_order_cashback"
        assert tx.source_id == "order_flow_a"

    # 6. Telegram cashback notification sent
    mock_bot.send_message.assert_called()
    msg_args = mock_bot.send_message.call_args[0]
    assert 111001 in msg_args
    assert "кешбэк" in msg_args[1].lower()


# ---------------------------------------------------------------------------
# Flow B: Anonymous quick renewal using Adapt UUID copied from Telegram
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_flow_b_anonymous_quick_renewal(bot_session, web_session):
    mock_bot = _make_mock_bot()
    _DELIVERED_WEB_ORDERS.clear()

    async with bot_session() as b_session:
        user = _make_user(telegram_id=222002, username="anon_user")
        b_session.add(user)
        server = Server(name="NL-B", host="1.1.1.2", location="NL")
        b_session.add(server)
        await b_session.commit()
        await b_session.refresh(user)
        await b_session.refresh(server)

        sub = _make_subscription(
            user_id=user.id,
            server_id=server.id,
            client_name="adapt_b8a1c9e2-4f3d-4c5e-8b1a-9f0e1d2c3b4a",
            vpn_key="https://vpn.example/sub/b8a1c9e2-4f3d-4c5e-8b1a-9f0e1d2c3b4a",
        )
        b_session.add(sub)
        await b_session.commit()
        await b_session.refresh(sub)

        adapt_sub = _make_adapt_subscription(
            subscription_id=sub.id,
            adapt_uuid="b8a1c9e2-4f3d-4c5e-8b1a-9f0e1d2c3b4a",
        )
        b_session.add(adapt_sub)
        await b_session.commit()

    # Web order has NO profile link - pure anonymous quick renewal
    async with web_session() as w_session:
        order = _make_web_order(
            order_id="order_flow_b",
            amount_rub=300.0,
            subscription_url="https://vpn.example/sub/b8a1c9e2-4f3d-4c5e-8b1a-9f0e1d2c3b4a",
        )
        w_session.add(order)
        await w_session.commit()

    req = MagicMock()
    req.app = {"bot": mock_bot}
    req.headers = {"X-Internal-Secret": "secret"}
    req.json = AsyncMock(return_value={
        "order_id": "order_flow_b",
        "subscription_url": "https://vpn.example/sub/b8a1c9e2-4f3d-4c5e-8b1a-9f0e1d2c3b4a",
        "amount_rub": 300.0,
        "cashback_eligible": True,
    })

    with patch("bot.webhooks._verify_internal_secret", return_value=True), \
         patch("bot.webhooks.async_session", bot_session):
        resp = await handle_internal_web_order_delivered(req)

    import json
    data = json.loads(resp.text)
    assert data["ok"] is True
    assert data["matched"] is True
    assert data["cashback_status"] == "credited"
    assert data["cashback_rub"] == 30.0

    async with web_session() as w_session:
        db_order = await w_session.scalar(select(WebOrder).where(WebOrder.order_id == "order_flow_b"))
        status = await _finalize_order_cashback(w_session, db_order, data)
        await w_session.commit()
        assert status == "telegram_credited"

    async with bot_session() as b_session:
        db_user = await b_session.get(User, user.id)
        assert float(db_user.balance_rub) == 30.0


# ---------------------------------------------------------------------------
# Flow C: Duplicate callback
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_flow_c_duplicate_callback(bot_session):
    mock_bot = _make_mock_bot()
    _DELIVERED_WEB_ORDERS.clear()

    async with bot_session() as b_session:
        user = _make_user(telegram_id=333003, username="dup_user")
        b_session.add(user)
        server = Server(name="NL-C", host="1.1.1.3", location="NL")
        b_session.add(server)
        await b_session.commit()

        sub = _make_subscription(
            user_id=user.id,
            server_id=server.id,
            client_name="adapt_uuid-flow-c",
        )
        b_session.add(sub)
        await b_session.commit()

        adapt_sub = _make_adapt_subscription(
            subscription_id=sub.id,
            adapt_uuid="uuid-flow-c",
        )
        b_session.add(adapt_sub)
        await b_session.commit()

    req = MagicMock()
    req.app = {"bot": mock_bot}
    req.headers = {"X-Internal-Secret": "secret"}
    req.json = AsyncMock(return_value={
        "order_id": "order_flow_c",
        "adapt_uuid": "uuid-flow-c",
        "amount_rub": 100.0,
        "cashback_eligible": True,
    })

    # Call 1
    with patch("bot.webhooks._verify_internal_secret", return_value=True), \
         patch("bot.webhooks.async_session", bot_session):
        resp1 = await handle_internal_web_order_delivered(req)

    import json
    data1 = json.loads(resp1.text)
    assert data1["cashback_status"] == "credited"
    assert data1["cashback_rub"] == 10.0

    # Call 2 (Duplicate)
    with patch("bot.webhooks._verify_internal_secret", return_value=True), \
         patch("bot.webhooks.async_session", bot_session):
        resp2 = await handle_internal_web_order_delivered(req)

    data2 = json.loads(resp2.text)
    assert data2["cashback_status"] == "already_credited"
    assert data2["cashback_rub"] == 10.0

    # Verify balance was NOT credited twice
    async with bot_session() as b_session:
        db_user = await b_session.get(User, user.id)
        assert float(db_user.balance_rub) == 10.0

        txs = (await b_session.execute(
            select(BalanceTransaction).where(BalanceTransaction.user_id == user.id)
        )).scalars().all()
        assert len(txs) == 1


# ---------------------------------------------------------------------------
# Flow D: Memory dedupe already has order_id, but cashback transaction absent
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_flow_d_memory_dedupe_bypassed_for_cashback(bot_session):
    mock_bot = _make_mock_bot()
    _DELIVERED_WEB_ORDERS.clear()
    _DELIVERED_WEB_ORDERS.add("order_flow_d")  # Pretend delivery message was sent previously

    async with bot_session() as b_session:
        user = _make_user(telegram_id=444004, username="dedupe_user")
        b_session.add(user)
        server = Server(name="NL-D", host="1.1.1.4", location="NL")
        b_session.add(server)
        await b_session.commit()

        sub = _make_subscription(
            user_id=user.id,
            server_id=server.id,
            client_name="adapt_uuid-flow-d",
        )
        b_session.add(sub)
        await b_session.commit()

        adapt_sub = _make_adapt_subscription(
            subscription_id=sub.id,
            adapt_uuid="uuid-flow-d",
        )
        b_session.add(adapt_sub)
        await b_session.commit()

    req = MagicMock()
    req.app = {"bot": mock_bot}
    req.headers = {"X-Internal-Secret": "secret"}
    req.json = AsyncMock(return_value={
        "order_id": "order_flow_d",
        "adapt_uuid": "uuid-flow-d",
        "amount_rub": 200.0,
        "cashback_eligible": True,
    })

    with patch("bot.webhooks._verify_internal_secret", return_value=True), \
         patch("bot.webhooks.async_session", bot_session):
        resp = await handle_internal_web_order_delivered(req)

    import json
    data = json.loads(resp.text)
    assert data["ok"] is True
    assert data["matched"] is True
    assert data["duplicate"] is True  # Delivery was deduped
    assert data["cashback_status"] == "credited"  # Cashback was recovered!
    assert data["cashback_rub"] == 20.0

    async with bot_session() as b_session:
        db_user = await b_session.get(User, user.id)
        assert float(db_user.balance_rub) == 20.0


# ---------------------------------------------------------------------------
# Flow E: Legacy order already has web cashback transaction
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_flow_e_legacy_order_with_web_cashback(bot_session, web_session):
    mock_bot = _make_mock_bot()
    _DELIVERED_WEB_ORDERS.clear()

    async with bot_session() as b_session:
        user = _make_user(telegram_id=555005, username="legacy_user")
        b_session.add(user)
        server = Server(name="NL-E", host="1.1.1.5", location="NL")
        b_session.add(server)
        await b_session.commit()

        sub = _make_subscription(
            user_id=user.id,
            server_id=server.id,
            client_name="adapt_uuid-flow-e",
        )
        b_session.add(sub)
        await b_session.commit()

        adapt_sub = _make_adapt_subscription(
            subscription_id=sub.id,
            adapt_uuid="uuid-flow-e",
        )
        b_session.add(adapt_sub)
        await b_session.commit()

    async with web_session() as w_session:
        order = _make_web_order(
            order_id="order_flow_e",
            amount_rub=100.0,
            marzban_username="adapt_uuid-flow-e",
            cashback_status="web_credited",
        )
        w_session.add(order)
        # Pre-existing web balance transaction
        account = WebBalanceAccount(profile_token="tok_e", ref_code="ref_e", balance_rub=10.0)
        w_session.add(account)
        await w_session.flush()

        tx = WebBalanceTransaction(
            profile_token="tok_e",
            amount_rub=10.0,
            direction="credit",
            kind="cashback",
            balance_after_rub=10.0,
            source_id="order_flow_e",
        )
        w_session.add(tx)
        await w_session.commit()

    # Bot notify payload should have cashback_eligible=False
    req = MagicMock()
    req.app = {"bot": mock_bot}
    req.headers = {"X-Internal-Secret": "secret"}
    req.json = AsyncMock(return_value={
        "order_id": "order_flow_e",
        "adapt_uuid": "uuid-flow-e",
        "amount_rub": 100.0,
        "cashback_eligible": False,  # protected!
    })

    with patch("bot.webhooks._verify_internal_secret", return_value=True), \
         patch("bot.webhooks.async_session", bot_session):
        resp = await handle_internal_web_order_delivered(req)

    import json
    data = json.loads(resp.text)
    assert data["cashback_status"] == "web_already_credited"

    # Webstore finalizer should keep web_credited
    async with web_session() as w_session:
        db_order = await w_session.scalar(select(WebOrder).where(WebOrder.order_id == "order_flow_e"))
        status = await _finalize_order_cashback(w_session, db_order, data)
        await w_session.commit()
        assert status == "web_credited"

    # User in Bot DB should NOT receive cashback
    async with bot_session() as b_session:
        db_user = await b_session.get(User, user.id)
        assert float(db_user.balance_rub) == 0.0


# ---------------------------------------------------------------------------
# Flow F: Pure web user, bot returns matched=False
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_flow_f_pure_web_user_unmatched(web_session):
    async with web_session() as w_session:
        order = _make_web_order(
            order_id="order_flow_f",
            amount_rub=200.0,
            profile_token="pure_web_token",
            contact="pure@test.com",
        )
        w_session.add(order)
        await w_session.commit()

    bot_result = {
        "ok": True,
        "matched": False,
        "delivered": False,
        "cashback_status": "not_eligible",
    }

    with patch("webstore.routes._fetch_public_balance_config", return_value={"cashback_enabled": True, "cashback_percent": 10.0}):
        async with web_session() as w_session:
            db_order = await w_session.scalar(select(WebOrder).where(WebOrder.order_id == "order_flow_f"))
            status = await _finalize_order_cashback(w_session, db_order, bot_result)
            await w_session.commit()

            assert status == "web_credited"
            assert db_order.cashback_status == "web_credited"
            assert db_order.cashback_amount_rub == 20

            # Check WebBalanceAccount received credit
            acc = await w_session.scalar(
                select(WebBalanceAccount).where(WebBalanceAccount.profile_token == "pure_web_token")
            )
            assert acc is not None
            assert float(acc.balance_rub) == 20.0


# ---------------------------------------------------------------------------
# Flow G: Bot bridge timeout/network error
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_flow_g_bridge_timeout_keeps_pending(web_session):
    async with web_session() as w_session:
        order = _make_web_order(
            order_id="order_flow_g",
            amount_rub=200.0,
            profile_token="tok_g",
        )
        w_session.add(order)
        await w_session.commit()

    # 1. Bot bridge failed / 500 / timeout
    bot_result = {"ok": False, "delivered": False, "error": "Connection timeout"}

    async with web_session() as w_session:
        db_order = await w_session.scalar(select(WebOrder).where(WebOrder.order_id == "order_flow_g"))
        status = await _finalize_order_cashback(w_session, db_order, bot_result)
        await w_session.commit()

        # Crucial check: never credit web balance on bridge failure
        assert status == "pending"
        assert db_order.cashback_status == "pending"
        txs = (await w_session.execute(select(WebBalanceTransaction))).scalars().all()
        assert len(txs) == 0

    # 2. Later successful retry via scheduler or webhook
    successful_bot_result = {
        "ok": True,
        "matched": True,
        "delivered": True,
        "cashback_status": "credited",
        "cashback_rub": 20.0,
    }

    async with web_session() as w_session:
        db_order = await w_session.scalar(select(WebOrder).where(WebOrder.order_id == "order_flow_g"))
        status2 = await _finalize_order_cashback(w_session, db_order, successful_bot_result)
        await w_session.commit()

        assert status2 == "telegram_credited"
        assert db_order.cashback_status == "telegram_credited"
        assert db_order.cashback_amount_rub == 20


# ---------------------------------------------------------------------------
# Flow H: Cashback disabled in bot settings
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_flow_h_cashback_disabled(bot_session, web_session):
    mock_bot = _make_mock_bot()
    _DELIVERED_WEB_ORDERS.clear()

    # Set cashback disabled
    async with bot_session() as b_session:
        setting = await b_session.get(BotSettings, "cashback_enabled")
        setting.value = "0"
        user = _make_user(telegram_id=888008)
        b_session.add(user)
        server = Server(name="NL-H", host="1.1.1.8", location="NL")
        b_session.add(server)
        await b_session.commit()

        sub = _make_subscription(user_id=user.id, server_id=server.id, client_name="adapt_uuid-flow-h")
        b_session.add(sub)
        await b_session.commit()
        adapt_sub = _make_adapt_subscription(subscription_id=sub.id, adapt_uuid="uuid-flow-h")
        b_session.add(adapt_sub)
        await b_session.commit()

    async with web_session() as w_session:
        order = _make_web_order(
            order_id="order_flow_h",
            amount_rub=200.0,
            profile_token="tok_h",
        )
        w_session.add(order)
        await w_session.commit()

    req = MagicMock()
    req.app = {"bot": mock_bot}
    req.headers = {"X-Internal-Secret": "secret"}
    req.json = AsyncMock(return_value={
        "order_id": "order_flow_h",
        "adapt_uuid": "uuid-flow-h",
        "amount_rub": 200.0,
        "cashback_eligible": True,
    })

    with patch("bot.webhooks._verify_internal_secret", return_value=True), \
         patch("bot.webhooks.async_session", bot_session):
        resp = await handle_internal_web_order_delivered(req)

    import json
    data = json.loads(resp.text)
    assert data["matched"] is True
    assert data["cashback_status"] == "disabled"

    # Webstore coordinator marks disabled, NO fallback to web balance!
    async with web_session() as w_session:
        db_order = await w_session.scalar(select(WebOrder).where(WebOrder.order_id == "order_flow_h"))
        status = await _finalize_order_cashback(w_session, db_order, data)
        await w_session.commit()

        assert status == "disabled"
        assert db_order.cashback_status == "disabled"
        assert db_order.cashback_amount_rub == 0
        txs = (await w_session.execute(select(WebBalanceTransaction))).scalars().all()
        assert len(txs) == 0

    async with bot_session() as b_session:
        db_user = await b_session.get(User, user.id)
        assert float(db_user.balance_rub) == 0.0


# ---------------------------------------------------------------------------
# Flow I: amount_rub = 0
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_flow_i_zero_rub_order(web_session):
    async with web_session() as w_session:
        order = _make_web_order(
            order_id="order_flow_i",
            amount_rub=0.0,
            original_amount_rub=100.0,
        )
        w_session.add(order)
        await w_session.commit()

        db_order = await w_session.scalar(select(WebOrder).where(WebOrder.order_id == "order_flow_i"))
        status = await _finalize_order_cashback(w_session, db_order, {"ok": True, "matched": True})
        await w_session.commit()

        assert status == "not_eligible"
        assert db_order.cashback_status == "not_eligible"
        assert db_order.cashback_amount_rub == 0


# ---------------------------------------------------------------------------
# Flow J: Partial balance payment (real rub paid is less than original)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_flow_j_partial_balance_payment(bot_session):
    async with bot_session() as b_session:
        user = _make_user(telegram_id=999009)
        b_session.add(user)
        await b_session.commit()

        # Tariff was 200 ₽, user paid 100 ₽ from balance and 100 ₽ real money
        res = await credit_cashback(
            session=b_session,
            user_db_id=user.id,
            payment_id=None,
            amount_rub=100.0,  # real money paid only
            source_type="web_order_cashback",
            source_id="order_flow_j",
        )
        await b_session.commit()

        assert res["status"] == "credited"
        assert res["cashback_rub"] == 10.0  # 10% of 100 ₽, NOT 200 ₽!
        assert float(user.balance_rub) == 10.0


# ---------------------------------------------------------------------------
# Flow K: Durable DB idempotency / concurrent handling
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_flow_k_durable_db_idempotency_concurrent(bot_session):
    async with bot_session() as b_session:
        user = _make_user(telegram_id=777007)
        b_session.add(user)
        await b_session.commit()

    # 1. First credit succeeds
    async with bot_session() as session1:
        res1 = await credit_cashback(
            session=session1,
            user_db_id=user.id,
            payment_id=None,
            amount_rub=150.0,
            source_type="web_order_cashback",
            source_id="order_flow_k",
        )
        await session1.commit()
        assert res1["status"] == "credited"

    # 2. Second credit hits unique partial index or select check
    async with bot_session() as session2:
        res2 = await credit_cashback(
            session=session2,
            user_db_id=user.id,
            payment_id=None,
            amount_rub=150.0,
            source_type="web_order_cashback",
            source_id="order_flow_k",
        )
        await session2.commit()
        assert res2["status"] == "already_credited"

    # Verify only 1 transaction exists in DB
    async with bot_session() as b_session:
        txs = (await b_session.execute(
            select(BalanceTransaction).where(
                BalanceTransaction.user_id == user.id,
                BalanceTransaction.source_type == "web_order_cashback",
                BalanceTransaction.source_id == "order_flow_k",
            )
        )).scalars().all()
        assert len(txs) == 1
        db_user = await b_session.get(User, user.id)
        assert float(db_user.balance_rub) == 15.0


# ---------------------------------------------------------------------------
# Flow L: Scheduler retry of pending cashback orders
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_flow_l_scheduler_retry_pending_cashback(web_session):
    async with web_session() as w_session:
        order = _make_web_order(
            order_id="order_flow_l",
            amount_rub=250.0,
        )
        w_session.add(order)
        await w_session.commit()

    mock_notify = AsyncMock(return_value={
        "ok": True,
        "matched": True,
        "delivered": True,
        "cashback_status": "credited",
        "cashback_rub": 25.0,
    })

    with patch("webstore.scheduler.async_session", web_session), \
         patch("webstore.scheduler._notify_bot_order_delivered", mock_notify):
        await retry_pending_cashback_orders()

    async with web_session() as w_session:
        db_order = await w_session.scalar(select(WebOrder).where(WebOrder.order_id == "order_flow_l"))
        assert db_order.cashback_status == "telegram_credited"
        assert db_order.cashback_amount_rub == 25


# ---------------------------------------------------------------------------
# Flow L (Part 2): Verify all delivery paths use the cashback coordinator
# ---------------------------------------------------------------------------
def test_all_delivered_paths_use_cashback_coordinator():
    """Verify that _apply_order_cashback is only called within _finalize_order_cashback.

    No delivery path (YooKassa, Adapt reconciliation, scheduler retry, admin fulfillment,
    bonus orders) may call _apply_order_cashback directly.
    """
    import ast
    from pathlib import Path

    routes_path = Path(__file__).resolve().parent.parent / "webstore" / "routes.py"
    scheduler_path = Path(__file__).resolve().parent.parent / "webstore" / "scheduler.py"

    for file_path in (routes_path, scheduler_path):
        source = file_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Inside _finalize_order_cashback, calling _apply_order_cashback is expected (Rule C)
                if node.name == "_finalize_order_cashback":
                    continue
                # For all other functions, check they never call _apply_order_cashback
                for child in ast.walk(node):
                    if isinstance(child, ast.Call):
                        func_name = None
                        if isinstance(child.func, ast.Name):
                            func_name = child.func.id
                        elif isinstance(child.func, ast.Attribute):
                            func_name = child.func.attr
                        if func_name == "_apply_order_cashback":
                            pytest.fail(
                                f"Function '{node.name}' in {file_path.name} directly calls "
                                f"'_apply_order_cashback'. All delivery paths must route through "
                                f"'_finalize_order_cashback'."
                            )


# ---------------------------------------------------------------------------
# Flow K (Part 2): Real concurrent callbacks race with independent DB connections
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_flow_k_part2_real_concurrent_callbacks_race(tmp_path):
    db_file = tmp_path / "test_concurrency_race.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"
    engine = create_async_engine(db_url, echo=False)

    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA journal_mode=WAL;"))
        await conn.execute(text("PRAGMA busy_timeout=5000;"))
        await conn.run_sync(BotBase.metadata.create_all)
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_balance_tx_web_cashback "
                "ON balance_transactions (source_type, source_id) "
                "WHERE kind = 'CASHBACK' AND source_type = 'web_order_cashback' AND source_id IS NOT NULL;"
            )
        )

    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with session_maker() as init_session:
        init_session.add(BotSettings(key="cashback_enabled", value="1"))
        init_session.add(BotSettings(key="cashback_percent", value="10"))
        user = _make_user(telegram_id=999888)
        init_session.add(user)
        server = Server(name="NL-RACE", host="1.1.1.9", location="NL")
        init_session.add(server)
        await init_session.commit()
        user_id = user.id
        sub = _make_subscription(user_id=user.id, server_id=server.id, client_name="adapt_race_uuid")
        init_session.add(sub)
        await init_session.commit()
        adapt_sub = _make_adapt_subscription(subscription_id=sub.id, adapt_uuid="race_uuid")
        init_session.add(adapt_sub)
        await init_session.commit()

    mock_bot = _make_mock_bot()
    barrier = asyncio.Barrier(2)

    async def _call_webhook(worker_id: int):
        await barrier.wait()
        req = MagicMock()
        req.app = {"bot": mock_bot}
        req.headers = {"X-Internal-Secret": "secret"}
        req.json = AsyncMock(return_value={
            "order_id": "order_concurrent_race",
            "adapt_uuid": "race_uuid",
            "amount_rub": 200.0,
            "cashback_eligible": True,
        })
        with patch("bot.webhooks._verify_internal_secret", return_value=True), \
             patch("bot.webhooks.async_session", session_maker), \
             patch("bot.handlers.payment.async_session", session_maker), \
             patch("bot.services.guide_service.async_session", session_maker):
            for attempt in range(5):
                try:
                    resp = await handle_internal_web_order_delivered(req)
                    import json
                    return json.loads(resp.text)
                except Exception as exc:
                    if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                        await asyncio.sleep(0.05 * (attempt + 1))
                        continue
                    raise

    res1, res2 = await asyncio.gather(_call_webhook(1), _call_webhook(2))

    statuses = {res1.get("cashback_status"), res2.get("cashback_status")}
    assert statuses == {"credited", "already_credited"}

    async with session_maker() as session:
        txs = (await session.execute(
            select(BalanceTransaction).where(
                BalanceTransaction.user_id == user_id,
                BalanceTransaction.source_type == "web_order_cashback",
                BalanceTransaction.source_id == "order_concurrent_race",
            )
        )).scalars().all()
        assert len(txs) == 1
        assert float(txs[0].amount_rub) == 20.0

        db_user = await session.get(User, user_id)
        assert float(db_user.balance_rub) == 20.0

    cashback_messages = [
        call for call in mock_bot.send_message.call_args_list
        if "кешбэк" in str(call).lower()
    ]
    assert len(cashback_messages) == 1

    await engine.dispose()


# ---------------------------------------------------------------------------
# Atomic Notification Ordering: Notification NEVER sent before durable commit
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_notification_never_sent_if_commit_fails(bot_session):
    mock_bot = _make_mock_bot()
    async with bot_session() as session:
        user = _make_user(telegram_id=666006)
        session.add(user)
        server = Server(name="NL-CRASH", host="1.1.1.6", location="NL")
        session.add(server)
        await session.commit()
        user_id = user.id

        sub = _make_subscription(user_id=user.id, server_id=server.id, client_name="adapt_crash_uuid")
        session.add(sub)
        await session.commit()
        adapt_sub = _make_adapt_subscription(subscription_id=sub.id, adapt_uuid="crash_uuid")
        session.add(adapt_sub)
        await session.commit()

    req = MagicMock()
    req.app = {"bot": mock_bot}
    req.headers = {"X-Internal-Secret": "secret"}
    req.json = AsyncMock(return_value={
        "order_id": "order_crash_test",
        "adapt_uuid": "crash_uuid",
        "amount_rub": 200.0,
        "cashback_eligible": True,
    })

    async def _failing_commit(self_sess):
        raise RuntimeError("DB commit crash")

    with patch.object(AsyncSession, "commit", _failing_commit), \
         patch("bot.webhooks._verify_internal_secret", return_value=True), \
         patch("bot.webhooks.async_session", bot_session):
        with pytest.raises(RuntimeError):
            await handle_internal_web_order_delivered(req)

    # Crucial invariant: Telegram notification must NEVER have been sent!
    cashback_messages = [
        call for call in mock_bot.send_message.call_args_list
        if "кешбэк" in str(call).lower()
    ]
    assert len(cashback_messages) == 0

    # Verify balance was NOT committed in DB
    async with bot_session() as session:
        db_user = await session.get(User, user_id)
        assert float(db_user.balance_rub) == 0.0

    # On subsequent retry, commit succeeds and notification is sent exactly ONCE
    with patch("bot.webhooks._verify_internal_secret", return_value=True), \
         patch("bot.webhooks.async_session", bot_session):
        resp = await handle_internal_web_order_delivered(req)
        import json
        data = json.loads(resp.text)
        assert data["cashback_status"] == "credited"

    cashback_messages = [
        call for call in mock_bot.send_message.call_args_list
        if "кешбэк" in str(call).lower()
    ]
    assert len(cashback_messages) == 1

    async with bot_session() as session:
        db_user = await session.get(User, user_id)
        assert float(db_user.balance_rub) == 20.0


# ---------------------------------------------------------------------------
# Safety Tests A, B, C, D: Migration from PRE-FEATURE DB and Restart
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_safety_a_b_c_d_pre_feature_migration_and_restart(tmp_path):
    db_file = tmp_path / "legacy_web.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"
    engine = create_async_engine(db_url, echo=False)

    # 1. Create PRE-FEATURE database schema WITHOUT cashback_status, cashback_amount_rub, cashback_processed_at
    from sqlalchemy import Table, MetaData
    meta = MetaData()
    cols = [c.copy() for c in WebOrder.__table__.columns if c.name not in ("cashback_status", "cashback_amount_rub", "cashback_processed_at")]
    Table("web_orders", meta, *cols)
    WebBalanceTransaction.__table__.to_metadata(meta)

    async with engine.begin() as conn:
        await conn.run_sync(meta.create_all)
        # Insert historical delivered order with existing web cashback
        await conn.execute(text(
            """
            INSERT INTO web_orders (order_id, profile_token, tariff_key, tariff_label, days, amount_rub, status, purchase_action, fulfillment_attempts, created_at)
            VALUES ('order_hist_with_cb', 'tok_1', 'basic', 'Basic', 30, 200, 'delivered', 'new', 0, CURRENT_TIMESTAMP);
            """
        ))
        await conn.execute(text(
            """
            INSERT INTO web_balance_transactions (profile_token, amount_rub, direction, kind, balance_after_rub, source_id, created_at)
            VALUES ('tok_1', 20, 'in', 'cashback', 20, 'order_hist_with_cb', CURRENT_TIMESTAMP);
            """
        ))
        # Insert historical delivered order WITHOUT existing cashback
        await conn.execute(text(
            """
            INSERT INTO web_orders (order_id, profile_token, tariff_key, tariff_label, days, amount_rub, status, purchase_action, fulfillment_attempts, created_at)
            VALUES ('order_hist_no_cb', 'tok_2', 'basic', 'Basic', 30, 200, 'delivered', 'new', 0, CURRENT_TIMESTAMP);
            """
        ))
        # Insert existing non-delivered order (e.g. status='paid')
        await conn.execute(text(
            """
            INSERT INTO web_orders (order_id, profile_token, tariff_key, tariff_label, days, amount_rub, status, purchase_action, fulfillment_attempts, created_at)
            VALUES ('order_paid_unfulfilled', 'tok_3', 'basic', 'Basic', 30, 200, 'paid', 'new', 0, CURRENT_TIMESTAMP);
            """
        ))

    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    # Run init_db for the first time (migration triggers on first introduction)
    import webstore.database as webstore_db
    with patch.object(webstore_db, "engine", engine), \
         patch.object(webstore_db, "async_session", session_maker):
        await webstore_db.init_db()

    async with session_maker() as session:
        # A. Historical delivered + existing web cashback -> web_credited + amount backfilled
        order_a = await session.scalar(select(WebOrder).where(WebOrder.order_id == "order_hist_with_cb"))
        assert order_a.cashback_status == "web_credited"
        assert order_a.cashback_amount_rub == 20

        # B. Historical delivered without cashback -> legacy_skipped
        order_b = await session.scalar(select(WebOrder).where(WebOrder.order_id == "order_hist_no_cb"))
        assert order_b.cashback_status == "legacy_skipped"

        # _finalize_order_cashback must treat legacy_skipped as terminal
        status = await _finalize_order_cashback(session, order_b, {"ok": True, "matched": True})
        assert status == "legacy_skipped"

        # C. Existing non-delivered order (paid/pending) -> remains pending
        order_c = await session.scalar(select(WebOrder).where(WebOrder.order_id == "order_paid_unfulfilled"))
        assert order_c.cashback_status == "pending"

        # D. Restart after migration: legitimate NEW delivered cashback_status=pending remains pending
        new_order = _make_web_order(
            order_id="order_new_legit_pending",
            status="delivered",
            cashback_status="pending",
            amount_rub=300,
        )
        session.add(new_order)
        await session.commit()

    # Simulate server restart (calling init_db again)
    with patch.object(webstore_db, "engine", engine), \
         patch.object(webstore_db, "async_session", session_maker):
        await webstore_db.init_db()

    async with session_maker() as session:
        reloaded_new = await session.scalar(select(WebOrder).where(WebOrder.order_id == "order_new_legit_pending"))
        assert reloaded_new.cashback_status == "pending", "Restart must NOT overwrite legitimate pending delivered orders to legacy_skipped"

        # Scheduler must NOT pick up legacy_skipped order_b
        mock_notify = AsyncMock()
        with patch("webstore.scheduler.async_session", session_maker), \
             patch("webstore.scheduler._notify_bot_order_delivered", mock_notify):
            await retry_pending_cashback_orders()

        called_order_ids = [call.args[0].order_id for call in mock_notify.call_args_list]
        assert "order_hist_no_cb" not in called_order_ids
        assert "order_hist_with_cb" not in called_order_ids
        assert "order_new_legit_pending" in called_order_ids

    await engine.dispose()


# ---------------------------------------------------------------------------
# Safety Tests E & F: cashback_only scheduler retry and no rollback of expiry
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_safety_e_and_f_cashback_only_scheduler_retry_and_no_rollback_of_expiry(bot_session, web_session):
    # Setup bot DB
    active_until = datetime(2027, 6, 1, 12, 0, 0)
    async with bot_session() as b_session:
        user = _make_user(telegram_id=888777)
        b_session.add(user)
        server = Server(name="NL-RETRY", host="1.1.1.8", location="NL")
        b_session.add(server)
        await b_session.commit()
        user_id = user.id

        sub = _make_subscription(
            user_id=user.id,
            server_id=server.id,
            client_name="adapt_uuid-retry-test",
            status=SubStatus.ACTIVE,
            billing_mode="tariff",
            expires_at=active_until,
        )
        b_session.add(sub)
        await b_session.commit()
        sub_id = sub.id

        adapt_sub = _make_adapt_subscription(
            subscription_id=sub.id,
            adapt_uuid="uuid-retry-test",
            end_date=active_until,
            is_frozen=False,
        )
        b_session.add(adapt_sub)
        await b_session.commit()

    # Web order delivered previously, but cashback is pending; order has OLD access_expires_at from 2025
    old_expiry = datetime(2025, 1, 1, 12, 0, 0)
    async with web_session() as w_session:
        order = _make_web_order(
            order_id="order_retry_safety",
            amount_rub=200.0,
            marzban_username="adapt_uuid-retry-test",
            access_expires_at=old_expiry,
            cashback_status="pending",
        )
        w_session.add(order)
        await w_session.commit()

    mock_bot = _make_mock_bot()
    mock_send_key = AsyncMock(return_value=True)

    # In _notify_bot_order_delivered, intercept HTTP call to route directly to handle_internal_web_order_delivered
    async def _mock_http_bridge(order, *, force=False, cashback_only=False, session=None):
        req = MagicMock()
        req.app = {"bot": mock_bot}
        req.headers = {"X-Internal-Secret": "secret"}
        req.json = AsyncMock(return_value={
            "order_id": order.order_id,
            "adapt_uuid": _get_order_adapt_uuid(order) or "",
            "marzban_username": order.marzban_username or "",
            "target_order_id": order.target_order_id or "",
            "subscription_url": order.subscription_url or "",
            "access_expires_at": order.access_expires_at.isoformat() if order.access_expires_at else None,
            "tariff_label": order.tariff_label or "",
            "days": order.days,
            "force": force,
            "cashback_only": cashback_only,
            "amount_rub": float(order.amount_rub),
            "cashback_eligible": True,
        })
        with patch("bot.webhooks._verify_internal_secret", return_value=True), \
             patch("bot.webhooks.async_session", bot_session), \
             patch("bot.handlers.payment._send_subscription_key_for_platform", mock_send_key):
            resp = await handle_internal_web_order_delivered(req)
            import json
            return json.loads(resp.text)

    with patch("webstore.scheduler.async_session", web_session), \
         patch("webstore.scheduler._notify_bot_order_delivered", side_effect=_mock_http_bridge):
        await retry_pending_cashback_orders()

    # E. Verify:
    # 1) _send_subscription_key_for_platform was NOT called
    mock_send_key.assert_not_called()
    # 2) No normal renewal message sent; only cashback message sent
    cashback_messages = [
        call for call in mock_bot.send_message.call_args_list
        if "кешбэк" in str(call).lower()
    ]
    assert len(cashback_messages) == 1
    renewal_messages = [
        call for call in mock_bot.send_message.call_args_list
        if "готово" in str(call).lower() or "инструкция" in str(call).lower()
    ]
    assert len(renewal_messages) == 0

    # 3) TG cashback was credited
    async with bot_session() as b_session:
        db_user = await b_session.get(User, user_id)
        assert float(db_user.balance_rub) == 20.0

        # F. Old historical order expiry (2025) did NOT roll active subscription expiry (2027) backwards
        db_sub = await b_session.get(Subscription, sub_id)
        assert db_sub.expires_at == active_until
        assert db_sub.status == SubStatus.ACTIVE
        assert db_sub.billing_mode == "tariff"

        db_adapt = await b_session.scalar(
            select(AdaptSubscription).where(AdaptSubscription.subscription_id == sub_id)
        )
        assert db_adapt.end_date == active_until
        assert db_adapt.is_frozen is False

    # Web order updated to telegram_credited
    async with web_session() as w_session:
        db_order = await w_session.scalar(select(WebOrder).where(WebOrder.order_id == "order_retry_safety"))
        assert db_order.cashback_status == "telegram_credited"
        assert db_order.cashback_amount_rub == 20


# ---------------------------------------------------------------------------
# Safety Test G: Failure while checking existing web cashback fails closed
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_safety_g_fail_closed_if_web_cashback_check_fails(web_session):
    async with web_session() as session:
        order = _make_web_order(
            order_id="order_db_uncertainty",
            amount_rub=200.0,
            cashback_status="pending",
        )
        session.add(order)
        await session.commit()

    failing_session = MagicMock()
    failing_session.scalar = AsyncMock(side_effect=RuntimeError("Web DB connection lost"))

    # When _notify_bot_order_delivered cannot determine if web cashback exists:
    res = await _notify_bot_order_delivered(order, session=failing_session)
    assert res["ok"] is False
    assert res.get("cashback_status") == "pending"

    # In _finalize_order_cashback:
    async with web_session() as session:
        db_order = await session.scalar(select(WebOrder).where(WebOrder.order_id == "order_db_uncertainty"))
        status = await _finalize_order_cashback(session, db_order, res)
        assert status == "pending"
        assert db_order.cashback_status == "pending"

        # Also verify if _finalize_order_cashback directly encounters an error querying WebBalanceTransaction
        failing_finalize_session = MagicMock()
        failing_finalize_session.scalar = AsyncMock(side_effect=RuntimeError("Transaction check failed"))
        status2 = await _finalize_order_cashback(failing_finalize_session, db_order, {"ok": False})
        assert status2 == "pending"
        assert db_order.cashback_status == "pending"


# ---------------------------------------------------------------------------
# Safety Test H: Malformed custom cashback template does not break payments
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_safety_h_malformed_cashback_notify_text(bot_session):
    mock_bot = _make_mock_bot()
    async with bot_session() as session:
        session.add(BotSettings(key="cashback_notify_text", value="Broken {unknown_field} template {missing_brace"))
        user = _make_user(telegram_id=777666)
        session.add(user)
        await session.commit()
        user_id = user.id

    async with bot_session() as session:
        res = await credit_cashback(
            session=session,
            user_db_id=user_id,
            payment_id=None,
            amount_rub=200.0,
            bot=mock_bot,
            source_type="web_order_cashback",
            source_id="order_bad_template",
        )
        await session.commit()

    assert res["status"] == "credited"
    assert res["cashback_rub"] == 20.0
    assert "Вам начислен кешбэк" in res["notify_text"]

    async with bot_session() as session:
        db_user = await session.get(User, user_id)
        assert float(db_user.balance_rub) == 20.0

    mock_bot.send_message.assert_called_once()
    msg = mock_bot.send_message.call_args[0][1]
    assert "Вам начислен кешбэк" in msg
    assert "+20.00 ₽" in msg
