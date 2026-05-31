"""Client for the VHQ Partner API."""

from __future__ import annotations

import os
from typing import Any

import aiohttp

_TIMEOUT = aiohttp.ClientTimeout(total=15)
_ALLOWED_DAYS = {
    "lite": {1, 7, 30, 90, 365},
    "basic": {30, 180, 365},
}


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
