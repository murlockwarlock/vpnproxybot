from bot.services.relay_health import check_expected_routes, parse_relay_routes


def test_parse_relay_routes_extracts_map_entries_only():
    config = """
map $ssl_preread_server_name $relay_backend {
    eh1.vk.com              192.0.2.10:443;
    default                 198.51.100.20:443;
}

server {
    listen 443;
}
"""

    assert parse_relay_routes(config) == {
        "eh1.vk.com": "192.0.2.10:443",
        "default": "198.51.100.20:443",
    }


def test_check_expected_routes_detects_mismatch_and_unexpected_routes(monkeypatch):
    from bot.services import relay_health
    monkeypatch.setattr(relay_health, "EXPECTED_ROUTES", {
        "test-relay": {
            "eh1.vk.com": "192.0.2.10:443",
            "default": "198.51.100.20:443",
        }
    })
    routes = {
        "eh1.vk.com": "203.0.113.30:443",
        "default": "198.51.100.20:443",
        "unexpected.example.com": "1.2.3.4:443",
    }

    problems = check_expected_routes("test-relay", routes)

    assert "test-relay: route mismatch for eh1.vk.com: expected 192.0.2.10:443, got 203.0.113.30:443" in problems
    assert "test-relay: unexpected route present for unexpected.example.com: 1.2.3.4:443" in problems
