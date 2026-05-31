"""Tests for support chat REST and WebSocket endpoints."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiohttp import web
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from webstore import routes as webstore_routes
from webstore.models import Base, SupportAgentSession, SupportMessage, SupportTicket

pytestmark = pytest.mark.asyncio


# ── Helpers ────────────────────────────────────────────────────────────────


class _Request(SimpleNamespace):
    async def json(self):
        return self._json_body


def _make_request(body=None, *, cookies=None, path_args=None, query=None, headers=None):
    req = _Request()
    req._json_body = body or {}
    req.cookies = cookies or {}
    req.rel_url = SimpleNamespace(query=query or {})
    req.match_info = path_args or {}
    req.headers = headers or {}
    return req


def _tg_login_data(telegram_id: int, bot_token: str = "TEST_BOT_TOKEN") -> dict:
    auth_date = int(time.time())
    data = {"id": telegram_id, "first_name": "Agent", "auth_date": auth_date}
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret_key = hashlib.sha256(bot_token.encode()).digest()
    data["hash"] = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return data


async def _call(coro):
    """Call handler, converting aiohttp HTTP exceptions to their response equivalent."""
    try:
        return await coro
    except web.HTTPException as exc:
        return exc


# ── Fixtures ───────────────────────────────────────────────────────────────


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


@pytest_asyncio.fixture
async def patched(monkeypatch, db_session_factory):
    monkeypatch.setattr(webstore_routes, "async_session", db_session_factory)
    monkeypatch.setattr(webstore_routes.settings, "site_name", "ДариМир")
    monkeypatch.setattr(webstore_routes.settings, "telegram_bot_name", "darimiru_bot")
    monkeypatch.setattr(webstore_routes.settings, "support_agent_ids", [111222333])
    monkeypatch.setattr(webstore_routes.settings, "admin_bot_token", "TEST_BOT_TOKEN")
    monkeypatch.setattr(webstore_routes, "_notify_support_agents", AsyncMock())
    monkeypatch.setattr(webstore_routes, "_notify_support_agents_message", AsyncMock())
    return db_session_factory


# ── Agent login helper ─────────────────────────────────────────────────────


async def _login_agent(patched) -> str:
    tg_data = _tg_login_data(111222333)
    req = _make_request(tg_data)
    return json.loads((await webstore_routes.handle_support_admin_login(req)).text)["token"]


# ── Ticket creation ────────────────────────────────────────────────────────


async def test_create_ticket_basic(patched):
    req = _make_request({"message": "Ключ не работает", "contact": "88001234567"})
    resp = await webstore_routes.handle_support_new_ticket(req)
    data = json.loads(resp.text)
    assert data["token"]
    assert data["ticket_id"]


async def test_create_ticket_empty_message(patched):
    req = _make_request({"message": "   "})
    resp = await webstore_routes.handle_support_new_ticket(req)
    assert resp.status == 400
    assert json.loads(resp.text)["error"] == "message_required"


async def test_create_ticket_missing_message(patched):
    req = _make_request({})
    resp = await webstore_routes.handle_support_new_ticket(req)
    assert resp.status == 400


async def test_create_ticket_stores_first_message(patched, db_session_factory):
    msg_text = "Помогите пожалуйста"
    req = _make_request({"message": msg_text})
    resp = await webstore_routes.handle_support_new_ticket(req)
    token = json.loads(resp.text)["token"]
    await asyncio.sleep(0)

    async with db_session_factory() as session:
        ticket = (await session.execute(
            select(SupportTicket).where(SupportTicket.token == token)
        )).scalar_one()
        msgs = (await session.execute(
            select(SupportMessage).where(SupportMessage.ticket_id == ticket.id)
        )).scalars().all()

    assert ticket.status == "open"
    assert len(msgs) == 1
    assert msgs[0].text == msg_text
    assert msgs[0].sender == "client"


async def test_create_ticket_notifies_agents(patched):
    req = _make_request({"message": "тест уведомления"})
    await webstore_routes.handle_support_new_ticket(req)
    await asyncio.sleep(0)
    webstore_routes._notify_support_agents.assert_awaited_once()


# ── Ticket info ────────────────────────────────────────────────────────────


async def test_ticket_info_with_messages(patched):
    req = _make_request({"message": "Первый вопрос"})
    token = json.loads((await webstore_routes.handle_support_new_ticket(req)).text)["token"]

    info_resp = await webstore_routes.handle_support_ticket_info(_make_request(path_args={"token": token}))
    assert info_resp.status == 200
    data = json.loads(info_resp.text)
    assert data["status"] == "open"
    assert len(data["messages"]) == 1
    assert data["messages"][0]["sender"] == "client"


async def test_ticket_info_unknown_token(patched):
    resp = await _call(webstore_routes.handle_support_ticket_info(
        _make_request(path_args={"token": "nonexistent_token_xyz"})
    ))
    assert resp.status == 404


# ── Client sends message ───────────────────────────────────────────────────


async def test_client_send_message(patched, db_session_factory):
    token = json.loads((await webstore_routes.handle_support_new_ticket(
        _make_request({"message": "Привет"})
    )).text)["token"]

    resp = await webstore_routes.handle_support_send_message(
        _make_request({"text": "Ещё вопрос"}, path_args={"token": token})
    )
    assert resp.status == 200

    async with db_session_factory() as session:
        ticket = (await session.execute(
            select(SupportTicket).where(SupportTicket.token == token)
        )).scalar_one()
        msgs = (await session.execute(
            select(SupportMessage).where(SupportMessage.ticket_id == ticket.id).order_by(SupportMessage.id)
        )).scalars().all()

    assert len(msgs) == 2
    assert msgs[1].text == "Ещё вопрос"
    assert msgs[1].sender == "client"


async def test_client_send_empty_message(patched):
    token = json.loads((await webstore_routes.handle_support_new_ticket(
        _make_request({"message": "Вопрос"})
    )).text)["token"]
    resp = await webstore_routes.handle_support_send_message(
        _make_request({"text": "  "}, path_args={"token": token})
    )
    assert resp.status == 400


async def test_client_send_to_resolved_ticket(patched, db_session_factory):
    token = json.loads((await webstore_routes.handle_support_new_ticket(
        _make_request({"message": "Вопрос"})
    )).text)["token"]

    async with db_session_factory() as session:
        ticket = (await session.execute(
            select(SupportTicket).where(SupportTicket.token == token)
        )).scalar_one()
        ticket.status = "resolved"
        ticket.resolved_at = datetime.utcnow()
        await session.commit()

    resp = await webstore_routes.handle_support_send_message(
        _make_request({"text": "поздно"}, path_args={"token": token})
    )
    assert resp.status == 400
    assert json.loads(resp.text)["error"] == "ticket_closed"


# ── Agent login ────────────────────────────────────────────────────────────


async def test_agent_login_valid(patched):
    resp = await webstore_routes.handle_support_admin_login(_make_request(_tg_login_data(111222333)))
    assert resp.status == 200
    data = json.loads(resp.text)
    assert data["ok"] is True
    assert data["token"]


async def test_agent_login_unauthorized_id(patched):
    resp = await _call(webstore_routes.handle_support_admin_login(_make_request(_tg_login_data(999888777))))
    assert resp.status == 403


async def test_agent_login_invalid_hash(patched):
    tg_data = _tg_login_data(111222333)
    tg_data["hash"] = "bad" * 21
    resp = await _call(webstore_routes.handle_support_admin_login(_make_request(tg_data)))
    assert resp.status == 403


async def test_agent_login_creates_session(patched, db_session_factory):
    resp = await webstore_routes.handle_support_admin_login(_make_request(_tg_login_data(111222333)))
    token = json.loads(resp.text)["token"]

    async with db_session_factory() as session:
        agent_session = (await session.execute(
            select(SupportAgentSession).where(SupportAgentSession.token == token)
        )).scalar_one()
    assert agent_session.telegram_id == 111222333


async def test_password_login(patched, monkeypatch, db_session_factory):
    monkeypatch.setattr(webstore_routes.settings, "support_admin_password", "testpass")

    resp = await webstore_routes.handle_support_admin_login(_make_request({"password": "testpass"}))
    assert resp.status == 200
    data = json.loads(resp.text)
    assert data["ok"] is True
    assert data["token"]

    async with db_session_factory() as session:
        agent_session = (await session.execute(
            select(SupportAgentSession).where(SupportAgentSession.token == data["token"])
        )).scalar_one()
    assert agent_session.telegram_id == 0


# ── Agent me ───────────────────────────────────────────────────────────────


async def test_agent_me_valid_token(patched):
    agent_token = await _login_agent(patched)
    resp = await _call(webstore_routes.handle_support_admin_me(
        _make_request(headers={"X-Support-Agent-Token": agent_token})
    ))
    assert resp.status == 200
    assert json.loads(resp.text)["telegram_id"] == 111222333


async def test_agent_me_invalid_token(patched):
    resp = await _call(webstore_routes.handle_support_admin_me(
        _make_request(headers={"X-Support-Agent-Token": "invalid_xyz"})
    ))
    assert resp.status == 401


# ── Agent ticket list ──────────────────────────────────────────────────────


async def test_agent_tickets_list(patched):
    for msg in ["Вопрос 1", "Вопрос 2"]:
        await webstore_routes.handle_support_new_ticket(_make_request({"message": msg}))

    agent_token = await _login_agent(patched)
    resp = await _call(webstore_routes.handle_support_admin_tickets(
        _make_request(headers={"X-Support-Agent-Token": agent_token}, query={"status": "open"})
    ))
    assert len(json.loads(resp.text)) == 2


async def test_agent_tickets_list_resolved_filter(patched, db_session_factory):
    await webstore_routes.handle_support_new_ticket(_make_request({"message": "open ticket"}))
    token2 = json.loads((await webstore_routes.handle_support_new_ticket(
        _make_request({"message": "resolved ticket"})
    )).text)["token"]

    async with db_session_factory() as session:
        t = (await session.execute(
            select(SupportTicket).where(SupportTicket.token == token2)
        )).scalar_one()
        t.status = "resolved"
        t.resolved_at = datetime.utcnow()
        await session.commit()

    agent_token = await _login_agent(patched)

    open_data = json.loads((await _call(webstore_routes.handle_support_admin_tickets(
        _make_request(headers={"X-Support-Agent-Token": agent_token}, query={"status": "open"})
    ))).text)
    assert len(open_data) == 1

    resolved_data = json.loads((await _call(webstore_routes.handle_support_admin_tickets(
        _make_request(headers={"X-Support-Agent-Token": agent_token}, query={"status": "resolved"})
    ))).text)
    assert len(resolved_data) == 1


# ── Agent messages ─────────────────────────────────────────────────────────


async def test_agent_ticket_messages(patched):
    ticket_id = json.loads((await webstore_routes.handle_support_new_ticket(
        _make_request({"message": "помогите"})
    )).text)["ticket_id"]

    agent_token = await _login_agent(patched)
    resp = await _call(webstore_routes.handle_support_admin_ticket_messages(
        _make_request(headers={"X-Support-Agent-Token": agent_token}, path_args={"ticket_id": str(ticket_id)})
    ))
    data = json.loads(resp.text)
    msgs = data["messages"]
    assert len(msgs) == 1
    assert msgs[0]["sender"] == "client"


async def test_admin_sees_client_messages(patched):
    created = json.loads((await webstore_routes.handle_support_new_ticket(
        _make_request({"message": "Первое сообщение"})
    )).text)
    token = created["token"]
    ticket_id = created["ticket_id"]

    await webstore_routes.handle_support_send_message(
        _make_request({"text": "Второе сообщение"}, path_args={"token": token})
    )

    agent_token = await _login_agent(patched)
    resp = await _call(webstore_routes.handle_support_admin_ticket_messages(
        _make_request(headers={"X-Support-Agent-Token": agent_token}, path_args={"ticket_id": str(ticket_id)})
    ))
    assert resp.status == 200
    data = json.loads(resp.text)
    assert [msg["text"] for msg in data["messages"]] == ["Первое сообщение", "Второе сообщение"]
    assert all(msg["sender"] == "client" for msg in data["messages"])


async def test_agent_send_reply(patched, db_session_factory):
    ticket_id = json.loads((await webstore_routes.handle_support_new_ticket(
        _make_request({"message": "вопрос"})
    )).text)["ticket_id"]

    agent_token = await _login_agent(patched)
    resp = await _call(webstore_routes.handle_support_admin_send_message(
        _make_request(
            {"text": "Ваш ключ активирован"},
            headers={"X-Support-Agent-Token": agent_token},
            path_args={"ticket_id": str(ticket_id)},
        )
    ))
    assert resp.status == 200

    async with db_session_factory() as session:
        msgs = (await session.execute(
            select(SupportMessage).where(SupportMessage.ticket_id == ticket_id).order_by(SupportMessage.id)
        )).scalars().all()
    assert len(msgs) == 2
    assert msgs[1].sender == "agent"
    assert msgs[1].text == "Ваш ключ активирован"
    assert msgs[1].agent_telegram_id == 111222333


async def test_agent_send_reply_empty(patched):
    ticket_id = json.loads((await webstore_routes.handle_support_new_ticket(
        _make_request({"message": "вопрос"})
    )).text)["ticket_id"]

    agent_token = await _login_agent(patched)
    resp = await _call(webstore_routes.handle_support_admin_send_message(
        _make_request({"text": "  "}, headers={"X-Support-Agent-Token": agent_token}, path_args={"ticket_id": str(ticket_id)})
    ))
    assert resp.status == 400


# ── Resolve ticket ─────────────────────────────────────────────────────────


async def test_resolve_ticket(patched, db_session_factory):
    ticket_id = json.loads((await webstore_routes.handle_support_new_ticket(
        _make_request({"message": "проблема?"})
    )).text)["ticket_id"]

    agent_token = await _login_agent(patched)
    resp = await _call(webstore_routes.handle_support_admin_resolve(
        _make_request(headers={"X-Support-Agent-Token": agent_token}, path_args={"ticket_id": str(ticket_id)})
    ))
    assert resp.status == 200

    async with db_session_factory() as session:
        ticket = (await session.execute(
            select(SupportTicket).where(SupportTicket.id == ticket_id)
        )).scalar_one()
    assert ticket.status == "resolved"
    assert ticket.resolved_at is not None


async def test_resolve_already_resolved(patched):
    ticket_id = json.loads((await webstore_routes.handle_support_new_ticket(
        _make_request({"message": "вопрос"})
    )).text)["ticket_id"]

    agent_token = await _login_agent(patched)
    for _ in range(2):
        resp = await _call(webstore_routes.handle_support_admin_resolve(
            _make_request(headers={"X-Support-Agent-Token": agent_token}, path_args={"ticket_id": str(ticket_id)})
        ))
        assert resp.status == 200


async def test_resolve_nonexistent_ticket(patched):
    agent_token = await _login_agent(patched)
    resp = await _call(webstore_routes.handle_support_admin_resolve(
        _make_request(headers={"X-Support-Agent-Token": agent_token}, path_args={"ticket_id": "99999"})
    ))
    assert resp.status == 404


# ── Full flow ──────────────────────────────────────────────────────────────


async def test_full_support_flow(patched, db_session_factory):
    """Client creates ticket → sends more messages → agent replies → resolves."""
    create_data = json.loads((await webstore_routes.handle_support_new_ticket(
        _make_request({"message": "Не подключается, что делать?", "contact": "user@example.com"})
    )).text)
    token = create_data["token"]
    ticket_id = create_data["ticket_id"]

    await webstore_routes.handle_support_send_message(
        _make_request({"text": "Приложение — Happ"}, path_args={"token": token})
    )

    agent_token = await _login_agent(patched)

    ticket_data = json.loads((await _call(webstore_routes.handle_support_admin_ticket_messages(
        _make_request(headers={"X-Support-Agent-Token": agent_token}, path_args={"ticket_id": str(ticket_id)})
    ))).text)
    msgs = ticket_data["messages"]
    assert len(msgs) == 2
    assert all(m["sender"] == "client" for m in msgs)

    await _call(webstore_routes.handle_support_admin_send_message(
        _make_request(
            {"text": "Обновите подписку"},
            headers={"X-Support-Agent-Token": agent_token},
            path_args={"ticket_id": str(ticket_id)},
        )
    ))

    resolve_resp = await _call(webstore_routes.handle_support_admin_resolve(
        _make_request(headers={"X-Support-Agent-Token": agent_token}, path_args={"ticket_id": str(ticket_id)})
    ))
    assert resolve_resp.status == 200

    # client can't send after resolve
    late_resp = await webstore_routes.handle_support_send_message(
        _make_request({"text": "ещё вопрос"}, path_args={"token": token})
    )
    assert late_resp.status == 400
    assert json.loads(late_resp.text)["error"] == "ticket_closed"

    async with db_session_factory() as session:
        ticket = (await session.execute(
            select(SupportTicket).where(SupportTicket.id == ticket_id)
        )).scalar_one()
        all_msgs = (await session.execute(
            select(SupportMessage).where(SupportMessage.ticket_id == ticket_id).order_by(SupportMessage.id)
        )).scalars().all()

    assert ticket.status == "resolved"
    assert len(all_msgs) == 3
    assert all_msgs[2].sender == "agent"
    assert all_msgs[2].agent_telegram_id == 111222333
