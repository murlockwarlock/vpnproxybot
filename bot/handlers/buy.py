"""Purchase flow handler - product type → tariff (from DB) → platform → payment."""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timezone
from html import escape
from urllib.parse import urlparse

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from bot.config import settings
from bot.database import async_session
from bot.keyboards.client import (
    payment_kb,
    platform_kb,
    product_type_kb,
    tariffs_kb,
    purchase_intro_kb,
    purchase_subscription_kb,
    purchase_target_kb,
)
from bot.models import (
    AdaptSubscription,
    BotSettings,
    Partner,
    Payment,
    PaymentStatus,
    Server,
    Subscription,
    Tariff,
    TariffType,
    User,
)
from bot.services.adapt_api import (
    AdaptAPI,
    can_upgrade_after_minimum_custom_renew,
    retry_adapt_read,
)
from bot.services.balance_service import get_user_balance
from bot.services.legal_docs import build_legal_notice, get_all_legal_doc_urls
from bot.services.purchase_intent import (
    decode_intent,
    effective_expired_adapt_action,
    encode_intent,
    get_purchase_price_rub,
)
from bot.services.adapt_routing import is_adapt_subscription
from bot.services.subscription_semantics import (
    is_adapt_trial_subscription,
    is_adapt_trial_tariff,
    paid_access_clause,
)
from bot.services.tariff_rules import (
    INTRO_BASIC_ALREADY_USED_TEXT,
    build_darimiru_tariff_text,
    build_tariff_purchase_note,
    can_purchase_intro_basic_tariff,
    is_intro_basic_tariff,
    supports_extra_devices,
)
from bot.services.vhq_subscription_proxy import get_subscription_display_key
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
    purchase_targets = await _purchase_targets(callback.from_user.id)

    if purchase_targets:
        only_expires_at = (
            getattr(purchase_targets[0], "expires_at", None)
            if len(purchase_targets) == 1
            else None
        )
        if only_expires_at and only_expires_at <= datetime.utcnow():
            sub = purchase_targets[0]
            await callback.message.edit_text(
                _purchase_subscription_text(sub, 0, 1),
                reply_markup=purchase_subscription_kb(
                    sub.id,
                    position=0,
                    total=1,
                    show_upgrade=False,
                    back_callback="profile",
                ),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            await callback.answer()
            return
        if (
            len(purchase_targets) == 1
            and is_adapt_trial_subscription(purchase_targets[0])
            and not await _has_completed_payment(callback.from_user.id)
        ):
            await _open_renew_target(callback, purchase_targets[0].id)
            return
        await callback.message.edit_text(
            "<b>Оплата подписки</b>\n\n"
            "<b>Продлить</b> — добавить срок к выбранной подписке и сохранить её ссылку.\n\n"
            "<b>Улучшить</b> — перейти на другой доступный тариф.\n\n"
            "На следующем экране вы увидите каждую подписку отдельно вместе с её ссылкой.",
            reply_markup=purchase_intro_kb(),
            parse_mode="HTML",
        )
        await callback.answer()
        return

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
    await _show_tariffs(callback, TariffType.VPN, has_product_types=False, intent_suffix="~n~0")


async def _has_completed_payment(telegram_id: int) -> bool:
    async with async_session() as session:
        payment_id = await session.scalar(
            select(Payment.id)
            .join(User, User.id == Payment.user_id)
            .where(User.telegram_id == telegram_id)
            .where(Payment.status == PaymentStatus.COMPLETED)
            .limit(1)
        )
    return payment_id is not None


async def _quote_tariff_list(
    telegram_id: int,
    tariffs: list[Tariff],
    *,
    action: str,
    target_subscription_id: int,
) -> tuple[list[Tariff], dict[int, float]]:
    """Use the same quote as checkout for every tariff shown to the client."""
    async with async_session() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        target = await session.get(Subscription, target_subscription_id)
        current_tariff = (
            await session.get(Tariff, target.tariff_id)
            if target and target.tariff_id
            else None
        )
        priced_tariffs: list[Tariff] = []
        prices: dict[int, float] = {}
        for tariff in tariffs:
            candidate_action = action
            if target and current_tariff:
                candidate_action = effective_expired_adapt_action(
                    action,
                    current_plan_uuid=current_tariff.adapt_plan_uuid,
                    selected_plan_uuid=tariff.adapt_plan_uuid,
                    expires_at=target.expires_at,
                )
            try:
                prices[tariff.id] = await get_purchase_price_rub(
                    session,
                    user=user,
                    tariff=tariff,
                    action=candidate_action,
                    target_subscription_id=target_subscription_id,
                )
            except ValueError as exc:
                logger.warning(
                    "Hiding tariff with unavailable quote: user=%s tariff=%s target=%s error=%s",
                    telegram_id,
                    tariff.id,
                    target_subscription_id,
                    exc,
                )
                continue
            priced_tariffs.append(tariff)
    return priced_tariffs, prices


def _purchase_subscription_text(sub: Subscription, position: int, total: int) -> str:
    status = "активна" if sub.status.value == "active" else "срок закончился"
    expires = sub.expires_at.strftime("%d.%m.%Y") if sub.expires_at else "—"
    days = int(getattr(sub.tariff, "days", 0) or sub.tariff_days or 0)
    devices = int(sub.device_slots or getattr(sub.tariff, "device_count", 0) or 1)
    url = get_subscription_display_key(sub) or sub.vpn_key or "Ссылка недоступна"
    return (
        f"<b>Подписка {position + 1}/{total}</b>\n\n"
        f"Статус: <b>{status}</b>\n"
        f"Срок тарифа: <b>{days} дн.</b>\n"
        f"Устройств: <b>{devices}</b>\n"
        f"Действует до: <b>{expires}</b>\n\n"
        f"Ссылка подписки:\n<code>{escape(str(url))}</code>"
    )


def _format_rubles(value: float) -> str:
    rounded = round(float(value) + 1e-9, 2)
    if rounded.is_integer():
        return str(int(rounded))
    return f"{rounded:.2f}".replace(".", ",")


def _upgrade_explanation_text(sub: Subscription, tariff: Tariff) -> str:
    price = max(0.0, float(tariff.price_rub or 0))
    tariff_days = max(1, int(tariff.days or 0))
    remaining_days = max(0.0, (sub.expires_at - datetime.utcnow()).total_seconds() / 86400)
    residual = min(price, price / tariff_days * remaining_days)
    used = max(0.0, price - residual)
    devices = int(sub.device_slots or tariff.device_count or 1)
    expires = sub.expires_at.strftime("%d.%m.%Y")
    return (
        "<b>Улучшение подписки</b>\n\n"
        f"Текущий тариф: <b>{escape(str(tariff.label))}</b>\n"
        f"Параметры: <b>{tariff_days} дн., {devices} устр.</b>\n"
        f"Срок действия до: <b>{expires}</b>\n"
        f"Стоимость: <b>{_format_rubles(price)} ₽</b>\n"
        f"Использовано: <b>{_format_rubles(used)} ₽</b>\n"
        f"Остаточная стоимость: <b>{_format_rubles(residual)} ₽</b>\n\n"
        "При выборе нового тарифа вы доплачиваете разницу между стоимостью "
        "нового тарифа и остаточной стоимостью текущего.\n\n"
        "Остаточная стоимость — это сумма, которую вы заплатили по тарифу, "
        "за вычетом использованного периода.\n\n"
        "Выберите новый тариф:"
    )


@router.callback_query(F.data.startswith("purchase_browse_"))
async def browse_purchase_subscriptions(callback: CallbackQuery) -> None:
    try:
        requested_position = int(callback.data.removeprefix("purchase_browse_"))
    except ValueError:
        requested_position = 0
    await callback.answer()
    targets = await _purchase_targets(callback.from_user.id)
    if not targets:
        await callback.message.edit_text(
            "Не удалось проверить подписки. Попробуйте ещё раз. "
            f"Если ошибка повторится, напишите в поддержку {settings.support_username or ''}.",
            reply_markup=purchase_intro_kb(),
        )
        return
    position = requested_position % len(targets)
    sub = targets[position]
    upgrade_ids = {
        item.id
        for item in await _purchase_targets(
            callback.from_user.id,
            upgrade_only=True,
            refresh_provider=False,
        )
    }
    try:
        await callback.message.edit_text(
            _purchase_subscription_text(sub, position, len(targets)),
            reply_markup=purchase_subscription_kb(
                sub.id,
                position=position,
                total=len(targets),
                show_upgrade=sub.id in upgrade_ids,
            ),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


@router.callback_query(F.data.startswith("purchase_return_"))
async def return_to_purchase_subscription(callback: CallbackQuery) -> None:
    """Return from a tariff list to the exact subscription card."""
    try:
        subscription_id = int(callback.data.removeprefix("purchase_return_"))
    except ValueError:
        await callback.answer("Не удалось открыть подписку. Попробуйте ещё раз.", show_alert=True)
        return

    await callback.answer()
    targets = await _purchase_targets(callback.from_user.id)
    position = next(
        (index for index, sub in enumerate(targets) if sub.id == subscription_id),
        None,
    )
    if position is None:
        await callback.message.answer(
            "Подписка не найдена. Попробуйте ещё раз или напишите в поддержку "
            f"{settings.support_username or ''}."
        )
        return

    upgrade_ids = {
        item.id
        for item in await _purchase_targets(
            callback.from_user.id,
            upgrade_only=True,
            refresh_provider=False,
        )
    }
    sub = targets[position]
    try:
        await callback.message.edit_text(
            _purchase_subscription_text(sub, position, len(targets)),
            reply_markup=purchase_subscription_kb(
                sub.id,
                position=position,
                total=len(targets),
                show_upgrade=sub.id in upgrade_ids,
                back_callback=(
                    "profile"
                    if len(targets) == 1 and sub.expires_at <= datetime.utcnow()
                    else "buy"
                ),
            ),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


@router.callback_query(F.data == "buy_new")
async def start_new_purchase(callback: CallbackQuery) -> None:
    await _open_new_purchase(callback, source_subscription_id=None)


@router.callback_query(F.data.startswith("buy_new_"))
async def start_new_purchase_from_subscription(callback: CallbackQuery) -> None:
    try:
        source_subscription_id = int(callback.data.removeprefix("buy_new_"))
    except ValueError:
        await callback.answer("Не удалось открыть тарифы. Попробуйте ещё раз.", show_alert=True)
        return
    if not any(
        sub.id == source_subscription_id
        for sub in await _purchase_targets(callback.from_user.id)
    ):
        await callback.answer(
            "Подписка не найдена. Попробуйте ещё раз или напишите в поддержку.",
            show_alert=True,
        )
        return
    await _open_new_purchase(callback, source_subscription_id=source_subscription_id)


async def _open_new_purchase(
    callback: CallbackQuery,
    *,
    source_subscription_id: int | None,
) -> None:
    async with async_session() as session:
        has_extra = await _has_non_vpn_tariffs(session, user_id=callback.from_user.id)
    if has_extra:
        await callback.message.edit_text(
            SELECT_PRODUCT_TYPE,
            reply_markup=product_type_kb(
                back_callback=(
                    f"purchase_return_{source_subscription_id}"
                    if source_subscription_id
                    else "buy"
                ),
                source_subscription_id=source_subscription_id,
            ),
            parse_mode="HTML",
        )
        await callback.answer()
        return
    await _show_tariffs(
        callback,
        TariffType.VPN,
        has_product_types=False,
        intent_suffix=f"~n~{source_subscription_id or 0}",
        back_callback=(
            f"purchase_return_{source_subscription_id}"
            if source_subscription_id
            else "buy"
        ),
    )


async def _purchase_targets(
    telegram_id: int,
    *,
    upgrade_only: bool = False,
    refresh_provider: bool = False,
    target_subscription_id: int | None = None,
) -> list[Subscription]:
    async with async_session() as session:
        query = (
            select(Subscription)
            .options(selectinload(Subscription.tariff))
            .where(Subscription.user.has(telegram_id=telegram_id))
            .where(paid_access_clause(Subscription))
            .order_by(Subscription.expires_at.desc(), Subscription.id.desc())
        )
        if target_subscription_id is not None:
            query = query.where(Subscription.id == target_subscription_id)
        result = await session.execute(query)
        items = result.scalars().all()
        adapt_items = [sub for sub in items if is_adapt_subscription(sub)]
        if adapt_items and refresh_provider:
            record_rows = await session.execute(
                select(AdaptSubscription).where(
                    AdaptSubscription.subscription_id.in_([sub.id for sub in adapt_items])
                )
            )
            records = {row.subscription_id: row for row in record_rows.scalars().all()}
            tariff_rows = await session.execute(
                select(Tariff).where(Tariff.adapt_plan_uuid.is_not(None))
            )
            tariffs_by_plan = {
                str(row.adapt_plan_uuid).strip(): row for row in tariff_rows.scalars().all()
            }
            api = AdaptAPI()
            verified_ids: set[int] = set()
            changed = False
            status_targets = []
            for sub in adapt_items:
                record = records.get(sub.id)
                if not record:
                    # Legacy provider markers without an Adapt UUID cannot be
                    # refreshed here; the paid-operation preflight still blocks
                    # an unsafe mutation later.
                    verified_ids.add(sub.id)
                    continue
                status_targets.append((sub, record))
            statuses = await asyncio.gather(
                *(
                    retry_adapt_read(
                        lambda record=record: api.get_status(record.adapt_uuid),
                        label=f"subscription_status:{_sub.id}",
                    )
                    for _sub, record in status_targets
                ),
                return_exceptions=True,
            )
            for (sub, record), status in zip(status_targets, statuses):
                try:
                    if isinstance(status, BaseException):
                        raise status
                    plan_uuid = str(status.get("plan_uuid") or "").strip()
                    devices = status.get("devices")
                    end_raw = status.get("end_date")
                    actual_tariff = tariffs_by_plan.get(plan_uuid)
                    if not plan_uuid or devices is None or not end_raw or not actual_tariff:
                        raise ValueError("provider status is incomplete or plan is not mapped")
                    expires_at = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
                    if expires_at.tzinfo is not None:
                        expires_at = expires_at.astimezone(timezone.utc).replace(tzinfo=None)
                except Exception as exc:
                    logger.warning(
                        "Hiding unverified Adapt subscription from purchase actions: subscription_id=%s error=%s",
                        sub.id,
                        exc,
                    )
                    continue
                verified_ids.add(sub.id)
                if record.adapt_plan_uuid != plan_uuid:
                    record.adapt_plan_uuid = plan_uuid
                    changed = True
                if sub.tariff_id != actual_tariff.id:
                    sub.tariff_id = actual_tariff.id
                    sub.tariff = actual_tariff
                    sub.tariff_days = actual_tariff.days
                    sub.tariff_months = actual_tariff.days // 30
                    changed = True
                if sub.device_slots != int(devices):
                    sub.device_slots = int(devices)
                    changed = True
                if sub.expires_at != expires_at:
                    sub.expires_at = expires_at
                    record.end_date = expires_at
                    changed = True
            items = [sub for sub in items if not is_adapt_subscription(sub) or sub.id in verified_ids]
            if changed:
                await session.commit()
        upgrade_tariffs: list[tuple[str, float]] = []
        if upgrade_only:
            tariff_query = (
                select(Tariff.adapt_plan_uuid, Tariff.price_rub)
                .where(Tariff.is_active == True)  # noqa: E712
                .where(Tariff.adapt_plan_uuid.is_not(None))
            )
            if not settings.is_admin(telegram_id):
                tariff_query = tariff_query.where(Tariff.is_admin_only == False)  # noqa: E712
            upgrade_tariffs = [
                (str(plan_uuid).strip(), float(price))
                for plan_uuid, price in (await session.execute(tariff_query)).all()
                if str(plan_uuid or "").strip()
            ]
    items = [
        sub
        for sub in items
        if sub.status.value in {"active", "expired"}
        and bool(sub.vpn_key)
        and is_adapt_subscription(sub)
        and sub.tariff is not None
        and sub.tariff.is_active
    ]
    if not settings.is_admin(telegram_id):
        items = [sub for sub in items if not sub.tariff.is_admin_only]
    if not upgrade_only:
        return items
    now = datetime.utcnow()
    return [
        sub for sub in items
        if is_adapt_subscription(sub)
        and not is_adapt_trial_subscription(sub)
        and sub.expires_at > now
        and sub.tariff
        and any(
            plan_uuid != str(sub.tariff.adapt_plan_uuid or "").strip()
            and price > float(sub.tariff.price_rub)
            for plan_uuid, price in upgrade_tariffs
        )
    ]


def _subscription_count_text(count: int, action: str) -> str:
    if count % 10 == 1 and count % 100 != 11:
        noun = "подписка"
    elif count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        noun = "подписки"
    else:
        noun = "подписок"
    verb = "продлить" if action == "renew" else "улучшить"
    return f"У вас найдено {count} {noun}. Выберите, какую хотите {verb}."


@router.callback_query(F.data.in_({"purchase_action_renew", "purchase_action_upgrade"}))
async def choose_purchase_target(callback: CallbackQuery) -> None:
    action = callback.data.removeprefix("purchase_action_")
    targets = await _purchase_targets(callback.from_user.id, upgrade_only=action == "upgrade")
    if not targets:
        await callback.answer("Подходящих подписок не найдено", show_alert=True)
        return
    if len(targets) == 1:
        if action == "renew":
            await _open_renew_target(callback, targets[0].id)
        else:
            await _open_upgrade_target(callback, targets[0].id)
        return
    await callback.message.edit_text(
        _subscription_count_text(len(targets), action),
        reply_markup=purchase_target_kb(targets, action),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("purchase_renew_"))
async def choose_renew_target(callback: CallbackQuery) -> None:
    sub_id = int(callback.data.removeprefix("purchase_renew_"))
    await _open_renew_target(callback, sub_id)


@router.callback_query(F.data.startswith("purchase_tariffs_"))
async def return_to_subscription_tariffs(callback: CallbackQuery) -> None:
    """Return from payment to the tariff list for the same subscription."""
    try:
        sub_id = int(callback.data.removeprefix("purchase_tariffs_"))
    except ValueError:
        await callback.answer("Не удалось открыть тарифы. Попробуйте ещё раз.", show_alert=True)
        return

    async with async_session() as session:
        sub = await session.get(Subscription, sub_id)
        tariff = await session.get(Tariff, sub.tariff_id) if sub and sub.tariff_id else None
        owner_ok = bool(
            sub
            and await session.scalar(
                select(User.id).where(
                    User.id == sub.user_id,
                    User.telegram_id == callback.from_user.id,
                )
            )
        )
    if not owner_ok or not sub or not tariff:
        await callback.answer(
            "Подписка не найдена. Попробуйте ещё раз или напишите в поддержку.",
            show_alert=True,
        )
        return
    if sub.expires_at <= datetime.utcnow() and not is_adapt_trial_tariff(tariff):
        await _open_expired_tariff_choice(callback, sub_id)
    elif is_adapt_trial_tariff(tariff):
        await _open_renew_target(callback, sub_id)
    else:
        await _open_upgrade_target(callback, sub_id)


async def _open_renew_target(callback: CallbackQuery, sub_id: int) -> None:
    async with async_session() as session:
        sub = await session.get(Subscription, sub_id)
        tariff = await session.get(Tariff, sub.tariff_id) if sub and sub.tariff_id else None
        owner_ok = bool(sub and await session.scalar(select(User.id).where(User.id == sub.user_id, User.telegram_id == callback.from_user.id)))
    if (
        not owner_ok
        or not sub
        or not is_adapt_subscription(sub)
        or not tariff
        or not tariff.adapt_plan_uuid
        or not tariff.is_active
    ):
        await callback.answer("Тариф этой подписки больше недоступен", show_alert=True)
        return
    if tariff.is_admin_only and not settings.is_admin(callback.from_user.id):
        await callback.answer("Тариф этой подписки больше недоступен", show_alert=True)
        return
    if sub.expires_at <= datetime.utcnow() and not is_adapt_trial_tariff(tariff):
        await _open_expired_tariff_choice(callback, sub_id)
        return
    if is_adapt_trial_tariff(tariff):
        async with async_session() as session:
            query = (
                select(Tariff)
                .where(Tariff.is_active == True)  # noqa: E712
                .where(Tariff.adapt_plan_uuid.is_not(None))
                .where(Tariff.price_rub > tariff.price_rub)
                .order_by(Tariff.price_rub, Tariff.days, Tariff.id)
            )
            if not settings.is_admin(callback.from_user.id):
                query = query.where(Tariff.is_admin_only == False)  # noqa: E712
            tariffs = (await session.execute(query)).scalars().all()
            stars_enabled = await _get_stars_enabled(session)
        tariffs, price_overrides = await _quote_tariff_list(
            callback.from_user.id,
            tariffs,
            action="upgrade",
            target_subscription_id=sub_id,
        )
        if not tariffs:
            await callback.answer("Подходящих тарифов сейчас нет. Напишите в поддержку.", show_alert=True)
            return
        await callback.message.edit_text(
            "Выберите тариф для продления:",
            reply_markup=tariffs_kb(
                tariffs,
                stars_enabled,
                intent_suffix=f"~u~{sub_id}",
                back_callback=f"purchase_return_{sub_id}",
                price_overrides_rub=price_overrides,
            ),
        )
        await callback.answer()
        return
    await _select_tariff_token(callback, f"{tariff.id}~r~{sub_id}")


@router.callback_query(F.data.startswith("purchase_upgrade_"))
async def choose_upgrade_target(callback: CallbackQuery) -> None:
    sub_id = int(callback.data.removeprefix("purchase_upgrade_"))
    await _open_upgrade_target(callback, sub_id)


async def _open_upgrade_target(callback: CallbackQuery, sub_id: int) -> None:
    async with async_session() as session:
        sub = await session.get(Subscription, sub_id)
        current = await session.get(Tariff, sub.tariff_id) if sub and sub.tariff_id else None
        owner_ok = bool(sub and await session.scalar(select(User.id).where(User.id == sub.user_id, User.telegram_id == callback.from_user.id)))
        if (
            not owner_ok
            or not is_adapt_subscription(sub)
            or not current
            or not current.adapt_plan_uuid
            or (current.is_admin_only and not settings.is_admin(callback.from_user.id))
        ):
            await callback.answer("Эту подписку сейчас нельзя улучшить", show_alert=True)
            return
        if is_adapt_trial_tariff(current):
            await _open_renew_target(callback, sub_id)
            return
        query = (
            select(Tariff)
            .where(Tariff.is_active == True)  # noqa: E712
            .where(Tariff.adapt_plan_uuid.is_not(None))
            .where(Tariff.adapt_plan_uuid != current.adapt_plan_uuid)
            .order_by(Tariff.price_rub)
        )
        if sub.expires_at > datetime.utcnow():
            query = query.where(Tariff.price_rub > current.price_rub)
        if not settings.is_admin(callback.from_user.id):
            query = query.where(Tariff.is_admin_only == False)  # noqa: E712
        result = await session.execute(query)
        tariffs = result.scalars().all()
        stars_enabled = await _get_stars_enabled(session)
    tariffs, price_overrides = await _quote_tariff_list(
        callback.from_user.id,
        tariffs,
        action="upgrade",
        target_subscription_id=sub_id,
    )
    if not tariffs:
        await callback.answer("Других доступных тарифов сейчас нет", show_alert=True)
        return
    await callback.message.edit_text(
        _upgrade_explanation_text(sub, current),
        reply_markup=tariffs_kb(
            tariffs,
            stars_enabled,
            intent_suffix=f"~u~{sub_id}",
            back_callback=f"purchase_return_{sub_id}",
            price_overrides_rub=price_overrides,
        ),
        parse_mode="HTML",
    )
    await callback.answer()


async def _open_expired_tariff_choice(callback: CallbackQuery, sub_id: int) -> None:
    """Show every available Adapt tariff for one expired subscription."""
    async with async_session() as session:
        sub = await session.get(Subscription, sub_id)
        current = await session.get(Tariff, sub.tariff_id) if sub and sub.tariff_id else None
        owner_ok = bool(
            sub
            and await session.scalar(
                select(User.id).where(
                    User.id == sub.user_id,
                    User.telegram_id == callback.from_user.id,
                )
            )
        )
        if (
            not owner_ok
            or not sub
            or sub.expires_at > datetime.utcnow()
            or not is_adapt_subscription(sub)
            or not current
            or not current.adapt_plan_uuid
        ):
            await callback.answer("Не удалось открыть тарифы для этой подписки", show_alert=True)
            return
        query = (
            select(Tariff)
            .where(Tariff.is_active == True)  # noqa: E712
            .where(Tariff.adapt_plan_uuid.is_not(None))
            .order_by(Tariff.price_rub, Tariff.days, Tariff.id)
        )
        if not settings.is_admin(callback.from_user.id):
            query = query.where(Tariff.is_admin_only == False)  # noqa: E712
        tariffs = (await session.execute(query)).scalars().all()
        stars_enabled = await _get_stars_enabled(session)
    tariffs, price_overrides = await _quote_tariff_list(
        callback.from_user.id,
        tariffs,
        action="upgrade",
        target_subscription_id=sub_id,
    )
    if not tariffs:
        await callback.answer("Доступных тарифов сейчас нет", show_alert=True)
        return
    await callback.message.edit_text(
        "Выберите тариф:",
        reply_markup=tariffs_kb(
            tariffs,
            stars_enabled,
            intent_suffix=f"~u~{sub_id}",
            back_callback=f"purchase_return_{sub_id}",
            price_overrides_rub=price_overrides,
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ptype_"))
async def select_product_type(callback: CallbackQuery) -> None:
    """Step 0b - User picked product type, show tariffs for that type."""
    ptype_token = callback.data.removeprefix("ptype_")
    ptype_str, _, source_token = ptype_token.partition("~")
    try:
        source_subscription_id = int(source_token) if source_token else None
    except ValueError:
        source_subscription_id = None
    try:
        tariff_type = TariffType(ptype_str)
    except ValueError:
        tariff_type = TariffType.VPN

    await _show_tariffs(
        callback,
        tariff_type,
        has_product_types=True,
        intent_suffix=f"~n~{source_subscription_id or 0}",
        back_callback=(
            f"buy_new_{source_subscription_id}"
            if source_subscription_id
            else "buy"
        ),
    )


async def _show_tariffs(
    callback: CallbackQuery,
    tariff_type: TariffType,
    has_product_types: bool = False,
    intent_suffix: str = "",
    back_callback: str | None = None,
) -> None:
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
            reply_markup=tariffs_kb(
                tariffs,
                stars_enabled,
                has_product_types=has_product_types,
                intent_suffix=intent_suffix,
                back_callback=back_callback,
            ),
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
    if tariff.is_admin_only and not settings.is_admin(callback.from_user.id):
        await callback.answer("Этот тариф больше недоступен", show_alert=True)
        return
    if tariff.tariff_type in (TariffType.VPN, TariffType.BOTH) and not tariff.adapt_plan_uuid and not tariff.vhq_tier:
        await callback.answer("Этот тариф больше недоступен", show_alert=True)
        return

    await _select_tariff_token(callback, str(tariff_id))


@router.callback_query(F.data.startswith("tariff_"))
async def select_tariff(callback: CallbackQuery) -> None:
    """Step 2 - User picked a tariff, show platform selection or payment."""
    await _select_tariff_token(callback, callback.data.removeprefix("tariff_"))


def _tariff_payment_back_callback(
    *,
    tariff_type: TariffType,
    has_product_types: bool,
    requested_purchase_action: str,
    target_subscription_id: int | None,
) -> str:
    if requested_purchase_action == "upgrade" and target_subscription_id:
        return f"purchase_tariffs_{target_subscription_id}"
    if requested_purchase_action == "renew" and target_subscription_id:
        return f"purchase_return_{target_subscription_id}"
    if requested_purchase_action == "new" and target_subscription_id:
        if has_product_types:
            return f"ptype_{tariff_type.value}~{target_subscription_id}"
        return f"buy_new_{target_subscription_id}"
    if has_product_types:
        return f"ptype_{tariff_type.value}"
    return "buy"


async def _select_tariff_token(callback: CallbackQuery, encoded_tariff: str) -> None:
    """Open a tariff without mutating aiogram's frozen CallbackQuery model."""
    tariff_token, purchase_action, target_subscription_id = decode_intent(encoded_tariff)
    requested_purchase_action = purchase_action
    tariff_id = int(tariff_token)
    callback_answered = False

    async def respond(text: str | None = None, *, show_alert: bool = False) -> None:
        nonlocal callback_answered
        if not callback_answered:
            await callback.answer(text, show_alert=show_alert)
            callback_answered = True
        elif text:
            await callback.message.answer(text)

    if purchase_action in {"renew", "upgrade"}:
        await respond("Проверяю подписку…")
        verified_targets = await _purchase_targets(
            callback.from_user.id,
            refresh_provider=True,
            target_subscription_id=target_subscription_id,
        )
        if not any(sub.id == target_subscription_id for sub in verified_targets):
            await respond(
                "Не удалось проверить выбранную подписку. Попробуйте ещё раз или напишите в поддержку.",
                show_alert=True,
            )
            return

    async with async_session() as session:
        tariff = await session.get(Tariff, tariff_id)
        has_product_types = await _has_non_vpn_tariffs(session, user_id=callback.from_user.id)
        user = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
        if not tariff:
            await respond("Тариф не найден", show_alert=True)
            return
        if not tariff.is_active:
            await respond("Этот тариф больше недоступен", show_alert=True)
            return
        if tariff.is_admin_only and not settings.is_admin(callback.from_user.id):
            await respond("Этот тариф больше недоступен", show_alert=True)
            return
        if tariff.adapt_plan_uuid:
            await respond("Проверяю тариф…")
            try:
                provider_plans = await retry_adapt_read(
                    lambda: AdaptAPI().list_plans(),
                    label=f"selected_plan:{tariff.id}",
                )
                provider_plan = next(
                    (
                        item for item in provider_plans
                        if str(item.get("uuid") or item.get("plan_uuid") or "").strip()
                        == str(tariff.adapt_plan_uuid).strip()
                    ),
                    None,
                )
                if (
                    not provider_plan
                    or provider_plan.get("devices") is None
                    or provider_plan.get("is_active") is False
                ):
                    raise ValueError("provider plan is unavailable")
            except Exception as exc:
                logger.warning(
                    "Blocking unavailable Adapt tariff before payment: tariff_id=%s error=%s",
                    tariff.id,
                    exc,
                )
                await respond(
                    "Не удалось проверить тариф. Попробуйте ещё раз или напишите в поддержку.",
                    show_alert=True,
                )
                return
        if purchase_action == "upgrade" and target_subscription_id:
            target = await session.get(Subscription, target_subscription_id)
            current_tariff = (
                await session.get(Tariff, target.tariff_id)
                if target and target.tariff_id
                else None
            )
            if target and current_tariff and not is_adapt_trial_tariff(current_tariff):
                purchase_action = effective_expired_adapt_action(
                    purchase_action,
                    current_plan_uuid=current_tariff.adapt_plan_uuid,
                    selected_plan_uuid=tariff.adapt_plan_uuid,
                    expires_at=target.expires_at,
                )
            if (
                target
                and current_tariff
                and target.expires_at <= datetime.utcnow()
                and purchase_action == "upgrade"
            ):
                current_provider_plan = next(
                    (
                        item
                        for item in provider_plans
                        if str(item.get("uuid") or item.get("plan_uuid") or "").strip()
                        == str(current_tariff.adapt_plan_uuid or "").strip()
                    ),
                    None,
                )
                if not can_upgrade_after_minimum_custom_renew(
                    current_provider_plan,
                    provider_plan,
                ):
                    await respond(
                        "На этот тариф нельзя перейти с сохранением ссылки. "
                        "Выберите «Создать новую» или другой тариф.",
                        show_alert=True,
                    )
                    return
        intro_basic_available = await can_purchase_intro_basic_tariff(session, user=user, tariff=tariff)
        try:
            purchase_price = await get_purchase_price_rub(
                session, user=user, tariff=tariff, action=purchase_action,
                target_subscription_id=target_subscription_id,
            )
        except ValueError as exc:
            await respond(str(exc), show_alert=True)
            return

    if not intro_basic_available:
        await respond(INTRO_BASIC_ALREADY_USED_TEXT, show_alert=True)
        return

    # Keep the exact purchase context when returning from the payment screen.
    tariff_back = _tariff_payment_back_callback(
        tariff_type=tariff.tariff_type,
        has_product_types=has_product_types,
        requested_purchase_action=requested_purchase_action,
        target_subscription_id=target_subscription_id,
    )

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
        await respond()
        return

    # VPN or Both — platform is selected after successful payment, before key delivery.
    async with async_session() as session:
        stars_enabled = await _get_stars_enabled(session)
        legal_urls = await get_all_legal_doc_urls(session)
        user = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
        user_balance = get_user_balance(user)

    partner_discount_text = await _partner_discount_text(callback.from_user.id, tariff)
    price_str = f"{purchase_price}₽"
    if stars_enabled and tariff.price_stars:
        quoted_stars = max(1, math.ceil(tariff.price_stars * purchase_price / tariff.price_rub))
        price_str += f" / {quoted_stars}⭐"
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
            + "\nПосле оплаты бот предложит выбрать устройство и отправит ключ с нужной инструкцией.\n"
            + build_tariff_purchase_note(tariff, darimiru=_is_darimiru_tariff_catalog())
            + partner_discount_text,
            reply_markup=payment_kb(
                tariff_id,
                encode_intent("deferred", purchase_action, target_subscription_id),
                stars_enabled,
                user_balance,
                float(purchase_price),
                legal_urls,
                back_callback=tariff_back,
            ),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    await respond()


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
