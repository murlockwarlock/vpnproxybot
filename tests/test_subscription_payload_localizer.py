from __future__ import annotations

import base64
import json
from urllib.parse import unquote, urlsplit
from unittest.mock import patch

import pytest

from bot.services.subscription_payload_localizer import localize_subscription_payload


def test_localizes_uri_display_labels_without_changing_connection_fields():
    body = (
        b"vless://user@example.com:443?type=tcp&security=tls#%F0%9F%9A%A8%20Subscription%20expired\n"
        b"vless://user@example.com:8443?type=ws&security=none#Contact%20support\n"
    )

    localized = localize_subscription_payload(body)
    lines = localized.decode().splitlines()

    first = urlsplit(lines[0])
    second = urlsplit(lines[1])
    assert (first.netloc, first.path, first.query) == ("user@example.com:443", "", "type=tcp&security=tls")
    assert (second.netloc, second.path, second.query) == ("user@example.com:8443", "", "type=ws&security=none")
    assert unquote(first.fragment) == "🚨 Подписка истекла"
    assert unquote(second.fragment) == "Связаться с поддержкой"


def test_localizes_json_display_fields_and_preserves_nested_values():
    body = json.dumps(
        {
            "outbounds": [
                {
                    "tag": "🚨 Subscription expired",
                    "settings": {
                        "vnext": [
                            {
                                "address": "node.example",
                                "port": 443,
                                "users": [{"id": "secret-id", "encryption": "none"}],
                            }
                        ]
                    },
                },
                {"remarks": "Contact support"},
            ]
        }
    ).encode()

    localized = json.loads(localize_subscription_payload(body))
    assert localized["outbounds"][0]["tag"] == "🚨 Подписка истекла"
    assert localized["outbounds"][1]["remarks"] == "Связаться с поддержкой"
    assert localized["outbounds"][0]["settings"]["vnext"][0] == {
        "address": "node.example",
        "port": 443,
        "users": [{"id": "secret-id", "encryption": "none"}],
    }


def test_localizes_base64_subscription_payload():
    source = b"vless://user@example.com:443?type=tcp#Subscription%20expired\n"
    encoded = base64.b64encode(source).rstrip(b"=")

    localized = localize_subscription_payload(encoded)
    decoded = base64.b64decode(localized + b"=" * (-len(localized) % 4)).decode()
    assert unquote(urlsplit(decoded.strip()).fragment) == "Подписка истекла"


def test_localizes_vmess_embedded_display_name_without_changing_credentials():
    source = {
        "v": "2",
        "ps": "🚨 Subscription expired",
        "add": "node.example",
        "port": "443",
        "id": "secret-id",
    }
    encoded = base64.b64encode(json.dumps(source, separators=(",", ":")).encode()).rstrip(b"=")

    localized = localize_subscription_payload(b"vmess://" + encoded)
    decoded = base64.b64decode(localized.split(b"://", 1)[1] + b"=" * (-len(localized.split(b"://", 1)[1]) % 4))
    result = json.loads(decoded)
    assert result["ps"] == "🚨 Подписка истекла"
    assert result["add"] == "node.example"
    assert result["port"] == "443"
    assert result["id"] == "secret-id"


def test_leaves_unrelated_and_binary_payloads_byte_for_byte():
    plain = b"vless://user@example.com:443?type=tcp#My%20server\n"
    binary = b"\x00\xff\x01\x80"

    assert localize_subscription_payload(plain) == plain
    assert localize_subscription_payload(binary) == binary


class _FakeResponse:
    def __init__(self, body, headers, status=200):
        self._body = body
        self.headers = headers
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def read(self):
        return self._body


class _FakeSession:
    def __init__(self, response):
        self.response = response
        self.request_headers = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    def get(self, _url, *, headers=None, **_kwargs):
        self.request_headers = headers
        return self.response


@pytest.mark.asyncio
async def test_adapt_proxy_localizes_body_and_does_not_forward_cache_validators():
    from bot.services import adapt_subscription_proxy

    session = _FakeSession(
        _FakeResponse(
            b"vless://user@example.com:443?type=tcp#Subscription%20expired\n",
            {
                "content-type": "text/plain",
                "etag": '"upstream"',
                "last-modified": "Wed, 21 Oct 2026 12:00:00 GMT",
                "cache-control": "max-age=3600",
            },
        )
    )
    with patch.object(adapt_subscription_proxy.aiohttp, "ClientSession", return_value=session):
        status, body, headers = await adapt_subscription_proxy.fetch_adapt_mirror_payload(
            "00000000-0000-0000-0000-000000000000",
            request_headers={"If-None-Match": '"old"', "User-Agent": "Happ"},
        )

    assert status == 200
    assert "Подписка истекла" in unquote(body.decode())
    assert "etag" not in headers
    assert "last-modified" not in headers
    assert headers["cache-control"] == "no-cache"
    assert "If-None-Match" not in session.request_headers


@pytest.mark.asyncio
async def test_vhq_proxy_localizes_body_and_does_not_forward_cache_validators():
    from bot.services import vhq_subscription_proxy

    session = _FakeSession(
        _FakeResponse(
            b"vless://user@example.com:443?type=tcp#Contact%20support\n",
            {
                "content-type": "text/plain",
                "ETag": '"upstream"',
                "Last-Modified": "Wed, 21 Oct 2026 12:00:00 GMT",
                "Cache-Control": "max-age=3600",
            },
        )
    )
    with patch.object(vhq_subscription_proxy.aiohttp, "ClientSession", return_value=session):
        status, body, headers = await vhq_subscription_proxy.fetch_vhq_mirror_payload(
            "https://example.com/subscription",
            {"If-None-Match": '"old"', "User-Agent": "Happ"},
        )

    assert status == 200
    assert "Связаться с поддержкой" in unquote(body.decode())
    assert "etag" not in headers
    assert "last-modified" not in headers
    assert headers["cache-control"] == "no-cache"
    assert "If-None-Match" not in session.request_headers
