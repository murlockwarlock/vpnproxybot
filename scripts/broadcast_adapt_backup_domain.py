#!/usr/bin/env python3
"""Safe, one-time, resumable broadcast tool for delivering darimiru.xyz backup URLs to active Adapt customers.

Usage:
  # 1. READ-ONLY dry-run (default):
  python scripts/broadcast_adapt_backup_domain.py --dry-run

  # 2. Preview rendered message for a specific Telegram ID:
  python scripts/broadcast_adapt_backup_domain.py --preview-tg-id 6392344281

  # 3. Test send to a single authorized test recipient:
  python scripts/broadcast_adapt_backup_domain.py --test-send 6392344281 --confirm-test

  # 4. Production broadcast execution (requires separate explicit authorization):
  python scripts/broadcast_adapt_backup_domain.py --execute --confirm-production
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("adapt_broadcast")

# Denis's exact approved broadcast text template
APPROVED_TEMPLATE = (
    "Дорогие! В связи с массовыми сбоями в рунете (российском интернете) мы приготовили для вас резервную ссылку, которую вы можете использовать в качестве альтернативной в приложении HAPP/INCY.\n"
    "Воспользуйтесь ей в случае, если у вас не работает/не обновляется основная.\n\n"
    "Ниже - ваша альтернативная ссылка:\n"
    "{personal_backup_urls}\n\n"
    "Если приложение будет писать \"Достигнуто максимальное количество устройств\", то удалите ваше текущее устройство через кнопку в главном меню бота \"Управление устройствами\".\n\n"
    "Если будут вопросы, обращайтесь в поддержку, вам помогут:\n"
    "😀 https://t.me/darimiru_bot\n"
    "📱 https://vk.ru/id1115218916\n"
    "📲 https://max.ru/u/f9LHodD0cOKzI-R5CWzQyIjMEUYHv4pNEAgm3XG06rOqO69vc1Zsh5DZhGU"
)

DEFAULT_CHECKPOINT_FILE = PROJECT_ROOT / "scripts" / "adapt_broadcast_checkpoint.json"
DEFAULT_DB_PATH = PROJECT_ROOT / "darimiru_bot.db"
DEFAULT_WEBSTORE_DB_PATH = PROJECT_ROOT / "webstore.db"

UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


def _extract_uuid_from_text(text: str | None) -> str | None:
    """Extract canonical lowercase UUID from text or URL."""
    if not text:
        return None
    match = UUID_PATTERN.search(str(text))
    return match.group(0).lower() if match else None


def redact_uuid(uuid_str: str) -> str:
    """Redact UUID for safe logging (e.g. bcbccd61-...-5ead9d8c2c55 -> ...-5ead9d8c2c55)."""
    clean = str(uuid_str or "").strip()
    if len(clean) >= 12:
        return f"...{clean[-12:]}"
    return "[REDACTED]"


def redact_url(url_str: str) -> str:
    """Redact subscription URL for safe logging."""
    match = re.search(r"/(?:adapt-sub|sub)/([0-9a-fA-F-]+)", url_str)
    if match:
        full_uuid = match.group(1)
        return url_str.replace(full_uuid, redact_uuid(full_uuid))
    return "[REDACTED_URL]"


def build_customer_backup_url(adapt_uuid: str) -> str:
    """Construct preferred darimiru.xyz URL for an Adapt UUID."""
    clean_uuid = str(adapt_uuid or "").strip()
    try:
        from bot.services.adapt_subscription_proxy import build_adapt_mirror_url
        url = build_adapt_mirror_url(clean_uuid)
        if "darimiru.xyz" in url:
            return url
    except Exception:
        pass
    return f"https://darimiru.xyz/vpnbot/adapt-sub/{clean_uuid}"


def render_message_for_urls(urls: list[str]) -> str:
    """Render Denis's approved message inserting customer's personal backup URL(s)."""
    if len(urls) == 1:
        escaped_url = html.escape(urls[0])
        urls_block = f"<code>{escaped_url}</code>"
    else:
        lines = []
        for i, u in enumerate(urls, start=1):
            escaped_url = html.escape(u)
            lines.append(f"{i}. <code>{escaped_url}</code>")
        urls_block = "\n".join(lines)

    return APPROVED_TEMPLATE.format(personal_backup_urls=urls_block)


class CheckpointManager:
    """Thread-safe and restart-safe checkpoint tracking for broadcast delivery."""

    def __init__(self, filepath: Path):
        self.filepath = filepath
        self.data: dict[str, Any] = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "sent": [],
            "failed": {},
            "ambiguous": {},
            "skipped": [],
        }
        self.load()

    def load(self) -> None:
        if self.filepath.exists():
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
                if not isinstance(self.data, dict) or "sent" not in self.data:
                    raise ValueError("Checkpoint file structure is invalid (missing 'sent' list)")
                self.data.setdefault("ambiguous", {})
                logger.info(
                    "Loaded existing checkpoint with %d sent, %d failed, %d ambiguous, %d skipped",
                    len(self.data.get("sent", [])),
                    len(self.data.get("failed", {})),
                    len(self.data.get("ambiguous", {})),
                    len(self.data.get("skipped", [])),
                )
            except Exception as e:
                logger.error("Failed to load checkpoint file %s: %s", self.filepath, e)
                raise RuntimeError(f"Corrupted or invalid checkpoint file at {self.filepath}: {e}")

    def save(self) -> None:
        self.data["updated_at"] = datetime.now(timezone.utc).isoformat()
        tmp_file = self.filepath.with_suffix(".tmp")
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)
        tmp_file.replace(self.filepath)

    def is_sent(self, telegram_id: int) -> bool:
        return telegram_id in self.data.get("sent", [])

    def is_ambiguous(self, telegram_id: int) -> bool:
        return str(telegram_id) in self.data.get("ambiguous", {})

    def mark_sent(self, telegram_id: int) -> None:
        if telegram_id not in self.data["sent"]:
            self.data["sent"].append(telegram_id)
        if str(telegram_id) in self.data["failed"]:
            del self.data["failed"][str(telegram_id)]
        if str(telegram_id) in self.data.get("ambiguous", {}):
            del self.data["ambiguous"][str(telegram_id)]
        self.save()

    def mark_failed(self, telegram_id: int, reason: str) -> None:
        self.data["failed"][str(telegram_id)] = reason
        self.save()

    def mark_ambiguous(self, telegram_id: int, reason: str) -> None:
        self.data.setdefault("ambiguous", {})[str(telegram_id)] = reason
        self.save()

    def mark_skipped(self, telegram_id: int) -> None:
        if telegram_id not in self.data["skipped"]:
            self.data["skipped"].append(telegram_id)
        self.save()


def query_active_adapt_recipients(
    bot_db_path: Path,
    webstore_db_path: Path | None = None,
) -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
    """Query production database(s) for genuinely active, unexpired Adapt subscriptions."""
    import sqlite3
    from bot.services.adapt_routing import get_adapt_uuid_from_subscription

    conn = sqlite3.connect(f"file:{bot_db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Query active subscriptions with users and adapt_subscriptions
    query = """
    SELECT 
        u.id as user_id,
        u.telegram_id,
        u.username,
        u.full_name,
        u.is_blocked,
        s.id as subscription_id,
        s.client_name,
        s.vpn_key,
        s.status,
        s.billing_mode,
        s.expires_at,
        a.adapt_uuid,
        a.adapt_plan_uuid
    FROM subscriptions s
    JOIN users u ON u.id = s.user_id
    LEFT JOIN adapt_subscriptions a ON a.subscription_id = s.id
    WHERE s.status = 'ACTIVE' 
      AND datetime(s.expires_at) > datetime('now')
      AND (a.adapt_uuid IS NOT NULL OR s.client_name LIKE 'adapt_%')
    ORDER BY u.id ASC, s.id ASC;
    """
    rows = cursor.execute(query).fetchall()

    # Also query total excluded counts for audit
    expired_demo_cnt = cursor.execute(
        "SELECT COUNT(*) FROM subscriptions WHERE billing_mode = 'demo' AND (status = 'EXPIRED' OR datetime(expires_at) <= datetime('now'));"
    ).fetchone()[0]

    vhq_cnt = cursor.execute(
        "SELECT COUNT(*) FROM subscriptions WHERE (client_name LIKE 'vhq_%' OR vhq_order_id IS NOT NULL) AND status = 'ACTIVE' AND datetime(expires_at) > datetime('now');"
    ).fetchone()[0]

    marzban_cnt = cursor.execute(
        "SELECT COUNT(*) FROM subscriptions WHERE client_name NOT LIKE 'adapt_%' AND client_name NOT LIKE 'vhq_%' AND vhq_order_id IS NULL AND status = 'ACTIVE' AND datetime(expires_at) > datetime('now');"
    ).fetchone()[0]

    conn.close()

    recipients: dict[int, dict[str, Any]] = {}
    stats: dict[str, Any] = {
        "bot_active_adapt_subscriptions": len(rows),
        "bot_distinct_recipients": 0,
        "bot_distinct_adapt_uuids": 0,
        "excluded_expired_demos": expired_demo_cnt,
        "excluded_active_vhq": vhq_cnt,
        "excluded_active_marzban": marzban_cnt,
        "excluded_blocked_users": 0,
        "excluded_missing_uuid": 0,
        "webstore_active_orders": 0,
        "webstore_active_adapt_orders": 0,
        "webstore_distinct_adapt_uuids": 0,
        "webstore_distinct_customers": 0,
        "webstore_linked_orders": 0,
        "webstore_linked_customers": 0,
        "webstore_unlinked_orders": 0,
        "webstore_unlinked_customers": 0,
        "webstore_excluded_vhq": 0,
        "webstore_excluded_marzban": 0,
        "shared_adapt_uuids": 0,
        "bot_only_recipients": 0,
        "webstore_only_linked_recipients": 0,
        "overlap_recipients": 0,
        "total_unique_telegram_recipients": 0,
        "total_distinct_adapt_uuids_all": 0,
        "total_adapt_uuids_covered": 0,
        "unreachable_customers": 0,
    }

    bot_adapt_uuids_set: set[str] = set()

    for row in rows:
        tg_id = int(row["telegram_id"])
        is_blocked = bool(row["is_blocked"])

        if is_blocked:
            stats["excluded_blocked_users"] += 1
            continue

        # Extract Adapt UUID
        adapt_uuid = str(row["adapt_uuid"] or "").strip().lower()
        if not adapt_uuid:
            adapt_uuid = (get_adapt_uuid_from_subscription(row) or "").lower()

        if not adapt_uuid:
            stats["excluded_missing_uuid"] += 1
            logger.warning("Subscription %d missing Adapt UUID for user %d", row["subscription_id"], tg_id)
            continue

        bot_adapt_uuids_set.add(adapt_uuid)

        if tg_id not in recipients:
            recipients[tg_id] = {
                "user_id": int(row["user_id"]),
                "telegram_id": tg_id,
                "username": row["username"],
                "full_name": row["full_name"],
                "subscriptions": [],
                "adapt_uuids": set(),
                "urls": [],
            }

        recipients[tg_id]["subscriptions"].append({
            "source": "bot",
            "id": int(row["subscription_id"]),
            "billing_mode": row["billing_mode"],
            "expires_at": row["expires_at"],
            "adapt_uuid": adapt_uuid,
        })

        if adapt_uuid not in recipients[tg_id]["adapt_uuids"]:
            recipients[tg_id]["adapt_uuids"].add(adapt_uuid)
            recipients[tg_id]["urls"].append(build_customer_backup_url(adapt_uuid))

    stats["bot_distinct_recipients"] = len(recipients)
    stats["bot_distinct_adapt_uuids"] = len(bot_adapt_uuids_set)
    initial_bot_recipients_set = set(recipients.keys())

    # Query WebStore DB if provided and exists
    web_adapt_uuids_set: set[str] = set()
    unlinked_profiles_set: set[str] = set()
    linked_profiles_set: set[str] = set()
    profile_links: dict[str, dict[str, Any]] = {}

    if webstore_db_path and webstore_db_path.exists():
        w_conn = sqlite3.connect(f"file:{webstore_db_path}?mode=ro", uri=True)
        w_conn.row_factory = sqlite3.Row
        w_cursor = w_conn.cursor()

        # Query profile links
        links_rows = w_cursor.execute(
            "SELECT profile_token, telegram_id, contact, telegram_username, telegram_full_name FROM web_profile_links;"
        ).fetchall()
        profile_links = {
            r["profile_token"]: {
                "telegram_id": int(r["telegram_id"]),
                "contact": r["contact"],
                "username": r["telegram_username"],
                "full_name": r["telegram_full_name"],
            }
            for r in links_rows
            if r["telegram_id"]
        }

        # Query active web orders
        orders_query = """
        SELECT id, order_id, status, tariff_key, tariff_label, days, amount_rub,
               marzban_username, vhq_order_id, subscription_url, profile_token,
               contact, email, access_expires_at
        FROM web_orders
        WHERE status IN ('delivered', 'demo') AND datetime(access_expires_at) > datetime('now')
        ORDER BY id ASC;
        """
        web_rows = w_cursor.execute(orders_query).fetchall()
        w_conn.close()

        stats["webstore_active_orders"] = len(web_rows)
        web_profiles_set: set[str] = set()

        for w_row in web_rows:
            c_name = str(w_row["marzban_username"] or "")
            vhq_id = str(w_row["vhq_order_id"] or "")
            sub_url = str(w_row["subscription_url"] or "")
            p_token = str(w_row["profile_token"] or "")
            if p_token:
                web_profiles_set.add(p_token)

            # Check provider
            if vhq_id or c_name.startswith("vhq_") or "vhq" in sub_url:
                stats["webstore_excluded_vhq"] += 1
                continue

            uuid_val = _extract_uuid_from_text(c_name) or _extract_uuid_from_text(sub_url)
            if not uuid_val and not c_name.startswith("adapt_"):
                stats["webstore_excluded_marzban"] += 1
                continue

            if not uuid_val:
                stats["excluded_missing_uuid"] += 1
                continue

            stats["webstore_active_adapt_orders"] += 1
            web_adapt_uuids_set.add(uuid_val)

            # Check link to Telegram
            link = profile_links.get(p_token)
            if link and link.get("telegram_id"):
                tg_id = link["telegram_id"]
                stats["webstore_linked_orders"] += 1
                linked_profiles_set.add(p_token)

                if tg_id in recipients:
                    rec = recipients[tg_id]
                    rec["subscriptions"].append({
                        "source": "webstore",
                        "id": int(w_row["id"]),
                        "order_id": str(w_row["order_id"]),
                        "tariff_key": w_row["tariff_key"],
                        "expires_at": w_row["access_expires_at"],
                        "adapt_uuid": uuid_val,
                    })
                    if uuid_val not in rec["adapt_uuids"]:
                        rec["adapt_uuids"].add(uuid_val)
                        rec["urls"].append(build_customer_backup_url(uuid_val))
                else:
                    recipients[tg_id] = {
                        "user_id": None,
                        "telegram_id": tg_id,
                        "username": link.get("username"),
                        "full_name": link.get("full_name"),
                        "subscriptions": [{
                            "source": "webstore",
                            "id": int(w_row["id"]),
                            "order_id": str(w_row["order_id"]),
                            "tariff_key": w_row["tariff_key"],
                            "expires_at": w_row["access_expires_at"],
                            "adapt_uuid": uuid_val,
                        }],
                        "adapt_uuids": {uuid_val},
                        "urls": [build_customer_backup_url(uuid_val)],
                    }
            else:
                stats["webstore_unlinked_orders"] += 1
                if p_token:
                    unlinked_profiles_set.add(p_token)

        stats["webstore_distinct_adapt_uuids"] = len(web_adapt_uuids_set)
        stats["webstore_distinct_customers"] = len(web_profiles_set)
        stats["webstore_unlinked_customers"] = len(unlinked_profiles_set)
        stats["webstore_linked_customers"] = len(linked_profiles_set)
        stats["unreachable_customers"] = len(unlinked_profiles_set)

    shared_uuids = bot_adapt_uuids_set & web_adapt_uuids_set
    stats["shared_adapt_uuids"] = len(shared_uuids)
    all_uuids = bot_adapt_uuids_set | web_adapt_uuids_set
    stats["total_distinct_adapt_uuids_all"] = len(all_uuids)

    # Breakdown of recipients
    final_recipients_set = set(recipients.keys())
    stats["total_unique_telegram_recipients"] = len(final_recipients_set)
    linked_tg_ids = {profile_links[pt]["telegram_id"] for pt in linked_profiles_set if pt in profile_links}
    stats["overlap_recipients"] = len(initial_bot_recipients_set & linked_tg_ids)
    stats["webstore_only_linked_recipients"] = len(final_recipients_set - initial_bot_recipients_set)
    stats["bot_only_recipients"] = len(initial_bot_recipients_set) - stats["overlap_recipients"]

    covered_uuids = set()
    for r in recipients.values():
        covered_uuids.update(r["adapt_uuids"])
    stats["total_adapt_uuids_covered"] = len(covered_uuids)

    return recipients, stats


async def execute_broadcast(
    recipients: dict[int, dict[str, Any]],
    checkpoint: CheckpointManager,
    *,
    dry_run: bool = True,
    target_tg_id: int | None = None,
    record_checkpoint: bool | None = None,
    bot_token: str | None = None,
) -> dict[str, int]:
    """Execute broadcast with strict rate limiting, checkpointing, and error handling."""
    if record_checkpoint is None:
        # Never record test-sends into the campaign checkpoint
        record_checkpoint = target_tg_id is None

    results = {"total": len(recipients), "sent": 0, "skipped": 0, "failed": 0, "ambiguous": 0}

    if dry_run:
        logger.info("[DRY-RUN] Simulating broadcast for %d eligible recipients...", len(recipients))
        for tg_id, rec in recipients.items():
            if target_tg_id and tg_id != target_tg_id:
                continue
            if checkpoint.is_sent(tg_id) or checkpoint.is_ambiguous(tg_id):
                results["skipped"] += 1
            else:
                results["sent"] += 1
        return results

    if not bot_token:
        from bot.config import settings
        bot_token = settings.bot_token
    if not bot_token:
        raise ValueError("BOT_TOKEN is required to execute real broadcast.")

    from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest, TelegramRetryAfter

    try:
        from bot.telegram_client import create_telegram_bot
        bot = create_telegram_bot(token=bot_token)
    except Exception:
        from aiogram import Bot
        from aiogram.client.session.aiohttp import AiohttpSession
        proxy = os.getenv("TELEGRAM_PROXY") or os.getenv("HTTPS_PROXY")
        session = AiohttpSession(proxy=proxy) if proxy else None
        bot = Bot(token=bot_token, session=session)

    try:
        targets = [recipients[target_tg_id]] if target_tg_id else list(recipients.values())
        logger.info("Starting live broadcast to %d target(s) (record_checkpoint=%s)...", len(targets), record_checkpoint)

        for idx, rec in enumerate(targets, start=1):
            tg_id = rec["telegram_id"]

            if record_checkpoint and checkpoint.is_sent(tg_id):
                logger.info("[%d/%d] Skipping already sent user %d", idx, len(targets), tg_id)
                results["skipped"] += 1
                continue

            if record_checkpoint and checkpoint.is_ambiguous(tg_id):
                logger.warning("[%d/%d] Skipping ambiguous user %d pending manual review", idx, len(targets), tg_id)
                results["skipped"] += 1
                continue

            message_text = render_message_for_urls(rec["urls"])

            try:
                await bot.send_message(
                    chat_id=tg_id,
                    text=message_text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                if record_checkpoint:
                    checkpoint.mark_sent(tg_id)
                results["sent"] += 1
                logger.info(
                    "[%d/%d] Sent backup URL to telegram_id=%d (%s) [urls=%d]",
                    idx,
                    len(targets),
                    tg_id,
                    rec.get("username") or rec.get("full_name"),
                    len(rec["urls"]),
                )
            except TelegramForbiddenError:
                if record_checkpoint:
                    checkpoint.mark_failed(tg_id, "Forbidden: bot was blocked by user")
                results["failed"] += 1
                logger.warning("[%d/%d] User %d blocked the bot", idx, len(targets), tg_id)
            except TelegramBadRequest as e:
                if record_checkpoint:
                    checkpoint.mark_failed(tg_id, f"BadRequest: {e}")
                results["failed"] += 1
                logger.warning("[%d/%d] BadRequest for user %d: %s", idx, len(targets), tg_id, e)
            except TelegramRetryAfter as e:
                logger.warning("Telegram flood limit hit: sleeping %s seconds before retry for %d", e.retry_after, tg_id)
                await asyncio.sleep(e.retry_after + 1)
                try:
                    await bot.send_message(
                        chat_id=tg_id,
                        text=message_text,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                    if record_checkpoint:
                        checkpoint.mark_sent(tg_id)
                    results["sent"] += 1
                except Exception as retry_err:
                    if record_checkpoint:
                        checkpoint.mark_ambiguous(tg_id, f"RetryAfterFollowupError: {retry_err}")
                    results["ambiguous"] += 1
                    logger.error("[%d/%d] Followup failure after RetryAfter for %d: %s (marked for manual review)", idx, len(targets), tg_id, retry_err)
            except (asyncio.TimeoutError, TimeoutError, OSError) as net_err:
                # Network timeout or connection drop: message may or may not have been delivered.
                # DO NOT retry automatically to avoid duplicate delivery!
                if record_checkpoint:
                    checkpoint.mark_ambiguous(tg_id, f"UncertainDeliveryNetworkError: {net_err}")
                results["ambiguous"] += 1
                logger.error(
                    "[%d/%d] UNCERTAIN DELIVERY for user %d due to network error (%s). "
                    "Marked for MANUAL REVIEW; automatic duplicate retry aborted.",
                    idx, len(targets), tg_id, net_err
                )
            except Exception as e:
                if record_checkpoint:
                    checkpoint.mark_failed(tg_id, f"Unexpected: {e}")
                results["failed"] += 1
                logger.error("[%d/%d] Unexpected error sending to %d: %s", idx, len(targets), tg_id, e)

            # Safe rate limit: sleep 0.05s between sends (~20 msgs/sec max)
            await asyncio.sleep(0.05)

    finally:
        await bot.session.close()

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Deliver darimiru.xyz backup URLs to active Adapt customers.")
    parser.add_argument("--bot-db", "--db-path", dest="db_path", type=Path, default=DEFAULT_DB_PATH, help="Path to SQLite darimiru_bot.db")
    parser.add_argument("--webstore-db", type=Path, default=DEFAULT_WEBSTORE_DB_PATH, help="Path to SQLite webstore.db")
    parser.add_argument("--checkpoint-file", type=Path, default=DEFAULT_CHECKPOINT_FILE, help="Path to checkpoint json")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Read-only analysis and preview")
    parser.add_argument("--preview-tg-id", type=int, help="Render message preview for specific Telegram ID")
    parser.add_argument("--test-send", type=int, help="Send message ONLY to this Telegram ID")
    parser.add_argument("--confirm-test", action="store_true", help="Explicit confirmation for --test-send")
    parser.add_argument("--record-checkpoint", action="store_true", help="Record test send in campaign checkpoint to skip during full broadcast")
    parser.add_argument("--execute", action="store_true", help="Execute real production broadcast")
    parser.add_argument("--confirm-production", action="store_true", help="Explicit confirmation for production broadcast")

    args = parser.parse_args()

    # Determine execution mode
    is_live_execute = args.execute and args.confirm_production
    is_test_send = bool(args.test_send and args.confirm_test)

    if args.execute and not args.confirm_production:
        print("ERROR: --execute requires --confirm-production. Refusing to run live broadcast without explicit confirmation.", file=sys.stderr)
        sys.exit(2)

    if args.test_send and not args.confirm_test:
        print("ERROR: --test-send requires --confirm-test.", file=sys.stderr)
        sys.exit(2)

    db_path = args.db_path
    if not db_path.exists():
        print(f"ERROR: Database file not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    webstore_db_path = args.webstore_db if args.webstore_db.exists() else None

    checkpoint = CheckpointManager(args.checkpoint_file)
    recipients, stats = query_active_adapt_recipients(db_path, webstore_db_path)

    print("\n" + "=" * 65)
    print("      ADAPT BACKUP DOMAIN BROADCAST - UNIFIED AUDIT REPORT")
    print("=" * 65)
    print(f"Bot DB:      {db_path}")
    print(f"Webstore DB: {webstore_db_path if webstore_db_path else 'None (not found or not specified)'}")
    print("-" * 65)
    print("Active Accesses Breakdown (Total 104 in system):")
    print(f"  • Telegram Bot: {stats['bot_active_adapt_subscriptions']} Adapt | {stats['excluded_active_vhq']} VHQ | {stats['excluded_active_marzban']} Marzban (Total: {stats['bot_active_adapt_subscriptions'] + stats['excluded_active_vhq'] + stats['excluded_active_marzban']})")
    print(f"  • Webstore:     {stats['webstore_active_adapt_orders']} Adapt | {stats['webstore_excluded_vhq']} VHQ | {stats['webstore_excluded_marzban']} Marzban (Total: {stats['webstore_active_orders']})")
    print("-" * 65)
    print("Adapt Telegram Delivery Scope:")
    print(f"  • Bot-only active Telegram recipients:      {stats['bot_only_recipients']}")
    print(f"  • Webstore-only Telegram-linked recipients: {stats['webstore_only_linked_recipients']}")
    print(f"  • Customers present in BOTH:                {stats['overlap_recipients']}")
    print(f"  • TOTAL UNIQUE TELEGRAM RECIPIENTS:         {len(recipients)}")
    print(f"  • TOTAL ADAPT UUIDs COVERED VIA TELEGRAM:   {stats['total_adapt_uuids_covered']}")
    print("-" * 65)
    print("Unlinked Webstore Adapt Customers (Not in Telegram):")
    print(f"  • Active orders:    {stats['webstore_unlinked_orders']}")
    print(f"  • Unique customers: {stats['webstore_unlinked_customers']} (profile tokens without Telegram link)")
    print(f"  • Total Adapt UUIDs across Bot & Webstore:  {stats['total_distinct_adapt_uuids_all']}")
    print("=" * 65 + "\n")

    # Distribution by sub count
    sub_counts: dict[int, int] = {}
    for r in recipients.values():
        cnt = len(r["urls"])
        sub_counts[cnt] = sub_counts.get(cnt, 0) + 1
    print("Subscription URL counts per Telegram recipient:")
    for cnt, num_users in sorted(sub_counts.items()):
        print(f"  • {cnt} URL(s): {num_users} customer(s)")
    print("=" * 65 + "\n")

    # Preview specific user if requested
    preview_id = args.preview_tg_id or (args.test_send if is_test_send else None)
    if preview_id:
        if preview_id in recipients:
            rec = recipients[preview_id]
            msg = render_message_for_urls(rec["urls"])
            print(f"--- RENDERED MESSAGE PREVIEW FOR TELEGRAM ID {preview_id} ---")
            print(msg)
            print("----------------------------------------------------------------\n")
        else:
            print(f"WARNING: Telegram ID {preview_id} has no eligible active Adapt subscription.\n")

    # Show a sample rendered message with redacted UUID
    if recipients and not preview_id:
        sample_tg = next(iter(recipients))
        sample_rec = recipients[sample_tg]
        redacted_urls = [redact_url(u) for u in sample_rec["urls"]]
        sample_msg = render_message_for_urls(redacted_urls)
        print("--- SAMPLE RENDERED MESSAGE (UUID REDACTED) ---")
        print(sample_msg)
        print("------------------------------------------------\n")

    if is_live_execute:
        print(">>> EXECUTING PRODUCTION BROADCAST <<<")
        results = asyncio.run(
            execute_broadcast(recipients, checkpoint, dry_run=False)
        )
        print(f"\nBroadcast completed: Sent={results['sent']}, Skipped={results['skipped']}, Failed={results['failed']}")
    elif is_test_send:
        print(f">>> EXECUTING TEST SEND TO {args.test_send} <<<")
        results = asyncio.run(
            execute_broadcast(
                recipients,
                checkpoint,
                dry_run=False,
                target_tg_id=args.test_send,
                record_checkpoint=True if args.record_checkpoint else None,
            )
        )
        print(f"\nTest send completed: Sent={results['sent']}, Failed={results['failed']}")
    else:
        print("Mode: READ-ONLY DRY-RUN (No messages were sent).")
        print("To preview a recipient:  python scripts/broadcast_adapt_backup_domain.py --preview-tg-id <tg_id>")
        print("To send test message:    python scripts/broadcast_adapt_backup_domain.py --test-send <tg_id> --confirm-test")
        print("To run production:       python scripts/broadcast_adapt_backup_domain.py --execute --confirm-production")


if __name__ == "__main__":
    main()

