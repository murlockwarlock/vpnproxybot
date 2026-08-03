from bot.services.notifications import _build_admin_payment_text


def test_admin_payment_notification_separates_price_and_paid_amount():
    text = _build_admin_payment_text(
        telegram_id=123,
        full_name="Покупатель",
        username="buyer",
        amount_rub=15,
        price_rub=105,
        tariff_label="Базовый",
        method="💳 YooKassa",
        platform="ios",
    )

    assert "Стоимость: <b>105 ₽</b>" in text
    assert "Оплачено: <b>15 ₽</b>" in text
