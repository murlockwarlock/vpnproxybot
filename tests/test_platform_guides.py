import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.models import Base, PlatformGuide
from bot.services.guide_service import send_guide

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session_factory(db_engine):
    return async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)


async def test_send_guide_default_fallback(db_session_factory):
    bot = AsyncMock()
    chat_id = 99999
    
    with patch("bot.services.guide_service.async_session", db_session_factory):
        await send_guide(
            bot=bot,
            chat_id=chat_id,
            platform="android",
            guide_text="Fallback default text",
        )
        
    bot.send_message.assert_called_once()
    sent_text = bot.send_message.call_args[0][1]
    assert sent_text == "Fallback default text"


async def test_send_guide_custom_text_and_buttons(db_session_factory):
    async with db_session_factory() as session:
        pg = PlatformGuide(
            platform="android",
            guide_text="Custom custom text HTML",
            buttons_json='[{"text": "Download Incy", "type": "url", "data": "https://example.com/incy"}]'
        )
        session.add(pg)
        await session.commit()

    bot = AsyncMock()
    chat_id = 99999
    
    with patch("bot.services.guide_service.async_session", db_session_factory):
        await send_guide(
            bot=bot,
            chat_id=chat_id,
            platform="android",
            guide_text="Fallback default text",
        )
        
    bot.send_message.assert_called_once()
    sent_text = bot.send_message.call_args[0][1]
    assert sent_text == "Custom custom text HTML"
    
    reply_markup = bot.send_message.call_args[1]["reply_markup"]
    assert reply_markup is not None
    assert len(reply_markup.inline_keyboard) == 1
    assert reply_markup.inline_keyboard[0][0].text == "Download Incy"
    assert reply_markup.inline_keyboard[0][0].url == "https://example.com/incy"


async def test_send_guide_with_media_album(db_session_factory):
    async with db_session_factory() as session:
        pg = PlatformGuide(
            platform="ios",
            guide_text="iOS Guide",
            media_file_id="photo:file1,video:file2",
            media_type="album",
        )
        session.add(pg)
        await session.commit()

    bot = AsyncMock()
    chat_id = 99999
    
    with patch("bot.services.guide_service.async_session", db_session_factory):
        await send_guide(
            bot=bot,
            chat_id=chat_id,
            platform="ios",
            guide_text="Fallback default text",
        )
        
    bot.send_media_group.assert_called_once()
    media_arg = bot.send_media_group.call_args[1]["media"]
    assert len(media_arg) == 2
    assert media_arg[0].media == "file1"
    assert media_arg[1].media == "file2"
    
    bot.send_message.assert_called_once()
    assert bot.send_message.call_args[0][1] == "iOS Guide"
