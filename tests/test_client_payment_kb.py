from datetime import datetime
from types import SimpleNamespace

from bot.keyboards.client import (
    payment_kb,
    purchase_intro_kb,
    purchase_subscription_kb,
    purchase_target_kb,
    tariffs_kb,
)


def _button_texts(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def test_payment_kb_hides_telegram_pay_for_tariffs_below_70(monkeypatch):
    monkeypatch.setattr("bot.keyboards.client.settings.telegram_payment_provider_token", "token")
    monkeypatch.setattr("bot.keyboards.client.settings.yookassa_shop_id", "shop")
    monkeypatch.setattr("bot.keyboards.client.settings.yookassa_secret_key", "secret")

    markup = payment_kb(
        tariff_id=1,
        platform="ios",
        stars_enabled=True,
        user_balance=0,
        tariff_price=59,
    )

    texts = _button_texts(markup)
    assert "💳 Оплатить через Telegram Pay" not in texts
    assert "⭐ Telegram Stars" in texts
    assert "💳 Оплатить через YooKassa" in texts


def test_payment_kb_shows_telegram_pay_for_tariffs_from_70(monkeypatch):
    monkeypatch.setattr("bot.keyboards.client.settings.telegram_payment_provider_token", "token")

    markup = payment_kb(
        tariff_id=1,
        platform="ios",
        stars_enabled=False,
        user_balance=0,
        tariff_price=70,
    )

    texts = _button_texts(markup)
    assert "💳 Оплатить через Telegram Pay" in texts


def test_tariffs_kb_groups_by_family_and_sorts_by_price():
    tariffs = [
        SimpleNamespace(id=4, label="Базовый (90 дней)", days=90, price_rub=659, price_stars=0, tariff_type="VPN"),
        SimpleNamespace(id=1, label="Лайт (1 месяц)", days=30, price_rub=95, price_stars=0, tariff_type="VPN"),
        SimpleNamespace(id=3, label="Базовый (14 дней)", days=14, price_rub=125, price_stars=0, tariff_type="VPN"),
        SimpleNamespace(id=2, label="Лайт (7 дней)", days=7, price_rub=23, price_stars=0, tariff_type="VPN"),
    ]

    texts = _button_texts(tariffs_kb(tariffs))

    assert texts[:7] == [
        "— Лайт —",
        "Лайт (7 дней) - 23₽",
        "Лайт (1 месяц) - 95₽",
        "— Базовый —",
        "Базовый (14 дней) - 125₽",
        "Базовый (90 дней) - 659₽",
        "◀️ Назад",
    ]


def test_subscription_target_button_is_short_and_keeps_term_visible():
    sub = SimpleNamespace(
        id=243,
        tariff=SimpleNamespace(label="Базовый • 90 дн • 5📱• 136 Гб⚡️", days=90),
        tariff_days=90,
        device_slots=5,
        expires_at=datetime(2026, 10, 11),
    )

    texts = _button_texts(purchase_target_kb([sub], "renew"))

    assert texts[0] == "90 дн · 5 устр. · до 11.10.2026"
    assert "Базовый" not in texts[0]


def test_purchase_carousel_keyboard_has_expected_actions():
    assert _button_texts(purchase_intro_kb())[0] == "Продолжить"

    texts = _button_texts(
        purchase_subscription_kb(243, position=0, total=2, show_upgrade=True)
    )
    assert texts[:4] == ["↻ Продлить", "↑ Улучшить", "➕ Создать новую", "Следующая"]


def test_trial_carousel_omits_upgrade_action():
    texts = _button_texts(
        purchase_subscription_kb(199, position=0, total=1, show_upgrade=False)
    )
    assert "↻ Продлить" in texts
    assert "↑ Улучшить" not in texts
