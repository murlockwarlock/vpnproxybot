from unittest.mock import AsyncMock, Mock

import pytest

from webstore import __main__ as webstore_main


@pytest.mark.asyncio
async def test_shutdown_stops_scheduler_before_server_cleanup(monkeypatch):
    stop_scheduler = Mock()
    close_websockets = AsyncMock(return_value=2)
    monkeypatch.setattr(webstore_main, "stop_scheduler", stop_scheduler)
    monkeypatch.setattr(webstore_main, "close_support_websockets", close_websockets)

    await webstore_main.on_shutdown(Mock())

    stop_scheduler.assert_called_once_with()
    close_websockets.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_close_support_websockets_closes_every_tracked_connection():
    from webstore import routes

    client = Mock(closed=False)
    client._set_code_close_transport = Mock()
    agent = Mock(closed=False)
    agent._set_code_close_transport = Mock()
    routes._support_ws_pool["ticket"] = {"client": client, "agents": [agent, client]}

    closed_count = await routes.close_support_websockets()

    assert closed_count == 2
    client._set_code_close_transport.assert_called_once()
    agent._set_code_close_transport.assert_called_once()
    assert routes._support_ws_pool == {}
