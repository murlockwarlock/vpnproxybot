"""Minimal Marzban API client for the web store."""

from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import urlparse

import aiohttp
from aiohttp import ClientTimeout

from webstore.config import settings

logger = logging.getLogger(__name__)



class MarzbanClient:
    """Async context manager for Marzban API calls."""

    def __init__(self) -> None:
        self.api_url = settings.marzban_api_url.rstrip("/")
        self.username = settings.marzban_username
        self.password = settings.marzban_password
        self.token: Optional[str] = None
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self) -> MarzbanClient:
        self._session = aiohttp.ClientSession()
        try:
            await self._login()
        except Exception:
            await self._session.close()
            self._session = None
            raise
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def _login(self) -> None:
        url = f"{self.api_url}/api/admin/token"
        data = {"username": self.username, "password": self.password, "grant_type": "password"}
        async with self._session.post(url, data=data) as resp:
            if resp.status == 200:
                result = await resp.json()
                self.token = result["access_token"]
            else:
                text = await resp.text()
                raise Exception(f"Marzban login failed: {resp.status} — {text}")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    async def get_inbounds(self) -> dict[str, Any]:
        url = f"{self.api_url}/api/inbounds"
        async with self._session.get(url, headers=self._headers()) as resp:
            if resp.status == 200:
                return await resp.json()
            logger.error("Failed to get inbounds: %s", resp.status)
            return {}

    async def _resolve_inbounds(self) -> tuple[dict, dict]:
        available = await self.get_inbounds()
        for protocol in ("vless", "trojan", "vmess", "shadowsocks"):
            entries = available.get(protocol, [])
            tags = [e["tag"] for e in entries if isinstance(e, dict) and e.get("tag")]
            if tags:
                return {protocol: {}}, {protocol: tags}
        raise Exception("No available inbounds on Marzban")

    async def create_user(
        self,
        username: str,
        expire: int = 0,
        note: str = "",
    ) -> Optional[dict[str, Any]]:
        proxies, inbounds = await self._resolve_inbounds()
        payload = {
            "username": username,
            "proxies": proxies,
            "inbounds": inbounds,
            "data_limit": 0,
            "expire": expire,
            "status": "active",
            "note": note,
            "on_hold_expire_duration": 0,
            "on_hold_timeout": None,
        }
        url = f"{self.api_url}/api/user"
        async with self._session.post(url, json=payload, headers=self._headers()) as resp:
            if resp.status in (200, 201):
                return await resp.json()
            if resp.status == 409:
                return await self.get_user(username)
            text = await resp.text()
            logger.error("Failed to create user %s: %s — %s", username, resp.status, text)
            return None

    async def get_user(self, username: str) -> Optional[dict[str, Any]]:
        url = f"{self.api_url}/api/user/{username}"
        async with self._session.get(url, headers=self._headers()) as resp:
            if resp.status == 200:
                return await resp.json()
            return None

    async def update_user(self, username: str, **kwargs) -> Optional[dict[str, Any]]:
        url = f"{self.api_url}/api/user/{username}"
        async with self._session.put(url, json=kwargs, headers=self._headers()) as resp:
            if resp.status == 200:
                return await resp.json()
            text = await resp.text()
            logger.error("Failed to update user %s: %s — %s", username, resp.status, text)
            return None

    async def get_subscription_url(self, username: str) -> Optional[str]:
        user_data = await self.get_user(username)
        if not user_data or "subscription_url" not in user_data:
            return None
        sub_url = user_data["subscription_url"]
        if sub_url.startswith("/"):
            sub_url = f"{self.api_url}{sub_url}"
        if settings.subscription_sub_path:
            sub_url = sub_url.replace("/sub/", f"/{settings.subscription_sub_path}/")
        # Rewrite domain to custom base URL
        if settings.subscription_base_url:
            parsed = urlparse(sub_url)
            base = settings.subscription_base_url.rstrip("/")
            sub_url = f"{base}{parsed.path}"
        if not await self._validate_subscription_url(sub_url, username=username):
            return None
        return sub_url

    async def _validate_subscription_url(self, sub_url: str, *, username: str) -> bool:
        if not sub_url.startswith(("http://", "https://")):
            return True

        timeout = ClientTimeout(total=12)
        headers = {
            "Accept": "text/plain,*/*",
            "User-Agent": "webstore-subscription-check/1.0",
        }
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(sub_url, headers=headers, ssl=False) as resp:
                    await resp.read()
                    if resp.status == 200:
                        return True
                    logger.error(
                        "Subscription URL validation failed: username=%s status=%s url=%s",
                        username,
                        resp.status,
                        sub_url,
                    )
                    return False
        except Exception as exc:
            logger.error(
                "Subscription URL validation error: username=%s url=%s error=%s",
                username,
                sub_url,
                exc,
            )
            return False
