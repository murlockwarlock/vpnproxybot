"""Comprehensive test suite for YooKassa payment verification and reconciliation."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.models import (
    AdminActionLog,
    Base,
    BalanceTopUp,
    BalanceTransaction,
    BalanceTransactionKind,
    Partner,
    PartnerEarning,
    Payment,
    PaymentNotificationOutbox,
    PaymentMethod,
    PaymentStatus,
    Platform,
    ReferralConfig,
    ReferralEarning,
    ReferralPaymentLog,
    Server,
    Subscription,
    Tariff,
    TariffType,
    User,
)
from bot.services.cluster import LeaseManager
from bot.services.payment_service import credit_cashback, credit_partner, credit_referral
from bot.services.provisioning_issues import AccessProvisionError, build_adapt_access_error
from bot.services.yookassa_reconciliation import (
    _schedule_payment_retry,
    reconcile_pending_yookassa_payments,
    verify_and_fulfill_yookassa_payment,
)
from bot.webhooks import handle_yookassa

pytestmark = pytest.mark.asyncio


class _Request(SimpleNamespace):
    async def json(self):
        return self._json_body


async def test_adapt_http_402_is_confirmed_safe_pre_mutation_rejection():
    issue = build_adapt_access_error(status=402, message="Insufficient balance")

    assert issue.code == "adapt_api_402"
    assert issue.stage == "pre_mutation"
    assert issue.confirmed_rejection is True
    assert issue.safe_retry_eligible is True


async def test_waiting_funds_retry_schedule_is_durable_and_backed_off():
    payment = Payment(
        reconciliation_attempts=0,
        next_attempt_at=None,
    )
    base = datetime(2026, 1, 1, 12, 0, 0)

    _schedule_payment_retry(payment, "vhq_balance", now=base)
    first_due = payment.next_attempt_at
    _schedule_payment_retry(payment, "vhq_balance", now=base)

    assert payment.reconciliation_attempts == 2
    assert first_due == base + timedelta(minutes=5)
    assert payment.next_attempt_at == base + timedelta(minutes=15)


async def test_delayed_yookassa_payment_enqueues_one_neutral_customer_ack(
    monkeypatch, db_session_factory
):
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.cluster.LeaseManager.acquire_or_renew", AsyncMock(return_value=True))
    monkeypatch.setattr("bot.services.cluster.LeaseManager.release", AsyncMock())
    monkeypatch.setattr("bot.services.cluster.LeaseManager.is_owned_by_me", AsyncMock(return_value=True))

    async with db_session_factory() as session:
        user = User(telegram_id=888000, full_name="Delayed User")
        tariff = Tariff(
            id=600,
            label="VHQ delayed",
            price_rub=250,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            vhq_tier="basic",
        )
        payment = Payment(
            user_id=1,
            tariff_id=600,
            platform=Platform.ANDROID.value,
            amount=25000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_delayed_ack_600",
        )
        session.add_all([user, tariff])
        await session.flush()
        payment.user_id = user.id
        session.add(payment)
        await session.commit()

    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.fetch_yookassa_payment",
        AsyncMock(
            return_value={
                "id": "yk_delayed_ack_600",
                "status": "succeeded",
                "paid": True,
                "amount": {"value": "250.00", "currency": "RUB"},
                "metadata": {
                    "user_id": str(user.telegram_id),
                    "chat_id": str(user.telegram_id),
                    "tariff_id": "600",
                    "platform": Platform.ANDROID.value,
                },
            }
        ),
    )
    async def supplier_attempt(*args, **kwargs):
        async with db_session_factory() as session:
            ack = await session.scalar(
                select(PaymentNotificationOutbox).where(
                    PaymentNotificationOutbox.payment_id == payment.id,
                    PaymentNotificationOutbox.kind == "payment_received",
                )
            )
            assert ack is not None
        raise AccessProvisionError(
            provider="vhq",
            code="vhq_balance",
            client_message="internal supplier details must not be sent",
            admin_message="Insufficient balance",
            status=402,
            stage="pre_mutation",
            confirmed_rejection=True,
            safe_retry_eligible=True,
        )

    supplier = AsyncMock(side_effect=supplier_attempt)
    monkeypatch.setattr("bot.webhooks._process_and_deliver", supplier)
    bot_mock = SimpleNamespace(send_message=AsyncMock())

    first = await verify_and_fulfill_yookassa_payment(bot_mock, "yk_delayed_ack_600")
    second = await verify_and_fulfill_yookassa_payment(bot_mock, "yk_delayed_ack_600")

    assert first == (False, "delivery_failed:vhq_balance")
    assert second == (False, "delivery_failed:vhq_balance")
    supplier.assert_awaited_once()

    async with db_session_factory() as session:
        rows = (
            await session.scalars(
                select(PaymentNotificationOutbox).where(
                    PaymentNotificationOutbox.payment_id == payment.id,
                    PaymentNotificationOutbox.kind == "payment_received",
                )
            )
        ).all()
        assert len(rows) == 1
        assert "vhq_balance" not in rows[0].payload_json
        assert "Insufficient balance" not in rows[0].payload_json

    assert bot_mock.send_message.await_count == 1
    assert "✅ Оплата получена." in bot_mock.send_message.await_args.args[1]
    assert "VPN-доступ готовится" in bot_mock.send_message.await_args.args[1]


@pytest.mark.parametrize(
    "failure_code",
    ["uncertain_supplier_mutation", "ambiguous_buy_mutation_manual_review_required"],
)
async def test_problem_supplier_state_gets_durable_customer_ack_without_retry(
    monkeypatch, db_session_factory, failure_code
):
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.cluster.LeaseManager.acquire_or_renew", AsyncMock(return_value=True))
    monkeypatch.setattr("bot.services.cluster.LeaseManager.release", AsyncMock())
    monkeypatch.setattr("bot.services.cluster.LeaseManager.is_owned_by_me", AsyncMock(return_value=True))
    notify_admins = AsyncMock()
    monkeypatch.setattr("bot.services.yookassa_reconciliation.notify_admins_issue", notify_admins)

    class DisabledVhq:
        enabled = False

    monkeypatch.setattr("bot.services.vhq_partner_api.VHQPartnerAPI", DisabledVhq)

    async with db_session_factory() as session:
        user = User(telegram_id=888003, full_name="Historical uncertain user")
        tariff = Tariff(
            id=603,
            label="VHQ historical uncertain",
            price_rub=250,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            vhq_tier="basic",
        )
        payment = Payment(
            user_id=1,
            tariff_id=603,
            platform=Platform.ANDROID.value,
            amount=25000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_historical_uncertain_603",
            provider_status="succeeded",
            provider_paid=True,
            provisioning_failure_code=failure_code,
            created_at=datetime.utcnow() - timedelta(days=30),
        )
        session.add_all([user, tariff])
        await session.flush()
        payment.user_id = user.id
        session.add(payment)
        await session.commit()

    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.fetch_yookassa_payment",
        AsyncMock(
            return_value={
                "id": "yk_historical_uncertain_603",
                "status": "succeeded",
                "paid": True,
                "amount": {"value": "250.00", "currency": "RUB"},
                "metadata": {
                    "user_id": str(user.telegram_id),
                    "chat_id": str(user.telegram_id),
                    "tariff_id": "603",
                    "platform": Platform.ANDROID.value,
                },
            }
        ),
    )
    supplier = AsyncMock(side_effect=AssertionError("uncertain mutation must stay fail-closed"))
    monkeypatch.setattr("bot.webhooks._process_and_deliver", supplier)
    bot_mock = SimpleNamespace(send_message=AsyncMock())

    result = await verify_and_fulfill_yookassa_payment(
        bot_mock,
        "yk_historical_uncertain_603",
        source="scheduler",
    )

    assert result[0] is False
    assert result[1] in {
        failure_code,
        "supplier_mutation_in_flight_or_uncertain",
        "vhq_api_not_configured",
    }
    supplier.assert_not_awaited()
    assert bot_mock.send_message.await_count == 1
    assert bot_mock.send_message.await_args.args[0] == user.telegram_id
    assert "✅ Оплата получена." in bot_mock.send_message.await_args.args[1]
    assert failure_code not in bot_mock.send_message.await_args.args[1]
    notify_admins.assert_not_awaited()

    async with db_session_factory() as session:
        rows = (
            await session.scalars(
                select(PaymentNotificationOutbox).where(
                    PaymentNotificationOutbox.payment_id == payment.id,
                    PaymentNotificationOutbox.kind == "payment_received",
                )
            )
        ).all()
        assert len(rows) == 1


async def test_conflicting_yookassa_recipient_identifiers_do_not_receive_payment_ack(
    monkeypatch, db_session_factory
):
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.cluster.LeaseManager.acquire_or_renew", AsyncMock(return_value=True))
    monkeypatch.setattr("bot.services.cluster.LeaseManager.release", AsyncMock())
    monkeypatch.setattr("bot.services.cluster.LeaseManager.is_owned_by_me", AsyncMock(return_value=True))
    notify_admins = AsyncMock()
    monkeypatch.setattr("bot.services.yookassa_reconciliation.notify_admins_issue", notify_admins)

    async with db_session_factory() as session:
        user = User(telegram_id=888007, full_name="Trusted payment owner")
        tariff = Tariff(
            id=607,
            label="VHQ recipient conflict",
            price_rub=250,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            vhq_tier="basic",
        )
        payment = Payment(
            user_id=1,
            tariff_id=607,
            platform=Platform.ANDROID.value,
            amount=25000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_recipient_conflict_607",
            provider_status="succeeded",
            provider_paid=True,
            provisioning_failure_code="uncertain_supplier_mutation",
        )
        session.add_all([user, tariff])
        await session.flush()
        payment.user_id = user.id
        session.add(payment)
        await session.commit()

    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.fetch_yookassa_payment",
        AsyncMock(
            return_value={
                "id": "yk_recipient_conflict_607",
                "status": "succeeded",
                "paid": True,
                "amount": {"value": "250.00", "currency": "RUB"},
                "metadata": {
                    "user_id": str(user.telegram_id),
                    "chat_id": "999999007",
                    "tariff_id": "607",
                    "platform": Platform.ANDROID.value,
                },
            }
        ),
    )
    bot_mock = SimpleNamespace(send_message=AsyncMock())

    result = await verify_and_fulfill_yookassa_payment(
        bot_mock,
        "yk_recipient_conflict_607",
        source="scheduler",
    )

    assert result == (False, "user_mismatch")
    bot_mock.send_message.assert_not_awaited()
    notify_admins.assert_awaited_once()

    async with db_session_factory() as session:
        rows = (
            await session.scalars(
                select(PaymentNotificationOutbox).where(
                    PaymentNotificationOutbox.payment_id == payment.id,
                    PaymentNotificationOutbox.kind == "payment_received",
                )
            )
        ).all()
        assert rows == []


async def test_yookassa_without_precreated_payment_remains_reconcilable_after_funds_failure(
    monkeypatch, db_session_factory
):
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.cluster.LeaseManager.acquire_or_renew", AsyncMock(return_value=True))
    monkeypatch.setattr("bot.services.cluster.LeaseManager.release", AsyncMock())
    monkeypatch.setattr("bot.services.cluster.LeaseManager.is_owned_by_me", AsyncMock(return_value=True))

    async with db_session_factory() as session:
        user = User(telegram_id=888004, full_name="No precreated payment")
        tariff = Tariff(
            id=604,
            label="VHQ no precreated",
            price_rub=250,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            vhq_tier="basic",
        )
        session.add_all([user, tariff])
        await session.commit()

    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.fetch_yookassa_payment",
        AsyncMock(
            return_value={
                "id": "yk_no_precreated_604",
                "status": "succeeded",
                "paid": True,
                "amount": {"value": "250.00", "currency": "RUB"},
                "metadata": {
                    "user_id": str(user.telegram_id),
                    "chat_id": str(user.telegram_id),
                    "tariff_id": "604",
                    "platform": Platform.ANDROID.value,
                },
            }
        ),
    )
    monkeypatch.setattr(
        "bot.webhooks._process_and_deliver",
        AsyncMock(
            side_effect=AccessProvisionError(
                provider="vhq",
                code="vhq_balance",
                client_message="hidden",
                admin_message="Insufficient balance",
                status=400,
                stage="pre_mutation",
                confirmed_rejection=True,
                safe_retry_eligible=True,
            )
        ),
    )
    bot_mock = SimpleNamespace(send_message=AsyncMock())
    request = _Request(
        _json_body={
            "event": "payment.succeeded",
            "object": {
                "id": "yk_no_precreated_604",
                "metadata": {
                    "user_id": str(user.telegram_id),
                    "chat_id": str(user.telegram_id),
                    "tariff_id": "604",
                    "platform": Platform.ANDROID.value,
                },
            },
        },
        headers={},
        remote="127.0.0.1",
        app={"bot": bot_mock},
    )

    response = await handle_yookassa(request)
    assert response.status == 503

    async with db_session_factory() as session:
        waiting = await session.scalar(
            select(Payment).where(Payment.provider_payment_id == "yk_no_precreated_604")
        )
        assert waiting.status == PaymentStatus.PENDING
        assert waiting.subscription_id is None
        assert waiting.provider_status == "succeeded"
        assert waiting.provider_paid is True
        assert waiting.provisioning_failure_code == "vhq_balance"
        assert waiting.next_attempt_at is not None
        waiting.created_at = datetime.utcnow() - timedelta(hours=72)
        waiting.next_attempt_at = datetime.utcnow()
        await session.commit()

    verify_mock = AsyncMock(return_value=(False, "delivery_failed:vhq_balance"))
    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.verify_and_fulfill_yookassa_payment",
        verify_mock,
    )
    stats = await reconcile_pending_yookassa_payments(bot_mock)
    assert stats["scanned"] == 1
    verify_mock.assert_awaited_once_with(
        bot_mock,
        "yk_no_precreated_604",
        source="scheduler",
    )


async def test_unexpected_yookassa_failure_uses_durable_neutral_ack(
    monkeypatch, db_session_factory
):
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr("bot.webhooks.notify_admins_issue", AsyncMock())
    monkeypatch.setattr("bot.webhooks.notify_expiring", AsyncMock())
    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.verify_and_fulfill_yookassa_payment",
        AsyncMock(side_effect=RuntimeError("internal fulfillment crash")),
    )

    async with db_session_factory() as session:
        user = User(telegram_id=888005, full_name="Unexpected failure user")
        session.add(user)
        await session.flush()
        payment = Payment(
            user_id=user.id,
            amount=25000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_unexpected_ack_605",
            provider_status="succeeded",
            provider_paid=True,
            telegram_chat_id=user.telegram_id,
        )
        session.add(payment)
        await session.commit()

    bot_mock = SimpleNamespace(send_message=AsyncMock())
    request = _Request(
        _json_body={
            "event": "payment.succeeded",
            "object": {
                "id": "yk_unexpected_ack_605",
                "metadata": {"chat_id": str(user.telegram_id)},
            },
        },
        headers={},
        remote="127.0.0.1",
        app={"bot": bot_mock},
    )

    response = await handle_yookassa(request)

    assert response.status == 503
    neutral_text = "✅ Оплата получена. Ваш VPN-доступ готовится. Мы автоматически отправим его сюда после активации"
    assert [call.args[1] for call in bot_mock.send_message.await_args_list] == [neutral_text]
    from bot.models import PaymentNotificationOutbox

    async with db_session_factory() as session:
        row = await session.scalar(
            select(PaymentNotificationOutbox).where(
                PaymentNotificationOutbox.payment_id == payment.id,
                PaymentNotificationOutbox.kind == "payment_received",
            )
        )
        assert row.status == "sent"


async def test_successful_yookassa_fulfillment_uses_subscription_delivery_outbox(
    monkeypatch, db_session_factory
):
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.cluster.LeaseManager.acquire_or_renew", AsyncMock(return_value=True))
    monkeypatch.setattr("bot.services.cluster.LeaseManager.release", AsyncMock())
    monkeypatch.setattr("bot.services.cluster.LeaseManager.is_owned_by_me", AsyncMock(return_value=True))
    monkeypatch.setattr("bot.services.yookassa_reconciliation.notify_admins_payment", AsyncMock())

    async with db_session_factory() as session:
        user = User(telegram_id=888006, full_name="Ready User", platform=Platform.ANDROID)
        tariff = Tariff(
            id=606,
            label="Ready VHQ",
            price_rub=250,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            vhq_tier="basic",
        )
        server = Server(id=606, name="ready-vhq", host="ready-vhq.test", location="test")
        subscription = Subscription(
            id=606,
            user_id=1,
            server_id=606,
            tariff_id=606,
            tariff_months=1,
            tariff_days=30,
            status="active",
            platform=Platform.ANDROID,
            client_name="vhq_ready_606",
            vpn_key="https://sub.example.test/ready-606",
            expires_at=datetime.utcnow() + timedelta(days=30),
        )
        session.add_all([user, tariff, server])
        await session.flush()
        subscription.user_id = user.id
        session.add(subscription)
        await session.flush()
        payment = Payment(
            user_id=user.id,
            tariff_id=606,
            platform=Platform.ANDROID.value,
            amount=25000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_ready_outbox_606",
        )
        session.add(payment)
        await session.commit()

    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.fetch_yookassa_payment",
        AsyncMock(
            return_value={
                "id": "yk_ready_outbox_606",
                "status": "succeeded",
                "paid": True,
                "amount": {"value": "250.00", "currency": "RUB"},
                "metadata": {
                    "user_id": str(user.telegram_id),
                    "chat_id": str(user.telegram_id),
                    "tariff_id": "606",
                    "platform": Platform.ANDROID.value,
                },
            }
        ),
    )
    monkeypatch.setattr(
        "bot.webhooks._process_and_deliver",
        AsyncMock(return_value=(user.id, subscription.id)),
    )
    monkeypatch.setattr(
        "bot.webhooks.send_subscription_delivery_messages",
        AsyncMock(return_value=(True, None)),
    )
    bot_mock = SimpleNamespace(send_message=AsyncMock())

    success, code = await verify_and_fulfill_yookassa_payment(
        bot_mock, "yk_ready_outbox_606"
    )

    assert success is True
    assert code == "subscription_completed"
    async with db_session_factory() as session:
        rows = (
            await session.scalars(
                select(PaymentNotificationOutbox).where(
                    PaymentNotificationOutbox.payment_id == payment.id,
                )
            )
        ).all()
        assert [row.kind for row in rows] == ["payment_received", "subscription_delivery"]
        assert rows[0].status == "sent"
        assert "https://sub.example.test/ready-606" not in rows[1].payload_json


async def test_key_delivery_failure_keeps_outbox_and_sends_neutral_ack_without_repurchase(
    monkeypatch, db_session_factory
):
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.cluster.LeaseManager.acquire_or_renew", AsyncMock(return_value=True))
    monkeypatch.setattr("bot.services.cluster.LeaseManager.release", AsyncMock())
    monkeypatch.setattr("bot.services.cluster.LeaseManager.is_owned_by_me", AsyncMock(return_value=True))
    monkeypatch.setattr("bot.services.yookassa_reconciliation.notify_admins_payment", AsyncMock())

    async with db_session_factory() as session:
        user = User(telegram_id=888009, full_name="Delivery retry user")
        tariff = Tariff(
            id=609,
            label="Delivery retry",
            price_rub=250,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            vhq_tier="basic",
        )
        server = Server(id=609, name="delivery-retry", host="delivery-retry.test", location="test")
        session.add_all([user, tariff, server])
        await session.flush()
        payment = Payment(
            user_id=user.id,
            tariff_id=609,
            platform=Platform.ANDROID.value,
            amount=25000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_delivery_retry_609",
        )
        session.add(payment)
        await session.commit()

    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.fetch_yookassa_payment",
        AsyncMock(
            return_value={
                "id": "yk_delivery_retry_609",
                "status": "succeeded",
                "paid": True,
                "amount": {"value": "250.00", "currency": "RUB"},
                "metadata": {
                    "user_id": str(user.telegram_id),
                    "chat_id": str(user.telegram_id),
                    "tariff_id": "609",
                    "platform": Platform.ANDROID.value,
                },
            }
        ),
    )
    class FakeVhqPartnerApi:
        enabled = True
        buy_calls = 0

        async def buy(self, *, tier, days):
            type(self).buy_calls += 1
            return {
                "order_id": "vhq_delivery_retry_609",
                "config_url": "https://sub.example.test/delivery-retry-609",
                "active_until": (datetime.utcnow() + timedelta(days=30)).isoformat(),
            }

        @staticmethod
        def extract_subscription_url(data):
            return str(data.get("config_url") or "")

    monkeypatch.setattr("bot.services.subscription_service.VHQPartnerAPI", FakeVhqPartnerApi)
    monkeypatch.setattr("bot.services.vhq_partner_api.VHQPartnerAPI", FakeVhqPartnerApi)
    key_delivery = AsyncMock(return_value=(False, "TelegramNetworkTimeout"))
    monkeypatch.setattr("bot.webhooks.send_subscription_delivery_messages", key_delivery)
    bot_mock = SimpleNamespace(send_message=AsyncMock())

    first = await verify_and_fulfill_yookassa_payment(bot_mock, "yk_delivery_retry_609")
    replay = await verify_and_fulfill_yookassa_payment(bot_mock, "yk_delivery_retry_609")

    assert first == (True, "subscription_completed")
    assert replay == (True, "already_completed")
    assert FakeVhqPartnerApi.buy_calls == 1
    key_delivery.assert_awaited_once()
    assert bot_mock.send_message.await_count == 1
    assert "✅ Оплата получена." in bot_mock.send_message.await_args.args[1]

    async with db_session_factory() as session:
        rows = (
            await session.scalars(
                select(PaymentNotificationOutbox).where(
                    PaymentNotificationOutbox.payment_id == payment.id,
                )
            )
        ).all()
        rows_by_kind = {row.kind: row for row in rows}
        assert rows_by_kind["subscription_delivery"].status == "pending"
        assert rows_by_kind["payment_received"].status == "sent"


async def test_real_yookassa_adapt_402_waits_past_24h_and_delivers_after_topup(
    monkeypatch, db_session_factory
):
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.handlers.payment.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.guide_service.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.cluster.LeaseManager.acquire_or_renew", AsyncMock(return_value=True))
    monkeypatch.setattr("bot.services.cluster.LeaseManager.release", AsyncMock())
    monkeypatch.setattr("bot.services.cluster.LeaseManager.is_owned_by_me", AsyncMock(return_value=True))
    monkeypatch.setattr("bot.services.yookassa_reconciliation.notify_admins_payment", AsyncMock())

    async with db_session_factory() as session:
        user = User(telegram_id=888007, full_name="Adapt delayed user")
        tariff = Tariff(
            id=607,
            label="Adapt delayed",
            price_rub=250,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            adapt_plan_uuid="adapt-plan-607",
        )
        server = Server(id=607, name="adapt-delayed", host="adapt-delayed.test", location="test")
        payment = Payment(
            user_id=1,
            tariff_id=607,
            platform=Platform.ANDROID.value,
            amount=25000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_adapt_delayed_607",
        )
        session.add_all([user, tariff, server])
        await session.flush()
        payment.user_id = user.id
        session.add(payment)
        await session.commit()

    from bot.services.adapt_api import AdaptAPIError

    class FakeAdaptAPI:
        enabled = False
        create_attempts = 0

        async def create_subscription(self, plan_uuid, *, external_user_id=None):
            type(self).create_attempts += 1
            if type(self).create_attempts == 1:
                raise AdaptAPIError("Insufficient balance", status=402)
            return {
                "subscription_uuid": "adapt-e2e-607",
                "subscription_url": "https://adapt.test/sub/adapt-e2e-607",
                "plan_uuid": plan_uuid,
                "days": 30,
                "devices": 3,
            }

    monkeypatch.setattr("bot.services.subscription_service.AdaptAPI", FakeAdaptAPI)
    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.fetch_yookassa_payment",
        AsyncMock(
            return_value={
                "id": "yk_adapt_delayed_607",
                "status": "succeeded",
                "paid": True,
                "amount": {"value": "250.00", "currency": "RUB"},
                "metadata": {
                    "user_id": str(user.telegram_id),
                    "chat_id": str(user.telegram_id),
                    "tariff_id": "607",
                    "platform": Platform.ANDROID.value,
                },
            }
        ),
    )

    fake_bot = AsyncMock()
    webhook_request = _Request(
        _json_body={
            "event": "payment.succeeded",
            "object": {
                "id": "yk_adapt_delayed_607",
                "metadata": {
                    "user_id": str(user.telegram_id),
                    "chat_id": str(user.telegram_id),
                    "tariff_id": "607",
                    "platform": Platform.ANDROID.value,
                },
            },
        },
        headers={},
        remote="127.0.0.1",
        app={"bot": fake_bot},
    )

    response = await handle_yookassa(webhook_request)
    assert response.status == 503
    assert FakeAdaptAPI.create_attempts == 1

    neutral_text = "✅ Оплата получена. Ваш VPN-доступ готовится. Мы автоматически отправим его сюда после активации"
    assert [call.args[1] for call in fake_bot.send_message.await_args_list].count(neutral_text) == 1

    async with db_session_factory() as session:
        waiting = await session.get(Payment, payment.id)
        waiting.created_at = datetime.utcnow() - timedelta(hours=72)
        waiting.next_attempt_at = datetime.utcnow()
        await session.commit()

    stats = await reconcile_pending_yookassa_payments(fake_bot)
    assert stats["fulfilled"] == 1
    assert FakeAdaptAPI.create_attempts == 2

    async with db_session_factory() as session:
        completed = await session.get(Payment, payment.id)
        assert completed.status == PaymentStatus.COMPLETED
        assert completed.provisioning_failure_code is None
        assert completed.next_attempt_at is None
        subscription = await session.get(Subscription, completed.subscription_id)
        assert subscription.vpn_key.endswith("/adapt-sub/adapt-e2e-607")

    sent_texts = [call.args[1] for call in fake_bot.send_message.await_args_list]
    assert sent_texts.count(neutral_text) == 1
    assert any("adapt-e2e-607" in text for text in sent_texts)

    sent_count = fake_bot.send_message.await_count
    replay = await reconcile_pending_yookassa_payments(fake_bot)
    assert replay["fulfilled"] == 0
    assert FakeAdaptAPI.create_attempts == 2
    assert fake_bot.send_message.await_count == sent_count


async def test_72_hour_confirmed_vhq_funds_wait_can_resume_without_manual_review(
    monkeypatch, db_session_factory
):
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.cluster.LeaseManager.acquire_or_renew", AsyncMock(return_value=True))
    monkeypatch.setattr("bot.services.cluster.LeaseManager.release", AsyncMock())
    monkeypatch.setattr("bot.services.cluster.LeaseManager.is_owned_by_me", AsyncMock(return_value=True))
    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.process_payment_notification_outbox",
        AsyncMock(),
    )
    monkeypatch.setattr("bot.services.yookassa_reconciliation.notify_admins_payment", AsyncMock())

    class DisabledVhq:
        enabled = False

    monkeypatch.setattr("bot.services.vhq_partner_api.VHQPartnerAPI", DisabledVhq)

    created_at = datetime.utcnow() - timedelta(hours=72)
    async with db_session_factory() as session:
        user = User(telegram_id=888072, full_name="Old VHQ User")
        tariff = Tariff(
            id=672,
            label="Old VHQ",
            price_rub=250,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            vhq_tier="basic",
        )
        server = Server(id=672, name="old-vhq", host="old-vhq.test", location="test")
        session.add_all([user, tariff, server])
        await session.flush()
        subscription = Subscription(
            id=672,
            user_id=user.id,
            server_id=server.id,
            tariff_id=tariff.id,
            tariff_months=1,
            tariff_days=30,
            status="active",
            platform=Platform.ANDROID,
            client_name="vhq_old_672",
            vpn_key="https://sub.example.test/old-672",
            expires_at=datetime.utcnow() + timedelta(days=30),
        )
        payment = Payment(
            user_id=user.id,
            tariff_id=tariff.id,
            platform=Platform.ANDROID.value,
            amount=25000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_old_vhq_672",
            provider_status="succeeded",
            provider_paid=True,
            provisioning_failure_code="vhq_balance",
            created_at=created_at,
        )
        session.add_all([subscription, payment])
        await session.commit()

    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.fetch_yookassa_payment",
        AsyncMock(
            return_value={
                "id": "yk_old_vhq_672",
                "status": "succeeded",
                "paid": True,
                "amount": {"value": "250.00", "currency": "RUB"},
                "metadata": {
                    "user_id": "888072",
                    "chat_id": "888072",
                    "tariff_id": "672",
                    "platform": Platform.ANDROID.value,
                },
            }
        ),
    )
    monkeypatch.setattr(
        "bot.webhooks._process_and_deliver",
        AsyncMock(return_value=(user.id, subscription.id)),
    )

    success, code = await verify_and_fulfill_yookassa_payment(
        SimpleNamespace(send_message=AsyncMock()), "yk_old_vhq_672", source="scheduler"
    )

    assert success is True
    assert code == "subscription_completed"
    async with db_session_factory() as session:
        completed = await session.scalar(
            select(Payment).where(Payment.provider_payment_id == "yk_old_vhq_672")
        )
        assert completed.status == PaymentStatus.COMPLETED
        assert completed.provisioning_failure_code is None
        assert completed.next_attempt_at is None


async def test_old_adapt_funds_wait_renewal_bypasses_only_the_age_gate(
    monkeypatch, db_session_factory
):
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.cluster.LeaseManager.acquire_or_renew", AsyncMock(return_value=True))
    monkeypatch.setattr("bot.services.cluster.LeaseManager.release", AsyncMock())
    monkeypatch.setattr("bot.services.cluster.LeaseManager.is_owned_by_me", AsyncMock(return_value=True))
    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.process_payment_notification_outbox",
        AsyncMock(),
    )
    monkeypatch.setattr("bot.services.yookassa_reconciliation.notify_admins_payment", AsyncMock())

    class DisabledAdapt:
        enabled = False

    monkeypatch.setattr("bot.services.yookassa_reconciliation.AdaptAPI", DisabledAdapt)

    async with db_session_factory() as session:
        user = User(telegram_id=888073, full_name="Old Adapt User")
        tariff = Tariff(
            id=673,
            label="Old Adapt",
            price_rub=250,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            adapt_plan_uuid="plan-673",
        )
        server = Server(id=673, name="old-adapt", host="old-adapt.test", location="test")
        session.add_all([user, tariff, server])
        await session.flush()
        subscription = Subscription(
            id=673,
            user_id=user.id,
            server_id=server.id,
            tariff_id=tariff.id,
            tariff_months=1,
            tariff_days=30,
            status="active",
            platform=Platform.ANDROID,
            client_name="adapt_00000000-0000-4000-8000-000000000673",
            vpn_key="https://sub.example.test/adapt-673",
            expires_at=datetime.utcnow() + timedelta(days=30),
        )
        payment = Payment(
            user_id=user.id,
            tariff_id=tariff.id,
            platform="deferred~r~673",
            amount=25000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_old_adapt_673",
            provider_status="succeeded",
            provider_paid=True,
            provisioning_failure_code="adapt_api_402",
            created_at=datetime.utcnow() - timedelta(days=30),
        )
        session.add_all([subscription, payment])
        await session.commit()

    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.fetch_yookassa_payment",
        AsyncMock(
            return_value={
                "id": "yk_old_adapt_673",
                "status": "succeeded",
                "paid": True,
                "amount": {"value": "250.00", "currency": "RUB"},
                "metadata": {
                    "user_id": "888073",
                    "chat_id": "888073",
                    "tariff_id": "673",
                    "platform": "deferred~r~673",
                },
            }
        ),
    )
    process_mock = AsyncMock(return_value=(user.id, subscription.id))
    monkeypatch.setattr("bot.webhooks._process_and_deliver", process_mock)

    success, code = await verify_and_fulfill_yookassa_payment(
        SimpleNamespace(send_message=AsyncMock()), "yk_old_adapt_673", source="scheduler"
    )

    assert success is True
    assert code == "subscription_completed"
    process_mock.assert_awaited_once()


async def test_reconciliation_fair_slice_keeps_new_paid_rows_visible(
    monkeypatch, db_session_factory
):
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    now = datetime.utcnow()
    async with db_session_factory() as session:
        user = User(telegram_id=888074, full_name="Queue User")
        session.add(user)
        await session.flush()
        old_rows = [
            Payment(
                user_id=user.id,
                amount=25000,
                currency="RUB",
                method=PaymentMethod.YOOKASSA,
                status=PaymentStatus.PENDING,
                provider_payment_id=f"yk_old_queue_{index}",
                provider_status="succeeded",
                provider_paid=True,
                provisioning_failure_code="vhq_balance",
                created_at=now - timedelta(days=7, minutes=index),
                next_attempt_at=now - timedelta(minutes=1),
            )
            for index in range(60)
        ]
        new_rows = [
            Payment(
                user_id=user.id,
                amount=25000,
                currency="RUB",
                method=PaymentMethod.YOOKASSA,
                status=PaymentStatus.PENDING,
                provider_payment_id=f"yk_new_queue_{index}",
                provider_status="succeeded",
                provider_paid=True,
                created_at=now - timedelta(minutes=5 - index),
            )
            for index in range(3)
        ]
        session.add_all([*old_rows, *new_rows])
        await session.commit()

    seen: list[str] = []

    async def reconcile_mock(bot, provider_payment_id, source="scheduler"):
        seen.append(provider_payment_id)
        return False, "delivery_failed:vhq_balance"

    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.verify_and_fulfill_yookassa_payment",
        reconcile_mock,
    )

    stats = await reconcile_pending_yookassa_payments(SimpleNamespace())

    assert stats["scanned"] <= 50
    assert {f"yk_new_queue_{index}" for index in range(3)}.issubset(seen)


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


# ── Test 1: Recover Lost Webhook via Scheduled Reconciliation ───────────────


async def test_reconciliation_recovers_pending_subscription_lost_webhook(monkeypatch, db_session_factory):
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)

    async with db_session_factory() as session:
        user = User(telegram_id=888001, username="lostuser", full_name="Lost Webhook User")
        tariff = Tariff(
            id=60,
            label="Премиум 30 дней",
            price_rub=300,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
        )
        session.add_all([user, tariff])
        await session.flush()

        from datetime import datetime, timedelta
        created_time = datetime.utcnow() - timedelta(minutes=10)
        pending_payment = Payment(
            user_id=user.id,
            tariff_id=60,
            platform=Platform.ANDROID.value,
            amount=30000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_lost_hook_1",
            created_at=created_time,
        )
        session.add(pending_payment)
        await session.commit()
        user_id = user.id

    process_and_deliver = AsyncMock(return_value=(user_id, 8001))
    monkeypatch.setattr("bot.webhooks._process_and_deliver", process_and_deliver)

    verified_payment = {
        "id": "yk_lost_hook_1",
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "300.00", "currency": "RUB"},
        "metadata": {
            "user_id": "888001",
            "chat_id": "888001",
            "tariff_id": "60",
            "platform": Platform.ANDROID.value,
        },
    }
    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.fetch_yookassa_payment",
        AsyncMock(return_value=verified_payment),
    )

    bot_mock = SimpleNamespace(send_message=AsyncMock())
    stats = await reconcile_pending_yookassa_payments(bot_mock)

    assert stats["scanned"] == 1
    assert stats["fulfilled"] == 1

    async with db_session_factory() as session:
        payment = await session.scalar(
            select(Payment).where(Payment.provider_payment_id == "yk_lost_hook_1")
        )
        assert payment is not None
        assert payment.status == PaymentStatus.COMPLETED
        assert payment.subscription_id == 8001


# ── Test 2: Concurrency Race Safety (Webhook vs Scheduler) ──────────────────


async def test_concurrent_webhook_and_scheduler_execution(monkeypatch, db_session_factory):
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)

    async with db_session_factory() as session:
        user = User(telegram_id=888002, username="raceuser", full_name="Race User")
        tariff = Tariff(
            id=61,
            label="Стандарт",
            price_rub=150,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
        )
        session.add_all([user, tariff])
        await session.flush()
        user_id = user.id
        pending_payment = Payment(
            user_id=user.id,
            tariff_id=61,
            platform=Platform.IOS.value,
            amount=15000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_race_123",
        )
        session.add(pending_payment)
        await session.commit()

    delivery_count = 0

    async def _mock_deliver(*args, **kwargs):
        nonlocal delivery_count
        delivery_count += 1
        return (user_id, 8002)

    monkeypatch.setattr("bot.webhooks._process_and_deliver", _mock_deliver)

    verified_payment = {
        "id": "yk_race_123",
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "150.00", "currency": "RUB"},
        "metadata": {
            "user_id": "888002",
            "chat_id": "888002",
            "tariff_id": "61",
            "platform": Platform.IOS.value,
        },
    }
    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.fetch_yookassa_payment",
        AsyncMock(return_value=verified_payment),
    )

    bot_mock = SimpleNamespace(send_message=AsyncMock())

    import asyncio

    task_webhook = verify_and_fulfill_yookassa_payment(bot_mock, "yk_race_123", source="webhook")
    task_scheduler = verify_and_fulfill_yookassa_payment(bot_mock, "yk_race_123", source="scheduler")

    results = await asyncio.gather(task_webhook, task_scheduler)

    # Exactly one should have successfully fulfilled, or one fulfilled and the other saw already completed / locked
    successes = [r for r in results if r[0] is True]
    assert len(successes) >= 1
    # Delivery must be performed exactly once
    assert delivery_count == 1

    async with db_session_factory() as session:
        payment = await session.scalar(
            select(Payment).where(Payment.provider_payment_id == "yk_race_123")
        )
        assert payment.status == PaymentStatus.COMPLETED
        assert payment.subscription_id == 8002


# ── Test 3: Duplicate Delayed Webhook After Fulfillment ─────────────────────


async def test_duplicate_webhook_after_fulfillment_returns_ok_without_dup(monkeypatch, db_session_factory):
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)

    async with db_session_factory() as session:
        user = User(telegram_id=888003, username="dupuser", full_name="Dup User")
        session.add(user)
        await session.flush()
        completed_payment = Payment(
            user_id=user.id,
            subscription_id=8003,
            tariff_id=62,
            platform=Platform.WINDOWS.value,
            amount=20000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            provider_payment_id="yk_dup_456",
        )
        session.add(completed_payment)
        await session.commit()

    deliver_mock = AsyncMock()
    monkeypatch.setattr("bot.webhooks._process_and_deliver", deliver_mock)

    request = _Request(
        _json_body={
            "event": "payment.succeeded",
            "object": {
                "id": "yk_dup_456",
                "amount": {"value": "200.00"},
                "metadata": {
                    "user_id": "888003",
                    "chat_id": "888003",
                    "tariff_id": "62",
                    "platform": Platform.WINDOWS.value,
                },
            },
        },
        headers={},
        remote="127.0.0.1",
        app={"bot": SimpleNamespace(send_message=AsyncMock())},
    )

    response = await handle_yookassa(request)
    assert response.text == "OK"
    deliver_mock.assert_not_awaited()


# ── Test 4: Financial Side Effects Idempotency ──────────────────────────────


async def test_financial_side_effects_idempotency(monkeypatch, db_session_factory):
    async with db_session_factory() as session:
        referrer = User(telegram_id=999001, username="referrer", balance_rub=0.0)
        user = User(telegram_id=999002, username="refuser", referred_by=999001)
        partner = Partner(id=1, name="Partner1", telegram_id=999003, commission_percent=10.0, is_active=True)
        session.add_all([referrer, user, partner])
        await session.flush()

        user.partner_id = partner.id
        from bot.models import BotSettings
        ref_config = ReferralConfig(id=1, is_enabled=True, commission_percent=15.0)
        cashback_en = BotSettings(key="cashback_enabled", value="1")
        cashback_pct = BotSettings(key="cashback_percent", value="10.0")
        session.add_all([ref_config, cashback_en, cashback_pct])

        payment = Payment(
            user_id=user.id,
            amount=100000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            provider_payment_id="yk_fin_side_effects",
        )
        session.add(payment)
        await session.commit()
        user_id = user.id
        payment_id = payment.id

    # 1. First credit invocation
    async with db_session_factory() as session:
        await credit_referral(session, user_id, payment_id, 1000.0)
        await credit_partner(session, user_id, payment_id, 1000.0)
        await credit_cashback(session, user_id, payment_id, 1000.0)
        await session.commit()

    # 2. Second credit invocation for the same payment
    async with db_session_factory() as session:
        await credit_referral(session, user_id, payment_id, 1000.0)
        await credit_partner(session, user_id, payment_id, 1000.0)
        await credit_cashback(session, user_id, payment_id, 1000.0)
        await session.commit()

    # 3. Assert exact single credits
    async with db_session_factory() as session:
        ref_earnings = (await session.execute(
            select(ReferralEarning).where(ReferralEarning.payment_id == payment_id)
        )).scalars().all()
        assert len(ref_earnings) == 1
        assert ref_earnings[0].amount_rub == 150.0

        partner_earnings = (await session.execute(
            select(PartnerEarning).where(PartnerEarning.payment_id == payment_id)
        )).scalars().all()
        assert len(partner_earnings) == 1
        assert partner_earnings[0].amount == 100.0

        cashback_txs = (await session.execute(
            select(BalanceTransaction).where(
                BalanceTransaction.user_id == user_id,
                BalanceTransaction.kind == BalanceTransactionKind.CASHBACK,
                BalanceTransaction.source_id == str(payment_id),
            )
        )).scalars().all()
        assert len(cashback_txs) == 1


# ── Test 5: Extra Device Slot Idempotency ───────────────────────────────────


async def test_device_slot_purchase_and_idempotency(monkeypatch, db_session_factory):
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr("bot.handlers.payment.async_session", db_session_factory)

    async with db_session_factory() as session:
        user = User(telegram_id=888005, username="deviceuser", full_name="Device User")
        session.add(user)
        await session.flush()
        sub = Subscription(
            user_id=user.id,
            server_id=1,
            client_name="test_client_dev",
            platform=Platform.IOS.value,
            tariff_months=1,
            tariff_days=30,
            expires_at=datetime.utcnow() + timedelta(days=30),
            device_slots=1,
            vpn_key="vless://existing-key",
        )
        session.add(sub)
        await session.flush()

        payment = Payment(
            user_id=user.id,
            subscription_id=sub.id,
            amount=5000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_dev_slot_1",
            platform="device",
        )
        session.add(payment)
        await session.commit()
        sub_id = sub.id

    verified_payment = {
        "id": "yk_dev_slot_1",
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "50.00", "currency": "RUB"},
        "metadata": {
            "is_device": "1",
            "sub_id": str(sub_id),
            "user_id": "888005",
            "chat_id": "888005",
        },
    }
    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.fetch_yookassa_payment",
        AsyncMock(return_value=verified_payment),
    )

    bot_mock = SimpleNamespace(send_message=AsyncMock())

    # First fulfillment: increments slot
    success, code = await verify_and_fulfill_yookassa_payment(bot_mock, "yk_dev_slot_1")
    assert success is True
    assert code == "device_slot_completed"

    async with db_session_factory() as session:
        sub_after = await session.get(Subscription, sub_id)
        assert sub_after.device_slots == 2
        p_after = await session.scalar(
            select(Payment).where(Payment.provider_payment_id == "yk_dev_slot_1")
        )
        assert p_after.status == PaymentStatus.COMPLETED
        device_delivery = (
            await session.scalars(
                select(PaymentNotificationOutbox).where(
                    PaymentNotificationOutbox.payment_id == p_after.id,
                    PaymentNotificationOutbox.kind == "subscription_delivery",
                )
            )
        ).one()
        assert device_delivery.status == "sent"

    # Second fulfillment attempt: must be idempotent and NOT increment slot again
    success2, code2 = await verify_and_fulfill_yookassa_payment(bot_mock, "yk_dev_slot_1")
    assert success2 is True
    assert code2 == "already_completed"

    async with db_session_factory() as session:
        sub_final = await session.get(Subscription, sub_id)
        assert sub_final.device_slots == 2


# ── Test 6: Balance Topup Recovery ──────────────────────────────────────────


async def test_balance_topup_reconciliation_recovery(monkeypatch, db_session_factory):
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)

    from datetime import datetime, timedelta

    async with db_session_factory() as session:
        user = User(telegram_id=888006, username="topupuser", balance_rub=10.0)
        session.add(user)
        await session.flush()

        topup = BalanceTopUp(
            user_id=user.id,
            telegram_id=888006,
            amount_rub=250.0,
            provider="yookassa",
            status="pending",
            provider_payment_id="yk_topup_lost_1",
            created_at=datetime.utcnow() - timedelta(minutes=5),
        )
        session.add(topup)
        await session.commit()
        user_id = user.id

    verified_topup = {
        "id": "yk_topup_lost_1",
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "250.00", "currency": "RUB"},
        "metadata": {
            "purpose": "balance_topup",
            "user_id": "888006",
            "chat_id": "888006",
        },
    }
    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.fetch_yookassa_payment",
        AsyncMock(return_value=verified_topup),
    )

    bot_mock = SimpleNamespace(send_message=AsyncMock())
    stats = await reconcile_pending_yookassa_payments(bot_mock)

    assert stats["fulfilled"] == 1

    async with db_session_factory() as session:
        refreshed_topup = await session.scalar(
            select(BalanceTopUp).where(BalanceTopUp.provider_payment_id == "yk_topup_lost_1")
        )
        refreshed_user = await session.get(User, user_id)
        assert refreshed_topup.status == "completed"
        assert refreshed_user.balance_rub == 260.0


# ── Test 7: VHQ Pending Fulfillment Compatibility ───────────────────────────


async def test_vhq_pending_fulfillment_compatibility(monkeypatch, db_session_factory):
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)

    async with db_session_factory() as session:
        user = User(telegram_id=888007, username="vhquser")
        tariff = Tariff(
            id=70,
            label="VHQ Тариф",
            price_rub=500,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
        )
        session.add_all([user, tariff])
        await session.flush()
        payment = Payment(
            user_id=user.id,
            tariff_id=70,
            platform=Platform.ANDROID.value,
            amount=50000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_vhq_async_1",
        )
        session.add(payment)
        await session.commit()

    async def _mock_vhq_pending(*args, **kwargs):
        raise AccessProvisionError(
            provider="vhq",
            code="supplier_fulfillment_pending",
            client_message="Заказ принят в обработку",
            admin_message="VHQ order submitted",
        )

    monkeypatch.setattr("bot.webhooks._process_and_deliver", _mock_vhq_pending)

    verified_payment = {
        "id": "yk_vhq_async_1",
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "500.00", "currency": "RUB"},
        "metadata": {
            "user_id": "888007",
            "chat_id": "888007",
            "tariff_id": "70",
            "platform": Platform.ANDROID.value,
        },
    }
    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.fetch_yookassa_payment",
        AsyncMock(return_value=verified_payment),
    )

    bot_mock = SimpleNamespace(send_message=AsyncMock())
    success, code = await verify_and_fulfill_yookassa_payment(bot_mock, "yk_vhq_async_1")

    assert success is True
    assert code == "supplier_fulfillment_pending"

    # Provider success is durable, while fulfillment remains unfinished.
    async with db_session_factory() as session:
        payment_rec = await session.scalar(
            select(Payment).where(Payment.provider_payment_id == "yk_vhq_async_1")
        )
        assert payment_rec.status == PaymentStatus.PENDING
        assert payment_rec.provider_status == "succeeded"
        assert payment_rec.provider_paid is True
        assert payment_rec.provisioning_failure_code == "supplier_fulfillment_pending"
        assert payment_rec.subscription_id is None


# ── Test 8: Canceled Payment Transitions to FAILED ─────────────────────────


async def test_canceled_payment_updates_local_status(monkeypatch, db_session_factory):
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)

    async with db_session_factory() as session:
        user = User(telegram_id=888008, username="canceluser")
        session.add(user)
        await session.flush()
        payment = Payment(
            user_id=user.id,
            tariff_id=71,
            platform=Platform.IOS.value,
            amount=10000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_canceled_1",
        )
        session.add(payment)
        await session.commit()

    canceled_payment = {
        "id": "yk_canceled_1",
        "status": "canceled",
        "paid": False,
        "amount": {"value": "100.00", "currency": "RUB"},
    }
    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.fetch_yookassa_payment",
        AsyncMock(return_value=canceled_payment),
    )

    bot_mock = SimpleNamespace(send_message=AsyncMock())
    success, code = await verify_and_fulfill_yookassa_payment(bot_mock, "yk_canceled_1")

    assert success is False
    assert code == "payment_canceled"

    async with db_session_factory() as session:
        payment_rec = await session.scalar(
            select(Payment).where(Payment.provider_payment_id == "yk_canceled_1")
        )
        assert payment_rec.status == PaymentStatus.FAILED


# ── Test 9: Provider Downtime Handled Gracefully ───────────────────────────


async def test_provider_downtime_temporary_error(monkeypatch, db_session_factory):
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)

    async with db_session_factory() as session:
        user = User(telegram_id=888009, username="downuser")
        session.add(user)
        await session.flush()
        payment = Payment(
            user_id=user.id,
            tariff_id=72,
            platform=Platform.MAC.value,
            amount=12000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_down_1",
        )
        session.add(payment)
        await session.commit()

    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.fetch_yookassa_payment",
        AsyncMock(side_effect=RuntimeError("502 Bad Gateway from YooKassa")),
    )

    bot_mock = SimpleNamespace(send_message=AsyncMock())
    success, code = await verify_and_fulfill_yookassa_payment(bot_mock, "yk_down_1")

    assert success is False
    assert code == "provider_unavailable"

    # Payment must remain PENDING for next retry
    async with db_session_factory() as session:
        payment_rec = await session.scalar(
            select(Payment).where(Payment.provider_payment_id == "yk_down_1")
        )
        assert payment_rec.status == PaymentStatus.PENDING


# ── Test 10: Metadata Mismatch Rejection ────────────────────────────────────


async def test_metadata_mismatch_rejection(monkeypatch, db_session_factory):
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)

    async with db_session_factory() as session:
        user = User(telegram_id=888010, username="mismatchuser")
        tariff = Tariff(id=73, label="Тариф 73", price_rub=100, price_stars=0, days=30, is_active=True, tariff_type=TariffType.VPN)
        session.add_all([user, tariff])
        await session.flush()
        payment = Payment(
            user_id=user.id,
            tariff_id=73,
            platform=Platform.ANDROID.value,
            amount=10000,  # 100.00 RUB
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_mismatch_1",
        )
        session.add(payment)
        await session.commit()

    # Provider returns different amount (tampered or mismatched)
    mismatched_payment = {
        "id": "yk_mismatch_1",
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "50.00", "currency": "RUB"},  # Mismatch: 50 instead of 100
        "metadata": {
            "user_id": "888010",
            "chat_id": "888010",
            "tariff_id": "73",
            "platform": Platform.ANDROID.value,
        },
    }
    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.fetch_yookassa_payment",
        AsyncMock(return_value=mismatched_payment),
    )

    alert_mock = AsyncMock()
    monkeypatch.setattr("bot.services.yookassa_reconciliation.notify_admins_issue", alert_mock)

    bot_mock = SimpleNamespace(send_message=AsyncMock())
    success, code = await verify_and_fulfill_yookassa_payment(bot_mock, "yk_mismatch_1")

    assert success is False
    assert code == "amount_mismatch"
    alert_mock.assert_awaited_once()

    # Must NOT deliver
    async with db_session_factory() as session:
        payment_rec = await session.scalar(
            select(Payment).where(Payment.provider_payment_id == "yk_mismatch_1")
        )
        assert payment_rec.status == PaymentStatus.PENDING
        assert payment_rec.subscription_id is None


# ── Test 11: Lease Expiry Before Supplier Fulfillment ───────────────────────


async def test_lease_expiry_before_supplier_fulfillment(monkeypatch, db_session_factory):
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)

    async with db_session_factory() as session:
        user = User(telegram_id=888011, username="leaseexpuser")
        tariff = Tariff(id=74, label="Тариф 74", price_rub=100, price_stars=0, days=30, is_active=True, tariff_type=TariffType.VPN)
        session.add_all([user, tariff])
        await session.flush()
        payment = Payment(
            user_id=user.id,
            tariff_id=74,
            platform=Platform.ANDROID.value,
            amount=10000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_lease_exp_1",
        )
        session.add(payment)
        await session.commit()

    verified_payment = {
        "id": "yk_lease_exp_1",
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "100.00", "currency": "RUB"},
        "metadata": {
            "user_id": "888011",
            "chat_id": "888011",
            "tariff_id": "74",
            "platform": Platform.ANDROID.value,
        },
    }
    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.fetch_yookassa_payment",
        AsyncMock(return_value=verified_payment),
    )

    # LeaseManager.try_acquire will succeed using SQLite DB, but is_owned_by_me returns False (simulating lease expiry)
    monkeypatch.setattr(LeaseManager, "is_owned_by_me", AsyncMock(return_value=False))

    deliver_mock = AsyncMock()
    monkeypatch.setattr("bot.webhooks._process_and_deliver", deliver_mock)

    bot_mock = SimpleNamespace(send_message=AsyncMock())
    success, code = await verify_and_fulfill_yookassa_payment(bot_mock, "yk_lease_exp_1")

    assert success is False
    assert code == "lease_expired"
    deliver_mock.assert_not_called()


# ── Test 12: Admin Manual Reconcile Command ─────────────────────────────────


async def test_admin_manual_reconcile_command(monkeypatch, db_session_factory):
    from bot.handlers.admin import cmd_reconcile_yookassa

    monkeypatch.setattr("bot.handlers.admin.async_session", db_session_factory)
    monkeypatch.setattr("bot.handlers.admin._is_admin", lambda uid: True)

    async with db_session_factory() as session:
        user = User(telegram_id=888012, username="manualuser")
        tariff = Tariff(id=75, label="Тариф 75", price_rub=100, price_stars=0, days=30, is_active=True, tariff_type=TariffType.VPN)
        session.add_all([user, tariff])
        await session.flush()
        payment = Payment(
            user_id=user.id,
            tariff_id=75,
            platform=Platform.ANDROID.value,
            amount=10000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_manual_123",
        )
        session.add(payment)
        await session.commit()
        local_payment_id = payment.id

    fulfill_mock = AsyncMock(return_value=(True, "subscription_fulfilled"))
    monkeypatch.setattr("bot.services.yookassa_reconciliation.verify_and_fulfill_yookassa_payment", fulfill_mock)

    # Simulate admin sending: /reconcile_yookassa <local_id>
    message_mock = SimpleNamespace(
        from_user=SimpleNamespace(id=999),
        text=f"/reconcile_yookassa {local_payment_id}",
        bot=SimpleNamespace(send_message=AsyncMock()),
        answer=AsyncMock(),
    )

    await cmd_reconcile_yookassa(message_mock)

    fulfill_mock.assert_awaited_once_with(message_mock.bot, "yk_manual_123", source="admin_manual")
    assert any("успешно" in call.args[0].lower() for call in message_mock.answer.call_args_list)


# ── Test 13: Financial Finalization with Discount and Zero Supplier Re-mutation ──


async def test_reconciliation_financial_finalization_with_discount_and_zero_supplier_remutation(
    monkeypatch, db_session_factory
):
    """Reproduces production incident #309:

    - Payment was previously supplier-fulfilled with existing subscription_id.
    - Status is PENDING with failure_code 'financial_finalization_pending'.
    - discount_applied > 0 requires accessing BalanceTransaction during financial finalization.
    - Confirms that UnboundLocalError is resolved, BalanceTransaction is created exactly once,
      and supplier renew calls remain strictly 0.
    """
    from bot.models import ReferralPaymentLog, SubStatus

    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.notifications.notify_admins_payment", AsyncMock())
    monkeypatch.setattr("bot.services.notifications.notify_admins_issue", AsyncMock())

    async with db_session_factory() as session:
        user = User(
            telegram_id=5204426782,
            username="vera_user",
            full_name="Вера Вера",
            balance_rub=50.0,
            referral_balance=0.0,
        )
        tariff = Tariff(
            id=30,
            label="Базовый 30 дней",
            price_rub=125,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
        )
        session.add_all([user, tariff])
        await session.flush()

        subscription = Subscription(
            id=182,
            user_id=user.id,
            tariff_id=30,
            tariff_days=30,
            tariff_months=1,
            server_id=1,
            status=SubStatus.ACTIVE,
            platform=Platform.ANDROID,
            expires_at=datetime.utcnow() + timedelta(days=31),
            client_name="adapt_194149bd-c98c-4426-bce8-77a2ac9df469",
            vpn_key="https://darimiru.ru/vpnbot/adapt-sub/194149bd-c98c-4426-bce8-77a2ac9df469",
        )
        session.add(subscription)
        await session.flush()

        payment = Payment(
            id=309,
            user_id=user.id,
            subscription_id=182,
            tariff_id=30,
            platform="deferred~r~182",
            amount=11250,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="327e9f9c-000f-5001-a000-10dcba4a5a02",
            discount_applied=12.50,
            telegram_chat_id=5204426782,
            provisioning_failure_code="financial_finalization_pending",
            provisioning_baseline_expires_at=datetime.utcnow() - timedelta(days=30),
            provisioning_baseline_plan_uuid="adapt_plan_30",
            created_at=datetime.utcnow() - timedelta(minutes=15),
        )
        session.add(payment)
        await session.commit()
        user_id = user.id
        payment_id = payment.id

    # YooKassa reports payment succeeded and paid
    yk_verified_payment = {
        "id": "327e9f9c-000f-5001-a000-10dcba4a5a02",
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "112.50", "currency": "RUB"},
        "payment_method": {"type": "sberbank", "id": "327e9f9c-000f-5001-a000-10dcba4a5a02", "saved": False},
        "metadata": {
            "user_id": "5204426782",
            "chat_id": "5204426782",
            "payment_id": str(payment_id),
            "tariff_id": "30",
            "platform": "deferred~r~182",
        },
    }
    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.fetch_yookassa_payment",
        AsyncMock(return_value=yk_verified_payment),
    )

    # CRITICAL INVARIANT: Supplier renewal must NEVER be called
    supplier_renew_mock = AsyncMock()
    monkeypatch.setattr("bot.webhooks._process_and_deliver", supplier_renew_mock)
    monkeypatch.setattr("bot.services.subscription_service.renew_adapt_subscription", supplier_renew_mock)
    monkeypatch.setattr("bot.services.adapt_api.AdaptAPI.renew_subscription", supplier_renew_mock)

    delivery_messages_mock = AsyncMock()
    monkeypatch.setattr("bot.webhooks.send_subscription_delivery_messages", delivery_messages_mock)

    bot_mock = SimpleNamespace(send_message=AsyncMock())

    # Execute financial finalization
    success, code = await verify_and_fulfill_yookassa_payment(
        bot_mock, "327e9f9c-000f-5001-a000-10dcba4a5a02", source="manual_remediation"
    )

    assert success is True
    assert code == "subscription_completed"

    # Supplier renew was NEVER called
    supplier_renew_mock.assert_not_called()
    assert supplier_renew_mock.call_count == 0

    # Delivery messages were sent to customer
    delivery_messages_mock.assert_awaited_once()

    # Verify DB state after first finalization
    async with db_session_factory() as session:
        p_row = await session.get(Payment, 309)
        assert p_row.status == PaymentStatus.COMPLETED
        assert p_row.subscription_id == 182
        assert p_row.provisioning_failure_code is None

        u_row = await session.get(User, user_id)
        assert u_row.balance_rub == 37.50  # 50.0 - 12.50 discount applied

        discount_txs = (
            await session.scalars(
                select(BalanceTransaction).where(
                    BalanceTransaction.user_id == user_id,
                    BalanceTransaction.source_type == "payment_discount",
                    BalanceTransaction.source_id == "327e9f9c-000f-5001-a000-10dcba4a5a02",
                )
            )
        ).all()
        assert len(discount_txs) == 1
        assert discount_txs[0].amount_rub == 12.50

    # Idempotency test: Re-run reconciliation on the completed payment
    success_idem, code_idem = await verify_and_fulfill_yookassa_payment(
        bot_mock, "327e9f9c-000f-5001-a000-10dcba4a5a02", source="reconciliation"
    )
    assert success_idem is True
    assert code_idem == "already_completed"

    # Supplier renew still NEVER called
    supplier_renew_mock.assert_not_called()
    assert supplier_renew_mock.call_count == 0

    # Verify no duplicate transactions were created
    async with db_session_factory() as session:
        discount_txs_after = (
            await session.scalars(
                select(BalanceTransaction).where(
                    BalanceTransaction.user_id == user_id,
                    BalanceTransaction.source_type == "payment_discount",
                    BalanceTransaction.source_id == "327e9f9c-000f-5001-a000-10dcba4a5a02",
                )
            )
        ).all()
        assert len(discount_txs_after) == 1

        u_row_after = await session.get(User, user_id)
        assert u_row_after.balance_rub == 37.50


async def test_payment_317_legacy_float_truncation_repairs_only_proven_amount(
    monkeypatch, db_session_factory
):
    """Production-shaped #317 renewal: repair the legacy cent and renew VHQ once."""
    from bot.models import SubStatus
    from bot.services.purchase_intent import encode_intent

    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.notifications.notify_admins_payment", AsyncMock())
    monkeypatch.setattr("bot.services.yookassa_reconciliation.notify_admins_payment", AsyncMock())
    delivery_mock = AsyncMock(return_value=(True, None))
    monkeypatch.setattr("bot.webhooks.send_subscription_delivery_messages", delivery_mock)

    now = datetime.utcnow()
    baseline_expiry = now - timedelta(days=1)
    expected_supplier_expiry = now + timedelta(days=30, minutes=1)

    async with db_session_factory() as session:
        user = User(telegram_id=871422, username="Promoparty", balance_rub=109.84)
        tariff = Tariff(
            id=317,
            label="Премиум (1 месяц)",
            price_rub=399,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            vhq_tier="basic",
        )
        server = Server(id=1, name="VHQ", host="vhq.test", location="test")
        subscription = Subscription(
            id=317,
            user_id=1,
            tariff_id=317,
            server_id=1,
            tariff_months=1,
            tariff_days=30,
            status=SubStatus.ACTIVE,
            platform=Platform.ANDROID,
            client_name="vhq_ord_317",
            vpn_key="https://sub.vhq-connect.xyz/premium-317",
            vhq_order_id="ord_317",
            expires_at=baseline_expiry,
            vhq_active_until=baseline_expiry,
        )
        payment = Payment(
            id=317,
            user_id=1,
            subscription_id=None,
            tariff_id=317,
            platform=encode_intent("deferred", "renew", 317),
            amount=28915,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_synthetic_317",
            discount_applied=109.84,
            discount_kopecks=None,
            purchase_kopecks=None,
        )
        session.add_all([user, tariff, server, subscription, payment])
        await session.commit()

    vhq_api = SimpleNamespace(
        renew=AsyncMock(
            return_value={
                "status": "scheduled",
                "active_until": expected_supplier_expiry.isoformat() + "Z",
                "order_id": "ord_317",
            }
        ),
        get_orders=AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "bot.services.subscription_service.VHQPartnerAPI",
        lambda: vhq_api,
    )

    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.fetch_yookassa_payment",
        AsyncMock(
            return_value={
                "id": "yk_synthetic_317",
                "status": "succeeded",
                "paid": True,
                "amount": {"value": "289.16", "currency": "RUB"},
                "metadata": {
                    "user_id": "871422",
                    "chat_id": "871422",
                    "payment_id": "317",
                    "tariff_id": "317",
                    "platform": encode_intent("deferred", "renew", 317),
                },
            }
        ),
    )

    ok, code = await verify_and_fulfill_yookassa_payment(
        SimpleNamespace(send_message=AsyncMock()), "yk_synthetic_317", source="synthetic"
    )
    assert ok is True
    assert code == "subscription_completed"
    delivery_mock.assert_awaited_once()

    async with db_session_factory() as session:
        repaired = await session.get(Payment, 317)
        assert repaired.amount == 28916
        assert repaired.purchase_kopecks == 39900
        assert repaired.discount_kopecks == 10984
        assert repaired.provider_amount_kopecks == 28916
        assert repaired.provider_status == "succeeded"
        assert repaired.provider_paid is True
        assert repaired.status == PaymentStatus.COMPLETED
        assert repaired.subscription_id == 317
        renewed_sub = await session.get(Subscription, 317)
        assert renewed_sub.vpn_key == "https://sub.vhq-connect.xyz/premium-317"
        assert renewed_sub.expires_at >= expected_supplier_expiry - timedelta(hours=2)
        assert (await session.get(User, 1)).balance_rub == 0.0
        debit_rows = (
            await session.scalars(
                select(BalanceTransaction).where(
                    BalanceTransaction.source_type == "payment_discount",
                    BalanceTransaction.source_id == "yk_synthetic_317",
                )
            )
        ).all()
        assert len(debit_rows) == 1
        audit = (
            await session.scalars(
                select(AdminActionLog).where(AdminActionLog.action == "repair_yookassa_legacy_amount")
            )
        ).all()
        assert len(audit) == 1
        side_effect_counts = {
            "referral_earnings": len(
                (
                    await session.scalars(
                        select(ReferralEarning).where(ReferralEarning.payment_id == 317)
                    )
                ).all()
            ),
            "referral_logs": len(
                (
                    await session.scalars(
                        select(ReferralPaymentLog).where(ReferralPaymentLog.payment_id == 317)
                    )
                ).all()
            ),
            "partner_earnings": len(
                (
                    await session.scalars(
                        select(PartnerEarning).where(PartnerEarning.payment_id == 317)
                    )
                ).all()
            ),
            "cashback": len(
                (
                    await session.scalars(
                        select(BalanceTransaction).where(
                            BalanceTransaction.kind == BalanceTransactionKind.CASHBACK,
                            BalanceTransaction.source_type == "payment",
                            BalanceTransaction.source_id == "317",
                        )
                    )
                ).all()
            ),
        }
    assert vhq_api.renew.await_count == 1

    replay_ok, replay_code = await verify_and_fulfill_yookassa_payment(
        SimpleNamespace(send_message=AsyncMock()), "yk_synthetic_317", source="synthetic_replay"
    )
    assert replay_ok is True
    assert replay_code == "already_completed"
    delivery_mock.assert_awaited_once()
    assert vhq_api.renew.await_count == 1
    async with db_session_factory() as session:
        assert len(
            (
                await session.scalars(
                    select(ReferralEarning).where(ReferralEarning.payment_id == 317)
                )
            ).all()
        ) == side_effect_counts["referral_earnings"]
        assert len(
            (
                await session.scalars(
                    select(ReferralPaymentLog).where(ReferralPaymentLog.payment_id == 317)
                )
            ).all()
        ) == side_effect_counts["referral_logs"]
        assert len(
            (
                await session.scalars(
                    select(PartnerEarning).where(PartnerEarning.payment_id == 317)
                )
            ).all()
        ) == side_effect_counts["partner_earnings"]
        assert len(
            (
                await session.scalars(
                    select(BalanceTransaction).where(
                        BalanceTransaction.kind == BalanceTransactionKind.CASHBACK,
                        BalanceTransaction.source_type == "payment",
                        BalanceTransaction.source_id == "317",
                    )
                )
            ).all()
        ) == side_effect_counts["cashback"]


# ── Test 14: Canceled YooKassa Payment Refunds Discount with Module-level BalanceTransaction ──


async def test_canceled_yookassa_payment_refunds_discount_cleanly(monkeypatch, db_session_factory):
    """Verifies that canceled payments properly use module-level BalanceTransaction to refund discounts."""
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)

    async with db_session_factory() as session:
        user = User(
            telegram_id=888015,
            username="refunduser",
            balance_rub=0.0,
            referral_balance=0.0,
        )
        session.add(user)
        await session.flush()

        payment = Payment(
            user_id=user.id,
            tariff_id=30,
            platform=Platform.ANDROID.value,
            amount=11250,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_canceled_discount_1",
            discount_applied=12.50,
        )
        session.add(payment)
        await session.flush()

        # Simulate pre-existing discount transaction that was debited
        discount_tx = BalanceTransaction(
            user_id=user.id,
            amount_rub=12.50,
            kind=BalanceTransactionKind.TOPUP,
            direction="DEBIT",
            balance_after_rub=0.0,
            description="Списание с баланса при оплате тарифа",
            source_type="payment_discount",
            source_id="yk_canceled_discount_1",
        )
        session.add(discount_tx)
        await session.commit()
        user_id = user.id

    canceled_payment = {
        "id": "yk_canceled_discount_1",
        "status": "canceled",
        "paid": False,
        "amount": {"value": "112.50", "currency": "RUB"},
    }
    monkeypatch.setattr(
        "bot.services.yookassa_reconciliation.fetch_yookassa_payment",
        AsyncMock(return_value=canceled_payment),
    )

    bot_mock = SimpleNamespace(send_message=AsyncMock())
    success, code = await verify_and_fulfill_yookassa_payment(bot_mock, "yk_canceled_discount_1")

    assert success is False
    assert code == "payment_canceled"

    async with db_session_factory() as session:
        p_row = await session.scalar(
            select(Payment).where(Payment.provider_payment_id == "yk_canceled_discount_1")
        )
        assert p_row.status == PaymentStatus.FAILED
        assert p_row.provisioning_failure_code == "payment_canceled_at_provider"

        u_row = await session.get(User, user_id)
        assert u_row.balance_rub == 12.50  # Refunded!

        refund_tx = await session.scalar(
            select(BalanceTransaction).where(
                BalanceTransaction.user_id == user_id,
                BalanceTransaction.source_type == "payment_discount_refund",
                BalanceTransaction.source_id == "yk_canceled_discount_1",
            )
        )
        assert refund_tx is not None
        assert refund_tx.amount_rub == 12.50


