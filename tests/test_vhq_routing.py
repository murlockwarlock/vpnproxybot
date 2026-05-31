from types import SimpleNamespace

from bot.services.vhq_routing import (
    get_vhq_spec_for_store_tariff,
    get_vhq_spec_for_tariff,
    is_vhq_subscription,
    is_vhq_tariff,
)


def test_vhq_tariff_routing_matches_basic_tariffs():
    assert get_vhq_spec_for_tariff(SimpleNamespace(label="Базовый (1 день)", days=1, price_rub=10)) == {
        "tier": "lite",
        "days": 1,
    }
    assert get_vhq_spec_for_tariff(SimpleNamespace(label="Базовый (7 дней)", days=7, price_rub=59)) == {
        "tier": "lite",
        "days": 7,
    }
    assert get_vhq_spec_for_tariff(SimpleNamespace(label="Премиум (1 месяц)", days=30, price_rub=399)) == {
        "tier": "basic",
        "days": 30,
    }
    assert is_vhq_tariff(SimpleNamespace(label="VIP 30 дней", days=30, price_rub=149)) is True
    assert is_vhq_tariff(SimpleNamespace(label="Лайт (1 месяц)", days=30, price_rub=95)) is False
    assert is_vhq_tariff(SimpleNamespace(label="Базовый (1 месяц)", days=30, price_rub=249)) is False
    assert is_vhq_tariff(SimpleNamespace(label="Премиум (90 дней)", days=90, price_rub=999)) is False


def test_vhq_tariff_routing_uses_explicit_tier_when_label_changes():
    tariff = SimpleNamespace(label="Премиум без ограничений", days=30, price_rub=399, vhq_tier="basic")

    assert get_vhq_spec_for_tariff(tariff) == {"tier": "basic", "days": 30}
    assert is_vhq_tariff(tariff) is True


def test_adapt_uuid_has_priority_over_vhq_tier():
    tariff = SimpleNamespace(
        label="Премиум",
        days=30,
        price_rub=399,
        vhq_tier="basic",
        adapt_plan_uuid="adapt-plan",
    )

    assert get_vhq_spec_for_tariff(tariff) is None


def test_vhq_store_tariff_routing_uses_explicit_provider_flag():
    assert get_vhq_spec_for_store_tariff(
        {"provider": "vhq", "vhq_tier": "lite", "days": 1}
    ) == {"tier": "lite", "days": 1}
    assert get_vhq_spec_for_store_tariff(
        {"provider": "vhq", "vhq_tier": "lite", "days": 7}
    ) == {"tier": "lite", "days": 7}
    assert get_vhq_spec_for_store_tariff(
        {"provider": "vhq", "vhq_tier": "basic", "days": 30}
    ) == {"tier": "basic", "days": 30}
    assert get_vhq_spec_for_store_tariff(
        {"provider": "marzban", "days": 30}
    ) is None


def test_vhq_subscription_detection_uses_client_name_prefix():
    assert is_vhq_subscription(SimpleNamespace(client_name="vhq_order123")) is True
    assert is_vhq_subscription(SimpleNamespace(client_name="botuser1")) is False
