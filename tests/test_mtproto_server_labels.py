from bot.services.mtproto_manager import _display_label


def test_existing_netherlands_server_has_numbered_fallback_label():
    assert _display_label({"host": "72.56.71.124"}) == "🇳🇱 Нидерланды 1"


def test_new_netherlands_server_has_fallback_label():
    assert _display_label({"host": "185.70.185.209"}) == "🇳🇱 Нидерланды 2"


def test_removed_estonia_server_has_no_estonia_fallback():
    assert _display_label({"host": "45.92.174.214"}) == "45.92.174.214"
