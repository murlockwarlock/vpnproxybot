from bot.services.proxy_manager import MarzbanAPI


def test_order_inbound_tags_prioritizes_stable_vless_routes():
    tags = [
        "VLESS_REALITY_X5",
        "VLESS_REALITY_MICROSOFT",
        "VLESS_REALITY_YANDEX",
        "VLESS_REALITY_VK",
        "VLESS_REALITY_OZONE",
        "VLESS_REALITY_TRAVEL",
        "VLESS_REALITY_DISK",
        "VLESS_REALITY_CAPTCHA",
        "VLESS_REALITY_WB",
    ]

    ordered = MarzbanAPI._order_inbound_tags("vless", tags)

    assert ordered == [
        "VLESS_REALITY_TRAVEL",
        "VLESS_REALITY_YANDEX",
        "VLESS_REALITY_CAPTCHA",
        "VLESS_REALITY_VK",
        "VLESS_REALITY_MICROSOFT",
        "VLESS_REALITY_DISK",
        "VLESS_REALITY_WB",
        "VLESS_REALITY_OZONE",
        "VLESS_REALITY_X5",
    ]


def test_order_inbound_tags_keeps_unknown_vless_tags_after_preferred_ones():
    tags = [
        "VLESS_REALITY_UNKNOWN_B",
        "VLESS_REALITY_MICROSOFT",
        "VLESS_REALITY_UNKNOWN_A",
        "VLESS_REALITY_TRAVEL",
    ]

    ordered = MarzbanAPI._order_inbound_tags("vless", tags)

    assert ordered == [
        "VLESS_REALITY_TRAVEL",
        "VLESS_REALITY_MICROSOFT",
        "VLESS_REALITY_UNKNOWN_A",
        "VLESS_REALITY_UNKNOWN_B",
    ]


def test_order_inbound_tags_does_not_reorder_other_protocols():
    tags = ["trojan_b", "trojan_a"]

    assert MarzbanAPI._order_inbound_tags("trojan", tags) == tags
