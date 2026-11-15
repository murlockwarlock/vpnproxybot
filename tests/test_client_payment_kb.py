from datetime import datetime
from types import SimpleNamespace

from bot.keyboards.client import (
    main_menu_kb,
    payment_kb,
    profile_kb,
    product_type_kb,
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


def test_tariffs_kb_can_return_to_selected_subscription():
    tariffs = [
        SimpleNamespace(
            id=3,
            label="Базовый (14 дней)",
            days=14,
            price_rub=125,
            price_stars=0,
            tariff_type="VPN",
        )
    ]

    markup = tariffs_kb(tariffs, back_callback="purchase_return_244")

    assert markup.inline_keyboard[-1][0].callback_data == "purchase_return_244"


def test_tariffs_kb_displays_checkout_upgrade_quote():
    tariff = SimpleNamespace(
        id=3,
        label="Базовый (14 дней)",
        days=14,
        price_rub=125,
        price_stars=1250,
        tariff_type="VPN",
    )

    texts = _button_texts(
        tariffs_kb([tariff], stars_enabled=True, price_overrides_rub={3: 10})
    )

    assert "Базовый (14 дней) - 10₽ / 100⭐" in texts


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

    # Default / show_new=False -> '➕ Создать новую' is absent
    texts_no_new = _button_texts(
        purchase_subscription_kb(243, position=0, total=2, show_upgrade=True, show_new=False)
    )
    assert texts_no_new[:4] == ["↻ Продлить", "↑ Улучшить", "1/2", "➡️"]
    assert "➕ Создать новую" not in texts_no_new
    callbacks_no_new = [
        button.callback_data
        for row in purchase_subscription_kb(
            243, position=0, total=2, show_upgrade=True, show_new=False
        ).inline_keyboard
        for button in row
    ]
    assert "purchase_browse_1" in callbacks_no_new
    assert "purchase_browse_0" not in callbacks_no_new

    # show_new=True -> '➕ Создать новую' is present
    markup_with_new = purchase_subscription_kb(243, position=0, total=2, show_upgrade=True, show_new=True)
    texts_with_new = _button_texts(markup_with_new)
    assert texts_with_new[:5] == ["↻ Продлить", "↑ Улучшить", "➕ Создать новую", "1/2", "➡️"]
    callbacks = [button.callback_data for row in markup_with_new.inline_keyboard for button in row]
    assert "buy_new_243" in callbacks

    page_two = purchase_subscription_kb(
        243, position=1, total=2, show_upgrade=True, show_new=False
    )
    assert _button_texts(page_two)[:4] == ["↻ Продлить", "↑ Улучшить", "⬅️", "2/2"]
    page_two_callbacks = [
        button.callback_data for row in page_two.inline_keyboard for button in row
    ]
    assert "purchase_browse_0" in page_two_callbacks
    assert "purchase_browse_2" not in page_two_callbacks

    page_three = purchase_subscription_kb(
        243, position=1, total=3, show_upgrade=True, show_new=False
    )
    assert _button_texts(page_three)[:5] == ["↻ Продлить", "↑ Улучшить", "⬅️", "2/3", "➡️"]
    single_page = purchase_subscription_kb(
        243, position=0, total=1, show_upgrade=True, show_new=False
    )
    assert all(text not in {"Следующая", "⬅️", "➡️"} for text in _button_texts(single_page))


def test_my_subscriptions_label_replaces_my_keys_label():
    assert "🔑 Мои подписки" in _button_texts(main_menu_kb())
    assert "🔑 Мои подписки" in _button_texts(profile_kb(has_active_subs=True))
    assert "🔑 Мои ключи" not in _button_texts(main_menu_kb())


def test_product_type_keyboard_preserves_subscription_navigation():
    markup = product_type_kb(
        back_callback="purchase_return_243",
        source_subscription_id=243,
    )
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]

    assert callbacks == [
        "ptype_vpn~243",
        "ptype_tg_proxy~243",
        "ptype_both~243",
        "purchase_return_243",
    ]


def test_trial_carousel_omits_upgrade_action():
    texts = _button_texts(
        purchase_subscription_kb(199, position=0, total=1, show_upgrade=False)
    )
    assert "↻ Продлить" in texts
    assert "↑ Улучшить" not in texts


def test_external_payment_back_reopens_same_payment_method_screen():
    from bot.handlers.payment import _payment_method_back_callback

    assert _payment_method_back_callback(17, "deferred~r~197") == "tariff_17~r~197"
    assert _payment_method_back_callback(17, "deferred~u~197") == "tariff_17~u~197"
    assert _payment_method_back_callback(17, "deferred~n~197") == "tariff_17~n~197"
    assert _payment_method_back_callback(17, "deferred") == "tariff_17"
