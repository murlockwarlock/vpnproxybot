"""Referral system - commission balance for referral payments."""

from __future__ import annotations

import logging
from datetime import timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select

from bot.config import settings
from bot.database import async_session
from bot.models import ReferralConfig, ReferralPaymentLog, SubStatus, User
from bot.services.balance_service import get_user_balance

logger = logging.getLogger(__name__)
router = Router(name="referral")
ESCAPE_COMMANDS = {"/start", "/help", "/admin", "/policy", "/agree", "/oferta"}

ITEMS_PER_PAGE = 10


# ── FSM ───────────────────────────────────────────────

class ReferralAdminStates(StatesGroup):
    edit_commission_percent = State()
    edit_btn_name      = State()
    edit_sub_btn_name  = State()


# ── Guard: /commands don't interrupt FSM ──────────────

@router.message(Command("start"), ReferralAdminStates())
async def _ref_state_start(message: Message, state: FSMContext) -> None:
    from bot.handlers.start import cmd_start

    await cmd_start(message, state)


@router.message(Command("help"), ReferralAdminStates())
async def _ref_state_help(message: Message, state: FSMContext) -> None:
    from bot.handlers.start import show_help_command

    await show_help_command(message, state)


@router.message(Command("admin"), ReferralAdminStates())
async def _ref_state_admin(message: Message, state: FSMContext) -> None:
    from bot.handlers.admin import cmd_admin

    await cmd_admin(message, state)


@router.message(Command("policy"), ReferralAdminStates())
async def _ref_state_policy(message: Message, state: FSMContext) -> None:
    from bot.handlers.start import show_policy_command

    await state.clear()
    await show_policy_command(message)


@router.message(Command("agree"), ReferralAdminStates())
async def _ref_state_agree(message: Message, state: FSMContext) -> None:
    from bot.handlers.start import show_agree_command

    await state.clear()
    await show_agree_command(message)


@router.message(Command("oferta"), ReferralAdminStates())
async def _ref_state_oferta(message: Message, state: FSMContext) -> None:
    from bot.handlers.start import show_oferta_command

    await state.clear()
    await show_oferta_command(message)


@router.message(ReferralAdminStates(), F.text.startswith("/"))
async def _guard_ref_admin(message: Message) -> None:
    command = (message.text or "").split(maxsplit=1)[0].lower()
    if command in ESCAPE_COMMANDS:
        return
    await message.answer("⚠️ Введите значение или нажмите «◀️ Отмена».")


# ── Helpers ───────────────────────────────────────────

def _is_admin(uid: int) -> bool:
    return settings.is_admin(uid)


async def _get_or_create_config(session) -> ReferralConfig:
    config = await session.get(ReferralConfig, 1)
    if not config:
        config = ReferralConfig(
            id=1,
            is_enabled=False,
            commission_percent=10.0,
        )
        session.add(config)
        await session.flush()
    # Ensure new columns have defaults if they came back as None
    if config.btn_name is None:
        config.btn_name = "👥 Пригласить друзей"
    if config.sub_btn_name is None:
        config.sub_btn_name = "🤝 Бонус за приглашение"
    if config.bonus_days_referrer is None:
        config.bonus_days_referrer = 1
    if config.bonus_days_referral is None:
        config.bonus_days_referral = 1
    if config.pay_bonus_enabled is None:
        config.pay_bonus_enabled = False
    if config.pay_bonus_days is None:
        config.pay_bonus_days = 1
    if config.pay_bonus_first_only is None:
        config.pay_bonus_first_only = True
    return config


async def apply_bonus_days(session, user_db_id: int, days: int) -> bool:
    """Extend the user's active subscription by `days`, or store in bonus_days.
    Returns True if applied to subscription, False if stored as pending bonus."""
    user = await session.get(User, user_db_id)
    if not user or days <= 0:
        return False
    for sub in user.subscriptions:
        if sub.status == SubStatus.ACTIVE:
            sub.expires_at += timedelta(days=days)
            return True
    user.bonus_days = (user.bonus_days or 0) + days
    return False


def _build_referral_copy_messages(tg_link: str, web_link: str | None = None, bot_username: str = "") -> list[tuple[str, InlineKeyboardMarkup | None]]:
    """Return list of (text, markup) tuples for clickable referral link messages."""
    empty_kb = InlineKeyboardMarkup(inline_keyboard=[])
    tg_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Открыть бота →", url=tg_link)]
    ])
    items: list[tuple[str, InlineKeyboardMarkup | None]] = [
        ("Ваша реферальная ссылка для Telegram:", None),
        (tg_link, tg_kb),
    ]
    if web_link:
        web_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Открыть сайт →", url=web_link)]
        ])
        items.append(("Ваша реферальная ссылка для сайта:", None))
        items.append((web_link, web_kb))
    return items


# ── User side: show referral info ─────────────────────

@router.callback_query(F.data.in_({"ref_link", "ref_link_main"}))
async def show_referral_info(callback: CallbackQuery) -> None:
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == callback.from_user.id)
        )
        user = result.scalar_one_or_none()
        if not user:
            await callback.answer("Ошибка: пользователь не найден.", show_alert=True)
            return

        config = await _get_or_create_config(session)
        await session.commit()

        ref_count = await session.scalar(
            select(func.count(User.id)).where(User.referred_by == callback.from_user.id)
        ) or 0

    bot_info = await callback.bot.get_me()
    tg_link = f"https://t.me/{bot_info.username}?start=ref_{callback.from_user.id}"
    web_link = None
    if not config.is_enabled:
        program_text = "Реферальная программа сейчас отключена."
    else:
        program_text = (
            f"Пригласите друга по ссылке - вы получите {config.commission_percent}% от его первой оплаты на свой баланс!\n\n"
            f"Накопленный баланс можно использовать для оплаты наших услуг."
        )

    web_link = None
    if settings.webstore_public_enabled:
        web_link = f"{settings.webstore_api_base_url.rstrip('/')}/buy?ref={callback.from_user.id}"

    back_cb = "back_main" if callback.data == "ref_link_main" else "profile"
    text = (
        f"🔗 <b>Реферальная программа</b>\n\n"
        f"{program_text}\n\n"
        f"👥 Приглашено: <b>{ref_count} чел.</b>\n"
        f"💰 Ваш баланс: <b>{get_user_balance(user):.2f} ₽</b>\n\n"
        f"Ваши реферальные ссылки 👇"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data=back_cb)]
    ])
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    for msg_text, msg_kb in _build_referral_copy_messages(tg_link, web_link):
        kwargs: dict = {"disable_web_page_preview": True}
        if msg_kb is not None:
            kwargs["reply_markup"] = msg_kb
        await callback.message.answer(msg_text, **kwargs)
    await callback.answer()


# ── Admin: referral main menu ─────────────────────────

@router.callback_query(F.data == "adm_referral")
async def admin_referral_menu(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return

    from bot.keyboards.admin import referral_menu_kb

    async with async_session() as session:
        config = await _get_or_create_config(session)
        await session.commit()

        total_referrers = await session.scalar(
            select(func.count(func.distinct(User.referred_by))).where(User.referred_by.isnot(None))
        ) or 0
        total_referred = await session.scalar(
            select(func.count(User.id)).where(User.referred_by.isnot(None))
        ) or 0
        total_turnover = await session.scalar(
            select(func.sum(ReferralPaymentLog.amount))
        ) or 0.0

    status = "🟢 ВКЛ" if config.is_enabled else "🔴 ВЫКЛ"
    text = (
        f"👫 <b>Реферальная программа</b>\n\n"
        f"Статус: {status}\n"
        f"Всего рефереров: <b>{total_referrers}</b>\n"
        f"Всего рефералов: <b>{total_referred}</b>\n"
        f"Общий оборот: <b>{total_turnover:.2f} ₽</b>"
    )
    try:
        await callback.message.edit_text(text, reply_markup=referral_menu_kb(), parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=referral_menu_kb(), parse_mode="HTML")
    await callback.answer()


# ── Admin: settings ───────────────────────────────────

@router.callback_query(F.data == "adm_ref_settings")
async def admin_ref_settings(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return

    from bot.keyboards.admin import referral_settings_kb

    async with async_session() as session:
        config = await _get_or_create_config(session)
        await session.commit()

    try:
        await callback.message.edit_text(
            "⚙️ <b>Настройки реферальной программы</b>",
            reply_markup=referral_settings_kb(config),
            parse_mode="HTML",
        )
    except Exception:
        await callback.message.answer(
            "⚙️ <b>Настройки реферальной программы</b>",
            reply_markup=referral_settings_kb(config),
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data == "adm_ref_toggle")
async def admin_ref_toggle(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    async with async_session() as session:
        config = await _get_or_create_config(session)
        config.is_enabled = not config.is_enabled
        await session.commit()
    await admin_ref_settings(callback)


# ── Admin: FSM edits ──────────────────────────────────

def _cancel_kb(back_cb: str = "adm_ref_settings") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Отмена", callback_data=back_cb)]
    ])


async def _start_edit(callback: CallbackQuery, state: FSMContext, st: State, prompt: str) -> None:
    await state.set_state(st)
    await callback.message.edit_text(prompt, parse_mode="HTML", reply_markup=_cancel_kb())
    await callback.answer()


@router.callback_query(F.data == "adm_ref_edit_comm")
async def adm_ref_edit_comm(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await _start_edit(callback, state, ReferralAdminStates.edit_commission_percent,
                      "🎁 Введите процент отчислений от первой оплаты друга (число от 0 до 100):")


@router.callback_query(F.data == "adm_ref_edit_btn")
async def adm_ref_edit_btn(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await _start_edit(callback, state, ReferralAdminStates.edit_btn_name,
                      "📌 Введите новое название кнопки в меню профиля:")


@router.callback_query(F.data == "adm_ref_edit_sub")
async def adm_ref_edit_sub(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await _start_edit(callback, state, ReferralAdminStates.edit_sub_btn_name,
                      "📌 Введите новое название кнопки в меню подписки:")


@router.message(ReferralAdminStates.edit_commission_percent, F.text)
async def proc_edit_comm(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    try:
        val = float(message.text.strip().replace(",", "."))
        assert 0 <= val <= 100
    except (ValueError, AssertionError):
        await message.answer("❌ Введите число от 0 до 100")
        return
    async with async_session() as session:
        config = await _get_or_create_config(session)
        config.commission_percent = val
        await session.commit()
    await state.clear()
    from bot.keyboards.admin import referral_settings_kb
    async with async_session() as session:
        config = await _get_or_create_config(session)
    await message.answer(
        f"✅ Сохранено: {val}%\n\n⚙️ <b>Настройки реферальной программы</b>",
        reply_markup=referral_settings_kb(config),
        parse_mode="HTML",
    )


@router.message(ReferralAdminStates.edit_btn_name, F.text)
async def proc_edit_btn(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    val = message.text.strip()
    if len(val) > 64:
        await message.answer("❌ Слишком длинное название (макс. 64 символа)")
        return
    async with async_session() as session:
        config = await _get_or_create_config(session)
        config.btn_name = val
        await session.commit()
    await state.clear()
    from bot.keyboards.admin import referral_settings_kb
    async with async_session() as session:
        config = await _get_or_create_config(session)
    await message.answer(
        "✅ Название кнопки обновлено.\n\n⚙️ <b>Настройки реферальной программы</b>",
        reply_markup=referral_settings_kb(config),
        parse_mode="HTML",
    )


@router.message(ReferralAdminStates.edit_sub_btn_name, F.text)
async def proc_edit_sub(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    val = message.text.strip()
    if len(val) > 64:
        await message.answer("❌ Слишком длинное название (макс. 64 символа)")
        return
    async with async_session() as session:
        config = await _get_or_create_config(session)
        config.sub_btn_name = val
        await session.commit()
    await state.clear()
    from bot.keyboards.admin import referral_settings_kb
    async with async_session() as session:
        config = await _get_or_create_config(session)
    await message.answer(
        "✅ Название кнопки подписки обновлено.\n\n⚙️ <b>Настройки реферальной программы</b>",
        reply_markup=referral_settings_kb(config),
        parse_mode="HTML",
    )


# ── Admin: referrers list ─────────────────────────────

@router.callback_query(F.data.startswith("adm_ref_list_"))
async def admin_ref_list(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return

    from bot.keyboards.admin import referral_referrers_kb

    try:
        page = int(callback.data.split("_")[-1])
    except ValueError:
        page = 1

    async with async_session() as session:
        # Count total distinct referrers
        total_count = await session.scalar(
            select(func.count(func.distinct(User.referred_by))).where(User.referred_by.isnot(None))
        ) or 0

        if total_count == 0:
            try:
                await callback.message.edit_text(
                    "👥 Рефереров пока нет.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_referral")]
                    ]),
                )
            except Exception:
                pass
            await callback.answer()
            return

        total_pages = max(1, (total_count + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
        page = max(1, min(page, total_pages))
        offset = (page - 1) * ITEMS_PER_PAGE

        # Get referred_by telegram_ids (with counts) for this page
        ref_counts_result = await session.execute(
            select(User.referred_by, func.count(User.id).label("cnt"))
            .where(User.referred_by.isnot(None))
            .group_by(User.referred_by)
            .order_by(func.count(User.id).desc())
            .offset(offset)
            .limit(ITEMS_PER_PAGE)
        )
        ref_rows = ref_counts_result.all()

        rows = []
        for (tg_id, cnt) in ref_rows:
            # Get referrer info
            ref_user_result = await session.execute(
                select(User).where(User.telegram_id == tg_id)
            )
            ref_user = ref_user_result.scalar_one_or_none()
            name = ref_user.full_name if ref_user else ""
            username = ref_user.username if ref_user else ""

            # Get turnover for this referrer
            if ref_user:
                turnover = await session.scalar(
                    select(func.sum(ReferralPaymentLog.amount))
                    .where(ReferralPaymentLog.referrer_id == ref_user.id)
                ) or 0.0
            else:
                turnover = 0.0

            rows.append((tg_id, name, username, cnt, turnover))

    text = f"👫 <b>Рефереры</b> · стр. {page}/{total_pages}\n━━━━━━━━━━━━━━"
    try:
        await callback.message.edit_text(
            text,
            reply_markup=referral_referrers_kb(rows, page, total_pages),
            parse_mode="HTML",
        )
    except Exception:
        await callback.message.answer(
            text,
            reply_markup=referral_referrers_kb(rows, page, total_pages),
            parse_mode="HTML",
        )
    await callback.answer()


# ── Admin: referrer detail ────────────────────────────

@router.callback_query(F.data.startswith("adm_ref_detail_"))
async def admin_ref_detail(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return

    from bot.keyboards.admin import referral_referrer_detail_kb

    # Format: adm_ref_detail_{tg_id}_{page}
    parts = callback.data.split("_")
    try:
        referrer_tg_id = int(parts[3])
        page = int(parts[4]) if len(parts) > 4 else 0
    except (IndexError, ValueError):
        await callback.answer("Ошибка", show_alert=True)
        return

    async with async_session() as session:
        ref_user_result = await session.execute(
            select(User).where(User.telegram_id == referrer_tg_id)
        )
        referrer = ref_user_result.scalar_one_or_none()
        if not referrer:
            await callback.answer("Реферер не найден", show_alert=True)
            return

        # Get all referrals (paginated)
        total_count = await session.scalar(
            select(func.count(User.id)).where(User.referred_by == referrer_tg_id)
        ) or 0
        total_pages = max(1, (total_count + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
        page = max(0, min(page, total_pages - 1))

        referrals_result = await session.execute(
            select(User)
            .where(User.referred_by == referrer_tg_id)
            .order_by(User.created_at.desc())
            .offset(page * ITEMS_PER_PAGE)
            .limit(ITEMS_PER_PAGE)
        )
        referrals = referrals_result.scalars().all()

        total_turnover = await session.scalar(
            select(func.sum(ReferralPaymentLog.amount))
            .where(ReferralPaymentLog.referrer_id == referrer.id)
        ) or 0.0

        # Per-referral turnover
        referral_lines = []
        for r in referrals:
            r_turnover = await session.scalar(
                select(func.sum(ReferralPaymentLog.amount))
                .where(
                    ReferralPaymentLog.referrer_id == referrer.id,
                    ReferralPaymentLog.referred_user_id == r.id,
                )
            ) or 0.0
            reg_date = r.created_at.strftime("%d.%m.%Y")
            name = r.full_name or r.username or str(r.telegram_id)
            referral_lines.append(
                f"👤 {name} [<code>{r.telegram_id}</code>] - {reg_date}\n"
                f"   💰 Оборот: {r_turnover:.2f} ₽"
            )

    ref_name = referrer.full_name or referrer.username or str(referrer_tg_id)
    text = (
        f"👤 <b>Реферер:</b> {ref_name} [<code>{referrer_tg_id}</code>]\n"
        f"Всего приглашено: <b>{total_count}</b>\n"
        f"Общий оборот: <b>{total_turnover:.2f} ₽</b>\n\n"
        f"<b>Рефералы (стр. {page + 1}/{total_pages}):</b>\n"
        f"━━━━━━━━━━━━━━\n"
        + "\n━━━━━━━━━━━━━━\n".join(referral_lines)
    )

    try:
        await callback.message.edit_text(
            text,
            reply_markup=referral_referrer_detail_kb(referrer_tg_id, page, total_pages),
            parse_mode="HTML",
        )
    except Exception:
        await callback.message.answer(
            text,
            reply_markup=referral_referrer_detail_kb(referrer_tg_id, page, total_pages),
            parse_mode="HTML",
        )
    await callback.answer()
