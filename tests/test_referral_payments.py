from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.models import Base, Platform, ReferralConfig, ReferralEarning, ReferralPaymentLog, Server, SubStatus, Subscription, User
from bot.services.payment_service import credit_referral, log_referral_payment

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def referral_setup(session: AsyncSession):
    referrer = User(telegram_id=9001, username="referrer", full_name="Referrer")
    referred = User(telegram_id=9002, username="referred", full_name="Referred", referred_by=9001)
    config = ReferralConfig(
        id=1,
        is_enabled=True,
        commission_percent=10.0,
        pay_bonus_enabled=True,
        pay_bonus_days=5,
        pay_bonus_first_only=True,
    )
    session.add_all([referrer, referred, config])
    await session.commit()
    await session.refresh(referrer)
    await session.refresh(referred)
    return referrer, referred, config


async def test_credit_referral_creates_earning_and_updates_balance(session, referral_setup):
    referrer, referred, _ = referral_setup

    await credit_referral(session, referred.id, 77, 500.0)
    await session.commit()
    await session.refresh(referrer)

    earning = await session.scalar(select(ReferralEarning).where(ReferralEarning.referrer_id == referrer.id))
    assert earning is not None
    assert earning.payment_id == 77
    assert earning.amount_rub == 50.0
    assert referrer.balance_rub == 50.0
    assert referrer.referral_balance == 0.0


async def test_log_referral_payment_adds_bonus_days_without_subscription(session, referral_setup):
    referrer, referred, _ = referral_setup

    await log_referral_payment(session, referred.id, 300.0)
    await session.commit()
    await session.refresh(referrer)

    log_count = await session.scalar(select(func.count(ReferralPaymentLog.id)))
    assert log_count == 1
    assert referrer.bonus_days == 5


async def test_log_referral_payment_extends_active_subscription_only_once(session, referral_setup):
    referrer, referred, config = referral_setup

    server = Server(name="demo", host="127.0.0.1", location="Test")
    session.add(server)
    await session.commit()
    await session.refresh(server)

    original_expiry = datetime.utcnow() + timedelta(days=10)
    sub = Subscription(
        user_id=referrer.id,
        server_id=server.id,
        tariff_months=1,
        status=SubStatus.ACTIVE,
        device_slots=1,
        client_name="client",
        platform=Platform.ANDROID,
        expires_at=original_expiry,
    )
    session.add(sub)
    await session.commit()
    await session.refresh(sub)
    await session.refresh(referrer, attribute_names=["subscriptions"])

    await log_referral_payment(session, referred.id, 300.0)
    await session.commit()
    await session.refresh(sub)
    first_expiry = sub.expires_at

    await log_referral_payment(session, referred.id, 300.0)
    await session.commit()
    await session.refresh(sub)

    assert first_expiry > original_expiry
    assert sub.expires_at == first_expiry

    log_count = await session.scalar(select(func.count(ReferralPaymentLog.id)))
    assert log_count == 2
    assert config.pay_bonus_first_only is True
