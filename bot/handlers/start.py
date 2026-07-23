"""Start handler - /start, welcome, registration, help."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from bot.config import settings
from bot.database import async_session
from bot.keyboards.client import back_to_menu_kb, demo_key_kb, guide_platform_kb, help_kb, main_menu_kb
from bot.models import AdTrackingLink, BotSettings, Feedback, Partner, PartnerLink, Platform, ProxyAccount, ReferralConfig, Server, Subscription, User
from bot.services.balance_service import get_user_balance
from bot.services.subscription_semantics import is_demo_subscription_row
from bot.services.webstore_bridge import claim_web_auth, claim_web_link


class FeedbackStates(StatesGroup):
    waiting_message = State()
from bot.services.device_slots import get_included_device_slots
from bot.services.guide_service import send_guide
from bot.services.legal_docs import LEGAL_DOCS, get_legal_doc_url
from bot.services.client_names import build_client_name
from bot.utils.texts import (
    GUIDE_ANDROID,
    GUIDE_ANDROID_TV,
    GUIDE_IOS,
    GUIDE_MAC,
    GUIDE_WINDOWS,
    HELP,
    WELCOME,
    WELCOME_BACK,
    WELCOME_RENEW,
)

logger = logging.getLogger(__name__)
router = Router(name="start")


async def _is_partner(session, telegram_id: int) -> bool:
    """Check if the user is a partner (has a Partner record with matching telegram_id)."""
    result = await session.execute(select(Partner).where(Partner.telegram_id == telegram_id))
    return result.scalar_one_or_none() is not None


async def _ref_btn_name(session) -> str:
    """Return the referral button label from config (or default)."""
    config = await session.get(ReferralConfig, 1)
    if config and config.btn_name:
        return config.btn_name
    return "👥 Пригласить друзей"


def _normalize_ad_source_kind(value: str) -> str:
    raw = (value or "").strip().lower()
    if raw in {"ch", "channel", "channels"}:
        return "channels"
    if raw in {"bot", "bots"}:
        return "bots"
    if raw in {"search", "srch"}:
        return "search"
    return "custom"


def _has_active_subscription(user: User) -> bool:
    return any(sub.status.value == "active" for sub in user.subscriptions)


def _has_expired_paid_subscription(user: User) -> bool:
    return any(
        sub.status.value == "expired" and not is_demo_subscription_row(sub)
        for sub in user.subscriptions
    )


def _purchase_button_text(user: User | None) -> str:
    if not user:
        return "🛒 Купить доступ"
    if _has_active_subscription(user) or _has_expired_paid_subscription(user):
        return "Оплатить подписку 💳"
    return "🛒 Купить доступ"


def _format_date(dt: datetime) -> str:
    from datetime import timezone, timedelta
    msk = timezone(timedelta(hours=3))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(msk).strftime("%d.%m.%Y %H:%M МСК")


def _get_active_subscription_expiry(user: User) -> str | None:
    active_subs = [
        s for s in user.subscriptions
        if s.status.value == "active"
    ]
    if not active_subs:
        return None
    latest = max(active_subs, key=lambda s: s.expires_at)
    return _format_date(latest.expires_at)


def _get_welcome_text(user: User, name: str) -> str:
    active_expiry = _get_active_subscription_expiry(user)
    if active_expiry:
        from bot.utils.texts import WELCOME_BACK_ACTIVE
        return WELCOME_BACK_ACTIVE.format(name=name, expires=active_expiry)
    if _purchase_button_text(user) == "Оплатить подписку 💳":
        from bot.utils.texts import WELCOME_RENEW
        return WELCOME_RENEW
    from bot.utils.texts import WELCOME_BACK
    return WELCOME_BACK.format(name=name)


def _guide_text(platform: str) -> str:
    guides = {
        "android": GUIDE_ANDROID,
        "ios": GUIDE_IOS,
        "mac": GUIDE_MAC,
        "windows": GUIDE_WINDOWS,
        "android_tv": GUIDE_ANDROID_TV,
    }
    return guides.get(platform, GUIDE_ANDROID)


def _with_balance_line(text: str, user: User | None) -> str:
    balance = get_user_balance(user)
    return f"{text}\n\n💰 Баланс: <b>{balance:.2f} ₽</b>"


async def _maybe_create_demo_key(message: Message, user_db_id: int) -> None:
    """Create a free demo VPN key for a newly registered user if the feature is enabled."""
    if settings.adapt_demo_enabled:
        await _maybe_create_adapt_demo_key(message, user_db_id)
    else:
        await _maybe_create_marzban_demo_key(message, user_db_id)


async def _maybe_create_adapt_demo_key(message: Message, user_db_id: int) -> None:
    """Create a free demo ADAPT subscription for a newly registered user."""
    from bot.models import Tariff

    async with async_session() as session:
        demo_enabled_row = await session.get(BotSettings, "demo_key_enabled")
        if not demo_enabled_row or demo_enabled_row.value != "1":
            return

        demo_days_row = await session.get(BotSettings, "demo_key_days")
        try:
            demo_days = int(demo_days_row.value) if demo_days_row else 3
        except ValueError:
            demo_days = 3

        demo_tariff = None
        if settings.adapt_demo_plan_uuid:
            demo_tariff_result = await session.execute(
                select(Tariff)
                .where(Tariff.adapt_plan_uuid == settings.adapt_demo_plan_uuid)
                .limit(1)
            )
            demo_tariff = demo_tariff_result.scalar_one_or_none()

        if not demo_tariff:
            # Look for the ADAPT demo tariff. Prefer admin-only 7-day tariff,
            # fallback to any cheapest 7-day ADAPT tariff.
            demo_tariff_result = await session.execute(
                select(Tariff)
                .where(Tariff.adapt_plan_uuid.isnot(None))
                .where(Tariff.is_admin_only == True)  # noqa: E712
                .where(Tariff.days == 7)
                .order_by(Tariff.price_rub)
                .limit(1)
            )
            demo_tariff = demo_tariff_result.scalar_one_or_none()
            if not demo_tariff:
                demo_tariff_result = await session.execute(
                    select(Tariff)
                    .where(Tariff.adapt_plan_uuid.isnot(None))
                    .where(Tariff.days == 7)
                    .order_by(Tariff.price_rub)
                    .limit(1)
                )
                demo_tariff = demo_tariff_result.scalar_one_or_none()

    if not demo_tariff:
        logger.error("ADAPT demo tariff (7 days, admin_only or cheapest) not found in DB.")
        return

    if demo_tariff.days:
        demo_days = demo_tariff.days

    async with async_session() as session:
        user = await session.get(User, user_db_id)
        if not user:
            return

        from bot.services.subscription_service import create_adapt_demo_subscription
        subscription, vpn_key = await create_adapt_demo_subscription(
            session,
            user=user,
            tariff=demo_tariff,
            platform=Platform.ANDROID,
        )

        if not subscription or not vpn_key:
            logger.error(f"Failed to issue ADAPT demo for user {user_db_id}")
            return

        await session.commit()

    await message.answer(
        f"🎁 <b>Вам выдан демо-доступ на {demo_days} дн.!</b>\n\n"
        f"Ссылка на подписку:\n<code>{vpn_key}</code>\n"
        f"<i>Нажмите на ссылку выше, чтобы скопировать её.</i>\n\n"
        f"📱 Демо-доступ работает на <b>1 устройстве</b>.\n"
        f"Полный тариф даёт доступ на <b>3 устройства</b>.\n\n"
        f"Найти ключ позже: «👤 Мой профиль» → «🔑 Мои ключи».",
        parse_mode="HTML",
        reply_markup=demo_key_kb(),
    )
    logger.info(f"ADAPT Demo key created for user {user_db_id}")


async def _maybe_create_marzban_demo_key(message: Message, user_db_id: int) -> None:
    """Create a free demo Marzban VPN key for a newly registered user."""
    from bot.services import vpn_manager

    async with async_session() as session:
        demo_enabled_row = await session.get(BotSettings, "demo_key_enabled")
        if not demo_enabled_row or demo_enabled_row.value != "1":
            return

        demo_days_row = await session.get(BotSettings, "demo_key_days")
        try:
            demo_days = int(demo_days_row.value) if demo_days_row else 3
        except ValueError:
            demo_days = 3

        srv_result = await session.execute(
            select(Server).where(Server.is_active == True).order_by(Server.id).limit(1)  # noqa: E712
        )
        server = srv_result.scalar_one_or_none()
        if not server:
            # Fallback: use any server if all marked inactive by health check
            srv_result = await session.execute(
                select(Server).where(Server.api_url.isnot(None)).order_by(Server.id).limit(1)
            )
            server = srv_result.scalar_one_or_none()
        if not server:
            return
        server_id = server.id

    client_name = build_client_name(message.from_user.id, is_demo=True)
    expires_at = datetime.utcnow() + timedelta(days=demo_days)
    try:
        vpn_key = await vpn_manager.generate_key(
            server=server,
            client_name=client_name,
            expire=int(expires_at.timestamp()),
        )
    except Exception as e:
        logger.error(f"Demo key generation failed for user {user_db_id}: {e}")
        return

    if not vpn_key or vpn_key.startswith("Error"):
        logger.warning(f"Demo key failed for user {user_db_id}: {vpn_key}")
        return

    async with async_session() as session:
        included_slots = await get_included_device_slots(session)
        subscription = Subscription(
            user_id=user_db_id,
            server_id=server_id,
            tariff_months=0,
            tariff_days=demo_days,
            vpn_key=vpn_key,
            client_name=client_name,
            platform=Platform.ANDROID,
            device_slots=included_slots,
            expires_at=expires_at,
        )
        session.add(subscription)
        await session.flush()
        session.add(ProxyAccount(
            user_id=user_db_id,
            server_id=server_id,
            subscription_id=subscription.id,
            marzban_username=client_name,
            sub_url=vpn_key,
            device_limit=1,
        ))
        server_obj = await session.get(Server, server_id)
        if server_obj:
            server_obj.current_clients += 1
        await session.commit()

    await message.answer(
        f"🎁 <b>Вам выдан демо-доступ на {demo_days} дн.!</b>\n\n"
        f"Ссылка на подписку:\n<code>{vpn_key}</code>\n"
        f"<i>Нажмите на ссылку выше, чтобы скопировать её.</i>\n\n"
        f"📱 Демо-доступ работает на <b>1 устройстве</b>.\n"
        f"Полный тариф даёт доступ на <b>3 устройства</b>.\n\n"
        f"Найти ключ позже: «👤 Мой профиль» → «🔑 Мои ключи».",
        parse_mode="HTML",
        reply_markup=demo_key_kb(),
    )
    logger.info(f"Demo key created for user {user_db_id}")


async def _handle_web_link(message: Message, code: str) -> None:
    async with async_session() as session:
        result = await session.execute(
            select(User)
            .options(selectinload(User.subscriptions).selectinload(Subscription.server))
            .where(User.telegram_id == message.from_user.id)
        )
        user = result.scalar_one_or_none()
        if not user:
            return
        from bot.models import MTProtoAccount
        mtproto_result = await session.execute(
            select(MTProtoAccount)
            .where(MTProtoAccount.user_id == user.id)
            .where(MTProtoAccount.is_active == True)  # noqa: E712
        )
        mtproto_accounts = mtproto_result.scalars().all()
        success = await claim_web_link(code, user, user.subscriptions, mtproto_accounts)

    if success:
        await message.answer(
            "✅ Telegram привязан к вашему профилю на сайте.\n"
            "Теперь покупки и ключи будут видны и здесь, и в веб-профиле."
        )
    else:
        await message.answer(
            "Не удалось привязать профиль с сайта. Откройте веб-профиль и попробуйте снова."
        )


async def _handle_web_auth(message: Message, code: str) -> None:
    async with async_session() as session:
        result = await session.execute(
            select(User)
            .options(selectinload(User.subscriptions).selectinload(Subscription.server))
            .where(User.telegram_id == message.from_user.id)
        )
        user = result.scalar_one_or_none()
        if not user:
            return
        from bot.models import MTProtoAccount
        mtproto_result = await session.execute(
            select(MTProtoAccount)
            .where(MTProtoAccount.user_id == user.id)
            .where(MTProtoAccount.is_active == True)  # noqa: E712
        )
        mtproto_accounts = mtproto_result.scalars().all()
        success = await claim_web_auth(code, user, user.subscriptions, mtproto_accounts)

    if success:
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        site_url = settings.webstore_api_base_url.rstrip("/") if settings.webstore_public_enabled else None
        kb = None
        if site_url:
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🌐 Открыть профиль на сайте", url=f"{site_url}/profile?login={code}")
            ]])
        await message.answer(
            "✅ Вход на сайт подтверждён.\n"
            "Вернитесь в браузер или откройте профиль кнопкой ниже.",
            reply_markup=kb,
        )
    else:
        await message.answer(
            "Не удалось подтвердить вход на сайт. Вернитесь на сайт и попробуйте ещё раз."
        )


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    """Handle /start - register user or welcome back from any FSM state."""
    await state.clear()
    args = message.text.split(maxsplit=1)
    referrer_tg_id: int | None = None
    partner_link_obj: PartnerLink | None = None
    partner_obj: Partner | None = None
    ad_source: str | None = None
    ad_source_kind: str | None = None
    web_link_code: str | None = None
    web_auth_code: str | None = None

    if len(args) > 1:
        param = args[1].strip()
        if param.startswith("webauth_"):
            web_auth_code = param[8:]
        elif param.startswith("web_"):
            web_link_code = param[4:]
        elif param.startswith("ads_"):
            raw_source = param[4:].strip()
            if raw_source:
                async with async_session() as _s:
                    tracked_link = await _s.scalar(
                        select(AdTrackingLink).where(AdTrackingLink.code == raw_source[:64])
                    )
                if tracked_link:
                    if tracked_link.is_active:
                        ad_source = tracked_link.code
                        ad_source_kind = _normalize_ad_source_kind(tracked_link.source_kind)
                    else:
                        logger.info("Ignored inactive ad link %s", raw_source[:64])
                else:
                    ad_source = raw_source[:64]
                    ad_source_kind = _normalize_ad_source_kind(raw_source.split("_", 1)[0])
        elif param.startswith("p_"):
            # Partner deep link: p_{code}
            code = param[2:]
            async with async_session() as _s:
                from datetime import datetime as _dt
                r = await _s.execute(
                    select(PartnerLink).where(PartnerLink.code == code, PartnerLink.is_active == True)  # noqa: E712
                )
                _pl = r.scalar_one_or_none()
                if _pl:
                    _p = await _s.get(Partner, _pl.partner_id)
                    if _p and _p.is_active and _p.telegram_id != message.from_user.id:
                        if _p.valid_until is None or _p.valid_until > _dt.utcnow():
                            partner_link_obj = _pl
                            partner_obj = _p
        elif param.startswith("ref_"):
            raw = param[4:]
            try:
                ref_id_parsed = int(raw)
                if ref_id_parsed != message.from_user.id:
                    referrer_tg_id = ref_id_parsed
            except ValueError:
                async with async_session() as _s:
                    r = await _s.execute(select(User).where(User.referral_code == raw.upper()))
                    _ref = r.scalar_one_or_none()
                    if _ref and _ref.telegram_id != message.from_user.id:
                        referrer_tg_id = _ref.telegram_id

    new_user_id: int | None = None
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == message.from_user.id)
        )
        user = result.scalar_one_or_none()
        btn_name = await _ref_btn_name(session)

        if user is None:
            # Verify referrer exists and resolve referral chain
            referred_by: int | None = None
            referrer_obj: User | None = None
            if referrer_tg_id and not partner_obj:
                ref_result = await session.execute(
                    select(User).where(User.telegram_id == referrer_tg_id)
                )
                referrer_obj = ref_result.scalar_one_or_none()
                if referrer_obj:
                    referred_by = referrer_tg_id

            # Multi-level referral chain: propagate root partner from referrer
            root_partner_id: int | None = None
            ref_depth: int = 0
            if partner_obj:
                # Direct partner link click — depth 1
                root_partner_id = partner_obj.id
                ref_depth = 1
            elif referrer_obj and referrer_obj.referral_root_partner_id is not None:
                # Came via another user who belongs to a partner chain
                root_partner_id = referrer_obj.referral_root_partner_id
                ref_depth = (referrer_obj.referral_depth or 1) + 1

            user = User(
                telegram_id=message.from_user.id,
                username=message.from_user.username or "",
                full_name=message.from_user.full_name or "",
                referred_by=referred_by,
                partner_id=partner_obj.id if partner_obj else None,
                partner_link_id=partner_link_obj.id if partner_link_obj else None,
                ad_source=ad_source,
                ad_source_kind=ad_source_kind,
                referral_root_partner_id=root_partner_id,
                referral_depth=ref_depth,
            )
            session.add(user)
            await session.commit()
            new_user_id = user.id

            # Apply partner audience bonus days
            if partner_obj and partner_obj.audience_bonus_days > 0:
                user.bonus_days = (user.bonus_days or 0) + partner_obj.audience_bonus_days
                await session.commit()

            # Notify referrer
            if referred_by:
                from bot.utils.texts import fmt_user
                try:
                    user_info = fmt_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
                    await message.bot.send_message(
                        referred_by,
                        f"🤝 По вашей ссылке зарегистрировался новый пользователь: {user_info}",
                        parse_mode="HTML"
                    )
                except Exception as e:
                    logger.error(f"Failed to notify referrer {referred_by}: {e}")

            # Notify partner
            if partner_obj and partner_obj.telegram_id:
                from bot.utils.texts import fmt_user
                try:
                    user_info = fmt_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
                    platform_name = partner_link_obj.platform.value if partner_link_obj else "direct"
                    await message.bot.send_message(
                        partner_obj.telegram_id,
                        f"🤝 Новый пользователь по вашей партнёрской ссылке ({platform_name}): {user_info}",
                        parse_mode="HTML",
                    )
                except Exception as e:
                    logger.error(f"Failed to notify partner {partner_obj.name}: {e}")

            logger.info(
                f"New user registered: {message.from_user.id}"
                + (f" via ref {referred_by}" if referred_by else "")
                + (f" via partner {partner_obj.name}" if partner_obj else "")
                + (f" via ads {ad_source}" if ad_source else "")
            )

            # Welcome text: use partner custom text or default
            welcome = partner_obj.welcome_text if (partner_obj and partner_obj.welcome_text) else WELCOME
            await message.answer(
                _with_balance_line(welcome, user),
                reply_markup=main_menu_kb(btn_name, _purchase_button_text(None),
                                          is_admin=settings.is_admin(message.from_user.id)),
                parse_mode="HTML",
            )
        else:
            await session.refresh(user, attribute_names=["subscriptions"])
            purchase_button_text = _purchase_button_text(user)
            is_partner = await _is_partner(session, message.from_user.id)
            
            # If user was reset (no active or expired paid subs, and no demo subs either since they are deleted), treat as new
            if not user.subscriptions:
                new_user_id = user.id
                welcome_text = partner_obj.welcome_text if (partner_obj and partner_obj.welcome_text) else WELCOME
            else:
                welcome_text = _get_welcome_text(user, message.from_user.first_name)
                
            await message.answer(
                _with_balance_line(welcome_text, user),
                reply_markup=main_menu_kb(btn_name, purchase_button_text, is_partner=is_partner,
                                          is_admin=settings.is_admin(message.from_user.id)),
                parse_mode="HTML",
            )

    if new_user_id is not None:
        await _maybe_create_demo_key(message, new_user_id)
    if web_link_code:
        await _handle_web_link(message, web_link_code)
    if web_auth_code:
        await _handle_web_auth(message, web_auth_code)


@router.callback_query(F.data == "back_main")
async def back_to_main(callback: CallbackQuery, state: FSMContext) -> None:
    """Return to main menu - also clears any leftover FSM state."""
    await state.clear()
    async with async_session() as session:
        btn_name = await _ref_btn_name(session)
        result = await session.execute(
            select(User).where(User.telegram_id == callback.from_user.id)
        )
        user = result.scalar_one_or_none()
        if user is not None:
            await session.refresh(user, attribute_names=["subscriptions"])
        purchase_button_text = _purchase_button_text(user)
        is_partner = await _is_partner(session, callback.from_user.id)
        welcome_text = _get_welcome_text(user, callback.from_user.first_name)
    try:
        await callback.message.edit_text(
            _with_balance_line(welcome_text, user),
            reply_markup=main_menu_kb(btn_name, purchase_button_text, is_partner=is_partner,
                                      is_admin=settings.is_admin(callback.from_user.id)),
            parse_mode="HTML",
        )
    except Exception:
        await callback.message.answer(
            _with_balance_line(welcome_text, user),
            reply_markup=main_menu_kb(btn_name, purchase_button_text, is_partner=is_partner,
                                      is_admin=settings.is_admin(callback.from_user.id)),
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data == "help")
async def show_help(callback: CallbackQuery) -> None:
    """Show help message."""
    await callback.message.edit_text(
        HELP,
        reply_markup=help_kb(),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await callback.answer()


@router.callback_query(F.data == "guide_menu")
async def show_guides_menu(callback: CallbackQuery) -> None:
    await callback.message.answer(
        "📲 <b>Выберите ваше устройство</b>\n\n"
        "После оплаты вы получите вашу ссылку.\n"
        "Если <b>Happ</b> уже установлен, обычно скачивать его заново не нужно.\n"
        "Я отправлю инструкцию именно для вашего устройства.",
        parse_mode="HTML",
        reply_markup=guide_platform_kb("guide_select"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("guide_select_"))
async def send_platform_guide(callback: CallbackQuery) -> None:
    platform = callback.data[len("guide_select_"):]
    await send_guide(
        callback.bot,
        callback.message.chat.id,
        platform,
        _guide_text(platform),
        reply_markup=back_to_menu_kb(),
    )
    try:
        await callback.message.edit_text(
            "✅ Инструкция отправлена ниже. Если нужно, можете выбрать другое устройство:",
            reply_markup=guide_platform_kb("guide_select"),
        )
    except Exception:
        pass
    await callback.answer()


@router.message(Command("help"))
async def show_help_command(message: Message, state: FSMContext) -> None:
    """Show help message via /help command from any FSM state."""
    await state.clear()
    await message.answer(
        HELP,
        reply_markup=help_kb(),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def _send_legal_doc(message: Message, doc_code: str) -> None:
    async with async_session() as session:
        url = await get_legal_doc_url(session, doc_code)
    _, label = LEGAL_DOCS[doc_code]
    if not url:
        await message.answer(f"📄 {label} пока не настроено.")
        return
    await message.answer(f"📄 <b>{label}:</b> {url}", parse_mode="HTML", disable_web_page_preview=True)


@router.message(Command("policy"))
async def show_policy_command(message: Message) -> None:
    await _send_legal_doc(message, "policy")


@router.message(Command("agree"))
async def show_agree_command(message: Message) -> None:
    await _send_legal_doc(message, "agree")


@router.message(Command("oferta"))
async def show_oferta_command(message: Message) -> None:
    await _send_legal_doc(message, "oferta")


# ── Feedback ──────────────────────────────────────────

_FEEDBACK_CANCEL_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="◀️ Отмена", callback_data="help")],
])


@router.callback_query(F.data == "feedback_start")
async def feedback_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(FeedbackStates.waiting_message)
    await callback.message.edit_text(
        "📝 <b>Обратная связь</b>\n\n"
        "Напишите ваше сообщение — текст, фото или видео.\n\n"
        "💡 <i>С медиа — до 1024 символов, без медиа — до 4096 символов.</i>",
        reply_markup=_FEEDBACK_CANCEL_KB,
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(FeedbackStates.waiting_message)
async def feedback_receive(message: Message, state: FSMContext) -> None:
    text = message.text or message.caption or ""
    media_file_id = None
    media_type = None

    if message.photo:
        media_file_id = message.photo[-1].file_id
        media_type = "photo"
    elif message.video:
        media_file_id = message.video.file_id
        media_type = "video"

    if not text and not media_file_id:
        await message.answer("❌ Отправьте текст, фото или видео.", reply_markup=_FEEDBACK_CANCEL_KB)
        return

    # Save to DB
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == message.from_user.id)
        )
        user = result.scalar_one_or_none()

        fb = Feedback(
            user_id=user.id if user else 0,
            telegram_id=message.from_user.id,
            text=text or None,
            media_file_id=media_file_id,
            media_type=media_type,
        )
        session.add(fb)
        await session.commit()
        fb_id = fb.id

    await state.clear()
    await message.answer(
        "✅ Спасибо! Ваше сообщение отправлено.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Главное меню", callback_data="back_main")],
        ]),
    )

    # Forward to admins
    user_name = message.from_user.full_name or ""
    user_username = f"@{message.from_user.username}" if message.from_user.username else ""
    header = (
        f"📝 <b>Фидбэк #{fb_id}</b>\n"
        f"От: {user_name} {user_username} (ID: <code>{message.from_user.id}</code>)\n"
        f"{'─' * 20}"
    )

    # Collect all admin IDs from env settings AND database
    admin_ids_to_notify = set(settings.admin_ids)
    try:
        async with async_session() as db_sess:
            db_adm_res = await db_sess.execute(select(User.telegram_id).where(User.is_admin == True))
            for db_adm_id in db_adm_res.scalars().all():
                if db_adm_id:
                    admin_ids_to_notify.add(int(db_adm_id))
    except Exception as e:
        logger.warning("Error fetching DB admins for feedback: %s", e)

    for admin_id in admin_ids_to_notify:
        try:
            if media_file_id and media_type:
                caption = f"{header}\n\n{text}" if text else header
                send_fn = message.bot.send_photo if media_type == "photo" else message.bot.send_video
                await send_fn(admin_id, media_file_id, caption=caption, parse_mode="HTML")
            else:
                await message.bot.send_message(
                    admin_id,
                    f"{header}\n\n{text}",
                    parse_mode="HTML",
                )
        except Exception as e:
            logger.warning("Failed to send feedback to admin %s: %s", admin_id, e)
