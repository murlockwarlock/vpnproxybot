from datetime import datetime
from types import SimpleNamespace

from bot.services.vhq_subscription_proxy import (
    build_vhq_mirror_url,
    build_vhq_subscription_ref_url,
    build_vhq_response_headers,
    get_subscription_display_key,
    resolve_vhq_mirror_url,
    resolve_vhq_mirror_token,
)


def test_vhq_proxy_url_roundtrip_with_explicit_settings():
    upstream = "https://example.com/sub/abc123"
    public = build_vhq_mirror_url(
        upstream,
        public_base_url="https://darimiru.ru",
        path_prefix="/vpnbot",
        secret="secret123",
    )

    assert public.startswith("https://darimiru.ru/vpnbot/vhq-sub/")
    token = public.rsplit("/", 1)[-1]
    resolved = resolve_vhq_mirror_token(token, secret="secret123")
    assert resolved == {"kind": "upstream", "upstream_url": upstream}
    assert resolve_vhq_mirror_url(public, secret="secret123") == {
        "kind": "upstream",
        "upstream_url": upstream,
    }


def test_vhq_proxy_token_rejects_tampering():
    public = build_vhq_mirror_url(
        "https://example.com/sub/abc123",
        public_base_url="https://darimiru.ru",
        path_prefix="/vpnbot",
        secret="secret123",
    )
    token = public.rsplit("/", 1)[-1]
    tampered = token[:-1] + ("0" if token[-1] != "0" else "1")
    assert resolve_vhq_mirror_token(tampered, secret="secret123") is None


def test_vhq_proxy_url_does_not_duplicate_existing_prefix():
    public = build_vhq_mirror_url(
        "https://example.com/sub/abc123",
        public_base_url="https://darimiru.ru/vpnbot",
        path_prefix="/vpnbot",
        secret="secret123",
    )
    assert public.startswith("https://darimiru.ru/vpnbot/vhq-sub/")


def test_vhq_subscription_ref_url_roundtrip():
    public = build_vhq_subscription_ref_url(
        113,
        public_base_url="https://darimiru.ru",
        path_prefix="/vpnbot",
        secret="secret123",
    )
    token = public.rsplit("/", 1)[-1]
    assert resolve_vhq_mirror_token(token, secret="secret123") == {
        "kind": "subscription",
        "subscription_id": 113,
    }


def test_vhq_response_headers_include_pretty_expiry():
    headers = build_vhq_response_headers({}, expires_at=datetime(2026, 4, 16, 18, 43, 11))
    assert "sub-info-text" in headers


def test_display_key_wraps_only_vhq_subscriptions():
    vhq_sub = SimpleNamespace(id=113, client_name="vhq_order123", vpn_key="https://example.com/raw")
    local_sub = SimpleNamespace(client_name="local123", vpn_key="https://example.com/local")

    assert get_subscription_display_key(local_sub) == local_sub.vpn_key

    vhq_display = build_vhq_subscription_ref_url(
        vhq_sub.id,
        public_base_url="https://darimiru.ru",
        path_prefix="/vpnbot",
        secret="secret123",
    )
    assert resolve_vhq_mirror_token(vhq_display.rsplit("/", 1)[-1], secret="secret123") == {
        "kind": "subscription",
        "subscription_id": vhq_sub.id,
    }
