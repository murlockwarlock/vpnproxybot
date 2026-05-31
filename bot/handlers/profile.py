"""Profile handler - user subscriptions, keys display."""

from __future__ import annotations

import logging
from datetime import timezone, timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from bot.config import settings
from bot.database import async_session
from bot.keyboards.client import profile_kb, renewal_choice_kb
from bot.models import BalanceTransaction, MTProtoAccount, RecurringPaymentProfile, Subscription, Tariff, User
from bot.services.adapt_routing import is_adapt_subscription, get_adapt_uuid_from_subscription
from bot.services.device_slots import get_included_device_slots
from bot.services.balance_service import get_daily_charge_rub, get_user_balance, next_charge_datetime
from bot.services.balance_mode_service import disable_balance_mode, enable_balance_mode
from bot.services.tariff_utils import format_subscription_duration
from bot.services.vhq_subscription_proxy import get_subscription_display_key
from bot.services.vhq_routing import is_vhq_subscription
from bot.services.webstore_bridge import fetch_linked_web_profile, sync_user_profile
from bot.services.subscription_service import _format_proxy_links
from bot.services.subscription_semantics import is_demo_subscription_row, paid_access_clause
from bot.utils.texts import (
    MTPROTO_KEY_INFO,
    NO_SUBSCRIPTIONS,
    PROFILE,
    PROFILE_RENEW_HINT,
    RECURRING_TOGGLE_OFF,
    RECURRING_TOGGLE_ON,
    SUB_STATUS_MAP,
    SUBSCRIPTION_ITEM,
)

logger = logging.getLogger(__name__)
router = Router(name="profile")
_MSK = timezone(timedelta(hours=3))


def _fmt_msk(dt) -> str:
    if not dt:
        return "-"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_MSK).strftime("%d.%m.%Y %H:%M")


def _fmt_iso_msk(iso_str: str | None) -> str:
    """Parse an ISO-8601 string (possibly with Z suffix) and format in MSK."""
    if not iso_str:
        return "-"
    try:
        from datetime import datetime
        s = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt.astimezone(_MSK).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return iso_str


def _is_mtproto_placeholder(vpn_key: str | None) -> bool:
    return not vpn_key or vpn_key == "mtproto_only"


def _subscription_product_label(sub: Subscription, mtproto_accounts: list[MTProtoAccount]) -> str:
    has_mtproto = any(acc.subscription_id == sub.id for acc in mtproto_accounts)
    if _is_mtproto_placeholder(sub.vpn_key):
        return "TG-ускоритель"
    if has_mtproto:
        return "Весь интернет + TG-ускоритель"
    return "Весь интернет"


def _subscription_tariff_label(sub: Subscription, mtproto_accounts: list[MTProtoAccount]) -> str:
    duration = format_subscription_duration(
        tariff_days=sub.tariff_days,
        tariff_months=sub.tariff_months,
    )
    return f"{_subscription_product_label(sub, mtproto_accounts)} - {duration}"


def _subscription_device_slots(sub: Subscription, included_slots: int) -> int:
    slots = int(sub.device_slots or 0)
    if is_vhq_subscription(sub) or is_adapt_subscription(sub):
        return max(slots, included_slots)
    return max(slots, 1)


def _provider_label(sub: Subscription) -> str:
    """Return a short provider label for a subscription."""
    if is_adapt_subscription(sub):
        return "Базовый"
    if is_vhq_subscription(sub):
        return "VHQ"
    return "Лайт"


async def _build_renewal_options(session, user: User) -> list[tuple[int, str, float]]:
    """Return list of (tariff_id, display_label, daily_rate_rub) for renewal choice keyboard.

    Groups user's active/recent-expired subs by tariff_id and returns unique options.
    Falls back to tariff-less provider label if tariff_id is not set.
    """
    from datetime import datetime
    from bot.models import Subscription as Sub
    now = datetime.utcnow()
    subs_result = await session.execute(
        select(Sub)
        .where(Sub.user_id == user.id)
        .where(paid_access_clause(Sub))
        .order_by(Sub.expires_at.desc())
    )
    subs = subs_result.scalars().all()

    seen_tariff_ids: set[int] = set()
    options: list[tuple[int, str, float]] = []

    for sub in subs:
        if sub.tariff_id and sub.tariff_id not in seen_tariff_ids:
            tariff = await session.get(Tariff, sub.tariff_id)
            if tariff and tariff.is_active:
                daily_rate = round(float(tariff.price_rub) / max(tariff.days, 1), 2)
                options.append((tariff.id, tariff.label, daily_rate))
                seen_tariff_ids.add(sub.tariff_id)

    return options


@router.callback_query(F.data == "profile")
async def show_profile(callback: CallbackQuery) -> None:
    """Show user profile with subscription count."""
    async with async_session() as session:
        result = await session.execute(
            select(User)
            .options(selectinload(User.subscriptions).selectinload(Subscription.server))
            .where(User.telegram_id == callback.from_user.id)
        )
        user = result.scalar_one_or_none()

        if not user:
            await callback.answer("Пользователь не найден", show_alert=True)
            return

        # Recurring payment profile
        rec_result = await session.execute(
            select(RecurringPaymentProfile)
            .where(RecurringPaymentProfile.user_id == user.id)
            .where(RecurringPaymentProfile.is_active == True)  # noqa: E712
        )
        rec_profile = rec_result.scalar_one_or_none()
        has_recurring = rec_profile is not None
        recurring_active = rec_profile.consent_granted if rec_profile else False

        # MTProto proxy accounts
        mtproto_result = await session.execute(
            select(MTProtoAccount)
            .where(MTProtoAccount.user_id == user.id)
            .where(MTProtoAccount.is_active == True)  # noqa: E712
        )
        mtproto_accounts = mtproto_result.scalars().all()

        balance_history_result = await session.execute(
            select(BalanceTransaction)
            .where(BalanceTransaction.user_id == user.id)
            .order_by(BalanceTransaction.created_at.desc(), BalanceTransaction.id.desc())
            .limit(5)
        )
        balance_history = balance_history_result.scalars().all()

        # WhatsApp settings
        from bot.models import BotSettings
        wa_enabled_row = await session.get(BotSettings, "whatsapp_proxy_enabled")
        wa_host_row = await session.get(BotSettings, "whatsapp_proxy_host")
        global_daily_rate = await get_daily_charge_rub(session)
        included_slots = await get_included_device_slots(session)

        # Per-tariff daily rate (if user chose a specific tariff for daily charges)
        charge_tariff: Tariff | None = None
        if user.daily_charge_tariff_id:
            charge_tariff = await session.get(Tariff, user.daily_charge_tariff_id)
        effective_daily_rate = (
            round(float(charge_tariff.price_rub) / max(charge_tariff.days, 1), 2)
            if charge_tariff and charge_tariff.days > 0
            else global_daily_rate
        )

        # Referral count
        from sqlalchemy import func
        ref_count = await session.scalar(
            select(func.count(User.id)).where(User.referred_by == user.telegram_id)
        ) or 0

        # Renewal options
        renewal_options = await _build_renewal_options(session, user)

    await sync_user_profile(user, user.subscriptions, mtproto_accounts)
    web_profile = await fetch_linked_web_profile(user.telegram_id)

    sub_count = len(user.subscriptions)
    active_sub_count = sum(1 for sub in user.subscriptions if sub.status.value == "active")
    has_manageable_device_subs = any(
        sub.status.value == "active" and not is_vhq_subscription(sub)
        for sub in user.subscriptions
    )
    has_expired_paid_subs = any(
        sub.status.value == "expired" and not is_demo_subscription_row(sub)
        for sub in user.subscriptions
    )
    purchase_button_text = (
        "🔄 Продлить доступ"
        if active_sub_count > 0 or has_expired_paid_subs
        else "🛒 Купить доступ"
    )

    text = PROFILE.format(
        telegram_id=user.telegram_id,
        registered=user.created_at.strftime("%d.%m.%Y"),
        sub_count=sub_count,
        balance=f"{get_user_balance(user):.2f} ₽",
    )

    # Referral summary
    referral_balance = float(user.referral_balance or 0.0)
    if ref_count > 0 or referral_balance > 0:
        text += f"\n👥 Приглашено: <b>{ref_count} чел.</b>"
        if referral_balance > 0:
            text += f" · Реф. баланс: <b>{referral_balance:.2f} ₽</b>"

    if user.balance_mode_enabled and user.balance_autodebit_enabled:
        text += (
            f"\n📅 Следующее списание: <b>{_fmt_msk(user.next_daily_charge_at or next_charge_datetime())}</b>"
        )

    # Show per-tariff daily rate if charge tariff is set
    if charge_tariff:
        text += (
            f"\n💸 Дневная ставка: <b>{effective_daily_rate:.2f} ₽</b>"
            f" ({charge_tariff.label})"
        )
    else:
        text += f"\n💸 Дневная ставка: <b>{effective_daily_rate:.2f} ₽</b>"
    text += (
        f"\n⚙️ Ежедневные списания: "
        f"<b>{'включены' if user.balance_mode_enabled and user.balance_autodebit_enabled else 'выключены'}</b>"
    )
    if balance_history:
        text += "\n\n📜 <b>Последние операции</b>"
        for item in balance_history:
            sign = "+" if item.direction.value == "credit" else "-"
            text += (
                f"\n• {item.created_at.strftime('%d.%m %H:%M')} · "
                f"{sign}{item.amount_rub:.2f} ₽ · {item.description or 'Операция'}"
            )

    if sub_count > 0:
        shown_subs = sorted(user.subscriptions, key=lambda s: s.expires_at, reverse=True)[:5]
        for sub in shown_subs:
            status_emoji, status_text = SUB_STATUS_MAP.get(
                sub.status.value, ("⚪", "Неизвестно")
            )
            key_short = "-"
            if _is_mtproto_placeholder(sub.vpn_key):
                key_short = "TG-ускоритель"
            else:
                display_key = get_subscription_display_key(sub)
                if display_key:
                    key_short = display_key[:30] + "..."

            text += "\n" + SUBSCRIPTION_ITEM.format(
                status_emoji=status_emoji,
                status=status_text,
                tariff=_subscription_tariff_label(sub, mtproto_accounts),
                expires=sub.expires_at.strftime("%d.%m.%Y"),
                key_short=key_short,
            )
    else:
        text += "\n\n" + NO_SUBSCRIPTIONS

    for acc in mtproto_accounts:
        proxy_links = _format_proxy_links(acc.secret)
        expires = "N/A"
        if acc.subscription_id:
            linked_sub = next(
                (s for s in user.subscriptions if s.id == acc.subscription_id),
                None,
            )
            if linked_sub:
                expires = linked_sub.expires_at.strftime("%d.%m.%Y")
        text += "\n" + MTPROTO_KEY_INFO.format(
            status_emoji="🟢",
            status="Активен",
            expires=expires,
            proxy_links=proxy_links,
        )

    if web_profile and web_profile.get("orders"):
        # Show only meaningful completed web orders in profile.
        visible_orders = [
            o for o in web_profile["orders"]
            if o.get("status") in ("delivered", "demo", "paid")
        ]
        if visible_orders:
            text += "\n\n🌐 <b>Покупки на сайте</b>"
            for order in visible_orders[:3]:
                status_text = {
                    "delivered": "Выдан",
                    "demo": "Демо",
                    "paid": "Оплачен",
                }.get(order.get("status"), order.get("status", ""))
                key_line = "\n🔗 Ключ: доступен" if order.get("subscription_url") else ""
                issued = _fmt_iso_msk(order.get("created_at"))
                expires = _fmt_iso_msk(order.get("access_expires_at"))
                dates_line = f"\nВыдан: {issued}"
                if order.get("access_expires_at"):
                    dates_line += f" · До: {expires}"
                text += (
                    f"\n\n• <b>{order['tariff_label']}</b> · {order['amount_rub']}₽"
                    f"\nСтатус: {status_text}{dates_line}"
                    f"\nЗаказ: <code>{order['order_id']}</code>{key_line}"
                )

    if active_sub_count > 0:
        from bot.utils.texts import WHATSAPP_PROXY_BONUS
        if wa_enabled_row and wa_enabled_row.value == "1" and wa_host_row and wa_host_row.value:
            text += "\n\n" + WHATSAPP_PROXY_BONUS.format(proxy_host=wa_host_row.value)

    if active_sub_count == 0 and has_expired_paid_subs:
        text += PROFILE_RENEW_HINT

    TG_MSG_LIMIT = 4096
    if len(text) > TG_MSG_LIMIT:
        text = text[: TG_MSG_LIMIT - 30] + "\n\n<i>…показаны не все записи</i>"

    await callback.message.edit_text(
        text,
        reply_markup=profile_kb(
            has_active_subs=active_sub_count > 0,
            has_device_manageable_subs=has_manageable_device_subs,
            purchase_button_text=purchase_button_text,
            has_recurring=has_recurring,
            recurring_active=recurring_active,
            balance_mode_enabled=bool(user.balance_mode_enabled),
            balance_autodebit_enabled=bool(user.balance_autodebit_enabled),
            site_profile_url=(
                f"{settings.webstore_api_base_url.rstrip('/')}/profile"
                if settings.webstore_public_enabled
                else None
            ),
            renewal_options=renewal_options,
            has_daily_charge_tariff_choice=len(renewal_options) > 1,
        ),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "balance_toggle")
async def balance_toggle(callback: CallbackQuery) -> None:
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == callback.from_user.id)
        )
        user = result.scalar_one_or_none()
        if not user:
            await callback.answer("Пользователь не найден", show_alert=True)
            return

        enabled = not (user.balance_mode_enabled and user.balance_autodebit_enabled)
        if enabled:
            ok, error = await enable_balance_mode(session, user)
            if not ok:
                await callback.answer(error or "Не удалось включить режим", show_alert=True)
                return
            alert_text = "Ежедневные списания включены"
        else:
            _, access_until = await disable_balance_mode(session, user)
            alert_text = (
                f"Доступ сохранён до {_fmt_msk(access_until)}"
                if access_until
                else "Ежедневные списания выключены"
            )
        await session.commit()

    await callback.answer(alert_text, show_alert=True)
    callback.data = "profile"
    await show_profile(callback)


@router.callback_query(F.data == "balance_history")
async def balance_history(callback: CallbackQuery) -> None:
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == callback.from_user.id)
        )
        user = result.scalar_one_or_none()
        if not user:
            await callback.answer("Пользователь не найден", show_alert=True)
            return

        tx_result = await session.execute(
            select(BalanceTransaction)
            .where(BalanceTransaction.user_id == user.id)
            .order_by(BalanceTransaction.created_at.desc(), BalanceTransaction.id.desc())
            .limit(20)
        )
        transactions = tx_result.scalars().all()

    if not transactions:
        text = "📜 <b>История баланса</b>\n\nОпераций пока нет."
    else:
        lines = ["📜 <b>История баланса</b>", ""]
        for item in transactions:
            sign = "+" if item.direction.value == "credit" else "-"
            lines.append(
                f"• <b>{item.created_at.strftime('%d.%m.%Y %H:%M')}</b>"
                f"\n{sign}{item.amount_rub:.2f} ₽"
                f"\n{item.description or 'Операция'}"
                f"\nОстаток: {item.balance_after_rub:.2f} ₽"
            )
            lines.append("")
        text = "\n".join(lines).strip()

    from bot.keyboards.client import back_to_menu_kb
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=back_to_menu_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "daily_charge_tariff_choice")
async def daily_charge_tariff_choice(callback: CallbackQuery) -> None:
    """Show available tariffs to choose which one to use for daily auto-charges."""
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton

    async with async_session() as session:
        user = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
        if not user:
            await callback.answer("Пользователь не найден", show_alert=True)
            return
        options = await _build_renewal_options(session, user)
        current_tariff_id = user.daily_charge_tariff_id

    if not options:
        await callback.answer("Нет активных тарифов для выбора", show_alert=True)
        return

    builder = InlineKeyboardBuilder()
    for tid, label, rate in options:
        marker = "✅ " if tid == current_tariff_id else ""
        builder.row(
            InlineKeyboardButton(
                text=f"{marker}{label} — {rate:.2f} ₽/день",
                callback_data=f"set_daily_tariff_{tid}",
            )
        )
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="profile"))

    text = "Выберите тариф для ежедневных списаний:\n\n"
    for tid, label, rate in options:
        text += f"• <b>{label}</b> — {rate:.2f} ₽/день\n"

    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("set_daily_tariff_"))
async def set_daily_tariff(callback: CallbackQuery) -> None:
    """Save chosen tariff for daily auto-charge."""
    tariff_id = int(callback.data.removeprefix("set_daily_tariff_"))

    async with async_session() as session:
        user = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
        if not user:
            await callback.answer("Пользователь не найден", show_alert=True)
            return
        tariff = await session.get(Tariff, tariff_id)
        if not tariff or not tariff.is_active:
            await callback.answer("Тариф недоступен", show_alert=True)
            return
        user.daily_charge_tariff_id = tariff_id
        await session.commit()

    await callback.answer(f"✅ Тариф «{tariff.label}» выбран для ежедневных списаний", show_alert=True)
    callback.data = "profile"
    await show_profile(callback)


@router.callback_query(F.data == "my_keys")
async def show_keys_shortcut(callback: CallbackQuery) -> None:
    """Shortcut from main menu — show first page of keys."""
    text, kb = await _build_keys_page(callback.from_user.id, page=1)
    if kb is None:
        await callback.answer(text, show_alert=True)
        return
    try:
        await callback.message.edit_text(
            text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True,
        )
    except Exception as e:
        if "message is not modified" not in str(e):
            await callback.message.answer(
                text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True,
            )
    await callback.answer()


@router.message(Command("keys"))
async def cmd_keys(message: Message) -> None:
    """Show keys via /keys command — sends as new message."""
    text, kb = await _build_keys_page(message.from_user.id, page=1)
    await message.answer(text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)


async def _build_keys_page(
    telegram_id: int, page: int = 1
) -> tuple[str, "InlineKeyboardMarkup | None"]:
    """Build keys page text and keyboard. Returns (text, keyboard) or (error_text, None)."""
    from bot.keyboards.client import profile_keys_kb

    async with async_session() as session:
        result = await session.execute(
            select(User)
            .options(
                selectinload(User.subscriptions)
                .selectinload(Subscription.server),
                selectinload(User.subscriptions)
                .selectinload(Subscription.tariff),
            )
            .where(User.telegram_id == telegram_id)
        )
        user = result.scalar_one_or_none()
        mtproto_result = await session.execute(
            select(MTProtoAccount)
            .where(MTProtoAccount.user_id == user.id)
            .where(MTProtoAccount.is_active == True)  # noqa: E712
        ) if user else None
        mtproto_accounts = mtproto_result.scalars().all() if mtproto_result else []
    if user:
        async with async_session() as session:
            included_slots = await get_included_device_slots(session)
        await sync_user_profile(user, user.subscriptions, mtproto_accounts)
    web_profile = await fetch_linked_web_profile(telegram_id)

    if not user:
        return "Пользователь не найден", None
    included_slots = locals().get("included_slots", 3)

    key_items: list[tuple[str, object]] = []
    for sub in user.subscriptions:
        if sub.status.value == "active" and not _is_mtproto_placeholder(sub.vpn_key):
            key_items.append(("vpn", sub))
    for acc in mtproto_accounts:
        key_items.append(("mtproto", acc))
    if web_profile:
        for order in web_profile.get("orders", []):
            if order.get("subscription_url"):
                key_items.append(("web", order))

    if not key_items:
        return "У вас пока нет активных ключей.\nНажмите «🛒 Купить доступ» в главном меню.", None

    total_items = len(key_items)
    total_pages = total_items
    page = max(1, min(page, total_pages))

    item_type, item = key_items[page - 1]

    if item_type == "vpn":
        sub = item
        location = sub.server.location if sub.server else "N/A"
        emoji = sub.server.country_emoji if sub.server else "🌍"
        expires = sub.expires_at.strftime("%d.%m.%Y")
        provider_label = _provider_label(sub)
        tariff_line = ""
        if sub.tariff:
            tariff_line = f"📦 Тариф: {sub.tariff.label}\n"
        elif provider_label:
            tariff_line = f"📦 Провайдер: {provider_label}\n"
        text = (
            f"🔗 <b>{emoji} {location}</b>\n"
            f"{tariff_line}"
            f"📅 До: {expires}\n"
            f"🖥 Устройств: {_subscription_device_slots(sub, included_slots)}\n\n"
            f"<code>{get_subscription_display_key(sub) or sub.vpn_key}</code>\n"
            f"<i>Нажмите на ссылку выше, чтобы скопировать её.</i>"
        )
        if is_adapt_subscription(sub):
            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
            from aiogram.utils.keyboard import InlineKeyboardBuilder as IKB
            from bot.models import AdaptSubscription
            adapt_kb_builder = IKB()
            # Load freeze state from DB
            adapt_is_frozen = False
            async with async_session() as _sess:
                _ar = (await _sess.execute(
                    select(AdaptSubscription).where(AdaptSubscription.subscription_id == sub.id)
                )).scalar_one_or_none()
                if _ar:
                    adapt_is_frozen = bool(_ar.is_frozen)
            if adapt_is_frozen:
                adapt_kb_builder.row(InlineKeyboardButton(
                    text="▶️ Разморозить", callback_data=f"adapt_unfreeze:{sub.id}"
                ))
                text += "\n\n❄️ <i>Подписка заморожена.</i>"
            else:
                adapt_kb_builder.row(InlineKeyboardButton(
                    text="❄️ Заморозить", callback_data=f"adapt_freeze:{sub.id}"
                ))
            adapt_kb_builder.row(
                InlineKeyboardButton(text="⚡️ Докупить трафик", callback_data=f"adapt_traffic:{sub.id}"),
                InlineKeyboardButton(text="⬆️ Апгрейд", callback_data=f"adapt_upgrade_menu:{sub.id}"),
            )
            # Add nav buttons from standard kb
            nav_buttons = []
            if page > 1:
                nav_buttons.append(InlineKeyboardButton(text="⬅️", callback_data=f"my_keys_{page-1}"))
            nav_buttons.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="ignore"))
            if page < total_pages:
                nav_buttons.append(InlineKeyboardButton(text="➡️", callback_data=f"my_keys_{page+1}"))
            adapt_kb_builder.row(*nav_buttons)
            adapt_kb_builder.row(InlineKeyboardButton(text="◀️ В профиль", callback_data="profile"))
            return text, adapt_kb_builder.as_markup()
    elif item_type == "mtproto":
        acc = item
        linked_sub = next((s for s in user.subscriptions if s.id == acc.subscription_id), None)
        expires = linked_sub.expires_at.strftime("%d.%m.%Y") if linked_sub else "N/A"
        text = (
            f"📱 <b>Telegram-ускоритель</b>\n"
            f"📅 До: {expires}\n\n"
            f"{_format_proxy_links(acc.secret)}\n\n"
            f"<i>Нажмите на одну из ссылок выше, чтобы подключить ускоритель.</i>"
        )
    else:
        order = item
        issued = _fmt_iso_msk(order.get("created_at"))
        expires = _fmt_iso_msk(order.get("access_expires_at"))
        expires_line = f"\n📅 До: {expires}" if order.get("access_expires_at") else ""
        text = (
            f"🌐 <b>Покупка на сайте — {order['tariff_label']}</b>\n"
            f"📆 Выдан: {issued}{expires_line}\n\n"
            f"<code>{order['subscription_url']}</code>\n"
            f"<i>Нажмите на ссылку выше, чтобы скопировать её.</i>"
        )

    return text, profile_keys_kb(page, total_pages)


@router.callback_query(F.data.startswith("my_keys_"))
async def show_keys_paginated(callback: CallbackQuery) -> None:
    """Show user's active subscription keys with pagination."""
    parts = callback.data.split("_")
    page = 1
    if len(parts) > 2:
        try:
            page = int(parts[2])
        except ValueError:
            page = 1

    text, kb = await _build_keys_page(callback.from_user.id, page)
    if kb is None:
        await callback.answer(text, show_alert=True)
        return

    try:
        await callback.message.edit_text(
            text,
            reply_markup=kb,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        if "message is not modified" not in str(e):
            await callback.message.answer(
                text,
                reply_markup=kb,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )

    await callback.answer()


@router.callback_query(F.data == "recurring_toggle_off")
async def recurring_off(callback: CallbackQuery) -> None:
    """Disable auto-renewal."""
    async with async_session() as session:
        result = await session.execute(
            select(RecurringPaymentProfile)
            .join(User, User.id == RecurringPaymentProfile.user_id)
            .where(User.telegram_id == callback.from_user.id)
            .where(RecurringPaymentProfile.is_active == True)  # noqa: E712
        )
        profile = result.scalar_one_or_none()
        if not profile:
            await callback.answer("Автопродление не найдено", show_alert=True)
            return

        profile.consent_granted = False
        await session.commit()

    await callback.answer()
    await callback.message.answer(RECURRING_TOGGLE_OFF, parse_mode="HTML")
    # Refresh profile view
    await show_profile(callback)


@router.callback_query(F.data == "recurring_toggle_on")
async def recurring_on(callback: CallbackQuery) -> None:
    """Re-enable auto-renewal."""
    async with async_session() as session:
        result = await session.execute(
            select(RecurringPaymentProfile)
            .options(selectinload(RecurringPaymentProfile.subscription))
            .join(User, User.id == RecurringPaymentProfile.user_id)
            .where(User.telegram_id == callback.from_user.id)
            .where(RecurringPaymentProfile.is_active == True)  # noqa: E712
        )
        profile = result.scalar_one_or_none()
        if not profile:
            await callback.answer("Автопродление не найдено", show_alert=True)
            return

        profile.consent_granted = True
        profile.payment_attempt_count = 0
        profile.last_payment_attempt = None
        # Set next charge to subscription expiry if available
        if profile.subscription and profile.subscription.expires_at:
            profile.next_charge_at = profile.subscription.expires_at
        await session.commit()

        payment_label = profile.payment_method_label or profile.provider

    await callback.answer()
    await callback.message.answer(
        RECURRING_TOGGLE_ON.format(payment_method=payment_label),
        parse_mode="HTML",
    )
    # Refresh profile view
    await show_profile(callback)


# ── Adapt Group: freeze / unfreeze / upgrade / traffic ─────────────────

@router.callback_query(F.data.startswith("adapt_freeze:"))
async def adapt_freeze(callback: CallbackQuery) -> None:
    """Freeze an Adapt subscription."""
    sub_id = int(callback.data.split(":")[1])
    async with async_session() as session:
        from bot.models import AdaptSubscription
        from bot.services.adapt_api import AdaptAPI, AdaptAPIError
        sub = await session.get(Subscription, sub_id)
        if not sub:
            await callback.answer("Подписка не найдена", show_alert=True)
            return

        user_result = await session.execute(
            select(User).where(User.telegram_id == callback.from_user.id)
        )
        user = user_result.scalar_one_or_none()
        if not user or sub.user_id != user.id:
            await callback.answer("Нет доступа", show_alert=True)
            return

        result = await session.execute(
            select(AdaptSubscription).where(AdaptSubscription.subscription_id == sub_id)
        )
        adapt_record = result.scalar_one_or_none()
        if not adapt_record:
            await callback.answer("Подписка Adapt не найдена", show_alert=True)
            return
        if adapt_record.is_frozen:
            await callback.answer("Подписка уже заморожена", show_alert=True)
            return

        try:
            resp = await AdaptAPI().freeze_subscription(adapt_record.adapt_uuid)
        except AdaptAPIError as exc:
            await callback.answer(f"Ошибка заморозки: {exc}", show_alert=True)
            return

        from datetime import datetime
        adapt_record.is_frozen = True
        adapt_record.frozen_at = datetime.utcnow()
        await session.commit()

    await callback.answer("✅ Подписка заморожена")
    await show_profile(callback)


@router.callback_query(F.data.startswith("adapt_unfreeze:"))
async def adapt_unfreeze(callback: CallbackQuery) -> None:
    """Unfreeze an Adapt subscription."""
    sub_id = int(callback.data.split(":")[1])
    async with async_session() as session:
        from bot.models import AdaptSubscription
        from bot.services.adapt_api import AdaptAPI, AdaptAPIError
        sub = await session.get(Subscription, sub_id)
        if not sub:
            await callback.answer("Подписка не найдена", show_alert=True)
            return

        user_result = await session.execute(
            select(User).where(User.telegram_id == callback.from_user.id)
        )
        user = user_result.scalar_one_or_none()
        if not user or sub.user_id != user.id:
            await callback.answer("Нет доступа", show_alert=True)
            return

        result = await session.execute(
            select(AdaptSubscription).where(AdaptSubscription.subscription_id == sub_id)
        )
        adapt_record = result.scalar_one_or_none()
        if not adapt_record:
            await callback.answer("Подписка Adapt не найдена", show_alert=True)
            return
        if not adapt_record.is_frozen:
            await callback.answer("Подписка не заморожена", show_alert=True)
            return

        try:
            resp = await AdaptAPI().unfreeze_subscription(adapt_record.adapt_uuid)
        except AdaptAPIError as exc:
            await callback.answer(f"Ошибка разморозки: {exc}", show_alert=True)
            return

        adapt_record.is_frozen = False
        adapt_record.frozen_at = None
        if resp.get("end_date"):
            from datetime import datetime
            try:
                adapt_record.end_date = datetime.fromisoformat(
                    str(resp["end_date"]).replace("Z", "+00:00")
                )
                sub.expires_at = adapt_record.end_date
            except Exception:
                pass
        await session.commit()

    await callback.answer("✅ Подписка разморожена")
    await show_profile(callback)


@router.callback_query(F.data.startswith("adapt_traffic:"))
async def adapt_traffic(callback: CallbackQuery) -> None:
    """Show traffic purchase options for an Adapt subscription."""
    sub_id = int(callback.data.split(":")[1])
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    gb_options = [10, 50, 100]
    buttons = [
        [InlineKeyboardButton(text=f"⚡️ +{gb} ГБ", callback_data=f"adapt_buy_traffic:{sub_id}:{gb}")]
        for gb in gb_options
    ]
    buttons.append([InlineKeyboardButton(text="← Назад", callback_data="profile")])
    await callback.message.edit_text(
        "⚡️ <b>Докупить трафик</b>\n\nВыберите количество гигабайт:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adapt_buy_traffic:"))
async def adapt_buy_traffic(callback: CallbackQuery) -> None:
    """Purchase additional traffic for an Adapt subscription."""
    parts = callback.data.split(":")
    sub_id = int(parts[1])
    gb_amount = int(parts[2])

    async with async_session() as session:
        from bot.models import AdaptSubscription
        from bot.services.adapt_api import AdaptAPI, AdaptAPIError
        sub = await session.get(Subscription, sub_id)
        if not sub:
            await callback.answer("Подписка не найдена", show_alert=True)
            return

        user_result = await session.execute(
            select(User).where(User.telegram_id == callback.from_user.id)
        )
        user = user_result.scalar_one_or_none()
        if not user or sub.user_id != user.id:
            await callback.answer("Нет доступа", show_alert=True)
            return

        result = await session.execute(
            select(AdaptSubscription).where(AdaptSubscription.subscription_id == sub_id)
        )
        adapt_record = result.scalar_one_or_none()
        if not adapt_record:
            await callback.answer("Подписка Adapt не найдена", show_alert=True)
            return

        try:
            resp = await AdaptAPI().purchase_traffic(adapt_record.adapt_uuid, gb_amount)
        except AdaptAPIError as exc:
            await callback.answer(f"Ошибка: {exc}", show_alert=True)
            return

        total_price = resp.get("total_price", "?")

    await callback.answer(f"✅ Куплено {gb_amount} ГБ (списано {total_price} USD)")
    await show_profile(callback)


@router.callback_query(F.data.startswith("adapt_upgrade:"))
async def adapt_upgrade_menu(callback: CallbackQuery) -> None:
    """Show upgrade plan options for an Adapt subscription."""
    sub_id = int(callback.data.split(":")[1])
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    from bot.services.adapt_api import AdaptAPI, AdaptAPIError

    try:
        plans = await AdaptAPI().list_plans()
    except AdaptAPIError as exc:
        await callback.answer(f"Ошибка загрузки планов: {exc}", show_alert=True)
        return

    active_plans = [p for p in plans if p.get("is_active") and not p.get("is_trial")]
    if not active_plans:
        await callback.answer("Нет доступных планов для улучшения", show_alert=True)
        return

    buttons = []
    for plan in active_plans:
        name = plan.get("name", "?")
        price = plan.get("retail_price_usd") or plan.get("price_usd", "?")
        days = plan.get("days", "?")
        plan_uuid = plan.get("uuid", "")
        buttons.append([
            InlineKeyboardButton(
                text=f"⬆️ {name} — {days} дн. / {price} USD",
                callback_data=f"adapt_do_upgrade:{sub_id}:{plan_uuid}",
            )
        ])
    buttons.append([InlineKeyboardButton(text="← Назад", callback_data="profile")])

    await callback.message.edit_text(
        "⬆️ <b>Улучшить тариф</b>\n\nВыберите новый план:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adapt_do_upgrade:"))
async def adapt_do_upgrade(callback: CallbackQuery) -> None:
    """Execute upgrade to the selected Adapt plan."""
    parts = callback.data.split(":")
    sub_id = int(parts[1])
    new_plan_uuid = parts[2]

    async with async_session() as session:
        from bot.models import AdaptSubscription
        from bot.services.adapt_api import AdaptAPI, AdaptAPIError
        sub = await session.get(Subscription, sub_id)
        if not sub:
            await callback.answer("Подписка не найдена", show_alert=True)
            return

        user_result = await session.execute(
            select(User).where(User.telegram_id == callback.from_user.id)
        )
        user = user_result.scalar_one_or_none()
        if not user or sub.user_id != user.id:
            await callback.answer("Нет доступа", show_alert=True)
            return

        result = await session.execute(
            select(AdaptSubscription).where(AdaptSubscription.subscription_id == sub_id)
        )
        adapt_record = result.scalar_one_or_none()
        if not adapt_record:
            await callback.answer("Подписка Adapt не найдена", show_alert=True)
            return

        try:
            resp = await AdaptAPI().upgrade_subscription(adapt_record.adapt_uuid, new_plan_uuid)
        except AdaptAPIError as exc:
            await callback.answer(f"Ошибка улучшения: {exc}", show_alert=True)
            return

        adapt_record.adapt_plan_uuid = new_plan_uuid
        if resp.get("devices"):
            sub.device_slots = int(resp["devices"])
        await session.commit()

        upgrade_price = resp.get("upgrade_price", "?")

    await callback.answer(f"✅ Тариф улучшен! Стоимость: {upgrade_price} USD")
    await show_profile(callback)
