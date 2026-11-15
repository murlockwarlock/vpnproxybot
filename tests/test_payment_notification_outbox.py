from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
import pytest_asyncio
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.models import (
    Base,
    Payment,
    PaymentMethod,
    PaymentNotificationOutbox,
    PaymentStatus,
    Platform,
    Server,
    Subscription,
    User,
)
from bot.services.payment_notification_outbox import (
    enqueue_payment_notification,
    process_payment_notification_outbox,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
def patch_outbox_session(monkeypatch, db_session_factory):
    monkeypatch.setattr("bot.services.payment_notification_outbox.async_session", db_session_factory)


async def _create_payment(db_session_factory):
    async with db_session_factory() as session:
        user = User(telegram_id=700001)
        session.add(user)
        await session.flush()
        payment = Payment(
            user_id=user.id,
            amount=10000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk-outbox-1",
        )
        session.add(payment)
        await session.commit()
        return payment.id


async def test_enqueue_payment_notification_is_deduplicated(db_session_factory, patch_outbox_session):
    payment_id = await _create_payment(db_session_factory)

    async with db_session_factory() as session:
        first = await enqueue_payment_notification(
            session,
            payment_id=payment_id,
            kind="payment_received",
            recipient_telegram_id=700001,
            payload=[{"text": "accepted"}],
        )
        await session.commit()

    async with db_session_factory() as session:
        second = await enqueue_payment_notification(
            session,
            payment_id=payment_id,
            kind="payment_received",
            recipient_telegram_id=700001,
            payload=[{"text": "duplicate"}],
        )
        await session.commit()
        rows = (await session.scalars(select(PaymentNotificationOutbox))).all()

    assert second.id == first.id
    assert len(rows) == 1
    assert rows[0].payload_json == '[{"text": "accepted"}]'


async def test_outbox_claim_is_atomic_for_concurrent_workers(db_session_factory, patch_outbox_session):
    payment_id = await _create_payment(db_session_factory)
    async with db_session_factory() as session:
        session.add(
            PaymentNotificationOutbox(
                payment_id=payment_id,
                kind="payment_received",
                recipient_telegram_id=700001,
                payload_json='[{"text": "accepted"}]',
            )
        )
        await session.commit()

    sent = []

    async def send_message(chat_id, text, **kwargs):
        await asyncio.sleep(0.02)
        sent.append((chat_id, text))

    bot = SimpleNamespace(send_message=send_message)
    results = await asyncio.gather(
        process_payment_notification_outbox(bot),
        process_payment_notification_outbox(bot),
    )

    assert sum(result["sent"] for result in results) == 1
    assert sent == [(700001, "accepted")]


async def test_outbox_send_failure_sets_backoff_and_admin_error(db_session_factory, patch_outbox_session):
    payment_id = await _create_payment(db_session_factory)
    async with db_session_factory() as session:
        session.add(
            PaymentNotificationOutbox(
                payment_id=payment_id,
                kind="payment_received",
                recipient_telegram_id=700001,
                payload_json='[{"text": "accepted"}]',
            )
        )
        await session.commit()

    async def send_message(*args, **kwargs):
        raise RuntimeError("telegram unavailable")

    result = await process_payment_notification_outbox(SimpleNamespace(send_message=send_message))

    assert result["retrying"] == 1
    async with db_session_factory() as session:
        row = await session.scalar(select(PaymentNotificationOutbox))
        payment = await session.get(Payment, payment_id)
        assert row.status == "pending"
        assert row.attempts == 1
        assert row.next_attempt_at > datetime.utcnow()
        assert payment.telegram_delivery_error


async def test_outbox_commits_message_progress_before_retrying_next_message(db_session_factory, patch_outbox_session):
    payment_id = await _create_payment(db_session_factory)
    async with db_session_factory() as session:
        session.add(
            PaymentNotificationOutbox(
                payment_id=payment_id,
                kind="subscription_delivery",
                recipient_telegram_id=700001,
                payload_json='[{"text": "first"}, {"text": "second"}]',
            )
        )
        await session.commit()

    calls = []

    async def send_message(chat_id, text, **kwargs):
        calls.append(text)
        if text == "second" and calls.count("second") == 1:
            raise RuntimeError("second message failed")

    bot = SimpleNamespace(send_message=send_message)
    first_result = await process_payment_notification_outbox(bot)

    async with db_session_factory() as session:
        row = await session.scalar(select(PaymentNotificationOutbox))
        assert row.message_index == 1
        row.next_attempt_at = datetime.utcnow() - timedelta(seconds=1)
        await session.commit()

    second_result = await process_payment_notification_outbox(bot)

    assert first_result["retrying"] == 1
    assert second_result["sent"] == 1
    assert calls == ["first", "second", "second"]


async def test_expired_claim_is_recorded_as_unknown_before_recovery(db_session_factory, patch_outbox_session):
    payment_id = await _create_payment(db_session_factory)
    async with db_session_factory() as session:
        session.add(
            PaymentNotificationOutbox(
                payment_id=payment_id,
                kind="payment_received",
                recipient_telegram_id=700001,
                payload_json='[{"text": "accepted"}]',
                status="sending",
                lease_token="dead-worker",
                lease_expires_at=datetime.utcnow() - timedelta(seconds=1),
                next_attempt_at=datetime.utcnow() + timedelta(hours=1),
            )
        )
        await session.commit()

    result = await process_payment_notification_outbox(SimpleNamespace(send_message=None), limit=0)

    assert result["unknown"] == 1
    async with db_session_factory() as session:
        row = await session.scalar(select(PaymentNotificationOutbox))
        assert row.status == "unknown"
        assert "outcome unknown" in (row.last_error or "")


async def test_subscription_delivery_outbox_retries_key_without_supplier_repurchase(
    db_session_factory,
    patch_outbox_session,
    monkeypatch,
):
    async with db_session_factory() as session:
        user = User(telegram_id=700002)
        server = Server(name="outbox-server", host="outbox.test", location="test")
        session.add_all([user, server])
        await session.flush()
        subscription = Subscription(
            user_id=user.id,
            server_id=server.id,
            tariff_months=1,
            tariff_days=30,
            status="active",
            device_slots=1,
            vpn_key="https://vpn.test/subscription-key",
            client_name="outbox-client",
            platform=Platform.ANDROID,
            expires_at=datetime.utcnow() + timedelta(days=30),
        )
        session.add(subscription)
        await session.flush()
        payment = Payment(
            user_id=user.id,
            subscription_id=subscription.id,
            amount=10000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            provider_payment_id="yk-outbox-key-2",
        )
        session.add(payment)
        await session.flush()
        await enqueue_payment_notification(
            session,
            payment_id=payment.id,
            kind="subscription_delivery",
            recipient_telegram_id=user.telegram_id,
            payload={
                "action": "subscription_delivery",
                "telegram_user_id": user.telegram_id,
                "subscription_id": subscription.id,
                "delivery_platform": "android",
                "needs_platform_choice": False,
            },
        )
        await session.commit()
        payment_id = payment.id

    delivery = AsyncMock(side_effect=[(False, "Telegram timeout"), (True, None)])
    monkeypatch.setattr("bot.webhooks.send_subscription_delivery_messages", delivery)
    bot = SimpleNamespace()

    first = await process_payment_notification_outbox(bot)
    assert first["retrying"] == 1
    async with db_session_factory() as session:
        row = await session.scalar(select(PaymentNotificationOutbox))
        payment = await session.get(Payment, payment_id)
        assert row.status == "pending"
        assert row.attempts == 1
        assert payment.telegram_delivery_error
        row.next_attempt_at = datetime.utcnow() - timedelta(seconds=1)
        await session.commit()

    second = await process_payment_notification_outbox(bot)
    assert second["sent"] == 1
    assert delivery.await_count == 2
    assert delivery.await_args_list[0].kwargs["vpn_key_str"] == "https://vpn.test/subscription-key"
    assert delivery.await_args_list[1].kwargs["vpn_key_str"] == "https://vpn.test/subscription-key"
    async with db_session_factory() as session:
        row = await session.scalar(select(PaymentNotificationOutbox))
        payment = await session.get(Payment, payment_id)
        assert row.status == "sent"
        assert payment.telegram_delivery_error is None


async def test_deferred_delivery_uses_factory_bot_and_sends_key_before_picker(
    db_session_factory,
    monkeypatch,
):
    from bot.telegram_client import create_telegram_bot
    from bot.webhooks import send_subscription_delivery_messages

    monkeypatch.setenv("TELEGRAM_PROXY", "http://relay.example:3128")
    monkeypatch.setattr("bot.webhooks.async_session", db_session_factory)
    monkeypatch.setattr("bot.handlers.payment.async_session", db_session_factory)

    async with db_session_factory() as session:
        user = User(telegram_id=700003)
        server = Server(name="factory-delivery", host="factory.test", location="test")
        session.add_all([user, server])
        await session.flush()
        subscription = Subscription(
            user_id=user.id,
            server_id=server.id,
            tariff_months=1,
            tariff_days=30,
            status="active",
            device_slots=1,
            vpn_key="https://vpn.test/factory-key",
            client_name="factory-client",
            platform=Platform.ANDROID,
            expires_at=datetime.utcnow() + timedelta(days=30),
        )
        session.add(subscription)
        await session.commit()

    telegram_bot = SimpleNamespace(send_message=AsyncMock())
    session = Mock()
    with patch("bot.telegram_client.AiohttpSession", return_value=session) as session_factory:
        with patch("bot.telegram_client.Bot", return_value=telegram_bot):
            bot = create_telegram_bot("token")

    delivered, error = await send_subscription_delivery_messages(
        bot=bot,
        chat_id=user.telegram_id,
        telegram_user_id=user.telegram_id,
        subscription_id=subscription.id,
        delivery_platform=Platform.ANDROID,
        vpn_key_str=subscription.vpn_key,
        proxy_link=None,
        expires_str="01.12.2026",
        needs_platform_choice=True,
    )

    assert delivered is True
    assert error is None
    session_factory.assert_called_once_with(proxy="http://relay.example:3128")
    sent_texts = [call.args[1] for call in telegram_bot.send_message.await_args_list]
    key_index = next(i for i, text in enumerate(sent_texts) if subscription.vpn_key in text)
    picker_index = next(i for i, text in enumerate(sent_texts) if "Выберите устройство" in text)
    assert key_index < picker_index


async def test_old_picker_callback_does_not_create_second_delivery(
    db_session_factory,
    monkeypatch,
):
    from bot.handlers.payment import deliver_paid_key_for_platform

    monkeypatch.setattr("bot.handlers.payment.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.payment_notification_outbox.async_session", db_session_factory)

    async with db_session_factory() as session:
        user = User(telegram_id=700004)
        server = Server(name="callback-delivery", host="callback.test", location="test")
        session.add_all([user, server])
        await session.flush()
        subscription = Subscription(
            user_id=user.id,
            server_id=server.id,
            tariff_months=1,
            tariff_days=30,
            status="active",
            device_slots=1,
            vpn_key="https://vpn.test/callback-key",
            client_name="callback-client",
            platform=Platform.ANDROID,
            expires_at=datetime.utcnow() + timedelta(days=30),
        )
        session.add(subscription)
        await session.flush()
        payment = Payment(
            user_id=user.id,
            subscription_id=subscription.id,
            amount=10000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            provider_payment_id="yk-old-picker-700004",
        )
        session.add(payment)
        await session.flush()
        session.add_all(
            [
                PaymentNotificationOutbox(
                    payment_id=payment.id,
                    kind="platform_selection",
                    recipient_telegram_id=user.telegram_id,
                    payload_json='[{"text":"picker"}]',
                    status="sent",
                    sent_at=datetime.utcnow(),
                ),
                PaymentNotificationOutbox(
                    payment_id=payment.id,
                    kind="subscription_delivery",
                    recipient_telegram_id=user.telegram_id,
                    payload_json=(
                        '[{"action":"subscription_delivery","telegram_user_id":700004,'
                        f'"subscription_id":{subscription.id},"delivery_platform":"android",'
                        '"needs_platform_choice":true}]'
                    ),
                    status="sent",
                    sent_at=datetime.utcnow(),
                ),
            ]
        )
        await session.commit()
        subscription_id = subscription.id
        payment_id = payment.id

    bot = SimpleNamespace(send_message=AsyncMock())
    callback = SimpleNamespace(
        data=f"deliver_{subscription_id}_ios",
        from_user=SimpleNamespace(id=user.telegram_id),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=user.telegram_id),
            edit_reply_markup=AsyncMock(),
        ),
        bot=bot,
        answer=AsyncMock(),
    )

    await deliver_paid_key_for_platform(callback)

    assert bot.send_message.await_count == 0
    callback.answer.assert_awaited_once_with("Ключ отправлен")
    async with db_session_factory() as session:
        rows = (
            await session.scalars(
                select(PaymentNotificationOutbox).where(
                    PaymentNotificationOutbox.payment_id == payment_id,
                    PaymentNotificationOutbox.kind == "subscription_delivery",
                )
            )
        ).all()
        assert len(rows) == 1


async def test_init_db_migrates_payment_schedule_and_outbox_table(tmp_path, monkeypatch):
    import bot.database as bot_database

    database_url = f"sqlite+aiosqlite:///{tmp_path / 'payments.sqlite3'}"
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(bot_database, "engine", engine)
    monkeypatch.setattr(bot_database, "async_session", factory)

    await bot_database.init_db()
    await bot_database.init_db()

    async with engine.begin() as connection:
        columns = await connection.run_sync(
            lambda sync_connection: {
                table: {column["name"] for column in inspect(sync_connection).get_columns(table)}
                for table in ("payments", "balance_topups", "payment_notification_outbox")
            }
        )

    assert {"reconciliation_attempts", "next_attempt_at"} <= columns["payments"]
    assert {"reconciliation_attempts", "next_attempt_at"} <= columns["balance_topups"]
    assert "payload_json" in columns["payment_notification_outbox"]
    await engine.dispose()
