"""Inline keyboards for the admin panel."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.models import Server, PlatformGuide


# ── Admin Main Menu ────────────────────────────────────

def admin_menu_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📊 Статистика",      callback_data="adm_stats"),
    )
    builder.row(
        InlineKeyboardButton(text="🌐 Вебстор",         callback_data="adm_stats_web"),
    )
    builder.row(
        InlineKeyboardButton(text="🖥 Серверы",          callback_data="adm_servers"),
        InlineKeyboardButton(text="👥 Клиенты",            callback_data="adm_users"),
    )
    builder.row(
        InlineKeyboardButton(text="🔑 Выдать ключ",      callback_data="adm_gen_key"),
        InlineKeyboardButton(text="➕ Добавить сервер",   callback_data="adm_add_server"),
    )
    builder.row(
        InlineKeyboardButton(text="📋 Тарифы",           callback_data="adm_tariffs"),
        InlineKeyboardButton(text="⚙️ Оплата и устройства",  callback_data="adm_settings"),
    )
    builder.row(
        InlineKeyboardButton(text="✉️ Рассылки",          callback_data="adm_mailing"),
        InlineKeyboardButton(text="🤝 Рефералки",         callback_data="adm_referral"),
    )
    builder.row(
        InlineKeyboardButton(text="🤝 Партнёры",          callback_data="adm_partners"),
    )
    builder.row(
        InlineKeyboardButton(text="📚 Гайды по платформам", callback_data="adm_guides"),
        InlineKeyboardButton(text="🤖 ИИ-ассистент",        callback_data="adm_ai_settings"),
    )
    builder.row(
        InlineKeyboardButton(text="📋 Логи оплат",          callback_data="adm_logs"),
        InlineKeyboardButton(text="📣 Рекламные ссылки",    callback_data="adm_ads"),
    )
    builder.row(
        InlineKeyboardButton(text="📜 Журнал действий", callback_data="adm_audit_1"),
    )
    builder.row(
        InlineKeyboardButton(text="🔌 Adapt планы",          callback_data="adm_adapt_plans"),
    )
    return builder.as_markup()


# ── Platform Guides ────────────────────────────────────

PLATFORM_LABELS = {
    "android":    "🤖 Android",
    "ios":        "🍎 iOS",
    "mac":        "🍏 Mac",
    "windows":    "💻 Windows",
    "android_tv": "📺 Android TV",
}


def guides_menu_kb(has_media: dict[str, bool]) -> InlineKeyboardMarkup:
    """List all platforms; show 📎 if media is set."""
    builder = InlineKeyboardBuilder()
    for p, label in PLATFORM_LABELS.items():
        icon = "📎 " if has_media.get(p) else ""
        builder.row(InlineKeyboardButton(
            text=f"{icon}{label}",
            callback_data=f"adm_guide_{p}",
        ))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back"))
    return builder.as_markup()


def guide_detail_kb(platform: str, has_media: bool, has_text: bool, has_buttons: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✏️ Изменить текст", callback_data=f"adm_guide_etext_{platform}"),
    )
    if has_text:
        builder.row(
            InlineKeyboardButton(text="🗑 Сбросить текст к исходному", callback_data=f"adm_guide_rtext_{platform}"),
        )
    builder.row(
        InlineKeyboardButton(text="📥 Загрузить медиа (фото/видео/альбом)", callback_data=f"adm_guide_upload_{platform}"),
    )
    if has_media:
        builder.row(
            InlineKeyboardButton(text="🗑 Удалить медиа", callback_data=f"adm_guide_clear_{platform}"),
        )
    builder.row(
        InlineKeyboardButton(
            text=f"🔘 Изменить кнопки (установлены)" if has_buttons else "🔘 Изменить кнопки",
            callback_data=f"adm_guide_btns_{platform}",
        ),
    )
    builder.row(
        InlineKeyboardButton(text="👁 Предпросмотр", callback_data=f"adm_guide_prev_{platform}"),
    )
    builder.row(InlineKeyboardButton(text="◀️ К гайдам", callback_data="adm_guides"))
    return builder.as_markup()


# ── Stats Sub-menu ────────────────────────────────────

def stats_menu_kb(webstore: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📋 Обзор", callback_data="adm_stats_overview"),
    )
    builder.row(
        InlineKeyboardButton(text="💰 Выручка", callback_data="adm_stats_revenue"),
        InlineKeyboardButton(text="👥 Пользователи", callback_data="adm_stats_users"),
    )
    builder.row(
        InlineKeyboardButton(text="📈 Конверсия и отток", callback_data="adm_stats_conversion"),
        InlineKeyboardButton(text="💳 Способы оплаты", callback_data="adm_stats_methods"),
    )
    builder.row(
        InlineKeyboardButton(text="🏆 Топ тарифов", callback_data="adm_stats_tariffs"),
    )
    if webstore:
        builder.row(
            InlineKeyboardButton(text="🌐 Вебстор", callback_data="adm_stats_web"),
        )
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back"))
    return builder.as_markup()


def stats_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ К статистике", callback_data="adm_stats")],
    ])


# ── Servers ────────────────────────────────────────────

def server_list_kb(servers: list[Server]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for srv in servers:
        status = "🟢" if srv.is_active else "🔴"
        load = f"{srv.current_clients}/{srv.max_clients}"
        builder.row(
            InlineKeyboardButton(
                text=f"{status} {srv.country_emoji} {srv.name} [{load}]",
                callback_data=f"adm_srv_{srv.id}",
            )
        )
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back"))
    return builder.as_markup()


def server_actions_kb(server_id: int, is_active: bool) -> InlineKeyboardMarkup:
    toggle_text = "🔴 Выключить" if is_active else "🟢 Включить"
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=toggle_text, callback_data=f"adm_srv_toggle_{server_id}"),
    )
    builder.row(
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"adm_srv_del_{server_id}"),
    )
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="adm_servers"))
    return builder.as_markup()


# ── Users ──────────────────────────────────────────────

def user_search_kb(users: list = None, page: int = 1, total_pages: int = 1) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    
    # User buttons
    if users:
        for u in users:
            name = u.full_name or u.username or f"ID: {u.telegram_id}"
            status = "🚫" if u.is_blocked else "✅"
            builder.row(InlineKeyboardButton(
                text=f"{status} {name}", 
                callback_data=f"adm_usr_info_{u.telegram_id}"
            ))
    
    # Pagination
    nav = []
    prev_page = page - 1 if page > 1 else total_pages
    next_page = page + 1 if page < total_pages else 1
    
    nav.append(InlineKeyboardButton(text="◀️ Пред.", callback_data=f"adm_users_{prev_page}"))
    nav.append(InlineKeyboardButton(text=f"• {page}/{total_pages} •", callback_data="ignore"))
    nav.append(InlineKeyboardButton(text="След. ▶️", callback_data=f"adm_users_{next_page}"))
    builder.row(*nav)
    
    builder.row(InlineKeyboardButton(text="🔎 Поиск", callback_data="adm_user_search"))
    builder.row(InlineKeyboardButton(text="◀️ Назад",          callback_data="adm_back"))
    return builder.as_markup()


def user_actions_kb(
    telegram_id: int,
    is_blocked: bool,
    partner_id: int | None = None,
    has_active_sub: bool = False,
) -> InlineKeyboardMarkup:
    block_text = "🔓 Разблокировать" if is_blocked else "🚫 Заблокировать"
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=block_text, callback_data=f"adm_usr_block_{telegram_id}"),
        InlineKeyboardButton(text="🔄 Обновить", callback_data=f"adm_usr_refresh_{telegram_id}"),
    )
    builder.row(
        InlineKeyboardButton(text="🔑 Выдать ключ", callback_data=f"adm_usr_key_{telegram_id}"),
    )
    builder.row(
        InlineKeyboardButton(text="☠️ Сбросить аккаунт", callback_data=f"adm_usr_reset_conf_{telegram_id}"),
    )
    builder.row(
        InlineKeyboardButton(text="📊 Подписки (подробно)", callback_data=f"adm_usr_subs_{telegram_id}"),
        InlineKeyboardButton(text="💳 Оплаты", callback_data=f"adm_usr_pays_{telegram_id}_1"),
    )
    if partner_id is None:
        builder.row(
            InlineKeyboardButton(text="🤝 Выдать партнёрку", callback_data=f"adm_usr_partner_{telegram_id}"),
        )
    else:
        builder.row(
            InlineKeyboardButton(text="📊 Открыть партнёра", callback_data=f"adm_pt_{partner_id}"),
        )
    if has_active_sub:
        builder.row(
            InlineKeyboardButton(text="📱 Управление устройствами", callback_data=f"adm_usr_devices_{telegram_id}"),
        )
    builder.row(InlineKeyboardButton(text="◀️ К списку клиентов", callback_data="adm_users"))
    return builder.as_markup()


def user_reset_confirm_kb(telegram_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"adm_usr_info_{telegram_id}"),
        InlineKeyboardButton(text="⚠️ ДА, СБРОСИТЬ", callback_data=f"adm_usr_reset_do_{telegram_id}"),
    )
    return builder.as_markup()


# ── Back ───────────────────────────────────────────────

def admin_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Админ-панель", callback_data="adm_back")]
    ])


# ── Tariffs ────────────────────────────────────────────

def tariff_actions_kb(tariff_id: int, is_active: bool, is_admin_only: bool = False) -> InlineKeyboardMarkup:
    toggle_text = "🔴 Деактивировать" if is_active else "🟢 Активировать"
    visibility_text = "👁 Показать пользователям" if is_admin_only else "🔒 Только для админов"
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✏️ Метка", callback_data=f"adm_tedit_label_{tariff_id}"),
        InlineKeyboardButton(text="📅 Дни", callback_data=f"adm_tedit_days_{tariff_id}"),
    )
    builder.row(
        InlineKeyboardButton(text="💰 Цена ₽", callback_data=f"adm_tedit_rub_{tariff_id}"),
        InlineKeyboardButton(text="⭐ Stars", callback_data=f"adm_tedit_stars_{tariff_id}"),
    )
    builder.row(
        InlineKeyboardButton(text="🔌 Adapt UUID", callback_data=f"adm_tedit_adapt_{tariff_id}"),
        InlineKeyboardButton(text="⚡️ VHQ tier", callback_data=f"adm_tedit_vhq_{tariff_id}"),
    )
    builder.row(
        InlineKeyboardButton(text=visibility_text, callback_data=f"adm_tariff_admonly_{tariff_id}"),
    )
    builder.row(
        InlineKeyboardButton(text=toggle_text, callback_data=f"adm_tariff_toggle_{tariff_id}"),
    )
    builder.row(
        InlineKeyboardButton(text="🗑 Удалить тариф", callback_data=f"adm_tariff_del_{tariff_id}"),
    )
    builder.row(InlineKeyboardButton(text="◀️ К тарифам", callback_data="adm_tariffs"))
    return builder.as_markup()


def _tariff_admin_group(tariff) -> tuple[int, str]:
    label = str(getattr(tariff, "label", "") or "").lower().replace("ё", "е")
    tariff_type = str(getattr(getattr(tariff, "tariff_type", ""), "value", getattr(tariff, "tariff_type", "")))
    if tariff_type == "TG_PROXY":
        return 40, "Telegram"
    if tariff_type == "BOTH":
        return 50, "Комбо"
    if "лайт" in label:
        return 10, "Лайт"
    if "базов" in label:
        return 20, "Базовый"
    if "преми" in label:
        return 30, "Премиум"
    return 90, "Другое"


def _sort_tariffs_for_admin(tariffs):
    return sorted(
        tariffs,
        key=lambda t: (
            _tariff_admin_group(t)[0],
            int(getattr(t, "days", 0) or 0),
            int(getattr(t, "price_rub", 0) or 0),
            int(getattr(t, "id", 0) or 0),
        ),
    )


def tariffs_admin_kb(tariffs, page: int = 0, page_size: int = 20) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    tariffs = _sort_tariffs_for_admin(tariffs)
    total = len(tariffs)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    page_tariffs = tariffs[start:start + page_size]

    current_group = None
    for t in page_tariffs:
        _group_order, group_name = _tariff_admin_group(t)
        if group_name != current_group:
            current_group = group_name
            builder.row(InlineKeyboardButton(text=f"— {group_name} —", callback_data="noop"))
        status = "🟢" if t.is_active else "🔴"
        lock = " 🔒" if getattr(t, "is_admin_only", False) else ""
        builder.row(
            InlineKeyboardButton(
                text=f"{status}{lock} {t.label} - {t.price_rub}₽",
                callback_data=f"adm_tariff_{t.id}",
            )
        )

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="◀️", callback_data=f"adm_tariffs_page_{page - 1}"))
    if total_pages > 1:
        nav_buttons.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton(text="▶️", callback_data=f"adm_tariffs_page_{page + 1}"))
    if nav_buttons:
        builder.row(*nav_buttons)

    builder.row(
        InlineKeyboardButton(text="➕ Добавить тариф", callback_data="adm_tariff_add"),
    )
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back"))
    return builder.as_markup()


def referral_menu_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="⚙️ Настройки реферальной программы", callback_data="adm_ref_settings"))
    builder.row(InlineKeyboardButton(text="👥 Список рефереров", callback_data="adm_ref_list_1"))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back"))
    return builder.as_markup()


def ad_links_menu_kb(links: list[tuple[int, str, bool, int, int]]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for link_id, title, is_active, visitors, buyers in links:
        status = "🟢" if is_active else "🔴"
        builder.row(
            InlineKeyboardButton(
                text=f"{status} {title[:28]} • {visitors}/{buyers}",
                callback_data=f"adm_ads_link_{link_id}",
            )
        )
    builder.row(InlineKeyboardButton(text="➕ Создать ссылку", callback_data="adm_ads_new"))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back"))
    return builder.as_markup()


def ad_link_kind_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📢 Channels", callback_data="adm_ads_kind_channels"),
        InlineKeyboardButton(text="🤖 Bots", callback_data="adm_ads_kind_bots"),
    )
    builder.row(
        InlineKeyboardButton(text="🔎 Search", callback_data="adm_ads_kind_search"),
        InlineKeyboardButton(text="🏷 Custom", callback_data="adm_ads_kind_custom"),
    )
    builder.row(InlineKeyboardButton(text="◀️ К ссылкам", callback_data="adm_ads"))
    return builder.as_markup()


def ad_link_detail_kb(link_id: int, is_active: bool) -> InlineKeyboardMarkup:
    toggle_text = "🔴 Выключить" if is_active else "🟢 Включить"
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🔄 Обновить", callback_data=f"adm_ads_link_{link_id}"),
        InlineKeyboardButton(text=toggle_text, callback_data=f"adm_ads_toggle_{link_id}"),
    )
    builder.row(InlineKeyboardButton(text="◀️ К ссылкам", callback_data="adm_ads"))
    return builder.as_markup()


def referral_settings_kb(cfg) -> InlineKeyboardMarkup:
    """cfg is a ReferralConfig instance."""
    builder = InlineKeyboardBuilder()
    enabled_txt = "🟢 Программа: ВКЛ" if cfg.is_enabled else "🔴 Программа: ВЫКЛ"
    builder.row(InlineKeyboardButton(text=enabled_txt, callback_data="adm_ref_toggle"))
    builder.row(
        InlineKeyboardButton(
            text=f"🎁 Процент отчислений: {cfg.commission_percent}%",
            callback_data="adm_ref_edit_comm",
        )
    )
    builder.row(InlineKeyboardButton(text=f"📌 Кнопка меню: «{cfg.btn_name}»", callback_data="adm_ref_edit_btn"))
    builder.row(InlineKeyboardButton(text=f"📌 Кнопка подписки: «{cfg.sub_btn_name}»", callback_data="adm_ref_edit_sub"))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="adm_referral"))
    return builder.as_markup()


def referral_referrers_kb(rows: list, page: int, total_pages: int) -> InlineKeyboardMarkup:
    """rows: list of (telegram_id, full_name, username, referral_count, turnover)"""
    builder = InlineKeyboardBuilder()
    for tg_id, name, username, count, turnover in rows:
        label = str(name or username or tg_id)
        builder.row(InlineKeyboardButton(
            text=f"👤 {label[:20]} - {count} реф. / {turnover:.0f}₽",
            callback_data=f"adm_ref_detail_{tg_id}_0",
        ))
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"adm_ref_list_{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="ignore"))
    if page < total_pages:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"adm_ref_list_{page + 1}"))
    if nav:
        builder.row(*nav)
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="adm_referral"))
    return builder.as_markup()


def referral_referrer_detail_kb(referrer_tg_id: int, page: int, total_pages: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"adm_ref_detail_{referrer_tg_id}_{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="ignore"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"adm_ref_detail_{referrer_tg_id}_{page + 1}"))
    if nav:
        builder.row(*nav)
    builder.row(InlineKeyboardButton(text="◀️ К списку", callback_data="adm_ref_list_1"))
    return builder.as_markup()


def settings_kb(
    stars_enabled: bool,
    demo_key_enabled: bool = False,
    whatsapp_enabled: bool = False,
) -> InlineKeyboardMarkup:
    stars_text = "⭐ Stars: ВКЛ ✅" if stars_enabled else "⭐ Stars: ВЫКЛ ❌"
    demo_text = "🎁 Демо-ключ: ВКЛ ✅" if demo_key_enabled else "🎁 Демо-ключ: ВЫКЛ ❌"
    wa_text = "💬 WhatsApp: ВКЛ ✅" if whatsapp_enabled else "💬 WhatsApp: ВЫКЛ ❌"
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=stars_text, callback_data="adm_toggle_stars"),
    )
    builder.row(
        InlineKeyboardButton(text=demo_text, callback_data="adm_toggle_demo_key"),
    )
    builder.row(
        InlineKeyboardButton(text=wa_text, callback_data="adm_toggle_whatsapp"),
    )
    if whatsapp_enabled:
        builder.row(
            InlineKeyboardButton(text="💬 Изменить адрес WhatsApp-ускорителя", callback_data="adm_set_whatsapp_host"),
        )
    builder.row(
        InlineKeyboardButton(text="📱 Изменить макс. устройств", callback_data="adm_set_max_devices"),
    )
    builder.row(
        InlineKeyboardButton(text="💰 Изменить дневную ставку", callback_data="adm_set_daily_charge"),
    )
    builder.row(
        InlineKeyboardButton(text="💸 Изменить цену доп. устройства", callback_data="adm_set_device_price"),
    )
    builder.row(
        InlineKeyboardButton(text="📄 Изменить /policy", callback_data="adm_doc_policy"),
        InlineKeyboardButton(text="📃 Изменить /agree", callback_data="adm_doc_agree"),
    )
    builder.row(
        InlineKeyboardButton(text="📑 Изменить /oferta", callback_data="adm_doc_oferta"),
    )
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back"))
    return builder.as_markup()
