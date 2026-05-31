from bot.config import Settings


def test_bot_filters_blocked_admin_ids(monkeypatch):
    monkeypatch.setenv("ADMIN_IDS", "272982544,979514796,806750628")

    settings = Settings()

    assert settings.admin_ids == [272982544, 806750628]
