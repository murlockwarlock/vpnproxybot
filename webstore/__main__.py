"""Entry point for the web store service."""

import logging
import os
import threading

from aiohttp import web

from webstore.alerts import notify_webstore_error
from webstore.database import init_db
from webstore.config import settings
from webstore.routes import close_support_websockets, setup_routes
from webstore.scheduler import start_scheduler, stop_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("webstore")
_shutdown_watchdog: threading.Timer | None = None


def _force_bounded_shutdown() -> None:
    logger.warning("aiohttp cleanup reached the bounded timeout; exiting process")
    os._exit(0)


def _arm_shutdown_watchdog(timeout_seconds: float = 5.0) -> None:
    """Ensure aiohttp cannot hang forever while cancelling internal tasks."""
    global _shutdown_watchdog
    if _shutdown_watchdog is not None:
        return
    _shutdown_watchdog = threading.Timer(timeout_seconds, _force_bounded_shutdown)
    _shutdown_watchdog.daemon = True
    _shutdown_watchdog.start()


async def on_startup(app: web.Application) -> None:
    try:
        await init_db()
    except Exception as exc:
        logger.exception("Webstore startup failed")
        await notify_webstore_error("Webstore startup failed", exc=exc)
        raise
    start_scheduler()
    logger.info("Database initialized")
    logger.info(
        "YooKassa: %s",
        "enabled" if settings.yookassa_enabled else "DISABLED (no credentials)",
    )


async def on_shutdown(app: web.Application) -> None:
    """Stop background work before waiting for HTTP/WebSocket clients."""
    stop_scheduler()
    closed_sockets = await close_support_websockets()
    logger.info("Closed %d support websocket(s) for shutdown", closed_sockets)
    _arm_shutdown_watchdog()


def main() -> None:
    @web.middleware
    async def error_alert_middleware(request: web.Request, handler):
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            if exc.status >= 500:
                await notify_webstore_error(
                    "Webstore HTTP error",
                    details=[
                        f"Method: {request.method}",
                        f"Path: {request.path_qs}",
                        f"Status: {exc.status}",
                        f"Reason: {exc.reason}",
                    ],
                    exc=exc,
                )
            raise
        except Exception as exc:
            logger.exception("Unhandled webstore request error: %s %s", request.method, request.path_qs)
            await notify_webstore_error(
                "Webstore unhandled request error",
                details=[
                    f"Method: {request.method}",
                    f"Path: {request.path_qs}",
                    f"Remote: {request.remote}",
                ],
                exc=exc,
            )
            raise

        if response.status >= 500:
            await notify_webstore_error(
                "Webstore 5xx response",
                details=[
                    f"Method: {request.method}",
                    f"Path: {request.path_qs}",
                    f"Status: {response.status}",
                ],
            )
        return response

    app = web.Application(middlewares=[error_alert_middleware])
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    setup_routes(app)

    logger.info("Starting web store on %s:%s", settings.host, settings.port)
    # The production unit has a short stop timeout.  Finish aiohttp cleanup
    # before systemd resorts to SIGKILL, especially while SQLite is in use.
    web.run_app(
        app,
        host=settings.host,
        port=settings.port,
        print=None,
        shutdown_timeout=1.0,
    )


if __name__ == "__main__":
    main()
