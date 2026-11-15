from pathlib import Path

import pytest

from bot.config import settings
from webstore.routes import _effective_adapt_purchase_action


DEMO_TRIAL = {
    "days": 3,
    "price_rub": 0,
    "adapt_plan_uuid": settings.adapt_demo_plan_uuid,
}

PAID_7D = {
    "days": 7,
    "price_rub": 75,
    "adapt_plan_uuid": "paid-7d-plan-uuid",
}


def test_web_trial_renewal_is_sent_to_adapt_as_upgrade():
    assert _effective_adapt_purchase_action("renew", DEMO_TRIAL, "paid-plan") == "upgrade"


def test_web_trial_cannot_be_renewed_on_the_same_trial_plan():
    with pytest.raises(ValueError, match="платный тариф"):
        _effective_adapt_purchase_action("renew", DEMO_TRIAL, settings.adapt_demo_plan_uuid)


def test_regular_web_renewal_stays_renewal():
    paid = {"days": 30, "price_rub": 125, "adapt_plan_uuid": "paid-plan"}
    assert _effective_adapt_purchase_action("renew", paid, "paid-plan") == "renew"


def test_paid_short_tariff_stays_renewal_and_not_trial():
    assert _effective_adapt_purchase_action("renew", PAID_7D, "paid-7d-plan-uuid") == "renew"


def test_web_profile_uses_same_trial_wording_and_internal_upgrade():
    html = (Path(__file__).parents[1] / "webstore" / "templates" / "profile.html").read_text()
    assert "У вас найдено ${subscriptionCount} ${noun}. Выберите, какую хотите продлить." in html
    assert 'const renewAction = item.is_trial ? "upgrade" : "renew";' in html
    assert 'const renewAction = o.is_trial ? "upgrade" : "renew";' in html
    assert "item.is_trial && !isActive" not in html
    assert "o.is_trial && isExpired" not in html
