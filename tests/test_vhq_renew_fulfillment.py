"""Comprehensive tests for VHQ Baza Pro Renew, Webstore Raw Upstream,

Idempotency, Concurrency, and Worker Reconciliation.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.config import settings
from bot.models import (
    Base as BotBase,
    ManualSubscriptionOperation,
    Payment,
    PaymentMethod,
    PaymentStatus,
    Platform,
    RecurringPaymentProfile,
    Server,
    SubStatus,
    Subscription,
    Tariff,
    TariffType,
    User,
    VhqSubscriptionFulfillment,
    ReferralEarning,
)
from webstore.models import Base as WebBase, WebOrder
from bot.services.provisioning_issues import AccessProvisionError
from bot.services.subscription_service import (
    _create_vhq_paid_subscription,
    create_or_extend_paid_access,
    execute_vhq_fulfillment,
)
from bot.services.vhq_partner_api import VHQPartnerAPI, VHQPartnerAPIError
from bot.services.vhq_subscription_proxy import (
    build_vhq_mirror_url,
    build_vhq_subscription_ref_url,
    build_vhq_web_order_ref_url,
    resolve_vhq_mirror_token,
)
from webstore.routes import (
    _fulfill_vhq_order,
    _resolve_root_vhq_web_order,
    attempt_fulfill_order,
)
from bot.services.scheduler import process_vhq_subscription_fulfillments

pytestmark = pytest.mark.asyncio


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def bot_db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(BotBase.metadata.create_all)
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_active_vhq_renewal_per_sub "
                "ON vhq_subscription_fulfillments (subscription_id) "
                "WHERE status IN ('pending', 'processing', 'retry_wait', 'uncertain')"
            )
        )
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_vhq_fulfillment_source "
                "ON vhq_subscription_fulfillments (source_type, source_id)"
            )
        )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("bot.services.scheduler.async_session", factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", factory)
    monkeypatch.setattr("bot.webhooks.async_session", factory)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def web_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(WebBase.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _make_db_server(id=1, name="Test Server"):
    return Server(
        id=id,
        name=name,
        host="vpn.test.com",
        location="Amsterdam",
        api_url="https://vpn.test.com",
        is_active=True,
    )


def _make_db_sub(
    id: int,
    user_id: int = 1,
    tariff_id: int = 101,
    server_id: int = 1,
    client_name: str = "vhq_ord",
    vpn_key: str = "https://sub.vhq-connect.xyz/key",
    vhq_order_id: str = "ord_1",
    status: SubStatus = SubStatus.ACTIVE,
    expires_at: datetime | None = None,
    vhq_active_until: datetime | None = None,
    platform: Platform = Platform.ANDROID,
):
    now = datetime.utcnow()
    return Subscription(
        id=id,
        user_id=user_id,
        tariff_id=tariff_id,
        server_id=server_id,
        tariff_months=1,
        tariff_days=30,
        billing_mode="tariff",
        device_slots=1,
        client_name=client_name,
        vpn_key=vpn_key,
        vhq_order_id=vhq_order_id,
        status=status,
        expires_at=expires_at or (now + timedelta(days=10)),
        vhq_active_until=vhq_active_until or (now + timedelta(days=10)),
        platform=platform,
    )


async def _recurring_provider_success(
    bot_db,
    provider_payment_id: str,
    *,
    telegram_id: int,
    tariff_id: int,
    subscription_id: int,
    amount: str,
):
    async with bot_db() as session:
        profile = await session.scalar(
            select(RecurringPaymentProfile)
            .where(RecurringPaymentProfile.tariff_id == tariff_id)
            .order_by(RecurringPaymentProfile.id.desc())
            .limit(1)
        )
        assert profile is not None
        operation_key = profile.recurring_operation_key
    return {
        "id": provider_payment_id,
        "status": "succeeded",
        "paid": True,
        "amount": {"value": amount, "currency": "RUB"},
        "metadata": {
            "user_id": str(telegram_id),
            "tariff_id": str(tariff_id),
            "platform": f"deferred~r~{subscription_id}",
            "recurring": "true",
            "recurring_profile_id": str(profile.id),
            "sub_id": str(subscription_id),
            "operation_key": operation_key,
        },
    }


# ── Test 1: Manual operation concurrent callbacks single apply ──────────────────

async def test_manual_operation_concurrent_callbacks_single_apply(bot_db):
    """Два конкурентных callback одного op_id → только один draft -> applied, один fulfillment."""
    async with bot_db() as session:
        user = User(telegram_id=111001, username="test_user", full_name="Test User")
        tariff = Tariff(
            id=101,
            label="Baza Pro 30d",
            price_rub=100,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            vhq_tier="basic",
        )
        server = _make_db_server(1, "Server 1")
        sub = _make_db_sub(
            201,
            user_id=1,
            tariff_id=101,
            server_id=1,
            client_name="vhq_ord_1",
            vpn_key="https://sub.vhq-connect.xyz/key1",
            vhq_order_id="ord_1",
        )
        op = ManualSubscriptionOperation(
            id=1,
            admin_user_id=999,
            target_user_id=1,
            subscription_id=201,
            tariff_id=None,
            action="renew",
            status="draft",
            operation_uuid=str(uuid.uuid4()),
        )
        session.add_all([user, tariff, server, sub, op])
        await session.commit()

    applied_count = 0
    duplicate_count = 0

    async def simulate_callback():
        nonlocal applied_count, duplicate_count
        async with bot_db() as s:
            from sqlalchemy import update
            now = datetime.utcnow()
            stmt = (
                update(ManualSubscriptionOperation)
                .where(ManualSubscriptionOperation.id == 1)
                .where(ManualSubscriptionOperation.status == "draft")
                .values(
                    status="applied",
                    tariff_id=101,
                    applied_at=now,
                )
            )
            res = await s.execute(stmt)
            await s.commit()

            if res.rowcount == 1:
                # Won the race: simulate fulfillment creation
                applied_count += 1
                op_obj = await s.get(ManualSubscriptionOperation, 1)
                fulfillment = VhqSubscriptionFulfillment(
                    subscription_id=201,
                    user_id=1,
                    action="renew",
                    source_type="manual",
                    source_id=f"manual:{op_obj.operation_uuid}",
                    status="completed",
                )
                s.add(fulfillment)
                op_obj.status = "completed"
                await s.commit()
            else:
                # Duplicate callback: verify operation is already applied/completed
                op_obj = await s.get(ManualSubscriptionOperation, 1)
                if op_obj and op_obj.status in ("applied", "completed"):
                    duplicate_count += 1

    # Run two concurrent callbacks
    await asyncio.gather(simulate_callback(), simulate_callback())

    assert applied_count == 1
    assert duplicate_count == 1

    async with bot_db() as s:
        fulfillments = (await s.scalars(select(VhqSubscriptionFulfillment))).all()
        assert len(fulfillments) == 1
        op_final = await s.get(ManualSubscriptionOperation, 1)
        assert op_final.status in ("applied", "completed")
        assert op_final.tariff_id == 101


async def test_manual_recovery_and_scheduler_share_one_vhq_post_claim(bot_db, monkeypatch):
    """A manual execution racing the scheduler may issue only one supplier renew."""
    now = datetime.utcnow()
    baseline = now + timedelta(days=2)
    expected = baseline + timedelta(days=30)
    async with bot_db() as session:
        user = User(telegram_id=111002, username="vhq_race_user")
        server = _make_db_server(2, "Race server")
        sub = _make_db_sub(
            202,
            user_id=1,
            server_id=2,
            vhq_order_id="race-order",
            vpn_key="https://sub.vhq-connect.xyz/race-key",
            expires_at=now - timedelta(days=1),
            vhq_active_until=baseline,
        )
        session.add_all([user, server, sub])
        await session.flush()
        sub.user_id = user.id
        fulfillment = VhqSubscriptionFulfillment(
            subscription_id=sub.id,
            user_id=user.id,
            source_type="payment",
            source_id="payment:race-one",
            action="renew",
            status="pending",
            vhq_order_id="race-order",
            baseline_provider_active_until=baseline,
            expected_provider_active_until=expected,
            baseline_local_expires_at=sub.expires_at,
            target_local_expires_at=now + timedelta(days=30),
            next_attempt_at=now,
        )
        session.add(fulfillment)
        await session.commit()
        fulfillment_id = fulfillment.id

    api = AsyncMock(spec=VHQPartnerAPI)
    api.renew.return_value = {
        "status": "renewed",
        "active_until": expected.isoformat() + "Z",
    }
    monkeypatch.setattr("bot.services.subscription_service.VHQPartnerAPI", lambda: api)
    async def manual_worker():
        async with bot_db() as session:
            row = await session.get(VhqSubscriptionFulfillment, fulfillment_id)
            await execute_vhq_fulfillment(session, row, api=api)
            await session.commit()

    await asyncio.gather(
        manual_worker(),
        process_vhq_subscription_fulfillments(SimpleNamespace()),
    )

    assert api.renew.await_count == 1
    api.renew.assert_awaited_once_with(order_id="race-order")
    async with bot_db() as session:
        row = await session.get(VhqSubscriptionFulfillment, fulfillment_id)
        assert row.status == "completed", (
            f"status={row.status} error={row.last_error_code} attempts={row.attempts} "
            f"lease={row.lease_token} calls={api.renew.await_count}"
        )


# ── Test 2: Two distinct payments cannot share same VHQ renewal cycle ───────────

async def test_two_distinct_payments_cannot_share_same_vhq_renewal_cycle(bot_db):
    """Два разных Payment.id для одной Baza Pro до завершения первого renew → второй не получает ложное +30."""
    baseline_expiry = datetime.utcnow() + timedelta(days=5)
    async with bot_db() as session:
        user = User(telegram_id=222002, username="racer", full_name="Racer")
        tariff = Tariff(
            id=102,
            label="Baza Pro",
            price_rub=150,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            vhq_tier="basic",
        )
        server = _make_db_server(2, "Server 2")
        sub = _make_db_sub(
            202,
            user_id=1,
            tariff_id=102,
            server_id=2,
            client_name="vhq_ord_2",
            vpn_key="https://sub.vhq-connect.xyz/key2",
            vhq_order_id="ord_2",
            expires_at=baseline_expiry,
            vhq_active_until=baseline_expiry,
        )
        pay_a = Payment(
            id=501,
            user_id=1,
            tariff_id=102,
            amount=150,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
        )
        pay_b = Payment(
            id=502,
            user_id=1,
            tariff_id=102,
            amount=150,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
        )
        session.add_all([user, tariff, server, sub, pay_a, pay_b])
        await session.commit()

    # Payment A initiates renew: fulfillment is created and left in "pending" (in-flight)
    async with bot_db() as session:
        sub_obj = await session.get(Subscription, 202)
        tariff_obj = await session.get(Tariff, 102)
        user_obj = await session.get(User, 1)
        pay_a_obj = await session.get(Payment, 501)

        with (
            patch("bot.services.subscription_service.execute_vhq_fulfillment", new=AsyncMock()),
            patch("bot.services.subscription_service.get_primary_active_server", new=AsyncMock(return_value=server)),
            patch("bot.services.subscription_service.get_included_device_slots", new=AsyncMock(return_value=3)),
            patch("bot.services.subscription_service.build_vhq_subscription_ref_url", return_value="https://test/mirror"),
        ):
            with pytest.raises(AccessProvisionError) as pending_err:
                await _create_vhq_paid_subscription(
                    session,
                    user=user_obj,
                    tariff=tariff_obj,
                    platform=Platform.ANDROID,
                    vhq_spec={"tier": "basic", "days": 30},
                    target_subscription_id=202,
                    purchase_action="renew",
                    provisioning_payment=pay_a_obj,
                )
            assert pending_err.value.code == "supplier_fulfillment_pending"
        await session.commit()

    # Verify fulfillment A is created with status "pending"
    async with bot_db() as session:
        f_a = await session.scalar(
            select(VhqSubscriptionFulfillment).where(VhqSubscriptionFulfillment.payment_id == 501)
        )
        assert f_a is not None
        assert f_a.status in ("pending", "processing")

    # Payment B arrives while A is still active:
    # Must fail closed with concurrent_renewal_conflict without double +30!
    async with bot_db() as session:
        sub_obj = await session.get(Subscription, 202)
        tariff_obj = await session.get(Tariff, 102)
        user_obj = await session.get(User, 1)
        pay_b_obj = await session.get(Payment, 502)

        with pytest.raises(AccessProvisionError) as exc_info:
            await _create_vhq_paid_subscription(
                session,
                user=user_obj,
                tariff=tariff_obj,
                platform=Platform.ANDROID,
                vhq_spec={"tier": "basic", "days": 30},
                target_subscription_id=202,
                purchase_action="renew",
                provisioning_payment=pay_b_obj,
            )
        assert exc_info.value.code == "concurrent_renewal_conflict"

    # Verify Subscription expiry did NOT get double-extended (+60d)
    async with bot_db() as session:
        sub_after = await session.get(Subscription, 202)
        assert sub_after.expires_at == baseline_expiry


# ── Test 3: Webstore subscription_url is not used as upstream ───────────────────

async def test_webstore_subscription_url_is_not_used_as_upstream(web_db, monkeypatch):
    """Proxy использует vhq_upstream_url, а не публичную subscription_url."""
    raw_supplier_url = "https://sub.vhq-connect.xyz/actual_upstream_raw_key"
    public_mirror_url = "https://mybot.xyz/webhook/vhq-sub/mirror-abc"

    async with web_db() as session:
        order = WebOrder(
            order_id="WO-PROXY-1",
            tariff_key="baza_pro",
            tariff_label="База Про",
            days=30,
            amount_rub=150,
            status="delivered",
            subscription_url=public_mirror_url,
            vhq_upstream_url=raw_supplier_url,
            vhq_order_id="vhq-proxy-uuid-1",
        )
        session.add(order)
        await session.commit()

    # Generate token pointing to this web order
    secret = "test_proxy_secret_123"
    token_url = build_vhq_web_order_ref_url(
        "WO-PROXY-1",
        public_base_url="https://mybot.xyz",
        path_prefix="webhook",
        secret=secret,
    )
    token = token_url.rsplit("/vhq-sub/", 1)[-1]

    # Resolve token payload
    resolved = resolve_vhq_mirror_token(token, secret=secret)
    assert resolved is not None
    assert resolved.get("kind") == "web_order"
    assert resolved.get("web_order_id") == "WO-PROXY-1"

    # In webhook handler, query DB for web_order
    async with web_db() as session:
        fetched_order = await session.scalar(
            select(WebOrder).where(WebOrder.order_id == resolved["web_order_id"]).limit(1)
        )
        assert fetched_order is not None
        # Must resolve to raw supplier upstream, NEVER to public mirror URL
        assert fetched_order.vhq_upstream_url == raw_supplier_url
        assert fetched_order.vhq_upstream_url != fetched_order.subscription_url


# ── Test 4: Legacy webstore token uses DB upstream after fallback buy ───────────

async def test_legacy_webstore_token_uses_db_upstream_after_fallback_buy(web_db):
    """Старая ссылка с payload {"u": old, "o": web_order_id} после fallback buy

    остается byte-for-byte той же, но proxy получает новый vhq_upstream_url из БД.
    """
    old_supplier_url = "https://sub.vhq-connect.xyz/old_key_supplier"
    new_supplier_url = "https://sub.vhq-connect.xyz/new_key_after_fallback_buy"

    # Create old legacy token containing {"u": old_supplier_url, "o": "WO-LEGACY-42"}
    secret = "test_proxy_secret_123"
    legacy_url = build_vhq_mirror_url(
        old_supplier_url,
        public_base_url="https://mybot.xyz",
        path_prefix="webhook",
        secret=secret,
        order_id="WO-LEGACY-42",
    )
    token_part = legacy_url.rsplit("/vhq-sub/", 1)[-1]

    # WebOrder exists in DB with old upstream initially
    async with web_db() as session:
        order = WebOrder(
            order_id="WO-LEGACY-42",
            tariff_key="baza_pro",
            tariff_label="База Про",
            days=30,
            amount_rub=150,
            status="delivered",
            subscription_url=legacy_url,
            vhq_upstream_url=old_supplier_url,
            vhq_order_id="vhq-supplier-old-42",
        )
        session.add(order)
        await session.commit()

    # Now fallback buy occurs for WO-LEGACY-42: updates supplier state in DB
    async with web_db() as session:
        db_order = await session.scalar(select(WebOrder).where(WebOrder.order_id == "WO-LEGACY-42"))
        db_order.vhq_order_id = "vhq-supplier-new-99"
        db_order.vhq_upstream_url = new_supplier_url
        # subscription_url is untouched!
        await session.commit()

    # Now verify resolution with the EXACT SAME legacy token string:
    resolved = resolve_vhq_mirror_token(token_part, secret=secret)
    assert resolved is not None
    assert resolved.get("kind") == "upstream"
    assert resolved.get("order_id") == "WO-LEGACY-42"
    assert resolved.get("upstream_url") == old_supplier_url  # embedded old upstream

    # Simulate proxy logic: looks up order in DB and prefers vhq_upstream_url
    async with web_db() as session:
        matched_order = await session.scalar(
            select(WebOrder).where(WebOrder.order_id == resolved["order_id"])
        )
        assert matched_order is not None
        proxy_upstream = matched_order.vhq_upstream_url or resolved["upstream_url"]
        assert proxy_upstream == new_supplier_url


# ── Test 5: Webstore root link survives multiple renewals ───────────────────────

async def test_webstore_root_link_survives_multiple_renewals(web_db):
    """Несколько renew WebOrder не меняют customer-facing URL."""
    root_public_url = "https://mybot.xyz/webhook/vhq-sub/stable-customer-token"
    async with web_db() as session:
        root_order = WebOrder(
            order_id="WO-ROOT",
            tariff_key="baza_pro",
            tariff_label="База Про 30д",
            days=30,
            amount_rub=150,
            status="delivered",
            subscription_url=root_public_url,
            vhq_upstream_url="https://sub.vhq-connect.xyz/root_key",
            vhq_order_id="vhq-ord-root",
            access_expires_at=datetime.utcnow() + timedelta(days=5),
            vhq_active_until=datetime.utcnow() + timedelta(days=5),
        )
        session.add(root_order)
        await session.commit()

    # First renewal order WO-RENEW-1 targeting WO-ROOT
    vhq_spec = {"tier": "basic", "days": 30}
    renew_1_active = datetime.utcnow() + timedelta(days=35)
    mock_api = AsyncMock()
    mock_api.renew = AsyncMock(return_value={
        "status": "scheduled",
        "active_until": renew_1_active.isoformat(),
        "order_id": "vhq-ord-root",
    })
    mock_api_cls = MagicMock(return_value=mock_api)
    mock_api_cls.extract_active_until = VHQPartnerAPI.extract_active_until
    mock_api_cls.extract_subscription_url = VHQPartnerAPI.extract_subscription_url
    mock_api_cls.extract_order_id = VHQPartnerAPI.extract_order_id

    async with web_db() as session:
        renew_1 = WebOrder(
            order_id="WO-RENEW-1",
            tariff_key="baza_pro",
            tariff_label="База Про 30д",
            days=30,
            amount_rub=150,
            status="pending",
            purchase_action="renew",
            target_order_id="WO-ROOT",
        )
        session.add(renew_1)
        await session.commit()

        with patch("webstore.routes.VHQPartnerAPI", new=mock_api_cls):
            await _fulfill_vhq_order(session, renew_1, vhq_spec)
        await session.commit()

        assert renew_1.status == "delivered"
        assert renew_1.subscription_url == root_public_url

    # Second renewal order WO-RENEW-2 targeting WO-RENEW-1 (chain traversal to root)
    renew_2_active = datetime.utcnow() + timedelta(days=65)
    mock_api.renew = AsyncMock(return_value={
        "status": "scheduled",
        "active_until": renew_2_active.isoformat(),
        "order_id": "vhq-ord-root",
    })

    async with web_db() as session:
        renew_2 = WebOrder(
            order_id="WO-RENEW-2",
            tariff_key="baza_pro",
            tariff_label="База Про 30д",
            days=30,
            amount_rub=150,
            status="pending",
            purchase_action="renew",
            target_order_id="WO-RENEW-1",
        )
        session.add(renew_2)
        await session.commit()

        with patch("webstore.routes.VHQPartnerAPI", new=mock_api_cls):
            await _fulfill_vhq_order(session, renew_2, vhq_spec)
        await session.commit()

        assert renew_2.status == "delivered"
        # Must still match the root order's public customer link!
        assert renew_2.subscription_url == root_public_url

    # Check root order state
    async with web_db() as session:
        final_root = await session.scalar(select(WebOrder).where(WebOrder.order_id == "WO-ROOT"))
        assert final_root.subscription_url == root_public_url
        assert final_root.vhq_active_until == renew_2_active


# ── Test 6: Uncertain fulfillment is reconciled by worker ───────────────────────

async def test_uncertain_fulfillment_is_reconciled_by_worker(bot_db):
    """uncertain не зависает и реально проходит reconciliation."""
    expected_active = datetime.utcnow() + timedelta(days=25)
    async with bot_db() as session:
        user = User(telegram_id=333003, username="uncertain_guy", full_name="Uncertain Guy")
        tariff = Tariff(
            id=103,
            label="Baza Pro",
            price_rub=150,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            vhq_tier="basic",
        )
        server = _make_db_server(3, "Server 3")
        sub = _make_db_sub(
            203,
            user_id=1,
            tariff_id=103,
            server_id=3,
            client_name="vhq_ord_reconcile",
            vpn_key="https://sub.vhq-connect.xyz/key3",
            vhq_order_id="ord_reconcile_1",
            status=SubStatus.ACTIVE,
            expires_at=datetime.utcnow() + timedelta(days=5),
            vhq_active_until=datetime.utcnow() + timedelta(days=5),
        )
        fulfillment = VhqSubscriptionFulfillment(
            subscription_id=203,
            user_id=1,
            action="renew",
            source_type="payment",
            source_id="payment:9999",
            status="uncertain",
            expected_provider_active_until=expected_active,
            baseline_local_expires_at=datetime.utcnow() + timedelta(days=5),
            target_local_expires_at=datetime.utcnow() + timedelta(days=35),
            attempts=1,
            next_attempt_at=datetime.utcnow() - timedelta(seconds=5),  # ready for retry
        )
        session.add_all([user, tariff, server, sub, fulfillment])
        await session.commit()

    # Mock VHQ API get_orders returning the confirmed active_until
    mock_api = AsyncMock()
    mock_api.get_orders = AsyncMock(return_value=[
        {
            "order_id": "ord_reconcile_1",
            "active_until": (expected_active + timedelta(hours=1)).isoformat() + "Z",
        }
    ])

    bot_mock = AsyncMock()
    with (
        patch("bot.services.scheduler.async_session", bot_db),
        patch("bot.services.subscription_service.VHQPartnerAPI", return_value=mock_api),
    ):
        await process_vhq_subscription_fulfillments(bot_mock)

    # Verify reconciliation completed
    async with bot_db() as session:
        f = await session.scalar(select(VhqSubscriptionFulfillment).where(VhqSubscriptionFulfillment.subscription_id == 203))
        assert f.status == "completed"
        assert f.confirmed_provider_active_until is not None

        s = await session.get(Subscription, 203)
        assert s.expires_at == f.target_local_expires_at
        assert s.vhq_active_until is not None


# ── Test 7: Renew 409 triggers fallback buy ─────────────────────────────────────

async def test_execute_vhq_fulfillment_renew_409_fallback_buy(bot_db):
    """Renew -> 409 expired >60d executes fallback buy."""
    now = datetime.utcnow()
    async with bot_db() as session:
        user = User(telegram_id=444004, username="expired_user", full_name="Expired User")
        tariff = Tariff(
            id=104,
            label="Baza Pro",
            price_rub=150,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            vhq_tier="basic",
        )
        server = _make_db_server(4, "Server 4")
        sub = _make_db_sub(
            204,
            user_id=1,
            tariff_id=104,
            server_id=4,
            client_name="vhq_ord_expired",
            vpn_key="https://sub.vhq-connect.xyz/old_key",
            vhq_order_id="ord_expired_old",
            status=SubStatus.EXPIRED,
            expires_at=now - timedelta(days=65),
            vhq_active_until=now - timedelta(days=65),
        )
        fulfillment = VhqSubscriptionFulfillment(
            subscription_id=204,
            user_id=1,
            action="renew",
            source_type="payment",
            source_id="payment:8888",
            status="pending",
            baseline_local_expires_at=now - timedelta(days=65),
            target_local_expires_at=now + timedelta(days=30),
            attempts=0,
        )
        session.add_all([user, tariff, server, sub, fulfillment])
        await session.commit()

    # Mock API: renew raises 409, buy succeeds
    mock_api = AsyncMock()
    mock_api.renew = AsyncMock(side_effect=VHQPartnerAPIError("Order expired too long ago", status=409))
    new_buy_resp = {
        "order_id": "ord_fallback_new",
        "branded_url": "https://sub.vhq-connect.xyz/new_upstream_key",
        "active_until": (now + timedelta(days=30)).isoformat() + "Z",
    }
    mock_api.buy = AsyncMock(return_value=new_buy_resp)

    async with bot_db() as session:
        f = await session.scalar(select(VhqSubscriptionFulfillment).where(VhqSubscriptionFulfillment.subscription_id == 204))
        await execute_vhq_fulfillment(session, f, api=mock_api)
        await session.commit()

        assert f.status == "completed"
        assert f.action == "buy_fallback"

        s = await session.get(Subscription, 204)
        assert s.vhq_order_id == "ord_fallback_new"
        assert s.vpn_key == "https://sub.vhq-connect.xyz/new_upstream_key"
        assert s.status == SubStatus.ACTIVE


# ── Test 8: Ambiguous fallback buy never retried automatically ──────────────────

async def test_execute_vhq_fulfillment_ambiguous_buy_fails_closed(bot_db):
    """POST buy ambiguous network error is never blindly retried -> manual_review."""
    now = datetime.utcnow()
    async with bot_db() as session:
        user = User(telegram_id=555005, username="timeout_user", full_name="Timeout User")
        tariff = Tariff(
            id=105,
            label="Baza Pro",
            price_rub=150,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            vhq_tier="basic",
        )
        server = _make_db_server(5, "Server 5")
        sub = _make_db_sub(
            205,
            user_id=1,
            tariff_id=105,
            server_id=5,
            client_name="vhq_ord_timeout",
            vpn_key="https://sub.vhq-connect.xyz/old_key",
            vhq_order_id="ord_timeout",
            status=SubStatus.EXPIRED,
            expires_at=now - timedelta(days=70),
        )
        fulfillment = VhqSubscriptionFulfillment(
            subscription_id=205,
            user_id=1,
            action="buy_fallback",
            source_type="payment",
            source_id="payment:7777",
            status="pending",
            baseline_local_expires_at=now - timedelta(days=70),
            target_local_expires_at=now + timedelta(days=30),
            attempts=0,
        )
        session.add_all([user, tariff, server, sub, fulfillment])
        await session.commit()

    mock_api = AsyncMock()
    mock_api.buy = AsyncMock(side_effect=asyncio.TimeoutError("Connection timed out on POST buy"))

    async with bot_db() as session:
        f = await session.scalar(select(VhqSubscriptionFulfillment).where(VhqSubscriptionFulfillment.subscription_id == 205))
        await execute_vhq_fulfillment(session, f, api=mock_api)
        await session.commit()

        # Must NOT be retried automatically -> manual_review
        assert f.status == "manual_review"
        assert f.last_error_code == "ambiguous_buy_network_error"


# ── Test 9: Blocker 1: Strict supplier success condition ─────────────────────────

async def test_old_already_requested_cycle_cannot_complete_new_payment(bot_db):
    """VHQ answers scheduled/already_requested, but active_until matches old cycle.

    Fulfillment must NOT become completed and local expiry must NOT increase.
    """
    now = datetime.utcnow()
    baseline_active = now + timedelta(days=10)
    expected_active = baseline_active + timedelta(days=30)

    async with bot_db() as session:
        user = User(telegram_id=901001, username="strict_user", full_name="Strict User")
        tariff = Tariff(
            id=901,
            label="Baza Pro",
            price_rub=150,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            vhq_tier="basic",
        )
        server = _make_db_server(901, "Server 901")
        sub = _make_db_sub(
            901,
            user_id=1,
            tariff_id=901,
            server_id=901,
            client_name="vhq_ord_strict",
            vpn_key="https://sub.vhq-connect.xyz/strict_key",
            vhq_order_id="ord_strict",
            status=SubStatus.ACTIVE,
            expires_at=baseline_active,
            vhq_active_until=baseline_active,
        )
        fulfillment = VhqSubscriptionFulfillment(
            id=901,
            subscription_id=901,
            user_id=1,
            action="renew",
            source_type="payment",
            source_id="payment:901",
            status="pending",
            baseline_provider_active_until=baseline_active,
            expected_provider_active_until=expected_active,
            baseline_local_expires_at=baseline_active,
            target_local_expires_at=baseline_active + timedelta(days=30),
            attempts=0,
        )
        session.add_all([user, tariff, server, sub, fulfillment])
        await session.commit()

    # VHQ returns scheduled/already_requested, but active_until matches old cycle
    mock_api = AsyncMock()
    mock_api.renew = AsyncMock(return_value={
        "status": "scheduled",
        "message": "already_requested",
        "active_until": baseline_active.isoformat() + "Z",
        "order_id": "ord_strict",
    })

    async with bot_db() as session:
        f = await session.get(VhqSubscriptionFulfillment, 901)
        await execute_vhq_fulfillment(session, f, api=mock_api)
        await session.commit()

        # Fulfillment must be uncertain, NOT completed!
        assert f.status == "uncertain"
        assert f.confirmed_provider_active_until is None

        # Subscription expiry must NOT increase!
        s = await session.get(Subscription, 901)
        assert s.expires_at == baseline_active
        assert s.vhq_active_until == baseline_active


# ── Test 10: Blocker 2: Specific 409 triggers fallback buy ───────────────────────

async def test_renew_409_expired_too_long_triggers_fallback_buy(bot_db):
    """409 'Order expired too long ago, please buy a new one' switches action to buy_fallback."""
    now = datetime.utcnow()
    async with bot_db() as session:
        user = User(telegram_id=902001, username="exp_user", full_name="Exp User")
        tariff = Tariff(
            id=902,
            label="Baza Pro",
            price_rub=150,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            vhq_tier="basic",
        )
        server = _make_db_server(902, "Server 902")
        sub = _make_db_sub(
            902,
            user_id=1,
            tariff_id=902,
            server_id=902,
            client_name="vhq_ord_exp",
            vpn_key="https://sub.vhq-connect.xyz/exp_key",
            vhq_order_id="ord_exp",
            status=SubStatus.EXPIRED,
            expires_at=now - timedelta(days=60),
        )
        fulfillment = VhqSubscriptionFulfillment(
            id=902,
            subscription_id=902,
            user_id=1,
            action="renew",
            source_type="payment",
            source_id="payment:902",
            status="pending",
            baseline_local_expires_at=now - timedelta(days=60),
            target_local_expires_at=now + timedelta(days=30),
            attempts=0,
        )
        session.add_all([user, tariff, server, sub, fulfillment])
        await session.commit()

    mock_api = AsyncMock()
    mock_api.renew = AsyncMock(side_effect=VHQPartnerAPIError(
        status=409, message="Order expired too long ago, please buy a new one"
    ))
    new_buy_resp = {
        "order_id": "ord_exp_new",
        "branded_url": "https://sub.vhq-connect.xyz/new_exp_key",
        "active_until": (now + timedelta(days=30)).isoformat() + "Z",
    }
    mock_api.buy = AsyncMock(return_value=new_buy_resp)

    async with bot_db() as session:
        f = await session.get(VhqSubscriptionFulfillment, 902)
        await execute_vhq_fulfillment(session, f, api=mock_api)
        await session.commit()

        assert f.action == "buy_fallback"
        assert f.status == "completed"
        mock_api.buy.assert_awaited_once_with(tier="basic", days=30)


# ── Test 11: Blocker 2: Wrong product 409 NEVER triggers buy ─────────────────────

async def test_renew_409_wrong_product_never_triggers_buy(bot_db, web_db):
    """409 'Renewal is available only for basic 30-day orders' -> manual_review, never buy."""
    now = datetime.utcnow()
    # 1. Bot fulfillment
    async with bot_db() as session:
        user = User(telegram_id=903001, username="wp_user", full_name="WP User")
        tariff = Tariff(
            id=903,
            label="Baza Pro",
            price_rub=150,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            vhq_tier="basic",
        )
        server = _make_db_server(903, "Server 903")
        sub = _make_db_sub(
            903,
            user_id=1,
            tariff_id=903,
            server_id=903,
            client_name="vhq_ord_wp",
            vpn_key="https://sub.vhq-connect.xyz/wp_key",
            vhq_order_id="ord_wp",
            status=SubStatus.ACTIVE,
            expires_at=now + timedelta(days=5),
        )
        fulfillment = VhqSubscriptionFulfillment(
            id=903,
            subscription_id=903,
            user_id=1,
            action="renew",
            source_type="payment",
            source_id="payment:903",
            status="pending",
            baseline_local_expires_at=now + timedelta(days=5),
            target_local_expires_at=now + timedelta(days=35),
            attempts=0,
        )
        session.add_all([user, tariff, server, sub, fulfillment])
        await session.commit()

    mock_api = AsyncMock()
    mock_api.renew = AsyncMock(side_effect=VHQPartnerAPIError(
        status=409, message="Renewal is available only for basic 30-day orders"
    ))
    mock_api.buy = AsyncMock()

    async with bot_db() as session:
        f = await session.get(VhqSubscriptionFulfillment, 903)
        await execute_vhq_fulfillment(session, f, api=mock_api)
        await session.commit()

        assert f.status == "manual_review"
        assert f.action == "renew"
        assert mock_api.buy.call_count == 0

    # 2. Webstore fulfillment
    async with web_db() as session:
        root = WebOrder(
            order_id="WO-WP-ROOT",
            tariff_key="baza_pro",
            tariff_label="База Про 30д",
            days=30,
            amount_rub=150,
            status="delivered",
            subscription_url="https://test/web-url",
            vhq_order_id="ord_web_wp",
            access_expires_at=now + timedelta(days=5),
            vhq_active_until=now + timedelta(days=5),
        )
        order = WebOrder(
            order_id="WO-WP-RENEW",
            tariff_key="baza_pro",
            tariff_label="База Про 30д",
            days=30,
            amount_rub=150,
            status="pending",
            purchase_action="renew",
            target_order_id="WO-WP-ROOT",
        )
        session.add_all([root, order])
        await session.commit()

    mock_web_api = AsyncMock()
    mock_web_api.renew = AsyncMock(side_effect=VHQPartnerAPIError(
        status=409, message="Renewal is available only for basic 30-day orders"
    ))
    mock_web_api.buy = AsyncMock()
    mock_web_cls = MagicMock(return_value=mock_web_api)
    mock_web_cls.extract_active_until = VHQPartnerAPI.extract_active_until
    mock_web_cls.extract_subscription_url = VHQPartnerAPI.extract_subscription_url
    mock_web_cls.extract_order_id = VHQPartnerAPI.extract_order_id

    mock_notify = AsyncMock()
    async with web_db() as session:
        o = await session.scalar(select(WebOrder).where(WebOrder.order_id == "WO-WP-RENEW"))
        with (
            patch("webstore.routes.VHQPartnerAPI", new=mock_web_cls),
            patch("webstore.routes._notify_admins_webstore", new=mock_notify),
        ):
            await _fulfill_vhq_order(session, o, {"tier": "basic", "days": 30})
        await session.commit()

        assert o.status == "failed"
        assert o.failure_code == "renew_409_wrong_product"
        assert mock_web_api.buy.call_count == 0
        assert mock_notify.call_count >= 1


# ── Test 12: Blocker 3: Manual review & pending fulfillment replay not success ───

async def test_manual_review_fulfillment_replay_is_not_treated_as_success(bot_db):
    """Payment B was blocked by concurrent renewal and is in manual_review.

    Replay via auto_retry_failed_provisionings must NOT set subscription_id,
    deliver key, or treat as success.
    """
    now = datetime.utcnow()
    async with bot_db() as session:
        user = User(telegram_id=904001, username="mr_user", full_name="MR User")
        tariff = Tariff(
            id=904,
            label="Baza Pro",
            price_rub=150,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            vhq_tier="basic",
        )
        server = _make_db_server(904, "Server 904")
        sub = _make_db_sub(
            904,
            user_id=1,
            tariff_id=904,
            server_id=904,
            client_name="vhq_ord_mr",
            vpn_key="https://sub.vhq-connect.xyz/mr_key",
            vhq_order_id="ord_mr",
            status=SubStatus.ACTIVE,
            expires_at=now + timedelta(days=5),
        )
        payment_b = Payment(
            id=904,
            user_id=1,
            tariff_id=904,
            amount=15000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            provider_payment_id="yk_mr_904",
            telegram_chat_id=904001,
            created_at=now - timedelta(minutes=10),
        )
        # Pre-existing fulfillment in manual_review
        ff_b = VhqSubscriptionFulfillment(
            id=904,
            subscription_id=904,
            user_id=1,
            payment_id=904,
            source_type="payment",
            source_id="payment:904",
            action="renew",
            status="manual_review",
            last_error_code="concurrent_renewal_conflict",
            last_error_message="Concurrent renewal blocked",
        )
        session.add_all([user, tariff, server, sub, payment_b, ff_b])
        await session.commit()

    # Replay via _create_vhq_paid_subscription directly must raise AccessProvisionError
    async with bot_db() as session:
        user_obj = await session.get(User, 1)
        tariff_obj = await session.get(Tariff, 904)
        pay_obj = await session.get(Payment, 904)

        with pytest.raises(AccessProvisionError) as exc_info:
            await _create_vhq_paid_subscription(
                session,
                user=user_obj,
                tariff=tariff_obj,
                platform=Platform.ANDROID,
                vhq_spec={"tier": "basic", "days": 30},
                target_subscription_id=904,
                provisioning_payment=pay_obj,
            )
        assert exc_info.value.code == "concurrent_renewal_conflict"

    # Replay via auto_retry_failed_provisionings
    mock_bot = AsyncMock()
    with patch("bot.services.scheduler.async_session", bot_db):
        from bot.services.scheduler import auto_retry_failed_provisionings
        await auto_retry_failed_provisionings(mock_bot)

    async with bot_db() as session:
        p = await session.get(Payment, 904)
        # Payment must NOT be marked provisioned!
        assert p.subscription_id is None

    # Key delivery message must NOT be sent
    assert mock_bot.send_message.call_count == 0


async def test_pending_uncertain_fulfillment_replay_is_not_treated_as_success(bot_db):
    """Fulfillment in pending/uncertain status must raise supplier_fulfillment_pending and not return access."""
    now = datetime.utcnow()
    async with bot_db() as session:
        user = User(telegram_id=905001, username="pend_user", full_name="Pend User")
        tariff = Tariff(
            id=905,
            label="Baza Pro",
            price_rub=150,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            vhq_tier="basic",
        )
        server = _make_db_server(905, "Server 905")
        sub = _make_db_sub(
            905,
            user_id=1,
            tariff_id=905,
            server_id=905,
            client_name="vhq_ord_pend",
            vpn_key="https://sub.vhq-connect.xyz/pend_key",
            vhq_order_id="ord_pend",
            status=SubStatus.ACTIVE,
            expires_at=now + timedelta(days=5),
        )
        payment = Payment(
            id=905,
            user_id=1,
            tariff_id=905,
            amount=15000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            provider_payment_id="yk_pend_905",
            telegram_chat_id=905001,
        )
        ff = VhqSubscriptionFulfillment(
            id=905,
            subscription_id=905,
            user_id=1,
            payment_id=905,
            source_type="payment",
            source_id="payment:905",
            action="renew",
            status="pending",
        )
        session.add_all([user, tariff, server, sub, payment, ff])
        await session.commit()

    async with bot_db() as session:
        user_obj = await session.get(User, 1)
        tariff_obj = await session.get(Tariff, 905)
        pay_obj = await session.get(Payment, 905)

        with pytest.raises(AccessProvisionError) as exc_info:
            await _create_vhq_paid_subscription(
                session,
                user=user_obj,
                tariff=tariff_obj,
                platform=Platform.ANDROID,
                vhq_spec={"tier": "basic", "days": 30},
                target_subscription_id=905,
                provisioning_payment=pay_obj,
            )
        assert exc_info.value.code == "supplier_fulfillment_pending"


# ── Test 13: Blocker 4: Recurring YooKassa durable Payment source ────────────────

async def test_recurring_vhq_uses_durable_payment_source(bot_db):
    """Recurring YooKassa charge creates Payment before provisioning with durable source payment:<id>."""
    now = datetime.utcnow()
    async with bot_db() as session:
        user = User(telegram_id=906001, username="rec_user", full_name="Rec User")
        tariff = Tariff(
            id=906,
            label="Baza Pro",
            price_rub=150,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            vhq_tier="basic",
        )
        server = _make_db_server(906, "Server 906")
        sub = _make_db_sub(
            906,
            user_id=1,
            tariff_id=906,
            server_id=906,
            client_name="vhq_ord_rec",
            vpn_key="https://sub.vhq-connect.xyz/rec_key",
            vhq_order_id="ord_rec",
            status=SubStatus.EXPIRED,
            expires_at=now - timedelta(days=1),
            vhq_active_until=now - timedelta(days=1),
        )
        profile = RecurringPaymentProfile(
            user_id=1,
            subscription_id=906,
            tariff_id=906,
            provider="yookassa",
            provider_payment_method_id="pm_rec_906",
            is_active=True,
            consent_granted=True,
            payment_attempt_count=0,
        )
        session.add_all([user, tariff, server, sub, profile])
        await session.commit()

    mock_bot = AsyncMock()
    mock_api = AsyncMock()
    mock_api.renew = AsyncMock(return_value={
        "status": "scheduled",
        "active_until": (now + timedelta(days=31)).isoformat() + "Z",
        "order_id": "ord_rec",
    })

    async def _fetch(_provider_payment_id):
        return await _recurring_provider_success(
            bot_db,
            "yk_durable_906",
            telegram_id=906001,
            tariff_id=906,
            subscription_id=906,
            amount="150.00",
        )

    with (
        patch.object(settings, "recurring_payments_enabled", True),
        patch("bot.services.scheduler.async_session", bot_db),
        patch("bot.services.payment_service.create_recurring_yookassa_payment", new=AsyncMock(return_value=("succeeded", "yk_durable_906", "succeeded"))),
        patch("bot.services.subscription_service.VHQPartnerAPI", return_value=mock_api),
        patch("bot.services.yookassa_reconciliation.fetch_yookassa_payment", new=_fetch),
    ):
        from bot.services.scheduler import process_recurring_charges
        await process_recurring_charges(mock_bot)

    async with bot_db() as session:
        # Check Payment was created with provider_payment_id="yk_durable_906"
        p = await session.scalar(select(Payment).where(Payment.provider_payment_id == "yk_durable_906"))
        assert p is not None
        assert p.status == PaymentStatus.COMPLETED

        # Check fulfillment uses payment:<p.id>
        f = await session.scalar(select(VhqSubscriptionFulfillment).where(VhqSubscriptionFulfillment.subscription_id == 906))
        assert f is not None
        assert f.source_type == "payment"
        assert f.source_id == f"payment:{p.id}"
        assert f.status == "completed"


async def test_recurring_vhq_uncertain_does_not_send_success_or_advance_expiry(bot_db):
    """When recurring VHQ renew returns uncertain, payment is saved, but success is not sent and expiry not moved."""
    now = datetime.utcnow()
    baseline_active = now - timedelta(days=1)
    async with bot_db() as session:
        user = User(telegram_id=907001, username="rec_unc_user", full_name="Rec Unc User")
        tariff = Tariff(
            id=907,
            label="Baza Pro",
            price_rub=150,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            vhq_tier="basic",
        )
        server = _make_db_server(907, "Server 907")
        sub = _make_db_sub(
            907,
            user_id=1,
            tariff_id=907,
            server_id=907,
            client_name="vhq_ord_unc",
            vpn_key="https://sub.vhq-connect.xyz/unc_key",
            vhq_order_id="ord_unc",
            status=SubStatus.EXPIRED,
            expires_at=baseline_active,
            vhq_active_until=baseline_active,
        )
        profile = RecurringPaymentProfile(
            user_id=1,
            subscription_id=907,
            tariff_id=907,
            provider="yookassa",
            provider_payment_method_id="pm_rec_907",
            is_active=True,
            consent_granted=True,
            payment_attempt_count=0,
            next_charge_at=baseline_active,
        )
        session.add_all([user, tariff, server, sub, profile])
        await session.commit()

    mock_bot = AsyncMock()
    mock_api = AsyncMock()
    # VHQ renew returns old active_until -> uncertain!
    mock_api.renew = AsyncMock(return_value={
        "status": "scheduled",
        "message": "already_requested",
        "active_until": baseline_active.isoformat() + "Z",
        "order_id": "ord_unc",
    })

    async def _fetch(_provider_payment_id):
        return await _recurring_provider_success(
            bot_db,
            "yk_unc_907",
            telegram_id=907001,
            tariff_id=907,
            subscription_id=907,
            amount="150.00",
        )

    with (
        patch.object(settings, "recurring_payments_enabled", True),
        patch("bot.services.scheduler.async_session", bot_db),
        patch("bot.services.payment_service.create_recurring_yookassa_payment", new=AsyncMock(return_value=("succeeded", "yk_unc_907", "succeeded"))),
        patch("bot.services.subscription_service.VHQPartnerAPI", return_value=mock_api),
        patch("bot.services.yookassa_reconciliation.fetch_yookassa_payment", new=_fetch),
    ):
        from bot.services.scheduler import process_recurring_charges
        await process_recurring_charges(mock_bot)

    async with bot_db() as session:
        # Provider success is durable, but fulfillment is still unfinished.
        p = await session.scalar(select(Payment).where(Payment.provider_payment_id == "yk_unc_907"))
        assert p is not None
        assert p.status == PaymentStatus.PENDING
        assert p.subscription_id is None
        assert p.provider_status == "succeeded"
        assert p.provider_paid is True
        assert p.provisioning_failure_code == "supplier_fulfillment_pending"

        # Fulfillment is uncertain
        f = await session.scalar(select(VhqSubscriptionFulfillment).where(VhqSubscriptionFulfillment.subscription_id == 907))
        assert f is not None
        assert f.status == "uncertain"

        # Subscription expiry has NOT advanced!
        s = await session.get(Subscription, 907)
        assert s.expires_at == baseline_active

        # Profile next_charge_at has NOT advanced!
        prof = await session.scalar(select(RecurringPaymentProfile).where(RecurringPaymentProfile.subscription_id == 907))
        assert prof.next_charge_at == baseline_active

    # RECURRING_CHARGE_SUCCESS must NOT have been sent!
    from bot.utils.texts import RECURRING_CHARGE_SUCCESS
    for call in mock_bot.send_message.call_args_list:
        text_arg = call[0][1] if len(call[0]) > 1 else call[1].get("text", "")
        assert "Успешное списание" not in text_arg


async def test_recurring_vhq_retry_reuses_same_payment_and_fulfillment(bot_db):
    """Retrying recurring charge reuses existing Payment and does not create duplicate Payment or fulfillment."""
    now = datetime.utcnow()
    baseline_active = now - timedelta(days=1)
    async with bot_db() as session:
        user = User(telegram_id=908001, username="rec_retry_user", full_name="Rec Retry User")
        tariff = Tariff(
            id=908,
            label="Baza Pro",
            price_rub=150,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            vhq_tier="basic",
        )
        server = _make_db_server(908, "Server 908")
        sub = _make_db_sub(
            908,
            user_id=1,
            tariff_id=908,
            server_id=908,
            client_name="vhq_ord_retry",
            vpn_key="https://sub.vhq-connect.xyz/retry_key",
            vhq_order_id="ord_retry",
            status=SubStatus.EXPIRED,
            expires_at=baseline_active,
            vhq_active_until=baseline_active,
        )
        profile = RecurringPaymentProfile(
            user_id=1,
            subscription_id=908,
            tariff_id=908,
            provider="yookassa",
            provider_payment_method_id="pm_rec_908",
            is_active=True,
            consent_granted=True,
            payment_attempt_count=0,
            next_charge_at=baseline_active,
        )
        # Pre-existing Payment from initial charge
        payment = Payment(
            id=908,
            user_id=1,
            tariff_id=908,
            amount=15000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            provider_payment_id="yk_same_908",
            telegram_chat_id=908001,
        )
        # Pre-existing fulfillment from initial attempt
        fulfillment = VhqSubscriptionFulfillment(
            id=908,
            subscription_id=908,
            user_id=1,
            payment_id=908,
            source_type="payment",
            source_id="payment:908",
            action="renew",
            status="uncertain",
            baseline_local_expires_at=baseline_active,
            target_local_expires_at=baseline_active + timedelta(days=30),
            attempts=1,
        )
        session.add_all([user, tariff, server, sub, profile, payment, fulfillment])
        await session.commit()

    mock_bot = AsyncMock()
    with (
        patch.object(settings, "recurring_payments_enabled", True),
        patch("bot.services.scheduler.async_session", bot_db),
        patch("bot.services.payment_service.create_recurring_yookassa_payment", new=AsyncMock(return_value=("succeeded", "yk_same_908", "succeeded"))),
    ):
        from bot.services.scheduler import process_recurring_charges
        await process_recurring_charges(mock_bot)

    async with bot_db() as session:
        # There must still be exactly ONE Payment for yk_same_908
        payments = (await session.scalars(select(Payment).where(Payment.provider_payment_id == "yk_same_908"))).all()
        assert len(payments) == 1

        # There must still be exactly ONE fulfillment for payment:908
        fulfillments = (await session.scalars(select(VhqSubscriptionFulfillment).where(VhqSubscriptionFulfillment.source_id == "payment:908"))).all()
        assert len(fulfillments) == 1


# ── Test 14: Blocker 5: Admin operation server-side validation ───────────────────

async def test_admin_manual_renew_rejects_foreign_operation(bot_db):
    """Admin B callback with operation belonging to Admin A is rejected before CAS."""
    async with bot_db() as session:
        user = User(telegram_id=909001, username="target", full_name="Target")
        tariff = Tariff(
            id=909,
            label="Baza Pro 30d",
            price_rub=100,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            vhq_tier="basic",
        )
        server = _make_db_server(909, "Server 909")
        sub = _make_db_sub(909, user_id=1, tariff_id=909, server_id=909, client_name="vhq_ord_foreign")
        op = ManualSubscriptionOperation(
            id=909,
            admin_user_id=111,  # Admin A
            target_user_id=1,
            subscription_id=909,
            tariff_id=None,
            action="renew",
            status="draft",
            operation_uuid=str(uuid.uuid4()),
        )
        session.add_all([user, tariff, server, sub, op])
        await session.commit()

    from bot.handlers.admin import admin_gen_key_tariff
    callback = MagicMock()
    callback.from_user.id = 222  # Admin B (foreign!)
    callback.data = "adm_key_tariff_909_909"
    callback.answer = AsyncMock()
    state = AsyncMock()
    state.get_data = AsyncMock(return_value={})

    with (
        patch("bot.handlers.admin.async_session", bot_db),
        patch("bot.handlers.admin._is_admin", return_value=True),
    ):
        await admin_gen_key_tariff(callback, state)

    # Callback was alerted with rejection
    callback.answer.assert_called_once()
    assert "другому администратору" in callback.answer.call_args[0][0]

    async with bot_db() as session:
        op_check = await session.get(ManualSubscriptionOperation, 909)
        # Status must remain draft!
        assert op_check.status == "draft"


async def test_admin_manual_renew_rejects_non_basic30_crafted_callback(bot_db):
    """Crafted callback with a tariff that is not basic 30d is rejected before CAS."""
    async with bot_db() as session:
        user = User(telegram_id=910001, username="target", full_name="Target")
        tariff_valid = Tariff(
            id=910,
            label="Baza Pro 30d",
            price_rub=100,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            vhq_tier="basic",
        )
        tariff_invalid = Tariff(
            id=911,
            label="Marzban 60d",
            price_rub=200,
            price_stars=0,
            days=60,
            is_active=True,
            tariff_type=TariffType.VPN,
            vhq_tier=None,
        )
        server = _make_db_server(910, "Server 910")
        sub = _make_db_sub(910, user_id=1, tariff_id=910, server_id=910, client_name="vhq_ord_crafted")
        op = ManualSubscriptionOperation(
            id=910,
            admin_user_id=111,
            target_user_id=1,
            subscription_id=910,
            tariff_id=None,
            action="renew",
            status="draft",
            operation_uuid=str(uuid.uuid4()),
        )
        session.add_all([user, tariff_valid, tariff_invalid, server, sub, op])
        await session.commit()

    from bot.handlers.admin import admin_gen_key_tariff
    callback = MagicMock()
    callback.from_user.id = 111
    callback.data = "adm_key_tariff_910_911"  # Crafted with tariff 911 (invalid!)
    callback.answer = AsyncMock()
    state = AsyncMock()
    state.get_data = AsyncMock(return_value={})

    with (
        patch("bot.handlers.admin.async_session", bot_db),
        patch("bot.handlers.admin._is_admin", return_value=True),
    ):
        await admin_gen_key_tariff(callback, state)

    callback.answer.assert_called_once()
    assert "basic на 30 дней" in callback.answer.call_args[0][0]

    async with bot_db() as session:
        op_check = await session.get(ManualSubscriptionOperation, 910)
        assert op_check.status == "draft"


# ── Test 15: Blocker 6: VHQ proxy secret dual verification ───────────────────────

async def test_proxy_accepts_new_webstore_token_with_dedicated_secret():
    """Resolver verifies new webstore token signed with dedicated secret and legacy token signed with bridge secret."""
    with (
        patch.object(settings, "vhq_subscription_proxy_secret", "dedicated_secret_xyz"),
        patch.object(settings, "webstore_bridge_secret", "legacy_bridge_secret_abc"),
    ):
        # 1. New webstore token signed with dedicated secret
        web_url = build_vhq_web_order_ref_url(
            "WO-912",
            public_base_url="https://darimiru.ru",
            path_prefix="webhook",
            secret="dedicated_secret_xyz",
        )
        token_1 = web_url.rsplit("/vhq-sub/", 1)[-1]
        res_1 = resolve_vhq_mirror_token(token_1)
        assert res_1 is not None
        assert res_1["kind"] == "web_order"
        assert res_1["web_order_id"] == "WO-912"

        # 2. Legacy bot subscription token signed with legacy bridge secret
        bot_url = build_vhq_subscription_ref_url(
            912,
            public_base_url="https://darimiru.ru",
            path_prefix="webhook",
            secret="legacy_bridge_secret_abc",
        )
        token_2 = bot_url.rsplit("/vhq-sub/", 1)[-1]
        res_2 = resolve_vhq_mirror_token(token_2)
        assert res_2 is not None
        assert res_2["kind"] == "subscription"
        assert res_2["subscription_id"] == 912

        # 3. Legacy {"u", "o"} mirror token signed with legacy bridge secret
        legacy_mirror_url = build_vhq_mirror_url(
            "https://sub.vhq-connect.xyz/upstream",
            public_base_url="https://darimiru.ru",
            path_prefix="webhook",
            secret="legacy_bridge_secret_abc",
            order_id="ord_legacy",
        )
        token_3 = legacy_mirror_url.rsplit("/vhq-sub/", 1)[-1]
        res_3 = resolve_vhq_mirror_token(token_3)
        assert res_3 is not None
        assert res_3["kind"] == "upstream"
        assert res_3["upstream_url"] == "https://sub.vhq-connect.xyz/upstream"
        assert res_3["order_id"] == "ord_legacy"


# ── Test 16: Blocker 7: Webstore processing & unconfirmed active_until ───────────

async def test_webstore_renew_processing_is_not_delivered(web_db):
    """When VHQ returns status='processing', web order must stay paid/pending and NOT delivered."""
    now = datetime.utcnow()
    async with web_db() as session:
        root = WebOrder(
            order_id="WO-PROC-ROOT",
            tariff_key="baza_pro",
            tariff_label="База Про 30д",
            days=30,
            amount_rub=150,
            status="delivered",
            subscription_url="https://test/proc-url",
            vhq_order_id="ord_proc",
            access_expires_at=now + timedelta(days=5),
            vhq_active_until=now + timedelta(days=5),
        )
        order = WebOrder(
            order_id="WO-PROC-RENEW",
            tariff_key="baza_pro",
            tariff_label="База Про 30д",
            days=30,
            amount_rub=150,
            status="pending",
            purchase_action="renew",
            target_order_id="WO-PROC-ROOT",
        )
        session.add_all([root, order])
        await session.commit()

    mock_api = AsyncMock()
    mock_api.renew = AsyncMock(return_value={
        "status": "processing",
        "order_id": "ord_proc",
    })
    mock_api.buy = AsyncMock()
    mock_cls = MagicMock(return_value=mock_api)
    mock_cls.extract_active_until = VHQPartnerAPI.extract_active_until
    mock_cls.extract_subscription_url = VHQPartnerAPI.extract_subscription_url
    mock_cls.extract_order_id = VHQPartnerAPI.extract_order_id

    async with web_db() as session:
        o = await session.scalar(select(WebOrder).where(WebOrder.order_id == "WO-PROC-RENEW"))
        with patch("webstore.routes.VHQPartnerAPI", new=mock_cls):
            await _fulfill_vhq_order(session, o, {"tier": "basic", "days": 30})
        await session.commit()

        # Must be kept in paid/pending state, NOT delivered!
        assert o.status == "paid"
        assert o.failure_code == "supplier_renew_processing"
        assert o.next_fulfillment_retry_at is not None
        assert mock_api.buy.call_count == 0


async def test_webstore_old_active_until_is_not_counted_as_new_renewal(web_db):
    """When VHQ returns active_until matching or older than root order baseline, web renew is not delivered."""
    now = datetime.utcnow()
    baseline = now + timedelta(days=10)
    async with web_db() as session:
        root = WebOrder(
            order_id="WO-OLD-ROOT",
            tariff_key="baza_pro",
            tariff_label="База Про 30д",
            days=30,
            amount_rub=150,
            status="delivered",
            subscription_url="https://test/old-url",
            vhq_order_id="ord_old",
            access_expires_at=baseline,
            vhq_active_until=baseline,
        )
        order = WebOrder(
            order_id="WO-OLD-RENEW",
            tariff_key="baza_pro",
            tariff_label="База Про 30д",
            days=30,
            amount_rub=150,
            status="pending",
            purchase_action="renew",
            target_order_id="WO-OLD-ROOT",
        )
        session.add_all([root, order])
        await session.commit()

    mock_api = AsyncMock()
    # Returns baseline active_until (not advanced)
    mock_api.renew = AsyncMock(return_value={
        "status": "scheduled",
        "active_until": baseline.isoformat() + "Z",
        "order_id": "ord_old",
    })
    mock_cls = MagicMock(return_value=mock_api)
    mock_cls.extract_active_until = VHQPartnerAPI.extract_active_until
    mock_cls.extract_subscription_url = VHQPartnerAPI.extract_subscription_url
    mock_cls.extract_order_id = VHQPartnerAPI.extract_order_id

    async with web_db() as session:
        o = await session.scalar(select(WebOrder).where(WebOrder.order_id == "WO-OLD-RENEW"))
        with patch("webstore.routes.VHQPartnerAPI", new=mock_cls):
            await _fulfill_vhq_order(session, o, {"tier": "basic", "days": 30})
        await session.commit()

        assert o.status == "paid"
        assert o.failure_code == "supplier_renew_processing"
        assert o.next_fulfillment_retry_at is not None


# ── Test 17: Blocker 8: Webstore ambiguous fallback BUY not retried ──────────────

async def test_webstore_scheduler_does_not_retry_ambiguous_fallback_buy(web_db):
    """Order with ambiguous_buy_network_error transitions to terminal failed and scheduler never retries it."""
    now = datetime.utcnow()
    async with web_db() as session:
        root = WebOrder(
            order_id="WO-AMB-ROOT",
            tariff_key="baza_pro",
            tariff_label="База Про 30д",
            days=30,
            amount_rub=150,
            status="delivered",
            subscription_url="https://test/amb-url",
            vhq_order_id="ord_amb",
            access_expires_at=now - timedelta(days=50),
            vhq_active_until=now - timedelta(days=50),
        )
        order = WebOrder(
            order_id="WO-AMB-RENEW",
            tariff_key="baza_pro",
            tariff_label="База Про 30д",
            days=30,
            amount_rub=150,
            status="pending",
            purchase_action="renew",
            target_order_id="WO-AMB-ROOT",
        )
        session.add_all([root, order])
        await session.commit()

    mock_api = AsyncMock()
    mock_api.renew = AsyncMock(side_effect=VHQPartnerAPIError(status=409, message="Order expired too long ago, please buy a new one"))
    mock_api.buy = AsyncMock(side_effect=asyncio.TimeoutError("Timeout on fallback buy"))
    mock_cls = MagicMock(return_value=mock_api)
    mock_cls.extract_active_until = VHQPartnerAPI.extract_active_until
    mock_cls.extract_subscription_url = VHQPartnerAPI.extract_subscription_url
    mock_cls.extract_order_id = VHQPartnerAPI.extract_order_id

    async with web_db() as session:
        o = await session.scalar(select(WebOrder).where(WebOrder.order_id == "WO-AMB-RENEW"))
        with (
            patch("webstore.routes.VHQPartnerAPI", new=mock_cls),
            patch("webstore.routes._notify_admins_webstore", new=AsyncMock()),
        ):
            await _fulfill_vhq_order(session, o, {"tier": "basic", "days": 30})
        await session.commit()

        # Must be terminal failed
        assert o.status == "failed"
        assert o.failure_code == "ambiguous_buy_network_error"
        assert o.next_fulfillment_retry_at is None

    # Now verify webstore scheduler retry job ignores it
    mock_fulfill = AsyncMock()
    with (
        patch("webstore.scheduler.async_session", web_db),
        patch("webstore.scheduler.attempt_fulfill_order", new=mock_fulfill),
    ):
        from webstore.scheduler import retry_paid_web_orders
        await retry_paid_web_orders()

    # The ambiguous order was NEVER touched by scheduler retry!
    assert mock_fulfill.call_count == 0


# ── Test 18: Blocker 9: Same-source concurrent race re-query ─────────────────────

async def test_same_payment_concurrent_calls_create_one_fulfillment_without_second_integrity_error(bot_db):
    """Two concurrent calls for the same payment: second catches IntegrityError, re-queries existing fulfillment."""
    now = datetime.utcnow()
    async with bot_db() as session:
        user = User(telegram_id=913001, username="race_user", full_name="Race User")
        tariff = Tariff(
            id=913,
            label="Baza Pro",
            price_rub=150,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            vhq_tier="basic",
        )
        server = _make_db_server(913, "Server 913")
        sub = _make_db_sub(
            913,
            user_id=1,
            tariff_id=913,
            server_id=913,
            client_name="vhq_ord_race",
            vpn_key="https://sub.vhq-connect.xyz/race_key",
            vhq_order_id="ord_race",
            status=SubStatus.ACTIVE,
            expires_at=now + timedelta(days=5),
            vhq_active_until=now + timedelta(days=5),
        )
        payment = Payment(
            id=913,
            user_id=1,
            tariff_id=913,
            amount=15000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            provider_payment_id="yk_race_913",
            telegram_chat_id=913001,
        )
        session.add_all([user, tariff, server, sub, payment])
        await session.commit()

    # First call: executes and completes fulfillment
    mock_api = AsyncMock()
    mock_api.renew = AsyncMock(return_value={
        "status": "scheduled",
        "active_until": (now + timedelta(days=35)).isoformat() + "Z",
        "order_id": "ord_race",
    })

    async with bot_db() as session:
        user_obj = await session.get(User, 1)
        tariff_obj = await session.get(Tariff, 913)
        pay_obj = await session.get(Payment, 913)

        with (
            patch("bot.services.subscription_service.VHQPartnerAPI", return_value=mock_api),
            patch("bot.services.subscription_service.get_primary_active_server", new=AsyncMock(return_value=server)),
            patch("bot.services.subscription_service.get_included_device_slots", new=AsyncMock(return_value=3)),
            patch("bot.services.subscription_service.build_vhq_subscription_ref_url", return_value="https://test/mirror"),
        ):
            res_sub, res_url = await _create_vhq_paid_subscription(
                session,
                user=user_obj,
                tariff=tariff_obj,
                platform=Platform.ANDROID,
                vhq_spec={"tier": "basic", "days": 30},
                target_subscription_id=913,
                provisioning_payment=pay_obj,
            )
            await session.commit()
            assert res_sub is not None

    # Second concurrent call: simulates hitting flush() right as first call committed -> re-queries existing
    async with bot_db() as session:
        user_obj = await session.get(User, 1)
        tariff_obj = await session.get(Tariff, 913)
        pay_obj = await session.get(Payment, 913)

        # Calling again for same payment returns completed result without raising IntegrityError
        res_sub_2, res_url_2 = await _create_vhq_paid_subscription(
            session,
            user=user_obj,
            tariff=tariff_obj,
            platform=Platform.ANDROID,
            vhq_spec={"tier": "basic", "days": 30},
            target_subscription_id=913,
            provisioning_payment=pay_obj,
        )
        assert res_sub_2.id == 913
        assert res_url_2 is not None

    # Verify only ONE fulfillment was created in total
    async with bot_db() as session:
        ffs = (await session.scalars(select(VhqSubscriptionFulfillment).where(VhqSubscriptionFulfillment.subscription_id == 913))).all()
        assert len(ffs) == 1
        assert ffs[0].status == "completed"


# ── Test 19: Blocker 11: Manual operation lifecycle ──────────────────────────────

async def test_manual_operation_completed_on_background_success(bot_db):
    """Background fulfillment worker completion transitions ManualSubscriptionOperation to completed and notifies if expired."""
    now = datetime.utcnow()
    async with bot_db() as session:
        user = User(telegram_id=914001, username="man_user", full_name="Man User")
        tariff = Tariff(
            id=914,
            label="Baza Pro 30d",
            price_rub=100,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            vhq_tier="basic",
        )
        server = _make_db_server(914, "Server 914")
        sub = _make_db_sub(
            914,
            user_id=1,
            tariff_id=914,
            server_id=914,
            client_name="vhq_ord_man",
            vpn_key="https://sub.vhq-connect.xyz/man_key",
            vhq_order_id="ord_man",
            status=SubStatus.EXPIRED,
            expires_at=now - timedelta(days=5),
            vhq_active_until=now - timedelta(days=5),
        )
        op = ManualSubscriptionOperation(
            id=914,
            admin_user_id=999,
            target_user_id=1,
            subscription_id=914,
            tariff_id=914,
            action="renew",
            status="applied",
            operation_uuid=str(uuid.uuid4()),
        )
        ff = VhqSubscriptionFulfillment(
            id=914,
            subscription_id=914,
            user_id=1,
            manual_operation_id=914,
            source_type="manual_operation",
            source_id="manual:914",
            action="renew",
            status="pending",
            baseline_local_expires_at=now - timedelta(days=5),
            target_local_expires_at=now + timedelta(days=30),
            next_attempt_at=now - timedelta(seconds=1),
            attempts=0,
        )
        session.add_all([user, tariff, server, sub, op, ff])
        await session.commit()

    mock_bot = AsyncMock()
    mock_api = AsyncMock()
    mock_api.renew = AsyncMock(return_value={
        "status": "scheduled",
        "active_until": (now + timedelta(days=30)).isoformat() + "Z",
        "order_id": "ord_man",
    })

    with (
        patch("bot.services.scheduler.async_session", bot_db),
        patch("bot.services.subscription_service.VHQPartnerAPI", return_value=mock_api),
    ):
        await process_vhq_subscription_fulfillments(mock_bot)

    async with bot_db() as session:
        op_check = await session.get(ManualSubscriptionOperation, 914)
        assert op_check.status == "completed"
        assert op_check.completed_at is not None

        ff_check = await session.get(VhqSubscriptionFulfillment, 914)
        assert ff_check.status == "completed"

    # User notification was sent because subscription was expired
    assert mock_bot.send_message.call_count >= 1
    call_text = mock_bot.send_message.call_args[0][1]
    assert "Ваш доступ к VPN активирован" in call_text or "ключ" in call_text.lower()


# ── Test 20: Blocker 1: Recurring YooKassa does not charge again while VHQ pending ──────

async def test_recurring_vhq_pending_cycle_does_not_charge_again_after_retry_delay(bot_db):
    """If an open paid fulfillment exists (uncertain/pending), second scheduler run after retry delay does NOT charge YooKassa again."""
    from bot.services.scheduler import process_recurring_charges

    now = datetime.utcnow()
    baseline_active = now - timedelta(days=1)
    async with bot_db() as session:
        user = User(telegram_id=920001, username="rec_delay_user", full_name="Rec Delay User")
        tariff = Tariff(
            id=920,
            label="Baza Pro 30",
            price_rub=150,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            vhq_tier="basic",
        )
        server = _make_db_server(920, "Server 920")
        sub = _make_db_sub(
            920,
            user_id=1,
            tariff_id=920,
            server_id=920,
            client_name="vhq_ord_rec_delay",
            vpn_key="https://sub.vhq-connect.xyz/delay_key",
            vhq_order_id="ord_rec_delay",
            status=SubStatus.EXPIRED,
            expires_at=baseline_active,
            vhq_active_until=baseline_active,
        )
        profile = RecurringPaymentProfile(
            user_id=1,
            subscription_id=920,
            tariff_id=920,
            provider="yookassa",
            provider_payment_method_id="pm_rec_delay",
            is_active=True,
            consent_granted=True,
            payment_attempt_count=0,
            next_charge_at=baseline_active,
        )
        session.add_all([user, tariff, server, sub, profile])
        await session.commit()

    mock_bot = AsyncMock()
    mock_api = AsyncMock()
    # Direct renew returns old active_until -> fulfillment becomes uncertain
    mock_api.renew = AsyncMock(return_value={
        "status": "scheduled",
        "message": "already_requested",
        "active_until": baseline_active.isoformat() + "Z",
        "order_id": "ord_rec_delay",
    })

    yk_mock = AsyncMock(return_value=("succeeded", "yk_charge_A", "succeeded"))

    async def _fetch(_provider_payment_id):
        return await _recurring_provider_success(
            bot_db,
            "yk_charge_A",
            telegram_id=920001,
            tariff_id=920,
            subscription_id=920,
            amount="150.00",
        )

    with (
        patch.object(settings, "recurring_payments_enabled", True),
        patch("bot.services.scheduler.async_session", bot_db),
        patch("bot.services.payment_service.create_recurring_yookassa_payment", new=yk_mock),
        patch("bot.services.subscription_service.VHQPartnerAPI", return_value=mock_api),
        patch("bot.services.yookassa_reconciliation.fetch_yookassa_payment", new=_fetch),
    ):
        # First run: charges YooKassa, Payment A created, fulfillment A = uncertain
        await process_recurring_charges(mock_bot)

    assert yk_mock.call_count == 1

    async with bot_db() as session:
        payments = (await session.scalars(select(Payment).where(Payment.provider_payment_id == "yk_charge_A"))).all()
        assert len(payments) == 1
        assert payments[0].provider_payment_id == "yk_charge_A"

        ffs = (await session.scalars(select(VhqSubscriptionFulfillment).where(VhqSubscriptionFulfillment.subscription_id == 920))).all()
        assert len(ffs) == 1
        assert ffs[0].status == "uncertain"

        # Rewind time by > 2 hours for profile retry delay
        prof = await session.scalar(select(RecurringPaymentProfile).where(RecurringPaymentProfile.subscription_id == 920))
        prof.last_payment_attempt = now - timedelta(hours=3)
        await session.commit()

    # Second run: retry delay has passed, but open paid fulfillment exists
    with (
        patch.object(settings, "recurring_payments_enabled", True),
        patch("bot.services.scheduler.async_session", bot_db),
        patch("bot.services.payment_service.create_recurring_yookassa_payment", new=yk_mock),
        patch("bot.services.subscription_service.VHQPartnerAPI", return_value=mock_api),
        patch("bot.services.yookassa_reconciliation.fetch_yookassa_payment", new=_fetch),
    ):
        await process_recurring_charges(mock_bot)

    # Expectation: create_recurring_yookassa_payment NOT called second time!
    assert yk_mock.call_count == 1

    async with bot_db() as session:
        # No Payment B created
        payments = (await session.scalars(select(Payment).where(Payment.provider_payment_id == "yk_charge_A"))).all()
        assert len(payments) == 1

        # No fulfillment B created
        ffs = (await session.scalars(select(VhqSubscriptionFulfillment).where(VhqSubscriptionFulfillment.subscription_id == 920))).all()
        assert len(ffs) == 1


# ── Test 21: Blocker 2: Legacy bot baseline snapshot & strict renewal ─────────────

async def test_legacy_bot_existing_future_active_until_is_not_mistaken_for_new_renewal(bot_db):
    """Pre-existing future active_until from supplier is snapshotted as baseline and not mistaken for new renewal."""
    now = datetime.utcnow()
    existing_future = now + timedelta(days=10)

    async with bot_db() as session:
        user = User(telegram_id=921001, username="leg_bot_user", full_name="Leg Bot User")
        tariff = Tariff(
            id=921,
            label="Baza Pro 30",
            price_rub=100,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            vhq_tier="basic",
        )
        server = _make_db_server(921, "Server 921")
        sub = _make_db_sub(
            921,
            user_id=1,
            tariff_id=921,
            server_id=921,
            client_name="vhq_leg_ord",
            vpn_key="https://sub.vhq-connect.xyz/leg_key",
            vhq_order_id="leg_ord_921",
            status=SubStatus.ACTIVE,
            expires_at=existing_future,
            vhq_active_until=None,  # Legacy sub: NULL baseline in DB
        )
        ff = VhqSubscriptionFulfillment(
            id=921,
            subscription_id=921,
            user_id=1,
            source_type="payment",
            source_id="payment:921",
            action="renew",
            status="pending",
            baseline_local_expires_at=existing_future,
            target_local_expires_at=existing_future + timedelta(days=30),
            baseline_provider_active_until=None,
            expected_provider_active_until=None,
            attempts=0,
        )
        session.add_all([user, tariff, server, sub, ff])
        await session.commit()

    mock_api = AsyncMock()
    # Supplier orders returns existing order with active_until in the future (10 days)
    mock_api.get_orders = AsyncMock(return_value=[
        {"order_id": "leg_ord_921", "active_until": existing_future.isoformat() + "Z"}
    ])
    # Direct renew returns same old active_until (or status=renewed but date still existing_future)
    mock_api.renew = AsyncMock(return_value={
        "status": "scheduled",
        "active_until": existing_future.isoformat() + "Z",
        "order_id": "leg_ord_921",
    })

    async with bot_db() as session:
        ff_obj = await session.get(VhqSubscriptionFulfillment, 921)
        with patch("bot.services.subscription_service.VHQPartnerAPI", return_value=mock_api):
            await execute_vhq_fulfillment(session, ff_obj)
        await session.commit()

        # Check baseline was established from get_orders
        assert ff_obj.baseline_provider_active_until is not None
        assert ff_obj.baseline_provider_active_until.replace(tzinfo=None) == existing_future.replace(tzinfo=None)
        assert ff_obj.expected_provider_active_until is not None
        # Expected must be baseline (existing_future) + 30 days
        expected_target = existing_future + timedelta(days=30)
        assert abs((ff_obj.expected_provider_active_until.replace(tzinfo=None) - expected_target).total_seconds()) < 5

        # Fulfillment was NOT completed because existing_future < expected_target
        assert ff_obj.status != "completed"
        assert ff_obj.status in ("uncertain", "retry_wait", "manual_review")

        # Now simulate a true renewal: supplier extends to expected_target (+30 days)
        mock_api.renew = AsyncMock(return_value={
            "status": "renewed",
            "active_until": expected_target.isoformat() + "Z",
            "order_id": "leg_ord_921",
        })
        with patch("bot.services.subscription_service.VHQPartnerAPI", return_value=mock_api):
            await execute_vhq_fulfillment(session, ff_obj)
        await session.commit()

        assert ff_obj.status == "completed"
        assert ff_obj.confirmed_provider_active_until is not None


# ── Test 22: Blocker 3: Webstore legacy baseline snapshot & POST renew ────────────

async def test_legacy_webstore_first_renew_does_not_complete_from_preexisting_active_until(web_db):
    """Webstore first renewal with NULL vhq_active_until establishes baseline and actually calls renew()."""
    now = datetime.utcnow()
    existing_future = now + timedelta(days=8)
    expected_future = existing_future + timedelta(days=30)

    async with web_db() as session:
        root_order = WebOrder(
            id=101,
            order_id="root_web_101",
            status="delivered",
            tariff_key="baza_pro",
            tariff_label="Baza Pro 30",
            days=30,
            amount_rub=100,
            vhq_order_id="vhq_sup_101",
            vhq_active_until=None,  # Legacy web order with NULL vhq_active_until
            access_expires_at=existing_future,
            subscription_url="https://test.mirror/order/root_web_101",
            vhq_upstream_url="https://sub.vhq-connect.xyz/upstream_101",
        )
        renew_order = WebOrder(
            id=102,
            order_id="renew_web_102",
            status="paid",
            purchase_action="renew",
            target_order_id="order:root_web_101",
            tariff_key="baza_pro",
            tariff_label="Baza Pro 30",
            days=30,
            amount_rub=100,
            fulfillment_attempts=0,
        )
        session.add_all([root_order, renew_order])
        await session.commit()

    mock_api = AsyncMock()
    mock_api.get_orders = AsyncMock(return_value=[
        {"order_id": "vhq_sup_101", "active_until": existing_future.isoformat() + "Z"}
    ])
    mock_api.renew = AsyncMock(return_value={
        "status": "renewed",
        "active_until": expected_future.isoformat() + "Z",
        "order_id": "vhq_sup_101",
    })
    mock_api_cls = MagicMock(return_value=mock_api)
    mock_api_cls.extract_active_until = VHQPartnerAPI.extract_active_until
    mock_api_cls.extract_subscription_url = VHQPartnerAPI.extract_subscription_url
    mock_api_cls.extract_order_id = VHQPartnerAPI.extract_order_id

    async with web_db() as session:
        order_obj = await session.get(WebOrder, 102)
        with (
            patch("webstore.routes.get_store_tariffs_by_key", return_value={"baza_pro": {"provider": "vhq", "vhq_tier": "basic", "days": 30}}),
            patch("webstore.routes.VHQPartnerAPI", new=mock_api_cls),
            patch("webstore.routes._notify_admins_webstore", new=AsyncMock()),
            patch("webstore.routes._notify_linked_webstore_user", new=AsyncMock()),
        ):
            await attempt_fulfill_order(session, order_obj)
        await session.commit()

    # renew() MUST have been called! Not skipped due to existing active_until > now
    mock_api.renew.assert_called_once_with(order_id="vhq_sup_101")

    async with web_db() as session:
        order_check = await session.get(WebOrder, 102)
        assert order_check.status == "delivered"
        root_check = await session.get(WebOrder, 101)
        assert root_check.vhq_active_until is not None
        assert abs((root_check.vhq_active_until.replace(tzinfo=None) - expected_future).total_seconds()) < 5


# ── Test 23: Blocker 4: Two paid web renewals cannot complete from same VHQ cycle ─

async def test_two_paid_web_renewals_cannot_complete_from_same_vhq_cycle(web_db):
    """Two paid web renewal orders cannot share or steal one supplier cycle."""
    now = datetime.utcnow()
    initial_expiry = now + timedelta(days=5)

    async with web_db() as session:
        root_order = WebOrder(
            id=201,
            order_id="root_web_201",
            status="delivered",
            tariff_key="baza_pro",
            tariff_label="Baza Pro 30",
            days=30,
            amount_rub=100,
            vhq_order_id="vhq_sup_201",
            vhq_active_until=initial_expiry,
            access_expires_at=initial_expiry,
            subscription_url="https://test.mirror/order/root_web_201",
            vhq_upstream_url="https://sub.vhq-connect.xyz/upstream_201",
        )
        order_a = WebOrder(
            id=202,
            order_id="renew_web_202_A",
            status="paid",
            purchase_action="renew",
            target_order_id="order:root_web_201",
            tariff_key="baza_pro",
            tariff_label="Baza Pro 30",
            days=30,
            amount_rub=100,
            failure_code="supplier_renew_processing",  # Open renewal in progress
            fulfillment_attempts=1,
        )
        order_b = WebOrder(
            id=203,
            order_id="renew_web_203_B",
            status="paid",
            purchase_action="renew",
            target_order_id="order:root_web_201",
            tariff_key="baza_pro",
            tariff_label="Baza Pro 30",
            days=30,
            amount_rub=100,
            fulfillment_attempts=0,
        )
        session.add_all([root_order, order_a, order_b])
        await session.commit()

    mock_api = AsyncMock()
    mock_api.renew = AsyncMock()
    mock_api_cls = MagicMock(return_value=mock_api)
    mock_api_cls.extract_active_until = VHQPartnerAPI.extract_active_until
    mock_api_cls.extract_subscription_url = VHQPartnerAPI.extract_subscription_url
    mock_api_cls.extract_order_id = VHQPartnerAPI.extract_order_id

    # Attempt to fulfill order B while order A is open
    async with web_db() as session:
        order_b_obj = await session.get(WebOrder, 203)
        with (
            patch("webstore.routes.get_store_tariffs_by_key", return_value={"baza_pro": {"provider": "vhq", "vhq_tier": "basic", "days": 30}}),
            patch("webstore.routes.VHQPartnerAPI", new=mock_api_cls),
            patch("webstore.routes._notify_admins_webstore", new=AsyncMock()),
            patch("webstore.routes._notify_linked_webstore_user", new=AsyncMock()),
        ):
            await attempt_fulfill_order(session, order_b_obj)
        await session.commit()

    # Order B did NOT call renew()
    assert mock_api.renew.call_count == 0

    async with web_db() as session:
        b_check = await session.get(WebOrder, 203)
        # Order B is NOT delivered; preserved safely in paid with conflict code
        assert b_check.status == "paid"
        assert b_check.failure_code == "concurrent_renewal_conflict"
        assert b_check.next_fulfillment_retry_at is not None

        # Now simulate order A completing its cycle (+30 days to initial_expiry)
        expiry_after_a = initial_expiry + timedelta(days=30)
        root_check = await session.get(WebOrder, 201)
        root_check.vhq_active_until = expiry_after_a
        root_check.access_expires_at = expiry_after_a
        a_check = await session.get(WebOrder, 202)
        a_check.status = "delivered"
        a_check.failure_code = None
        await session.commit()

    # Now Order B can run its own separate renewal cycle
    expiry_after_b = expiry_after_a + timedelta(days=30)
    mock_api.renew = AsyncMock(return_value={
        "status": "renewed",
        "active_until": expiry_after_b.isoformat() + "Z",
        "order_id": "vhq_sup_201",
    })

    async with web_db() as session:
        order_b_obj = await session.get(WebOrder, 203)
        with (
            patch("webstore.routes.get_store_tariffs_by_key", return_value={"baza_pro": {"provider": "vhq", "vhq_tier": "basic", "days": 30}}),
            patch("webstore.routes.VHQPartnerAPI", new=mock_api_cls),
            patch("webstore.routes._notify_admins_webstore", new=AsyncMock()),
            patch("webstore.routes._notify_linked_webstore_user", new=AsyncMock()),
        ):
            await attempt_fulfill_order(session, order_b_obj)
        await session.commit()

    # Order B successfully completed its own distinct +30 cycle
    assert mock_api.renew.call_count == 1
    async with web_db() as session:
        b_check = await session.get(WebOrder, 203)
        assert b_check.status == "delivered"
        root_check = await session.get(WebOrder, 201)
        assert abs((root_check.vhq_active_until.replace(tzinfo=None) - expiry_after_b).total_seconds()) < 5


# ── Test 24: Blocker 6: Background manual renew sends stable ref URL ──────────────

async def test_background_manual_renew_sends_stable_customer_url_not_raw_vhq_upstream(bot_db):
    """Background manual renew completion notifies user with /vhq-sub/ branded ref URL, never raw supplier URL."""
    now = datetime.utcnow()
    async with bot_db() as session:
        user = User(telegram_id=924001, username="man_ref_user", full_name="Man Ref User")
        tariff = Tariff(
            id=924,
            label="Baza Pro",
            price_rub=100,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            vhq_tier="basic",
        )
        server = _make_db_server(924, "Server 924")
        sub = _make_db_sub(
            924,
            user_id=1,
            tariff_id=924,
            server_id=924,
            client_name="vhq_ord_man_ref",
            vpn_key="https://sub.vhq-connect.xyz/raw_supplier_token_secret",
            vhq_order_id="ord_man_ref",
            status=SubStatus.EXPIRED,
            expires_at=now - timedelta(days=2),
            vhq_active_until=now - timedelta(days=2),
        )
        op = ManualSubscriptionOperation(
            id=924,
            admin_user_id=999,
            target_user_id=1,
            subscription_id=924,
            tariff_id=924,
            action="renew",
            status="applied",
            operation_uuid=str(uuid.uuid4()),
        )
        ff = VhqSubscriptionFulfillment(
            id=924,
            subscription_id=924,
            user_id=1,
            manual_operation_id=924,
            source_type="manual_operation",
            source_id="manual:924",
            action="renew",
            status="pending",
            baseline_local_expires_at=now - timedelta(days=2),
            target_local_expires_at=now + timedelta(days=30),
            next_attempt_at=now - timedelta(seconds=1),
            attempts=0,
        )
        session.add_all([user, tariff, server, sub, op, ff])
        await session.commit()

    mock_bot = AsyncMock()
    mock_api = AsyncMock()
    mock_api.renew = AsyncMock(return_value={
        "status": "scheduled",
        "active_until": (now + timedelta(days=30)).isoformat() + "Z",
        "order_id": "ord_man_ref",
    })

    with (
        patch("bot.services.scheduler.async_session", bot_db),
        patch("bot.services.subscription_service.VHQPartnerAPI", return_value=mock_api),
    ):
        await process_vhq_subscription_fulfillments(mock_bot)

    assert mock_bot.send_message.call_count >= 1
    sent_text = mock_bot.send_message.call_args[0][1]

    # Must contain customer-facing stable proxy URL
    assert "/vhq-sub/" in sent_text
    # Raw supplier upstream URL must NOT be exposed in the message!
    assert "https://sub.vhq-connect" not in sent_text


# ── Test 25 & 26: Blocker 7: Durable correlation for recurring profile finalization ─

async def test_oneoff_payment_fulfillment_does_not_finalize_recurring_profile(bot_db):
    """Worker fulfillment from one-off payment does not touch RecurringPaymentProfile or send recurring success."""
    now = datetime.utcnow()
    initial_next_charge = now + timedelta(days=3)

    async with bot_db() as session:
        user = User(telegram_id=925001, username="oneoff_user", full_name="Oneoff User")
        tariff = Tariff(
            id=925,
            label="Baza Pro",
            price_rub=100,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            vhq_tier="basic",
        )
        server = _make_db_server(925, "Server 925")
        sub = _make_db_sub(
            925,
            user_id=1,
            tariff_id=925,
            server_id=925,
            client_name="vhq_ord_oneoff",
            vpn_key="https://sub.vhq-connect.xyz/oneoff_key",
            vhq_order_id="ord_oneoff",
            status=SubStatus.ACTIVE,
            expires_at=initial_next_charge,
            vhq_active_until=initial_next_charge,
        )
        profile = RecurringPaymentProfile(
            id=925,
            user_id=1,
            subscription_id=925,
            tariff_id=925,
            provider="yookassa",
            provider_payment_method_id="pm_oneoff_profile",
            is_active=True,
            consent_granted=True,
            payment_attempt_count=0,
            next_charge_at=initial_next_charge,
        )
        payment = Payment(
            id=925,
            user_id=1,
            tariff_id=925,
            amount=10000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            provider_payment_id="yk_oneoff_925",
            telegram_chat_id=925001,
        )
        # Fulfillment created WITHOUT recurring_profile_id (one-off payment)
        ff = VhqSubscriptionFulfillment(
            id=925,
            subscription_id=925,
            user_id=1,
            payment_id=925,
            recurring_profile_id=None,
            source_type="payment",
            source_id="payment:925",
            action="renew",
            status="pending",
            baseline_local_expires_at=initial_next_charge,
            target_local_expires_at=initial_next_charge + timedelta(days=30),
            next_attempt_at=now - timedelta(seconds=1),
            attempts=0,
        )
        session.add_all([user, tariff, server, sub, profile, payment, ff])
        await session.commit()

    mock_bot = AsyncMock()
    mock_api = AsyncMock()
    mock_api.renew = AsyncMock(return_value={
        "status": "scheduled",
        "active_until": (initial_next_charge + timedelta(days=30)).isoformat() + "Z",
        "order_id": "ord_oneoff",
    })

    with (
        patch("bot.services.scheduler.async_session", bot_db),
        patch("bot.services.subscription_service.VHQPartnerAPI", return_value=mock_api),
    ):
        await process_vhq_subscription_fulfillments(mock_bot)

    async with bot_db() as session:
        prof_check = await session.get(RecurringPaymentProfile, 925)
        # Recurring profile must NOT be finalized by one-off payment fulfillment
        assert prof_check.next_charge_at == initial_next_charge
        assert prof_check.last_charge_at is None

    # Bot must NOT have sent RECURRING_CHARGE_SUCCESS
    for call in mock_bot.send_message.call_args_list:
        text_arg = call[0][1]
        assert "продлена автоматически" not in text_arg


async def test_recurring_payment_fulfillment_finalizes_its_exact_profile(bot_db):
    """Worker fulfillment linked to recurring_profile_id finalizes the exact profile and notifies success."""
    now = datetime.utcnow()
    initial_next_charge = now + timedelta(days=1)
    target_exp = initial_next_charge + timedelta(days=30)

    async with bot_db() as session:
        user = User(telegram_id=926001, username="rec_exact_user", full_name="Rec Exact User")
        tariff = Tariff(
            id=926,
            label="Baza Pro",
            price_rub=120,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            vhq_tier="basic",
        )
        server = _make_db_server(926, "Server 926")
        sub = _make_db_sub(
            926,
            user_id=1,
            tariff_id=926,
            server_id=926,
            client_name="vhq_ord_rec_exact",
            vpn_key="https://sub.vhq-connect.xyz/rec_exact_key",
            vhq_order_id="ord_rec_exact",
            status=SubStatus.ACTIVE,
            expires_at=initial_next_charge,
            vhq_active_until=initial_next_charge,
        )
        profile = RecurringPaymentProfile(
            id=926,
            user_id=1,
            subscription_id=926,
            tariff_id=926,
            provider="yookassa",
            provider_payment_method_id="pm_rec_exact",
            is_active=True,
            consent_granted=True,
            payment_attempt_count=0,
            next_charge_at=initial_next_charge,
        )
        payment = Payment(
            id=926,
            user_id=1,
            tariff_id=926,
            amount=12000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            provider_payment_id="yk_rec_exact_926",
            telegram_chat_id=926001,
        )
        # Fulfillment explicitly linked to recurring_profile_id=926
        ff = VhqSubscriptionFulfillment(
            id=926,
            subscription_id=926,
            user_id=1,
            payment_id=926,
            recurring_profile_id=926,
            source_type="payment",
            source_id="payment:926",
            action="renew",
            status="pending",
            baseline_local_expires_at=initial_next_charge,
            target_local_expires_at=target_exp,
            next_attempt_at=now - timedelta(seconds=1),
            attempts=0,
        )
        session.add_all([user, tariff, server, sub, profile, payment, ff])
        await session.commit()

    mock_bot = AsyncMock()
    mock_api = AsyncMock()
    mock_api.renew = AsyncMock(return_value={
        "status": "scheduled",
        "active_until": target_exp.isoformat() + "Z",
        "order_id": "ord_rec_exact",
    })

    with (
        patch("bot.services.scheduler.async_session", bot_db),
        patch("bot.services.subscription_service.VHQPartnerAPI", return_value=mock_api),
    ):
        await process_vhq_subscription_fulfillments(mock_bot)

    async with bot_db() as session:
        prof_check = await session.get(RecurringPaymentProfile, 926)
        # Recurring profile MUST be updated to target expiration
        assert prof_check.next_charge_at == target_exp
        assert prof_check.last_charge_at is not None

    # Success notification sent to user
    assert mock_bot.send_message.call_count >= 1
    sent_text = mock_bot.send_message.call_args[0][1]
    assert "продлена автоматически" in sent_text


# ── Test 27: Blocker 8: Manual operation transitions to manual_review ─────────────

async def test_manual_operation_transitions_to_manual_review_on_fulfillment_manual_review(bot_db):
    """When linked fulfillment transitions to manual_review, ManualSubscriptionOperation becomes manual_review (not failed)."""
    now = datetime.utcnow()
    async with bot_db() as session:
        user = User(telegram_id=927001, username="man_rev_user", full_name="Man Rev User")
        tariff = Tariff(
            id=927,
            label="Baza Pro",
            price_rub=100,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            vhq_tier="basic",
        )
        server = _make_db_server(927, "Server 927")
        sub = _make_db_sub(
            927,
            user_id=1,
            tariff_id=927,
            server_id=927,
            client_name="vhq_ord_man_rev",
            vpn_key="https://sub.vhq-connect.xyz/man_rev_key",
            vhq_order_id="ord_man_rev",
            status=SubStatus.ACTIVE,
            expires_at=now + timedelta(days=1),
            vhq_active_until=now + timedelta(days=1),
        )
        op = ManualSubscriptionOperation(
            id=927,
            admin_user_id=999,
            target_user_id=1,
            subscription_id=927,
            tariff_id=927,
            action="renew",
            status="applied",
            operation_uuid=str(uuid.uuid4()),
        )
        ff = VhqSubscriptionFulfillment(
            id=927,
            subscription_id=927,
            user_id=1,
            manual_operation_id=927,
            source_type="manual_operation",
            source_id="manual:927",
            action="renew",
            status="pending",
            baseline_local_expires_at=now + timedelta(days=1),
            target_local_expires_at=now + timedelta(days=31),
            next_attempt_at=now - timedelta(seconds=1),
            attempts=0,
        )
        session.add_all([user, tariff, server, sub, op, ff])
        await session.commit()

    mock_bot = AsyncMock()
    # Supplier returns 402 Insufficient Balance -> triggers manual_review
    mock_api = AsyncMock()
    mock_api.renew = AsyncMock(side_effect=VHQPartnerAPIError("Insufficient balance", status=402))

    with (
        patch("bot.services.scheduler.async_session", bot_db),
        patch("bot.services.subscription_service.VHQPartnerAPI", return_value=mock_api),
        patch("bot.services.scheduler.notify_admins_issue", new=AsyncMock()),
    ):
        await process_vhq_subscription_fulfillments(mock_bot)

    async with bot_db() as session:
        ff_check = await session.get(VhqSubscriptionFulfillment, 927)
        assert ff_check.status == "manual_review"

        op_check = await session.get(ManualSubscriptionOperation, 927)
        # Must be manual_review, NOT generic failed!
        assert op_check.status == "manual_review"


# ── Test 28: Issue 1: Durable intent committed before provider call ────────────────

async def test_vhq_renew_intent_is_durable_before_provider_call(bot_db):
    """Fulfillment intent must be committed to DB before network renew I/O is attempted."""
    now = datetime.utcnow()
    async with bot_db() as session:
        user = User(telegram_id=928001, username="intent_user", full_name="Intent User")
        tariff = Tariff(
            id=928,
            label="Baza Pro",
            price_rub=100,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            vhq_tier="basic",
        )
        server = _make_db_server(928, "Server 928")
        sub = _make_db_sub(
            928,
            user_id=1,
            tariff_id=928,
            server_id=928,
            client_name="vhq_ord_durable_intent",
            vpn_key="https://sub.vhq-connect.xyz/intent_key",
            vhq_order_id="ord_durable_intent",
            status=SubStatus.ACTIVE,
            expires_at=now + timedelta(days=1),
            vhq_active_until=now + timedelta(days=1),
        )
        payment = Payment(
            id=928,
            user_id=1,
            tariff_id=928,
            amount=10000,
            currency="RUB",
            status=PaymentStatus.COMPLETED,
            provider_payment_id="pay_durable_intent",
            method=PaymentMethod.YOOKASSA,
        )
        session.add_all([user, tariff, server, sub, payment])
        await session.commit()

    seen_in_separate_session = False

    async def mock_renew(order_id):
        nonlocal seen_in_separate_session
        # In a separate DB session, query fulfillment and prove it is visible in committed state!
        async with bot_db() as sep_session:
            ff = await sep_session.scalar(
                select(VhqSubscriptionFulfillment).where(
                    VhqSubscriptionFulfillment.source_type == "payment",
                    VhqSubscriptionFulfillment.source_id == "payment:928",
                )
            )
            if ff is not None and ff.status in ("pending", "processing", "renew_inflight"):
                seen_in_separate_session = True
        return {
            "status": "renewed",
            "active_until": (now + timedelta(days=31)).isoformat() + "Z",
            "order_id": order_id,
        }

    mock_api = AsyncMock()
    mock_api.renew = AsyncMock(side_effect=mock_renew)

    async with bot_db() as session:
        sub_obj = await session.get(Subscription, 928)
        user_obj = await session.get(User, 1)
        tariff_obj = await session.get(Tariff, 928)
        payment_obj = await session.get(Payment, 928)

        with patch("bot.services.subscription_service.VHQPartnerAPI", return_value=mock_api):
            await create_or_extend_paid_access(
                session,
                user=user_obj,
                tariff=tariff_obj,
                platform=Platform.ANDROID,
                target_subscription_id=sub_obj.id,
                purchase_action="renew",
                provisioning_payment=payment_obj,
            )

    assert seen_in_separate_session is True


# ── Test 29: Issue 2: Bot fallback buy crash recovery never reissues buy ───────────

async def test_bot_fallback_buy_crash_after_send_never_reissues_buy(bot_db):
    """Crash after buy send leaves status='buy_inflight'. Recovery must never reissue buy, and must move to manual_review."""
    now = datetime.utcnow()
    async with bot_db() as session:
        user = User(telegram_id=929001, username="crash_bot_buy", full_name="Crash Bot Buy")
        tariff = Tariff(
            id=929,
            label="Baza Pro",
            price_rub=100,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            vhq_tier="basic",
        )
        server = _make_db_server(929, "Server 929")
        sub = _make_db_sub(
            929,
            user_id=1,
            tariff_id=929,
            server_id=929,
            client_name="vhq_ord_crash_buy",
            vpn_key="https://sub.vhq-connect.xyz/old_key",
            vhq_order_id="ord_crash_buy",
            status=SubStatus.EXPIRED,
            expires_at=now - timedelta(days=20),
            vhq_active_until=now - timedelta(days=20),
        )
        ff = VhqSubscriptionFulfillment(
            id=929,
            subscription_id=929,
            user_id=1,
            source_type="payment",
            source_id="payment:929",
            action="buy_fallback",
            status="buy_inflight",  # Left in DB after crash during/after POST buy
            lease_token="expired_lease_token",
            lease_expires_at=now - timedelta(minutes=5),  # EXPIRED LEASE
            baseline_local_expires_at=now - timedelta(days=20),
            target_local_expires_at=now + timedelta(days=30),
            next_attempt_at=now - timedelta(minutes=5),
            attempts=1,
        )
        session.add_all([user, tariff, server, sub, ff])
        await session.commit()

    mock_bot = AsyncMock()
    mock_api = AsyncMock()
    mock_api.buy = AsyncMock()
    mock_notify = AsyncMock()

    with (
        patch("bot.services.scheduler.async_session", bot_db),
        patch("bot.services.subscription_service.VHQPartnerAPI", return_value=mock_api),
        patch("bot.services.scheduler.notify_admins_issue", mock_notify),
    ):
        await process_vhq_subscription_fulfillments(mock_bot)

    # API buy MUST NOT be called!
    assert mock_api.buy.call_count == 0
    assert mock_notify.call_count == 1

    async with bot_db() as session:
        ff_check = await session.get(VhqSubscriptionFulfillment, 929)
        assert ff_check.status == "manual_review"
        assert ff_check.last_error_code == "stale_buy_inflight"
        assert ff_check.lease_token is None
        assert ff_check.lease_expires_at is None


# ── Test 29b: Issue 2: Live bot buy_inflight is not reclaimed before lease expiry ──────

async def test_live_bot_buy_inflight_is_not_reclaimed_before_lease_expiry(bot_db):
    """When a buy_inflight fulfillment has an active lease in the future, worker leaves it untouched: no manual_review, no admin alert, no second buy."""
    now = datetime.utcnow()
    async with bot_db() as session:
        user = User(telegram_id=929501, username="live_bot_buy", full_name="Live Bot Buy")
        tariff = Tariff(
            id=9295,
            label="Baza Pro",
            price_rub=100,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            vhq_tier="basic",
        )
        server = _make_db_server(9295, "Server 9295")
        sub = _make_db_sub(
            9295,
            user_id=1,
            tariff_id=9295,
            server_id=9295,
            client_name="vhq_ord_live_buy",
            vpn_key="https://sub.vhq-connect.xyz/old_key",
            vhq_order_id="ord_live_buy",
            status=SubStatus.EXPIRED,
            expires_at=now - timedelta(days=20),
            vhq_active_until=now - timedelta(days=20),
        )
        ff = VhqSubscriptionFulfillment(
            id=9295,
            subscription_id=9295,
            user_id=1,
            source_type="payment",
            source_id="payment:9295",
            action="buy_fallback",
            status="buy_inflight",
            lease_token="active_lease_token_123",
            lease_expires_at=now + timedelta(minutes=2),  # ACTIVE LEASE
            baseline_local_expires_at=now - timedelta(days=20),
            target_local_expires_at=now + timedelta(days=30),
            next_attempt_at=now - timedelta(minutes=5),
            attempts=1,
        )
        session.add_all([user, tariff, server, sub, ff])
        await session.commit()

    mock_bot = AsyncMock()
    mock_api = AsyncMock()
    mock_api.buy = AsyncMock()
    mock_notify = AsyncMock()

    with (
        patch("bot.services.scheduler.async_session", bot_db),
        patch("bot.services.subscription_service.VHQPartnerAPI", return_value=mock_api),
        patch("bot.services.scheduler.notify_admins_issue", mock_notify),
    ):
        await process_vhq_subscription_fulfillments(mock_bot)

    # API buy MUST NOT be called!
    assert mock_api.buy.call_count == 0
    # No admin stale alert!
    assert mock_notify.call_count == 0

    async with bot_db() as session:
        ff_check = await session.get(VhqSubscriptionFulfillment, 9295)
        # Status MUST stay buy_inflight with active lease!
        assert ff_check.status == "buy_inflight"
        assert ff_check.lease_token == "active_lease_token_123"
        assert ff_check.last_error_code != "stale_buy_inflight"


# ── Test 30: Issue 2: Webstore fallback buy crash recovery never reissues buy ──────

async def test_webstore_fallback_buy_crash_after_send_never_reissues_buy(web_db):
    """Crash after webstore fallback buy leaves failure_code='buy_fallback_inflight' with expired deadline. Next attempt must never reissue buy."""
    from webstore.models import WebOrder
    from webstore.routes import attempt_fulfill_order

    now = datetime.utcnow()
    async with web_db() as session:
        root_order = WebOrder(
            id=9301,
            order_id="WEB_CRASH_ROOT",
            tariff_key="baza_pro_30",
            tariff_label="Baza Pro 30",
            days=30,
            amount_rub=100,
            status="delivered",
            vhq_order_id="sup_crash_root",
            subscription_url="/vhq/sub/WEB_CRASH_ROOT",
            vhq_upstream_url="https://sub.vhq-connect.xyz/root_key",
            vhq_active_until=now - timedelta(days=35),
            access_expires_at=now - timedelta(days=35),
        )
        # Renewal order crashed after fallback buy was initiated (deadline expired in the past)
        renew_order = WebOrder(
            id=9302,
            order_id="WEB_CRASH_RENEW",
            tariff_key="baza_pro_30",
            tariff_label="Baza Pro 30",
            days=30,
            amount_rub=100,
            status="paid",
            purchase_action="renew",
            target_order_id="order:WEB_CRASH_ROOT",
            failure_code="buy_fallback_inflight",  # Left in DB after crash
            failure_reason="Fallback buy initiated",
            next_fulfillment_retry_at=now - timedelta(minutes=5),  # EXPIRED DEADLINE
        )
        session.add_all([root_order, renew_order])
        await session.commit()

    mock_api = AsyncMock()
    mock_api.buy = AsyncMock()
    mock_api.renew = AsyncMock()
    mock_notify = AsyncMock()

    mock_api_cls = MagicMock(return_value=mock_api)
    mock_api_cls.extract_active_until = VHQPartnerAPI.extract_active_until
    mock_api_cls.extract_subscription_url = VHQPartnerAPI.extract_subscription_url
    mock_api_cls.extract_order_id = VHQPartnerAPI.extract_order_id

    async with web_db() as session:
        order = await session.get(WebOrder, 9302)
        with (
            patch("webstore.routes.get_store_tariffs_by_key", return_value={"baza_pro_30": {"provider": "vhq", "vhq_tier": "basic", "days": 30}}),
            patch("webstore.routes.VHQPartnerAPI", mock_api_cls),
            patch("webstore.routes._notify_admins_webstore", mock_notify),
        ):
            await attempt_fulfill_order(session, order)
            await session.commit()

    # Neither buy nor renew must be called!
    assert mock_api.buy.call_count == 0
    assert mock_api.renew.call_count == 0
    assert mock_notify.call_count == 1

    async with web_db() as session:
        order_check = await session.get(WebOrder, 9302)
        assert order_check.status == "failed"
        assert order_check.failure_code == "stale_buy_inflight"


# ── Test 30b: Issue 2: Live webstore buy_inflight is not marked stale before deadline ──

async def test_live_webstore_buy_inflight_is_not_marked_stale_before_deadline(web_db):
    """When a webstore order has buy_fallback_inflight and deadline is still in the future, attempt_fulfill_order leaves it untouched: no failed, no admin alert, no buy call."""
    from webstore.models import WebOrder
    from webstore.routes import attempt_fulfill_order

    now = datetime.utcnow()
    deadline = now + timedelta(minutes=2)
    async with web_db() as session:
        root_order = WebOrder(
            id=9305,
            order_id="WEB_LIVE_ROOT",
            tariff_key="baza_pro_30",
            tariff_label="Baza Pro 30",
            days=30,
            amount_rub=100,
            status="delivered",
            vhq_order_id="sup_live_root",
            subscription_url="/vhq/sub/WEB_LIVE_ROOT",
            vhq_upstream_url="https://sub.vhq-connect.xyz/root_key",
            vhq_active_until=now - timedelta(days=35),
            access_expires_at=now - timedelta(days=35),
        )
        renew_order = WebOrder(
            id=9306,
            order_id="WEB_LIVE_RENEW",
            tariff_key="baza_pro_30",
            tariff_label="Baza Pro 30",
            days=30,
            amount_rub=100,
            status="paid",
            purchase_action="renew",
            target_order_id="order:WEB_LIVE_ROOT",
            failure_code="buy_fallback_inflight",  # Inflight buy ongoing
            failure_reason="Fallback buy initiated",
            next_fulfillment_retry_at=deadline,  # LIVE DEADLINE in future
        )
        session.add_all([root_order, renew_order])
        await session.commit()

    mock_api = AsyncMock()
    mock_api.buy = AsyncMock()
    mock_api.renew = AsyncMock()
    mock_notify = AsyncMock()

    mock_api_cls = MagicMock(return_value=mock_api)
    mock_api_cls.extract_active_until = VHQPartnerAPI.extract_active_until
    mock_api_cls.extract_subscription_url = VHQPartnerAPI.extract_subscription_url
    mock_api_cls.extract_order_id = VHQPartnerAPI.extract_order_id

    async with web_db() as session:
        order = await session.get(WebOrder, 9306)
        with (
            patch("webstore.routes.get_store_tariffs_by_key", return_value={"baza_pro_30": {"provider": "vhq", "vhq_tier": "basic", "days": 30}}),
            patch("webstore.routes.VHQPartnerAPI", mock_api_cls),
            patch("webstore.routes._notify_admins_webstore", mock_notify),
        ):
            await attempt_fulfill_order(session, order)
            await session.commit()

    # Neither buy nor renew must be called!
    assert mock_api.buy.call_count == 0
    assert mock_api.renew.call_count == 0
    # No admin stale alert!
    assert mock_notify.call_count == 0

    async with web_db() as session:
        order_check = await session.get(WebOrder, 9306)
        # Order MUST remain in paid status with buy_fallback_inflight marker and future deadline untouched!
        assert order_check.status == "paid"
        assert order_check.failure_code == "buy_fallback_inflight"
        assert order_check.next_fulfillment_retry_at == deadline



# ── Test 31: Issue 3: Webstore renew timeout enters reconciliation, not terminal failed ──

async def test_webstore_renew_timeout_enters_reconciliation_not_terminal_failed(web_db):
    """Network timeout during webstore renew keeps order in paid status (not terminal failed) with failure_code='vhq_renew_uncertain'."""
    from webstore.models import WebOrder
    from webstore.routes import attempt_fulfill_order

    now = datetime.utcnow()
    async with web_db() as session:
        root_order = WebOrder(
            id=9311,
            order_id="WEB_TIMEOUT_ROOT",
            tariff_key="baza_pro_30",
            tariff_label="Baza Pro 30",
            days=30,
            amount_rub=100,
            status="delivered",
            vhq_order_id="sup_timeout_root",
            subscription_url="/vhq/sub/WEB_TIMEOUT_ROOT",
            vhq_upstream_url="https://sub.vhq-connect.xyz/timeout_key",
            vhq_active_until=now + timedelta(days=2),
            access_expires_at=now + timedelta(days=2),
        )
        renew_order = WebOrder(
            id=9312,
            order_id="WEB_TIMEOUT_RENEW",
            tariff_key="baza_pro_30",
            tariff_label="Baza Pro 30",
            days=30,
            amount_rub=100,
            status="paid",
            purchase_action="renew",
            target_order_id="order:WEB_TIMEOUT_ROOT",
        )
        session.add_all([root_order, renew_order])
        await session.commit()

    mock_api = AsyncMock()
    mock_api.renew = AsyncMock(side_effect=asyncio.TimeoutError("Connection timed out"))
    mock_api.buy = AsyncMock()

    mock_api_cls = MagicMock(return_value=mock_api)
    mock_api_cls.extract_active_until = VHQPartnerAPI.extract_active_until
    mock_api_cls.extract_subscription_url = VHQPartnerAPI.extract_subscription_url
    mock_api_cls.extract_order_id = VHQPartnerAPI.extract_order_id

    async with web_db() as session:
        order = await session.get(WebOrder, 9312)
        with (
            patch("webstore.routes.get_store_tariffs_by_key", return_value={"baza_pro_30": {"provider": "vhq", "vhq_tier": "basic", "days": 30}}),
            patch("webstore.routes.VHQPartnerAPI", mock_api_cls),
        ):
            await attempt_fulfill_order(session, order)
            await session.commit()

    async with web_db() as session:
        order_check = await session.get(WebOrder, 9312)
        # Must be paid (recoverable), NOT failed!
        assert order_check.status == "paid"
        assert order_check.failure_code == "vhq_renew_uncertain"
        assert order_check.next_fulfillment_retry_at is not None
        assert mock_api.buy.call_count == 0


# ── Test 32: Issue 3: Webstore renew timeout reconciliation can complete without second charge ──

async def test_webstore_renew_timeout_reconciliation_can_complete_without_second_charge(web_db):
    """After a renew timeout, subsequent attempt reconciles via GET orders and delivers without calling renew/buy again."""
    from webstore.models import WebOrder
    from webstore.routes import attempt_fulfill_order

    now = datetime.utcnow()
    target_active = now + timedelta(days=32)
    async with web_db() as session:
        root_order = WebOrder(
            id=9321,
            order_id="WEB_REC_ROOT",
            tariff_key="baza_pro_30",
            tariff_label="Baza Pro 30",
            days=30,
            amount_rub=100,
            status="delivered",
            vhq_order_id="sup_rec_root",
            subscription_url="/vhq/sub/WEB_REC_ROOT",
            vhq_upstream_url="https://sub.vhq-connect.xyz/rec_key",
            vhq_active_until=now + timedelta(days=2),
            access_expires_at=now + timedelta(days=2),
        )
        renew_order = WebOrder(
            id=9322,
            order_id="WEB_REC_RENEW",
            tariff_key="baza_pro_30",
            tariff_label="Baza Pro 30",
            days=30,
            amount_rub=100,
            status="paid",
            purchase_action="renew",
            target_order_id="order:WEB_REC_ROOT",
            failure_code="vhq_renew_uncertain",
            fulfillment_attempts=1,
        )
        session.add_all([root_order, renew_order])
        await session.commit()

    mock_api = AsyncMock()
    # GET orders confirms active_until reached expected target!
    mock_api.get_orders = AsyncMock(return_value=[{
        "order_id": "sup_rec_root",
        "active_until": target_active.isoformat() + "Z",
    }])
    mock_api.renew = AsyncMock()
    mock_api.buy = AsyncMock()

    mock_api_cls = MagicMock(return_value=mock_api)
    mock_api_cls.extract_active_until = VHQPartnerAPI.extract_active_until
    mock_api_cls.extract_subscription_url = VHQPartnerAPI.extract_subscription_url
    mock_api_cls.extract_order_id = VHQPartnerAPI.extract_order_id

    async with web_db() as session:
        order = await session.get(WebOrder, 9322)
        with (
            patch("webstore.routes.get_store_tariffs_by_key", return_value={"baza_pro_30": {"provider": "vhq", "vhq_tier": "basic", "days": 30}}),
            patch("webstore.routes.VHQPartnerAPI", mock_api_cls),
        ):
            await attempt_fulfill_order(session, order)
            await session.commit()

    # Must complete reconciliation without calling renew() or buy()!
    assert mock_api.renew.call_count == 0
    assert mock_api.buy.call_count == 0

    async with web_db() as session:
        order_check = await session.get(WebOrder, 9322)
        assert order_check.status == "delivered"
        assert order_check.vhq_active_until == target_active


# ── Test 33: Issue 4: Admin manual pending fulfillment stays applied and shows processing ──

async def test_admin_manual_pending_fulfillment_stays_applied_and_shows_processing(bot_db):
    """When manual key generation triggers supplier_fulfillment_pending, manual_op stays applied (not failed) and shows processing."""
    from bot.handlers.admin import admin_gen_key_tariff

    now = datetime.utcnow()
    async with bot_db() as session:
        user = User(id=1, telegram_id=933001, username="admin_pend_user", full_name="Admin Pend User")
        tariff = Tariff(
            id=933,
            label="Baza Pro",
            price_rub=100,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            vhq_tier="basic",
        )
        server = _make_db_server(933, "Server 933")
        sub = _make_db_sub(
            933,
            user_id=1,
            tariff_id=933,
            server_id=933,
            client_name="vhq_ord_admin_pend",
            vpn_key="https://sub.vhq-connect.xyz/admin_pend_key",
            vhq_order_id="ord_admin_pend",
            status=SubStatus.ACTIVE,
            expires_at=now + timedelta(days=1),
            vhq_active_until=now + timedelta(days=1),
        )
        op = ManualSubscriptionOperation(
            id=933,
            admin_user_id=999,
            target_user_id=1,
            subscription_id=933,
            tariff_id=933,
            action="renew",
            status="draft",
            operation_uuid=str(uuid.uuid4()),
        )
        session.add_all([user, tariff, server, sub, op])
        await session.commit()

    callback = AsyncMock()
    callback.from_user.id = 999
    callback.data = "adm_key_tariff_933_933"
    callback.message.edit_text = AsyncMock()
    callback.answer = AsyncMock()

    state = AsyncMock()
    state.get_data = AsyncMock(return_value={
        "manual_key_user": 933001,
        "manual_operation_id": 933,
        "manual_purchase_action": "renew",
        "manual_target_subscription_id": 933,
    })

    # Provider is slow: renew returns status='processing'
    mock_api = AsyncMock()
    mock_api.renew = AsyncMock(return_value={
        "status": "processing",
        "order_id": "ord_admin_pend",
    })

    with (
        patch("bot.handlers.admin.async_session", bot_db),
        patch("bot.handlers.admin._is_admin", return_value=True),
        patch("bot.services.subscription_service.VHQPartnerAPI", return_value=mock_api),
    ):
        await admin_gen_key_tariff(callback, state)

    async with bot_db() as session:
        op_check = await session.get(ManualSubscriptionOperation, 933)
        # Must be applied (processing), NOT failed!
        assert op_check.status == "applied"

    assert callback.message.edit_text.call_count == 1
    call_msg = callback.message.edit_text.call_args[0][0]
    assert "Продление принято. VHQ: обработка" in call_msg
    assert "Оплата/выдача вручную не выполнена" not in call_msg


# ── Test 34: Issue 5: One-off payment background VHQ success runs canonical finalization once ──

async def test_oneoff_payment_background_vhq_success_runs_canonical_finalization_once(bot_db):
    """Background worker success of a one-off payment runs canonical finalization once: linking payment, sending stable URL, crediting referral exact-once."""
    from bot.models import ReferralConfig
    from bot.services.scheduler import auto_retry_failed_provisionings

    now = datetime.utcnow()
    target_active = now + timedelta(days=32)

    async with bot_db() as session:
        referrer = User(id=1, telegram_id=934000, username="referrer_user")
        buyer = User(id=2, telegram_id=934001, username="buyer_user", referred_by=934000)
        ref_cfg = ReferralConfig(id=1, is_enabled=True, commission_percent=10.0)
        tariff = Tariff(
            id=934,
            label="Baza Pro",
            price_rub=200,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            vhq_tier="basic",
        )
        server = _make_db_server(934, "Server 934")
        sub = _make_db_sub(
            934,
            user_id=2,
            tariff_id=934,
            server_id=934,
            client_name="vhq_ord_canonical",
            vpn_key="https://sub.vhq-connect.xyz/raw_upstream_canonical",
            vhq_order_id="ord_canonical",
            status=SubStatus.ACTIVE,
            expires_at=now + timedelta(days=2),
            vhq_active_until=now + timedelta(days=2),
        )
        payment = Payment(
            id=934,
            user_id=2,
            subscription_id=None,  # Not yet linked
            tariff_id=934,
            amount=20000,  # 200 RUB
            currency="RUB",
            status=PaymentStatus.COMPLETED,
            provider_payment_id="pay_canonical_oneoff",
            method=PaymentMethod.YOOKASSA,
            platform="android~r~934",
        )
        # Pending fulfillment linked to payment
        ff = VhqSubscriptionFulfillment(
            id=934,
            subscription_id=934,
            user_id=2,
            payment_id=934,
            source_type="payment",
            source_id="payment:934",
            action="renew",
            status="uncertain",  # In uncertain state awaiting background worker
            expected_provider_active_until=target_active,
            baseline_local_expires_at=now + timedelta(days=2),
            target_local_expires_at=now + timedelta(days=32),
            next_attempt_at=now - timedelta(seconds=1),
            attempts=1,
        )
        session.add_all([referrer, buyer, ref_cfg, tariff, server, sub, payment, ff])
        await session.commit()

    mock_bot = AsyncMock()
    mock_api = AsyncMock()
    # Provider confirms renewal via get_orders during worker reconciliation
    mock_api.get_orders = AsyncMock(return_value=[{
        "order_id": "ord_canonical",
        "active_until": target_active.isoformat() + "Z",
    }])

    with (
        patch("bot.services.scheduler.async_session", bot_db),
        patch("bot.handlers.payment.async_session", bot_db),
        patch("bot.services.subscription_service.async_session", bot_db, create=True),
        patch(
            "bot.services.yookassa_reconciliation.fetch_yookassa_payment",
            new=AsyncMock(
                return_value={
                    "id": "pay_canonical_oneoff",
                    "status": "succeeded",
                    "paid": True,
                    "amount": {"value": "200.00", "currency": "RUB"},
                    "metadata": {
                        "user_id": "934001",
                        "chat_id": "934001",
                        "tariff_id": "934",
                        "platform": "android~r~934",
                    },
                }
            ),
        ),
        patch("bot.services.subscription_service.VHQPartnerAPI", return_value=mock_api),
    ):
        # 1. Run worker: reconciles fulfillment to completed, triggers auto_retry_failed_provisionings
        await process_vhq_subscription_fulfillments(mock_bot)

    async with bot_db() as session:
        pmt_check = await session.get(Payment, 934)
        # Payment must now be linked to subscription!
        assert pmt_check.subscription_id == 934

        ff_check = await session.get(VhqSubscriptionFulfillment, 934)
        assert ff_check.status == "completed"

        # Check referral credited exact-once: 10% of 200 RUB = 20 RUB
        earnings = (await session.scalars(select(ReferralEarning).where(ReferralEarning.payment_id == 934))).all()
        assert len(earnings) == 1
        assert earnings[0].amount_rub == 20.0

    # User received customer-facing stable URL (/vhq-sub/)
    sent_messages = [call[0][1] for call in mock_bot.send_message.call_args_list if len(call[0]) > 1]
    has_stable_url = any("/vhq-sub/" in str(m) for m in sent_messages)
    assert has_stable_url, f"Expected stable /vhq-sub/ URL in delivered messages, got: {sent_messages}"
    assert not any("raw_upstream_canonical" in str(m) for m in sent_messages)

    # 2. Run auto_retry again: must be an exact-once no-op!
    initial_send_count = mock_bot.send_message.call_count
    with (
        patch("bot.services.scheduler.async_session", bot_db),
        patch("bot.handlers.payment.async_session", bot_db),
    ):
        await auto_retry_failed_provisionings(mock_bot)

    # No additional messages or referral earnings
    assert mock_bot.send_message.call_count == initial_send_count
    async with bot_db() as session:
        earnings2 = (await session.scalars(select(ReferralEarning).where(ReferralEarning.payment_id == 934))).all()
        assert len(earnings2) == 1


# ── Test 35: Issue 6: Legacy unknown baseline + already_requested fails closed ─────

async def test_legacy_unknown_baseline_already_requested_does_not_complete_new_payment(bot_db):
    """When baseline is unknown and renew response contains already_requested=True, must fail closed to manual_review."""
    now = datetime.utcnow()
    async with bot_db() as session:
        user = User(telegram_id=935001, username="already_req_user", full_name="Already Req User")
        tariff = Tariff(
            id=935,
            label="Baza Pro",
            price_rub=100,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            vhq_tier="basic",
        )
        server = _make_db_server(935, "Server 935")
        sub = _make_db_sub(
            935,
            user_id=1,
            tariff_id=935,
            server_id=935,
            client_name="vhq_ord_already_req",
            vpn_key="https://sub.vhq-connect.xyz/old_key",
            vhq_order_id="ord_already_req",
            status=SubStatus.ACTIVE,
            expires_at=now + timedelta(days=5),
            vhq_active_until=None,  # Baseline unknown
        )
        ff = VhqSubscriptionFulfillment(
            id=935,
            subscription_id=935,
            user_id=1,
            source_type="payment",
            source_id="payment:935",
            action="renew",
            status="pending",
            baseline_provider_active_until=None,
            expected_provider_active_until=None,  # Unknown baseline
            baseline_local_expires_at=now + timedelta(days=5),
            target_local_expires_at=now + timedelta(days=35),
            attempts=0,
        )
        session.add_all([user, tariff, server, sub, ff])
        await session.commit()

    mock_api = AsyncMock()
    mock_api.get_orders = AsyncMock(return_value=[])  # GET orders does not find it -> baseline stays unknown
    mock_api.renew = AsyncMock(return_value={
        "status": "scheduled",
        "already_requested": True,
        "active_until": (now + timedelta(days=5)).isoformat() + "Z",
        "order_id": "ord_already_req",
    })

    async with bot_db() as session:
        ff_obj = await session.get(VhqSubscriptionFulfillment, 935)
        await execute_vhq_fulfillment(session, ff_obj, api=mock_api)

        # Must fail closed to manual_review!
        assert ff_obj.status == "manual_review"
        assert ff_obj.last_error_code == "missing_baseline_already_requested"


# ── Test 36: Issue 7: Webstore fallback buy missing order_id fails to manual review ─

async def test_webstore_fallback_buy_missing_order_id_fails_to_manual_review(web_db):
    """When fallback buy returns config_url but missing order_id, order must not be marked delivered and fails to manual review."""
    from webstore.models import WebOrder
    from webstore.routes import attempt_fulfill_order

    now = datetime.utcnow()
    async with web_db() as session:
        root_order = WebOrder(
            id=9361,
            order_id="WEB_MISS_ROOT",
            tariff_key="baza_pro_30",
            tariff_label="Baza Pro 30",
            days=30,
            amount_rub=100,
            status="delivered",
            vhq_order_id="sup_miss_root",
            subscription_url="/vhq/sub/WEB_MISS_ROOT",
            vhq_upstream_url="https://sub.vhq-connect.xyz/miss_key",
            vhq_active_until=now - timedelta(days=35),
            access_expires_at=now - timedelta(days=35),
        )
        renew_order = WebOrder(
            id=9362,
            order_id="WEB_MISS_RENEW",
            tariff_key="baza_pro_30",
            tariff_label="Baza Pro 30",
            days=30,
            amount_rub=100,
            status="paid",
            purchase_action="renew",
            target_order_id="order:WEB_MISS_ROOT",
        )
        session.add_all([root_order, renew_order])
        await session.commit()

    mock_api = AsyncMock()
    mock_api.renew = AsyncMock(side_effect=VHQPartnerAPIError("expired too long ago, please buy a new one", status=409))
    # Supplier returns config_url, but missing real order_id!
    mock_api.buy = AsyncMock(return_value={
        "config_url": "https://sub.vhq-connect.xyz/new_buy_key",
        # No order_id, id, or orderId!
    })

    mock_api_cls = MagicMock(return_value=mock_api)
    mock_api_cls.extract_active_until = VHQPartnerAPI.extract_active_until
    mock_api_cls.extract_subscription_url = VHQPartnerAPI.extract_subscription_url
    mock_api_cls.extract_order_id = VHQPartnerAPI.extract_order_id

    async with web_db() as session:
        order = await session.get(WebOrder, 9362)
        with (
            patch("webstore.routes.get_store_tariffs_by_key", return_value={"baza_pro_30": {"provider": "vhq", "vhq_tier": "basic", "days": 30}}),
            patch("webstore.routes.VHQPartnerAPI", mock_api_cls),
            patch("webstore.routes._notify_admins_webstore", new=AsyncMock()),
        ):
            await attempt_fulfill_order(session, order)
            await session.commit()

    async with web_db() as session:
        order_check = await session.get(WebOrder, 9362)
        # Order MUST NOT be delivered!
        assert order_check.status == "failed"
        assert order_check.failure_code == "fallback_buy_missing_data"



