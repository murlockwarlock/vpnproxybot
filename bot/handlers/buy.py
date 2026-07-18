"""Purchase flow handler - product type → tariff (from DB) → platform → payment."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery
from sqlalchemy import select

from bot.config import settings
from bot.database import async_session
from bot.keyboards.client import (
    payment_kb,
    platform_kb,
    product_type_kb,
    tariffs_kb,
)
from bot.models import BotSettings, Partner, Server, Tariff, TariffType, User
from bot.services.balance_service import get_user_balance
from bot.services.legal_docs import build_legal_notice, get_all_legal_doc_urls
from bot.services.tariff_rules import (
    INTRO_BASIC_ALREADY_USED_TEXT,
    build_darimiru_tariff_text,
    build_tariff_purchase_note,
    can_purchase_intro_basic_tariff,
    is_intro_basic_tariff,
    supports_extra_devices,
)
from bot.utils.texts import (
    NO_TARIFFS,
    SELECT_PAYMENT,
    SELECT_PAYMENT_DARIMIRU,
    SELECT_PLATFORM,
    SELECT_TARIFF_ANEWKA,
    SELECT_PRODUCT_TYPE,
    SELECT_TARIFF,
)

logger = logging.getLogger(__name__)
router = Router(name="buy")


def _is_darimiru_tariff_catalog() -> bool:
    hosts = {
        urlparse(settings.subscription_base_url).hostname,
        urlparse(settings.webstore_api_base_url).hostname,
    }
    return "darimiru.ru" in hosts


def _select_payment_text() -> str:
    return SELECT_PAYMENT_DARIMIRU if _is_darimiru_tariff_catalog() else SELECT_PAYMENT


def _is_anewka_tariff_catalog() -> bool:
    hosts = {
        urlparse(settings.subscription_base_url).hostname,
        urlparse(settings.webstore_api_base_url).hostname,
    }
    return "loonapie.xyz" in hosts


async def _partner_discount_text(telegram_id: int, tariff: Tariff) -> str:
    """Return discount info line if user came via partner with audience discount."""
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        user = result.scalar_one_or_none()
        if not user or not user.partner_id:
            return ""
        partner = await session.get(Partner, user.partner_id)
        if (
            not partner
            or not partner.is_active
            or partner.audience_discount_percent <= 0
            or partner.telegram_id == telegram_id
        ):
            return ""
        discount = round(float(tariff.price_rub) * partner.audience_discount_percent / 100, 2)
        final = round(float(tariff.price_rub) - discount, 2)
        return f"\n🎁 <b>Партнёрская скидка {int(partner.audience_discount_percent)}%: {final}₽</b>"


async def _get_stars_enabled(session) -> bool:
    result = await session.execute(
        select(BotSettings).where(BotSettings.key == "stars_enabled")
    )
    row = result.scalar_one_or_none()
    return (row.value if row else "1") == "1"


async def _get_active_locations(session) -> str:
    """Return a formatted string of active server locations."""
    result = await session.execute(
        select(Server).where(Server.is_active == True).order_by(Server.id)  # noqa: E712
    )
    servers = result.scalars().all()
    if not servers:
        # Fallback: show all servers even if health check marked them inactive
        result = await session.execute(
            select(Server).where(Server.api_url.isnot(None)).order_by(Server.id)
        )
        servers = result.scalars().all()
    if not servers:
        return ""
    parts = [f"{s.country_emoji} {s.location}" for s in servers]
    return "  •  ".join(parts)


async def _has_non_vpn_tariffs(session, user_id: int = 0) -> bool:
    """Check if any active TG proxy or Both tariffs exist."""
    q = (
        select(Tariff.id)
        .where(Tariff.is_active == True)  # noqa: E712
        .where(Tariff.tariff_type != TariffType.VPN)
    )
    if not settings.is_admin(user_id):
        q = q.where(Tariff.is_admin_only == False)  # noqa: E712
    q = q.limit(1)
    result = await session.execute(q)
    return result.scalar_one_or_none() is not None


@router.callback_query(F.data == "buy")
async def start_purchase(callback: CallbackQuery) -> None:
    """Step 0 - Show product type selection, or skip to tariffs if only VPN."""
    async with async_session() as session:
        has_extra = await _has_non_vpn_tariffs(session, user_id=callback.from_user.id)

    if has_extra:
        try:
            await callback.message.edit_text(
                SELECT_PRODUCT_TYPE,
                reply_markup=product_type_kb(),
                parse_mode="HTML",
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise
        await callback.answer()
        return

    # No TG proxy tariffs — go directly to VPN tariff list
    await _show_tariffs(callback, TariffType.VPN, has_product_types=False)


@router.callback_query(F.data.startswith("ptype_"))
async def select_product_type(callback: CallbackQuery) -> None:
    """Step 0b - User picked product type, show tariffs for that type."""
    ptype_str = callback.data.removeprefix("ptype_")
    try:
        tariff_type = TariffType(ptype_str)
    except ValueError:
        tariff_type = TariffType.VPN

    await _show_tariffs(callback, tariff_type, has_product_types=True)


async def _show_tariffs(callback: CallbackQuery, tariff_type: TariffType, has_product_types: bool = False) -> None:
    """Show tariffs filtered by type."""
    is_admin = settings.is_admin(callback.from_user.id)
    async with async_session() as session:
        q = (
            select(Tariff)
            .where(Tariff.is_active == True)  # noqa: E712
            .where(Tariff.tariff_type == tariff_type)
        )
        if not is_admin:
            q = q.where(Tariff.is_admin_only == False)  # noqa: E712
        q = q.order_by(Tariff.price_rub)
        tariff_result = await session.execute(q)
        tariffs = tariff_result.scalars().all()

        stars_enabled = await _get_stars_enabled(session)
        locations_str = await _get_active_locations(session)
        user = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
        if user and any(is_intro_basic_tariff(tariff) for tariff in tariffs):
            intro_tariff = next((tariff for tariff in tariffs if is_intro_basic_tariff(tariff)), None)
            if not await can_purchase_intro_basic_tariff(session, user=user, tariff=intro_tariff):
                tariffs = [tariff for tariff in tariffs if not is_intro_basic_tariff(tariff)]

    if not tariffs:
        try:
            await callback.message.edit_text(NO_TARIFFS, parse_mode="HTML")
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise
        await callback.answer()
        return

    if _is_darimiru_tariff_catalog() and tariff_type in (TariffType.VPN, TariffType.BOTH):
        extra_device_tariffs = [tariff.label for tariff in tariffs if supports_extra_devices(tariff)]
        text = build_darimiru_tariff_text(
            locations_str=locations_str or None,
            extra_device_tariffs=extra_device_tariffs,
        )
    elif _is_anewka_tariff_catalog() and tariff_type in (TariffType.VPN, TariffType.BOTH):
        text = SELECT_TARIFF_ANEWKA
    else:
        text = SELECT_TARIFF

    try:
        await callback.message.edit_text(
            text,
            reply_markup=tariffs_kb(tariffs, stars_enabled, has_product_types=has_product_types),
            parse_mode="HTML",
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    await callback.answer()


@router.callback_query(F.data == "renewal_choice")
async def renewal_choice(callback: CallbackQuery) -> None:
    """Show renewal choice when user has multiple tariffs (from different providers)."""
    from bot.keyboards.client import renewal_choice_kb
    from bot.handlers.profile import _build_renewal_options

    async with async_session() as session:
        user = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
        if not user:
            await callback.answer("Пользователь не найден", show_alert=True)
            return
        renewal_options = await _build_renewal_options(session, user)

    if not renewal_options:
        # No active tariffs found — fall back to full tariff list
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        from aiogram.utils.keyboard import InlineKeyboardBuilder
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="profile"))
        await callback.message.edit_text(
            "Нет доступных тарифов. Перейдите в раздел покупки.",
            reply_markup=builder.as_markup(),
        )
        await callback.answer()
        return

    text = "Выберите, какой тариф продлить:\n\n"
    for _tid, label, rate in renewal_options:
        text += f"• <b>{label}</b> — {rate:.2f} ₽/день\n"

    await callback.message.edit_text(
        text,
        reply_markup=renewal_choice_kb(renewal_options),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("renew_tariff_"))
async def renew_tariff(callback: CallbackQuery) -> None:
    """User selected a specific tariff for renewal — go to payment flow."""
    tariff_id = int(callback.data.removeprefix("renew_tariff_"))

    async with async_session() as session:
        tariff = await session.get(Tariff, tariff_id)
        user = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))

    if not tariff:
        await callback.answer("Тариф не найден", show_alert=True)
        return
    if not tariff.is_active:
        await callback.answer("Этот тариф больше недоступен", show_alert=True)
        return
    if tariff.tariff_type in (TariffType.VPN, TariffType.BOTH) and not tariff.adapt_plan_uuid and not tariff.vhq_tier:
        await callback.answer("Этот тариф больше недоступен", show_alert=True)
        return

    # Simulate a tariff_ callback to reuse existing select_tariff logic
    callback.data = f"tariff_{tariff_id}"
    await select_tariff(callback)


@router.callback_query(F.data.startswith("tariff_"))
async def select_tariff(callback: CallbackQuery) -> None:
    """Step 2 - User picked a tariff, show platform selection or payment."""
    parts = callback.data.split("_")
    tariff_id = int(parts[1])

    async with async_session() as session:
        tariff = await session.get(Tariff, tariff_id)
        has_product_types = await _has_non_vpn_tariffs(session, user_id=callback.from_user.id)
        user = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
        intro_basic_available = await can_purchase_intro_basic_tariff(session, user=user, tariff=tariff)

    if not tariff:
        await callback.answer("Тариф не найден", show_alert=True)
        return
    if not tariff.is_active:
        await callback.answer("Этот тариф больше недоступен", show_alert=True)
        return
    if not intro_basic_available:
        await callback.answer(INTRO_BASIC_ALREADY_USED_TEXT, show_alert=True)
        return

    # Back should go to tariff list for the same product type
    if has_product_types:
        tariff_back = f"ptype_{tariff.tariff_type.value}"
    else:
        tariff_back = "buy"

    # For TG proxy tariffs, skip platform selection
    if tariff.tariff_type == TariffType.TG_PROXY:
        async with async_session() as session:
            stars_enabled = await _get_stars_enabled(session)
            legal_urls = await get_all_legal_doc_urls(session)
            result = await session.execute(
                select(User).where(User.telegram_id == callback.from_user.id)
            )
            user = result.scalar_one_or_none()
            user_balance = get_user_balance(user)

        partner_discount_text = await _partner_discount_text(callback.from_user.id, tariff)
        price_str = f"{tariff.price_rub}₽"
        if stars_enabled and tariff.price_stars:
            price_str += f" / {tariff.price_stars}⭐"

        balance_info = f"💰 Ваш баланс: <b>{user_balance:.2f} ₽</b>\n\n" if user_balance > 0 else ""
        try:
            await callback.message.edit_text(
                _select_payment_text().format(
                    balance_info=balance_info,
                    tariff=tariff.label,
                    platform="📱 Telegram",
                    price=price_str,
                    legal_notice=build_legal_notice(legal_urls),
                ) + build_tariff_purchase_note(tariff, darimiru=_is_darimiru_tariff_catalog()) + partner_discount_text,
                reply_markup=payment_kb(
                    tariff_id,
                    "tgproxy",
                    stars_enabled,
                    user_balance,
                    float(tariff.price_rub),
                    legal_urls,
                    back_callback=tariff_back,
                ),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise
        await callback.answer()
        return

    # VPN or Both — platform is selected after successful payment, before key delivery.
    async with async_session() as session:
        stars_enabled = await _get_stars_enabled(session)
        legal_urls = await get_all_legal_doc_urls(session)
        user = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
        user_balance = get_user_balance(user)

    partner_discount_text = await _partner_discount_text(callback.from_user.id, tariff)
    price_str = f"{tariff.price_rub}₽"
    if stars_enabled and tariff.price_stars:
        price_str += f" / {tariff.price_stars}⭐"
    balance_info = f"💰 Ваш баланс: <b>{user_balance:.2f} ₽</b>\n\n" if user_balance > 0 else ""
    try:
        await callback.message.edit_text(
            _select_payment_text().format(
                balance_info=balance_info,
                tariff=tariff.label,
                platform="после оплаты, если нужно",
                price=price_str,
                legal_notice=build_legal_notice(legal_urls),
            )
            + "\nЕсли устройство уже выбирали раньше, ключ придёт сразу. Если нет — после оплаты бот спросит устройство и отправит ключ с нужным гайдом.\n"
            + build_tariff_purchase_note(tariff, darimiru=_is_darimiru_tariff_catalog())
            + partner_discount_text,
            reply_markup=payment_kb(
                tariff_id,
                "deferred",
                stars_enabled,
                user_balance,
                float(tariff.price_rub),
                legal_urls,
                back_callback=tariff_back,
            ),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    await callback.answer()


@router.callback_query(F.data.startswith("plat_"))
async def select_platform(callback: CallbackQuery) -> None:
    """Step 3 - User picked platform, show payment methods."""
    parts = callback.data.split("_")
    tariff_id = int(parts[1])
    platform = parts[2]

    async with async_session() as session:
        tariff = await session.get(Tariff, tariff_id)
        stars_enabled = await _get_stars_enabled(session)
        legal_urls = await get_all_legal_doc_urls(session)
        result = await session.execute(
            select(User).where(User.telegram_id == callback.from_user.id)
        )
        user = result.scalar_one_or_none()
        user_balance = get_user_balance(user)

    if not tariff:
        await callback.answer("Тариф не найден", show_alert=True)
        return
    if not tariff.is_active or tariff.is_admin_only:
        await callback.answer("Этот тариф больше недоступен", show_alert=True)
        return

    platform_labels = {
        "android": "🤖 Android",
        "ios": "🍎 iOS",
        "mac": "🍏 Mac",
        "windows": "💻 Windows",
        "android_tv": "📺 Android TV",
    }
    platform_label = platform_labels.get(platform, platform)
    partner_discount_text = await _partner_discount_text(callback.from_user.id, tariff)
    price_str = f"{tariff.price_rub}₽"
    if stars_enabled and tariff.price_stars:
        price_str += f" / {tariff.price_stars}⭐"

    balance_info = f"💰 Ваш баланс: <b>{user_balance:.2f} ₽</b>\n\n" if user_balance > 0 else ""
    try:
        await callback.message.edit_text(
            _select_payment_text().format(
                balance_info=balance_info,
                tariff=tariff.label,
                platform=platform_label,
                price=price_str,
                legal_notice=build_legal_notice(legal_urls),
            ) + build_tariff_purchase_note(tariff, darimiru=_is_darimiru_tariff_catalog()) + partner_discount_text,
            reply_markup=payment_kb(
                tariff_id,
                platform,
                stars_enabled,
                user_balance,
                float(tariff.price_rub),
                legal_urls,
            ),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    await callback.answer()
