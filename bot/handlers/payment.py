"""Payment handler - Stars invoicing, pre-checkout, successful payment → key delivery."""

from __future__ import annotations

import asyncio
import logging
import math
import uuid
from datetime import datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    LabeledPrice, Message, PreCheckoutQuery,
)
from sqlalchemy import select

from bot.config import settings
from bot.database import async_session
from bot.keyboards.client import back_to_menu_kb, balance_menu_kb, balance_payment_kb
from bot.models import (
    BalanceTransactionKind,
    BalanceTopUp,
    Payment,
    PaymentMethod,
    PaymentStatus,
    Platform,
    RobokassaPayment,
    Server,
    Subscription,
    Tariff,
    TariffType,
    User,
    ProxyAccount,
)
from bot.services.balance_service import credit_user_balance, debit_user_balance, get_daily_charge_rub, get_user_balance
from bot.services import vpn_manager
from bot.services.client_names import build_client_name
from bot.services.device_slots import get_included_device_slots, get_max_device_slots
from bot.services.notifications import notify_admins_issue, notify_admins_payment, notify_expiring, notify_staff_text
from bot.services.payment_logger import plog
from bot.services.payment_service import (
    create_stars_invoice,
    create_telegram_pay_invoice,
    create_yookassa_payment,
    credit_referral,
    generate_robokassa_url,
    log_referral_payment,
)
from bot.services.provisioning_issues import AccessProvisionError
from bot.services.subscription_service import (
    create_mtproto_subscription,
    create_or_extend_paid_access,
)
from bot.services.purchase_intent import decode_intent, get_purchase_price_rub
from bot.services.tariff_rules import (
    INTRO_BASIC_ALREADY_USED_TEXT,
    can_purchase_intro_basic_tariff,
)
from bot.services.vhq_routing import is_vhq_tariff
from bot.utils.texts import (
    ERROR,
    GUIDE_ANDROID,
    GUIDE_IOS,
    GUIDE_MAC,
    GUIDE_WINDOWS,
    GUIDE_ANDROID_TV,
    KEY_DELIVERED,
    MTPROTO_KEY_DELIVERED,
)

logger = logging.getLogger(__name__)
router = Router(name="payment")


class BalanceTopUpStates(StatesGroup):
    waiting_amount = State()


# ── Helpers ────────────────────────────────────────────

async def _get_tariff(tariff_id: int) -> Tariff | None:
    async with async_session() as session:
        return await session.get(Tariff, tariff_id)


def _platform_from_str(platform_str: str) -> Platform:
    platform_str, _, _ = decode_intent(platform_str)
    return {
        "android": Platform.ANDROID,
        "ios": Platform.IOS,
        "mac": Platform.MAC,
        "windows": Platform.WINDOWS,
        "android_tv": Platform.ANDROID_TV,
        "tgproxy": Platform.ANDROID,
    }.get(platform_str, Platform.ANDROID)


def _is_deferred_platform(platform_str: str) -> bool:
    platform_str, _, _ = decode_intent(platform_str)
    return platform_str in {"deferred", "after_payment", "pending"}


def _purchase_access_kwargs(platform_str: str) -> dict:
    _, action, target_subscription_id = decode_intent(platform_str)
    if action == "auto":
        return {}
    return {
        "purchase_action": action,
        "target_subscription_id": target_subscription_id,
    }


def _delivery_platform_kb(subscription_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🤖 Android", callback_data=f"deliver_{subscription_id}_android"),
            InlineKeyboardButton(text="🍎 iOS", callback_data=f"deliver_{subscription_id}_ios"),
        ],
        [
            InlineKeyboardButton(text="💻 Windows", callback_data=f"deliver_{subscription_id}_windows"),
            InlineKeyboardButton(text="🍏 Mac", callback_data=f"deliver_{subscription_id}_mac"),
        ],
        [InlineKeyboardButton(text="📺 Android TV", callback_data=f"deliver_{subscription_id}_android_tv")],
    ])


async def _ask_platform_before_key(message_or_callback, subscription_id: int) -> None:
    text = (
        "✅ <b>Оплата прошла.</b>\n\n"
        "Выберите устройство, и я отправлю ключ вместе с подходящим гайдом."
    )
    if isinstance(message_or_callback, CallbackQuery):
        await message_or_callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=_delivery_platform_kb(subscription_id),
        )
    else:
        await message_or_callback.answer(
            text,
            parse_mode="HTML",
            reply_markup=_delivery_platform_kb(subscription_id),
        )


async def _get_key_change_explanation(session, user_id: int, current_sub_id: int, current_client_name: str) -> str | None:
    if not current_client_name or not current_client_name.startswith("vhq_"):
        return None
    previous = await session.scalar(
        select(Subscription)
        .where(Subscription.user_id == user_id)
        .where(Subscription.id < current_sub_id)
        .where(Subscription.client_name.like("adapt_%"))
        .order_by(Subscription.id.desc())
        .limit(1)
    )
    if previous:
        return (
            "ℹ️ <b>Почему ключ другой?</b> Для выбранного тарифа нужна отдельная ссылка, "
            "поэтому для вас был сгенерирован новый, более быстрый ключ. "
            "Старый ключ демо-доступа больше не понадобится."
        )
    return None


async def _send_subscription_key_for_platform(
    bot,
    chat_id: int,
    telegram_id: int,
    subscription_id: int,
    platform: Platform,
) -> bool:
    async with async_session() as session:
        subscription = await session.get(Subscription, subscription_id)
        if not subscription:
            return False
        user = await session.get(User, subscription.user_id)
        if not user or int(user.telegram_id) != int(telegram_id):
            return False
        subscription.platform = platform
        user.platform = platform
        vpn_key = subscription.vpn_key
        client_name = subscription.client_name
        expires_str = subscription.expires_at.strftime("%d.%m.%Y") if subscription.expires_at else "N/A"
        explanation = await _get_key_change_explanation(session, user.id, subscription.id, client_name)
        await session.commit()

    if not vpn_key:
        return False

    key_display = vpn_key if len(vpn_key) <= 200 else vpn_key[:200] + "..."
    await bot.send_message(
        chat_id,
        KEY_DELIVERED.format(key=key_display, expires=expires_str),
        parse_mode="HTML",
    )
    if explanation:
        await bot.send_message(chat_id, explanation, parse_mode="HTML")
    
    await bot.send_message(
        chat_id,
        f"📋 <b>Полный ключ (нажмите чтобы скопировать):</b>\n\n<code>{vpn_key}</code>",
        parse_mode="HTML",
    )
    guides = {
        Platform.ANDROID: GUIDE_ANDROID,
        Platform.IOS: GUIDE_IOS,
        Platform.MAC: GUIDE_MAC,
        Platform.WINDOWS: GUIDE_WINDOWS,
        Platform.ANDROID_TV: GUIDE_ANDROID_TV,
    }
    from bot.services.guide_service import send_guide
    await send_guide(
        bot,
        chat_id,
        platform,
        guides.get(platform, GUIDE_ANDROID),
        reply_markup=back_to_menu_kb(),
    )
    return True


def _parse_pay_callback(data: str) -> tuple[int, str, int]:
    """Parse pay callback data: pay_{method}_{tariff_id}_{platform}_{use_bal}.

    Platform may contain underscores (e.g. android_tv, tg_proxy),
    so we take tariff_id as parts[2], use_bal as last part, and
    everything in between as the platform string.

    Returns (tariff_id, platform_str, use_bal).
    """
    parts = data.split("_")
    # parts[0] = "pay", parts[1] = method, parts[2] = tariff_id
    tariff_id = int(parts[2])
    # Last part is use_bal (0 or 1) if there are more than 4 parts
    # Format: pay_method_id_platform[_subplatform]_usebal
    # Minimum: pay_stars_1_android_0 (5 parts)
    # With compound platform: pay_stars_1_android_tv_0 (6 parts)
    use_bal = int(parts[-1])
    platform = "_".join(parts[3:-1])
    return tariff_id, platform, use_bal


TOPUP_MIN_RUB = 70
TOPUP_MAX_RUB = 50000


@router.callback_query(F.data.startswith("deliver_"))
async def deliver_paid_key_for_platform(callback: CallbackQuery) -> None:
    parts = callback.data.split("_")
    if len(parts) < 3:
        await callback.answer("Некорректный выбор", show_alert=True)
        return
    try:
        subscription_id = int(parts[1])
    except ValueError:
        await callback.answer("Некорректный ключ", show_alert=True)
        return
    platform = _platform_from_str("_".join(parts[2:]))
    delivered = await _send_subscription_key_for_platform(
        callback.bot,
        callback.message.chat.id,
        callback.from_user.id,
        subscription_id,
        platform,
    )
    if not delivered:
        await callback.answer("Ключ не найден. Напишите в поддержку.", show_alert=True)
        return
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer("Ключ отправлен")


def _payment_method_from_currency(currency: str) -> PaymentMethod:
    return PaymentMethod.STARS if currency == "XTR" else PaymentMethod.TELEGRAM


async def _find_existing_payment(session, provider_payment_id: str | None) -> Payment | None:
    if not provider_payment_id:
        return None
    return await session.scalar(
        select(Payment).where(Payment.provider_payment_id == provider_payment_id)
    )


async def _ensure_completed_payment_record(
    session,
    *,
    user_id: int,
    provider_payment_id: str | None,
    amount: int,
    currency: str,
    discount_applied: float = 0.0,
    subscription_id: int | None = None,
    tariff_id: int | None = None,
    platform: str | None = None,
) -> Payment:
    existing = await _find_existing_payment(session, provider_payment_id)
    if existing:
        if tariff_id and not existing.tariff_id:
            existing.tariff_id = tariff_id
        if platform and not existing.platform:
            existing.platform = platform
        return existing

    payment = Payment(
        user_id=user_id,
        subscription_id=subscription_id,
        amount=amount,
        currency=currency,
        method=_payment_method_from_currency(currency),
        status=PaymentStatus.COMPLETED,
        provider_payment_id=provider_payment_id,
        discount_applied=discount_applied,
        tariff_id=tariff_id,
        platform=platform,
    )
    session.add(payment)
    await session.commit()
    await session.refresh(payment)
    return payment


def _parse_topup_amount(raw: str) -> int | None:
    cleaned = "".join(ch for ch in str(raw) if ch.isdigit())
    if not cleaned:
        return None
    amount = int(cleaned)
    if amount < TOPUP_MIN_RUB or amount > TOPUP_MAX_RUB:
        return None
    return amount


def _build_delivery_issue_text(issue: AccessProvisionError, *, refunded_balance: bool = False) -> str:
    text = issue.client_message
    if refunded_balance:
        text += "\nСредства уже возвращены на баланс."
    if settings.support_username:
        text += f"\nЕсли вопрос срочный, напишите в поддержку {settings.support_username}."
    return text


async def _notify_delivery_issue(
    bot,
    *,
    telegram_id: int,
    full_name: str,
    username: str | None,
    tariff_label: str,
    platform: str,
    payment_source: str,
    issue: AccessProvisionError,
) -> None:
    uname = f"@{username}" if username else "—"
    await notify_admins_issue(
        bot,
        title="Ошибка выдачи доступа",
        details=[
            f"Пользователь: {full_name} ({telegram_id}, {uname})",
            f"Тариф: {tariff_label}",
            f"Платформа: {platform}",
            f"Источник оплаты: {payment_source}",
            f"Провайдер: {issue.provider}",
            f"Код: {issue.code}",
            f"HTTP статус: {issue.status or '—'}",
            f"Причина: {issue.admin_message}",
        ],
    )


async def _notify_generic_delivery_issue(
    bot,
    *,
    telegram_id: int,
    full_name: str,
    username: str | None,
    tariff_label: str,
    platform: str,
    payment_source: str,
    reason: str,
) -> None:
    uname = f"@{username}" if username else "—"
    await notify_admins_issue(
        bot,
        title="Оплата прошла, но доступ не выдан",
        details=[
            f"Пользователь: {full_name} ({telegram_id}, {uname})",
            f"Тариф: {tariff_label}",
            f"Платформа: {platform}",
            f"Источник оплаты: {payment_source}",
            f"Причина: {reason}",
        ],
    )


def _topup_text(balance_rub: float, daily_rate: float, amount_rub: int | None = None) -> str:
    lines = [
        "💰 <b>Пополнить баланс</b>",
        "",
        f"Сейчас на балансе: <b>{balance_rub:.2f} ₽</b>",
        "",
        "Можно купить тариф как обычно или пополнить баланс на любую сумму.",
        f"Если включить ежедневные списания, доступ будет продлеваться автоматически по <b>{daily_rate:.2f} ₽ в день</b>.",
        "Одна ссылка работает на 3 устройства.",
        "Дополнительные устройства покупаются отдельно и в это списание не входят.",
        "Если потом выключить списания, доступ останется до уже оплаченной даты.",
        "Бонусы за приглашения тоже падают на этот же баланс.",
        "",
        "Можно пополнить баланс на любую сумму от <b>70 ₽</b> до <b>50 000 ₽</b>.",
    ]
    if amount_rub:
        lines.extend([
            "",
            f"Сумма пополнения: <b>{amount_rub} ₽</b>",
            "Выберите способ оплаты.",
        ])
    return "\n".join(lines)


async def _get_or_create_user(telegram_user) -> User:
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == telegram_user.id)
        )
        user = result.scalar_one_or_none()
        if user:
            return user

        user = User(
            telegram_id=telegram_user.id,
            username=telegram_user.username,
            full_name=telegram_user.full_name or "",
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def _ensure_intro_basic_available(callback: CallbackQuery, tariff: Tariff) -> bool:
    async with async_session() as session:
        user = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
        if not user:
            await callback.answer("Пользователь не найден", show_alert=True)
            return False
        if await can_purchase_intro_basic_tariff(session, user=user, tariff=tariff):
            return True
    await callback.answer(INTRO_BASIC_ALREADY_USED_TEXT, show_alert=True)
    return False


@router.callback_query(F.data == "balance_menu")
async def show_balance_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    async with async_session() as session:
        user = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
        balance_rub = get_user_balance(user)
        daily_rate = await get_daily_charge_rub(session)
    await callback.message.edit_text(
        _topup_text(balance_rub, daily_rate),
        reply_markup=balance_menu_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("balance_amount_"))
async def choose_balance_amount(callback: CallbackQuery, state: FSMContext) -> None:
    amount_key = callback.data.removeprefix("balance_amount_")
    if amount_key == "custom":
        await state.set_state(BalanceTopUpStates.waiting_amount)
        await callback.message.edit_text(
            "💰 <b>Введите сумму пополнения</b>\n\n"
            "Напишите сумму в рублях, например: <b>350</b>\n"
            f"Минимум: <b>{TOPUP_MIN_RUB} ₽</b>",
            reply_markup=back_to_menu_kb(),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    amount_rub = _parse_topup_amount(amount_key)
    if not amount_rub:
        await callback.answer("Некорректная сумма", show_alert=True)
        return

    async with async_session() as session:
        user = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
        balance_rub = get_user_balance(user)
        daily_rate = await get_daily_charge_rub(session)

    await state.clear()
    await callback.message.edit_text(
        _topup_text(balance_rub, daily_rate, amount_rub),
        reply_markup=balance_payment_kb(amount_rub),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(BalanceTopUpStates.waiting_amount, F.text)
async def process_balance_amount(message: Message, state: FSMContext) -> None:
    amount_rub = _parse_topup_amount(message.text)
    if not amount_rub:
        await message.answer(
            f"Введите сумму от {TOPUP_MIN_RUB} до {TOPUP_MAX_RUB} ₽ одним числом. Например: <b>350</b>",
            parse_mode="HTML",
            reply_markup=back_to_menu_kb(),
        )
        return

    user = await _get_or_create_user(message.from_user)
    async with async_session() as session:
        session_user = await session.scalar(select(User).where(User.id == user.id))
        daily_rate = await get_daily_charge_rub(session)
    await state.clear()
    await message.answer(
        _topup_text(get_user_balance(session_user or user), daily_rate, amount_rub),
        reply_markup=balance_payment_kb(amount_rub),
        parse_mode="HTML",
    )


async def _create_balance_topup_record(
    *,
    telegram_user,
    amount_rub: int,
    provider: str,
    provider_payment_id: str | None = None,
) -> tuple[int, int]:
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == telegram_user.id)
        )
        user = result.scalar_one_or_none()
        if not user:
            user = User(
                telegram_id=telegram_user.id,
                username=telegram_user.username,
                full_name=telegram_user.full_name or "",
            )
            session.add(user)
            await session.flush()

        topup = BalanceTopUp(
            user_id=user.id,
            telegram_id=telegram_user.id,
            amount_rub=float(amount_rub),
            provider=provider,
            status="pending",
            provider_payment_id=provider_payment_id,
        )
        session.add(topup)
        await session.flush()
        topup_id = topup.id
        if provider_payment_id is None:
            topup.provider_payment_id = str(topup_id)
        await session.commit()
        return user.id, topup_id


@router.callback_query(F.data.startswith("paytopup_telegram_"))
async def initiate_telegram_topup(callback: CallbackQuery) -> None:
    if not settings.telegram_payment_provider_token:
        await callback.answer("Этот способ оплаты недоступен", show_alert=True)
        return

    amount_rub = _parse_topup_amount(callback.data.rsplit("_", 1)[-1])
    if not amount_rub:
        await callback.answer("Некорректная сумма", show_alert=True)
        return

    _, topup_id = await _create_balance_topup_record(
        telegram_user=callback.from_user,
        amount_rub=amount_rub,
        provider="telegram",
    )

    try:
        await callback.bot.send_invoice(
            chat_id=callback.message.chat.id,
            title="💰 Пополнение баланса",
            description=f"Пополнение баланса на {amount_rub} ₽",
            payload=f"topup_{amount_rub}_{topup_id}",
            provider_token=settings.telegram_payment_provider_token,
            currency="RUB",
            prices=[LabeledPrice(label="Пополнение баланса", amount=int(amount_rub * 100))],
        )
    except Exception as exc:
        logger.error("Telegram Pay topup invoice failed: %s", exc)
        await callback.message.answer(
            "❌ Не удалось создать счёт. Попробуйте другой способ оплаты.",
        )
    await callback.answer()


@router.callback_query(F.data.startswith("paytopup_yookassa_"))
async def initiate_yookassa_topup(callback: CallbackQuery) -> None:
    if not settings.yookassa_shop_id or not settings.yookassa_secret_key:
        await callback.answer("YooKassa сейчас недоступна", show_alert=True)
        return

    amount_rub = _parse_topup_amount(callback.data.rsplit("_", 1)[-1])
    if not amount_rub:
        await callback.answer("Некорректная сумма", show_alert=True)
        return

    _, topup_id = await _create_balance_topup_record(
        telegram_user=callback.from_user,
        amount_rub=amount_rub,
        provider="yookassa",
    )

    try:
        import yookassa

        yookassa.Configuration.account_id = settings.yookassa_shop_id
        yookassa.Configuration.secret_key = settings.yookassa_secret_key

        def _create():
            return yookassa.Payment.create(
                {
                    "amount": {"value": f"{amount_rub:.2f}", "currency": "RUB"},
                    "confirmation": {
                        "type": "redirect",
                        "return_url": settings.base_webhook_url or "https://t.me/",
                    },
                    "capture": True,
                    "description": "Пополнение баланса",
                    "metadata": {
                        "purpose": "balance_topup",
                        "topup_id": str(topup_id),
                        "user_id": str(callback.from_user.id),
                        "chat_id": str(callback.message.chat.id),
                    },
                },
                f"balance-topup-{topup_id}",
            )

        payment = await asyncio.to_thread(_create)
    except Exception as exc:
        logger.error("Failed to create YooKassa topup: %s", exc)
        await callback.answer("Не удалось создать платёж", show_alert=True)
        return

    async with async_session() as session:
        topup = await session.get(BalanceTopUp, topup_id)
        if topup:
            topup.provider_payment_id = payment.id
            await session.commit()

    await callback.message.answer(
        f"💳 <b>Пополнение баланса</b>\n\n"
        f"Сумма: <b>{amount_rub} ₽</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить через ЮKassa", url=payment.confirmation.confirmation_url)],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("paytopup_robokassa_"))
async def initiate_robokassa_topup(callback: CallbackQuery) -> None:
    if not settings.robokassa_merchant_login:
        await callback.answer("Robokassa сейчас недоступна", show_alert=True)
        return

    amount_rub = _parse_topup_amount(callback.data.rsplit("_", 1)[-1])
    if not amount_rub:
        await callback.answer("Некорректная сумма", show_alert=True)
        return

    _, topup_id = await _create_balance_topup_record(
        telegram_user=callback.from_user,
        amount_rub=amount_rub,
        provider="robokassa",
    )

    payment_url = generate_robokassa_url(
        merchant_login=settings.robokassa_merchant_login,
        password1=settings.robokassa_password_1,
        inv_id=topup_id,
        amount=float(amount_rub),
        description="Пополнение баланса",
    )

    await callback.message.answer(
        f"💰 <b>Пополнение баланса</b>\n\n"
        f"Сумма: <b>{amount_rub} ₽</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить через Robokassa", url=payment_url)],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")],
        ]),
    )
    await callback.answer()


# ── Initiate Stars Payment ────────────────────────────

@router.callback_query(F.data.startswith("pay_stars_"))
async def initiate_stars_payment(callback: CallbackQuery) -> None:
    """Send Telegram Stars invoice.
    
    Callback format: pay_stars_{tariff_id}_{platform}_{use_bal}
    """
    tariff_id, platform, use_bal = _parse_pay_callback(callback.data)

    tariff = await _get_tariff(tariff_id)
    if not tariff:
        await callback.answer("Тариф не найден", show_alert=True)
        return
    if not tariff.is_active:
        await callback.answer("Этот тариф больше недоступен", show_alert=True)
        return
    if not await _ensure_intro_basic_available(callback, tariff):
        return
    if not tariff.price_stars:
        await callback.answer("Stars-оплата недоступна для этого тарифа", show_alert=True)
        return

    async with async_session() as session:
        user = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
        if not user:
            await callback.answer("Пользователь не найден", show_alert=True)
            return
        try:
            purchase_price = await get_purchase_price_rub(
                session, user=user, tariff=tariff,
                action=decode_intent(platform)[1],
                target_subscription_id=decode_intent(platform)[2],
            )
        except ValueError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        discount = 0.0
        if use_bal == 1 and user:
            discount = min(get_user_balance(user), purchase_price)

    if discount >= purchase_price and purchase_price > 0:
        await callback.answer("Баланса достаточно для полной оплаты. Используйте 'Оплатить с баланса'", show_alert=True)
        return

    final_price_stars = tariff.price_stars
    if purchase_price != tariff.price_rub or discount > 0:
        ratio = (purchase_price - discount) / tariff.price_rub
        final_price_stars = math.ceil(tariff.price_stars * ratio)
        if final_price_stars < 1:
            final_price_stars = 1

    payload = f"{tariff_id}_{platform}_{discount}"

    try:
        await create_stars_invoice(
            bot=callback.bot,
            chat_id=callback.message.chat.id,
            tariff_id=tariff_id,
            tariff_label=tariff.label,
            price_stars=final_price_stars,
            platform=platform,
            payload=payload,
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"Stars invoice creation failed: {e}")
        await callback.answer("Ошибка создания счёта", show_alert=True)


# ── Telegram Pay (native card via Telegram API) ───────

@router.callback_query(F.data.startswith("pay_telegram_"))
async def initiate_telegram_payment(callback: CallbackQuery) -> None:
    """Send Telegram Pay invoice (native card payment via Telegram).
    
    Callback format: pay_telegram_{tariff_id}_{platform}_{use_bal}
    """
    if not settings.telegram_payment_provider_token:
        await callback.answer("Этот способ оплаты недоступен.", show_alert=True)
        return

    tariff_id, platform, use_bal = _parse_pay_callback(callback.data)

    tariff = await _get_tariff(tariff_id)
    if not tariff:
        await callback.answer("Тариф не найден", show_alert=True)
        return
    if not tariff.is_active:
        await callback.answer("Этот тариф больше недоступен", show_alert=True)
        return
    if not await _ensure_intro_basic_available(callback, tariff):
        return

    async with async_session() as session:
        user = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
        if not user:
            await callback.answer("Пользователь не найден", show_alert=True)
            return
        try:
            purchase_price = await get_purchase_price_rub(
                session, user=user, tariff=tariff,
                action=decode_intent(platform)[1],
                target_subscription_id=decode_intent(platform)[2],
            )
        except ValueError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        discount = 0.0
        if use_bal == 1 and user:
            discount = min(get_user_balance(user), purchase_price)

    if discount >= purchase_price and purchase_price > 0:
        await callback.answer("Баланса достаточно для полной оплаты. Используйте 'Оплатить с баланса'", show_alert=True)
        return

    final_price_rub = float(purchase_price - discount)
    
    # Telegram Pay minimum is ~1 USD (approx 65-70 RUB). We set 70 RUB as safe minimum.
    if 0 < final_price_rub < 70:
        if use_bal == 1:
            final_price_rub = 70.0
            discount = float(purchase_price - final_price_rub)
        else:
            await callback.answer(f"Сумма к оплате ({final_price_rub} ₽) меньше минимальной (70 ₽).", show_alert=True)
            return

    payload = f"{tariff_id}_{platform}_{discount}"

    try:
        await create_telegram_pay_invoice(
            bot=callback.bot,
            chat_id=callback.message.chat.id,
            tariff_id=tariff_id,
            tariff_label=tariff.label,
            price_rub=final_price_rub,
            platform=platform,
            payload=payload,
            provider_token=settings.telegram_payment_provider_token,
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"Telegram Pay invoice creation failed: {e}")
        await callback.answer("Ошибка создания счёта", show_alert=True)


@router.callback_query(F.data.startswith("pay_balance_"))
async def initiate_balance_payment(callback: CallbackQuery) -> None:
    tariff_id, platform_str, _ = _parse_pay_callback(callback.data)
    platform = _platform_from_str(platform_str)

    tariff = await _get_tariff(tariff_id)
    if not tariff:
        await callback.answer("Тариф не найден", show_alert=True)
        return
    if not tariff.is_active:
        await callback.answer("Этот тариф больше недоступен", show_alert=True)
        return
    if not await _ensure_intro_basic_available(callback, tariff):
        return
    is_tg_proxy_only = tariff.tariff_type == TariffType.TG_PROXY
    is_both = tariff.tariff_type == TariffType.BOTH

    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == callback.from_user.id)
        )
        user = result.scalar_one_or_none()

        if not user:
            await callback.answer("Пользователь не найден", show_alert=True)
            return

        try:
            purchase_price = await get_purchase_price_rub(
                session, user=user, tariff=tariff,
                action=decode_intent(platform_str)[1],
                target_subscription_id=decode_intent(platform_str)[2],
            )
        except ValueError as exc:
            await callback.answer(str(exc), show_alert=True)
            return

        if get_user_balance(user) < purchase_price:
            await callback.answer("Недостаточно средств на балансе", show_alert=True)
            return

        await callback.answer("Обрабатываем оплату…")

        debit_user_balance(
            session,
            user,
            float(purchase_price),
            BalanceTransactionKind.TOPUP,
            f"Оплата тарифа «{tariff.label}» с баланса",
            source_type="tariff",
            source_id=str(tariff.id),
        )

        payment = Payment(
            user_id=user.id,
            subscription_id=None,
            amount=int(round(float(purchase_price) * 100)),
            currency="RUB",
            method=PaymentMethod.BALANCE,
            status=PaymentStatus.COMPLETED,
            provider_payment_id="balance_" + str(uuid.uuid4()),
            tariff_id=tariff.id,
            platform=platform_str,
        )
        session.add(payment)
        await session.commit()

        saved_platform = user.platform if _is_deferred_platform(platform_str) and user.platform and not is_tg_proxy_only else None
        needs_platform_choice = _is_deferred_platform(platform_str) and saved_platform is None and not is_tg_proxy_only
        delivery_platform = saved_platform or platform

        if user.platform is None and not is_tg_proxy_only and not _is_deferred_platform(platform_str):
            user.platform = platform

        subscription = None
        vpn_key = None
        proxy_link = None

        # VPN part
        if not is_tg_proxy_only:
            try:
                subscription, vpn_key = await create_or_extend_paid_access(
                    session,
                    user=user,
                    tariff=tariff,
                    platform=delivery_platform,
                    provisioning_payment=payment,
                    **_purchase_access_kwargs(platform_str),
                )
            except AccessProvisionError as issue:
                ambiguous_adapt = issue.provider == "adapt" and issue.code in {
                    "adapt_create_awaiting_webhook",
                    "adapt_runtime",
                    "adapt_api_error",
                    "adapt_api_500",
                    "adapt_api_502",
                    "adapt_api_503",
                    "adapt_api_504",
                    "adapt_renew_failed",
                    "adapt_upgrade_failed",
                }
                if not ambiguous_adapt:
                    credit_user_balance(
                        session,
                        user,
                        float(purchase_price),
                        BalanceTransactionKind.REFUND,
                        "Возврат на баланс после ошибки выдачи ключа",
                        source_type="tariff",
                        source_id=str(tariff.id),
                    )
                    payment.status = PaymentStatus.REFUNDED
                plog(
                    "ОШИБКА_ВЫДАЧИ",
                    provider=issue.provider.upper(),
                    user_id=callback.from_user.id,
                    tariff=tariff.label,
                    method="balance",
                    code=issue.code,
                    status=issue.status or "",
                )
                await session.commit()
                await _notify_delivery_issue(
                    callback.bot,
                    telegram_id=callback.from_user.id,
                    full_name=callback.from_user.full_name or "",
                    username=callback.from_user.username,
                    tariff_label=tariff.label,
                    platform=delivery_platform.value if hasattr(delivery_platform, "value") else str(delivery_platform),
                    payment_source="Баланс",
                    issue=issue,
                )
                await notify_expiring(
                    callback.bot,
                    callback.from_user.id,
                    f"⚠️ {_build_delivery_issue_text(issue, refunded_balance=not ambiguous_adapt)}",
                )
                return
            if not subscription or not vpn_key:
                await _notify_generic_delivery_issue(
                    callback.bot,
                    telegram_id=callback.from_user.id,
                    full_name=callback.from_user.full_name or "",
                    username=callback.from_user.username,
                    tariff_label=tariff.label,
                    platform=delivery_platform.value if hasattr(delivery_platform, "value") else str(delivery_platform),
                    payment_source="Баланс",
                    reason="create_or_extend_paid_access returned no subscription/key",
                )
                await callback.message.answer(
                    "❌ Ошибка генерации ключа. Средства не списаны.\nОбратитесь в поддержку.",
                    parse_mode="HTML",
                )
                credit_user_balance(
                    session,
                    user,
                    float(purchase_price),
                    BalanceTransactionKind.REFUND,
                    "Возврат на баланс после ошибки выдачи ключа",
                    source_type="tariff",
                    source_id=str(tariff.id),
                )
                payment.status = PaymentStatus.REFUNDED
                await session.commit()
                return

        # MTProto part
        if is_tg_proxy_only or is_both:
            if is_tg_proxy_only:
                from datetime import timedelta
                from bot.services.subscription_service import get_primary_active_server
                server = await get_primary_active_server(session)
                if not server:
                    credit_user_balance(
                        session,
                        user,
                        float(purchase_price),
                        BalanceTransactionKind.REFUND,
                        "Возврат на баланс после ошибки сервера",
                        source_type="tariff",
                        source_id=str(tariff.id),
                    )
                    payment.status = PaymentStatus.REFUNDED
                    await session.commit()
                    await callback.answer("Нет доступных серверов", show_alert=True)
                    return
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
                session,
                user=user,
                tariff=tariff,
                subscription=subscription,
            )
            if not mtproto_account and is_tg_proxy_only:
                credit_user_balance(
                    session,
                    user,
                    float(purchase_price),
                    BalanceTransactionKind.REFUND,
                    "Возврат на баланс после ошибки создания Telegram-ускорителя",
                    source_type="tariff",
                    source_id=str(tariff.id),
                )
                payment.status = PaymentStatus.REFUNDED
                await session.commit()
                await callback.message.answer(
                    "❌ Ошибка создания Telegram-ускоритель.\nОбратитесь в поддержку.",
                    parse_mode="HTML",
                )
                return

        payment.subscription_id = subscription.id if subscription else None
        await session.flush()

        # Referrer logic
        if user.referred_by:
            logger.info(f"Payment {payment.id} from {user.telegram_id} - triggering referral credit.")
            await credit_referral(session, user.id, payment.id, float(tariff.price_rub), bot=callback.bot)
            await log_referral_payment(session, user.id, float(tariff.price_rub), bot=callback.bot)

        # Partner logic
        if user.partner_id:
            from bot.services.payment_service import credit_partner
            await credit_partner(session, user.id, payment.id, float(tariff.price_rub), bot=callback.bot)

        await session.commit()
        plog(
            "ОПЛАТА",
            provider="Balance",
            user_id=callback.from_user.id,
            amount=f"{float(tariff.price_rub):.2f}",
            tariff=tariff.label,
        )

        expires_str = subscription.expires_at.strftime("%d.%m.%Y") if subscription else "N/A"

        # Deliver keys. For the new flow, choose platform after payment and then send the guide.
        if vpn_key and needs_platform_choice and subscription:
            await _ask_platform_before_key(callback, subscription.id)
        elif vpn_key:
            from bot.utils.texts import KEY_DELIVERED, GUIDE_ANDROID, GUIDE_IOS, GUIDE_MAC, GUIDE_WINDOWS, GUIDE_ANDROID_TV
            key_display = vpn_key if len(vpn_key) <= 200 else vpn_key[:200] + "..."
            
            client_name = subscription.client_name if subscription else ""
            explanation = await _get_key_change_explanation(session, user.id, subscription.id if subscription else 0, client_name)
            
            await callback.message.edit_text(
                KEY_DELIVERED.format(key=key_display, expires=expires_str),
                parse_mode="HTML",
            )
            
            if explanation:
                await callback.message.answer(explanation, parse_mode="HTML")
            
            await callback.message.answer(
                f"📋 <b>Полный ключ:</b>\n\n<code>{vpn_key}</code>",
                parse_mode="HTML",
            )

            guides = {
                "android": GUIDE_ANDROID, "ios": GUIDE_IOS, "mac": GUIDE_MAC,
                "windows": GUIDE_WINDOWS, "android_tv": GUIDE_ANDROID_TV,
            }
            from bot.services.guide_service import send_guide
            await send_guide(
                callback.bot, callback.from_user.id,
                delivery_platform, guides.get(delivery_platform.value if hasattr(delivery_platform, "value") else str(delivery_platform), GUIDE_ANDROID),
            )

        if proxy_link:
            await callback.message.answer(
                MTPROTO_KEY_DELIVERED.format(
                    proxy_links=proxy_link,
                    expires=expires_str,
                ),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )

        product_label = {
            TariffType.VPN: "Весь интернет",
            TariffType.TG_PROXY: "TG-ускоритель",
            TariffType.BOTH: "Весь интернет + TG-ускоритель",
        }.get(tariff.tariff_type, "Весь интернет")
        await notify_admins_payment(
            callback.bot,
            telegram_id=callback.from_user.id,
            full_name=callback.from_user.full_name or "",
            username=callback.from_user.username,
            amount_rub=float(tariff.price_rub),
            tariff_label=f"{tariff.label} ({product_label})",
            method="💎 Баланс",
            platform=delivery_platform.value if not is_tg_proxy_only else "telegram",
        )

        await callback.message.answer("✅ Оплата с баланса успешна!")



# ── YooKassa Payment ──────────────────────────────────

@router.callback_query(F.data.startswith("pay_yookassa_"))
async def initiate_yookassa_payment(callback: CallbackQuery) -> None:
    """Create YooKassa payment and send confirmation link to user.
    
    Callback format: pay_yookassa_{tariff_id}_{platform}_{use_bal}
    """
    if not settings.yookassa_shop_id or not settings.yookassa_secret_key:
        await callback.answer(
            "💳 Оплата через YooKassa сейчас недоступна.\n"
            "Используйте Telegram Stars ⭐",
            show_alert=True,
        )
        return

    tariff_id, platform_str, use_bal = _parse_pay_callback(callback.data)

    tariff = await _get_tariff(tariff_id)
    if not tariff:
        await callback.answer("Некорректный тариф", show_alert=True)
        return
    if not tariff.is_active or tariff.is_admin_only:
        await callback.answer("Этот тариф больше недоступен", show_alert=True)
        return
    if not await _ensure_intro_basic_available(callback, tariff):
        return

    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == callback.from_user.id)
        )
        user = result.scalar_one_or_none()
        if not user:
            await callback.answer("Ошибка: пользователь не найден", show_alert=True)
            return

        try:
            purchase_price = await get_purchase_price_rub(
                session, user=user, tariff=tariff,
                action=decode_intent(platform_str)[1],
                target_subscription_id=decode_intent(platform_str)[2],
            )
        except ValueError as exc:
            await callback.answer(str(exc), show_alert=True)
            return

        # Calculate discount
        discount = 0.0
        if use_bal == 1:
            discount = min(get_user_balance(user), purchase_price)
        
        if discount >= purchase_price and purchase_price > 0:
            await callback.answer("Баланса достаточно для полной оплаты. Используйте 'Оплатить с баланса'", show_alert=True)
            return
            
        final_price_rub = float(purchase_price - discount)
        
        # YooKassa minimum is usually 10 RUB
        if 0 < final_price_rub < 10:
            if use_bal == 1:
                final_price_rub = 10.0
                discount = float(purchase_price - final_price_rub)
            else:
                await callback.answer(f"Сумма к оплате ({final_price_rub} ₽) меньше минимальной (10 ₽).", show_alert=True)
                return

        # Create pending payment record
        payment = Payment(
            user_id=user.id,
            amount=int(final_price_rub * 100),  # rubles → kopecks
            currency="RUB",
            method=PaymentMethod.YOOKASSA,
            status=PaymentStatus.PENDING,
            telegram_chat_id=callback.message.chat.id,
            discount_applied=discount,
            tariff_id=tariff_id,
            platform=platform_str,
        )
        session.add(payment)
        await session.flush()
        payment_id = payment.id
        user_telegram_id = user.telegram_id
        await session.commit()

    return_url = settings.base_webhook_url or "https://t.me/"

    try:
        confirmation_url, yookassa_payment_id = await create_yookassa_payment(
            user_id=user_telegram_id,
            tariff_id=tariff_id,
            platform=platform_str,
            chat_id=callback.message.chat.id,
            return_url=return_url,
            tariff_label=tariff.label,
            price_rub=float(final_price_rub),
        )
    except Exception as exc:
        logger.error(f"Failed to create YooKassa payment: {exc}")
        await callback.message.answer(
            "❌ Ошибка создания платежа. Попробуйте позже или используйте другой способ оплаты.",
            reply_markup=back_to_menu_kb(),
        )
        await callback.answer()
        return

    # Save Yookassa payment ID to the pending record
    async with async_session() as session:
        payment_record = await session.get(Payment, payment_id)
        if payment_record:
            payment_record.provider_payment_id = yookassa_payment_id
        await session.commit()

    await callback.message.answer(
        f"💳 <b>Оплата через YooKassa</b>\n\n"
        f"Тариф: <b>{tariff.label}</b> - {purchase_price}₽\n\n"
        f"<i>Ключ будет отправлен сюда автоматически после оплаты.</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить через ЮKassa", url=confirmation_url)],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")],
        ]),
    )
    await callback.answer()


# ── Robokassa Payment ─────────────────────────────────

@router.callback_query(F.data.startswith("pay_robokassa_"))
async def initiate_robokassa_payment(callback: CallbackQuery) -> None:
    """Create Robokassa payment and send payment link to user.
    
    Callback format: pay_robokassa_{tariff_id}_{platform}_{use_bal}
    """
    if not settings.robokassa_merchant_login:
        await callback.answer(
            "💰 Оплата через Robokassa сейчас недоступна.\n"
            "Используйте Telegram Stars ⭐",
            show_alert=True,
        )
        return

    tariff_id, platform_str, use_bal = _parse_pay_callback(callback.data)

    tariff = await _get_tariff(tariff_id)
    if not tariff:
        await callback.answer("Некорректный тариф", show_alert=True)
        return
    if not await _ensure_intro_basic_available(callback, tariff):
        return

    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == callback.from_user.id)
        )
        user = result.scalar_one_or_none()
        if not user:
            await callback.answer("Ошибка: пользователь не найден", show_alert=True)
            return

        try:
            purchase_price = await get_purchase_price_rub(
                session, user=user, tariff=tariff,
                action=decode_intent(platform_str)[1],
                target_subscription_id=decode_intent(platform_str)[2],
            )
        except ValueError as exc:
            await callback.answer(str(exc), show_alert=True)
            return

        discount = 0.0
        if use_bal == 1:
            discount = min(get_user_balance(user), purchase_price)
        
        if discount >= purchase_price and purchase_price > 0:
            await callback.answer("Баланса достаточно для полной оплаты. Используйте 'Оплатить с баланса'", show_alert=True)
            return

        final_price_rub = float(purchase_price - discount)

        # Robokassa minimum is usually 50 RUB
        if 0 < final_price_rub < 50:
            if use_bal == 1:
                final_price_rub = 50.0
                discount = float(purchase_price - final_price_rub)
            else:
                await callback.answer(f"Сумма к оплате ({final_price_rub} ₽) меньше минимальной (50 ₽).", show_alert=True)
                return

        robokassa_payment = RobokassaPayment(
            user_id=user.id,
            tariff_id=tariff_id,
            tariff_idx=0,  # legacy compat field
            platform=platform_str,
            amount=float(final_price_rub),
            telegram_chat_id=callback.message.chat.id,
            discount_applied=discount,
        )
        session.add(robokassa_payment)
        await session.flush()
        inv_id = robokassa_payment.id
        await session.commit()

    description = f"Весь интернет {tariff.label}"
    payment_url = generate_robokassa_url(
        merchant_login=settings.robokassa_merchant_login,
        password1=settings.robokassa_password_1,
        inv_id=inv_id,
        amount=float(final_price_rub),
        description=description,
    )

    await callback.message.answer(
        f"💰 <b>Оплата через Robokassa</b>\n\n"
        f"Тариф: <b>{tariff.label}</b> - {tariff.price_rub}₽\n"
        f"Принимаем: Mir, СБП, наличные и другие\n\n"
        f"<i>Ключ будет отправлен сюда автоматически после оплаты.</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить через Robokassa", url=payment_url)],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")],
        ]),
    )
    await callback.answer()


# ── Pre-Checkout Query ────────────────────────────────

@router.pre_checkout_query()
async def process_pre_checkout(pre_checkout: PreCheckoutQuery) -> None:
    """Validate pre-checkout payload before approving Telegram Pay / Stars charge.

    Decline if the invoice references a tariff that is no longer active or is
    admin-only. This blocks replay of old invoices whose tariff was disabled
    after the invoice was originally sent.
    """
    payload_str = pre_checkout.invoice_payload

    if payload_str.startswith("dev_") or payload_str.startswith("topup_"):
        logger.info(
            "Pre-checkout approved: user_id=%s payload=%s currency=%s total_amount=%s",
            pre_checkout.from_user.id,
            payload_str,
            pre_checkout.currency,
            pre_checkout.total_amount,
        )
        await pre_checkout.answer(ok=True)
        return

    try:
        tariff_id = int(payload_str.split("_", 1)[0])
    except (ValueError, IndexError):
        logger.error(
            "Pre-checkout rejected (malformed payload): user_id=%s payload=%s",
            pre_checkout.from_user.id,
            payload_str,
        )
        await pre_checkout.answer(ok=False, error_message="Платёж не может быть обработан")
        return

    tariff = await _get_tariff(tariff_id)
    if not tariff or not tariff.is_active or tariff.is_admin_only:
        logger.warning(
            "Pre-checkout rejected (inactive/admin-only tariff): user_id=%s payload=%s tariff_id=%s is_active=%s is_admin_only=%s",
            pre_checkout.from_user.id,
            payload_str,
            tariff_id,
            getattr(tariff, "is_active", None),
            getattr(tariff, "is_admin_only", None),
        )
        await pre_checkout.answer(ok=False, error_message="Тариф больше недоступен")
        return

    logger.info(
        "Pre-checkout approved: user_id=%s payload=%s currency=%s total_amount=%s",
        pre_checkout.from_user.id,
        payload_str,
        pre_checkout.currency,
        pre_checkout.total_amount,
    )
    await pre_checkout.answer(ok=True)


# ── Successful Payment → Generate Key ─────────────────

@router.message(F.successful_payment)
async def process_successful_payment(message: Message) -> None:
    """Payment received - generate key on ALL active servers and deliver to user.
    
    Payload format: {tariff_id}_{platform}
    One subscription gives access to all active Marzban servers.
    """
    payment_info = message.successful_payment
    payload_str = payment_info.invoice_payload

    async with async_session() as session:
        existing_payment = await _find_existing_payment(
            session,
            payment_info.telegram_payment_charge_id,
        )
    if existing_payment:
        logger.warning(
            "Duplicate successful payment ignored: user_id=%s charge_id=%s payment_id=%s",
            message.from_user.id,
            payment_info.telegram_payment_charge_id,
            existing_payment.id,
        )
        await message.answer(
            "ℹ️ Этот платёж уже обработан. Если доступ не появился, напишите в поддержку.",
            parse_mode="HTML",
        )
        return

    if payload_str.startswith("topup_"):
        parts = payload_str.split("_")
        amount_rub = _parse_topup_amount(parts[1] if len(parts) > 1 else "")
        topup_id = str(parts[2]) if len(parts) > 2 else ""
        if not amount_rub:
            await message.answer(ERROR, parse_mode="HTML")
            return

        async with async_session() as session:
            result = await session.execute(
                select(User).where(User.telegram_id == message.from_user.id)
            )
            user = result.scalar_one_or_none()
            if not user:
                user = User(
                    telegram_id=message.from_user.id,
                    username=message.from_user.username,
                    full_name=message.from_user.full_name or "",
                )
                session.add(user)
                await session.flush()

            payment = Payment(
                user_id=user.id,
                subscription_id=None,
                amount=payment_info.total_amount,
                currency=payment_info.currency,
                method=PaymentMethod.TELEGRAM,
                status=PaymentStatus.COMPLETED,
                provider_payment_id=payment_info.telegram_payment_charge_id,
            )
            session.add(payment)
            existing_topup = None
            if topup_id.isdigit():
                existing_topup = await session.get(BalanceTopUp, int(topup_id))
            if existing_topup:
                existing_topup.status = "completed"
                existing_topup.provider_payment_id = payment_info.telegram_payment_charge_id
                existing_topup.completed_at = datetime.utcnow()
            else:
                session.add(
                    BalanceTopUp(
                        user_id=user.id,
                        telegram_id=user.telegram_id,
                        amount_rub=float(amount_rub),
                        provider="telegram",
                        status="completed",
                        provider_payment_id=payment_info.telegram_payment_charge_id,
                        completed_at=datetime.utcnow(),
                    )
                )
            credit_user_balance(
                session,
                user,
                float(amount_rub),
                BalanceTransactionKind.TOPUP,
                "Пополнение баланса",
                source_type="telegram_topup",
                source_id=payment_info.telegram_payment_charge_id,
            )
            await session.commit()

            new_balance = get_user_balance(user)

        await notify_admins_payment(
            message.bot,
            telegram_id=message.from_user.id,
            full_name=message.from_user.full_name or "",
            username=message.from_user.username,
            amount_rub=float(amount_rub),
            tariff_label="Пополнение баланса",
            method="💳 Telegram Pay",
            platform="Баланс",
        )
        await message.answer(
            f"✅ <b>Баланс пополнен</b>\n\n"
            f"Зачислено: <b>{amount_rub} ₽</b>\n"
            f"Сейчас на балансе: <b>{new_balance:.2f} ₽</b>",
            parse_mode="HTML",
            reply_markup=back_to_menu_kb(),
        )
        return

    logger.info(
        "Successful payment received: user_id=%s payload=%s currency=%s total_amount=%s charge_id=%s",
        message.from_user.id,
        payload_str,
        payment_info.currency,
        payment_info.total_amount,
        payment_info.telegram_payment_charge_id,
    )

    if payload_str.startswith("dev_"):
        await process_device_payment(message, payload_str)
        return
        
    try:
        parts = payload_str.split("_")
        tariff_id = int(parts[0])
        # Last part is discount, everything between first and last is platform
        discount = float(parts[-1]) if len(parts) > 2 else 0.0
        platform_str = "_".join(parts[1:-1]) if len(parts) > 2 else parts[1]
    except (ValueError, IndexError) as e:
        logger.error(f"Invalid payment payload '{payload_str}': {e}")
        await message.answer(ERROR, parse_mode="HTML")
        return

    tariff = await _get_tariff(tariff_id)
    if not tariff:
        logger.error(f"Tariff {tariff_id} not found after payment!")
        await message.answer(ERROR, parse_mode="HTML")
        return
    if not tariff.is_active or tariff.is_admin_only:
        logger.error(
            "Successful payment rejected (inactive/admin-only tariff): user_id=%s payload=%s tariff_id=%s is_active=%s is_admin_only=%s",
            message.from_user.id,
            payload_str,
            tariff_id,
            tariff.is_active,
            tariff.is_admin_only,
        )
        await message.answer(
            "❌ Этот тариф больше недоступен. Напишите в поддержку, если нужна помощь.",
            parse_mode="HTML",
        )
        return

    is_tg_proxy_only = tariff.tariff_type == TariffType.TG_PROXY
    is_both = tariff.tariff_type == TariffType.BOTH
    platform = Platform.ANDROID if is_tg_proxy_only else _platform_from_str(platform_str)
    logger.info(
        "Processing successful payment: user_id=%s tariff_id=%s tariff_type=%s platform=%s discount=%.2f",
        message.from_user.id,
        tariff.id,
        tariff.tariff_type.value,
        platform.value,
        discount,
    )

    async with async_session() as session:
        # Get user
        result = await session.execute(
            select(User).where(User.telegram_id == message.from_user.id)
        )
        user = result.scalar_one_or_none()
        if not user:
            logger.error("Successful payment processing failed: user not found telegram_id=%s", message.from_user.id)
            await message.answer(ERROR, parse_mode="HTML")
            return

        saved_platform = user.platform if _is_deferred_platform(platform_str) and user.platform and not is_tg_proxy_only else None
        needs_platform_choice = _is_deferred_platform(platform_str) and saved_platform is None and not is_tg_proxy_only
        delivery_platform = saved_platform or platform

        if user.platform is None and not is_tg_proxy_only and not _is_deferred_platform(platform_str):
            user.platform = platform

        payment = await _ensure_completed_payment_record(
            session,
            user_id=user.id,
            provider_payment_id=payment_info.telegram_payment_charge_id,
            amount=payment_info.total_amount,
            currency=payment_info.currency,
            discount_applied=discount,
            tariff_id=tariff_id,
            platform=platform_str,
        )

        subscription = None
        vpn_key = None
        proxy_link = None

        # VPN part (for vpn and both tariff types)
        if not is_tg_proxy_only:
            bonus = 0 if is_vhq_tariff(tariff) else (user.bonus_days or 0)
            if bonus > 0:
                user.bonus_days = 0
                logger.info(f"Applying {bonus} bonus days for Stars user {message.from_user.id}")
            try:
                subscription, vpn_key = await create_or_extend_paid_access(
                    session,
                    user=user,
                    tariff=tariff,
                    platform=delivery_platform,
                    bonus_days=bonus,
                    provisioning_payment=payment,
                    **_purchase_access_kwargs(platform_str),
                )
            except AccessProvisionError as issue:
                plog(
                    "ОШИБКА_ВЫДАЧИ",
                    provider=issue.provider.upper(),
                    user_id=message.from_user.id,
                    tariff=tariff.label,
                    method="Stars",
                    code=issue.code,
                    status=issue.status or "",
                )
                await _notify_delivery_issue(
                    message.bot,
                    telegram_id=message.from_user.id,
                    full_name=message.from_user.full_name or "",
                    username=message.from_user.username,
                    tariff_label=tariff.label,
                    platform=delivery_platform.value if hasattr(delivery_platform, "value") else str(delivery_platform),
                    payment_source="Stars/Telegram Pay",
                    issue=issue,
                )
                await notify_expiring(
                    message.bot,
                    message.from_user.id,
                    f"❌ {_build_delivery_issue_text(issue)}",
                )
                return
            if not subscription or not vpn_key:
                logger.error(
                    "Successful payment VPN delivery failed: user_id=%s tariff_id=%s payload=%s",
                    user.id,
                    tariff.id,
                    payload_str,
                )
                await _notify_generic_delivery_issue(
                    message.bot,
                    telegram_id=message.from_user.id,
                    full_name=message.from_user.full_name or "",
                    username=message.from_user.username,
                    tariff_label=tariff.label,
                    platform=delivery_platform.value if hasattr(delivery_platform, "value") else str(delivery_platform),
                    payment_source="Stars/Telegram Pay",
                    reason="create_or_extend_paid_access returned no subscription/key",
                )
                await message.answer(
                    "❌ Оплата прошла, но выдача доступа задержалась. Мы уже получили уведомление и проверяем проблему.",
                    parse_mode="HTML",
                )
                return

        # MTProto part (for tg_proxy and both tariff types)
        if is_tg_proxy_only or is_both:
            # For tg_proxy only, create a lightweight subscription record
            if is_tg_proxy_only:
                from datetime import timedelta
                from bot.services.subscription_service import get_primary_active_server
                server = await get_primary_active_server(session)
                if not server:
                    await _notify_generic_delivery_issue(
                        message.bot,
                        telegram_id=message.from_user.id,
                        full_name=message.from_user.full_name or "",
                        username=message.from_user.username,
                        tariff_label=tariff.label,
                        platform="telegram",
                        payment_source="Stars/Telegram Pay",
                        reason="No active server available for tg_proxy provisioning",
                    )
                    await message.answer(ERROR, parse_mode="HTML")
                    return
                now = datetime.utcnow()
                expires_at = now + timedelta(days=tariff.days)
                from bot.services.client_names import build_client_name
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
                logger.info(
                    "Created TG-only lightweight subscription: user_id=%s subscription_id=%s server_id=%s expires_at=%s",
                    user.id,
                    subscription.id,
                    server.id,
                    expires_at.isoformat(),
                )

            mtproto_account, proxy_link = await create_mtproto_subscription(
                session,
                user=user,
                tariff=tariff,
                subscription=subscription,
            )
            if not mtproto_account:
                if is_tg_proxy_only:
                    logger.error(
                        "Successful payment MTProto delivery failed: user_id=%s tariff_id=%s payload=%s",
                        user.id,
                        tariff.id,
                        payload_str,
                    )
                    await _notify_generic_delivery_issue(
                        message.bot,
                        telegram_id=message.from_user.id,
                        full_name=message.from_user.full_name or "",
                        username=message.from_user.username,
                        tariff_label=tariff.label,
                        platform="telegram",
                        payment_source="Stars/Telegram Pay",
                        reason="create_mtproto_subscription returned no account",
                    )
                    await message.answer(
                        "❌ Оплата прошла, но выдача Telegram-ускорителя задержалась. Мы уже получили уведомление и проверяем проблему.",
                        parse_mode="HTML",
                    )
                    return
                else:
                    logger.error(
                        "Failed to create MTProto account for BOTH tariff, VPN key already delivered candidate: user_id=%s tariff_id=%s",
                        user.id,
                        tariff.id,
                    )

        pay_method = _payment_method_from_currency(payment_info.currency)
        payment.subscription_id = subscription.id if subscription else None
        payment.discount_applied = discount

        if discount > 0:
            actual_discount = min(float(discount), max(0.0, get_user_balance(user)))
            if actual_discount > 0:
                debit_user_balance(
                    session,
                    user,
                    actual_discount,
                    BalanceTransactionKind.TOPUP,
                    "Списание с баланса при частичной оплате тарифа",
                    source_type="payment_discount",
                    source_id=payment_info.telegram_payment_charge_id,
                )

        await session.flush()

        # Referral commission + log
        amount_rub = (
            float(tariff.price_rub)
            if payment_info.currency == "XTR"
            else payment_info.total_amount / 100.0
        )
        await credit_referral(session, user.id, payment.id, amount_rub, bot=message.bot)
        await log_referral_payment(session, user.id, amount_rub, bot=message.bot)
        # Partner logic
        if user.partner_id:
            from bot.services.payment_service import credit_partner
            await credit_partner(session, user.id, payment.id, amount_rub, bot=message.bot)
        plog("ОПЛАТА", provider="Stars", user_id=message.from_user.id,
             amount=f"{amount_rub:.2f}", payment_id=payment.id)
        await session.commit()
        logger.info(
            "Successful payment committed: user_id=%s payment_id=%s subscription_id=%s method=%s currency=%s amount=%s",
            user.id,
            payment.id,
            subscription.id if subscription else None,
            pay_method.value,
            payment_info.currency,
            payment_info.total_amount,
        )

        expires_str = subscription.expires_at.strftime("%d.%m.%Y") if subscription else "N/A"

        # Deliver keys to user. New purchases choose the platform after payment.
        if vpn_key and needs_platform_choice and subscription:
            await _ask_platform_before_key(message, subscription.id)
        elif vpn_key:
            key_display = vpn_key if len(vpn_key) <= 200 else vpn_key[:200] + "..."
            
            client_name = subscription.client_name if subscription else ""
            explanation = await _get_key_change_explanation(session, user.id, subscription.id if subscription else 0, client_name)
            
            await message.answer(
                KEY_DELIVERED.format(key=key_display, expires=expires_str),
                parse_mode="HTML",
            )
            
            if explanation:
                await message.answer(explanation, parse_mode="HTML")
            
            await message.answer(
                f"📋 <b>Полный ключ (нажмите чтобы скопировать):</b>\n\n"
                f"<code>{vpn_key}</code>",
                parse_mode="HTML",
            )

            guides = {
                Platform.ANDROID: GUIDE_ANDROID,
                Platform.IOS: GUIDE_IOS,
                Platform.MAC: GUIDE_MAC,
                Platform.WINDOWS: GUIDE_WINDOWS,
                Platform.ANDROID_TV: GUIDE_ANDROID_TV,
            }
            from bot.services.guide_service import send_guide
            await send_guide(
                message.bot, message.from_user.id,
                delivery_platform, guides.get(delivery_platform, GUIDE_ANDROID),
                reply_markup=back_to_menu_kb(),
            )

        if proxy_link:
            await message.answer(
                MTPROTO_KEY_DELIVERED.format(
                    proxy_links=proxy_link,
                    expires=expires_str,
                ),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )

        if not vpn_key and not proxy_link:
            logger.error(
                "Successful payment produced no deliverables: user_id=%s payment_id=%s subscription_id=%s",
                user.id,
                payment.id,
                subscription.id if subscription else None,
            )
            await message.answer(ERROR, parse_mode="HTML")
            return

        # WhatsApp proxy bonus
        from bot.models import BotSettings
        async with async_session() as wa_session:
            wa_enabled_row = await wa_session.get(BotSettings, "whatsapp_proxy_enabled")
            wa_host_row = await wa_session.get(BotSettings, "whatsapp_proxy_host")
        if wa_enabled_row and wa_enabled_row.value == "1" and wa_host_row and wa_host_row.value:
            from bot.utils.texts import WHATSAPP_PROXY_BONUS
            await message.answer(
                WHATSAPP_PROXY_BONUS.format(proxy_host=wa_host_row.value),
                parse_mode="HTML",
            )

        amount_rub = (
            float(tariff.price_rub) if payment_info.currency == "XTR"
            else payment_info.total_amount / 100.0
        )
        method_str = "⭐ Telegram Stars" if payment_info.currency == "XTR" else "💳 Telegram Pay"
        product_label = {
            TariffType.VPN: "Весь интернет",
            TariffType.TG_PROXY: "TG-ускоритель",
            TariffType.BOTH: "Весь интернет + TG-ускоритель",
        }.get(tariff.tariff_type, "Весь интернет")
        await notify_admins_payment(
            message.bot,
            telegram_id=message.from_user.id,
            full_name=message.from_user.full_name or "",
            username=message.from_user.username,
            amount_rub=amount_rub,
            tariff_label=f"{tariff.label} ({product_label})",
            method=method_str,
            platform=delivery_platform.value if not is_tg_proxy_only else "telegram",
        )
        logger.info(
            "Successful payment flow finished: user_id=%s payment_id=%s vpn_delivered=%s mtproto_delivered=%s",
            user.id,
            payment.id,
            bool(vpn_key),
            bool(proxy_link),
        )


async def process_device_payment(message: Message, payload_str: str) -> None:
    """Handle successful payment for an extra device slot."""
    payment_info = message.successful_payment
    logger.info(
        "Successful device payment received: user_id=%s payload=%s currency=%s total_amount=%s charge_id=%s",
        message.from_user.id,
        payload_str,
        payment_info.currency,
        payment_info.total_amount,
        payment_info.telegram_payment_charge_id,
    )
    try:
        sub_id = int(payload_str.split("_")[1])
    except (ValueError, IndexError):
        await message.answer(ERROR, parse_mode="HTML")
        return

    telegram_id = message.from_user.id

    async with async_session() as session:
        sub = await session.get(Subscription, sub_id)
        owner = await session.get(User, sub.user_id) if sub else None
        if not sub or not owner or owner.telegram_id != telegram_id:
            logger.error("Device payment failed: subscription not found or ownership mismatch sub_id=%s telegram_id=%s", sub_id, telegram_id)
            await message.answer("❌ Подписка не найдена.")
            return

        payment = await _ensure_completed_payment_record(
            session,
            user_id=sub.user_id,
            provider_payment_id=payment_info.telegram_payment_charge_id,
            amount=payment_info.total_amount,
            currency=payment_info.currency,
            subscription_id=sub.id,
        )

        max_slots = await get_max_device_slots(session)
        if max_slots is not None and sub.device_slots >= max_slots:
            logger.warning("Device payment blocked by slot limit: sub_id=%s telegram_id=%s current_slots=%s max_slots=%s", sub_id, telegram_id, sub.device_slots, max_slots)
            await message.answer("❌ Лимит устройств для этой подписки уже достигнут.")
            return

        if not sub.vpn_key:
            logger.error("Device payment failed: subscription has no existing key sub_id=%s telegram_id=%s", sub_id, telegram_id)
            await _notify_generic_delivery_issue(
                message.bot,
                telegram_id=message.from_user.id,
                full_name=message.from_user.full_name or "",
                username=message.from_user.username,
                tariff_label="Дополнительное устройство",
                platform="device_slot",
                payment_source="Stars/Telegram Pay",
                reason="Subscription has no existing key for extra device",
            )
            await message.answer("❌ Не найден основной ключ подписки. Обратитесь в поддержку.")
            return

        # Extra devices use the same subscription link. The slot counter is
        # accounting/UI state; Marzban keeps the same user/key.
        sub.device_slots += 1
        new_slot = sub.device_slots
        vpn_key = sub.vpn_key

        payment.subscription_id = sub.id
        
        # Referral commission + log
        amount_rub = (
            payment_info.total_amount / 100.0 
            if payment_info.currency != "XTR" 
            else float(payment_info.total_amount) # Stars are 1:1 rub for commission in this logic
        )
        try:
            from bot.services.payment_service import credit_referral, log_referral_payment
            await credit_referral(session, sub.user_id, payment.id, amount_rub, bot=message.bot)
        except Exception as e:
            logger.error(f"Failed to credit referral for device: {e}")

        try:
            from bot.services.payment_service import credit_partner
            await credit_partner(session, sub.user_id, payment.id, amount_rub, bot=message.bot)
        except Exception as e:
            logger.error(f"Failed to credit partner for device: {e}")

        await session.commit()
        logger.info(
            "Device payment committed: user_id=%s sub_id=%s payment_provider_id=%s slot=%s",
            telegram_id,
            sub.id,
            payment.provider_payment_id,
            new_slot,
        )

        await message.answer(
            "✅ <b>Слот успешно добавлен!</b>\n\n"
            "Вы приобрели дополнительное устройство для вашей подписки.\n"
            "Используйте тот же ключ на новом устройстве.\n\n"
            f"📋 <b>Ваш ключ:</b>\n\n<code>{vpn_key}</code>",
            parse_mode="HTML"
        )

        logger.info(
            f"Device key delivered: user={message.from_user.id}, "
            f"sub_id={sub.id}, slot={new_slot}"
        )

        # Notify admins
        try:
            from bot.utils.texts import fmt_user
            user_link = fmt_user(
                message.from_user.id,
                message.from_user.username,
                message.from_user.first_name or "",
            )
            admin_text = (
                f"💰 <b>Дополнительное устройство оплачено!</b>\n"
                f"Пользователь: {user_link} (<code>{message.from_user.id}</code>)\n"
                f"Сумма: {payment_info.total_amount / 100.0 if payment_info.currency != 'XTR' else payment_info.total_amount} {payment_info.currency}\n"
                f"Подписка ID: {sub_id}\n"
                f"Устройство: №{new_slot}"
            )
            await notify_staff_text(message.bot, admin_text)
        except Exception as e:
            logger.error(f"Failed to send admin notification: {e}")
