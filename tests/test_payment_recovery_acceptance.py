"""End-to-end business acceptance tests for payment recovery, competing payments, and problem payment alerts.

Validates the 3 core business acceptance scenarios:
1. Scenario A: Unpaid checkout (#308) does NOT block paid checkout (#309), supplier renews exactly once,
   ledger/cashback/referral applied exactly once, customer/admin notified, replay-safe.
2. Scenario B: Pre-mutated supplier subscription recovery (failure_code='financial_finalization_pending')
   completes with 0 supplier calls, commits finances once, and is replay-safe.
3. Scenario C: Two genuinely paid competing payments fail closed with 'ambiguous_competing_payments_for_subscription',
   0 supplier calls, exactly one urgent alert, no spam on scheduler ticks, and exactly one resolution alert when resolved.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.models import (
    AdaptSubscription,
    AdminActionLog,
    BalanceDirection,
    BalanceTopUp,
    BalanceTransaction,
    BalanceTransactionKind,
    Base,
    BotSettings,
    Payment,
    PaymentMethod,
    PaymentStatus,
    Platform,
    ReferralConfig,
    ReferralEarning,
    ReferralPaymentLog,
    Server,
    SubStatus,
    Subscription,
    Tariff,
    TariffType,
    User,
)
from bot.services.adapt_api import AdaptAPIError
from bot.services.problem_payments import (
    ProblemCategory,
    alert_admins_if_problem_payments_detected,
    build_problem_payments_report,
)
from bot.services.yookassa_reconciliation import (
    reconcile_pending_yookassa_payments,
    verify_and_fulfill_yookassa_payment,
)

pytestmark = pytest.mark.asyncio


# ── Stateful External Provider Fakes ──────────────────────────────────────────


class FakeYooKassa:
    """Stateful fake YooKassa external API."""

    def __init__(self) -> None:
        self.payments: dict[str, dict[str, Any]] = {}
        self.timeouts: set[str] = set()

    def set_payment(
        self,
        payment_id: str,
        *,
        status: str,
        paid: bool,
        amount: float = 125.0,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.payments[payment_id] = {
            "id": payment_id,
            "status": status,
            "paid": paid,
            "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
            "metadata": metadata or {},
        }

    def set_timeout(self, payment_id: str) -> None:
        self.timeouts.add(payment_id)

    async def fetch(self, payment_id: str) -> dict[str, Any] | None:
        if payment_id in self.timeouts:
            raise asyncio.TimeoutError(f"Simulated network timeout for {payment_id}")
        return self.payments.get(payment_id)


class FakeAdaptAPI:
    """Stateful fake Adapt Group external supplier API.

    Tracks renew_call_count, upgrade_call_count, and mutates subscription state.
    """

    def __init__(
        self,
        initial_end_date: datetime,
        plan_uuid: str = "plan-uuid-30",
        devices: int = 1,
    ) -> None:
        self.end_date = initial_end_date
        self.renew_call_count = 0
        self.upgrade_call_count = 0
        self.plan_uuid = plan_uuid
        self.devices = devices
        self.is_frozen = False
        self.get_status_error: Exception | None = None
        self.upgrade_error: Exception | None = None
        self.upgrade_mutates_before_error: bool = False
        self.renew_error: Exception | None = None

    async def get_status(self, subscription_uuid: str) -> dict[str, Any]:
        if self.get_status_error:
            raise self.get_status_error
        return {
            "success": True,
            "subscription_uuid": subscription_uuid,
            "end_date": self.end_date.isoformat(),
            "plan_uuid": self.plan_uuid,
            "devices": self.devices,
            "is_frozen": self.is_frozen,
            "is_active": True,
        }

    async def upgrade_subscription(self, subscription_uuid: str, new_plan_uuid: str) -> dict[str, Any]:
        self.upgrade_call_count += 1
        if self.upgrade_error:
            if self.upgrade_mutates_before_error:
                self.plan_uuid = new_plan_uuid
                self.devices = 3
                self.end_date = self.end_date + timedelta(days=30)
            raise self.upgrade_error
        self.plan_uuid = new_plan_uuid
        self.devices = 3
        self.end_date = self.end_date + timedelta(days=30)
        return {
            "success": True,
            "subscription_uuid": subscription_uuid,
            "end_date": self.end_date.isoformat(),
            "plan_uuid": self.plan_uuid,
            "devices": self.devices,
        }

    async def renew_subscription(self, subscription_uuid: str) -> dict[str, Any]:
        self.renew_call_count += 1
        if self.renew_error:
            raise self.renew_error
        self.end_date += timedelta(days=30)
        return {
            "success": True,
            "subscription_uuid": subscription_uuid,
            "end_date": self.end_date.isoformat(),
            "plan_uuid": self.plan_uuid,
            "devices": self.devices,
        }

    async def renew_subscription_custom(self, subscription_uuid: str, days: int) -> dict[str, Any]:
        self.end_date += timedelta(days=days)
        return {
            "success": True,
            "subscription_uuid": subscription_uuid,
            "end_date": self.end_date.isoformat(),
            "plan_uuid": self.plan_uuid,
            "devices": self.devices,
        }

    async def unfreeze_subscription(self, subscription_uuid: str) -> dict[str, Any]:
        self.is_frozen = False
        return {"success": True}

    async def freeze_subscription(self, subscription_uuid: str) -> dict[str, Any]:
        self.is_frozen = True
        return {"success": True}


class FakeTelegramBot:
    """Stateful fake Telegram bot.

    Categorizes and counts customer messages, admin incident alerts, and admin resolution alerts.
    """

    def __init__(self) -> None:
        self.customer_messages: list[dict[str, Any]] = []
        self.admin_incident_alerts: list[dict[str, Any]] = []
        self.admin_resolution_alerts: list[dict[str, Any]] = []
        self.other_messages: list[dict[str, Any]] = []

    async def send_message(self, chat_id: int | str, text: str, **kwargs: Any) -> Any:
        msg = {"chat_id": chat_id, "text": text, "kwargs": kwargs}
        if "⚠️ ПРОБЛЕМНЫЕ ОПЛАТЫ" in text or "🚨" in text:
            self.admin_incident_alerts.append(msg)
        elif "ПРОБЛЕМНАЯ ОПЛАТА ОБРАБОТАНА" in text or "ПРОБЛЕМНОЕ ПОПОЛНЕНИЕ ОБРАБОТАНО" in text:
            self.admin_resolution_alerts.append(msg)
        elif str(chat_id) in ["999001", "999002"] or str(chat_id).startswith("-"):
            # Configured admin chat
            if "ОПЛАТА" in text or "💳" in text:
                self.other_messages.append(msg)
            else:
                self.admin_incident_alerts.append(msg)
        else:
            self.customer_messages.append(msg)
        return SimpleNamespace(message_id=len(self.customer_messages) + len(self.admin_incident_alerts))


# ── Database & Fixtures ───────────────────────────────────────────────────────


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
def _patch_system_sessions(monkeypatch, db_session_factory):
    monkeypatch.setattr("bot.database.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.cluster.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.handlers.admin.async_session", db_session_factory)
    monkeypatch.setattr("bot.handlers.payment.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.guide_service.async_session", db_session_factory)


# ── ACCEPTANCE SCENARIO 1: Unpaid Checkout (#308) + Paid Checkout (#309) ──────


async def test_competing_unpaid_checkout_does_not_block_paid_payment(monkeypatch, db_session_factory):
    """E2E Test 1: Exact #308/#309 Scenario.

    Payment A: local PENDING, YooKassa pending/unpaid (abandoned checkout).
    Payment B: local PENDING, YooKassa succeeded/paid, discount_applied > 0.
    Target: Same existing subscription #182.

    Expected:
    - Payment A is evaluated via authoritative YooKassa status and NOT considered a competitor.
    - Reconciliation B succeeds:
        - Supplier renew count == 1.
        - Subscription expiry extended by exactly 30 days.
        - B status == COMPLETED, subscription_id linked, provisioning_failure_code is None.
        - Discount debit tx count == 1 (12.50 RUB debited).
        - Cashback credit tx count == 1 (11.25 RUB credited).
        - ReferralPaymentLog <= 1 (112.50 RUB).
        - ReferralEarning <= 1 (45.00 RUB).
        - Customer delivery message sent.
        - Problem payments for B == 0 urgent.
    - Replay webhook B & scheduler B:
        - Supplier renew count remains 1.
        - Financial side effects count remains 1.
        - Customer delivery not duplicated.
        - Admin resolution not duplicated.
    """
    t0 = datetime(2026, 11, 6, 16, 4, 1)
    fake_adapt = FakeAdaptAPI(initial_end_date=t0)
    fake_yk = FakeYooKassa()
    fake_tg = FakeTelegramBot()

    # Wire up stateful fakes to production code boundaries
    monkeypatch.setattr("bot.services.yookassa_reconciliation.fetch_yookassa_payment", fake_yk.fetch)
    monkeypatch.setattr("bot.services.problem_payments.fetch_yookassa_payment", fake_yk.fetch)
    monkeypatch.setattr("bot.services.subscription_service.AdaptAPI", lambda *args, **kwargs: fake_adapt)
    monkeypatch.setattr("bot.services.notifications.settings.admin_ids", [999001])
    monkeypatch.setattr("bot.services.notifications.settings.owner_ids", [])
    monkeypatch.setattr("bot.services.notifications.settings.support_agent_ids", [])

    async with db_session_factory() as session:
        # Configuration
        session.add(BotSettings(key="cashback_enabled", value="1"))
        session.add(BotSettings(key="cashback_percent", value="10.0"))
        session.add(ReferralConfig(id=1, is_enabled=True, commission_percent=40.0))

        referrer = User(id=40, telegram_id=404040, username="partner_user")
        customer = User(id=72, telegram_id=5204426782, username="customer_72", balance_rub=12.50, referred_by=404040)
        server = Server(id=1, name="srv1", host="1.1.1.1", location="Frankfurt", is_active=True)
        tariff = Tariff(
            id=30,
            label="1 месяц",
            price_rub=125,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            adapt_plan_uuid="plan-uuid-30",
        )
        sub182 = Subscription(
            id=182,
            user_id=72,
            server_id=1,
            tariff_months=1,
            tariff_days=30,
            status=SubStatus.ACTIVE,
            platform=Platform.IOS,
            client_name="adapt_194149bd-c98c-4426-bce8-77a2ac9df469",
            vpn_key="https://darimiru.ru/sub/194149bd-c98c-4426-bce8-77a2ac9df469",
            expires_at=t0,
        )
        adapt_sub = AdaptSubscription(
            subscription_id=182,
            adapt_uuid="194149bd-c98c-4426-bce8-77a2ac9df469",
            adapt_plan_uuid="plan-uuid-30",
            end_date=t0,
        )
        session.add_all([referrer, customer, server, tariff, sub182, adapt_sub])
        await session.flush()

        now = datetime.utcnow()
        # Payment A (#308): abandoned checkout
        payment_a = Payment(
            id=308,
            user_id=customer.id,
            tariff_id=30,
            platform="deferred~r~182",
            amount=12500,
            discount_applied=0.0,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="327e9f9c-000f-5001-a000-unpaid308",
            created_at=now - timedelta(minutes=15),
        )
        # Payment B (#309): paid checkout with discount
        payment_b = Payment(
            id=309,
            user_id=customer.id,
            tariff_id=30,
            platform="deferred~r~182",
            amount=11250,
            discount_applied=12.50,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="327e9f9c-000f-5001-a000-paid309",
            created_at=now - timedelta(minutes=5),
        )
        session.add_all([payment_a, payment_b])
        await session.commit()

    # Configure Fake YooKassa states
    fake_yk.set_payment("327e9f9c-000f-5001-a000-unpaid308", status="pending", paid=False, amount=125.0)
    fake_yk.set_payment(
        "327e9f9c-000f-5001-a000-paid309",
        status="succeeded",
        paid=True,
        amount=112.50,
        metadata={"user_id": 5204426782, "tariff_id": 30, "platform": "deferred~r~182"},
    )

    # ── Step 1: Run reconciliation for Payment B ──────────────────────────────
    success, code = await verify_and_fulfill_yookassa_payment(
        fake_tg,
        "327e9f9c-000f-5001-a000-paid309",
        source="scheduler",
    )
    assert success is True
    assert code == "subscription_completed"

    # Verify Supplier Mutation & Subscription Expiry
    assert fake_adapt.renew_call_count == 1
    assert fake_adapt.end_date == t0 + timedelta(days=30)

    # Verify Database state
    async with db_session_factory() as session:
        sub_check = await session.get(Subscription, 182)
        assert sub_check.expires_at == t0 + timedelta(days=30)
        assert sub_check.platform == Platform.IOS

        p_b = await session.get(Payment, 309)
        assert p_b.status == PaymentStatus.COMPLETED
        assert p_b.subscription_id == 182
        assert p_b.provisioning_failure_code is None

        # Verify Payment A was NOT modified or corrupted
        p_a = await session.get(Payment, 308)
        assert p_a.status == PaymentStatus.PENDING

        # Ledger: Discount debit
        discount_tx = await session.scalar(
            select(BalanceTransaction).where(
                BalanceTransaction.user_id == 72,
                BalanceTransaction.source_type == "payment_discount",
                BalanceTransaction.source_id == "327e9f9c-000f-5001-a000-paid309",
            )
        )
        assert discount_tx is not None
        assert discount_tx.amount_rub == 12.50
        assert discount_tx.direction == BalanceDirection.DEBIT
        assert discount_tx.balance_after_rub == 0.0

        # Ledger: Cashback credit (10% of 112.50 = 11.25 RUB)
        cashback_tx = await session.scalar(
            select(BalanceTransaction).where(
                BalanceTransaction.user_id == 72,
                BalanceTransaction.kind == BalanceTransactionKind.CASHBACK,
                BalanceTransaction.source_id == "309",
            )
        )
        assert cashback_tx is not None
        assert cashback_tx.amount_rub == 11.25
        assert cashback_tx.direction == BalanceDirection.CREDIT

        # Final customer balance
        customer_rec = await session.get(User, 72)
        assert customer_rec.balance_rub == 11.25

        # Referral Payment Log
        ref_log = await session.scalar(
            select(ReferralPaymentLog).where(ReferralPaymentLog.payment_id == 309)
        )
        assert ref_log is not None
        assert ref_log.referrer_id == 40
        assert ref_log.referred_user_id == 72
        assert ref_log.amount == 112.50

        # Referral Earning (40% of 112.50 = 45.00 RUB)
        ref_earning = await session.scalar(
            select(ReferralEarning).where(ReferralEarning.payment_id == 309)
        )
        assert ref_earning is not None
        assert ref_earning.referrer_id == 40
        assert ref_earning.amount_rub == 45.00

    # Customer and admin notifications
    assert len(fake_tg.customer_messages) >= 1
    initial_customer_msg_count = len(fake_tg.customer_messages)

    # Delivery used real subscription iOS platform, and platform picker was NOT offered
    assert any("Настройка для iOS" in m["text"] for m in fake_tg.customer_messages)
    assert not any("Настройка для Android" in m["text"] for m in fake_tg.customer_messages)
    assert not any("Выберите устройство" in m["text"] for m in fake_tg.customer_messages)

    # Admin notification check:
    # Scenario A is normal checkout recovery (unpaid A + paid B), no prior problem alert was fired.
    # Must receive standard admin payment notification, NOT a resolution alert!
    assert len(fake_tg.admin_resolution_alerts) == 0
    assert len(fake_tg.other_messages) == 1
    assert "💰 <b>Новая оплата!</b>" in fake_tg.other_messages[0]["text"]

    # Problem payments check
    report = await build_problem_payments_report()
    urgent_items_b = [i for i in report.items if i.item_type == "payment" and i.item_id == 309]
    assert len(urgent_items_b) == 0

    # ── Step 2: Replay Webhook & Scheduler (Idempotency) ──────────────────────
    success_replay_wh, code_wh = await verify_and_fulfill_yookassa_payment(
        fake_tg,
        "327e9f9c-000f-5001-a000-paid309",
        source="webhook",
    )
    assert success_replay_wh is True
    assert code_wh == "already_completed"

    success_replay_sched, code_sched = await verify_and_fulfill_yookassa_payment(
        fake_tg,
        "327e9f9c-000f-5001-a000-paid309",
        source="scheduler",
    )
    assert success_replay_sched is True
    assert code_sched == "already_completed"

    # Supplier renew count MUST REMAIN 1
    assert fake_adapt.renew_call_count == 1
    assert fake_adapt.end_date == t0 + timedelta(days=30)

    # Ledger and side effects MUST NOT be duplicated
    async with db_session_factory() as session:
        discount_count = await session.scalar(
            select(func.count()).select_from(BalanceTransaction).where(
                BalanceTransaction.user_id == 72,
                BalanceTransaction.source_type == "payment_discount",
            )
        )
        assert discount_count == 1

        cashback_count = await session.scalar(
            select(func.count()).select_from(BalanceTransaction).where(
                BalanceTransaction.user_id == 72,
                BalanceTransaction.kind == BalanceTransactionKind.CASHBACK,
            )
        )
        assert cashback_count == 1

        ref_log_count = await session.scalar(
            select(func.count()).select_from(ReferralPaymentLog).where(
                ReferralPaymentLog.payment_id == 309,
            )
        )
        assert ref_log_count == 1

        ref_earning_count = await session.scalar(
            select(func.count()).select_from(ReferralEarning).where(
                ReferralEarning.payment_id == 309,
            )
        )
        assert ref_earning_count == 1

    # Customer and admin notifications must not be duplicated
    assert len(fake_tg.customer_messages) == initial_customer_msg_count
    assert len(fake_tg.admin_resolution_alerts) == 0
    assert len(fake_tg.other_messages) == 1


# ── ACCEPTANCE SCENARIO 2: Pre-mutated Supplier Recovery (Zero Re-mutation) ────


async def test_recovery_of_pre_mutated_supplier_subscription_without_remutation(monkeypatch, db_session_factory):
    """E2E Test 2: Supplier mutation already succeeded, crash/restart before financial finalization.

    Payment state:
    - YooKassa: succeeded, paid=True.
    - Adapt subscription: already extended to authoritative end_date.
    - Payment: status=PENDING, subscription_id=182, failure_code="financial_finalization_pending".

    Expected:
    - Reconciliation finalizes the payment cleanly.
    - Supplier renew count == 0 (STRICTLY ZERO remutations).
    - Payment transitions to COMPLETED with provisioning_failure_code = None.
    - Ledger side-effects (discount debit, cashback) are committed once.
    - Problem Payments are completely clean.
    - Replay causes no supplier calls and no duplicate ledger entries.
    """
    t0 = datetime(2026, 11, 6, 16, 4, 1)
    t_extended = t0 + timedelta(days=30)
    fake_adapt = FakeAdaptAPI(initial_end_date=t_extended)
    fake_yk = FakeYooKassa()
    fake_tg = FakeTelegramBot()

    monkeypatch.setattr("bot.services.yookassa_reconciliation.fetch_yookassa_payment", fake_yk.fetch)
    monkeypatch.setattr("bot.services.problem_payments.fetch_yookassa_payment", fake_yk.fetch)
    monkeypatch.setattr("bot.services.subscription_service.AdaptAPI", lambda *args, **kwargs: fake_adapt)

    async with db_session_factory() as session:
        session.add(BotSettings(key="cashback_enabled", value="1"))
        session.add(BotSettings(key="cashback_percent", value="10.0"))

        customer = User(id=73, telegram_id=737373, username="user_73", balance_rub=15.0)
        server = Server(id=2, name="srv2", host="2.2.2.2", location="Frankfurt", is_active=True)
        tariff = Tariff(
            id=31,
            label="1 месяц",
            price_rub=150,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            adapt_plan_uuid="plan-uuid-30",
        )
        sub = Subscription(
            id=200,
            user_id=73,
            server_id=2,
            tariff_months=1,
            tariff_days=30,
            status=SubStatus.ACTIVE,
            platform=Platform.ANDROID,
            client_name="adapt_premutated_uuid",
            vpn_key="https://darimiru.ru/sub/premutated",
            expires_at=t_extended,
        )
        adapt_sub = AdaptSubscription(
            subscription_id=200,
            adapt_uuid="premutated_uuid",
            adapt_plan_uuid="plan-uuid-30",
            end_date=t_extended,
        )
        payment = Payment(
            id=350,
            user_id=customer.id,
            tariff_id=31,
            platform="deferred~r~200",
            amount=13500,
            discount_applied=15.0,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            subscription_id=200,
            provisioning_failure_code="financial_finalization_pending",
            provider_payment_id="yk_premutated_350",
            created_at=datetime.utcnow() - timedelta(minutes=10),
        )
        session.add_all([customer, server, tariff, sub, adapt_sub, payment])
        await session.commit()

    fake_yk.set_payment(
        "yk_premutated_350",
        status="succeeded",
        paid=True,
        amount=135.0,
        metadata={"user_id": 737373, "tariff_id": 31, "platform": "deferred~r~200"},
    )

    # Execute reconciliation
    success, code = await verify_and_fulfill_yookassa_payment(fake_tg, "yk_premutated_350", source="scheduler")
    assert success is True

    # Critical Assertion: Supplier API was NOT called to renew
    assert fake_adapt.renew_call_count == 0
    assert fake_adapt.end_date == t_extended

    async with db_session_factory() as session:
        p_check = await session.get(Payment, 350)
        assert p_check.status == PaymentStatus.COMPLETED
        assert p_check.subscription_id == 200
        assert p_check.provisioning_failure_code is None

        # Ledger check
        discount_tx = await session.scalar(
            select(BalanceTransaction).where(
                BalanceTransaction.user_id == 73,
                BalanceTransaction.source_type == "payment_discount",
                BalanceTransaction.source_id == "yk_premutated_350",
            )
        )
        assert discount_tx is not None
        assert discount_tx.amount_rub == 15.0
        assert discount_tx.direction == BalanceDirection.DEBIT

        cashback_tx = await session.scalar(
            select(BalanceTransaction).where(
                BalanceTransaction.user_id == 73,
                BalanceTransaction.kind == BalanceTransactionKind.CASHBACK,
                BalanceTransaction.source_id == "350",
            )
        )
        assert cashback_tx is not None
        assert cashback_tx.amount_rub == 13.50

    # Replay reconciliation
    success_rep, code_rep = await verify_and_fulfill_yookassa_payment(fake_tg, "yk_premutated_350", source="scheduler")
    assert success_rep is True
    assert code_rep == "already_completed"
    assert fake_adapt.renew_call_count == 0


# ── ACCEPTANCE SCENARIO 3: Two Paid Competing Payments & Alert Lifecycle ──────


async def test_two_actually_paid_competing_payments_fail_closed_with_single_alert(monkeypatch, db_session_factory):
    """E2E Test 3: Two genuinely paid competing payments targeting one subscription.

    Payment A: YooKassa succeeded + paid=True.
    Payment B: YooKassa succeeded + paid=True.
    Both target the same subscription in the conflict window without covering extension.

    Expected:
    - Reconciliation for B fails closed with 'ambiguous_competing_payments_for_subscription'.
    - Supplier renew count == 0.
    - Financial finalization does not take place.
    - Admin urgent alert is sent exactly once.
    - Replay scheduler tick does not mutate supplier and does NOT spam the admin.
    - When the conflict is resolved, admin receives exactly ONE resolution alert!
    """
    t0 = datetime(2026, 11, 6, 16, 4, 1)
    fake_adapt = FakeAdaptAPI(initial_end_date=t0)
    fake_yk = FakeYooKassa()
    fake_tg = FakeTelegramBot()

    monkeypatch.setattr("bot.services.yookassa_reconciliation.fetch_yookassa_payment", fake_yk.fetch)
    monkeypatch.setattr("bot.services.problem_payments.fetch_yookassa_payment", fake_yk.fetch)
    monkeypatch.setattr("bot.services.subscription_service.AdaptAPI", lambda *args, **kwargs: fake_adapt)
    monkeypatch.setattr("bot.services.notifications.settings.admin_ids", [999001])
    monkeypatch.setattr("bot.services.notifications.settings.owner_ids", [])
    monkeypatch.setattr("bot.services.notifications.settings.support_agent_ids", [])

    now = datetime.utcnow()
    p_created = now - timedelta(hours=2)

    async with db_session_factory() as session:
        customer = User(id=80, telegram_id=808080, username="user_80", balance_rub=0.0)
        server = Server(id=3, name="srv3", host="3.3.3.3", location="Frankfurt", is_active=True)
        tariff = Tariff(
            id=32,
            label="1 месяц",
            price_rub=200,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            adapt_plan_uuid="plan-uuid-30",
        )
        sub = Subscription(
            id=250,
            user_id=80,
            server_id=3,
            tariff_months=1,
            tariff_days=30,
            status=SubStatus.ACTIVE,
            platform=Platform.ANDROID,
            client_name="adapt_competing_sub",
            vpn_key="https://darimiru.ru/sub/competing",
            expires_at=t0,
        )
        adapt_sub = AdaptSubscription(
            subscription_id=250,
            adapt_uuid="competing_uuid",
            adapt_plan_uuid="plan-uuid-30",
            end_date=t0,
        )

        payment_a = Payment(
            id=401,
            user_id=customer.id,
            tariff_id=32,
            platform="deferred~r~250",
            amount=20000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_paid_competitor_A",
            provisioning_baseline_expires_at=t0,
            created_at=p_created,
        )
        payment_b = Payment(
            id=402,
            user_id=customer.id,
            tariff_id=32,
            platform="deferred~r~250",
            amount=20000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_paid_competitor_B",
            provisioning_baseline_expires_at=t0,
            created_at=p_created + timedelta(minutes=10),
        )
        session.add_all([customer, server, tariff, sub, adapt_sub, payment_a, payment_b])
        await session.commit()

    # Both are paid at YooKassa
    fake_yk.set_payment(
        "yk_paid_competitor_A",
        status="succeeded",
        paid=True,
        amount=200.0,
        metadata={"user_id": 808080, "tariff_id": 32, "platform": "deferred~r~250"},
    )
    fake_yk.set_payment(
        "yk_paid_competitor_B",
        status="succeeded",
        paid=True,
        amount=200.0,
        metadata={"user_id": 808080, "tariff_id": 32, "platform": "deferred~r~250"},
    )

    # ── Step 1: Reconcile Payment B ───────────────────────────────────────────
    success, code = await verify_and_fulfill_yookassa_payment(fake_tg, "yk_paid_competitor_B", source="scheduler")
    assert success is False
    assert code == "ambiguous_competing_payments_for_subscription"

    # Supplier API MUST NOT be mutated
    assert fake_adapt.renew_call_count == 0

    async with db_session_factory() as session:
        p_b = await session.get(Payment, 402)
        assert p_b.status == PaymentStatus.PENDING
        assert p_b.provisioning_failure_code == "ambiguous_competing_payments_for_subscription"

    # ── Step 2: Problem Payments Alerting ─────────────────────────────────────
    # Run problem payment alert evaluation
    await alert_admins_if_problem_payments_detected(fake_tg)
    assert len(fake_tg.admin_incident_alerts) == 1
    assert "⚠️ ПРОБЛЕМНЫЕ ОПЛАТЫ" in fake_tg.admin_incident_alerts[0]["text"]

    # ── Step 3: Replay Scheduler Tick (No Spam) ───────────────────────────────
    success_rep, code_rep = await verify_and_fulfill_yookassa_payment(fake_tg, "yk_paid_competitor_B", source="scheduler")
    assert success_rep is False
    assert code_rep == "ambiguous_competing_payments_for_subscription"
    assert fake_adapt.renew_call_count == 0

    # Scheduler tick alert re-check MUST NOT spam
    await alert_admins_if_problem_payments_detected(fake_tg)
    assert len(fake_tg.admin_incident_alerts) == 1  # Alert count remains 1

    # ── Step 4: Resolution of Conflicted Payment ──────────────────────────────
    # Simulate administrator resolution: payment 402 is fulfilled to a new sub
    async with db_session_factory() as session:
        p_resolve = await session.get(Payment, 402)
        p_resolve.status = PaymentStatus.COMPLETED
        p_resolve.subscription_id = 250
        p_resolve.provisioning_failure_code = None
        await session.commit()

    # Now run problem payment check: urgent items count drops, resolution alert must fire!
    await alert_admins_if_problem_payments_detected(fake_tg)
    assert len(fake_tg.admin_resolution_alerts) == 1
    assert "ПРОБЛЕМНАЯ ОПЛАТА ОБРАБОТАНА" in fake_tg.admin_resolution_alerts[0]["text"]
    assert "payment #402" in fake_tg.admin_resolution_alerts[0]["text"]
    assert "200.00 ₽" in fake_tg.admin_resolution_alerts[0]["text"]

    # Subsequent tick: no further alerts
    await alert_admins_if_problem_payments_detected(fake_tg)
    assert len(fake_tg.admin_resolution_alerts) == 1
    assert len(fake_tg.admin_incident_alerts) == 1


# ── ACCEPTANCE SCENARIO 4: Transition to Non-Urgent Problem Suppresses Resolution ─


async def test_urgent_transition_to_tg_delivery_failed_suppresses_resolution_alert(monkeypatch, db_session_factory):
    """Test Blocker 1: Urgent problem transitioning to non-urgent problem suppresses resolution alert.

    - Payment starts in FAILED_SUPPLIER -> urgent alert sent once.
    - Transition A: payment completed but telegram_delivery_error set -> resolution alert suppressed (0 alerts).
    - Transition B: telegram_delivery_error resolved -> exactly one resolution alert sent.
    - Next tick: resolution alert not duplicated.
    """
    fake_tg = FakeTelegramBot()
    fake_yk = FakeYooKassa()
    monkeypatch.setattr("bot.services.yookassa_reconciliation.fetch_yookassa_payment", fake_yk.fetch)
    monkeypatch.setattr("bot.services.problem_payments.fetch_yookassa_payment", fake_yk.fetch)
    monkeypatch.setattr("bot.services.notifications.settings.admin_ids", [999001])
    monkeypatch.setattr("bot.services.notifications.settings.owner_ids", [])
    monkeypatch.setattr("bot.services.notifications.settings.support_agent_ids", [])

    now = datetime.utcnow()
    async with db_session_factory() as session:
        user = User(id=91, telegram_id=919191, username="user_91")
        server = Server(id=4, name="srv4", host="4.4.4.4", location="Frankfurt", is_active=True)
        tariff = Tariff(
            id=34,
            label="1 месяц",
            price_rub=100,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
        )
        sub = Subscription(
            id=280,
            user_id=91,
            server_id=4,
            tariff_months=1,
            tariff_days=30,
            status=SubStatus.ACTIVE,
            platform=Platform.IOS,
            client_name="test_client_name",
            vpn_key="https://darimiru.ru/sub/tg_deliv_test",
            expires_at=now + timedelta(days=30),
        )
        payment = Payment(
            id=480,
            user_id=91,
            tariff_id=34,
            platform="deferred~r~280",
            amount=10000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            subscription_id=280,
            provisioning_failure_code="supplier_temporary_error",
            provider_payment_id="yk_fail_480",
            created_at=now - timedelta(minutes=20),
        )
        session.add_all([user, server, tariff, sub, payment])
        await session.commit()

    # Step 1: Initial urgent alert
    await alert_admins_if_problem_payments_detected(fake_tg)
    assert len(fake_tg.admin_incident_alerts) == 1
    assert "⚠️ ПРОБЛЕМНЫЕ ОПЛАТЫ" in fake_tg.admin_incident_alerts[0]["text"]
    assert len(fake_tg.admin_resolution_alerts) == 0

    # Step 2: Transition A - Payment is COMPLETED, but Telegram delivery failed
    async with db_session_factory() as session:
        p = await session.get(Payment, 480)
        p.status = PaymentStatus.COMPLETED
        p.provisioning_failure_code = None
        p.telegram_delivery_error = "bot was blocked by the user"
        await session.commit()

    # Evaluate alerts: payment is no longer urgent, but NOT clean. Resolution alert MUST NOT fire!
    await alert_admins_if_problem_payments_detected(fake_tg)
    assert len(fake_tg.admin_resolution_alerts) == 0
    assert len(fake_tg.admin_incident_alerts) == 1

    # Step 3: Transition B - Telegram delivery error resolved
    async with db_session_factory() as session:
        p = await session.get(Payment, 480)
        p.telegram_delivery_error = None
        await session.commit()

    # Evaluate alerts: now payment is genuinely clean. Resolution alert MUST fire exactly once!
    await alert_admins_if_problem_payments_detected(fake_tg)
    assert len(fake_tg.admin_resolution_alerts) == 1
    assert "ПРОБЛЕМНАЯ ОПЛАТА ОБРАБОТАНА" in fake_tg.admin_resolution_alerts[0]["text"]
    assert "payment #480" in fake_tg.admin_resolution_alerts[0]["text"]

    # Step 4: Next scheduler tick: no duplicates
    await alert_admins_if_problem_payments_detected(fake_tg)
    assert len(fake_tg.admin_resolution_alerts) == 1
    assert len(fake_tg.admin_incident_alerts) == 1


# ── ACCEPTANCE SCENARIO 5: YooKassa Competing Payment Lookup Timeout Fails Closed ─


async def test_competing_yookassa_timeout_fails_closed(monkeypatch, db_session_factory):
    """Test Blocker 3: Network timeout while querying competing payment fails closed.

    When querying YooKassa for a competing payment's status times out:
    - Competitor must be treated as a genuine competitor (fail closed).
    - Supplier API must NOT be called (0 renew calls).
    - Payment under fulfillment must fail with 'ambiguous_competing_payments_for_subscription'.
    """
    t0 = datetime(2026, 11, 6, 16, 4, 1)
    fake_adapt = FakeAdaptAPI(initial_end_date=t0)
    fake_yk = FakeYooKassa()
    fake_tg = FakeTelegramBot()

    monkeypatch.setattr("bot.services.yookassa_reconciliation.fetch_yookassa_payment", fake_yk.fetch)
    monkeypatch.setattr("bot.services.problem_payments.fetch_yookassa_payment", fake_yk.fetch)
    monkeypatch.setattr("bot.services.subscription_service.AdaptAPI", lambda *args, **kwargs: fake_adapt)
    monkeypatch.setattr("bot.services.notifications.settings.admin_ids", [999001])
    monkeypatch.setattr("bot.services.notifications.settings.owner_ids", [])
    monkeypatch.setattr("bot.services.notifications.settings.support_agent_ids", [])

    now = datetime.utcnow()
    p_created = now - timedelta(hours=1)

    async with db_session_factory() as session:
        user = User(id=92, telegram_id=929292, username="user_92")
        server = Server(id=5, name="srv5", host="5.5.5.5", location="Frankfurt", is_active=True)
        tariff = Tariff(
            id=35,
            label="1 месяц",
            price_rub=150,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            adapt_plan_uuid="plan-uuid-30",
        )
        sub = Subscription(
            id=300,
            user_id=92,
            server_id=5,
            tariff_months=1,
            tariff_days=30,
            status=SubStatus.ACTIVE,
            platform=Platform.IOS,
            client_name="adapt_timeout_test_uuid",
            vpn_key="https://darimiru.ru/sub/timeout_test",
            expires_at=t0,
        )
        adapt_sub = AdaptSubscription(
            subscription_id=300,
            adapt_uuid="timeout_test_uuid",
            adapt_plan_uuid="plan-uuid-30",
            end_date=t0,
        )
        # Payment A: candidate competitor whose status check will time out
        payment_a = Payment(
            id=601,
            user_id=92,
            tariff_id=35,
            platform="deferred~r~300",
            amount=15000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_timeout_competitor",
            provisioning_baseline_expires_at=t0,
            created_at=p_created,
        )
        # Payment B: payment being fulfilled
        payment_b = Payment(
            id=602,
            user_id=92,
            tariff_id=35,
            platform="deferred~r~300",
            amount=15000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_paid_target",
            provisioning_baseline_expires_at=t0,
            created_at=p_created + timedelta(minutes=5),
        )
        session.add_all([user, server, tariff, sub, adapt_sub, payment_a, payment_b])
        await session.commit()

    # Configure YooKassa: payment B is succeeded; payment A times out
    fake_yk.set_payment(
        "yk_paid_target",
        status="succeeded",
        paid=True,
        amount=150.0,
        metadata={"user_id": 929292, "tariff_id": 35, "platform": "deferred~r~300"},
    )
    fake_yk.set_timeout("yk_timeout_competitor")

    # Run reconciliation for Payment B
    success, code = await verify_and_fulfill_yookassa_payment(fake_tg, "yk_paid_target", source="scheduler")

    # Verification: must fail closed without mutating supplier
    assert success is False
    assert code == "ambiguous_competing_payments_for_subscription"
    assert fake_adapt.renew_call_count == 0

    async with db_session_factory() as session:
        p_b = await session.get(Payment, 602)
        assert p_b.status == PaymentStatus.PENDING
        assert p_b.provisioning_failure_code == "ambiguous_competing_payments_for_subscription"


# ── AUTONOMOUS ADAPT MUTATION RECOVERY ACCEPTANCE TESTS (A, B, C, D, E) ────────


async def test_autonomous_adapt_recovery_scenario_a_exact_314(monkeypatch, db_session_factory):
    """TEST A: Exact #314 scenario.

    Trial A (trial-plan-uuid), Paid target B (paid-plan-uuid).
    Initial ambiguous POST failed (503/timeout), Adapt remains baseline A.
    Scheduler runs:
    - Authoritative GET returns baseline A.
    - Exactly ONE safe retry POST is executed.
    - Post-retry GET verifies target plan B.
    - Completes automatically.
    - Financial side-effects committed once (cashback).
    - Customer notified once.
    - Staff resolution alert sent once.
    - Problem Payments report clean.
    - Replay is no-op (0 supplier calls, 0 financial effects).
    """
    t0 = datetime(2026, 11, 8, 7, 38, 13)
    fake_adapt = FakeAdaptAPI(initial_end_date=t0, plan_uuid="trial-plan-uuid", devices=1)
    fake_yk = FakeYooKassa()
    fake_tg = FakeTelegramBot()

    monkeypatch.setattr("bot.services.yookassa_reconciliation.fetch_yookassa_payment", fake_yk.fetch)
    monkeypatch.setattr("bot.services.problem_payments.fetch_yookassa_payment", fake_yk.fetch)
    monkeypatch.setattr("bot.services.subscription_service.AdaptAPI", lambda *args, **kwargs: fake_adapt)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.AdaptAPI", lambda *args, **kwargs: fake_adapt)
    monkeypatch.setattr("bot.services.notifications.settings.admin_ids", [999001])
    monkeypatch.setattr("bot.services.notifications.settings.owner_ids", [])
    monkeypatch.setattr("bot.services.notifications.settings.support_agent_ids", [])

    now = datetime.utcnow()
    p_created = now - timedelta(minutes=10)

    async with db_session_factory() as session:
        session.add(BotSettings(key="cashback_enabled", value="1"))
        session.add(BotSettings(key="cashback_percent", value="10.0"))

        user = User(
            id=238,
            telegram_id=925736360,
            username="AlekseiKostromin",
            platform=Platform.ANDROID,
            balance_autodebit_enabled=True,
            balance_mode_enabled=True,
        )
        server = Server(id=1, name="srv1", host="1.1.1.1", location="Helsinki", is_active=True)
        trial_tariff = Tariff(
            id=30,
            label="Пробный 3 дня",
            price_rub=0,
            price_stars=0,
            days=3,
            device_count=1,
            is_active=True,
            tariff_type=TariffType.VPN,
            adapt_plan_uuid="trial-plan-uuid",
        )
        paid_tariff = Tariff(
            id=12,
            label="1 месяц",
            price_rub=155,
            price_stars=0,
            days=30,
            device_count=3,
            is_active=True,
            tariff_type=TariffType.VPN,
            adapt_plan_uuid="paid-plan-uuid",
        )
        sub = Subscription(
            id=348,
            user_id=238,
            server_id=1,
            tariff_id=30,
            tariff_months=0,
            tariff_days=7,
            billing_mode="demo",
            device_slots=1,
            status=SubStatus.ACTIVE,
            platform=Platform.ANDROID,
            client_name="adapt_314_sub",
            vpn_key="vless://adapt-key-314@1.1.1.1:443",
            expires_at=t0,
        )
        adapt_sub = AdaptSubscription(
            subscription_id=348,
            adapt_uuid="adapt-uuid-348",
            adapt_plan_uuid="trial-plan-uuid",
            end_date=t0,
        )
        payment = Payment(
            id=314,
            user_id=238,
            tariff_id=12,
            platform="deferred~u~348",
            amount=15500,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="32820efa-000f-5000-b000-1f69428edaa3",
            provisioning_baseline_plan_uuid="trial-plan-uuid",
            provisioning_baseline_expires_at=t0,
            provisioning_failure_code="uncertain_supplier_mutation",
            created_at=p_created,
        )
        session.add_all([user, server, trial_tariff, paid_tariff, sub, adapt_sub, payment])
        await session.commit()

    # Seed alert tracker showing payment was previously alerted as urgent
    async with db_session_factory() as session:
        tracker_data = {
            "alerted_keys": ["payment:314"],
            "last_alert_at": (now - timedelta(minutes=5)).isoformat(),
        }
        session.add(BotSettings(key="problem_payments_alert_tracker", value=json.dumps(tracker_data)))
        await session.commit()

    # YooKassa: payment is succeeded and paid
    fake_yk.set_payment(
        "32820efa-000f-5000-b000-1f69428edaa3",
        status="succeeded",
        paid=True,
        amount=155.0,
        metadata={"user_id": 925736360, "tariff_id": 12, "platform": "deferred~u~348"},
    )

    # 1. Run scheduled reconciliation
    stats = await reconcile_pending_yookassa_payments(fake_tg)
    assert stats["fulfilled"] == 1

    # 2. Assert exactly ONE supplier mutation occurred
    assert fake_adapt.upgrade_call_count == 1
    assert fake_adapt.plan_uuid == "paid-plan-uuid"
    assert fake_adapt.devices == 3

    # 3. Assert DB state
    async with db_session_factory() as session:
        p = await session.get(Payment, 314)
        assert p.status == PaymentStatus.COMPLETED
        assert p.subscription_id == 348
        assert p.provisioning_failure_code is None
        assert p.telegram_delivery_error is None

        s = await session.get(Subscription, 348)
        assert s.status == SubStatus.ACTIVE
        assert s.billing_mode == "tariff"
        assert s.tariff_days == 30
        assert s.tariff_months == 1
        assert s.tariff_id == 12
        assert s.device_slots == 3
        assert s.expires_at is not None

        a = await session.scalar(select(AdaptSubscription).where(AdaptSubscription.subscription_id == 348))
        assert a.adapt_plan_uuid == "paid-plan-uuid"

        # Financial ledger effects: cashback exactly once (+15.50 RUB = 1550 kopecks)
        u = await session.get(User, 238)
        assert abs(u.balance_rub - 15.50) < 0.01
        assert u.balance_autodebit_enabled is False
        assert u.balance_mode_enabled is False

        cashback_txs = (await session.execute(
            select(BalanceTransaction).where(
                BalanceTransaction.user_id == 238,
                BalanceTransaction.kind == BalanceTransactionKind.CASHBACK,
            )
        )).scalars().all()
        assert len(cashback_txs) == 1
        assert abs(cashback_txs[0].amount_rub - 15.50) < 0.01

    # 4. Assert customer notified (delivery access messages sent to customer)
    assert len(fake_tg.customer_messages) >= 1
    assert all(m["chat_id"] == 925736360 for m in fake_tg.customer_messages)
    initial_customer_msg_count = len(fake_tg.customer_messages)

    # 5. Assert Problem Payments clean and staff resolution alert sent once
    await alert_admins_if_problem_payments_detected(fake_tg)
    assert len(fake_tg.admin_resolution_alerts) == 1
    res_text = fake_tg.admin_resolution_alerts[0]["text"]
    assert "✅ <b>ПРОБЛЕМНАЯ ОПЛАТА ОБРАБОТАНА</b>" in res_text
    assert "payment #314" in res_text

    report = await build_problem_payments_report()
    assert report.counts.get(ProblemCategory.CONFIRMED_UNFULFILLED, 0) == 0
    assert report.counts.get(ProblemCategory.FAILED_SUPPLIER, 0) == 0

    # 6. Replay safety: re-running reconciler is a pure no-op
    success, code = await verify_and_fulfill_yookassa_payment(
        fake_tg, "32820efa-000f-5000-b000-1f69428edaa3", source="webhook"
    )
    assert success is True
    assert code == "already_completed"
    assert fake_adapt.upgrade_call_count == 1  # 0 additional supplier mutations!
    assert len(fake_tg.customer_messages) == initial_customer_msg_count  # 0 duplicate customer messages!

    async with db_session_factory() as session:
        u = await session.get(User, 238)
        assert abs(u.balance_rub - 15.50) < 0.01


async def test_autonomous_adapt_recovery_scenario_b_applied_response_lost(monkeypatch, db_session_factory):
    """TEST B: POST actually mutates A->B, but response was lost (caller received 503/timeout).

    Scheduler runs:
    - Authoritative GET returns target plan B.
    - Second POST count is ZERO (0 additional mutations).
    - Payment completes automatically.
    - Finances committed once.
    - Customer notified once.
    - Staff resolution alert sent once.
    """
    t0 = datetime(2026, 11, 8, 7, 38, 13)
    # Supplier already at target state B
    fake_adapt = FakeAdaptAPI(initial_end_date=t0 + timedelta(days=30), plan_uuid="paid-plan-uuid", devices=3)
    fake_yk = FakeYooKassa()
    fake_tg = FakeTelegramBot()

    monkeypatch.setattr("bot.services.yookassa_reconciliation.fetch_yookassa_payment", fake_yk.fetch)
    monkeypatch.setattr("bot.services.problem_payments.fetch_yookassa_payment", fake_yk.fetch)
    monkeypatch.setattr("bot.services.subscription_service.AdaptAPI", lambda *args, **kwargs: fake_adapt)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.AdaptAPI", lambda *args, **kwargs: fake_adapt)
    monkeypatch.setattr("bot.services.notifications.settings.admin_ids", [999001])
    monkeypatch.setattr("bot.services.notifications.settings.owner_ids", [])
    monkeypatch.setattr("bot.services.notifications.settings.support_agent_ids", [])

    now = datetime.utcnow()
    p_created = now - timedelta(minutes=10)

    async with db_session_factory() as session:
        session.add(BotSettings(key="cashback_enabled", value="1"))
        session.add(BotSettings(key="cashback_percent", value="10.0"))

        user = User(
            id=239,
            telegram_id=925736361,
            username="test_user_b",
            platform=Platform.IOS,
            balance_autodebit_enabled=True,
            balance_mode_enabled=True,
        )
        server = Server(id=1, name="srv1", host="1.1.1.1", location="Helsinki", is_active=True)
        trial_tariff = Tariff(
            id=30,
            label="Пробный 3 дня",
            price_rub=0,
            price_stars=0,
            days=3,
            device_count=1,
            is_active=True,
            tariff_type=TariffType.VPN,
            adapt_plan_uuid="trial-plan-uuid",
        )
        paid_tariff = Tariff(
            id=12,
            label="1 месяц",
            price_rub=155,
            price_stars=0,
            days=30,
            device_count=3,
            is_active=True,
            tariff_type=TariffType.VPN,
            adapt_plan_uuid="paid-plan-uuid",
        )
        sub = Subscription(
            id=349,
            user_id=239,
            server_id=1,
            tariff_id=30,
            tariff_months=0,
            tariff_days=7,
            billing_mode="demo",
            device_slots=1,
            status=SubStatus.ACTIVE,
            platform=Platform.IOS,
            client_name="adapt_b_sub",
            vpn_key="vless://adapt-key-b@1.1.1.1:443",
            expires_at=t0,
        )
        adapt_sub = AdaptSubscription(
            subscription_id=349,
            adapt_uuid="adapt-uuid-349",
            adapt_plan_uuid="trial-plan-uuid",
            end_date=t0,
        )
        payment = Payment(
            id=315,
            user_id=239,
            tariff_id=12,
            platform="deferred~u~349",
            amount=15500,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk-pay-lost-response-315",
            provisioning_baseline_plan_uuid="trial-plan-uuid",
            provisioning_baseline_expires_at=t0,
            provisioning_failure_code="uncertain_supplier_mutation",
            created_at=p_created,
        )
        session.add_all([user, server, trial_tariff, paid_tariff, sub, adapt_sub, payment])
        await session.commit()

    # Seed alert tracker showing payment was previously alerted as urgent
    async with db_session_factory() as session:
        tracker_data = {
            "alerted_keys": ["payment:315"],
            "last_alert_at": (now - timedelta(minutes=5)).isoformat(),
        }
        session.add(BotSettings(key="problem_payments_alert_tracker", value=json.dumps(tracker_data)))
        await session.commit()

    fake_yk.set_payment(
        "yk-pay-lost-response-315",
        status="succeeded",
        paid=True,
        amount=155.0,
        metadata={"user_id": 925736361, "tariff_id": 12, "platform": "deferred~u~349"},
    )

    # 1. Run reconciliation
    success, code = await verify_and_fulfill_yookassa_payment(fake_tg, "yk-pay-lost-response-315", source="scheduler")
    assert success is True
    assert code == "subscription_completed"

    # 2. Critical invariant: second POST count = 0!
    assert fake_adapt.upgrade_call_count == 0

    # 3. Assert DB state
    async with db_session_factory() as session:
        p = await session.get(Payment, 315)
        assert p.status == PaymentStatus.COMPLETED
        assert p.subscription_id == 349
        assert p.provisioning_failure_code is None

        s = await session.get(Subscription, 349)
        assert s.status == SubStatus.ACTIVE
        assert s.billing_mode == "tariff"
        assert s.tariff_days == 30
        assert s.tariff_months == 1
        assert s.tariff_id == 12
        assert s.device_slots == 3
        assert s.expires_at is not None

        u = await session.get(User, 239)
        assert abs(u.balance_rub - 15.50) < 0.01
        assert u.balance_autodebit_enabled is False
        assert u.balance_mode_enabled is False

    # 4. Customer message received (delivery access messages sent to customer)
    assert len(fake_tg.customer_messages) >= 1
    assert all(m["chat_id"] == 925736361 for m in fake_tg.customer_messages)

    # 5. Staff resolution alert sent once
    await alert_admins_if_problem_payments_detected(fake_tg)
    assert len(fake_tg.admin_resolution_alerts) == 1
    assert "✅ <b>ПРОБЛЕМНАЯ ОПЛАТА ОБРАБОТАНА</b>" in fake_tg.admin_resolution_alerts[0]["text"]


async def test_autonomous_adapt_recovery_scenario_c_get_transient_outage(monkeypatch, db_session_factory):
    """TEST C: GET transient outage (502/503/504/timeout).

    - Ambiguous POST occurred.
    - GET returns 503/timeout -> 0 mutations attempted.
    - No alert spam during transient backoff window (<5m).
    - Outage resolves -> later GET succeeds -> recovers automatically!
    """
    t0 = datetime(2026, 11, 8, 7, 38, 13)
    fake_adapt = FakeAdaptAPI(initial_end_date=t0, plan_uuid="trial-plan-uuid", devices=1)
    fake_yk = FakeYooKassa()
    fake_tg = FakeTelegramBot()

    monkeypatch.setattr("bot.services.yookassa_reconciliation.fetch_yookassa_payment", fake_yk.fetch)
    monkeypatch.setattr("bot.services.problem_payments.fetch_yookassa_payment", fake_yk.fetch)
    monkeypatch.setattr("bot.services.subscription_service.AdaptAPI", lambda *args, **kwargs: fake_adapt)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.AdaptAPI", lambda *args, **kwargs: fake_adapt)
    monkeypatch.setattr("bot.services.notifications.settings.admin_ids", [999001])
    monkeypatch.setattr("bot.services.notifications.settings.owner_ids", [])
    monkeypatch.setattr("bot.services.notifications.settings.support_agent_ids", [])

    now = datetime.utcnow()
    # Recent payment (1 minute ago) — inside the 5-minute backoff window
    p_created = now - timedelta(minutes=1)

    async with db_session_factory() as session:
        session.add(BotSettings(key="cashback_enabled", value="1"))
        session.add(BotSettings(key="cashback_percent", value="10.0"))

        user = User(id=240, telegram_id=925736362, username="test_user_c", platform=Platform.ANDROID)
        server = Server(id=1, name="srv1", host="1.1.1.1", location="Helsinki", is_active=True)
        trial_tariff = Tariff(
            id=30, label="Пробный 3 дня", price_rub=0, days=3, device_count=1,
            is_active=True, tariff_type=TariffType.VPN, adapt_plan_uuid="trial-plan-uuid",
        )
        paid_tariff = Tariff(
            id=12, label="1 месяц", price_rub=155, days=30, device_count=3,
            is_active=True, tariff_type=TariffType.VPN, adapt_plan_uuid="paid-plan-uuid",
        )
        sub = Subscription(
            id=350, user_id=240, server_id=1, tariff_id=30, tariff_months=1, tariff_days=3,
            device_slots=1, status=SubStatus.ACTIVE, platform=Platform.ANDROID,
            client_name="adapt_c_sub", vpn_key="vless://key-c@1.1.1.1:443", expires_at=t0,
        )
        adapt_sub = AdaptSubscription(
            subscription_id=350, adapt_uuid="adapt-uuid-350", adapt_plan_uuid="trial-plan-uuid",
            end_date=t0,
        )
        payment = Payment(
            id=316, user_id=240, tariff_id=12, platform="deferred~u~350", amount=15500,
            currency="RUB", method=PaymentMethod.YOOKASSA, status=PaymentStatus.PENDING,
            provider_payment_id="yk-pay-outage-316", provisioning_baseline_plan_uuid="trial-plan-uuid",
            provisioning_baseline_expires_at=t0, provisioning_failure_code="uncertain_supplier_mutation",
            created_at=p_created,
        )
        session.add_all([user, server, trial_tariff, paid_tariff, sub, adapt_sub, payment])
        await session.commit()

    fake_yk.set_payment(
        "yk-pay-outage-316", status="succeeded", paid=True, amount=155.0,
        metadata={"user_id": 925736362, "tariff_id": 12, "platform": "deferred~u~350"},
    )

    # 1. Simulate GET transient outage (503)
    fake_adapt.get_status_error = AdaptAPIError("HTTP 503 Service Unavailable", status=503)

    # Reconciler tick during outage
    success, code = await verify_and_fulfill_yookassa_payment(fake_tg, "yk-pay-outage-316", source="scheduler")
    assert success is False
    assert code == "supplier_status_temporarily_unavailable"
    assert fake_adapt.upgrade_call_count == 0  # CRITICAL: 0 mutations while GET unavailable!

    # Problem payments check during outage: alert suppressed because age < 5m
    await alert_admins_if_problem_payments_detected(fake_tg)
    assert len(fake_tg.admin_incident_alerts) == 0  # NO alert spam!

    # 2. Outage resolves!
    fake_adapt.get_status_error = None

    # Next reconciler tick
    success, code = await verify_and_fulfill_yookassa_payment(fake_tg, "yk-pay-outage-316", source="scheduler")
    assert success is True
    assert code == "subscription_completed"

    # Exactly 1 safe retry mutation occurred now that GET is available
    assert fake_adapt.upgrade_call_count == 1
    assert fake_adapt.plan_uuid == "paid-plan-uuid"

    async with db_session_factory() as session:
        p = await session.get(Payment, 316)
        assert p.status == PaymentStatus.COMPLETED
        assert p.subscription_id == 350
        assert p.provisioning_failure_code is None


async def test_autonomous_adapt_recovery_scenario_d_contradictory_supplier_state(monkeypatch, db_session_factory):
    """TEST D: Contradictory supplier state (baseline=A, target=B, supplier=C).

    - Supplier returns an unrecognized plan C.
    - 0 mutations attempted.
    - Fails closed with 'contradictory_supplier_state'.
    - Exactly ONE actionable staff alert sent.
    """
    t0 = datetime(2026, 11, 8, 7, 38, 13)
    # Supplier has unexpected plan C
    fake_adapt = FakeAdaptAPI(initial_end_date=t0, plan_uuid="weird-third-party-plan-c", devices=1)
    fake_yk = FakeYooKassa()
    fake_tg = FakeTelegramBot()

    monkeypatch.setattr("bot.services.yookassa_reconciliation.fetch_yookassa_payment", fake_yk.fetch)
    monkeypatch.setattr("bot.services.problem_payments.fetch_yookassa_payment", fake_yk.fetch)
    monkeypatch.setattr("bot.services.subscription_service.AdaptAPI", lambda *args, **kwargs: fake_adapt)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.AdaptAPI", lambda *args, **kwargs: fake_adapt)
    monkeypatch.setattr("bot.services.notifications.settings.admin_ids", [999001])
    monkeypatch.setattr("bot.services.notifications.settings.owner_ids", [])
    monkeypatch.setattr("bot.services.notifications.settings.support_agent_ids", [])

    now = datetime.utcnow()
    p_created = now - timedelta(minutes=10)

    async with db_session_factory() as session:
        user = User(id=241, telegram_id=925736363, username="test_user_d", platform=Platform.ANDROID)
        server = Server(id=1, name="srv1", host="1.1.1.1", location="Helsinki", is_active=True)
        trial_tariff = Tariff(
            id=30, label="Пробный 3 дня", price_rub=0, days=3, device_count=1,
            is_active=True, tariff_type=TariffType.VPN, adapt_plan_uuid="trial-plan-uuid",
        )
        paid_tariff = Tariff(
            id=12, label="1 месяц", price_rub=155, days=30, device_count=3,
            is_active=True, tariff_type=TariffType.VPN, adapt_plan_uuid="paid-plan-uuid",
        )
        sub = Subscription(
            id=351, user_id=241, server_id=1, tariff_id=30, tariff_months=1, tariff_days=3,
            device_slots=1, status=SubStatus.ACTIVE, platform=Platform.ANDROID,
            client_name="adapt_d_sub", vpn_key="vless://key-d@1.1.1.1:443", expires_at=t0,
        )
        adapt_sub = AdaptSubscription(
            subscription_id=351, adapt_uuid="adapt-uuid-351", adapt_plan_uuid="trial-plan-uuid",
            end_date=t0,
        )
        payment = Payment(
            id=317, user_id=241, tariff_id=12, platform="deferred~u~351", amount=15500,
            currency="RUB", method=PaymentMethod.YOOKASSA, status=PaymentStatus.PENDING,
            provider_payment_id="yk-pay-contradictory-317", provisioning_baseline_plan_uuid="trial-plan-uuid",
            provisioning_baseline_expires_at=t0, provisioning_failure_code="uncertain_supplier_mutation",
            created_at=p_created,
        )
        session.add_all([user, server, trial_tariff, paid_tariff, sub, adapt_sub, payment])
        await session.commit()

    fake_yk.set_payment(
        "yk-pay-contradictory-317", status="succeeded", paid=True, amount=155.0,
        metadata={"user_id": 925736363, "tariff_id": 12, "platform": "deferred~u~351"},
    )

    # 1. Run reconciler
    success, code = await verify_and_fulfill_yookassa_payment(fake_tg, "yk-pay-contradictory-317", source="scheduler")
    assert success is False
    assert code == "contradictory_supplier_state"

    # CRITICAL: 0 supplier mutations attempted!
    assert fake_adapt.upgrade_call_count == 0

    async with db_session_factory() as session:
        p = await session.get(Payment, 317)
        assert p.status == PaymentStatus.PENDING
        assert p.provisioning_failure_code == "contradictory_supplier_state"

    # 2. Staff alert is sent (actionable, exactly once)
    await alert_admins_if_problem_payments_detected(fake_tg)
    assert len(fake_tg.admin_incident_alerts) == 1
    assert "contradictory_supplier_state" in fake_tg.admin_incident_alerts[0]["text"]

    # Subsequent tick does not spam
    await alert_admins_if_problem_payments_detected(fake_tg)
    assert len(fake_tg.admin_incident_alerts) == 1


async def test_autonomous_adapt_recovery_scenario_e_restart_recovery(monkeypatch, db_session_factory):
    """TEST E: Restart recovery across process / session restart.

    - Payment has durable 'uncertain_supplier_mutation' with baseline plan uuid in SQLite.
    - Process restart simulated with fresh Bot and session.
    - Recovers safely without double mutation.
    """
    t0 = datetime(2026, 11, 8, 7, 38, 13)
    fake_adapt = FakeAdaptAPI(initial_end_date=t0, plan_uuid="trial-plan-uuid", devices=1)
    fake_yk = FakeYooKassa()

    monkeypatch.setattr("bot.services.yookassa_reconciliation.fetch_yookassa_payment", fake_yk.fetch)
    monkeypatch.setattr("bot.services.problem_payments.fetch_yookassa_payment", fake_yk.fetch)
    monkeypatch.setattr("bot.services.subscription_service.AdaptAPI", lambda *args, **kwargs: fake_adapt)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.AdaptAPI", lambda *args, **kwargs: fake_adapt)
    monkeypatch.setattr("bot.services.notifications.settings.admin_ids", [999001])
    monkeypatch.setattr("bot.services.notifications.settings.owner_ids", [])
    monkeypatch.setattr("bot.services.notifications.settings.support_agent_ids", [])

    now = datetime.utcnow()
    p_created = now - timedelta(minutes=15)

    # 1. Persist durable initial state
    async with db_session_factory() as session:
        session.add(BotSettings(key="cashback_enabled", value="1"))
        session.add(BotSettings(key="cashback_percent", value="10.0"))

        user = User(id=242, telegram_id=925736364, username="test_user_e", platform=Platform.ANDROID)
        server = Server(id=1, name="srv1", host="1.1.1.1", location="Helsinki", is_active=True)
        trial_tariff = Tariff(
            id=30, label="Пробный 3 дня", price_rub=0, days=3, device_count=1,
            is_active=True, tariff_type=TariffType.VPN, adapt_plan_uuid="trial-plan-uuid",
        )
        paid_tariff = Tariff(
            id=12, label="1 месяц", price_rub=155, days=30, device_count=3,
            is_active=True, tariff_type=TariffType.VPN, adapt_plan_uuid="paid-plan-uuid",
        )
        sub = Subscription(
            id=352, user_id=242, server_id=1, tariff_id=30, tariff_months=0, tariff_days=7,
            billing_mode="demo", device_slots=1, status=SubStatus.ACTIVE, platform=Platform.ANDROID,
            client_name="adapt_e_sub", vpn_key="vless://key-e@1.1.1.1:443", expires_at=t0,
        )
        adapt_sub = AdaptSubscription(
            subscription_id=352, adapt_uuid="adapt-uuid-352", adapt_plan_uuid="trial-plan-uuid",
            end_date=t0,
        )
        payment = Payment(
            id=318, user_id=242, tariff_id=12, platform="deferred~u~352", amount=15500,
            currency="RUB", method=PaymentMethod.YOOKASSA, status=PaymentStatus.PENDING,
            provider_payment_id="yk-pay-restart-318", provisioning_baseline_plan_uuid="trial-plan-uuid",
            provisioning_baseline_expires_at=t0, provisioning_failure_code="uncertain_supplier_mutation",
            created_at=p_created,
        )
        session.add_all([user, server, trial_tariff, paid_tariff, sub, adapt_sub, payment])
        await session.commit()

    fake_yk.set_payment(
        "yk-pay-restart-318", status="succeeded", paid=True, amount=155.0,
        metadata={"user_id": 925736364, "tariff_id": 12, "platform": "deferred~u~352"},
    )

    # 2. Simulate PM2 process restart: create a new FakeTelegramBot and execute in fresh cycle
    fresh_fake_tg = FakeTelegramBot()

    success, code = await verify_and_fulfill_yookassa_payment(fresh_fake_tg, "yk-pay-restart-318", source="scheduler")
    assert success is True
    assert code == "subscription_completed"

    # Exactly 1 mutation across restart
    assert fake_adapt.upgrade_call_count == 1

    async with db_session_factory() as session:
        p = await session.get(Payment, 318)
        assert p.status == PaymentStatus.COMPLETED
        assert p.subscription_id == 352
        assert p.provisioning_failure_code is None

        s = await session.get(Subscription, 352)
        assert s.status == SubStatus.ACTIVE
        assert s.billing_mode == "tariff"
        assert s.tariff_days == 30
        assert s.tariff_months == 1
        assert s.tariff_id == 12
        assert s.device_slots == 3
