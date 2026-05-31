from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.handlers import start as start_handler
from bot.models import Base, User

pytestmark = pytest.mark.asyncio


class _State:
    def __init__(self) -> None:
        self.data: dict = {"stale": "value"}
        self.current_state = "old"

    async def clear(self) -> None:
        self.data.clear()
        self.current_state = None


def _make_message(user_id: int, text: str, username: str = "newuser", first_name: str = "New") -> SimpleNamespace:
    bot = SimpleNamespace(
        send_message=AsyncMock(),
        get_me=AsyncMock(return_value=SimpleNamespace(username="testbot")),
    )
    from_user = SimpleNamespace(
        id=user_id,
        username=username,
        first_name=first_name,
        full_name=f"{first_name} Example",
    )
    return SimpleNamespace(
        text=text,
        from_user=from_user,
        bot=bot,
        answer=AsyncMock(),
    )


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


async def test_cmd_start_registers_plain_user_and_sends_default_welcome(monkeypatch, db_session_factory):
    monkeypatch.setattr(start_handler, "async_session", db_session_factory)
    maybe_demo = AsyncMock()
    monkeypatch.setattr(start_handler, "_maybe_create_demo_key", maybe_demo)

    message = _make_message(555001, "/start")
    state = _State()

    await start_handler.cmd_start(message, state)

    async with db_session_factory() as session:
        user = await session.scalar(select(User).where(User.telegram_id == 555001))

    assert user is not None
    assert user.username == "newuser"
    assert user.full_name == "New Example"
    assert state.data == {}
    assert state.current_state is None
    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert "Добро пожаловать" in text
    assert "3 устройства" in text
    maybe_demo.assert_awaited_once_with(message, user.id)
