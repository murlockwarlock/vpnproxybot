"""Helpers for exposing Adapt subscription URLs through our own mirror URL.

Adapt subscription URLs look like:
  https://network-api.adaptgroup.app/sub/{uuid}

We proxy them through our domain as:
  https://darimiru.ru/vpnbot/adapt-sub/{uuid}

The UUID is validated against the adapt_subscriptions table on each request,
ensuring we only proxy UUIDs we own.
"""

from __future__ import annotations

from typing import Any, Mapping

import aiohttp

from bot.config import settings

_TIMEOUT = aiohttp.ClientTimeout(total=15)
_ADAPT_UPSTREAM_BASE = "https://network-api.adaptgroup.app/sub"

_PASSTHROUGH_HEADERS = {
    "content-type",
    "cache-control",
    "etag",
    "last-modified",
    "subscription-userinfo",
    "profile-update-interval",
    "profile-title",
    "support-url",
    "profile-web-page-url",
    "announce",
    "announce-url",
    "sub-info-text",
}
_FORWARDED_REQUEST_HEADERS = {
    "user-agent",
    "accept",
    "if-none-match",
    "if-modified-since",
}


def _header_text(value: str) -> str:
    import base64

    encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
    return f"base64:{encoded}"


def _build_route_base() -> str:
    base = str(settings.base_webhook_url or settings.subscription_base_url or "").strip().rstrip("/")
    prefix = str(settings.webhook_path_prefix or "").strip()
    prefix_clean = f"/{prefix.strip('/')}" if prefix else ""
    if not base:
        return ""
    return base if (prefix_clean and base.endswith(prefix_clean)) else f"{base}{prefix_clean}"


def build_adapt_mirror_url(adapt_uuid: str) -> str:
    """Build branded subscription URL pointing to our proxy endpoint."""
    route_base = _build_route_base()
    if not route_base or not adapt_uuid:
        return f"{_ADAPT_UPSTREAM_BASE}/{adapt_uuid}"
    return f"{route_base}/adapt-sub/{adapt_uuid}"


async def fetch_adapt_mirror_payload(
    adapt_uuid: str,
    *,
    request_headers: Mapping[str, str] | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    """Fetch the upstream Adapt subscription response and return (status, body, headers).

    We pass through relevant headers from the upstream response.
    """
    upstream_url = f"{_ADAPT_UPSTREAM_BASE}/{adapt_uuid}"
    forward_headers: dict[str, str] = {}
    if request_headers:
        for h in _FORWARDED_REQUEST_HEADERS:
            val = request_headers.get(h) or request_headers.get(h.title())
            if val:
                forward_headers[h] = val

    async with aiohttp.ClientSession(timeout=_TIMEOUT) as http:
        async with http.get(upstream_url, headers=forward_headers) as resp:
            body = await resp.read()
            out_headers: dict[str, str] = {}
            for h in _PASSTHROUGH_HEADERS:
                val = resp.headers.get(h) or resp.headers.get(h.title())
                if val:
                    out_headers[h] = val
            # Override branding headers with our domain settings
            if settings.subscription_profile_title:
                out_headers["profile-title"] = _header_text(settings.subscription_profile_title)
            if settings.subscription_support_url:
                out_headers["support-url"] = settings.subscription_support_url
            if settings.subscription_profile_web_page_url:
                out_headers["profile-web-page-url"] = settings.subscription_profile_web_page_url
            if settings.subscription_announce:
                import base64
                out_headers["announce"] = (
                    "base64:" + base64.b64encode(settings.subscription_announce.encode()).decode()
                )
            if settings.subscription_announce_url:
                out_headers["announce-url"] = settings.subscription_announce_url
            return resp.status, body, out_headers
