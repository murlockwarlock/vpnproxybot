"""Tests for VHQ migration and legacy backfill hardening.

Verifies:
1. bot.database.init_db does NOT blindly backfill vhq_order_id from client_name.
2. Existing bot vhq_order_id values are preserved and never overwritten.
3. Legacy VHQ renewals without vhq_order_id do NOT trust stale client_name blindly;
   they require verified supplier evidence or transition to manual_review.
4. If supplier evidence matches vpn_key, legacy order is safely reconciled and renewed.
5. webstore.database.init_db does NOT fill vhq_upstream_url for Marzban or Adapt URLs.
6. Proven legacy VHQ web orders can safely backfill vhq_upstream_url.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import bot.database as bot_db
from bot.models import (
    Base as BotBase,
    Platform,
    SubStatus,
    Subscription,
    Tariff,
    User,
    VhqSubscriptionFulfillment,
)
from bot.services.subscription_service import execute_vhq_fulfillment
from bot.services.vhq_partner_api import VHQPartnerAPI
import webstore.database as webstore_db
from webstore.models import Base as WebBase, WebOrder

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def bot_test_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(BotBase.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def web_test_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(WebBase.metadata.create_all)
    yield engine
    await engine.dispose()


async def test_bot_init_does_not_blindly_backfill_vhq_order_id_from_client_name(bot_test_engine):
    """Production-like fixture:

    Subscription has client_name='vhq_order_A', but vpn_key belongs to order_B.
    vhq_order_id is NULL.
    After init_db, vhq_order_id must remain NULL (no blind backfill).
    """
    session_maker = async_sessionmaker(bot_test_engine, expire_on_commit=False)

    async with session_maker() as session:
        user = User(telegram_id=123456, username="test_user")
        session.add(user)
        await session.flush()

        sub = Subscription(
            user_id=user.id,
            server_id=1,
            tariff_months=1,
            status=SubStatus.EXPIRED,
            device_slots=1,
            vpn_key="https://sub.vhq-connect.online/token_order_B",
            client_name="vhq_order_A_stale",
            platform=Platform.IOS,
            expires_at=datetime.utcnow() - timedelta(days=10),
            vhq_order_id=None,
        )
        session.add(sub)
        await session.commit()
        sub_id = sub.id

    # Run bot init_db
    with patch.object(bot_db, "engine", bot_test_engine), patch.object(bot_db, "async_session", session_maker):
        await bot_db.init_db()

    async with session_maker() as session:
        reloaded = await session.get(Subscription, sub_id)
        assert reloaded.vhq_order_id is None, "vhq_order_id must remain NULL, not blindly backfilled from client_name"
        assert reloaded.client_name == "vhq_order_A_stale"
        assert reloaded.vpn_key == "https://sub.vhq-connect.online/token_order_B"


async def test_bot_migration_does_not_overwrite_existing_vhq_order_id(bot_test_engine):
    """If vhq_order_id is already populated, init_db must never overwrite or clear it."""
    session_maker = async_sessionmaker(bot_test_engine, expire_on_commit=False)

    async with session_maker() as session:
        user = User(telegram_id=234567, username="confirmed_user")
        session.add(user)
        await session.flush()

        sub = Subscription(
            user_id=user.id,
            server_id=1,
            tariff_months=1,
            status=SubStatus.ACTIVE,
            device_slots=1,
            vpn_key="https://sub.vhq-connect.online/current_token",
            client_name="vhq_legacy_initial_order",
            platform=Platform.IOS,
            expires_at=datetime.utcnow() + timedelta(days=20),
            vhq_order_id="proven_order_uuid_999",
        )
        session.add(sub)
        await session.commit()
        sub_id = sub.id

    with patch.object(bot_db, "engine", bot_test_engine), patch.object(bot_db, "async_session", session_maker):
        await bot_db.init_db()

    async with session_maker() as session:
        reloaded = await session.get(Subscription, sub_id)
        assert reloaded.vhq_order_id == "proven_order_uuid_999"
        assert reloaded.client_name == "vhq_legacy_initial_order"


async def test_legacy_vhq_client_name_is_not_used_as_unverified_supplier_truth(bot_test_engine):
    """A legacy subscription with vhq_order_id=NULL and stale client_name must NOT

    trigger api.renew(stale_client_name).
    If supplier state does not verify candidate, it must transition to manual_review.
    """
    session_maker = async_sessionmaker(bot_test_engine, expire_on_commit=False)

    async with session_maker() as session:
        user = User(telegram_id=345678, username="stale_client")
        session.add(user)
        await session.flush()

        sub = Subscription(
            user_id=user.id,
            server_id=1,
            tariff_months=1,
            status=SubStatus.EXPIRED,
            device_slots=1,
            vpn_key="https://sub.vhq-connect.online/unmatched_key_token_123456",
            client_name="vhq_stale_order_id_111",
            platform=Platform.IOS,
            expires_at=datetime.utcnow() - timedelta(days=5),
            vhq_order_id=None,
        )
        session.add(sub)
        await session.flush()

        fulfillment = VhqSubscriptionFulfillment(
            fulfillment_uuid="test_unverified_legacy_ff",
            subscription_id=sub.id,
            user_id=user.id,
            source_type="payment",
            source_id="pay_999",
            action="renew",
            status="pending",
            vhq_order_id=None,
            attempts=0,
            next_attempt_at=datetime.utcnow(),
        )
        session.add(fulfillment)
        await session.commit()
        ff_id = fulfillment.id
        sub_id = sub.id

    api_mock = AsyncMock(spec=VHQPartnerAPI)
    # Supplier returns orders, but none matches the active vpn_key token
    api_mock.get_orders.return_value = {
        "orders": [
            {
                "id": "stale_order_id_111",
                "branded_url": "https://sub.vhq-connect.online/old_token_00000000",
                "active_until": "2026-06-01T00:00:00Z",
            }
        ]
    }

    async with session_maker() as session:
        ff = await session.get(VhqSubscriptionFulfillment, ff_id)
        await execute_vhq_fulfillment(session, ff, api=api_mock)
        await session.commit()

    api_mock.renew.assert_not_called()

    async with session_maker() as session:
        reloaded_ff = await session.get(VhqSubscriptionFulfillment, ff_id)
        assert reloaded_ff.status == "manual_review"
        assert reloaded_ff.last_error_code == "unverified_legacy_order"
        assert "not verified by current upstream key" in (reloaded_ff.last_error_message or "")

        reloaded_sub = await session.get(Subscription, sub_id)
        assert reloaded_sub.vhq_order_id is None, "Sub vhq_order_id must not be populated with unverified candidate"


async def test_legacy_vhq_reconciles_order_from_supplier_evidence_matching_vpn_key(bot_test_engine):
    """If vhq_order_id is NULL, but supplier get_orders matches current vpn_key,

    it reconciles the proven order_id and safely renews.
    """
    session_maker = async_sessionmaker(bot_test_engine, expire_on_commit=False)
    now = datetime.utcnow()

    async with session_maker() as session:
        user = User(telegram_id=456789, username="recoverable_legacy")
        session.add(user)
        await session.flush()

        sub = Subscription(
            user_id=user.id,
            server_id=1,
            tariff_months=1,
            status=SubStatus.EXPIRED,
            device_slots=1,
            vpn_key="https://sub.vhq-connect.online/matching_token_abcdef123456",
            client_name="vhq_old_initial_candidate",
            platform=Platform.IOS,
            expires_at=now - timedelta(days=2),
            vhq_order_id=None,
        )
        session.add(sub)
        await session.flush()

        fulfillment = VhqSubscriptionFulfillment(
            fulfillment_uuid="test_reconciled_legacy_ff",
            subscription_id=sub.id,
            user_id=user.id,
            source_type="payment",
            source_id="pay_1000",
            action="renew",
            status="pending",
            vhq_order_id=None,
            attempts=0,
            next_attempt_at=now,
            target_local_expires_at=now + timedelta(days=30),
        )
        session.add(fulfillment)
        await session.commit()
        ff_id = fulfillment.id
        sub_id = sub.id

    api_mock = AsyncMock(spec=VHQPartnerAPI)
    # Supplier returns the order whose URL token matches vpn_key
    supplier_base_active = now + timedelta(days=5)
    target_active = supplier_base_active + timedelta(days=30)
    api_mock.get_orders.return_value = {
        "orders": [
            {
                "id": "proven_current_order_777",
                "branded_url": "https://sub.vhq-connect.online/matching_token_abcdef123456",
                "active_until": supplier_base_active.isoformat() + "Z",
            }
        ]
    }
    api_mock.renew.return_value = {
        "status": "renewed",
        "active_until": target_active.isoformat() + "Z",
    }

    async with session_maker() as session:
        ff = await session.get(VhqSubscriptionFulfillment, ff_id)
        await execute_vhq_fulfillment(session, ff, api=api_mock)
        await session.commit()

    api_mock.renew.assert_called_once_with(order_id="proven_current_order_777")

    async with session_maker() as session:
        reloaded_ff = await session.get(VhqSubscriptionFulfillment, ff_id)
        assert reloaded_ff.status == "completed"
        assert reloaded_ff.vhq_order_id == "proven_current_order_777"

        reloaded_sub = await session.get(Subscription, sub_id)
        assert reloaded_sub.vhq_order_id == "proven_current_order_777"
        assert reloaded_sub.status == SubStatus.ACTIVE


async def test_webstore_init_does_not_fill_vhq_upstream_for_marzban_url(web_test_engine):
    """Marzban historical URLs (/sub/... and /s/...) must NOT receive vhq_upstream_url."""
    session_maker = async_sessionmaker(web_test_engine, expire_on_commit=False)

    async with session_maker() as session:
        order_marzban = WebOrder(
            order_id="marzban_order_1",
            profile_token="p_tok_1",
            tariff_key="basic_30",
            tariff_label="Basic 30",
            days=30,
            amount_rub=300,
            status="delivered",
            subscription_url="https://darimiru.ru/sub/d2ViXzBiZjQwZDlmMWM4ODRjZDUsMTc3NTIzMzQ5NQ",
            vhq_upstream_url=None,
        )
        order_short = WebOrder(
            order_id="marzban_order_short",
            profile_token="p_tok_2",
            tariff_key="basic_30",
            tariff_label="Basic 30",
            days=30,
            amount_rub=300,
            status="delivered",
            subscription_url="https://darimiru.ru/s/abcdef12345",
            vhq_upstream_url=None,
        )
        session.add_all([order_marzban, order_short])
        await session.commit()

    with patch.object(webstore_db, "engine", web_test_engine), patch.object(webstore_db, "async_session", session_maker):
        await webstore_db.init_db()

    async with session_maker() as session:
        o1 = await session.scalar(select(WebOrder).where(WebOrder.order_id == "marzban_order_1"))
        o2 = await session.scalar(select(WebOrder).where(WebOrder.order_id == "marzban_order_short"))
        assert o1.vhq_upstream_url is None, "Marzban subscription_url must NOT be copied into vhq_upstream_url"
        assert o2.vhq_upstream_url is None, "Marzban short subscription_url must NOT be copied into vhq_upstream_url"


async def test_webstore_init_does_not_fill_vhq_upstream_for_adapt_url(web_test_engine):
    """Adapt historical URLs (/vpnbot/adapt-sub/...) must NOT receive vhq_upstream_url."""
    session_maker = async_sessionmaker(web_test_engine, expire_on_commit=False)

    async with session_maker() as session:
        order_adapt = WebOrder(
            order_id="adapt_order_1",
            profile_token="p_tok_adapt",
            tariff_key="adapt_basic_30",
            tariff_label="Adapt Basic 30",
            days=30,
            amount_rub=350,
            status="delivered",
            subscription_url="https://darimiru.ru/vpnbot/adapt-sub/f828595d-8821-43fb-88db-93ba0f3f259c",
            vhq_upstream_url=None,
        )
        session.add(order_adapt)
        await session.commit()

    with patch.object(webstore_db, "engine", web_test_engine), patch.object(webstore_db, "async_session", session_maker):
        await webstore_db.init_db()

    async with session_maker() as session:
        o = await session.scalar(select(WebOrder).where(WebOrder.order_id == "adapt_order_1"))
        assert o.vhq_upstream_url is None, "Adapt URL must NOT be copied into vhq_upstream_url"


async def test_webstore_init_can_backfill_proven_vhq_legacy_row(web_test_engine):
    """Only orders with a proven VHQ marker (e.g. marzban_username='vhq_...' or vhq supplier domains)

    receive vhq_upstream_url.
    """
    session_maker = async_sessionmaker(web_test_engine, expire_on_commit=False)

    async with session_maker() as session:
        vhq_proven_username = WebOrder(
            order_id="vhq_proven_1",
            profile_token="p_tok_vhq1",
            tariff_key="vhq_premium",
            tariff_label="VHQ Premium",
            days=30,
            amount_rub=399,
            status="delivered",
            marzban_username="vhq_order_uuid_123",
            subscription_url="https://darimiru.ru/sub/external_config_url",
            vhq_upstream_url=None,
        )
        vhq_proven_url = WebOrder(
            order_id="vhq_proven_2",
            profile_token="p_tok_vhq2",
            tariff_key="vhq_premium",
            tariff_label="VHQ Premium",
            days=30,
            amount_rub=399,
            status="delivered",
            subscription_url="https://sub.vhq-connect.online/token_direct",
            vhq_upstream_url=None,
        )
        session.add_all([vhq_proven_username, vhq_proven_url])
        await session.commit()

    with patch.object(webstore_db, "engine", web_test_engine), patch.object(webstore_db, "async_session", session_maker):
        await webstore_db.init_db()

    async with session_maker() as session:
        o1 = await session.scalar(select(WebOrder).where(WebOrder.order_id == "vhq_proven_1"))
        o2 = await session.scalar(select(WebOrder).where(WebOrder.order_id == "vhq_proven_2"))
        assert o1.vhq_upstream_url == "https://darimiru.ru/sub/external_config_url"
        assert o2.vhq_upstream_url == "https://sub.vhq-connect.online/token_direct"


async def test_legacy_recovery_preserves_existing_expected_and_does_not_double_renew(bot_test_engine):
    """Fixture:
    sub.vhq_order_id = NULL
    fulfillment.vhq_order_id = NULL
    baseline = B0
    expected = B0 + 30d
    status = uncertain
    attempts >= 1
    Supplier GET orders returns proven order matching vpn_key:
    active_until = expected
    Expectation:
    order_id is recovered;
    baseline remains B0;
    expected remains B0 + 30d;
    fulfillment becomes completed via reconciliation;
    api.renew.assert_not_called().
    """
    session_maker = async_sessionmaker(bot_test_engine, expire_on_commit=False)
    now = datetime.utcnow()
    b0 = now - timedelta(days=2)
    expected = b0 + timedelta(days=30)

    async with session_maker() as session:
        user = User(telegram_id=987654, username="recovery_user")
        session.add(user)
        await session.flush()

        sub = Subscription(
            user_id=user.id,
            server_id=1,
            tariff_months=1,
            status=SubStatus.EXPIRED,
            device_slots=1,
            vpn_key="https://sub.vhq-connect.online/token_rec_preserve_12345",
            client_name="vhq_candidate_999",
            platform=Platform.IOS,
            expires_at=now - timedelta(days=2),
            vhq_order_id=None,
        )
        session.add(sub)
        await session.flush()

        fulfillment = VhqSubscriptionFulfillment(
            fulfillment_uuid="test_preserve_target_ff",
            subscription_id=sub.id,
            user_id=user.id,
            source_type="payment",
            source_id="pay_preserve_999",
            action="renew",
            status="uncertain",
            vhq_order_id=None,
            baseline_provider_active_until=b0,
            expected_provider_active_until=expected,
            baseline_local_expires_at=sub.expires_at,
            target_local_expires_at=now + timedelta(days=30),
            attempts=1,
            next_attempt_at=now,
        )
        session.add(fulfillment)
        await session.commit()
        ff_id = fulfillment.id
        sub_id = sub.id

    api_mock = AsyncMock(spec=VHQPartnerAPI)
    # Supplier GET orders already shows active_until == expected
    api_mock.get_orders.return_value = {
        "orders": [
            {
                "id": "proven_order_recovered_456",
                "branded_url": "https://sub.vhq-connect.online/token_rec_preserve_12345",
                "active_until": expected.isoformat() + "Z",
            }
        ]
    }

    async with session_maker() as session:
        ff = await session.get(VhqSubscriptionFulfillment, ff_id)
        await execute_vhq_fulfillment(session, ff, api=api_mock)
        await session.commit()

    api_mock.renew.assert_not_called()

    async with session_maker() as session:
        reloaded_ff = await session.get(VhqSubscriptionFulfillment, ff_id)
        assert reloaded_ff.status == "completed"
        assert reloaded_ff.vhq_order_id == "proven_order_recovered_456"
        assert reloaded_ff.baseline_provider_active_until == b0, "Baseline B0 must be preserved, not overwritten"
        assert reloaded_ff.expected_provider_active_until == expected, "Expected must remain B0 + 30d"

        reloaded_sub = await session.get(Subscription, sub_id)
        assert reloaded_sub.vhq_order_id == "proven_order_recovered_456"
        assert reloaded_sub.status == SubStatus.ACTIVE


async def test_legacy_initial_identity_and_baseline_are_committed_before_renew(bot_test_engine):
    """Fixture:
    vhq_order_id NULL;
    baseline NULL;
    expected NULL.
    GET orders returns matched current order with baseline B0.
    At the moment of calling mocked api.renew(), open a SEPARATE DB session and prove that already committed:
    fulfillment.vhq_order_id == proven order;
    subscription.vhq_order_id == proven order;
    fulfillment.baseline_provider_active_until == B0;
    fulfillment.expected_provider_active_until == max(B0, now) + 30d.
    Only after this check is supplier POST permitted.
    """
    session_maker = async_sessionmaker(bot_test_engine, expire_on_commit=False)
    now = datetime.utcnow()
    b0 = now + timedelta(days=5)
    ref_time = b0 if b0 > now else now
    expected_calc = ref_time + timedelta(days=30)

    async with session_maker() as session:
        user = User(telegram_id=987655, username="commit_before_renew_user")
        session.add(user)
        await session.flush()

        sub = Subscription(
            user_id=user.id,
            server_id=1,
            tariff_months=1,
            status=SubStatus.EXPIRED,
            device_slots=1,
            vpn_key="https://sub.vhq-connect.online/token_commit_check_999",
            client_name="vhq_old_cand",
            platform=Platform.IOS,
            expires_at=now - timedelta(days=1),
            vhq_order_id=None,
        )
        session.add(sub)
        await session.flush()

        fulfillment = VhqSubscriptionFulfillment(
            fulfillment_uuid="test_commit_before_renew_ff",
            subscription_id=sub.id,
            user_id=user.id,
            source_type="payment",
            source_id="pay_commit_check_1",
            action="renew",
            status="pending",
            vhq_order_id=None,
            baseline_provider_active_until=None,
            expected_provider_active_until=None,
            baseline_local_expires_at=sub.expires_at,
            target_local_expires_at=now + timedelta(days=30),
            attempts=0,
            next_attempt_at=now,
        )
        session.add(fulfillment)
        await session.commit()
        ff_id = fulfillment.id
        sub_id = sub.id

    api_mock = AsyncMock(spec=VHQPartnerAPI)
    api_mock.get_orders.return_value = {
        "orders": [
            {
                "id": "order_committed_prior_to_renew",
                "branded_url": "https://sub.vhq-connect.online/token_commit_check_999",
                "active_until": b0.isoformat() + "Z",
            }
        ]
    }

    committed_checked = False

    async def verify_committed_before_renew(order_id: str):
        nonlocal committed_checked
        # Open an independent session to verify what was committed to the DB
        async with session_maker() as separate_session:
            db_ff = await separate_session.get(VhqSubscriptionFulfillment, ff_id)
            db_sub = await separate_session.get(Subscription, sub_id)
            assert db_ff.vhq_order_id == "order_committed_prior_to_renew"
            assert db_sub.vhq_order_id == "order_committed_prior_to_renew"
            assert db_ff.baseline_provider_active_until == b0
            assert db_ff.expected_provider_active_until == expected_calc
        committed_checked = True
        return {
            "status": "renewed",
            "active_until": expected_calc.isoformat() + "Z",
        }

    api_mock.renew.side_effect = verify_committed_before_renew

    async with session_maker() as session:
        ff = await session.get(VhqSubscriptionFulfillment, ff_id)
        await execute_vhq_fulfillment(session, ff, api=api_mock)
        await session.commit()

    assert committed_checked is True, "Separate DB session must verify committed identity & baseline before renew"
    api_mock.renew.assert_called_once_with(order_id="order_committed_prior_to_renew")


async def test_legacy_crash_after_supplier_renew_does_not_request_second_cycle(bot_test_engine):
    """Crash recovery simulation:
    Attempt 1:
      - vhq_order_id NULL, baseline NULL, expected NULL.
      - First attempt durable saves identity/baseline/expected.
      - Supplier renew is executed successfully, but process crashes before local success commit.
    Attempt 2 (Recovery):
      - Gets orders from supplier: active_until == previously persisted expected.
      - Fulfillment finishes via reconciliation.
      - Second api.renew() is NOT called.
    Proof: one Payment / fulfillment cannot turn into two supplier renewal cycles.
    """
    session_maker = async_sessionmaker(bot_test_engine, expire_on_commit=False)
    now = datetime.utcnow()
    b0 = now + timedelta(days=2)
    ref_time = b0 if b0 > now else now
    expected = ref_time + timedelta(days=30)

    async with session_maker() as session:
        user = User(telegram_id=987656, username="crash_simulation_user")
        session.add(user)
        await session.flush()

        sub = Subscription(
            user_id=user.id,
            server_id=1,
            tariff_months=1,
            status=SubStatus.EXPIRED,
            device_slots=1,
            vpn_key="https://sub.vhq-connect.online/token_crash_sim_key",
            client_name="vhq_candidate_crash",
            platform=Platform.IOS,
            expires_at=now - timedelta(days=1),
            vhq_order_id=None,
        )
        session.add(sub)
        await session.flush()

        fulfillment = VhqSubscriptionFulfillment(
            fulfillment_uuid="test_crash_recovery_ff",
            subscription_id=sub.id,
            user_id=user.id,
            source_type="payment",
            source_id="pay_crash_recovery_1",
            action="renew",
            status="pending",
            vhq_order_id=None,
            baseline_provider_active_until=None,
            expected_provider_active_until=None,
            baseline_local_expires_at=sub.expires_at,
            target_local_expires_at=now + timedelta(days=30),
            attempts=0,
            next_attempt_at=now,
        )
        session.add(fulfillment)
        await session.commit()
        ff_id = fulfillment.id
        sub_id = sub.id

    api_mock = AsyncMock(spec=VHQPartnerAPI)
    # Initially supplier returns baseline B0
    api_mock.get_orders.return_value = {
        "orders": [
            {
                "id": "order_crash_sim_123",
                "branded_url": "https://sub.vhq-connect.online/token_crash_sim_key",
                "active_until": b0.isoformat() + "Z",
            }
        ]
    }

    renew_calls = 0

    async def supplier_renew_and_crash(order_id: str):
        nonlocal renew_calls
        renew_calls += 1
        # Supplier records the renewal!
        api_mock.get_orders.return_value = {
            "orders": [
                {
                    "id": "order_crash_sim_123",
                    "branded_url": "https://sub.vhq-connect.online/token_crash_sim_key",
                    "active_until": expected.isoformat() + "Z",
                }
            ]
        }
        # Simulate worker process crash right after HTTP POST succeeded
        raise RuntimeError("Simulated process crash immediately after supplier POST renew succeeded")

    api_mock.renew.side_effect = supplier_renew_and_crash

    # --- Attempt 1: Crashes ---
    async with session_maker() as session1:
        ff1 = await session1.get(VhqSubscriptionFulfillment, ff_id)
        await execute_vhq_fulfillment(session1, ff1, api=api_mock)
        # Session 1 rolls back uncommitted success modifications, but prior durable commit remains!
        await session1.rollback()

    assert renew_calls == 1

    # Verify that before the crash, identity + baseline + expected were already committed in DB!
    async with session_maker() as verify_session:
        db_ff = await verify_session.get(VhqSubscriptionFulfillment, ff_id)
        assert db_ff.vhq_order_id == "order_crash_sim_123"
        assert db_ff.baseline_provider_active_until == b0
        assert db_ff.expected_provider_active_until == expected
        # A restarted worker may reconcile only after the durable POST lease
        # expires. Active leases must remain untouched to prevent a second POST.
        db_ff.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
        await verify_session.commit()

    # --- Attempt 2: Worker restart / recovery ---
    # Now simulate a subsequent execution (e.g. status='uncertain' or next attempt)
    api_mock.renew.side_effect = None
    api_mock.renew.return_value = {"status": "renewed", "active_until": (expected + timedelta(days=30)).isoformat() + "Z"}

    async with session_maker() as session2:
        ff2 = await session2.get(VhqSubscriptionFulfillment, ff_id)
        # Next attempt recovers without issuing a second renew
        await execute_vhq_fulfillment(session2, ff2, api=api_mock)
        await session2.commit()

    # CRITICAL: renew must NOT have been called a second time!
    assert renew_calls == 1
    api_mock.renew.assert_called_once_with(order_id="order_crash_sim_123")

    async with session_maker() as final_session:
        final_ff = await final_session.get(VhqSubscriptionFulfillment, ff_id)
        assert final_ff.status == "completed"
        assert final_ff.baseline_provider_active_until == b0
        assert final_ff.expected_provider_active_until == expected
        assert final_ff.confirmed_provider_active_until == expected


async def test_legacy_identity_commit_failure_never_calls_renew(bot_test_engine):
    """Scenario:
    - supplier GET orders successfully finds proven order;
    - first pre-renew session.commit() artificially fails;
    - api.renew.assert_not_called();
    - Check that the exception is NOT swallowed as an ordinary supplier lookup failure.
    """
    session_maker = async_sessionmaker(bot_test_engine, expire_on_commit=False)
    now = datetime.utcnow()
    b0 = now + timedelta(days=5)

    async with session_maker() as session:
        user = User(telegram_id=888111, username="commit_fail_user_1")
        session.add(user)
        await session.flush()

        sub = Subscription(
            user_id=user.id,
            server_id=1,
            tariff_months=1,
            status=SubStatus.EXPIRED,
            device_slots=1,
            vpn_key="https://sub.vhq-connect.online/token_commit_fail_key_1",
            client_name="vhq_cand_fail_1",
            platform=Platform.IOS,
            expires_at=now - timedelta(days=1),
            vhq_order_id=None,
        )
        session.add(sub)
        await session.flush()

        fulfillment = VhqSubscriptionFulfillment(
            fulfillment_uuid="test_commit_fail_ff_1",
            subscription_id=sub.id,
            user_id=user.id,
            source_type="payment",
            source_id="pay_fail_1",
            action="renew",
            status="pending",
            vhq_order_id=None,
            baseline_provider_active_until=None,
            expected_provider_active_until=None,
            baseline_local_expires_at=sub.expires_at,
            target_local_expires_at=now + timedelta(days=30),
            attempts=0,
            next_attempt_at=now,
        )
        session.add(fulfillment)
        await session.commit()
        ff_id = fulfillment.id

    api_mock = AsyncMock(spec=VHQPartnerAPI)
    api_mock.get_orders.return_value = {
        "orders": [
            {
                "id": "order_fail_sim_1",
                "branded_url": "https://sub.vhq-connect.online/token_commit_fail_key_1",
                "active_until": b0.isoformat() + "Z",
            }
        ]
    }

    async with session_maker() as session:
        ff = await session.get(VhqSubscriptionFulfillment, ff_id)

        # Patch session.commit to fail on the pre-renew durable commit
        async def fail_commit():
            raise RuntimeError("Simulated DB commit failure during legacy identity persistence")
        session.commit = fail_commit

        with pytest.raises(RuntimeError, match="Simulated DB commit failure during legacy identity persistence"):
            await execute_vhq_fulfillment(session, ff, api=api_mock)

    api_mock.renew.assert_not_called()


async def test_initial_baseline_commit_failure_never_calls_renew(bot_test_engine):
    """Scenario:
    - fulfillment already has verified order_id;
    - baseline/expected are still NULL;
    - GET orders returns baseline;
    - durable commit baseline/expected fails;
    - api.renew.assert_not_called().
    """
    session_maker = async_sessionmaker(bot_test_engine, expire_on_commit=False)
    now = datetime.utcnow()
    b0 = now + timedelta(days=5)

    async with session_maker() as session:
        user = User(telegram_id=888222, username="commit_fail_user_2")
        session.add(user)
        await session.flush()

        sub = Subscription(
            user_id=user.id,
            server_id=1,
            tariff_months=1,
            status=SubStatus.EXPIRED,
            device_slots=1,
            vpn_key="https://sub.vhq-connect.online/token_commit_fail_key_2",
            client_name="vhq_known_order_2",
            platform=Platform.IOS,
            expires_at=now - timedelta(days=1),
            vhq_order_id="known_order_verified_222",
        )
        session.add(sub)
        await session.flush()

        fulfillment = VhqSubscriptionFulfillment(
            fulfillment_uuid="test_commit_fail_ff_2",
            subscription_id=sub.id,
            user_id=user.id,
            source_type="payment",
            source_id="pay_fail_2",
            action="renew",
            status="pending",
            vhq_order_id="known_order_verified_222",
            baseline_provider_active_until=None,
            expected_provider_active_until=None,
            baseline_local_expires_at=sub.expires_at,
            target_local_expires_at=now + timedelta(days=30),
            attempts=0,
            next_attempt_at=now,
        )
        session.add(fulfillment)
        await session.commit()
        ff_id = fulfillment.id

    api_mock = AsyncMock(spec=VHQPartnerAPI)
    api_mock.get_orders.return_value = {
        "orders": [
            {
                "id": "known_order_verified_222",
                "branded_url": "https://sub.vhq-connect.online/token_commit_fail_key_2",
                "active_until": b0.isoformat() + "Z",
            }
        ]
    }

    async with session_maker() as session:
        ff = await session.get(VhqSubscriptionFulfillment, ff_id)

        # Patch session.commit to fail when committing initial baseline
        async def fail_commit():
            raise RuntimeError("Simulated DB commit failure during baseline persistence")
        session.commit = fail_commit

        with pytest.raises(RuntimeError, match="Simulated DB commit failure during baseline persistence"):
            await execute_vhq_fulfillment(session, ff, api=api_mock)

    api_mock.renew.assert_not_called()
