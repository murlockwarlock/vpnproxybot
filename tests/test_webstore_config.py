import importlib
import sqlite3

from webstore.config import TARIFFS, TARIFFS_BY_KEY


def test_webstore_basic_tariffs_match_current_offering():
    assert [tariff["key"] for tariff in TARIFFS] == ["vpn_30", "basic_1", "basic_7", "basic_30", "premium_30"]

    assert TARIFFS_BY_KEY["vpn_30"]["label"] == "Лайт (1 месяц)"
    assert TARIFFS_BY_KEY["vpn_30"]["provider"] == "marzban"

    assert TARIFFS_BY_KEY["basic_1"]["label"] == "Базовый (1 день)"
    assert TARIFFS_BY_KEY["basic_1"]["days"] == 1
    assert TARIFFS_BY_KEY["basic_1"]["price_rub"] == 10
    assert TARIFFS_BY_KEY["basic_1"]["badge"] == "1 раз"
    assert "один раз" in TARIFFS_BY_KEY["basic_1"]["description"].lower()
    assert TARIFFS_BY_KEY["basic_1"]["provider"] == "vhq"

    assert TARIFFS_BY_KEY["basic_7"]["label"] == "Базовый (7 дней)"
    assert TARIFFS_BY_KEY["basic_7"]["days"] == 7
    assert TARIFFS_BY_KEY["basic_7"]["price_rub"] == 59
    assert TARIFFS_BY_KEY["basic_7"]["description"] == "С обходом блокировок на мобильном интернете"
    assert TARIFFS_BY_KEY["basic_7"]["provider"] == "vhq"

    assert TARIFFS_BY_KEY["basic_30"]["label"] == "Базовый (1 месяц)"
    assert TARIFFS_BY_KEY["basic_30"]["days"] == 30
    assert TARIFFS_BY_KEY["basic_30"]["price_rub"] == 249
    assert TARIFFS_BY_KEY["basic_30"]["provider"] == "vhq"

    assert TARIFFS_BY_KEY["premium_30"]["label"] == "Премиум (1 месяц)"
    assert TARIFFS_BY_KEY["premium_30"]["days"] == 30
    assert TARIFFS_BY_KEY["premium_30"]["price_rub"] == 399
    assert "70 серверов" in TARIFFS_BY_KEY["premium_30"]["description"]
    assert TARIFFS_BY_KEY["premium_30"]["provider"] == "vhq"


def test_webstore_can_filter_tariffs_by_env(monkeypatch):
    import webstore.config as webstore_config

    monkeypatch.setenv("WEBSTORE_ENABLED_TARIFF_KEYS", "vpn_30,basic_7")
    reloaded = importlib.reload(webstore_config)

    try:
        assert [tariff["key"] for tariff in reloaded.TARIFFS] == ["vpn_30", "basic_7"]
        assert set(reloaded.TARIFFS_BY_KEY) == {"vpn_30", "basic_7"}
    finally:
        monkeypatch.delenv("WEBSTORE_ENABLED_TARIFF_KEYS", raising=False)
        importlib.reload(webstore_config)


def test_webstore_can_enable_uskoritel_seven_day_tariff(monkeypatch):
    import webstore.config as webstore_config

    monkeypatch.setenv("WEBSTORE_TARIFF_VPN_7_ENABLED", "1")
    monkeypatch.setenv("WEBSTORE_TARIFF_VPN_7_PRICE_RUB", "23")
    monkeypatch.setenv("WEBSTORE_ENABLED_TARIFF_KEYS", "vpn_7,vpn_30")
    reloaded = importlib.reload(webstore_config)

    try:
        assert [tariff["key"] for tariff in reloaded.TARIFFS] == ["vpn_7", "vpn_30"]
        assert reloaded.TARIFFS_BY_KEY["vpn_7"]["label"] == "7 дней"
        assert reloaded.TARIFFS_BY_KEY["vpn_7"]["days"] == 7
        assert reloaded.TARIFFS_BY_KEY["vpn_7"]["price_rub"] == 23
        assert reloaded.TARIFFS_BY_KEY["vpn_7"]["provider"] == "marzban"
    finally:
        monkeypatch.delenv("WEBSTORE_TARIFF_VPN_7_ENABLED", raising=False)
        monkeypatch.delenv("WEBSTORE_TARIFF_VPN_7_PRICE_RUB", raising=False)
        monkeypatch.delenv("WEBSTORE_ENABLED_TARIFF_KEYS", raising=False)
        importlib.reload(webstore_config)


def test_webstore_replaces_static_monthly_basic_with_adapt_when_uuid_is_set(monkeypatch):
    import webstore.config as webstore_config

    monkeypatch.setenv("ADAPT_PLAN_UUID_BASIC_30", "adapt-plan")
    monkeypatch.setenv("WEBSTORE_TARIFF_ADAPT_30_PRICE_RUB", "249")
    monkeypatch.delenv("WEBSTORE_BOT_DB_PATH", raising=False)
    monkeypatch.setenv("WEBSTORE_SYNC_TARIFFS_FROM_BOT_DB", "0")
    reloaded = importlib.reload(webstore_config)

    try:
        assert [tariff["key"] for tariff in reloaded.TARIFFS].count("basic_30") == 1
        assert "adapt_basic_30" not in reloaded.TARIFFS_BY_KEY
        assert reloaded.TARIFFS_BY_KEY["basic_30"]["provider"] == "adapt"
        assert reloaded.TARIFFS_BY_KEY["basic_30"]["adapt_plan_uuid"] == "adapt-plan"
        assert "39 ГБ" not in reloaded.TARIFFS_BY_KEY["basic_30"]["description"]
        assert "1 устройство" in reloaded.TARIFFS_BY_KEY["basic_30"]["description"]
        assert "1 устройство" in reloaded.TARIFFS_BY_KEY["basic_30"]["features"]
        assert all("3 устройства" not in feature for feature in reloaded.TARIFFS_BY_KEY["basic_30"]["features"])
        assert any("ГБ на обходы" in feature for feature in reloaded.TARIFFS_BY_KEY["basic_30"]["features"])
    finally:
        monkeypatch.delenv("ADAPT_PLAN_UUID_BASIC_30", raising=False)
        monkeypatch.delenv("WEBSTORE_TARIFF_ADAPT_30_PRICE_RUB", raising=False)
        monkeypatch.delenv("WEBSTORE_SYNC_TARIFFS_FROM_BOT_DB", raising=False)
        importlib.reload(webstore_config)


def test_webstore_can_sync_visible_tariffs_from_bot_db(monkeypatch, tmp_path):
    import webstore.config as webstore_config

    db_path = tmp_path / "bot.db"
    con = sqlite3.connect(db_path)
    con.execute(
        """
        CREATE TABLE tariffs (
            id INTEGER PRIMARY KEY,
            label TEXT,
            days INTEGER,
            price_rub INTEGER,
            price_stars INTEGER,
            tariff_type TEXT,
            is_active INTEGER,
            is_admin_only INTEGER,
            adapt_plan_uuid TEXT,
            vhq_tier TEXT
        )
        """
    )
    con.executemany(
        """
        INSERT INTO tariffs
        (id, label, days, price_rub, price_stars, tariff_type, is_active, is_admin_only, adapt_plan_uuid, vhq_tier)
        VALUES (?, ?, ?, ?, 0, 'VPN', ?, ?, ?, ?)
        """,
        [
            (0, "Базовый (1 день)", 1, 10, 1, 0, "", ""),
            (1, "Лайт (30 дней)", 30, 95, 1, 0, "", ""),
            (2, "Базовый (30 дней)", 30, 249, 1, 0, "adapt-plan", ""),
            (3, "Базовый (1 месяц) vhq", 30, 249, 1, 1, "", "lite"),
            (4, "Премиум (1 месяц) vhq", 30, 399, 1, 1, "", "basic"),
        ],
    )
    con.commit()
    con.close()

    monkeypatch.setenv("WEBSTORE_BOT_DB_PATH", str(db_path))
    monkeypatch.setenv("WEBSTORE_SYNC_TARIFFS_FROM_BOT_DB", "1")
    reloaded = importlib.reload(webstore_config)

    try:
        assert [tariff["key"] for tariff in reloaded.TARIFFS] == ["basic_1", "vpn_30", "basic_30"]
        assert reloaded.TARIFFS_BY_KEY["basic_1"]["provider"] == "vhq"
        assert reloaded.TARIFFS_BY_KEY["basic_1"]["vhq_tier"] == "lite"
        assert reloaded.TARIFFS_BY_KEY["basic_30"]["provider"] == "adapt"
        assert reloaded.TARIFFS_BY_KEY["basic_30"]["price_rub"] == 249
        assert reloaded.TARIFFS_BY_KEY["basic_30"]["adapt_plan_uuid"] == "adapt-plan"
    finally:
        monkeypatch.delenv("WEBSTORE_BOT_DB_PATH", raising=False)
        monkeypatch.delenv("WEBSTORE_SYNC_TARIFFS_FROM_BOT_DB", raising=False)
        importlib.reload(webstore_config)


def test_webstore_synced_bot_tariffs_default_to_marzban_without_adapt_uuid_except_vhq_exceptions(monkeypatch, tmp_path):
    import webstore.config as webstore_config

    db_path = tmp_path / "bot_default_provider.db"
    con = sqlite3.connect(db_path)
    con.execute(
        """
        CREATE TABLE tariffs (
            id INTEGER PRIMARY KEY,
            label TEXT,
            days INTEGER,
            price_rub INTEGER,
            price_stars INTEGER,
            tariff_type TEXT,
            is_active INTEGER,
            is_admin_only INTEGER,
            adapt_plan_uuid TEXT,
            vhq_tier TEXT
        )
        """
    )
    con.executemany(
        """
        INSERT INTO tariffs
        (id, label, days, price_rub, price_stars, tariff_type, is_active, is_admin_only, adapt_plan_uuid, vhq_tier)
        VALUES (?, ?, ?, ?, 0, 'VPN', ?, ?, ?, ?)
        """,
        [
            (0, "Базовый (1 день)", 1, 10, 1, 0, "", ""),
            (1, "Базовый (30 дней)", 30, 249, 1, 0, "", ""),
            (2, "Максимум (30 дней)", 30, 399, 1, 0, "", "basic"),
            (3, "Премиум (1 месяц)", 30, 399, 1, 0, "", ""),
        ],
    )
    con.commit()
    con.close()

    monkeypatch.setenv("WEBSTORE_BOT_DB_PATH", str(db_path))
    monkeypatch.setenv("WEBSTORE_SYNC_TARIFFS_FROM_BOT_DB", "1")
    reloaded = importlib.reload(webstore_config)

    try:
        assert reloaded.TARIFFS_BY_KEY["basic_1"]["provider"] == "vhq"
        assert reloaded.TARIFFS_BY_KEY["basic_1"]["vhq_tier"] == "lite"
        assert reloaded.TARIFFS_BY_KEY["basic_30"]["provider"] == "marzban"
        assert reloaded.TARIFFS_BY_KEY["premium_30"]["provider"] == "vhq"
        assert reloaded.TARIFFS_BY_KEY["premium_30"]["vhq_tier"] == "basic"
        assert reloaded.TARIFFS_BY_KEY["premium_30_3"]["provider"] == "vhq"
        assert reloaded.TARIFFS_BY_KEY["premium_30_3"]["vhq_tier"] == "basic"
        assert "vhq_tier" not in reloaded.TARIFFS_BY_KEY["basic_30"]
    finally:
        monkeypatch.delenv("WEBSTORE_BOT_DB_PATH", raising=False)
        monkeypatch.delenv("WEBSTORE_SYNC_TARIFFS_FROM_BOT_DB", raising=False)
        importlib.reload(webstore_config)


def test_webstore_filters_blocked_admin_ids(monkeypatch):
    from webstore.config import WebStoreSettings

    monkeypatch.setenv("ADMIN_IDS", "272982544,979514796,806750628")
    monkeypatch.setenv("SUPPORT_AGENT_IDS", "979514796,806750628")

    settings = WebStoreSettings()

    assert settings.admin_ids == [272982544, 806750628]
    assert settings.support_agent_ids == [806750628]
