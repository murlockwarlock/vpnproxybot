from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from webstore.models import Base, WebTelegramAuthCode

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def test_web_telegram_auth_code_allows_missing_telegram_id(session: AsyncSession):
    session.add(
        WebTelegramAuthCode(
            code="auth-code",
            expires_at=datetime.utcnow() + timedelta(minutes=15),
        )
    )
    await session.commit()

    saved = await session.get(WebTelegramAuthCode, 1)
    assert saved is not None
    assert saved.telegram_id is None
