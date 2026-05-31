"""Partner (blogger/affiliate) management - admin CRUD + partner self-service dashboard."""

from __future__ import annotations

import csv
import io
import logging
import re
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select

from bot.config import settings
from bot.database import async_session
from bot.models import (
    Partner,
    PartnerApplication,
    PartnerApplicationStatus,
    PartnerEarning,
    PartnerLink,
    PartnerPayout,
    PartnerPayoutStatus,
    PartnerPlatform,
    User,
    WebPartnerEarning,
)

logger = logging.getLogger(__name__)
router = Router(name="partner")

ESCAPE_COMMANDS = {"/start", "/help", "/admin", "/policy", "/agree", "/oferta"}
ITEMS_PER_PAGE = 10

PLATFORM_LABELS = {
    PartnerPlatform.YOUTUBE: "YouTube",
    PartnerPlatform.INSTAGRAM: "Instagram",
    PartnerPlatform.TELEGRAM: "Telegram",
    PartnerPlatform.TIKTOK: "TikTok",
    PartnerPlatform.OTHER: "Другое",
}

PLATFORM_EMOJI = {
    PartnerPlatform.YOUTUBE: "📺",
    PartnerPlatform.INSTAGRAM: "📸",
    PartnerPlatform.TELEGRAM: "✈️",
    PartnerPlatform.TIKTOK: "🎵",
    PartnerPlatform.OTHER: "🔗",
}


# ── FSM ───────────────────────────────────────────────

class PartnerAdminStates(StatesGroup):
    create_target = State()
    create_telegram_id = State()
    create_commission = State()
    edit_field = State()
    link_code = State()
    free_months = State()


class PartnerPayoutStates(StatesGroup):
    request_amount = State()
    request_details = State()
    reject_comment = State()


class PartnerSelfStates(StatesGroup):
    link_code = State()


class PartnerApplicationStates(StatesGroup):
    details = State()
    reject_comment = State()


# ── Guards ────────────────────────────────────────────

@router.message(Command("start"), PartnerAdminStates())
async def _partner_state_start(message: Message, state: FSMContext) -> None:
    from bot.handlers.start import cmd_start
    await cmd_start(message, state)


@router.message(Command("help"), PartnerAdminStates())
async def _partner_state_help(message: Message, state: FSMContext) -> None:
    from bot.handlers.start import show_help_command
    await show_help_command(message, state)


@router.message(Command("admin"), PartnerAdminStates())
async def _partner_state_admin(message: Message, state: FSMContext) -> None:
    from bot.handlers.admin import cmd_admin
    await cmd_admin(message, state)


@router.message(PartnerAdminStates(), F.text.startswith("/"))
async def _guard_partner_admin(message: Message) -> None:
    command = (message.text or "").split(maxsplit=1)[0].lower()
    if command in ESCAPE_COMMANDS:
        return
    await message.answer("⚠️ Введите значение или нажмите «◀️ Отмена».")


@router.message(PartnerSelfStates(), F.text.startswith("/"))
async def _guard_partner_self(message: Message) -> None:
    await message.answer("⚠️ Введите значение или нажмите «◀️ Назад».")


@router.message(PartnerApplicationStates(), F.text.startswith("/"))
async def _guard_partner_application(message: Message) -> None:
    await message.answer("⚠️ Опишите заявку или нажмите «◀️ Главное меню».")


# ── Helpers ───────────────────────────────────────────

def _is_admin(uid: int) -> bool:
    return settings.is_admin(uid)


def _back_partners_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ К партнёрам", callback_data="adm_partners")],
    ])


def _back_partner_kb(partner_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"adm_pt_{partner_id}")],
    ])


def _partner_dashboard_kb(*, has_pending: bool = False, payouts_enabled: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🔗 Управление ссылками", callback_data="partner_links_manage"),
    )
    builder.row(
        InlineKeyboardButton(text="📤 Экспорт CSV", callback_data="partner_export_csv"),
    )
    builder.row(
        InlineKeyboardButton(text="💰 История начислений", callback_data="partner_earnings_history"),
        InlineKeyboardButton(text="📥 Начисления CSV", callback_data="partner_earnings_export_csv"),
    )
    if payouts_enabled:
        builder.row(
            InlineKeyboardButton(text="💸 Запросить выплату", callback_data="partner_payout_request"),
        )
        builder.row(
            InlineKeyboardButton(text="📜 История выплат", callback_data="partner_payout_history"),
        )
    if payouts_enabled and has_pending:
        builder.row(
            InlineKeyboardButton(text="⏳ Есть заявка в обработке", callback_data="ignore"),
        )
    builder.row(InlineKeyboardButton(text="◀️ Главное меню", callback_data="back_main"))
    return builder.as_markup()


def _back_payouts_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ К заявкам", callback_data="adm_partner_payouts")],
    ])


def _back_partner_dashboard_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ В партнёрский кабинет", callback_data="partner_dashboard")],
    ])


def _back_partner_applications_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ К заявкам", callback_data="adm_partner_apps")],
    ])


def _back_partner_links_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ К ссылкам", callback_data="partner_links_manage")],
    ])


def _back_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Главное меню", callback_data="back_main")],
    ])


def _format_payout_status(status: PartnerPayoutStatus) -> str:
    return {
        PartnerPayoutStatus.PENDING: "⏳ В обработке",
        PartnerPayoutStatus.APPROVED: "✅ Выплачено",
        PartnerPayoutStatus.REJECTED: "❌ Отклонено",
    }.get(status, status.value)


def _format_application_status(status: PartnerApplicationStatus) -> str:
    return {
        PartnerApplicationStatus.PENDING: "⏳ На рассмотрении",
        PartnerApplicationStatus.APPROVED: "✅ Одобрена",
        PartnerApplicationStatus.REJECTED: "❌ Отклонена",
    }.get(status, status.value)


def _build_partner_web_link(code: str) -> str | None:
    if not settings.webstore_public_enabled:
        return None
    base_url = settings.webstore_api_base_url.strip()
    if not base_url:
        return None
    return f"{base_url.rstrip('/')}/buy?ref=p_{code}"


def _partner_link_prompt(suggested: str) -> str:
    text = (
        "🔗 Введите код ссылки (латиница, цифры, _ ).\n\n"
        f"Пример: <code>{suggested}</code>\n\n"
        "Telegram: <code>t.me/bot?start=p_ВАШ_КОД</code>"
    )
    web_hint = _build_partner_web_link("ВАШ_КОД")
    if web_hint:
        text += f"\nСайт: <code>{web_hint}</code>"
    return text


def _partner_self_link_prompt(platform_label: str, suggested: str) -> str:
    text = (
        f"🔗 Введите код ссылки для {platform_label}.\n\n"
        f"Пример: <code>{suggested}</code>\n\n"
        "Реферальная ссылка будет такой:"
    )
    web_hint = _build_partner_web_link("ВАШ_КОД")
    if web_hint:
        text += f"\nСайт: <code>{web_hint}</code>"
    return text


async def _partner_telegram_earnings_total(session, partner_id: int) -> float:
    return await session.scalar(
        select(func.coalesce(func.sum(PartnerEarning.amount), 0))
        .where(PartnerEarning.partner_id == partner_id)
    ) or 0


async def _partner_web_earnings_total(session, partner_id: int) -> float:
    return await session.scalar(
        select(func.coalesce(func.sum(WebPartnerEarning.earning_amount_rub), 0))
        .where(WebPartnerEarning.partner_id == partner_id)
    ) or 0


async def _partner_web_orders_total(session, partner_id: int) -> int:
    return await session.scalar(
        select(func.count(WebPartnerEarning.id))
        .where(WebPartnerEarning.partner_id == partner_id)
    ) or 0


async def _partner_period_stats(session, partner_id: int) -> dict[str, float]:
    from bot.models import Payment, PaymentStatus

    now = datetime.utcnow()
    since_7 = now - timedelta(days=7)
    since_30 = now - timedelta(days=30)

    regs_7 = await session.scalar(
        select(func.count(User.id)).where(
            User.partner_id == partner_id,
            User.created_at >= since_7,
        )
    ) or 0
    regs_30 = await session.scalar(
        select(func.count(User.id)).where(
            User.partner_id == partner_id,
            User.created_at >= since_30,
        )
    ) or 0
    pays_7 = await session.scalar(
        select(func.count(PartnerEarning.id))
        .join(Payment, Payment.id == PartnerEarning.payment_id)
        .where(
            PartnerEarning.partner_id == partner_id,
            Payment.status == PaymentStatus.COMPLETED,
            PartnerEarning.created_at >= since_7,
        )
    ) or 0
    pays_30 = await session.scalar(
        select(func.count(PartnerEarning.id))
        .join(Payment, Payment.id == PartnerEarning.payment_id)
        .where(
            PartnerEarning.partner_id == partner_id,
            Payment.status == PaymentStatus.COMPLETED,
            PartnerEarning.created_at >= since_30,
        )
    ) or 0
    earn_7 = await session.scalar(
        select(func.coalesce(func.sum(PartnerEarning.amount), 0)).where(
            PartnerEarning.partner_id == partner_id,
            PartnerEarning.created_at >= since_7,
        )
    ) or 0
    earn_30 = await session.scalar(
        select(func.coalesce(func.sum(PartnerEarning.amount), 0)).where(
            PartnerEarning.partner_id == partner_id,
            PartnerEarning.created_at >= since_30,
        )
    ) or 0
    web_orders_7 = await session.scalar(
        select(func.count(WebPartnerEarning.id)).where(
            WebPartnerEarning.partner_id == partner_id,
            WebPartnerEarning.created_at >= since_7,
        )
    ) or 0
    web_orders_30 = await session.scalar(
        select(func.count(WebPartnerEarning.id)).where(
            WebPartnerEarning.partner_id == partner_id,
            WebPartnerEarning.created_at >= since_30,
        )
    ) or 0
    web_earn_7 = await session.scalar(
        select(func.coalesce(func.sum(WebPartnerEarning.earning_amount_rub), 0)).where(
            WebPartnerEarning.partner_id == partner_id,
            WebPartnerEarning.created_at >= since_7,
        )
    ) or 0
    web_earn_30 = await session.scalar(
        select(func.coalesce(func.sum(WebPartnerEarning.earning_amount_rub), 0)).where(
            WebPartnerEarning.partner_id == partner_id,
            WebPartnerEarning.created_at >= since_30,
        )
    ) or 0

    return {
        "regs_7": regs_7,
        "regs_30": regs_30,
        "pays_7": pays_7,
        "pays_30": pays_30,
        "earn_7": earn_7,
        "earn_30": earn_30,
        "web_orders_7": web_orders_7,
        "web_orders_30": web_orders_30,
        "web_earn_7": web_earn_7,
        "web_earn_30": web_earn_30,
    }


async def _resolve_partner_identity(session, raw: str) -> tuple[int | None, str | None, str | None]:
    value = raw.strip()
    if not value:
        return None, None, None

    lookup_user = None
    if value.startswith("@") or (not re.fullmatch(r"-?\d+", value) and re.fullmatch(r"[A-Za-z0-9_]{3,32}", value)):
        username = value.lstrip("@").lower()
        lookup_user = await session.scalar(
            select(User).where(func.lower(User.username) == username)
        )
        if not lookup_user:
            return None, None, None
    else:
        try:
            telegram_id = int(value)
        except ValueError:
            return None, None, None
        lookup_user = await session.scalar(
            select(User).where(User.telegram_id == telegram_id)
        )
        if lookup_user:
            pass
        else:
            fallback_name = f"Partner {telegram_id}"
            return telegram_id, fallback_name, None

    display_name = (lookup_user.full_name or lookup_user.username or f"Partner {lookup_user.telegram_id}")[:128]
    contact_info = f"@{lookup_user.username}" if lookup_user.username else None
    return lookup_user.telegram_id, display_name, contact_info


async def _build_partners_csv(session) -> bytes:
    from bot.models import Payment, PaymentStatus

    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow([
        "partner_id",
        "name",
        "is_active",
        "telegram_id",
        "commission_percent",
        "audience_discount_percent",
        "audience_bonus_days",
        "payouts_enabled",
        "min_payout",
        "partner_balance",
        "users_total",
        "purchases_total",
        "telegram_earnings_total",
        "web_orders_total",
        "web_earnings_total",
        "earnings_total",
        "regs_7d",
        "purchases_7d",
        "telegram_earnings_7d",
        "web_orders_7d",
        "web_earnings_7d",
        "regs_30d",
        "purchases_30d",
        "telegram_earnings_30d",
        "web_orders_30d",
        "web_earnings_30d",
    ])

    partners = (await session.execute(
        select(Partner).order_by(Partner.created_at.desc())
    )).scalars().all()
    for partner in partners:
        user_count = await session.scalar(
            select(func.count(User.id)).where(User.partner_id == partner.id)
        ) or 0
        purchase_count = await session.scalar(
            select(func.count(Payment.id))
            .join(User, Payment.user_id == User.id)
            .where(User.partner_id == partner.id, Payment.status == PaymentStatus.COMPLETED)
        ) or 0
        tg_earned = await _partner_telegram_earnings_total(session, partner.id)
        web_orders_total = await _partner_web_orders_total(session, partner.id)
        web_earned = await _partner_web_earnings_total(session, partner.id)
        total_earned = tg_earned + web_earned
        period_stats = await _partner_period_stats(session, partner.id)
        writer.writerow([
            partner.id,
            partner.name,
            int(bool(partner.is_active)),
            partner.telegram_id or "",
            partner.commission_percent,
            partner.audience_discount_percent,
            partner.audience_bonus_days,
            int(bool(partner.payouts_enabled)),
            partner.min_payout,
            partner.partner_balance,
            user_count,
            purchase_count,
            tg_earned,
            web_orders_total,
            web_earned,
            total_earned,
            period_stats["regs_7"],
            period_stats["pays_7"],
            period_stats["earn_7"],
            period_stats["web_orders_7"],
            period_stats["web_earn_7"],
            period_stats["regs_30"],
            period_stats["pays_30"],
            period_stats["earn_30"],
            period_stats["web_orders_30"],
            period_stats["web_earn_30"],
        ])
    return buf.getvalue().encode("utf-8-sig")


async def _build_partner_payouts_csv(session, partner_id: int | None = None) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow([
        "payout_id",
        "partner_id",
        "partner_name",
        "amount",
        "status",
        "details",
        "admin_comment",
        "requested_at",
        "processed_at",
        "processed_by",
    ])

    query = select(PartnerPayout).order_by(PartnerPayout.requested_at.desc())
    if partner_id is not None:
        query = query.where(PartnerPayout.partner_id == partner_id)
    payouts = (await session.execute(query)).scalars().all()

    for payout in payouts:
        partner = await session.get(Partner, payout.partner_id)
        writer.writerow([
            payout.id,
            payout.partner_id,
            partner.name if partner else "",
            payout.amount,
            payout.status.value,
            payout.details or "",
            payout.admin_comment or "",
            payout.requested_at.isoformat(sep=" ", timespec="seconds"),
            payout.processed_at.isoformat(sep=" ", timespec="seconds") if payout.processed_at else "",
            payout.processed_by or "",
        ])
    return buf.getvalue().encode("utf-8-sig")


async def _build_partner_earnings_csv(session, partner_id: int) -> bytes:
    partner = await session.get(Partner, partner_id)
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow([
        "source",
        "earning_id",
        "partner_id",
        "partner_name",
        "amount",
        "created_at",
        "user_id",
        "user_telegram_id",
        "username",
        "full_name",
        "payment_id",
        "web_order_id",
        "buyer_contact",
        "tariff_label",
        "ref_code",
    ])

    if not partner:
        return buf.getvalue().encode("utf-8-sig")

    tg_earnings = (await session.execute(
        select(PartnerEarning)
        .where(PartnerEarning.partner_id == partner_id)
        .order_by(PartnerEarning.created_at.desc(), PartnerEarning.id.desc())
    )).scalars().all()
    web_earnings = (await session.execute(
        select(WebPartnerEarning)
        .where(WebPartnerEarning.partner_id == partner_id)
        .order_by(WebPartnerEarning.created_at.desc(), WebPartnerEarning.id.desc())
    )).scalars().all()

    for earning in tg_earnings:
        user = await session.get(User, earning.user_id)
        writer.writerow([
            "telegram",
            earning.id,
            partner.id,
            partner.name,
            earning.amount,
            earning.created_at.isoformat(sep=" ", timespec="seconds"),
            earning.user_id,
            user.telegram_id if user else "",
            user.username if user and user.username else "",
            user.full_name if user and user.full_name else "",
            earning.payment_id or "",
            "",
            "",
            "",
            "",
        ])
    for earning in web_earnings:
        writer.writerow([
            "web",
            earning.id,
            partner.id,
            partner.name,
            earning.earning_amount_rub,
            earning.created_at.isoformat(sep=" ", timespec="seconds"),
            "",
            "",
            "",
            "",
            "",
            earning.web_order_id,
            earning.buyer_contact or "",
            earning.tariff_label or "",
            earning.ref_code or "",
        ])
    return buf.getvalue().encode("utf-8-sig")


async def _render_partner_links_manage(message, partner_id: int, bot, edit: bool = True) -> None:
    async with async_session() as session:
        partner = await session.get(Partner, partner_id)
        if not partner:
            text = "Партнёр не найден."
            if edit:
                await message.edit_text(text, reply_markup=_back_partner_dashboard_kb())
            else:
                await message.answer(text, reply_markup=_back_partner_dashboard_kb())
            return
        await session.refresh(partner, attribute_names=["links"])

        lines = []
        for link in partner.links:
            emoji = PLATFORM_EMOJI.get(link.platform, "🔗")
            plat = PLATFORM_LABELS.get(link.platform, link.platform.value)
            status = "🟢" if link.is_active else "🔴"
            user_count = await session.scalar(
                select(func.count(User.id)).where(User.partner_link_id == link.id)
            ) or 0
            tg_purchase_count = await session.scalar(
                select(func.count(PartnerEarning.id))
                .join(User, PartnerEarning.user_id == User.id)
                .where(User.partner_link_id == link.id)
            ) or 0
            web_orders_count = await session.scalar(
                select(func.count(WebPartnerEarning.id)).where(WebPartnerEarning.partner_link_id == link.id)
            ) or 0
            web_link = _build_partner_web_link(link.code)
            lines.append(
                f"{status} {emoji} <b>{plat}</b>\n"
                + (f"  Ссылка: <code>{web_link}</code>\n" if web_link else f"  Код: <code>{link.code}</code>\n")
                + f"  Telegram: {user_count} рег. / {tg_purchase_count} оплат.\n"
                + f"  Сайт: {web_orders_count} заказ."
            )

    builder = InlineKeyboardBuilder()
    for p_enum in PartnerPlatform:
        emoji = PLATFORM_EMOJI.get(p_enum, "🔗")
        label = PLATFORM_LABELS.get(p_enum, p_enum.value)
        builder.row(InlineKeyboardButton(
            text=f"➕ {emoji} {label}",
            callback_data=f"partner_lp_{p_enum.value}",
        ))
    for link in partner.links:
        toggle = "🔴 Отключить" if link.is_active else "🟢 Включить"
        plat = PLATFORM_LABELS.get(link.platform, link.platform.value)
        builder.row(InlineKeyboardButton(
            text=f"{toggle}: {plat} ({link.code})",
            callback_data=f"partner_lt_{link.id}",
        ))
    builder.row(InlineKeyboardButton(text="◀️ В партнёрский кабинет", callback_data="partner_dashboard"))

    text = f"🔗 <b>Управление ссылками</b>\n\n"
    text += "\n\n".join(lines) if lines else "Ссылок пока нет. Добавьте первую ссылку для своей площадки."

    if edit:
        await message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")


async def _build_single_partner_report_csv(session, partner_id: int) -> bytes:
    from bot.models import Payment, PaymentStatus

    partner = await session.get(Partner, partner_id)
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")

    if not partner:
        writer.writerow(["error", "partner_not_found"])
        return buf.getvalue().encode("utf-8-sig")
    await session.refresh(partner, attribute_names=["links"])

    user_count = await session.scalar(
        select(func.count(User.id)).where(User.partner_id == partner.id)
    ) or 0
    purchase_count = await session.scalar(
        select(func.count(Payment.id))
        .join(User, Payment.user_id == User.id)
        .where(User.partner_id == partner.id, Payment.status == PaymentStatus.COMPLETED)
    ) or 0
    tg_earned = await _partner_telegram_earnings_total(session, partner.id)
    web_orders_total = await _partner_web_orders_total(session, partner.id)
    web_earned = await _partner_web_earnings_total(session, partner.id)
    total_earned = tg_earned + web_earned
    period_stats = await _partner_period_stats(session, partner.id)

    writer.writerow(["section", "partner_summary"])
    writer.writerow(["partner_id", partner.id])
    writer.writerow(["name", partner.name])
    writer.writerow(["telegram_id", partner.telegram_id or ""])
    writer.writerow(["is_active", int(bool(partner.is_active))])
    writer.writerow(["commission_percent", partner.commission_percent])
    writer.writerow(["audience_discount_percent", partner.audience_discount_percent])
    writer.writerow(["audience_bonus_days", partner.audience_bonus_days])
    writer.writerow(["payouts_enabled", int(bool(partner.payouts_enabled))])
    writer.writerow(["partner_balance", partner.partner_balance])
    writer.writerow(["min_payout", partner.min_payout])
    writer.writerow(["users_total", user_count])
    writer.writerow(["purchases_total", purchase_count])
    writer.writerow(["telegram_earnings_total", tg_earned])
    writer.writerow(["web_orders_total", web_orders_total])
    writer.writerow(["web_earnings_total", web_earned])
    writer.writerow(["earnings_total", total_earned])
    writer.writerow(["regs_7d", period_stats["regs_7"]])
    writer.writerow(["purchases_7d", period_stats["pays_7"]])
    writer.writerow(["telegram_earnings_7d", period_stats["earn_7"]])
    writer.writerow(["web_orders_7d", period_stats["web_orders_7"]])
    writer.writerow(["web_earnings_7d", period_stats["web_earn_7"]])
    writer.writerow(["regs_30d", period_stats["regs_30"]])
    writer.writerow(["purchases_30d", period_stats["pays_30"]])
    writer.writerow(["telegram_earnings_30d", period_stats["earn_30"]])
    writer.writerow(["web_orders_30d", period_stats["web_orders_30"]])
    writer.writerow(["web_earnings_30d", period_stats["web_earn_30"]])
    writer.writerow([])

    writer.writerow(["section", "link_breakdown"])
    writer.writerow(["link_id", "code", "platform", "is_active", "registrations", "purchases", "web_orders"])
    for link in partner.links:
        link_users = await session.scalar(
            select(func.count(User.id)).where(User.partner_link_id == link.id)
        ) or 0
        link_purchases = await session.scalar(
            select(func.count(Payment.id))
            .join(User, Payment.user_id == User.id)
            .where(
                User.partner_link_id == link.id,
                Payment.status == PaymentStatus.COMPLETED,
            )
        ) or 0
        web_orders = await session.scalar(
            select(func.count(WebPartnerEarning.id)).where(WebPartnerEarning.partner_link_id == link.id)
        ) or 0
        writer.writerow([
            link.id,
            link.code,
            link.platform.value,
            int(bool(link.is_active)),
            link_users,
            link_purchases,
            web_orders,
        ])
    return buf.getvalue().encode("utf-8-sig")


async def _render_admin_partner_applications_list(message, edit: bool = True) -> None:
    async with async_session() as session:
        applications = (await session.execute(
            select(PartnerApplication).order_by(
                PartnerApplication.created_at.desc(),
                PartnerApplication.id.desc(),
            ).limit(30)
        )).scalars().all()

    builder = InlineKeyboardBuilder()
    for application in applications:
        status = _format_application_status(application.status)
        title = application.full_name or application.username or str(application.telegram_id)
        builder.row(InlineKeyboardButton(
            text=f"{status} • {title}",
            callback_data=f"adm_ptappo_{application.id}",
        ))
    builder.row(InlineKeyboardButton(text="◀️ К партнёрам", callback_data="adm_partners"))

    if not applications:
        text = "📝 <b>Заявки в партнёрскую программу</b>\n\nЗаявок пока нет."
    else:
        pending_count = sum(1 for app in applications if app.status == PartnerApplicationStatus.PENDING)
        text = (
            "📝 <b>Заявки в партнёрскую программу</b>\n\n"
            f"Всего в списке: <b>{len(applications)}</b>\n"
            f"Ожидают решения: <b>{pending_count}</b>"
        )

    if edit:
        await message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")


async def _render_admin_partner_application_detail(message, application_id: int, edit: bool = True) -> None:
    async with async_session() as session:
        application = await session.get(PartnerApplication, application_id)
        if not application:
            text = "Заявка не найдена."
            if edit:
                await message.edit_text(text, reply_markup=_back_partner_applications_kb())
            else:
                await message.answer(text, reply_markup=_back_partner_applications_kb())
            return

    status = _format_application_status(application.status)
    processed_at = application.processed_at.strftime("%d.%m.%Y %H:%M") if application.processed_at else "—"
    text = (
        f"📝 <b>Заявка #{application.id}</b>\n\n"
        f"Статус: <b>{status}</b>\n"
        f"Имя: <b>{application.full_name or '—'}</b>\n"
        f"Telegram ID: <code>{application.telegram_id}</code>\n"
    )
    if application.username:
        text += f"Username: <b>@{application.username}</b>\n"
    if application.contact_info:
        text += f"Контакт: {application.contact_info}\n"
    text += (
        f"Создана: <b>{application.created_at.strftime('%d.%m.%Y %H:%M')}</b>\n"
        f"Обработана: <b>{processed_at}</b>\n"
    )
    if application.notes:
        text += f"\nОписание:\n{application.notes}\n"
    if application.admin_comment:
        text += f"\nКомментарий администратора:\n{application.admin_comment}\n"

    builder = InlineKeyboardBuilder()
    if application.status == PartnerApplicationStatus.PENDING:
        builder.row(
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"adm_ptapp_ok_{application.id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm_ptapp_rej_{application.id}"),
        )
    builder.row(InlineKeyboardButton(text="◀️ К заявкам", callback_data="adm_partner_apps"))

    if edit:
        await message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")


# ── Admin: Partner List ──────────────────────────────

@router.callback_query(F.data == "adm_partners")
async def admin_partners_list(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await state.clear()

    async with async_session() as session:
        partners = (await session.execute(
            select(Partner).order_by(Partner.created_at.desc())
        )).scalars().all()

        total_tg_earnings = await session.scalar(
            select(func.coalesce(func.sum(PartnerEarning.amount), 0))
        ) or 0
        total_web_earnings = await session.scalar(
            select(func.coalesce(func.sum(WebPartnerEarning.earning_amount_rub), 0))
        ) or 0
        total_web_orders = await session.scalar(
            select(func.count(WebPartnerEarning.id))
        ) or 0
        total_users = await session.scalar(
            select(func.count(User.id)).where(User.partner_id.isnot(None))
        ) or 0
        pending_applications = await session.scalar(
            select(func.count(PartnerApplication.id)).where(
                PartnerApplication.status == PartnerApplicationStatus.PENDING
            )
        ) or 0
        pending_payouts = await session.scalar(
            select(func.count(PartnerPayout.id)).where(PartnerPayout.status == PartnerPayoutStatus.PENDING)
        ) or 0

    builder = InlineKeyboardBuilder()
    for p in partners:
        status = "🟢" if p.is_active else "🔴"
        builder.row(InlineKeyboardButton(
            text=f"{status} {p.name}",
            callback_data=f"adm_pt_{p.id}",
        ))
    if pending_applications:
        builder.row(InlineKeyboardButton(
            text=f"📝 Заявки в партнёрку ({pending_applications})",
            callback_data="adm_partner_apps",
        ))
    else:
        builder.row(InlineKeyboardButton(
            text="📝 Заявки в партнёрку",
            callback_data="adm_partner_apps",
        ))
    if pending_payouts:
        builder.row(InlineKeyboardButton(
            text=f"💸 Заявки на выплаты ({pending_payouts})",
            callback_data="adm_partner_payouts",
        ))
    else:
        builder.row(InlineKeyboardButton(
            text="💸 Заявки на выплаты",
            callback_data="adm_partner_payouts",
        ))
    builder.row(InlineKeyboardButton(text="📤 Экспорт партнёров CSV", callback_data="adm_partners_export"))
    builder.row(InlineKeyboardButton(text="➕ Добавить партнёра", callback_data="adm_pt_add"))
    builder.row(InlineKeyboardButton(text="◀️ В админку", callback_data="adm_back"))

    await callback.message.edit_text(
        f"🤝 <b>Партнёрская программа</b>\n\n"
        f"Партнёров: <b>{len(partners)}</b>\n"
        f"Пользователей от партнёров: <b>{total_users}</b>\n"
        f"Telegram начисления: <b>{total_tg_earnings:.0f}₽</b>\n"
        f"Веб-заказы: <b>{total_web_orders}</b>\n"
        f"Веб начисления: <b>{total_web_earnings:.0f}₽</b>\n"
        f"Всего начислено: <b>{(total_tg_earnings + total_web_earnings):.0f}₽</b>",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )
    await callback.answer()


# ── Admin: Partner Applications ──────────────────────

@router.callback_query(F.data == "adm_partner_apps")
async def admin_partner_applications(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await _render_admin_partner_applications_list(callback.message)
    await callback.answer()


@router.callback_query(F.data.startswith("adm_ptappo_"))
async def admin_partner_application_detail(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    application_id = int(callback.data.removeprefix("adm_ptappo_"))
    await _render_admin_partner_application_detail(callback.message, application_id)
    await callback.answer()


@router.callback_query(F.data.startswith("adm_ptapp_ok_"))
async def admin_partner_application_approve(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    application_id = int(callback.data.removeprefix("adm_ptapp_ok_"))
    async with async_session() as session:
        application = await session.get(PartnerApplication, application_id)
        if not application or application.status != PartnerApplicationStatus.PENDING:
            await callback.answer("Заявка уже обработана", show_alert=True)
            return

        existing_partner = await session.scalar(
            select(Partner).where(Partner.telegram_id == application.telegram_id)
        )
        if existing_partner:
            await callback.answer("Партнёр с таким Telegram ID уже существует", show_alert=True)
            return

        partner_name = (application.full_name or application.username or f"Partner {application.telegram_id}")[:128]
        partner = Partner(
            name=partner_name,
            telegram_id=application.telegram_id,
            contact_info=application.contact_info,
            notes=application.notes,
            commission_percent=20.0,
            payouts_enabled=False,
        )
        session.add(partner)
        application.status = PartnerApplicationStatus.APPROVED
        application.processed_at = datetime.utcnow()
        application.processed_by = callback.from_user.id
        await session.commit()
        await session.refresh(partner)
        partner_id = partner.id
        telegram_id = application.telegram_id

    try:
        await callback.bot.send_message(
            telegram_id,
            "✅ Ваша заявка в партнёрскую программу одобрена. В главном меню уже доступен партнёрский кабинет.",
        )
    except Exception as e:
        logger.warning("Failed to notify user %s about approved partner application: %s", telegram_id, e)

    await _show_partner_detail(callback.message, partner_id)
    await callback.answer("Партнёр создан")


@router.callback_query(F.data.startswith("adm_ptapp_rej_"))
async def admin_partner_application_reject_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    application_id = int(callback.data.removeprefix("adm_ptapp_rej_"))
    await state.set_state(PartnerApplicationStates.reject_comment)
    await state.update_data(pt_app_reject_id=application_id)
    await callback.message.edit_text(
        "Введите комментарий для отклонения или <b>-</b> без комментария:",
        reply_markup=_back_partner_applications_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(PartnerApplicationStates.reject_comment)
async def admin_partner_application_reject_save(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    application_id = data["pt_app_reject_id"]
    comment = (message.text or "").strip()
    if comment == "-":
        comment = None

    async with async_session() as session:
        application = await session.get(PartnerApplication, application_id)
        if not application or application.status != PartnerApplicationStatus.PENDING:
            await state.clear()
            await message.answer("Заявка уже обработана.", reply_markup=_back_partner_applications_kb())
            return
        application.status = PartnerApplicationStatus.REJECTED
        application.admin_comment = comment
        application.processed_at = datetime.utcnow()
        application.processed_by = message.from_user.id
        await session.commit()
        telegram_id = application.telegram_id

    await state.clear()
    try:
        user_text = "❌ Ваша заявка в партнёрскую программу отклонена."
        if comment:
            user_text += f"\n\nКомментарий: {comment}"
        await message.bot.send_message(telegram_id, user_text, parse_mode="HTML")
    except Exception as e:
        logger.warning("Failed to notify user %s about rejected partner application: %s", telegram_id, e)

    await _render_admin_partner_application_detail(message, application_id, edit=False)


# ── Admin: Partner Detail ────────────────────────────

async def _show_partner_detail(message, partner_id: int, edit: bool = True) -> None:
    async with async_session() as session:
        from bot.models import Payment, PaymentStatus

        partner = await session.get(Partner, partner_id)
        if not partner:
            if edit:
                await message.edit_text("Партнёр не найден.", reply_markup=_back_partners_kb())
            else:
                await message.answer("Партнёр не найден.", reply_markup=_back_partners_kb())
            return

        # Stats
        user_count = await session.scalar(
            select(func.count(User.id)).where(User.partner_id == partner.id)
        ) or 0
        tg_earned = await _partner_telegram_earnings_total(session, partner.id)
        web_orders_total = await _partner_web_orders_total(session, partner.id)
        web_earned = await _partner_web_earnings_total(session, partner.id)
        total_earned = tg_earned + web_earned
        pending_payout_sum = await session.scalar(
            select(func.coalesce(func.sum(PartnerPayout.amount), 0)).where(
                PartnerPayout.partner_id == partner.id,
                PartnerPayout.status == PartnerPayoutStatus.PENDING,
            )
        ) or 0
        period_stats = await _partner_period_stats(session, partner.id)

        # Per-link stats
        link_stats = []
        for link in partner.links:
            link_users = await session.scalar(
                select(func.count(User.id)).where(User.partner_link_id == link.id)
            ) or 0
            link_purchases = await session.scalar(
                select(func.count(PartnerEarning.id))
                .join(User, PartnerEarning.user_id == User.id)
                .join(Payment, Payment.id == PartnerEarning.payment_id)
                .where(
                    PartnerEarning.partner_id == partner.id,
                    User.partner_link_id == link.id,
                    Payment.status == PaymentStatus.COMPLETED,
                )
            ) or 0
            web_orders = await session.scalar(
                select(func.count(WebPartnerEarning.id)).where(WebPartnerEarning.partner_link_id == link.id)
            ) or 0
            emoji = PLATFORM_EMOJI.get(link.platform, "🔗")
            label = PLATFORM_LABELS.get(link.platform, link.platform.value)
            link_stats.append(
                f"  {emoji} {label} (<code>{link.code}</code>): "
                f"{link_users} рег. / {link_purchases} покуп. / {web_orders} веб-заказ."
            )

    status = "🟢 Активен" if partner.is_active else "🔴 Неактивен"
    valid = f"\nДействует до: <b>{partner.valid_until.strftime('%d.%m.%Y')}</b>" if partner.valid_until else ""

    # Purchases from partner users
    async with async_session() as session:
        from bot.models import Payment, PaymentStatus
        purchase_count = await session.scalar(
            select(func.count(PartnerEarning.id))
            .join(Payment, Payment.id == PartnerEarning.payment_id)
            .where(
                PartnerEarning.partner_id == partner.id,
                Payment.status == PaymentStatus.COMPLETED,
            )
        ) or 0
        purchase_rows = (
            await session.execute(
                select(Payment.amount, Payment.currency)
                .join(PartnerEarning, PartnerEarning.payment_id == Payment.id)
                .where(
                    PartnerEarning.partner_id == partner.id,
                    Payment.status == PaymentStatus.COMPLETED,
                )
            )
        ).all()
        purchase_sum = sum(
            (amount / 100 if currency == "RUB" else amount)
            for amount, currency in purchase_rows
        )

    conversion = f"{purchase_count / user_count * 100:.1f}%" if user_count > 0 else "—"

    text = (
        f"🤝 <b>{partner.name}</b>\n"
        f"Статус: {status}{valid}\n\n"
        f"📊 <b>Аналитика:</b>\n"
        f"  Пользователей: <b>{user_count}</b>\n"
        f"  Покупок: <b>{purchase_count}</b> на <b>{purchase_sum:.0f}₽</b>\n"
        f"  Конверсия: <b>{conversion}</b>\n"
        f"  Telegram начислено: <b>{tg_earned:.0f}₽</b>\n"
        f"  Веб-заказов: <b>{web_orders_total}</b>\n"
        f"  Веб начислено: <b>{web_earned:.0f}₽</b>\n"
        f"  Всего начислено: <b>{total_earned:.0f}₽</b>\n"
        f"  Баланс: <b>{partner.partner_balance:.0f}₽</b>\n\n"
        f"📅 <b>Периоды:</b>\n"
        f"  7 дней: <b>{period_stats['regs_7']}</b> рег. / "
        f"<b>{period_stats['pays_7']}</b> покуп. / "
        f"<b>{period_stats['earn_7']:.0f}₽</b> TG / "
        f"<b>{period_stats['web_orders_7']}</b> веб / "
        f"<b>{period_stats['web_earn_7']:.0f}₽</b>\n"
        f"  30 дней: <b>{period_stats['regs_30']}</b> рег. / "
        f"<b>{period_stats['pays_30']}</b> покуп. / "
        f"<b>{period_stats['earn_30']:.0f}₽</b> TG / "
        f"<b>{period_stats['web_orders_30']}</b> веб / "
        f"<b>{period_stats['web_earn_30']:.0f}₽</b>\n\n"
        f"⚙️ <b>Настройки:</b>\n"
        f"  Комиссия: <b>{partner.commission_percent:.0f}%</b>\n"
        f"  Скидка для аудитории: <b>{partner.audience_discount_percent:.0f}%</b>\n"
        f"  Бонусные дни: <b>{partner.audience_bonus_days}</b>\n"
        f"  Выводы: <b>{'включены' if partner.payouts_enabled else 'выключены'}</b>\n"
        f"  Мин. выплата: <b>{partner.min_payout:.0f}₽</b>\n"
    )
    if partner.telegram_id:
        text += f"  Telegram ID: <code>{partner.telegram_id}</code>\n"
    if pending_payout_sum:
        text += f"  Ожидает выплаты: <b>{pending_payout_sum:.0f}₽</b>\n"
    if partner.contact_info:
        text += f"  Контакт: {partner.contact_info}\n"
    if link_stats:
        text += f"\n🔗 <b>Ссылки:</b>\n" + "\n".join(link_stats) + "\n"

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📈 Аналитика", callback_data=f"adm_pt_an_{partner.id}"),
        InlineKeyboardButton(text="✏️ Настройки", callback_data=f"adm_pt_edit_{partner.id}"),
        InlineKeyboardButton(text="🔗 Ссылки", callback_data=f"adm_pt_links_{partner.id}"),
    )
    builder.row(
        InlineKeyboardButton(text="🎁 Выдать доступ", callback_data=f"adm_pt_free_{partner.id}"),
        InlineKeyboardButton(text="💸 Выплаты", callback_data=f"adm_pt_pay_{partner.id}"),
    )
    payouts_toggle_text = "🚫 Отключить выводы" if partner.payouts_enabled else "💸 Включить выводы"
    toggle_text = "🔴 Деактивировать" if partner.is_active else "🟢 Активировать"
    builder.row(
        InlineKeyboardButton(text=payouts_toggle_text, callback_data=f"adm_pt_payouts_toggle_{partner.id}"),
        InlineKeyboardButton(text=toggle_text, callback_data=f"adm_pt_toggle_{partner.id}"),
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"adm_pt_del_{partner.id}"),
    )
    builder.row(InlineKeyboardButton(text="◀️ К партнёрам", callback_data="adm_partners"))

    if edit:
        try:
            await message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        except Exception:
            await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")


@router.callback_query(F.data.regexp(r"^adm_pt_(\d+)$"))
async def admin_partner_detail(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await state.clear()
    partner_id = int(callback.data.split("_")[-1])
    await _show_partner_detail(callback.message, partner_id)
    await callback.answer()


# ── Admin: Toggle Active ─────────────────────────────

@router.callback_query(F.data.startswith("adm_pt_toggle_"))
async def admin_partner_toggle(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    partner_id = int(callback.data.removeprefix("adm_pt_toggle_"))
    async with async_session() as session:
        partner = await session.get(Partner, partner_id)
        if partner:
            partner.is_active = not partner.is_active
            await session.commit()
    await _show_partner_detail(callback.message, partner_id)
    await callback.answer()


@router.callback_query(F.data.startswith("adm_pt_payouts_toggle_"))
async def admin_partner_payouts_toggle(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    partner_id = int(callback.data.removeprefix("adm_pt_payouts_toggle_"))
    async with async_session() as session:
        partner = await session.get(Partner, partner_id)
        if partner:
            partner.payouts_enabled = not bool(partner.payouts_enabled)
            await session.commit()
    await _show_partner_detail(callback.message, partner_id)
    await callback.answer("Выводы обновлены")


# ── Admin: Delete Partner ─────────────────────────────

@router.callback_query(F.data.startswith("adm_pt_del_"))
async def admin_partner_delete(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    partner_id = int(callback.data.removeprefix("adm_pt_del_"))
    async with async_session() as session:
        partner = await session.get(Partner, partner_id)
        if partner:
            # Delete links and earnings
            for link in partner.links:
                await session.delete(link)
            earnings = (await session.execute(
                select(PartnerEarning).where(PartnerEarning.partner_id == partner_id)
            )).scalars().all()
            for e in earnings:
                await session.delete(e)
            web_earnings = (await session.execute(
                select(WebPartnerEarning).where(WebPartnerEarning.partner_id == partner_id)
            )).scalars().all()
            for e in web_earnings:
                await session.delete(e)
            payouts = (await session.execute(
                select(PartnerPayout).where(PartnerPayout.partner_id == partner_id)
            )).scalars().all()
            for payout in payouts:
                await session.delete(payout)
            await session.delete(partner)
            await session.commit()
    await callback.answer("Партнёр удалён", show_alert=True)
    await admin_partners_list(callback, None)


# ── Admin: Create Partner ────────────────────────────

@router.callback_query(F.data == "adm_pt_add")
async def admin_partner_add(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await state.set_state(PartnerAdminStates.create_target)
    await callback.message.edit_text(
        "➕ <b>Новый партнёр</b>\n\n"
        "Введите <b>Telegram ID</b> или <b>@username</b> пользователя.\n\n"
        "Если пользователь уже есть в базе бота, имя подтянется автоматически.",
        reply_markup=_back_partners_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(PartnerAdminStates.create_target)
async def partner_create_target(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    async with async_session() as session:
        tg_id, name, contact_info = await _resolve_partner_identity(session, raw)
        if tg_id is None or not name:
            await message.answer(
                "Введите существующий <b>@username</b> пользователя из базы бота или числовой Telegram ID.",
                reply_markup=_back_partners_kb(),
                parse_mode="HTML",
            )
            return
        existing_partner = await session.scalar(
            select(Partner).where(Partner.telegram_id == tg_id)
        )
        if existing_partner:
            await message.answer(
                f"Партнёр с Telegram ID <code>{tg_id}</code> уже существует.",
                reply_markup=_back_partners_kb(),
                parse_mode="HTML",
            )
            return

    await state.update_data(pt_name=name, pt_tg_id=tg_id, pt_contact_info=contact_info)
    await state.set_state(PartnerAdminStates.create_commission)
    await message.answer(
        f"Пользователь: <b>{name}</b>\n"
        f"Telegram ID: <code>{tg_id}</code>\n\n"
        "Введите процент комиссии от продаж (0-100):",
        reply_markup=_back_partners_kb(),
        parse_mode="HTML",
    )


@router.message(PartnerAdminStates.create_telegram_id)
async def partner_create_tg_id_legacy(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    await message.answer(
        "Этот шаг больше не используется. Начните создание партнёра заново через админку.",
        reply_markup=_back_partners_kb(),
        parse_mode="HTML",
    )


@router.message(PartnerAdminStates.create_commission)
async def partner_create_commission(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    try:
        commission = float((message.text or "").strip().replace(",", "."))
        if not (0 <= commission <= 100):
            raise ValueError
    except ValueError:
        await message.answer("Введите число от 0 до 100:", reply_markup=_back_partners_kb())
        return

    data = await state.get_data()
    await state.clear()

    async with async_session() as session:
        partner = Partner(
            name=data["pt_name"],
            telegram_id=data.get("pt_tg_id"),
            contact_info=data.get("pt_contact_info"),
            commission_percent=commission,
            payouts_enabled=False,
        )
        session.add(partner)
        await session.commit()
        partner_id = partner.id

    await message.answer(f"✅ Партнёр <b>{data['pt_name']}</b> создан!", parse_mode="HTML")
    await _show_partner_detail(message, partner_id, edit=False)


async def _render_admin_partner_analytics(message, partner_id: int, edit: bool = True) -> None:
    from bot.models import Payment, PaymentStatus

    async with async_session() as session:
        partner = await session.get(Partner, partner_id)
        if not partner:
            text = "Партнёр не найден."
            if edit:
                await message.edit_text(text, reply_markup=_back_partners_kb())
            else:
                await message.answer(text, reply_markup=_back_partners_kb())
            return

        user_count = await session.scalar(
            select(func.count(User.id)).where(User.partner_id == partner.id)
        ) or 0
        purchase_count = await session.scalar(
            select(func.count(Payment.id))
            .join(User, Payment.user_id == User.id)
            .where(User.partner_id == partner.id, Payment.status == PaymentStatus.COMPLETED)
        ) or 0
        tg_earned = await _partner_telegram_earnings_total(session, partner.id)
        web_orders_total = await _partner_web_orders_total(session, partner.id)
        web_earned = await _partner_web_earnings_total(session, partner.id)
        total_earned = tg_earned + web_earned
        period_stats = await _partner_period_stats(session, partner.id)

        link_lines = []
        for link in partner.links:
            regs = await session.scalar(
                select(func.count(User.id)).where(User.partner_link_id == link.id)
            ) or 0
            pays = await session.scalar(
                select(func.count(Payment.id))
                .join(User, Payment.user_id == User.id)
                .where(
                    User.partner_link_id == link.id,
                    Payment.status == PaymentStatus.COMPLETED,
                )
            ) or 0
            web_orders = await session.scalar(
                select(func.count(WebPartnerEarning.id)).where(WebPartnerEarning.partner_link_id == link.id)
            ) or 0
            conv = f"{(pays / regs * 100):.1f}%" if regs else "—"
            emoji = PLATFORM_EMOJI.get(link.platform, "🔗")
            label = PLATFORM_LABELS.get(link.platform, link.platform.value)
            web_link = _build_partner_web_link(link.code)
            link_lines.append(
                f"  {emoji} {label} / <code>{link.code}</code>: "
                f"<b>{regs}</b> рег. / <b>{pays}</b> покуп. / <b>{conv}</b> / "
                f"<b>{web_orders}</b> веб"
                + (f"\n  <code>{web_link}</code>" if web_link else "")
            )

    total_conv = f"{(purchase_count / user_count * 100):.1f}%" if user_count else "—"
    text = (
        f"📈 <b>Аналитика партнёра: {partner.name}</b>\n\n"
        f"Telegram: <b>{user_count}</b> рег. / <b>{purchase_count}</b> покуп. / <b>{total_conv}</b>\n"
        f"Веб: <b>{web_orders_total}</b> заказ.\n"
        f"Telegram начислено: <b>{tg_earned:.0f}₽</b>\n"
        f"Веб начислено: <b>{web_earned:.0f}₽</b>\n"
        f"Всего начислено: <b>{total_earned:.0f}₽</b>\n\n"
        f"За 7 дней: TG <b>{period_stats['regs_7']}</b> рег. / "
        f"<b>{period_stats['pays_7']}</b> покуп. / "
        f"<b>{period_stats['earn_7']:.0f}₽</b>; "
        f"WEB <b>{period_stats['web_orders_7']}</b> заказ. / <b>{period_stats['web_earn_7']:.0f}₽</b>\n"
        f"За 30 дней: TG <b>{period_stats['regs_30']}</b> рег. / "
        f"<b>{period_stats['pays_30']}</b> покуп. / "
        f"<b>{period_stats['earn_30']:.0f}₽</b>; "
        f"WEB <b>{period_stats['web_orders_30']}</b> заказ. / <b>{period_stats['web_earn_30']:.0f}₽</b>\n"
    )
    if link_lines:
        text += f"\n\n🔗 <b>По ссылкам:</b>\n" + "\n".join(link_lines)

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📤 Экспорт CSV", callback_data=f"adm_pt_export_{partner_id}"),
    )
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data=f"adm_pt_{partner_id}"))
    if edit:
        await message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")


@router.callback_query(F.data.startswith("adm_pt_an_"))
async def admin_partner_analytics(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    partner_id = int(callback.data.removeprefix("adm_pt_an_"))
    await _render_admin_partner_analytics(callback.message, partner_id)
    await callback.answer()


@router.callback_query(F.data.startswith("adm_pt_export_"))
async def admin_partner_export_single(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    partner_id = int(callback.data.removeprefix("adm_pt_export_"))
    async with async_session() as session:
        file_bytes = await _build_single_partner_report_csv(session, partner_id)
        partner = await session.get(Partner, partner_id)
    suffix = partner.name.lower().replace(" ", "_")[:24] if partner else str(partner_id)
    await callback.message.answer_document(
        BufferedInputFile(file_bytes, filename=f"partner_report_{suffix}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"),
        caption="📤 Экспорт аналитики партнёра",
    )
    await callback.answer("CSV сформирован")


# ── Admin: Edit Partner Settings ─────────────────────

EDITABLE_FIELDS = {
    "commission": ("Комиссия %", "commission_percent", float, 0, 100),
    "discount": ("Скидка для аудитории %", "audience_discount_percent", float, 0, 100),
    "bonus": ("Бонусные дни для аудитории", "audience_bonus_days", int, 0, 365),
    "minpay": ("Мин. выплата ₽", "min_payout", float, 0, 1000000),
    "contact": ("Контактная информация", "contact_info", str, 0, 0),
    "welcome": ("Приветственный текст (HTML)", "welcome_text", str, 0, 0),
    "notes": ("Заметки", "notes", str, 0, 0),
    "valid": ("Срок действия (ДД.ММ.ГГГГ или -)", "valid_until", "date", 0, 0),
    "tgid": ("Telegram ID (или -)", "telegram_id", "tgid", 0, 0),
}


@router.callback_query(F.data.startswith("adm_pt_edit_"))
async def admin_partner_edit_menu(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    partner_id = int(callback.data.removeprefix("adm_pt_edit_"))

    builder = InlineKeyboardBuilder()
    for key, (label, *_) in EDITABLE_FIELDS.items():
        builder.row(InlineKeyboardButton(
            text=f"✏️ {label}",
            callback_data=f"adm_ptef_{partner_id}_{key}",
        ))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data=f"adm_pt_{partner_id}"))

    await callback.message.edit_text(
        "⚙️ <b>Редактирование настроек</b>\n\nВыберите параметр:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm_ptef_"))
async def admin_partner_edit_field_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    parts = callback.data.removeprefix("adm_ptef_").split("_", 1)
    partner_id = int(parts[0])
    field_key = parts[1]

    if field_key not in EDITABLE_FIELDS:
        await callback.answer("Неизвестное поле", show_alert=True)
        return

    label = EDITABLE_FIELDS[field_key][0]
    await state.set_state(PartnerAdminStates.edit_field)
    await state.update_data(pt_edit_id=partner_id, pt_edit_field=field_key)

    await callback.message.edit_text(
        f"✏️ Введите новое значение для <b>{label}</b>:",
        reply_markup=_back_partner_kb(partner_id),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(PartnerAdminStates.edit_field)
async def admin_partner_edit_field_save(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    partner_id = data["pt_edit_id"]
    field_key = data["pt_edit_field"]
    label, attr, typ, min_val, max_val = EDITABLE_FIELDS[field_key]
    raw = (message.text or "").strip()

    value = None
    if typ == float:
        try:
            value = float(raw.replace(",", "."))
            if not (min_val <= value <= max_val):
                raise ValueError
        except ValueError:
            await message.answer(f"Введите число от {min_val} до {max_val}:", reply_markup=_back_partner_kb(partner_id))
            return
    elif typ == int:
        try:
            value = int(raw)
            if not (min_val <= value <= max_val):
                raise ValueError
        except ValueError:
            await message.answer(f"Введите целое число от {min_val} до {max_val}:", reply_markup=_back_partner_kb(partner_id))
            return
    elif typ == str:
        value = raw if raw != "-" else None
    elif typ == "date":
        if raw == "-":
            value = None
        else:
            try:
                value = datetime.strptime(raw, "%d.%m.%Y")
            except ValueError:
                await message.answer("Формат: ДД.ММ.ГГГГ или - чтобы убрать:", reply_markup=_back_partner_kb(partner_id))
                return
    elif typ == "tgid":
        if raw == "-":
            value = None
        else:
            try:
                value = int(raw)
            except ValueError:
                await message.answer("Введите Telegram ID или -:", reply_markup=_back_partner_kb(partner_id))
                return

    await state.clear()
    async with async_session() as session:
        partner = await session.get(Partner, partner_id)
        if partner:
            setattr(partner, attr, value)
            await session.commit()

    await message.answer(f"✅ <b>{label}</b> обновлено!", parse_mode="HTML")
    await _show_partner_detail(message, partner_id, edit=False)


# ── Admin: Link Management ───────────────────────────

@router.callback_query(F.data.startswith("adm_pt_links_"))
async def admin_partner_links(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    partner_id = int(callback.data.removeprefix("adm_pt_links_"))

    async with async_session() as session:
        partner = await session.get(Partner, partner_id)
        if not partner:
            await callback.answer("Не найден", show_alert=True)
            return

        lines = []
        for link in partner.links:
            emoji = PLATFORM_EMOJI.get(link.platform, "🔗")
            plat = PLATFORM_LABELS.get(link.platform, link.platform.value)
            status = "🟢" if link.is_active else "🔴"
            user_count = await session.scalar(
                select(func.count(User.id)).where(User.partner_link_id == link.id)
            ) or 0
            tg_purchase_count = await session.scalar(
                select(func.count(PartnerEarning.id))
                .join(User, PartnerEarning.user_id == User.id)
                .where(User.partner_link_id == link.id)
            ) or 0
            web_orders_count = await session.scalar(
                select(func.count(WebPartnerEarning.id)).where(WebPartnerEarning.partner_link_id == link.id)
            ) or 0
            bot_info = await callback.bot.get_me()
            deep_link = f"https://t.me/{bot_info.username}?start=p_{link.code}"
            web_link = _build_partner_web_link(link.code)
            lines.append(
                f"{status} {emoji} <b>{plat}</b>\n"
                f"  Код: <code>{link.code}</code>\n"
                f"  Telegram: <code>{deep_link}</code>\n"
                + (f"  Сайт: <code>{web_link}</code>\n" if web_link else "")
                + f"  Telegram: {user_count} рег. / {tg_purchase_count} оплат.\n"
                + f"  Сайт: {web_orders_count} заказ."
            )

    builder = InlineKeyboardBuilder()
    for p_enum in PartnerPlatform:
        emoji = PLATFORM_EMOJI.get(p_enum, "🔗")
        label = PLATFORM_LABELS.get(p_enum, p_enum.value)
        builder.row(InlineKeyboardButton(
            text=f"➕ {emoji} {label}",
            callback_data=f"adm_ptlp_{partner_id}_{p_enum.value}",
        ))
    # Toggle existing links
    if partner.links:
        for link in partner.links:
            toggle = "🔴" if link.is_active else "🟢"
            plat = PLATFORM_LABELS.get(link.platform, link.platform.value)
            builder.row(InlineKeyboardButton(
                text=f"{toggle} {plat} ({link.code})",
                callback_data=f"adm_ptlt_{link.id}",
            ))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data=f"adm_pt_{partner_id}"))

    text = f"🔗 <b>Ссылки: {partner.name}</b>\n\n"
    text += "\n\n".join(lines) if lines else "Нет ссылок. Добавьте для нужных платформ."

    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("adm_ptlp_"))
async def admin_partner_link_platform(callback: CallbackQuery, state: FSMContext) -> None:
    """Admin selected platform for new link — ask for code."""
    if not _is_admin(callback.from_user.id):
        return
    parts = callback.data.removeprefix("adm_ptlp_").split("_", 1)
    partner_id = int(parts[0])
    platform = parts[1]

    async with async_session() as session:
        partner = await session.get(Partner, partner_id)
        if not partner:
            await callback.answer("Не найден", show_alert=True)
            return
        # Suggest code
        safe_name = re.sub(r"[^a-zA-Z0-9]", "", partner.name.lower())[:20] or "partner"
        suggested = f"{safe_name}_{platform[:2]}"

    await state.set_state(PartnerAdminStates.link_code)
    await state.update_data(pt_link_partner=partner_id, pt_link_platform=platform)
    await callback.message.edit_text(
        _partner_link_prompt(suggested),
        reply_markup=_back_partner_kb(partner_id),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(PartnerAdminStates.link_code)
async def admin_partner_link_save(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    code = (message.text or "").strip().lower()
    if not re.match(r"^[a-z0-9_]{2,60}$", code):
        await message.answer("Код: 2-60 символов, латиница/цифры/_. Попробуйте ещё:")
        return

    data = await state.get_data()
    partner_id = data["pt_link_partner"]
    platform = data["pt_link_platform"]

    async with async_session() as session:
        # Check uniqueness
        existing = await session.scalar(
            select(func.count(PartnerLink.id)).where(PartnerLink.code == code)
        )
        if existing:
            await message.answer(f"Код <code>{code}</code> уже занят. Введите другой:", parse_mode="HTML")
            return

        link = PartnerLink(
            partner_id=partner_id,
            code=code,
            platform=PartnerPlatform(platform),
        )
        session.add(link)
        await session.commit()

    await state.clear()
    bot_info = await message.bot.get_me()
    deep_link = f"https://t.me/{bot_info.username}?start=p_{code}"
    text = (
        f"✅ Ссылка создана!\n\n"
        f"Код: <code>{code}</code>\n"
        f"Telegram: <code>{deep_link}</code>"
    )
    web_link = _build_partner_web_link(code)
    if web_link:
        text += f"\nСайт: <code>{web_link}</code>"
    await message.answer(text, reply_markup=_back_partner_kb(partner_id), parse_mode="HTML")


@router.callback_query(F.data.startswith("adm_ptlt_"))
async def admin_partner_link_toggle(callback: CallbackQuery) -> None:
    """Toggle link active/inactive."""
    if not _is_admin(callback.from_user.id):
        return
    link_id = int(callback.data.removeprefix("adm_ptlt_"))
    async with async_session() as session:
        link = await session.get(PartnerLink, link_id)
        if link:
            link.is_active = not link.is_active
            partner_id = link.partner_id
            await session.commit()
        else:
            await callback.answer("Не найдена", show_alert=True)
            return
    # Refresh links page
    callback.data = f"adm_pt_links_{partner_id}"
    await admin_partner_links(callback)


# ── Admin: Free Access for Partner ───────────────────

@router.callback_query(F.data.startswith("adm_pt_free_"))
async def admin_partner_free_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    partner_id = int(callback.data.removeprefix("adm_pt_free_"))

    async with async_session() as session:
        partner = await session.get(Partner, partner_id)
        if not partner or not partner.telegram_id:
            await callback.answer("У партнёра не указан Telegram ID", show_alert=True)
            return

    await state.set_state(PartnerAdminStates.free_months)
    await state.update_data(pt_free_id=partner_id)
    await callback.message.edit_text(
        "🎁 <b>Бесплатный доступ для партнёра</b>\n\n"
        "Введите количество месяцев (1-24):",
        reply_markup=_back_partner_kb(partner_id),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(PartnerAdminStates.free_months)
async def admin_partner_free_give(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    try:
        months = int((message.text or "").strip())
        if not (1 <= months <= 24):
            raise ValueError
    except ValueError:
        await message.answer("Введите число от 1 до 24:")
        return

    data = await state.get_data()
    partner_id = data["pt_free_id"]
    await state.clear()

    from datetime import timedelta
    from bot.models import Platform, Server, Subscription
    from bot.services import vpn_manager
    from bot.services.client_names import build_client_name
    from bot.services.device_slots import get_included_device_slots

    async with async_session() as session:
        partner = await session.get(Partner, partner_id)
        if not partner or not partner.telegram_id:
            await message.answer("Партнёр не найден или нет Telegram ID.", reply_markup=_back_partners_kb())
            return

        # Find user by telegram_id
        result = await session.execute(
            select(User).where(User.telegram_id == partner.telegram_id)
        )
        user = result.scalar_one_or_none()
        if not user:
            await message.answer("Пользователь не найден в боте. Партнёр должен сначала нажать /start.", reply_markup=_back_partner_kb(partner_id))
            return

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
            await message.answer("Нет активных серверов.", reply_markup=_back_partner_kb(partner_id))
            return

        expires_at = datetime.utcnow() + timedelta(days=months * 30)
        client_name = build_client_name(partner.telegram_id, slot=1)

        vpn_key = await vpn_manager.generate_key(
            server=server,
            client_name=client_name,
            expire=int(expires_at.timestamp()),
        )
        if not vpn_key or vpn_key.startswith("Error"):
            await message.answer(f"Ошибка генерации ключа: {vpn_key}", reply_markup=_back_partner_kb(partner_id))
            return

        included_slots = await get_included_device_slots(session)
        subscription = Subscription(
            user_id=user.id,
            server_id=server.id,
            tariff_months=months,
            tariff_days=months * 30,
            vpn_key=vpn_key,
            client_name=client_name,
            platform=Platform.ANDROID,
            device_slots=included_slots,
            expires_at=expires_at,
        )
        session.add(subscription)
        server.current_clients += 1
        await session.commit()

    # Notify partner
    try:
        await message.bot.send_message(
            partner.telegram_id,
            f"🎁 <b>Вам выдан бесплатный доступ на {months} мес.!</b>\n\n"
            f"Ссылка на подписку:\n<code>{vpn_key}</code>\n"
            f"<i>Нажмите чтобы скопировать.</i>\n\n"
            f"Действует до: <b>{expires_at.strftime('%d.%m.%Y')}</b>",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(f"Failed to notify partner {partner.telegram_id}: {e}")

    await message.answer(
        f"✅ Доступ на <b>{months} мес.</b> выдан партнёру <b>{partner.name}</b>!",
        parse_mode="HTML",
    )
    await _show_partner_detail(message, partner_id, edit=False)


async def _render_admin_payout_list(message, partner_id: int | None = None, edit: bool = True) -> None:
    async with async_session() as session:
        query = select(PartnerPayout).order_by(
            PartnerPayout.status.asc(),
            PartnerPayout.requested_at.desc(),
        )
        if partner_id is not None:
            query = query.where(PartnerPayout.partner_id == partner_id)
            partner = await session.get(Partner, partner_id)
            title = f"💸 <b>Выплаты: {partner.name if partner else f'#{partner_id}'}</b>\n\n"
            back_markup = _back_partner_kb(partner_id)
        else:
            title = "💸 <b>Заявки на выплаты</b>\n\n"
            back_markup = _back_partners_kb()

        payouts = (await session.execute(query.limit(20))).scalars().all()
        if not payouts:
            text = title + "Заявок пока нет."
            if edit:
                await message.edit_text(text, reply_markup=back_markup, parse_mode="HTML")
            else:
                await message.answer(text, reply_markup=back_markup, parse_mode="HTML")
            return

        builder = InlineKeyboardBuilder()
        lines = []
        for payout in payouts:
            partner = await session.get(Partner, payout.partner_id)
            status = _format_payout_status(payout.status)
            builder.row(InlineKeyboardButton(
                text=f"{status} • {partner.name if partner else payout.partner_id} • {payout.amount:.0f}₽",
                callback_data=f"adm_ptpo_{payout.id}",
            ))
            lines.append(
                f"• <b>{partner.name if partner else payout.partner_id}</b>: "
                f"{payout.amount:.0f}₽ - {status}"
            )
        export_cb = "adm_partner_payouts_export" if partner_id is None else f"adm_partner_payouts_export_{partner_id}"
        builder.row(InlineKeyboardButton(text="📤 Экспорт выплат CSV", callback_data=export_cb))
        builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="adm_partners" if partner_id is None else f"adm_pt_{partner_id}"))
        text = title + "\n".join(lines)
        if edit:
            await message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        else:
            await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")


@router.callback_query(F.data == "adm_partners_export")
async def admin_partners_export(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    async with async_session() as session:
        file_bytes = await _build_partners_csv(session)
    await callback.message.answer_document(
        BufferedInputFile(file_bytes, filename=f"partners_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"),
        caption="📤 Экспорт партнёров",
    )
    await callback.answer("CSV сформирован")


async def _render_admin_payout_detail(message, payout_id: int, edit: bool = True) -> None:
    async with async_session() as session:
        payout = await session.get(PartnerPayout, payout_id)
        if not payout:
            if edit:
                await message.edit_text("Заявка не найдена.", reply_markup=_back_payouts_kb())
            else:
                await message.answer("Заявка не найдена.", reply_markup=_back_payouts_kb())
            return
        partner = await session.get(Partner, payout.partner_id)
        status = _format_payout_status(payout.status)
        processed_at = payout.processed_at.strftime("%d.%m.%Y %H:%M") if payout.processed_at else "—"
        text = (
            f"💸 <b>Заявка #{payout.id}</b>\n\n"
            f"Партнёр: <b>{partner.name if partner else payout.partner_id}</b>\n"
            f"Сумма: <b>{payout.amount:.2f}₽</b>\n"
            f"Статус: <b>{status}</b>\n"
            f"Создана: <b>{payout.requested_at.strftime('%d.%m.%Y %H:%M')}</b>\n"
            f"Обработана: <b>{processed_at}</b>\n"
        )
        if payout.details:
            text += f"\nРеквизиты / комментарий:\n<code>{payout.details}</code>\n"
        if payout.admin_comment:
            text += f"\nКомментарий администратора:\n{payout.admin_comment}\n"

        builder = InlineKeyboardBuilder()
        if payout.status == PartnerPayoutStatus.PENDING:
            builder.row(
                InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"adm_ptpay_ok_{payout.id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm_ptpay_rej_{payout.id}"),
            )
        builder.row(InlineKeyboardButton(text="◀️ К заявкам", callback_data="adm_partner_payouts"))

    if edit:
        await message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")


@router.callback_query(F.data == "adm_partner_payouts")
async def admin_partner_payouts(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await _render_admin_payout_list(callback.message)
    await callback.answer()


@router.callback_query(F.data == "adm_partner_payouts_export")
async def admin_partner_payouts_export(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    async with async_session() as session:
        file_bytes = await _build_partner_payouts_csv(session)
    await callback.message.answer_document(
        BufferedInputFile(file_bytes, filename=f"partner_payouts_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"),
        caption="📤 Экспорт заявок на выплаты",
    )
    await callback.answer("CSV сформирован")


@router.callback_query(F.data.regexp(r"^adm_partner_payouts_export_(\d+)$"))
async def admin_partner_payouts_export_for_partner(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    partner_id = int(callback.data.rsplit("_", 1)[-1])
    async with async_session() as session:
        file_bytes = await _build_partner_payouts_csv(session, partner_id=partner_id)
        partner = await session.get(Partner, partner_id)
    suffix = partner.name.lower().replace(" ", "_")[:24] if partner else str(partner_id)
    await callback.message.answer_document(
        BufferedInputFile(file_bytes, filename=f"partner_payouts_{suffix}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"),
        caption="📤 Экспорт выплат партнёра",
    )
    await callback.answer("CSV сформирован")


@router.callback_query(F.data.startswith("adm_pt_pay_"))
async def admin_partner_payouts_for_partner(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    partner_id = int(callback.data.removeprefix("adm_pt_pay_"))
    await _render_admin_payout_list(callback.message, partner_id=partner_id)
    await callback.answer()


@router.callback_query(F.data.startswith("adm_ptpo_"))
async def admin_partner_payout_detail(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    payout_id = int(callback.data.removeprefix("adm_ptpo_"))
    await _render_admin_payout_detail(callback.message, payout_id)
    await callback.answer()


@router.callback_query(F.data.startswith("adm_ptpay_ok_"))
async def admin_partner_payout_approve(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    payout_id = int(callback.data.removeprefix("adm_ptpay_ok_"))
    async with async_session() as session:
        payout = await session.get(PartnerPayout, payout_id)
        if not payout or payout.status != PartnerPayoutStatus.PENDING:
            await callback.answer("Заявка уже обработана", show_alert=True)
            return
        partner = await session.get(Partner, payout.partner_id)
        if not partner or (partner.partner_balance or 0.0) < payout.amount:
            await callback.answer("Недостаточно баланса у партнёра", show_alert=True)
            return
        partner.partner_balance = round((partner.partner_balance or 0.0) - payout.amount, 2)
        payout.status = PartnerPayoutStatus.APPROVED
        payout.processed_at = datetime.utcnow()
        payout.processed_by = callback.from_user.id
        await session.commit()

        partner_name = partner.name
        partner_tg = partner.telegram_id

    if partner_tg:
        try:
            await callback.bot.send_message(
                partner_tg,
                f"✅ Ваша заявка на выплату <b>{payout.amount:.2f}₽</b> подтверждена.",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning("Failed to notify partner %s about approved payout: %s", partner_name, e)
    await _render_admin_payout_detail(callback.message, payout_id)
    await callback.answer("Заявка подтверждена", show_alert=True)


@router.callback_query(F.data.startswith("adm_ptpay_rej_"))
async def admin_partner_payout_reject_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    payout_id = int(callback.data.removeprefix("adm_ptpay_rej_"))
    await state.set_state(PartnerPayoutStates.reject_comment)
    await state.update_data(pt_reject_payout_id=payout_id)
    await callback.message.edit_text(
        "❌ <b>Отклонение заявки</b>\n\n"
        "Введите комментарий для партнёра или <b>-</b> без комментария:",
        reply_markup=_back_payouts_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(PartnerPayoutStates.reject_comment)
async def admin_partner_payout_reject_save(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    payout_id = data["pt_reject_payout_id"]
    comment = (message.text or "").strip()
    if comment == "-":
        comment = None
    await state.clear()

    async with async_session() as session:
        payout = await session.get(PartnerPayout, payout_id)
        if not payout or payout.status != PartnerPayoutStatus.PENDING:
            await message.answer("Заявка уже обработана.", reply_markup=_back_payouts_kb())
            return
        payout.status = PartnerPayoutStatus.REJECTED
        payout.admin_comment = comment
        payout.processed_at = datetime.utcnow()
        payout.processed_by = message.from_user.id
        partner = await session.get(Partner, payout.partner_id)
        await session.commit()

        partner_tg = partner.telegram_id if partner else None
        amount = payout.amount

    if partner_tg:
        try:
            text = f"❌ Ваша заявка на выплату <b>{amount:.2f}₽</b> отклонена."
            if comment:
                text += f"\n\nКомментарий: {comment}"
            await message.bot.send_message(partner_tg, text, parse_mode="HTML")
        except Exception as e:
            logger.warning("Failed to notify partner %s about rejected payout: %s", partner_tg, e)

    await message.answer("✅ Заявка отклонена.")
    await _render_admin_payout_detail(message, payout_id, edit=False)


# ── Partner Self-Service Dashboard ───────────────────

@router.callback_query(F.data == "partner_dashboard")
async def partner_dashboard(callback: CallbackQuery) -> None:
    """Partner sees their own stats."""
    async with async_session() as session:
        from bot.models import Payment, PaymentStatus

        result = await session.execute(
            select(Partner).where(Partner.telegram_id == callback.from_user.id)
        )
        partner = result.scalar_one_or_none()
        if not partner:
            await callback.answer("Вы не являетесь партнёром", show_alert=True)
            return

        user_count = await session.scalar(
            select(func.count(User.id)).where(User.partner_id == partner.id)
        ) or 0

        tg_earned = await _partner_telegram_earnings_total(session, partner.id)
        web_orders_total = await _partner_web_orders_total(session, partner.id)
        web_earned = await _partner_web_earnings_total(session, partner.id)
        total_earned = tg_earned + web_earned

        purchase_count = await session.scalar(
            select(func.count(Payment.id))
            .join(User, Payment.user_id == User.id)
            .where(User.partner_id == partner.id, Payment.status == PaymentStatus.COMPLETED)
        ) or 0
        period_stats = await _partner_period_stats(session, partner.id)
        pending_payout = await session.scalar(
            select(func.count(PartnerPayout.id)).where(
                PartnerPayout.partner_id == partner.id,
                PartnerPayout.status == PartnerPayoutStatus.PENDING,
            )
        ) or 0
        recent_payouts = (await session.execute(
            select(PartnerPayout)
            .where(PartnerPayout.partner_id == partner.id)
            .order_by(PartnerPayout.requested_at.desc())
            .limit(5)
        )).scalars().all()

        # Per-link breakdown
        link_lines = []
        for link in partner.links:
            if not link.is_active:
                continue
            emoji = PLATFORM_EMOJI.get(link.platform, "🔗")
            plat = PLATFORM_LABELS.get(link.platform, link.platform.value)
            link_users = await session.scalar(
                select(func.count(User.id)).where(User.partner_link_id == link.id)
            ) or 0
            link_purchases = await session.scalar(
                select(func.count(Payment.id))
                .join(User, Payment.user_id == User.id)
                .where(
                    User.partner_link_id == link.id,
                    Payment.status == PaymentStatus.COMPLETED,
                )
            ) or 0
            web_orders = await session.scalar(
                select(func.count(WebPartnerEarning.id)).where(WebPartnerEarning.partner_link_id == link.id)
            ) or 0
            web_link = _build_partner_web_link(link.code)
            link_lines.append(
                f"  {emoji} {plat}: TG <b>{link_users}</b> рег. / <b>{link_purchases}</b> покуп. / "
                f"WEB <b>{web_orders}</b> заказ."
                + (f"\n  <code>{web_link}</code>" if web_link else "")
            )

        # Multi-level referral chain stats
        chain_rows = (await session.execute(
            select(User.referral_depth, func.count(User.id))
            .where(
                User.referral_root_partner_id == partner.id,
                User.referral_depth > 1,  # depth 1 = direct (already in user_count)
            )
            .group_by(User.referral_depth)
            .order_by(User.referral_depth)
        )).all()
        chain_total = sum(cnt for _, cnt in chain_rows)

    conversion = f"{purchase_count / user_count * 100:.1f}%" if user_count > 0 else "—"

    text = (
        f"📊 <b>Ваша партнёрская статистика</b>\n\n"
        f"Telegram: <b>{user_count}</b> переход. / <b>{purchase_count}</b> покуп. / <b>{conversion}</b>\n"
        f"Сайт: <b>{web_orders_total}</b> заказ.\n"
        f"Telegram начислено: <b>{tg_earned:.0f}₽</b>\n"
        f"Сайт начислено: <b>{web_earned:.0f}₽</b>\n"
        f"Заработано всего: <b>{total_earned:.0f}₽</b>\n"
        f"Текущий баланс: <b>{partner.partner_balance:.0f}₽</b>\n"
        f"Комиссия: <b>{partner.commission_percent:.0f}%</b>\n"
        f"\n📅 <b>За 7 дней:</b> TG {period_stats['regs_7']} рег. / {period_stats['pays_7']} покуп. / {period_stats['earn_7']:.0f}₽; "
        f"WEB {period_stats['web_orders_7']} заказ. / {period_stats['web_earn_7']:.0f}₽\n"
        f"📅 <b>За 30 дней:</b> TG {period_stats['regs_30']} рег. / {period_stats['pays_30']} покуп. / {period_stats['earn_30']:.0f}₽; "
        f"WEB {period_stats['web_orders_30']} заказ. / {period_stats['web_earn_30']:.0f}₽\n"
    )
    if chain_total > 0:
        depth_lines = " / ".join(f"ур.{depth}: {cnt}" for depth, cnt in chain_rows)
        text += f"\n🔗 <b>По цепочке (приглашённые вашими пользователями):</b> {chain_total} чел.\n{depth_lines}\n"
    if partner.payouts_enabled:
        text += f"Мин. выплата: <b>{partner.min_payout:.0f}₽</b>\n"
    if link_lines:
        text += f"\n🔗 <b>Ваши ссылки:</b>\n" + "\n".join(link_lines)
    if partner.payouts_enabled and recent_payouts:
        payout_lines = [
            f"• {p.amount:.0f}₽ - {_format_payout_status(p.status)}"
            for p in recent_payouts
        ]
        text += f"\n\n💸 <b>Последние заявки:</b>\n" + "\n".join(payout_lines)

    kb = _partner_dashboard_kb(has_pending=bool(pending_payout), payouts_enabled=bool(partner.payouts_enabled))
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "partner_links_manage")
async def partner_links_manage(callback: CallbackQuery) -> None:
    async with async_session() as session:
        result = await session.execute(
            select(Partner).where(Partner.telegram_id == callback.from_user.id)
        )
        partner = result.scalar_one_or_none()
        if not partner:
            await callback.answer("Вы не являетесь партнёром", show_alert=True)
            return
    await _render_partner_links_manage(callback.message, partner.id, callback.bot)
    await callback.answer()


@router.callback_query(F.data.startswith("partner_lp_"))
async def partner_link_platform(callback: CallbackQuery, state: FSMContext) -> None:
    platform = callback.data.removeprefix("partner_lp_")
    async with async_session() as session:
        result = await session.execute(
            select(Partner).where(Partner.telegram_id == callback.from_user.id)
        )
        partner = result.scalar_one_or_none()
        if not partner:
            await callback.answer("Вы не являетесь партнёром", show_alert=True)
            return
        safe_name = re.sub(r"[^a-zA-Z0-9]", "", partner.name.lower())[:20] or "partner"
        suggested = f"{safe_name}_{platform[:2]}"

    await state.set_state(PartnerSelfStates.link_code)
    await state.update_data(pt_self_link_platform=platform)
    await callback.message.edit_text(
        _partner_self_link_prompt(PLATFORM_LABELS.get(PartnerPlatform(platform), platform), suggested),
        reply_markup=_back_partner_links_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(PartnerSelfStates.link_code)
async def partner_link_save(message: Message, state: FSMContext) -> None:
    code = (message.text or "").strip().lower()
    if not re.match(r"^[a-z0-9_]{2,60}$", code):
        await message.answer("Код: 2-60 символов, латиница/цифры/_. Попробуйте ещё:", reply_markup=_back_partner_links_kb())
        return

    async with async_session() as session:
        result = await session.execute(
            select(Partner).where(Partner.telegram_id == message.from_user.id)
        )
        partner = result.scalar_one_or_none()
        if not partner:
            await state.clear()
            await message.answer("Вы не являетесь партнёром.", reply_markup=_back_main_kb())
            return

        data = await state.get_data()
        platform = data["pt_self_link_platform"]
        existing = await session.scalar(
            select(func.count(PartnerLink.id)).where(PartnerLink.code == code)
        )
        if existing:
            await message.answer(f"Код <code>{code}</code> уже занят. Введите другой:", parse_mode="HTML", reply_markup=_back_partner_links_kb())
            return

        link = PartnerLink(
            partner_id=partner.id,
            code=code,
            platform=PartnerPlatform(platform),
        )
        session.add(link)
        await session.commit()

    await state.clear()
    web_link = _build_partner_web_link(code)
    text = f"✅ Ссылка создана!\n\nКод: <code>{code}</code>"
    if web_link:
        text += "\nНиже отправляю отдельным сообщением вашу реферальную ссылку."
    await message.answer(text, reply_markup=_back_partner_links_kb(), parse_mode="HTML")
    if web_link:
        await message.answer(web_link)


@router.callback_query(F.data.startswith("partner_lt_"))
async def partner_link_toggle(callback: CallbackQuery) -> None:
    link_id = int(callback.data.removeprefix("partner_lt_"))
    async with async_session() as session:
        result = await session.execute(
            select(Partner).where(Partner.telegram_id == callback.from_user.id)
        )
        partner = result.scalar_one_or_none()
        if not partner:
            await callback.answer("Вы не являетесь партнёром", show_alert=True)
            return
        link = await session.get(PartnerLink, link_id)
        if not link or link.partner_id != partner.id:
            await callback.answer("Ссылка не найдена", show_alert=True)
            return
        link.is_active = not link.is_active
        await session.commit()
        partner_id = partner.id

    await _render_partner_links_manage(callback.message, partner_id, callback.bot)
    await callback.answer()


@router.callback_query(F.data == "partner_apply_start")
async def partner_apply_start(callback: CallbackQuery, state: FSMContext) -> None:
    async with async_session() as session:
        partner = await session.scalar(
            select(Partner).where(Partner.telegram_id == callback.from_user.id)
        )
        if partner:
            await callback.answer("У вас уже есть партнёрский кабинет", show_alert=True)
            return
        pending = await session.scalar(
            select(func.count(PartnerApplication.id)).where(
                PartnerApplication.telegram_id == callback.from_user.id,
                PartnerApplication.status == PartnerApplicationStatus.PENDING,
            )
        ) or 0
        if pending:
            await callback.answer("Заявка уже отправлена и ждет рассмотрения", show_alert=True)
            return

    await state.set_state(PartnerApplicationStates.details)
    await callback.message.edit_text(
        "🤝 <b>Заявка в партнёрскую программу</b>\n\n"
        "Опишите свои площадки и аудиторию.\n"
        "Например: ссылки на соцсети, тематика, размер аудитории, ожидаемый трафик, контакт для связи.\n\n"
        "Одним сообщением.",
        reply_markup=_back_main_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(PartnerApplicationStates.details)
async def partner_apply_save(message: Message, state: FSMContext) -> None:
    details = (message.text or "").strip()
    if len(details) < 10:
        await message.answer("Опишите заявку чуть подробнее, минимум 10 символов.", reply_markup=_back_main_kb())
        return

    async with async_session() as session:
        partner = await session.scalar(
            select(Partner).where(Partner.telegram_id == message.from_user.id)
        )
        if partner:
            await state.clear()
            await message.answer("У вас уже есть партнёрский кабинет.", reply_markup=_back_main_kb())
            return
        pending = await session.scalar(
            select(func.count(PartnerApplication.id)).where(
                PartnerApplication.telegram_id == message.from_user.id,
                PartnerApplication.status == PartnerApplicationStatus.PENDING,
            )
        ) or 0
        if pending:
            await state.clear()
            await message.answer("Заявка уже отправлена и ждет рассмотрения.", reply_markup=_back_main_kb())
            return

        application = PartnerApplication(
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            full_name=message.from_user.full_name or message.from_user.first_name or "",
            notes=details,
            contact_info=f"@{message.from_user.username}" if message.from_user.username else None,
        )
        session.add(application)
        await session.commit()
        await session.refresh(application)

    await state.clear()

    user_label = f"@{message.from_user.username}" if message.from_user.username else str(message.from_user.id)
    admin_text = (
        f"🤝 <b>Новая заявка в партнёрку</b>\n\n"
        f"Заявка: <b>#{application.id}</b>\n"
        f"Пользователь: <b>{message.from_user.full_name or message.from_user.first_name}</b>\n"
        f"Telegram: <code>{message.from_user.id}</code>\n"
        f"Username: {user_label}\n\n"
        f"Описание:\n{details}\n\n"
        f"Если подходящий кандидат, создайте партнёра через админку и укажите его Telegram ID."
    )
    for admin_id in settings.admin_ids:
        try:
            await message.bot.send_message(
                admin_id,
                admin_text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Открыть заявку", callback_data=f"adm_ptappo_{application.id}")],
                ]),
            )
        except Exception as e:
            logger.warning("Failed to notify admin %s about partner application: %s", admin_id, e)

    await message.answer(
        "✅ Заявка отправлена. Когда администратор одобрит вас как партнёра, в главном меню появится партнёрский кабинет.",
        reply_markup=_back_main_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "partner_payout_request")
async def partner_payout_request_start(callback: CallbackQuery, state: FSMContext) -> None:
    async with async_session() as session:
        result = await session.execute(
            select(Partner).where(Partner.telegram_id == callback.from_user.id)
        )
        partner = result.scalar_one_or_none()
        if not partner:
            await callback.answer("Вы не являетесь партнёром", show_alert=True)
            return
        if not partner.payouts_enabled:
            await callback.answer("Выводы для вас отключены", show_alert=True)
            return
        pending_exists = await session.scalar(
            select(func.count(PartnerPayout.id)).where(
                PartnerPayout.partner_id == partner.id,
                PartnerPayout.status == PartnerPayoutStatus.PENDING,
            )
        ) or 0
        if pending_exists:
            await callback.answer("У вас уже есть заявка в обработке", show_alert=True)
            return
        if (partner.partner_balance or 0.0) < (partner.min_payout or 0.0):
            await callback.answer(
                f"Минимальная сумма для выплаты: {partner.min_payout:.0f}₽",
                show_alert=True,
            )
            return
        max_amount = round(partner.partner_balance or 0.0, 2)
        min_amount = round(partner.min_payout or 0.0, 2)

    await state.set_state(PartnerPayoutStates.request_amount)
    await callback.message.edit_text(
        "💸 <b>Запрос выплаты</b>\n\n"
        f"Доступно к выплате: <b>{max_amount:.2f}₽</b>\n"
        f"Минимальная сумма: <b>{min_amount:.2f}₽</b>\n\n"
        "Введите сумму выплаты:",
        reply_markup=_back_partner_dashboard_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "partner_export_csv")
async def partner_export_csv(callback: CallbackQuery) -> None:
    async with async_session() as session:
        result = await session.execute(
            select(Partner).where(Partner.telegram_id == callback.from_user.id)
        )
        partner = result.scalar_one_or_none()
        if not partner:
            await callback.answer("Вы не являетесь партнёром", show_alert=True)
            return
        file_bytes = await _build_single_partner_report_csv(session, partner.id)

    suffix = partner.name.lower().replace(" ", "_")[:24]
    await callback.message.answer_document(
        BufferedInputFile(file_bytes, filename=f"partner_report_{suffix}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"),
        caption="📤 Экспорт вашей партнёрской статистики",
    )
    await callback.answer("CSV сформирован")


@router.callback_query(F.data == "partner_earnings_export_csv")
async def partner_earnings_export_csv(callback: CallbackQuery) -> None:
    async with async_session() as session:
        result = await session.execute(
            select(Partner).where(Partner.telegram_id == callback.from_user.id)
        )
        partner = result.scalar_one_or_none()
        if not partner:
            await callback.answer("Вы не являетесь партнёром", show_alert=True)
            return
        file_bytes = await _build_partner_earnings_csv(session, partner.id)

    suffix = partner.name.lower().replace(" ", "_")[:24]
    await callback.message.answer_document(
        BufferedInputFile(file_bytes, filename=f"partner_earnings_{suffix}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"),
        caption="📥 Экспорт истории начислений",
    )
    await callback.answer("CSV начислений сформирован")


@router.message(PartnerPayoutStates.request_amount)
async def partner_payout_request_amount(message: Message, state: FSMContext) -> None:
    async with async_session() as session:
        result = await session.execute(
            select(Partner).where(Partner.telegram_id == message.from_user.id)
        )
        partner = result.scalar_one_or_none()
        if not partner:
            await state.clear()
            return
        if not partner.payouts_enabled:
            await state.clear()
            await message.answer("Выводы для вас отключены.", reply_markup=_back_partner_dashboard_kb())
            return

    try:
        amount = round(float((message.text or "").strip().replace(",", ".")), 2)
    except ValueError:
        await message.answer("Введите корректную сумму в рублях.", reply_markup=_back_partner_dashboard_kb())
        return

    balance = round(partner.partner_balance or 0.0, 2)
    min_payout = round(partner.min_payout or 0.0, 2)
    if amount < min_payout:
        await message.answer(f"Сумма должна быть не меньше {min_payout:.2f}₽.", reply_markup=_back_partner_dashboard_kb())
        return
    if amount > balance:
        await message.answer(f"На балансе только {balance:.2f}₽.", reply_markup=_back_partner_dashboard_kb())
        return

    await state.update_data(pt_payout_amount=amount)
    await state.set_state(PartnerPayoutStates.request_details)
    await message.answer(
        "Введите реквизиты или комментарий для выплаты.\n\n"
        "Например: номер карты, СБП, USDT TRC20, ФИО.\n"
        "Если комментарий не нужен, отправьте <b>-</b>.",
        reply_markup=_back_partner_dashboard_kb(),
        parse_mode="HTML",
    )


@router.message(PartnerPayoutStates.request_details)
async def partner_payout_request_save(message: Message, state: FSMContext) -> None:
    async with async_session() as session:
        result = await session.execute(
            select(Partner).where(Partner.telegram_id == message.from_user.id)
        )
        partner = result.scalar_one_or_none()
        if not partner:
            await state.clear()
            return
        if not partner.payouts_enabled:
            await state.clear()
            await message.answer("Выводы для вас отключены.", reply_markup=_back_partner_dashboard_kb())
            return
        pending_exists = await session.scalar(
            select(func.count(PartnerPayout.id)).where(
                PartnerPayout.partner_id == partner.id,
                PartnerPayout.status == PartnerPayoutStatus.PENDING,
            )
        ) or 0
        if pending_exists:
            await state.clear()
            await message.answer("У вас уже есть заявка в обработке.", reply_markup=_back_partner_dashboard_kb())
            return

        data = await state.get_data()
        amount = round(float(data["pt_payout_amount"]), 2)
        if amount > round(partner.partner_balance or 0.0, 2):
            await state.clear()
            await message.answer("Баланс изменился, попробуйте создать заявку заново.", reply_markup=_back_partner_dashboard_kb())
            return

        details = (message.text or "").strip()
        if details == "-":
            details = None
        payout = PartnerPayout(
            partner_id=partner.id,
            amount=amount,
            details=details,
        )
        session.add(payout)
        await session.commit()
        await session.refresh(payout)
        partner_name = partner.name

    await state.clear()
    for admin_id in settings.admin_ids:
        try:
            admin_text = (
                f"💸 <b>Новая заявка на выплату</b>\n\n"
                f"Партнёр: <b>{partner_name}</b>\n"
                f"Сумма: <b>{amount:.2f}₽</b>\n"
                f"Заявка: <b>#{payout.id}</b>"
            )
            if details:
                admin_text += f"\nРеквизиты:\n<code>{details}</code>"
            await message.bot.send_message(
                admin_id,
                admin_text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Открыть заявку", callback_data=f"adm_ptpo_{payout.id}")],
                ]),
            )
        except Exception as e:
            logger.warning("Failed to notify admin %s about payout request: %s", admin_id, e)

    await message.answer(
        f"✅ Заявка на выплату <b>{amount:.2f}₽</b> отправлена администратору.",
        reply_markup=_back_partner_dashboard_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "partner_earnings_history")
async def partner_earnings_history(callback: CallbackQuery) -> None:
    async with async_session() as session:
        result = await session.execute(
            select(Partner).where(Partner.telegram_id == callback.from_user.id)
        )
        partner = result.scalar_one_or_none()
        if not partner:
            await callback.answer("Вы не являетесь партнёром", show_alert=True)
            return
        tg_earnings = (await session.execute(
            select(PartnerEarning)
            .where(PartnerEarning.partner_id == partner.id)
            .order_by(PartnerEarning.created_at.desc(), PartnerEarning.id.desc())
            .limit(20)
        )).scalars().all()
        web_earnings = (await session.execute(
            select(WebPartnerEarning)
            .where(WebPartnerEarning.partner_id == partner.id)
            .order_by(WebPartnerEarning.created_at.desc(), WebPartnerEarning.id.desc())
            .limit(20)
        )).scalars().all()

        lines = []
        items: list[tuple[datetime, int, str]] = []
        for earning in tg_earnings:
            user = await session.get(User, earning.user_id)
            if user and user.username:
                user_label = f"@{user.username}"
            elif user and user.full_name:
                user_label = user.full_name
            else:
                user_label = f"User #{earning.user_id}"
            items.append((earning.created_at, earning.id, (
                f"• <b>{earning.amount:.2f}₽</b> от {user_label}"
                f"\n  {earning.created_at.strftime('%d.%m.%Y %H:%M')}"
            )))
            if earning.payment_id:
                items[-1] = (items[-1][0], items[-1][1], items[-1][2] + f"\n  Платёж #{earning.payment_id}")
        for earning in web_earnings:
            line = (
                f"• <b>{earning.earning_amount_rub:.2f}₽</b> с сайта"
                f"\n  {earning.created_at.strftime('%d.%m.%Y %H:%M')}"
            )
            if earning.tariff_label:
                line += f"\n  Тариф: {earning.tariff_label}"
            if earning.buyer_contact:
                line += f"\n  Покупатель: {earning.buyer_contact}"
            line += f"\n  Заказ: <code>{earning.web_order_id}</code>"
            items.append((earning.created_at, earning.id, line))
        items.sort(key=lambda item: (item[0], item[1]), reverse=True)
        lines = [item[2] for item in items[:15]]

    if not lines:
        text = "💰 <b>История начислений</b>\n\nНачислений пока не было."
    else:
        text = "💰 <b>История начислений</b>\n\n" + "\n\n".join(lines)

    try:
        await callback.message.edit_text(
            text,
            reply_markup=_back_partner_dashboard_kb(),
            parse_mode="HTML",
        )
    except Exception:
        await callback.message.answer(
            text,
            reply_markup=_back_partner_dashboard_kb(),
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data == "partner_payout_history")
async def partner_payout_history(callback: CallbackQuery) -> None:
    async with async_session() as session:
        result = await session.execute(
            select(Partner).where(Partner.telegram_id == callback.from_user.id)
        )
        partner = result.scalar_one_or_none()
        if not partner:
            await callback.answer("Вы не являетесь партнёром", show_alert=True)
            return
        if not partner.payouts_enabled:
            await callback.answer("Выводы для вас отключены", show_alert=True)
            return
        payouts = (await session.execute(
            select(PartnerPayout)
            .where(PartnerPayout.partner_id == partner.id)
            .order_by(PartnerPayout.requested_at.desc())
            .limit(15)
        )).scalars().all()

    if not payouts:
        text = "💸 <b>История выплат</b>\n\nЗаявок пока не было."
    else:
        lines = []
        for payout in payouts:
            line = (
                f"• #{payout.id} - <b>{payout.amount:.2f}₽</b> - {_format_payout_status(payout.status)}"
                f"\n  {payout.requested_at.strftime('%d.%m.%Y %H:%M')}"
            )
            if payout.admin_comment:
                line += f"\n  Комментарий: {payout.admin_comment}"
            lines.append(line)
        text = "💸 <b>История выплат</b>\n\n" + "\n\n".join(lines)

    try:
        await callback.message.edit_text(
            text,
            reply_markup=_back_partner_dashboard_kb(),
            parse_mode="HTML",
        )
    except Exception:
        await callback.message.answer(
            text,
            reply_markup=_back_partner_dashboard_kb(),
            parse_mode="HTML",
        )
    await callback.answer()
