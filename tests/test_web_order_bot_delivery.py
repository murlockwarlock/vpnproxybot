"""Tests for web order bot delivery, user resolution, and admin notifications."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import pytest_asyncio
from aiohttp import web

from bot.models import Base, AdaptSubscription, Server, Subscription, SubStatus, User, Platform
from webstore.models import WebOrder, WebProfileLink


@pytest_asyncio.fixture
async def sqlite_engine():
    """Create in-memory SQLite engine with all tables."""
    from sqlalchemy.ext.asyncio import create_async_engine
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.mark.asyncio
async def test_internal_web_order_resolve_user(sqlite_engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from bot.webhooks import handle_internal_web_order_resolve_user

    session_factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with session_factory() as session:
        server = Server(name="NL-1", host="127.0.0.1", location="Amsterdam")
        session.add(server)
        await session.commit()
        await session.refresh(server)

        user = User(
            telegram_id=5812603209,
            username="saiamirey",
            full_name="Margarita",
            platform=Platform.IOS,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)

        sub = Subscription(
            user_id=user.id,
            server_id=server.id,
            tariff_months=1,
            platform=Platform.IOS,
            status=SubStatus.ACTIVE,
            vpn_key="https://darimiru.ru/vpnbot/adapt-sub/9585d709-c8fd-4860-bc29-90790fc0cf1f",
            client_name="adapt_9585d709-c8fd-4860-bc29-90790fc0cf1f",
            expires_at=datetime.utcnow() + timedelta(days=10),
        )
        session.add(sub)
        await session.commit()
        await session.refresh(sub)

        adapt_sub = AdaptSubscription(
            subscription_id=sub.id,
            adapt_uuid="9585d709-c8fd-4860-bc29-90790fc0cf1f",
            adapt_plan_uuid="plan-90d",
            end_date=datetime.utcnow() + timedelta(days=10),
            is_frozen=False,
        )
        session.add(adapt_sub)
        await session.commit()

    req = MagicMock()
    req.headers = {"X-Internal-Secret": "test-secret"}
    req.query = {"adapt_uuid": "9585d709-c8fd-4860-bc29-90790fc0cf1f"}

    with patch("bot.webhooks._verify_internal_secret", return_value=True), \
         patch("bot.webhooks.async_session", session_factory):
        resp = await handle_internal_web_order_resolve_user(req)

    assert resp.status == 200
    import json
    data = json.loads(resp.text)
    assert data["ok"] is True
    assert data["user"]["telegram_id"] == 5812603209
    assert data["user"]["username"] == "saiamirey"
    assert data["user"]["full_name"] == "Margarita"
    assert data["user"]["subscription_id"] == sub.id


@pytest.mark.asyncio
async def test_internal_web_order_delivered_syncs_and_delivers(sqlite_engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from bot.webhooks import handle_internal_web_order_delivered, _DELIVERED_WEB_ORDERS

    _DELIVERED_WEB_ORDERS.clear()

    session_factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with session_factory() as session:
        server = Server(name="NL-2", host="127.0.0.1", location="Amsterdam")
        session.add(server)
        await session.commit()
        await session.refresh(server)

        user = User(
            telegram_id=5812603209,
            username="saiamirey",
            full_name="Margarita",
            platform=Platform.ANDROID,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)

        sub = Subscription(
            user_id=user.id,
            server_id=server.id,
            tariff_months=1,
            platform=Platform.ANDROID,
            status=SubStatus.EXPIRED,
            billing_mode="demo",
            vpn_key="https://darimiru.ru/vpnbot/adapt-sub/9585d709-c8fd-4860-bc29-90790fc0cf1f",
            client_name="adapt_9585d709-c8fd-4860-bc29-90790fc0cf1f",
            expires_at=datetime.utcnow() - timedelta(days=1),
        )
        session.add(sub)
        await session.commit()
        await session.refresh(sub)

        adapt_sub = AdaptSubscription(
            subscription_id=sub.id,
            adapt_uuid="9585d709-c8fd-4860-bc29-90790fc0cf1f",
            adapt_plan_uuid="plan-90d",
            end_date=datetime.utcnow() - timedelta(days=1),
            is_frozen=True,
        )
        session.add(adapt_sub)
        await session.commit()

    new_expiry = (datetime.utcnow() + timedelta(days=90)).isoformat()
    body = {
        "order_id": "order-abc-123",
        "adapt_uuid": "9585d709-c8fd-4860-bc29-90790fc0cf1f",
        "access_expires_at": new_expiry,
        "tariff_label": "Базовый • 90 дн",
    }

    mock_bot = AsyncMock()
    req = MagicMock()
    req.headers = {"X-Internal-Secret": "test-secret"}
    req.json = AsyncMock(return_value=body)
    req.app = {"bot": mock_bot}

    with patch("bot.webhooks._verify_internal_secret", return_value=True), \
         patch("bot.webhooks.async_session", session_factory), \
         patch("bot.handlers.payment._send_subscription_key_for_platform", new_callable=AsyncMock) as mock_send_key:
        mock_send_key.return_value = True
        resp = await handle_internal_web_order_delivered(req)

    assert resp.status == 200
    import json
    data = json.loads(resp.text)
    assert data["ok"] is True
    assert data["matched"] is True
    assert data["delivered"] is True
    assert data["telegram_id"] == 5812603209

    # Verify DB was synced
    async with session_factory() as session:
        updated_sub = await session.get(Subscription, sub.id)
        assert updated_sub.status == SubStatus.ACTIVE
        assert updated_sub.billing_mode == "tariff"
        updated_adapt = await session.get(AdaptSubscription, adapt_sub.id)
        assert updated_adapt.is_frozen is False

    # Verify duplicate call does not resend
    with patch("bot.webhooks._verify_internal_secret", return_value=True), \
         patch("bot.webhooks.async_session", session_factory), \
         patch("bot.handlers.payment._send_subscription_key_for_platform", new_callable=AsyncMock) as mock_send_key2:
        resp2 = await handle_internal_web_order_delivered(req)
        assert resp2.status == 200
        data2 = json.loads(resp2.text)
        assert data2["duplicate"] is True
        mock_send_key2.assert_not_called()

    # Verify force=True resends
    body["force"] = True
    req.json = AsyncMock(return_value=body)
    with patch("bot.webhooks._verify_internal_secret", return_value=True), \
         patch("bot.webhooks.async_session", session_factory), \
         patch("bot.handlers.payment._send_subscription_key_for_platform", new_callable=AsyncMock) as mock_send_key3:
        mock_send_key3.return_value = True
        resp3 = await handle_internal_web_order_delivered(req)
        assert resp3.status == 200
        data3 = json.loads(resp3.text)
        assert data3.get("duplicate") is not True
        mock_send_key3.assert_called_once()


@pytest.mark.asyncio
async def test_admin_notifications_include_client_in_bot():
    from webstore.routes import _format_telegram_user_line, _notify_admins_webstore

    # 1. Format test
    assert _format_telegram_user_line(None) == ""
    assert _format_telegram_user_line({"telegram_id": 123}) == "Клиент в боте: ID: 123\n"
    assert _format_telegram_user_line({
        "telegram_id": 5812603209,
        "username": "saiamirey",
        "full_name": "Margarita",
    }) == "Клиент в боте: Margarita (@saiamirey, ID: 5812603209)\n"

    # 2. _notify_admins_webstore test
    order = MagicMock(spec=WebOrder)
    order.order_id = "0bf6c5037bcc4ba0"
    order.tariff_label = "Базовый • 90 дн • 1📱• 91 Гб⚡️"
    order.amount_rub = 325
    order.original_amount_rub = 325
    order.contact = "saiamirey@yandex.ru"
    order.email = "saiamirey@yandex.ru"
    order.entry_referrer = ""
    order.entry_url = "https://darimiru.ru/renew"
    order.ref_source = ""
    order.marzban_username = ""
    order.subscription_url = ""
    order.target_order_id = "uuid:9585d709-c8fd-4860-bc29-90790fc0cf1f"

    with patch("webstore.routes._get_all_webstore_admin_ids", new_callable=AsyncMock) as mock_admins, \
         patch("webstore.routes._resolve_telegram_user_for_order", new_callable=AsyncMock) as mock_resolve, \
         patch("webstore.routes.enqueue_notifications", new_callable=AsyncMock) as mock_enqueue:
        mock_admins.return_value = {111222333}
        mock_resolve.return_value = {
            "telegram_id": 5812603209,
            "username": "saiamirey",
            "full_name": "Margarita",
        }

        # Paid notification
        await _notify_admins_webstore(order, "paid")
        mock_enqueue.assert_awaited_once()
        text_paid = mock_enqueue.await_args.kwargs["text"]
        assert "Клиент в боте: Margarita (@saiamirey, ID: 5812603209)" in text_paid
        assert "0bf6c5037bcc4ba0" in text_paid
        assert "saiamirey@yandex.ru" in text_paid

        # Delivered notification
        mock_enqueue.reset_mock()
        await _notify_admins_webstore(order, "delivered")
        mock_enqueue.assert_awaited_once()
        text_delivered = mock_enqueue.await_args.kwargs["text"]
        assert "Клиент в боте: Margarita (@saiamirey, ID: 5812603209)" in text_delivered
        assert "Adapt: <code>9585d709-c8fd-4860-bc29-90790fc0cf1f</code>" in text_delivered
        assert "Marzban: None" not in text_delivered


@pytest.mark.asyncio
async def test_notify_bot_order_delivered_and_suppression():
    from webstore.routes import _notify_bot_order_delivered, settings

    order = MagicMock(spec=WebOrder)
    order.order_id = "0bf6c5037bcc4ba0"
    order.tariff_label = "Базовый • 90 дн"
    order.days = 90
    order.access_expires_at = datetime(2026, 12, 27, 12, 0, 0)
    order.subscription_url = "https://darimiru.ru/vpnbot/adapt-sub/9585d709-c8fd-4860-bc29-90790fc0cf1f"
    order.marzban_username = ""
    order.target_order_id = "uuid:9585d709-c8fd-4860-bc29-90790fc0cf1f"

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.content_type = "application/json"
    mock_resp.json = AsyncMock(return_value={
        "ok": True,
        "matched": True,
        "delivered": True,
        "telegram_id": 5812603209,
    })

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = MagicMock(return_value=mock_resp)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=None)

    with patch("aiohttp.ClientSession", return_value=mock_client), \
         patch.object(settings, "referral_bridge_url", "http://127.0.0.1:8080/vpnbot"), \
         patch.object(settings, "bridge_shared_secret", "secret"):
        result = await _notify_bot_order_delivered(order)

    assert result["ok"] is True
    assert result["delivered"] is True
    assert result["telegram_id"] == 5812603209

    # Verify url and payload
    call_args = mock_client.post.call_args
    assert call_args[0][0] == "http://127.0.0.1:8080/vpnbot/internal/web-order/delivered"
    payload = call_args[1]["json"]
    assert payload["order_id"] == "0bf6c5037bcc4ba0"
    assert payload["adapt_uuid"] == "9585d709-c8fd-4860-bc29-90790fc0cf1f"
    assert payload["access_expires_at"] == "2026-12-27T12:00:00"


@pytest.mark.asyncio
async def test_internal_web_order_delivered_full_flow_no_mock_send_key(sqlite_engine):
    """Deep integration test: executes handle_internal_web_order_delivered WITHOUT mocking _send_subscription_key_for_platform.

    Verifies the actual production message sequence:
    1. SUBSCRIPTION_READY notice with formatted expiration date
    2. VPN key in <code>...</code> tags
    3. Platform setup guide with 'Главное меню' inline keyboard
    4. Database state updates (status=ACTIVE, billing_mode=tariff, is_frozen=False)
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from bot.webhooks import handle_internal_web_order_delivered, _DELIVERED_WEB_ORDERS

    _DELIVERED_WEB_ORDERS.clear()

    session_factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with session_factory() as session:
        server = Server(name="NL-3", host="127.0.0.1", location="Amsterdam")
        session.add(server)
        await session.commit()
        await session.refresh(server)

        user = User(
            telegram_id=5812603209,
            username="margarita_p",
            full_name="Маргарита",
            platform=Platform.IOS,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)

        sub = Subscription(
            user_id=user.id,
            server_id=server.id,
            tariff_months=3,
            platform=Platform.IOS,
            status=SubStatus.EXPIRED,
            billing_mode="demo",
            vpn_key="https://darimiru.ru/vpnbot/adapt-sub/9585d709-c8fd-4860-bc29-90790fc0cf1f",
            client_name="adapt_9585d709-c8fd-4860-bc29-90790fc0cf1f",
            expires_at=datetime(2026, 9, 20, 12, 0, 0),
        )
        session.add(sub)
        await session.commit()
        await session.refresh(sub)

        adapt_sub = AdaptSubscription(
            subscription_id=sub.id,
            adapt_uuid="9585d709-c8fd-4860-bc29-90790fc0cf1f",
            adapt_plan_uuid="plan-90d",
            end_date=datetime(2026, 9, 20, 12, 0, 0),
            is_frozen=True,
        )
        session.add(adapt_sub)
        await session.commit()

    order_id = "real_exec_order_001"
    new_expiry = datetime(2026, 12, 28, 12, 0, 0)
    body = {
        "order_id": order_id,
        "adapt_uuid": "9585D709-C8FD-4860-BC29-90790FC0CF1F",  # uppercase to test case-insensitivity
        "access_expires_at": new_expiry.isoformat(),
        "tariff_label": "Базовый • 90 дн",
    }

    mock_bot = AsyncMock()
    req = MagicMock()
    req.headers = {"X-Internal-Secret": "test-secret"}
    req.json = AsyncMock(return_value=body)
    req.app = {"bot": mock_bot}

    with patch("bot.webhooks._verify_internal_secret", return_value=True), \
         patch("bot.webhooks.async_session", session_factory), \
         patch("bot.handlers.payment.async_session", session_factory), \
         patch("bot.services.guide_service.async_session", session_factory):
        resp = await handle_internal_web_order_delivered(req)

    assert resp.status == 200
    import json
    data = json.loads(resp.text)
    assert data["ok"] is True
    assert data["matched"] is True
    assert data["delivered"] is True
    assert data["telegram_id"] == 5812603209

    # Verify actual Telegram message calls made by _send_subscription_key_for_platform
    assert mock_bot.send_message.call_count >= 3
    calls = mock_bot.send_message.call_args_list

    # Call 1: Ready notification with formatted expiration date
    c1_args, c1_kwargs = calls[0]
    assert c1_args[0] == 5812603209
    assert "28.12.2026" in c1_args[1]

    # Call 2: Monospace VPN key
    c2_args, c2_kwargs = calls[1]
    assert c2_args[0] == 5812603209
    assert "<code>https://darimiru.ru/vpnbot/adapt-sub/9585d709-c8fd-4860-bc29-90790fc0cf1f</code>" in c2_args[1]

    # Call 3: Guide and main menu keyboard
    c3_args, c3_kwargs = calls[2]
    assert c3_args[0] == 5812603209
    assert "reply_markup" in c3_kwargs
    reply_markup = c3_kwargs["reply_markup"]
    assert any("Главное меню" in btn.text or "меню" in btn.text.lower() for row in reply_markup.inline_keyboard for btn in row)

    # Verify DB was correctly synced
    async with session_factory() as session:
        updated_sub = await session.get(Subscription, sub.id)
        assert updated_sub.status == SubStatus.ACTIVE
        assert updated_sub.billing_mode == "tariff"
        assert updated_sub.expires_at == new_expiry

        updated_adapt = await session.get(AdaptSubscription, adapt_sub.id)
        assert updated_adapt.is_frozen is False
        assert updated_adapt.end_date == new_expiry


def test_resolve_telegram_user_locally_real_sqlite(tmp_path):
    """Test _resolve_telegram_user_locally against an actual SQLite database file on disk."""
    import sqlite3
    from webstore.routes import _resolve_telegram_user_locally

    db_path = tmp_path / "bot_test.db"
    with sqlite3.connect(str(db_path)) as con:
        con.execute("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                telegram_id INTEGER,
                username TEXT,
                full_name TEXT
            )
        """)
        con.execute("""
            CREATE TABLE subscriptions (
                id INTEGER PRIMARY KEY,
                user_id INTEGER,
                client_name TEXT,
                vpn_key TEXT
            )
        """)
        con.execute("""
            CREATE TABLE adapt_subscriptions (
                id INTEGER PRIMARY KEY,
                subscription_id INTEGER,
                adapt_uuid TEXT
            )
        """)
        # Insert user 1 with adapt sub
        con.execute("INSERT INTO users VALUES (1, 5812603209, 'saiamirey', 'Margarita')")
        con.execute("INSERT INTO subscriptions VALUES (224, 1, 'adapt_9585d709-c8fd-4860-bc29-90790fc0cf1f', 'https://darimiru.ru/vpnbot/adapt-sub/9585d709-c8fd-4860-bc29-90790fc0cf1f')")
        con.execute("INSERT INTO adapt_subscriptions VALUES (10, 224, '9585d709-c8fd-4860-bc29-90790fc0cf1f')")

        # Insert user 2 with marzban sub
        con.execute("INSERT INTO users VALUES (2, 777888999, 'denis', 'Denis')")
        con.execute("INSERT INTO subscriptions VALUES (225, 2, 'marzban_denis', 'vless://...')")

    with patch("webstore.config._bot_db_path", return_value=db_path):
        # 1. Match by Adapt UUID (case-insensitive)
        u1 = _resolve_telegram_user_locally(adapt_uuid="9585D709-C8FD-4860-BC29-90790FC0CF1F")
        assert u1 is not None
        assert u1["telegram_id"] == 5812603209
        assert u1["username"] == "saiamirey"
        assert u1["full_name"] == "Margarita"
        assert u1["subscription_id"] == 224

        # 2. Match by target_order_id
        u2 = _resolve_telegram_user_locally(target_order_id="sub_224")
        assert u2 is not None
        assert u2["telegram_id"] == 5812603209
        assert u2["subscription_id"] == 224

        # 3. Match by marzban_username
        u3 = _resolve_telegram_user_locally(marzban_username="marzban_denis")
        assert u3 is not None
        assert u3["telegram_id"] == 777888999
        assert u3["username"] == "denis"
        assert u3["subscription_id"] == 225

        # 4. Fallback by telegram_id
        u4 = _resolve_telegram_user_locally(telegram_id=777888999)
        assert u4 is not None
        assert u4["telegram_id"] == 777888999

        # 5. Non-existent returns None
        u5 = _resolve_telegram_user_locally(adapt_uuid="00000000-0000-0000-0000-000000000000")
        assert u5 is None


@pytest.mark.asyncio
async def test_internal_web_order_security_and_edge_cases(sqlite_engine):
    """Test authentication rejection, malformed bodies, and unlinked web customers."""
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from bot.webhooks import handle_internal_web_order_delivered

    session_factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)

    # 1. Unauthorized
    req_unauth = MagicMock()
    req_unauth.headers = {}
    with patch("bot.webhooks._verify_internal_secret", return_value=False):
        resp_unauth = await handle_internal_web_order_delivered(req_unauth)
        assert resp_unauth.status == 403

    # 2. Missing order_id
    req_bad = MagicMock()
    req_bad.headers = {"X-Internal-Secret": "test"}
    req_bad.json = AsyncMock(return_value={"adapt_uuid": "some-uuid"})
    with patch("bot.webhooks._verify_internal_secret", return_value=True):
        resp_bad = await handle_internal_web_order_delivered(req_bad)
        assert resp_bad.status == 400

    # 3. Web-only customer not found in bot DB
    req_not_found = MagicMock()
    req_not_found.headers = {"X-Internal-Secret": "test"}
    req_not_found.json = AsyncMock(return_value={"order_id": "pure_web_123", "adapt_uuid": "unknown-uuid-1111"})
    with patch("bot.webhooks._verify_internal_secret", return_value=True), \
         patch("bot.webhooks.async_session", session_factory):
        resp_not_found = await handle_internal_web_order_delivered(req_not_found)
        assert resp_not_found.status == 200
        import json
        data = json.loads(resp_not_found.text)
        assert data["ok"] is True
        assert data["matched"] is False
        assert data["delivered"] is False
        assert data["reason"] == "subscription_not_found"


