#!/usr/bin/env python3
"""CLI utility for authorized recovery or reclassification of manual review problem payments.

Usage:
    # Dry run inspection for an ambiguous historical payment:
    python3 -m scripts.recover_manual_payment --payment-id <payment-id> --admin-tg <admin-tg> --reason "Audited evidence" --confirm-no-prior-mutation --provider-status 400 --provider-error "Insufficient balance" --evidence-reference "vhq-audit-2026-..." --dry-run

    # Reclassify ambiguous historical payment to waiting funds queue (auto-recovers when funds arrive):
    python3 -m scripts.recover_manual_payment --payment-id <payment-id> --admin-tg <admin-tg> --reason "Verified insufficient balance refusal" --confirm-no-prior-mutation --provider-status 400 --provider-error "Insufficient balance" --evidence-reference "vhq-audit-2026-..." --reclassify-waiting-funds-only

    # Active immediate recovery under lease (if balance is already topped up):
    python3 -m scripts.recover_manual_payment --payment-id <payment-id> --admin-tg <admin-tg> --reason "Verified VHQ refusal and topped up balance" --confirm-no-prior-mutation --provider-status 400 --provider-error "Insufficient balance" --evidence-reference "vhq-audit-2026-..."
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from aiogram import Bot

from bot.config import settings
from bot.services.manual_payment_recovery import authorize_and_recover_manual_payment

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("recover_manual_payment")


async def main() -> int:
    parser = argparse.ArgumentParser(description="Authorize manual payment recovery with verifiable audit trail")
    parser.add_argument("--payment-id", type=int, required=True, help="Internal payment ID to recover")
    parser.add_argument("--admin-tg", type=int, required=True, help="Admin Telegram ID authorizing the recovery (must be in ADMIN_IDS)")
    parser.add_argument("--reason", type=str, required=True, help="Justification / verified evidence description")
    parser.add_argument(
        "--confirm-no-prior-mutation",
        action="store_true",
        help="Operator confirmation; this flag alone is not evidence and must be accompanied by provider rejection evidence",
    )
    parser.add_argument(
        "--provider-status",
        type=int,
        help="Authoritative supplier HTTP status for the pre-mutation rejection (400 or 402)",
    )
    parser.add_argument(
        "--provider-error",
        type=str,
        help="Exact supplier rejection class/message, such as Insufficient balance",
    )
    parser.add_argument(
        "--evidence-reference",
        type=str,
        help="External audit/log reference proving the original pre-mutation rejection",
    )
    parser.add_argument(
        "--reclassify-waiting-funds-only",
        action="store_true",
        help="Move payment to WAITING_SUPPLIER_FUNDS queue (vhq_balance) without immediate execution, letting scheduler fulfill on top-up",
    )
    parser.add_argument("--dry-run", action="store_true", help="Inspect and verify without mutating state or sending POST requests")
    args = parser.parse_args()

    if not settings.bot_token:
        logger.error("BOT_TOKEN is not configured")
        return 1

    bot = Bot(token=settings.bot_token)
    try:
        logger.info(
            "Executing authorized recovery for payment #%d (admin=%d, reason=%r, dry_run=%s)",
            args.payment_id, args.admin_tg, args.reason, args.dry_run,
        )

        success, code = await authorize_and_recover_manual_payment(
            bot=bot,
            payment_id=args.payment_id,
            admin_telegram_id=args.admin_tg,
            reason=args.reason,
            confirm_no_prior_mutation=args.confirm_no_prior_mutation,
            pre_mutation_evidence={
                "provider_status": args.provider_status,
                "provider_error": args.provider_error,
                "evidence_reference": args.evidence_reference,
            },
            reclassify_to_waiting_funds_only=args.reclassify_waiting_funds_only,
            dry_run=args.dry_run,
        )
        if success:
            logger.info("Operation SUCCEEDED for payment #%d: result=%s", args.payment_id, code)
            return 0
        else:
            logger.error("Operation FAILED for payment #%d: result=%s", args.payment_id, code)
            return 1
    finally:
        await bot.session.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
