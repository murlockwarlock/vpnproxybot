from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.config import settings as bot_settings
from bot.handlers import payment as payment_handler
from bot.models import (
    BalanceDirection,
    BalanceTransaction,
    BalanceTransactionKind,
    Base as BotBase,
    Payment,
    PaymentMethod,
    PaymentStatus,
    Platform,
    Server,
    SubStatus,
    Subscription,
    Tariff,
    TariffType,
    User,
)
from bot.services import cluster as bot_cluster
from bot.services import yookassa_reconciliation
from bot.services.provisioning_issues import AccessProvisionError
from webstore import routes as web_routes
from webstore import scheduler as web_scheduler
from webstore.config import settings as web_settings
from webstore.models import (
    Base as WebBase,
    WebBalanceAccount,
    WebBalanceTransaction,
    WebOrder,
    WebProfileLink,
    WebSession,
)

pytestmark = pytest.mark.asyncio


def _configure_sqlite_busy_timeout(dbapi_conn, _connection_record) -> None:
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA busy_timeout = 5000")
    cursor.close()


@pytest_asyncio.fixture
async def bot_file_db(tmp_path, monkeypatch):
    db_path = tmp_path / "bot_atomic_balance.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    event.listen(engine.sync_engine, "connect", _configure_sqlite_busy_timeout)
    async with engine.begin() as conn:
        await conn.run_sync(BotBase.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("bot.database.async_session", factory)
    monkeypatch.setattr(bot_cluster, "async_session", factory)
    monkeypatch.setattr(payment_handler, "async_session", factory)
    monkeypatch.setattr(yookassa_reconciliation, "async_session", factory)
    monkeypatch.setattr(bot_settings, "yookassa_shop_id", "shop_test")
    monkeypatch.setattr(bot_settings, "yookassa_secret_key", "secret_test")
    monkeypatch.setattr(bot_settings, "base_webhook_url", "https://bot.example.test")

    yield factory

    await engine.dispose()


@pytest_asyncio.fixture
async def web_file_db(tmp_path, monkeypatch):
    db_path = tmp_path / "web_atomic_balance.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    event.listen(engine.sync_engine, "connect", _configure_sqlite_busy_timeout)
    async with engine.begin() as conn:
        await conn.run_sync(WebBase.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("webstore.database.async_session", factory)
    monkeypatch.setattr(web_routes, "async_session", factory)
    monkeypatch.setattr(web_scheduler, "async_session", factory)
    monkeypatch.setattr(web_settings, "yookassa_shop_id", "shop_web_test")
    monkeypatch.setattr(web_settings, "yookassa_secret_key", "secret_web_test")
    monkeypatch.setattr(web_settings, "subscription_base_url", "https://store.example.test")

    yield factory

    await engine.dispose()


def _make_callback(user_id: int, data: str):
    bot = SimpleNamespace(send_message=AsyncMock())
    message = SimpleNamespace(
        chat=SimpleNamespace(id=user_id),
        answer=AsyncMock(),
        edit_text=AsyncMock(),
        bot=bot,
    )
    return SimpleNamespace(
        from_user=SimpleNamespace(
            id=user_id,
            username=f"user_{user_id}",
            full_name=f"User {user_id}",
        ),
        data=data,
        message=message,
        bot=bot,
        answer=AsyncMock(),
    )


class _WebRequest:
    def __init__(self, payload: dict | None = None, *, query: dict | None = None, cookies: dict | None = None):
        self._payload = payload or {}
        self.query = query or {}
        self.cookies = cookies or {}
        self.headers = {"Host": "store.example.test"}
        self.remote = "127.0.0.1"
        self.scheme = "https"
        self.host = "store.example.test"

    async def json(self):
        return self._payload


async def test_bot_concurrent_balance_payments_prevent_double_spend(bot_file_db, monkeypatch):
    async with bot_file_db() as session:
        user = User(telegram_id=770001, username="race_buyer", full_name="Race Buyer", balance_rub=100.0)
        server = Server(name="NL", host="10.0.0.1", location="NL", is_active=True)
        tariff = Tariff(days=30, label="Базовый 30д", price_rub=100, tariff_type=TariffType.VPN, is_active=True)
        session.add_all([user, server, tariff])
        await session.commit()
        user_db_id = user.id
        server_id = server.id
        tariff_id = tariff.id

    provision_calls = 0

    async def _slow_provision(session, *, user, tariff, platform, **_kwargs):
        nonlocal provision_calls
        provision_calls += 1
        await asyncio.sleep(0.06)
        sub = Subscription(
            user_id=user.id,
            server_id=server_id,
            tariff_months=1,
            tariff_days=tariff.days,
            status=SubStatus.ACTIVE,
            device_slots=3,
            vpn_key=f"https://vpn.example.test/sub/{provision_calls}",
            client_name=f"tg{user.telegram_id}_{provision_calls}",
            platform=platform,
            expires_at=datetime.utcnow() + timedelta(days=30),
        )
        session.add(sub)
        await session.flush()
        return sub, sub.vpn_key

    monkeypatch.setattr(payment_handler, "create_or_extend_paid_access", _slow_provision)
    monkeypatch.setattr(payment_handler, "_send_subscription_key_for_platform", AsyncMock())
    monkeypatch.setattr(payment_handler, "notify_admins_payment", AsyncMock())
    monkeypatch.setattr(payment_handler, "credit_referral", AsyncMock())
    monkeypatch.setattr(payment_handler, "log_referral_payment", AsyncMock())

    cb1 = _make_callback(770001, f"pay_balance_{tariff_id}_android_1")
    cb2 = _make_callback(770001, f"pay_balance_{tariff_id}_android_1")

    await asyncio.gather(
        payment_handler.initiate_balance_payment(cb1),
        payment_handler.initiate_balance_payment(cb2),
    )

    assert provision_calls == 1

    async with bot_file_db() as session:
        refreshed_user = await session.get(User, user_db_id)
        payments = (await session.scalars(select(Payment).where(Payment.user_id == user_db_id))).all()
        debits = (
            await session.scalars(
                select(BalanceTransaction).where(
                    BalanceTransaction.user_id == user_db_id,
                    BalanceTransaction.direction == BalanceDirection.DEBIT,
                )
            )
        ).all()

    assert refreshed_user.balance_rub == 0.0
    assert len(payments) == 1
    assert payments[0].status == PaymentStatus.COMPLETED
    assert len(debits) == 1
    assert debits[0].source_type == "balance_payment"
    assert debits[0].amount_rub == 100.0

    all_alerts = [
        call.args[0]
        for cb in (cb1, cb2)
        for call in cb.answer.await_args_list
        if call.args
    ]
    assert any("Недостаточно средств" in text for text in all_alerts)


async def test_bot_balance_payment_holds_balance_on_ambiguous_vhq_and_refunds_confirmed_rejection(
    bot_file_db, monkeypatch
):
    async with bot_file_db() as session:
        u_ambig = User(telegram_id=770002, username="ambig_vhq", full_name="Ambig VHQ", balance_rub=250.0)
        u_reject = User(telegram_id=770003, username="reject_vhq", full_name="Reject VHQ", balance_rub=250.0)
        tariff = Tariff(days=30, label="VHQ 30д", price_rub=250, tariff_type=TariffType.VPN, is_active=True)
        session.add_all([u_ambig, u_reject, tariff])
        await session.commit()
        ambig_id = u_ambig.id
        reject_id = u_reject.id
        tariff_id = tariff.id

    monkeypatch.setattr(payment_handler, "_notify_delivery_issue", AsyncMock())
    monkeypatch.setattr(payment_handler, "notify_expiring", AsyncMock())

    async def _raise_ambiguous(*_args, **_kwargs):
        raise AccessProvisionError(
            provider="vhq",
            code="ambiguous_buy_network_error",
            client_message="Проверяем статус выдачи",
            admin_message="VHQ buy network timeout",
            confirmed_rejection=False,
        )

    monkeypatch.setattr(payment_handler, "create_or_extend_paid_access", _raise_ambiguous)
    cb_ambig = _make_callback(770002, f"pay_balance_{tariff_id}_android_1")
    await payment_handler.initiate_balance_payment(cb_ambig)

    async with bot_file_db() as session:
        refreshed_ambig = await session.get(User, ambig_id)
        ambig_payment = await session.scalar(select(Payment).where(Payment.user_id == ambig_id))
        ambig_refunds = (
            await session.scalars(
                select(BalanceTransaction).where(
                    BalanceTransaction.user_id == ambig_id,
                    BalanceTransaction.source_type == "payment_refund",
                )
            )
        ).all()

    assert refreshed_ambig.balance_rub == 0.0
    assert ambig_payment.status == PaymentStatus.COMPLETED
    assert ambig_payment.provisioning_failure_code == "ambiguous_buy_network_error"
    assert len(ambig_refunds) == 0

    async def _raise_confirmed_402(*_args, **_kwargs):
        raise AccessProvisionError(
            provider="vhq",
            code="insufficient_funds",
            status=402,
            client_message="Временная задержка у поставщика",
            admin_message="VHQ 402 insufficient partner funds",
            confirmed_rejection=True,
        )

    monkeypatch.setattr(payment_handler, "create_or_extend_paid_access", _raise_confirmed_402)
    cb_reject = _make_callback(770003, f"pay_balance_{tariff_id}_android_1")
    await payment_handler.initiate_balance_payment(cb_reject)

    async with bot_file_db() as session:
        refreshed_reject = await session.get(User, reject_id)
        reject_payment = await session.scalar(select(Payment).where(Payment.user_id == reject_id))
        reject_refunds = (
            await session.scalars(
                select(BalanceTransaction).where(
                    BalanceTransaction.user_id == reject_id,
                    BalanceTransaction.source_type == "payment_refund",
                )
            )
        ).all()
        assert refreshed_reject.balance_rub == 250.0
        assert reject_payment.status == PaymentStatus.REFUNDED
        assert len(reject_refunds) == 1

        # Unique index must reject duplicate refund for the same payment source_id
        session.add(
            BalanceTransaction(
                user_id=reject_id,
                kind=BalanceTransactionKind.REFUND,
                direction=BalanceDirection.CREDIT,
                amount_rub=250.0,
                balance_after_rub=500.0,
                description="dup",
                source_type="payment_refund",
                source_id=str(reject_payment.id),
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()


async def test_bot_yookassa_discount_reservation_blocks_multi_checkout_and_handles_cancel_or_success(
    bot_file_db, monkeypatch
):
    async with bot_file_db() as session:
        user = User(telegram_id=770004, username="yk_disc", full_name="YK Disc", balance_rub=80.0)
        server = Server(name="NL", host="10.0.0.2", location="NL", is_active=True)
        t1 = Tariff(days=30, label="T1 200", price_rub=200, tariff_type=TariffType.VPN, is_active=True)
        t2 = Tariff(days=90, label="T2 300", price_rub=300, tariff_type=TariffType.VPN, is_active=True)
        session.add_all([user, server, t1, t2])
        await session.commit()
        user_db_id = user.id
        server_id = server.id
        t1_id = t1.id
        t2_id = t2.id

    yk_counter = 0
    yk_by_idem: dict[str, tuple[str, str]] = {}

    async def _fake_create_yk(**kwargs):
        nonlocal yk_counter
        idem = kwargs.get("idempotence_key")
        if idem and idem in yk_by_idem:
            return yk_by_idem[idem]
        yk_counter += 1
        pair = (f"https://yookassa.test/pay/{yk_counter}", f"yk_pay_{yk_counter}")
        if idem:
            yk_by_idem[idem] = pair
        return pair

    monkeypatch.setattr(payment_handler, "create_yookassa_payment", _fake_create_yk)

    # 1. Checkout T1 with use_bal=1 reserves all 80 RUB immediately
    await payment_handler.initiate_yookassa_payment(_make_callback(770004, f"pay_yookassa_{t1_id}_android_1"))

    async with bot_file_db() as session:
        u = await session.get(User, user_db_id)
        p1 = await session.scalar(select(Payment).where(Payment.provider_payment_id == "yk_pay_1"))
        assert u.balance_rub == 0.0
        assert p1.amount == 12000
        assert p1.discount_kopecks == 8000

    # 2. Second checkout on T2 with use_bal=1 CANNOT reuse the reserved 80 RUB
    await payment_handler.initiate_yookassa_payment(_make_callback(770004, f"pay_yookassa_{t2_id}_android_1"))

    async with bot_file_db() as session:
        p2 = await session.scalar(select(Payment).where(Payment.provider_payment_id == "yk_pay_2"))
        assert p2.amount == 30000
        assert p2.discount_kopecks == 0

    # 3. Re-clicking T1 reuses existing PENDING payment without extra debit
    await payment_handler.initiate_yookassa_payment(_make_callback(770004, f"pay_yookassa_{t1_id}_android_1"))
    async with bot_file_db() as session:
        debits = (
            await session.scalars(
                select(BalanceTransaction).where(
                    BalanceTransaction.user_id == user_db_id,
                    BalanceTransaction.source_type == "payment_discount",
                )
            )
        ).all()
        assert len(debits) == 1

    # 4. Canceled at YooKassa refunds reserved 80 RUB idempotently even when replayed
    monkeypatch.setattr(
        yookassa_reconciliation,
        "fetch_yookassa_payment",
        AsyncMock(
            return_value={
                "id": "yk_pay_1",
                "status": "canceled",
                "paid": False,
                "amount": {"value": "120.00", "currency": "RUB"},
            }
        ),
    )
    bot_mock = SimpleNamespace(send_message=AsyncMock())
    ok1, code1 = await yookassa_reconciliation.verify_and_fulfill_yookassa_payment(bot_mock, "yk_pay_1")
    ok2, code2 = await yookassa_reconciliation.verify_and_fulfill_yookassa_payment(bot_mock, "yk_pay_1")
    assert (ok1, code1) == (False, "payment_canceled")
    assert (ok2, code2) == (False, "payment_canceled")

    async with bot_file_db() as session:
        u = await session.get(User, user_db_id)
        refunds = (
            await session.scalars(
                select(BalanceTransaction).where(
                    BalanceTransaction.user_id == user_db_id,
                    BalanceTransaction.source_type == "payment_discount_refund",
                )
            )
        ).all()
        assert u.balance_rub == 80.0
        assert len(refunds) == 1

    # 5. Fresh discounted checkout that SUCCEEDS at YooKassa must NOT double-debit the reserved 80 RUB
    await payment_handler.initiate_yookassa_payment(_make_callback(770004, f"pay_yookassa_{t1_id}_ios_1"))
    async with bot_file_db() as session:
        u = await session.get(User, user_db_id)
        p_succ = await session.scalar(select(Payment).where(Payment.provider_payment_id == "yk_pay_3"))
        assert u.balance_rub == 0.0
        assert p_succ is not None
        assert p_succ.amount == 12000
        assert p_succ.discount_kopecks == 8000
        p_succ_id = p_succ.id

    async def _provision_sub(session, *, user, tariff, platform, **_kwargs):
        sub = Subscription(
            user_id=user.id,
            server_id=server_id,
            tariff_months=1,
            tariff_days=tariff.days,
            status=SubStatus.ACTIVE,
            device_slots=3,
            vpn_key="https://vpn.example.test/sub/yk3",
            client_name="tg770004_yk3",
            platform=platform,
            expires_at=datetime.utcnow() + timedelta(days=30),
        )
        session.add(sub)
        await session.flush()
        return sub, sub.vpn_key

    monkeypatch.setattr("bot.webhooks.create_or_extend_paid_access", _provision_sub)
    monkeypatch.setattr(yookassa_reconciliation, "notify_admins_payment", AsyncMock())
    monkeypatch.setattr(yookassa_reconciliation, "process_payment_notification_outbox", AsyncMock(return_value={"sent": 1}))
    monkeypatch.setattr(
        yookassa_reconciliation,
        "fetch_yookassa_payment",
        AsyncMock(
            return_value={
                "id": "yk_pay_3",
                "status": "succeeded",
                "paid": True,
                "amount": {"value": "120.00", "currency": "RUB"},
                "metadata": {
                    "payment_id": str(p_succ_id),
                    "user_id": "770004",
                    "chat_id": "770004",
                    "tariff_id": str(t1_id),
                    "platform": "ios",
                },
                "payment_method": {"saved": False},
            }
        ),
    )

    ok_succ, code_succ = await yookassa_reconciliation.verify_and_fulfill_yookassa_payment(bot_mock, "yk_pay_3")
    assert (ok_succ, code_succ) == (True, "subscription_completed")

    async with bot_file_db() as session:
        u = await session.get(User, user_db_id)
        yk3_debits = (
            await session.scalars(
                select(BalanceTransaction).where(
                    BalanceTransaction.user_id == user_db_id,
                    BalanceTransaction.source_type == "payment_discount",
                    BalanceTransaction.source_id.in_(["yk_pay_3", f"local:{p_succ_id}"]),
                )
            )
        ).all()
        assert u.balance_rub == 0.0
        assert len(yk3_debits) == 1


async def test_bot_pre_checkout_rejects_stale_discount_and_xtr_referral_uses_external_portion(
    bot_file_db, monkeypatch
):
    async with bot_file_db() as session:
        user = User(telegram_id=770005, username="stars_buyer", full_name="Stars Buyer", balance_rub=50.0)
        server = Server(name="NL", host="10.0.0.3", location="NL", is_active=True)
        tariff = Tariff(
            days=30,
            label="Stars 200",
            price_rub=200,
            price_stars=100,
            tariff_type=TariffType.VPN,
            is_active=True,
        )
        session.add_all([user, server, tariff])
        await session.commit()
        user_db_id = user.id
        server_id = server.id
        tariff_id = tariff.id

    pre_ok = SimpleNamespace(
        from_user=SimpleNamespace(id=770005),
        invoice_payload=f"{tariff_id}_android_50.0",
        currency="XTR",
        total_amount=75,
        answer=AsyncMock(),
    )
    await payment_handler.process_pre_checkout(pre_ok)
    pre_ok.answer.assert_awaited_once_with(ok=True)

    # Spend 45 RUB elsewhere -> stale 50 RUB discount invoice must be declined at pre-checkout
    async with bot_file_db() as session:
        u = await session.get(User, user_db_id)
        u.balance_rub = 5.0
        await session.commit()

    pre_stale = SimpleNamespace(
        from_user=SimpleNamespace(id=770005),
        invoice_payload=f"{tariff_id}_android_50.0",
        currency="XTR",
        total_amount=75,
        answer=AsyncMock(),
    )
    await payment_handler.process_pre_checkout(pre_stale)
    assert pre_stale.answer.await_args.kwargs.get("ok") is False

    # Restore 50 RUB and verify XTR successful payment credits referral strictly on (200 - 50) = 150 RUB
    async with bot_file_db() as session:
        u = await session.get(User, user_db_id)
        u.balance_rub = 50.0
        await session.commit()

    async def _provision(session, *, user, tariff, platform, **_kwargs):
        sub = Subscription(
            user_id=user.id,
            server_id=server_id,
            tariff_months=1,
            tariff_days=tariff.days,
            status=SubStatus.ACTIVE,
            device_slots=3,
            vpn_key="https://vpn.example.test/sub/stars",
            client_name="tg770005_stars",
            platform=platform,
            expires_at=datetime.utcnow() + timedelta(days=30),
        )
        session.add(sub)
        await session.flush()
        return sub, sub.vpn_key

    ref_mock = AsyncMock()
    log_ref_mock = AsyncMock()
    monkeypatch.setattr(payment_handler, "create_or_extend_paid_access", _provision)
    monkeypatch.setattr(payment_handler, "_send_subscription_key_for_platform", AsyncMock())
    monkeypatch.setattr(payment_handler, "notify_admins_payment", AsyncMock())
    monkeypatch.setattr(payment_handler, "credit_referral", ref_mock)
    monkeypatch.setattr(payment_handler, "log_referral_payment", log_ref_mock)

    msg = SimpleNamespace(
        from_user=SimpleNamespace(id=770005, username="stars_buyer", full_name="Stars Buyer"),
        successful_payment=SimpleNamespace(
            invoice_payload=f"{tariff_id}_android_50.0",
            telegram_payment_charge_id="xtr_charge_1",
            total_amount=75,
            currency="XTR",
        ),
        bot=SimpleNamespace(send_message=AsyncMock()),
        answer=AsyncMock(),
    )
    await payment_handler.process_successful_payment(msg)

    assert ref_mock.await_count == 1
    assert ref_mock.await_args.args[3] == 150.0
    async with bot_file_db() as session:
        u = await session.get(User, user_db_id)
        assert u.balance_rub == 0.0


async def test_webstore_concurrent_balance_orders_prevent_double_spend_and_require_auth(
    web_file_db, monkeypatch
):
    contact = "web_race@example.com"
    profile = web_routes._make_profile_token(contact)

    async with web_file_db() as session:
        # Telegram-linked profile WITHOUT WebAccount row — must still block unauthenticated use_balance!
        session.add(WebProfileLink(profile_token=profile, telegram_id=880001, contact=contact))
        session.add(WebBalanceAccount(profile_token=profile, contact=contact, balance_rub=300, ref_code="ref_race_1"))
        await session.commit()

    auth_resp = await web_routes._auth_response({"ok": True}, profile, method="telegram")
    auth_cookies = {web_routes._AUTH_COOKIE: auth_resp.cookies[web_routes._AUTH_COOKIE].value}

    tariff_def = {"key": "vpn_30", "label": "Базовый 30д", "days": 30, "price_rub": 300}
    monkeypatch.setattr(web_routes, "get_store_tariffs_by_key", lambda: {"vpn_30": tariff_def})
    monkeypatch.setattr(web_routes, "get_store_tariffs", lambda: [tariff_def])
    monkeypatch.setattr(web_routes, "_notify_admins_webstore", AsyncMock())
    monkeypatch.setattr(web_routes, "_notify_bot_order_delivered", AsyncMock(return_value={"ok": True, "matched": False, "delivered": False}))
    monkeypatch.setattr(web_routes, "_notify_linked_webstore_user", AsyncMock())
    monkeypatch.setattr(web_routes, "_fetch_public_balance_config", AsyncMock(return_value={"cashback_enabled": False}))

    # 1. Unauthenticated request with victim contact + use_balance=True must be rejected (401)
    unauth_resp = await web_routes.handle_create_order(
        _WebRequest({"tariff_key": "vpn_30", "contact": contact, "use_balance": True})
    )
    assert unauth_resp.status == 401
    async with web_file_db() as session:
        assert (await session.get(WebBalanceAccount, profile)).balance_rub == 300

    # 2. Two concurrent authenticated requests on 300 RUB balance: only 1 gets 300 RUB bonus
    fulfill_calls = 0

    async def _slow_fulfill(_session, order):
        nonlocal fulfill_calls
        fulfill_calls += 1
        await asyncio.sleep(0.06)
        order.status = "delivered"
        order.delivered_at = datetime.utcnow()
        order.access_expires_at = datetime.utcnow() + timedelta(days=30)
        order.subscription_url = f"https://vpn.example.test/web/{order.order_id}"
        order.marzban_username = f"web_{order.order_id}"

    monkeypatch.setattr(web_routes, "_fulfill_order", _slow_fulfill)

    import yookassa

    monkeypatch.setattr(
        yookassa.Payment,
        "create",
        staticmethod(
            lambda _params, idem_key: SimpleNamespace(
                id=f"yk_web_{idem_key}",
                confirmation=SimpleNamespace(confirmation_url=f"https://yookassa.test/web/{idem_key}"),
            )
        ),
    )

    r1, r2 = await asyncio.gather(
        web_routes.handle_create_order(
            _WebRequest({"tariff_key": "vpn_30", "contact": contact, "use_balance": True}, cookies=auth_cookies)
        ),
        web_routes.handle_create_order(
            _WebRequest({"tariff_key": "vpn_30", "contact": contact, "use_balance": True}, cookies=auth_cookies)
        ),
    )
    assert r1.status == 200 and r2.status == 200
    assert fulfill_calls == 1

    async with web_file_db() as session:
        acct = await session.get(WebBalanceAccount, profile)
        orders = (await session.scalars(select(WebOrder).where(WebOrder.profile_token == profile))).all()
        debits = (
            await session.scalars(
                select(WebBalanceTransaction).where(
                    WebBalanceTransaction.profile_token == profile,
                    WebBalanceTransaction.kind == "order_payment",
                )
            )
        ).all()

    assert acct.balance_rub == 0
    assert len(debits) == 1
    assert debits[0].amount_rub == 300
    delivered_orders = [o for o in orders if o.status == "delivered"]
    pending_yk_orders = [o for o in orders if o.status == "pending"]
    assert len(delivered_orders) == 1
    assert delivered_orders[0].bonus_applied_rub == 300
    assert delivered_orders[0].amount_rub == 0
    assert len(pending_yk_orders) == 1
    assert pending_yk_orders[0].bonus_applied_rub == 0
    assert pending_yk_orders[0].amount_rub == 300


async def test_webstore_discounted_order_cancel_and_reconcile_refund_invariants(
    web_file_db, monkeypatch
):
    contact = "web_cancel@example.com"
    profile = web_routes._make_profile_token(contact)

    async with web_file_db() as session:
        session.add(WebBalanceAccount(profile_token=profile, contact=contact, balance_rub=100, ref_code="ref_cancel_1"))
        await session.commit()

    auth_resp = await web_routes._auth_response({"ok": True}, profile, method="otp")
    auth_cookies = {web_routes._AUTH_COOKIE: auth_resp.cookies[web_routes._AUTH_COOKIE].value}

    tariff_def = {"key": "vpn_30", "label": "Базовый 30д", "days": 30, "price_rub": 300}
    monkeypatch.setattr(web_routes, "get_store_tariffs_by_key", lambda: {"vpn_30": tariff_def})
    monkeypatch.setattr(web_routes, "get_store_tariffs", lambda: [tariff_def])

    import yookassa

    monkeypatch.setattr(
        yookassa.Payment,
        "create",
        staticmethod(
            lambda _params, idem_key: SimpleNamespace(
                id=f"yk_partial_{idem_key}",
                confirmation=SimpleNamespace(confirmation_url=f"https://yookassa.test/web/{idem_key}"),
            )
        ),
    )

    resp = await web_routes.handle_create_order(
        _WebRequest({"tariff_key": "vpn_30", "contact": contact, "use_balance": True}, cookies=auth_cookies)
    )
    assert resp.status == 200
    order_id = json.loads(resp.text)["order_id"]

    # Bonus is reserved immediately on checkout
    async with web_file_db() as session:
        assert (await session.get(WebBalanceAccount, profile)).balance_rub == 0

    # Local cancel while yookassa_payment_id exists (unconfirmed) MUST NOT refund yet
    cancel_resp = await web_routes.handle_cancel_order(
        _WebRequest({"order_id": order_id, "token": profile}, cookies=auth_cookies)
    )
    assert cancel_resp.status == 200
    async with web_file_db() as session:
        ord_row = await session.scalar(select(WebOrder).where(WebOrder.order_id == order_id))
        assert ord_row.cancellation_confirmed is False
        assert (await session.get(WebBalanceAccount, profile)).balance_rub == 0

    # Authenticated YooKassa cancel confirmation refunds reserved 100 RUB once (replay-safe)
    monkeypatch.setattr(
        web_routes,
        "fetch_yookassa_payment",
        AsyncMock(
            return_value=SimpleNamespace(
                payment_id=f"yk_partial_{order_id}",
                status="canceled",
                paid=False,
                amount_rub=Decimal("200.00"),
                currency="RUB",
                metadata={"order_id": order_id, "source": "webstore"},
            )
        ),
    )
    s1 = await web_routes.reconcile_yookassa_web_order(order_id, f"yk_partial_{order_id}")
    s2 = await web_routes.reconcile_yookassa_web_order(order_id, f"yk_partial_{order_id}")
    assert s1 == "canceled" and s2 == "canceled"

    async with web_file_db() as session:
        assert (await session.get(WebBalanceAccount, profile)).balance_rub == 100
        refunds = (
            await session.scalars(
                select(WebBalanceTransaction).where(
                    WebBalanceTransaction.kind == "order_bonus_refund",
                    WebBalanceTransaction.source_id == order_id,
                )
            )
        ).all()
        assert len(refunds) == 1

    # YooKassa Payment.create failure immediately cancels and refunds reserved bonus
    def _boom(_params, _idem_key):
        raise RuntimeError("yookassa api down")

    monkeypatch.setattr(yookassa.Payment, "create", staticmethod(_boom))
    fail_resp = await web_routes.handle_create_order(
        _WebRequest({"tariff_key": "vpn_30", "contact": contact, "use_balance": True}, cookies=auth_cookies)
    )
    assert fail_resp.status == 500
    async with web_file_db() as session:
        assert (await session.get(WebBalanceAccount, profile)).balance_rub == 100

    # Stale 25h+ pending order without provider payment id refunds reserved bonus via expire_pending_orders
    async with web_file_db() as session:
        await web_routes._atomic_debit_web_balance(
            session,
            profile,
            amount_rub=60,
            kind="order_payment",
            description="stale reserve",
            source_id="stale_ord_1",
            contact=contact,
        )
        session.add(
            WebOrder(
                order_id="stale_ord_1",
                profile_token=profile,
                contact=contact,
                tariff_key="vpn_30",
                tariff_label="Базовый 30д",
                days=30,
                original_amount_rub=300,
                bonus_applied_rub=60,
                bonus_spent_at=datetime.utcnow() - timedelta(hours=25),
                amount_rub=240,
                status="pending",
                created_at=datetime.utcnow() - timedelta(hours=25),
            )
        )
        await session.commit()
        assert (await session.get(WebBalanceAccount, profile)).balance_rub == 40

    await web_scheduler.expire_pending_orders()
    await web_scheduler.expire_pending_orders()  # replay-safe
    async with web_file_db() as session:
        assert (await session.get(WebBalanceAccount, profile)).balance_rub == 100


async def test_webstore_referral_uses_paid_amount_only_and_skips_zero_external_orders(
    web_file_db, monkeypatch
):
    alice_token = "profile_alice_ref"
    bob_token = "profile_bob_buyer"

    async with web_file_db() as session:
        session.add(
            WebBalanceAccount(
                profile_token=alice_token,
                contact="alice@example.com",
                balance_rub=0,
                ref_code="ref_alice_code",
            )
        )
        session.add(
            WebBalanceAccount(
                profile_token=bob_token,
                contact="bob@example.com",
                balance_rub=0,
                ref_code="ref_bob_code",
            )
        )
        # Order 1: 100% paid with web bonus (original=300, bonus=300, amount_rub=0) -> 0 RUB referral!
        session.add(
            WebOrder(
                order_id="ord_full_bonus",
                profile_token=bob_token,
                contact="bob@example.com",
                tariff_key="vpn_30",
                tariff_label="Базовый 30д",
                days=30,
                original_amount_rub=300,
                bonus_applied_rub=300,
                amount_rub=0,
                status="delivered",
                ref_source="ref_alice_code",
                referral_status="ready",
            )
        )
        # Order 2: partially paid externally (original=300, bonus=200, amount_rub=100) -> 10% of 100 = 10 RUB!
        session.add(
            WebOrder(
                order_id="ord_partial_bonus",
                profile_token=bob_token,
                contact="bob@example.com",
                tariff_key="vpn_30",
                tariff_label="Базовый 30д",
                days=30,
                original_amount_rub=300,
                bonus_applied_rub=200,
                amount_rub=100,
                status="delivered",
                ref_source="ref_alice_code",
                referral_status="ready",
            )
        )
        await session.commit()

    monkeypatch.setattr(
        web_routes,
        "_fetch_public_balance_config",
        AsyncMock(return_value={"referral_commission_percent": 10.0}),
    )
    bridge_mock = AsyncMock(return_value={"status": "credited"})
    monkeypatch.setattr(web_routes, "_credit_web_referral", bridge_mock)

    await web_routes._sync_referral_credit_for_order("ord_full_bonus")
    async with web_file_db() as session:
        alice = await session.get(WebBalanceAccount, alice_token)
        o_full = await session.scalar(select(WebOrder).where(WebOrder.order_id == "ord_full_bonus"))
        assert alice.balance_rub == 0
        assert o_full.referral_status == "not_eligible"
    assert bridge_mock.await_count == 0

    await web_routes._sync_referral_credit_for_order("ord_partial_bonus")
    await web_routes._sync_referral_credit_for_order("ord_partial_bonus")  # idempotent replay
    async with web_file_db() as session:
        alice = await session.get(WebBalanceAccount, alice_token)
        o_part = await session.scalar(select(WebOrder).where(WebOrder.order_id == "ord_partial_bonus"))
        ref_txs = (
            await session.scalars(
                select(WebBalanceTransaction).where(
                    WebBalanceTransaction.profile_token == alice_token,
                    WebBalanceTransaction.kind == "referral_bonus",
                )
            )
        ).all()
        assert alice.balance_rub == 10
        assert o_part.referral_status == "credited"
        assert len(ref_txs) == 1
        assert ref_txs[0].amount_rub == 10
