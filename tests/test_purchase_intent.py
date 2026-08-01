from datetime import datetime, timedelta

from bot.services.purchase_intent import (
    calculate_upgrade_price_rub,
    decode_intent,
    encode_intent,
)


def test_purchase_intent_round_trip():
    encoded = encode_intent("android_tv", "upgrade", 42)
    assert encoded == "android_tv~u~42"
    assert decode_intent(encoded) == ("android_tv", "upgrade", 42)


def test_upgrade_quote_uses_current_adapt_replacement_formula():
    now = datetime(2026, 8, 1, 12, 0, 0)
    # 300 RUB / 30 days * 10 remaining = 100 RUB residual value.
    # A new 500 RUB plan therefore costs 400 RUB.
    assert calculate_upgrade_price_rub(
        current_price_rub=300,
        current_days=30,
        new_price_rub=500,
        expires_at=now + timedelta(days=10),
        now=now,
    ) == 400
