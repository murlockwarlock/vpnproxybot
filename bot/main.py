"""Main entry point - initialize bot, register routers, start polling."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

from bot.config import settings
from bot.database import init_db
from bot.telegram_client import create_telegram_bot


async def main() -> None:
    """Start the bot."""
    # ── Logging ────────────────────────────────────────
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    logger = logging.getLogger(__name__)

    if not settings.bot_token:
        logger.error("BOT_TOKEN is not set! Check your .env file.")
        sys.exit(1)

    logger.info("=" * 50)
    logger.info("🛡  AmneziaVPN Telegram Bot starting...")
    logger.info(f"   Mock mode: {'ON' if settings.mock_mode else 'OFF'}")
    logger.info(f"   Admin IDs: {settings.admin_ids}")
    logger.info(f"   Instance ID: {settings.instance_id}")
    logger.info("=" * 50)

    # ── Database ───────────────────────────────────────
    await init_db()
    logger.info("Database initialized")

    # ── Seed demo server in mock mode ──────────────────
    if settings.mock_mode:
        await _seed_demo_servers()

    # ── Bot + Dispatcher ───────────────────────────────
    bot = create_telegram_bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    from bot.middlewares.error_feedback import ErrorFeedbackMiddleware
    dp.update.outer_middleware(ErrorFeedbackMiddleware())

    # ── Register routers ───────────────────────────────
    from bot.handlers.admin import router as admin_router
    from bot.handlers.admin_ai import router as admin_ai_router
    from bot.handlers.buy import router as buy_router
    from bot.handlers.devices import router as devices_router
    from bot.handlers.helpai import router as helpai_router
    from bot.handlers.mailing import router as mailing_router
    from bot.handlers.payment import router as payment_router
    from bot.handlers.profile import router as profile_router
    from bot.handlers.partner import router as partner_router
    from bot.handlers.referral import router as referral_router
    from bot.handlers.start import router as start_router

    dp.include_routers(
        helpai_router,
        admin_ai_router,
        admin_router,
        mailing_router,
        referral_router,
        partner_router,
        payment_router,
        buy_router,
        profile_router,
        devices_router,
        start_router,  # start last - it has the catch-all /start
    )
    logger.info("Routers registered: helpai, admin, mailing, referral, payment, buy, profile, devices, start")

    # ── Bot commands menu ──────────────────────────────
    await _setup_commands(bot)
    logger.info("Bot commands set")

    runner = None
    try:
        if settings.run_payment_webhook:
            runner = await _start_payment_webhook_server(bot)

        if settings.run_telegram_polling or settings.run_scheduler or settings.run_mailing_worker:
            await _run_single_instance_core(bot, dp)
        else:
            logger.info("All singleton roles are disabled for this instance; waiting idle.")
            await asyncio.Event().wait()
    finally:
        if runner is not None:
            await runner.cleanup()
        await bot.session.close()


async def _setup_commands(bot: Bot) -> None:
    """Set bot command menu: common commands for all, /admin only for admins."""
    user_commands = [
        BotCommand(command="start",  description="Главное меню"),
        BotCommand(command="keys",   description="Мои ключи"),
        BotCommand(command="help",   description="Помощь"),
    ]
    if os.getenv("LEGAL_COMMANDS_ENABLED", "true").lower() not in ("0", "false", "no", "off"):
        user_commands += [
            BotCommand(command="policy", description="Политика"),
            BotCommand(command="agree",  description="Соглашение"),
            BotCommand(command="oferta", description="Оферта"),
        ]
    user_commands.append(
        BotCommand(command="helpai", description="ИИ-помощник"),
    )
    admin_commands = user_commands + [
        BotCommand(command="admin",  description="Панель администратора"),
    ]

    # Default scope - all users
    await bot.set_my_commands(user_commands, scope=BotCommandScopeDefault())

    # Per-admin scope - extends the menu with /admin
    for admin_id in settings.admin_ids:
        try:
            await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception:
            pass  # admin may not have started the bot yet


async def _seed_demo_servers() -> None:
    """Seed demo servers for development/testing in mock mode."""
    from sqlalchemy import select

    from bot.database import async_session
    from bot.models import Server

    logger = logging.getLogger(__name__)

    demo_servers = [
        {
            "name": "NL-1",
            "host": "mock-nl.example.com",
            "location": "Amsterdam",
            "country_emoji": "🇳🇱",
            "max_clients": 50,
        },
        {
            "name": "DE-1",
            "host": "mock-de.example.com",
            "location": "Frankfurt",
            "country_emoji": "🇩🇪",
            "max_clients": 50,
        },
        {
            "name": "US-1",
            "host": "mock-us.example.com",
            "location": "New York",
            "country_emoji": "🇺🇸",
            "max_clients": 50,
        },
        {
            "name": "FI-1",
            "host": "mock-fi.example.com",
            "location": "Helsinki",
            "country_emoji": "🇫🇮",
            "max_clients": 30,
        },
    ]

    async with async_session() as session:
        for srv_data in demo_servers:
            result = await session.execute(
                select(Server).where(Server.name == srv_data["name"])
            )
            if result.scalar_one_or_none() is None:
                session.add(Server(**srv_data))
                logger.info(f"  Seeded demo server: {srv_data['name']}")

        await session.commit()


async def _start_payment_webhook_server(bot: Bot):
    """Start aiohttp server for payment webhooks."""
    from aiohttp import web
    from bot.webhooks import setup_webhooks

    webhook_app = web.Application()
    webhook_app["bot"] = bot
    setup_webhooks(webhook_app, settings.webhook_path_prefix)

    runner = web.AppRunner(webhook_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", settings.app_port)
    await site.start()
    logging.getLogger(__name__).info(
        "Payment webhook server listening on :%s (prefix: %s)",
        settings.app_port,
        settings.webhook_path_prefix,
    )
    return runner


async def _run_single_instance_core(bot: Bot, dp: Dispatcher) -> None:
    """Run bot roles directly for a single-instance deployment."""
    logger = logging.getLogger(__name__)
    mailing_task = None
    mailing_stop = asyncio.Event()

    try:
        if settings.run_scheduler:
            from bot.services.scheduler import setup_scheduler
            setup_scheduler(bot)

        if settings.run_mailing_worker:
            from bot.services.background_worker import process_mailings
            mailing_task = asyncio.create_task(process_mailings(bot, stop_event=mailing_stop))

        if settings.run_telegram_polling:
            logger.info("Single-instance mode: starting Telegram polling.")
            await dp.start_polling(
                bot,
                allowed_updates=dp.resolve_used_update_types(),
                handle_signals=False,
                close_bot_session=False,
            )
        else:
            logger.info("Telegram polling is disabled; waiting idle.")
            await asyncio.Event().wait()
    finally:
        if settings.run_scheduler:
            from bot.services.scheduler import stop_scheduler
            stop_scheduler()

        if mailing_task:
            mailing_stop.set()
            try:
                await asyncio.wait_for(mailing_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                mailing_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
