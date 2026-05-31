"""Daily SQLite backup creation, retention, and Telegram delivery to admins."""

from __future__ import annotations

import asyncio
import gzip
import logging
import re
import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from aiogram import Bot
from aiogram.types import BufferedInputFile

from bot.config import BASE_DIR, settings

logger = logging.getLogger(__name__)


def _sqlite_db_path() -> Path | None:
    url = settings.database_url
    for prefix in ("sqlite+aiosqlite:///", "sqlite:///"):
        if url.startswith(prefix):
            raw_path = url[len(prefix):]
            path = Path(raw_path)
            if not path.is_absolute():
                path = (BASE_DIR / path).resolve()
            return path
    return None


def _safe_backup_name(source_db: Path, fallback: str) -> str:
    stem = source_db.stem or fallback
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", stem).strip("._")
    return safe or fallback


def _backup_archive_path(backup_dir: Path, source_db: Path, *, fallback: str = "database") -> Path:
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    name = _safe_backup_name(source_db, fallback)
    return backup_dir / f"{name}_{timestamp}.sqlite3.gz"


def _extra_sqlite_db_paths() -> list[Path]:
    raw = str(getattr(settings, "backup_extra_sqlite_dbs", "") or "").strip()
    if not raw:
        return []

    paths: list[Path] = []
    for item in re.split(r"[,;:]", raw):
        value = item.strip()
        if not value:
            continue
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = (BASE_DIR / path).resolve()
        paths.append(path)
    return paths


def _create_sqlite_backup_archive(source_db: Path, target_archive: Path) -> int:
    backup_dir = target_archive.parent
    backup_dir.mkdir(parents=True, exist_ok=True)

    temp_db = backup_dir / f"{target_archive.stem}.tmp.sqlite3"
    temp_archive = backup_dir / f"{target_archive.name}.tmp"

    if temp_db.exists():
        temp_db.unlink()
    if temp_archive.exists():
        temp_archive.unlink()

    src = sqlite3.connect(source_db)
    dst = sqlite3.connect(temp_db)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()

    try:
        with temp_db.open("rb") as src_file, gzip.open(temp_archive, "wb", compresslevel=6) as gz_file:
            shutil.copyfileobj(src_file, gz_file)
        temp_archive.replace(target_archive)
        return target_archive.stat().st_size
    finally:
        if temp_db.exists():
            temp_db.unlink()
        if temp_archive.exists():
            temp_archive.unlink()


def _cleanup_old_backups(backup_dir: Path, retention_days: int) -> list[Path]:
    cutoff = datetime.utcnow() - timedelta(days=retention_days)
    deleted: list[Path] = []

    for path in backup_dir.glob("*.sqlite3.gz"):
        mtime = datetime.utcfromtimestamp(path.stat().st_mtime)
        if mtime < cutoff:
            path.unlink(missing_ok=True)
            deleted.append(path)
    return deleted


async def create_and_send_backup(bot: Bot) -> None:
    """Create compressed SQLite backups, prune old backups, and send them to admins."""
    if not settings.admin_ids:
        logger.info("Skipping backup delivery: ADMIN_IDS is empty.")
        return

    primary_db = _sqlite_db_path()
    if primary_db is None or not primary_db.exists():
        logger.warning("Skipping backup: unsupported or missing database path for %s", settings.database_url)
        return

    source_dbs = [primary_db]
    for extra_db in _extra_sqlite_db_paths():
        if extra_db.exists() and extra_db not in source_dbs:
            source_dbs.append(extra_db)
        else:
            logger.warning("Skipping extra backup DB: missing or duplicate path %s", extra_db)

    backup_dir = Path(settings.backup_dir).expanduser()
    backup_dir.mkdir(parents=True, exist_ok=True)

    created: list[tuple[Path, int]] = []
    try:
        for index, source_db in enumerate(source_dbs):
            target_archive = _backup_archive_path(
                backup_dir,
                source_db,
                fallback="vpn_bot" if index == 0 else f"extra_db_{index}",
            )
            size_bytes = await asyncio.to_thread(_create_sqlite_backup_archive, source_db, target_archive)
            created.append((target_archive, size_bytes))
        deleted = await asyncio.to_thread(_cleanup_old_backups, backup_dir, settings.backup_retention_days)
    except Exception:
        logger.exception("Failed to create backup archive.")
        return

    for target_archive, size_bytes in created:
        file_bytes = await asyncio.to_thread(target_archive.read_bytes)
        size_mb = size_bytes / (1024 * 1024)
        caption = (
            "💾 <b>Ежедневный бэкап БД</b>\n\n"
            f"Файл: <code>{target_archive.name}</code>\n"
            f"Размер: <b>{size_mb:.2f} MB</b>\n"
            f"Хранение: <b>{settings.backup_retention_days} дней</b>\n"
            f"Удалено старых: <b>{len(deleted)}</b>"
        )

        for admin_id in settings.admin_ids:
            try:
                tg_file = BufferedInputFile(file_bytes, filename=target_archive.name)
                await bot.send_document(admin_id, tg_file, caption=caption, parse_mode="HTML")
            except Exception:
                logger.exception("Failed to send backup to admin %s", admin_id)

    logger.info(
        "Backups created: %s, deleted old backups: %s",
        ", ".join(f"{path} ({size / (1024 * 1024):.2f} MB)" for path, size in created),
        len(deleted),
    )
