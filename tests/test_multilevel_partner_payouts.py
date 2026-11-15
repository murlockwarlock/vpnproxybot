import pytest
import pytest_asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.models import (
    Base,
    Partner,
    PartnerEarning,
    PartnerLink,
    PartnerPlatform,
    Payment,
    PaymentMethod,
    PaymentStatus,
    ReferralConfig,
    User,
    WebPartnerEarning,
)
from bot.services.payment_service import credit_partner, credit_referral


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


@pytest_asyncio.fixture
async def setup_multilevel_chain(session):
    # Setup referral config (10% standard user referral)
    config = ReferralConfig(id=1, is_enabled=True, commission_percent=10.0)
    session.add(config)

    # Setup partner with 3 tiers: 40% / 20% / 10%
    partner = Partner(
        id=10,
        name="SuperBlogger",
        telegram_id=999000,
        commission_percent=40.0,
        commission_l2_percent=20.0,
        commission_l3_percent=10.0,
        is_active=True,
    )
    session.add(partner)
    await session.commit()

    # Users in chain:
    # Level 1: direct via partner link
    u1 = User(
        id=101,
        telegram_id=100101,
        username="user1",
        full_name="User Level 1",
        partner_id=partner.id,
        referral_root_partner_id=partner.id,
        referral_depth=1,
    )
    # Level 2: invited by user1
    u2 = User(
        id=102,
        telegram_id=100102,
        username="user2",
        full_name="User Level 2",
        referred_by=100101,
        referral_root_partner_id=partner.id,
        referral_depth=2,
    )
    # Level 3: invited by user2
    u3 = User(
        id=103,
        telegram_id=100103,
        username="user3",
        full_name="User Level 3",
        referred_by=100102,
        referral_root_partner_id=partner.id,
        referral_depth=3,
    )
    # Level 4: invited by user3
    u4 = User(
        id=104,
        telegram_id=100104,
        username="user4",
        full_name="User Level 4",
        referred_by=100103,
        referral_root_partner_id=partner.id,
        referral_depth=4,
    )
    session.add_all([u1, u2, u3, u4])
    await session.commit()

    return partner, [u1, u2, u3, u4]


@pytest.mark.asyncio
async def test_multilevel_partner_and_referral_payouts(session, setup_multilevel_chain):
    partner, (u1, u2, u3, u4) = setup_multilevel_chain
    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()

    # 1. Level 1 payment of 1000 RUB
    # Partner gets 40% = 400 RUB
    await credit_partner(session, u1.id, None, 1000.0, bot=mock_bot)
    await credit_referral(session, u1.id, None, 1000.0, bot=mock_bot)
    await session.commit()

    await session.refresh(partner)
    await session.refresh(u1)
    assert partner.partner_balance == 400.0
    assert (u1.balance_rub or 0.0) == 0.0  # u1 had no inviter

    # 2. Level 2 payment of 1000 RUB
    # Partner gets 20% = 200 RUB
    # User 1 (inviter) gets standard referral 10% = 100 RUB to bot balance
    await credit_partner(session, u2.id, None, 1000.0, bot=mock_bot)
    await credit_referral(session, u2.id, None, 1000.0, bot=mock_bot)
    await session.commit()

    await session.refresh(partner)
    await session.refresh(u1)
    assert partner.partner_balance == 400.0 + 200.0
    assert u1.balance_rub == 100.0

    # 3. Level 3 payment of 1000 RUB
    # Partner gets 10% = 100 RUB
    # User 2 gets 10% = 100 RUB
    await credit_partner(session, u3.id, None, 1000.0, bot=mock_bot)
    await credit_referral(session, u3.id, None, 1000.0, bot=mock_bot)
    await session.commit()

    await session.refresh(partner)
    await session.refresh(u2)
    assert partner.partner_balance == 600.0 + 100.0
    assert u2.balance_rub == 100.0

    # 4. Level 4 payment of 1000 RUB
    # Partner gets 10% (3+ level) = 100 RUB
    # User 3 gets 10% = 100 RUB
    await credit_partner(session, u4.id, None, 1000.0, bot=mock_bot)
    await credit_referral(session, u4.id, None, 1000.0, bot=mock_bot)
    await session.commit()

    await session.refresh(partner)
    await session.refresh(u3)
    assert partner.partner_balance == 700.0 + 100.0
    assert u3.balance_rub == 100.0

    # Check partner earnings table
    earnings = (await session.execute(
        select(PartnerEarning).where(PartnerEarning.partner_id == partner.id).order_by(PartnerEarning.id)
    )).scalars().all()

    assert len(earnings) == 4
    assert [e.depth for e in earnings] == [1, 2, 3, 4]
    assert [e.commission_percent for e in earnings] == [40.0, 20.0, 10.0, 10.0]
    assert [e.amount for e in earnings] == [400.0, 200.0, 100.0, 100.0]


@pytest.mark.asyncio
async def test_multilevel_partner_skips_when_percentage_is_zero(session, setup_multilevel_chain):
    partner, (u1, u2, u3, u4) = setup_multilevel_chain
    partner.commission_percent = 25.0
    partner.commission_l2_percent = 0.0  # 0% for level 2
    partner.commission_l3_percent = 0.0  # 0% for level 3+
    await session.commit()

    await credit_partner(session, u2.id, None, 1000.0)
    await credit_partner(session, u3.id, None, 1000.0)
    await session.commit()

    await session.refresh(partner)
    assert partner.partner_balance == 0.0
