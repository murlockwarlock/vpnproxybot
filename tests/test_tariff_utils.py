from bot.services.tariff_utils import format_duration_days, format_subscription_duration


def test_format_duration_days_handles_non_month_tariffs():
    assert format_duration_days(7) == "7 дней"


def test_format_duration_days_handles_month_tariffs():
    assert format_duration_days(30) == "1 мес."
    assert format_duration_days(180) == "6 мес."


def test_format_duration_days_handles_year_tariffs():
    assert format_duration_days(365) == "1 год"


def test_format_subscription_duration_prefers_tariff_days():
    assert format_subscription_duration(tariff_days=7, tariff_months=0) == "7 дней"


def test_format_subscription_duration_falls_back_to_months():
    assert format_subscription_duration(tariff_days=0, tariff_months=3) == "3 мес."
