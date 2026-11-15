from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramNetworkError
from aiogram.types import CallbackQuery, Message, User as TgUser

from bot.middlewares.error_feedback import ErrorFeedbackMiddleware, _is_network_transport_error
from bot.models import (
    BalanceTransaction,
    BalanceTransactionKind,
    Base,
    BotSettings,
    Payment,
    PaymentMethod,
    PaymentStatus,
    Subscription,
    SubStatus,
    User,
)
from bot.services.payment_service import credit_cashback
from bot.services.scheduler import _expired_text, _paid_warning_text
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


@pytest.fixture
def engine():
    return create_async_engine("sqlite+aiosqlite:///:memory:")


@pytest_asyncio.fixture
async def session(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as s:
        yield s
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


# ── Test 1: Network Error Filtering in Middleware ─────────────────────────────

@pytest.mark.asyncio
async def test_error_feedback_middleware_filters_network_transport_errors(monkeypatch):
    middleware = ErrorFeedbackMiddleware()
    mock_notify = AsyncMock()
    monkeypatch.setattr("bot.middlewares.error_feedback.notify_admins_issue", mock_notify)

    # Fake handler raising TelegramNetworkError
    async def failing_handler(event, data):
        raise TelegramNetworkError(method="answerCallbackQuery", message="ClientOSError: [Errno 104] Connection reset by peer")

    mock_callback = MagicMock(spec=CallbackQuery)
    mock_callback.answer = AsyncMock()
    mock_callback.from_user = MagicMock(id=123456789)
    mock_callback.callback_query = mock_callback

    result = await middleware(failing_handler, mock_callback, {"bot": MagicMock()})
    assert result is None
    # notify_admins_issue should NOT have been called for network reset
    mock_notify.assert_not_called()


@pytest.mark.asyncio
async def test_error_feedback_middleware_alerts_on_real_exceptions(monkeypatch):
    middleware = ErrorFeedbackMiddleware()
    mock_notify = AsyncMock()
    monkeypatch.setattr("bot.middlewares.error_feedback.notify_admins_issue", mock_notify)

    # Fake handler raising real logic/database error
    async def failing_handler(event, data):
        raise ValueError("Critical logic bug")

    mock_callback = MagicMock(spec=CallbackQuery)
    mock_callback.answer = AsyncMock()
    mock_callback.from_user = MagicMock(id=123456789)
    mock_callback.callback_query = mock_callback
    mock_callback.message = MagicMock()
    mock_callback.message.answer = AsyncMock()

    result = await middleware(failing_handler, mock_callback, {"bot": MagicMock()})
    assert result is None
    # notify_admins_issue SHOULD be called for real ValueError
    mock_notify.assert_called_once()


# ── Test 2: Cashback Crediting ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_credit_cashback_applies_percentage_to_user_balance(session):
    # Enable cashback: 15%
    session.add(BotSettings(key="cashback_enabled", value="1"))
    session.add(BotSettings(key="cashback_percent", value="15"))

    user = User(
        id=501,
        telegram_id=500001,
        username="cashback_user",
        full_name="Cashback User",
        balance_rub=0.0,
    )
    session.add(user)
    await session.commit()

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()

    # User pays 1000 RUB -> 15% cashback = 150 RUB
    await credit_cashback(session, user.id, None, 1000.0, bot=mock_bot)
    await session.commit()

    await session.refresh(user)
    assert user.balance_rub == 150.0

    # Verify transaction record
    txs = (await session.execute(
        select(BalanceTransaction).where(BalanceTransaction.user_id == user.id)
    )).scalars().all()
    assert len(txs) == 1
    assert txs[0].kind == BalanceTransactionKind.CASHBACK
    assert txs[0].amount_rub == 150.0

    mock_bot.send_message.assert_called_once()
    sent_text = mock_bot.send_message.call_args.args[1]
    assert "+150.00 ₽" in sent_text or "+150.0 ₽" in sent_text or "150" in sent_text


@pytest.mark.asyncio
async def test_credit_cashback_skips_when_disabled_or_balance_payment(session):
    # Cashback enabled 10%
    session.add(BotSettings(key="cashback_enabled", value="1"))
    session.add(BotSettings(key="cashback_percent", value="10"))

    user = User(id=502, telegram_id=500002, balance_rub=50.0)
    session.add(user)

    # Payment made with BALANCE
    payment = Payment(
        id=701,
        user_id=user.id,
        amount=500,
        currency="RUB",
        method=PaymentMethod.BALANCE,
        status=PaymentStatus.COMPLETED,
    )
    session.add(payment)
    await session.commit()

    await credit_cashback(session, user.id, payment.id, 500.0)
    await session.commit()

    await session.refresh(user)
    assert user.balance_rub == 50.0  # Unchanged, no cashback on balance payment


@pytest.mark.asyncio
async def test_credit_cashback_uses_default_percent_when_key_missing(session):
    # Only cashback_enabled = 1, cashback_percent is missing
    session.add(BotSettings(key="cashback_enabled", value="1"))

    user = User(id=503, telegram_id=500003, balance_rub=0.0)
    session.add(user)
    await session.commit()

    # User pays 200 RUB -> default 10% = 20 RUB
    await credit_cashback(session, user.id, None, 200.0)
    await session.commit()

    await session.refresh(user)
    assert user.balance_rub == 20.0


# ── Test 3: Retention Warning Texts with Balance ──────────────────────────────

def test_warning_and_expired_texts_include_balance_when_positive():
    sub = MagicMock(spec=Subscription)
    sub.expires_at = datetime(2026, 10, 1, 12, 0)
    sub.billing_mode = "tariff"
    sub.tariff_days = 30

    # Warning text with balance
    warn_text = _paid_warning_text(sub, 2, user_balance=250.0)
    assert "250.00 ₽" in warn_text or "250" in warn_text
    assert "У вас на балансе" in warn_text

    # Warning text without balance
    warn_no_bal = _paid_warning_text(sub, 2, user_balance=0.0)
    assert "У вас на балансе" not in warn_no_bal

    # Expired text with balance
    exp_text = _expired_text(sub, user_balance=180.50)
    assert "180.50 ₽" in exp_text
    assert "У вас на балансе" in exp_text
