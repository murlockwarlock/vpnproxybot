"""Admin panel for RAG AI Settings (Temperature, Prompt, Documents)."""

from __future__ import annotations

import html
import io
import logging
import os
import uuid

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.config import settings
from bot.database import async_session
from bot.models import RAGConfig, RAGDocument
from bot.services import rag_service

logger = logging.getLogger(__name__)

router = Router(name="admin_ai")
ESCAPE_COMMANDS = {"/start", "/help", "/admin", "/policy", "/agree", "/oferta"}


class AdminAIStates(StatesGroup):
    waiting_system_prompt = State()
    waiting_prompt_file = State()
    waiting_temperature = State()
    waiting_document = State()


@router.message(Command("start"), AdminAIStates())
async def _admin_ai_state_start(message: Message, state: FSMContext) -> None:
    from bot.handlers.start import cmd_start

    await cmd_start(message, state)


@router.message(Command("help"), AdminAIStates())
async def _admin_ai_state_help(message: Message, state: FSMContext) -> None:
    from bot.handlers.start import show_help_command

    await show_help_command(message, state)


@router.message(Command("admin"), AdminAIStates())
async def _admin_ai_state_admin(message: Message, state: FSMContext) -> None:
    from bot.handlers.admin import cmd_admin

    await cmd_admin(message, state)


@router.message(Command("policy"), AdminAIStates())
async def _admin_ai_state_policy(message: Message, state: FSMContext) -> None:
    from bot.handlers.start import show_policy_command

    await state.clear()
    await show_policy_command(message)


@router.message(Command("agree"), AdminAIStates())
async def _admin_ai_state_agree(message: Message, state: FSMContext) -> None:
    from bot.handlers.start import show_agree_command

    await state.clear()
    await show_agree_command(message)


@router.message(Command("oferta"), AdminAIStates())
async def _admin_ai_state_oferta(message: Message, state: FSMContext) -> None:
    from bot.handlers.start import show_oferta_command

    await state.clear()
    await show_oferta_command(message)


@router.message(AdminAIStates(), F.text.startswith("/"))
async def _guard_admin_ai_commands(message: Message) -> None:
    command = (message.text or "").split(maxsplit=1)[0].lower()
    if command in ESCAPE_COMMANDS:
        return
    await message.answer("⚠️ Завершите текущее действие или нажмите «◀️ Назад».")


def _is_admin(user_id: int) -> bool:
    return settings.is_admin(user_id)


def ai_settings_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="✏️ Изменить промпт", callback_data="ai_set_prompt"),
        InlineKeyboardButton(text="📥 Скачать промпт", callback_data="ai_download_prompt"),
    )
    b.row(
        InlineKeyboardButton(text="🌡 Изменить t°", callback_data="ai_set_temp"),
    )
    b.row(
        InlineKeyboardButton(text="📄 Загрузить документ", callback_data="ai_upload_doc"),
        InlineKeyboardButton(text="📚 База знаний", callback_data="ai_list_docs"),
    )
    b.row(InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back"))
    return b.as_markup()


def ai_prompt_edit_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="📤 Загрузить из файла (.txt / .md)", callback_data="ai_upload_prompt_file"))
    b.row(InlineKeyboardButton(text="✏️ Ввести вручную (короткий промпт)", callback_data="ai_type_prompt"))
    b.row(InlineKeyboardButton(text="◀️ Назад", callback_data="adm_ai_settings"))
    return b.as_markup()


def ai_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад к ИИ", callback_data="adm_ai_settings")]]
    )


@router.callback_query(F.data == "adm_ai_settings")
async def ai_settings_menu(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await state.clear()

    async with async_session() as session:
        rag_config = await session.get(RAGConfig, 1)
        if not rag_config:
            rag_config = RAGConfig(
                system_prompt="Ты - умный и вежливый AI-помощник сервиса.",
                temperature=0.7
            )
            session.add(rag_config)
            await session.commit()
            
        system_prompt = rag_config.system_prompt
        temperature = rag_config.temperature

    preview = html.escape(system_prompt[:300]) + ("…" if len(system_prompt) > 300 else "")
    text = (
        "🤖 <b>Настройки ИИ-ассистента</b>\n\n"
        f"<b>Системный промпт</b> ({len(system_prompt)} симв.):\n<i>{preview}</i>\n\n"
        f"<b>Temperature:</b> <code>{temperature}</code>\n"
        "<i>(низкая = точнее, высокая = креативнее)</i>\n\n"
        "Выберите действие:"
    )

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=ai_settings_kb())
    await callback.answer()


@router.callback_query(F.data == "ai_set_prompt")
async def ai_ask_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await state.clear()
    await callback.message.edit_text(
        "📝 <b>Изменение системного промпта</b>\n\n"
        "Рекомендуется загружать промпт из файла — так нет ограничений на длину.\n"
        "Можно также ввести короткий промпт вручную.",
        parse_mode="HTML",
        reply_markup=ai_prompt_edit_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "ai_download_prompt")
async def ai_download_prompt(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    async with async_session() as session:
        rag_config = await session.get(RAGConfig, 1)
        prompt_text = rag_config.system_prompt if rag_config else ""

    file_bytes = prompt_text.encode("utf-8")
    await callback.message.answer_document(
        BufferedInputFile(file_bytes, filename="system_prompt.txt"),
        caption="📥 Текущий системный промпт",
    )
    await callback.answer()


@router.callback_query(F.data == "ai_upload_prompt_file")
async def ai_upload_prompt_file_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await state.set_state(AdminAIStates.waiting_prompt_file)
    await callback.message.edit_text(
        "📤 Отправьте файл с промптом (.txt или .md).\n"
        "Весь текст файла станет новым системным промптом.",
        reply_markup=ai_back_kb(),
    )
    await callback.answer()


@router.message(AdminAIStates.waiting_prompt_file, F.document)
async def ai_save_prompt_file(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    doc = message.document
    ext = os.path.splitext(doc.file_name or "")[1].lower()
    if ext not in (".txt", ".md"):
        await message.answer("❌ Нужен файл .txt или .md")
        return

    file_info = await message.bot.get_file(doc.file_id)
    buf = io.BytesIO()
    await message.bot.download_file(file_info.file_path, destination=buf)
    prompt_text = buf.getvalue().decode("utf-8").strip()

    if not prompt_text:
        await message.answer("❌ Файл пустой.")
        return

    async with async_session() as session:
        rag_config = await session.get(RAGConfig, 1)
        if rag_config:
            rag_config.system_prompt = prompt_text
            await session.commit()

    await state.clear()
    await message.answer(
        f"✅ Промпт загружен из файла <b>{doc.file_name}</b> ({len(prompt_text)} симв.)",
        parse_mode="HTML",
        reply_markup=ai_back_kb(),
    )


@router.callback_query(F.data == "ai_type_prompt")
async def ai_type_prompt_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await state.set_state(AdminAIStates.waiting_system_prompt)
    await callback.message.edit_text(
        "✏️ Введите новый системный промпт текстом:",
        reply_markup=ai_back_kb(),
    )
    await callback.answer()


@router.message(AdminAIStates.waiting_system_prompt)
async def ai_save_prompt(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id) or not message.text:
        return

    async with async_session() as session:
        rag_config = await session.get(RAGConfig, 1)
        if rag_config:
            rag_config.system_prompt = message.text
            await session.commit()

    await state.clear()
    await message.answer(
        f"✅ Системный промпт сохранён ({len(message.text)} симв.)",
        reply_markup=ai_back_kb(),
    )


@router.callback_query(F.data == "ai_set_temp")
async def ai_ask_temp(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await state.set_state(AdminAIStates.waiting_temperature)
    await callback.message.edit_text(
        "🌡 Введите Temperature (от 0.0 до 2.0):\nПример: 0.7",
        reply_markup=ai_back_kb()
    )
    await callback.answer()


@router.message(AdminAIStates.waiting_temperature)
async def ai_save_temp(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id) or not message.text:
        return

    try:
        temp = float(message.text.replace(",", "."))
        if not (0.0 <= temp <= 2.0):
            raise ValueError
    except ValueError:
        await message.answer("❌ Ошибка: введите число от 0.0 до 2.0.")
        return

    async with async_session() as session:
        rag_config = await session.get(RAGConfig, 1)
        if rag_config:
            rag_config.temperature = temp
            await session.commit()

    await state.clear()
    await message.answer(f"✅ Temperature сохранена: {temp}", reply_markup=ai_back_kb())


@router.callback_query(F.data == "ai_upload_doc")
async def ai_ask_doc(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await state.set_state(AdminAIStates.waiting_document)
    await callback.message.edit_text(
        "📄 Отправьте файл для Базы Знаний ИИ.\nФорматы: .txt, .doc, .docx",
        reply_markup=ai_back_kb()
    )
    await callback.answer()


@router.message(AdminAIStates.waiting_document, F.document)
async def ai_save_doc(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return

    doc = message.document
    if not doc.file_name:
        return
    
    ext = os.path.splitext(doc.file_name)[1].lower()
    if ext not in [".txt", ".doc", ".docx"]:
        await message.answer("❌ Неподдерживаемый формат. Жду .txt, .doc, или .docx")
        return

    await message.answer("⏳ Скачиваю и обрабатываю файл...")

    os.makedirs("downloads", exist_ok=True)
    temp_path = f"downloads/{uuid.uuid4().hex[:8]}_{doc.file_name}"
    
    file_info = await message.bot.get_file(doc.file_id)
    await message.bot.download_file(file_info.file_path, destination=temp_path)

    try:
        chunks_added = await rag_service.process_document(temp_path, doc.file_name)
        
        async with async_session() as session:
            new_doc = RAGDocument(filename=doc.file_name)
            session.add(new_doc)
            await session.commit()
            
        await message.answer(
            f"✅ Документ <b>{doc.file_name}</b> загружен в ChromaDB!\n"
            f"Разбит на {chunks_added} фрагментов.",
            parse_mode="HTML",
            reply_markup=ai_back_kb()
        )
    except Exception as e:
        logger.error(f"Failed to process RAG doc: {e}")
        await message.answer(f"❌ Ошибка обработки: {e}", reply_markup=ai_back_kb())
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
    
    await state.clear()


@router.callback_query(F.data == "ai_list_docs")
async def ai_list_docs(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return

    from sqlalchemy import select
    async with async_session() as session:
        result = await session.execute(select(RAGDocument).order_by(RAGDocument.uploaded_at.desc()))
        docs = result.scalars().all()

    if not docs:
        await callback.message.edit_text("База знаний пуста.", reply_markup=ai_back_kb())
        await callback.answer()
        return

    text = "📚 <b>Загруженные документы (База знаний ИИ):</b>\n\n"
    for d in docs:
        date_str = d.uploaded_at.strftime("%Y-%m-%d %H:%M")
        text += f"• <code>{d.filename}</code> <i>({date_str})</i>\n"

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=ai_back_kb())
    await callback.answer()
