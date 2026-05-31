from bot.services.client_names import build_client_name


def test_build_client_name_with_explicit_prefix():
    assert build_client_name(123456, slot=2, prefix="bota") == "bota_tg123456_2"


def test_build_client_name_demo_and_suffix():
    assert build_client_name(123456, is_demo=True, prefix="botb") == "botb_tg123456_demo"
    assert build_client_name(123456, suffix="abcd", prefix="botb") == "botb_tg123456_abcd"
