"""Tests for the Partner (blogger/affiliate) system."""

import pytest
import pytest_asyncio
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

pytestmark = pytest.mark.asyncio

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.models import (
    Base, Partner, PartnerEarning, PartnerLink, PartnerPlatform,
    User, Payment, PaymentMethod, PaymentStatus, Server, Subscription,
    Platform, SubStatus,
)


# ── Fixtures ─────────────────────────────────────────

@pytest.fixture
def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    return eng


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
async def partner(session: AsyncSession) -> Partner:
    p = Partner(
        name="TestBlogger",
        telegram_id=999000,
        commission_percent=20.0,
        audience_discount_percent=10.0,
        audience_bonus_days=3,
        min_payout=500.0,
        welcome_text="<b>Welcome from TestBlogger!</b>",
    )
    session.add(p)
    await session.commit()
    await session.refresh(p)
    return p


@pytest_asyncio.fixture
async def partner_links(session: AsyncSession, partner: Partner) -> list[PartnerLink]:
    links = []
    for platform, code in [
        (PartnerPlatform.YOUTUBE, "blogger_yt"),
        (PartnerPlatform.INSTAGRAM, "blogger_ig"),
        (PartnerPlatform.TELEGRAM, "blogger_tg"),
    ]:
        link = PartnerLink(
            partner_id=partner.id,
            code=code,
            platform=platform,
        )
        session.add(link)
        links.append(link)
    await session.commit()
    for link in links:
        await session.refresh(link)
    return links


@pytest_asyncio.fixture
async def user_via_partner(session: AsyncSession, partner: Partner, partner_links: list[PartnerLink]) -> User:
    u = User(
        telegram_id=111222333,
        username="testuser",
        full_name="Test User",
        partner_id=partner.id,
        partner_link_id=partner_links[0].id,  # YouTube
    )
    session.add(u)
    await session.commit()
    await session.refresh(u)
    return u


@pytest_asyncio.fixture
async def regular_user(session: AsyncSession) -> User:
    u = User(
        telegram_id=444555666,
        username="regularuser",
        full_name="Regular User",
    )
    session.add(u)
    await session.commit()
    await session.refresh(u)
    return u


# ── Model Tests ──────────────────────────────────────

class TestPartnerModel:
    async def test_create_partner(self, session, partner):
        result = await session.get(Partner, partner.id)
        assert result is not None
        assert result.name == "TestBlogger"
        assert result.commission_percent == 20.0
        assert result.audience_discount_percent == 10.0
        assert result.audience_bonus_days == 3
        assert result.is_active is True
        assert result.partner_balance == 0.0

    async def test_partner_defaults(self, session):
        p = Partner(name="MinPartner")
        session.add(p)
        await session.commit()
        await session.refresh(p)

        assert p.commission_percent == 0.0
        assert p.audience_discount_percent == 0.0
        assert p.audience_bonus_days == 0
        assert p.min_payout == 1000.0
        assert p.is_active is True
        assert p.valid_until is None

    async def test_partner_valid_until(self, session):
        future = datetime.utcnow() + timedelta(days=90)
        p = Partner(name="TimedPartner", valid_until=future)
        session.add(p)
        await session.commit()
        await session.refresh(p)

        assert p.valid_until is not None
        assert p.valid_until > datetime.utcnow()

    async def test_partner_expired(self, session):
        past = datetime.utcnow() - timedelta(days=1)
        p = Partner(name="ExpiredPartner", valid_until=past)
        session.add(p)
        await session.commit()
        await session.refresh(p)

        assert p.valid_until < datetime.utcnow()


class TestPartnerLinkModel:
    async def test_create_links(self, session, partner, partner_links):
        assert len(partner_links) == 3
        codes = {link.code for link in partner_links}
        assert codes == {"blogger_yt", "blogger_ig", "blogger_tg"}

    async def test_link_code_unique(self, session, partner, partner_links):
        duplicate = PartnerLink(
            partner_id=partner.id,
            code="blogger_yt",  # already exists
            platform=PartnerPlatform.TIKTOK,
        )
        session.add(duplicate)
        with pytest.raises(Exception):  # IntegrityError
            await session.commit()
        await session.rollback()

    async def test_link_platforms(self, session, partner_links):
        platforms = {link.platform for link in partner_links}
        assert PartnerPlatform.YOUTUBE in platforms
        assert PartnerPlatform.INSTAGRAM in platforms
        assert PartnerPlatform.TELEGRAM in platforms

    async def test_link_toggle(self, session, partner_links):
        link = partner_links[0]
        assert link.is_active is True
        link.is_active = False
        await session.commit()
        await session.refresh(link)
        assert link.is_active is False


class TestUserPartnerTracking:
    async def test_user_linked_to_partner(self, session, user_via_partner, partner, partner_links):
        assert user_via_partner.partner_id == partner.id
        assert user_via_partner.partner_link_id == partner_links[0].id

    async def test_regular_user_no_partner(self, session, regular_user):
        assert regular_user.partner_id is None
        assert regular_user.partner_link_id is None

    async def test_count_users_by_partner(self, session, partner, partner_links, user_via_partner):
        # Add more users via different links
        for i, link in enumerate(partner_links[1:], start=2):
            u = User(
                telegram_id=100000 + i,
                username=f"user{i}",
                full_name=f"User {i}",
                partner_id=partner.id,
                partner_link_id=link.id,
            )
            session.add(u)
        await session.commit()

        count = await session.scalar(
            select(func.count(User.id)).where(User.partner_id == partner.id)
        )
        assert count == 3  # 1 from fixture + 2 added

    async def test_count_users_by_link(self, session, partner, partner_links, user_via_partner):
        yt_count = await session.scalar(
            select(func.count(User.id)).where(User.partner_link_id == partner_links[0].id)
        )
        assert yt_count == 1

        ig_count = await session.scalar(
            select(func.count(User.id)).where(User.partner_link_id == partner_links[1].id)
        )
        assert ig_count == 0


# ── Partner Earning / Commission Tests ───────────────

class TestPartnerCommission:
    async def test_credit_partner_basic(self, session, partner, user_via_partner):
        """Test that credit_partner creates earning and updates balance."""
        from bot.services.payment_service import credit_partner

        # Patch to use our session — credit_partner expects session as first arg
        initial_balance = partner.partner_balance
        await credit_partner(session, user_via_partner.id, None, 1000.0)
        await session.commit()

        await session.refresh(partner)
        expected_earning = 1000.0 * 20.0 / 100  # 200
        assert partner.partner_balance == initial_balance + expected_earning

        # Check earning record
        earnings = (await session.execute(
            select(PartnerEarning).where(PartnerEarning.partner_id == partner.id)
        )).scalars().all()
        assert len(earnings) == 1
        assert earnings[0].amount == expected_earning
        assert earnings[0].user_id == user_via_partner.id

    async def test_credit_partner_zero_commission(self, session, user_via_partner):
        """Partner with 0% commission should get nothing."""
        from bot.services.payment_service import credit_partner

        partner = await session.get(Partner, user_via_partner.partner_id)
        partner.commission_percent = 0.0
        await session.commit()

        await credit_partner(session, user_via_partner.id, None, 1000.0)

        await session.refresh(partner)
        assert partner.partner_balance == 0.0

        count = await session.scalar(
            select(func.count(PartnerEarning.id)).where(PartnerEarning.partner_id == partner.id)
        )
        assert count == 0

    async def test_credit_partner_inactive(self, session, partner, user_via_partner):
        """Inactive partner should not receive commission."""
        from bot.services.payment_service import credit_partner

        partner.is_active = False
        await session.commit()

        await credit_partner(session, user_via_partner.id, None, 1000.0)

        await session.refresh(partner)
        assert partner.partner_balance == 0.0

    async def test_credit_partner_skips_self_referred_partner(self, session):
        """Partner should not earn commission from own payments."""
        from bot.services.payment_service import credit_partner

        partner = Partner(
            name="SelfPartner",
            telegram_id=123456789,
            commission_percent=25.0,
            is_active=True,
        )
        session.add(partner)
        await session.commit()
        await session.refresh(partner)

        user = User(
            telegram_id=123456789,
            username="selfpartner",
            full_name="Self Partner",
            partner_id=partner.id,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)

        await credit_partner(session, user.id, None, 1000.0)
        await session.commit()
        await session.refresh(partner)

        assert partner.partner_balance == 0.0
        count = await session.scalar(
            select(func.count(PartnerEarning.id)).where(PartnerEarning.partner_id == partner.id)
        )
        assert count == 0

    async def test_credit_partner_no_partner(self, session, regular_user):
        """User without partner_id should be skipped."""
        from bot.services.payment_service import credit_partner

        # Should not raise
        await credit_partner(session, regular_user.id, None, 1000.0)

    async def test_credit_partner_negative_amount(self, session, partner, user_via_partner):
        """Negative amount should be skipped."""
        from bot.services.payment_service import credit_partner

        await credit_partner(session, user_via_partner.id, None, -500.0)

        await session.refresh(partner)
        assert partner.partner_balance == 0.0

    async def test_multiple_commissions_accumulate(self, session, partner, user_via_partner):
        """Multiple payments should accumulate balance."""
        from bot.services.payment_service import credit_partner

        await credit_partner(session, user_via_partner.id, None, 500.0)
        await credit_partner(session, user_via_partner.id, None, 300.0)
        await session.commit()

        await session.refresh(partner)
        expected = round(500 * 0.2 + 300 * 0.2, 2)  # 100 + 60 = 160
        assert partner.partner_balance == expected

        count = await session.scalar(
            select(func.count(PartnerEarning.id)).where(PartnerEarning.partner_id == partner.id)
        )
        assert count == 2


# ── Partner Link Resolution Tests ────────────────────

class TestPartnerLinkResolution:
    async def test_lookup_active_link(self, session, partner_links):
        result = await session.execute(
            select(PartnerLink).where(
                PartnerLink.code == "blogger_yt",
                PartnerLink.is_active == True,  # noqa: E712
            )
        )
        link = result.scalar_one_or_none()
        assert link is not None
        assert link.platform == PartnerPlatform.YOUTUBE

    async def test_lookup_inactive_link(self, session, partner_links):
        # Deactivate the link
        partner_links[0].is_active = False
        await session.commit()

        result = await session.execute(
            select(PartnerLink).where(
                PartnerLink.code == "blogger_yt",
                PartnerLink.is_active == True,  # noqa: E712
            )
        )
        link = result.scalar_one_or_none()
        assert link is None  # Should not find inactive link

    async def test_lookup_nonexistent_code(self, session):
        result = await session.execute(
            select(PartnerLink).where(PartnerLink.code == "nonexistent_code")
        )
        assert result.scalar_one_or_none() is None


# ── Discount Calculation Tests ───────────────────────

class TestAudienceDiscount:
    async def test_discount_calculation(self, partner):
        """10% discount on 1000₽ = 100₽ discount, 900₽ final."""
        base_price = 1000.0
        discount = round(base_price * partner.audience_discount_percent / 100, 2)
        final = round(base_price - discount, 2)
        assert discount == 100.0
        assert final == 900.0

    async def test_zero_discount(self, session):
        p = Partner(name="NoDiscount", audience_discount_percent=0.0)
        session.add(p)
        await session.commit()

        discount = round(500.0 * p.audience_discount_percent / 100, 2)
        assert discount == 0.0

    async def test_full_discount(self, session):
        p = Partner(name="FullDiscount", audience_discount_percent=100.0)
        session.add(p)
        await session.commit()

        base = 500.0
        discount = round(base * p.audience_discount_percent / 100, 2)
        assert discount == 500.0
        assert base - discount == 0.0


# ── Analytics / Stats Tests ──────────────────────────

class TestPartnerAnalytics:
    async def test_conversion_rate(self, session, partner, partner_links, user_via_partner):
        """Conversion = purchases / registrations."""
        # Add 2 more users (total 3)
        for i in range(2):
            u = User(
                telegram_id=200000 + i,
                username=f"conv_user{i}",
                full_name=f"Conv User {i}",
                partner_id=partner.id,
                partner_link_id=partner_links[0].id,
            )
            session.add(u)
        await session.commit()

        # Create a server for the payment
        server = Server(
            name="TestServer",
            host="127.0.0.1",
            location="Test",
            is_active=True,
        )
        session.add(server)
        await session.commit()

        # 1 out of 3 purchased
        payment = Payment(
            user_id=user_via_partner.id,
            amount=500.0,
            currency="RUB",
            method=PaymentMethod.BALANCE,
            status=PaymentStatus.COMPLETED,
        )
        session.add(payment)
        await session.commit()

        user_count = await session.scalar(
            select(func.count(User.id)).where(User.partner_id == partner.id)
        )
        purchase_count = await session.scalar(
            select(func.count(Payment.id))
            .join(User, Payment.user_id == User.id)
            .where(User.partner_id == partner.id, Payment.status == PaymentStatus.COMPLETED)
        )

        assert user_count == 3
        assert purchase_count == 1
        conversion = purchase_count / user_count * 100
        assert abs(conversion - 33.3) < 0.5

    async def test_per_platform_breakdown(self, session, partner, partner_links):
        """Test counting users per platform link."""
        # 5 YouTube, 3 Instagram, 0 Telegram
        for i in range(5):
            session.add(User(
                telegram_id=300000 + i,
                username=f"yt_user{i}",
                full_name=f"YT User {i}",
                partner_id=partner.id,
                partner_link_id=partner_links[0].id,  # YouTube
            ))
        for i in range(3):
            session.add(User(
                telegram_id=400000 + i,
                username=f"ig_user{i}",
                full_name=f"IG User {i}",
                partner_id=partner.id,
                partner_link_id=partner_links[1].id,  # Instagram
            ))
        await session.commit()

        for link, expected in [(partner_links[0], 5), (partner_links[1], 3), (partner_links[2], 0)]:
            count = await session.scalar(
                select(func.count(User.id)).where(User.partner_link_id == link.id)
            )
            assert count == expected, f"Expected {expected} for {link.code}, got {count}"

    async def test_total_earnings(self, session, partner, user_via_partner):
        """Test sum of earnings."""
        for amount in [100.0, 50.0, 75.5]:
            session.add(PartnerEarning(
                partner_id=partner.id,
                user_id=user_via_partner.id,
                amount=amount,
            ))
        await session.commit()

        total = await session.scalar(
            select(func.coalesce(func.sum(PartnerEarning.amount), 0))
            .where(PartnerEarning.partner_id == partner.id)
        )
        assert total == 225.5


# ── Import / Router Tests ───────────────────────────

class TestPartnerImports:
    async def test_partner_module_imports(self):
        import bot.handlers.partner
        assert hasattr(bot.handlers.partner, "router")

    async def test_partner_models_import(self):
        from bot.models import Partner, PartnerLink, PartnerEarning, PartnerPlatform
        assert Partner is not None
        assert PartnerPlatform.YOUTUBE.value == "youtube"

    async def test_credit_partner_importable(self):
        from bot.services.payment_service import credit_partner
        assert callable(credit_partner)
