"""Client for the Adapt Group VPN API.

Docs: https://docs.adaptgroup.pro/
Base URL: https://network-api.adaptgroup.app
Auth: X-Api-Key header + api_key_id in request body.
Rate limit: 100 req/60s per api_key.
"""

from __future__ import annotations

import os
import asyncio
from typing import Any

import aiohttp

_TIMEOUT = aiohttp.ClientTimeout(total=20)
_DEFAULT_BASE_URL = "https://network-api.adaptgroup.app"


class AdaptAPIError(RuntimeError):
    """Raised when Adapt API returns an error response."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class AdaptAPI:
    """Adapt Group VPN API client."""

    def __init__(
        self,
        *,
        api_id: int | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.api_id = api_id if api_id is not None else int(os.getenv("ADAPT_API_ID", "0"))
        self.api_key = (
            api_key if api_key is not None else os.getenv("ADAPT_API_KEY", "")
        ).strip()
        self.base_url = (
            base_url if base_url is not None else os.getenv("ADAPT_BASE_URL", _DEFAULT_BASE_URL)
        ).strip().rstrip("/")

    @property
    def enabled(self) -> bool:
        return bool(self.api_id and self.api_key and self.base_url)

    # ── Balance ────────────────────────────────────────────────────────────

    async def get_balance(self) -> dict[str, Any]:
        """Return the current integration balance in USD.
        
        Docs: https://docs.adaptgroup.pro/docs/api-vpn/check-balance
        """
        return await self._post("/balance/check", {}, retry_safe=True)

    # ── Plans ──────────────────────────────────────────────────────────────

    async def list_plans(self) -> list[dict[str, Any]]:
        """Return all plans for this integration."""
        data = await self._post("/plans/list", {}, retry_safe=True)
        return data.get("plans") or []

    # ── Subscription lifecycle ─────────────────────────────────────────────

    async def create_subscription(
        self,
        plan_uuid: str,
        *,
        external_user_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a new subscription for the given plan.

        Returns CreateSubscriptionResponse:
          subscription_uuid, subscription_url, plan_uuid, end_date, devices, days,
          traffic_limit_bytes, balance.
        """
        payload: dict[str, Any] = {"plan_uuid": plan_uuid}
        if external_user_id:
            payload["external_user_id"] = external_user_id
        return await self._post("/subs/create", payload)

    async def renew_subscription(self, subscription_uuid: str) -> dict[str, Any]:
        """Renew subscription for another plan period.

        Returns RenewSubscriptionResponse with new end_date.
        """
        return await self._post("/subs/renew", {"subscription_uuid": subscription_uuid})

    async def renew_subscription_custom(self, subscription_uuid: str, custom_days: int) -> dict[str, Any]:
        """Renew subscription for a custom number of days.
        
        Returns RenewSubscriptionCustomResponse with new end_date and total_price.
        """
        return await self._post(
            "/subs/renew/custom",
            {"subscription_uuid": subscription_uuid, "custom_days": custom_days},
        )

    async def freeze_subscription(self, subscription_uuid: str) -> dict[str, Any]:
        """Pause the subscription countdown.

        Returns FreezeSubscriptionResponse with frozen_at timestamp.
        """
        return await self._post("/subs/freeze", {"subscription_uuid": subscription_uuid})

    async def unfreeze_subscription(self, subscription_uuid: str) -> dict[str, Any]:
        """Resume a frozen subscription.

        Returns UnfreezeSubscriptionResponse with new end_date.
        """
        return await self._post("/subs/unfreeze", {"subscription_uuid": subscription_uuid})

    async def upgrade_subscription(
        self, subscription_uuid: str, new_plan_uuid: str
    ) -> dict[str, Any]:
        """Upgrade subscription to a more expensive plan (prorated cost).

        Returns UpgradeSubscriptionResponse with upgrade_price and new devices count.
        """
        return await self._post(
            "/subs/upgrade",
            {"subscription_uuid": subscription_uuid, "new_plan_uuid": new_plan_uuid},
        )

    async def purchase_traffic(
        self, subscription_uuid: str, gb_amount: int
    ) -> dict[str, Any]:
        """Purchase additional traffic GB for a traffic-limited subscription.

        Returns PurchaseTrafficResponse with total_price and additional_bytes.
        """
        return await self._post(
            "/subs/traffic",
            {"subscription_uuid": subscription_uuid, "gb_amount": gb_amount},
        )

    async def get_status(self, subscription_uuid: str) -> dict[str, Any]:
        """Fetch current subscription status.

        Returns SubscriptionStatusResponse with is_active, is_frozen, end_date,
        used_traffic_bytes, traffic_limit_bytes, etc.
        """
        return await self._post("/subs/status", {"subscription_uuid": subscription_uuid}, retry_safe=True)

    async def get_devices(self, subscription_uuid: str) -> list[dict[str, Any]]:
        """Return list of registered devices for this subscription."""
        data = await self._post("/subs/devices", {"subscription_uuid": subscription_uuid}, retry_safe=True)
        return data.get("devices") or []

    async def delete_device(self, subscription_uuid: str, device_id: int) -> bool:
        """Remove a registered device by its ID. Returns True on success."""
        data = await self._post(
            "/subs/devices/delete",
            {"subscription_uuid": subscription_uuid, "device_id": device_id},
        )
        return bool(data.get("success"))

    # ── Internal ───────────────────────────────────────────────────────────

    async def _post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        retry_safe: bool = False,
    ) -> dict[str, Any]:
        if not self.enabled:
            raise AdaptAPIError("Adapt API is not configured (missing api_id or api_key)")

        body = {"api_key_id": self.api_id, **payload}
        headers = {
            "X-Api-Key": self.api_key,
            "Content-Type": "application/json",
        }
        url = f"{self.base_url}{path}"

        attempts = 3 if retry_safe else 1
        for attempt in range(1, attempts + 1):
            try:
                async with aiohttp.ClientSession(timeout=_TIMEOUT) as http:
                    async with http.post(url, json=body, headers=headers) as resp:
                        try:
                            data = await resp.json(content_type=None)
                        except Exception:
                            text = (await resp.text()).strip() or "Empty response"
                            raise AdaptAPIError(text, status=resp.status) from None

                        if resp.status >= 400:
                            detail = (
                                data.get("detail")
                                if isinstance(data, dict)
                                else str(data)
                            ) or f"HTTP {resp.status}"
                            raise AdaptAPIError(detail, status=resp.status)

                        if isinstance(data, dict) and not data.get("success", True):
                            detail = data.get("message") or "Unknown Adapt API error"
                            raise AdaptAPIError(detail, status=resp.status)

                        return data
            except AdaptAPIError as exc:
                if attempt >= attempts or exc.status not in {429, 500, 502, 503, 504}:
                    raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if attempt >= attempts:
                    raise AdaptAPIError(f"Adapt transport error: {exc}") from exc
            await asyncio.sleep(float(attempt))

        raise AdaptAPIError("Adapt request failed")
