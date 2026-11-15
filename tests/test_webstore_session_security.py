import hashlib
import json

import pytest
from sqlalchemy import select

from test_webstore_telegram_linking import db_engine, db_session_factory, patched_webstore, _Request
from webstore import routes
from webstore.models import WebAccount, WebSession

pytestmark = pytest.mark.asyncio


async def test_session_is_random_hashed_and_revoked(patched_webstore):
    async with patched_webstore() as session:
        session.add(WebAccount(contact="123456", profile_token="profile-id", password_hash=routes._hash_password("safe-password")))
        await session.commit()
    response = await routes.handle_auth_login(_Request(_json_body={"contact": "123456", "password": "safe-password"}))
    assert response.status == 200
    cookie = response.cookies[routes._AUTH_COOKIE]
    secret = cookie.value
    assert secret != "profile-id" and cookie["httponly"] and cookie["secure"] and cookie["samesite"] == "Lax"
    async with patched_webstore() as session:
        stored = await session.scalar(select(WebSession))
        assert stored.token_hash == hashlib.sha256(secret.encode()).hexdigest()
    assert (await routes.handle_profile_orders(_Request(query={"token": "profile-id"}, cookies={routes._AUTH_COOKIE: "profile-id"}))).status == 403
    assert json.loads((await routes.handle_auth_me(_Request(cookies={routes._AUTH_COOKIE: secret}))).text)["authenticated"]
    await routes.handle_auth_logout(_Request(cookies={routes._AUTH_COOKIE: secret}))
    assert not json.loads((await routes.handle_auth_me(_Request(cookies={routes._AUTH_COOKIE: secret}))).text)["authenticated"]
