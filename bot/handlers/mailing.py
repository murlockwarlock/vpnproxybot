"""Mailing system - admin broadcast with audience targeting, media, preview."""

from __future__ import annotations

import html
import logging
import math
import re
from datetime import timedelta, timezone

import asyncio
from typing import Any, Awaitable, Callable
from aiogram import Bot, F, Router, BaseMiddleware
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
    InputMediaPhoto,
    InputMediaVideo,
)
from sqlalchemy import func, select

from bot.config import settings
from bot.database import async_session
from bot.models import BotSettings, FollowUpCampaign, Mailing
from bot.services.device_slots import get_included_device_slots
from bot.handlers.admin import AdminStates

logger = logging.getLogger(__name__)
router = Router(name="mailing")


class MediaGroupMiddleware(BaseMiddleware):
    def __init__(self, latency: float = 0.5):
        super().__init__()
        self.latency = latency
        self.cache: dict[str, list[Message]] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message) or event.media_group_id is None:
            return await handler(event, data)

        media_group_id = event.media_group_id
        
        if media_group_id not in self.cache:
            self.cache[media_group_id] = [event]
            await asyncio.sleep(self.latency)
            messages = self.cache.pop(media_group_id, [])
            messages.sort(key=lambda m: m.message_id)
            data["album"] = messages
            return await handler(event, data)
        else:
            self.cache[media_group_id].append(event)
            return None


router.message.middleware(MediaGroupMiddleware())


def _parse_album_media(media_str: str, caption: str | None = None) -> list[InputMediaPhoto | InputMediaVideo]:
    items = []
    parts = media_str.split(",")
    for idx, part in enumerate(parts):
        if ":" in part:
            m_type, file_id = part.split(":", 1)
        else:
            m_type, file_id = "photo", part
        
        item_caption = caption if idx == 0 else None
        
        if m_type == "video":
            items.append(InputMediaVideo(media=file_id, caption=item_caption, parse_mode="HTML"))
        else:
            items.append(InputMediaPhoto(media=file_id, caption=item_caption, parse_mode="HTML"))
    return items


ESCAPE_COMMANDS = {"/start", "/help", "/admin", "/policy", "/agree", "/oferta"}

MSK = timezone(timedelta(hours=3))
PAGE_SIZE = 5

AUDIENCES_MAP: dict[str, str] = {
    "all":                  "Всем пользователям",
    "self":                 "👤 Только себе (тест)",
    "active_subscription":  "Активным подписчикам",
    "inactive_subscription":"С истёкшей подпиской",
    "no_subscription":      "Без подписки",
    "referred":             "Пришедшим по реферальной ссылке",
}


# ── FSM ───────────────────────────────────────────────

class MailingStates(StatesGroup):
    audience     = State()
    content      = State()
    media_pos    = State()
    buttons      = State()
    btn_type     = State()
    btn_text     = State()
    btn_url      = State()
    confirmation = State()


class FollowUpStates(StatesGroup):
    create_name  = State()
    create_days  = State()
    create_text  = State()
    edit_name    = State()
    edit_days    = State()
    edit_text    = State()
    upload_media = State()
    buttons      = State()
    btn_type     = State()
    btn_text     = State()
    btn_url      = State()


class BulkKeyStates(StatesGroup):
    audience     = State()
    tariff       = State()
    text         = State()
    confirmation = State()


# ── Guard: intercept /commands while in mailing FSM ───

@router.message(Command("start"), MailingStates())
@router.message(Command("start"), FollowUpStates())
async def _mailing_state_start(message: Message, state: FSMContext) -> None:
    from bot.handlers.start import cmd_start

    await cmd_start(message, state)


@router.message(Command("help"), MailingStates())
@router.message(Command("help"), FollowUpStates())
async def _mailing_state_help(message: Message, state: FSMContext) -> None:
    from bot.handlers.start import show_help_command

    await show_help_command(message, state)


@router.message(Command("admin"), MailingStates())
@router.message(Command("admin"), FollowUpStates())
async def _mailing_state_admin(message: Message, state: FSMContext) -> None:
    from bot.handlers.admin import cmd_admin

    await cmd_admin(message, state)


@router.message(Command("policy"), MailingStates())
@router.message(Command("policy"), FollowUpStates())
async def _mailing_state_policy(message: Message, state: FSMContext) -> None:
    from bot.handlers.start import show_policy_command

    await state.clear()
    await show_policy_command(message)


@router.message(Command("agree"), MailingStates())
@router.message(Command("agree"), FollowUpStates())
async def _mailing_state_agree(message: Message, state: FSMContext) -> None:
    from bot.handlers.start import show_agree_command

    await state.clear()
    await show_agree_command(message)


@router.message(Command("oferta"), MailingStates())
@router.message(Command("oferta"), FollowUpStates())
async def _mailing_state_oferta(message: Message, state: FSMContext) -> None:
    from bot.handlers.start import show_oferta_command

    await state.clear()
    await show_oferta_command(message)


@router.message(MailingStates(), F.text.startswith("/"))
async def _guard_commands(message: Message) -> None:
    """Block /commands from interrupting the mailing creation flow."""
    command = (message.text or "").split(maxsplit=1)[0].lower()
    if command in ESCAPE_COMMANDS:
        return
    await message.answer(
        "⚠️ Вы в режиме создания рассылки.\n"
        "Нажмите «❌ Отмена» для выхода."
    )


# ── Admin check ───────────────────────────────────────

def _is_admin(uid: int) -> bool:
    return settings.is_admin(uid)


# ── Button utilities ─────────────────────────────────

import json as _json

# Preset internal buttons that map to bot callback_data
INTERNAL_BUTTONS: dict[str, str] = {
    "buy":              "🛒 Купить доступ",
    "ptype_vpn":        "🌐 Весь интернет",
    "ptype_tg_proxy":   "📱 Telegram-ускоритель",
    "ptype_both":       "🔥 Весь интернет + Telegram-ускоритель",
    "profile":          "👤 Мой профиль",
    "guide_menu":       "📲 Как подключить",
    "ref_link_main":    "🤝 Реферальная программа",
    "help":             "❓ Помощь",
}


def _parse_buttons(buttons_json: str | None) -> list[dict]:
    if not buttons_json:
        return []
    try:
        return _json.loads(buttons_json)
    except (ValueError, TypeError):
        return []


def _buttons_to_json(buttons: list[dict]) -> str | None:
    if not buttons:
        return None
    return _json.dumps(buttons, ensure_ascii=False)


def build_user_kb(buttons_json: str | None) -> InlineKeyboardMarkup | None:
    """Build InlineKeyboardMarkup from buttons_json for sending to users."""
    buttons = _parse_buttons(buttons_json)
    if not buttons:
        return None
    # Group by row number
    rows_map: dict[int, list] = {}
    for b in buttons:
        row_num = b.get("row", 0)
        rows_map.setdefault(row_num, [])
        if b.get("type") == "url":
            rows_map[row_num].append(InlineKeyboardButton(text=b["text"], url=b["data"]))
        else:
            rows_map[row_num].append(InlineKeyboardButton(text=b["text"], callback_data=b["data"]))
    kb_rows = [rows_map[r] for r in sorted(rows_map)]
    return InlineKeyboardMarkup(inline_keyboard=kb_rows)


def _buttons_preview(buttons_json: str | None) -> str:
    buttons = _parse_buttons(buttons_json)
    if not buttons:
        return "—"
    lines = []
    for b in buttons:
        btype = "🔗" if b.get("type") == "url" else "🔘"
        lines.append(f"{btype} {b['text']}")
    return "\n".join(lines)


def _btn_editor_kb(buttons: list[dict], back_callback: str) -> InlineKeyboardMarkup:
    """Keyboard for the button editor screen."""
    rows = []
    for idx, b in enumerate(buttons):
        btype = "🔗" if b.get("type") == "url" else "🔘"
        row_label = f"[ряд {b.get('row', 1)}]"
        rows.append([
            InlineKeyboardButton(text=f"{btype} {b['text']} {row_label}", callback_data=f"btn_noop_{idx}"),
            InlineKeyboardButton(text="🗑", callback_data=f"btn_del_{idx}"),
        ])
    rows.append([InlineKeyboardButton(text="➕ Добавить кнопку", callback_data="btn_add")])
    rows.append([InlineKeyboardButton(text="✅ Готово", callback_data=back_callback)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _btn_type_kb() -> InlineKeyboardMarkup:
    """Choose button type."""
    rows = []
    for key, label in INTERNAL_BUTTONS.items():
        rows.append([InlineKeyboardButton(text=label, callback_data=f"btn_preset_{key}")])
    rows.append([InlineKeyboardButton(text="🔗 Ссылка (URL)", callback_data="btn_preset_url")])
    rows.append([InlineKeyboardButton(text="◀️ Отмена", callback_data="btn_back_editor")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Keyboards ─────────────────────────────────────────

def _menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Создать рассылку",     callback_data="adm_mailing_create")],
        [InlineKeyboardButton(text="🔑 Массовая выдача ключей", callback_data="adm_bulk_keys")],
        [InlineKeyboardButton(text="📜 История рассылок",     callback_data="adm_mailing_history_0")],
        [InlineKeyboardButton(text="🔔 Догоняющая рассылка",  callback_data="adm_followup")],
        [InlineKeyboardButton(text="◀️ Назад",                callback_data="adm_back")],
    ])


def _audience_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"adm_ml_aud_{key}")]
        for key, label in AUDIENCES_MAP.items()
    ]
    rows.append([InlineKeyboardButton(text="◀️ Отмена", callback_data="adm_mailing")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _back_to_audience_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад к аудитории", callback_data="adm_mailing_create")]
    ])


def _media_pos_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🖼 Медиа сверху", callback_data="adm_ml_pos_media_top")],
        [InlineKeyboardButton(text="📝 Текст сверху",  callback_data="adm_ml_pos_text_top")],
        [InlineKeyboardButton(text="◀️ Назад",         callback_data="adm_ml_edit")],
    ])


def _confirm_kb(btn_count: int = 0) -> InlineKeyboardMarkup:
    btn_label = f"🔘 Кнопки ({btn_count})" if btn_count else "🔘 Кнопки"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Отправить", callback_data="adm_ml_send"),
            InlineKeyboardButton(text="✏️ Изменить",  callback_data="adm_ml_edit"),
        ],
        [InlineKeyboardButton(text=btn_label, callback_data="adm_ml_btns")],
        [InlineKeyboardButton(text="❌ Отмена",       callback_data="adm_mailing")],
    ])


def _history_kb(mailings: list[Mailing], page: int, total_pages: int) -> InlineKeyboardMarkup:
    rows = []
    status_icons = {"pending": "⏳", "sending": "🚀", "completed": "✅", "failed": "❌"}
    for m in mailings:
        icon = status_icons.get(m.status, "❓")
        ts = (m.start_time or m.created_at).replace(tzinfo=timezone.utc).astimezone(MSK)
        preview = (m.text or "Без текста")[:20]
        rows.append([InlineKeyboardButton(
            text=f"{icon} {ts.strftime('%d.%m %H:%M')} | {preview}…",
            callback_data=f"adm_ml_det_{m.id}",
        )])

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"adm_mailing_history_{page - 1}"))
    if total_pages > 1:
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"adm_mailing_history_{page + 1}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm_mailing")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "adm_mailing")
async def mailing_menu(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    preview_media_ids = data.get("preview_media_ids", [])
    for p_id in preview_media_ids:
        try:
            await callback.message.bot.delete_message(callback.message.chat.id, p_id)
        except Exception:
            pass
    await state.clear()

    # If current message contains media we can't edit it to text - delete + re-send
    if callback.message.photo or callback.message.video:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.answer(
            "✉️ <b>Управление рассылками</b>",
            reply_markup=_menu_kb(),
            parse_mode="HTML",
        )
    else:
        try:
            await callback.message.edit_text(
                "✉️ <b>Управление рассылками</b>",
                reply_markup=_menu_kb(),
                parse_mode="HTML",
            )
        except Exception:
            await callback.message.answer(
                "✉️ <b>Управление рассылками</b>",
                reply_markup=_menu_kb(),
                parse_mode="HTML",
            )
    await callback.answer()


# ── Step 1: audience ──────────────────────────────────

@router.callback_query(F.data == "adm_mailing_create")
async def mailing_create(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await state.set_state(MailingStates.audience)
    await state.update_data(msg_id=callback.message.message_id)
    try:
        await callback.message.edit_text(
            "📋 <b>Шаг 1 из 3: Аудитория</b>\n\nВыберите, кому отправить рассылку:",
            reply_markup=_audience_kb(),
            parse_mode="HTML",
        )
    except Exception:
        sent = await callback.message.answer(
            "📋 <b>Шаг 1 из 3: Аудитория</b>\n\nВыберите, кому отправить рассылку:",
            reply_markup=_audience_kb(),
            parse_mode="HTML",
        )
        await state.update_data(msg_id=sent.message_id)
    await callback.answer()


@router.callback_query(F.data.startswith("adm_ml_aud_"), MailingStates.audience)
async def mailing_select_audience(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    audience = callback.data.replace("adm_ml_aud_", "")
    await state.update_data(audience=audience)
    await state.set_state(MailingStates.content)
    await callback.message.edit_text(
        "📝 <b>Шаг 2 из 3: Содержимое</b>\n\n"
        "Отправьте сообщение для рассылки:\n"
        "• Текст (HTML-разметка поддерживается)\n"
        "• Фото с подписью\n"
        "• Видео с подписью",
        reply_markup=_back_to_audience_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


# ── Step 2: content ───────────────────────────────────

@router.message(MailingStates.content, F.text | F.photo | F.video)
async def mailing_receive_content(message: Message, state: FSMContext, bot: Bot, album: list[Message] | None = None) -> None:
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    msg_id = data.get("msg_id")

    # Merge with previously saved content
    media_id   = data.get("media_file_id")
    media_type = data.get("media_file_type")
    text       = data.get("text")
    position   = data.get("media_position")

    if album:
        media_list = []
        for msg in album:
            if msg.photo:
                media_list.append(f"photo:{msg.photo[-1].file_id}")
            elif msg.video:
                media_list.append(f"video:{msg.video.file_id}")
            if msg.caption:
                text = msg.html_text
        if media_list:
            media_id = ",".join(media_list)
            media_type = "album"
    else:
        if message.photo:
            media_id   = message.photo[-1].file_id
            media_type = "photo"
            if message.caption:
                text = message.html_text
        elif message.video:
            media_id   = message.video.file_id
            media_type = "video"
            if message.caption:
                text = message.html_text
        elif message.text:
            text = message.html_text

    if album:
        for msg in album:
            try:
                await msg.delete()
            except Exception:
                pass
    else:
        try:
            await message.delete()
        except Exception:
            pass

    await state.update_data(
        text=text,
        media_file_id=media_id,
        media_file_type=media_type,
    )

    if media_id and not position:
        await state.set_state(MailingStates.media_pos)
        try:
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=msg_id,
                text="🎨 <b>Шаг 2б: Порядок</b>\n\nВыберите порядок медиа и текста:",
                reply_markup=_media_pos_kb(),
                parse_mode="HTML",
            )
        except Exception:
            sent = await message.answer(
                "🎨 <b>Шаг 2б: Порядок</b>\n\nВыберите порядок медиа и текста:",
                reply_markup=_media_pos_kb(),
                parse_mode="HTML",
            )
            await state.update_data(msg_id=sent.message_id)
    else:
        if not position:
            await state.update_data(media_position="media_top")
        await _show_preview(message.chat.id, msg_id, state, bot)


@router.callback_query(F.data.startswith("adm_ml_pos_"), MailingStates.media_pos)
async def mailing_select_position(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    if not _is_admin(callback.from_user.id):
        return
    pos = callback.data.replace("adm_ml_pos_", "")
    await state.update_data(media_position=pos)
    await _show_preview(callback.message.chat.id, callback.message.message_id, state, bot)
    await callback.answer()


# ── Step 3: preview & confirm ─────────────────────────

async def _show_preview(chat_id: int, msg_id: int, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    text       = data.get("text") or ""
    media_id   = data.get("media_file_id")
    media_type = data.get("media_file_type")
    position   = data.get("media_position", "media_top")
    audience   = data.get("audience", "")
    buttons    = data.get("buttons", [])

    audience_label = AUDIENCES_MAP.get(audience, audience)
    pos_label      = "🖼 Медиа сверху" if position == "media_top" else "📝 Текст сверху"

    plain   = re.sub(r"<[^>]+>", "", text)
    display = plain[:500] + "…" if len(plain) > 500 else plain

    btns_info = _buttons_preview(_buttons_to_json(buttons)) if buttons else "—"
    caption = (
        f"<b>👁 Предпросмотр рассылки</b>\n"
        f"Аудитория: <i>{audience_label}</i>\n"
        f"Порядок: {pos_label}\n"
        f"Кнопки: {btns_info}\n"
        f"───────────────────\n"
        f"{html.escape(display) if display else '<i>[Текст отсутствует]</i>'}\n"
        f"───────────────────"
    )

    await state.set_state(MailingStates.confirmation)
    kb = _confirm_kb(len(buttons))

    # Delete previous preview media group if any
    preview_media_ids = data.get("preview_media_ids", [])
    for p_id in preview_media_ids:
        try:
            await bot.delete_message(chat_id, p_id)
        except Exception:
            pass
    await state.update_data(preview_media_ids=[])

    try:
        if media_id:
            try:
                await bot.delete_message(chat_id, msg_id)
            except Exception:
                pass

            if media_type == "album":
                album_media = _parse_album_media(media_id)
                sent_msgs = await bot.send_media_group(chat_id, media=album_media)
                await state.update_data(preview_media_ids=[m.message_id for m in sent_msgs])
                
                sent = await bot.send_message(
                    chat_id, caption, parse_mode="HTML", reply_markup=kb
                )
                await state.update_data(msg_id=sent.message_id)
            else:
                send_fn = bot.send_photo if media_type == "photo" else bot.send_video
                sent = await send_fn(
                    chat_id, media_id,
                    caption=caption, parse_mode="HTML", reply_markup=kb,
                )
                await state.update_data(msg_id=sent.message_id)
        else:
            await bot.edit_message_text(
                caption,
                chat_id=chat_id, message_id=msg_id,
                parse_mode="HTML", reply_markup=kb,
            )
    except Exception:
        sent = await bot.send_message(
            chat_id, caption, parse_mode="HTML", reply_markup=kb,
        )
        await state.update_data(msg_id=sent.message_id)


@router.callback_query(F.data == "adm_ml_btns", MailingStates.confirmation)
async def mailing_buttons_editor(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    data = await state.get_data()
    buttons = data.get("buttons", [])
    await state.set_state(MailingStates.buttons)
    await state.update_data(btn_context="mailing")
    btn_text = "🔘 <b>Редактор кнопок</b>\n\nДобавьте кнопки или нажмите «Готово»."
    btn_kb = _btn_editor_kb(buttons, "adm_ml_btns_done")
    try:
        await callback.message.edit_text(btn_text, reply_markup=btn_kb, parse_mode="HTML")
    except Exception:
        try:
            await callback.message.delete()
        except Exception:
            pass
        sent = await callback.message.answer(btn_text, reply_markup=btn_kb, parse_mode="HTML")
        await state.update_data(msg_id=sent.message_id)
    await callback.answer()


@router.callback_query(F.data == "adm_ml_btns_done", MailingStates.buttons)
async def mailing_buttons_done(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    if not _is_admin(callback.from_user.id):
        return
    # Return to preview
    data = await state.get_data()
    await _show_preview(callback.message.chat.id, callback.message.message_id, state, bot)
    await callback.answer()


@router.callback_query(F.data == "adm_ml_edit", MailingStates.confirmation)
async def mailing_edit(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    data = await state.get_data()
    preview_media_ids = data.get("preview_media_ids", [])
    for p_id in preview_media_ids:
        try:
            await callback.message.bot.delete_message(callback.message.chat.id, p_id)
        except Exception:
            pass
    await state.update_data(preview_media_ids=[])

    await state.set_state(MailingStates.content)
    try:
        await callback.message.delete()
    except Exception:
        pass
    sent = await callback.message.answer(
        "📝 Отправьте новое содержимое (текст, фото, видео):",
        reply_markup=_back_to_audience_kb(),
    )
    await state.update_data(msg_id=sent.message_id)
    await callback.answer()


@router.callback_query(F.data == "adm_ml_send", MailingStates.confirmation)
async def mailing_confirm_send(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    data = await state.get_data()
    preview_media_ids = data.get("preview_media_ids", [])
    for p_id in preview_media_ids:
        try:
            await callback.message.bot.delete_message(callback.message.chat.id, p_id)
        except Exception:
            pass

    async with async_session() as session:
        m = Mailing(
            text=data.get("text"),
            media_file_id=data.get("media_file_id"),
            media_file_type=data.get("media_file_type"),
            media_position=data.get("media_position", "media_top"),
            buttons_json=_buttons_to_json(data.get("buttons", [])),
            target_audience=data.get("audience"),
            creator_id=callback.from_user.id,
            status="pending",
        )
        session.add(m)
        await session.commit()
        mailing_id = m.id

    await state.clear()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(
        f"✅ Рассылка #{mailing_id} добавлена в очередь.\n"
        "Вы получите отчёт по завершении.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ К рассылкам", callback_data="adm_mailing")]
        ]),
    )
    await callback.answer()


# ── History ───────────────────────────────────────────

@router.callback_query(F.data.startswith("adm_mailing_history_"))
async def mailing_history(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    page = int(callback.data.split("_")[-1])

    async with async_session() as session:
        total = await session.scalar(select(func.count(Mailing.id))) or 0

    if total == 0:
        try:
            await callback.message.edit_text(
                "📜 История рассылок пуста.",
                reply_markup=_history_kb([], 0, 1),
            )
        except Exception:
            pass
        await callback.answer()
        return

    total_pages = math.ceil(total / PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    async with async_session() as session:
        result = await session.execute(
            select(Mailing)
            .order_by(Mailing.created_at.desc())
            .offset(page * PAGE_SIZE)
            .limit(PAGE_SIZE)
        )
        mailings = result.scalars().all()

    try:
        await callback.message.edit_text(
            f"📜 <b>История рассылок</b> (стр. {page + 1}/{total_pages})",
            reply_markup=_history_kb(mailings, page, total_pages),
            parse_mode="HTML",
        )
    except Exception:
        await callback.message.answer(
            f"📜 <b>История рассылок</b> (стр. {page + 1}/{total_pages})",
            reply_markup=_history_kb(mailings, page, total_pages),
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data.startswith("adm_ml_det_"))
async def mailing_details(callback: CallbackQuery, bot: Bot) -> None:
    if not _is_admin(callback.from_user.id):
        return
    mailing_id = int(callback.data.split("_")[-1])

    async with async_session() as session:
        m = await session.get(Mailing, mailing_id)

    if not m:
        await callback.answer("Рассылка не найдена.", show_alert=True)
        return

    audience_label = AUDIENCES_MAP.get(m.target_audience, m.target_audience)
    status_labels  = {
        "pending":   "⏳ Ожидает",
        "sending":   "🚀 Отправляется",
        "completed": "✅ Завершена",
        "failed":    "❌ Ошибка",
    }
    start_str = (
        m.start_time.replace(tzinfo=timezone.utc).astimezone(MSK).strftime("%d.%m.%Y %H:%M")
        if m.start_time else "-"
    )
    end_str = (
        m.end_time.replace(tzinfo=timezone.utc).astimezone(MSK).strftime("%d.%m.%Y %H:%M")
        if m.end_time else "-"
    )

    text = (
        f"<b>📋 Рассылка #{m.id}</b>\n\n"
        f"<b>Аудитория:</b> {audience_label}\n"
        f"<b>Статус:</b> {status_labels.get(m.status, m.status)}\n"
        f"<b>Начало:</b> {start_str}\n"
        f"<b>Конец:</b> {end_str}\n"
        f"<b>Успешно:</b> {m.success_count}\n"
        f"<b>Ошибки:</b> {m.failure_count}\n\n"
        f"<b>Текст:</b>\n{m.text or '<i>Нет текста</i>'}"
    )

    back_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ К истории", callback_data="adm_mailing_history_0")]
    ])

    try:
        await callback.message.delete()
    except Exception:
        pass

    try:
        if m.media_file_id:
            if m.media_file_type == "album":
                album_media = _parse_album_media(m.media_file_id)
                await bot.send_media_group(callback.from_user.id, media=album_media)
                await bot.send_message(
                    callback.from_user.id, text, reply_markup=back_kb, parse_mode="HTML",
                )
            else:
                send_fn = bot.send_photo if m.media_file_type == "photo" else bot.send_video
                if len(text) <= 1024:
                    await send_fn(
                        callback.from_user.id, m.media_file_id,
                        caption=text, reply_markup=back_kb, parse_mode="HTML",
                    )
                else:
                    await send_fn(callback.from_user.id, m.media_file_id)
                    await bot.send_message(
                        callback.from_user.id, text, reply_markup=back_kb, parse_mode="HTML",
                    )
        else:
            await bot.send_message(
                callback.from_user.id, text, reply_markup=back_kb, parse_mode="HTML",
            )
    except Exception as exc:
        logger.error(f"Error showing mailing details: {exc}")

    await callback.answer()


@router.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery) -> None:
    await callback.answer()


# ── Follow-up campaigns (догоняющие рассылки) ─────────


def _fu_list_kb(campaigns) -> InlineKeyboardMarkup:
    rows = []
    for c in campaigns:
        status = "🟢" if c.is_enabled else "🔴"
        rows.append([InlineKeyboardButton(
            text=f"{status} {c.name} (день {c.days_after_demo})",
            callback_data=f"adm_fu_view_{c.id}",
        )])
    rows.append([InlineKeyboardButton(text="➕ Создать кампанию", callback_data="adm_fu_create")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm_mailing")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _fu_detail_kb(c) -> InlineKeyboardMarkup:
    toggle_text = "🔴 Выключить" if c.is_enabled else "🟢 Включить"
    btn_count = len(_parse_buttons(c.buttons_json))
    btn_label = f"🔘 Кнопки ({btn_count})" if btn_count else "🔘 Кнопки"
    rows = [
        [InlineKeyboardButton(text=toggle_text, callback_data=f"adm_fu_toggle_{c.id}")],
        [
            InlineKeyboardButton(text="✏️ Название", callback_data=f"adm_fu_ename_{c.id}"),
            InlineKeyboardButton(text="📅 День", callback_data=f"adm_fu_edays_{c.id}"),
        ],
        [InlineKeyboardButton(text="✏️ Текст", callback_data=f"adm_fu_etext_{c.id}")],
        [InlineKeyboardButton(text=btn_label, callback_data=f"adm_fu_btns_{c.id}")],
        [InlineKeyboardButton(text="🖼 Загрузить медиа", callback_data=f"adm_fu_emedia_{c.id}")],
    ]
    if c.media_file_id:
        rows.append([InlineKeyboardButton(text="🗑 Удалить медиа", callback_data=f"adm_fu_cmedia_{c.id}")])
    rows.append([InlineKeyboardButton(text="📨 Тест себе", callback_data=f"adm_fu_test_{c.id}")])
    rows.append([InlineKeyboardButton(text="🗑 Удалить кампанию", callback_data=f"adm_fu_del_{c.id}")])
    rows.append([InlineKeyboardButton(text="◀️ К списку", callback_data="adm_followup")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _fu_cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Отмена", callback_data="adm_followup")]
    ])


def _fu_cancel_detail_kb(campaign_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Отмена", callback_data=f"adm_fu_view_{campaign_id}")]
    ])


async def _show_fu_detail(target, campaign_id: int) -> None:

    async with async_session() as session:
        c = await session.get(FollowUpCampaign, campaign_id)
    if not c:
        msg = target.message if isinstance(target, CallbackQuery) else target
        await msg.answer("❌ Кампания не найдена.")
        return

    status = "🟢 ВКЛ" if c.is_enabled else "🔴 ВЫКЛ"
    text_preview = (c.text[:120] + "…") if len(c.text) > 120 else c.text
    media_info = f"📎 {c.media_type}" if c.media_file_id else "—"
    btns_info = _buttons_preview(c.buttons_json)
    body = (
        f"🔔 <b>{html.escape(c.name)}</b>\n\n"
        f"Статус: {status}\n"
        f"Через дней после демо: <b>{c.days_after_demo}</b>\n"
        f"Медиа: {media_info}\n"
        f"Кнопки: {btns_info}\n\n"
        f"<b>Текст:</b>\n{text_preview}"
    )
    kb = _fu_detail_kb(c)
    try:
        if isinstance(target, CallbackQuery):
            await target.message.edit_text(body, reply_markup=kb, parse_mode="HTML")
        else:
            await target.answer(body, reply_markup=kb, parse_mode="HTML")
    except Exception:
        msg = target.message if isinstance(target, CallbackQuery) else target
        await msg.answer(body, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "adm_followup")
async def followup_list(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await state.clear()

    async with async_session() as session:
        campaigns = (await session.execute(
            select(FollowUpCampaign).order_by(FollowUpCampaign.days_after_demo)
        )).scalars().all()
    text = "🔔 <b>Догоняющие рассылки</b>\n\n"
    if campaigns:
        text += f"Кампаний: {len(campaigns)}"
    else:
        text += "Нет кампаний. Создайте первую."
    await callback.message.edit_text(text, reply_markup=_fu_list_kb(campaigns), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("adm_fu_view_"))
async def followup_view(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await state.clear()
    cid = int(callback.data.split("_")[-1])
    await _show_fu_detail(callback, cid)
    await callback.answer()


@router.callback_query(F.data.startswith("adm_fu_toggle_"))
async def followup_toggle(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    cid = int(callback.data.split("_")[-1])

    async with async_session() as session:
        c = await session.get(FollowUpCampaign, cid)
        if c:
            c.is_enabled = not c.is_enabled
            await session.commit()
    await _show_fu_detail(callback, cid)
    await callback.answer()


# ── Create campaign ──

@router.callback_query(F.data == "adm_fu_create")
async def followup_create_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await state.set_state(FollowUpStates.create_name)
    await callback.message.edit_text(
        "✏️ Введите <b>название</b> кампании (для себя, пользователи не увидят):",
        reply_markup=_fu_cancel_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(FollowUpStates.create_name, F.text)
async def followup_create_name(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    await state.update_data(name=message.text.strip()[:128])
    await state.set_state(FollowUpStates.create_days)
    await message.answer(
        "📅 Через сколько <b>дней после демо</b> отправлять? (число):",
        reply_markup=_fu_cancel_kb(),
        parse_mode="HTML",
    )


@router.message(FollowUpStates.create_days, F.text)
async def followup_create_days(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    try:
        val = int(message.text.strip())
        assert val >= 1
    except (ValueError, AssertionError):
        await message.answer("❌ Введите положительное целое число.")
        return
    await state.update_data(days=val)
    await state.set_state(FollowUpStates.create_text)
    await message.answer(
        "✏️ Введите <b>текст</b> рассылки (HTML: <b>bold</b>, <i>italic</i>):",
        reply_markup=_fu_cancel_kb(),
        parse_mode="HTML",
    )


@router.message(FollowUpStates.create_text, F.text)
async def followup_create_text(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()

    async with async_session() as session:
        c = FollowUpCampaign(
            name=data["name"],
            days_after_demo=data["days"],
            text=message.text,
            is_enabled=True,
        )
        session.add(c)
        await session.commit()
        cid = c.id
    await state.clear()
    await message.answer("✅ Кампания создана.")
    await _show_fu_detail(message, cid)


# ── Edit handlers ──

@router.callback_query(F.data.startswith("adm_fu_ename_"))
async def followup_edit_name_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    cid = int(callback.data.split("_")[-1])
    await state.set_state(FollowUpStates.edit_name)
    await state.update_data(campaign_id=cid)
    await callback.message.edit_text(
        "✏️ Введите новое <b>название</b> кампании:",
        reply_markup=_fu_cancel_detail_kb(cid),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(FollowUpStates.edit_name, F.text)
async def followup_edit_name_save(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    cid = data["campaign_id"]

    async with async_session() as session:
        c = await session.get(FollowUpCampaign, cid)
        if c:
            c.name = message.text.strip()[:128]
            await session.commit()
    await state.clear()
    await message.answer("✅ Название обновлено.")
    await _show_fu_detail(message, cid)


@router.callback_query(F.data.startswith("adm_fu_edays_"))
async def followup_edit_days_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    cid = int(callback.data.split("_")[-1])
    await state.set_state(FollowUpStates.edit_days)
    await state.update_data(campaign_id=cid)
    await callback.message.edit_text(
        "📅 Введите число дней после демо (например: <b>4</b>):",
        reply_markup=_fu_cancel_detail_kb(cid),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(FollowUpStates.edit_days, F.text)
async def followup_edit_days_save(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    try:
        val = int(message.text.strip())
        assert val >= 1
    except (ValueError, AssertionError):
        await message.answer("❌ Введите положительное целое число.")
        return
    data = await state.get_data()
    cid = data["campaign_id"]

    async with async_session() as session:
        c = await session.get(FollowUpCampaign, cid)
        if c:
            c.days_after_demo = val
            await session.commit()
    await state.clear()
    await message.answer(f"✅ День: <b>{val}</b>", parse_mode="HTML")
    await _show_fu_detail(message, cid)


@router.callback_query(F.data.startswith("adm_fu_etext_"))
async def followup_edit_text_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    cid = int(callback.data.split("_")[-1])
    await state.set_state(FollowUpStates.edit_text)
    await state.update_data(campaign_id=cid)
    await callback.message.edit_text(
        "✏️ Введите новый текст рассылки (HTML: <b>bold</b>, <i>italic</i>):",
        reply_markup=_fu_cancel_detail_kb(cid),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(FollowUpStates.edit_text, F.text)
async def followup_edit_text_save(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    cid = data["campaign_id"]

    async with async_session() as session:
        c = await session.get(FollowUpCampaign, cid)
        if c:
            c.text = message.text
            await session.commit()
    await state.clear()
    await message.answer("✅ Текст обновлён.")
    await _show_fu_detail(message, cid)


# ── Media ──

@router.callback_query(F.data.startswith("adm_fu_emedia_"))
async def followup_upload_media_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    cid = int(callback.data.split("_")[-1])
    await state.set_state(FollowUpStates.upload_media)
    await state.update_data(campaign_id=cid)
    await callback.message.edit_text(
        "🖼 Отправьте <b>фото или видео</b>.",
        reply_markup=_fu_cancel_detail_kb(cid),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(FollowUpStates.upload_media)
async def followup_upload_media_save(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    if message.photo:
        file_id = message.photo[-1].file_id
        media_type = "photo"
    elif message.video:
        file_id = message.video.file_id
        media_type = "video"
    elif message.animation:
        file_id = message.animation.file_id
        media_type = "video"
    else:
        await message.answer("❌ Отправьте фото или видео.")
        return
    data = await state.get_data()
    cid = data["campaign_id"]

    async with async_session() as session:
        c = await session.get(FollowUpCampaign, cid)
        if c:
            c.media_file_id = file_id
            c.media_type = media_type
            await session.commit()
    await state.clear()
    await message.answer("✅ Медиа сохранено.")
    await _show_fu_detail(message, cid)


@router.callback_query(F.data.startswith("adm_fu_cmedia_"))
async def followup_clear_media(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    cid = int(callback.data.split("_")[-1])

    async with async_session() as session:
        c = await session.get(FollowUpCampaign, cid)
        if c:
            c.media_file_id = None
            c.media_type = None
            await session.commit()
    await callback.answer("Медиа удалено", show_alert=True)
    await _show_fu_detail(callback, cid)


# ── Test campaign to self ──

@router.callback_query(F.data.startswith("adm_fu_test_"))
async def followup_test_self(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    cid = int(callback.data.split("_")[-1])
    async with async_session() as session:
        c = await session.get(FollowUpCampaign, cid)
    if not c:
        await callback.answer("Не найдена", show_alert=True)
        return

    reply_markup = build_user_kb(c.buttons_json)
    tg_id = callback.from_user.id
    try:
        if c.media_file_id and c.media_type:
            send_fn = callback.bot.send_photo if c.media_type == "photo" else callback.bot.send_video
            await send_fn(tg_id, c.media_file_id, caption=c.text, parse_mode="HTML", reply_markup=reply_markup)
        else:
            await callback.bot.send_message(tg_id, c.text, parse_mode="HTML", reply_markup=reply_markup)
        await callback.answer("Отправлено!", show_alert=True)
    except Exception as e:
        await callback.answer(f"Ошибка: {e}", show_alert=True)


# ── Follow-up buttons editor ──

@router.callback_query(F.data.startswith("adm_fu_btns_"))
async def followup_buttons_editor(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    cid = int(callback.data.split("_")[-1])
    async with async_session() as session:
        c = await session.get(FollowUpCampaign, cid)
    if not c:
        await callback.answer("Не найдена", show_alert=True)
        return
    buttons = _parse_buttons(c.buttons_json)
    await state.set_state(FollowUpStates.buttons)
    await state.update_data(campaign_id=cid, buttons=buttons, btn_context="followup")
    await callback.message.edit_text(
        "🔘 <b>Редактор кнопок</b>\n\nДобавьте кнопки или нажмите «Готово».",
        reply_markup=_btn_editor_kb(buttons, f"adm_fubsave_{cid}"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm_fubsave_"))
async def followup_buttons_save(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    cid = int(callback.data.split("_")[-1])
    data = await state.get_data()
    buttons = data.get("buttons", [])
    async with async_session() as session:
        c = await session.get(FollowUpCampaign, cid)
        if c:
            c.buttons_json = _buttons_to_json(buttons)
            await session.commit()
    await state.clear()
    await _show_fu_detail(callback, cid)
    await callback.answer("Кнопки сохранены")


# ── Shared button editor handlers ──
# These work for both FollowUpStates.buttons and MailingStates.buttons

@router.callback_query(F.data == "btn_add", FollowUpStates.buttons)
@router.callback_query(F.data == "btn_add", MailingStates.buttons)
@router.callback_query(F.data == "btn_add", AdminStates.guide_buttons)
async def btn_add(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await callback.message.edit_text(
        "🔘 <b>Выберите тип кнопки:</b>",
        reply_markup=_btn_type_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("btn_del_"), FollowUpStates.buttons)
@router.callback_query(F.data.startswith("btn_del_"), MailingStates.buttons)
@router.callback_query(F.data.startswith("btn_del_"), AdminStates.guide_buttons)
async def btn_delete(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    idx = int(callback.data.split("_")[-1])
    data = await state.get_data()
    buttons = data.get("buttons", [])
    if 0 <= idx < len(buttons):
        buttons.pop(idx)
        await state.update_data(buttons=buttons)
    
    cid = data.get("campaign_id")
    btn_context = data.get("btn_context")
    if btn_context == "followup":
        back_cb = f"adm_fubsave_{cid}"
    elif btn_context == "guide":
        g_platform = data.get("guide_platform")
        back_cb = f"adm_guide_bdone_{g_platform}"
    else:
        back_cb = "adm_ml_btns_done"

    await callback.message.edit_text(
        "🔘 <b>Редактор кнопок</b>\n\nДобавьте кнопки или нажмите «Готово».",
        reply_markup=_btn_editor_kb(buttons, back_cb),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "btn_back_editor", FollowUpStates.buttons)
@router.callback_query(F.data == "btn_back_editor", MailingStates.buttons)
@router.callback_query(F.data == "btn_back_editor", AdminStates.guide_buttons)
async def btn_back_to_editor(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    data = await state.get_data()
    buttons = data.get("buttons", [])
    
    cid = data.get("campaign_id")
    btn_context = data.get("btn_context")
    if btn_context == "followup":
        back_cb = f"adm_fubsave_{cid}"
    elif btn_context == "guide":
        g_platform = data.get("guide_platform")
        back_cb = f"adm_guide_bdone_{g_platform}"
    else:
        back_cb = "adm_ml_btns_done"

    await callback.message.edit_text(
        "🔘 <b>Редактор кнопок</b>\n\nДобавьте кнопки или нажмите «Готово».",
        reply_markup=_btn_editor_kb(buttons, back_cb),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("btn_preset_"), FollowUpStates.buttons)
@router.callback_query(F.data.startswith("btn_preset_"), MailingStates.buttons)
@router.callback_query(F.data.startswith("btn_preset_"), AdminStates.guide_buttons)
async def btn_preset_selected(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    preset = callback.data.replace("btn_preset_", "")
    data = await state.get_data()
    buttons = data.get("buttons", [])
    max_row = max((b.get("row", 1) for b in buttons), default=0)

    btn_context = data.get("btn_context")
    if preset == "url":
        # Need custom text + URL — go to FSM
        if btn_context == "followup":
            await state.set_state(FollowUpStates.btn_text)
        elif btn_context == "guide":
            await state.set_state(AdminStates.guide_btn_text)
        else:
            await state.set_state(MailingStates.btn_text)
        await callback.message.edit_text(
            "✏️ Введите <b>текст кнопки</b>:",
            parse_mode="HTML",
        )
        await callback.answer()
        return

    # Internal preset
    label = INTERNAL_BUTTONS.get(preset, preset)
    new_btn = {"text": label, "type": "callback", "data": preset, "row": max_row + 1}
    buttons.append(new_btn)
    await state.update_data(buttons=buttons)

    cid = data.get("campaign_id")
    if btn_context == "followup":
        back_cb = f"adm_fubsave_{cid}"
    elif btn_context == "guide":
        g_platform = data.get("guide_platform")
        back_cb = f"adm_guide_bdone_{g_platform}"
    else:
        back_cb = "adm_ml_btns_done"

    await callback.message.edit_text(
        "🔘 <b>Редактор кнопок</b>\n\nДобавьте кнопки или нажмите «Готово».",
        reply_markup=_btn_editor_kb(buttons, back_cb),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(FollowUpStates.btn_text, F.text)
@router.message(MailingStates.btn_text, F.text)
@router.message(AdminStates.guide_btn_text, F.text)
async def btn_url_text_entered(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    await state.update_data(btn_text_pending=message.text.strip()[:64])
    data = await state.get_data()
    btn_context = data.get("btn_context")
    if btn_context == "followup":
        await state.set_state(FollowUpStates.btn_url)
    elif btn_context == "guide":
        await state.set_state(AdminStates.guide_btn_url)
    else:
        await state.set_state(MailingStates.btn_url)
    await message.answer("🔗 Введите <b>URL</b> (например https://t.me/channel):", parse_mode="HTML")


@router.message(FollowUpStates.btn_url, F.text)
@router.message(MailingStates.btn_url, F.text)
@router.message(AdminStates.guide_btn_url, F.text)
async def btn_url_entered(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    url = message.text.strip()
    if not url.startswith(("http://", "https://", "tg://")):
        await message.answer("❌ URL должен начинаться с http://, https:// или tg://")
        return
    data = await state.get_data()
    buttons = data.get("buttons", [])
    max_row = max((b.get("row", 1) for b in buttons), default=0)
    btn_text = data.get("btn_text_pending", "Ссылка")
    new_btn = {"text": btn_text, "type": "url", "data": url, "row": max_row + 1}
    buttons.append(new_btn)
    await state.update_data(buttons=buttons)

    # Return to button editor
    btn_context = data.get("btn_context")
    if btn_context == "followup":
        await state.set_state(FollowUpStates.buttons)
    elif btn_context == "guide":
        await state.set_state(AdminStates.guide_buttons)
    else:
        await state.set_state(MailingStates.buttons)

    cid = data.get("campaign_id")
    if btn_context == "followup":
        back_cb = f"adm_fubsave_{cid}"
    elif btn_context == "guide":
        g_platform = data.get("guide_platform")
        back_cb = f"adm_guide_bdone_{g_platform}"
    else:
        back_cb = "adm_ml_btns_done"

    await message.answer(
        "🔘 <b>Редактор кнопок</b>\n\nДобавьте кнопки или нажмите «Готово».",
        reply_markup=_btn_editor_kb(buttons, back_cb),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("btn_noop_"))
async def btn_noop(callback: CallbackQuery) -> None:
    await callback.answer()


# ── Delete ──

@router.callback_query(F.data.startswith("adm_fu_del_"))
async def followup_delete_confirm(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    cid = int(callback.data.split("_")[-1])

    async with async_session() as session:
        c = await session.get(FollowUpCampaign, cid)
    if not c:
        await callback.answer("Не найдена", show_alert=True)
        return
    await callback.message.edit_text(
        f"❗️ Удалить кампанию <b>{html.escape(c.name)}</b>?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"adm_fu_delok_{cid}"),
                InlineKeyboardButton(text="◀️ Нет", callback_data=f"adm_fu_view_{cid}"),
            ]
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm_fu_delok_"))
async def followup_delete_ok(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    cid = int(callback.data.split("_")[-1])

    async with async_session() as session:
        c = await session.get(FollowUpCampaign, cid)
        if c:
            await session.delete(c)
            await session.commit()
    await callback.answer("Удалено", show_alert=True)
    # Show list
    FC = FollowUpCampaign
    async with async_session() as session:
        campaigns = (await session.execute(
            select(FC).order_by(FC.days_after_demo)
        )).scalars().all()
    text = "🔔 <b>Догоняющие рассылки</b>\n\n"
    text += f"Кампаний: {len(campaigns)}" if campaigns else "Нет кампаний."
    await callback.message.edit_text(text, reply_markup=_fu_list_kb(campaigns), parse_mode="HTML")


# Guard: block /commands during follow-up FSM
@router.message(FollowUpStates(), F.text.startswith("/"))
async def _guard_followup(message: Message) -> None:
    command = (message.text or "").split(maxsplit=1)[0].lower()
    if command in ESCAPE_COMMANDS:
        return
    await message.answer("⚠️ Введите значение или нажмите «◀️ Отмена».")


# ── Bulk Key Distribution ─────────────────────────────

BULK_AUDIENCES: dict[str, str] = {
    "all":                  "Всем пользователям",
    "active_subscription":  "Активным подписчикам",
    "inactive_subscription":"С истёкшей подпиской",
    "no_subscription":      "Без подписки",
    "demo_only":            "Только демо-пользователям",
}


def _bulk_audience_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"adm_bk_aud_{key}")]
        for key, label in BULK_AUDIENCES.items()
    ]
    rows.append([InlineKeyboardButton(text="◀️ Отмена", callback_data="adm_mailing")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "adm_bulk_keys")
async def bulk_keys_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await state.clear()
    await callback.message.edit_text(
        "🔑 <b>Массовая выдача ключей</b>\n\nВыберите аудиторию:",
        reply_markup=_bulk_audience_kb(),
        parse_mode="HTML",
    )
    await state.set_state(BulkKeyStates.audience)
    await callback.answer()


@router.callback_query(BulkKeyStates.audience, F.data.startswith("adm_bk_aud_"))
async def bulk_keys_audience(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    audience = callback.data.removeprefix("adm_bk_aud_")
    audience_label = BULK_AUDIENCES.get(audience, audience)
    await state.update_data(bk_audience=audience, bk_audience_label=audience_label)

    from bot.models import Tariff, TariffType
    async with async_session() as session:
        result = await session.execute(
            select(Tariff).where(Tariff.is_active == True).order_by(Tariff.price_rub)  # noqa: E712
        )
        tariffs = result.scalars().all()

    type_labels = {
        TariffType.VPN: "🌐 Весь интернет",
        TariffType.TG_PROXY: "📱 TG-ускоритель",
        TariffType.BOTH: "🔥 Весь интернет + TG",
    }

    rows = []
    current_type = None
    for t in sorted(tariffs, key=lambda x: (x.tariff_type.value, x.price_rub)):
        if t.tariff_type != current_type:
            current_type = t.tariff_type
            rows.append([InlineKeyboardButton(
                text=f"── {type_labels.get(current_type, str(current_type))} ──",
                callback_data="noop",
            )])
        rows.append([InlineKeyboardButton(
            text=f"{t.label} - {t.price_rub}₽",
            callback_data=f"adm_bk_tar_{t.id}",
        )])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm_bulk_keys")])

    await callback.message.edit_text(
        f"🔑 Аудитория: <b>{audience_label}</b>\n\nВыберите тариф для выдачи:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML",
    )
    await state.set_state(BulkKeyStates.tariff)
    await callback.answer()


@router.callback_query(BulkKeyStates.tariff, F.data.startswith("adm_bk_tar_"))
async def bulk_keys_tariff(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    tariff_id = int(callback.data.removeprefix("adm_bk_tar_"))

    from bot.models import Tariff
    async with async_session() as session:
        tariff = await session.get(Tariff, tariff_id)

    if not tariff:
        await callback.answer("Тариф не найден", show_alert=True)
        return

    await state.update_data(bk_tariff_id=tariff_id, bk_tariff_label=tariff.label)

    await callback.message.edit_text(
        f"🔑 Тариф: <b>{tariff.label}</b>\n\n"
        f"Отправьте сопроводительный текст (HTML), который будет отправлен вместе с ключом.\n"
        f"Или отправьте <code>-</code> чтобы отправить без текста.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_bulk_keys")],
        ]),
        parse_mode="HTML",
    )
    await state.set_state(BulkKeyStates.text)
    await callback.answer()


@router.message(BulkKeyStates.text, F.text)
async def bulk_keys_text(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    text = message.text.strip()
    custom_text = "" if text == "-" else text
    await state.update_data(bk_text=custom_text)

    data = await state.get_data()

    # Count target users
    from bot.models import Subscription, SubStatus, User
    async with async_session() as session:
        count = await _count_bulk_audience(session, data["bk_audience"])

    text_preview = custom_text[:200] + "..." if len(custom_text) > 200 else custom_text
    if not text_preview:
        text_preview = "<i>(без текста)</i>"

    await message.answer(
        f"🔑 <b>Массовая выдача ключей — подтверждение</b>\n\n"
        f"Аудитория: <b>{data['bk_audience_label']}</b>\n"
        f"Тариф: <b>{data['bk_tariff_label']}</b>\n"
        f"Получателей: <b>~{count}</b>\n\n"
        f"Текст:\n{text_preview}\n\n"
        f"Подтвердить отправку?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Отправить", callback_data="adm_bk_send"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="adm_mailing"),
            ],
        ]),
        parse_mode="HTML",
    )
    await state.set_state(BulkKeyStates.confirmation)


async def _count_bulk_audience(session, audience: str) -> int:
    from bot.models import Subscription, SubStatus, User

    if audience == "all":
        return await session.scalar(
            select(func.count(User.id)).where(User.is_blocked == False)  # noqa: E712
        ) or 0
    elif audience == "active_subscription":
        return await session.scalar(
            select(func.count(func.distinct(Subscription.user_id))).where(
                Subscription.status == SubStatus.ACTIVE,
                Subscription.billing_mode != "demo",
            )
        ) or 0
    elif audience == "inactive_subscription":
        sub_users = select(Subscription.user_id).where(
            Subscription.status == SubStatus.ACTIVE
        ).distinct()
        return await session.scalar(
            select(func.count(func.distinct(Subscription.user_id))).where(
                Subscription.status == SubStatus.EXPIRED,
                ~Subscription.user_id.in_(sub_users),
            )
        ) or 0
    elif audience == "no_subscription":
        has_sub = select(Subscription.user_id).distinct()
        return await session.scalar(
            select(func.count(User.id)).where(
                User.is_blocked == False,  # noqa: E712
                ~User.id.in_(has_sub),
            )
        ) or 0
    elif audience == "demo_only":
        paid_users = select(Subscription.user_id).where(
            Subscription.billing_mode != "demo",
        ).distinct()
        demo_users_q = select(func.distinct(Subscription.user_id)).where(
            Subscription.billing_mode == "demo",
            ~Subscription.user_id.in_(paid_users),
        )
        return await session.scalar(
            select(func.count()).select_from(demo_users_q.subquery())
        ) or 0
    return 0


async def _get_bulk_user_ids(session, audience: str) -> list[tuple[int, int]]:
    """Return list of (user.id, user.telegram_id) for the audience."""
    from bot.models import Subscription, SubStatus, User

    if audience == "all":
        result = await session.execute(
            select(User.id, User.telegram_id).where(User.is_blocked == False)  # noqa: E712
        )
    elif audience == "active_subscription":
        result = await session.execute(
            select(User.id, User.telegram_id).where(
                User.id.in_(
                    select(func.distinct(Subscription.user_id)).where(
                        Subscription.status == SubStatus.ACTIVE,
                        Subscription.billing_mode != "demo",
                    )
                ),
                User.is_blocked == False,  # noqa: E712
            )
        )
    elif audience == "inactive_subscription":
        active_users = select(Subscription.user_id).where(
            Subscription.status == SubStatus.ACTIVE
        ).distinct()
        expired_users = select(func.distinct(Subscription.user_id)).where(
            Subscription.status == SubStatus.EXPIRED,
            ~Subscription.user_id.in_(active_users),
        )
        result = await session.execute(
            select(User.id, User.telegram_id).where(
                User.id.in_(expired_users),
                User.is_blocked == False,  # noqa: E712
            )
        )
    elif audience == "no_subscription":
        has_sub = select(Subscription.user_id).distinct()
        result = await session.execute(
            select(User.id, User.telegram_id).where(
                ~User.id.in_(has_sub),
                User.is_blocked == False,  # noqa: E712
            )
        )
    elif audience == "demo_only":
        paid_users = select(Subscription.user_id).where(
            Subscription.billing_mode != "demo",
        ).distinct()
        demo_users_q = select(func.distinct(Subscription.user_id)).where(
            Subscription.billing_mode == "demo",
            ~Subscription.user_id.in_(paid_users),
        )
        result = await session.execute(
            select(User.id, User.telegram_id).where(
                User.id.in_(demo_users_q),
                User.is_blocked == False,  # noqa: E712
            )
        )
    else:
        return []

    return list(result.all())


@router.callback_query(BulkKeyStates.confirmation, F.data == "adm_bk_send")
async def bulk_keys_send(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return

    data = await state.get_data()
    await state.clear()

    audience = data["bk_audience"]
    tariff_id = data["bk_tariff_id"]
    custom_text = data.get("bk_text", "")

    await callback.message.edit_text(
        "⏳ <b>Начинаю массовую выдачу ключей...</b>\n\n"
        "Это может занять некоторое время. Отчёт придёт по завершении.",
        parse_mode="HTML",
    )
    await callback.answer()

    import asyncio
    asyncio.create_task(
        _execute_bulk_keys(callback.bot, audience, tariff_id, custom_text)
    )


async def _execute_bulk_keys(bot, audience: str, tariff_id: int, custom_text: str) -> None:
    """Background task: generate keys for each user and send them."""
    import asyncio
    from datetime import datetime, timedelta

    from bot.models import (
        Platform,
        Subscription,
        SubStatus,
        Tariff,
        TariffType,
        User,
    )
    from bot.services.subscription_service import (
        create_mtproto_subscription,
        create_or_extend_paid_subscription,
        get_primary_active_server,
    )
    from bot.services.mtproto_manager import restart_proxies
    from bot.utils.texts import KEY_DELIVERED, MTPROTO_KEY_BULK
    from bot.keyboards.client import back_to_menu_kb

    success = 0
    failed = 0
    skipped = 0
    need_proxy_restart = False

    async with async_session() as session:
        tariff = await session.get(Tariff, tariff_id)
        if not tariff:
            for admin_id in settings.admin_ids:
                try:
                    await bot.send_message(admin_id, "❌ Массовая выдача: тариф не найден.")
                except Exception:
                    pass
            return

        user_list = await _get_bulk_user_ids(session, audience)

    total = len(user_list)
    logger.info(
        "Bulk key generation started: audience=%s tariff_id=%s total_users=%s custom_text=%s",
        audience,
        tariff_id,
        total,
        bool(custom_text),
    )
    is_tg_proxy_only = tariff.tariff_type == TariffType.TG_PROXY
    is_both = tariff.tariff_type == TariffType.BOTH

    # Phase 1: generate all keys and subscriptions (without restarting proxies each time)
    user_results: list[tuple[int, str | None, str | None, str]] = []  # (tg_id, vpn_key, proxy_link, expires)

    for idx, (user_db_id, telegram_id) in enumerate(user_list):
        try:
            async with async_session() as session:
                user = await session.get(User, user_db_id)
                if not user:
                    skipped += 1
                    continue

                platform = user.platform or Platform.ANDROID
                vpn_key = None
                proxy_link = None
                subscription = None

                # VPN part
                if not is_tg_proxy_only:
                    subscription, vpn_key = await create_or_extend_paid_subscription(
                        session, user=user, tariff=tariff, platform=platform,
                    )
                    if not subscription:
                        failed += 1
                        continue

                # MTProto part (add secrets WITHOUT restarting proxy)
                if is_tg_proxy_only or is_both:
                    if is_tg_proxy_only:
                        server = await get_primary_active_server(session)
                        if not server:
                            failed += 1
                            continue
                        now = datetime.utcnow()
                        expires_at = now + timedelta(days=tariff.days)
                        client_name = f"mtproto_tg{user.telegram_id}"
                        included_slots = await get_included_device_slots(session)
                        subscription = Subscription(
                            user_id=user.id,
                            server_id=server.id,
                            tariff_months=tariff.days // 30,
                            tariff_days=tariff.days,
                            vpn_key=None,
                            client_name=client_name,
                            platform=Platform.ANDROID,
                            device_slots=included_slots,
                            expires_at=expires_at,
                        )
                        session.add(subscription)
                        await session.flush()

                    mtproto_account, proxy_link = await create_mtproto_subscription(
                        session, user=user, tariff=tariff, subscription=subscription,
                        restart_proxy=False,
                    )
                    if mtproto_account:
                        need_proxy_restart = True

                await session.commit()
                expires_str = subscription.expires_at.strftime("%d.%m.%Y") if subscription else "N/A"

            user_results.append((telegram_id, vpn_key, proxy_link, expires_str))
            success += 1

        except Exception as e:
            logger.error(f"Bulk key failed for user {telegram_id}: {e}")
            failed += 1

        # Rate-limit Marzban API calls (prevent overload on large batches)
        await asyncio.sleep(0.3)

        # Progress every 50 users
        if (idx + 1) % 50 == 0:
            for admin_id in settings.admin_ids:
                try:
                    await bot.send_message(
                        admin_id,
                        f"⏳ Генерация ключей: {idx + 1}/{total} "
                        f"(✅ {success} / ❌ {failed} / ⏭ {skipped})",
                    )
                except Exception:
                    pass

    # Phase 2: restart proxies ONCE after all secrets are added
    if need_proxy_restart:
        logger.info("Restarting MTProto proxies after bulk key generation...")
        await restart_proxies()
        await asyncio.sleep(3)  # Let proxies fully start

    # Phase 3: send messages to all users
    sent = 0
    for telegram_id, vpn_key, proxy_link, expires_str in user_results:
        try:
            # Send custom text first
            if custom_text:
                try:
                    await bot.send_message(telegram_id, custom_text, parse_mode="HTML")
                except Exception:
                    pass

            # Send VPN key
            if vpn_key:
                try:
                    await bot.send_message(
                        telegram_id,
                        KEY_DELIVERED.format(
                            key=vpn_key[:200] + "..." if len(vpn_key) > 200 else vpn_key,
                            expires=expires_str,
                        ),
                        parse_mode="HTML",
                    )
                    await bot.send_message(
                        telegram_id,
                        f"📋 <b>Полный ключ:</b>\n\n<code>{vpn_key}</code>",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

            # Send MTProto proxy link (use bulk template without "Оплата прошла")
            if proxy_link:
                try:
                    await bot.send_message(
                        telegram_id,
                        MTPROTO_KEY_BULK.format(proxy_links=proxy_link, expires=expires_str),
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                except Exception:
                    pass

            sent += 1
        except Exception as e:
            logger.error(f"Bulk send failed for user {telegram_id}: {e}")

        # Rate limiting
        await asyncio.sleep(0.1)

    # Final report
    logger.info(
        "Bulk key generation finished: audience=%s tariff_id=%s generated_success=%s failed=%s skipped=%s messages_sent=%s",
        audience,
        tariff_id,
        success,
        failed,
        skipped,
        sent,
    )
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(
                admin_id,
                f"✅ <b>Массовая выдача завершена</b>\n\n"
                f"Тариф: <b>{tariff.label}</b>\n"
                f"Всего: {total}\n"
                f"Ключей создано: {success}\n"
                f"Сообщений отправлено: {sent}\n"
                f"Ошибок: {failed}\n"
                f"Пропущено: {skipped}",
                parse_mode="HTML",
            )
        except Exception:
            pass


# Guard: block /commands during bulk key FSM
@router.message(BulkKeyStates(), F.text.startswith("/"))
async def _guard_bulk_keys(message: Message) -> None:
    command = (message.text or "").split(maxsplit=1)[0].lower()
    if command in ESCAPE_COMMANDS:
        return
    await message.answer("⚠️ Введите значение или нажмите «◀️ Отмена».")
