import pytest
from bot.handlers.mailing import _parse_album_media
from bot.services.background_worker import _send_one
from bot.models import Mailing
from unittest.mock import AsyncMock, MagicMock

def test_parse_album_media():
    media_str = "photo:file1,video:file2,photo:file3"
    media_items = _parse_album_media(media_str, caption="Hello World")
    
    assert len(media_items) == 3
    assert media_items[0].media == "file1"
    assert media_items[0].caption == "Hello World"
    assert media_items[1].media == "file2"
    assert media_items[1].caption is None
    assert media_items[2].media == "file3"
    assert media_items[2].caption is None

@pytest.mark.asyncio
async def test_send_one_album_combined():
    bot = AsyncMock()
    chat_id = 12345
    mailing = Mailing(
        text="Mailing text",
        media_file_id="photo:file1,video:file2",
        media_file_type="album",
        media_position="media_top",
        buttons_json=None,
    )
    
    await _send_one(bot, chat_id, mailing)
    
    bot.send_media_group.assert_called_once()
    media_arg = bot.send_media_group.call_args[1]["media"]
    assert len(media_arg) == 2
    assert media_arg[0].media == "file1"
    assert media_arg[0].caption == "Mailing text"
    bot.send_message.assert_not_called()

@pytest.mark.asyncio
async def test_send_one_album_separate():
    bot = AsyncMock()
    chat_id = 12345
    mailing = Mailing(
        text="Mailing text",
        media_file_id="photo:file1,video:file2",
        media_file_type="album",
        media_position="media_top",
        buttons_json='[{"text": "Btn", "type": "url", "data": "https://example.com"}]',
    )
    
    await _send_one(bot, chat_id, mailing)
    
    bot.send_media_group.assert_called_once()
    media_arg = bot.send_media_group.call_args[1]["media"]
    assert len(media_arg) == 2
    assert media_arg[0].media == "file1"
    assert media_arg[0].caption is None
    
    bot.send_message.assert_called_once()
    assert bot.send_message.call_args[0][1] == "Mailing text"
