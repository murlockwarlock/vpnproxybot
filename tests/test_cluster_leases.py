from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy.exc import IntegrityError

from bot.services.cluster import LeaseManager


class _FakeResult:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _FakeSession:
    def __init__(
        self,
        execute_rowcounts: list[int],
        commit_plan: list[object | None],
        get_result=None,
    ) -> None:
        self.execute_rowcounts = execute_rowcounts
        self.commit_plan = commit_plan
        self.get_result = get_result
        self.added = []
        self.commit_calls = 0
        self.rollback_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _statement):
        if not self.execute_rowcounts:
            raise AssertionError("Unexpected execute call")
        return _FakeResult(self.execute_rowcounts.pop(0))

    def add(self, obj) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commit_calls += 1
        if self.commit_plan:
            action = self.commit_plan.pop(0)
            if isinstance(action, BaseException):
                raise action

    async def rollback(self) -> None:
        self.rollback_calls += 1

    async def get(self, _model, _key):
        return self.get_result


class _FakeSessionFactory:
    def __init__(self, session: _FakeSession) -> None:
        self.session = session

    def __call__(self):
        return self.session


def test_lease_manager_renews_existing_lease():
    async def scenario() -> None:
        session = _FakeSession(execute_rowcounts=[1], commit_plan=[None])
        manager = LeaseManager("instance-a", session_factory=_FakeSessionFactory(session))

        acquired = await manager.acquire_or_renew("core", ttl_seconds=30)

        assert acquired is True
        assert session.commit_calls == 1
        assert session.rollback_calls == 0
        assert session.added == []

    asyncio.run(scenario())


def test_lease_manager_acquires_after_insert_race():
    async def scenario() -> None:
        session = _FakeSession(
            execute_rowcounts=[0, 1],
            commit_plan=[IntegrityError("insert race", None, None), None],
        )
        manager = LeaseManager("instance-a", session_factory=_FakeSessionFactory(session))

        acquired = await manager.acquire_or_renew("core", ttl_seconds=30)

        assert acquired is True
        assert session.commit_calls == 2
        assert session.rollback_calls == 1
        assert len(session.added) == 1

    asyncio.run(scenario())


def test_lease_manager_returns_false_when_other_owner_holds_lock():
    async def scenario() -> None:
        session = _FakeSession(
            execute_rowcounts=[0, 0],
            commit_plan=[IntegrityError("busy", None, None)],
        )
        manager = LeaseManager("instance-b", session_factory=_FakeSessionFactory(session))

        acquired = await manager.acquire_or_renew("core", ttl_seconds=30)

        assert acquired is False
        assert session.commit_calls == 1
        assert session.rollback_calls == 2

    asyncio.run(scenario())


def test_lease_manager_release_commits_delete():
    async def scenario() -> None:
        session = _FakeSession(execute_rowcounts=[1], commit_plan=[None])
        manager = LeaseManager("instance-a", session_factory=_FakeSessionFactory(session))

        await manager.release("core")

        assert session.commit_calls == 1
        assert session.rollback_calls == 0

    asyncio.run(scenario())


def test_lease_manager_is_owned_by_me_respects_owner_and_expiry():
    async def scenario() -> None:
        session = _FakeSession(
            execute_rowcounts=[],
            commit_plan=[],
            get_result=SimpleNamespace(
                owner_id="instance-a",
                lease_until=datetime.utcnow() + timedelta(seconds=30),
            ),
        )
        manager = LeaseManager("instance-a", session_factory=_FakeSessionFactory(session))

        assert await manager.is_owned_by_me("core") is True

        session.get_result = SimpleNamespace(
            owner_id="instance-b",
            lease_until=datetime.utcnow() + timedelta(seconds=30),
        )
        assert await manager.is_owned_by_me("core") is False

        session.get_result = SimpleNamespace(
            owner_id="instance-a",
            lease_until=datetime.utcnow() - timedelta(seconds=30),
        )
        assert await manager.is_owned_by_me("core") is False

    asyncio.run(scenario())
