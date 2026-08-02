from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.services.purchase_intent import (
    calculate_upgrade_price_rub,
    decode_intent,
    effective_expired_adapt_action,
    encode_intent,
    get_purchase_price_rub,
)


def test_purchase_intent_round_trip():
    encoded = encode_intent("android_tv", "upgrade", 42)
    assert encoded == "android_tv~u~42"
    assert decode_intent(encoded) == ("android_tv", "upgrade", 42)


def test_same_tariff_on_expired_choice_becomes_renew():
    expired = datetime.utcnow() - timedelta(days=1)
    assert effective_expired_adapt_action(
        "upgrade",
        current_plan_uuid="same-plan",
        selected_plan_uuid="same-plan",
        expires_at=expired,
    ) == "renew"
    assert effective_expired_adapt_action(
        "upgrade",
        current_plan_uuid="old-plan",
        selected_plan_uuid="other-plan",
        expires_at=expired,
    ) == "upgrade"


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


@pytest.mark.asyncio
async def test_expired_adapt_trial_upgrade_costs_full_paid_tariff_price():
    current = SimpleNamespace(
        id=1, days=7, price_rub=45, adapt_plan_uuid="trial-plan"
    )
    target = SimpleNamespace(
        id=199, user_id=10, tariff_id=1, expires_at=datetime.utcnow() - timedelta(days=1),
        client_name="adapt_trial",
    )
    paid = SimpleNamespace(
        id=2, days=14, price_rub=75, adapt_plan_uuid="paid-plan"
    )
    session = SimpleNamespace(get=AsyncMock(side_effect=[target, current]))

    price = await get_purchase_price_rub(
        session,
        user=SimpleNamespace(id=10),
        tariff=paid,
        action="upgrade",
        target_subscription_id=199,
    )

    assert price == 75


@pytest.mark.asyncio
async def test_expired_paid_subscription_can_move_to_cheaper_tariff_for_full_price():
    current = SimpleNamespace(
        id=1, days=365, price_rub=995, adapt_plan_uuid="annual-plan"
    )
    target = SimpleNamespace(
        id=200, user_id=10, tariff_id=1,
        expires_at=datetime.utcnow() - timedelta(days=5),
        client_name="adapt_expired",
    )
    cheaper = SimpleNamespace(
        id=2, days=30, price_rub=155, adapt_plan_uuid="monthly-plan"
    )
    session = SimpleNamespace(get=AsyncMock(side_effect=[target, current]))

    price = await get_purchase_price_rub(
        session,
        user=SimpleNamespace(id=10),
        tariff=cheaper,
        action="upgrade",
        target_subscription_id=200,
    )

    assert price == 155


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("new_price", "remaining_days", "expected"),
    [
        (101, 9.8, 10),
        (130, 9.4, 37),
    ],
)
async def test_active_upgrade_quote_respects_payment_minimum_only_below_ten(
    new_price,
    remaining_days,
    expected,
):
    now = datetime.utcnow()
    current = SimpleNamespace(
        id=1, days=10, price_rub=100, adapt_plan_uuid="current-plan"
    )
    target = SimpleNamespace(
        id=201,
        user_id=10,
        tariff_id=1,
        expires_at=now + timedelta(days=remaining_days),
        client_name="adapt_active",
    )
    upgrade = SimpleNamespace(
        id=2, days=10, price_rub=new_price, adapt_plan_uuid="upgrade-plan"
    )
    session = SimpleNamespace(get=AsyncMock(side_effect=[target, current]))

    price = await get_purchase_price_rub(
        session,
        user=SimpleNamespace(id=10),
        tariff=upgrade,
        action="upgrade",
        target_subscription_id=201,
    )

    assert price == expected
