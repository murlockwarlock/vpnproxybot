"""Structured payment event logger — writes to a separate log file."""

import logging
from logging.handlers import RotatingFileHandler
import os
import re
from datetime import datetime, timezone, timedelta

_MSK = timezone(timedelta(hours=3))

_LOGGERS: dict[tuple[str, str], logging.Logger] = {}


def _sanitize_filename_part(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "_", value.strip())
    return cleaned or "default"


def _log_dir() -> str:
    log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "logs")
    os.makedirs(log_dir, exist_ok=True)
    return log_dir


def _get_logger(stream: str, instance: str) -> logging.Logger:
    key = (stream, instance)
    existing = _LOGGERS.get(key)
    if existing is not None:
        return existing

    logger = logging.getLogger(f"{stream}:{instance}")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    handler = RotatingFileHandler(
        os.path.join(_log_dir(), f"{stream}_{instance}.log"),
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    _LOGGERS[key] = logger
    return logger


def _now_msk() -> str:
    return datetime.now(_MSK).strftime("%d.%m.%Y %H:%M:%S")


def _resolve_instance(instance_hint: str | None = None) -> str:
    raw = instance_hint or os.getenv("APP_PORT") or os.getenv("WEBSTORE_PORT") or "default"
    return _sanitize_filename_part(raw)


def _emit(stream: str, event: str, *, instance_hint: str | None = None, **fields: object) -> None:
    logger = _get_logger(_sanitize_filename_part(stream), _resolve_instance(instance_hint))
    parts = [_now_msk(), event]
    for k, v in fields.items():
        parts.append(f"{k}={v}")
    logger.info(" | ".join(parts))


def plog(event: str, _instance_hint: str | None = None, **fields: object) -> None:
    """Log a structured payment event.

    Example:
        plog("ОПЛАТА", provider="Yookassa", user_id=123, amount=95.0)
        → "03.04.2026 14:23:45 | ОПЛАТА | provider=Yookassa | user_id=123 | amount=95.0"
    """
    _emit("payment_events", event, instance_hint=_instance_hint, **fields)


def get_payment_log_tail(n: int = 50) -> list[str]:
    """Return the last *n* lines from the payment-events log file.

    Returns an empty list if the file does not exist yet.
    Uses a deque to avoid reading the entire file into memory.
    """
    import collections
    log_path = os.path.join(_log_dir(), f"payment_events_{_resolve_instance()}.log")
    if not os.path.exists(log_path):
        return []
    with open(log_path, encoding="utf-8") as fh:
        tail = collections.deque(fh, maxlen=n)
    return [line.rstrip("\n") for line in tail]
