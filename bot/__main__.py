"""Convenience __main__ module - allows `python -m bot`."""

from bot.main import main
import asyncio

asyncio.run(main())
