from bot.handlers.referral import _build_referral_copy_messages


def test_referral_copy_messages_include_telegram_link():
    messages = _build_referral_copy_messages("https://t.me/testbot?start=ref_123")
    assert len(messages) == 2
    assert messages[0][0] == "Ваша реферальная ссылка для Telegram:"
    assert messages[1][0] == "https://t.me/testbot?start=ref_123"
    assert messages[1][1] is not None


def test_referral_copy_messages_include_web_link_when_present():
    messages = _build_referral_copy_messages(
        "https://t.me/testbot?start=ref_123",
        "https://example.com/buy?ref=123",
    )
    assert len(messages) == 4
    assert messages[0][0] == "Ваша реферальная ссылка для Telegram:"
    assert messages[1][0] == "https://t.me/testbot?start=ref_123"
    assert messages[2][0] == "Ваша реферальная ссылка для сайта:"
    assert messages[3][0] == "https://example.com/buy?ref=123"

