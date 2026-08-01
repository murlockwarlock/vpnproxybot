from bot.services.mtproto_manager import _display_label


def test_configured_server_label_is_displayed():
    assert _display_label({"host": "192.0.2.10", "label": "🇳🇱 Нидерланды 1"}) == "🇳🇱 Нидерланды 1"


def test_host_is_used_when_label_is_missing():
    assert _display_label({"host": "198.51.100.20"}) == "198.51.100.20"


def test_broken_label_falls_back_to_host():
    assert _display_label({"host": "203.0.113.30", "label": "\\ud83c broken"}) == "203.0.113.30"
