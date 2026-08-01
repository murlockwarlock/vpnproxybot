from unittest.mock import Mock, patch

from bot import telegram_client


def test_create_telegram_bot_uses_proxy(monkeypatch):
    monkeypatch.setenv("TELEGRAM_PROXY", "http://relay.example:3128")
    session = Mock()

    with patch.object(telegram_client, "AiohttpSession", return_value=session) as session_factory:
        with patch.object(telegram_client, "Bot") as bot_factory:
            telegram_client.create_telegram_bot("1234567890:test-token")

    session_factory.assert_called_once_with(proxy="http://relay.example:3128")
    bot_factory.assert_called_once_with(token="1234567890:test-token", session=session, default=None)
