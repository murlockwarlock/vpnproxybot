"""Inline keyboards for the client-facing side of the bot."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.config import settings
from bot.models import Tariff


# ── Main Menu ──────────────────────────────────────────

def main_menu_kb(
    ref_btn_name: str | None = None,
    purchase_button_text: str = "🛒 Купить доступ",
    is_partner: bool = False,
    is_admin: bool = False,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=purchase_button_text, callback_data="buy"),
    )
    builder.row(
        InlineKeyboardButton(text="🔑 Мои ключи", callback_data="my_keys"),
        InlineKeyboardButton(text="👤 Мой профиль", callback_data="profile"),
    )
    builder.row(
        InlineKeyboardButton(text="📲 Как подключить", callback_data="guide_menu"),
        InlineKeyboardButton(text="❓ Помощь", callback_data="help"),
    )
    builder.row(
        InlineKeyboardButton(text="📱 Управление устройствами", callback_data="my_devices"),
    )
    if ref_btn_name:
        builder.row(
            InlineKeyboardButton(text=ref_btn_name, callback_data="ref_link_main"),
        )
    if is_partner:
        builder.row(
            InlineKeyboardButton(text="📊 Партнёрский кабинет", callback_data="partner_dashboard"),
        )
    if is_admin:
        builder.row(
            InlineKeyboardButton(text="🔧 Админ-панель", callback_data="admin_panel"),
        )
    return builder.as_markup()


# ── Renew Button (for notifications) ──────────────────

def renew_kb() -> InlineKeyboardMarkup:
    """Single-button keyboard for expiry notifications."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оплатить подписку 💳", callback_data="buy")]
    ])


def renewal_choice_kb(
    sub_tariffs: list[tuple[int, str, float]],  # (tariff_id, label, daily_rate_rub)
    back_callback: str = "back_main",
) -> InlineKeyboardMarkup:
    """Keyboard for choosing which subscription to renew when user has multiple.

    sub_tariffs: list of (tariff_id, display_label, daily_rate_rub).
    """
    builder = InlineKeyboardBuilder()
    for tariff_id, label, daily_rate in sub_tariffs:
        rate_text = f" ({daily_rate:.2f}₽/день)" if daily_rate > 0 else ""
        builder.row(
            InlineKeyboardButton(
                text=f"🔄 {label}{rate_text}",
                callback_data=f"renew_tariff_{tariff_id}",
            )
        )
    builder.row(
        InlineKeyboardButton(text="🛒 Новый тариф", callback_data="buy"),
    )
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data=back_callback))
    return builder.as_markup()


# ── Tariff Picker ──────────────────────────────────────

def _tariff_group(tariff: Tariff) -> tuple[int, str]:
    label = str(getattr(tariff, "label", "") or "").lower().replace("ё", "е")
    tariff_type = str(getattr(getattr(tariff, "tariff_type", ""), "value", getattr(tariff, "tariff_type", "")))
    if "лайт" in label:
        return 10, "Лайт"
    if "базов" in label:
        return 20, "Базовый"
    if "преми" in label:
        return 30, "Премиум"
    if tariff_type == "TG_PROXY":
        return 40, "Telegram"
    if tariff_type == "BOTH":
        return 50, "Комбо"
    return 90, "Другое"


def _sort_tariffs_for_client(tariffs: list[Tariff]) -> list[Tariff]:
    return sorted(
        tariffs,
        key=lambda t: (
            _tariff_group(t)[0],
            float(getattr(t, "price_rub", 0) or 0),
            int(getattr(t, "days", 0) or 0),
            int(getattr(t, "id", 0) or 0),
        ),
    )


def tariffs_kb(
    tariffs: list[Tariff],
    stars_enabled: bool = True,
    has_product_types: bool = False,
    intent_suffix: str = "",
    back_callback: str | None = None,
) -> InlineKeyboardMarkup:
    """Build tariff selection keyboard grouped by family, then sorted by price."""
    builder = InlineKeyboardBuilder()
    current_group = None
    for t in _sort_tariffs_for_client(tariffs):
        _group_order, group_name = _tariff_group(t)
        if group_name != current_group:
            current_group = group_name
            builder.row(InlineKeyboardButton(text=f"— {group_name} —", callback_data="noop"))
        if stars_enabled and t.price_stars:
            label = f"{t.label} - {t.price_rub}₽ / {t.price_stars}⭐"
        else:
            label = f"{t.label} - {t.price_rub}₽"
        builder.row(
            InlineKeyboardButton(
                text=label,
                callback_data=f"tariff_{t.id}{intent_suffix}",
            )
        )
    resolved_back_callback = back_callback or ("buy" if has_product_types else "back_main")
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data=resolved_back_callback))
    return builder.as_markup()


def purchase_action_kb(*, show_upgrade: bool = True) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="↻ Продлить подписку", callback_data="purchase_action_renew")],
    ]
    if show_upgrade:
        rows.append([InlineKeyboardButton(text="↑ Улучшить подписку", callback_data="purchase_action_upgrade")])
    rows.extend([
        [InlineKeyboardButton(text="➕ Создать новую", callback_data="buy_new")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="profile")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def purchase_intro_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Продолжить", callback_data="purchase_browse_0")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="profile")],
    ])


def purchase_subscription_kb(
    subscription_id: int,
    *,
    position: int,
    total: int,
    show_upgrade: bool,
    back_callback: str = "buy",
) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="↻ Продлить", callback_data=f"purchase_renew_{subscription_id}")],
    ]
    if show_upgrade:
        rows.append([
            InlineKeyboardButton(text="↑ Улучшить", callback_data=f"purchase_upgrade_{subscription_id}")
        ])
    rows.append([
        InlineKeyboardButton(
            text="➕ Создать новую",
            callback_data=f"buy_new_{subscription_id}",
        )
    ])
    if total > 1:
        rows.append([
            InlineKeyboardButton(
                text="Следующая",
                callback_data=f"purchase_browse_{(position + 1) % total}",
            )
        ])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=back_callback)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def purchase_target_kb(subscriptions, action: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for sub in subscriptions:
        label = sub.tariff.label if sub.tariff else f"Подписка #{sub.id}"
        if "базов" in label.lower() and sub.tariff:
            days = int(getattr(sub.tariff, "days", 0) or getattr(sub, "tariff_days", 0) or 0)
            devices = int(getattr(sub, "device_slots", 0) or 0)
            label = f"{days} дн" if days else f"Подписка #{sub.id}"
            if devices:
                label += f" · {devices} устр."
        expires = sub.expires_at.strftime("%d.%m.%Y") if sub.expires_at else "—"
        builder.row(InlineKeyboardButton(
            text=f"{label} · до {expires}",
            callback_data=f"purchase_{action}_{sub.id}",
        ))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="buy"))
    return builder.as_markup()


# ── Platform Picker ────────────────────────────────────

def platform_kb(tariff_id: int, back_callback: str = "buy") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="🤖 Android",
            callback_data=f"plat_{tariff_id}_android",
        ),
        InlineKeyboardButton(
            text="🍎 iOS",
            callback_data=f"plat_{tariff_id}_ios",
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text="💻 Windows",
            callback_data=f"plat_{tariff_id}_windows",
        ),
        InlineKeyboardButton(
            text="🍏 Mac",
            callback_data=f"plat_{tariff_id}_mac",
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text="📺 Android TV",
            callback_data=f"plat_{tariff_id}_android_tv",
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text="◀️ Назад",
            callback_data=back_callback,
        )
    )
    return builder.as_markup()


# ── Payment Method ─────────────────────────────────────

def payment_kb(
    tariff_id: int,
    platform: str,
    stars_enabled: bool = True,
    user_balance: float = 0,
    tariff_price: float = 0,
    legal_urls: dict[str, str] | None = None,
    back_callback: str | None = None,
) -> InlineKeyboardMarkup:
    """Build payment method keyboard - show buttons with and without discounts."""
    builder = InlineKeyboardBuilder()

    legal_urls = legal_urls or {}

    legal_buttons = []
    if legal_urls.get("oferta"):
        legal_buttons.append(InlineKeyboardButton(text="📄 Оферта", url=legal_urls["oferta"]))
    if legal_urls.get("agree"):
        legal_buttons.append(InlineKeyboardButton(text="📃 Соглашение", url=legal_urls["agree"]))
    if legal_urls.get("policy"):
        legal_buttons.append(InlineKeyboardButton(text="🔒 Policy", url=legal_urls["policy"]))
    if legal_buttons:
        builder.row(*legal_buttons)

    def add_provider(name: str, code: str, minimum: int = 0):
        # 1. Full Payment (No Discount)
        builder.row(InlineKeyboardButton(text=f"{name}", callback_data=f"pay_{code}_{tariff_id}_{platform}_0"))
        
        # 2. Discounted Payment (If balance available and not full balance payment)
        if 0 < user_balance < tariff_price:
            discount = min(user_balance, tariff_price)
            final_price = tariff_price - discount
            # Capping final price to minimum provider threshold
            if 0 < final_price < minimum:
                final_price = float(minimum)
                discount = tariff_price - final_price
                
            if discount > 0:
                builder.row(InlineKeyboardButton(text=f"📉 {name} (со скидкой: {int(final_price)}₽)", callback_data=f"pay_{code}_{tariff_id}_{platform}_1"))

    if settings.yookassa_shop_id and settings.yookassa_secret_key:
        add_provider("💳 Оплатить через YooKassa", "yookassa", minimum=10)

    if stars_enabled:
        add_provider("⭐ Telegram Stars", "stars")

    if settings.telegram_payment_provider_token and tariff_price >= 70:
        add_provider("💳 Оплатить через Telegram Pay", "telegram", minimum=70)

    if settings.robokassa_merchant_login:
        add_provider("💰 Robokassa (Мир / СБП)", "robokassa", minimum=50)

    if user_balance >= tariff_price and tariff_price > 0:
        builder.row(
            InlineKeyboardButton(
                text=f"💎 Оплатить 100% с баланса",
                callback_data=f"pay_balance_{tariff_id}_{platform}_1",
            )
        )

    builder.row(
        InlineKeyboardButton(
            text="◀️ Назад",
            callback_data=back_callback or f"tariff_{tariff_id}",
        ),
    )
    return builder.as_markup()


# ── Profile ────────────────────────────────────────────

def profile_kb(
    has_active_subs: bool = False,
    has_device_manageable_subs: bool = False,
    purchase_button_text: str = "🛒 Купить доступ",
    has_recurring: bool = False,
    recurring_active: bool = False,
    balance_mode_enabled: bool = False,
    balance_autodebit_enabled: bool = False,
    site_profile_url: str | None = None,
    renewal_options: list[tuple[int, str, float]] | None = None,
    has_daily_charge_tariff_choice: bool = False,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if has_active_subs:
        row = [InlineKeyboardButton(text="🔑 Мои ключи", callback_data="my_keys_1")]
        if has_device_manageable_subs:
            row.append(InlineKeyboardButton(text="📱 Устройства", callback_data="my_devices"))
        builder.row(*row)
    builder.row(
        InlineKeyboardButton(text=purchase_button_text, callback_data="buy"),
    )
    if balance_mode_enabled or balance_autodebit_enabled:
        builder.row(
            InlineKeyboardButton(text="💰 Пополнить баланс", callback_data="balance_menu"),
        )
        balance_toggle_text = (
            "🟢 Ежедневные списания включены"
            if balance_mode_enabled and balance_autodebit_enabled
            else "⚪️ Ежедневные списания выключены"
        )
        row_balance = [
            InlineKeyboardButton(text=balance_toggle_text, callback_data="balance_toggle"),
            InlineKeyboardButton(text="📜 История баланса", callback_data="balance_history"),
        ]
        builder.row(*row_balance)
        if has_daily_charge_tariff_choice:
            builder.row(
                InlineKeyboardButton(
                    text="⚙️ Тариф для списаний", callback_data="daily_charge_tariff_choice"
                )
            )
    if has_recurring:
        if recurring_active:
            builder.row(
                InlineKeyboardButton(
                    text="🔕 Отключить автопродление",
                    callback_data="recurring_toggle_off",
                ),
            )
        else:
            builder.row(
                InlineKeyboardButton(
                    text="🔔 Включить автопродление",
                    callback_data="recurring_toggle_on",
                ),
            )
    builder.row(
        InlineKeyboardButton(text="🤝 Реферальная программа", callback_data="ref_link"),
    )
    if site_profile_url:
        builder.row(
            InlineKeyboardButton(text="🌐 Профиль на сайте", url=site_profile_url),
        )
    builder.row(
        InlineKeyboardButton(text="◀️ Главное меню", callback_data="back_main"),
    )
    return builder.as_markup()


def profile_keys_kb(page: int, total_pages: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    nav_buttons = []
    if page > 1:
        nav_buttons.append(InlineKeyboardButton(text="⬅️", callback_data=f"my_keys_{page-1}"))
    nav_buttons.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="ignore"))
    if page < total_pages:
        nav_buttons.append(InlineKeyboardButton(text="➡️", callback_data=f"my_keys_{page+1}"))
    builder.row(*nav_buttons)
    builder.row(InlineKeyboardButton(text="◀️ В профиль", callback_data="profile"))
    return builder.as_markup()


# ── Confirm / Cancel ───────────────────────────────────

def confirm_cancel_kb(confirm_data: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=confirm_data),
        InlineKeyboardButton(text="❌ Отмена", callback_data="back_main"),
    )
    return builder.as_markup()


# ── Help ───────────────────────────────────────────────

def help_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 ИИ-помощник", callback_data="help_ai_start")],
        [InlineKeyboardButton(text="📝 Обратная связь", callback_data="feedback_start")],
        [InlineKeyboardButton(text="◀️ Главное меню", callback_data="back_main")],
    ])


def demo_key_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📲 Как подключить", callback_data="guide_menu")],
        [InlineKeyboardButton(text="👤 Мой профиль", callback_data="profile")],
    ])


def guide_platform_kb(prefix: str, back_callback: str = "back_main") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🤖 Android", callback_data=f"{prefix}_android"),
        InlineKeyboardButton(text="🍎 iOS", callback_data=f"{prefix}_ios"),
    )
    builder.row(
        InlineKeyboardButton(text="💻 Windows", callback_data=f"{prefix}_windows"),
        InlineKeyboardButton(text="🍏 Mac", callback_data=f"{prefix}_mac"),
    )
    builder.row(
        InlineKeyboardButton(text="📺 Android TV", callback_data=f"{prefix}_android_tv"),
    )
    builder.row(
        InlineKeyboardButton(text="◀️ Главное меню", callback_data=back_callback),
    )
    return builder.as_markup()


# ── Back to Menu ───────────────────────────────────────

def back_to_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Главное меню", callback_data="back_main")]
    ])


def balance_menu_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="100 ₽", callback_data="balance_amount_100"),
        InlineKeyboardButton(text="300 ₽", callback_data="balance_amount_300"),
        InlineKeyboardButton(text="500 ₽", callback_data="balance_amount_500"),
    )
    builder.row(
        InlineKeyboardButton(text="1000 ₽", callback_data="balance_amount_1000"),
        InlineKeyboardButton(text="3000 ₽", callback_data="balance_amount_3000"),
    )
    builder.row(
        InlineKeyboardButton(text="✍️ Ввести сумму", callback_data="balance_amount_custom"),
    )
    builder.row(
        InlineKeyboardButton(text="◀️ Главное меню", callback_data="back_main"),
    )
    return builder.as_markup()


def balance_payment_kb(amount_rub: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if settings.yookassa_shop_id and settings.yookassa_secret_key:
        builder.row(
            InlineKeyboardButton(
                text="💳 Оплатить через ЮKassa",
                callback_data=f"paytopup_yookassa_{amount_rub}",
            )
        )
    if settings.telegram_payment_provider_token:
        builder.row(
            InlineKeyboardButton(
                text="💳 Оплатить через Telegram Pay",
                callback_data=f"paytopup_telegram_{amount_rub}",
            )
        )
    if settings.robokassa_merchant_login:
        builder.row(
            InlineKeyboardButton(
                text="💰 Robokassa",
                callback_data=f"paytopup_robokassa_{amount_rub}",
            )
        )
    builder.row(
        InlineKeyboardButton(text="◀️ Назад", callback_data="balance_menu"),
    )
    return builder.as_markup()


# ── Product Type ──────────────────────────────────────

def product_type_kb(
    *,
    back_callback: str = "back_main",
    source_subscription_id: int | None = None,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    suffix = f"~{source_subscription_id}" if source_subscription_id else ""
    builder.row(
        InlineKeyboardButton(text="🌐 Весь интернет", callback_data=f"ptype_vpn{suffix}"),
    )
    builder.row(
        InlineKeyboardButton(text="📱 Telegram-ускоритель", callback_data=f"ptype_tg_proxy{suffix}"),
    )
    builder.row(
        InlineKeyboardButton(
            text="🔥 Весь интернет + Telegram-ускоритель",
            callback_data=f"ptype_both{suffix}",
        ),
    )
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data=back_callback))
    return builder.as_markup()
