from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.handlers import buy as buy_handler
from bot.handlers import admin as admin_handler
from bot.handlers import partner as partner_handler
from bot.handlers import start as start_handler
from bot import webhooks as webhook_handlers
from bot.models import AdTrackingLink, Base, Partner, PartnerApplication, PartnerApplicationStatus, PartnerEarning, PartnerLink, PartnerPayout, PartnerPayoutStatus, PartnerPlatform, Payment, PaymentMethod, PaymentStatus, ReferralConfig, Tariff, TariffType, User, WebPartnerEarning

pytestmark = pytest.mark.asyncio


class _State:
    def __init__(self, data: dict | None = None) -> None:
        self.data = data or {}
        self.current_state = None

    async def clear(self) -> None:
        self.data.clear()
        self.current_state = None

    async def set_state(self, state) -> None:
        self.current_state = state

    async def update_data(self, **kwargs) -> None:
        self.data.update(kwargs)

    async def get_data(self) -> dict:
        return dict(self.data)


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


@pytest_asyncio.fixture
async def db_session(db_session_factory):
    async with db_session_factory() as session:
        yield session


def _make_message(user_id: int, text: str, username: str = "alice", first_name: str = "Alice"):
    bot = SimpleNamespace(
        send_message=AsyncMock(),
        get_me=AsyncMock(return_value=SimpleNamespace(username="testbot")),
    )
    from_user = SimpleNamespace(
        id=user_id,
        username=username,
        first_name=first_name,
        full_name=f"{first_name} Example",
    )
    return SimpleNamespace(
        text=text,
        from_user=from_user,
        bot=bot,
        answer=AsyncMock(),
    )


def _make_callback(user_id: int, data: str):
    bot = SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(username="testbot")),
        send_message=AsyncMock(),
    )
    message = SimpleNamespace(
        edit_text=AsyncMock(),
        answer=AsyncMock(),
        answer_document=AsyncMock(),
        chat=SimpleNamespace(id=user_id),
    )
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id, first_name="Alice", username="alice"),
        data=data,
        bot=bot,
        message=message,
        answer=AsyncMock(),
    )


def _make_webhook_request(*, secret: str, ref: str | None = None, body: dict | None = None, bot=None):
    request = SimpleNamespace(
        headers={"X-Internal-Secret": secret},
        query={"ref": ref} if ref is not None else {},
        app={"bot": bot or SimpleNamespace(send_message=AsyncMock())},
    )

    async def _json():
        return body or {}

    request.json = _json
    return request


async def test_cmd_start_registers_user_via_partner_link(monkeypatch, db_session, db_session_factory):
    partner = Partner(
        name="Blogger",
        telegram_id=7001,
        is_active=True,
        valid_until=datetime.utcnow() + timedelta(days=10),
        audience_bonus_days=3,
        welcome_text="<b>Custom welcome</b>",
    )
    db_session.add(partner)
    await db_session.commit()
    await db_session.refresh(partner)

    link = PartnerLink(partner_id=partner.id, code="blogger_yt", platform=PartnerPlatform.YOUTUBE)
    db_session.add(link)
    await db_session.commit()
    await db_session.refresh(link)

    monkeypatch.setattr(start_handler, "async_session", db_session_factory)
    maybe_demo = AsyncMock()
    monkeypatch.setattr(start_handler, "_maybe_create_demo_key", maybe_demo)

    message = _make_message(1001, "/start p_blogger_yt")
    state = _State({"old": "state"})

    await start_handler.cmd_start(message, state)

    user = await db_session.scalar(select(User).where(User.telegram_id == 1001))
    assert user is not None
    assert user.partner_id == partner.id
    assert user.partner_link_id == link.id
    assert user.bonus_days == 3
    message.answer.assert_awaited_once()
    assert message.answer.await_args.args[0].startswith("<b>Custom welcome</b>")
    message.bot.send_message.assert_awaited_once()
    assert message.bot.send_message.await_args.args[0] == partner.telegram_id
    maybe_demo.assert_awaited_once_with(message, user.id)
    assert state.data == {}


async def test_cmd_start_ignores_expired_partner_link(monkeypatch, db_session, db_session_factory):
    partner = Partner(
        name="Expired",
        telegram_id=7002,
        is_active=True,
        valid_until=datetime.utcnow() - timedelta(days=1),
        welcome_text="<b>Partner welcome</b>",
    )
    db_session.add(partner)
    await db_session.commit()
    await db_session.refresh(partner)

    link = PartnerLink(partner_id=partner.id, code="expired_yt", platform=PartnerPlatform.YOUTUBE)
    db_session.add(link)
    await db_session.commit()

    monkeypatch.setattr(start_handler, "async_session", db_session_factory)
    maybe_demo = AsyncMock()
    monkeypatch.setattr(start_handler, "_maybe_create_demo_key", maybe_demo)

    message = _make_message(1002, "/start p_expired_yt")
    state = _State()

    await start_handler.cmd_start(message, state)

    user = await db_session.scalar(select(User).where(User.telegram_id == 1002))
    assert user is not None
    assert user.partner_id is None
    assert user.partner_link_id is None
    message.bot.send_message.assert_not_awaited()
    maybe_demo.assert_awaited_once_with(message, user.id)


async def test_cmd_start_ignores_own_partner_link(monkeypatch, db_session, db_session_factory):
    partner = Partner(
        name="SelfPartner",
        telegram_id=1003,
        is_active=True,
        valid_until=datetime.utcnow() + timedelta(days=1),
        audience_bonus_days=7,
    )
    db_session.add(partner)
    await db_session.commit()
    await db_session.refresh(partner)

    link = PartnerLink(partner_id=partner.id, code="self_yt", platform=PartnerPlatform.YOUTUBE)
    db_session.add(link)
    await db_session.commit()

    monkeypatch.setattr(start_handler, "async_session", db_session_factory)
    maybe_demo = AsyncMock()
    monkeypatch.setattr(start_handler, "_maybe_create_demo_key", maybe_demo)

    message = _make_message(1003, "/start p_self_yt", username="selfp", first_name="Self")
    state = _State()

    await start_handler.cmd_start(message, state)

    user = await db_session.scalar(select(User).where(User.telegram_id == 1003))
    assert user is not None
    assert user.partner_id is None
    assert user.partner_link_id is None
    assert user.bonus_days == 0
    message.bot.send_message.assert_not_awaited()
    maybe_demo.assert_awaited_once_with(message, user.id)


async def test_cmd_start_registers_user_via_ad_link(monkeypatch, db_session, db_session_factory):
    monkeypatch.setattr(start_handler, "async_session", db_session_factory)
    maybe_demo = AsyncMock()
    monkeypatch.setattr(start_handler, "_maybe_create_demo_key", maybe_demo)

    message = _make_message(1004, "/start ads_ch_crypto_top")
    state = _State()

    await start_handler.cmd_start(message, state)

    user = await db_session.scalar(select(User).where(User.telegram_id == 1004))
    assert user is not None
    assert user.ad_source == "ch_crypto_top"
    assert user.ad_source_kind == "channels"
    maybe_demo.assert_awaited_once_with(message, user.id)


async def test_partner_discount_text_for_active_partner(monkeypatch, db_session, db_session_factory):
    partner = Partner(name="Discount", is_active=True, audience_discount_percent=15)
    db_session.add(partner)
    await db_session.commit()
    await db_session.refresh(partner)

    user = User(telegram_id=2001, username="buyer", full_name="Buyer", partner_id=partner.id)
    tariff = Tariff(days=30, label="1 month", price_rub=1000, tariff_type=TariffType.VPN)
    db_session.add_all([user, tariff])
    await db_session.commit()

    monkeypatch.setattr(buy_handler, "async_session", db_session_factory)

    text = await buy_handler._partner_discount_text(2001, tariff)

    assert "15%" in text
    assert "850.0" in text


async def test_partner_discount_text_skips_partner_self_purchase(monkeypatch, db_session, db_session_factory):
    partner = Partner(name="Discount", telegram_id=2002, is_active=True, audience_discount_percent=15)
    db_session.add(partner)
    await db_session.commit()
    await db_session.refresh(partner)

    user = User(telegram_id=2002, username="owner", full_name="Owner", partner_id=partner.id)
    tariff = Tariff(days=30, label="1 month", price_rub=1000, tariff_type=TariffType.VPN)
    db_session.add_all([user, tariff])
    await db_session.commit()

    monkeypatch.setattr(buy_handler, "async_session", db_session_factory)

    text = await buy_handler._partner_discount_text(2002, tariff)

    assert text == ""


async def test_partner_dashboard_shows_only_active_links(monkeypatch, db_session, db_session_factory):
    partner = Partner(name="Stats", telegram_id=3001, commission_percent=20, partner_balance=120)
    db_session.add(partner)
    await db_session.commit()
    await db_session.refresh(partner)

    active_link = PartnerLink(partner_id=partner.id, code="yt_code", platform=PartnerPlatform.YOUTUBE, is_active=True)
    inactive_link = PartnerLink(partner_id=partner.id, code="tg_code", platform=PartnerPlatform.TELEGRAM, is_active=False)
    db_session.add_all([active_link, inactive_link])
    await db_session.commit()
    await db_session.refresh(active_link)
    await db_session.refresh(inactive_link)
    users = [
        User(telegram_id=30011, username="u1", full_name="U1", partner_id=partner.id, partner_link_id=active_link.id),
        User(telegram_id=30012, username="u2", full_name="U2", partner_id=partner.id, partner_link_id=active_link.id),
        User(
            telegram_id=30013,
            username="u3",
            full_name="U3",
            partner_id=partner.id,
            partner_link_id=active_link.id,
            created_at=datetime.utcnow() - timedelta(days=45),
        ),
    ]
    db_session.add_all(users)
    await db_session.commit()
    await db_session.flush()

    payment1 = Payment(user_id=users[0].id, amount=500, currency="RUB", method=PaymentMethod.BALANCE, status=PaymentStatus.COMPLETED)
    db_session.add(payment1)
    await db_session.flush()
    db_session.add(PartnerEarning(partner_id=partner.id, user_id=users[0].id, payment_id=payment1.id, amount=100))
    db_session.add(WebPartnerEarning(
        partner_id=partner.id,
        partner_link_id=active_link.id,
        web_order_id="web_order_1",
        ref_code="p_yt_code",
        buyer_contact="@buyer",
        tariff_label="Базовый (7 дней)",
        payment_amount_rub=500,
        earning_amount_rub=50,
    ))
    payment2 = Payment(
        user_id=users[2].id,
        amount=400,
        currency="RUB",
        method=PaymentMethod.BALANCE,
        status=PaymentStatus.COMPLETED,
        created_at=datetime.utcnow() - timedelta(days=45),
    )
    db_session.add(payment2)
    await db_session.flush()
    db_session.add(PartnerEarning(
        partner_id=partner.id,
        user_id=users[2].id,
        payment_id=payment2.id,
        amount=80,
        created_at=datetime.utcnow() - timedelta(days=45),
    ))
    await db_session.commit()

    monkeypatch.setattr(partner_handler, "async_session", db_session_factory)
    callback = _make_callback(3001, "partner_dashboard")

    await partner_handler.partner_dashboard(callback)

    callback.message.edit_text.assert_awaited_once()
    text = callback.message.edit_text.await_args.args[0]
    assert "tg_code" not in text
    web_link = partner_handler._build_partner_web_link(active_link.code)
    if web_link:
        assert web_link in text
    assert "Telegram: <b>3</b> переход. / <b>2</b> покуп. / <b>66.7%</b>" in text
    assert "Сайт: <b>1</b> заказ." in text
    assert "Сайт начислено: <b>50₽</b>" in text
    assert "Выводы:" not in text
    markup = callback.message.edit_text.await_args.kwargs["reply_markup"]
    assert "partner_payout_request" not in str(markup.inline_keyboard)
    assert "partner_payout_history" not in str(markup.inline_keyboard)
    assert "За 7 дней:</b> TG 2 рег. / 1 покуп. / 100" in text
    assert "WEB 1 заказ. / 50" in text
    assert "TG <b>3</b> рег. / <b>2</b> покуп. / WEB <b>1</b> заказ." in text
    callback.answer.assert_awaited_once()


async def test_admin_partner_link_save_creates_link(monkeypatch, db_session, db_session_factory):
    partner = Partner(name="Creator")
    db_session.add(partner)
    await db_session.commit()
    await db_session.refresh(partner)

    monkeypatch.setattr(partner_handler, "async_session", db_session_factory)
    monkeypatch.setattr(partner_handler, "_is_admin", lambda _uid: True)

    state = _State({"pt_link_partner": partner.id, "pt_link_platform": PartnerPlatform.YOUTUBE.value})
    message = _make_message(5001, "creator_yt")

    await partner_handler.admin_partner_link_save(message, state)

    link = await db_session.scalar(select(PartnerLink).where(PartnerLink.code == "creator_yt"))
    assert link is not None
    assert link.partner_id == partner.id
    message.answer.assert_awaited_once()
    assert "https://t.me/testbot?start=p_creator_yt" in message.answer.await_args.args[0]
    assert state.current_state is None


async def test_partner_create_target_resolves_username_and_creates_partner(monkeypatch, db_session, db_session_factory):
    user = User(telegram_id=51001, username="targetuser", full_name="Target User")
    db_session.add(user)
    await db_session.commit()

    monkeypatch.setattr(partner_handler, "async_session", db_session_factory)
    monkeypatch.setattr(partner_handler, "_is_admin", lambda _uid: True)

    state = _State()
    message = _make_message(9999, "@targetuser", username="admin", first_name="Admin")
    await partner_handler.partner_create_target(message, state)

    commission_message = _make_message(9999, "25", username="admin", first_name="Admin")
    await partner_handler.partner_create_commission(commission_message, state)

    partner = await db_session.scalar(select(Partner).where(Partner.telegram_id == 51001))
    assert partner is not None
    assert partner.name == "Target User"
    assert partner.contact_info == "@targetuser"
    assert partner.commission_percent == 25.0
    assert partner.payouts_enabled is False


async def test_partner_create_target_accepts_numeric_id_without_user(monkeypatch, db_session, db_session_factory):
    monkeypatch.setattr(partner_handler, "async_session", db_session_factory)
    monkeypatch.setattr(partner_handler, "_is_admin", lambda _uid: True)

    state = _State()
    message = _make_message(9999, "51002", username="admin", first_name="Admin")
    await partner_handler.partner_create_target(message, state)

    commission_message = _make_message(9999, "15", username="admin", first_name="Admin")
    await partner_handler.partner_create_commission(commission_message, state)

    partner = await db_session.scalar(select(Partner).where(Partner.telegram_id == 51002))
    assert partner is not None
    assert partner.name == "Partner 51002"
    assert partner.contact_info is None
    assert partner.commission_percent == 15.0
    assert partner.payouts_enabled is False


async def test_admin_user_make_partner_creates_partner_from_user_card(monkeypatch, db_session, db_session_factory):
    user = User(telegram_id=51003, username="carduser", full_name="Card User")
    db_session.add(user)
    await db_session.commit()

    monkeypatch.setattr(admin_handler, "async_session", db_session_factory)
    monkeypatch.setattr(admin_handler, "_is_admin", lambda _uid: True)
    monkeypatch.setattr(partner_handler, "_show_partner_detail", AsyncMock())

    callback = _make_callback(9999, "adm_usr_partner_51003")

    await admin_handler.admin_user_make_partner(callback)

    partner = await db_session.scalar(select(Partner).where(Partner.telegram_id == 51003))
    assert partner is not None
    assert partner.name == "Card User"
    assert partner.contact_info == "@carduser"
    assert partner.commission_percent == 20.0
    assert partner.payouts_enabled is False
    partner_handler._show_partner_detail.assert_awaited_once_with(callback.message, partner.id)
    callback.answer.assert_awaited_once()


async def test_admin_user_card_shows_ad_source(monkeypatch, db_session, db_session_factory):
    user = User(
        telegram_id=51004,
        username="aduser",
        full_name="Ad User",
        ad_source="ch_crypto_top",
        ad_source_kind="channels",
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    monkeypatch.setattr(admin_handler, "async_session", db_session_factory)
    message = _make_message(9999, "noop")

    await admin_handler._show_user_info(message, user)

    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert "📣 Реклама" in text
    assert "Channels" in text
    assert "ch_crypto_top" in text


async def test_cmd_start_registers_user_via_managed_ad_link(monkeypatch, db_session, db_session_factory):
    link = AdTrackingLink(title="Crypto Channel", code="ch_crypto_feed", source_kind="channels", is_active=True)
    db_session.add(link)
    await db_session.commit()

    monkeypatch.setattr(start_handler, "async_session", db_session_factory)
    maybe_demo = AsyncMock()
    monkeypatch.setattr(start_handler, "_maybe_create_demo_key", maybe_demo)

    message = _make_message(1005, "/start ads_ch_crypto_feed")
    state = _State()

    await start_handler.cmd_start(message, state)

    user = await db_session.scalar(select(User).where(User.telegram_id == 1005))
    assert user is not None
    assert user.ad_source == "ch_crypto_feed"
    assert user.ad_source_kind == "channels"
    maybe_demo.assert_awaited_once_with(message, user.id)


async def test_admin_ad_link_save_creates_managed_link(monkeypatch, db_session, db_session_factory):
    monkeypatch.setattr(admin_handler, "async_session", db_session_factory)
    monkeypatch.setattr(admin_handler, "_is_admin", lambda _uid: True)

    state = _State({"ad_link_kind": "channels"})
    message = _make_message(9999, "Войнарев канал 1", username="admin", first_name="Admin")

    await admin_handler.admin_ad_link_save(message, state)

    links = (await db_session.execute(select(AdTrackingLink))).scalars().all()
    assert len(links) == 1
    assert links[0].title == "Войнарев канал 1"
    assert links[0].source_kind == "channels"
    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert "https://t.me/testbot?start=ads_" in text
    assert "Channels" in text


async def test_admin_ad_link_detail_shows_stats(monkeypatch, db_session, db_session_factory):
    link = AdTrackingLink(title="Crypto Feed", code="ch_feed", source_kind="channels", is_active=True)
    db_session.add(link)
    await db_session.flush()

    buyer = User(telegram_id=71001, username="buyer", full_name="Buyer", ad_source="ch_feed", ad_source_kind="channels")
    lead = User(telegram_id=71002, username="lead", full_name="Lead", ad_source="ch_feed", ad_source_kind="channels")
    db_session.add_all([buyer, lead])
    await db_session.flush()

    payment = Payment(
        user_id=buyer.id,
        amount=5900,
        currency="RUB",
        method=PaymentMethod.YOOKASSA,
        status=PaymentStatus.COMPLETED,
    )
    db_session.add(payment)
    await db_session.commit()

    monkeypatch.setattr(admin_handler, "async_session", db_session_factory)
    monkeypatch.setattr(admin_handler, "_is_admin", lambda _uid: True)

    callback = _make_callback(9999, f"adm_ads_link_{link.id}")

    await admin_handler.admin_ad_link_detail(callback)

    callback.message.edit_text.assert_awaited_once()
    text = callback.message.edit_text.await_args.args[0]
    assert "https://t.me/testbot?start=ads_ch_feed" in text
    assert "Пришло пользователей: <b>2</b>" in text
    assert "Купили хотя бы раз: <b>1</b>" in text
    assert "Всего успешных оплат: <b>1</b>" in text


async def test_admin_partner_link_save_rejects_duplicate_code(monkeypatch, db_session, db_session_factory):
    partner = Partner(name="Creator")
    db_session.add(partner)
    await db_session.commit()
    await db_session.refresh(partner)

    db_session.add(PartnerLink(partner_id=partner.id, code="creator_yt", platform=PartnerPlatform.YOUTUBE))
    await db_session.commit()

    monkeypatch.setattr(partner_handler, "async_session", db_session_factory)
    monkeypatch.setattr(partner_handler, "_is_admin", lambda _uid: True)

    state = _State({"pt_link_partner": partner.id, "pt_link_platform": PartnerPlatform.YOUTUBE.value})
    message = _make_message(5001, "creator_yt")

    await partner_handler.admin_partner_link_save(message, state)

    count = await db_session.scalar(
        select(func.count(PartnerLink.id)).where(PartnerLink.code == "creator_yt")
    )
    assert count == 1
    message.answer.assert_awaited_once()
    assert "уже занят" in message.answer.await_args.args[0]
    assert state.current_state is None


async def test_partner_link_save_creates_link_self_service(monkeypatch, db_session, db_session_factory):
    partner = Partner(name="SelfService", telegram_id=6001)
    db_session.add(partner)
    await db_session.commit()
    await db_session.refresh(partner)

    monkeypatch.setattr(partner_handler, "async_session", db_session_factory)

    state = _State()
    callback = _make_callback(6001, "partner_lp_youtube")
    await partner_handler.partner_link_platform(callback, state)

    message = _make_message(6001, "selfservice_yt")
    await partner_handler.partner_link_save(message, state)

    link = await db_session.scalar(select(PartnerLink).where(PartnerLink.partner_id == partner.id))
    assert link is not None
    assert link.code == "selfservice_yt"
    assert link.platform == PartnerPlatform.YOUTUBE
    assert message.answer.await_count >= 1


async def test_partner_links_manage_and_toggle_own_link(monkeypatch, db_session, db_session_factory):
    partner = Partner(name="ToggleSelf", telegram_id=6002)
    db_session.add(partner)
    await db_session.commit()
    await db_session.refresh(partner)

    link = PartnerLink(partner_id=partner.id, code="toggle_self", platform=PartnerPlatform.TELEGRAM, is_active=True)
    db_session.add(link)
    await db_session.commit()
    await db_session.refresh(link)

    monkeypatch.setattr(partner_handler, "async_session", db_session_factory)

    manage_callback = _make_callback(6002, "partner_links_manage")
    await partner_handler.partner_links_manage(manage_callback)
    text = manage_callback.message.edit_text.await_args.args[0]
    assert "Управление ссылками" in text
    assert "toggle_self" in text

    toggle_callback = _make_callback(6002, f"partner_lt_{link.id}")
    await partner_handler.partner_link_toggle(toggle_callback)

    await db_session.refresh(link)
    assert link.is_active is False


async def test_partner_apply_save_creates_application_and_notifies_admins(monkeypatch, db_session, db_session_factory):
    monkeypatch.setattr(partner_handler, "async_session", db_session_factory)
    monkeypatch.setattr(partner_handler.settings, "admin_ids", [90001, 90002])

    state = _State()
    callback = _make_callback(6003, "partner_apply_start")
    await partner_handler.partner_apply_start(callback, state)

    message = _make_message(6003, "YouTube 10k, Telegram 3k, тематика VPN и приватность, контакт @alice")
    await partner_handler.partner_apply_save(message, state)

    application = await db_session.scalar(
        select(PartnerApplication).where(PartnerApplication.telegram_id == 6003)
    )
    assert application is not None
    assert application.status == PartnerApplicationStatus.PENDING
    assert "VPN" in application.notes
    assert message.bot.send_message.await_count == 2
    message.answer.assert_awaited_once()


async def test_admin_partner_application_approve_creates_partner(monkeypatch, db_session, db_session_factory):
    application = PartnerApplication(
        telegram_id=6004,
        username="candidate",
        full_name="Candidate Name",
        notes="YouTube 50k",
        contact_info="@candidate",
        status=PartnerApplicationStatus.PENDING,
    )
    db_session.add(application)
    await db_session.commit()
    await db_session.refresh(application)

    monkeypatch.setattr(partner_handler, "async_session", db_session_factory)
    monkeypatch.setattr(partner_handler, "_is_admin", lambda _uid: True)
    callback = _make_callback(9999, f"adm_ptapp_ok_{application.id}")

    await partner_handler.admin_partner_application_approve(callback)

    await db_session.refresh(application)
    partner = await db_session.scalar(select(Partner).where(Partner.telegram_id == 6004))
    assert partner is not None
    assert partner.name == "Candidate Name"
    assert partner.contact_info == "@candidate"
    assert partner.notes == "YouTube 50k"
    assert partner.commission_percent == 20.0
    assert partner.payouts_enabled is False
    assert application.status == PartnerApplicationStatus.APPROVED
    assert application.processed_by == 9999
    callback.bot.send_message.assert_awaited_once()


async def test_admin_partner_application_reject_save_updates_status(monkeypatch, db_session, db_session_factory):
    application = PartnerApplication(
        telegram_id=6005,
        username="reject_me",
        full_name="Reject Me",
        notes="No details",
        status=PartnerApplicationStatus.PENDING,
    )
    db_session.add(application)
    await db_session.commit()
    await db_session.refresh(application)

    monkeypatch.setattr(partner_handler, "async_session", db_session_factory)
    monkeypatch.setattr(partner_handler, "_is_admin", lambda _uid: True)
    state = _State({"pt_app_reject_id": application.id})
    message = _make_message(9999, "Нужно больше данных по аудитории")

    await partner_handler.admin_partner_application_reject_save(message, state)

    await db_session.refresh(application)
    assert application.status == PartnerApplicationStatus.REJECTED
    assert application.admin_comment == "Нужно больше данных по аудитории"
    assert application.processed_by == 9999
    message.bot.send_message.assert_awaited_once()
    message.answer.assert_awaited()


async def test_admin_partner_payouts_toggle_updates_flag(monkeypatch, db_session, db_session_factory):
    partner = Partner(name="TogglePayouts", telegram_id=6006, payouts_enabled=False)
    db_session.add(partner)
    await db_session.commit()
    await db_session.refresh(partner)

    monkeypatch.setattr(partner_handler, "async_session", db_session_factory)
    monkeypatch.setattr(partner_handler, "_is_admin", lambda _uid: True)
    monkeypatch.setattr(partner_handler, "_show_partner_detail", AsyncMock())

    callback = _make_callback(9999, f"adm_pt_payouts_toggle_{partner.id}")

    await partner_handler.admin_partner_payouts_toggle(callback)

    await db_session.refresh(partner)
    assert partner.payouts_enabled is True
    partner_handler._show_partner_detail.assert_awaited_once_with(callback.message, partner.id)
    callback.answer.assert_awaited_once()


async def test_partner_payout_request_start_rejects_when_disabled(monkeypatch, db_session, db_session_factory):
    partner = Partner(
        name="PayoutOff",
        telegram_id=7006,
        partner_balance=1400.0,
        min_payout=500.0,
        payouts_enabled=False,
    )
    db_session.add(partner)
    await db_session.commit()

    monkeypatch.setattr(partner_handler, "async_session", db_session_factory)
    callback = _make_callback(7006, "partner_payout_request")
    state = _State()

    await partner_handler.partner_payout_request_start(callback, state)

    callback.message.edit_text.assert_not_awaited()
    callback.answer.assert_awaited_once()
    assert "отключены" in callback.answer.await_args.args[0]
    assert callback.answer.await_args.kwargs["show_alert"] is True


async def test_partner_payout_request_start_rejects_below_minimum(monkeypatch, db_session, db_session_factory):
    partner = Partner(name="Payout", telegram_id=7007, partner_balance=400.0, min_payout=500.0, payouts_enabled=True)
    db_session.add(partner)
    await db_session.commit()

    monkeypatch.setattr(partner_handler, "async_session", db_session_factory)
    callback = _make_callback(7007, "partner_payout_request")
    state = _State()

    await partner_handler.partner_payout_request_start(callback, state)

    callback.message.edit_text.assert_not_awaited()
    callback.answer.assert_awaited_once()
    assert "Минимальная сумма" in callback.answer.await_args.args[0]
    assert callback.answer.await_args.kwargs["show_alert"] is True


async def test_partner_payout_request_save_creates_pending_request(monkeypatch, db_session, db_session_factory):
    partner = Partner(name="Payout", telegram_id=7008, partner_balance=1200.0, min_payout=500.0, payouts_enabled=True)
    db_session.add(partner)
    await db_session.commit()
    await db_session.refresh(partner)

    monkeypatch.setattr(partner_handler, "async_session", db_session_factory)
    monkeypatch.setattr(partner_handler.settings, "admin_ids", [90001, 90002])

    state = _State({"pt_payout_amount": 750.0})
    message = _make_message(7008, "СБП +79991234567")

    await partner_handler.partner_payout_request_save(message, state)

    payout = await db_session.scalar(select(PartnerPayout).where(PartnerPayout.partner_id == partner.id))
    assert payout is not None
    assert payout.amount == 750.0
    assert payout.details == "СБП +79991234567"
    assert payout.status == PartnerPayoutStatus.PENDING
    assert message.bot.send_message.await_count == 2
    message.answer.assert_awaited_once()
    assert "отправлена администратору" in message.answer.await_args.args[0]


async def test_admin_partner_payout_approve_updates_balance(monkeypatch, db_session, db_session_factory):
    partner = Partner(name="Approve", telegram_id=7010, partner_balance=1000.0, min_payout=500.0, payouts_enabled=True)
    db_session.add(partner)
    await db_session.commit()
    await db_session.refresh(partner)

    payout = PartnerPayout(partner_id=partner.id, amount=600.0, details="card 1234")
    db_session.add(payout)
    await db_session.commit()
    await db_session.refresh(payout)

    monkeypatch.setattr(partner_handler, "async_session", db_session_factory)
    monkeypatch.setattr(partner_handler, "_is_admin", lambda _uid: True)
    callback = _make_callback(9999, f"adm_ptpay_ok_{payout.id}")

    await partner_handler.admin_partner_payout_approve(callback)

    await db_session.refresh(partner)
    await db_session.refresh(payout)
    assert partner.partner_balance == 400.0
    assert payout.status == PartnerPayoutStatus.APPROVED
    assert payout.processed_by == 9999
    callback.bot.send_message.assert_awaited_once()
    callback.message.edit_text.assert_awaited()
    callback.answer.assert_awaited_once()


async def test_admin_partner_payout_reject_save_marks_request(monkeypatch, db_session, db_session_factory):
    partner = Partner(name="Reject", telegram_id=7011, partner_balance=1000.0, min_payout=500.0, payouts_enabled=True)
    db_session.add(partner)
    await db_session.commit()
    await db_session.refresh(partner)

    payout = PartnerPayout(partner_id=partner.id, amount=550.0, details="USDT")
    db_session.add(payout)
    await db_session.commit()
    await db_session.refresh(payout)

    monkeypatch.setattr(partner_handler, "async_session", db_session_factory)
    monkeypatch.setattr(partner_handler, "_is_admin", lambda _uid: True)
    state = _State({"pt_reject_payout_id": payout.id})
    message = _make_message(9999, "Нужны корректные реквизиты", username="admin", first_name="Admin")

    await partner_handler.admin_partner_payout_reject_save(message, state)

    await db_session.refresh(partner)
    await db_session.refresh(payout)
    assert partner.partner_balance == 1000.0
    assert payout.status == PartnerPayoutStatus.REJECTED
    assert payout.admin_comment == "Нужны корректные реквизиты"
    assert payout.processed_by == 9999
    message.bot.send_message.assert_awaited_once()
    message.answer.assert_awaited()


async def test_build_partners_csv_contains_period_columns(db_session):
    partner = Partner(
        name="Exporter",
        telegram_id=8001,
        commission_percent=20,
        audience_discount_percent=10,
        audience_bonus_days=3,
        partner_balance=150.0,
    )
    db_session.add(partner)
    await db_session.commit()
    await db_session.refresh(partner)

    user = User(telegram_id=8101, username="u1", full_name="U1", partner_id=partner.id)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    db_session.add(Payment(user_id=user.id, amount=500, currency="RUB", method=PaymentMethod.BALANCE, status=PaymentStatus.COMPLETED))
    db_session.add(PartnerEarning(partner_id=partner.id, user_id=user.id, amount=100))
    db_session.add(WebPartnerEarning(
        partner_id=partner.id,
        web_order_id="web_csv_1",
        ref_code="p_exporter",
        buyer_contact="@buyer",
        tariff_label="Базовый (7 дней)",
        payment_amount_rub=500,
        earning_amount_rub=80,
    ))
    await db_session.commit()

    file_bytes = await partner_handler._build_partners_csv(db_session)
    text = file_bytes.decode("utf-8-sig")

    assert "partner_id;name;is_active" in text
    assert "payouts_enabled" in text
    assert "telegram_earnings_total;web_orders_total;web_earnings_total;earnings_total" in text
    assert "telegram_earnings_7d;web_orders_7d;web_earnings_7d" in text
    assert "Exporter" in text
    assert ";1;1;100.0;1;80.0;180.0;" in text or ";1;1;100;1;80;180;" in text


async def test_build_partner_payouts_csv_contains_payout_rows(db_session):
    partner = Partner(name="PayoutCsv", telegram_id=8002, payouts_enabled=True)
    db_session.add(partner)
    await db_session.commit()
    await db_session.refresh(partner)

    db_session.add(PartnerPayout(
        partner_id=partner.id,
        amount=700.0,
        details="SBP",
        status=PartnerPayoutStatus.PENDING,
    ))
    await db_session.commit()

    file_bytes = await partner_handler._build_partner_payouts_csv(db_session)
    text = file_bytes.decode("utf-8-sig")

    assert "payout_id;partner_id;partner_name;amount;status" in text
    assert "PayoutCsv" in text
    assert "700.0;pending;SBP" in text


async def test_admin_partners_export_sends_document(monkeypatch, db_session, db_session_factory):
    partner = Partner(name="Exported", telegram_id=8003)
    db_session.add(partner)
    await db_session.commit()

    monkeypatch.setattr(partner_handler, "async_session", db_session_factory)
    monkeypatch.setattr(partner_handler, "_is_admin", lambda _uid: True)
    callback = _make_callback(9999, "adm_partners_export")

    await partner_handler.admin_partners_export(callback)

    callback.message.answer_document.assert_awaited_once()
    callback.answer.assert_awaited_once()


async def test_build_single_partner_report_csv_contains_link_breakdown(db_session):
    partner = Partner(name="SingleExport", telegram_id=8004)
    db_session.add(partner)
    await db_session.commit()
    await db_session.refresh(partner)

    link = PartnerLink(partner_id=partner.id, code="single_yt", platform=PartnerPlatform.YOUTUBE)
    db_session.add(link)
    await db_session.commit()
    await db_session.refresh(link)

    user = User(telegram_id=8201, username="u2", full_name="U2", partner_id=partner.id, partner_link_id=link.id)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    db_session.add(Payment(user_id=user.id, amount=500, currency="RUB", method=PaymentMethod.BALANCE, status=PaymentStatus.COMPLETED))
    db_session.add(PartnerEarning(partner_id=partner.id, user_id=user.id, amount=100))
    db_session.add(WebPartnerEarning(
        partner_id=partner.id,
        partner_link_id=link.id,
        web_order_id="single_web_1",
        ref_code="p_single_yt",
        buyer_contact="@buyer",
        tariff_label="Премиум (1 месяц)",
        payment_amount_rub=700,
        earning_amount_rub=140,
    ))
    await db_session.commit()

    file_bytes = await partner_handler._build_single_partner_report_csv(db_session, partner.id)
    text = file_bytes.decode("utf-8-sig")

    assert "section;partner_summary" in text
    assert "section;link_breakdown" in text
    assert "telegram_earnings_total;100.0" in text or "telegram_earnings_total;100" in text
    assert "web_orders_total;1" in text
    assert "single_yt;youtube;1;1;1;1" in text


async def test_admin_partner_analytics_renders_breakdown(monkeypatch, db_session, db_session_factory):
    partner = Partner(name="Analytic", telegram_id=8005, commission_percent=20)
    db_session.add(partner)
    await db_session.commit()
    await db_session.refresh(partner)

    link = PartnerLink(partner_id=partner.id, code="analytic_yt", platform=PartnerPlatform.YOUTUBE)
    db_session.add(link)
    await db_session.commit()
    await db_session.refresh(link)

    user = User(telegram_id=8301, username="u3", full_name="U3", partner_id=partner.id, partner_link_id=link.id)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    db_session.add(Payment(user_id=user.id, amount=500, currency="RUB", method=PaymentMethod.BALANCE, status=PaymentStatus.COMPLETED))
    db_session.add(PartnerEarning(partner_id=partner.id, user_id=user.id, amount=100))
    db_session.add(WebPartnerEarning(
        partner_id=partner.id,
        partner_link_id=link.id,
        web_order_id="analytic_web_1",
        ref_code="p_analytic_yt",
        buyer_contact="@buyer",
        tariff_label="Базовый (7 дней)",
        payment_amount_rub=500,
        earning_amount_rub=60,
    ))
    await db_session.commit()

    monkeypatch.setattr(partner_handler, "async_session", db_session_factory)
    monkeypatch.setattr(partner_handler, "_is_admin", lambda _uid: True)
    callback = _make_callback(9999, f"adm_pt_an_{partner.id}")

    await partner_handler.admin_partner_analytics(callback)

    callback.message.edit_text.assert_awaited_once()
    text = callback.message.edit_text.await_args.args[0]
    assert "Аналитика партнёра: Analytic" in text
    assert "analytic_yt" in text
    assert "Веб: <b>1</b> заказ." in text
    assert "<b>1</b> рег. / <b>1</b> покуп." in text


async def test_partner_export_csv_sends_document(monkeypatch, db_session, db_session_factory):
    partner = Partner(name="PartnerCsv", telegram_id=8006)
    db_session.add(partner)
    await db_session.commit()
    await db_session.refresh(partner)

    monkeypatch.setattr(partner_handler, "async_session", db_session_factory)
    callback = _make_callback(8006, "partner_export_csv")

    await partner_handler.partner_export_csv(callback)

    callback.message.answer_document.assert_awaited_once()
    callback.answer.assert_awaited_once()


async def test_build_partner_earnings_csv_contains_user_rows(db_session):
    partner = Partner(name="EarnCsv", telegram_id=8007)
    db_session.add(partner)
    await db_session.commit()
    await db_session.refresh(partner)

    user = User(telegram_id=8401, username="earn_user", full_name="Earn User", partner_id=partner.id)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    payment = Payment(
        user_id=user.id,
        amount=900,
        currency="RUB",
        method=PaymentMethod.BALANCE,
        status=PaymentStatus.COMPLETED,
    )
    db_session.add(payment)
    await db_session.commit()
    await db_session.refresh(payment)

    db_session.add(PartnerEarning(partner_id=partner.id, user_id=user.id, payment_id=payment.id, amount=180))
    db_session.add(WebPartnerEarning(
        partner_id=partner.id,
        web_order_id="earn_web_1",
        ref_code="p_earn",
        buyer_contact="@buyer",
        tariff_label="Базовый (7 дней)",
        payment_amount_rub=450,
        earning_amount_rub=90,
    ))
    await db_session.commit()

    file_bytes = await partner_handler._build_partner_earnings_csv(db_session, partner.id)
    text = file_bytes.decode("utf-8-sig")

    assert "source;earning_id;partner_id;partner_name;amount;created_at" in text
    assert "EarnCsv" in text
    assert "earn_user" in text
    assert "web;1;" in text
    assert ";180;" in text or ";180.0;" in text


async def test_partner_earnings_history_shows_recent_rows(monkeypatch, db_session, db_session_factory):
    partner = Partner(name="EarnHistory", telegram_id=8008)
    db_session.add(partner)
    await db_session.commit()
    await db_session.refresh(partner)

    user = User(telegram_id=8402, username="history_user", full_name="History User", partner_id=partner.id)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    payment = Payment(
        user_id=user.id,
        amount=500,
        currency="RUB",
        method=PaymentMethod.BALANCE,
        status=PaymentStatus.COMPLETED,
    )
    db_session.add(payment)
    await db_session.commit()
    await db_session.refresh(payment)

    db_session.add(PartnerEarning(partner_id=partner.id, user_id=user.id, payment_id=payment.id, amount=100))
    db_session.add(WebPartnerEarning(
        partner_id=partner.id,
        web_order_id="history_web_1",
        ref_code="p_history",
        buyer_contact="@buyer",
        tariff_label="Премиум (1 месяц)",
        payment_amount_rub=600,
        earning_amount_rub=120,
        created_at=datetime.utcnow() + timedelta(seconds=1),
    ))
    await db_session.commit()

    monkeypatch.setattr(partner_handler, "async_session", db_session_factory)
    callback = _make_callback(8008, "partner_earnings_history")

    await partner_handler.partner_earnings_history(callback)

    callback.message.edit_text.assert_awaited_once()
    text = callback.message.edit_text.await_args.args[0]
    assert "История начислений" in text
    assert "history_user" in text
    assert "100.00₽" in text
    assert "с сайта" in text
    assert "history_web_1" in text
    assert f"Платёж #{payment.id}" in text


async def test_partner_earnings_export_csv_sends_document(monkeypatch, db_session, db_session_factory):
    partner = Partner(name="EarnExport", telegram_id=8009)
    db_session.add(partner)
    await db_session.commit()
    await db_session.refresh(partner)

    monkeypatch.setattr(partner_handler, "async_session", db_session_factory)
    callback = _make_callback(8009, "partner_earnings_export_csv")

    await partner_handler.partner_earnings_export_csv(callback)

    callback.message.answer_document.assert_awaited_once()
    callback.answer.assert_awaited_once()


async def test_web_referral_resolve_and_credit_partner_link(monkeypatch, db_session, db_session_factory):
    partner = Partner(
        name="Naumov",
        telegram_id=9001,
        commission_percent=25.0,
        is_active=True,
        valid_until=datetime.utcnow() + timedelta(days=10),
    )
    db_session.add_all([
        partner,
        ReferralConfig(id=1, is_enabled=True, commission_percent=10.0),
    ])
    await db_session.commit()
    await db_session.refresh(partner)

    link = PartnerLink(partner_id=partner.id, code="naumov", platform=PartnerPlatform.TELEGRAM, is_active=True)
    db_session.add(link)
    await db_session.commit()
    await db_session.refresh(link)

    monkeypatch.setattr(webhook_handlers, "async_session", db_session_factory)
    monkeypatch.setattr(webhook_handlers.settings, "webstore_bridge_secret", "secret")

    resolve_request = _make_webhook_request(secret="secret", ref="p_naumov")
    resolve_response = await webhook_handlers.handle_internal_web_referral_resolve(resolve_request)
    resolve_data = json.loads(resolve_response.text)

    assert resolve_response.status == 200
    assert resolve_data["status"] == "ok"
    assert resolve_data["tracking_kind"] == "partner"
    assert resolve_data["telegram_id"] == str(partner.telegram_id)
    assert resolve_data["ref_code"] == "p_naumov"

    bot = SimpleNamespace(send_message=AsyncMock())
    credit_request = _make_webhook_request(
        secret="secret",
        body={
            "order_id": "web_partner_order_1",
            "ref_code": "p_naumov",
            "buyer_contact": "@buyer",
            "tariff_label": "Базовый (7 дней)",
            "amount_rub": 400,
        },
        bot=bot,
    )
    credit_response = await webhook_handlers.handle_internal_web_referral_credit(credit_request)
    credit_data = json.loads(credit_response.text)

    assert credit_response.status == 200
    assert credit_data["status"] == "credited"
    assert credit_data["tracking_kind"] == "partner"
    assert credit_data["partner_id"] == partner.id
    assert credit_data["earning_rub"] == 100.0

    await db_session.refresh(partner)
    earning = await db_session.scalar(
        select(WebPartnerEarning).where(WebPartnerEarning.web_order_id == "web_partner_order_1")
    )
    assert earning is not None
    assert earning.partner_link_id == link.id
    assert partner.partner_balance == 100.0
    bot.send_message.assert_awaited_once()


async def test_web_partner_link_works_when_referral_program_disabled(monkeypatch, db_session, db_session_factory):
    partner = Partner(
        name="WebOnly",
        telegram_id=9002,
        commission_percent=30.0,
        is_active=True,
        valid_until=datetime.utcnow() + timedelta(days=10),
    )
    db_session.add_all([
        partner,
        ReferralConfig(id=1, is_enabled=False, commission_percent=10.0),
    ])
    await db_session.commit()
    await db_session.refresh(partner)

    link = PartnerLink(partner_id=partner.id, code="webonly", platform=PartnerPlatform.TELEGRAM, is_active=True)
    db_session.add(link)
    await db_session.commit()

    monkeypatch.setattr(webhook_handlers, "async_session", db_session_factory)
    monkeypatch.setattr(webhook_handlers.settings, "webstore_bridge_secret", "secret")

    request = _make_webhook_request(
        secret="secret",
        body={
            "order_id": "web_partner_order_2",
            "ref_code": "p_webonly",
            "buyer_contact": "@buyer",
            "tariff_label": "Премиум (1 месяц)",
            "amount_rub": 500,
        },
    )
    response = await webhook_handlers.handle_internal_web_referral_credit(request)
    data = json.loads(response.text)

    assert response.status == 200
    assert data["status"] == "credited"
    assert data["tracking_kind"] == "partner"
