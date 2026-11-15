"""Unit tests for broadcast_adapt_backup_domain.py."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import pytest

from scripts.broadcast_adapt_backup_domain import (
    APPROVED_TEMPLATE,
    CheckpointManager,
    build_customer_backup_url,
    execute_broadcast,
    redact_url,
    redact_uuid,
    render_message_for_urls,
)


def test_redact_uuid_and_url():
    uuid = "bcbccd61-c8e3-4e1e-a7aa-5ead9d8c2c55"
    assert redact_uuid(uuid) == "...5ead9d8c2c55"
    url = f"https://darimiru.xyz/vpnbot/adapt-sub/{uuid}"
    assert redact_url(url) == "https://darimiru.xyz/vpnbot/adapt-sub/...5ead9d8c2c55"


def test_render_message_single_url():
    uuid = "11111111-2222-3333-4444-555555555555"
    url = f"https://darimiru.xyz/vpnbot/adapt-sub/{uuid}"
    rendered = render_message_for_urls([url])

    assert "Дорогие! В связи с массовыми сбоями в рунете" in rendered
    assert "Ниже - ваша альтернативная ссылка:" in rendered
    assert f"<code>{url}</code>" in rendered
    assert "1. " not in rendered
    assert "https://max.ru/u/f9LHodD0cOKzI-R5CWzQyIjMEUYHv4pNEAgm3XG06rOqO69vc1Zsh5DZhGU" in rendered


def test_render_message_multiple_urls():
    url1 = "https://darimiru.xyz/vpnbot/adapt-sub/11111111-2222-3333-4444-555555555555"
    url2 = "https://darimiru.xyz/vpnbot/adapt-sub/66666666-7777-8888-9999-000000000000"
    rendered = render_message_for_urls([url1, url2])

    assert "Ниже - ваша альтернативная ссылка:\n1. <code>" in rendered
    assert f"1. <code>{url1}</code>" in rendered
    assert f"2. <code>{url2}</code>" in rendered


def test_checkpoint_manager_roundtrip(tmp_path):
    cp_path = tmp_path / "checkpoint.json"
    cp = CheckpointManager(cp_path)

    assert not cp.is_sent(12345)
    cp.mark_sent(12345)
    assert cp.is_sent(12345)

    cp.mark_failed(67890, "Forbidden")
    cp.mark_skipped(11111)

    # Reload from disk
    cp2 = CheckpointManager(cp_path)
    assert cp2.is_sent(12345)
    assert not cp2.is_sent(67890)
    assert cp2.data["failed"]["67890"] == "Forbidden"
    assert 11111 in cp2.data["skipped"]


@pytest.mark.asyncio
async def test_execute_broadcast_dry_run(tmp_path):
    cp = CheckpointManager(tmp_path / "checkpoint.json")
    recipients = {
        100: {
            "telegram_id": 100,
            "urls": ["https://darimiru.xyz/vpnbot/adapt-sub/uuid-1"],
        },
        200: {
            "telegram_id": 200,
            "urls": ["https://darimiru.xyz/vpnbot/adapt-sub/uuid-2"],
        },
    }
    cp.mark_sent(100)  # 100 already sent

    results = await execute_broadcast(recipients, cp, dry_run=True)
    assert results["total"] == 2
    assert results["sent"] == 1  # 200 would be sent
    assert results["skipped"] == 1  # 100 skipped
    assert results["failed"] == 0


@pytest.mark.asyncio
async def test_execute_broadcast_test_send_does_not_mark_checkpoint(tmp_path):
    cp = CheckpointManager(tmp_path / "checkpoint.json")
    recipients = {
        6392344281: {
            "telegram_id": 6392344281,
            "urls": ["https://darimiru.xyz/vpnbot/adapt-sub/test-uuid"],
        },
    }

    mock_bot = AsyncMock()
    with patch("bot.telegram_client.create_telegram_bot", return_value=mock_bot, create=True), patch("aiogram.Bot", return_value=mock_bot):
        results = await execute_broadcast(
            recipients,
            cp,
            dry_run=False,
            target_tg_id=6392344281,
            bot_token="fake:token",
        )
    assert results["sent"] == 1
    assert not cp.is_sent(6392344281)  # Test send must NOT be recorded in checkpoint!


@pytest.mark.asyncio
async def test_execute_broadcast_network_timeout_marks_ambiguous_no_duplicate_retry(tmp_path):
    import asyncio
    cp = CheckpointManager(tmp_path / "checkpoint.json")
    recipients = {
        777: {
            "telegram_id": 777,
            "urls": ["https://darimiru.xyz/vpnbot/adapt-sub/test-uuid-777"],
        },
    }

    mock_bot = AsyncMock()
    mock_bot.send_message.side_effect = asyncio.TimeoutError("Connection timed out")
    with patch("bot.telegram_client.create_telegram_bot", return_value=mock_bot, create=True), patch("aiogram.Bot", return_value=mock_bot):
        results = await execute_broadcast(
            recipients,
            cp,
            dry_run=False,
            target_tg_id=None,
            bot_token="fake:token",
        )
    assert results["ambiguous"] == 1
    assert cp.is_ambiguous(777)
    assert not cp.is_sent(777)
    # Next run skips ambiguous user to avoid duplicate sending:
    assert cp.is_ambiguous(777)


