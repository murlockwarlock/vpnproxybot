"""Tariff-specific business rules shared across purchase flows."""

from __future__ import annotations

from sqlalchemy import select

from bot.models import Subscription, TariffType, User
from bot.services.vhq_routing import is_vhq_tariff

INTRO_BASIC_ALREADY_USED_TEXT = (
    "Тестовый тариф «Базовый (1 день)» можно оформить только один раз. "
    "Выберите другой тариф."
)


def is_intro_basic_tariff(tariff) -> bool:
    """Return True for the one-time VHQ trial tariff."""
    if not tariff:
        return False
    label = str(getattr(tariff, "label", "") or "").strip().lower()
    days = int(getattr(tariff, "days", 0) or 0)
    return days == 1 and label.startswith("базовый")


async def has_used_intro_basic_tariff(session, *, user_id: int) -> bool:
    """Return True if the user has ever received the 1-day intro tariff."""
    result = await session.execute(
        select(Subscription.id)
        .where(Subscription.user_id == user_id)
        .where(Subscription.billing_mode == "tariff")
        .where(Subscription.tariff_days == 1)
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def can_purchase_intro_basic_tariff(session, *, user: User | None, tariff) -> bool:
    if not user or not is_intro_basic_tariff(tariff):
        return True
    return not await has_used_intro_basic_tariff(session, user_id=user.id)


def build_darimiru_tariff_text(
    locations_str: str | None = None,
    extra_device_tariffs: list[str] | None = None,
) -> str:
    lines = [
        "💰 Выберите тариф ⬇️",
        "Чем дольше срок, тем выгоднее!",
        "",
        "<b>Тариф Лайт 🌟",
        "❯ 4 сервера 🌐",
        "❯ 3 устройства 📱📱📱</b>",
        "",
        "Стабильная работа основных приложений и сервисов на Wi-Fi и мобильном интернете без блокировок. Без лимитов.",
        "Подключение до 3-х устройств.",
        "",
        "<b>Тариф Базовый 🎮",
        "❯ 50 серверов 🌐 + 50 обходов ⚡️",
        "❯ 1 устройство 📱</b>",
        "",
        "Быстрый VPN для любых задач. Без лимита на основные сервера.",
        "Сервера-обходы ⚡️ — используйте при глушении мобильного интернета (лимит на такие сервера в скобках)",
        "Подключение на 1 устройство.",
        "",
        "<b>Тариф Премиум 🔥",
        "❯ 50 серверов 🌐 + 50 обходов ⚡️",
        "❯ 3 устройства 📱📱📱",
        "❯ Увеличенный трафик на ⚡️ серверах</b>",
        "",
        "Максимальная стабильность и комфорт для активного использования сразу на нескольких устройствах.",
        "",
        "<b>🌍 Доступные локации",
        "🇪🇪 Эстония • ",
        "🇳🇱 Нидерланды • ",
        "🇩🇪 Германия • и другие</b>",
        "",
        "Переключение между локациями — в 1 клик.",
    ]
    return "\n".join(lines)


def supports_extra_devices(tariff) -> bool:
    if not tariff:
        return False
    tariff_type = getattr(tariff, "tariff_type", None)
    return tariff_type in {TariffType.VPN, TariffType.BOTH} and not is_vhq_tariff(tariff)


def _tariff_family(tariff) -> str:
    label = str(getattr(tariff, "label", "") or "").strip().lower().replace("ё", "е")
    if "преми" in label:
        return "premium"
    if "базов" in label:
        return "basic"
    if "лайт" in label:
        return "light"
    return ""


def build_tariff_purchase_note(tariff, *, darimiru: bool = False) -> str:
    family = _tariff_family(tariff)
    if darimiru and family == "basic":
        return (
            "\n🖥 Ключ по выбранному тарифу можно установить только на <b>1 устройство</b>. "
            "Если вам нужно больше - выберите другой тариф."
        )
    if darimiru and family == "premium":
        return "\n🖥 Ключ по выбранному тарифу можно установить на <b>3 устройства</b>."

    lines = [
        "",
        "🖥 В тариф уже включено <b>3 устройства</b>.",
    ]
    if supports_extra_devices(tariff):
        lines.append("➕ Для этого тарифа можно докупить дополнительные устройства в профиле.")
    else:
        lines.append("➕ Дополнительные устройства сейчас доступны только для тарифов на наших серверах.")
    return "\n".join(lines)
