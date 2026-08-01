"""VPN key management - SSH-based generation + mock mode for dev."""

from __future__ import annotations

import logging
from bot.config import settings
from bot.models import Server
from bot.services.proxy_manager import MarzbanAPI, create_proxy_account, get_subscription_link

logger = logging.getLogger(__name__)

async def generate_key(server: Server, client_name: str, expire: int = 0) -> str:
    """
    Generates a Marzban user payload and returns the subscription link.
    """
    if settings.mock_mode:
        logger.info(f"[MOCK] Generated key for client: {client_name}")
        return f"vpn://mock_sub_link_for_{client_name}"

    try:
        user_data = await create_proxy_account(server, client_name, expire=expire)
        if user_data:
            link = await get_subscription_link(server, client_name)
            return link or f"Error retrieving link for {client_name}"
        return f"Error creating user {client_name}"
    except Exception as e:
        logger.error(f"Failed to generate key on {server.name}: {e}")
        return "Error creating proxy link"


async def revoke_key(server: Server, client_name: str) -> bool:
    """Revoke a VPN peer on the remote server."""
    if settings.mock_mode:
        logger.info(f"[MOCK] Revoked key for client: {client_name}")
        return True

    try:
         async with MarzbanAPI(server) as api:
             return await api.delete_user(client_name)
    except Exception as e:
        logger.error(f"Failed to revoke key on {server.name}: {e}")
        return False


async def disable_key(server: Server, client_name: str) -> bool:
    """Disable an existing Marzban user without rotating the subscription link."""
    if settings.mock_mode:
        logger.info(f"[MOCK] Disabled key for client: {client_name}")
        return True

    try:
        async with MarzbanAPI(server) as api:
            user_data = await api.get_user(client_name)
            if not user_data:
                return False
            return bool(await api.update_user(client_name, status="disabled"))
    except Exception as e:
        logger.error(f"Failed to disable key on {server.name}: {e}")
        return False


async def get_key_activity(server: Server, client_name: str) -> dict:
    """Return the available Marzban activity metadata for a subscription key."""
    if settings.mock_mode:
        return {}

    try:
        async with MarzbanAPI(server) as api:
            return await api.get_user(client_name) or {}
    except Exception as e:
        logger.error("Failed to get key activity on %s: %s", server.name, e)
        return {}


async def get_server_status(server: Server) -> dict:
    """Get server status (peer count, uptime, etc.)."""
    if settings.mock_mode:
        return {
            "online": True,
            "peers": 12,
            "uptime": "14 days",
            "load": "0.42",
        }

    try:
        async with MarzbanAPI(server) as api:
             status_data = await api.get_node_status()
             
             users_active = status_data.get("users_active", 0)
             uptime = status_data.get("uptime", "Unknown")
             if isinstance(uptime, int):
                 days = uptime // 86400
                 uptime = f"{days} days"
             
             load = status_data.get("cpu_usage", 0.0)
             
             return {
                 "online": True,
                 "peers": users_active,
                 "uptime": uptime,
                 "load": f"{load}%",
             }
    except Exception as e:
        logger.error(f"Failed to get status from {server.name}: {e}")
        return {"online": False, "peers": 0, "uptime": "N/A", "load": "N/A"}
