from types import SimpleNamespace

from bot.models import TariffType
from bot.services.tariff_rules import (
    build_darimiru_tariff_text,
    build_tariff_purchase_note,
    is_intro_basic_tariff,
    supports_extra_devices,
)


def test_intro_basic_tariff_detection_matches_one_day_basic_trial():
    assert is_intro_basic_tariff(SimpleNamespace(label="Базовый (1 день)", days=1)) is True
    assert is_intro_basic_tariff(SimpleNamespace(label="Базовый (7 дней)", days=7)) is False
    assert is_intro_basic_tariff(SimpleNamespace(label="Лайт (1 день)", days=1)) is False


def test_darimiru_tariff_text_contains_descriptions_and_locations():
    text = build_darimiru_tariff_text(
        "🇳🇱 Нидерланды 1  •  🇳🇱 Нидерланды 2  •  🇩🇪 Германия",
        extra_device_tariffs=["Лайт (1 месяц)"],
    )

    assert "Тариф Базовый" in text
    assert "Тариф Максимум" in text
    assert "50 серверов" in text
    assert "80 серверов" in text
    assert "🇳🇱 Нидерланды 1" in text
    assert "🇳🇱 Нидерланды 2" in text
    assert "Эстония" not in text
    assert "и другие" in text
    assert "1-5 устройств" in text


def test_extra_devices_are_supported_only_for_non_vhq_vpn_tariffs():
    marzban_tariff = SimpleNamespace(label="Лайт (1 месяц)", days=30, price_rub=95, tariff_type=TariffType.VPN)
    vhq_tariff = SimpleNamespace(label="Базовый (7 дней)", days=7, price_rub=59, tariff_type=TariffType.VPN)

    assert supports_extra_devices(marzban_tariff) is True
    assert supports_extra_devices(vhq_tariff) is False
    assert "можно докупить дополнительные устройства" in build_tariff_purchase_note(marzban_tariff).lower()
    assert "доступны только для тарифов на наших серверах" in build_tariff_purchase_note(vhq_tariff).lower()
    assert "только на <b>1 устройство</b>" in build_tariff_purchase_note(vhq_tariff, darimiru=True).lower()


def test_payment_note_mentions_three_devices_for_premium():
    premium_tariff = SimpleNamespace(label="Премиум • 30 дн", days=30, price_rub=249, tariff_type=TariffType.VPN)

    assert "на <b>3 устройства</b>" in build_tariff_purchase_note(premium_tariff, darimiru=True).lower()
