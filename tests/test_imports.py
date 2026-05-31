import pytest
import importlib
import pkgutil
import sys
import types
import bot.handlers


if "openai" not in sys.modules:
    openai_stub = types.ModuleType("openai")

    class _AsyncOpenAI:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    openai_stub.AsyncOpenAI = _AsyncOpenAI
    sys.modules["openai"] = openai_stub

def test_all_handlers_import_correctly():
    """Test to ensure all handlers can be imported without NameErrors or SyntaxErrors."""
    handler_modules = [name for _, name, _ in pkgutil.iter_modules(bot.handlers.__path__)]
    assert handler_modules, "Expected to find handler modules"
    
    for module_name in handler_modules:
        full_module_name = f"bot.handlers.{module_name}"
        # This will raise an exception if there is any NameError or SyntaxError during module import
        importlib.import_module(full_module_name)

def test_router_configuration():
    """Verify that every handler has a router configured correctly."""
    from aiogram import Router
    handler_modules = [name for _, name, _ in pkgutil.iter_modules(bot.handlers.__path__)]
    for module_name in handler_modules:
        full_module_name = f"bot.handlers.{module_name}"
        module = importlib.import_module(full_module_name)
        assert hasattr(module, "router"), f"Module {module_name} is missing a 'router' attribute"
        assert isinstance(module.router, Router), f"Module {module_name}.router is not a Router"

def test_bot_texts_import():
    """Verify texts file has no SyntaxErrors or missing variables."""
    import bot.utils.texts
    assert bot.utils.texts.SELECT_PAYMENT

def test_bot_database_models():
    """Verify all database models are valid and can be imported."""
    import bot.models
    assert hasattr(bot.models, "User")
    assert hasattr(bot.models, "Tariff")
    assert hasattr(bot.models, "Server")
