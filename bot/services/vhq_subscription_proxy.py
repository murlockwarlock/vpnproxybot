"""Helpers for exposing VHQ subscription links through our own mirror URL."""

from __future__ import annotations

import base64
from datetime import datetime, timezone, timedelta
import hashlib
import hmac
import json
from typing import Any, Mapping
from urllib.parse import urlparse

import aiohttp

from bot.config import settings

_TIMEOUT = aiohttp.ClientTimeout(total=15)
_PASSTHROUGH_HEADERS = {
    "content-type",
    "cache-control",
    "etag",
    "last-modified",
    "subscription-userinfo",
    "sub-info-text",
    "profile-update-interval",
}
_OVERRIDDEN_HEADERS = {
    "profile-title",
    "support-url",
    "profile-web-page-url",
    "announce",
    "announce-url",
    "content-length",
}
_FORWARDED_REQUEST_HEADERS = {
    "user-agent",
    "accept",
    "if-none-match",
    "if-modified-since",
}


def _urlsafe_b64encode(payload: bytes) -> str:
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _urlsafe_b64decode(payload: str) -> bytes:
    padding = "=" * (-len(payload) % 4)
    return base64.urlsafe_b64decode(payload + padding)


def _sign_payload(payload_part: str, secret: str) -> str:
    return hmac.new(secret.encode(), payload_part.encode(), hashlib.sha256).hexdigest()


def _default_proxy_secret() -> str:
    return (settings.webstore_bridge_secret or settings.bot_token).strip()


def _header_text(value: str) -> str:
    encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
    return f"base64:{encoded}"


def _build_route_base(
    public_base_url: str | None,
    path_prefix: str | None,
) -> str:
    base = str(public_base_url or settings.base_webhook_url or settings.subscription_base_url).strip().rstrip("/")
    prefix_value = str(path_prefix if path_prefix is not None else settings.webhook_path_prefix).strip()
    prefix = f"/{prefix_value.strip('/')}" if prefix_value else ""
    if not base:
        return ""
    return base if (prefix and base.endswith(prefix)) else f"{base}{prefix}"


def build_vhq_mirror_url(
    upstream_url: str,
    *,
    public_base_url: str | None = None,
    path_prefix: str | None = None,
    secret: str | None = None,
    order_id: str | None = None,
) -> str:
    upstream = str(upstream_url or "").strip()
    if not upstream:
        return upstream

    route_base = _build_route_base(public_base_url, path_prefix)
    signing_secret = str(secret or _default_proxy_secret()).strip()
    if not route_base or not signing_secret:
        return upstream

    payload = {"u": upstream}
    if order_id:
        payload["o"] = str(order_id)
    payload_part = _urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = _sign_payload(payload_part, signing_secret)
    return f"{route_base}/vhq-sub/{payload_part}.{signature}"


def build_vhq_subscription_ref_url(
    subscription_id: int,
    *,
    public_base_url: str | None = None,
    path_prefix: str | None = None,
    secret: str | None = None,
) -> str:
    route_base = _build_route_base(public_base_url, path_prefix)
    signing_secret = str(secret or _default_proxy_secret()).strip()
    if not route_base or not signing_secret:
        return ""

    payload_part = _urlsafe_b64encode(
        json.dumps({"s": int(subscription_id)}, separators=(",", ":")).encode("utf-8")
    )
    signature = _sign_payload(payload_part, signing_secret)
    return f"{route_base}/vhq-sub/{payload_part}.{signature}"


def resolve_vhq_mirror_token(token: str, *, secret: str | None = None) -> dict[str, Any] | None:
    raw_token = str(token or "").strip()
    if "." not in raw_token:
        return None

    payload_part, signature = raw_token.rsplit(".", 1)
    signing_secret = str(secret or _default_proxy_secret()).strip()
    if not payload_part or not signature or not signing_secret:
        return None

    expected = _sign_payload(payload_part, signing_secret)
    if not hmac.compare_digest(expected, signature):
        return None

    try:
        payload = json.loads(_urlsafe_b64decode(payload_part).decode("utf-8"))
    except Exception:
        return None

    upstream = str(payload.get("u") or "").strip()
    if upstream.startswith(("http://", "https://")):
        result = {"kind": "upstream", "upstream_url": upstream}
        order_id = str(payload.get("o") or "").strip()
        if order_id:
            result["order_id"] = order_id
        return result

    try:
        subscription_id = int(payload.get("s") or 0)
    except (TypeError, ValueError):
        subscription_id = 0
    if subscription_id > 0:
        return {"kind": "subscription", "subscription_id": subscription_id}

    return None


def resolve_vhq_mirror_url(url: str, *, secret: str | None = None) -> dict[str, Any] | None:
    raw_url = str(url or "").strip()
    if not raw_url:
        return None

    parsed = urlparse(raw_url)
    path = parsed.path or raw_url
    marker = "/vhq-sub/"
    if marker not in path:
        return None

    token = path.rsplit(marker, 1)[-1].strip("/")
    return resolve_vhq_mirror_token(token, secret=secret)


def _format_expires_text(expires_at: datetime | None) -> str | None:
    if not expires_at:
        return None
    dt = expires_at
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    msk = timezone(timedelta(hours=3))
    return f"Действует до {dt.astimezone(msk).strftime('%d.%m.%Y %H:%M')}"


def _build_key_info_text(*, expires_at: datetime | None = None, key_id: str | None = None) -> str | None:
    parts = []
    if key_id:
        parts.append(f"Номер ключа/заказа: {key_id}")
    if expires_at:
        now = datetime.now(timezone.utc)
        dt = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=timezone.utc)
        if dt <= now:
            parts.append("Срок истёк. Оплатите новый тариф или напишите в поддержку по кнопке с самолётиком.")
        else:
            parts.append(_format_expires_text(expires_at) or "")
    return " · ".join(part for part in parts if part)


def _setting_str(name: str, default: str = "") -> str:
    return str(getattr(settings, name, default) or "").strip()


def _setting_int(name: str, default: int = 0) -> int:
    try:
        return int(getattr(settings, name, default) or 0)
    except (TypeError, ValueError):
        return default


def build_vhq_response_headers(
    upstream_headers: Mapping[str, str],
    *,
    expires_at: datetime | None = None,
    key_id: str | None = None,
) -> dict[str, str]:
    headers: dict[str, str] = {}

    for name, value in upstream_headers.items():
        lowered = name.lower()
        if lowered in _OVERRIDDEN_HEADERS:
            continue
        if lowered in _PASSTHROUGH_HEADERS:
            headers[name] = value

    title = _setting_str("subscription_profile_title")
    if title:
        headers["profile-title"] = _header_text(title)

    support_url = _setting_str("subscription_support_url")
    if support_url:
        headers["support-url"] = support_url

    profile_url = (
        _setting_str("subscription_profile_web_page_url")
        or _setting_str("subscription_base_url")
        or _setting_str("base_webhook_url")
    )
    if profile_url:
        headers["profile-web-page-url"] = profile_url

    announce = _setting_str("subscription_announce")
    if announce:
        headers["announce"] = _header_text(announce)

    announce_url = _setting_str("subscription_announce_url")
    if announce_url:
        headers["announce-url"] = announce_url

    update_interval_hours = _setting_int("subscription_update_interval_hours")
    if update_interval_hours > 0:
        headers["profile-update-interval"] = str(update_interval_hours)

    key_info = _build_key_info_text(expires_at=expires_at, key_id=key_id)
    if key_info:
        headers["sub-info-text"] = _header_text(key_info)

    return headers


async def fetch_vhq_mirror_payload(
    upstream_url: str,
    request_headers: Mapping[str, str],
    *,
    expires_at: datetime | None = None,
    key_id: str | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    forwarded_headers = {
        name: value
        for name, value in request_headers.items()
        if name.lower() in _FORWARDED_REQUEST_HEADERS
    }

    async with aiohttp.ClientSession(timeout=_TIMEOUT) as http:
        async with http.get(upstream_url, headers=forwarded_headers) as response:
            body = await response.read()
            headers = build_vhq_response_headers(response.headers, expires_at=expires_at, key_id=key_id)
            return response.status, body, headers


def get_subscription_display_key(subscription: Any) -> str | None:
    raw_key = getattr(subscription, "vpn_key", None)
    subscription_id = getattr(subscription, "id", None)
    client_name = str(getattr(subscription, "client_name", "") or "")
    if not raw_key:
        return raw_key
    if not client_name.startswith("vhq_"):
        return raw_key
    if subscription_id:
        return build_vhq_subscription_ref_url(int(subscription_id)) or raw_key
    return build_vhq_mirror_url(raw_key)
