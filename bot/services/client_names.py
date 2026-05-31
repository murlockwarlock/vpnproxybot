"""Helpers for generating unique Marzban client names per bot instance."""

from __future__ import annotations

from bot.config import settings


def build_client_name(
    telegram_id: int,
    slot: int | None = 1,
    *,
    is_demo: bool = False,
    suffix: str | None = None,
    prefix: str | None = None,
) -> str:
    """Build a Marzban username unique per bot deployment."""
    name_prefix = prefix or settings.bot_client_prefix
    base = f"{name_prefix}_tg{telegram_id}"

    if is_demo:
        return f"{base}_demo"
    if suffix:
        return f"{base}_{suffix}"
    if slot is None:
        raise ValueError("slot must be provided when suffix is not set")
    return f"{base}_{slot}"
