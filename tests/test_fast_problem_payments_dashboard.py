"""Tests for the redesigned fast problem payments dashboard.

Verifies:
1. Fast DB-only operations: ZERO live external YooKassa API calls on dashboard open,
   pagination, category switch, card inspection, or card back navigation.
2. Live verification strictly on-demand: authenticated YooKassa API calls occur
   ONLY when administrator explicitly clicks "🔄 Проверить в YooKassa".
3. Historical separation: records older than 7 days are hidden from the default view,
   kept intact in the read-only "🗂 Архив", and searchable by local ID.
4. Clean empty state: shows "✅ Новых проблемных платежей не обнаружено." when counts are 0.
5. Accurate classifications: local PENDING checkouts are not labeled as confirmed failures.
6. Read-only integrity: viewing dashboard, categories, and archive causes zero database mutations.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.database import async_session
from bot.handlers.admin import (
    AdminStates,
    admin_payment_command,
    admin_problem_payment_inspect,
    admin_problem_payment_refresh,
    admin_problem_payment_search_process,
    admin_problem_payments_archive_view,
    admin_problem_payments_cat_view,
    admin_problem_payments_list,
)
from bot.models import Base, Payment, PaymentMethod, PaymentStatus, Platform, Server, Tariff, TariffType, User
from bot.services.problem_payments import (
    FastDashboardSummary,
    ProblemCategory,
    ProblemScope,
    find_problem_payment_by_query,
    get_fast_dashboard_summary,
    get_problem_payment_by_id,
    get_problem_payments_page,
    verify_single_payment_live,
)

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
    from bot.services.problem_payments import _live_yk_cache
    _live_yk_cache.clear()
    monkeypatch.setattr("bot.database.async_session", db_session_factory)
    monkeypatch.setattr("bot.handlers.admin.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.async_session", db_session_factory)


@pytest.fixture
def mock_no_yookassa(monkeypatch):
    """Enforce that fetch_yookassa_payment is NEVER called unless explicitly mocked."""
    spy = AsyncMock(side_effect=AssertionError("YooKassa API must NOT be called in fast DB mode!"))
    monkeypatch.setattr("bot.services.problem_payments.fetch_yookassa_payment", spy)
    monkeypatch.setattr("bot.services.yookassa_reconciliation.fetch_yookassa_payment", spy)
    return spy


async def test_dashboard_open_triggers_zero_yookassa_calls(monkeypatch, db_session_factory, mock_no_yookassa):
    """Opening the problem payments dashboard must make ZERO YooKassa API calls."""
    monkeypatch.setattr("bot.handlers.admin.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)
    monkeypatch.setattr("bot.handlers.admin._is_admin", lambda uid: True)

    now = datetime.utcnow()
    async with db_session_factory() as session:
        user = User(telegram_id=987001, username="test_speed_user")
        tariff = Tariff(id=101, label="T101", price_rub=300, days=30, is_active=True, tariff_type=TariffType.VPN)
        session.add_all([user, tariff])
        await session.flush()

        # Add multiple pending and old payments
        for i in range(10):
            session.add(Payment(
                user_id=user.id,
                tariff_id=101,
                amount=30000,
                currency="RUB",
                method=PaymentMethod.YOOKASSA,
                status=PaymentStatus.PENDING,
                provider_payment_id=f"yk_pending_{i}",
                created_at=now - timedelta(days=i),
            ))
        await session.commit()

    cb = SimpleNamespace(
        from_user=SimpleNamespace(id=999),
        data="adm_problem_payments",
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
    )

    # Open dashboard
    await admin_problem_payments_list(cb)

    # 1. Edit text must be called with clean dashboard
    cb.message.edit_text.assert_awaited_once()
    msg_text = cb.message.edit_text.call_args.args[0]
    assert "Проблемные платежи" in msg_text
    assert "Актуальные · последние 7 дней" in msg_text

    # 2. ZERO network requests made!
    mock_no_yookassa.assert_not_called()


async def test_empty_state_shows_no_issues_message(monkeypatch, db_session_factory, mock_no_yookassa):
    """When no active problems exist in the last 7 days, show clean reassurance message."""
    monkeypatch.setattr("bot.handlers.admin.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)
    monkeypatch.setattr("bot.handlers.admin._is_admin", lambda uid: True)

    cb = SimpleNamespace(
        from_user=SimpleNamespace(id=999),
        data="adm_problem_payments",
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
    )

    await admin_problem_payments_list(cb)
    cb.message.edit_text.assert_awaited_once()
    msg_text = cb.message.edit_text.call_args.args[0]
    assert "Новых проблемных платежей не обнаружено" in msg_text
    mock_no_yookassa.assert_not_called()


async def test_category_navigation_triggers_zero_yookassa_calls(monkeypatch, db_session_factory, mock_no_yookassa):
    """Navigating between categories executes fast SQL only, ZERO YooKassa calls."""
    monkeypatch.setattr("bot.handlers.admin.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)
    monkeypatch.setattr("bot.handlers.admin._is_admin", lambda uid: True)

    now = datetime.utcnow()
    async with db_session_factory() as session:
        user = User(telegram_id=987002, username="tg_err_user")
        session.add(user)
        await session.flush()
        payment = Payment(
            user_id=user.id,
            amount=50000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            subscription_id=42,
            telegram_delivery_error="Forbidden: bot was blocked by the user",
            created_at=now - timedelta(hours=2),
        )
        session.add(payment)
        await session.commit()

    cb_cat = SimpleNamespace(
        from_user=SimpleNamespace(id=999),
        data="adm_prob_cat:tg_delivery:1",
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
    )

    await admin_problem_payments_cat_view(cb_cat)
    cb_cat.message.edit_text.assert_awaited_once()
    text = cb_cat.message.edit_text.call_args.args[0]
    assert "Ошибка доставки в TG" in text
    mock_no_yookassa.assert_not_called()


async def test_archive_view_contains_historical_and_triggers_zero_yookassa_calls(monkeypatch, db_session_factory, mock_no_yookassa):
    """Historical records (>7 days) are moved to Archive and paginated with ZERO YooKassa calls."""
    monkeypatch.setattr("bot.handlers.admin.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)
    monkeypatch.setattr("bot.handlers.admin._is_admin", lambda uid: True)

    now = datetime.utcnow()
    async with db_session_factory() as session:
        user = User(telegram_id=987003, username="hist_user")
        session.add(user)
        await session.flush()

        # Historical unlinked payment 120 days ago (like payment #80 or #106)
        p_hist = Payment(
            user_id=user.id,
            amount=30000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            subscription_id=None,
            created_at=now - timedelta(days=120),
            provider_payment_id="yk_hist_120d",
        )
        # Recent payment 1 day ago
        p_recent = Payment(
            user_id=user.id,
            amount=20000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            subscription_id=None,
            created_at=now - timedelta(days=1),
            provider_payment_id="yk_recent_1d",
        )
        session.add_all([p_hist, p_recent])
        await session.commit()
        hist_id = p_hist.id
        recent_id = p_recent.id

    # 1. Summary check: recent is counted in active, historical is in archive
    summary = await get_fast_dashboard_summary(days=7)
    assert summary.urgent_unfulfilled >= 1
    assert any(it.item_id == recent_id for it in summary.urgent_items)
    assert not any(it.item_id == hist_id for it in summary.urgent_items)
    assert summary.total_archive >= 1

    # 2. Open archive view
    cb_arch = SimpleNamespace(
        from_user=SimpleNamespace(id=999),
        data="adm_prob_archive:1",
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
    )
    await admin_problem_payments_archive_view(cb_arch)
    cb_arch.message.edit_text.assert_awaited_once()
    arch_text = cb_arch.message.edit_text.call_args.args[0]
    assert "Архив проблемных платежей" in arch_text
    assert f"#{hist_id}" in arch_text
    assert f"#{recent_id}" not in arch_text

    # ZERO external API calls!
    mock_no_yookassa.assert_not_called()


async def test_search_by_id_retrieves_records_instantly(monkeypatch, db_session_factory, mock_no_yookassa):
    """Searching by ID or UUID finds payments with zero external API calls."""
    monkeypatch.setattr("bot.handlers.admin.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)
    monkeypatch.setattr("bot.handlers.admin._is_admin", lambda uid: True)

    now = datetime.utcnow()
    async with db_session_factory() as session:
        user = User(telegram_id=987004, username="search_user")
        session.add(user)
        await session.flush()
        payment = Payment(
            user_id=user.id,
            amount=45000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            subscription_id=None,
            created_at=now - timedelta(days=90),
            provider_payment_id="yk_uuid_search_test_123",
        )
        session.add(payment)
        await session.commit()
        pay_id = payment.id

    # 1. Search by local ID
    found_by_id = await find_problem_payment_by_query(str(pay_id))
    assert found_by_id is not None
    assert found_by_id.item_id == pay_id
    assert found_by_id.username == "search_user"

    # 2. Search by provider UUID
    found_by_uuid = await find_problem_payment_by_query("yk_uuid_search_test_123")
    assert found_by_uuid is not None
    assert found_by_uuid.item_id == pay_id

    # 3. Via FSM message handler
    mock_state = SimpleNamespace(clear=AsyncMock())
    msg = SimpleNamespace(
        from_user=SimpleNamespace(id=999),
        text=str(pay_id),
        answer=AsyncMock(),
    )
    await admin_problem_payment_search_process(msg, mock_state)
    mock_state.clear.assert_awaited_once()
    msg.answer.assert_awaited_once()
    card_text = msg.answer.call_args.args[0]
    assert f"#{pay_id}" in card_text
    assert "Инспекция проблемного платежа" in card_text

    # 4. Via /payment command
    cmd_msg = SimpleNamespace(
        from_user=SimpleNamespace(id=999),
        answer=AsyncMock(),
    )
    cmd_obj = SimpleNamespace(args=str(pay_id))
    await admin_payment_command(cmd_msg, cmd_obj)
    cmd_msg.answer.assert_awaited_once()
    assert f"#{pay_id}" in cmd_msg.answer.call_args.args[0]

    mock_no_yookassa.assert_not_called()


async def test_live_yookassa_check_called_only_on_explicit_demand(monkeypatch, db_session_factory):
    """Live YooKassa API call occurs ONLY when admin explicitly clicks '🔄 Проверить в YooKassa'."""
    monkeypatch.setattr("bot.handlers.admin.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)
    monkeypatch.setattr("bot.handlers.admin._is_admin", lambda uid: True)

    yk_spy = AsyncMock(return_value={
        "id": "yk_live_demand_1",
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "250.00", "currency": "RUB"},
    })
    monkeypatch.setattr("bot.services.problem_payments.fetch_yookassa_payment", yk_spy)

    now = datetime.utcnow()
    async with db_session_factory() as session:
        user = User(telegram_id=987005, username="ondemand_user")
        session.add(user)
        await session.flush()
        payment = Payment(
            user_id=user.id,
            amount=25000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            created_at=now - timedelta(hours=1),
            provider_payment_id="yk_live_demand_1",
        )
        session.add(payment)
        await session.commit()
        pay_id = payment.id

    # Normal inspect card -> 0 YooKassa calls!
    cb_inspect = SimpleNamespace(
        from_user=SimpleNamespace(id=999),
        data=f"adm_prob_inspect:p:{pay_id}:main",
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
    )
    await admin_problem_payment_inspect(cb_inspect)
    yk_spy.assert_not_called()

    # Explicit refresh click -> EXACTLY ONE YooKassa call!
    cb_refresh = SimpleNamespace(
        from_user=SimpleNamespace(id=999),
        data=f"adm_prob_refresh:p:{pay_id}:main",
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
    )
    await admin_problem_payment_refresh(cb_refresh)
    yk_spy.assert_awaited_once_with("yk_live_demand_1")
    cb_refresh.message.edit_text.assert_awaited_once()
    refreshed_text = cb_refresh.message.edit_text.call_args.args[0]
    assert "YooKassa: succeeded (оплачен)" in refreshed_text


async def test_pending_checkout_is_not_mislabeled_as_confirmed_failure(monkeypatch, db_session_factory, mock_no_yookassa):
    """Recent local PENDING checkout is displayed as 'Ожидает оплаты', not as confirmed failure."""
    monkeypatch.setattr("bot.handlers.admin.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)

    now = datetime.utcnow()
    async with db_session_factory() as session:
        user = User(telegram_id=987006, username="pending_client")
        session.add(user)
        await session.flush()
        payment = Payment(
            user_id=user.id,
            amount=19900,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            created_at=now - timedelta(minutes=10),
            provider_payment_id="yk_pending_recent",
        )
        session.add(payment)
        await session.commit()
        pay_id = payment.id

    item = await get_problem_payment_by_id(pay_id, "payment")
    assert item is not None
    # Must be PROVIDER_PENDING, not CONFIRMED_UNFULFILLED!
    assert item.category == ProblemCategory.PROVIDER_PENDING
    assert item.fulfillment_state == "Ожидает оплаты"

    # Dashboard summary counters: urgent count must be 0!
    summary = await get_fast_dashboard_summary(days=7)
    assert summary.urgent_unfulfilled == 0


async def test_dashboard_is_strictly_read_only(monkeypatch, db_session_factory, mock_no_yookassa):
    """Browsing dashboard, categories, archive, and inspecting cards causes zero DB mutations."""
    monkeypatch.setattr("bot.handlers.admin.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)
    monkeypatch.setattr("bot.handlers.admin._is_admin", lambda uid: True)

    now = datetime.utcnow()
    async with db_session_factory() as session:
        user = User(telegram_id=987007, username="readonly_user")
        session.add(user)
        await session.flush()
        payment = Payment(
            user_id=user.id,
            amount=35000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            subscription_id=None,
            created_at=now - timedelta(days=2),
            provider_payment_id="yk_readonly_test",
        )
        session.add(payment)
        await session.commit()
        pay_id = payment.id

    async with db_session_factory() as session:
        p_before = await session.get(Payment, pay_id)
        status_before = p_before.status
        sub_id_before = p_before.subscription_id
        error_before = p_before.telegram_delivery_error

    # Browse main
    cb_main = SimpleNamespace(from_user=SimpleNamespace(id=999), data="adm_problem_payments", answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()))
    await admin_problem_payments_list(cb_main)

    # Browse cat
    cb_cat = SimpleNamespace(from_user=SimpleNamespace(id=999), data="adm_prob_cat:urgent:1", answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()))
    await admin_problem_payments_cat_view(cb_cat)

    # Browse archive
    cb_arch = SimpleNamespace(from_user=SimpleNamespace(id=999), data="adm_prob_archive:1", answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()))
    await admin_problem_payments_archive_view(cb_arch)

    # Inspect card
    cb_inspect = SimpleNamespace(from_user=SimpleNamespace(id=999), data=f"adm_prob_inspect:p:{pay_id}:main", answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()))
    await admin_problem_payment_inspect(cb_inspect)

    # Verify zero mutations
    async with db_session_factory() as session:
        p_after = await session.get(Payment, pay_id)
        assert p_after.status == status_before
        assert p_after.subscription_id == sub_id_before
        assert p_after.telegram_delivery_error == error_before


async def test_search_finds_normal_completed_payments_with_read_only_card(monkeypatch, db_session_factory, mock_no_yookassa):
    """Searching for any existing completed payment (like #306) or balance topup renders clean read-only card."""
    monkeypatch.setattr("bot.handlers.admin.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)
    monkeypatch.setattr("bot.handlers.admin._is_admin", lambda uid: True)

    from bot.keyboards.admin import problem_payment_detail_kb
    from bot.models import BalanceDirection, BalanceTopUp, BalanceTransaction, Platform, Subscription, SubStatus

    now = datetime.utcnow()
    async with db_session_factory() as session:
        user = User(telegram_id=987010, username="normal_client")
        server = Server(id=1, name="srv1", host="1.1.1.1", location="Amsterdam")
        session.add_all([user, server])
        await session.flush()

        sub = Subscription(
            id=306,
            user_id=user.id,
            server_id=server.id,
            tariff_months=1,
            client_name="client306",
            platform=Platform.ANDROID,
            status=SubStatus.ACTIVE,
            expires_at=now + timedelta(days=30),
        )
        session.add(sub)
        await session.flush()

        # Normal completed payment with linked subscription (e.g. #306)
        pay_306 = Payment(
            id=306,
            user_id=user.id,
            amount=37400,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            subscription_id=306,
            created_at=now - timedelta(days=3),
            provider_payment_id="yk_306_success",
        )

        # Normal completed balance topup (e.g. #55) with ledger credit
        topup_55 = BalanceTopUp(
            id=55,
            user_id=user.id,
            telegram_id=user.telegram_id,
            amount_rub=500.0,
            provider="yookassa",
            status="completed",
            created_at=now - timedelta(days=2),
            completed_at=now - timedelta(days=2),
            provider_payment_id="yk_topup_55_success",
        )
        tx_55 = BalanceTransaction(
            user_id=user.id,
            kind="TOPUP",
            direction=BalanceDirection.CREDIT,
            amount_rub=500.0,
            balance_after_rub=500.0,
            description="Пополнение баланса",
            source_type="yookassa_topup",
            source_id="yk_topup_55_success",
            created_at=now - timedelta(days=2),
        )
        session.add_all([pay_306, topup_55, tx_55])
        await session.commit()

    # 1. Search payment #306
    item_306 = await find_problem_payment_by_query("306")
    assert item_306 is not None
    assert item_306.item_id == 306
    assert item_306.category == ProblemCategory.PROCESSED
    assert "✅ Обработан" in item_306.fulfillment_state

    # Verify detail keyboard for processed item has NO reconcile / retry_tg actions
    kb_306 = problem_payment_detail_kb(
        item_type="payment",
        item_id=306,
        user_id=user.id,
        provider_payment_id="yk_306_success",
        page="search",
        category=item_306.category,
    )
    callbacks = [btn.callback_data for row in kb_306.inline_keyboard for btn in row]
    assert not any("reconcile" in cb for cb in callbacks)
    assert not any("retry_tg" in cb for cb in callbacks)
    button_texts = [btn.text for row in kb_306.inline_keyboard for btn in row]
    assert "✅ Обработан" in button_texts

    # 2. Search topup #55
    item_55 = await find_problem_payment_by_query("55")
    assert item_55 is not None
    assert item_55.item_id == 55
    assert item_55.category == ProblemCategory.PROCESSED
    assert "✅ Баланс пополнен" in item_55.fulfillment_state

    # 3. Via direct /payment 306 command
    cmd_msg = SimpleNamespace(from_user=SimpleNamespace(id=999), answer=AsyncMock())
    cmd_obj = SimpleNamespace(args="306")
    await admin_payment_command(cmd_msg, cmd_obj)
    cmd_msg.answer.assert_awaited_once()
    rendered_text = cmd_msg.answer.call_args.args[0]
    assert "#306" in rendered_text
    assert "✅ Обработан" in rendered_text

    mock_no_yookassa.assert_not_called()


async def test_balance_topup_visibility_in_review_urgent_and_archive(monkeypatch, db_session_factory, mock_no_yookassa):
    """Problematic BalanceTopUps appear in Review (missing ID), Urgent (paid uncredited), and Archive."""
    monkeypatch.setattr("bot.handlers.admin.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)

    from bot.models import BalanceTopUp
    from bot.services.problem_payments import _get_active_problem_categories, set_cached_live_status

    now = datetime.utcnow()
    async with db_session_factory() as session:
        user = User(telegram_id=987020, username="topup_user")
        session.add(user)
        await session.flush()

        # 1. TopUp missing provider ID (>15m) in active window -> should be in 'review'
        t_review = BalanceTopUp(
            id=101,
            user_id=user.id,
            telegram_id=user.telegram_id,
            amount_rub=250.0,
            status="pending",
            provider_payment_id=None,
            created_at=now - timedelta(minutes=25),
        )

        # 2. TopUp with provider ID verified succeeded via cache but uncredited -> should be in 'urgent'
        t_urgent = BalanceTopUp(
            id=102,
            user_id=user.id,
            telegram_id=user.telegram_id,
            amount_rub=300.0,
            status="pending",
            provider_payment_id="yk_topup_urgent_paid",
            created_at=now - timedelta(hours=2),
        )

        # 3. Ordinary fresh pending checkout (<15m) -> should NOT appear in active problem categories
        t_normal_pending = BalanceTopUp(
            id=103,
            user_id=user.id,
            telegram_id=user.telegram_id,
            amount_rub=150.0,
            status="pending",
            provider_payment_id="yk_topup_awaiting",
            created_at=now - timedelta(minutes=5),
        )

        # 4. Historical topup (>7 days) -> should be in 'archive'
        t_arch = BalanceTopUp(
            id=104,
            user_id=user.id,
            telegram_id=user.telegram_id,
            amount_rub=400.0,
            status="pending",
            provider_payment_id="yk_topup_old",
            created_at=now - timedelta(days=20),
        )

        session.add_all([t_review, t_urgent, t_normal_pending, t_arch])
        await session.commit()

    # Seed cache for t_urgent so classifier knows it was paid at provider
    set_cached_live_status("topup", 102, {
        "status": "succeeded",
        "paid": True,
        "status_label": "YooKassa: succeeded (оплачен)",
    })

    # 1. Verify active categorization:
    cat_map = await _get_active_problem_categories(days=7, now=now)
    assert any(it.item_id == 101 and it.item_type == "topup" for it in cat_map["review"])
    assert any(it.item_id == 102 and it.item_type == "topup" for it in cat_map["urgent"])
    # Normal pending topup must NOT be in active problem lists
    assert not any(it.item_id == 103 for items in cat_map.values() for it in items)

    # 2. Verify archive retrieval and count consistency
    summary = await get_fast_dashboard_summary(days=7)
    arch_items, arch_count, arch_pages = await get_problem_payments_page(
        scope=ProblemScope.ARCHIVE,
        page=1,
        per_page=6,
    )
    assert summary.total_archive == arch_count
    assert any(it.item_id == 104 and it.item_type == "topup" for it in arch_items)

    mock_no_yookassa.assert_not_called()


async def test_consistent_counters_and_filtered_lists(monkeypatch, db_session_factory, mock_no_yookassa):
    """Counters on dashboard summary strictly match the actual filtered items on category pages."""
    monkeypatch.setattr("bot.handlers.admin.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)

    from bot.models import AdminActionLog

    now = datetime.utcnow()
    async with db_session_factory() as session:
        user1 = User(telegram_id=987031, username="urgent_client")
        user2 = User(telegram_id=987032, username="manual_client")
        user = User(telegram_id=987030, username="consistency_user")
        session.add_all([user1, user2, user])
        await session.flush()

        # 1. Urgent unfulfilled payment (completed without sub, no manual evidence)
        p_urgent = Payment(
            id=201,
            user_id=user1.id,
            amount=30000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            subscription_id=None,
            created_at=now - timedelta(hours=3),
            provider_payment_id="yk_urgent_201",
        )

        # 2. Manually fulfilled payment (must NOT be counted in urgent!)
        p_manually_fulfilled = Payment(
            id=202,
            user_id=user2.id,
            amount=30000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            subscription_id=None,
            created_at=now - timedelta(hours=4),
            provider_payment_id="yk_manual_202",
        )
        admin_log = AdminActionLog(
            actor_telegram_id=999999,
            action="grant_access",
            status="success",
            target_telegram_id=user2.telegram_id,
            summary="Manually granted for payment #202",
            details="Manually granted for payment #202",
            created_at=now - timedelta(hours=3, minutes=30),
        )

        # 3. Telegram delivery failure
        p_tg = Payment(
            id=203,
            user_id=user.id,
            amount=30000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            subscription_id=42,
            telegram_delivery_error="Bad Request: chat not found",
            created_at=now - timedelta(hours=2),
            provider_payment_id="yk_tg_203",
        )

        # 4. Supplier failure
        p_supp = Payment(
            id=204,
            user_id=user.id,
            amount=30000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            provisioning_failure_code="upstream_server_timeout",
            created_at=now - timedelta(hours=1),
            provider_payment_id="yk_supp_204",
        )

        # 5. Review issue: pending payment missing provider ID (>15m)
        p_rev = Payment(
            id=205,
            user_id=user.id,
            amount=20000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id=None,
            created_at=now - timedelta(minutes=20),
        )

        # 6. Ordinary fresh pending checkout (<15m) -> excluded from active problem categories
        p_pending = Payment(
            id=206,
            user_id=user.id,
            amount=20000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_pending_206",
            created_at=now - timedelta(minutes=5),
        )

        session.add_all([p_urgent, p_manually_fulfilled, admin_log, p_tg, p_supp, p_rev, p_pending])
        await session.commit()

    summary = await get_fast_dashboard_summary(days=7)
    assert summary.urgent_unfulfilled == 1  # Exactly #201, NOT #202 and NOT #206
    assert summary.tg_delivery_failed == 1   # Exactly #203
    assert summary.supplier_failed == 1      # Exactly #204
    assert summary.needs_review == 1         # Exactly #205
    assert summary.total_active == 4

    # Verify category pages return exactly the same count and matching items
    for cat, expected_id in [("urgent", 201), ("tg_delivery", 203), ("supplier", 204), ("review", 205)]:
        items, count, pages = await get_problem_payments_page(category=cat, scope=ProblemScope.ACTIVE)
        assert count == 1
        assert len(items) == 1
        assert items[0].item_id == expected_id

    mock_no_yookassa.assert_not_called()


async def test_search_fsm_cancellation_on_cancel_and_menu_navigation(monkeypatch, db_session_factory):
    """Canceling search or navigating away cleanly clears AdminStates.waiting_problem_payment_search."""
    monkeypatch.setattr("bot.handlers.admin.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)
    monkeypatch.setattr("bot.handlers.admin._is_admin", lambda uid: True)

    from bot.handlers.admin import admin_problem_payment_search_start

    class DummyState:
        def __init__(self):
            self.current_state = None

        async def set_state(self, st):
            self.current_state = st

        async def get_state(self):
            return self.current_state

        async def clear(self):
            self.current_state = None

    state = DummyState()
    cb_start = SimpleNamespace(
        from_user=SimpleNamespace(id=999),
        data="adm_prob_search",
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
    )

    # 1. Start search
    await admin_problem_payment_search_start(cb_start, state)
    assert await state.get_state() == AdminStates.waiting_problem_payment_search

    # 2. Click cancel (which triggers admin_problem_payments_list)
    cb_cancel = SimpleNamespace(
        from_user=SimpleNamespace(id=999),
        data="adm_problem_payments",
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
    )
    await admin_problem_payments_list(cb_cancel, state=state)
    assert await state.get_state() is None

    # 3. Enter search again and cancel via /cancel command
    await admin_problem_payment_search_start(cb_start, state)
    assert await state.get_state() == AdminStates.waiting_problem_payment_search

    msg_cancel = SimpleNamespace(
        from_user=SimpleNamespace(id=999),
        text="/cancel",
        answer=AsyncMock(),
    )
    await admin_problem_payment_search_process(msg_cancel, state=state)
    assert await state.get_state() is None
    msg_cancel.answer.assert_awaited_once()
    assert "Поиск отменен" in msg_cancel.answer.call_args.args[0]


async def test_live_yookassa_cache_ttl_and_bounded_rate_limit(monkeypatch, db_session_factory):
    """Live YooKassa checks respect 5-minute TTL and 3-second debounce rate limiting."""
    monkeypatch.setattr("bot.handlers.admin.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)

    yk_spy = AsyncMock(return_value={
        "id": "yk_ttl_test_1",
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "300.00", "currency": "RUB"},
    })
    monkeypatch.setattr("bot.services.problem_payments.fetch_yookassa_payment", yk_spy)

    now = datetime.utcnow()
    async with db_session_factory() as session:
        user = User(telegram_id=987040, username="ttl_user")
        session.add(user)
        await session.flush()
        payment = Payment(
            id=350,
            user_id=user.id,
            amount=30000,
            currency="RUB",
            status=PaymentStatus.PENDING,
            method=PaymentMethod.YOOKASSA,
            provider_payment_id="yk_ttl_test_1",
            created_at=now - timedelta(hours=1),
        )
        session.add(payment)
        await session.commit()

    # 1. First forced live check -> external API called once
    item1, msg1 = await verify_single_payment_live("payment", 350, force=True)
    assert yk_spy.await_count == 1
    assert "проверено" in msg1

    # 2. Cached check (force=False) -> external API NOT called
    item2, msg2 = await verify_single_payment_live("payment", 350, force=False)
    assert yk_spy.await_count == 1
    assert "из кэша" in msg2

    # 3. Rapid second forced refresh (<3s) -> rate-limited debounce, external API NOT called again
    item3, msg3 = await verify_single_payment_live("payment", 350, force=True)
    assert yk_spy.await_count == 1
    assert "проверено только что" in msg3


async def test_completed_linked_subscription_failure_codes_vs_processed(monkeypatch, db_session_factory, mock_no_yookassa):
    """Do not hide failed payments as processed:
    - Completed + linked subscription + unresolved processing failure -> FAILED_SUPPLIER
    - Completed + linked subscription + normal clean state -> PROCESSED
    - Completed + linked subscription + Telegram delivery error -> TELEGRAM_DELIVERY_FAILED
    """
    monkeypatch.setattr("bot.handlers.admin.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)

    from bot.models import Subscription, SubStatus
    from bot.services.problem_payments import _get_active_problem_categories, classify_candidate_db_only

    now = datetime.utcnow()
    async with db_session_factory() as session:
        user = User(telegram_id=987050, username="fail_check_user")
        server = Server(id=10, name="srv10", host="10.10.10.10", location="NL")
        session.add_all([user, server])
        await session.flush()

        sub1 = Subscription(
            id=501,
            user_id=user.id,
            server_id=server.id,
            tariff_months=1,
            client_name="c501",
            platform=Platform.ANDROID,
            status=SubStatus.ACTIVE,
            expires_at=now + timedelta(days=30),
        )
        sub2 = Subscription(
            id=502,
            user_id=user.id,
            server_id=server.id,
            tariff_months=1,
            client_name="c502",
            platform=Platform.ANDROID,
            status=SubStatus.ACTIVE,
            expires_at=now + timedelta(days=30),
        )
        sub3 = Subscription(
            id=503,
            user_id=user.id,
            server_id=server.id,
            tariff_months=1,
            client_name="c503",
            platform=Platform.ANDROID,
            status=SubStatus.ACTIVE,
            expires_at=now + timedelta(days=30),
        )
        session.add_all([sub1, sub2, sub3])
        await session.flush()

        # 1. Completed + linked sub + unresolved processing failure
        p_failed = Payment(
            id=601,
            user_id=user.id,
            amount=30000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            subscription_id=sub1.id,
            provisioning_failure_code="upstream_server_timeout",
            created_at=now - timedelta(hours=1),
            provider_payment_id="yk_fail_601",
        )

        # 2. Completed + linked sub + normal clean state
        p_clean = Payment(
            id=602,
            user_id=user.id,
            amount=30000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            subscription_id=sub2.id,
            provisioning_failure_code=None,
            telegram_delivery_error=None,
            created_at=now - timedelta(hours=2),
            provider_payment_id="yk_clean_602",
        )

        # 3. Completed + linked sub + Telegram delivery error (priority preserved)
        p_tg_err = Payment(
            id=603,
            user_id=user.id,
            amount=30000,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            subscription_id=sub3.id,
            telegram_delivery_error="Bad Request: chat not found",
            created_at=now - timedelta(hours=3),
            provider_payment_id="yk_tg_603",
        )

        session.add_all([p_failed, p_clean, p_tg_err])
        await session.commit()

    # Verify classification of each candidate
    res_failed = await classify_candidate_db_only({"type": "payment", "obj": p_failed, "user": user}, now=now)
    assert res_failed.category == ProblemCategory.FAILED_SUPPLIER
    assert "upstream_server_timeout" in res_failed.fulfillment_state

    res_clean = await classify_candidate_db_only({"type": "payment", "obj": p_clean, "user": user}, now=now)
    assert res_clean.category == ProblemCategory.PROCESSED
    assert "✅ Обработан" in res_clean.fulfillment_state

    res_tg = await classify_candidate_db_only({"type": "payment", "obj": p_tg_err, "user": user}, now=now)
    assert res_tg.category == ProblemCategory.TELEGRAM_DELIVERY_FAILED
    assert "сбой отправки в TG" in res_tg.fulfillment_state

    # Verify dashboard category placement
    cat_map = await _get_active_problem_categories(days=7, now=now)
    assert any(it.item_id == 601 for it in cat_map["supplier"])
    assert any(it.item_id == 603 for it in cat_map["tg_delivery"])
    assert not any(it.item_id == 602 for items in cat_map.values() for it in items)


async def test_paid_vhq_supplier_pending_is_suppressed_until_stalled(monkeypatch, db_session_factory, mock_no_yookassa):
    """Healthy VHQ background recovery is quiet; a stalled paid renewal is actionable."""
    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)
    from bot.models import Subscription, SubStatus, VhqSubscriptionFulfillment
    from bot.services.problem_payments import (
        _get_active_problem_categories,
        alert_admins_if_problem_payments_detected,
        classify_candidate_db_only,
    )

    now = datetime.utcnow()
    async with db_session_factory() as session:
        user = User(telegram_id=987060, username="vhq_pending_user")
        server = Server(id=60, name="vhq-pending-server", host="vhq.test", location="test")
        tariff = Tariff(
            id=60,
            label="Baza Pro",
            price_rub=399,
            price_stars=0,
            days=30,
            is_active=True,
            tariff_type=TariffType.VPN,
            vhq_tier="basic",
        )
        session.add_all([user, server, tariff])
        await session.flush()
        sub = Subscription(
            id=60,
            user_id=user.id,
            tariff_id=tariff.id,
            server_id=server.id,
            tariff_months=1,
            tariff_days=30,
            status=SubStatus.EXPIRED,
            platform=Platform.ANDROID,
            client_name="vhq_pending_order",
            expires_at=now - timedelta(days=1),
            vhq_active_until=now - timedelta(days=1),
        )
        payment = Payment(
            id=60,
            user_id=user.id,
            tariff_id=tariff.id,
            amount=39900,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            provider_payment_id="yk_vhq_pending_60",
            provider_status="succeeded",
            provider_paid=True,
            provisioning_failure_code="supplier_fulfillment_pending",
            created_at=now - timedelta(minutes=5),
        )
        fulfillment = VhqSubscriptionFulfillment(
            id=60,
            subscription_id=sub.id,
            user_id=user.id,
            payment_id=payment.id,
            source_type="payment",
            source_id="payment:60",
            action="renew",
            status="pending",
            attempts=1,
            max_attempts=5,
            created_at=now - timedelta(minutes=5),
            next_attempt_at=now,
        )
        session.add_all([sub, payment, fulfillment])
        await session.commit()

    async with db_session_factory() as session:
        payment = await session.get(Payment, 60)
        user = await session.get(User, payment.user_id)
    healthy = await classify_candidate_db_only({"type": "payment", "obj": payment, "user": user}, now=now)
    assert healthy is None
    healthy_map = await _get_active_problem_categories(days=7, now=now)
    assert not any(item.item_id == 60 for items in healthy_map.values() for item in items)

    async with db_session_factory() as session:
        payment = await session.get(Payment, 60)
        payment.provisioning_failure_code = "yookassa_amount_mismatch"
        await session.commit()
        user = await session.get(User, payment.user_id)
    validation_failure = await classify_candidate_db_only(
        {"type": "payment", "obj": payment, "user": user}, now=now
    )
    assert validation_failure is not None
    assert validation_failure.category == ProblemCategory.CONFIRMED_UNFULFILLED

    async with db_session_factory() as session:
        fulfillment = await session.get(VhqSubscriptionFulfillment, 60)
        fulfillment.created_at = now - timedelta(minutes=31)
        payment = await session.get(Payment, 60)
        payment.provisioning_failure_code = "supplier_fulfillment_pending"
        await session.commit()
        payment = await session.get(Payment, 60)
        user = await session.get(User, payment.user_id)
    stalled = await classify_candidate_db_only({"type": "payment", "obj": payment, "user": user}, now=now)
    assert stalled is not None
    assert stalled.category == ProblemCategory.FAILED_SUPPLIER
    stalled_map = await _get_active_problem_categories(days=7, now=now)
    assert any(item.item_id == 60 for item in stalled_map["supplier"])

    delivered = AsyncMock(return_value=True)
    monkeypatch.setattr("bot.services.problem_payments.notify_admins_issue", delivered)
    bot = SimpleNamespace()
    await alert_admins_if_problem_payments_detected(bot)
    await alert_admins_if_problem_payments_detected(bot)
    delivered.assert_awaited_once()


async def test_balance_topup_real_credit_evidence_in_ledger(monkeypatch, db_session_factory, mock_no_yookassa):
    """Balance topup must have real credit evidence in BalanceTransaction ledger:
    - completed + completed_at=NULL + matching BalanceTransaction -> PROCESSED (not failure)
    - completed + no matching BalanceTransaction -> DELIVERY_UNCERTAIN (manual review, NOT urgent)
    - active dashboard, archive, and search use identical classification
    """
    monkeypatch.setattr("bot.handlers.admin.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)

    from bot.models import BalanceDirection, BalanceTopUp, BalanceTransaction
    from bot.services.problem_payments import _get_active_problem_categories, classify_candidate_db_only

    now = datetime.utcnow()
    async with db_session_factory() as session:
        user = User(telegram_id=987060, username="ledger_user")
        session.add(user)
        await session.flush()

        # 1. Topup with completed_at=NULL, BUT funds were credited in ledger -> PROCESSED
        t_credited = BalanceTopUp(
            id=701,
            user_id=user.id,
            telegram_id=user.telegram_id,
            amount_rub=200.0,
            status="completed",
            provider_payment_id="yk_topup_701",
            completed_at=None,
            created_at=now - timedelta(hours=2),
        )
        tx_701 = BalanceTransaction(
            user_id=user.id,
            kind="TOPUP",
            direction=BalanceDirection.CREDIT,
            amount_rub=200.0,
            balance_after_rub=200.0,
            description="Пополнение баланса",
            source_type="yookassa_topup",
            source_id="yk_topup_701",
            created_at=now - timedelta(hours=2),
        )

        # 2. Topup completed, but NO matching BalanceTransaction -> manual review (DELIVERY_UNCERTAIN)
        t_uncredited = BalanceTopUp(
            id=702,
            user_id=user.id,
            telegram_id=user.telegram_id,
            amount_rub=350.0,
            status="completed",
            provider_payment_id="yk_topup_702_uncredited",
            completed_at=now - timedelta(hours=1),
            created_at=now - timedelta(hours=1),
        )

        # 3. Historical uncredited topup (>7d) -> included in archive
        t_arch_uncredited = BalanceTopUp(
            id=703,
            user_id=user.id,
            telegram_id=user.telegram_id,
            amount_rub=500.0,
            status="completed",
            provider_payment_id="yk_topup_703_old_uncredited",
            completed_at=now - timedelta(days=15),
            created_at=now - timedelta(days=15),
        )

        # 4. Historical credited topup (>7d) -> excluded from archive (normal completed topup)
        t_arch_credited = BalanceTopUp(
            id=704,
            user_id=user.id,
            telegram_id=user.telegram_id,
            amount_rub=600.0,
            status="completed",
            provider_payment_id="yk_topup_704_old_credited",
            completed_at=now - timedelta(days=20),
            created_at=now - timedelta(days=20),
        )
        tx_704 = BalanceTransaction(
            user_id=user.id,
            kind="TOPUP",
            direction=BalanceDirection.CREDIT,
            amount_rub=600.0,
            balance_after_rub=800.0,
            description="Пополнение баланса",
            source_type="yookassa_topup",
            source_id="yk_topup_704_old_credited",
            created_at=now - timedelta(days=20),
        )

        session.add_all([t_credited, tx_701, t_uncredited, t_arch_uncredited, t_arch_credited, tx_704])
        await session.commit()

    # 1. Classification check
    res_701 = await classify_candidate_db_only({"type": "topup", "obj": t_credited, "user": user}, now=now)
    assert res_701.category == ProblemCategory.PROCESSED
    assert "✅ Баланс пополнен" in res_701.fulfillment_state

    res_702 = await classify_candidate_db_only({"type": "topup", "obj": t_uncredited, "user": user}, now=now)
    assert res_702.category == ProblemCategory.DELIVERY_UNCERTAIN
    assert "Требует ручной проверки" in res_702.fulfillment_state
    assert "BalanceTransaction не найдено" in res_702.details

    # 2. Active dashboard check: 702 appears in review (NOT urgent), 701 is not a problem
    cat_map = await _get_active_problem_categories(days=7, now=now)
    assert any(it.item_id == 702 and it.item_type == "topup" for it in cat_map["review"])
    assert not any(it.item_id == 702 for it in cat_map["urgent"])
    assert not any(it.item_id == 701 for items in cat_map.values() for it in items)

    # 3. Archive check: 703 (uncredited) is in archive, 704 (credited) is excluded
    summary = await get_fast_dashboard_summary(days=7)
    arch_items, arch_count, _ = await get_problem_payments_page(scope=ProblemScope.ARCHIVE, page=1, per_page=10)
    assert any(it.item_id == 703 and it.item_type == "topup" for it in arch_items)
    assert not any(it.item_id == 704 for it in arch_items)

    # 4. Search check: search finds both with accurate labels
    item_s_701 = await find_problem_payment_by_query("701")
    assert item_s_701.category == ProblemCategory.PROCESSED
    item_s_702 = await find_problem_payment_by_query("702")
    assert item_s_702.category == ProblemCategory.DELIVERY_UNCERTAIN


async def test_aiogram_router_dispatch_fsm_search_commands(monkeypatch, db_session_factory):
    """Test actual aiogram router dispatch for waiting_problem_payment_search:
    - /cancel clears state and prints 'Поиск отменен.'
    - /payment <id> clears state and shows payment card
    - other admin FSM states remain protected by _guard_admin_commands
    """
    monkeypatch.setattr("bot.handlers.admin.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)

    from aiogram import Bot, Dispatcher
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.types import Chat, Message, Update, User as TgUser
    from bot.handlers.admin import router as admin_router
    from bot.models import Subscription, SubStatus

    now = datetime.utcnow()
    async with db_session_factory() as session:
        user = User(telegram_id=987070, username="router_admin")
        server = Server(id=20, name="srv20", host="20.20.20.20", location="NL")
        session.add_all([user, server])
        await session.flush()
        sub = Subscription(
            id=306,
            user_id=user.id,
            server_id=server.id,
            tariff_months=1,
            client_name="c306",
            platform=Platform.ANDROID,
            status=SubStatus.ACTIVE,
            expires_at=now + timedelta(days=30),
        )
        session.add(sub)
        await session.flush()

        pay = Payment(
            id=306,
            user_id=user.id,
            amount=37400,
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.COMPLETED,
            subscription_id=sub.id,
            provider_payment_id="yk_router_306",
            created_at=now - timedelta(days=2),
        )
        session.add(pay)
        await session.commit()

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(admin_router)

    from aiogram.methods import SendMessage

    sent_requests = []
    async def fake_make_request(bot, method, timeout=None):
        sent_requests.append(method)
        if isinstance(method, SendMessage):
            return Message(
                message_id=999,
                date=datetime.now(),
                chat=Chat(id=method.chat_id, type="private"),
                text=method.text,
            )
        return True

    bot = Bot(token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz")
    bot.session.make_request = fake_make_request

    admin_id = 987070
    monkeypatch.setattr("bot.config.settings.admin_ids", [admin_id])

    key = StorageKey(bot_id=bot.id, chat_id=admin_id, user_id=admin_id)

    # 1. State: waiting_problem_payment_search + message: /cancel
    sent_requests.clear()
    await dp.storage.set_state(key=key, state=AdminStates.waiting_problem_payment_search)
    msg_cancel = Message(
        message_id=1,
        date=datetime.now(),
        chat=Chat(id=admin_id, type="private"),
        from_user=TgUser(id=admin_id, is_bot=False, first_name="Admin"),
        text="/cancel",
    )
    await dp.feed_update(bot=bot, update=Update(update_id=1, message=msg_cancel))

    assert await dp.storage.get_state(key=key) is None
    assert any("Поиск отменен" in getattr(m, "text", "") for m in sent_requests)

    # 2. State: waiting_problem_payment_search + message: /payment 306
    sent_requests.clear()
    await dp.storage.set_state(key=key, state=AdminStates.waiting_problem_payment_search)
    msg_pay = Message(
        message_id=2,
        date=datetime.now(),
        chat=Chat(id=admin_id, type="private"),
        from_user=TgUser(id=admin_id, is_bot=False, first_name="Admin"),
        text="/payment 306",
    )
    await dp.feed_update(bot=bot, update=Update(update_id=2, message=msg_pay))

    assert await dp.storage.get_state(key=key) is None
    assert any("Платеж" in getattr(m, "text", "") and "306" in getattr(m, "text", "") for m in sent_requests)

    # 3. State: waiting_user_search (different admin state) + message: /cancel
    sent_requests.clear()
    await dp.storage.set_state(key=key, state=AdminStates.waiting_user_search)
    msg_guard = Message(
        message_id=3,
        date=datetime.now(),
        chat=Chat(id=admin_id, type="private"),
        from_user=TgUser(id=admin_id, is_bot=False, first_name="Admin"),
        text="/cancel",
    )
    await dp.feed_update(bot=bot, update=Update(update_id=3, message=msg_guard))

    # State was NOT cleared because it was guarded!
    assert await dp.storage.get_state(key=key) == AdminStates.waiting_user_search.state
    assert any("Завершите текущее действие" in getattr(m, "text", "") for m in sent_requests)


async def test_balance_ledger_correlation_regression_scenarios(monkeypatch, db_session_factory, mock_no_yookassa):
    """Verify strict BalanceTransaction ledger correlation for BalanceTopUp:
    1. Paid topup without credit + unrelated same-amount cashback => DELIVERY_UNCERTAIN.
    2. Paid topup + matching transaction with DEBIT direction => not credited.
    3. Transaction for another user with matching source_id => not credited; archive record remains visible.
    4. Genuine YooKassa/Telegram topup with correct TOPUP/CREDIT ledger => PROCESSED.
    5. Active counters, archive pagination and search classify these records consistently.
    """
    monkeypatch.setattr("bot.handlers.admin.async_session", db_session_factory)
    monkeypatch.setattr("bot.services.problem_payments.async_session", db_session_factory)

    from bot.models import BalanceDirection, BalanceTopUp, BalanceTransaction, BalanceTransactionKind, User
    from bot.services.problem_payments import (
        _get_active_problem_categories,
        classify_candidate_db_only,
        find_problem_payment_by_query,
        get_fast_dashboard_summary,
        get_problem_payments_page,
    )

    now = datetime.utcnow()
    async with db_session_factory() as session:
        user_a = User(id=801, telegram_id=987801, username="user_a")
        user_b = User(id=802, telegram_id=987802, username="user_b")
        user_other = User(id=804, telegram_id=987804, username="user_other")
        session.add_all([user_a, user_b, user_other])
        await session.flush()

        # 1. Paid topup #801 without credit + unrelated same-amount cashback for user_a
        topup_801 = BalanceTopUp(
            id=801,
            user_id=user_a.id,
            telegram_id=user_a.telegram_id,
            amount_rub=500.0,
            status="completed",
            provider="yookassa",
            provider_payment_id="yk_topup_801",
            completed_at=now - timedelta(hours=2),
            created_at=now - timedelta(hours=2),
        )
        tx_cashback_801 = BalanceTransaction(
            user_id=user_a.id,
            kind=BalanceTransactionKind.CASHBACK,
            direction=BalanceDirection.CREDIT,
            amount_rub=500.0,
            balance_after_rub=500.0,
            description="Кэшбэк за оплату",
            source_type="payment",
            source_id="unrelated_pay_999",
            created_at=now - timedelta(hours=2),
        )

        # 2. Paid topup #802 + matching transaction with DEBIT direction (e.g. payment discount debit)
        topup_802 = BalanceTopUp(
            id=802,
            user_id=user_b.id,
            telegram_id=user_b.telegram_id,
            amount_rub=300.0,
            status="completed",
            provider="yookassa",
            provider_payment_id="yk_topup_802",
            completed_at=now - timedelta(hours=2),
            created_at=now - timedelta(hours=2),
        )
        tx_debit_802 = BalanceTransaction(
            user_id=user_b.id,
            kind=BalanceTransactionKind.TOPUP,
            direction=BalanceDirection.DEBIT,
            amount_rub=300.0,
            balance_after_rub=0.0,
            description="Списание",
            source_type="payment_discount",
            source_id="yk_topup_802",
            created_at=now - timedelta(hours=2),
        )

        # 3. Historical uncredited topup (>7d) #803 for user_a, but matching source_id transaction belongs to user_other
        topup_803_arch = BalanceTopUp(
            id=803,
            user_id=user_a.id,
            telegram_id=user_a.telegram_id,
            amount_rub=400.0,
            status="completed",
            provider="yookassa",
            provider_payment_id="yk_topup_803_shared",
            completed_at=now - timedelta(days=12),
            created_at=now - timedelta(days=12),
        )
        tx_other_user = BalanceTransaction(
            user_id=user_other.id,  # Other user!
            kind=BalanceTransactionKind.TOPUP,
            direction=BalanceDirection.CREDIT,
            amount_rub=400.0,
            balance_after_rub=400.0,
            description="Пополнение баланса",
            source_type="yookassa_topup",
            source_id="yk_topup_803_shared",
            created_at=now - timedelta(days=12),
        )

        # 4. Genuine YooKassa (#805) and Telegram (#806) topups with correct TOPUP/CREDIT ledger
        topup_805_genuine_yk = BalanceTopUp(
            id=805,
            user_id=user_a.id,
            telegram_id=user_a.telegram_id,
            amount_rub=250.0,
            status="completed",
            provider="yookassa",
            provider_payment_id="yk_topup_805_gen",
            completed_at=now - timedelta(hours=1),
            created_at=now - timedelta(hours=1),
        )
        tx_805 = BalanceTransaction(
            user_id=user_a.id,
            kind=BalanceTransactionKind.TOPUP,
            direction=BalanceDirection.CREDIT,
            amount_rub=250.0,
            balance_after_rub=750.0,
            description="Пополнение баланса",
            source_type="yookassa_topup",
            source_id="yk_topup_805_gen",
            created_at=now - timedelta(hours=1),
        )

        topup_806_genuine_tg = BalanceTopUp(
            id=806,
            user_id=user_b.id,
            telegram_id=user_b.telegram_id,
            amount_rub=150.0,
            status="completed",
            provider="telegram",
            provider_payment_id="tg_topup_806_gen",
            completed_at=now - timedelta(hours=1),
            created_at=now - timedelta(hours=1),
        )
        tx_806 = BalanceTransaction(
            user_id=user_b.id,
            kind=BalanceTransactionKind.TOPUP,
            direction=BalanceDirection.CREDIT,
            amount_rub=150.0,
            balance_after_rub=150.0,
            description="Пополнение баланса",
            source_type="telegram_topup",
            source_id="tg_topup_806_gen",
            created_at=now - timedelta(hours=1),
        )

        session.add_all([
            topup_801, tx_cashback_801,
            topup_802, tx_debit_802,
            topup_803_arch, tx_other_user,
            topup_805_genuine_yk, tx_805,
            topup_806_genuine_tg, tx_806,
        ])
        await session.commit()

    # Regression 1: Paid topup without credit + unrelated same-amount cashback => DELIVERY_UNCERTAIN
    res_801 = await classify_candidate_db_only({"type": "topup", "obj": topup_801, "user": user_a}, now=now)
    assert res_801 is not None
    assert res_801.category == ProblemCategory.DELIVERY_UNCERTAIN
    assert "Требует ручной проверки" in res_801.fulfillment_state

    # Regression 2: Paid topup + matching transaction with DEBIT direction => not credited (DELIVERY_UNCERTAIN)
    res_802 = await classify_candidate_db_only({"type": "topup", "obj": topup_802, "user": user_b}, now=now)
    assert res_802 is not None
    assert res_802.category == ProblemCategory.DELIVERY_UNCERTAIN
    assert "Требует ручной проверки" in res_802.fulfillment_state

    # Regression 3: Transaction for another user with matching source_id => not credited, archive record remains visible
    res_803 = await classify_candidate_db_only({"type": "topup", "obj": topup_803_arch, "user": user_a}, now=now)
    assert res_803 is not None
    assert res_803.category == ProblemCategory.DELIVERY_UNCERTAIN

    # Regression 4: Genuine YooKassa/Telegram topups with correct TOPUP/CREDIT ledger => PROCESSED
    res_805 = await classify_candidate_db_only({"type": "topup", "obj": topup_805_genuine_yk, "user": user_a}, now=now)
    assert res_805 is not None
    assert res_805.category == ProblemCategory.PROCESSED
    assert "✅ Баланс пополнен" in res_805.fulfillment_state

    res_806 = await classify_candidate_db_only({"type": "topup", "obj": topup_806_genuine_tg, "user": user_b}, now=now)
    assert res_806 is not None
    assert res_806.category == ProblemCategory.PROCESSED
    assert "✅ Баланс пополнен" in res_806.fulfillment_state

    # Regression 5: Active counters, archive pagination and search classify these records consistently
    # Active categories: 801 and 802 are in review; 805 and 806 (processed) are NOT in problem list
    cat_map = await _get_active_problem_categories(days=7, now=now)
    review_ids = [it.item_id for it in cat_map["review"] if it.item_type == "topup"]
    assert 801 in review_ids
    assert 802 in review_ids
    assert 805 not in review_ids
    assert 806 not in review_ids
    assert not any(it.item_id in (805, 806) for items in cat_map.values() for it in items)

    # Archive pagination: 803 (uncredited for user_a) remains visible in archive
    arch_items, arch_count, _ = await get_problem_payments_page(scope=ProblemScope.ARCHIVE, page=1, per_page=10)
    arch_topup_ids = [it.item_id for it in arch_items if it.item_type == "topup"]
    assert 803 in arch_topup_ids

    # Summary count includes 803 in total_archive and 801, 802 in needs_review
    summary = await get_fast_dashboard_summary(days=7)
    assert summary.total_archive >= 1
    assert summary.needs_review >= 2

    # Search: classifies each record consistently
    s_801 = await find_problem_payment_by_query("801")
    assert s_801.category == ProblemCategory.DELIVERY_UNCERTAIN
    s_802 = await find_problem_payment_by_query("802")
    assert s_802.category == ProblemCategory.DELIVERY_UNCERTAIN
    s_803 = await find_problem_payment_by_query("803")
    assert s_803.category == ProblemCategory.DELIVERY_UNCERTAIN
    s_805 = await find_problem_payment_by_query("805")
    assert s_805.category == ProblemCategory.PROCESSED
    s_806 = await find_problem_payment_by_query("806")
    assert s_806.category == ProblemCategory.PROCESSED

