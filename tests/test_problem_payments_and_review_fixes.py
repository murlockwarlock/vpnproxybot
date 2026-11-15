"""Comprehensive tests covering PR #22 review findings and Problem Payments feature.

Covers:
- Tier-2 pending audit reachability and BalanceTopUp audit (independent of completed unlinked query).
- Fail-closed device slot delivery without durable Payment record (preventing duplicate slots on replay).
- Lease expiry while Adapt supplier mutation is in-flight (fencing + uncertain operation recovery).
- Lease heartbeat active renewal during execution.
- Duplicate referral bonus days and financial side effects prevention (ReferralPaymentLog payment_id, unique constraints).
- Strict provider metadata verification (currency missing, exact kopecks, required fields).
- Notification timing: customer and admin notifications dispatched only post-commit (no duplicate admin alerts).
- Problem payments classification into all 5 categories.
- Legitimate asynchronous VHQ provisioning exclusion vs stalled/failed escalation.
- Admin problem payments dashboard authorization, pagination, and safe actions (inspect, refresh, reconcile, user).
- Non-spamming admin alerts with deduplication and sensible reminders.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.config import settings
from bot.models import (
    AdminActionLog,
    Base,
    BalanceTopUp,
    BalanceTransaction,
    BalanceTransactionKind,
    BotSettings,
    ManualSubscriptionOperation,
    Partner,
    PartnerEarning,
    Payment,
    PaymentMethod,
    PaymentNotificationOutbox,
    PaymentStatus,
    Platform,
    ReferralConfig,
    ReferralEarning,
    ReferralPaymentLog,
    Server,
    Subscription,
    SubStatus,
    Tariff,
    TariffType,
    User,
    VhqSubscriptionFulfillment,
)
from bot.services.cluster import AppLease, LeaseHeartbeat, LeaseManager
from bot.services.payment_service import (
    credit_cashback,
    credit_partner,
    credit_referral,
    log_referral_payment,
)
from bot.services.problem_payments import (
    ProblemCategory,
    alert_admins_if_problem_payments_detected,
    build_problem_payments_report,
    classify_and_verify_candidate,
)
from bot.services.scheduler import (
    check_overdue_pending_payments,
    check_payment_integrity,
)
from bot.services.yookassa_reconciliation import (
    verify_and_fulfill_yookassa_payment,
)
from bot.webhooks import _process_device_deliver

pytestmark = pytest.mark.asyncio


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


@pytest_asyncio.fixture(autouse=True)
def _patch_async_sessions(monkeypatch, db_session_factory):
    monkeypatch.setattr("bot.database.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.cluster.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.scheduler.async_session", db_session_factory)
    monkeypatch.setattr("bot.handlers.admin.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.admin_audit.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.guide_service.async_session", db_session_factory)
    monkeypatch.setattr("bot.handlers.payment.async_session", db_session_factory)


# ── 1. Tier-2 Audit Reachability & Overdue BalanceTopUp ─────────────────────


async def test_tier2_audit_is_reachable_when_completed_unlinked_is_empty(monkeypatch, db_session_factory):
    """Tier-2 pending audit must execute even when completed unlinked payments query has no rows."""
    monkeypatch.setattr("bot.services.scheduler.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)

    # Database has NO completed unlinked payments, but HAS an overdue pending payment and topup
    now = datetime.utcnow()
    async with db_session_factory() as session:
        user = User(telegram_id=900001, username="tier2user")
        session.add(user)
        await session.flush()

        payment = Payment(
            user_id=user.id,
            amount=50000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_overdue_p1",
            created_at=now - timedelta(hours=36),
        )
        topup = BalanceTopUp(
            telegram_id=user.telegram_id,
            amount_rub=250.0,
            provider="yookassa",
            status="pending",
            provider_payment_id="yk_overdue_t1",
            created_at=now - timedelta(hours=48),
        )
        session.add_all([payment, topup])
        await session.commit()

    # Mock YooKassa returning succeeded for the overdue payment
    def mock_fetch(pid):
        if pid == "yk_overdue_p1":
            return {"status": "succeeded", "paid": True, "amount": {"value": "500.00", "currency": "RUB"}}
        if pid == "yk_overdue_t1":
            return {"status": "succeeded", "paid": True, "amount": {"value": "250.00", "currency": "RUB"}}
        return None

    monkeypatch.setattr("bot.services.problem_payments.fetch_yookassa_payment", AsyncMock(side_effect=mock_fetch))

    alert_mock = AsyncMock()
    monkeypatch.setattr("bot.services.problem_payments.notify_admins_issue", alert_mock)

    bot_mock = SimpleNamespace(send_message=AsyncMock())

    # Running check_payment_integrity (which returns early because 0 completed unlinked rows exist)
    await check_payment_integrity(bot_mock)

    # Now running the separate, always-runnable overdue audit:
    await check_overdue_pending_payments(bot_mock)

    # Alert MUST have been called for both the overdue payment and overdue topup!
    assert alert_mock.await_count == 1
    call_kwargs = alert_mock.call_args.kwargs
    assert "ПРОБЛЕМНЫЕ ОПЛАТЫ" in call_kwargs["title"]
    details_str = " ".join(call_kwargs["details"])
    assert "payment #1" in details_str
    assert "topup #1" in details_str


async def test_problem_alert_delivery_failure_remains_retryable(monkeypatch, db_session_factory):
    """An incident is tracked only after at least one staff delivery succeeds."""
    monkeypatch.setattr(settings, "admin_ids", [990001])
    monkeypatch.setattr(settings, "owner_ids", [])
    monkeypatch.setattr(settings, "support_agent_ids", [])
    monkeypatch.setattr("bot.services.notifications.asyncio.sleep", AsyncMock())

    async with db_session_factory() as session:
        user = User(telegram_id=990002, username="alert_user")
        session.add(user)
        await session.flush()
        payment = Payment(
            user_id=user.id,
            amount=10000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_alert_delivery_1",
            provider_status="succeeded",
            provider_paid=True,
            provider_amount_kopecks=10000,
            provider_currency="RUB",
            provider_verified_at=datetime.utcnow(),
            provisioning_failure_code="yookassa_amount_mismatch",
        )
        session.add(payment)
        await session.commit()
        payment_id = payment.id

    bot_mock = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("telegram unavailable")))
    await alert_admins_if_problem_payments_detected(bot_mock)
    assert bot_mock.send_message.await_count == 3

    async with db_session_factory() as session:
        tracker = await session.get(BotSettings, "problem_payments_alert_tracker")
        assert tracker is not None
        assert json.loads(tracker.value).get("alerted_keys", []) == []

    bot_mock.send_message.reset_mock()
    bot_mock.send_message.side_effect = None
    await alert_admins_if_problem_payments_detected(bot_mock)
    assert bot_mock.send_message.await_count == 1

    async with db_session_factory() as session:
        tracker = await session.get(BotSettings, "problem_payments_alert_tracker")
        assert f"payment:{payment_id}" in json.loads(tracker.value)["alerted_keys"]

    bot_mock.send_message.reset_mock()
    await alert_admins_if_problem_payments_detected(bot_mock)
    bot_mock.send_message.assert_not_awaited()


@pytest.mark.parametrize("outbox_status", ["pending", "sending", "unknown"])
async def test_stale_telegram_outbox_is_alerted_after_active_window(
    monkeypatch, db_session_factory, outbox_status
):
    admin_alert = AsyncMock(return_value=True)
    monkeypatch.setattr("bot.services.problem_payments.notify_admins_issue", admin_alert)
    now = datetime.utcnow()
    old = now - timedelta(days=8)

    async with db_session_factory() as session:
        user = User(telegram_id=990003, username="stale_outbox_user")
        server = Server(name=f"stale-outbox-{outbox_status}", host="10.0.0.3", location="test")
        session.add_all([user, server])
        await session.flush()
        subscription = Subscription(
            user_id=user.id,
            server_id=server.id,
            tariff_months=1,
            tariff_days=30,
            client_name=f"stale_{outbox_status}",
            platform=Platform.ANDROID,
            vpn_key="https://darimiru.ru/sub/stale-outbox",
            status=SubStatus.ACTIVE,
            expires_at=now + timedelta(days=22),
        )
        session.add(subscription)
        await session.flush()
        payment = Payment(
            user_id=user.id,
            amount=25000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            subscription_id=subscription.id,
            provider_payment_id=f"yk_stale_outbox_{outbox_status}",
            created_at=old,
        )
        session.add(payment)
        await session.flush()
        session.add(
            PaymentNotificationOutbox(
                payment_id=payment.id,
                kind="payment_received",
                recipient_telegram_id=user.telegram_id,
                payload_json='[{"text":"accepted"}]',
                status=outbox_status,
                attempts=2,
                next_attempt_at=old,
                last_attempt_at=old,
                lease_expires_at=old if outbox_status == "sending" else None,
                last_error="Telegram delivery outcome unknown after worker restart"
                if outbox_status == "unknown"
                else "TelegramNetworkTimeout",
                created_at=old,
            )
        )
        await session.commit()
        payment_id = payment.id

    from bot.services.problem_payments import _get_active_problem_categories

    category_map = await _get_active_problem_categories(days=7, now=now)
    assert any(item.item_id == payment_id for item in category_map["tg_delivery"])

    bot_mock = SimpleNamespace(send_message=AsyncMock())
    await alert_admins_if_problem_payments_detected(bot_mock)
    await alert_admins_if_problem_payments_detected(bot_mock)

    assert admin_alert.await_count == 1
    details = " ".join(admin_alert.await_args.kwargs["details"])
    assert f"payment #{payment_id}" in details
    assert "telegram_delivery_failed" in details


async def test_paid_unresolved_payment_older_than_seven_days_stays_alertable(
    monkeypatch, db_session_factory
):
    admin_alert = AsyncMock(return_value=True)
    monkeypatch.setattr("bot.services.problem_payments.notify_admins_issue", admin_alert)
    old = datetime.utcnow() - timedelta(days=31)

    async with db_session_factory() as session:
        user = User(telegram_id=990004, username="old_paid_user")
        session.add(user)
        await session.flush()
        payment = Payment(
            user_id=user.id,
            amount=25000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_old_paid_unresolved",
            provider_status="succeeded",
            provider_paid=True,
            provider_amount_kopecks=25000,
            provider_currency="RUB",
            provider_verified_at=old,
            provisioning_failure_code="vhq_balance",
            created_at=old,
        )
        session.add(payment)
        await session.commit()
        payment_id = payment.id

    await alert_admins_if_problem_payments_detected(SimpleNamespace(send_message=AsyncMock()))

    assert admin_alert.await_count == 1
    details = " ".join(admin_alert.await_args.kwargs["details"])
    assert f"payment #{payment_id}" in details
    assert "vhq_balance" in details


# ── 2. Device Slots Fail-Closed Absent Durable Payment Record ────────────────


async def test_device_slots_fail_closed_without_durable_payment_record(monkeypatch, db_session_factory):
    """Replaying or sending an is_device payment webhook without a durable local Payment must NOT grant slots."""
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)

    async with db_session_factory() as session:
        user = User(telegram_id=900002, username="devuser")
        server = Server(id=1, name="srv1", host="1.1.1.1", location="Amsterdam")
        session.add_all([user, server])
        await session.flush()
        sub = Subscription(
            user_id=user.id,
            server_id=server.id,
            tariff_months=1,
            tariff_days=30,
            client_name="devsub_1",
            platform=Platform.ANDROID,
            device_slots=1,
            vpn_key="vless://devkey",
            expires_at=datetime.utcnow() + timedelta(days=30),
            status=SubStatus.ACTIVE,
        )
        session.add(sub)
        await session.commit()
        sub_id = sub.id

    # Simulated verified YooKassa payment with is_device=1, BUT NO local Payment record exists
    verified_yk = {
        "id": "yk_device_orphan_1",
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "150.00", "currency": "RUB"},
        "metadata": {
            "is_device": "1",
            "sub_id": str(sub_id),
            "user_id": "900002",
        },
    }
    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.fetch_yookassa_payment",
        AsyncMock(return_value=verified_yk),
    )

    bot_mock = SimpleNamespace(send_message=AsyncMock())
    success, code = await verify_and_fulfill_yookassa_payment(bot_mock, "yk_device_orphan_1")

    # MUST fail closed!
    assert success is False
    assert code == "missing_local_payment"

    # Verify device_slots did NOT increment
    async with db_session_factory() as session:
        reloaded_sub = await session.get(Subscription, sub_id)
        assert reloaded_sub.device_slots == 1

    # Also test _process_device_deliver directly with no payment record
    app_mock = {"bot": bot_mock}
    user_db_id, delivered_sub_id = await _process_device_deliver(
        app_mock,
        telegram_user_id=900002,
        chat_id=900002,
        sub_id=sub_id,
        payment_id=None,
        provider_payment_id="nonexistent_provider_id",
    )
    assert user_db_id is None
    assert delivered_sub_id is None

    async with db_session_factory() as session:
        reloaded_sub = await session.get(Subscription, sub_id)
        assert reloaded_sub.device_slots == 1


async def test_device_slots_idempotency_prevents_duplicate_on_replay(monkeypatch, db_session_factory):
    """Replaying device payment provider ID multiple times must grant exactly 1 slot."""
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)

    async with db_session_factory() as session:
        user = User(telegram_id=900003, username="replayuser")
        server = Server(id=1, name="srv1", host="1.1.1.1", location="Amsterdam")
        session.add_all([user, server])
        await session.flush()
        sub = Subscription(
            user_id=user.id,
            server_id=server.id,
            tariff_months=1,
            tariff_days=30,
            client_name="replaysub_1",
            platform=Platform.ANDROID,
            device_slots=1,
            vpn_key="vless://replaykey",
            expires_at=datetime.utcnow() + timedelta(days=30),
            status=SubStatus.ACTIVE,
        )
        session.add(sub)
        await session.flush()
        payment = Payment(
            user_id=user.id,
            subscription_id=sub.id,
            amount=15000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_dev_replay_1",
            platform="device",
        )
        session.add(payment)
        await session.commit()
        sub_id = sub.id

    verified_yk = {
        "id": "yk_dev_replay_1",
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "150.00", "currency": "RUB"},
        "metadata": {
            "is_device": "1",
            "sub_id": str(sub_id),
            "user_id": "900003",
        },
    }
    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.fetch_yookassa_payment",
        AsyncMock(return_value=verified_yk),
    )

    bot_mock = SimpleNamespace(send_message=AsyncMock())

    # 1st run: successfully delivers slot
    success1, code1 = await verify_and_fulfill_yookassa_payment(bot_mock, "yk_dev_replay_1")
    assert success1 is True
    assert code1 == "device_slot_completed"

    async with db_session_factory() as session:
        sub_after1 = await session.get(Subscription, sub_id)
        assert sub_after1.device_slots == 2

    # 2nd run (replay): must recognize already_completed and NOT increment again
    success2, code2 = await verify_and_fulfill_yookassa_payment(bot_mock, "yk_dev_replay_1")
    assert success2 is True
    assert code2 == "already_completed"

    async with db_session_factory() as session:
        sub_after2 = await session.get(Subscription, sub_id)
        assert sub_after2.device_slots == 2  # STILL 2!


# ── 3. Lease Expiry While Adapt Call Is In-Flight & Recovery ────────────────


async def test_lease_expiry_while_supplier_mutation_in_flight(monkeypatch, db_session_factory):
    """When a lease expires mid-call while supplier mutation is running, execution must be fenced."""
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.cluster.async_session", db_session_factory)

    async with db_session_factory() as session:
        user = User(telegram_id=900004, username="inflightuser")
        tariff = Tariff(id=80, label="Тариф 80", price_rub=200, price_stars=0, days=30, is_active=True, tariff_type=TariffType.VPN)
        session.add_all([user, tariff])
        await session.flush()
        payment = Payment(
            user_id=user.id,
            tariff_id=80,
            platform=Platform.ANDROID.value,
            amount=20000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_inflight_1",
        )
        session.add(payment)
        await session.commit()
        db_payment_id = payment.id

    verified_payment = {
        "id": "yk_inflight_1",
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "200.00", "currency": "RUB"},
        "metadata": {
            "user_id": "900004",
            "chat_id": "900004",
            "tariff_id": "80",
            "platform": Platform.ANDROID.value,
        },
    }
    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.fetch_yookassa_payment",
        AsyncMock(return_value=verified_payment),
    )

    # Simulate _process_and_deliver taking time and lease expiring mid-call
    original_is_owned = LeaseManager.is_owned_by_me

    call_count = 0
    async def mock_is_owned(self, lease_name):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return True  # Valid before supplier call
        return False     # EXPIRED during/after supplier call!

    monkeypatch.setattr(LeaseManager, "is_owned_by_me", mock_is_owned)

    async def mock_deliver(*args, **kwargs):
        return user.id, 999  # Supplier returned, but our lease expired in flight

    monkeypatch.setattr("bot.webhooks._process_and_deliver", mock_deliver)

    bot_mock = SimpleNamespace(send_message=AsyncMock())
    success, code = await verify_and_fulfill_yookassa_payment(bot_mock, "yk_inflight_1")

    # MUST fail with lease_expired and fence the execution!
    assert success is False
    assert code == "lease_expired"

    async with db_session_factory() as session:
        p = await session.get(Payment, db_payment_id)
        assert p.provisioning_failure_code == "uncertain_supplier_mutation"
        assert p.status == PaymentStatus.PENDING  # Not committed as completed by the expired worker


# ── 4. Lease Heartbeat Active Renewal ───────────────────────────────────────


async def test_lease_heartbeat_actively_renews_lease(db_session_factory):
    """LeaseHeartbeat periodically renews the lease in the database."""
    lm = LeaseManager(owner_id="heartbeat_worker_1", session_factory=db_session_factory)
    lock_name = "test:heartbeat:lock"

    # Acquire initial lease with short ttl of 2 seconds
    acquired = await lm.acquire_or_renew(lock_name, ttl_seconds=2)
    assert acquired is True

    # Start heartbeat renewing every 0.2 seconds
    heartbeat = LeaseHeartbeat(lm, lock_name, ttl_seconds=2, interval_seconds=0.2)
    heartbeat.start()

    # Sleep for 1.0 second (well within ttl, but multiple renewals occur)
    await asyncio.sleep(0.6)
    assert heartbeat.lease_lost is False
    assert await lm.is_owned_by_me(lock_name) is True

    await heartbeat.stop()


# ── 5. Duplicate Referral Bonus Days and Financial Side Effects ─────────────


async def test_referral_payment_log_idempotency_prevents_duplicate_bonus_days(db_session_factory):
    """log_referral_payment must not duplicate logs or grant bonus days twice for the same payment_id."""
    async with db_session_factory() as session:
        referrer = User(telegram_id=900010, username="referrer_boss", bonus_days=0)
        referred = User(telegram_id=900011, username="referred_sub", referred_by=900010)
        config = ReferralConfig(id=1, is_enabled=True, pay_bonus_enabled=True, pay_bonus_days=3, pay_bonus_first_only=True)
        session.add_all([referrer, referred, config])
        await session.flush()
        payment = Payment(
            user_id=referred.id,
            amount=30000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
        )
        session.add(payment)
        await session.commit()
        ref_user_id = referred.id
        referrer_id = referrer.id
        pay_id = payment.id

    bot_mock = SimpleNamespace(send_message=AsyncMock())

    # 1st execution: should award 3 bonus days and add 1 log
    async with db_session_factory() as session:
        await log_referral_payment(session, ref_user_id, 300.0, bot=bot_mock, payment_id=pay_id)
        await session.commit()

    async with db_session_factory() as session:
        ref_db = await session.get(User, referrer_id)
        assert ref_db.bonus_days == 3
        logs = (await session.scalars(select(ReferralPaymentLog).where(ReferralPaymentLog.payment_id == pay_id))).all()
        assert len(logs) == 1

    # 2nd execution for the SAME payment_id: must be a no-op!
    async with db_session_factory() as session:
        await log_referral_payment(session, ref_user_id, 300.0, bot=bot_mock, payment_id=pay_id)
        await session.commit()

    async with db_session_factory() as session:
        ref_db = await session.get(User, referrer_id)
        assert ref_db.bonus_days == 3  # STILL 3 (no duplicate +3 bonus days!)
        logs = (await session.scalars(select(ReferralPaymentLog).where(ReferralPaymentLog.payment_id == pay_id))).all()
        assert len(logs) == 1


async def test_credit_referral_and_partner_database_uniqueness(db_session_factory):
    """Concurrent or repeated calls to credit_referral and credit_partner must be idempotent."""
    async with db_session_factory() as session:
        partner_user = User(telegram_id=900020, username="partner_user")
        session.add(partner_user)
        await session.flush()
        partner = Partner(id=1, name="BigPartner", telegram_id=900020, is_active=True, commission_percent=20.0, partner_balance=0.0)
        referrer = User(telegram_id=900021, username="ref_user", balance_rub=0.0)
        payer = User(telegram_id=900022, username="payer", referred_by=900021, partner_id=1)
        ref_cfg = ReferralConfig(id=1, is_enabled=True, commission_percent=10.0)
        session.add_all([partner, referrer, payer, ref_cfg])
        await session.flush()
        payment = Payment(
            user_id=payer.id,
            amount=50000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
        )
        session.add(payment)
        await session.commit()
        payer_id = payer.id
        pay_id = payment.id

    # Call credit_referral twice
    async with db_session_factory() as session:
        await credit_referral(session, payer_id, pay_id, 500.0)
        await session.commit()

    async with db_session_factory() as session:
        await credit_referral(session, payer_id, pay_id, 500.0)
        await session.commit()

    async with db_session_factory() as session:
        earnings = (await session.scalars(select(ReferralEarning).where(ReferralEarning.payment_id == pay_id))).all()
        assert len(earnings) == 1
        ref_db = await session.scalar(select(User).where(User.telegram_id == 900021))
        assert ref_db.balance_rub == 50.0  # 10% of 500.0, not credited twice!

    # Call credit_partner twice
    async with db_session_factory() as session:
        await credit_partner(session, payer_id, pay_id, 500.0)
        await session.commit()

    async with db_session_factory() as session:
        await credit_partner(session, payer_id, pay_id, 500.0)
        await session.commit()

    async with db_session_factory() as session:
        p_earnings = (await session.scalars(select(PartnerEarning).where(PartnerEarning.payment_id == pay_id))).all()
        assert len(p_earnings) == 1
        p_db = await session.get(Partner, 1)
        assert p_db.partner_balance == 100.0  # 20% of 500.0, not credited twice!


# ── 6. Strict Provider Metadata Verification ───────────────────────────────


async def test_missing_currency_or_non_rub_fails_closed(monkeypatch, db_session_factory):
    """YooKassa payment missing currency or in non-RUB must fail closed immediately."""
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)

    # Missing currency
    yk_no_curr = {
        "id": "yk_nocurr_1",
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "100.00"},  # NO currency field!
        "metadata": {"user_id": "900030", "tariff_id": "1", "platform": "android"},
    }
    monkeypatch.setattr("bot.services.yookassa_reconciliation.fetch_yookassa_payment", AsyncMock(return_value=yk_no_curr))

    bot_mock = SimpleNamespace(send_message=AsyncMock())
    success, code = await verify_and_fulfill_yookassa_payment(bot_mock, "yk_nocurr_1")
    assert success is False
    assert code == "currency_missing"

    # Non-RUB currency (e.g. USD)
    yk_usd = {
        "id": "yk_usd_1",
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "10.00", "currency": "USD"},
        "metadata": {"user_id": "900030", "tariff_id": "1", "platform": "android"},
    }
    monkeypatch.setattr("bot.services.yookassa_reconciliation.fetch_yookassa_payment", AsyncMock(return_value=yk_usd))
    alert_mock = AsyncMock()
    monkeypatch.setattr("bot.services.yookassa_reconciliation.notify_admins_issue", alert_mock)

    success_usd, code_usd = await verify_and_fulfill_yookassa_payment(bot_mock, "yk_usd_1")
    assert success_usd is False
    assert code_usd == "currency_mismatch"
    alert_mock.assert_awaited_once()


async def test_exact_kopecks_comparison_fails_closed(monkeypatch, db_session_factory):
    """Amount comparison must not allow even a 1-kopeck mismatch."""
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)

    async with db_session_factory() as session:
        user = User(telegram_id=900031, username="exactkopuser")
        tariff = Tariff(id=81, label="Тариф 81", price_rub=100, price_stars=0, days=30, is_active=True, tariff_type=TariffType.VPN)
        session.add_all([user, tariff])
        await session.flush()
        payment = Payment(
            user_id=user.id,
            tariff_id=81,
            platform=Platform.ANDROID.value,
            amount=10000,  # 100.00 RUB
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_exact_kop_1",
        )
        session.add(payment)
        await session.commit()

    # Provider reports 99.99 RUB (9999 kopecks, 1 kopeck off)
    yk_mismatch = {
        "id": "yk_exact_kop_1",
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "99.99", "currency": "RUB"},
        "metadata": {"user_id": "900031", "tariff_id": "81", "platform": Platform.ANDROID.value},
    }
    monkeypatch.setattr("bot.services.yookassa_reconciliation.fetch_yookassa_payment", AsyncMock(return_value=yk_mismatch))

    alert_mock = AsyncMock()
    monkeypatch.setattr("bot.services.yookassa_reconciliation.notify_admins_issue", alert_mock)

    bot_mock = SimpleNamespace(send_message=AsyncMock())
    success, code = await verify_and_fulfill_yookassa_payment(bot_mock, "yk_exact_kop_1")

    assert success is False
    assert code == "amount_mismatch"

    async with db_session_factory() as session:
        persisted = await session.scalar(select(Payment).where(Payment.provider_payment_id == "yk_exact_kop_1"))
        assert persisted.provider_status == "succeeded"
        assert persisted.provider_paid is True
        assert persisted.provider_amount_kopecks == 9999
        assert persisted.provisioning_failure_code == "yookassa_amount_mismatch"

    from bot.services.problem_payments import classify_candidate_db_only
    item = await classify_candidate_db_only({"type": "payment", "obj": persisted, "user": None})
    assert item is not None
    assert item.category == ProblemCategory.CONFIRMED_UNFULFILLED


@pytest.mark.parametrize(
    ("case_name", "metadata", "amount", "currency", "expected_code", "expected_result"),
    [
        ("currency", {"user_id": "990101", "tariff_id": "901", "platform": "android"}, "100.00", "USD", "yookassa_currency_mismatch", "currency_mismatch"),
        ("user", {"user_id": "999999", "tariff_id": "901", "platform": "android"}, "100.00", "RUB", "yookassa_user_mismatch", "user_mismatch"),
        ("tariff", {"user_id": "990101", "tariff_id": "902", "platform": "android"}, "100.00", "RUB", "yookassa_tariff_mismatch", "tariff_mismatch"),
        ("intent", {"user_id": "990101", "tariff_id": "901", "platform": "ios"}, "100.00", "RUB", "yookassa_intent_mismatch", "intent_mismatch"),
        ("invalid_metadata", {"user_id": "not-an-int", "tariff_id": "901", "platform": "android"}, "100.00", "RUB", "yookassa_invalid_metadata", "invalid_metadata"),
        ("missing_user", {"tariff_id": "901", "platform": "android"}, "100.00", "RUB", "yookassa_missing_user_metadata", "missing_metadata:user_id"),
        ("missing_tariff", {"user_id": "990101", "platform": "android"}, "100.00", "RUB", "yookassa_missing_tariff_metadata", "missing_metadata:tariff_id"),
    ],
)
async def test_paid_validation_failures_persist_provider_truth(
    monkeypatch,
    db_session_factory,
    case_name,
    metadata,
    amount,
    currency,
    expected_code,
    expected_result,
):
    """Every deterministic paid validation failure remains visible after restart."""
    provider_id = f"yk_validation_{case_name}"
    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.fetch_yookassa_payment",
        AsyncMock(
            return_value={
                "id": provider_id,
                "status": "succeeded",
                "paid": True,
                "amount": {"value": amount, "currency": currency},
                "metadata": metadata,
            }
        ),
    )
    monkeypatch.setattr("bot.services.yookassa_reconciliation.notify_admins_issue", AsyncMock())

    async with db_session_factory() as session:
        user = User(telegram_id=990101, username="validation_user")
        tariff = Tariff(
            id=901,
            label="Validation tariff",
            price_rub=100,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
        )
        session.add_all([user, tariff])
        await session.flush()
        session.add(
            Payment(
                user_id=user.id,
                tariff_id=tariff.id,
                platform="android",
                amount=10000,
                currency="RUB",
                method=PaymentMethod.YOOKASSA,
                status=PaymentStatus.PENDING,
                provider_payment_id=provider_id,
            )
        )
        await session.commit()

    success, result = await verify_and_fulfill_yookassa_payment(
        SimpleNamespace(send_message=AsyncMock()), provider_id, source="validation_test"
    )
    assert success is False
    assert result == expected_result

    async with db_session_factory() as session:
        payment = await session.scalar(select(Payment).where(Payment.provider_payment_id == provider_id))
        assert payment.provider_status == "succeeded"
        assert payment.provider_paid is True
        assert payment.provider_amount_kopecks == 10000
        assert payment.provider_currency == currency
        assert payment.provisioning_failure_code == expected_code


# ── 7. Notifications Timing & No Duplicate Admin Notifications ─────────────


async def test_notifications_sent_post_commit_without_duplicate_admin_alert(monkeypatch, db_session_factory):
    """Customer access and admin notifications are sent only AFTER successful commit; exactly 1 admin payment notification is sent."""
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)

    async with db_session_factory() as session:
        user = User(telegram_id=900040, username="notifyuser", full_name="Notify User")
        tariff = Tariff(id=82, label="Тариф 82", price_rub=250, price_stars=0, days=30, is_active=True, tariff_type=TariffType.VPN)
        server = Server(id=1, name="srv1", host="1.1.1.1", location="Amsterdam")
        session.add_all([user, tariff, server])
        await session.flush()
        payment = Payment(
            user_id=user.id,
            tariff_id=82,
            platform=Platform.ANDROID.value,
            amount=25000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_notify_1",
        )
        session.add(payment)
        await session.commit()
        db_user_id = user.id

    verified_payment = {
        "id": "yk_notify_1",
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "250.00", "currency": "RUB"},
        "metadata": {
            "user_id": "900040",
            "chat_id": "900040",
            "tariff_id": "82",
            "platform": Platform.ANDROID.value,
        },
    }
    monkeypatch.setattr("bot.services.yookassa_reconciliation.fetch_yookassa_payment", AsyncMock(return_value=verified_payment))

    # Mock _process_and_deliver returning created subscription ID without delivering messages itself
    async def mock_deliver(app, telegram_user_id, chat_id, tariff_id, platform_str, provisioning_payment_id, paid_amount_rub, notify_admin, deliver_customer_messages):
        assert notify_admin is False  # Must not send premature admin notification
        assert deliver_customer_messages is False  # Must not send premature customer messages
        async with db_session_factory() as session:
            sub = Subscription(
                user_id=db_user_id,
                server_id=1,
                tariff_months=1,
                tariff_days=30,
                client_name="notifysub",
                platform=Platform.ANDROID,
                expires_at=datetime.utcnow() + timedelta(days=30),
                status=SubStatus.ACTIVE,
                vpn_key="vless://delivered_key",
            )
            session.add(sub)
            await session.commit()
            return db_user_id, sub.id

    monkeypatch.setattr("bot.webhooks._process_and_deliver", mock_deliver)

    admin_notify_mock = AsyncMock()
    monkeypatch.setattr("bot.services.yookassa_reconciliation.notify_admins_payment", admin_notify_mock)

    customer_send_mock = AsyncMock()
    monkeypatch.setattr("bot.webhooks.send_subscription_delivery_messages", customer_send_mock)

    bot_mock = SimpleNamespace(send_message=AsyncMock())
    success, code = await verify_and_fulfill_yookassa_payment(bot_mock, "yk_notify_1")

    assert success is True
    assert code == "subscription_completed"

    # Exactly 1 admin notification and customer delivery called post-commit!
    admin_notify_mock.assert_awaited_once()
    customer_send_mock.assert_awaited_once()


# ── 8. Problem Payments Classification & Legitimate VHQ Exclusion ───────────


async def test_problem_payments_classification_and_vhq_handling(monkeypatch, db_session_factory):
    """Test classification into all 5 categories and proper VHQ provisioning handling."""
    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)

    now = datetime.utcnow()
    async with db_session_factory() as session:
        server = Server(id=1, name="srv1", host="1.1.1.1", location="Amsterdam")
        session.add(server)
        u1 = User(telegram_id=900051, username="u1")
        u2 = User(telegram_id=900052, username="u2")
        u3 = User(telegram_id=900053, username="u3")
        u4 = User(telegram_id=900054, username="u4")
        session.add_all([u1, u2, u3, u4])
        await session.flush()

        # Item A: Missing provider_payment_id (>15m)
        p_missing_id = Payment(
            user_id=u1.id,
            amount=10000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id=None,
            created_at=now - timedelta(minutes=25),
        )

        # Item B: Unresolved subscription (COMPLETED without subscription_id)
        p_unresolved = Payment(
            user_id=u2.id,
            amount=20000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            subscription_id=None,
            provider_payment_id="yk_unres_1",
            created_at=now - timedelta(hours=2),
        )

        # Item C: Legitimate VHQ in progress (attempts 1/5, 5m old) -> MUST BE EXCLUDED!
        sub_vhq_normal = Subscription(
            user_id=u3.id,
            server_id=server.id,
            tariff_months=1,
            tariff_days=30,
            client_name="vhq_norm",
            platform=Platform.ANDROID,
            expires_at=now + timedelta(days=30),
            status=SubStatus.ACTIVE,
        )
        session.add(sub_vhq_normal)
        await session.flush()
        p_vhq_normal = Payment(
            user_id=u3.id,
            subscription_id=sub_vhq_normal.id,
            amount=30000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            provider_payment_id="yk_vhq_norm_1",
            created_at=now - timedelta(minutes=5),
        )
        session.add(p_vhq_normal)
        await session.flush()
        vhq_f1 = VhqSubscriptionFulfillment(
            subscription_id=sub_vhq_normal.id,
            payment_id=p_vhq_normal.id,
            source_type="payment",
            source_id="yk_vhq_norm_1",
            status="pending",
            attempts=1,
            max_attempts=5,
            created_at=now - timedelta(minutes=5),
        )
        session.add(vhq_f1)

        # Item D: Stalled VHQ (failed status) -> FAILED_SUPPLIER!
        sub_vhq_failed = Subscription(
            user_id=u4.id,
            server_id=server.id,
            tariff_months=1,
            tariff_days=30,
            client_name="vhq_fail",
            platform=Platform.ANDROID,
            expires_at=now + timedelta(days=30),
            status=SubStatus.ACTIVE,
        )
        session.add(sub_vhq_failed)
        await session.flush()
        p_vhq_failed = Payment(
            user_id=u4.id,
            subscription_id=sub_vhq_failed.id,
            amount=40000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            provider_payment_id="yk_vhq_fail_1",
            created_at=now - timedelta(hours=1),
        )
        session.add(p_vhq_failed)
        await session.flush()
        vhq_f2 = VhqSubscriptionFulfillment(
            subscription_id=sub_vhq_failed.id,
            payment_id=p_vhq_failed.id,
            source_type="payment",
            source_id="yk_vhq_fail_1",
            status="failed",
            last_error_code="upstream_timeout",
            attempts=5,
            max_attempts=5,
            created_at=now - timedelta(hours=1),
        )
        session.add(vhq_f2)

        session.add_all([p_missing_id, p_unresolved])
        await session.commit()

    # Candidate classification check
    report = await build_problem_payments_report()

    # Legitimate VHQ item must NOT be in report!
    assert not any(item.provider_payment_id == "yk_vhq_norm_1" for item in report.items)

    # Failed VHQ item must be classified as FAILED_SUPPLIER
    failed_vhq = next((item for item in report.items if item.provider_payment_id == "yk_vhq_fail_1"), None)
    assert failed_vhq is not None
    assert failed_vhq.category == ProblemCategory.FAILED_SUPPLIER

    # Missing ID item must be classified as MISSING_PROVIDER_ID
    missing_item = next((item for item in report.items if item.item_id == p_missing_id.id), None)
    assert missing_item is not None
    assert missing_item.category == ProblemCategory.MISSING_PROVIDER_ID

    # Unresolved item must be classified as UNRESOLVED_SUBSCRIPTION
    unres_item = next((item for item in report.items if item.provider_payment_id == "yk_unres_1"), None)
    assert unres_item is not None
    assert unres_item.category == ProblemCategory.UNRESOLVED_SUBSCRIPTION


# ── 9. Admin Problem Payments UI & Safe Action Handlers ─────────────────────


async def test_admin_problem_payments_authorization_and_actions(monkeypatch, db_session_factory):
    """Test admin authorization, inspection, live refresh, and safe reconciliation."""
    from bot.handlers.admin import (
        admin_problem_payment_inspect,
        admin_problem_payment_reconcile,
        admin_problem_payment_refresh,
        admin_problem_payment_user,
        admin_problem_payments_list,
    )

    monkeypatch.setattr("bot.handlers.admin.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)

    mock_yk = {
        "id": "yk_action_1",
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "100.00", "currency": "RUB"},
    }
    monkeypatch.setattr("bot.services.problem_payments.fetch_yookassa_payment", AsyncMock(return_value=mock_yk))
    monkeypatch.setattr("bot.services.yookassa_reconciliation.fetch_yookassa_payment", AsyncMock(return_value=mock_yk))

    async with db_session_factory() as session:
        user = User(telegram_id=900060, username="actionuser")
        tariff = Tariff(id=83, label="Тариф 83", price_rub=100, price_stars=0, days=30, is_active=True, tariff_type=TariffType.VPN)
        session.add_all([user, tariff])
        await session.flush()
        payment = Payment(
            user_id=user.id,
            tariff_id=83,
            platform=Platform.ANDROID.value,
            amount=10000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_action_1",
            created_at=datetime.utcnow() - timedelta(minutes=20),
        )
        session.add(payment)
        await session.commit()
        pay_id = payment.id
        db_user_id = user.id

    # 1. Non-admin access attempt -> Denied!
    monkeypatch.setattr("bot.handlers.admin._is_admin", lambda uid: False)
    cb_non_admin = SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        data="adm_problem_payments",
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
    )
    await admin_problem_payments_list(cb_non_admin)
    assert any("нет доступа" in call.args[0].lower() for call in cb_non_admin.answer.call_args_list)

    # 2. Admin access attempt -> Success dashboard!
    monkeypatch.setattr("bot.handlers.admin._is_admin", lambda uid: True)
    cb_admin = SimpleNamespace(
        from_user=SimpleNamespace(id=999),
        data="adm_problem_payments",
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
    )
    await admin_problem_payments_list(cb_admin)
    cb_admin.message.edit_text.assert_awaited_once()
    dashboard_text = cb_admin.message.edit_text.call_args.args[0]
    assert "Проблемные платежи" in dashboard_text

    # 3. Inspect single item
    cb_inspect = SimpleNamespace(
        from_user=SimpleNamespace(id=999),
        data=f"adm_prob_inspect:p:{pay_id}:1",
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
    )
    await admin_problem_payment_inspect(cb_inspect)
    cb_inspect.message.edit_text.assert_awaited_once()
    inspect_text = cb_inspect.message.edit_text.call_args.args[0]
    assert "Инспекция проблемного платежа" in inspect_text
    assert f"#{pay_id}" in inspect_text

    # 4. Refresh single item
    cb_refresh = SimpleNamespace(
        from_user=SimpleNamespace(id=999),
        data=f"adm_prob_refresh:p:{pay_id}:1",
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
        model_copy=lambda update: SimpleNamespace(
            from_user=SimpleNamespace(id=999),
            data=update["data"],
            answer=AsyncMock(),
            message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
        ),
    )
    await admin_problem_payment_refresh(cb_refresh)

    # 5. Open user details
    show_user_mock = AsyncMock()
    monkeypatch.setattr("bot.handlers.admin._show_user_info", show_user_mock)
    cb_user = SimpleNamespace(
        from_user=SimpleNamespace(id=999),
        data=f"adm_prob_user:{db_user_id}",
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
    )
    await admin_problem_payment_user(cb_user)
    show_user_mock.assert_awaited_once()

    # 6. Reconcile confirmation prompt
    reconcile_mock = AsyncMock(return_value=(True, "subscription_completed"))
    monkeypatch.setattr("bot.services.yookassa_reconciliation.verify_and_fulfill_yookassa_payment", reconcile_mock)
    audit_mock = AsyncMock()
    monkeypatch.setattr("bot.handlers.admin.write_admin_audit", audit_mock)

    cb_reconcile = SimpleNamespace(
        from_user=SimpleNamespace(id=999),
        data=f"adm_prob_reconcile:p:{pay_id}:1",
        answer=AsyncMock(),
        bot=SimpleNamespace(send_message=AsyncMock()),
        message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
    )
    await admin_problem_payment_reconcile(cb_reconcile)
    cb_reconcile.message.edit_text.assert_awaited_once()
    assert "Подтверждение принудительной сверки" in cb_reconcile.message.edit_text.call_args.args[0]
    assert reconcile_mock.await_count == 0

    # 7. Confirmed reconcile action executes and writes admin audit
    from bot.handlers.admin import admin_problem_payment_reconcile_confirm
    cb_confirm = SimpleNamespace(
        from_user=SimpleNamespace(id=999),
        data=f"adm_prob_rec_confirm:p:{pay_id}:1",
        answer=AsyncMock(),
        bot=SimpleNamespace(send_message=AsyncMock()),
        message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
        model_copy=lambda update: SimpleNamespace(
            from_user=SimpleNamespace(id=999),
            data=update["data"],
            answer=AsyncMock(),
            message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
        ),
    )
    await admin_problem_payment_reconcile_confirm(cb_confirm)
    reconcile_mock.assert_awaited_once_with(cb_confirm.bot, "yk_action_1", source="admin_dashboard")
    audit_mock.assert_awaited_once()
    assert audit_mock.call_args.kwargs["action"] == "problem_payment_reconcile"


# ── 10. Review #5735231752: Financial UNIQUE Indexes & Checked Migration ──


async def test_financial_unique_indexes_actually_enforce_sqlite_uniqueness(db_session_factory):
    """Verify that SQLite itself enforces the 4 financial partial unique indexes via direct SQL."""
    from sqlalchemy.exc import IntegrityError

    async with db_session_factory() as session:
        u1 = User(id=1, telegram_id=1001, full_name="U1")
        u2 = User(id=2, telegram_id=1002, full_name="U2")
        p = Payment(id=10, user_id=1, amount=1000, currency="RUB", method=PaymentMethod.YOOKASSA, status=PaymentStatus.COMPLETED)
        part = Partner(id=1, telegram_id=1001, name="P1", commission_percent=20.0, is_active=True)
        session.add_all([u1, u2, p, part])
        await session.commit()

    async with db_session_factory() as session:
        conn = await session.connection()

        # 1. referral_payment_logs (uq_referral_payment_logs_payment_id)
        await conn.exec_driver_sql(
            "INSERT INTO referral_payment_logs (referrer_id, referred_user_id, payment_id, amount, paid_at) VALUES (1, 2, 10, 100, datetime('now'))"
        )
        with pytest.raises(IntegrityError):
            await conn.exec_driver_sql(
                "INSERT INTO referral_payment_logs (referrer_id, referred_user_id, payment_id, amount, paid_at) VALUES (1, 2, 10, 100, datetime('now'))"
            )

        # 2. referral_earnings (uq_referral_earnings_payment_id)
        await conn.exec_driver_sql(
            "INSERT INTO referral_earnings (referrer_id, referred_id, payment_id, amount_rub, created_at) VALUES (1, 2, 10, 50, datetime('now'))"
        )
        with pytest.raises(IntegrityError):
            await conn.exec_driver_sql(
                "INSERT INTO referral_earnings (referrer_id, referred_id, payment_id, amount_rub, created_at) VALUES (1, 2, 10, 50, datetime('now'))"
            )

        # 3. partner_earnings (uq_partner_earnings_payment_depth)
        await conn.exec_driver_sql(
            "INSERT INTO partner_earnings (partner_id, user_id, payment_id, amount, depth, commission_percent, created_at) VALUES (1, 2, 10, 20, 1, 10.0, datetime('now'))"
        )
        with pytest.raises(IntegrityError):
            await conn.exec_driver_sql(
                "INSERT INTO partner_earnings (partner_id, user_id, payment_id, amount, depth, commission_percent, created_at) VALUES (1, 2, 10, 20, 1, 10.0, datetime('now'))"
            )

        # 4. balance_transactions cashback (uq_balance_tx_payment_cashback)
        await conn.exec_driver_sql(
            "INSERT INTO balance_transactions (user_id, amount_rub, balance_after_rub, kind, direction, source_type, source_id, created_at) VALUES (1, 10, 10, 'CASHBACK', 'credit', 'payment', '10', datetime('now'))"
        )
        with pytest.raises(IntegrityError):
            await conn.exec_driver_sql(
                "INSERT INTO balance_transactions (user_id, amount_rub, balance_after_rub, kind, direction, source_type, source_id, created_at) VALUES (1, 10, 20, 'CASHBACK', 'credit', 'payment', '10', datetime('now'))"
            )


async def test_critical_financial_unique_indexes_migration_preflight_and_verification():
    """Test that _migrate_critical_financial_unique_indexes halts loudly with RuntimeError if duplicates exist, and creates indexes when clean."""
    from bot.database import _migrate_critical_financial_unique_indexes
    from sqlalchemy import text

    # Clean DB: migration succeeds and verifies in sqlite_master
    engine_clean = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine_clean.begin() as conn:
        await conn.execute(text("CREATE TABLE referral_payment_logs (id INTEGER PRIMARY KEY, payment_id INTEGER)"))
        await conn.execute(text("CREATE TABLE referral_earnings (id INTEGER PRIMARY KEY, payment_id INTEGER)"))
        await conn.execute(text("CREATE TABLE partner_earnings (id INTEGER PRIMARY KEY, payment_id INTEGER, depth INTEGER)"))
        await conn.execute(text("CREATE TABLE balance_transactions (id INTEGER PRIMARY KEY, kind TEXT, source_type TEXT, source_id TEXT)"))
        await _migrate_critical_financial_unique_indexes(conn)

        res = await conn.execute(text("SELECT name FROM sqlite_master WHERE type='index'"))
        indexes = {row[0] for row in res.fetchall()}
        assert "uq_referral_payment_logs_payment_id" in indexes
        assert "uq_referral_earnings_payment_id" in indexes
        assert "uq_partner_earnings_payment_depth" in indexes
        assert "uq_balance_tx_payment_cashback" in indexes
    await engine_clean.dispose()

    # Dirty DB with duplicates: preflight check fails loudly with RuntimeError without modifying data
    engine_dirty = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine_dirty.begin() as conn:
        await conn.execute(text("CREATE TABLE referral_payment_logs (id INTEGER PRIMARY KEY, payment_id INTEGER)"))
        await conn.execute(text("CREATE TABLE referral_earnings (id INTEGER PRIMARY KEY, payment_id INTEGER)"))
        await conn.execute(text("CREATE TABLE partner_earnings (id INTEGER PRIMARY KEY, payment_id INTEGER, depth INTEGER)"))
        await conn.execute(text("CREATE TABLE balance_transactions (id INTEGER PRIMARY KEY, kind TEXT, source_type TEXT, source_id TEXT)"))
        await conn.execute(text("INSERT INTO referral_payment_logs (payment_id) VALUES (42), (42)"))

        with pytest.raises(RuntimeError) as exc_info:
            await _migrate_critical_financial_unique_indexes(conn)
        assert "uq_referral_payment_logs_payment_id" in str(exc_info.value)
        assert "CRITICAL MIGRATION FAILURE" in str(exc_info.value)

        # Verify no records were deleted or altered
        count_res = await conn.execute(text("SELECT COUNT(*) FROM referral_payment_logs"))
        assert count_res.scalar() == 2
    await engine_dirty.dispose()


# ── 11. Competing Workers & Adapt Mutation Concurrency Fencing ──────────────


async def test_concurrent_competing_workers_adapt_mutation(monkeypatch, db_session_factory):
    """Two competing workers: Worker A hangs in Adapt mutation, lease expires, Worker B acquires lease but refuses duplicate mutation."""
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.cluster.async_session", db_session_factory)

    async with db_session_factory() as session:
        user = User(telegram_id=900070, username="compete_user")
        tariff = Tariff(id=85, label="Тариф 85", price_rub=300, price_stars=0, days=30, is_active=True, tariff_type=TariffType.VPN)
        session.add_all([user, tariff])
        await session.flush()
        payment = Payment(
            user_id=user.id,
            tariff_id=85,
            platform=Platform.ANDROID.value,
            amount=30000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_compete_w1",
        )
        session.add(payment)
        await session.commit()
        pay_id = payment.id

    verified_payment = {
        "id": "yk_compete_w1",
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "300.00", "currency": "RUB"},
        "metadata": {
            "user_id": "900070",
            "chat_id": "900070",
            "tariff_id": "85",
            "platform": Platform.ANDROID.value,
        },
    }
    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.fetch_yookassa_payment",
        AsyncMock(return_value=verified_payment),
    )

    worker_a_in_mutation = asyncio.Event()
    worker_a_can_finish = asyncio.Event()
    mutation_calls = 0

    async def mock_deliver(*args, **kwargs):
        nonlocal mutation_calls
        mutation_calls += 1
        worker_a_in_mutation.set()
        await worker_a_can_finish.wait()
        return user.id, 1001

    monkeypatch.setattr("bot.webhooks._process_and_deliver", mock_deliver)
    # Don't auto-renew in background so Worker A's lease can expire
    monkeypatch.setattr(LeaseHeartbeat, "start", lambda self: None)

    bot_mock = SimpleNamespace(send_message=AsyncMock())

    # Worker A starts reconciliation
    task_a = asyncio.create_task(
        verify_and_fulfill_yookassa_payment(bot_mock, "yk_compete_w1", source="reconciliation")
    )

    # Wait until Worker A is actively inside supplier mutation
    await asyncio.wait_for(worker_a_in_mutation.wait(), timeout=3.0)

    # Verify Worker A persisted mutation_in_flight BEFORE calling supplier!
    async with db_session_factory() as session:
        p_chk = await session.get(Payment, pay_id)
        assert p_chk.provisioning_failure_code == "mutation_in_flight"

        # Worker A's lease expires: delete lease so Worker B acquires it
        lease = await session.get(AppLease, "payment:yookassa:yk_compete_w1")
        assert lease is not None
        await session.delete(lease)
        await session.commit()

    try:
        # Worker B acquires the expired lease and runs reconciliation
        success_b, code_b = await verify_and_fulfill_yookassa_payment(
            bot_mock, "yk_compete_w1", source="reconciliation"
        )
    finally:
        worker_a_can_finish.set()

    # Worker B must REFUSE duplicate mutation
    assert success_b is False
    assert code_b == "supplier_mutation_in_flight_or_uncertain"
    assert mutation_calls == 1  # Only 1 mutation call!

    success_a, code_a = await task_a

    # Worker A detects lease lost post-mutation
    assert success_a is False
    assert code_a == "lease_expired"

    # Exactly 1 upstream mutation call occurred across both competing workers
    assert mutation_calls == 1

    async with db_session_factory() as session:
        p_final = await session.get(Payment, pay_id)
        assert p_final.provisioning_failure_code == "uncertain_supplier_mutation"
        assert p_final.status == PaymentStatus.PENDING


# ── 12. Lease Heartbeat Active Renewal Beyond TTL & Failure Handling ────────


async def test_lease_heartbeat_actively_renews_beyond_ttl_and_handles_exception(db_session_factory):
    """LeaseHeartbeat renews lease past TTL; on exception sets lease_lost and exits."""
    lm = LeaseManager(owner_id="heartbeat_worker_ttl", session_factory=db_session_factory)
    lock_name = "test:heartbeat:ttl_renewal"

    # TTL is 1.0 second
    acquired = await lm.acquire_or_renew(lock_name, ttl_seconds=1)
    assert acquired is True

    heartbeat = LeaseHeartbeat(lm, lock_name, ttl_seconds=1, interval_seconds=0.2)
    heartbeat.start()

    # Sleep 2.2 seconds (more than 2x TTL); heartbeat must keep lease alive
    await asyncio.sleep(2.2)
    assert heartbeat.lease_lost is False
    assert await lm.is_owned_by_me(lock_name) is True

    # Simulate DB error during renewal
    with patch.object(lm, "acquire_or_renew", AsyncMock(side_effect=RuntimeError("DB disconnected"))):
        await asyncio.sleep(0.4)
        assert heartbeat.lease_lost is True
        assert heartbeat._task.done() is True

    await heartbeat.stop()


# ── 13. Provider-Payment-ID Crash Gap Recovery ──────────────────────────────


async def test_crash_recovery_links_provider_id_and_fulfills_device(monkeypatch, db_session_factory):
    """Crash gap recovery: process crashes after YooKassa create; webhook/reconciliation safely links provider_payment_id via metadata.payment_id."""
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.cluster.async_session", db_session_factory)

    async with db_session_factory() as session:
        user = User(telegram_id=900080, username="crash_user")
        server = Server(id=1, name="srv1", host="1.1.1.1", location="Amsterdam")
        session.add_all([user, server])
        await session.flush()
        sub = Subscription(
            user_id=user.id,
            server_id=server.id,
            tariff_months=1,
            tariff_days=30,
            client_name="crash_sub",
            platform=Platform.ANDROID,
            device_slots=1,
            vpn_key="vless://crashkey",
            expires_at=datetime.utcnow() + timedelta(days=30),
            status=SubStatus.ACTIVE,
        )
        session.add(sub)
        await session.flush()
        # Payment created locally, but crash happened before provider_payment_id could be saved!
        payment = Payment(
            user_id=user.id,
            subscription_id=sub.id,
            amount=15000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id=None,  # CRASH GAP!
            platform="device",
        )
        session.add(payment)
        await session.commit()
        db_pay_id = payment.id
        db_sub_id = sub.id

    # Webhook or reconciliation comes with provider payment ID and metadata.payment_id
    yk_crash_payload = {
        "id": "yk_recovered_dev_1",
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "150.00", "currency": "RUB"},
        "metadata": {
            "payment_id": str(db_pay_id),
            "is_device": "1",
            "sub_id": str(db_sub_id),
            "user_id": "900080",
        },
    }
    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.fetch_yookassa_payment",
        AsyncMock(return_value=yk_crash_payload),
    )

    bot_mock = SimpleNamespace(send_message=AsyncMock())
    success, code = await verify_and_fulfill_yookassa_payment(bot_mock, "yk_recovered_dev_1")

    assert success is True
    assert code == "device_slot_completed"

    async with db_session_factory() as session:
        reloaded_pay = await session.get(Payment, db_pay_id)
        assert reloaded_pay.provider_payment_id == "yk_recovered_dev_1"
        assert reloaded_pay.status == PaymentStatus.COMPLETED

        reloaded_sub = await session.get(Subscription, db_sub_id)
        assert reloaded_sub.device_slots == 2


async def test_crash_recovery_mismatched_metadata_fails_closed(monkeypatch, db_session_factory):
    """Metadata spoofing or mismatch must fail closed and refuse to link provider_payment_id."""
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.cluster.async_session", db_session_factory)

    async with db_session_factory() as session:
        user_real = User(telegram_id=900081, username="real_user")
        session.add(user_real)
        await session.flush()
        payment = Payment(
            user_id=user_real.id,
            amount=15000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id=None,
            platform="device",
        )
        session.add(payment)
        await session.commit()
        db_pay_id = payment.id

    # YooKassa payment with user_id belonging to an attacker (999999 != 900081)
    yk_spoofed = {
        "id": "yk_spoof_1",
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "150.00", "currency": "RUB"},
        "metadata": {
            "payment_id": str(db_pay_id),
            "is_device": "1",
            "user_id": "999999",
        },
    }
    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.fetch_yookassa_payment",
        AsyncMock(return_value=yk_spoofed),
    )

    bot_mock = SimpleNamespace(send_message=AsyncMock())
    success, code = await verify_and_fulfill_yookassa_payment(bot_mock, "yk_spoof_1")

    assert success is False
    assert code == "missing_local_payment"

    async with db_session_factory() as session:
        reloaded_pay = await session.get(Payment, db_pay_id)
        assert reloaded_pay.provider_payment_id is None  # NOT LINKED!


# ── 14. Notification Timing & Unexpected IntegrityError Handling ────────────


async def test_unexpected_integrity_error_is_reraised(db_session_factory):
    """IntegrityError during credit_cashback or referral must be re-raised if no conflicting record exists."""
    from sqlalchemy.exc import IntegrityError
    from unittest.mock import patch

    async with db_session_factory() as session:
        user = User(telegram_id=900090, username="int_user", balance_rub=0.0)
        bot_settings = BotSettings(key="cashback_enabled", value="1")
        bot_settings_pct = BotSettings(key="cashback_percent", value="10")
        session.add_all([user, bot_settings, bot_settings_pct])
        await session.flush()
        payment = Payment(
            user_id=user.id,
            amount=10000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
        )
        session.add(payment)
        await session.commit()
        u_id = user.id
        p_id = payment.id

    async with db_session_factory() as session:
        with patch.object(session, "flush", AsyncMock(side_effect=IntegrityError("FOREIGN KEY constraint failed", None, None))):
            with pytest.raises(IntegrityError):
                await credit_cashback(session, u_id, p_id, 100.0)


# ── 15. Long-lived Unresolved Payments Remain Discoverable ──────────────────


async def test_old_unresolved_payments_remain_discoverable_and_audited(monkeypatch, db_session_factory):
    """Old unresolved records (>30 days old) are not crowded out by newer items in problem payments dashboard."""
    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)

    now = datetime.utcnow()
    async with db_session_factory() as session:
        user = User(telegram_id=900095, username="old_user")
        session.add(user)
        await session.flush()

        # 1 old pending payment (35 days old)
        old_payment = Payment(
            user_id=user.id,
            amount=50000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_old_35d",
            created_at=now - timedelta(days=35),
        )
        # 1 old completed unlinked payment (40 days old)
        old_unlinked = Payment(
            user_id=user.id,
            amount=60000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            subscription_id=None,
            provider_payment_id="yk_old_unlinked_40d",
            created_at=now - timedelta(days=40),
        )
        session.add_all([old_payment, old_unlinked])

        # 50 newer pending payments (10 minutes old)
        for i in range(50):
            p = Payment(
                user_id=user.id,
                amount=10000 + i,
                currency="RUB",
                method=PaymentMethod.YOOKASSA,
                status=PaymentStatus.PENDING,
                provider_payment_id=f"yk_newer_{i}",
                created_at=now - timedelta(minutes=10),
            )
            session.add(p)
        await session.commit()

    report = await build_problem_payments_report(limit=60)

    # 1. Total DB problems counts all 52 problem records accurately!
    assert report.total_db_problems == 52

    # 2. The 35-day-old payment and 40-day-old payment are both included in report items (not crowded out)
    item_provider_ids = [it.provider_payment_id for it in report.items]
    assert "yk_old_35d" in item_provider_ids
    assert "yk_old_unlinked_40d" in item_provider_ids


# ── 16. Transient Pre-Mutation Failure Does Not Permanently Leave In-Flight ──


async def test_transient_pre_mutation_failure_clears_in_flight_and_allows_subsequent_retry(monkeypatch, db_session_factory):
    """A transient pre-mutation failure clears mutation_in_flight so subsequent reconciliation can succeed."""
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.cluster.async_session", db_session_factory)
    monkeypatch.setattr("bot.database.async_session", db_session_factory)

    from bot.services.provisioning_issues import AccessProvisionError

    async with db_session_factory() as session:
        user = User(telegram_id=900101, username="transient_user")
        tariff = Tariff(id=301, label="Month", price_rub=100, days=30, is_active=True, tariff_type=TariffType.VPN)
        server = Server(name="srv_trans", host="1.1.1.1", location="Frankfurt", is_active=True)
        session.add_all([user, tariff, server])
        await session.flush()
        server_id = server.id

        payment = Payment(
            user_id=user.id,
            amount=10000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_transient_1",
            tariff_id=tariff.id,
            platform="android",
        )
        session.add(payment)
        await session.commit()
        p_id = payment.id
        u_id = user.id

    verified_yk = {
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "100.00", "currency": "RUB"},
        "metadata": {"user_id": str(user.telegram_id), "tariff_id": "301", "platform": "android"},
    }
    monkeypatch.setattr("bot.services.yookassa_reconciliation.fetch_yookassa_payment", AsyncMock(return_value=verified_yk))

    # 1. First attempt: _process_and_deliver raises transient AccessProvisionError
    fail_mock = AsyncMock(side_effect=AccessProvisionError(provider="vhq", code="missing_server", admin_message="No active server", client_message="Server error"))
    monkeypatch.setattr("bot.webhooks._process_and_deliver", fail_mock)

    bot_mock = SimpleNamespace(send_message=AsyncMock())
    success1, code1 = await verify_and_fulfill_yookassa_payment(bot_mock, "yk_transient_1", source="reconciliation")
    assert success1 is False
    assert "delivery_failed:missing_server" in code1

    # Verify that in DB, provisioning_failure_code was updated to missing_server (NOT left as mutation_in_flight)
    async with db_session_factory() as session:
        p1 = await session.get(Payment, p_id)
        assert p1.provisioning_failure_code == "missing_server"
        assert p1.provisioning_failure_code != "mutation_in_flight"
        assert p1.status == PaymentStatus.PENDING

    # 2. Second attempt: issue resolved, _process_and_deliver now succeeds!
    async with db_session_factory() as session:
        sub = Subscription(
            user_id=u_id,
            server_id=server_id,
            client_name="test_client_transient",
            tariff_months=1,
            tariff_days=30,
            vpn_key="vless://resolved_key",
            status=SubStatus.ACTIVE,
            platform=Platform.ANDROID,
            expires_at=datetime.utcnow() + timedelta(days=30),
        )
        session.add(sub)
        await session.commit()
        sub_id = sub.id

    success_deliver_mock = AsyncMock(return_value=(u_id, sub_id))
    monkeypatch.setattr("bot.webhooks._process_and_deliver", success_deliver_mock)

    success2, code2 = await verify_and_fulfill_yookassa_payment(bot_mock, "yk_transient_1", source="reconciliation")
    assert success2 is True
    assert code2 == "subscription_completed"

    async with db_session_factory() as session:
        p2 = await session.get(Payment, p_id)
        assert p2.status == PaymentStatus.COMPLETED
        assert p2.subscription_id == sub_id
        assert p2.provisioning_failure_code is None


async def test_adapt_create_subscription_allows_mutation_in_flight(monkeypatch, db_session_factory):
    """_adapt_create_new_subscription must not treat mutation_in_flight as an ambiguous create awaiting webhook."""
    from bot.services.subscription_service import _adapt_create_new_subscription
    from bot.services.provisioning_issues import AccessProvisionError

    async with db_session_factory() as session:
        user = User(telegram_id=900109, username="adapt_user")
        tariff = Tariff(id=309, label="Adapt Month", price_rub=200, days=30, is_active=True, tariff_type=TariffType.VPN, adapt_plan_uuid="plan-123")
        server = Server(name="srv_adapt", host="1.1.1.1", location="Frankfurt", is_active=True)
        session.add_all([user, tariff, server])
        await session.flush()

        payment = Payment(
            user_id=user.id,
            amount=20000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            tariff_id=tariff.id,
            provisioning_failure_code="mutation_in_flight",
        )
        session.add(payment)
        await session.commit()

        # Mock AdaptAPI
        adapt_mock = MagicMock()
        adapt_mock.create_subscription = AsyncMock(return_value={"subscription_uuid": "adapt-sub-uuid-1", "subscription_url": "https://adapt/sub1", "end_date": "2026-12-01T00:00:00Z"})
        monkeypatch.setattr("bot.services.subscription_service.AdaptAPI", lambda: adapt_mock)

        # Calling with mutation_in_flight must proceed to AdaptAPI.create_subscription!
        await _adapt_create_new_subscription(
            session,
            user=user,
            tariff=tariff,
            platform=Platform.ANDROID,
            server=server,
            provisioning_payment=payment,
        )
        adapt_mock.create_subscription.assert_awaited_once()

        # Conversely, calling with an ambiguous error code (e.g. adapt_runtime) MUST raise adapt_create_awaiting_webhook
        payment.provisioning_failure_code = "adapt_runtime"
        await session.commit()
        with pytest.raises(AccessProvisionError) as exc_info:
            await _adapt_create_new_subscription(
                session,
                user=user,
                tariff=tariff,
                platform=Platform.ANDROID,
                server=server,
                provisioning_payment=payment,
            )
        assert exc_info.value.code == "adapt_create_awaiting_webhook"


# ── 17. Auto-Retry Respects Uncertain and In-Flight Markers & Passes payment_id ──


async def test_auto_retry_failed_provisionings_respects_uncertain_and_in_flight(monkeypatch, db_session_factory):
    """auto_retry_failed_provisionings respects uncertain_supplier_mutation/in-flight and passes payment_id to referral logging."""
    from bot.services.scheduler import auto_retry_failed_provisionings

    monkeypatch.setattr("bot.services.scheduler.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.cluster.async_session", db_session_factory)
    monkeypatch.setattr("bot.database.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)

    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.fetch_yookassa_payment",
        AsyncMock(return_value={
            "status": "succeeded",
            "paid": True,
            "amount": {"value": "150.00", "currency": "RUB"},
            "metadata": {"user_id": "900102", "tariff_id": "302", "platform": "android"},
        }),
    )

    now = datetime.utcnow()
    async with db_session_factory() as session:
        user = User(telegram_id=900102, username="retry_user")
        tariff = Tariff(id=302, label="Month", price_rub=150, days=30, is_active=True, tariff_type=TariffType.VPN)
        server = Server(name="srv_retry", host="1.1.1.1", location="Frankfurt", is_active=True)
        session.add_all([user, tariff, server])
        await session.flush()
        server_id = server.id

        # Payment A: has uncertain_supplier_mutation marker
        p_uncertain = Payment(
            user_id=user.id,
            amount=15000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            subscription_id=None,
            provisioning_failure_code="uncertain_supplier_mutation",
            provider_payment_id="yk_retry_uncertain",
            tariff_id=tariff.id,
            created_at=now - timedelta(hours=1),
        )
        # Payment B: has mutation_in_flight marker from expired lease
        p_inflight = Payment(
            user_id=user.id,
            amount=15000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            subscription_id=None,
            provisioning_failure_code="mutation_in_flight",
            provider_payment_id="yk_retry_inflight",
            tariff_id=tariff.id,
            created_at=now - timedelta(hours=1),
        )
        # Payment C: clean completed payment awaiting auto-retry
        p_clean = Payment(
            user_id=user.id,
            amount=15000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            subscription_id=None,
            provisioning_failure_code=None,
            provider_payment_id="yk_retry_clean",
            tariff_id=tariff.id,
            created_at=now - timedelta(hours=1),
        )
        session.add_all([p_uncertain, p_inflight, p_clean])
        await session.commit()
        p_clean_id = p_clean.id

    create_access_calls = []

    async def mock_create_access(session, *args, **kwargs):
        payment = kwargs.get("provisioning_payment")
        create_access_calls.append(payment.id if payment else None)
        sub = Subscription(
            user_id=user.id,
            server_id=server_id,
            client_name="test_client_retry",
            tariff_months=1,
            tariff_days=30,
            vpn_key="vless://retry_key",
            status=SubStatus.ACTIVE,
            platform=Platform.ANDROID,
            expires_at=datetime.utcnow() + timedelta(days=30),
        )
        session.add(sub)
        await session.flush()
        return sub, "vless://retry_key"

    monkeypatch.setattr("bot.services.subscription_service.create_or_extend_paid_access", mock_create_access)
    monkeypatch.setattr("bot.webhooks.create_or_extend_paid_access", mock_create_access)

    logged_referrals = []

    async def mock_log_referral_payment(session, user_id, amount_rub, bot=None, payment_id=None):
        logged_referrals.append({"user_id": user_id, "amount_rub": amount_rub, "payment_id": payment_id})

    monkeypatch.setattr("bot.services.payment_service.log_referral_payment", mock_log_referral_payment)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.log_referral_payment", mock_log_referral_payment)
    monkeypatch.setattr("bot.services.scheduler.notify_staff_text", AsyncMock())

    bot_mock = SimpleNamespace(send_message=AsyncMock())
    await auto_retry_failed_provisionings(bot_mock)

    # Payments with uncertain_supplier_mutation and mutation_in_flight MUST NOT be mutated!
    assert len(create_access_calls) == 1
    assert create_access_calls[0] == p_clean_id

    # Clean payment must have logged referral with explicit payment_id!
    assert len(logged_referrals) == 1
    assert logged_referrals[0]["payment_id"] == p_clean_id

    async with db_session_factory() as session:
        p_clean_after = await session.get(Payment, p_clean_id)
        assert p_clean_after.subscription_id is not None
        assert p_clean_after.provisioning_failure_code is None


# ── 18. Dashboard Urgent Payments Ordering and Discovery ───────────────────────


async def test_dashboard_urgent_ordering_and_candidate_discovery(monkeypatch, db_session_factory):
    """Recent urgent confirmed-unfulfilled payments are not hidden behind older unresolved records."""
    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)

    now = datetime.utcnow()
    async with db_session_factory() as session:
        user = User(telegram_id=900103, username="dashboard_user")
        session.add(user)
        await session.flush()

        # 65 old abandoned pending payments (>30 days old)
        for i in range(65):
            p_old = Payment(
                user_id=user.id,
                amount=5000,
                currency="RUB",
                method=PaymentMethod.YOOKASSA,
                status=PaymentStatus.PENDING,
                provider_payment_id=f"yk_abandoned_{i}",
                created_at=now - timedelta(days=35, minutes=i),
            )
            session.add(p_old)

        # 1 fresh pending payment created 10 minutes ago that was actually paid at YooKassa
        p_urgent = Payment(
            user_id=user.id,
            amount=99000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_fresh_urgent_1",
            created_at=now - timedelta(minutes=10),
        )
        session.add(p_urgent)
        await session.commit()

    async def mock_fetch(payment_id):
        if payment_id == "yk_fresh_urgent_1":
            return {"status": "succeeded", "paid": True}
        return {"status": "pending", "paid": False}

    monkeypatch.setattr("bot.services.problem_payments.fetch_yookassa_payment", mock_fetch)

    # Build report with default limit=60
    report = await build_problem_payments_report(limit=60)

    # 1. The fresh urgent payment must be discovered (not starved out by the 65 old abandoned ones)
    item_ids = [it.provider_payment_id for it in report.items]
    assert "yk_fresh_urgent_1" in item_ids

    # 2. It must appear as Item #0 at the very top of report.items (highest priority urgent tier)
    assert report.items[0].provider_payment_id == "yk_fresh_urgent_1"
    assert report.items[0].category == ProblemCategory.CONFIRMED_UNFULFILLED


# ── 19. Mutually Exclusive Problem Totals ─────────────────────────────────────


async def test_problem_payments_report_mutually_exclusive_totals(monkeypatch, db_session_factory):
    """Problem payment totals must be mutually exclusive with zero double-counting across categories."""
    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)

    now = datetime.utcnow()
    async with db_session_factory() as session:
        user = User(telegram_id=900104, username="totals_user")
        server = Server(name="test_srv_totals", host="10.0.0.1", location="Frankfurt", is_active=True)
        session.add_all([user, server])
        await session.flush()

        # 1. Payment with provisioning_failure_code AND failed VHQ fulfillment (must be counted ONCE in FAILED_SUPPLIER)
        p_failed_supplier = Payment(
            user_id=user.id,
            amount=10000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            subscription_id=None,
            provisioning_failure_code="missing_server",
            provider_payment_id="yk_failed_sup_1",
            created_at=now - timedelta(minutes=20),
        )
        session.add(p_failed_supplier)
        await session.flush()
        sub_failed = Subscription(
            user_id=user.id,
            server_id=server.id,
            client_name="test_client_totals",
            tariff_months=1,
            tariff_days=30,
            vpn_key="vless://key1",
            status=SubStatus.ACTIVE,
            platform=Platform.ANDROID,
            expires_at=now + timedelta(days=30),
        )
        session.add(sub_failed)
        await session.flush()
        session.add(VhqSubscriptionFulfillment(
            subscription_id=sub_failed.id,
            payment_id=p_failed_supplier.id,
            user_id=user.id,
            source_type="payment",
            source_id=str(p_failed_supplier.id),
            fulfillment_uuid="vhq_failed_uuid",
            target_local_expires_at=now + timedelta(days=30),
            status="failed",
            attempts=3,
            max_attempts=3,
        ))

        # 2. Completed payment without subscription and NO failure code (UNRESOLVED_SUBSCRIPTION)
        p_unresolved = Payment(
            user_id=user.id,
            amount=12000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            subscription_id=None,
            provisioning_failure_code=None,
            provider_payment_id="yk_unres_1",
            created_at=now - timedelta(minutes=25),
        )
        # 3. Pending payment with missing provider_payment_id >15m (MISSING_PROVIDER_ID)
        p_missing_id = Payment(
            user_id=user.id,
            amount=13000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id=None,
            created_at=now - timedelta(minutes=20),
        )
        # 4. Pending topup with missing provider_payment_id >15m (MISSING_PROVIDER_ID)
        t_missing_id = BalanceTopUp(
            user_id=user.id,
            telegram_id=user.telegram_id,
            amount_rub=100.0,
            provider="yookassa",
            status="pending",
            provider_payment_id=None,
            created_at=now - timedelta(minutes=20),
        )
        # 5. Pending payment verified as SUCCEEDED (CONFIRMED_UNFULFILLED)
        p_confirmed = Payment(
            user_id=user.id,
            amount=14000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_confirmed_1",
            created_at=now - timedelta(minutes=10),
        )
        # 6. Pending payment verified as PENDING (PROVIDER_PENDING)
        p_pending = Payment(
            user_id=user.id,
            amount=15000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_pending_1",
            created_at=now - timedelta(minutes=10),
        )
        session.add_all([p_unresolved, p_missing_id, t_missing_id, p_confirmed, p_pending])
        await session.commit()

    async def mock_fetch(payment_id):
        if payment_id == "yk_confirmed_1":
            return {"status": "succeeded", "paid": True}
        return {"status": "pending", "paid": False}

    monkeypatch.setattr("bot.services.problem_payments.fetch_yookassa_payment", mock_fetch)

    report = await build_problem_payments_report(limit=60)

    # Verify mutually exclusive counts
    assert report.counts[ProblemCategory.FAILED_SUPPLIER] == 1  # p_failed_supplier counted once, not twice
    assert report.counts[ProblemCategory.UNRESOLVED_SUBSCRIPTION] == 1  # p_unresolved only
    assert report.counts[ProblemCategory.MISSING_PROVIDER_ID] == 2  # p_missing_id + t_missing_id
    assert report.counts[ProblemCategory.CONFIRMED_UNFULFILLED] == 1  # p_confirmed
    assert report.counts[ProblemCategory.PROVIDER_PENDING] == 1  # p_pending (p_confirmed is excluded!)

    # Exact total
    assert report.total_db_problems == 6
    assert sum(report.counts.values()) == 6


# ── 20. Read-Only Production Financial-Index Duplicate Check ──────────────────


async def test_deploy_safety_preflight_checks_financial_duplicates_read_only(tmp_path):
    """inspect_database executes duplicate queries in read-only mode and rejects duplicates before downtime."""
    import sqlite3
    from scripts import darimiru_deploy_safety as safety

    db_path = tmp_path / "bot_preflight.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, telegram_id INTEGER)")
        conn.execute("CREATE TABLE balance_topups (id INTEGER PRIMARY KEY, provider_payment_id TEXT)")
        conn.execute("CREATE TABLE referral_payment_logs (id INTEGER PRIMARY KEY, payment_id INTEGER, user_id INTEGER, amount REAL)")
        conn.execute("CREATE TABLE referral_earnings (id INTEGER PRIMARY KEY, payment_id INTEGER, user_id INTEGER, amount REAL)")
        conn.execute("CREATE TABLE partner_earnings (id INTEGER PRIMARY KEY, payment_id INTEGER, depth INTEGER, amount REAL)")
        conn.execute("CREATE TABLE balance_transactions (id INTEGER PRIMARY KEY, kind TEXT, source_type TEXT, source_id TEXT)")

        # Normal unique records
        conn.execute("INSERT INTO users (id, telegram_id) VALUES (1, 123)")
        conn.execute("INSERT INTO referral_payment_logs (id, payment_id, user_id, amount) VALUES (1, 101, 1, 100.0)")
        conn.execute("INSERT INTO referral_payment_logs (id, payment_id, user_id, amount) VALUES (2, 102, 1, 100.0)")

    # 1. Clean database passes duplicate check without SafetyError
    result = safety.inspect_database(db_path, "bot")
    assert isinstance(result, dict)
    assert "missing_indexes" in result

    # 2. Insert duplicate into referral_payment_logs
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO referral_payment_logs (id, payment_id, user_id, amount) VALUES (3, 101, 1, 100.0)")

    with pytest.raises(safety.SafetyError, match="ambiguous financial duplicates in referral_payment_logs"):
        safety.inspect_database(db_path, "bot")

    # Clean up duplicate and test partner_earnings duplicate
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM referral_payment_logs WHERE id = 3")
        conn.execute("INSERT INTO partner_earnings (id, payment_id, depth, amount) VALUES (1, 201, 1, 50.0)")
        conn.execute("INSERT INTO partner_earnings (id, payment_id, depth, amount) VALUES (2, 201, 1, 50.0)")

    with pytest.raises(safety.SafetyError, match="ambiguous financial duplicates in partner_earnings"):
        safety.inspect_database(db_path, "bot")


# ── 21. Ambiguous Adapt Mutation Never Automatically Retryable ────────────────


async def test_ambiguous_adapt_mutation_incomplete_response_refuses_second_mutation(monkeypatch, db_session_factory):
    """Adapt mutation with incomplete response sets uncertain_supplier_mutation; second attempt refuses mutation."""
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.cluster.async_session", db_session_factory)
    monkeypatch.setattr("bot.database.async_session", db_session_factory)
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)

    async with db_session_factory() as session:
        user = User(telegram_id=900301, username="ambig_user")
        tariff = Tariff(
            id=305,
            label="Adapt Plan",
            price_rub=100,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            adapt_plan_uuid="adapt_plan_uuid_123",
        )
        server = Server(name="srv_ambig", host="1.1.1.1", location="Frankfurt", is_active=True)
        session.add_all([user, tariff, server])
        await session.flush()

        payment = Payment(
            user_id=user.id,
            amount=10000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_ambig_adapt_1",
            tariff_id=tariff.id,
            platform="android",
        )
        session.add(payment)
        await session.commit()
        p_id = payment.id

    verified_yk = {
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "100.00", "currency": "RUB"},
        "metadata": {"user_id": "900301", "tariff_id": "305", "platform": "android"},
    }
    monkeypatch.setattr("bot.services.yookassa_reconciliation.fetch_yookassa_payment", AsyncMock(return_value=verified_yk))

    # Mock AdaptAPI().create_subscription to return an incomplete response (no subscription_uuid)
    adapt_create_mock = AsyncMock(return_value={"status": "ok", "devices": 3})  # Missing subscription_uuid!
    monkeypatch.setattr("bot.services.adapt_api.AdaptAPI.create_subscription", adapt_create_mock)

    bot_mock = SimpleNamespace(send_message=AsyncMock())

    # 1. First reconciliation attempt:
    # AdaptAPI called, but incomplete response raises adapt_missing_uuid
    success1, code1 = await verify_and_fulfill_yookassa_payment(bot_mock, "yk_ambig_adapt_1", source="reconciliation")
    assert success1 is False
    assert "uncertain_supplier_mutation" in code1

    # Verify that in DB, provisioning_failure_code is preserved as uncertain_supplier_mutation
    async with db_session_factory() as session:
        p1 = await session.get(Payment, p_id)
        assert p1.provisioning_failure_code == "uncertain_supplier_mutation"
        assert p1.status == PaymentStatus.PENDING
        assert p1.subscription_id is None

    assert adapt_create_mock.call_count == 1

    # 2. Second reconciliation attempt:
    # Must refuse duplicate mutation and NOT call AdaptAPI().create_subscription again!
    success2, code2 = await verify_and_fulfill_yookassa_payment(bot_mock, "yk_ambig_adapt_1", source="reconciliation")
    assert success2 is False
    assert code2 == "supplier_mutation_in_flight_or_uncertain"

    # CRITICAL: Verify that ONLY ONE supplier mutation occurred across both attempts
    assert adapt_create_mock.call_count == 1


# ── 22. YooKassa Auto-Retry Authenticates Provider Payment Before Provisioning ──


async def test_auto_retry_yookassa_canceled_never_provisions(monkeypatch, db_session_factory):
    """auto_retry_failed_provisionings does not provision canceled YooKassa payments and marks them FAILED."""
    monkeypatch.setattr("bot.services.scheduler.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr("bot.database.async_session", db_session_factory)

    async with db_session_factory() as session:
        user = User(telegram_id=900401, username="canceled_retry_user", balance_rub=10.0)
        tariff = Tariff(id=306, label="T306", price_rub=100, days=30, is_active=True, tariff_type=TariffType.VPN)
        server = Server(name="srv_c", host="1.1.1.1", location="Frankfurt", is_active=True)
        session.add_all([user, tariff, server])
        await session.flush()

        payment = Payment(
            user_id=user.id,
            amount=8000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,  # Locally marked COMPLETED, but canceled at provider!
            provider_payment_id="yk_retry_canceled_1",
            tariff_id=tariff.id,
            platform="android",
            discount_applied=20.0,
        )
        session.add(payment)
        await session.flush()

        # Simulate debited balance discount transaction and referral log
        from bot.models import ReferralPaymentLog, BalanceTransaction, BalanceDirection
        tx = BalanceTransaction(
            user_id=user.id,
            amount_rub=20.0,
            balance_after_rub=10.0,
            kind=BalanceTransactionKind.TOPUP,
            direction=BalanceDirection.DEBIT,
            description="discount debit",
            source_type="payment_discount",
            source_id="yk_retry_canceled_1",
        )
        session.add(tx)
        await session.commit()
        p_id = payment.id

    from bot.services import scheduler
    monkeypatch.setattr(scheduler, "async_session", db_session_factory)

    canceled_yk = {
        "status": "canceled",
        "paid": False,
        "amount": {"value": "80.00", "currency": "RUB"},
        "metadata": {"user_id": "900401", "tariff_id": "306"},
    }
    monkeypatch.setattr("bot.services.yookassa_reconciliation.fetch_yookassa_payment", AsyncMock(return_value=canceled_yk))

    supplier_provision_mock = AsyncMock()
    monkeypatch.setattr("bot.services.subscription_service.create_or_extend_paid_access", supplier_provision_mock)
    monkeypatch.setattr("bot.webhooks.create_or_extend_paid_access", supplier_provision_mock)

    bot_mock = SimpleNamespace(send_message=AsyncMock())
    await scheduler.auto_retry_failed_provisionings(bot_mock)

    # Provider provisioning must NEVER be called for canceled payment
    assert not supplier_provision_mock.called

    async with db_session_factory() as session:
        p = await session.get(Payment, p_id)
        assert p.status == PaymentStatus.FAILED
        assert p.provisioning_failure_code == "payment_canceled_at_provider"
        assert p.subscription_id is None

        # Debited discount was refunded to user balance
        from bot.models import BalanceTransaction
        refund_tx = await session.scalar(
            select(BalanceTransaction).where(
                BalanceTransaction.user_id == p.user_id,
                BalanceTransaction.source_type == "payment_discount_refund",
            )
        )
        assert refund_tx is not None
        assert refund_tx.amount_rub == 20.0
        u = await session.get(User, p.user_id)
        assert u.balance_rub == 30.0  # 10.0 initial + 20.0 refunded


async def test_auto_retry_yookassa_pending_never_provisions(monkeypatch, db_session_factory):
    """auto_retry_failed_provisionings does not provision pending YooKassa payments."""
    monkeypatch.setattr("bot.services.scheduler.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr("bot.database.async_session", db_session_factory)

    async with db_session_factory() as session:
        user = User(telegram_id=900402, username="pending_retry_user")
        tariff = Tariff(id=307, label="T307", price_rub=100, days=30, is_active=True, tariff_type=TariffType.VPN)
        server = Server(name="srv_p", host="1.1.1.1", location="Frankfurt", is_active=True)
        session.add_all([user, tariff, server])
        await session.flush()

        payment = Payment(
            user_id=user.id,
            amount=10000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            provider_payment_id="yk_retry_pending_1",
            tariff_id=tariff.id,
            platform="android",
        )
        session.add(payment)
        await session.commit()
        p_id = payment.id

    from bot.services import scheduler
    monkeypatch.setattr(scheduler, "async_session", db_session_factory)

    pending_yk = {
        "status": "pending",
        "paid": False,
        "amount": {"value": "100.00", "currency": "RUB"},
        "metadata": {"user_id": "900402", "tariff_id": "307"},
    }
    monkeypatch.setattr("bot.services.yookassa_reconciliation.fetch_yookassa_payment", AsyncMock(return_value=pending_yk))

    supplier_provision_mock = AsyncMock()
    monkeypatch.setattr("bot.services.subscription_service.create_or_extend_paid_access", supplier_provision_mock)
    monkeypatch.setattr("bot.webhooks.create_or_extend_paid_access", supplier_provision_mock)

    bot_mock = SimpleNamespace(send_message=AsyncMock())
    await scheduler.auto_retry_failed_provisionings(bot_mock)

    # Provider provisioning must NEVER be called
    assert not supplier_provision_mock.called

    async with db_session_factory() as session:
        p = await session.get(Payment, p_id)
        assert p.subscription_id is None


async def test_auto_retry_yookassa_mismatched_never_provisions(monkeypatch, db_session_factory):
    """auto_retry_failed_provisionings fails closed on mismatched amount or user."""
    monkeypatch.setattr("bot.services.scheduler.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr("bot.database.async_session", db_session_factory)

    async with db_session_factory() as session:
        user = User(telegram_id=900403, username="mismatch_retry_user")
        tariff = Tariff(id=308, label="T308", price_rub=100, days=30, is_active=True, tariff_type=TariffType.VPN)
        server = Server(name="srv_m", host="1.1.1.1", location="Frankfurt", is_active=True)
        session.add_all([user, tariff, server])
        await session.flush()

        payment = Payment(
            user_id=user.id,
            amount=10000,  # 100.00 RUB
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            provider_payment_id="yk_retry_mismatch_1",
            tariff_id=tariff.id,
            platform="android",
        )
        session.add(payment)
        await session.commit()
        p_id = payment.id

    from bot.services import scheduler
    monkeypatch.setattr(scheduler, "async_session", db_session_factory)

    # Amount mismatch: paid only 50.00 RUB instead of 100.00 RUB
    mismatched_yk = {
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "50.00", "currency": "RUB"},
        "metadata": {"user_id": "900403", "tariff_id": "308"},
    }
    monkeypatch.setattr("bot.services.yookassa_reconciliation.fetch_yookassa_payment", AsyncMock(return_value=mismatched_yk))

    supplier_provision_mock = AsyncMock()
    monkeypatch.setattr("bot.services.subscription_service.create_or_extend_paid_access", supplier_provision_mock)
    monkeypatch.setattr("bot.webhooks.create_or_extend_paid_access", supplier_provision_mock)

    bot_mock = SimpleNamespace(send_message=AsyncMock())
    await scheduler.auto_retry_failed_provisionings(bot_mock)

    assert not supplier_provision_mock.called

    async with db_session_factory() as session:
        p = await session.get(Payment, p_id)
        assert p.subscription_id is None


async def test_auto_retry_yookassa_missing_provider_id_never_provisions(monkeypatch, db_session_factory):
    """auto_retry_failed_provisionings refuses unverified provisioning when provider_payment_id is missing."""
    monkeypatch.setattr("bot.services.scheduler.async_session", db_session_factory)
    monkeypatch.setattr("bot.database.async_session", db_session_factory)

    async with db_session_factory() as session:
        user = User(telegram_id=900404, username="noprov_user")
        tariff = Tariff(id=309, label="T309", price_rub=100, days=30, is_active=True, tariff_type=TariffType.VPN)
        server = Server(name="srv_np", host="1.1.1.1", location="Frankfurt", is_active=True)
        session.add_all([user, tariff, server])
        await session.flush()

        payment = Payment(
            user_id=user.id,
            amount=10000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            provider_payment_id=None,  # Missing provider_payment_id
            tariff_id=tariff.id,
            platform="android",
        )
        session.add(payment)
        await session.commit()
        p_id = payment.id

    from bot.services import scheduler
    monkeypatch.setattr(scheduler, "async_session", db_session_factory)

    supplier_provision_mock = AsyncMock()
    monkeypatch.setattr("bot.services.subscription_service.create_or_extend_paid_access", supplier_provision_mock)

    bot_mock = SimpleNamespace(send_message=AsyncMock())
    await scheduler.auto_retry_failed_provisionings(bot_mock)

    assert not supplier_provision_mock.called

    async with db_session_factory() as session:
        p = await session.get(Payment, p_id)
        assert p.subscription_id is None


async def test_auto_retry_yookassa_legitimate_captured_recovers(monkeypatch, db_session_factory):
    """auto_retry_failed_provisionings successfully recovers legitimate captured payment verified via API."""
    monkeypatch.setattr("bot.services.scheduler.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr("bot.database.async_session", db_session_factory)
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)

    async with db_session_factory() as session:
        user = User(telegram_id=900405, username="legit_retry_user")
        tariff = Tariff(id=310, label="T310", price_rub=100, days=30, is_active=True, tariff_type=TariffType.VPN)
        server = Server(name="srv_legit", host="1.1.1.1", location="Frankfurt", is_active=True)
        session.add_all([user, tariff, server])
        await session.flush()

        payment = Payment(
            user_id=user.id,
            amount=10000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            provider_payment_id="yk_retry_legit_1",
            tariff_id=tariff.id,
            platform="android",
        )
        session.add(payment)
        await session.commit()
        p_id = payment.id
        u_id = user.id
        s_id = server.id

    from bot.services import scheduler
    monkeypatch.setattr(scheduler, "async_session", db_session_factory)

    legit_yk = {
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "100.00", "currency": "RUB"},
        "metadata": {"user_id": "900405", "tariff_id": "310", "platform": "android"},
    }
    monkeypatch.setattr("bot.services.yookassa_reconciliation.fetch_yookassa_payment", AsyncMock(return_value=legit_yk))

    async def mock_create_access(session, user, tariff, platform, bonus_days, **kwargs):
        sub = Subscription(
            user_id=u_id,
            server_id=s_id,
            tariff_months=1,
            tariff_days=30,
            status=SubStatus.ACTIVE,
            device_slots=3,
            vpn_key="vless://auto_retry_recovered",
            client_name="tg900405_1",
            platform=Platform.ANDROID,
            expires_at=datetime.utcnow() + timedelta(days=30),
        )
        session.add(sub)
        await session.flush()
        return sub, "vless://auto_retry_recovered"

    monkeypatch.setattr("bot.webhooks.create_or_extend_paid_access", mock_create_access)
    monkeypatch.setattr("bot.services.subscription_service.create_or_extend_paid_access", mock_create_access)

    bot_mock = SimpleNamespace(send_message=AsyncMock())
    await scheduler.auto_retry_failed_provisionings(bot_mock)

    async with db_session_factory() as session:
        p = await session.get(Payment, p_id)
        assert p.subscription_id is not None
        sub = await session.get(Subscription, p.subscription_id)
        assert sub is not None
        assert sub.vpn_key == "vless://auto_retry_recovered"


# ── 23. Durable Supplier Fulfillment Before Financial Finalization ────────────


async def test_adapt_creation_succeeds_then_financial_finalization_fails_and_recovers_on_retry(monkeypatch, db_session_factory):
    """Adapt creates subscription; financial finalization fails on attempt 1.
    Retry resumes only incomplete accounting without second Adapt mutation."""
    from bot.services.yookassa_reconciliation import verify_and_fulfill_yookassa_payment
    from bot.models import ReferralPaymentLog, ReferralEarning, PartnerEarning, BalanceTransaction, BalanceDirection

    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.cluster.async_session", db_session_factory)
    monkeypatch.setattr("bot.database.async_session", db_session_factory)
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)

    async with db_session_factory() as session:
        user = User(telegram_id=900501, username="fin_fail_user")
        tariff = Tariff(
            id=311,
            label="Adapt Fin Plan",
            price_rub=100,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            adapt_plan_uuid="adapt_plan_uuid_fin",
        )
        server = Server(name="srv_fin", host="1.1.1.1", location="Frankfurt", is_active=True)
        session.add_all([user, tariff, server])
        await session.flush()

        payment = Payment(
            user_id=user.id,
            amount=10000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_fin_fail_1",
            tariff_id=tariff.id,
            platform="android",
        )
        session.add(payment)
        await session.commit()
        p_id = payment.id
        u_id = user.id

    verified_yk = {
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "100.00", "currency": "RUB"},
        "metadata": {"user_id": "900501", "tariff_id": "311", "platform": "android"},
    }
    monkeypatch.setattr("bot.services.yookassa_reconciliation.fetch_yookassa_payment", AsyncMock(return_value=verified_yk))

    adapt_create_mock = AsyncMock(return_value={
        "subscription_uuid": "adapt_uuid_fin_1",
        "subscription_url": "https://adapt.test/s/fin1",
        "days": 30,
        "devices": 2,
    })
    monkeypatch.setattr("bot.services.adapt_api.AdaptAPI.create_subscription", adapt_create_mock)

    # Track financial side effect calls
    referral_calls = []
    partner_calls = []
    cashback_calls = []

    async def mock_credit_referral(session, user_id, payment_id, amount_rub, bot=None):
        referral_calls.append(payment_id)
        earning = ReferralEarning(referrer_id=999, referred_id=user_id, payment_id=payment_id, amount_rub=10.0)
        session.add(earning)

    async def mock_log_referral_payment(session, user_id, amount_rub, bot=None, payment_id=None):
        if payment_id:
            log_rec = ReferralPaymentLog(referrer_id=999, referred_user_id=user_id, payment_id=payment_id, amount=amount_rub)
            session.add(log_rec)

    fail_financial_finalization = True

    async def mock_credit_partner(session, user_id, payment_id, amount_rub, bot=None):
        partner_calls.append(payment_id)
        if fail_financial_finalization:
            raise RuntimeError("Simulated transient financial finalization failure!")
        earning = PartnerEarning(partner_id=888, user_id=user_id, payment_id=payment_id, depth=1, commission_percent=10.0, amount=5.0)
        session.add(earning)

    async def mock_credit_cashback(session, user_id, payment_id, amount_rub, bot=None):
        cashback_calls.append(payment_id)
        tx = BalanceTransaction(
            user_id=user_id,
            amount_rub=5.0,
            balance_after_rub=5.0,
            kind=BalanceTransactionKind.CASHBACK,
            direction=BalanceDirection.CREDIT,
            description="cashback",
            source_type="payment",
            source_id=str(payment_id),
        )
        session.add(tx)
        return {"status": "credited"}

    monkeypatch.setattr("bot.services.yookassa_reconciliation.credit_referral", mock_credit_referral)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.log_referral_payment", mock_log_referral_payment)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.credit_partner", mock_credit_partner)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.credit_cashback", mock_credit_cashback)

    bot_mock = SimpleNamespace(send_message=AsyncMock())

    # 1. ATTEMPT 1: Adapt creates subscription, but financial finalization fails
    success1, code1 = await verify_and_fulfill_yookassa_payment(bot_mock, "yk_fin_fail_1", source="reconciliation")
    assert success1 is False
    assert "financial_finalization_failed" in code1

    # The customer gets the durable neutral payment acknowledgement while
    # fulfillment remains incomplete.
    neutral_ack = "✅ Оплата получена. Ваш VPN-доступ готовится. Мы автоматически отправим его сюда после активации"
    assert [call.args[1] for call in bot_mock.send_message.await_args_list] == [neutral_ack]

    # Upstream Adapt creation was called exactly once
    assert adapt_create_mock.call_count == 1

    # Local Subscription was committed atomically with the Payment linkage!
    async with db_session_factory() as session:
        all_subs = (await session.execute(select(Subscription))).scalars().all()
        assert len(all_subs) == 1
        sub1 = all_subs[0]

        p1 = await session.get(Payment, p_id)
        assert p1.subscription_id == sub1.id
        assert p1.provisioning_failure_code == "financial_finalization_pending"
        assert p1.status == PaymentStatus.PENDING  # Not completed until financial commit succeeds!

    # 2. ATTEMPT 2: Transient error resolved; retry the same payment
    fail_financial_finalization = False

    success2, code2 = await verify_and_fulfill_yookassa_payment(bot_mock, "yk_fin_fail_1", source="reconciliation")
    assert success2 is True
    assert code2 == "subscription_completed"

    # CRITICAL: Upstream Adapt creation was NOT called again! Exactly ONE call across both attempts!
    assert adapt_create_mock.call_count == 1

    # CRITICAL: Exactly ONE local subscription exists!
    async with db_session_factory() as session:
        all_subs = (await session.execute(select(Subscription))).scalars().all()
        assert len(all_subs) == 1

        p2 = await session.get(Payment, p_id)
        assert p2.subscription_id == sub1.id
        assert p2.status == PaymentStatus.COMPLETED
        assert p2.provisioning_failure_code is None

        # Verify financial side effects committed at most once
        ref_logs = (await session.execute(select(ReferralPaymentLog).where(ReferralPaymentLog.payment_id == p_id))).scalars().all()
        assert len(ref_logs) == 1

        ref_earnings = (await session.execute(select(ReferralEarning).where(ReferralEarning.payment_id == p_id))).scalars().all()
        assert len(ref_earnings) == 1

        partner_earnings = (await session.execute(select(PartnerEarning).where(PartnerEarning.payment_id == p_id))).scalars().all()
        assert len(partner_earnings) == 1

        cashback_txs = (await session.execute(select(BalanceTransaction).where(BalanceTransaction.source_id == str(p_id)))).scalars().all()
        assert len(cashback_txs) == 1

    # Success delivery notification was sent post-commit!
    assert bot_mock.send_message.called


async def test_yookassa_canceled_with_financial_effects_preserves_records_and_flags_review(monkeypatch, db_session_factory):
    """Review #6 fix: Canceled payment with financial side effects preserves ledger records and flags admin review."""
    from bot.services import scheduler, problem_payments
    from bot.services.yookassa_reconciliation import verify_and_fulfill_yookassa_payment
    monkeypatch.setattr(scheduler, "async_session", db_session_factory)
    monkeypatch.setattr(problem_payments, "async_session", db_session_factory)

    async with db_session_factory() as session:
        referrer = User(telegram_id=888111, username="canc_ref", balance_rub=50.0)
        partner = Partner(name="Canc Partner", partner_balance=100.0)
        session.add_all([referrer, partner])
        await session.flush()

        user = User(telegram_id=888222, username="canc_user", referred_by=888111, partner_id=partner.id)
        tariff = Tariff(id=401, label="T401", price_rub=100, days=30, is_active=True, tariff_type=TariffType.VPN)
        session.add_all([user, tariff])
        await session.flush()

        payment = Payment(
            user_id=user.id,
            amount=10000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_canc_fin_1",
            tariff_id=tariff.id,
            platform="android",
        )
        session.add(payment)
        await session.flush()

        # Seed financial side effects
        ref_earning = ReferralEarning(
            referrer_id=referrer.id,
            referred_id=user.id,
            payment_id=payment.id,
            amount_rub=10.0,
        )
        ref_log = ReferralPaymentLog(
            referrer_id=referrer.id,
            referred_user_id=user.id,
            payment_id=payment.id,
            amount=100.0,
        )
        partner_earning = PartnerEarning(
            partner_id=partner.id,
            user_id=user.id,
            payment_id=payment.id,
            amount=15.0,
        )
        session.add_all([ref_earning, ref_log, partner_earning])
        await session.commit()
        p_id = payment.id

    canceled_yk = {
        "status": "canceled",
        "paid": False,
        "amount": {"value": "100.00", "currency": "RUB"},
        "metadata": {"user_id": "888222", "tariff_id": "401"},
    }
    monkeypatch.setattr("bot.services.yookassa_reconciliation.fetch_yookassa_payment", AsyncMock(return_value=canceled_yk))

    bot_mock = SimpleNamespace(send_message=AsyncMock())
    success, code = await verify_and_fulfill_yookassa_payment(bot_mock, "yk_canc_fin_1", source="reconciliation")

    assert success is False
    assert code == "payment_canceled_with_financial_effects_review_needed"

    async with db_session_factory() as session:
        p = await session.get(Payment, p_id)
        assert p.status == PaymentStatus.FAILED
        assert p.provisioning_failure_code == "payment_canceled_with_financial_effects_review_needed"

        # CRITICAL: Audit trail preserved - records are NOT deleted
        db_ref_earning = await session.scalar(select(ReferralEarning).where(ReferralEarning.payment_id == p_id))
        assert db_ref_earning is not None
        assert db_ref_earning.amount_rub == 10.0

        db_ref_log = await session.scalar(select(ReferralPaymentLog).where(ReferralPaymentLog.payment_id == p_id))
        assert db_ref_log is not None

        db_partner_earning = await session.scalar(select(PartnerEarning).where(PartnerEarning.payment_id == p_id))
        assert db_partner_earning is not None
        assert db_partner_earning.amount == 15.0

        # Balances untouched (no corrupt automated clawback)
        ref_u = await session.get(User, referrer.id)
        assert ref_u.balance_rub == 50.0

        part = await session.get(Partner, partner.id)
        assert part.partner_balance == 100.0

    # Problem payment scanner classifies this payment as requiring manual review
    from bot.services.problem_payments import ProblemCategory
    report = await problem_payments.build_problem_payments_report()
    canc_problem = next((item for item in report.items if item.item_id == p_id), None)
    assert canc_problem is not None
    assert canc_problem.category == ProblemCategory.FAILED_SUPPLIER
    assert canc_problem.fulfillment_state == "Требует ручной проверки"


async def test_robokassa_not_affected_by_financial_finalization_checkpoint(monkeypatch, db_session_factory):
    """Review #6 fix: Ensure Robokassa payments are not affected by YooKassa financial finalization checkpoint."""
    from bot import webhooks
    monkeypatch.setattr(webhooks, "async_session", db_session_factory)

    async with db_session_factory() as session:
        user = User(telegram_id=777111, username="robo_user")
        tariff = Tariff(id=501, label="T501", price_rub=200, days=30, is_active=True, tariff_type=TariffType.VPN)
        server = Server(name="srv_r", host="2.2.2.2", location="Amsterdam", is_active=True)
        session.add_all([user, tariff, server])
        await session.flush()

        payment = Payment(
            user_id=user.id,
            amount=20000,
            currency="RUB",
            method=PaymentMethod.ROBOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="robo_inv_123",
            tariff_id=tariff.id,
            platform="android",
        )
        session.add(payment)
        await session.commit()
        p_id = payment.id

    sub_mock = SimpleNamespace(id=999, expires_at=datetime.utcnow() + timedelta(days=30), vpn_key="vless://robo")
    monkeypatch.setattr(
        "bot.webhooks.create_or_extend_paid_access",
        AsyncMock(return_value=(sub_mock, "vless://robo")),
    )
    monkeypatch.setattr(
        "bot.services.subscription_service.create_or_extend_paid_access",
        AsyncMock(return_value=(sub_mock, "vless://robo")),
    )

    bot_mock = SimpleNamespace(send_message=AsyncMock())
    u_id, s_id = await webhooks._process_and_deliver(
        bot_mock,
        telegram_user_id=777111,
        chat_id=777111,
        tariff_id=501,
        platform_str="android",
        provisioning_payment_id=p_id,
        paid_amount_rub=200.0,
    )

    assert u_id is not None
    assert s_id == 999

    async with db_session_factory() as session:
        p = await session.get(Payment, p_id)
        # Checkpoint failure code must NOT be set on Robokassa payment
        assert p.provisioning_failure_code is None


async def test_classify_and_verify_candidate_is_strictly_read_only_on_canceled(monkeypatch, db_session_factory):
    """Verify that inspecting a canceled payment or topup does not mutate the DB."""
    from bot.services.problem_payments import classify_and_verify_candidate
    from bot.keyboards.admin import problem_payments_list_kb
    from bot.services.problem_payments import ProblemCategory, ProblemPaymentItem

    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)
    monkeypatch.setattr(
        "bot.services.problem_payments.fetch_yookassa_payment",
        AsyncMock(return_value={"status": "canceled", "paid": False}),
    )

    async with db_session_factory() as session:
        user = User(telegram_id=987654, username="test_ro")
        session.add(user)
        await session.flush()

        payment = Payment(
            user_id=user.id,
            amount=50000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_canceled_123",
        )
        topup = BalanceTopUp(
            telegram_id=user.telegram_id,
            amount_rub=250.0,
            provider="yookassa",
            provider_payment_id="yk_canceled_topup_123",
            status="pending",
        )
        session.add_all([payment, topup])
        await session.commit()
        pay_id = payment.id
        topup_id = topup.id

    # Classify payment candidate
    cand_p = {"type": "payment", "obj": payment, "user": user}
    res_p = await classify_and_verify_candidate(cand_p)
    assert res_p is None

    # Classify topup candidate
    cand_t = {"type": "topup", "obj": topup, "user": user}
    res_t = await classify_and_verify_candidate(cand_t)
    assert res_t is None

    # Assert records in DB were NOT mutated
    async with db_session_factory() as session:
        p_db = await session.get(Payment, pay_id)
        assert p_db.status == PaymentStatus.PENDING, "Payment status must remain PENDING (read-only inspection)"
        t_db = await session.get(BalanceTopUp, topup_id)
        assert t_db.status == "pending", "BalanceTopUp status must remain pending (read-only inspection)"


async def test_problem_payments_list_kb_badges():
    """Verify distinct urgency badges on inline keyboard buttons."""
    from bot.keyboards.admin import problem_payments_list_kb
    from bot.services.problem_payments import ProblemCategory, ProblemPaymentItem

    now = datetime.utcnow()
    items = [
        ProblemPaymentItem(
            item_type="payment",
            item_id=80,
            user_id=1,
            telegram_id=111,
            username="u1",
            full_name="User 1",
            amount_rub=325.0,
            created_at=now,
            age_str="10m",
            provider_payment_id="yk_1",
            local_status="PaymentStatus.PENDING",
            verified_provider_status="YooKassa: succeeded",
            category=ProblemCategory.CONFIRMED_UNFULFILLED,
            fulfillment_state="Не выдана",
            details="Urgent",
        ),
        ProblemPaymentItem(
            item_type="payment",
            item_id=81,
            user_id=2,
            telegram_id=222,
            username="u2",
            full_name="User 2",
            amount_rub=100.0,
            created_at=now,
            age_str="5m",
            provider_payment_id="yk_2",
            local_status="PaymentStatus.PENDING",
            verified_provider_status="YooKassa: pending",
            category=ProblemCategory.PROVIDER_PENDING,
            fulfillment_state="Ожидает оплаты",
            details="Pending",
        ),
        ProblemPaymentItem(
            item_type="payment",
            item_id=82,
            user_id=3,
            telegram_id=333,
            username="u3",
            full_name="User 3",
            amount_rub=500.0,
            created_at=now,
            age_str="1h",
            provider_payment_id="yk_3",
            local_status="PaymentStatus.CONFIRMED",
            verified_provider_status="YooKassa: succeeded",
            category=ProblemCategory.FAILED_SUPPLIER,
            fulfillment_state="Сбой поставщика",
            details="Supplier failed",
        ),
    ]

    kb = problem_payments_list_kb(items, page=1, total_pages=1)
    texts = [btn.text for row in kb.inline_keyboard for btn in row]

    assert any("🚨 ОПЛАЧЕН #80 · 325₽" in t for t in texts)
    assert any("⏳ Ожидает #81 · 100₽" in t for t in texts)
    assert any("⚙️ СБОЙ #82 · 500₽" in t for t in texts)


async def test_historical_payment_manual_fulfillment_detection(monkeypatch, db_session_factory):
    """Test that historical payments (like #80 and #106) are classified as MANUALLY_FULFILLED and fail closed."""
    from bot.services.problem_payments import classify_and_verify_candidate, ProblemCategory
    from bot.services.yookassa_reconciliation import verify_and_fulfill_yookassa_payment

    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr(
        "bot.services.problem_payments.fetch_yookassa_payment",
        AsyncMock(return_value={"status": "succeeded", "paid": True, "amount": {"value": "325.00", "currency": "RUB"}}),
    )
    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.fetch_yookassa_payment",
        AsyncMock(return_value={"status": "succeeded", "paid": True, "amount": {"value": "325.00", "currency": "RUB"}, "metadata": {"user_id": 1051518238, "tariff_id": 31, "platform": "android"}}),
    )

    now = datetime.utcnow()
    past_date = now - timedelta(days=140)

    async with db_session_factory() as session:
        user = User(telegram_id=1051518238, username="sergey_petin")
        server = Server(name="test_srv_hist", host="10.0.0.1", location="Frankfurt", is_active=True)
        session.add_all([user, server])
        await session.flush()

        # Simulates Payment #80: created 140 days ago, 90 days tariff, but user currently has active sub #179
        sub_80 = Subscription(
            user_id=user.id,
            server_id=server.id,
            client_name="adapt_sergey",
            tariff_months=3,
            tariff_days=90,
            vpn_key="https://darimiru.ru/sub/80",
            status=SubStatus.ACTIVE,
            platform=Platform.ANDROID,
            started_at=past_date - timedelta(days=6),
            expires_at=now + timedelta(days=35), # Active in the future
        )
        session.add(sub_80)
        await session.flush()

        payment_80 = Payment(
            user_id=user.id,
            amount=32500,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_hist_80",
            subscription_id=None,
            created_at=past_date,
        )
        session.add(payment_80)
        await session.commit()
        p80_id = payment_80.id
        u_id = user.id

    # 1. Classification check: without durable operation/baseline correlation, an unrelated historical sub is DELIVERY_UNCERTAIN
    cand = {"type": "payment", "obj": payment_80, "user": user}
    item = await classify_and_verify_candidate(cand, now=now)
    assert item is not None
    assert item.category == ProblemCategory.DELIVERY_UNCERTAIN
    assert "Архив: статус неясен" in item.fulfillment_state

    # 2. Automated reconciler execution must FAIL CLOSED without mutating supplier
    mock_process_deliver = AsyncMock()
    monkeypatch.setattr("bot.webhooks._process_and_deliver", mock_process_deliver)

    bot_mock = SimpleNamespace(send_message=AsyncMock())
    success, code = await verify_and_fulfill_yookassa_payment(
        bot_mock,
        "yk_hist_80",
        source="scheduler",
    )
    assert success is False
    assert code == "historical_payment_manual_review_required"
    mock_process_deliver.assert_not_called()

    # DB record must reflect failure code
    async with db_session_factory() as session:
        p_chk = await session.get(Payment, p80_id)
        assert p_chk.provisioning_failure_code == "historical_payment_manual_review_required"
        assert p_chk.status == PaymentStatus.PENDING


async def test_today_renewal_upgrade_manual_safety(monkeypatch, db_session_factory):
    """Test that renewals/upgrades already applied (like today's #306 and #307) link safely without re-mutation."""
    from bot.services.problem_payments import classify_and_verify_candidate, ProblemCategory
    from bot.services.yookassa_reconciliation import verify_and_fulfill_yookassa_payment

    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr(
        "bot.services.problem_payments.fetch_yookassa_payment",
        AsyncMock(return_value={"status": "succeeded", "paid": True, "amount": {"value": "374.00", "currency": "RUB"}}),
    )
    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.fetch_yookassa_payment",
        AsyncMock(return_value={"status": "succeeded", "paid": True, "amount": {"value": "374.00", "currency": "RUB"}, "metadata": {"user_id": 953085646, "tariff_id": 20, "platform": "deferred~u~323"}}),
    )

    now = datetime.utcnow()
    async with db_session_factory() as session:
        user = User(telegram_id=953085646, username="epsmsu")
        server = Server(name="test_srv_today", host="10.0.0.1", location="Frankfurt", is_active=True)
        session.add_all([user, server])
        await session.flush()

        baseline = now + timedelta(days=5)
        # Simulates Payment #306: target sub 323 already renewed to 2027-02-01
        sub_323 = Subscription(
            id=323,
            user_id=user.id,
            server_id=server.id,
            client_name="adapt_epsmsu",
            tariff_months=3,
            tariff_days=90,
            vpn_key="https://darimiru.ru/sub/323",
            status=SubStatus.ACTIVE,
            platform=Platform.IOS,
            started_at=now - timedelta(days=25),
            expires_at=baseline + timedelta(days=90),  # Extended full 90 days beyond baseline!
        )
        session.add(sub_323)
        await session.flush()

        # Payment unlinked in DB but target sub already extended beyond baseline
        payment_306 = Payment(
            user_id=user.id,
            amount=37400,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_today_306",
            subscription_id=None,
            platform="deferred~u~323",
            provisioning_baseline_expires_at=baseline,
            created_at=now - timedelta(hours=2),
        )
        session.add(payment_306)
        await session.commit()
        p306_id = payment_306.id

    # 1. Classification check: durable baseline evidence present -> VERIFIED_MANUALLY_FULFILLED
    cand = {"type": "payment", "obj": payment_306, "user": user}
    item = await classify_and_verify_candidate(cand, now=now)
    assert item is not None
    assert item.category == ProblemCategory.VERIFIED_MANUALLY_FULFILLED
    assert "подписка #323" in item.fulfillment_state

    # 2. Automated reconciler execution must link without duplicate supplier mutation
    mock_process_deliver = AsyncMock()
    monkeypatch.setattr("bot.webhooks._process_and_deliver", mock_process_deliver)

    bot_mock = SimpleNamespace(send_message=AsyncMock())
    success, code = await verify_and_fulfill_yookassa_payment(
        bot_mock,
        "yk_today_306",
        source="scheduler",
    )
    assert success is True
    assert code == "already_manually_or_previously_fulfilled"
    mock_process_deliver.assert_not_called()

    # DB record must now be COMPLETED and linked to 323
    async with db_session_factory() as session:
        p_chk = await session.get(Payment, p306_id)
        assert p_chk.status == PaymentStatus.COMPLETED
        assert p_chk.subscription_id == 323


# ── Explicit New Safety Scenarios for Review ──────────────────


async def test_old_active_sub_but_payment_not_actually_fulfilled(monkeypatch, db_session_factory):
    """If an old renewal payment lacks evidence of being applied, reconciler fails closed and never marks COMPLETED."""
    from bot.services.problem_payments import classify_and_verify_candidate, ProblemCategory
    from bot.services.yookassa_reconciliation import verify_and_fulfill_yookassa_payment

    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr(
        "bot.services.problem_payments.fetch_yookassa_payment",
        AsyncMock(return_value={"status": "succeeded", "paid": True, "amount": {"value": "200.00", "currency": "RUB"}}),
    )
    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.fetch_yookassa_payment",
        AsyncMock(return_value={"status": "succeeded", "paid": True, "amount": {"value": "200.00", "currency": "RUB"}, "metadata": {"user_id": 771122, "tariff_id": 10, "platform": "deferred~u~400"}}),
    )

    now = datetime.utcnow()
    async with db_session_factory() as session:
        user = User(telegram_id=771122, username="user_unfulfilled_renewal")
        server = Server(name="test_srv_unfulfilled", host="10.0.0.1", location="Frankfurt", is_active=True)
        session.add_all([user, server])
        await session.flush()

        # Target sub 400 is active until now+10d, but baseline was now+10d (NEVER extended!)
        sub_400 = Subscription(
            id=400,
            user_id=user.id,
            server_id=server.id,
            client_name="adapt_400",
            tariff_months=1,
            tariff_days=30,
            vpn_key="https://darimiru.ru/sub/400",
            status=SubStatus.ACTIVE,
            platform=Platform.IOS,
            started_at=now - timedelta(days=20),
            expires_at=now + timedelta(days=10),
        )
        session.add(sub_400)
        await session.flush()

        # Payment older than 24h, baseline equals current expiration (not extended!)
        p_old_renewal = Payment(
            user_id=user.id,
            amount=20000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_unfulfilled_renewal",
            subscription_id=None,
            platform="deferred~u~400",
            provisioning_baseline_expires_at=now + timedelta(days=10),
            created_at=now - timedelta(hours=30),  # > 24 hours old
        )
        session.add(p_old_renewal)
        await session.commit()
        p_id = p_old_renewal.id

    # 1. Diagnostics classification: DELIVERY_UNCERTAIN (not manually fulfilled)
    cand = {"type": "payment", "obj": p_old_renewal, "user": user}
    item = await classify_and_verify_candidate(cand, now=now)
    assert item is not None
    assert item.category == ProblemCategory.DELIVERY_UNCERTAIN
    assert "Статус неясен" in item.fulfillment_state

    # 2. Automated reconciler execution must FAIL CLOSED
    mock_deliver = AsyncMock()
    monkeypatch.setattr("bot.webhooks._process_and_deliver", mock_deliver)
    bot_mock = SimpleNamespace(send_message=AsyncMock())

    success, code = await verify_and_fulfill_yookassa_payment(
        bot_mock,
        "yk_unfulfilled_renewal",
        source="scheduler",
    )
    assert success is False
    assert code == "renewal_evidence_missing_manual_review_required"
    mock_deliver.assert_not_called()

    # DB record must NOT be COMPLETED
    async with db_session_factory() as session:
        chk = await session.get(Payment, p_id)
        assert chk.status == PaymentStatus.PENDING
        assert chk.provisioning_failure_code == "renewal_evidence_missing_manual_review_required"


async def test_target_subscription_belonging_to_different_user(monkeypatch, db_session_factory):
    """Payment cannot link or fulfill against a subscription owned by a different user."""
    from bot.services.problem_payments import classify_and_verify_candidate, ProblemCategory
    from bot.services.yookassa_reconciliation import verify_and_fulfill_yookassa_payment

    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr(
        "bot.services.problem_payments.fetch_yookassa_payment",
        AsyncMock(return_value={"status": "succeeded", "paid": True, "amount": {"value": "200.00", "currency": "RUB"}}),
    )
    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.fetch_yookassa_payment",
        AsyncMock(return_value={"status": "succeeded", "paid": True, "amount": {"value": "200.00", "currency": "RUB"}, "metadata": {"user_id": 1111, "tariff_id": 10, "platform": "deferred~u~500"}}),
    )

    now = datetime.utcnow()
    async with db_session_factory() as session:
        user_a = User(id=1, telegram_id=1111, username="user_a")
        user_b = User(id=2, telegram_id=2222, username="user_b")
        server = Server(name="test_srv_mismatch", host="10.0.0.1", location="Frankfurt", is_active=True)
        session.add_all([user_a, user_b, server])
        await session.flush()

        # Subscription 500 belongs to User B!
        sub_500 = Subscription(
            id=500,
            user_id=user_b.id,
            server_id=server.id,
            client_name="adapt_user_b",
            tariff_months=1,
            tariff_days=30,
            vpn_key="https://darimiru.ru/sub/500",
            status=SubStatus.ACTIVE,
            platform=Platform.IOS,
            started_at=now - timedelta(days=5),
            expires_at=now + timedelta(days=25),
        )
        session.add(sub_500)
        await session.flush()

        # Payment made by User A targeting sub 500!
        p_mismatch = Payment(
            user_id=user_a.id,
            amount=20000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_user_mismatch",
            subscription_id=None,
            platform="deferred~u~500",
            created_at=now - timedelta(hours=1),
        )
        session.add(p_mismatch)
        await session.commit()
        p_id = p_mismatch.id

    # 1. Diagnostics classification: DELIVERY_UNCERTAIN with owner mismatch explanation
    cand = {"type": "payment", "obj": p_mismatch, "user": user_a}
    item = await classify_and_verify_candidate(cand, now=now)
    assert item is not None
    assert item.category == ProblemCategory.DELIVERY_UNCERTAIN
    assert "Несовпадение владельца" in item.fulfillment_state

    # 2. Automated reconciler execution must fail with user mismatch
    mock_deliver = AsyncMock()
    monkeypatch.setattr("bot.webhooks._process_and_deliver", mock_deliver)
    bot_mock = SimpleNamespace(send_message=AsyncMock())

    success, code = await verify_and_fulfill_yookassa_payment(
        bot_mock,
        "yk_user_mismatch",
        source="scheduler",
    )
    assert success is False
    assert code == "target_sub_user_mismatch"
    mock_deliver.assert_not_called()

    async with db_session_factory() as session:
        chk = await session.get(Payment, p_id)
        assert chk.status == PaymentStatus.PENDING
        assert chk.provisioning_failure_code == "target_sub_user_mismatch"


async def test_unrelated_historical_subscription(monkeypatch, db_session_factory):
    """Historical payment must not be classified as manually fulfilled merely because customer has an unrelated active sub."""
    from bot.services.problem_payments import classify_and_verify_candidate, ProblemCategory

    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)
    monkeypatch.setattr(
        "bot.services.problem_payments.fetch_yookassa_payment",
        AsyncMock(return_value={"status": "succeeded", "paid": True, "amount": {"value": "300.00", "currency": "RUB"}}),
    )

    now = datetime.utcnow()
    past_100d = now - timedelta(days=100)

    async with db_session_factory() as session:
        user = User(telegram_id=883311, username="historical_unrelated")
        server = Server(name="test_srv_unrelated", host="10.0.0.1", location="Frankfurt", is_active=True)
        session.add_all([user, server])
        await session.flush()

        # Unrelated subscription created 20 days ago (80 days AFTER the historical payment)
        sub_recent = Subscription(
            user_id=user.id,
            server_id=server.id,
            client_name="adapt_unrelated",
            tariff_months=1,
            tariff_days=30,
            vpn_key="https://darimiru.ru/sub/unrelated",
            status=SubStatus.ACTIVE,
            platform=Platform.ANDROID,
            started_at=now - timedelta(days=20),
            expires_at=now + timedelta(days=10),
        )
        session.add(sub_recent)
        await session.flush()

        # Payment from 100 days ago
        p_hist = Payment(
            user_id=user.id,
            amount=30000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_hist_unrelated",
            subscription_id=None,
            created_at=past_100d,
        )
        session.add(p_hist)
        await session.commit()

    cand = {"type": "payment", "obj": p_hist, "user": user}
    item = await classify_and_verify_candidate(cand, now=now)
    assert item is not None
    # Must NOT be classified as VERIFIED_MANUALLY_FULFILLED
    assert item.category == ProblemCategory.DELIVERY_UNCERTAIN
    assert "Архив: статус неясен" in item.fulfillment_state


async def test_ambiguous_payment_with_forged_or_stale_callback(monkeypatch, db_session_factory):
    """Reconciliation actions for ambiguous/uncertain records are blocked at execution time."""
    from bot.handlers.admin import admin_problem_payment_reconcile, admin_problem_payment_reconcile_confirm
    from bot.models import Payment, User, PaymentStatus, PaymentMethod

    monkeypatch.setattr("bot.handlers.admin.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)
    monkeypatch.setattr(
        "bot.services.problem_payments.fetch_yookassa_payment",
        AsyncMock(return_value={"status": "succeeded", "paid": True, "amount": {"value": "150.00", "currency": "RUB"}}),
    )
    monkeypatch.setattr("bot.handlers.admin._is_admin", lambda uid: True)

    now = datetime.utcnow()
    async with db_session_factory() as session:
        user = User(telegram_id=994411, username="test_ambiguous_admin")
        session.add(user)
        await session.flush()

        # Historical payment with no durable correlation -> DELIVERY_UNCERTAIN
        payment = Payment(
            user_id=user.id,
            amount=15000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_ambiguous_stale",
            subscription_id=None,
            created_at=now - timedelta(days=20),
        )
        session.add(payment)
        await session.commit()
        pay_id = payment.id

    # 1. Attempt initial reconcile prompt
    mock_cb_prompt = SimpleNamespace(
        from_user=SimpleNamespace(id=12345),
        data=f"adm_prob_reconcile:p:{pay_id}:1",
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
    )
    await admin_problem_payment_reconcile(mock_cb_prompt)
    mock_cb_prompt.answer.assert_called_once()
    assert "Автоматическая выдача заблокирована" in mock_cb_prompt.answer.call_args[0][0]

    # 2. Attempt direct confirmed reconciliation (bypassing UI)
    mock_cb_confirm = SimpleNamespace(
        from_user=SimpleNamespace(id=12345),
        data=f"adm_prob_rec_confirm:p:{pay_id}:1",
        answer=AsyncMock(),
        message=SimpleNamespace(answer=AsyncMock()),
        bot=SimpleNamespace(send_message=AsyncMock()),
    )
    mock_reconcile_call = AsyncMock()
    monkeypatch.setattr("bot.services.yookassa_reconciliation.verify_and_fulfill_yookassa_payment", mock_reconcile_call)

    await admin_problem_payment_reconcile_confirm(mock_cb_confirm)
    mock_cb_confirm.answer.assert_called_once()
    assert "Статус платежа неясен" in mock_cb_confirm.answer.call_args[0][0]
    mock_reconcile_call.assert_not_called()


async def test_failed_telegram_delivery_after_successful_provisioning(monkeypatch, db_session_factory):
    """When Telegram delivery fails after provisioning, delivery error is persisted, surfaced, and retriable without re-mutating supplier."""
    from bot.webhooks import send_subscription_delivery_messages
    from bot.services.problem_payments import classify_and_verify_candidate, ProblemCategory
    from bot.keyboards.admin import problem_payment_detail_kb
    from bot.handlers.admin import admin_problem_payment_retry_tg

    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.handlers.payment.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)
    monkeypatch.setattr("bot.handlers.admin.async_session", db_session_factory)
    monkeypatch.setattr("bot.handlers.admin._is_admin", lambda uid: True)

    now = datetime.utcnow()
    async with db_session_factory() as session:
        user = User(telegram_id=554433, username="tg_delivery_fail_user")
        server = Server(name="test_srv_tg", host="10.0.0.1", location="Frankfurt", is_active=True)
        session.add_all([user, server])
        await session.flush()

        sub = Subscription(
            user_id=user.id,
            server_id=server.id,
            client_name="adapt_tg_fail",
            tariff_months=1,
            tariff_days=30,
            vpn_key="https://darimiru.ru/sub/tg_fail",
            status=SubStatus.ACTIVE,
            platform=Platform.ANDROID,
            started_at=now,
            expires_at=now + timedelta(days=30),
        )
        session.add(sub)
        await session.flush()

        payment = Payment(
            user_id=user.id,
            amount=15500,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            provider_payment_id="yk_tg_fail_1",
            subscription_id=sub.id,
            platform="android",
            created_at=now - timedelta(minutes=5),
        )
        session.add(payment)
        await session.commit()
        pay_id = payment.id
        sub_id = sub.id

    # 1. Simulate send_subscription_delivery_messages failure
    bot_failing = SimpleNamespace(
        send_message=AsyncMock(side_effect=Exception("TelegramNetworkTimeout"))
    )
    success, err = await send_subscription_delivery_messages(
        bot=bot_failing,
        chat_id=554433,
        telegram_user_id=554433,
        subscription_id=sub_id,
        delivery_platform=Platform.ANDROID,
        vpn_key_str="https://darimiru.ru/sub/tg_fail",
        proxy_link=None,
        expires_str="01.12.2026",
        needs_platform_choice=False,
        payment_id=pay_id,
    )
    assert success is False
    assert "TelegramNetworkTimeout" in str(err)

    # 2. Verify durable persistence on Payment
    async with db_session_factory() as session:
        p_db = await session.get(Payment, pay_id)
        assert p_db.telegram_delivery_error is not None
        assert "TelegramNetworkTimeout" in p_db.telegram_delivery_error

    # 3. Problem payments diagnosis surfaces TELEGRAM_DELIVERY_FAILED
    cand = {"type": "payment", "obj": p_db, "user": user}
    diag = await classify_and_verify_candidate(cand, now=now)
    assert diag is not None
    assert diag.category == ProblemCategory.TELEGRAM_DELIVERY_FAILED
    assert "сбой отправки в TG" in diag.fulfillment_state

    # 4. Detail keyboard offers retry button without re-provisioning
    kb = problem_payment_detail_kb(
        item_type="payment",
        item_id=pay_id,
        user_id=user.id,
        provider_payment_id="yk_tg_fail_1",
        category=ProblemCategory.TELEGRAM_DELIVERY_FAILED,
    )
    all_buttons = [b for row in kb.inline_keyboard for b in row]
    retry_btn = next((b for b in all_buttons if "adm_prob_retry_tg:" in (b.callback_data or "")), None)
    assert retry_btn is not None
    assert "Отправить доступ клиенту в TG" in retry_btn.text

    # 5. Admin executes retry with healthy bot: resends existing info and clears failure marker
    bot_healthy = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=999))
    )
    mock_cb_retry = SimpleNamespace(
        from_user=SimpleNamespace(id=12345),
        data=f"adm_prob_retry_tg:p:{pay_id}:1",
        answer=AsyncMock(),
        bot=bot_healthy,
        message=SimpleNamespace(edit_text=AsyncMock()),
    )
    monkeypatch.setattr("bot.handlers.admin.admin_problem_payment_inspect", AsyncMock())
    await admin_problem_payment_retry_tg(mock_cb_retry)

    mock_cb_retry.answer.assert_called()
    assert "успешно отправлен" in mock_cb_retry.answer.call_args[0][0]

    # Verify telegram_delivery_error is cleared
    async with db_session_factory() as session:
        p_cleared = await session.get(Payment, pay_id)
        assert p_cleared.telegram_delivery_error is None
        assert p_cleared.subscription_id == sub_id
        # Sub dates must be completely unchanged
        s_chk = await session.get(Subscription, sub_id)
        assert s_chk.expires_at == now + timedelta(days=30)


async def test_no_duplicate_supplier_operation_payment_accounting_or_customer_notification(monkeypatch, db_session_factory):
    """Already completed/fulfilled payment rejects duplicate supplier mutation, payment accounting, and customer notifications."""
    from bot.services.yookassa_reconciliation import verify_and_fulfill_yookassa_payment
    from bot.models import BalanceTransaction, ReferralEarning

    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.fetch_yookassa_payment",
        AsyncMock(return_value={"status": "succeeded", "paid": True, "amount": {"value": "200.00", "currency": "RUB"}, "metadata": {"user_id": 667788, "tariff_id": 10, "platform": "android"}}),
    )

    now = datetime.utcnow()
    async with db_session_factory() as session:
        user = User(telegram_id=667788, username="user_idempotent")
        server = Server(name="test_srv_idem", host="10.0.0.1", location="Frankfurt", is_active=True)
        session.add_all([user, server])
        await session.flush()

        sub = Subscription(
            user_id=user.id,
            server_id=server.id,
            client_name="adapt_idem",
            tariff_months=1,
            tariff_days=30,
            vpn_key="https://darimiru.ru/sub/idem",
            status=SubStatus.ACTIVE,
            platform=Platform.ANDROID,
            started_at=now - timedelta(days=2),
            expires_at=now + timedelta(days=28),
        )
        session.add(sub)
        await session.flush()

        payment = Payment(
            user_id=user.id,
            amount=20000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            provider_payment_id="yk_idem_check",
            subscription_id=sub.id,
            platform="android",
            created_at=now - timedelta(days=2),
        )
        session.add(payment)
        await session.commit()

    mock_deliver = AsyncMock()
    monkeypatch.setattr("bot.webhooks._process_and_deliver", mock_deliver)
    mock_send_msg = AsyncMock()
    bot_mock = SimpleNamespace(send_message=mock_send_msg)

    # Reconciliation on already completed record
    success, code = await verify_and_fulfill_yookassa_payment(
        bot_mock,
        "yk_idem_check",
        source="scheduler",
    )
    assert success is True
    assert code == "already_completed"
    mock_deliver.assert_not_called()
    mock_send_msg.assert_not_called()

    # Verify no duplicate accounting entries were created
    async with db_session_factory() as session:
        tx_count = await session.scalar(select(func.count(BalanceTransaction.id)))
        ref_count = await session.scalar(select(func.count(ReferralEarning.id)))
        assert tx_count == 0
        assert ref_count == 0


# ── Strict Evidence & Telegram Retry Edge Case Tests ──────────


async def test_one_day_extension_vs_paid_90_day_renewal(monkeypatch, db_session_factory):
    """A 1-day extension must NOT qualify as manual fulfillment for a paid 90-day renewal."""
    from bot.services.problem_payments import classify_and_verify_candidate, ProblemCategory
    from bot.services.yookassa_reconciliation import verify_and_fulfill_yookassa_payment

    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr(
        "bot.services.problem_payments.fetch_yookassa_payment",
        AsyncMock(return_value={"status": "succeeded", "paid": True, "amount": {"value": "900.00", "currency": "RUB"}}),
    )
    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.fetch_yookassa_payment",
        AsyncMock(return_value={
            "status": "succeeded", "paid": True,
            "amount": {"value": "900.00", "currency": "RUB"},
            "metadata": {"user_id": 999111, "tariff_id": 90, "platform": "deferred~u~501"},
        }),
    )

    now = datetime.utcnow()
    async with db_session_factory() as session:
        user = User(telegram_id=999111, username="test_1d_90d")
        server = Server(name="srv_1d", host="10.0.0.1", location="Frankfurt", is_active=True)
        tariff_90 = Tariff(id=90, days=90, label="3 месяца", price_rub=900)
        session.add_all([user, server, tariff_90])
        await session.flush()

        # Target sub was only extended by 1 day beyond baseline
        baseline = now + timedelta(days=2)
        sub = Subscription(
            id=501,
            user_id=user.id,
            server_id=server.id,
            client_name="adapt_1d",
            tariff_months=3,
            tariff_days=90,
            vpn_key="https://darimiru.ru/sub/501",
            status=SubStatus.ACTIVE,
            platform=Platform.IOS,
            started_at=now - timedelta(days=30),
            expires_at=baseline + timedelta(days=1),  # Only 1 day!
        )
        session.add(sub)
        await session.flush()

        payment = Payment(
            user_id=user.id,
            amount=90000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_1d_vs_90d",
            subscription_id=None,
            platform="deferred~u~501",
            tariff_id=90,
            provisioning_baseline_expires_at=baseline,
            created_at=now - timedelta(hours=30),  # Historical renewal
        )
        session.add(payment)
        await session.commit()
        p_id = payment.id

    # 1. Classification: 1-day extension for 90d purchase must be DELIVERY_UNCERTAIN
    cand = {"type": "payment", "obj": payment, "user": user}
    item = await classify_and_verify_candidate(cand, now=now)
    assert item is not None
    assert item.category == ProblemCategory.DELIVERY_UNCERTAIN
    assert "Статус неясен" in item.fulfillment_state

    # 2. Automated reconciler execution must fail closed without supplier mutation
    mock_deliver = AsyncMock()
    monkeypatch.setattr("bot.webhooks._process_and_deliver", mock_deliver)
    bot_mock = SimpleNamespace(send_message=AsyncMock())

    success, code = await verify_and_fulfill_yookassa_payment(bot_mock, "yk_1d_vs_90d", source="scheduler")
    assert success is False
    assert code == "renewal_evidence_missing_manual_review_required"
    mock_deliver.assert_not_called()

    async with db_session_factory() as session:
        p_chk = await session.get(Payment, p_id)
        assert p_chk.status == PaymentStatus.PENDING
        assert p_chk.provisioning_failure_code == "renewal_evidence_missing_manual_review_required"


async def test_admin_action_for_same_user_different_subscription(monkeypatch, db_session_factory):
    """An admin action targeting a DIFFERENT subscription must NOT fulfill this payment."""
    from bot.services.problem_payments import classify_and_verify_candidate, ProblemCategory
    from bot.services.yookassa_reconciliation import verify_and_fulfill_yookassa_payment

    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr(
        "bot.services.problem_payments.fetch_yookassa_payment",
        AsyncMock(return_value={"status": "succeeded", "paid": True, "amount": {"value": "350.00", "currency": "RUB"}}),
    )
    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.fetch_yookassa_payment",
        AsyncMock(return_value={
            "status": "succeeded", "paid": True,
            "amount": {"value": "350.00", "currency": "RUB"},
            "metadata": {"user_id": 999222, "tariff_id": 30, "platform": "deferred~u~601"},
        }),
    )

    now = datetime.utcnow()
    async with db_session_factory() as session:
        user = User(telegram_id=999222, username="test_diff_sub")
        server = Server(name="srv_diff", host="10.0.0.1", location="Frankfurt", is_active=True)
        tariff_30 = Tariff(id=30, days=30, label="1 месяц", price_rub=350)
        session.add_all([user, server, tariff_30])
        await session.flush()

        # Intended target sub 601 (expired)
        sub_601 = Subscription(
            id=601,
            user_id=user.id,
            server_id=server.id,
            client_name="adapt_601",
            tariff_months=1,
            tariff_days=30,
            vpn_key="https://darimiru.ru/sub/601",
            status=SubStatus.EXPIRED,
            platform=Platform.ANDROID,
            started_at=now - timedelta(days=60),
            expires_at=now - timedelta(days=5),
        )
        # Unrelated sub 602 for same user
        sub_602 = Subscription(
            id=602,
            user_id=user.id,
            server_id=server.id,
            client_name="adapt_602",
            tariff_months=1,
            tariff_days=30,
            vpn_key="https://darimiru.ru/sub/602",
            status=SubStatus.ACTIVE,
            platform=Platform.ANDROID,
            started_at=now - timedelta(days=10),
            expires_at=now + timedelta(days=20),
        )
        session.add_all([sub_601, sub_602])
        await session.flush()

        # Admin renewed sub 602, NOT 601
        admin_action = AdminActionLog(
            actor_telegram_id=111,
            action="renew_subscription",
            entity_type="subscription",
            entity_id="602",  # Targets 602!
            target_telegram_id=user.telegram_id,
            status="success",
            summary="Renewed sub 602",
            created_at=now - timedelta(hours=1),
        )
        session.add(admin_action)

        payment = Payment(
            user_id=user.id,
            amount=35000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_diff_sub_payment",
            subscription_id=None,
            platform="deferred~u~601",  # Intended sub 601
            tariff_id=30,
            created_at=now - timedelta(hours=28),  # Historical
        )
        session.add(payment)
        await session.commit()
        p_id = payment.id

    cand = {"type": "payment", "obj": payment, "user": user}
    item = await classify_and_verify_candidate(cand, now=now)
    assert item is not None
    # Because sub 601 is expired and action was on 602, it must NOT be classified as VERIFIED_MANUALLY_FULFILLED
    assert item.category in (ProblemCategory.CONFIRMED_UNFULFILLED, ProblemCategory.DELIVERY_UNCERTAIN)
    assert item.category != ProblemCategory.VERIFIED_MANUALLY_FULFILLED

    mock_deliver = AsyncMock()
    monkeypatch.setattr("bot.webhooks._process_and_deliver", mock_deliver)
    bot_mock = SimpleNamespace(send_message=AsyncMock())

    success, code = await verify_and_fulfill_yookassa_payment(bot_mock, "yk_diff_sub_payment", source="scheduler")
    assert success is False
    assert code in ("renewal_evidence_missing_manual_review_required", "historical_payment_manual_review_required")
    mock_deliver.assert_not_called()


async def test_unrelated_later_manual_renewal(monkeypatch, db_session_factory):
    """A manual renewal occurring long after the payment must NOT correlate with it."""
    from bot.services.problem_payments import classify_and_verify_candidate, ProblemCategory
    from bot.services.yookassa_reconciliation import verify_and_fulfill_yookassa_payment

    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr(
        "bot.services.problem_payments.fetch_yookassa_payment",
        AsyncMock(return_value={"status": "succeeded", "paid": True, "amount": {"value": "350.00", "currency": "RUB"}}),
    )
    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.fetch_yookassa_payment",
        AsyncMock(return_value={
            "status": "succeeded", "paid": True,
            "amount": {"value": "350.00", "currency": "RUB"},
            "metadata": {"user_id": 999333, "tariff_id": 30, "platform": "deferred~u~701"},
        }),
    )

    now = datetime.utcnow()
    p_created = now - timedelta(days=60)  # Payment in June / 60 days ago
    async with db_session_factory() as session:
        user = User(telegram_id=999333, username="test_later_renew")
        server = Server(name="srv_later", host="10.0.0.1", location="Frankfurt", is_active=True)
        tariff_30 = Tariff(id=30, days=30, label="1 месяц", price_rub=350)
        session.add_all([user, server, tariff_30])
        await session.flush()

        sub = Subscription(
            id=701,
            user_id=user.id,
            server_id=server.id,
            client_name="adapt_701",
            tariff_months=1,
            tariff_days=30,
            vpn_key="https://darimiru.ru/sub/701",
            status=SubStatus.ACTIVE,
            platform=Platform.ANDROID,
            started_at=p_created,
            expires_at=now + timedelta(days=15),  # Active currently due to a much later renewal
        )
        session.add(sub)
        await session.flush()

        # Manual operation performed 55 days after payment (unrelated later renewal)
        op_later = ManualSubscriptionOperation(
            admin_user_id=111,
            target_user_id=user.id,
            subscription_id=sub.id,
            tariff_id=30,
            action="renew",
            status="completed",
            created_at=now - timedelta(days=5),
            completed_at=now - timedelta(days=5),
        )
        session.add(op_later)

        payment = Payment(
            user_id=user.id,
            amount=35000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_later_renew_payment",
            subscription_id=None,
            platform="deferred~u~701",
            tariff_id=30,
            created_at=p_created,
        )
        session.add(payment)
        await session.commit()

    cand = {"type": "payment", "obj": payment, "user": user}
    item = await classify_and_verify_candidate(cand, now=now)
    assert item is not None
    assert item.category == ProblemCategory.DELIVERY_UNCERTAIN

    mock_deliver = AsyncMock()
    monkeypatch.setattr("bot.webhooks._process_and_deliver", mock_deliver)
    bot_mock = SimpleNamespace(send_message=AsyncMock())

    success, code = await verify_and_fulfill_yookassa_payment(bot_mock, "yk_later_renew_payment", source="scheduler")
    assert success is False
    assert code == "renewal_evidence_missing_manual_review_required"
    mock_deliver.assert_not_called()


async def test_genuine_manually_fulfilled_renewal_with_strong_evidence(monkeypatch, db_session_factory):
    """A renewal with contemporaneous manual operation, matching tariff, and extended duration is verified."""
    from bot.services.problem_payments import classify_and_verify_candidate, ProblemCategory
    from bot.services.yookassa_reconciliation import verify_and_fulfill_yookassa_payment

    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr(
        "bot.services.problem_payments.fetch_yookassa_payment",
        AsyncMock(return_value={"status": "succeeded", "paid": True, "amount": {"value": "900.00", "currency": "RUB"}}),
    )
    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.fetch_yookassa_payment",
        AsyncMock(return_value={
            "status": "succeeded", "paid": True,
            "amount": {"value": "900.00", "currency": "RUB"},
            "metadata": {"user_id": 999444, "tariff_id": 90, "platform": "deferred~u~801"},
        }),
    )

    now = datetime.utcnow()
    p_created = now - timedelta(hours=3)
    baseline = p_created + timedelta(days=10)
    async with db_session_factory() as session:
        user = User(telegram_id=999444, username="test_strong_proof")
        server = Server(name="srv_strong", host="10.0.0.1", location="Frankfurt", is_active=True)
        tariff_90 = Tariff(id=90, days=90, label="3 месяца", price_rub=900)
        session.add_all([user, server, tariff_90])
        await session.flush()

        sub = Subscription(
            id=801,
            user_id=user.id,
            server_id=server.id,
            client_name="adapt_801",
            tariff_months=3,
            tariff_days=90,
            vpn_key="https://darimiru.ru/sub/801",
            status=SubStatus.ACTIVE,
            platform=Platform.IOS,
            started_at=p_created - timedelta(days=20),
            expires_at=baseline + timedelta(days=90),  # Fully extended 90 days
        )
        session.add(sub)
        await session.flush()

        # Contemporaneous manual operation within 30 minutes of payment
        manual_op = ManualSubscriptionOperation(
            admin_user_id=111,
            target_user_id=user.id,
            subscription_id=sub.id,
            tariff_id=90,
            action="renew",
            status="completed",
            created_at=p_created + timedelta(minutes=30),
            completed_at=p_created + timedelta(minutes=31),
        )
        session.add(manual_op)

        payment = Payment(
            user_id=user.id,
            amount=90000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_strong_proof",
            subscription_id=None,
            platform="deferred~u~801",
            tariff_id=90,
            provisioning_baseline_expires_at=baseline,
            created_at=p_created,
        )
        session.add(payment)
        await session.commit()
        p_id = payment.id

    cand = {"type": "payment", "obj": payment, "user": user}
    item = await classify_and_verify_candidate(cand, now=now)
    assert item is not None
    assert item.category == ProblemCategory.VERIFIED_MANUALLY_FULFILLED

    mock_deliver = AsyncMock()
    monkeypatch.setattr("bot.webhooks._process_and_deliver", mock_deliver)
    bot_mock = SimpleNamespace(send_message=AsyncMock())

    success, code = await verify_and_fulfill_yookassa_payment(bot_mock, "yk_strong_proof", source="scheduler")
    assert success is True
    assert code == "already_manually_or_previously_fulfilled"
    mock_deliver.assert_not_called()

    async with db_session_factory() as session:
        p_chk = await session.get(Payment, p_id)
        assert p_chk.status == PaymentStatus.COMPLETED
        assert p_chk.subscription_id == 801


# ── Telegram Delivery Retry Safety Tests ───────────────────────


async def test_telegram_retry_execution_checks(monkeypatch, db_session_factory):
    """Test admin_problem_payment_retry_tg execution checks: COMPLETED status, no error, user mismatch."""
    from bot.handlers.admin import admin_problem_payment_retry_tg

    monkeypatch.setattr("bot.handlers.admin.async_session", db_session_factory)
    monkeypatch.setattr("bot.handlers.admin._is_admin", lambda uid: True)

    now = datetime.utcnow()
    async with db_session_factory() as session:
        user_a = User(id=1, telegram_id=111001, username="user_a")
        user_b = User(id=2, telegram_id=111002, username="user_b")
        server = Server(name="srv_retry", host="10.0.0.1", location="Frankfurt", is_active=True)
        session.add_all([user_a, user_b, server])
        await session.flush()

        sub_a = Subscription(
            id=901, user_id=user_a.id, server_id=server.id, client_name="sub_a",
            tariff_months=1, tariff_days=30, vpn_key="vless://sub_a", status=SubStatus.ACTIVE, platform=Platform.IOS,
            expires_at=now + timedelta(days=30),
        )
        sub_b = Subscription(
            id=902, user_id=user_b.id, server_id=server.id, client_name="sub_b",
            tariff_months=1, tariff_days=30, vpn_key="vless://sub_b", status=SubStatus.ACTIVE, platform=Platform.IOS,
            expires_at=now + timedelta(days=30),
        )
        session.add_all([sub_a, sub_b])
        await session.flush()

        # Payment 1: PENDING (not COMPLETED)
        p_pending = Payment(
            id=1001, user_id=user_a.id, amount=30000, currency="RUB", method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING, subscription_id=sub_a.id, telegram_delivery_error="err",
        )
        # Payment 2: COMPLETED but NO delivery error
        p_no_err = Payment(
            id=1002, user_id=user_a.id, amount=30000, currency="RUB", method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED, subscription_id=sub_a.id, telegram_delivery_error=None,
        )
        # Payment 3: COMPLETED, has error, but sub belongs to user B! (Security mismatch)
        p_mismatch = Payment(
            id=1003, user_id=user_a.id, amount=30000, currency="RUB", method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED, subscription_id=sub_b.id, telegram_delivery_error="err",
        )
        # Payment 4: In progress by concurrent admin
        p_in_prog = Payment(
            id=1004, user_id=user_a.id, amount=30000, currency="RUB", method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED, subscription_id=sub_a.id, telegram_delivery_error="delivery_in_progress",
        )
        session.add_all([p_pending, p_no_err, p_mismatch, p_in_prog])
        await session.commit()

    # 1. PENDING check
    cb_pending = SimpleNamespace(
        from_user=SimpleNamespace(id=111),
        data="adm_prob_retry_tg:p:1001:1",
        answer=AsyncMock(),
        bot=SimpleNamespace(send_message=AsyncMock()),
    )
    await admin_problem_payment_retry_tg(cb_pending)
    cb_pending.answer.assert_called_once()
    assert "не завершен" in cb_pending.answer.call_args[0][0]

    # 2. No delivery error check
    cb_no_err = SimpleNamespace(
        from_user=SimpleNamespace(id=111),
        data="adm_prob_retry_tg:p:1002:1",
        answer=AsyncMock(),
        bot=SimpleNamespace(send_message=AsyncMock()),
    )
    await admin_problem_payment_retry_tg(cb_no_err)
    cb_no_err.answer.assert_called_once()
    assert "уже успешно доставлен" in cb_no_err.answer.call_args[0][0]

    # 3. User mismatch security check
    cb_mismatch = SimpleNamespace(
        from_user=SimpleNamespace(id=111),
        data="adm_prob_retry_tg:p:1003:1",
        answer=AsyncMock(),
        bot=SimpleNamespace(send_message=AsyncMock()),
    )
    await admin_problem_payment_retry_tg(cb_mismatch)
    cb_mismatch.answer.assert_called_once()
    assert "другому пользователю" in cb_mismatch.answer.call_args[0][0]

    # 4. In progress concurrency check
    cb_in_prog = SimpleNamespace(
        from_user=SimpleNamespace(id=111),
        data="adm_prob_retry_tg:p:1004:1",
        answer=AsyncMock(),
        bot=SimpleNamespace(send_message=AsyncMock()),
    )
    await admin_problem_payment_retry_tg(cb_in_prog)
    cb_in_prog.answer.assert_called_once()
    assert "уже выполняется" in cb_in_prog.answer.call_args[0][0]


async def test_telegram_retry_distinguishes_proxy_only_failure(monkeypatch, db_session_factory):
    """When only the auxiliary proxy link failed, retry does NOT resend the VPN key / picker."""
    from bot.handlers.admin import admin_problem_payment_retry_tg

    monkeypatch.setattr("bot.handlers.admin.async_session", db_session_factory)
    monkeypatch.setattr("bot.handlers.payment.async_session", db_session_factory)
    monkeypatch.setattr("bot.handlers.admin._is_admin", lambda uid: True)

    now = datetime.utcnow()
    async with db_session_factory() as session:
        user = User(id=10, telegram_id=222333, username="proxy_only_user")
        server = Server(name="srv_po", host="10.0.0.1", location="Frankfurt", is_active=True)
        session.add_all([user, server])
        await session.flush()

        sub = Subscription(
            id=950, user_id=user.id, server_id=server.id, client_name="sub_po",
            tariff_months=1, tariff_days=30, vpn_key="vless://sub_po", status=SubStatus.ACTIVE, platform=Platform.IOS,
            expires_at=now + timedelta(days=30),
        )
        session.add(sub)
        await session.flush()

        # Payment with proxy-only delivery error
        payment = Payment(
            id=1050, user_id=user.id, amount=30000, currency="RUB", method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED, subscription_id=sub.id,
            telegram_delivery_error="proxy_link_failed: telegram network timeout",
            platform="ios",
        )
        session.add(payment)
        await session.commit()

    mock_send = AsyncMock()
    bot_mock = SimpleNamespace(send_message=mock_send)

    cb = SimpleNamespace(
        from_user=SimpleNamespace(id=111),
        data="adm_prob_retry_tg:p:1050:1",
        answer=AsyncMock(),
        bot=bot_mock,
        message=SimpleNamespace(edit_text=AsyncMock()),
    )

    # Monkeypatch send_subscription_delivery_messages to monitor resend_primary & resend_secondary
    from bot import webhooks
    orig_send = webhooks.send_subscription_delivery_messages
    recorded_kwargs = {}

    async def fake_send(**kwargs):
        recorded_kwargs.update(kwargs)
        return await orig_send(**kwargs)

    monkeypatch.setattr("bot.webhooks.send_subscription_delivery_messages", fake_send)
    monkeypatch.setattr("bot.handlers.admin.admin_problem_payment_inspect", AsyncMock())

    await admin_problem_payment_retry_tg(cb)

    # Confirm that resend_primary was False and resend_secondary was True
    assert recorded_kwargs.get("resend_primary") is False
    assert recorded_kwargs.get("resend_secondary") is True

    # Error must now be cleared in DB
    async with db_session_factory() as session:
        p_upd = await session.get(Payment, 1050)
        assert p_upd.telegram_delivery_error is None


async def test_partial_duration_renewal_rejected_at_80_percent(monkeypatch, db_session_factory):
    """An 80% (72-day) extension on a 90-day renewal tariff must be rejected and fail closed."""
    from bot.services.problem_payments import classify_and_verify_candidate, ProblemCategory
    from bot.services.yookassa_reconciliation import verify_and_fulfill_yookassa_payment

    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr(
        "bot.services.problem_payments.fetch_yookassa_payment",
        AsyncMock(return_value={"status": "succeeded", "paid": True, "amount": {"value": "900.00", "currency": "RUB"}}),
    )
    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.fetch_yookassa_payment",
        AsyncMock(return_value={
            "status": "succeeded", "paid": True,
            "amount": {"value": "900.00", "currency": "RUB"},
            "metadata": {"user_id": 999555, "tariff_id": 90, "platform": "deferred~u~850"},
        }),
    )

    now = datetime.utcnow()
    async with db_session_factory() as session:
        user = User(telegram_id=999555, username="test_partial_80")
        server = Server(name="srv_partial", host="10.0.0.1", location="Frankfurt", is_active=True)
        tariff_90 = Tariff(id=90, days=90, label="3 месяца", price_rub=900)
        session.add_all([user, server, tariff_90])
        await session.flush()

        baseline = now + timedelta(days=5)
        # Sub was extended by exactly 72 days (80% of 90), which previously passed but must now be rejected!
        sub = Subscription(
            id=850,
            user_id=user.id,
            server_id=server.id,
            client_name="adapt_850",
            tariff_months=3,
            tariff_days=90,
            vpn_key="https://darimiru.ru/sub/850",
            status=SubStatus.ACTIVE,
            platform=Platform.IOS,
            started_at=now - timedelta(days=25),
            expires_at=baseline + timedelta(days=72),
        )
        session.add(sub)
        await session.flush()

        payment = Payment(
            user_id=user.id,
            amount=90000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_partial_80_test",
            subscription_id=None,
            platform="deferred~u~850",
            tariff_id=90,
            provisioning_baseline_expires_at=baseline,
            created_at=now - timedelta(hours=30),
        )
        session.add(payment)
        await session.commit()
        p_id = payment.id

    cand = {"type": "payment", "obj": payment, "user": user}
    item = await classify_and_verify_candidate(cand, now=now)
    assert item is not None
    assert item.category == ProblemCategory.DELIVERY_UNCERTAIN
    assert "частичное продление" in item.details or "Статус неясен" in item.fulfillment_state

    mock_deliver = AsyncMock()
    monkeypatch.setattr("bot.webhooks._process_and_deliver", mock_deliver)
    bot_mock = SimpleNamespace(send_message=AsyncMock())

    success, code = await verify_and_fulfill_yookassa_payment(bot_mock, "yk_partial_80_test", source="scheduler")
    assert success is False
    assert code == "renewal_evidence_missing_manual_review_required"
    mock_deliver.assert_not_called()

    async with db_session_factory() as session:
        p_chk = await session.get(Payment, p_id)
        assert p_chk.status == PaymentStatus.PENDING
        assert p_chk.provisioning_failure_code == "renewal_evidence_missing_manual_review_required"


async def test_two_nearby_payments_against_one_subscription(monkeypatch, db_session_factory):
    """Two competing payments targeting the same subscription in the same window fail closed with DELIVERY_UNCERTAIN."""
    from bot.services.problem_payments import classify_and_verify_candidate, ProblemCategory
    from bot.services.yookassa_reconciliation import verify_and_fulfill_yookassa_payment

    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr(
        "bot.services.problem_payments.fetch_yookassa_payment",
        AsyncMock(return_value={"status": "succeeded", "paid": True, "amount": {"value": "350.00", "currency": "RUB"}}),
    )
    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.fetch_yookassa_payment",
        AsyncMock(return_value={
            "status": "succeeded", "paid": True,
            "amount": {"value": "350.00", "currency": "RUB"},
            "metadata": {"user_id": 999666, "tariff_id": 30, "platform": "deferred~u~860"},
        }),
    )

    now = datetime.utcnow()
    p_created = now - timedelta(hours=5)
    baseline = p_created + timedelta(days=5)

    async with db_session_factory() as session:
        user = User(telegram_id=999666, username="test_competing")
        server = Server(name="srv_comp", host="10.0.0.1", location="Frankfurt", is_active=True)
        tariff_30 = Tariff(id=30, days=30, label="1 месяц", price_rub=350)
        session.add_all([user, server, tariff_30])
        await session.flush()

        # Subscription extended by only 30 days (1 payment duration), but TWO payments were made!
        sub = Subscription(
            id=860,
            user_id=user.id,
            server_id=server.id,
            client_name="adapt_860",
            tariff_months=1,
            tariff_days=30,
            vpn_key="https://darimiru.ru/sub/860",
            status=SubStatus.ACTIVE,
            platform=Platform.ANDROID,
            started_at=p_created - timedelta(days=25),
            expires_at=baseline + timedelta(days=30),
        )
        session.add(sub)
        await session.flush()

        payment_1 = Payment(
            id=1861,
            user_id=user.id,
            amount=35000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_competing_1",
            subscription_id=None,
            platform="deferred~u~860",
            tariff_id=30,
            provisioning_baseline_expires_at=baseline,
            created_at=p_created,
        )
        # Second competing payment for the same sub 20 minutes later
        payment_2 = Payment(
            id=1862,
            user_id=user.id,
            amount=35000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_competing_2",
            subscription_id=None,
            platform="deferred~u~860",
            tariff_id=30,
            provisioning_baseline_expires_at=baseline,
            created_at=p_created + timedelta(minutes=20),
        )
        session.add_all([payment_1, payment_2])
        await session.commit()

    cand = {"type": "payment", "obj": payment_1, "user": user}
    item = await classify_and_verify_candidate(cand, now=now)
    assert item is not None
    assert item.category == ProblemCategory.DELIVERY_UNCERTAIN
    assert "конкурирующие платежи" in item.fulfillment_state or "несколько платежей" in item.details

    mock_deliver = AsyncMock()
    monkeypatch.setattr("bot.webhooks._process_and_deliver", mock_deliver)
    bot_mock = SimpleNamespace(send_message=AsyncMock())

    success, code = await verify_and_fulfill_yookassa_payment(bot_mock, "yk_competing_1", source="scheduler")
    assert success is False
    assert code == "ambiguous_competing_payments_for_subscription"
    mock_deliver.assert_not_called()

    async with db_session_factory() as session:
        p_chk = await session.get(Payment, payment_1.id)
        assert p_chk.status == PaymentStatus.PENDING
        assert p_chk.provisioning_failure_code == "ambiguous_competing_payments_for_subscription"


async def test_unrelated_manual_operations_mismatched_tariff(monkeypatch, db_session_factory):
    """A manual operation with a mismatched tariff (e.g. 7d test vs 90d purchase) must NOT fulfill the payment."""
    from bot.services.problem_payments import classify_and_verify_candidate, ProblemCategory
    from bot.services.yookassa_reconciliation import verify_and_fulfill_yookassa_payment

    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr(
        "bot.services.problem_payments.fetch_yookassa_payment",
        AsyncMock(return_value={"status": "succeeded", "paid": True, "amount": {"value": "900.00", "currency": "RUB"}}),
    )
    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.fetch_yookassa_payment",
        AsyncMock(return_value={
            "status": "succeeded", "paid": True,
            "amount": {"value": "900.00", "currency": "RUB"},
            "metadata": {"user_id": 999777, "tariff_id": 90, "platform": "deferred~u~870"},
        }),
    )

    now = datetime.utcnow()
    p_created = now - timedelta(hours=28)
    baseline = p_created + timedelta(days=5)

    async with db_session_factory() as session:
        user = User(telegram_id=999777, username="test_mismatch_op")
        server = Server(name="srv_mis", host="10.0.0.1", location="Frankfurt", is_active=True)
        tariff_7 = Tariff(id=7, days=7, label="7 дней", price_rub=100)
        tariff_90 = Tariff(id=90, days=90, label="3 месяца", price_rub=900)
        session.add_all([user, server, tariff_7, tariff_90])
        await session.flush()

        sub = Subscription(
            id=870,
            user_id=user.id,
            server_id=server.id,
            client_name="adapt_870",
            tariff_months=3,
            tariff_days=90,
            vpn_key="https://darimiru.ru/sub/870",
            status=SubStatus.ACTIVE,
            platform=Platform.ANDROID,
            started_at=p_created - timedelta(days=25),
            expires_at=baseline + timedelta(days=7),  # Only 7 days from manual op
        )
        session.add(sub)
        await session.flush()

        # Manual op performed was for tariff 7 (7 days), NOT tariff 90
        manual_op = ManualSubscriptionOperation(
            admin_user_id=111,
            target_user_id=user.id,
            subscription_id=sub.id,
            tariff_id=7,
            action="renew",
            status="completed",
            created_at=p_created + timedelta(minutes=10),
            completed_at=p_created + timedelta(minutes=11),
        )
        session.add(manual_op)

        payment = Payment(
            user_id=user.id,
            amount=90000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_mismatch_op_test",
            subscription_id=None,
            platform="deferred~u~870",
            tariff_id=90,
            provisioning_baseline_expires_at=baseline,
            created_at=p_created,
        )
        session.add(payment)
        await session.commit()

    cand = {"type": "payment", "obj": payment, "user": user}
    item = await classify_and_verify_candidate(cand, now=now)
    assert item is not None
    assert item.category == ProblemCategory.DELIVERY_UNCERTAIN

    mock_deliver = AsyncMock()
    monkeypatch.setattr("bot.webhooks._process_and_deliver", mock_deliver)
    bot_mock = SimpleNamespace(send_message=AsyncMock())

    success, code = await verify_and_fulfill_yookassa_payment(bot_mock, "yk_mismatch_op_test", source="scheduler")
    assert success is False
    assert code == "renewal_evidence_missing_manual_review_required"
    mock_deliver.assert_not_called()


async def test_concurrent_admin_callbacks_exactly_one_dispatch(monkeypatch, db_session_factory):
    """Two concurrent admin callbacks claiming retry via CAS result in exactly ONE dispatch."""
    import asyncio
    from bot.handlers.admin import admin_problem_payment_retry_tg

    monkeypatch.setattr("bot.handlers.admin.async_session", db_session_factory)
    monkeypatch.setattr("bot.handlers.payment.async_session", db_session_factory)
    monkeypatch.setattr("bot.handlers.admin._is_admin", lambda uid: True)

    now = datetime.utcnow()
    async with db_session_factory() as session:
        user = User(id=77, telegram_id=777888, username="user_cas")
        server = Server(name="srv_cas", host="10.0.0.1", location="Frankfurt", is_active=True)
        session.add_all([user, server])
        await session.flush()

        sub = Subscription(
            id=990, user_id=user.id, server_id=server.id, client_name="sub_cas",
            tariff_months=1, tariff_days=30, vpn_key="vless://sub_cas", status=SubStatus.ACTIVE, platform=Platform.IOS,
            expires_at=now + timedelta(days=30),
        )
        session.add(sub)
        await session.flush()

        payment = Payment(
            id=1990, user_id=user.id, amount=30000, currency="RUB", method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED, subscription_id=sub.id,
            telegram_delivery_error="send_failed: temporary connection error",
            platform="ios",
        )
        session.add(payment)
        await session.commit()

    dispatch_count = 0

    async def fake_delivery(**kwargs):
        nonlocal dispatch_count
        dispatch_count += 1
        await asyncio.sleep(0.01)
        return True, None

    monkeypatch.setattr("bot.webhooks.send_subscription_delivery_messages", fake_delivery)
    monkeypatch.setattr("bot.handlers.admin.admin_problem_payment_inspect", AsyncMock())

    cb1 = SimpleNamespace(
        from_user=SimpleNamespace(id=111),
        data="adm_prob_retry_tg:p:1990:1",
        answer=AsyncMock(),
        bot=SimpleNamespace(send_message=AsyncMock()),
    )
    cb2 = SimpleNamespace(
        from_user=SimpleNamespace(id=222),
        data="adm_prob_retry_tg:p:1990:1",
        answer=AsyncMock(),
        bot=SimpleNamespace(send_message=AsyncMock()),
    )

    # Launch both concurrently
    await asyncio.gather(
        admin_problem_payment_retry_tg(cb1),
        admin_problem_payment_retry_tg(cb2),
    )

    # Exactly one dispatch must have happened
    assert dispatch_count == 1

    # One answered success, the other answered in progress
    answers = [cb1.answer.call_args[0][0], cb2.answer.call_args[0][0]]
    assert any("успешно" in a for a in answers)
    assert any("уже выполняется" in a or "была начата" in a for a in answers)


async def test_interrupted_crashed_delivery_safely_recoverable(monkeypatch, db_session_factory):
    """An interrupted delivery in progress older than 300s can be reclaimed and recovered; max 3 attempts limit enforced."""
    from bot.handlers.admin import admin_problem_payment_retry_tg

    monkeypatch.setattr("bot.handlers.admin.async_session", db_session_factory)
    monkeypatch.setattr("bot.handlers.payment.async_session", db_session_factory)
    monkeypatch.setattr("bot.handlers.admin._is_admin", lambda uid: True)

    now = datetime.utcnow()
    crashed_epoch = int((now - timedelta(seconds=350)).timestamp())

    async with db_session_factory() as session:
        user = User(id=88, telegram_id=888999, username="user_crashed")
        server = Server(name="srv_crash", host="10.0.0.1", location="Frankfurt", is_active=True)
        session.add_all([user, server])
        await session.flush()

        sub = Subscription(
            id=995, user_id=user.id, server_id=server.id, client_name="sub_crash",
            tariff_months=1, tariff_days=30, vpn_key="vless://sub_crash", status=SubStatus.ACTIVE, platform=Platform.IOS,
            expires_at=now + timedelta(days=30),
        )
        session.add(sub)
        await session.flush()

        # Payment crashed on attempt 1, left in progress 350s ago
        payment = Payment(
            id=1995, user_id=user.id, amount=30000, currency="RUB", method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED, subscription_id=sub.id,
            telegram_delivery_error=f"delivery_in_progress:{crashed_epoch}:1:general",
            platform="ios",
        )
        # Payment crashed on attempt 3, left in progress 350s ago (exceeded limit)
        payment_exceeded = Payment(
            id=1996, user_id=user.id, amount=30000, currency="RUB", method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED, subscription_id=sub.id,
            telegram_delivery_error=f"delivery_in_progress:{crashed_epoch}:3:general",
            platform="ios",
        )
        session.add_all([payment, payment_exceeded])
        await session.commit()

    dispatched = False

    async def fake_delivery(**kwargs):
        nonlocal dispatched
        dispatched = True
        return True, None

    monkeypatch.setattr("bot.webhooks.send_subscription_delivery_messages", fake_delivery)
    monkeypatch.setattr("bot.handlers.admin.admin_problem_payment_inspect", AsyncMock())

    # 1. Recovery of attempt 1: should succeed and deliver
    cb_rec = SimpleNamespace(
        from_user=SimpleNamespace(id=111),
        data="adm_prob_retry_tg:p:1995:1",
        answer=AsyncMock(),
        bot=SimpleNamespace(send_message=AsyncMock()),
    )
    await admin_problem_payment_retry_tg(cb_rec)
    assert dispatched is True
    assert "успешно" in cb_rec.answer.call_args[0][0]

    async with db_session_factory() as session:
        p_upd = await session.get(Payment, 1995)
        assert p_upd.telegram_delivery_error is None

    # 2. Exceeded attempts (attempt 3+1 = 4): should reject and not dispatch
    dispatched = False
    cb_exceeded = SimpleNamespace(
        from_user=SimpleNamespace(id=111),
        data="adm_prob_retry_tg:p:1996:1",
        answer=AsyncMock(),
        bot=SimpleNamespace(send_message=AsyncMock()),
    )
    await admin_problem_payment_retry_tg(cb_exceeded)
    assert dispatched is False
    assert "Превышен лимит попыток" in cb_exceeded.answer.call_args[0][0]


async def test_admin_log_with_partial_renewal_fails_closed(monkeypatch, db_session_factory):
    """An admin log mentioning the payment must NOT bypass required extension validation when renewal is partial."""
    from bot.services.problem_payments import classify_and_verify_candidate, ProblemCategory
    from bot.services.yookassa_reconciliation import verify_and_fulfill_yookassa_payment

    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr(
        "bot.services.problem_payments.fetch_yookassa_payment",
        AsyncMock(return_value={"status": "succeeded", "paid": True, "amount": {"value": "350.00", "currency": "RUB"}}),
    )
    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.fetch_yookassa_payment",
        AsyncMock(return_value={
            "status": "succeeded", "paid": True,
            "amount": {"value": "350.00", "currency": "RUB"},
            "metadata": {"user_id": 999777, "tariff_id": 30, "platform": "deferred~u~870"},
        }),
    )

    now = datetime.utcnow()
    p_created = now - timedelta(hours=4)
    baseline = p_created + timedelta(days=5)

    async with db_session_factory() as session:
        user = User(telegram_id=999777, username="test_partial_admin")
        server = Server(name="srv_partial_adm", host="10.0.0.1", location="Frankfurt", is_active=True)
        tariff_30 = Tariff(id=30, days=30, label="1 месяц", price_rub=350)
        session.add_all([user, server, tariff_30])
        await session.flush()

        # Target sub was extended by only 5 days instead of required 30 days!
        sub = Subscription(
            id=870,
            user_id=user.id,
            server_id=server.id,
            client_name="adapt_870",
            tariff_months=1,
            tariff_days=30,
            vpn_key="https://darimiru.ru/sub/870",
            status=SubStatus.ACTIVE,
            platform=Platform.IOS,
            started_at=p_created - timedelta(days=25),
            expires_at=baseline + timedelta(days=5),
        )
        session.add(sub)
        await session.flush()

        payment = Payment(
            id=1870,
            user_id=user.id,
            amount=35000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_partial_adm_1870",
            subscription_id=None,
            platform="deferred~u~870",
            tariff_id=30,
            provisioning_baseline_expires_at=baseline,
            created_at=p_created,
        )
        session.add(payment)
        await session.flush()

        # Admin log specifically mentions payment #1870
        admin_log = AdminActionLog(
            actor_telegram_id=111,
            action="renew_subscription",
            target_telegram_id=user.telegram_id,
            entity_type="subscription",
            entity_id=str(sub.id),
            status="success",
            summary="Manual renewal for payment #1870",
            details="Added 5 days bonus",
            created_at=p_created + timedelta(minutes=30),
        )
        session.add(admin_log)
        await session.commit()

    cand = {"type": "payment", "obj": payment, "user": user}
    item = await classify_and_verify_candidate(cand, now=now)
    assert item is not None
    # Must NOT classify as VERIFIED_MANUALLY_FULFILLED! Must fail closed to DELIVERY_UNCERTAIN
    assert item.category == ProblemCategory.DELIVERY_UNCERTAIN
    assert "частичное продление" in item.details or "частичное продление" in item.fulfillment_state

    mock_deliver = AsyncMock()
    monkeypatch.setattr("bot.webhooks._process_and_deliver", mock_deliver)
    bot_mock = SimpleNamespace(send_message=AsyncMock())

    success, code = await verify_and_fulfill_yookassa_payment(bot_mock, "yk_partial_adm_1870", source="scheduler")
    assert success is False
    assert code == "partial_admin_renewal_manual_review_required"
    mock_deliver.assert_not_called()

    async with db_session_factory() as session:
        p_chk = await session.get(Payment, 1870)
        assert p_chk.status != PaymentStatus.COMPLETED
        assert p_chk.subscription_id is None
        assert p_chk.provisioning_failure_code == "partial_admin_renewal_manual_review_required"

        ref_logs = (await session.execute(
            select(ReferralPaymentLog).where(ReferralPaymentLog.payment_id == 1870)
        )).scalars().all()
        assert len(ref_logs) == 0


async def test_audit_id_substring_collision_does_not_falsely_match(monkeypatch, db_session_factory):
    """Payment #12 must NOT match an AdminActionLog mentioning #123."""
    from bot.services.problem_payments import classify_and_verify_candidate, ProblemCategory
    from bot.services.yookassa_reconciliation import verify_and_fulfill_yookassa_payment

    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr(
        "bot.services.problem_payments.fetch_yookassa_payment",
        AsyncMock(return_value={"status": "succeeded", "paid": True, "amount": {"value": "350.00", "currency": "RUB"}}),
    )
    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.fetch_yookassa_payment",
        AsyncMock(return_value={
            "status": "succeeded", "paid": True,
            "amount": {"value": "350.00", "currency": "RUB"},
            "metadata": {"user_id": 999888, "tariff_id": 30, "platform": "deferred~u~880"},
        }),
    )

    now = datetime.utcnow()
    p_created = now - timedelta(hours=3)
    baseline = p_created + timedelta(days=5)

    async with db_session_factory() as session:
        user = User(telegram_id=999888, username="test_substring_collision")
        server = Server(name="srv_sub_col", host="10.0.0.1", location="Frankfurt", is_active=True)
        tariff_30 = Tariff(id=30, days=30, label="1 месяц", price_rub=350)
        session.add_all([user, server, tariff_30])
        await session.flush()

        sub = Subscription(
            id=880,
            user_id=user.id,
            server_id=server.id,
            client_name="adapt_880",
            tariff_months=1,
            tariff_days=30,
            vpn_key="https://darimiru.ru/sub/880",
            status=SubStatus.ACTIVE,
            platform=Platform.IOS,
            started_at=p_created - timedelta(days=25),
            expires_at=baseline + timedelta(days=30),
        )
        session.add(sub)
        await session.flush()

        payment_12 = Payment(
            id=12,
            user_id=user.id,
            amount=35000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_sub_col_12",
            subscription_id=None,
            platform="deferred~u~880",
            tariff_id=30,
            provisioning_baseline_expires_at=baseline,
            created_at=p_created,
        )
        # Competing payment 123 for same sub
        payment_123 = Payment(
            id=123,
            user_id=user.id,
            amount=35000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_sub_col_123",
            subscription_id=None,
            platform="deferred~u~880",
            tariff_id=30,
            provisioning_baseline_expires_at=baseline,
            created_at=p_created + timedelta(minutes=15),
        )
        session.add_all([payment_12, payment_123])
        await session.flush()

        # Admin log mentions #123 (NOT #12)
        admin_log = AdminActionLog(
            actor_telegram_id=111,
            action="renew_subscription",
            target_telegram_id=user.telegram_id,
            entity_type="subscription",
            entity_id=str(sub.id),
            status="success",
            summary="Ручное продление по обращению, платеж #123",
            created_at=p_created + timedelta(minutes=30),
        )
        session.add(admin_log)
        await session.commit()

    # Problem payments for #12 must see competing payment and NOT treat log #123 as unique log for #12
    cand = {"type": "payment", "obj": payment_12, "user": user}
    item = await classify_and_verify_candidate(cand, now=now)
    assert item is not None
    assert item.category == ProblemCategory.DELIVERY_UNCERTAIN
    assert "конкурирующие платежи" in item.fulfillment_state or "несколько платежей" in item.details

    mock_deliver = AsyncMock()
    monkeypatch.setattr("bot.webhooks._process_and_deliver", mock_deliver)
    bot_mock = SimpleNamespace(send_message=AsyncMock())

    # Reconciliation for #12 must NOT attribute log #123 to payment #12
    success, code = await verify_and_fulfill_yookassa_payment(bot_mock, "yk_sub_col_12", source="scheduler")
    assert success is False
    assert code == "ambiguous_competing_payments_for_subscription"
    mock_deliver.assert_not_called()


async def test_three_consecutive_failed_telegram_sends_blocks_fourth_and_force_recovers(monkeypatch, db_session_factory):
    """3 consecutive Telegram send failures must block attempt 4, and allow recovery via :force callback."""
    from bot.handlers.admin import admin_problem_payment_retry_tg

    monkeypatch.setattr("bot.handlers.admin.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)
    monkeypatch.setattr("bot.handlers.admin._is_admin", lambda uid: True)

    now = datetime.utcnow()
    async with db_session_factory() as session:
        user = User(telegram_id=777888, username="test_retry_cap")
        server = Server(name="srv_rt", host="10.0.0.1", location="Frankfurt", is_active=True)
        tariff = Tariff(id=1, days=30, label="1 месяц", price_rub=300)
        session.add_all([user, server, tariff])
        await session.flush()

        sub = Subscription(
            id=999,
            user_id=user.id,
            server_id=server.id,
            client_name="adapt_999",
            tariff_months=1,
            tariff_days=30,
            vpn_key="https://darimiru.ru/sub/999",
            status=SubStatus.ACTIVE,
            platform=Platform.IOS,
            started_at=now - timedelta(days=1),
            expires_at=now + timedelta(days=29),
        )
        session.add(sub)
        await session.flush()

        payment = Payment(
            id=2001,
            user_id=user.id,
            amount=30000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            subscription_id=sub.id,
            telegram_delivery_error="telegram_send_failed: Bot was blocked by the user",
            platform="ios",
        )
        session.add(payment)
        await session.commit()

    dispatch_count = 0
    delivery_should_succeed = False

    async def fake_delivery(**kwargs):
        nonlocal dispatch_count
        dispatch_count += 1
        if delivery_should_succeed:
            return True, None
        return False, "Bot was blocked by the user"

    monkeypatch.setattr("bot.webhooks.send_subscription_delivery_messages", fake_delivery)
    monkeypatch.setattr("bot.handlers.admin.admin_problem_payment_inspect", AsyncMock())

    cb = SimpleNamespace(
        from_user=SimpleNamespace(id=111),
        data="adm_prob_retry_tg:p:2001:1",
        answer=AsyncMock(),
        bot=SimpleNamespace(send_message=AsyncMock()),
    )

    # 1. First retry failure: attempts becomes 1
    await admin_problem_payment_retry_tg(cb)
    assert dispatch_count == 1
    assert "1/3" in cb.answer.call_args[0][0]
    async with db_session_factory() as session:
        p1 = await session.get(Payment, 2001)
        assert p1.telegram_delivery_error.startswith("retry_failed:1:general:")

    # 2. Second retry failure: attempts becomes 2
    cb.answer.reset_mock()
    await admin_problem_payment_retry_tg(cb)
    assert dispatch_count == 2
    assert "2/3" in cb.answer.call_args[0][0]
    async with db_session_factory() as session:
        p2 = await session.get(Payment, 2001)
        assert p2.telegram_delivery_error.startswith("retry_failed:2:general:")

    # 3. Third retry failure: attempts becomes 3
    cb.answer.reset_mock()
    await admin_problem_payment_retry_tg(cb)
    assert dispatch_count == 3
    assert "3/3" in cb.answer.call_args[0][0]
    async with db_session_factory() as session:
        p3 = await session.get(Payment, 2001)
        assert p3.telegram_delivery_error.startswith("retry_failed:3:general:")

    # 4. Fourth retry (normal): must be BLOCKED!
    cb.answer.reset_mock()
    await admin_problem_payment_retry_tg(cb)
    # dispatch_count MUST NOT increment!
    assert dispatch_count == 3
    assert "Превышен лимит попыток" in cb.answer.call_args[0][0]

    # 5. Recovery with :force
    delivery_should_succeed = True
    cb_force = SimpleNamespace(
        from_user=SimpleNamespace(id=111),
        data="adm_prob_retry_tg:p:2001:1:force",
        answer=AsyncMock(),
        bot=SimpleNamespace(send_message=AsyncMock()),
    )
    await admin_problem_payment_retry_tg(cb_force)
    assert dispatch_count == 4
    assert "успешно" in cb_force.answer.call_args[0][0]

    async with db_session_factory() as session:
        p_final = await session.get(Payment, 2001)
        assert p_final.telegram_delivery_error is None

        # Verify audit logs: force reset was audited
        audit_force = (await session.execute(
            select(AdminActionLog).where(
                AdminActionLog.action == "force_reset_telegram_retry",
                AdminActionLog.entity_id == "2001",
            )
        )).scalar_one_or_none()
        assert audit_force is not None
        assert audit_force.actor_telegram_id == 111

        audit_retry = (await session.execute(
            select(AdminActionLog).where(
                AdminActionLog.action == "retry_telegram_delivery",
                AdminActionLog.entity_id == "2001",
                AdminActionLog.status == "success",
            )
        )).scalars().all()
        assert len(audit_retry) >= 1


async def test_force_retry_cannot_bypass_active_delivery_claim(monkeypatch, db_session_factory):
    """Force retry (:force) must NEVER bypass an active, non-expired delivery claim."""
    from bot.handlers.admin import admin_problem_payment_retry_tg

    monkeypatch.setattr("bot.handlers.admin.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)
    monkeypatch.setattr("bot.handlers.admin._is_admin", lambda uid: True)

    now = datetime.utcnow()
    now_epoch = int(now.timestamp())
    active_epoch = now_epoch - 30  # 30 seconds ago (active < 300s)

    async with db_session_factory() as session:
        user = User(telegram_id=888111, username="test_active_force")
        server = Server(name="srv_act", host="10.0.0.1", location="Frankfurt", is_active=True)
        tariff = Tariff(id=1, days=30, label="1 месяц", price_rub=300)
        session.add_all([user, server, tariff])
        await session.flush()

        sub = Subscription(
            id=1001,
            user_id=user.id,
            server_id=server.id,
            client_name="adapt_1001",
            tariff_months=1,
            tariff_days=30,
            vpn_key="https://darimiru.ru/sub/1001",
            status=SubStatus.ACTIVE,
            platform=Platform.IOS,
            started_at=now - timedelta(days=1),
            expires_at=now + timedelta(days=29),
        )
        session.add(sub)
        await session.flush()

        payment = Payment(
            id=2002,
            user_id=user.id,
            amount=30000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            subscription_id=sub.id,
            telegram_delivery_error=f"delivery_in_progress:{active_epoch}:1:general",
            platform="ios",
        )
        session.add(payment)
        await session.commit()

    dispatched = False

    async def fake_delivery(**kwargs):
        nonlocal dispatched
        dispatched = True
        return True, None

    monkeypatch.setattr("bot.webhooks.send_subscription_delivery_messages", fake_delivery)
    monkeypatch.setattr("bot.handlers.admin.admin_problem_payment_inspect", AsyncMock())

    cb = SimpleNamespace(
        from_user=SimpleNamespace(id=111),
        data="adm_prob_retry_tg:p:2002:1:force",
        answer=AsyncMock(),
        bot=SimpleNamespace(send_message=AsyncMock()),
    )

    await admin_problem_payment_retry_tg(cb)
    assert dispatched is False
    assert "Отправка уже выполняется" in cb.answer.call_args[0][0]

    # Verify marker is completely untouched
    async with db_session_factory() as session:
        p = await session.get(Payment, 2002)
        assert p.telegram_delivery_error == f"delivery_in_progress:{active_epoch}:1:general"

        # No force audit recorded
        audit = (await session.execute(
            select(AdminActionLog).where(
                AdminActionLog.action == "force_reset_telegram_retry",
                AdminActionLog.entity_id == "2002",
            )
        )).scalar_one_or_none()
        assert audit is None


async def test_force_retry_blocked_on_unexhausted_attempts(monkeypatch, db_session_factory):
    """Force reset is allowed only for an exhausted retry state (prior_attempts >= 3)."""
    from bot.handlers.admin import admin_problem_payment_retry_tg

    monkeypatch.setattr("bot.handlers.admin.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)
    monkeypatch.setattr("bot.handlers.admin._is_admin", lambda uid: True)

    now = datetime.utcnow()
    async with db_session_factory() as session:
        user = User(telegram_id=888222, username="test_unexhausted_force")
        server = Server(name="srv_unex", host="10.0.0.1", location="Frankfurt", is_active=True)
        tariff = Tariff(id=1, days=30, label="1 месяц", price_rub=300)
        session.add_all([user, server, tariff])
        await session.flush()

        sub = Subscription(
            id=1002,
            user_id=user.id,
            server_id=server.id,
            client_name="adapt_1002",
            tariff_months=1,
            tariff_days=30,
            vpn_key="https://darimiru.ru/sub/1002",
            status=SubStatus.ACTIVE,
            platform=Platform.IOS,
            started_at=now - timedelta(days=1),
            expires_at=now + timedelta(days=29),
        )
        session.add(sub)
        await session.flush()

        payment = Payment(
            id=2003,
            user_id=user.id,
            amount=30000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            subscription_id=sub.id,
            telegram_delivery_error="retry_failed:1:general:Network timeout",
            platform="ios",
        )
        session.add(payment)
        await session.commit()

    dispatched = False

    async def fake_delivery(**kwargs):
        nonlocal dispatched
        dispatched = True
        return True, None

    monkeypatch.setattr("bot.webhooks.send_subscription_delivery_messages", fake_delivery)
    monkeypatch.setattr("bot.handlers.admin.admin_problem_payment_inspect", AsyncMock())

    cb = SimpleNamespace(
        from_user=SimpleNamespace(id=111),
        data="adm_prob_retry_tg:p:2003:1:force",
        answer=AsyncMock(),
        bot=SimpleNamespace(send_message=AsyncMock()),
    )

    await admin_problem_payment_retry_tg(cb)
    assert dispatched is False
    assert "только при исчерпании лимита" in cb.answer.call_args[0][0]

    # Verify no audit was written
    async with db_session_factory() as session:
        audit = (await session.execute(
            select(AdminActionLog).where(
                AdminActionLog.action == "force_reset_telegram_retry",
                AdminActionLog.entity_id == "2003",
            )
        )).scalar_one_or_none()
        assert audit is None


async def test_expired_claim_recovery_is_bounded_and_audited(monkeypatch, db_session_factory):
    """An expired in-progress claim must be handled as uncertain outcome with bounded, audited recovery."""
    from bot.handlers.admin import admin_problem_payment_retry_tg

    monkeypatch.setattr("bot.handlers.admin.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)
    monkeypatch.setattr("bot.handlers.admin._is_admin", lambda uid: True)

    now = datetime.utcnow()
    now_epoch = int(now.timestamp())
    expired_epoch = now_epoch - 360  # 360s ago (> 300s)

    async with db_session_factory() as session:
        user = User(telegram_id=888333, username="test_expired_rec")
        server = Server(name="srv_exp", host="10.0.0.1", location="Frankfurt", is_active=True)
        tariff = Tariff(id=1, days=30, label="1 месяц", price_rub=300)
        session.add_all([user, server, tariff])
        await session.flush()

        sub = Subscription(
            id=1003,
            user_id=user.id,
            server_id=server.id,
            client_name="adapt_1003",
            tariff_months=1,
            tariff_days=30,
            vpn_key="https://darimiru.ru/sub/1003",
            status=SubStatus.ACTIVE,
            platform=Platform.IOS,
            started_at=now - timedelta(days=1),
            expires_at=now + timedelta(days=29),
        )
        session.add(sub)
        await session.flush()

        payment = Payment(
            id=2004,
            user_id=user.id,
            amount=30000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            subscription_id=sub.id,
            telegram_delivery_error=f"delivery_in_progress:{expired_epoch}:2:general",
            platform="ios",
        )
        session.add(payment)
        await session.commit()

    dispatched = False

    async def fake_delivery(**kwargs):
        nonlocal dispatched
        dispatched = True
        return True, None

    monkeypatch.setattr("bot.webhooks.send_subscription_delivery_messages", fake_delivery)
    monkeypatch.setattr("bot.handlers.admin.admin_problem_payment_inspect", AsyncMock())

    cb = SimpleNamespace(
        from_user=SimpleNamespace(id=111),
        data="adm_prob_retry_tg:p:2004:1",
        answer=AsyncMock(),
        bot=SimpleNamespace(send_message=AsyncMock()),
    )

    # Next attempt should be 2 + 1 = 3 (bounded attempt progression)
    await admin_problem_payment_retry_tg(cb)
    assert dispatched is True
    assert "успешно" in cb.answer.call_args[0][0]

    async with db_session_factory() as session:
        p = await session.get(Payment, 2004)
        assert p.telegram_delivery_error is None

        # Verify audit for recover_expired_delivery_claim was recorded
        audit = (await session.execute(
            select(AdminActionLog).where(
                AdminActionLog.action == "recover_expired_delivery_claim",
                AdminActionLog.entity_id == "2004",
            )
        )).scalar_one_or_none()
        assert audit is not None
        assert "next_attempt=3" in (audit.details or "")


async def test_stale_completion_does_not_overwrite_superseded_claim(monkeypatch, db_session_factory):
    """Final success/failure updates conditionally match claim marker; stale sender cannot overwrite newer state."""
    from bot.handlers.admin import admin_problem_payment_retry_tg

    monkeypatch.setattr("bot.handlers.admin.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)
    monkeypatch.setattr("bot.handlers.admin._is_admin", lambda uid: True)

    now = datetime.utcnow()
    async with db_session_factory() as session:
        user = User(telegram_id=888444, username="test_stale_sender")
        server = Server(name="srv_stale", host="10.0.0.1", location="Frankfurt", is_active=True)
        tariff = Tariff(id=1, days=30, label="1 месяц", price_rub=300)
        session.add_all([user, server, tariff])
        await session.flush()

        sub = Subscription(
            id=1004,
            user_id=user.id,
            server_id=server.id,
            client_name="adapt_1004",
            tariff_months=1,
            tariff_days=30,
            vpn_key="https://darimiru.ru/sub/1004",
            status=SubStatus.ACTIVE,
            platform=Platform.IOS,
            started_at=now - timedelta(days=1),
            expires_at=now + timedelta(days=29),
        )
        session.add(sub)
        await session.flush()

        payment = Payment(
            id=2005,
            user_id=user.id,
            amount=30000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            subscription_id=sub.id,
            telegram_delivery_error="retry_failed:1:general:Network drop",
            platform="ios",
        )
        session.add(payment)
        await session.commit()

    newer_marker = "delivery_in_progress:999999999:2:general"

    # Simulate that while delivery is running, a newer attempt supersedes the claim
    async def fake_slow_delivery(**kwargs):
        async with db_session_factory() as session:
            p = await session.get(Payment, 2005)
            p.telegram_delivery_error = newer_marker
            await session.commit()
        return True, None

    monkeypatch.setattr("bot.webhooks.send_subscription_delivery_messages", fake_slow_delivery)
    monkeypatch.setattr("bot.handlers.admin.admin_problem_payment_inspect", AsyncMock())

    cb = SimpleNamespace(
        from_user=SimpleNamespace(id=111),
        data="adm_prob_retry_tg:p:2005:1",
        answer=AsyncMock(),
        bot=SimpleNamespace(send_message=AsyncMock()),
    )

    await admin_problem_payment_retry_tg(cb)
    assert "задача была вытеснена" in cb.answer.call_args[0][0]

    async with db_session_factory() as session:
        p = await session.get(Payment, 2005)
        # Stale sender must NOT have cleared error to None! Newer marker must be preserved!
        assert p.telegram_delivery_error == newer_marker

        # Audit should show status='superseded'
        audit = (await session.execute(
            select(AdminActionLog).where(
                AdminActionLog.action == "retry_telegram_delivery",
                AdminActionLog.entity_id == "2005",
            )
        )).scalar_one_or_none()
        assert audit is not None
        assert audit.status == "superseded"
        assert "was_applied=False" in (audit.details or "")








