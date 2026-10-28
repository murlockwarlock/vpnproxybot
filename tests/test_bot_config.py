from bot.config import Settings


def test_bot_filters_blocked_admin_ids(monkeypatch):
    monkeypatch.setenv("ADMIN_IDS", "272982544,979514796,806750628")

    settings = Settings()

    assert settings.admin_ids == [272982544, 806750628]


def test_mtproto_servers_accepts_json(monkeypatch):
    monkeypatch.setenv(
        "MTPROTO_SERVERS",
        '[{"host":"proxy.example","port":4430,"label":"NL"}]',
    )

    assert Settings().mtproto_servers == [
        {"host": "proxy.example", "port": 4430, "label": "NL"}
    ]


def test_mtproto_servers_accepts_legacy_python_literal(monkeypatch):
    monkeypatch.setenv(
        "MTPROTO_SERVERS",
        "[{'host': 'proxy.example', 'port': 4430, 'label': 'NL'}]",
    )

    assert Settings().mtproto_servers == [
        {"host": "proxy.example", "port": 4430, "label": "NL"}
    ]


def test_mtproto_servers_falls_back_when_config_is_invalid(monkeypatch):
    monkeypatch.setenv("MTPROTO_SERVERS", "{'host': 'proxy.example'}")
    monkeypatch.setenv("MTPROTO_HOST", "legacy.example")
    monkeypatch.setenv("MTPROTO_PORT", "4430")

    assert Settings().mtproto_servers == [
        {
            "host": "legacy.example",
            "port": 4430,
            "ssh_user": "root",
            "ssh_password": "",
            "label": "NL",
        }
    ]


def test_mtproto_servers_invalid_config_without_legacy_does_not_raise(monkeypatch):
    monkeypatch.setenv("MTPROTO_SERVERS", "not-json")
    monkeypatch.delenv("MTPROTO_HOST", raising=False)

    assert Settings().mtproto_servers == []


def test_proxy_link_builder_tolerates_invalid_server_config(monkeypatch):
    from bot.services import mtproto_manager

    monkeypatch.setenv("MTPROTO_SERVERS", "[{host: proxy.example}]")
    monkeypatch.delenv("MTPROTO_HOST", raising=False)
    monkeypatch.setattr(mtproto_manager, "settings", Settings())

    assert mtproto_manager.build_all_proxy_links("secret") == []
