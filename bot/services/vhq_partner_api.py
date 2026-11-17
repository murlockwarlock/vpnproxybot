"""Client for the VHQ Partner API."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp

_TIMEOUT = aiohttp.ClientTimeout(total=15)
_ALLOWED_DAYS = {
    "lite": {1, 7, 30, 90, 365},
    "basic": {30, 180, 365},
}
VHQ_RENEW_CONFIRMATION_TOLERANCE = timedelta(hours=36)


def is_vhq_renewal_active_until_confirmed(
    candidate_active_until: datetime | None,
    *,
    expected_active_until: datetime | None,
    baseline_active_until: datetime | None = None,
) -> bool:
    """Verify VHQ active_until reflects a newly applied/scheduled renewal cycle.

    VHQ anchors renewals to ``renews_at = active_until - 12h`` (or upstream
    activation timestamp on expired orders), so a genuine 30-day renewal can
    legitimately return ``expected - 12h`` .. ``expected - 20h`` while stale
    responses stay at or near ``baseline_active_until``.
    """
    if candidate_active_until is None or expected_active_until is None:
        return False
    cand = (
        candidate_active_until.replace(tzinfo=None)
        if candidate_active_until.tzinfo is not None
        else candidate_active_until
    )
    exp = (
        expected_active_until.replace(tzinfo=None)
        if expected_active_until.tzinfo is not None
        else expected_active_until
    )
    span = timedelta(days=30)
    if baseline_active_until is not None:
        base = (
            baseline_active_until.replace(tzinfo=None)
            if baseline_active_until.tzinfo is not None
            else baseline_active_until
        )
        if cand <= base + timedelta(days=1):
            return False
        if exp > base:
            span = exp - base
    tolerance = (
        VHQ_RENEW_CONFIRMATION_TOLERANCE
        if span >= timedelta(days=7)
        else timedelta(hours=2)
    )
    return cand >= (exp - tolerance)


class VHQPartnerAPIError(RuntimeError):
    """Raised when VHQ Partner API returns an error response."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class VHQPartnerAPI:
    def __init__(self, *, api_key: str | None = None, base_url: str | None = None) -> None:
        self.api_key = (api_key if api_key is not None else os.getenv("VHQ_PARTNER_API_KEY", "")).strip()
        self.base_url = (
            base_url
            if base_url is not None
            else os.getenv(
                "VHQ_PARTNER_API_URL",
                "https://yhmaeogxdxqszffrbjui.supabase.co/functions/v1/partner-api",
            )
        ).strip()

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.base_url)

    async def get_balance(self) -> dict[str, Any]:
        return await self._request("balance", method="GET")

    async def buy(self, *, tier: str, days: int) -> dict[str, Any]:
        normalized_tier = tier.strip().lower()
        allowed_days = _ALLOWED_DAYS.get(normalized_tier)
        if not allowed_days:
            raise VHQPartnerAPIError(f"Unsupported VHQ tier: {tier}")
        if days not in allowed_days:
            raise VHQPartnerAPIError(f"Unsupported VHQ duration for {normalized_tier}: {days}")
        return await self._request(
            "buy",
            method="POST",
            payload={"tier": normalized_tier, "days": days},
        )

    async def renew(self, *, order_id: str) -> dict[str, Any]:
        normalized_order_id = str(order_id or "").strip()
        if not normalized_order_id:
            raise VHQPartnerAPIError("Missing order_id for VHQ renew")
        return await self._request(
            "renew",
            method="POST",
            payload={"order_id": normalized_order_id},
        )

    async def get_orders(self) -> dict[str, Any]:
        return await self._request("orders", method="GET")

    @staticmethod
    def extract_subscription_url(data: dict[str, Any] | None) -> str:
        """Return the issued subscription URL from a VHQ order response.

        VHQ used to return ``config_url``. On 2026-04-15 live responses for
        ``lite`` trial purchases started returning ``branded_url`` instead.
        Accept both to stay compatible with the current API shape.
        """
        if not isinstance(data, dict):
            return ""
        for key in ("config_url", "branded_url"):
            value = str(data.get(key, "")).strip()
            if value:
                return value
        return ""

    @staticmethod
    def extract_order_id(data: dict[str, Any] | None) -> str:
        """Return the order ID from a VHQ response."""
        if not isinstance(data, dict):
            return ""
        for key in ("order_id", "id", "orderId"):
            val = str(data.get(key) or "").strip()
            if val:
                return val
        return ""

    @staticmethod
    def extract_active_until(data: dict[str, Any] | None) -> datetime | None:
        """Return the active_until timestamp parsed from a VHQ response."""
        if not isinstance(data, dict):
            return None
        raw = str(data.get("active_until") or "").strip()
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt
        except Exception:
            pass
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(raw[:19], fmt)
            except Exception:
                pass
        return None

    async def _request(
        self,
        action: str,
        *,
        method: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.enabled:
            raise VHQPartnerAPIError("VHQ Partner API is not configured")

        headers = {"X-API-Key": self.api_key}
        if payload is not None:
            headers["Content-Type"] = "application/json"

        url = f"{self.base_url}?action={action}"
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as http:
            async with http.request(method, url, json=payload, headers=headers) as resp:
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    text = (await resp.text()).strip() or "Empty response"
                    raise VHQPartnerAPIError(text, status=resp.status) from None

                if resp.status >= 400:
                    message = data.get("error") if isinstance(data, dict) else None
                    raise VHQPartnerAPIError(message or f"HTTP {resp.status}", status=resp.status)
                if isinstance(data, dict) and data.get("error"):
                    raise VHQPartnerAPIError(str(data["error"]), status=resp.status)
                if not isinstance(data, dict):
                    raise VHQPartnerAPIError("Unexpected VHQ response format", status=resp.status)
                return data
