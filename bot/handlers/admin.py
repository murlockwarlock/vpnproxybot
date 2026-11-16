"""Admin panel - stats, servers, users, manual key gen. Access: ADMIN_IDS from .env only."""

from __future__ import annotations

import logging
import re
import uuid
import html
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select, delete, text, or_, update
from sqlalchemy.orm import selectinload

from bot.config import settings
from bot.database import async_session
from bot.keyboards.admin import (
    ad_link_detail_kb,
    ad_link_kind_kb,
    ad_links_menu_kb,
    admin_back_kb,
    admin_menu_kb,
    server_actions_kb,
    server_list_kb,
    settings_kb,
    stats_back_kb,
    stats_menu_kb,
    tariff_actions_kb,
    tariffs_admin_kb,
    user_actions_kb,
    user_search_kb,
    user_reset_confirm_kb,
)
from bot.keyboards.client import back_to_menu_kb
from bot.models import (
    AdTrackingLink,
    BalanceTopUp,
    BotSettings,
    MTProtoAccount,
    Payment,
    PaymentMethod,
    PaymentStatus,
    Partner,
    Platform,
    PlatformGuide,
    ProxyAccount,
    RecurringPaymentProfile,
    Server,
    SubStatus,
    Subscription,
    SubscriptionNotificationLog,
    Tariff,
    TariffType,
    User,
    AdaptSubscription,
    AdminActionLog,
    Feedback,
    FeedbackMessage,
    ManualSubscriptionOperation,
    VhqSubscriptionFulfillment,
)
from bot.services.admin_audit import write_admin_audit
from bot.services.client_names import build_client_name
from bot.services.device_slots import get_included_device_slots
from bot.services.legal_docs import LEGAL_DOCS, get_all_legal_doc_urls
from bot.services.tariff_utils import format_duration_days, format_subscription_duration
from bot.services.adapt_routing import is_adapt_subscription, is_adapt_tariff, get_adapt_uuid_from_subscription
from bot.services.purchase_intent import get_paid_adapt_upgrade_plans
from bot.services.notifications import notify_admins_issue
from bot.services.provisioning_issues import (
    describe_access_provision_result,
    recommend_access_provision_action,
    safe_access_provision_reason,
)
from bot.services.subscription_semantics import (
    is_adapt_customer_upgrade_eligible,
    is_adapt_trial_subscription,
    is_adapt_trial_tariff,
)
from bot.services.vhq_routing import is_vhq_subscription, is_vhq_tariff
from bot.services.vhq_subscription_proxy import (
    get_subscription_display_key,
    resolve_vhq_mirror_url,
)
from bot.services import vpn_manager
from bot.utils.device_info import adapt_device_details, describe_user_agent, format_activity

from bot.utils.texts import (
    ADMIN_PANEL,
    ADMIN_SERVER_INFO,
    ADMIN_SETTINGS_HEADER,
    ADMIN_STATS,
    ADMIN_STATS_UNIFIED,
    ADMIN_TARIFF_ADDED,
    ADMIN_TARIFF_DELETED,
    ADMIN_TARIFF_TOGGLED,
    ADMIN_TARIFFS_HEADER,
    ADMIN_USER_INFO,
    GUIDE_ANDROID,
    GUIDE_IOS,
    GUIDE_MAC,
    GUIDE_WINDOWS,
    GUIDE_ANDROID_TV,
    KEY_DELIVERED,
    MANUAL_KEY_DELIVERED,
    MTPROTO_KEY_BULK,
    SERVER_ADDED,
    SERVER_TOGGLED,
)

logger = logging.getLogger(__name__)
router = Router(name="admin")


async def _notify_manual_provisioning_issue(
    callback: CallbackQuery,
    *,
    user: User,
    subscription_id: int | None,
    action: str,
    issue,
) -> None:
    if issue.stage == "post_mutation":
        confirmation = "изменение подтверждено"
    elif issue.confirmed_rejection:
        confirmation = "отклонено до изменения"
    else:
        confirmation = "неизвестно"
    reason = safe_access_provision_reason(issue)
    result = describe_access_provision_result(issue)
    recommendation = recommend_access_provision_action(issue)
    username = f"@{user.username}" if getattr(user, "username", None) else "без username"
    client = f"{getattr(user, 'full_name', None) or 'без имени'} ({username}, Telegram ID {user.telegram_id})"
    details = [
        f"Клиент: {client}",
        f"Подписка: {subscription_id or 'новая'}",
        f"Действие: {action or 'manual'}",
        f"Провайдер: {issue.provider}",
        f"Код: {issue.code}",
        f"HTTP статус: {issue.status or 'неизвестен'}",
        f"Причина: {reason}",
        f"Результат операции: {result}",
        f"Рекомендация: {recommendation}",
        f"Стадия операции: {issue.stage}",
        f"Подтверждение провайдера: {confirmation}",
    ]
    try:
        delivered = await notify_admins_issue(
            callback.bot,
            title="Ошибка ручной выдачи или улучшения",
            details=details,
        )
        if not delivered:
            logger.error(
                "manual provisioning alert delivery failed: provider=%s code=%s reason=%s result=false",
                issue.provider,
                issue.code,
                reason,
            )
        else:
            logger.info(
                "manual provisioning alert delivered: provider=%s code=%s result=true",
                issue.provider,
                issue.code,
            )
    except Exception as alert_error:
        logger.error(
            "manual provisioning alert delivery failed: provider=%s code=%s reason=%s error=%s",
            issue.provider,
            issue.code,
            reason,
            alert_error,
        )

import asyncio
from typing import Any, Callable, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

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


def _format_dt_msk(dt: datetime | None, include_time: bool = True) -> str:
    if not dt:
        return "—"
    from datetime import timezone, timedelta
    msk = timezone(timedelta(hours=3))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    fmt = "%d-%m-%Y %H:%M МСК" if include_time else "%d-%m-%Y"
    return dt.astimezone(msk).strftime(fmt)


def _fmt_dt_str_msk(raw: Any) -> str:
    if not raw or str(raw) in ("—", "None", ""):
        return "—"
    if isinstance(raw, datetime):
        return _format_dt_msk(raw)
    raw_str = str(raw).rstrip("Z")
    try:
        dt = datetime.fromisoformat(raw_str)
        return _format_dt_msk(dt)
    except Exception:
        return str(raw)[:16]


def _code(value: object) -> str:
    return f"<code>{html.escape(str(value or ''))}</code>"


def _format_subscription_key_block(subscription: Subscription) -> str:
    public_key = get_subscription_display_key(subscription) or subscription.vpn_key
    if not public_key or public_key == "mtproto_only":
        return "🔗 Ключ:\n<code>TG-ускоритель</code>"

    if is_vhq_subscription(subscription):
        original_key = str(subscription.vpn_key or "").strip()
        lines = [f"🔗 Наш ключ:\n{_code(public_key)}"]
        if original_key and original_key != public_key:
            lines.append(f"🔗 Оригинал VHQ:\n{_code(original_key)}")
        return "\n\n".join(lines)

    if is_adapt_subscription(subscription):
        adapt_uuid = get_adapt_uuid_from_subscription(subscription)
        original_key = f"https://network-api.adaptgroup.app/sub/{adapt_uuid}" if adapt_uuid else ""
        lines = [f"🔗 Наш ключ:\n{_code(public_key)}"]
        if original_key and original_key != public_key:
            lines.append(f"🔗 Оригинал Adapt:\n{_code(original_key)}")
        return "\n\n".join(lines)

    return f"🔗 Ключ:\n{_code(public_key)}"


def _format_vhq_mirror_key_block(public_url: str, *, label: str = "Ссылка") -> str:
    url = str(public_url or "").strip()
    if not url:
        return ""

    token_payload = resolve_vhq_mirror_url(url)
    if token_payload and token_payload.get("kind") == "upstream":
        upstream_url = str(token_payload.get("upstream_url") or "").strip()
        if upstream_url and upstream_url != url:
            return "\n".join([
                f"Наша ссылка: {_code(url)}",
                f"Оригинал VHQ: {_code(upstream_url)}",
            ])

    return f"{label}: {_code(url)}"


async def _format_external_key_block(key_value: str, *, label: str = "Ключ") -> str:
    key = str(key_value or "").strip()
    if not key:
        return ""

    token_payload = resolve_vhq_mirror_url(key)
    if token_payload and token_payload.get("kind") == "subscription":
        subscription_id = int(token_payload["subscription_id"])
        async with async_session() as session:
            subscription = await session.get(Subscription, subscription_id)
            if subscription and is_vhq_subscription(subscription):
                return _format_subscription_key_block(subscription)

    return _format_vhq_mirror_key_block(key, label=label)


def _format_ad_source_kind(value: str | None) -> str:
    mapping = {
        "channels": "Channels",
        "bots": "Bots",
        "search": "Search",
        "custom": "Custom",
    }
    return mapping.get((value or "").strip().lower(), value or "-")


def _ad_link_prefix(kind: str) -> str:
    mapping = {
        "channels": "ch",
        "bots": "bot",
        "search": "search",
        "custom": "src",
    }
    return mapping.get(kind, "src")


def _slugify_ad_link_title(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug[:24]


def _build_ad_link_code(kind: str, title: str) -> str:
    prefix = _ad_link_prefix(kind)
    slug = _slugify_ad_link_title(title)
    suffix = slug or uuid.uuid4().hex[:8]
    return f"{prefix}_{suffix}"[:64]


async def _build_ad_deep_link(bot, code: str) -> str:
    bot_info = await bot.get_me()
    return f"https://t.me/{bot_info.username}?start=ads_{code}"


def _manual_key_tariff_provider_label(tariff: Tariff) -> str:
    if tariff.tariff_type == TariffType.TG_PROXY:
        return "TG-прокси"
    if tariff.tariff_type == TariffType.BOTH:
        if is_adapt_tariff(tariff):
            return "Adapt + TG"
        return "VHQ + TG" if is_vhq_tariff(tariff) else "Marzban + TG"
    if is_adapt_tariff(tariff):
        return "Adapt"
    return "VHQ" if is_vhq_tariff(tariff) else "Marzban"


def _build_manual_key_tariff_kb(
    tariffs: list[Tariff],
    *,
    back_callback: str,
    operation_id: int | None = None,
) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    type_headers = {
        TariffType.VPN: "Весь интернет",
        TariffType.TG_PROXY: "TG-прокси",
        TariffType.BOTH: "Весь интернет + TG",
    }
    current_type = None
    for tariff in sorted(tariffs, key=lambda item: (item.tariff_type.value, item.price_rub, item.days, item.id)):
        if tariff.tariff_type != current_type:
            current_type = tariff.tariff_type
            kb.row(
                InlineKeyboardButton(
                    text=type_headers.get(current_type, str(current_type)),
                    callback_data="noop",
                )
            )
        duration = format_duration_days(int(tariff.days or 0))
        import re
        dev_match = re.search(r"(\d+)\s*📱", tariff.label or "")
        devices_str = f" • {dev_match.group(1)}📱" if dev_match else ""
        cb_data = (
            f"adm_key_tariff_{operation_id}_{tariff.id}"
            if operation_id is not None
            else f"adm_key_tariff_{tariff.id}"
        )
        kb.row(
            InlineKeyboardButton(
                text=f"{_manual_key_tariff_provider_label(tariff)} | {duration}{devices_str} | {tariff.price_rub}₽",
                callback_data=cb_data,
            )
        )
    kb.row(InlineKeyboardButton(text="◀️ Назад", callback_data=back_callback))
    return kb


async def _get_ad_link_stats(session, code: str) -> dict[str, int]:
    visitors = int(await session.scalar(select(func.count(User.id)).where(User.ad_source == code)) or 0)
    buyers = int(
        await session.scalar(
            select(func.count(func.distinct(Payment.user_id)))
            .select_from(Payment)
            .join(User, User.id == Payment.user_id)
            .where(User.ad_source == code, Payment.status == PaymentStatus.COMPLETED)
        )
        or 0
    )
    completed_payments = int(
        await session.scalar(
            select(func.count(Payment.id))
            .select_from(Payment)
            .join(User, User.id == Payment.user_id)
            .where(User.ad_source == code, Payment.status == PaymentStatus.COMPLETED)
        )
        or 0
    )
    return {
        "visitors": visitors,
        "buyers": buyers,
        "completed_payments": completed_payments,
    }


async def _render_ad_links(message, bot, edit: bool = True) -> None:
    async with async_session() as session:
        links = (
            await session.execute(select(AdTrackingLink).order_by(AdTrackingLink.created_at.desc()))
        ).scalars().all()
        rows: list[tuple[int, str, bool, int, int]] = []
        for link in links:
            stats = await _get_ad_link_stats(session, link.code)
            rows.append((link.id, link.title, bool(link.is_active), stats["visitors"], stats["buyers"]))

    text = (
        "📣 <b>Рекламные ссылки</b>\n\n"
        "Здесь создаются отдельные ссылки для Telegram Ads и ручных размещений.\n"
        "На кнопках показано: <b>пришло / купило</b>."
    )
    markup = ad_links_menu_kb(rows)
    if edit:
        await message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=markup, parse_mode="HTML")


async def _show_ad_link_detail(message, bot, link_id: int, edit: bool = True) -> None:
    async with async_session() as session:
        link = await session.get(AdTrackingLink, link_id)
        if not link:
            text = "❌ Рекламная ссылка не найдена."
            if edit:
                await message.edit_text(text, reply_markup=admin_back_kb(), parse_mode="HTML")
            else:
                await message.answer(text, reply_markup=admin_back_kb(), parse_mode="HTML")
            return
        stats = await _get_ad_link_stats(session, link.code)

    deep_link = await _build_ad_deep_link(bot, link.code)
    visitors = stats["visitors"]
    buyers = stats["buyers"]
    payments_count = stats["completed_payments"]
    conversion = (buyers / visitors * 100.0) if visitors else 0.0
    status = "Активна" if link.is_active else "Выключена"
    text = (
        f"📣 <b>{link.title}</b>\n\n"
        f"Статус: <b>{status}</b>\n"
        f"Тип: <b>{_format_ad_source_kind(link.source_kind)}</b>\n"
        f"Код: <code>{link.code}</code>\n\n"
        f"Ссылка:\n<code>{deep_link}</code>\n\n"
        f"Статистика:\n"
        f"• Пришло пользователей: <b>{visitors}</b>\n"
        f"• Купили хотя бы раз: <b>{buyers}</b>\n"
        f"• Всего успешных оплат: <b>{payments_count}</b>\n"
        f"• Конверсия в покупку: <b>{conversion:.1f}%</b>\n\n"
        f"Создана: <b>{_format_dt_msk(link.created_at)}</b>"
    )
    markup = ad_link_detail_kb(link.id, bool(link.is_active))
    if edit:
        await message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=markup, parse_mode="HTML")

ESCAPE_COMMANDS = {"/start", "/help", "/admin", "/policy", "/agree", "/oferta"}


# ── FSM States ────────────────────────────────────────

class AdminStates(StatesGroup):
    waiting_problem_payment_search = State()
    waiting_user_search           = State()
    waiting_user_id               = State()
    waiting_user_username         = State()
    waiting_user_link             = State()
    waiting_server_data           = State()
    waiting_manual_key_user       = State()
    waiting_manual_key_server     = State()
    waiting_manual_key_tariff     = State()
    # Tariff creation
    waiting_tariff_type           = State()
    waiting_tariff_days           = State()
    waiting_tariff_label          = State()
    waiting_tariff_price_rub      = State()
    waiting_tariff_price_stars    = State()
    # Tariff editing
    waiting_tariff_edit_label     = State()
    waiting_tariff_edit_days      = State()
    waiting_tariff_edit_price_rub = State()
    waiting_tariff_edit_price_stars = State()
    waiting_tariff_edit_adapt_uuid = State()
    waiting_tariff_edit_vhq_tier = State()
    # Settings
    waiting_max_devices           = State()
    waiting_daily_charge_rub      = State()
    waiting_device_price_rub      = State()
    waiting_device_price_stars    = State()
    waiting_whatsapp_host         = State()
    waiting_cashback_pct          = State()
    waiting_cashback_notify_text  = State()
    waiting_balance_reminder_text = State()
    waiting_legal_doc_url         = State()
    # Platform guide media
    waiting_guide_media           = State()
    waiting_guide_text            = State()
    guide_buttons                 = State()
    guide_btn_type                = State()
    guide_btn_text                = State()
    guide_btn_url                 = State()
    # Ad links
    waiting_ad_link_title         = State()
    # Feedback reply
    waiting_feedback_reply        = State()


# ── Guard: /commands don't interrupt admin FSM flows ──

@router.message(Command("start"), AdminStates())
async def _admin_state_start(message: Message, state: FSMContext) -> None:
    from bot.handlers.start import cmd_start

    await cmd_start(message, state)


@router.message(Command("help"), AdminStates())
async def _admin_state_help(message: Message, state: FSMContext) -> None:
    from bot.handlers.start import show_help_command

    await show_help_command(message, state)


@router.message(Command("admin"), AdminStates())
async def _admin_state_admin(message: Message, state: FSMContext) -> None:
    await cmd_admin(message, state)


@router.message(Command("policy"), AdminStates())
async def _admin_state_policy(message: Message, state: FSMContext) -> None:
    from bot.handlers.start import show_policy_command

    await state.clear()
    await show_policy_command(message)


@router.message(Command("agree"), AdminStates())
async def _admin_state_agree(message: Message, state: FSMContext) -> None:
    from bot.handlers.start import show_agree_command

    await state.clear()
    await show_agree_command(message)


@router.message(Command("oferta"), AdminStates())
async def _admin_state_oferta(message: Message, state: FSMContext) -> None:
    from bot.handlers.start import show_oferta_command

    await state.clear()
    await show_oferta_command(message)


@router.message(AdminStates(), ~StateFilter(AdminStates.waiting_problem_payment_search), F.text.startswith("/"))
async def _guard_admin_commands(message: Message) -> None:
    """Prevent /commands from aborting ongoing admin input flows."""
    command = (message.text or "").split(maxsplit=1)[0].lower()
    if command in ESCAPE_COMMANDS:
        return
    await message.answer(
        "⚠️ Завершите текущее действие или нажмите «◀️ Админ-панель» для отмены."
    )


# ── Admin check ───────────────────────────────────────

def _is_admin(user_id: int) -> bool:
    return settings.is_admin(user_id)


# ── /admin Entry ──────────────────────────────────────

@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext) -> None:
    """Open admin panel - only accessible to IDs listed in ADMIN_IDS (.env)."""
    await state.clear()
    if not _is_admin(message.from_user.id):
        await message.answer(
            f"⛔️ Нет доступа к админ-панели.\nВаш Telegram ID: <code>{message.from_user.id}</code>",
            parse_mode="HTML",
        )
        return
    await message.answer(
        ADMIN_PANEL,
        reply_markup=admin_menu_kb(),
        parse_mode="HTML",
    )


@router.message(Command("reconcile_yookassa"))
async def cmd_reconcile_yookassa(message: Message) -> None:
    """Manually reconcile and fulfill a YooKassa payment by local ID or YooKassa UUID."""
    if not _is_admin(message.from_user.id):
        return

    args = (message.text or "").split()[1:]
    if not args:
        await message.answer(
            "<b>Использование:</b> <code>/reconcile_yookassa &lt;payment_id или provider_uuid&gt;</code>\n\n"
            "Пример: <code>/reconcile_yookassa 306</code>\n"
            "или: <code>/reconcile_yookassa 327a4555-000f-5001-8000-1b97ab7e9018</code>",
            parse_mode="HTML",
        )
        return

    target = args[0].strip()
    provider_payment_id = None

    async with async_session() as session:
        if target.isdigit():
            p = await session.get(Payment, int(target))
            if p and p.provider_payment_id:
                provider_payment_id = p.provider_payment_id
            else:
                topup = await session.get(BalanceTopUp, int(target))
                if topup and topup.provider_payment_id:
                    provider_payment_id = topup.provider_payment_id
        else:
            provider_payment_id = target

    if not provider_payment_id:
        await message.answer(
            f"❌ Не удалось найти платеж по запросу <code>{html.escape(target)}</code> в базе данных.",
            parse_mode="HTML",
        )
        return

    await message.answer(
        f"⏳ Запуск аутентифицированной сверки YooKassa для <code>{html.escape(provider_payment_id)}</code>...",
        parse_mode="HTML",
    )

    from bot.services.yookassa_reconciliation import verify_and_fulfill_yookassa_payment

    try:
        success, code = await verify_and_fulfill_yookassa_payment(
            message.bot,
            provider_payment_id,
            source="admin_manual",
        )
        if success:
            await message.answer(
                f"✅ <b>Сверка YooKassa завершена успешно!</b>\n\n"
                f"Provider ID: <code>{html.escape(provider_payment_id)}</code>\n"
                f"Статус: <code>{code}</code>",
                parse_mode="HTML",
            )
        else:
            await message.answer(
                f"⚠️ <b>Результат сверки:</b> <code>{code}</code>\n\n"
                f"Provider ID: <code>{html.escape(provider_payment_id)}</code>\n"
                f"Услуга не выдана или платеж не подтвержден YooKassa.",
                parse_mode="HTML",
            )
    except Exception as exc:
        logger.exception("Manual reconciliation failed for %s: %s", provider_payment_id, exc)
        await message.answer(f"❌ Ошибка сверки: <code>{html.escape(str(exc))}</code>", parse_mode="HTML")


# ── Problem Payments Dashboard ─────────────────────────


async def _show_problem_payment_card(
    target: Message | CallbackQuery,
    item: Any,
    source: str = "main",
) -> None:
    """Helper to render the detailed inspection card."""
    from bot.keyboards.admin import problem_payment_detail_kb

    tg = item.telegram_id or "—"
    uname = f"@{item.username}" if item.username else "—"
    fname = item.full_name or "—"

    from bot.services.problem_payments import format_age, get_cached_live_status
    cached = get_cached_live_status(item.item_type, item.item_id)
    if cached:
        c_age = format_age(cached.checked_at, datetime.utcnow())
        yk_status_str = f"{cached.status_label} (проверено {c_age} назад)"
    else:
        yk_status_str = item.verified_provider_status or "не проверен"

    supplier_line = f"\nПоставщик: <b>{html.escape(item.supplier_name)}</b>" if getattr(item, "supplier_name", None) else ""
    diag_cause = getattr(item, "root_cause", None) or item.details
    diag_action = getattr(item, "recommended_action", None) or "Проверить статус платежа и подписки."
    retry_str = "Возможен (отказ подтверждён до мутации)" if getattr(item, "safe_retry_eligible", False) else "Требует предварительной проверки (во избежание двойной закупки)"
    diag_code = getattr(item, "diagnostic_code", None) or item.category.value

    text = (
        f"🔍 <b>Инспекция проблемного платежа</b>\n\n"
        f"Тип: <b>{'Платеж' if item.item_type == 'payment' else 'Пополнение баланса'} #{item.item_id}</b>\n"
        f"Сумма: <b>{item.amount_rub:.2f} ₽</b>"
        f"{supplier_line}\n"
        f"Создан: <code>{item.created_at.strftime('%Y-%m-%d %H:%M:%S')}</code> ({item.age_str} назад)\n\n"
        f"👤 <b>Клиент:</b>\n"
        f"  Telegram ID: <code>{tg}</code>\n"
        f"  Username: {uname}\n"
        f"  Имя: {html.escape(fname)}\n\n"
        f"💳 <b>Платёжный шлюз:</b>\n"
        f"  ID: <code>{item.provider_payment_id or '—'}</code>\n"
        f"  Статус в БД: <b>{item.local_status}</b>\n"
        f"  Статус в YooKassa: <b>{html.escape(yk_status_str)}</b>\n\n"
        f"⚙️ <b>Диагностика:</b>\n"
        f"  Причина: <b>{html.escape(diag_cause)}</b>\n"
        f"  Выдача: <b>{item.fulfillment_state}</b>\n"
        f"  Действие: <i>{html.escape(diag_action)}</i>\n\n"
        f"🛠 <b>Техническая информация:</b>\n"
        f"  Категория: <code>{item.category.value}</code>\n"
        f"  Код ошибки: <code>{html.escape(diag_code)}</code>\n"
        f"  Безопасный повтор: <b>{retry_str}</b>"
    )

    can_force = False
    if item.item_type == "payment":
        async with async_session() as session:
            p = await session.get(Payment, item.item_id)
            if p and p.telegram_delivery_error:
                err_str = p.telegram_delivery_error
                if err_str.startswith("retry_failed:"):
                    try:
                        parts = err_str.split(":", 3)
                        if len(parts) > 1 and parts[1].isdigit() and int(parts[1]) >= 3:
                            can_force = True
                    except (ValueError, IndexError):
                        pass
                elif err_str.startswith("delivery_in_progress:"):
                    try:
                        parts = err_str.split(":", 3)
                        claimed_at = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
                        attempts = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
                        now_epoch = int(datetime.utcnow().timestamp())
                        if (now_epoch - claimed_at >= 300) and attempts >= 3:
                            can_force = True
                    except (ValueError, IndexError):
                        pass

    kb = problem_payment_detail_kb(
        item_type=item.item_type,
        item_id=item.item_id,
        user_id=item.user_id,
        provider_payment_id=item.provider_payment_id,
        page=source,
        category=item.category,
        can_force_retry=can_force,
    )

    if hasattr(target, "message") and target.message:
        try:
            await target.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            await target.message.answer(text, reply_markup=kb, parse_mode="HTML")
        if hasattr(target, "answer"):
            await target.answer()
    elif hasattr(target, "edit_text"):
        try:
            await target.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            await target.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await target.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.in_({"adm_problem_payments", "adm_prob_main"}))
@router.callback_query(F.data.startswith("adm_prob_page:"))
async def admin_problem_payments_list(callback: CallbackQuery, state: FSMContext | None = None) -> None:
    """Problem payments dashboard - clean summary of last 7 days (DB-only, <1s)."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔️ Нет доступа", show_alert=True)
        return

    if state:
        await state.clear()

    from bot.keyboards.admin import problem_payments_main_kb
    from bot.services.problem_payments import ProblemCategory, get_fast_dashboard_summary

    summary = await get_fast_dashboard_summary(days=7)

    text_lines = [
        "⚠️ <b>Проблемные платежи</b>",
        "<i>Актуальные · последние 7 дней</i>\n",
        f"🚨 Оплачено, не выдано: <b>{summary.urgent_unfulfilled}</b>",
        f"📨 Ошибка доставки в TG: <b>{summary.tg_delivery_failed}</b>",
        f"⚙️ Ошибка выдачи Adapt/VHQ: <b>{summary.supplier_failed}</b>",
        f"🔎 Требует проверки: <b>{summary.needs_review}</b>",
    ]

    if summary.total_active == 0:
        text_lines.append("\n✅ <b>Новых проблемных платежей не обнаружено.</b>")
    else:
        text_lines.append("\n<b>Срочные записи:</b>")
        for item in summary.urgent_items:
            tg = item.telegram_id or "—"
            user_label = f"@{item.username}" if item.username else f"ID {tg}"
            cat_icon = {
                ProblemCategory.CONFIRMED_UNFULFILLED: "🚨",
                ProblemCategory.TELEGRAM_DELIVERY_FAILED: "📨",
                ProblemCategory.FAILED_SUPPLIER: "⚙️",
                ProblemCategory.DELIVERY_UNCERTAIN: "🔎",
            }.get(item.category, "⚠️")
            supp = f"[{item.supplier_name}] " if getattr(item, "supplier_name", None) else ""
            text_lines.append(
                f"• {cat_icon} <b>#{item.item_id}</b> · <b>{item.amount_rub:.0f} ₽</b> · {user_label} · {item.age_str} назад\n"
                f"  {supp}<i>{html.escape(item.fulfillment_state)}</i>"
            )

    msg_text = "\n".join(text_lines)
    kb = problem_payments_main_kb(summary)
    try:
        await callback.message.edit_text(msg_text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(msg_text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("adm_prob_cat:"))
async def admin_problem_payments_cat_view(callback: CallbackQuery, state: FSMContext | None = None) -> None:
    """Category-filtered view of problem payments (last 7 days, DB-only)."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔️ Нет доступа", show_alert=True)
        return

    if state:
        await state.clear()

    parts = callback.data.split(":")
    cat_name = parts[1] if len(parts) > 1 else "urgent"
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1

    from bot.keyboards.admin import problem_payments_list_kb
    from bot.services.problem_payments import ProblemCategory, ProblemScope, get_problem_payments_page

    cat_titles = {
        "urgent": "🚨 Оплачено, не выдано",
        "tg_delivery": "📨 Ошибка доставки в TG",
        "supplier": "⚙️ Сбои выдачи Adapt/VHQ",
        "review": "🔎 Требует проверки",
    }
    title = cat_titles.get(cat_name, "⚠️ Проблемные записи")

    items, total_count, total_pages = await get_problem_payments_page(
        category=cat_name,
        scope=ProblemScope.ACTIVE,
        page=page,
        per_page=6,
    )

    text_lines = [
        f"⚠️ <b>Проблемные платежи · {title}</b>",
        f"<i>Актуальные · последние 7 дней (стр. {page}/{total_pages}, всего: {total_count})</i>\n",
    ]
    if not items:
        text_lines.append("<i>В этой категории нет активных записей.</i>")
    else:
        for item in items:
            tg = item.telegram_id or "—"
            user_label = f"@{item.username}" if item.username else f"ID {tg}"
            cat_icon = {
                ProblemCategory.CONFIRMED_UNFULFILLED: "🚨",
                ProblemCategory.TELEGRAM_DELIVERY_FAILED: "📨",
                ProblemCategory.FAILED_SUPPLIER: "⚙️",
                ProblemCategory.DELIVERY_UNCERTAIN: "🔎",
            }.get(item.category, "⚠️")
            supp = f"[{item.supplier_name}] " if getattr(item, "supplier_name", None) else ""
            text_lines.append(
                f"• {cat_icon} <b>#{item.item_id}</b> · <b>{item.amount_rub:.0f} ₽</b> ({item.age_str} назад)\n"
                f"  Клиент: {user_label} | {supp}{item.fulfillment_state}"
            )

    msg_text = "\n".join(text_lines)
    kb = problem_payments_list_kb(items, category=cat_name, scope="active", page=page, total_pages=total_pages)
    try:
        await callback.message.edit_text(msg_text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(msg_text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("adm_prob_archive:"))
async def admin_problem_payments_archive_view(callback: CallbackQuery, state: FSMContext | None = None) -> None:
    """Archive view of problem payments older than 7 days (read-only, DB-only)."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔️ Нет доступа", show_alert=True)
        return

    if state:
        await state.clear()

    parts = callback.data.split(":")
    page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1

    from bot.keyboards.admin import problem_payments_list_kb
    from bot.services.problem_payments import ProblemCategory, ProblemScope, get_problem_payments_page

    items, total_count, total_pages = await get_problem_payments_page(
        scope=ProblemScope.ARCHIVE,
        page=page,
        per_page=6,
    )

    text_lines = [
        "🗂 <b>Архив проблемных платежей</b>",
        f"<i>Записи старше 7 дней (стр. {page}/{total_pages}, всего: {total_count})</i>\n",
        "<i>Исторические платежи перенесены в архив для сохранения чистоты текущей панели. "
        "Данные доступны для просмотра и поиска по ID.</i>\n",
    ]
    if not items:
        text_lines.append("<i>В архиве нет записей.</i>")
    else:
        for item in items:
            tg = item.telegram_id or "—"
            user_label = f"@{item.username}" if item.username else f"ID {tg}"
            cat_icon = {
                ProblemCategory.CONFIRMED_UNFULFILLED: "🚨",
                ProblemCategory.TELEGRAM_DELIVERY_FAILED: "📨",
                ProblemCategory.FAILED_SUPPLIER: "⚙️",
                ProblemCategory.DELIVERY_UNCERTAIN: "🔎",
                ProblemCategory.VERIFIED_MANUALLY_FULFILLED: "✅",
                ProblemCategory.MANUALLY_FULFILLED: "✅",
                ProblemCategory.PROVIDER_PENDING: "⏳",
            }.get(item.category, "⚠️")
            supp = f"[{item.supplier_name}] " if getattr(item, "supplier_name", None) else ""
            text_lines.append(
                f"• {cat_icon} <b>#{item.item_id}</b> · <b>{item.amount_rub:.0f} ₽</b> ({item.age_str} назад)\n"
                f"  Клиент: {user_label} | {supp}{item.fulfillment_state}"
            )

    msg_text = "\n".join(text_lines)
    kb = problem_payments_list_kb(items, scope="archive", page=page, total_pages=total_pages)
    try:
        await callback.message.edit_text(msg_text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(msg_text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "adm_prob_search")
async def admin_problem_payment_search_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Prompt admin to enter payment number or provider ID."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔️ Нет доступа", show_alert=True)
        return

    from bot.keyboards.admin import problem_payment_search_back_kb

    await state.set_state(AdminStates.waiting_problem_payment_search)
    prompt_text = (
        "🔍 <b>Поиск платежа</b>\n\n"
        "Введите номер платежа (например, <code>306</code>) или provider_payment_id из YooKassa:"
    )
    kb = problem_payment_search_back_kb()
    try:
        await callback.message.edit_text(prompt_text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(prompt_text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.message(AdminStates.waiting_problem_payment_search, F.text)
async def admin_problem_payment_search_process(message: Message, state: FSMContext) -> None:
    """Handle payment search input."""
    if not _is_admin(message.from_user.id):
        return

    text = message.text.strip()
    if text.startswith("/"):
        await state.clear()
        if text in ("/cancel", "/отмена"):
            await message.answer("Поиск отменен.", parse_mode="HTML")
            return
        if text.startswith(("/payment", "/pay")):
            parts = text.split(maxsplit=1)
            query = parts[1].strip() if len(parts) > 1 else ""
            if query:
                from bot.services.problem_payments import find_problem_payment_by_query
                item = await find_problem_payment_by_query(query)
                if item:
                    await _show_problem_payment_card(message, item, source="search")
                    return
                else:
                    await message.answer(f"❌ Платёж «{html.escape(query)}» не найден в базе данных.", parse_mode="HTML")
                    return
            else:
                await message.answer("Использование: <code>/payment 306</code>", parse_mode="HTML")
                return
        await message.answer("Поиск отменен. Выполните команду заново.", parse_mode="HTML")
        return

    query = text
    from bot.keyboards.admin import problem_payment_search_back_kb
    from bot.services.problem_payments import find_problem_payment_by_query

    item = await find_problem_payment_by_query(query)
    if not item:
        await message.answer(
            f"❌ Платёж «{html.escape(query)}» не найден в базе данных.\n"
            f"Проверьте номер или ID и попробуйте снова:",
            reply_markup=problem_payment_search_back_kb(),
            parse_mode="HTML",
        )
        return

    await state.clear()
    await _show_problem_payment_card(message, item, source="search")


@router.message(Command("payment", "pay"))
async def admin_payment_command(message: Message, command: CommandObject, state: FSMContext | None = None) -> None:
    """Direct command /payment <id> or /pay <id>."""
    if not _is_admin(message.from_user.id):
        return

    if state:
        await state.clear()

    query = (command.args or "").strip()
    if not query:
        await message.answer(
            "Использование: <code>/payment 306</code> или <code>/payment &lt;uuid&gt;</code>",
            parse_mode="HTML",
        )
        return

    from bot.services.problem_payments import find_problem_payment_by_query

    item = await find_problem_payment_by_query(query)
    if not item:
        await message.answer(
            f"❌ Платёж «{html.escape(query)}» не найден в базе данных.",
            parse_mode="HTML",
        )
        return

    await _show_problem_payment_card(message, item, source="search")


@router.callback_query(F.data.startswith("adm_prob_inspect:"))
async def admin_problem_payment_inspect(callback: CallbackQuery) -> None:
    """Inspect detailed diagnostics card of a single problem payment (fast, DB-only)."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔️ Нет доступа", show_alert=True)
        return

    parts = callback.data.split(":")
    code = parts[1]
    item_id = int(parts[2])
    source = parts[3] if len(parts) > 3 else "main"

    from bot.services.problem_payments import get_problem_payment_by_id

    item = await get_problem_payment_by_id(item_id, "payment" if code == "p" else "topup")
    if not item:
        await callback.answer("Запись не найдена в базе данных", show_alert=True)
        return

    await _show_problem_payment_card(callback, item, source=source)


@router.callback_query(F.data.startswith("adm_prob_refresh:"))
async def admin_problem_payment_refresh(callback: CallbackQuery) -> None:
    """Explicit on-demand live status verification from YooKassa API."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔️ Нет доступа", show_alert=True)
        return

    parts = callback.data.split(":")
    code = parts[1]
    item_id = int(parts[2])
    source = parts[3] if len(parts) > 3 else "main"

    await callback.answer("⏳ Запрос в YooKassa API...", show_alert=False)
    from bot.services.problem_payments import verify_single_payment_live

    item, msg = await verify_single_payment_live("payment" if code == "p" else "topup", item_id, force=True)
    if not item:
        await callback.answer(f"⚠️ {msg}", show_alert=True)
        return

    await _show_problem_payment_card(callback, item, source=source)
    await callback.message.answer(f"ℹ️ <b>Результат проверки:</b> {html.escape(msg)}", parse_mode="HTML")


@router.callback_query(F.data.startswith("adm_prob_reconcile:"))
async def admin_problem_payment_reconcile(callback: CallbackQuery) -> None:
    """Prompt for explicit confirmation before triggering potentially chargeable reconciliation."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔️ Нет доступа", show_alert=True)
        return

    parts = callback.data.split(":")
    code = parts[1]
    item_id = int(parts[2])
    page = parts[3] if len(parts) > 3 else "main"

    provider_id = None
    amount_str = "—"
    client_tg = "—"
    async with async_session() as session:
        if code == "p":
            p = await session.get(Payment, item_id)
            if p:
                provider_id = p.provider_payment_id
                amount_str = f"{(p.amount / 100.0):.2f}"
                u = await session.get(User, p.user_id) if p.user_id else None
                client_tg = str(u.telegram_id if u else p.telegram_chat_id or "—")
        else:
            t = await session.get(BalanceTopUp, item_id)
            if t:
                provider_id = t.provider_payment_id
                amount_str = f"{t.amount_rub:.2f}"
                client_tg = str(t.telegram_id)

    if not provider_id:
        await callback.answer("У записи нет provider_payment_id", show_alert=True)
        return

    from bot.services.problem_payments import classify_and_verify_candidate, ProblemCategory
    cand = {"type": "payment" if code == "p" else "topup", "obj": p if code == "p" else t, "user": u if code == "p" else None}
    diag = await classify_and_verify_candidate(cand)
    if diag and diag.category in (
        ProblemCategory.VERIFIED_MANUALLY_FULFILLED,
        ProblemCategory.MANUALLY_FULFILLED,
        ProblemCategory.DELIVERY_UNCERTAIN,
        ProblemCategory.TELEGRAM_DELIVERY_FAILED,
        ProblemCategory.PROVIDER_PENDING,
    ):
        if diag.category in (ProblemCategory.VERIFIED_MANUALLY_FULFILLED, ProblemCategory.MANUALLY_FULFILLED):
            await callback.answer(
                "⛔️ Услуга по этому платежу уже была выдана клиенту вручную! Повторная сверка заблокирована во избежание дублирования.",
                show_alert=True,
            )
        elif diag.category == ProblemCategory.DELIVERY_UNCERTAIN:
            await callback.answer(
                "⛔️ Статус платежа неясен (недостаточно данных). Автоматическая выдача заблокирована. Требуется ручная проверка администратором.",
                show_alert=True,
            )
        elif diag.category == ProblemCategory.TELEGRAM_DELIVERY_FAILED:
            await callback.answer(
                "ℹ️ Подписка уже создана! Для отправки сообщения клиенту воспользуйтесь кнопкой «Отправить доступ клиенту в TG».",
                show_alert=True,
            )
        else:
            await callback.answer(
                "⛔️ Платеж ещё не оплачен клиентом в YooKassa. Сверка невозможна.",
                show_alert=True,
            )
        return

    from bot.keyboards.admin import problem_payment_reconcile_confirm_kb
    confirm_text = (
        f"⚠️ <b>Подтверждение принудительной сверки</b>\n\n"
        f"Тип: <b>{'Платеж' if code == 'p' else 'Пополнение баланса'} #{item_id}</b>\n"
        f"Сумма: <b>{amount_str} ₽</b>\n"
        f"Клиент: <code>{client_tg}</code>\n"
        f"ID в YooKassa: <code>{provider_id}</code>\n\n"
        f"<i>Внимание: сверка выполнит обращение к YooKassa и может вызвать повторную выдачу услуги у поставщика. Продолжить?</i>"
    )
    kb = problem_payment_reconcile_confirm_kb(code, item_id, page=page)
    try:
        await callback.message.edit_text(confirm_text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(confirm_text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("adm_prob_rec_confirm:"))
async def admin_problem_payment_reconcile_confirm(callback: CallbackQuery) -> None:
    """Execute confirmed reconciliation, log admin audit, and show result."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔️ Нет доступа", show_alert=True)
        return

    parts = callback.data.split(":")
    code = parts[1]
    item_id = int(parts[2])
    page = parts[3] if len(parts) > 3 else "main"

    provider_id = None
    target_tg = None
    async with async_session() as session:
        if code == "p":
            p = await session.get(Payment, item_id)
            if p:
                provider_id = p.provider_payment_id
                u = await session.get(User, p.user_id) if p.user_id else None
                target_tg = u.telegram_id if u else p.telegram_chat_id
        else:
            t = await session.get(BalanceTopUp, item_id)
            if t:
                provider_id = t.provider_payment_id
                target_tg = t.telegram_id

    if not provider_id:
        await callback.answer("У записи нет provider_payment_id", show_alert=True)
        return

    from bot.services.problem_payments import classify_and_verify_candidate, ProblemCategory
    cand = {"type": "payment" if code == "p" else "topup", "obj": p if code == "p" else t, "user": u if code == "p" else None}
    diag = await classify_and_verify_candidate(cand)
    if diag and diag.category in (
        ProblemCategory.VERIFIED_MANUALLY_FULFILLED,
        ProblemCategory.MANUALLY_FULFILLED,
        ProblemCategory.DELIVERY_UNCERTAIN,
        ProblemCategory.TELEGRAM_DELIVERY_FAILED,
        ProblemCategory.PROVIDER_PENDING,
    ):
        if diag.category in (ProblemCategory.VERIFIED_MANUALLY_FULFILLED, ProblemCategory.MANUALLY_FULFILLED):
            await callback.answer(
                "⛔️ Услуга уже была выдана клиенту вручную! Повторная выдача отменена.",
                show_alert=True,
            )
        elif diag.category == ProblemCategory.DELIVERY_UNCERTAIN:
            await callback.answer(
                "⛔️ Статус платежа неясен (недостаточно данных). Сверка заблокирована.",
                show_alert=True,
            )
        elif diag.category == ProblemCategory.TELEGRAM_DELIVERY_FAILED:
            await callback.answer(
                "ℹ️ Подписка уже создана! Для отправки сообщения клиенту воспользуйтесь кнопкой «Отправить доступ клиенту в TG».",
                show_alert=True,
            )
        else:
            await callback.answer(
                "⛔️ Платеж ещё не оплачен клиентом в YooKassa. Сверка невозможна.",
                show_alert=True,
            )
        return

    await callback.answer("⏳ Запуск сверки...", show_alert=False)
    from bot.services.yookassa_reconciliation import verify_and_fulfill_yookassa_payment
    success, res_code = await verify_and_fulfill_yookassa_payment(
        callback.bot,
        provider_id,
        source="admin_dashboard",
    )

    await write_admin_audit(
        actor_telegram_id=callback.from_user.id,
        action="problem_payment_reconcile",
        summary=f"Ручная сверка {code}#{item_id} ({provider_id})",
        entity_type="payment" if code == "p" else "balance_topup",
        entity_id=str(item_id),
        target_telegram_id=target_tg,
        status="success" if success else "failed",
        details=f"result={res_code}",
    )

    from bot.services.problem_payments import format_reconciliation_result
    summary_text, action_text = format_reconciliation_result(res_code)

    if success:
        await callback.message.answer(
            f"✅ <b>Сверка успешна!</b>\n\n"
            f"<b>Результат:</b> {html.escape(summary_text)}\n"
            f"ID в YooKassa: <code>{provider_id}</code>\n"
            f"<i>Диагностический код: <code>{res_code}</code></i>",
            parse_mode="HTML",
        )
    else:
        await callback.message.answer(
            f"⚠️ <b>Результат сверки:</b> {html.escape(summary_text)}\n\n"
            f"<b>Рекомендуемое действие:</b> {html.escape(action_text)}\n"
            f"ID в YooKassa: <code>{provider_id}</code>\n"
            f"<i>Диагностический код: <code>{res_code}</code></i>",
            parse_mode="HTML",
        )

    synthetic = callback.model_copy(update={"data": f"adm_prob_inspect:{code}:{item_id}:{page}"}) if hasattr(callback, "model_copy") else callback
    await admin_problem_payment_inspect(synthetic)


@router.callback_query(F.data.startswith("adm_prob_retry_tg:"))
async def admin_problem_payment_retry_tg(callback: CallbackQuery) -> None:
    """Safely retry delivering existing subscription info to customer in Telegram without supplier re-mutation."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔️ Нет доступа", show_alert=True)
        return

    parts = callback.data.split(":")
    code = parts[1]
    item_id = int(parts[2])
    page = parts[3] if len(parts) > 3 and parts[3] != "force" else "main"
    is_force = (len(parts) > 4 and parts[4] == "force") or (len(parts) > 3 and parts[3] == "force")

    if code != "p":
        await callback.answer("Повторная отправка доступна только для платежей за подписку", show_alert=True)
        return

    async with async_session() as session:
        payment = await session.get(Payment, item_id)
        if not payment:
            await callback.answer("Платеж не найден", show_alert=True)
            return

        if payment.status != PaymentStatus.COMPLETED:
            await callback.answer("⛔️ Платеж еще не завершен (статус не COMPLETED). Повторная отправка невозможна.", show_alert=True)
            return

        if not payment.telegram_delivery_error:
            await callback.answer("✅ Доступ уже успешно доставлен клиенту в Telegram", show_alert=True)
            return

        if not payment.subscription_id:
            await callback.answer("⛔️ У платежа нет привязанной подписки", show_alert=True)
            return

        sub = await session.get(Subscription, payment.subscription_id)
        if not sub:
            await callback.answer("Подписка не найдена в базе данных", show_alert=True)
            return

        if sub.user_id != payment.user_id:
            logger.error(
                "Admin retry delivery security abort: sub %s user_id (%s) != payment user_id (%s)",
                sub.id, sub.user_id, payment.user_id,
            )
            await callback.answer("⛔️ Ошибка безопасности: подписка принадлежит другому пользователю!", show_alert=True)
            return

        user = await session.get(User, payment.user_id)
        if not user or not user.telegram_id:
            await callback.answer("Пользователь или Telegram ID не найден", show_alert=True)
            return

        prior_error = payment.telegram_delivery_error
        is_in_progress = prior_error.startswith("delivery_in_progress")
        is_retry_failed = prior_error.startswith("retry_failed")
        now_epoch = int(datetime.utcnow().timestamp())
        prior_attempts = 0
        orig_kind = "general"
        is_expired_recovery = False

        if is_in_progress:
            # format: delivery_in_progress:<epoch>:<attempts>:<orig_kind>
            prog_parts = prior_error.split(":", 3)
            claimed_at = now_epoch
            try:
                if len(prog_parts) > 1 and prog_parts[1].isdigit():
                    claimed_at = int(prog_parts[1])
                if len(prog_parts) > 2 and prog_parts[2].isdigit():
                    prior_attempts = int(prog_parts[2])
                if len(prog_parts) > 3:
                    orig_kind = prog_parts[3]
            except (ValueError, IndexError):
                pass

            # Requirement 1: Force retry must NEVER bypass an active, non-expired delivery claim.
            if now_epoch - claimed_at < 300:
                await callback.answer("⏳ Отправка уже выполняется другим администратором или задачей", show_alert=True)
                return

            # Claim has expired (> 300s): handle as uncertain delivery outcome with bounded recovery
            is_expired_recovery = True
        elif is_retry_failed:
            # format: retry_failed:<attempts>:<orig_kind>:<err>
            fail_parts = prior_error.split(":", 3)
            try:
                if len(fail_parts) > 1 and fail_parts[1].isdigit():
                    prior_attempts = int(fail_parts[1])
                if len(fail_parts) > 2:
                    orig_kind = fail_parts[2]
            except (ValueError, IndexError):
                pass
        else:
            orig_kind = "proxy" if prior_error.startswith("proxy_link_failed") else "general"

        if is_force:
            # Requirement 2: Allow force reset only for an exhausted retry state
            if prior_attempts < 3:
                await callback.answer(
                    "⛔️ Принудительный сброс доступен только при исчерпании лимита попыток (3). Используйте стандартную отправку.",
                    show_alert=True,
                )
                return
            next_attempt = 1
        else:
            next_attempt = prior_attempts + 1
            if next_attempt > 3:
                logger.warning("Payment %s exceeded retry attempts (%s)", payment.id, prior_attempts)
                await callback.answer(
                    "⛔️ Превышен лимит попыток отправки (3). Проверьте логи сервера или используйте принудительный сброс.",
                    show_alert=True,
                )
                synthetic = callback.model_copy(update={"data": f"adm_prob_inspect:p:{payment.id}:{page}"}) if hasattr(callback, "model_copy") else callback
                await admin_problem_payment_inspect(synthetic)
                return

        # Atomic Compare-And-Set (CAS) conditional claim
        in_progress_marker = f"delivery_in_progress:{now_epoch}:{next_attempt}:{orig_kind}"
        stmt = (
            update(Payment)
            .where(
                Payment.id == payment.id,
                Payment.telegram_delivery_error == prior_error,
            )
            .values(telegram_delivery_error=in_progress_marker)
        )
        cas_res = await session.execute(stmt)
        await session.commit()

        if cas_res.rowcount == 0:
            # Another concurrent execution claimed or resolved the payment first!
            await callback.answer("⏳ Отправка уже выполняется другим администратором", show_alert=True)
            return

        tariff = await session.get(Tariff, payment.tariff_id) if payment.tariff_id else None
        proxy_link = None
        proxy_acc = await session.scalar(
            select(ProxyAccount).where(ProxyAccount.subscription_id == sub.id)
        )
        if proxy_acc:
            proxy_link = proxy_acc.get_proxy_link()

    # Requirements 3 & 4: Audit force-reset or expired recovery ONLY AFTER atomic claim succeeded
    if is_force:
        await write_admin_audit(
            actor_telegram_id=callback.from_user.id,
            action="force_reset_telegram_retry",
            summary=f"Принудительный сброс лимита попыток отправки TG (платеж #{payment.id})",
            entity_type="payment",
            entity_id=str(payment.id),
            target_telegram_id=user.telegram_id,
            status="success",
            details=f"prior_attempts={prior_attempts} prior_error={prior_error[:100]}",
        )
    elif is_expired_recovery:
        await write_admin_audit(
            actor_telegram_id=callback.from_user.id,
            action="recover_expired_delivery_claim",
            summary=f"Восстановление зависшей отправки в TG (платеж #{payment.id}, попытка {next_attempt})",
            entity_type="payment",
            entity_id=str(payment.id),
            target_telegram_id=user.telegram_id,
            status="success",
            details=f"recovered_from={prior_error[:100]} prior_attempts={prior_attempts} next_attempt={next_attempt}",
        )

    from bot.webhooks import send_subscription_delivery_messages, _platform_from_str, _is_deferred_platform
    delivery_plat = _platform_from_str(payment.platform or "")
    needs_choice = bool(
        payment.platform
        and _is_deferred_platform(payment.platform)
        and tariff
        and tariff.tariff_type != TariffType.TG_PROXY
    )
    expires_str = sub.expires_at.strftime("%d.%m.%Y") if sub.expires_at else "N/A"

    # Distinguish failed primary key delivery from auxiliary failures (e.g. proxy link)
    is_proxy_only_failure = (orig_kind == "proxy")
    resend_primary = not is_proxy_only_failure
    resend_secondary = True

    try:
        success, err = await send_subscription_delivery_messages(
            bot=callback.bot,
            chat_id=payment.telegram_chat_id or user.telegram_id,
            telegram_user_id=user.telegram_id,
            subscription_id=sub.id,
            delivery_platform=delivery_plat,
            vpn_key_str=sub.vpn_key,
            proxy_link=proxy_link,
            expires_str=expires_str,
            needs_platform_choice=needs_choice,
            payment_id=payment.id,
            resend_primary=resend_primary,
            resend_secondary=resend_secondary,
            record_db_error=False,
        )
    except Exception as exc:
        success = False
        err = f"send_failed: {exc}"

    # Requirement 5: Final success/failure updates must conditionally match the exact claim marker
    was_applied = False
    if success:
        async with async_session() as session:
            stmt = (
                update(Payment)
                .where(
                    Payment.id == payment.id,
                    Payment.telegram_delivery_error == in_progress_marker,
                )
                .values(telegram_delivery_error=None)
            )
            res = await session.execute(stmt)
            await session.commit()
            was_applied = (res.rowcount > 0)
    else:
        async with async_session() as session:
            err_clean = (err or "send_failed").replace(":", " ")[:200]
            err_msg = f"retry_failed:{next_attempt}:{orig_kind}:{err_clean}"[:255]
            stmt = (
                update(Payment)
                .where(
                    Payment.id == payment.id,
                    Payment.telegram_delivery_error == in_progress_marker,
                )
                .values(telegram_delivery_error=err_msg)
            )
            res = await session.execute(stmt)
            await session.commit()
            was_applied = (res.rowcount > 0)

    if not was_applied:
        logger.warning(
            "Stale delivery completion ignored for payment %s; marker %s was already superseded",
            payment.id, in_progress_marker,
        )

    audit_status = "success" if (success and was_applied) else "superseded" if not was_applied else "failed"
    audit_details = (
        f"attempt={next_attempt} mode={'proxy_only' if is_proxy_only_failure else 'full'} "
        f"{'delivered' if success else f'err={err}'} was_applied={was_applied}"
    )
    await write_admin_audit(
        actor_telegram_id=callback.from_user.id,
        action="retry_telegram_delivery",
        summary=f"Повторная отправка доступа клиенту в TG (платеж #{payment.id}, подписка #{sub.id})",
        entity_type="payment",
        entity_id=str(payment.id),
        target_telegram_id=user.telegram_id,
        status=audit_status,
        details=audit_details,
    )

    if not was_applied:
        await callback.answer("⚠️ Отправка завершена, но статус не обновлен: задача была вытеснена более новой попыткой", show_alert=True)
    elif success:
        await callback.answer("✅ Доступ успешно отправлен клиенту в Telegram!", show_alert=True)
    else:
        await callback.answer(f"❌ Ошибка отправки (попытка {next_attempt}/3): {err}", show_alert=True)

    synthetic = callback.model_copy(update={"data": f"adm_prob_inspect:p:{payment.id}:{page}"}) if hasattr(callback, "model_copy") else callback
    await admin_problem_payment_inspect(synthetic)


@router.callback_query(F.data.startswith("adm_prob_user:"))
async def admin_problem_payment_user(callback: CallbackQuery) -> None:
    """Jump to comprehensive customer card."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔️ Нет доступа", show_alert=True)
        return

    user_id = int(callback.data.split(":")[1])
    async with async_session() as session:
        user = await session.get(User, user_id)

    if not user:
        await callback.answer("Пользователь не найден", show_alert=True)
        return

    await _show_user_info(callback.message, user)
    await callback.answer()


@router.callback_query(F.data == "admin_panel")
async def admin_panel_btn(callback: CallbackQuery, state: FSMContext) -> None:
    """Admin panel button from main menu."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔️ Нет доступа", show_alert=True)
        return
    await state.clear()
    try:
        await callback.message.edit_text(ADMIN_PANEL, reply_markup=admin_menu_kb(), parse_mode="HTML")
    except Exception:
        await callback.message.answer(ADMIN_PANEL, reply_markup=admin_menu_kb(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.in_({"adm_back", "admin"}))
async def admin_back(callback: CallbackQuery, state: FSMContext) -> None:
    """Return to admin menu."""
    if not _is_admin(callback.from_user.id):
        return
    await state.clear()
    try:
        await callback.message.edit_text(
            ADMIN_PANEL,
            reply_markup=admin_menu_kb(),
            parse_mode="HTML",
        )
    except Exception:
        await callback.message.answer(
            ADMIN_PANEL,
            reply_markup=admin_menu_kb(),
            parse_mode="HTML",
        )
    await callback.answer()


# ── Ad Links ──────────────────────────────────────────

@router.callback_query(F.data == "adm_ads")
async def admin_ad_links(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await state.clear()
    await _render_ad_links(callback.message, callback.bot, edit=True)
    await callback.answer()


@router.callback_query(F.data == "adm_ads_new")
async def admin_ad_link_new(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await state.clear()
    await callback.message.edit_text(
        "📣 <b>Новая рекламная ссылка</b>\n\nВыберите тип трафика:",
        reply_markup=ad_link_kind_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm_ads_kind_"))
async def admin_ad_link_kind(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    kind = callback.data.removeprefix("adm_ads_kind_")
    await state.update_data(ad_link_kind=kind)
    await state.set_state(AdminStates.waiting_ad_link_title)
    await callback.message.edit_text(
        "📣 <b>Новая рекламная ссылка</b>\n\n"
        "Отправьте название ссылки.\n"
        "Пример: <code>Войнарев канал 1</code> или <code>Crypto test A</code>",
        reply_markup=admin_back_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminStates.waiting_ad_link_title, F.text)
async def admin_ad_link_save(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    title = (message.text or "").strip()
    if not title:
        await message.answer("❌ Введите название ссылки.", reply_markup=admin_back_kb(), parse_mode="HTML")
        return

    data = await state.get_data()
    kind = str(data.get("ad_link_kind") or "custom").strip().lower()
    base_code = _build_ad_link_code(kind, title)

    async with async_session() as session:
        code = base_code
        suffix = 1
        while await session.scalar(select(AdTrackingLink.id).where(AdTrackingLink.code == code)):
            extra = uuid.uuid4().hex[:4] if suffix > 9 else str(suffix)
            code = f"{base_code[:59]}_{extra}"[:64]
            suffix += 1

        link = AdTrackingLink(
            title=title[:128],
            code=code,
            source_kind=kind[:32],
            created_by=message.from_user.id,
        )
        session.add(link)
        await session.commit()
        await session.refresh(link)
        link_id = link.id

    await state.clear()
    await _show_ad_link_detail(message, message.bot, link_id, edit=False)


@router.callback_query(F.data.regexp(r"^adm_ads_link_(\d+)$"))
async def admin_ad_link_detail(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    link_id = int(callback.data.rsplit("_", 1)[-1])
    await _show_ad_link_detail(callback.message, callback.bot, link_id, edit=True)
    await callback.answer()


@router.callback_query(F.data.regexp(r"^adm_ads_toggle_(\d+)$"))
async def admin_ad_link_toggle(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    link_id = int(callback.data.rsplit("_", 1)[-1])
    async with async_session() as session:
        link = await session.get(AdTrackingLink, link_id)
        if not link:
            await callback.answer("Ссылка не найдена", show_alert=True)
            return
        link.is_active = not link.is_active
        await session.commit()
    await _show_ad_link_detail(callback.message, callback.bot, link_id, edit=True)
    await callback.answer("Статус обновлён")


async def _fetch_webstore_stats_dict() -> dict[str, Any] | None:
    if not getattr(settings, "webstore_public_enabled", False) or not getattr(settings, "webstore_api_base_url", None):
        return None
    url = f"{settings.webstore_api_base_url.rstrip('/')}/api/store/internal/admin-stats"
    headers = {"X-Internal-Secret": settings.webstore_bridge_secret}
    try:
        import aiohttp as _aiohttp
        async with _aiohttp.ClientSession(timeout=_aiohttp.ClientTimeout(total=4)) as sess:
            async with sess.get(url, headers=headers) as resp:
                if resp.status == 200:
                    return await resp.json()
    except Exception as exc:
        logger.debug("Failed to fetch webstore stats: %s", exc)
    return None


@router.callback_query(F.data.in_({"adm_stats", "adm_stats_overview"}))
async def admin_stats_overview(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return

    now = datetime.utcnow()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = today - timedelta(days=7)
    month_ago = today - timedelta(days=30)

    web_data = await _fetch_webstore_stats_dict()

    async with async_session() as session:
        total_users = await session.scalar(select(func.count(User.id))) or 0
        new_today = await session.scalar(
            select(func.count(User.id)).where(User.created_at >= today)
        ) or 0
        paying_users = await session.scalar(
            select(func.count(func.distinct(Subscription.user_id))).where(
                Subscription.status == SubStatus.ACTIVE,
                Subscription.billing_mode != "demo",
            )
        ) or 0
        active_subs_res = await session.execute(
            select(Subscription).where(Subscription.status == SubStatus.ACTIVE)
        )
        active_list = active_subs_res.scalars().all()
        active_subs = len(active_list)

        expired_subs = await session.scalar(
            select(func.count(Subscription.id)).where(Subscription.status == SubStatus.EXPIRED)
        ) or 0
        total_payments = await session.scalar(
            select(func.count(Payment.id)).where(Payment.status == PaymentStatus.COMPLETED)
        ) or 0

        # Group and count active subscriptions
        marzban_paid = sum(1 for s in active_list if not is_adapt_subscription(s) and not is_vhq_subscription(s) and s.billing_mode != "demo")
        marzban_demo = sum(1 for s in active_list if not is_adapt_subscription(s) and not is_vhq_subscription(s) and s.billing_mode == "demo")
        adapt_paid = sum(1 for s in active_list if is_adapt_subscription(s) and s.billing_mode != "demo")
        adapt_demo = sum(1 for s in active_list if is_adapt_subscription(s) and s.billing_mode == "demo")
        vhq_paid = sum(1 for s in active_list if is_vhq_subscription(s) and s.billing_mode != "demo")
        vhq_demo = sum(1 for s in active_list if is_vhq_subscription(s) and s.billing_mode == "demo")

        details_lines = []
        if (adapt_paid + adapt_demo) > 0:
            demo_suffix = f" (из них демо: <b>{adapt_demo}</b>)" if adapt_demo > 0 else ""
            details_lines.append(f" ├ Adapt: <b>{adapt_paid + adapt_demo}</b>{demo_suffix}")
        if (vhq_paid + vhq_demo) > 0:
            demo_suffix = f" (из них демо: <b>{vhq_demo}</b>)" if vhq_demo > 0 else ""
            details_lines.append(f" ├ VHQ: <b>{vhq_paid + vhq_demo}</b>{demo_suffix}")
        if (marzban_paid + marzban_demo) > 0:
            demo_suffix = f" (из них демо: <b>{marzban_demo}</b>)" if marzban_demo > 0 else ""
            details_lines.append(f" ├ Marzban: <b>{marzban_paid + marzban_demo}</b>{demo_suffix}")

        if not details_lines:
            details_lines.append(" ├ Подписок пока нет")

        details = "\n".join(details_lines) + "\n"

        # Demo conversion in bot
        paid_user_ids_q = select(Payment.user_id).where(Payment.status == PaymentStatus.COMPLETED).distinct()
        bot_demo_total = await session.scalar(select(func.count(func.distinct(Subscription.user_id))).where(Subscription.billing_mode == "demo")) or 0
        bot_demo_paid = await session.scalar(select(func.count(func.distinct(Subscription.user_id))).where(Subscription.billing_mode == "demo", Subscription.user_id.in_(paid_user_ids_q))) or 0
        bot_conv = round((bot_demo_paid / bot_demo_total * 100), 1) if bot_demo_total > 0 else 0.0

        # Churn in bot
        ever_paid = await session.scalar(
            select(func.count(func.distinct(Payment.user_id))).where(
                Payment.status == PaymentStatus.COMPLETED,
            )
        ) or 0
        still_active = await session.scalar(
            select(func.count(func.distinct(Subscription.user_id))).where(
                Subscription.billing_mode != "demo",
                Subscription.status == SubStatus.ACTIVE,
                Subscription.user_id.in_(paid_user_ids_q),
            )
        ) or 0
        churned = ever_paid - still_active
        churn_rate = (churned / ever_paid * 100) if ever_paid > 0 else 0.0

        # Bot revenue RUB
        def _rub_sum(since=None):
            q = select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.status == PaymentStatus.COMPLETED,
                Payment.currency == "RUB",
            )
            if since is not None:
                q = q.where(Payment.created_at >= since)
            return q

        bot_rub_today = (await session.scalar(_rub_sum(today))) / 100
        bot_rub_week = (await session.scalar(_rub_sum(week_ago))) / 100
        bot_rub_month = (await session.scalar(_rub_sum(month_ago))) / 100
        bot_rub_total = (await session.scalar(_rub_sum())) / 100

        stars_total = await session.scalar(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.status == PaymentStatus.COMPLETED,
                Payment.currency == "XTR",
            )
        ) or 0

    if web_data:
        web_rev = web_data.get("revenue", {})
        web_sc = web_data.get("status_counts", {})
        web_profiles = int(web_data.get("total_profiles", 0) or 0)
        web_paid_count = int(web_sc.get("delivered", 0) or 0)

        web_active_dict = web_data.get("active", {})
        web_active_paid = int(web_active_dict.get("paid", 0) or 0)
        web_active_demo = int(web_active_dict.get("demo", 0) or 0)
        web_active_total = web_active_paid + web_active_demo
        web_expired_subs = int(web_active_dict.get("expired", 0) or 0)

        web_today = float(web_rev.get("today", 0) or 0)
        web_w7 = float(web_rev.get("w7", 0) or 0)
        web_w30 = float(web_rev.get("w30", 0) or 0)
        web_all = float(web_rev.get("all", 0) or 0)

        # Web conversion & demo
        web_conv_data = web_data.get("conversion", {})
        web_demo_total = int(web_conv_data.get("demo_total", 0) or 0)
        web_demo_converted = int(web_conv_data.get("demo_converted", 0) or 0)
        web_demo_conv = round((web_demo_converted / web_demo_total * 100), 1) if web_demo_total > 0 else 0.0

        # Web churn
        web_churn_data = web_data.get("churn", {})
        web_ever_paid = int(web_churn_data.get("ever_paid", 0) or 0)
        web_still_active = int(web_churn_data.get("still_active", 0) or 0)
        web_churned = int(web_churn_data.get("churned", 0) or 0)
        web_churn_rate = round((web_churned / web_ever_paid * 100), 1) if web_ever_paid > 0 else 0.0

        # Combinations
        total_clients = total_users + web_profiles
        total_active_all = active_subs + web_active_total
        total_expired_all = expired_subs + web_expired_subs
        total_payments_count = total_payments + web_paid_count

        comb_today = bot_rub_today + web_today
        comb_7d = bot_rub_week + web_w7
        comb_30d = bot_rub_month + web_w30
        comb_all = bot_rub_total + web_all

        comb_demo_total = bot_demo_total + web_demo_total
        comb_demo_paid = bot_demo_paid + web_demo_converted
        comb_demo_conv = round((comb_demo_paid / comb_demo_total * 100), 1) if comb_demo_total > 0 else 0.0

        comb_ever_paid = ever_paid + web_ever_paid
        comb_still_active = still_active + web_still_active
        comb_churned = churned + web_churned
        comb_churn_rate = round((comb_churned / comb_ever_paid * 100), 1) if comb_ever_paid > 0 else 0.0

        stars_text = f" + {stars_total}⭐" if stars_total > 0 else ""

        def _fmt_rev_line(comb_val, bot_val, web_val):
            if web_val > 0 and bot_val > 0:
                return f"<b>{comb_val:,.0f} ₽</b> (ТГ: {bot_val:,.0f} ₽ · Сайт: {web_val:,.0f} ₽)"
            elif web_val > 0:
                return f"<b>{comb_val:,.0f} ₽</b> (Сайт: {web_val:,.0f} ₽)"
            elif bot_val > 0:
                return f"<b>{comb_val:,.0f} ₽</b> (ТГ: {bot_val:,.0f} ₽)"
            return "<b>0 ₽</b>"

        # Detailed breakdown of active accesses
        details_lines = [
            f" ├ 📱 В Telegram: <b>{active_subs}</b> (платных: <b>{marzban_paid + adapt_paid + vhq_paid}</b> · демо: <b>{marzban_demo + adapt_demo + vhq_demo}</b>)",
            f" ├ 🌐 На Сайте: <b>{web_active_total}</b> (платных: <b>{web_active_paid}</b> · демо: <b>{web_active_demo}</b>)",
        ]
        proto_parts = []
        if (adapt_paid + adapt_demo) > 0:
            proto_parts.append(f"Adapt: <b>{adapt_paid + adapt_demo}</b>")
        if (vhq_paid + vhq_demo) > 0:
            proto_parts.append(f"VHQ: <b>{vhq_paid + vhq_demo}</b>")
        if (marzban_paid + marzban_demo) > 0:
            proto_parts.append(f"Marzban: <b>{marzban_paid + marzban_demo}</b>")
        if proto_parts:
            details_lines.append(f" └ В боте по протоколам: {' · '.join(proto_parts)}")
        details = "\n".join(details_lines) + "\n"

        conversion_block = (
            f"\n\n📈 <b>Воронка и конверсия:</b>\n"
            f" ├ 🎁 <b>Демо → Покупка: {comb_demo_conv}%</b> ({comb_demo_paid} из {comb_demo_total})\n"
            f" │   ├ В боте: <b>{bot_conv}%</b> ({bot_demo_paid} из {bot_demo_total})\n"
            f" │   └ На сайте: <b>{web_demo_conv}%</b> ({web_demo_converted} из {web_demo_total})\n"
            f" └ 📉 <b>Отток клиентов: {comb_churn_rate:.1f}%</b> ({comb_churned} не продлили из {comb_ever_paid})\n"
            f"     ├ В боте: <b>{churn_rate:.1f}%</b> ({churned} из {ever_paid})\n"
            f"     └ На сайте: <b>{web_churn_rate:.1f}%</b> ({web_churned} из {web_ever_paid})"
        )

        msg_text = ADMIN_STATS_UNIFIED.format(
            total_clients=total_clients,
            total_users=total_users,
            new_today=new_today,
            paying_users=paying_users,
            web_profiles=web_profiles,
            active_subs=total_active_all,
            bot_active_subs=active_subs,
            web_active_subs=web_active_total,
            details=details,
            total_payments=total_payments_count,
            bot_payments=total_payments,
            web_payments=web_paid_count,
            rev_today=_fmt_rev_line(comb_today, bot_rub_today, web_today),
            rev_7d=_fmt_rev_line(comb_7d, bot_rub_week, web_w7),
            rev_30d=_fmt_rev_line(comb_30d, bot_rub_month, web_w30),
            rev_all=_fmt_rev_line(comb_all, bot_rub_total, web_all),
            stars_text=stars_text,
            conversion_block=conversion_block,
            expired_subs=total_expired_all,
            bot_expired_subs=expired_subs,
            web_expired_subs=web_expired_subs,
        )
    else:
        stars_text = f" + {stars_total}⭐" if stars_total > 0 else ""
        conversion_block = (
            f"\n\n📈 <b>Воронка и конверсия:</b>\n"
            f" ├ 🎁 <b>Демо → Покупка:</b> <b>{bot_conv}%</b> ({bot_demo_paid} купили из {bot_demo_total} демо)\n"
            f" └ 📉 <b>Отток клиентов:</b> <b>{churn_rate:.1f}%</b> ({churned} не продлили из {ever_paid} плативших)"
        )
        msg_text = ADMIN_STATS.format(
            total_users=total_users or 0,
            new_today=new_today or 0,
            paying_users=paying_users or 0,
            active_subs=active_subs or 0,
            details=details,
            expired_subs=expired_subs or 0,
            total_payments=total_payments or 0,
            rev_today=f"<b>{bot_rub_today:,.0f} ₽</b>",
            rev_7d=f"<b>{bot_rub_week:,.0f} ₽</b>",
            rev_30d=f"<b>{bot_rub_month:,.0f} ₽</b>",
            rev_all=f"<b>{bot_rub_total:,.0f} ₽</b>",
            stars_text=stars_text,
            conversion_block=conversion_block,
        )

    await callback.message.edit_text(
        msg_text,
        reply_markup=stats_menu_kb(webstore=settings.webstore_public_enabled),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "adm_stats_revenue")
async def admin_stats_revenue(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return

    now = datetime.utcnow()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = today - timedelta(days=7)
    month_ago = today - timedelta(days=30)

    web_data = await _fetch_webstore_stats_dict()

    async with async_session() as session:
        # RUB revenue (amount in kopecks)
        def _rub_sum(since):
            return select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.status == PaymentStatus.COMPLETED,
                Payment.currency == "RUB",
                Payment.created_at >= since,
            )

        rub_today = (await session.scalar(_rub_sum(today))) / 100
        rub_week = (await session.scalar(_rub_sum(week_ago))) / 100
        rub_month = (await session.scalar(_rub_sum(month_ago))) / 100
        rub_total = (await session.scalar(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.status == PaymentStatus.COMPLETED,
                Payment.currency == "RUB",
            )
        )) / 100

        # Stars revenue
        def _stars_sum(since):
            return select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.status == PaymentStatus.COMPLETED,
                Payment.currency == "XTR",
                Payment.created_at >= since,
            )

        stars_today = await session.scalar(_stars_sum(today)) or 0
        stars_week = await session.scalar(_stars_sum(week_ago)) or 0
        stars_month = await session.scalar(_stars_sum(month_ago)) or 0
        stars_total = await session.scalar(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.status == PaymentStatus.COMPLETED,
                Payment.currency == "XTR",
            )
        ) or 0

        # Payment count
        cnt_today = await session.scalar(
            select(func.count(Payment.id)).where(
                Payment.status == PaymentStatus.COMPLETED,
                Payment.created_at >= today,
            )
        ) or 0
        cnt_week = await session.scalar(
            select(func.count(Payment.id)).where(
                Payment.status == PaymentStatus.COMPLETED,
                Payment.created_at >= week_ago,
            )
        ) or 0
        cnt_month = await session.scalar(
            select(func.count(Payment.id)).where(
                Payment.status == PaymentStatus.COMPLETED,
                Payment.created_at >= month_ago,
            )
        ) or 0
        cnt_total = await session.scalar(
            select(func.count(Payment.id)).where(
                Payment.status == PaymentStatus.COMPLETED,
            )
        ) or 0

    if web_data:
        web_rev = web_data.get("revenue", {})
        web_sc = web_data.get("status_counts", {})
        web_delivered = int(web_sc.get("delivered", 0) or 0)
        web_today = float(web_rev.get("today", 0) or 0)
        web_w7 = float(web_rev.get("w7", 0) or 0)
        web_w30 = float(web_rev.get("w30", 0) or 0)
        web_all = float(web_rev.get("all", 0) or 0)

        comb_today = rub_today + web_today
        comb_week = rub_week + web_w7
        comb_month = rub_month + web_w30
        comb_total = rub_total + web_all
        comb_cnt_total = cnt_total + web_delivered

        stars_today_text = f" + {stars_today}⭐" if stars_today > 0 else ""
        stars_week_text = f" + {stars_week}⭐" if stars_week > 0 else ""
        stars_month_text = f" + {stars_month}⭐" if stars_month > 0 else ""
        stars_total_text = f" + {stars_total}⭐" if stars_total > 0 else ""

        text = (
            "💰 <b>Выручка</b>\n\n"
            f"<b>Сегодня:</b>\n"
            f"  <b>{comb_today:,.0f} ₽</b> (ТГ: {rub_today:,.0f} ₽ · Сайт: {web_today:,.0f} ₽){stars_today_text}\n\n"
            f"<b>За 7 дней:</b>\n"
            f"  <b>{comb_week:,.0f} ₽</b> (ТГ: {rub_week:,.0f} ₽ · Сайт: {web_w7:,.0f} ₽){stars_week_text}\n\n"
            f"<b>За 30 дней:</b>\n"
            f"  <b>{comb_month:,.0f} ₽</b> (ТГ: {rub_month:,.0f} ₽ · Сайт: {web_w30:,.0f} ₽){stars_month_text}\n\n"
            f"<b>За всё время:</b>\n"
            f"  <b>{comb_total:,.0f} ₽</b> (ТГ: {rub_total:,.0f} ₽ · Сайт: {web_all:,.0f} ₽){stars_total_text}\n\n"
            f"<b>Оплат за всё время:</b> <b>{comb_cnt_total}</b> (ТГ: {cnt_total} · Сайт: {web_delivered})"
        )
    else:
        text = (
            "💰 <b>Выручка</b>\n\n"
            f"<b>Сегодня:</b>\n"
            f"  {rub_today:,.0f}₽ + {stars_today}⭐ ({cnt_today} оплат)\n\n"
            f"<b>За 7 дней:</b>\n"
            f"  {rub_week:,.0f}₽ + {stars_week}⭐ ({cnt_week} оплат)\n\n"
            f"<b>За 30 дней:</b>\n"
            f"  {rub_month:,.0f}₽ + {stars_month}⭐ ({cnt_month} оплат)\n\n"
            f"<b>За всё время:</b>\n"
            f"  {rub_total:,.0f}₽ + {stars_total}⭐ ({cnt_total} оплат)"
        )

    await callback.message.edit_text(text, reply_markup=stats_back_kb(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "adm_stats_users")
async def admin_stats_users(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return

    now = datetime.utcnow()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = today - timedelta(days=7)
    month_ago = today - timedelta(days=30)

    web_data = await _fetch_webstore_stats_dict()

    async with async_session() as session:
        total = await session.scalar(select(func.count(User.id))) or 0
        new_today = await session.scalar(
            select(func.count(User.id)).where(User.created_at >= today)
        ) or 0
        new_week = await session.scalar(
            select(func.count(User.id)).where(User.created_at >= week_ago)
        ) or 0
        new_month = await session.scalar(
            select(func.count(User.id)).where(User.created_at >= month_ago)
        ) or 0

        # Users with at least one active paid sub
        paying_users = await session.scalar(
            select(func.count(func.distinct(Subscription.user_id))).where(
                Subscription.status == SubStatus.ACTIVE,
                Subscription.billing_mode != "demo",
            )
        ) or 0

    if web_data:
        web_profiles = int(web_data.get("total_profiles", 0) or 0)
        total_clients = total + web_profiles
        text = (
            "👥 <b>Пользователи</b>\n\n"
            f"<b>Всего клиентов:</b> <b>{total_clients}</b>\n"
            f" ├ ТГ-бот: <b>{total}</b> (активных платящих: <b>{paying_users}</b>)\n"
            f" └ Сайт: <b>{web_profiles}</b>\n\n"
            "<b>Новых в ТГ-боте:</b>\n"
            f"  Сегодня: <b>{new_today}</b>\n"
            f"  За 7 дней: <b>{new_week}</b>\n"
            f"  За 30 дней: <b>{new_month}</b>"
        )
    else:
        text = (
            "👥 <b>Пользователи</b>\n\n"
            f"Всего в ТГ-боте: <b>{total}</b>\n"
            f"Активных платящих: <b>{paying_users}</b>\n\n"
            "<b>Новых:</b>\n"
            f"  Сегодня: <b>{new_today}</b>\n"
            f"  За 7 дней: <b>{new_week}</b>\n"
            f"  За 30 дней: <b>{new_month}</b>"
        )

    await callback.message.edit_text(text, reply_markup=stats_back_kb(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "adm_stats_conversion")
async def admin_stats_conversion(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return

    web_data = await _fetch_webstore_stats_dict()

    async with async_session() as session:
        # Users who actually paid in bot
        paid_user_ids_q = (
            select(Payment.user_id)
            .where(Payment.status == PaymentStatus.COMPLETED)
            .distinct()
        )

        demo_users = await session.scalar(
            select(func.count(func.distinct(Subscription.user_id))).where(
                Subscription.billing_mode == "demo",
            )
        ) or 0

        converted = await session.scalar(
            select(func.count(func.distinct(Subscription.user_id))).where(
                Subscription.billing_mode == "demo",
                Subscription.user_id.in_(paid_user_ids_q),
            )
        ) or 0

        conversion_rate = (converted / demo_users * 100) if demo_users > 0 else 0

        ever_paid = await session.scalar(
            select(func.count(func.distinct(Payment.user_id))).where(
                Payment.status == PaymentStatus.COMPLETED,
            )
        ) or 0

        still_active = await session.scalar(
            select(func.count(func.distinct(Subscription.user_id))).where(
                Subscription.billing_mode != "demo",
                Subscription.status == SubStatus.ACTIVE,
                Subscription.user_id.in_(paid_user_ids_q),
            )
        ) or 0

        churned = ever_paid - still_active
        churn_rate = (churned / ever_paid * 100) if ever_paid > 0 else 0

        manual_payments = await session.scalar(
            select(func.count(Payment.id)).where(
                Payment.method == PaymentMethod.MANUAL,
                Payment.status == PaymentStatus.COMPLETED,
            )
        ) or 0

    text = "📈 <b>Конверсия и отток</b>\n\n"
    text += (
        "<b>Демо → Оплата (ТГ-бот):</b>\n"
        f"  Взяли демо: <b>{demo_users}</b>\n"
        f"  Купили подписку: <b>{converted}</b>\n"
        f"  Конверсия: <b>{conversion_rate:.1f}%</b>\n\n"
    )

    if web_data:
        conv = web_data.get("conversion", {})
        total_30d = conv.get("total_30d", 0)
        paid_30d = conv.get("paid_30d", 0)
        conv_pct = round(paid_30d / total_30d * 100, 1) if total_30d else 0.0
        sc = web_data.get("status_counts", {})
        web_demo = sc.get("demo", 0)
        text += (
            "<b>Сайт (Вебстор, 30 дней):</b>\n"
            f"  Демо за всё время: <b>{web_demo}</b>\n"
            f"  Оформлений заказа: <b>{total_30d}</b>\n"
            f"  Оплачено: <b>{paid_30d}</b>\n"
            f"  Конверсия чекаута: <b>{conv_pct}%</b>\n\n"
        )

    text += (
        "<b>Отток (churn в боте):</b>\n"
        f"  Когда-либо платили: <b>{ever_paid}</b>\n"
        f"  Сейчас активны: <b>{still_active}</b>\n"
        f"  Не продлили: <b>{churned}</b>\n"
        f"  Отток: <b>{churn_rate:.1f}%</b>"
    )
    if manual_payments > 0:
        text += f"\n\n<b>Ручные выдачи админа:</b> <b>{manual_payments}</b>"

    await callback.message.edit_text(text, reply_markup=stats_back_kb(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "adm_stats_methods")
async def admin_stats_methods(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return

    method_labels = {
        PaymentMethod.STARS: "⭐ Stars",
        PaymentMethod.TELEGRAM: "💳 Telegram Pay",
        PaymentMethod.YOOKASSA: "💳 YooKassa",
        PaymentMethod.ROBOKASSA: "💰 Robokassa",
        PaymentMethod.MANUAL: "🔧 Ручная",
        PaymentMethod.BALANCE: "💎 Баланс",
    }

    async with async_session() as session:
        result = await session.execute(
            select(
                Payment.method,
                func.count(Payment.id),
                func.coalesce(func.sum(Payment.amount), 0),
                Payment.currency,
            )
            .where(Payment.status == PaymentStatus.COMPLETED)
            .group_by(Payment.method, Payment.currency)
            .order_by(func.count(Payment.id).desc())
        )
        rows = result.all()

    lines = []
    for method, cnt, total_amount, currency in rows:
        label = method_labels.get(method, str(method))
        if currency == "XTR":
            lines.append(f"  {label}: <b>{cnt}</b> оплат, {total_amount}⭐")
        else:
            lines.append(f"  {label}: <b>{cnt}</b> оплат, {total_amount / 100:,.0f}₽")

    text = "💳 <b>Способы оплаты</b>\n\n" + ("\n".join(lines) if lines else "Нет данных")

    await callback.message.edit_text(text, reply_markup=stats_back_kb(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "adm_stats_tariffs")
async def admin_stats_tariffs(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return

    async with async_session() as session:
        # Group subscriptions by tariff_id, Tariff fields, and subscription duration
        result = await session.execute(
            select(
                Subscription.tariff_id,
                Tariff.label,
                Tariff.days.label("t_days"),
                Tariff.tariff_type,
                Subscription.tariff_days,
                Subscription.tariff_months,
                func.count(Subscription.id).label("total_cnt"),
                func.count(Subscription.id).filter(Subscription.status == SubStatus.ACTIVE).label("active_cnt"),
            )
            .outerjoin(Tariff, Subscription.tariff_id == Tariff.id)
            .where(Subscription.billing_mode != "demo")
            .group_by(
                Subscription.tariff_id,
                Tariff.label,
                Tariff.days,
                Tariff.tariff_type,
                Subscription.tariff_days,
                Subscription.tariff_months,
            )
            .order_by(func.count(Subscription.id).desc())
        )
        rows = result.all()

    if not rows:
        await callback.message.edit_text("🏆 <b>Топ тарифов</b>\n\nНет данных", reply_markup=stats_back_kb(), parse_mode="HTML")
        await callback.answer()
        return

    categories: dict[str, list[str]] = {
        "🌐 Основные тарифы": [],
        "🚀 Adapt Group": [],
        "⚡️ VHQ": [],
        "📱 TG-Ускоритель": [],
        "📦 Прочие": [],
    }

    for tid, tlabel, t_days, t_type, s_days, s_months, total_cnt, active_cnt in rows:
        days_val = s_days or t_days or 0
        months_val = s_months or 0

        if days_val > 0 or months_val > 0:
            duration_str = format_subscription_duration(tariff_days=days_val, tariff_months=months_val)
        else:
            duration_str = ""

        if duration_str == "N/A":
            duration_str = ""

        if tlabel:
            if duration_str and duration_str not in tlabel:
                title = f"{tlabel} ({duration_str})"
            else:
                title = tlabel
        elif duration_str:
            title = f"Тариф {duration_str}"
        else:
            title = f"Тариф #{tid}" if tid else "Стандартный доступ"

        entry = f"  • <b>{title}</b>\n    └ Всего: <b>{total_cnt}</b> | Активных: <b>{active_cnt}</b>"

        if t_type == TariffType.TG_PROXY:
            categories["📱 TG-Ускоритель"].append(entry)
        elif tlabel and "adapt" in tlabel.lower():
            categories["🚀 Adapt Group"].append(entry)
        elif tlabel and "vhq" in tlabel.lower():
            categories["⚡️ VHQ"].append(entry)
        elif t_type == TariffType.BOTH or t_type == TariffType.VPN:
            categories["🌐 Основные тарифы"].append(entry)
        else:
            categories["📦 Прочие"].append(entry)

    text_blocks = ["🏆 <b>Топ тарифов</b>\n"]
    for cat_name, items in categories.items():
        if items:
            text_blocks.append(f"<b>{cat_name}</b>:\n" + "\n".join(items))

    text = "\n\n".join(text_blocks)

    await callback.message.edit_text(text, reply_markup=stats_back_kb(), parse_mode="HTML")
    await callback.answer()


# ── Servers ───────────────────────────────────────────

@router.callback_query(F.data == "adm_servers")
async def admin_servers(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return

    async with async_session() as session:
        result = await session.execute(select(Server).order_by(Server.name))
        servers = result.scalars().all()

    if not servers:
        await callback.message.edit_text(
            "🖥 <b>Серверы</b>\n\nСерверов пока нет.",
            reply_markup=admin_back_kb(),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    await callback.message.edit_text(
        "🖥 <b>Серверы</b>\n\nВыберите сервер:",
        reply_markup=server_list_kb(servers),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm_srv_toggle_"))
async def admin_toggle_server(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    server_id = int(callback.data.split("_")[-1])
    async with async_session() as session:
        server = await session.get(Server, server_id)
        if server:
            server.is_active = not server.is_active
            await session.commit()
            status = "🟢 включён" if server.is_active else "🔴 выключен"
            await write_admin_audit(
                actor_telegram_id=callback.from_user.id,
                action="server_status_changed",
                entity_type="server",
                entity_id=str(server_id),
                summary=f"Сервер «{server.name}» {status.replace('🟢 ', '').replace('🔴 ', '')}",
            )
            await callback.answer(
                SERVER_TOGGLED.format(name=server.name, status=status), show_alert=True,
            )
    await admin_servers(callback)


@router.callback_query(
    F.data.startswith("adm_srv_")
    & ~F.data.startswith("adm_srv_toggle_")
    & ~F.data.startswith("adm_srv_del_")
)
async def admin_server_info(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    server_id = int(callback.data.split("_")[-1])
    async with async_session() as session:
        server = await session.get(Server, server_id)
    if not server:
        await callback.answer("Сервер не найден", show_alert=True)
        return
        
    stats = await vpn_manager.get_server_status(server)
    
    await callback.message.edit_text(
        ADMIN_SERVER_INFO.format(
            name=server.name,
            emoji=server.country_emoji,
            location=server.location,
            host=server.host,
            api_url=server.api_url or "Не задан",
            api_user=server.api_username or "Не задан",
            protocol=server.protocol,
            current=server.current_clients,
            max=server.max_clients,
            peers=stats.get("peers", 0),
            uptime=stats.get("uptime", "Unknown"),
            load=stats.get("load", "0%"),
            online="🟢 Да" if stats.get("online") else "🔴 Нет",
            status="🟢 Активен" if server.is_active else "🔴 Выключен",
            created=_format_dt_msk(server.created_at, include_time=False),
        ),
        reply_markup=server_actions_kb(server.id, server.is_active),
        parse_mode="HTML",
    )
    await callback.answer()


# ── Add Server ────────────────────────────────────────

@router.callback_query(F.data == "adm_add_server")
async def admin_add_server_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await callback.message.edit_text(
        "➕ <b>Добавить сервер</b>\n\n"
        "Отправьте данные в формате:\n"
        "<code>Имя | Хост (IP) | API URL | API User | API Pass | Локация | Эмодзи | Макс.клиентов</code>\n\n"
        "Пример:\n"
        "<code>NL-1 | 185.100.50.1 | http://185.100.50.1:8000 | admin | secret | Amsterdam | 🇳🇱 | 50</code>",
        reply_markup=admin_back_kb(),
        parse_mode="HTML",
    )
    await state.set_state(AdminStates.waiting_server_data)
    await callback.answer()


@router.message(AdminStates.waiting_server_data, F.text)
async def admin_add_server_process(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    try:
        parts = [p.strip() for p in message.text.split("|")]
        if len(parts) != 8:
            raise ValueError("Need exactly 8 fields")
        name, host, api_url, api_username, api_password, location, emoji, max_clients = parts
        async with async_session() as session:
            session.add(Server(
                name=name, host=host, api_url=api_url,
                api_username=api_username, api_password=api_password,
                location=location, country_emoji=emoji, 
                max_clients=int(max_clients), protocol="Marzban"
            ))
            await session.commit()
        await message.answer(
            SERVER_ADDED.format(name=name),
            reply_markup=admin_menu_kb(),
            parse_mode="HTML",
        )
        await write_admin_audit(
            actor_telegram_id=message.from_user.id,
            action="server_created",
            entity_type="server",
            entity_id=name,
            summary=f"Сервер «{name}» добавлен",
            details=f"Локация: {location}; лимит клиентов: {int(max_clients)}",
        )
        await state.clear()
    except Exception as exc:
        logger.error(f"Server add error: {exc}")
        await message.answer(
            "❌ Ошибка. Формат: <code>Имя | Хост | API URL | API User | API Pass | Локация | Эмодзи | Макс</code>",
            reply_markup=admin_back_kb(),
            parse_mode="HTML",
        )


# ── Users ─────────────────────────────────────────────

@router.callback_query(F.data.startswith("adm_users"))
async def admin_users(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await state.clear()

    parts = callback.data.split("_")
    page = 1
    if len(parts) > 2:
        try:
            page = int(parts[2])
        except ValueError:
            page = 1

    async with async_session() as session:
        result = await session.execute(
            select(User).order_by(User.created_at.desc())
        )
        all_users = result.scalars().all()

    total_users = len(all_users)
    items_per_page = 10
    total_pages = max(1, (total_users + items_per_page - 1) // items_per_page)

    if page < 1:
        page = total_pages
    elif page > total_pages:
        page = 1

    start_idx = (page - 1) * items_per_page
    end_idx = start_idx + items_per_page
    users_on_page = all_users[start_idx:end_idx]

    text = (
        f"👥 <b>Управление пользователями</b>\n\n"
        f"Всего клиентов: <b>{total_users}</b>\n"
        f"Страница: <b>{page}/{total_pages}</b>\n\n"
        f"<i>Выберите клиента ниже или воспользуйтесь поиском:</i>"
    )

    try:
        await callback.message.edit_text(
            text,
            reply_markup=user_search_kb(users_on_page, page, total_pages),
            parse_mode="HTML",
        )
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data.regexp(r"^adm_audit_\d+$"))
async def admin_audit_journal(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    page = max(1, int(callback.data.removeprefix("adm_audit_")))
    page_size = 8
    async with async_session() as session:
        total = await session.scalar(select(func.count(AdminActionLog.id))) or 0
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, total_pages)
        rows = (await session.execute(
            select(AdminActionLog)
            .order_by(AdminActionLog.created_at.desc(), AdminActionLog.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )).scalars().all()

    action_labels = {
        "unified_user_search": "🔎 Поиск клиентов",
        "web_order_retry": "🔄 Повтор выдачи",
        "web_order_resend": "📨 Повтор уведомления",
        "web_order_cancel": "❌ Отмена заказа",
        "user_block_toggle": "👤 Доступ пользователя",
        "user_account_reset": "☠️ Сброс аккаунта",
        "partner_created": "🤝 Создание партнёра",
        "adapt_device_deleted": "📱 Удаление устройства",
        "server_status_changed": "🖥 Статус сервера",
        "server_created": "➕ Добавление сервера",
        "server_deleted": "🗑 Удаление сервера",
        "tariff_status_changed": "📋 Статус тарифа",
        "tariff_visibility_changed": "👁 Видимость тарифа",
        "tariff_deleted": "🗑 Удаление тарифа",
        "web_balance_adjusted": "💳 Изменение баланса сайта",
    }
    status_labels = {"success": "✅", "partial": "⚠️", "error": "❌"}
    blocks: list[str] = []
    for row in rows:
        target = ""
        if row.entity_id:
            target = f"\nОбъект: <code>{html.escape(row.entity_id)}</code>"
        if row.target_telegram_id:
            target += f"\nКлиент TG: <code>{row.target_telegram_id}</code>"
        details = f"\nДетали: {html.escape(row.details[:350])}" if row.details else ""
        blocks.append(
            f"{status_labels.get(row.status, '•')} <b>{html.escape(action_labels.get(row.action, row.action))}</b>\n"
            f"Администратор: <code>{row.actor_telegram_id}</code>\n"
            f"Время: <b>{html.escape(_format_dt_msk(row.created_at))}</b>{target}\n"
            f"Результат: {html.escape(row.summary)}{details}"
        )
    divider = "\n━━━━━━━━━━━━━━━━━━━━\n"
    text_value = (
        f"📜 <b>Журнал действий администраторов</b>\n"
        f"Записей: <b>{total}</b> · Страница <b>{page}/{total_pages}</b>\n\n"
        + (divider.join(blocks) if blocks else "<i>Записей пока нет.</i>")
    )
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="◀️", callback_data=f"adm_audit_{max(1, page - 1)}"),
        InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="ignore"),
        InlineKeyboardButton(text="▶️", callback_data=f"adm_audit_{min(total_pages, page + 1)}"),
    )
    kb.row(InlineKeyboardButton(text="🔄 Обновить", callback_data=f"adm_audit_{page}"))
    kb.row(InlineKeyboardButton(text="◀️ В админку", callback_data="adm_back"))
    try:
        await callback.message.edit_text(text_value, reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception:
        await callback.message.answer(text_value, reply_markup=kb.as_markup(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("adm_usr_info_"))
async def admin_user_info(callback: CallbackQuery, state: FSMContext) -> None:
    """Show user card from list."""
    if not _is_admin(callback.from_user.id):
        return
    
    telegram_id = int(callback.data.removeprefix("adm_usr_info_"))
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        user = result.scalar_one_or_none()
    
    if not user:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
        
    await _show_user_info(callback.message, user)
    await state.clear()
    await callback.answer()


async def _fetch_webstore_search_profiles(query: str) -> list[dict]:
    if not settings.webstore_public_enabled:
        return []
    import aiohttp as _aiohttp
    url = f"{settings.webstore_api_base_url.rstrip('/')}/api/store/internal/admin-client-lookup"
    headers = {"X-Internal-Secret": settings.webstore_bridge_secret}
    async with _aiohttp.ClientSession(timeout=_aiohttp.ClientTimeout(total=10)) as sess:
        async with sess.get(url, headers=headers, params={"q": query}) as resp:
            if resp.status == 404:
                return []
            data = await resp.json()
            if resp.status != 200:
                raise RuntimeError(str(data.get("error") or f"HTTP {resp.status}"))
            return list(data.get("profiles") or [])


async def _run_unified_user_search(query: str) -> tuple[list[dict], str | None]:
    clean = query.strip()
    username = clean.removeprefix("@")
    numeric = int(clean) if clean.isdigit() else -1
    like = f"%{clean}%"
    username_like = f"%{username}%"

    sub_user_ids = select(Subscription.user_id).outerjoin(
        AdaptSubscription, AdaptSubscription.subscription_id == Subscription.id
    ).where(or_(
        Subscription.vpn_key.contains(clean),
        Subscription.client_name.ilike(like),
        AdaptSubscription.adapt_uuid.ilike(like),
    ))
    proxy_user_ids = select(ProxyAccount.user_id).where(or_(
        ProxyAccount.sub_url.contains(clean),
        ProxyAccount.marzban_username.ilike(like),
    ))
    async with async_session() as session:
        result = await session.execute(
            select(User).where(or_(
                User.telegram_id == numeric,
                User.username.ilike(username_like),
                User.full_name.ilike(like),
                User.id.in_(sub_user_ids),
                User.id.in_(proxy_user_ids),
            )).order_by(User.created_at.desc()).limit(100)
        )
        bot_users = result.scalars().all()

    web_error = None
    try:
        web_profiles = await _fetch_webstore_search_profiles(clean)
    except Exception as exc:
        logger.warning("Unified admin search: webstore unavailable: %s", exc)
        web_profiles = []
        web_error = "Вебстор временно не ответил; показаны результаты Telegram-бота."

    results: list[dict] = []
    bot_tg_ids = {int(user.telegram_id) for user in bot_users}
    web_by_tg: dict[int, dict] = {}
    for profile in web_profiles:
        tg = profile.get("telegram") or {}
        tg_raw = tg.get("id")
        if str(tg_raw or "").isdigit():
            web_by_tg[int(tg_raw)] = profile

    for user in bot_users:
        profile = web_by_tg.get(int(user.telegram_id))
        results.append({
            "kind": "bot",
            "telegram_id": int(user.telegram_id),
            "username": user.username or "",
            "name": user.full_name or user.username or str(user.telegram_id),
            "blocked": bool(user.is_blocked),
            "has_web": bool(profile),
            "contact": str((profile or {}).get("contact") or ""),
        })
    for profile in web_profiles:
        tg = profile.get("telegram") or {}
        tg_raw = tg.get("id")
        if str(tg_raw or "").isdigit() and int(tg_raw) in bot_tg_ids:
            continue
        results.append({
            "kind": "web",
            "profile_token": str(profile.get("profile_token") or ""),
            "contact": str(profile.get("contact") or "—"),
            "telegram_id": int(tg_raw) if str(tg_raw or "").isdigit() else None,
            "username": str(tg.get("username") or ""),
            "name": str(tg.get("full_name") or profile.get("contact") or "Клиент сайта"),
        })
    return results, web_error


def _build_unified_search_page(results: list[dict], query: str, page: int, warning: str | None = None):
    page_size = 8
    total_pages = max(1, (len(results) + page_size - 1) // page_size)
    current = min(max(1, page), total_pages)
    start = (current - 1) * page_size
    shown = results[start:start + page_size]
    blocks: list[str] = []
    kb = InlineKeyboardBuilder()
    for absolute_idx, item in enumerate(shown, start=start):
        if item["kind"] == "bot":
            source = "Telegram + сайт" if item.get("has_web") else "Telegram"
            identifier = f"ID {item['telegram_id']}"
            if item.get("username"):
                identifier += f" · @{item['username']}"
            callback_data = f"adm_usr_info_{item['telegram_id']}"
        else:
            source = "Только сайт"
            identifier = item.get("contact") or "профиль сайта"
            callback_data = f"adm_usearch_web_{absolute_idx}"
        profile_token = item.get("profile_token")
        if item["kind"] == "web" and str(item.get("name") or "").strip() == str(identifier).strip():
            subline = f"├ Профиль: <code>{html.escape(str(profile_token)[:12])}…</code>" if profile_token else f"├ {html.escape(identifier)}"
        else:
            subline = f"├ {html.escape(identifier)}"
        blocks.append(
            f"<b>{absolute_idx + 1}. {html.escape(str(item.get('name') or 'Клиент'))}</b>\n"
            f"{subline}\n"
            f"└ Источник: <b>{source}</b>"
        )
        kb.row(InlineKeyboardButton(
            text=f"Открыть #{absolute_idx + 1} · {str(item.get('name') or 'Клиент')[:28]}",
            callback_data=callback_data,
        ))
    divider = "\n━━━━━━━━━━━━━━━━━━━━\n"
    text_value = (
        "🔎 <b>Поиск клиентов</b>\n"
        f"Запрос: <code>{html.escape(query)}</code>\n"
        f"Найдено: <b>{len(results)}</b> · Страница <b>{current}/{total_pages}</b>\n"
    )
    if warning:
        text_value += f"\n⚠️ {html.escape(warning)}\n"
    text_value += "\n" + (divider.join(blocks) if blocks else "<i>Совпадений нет.</i>")
    if total_pages > 1:
        kb.row(
            InlineKeyboardButton(text="◀️", callback_data=f"adm_usearch_page_{max(1, current - 1)}"),
            InlineKeyboardButton(text=f"{current}/{total_pages}", callback_data="ignore"),
            InlineKeyboardButton(text="▶️", callback_data=f"adm_usearch_page_{min(total_pages, current + 1)}"),
        )
    kb.row(InlineKeyboardButton(text="🔎 Поиск", callback_data="adm_user_search"))
    kb.row(InlineKeyboardButton(text="◀️ К клиентам", callback_data="adm_users"))
    return text_value, kb.as_markup()


@router.callback_query(F.data == "adm_user_search")
async def admin_unified_search_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await state.set_state(AdminStates.waiting_user_search)
    await callback.message.edit_text(
        "🔎 <b>Поиск клиентов</b>\n\n"
        "Введите Telegram ID, @username, имя, телефон, email, номер заказа, "
        "ссылку подписки, ключ, UUID Adapt или имя клиента на сервере:",
        reply_markup=admin_back_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminStates.waiting_user_search, F.text)
async def admin_unified_search_process(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    query = message.text.strip()
    if len(query) < 2:
        await message.answer("Введите минимум 2 символа.")
        return
    results, warning = await _run_unified_user_search(query)
    await state.update_data(unified_results=results, unified_query=query, unified_warning=warning)
    text_value, markup = _build_unified_search_page(results, query, 1, warning)
    await message.answer(text_value, reply_markup=markup, parse_mode="HTML")
    await write_admin_audit(
        actor_telegram_id=message.from_user.id,
        action="unified_user_search",
        entity_type="user_search",
        status="partial" if warning else "success",
        summary=f"Поиск клиентов: найдено {len(results)}",
        details=(
            "Запрос: ссылка/ключ скрыт"
            if query.lower().startswith(("http://", "https://")) or len(query) > 64
            else f"Запрос: {query[:120]}"
        ),
    )


@router.callback_query(F.data.startswith("adm_usearch_page_"))
async def admin_unified_search_page(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    data = await state.get_data()
    results = data.get("unified_results") or []
    if not results and "unified_query" not in data:
        await callback.answer("Поиск устарел. Запустите его заново.", show_alert=True)
        return
    page = int(callback.data.removeprefix("adm_usearch_page_"))
    text_value, markup = _build_unified_search_page(
        results, data.get("unified_query") or "", page, data.get("unified_warning")
    )
    await callback.message.edit_text(text_value, reply_markup=markup, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("adm_usearch_web_"))
async def admin_unified_search_web_open(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    data = await state.get_data()
    results = data.get("unified_results") or []
    idx = int(callback.data.removeprefix("adm_usearch_web_"))
    if idx < 0 or idx >= len(results) or results[idx].get("kind") != "web":
        await callback.answer("Результат устарел. Запустите поиск заново.", show_alert=True)
        return
    token_prefix = str(results[idx].get("profile_token") or "")[:16]
    if not token_prefix:
        await callback.answer("У профиля нет идентификатора", show_alert=True)
        return
    # Reuse the full web profile card using a callback safely below Telegram's 64-byte limit.
    synthetic = callback.model_copy(update={"data": f"adm_wb_prof_q:{token_prefix}:1"})
    await admin_web_client_quick_lookup(synthetic)
    await state.clear()


@router.callback_query(F.data == "adm_user_by_id")
async def admin_user_by_id_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await callback.message.edit_text(
        "🔍 Отправьте <b>Telegram ID</b> пользователя:",
        reply_markup=admin_back_kb(),
        parse_mode="HTML",
    )
    await state.set_state(AdminStates.waiting_user_id)
    await callback.answer()


@router.message(AdminStates.waiting_user_id, F.text)
async def admin_user_by_id_process(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    try:
        telegram_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введите числовой Telegram ID")
        return

    async with async_session() as session:
        result = await session.execute(
            select(User)
            .options(selectinload(User.subscriptions))
            .where(User.telegram_id == telegram_id)
        )
        user = result.scalar_one_or_none()

    if not user:
        await message.answer("❌ Пользователь не найден", reply_markup=admin_back_kb(), parse_mode="HTML")
        await state.clear()
        return

    await _show_user_info(message, user)
    await state.clear()


@router.callback_query(F.data == "adm_user_by_username")
async def admin_user_by_username_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await callback.message.edit_text(
        "🔍 Отправьте <b>Username</b> пользователя (с @ или без):",
        reply_markup=admin_back_kb(),
        parse_mode="HTML",
    )
    await state.set_state(AdminStates.waiting_user_username)
    await callback.answer()

@router.callback_query(F.data == "adm_user_by_link")
async def admin_user_by_link_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await callback.message.edit_text(
        "🔍 Отправьте <b>Ссылку на подписку</b> или <b>Ключ</b>:",
        reply_markup=admin_back_kb(),
        parse_mode="HTML",
    )
    await state.set_state(AdminStates.waiting_user_link)
    await callback.answer()

@router.message(AdminStates.waiting_user_username, F.text)
async def admin_user_by_username_process(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
        
    username = message.text.strip().replace("@", "")
    
    async with async_session() as session:
        result = await session.execute(
            select(User)
            .options(selectinload(User.subscriptions))
            .where(User.username.ilike(f"%{username}%"))
        )
        users = result.scalars().all()

    if not users:
        await message.answer("❌ Пользователь не найден", reply_markup=admin_back_kb(), parse_mode="HTML")
        await state.clear()
        return

    if len(users) > 1:
        # If multiple users match, we just show the first one for simplicity 
        # (or could build a selection list)
        user = users[0]
    else:
        user = users[0]

    await _show_user_info(message, user)
    await state.clear()


@router.message(AdminStates.waiting_user_link, F.text)
async def admin_user_by_link_process(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
        
    search_term = message.text.strip()
    
    async with async_session() as session:
        # Join User -> Subscription
        result = await session.execute(
            select(User)
            .join(Subscription, User.id == Subscription.user_id)
            .options(selectinload(User.subscriptions))
            .where(
                (Subscription.vpn_key.contains(search_term))
            )
        )
        user = result.scalar_one_or_none()

    if not user:
        await message.answer("❌ Пользователь по данной ссылке/ключу не найден", reply_markup=admin_back_kb(), parse_mode="HTML")
        await state.clear()
        return

    await _show_user_info(message, user)
    await state.clear()



@router.callback_query(F.data.startswith("adm_usr_block_"))
async def admin_toggle_block(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    telegram_id = int(callback.data.split("_")[-1])
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if user:
            user.is_blocked = not user.is_blocked
            await session.commit()
            status = "заблокирован 🚫" if user.is_blocked else "разблокирован ✅"
            await write_admin_audit(
                actor_telegram_id=callback.from_user.id,
                action="user_block_toggle",
                entity_type="telegram_user",
                entity_id=str(telegram_id),
                target_telegram_id=telegram_id,
                summary=f"Пользователь {status.replace(' 🚫', '').replace(' ✅', '')}",
            )
            await callback.answer(f"Пользователь {status}", show_alert=True)
            # Refresh card
            await _show_user_info(callback.message, user)
            return
    await callback.answer()


@router.callback_query(F.data.startswith("adm_usr_reset_conf_"))
async def admin_user_reset_conf(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    telegram_id = int(callback.data.split("_")[-1])
    kb = user_reset_confirm_kb(telegram_id)
    text = (
        "⚠️ <b>ВНИМАНИЕ!</b>\n\n"
        "Вы собираетесь ПОЛНОСТЬЮ обнулить этот аккаунт.\n"
        "Будут удалены все подписки, ключи доступа и баланс.\n\n"
        "Вы уверены?"
    )
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("adm_usr_reset_do_"))
async def admin_user_reset_do(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    telegram_id = int(callback.data.split("_")[-1])
    
    async with async_session() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        if not user:
            await callback.answer("Пользователь не найден", show_alert=True)
            return

        # Disable FK checks for the duration of this wipe
        await session.execute(text("PRAGMA foreign_keys = OFF"))

        subs = await session.scalars(select(Subscription.id).where(Subscription.user_id == user.id))
        sub_ids = subs.all()
        
        if sub_ids:
            try:
                from bot.models import VhqSubscription
                await session.execute(delete(VhqSubscription).where(VhqSubscription.subscription_id.in_(sub_ids)))
            except (ImportError, Exception):
                pass
            
            await session.execute(delete(AdaptSubscription).where(AdaptSubscription.subscription_id.in_(sub_ids)))
            await session.execute(delete(SubscriptionNotificationLog).where(SubscriptionNotificationLog.subscription_id.in_(sub_ids)))
            await session.execute(delete(RecurringPaymentProfile).where(RecurringPaymentProfile.subscription_id.in_(sub_ids)))
            await session.execute(delete(ProxyAccount).where(ProxyAccount.subscription_id.in_(sub_ids)))
            await session.execute(delete(Payment).where(Payment.subscription_id.in_(sub_ids)))
            await session.execute(delete(Subscription).where(Subscription.id.in_(sub_ids)))
        
        # Delete any remaining user-level records
        await session.execute(delete(ProxyAccount).where(ProxyAccount.user_id == user.id))
        await session.execute(delete(MTProtoAccount).where(MTProtoAccount.user_id == user.id))
        await session.execute(delete(Payment).where(Payment.user_id == user.id))
        
        user.balance = 0
        user.referral_balance = 0
        user.balance_rub = 0

        try:
            from webstore.models import WebOrder, WebProfileLink
            profile_link = await session.scalar(
                select(WebProfileLink).where(WebProfileLink.telegram_id == telegram_id)
            )
            if profile_link:
                await session.execute(
                    delete(WebOrder)
                    .where(WebOrder.profile_token == profile_link.profile_token)
                    .where(WebOrder.tariff_key == "basic_1")
                )
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Failed to clear web orders: %s", e)
        
        await session.commit()
        await session.execute(text("PRAGMA foreign_keys = ON"))
    
    await callback.answer("Аккаунт успешно обнулён!", show_alert=True)
    await write_admin_audit(
        actor_telegram_id=callback.from_user.id,
        action="user_account_reset",
        entity_type="telegram_user",
        entity_id=str(telegram_id),
        target_telegram_id=telegram_id,
        summary="Аккаунт обнулён",
        details=f"Удалено подписок: {len(sub_ids)}; очищены оплаты, ключи и баланс",
    )
    # Refresh to show empty profile
    await admin_refresh_user(callback)


@router.callback_query(F.data.startswith("adm_usr_refresh_"))
async def admin_refresh_user(callback: CallbackQuery) -> None:
    """Refresh user card info."""
    if not _is_admin(callback.from_user.id):
        return
    telegram_id = int(callback.data.split("_")[-1])
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        user = result.scalar_one_or_none()
    if not user:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    await _show_user_info(callback.message, user)
    await callback.answer()


@router.callback_query(F.data.startswith("adm_usr_partner_"))
async def admin_user_make_partner(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    telegram_id = int(callback.data.split("_")[-1])

    async with async_session() as session:
        user = await session.scalar(
            select(User).where(User.telegram_id == telegram_id)
        )
        if not user:
            await callback.answer("Пользователь не найден", show_alert=True)
            return

        partner = await session.scalar(
            select(Partner).where(Partner.telegram_id == telegram_id)
        )
        if partner is None:
            partner = Partner(
                name=(user.full_name or user.username or f"Partner {telegram_id}")[:128],
                telegram_id=telegram_id,
                contact_info=f"@{user.username}" if user.username else None,
                commission_percent=20.0,
                payouts_enabled=False,
            )
            session.add(partner)
            await session.commit()
            await session.refresh(partner)
            created = True
        else:
            created = False
        partner_id = partner.id

    from bot.handlers.partner import _show_partner_detail

    await _show_partner_detail(callback.message, partner_id)
    if created:
        await write_admin_audit(
            actor_telegram_id=callback.from_user.id,
            action="partner_created",
            entity_type="telegram_user",
            entity_id=str(partner_id),
            target_telegram_id=telegram_id,
            summary="Партнёр создан",
        )
    await callback.answer("Партнёр создан" if created else "Партнёр уже существует")


@router.callback_query(F.data.startswith("adm_usr_key_"))
async def admin_user_key_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Issue key directly from user card - offer renew/upgrade if user has subscriptions."""
    if not _is_admin(callback.from_user.id):
        return
    telegram_id = int(callback.data.split("_")[-1])

    await state.update_data(
        manual_key_user=telegram_id,
        manual_key_back_callback=f"adm_usr_refresh_{telegram_id}",
    )

    async with async_session() as session:
        result = await session.execute(
            select(User)
            .options(selectinload(User.subscriptions).selectinload(Subscription.tariff))
            .where(User.telegram_id == telegram_id)
        )
        user = result.scalar_one_or_none()
        if not user:
            await callback.answer("Пользователь не найден", show_alert=True)
            return

        adapt_records = {
            record.subscription_id: record
            for record in (
                await session.execute(
                    select(AdaptSubscription)
                    .join(Subscription, Subscription.id == AdaptSubscription.subscription_id)
                    .where(Subscription.user_id == user.id)
                )
            ).scalars().all()
        }
        canonical_uuids = {
            str(record.adapt_plan_uuid).strip()
            for record in adapt_records.values()
            if str(record.adapt_plan_uuid or "").strip()
        }
        adapt_tariffs = {}
        if canonical_uuids:
            for tariff in (
                await session.execute(
                    select(Tariff)
                    .where(Tariff.adapt_plan_uuid.in_(canonical_uuids))
                    .order_by(Tariff.id.desc())
                )
            ).scalars().all():
                adapt_tariffs.setdefault(str(tariff.adapt_plan_uuid).strip(), tariff)
        user_subscriptions = [
            sub for sub in user.subscriptions
            if (
                not is_adapt_subscription(sub)
                and sub.tariff
            ) or (
                is_adapt_subscription(sub)
                and (
                    adapt_records.get(sub.id)
                    or (not adapt_records.get(sub.id) and sub.tariff)
                )
            )
        ]
        if user_subscriptions:
            kb = InlineKeyboardBuilder()
            for sub in sorted(user_subscriptions, key=lambda item: (item.expires_at, item.id), reverse=True):
                expires = _format_dt_msk(sub.expires_at, include_time=False)
                adapt_record = adapt_records.get(sub.id)
                current_tariff = (
                    adapt_tariffs.get(str(adapt_record.adapt_plan_uuid or "").strip())
                    if adapt_record and str(adapt_record.adapt_plan_uuid or "").strip()
                    else sub.tariff
                )
                is_trial = is_adapt_trial_subscription(sub, adapt_sub=adapt_record)
                is_expired = bool(
                    (sub.expires_at and sub.expires_at <= datetime.utcnow())
                    or (hasattr(sub, "status") and getattr(sub.status, "value", str(sub.status)) == "expired")
                )
                if is_expired:
                    kb.row(InlineKeyboardButton(
                        text=f"↻ Продлить #{sub.id} · {current_tariff.label} (истекла)",
                        callback_data=f"adm_key_action_renew_{sub.id}",
                    ))
                elif not is_trial:
                    kb.row(InlineKeyboardButton(
                        text=f"↻ Продлить #{sub.id} · {current_tariff.label} · до {expires}",
                        callback_data=f"adm_key_action_renew_{sub.id}",
                    ))
                if is_adapt_subscription(sub) and not is_expired:
                    label_text = (
                        f"↑ Улучшить #{sub.id} (тестовый) · до {expires}"
                        if is_trial
                        else f"↑ Улучшить #{sub.id} · {current_tariff.label}"
                    )
                    kb.row(InlineKeyboardButton(
                        text=label_text,
                        callback_data=f"adm_key_action_upgrade_{sub.id}",
                    ))
            kb.row(InlineKeyboardButton(text="➕ Создать новую подписку", callback_data="adm_key_action_new_0"))
            kb.row(InlineKeyboardButton(text="◀️ Назад", callback_data=f"adm_usr_refresh_{telegram_id}"))

            text = f"Ключ для <code>{telegram_id}</code>\n\nВыберите действие и подписку:"
            try:
                await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
            except Exception:
                await callback.message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")
            await state.set_state(AdminStates.waiting_manual_key_tariff)
            await callback.answer()
            return

        # User has no subscriptions: go straight to tariff selection for a new subscription
        await state.update_data(
            manual_purchase_action="new",
            manual_target_subscription_id=None,
        )
        result = await session.execute(
            select(Tariff).where(Tariff.is_active == True).order_by(Tariff.price_rub)  # noqa: E712
        )
        db_tariffs = result.scalars().all()

    kb = _build_manual_key_tariff_kb(
        db_tariffs,
        back_callback=f"adm_usr_refresh_{telegram_id}",
    )

    try:
        await callback.message.edit_text(
            f"Выдать ключ для <code>{telegram_id}</code>\n\nВыберите тариф для новой подписки:",
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
        )
    except Exception:
        await callback.message.answer(
            f"Выдать ключ для <code>{telegram_id}</code>\n\nВыберите тариф для новой подписки:",
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
        )
    await state.set_state(AdminStates.waiting_manual_key_tariff)
    await callback.answer()


@router.callback_query(F.data.startswith("adm_srv_del_"))
async def admin_delete_server(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    server_id = int(callback.data.split("_")[-1])
    async with async_session() as session:
        server = await session.get(Server, server_id)
        if not server:
            await callback.answer("Сервер не найден", show_alert=True)
            return
        name = server.name
        await session.delete(server)
        await session.commit()
    await callback.answer(f"Сервер «{name}» удалён", show_alert=True)
    await write_admin_audit(
        actor_telegram_id=callback.from_user.id,
        action="server_deleted",
        entity_type="server",
        entity_id=str(server_id),
        summary=f"Сервер «{name}» удалён",
    )
    await admin_servers(callback)


@router.callback_query(F.data.startswith("adm_usr_subs_"))
async def admin_user_subs(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return

    parts = callback.data.split("_")
    telegram_id = int(parts[3])
    page = 1
    if len(parts) > 4:
        try:
            page = int(parts[4])
        except ValueError:
            page = 1

    async with async_session() as session:
        result = await session.execute(
            select(User)
            .options(
                selectinload(User.subscriptions).selectinload(Subscription.server),
                selectinload(User.subscriptions).selectinload(Subscription.tariff),
            )
            .where(User.telegram_id == telegram_id)
        )
        user = result.scalar_one_or_none()
        included_slots = await get_included_device_slots(session)

    if not user or not user.subscriptions:
        await callback.answer("Подписок нет", show_alert=True)
        return

    # Show all subs sorted: active first, then by expires_at desc
    all_subs = sorted(
        user.subscriptions,
        key=lambda s: (0 if s.status == SubStatus.ACTIVE else 1, -(s.expires_at.timestamp())),
    )

    total_items = len(all_subs)
    if page < 1:
        page = 1
    if page > total_items:
        page = total_items

    sub = all_subs[page - 1]
    srv_name = sub.server.name if sub.server else "?"
    srv_emoji = sub.server.country_emoji if sub.server else "🌍"

    from bot.services.adapt_routing import is_adapt_subscription, get_adapt_uuid_from_subscription
    is_adapt = is_adapt_subscription(sub)
    adapt_uuid = get_adapt_uuid_from_subscription(sub) if is_adapt else None

    extra_adapt = ""
    if is_adapt and adapt_uuid:
        connected_devices = "—"
        used_traffic_gb = "—"
        limit_traffic_gb = "—"
        try:
            from bot.services.adapt_api import AdaptAPI
            devices = await AdaptAPI().get_devices(adapt_uuid)
            connected_devices = len(devices)
        except Exception as exc:
            logger.error(f"Error fetching devices for adapt sub {sub.id}: {exc}")
            connected_devices = "ошибка"

        try:
            from bot.services.adapt_api import AdaptAPI
            status_data = await AdaptAPI().get_status(adapt_uuid)
            used_bytes = status_data.get("used_traffic_bytes") or 0
            limit_bytes = status_data.get("traffic_limit_bytes") or 0
            used_traffic_gb = f"{used_bytes / (1024**3):.2f}"
            limit_traffic_gb = f"{limit_bytes / (1024**3):.0f}"

            # Self-healing: sync authoritative end_date from Adapt API to DB if different
            adapt_end_str = status_data.get("end_date")
            if adapt_end_str:
                from bot.services.webstore_bridge import _parse_web_datetime
                adapt_end_dt = _parse_web_datetime(adapt_end_str)
                if adapt_end_dt and (not sub.expires_at or abs((sub.expires_at - adapt_end_dt).total_seconds()) > 5):
                    old_exp = sub.expires_at
                    sub.expires_at = adapt_end_dt
                    sub.status = SubStatus.ACTIVE if adapt_end_dt > datetime.utcnow() else SubStatus.EXPIRED
                    async with async_session() as write_session:
                        db_sub = await write_session.get(Subscription, sub.id)
                        if db_sub:
                            db_sub.expires_at = adapt_end_dt
                            db_sub.status = sub.status
                        adapt_rec = await write_session.scalar(
                            select(AdaptSubscription).where(AdaptSubscription.subscription_id == sub.id).limit(1)
                        )
                        if adapt_rec:
                            adapt_rec.end_date = adapt_end_dt
                            if limit_bytes:
                                adapt_rec.traffic_limit_bytes = limit_bytes
                        await write_session.commit()
                    logger.info(
                        "Self-healed Adapt sub %s expires_at in admin card: old=%s new=%s",
                        sub.id, old_exp, adapt_end_dt
                    )
        except Exception as exc:
            logger.error(f"Error fetching status for adapt sub {sub.id}: {exc}")
            used_traffic_gb = "ошибка"
            limit_traffic_gb = "ошибка"

        extra_adapt = (
            f"🖥 Подключено устройств: <b>{connected_devices}</b>\n"
            f"⚡️Трафик: <b>{used_traffic_gb}</b> из <b>{limit_traffic_gb}</b> Гб\n"
        )

    status_labels = {
        SubStatus.ACTIVE: "🟢 активна",
        SubStatus.EXPIRED: "🔴 истекла",
    }
    status_text = status_labels.get(sub.status, sub.status.value)

    billing_labels = {"tariff": "тариф", "balance": "баланс", "demo": "демо"}
    billing_text = billing_labels.get(sub.billing_mode, sub.billing_mode)

    if is_adapt:
        device_slots = sub.device_slots or 1
    else:
        device_slots = max(sub.device_slots or 0, included_slots)
    
    started = _format_dt_msk(sub.started_at, include_time=True)
    expires = _format_dt_msk(sub.expires_at, include_time=True)
    duration = f"{sub.tariff_days} дн." if sub.tariff_days else (f"{sub.tariff_months} мес." if sub.tariff_months else "—")
    key_block = _format_subscription_key_block(sub)
    if getattr(sub, "billing_mode", None) == "demo":
        tariff_name = "Демо-доступ"
    else:
        tariff_name = sub.tariff.label if sub.tariff else "—"

    if is_adapt:
        provider_line = "Провайдер: <b>Adapt</b>\n"
        client_line = f"ID Adapt: <code>{adapt_uuid}</code>\n" if adapt_uuid else ""
        server_line = ""
    elif sub.client_name and sub.client_name.startswith("vhq_"):
        provider_line = "Провайдер: <b>VHQ</b>\n"
        client_line = f"Клиент VHQ: <code>{html.escape(sub.client_name)}</code>\n"
        server_line = f"Сервер: {srv_emoji} {srv_name}\n"
    else:
        provider_line = ""
        client_line = f"Клиент Marzban: <code>{html.escape(sub.client_name)}</code>\n"
        server_line = f"Сервер: {srv_emoji} {srv_name}\n"

    text = (
        f"📊 <b>Подписка #{sub.id}</b>  [{page}/{total_items}]\n"
        f"Клиент: <code>{telegram_id}</code>\n\n"
        f"Статус: {status_text}\n"
        f"Тариф: <b>{tariff_name}</b>\n"
        f"Тип оплаты: <b>{billing_text}</b>\n"
        f"{provider_line}"
        f"Платформа: {sub.platform.value if sub.platform else '—'}\n"
        f"{server_line}"
        f"Начало: {started}\n"
        f"Истекает: <b>{expires}</b>\n"
        f"Срок тарифа: {duration}\n"
        f"Устройств: {device_slots}\n"
        f"{extra_adapt}"
        f"{client_line}\n"
        f"{key_block}"
    )

    kb = InlineKeyboardBuilder()
    nav_buttons = []
    if page > 1:
        nav_buttons.append(InlineKeyboardButton(text="⬅️", callback_data=f"adm_usr_subs_{telegram_id}_{page-1}"))
    nav_buttons.append(InlineKeyboardButton(text=f"{page}/{total_items}", callback_data="ignore"))
    if page < total_items:
        nav_buttons.append(InlineKeyboardButton(text="➡️", callback_data=f"adm_usr_subs_{telegram_id}_{page+1}"))
    kb.row(*nav_buttons)
    kb.row(InlineKeyboardButton(text="◀️ К клиенту", callback_data=f"adm_usr_refresh_{telegram_id}"))

    try:
        await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception as e:
        if "message is not modified" not in str(e):
            await callback.message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")

    await callback.answer()


# ── Admin User Devices ────────────────────────────────

@router.callback_query(F.data.startswith("adm_usr_devices_"))
async def admin_user_devices(
    callback: CallbackQuery,
    *,
    acknowledge: bool = True,
    telegram_id: int | None = None,
    subscription_id: int | None = None,
) -> None:
    if not _is_admin(callback.from_user.id):
        return

    if acknowledge:
        await callback.answer("Загружаем устройства…")

    selected_subscription_id = subscription_id
    if telegram_id is None:
        target_parts = callback.data.removeprefix("adm_usr_devices_").split("_", 1)
        try:
            telegram_id = int(target_parts[0])
            selected_subscription_id = int(target_parts[1]) if len(target_parts) > 1 else None
        except ValueError:
            await callback.message.answer("Не удалось определить клиента или подписку.")
            return

    async with async_session() as session:
        result = await session.execute(
            select(User)
            .options(
                selectinload(User.subscriptions).selectinload(Subscription.server),
                selectinload(User.subscriptions).selectinload(Subscription.tariff),
            )
            .where(User.telegram_id == telegram_id)
        )
        user = result.scalar_one_or_none()
        if not user:
            await callback.message.answer("Клиент не найден.")
            return

        active_subs = [s for s in user.subscriptions if s.status == SubStatus.ACTIVE]
        manageable_subs = active_subs
        if selected_subscription_id is None and len(manageable_subs) > 1:
            kb = InlineKeyboardBuilder()
            for sub in manageable_subs:
                label = sub.tariff.label if sub.tariff else f"Подписка #{sub.id}"
                expires = _format_dt_msk(sub.expires_at, include_time=False)
                kb.row(InlineKeyboardButton(
                    text=f"{label} · до {expires}",
                    callback_data=f"adm_usr_devices_{telegram_id}_{sub.id}",
                ))
            kb.row(InlineKeyboardButton(text="◀️ Назад к карточке", callback_data=f"adm_usr_refresh_{telegram_id}"))
            await callback.message.edit_text(
                "🖥 <b>Выберите подписку</b>\n\nУстройства каждой ссылки управляются отдельно.",
                reply_markup=kb.as_markup(),
                parse_mode="HTML",
            )
            return

        subscription = next(
            (sub for sub in manageable_subs if sub.id == selected_subscription_id),
            manageable_subs[0] if len(manageable_subs) == 1 else None,
        )

        if not subscription:
            await callback.message.answer("У этого пользователя нет активной подписки.")
            return

        if is_vhq_subscription(subscription):
            expires_str = _format_dt_msk(subscription.expires_at, include_time=True)
            tariff_name = subscription.tariff.label if subscription.tariff else "Ваш тариф"
            text = (
                "🖥 <b>Управление устройствами (Админ)</b>\n"
                f"Клиент: <code>{telegram_id}</code>\n"
                f"Тариф: <b>{html.escape(tariff_name)}</b>\n\n"
                f"📅 Подписка до: <b>{expires_str}</b>\n\n"
                "Отдельные данные об устройствах для этой подписки недоступны."
            )
            kb = InlineKeyboardBuilder()
            kb.row(InlineKeyboardButton(text="◀️ Назад к карточке", callback_data=f"adm_usr_refresh_{telegram_id}"))
            await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
            return

        if is_adapt_subscription(subscription):
            adapt_uuid = get_adapt_uuid_from_subscription(subscription)
            if not adapt_uuid:
                await callback.message.answer("Не удалось загрузить устройства. Проверьте служебные данные подписки.")
                return

            from bot.services.adapt_api import AdaptAPI
            api = AdaptAPI()
            devices_result, status_result = await asyncio.gather(
                api.get_devices(adapt_uuid),
                api.get_status(adapt_uuid),
                return_exceptions=True,
            )
            if isinstance(devices_result, Exception):
                exc = devices_result
                logger.error("Failed to get device list for sub %s: %s", subscription.id, exc)
                await callback.message.answer("Не удалось получить список устройств. Попробуйте позже.")
                return
            devices = devices_result

            if isinstance(status_result, dict) and status_result.get("devices") is not None:
                try:
                    provider_limit = max(1, int(status_result["devices"]))
                    if subscription.device_slots != provider_limit:
                        subscription.device_slots = provider_limit
                        await session.commit()
                except (TypeError, ValueError):
                    logger.warning("Invalid device limit for sub %s: %r", subscription.id, status_result.get("devices"))
            elif isinstance(status_result, Exception):
                logger.warning("Failed to refresh device limit for sub %s: %s", subscription.id, status_result)

            used_slots = len(devices)
            limit = subscription.device_slots or 1
            expires_str = _format_dt_msk(subscription.expires_at, include_time=True)
            tariff_name = subscription.tariff.label if subscription.tariff else "Ваш тариф"

            text = (
                f"🖥 <b>Управление устройствами (Админ)</b>\n"
                f"Клиент: <code>{telegram_id}</code>\n"
                f"Тариф: <b>{html.escape(tariff_name)}</b>\n\n"
                f"📅 Подписка до: <b>{expires_str}</b>\n"
                f"Подключено устройств: <b>{used_slots}</b> из <b>{limit}</b>\n"
            )

            if not devices:
                text += "\nУстройства пока не подключены."
            else:
                for idx, dev in enumerate(devices, 1):
                    info = adapt_device_details(dev, idx)
                    text += f"\n\n🖥 <b>{idx}. {html.escape(info['name'])}</b>"
                    if info["model"] and info["model"] != info["name"]:
                        text += f"\n📱 Модель: <b>{html.escape(info['model'])}</b>"
                    text += (
                        f"\n💻 ОС: <b>{html.escape(info['os'])}</b>"
                        f"\n🕓 Последняя активность: <b>{info['last_activity']}</b>"
                        f"\n🌐 Последний IP: <code>{html.escape(info['ip'])}</code>"
                    )

            # Let's build a keyboard allowing the admin to delete any of these devices
            kb = InlineKeyboardBuilder()
            for dev in devices:
                dev_id = dev.get("id") or dev.get("device_id")
                dev_name = dev.get("name") or dev.get("client_name") or f"Устройство {dev_id}"
                if dev_id is not None:
                    kb.row(InlineKeyboardButton(text=f"❌ Кикнуть {dev_name}", callback_data=f"adm_del_dev_{subscription.id}_{dev_id}_{telegram_id}"))

            kb.row(InlineKeyboardButton(text="◀️ Назад к карточке", callback_data=f"adm_usr_refresh_{telegram_id}"))

            await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
            return

        # non-adapt logic (Marzban)
        proxy_result = await session.execute(
            select(ProxyAccount)
            .options(selectinload(ProxyAccount.server))
            .where(ProxyAccount.subscription_id == subscription.id)
        )
        proxies = proxy_result.scalars().all()
        activity = await asyncio.gather(*(
            vpn_manager.get_key_activity(proxy.server, proxy.marzban_username)
            for proxy in proxies
        ))

        expires_str = _format_dt_msk(subscription.expires_at, include_time=True)
        tariff_name = subscription.tariff.label if subscription.tariff else "Лайт"
        text = (
            f"🖥 <b>Управление устройствами (Админ)</b>\n"
            f"Клиент: <code>{telegram_id}</code>\n"
            f"Тариф: <b>{html.escape(tariff_name)}</b>\n\n"
            f"📅 Подписка до: <b>{expires_str}</b>\n"
            f"Доступно устройств: <b>{subscription.device_slots}</b>\n\n"
            f"Данные подключения:\n"
        )
        if not proxies:
            text += "\nАктивный ключ не найден."
        for index, (proxy, key_info) in enumerate(zip(proxies, activity), 1):
            location = proxy.server.location or proxy.server.name
            os_label = describe_user_agent(key_info.get("sub_last_user_agent"))
            last_activity = format_activity(key_info.get("online_at"))
            text += (
                f"\n🔑 <b>{index}. {html.escape(location)}</b>"
                f"\n💻 ОС последнего клиента: <b>{html.escape(os_label)}</b>"
                f"\n🕓 Последняя активность: <b>{last_activity}</b>"
                f"\n🔗 <code>{html.escape(proxy.sub_url)}</code>\n"
            )

        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="◀️ Назад к карточке", callback_data=f"adm_usr_refresh_{telegram_id}"))
        await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")


@router.callback_query(F.data.startswith("adm_del_dev_"))
async def admin_delete_device(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    parts = callback.data.split("_")
    sub_id = int(parts[3])
    device_id = parts[4]
    telegram_id = int(parts[5])

    async with async_session() as session:
        sub = await session.get(Subscription, sub_id)
        if not sub:
            await callback.answer("Подписка не найдена.", show_alert=True)
            return
        adapt_uuid = get_adapt_uuid_from_subscription(sub)
        if not adapt_uuid:
            await callback.answer("Не найден UUID подписки Adapt.", show_alert=True)
            return

    try:
        from bot.services.adapt_api import AdaptAPI
        success = await AdaptAPI().delete_device(adapt_uuid, int(device_id))
        if success:
            await write_admin_audit(
                actor_telegram_id=callback.from_user.id,
                action="adapt_device_deleted",
                entity_type="subscription",
                entity_id=str(sub_id),
                target_telegram_id=telegram_id,
                summary="Устройство удалено",
                details=f"ID устройства: {device_id}",
            )
            await callback.answer("Устройство удалено!", show_alert=True)
        else:
            await callback.answer("Не удалось удалить устройство (API вернул false).", show_alert=True)
    except Exception as exc:
        logger.error(f"Failed to delete adapt device {device_id} for sub {sub_id} by admin: {exc}")
        await callback.answer("Не удалось удалить устройство. Попробуйте позже.", show_alert=True)

    # Refresh the device list page for admin!
    await admin_user_devices(
        callback,
        acknowledge=False,
        telegram_id=telegram_id,
        subscription_id=sub_id,
    )


# ── User Payments List ────────────────────────────────

@router.callback_query(F.data.regexp(r"^adm_usr_pays_(\d+)_(\d+)$"))
async def admin_user_payments(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return

    import re as _re
    m = _re.match(r"^adm_usr_pays_(\d+)_(\d+)$", callback.data)
    telegram_id = int(m.group(1))
    page = max(1, int(m.group(2)))

    async with async_session() as session:
        result = await session.execute(
            select(User)
            .options(selectinload(User.payments))
            .where(User.telegram_id == telegram_id)
        )
        user = result.scalar_one_or_none()

    if not user:
        await callback.answer("Пользователь не найден", show_alert=True)
        return

    payments = sorted(user.payments, key=lambda p: p.created_at, reverse=True)
    total = len(payments)

    if not payments:
        await callback.answer("Оплат нет", show_alert=True)
        return

    page_size = 5
    total_pages = max(1, (total + page_size - 1) // page_size)
    if page > total_pages:
        page = total_pages

    start = (page - 1) * page_size
    page_pays = payments[start : start + page_size]

    method_labels = {
        "stars": "⭐ Stars",
        "yookassa": "💳 YooKassa",
        "robokassa": "💳 Robokassa",
        "balance": "💰 Баланс",
        "manual": "🔑 Вручную",
    }
    status_labels = {
        "completed": "✅",
        "pending": "⏳",
        "failed": "❌",
        "refunded": "↩️",
    }

    lines = [f"💳 <b>Оплаты клиента</b> <code>{telegram_id}</code>  [{page}/{total_pages}]\n"]
    for p in page_pays:
        s = status_labels.get(p.status.value if hasattr(p.status, "value") else str(p.status), "?")
        method = method_labels.get(
            p.method.value if hasattr(p.method, "value") else str(p.method),
            p.method.value if hasattr(p.method, "value") else str(p.method),
        )
        if p.currency == "RUB":
            amount_str = f"{p.amount / 100:.2f} ₽"
        elif p.currency == "XTR":
            amount_str = f"{p.amount} ⭐"
        else:
            amount_str = f"{p.amount} {p.currency}"

        discount_str = f" (скидка {p.discount_applied:.0f}%)" if p.discount_applied else ""
        order_str = f"\n   Заказ: <code>{html.escape(p.provider_payment_id)}</code>" if p.provider_payment_id else ""
        lines.append(
            f"{s} #{p.id} | {_format_dt_msk(p.created_at)}\n"
            f"   {method} | <b>{amount_str}</b>{discount_str}{order_str}"
        )

    text = "\n\n".join(lines)
    kb = InlineKeyboardBuilder()
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"adm_usr_pays_{telegram_id}_{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="ignore"))
    if page < total_pages:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"adm_usr_pays_{telegram_id}_{page+1}"))
    if nav:
        kb.row(*nav)
    kb.row(InlineKeyboardButton(text="◀️ К клиенту", callback_data=f"adm_usr_refresh_{telegram_id}"))

    try:
        await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception as e:
        if "message is not modified" not in str(e):
            await callback.message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")

    await callback.answer()


# ── User Feedback Conversations ────────────────────────

def _format_dt_thread(dt: datetime | None) -> str:
    if not dt:
        return "—"
    from datetime import timezone, timedelta
    msk = timezone(timedelta(hours=3))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(msk).strftime("%d.%m %H:%M")


async def _is_feedback_admin(user_id: int) -> bool:
    if _is_admin(user_id):
        return True
    try:
        from bot.handlers.start import _get_feedback_admin_ids
        admin_ids = await _get_feedback_admin_ids()
        return user_id in admin_ids
    except Exception:
        return False


@router.callback_query(F.data.regexp(r"^adm_usr_fb_(\d+)_(\d+)$"))
async def admin_user_feedbacks(callback: CallbackQuery) -> None:
    if not await _is_feedback_admin(callback.from_user.id):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    m = re.match(r"^adm_usr_fb_(\d+)_(\d+)$", callback.data)
    if not m:
        await callback.answer("Некорректный запрос", show_alert=True)
        return

    telegram_id = int(m.group(1))
    page = max(1, int(m.group(2)))

    async with async_session() as session:
        total = await session.scalar(
            select(func.count(Feedback.id)).where(Feedback.telegram_id == telegram_id)
        ) or 0

        if total == 0:
            await callback.answer("У пользователя нет обращений", show_alert=True)
            return

        page_size = 10
        total_pages = max(1, (total + page_size - 1) // page_size)
        if page > total_pages:
            page = total_pages

        offset = (page - 1) * page_size
        stmt = (
            select(Feedback)
            .where(Feedback.telegram_id == telegram_id)
            .order_by(Feedback.created_at.desc(), Feedback.id.desc())
            .offset(offset)
            .limit(page_size)
        )
        feedbacks = (await session.scalars(stmt)).all()

    kb = InlineKeyboardBuilder()
    for fb in feedbacks:
        dt_str = _format_dt_thread(fb.created_at)
        media_marker = "📷 " if fb.media_type == "photo" else ("🎥 " if fb.media_type == "video" else ("📄 " if fb.media_type == "document" else ""))
        raw_text = (fb.text or "").strip().replace("\n", " ")
        preview = (raw_text[:20] + "…") if len(raw_text) > 20 else raw_text
        if not preview and media_marker:
            preview = "Медиа"
        btn_text = f"#{fb.id} · {dt_str} · {media_marker}{preview}".strip()
        if len(btn_text) > 55:
            btn_text = btn_text[:54] + "…"
        kb.row(InlineKeyboardButton(text=btn_text, callback_data=f"adm_fb_view_{fb.id}_{page}"))

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"adm_usr_fb_{telegram_id}_{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="ignore"))
    if page < total_pages:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"adm_usr_fb_{telegram_id}_{page+1}"))
    if nav:
        kb.row(*nav)
    kb.row(InlineKeyboardButton(text="◀️ К клиенту", callback_data=f"adm_usr_refresh_{telegram_id}"))

    text = (
        f"💬 <b>Обращения клиента</b> <code>{telegram_id}</code>  [{page}/{total_pages}]\n\n"
        f"Всего обращений: <b>{total}</b>\n"
        f"Выберите обращение для просмотра переписки:"
    )

    try:
        await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception as e:
        if "message is not modified" not in str(e):
            await callback.message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.regexp(r"^adm_fb_view_(\d+)(?:_(\d+))?$"))
async def admin_feedback_view_thread(callback: CallbackQuery) -> None:
    if not await _is_feedback_admin(callback.from_user.id):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    m = re.match(r"^adm_fb_view_(\d+)(?:_(\d+))?$", callback.data)
    if not m:
        await callback.answer("Некорректный запрос", show_alert=True)
        return

    feedback_id = int(m.group(1))
    page = int(m.group(2)) if m.group(2) else 1

    async with async_session() as session:
        result = await session.execute(
            select(Feedback)
            .options(selectinload(Feedback.messages))
            .where(Feedback.id == feedback_id)
        )
        fb = result.scalar_one_or_none()

    if not fb:
        await callback.answer("Обращение не найдено", show_alert=True)
        return

    header = f"💬 <b>Обращение #{fb.id}</b> · ID: <code>{fb.telegram_id}</code>\n{'─' * 28}\n\n"

    def _fmt_block(title: str, text_val: str | None, media: str | None) -> str:
        escaped = html.escape(text_val.strip()) if text_val and text_val.strip() else ""
        if media == "photo":
            content = f"📷 Фото\n{escaped}" if escaped else "📷 Фото"
        elif media == "video":
            content = f"🎥 Видео\n{escaped}" if escaped else "🎥 Видео"
        elif media == "document":
            content = f"📄 Документ\n{escaped}" if escaped else "📄 Документ"
        else:
            content = escaped or "—"
        return f"{title}\n{content}"

    blocks = []
    blocks.append(_fmt_block(f"👤 Клиент · {_format_dt_thread(fb.created_at)}", fb.text, fb.media_type))
    for msg in fb.messages:
        title = f"👤 Клиент · {_format_dt_thread(msg.created_at)}" if msg.sender_type == "user" else f"🛠 Поддержка · {_format_dt_thread(msg.created_at)}"
        blocks.append(_fmt_block(title, msg.text, msg.media_type))

    full_body = "\n\n".join(blocks)
    max_allowed = 3800
    if len(header) + len(full_body) <= max_allowed:
        text = header + full_body
    else:
        prefix = "<i>... (показаны последние сообщения, более ранние скрыты)</i>\n\n"
        budget = max_allowed - len(header) - len(prefix)
        selected_blocks = []
        accumulated_len = 0
        for block in reversed(blocks):
            needed = len(block) + (2 if selected_blocks else 0)
            if accumulated_len + needed > budget and selected_blocks:
                break
            selected_blocks.append(block)
            accumulated_len += needed
        selected_blocks.reverse()
        text = header + prefix + "\n\n".join(selected_blocks)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Ответить", callback_data=f"feedback_admin_reply_{fb.id}")],
        [InlineKeyboardButton(text="◀️ К обращениям", callback_data=f"adm_usr_fb_{fb.telegram_id}_{page}")],
    ])

    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        if "message is not modified" not in str(e):
            await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("feedback_admin_reply_"))
async def feedback_admin_reply_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _is_feedback_admin(callback.from_user.id):
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    raw_id = callback.data.removeprefix("feedback_admin_reply_")
    try:
        feedback_id = int(raw_id)
    except ValueError:
        await callback.answer("Обращение не найдено", show_alert=True)
        return

    async with async_session() as session:
        fb = await session.get(Feedback, feedback_id)

    if not fb:
        await callback.answer("Обращение не найдено", show_alert=True)
        return

    await state.set_state(AdminStates.waiting_feedback_reply)
    await state.update_data(admin_reply_feedback_id=feedback_id)

    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀️ Отмена", callback_data="adm_feedback_reply_cancel"),
    ]])
    await callback.message.answer(
        f"Введите ответ по обращению #{feedback_id}",
        reply_markup=cancel_kb,
    )
    await callback.answer()


@router.callback_query(F.data == "adm_feedback_reply_cancel")
async def feedback_admin_reply_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    try:
        await callback.message.edit_text("Ответ отменён.")
    except Exception:
        pass
    await callback.answer()


@router.message(AdminStates.waiting_feedback_reply)
async def feedback_admin_reply_receive(message: Message, state: FSMContext) -> None:
    text = message.text or message.caption or ""
    media_file_id = None
    media_type = None

    if getattr(message, "photo", None):
        media_file_id = message.photo[-1].file_id
        media_type = "photo"
    elif getattr(message, "video", None):
        media_file_id = message.video.file_id
        media_type = "video"
    elif getattr(message, "document", None):
        media_file_id = message.document.file_id
        media_type = "document"

    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀️ Отмена", callback_data="adm_feedback_reply_cancel"),
    ]])

    if not text and not media_file_id:
        await message.answer("❌ Отправьте текст, фото, видео или скриншот файлом.", reply_markup=cancel_kb)
        return

    data = await state.get_data()
    feedback_id = data.get("admin_reply_feedback_id")
    if not feedback_id:
        await message.answer("❌ Сессия ответа устарела.")
        await state.clear()
        return

    async with async_session() as session:
        fb = await session.get(Feedback, feedback_id)
        if not fb:
            await message.answer("❌ Обращение не найдено.")
            await state.clear()
            return

        reply_msg = FeedbackMessage(
            feedback_id=feedback_id,
            sender_type="admin",
            sender_telegram_id=message.from_user.id,
            text=text or None,
            media_file_id=media_file_id,
            media_type=media_type,
        )
        session.add(reply_msg)
        await session.commit()
        client_telegram_id = fb.telegram_id

    await state.clear()

    client_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="↩️ Ответить", callback_data=f"feedback_reply_{feedback_id}"),
    ]])
    client_header = (
        f"💬 <b>Ответ поддержки</b>\n"
        f"Обращение #{feedback_id}\n\n"
    )
    escaped_text = html.escape(text) if text else ""
    client_caption = f"{client_header}{escaped_text}" if escaped_text else client_header.rstrip()
    if media_file_id and len(client_caption) > 1024:
        client_caption = client_caption[:1020] + "..."

    try:
        if media_file_id and media_type:
            if media_type == "photo":
                send_fn = message.bot.send_photo
            elif media_type == "video":
                send_fn = message.bot.send_video
            else:
                send_fn = message.bot.send_document
            await send_fn(
                client_telegram_id,
                media_file_id,
                caption=client_caption,
                reply_markup=client_kb,
                parse_mode="HTML",
            )
        else:
            await message.bot.send_message(
                client_telegram_id,
                client_caption,
                reply_markup=client_kb,
                parse_mode="HTML",
            )
        await message.answer(f"✅ Ответ по обращению #{feedback_id} успешно отправлен клиенту.")
    except Exception as e:
        logger.error("Failed to deliver admin reply for feedback %s to user %s: %s", feedback_id, client_telegram_id, e)
        await message.answer(f"⚠️ Ошибка при отправке сообщения клиенту: {e}")


# ── Manual Key Generation ─────────────────────────────

@router.callback_query(F.data == "adm_gen_key")
async def admin_gen_key_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await callback.message.edit_text(
        "<b>Выдать ключ вручную</b>\n\nОтправьте Telegram ID пользователя:",
        reply_markup=admin_back_kb(),
        parse_mode="HTML",
    )
    await state.set_state(AdminStates.waiting_manual_key_user)
    await callback.answer()


@router.message(AdminStates.waiting_manual_key_user, F.text)
async def admin_gen_key_user(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    try:
        telegram_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введите числовой Telegram ID")
        return

    await state.update_data(
        manual_key_user=telegram_id,
        manual_key_back_callback="adm_back",
    )

    async with async_session() as session:
        result = await session.execute(
            select(User)
            .options(selectinload(User.subscriptions).selectinload(Subscription.tariff))
            .where(User.telegram_id == telegram_id)
        )
        user = result.scalar_one_or_none()
    if not user:
        await message.answer("❌ Пользователь не найден")
        return

    async with async_session() as session:
        adapt_records = {
            record.subscription_id: record
            for record in (
                await session.execute(
                    select(AdaptSubscription)
                    .join(Subscription, Subscription.id == AdaptSubscription.subscription_id)
                    .where(Subscription.user_id == user.id)
                )
            ).scalars().all()
        }
        canonical_uuids = {
            str(record.adapt_plan_uuid).strip()
            for record in adapt_records.values()
            if str(record.adapt_plan_uuid or "").strip()
        }
        adapt_tariffs = {}
        if canonical_uuids:
            for tariff in (
                await session.execute(
                    select(Tariff)
                    .where(Tariff.adapt_plan_uuid.in_(canonical_uuids))
                    .order_by(Tariff.id.desc())
                )
            ).scalars().all():
                adapt_tariffs.setdefault(str(tariff.adapt_plan_uuid).strip(), tariff)
    user_subscriptions = [
        sub for sub in user.subscriptions
        if (
            not is_adapt_subscription(sub)
            and sub.tariff
        ) or (
            is_adapt_subscription(sub)
            and (
                adapt_records.get(sub.id)
                or (not adapt_records.get(sub.id) and sub.tariff)
            )
        )
    ]
    kb = InlineKeyboardBuilder()
    for sub in sorted(user_subscriptions, key=lambda item: (item.expires_at, item.id), reverse=True):
        expires = _format_dt_msk(sub.expires_at, include_time=False)
        adapt_record = adapt_records.get(sub.id)
        current_tariff = (
            adapt_tariffs.get(str(adapt_record.adapt_plan_uuid or "").strip())
            if adapt_record and str(adapt_record.adapt_plan_uuid or "").strip()
            else sub.tariff
        )
        is_trial = is_adapt_trial_subscription(sub, adapt_sub=adapt_record)
        is_expired = bool(
            (sub.expires_at and sub.expires_at <= datetime.utcnow())
            or (hasattr(sub, "status") and getattr(sub.status, "value", str(sub.status)) == "expired")
        )
        if is_expired:
            kb.row(InlineKeyboardButton(
                text=f"↻ Продлить #{sub.id} · {current_tariff.label} (истекла)",
                callback_data=f"adm_key_action_renew_{sub.id}",
            ))
        elif not is_trial:
            kb.row(InlineKeyboardButton(
                text=f"↻ Продлить #{sub.id} · {current_tariff.label} · до {expires}",
                callback_data=f"adm_key_action_renew_{sub.id}",
            ))
        if is_adapt_subscription(sub) and not is_expired:
            label_text = (
                f"↑ Улучшить #{sub.id} (тестовый) · до {expires}"
                if is_trial
                else f"↑ Улучшить #{sub.id} · {current_tariff.label}"
            )
            kb.row(InlineKeyboardButton(
                text=label_text,
                callback_data=f"adm_key_action_upgrade_{sub.id}",
            ))
    kb.row(InlineKeyboardButton(text="➕ Создать новую подписку", callback_data="adm_key_action_new_0"))
    kb.row(InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back"))

    await message.answer(
        f"Ключ для <code>{telegram_id}</code>\n\nВыберите действие и подписку:",
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )
    await state.set_state(AdminStates.waiting_manual_key_tariff)


@router.callback_query(AdminStates.waiting_manual_key_tariff, F.data.startswith("adm_key_action_"))
async def admin_gen_key_action(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    payload = callback.data.removeprefix("adm_key_action_")
    action, raw_subscription_id = payload.rsplit("_", 1)
    target_subscription_id = int(raw_subscription_id) or None
    state_data = await state.get_data()
    telegram_id = int(state_data["manual_key_user"])
    back_callback = (
        f"adm_usr_key_{telegram_id}"
        if str(state_data.get("manual_key_back_callback", "")).startswith("adm_usr_")
        else state_data.get("manual_key_back_callback", "adm_back")
    )

    async with async_session() as session:
        target = await session.scalar(
            select(Subscription)
            .options(selectinload(Subscription.tariff))
            .where(Subscription.id == target_subscription_id)
            .where(Subscription.user.has(telegram_id=telegram_id))
        ) if target_subscription_id else None
        target_adapt_record = await session.scalar(
            select(AdaptSubscription)
            .where(AdaptSubscription.subscription_id == target.id)
            .limit(1)
        ) if target else None
        target_tariff = None
        if target_adapt_record and target_adapt_record.adapt_plan_uuid:
            target_tariff = await session.scalar(
                select(Tariff)
                .where(Tariff.adapt_plan_uuid == target_adapt_record.adapt_plan_uuid)
                .order_by(Tariff.id.desc())
                .limit(1)
            )
        if not str(getattr(target_adapt_record, "adapt_plan_uuid", None) or "").strip():
            target_tariff = target.tariff if target else None
        if action != "new" and (not target or not target_tariff):
            await callback.answer("Подписка или её тариф не найдены", show_alert=True)
            return
        query = select(Tariff).where(Tariff.is_active == True)  # noqa: E712
        is_target_expired = bool(
            target
            and (
                (target.expires_at and target.expires_at <= datetime.utcnow())
                or (hasattr(target, "status") and getattr(target.status, "value", str(target.status)) == "expired")
            )
        )
        if action == "renew":
            if is_target_expired:
                if is_adapt_subscription(target):
                    query = query.where(Tariff.adapt_plan_uuid.is_not(None))
                elif is_vhq_subscription(target):
                    query = query.where(Tariff.adapt_plan_uuid.is_(None), Tariff.vhq_tier.is_not(None))
                else:
                    query = query.where(Tariff.adapt_plan_uuid.is_(None), Tariff.vhq_tier.is_(None))
                candidate_tariffs = (await session.execute(query.order_by(Tariff.price_rub))).scalars().all()
                tariffs = [t for t in candidate_tariffs if int(t.price_rub or 0) > 0 and not is_adapt_trial_tariff(t)]
            elif is_adapt_trial_subscription(target, adapt_sub=target_adapt_record):
                await callback.answer(
                    "Тестовую подписку нельзя продлить тем же тарифом. Выберите «Улучшить» для перехода на платный тариф.",
                    show_alert=True,
                )
                return
            else:
                query = query.where(Tariff.id == target_tariff.id)
                tariffs = (await session.execute(query.order_by(Tariff.price_rub))).scalars().all()
        elif action == "upgrade":
            if not is_adapt_subscription(target) or not target_tariff.adapt_plan_uuid:
                await callback.answer("Улучшение доступно только для Adapt", show_alert=True)
                return
            query = query.where(Tariff.adapt_plan_uuid.is_not(None))
            is_trial = is_adapt_trial_subscription(target, adapt_sub=target_adapt_record)
            candidate_tariffs = (await session.execute(query.order_by(Tariff.price_rub))).scalars().all()
            tariffs = []
            for tariff in candidate_tariffs:
                if int(tariff.price_rub or 0) <= 0 or is_adapt_trial_tariff(tariff):
                    continue
                if is_trial:
                    if is_adapt_customer_upgrade_eligible(
                        target_tariff,
                        tariff,
                        expires_at=target.expires_at,
                        is_demo=True,
                    ):
                        tariffs.append(tariff)
                else:
                    validation_plans = await get_paid_adapt_upgrade_plans(
                        session,
                        target_tariff,
                        tariff,
                    )
                    if validation_plans and is_adapt_customer_upgrade_eligible(
                        validation_plans[0],
                        validation_plans[1],
                        expires_at=target.expires_at,
                        is_demo=False,
                    ):
                        tariffs.append(tariff)
        else:
            tariffs = (await session.execute(query.order_by(Tariff.price_rub))).scalars().all()

        op_id = None
        if action == "renew" and target and is_vhq_subscription(target):
            tariffs = [t for t in tariffs if t.vhq_tier == "basic" and t.days == 30]
            if not tariffs:
                await callback.answer("Подходящих тарифов нет", show_alert=True)
                return
            target_user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
            op = ManualSubscriptionOperation(
                operation_uuid=str(uuid.uuid4()),
                admin_user_id=callback.from_user.id,
                target_user_id=target_user.id if target_user else 0,
                subscription_id=target.id if target else None,
                tariff_id=None,
                action=action,
                status="draft",
            )
            session.add(op)
            await session.commit()
            op_id = op.id

        if not tariffs:
            await callback.answer("Подходящих тарифов нет", show_alert=True)
            return

    await state.update_data(
        manual_purchase_action=action,
        manual_target_subscription_id=target_subscription_id,
        manual_operation_id=op_id,
    )
    kb = _build_manual_key_tariff_kb(tariffs, back_callback=back_callback, operation_id=op_id)
    await callback.message.edit_text(
        "Выберите тариф для продления:" if action == "renew" else (
            "Выберите более дорогой тариф:" if action == "upgrade" else "Выберите тариф для новой подписки:"
        ),
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


@router.callback_query(AdminStates.waiting_manual_key_tariff, F.data.startswith("adm_key_tariff_"))
async def admin_gen_key_tariff(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    raw_payload = callback.data.removeprefix("adm_key_tariff_")
    if "_" in raw_payload:
        op_id_str, tariff_id_str = raw_payload.split("_", 1)
        operation_id = int(op_id_str)
        tariff_id = int(tariff_id_str)
    else:
        operation_id = None
        tariff_id = int(raw_payload)

    data = await state.get_data()
    telegram_id = data.get("manual_key_user")
    if operation_id is None:
        operation_id = data.get("manual_operation_id")
    purchase_action = data.get("manual_purchase_action", "new")
    target_subscription_id = data.get("manual_target_subscription_id")
    await state.clear()
    logger.info(
        "Manual key issuance requested: admin_id=%s target_telegram_id=%s tariff_id=%s op_id=%s",
        callback.from_user.id,
        telegram_id,
        tariff_id,
        operation_id,
    )

    from bot.services.subscription_service import (
        create_mtproto_subscription,
        create_or_extend_paid_access,
        get_primary_active_server,
    )
    from bot.services.provisioning_issues import AccessProvisionError

    async with async_session() as session:
        manual_op = None
        if operation_id:
            op = await session.get(ManualSubscriptionOperation, operation_id)
            if not op:
                await callback.answer("Операция не найдена.", show_alert=True)
                return

            if op.admin_user_id != callback.from_user.id:
                await callback.answer("Операция принадлежит другому администратору.", show_alert=True)
                return

            if op.status != "draft":
                await callback.answer("Операция уже выполняется или выполнена.", show_alert=True)
                return

            if op.action != "renew":
                await callback.answer("Недопустимое действие операции.", show_alert=True)
                return

            if not op.subscription_id or not op.target_user_id:
                await callback.answer("Некорректные параметры операции.", show_alert=True)
                return

            sub = await session.get(Subscription, op.subscription_id)
            if not sub or sub.id != op.subscription_id or sub.user_id != op.target_user_id:
                await callback.answer("Несоответствие целевой подписки.", show_alert=True)
                return

            is_vhq = bool(sub.client_name.startswith("vhq_") or sub.vhq_order_id)
            if not is_vhq:
                await callback.answer("Подписка не является VHQ.", show_alert=True)
                return

            val_tariff = await session.get(Tariff, tariff_id)
            if not val_tariff or val_tariff.vhq_tier != "basic" or val_tariff.days != 30:
                await callback.answer("Для продления VHQ разрешён только тариф basic на 30 дней.", show_alert=True)
                return

            cas_stmt = (
                update(ManualSubscriptionOperation)
                .where(
                    ManualSubscriptionOperation.id == operation_id,
                    ManualSubscriptionOperation.status == "draft",
                )
                .values(
                    status="applied",
                    tariff_id=tariff_id,
                    applied_at=datetime.utcnow(),
                )
            )
            cas_res = await session.execute(cas_stmt)
            if cas_res.rowcount == 0:
                await callback.answer("Операция уже выполняется или выполнена.", show_alert=True)
                return

            manual_op = op
            target_subscription_id = op.subscription_id
            purchase_action = op.action
            if not telegram_id and manual_op and manual_op.target_user_id:
                target_user = await session.get(User, manual_op.target_user_id)
                if target_user:
                    telegram_id = target_user.telegram_id

        tariff = await session.get(Tariff, tariff_id)
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()

        if not tariff:
            logger.warning("Manual key issuance failed: tariff not found tariff_id=%s", tariff_id)
            await callback.message.edit_text("❌ Тариф не найден.", reply_markup=admin_back_kb(), parse_mode="HTML")
            await callback.answer()
            return
        if not user:
            logger.warning("Manual key issuance failed: user not found telegram_id=%s", telegram_id)
            await callback.message.edit_text("❌ Пользователь не найден.", reply_markup=admin_back_kb(), parse_mode="HTML")
            await callback.answer()
            return

        is_tg_proxy_only = tariff.tariff_type == TariffType.TG_PROXY
        is_both = tariff.tariff_type == TariffType.BOTH
        platform = user.platform or Platform.ANDROID

        vpn_key = None
        proxy_link = None
        subscription = None

        # VPN part
        if not is_tg_proxy_only:
            try:
                if target_subscription_id and purchase_action == "renew":
                    target_sub = await session.get(Subscription, target_subscription_id)
                    if target_sub and is_adapt_subscription(target_sub):
                        target_adapt = await session.scalar(
                            select(AdaptSubscription).where(AdaptSubscription.subscription_id == target_sub.id).limit(1)
                        )
                        curr_plan = str(getattr(target_adapt, "adapt_plan_uuid", None) or "").strip()
                        new_plan = str(tariff.adapt_plan_uuid or "").strip()
                        if curr_plan and new_plan and curr_plan != new_plan:
                            purchase_action = "upgrade"

                subscription, vpn_key = await create_or_extend_paid_access(
                    session, user=user, tariff=tariff, platform=platform,
                    purchase_action=purchase_action,
                    target_subscription_id=target_subscription_id,
                    manual_operation=manual_op,
                )
            except AccessProvisionError as issue:
                if issue.provider == "vhq" and issue.code == "supplier_fulfillment_pending":
                    logger.info(
                        "Manual key issuance VHQ fulfillment pending: admin_id=%s target_user_id=%s tariff_id=%s",
                        callback.from_user.id,
                        user.id,
                        tariff.id,
                    )
                    if manual_op:
                        manual_op.status = "applied"
                        await session.commit()
                    await callback.message.edit_text(
                        f"⏳ <b>Продление принято. VHQ: обработка</b>\n\n"
                        f"Пользователь: <code>{telegram_id}</code>\n"
                        f"Тариф: {tariff.label}\n\n"
                        f"Операция поставлена в очередь и ожидает подтверждения поставщика.",
                        reply_markup=admin_back_kb(),
                        parse_mode="HTML",
                    )
                    await callback.answer()
                    return

                await _notify_manual_provisioning_issue(
                    callback,
                    user=user,
                    subscription_id=target_subscription_id,
                    action=purchase_action,
                    issue=issue,
                )
                logger.error(
                    "Manual key issuance VPN/VHQ failed: admin_id=%s target_user_id=%s tariff_id=%s code=%s reason=%s",
                    callback.from_user.id,
                    user.id,
                    tariff.id,
                    issue.code,
                    safe_access_provision_reason(issue),
                )
                if manual_op:
                    fulfillment = await session.scalar(
                        select(VhqSubscriptionFulfillment)
                        .where(VhqSubscriptionFulfillment.manual_operation_id == manual_op.id)
                        .order_by(VhqSubscriptionFulfillment.id.desc())
                        .limit(1)
                    )
                    if fulfillment and fulfillment.status == "manual_review":
                        manual_op.status = "manual_review"
                    else:
                        manual_op.status = "failed"
                    await session.commit()
                await callback.message.edit_text(
                    "❌ Ручная операция не выполнена.\n\n"
                    f"Клиент: <code>{html.escape(str(user.telegram_id))}</code>\n"
                    f"Подписка: <code>{html.escape(str(target_subscription_id or 'новая'))}</code>\n"
                    f"Действие: <code>{html.escape(str(purchase_action or 'manual'))}</code>\n"
                    f"Провайдер: <code>{html.escape(issue.provider)}</code>\n"
                    f"Код: <code>{html.escape(issue.code)}</code>\n"
                    f"HTTP статус: <code>{html.escape(str(issue.status or 'неизвестен'))}</code>\n"
                    f"Причина: {html.escape(safe_access_provision_reason(issue))}\n"
                    f"Результат: {html.escape(describe_access_provision_result(issue))}\n"
                    f"Рекомендация: {html.escape(recommend_access_provision_action(issue))}",
                    reply_markup=admin_back_kb(),
                    parse_mode="HTML",
                )
                await callback.answer()
                return
            if not subscription:
                logger.error(
                    "Manual key issuance VPN failed: admin_id=%s target_user_id=%s tariff_id=%s",
                    callback.from_user.id,
                    user.id,
                    tariff.id,
                )
                if manual_op:
                    manual_op.status = "failed"
                    await session.commit()
                await callback.message.edit_text("❌ Ошибка генерации ключа.", reply_markup=admin_back_kb(), parse_mode="HTML")
                await callback.answer()
                return

        # MTProto part
        if is_tg_proxy_only or is_both:
            if is_tg_proxy_only:
                server = await get_primary_active_server(session)
                if not server:
                    await callback.message.edit_text("❌ Нет доступных серверов.", reply_markup=admin_back_kb(), parse_mode="HTML")
                    await callback.answer()
                    return
                expires_at = datetime.utcnow() + timedelta(days=tariff.days)
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
            )

        fulfillment = None
        if manual_op:
            fulfillment = await session.scalar(
                select(VhqSubscriptionFulfillment)
                .where(VhqSubscriptionFulfillment.manual_operation_id == manual_op.id)
                .order_by(VhqSubscriptionFulfillment.id.desc())
                .limit(1)
            )
            if not fulfillment or fulfillment.status == "completed":
                manual_op.status = "completed"

        await session.commit()
        logger.info(
            "Manual key issuance committed: admin_id=%s target_user_id=%s subscription_id=%s vpn=%s mtproto=%s",
            callback.from_user.id,
            user.id,
            subscription.id if subscription else None,
            bool(vpn_key),
            bool(proxy_link),
        )

        expires_str = _format_dt_msk(subscription.expires_at, include_time=False) if subscription else "N/A"

    is_vhq = tariff.vhq_tier or (subscription and subscription.client_name.startswith("vhq_"))

    # Confirm to admin
    type_labels = {
        TariffType.VPN: "Весь интернет",
        TariffType.TG_PROXY: "TG-ускоритель",
        TariffType.BOTH: "Весь интернет + TG",
    }
    if is_vhq and fulfillment and fulfillment.status != "completed":
        await callback.message.edit_text(
            f"⏳ <b>Продление отправлено провайдеру</b>\n\n"
            f"Пользователь: <code>{telegram_id}</code>\n"
            f"Тариф: {tariff.label}\n\n"
            f"Операция поставлена в очередь и ожидает подтверждения поставщика.",
            reply_markup=admin_back_kb(),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    header_text = "VPN успешно продлен" if purchase_action == "renew" else "Ключ выдан"
    await callback.message.edit_text(
        f"✅ <b>{header_text}</b>\n\n"
        f"Пользователь: <code>{telegram_id}</code>\n"
        f"Тип: {type_labels.get(tariff.tariff_type, '')}\n"
        f"Тариф: {tariff.label} (до {expires_str})",
        reply_markup=admin_back_kb(),
        parse_mode="HTML",
    )
    await callback.answer()

    # Deliver VPN key to user
    if vpn_key:
        from bot.services.vhq_subscription_proxy import get_subscription_display_key
        from types import SimpleNamespace
        sub_obj = subscription or SimpleNamespace(vpn_key=vpn_key)
        display_key = get_subscription_display_key(sub_obj) or vpn_key
        key_display = display_key if len(display_key) <= 200 else display_key[:200] + "..."
        try:
            await callback.bot.send_message(
                telegram_id,
                MANUAL_KEY_DELIVERED.format(key=key_display, expires=expires_str),
                parse_mode="HTML",
            )
            await callback.bot.send_message(
                telegram_id,
                f"📋 <b>Полный ключ:</b>\n\n<code>{display_key}</code>",
                parse_mode="HTML",
            )
            guides = {
                Platform.ANDROID: GUIDE_ANDROID,
                Platform.IOS: GUIDE_IOS,
                Platform.MAC: GUIDE_MAC,
                Platform.WINDOWS: GUIDE_WINDOWS,
                Platform.ANDROID_TV: GUIDE_ANDROID_TV,
            }
            guide = guides.get(platform, GUIDE_ANDROID)
            from bot.services.guide_service import send_guide
            await send_guide(
                callback.bot, telegram_id, platform,
                guide, reply_markup=back_to_menu_kb(),
            )
        except Exception as exc:
            logger.error(f"Failed to deliver VPN key to {telegram_id}: {exc}")

    # Deliver MTProto proxy link to user
    if proxy_link:
        try:
            await callback.bot.send_message(
                telegram_id,
                MTPROTO_KEY_BULK.format(proxy_links=proxy_link, expires=expires_str),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as exc:
            logger.error(f"Failed to deliver MTProto link to {telegram_id}: {exc}")


# ── Helpers ───────────────────────────────────────────

async def _show_user_info(message: Message, user: User) -> None:
    """Show comprehensive user card with all available data."""
    from bot.services.webstore_bridge import fetch_linked_web_profile, sync_linked_web_subscriptions
    web_profile = await fetch_linked_web_profile(user.telegram_id)
    await sync_linked_web_subscriptions(user.telegram_id, web_profile)
    async with async_session() as session:
        # Re-fetch with all relations
        result = await session.execute(
            select(User)
            .options(
                selectinload(User.subscriptions).selectinload(Subscription.server),
                selectinload(User.subscriptions).selectinload(Subscription.tariff),
                selectinload(User.payments),
                selectinload(User.proxy_accounts),
            )
            .where(User.id == user.id)
        )
        u = result.scalar_one()

        # Referral stats
        ref_count = await session.scalar(
            select(func.count(User.id)).where(User.referred_by == u.telegram_id)
        ) or 0

        # Who referred this user
        referrer_info = "-"
        if u.referred_by:
            ref_result = await session.execute(
                select(User).where(User.telegram_id == u.referred_by)
            )
            referrer = ref_result.scalar_one_or_none()
            if referrer:
                referrer_info = f"{referrer.full_name or referrer.username or ''} [<code>{u.referred_by}</code>]"
            else:
                referrer_info = f"<code>{u.referred_by}</code>"

        # Completed payments count and total
        completed_payments = [p for p in u.payments if p.status == PaymentStatus.COMPLETED]
        total_paid = sum(
            p.amount / 100 if p.currency == "RUB" else p.amount
            for p in completed_payments
        )

        # Active subs
        active_subs = [s for s in u.subscriptions if s.status == SubStatus.ACTIVE]
        expired_subs = [s for s in u.subscriptions if s.status == SubStatus.EXPIRED]

        # Total device slots across active subs
        included_slots = await get_included_device_slots(session)
        from bot.services.adapt_routing import is_adapt_subscription
        total_device_slots = sum(
            (s.device_slots or 1) if is_adapt_subscription(s) else max(s.device_slots or 0, included_slots)
            for s in active_subs
        ) if active_subs else 0

        # Proxy accounts (keys)
        proxy_count = len(u.proxy_accounts)
        partner = await session.scalar(
            select(Partner).where(Partner.telegram_id == u.telegram_id)
        )
        feedback_count = await session.scalar(
            select(func.count(Feedback.id)).where(Feedback.telegram_id == u.telegram_id)
        ) or 0
        balance_autodebit_enabled = bool(u.balance_mode_enabled and u.balance_autodebit_enabled)
        next_daily_charge_at = u.next_daily_charge_at

    # Build text
    text = (
        f"👤 <b>Карточка клиента</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🆔 Telegram ID: <code>{u.telegram_id}</code>\n"
        f"📛 Имя: {u.full_name or '-'}\n"
        f"👤 Username: @{u.username}\n" if u.username else ""
    )
    if not u.username:
        text = (
            f"👤 <b>Карточка клиента</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🆔 Telegram ID: <code>{u.telegram_id}</code>\n"
            f"📛 Имя: {u.full_name or '-'}\n"
            f"👤 Username: -\n"
        )
    else:
        text = (
            f"👤 <b>Карточка клиента</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🆔 Telegram ID: <code>{u.telegram_id}</code>\n"
            f"📛 Имя: {u.full_name or '-'}\n"
            f"👤 Username: @{u.username}\n"
        )

    # Under "Устройств:" row add details if they have an active adapt subscription
    adapt_sub = next((s for s in active_subs if is_adapt_subscription(s)), None)
    connected_line = ""
    traffic_line = ""
    if adapt_sub:
        adapt_uuid = get_adapt_uuid_from_subscription(adapt_sub)
        if adapt_uuid:
            try:
                from bot.services.adapt_api import AdaptAPI
                devices = await AdaptAPI().get_devices(adapt_uuid)
                connected_count = len(devices)
            except Exception as e:
                logger.error(f"Error fetching adapt devices: {e}")
                connected_count = "ошибка"

            try:
                from bot.services.adapt_api import AdaptAPI
                status_data = await AdaptAPI().get_status(adapt_uuid)
                used_bytes = status_data.get("used_traffic_bytes") or 0
                limit_bytes = status_data.get("traffic_limit_bytes") or 0
                used_traffic_gb = f"{used_bytes / (1024**3):.2f}"
                limit_traffic_gb = f"{limit_bytes / (1024**3):.0f}"
            except Exception as e:
                logger.error(f"Error fetching adapt status: {e}")
                used_traffic_gb = "ошибка"
                limit_traffic_gb = "ошибка"

            connected_line = f"├ 🖥 Подключено: <b>{connected_count}</b>\n"
            traffic_line = f"├ ⚡️Трафик для обходов: <b>{used_traffic_gb}</b> из <b>{limit_traffic_gb}</b> Гб\n"

    text += (
        f"📱 Платформа: {u.platform.value if u.platform else '-'}\n"
        f"🚫 Заблокирован: {'Да' if u.is_blocked else 'Нет'}\n"
        f"📅 Регистрация: {_format_dt_msk(u.created_at)}\n"
        f"🤝 Партнёрка: {'Да' if partner else 'Нет'}\n"
        f"\n"
        f"<b>💰 Финансы</b>\n"
        f"├ Баланс: <b>{(u.balance_rub or 0.0):.2f} ₽</b>\n"
        f"├ Оплат всего: <b>{len(completed_payments)}</b>\n"
        f"└ Оплачено суммарно: <b>{total_paid:.0f} ₽</b>\n"
        f"\n"
        f"<b>🔗 Рефералы</b>\n"
        f"├ Приглашено: <b>{ref_count} чел.</b>\n"
        f"└ Приглашен кем: {referrer_info}\n"
        f"\n"
        f"<b>📊 Подписки</b>\n"
        f"├ Активных: <b>{len(active_subs)}</b>\n"
        f"├ Истекших: <b>{len(expired_subs)}</b>\n"
        f"├ Устройств: <b>{total_device_slots}</b>\n"
        f"{connected_line}"
        f"{traffic_line}"
        f"└ Ключей (proxy): <b>{proxy_count}</b>\n"
    )

    if u.ad_source:
        text += (
            f"\n"
            f"<b>📣 Реклама</b>\n"
            f"├ Тип: <b>{_format_ad_source_kind(u.ad_source_kind)}</b>\n"
            f"└ Метка: <code>{u.ad_source}</code>\n"
        )

    # Show active subscriptions
    if active_subs:
        text += "\n<b>🟢 Активные подписки:</b>\n"
        for s in active_subs:
            srv_name = s.server.name if s.server else "?"
            srv_emoji = s.server.country_emoji if s.server else "🌍"
            exp = _format_dt_msk(s.expires_at, include_time=True)
            if is_adapt_subscription(s):
                slots = s.device_slots or 1
            else:
                slots = max(s.device_slots or 0, included_slots)
            text += f"  {srv_emoji} {srv_name} - до {exp} МСК ({slots} устр.)\n"

    await message.answer(
        text,
        reply_markup=user_actions_kb(
            u.telegram_id,
            u.is_blocked,
            partner_id=partner.id if partner else None,
            has_active_sub=len(active_subs) > 0,
            feedback_count=feedback_count,
        ),
        parse_mode="HTML",
    )


# ── Helpers: Bot Settings ──────────────────────────────

async def _get_setting(session, key: str, default: str) -> str:
    row = await session.get(BotSettings, key)
    return row.value if row else default


async def _set_setting(session, key: str, value: str) -> None:
    row = await session.get(BotSettings, key)
    if row:
        row.value = value
    else:
        session.add(BotSettings(key=key, value=value))
    await session.commit()


# ── Tariff CRUD ───────────────────────────────────────

@router.callback_query(F.data == "adm_tariffs")
async def admin_tariffs(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    async with async_session() as session:
        result = await session.execute(
            select(Tariff).order_by(Tariff.price_rub)
        )
        tariffs = result.scalars().all()

    active = sum(1 for t in tariffs if t.is_active)
    try:
        await callback.message.edit_text(
            ADMIN_TARIFFS_HEADER.format(active=active, total=len(tariffs)),
            reply_markup=tariffs_admin_kb(tariffs, page=0),
            parse_mode="HTML",
        )
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("adm_tariffs_page_"))
async def admin_tariffs_page(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    page = int(callback.data.split("_")[-1])
    async with async_session() as session:
        result = await session.execute(
            select(Tariff).order_by(Tariff.price_rub)
        )
        tariffs = result.scalars().all()

    active = sum(1 for t in tariffs if t.is_active)
    try:
        await callback.message.edit_text(
            ADMIN_TARIFFS_HEADER.format(active=active, total=len(tariffs)),
            reply_markup=tariffs_admin_kb(tariffs, page=page),
            parse_mode="HTML",
        )
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("adm_tariff_") & ~F.data.startswith("adm_tariff_toggle_") & ~F.data.startswith("adm_tariff_del_") & ~F.data.startswith("adm_tariff_add") & ~F.data.startswith("adm_tariff_admonly_"))
async def admin_tariff_detail(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    tariff_id = int(callback.data.split("_")[-1])
    async with async_session() as session:
        tariff = await session.get(Tariff, tariff_id)
    if not tariff:
        await callback.answer("Тариф не найден", show_alert=True)
        return

    stars_part = f" / {tariff.price_stars}⭐" if tariff.price_stars else ""
    status_icon = "🟢" if tariff.is_active else "🔴"
    admin_only_label = "🔒 только для админов" if getattr(tariff, "is_admin_only", False) else "👁 виден пользователям"
    type_labels = {
        TariffType.VPN: "🌐 Весь интернет",
        TariffType.TG_PROXY: "📱 TG-ускоритель",
        TariffType.BOTH: "🔥 Весь интернет + TG-ускоритель",
    }
    type_label = type_labels.get(tariff.tariff_type, "🌐 Весь интернет")
    provider_label = _manual_key_tariff_provider_label(tariff)
    adapt_uuid_line = ""
    if is_adapt_tariff(tariff):
        adapt_uuid_line = f"\n├ Adapt UUID: <code>{tariff.adapt_plan_uuid}</code>"
    vhq_tier = getattr(tariff, "vhq_tier", None) or ""
    vhq_tier_line = f"\n├ VHQ tier: <code>{vhq_tier}</code>" if vhq_tier else ""
    text = (
        f"{status_icon} <b>{tariff.label}</b>\n\n"
        f"├ Тип: <b>{type_label}</b>\n"
        f"├ Провайдер: <b>{provider_label}</b>{adapt_uuid_line}{vhq_tier_line}\n"
        f"├ Срок: <b>{tariff.days} дн.</b>\n"
        f"├ Цена: <b>{tariff.price_rub}₽{stars_part}</b>\n"
        f"├ Видимость: <b>{admin_only_label}</b>\n"
        f"└ ID: <code>{tariff.id}</code>"
    )
    try:
        await callback.message.edit_text(
            text,
            reply_markup=tariff_actions_kb(tariff.id, tariff.is_active, getattr(tariff, "is_admin_only", False)),
            parse_mode="HTML",
        )
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("adm_tariff_toggle_"))
async def admin_tariff_toggle(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    tariff_id = int(callback.data.split("_")[-1])
    async with async_session() as session:
        tariff = await session.get(Tariff, tariff_id)
        if tariff:
            tariff.is_active = not tariff.is_active
            await session.commit()
            status = "активен 🟢" if tariff.is_active else "деактивирован 🔴"
            await write_admin_audit(
                actor_telegram_id=callback.from_user.id,
                action="tariff_status_changed",
                entity_type="tariff",
                entity_id=str(tariff_id),
                summary=f"Тариф «{tariff.label}»: {status}",
            )
            await callback.answer(
                ADMIN_TARIFF_TOGGLED.format(label=tariff.label, status=status),
                show_alert=True,
            )
    await admin_tariffs(callback)


@router.callback_query(F.data.startswith("adm_tariff_admonly_"))
async def admin_tariff_toggle_admin_only(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    tariff_id = int(callback.data.split("_")[-1])
    async with async_session() as session:
        tariff = await session.get(Tariff, tariff_id)
        if not tariff:
            await callback.answer("Тариф не найден", show_alert=True)
            return
        tariff.is_admin_only = not getattr(tariff, "is_admin_only", False)
        await session.commit()
        visibility = "только для админов 🔒" if tariff.is_admin_only else "виден пользователям 👁"
        await write_admin_audit(
            actor_telegram_id=callback.from_user.id,
            action="tariff_visibility_changed",
            entity_type="tariff",
            entity_id=str(tariff_id),
            summary=f"Тариф «{tariff.label}»: {visibility}",
        )
        await callback.answer(f"Тариф «{tariff.label}» теперь {visibility}", show_alert=True)
        await admin_tariff_detail(callback)


@router.callback_query(F.data.startswith("adm_tariff_del_"))
async def admin_tariff_delete(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    tariff_id = int(callback.data.split("_")[-1])
    async with async_session() as session:
        tariff = await session.get(Tariff, tariff_id)
        if not tariff:
            await callback.answer("Тариф не найден", show_alert=True)
            return
        tid = tariff.id
        tariff_label = tariff.label
        await session.delete(tariff)
        await session.commit()
    await callback.answer(ADMIN_TARIFF_DELETED.format(id=tid), show_alert=True)
    await write_admin_audit(
        actor_telegram_id=callback.from_user.id,
        action="tariff_deleted",
        entity_type="tariff",
        entity_id=str(tid),
        summary=f"Тариф «{tariff_label}» удалён",
    )
    await admin_tariffs(callback)


# ── Tariff Editing ────────────────────────────────────

@router.callback_query(F.data.startswith("adm_tedit_label_"))
async def admin_tariff_edit_label(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    tariff_id = int(callback.data.split("_")[-1])
    await state.update_data(edit_tariff_id=tariff_id)
    await state.set_state(AdminStates.waiting_tariff_edit_label)
    await callback.message.edit_text(
        "✏️ Введите новую <b>метку</b> тарифа (например: 1 месяц):",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminStates.waiting_tariff_edit_label, F.text)
async def admin_tariff_save_label(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    tariff_id = data["edit_tariff_id"]
    new_val = message.text.strip()
    async with async_session() as session:
        tariff = await session.get(Tariff, tariff_id)
        if tariff:
            tariff.label = new_val
            await session.commit()
    await state.clear()
    await message.answer(f"✅ Метка изменена на <b>{new_val}</b>", parse_mode="HTML", reply_markup=admin_menu_kb())


@router.callback_query(F.data.startswith("adm_tedit_days_"))
async def admin_tariff_edit_days(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    tariff_id = int(callback.data.split("_")[-1])
    await state.update_data(edit_tariff_id=tariff_id)
    await state.set_state(AdminStates.waiting_tariff_edit_days)
    await callback.message.edit_text(
        "📅 Введите новое количество <b>дней</b>:",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminStates.waiting_tariff_edit_days, F.text)
async def admin_tariff_save_days(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    try:
        new_val = int(message.text.strip())
        assert new_val > 0
    except (ValueError, AssertionError):
        await message.answer("❌ Введите положительное число")
        return
    data = await state.get_data()
    tariff_id = data["edit_tariff_id"]
    async with async_session() as session:
        tariff = await session.get(Tariff, tariff_id)
        if tariff:
            tariff.days = new_val
            await session.commit()
    await state.clear()
    await message.answer(f"✅ Срок изменен на <b>{new_val} дн.</b>", parse_mode="HTML", reply_markup=admin_menu_kb())


@router.callback_query(F.data.startswith("adm_tedit_rub_"))
async def admin_tariff_edit_rub(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    tariff_id = int(callback.data.split("_")[-1])
    await state.update_data(edit_tariff_id=tariff_id)
    await state.set_state(AdminStates.waiting_tariff_edit_price_rub)
    await callback.message.edit_text(
        "💰 Введите новую <b>цену в рублях</b>:",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminStates.waiting_tariff_edit_price_rub, F.text)
async def admin_tariff_save_rub(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    try:
        new_val = int(message.text.strip())
        assert new_val > 0
    except (ValueError, AssertionError):
        await message.answer("❌ Введите положительную сумму")
        return
    data = await state.get_data()
    tariff_id = data["edit_tariff_id"]
    async with async_session() as session:
        tariff = await session.get(Tariff, tariff_id)
        if tariff:
            tariff.price_rub = new_val
            await session.commit()
    await state.clear()
    await message.answer(f"✅ Цена изменена на <b>{new_val}₽</b>", parse_mode="HTML", reply_markup=admin_menu_kb())


@router.callback_query(F.data.startswith("adm_tedit_stars_"))
async def admin_tariff_edit_stars(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    tariff_id = int(callback.data.split("_")[-1])
    await state.update_data(edit_tariff_id=tariff_id)
    await state.set_state(AdminStates.waiting_tariff_edit_price_stars)
    await callback.message.edit_text(
        "⭐ Введите новую цену в <b>Stars</b> (0 - отключить):",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminStates.waiting_tariff_edit_price_stars, F.text)
async def admin_tariff_save_stars(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    try:
        new_val = int(message.text.strip())
        assert new_val >= 0
    except (ValueError, AssertionError):
        await message.answer("❌ Введите 0 или положительное число")
        return
    data = await state.get_data()
    tariff_id = data["edit_tariff_id"]
    async with async_session() as session:
        tariff = await session.get(Tariff, tariff_id)
        if tariff:
            tariff.price_stars = new_val
            await session.commit()
    await state.clear()
    await message.answer(f"✅ Цена Stars изменена на <b>{new_val}⭐</b>", parse_mode="HTML", reply_markup=admin_menu_kb())


@router.callback_query(F.data.startswith("adm_tedit_adapt_"))
async def admin_tariff_edit_adapt(callback: CallbackQuery, state: FSMContext) -> None:
    """Start editing adapt_plan_uuid for a tariff."""
    if not _is_admin(callback.from_user.id):
        return
    tariff_id = int(callback.data.split("_")[-1])
    async with async_session() as session:
        tariff = await session.get(Tariff, tariff_id)
    current = getattr(tariff, "adapt_plan_uuid", None) or "не задан"
    await state.update_data(edit_tariff_id=tariff_id)
    await state.set_state(AdminStates.waiting_tariff_edit_adapt_uuid)
    await callback.message.answer(
        f"🔌 Текущий Adapt UUID: <code>{current}</code>\n\nВведите новый UUID плана (или '-' чтобы очистить):",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminStates.waiting_tariff_edit_adapt_uuid, F.text)
async def admin_tariff_save_adapt(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    new_val = message.text.strip()
    if new_val == "-":
        new_val = None
    data = await state.get_data()
    tariff_id = data["edit_tariff_id"]
    async with async_session() as session:
        tariff = await session.get(Tariff, tariff_id)
        if tariff:
            tariff.adapt_plan_uuid = new_val
            await session.commit()
    await state.clear()
    display = f"<code>{new_val}</code>" if new_val else "<i>очищен</i>"
    await message.answer(f"✅ Adapt UUID изменён: {display}", parse_mode="HTML", reply_markup=admin_menu_kb())


@router.callback_query(F.data.startswith("adm_tedit_vhq_"))
async def admin_tariff_edit_vhq(callback: CallbackQuery, state: FSMContext) -> None:
    """Start editing explicit VHQ tier for a tariff."""
    if not _is_admin(callback.from_user.id):
        return
    tariff_id = int(callback.data.split("_")[-1])
    async with async_session() as session:
        tariff = await session.get(Tariff, tariff_id)
    current = getattr(tariff, "vhq_tier", None) or "не задан"
    await state.update_data(edit_tariff_id=tariff_id)
    await state.set_state(AdminStates.waiting_tariff_edit_vhq_tier)
    await callback.message.answer(
        f"⚡️ Текущий VHQ tier: <code>{current}</code>\n\n"
        "Введите <code>lite</code>, <code>basic</code> или <code>-</code> чтобы очистить.\n"
        "Если VHQ tier задан, переименование тарифа не переведёт его в Marzban.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminStates.waiting_tariff_edit_vhq_tier, F.text)
async def admin_tariff_save_vhq(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    new_val = message.text.strip().lower()
    if new_val == "-":
        new_val = None
    elif new_val not in {"lite", "basic"}:
        await message.answer("❌ Введите <code>lite</code>, <code>basic</code> или <code>-</code>", parse_mode="HTML")
        return
    data = await state.get_data()
    tariff_id = data["edit_tariff_id"]
    async with async_session() as session:
        tariff = await session.get(Tariff, tariff_id)
        if tariff:
            tariff.vhq_tier = new_val
            if new_val:
                tariff.adapt_plan_uuid = None
            await session.commit()
    await state.clear()
    display = f"<code>{new_val}</code>" if new_val else "<i>очищен</i>"
    await message.answer(f"✅ VHQ tier изменён: {display}", parse_mode="HTML", reply_markup=admin_menu_kb())


@router.callback_query(F.data == "adm_adapt_plans")
async def admin_adapt_plans_list(callback: CallbackQuery) -> None:
    """Show Adapt plans list fetched from the API."""
    if not _is_admin(callback.from_user.id):
        return
    from bot.services.adapt_api import AdaptAPI, AdaptAPIError
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    try:
        plans = await AdaptAPI().list_plans()
    except AdaptAPIError as exc:
        await callback.answer(f"Ошибка: {exc}", show_alert=True)
        return

    if not plans:
        await callback.answer("Планы не найдены", show_alert=True)
        return

    lines = ["🔌 <b>Adapt: список планов</b>\n"]
    for p in plans:
        status = "🟢" if p.get("is_active") else "🔴"
        name = p.get("name", "?")
        uuid_ = p.get("uuid", "?")
        days = p.get("days", "?")
        price = p.get("retail_price_usd") or p.get("price_usd", "?")
        lines.append(f"{status} <b>{name}</b>\n  UUID: <code>{uuid_}</code>\n  {days} дн. / {price} USD")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]
    ])
    await callback.message.edit_text("\n\n".join(lines), parse_mode="HTML", reply_markup=kb)
    await callback.answer()



@router.callback_query(F.data == "adm_tariff_add")
async def admin_tariff_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Весь интернет", callback_data="adm_ttype_vpn")],
        [InlineKeyboardButton(text="📱 Telegram-ускоритель", callback_data="adm_ttype_tg_proxy")],
        [InlineKeyboardButton(text="🔥 Весь интернет + TG-ускоритель", callback_data="adm_ttype_both")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_tariffs")],
    ])
    await callback.message.edit_text(
        "➕ <b>Добавить тариф</b>\n\nВыберите тип тарифа:",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await state.set_state(AdminStates.waiting_tariff_type)
    await callback.answer()


@router.callback_query(AdminStates.waiting_tariff_type, F.data.startswith("adm_ttype_"))
async def admin_tariff_type_selected(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    tariff_type = callback.data.removeprefix("adm_ttype_")
    type_labels = {"vpn": "🌐 Весь интернет", "tg_proxy": "📱 TG-ускоритель", "both": "🔥 Весь интернет + TG-ускоритель"}
    await state.update_data(tariff_type=tariff_type)
    await callback.message.edit_text(
        f"Тип: <b>{type_labels.get(tariff_type, tariff_type)}</b>\n\nВведите количество дней:",
        reply_markup=admin_back_kb(),
        parse_mode="HTML",
    )
    await state.set_state(AdminStates.waiting_tariff_days)
    await callback.answer()


@router.message(AdminStates.waiting_tariff_days, F.text)
async def admin_tariff_days(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    try:
        days = int(message.text.strip())
        assert days > 0
    except (ValueError, AssertionError):
        await message.answer("❌ Введите положительное число дней")
        return
    await state.update_data(tariff_days=days)
    await message.answer(
        f"Срок: <b>{days} дн.</b>\n\nТеперь введите <b>метку тарифа</b> (например: 10 дней, 1 месяц):",
        parse_mode="HTML",
    )
    await state.set_state(AdminStates.waiting_tariff_label)


@router.message(AdminStates.waiting_tariff_label, F.text)
async def admin_tariff_label(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    label = message.text.strip()
    await state.update_data(tariff_label=label)
    await message.answer(
        f"Метка: <b>{label}</b>\n\nВведите цену в <b>рублях</b>:",
        parse_mode="HTML",
    )
    await state.set_state(AdminStates.waiting_tariff_price_rub)


@router.message(AdminStates.waiting_tariff_price_rub, F.text)
async def admin_tariff_price_rub(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    try:
        price_rub = int(message.text.strip())
        assert price_rub > 0
    except (ValueError, AssertionError):
        await message.answer("❌ Введите положительную сумму в рублях")
        return
    await state.update_data(tariff_price_rub=price_rub)
    await message.answer(
        f"Цена: <b>{price_rub}₽</b>\n\nВведите цену в <b>Telegram Stars</b> (0 - отключить):",
        parse_mode="HTML",
    )
    await state.set_state(AdminStates.waiting_tariff_price_stars)


@router.message(AdminStates.waiting_tariff_price_stars, F.text)
async def admin_tariff_price_stars(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    try:
        price_stars = int(message.text.strip())
        assert price_stars >= 0
    except (ValueError, AssertionError):
        await message.answer("❌ Введите 0 или положительное число Stars")
        return

    data = await state.get_data()
    await state.clear()

    tariff_type_str = data.get("tariff_type", "vpn")
    try:
        tariff_type = TariffType(tariff_type_str)
    except ValueError:
        tariff_type = TariffType.VPN

    async with async_session() as session:
        tariff = Tariff(
            days=data["tariff_days"],
            label=data["tariff_label"],
            price_rub=data["tariff_price_rub"],
            price_stars=price_stars,
            tariff_type=tariff_type,
        )
        session.add(tariff)
        await session.commit()

    await message.answer(
        ADMIN_TARIFF_ADDED.format(
            label=data["tariff_label"],
            days=data["tariff_days"],
            price_rub=data["tariff_price_rub"],
        ),
        reply_markup=admin_menu_kb(),
        parse_mode="HTML",
    )


# ── Bot Settings ──────────────────────────────────────

@router.callback_query(F.data == "adm_settings")
async def admin_settings(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    async with async_session() as session:
        stars_val = await _get_setting(session, "stars_enabled", "1")
        cashback_val = await _get_setting(session, "cashback_enabled", "0")
        cashback_pct = await _get_setting(session, "cashback_percent", "10")
        max_dev = await _get_setting(session, "max_devices_per_sub", "3")
        daily_charge_rub = await _get_setting(session, "daily_charge_rub", "3.17")
        dev_price_rub = await _get_setting(session, "extra_device_price_rub", "50")
        dev_price_stars = await _get_setting(session, "extra_device_price_stars", "0")
        demo_key_val = await _get_setting(session, "demo_key_enabled", "0")
        demo_key_days = await _get_setting(session, "demo_key_days", "3")
        wa_val = await _get_setting(session, "whatsapp_proxy_enabled", "0")
        wa_host = await _get_setting(session, "whatsapp_proxy_host", "")
        legal_urls = await get_all_legal_doc_urls(session)

    stars_enabled = stars_val == "1"
    cashback_enabled = cashback_val == "1"
    demo_key_enabled = demo_key_val == "1"
    wa_enabled = wa_val == "1"
    wa_status = f"ВКЛ ✅ ({wa_host})" if wa_enabled and wa_host else ("ВКЛ ✅ (адрес не задан)" if wa_enabled else "ВЫКЛ ❌")
    text = ADMIN_SETTINGS_HEADER.format(
        stars_status="ВКЛ ✅" if stars_enabled else "ВЫКЛ ❌",
        cashback_status="ВКЛ ✅" if cashback_enabled else "ВЫКЛ ❌",
        cashback_pct=cashback_pct,
        max_devices="без лимита" if max_dev == "0" else max_dev,
        daily_charge_rub=daily_charge_rub,
        device_price_rub=dev_price_rub,
        device_price_stars_part=f" / {dev_price_stars}⭐" if dev_price_stars != "0" else "",
        demo_key_status="ВКЛ ✅" if demo_key_enabled else "ВЫКЛ ❌",
        demo_key_days=demo_key_days,
        whatsapp_status=wa_status,
        policy_status="настроен ✅" if legal_urls.get("policy") else "не задан",
        agree_status="настроен ✅" if legal_urls.get("agree") else "не задан",
        oferta_status="настроен ✅" if legal_urls.get("oferta") else "не задан",
    )
    try:
        await callback.message.edit_text(
            text,
            reply_markup=settings_kb(stars_enabled, demo_key_enabled, wa_enabled, cashback_enabled),
            parse_mode="HTML",
        )
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data == "adm_toggle_cashback")
async def admin_toggle_cashback(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    async with async_session() as session:
        current = await _get_setting(session, "cashback_enabled", "0")
        new_val = "0" if current == "1" else "1"
        await _set_setting(session, "cashback_enabled", new_val)
        if new_val == "1":
            pct_row = await _get_setting(session, "cashback_percent", "")
            if not pct_row:
                await _set_setting(session, "cashback_percent", "10")
    status = "включён 🎁" if new_val == "1" else "отключён"
    await callback.answer(f"Кешбэк {status}", show_alert=True)
    await admin_settings(callback)


@router.callback_query(F.data == "adm_set_cashback_pct")
async def admin_set_cashback_pct_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await callback.message.edit_text(
        "🎁 <b>Процент кешбэка</b>\n\n"
        "Введите процент кешбэка, начисляемый на баланс за оплату подписки (например, <code>10</code> или <code>15</code>):",
        reply_markup=admin_back_kb(),
        parse_mode="HTML",
    )
    await state.set_state(AdminStates.waiting_cashback_pct)
    await callback.answer()


@router.message(AdminStates.waiting_cashback_pct, F.text)
async def admin_set_cashback_pct_process(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    try:
        val = float(message.text.strip().replace(",", "."))
        assert 0 <= val <= 100
    except (ValueError, AssertionError):
        await message.answer("❌ Введите число от 0 до 100")
        return
    async with async_session() as session:
        await _set_setting(session, "cashback_percent", str(int(val) if val.is_integer() else val))
    await state.clear()
    await message.answer(
        f"✅ Процент кешбэка установлен: <b>{val:.0f}%</b>",
        reply_markup=admin_menu_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "adm_set_cashback_text")
async def admin_set_cashback_text_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    async with async_session() as session:
        cur = await _get_setting(session, "cashback_notify_text", "")
    cur_display = cur if cur else (
        "🎁 Вам начислен кешбэк <b>+{amount} ₽</b> ({pct}% от оплаты {payment_amount} ₽) на ваш баланс!\n\n"
        "Вы можете использовать баланс для оплаты и продления подписки."
    )
    await callback.message.edit_text(
        "✏️ <b>Текст уведомления о начислении кешбэка</b>\n\n"
        f"<b>Текущий текст:</b>\n{cur_display}\n\n"
        "Доступные переменные:\n"
        "• <code>{amount}</code> — сумма кешбэка\n"
        "• <code>{pct}</code> — процент кешбэка\n"
        "• <code>{payment_amount}</code> — сумма платежа\n\n"
        "Отправьте новый текст сообщения (поддерживается HTML) или отправьте <code>default</code> для сброса к стандартному:",
        reply_markup=admin_back_kb(),
        parse_mode="HTML",
    )
    await state.set_state(AdminStates.waiting_cashback_notify_text)
    await callback.answer()


@router.message(AdminStates.waiting_cashback_notify_text, F.text)
async def admin_set_cashback_notify_text_save(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    text_val = message.text.strip()
    if text_val.lower() == "default":
        text_val = ""
    async with async_session() as session:
        await _set_setting(session, "cashback_notify_text", text_val)
    await state.clear()
    await message.answer(
        "✅ Текст уведомления о кешбэке успешно обновлён!",
        reply_markup=admin_menu_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "adm_set_bal_remind_text")
async def admin_set_balance_reminder_text_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    async with async_session() as session:
        cur = await _get_setting(session, "balance_reminder_text", "")
    cur_display = cur if cur else "💰 У вас на балансе <b>{balance} ₽</b> — вы можете использовать их для оплаты продления!"
    await callback.message.edit_text(
        "✏️ <b>Текст напоминания о балансе</b>\n\n"
        "Эта строка автоматически добавляется в сообщения об окончании подписки, если у пользователя есть деньги на балансе.\n\n"
        f"<b>Текущий текст:</b>\n{cur_display}\n\n"
        "Доступные переменные:\n"
        "• <code>{balance}</code> — текущий баланс пользователя в рублях\n\n"
        "Отправьте новый текст напоминания (HTML) или отправьте <code>default</code> для сброса:",
        reply_markup=admin_back_kb(),
        parse_mode="HTML",
    )
    await state.set_state(AdminStates.waiting_balance_reminder_text)
    await callback.answer()


@router.message(AdminStates.waiting_balance_reminder_text, F.text)
async def admin_set_balance_reminder_text_save(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    text_val = message.text.strip()
    if text_val.lower() == "default":
        text_val = ""
    async with async_session() as session:
        await _set_setting(session, "balance_reminder_text", text_val)
    await state.clear()
    await message.answer(
        "✅ Текст напоминания о балансе успешно обновлён!",
        reply_markup=admin_menu_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "adm_toggle_demo_key")
async def admin_toggle_demo_key(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    async with async_session() as session:
        current = await _get_setting(session, "demo_key_enabled", "0")
        new_val = "0" if current == "1" else "1"
        await _set_setting(session, "demo_key_enabled", new_val)
    status = "включён 🎁" if new_val == "1" else "отключён"
    await callback.answer(f"Демо-ключ {status}", show_alert=True)
    await admin_settings(callback)


@router.callback_query(F.data == "adm_toggle_stars")
async def admin_toggle_stars(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    async with async_session() as session:
        current = await _get_setting(session, "stars_enabled", "1")
        new_val = "0" if current == "1" else "1"
        await _set_setting(session, "stars_enabled", new_val)
    status = "включены ⭐" if new_val == "1" else "отключены"
    await callback.answer(f"Telegram Stars {status}", show_alert=True)
    await admin_settings(callback)


@router.callback_query(F.data == "adm_toggle_whatsapp")
async def admin_toggle_whatsapp(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    async with async_session() as session:
        current = await _get_setting(session, "whatsapp_proxy_enabled", "0")
        new_val = "0" if current == "1" else "1"
        await _set_setting(session, "whatsapp_proxy_enabled", new_val)
    status = "включён 💬" if new_val == "1" else "отключён"
    await callback.answer(f"WhatsApp-ускоритель {status}", show_alert=True)
    await admin_settings(callback)


@router.callback_query(F.data == "adm_set_whatsapp_host")
async def admin_set_whatsapp_host_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await callback.message.edit_text(
        "💬 <b>Адрес WhatsApp-ускорителя</b>\n\n"
        "Отправьте адрес сервера (hostname или IP).\n"
        "Пользователи будут вводить его в настройках WhatsApp.\n\n"
        "Пример: <code>wa.example.com</code>",
        reply_markup=admin_back_kb(),
        parse_mode="HTML",
    )
    await state.set_state(AdminStates.waiting_whatsapp_host)
    await callback.answer()


@router.message(AdminStates.waiting_whatsapp_host, F.text)
async def admin_set_whatsapp_host_process(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    host = message.text.strip()
    async with async_session() as session:
        await _set_setting(session, "whatsapp_proxy_host", host)
    await state.clear()
    await message.answer(
        f"✅ Адрес WhatsApp-ускорителя установлен: <code>{host}</code>",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "adm_set_max_devices")
async def admin_set_max_devices_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await callback.message.edit_text(
        "📱 <b>Макс. устройств на подписку</b>\n\nВведите число или <code>0</code> для режима без лимита:",
        reply_markup=admin_back_kb(),
        parse_mode="HTML",
    )
    await state.set_state(AdminStates.waiting_max_devices)
    await callback.answer()


@router.message(AdminStates.waiting_max_devices, F.text)
async def admin_set_max_devices_process(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    try:
        val = int(message.text.strip())
        assert val >= 0
    except (ValueError, AssertionError):
        await message.answer("❌ Введите число 0 или больше")
        return
    async with async_session() as session:
        await _set_setting(session, "max_devices_per_sub", str(val))
    await state.clear()
    await message.answer(
        f"✅ Макс. устройств на подписку: <b>{'без лимита' if val == 0 else val}</b>",
        reply_markup=admin_menu_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "adm_set_daily_charge")
async def admin_set_daily_charge_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await callback.message.edit_text(
        "💰 <b>Дневная ставка</b>\n\n"
        "Введите сумму ежедневного списания в рублях.\n"
        "Можно с копейками, например: <b>3.17</b>",
        reply_markup=admin_back_kb(),
        parse_mode="HTML",
    )
    await state.set_state(AdminStates.waiting_daily_charge_rub)
    await callback.answer()


@router.message(AdminStates.waiting_daily_charge_rub, F.text)
async def admin_set_daily_charge_process(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    raw = message.text.strip().replace(",", ".")
    try:
        val = round(float(raw), 2)
        assert val > 0
    except (ValueError, AssertionError):
        await message.answer("❌ Введите положительную сумму. Например: 3.17")
        return
    async with async_session() as session:
        await _set_setting(session, "daily_charge_rub", f"{val:.2f}")
    await state.clear()
    await message.answer(
        f"✅ Дневная ставка: <b>{val:.2f} ₽</b>",
        reply_markup=admin_menu_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "adm_set_device_price")
async def admin_set_device_price_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await callback.message.edit_text(
        "💸 <b>Цена доп. устройства</b>\n\nВведите цену в <b>рублях</b>:",
        reply_markup=admin_back_kb(),
        parse_mode="HTML",
    )
    await state.set_state(AdminStates.waiting_device_price_rub)
    await callback.answer()


@router.message(AdminStates.waiting_device_price_rub, F.text)
async def admin_set_device_price_rub_process(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    try:
        val = int(message.text.strip())
        assert val >= 0
    except (ValueError, AssertionError):
        await message.answer("❌ Введите 0 или положительную сумму")
        return
    await state.update_data(device_price_rub=val)
    await message.answer(
        f"Цена в рублях: <b>{val}₽</b>\n\nВведите цену в <b>Stars</b> (0 - отключить):",
        parse_mode="HTML",
    )
    await state.set_state(AdminStates.waiting_device_price_stars)


@router.message(AdminStates.waiting_device_price_stars, F.text)
async def admin_set_device_price_stars_process(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    try:
        val = int(message.text.strip())
        assert val >= 0
    except (ValueError, AssertionError):
        await message.answer("❌ Введите 0 или положительное число Stars")
        return
    data = await state.get_data()
    await state.clear()
    async with async_session() as session:
        await _set_setting(session, "extra_device_price_rub", str(data["device_price_rub"]))
        await _set_setting(session, "extra_device_price_stars", str(val))
    await message.answer(
        f"✅ Цена доп. устройства: <b>{data['device_price_rub']}₽</b>"
        + (f" / <b>{val}⭐</b>" if val else ""),
        reply_markup=admin_menu_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("adm_doc_"))
async def admin_doc_edit_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    doc_code = callback.data[len("adm_doc_"):]
    if doc_code not in LEGAL_DOCS:
        await callback.answer("Документ не найден", show_alert=True)
        return
    _, label = LEGAL_DOCS[doc_code]
    await state.set_state(AdminStates.waiting_legal_doc_url)
    await state.update_data(legal_doc_code=doc_code)
    await callback.message.edit_text(
        f"📄 <b>{label}</b>\n\n"
        "Отправьте ссылку на документ.\n"
        "Чтобы очистить значение, отправьте <code>-</code>.",
        reply_markup=admin_back_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminStates.waiting_legal_doc_url, F.text)
async def admin_doc_edit_save(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    doc_code = data.get("legal_doc_code")
    if doc_code not in LEGAL_DOCS:
        await state.clear()
        await message.answer("❌ Документ не найден.", reply_markup=admin_menu_kb())
        return

    key, label = LEGAL_DOCS[doc_code]
    value = message.text.strip()
    if value == "-":
        value = ""
    elif not (value.startswith("http://") or value.startswith("https://")):
        await message.answer("❌ Нужна ссылка, начинающаяся с http:// или https://")
        return

    async with async_session() as session:
        await _set_setting(session, key, value)

    await state.clear()
    status = "очищен" if not value else "сохранён"
    await message.answer(
        f"✅ {label}: <b>{status}</b>",
        reply_markup=admin_menu_kb(),
        parse_mode="HTML",
    )


# ── Platform Guides ────────────────────────────────────

from bot.keyboards.admin import guides_menu_kb, guide_detail_kb, PLATFORM_LABELS


async def _show_guide_detail(callback: CallbackQuery, platform: str) -> None:
    from bot.handlers.start import _guide_text
    from bot.handlers.mailing import _buttons_preview

    async with async_session() as session:
        pg = await session.get(PlatformGuide, platform)

    has_media = bool(pg and pg.media_file_id)
    has_text = bool(pg and pg.guide_text)
    has_buttons = bool(pg and pg.buttons_json)

    current_text = pg.guide_text if has_text else _guide_text(platform)
    media_info = f"📎 Медиа: <b>{pg.media_type}</b>" if has_media else "📎 Медиа: <i>не загружено</i>"
    buttons_info = "🔘 Кнопки: <i>не настроены</i>"
    if has_buttons:
        buttons_info = f"🔘 Кнопки:\n{_buttons_preview(pg.buttons_json)}"

    text_info = f"📝 <b>Текст гайда:</b>\n───────────────────\n{current_text}\n───────────────────"
    if has_text:
        text_status = "🟢 Используется собственный кастомный текст."
    else:
        text_status = "⚪️ Используется стандартный текст по умолчанию."

    body = (
        f"📚 <b>Гайд: {PLATFORM_LABELS[platform]}</b>\n\n"
        f"{text_status}\n"
        f"{media_info}\n"
        f"{buttons_info}\n\n"
        f"{text_info}"
    )

    try:
        await callback.message.edit_text(
            body,
            reply_markup=guide_detail_kb(platform, has_media, has_text, has_buttons),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Error rendering guide detail: {e}")


@router.callback_query(F.data == "adm_guides")
async def admin_guides_menu(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    async with async_session() as session:
        has_media: dict[str, bool] = {}
        for p in PLATFORM_LABELS:
            pg = await session.get(PlatformGuide, p)
            has_media[p] = bool(pg and pg.media_file_id)
    try:
        await callback.message.edit_text(
            "📚 <b>Гайды по платформам</b>\n\nВыберите платформу для настройки текста, медиа и кнопок:",
            reply_markup=guides_menu_kb(has_media),
            parse_mode="HTML",
        )
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("adm_guide_") & ~F.data.startswith("adm_guide_upload_") & ~F.data.startswith("adm_guide_clear_") & ~F.data.startswith("adm_guide_etext_") & ~F.data.startswith("adm_guide_rtext_") & ~F.data.startswith("adm_guide_btns_") & ~F.data.startswith("adm_guide_bdone_") & ~F.data.startswith("adm_guide_prev_"))
async def admin_guide_detail(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    platform = callback.data[len("adm_guide_"):]
    if platform not in PLATFORM_LABELS:
        await callback.answer("Платформа не найдена", show_alert=True)
        return
    await _show_guide_detail(callback, platform)
    await callback.answer()


@router.callback_query(F.data.startswith("adm_guide_upload_"))
async def admin_guide_upload_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    platform = callback.data[len("adm_guide_upload_"):]
    if platform not in PLATFORM_LABELS:
        await callback.answer("Платформа не найдена", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_guide_media)
    await state.update_data(guide_platform=platform)
    
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Отмена", callback_data=f"adm_guide_{platform}")]
    ])
    
    await callback.message.edit_text(
        f"📤 Отправьте <b>фото, видео или альбом</b> для гайда «{PLATFORM_LABELS[platform]}».\n\n"
        f"Или нажмите «◀️ Отмена» для возврата.",
        reply_markup=cancel_kb,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm_guide_clear_"))
async def admin_guide_clear(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    platform = callback.data[len("adm_guide_clear_"):]
    async with async_session() as session:
        pg = await session.get(PlatformGuide, platform)
        if pg:
            pg.media_file_id = None
            pg.media_type = None
            await session.commit()
    await callback.answer("Медиа удалено", show_alert=False)
    await _show_guide_detail(callback, platform)


@router.message(AdminStates.waiting_guide_media)
async def admin_guide_media_received(message: Message, state: FSMContext, album: list[Message] | None = None) -> None:
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    platform = data.get("guide_platform", "")

    file_id = None
    media_type = None

    if album:
        media_list = []
        for msg in album:
            if msg.photo:
                media_list.append(f"photo:{msg.photo[-1].file_id}")
            elif msg.video:
                media_list.append(f"video:{msg.video.file_id}")
        if media_list:
            file_id = ",".join(media_list)
            media_type = "album"
    else:
        if message.photo:
            file_id = message.photo[-1].file_id
            media_type = "photo"
        elif message.video:
            file_id = message.video.file_id
            media_type = "video"
        elif message.animation:
            file_id = message.animation.file_id
            media_type = "video"

    if not file_id:
        await message.answer("❌ Отправьте фото, видео или альбом.")
        return

    # Delete messages to keep chat clean
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

    async with async_session() as session:
        pg = await session.get(PlatformGuide, platform)
        if pg:
            pg.media_file_id = file_id
            pg.media_type = media_type
            pg.updated_at = datetime.utcnow()
        else:
            session.add(PlatformGuide(
                platform=platform,
                media_file_id=file_id,
                media_type=media_type,
            ))
        await session.commit()

    await state.clear()
    
    await message.answer(f"✅ Медиа для «{PLATFORM_LABELS.get(platform, platform)}» успешно сохранено.")
    
    # Send detail view
    from bot.handlers.start import _guide_text
    async with async_session() as session:
        pg = await session.get(PlatformGuide, platform)
    has_media = bool(pg and pg.media_file_id)
    has_text = bool(pg and pg.guide_text)
    has_buttons = bool(pg and pg.buttons_json)
    
    current_text = pg.guide_text if has_text else _guide_text(platform)
    media_info = f"📎 Медиа: <b>{pg.media_type}</b>" if has_media else "📎 Медиа: <i>не загружено</i>"
    from bot.handlers.mailing import _buttons_preview
    buttons_info = f"🔘 Кнопки:\n{_buttons_preview(pg.buttons_json)}" if has_buttons else "🔘 Кнопки: <i>не настроены</i>"
    text_info = f"📝 <b>Текст гайда:</b>\n───────────────────\n{current_text}\n───────────────────"
    text_status = "🟢 Используется собственный кастомный текст." if has_text else "⚪️ Используется стандартный текст по умолчанию."
    
    body = (
        f"📚 <b>Гайд: {PLATFORM_LABELS.get(platform, platform)}</b>\n\n"
        f"{text_status}\n"
        f"{media_info}\n"
        f"{buttons_info}\n\n"
        f"{text_info}"
    )
    await message.answer(
        body,
        reply_markup=guide_detail_kb(platform, has_media, has_text, has_buttons),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("adm_guide_etext_"))
async def admin_guide_edit_text_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    platform = callback.data[len("adm_guide_etext_"):]
    if platform not in PLATFORM_LABELS:
        await callback.answer("Платформа не найдена", show_alert=True)
        return
        
    await state.set_state(AdminStates.waiting_guide_text)
    await state.update_data(guide_platform=platform)
    
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Отмена", callback_data=f"adm_guide_{platform}")]
    ])
    
    await callback.message.edit_text(
        f"📝 <b>Редактирование текста гайда «{PLATFORM_LABELS[platform]}»</b>\n\n"
        f"Отправьте новый текст инструкции. Поддерживается HTML-разметка.\n\n"
        f"Или нажмите «◀️ Отмена» для возврата.",
        reply_markup=cancel_kb,
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminStates.waiting_guide_text, F.text)
async def admin_guide_text_received(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    platform = data.get("guide_platform", "")
    new_text = message.html_text
    
    try:
        await message.delete()
    except Exception:
        pass
        
    async with async_session() as session:
        pg = await session.get(PlatformGuide, platform)
        if pg:
            pg.guide_text = new_text
            pg.updated_at = datetime.utcnow()
        else:
            session.add(PlatformGuide(
                platform=platform,
                guide_text=new_text,
            ))
        await session.commit()
        
    await state.clear()
    
    # Send details screen
    from bot.handlers.start import _guide_text
    async with async_session() as session:
        pg = await session.get(PlatformGuide, platform)
    has_media = bool(pg and pg.media_file_id)
    has_text = bool(pg and pg.guide_text)
    has_buttons = bool(pg and pg.buttons_json)
    
    current_text = pg.guide_text if has_text else _guide_text(platform)
    media_info = f"📎 Медиа: <b>{pg.media_type}</b>" if has_media else "📎 Медиа: <i>не загружено</i>"
    from bot.handlers.mailing import _buttons_preview
    buttons_info = f"🔘 Кнопки:\n{_buttons_preview(pg.buttons_json)}" if has_buttons else "🔘 Кнопки: <i>не настроены</i>"
    text_info = f"📝 <b>Текст гайда:</b>\n───────────────────\n{current_text}\n───────────────────"
    text_status = "🟢 Используется собственный кастомный текст." if has_text else "⚪️ Используется стандартный текст по умолчанию."
    
    body = (
        f"📚 <b>Гайд: {PLATFORM_LABELS.get(platform, platform)}</b>\n\n"
        f"{text_status}\n"
        f"{media_info}\n"
        f"{buttons_info}\n\n"
        f"{text_info}"
    )
    await message.answer(
        body,
        reply_markup=guide_detail_kb(platform, has_media, has_text, has_buttons),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("adm_guide_rtext_"))
async def admin_guide_reset_text(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    platform = callback.data[len("adm_guide_rtext_"):]
    async with async_session() as session:
        pg = await session.get(PlatformGuide, platform)
        if pg:
            pg.guide_text = None
            pg.updated_at = datetime.utcnow()
            await session.commit()
            
    await callback.answer("Текст сброшен к стандартному", show_alert=False)
    await _show_guide_detail(callback, platform)


@router.callback_query(F.data.startswith("adm_guide_btns_"))
async def admin_guide_buttons_editor(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    platform = callback.data[len("adm_guide_btns_"):]
    if platform not in PLATFORM_LABELS:
        await callback.answer("Платформа не найдена", show_alert=True)
        return
        
    async with async_session() as session:
        pg = await session.get(PlatformGuide, platform)
        
    from bot.handlers.mailing import _parse_buttons, _btn_editor_kb
    buttons = _parse_buttons(pg.buttons_json if pg else None)
    
    await state.set_state(AdminStates.guide_buttons)
    await state.update_data(guide_platform=platform, buttons=buttons, btn_context="guide")
    
    btn_text = f"🔘 <b>Редактор кнопок для гайда «{PLATFORM_LABELS[platform]}»</b>\n\nДобавьте кнопки или нажмите «Готово»."
    btn_kb = _btn_editor_kb(buttons, f"adm_guide_bdone_{platform}")
    await callback.message.edit_text(btn_text, reply_markup=btn_kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("adm_guide_bdone_"), AdminStates.guide_buttons)
async def admin_guide_buttons_done(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    platform = callback.data[len("adm_guide_bdone_"):]
    data = await state.get_data()
    buttons = data.get("buttons", [])
    
    from bot.handlers.mailing import _buttons_to_json
    async with async_session() as session:
        pg = await session.get(PlatformGuide, platform)
        if pg:
            pg.buttons_json = _buttons_to_json(buttons)
            pg.updated_at = datetime.utcnow()
        else:
            session.add(PlatformGuide(
                platform=platform,
                buttons_json=_buttons_to_json(buttons),
            ))
        await session.commit()
        
    await state.clear()
    await callback.answer("Кнопки сохранены", show_alert=False)
    await _show_guide_detail(callback, platform)


@router.callback_query(F.data.startswith("adm_guide_prev_"))
async def admin_guide_preview(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    platform = callback.data[len("adm_guide_prev_"):]
    if platform not in PLATFORM_LABELS:
        await callback.answer("Платформа не найдена", show_alert=True)
        return
        
    await callback.answer("Гайд отправлен ниже", show_alert=False)
    
    from bot.handlers.start import _guide_text
    from bot.services.guide_service import send_guide
    await send_guide(
        callback.bot,
        callback.from_user.id,
        platform,
        _guide_text(platform),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад к редактированию", callback_data=f"adm_guide_{platform}")]
        ])
    )


# ── Payment Logs ───────────────────────────────────────

_LOG_LINES = 50  # number of tail lines to display
_MAX_MSG = 4000  # Telegram message char limit (safe margin)


def _truncate_escaped_log_body(body: str, limit: int) -> str:
    if len(body) <= limit:
        return body
    if limit <= 1:
        return "…"[:limit]

    truncated = body[: limit - 1]
    entity_start = truncated.rfind("&")
    entity_end = truncated.rfind(";")
    if entity_start > entity_end:
        truncated = truncated[:entity_start]
    return truncated.rstrip() + "…"


def _payment_logs_text(lines: list[str]) -> str:
    escaped_lines = [html.escape(str(line)) for line in lines]
    first_line = 0

    while True:
        display = escaped_lines[first_line:]
        heading = f"📋 <b>Последние {len(display)} событий оплаты</b>\n\n"
        omitted_prefix = "…\n" if first_line else ""
        body_limit = _MAX_MSG - len(heading) - len("<pre>") - len("</pre>") - len(omitted_prefix)
        body = "\n".join(display)

        if len(body) <= body_limit:
            return f"{heading}<pre>{omitted_prefix}{body}</pre>"
        if len(display) > 1:
            first_line += 1
            continue

        body = _truncate_escaped_log_body(body, body_limit)
        return f"{heading}<pre>{omitted_prefix}{body}</pre>"


@router.callback_query(F.data == "adm_logs")
async def admin_logs(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return

    from bot.services.payment_logger import get_payment_log_tail

    lines = get_payment_log_tail(_LOG_LINES)
    if not lines:
        text = "📋 <b>Логи оплат</b>\n\nФайл логов пуст или ещё не создан."
    else:
        text = _payment_logs_text(lines)

    back_kb = InlineKeyboardBuilder()
    back_kb.row(InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back"))
    try:
        await callback.message.edit_text(text, reply_markup=back_kb.as_markup(), parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=back_kb.as_markup(), parse_mode="HTML")
    await callback.answer()


# ── Webstore Stats ─────────────────────────────────────

@router.callback_query(F.data == "adm_stats_web")
async def admin_stats_web(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    if not settings.webstore_public_enabled:
        await callback.answer("Вебстор не подключён", show_alert=True)
        return

    import aiohttp as _aiohttp
    url = f"{settings.webstore_api_base_url.rstrip('/')}/api/store/internal/admin-stats"
    headers = {"X-Internal-Secret": settings.webstore_bridge_secret}
    try:
        async with _aiohttp.ClientSession(timeout=_aiohttp.ClientTimeout(total=10)) as sess:
            async with sess.get(url, headers=headers) as resp:
                if resp.status != 200:
                    await callback.answer(f"Ошибка вебстора: {resp.status}", show_alert=True)
                    return
                d = await resp.json()
    except Exception as e:
        await callback.answer(f"Не удалось связаться с вебстором: {e}", show_alert=True)
        return

    rev = d.get("revenue", {})
    sc = d.get("status_counts", {})
    ref = d.get("referrals", {})
    conv = d.get("conversion", {})
    total_30d = conv.get("total_30d", 0)
    paid_30d = conv.get("paid_30d", 0)
    conv_pct = round(paid_30d / total_30d * 100, 1) if total_30d else 0.0

    delivered = sc.get("delivered", 0)
    demo = sc.get("demo", 0)
    canceled = sc.get("canceled", 0)
    pending = sc.get("pending", 0)

    pending_line = f"  ⏳ В процессе оплаты: <b>{pending}</b>\n" if pending > 0 else ""

    text = (
        "🌐 <b>Вебстор — статистика</b>\n\n"
        "<b>Выручка с сайта:</b>\n"
        f"  Сегодня: <b>{rev.get('today', 0):,.0f} ₽</b>\n"
        f"  7 дней: <b>{rev.get('w7', 0):,.0f} ₽</b>\n"
        f"  30 дней: <b>{rev.get('w30', 0):,.0f} ₽</b>\n"
        f"  За всё время: <b>{rev.get('all', 0):,.0f} ₽</b>\n\n"
        "<b>Заказы и доступы:</b>\n"
        f"  ✅ Оплачено и выдано: <b>{delivered}</b>\n"
        f"  🎁 Демо-доступы: <b>{demo}</b>\n"
        f"  ❌ Не завершили оплату: <b>{canceled}</b>\n"
        f"{pending_line}\n"
        "<b>Конверсия чекаута (30 дней):</b>\n"
        f"  Создано заказов: <b>{total_30d}</b>\n"
        f"  Оплачено: <b>{paid_30d}</b>\n"
        f"  Конверсия: <b>{conv_pct}%</b>\n\n"
        "<b>Клиенты и рефералы:</b>\n"
        f"  👥 Профилей на сайте: <b>{d.get('total_profiles', 0)}</b>\n"
        f"  🔗 Оплат по реф. ссылке: <b>{ref.get('orders', 0)}</b>\n"
        f"  💰 Начислено бонусов: <b>{ref.get('credited_rub', 0):,.0f} ₽</b>"
    )

    back_kb = InlineKeyboardBuilder()
    back_kb.row(InlineKeyboardButton(text="👥 Список клиентов сайта", callback_data="adm_web_clients_list"))
    back_kb.row(InlineKeyboardButton(text="🔎 Поиск", callback_data="adm_user_search"))
    back_kb.row(InlineKeyboardButton(text="📋 Логи заказов", callback_data="adm_web_orders:all:1"))
    back_kb.row(InlineKeyboardButton(text="💳 Балансы", callback_data="adm_web_balance_search"))
    back_kb.row(InlineKeyboardButton(text="◀️ К статистике", callback_data="adm_stats"))

    try:
        await callback.message.edit_text(text, reply_markup=back_kb.as_markup(), parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=back_kb.as_markup(), parse_mode="HTML")
    await callback.answer()


def _build_web_orders_log_page(orders: list[dict], page: int) -> tuple[str, InlineKeyboardBuilder]:
    status_labels = {
        "pending": "⏳ ожидает оплаты",
        "paid": "💳 оплачен",
        "delivered": "✅ выдан",
        "canceled": "❌ отменён",
        "failed": "⚠️ ошибка выдачи",
        "demo": "🎁 демо",
    }
    page_size = 5
    total_pages = max(1, (len(orders) + page_size - 1) // page_size)
    current_page = ((page - 1) % total_pages) + 1
    start = (current_page - 1) * page_size
    page_orders = orders[start:start + page_size]

    def _res_prov(o: dict) -> str:
        pr = str(o.get("provider") or "").lower()
        if pr in ("adapt", "marzban", "vhq"):
            return pr.upper()
        sub = str(o.get("subscription_url") or o.get("raw_subscription_url") or "").lower()
        if "adapt" in sub:
            return "ADAPT"
        if "vhq" in sub or "proxy-subscription" in sub:
            return "VHQ"
        return "MARZBAN"

    blocks: list[str] = []
    for idx, o in enumerate(page_orders, start=start + 1):
        order_id = str(o.get("order_id") or "—")
        status = str(o.get("status") or "unknown")
        contact = o.get("contact") or o.get("email") or "контакт не указан"
        profile_token = str(o.get("profile_token") or "")
        profile_short = profile_token[:10] + "…" if len(profile_token) > 10 else profile_token
        amount = o.get("amount_rub") or 0
        original_amount = o.get("original_amount_rub") or amount
        bonus = o.get("bonus_applied_rub") or 0
        price = f"{amount} ₽" if not bonus else f"{amount} ₽ из {original_amount} ₽, бонус {bonus} ₽"
        tariff = o.get("tariff_label") or o.get("tariff_key") or "тариф не указан"
        if o.get("days"):
            tariff = f"{tariff}, {o.get('days')} дн."
        prov = _res_prov(o)
        created_msk = _fmt_dt_str_msk(o.get("created_at"))

        lines = [
            f"<b>#{idx}</b> | {html.escape(status_labels.get(status, status))} | {html.escape(created_msk)}",
            f"Заказ: <code>{html.escape(order_id)}</code>",
            f"Клиент: <code>{html.escape(str(contact))}</code>",
            f"Профиль: <code>{html.escape(profile_short or '—')}</code>",
            f"Тариф: {html.escape(str(tariff))}",
            f"Сумма: {html.escape(str(price))}",
            f"Провайдер: <b>{prov}</b>",
        ]
        if o.get("paid_at"):
            lines.append(f"Оплачен: {html.escape(_fmt_dt_str_msk(o.get('paid_at')))}")
        if o.get("delivered_at"):
            lines.append(f"Выдан: {html.escape(_fmt_dt_str_msk(o.get('delivered_at')))}")
        if o.get("access_expires_at"):
            lines.append(f"Доступ до: {html.escape(_fmt_dt_str_msk(o.get('access_expires_at')))}")
        if o.get("yookassa_payment_id"):
            lines.append(f"Платёж: <code>{html.escape(str(o.get('yookassa_payment_id')))}</code>")
        if o.get("subscription_url"):
            url = str(o.get("subscription_url"))
            lines.append(_format_vhq_mirror_key_block(url))
        if o.get("failure_message"):
            lines.append(f"Ошибка: {html.escape(str(o.get('failure_message')))}")
        blocks.append("\n".join(lines))

    divider = "\n━━━━━━━━━━━━━━━━━━━━\n"
    text = f"📋 <b>Логи заказов сайта</b> ({len(orders)} всего)\nСтраница <b>{current_page}/{total_pages}</b>\n\n" + divider.join(blocks)
    kb = InlineKeyboardBuilder()
    prev_page = total_pages if current_page == 1 else current_page - 1
    next_page = 1 if current_page == total_pages else current_page + 1
    kb.row(
        InlineKeyboardButton(text="◀️ Назад", callback_data=f"adm_web_orders_log_{prev_page}"),
        InlineKeyboardButton(text=f"Стр. {current_page}/{total_pages}", callback_data="ignore"),
        InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"adm_web_orders_log_{next_page}"),
    )
    kb.row(InlineKeyboardButton(text="◀️ К статистике", callback_data="adm_stats_web"))
    return text, kb


@router.callback_query(F.data.regexp(r"^adm_web_clients_list(?:_\d+)?$"))
async def admin_web_clients_list(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    if not settings.webstore_public_enabled:
        await callback.answer("Вебстор не подключён", show_alert=True)
        return

    page = 1
    parts = callback.data.rsplit("_", 1)
    if len(parts) == 2 and parts[-1].isdigit():
        page = max(1, int(parts[-1]))

    import aiohttp as _aiohttp
    url = f"{settings.webstore_api_base_url.rstrip('/')}/api/store/internal/admin-clients-list?page={page}"
    headers = {"X-Internal-Secret": settings.webstore_bridge_secret}
    try:
        async with _aiohttp.ClientSession(timeout=_aiohttp.ClientTimeout(total=10)) as sess:
            async with sess.get(url, headers=headers) as resp:
                if resp.status != 200:
                    await callback.answer(f"Ошибка: {resp.status}", show_alert=True)
                    return
                d = await resp.json()
    except Exception as e:
        await callback.answer(f"Ошибка: {e}", show_alert=True)
        return

    clients = d.get("clients", [])
    total = d.get("total", 0)
    limit = d.get("limit", 10)
    total_pages = max(1, (total + limit - 1) // limit)
    current_page = max(1, min(page, total_pages))

    if not clients:
        await callback.answer("Клиентов нет", show_alert=True)
        return

    start_idx = (current_page - 1) * limit + 1
    blocks = []
    kb = InlineKeyboardBuilder()

    for idx, c in enumerate(clients, start=start_idx):
        contact = html.escape(str(c.get("contact") or "—"))
        token = str(c.get("profile_token") or "")
        short_token = token[:10]
        created = _fmt_dt_str_msk(c.get("created_at"))
        source = html.escape(str(c.get("traffic_source") or "—"))
        paid_sum = c.get("paid_sum") or 0
        paid_count = c.get("paid_count") or 0
        bal = c.get("balance_rub") or 0.0

        tg = c.get("telegram")
        tg_info = f" | TG: @{html.escape(str(tg.get('username')))}" if (tg and tg.get("username")) else ""

        blocks.append(
            f"<b>#{idx}. 👤 {contact}</b>{tg_info}\n"
            f"├ Регистрация: {created}\n"
            f"├ Источник: {source}\n"
            f"├ Баланс: <b>{bal:.2f} ₽</b> | Оплат: <b>{paid_sum} ₽</b> ({paid_count} шт.)\n"
            f"└ Профиль: <code>{token}</code>"
        )
        kb.row(InlineKeyboardButton(text=f"🔍 Карточка #{idx}: {contact[:20]}", callback_data=f"adm_wb_prof_q:{token[:16]}:{current_page}"))

    divider = "\n━━━━━━━━━━━━━━━━━━━━\n"
    text = (
        f"👥 <b>Список клиентов сайта</b> ({total} всего)\n"
        f"Страница <b>{current_page}/{total_pages}</b>\n\n"
        + divider.join(blocks)
    )

    prev_page = total_pages if current_page == 1 else current_page - 1
    next_page = 1 if current_page == total_pages else current_page + 1

    kb.row(
        InlineKeyboardButton(text="◀️ Назад", callback_data=f"adm_web_clients_list_{prev_page}"),
        InlineKeyboardButton(text=f"Стр. {current_page}/{total_pages}", callback_data="ignore"),
        InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"adm_web_clients_list_{next_page}"),
    )
    kb.row(InlineKeyboardButton(text="◀️ К статистике", callback_data="adm_stats_web"))

    try:
        await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("adm_wb_prof_q:"))
async def admin_web_client_quick_lookup(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    raw = callback.data.split(":", 1)[1].strip()
    if ":" in raw:
        query, from_page = raw.split(":", 1)
    else:
        query, from_page = raw, "1"

    import aiohttp as _aiohttp
    url = f"{settings.webstore_api_base_url.rstrip('/')}/api/store/internal/admin-client-lookup?q={query}"
    headers = {"X-Internal-Secret": settings.webstore_bridge_secret}
    try:
        async with _aiohttp.ClientSession(timeout=_aiohttp.ClientTimeout(total=10)) as sess:
            async with sess.get(url, headers=headers) as resp:
                if resp.status != 200:
                    await callback.answer(f"Ошибка: {resp.status}", show_alert=True)
                    return
                d = await resp.json()
    except Exception as e:
        await callback.answer(f"Ошибка: {e}", show_alert=True)
        return

    profiles = d.get("profiles") or []
    if not profiles:
        await callback.answer("Клиент не найден", show_alert=True)
        return

    p = profiles[0]
    orders = p.get("orders") or []
    items = p.get("telegram_items") or []

    created_str = _fmt_dt_str_msk(p.get("created_at"))
    if created_str == "—" and orders:
        order_dts = [o.get("created_at") for o in orders if o.get("created_at")]
        if order_dts:
            created_str = _fmt_dt_str_msk(min(order_dts))

    source_str = "—"
    for o in orders:
        ref = o.get("entry_referrer") or o.get("ref_source") or ""
        url_s = o.get("entry_url") or ""
        if "vk.ru" in ref or "vk.com" in ref or "vk" in url_s:
            source_str = "ВКонтакте (VK)"
            break
        elif ref or url_s:
            source_str = html.escape(ref or url_s)[:35]
            break

    total_paid_rub = sum(o.get("amount_rub") or 0 for o in orders if o.get("status") in ("delivered", "paid"))

    ref_id = p.get("referrer_telegram_id")
    referrer_str = "Нет (прямой заход)"
    if ref_id:
        try:
            async with async_session() as bot_sess:
                partner = await bot_sess.scalar(select(Partner).where(Partner.telegram_id == int(ref_id)))
                if partner:
                    referrer_str = f"{html.escape(partner.name)} [{ref_id}]"
                else:
                    inviter = await bot_sess.scalar(select(User).where(User.telegram_id == int(ref_id)))
                    if inviter:
                        u_name = f"@{inviter.username}" if inviter.username else (inviter.first_name or "Пользователь")
                        referrer_str = f"{html.escape(u_name)} [{ref_id}]"
                    else:
                        referrer_str = f"ID: {ref_id}"
        except Exception:
            referrer_str = f"ID: {ref_id}"

    refs_count = p.get("referrals_count") or 0

    lines = [
        "🌐 <b>Клиент сайта</b>",
        f"Контакт: <code>{html.escape(str(p.get('contact') or '—'))}</code>",
        f"Профиль: <code>{html.escape(str(p.get('profile_token') or '—'))}</code>",
        f"Регистрация: <b>{created_str}</b>",
        f"Источник: <b>{source_str}</b>",
        f"Приглашён кем: <b>{referrer_str}</b>",
        f"Рефералов приведено: <b>{refs_count} чел.</b>",
        f"Пароль задан: {'да' if p.get('has_password') else 'нет'}",
        f"Баланс сайта: <b>{float(p.get('balance_rub') or 0):.2f} ₽</b>",
        f"Оплачено всего: <b>{total_paid_rub} ₽</b>",
    ]
    tg = p.get("telegram")
    if tg:
        lines.append(
            "Telegram: "
            f"<code>{html.escape(str(tg.get('id') or ''))}</code> "
            f"@{html.escape(str(tg.get('username') or ''))}"
        )

    delivered_orders = [o for o in orders if o.get("status") in ("delivered", "paid")]
    demo_orders = [o for o in orders if o.get("status") == "demo" or o.get("tariff_key") == "demo"]
    pending_orders = [o for o in orders if o.get("status") == "pending"]
    failed_orders = [o for o in orders if o.get("status") == "failed"]

    def _resolve_prov_name(o: dict) -> str:
        pr = str(o.get("provider") or "").lower()
        if pr in ("adapt", "marzban", "vhq"):
            return pr.upper()
        sub = str(o.get("subscription_url") or o.get("raw_subscription_url") or "").lower()
        if "adapt" in sub:
            return "ADAPT"
        if "vhq" in sub or "proxy-subscription" in sub:
            return "VHQ"
        return "MARZBAN"

    if delivered_orders:
        lines.append("\n🟢 <b>Оплаченные подписки и ключи</b>")
        for o in delivered_orders:
            sub = o.get("subscription_url") or o.get("raw_subscription_url")
            prov = _resolve_prov_name(o)
            exp = _fmt_dt_str_msk(o.get("access_expires_at"))
            lines.append(
                f"✅ <code>{html.escape(str(o.get('order_id') or ''))}</code> | "
                f"{html.escape(str(o.get('tariff_label') or ''))} | {o.get('amount_rub') or 0} ₽ | <b>{prov}</b>"
            )
            if exp != "—":
                lines.append(f"└ Доступ до: <code>{html.escape(exp)}</code>")
            if sub:
                lines.append(await _format_external_key_block(str(sub), label=f"Ссылка ({prov})"))

    if demo_orders:
        lines.append("\n🎁 <b>Выданные демо-ключи</b>")
        for o in demo_orders:
            sub = o.get("subscription_url") or o.get("raw_subscription_url")
            prov = _resolve_prov_name(o)
            created = _fmt_dt_str_msk(o.get("created_at"))
            exp = _fmt_dt_str_msk(o.get("access_expires_at"))
            lines.append(
                f"🎁 <code>{html.escape(str(o.get('order_id') or ''))}</code> | "
                f"Провайдер: <b>{prov}</b> | Создан: {html.escape(created)}"
            )
            if exp != "—":
                lines.append(f"└ Срок до: <code>{html.escape(exp)}</code>")
            if sub:
                lines.append(await _format_external_key_block(str(sub), label=f"Ссылка демо ({prov})"))

    if failed_orders:
        lines.append("\n⚠️ <b>Ошибки выдачи</b>")
        for o in failed_orders[:3]:
            lines.append(
                f"⚠️ <code>{html.escape(str(o.get('order_id') or ''))}</code> | {html.escape(str(o.get('failure_message') or 'Ошибка'))}"
            )

    if pending_orders:
        lines.append(f"\n⏳ <b>Неоплаченные попытки:</b> {len(pending_orders)} шт. (счета созданы, карта не введена)")
        for o in pending_orders[:3]:
            created = _fmt_dt_str_msk(o.get("created_at"))
            lines.append(
                f"• <code>{html.escape(str(o.get('order_id') or ''))}</code> | {html.escape(str(o.get('tariff_label') or ''))} ({o.get('amount_rub')} ₽) | {html.escape(created)}"
            )
        if len(pending_orders) > 3:
            lines.append(f"<i>...и ещё {len(pending_orders) - 3} неоплаченных попыток</i>")

    if not orders and not items:
        lines.append("\n<i>Заказов и ключей нет</i>")

    back_kb = InlineKeyboardBuilder()
    back_kb.row(InlineKeyboardButton(text="👥 К списку клиентов", callback_data=f"adm_web_clients_list_{from_page}"))
    back_kb.row(InlineKeyboardButton(text="◀️ Меню сайта", callback_data="adm_stats_web"))

    text = "\n".join(lines)
    try:
        await callback.message.edit_text(text, reply_markup=back_kb.as_markup(), parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=back_kb.as_markup(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.regexp(r"^adm_web_orders_log(?:_\d+)?$"))
async def admin_web_orders_log(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    page = 1
    parts = callback.data.rsplit("_", 1)
    if len(parts) == 2 and parts[-1].isdigit():
        page = max(1, int(parts[-1]))
    synthetic = callback.model_copy(update={"data": f"adm_web_orders:all:{page}"})
    await admin_web_orders(synthetic)


async def _webstore_admin_request(method: str, path: str, *, params=None, json_body=None, timeout=15) -> dict:
    import aiohttp as _aiohttp
    url = f"{settings.webstore_api_base_url.rstrip('/')}{path}"
    headers = {"X-Internal-Secret": settings.webstore_bridge_secret}
    async with _aiohttp.ClientSession(timeout=_aiohttp.ClientTimeout(total=timeout)) as sess:
        async with sess.request(method, url, headers=headers, params=params, json=json_body) as resp:
            data = await resp.json()
            if resp.status != 200:
                raise RuntimeError(str(data.get("error") or f"HTTP {resp.status}"))
            return data


def _web_order_status_label(status: str) -> str:
    return {
        "pending": "⏳ Ожидает оплаты",
        "paid": "💳 Оплачен, выдаётся",
        "delivered": "✅ Выдан",
        "failed": "⚠️ Ошибка выдачи",
        "canceled": "❌ Отменён",
        "demo": "🎁 Демо",
    }.get(status, status)


@router.callback_query(F.data.regexp(r"^adm_web_orders:[a-z]+:\d+$"))
async def admin_web_orders(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    if not settings.webstore_public_enabled:
        await callback.answer("Вебстор не подключён", show_alert=True)
        return
    _, status_filter, page_raw = callback.data.split(":")
    await callback.answer()
    try:
        data = await _webstore_admin_request(
            "GET", "/api/store/internal/admin-orders",
            params={"status": status_filter, "page": int(page_raw)},
        )
    except Exception as exc:
        await callback.message.answer(
            f"❌ Не удалось получить заказы сайта: {html.escape(str(exc))}\n"
            "Повторите позже или напишите в поддержку.",
            reply_markup=admin_back_kb(),
            parse_mode="HTML",
        )
        return

    orders = data.get("orders") or []
    page = int(data.get("page") or 1)
    total_pages = int(data.get("total_pages") or 1)
    blocks: list[str] = []
    kb = InlineKeyboardBuilder()
    for idx, order in enumerate(orders, start=(page - 1) * int(data.get("limit") or 5) + 1):
        status = str(order.get("status") or "")
        action_label = {"new": "Создание", "renew": "Продление", "upgrade": "Улучшение"}.get(
            str(order.get("purchase_action") or "new"), str(order.get("purchase_action") or "new")
        )
        block = [
            f"<b>#{idx} · {_web_order_status_label(status)}</b>",
            f"Заказ: <code>{html.escape(str(order.get('order_id') or '—'))}</code>",
            f"Клиент: <code>{html.escape(str(order.get('contact') or '—'))}</code>",
            f"Тариф: {html.escape(str(order.get('tariff_label') or '—'))}, {order.get('days') or 0} дн.",
            f"Сумма: <b>{order.get('amount_rub') or 0} ₽</b> · Операция: {html.escape(action_label)}",
            f"Провайдер: <b>{html.escape(str(order.get('provider') or '—').upper())}</b>",
            f"Создан: {_fmt_dt_str_msk(order.get('created_at'))}",
        ]
        if order.get("failure_message"):
            block.append(f"Ошибка: {html.escape(str(order.get('failure_message')))[:300]}")
        blocks.append("\n".join(block))
        kb.row(InlineKeyboardButton(
            text=f"Открыть #{idx} · {_web_order_status_label(status)}",
            callback_data=f"adm_worder:{order.get('id')}:{status_filter}:{page}",
        ))

    filters = [
        ("Все", "all"), ("⚠️ Требуют внимания", "attention"),
        ("⏳ Ожидают", "pending"), ("✅ Выданы", "delivered"),
    ]
    for label, value in filters:
        prefix = "• " if value == status_filter else ""
        kb.row(InlineKeyboardButton(text=prefix + label, callback_data=f"adm_web_orders:{value}:1"))
    kb.row(
        InlineKeyboardButton(text="◀️", callback_data=f"adm_web_orders:{status_filter}:{max(1, page - 1)}"),
        InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="ignore"),
        InlineKeyboardButton(text="▶️", callback_data=f"adm_web_orders:{status_filter}:{min(total_pages, page + 1)}"),
    )
    kb.row(InlineKeyboardButton(text="🔎 Поиск", callback_data="adm_user_search"))
    kb.row(InlineKeyboardButton(text="◀️ Меню сайта", callback_data="adm_stats_web"))
    divider = "\n━━━━━━━━━━━━━━━━━━━━\n"
    text_value = (
        f"📋 <b>Логи заказов сайта</b>\n"
        f"Найдено: <b>{data.get('total') or 0}</b> · Страница <b>{page}/{total_pages}</b>\n\n"
        + (divider.join(blocks) if blocks else "<i>Заказов в этом разделе нет.</i>")
    )
    try:
        await callback.message.edit_text(text_value, reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception:
        await callback.message.answer(text_value, reply_markup=kb.as_markup(), parse_mode="HTML")


async def _render_web_order_detail(callback: CallbackQuery, db_id: int, status_filter: str, page: int) -> None:
    try:
        data = await _webstore_admin_request(
            "GET", "/api/store/internal/admin-order", params={"id": db_id}
        )
    except Exception as exc:
        await callback.message.answer(
            f"❌ Не удалось открыть заказ: {html.escape(str(exc))}\n"
            "Повторите позже или напишите в поддержку.",
            parse_mode="HTML",
        )
        return
    order = data["order"]
    action_label = {"new": "Создание подписки", "renew": "Продление", "upgrade": "Улучшение"}.get(
        str(order.get("purchase_action") or "new"), str(order.get("purchase_action") or "new")
    )
    lines = [
        "📦 <b>Заказ сайта</b>",
        f"Статус: <b>{_web_order_status_label(str(order.get('status') or ''))}</b>",
        f"Номер: <code>{html.escape(str(order.get('order_id') or '—'))}</code>",
        f"Клиент: <code>{html.escape(str(order.get('contact') or '—'))}</code>",
        f"Тариф: <b>{html.escape(str(order.get('tariff_label') or '—'))}</b>, {order.get('days') or 0} дн.",
        f"Сумма: <b>{order.get('amount_rub') or 0} ₽</b>",
        f"Операция: {html.escape(action_label)}",
        f"Провайдер: <b>{html.escape(str(order.get('provider') or '—').upper())}</b>",
        f"Создан: {_fmt_dt_str_msk(order.get('created_at'))}",
        f"Оплачен: {_fmt_dt_str_msk(order.get('paid_at'))}",
        f"Выдан: {_fmt_dt_str_msk(order.get('delivered_at'))}",
        f"Доступ до: {_fmt_dt_str_msk(order.get('access_expires_at'))}",
        f"Попыток выдачи: <b>{order.get('fulfillment_attempts') or 0}</b>",
        f"Последняя попытка: {_fmt_dt_str_msk(order.get('last_fulfillment_attempt_at'))}",
        f"Следующая попытка: {_fmt_dt_str_msk(order.get('next_fulfillment_retry_at'))}",
    ]
    if order.get("target_order_id"):
        lines.append(f"Исходная подписка: <code>{html.escape(str(order.get('target_order_id')))}</code>")
    if order.get("yookassa_payment_id"):
        lines.append(f"Платёж: <code>{html.escape(str(order.get('yookassa_payment_id')))}</code>")
    if order.get("failure_code") or order.get("failure_message"):
        lines.append("\n⚠️ <b>Ошибка выдачи</b>")
        if order.get("failure_code"):
            lines.append(f"Код: <code>{html.escape(str(order.get('failure_code')))}</code>")
        if order.get("failure_message"):
            lines.append(f"Для клиента: {html.escape(str(order.get('failure_message')))[:500]}")
        if order.get("failure_reason"):
            lines.append(f"Технически: {html.escape(str(order.get('failure_reason')))[:700]}")
    subscription_url = order.get("subscription_url") or order.get("raw_subscription_url")
    if subscription_url:
        lines.append("\n" + await _format_external_key_block(str(subscription_url), label="Ссылка подписки"))

    kb = InlineKeyboardBuilder()
    if order.get("can_retry"):
        lines.append("\n<i>Можно повторить выдачу: оплата подтверждена, доступ ещё не выдан.</i>")
        kb.row(InlineKeyboardButton(text="🔄 Повторить выдачу", callback_data=f"adm_woact:retry:{db_id}:{status_filter}:{page}"))
    if order.get("can_resend"):
        lines.append("<i>Можно повторно отправить клиенту уведомление о готовом доступе.</i>")
        kb.row(InlineKeyboardButton(text="📨 Повторить уведомление", callback_data=f"adm_woact:resend:{db_id}:{status_filter}:{page}"))
    if order.get("can_cancel"):
        lines.append(
            "<i>Можно отменить запись неоплаченного заказа. Если платёж подтвердится позже, "
            "система обработает его штатно.</i>"
        )
        kb.row(InlineKeyboardButton(text="❌ Отменить неоплаченный", callback_data=f"adm_woact:cancel:{db_id}:{status_filter}:{page}"))
    if not any(order.get(key) for key in ("can_retry", "can_resend", "can_cancel")):
        lines.append("\n<i>Для текущего состояния заказа действий нет.</i>")
    kb.row(InlineKeyboardButton(text="🔄 Обновить", callback_data=f"adm_worder:{db_id}:{status_filter}:{page}"))
    kb.row(InlineKeyboardButton(text="◀️ К заказам", callback_data=f"adm_web_orders:{status_filter}:{page}"))
    try:
        await callback.message.edit_text("\n".join(lines), reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception:
        await callback.message.answer("\n".join(lines), reply_markup=kb.as_markup(), parse_mode="HTML")


@router.callback_query(F.data.regexp(r"^adm_worder:\d+:[a-z]+:\d+$"))
async def admin_web_order_detail(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    _, db_id, status_filter, page = callback.data.split(":")
    await callback.answer()
    await _render_web_order_detail(callback, int(db_id), status_filter, int(page))


@router.callback_query(F.data.regexp(r"^adm_woact:(retry|resend|cancel):\d+:[a-z]+:\d+$"))
async def admin_web_order_action(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    _, action, db_id, status_filter, page = callback.data.split(":")
    action_labels = {"retry": "Повтор выдачи", "resend": "Повтор уведомления", "cancel": "Отмена заказа"}
    await callback.answer("Выполняю…")
    try:
        current = await _webstore_admin_request(
            "GET", "/api/store/internal/admin-order", params={"id": int(db_id)}
        )
        order_id = str(current["order"]["order_id"])
        await callback.message.edit_text(
            f"⏳ <b>{action_labels[action]}</b>\n\nЗаказ: <code>{html.escape(order_id)}</code>\nОжидайте результат…",
            parse_mode="HTML",
        )
        result = await _webstore_admin_request(
            "POST", "/api/store/internal/admin-order-action",
            json_body={"order_id": order_id, "action": action}, timeout=45,
        )
        after = result.get("order") or {}
        action_warnings = result.get("warnings") or []
        await write_admin_audit(
            actor_telegram_id=callback.from_user.id,
            action=f"web_order_{action}",
            entity_type="web_order",
            entity_id=order_id,
            status="partial" if action_warnings else "success",
            summary=(
                f"{action_labels[action]} выполнен с предупреждением"
                if action_warnings else f"{action_labels[action]} выполнен"
            ),
            details=(
                f"Статус: {(result.get('before') or {}).get('status')} → {after.get('status')}; "
                f"попыток: {after.get('fulfillment_attempts', 0)}"
                + (f"; предупреждения: {'; '.join(map(str, action_warnings))}" if action_warnings else "")
            ),
        )
        if action_warnings:
            await callback.message.answer(
                "⚠️ Доступ сохранён в профиле, но часть завершающих действий требует внимания:\n"
                + "\n".join(f"• {html.escape(str(item))}" for item in action_warnings),
                parse_mode="HTML",
            )
    except Exception as exc:
        await write_admin_audit(
            actor_telegram_id=callback.from_user.id,
            action=f"web_order_{action}",
            entity_type="web_order",
            entity_id=locals().get("order_id") or str(db_id),
            status="error",
            summary=f"{action_labels[action]} не выполнен",
            details=str(exc)[:1000],
        )
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="🔄 Вернуться к заказу", callback_data=f"adm_worder:{db_id}:{status_filter}:{page}"))
        kb.row(InlineKeyboardButton(text="🆘 Поддержка", url=settings.support_url)) if settings.support_url else None
        await callback.message.edit_text(
            f"❌ <b>Действие не выполнено</b>\n\n{html.escape(str(exc))}\n\n"
            "Состояние заказа не скрыто и не потеряно. Повторите попытку или напишите в поддержку.",
            reply_markup=kb.as_markup(), parse_mode="HTML",
        )
        return
    await _render_web_order_detail(callback, int(db_id), status_filter, int(page))


# ── Web Balance Admin ──────────────────────────────────

class WebBalanceAdminState(StatesGroup):
    waiting_query = State()
    waiting_amount = State()
    waiting_client_query = State()


@router.callback_query(F.data == "adm_web_client_search")
async def admin_web_client_search_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    if not settings.webstore_public_enabled:
        await callback.answer("Вебстор не подключён", show_alert=True)
        return
    await state.set_state(WebBalanceAdminState.waiting_client_query)
    back_kb = InlineKeyboardBuilder()
    back_kb.row(InlineKeyboardButton(text="◀️ Отмена", callback_data="adm_stats_web"))
    await callback.message.edit_text(
        "🔎 <b>Поиск клиента сайта</b>\n\n"
        "Введите телефон, email, токен профиля или номер заказа:",
        reply_markup=back_kb.as_markup(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(WebBalanceAdminState.waiting_client_query)
async def admin_web_client_query(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    await state.clear()

    import aiohttp as _aiohttp
    query = message.text.strip()
    url = f"{settings.webstore_api_base_url.rstrip('/')}/api/store/internal/admin-client-lookup"
    headers = {"X-Internal-Secret": settings.webstore_bridge_secret}
    try:
        async with _aiohttp.ClientSession(timeout=_aiohttp.ClientTimeout(total=10)) as sess:
            async with sess.get(url, headers=headers, params={"q": query}) as resp:
                data = await resp.json()
                if resp.status == 404:
                    await message.answer("❌ Клиент сайта не найден.")
                    return
                if resp.status != 200:
                    await message.answer(f"Ошибка вебстора: {data.get('error', resp.status)}")
                    return
    except Exception as e:
        await message.answer(f"Ошибка соединения с вебстором: {e}")
        return

    profiles = data.get("profiles") or []
    if not profiles:
        await message.answer("❌ Клиент сайта не найден.")
        return

    chunks = []
    for p in profiles[:3]:
        orders = p.get("orders") or []
        items = p.get("telegram_items") or []

        created_str = _fmt_dt_str_msk(p.get("created_at"))
        if created_str == "—" and orders:
            order_dts = [o.get("created_at") for o in orders if o.get("created_at")]
            if order_dts:
                created_str = _fmt_dt_str_msk(min(order_dts))

        # Detect traffic source from orders
        source_str = "—"
        for o in orders:
            ref = o.get("entry_referrer") or o.get("ref_source") or ""
            url = o.get("entry_url") or ""
            if "vk.ru" in ref or "vk.com" in ref or "vk" in url:
                source_str = "ВКонтакте (VK)"
                break
            elif ref or url:
                source_str = html.escape(ref or url)[:35]
                break

        total_paid_rub = sum(o.get("amount_rub") or 0 for o in orders if o.get("status") in ("delivered", "paid"))

        ref_id = p.get("referrer_telegram_id")
        referrer_str = "Нет (прямой заход)"
        if ref_id:
            try:
                async with async_session() as bot_sess:
                    partner = await bot_sess.scalar(select(Partner).where(Partner.telegram_id == int(ref_id)))
                    if partner:
                        referrer_str = f"{html.escape(partner.name)} [{ref_id}]"
                    else:
                        inviter = await bot_sess.scalar(select(User).where(User.telegram_id == int(ref_id)))
                        if inviter:
                            u_name = f"@{inviter.username}" if inviter.username else (inviter.first_name or "Пользователь")
                            referrer_str = f"{html.escape(u_name)} [{ref_id}]"
                        else:
                            referrer_str = f"ID: {ref_id}"
            except Exception:
                referrer_str = f"ID: {ref_id}"

        refs_count = p.get("referrals_count") or 0

        lines = [
            "🌐 <b>Клиент сайта</b>",
            f"Контакт: <code>{html.escape(str(p.get('contact') or '—'))}</code>",
            f"Профиль: <code>{html.escape(str(p.get('profile_token') or '—'))}</code>",
            f"Регистрация: <b>{created_str}</b>",
            f"Источник: <b>{source_str}</b>",
            f"Приглашён кем: <b>{referrer_str}</b>",
            f"Рефералов приведено: <b>{refs_count} чел.</b>",
            f"Пароль задан: {'да' if p.get('has_password') else 'нет'}",
            f"Баланс сайта: <b>{float(p.get('balance_rub') or 0):.2f} ₽</b>",
            f"Оплачено всего: <b>{total_paid_rub} ₽</b>",
        ]
        tg = p.get("telegram")
        if tg:
            lines.append(
                "Telegram: "
                f"<code>{html.escape(str(tg.get('id') or ''))}</code> "
                f"@{html.escape(str(tg.get('username') or ''))}"
            )

        delivered_orders = [o for o in orders if o.get("status") in ("delivered", "paid")]
        demo_orders = [o for o in orders if o.get("status") == "demo" or o.get("tariff_key") == "demo"]
        pending_orders = [o for o in orders if o.get("status") == "pending"]
        failed_orders = [o for o in orders if o.get("status") == "failed"]

        def _resolve_prov_name(o: dict) -> str:
            pr = str(o.get("provider") or "").lower()
            if pr in ("adapt", "marzban", "vhq"):
                return pr.upper()
            sub = str(o.get("subscription_url") or o.get("raw_subscription_url") or "").lower()
            if "adapt" in sub:
                return "ADAPT"
            if "vhq" in sub or "proxy-subscription" in sub:
                return "VHQ"
            return "MARZBAN"

        if delivered_orders:
            lines.append("\n🟢 <b>Оплаченные подписки и ключи</b>")
            for o in delivered_orders:
                sub = o.get("subscription_url") or o.get("raw_subscription_url")
                prov = _resolve_prov_name(o)
                exp = _fmt_dt_str_msk(o.get("access_expires_at"))
                lines.append(
                    f"✅ <code>{html.escape(str(o.get('order_id') or ''))}</code> | "
                    f"{html.escape(str(o.get('tariff_label') or ''))} | {o.get('amount_rub') or 0} ₽ | <b>{prov}</b>"
                )
                if exp != "—":
                    lines.append(f"└ Доступ до: <code>{html.escape(exp)}</code>")
                if sub:
                    lines.append(await _format_external_key_block(str(sub), label=f"Ссылка ({prov})"))

        if demo_orders:
            lines.append("\n🎁 <b>Выданные демо-ключи</b>")
            for o in demo_orders:
                sub = o.get("subscription_url") or o.get("raw_subscription_url")
                prov = _resolve_prov_name(o)
                created = _fmt_dt_str_msk(o.get("created_at"))
                exp = _fmt_dt_str_msk(o.get("access_expires_at"))
                lines.append(
                    f"🎁 <code>{html.escape(str(o.get('order_id') or ''))}</code> | "
                    f"Провайдер: <b>{prov}</b> | Создан: {html.escape(created)}"
                )
                if exp != "—":
                    lines.append(f"└ Срок до: <code>{html.escape(exp)}</code>")
                if sub:
                    lines.append(await _format_external_key_block(str(sub), label=f"Ссылка демо ({prov})"))

        if failed_orders:
            lines.append("\n⚠️ <b>Ошибки выдачи</b>")
            for o in failed_orders[:3]:
                lines.append(
                    f"⚠️ <code>{html.escape(str(o.get('order_id') or ''))}</code> | {html.escape(str(o.get('failure_message') or 'Ошибка'))}"
                )

        if pending_orders:
            lines.append(f"\n⏳ <b>Неоплаченные попытки:</b> {len(pending_orders)} шт. (счета созданы, карта не введена)")
            for o in pending_orders[:3]:
                created = _fmt_dt_str_msk(o.get("created_at"))
                lines.append(
                    f"• <code>{html.escape(str(o.get('order_id') or ''))}</code> | {html.escape(str(o.get('tariff_label') or ''))} ({o.get('amount_rub')} ₽) | {html.escape(created)}"
                )
            if len(pending_orders) > 3:
                lines.append(f"<i>...и ещё {len(pending_orders) - 3} неоплаченных попыток</i>")

        if not orders and not items:
            lines.append("\n<i>Заказов и ключей нет</i>")

        if items:
            lines.append("\n<b>Ключи из Telegram-профиля</b>")
            for item in items[:5]:
                key_block = await _format_external_key_block(
                    str(item.get("key_value") or ""),
                    label=str(item.get("title") or "Ключ"),
                )
                lines.append(f"{html.escape(str(item.get('title') or ''))}:\n{key_block}")
        chunks.append("\n".join(lines))

    back_kb = InlineKeyboardBuilder()
    back_kb.row(InlineKeyboardButton(text="🔎 Искать ещё", callback_data="adm_web_client_search"))
    back_kb.row(InlineKeyboardButton(text="◀️ Вебстор", callback_data="adm_stats_web"))
    for idx, chunk in enumerate(chunks):
        reply_markup = back_kb.as_markup() if idx == len(chunks) - 1 else None
        await message.answer(chunk, reply_markup=reply_markup, parse_mode="HTML")


@router.callback_query(F.data == "adm_web_balance_search")
async def admin_web_balance_search_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    if not settings.webstore_public_enabled:
        await callback.answer("Вебстор не подключён", show_alert=True)
        return
    await state.set_state(WebBalanceAdminState.waiting_query)
    back_kb = InlineKeyboardBuilder()
    back_kb.row(InlineKeyboardButton(text="◀️ Отмена", callback_data="adm_stats_web"))
    await callback.message.edit_text(
        "💳 <b>Управление балансом</b>\n\nВведите TG ID или email пользователя:",
        reply_markup=back_kb.as_markup(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(WebBalanceAdminState.waiting_query)
async def admin_web_balance_query(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return

    import aiohttp as _aiohttp
    query = message.text.strip()
    url = f"{settings.webstore_api_base_url.rstrip('/')}/api/store/internal/admin-balance-lookup"
    headers = {"X-Internal-Secret": settings.webstore_bridge_secret}
    try:
        async with _aiohttp.ClientSession(timeout=_aiohttp.ClientTimeout(total=10)) as sess:
            async with sess.get(url, headers=headers, params={"q": query}) as resp:
                data = await resp.json()
                if resp.status == 404:
                    await message.answer("❌ Пользователь не найден.")
                    await state.clear()
                    return
                if resp.status != 200:
                    await message.answer(f"Ошибка вебстора: {data.get('error', resp.status)}")
                    await state.clear()
                    return
    except Exception as e:
        await message.answer(f"Ошибка соединения: {e}")
        await state.clear()
        return

    await state.clear()
    profile_token = data["profile_token"]
    balance = data["balance_rub"]
    contact = data["contact"] or "—"

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="+50 ₽", callback_data=f"adm_wb_adj:+50:{profile_token}"),
        InlineKeyboardButton(text="+100 ₽", callback_data=f"adm_wb_adj:+100:{profile_token}"),
        InlineKeyboardButton(text="+200 ₽", callback_data=f"adm_wb_adj:+200:{profile_token}"),
    )
    builder.row(
        InlineKeyboardButton(text="-50 ₽", callback_data=f"adm_wb_adj:-50:{profile_token}"),
        InlineKeyboardButton(text="-100 ₽", callback_data=f"adm_wb_adj:-100:{profile_token}"),
        InlineKeyboardButton(text="-200 ₽", callback_data=f"adm_wb_adj:-200:{profile_token}"),
    )
    builder.row(
        InlineKeyboardButton(text="✏️ Произвольная сумма", callback_data=f"adm_wb_custom:{profile_token}"),
    )
    builder.row(InlineKeyboardButton(text="◀️ Отмена", callback_data="adm_stats_web"))

    await message.answer(
        f"💳 <b>Баланс пользователя</b>\n\n"
        f"Контакт: {contact}\n"
        f"Токен: <code>{profile_token}</code>\n"
        f"Баланс: <b>{balance:.2f} ₽</b>\n\n"
        "Выберите действие:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("adm_wb_adj:"))
async def admin_web_balance_adjust(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await callback.answer("Обрабатываем…")
    _, amount_str, profile_token = callback.data.split(":", 2)
    amount = int(amount_str)
    await _apply_web_balance_adjustment(callback, profile_token, amount)


@router.callback_query(F.data.startswith("adm_wb_custom:"))
async def admin_web_balance_custom_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    profile_token = callback.data.split(":", 1)[1]
    await state.set_state(WebBalanceAdminState.waiting_amount)
    await state.update_data(profile_token=profile_token)
    back_kb = InlineKeyboardBuilder()
    back_kb.row(InlineKeyboardButton(text="◀️ Отмена", callback_data="adm_stats_web"))
    await callback.message.edit_text(
        "Введите сумму (<code>150</code> для начисления или <code>-150</code> для списания):",
        reply_markup=back_kb.as_markup(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(WebBalanceAdminState.waiting_amount)
async def admin_web_balance_custom_amount(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    profile_token = data.get("profile_token", "")
    try:
        amount = int(message.text.strip().replace(",", ".").split(".")[0])
    except ValueError:
        await message.answer("❌ Введите целое число, например 150 или -50.")
        return
    await state.clear()
    await _apply_web_balance_adjustment(message, profile_token, amount)


async def _apply_web_balance_adjustment(event, profile_token: str, amount: int) -> None:
    import aiohttp as _aiohttp
    url = f"{settings.webstore_api_base_url.rstrip('/')}/api/store/internal/admin-balance-adjust"
    headers = {"X-Internal-Secret": settings.webstore_bridge_secret}
    try:
        async with _aiohttp.ClientSession(timeout=_aiohttp.ClientTimeout(total=10)) as sess:
            async with sess.post(url, headers=headers, json={"profile_token": profile_token, "amount": amount}) as resp:
                data = await resp.json()
                if resp.status != 200:
                    err = data.get("error", str(resp.status))
                    if hasattr(event, "answer") and hasattr(event, "message"):
                        await event.message.answer(f"Ошибка: {err}")
                    else:
                        await event.answer(f"Ошибка: {err}")
                    return
    except Exception as e:
        if hasattr(event, "answer") and hasattr(event, "message"):
            await event.message.answer(f"Ошибка соединения: {e}")
        else:
            await event.answer(f"Ошибка соединения: {e}")
        return

    sign = "+" if amount > 0 else ""
    new_balance = data.get("new_balance_rub", 0)
    actor = getattr(getattr(event, "from_user", None), "id", None)
    if actor:
        await write_admin_audit(
            actor_telegram_id=actor,
            action="web_balance_adjusted",
            entity_type="web_profile",
            entity_id=f"{profile_token[:12]}…",
            summary=f"Баланс изменён на {sign}{amount} ₽",
            details=f"Новый баланс: {float(new_balance):.2f} ₽",
        )
    text = f"✅ Баланс скорректирован: {sign}{amount} ₽\nНовый баланс: <b>{new_balance:.2f} ₽</b>"
    back_kb = InlineKeyboardBuilder()
    back_kb.row(InlineKeyboardButton(text="◀️ К статистике", callback_data="adm_stats_web"))
    if hasattr(event, "message"):
        try:
            await event.message.edit_text(text, reply_markup=back_kb.as_markup(), parse_mode="HTML")
        except Exception:
            await event.message.answer(text, reply_markup=back_kb.as_markup(), parse_mode="HTML")
        # Callback was acknowledged before the network request.
    else:
        await event.answer(text, reply_markup=back_kb.as_markup(), parse_mode="HTML")
