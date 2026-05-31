from bot.services.relay_health import check_expected_routes, parse_relay_routes


def test_parse_relay_routes_extracts_map_entries_only():
    config = """
map $ssl_preread_server_name $relay_backend {
    eh1.vk.com              72.56.71.124:443;
    default                 45.92.174.214:443;
}

server {
    listen 443;
}
"""

    assert parse_relay_routes(config) == {
        "eh1.vk.com": "72.56.71.124:443",
        "default": "45.92.174.214:443",
    }


def test_check_expected_routes_detects_mismatch_and_unexpected_routes():
    routes = {
        "eh1.vk.com": "81.200.156.43:443",
        "default": "45.92.174.214:443",
        "unexpected.example.com": "1.2.3.4:443",
    }

    problems = check_expected_routes("relay-ru", routes)

    assert "relay-ru: route mismatch for eh1.vk.com: expected 72.56.71.124:443, got 81.200.156.43:443" in problems
    assert "relay-ru: unexpected route present for unexpected.example.com: 1.2.3.4:443" in problems
