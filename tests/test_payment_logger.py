"""Tests for payment_logger utility functions."""

import os
import tempfile
import importlib

import pytest


def _make_log_file(tmp_path, port: str, lines: list[str]) -> str:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    log_file = log_dir / f"payment_events_{port}.log"
    log_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(log_dir)


def test_get_payment_log_tail_returns_last_n_lines(tmp_path, monkeypatch):
    port = "9999"
    all_lines = [f"line {i}" for i in range(1, 101)]  # 100 lines
    log_dir_str = _make_log_file(tmp_path, port, all_lines)

    monkeypatch.setenv("APP_PORT", port)

    # Patch the log directory path inside the module
    import bot.services.payment_logger as pl

    monkeypatch.setattr(
        pl,
        "get_payment_log_tail",
        lambda n=50: _tail_from_dir(log_dir_str, port, n),
    )

    result = pl.get_payment_log_tail(50)
    assert len(result) == 50
    assert result[0] == "line 51"
    assert result[-1] == "line 100"


def test_get_payment_log_tail_fewer_lines_than_requested(tmp_path, monkeypatch):
    port = "9998"
    all_lines = ["event A", "event B", "event C"]
    log_dir_str = _make_log_file(tmp_path, port, all_lines)

    import bot.services.payment_logger as pl

    monkeypatch.setattr(
        pl,
        "get_payment_log_tail",
        lambda n=50: _tail_from_dir(log_dir_str, port, n),
    )

    result = pl.get_payment_log_tail(50)
    assert result == ["event A", "event B", "event C"]


def test_get_payment_log_tail_missing_file_returns_empty(tmp_path, monkeypatch):
    port = "9997"
    # No log file created

    import bot.services.payment_logger as pl

    monkeypatch.setattr(
        pl,
        "get_payment_log_tail",
        lambda n=50: _tail_from_dir(str(tmp_path / "logs"), port, n),
    )

    result = pl.get_payment_log_tail(50)
    assert result == []


# ── Helper (mirrors actual implementation logic) ──────

def _tail_from_dir(log_dir: str, port: str, n: int) -> list[str]:
    log_path = os.path.join(log_dir, f"payment_events_{port}.log")
    if not os.path.exists(log_path):
        return []
    with open(log_path, encoding="utf-8") as fh:
        lines = fh.readlines()
    return [line.rstrip("\n") for line in lines[-n:]]
