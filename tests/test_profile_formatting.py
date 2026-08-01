from types import SimpleNamespace

from bot.handlers.profile import (
    _profile_vpn_subscriptions,
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


def test_key_list_includes_active_and_expired_renewable_subscriptions():
    active = SimpleNamespace(
        id=243, status=SimpleNamespace(value="active"), billing_mode="tariff",
        vpn_key="https://example.test/active", expires_at=None,
    )
    expired = SimpleNamespace(
        id=197, status=SimpleNamespace(value="expired"), billing_mode="tariff",
        vpn_key="https://example.test/expired", expires_at=None,
    )
    demo = SimpleNamespace(
        id=10, status=SimpleNamespace(value="expired"), billing_mode="demo",
        vpn_key="https://example.test/demo", expires_at=None,
    )

    assert {sub.id for sub in _profile_vpn_subscriptions([active, expired, demo])} == {197, 243}
