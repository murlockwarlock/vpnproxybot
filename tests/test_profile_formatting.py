from types import SimpleNamespace

from bot.handlers.profile import (
    _subscription_device_slots,
    _subscription_tariff_label,
)


def test_subscription_tariff_label_for_tg_proxy_only():
    sub = SimpleNamespace(id=1, vpn_key="mtproto_only", tariff_days=30, tariff_months=1)
    assert _subscription_tariff_label(sub, [SimpleNamespace(subscription_id=1)]) == "TG-ускоритель - 1 мес."


def test_subscription_tariff_label_for_combined_access():
    sub = SimpleNamespace(id=2, vpn_key="https://example.com/sub", tariff_days=30, tariff_months=1)
    assert _subscription_tariff_label(sub, [SimpleNamespace(subscription_id=2)]) == "Весь интернет + TG-ускоритель - 1 мес."


def test_subscription_device_slots_uses_included_slots_for_vhq():
    sub = SimpleNamespace(client_name="vhq_order123", device_slots=1)
    assert _subscription_device_slots(sub, included_slots=3) == 3


def test_subscription_device_slots_keeps_actual_slots_for_non_vhq():
    sub = SimpleNamespace(client_name="local_sub", device_slots=5)
    assert _subscription_device_slots(sub, included_slots=3) == 5
