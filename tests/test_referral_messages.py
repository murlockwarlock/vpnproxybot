from bot.handlers.referral import _build_referral_copy_messages


def test_referral_copy_messages_include_telegram_link():
    messages = _build_referral_copy_messages("https://t.me/testbot?start=ref_123")
    assert messages == [
        "Ваша реферальная ссылка для Telegram:\n\n<code>https://t.me/testbot?start=ref_123</code>"
    ]


def test_referral_copy_messages_include_web_link_when_present():
    messages = _build_referral_copy_messages(
        "https://t.me/testbot?start=ref_123",
        "https://example.com/buy?ref=123",
    )
    assert messages == [
        "Ваша реферальная ссылка для Telegram:\n\n<code>https://t.me/testbot?start=ref_123</code>",
        "Ваша реферальная ссылка для сайта:\n\n<code>https://example.com/buy?ref=123</code>",
    ]
