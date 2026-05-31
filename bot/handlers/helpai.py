"""AI Assistant handler - /helpai and chatting state."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from bot.database import async_session
from bot.models import Subscription, SubStatus, User
from bot.services import rag_service
from bot.services.balance_service import get_user_balance
from bot.utils.html_utils import markdown_to_html, split_html_text

logger = logging.getLogger(__name__)

router = Router(name="helpai")


class HelpAIStates(StatesGroup):
    chatting = State()


def exit_ai_kb() -> InlineKeyboardMarkup:
    """Keyboard to exit AI mode."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚪 Выйти из режима ИИ", callback_data="exit_ai")]
        ]
    )


_AI_WELCOME = (
    "🤖 <b>Привет! Я ИИ-помощник службы поддержки.</b>\n\n"
    "Задайте мне любой вопрос по оплате, настройке или работе сервиса — постараюсь помочь!\n\n"
    "<i>Для выхода нажмите кнопку ниже или введите /start.</i>"
)


_MAX_HISTORY = 20  # последних сообщений (10 пар вопрос/ответ)


async def _build_user_context(telegram_id: int) -> str | None:
    """Build a short summary of the user's profile for AI context."""
    try:
        async with async_session() as session:
            result = await session.execute(
                select(User).where(User.telegram_id == telegram_id)
            )
            user = result.scalar_one_or_none()
            if not user:
                return None

            lines: list[str] = []
            balance = get_user_balance(user)
            lines.append(f"- Баланс: {balance:.2f} ₽")

            if user.balance_mode_enabled:
                status = "включены" if user.balance_autodebit_enabled else "выключены"
                lines.append(f"- Ежедневные списания: {status}")
                if user.next_daily_charge_at:
                    label = "Следующее списание" if user.balance_autodebit_enabled else "Доступ до"
                    dt_str = user.next_daily_charge_at.strftime("%d.%m.%Y %H:%M")
                    lines.append(f"- {label}: {dt_str} МСК")

            subs_result = await session.execute(
                select(Subscription)
                .where(Subscription.user_id == user.id, Subscription.status == SubStatus.ACTIVE)
                .order_by(Subscription.expires_at.desc())
            )
            subs = subs_result.scalars().all()
            if subs:
                for s in subs:
                    days_left = max(0, (s.expires_at - datetime.now(timezone.utc).replace(tzinfo=None)).days)
                    mode = "баланс" if s.billing_mode == "balance" else "тариф"
                    lines.append(
                        f"- Подписка (id={s.id}): {mode}, до {s.expires_at.strftime('%d.%m.%Y')}, "
                        f"осталось {days_left} дн., устройств: {s.device_slots}"
                    )
            else:
                lines.append("- Активных подписок нет")

            return "\n".join(lines)
    except Exception as e:
        logger.warning(f"Failed to build user context for AI: {e}")
        return None


def _clear_state_data(state_data: dict) -> dict:
    return {"history": []}


@router.message(Command("helpai"))
async def cmd_helpai(message: Message, state: FSMContext) -> None:
    """Enter AI chat mode via /helpai command."""
    await state.set_state(HelpAIStates.chatting)
    await state.update_data(history=[])
    await message.answer(_AI_WELCOME, parse_mode="HTML", reply_markup=exit_ai_kb())


@router.callback_query(F.data == "help_ai_start")
async def cb_helpai(callback: CallbackQuery, state: FSMContext) -> None:
    """Enter AI chat mode via inline button from help screen."""
    await state.set_state(HelpAIStates.chatting)
    await state.update_data(history=[])
    try:
        await callback.message.edit_text(_AI_WELCOME, parse_mode="HTML", reply_markup=exit_ai_kb())
    except Exception:
        await callback.message.answer(_AI_WELCOME, parse_mode="HTML", reply_markup=exit_ai_kb())
    await callback.answer()


@router.callback_query(F.data == "exit_ai", HelpAIStates.chatting)
async def exit_ai_callback(callback: CallbackQuery, state: FSMContext) -> None:
    """Exit AI chat mode via button."""
    await state.clear()
    await callback.message.edit_text(
        "🚪 Вы вышли из режима общения с ИИ.\nНажмите /start чтобы открыть главное меню."
    )
    await callback.answer()


@router.message(Command("start"), HelpAIStates.chatting)
async def helpai_cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    from bot.handlers.start import cmd_start

    await cmd_start(message, state)


@router.message(Command("help"), HelpAIStates.chatting)
async def helpai_cmd_help(message: Message, state: FSMContext) -> None:
    await state.clear()
    from bot.handlers.start import show_help_command

    await show_help_command(message, state)


@router.message(Command("admin"), HelpAIStates.chatting)
async def helpai_cmd_admin(message: Message, state: FSMContext) -> None:
    await state.clear()
    from bot.handlers.admin import cmd_admin

    await cmd_admin(message, state)


@router.message(Command("policy"), HelpAIStates.chatting)
async def helpai_cmd_policy(message: Message, state: FSMContext) -> None:
    await state.clear()
    from bot.handlers.start import show_policy_command

    await show_policy_command(message)


@router.message(Command("agree"), HelpAIStates.chatting)
async def helpai_cmd_agree(message: Message, state: FSMContext) -> None:
    await state.clear()
    from bot.handlers.start import show_agree_command

    await show_agree_command(message)


@router.message(Command("oferta"), HelpAIStates.chatting)
async def helpai_cmd_oferta(message: Message, state: FSMContext) -> None:
    await state.clear()
    from bot.handlers.start import show_oferta_command

    await show_oferta_command(message)


@router.message(HelpAIStates.chatting)
async def ai_chat_message(message: Message, state: FSMContext) -> None:
    """Handle messages sent to the AI."""
    if not message.text:
        return

    if message.text.startswith("/"):
        return

    data = await state.get_data()
    history: list[dict] = data.get("history", [])

    # Keep "typing..." alive while waiting for DeepSeek
    async def _keep_typing():
        while True:
            await message.bot.send_chat_action(message.chat.id, "typing")
            await asyncio.sleep(4)

    typing_task = asyncio.create_task(_keep_typing())
    try:
        user_context = await _build_user_context(message.from_user.id)
        answer = await rag_service.ask_deepseek(
            message.text, history=history, user_context=user_context,
        )
    finally:
        typing_task.cancel()

    # Update history
    history.append({"role": "user", "content": message.text})
    history.append({"role": "assistant", "content": answer})
    if len(history) > _MAX_HISTORY:
        history = history[-_MAX_HISTORY:]
    await state.update_data(history=history)

    html_answer = markdown_to_html(answer)
    chunks = split_html_text(html_answer)
    for i, chunk in enumerate(chunks):
        await message.answer(
            chunk,
            parse_mode="HTML",
            reply_markup=exit_ai_kb() if i == len(chunks) - 1 else None,
        )
