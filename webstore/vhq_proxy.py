"""Helpers for building public VHQ mirror URLs inside the web store."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json


def _urlsafe_b64encode(payload: bytes) -> str:
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _sign_payload(payload_part: str, secret: str) -> str:
    return hmac.new(secret.encode(), payload_part.encode(), hashlib.sha256).hexdigest()


def build_vhq_mirror_url(
    upstream_url: str,
    *,
    public_base_url: str,
    path_prefix: str,
    secret: str,
    order_id: str | None = None,
) -> str:
    upstream = str(upstream_url or "").strip()
    base = str(public_base_url or "").strip().rstrip("/")
    signing_secret = str(secret or "").strip()
    if not upstream or not base or not signing_secret:
        return upstream

    prefix_value = str(path_prefix or "").strip()
    prefix = f"/{prefix_value.strip('/')}" if prefix_value else ""
    route_base = base if (prefix and base.endswith(prefix)) else f"{base}{prefix}"
    payload = {"u": upstream}
    if order_id:
        payload["o"] = str(order_id)
    payload_part = _urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = _sign_payload(payload_part, signing_secret)
    return f"{route_base}/vhq-sub/{payload_part}.{signature}"
