import logging
from typing import Optional, Dict, Any
import aiohttp
from aiohttp import ClientTimeout
from bot.models import Server
from bot.services.client_names import build_client_name

logger = logging.getLogger(__name__)

_PREFERRED_VLESS_TAG_ORDER: tuple[str, ...] = (
    # Relay-capable and empirically stable options should appear first for clients.
    "VLESS_REALITY_TRAVEL",
    "VLESS_REALITY_YANDEX",
    "VLESS_REALITY_CAPTCHA",
    "VLESS_REALITY_VK",
    # Direct-only fallback.
    "VLESS_REALITY_MICROSOFT",
    # Secondary relay-capable options.
    "VLESS_REALITY_DISK",
    "VLESS_REALITY_WB",
    # Nighttime-problematic SNI stay last.
    "VLESS_REALITY_OZONE",
    "VLESS_REALITY_X5",
)


def generate_marzban_username(telegram_id: int, slot: int = 1) -> str:
    """Generate a readable Marzban username unique for the current bot.

    Example: b1a2b3c4_tg806750628_1, b1a2b3c4_tg806750628_2
    Slot number allows one Telegram user to have multiple device keys.
    """
    return build_client_name(telegram_id, slot=slot)


class MarzbanAPI:
    """Client for interacting with the Marzban Panel API."""

    def __init__(self, server: Server):
        import os
        fallback_password = os.getenv("MARZBAN_API_PASSWORD", "")
        password = server.api_password or fallback_password
        if not server.api_url or not server.api_username or not password:
            raise ValueError(f"Server {server.name} missing Marzban API credentials.")

        self.api_url = server.api_url.rstrip("/")
        self.username = server.api_username
        self.password = password
        self.token: Optional[str] = None
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        self._session = aiohttp.ClientSession()
        try:
            await self.login()
        except Exception:
            await self._session.close()
            self._session = None
            raise
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._session:
            await self._session.close()
            self._session = None

    async def login(self) -> None:
        """Authenticate with Marzban and get an access token."""
        url = f"{self.api_url}/api/admin/token"
        data = {
            "username": self.username,
            "password": self.password,
            "grant_type": "password"
        }
        
        async with self._session.post(url, data=data) as resp:
            if resp.status == 200:
                result = await resp.json()
                self.token = result.get("access_token")
            else:
                text = await resp.text()
                logger.error(f"Marzban login failed: {resp.status} - {text}")
                raise Exception(f"Marzban login failed: {resp.status}")

    def _auth_headers(self) -> Dict[str, str]:
        if not self.token:
            raise ValueError("Not authenticated. Please call login() first.")
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }

    async def get_node_status(self) -> Dict[str, Any]:
        """Fetch general status and active connections from Marzban."""
        url = f"{self.api_url}/api/system"
        async with self._session.get(url, headers=self._auth_headers()) as resp:
            if resp.status == 200:
                return await resp.json()
            else:
                text = await resp.text()
                logger.error(f"Failed to get system status: {resp.status} - {text}")
                return {}

    async def get_nodes(self) -> list[Dict[str, Any]]:
        """Fetch all Marzban nodes with their current status (connected/error/etc)."""
        url = f"{self.api_url}/api/nodes"
        async with self._session.get(url, headers=self._auth_headers()) as resp:
            if resp.status == 200:
                return await resp.json()
            else:
                text = await resp.text()
                logger.error(f"Failed to get nodes: {resp.status} - {text}")
                return []

    async def get_inbounds(self) -> Dict[str, Any]:
        """Fetch available inbound tags grouped by protocol."""
        url = f"{self.api_url}/api/inbounds"
        async with self._session.get(url, headers=self._auth_headers()) as resp:
            if resp.status == 200:
                return await resp.json()

            text = await resp.text()
            logger.error(f"Failed to get inbounds: {resp.status} - {text}")
            return {}

    @staticmethod
    def _extract_inbound_tags(entries: Any) -> list[str]:
        if not isinstance(entries, list):
            return []
        return [
            item["tag"]
            for item in entries
            if isinstance(item, dict) and item.get("tag")
        ]

    @staticmethod
    def _order_inbound_tags(protocol: str, tags: list[str]) -> list[str]:
        if protocol != "vless":
            return tags

        priority = {tag: index for index, tag in enumerate(_PREFERRED_VLESS_TAG_ORDER)}
        return sorted(tags, key=lambda tag: (priority.get(tag, len(priority)), tag))

    async def _resolve_user_template(
        self,
        proxies: Optional[dict],
        inbounds: Optional[dict],
    ) -> tuple[Optional[dict], Optional[dict]]:
        if proxies is not None and inbounds is not None:
            return proxies, inbounds

        available_inbounds = await self.get_inbounds()
        preferred_protocols = ("vless", "trojan", "vmess", "shadowsocks")

        chosen_protocol = None
        chosen_tags: list[str] = []

        if inbounds:
            for protocol in inbounds:
                tags = self._extract_inbound_tags(available_inbounds.get(protocol))
                if tags:
                    chosen_protocol = protocol
                    chosen_tags = self._order_inbound_tags(protocol, tags)
                    break

        if not chosen_protocol:
            for protocol in preferred_protocols:
                tags = self._extract_inbound_tags(available_inbounds.get(protocol))
                if tags:
                    chosen_protocol = protocol
                    chosen_tags = self._order_inbound_tags(protocol, tags)
                    break

        if not chosen_protocol:
            for protocol, entries in available_inbounds.items():
                tags = self._extract_inbound_tags(entries)
                if tags:
                    chosen_protocol = protocol
                    chosen_tags = self._order_inbound_tags(protocol, tags)
                    break

        if not chosen_protocol or not chosen_tags:
            logger.error("No available inbounds found on %s", self.api_url)
            return None, None

        resolved_proxies = proxies or {chosen_protocol: {}}
        resolved_inbounds = inbounds or {chosen_protocol: chosen_tags}
        logger.info(
            "Using Marzban inbounds for %s: protocol=%s tags=%s",
            self.api_url,
            chosen_protocol,
            ", ".join(chosen_tags),
        )
        return resolved_proxies, resolved_inbounds

    async def get_user(self, username: str) -> Optional[Dict[str, Any]]:
        """Get an existing proxy user."""
        url = f"{self.api_url}/api/user/{username}"
        async with self._session.get(url, headers=self._auth_headers()) as resp:
            if resp.status == 200:
                return await resp.json()
            elif resp.status == 404:
                return None
            else:
                text = await resp.text()
                logger.error(f"Failed to get user {username}: {resp.status} - {text}")
                return None

    async def create_user(
        self, 
        username: str, 
        proxies: Optional[dict] = None,
        inbounds: Optional[dict] = None,
        data_limit: int = 0, 
        expire: int = 0, 
        status: str = "active",
        note: str = ""
    ) -> Optional[Dict[str, Any]]:
        """Create a new user on Marzban."""
        url = f"{self.api_url}/api/user"
        proxies, inbounds = await self._resolve_user_template(proxies, inbounds)
        if not proxies or not inbounds:
            return None
        
        payload = {
            "username": username,
            "proxies": proxies,
            "inbounds": inbounds,
            "data_limit": data_limit,
            "expire": expire,
            "status": status,
            "note": note,
            "on_hold_expire_duration": 0,
            "on_hold_timeout": None
        }
        
        async with self._session.post(url, json=payload, headers=self._auth_headers()) as resp:
            if resp.status in (200, 201):
                return await resp.json()
            elif resp.status == 409:
                logger.info(f"User {username} already exists.")
                return await self.get_user(username)
            else:
                text = await resp.text()
                logger.error(f"Failed to create user {username}: {resp.status} - {text}")
                return None

    async def update_user(self, username: str, **kwargs) -> Optional[Dict[str, Any]]:
        """Update an existing user (e.g. extending expire limit, status, data limit)."""
        url = f"{self.api_url}/api/user/{username}"
        
        async with self._session.put(url, json=kwargs, headers=self._auth_headers()) as resp:
            if resp.status == 200:
                return await resp.json()
            else:
                text = await resp.text()
                logger.error(f"Failed to update user {username}: {resp.status} - {text}")
                return None

    async def delete_user(self, username: str) -> bool:
        """Delete an existing user."""
        url = f"{self.api_url}/api/user/{username}"
        async with self._session.delete(url, headers=self._auth_headers()) as resp:
            if resp.status == 200:
                return True
            else:
                text = await resp.text()
                logger.error(f"Failed to delete user {username}: {resp.status} - {text}")
                return False

    async def revoke_user_sub(self, username: str) -> Optional[Dict[str, Any]]:
        """Revokes the user's subscription URL entirely (generates a new sub string)."""
        url = f"{self.api_url}/api/user/{username}/revoke_sub"
        async with self._session.post(url, headers=self._auth_headers()) as resp:
            if resp.status == 200:
                return await resp.json()
            else:
                text = await resp.text()
                logger.error(f"Failed to revoke sub for {username}: {resp.status} - {text}")
                return None

async def create_proxy_account(
    server: Server,
    username: str,
    telegram_username: Optional[str] = None,
    full_name: Optional[str] = None,
    data_limit: int = 0,
    expire: int = 0,
) -> Optional[Dict[str, Any]]:
    """Helper method to instantiate a Marzban user quickly.
    
    Note field will contain Telegram username and real name for readability in dashboard.
    """
    note_parts = []
    if telegram_username:
        note_parts.append(f"@{telegram_username}")
    if full_name:
        note_parts.append(full_name)
    note = " | ".join(note_parts) if note_parts else ""

    try:
        async with MarzbanAPI(server) as api:
            user_data = await api.create_user(
                username=username,
                data_limit=data_limit,
                expire=expire,
                note=note,
            )
            return user_data
    except Exception as e:
        logger.error(f"Error creating proxy account on {server.name}: {e}")
        return None


async def get_subscription_link(server: Server, username: str) -> Optional[str]:
    """Retrieves the full subscription URL for a proxy account."""
    from bot.config import settings

    try:
        async with MarzbanAPI(server) as api:
            user_data = await api.get_user(username)
            if user_data and "subscription_url" in user_data:
                sub_url = user_data["subscription_url"]
                # Ensure absolute URL
                if sub_url.startswith("/"):
                    sub_url = f"{server.api_url}{sub_url}"
                # Replace /sub/ with custom sub path if configured
                if settings.subscription_sub_path:
                    sub_url = sub_url.replace("/sub/", f"/{settings.subscription_sub_path}/")
                # Replace domain with custom base URL if configured
                if settings.subscription_base_url:
                    from urllib.parse import urlparse
                    parsed = urlparse(sub_url)
                    base = settings.subscription_base_url.rstrip("/")
                    sub_url = f"{base}{parsed.path}"
                # Network validation is intentionally skipped here: the bot may run on the
                # same host as the subscription domain, causing self-connect failures (hairpin
                # NAT). Marzban already confirmed the user exists — the URL is trusted.
                return sub_url
            return None
    except Exception as e:
        logger.error(f"Error getting sub link from {server.name}: {e}")
        return None


async def _validate_subscription_link(sub_url: str, *, username: str) -> bool:
    """Ensure the public subscription URL is readable before sending it to a user."""
    if not sub_url.startswith(("http://", "https://")):
        return True

    timeout = ClientTimeout(total=12)
    headers = {
        "Accept": "text/plain,*/*",
        "User-Agent": "vpn-bot-subscription-check/1.0",
    }
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(sub_url, headers=headers, ssl=False) as resp:
                await resp.read()
                if resp.status == 200:
                    return True
                logger.error(
                    "Subscription link validation failed: username=%s status=%s url=%s",
                    username,
                    resp.status,
                    sub_url,
                )
                return False
    except Exception as exc:
        logger.error(
            "Subscription link validation error: username=%s url=%s error=%s",
            username,
            sub_url,
            exc,
        )
        return False
