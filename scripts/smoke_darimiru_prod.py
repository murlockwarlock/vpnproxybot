"""Read-only Darimiru smoke: python scripts/smoke_darimiru_prod.py --expected-revision REVISION."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


APP_DIR = Path(__file__).resolve().parents[1]
WEBSTORE_URL = "http://127.0.0.1:8900"
WEBSTORE_SERVICE = "webstore-darimiru.service"
BOT_NAME = "darimiru_bot"


def run_command(args: list[str], *, timeout: int = 10) -> tuple[int | None, str]:
    try:
        process = subprocess.Popen(
            args,
            cwd=APP_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
        )
    except OSError:
        return None, ""
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        output, _ = process.communicate()
        return None, output
    return process.returncode, output


def get_status(path: str) -> int | None:
    try:
        with urlopen(Request(f"{WEBSTORE_URL}{path}", method="GET"), timeout=15) as response:
            return response.status
    except HTTPError as exc:
        return exc.code
    except (URLError, TimeoutError, OSError):
        return None


def post_json(path: str, payload: dict) -> tuple[int | None, dict]:
    request = Request(
        f"{WEBSTORE_URL}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode())
    except HTTPError as exc:
        try:
            body = json.loads(exc.read().decode())
        except Exception:
            body = {}
        return exc.code, body
    except (URLError, TimeoutError, OSError, ValueError):
        return None, {}


def list_count(value: object) -> int:
    return len(value) if isinstance(value, list) else 0


def revisions_match(actual: str | None, expected: str) -> bool:
    if not actual or not re.fullmatch(r"[0-9a-fA-F]{7,64}", actual):
        return False
    if not expected or not re.fullmatch(r"[0-9a-fA-F]{7,64}", expected):
        return False
    return actual == expected or actual.startswith(expected) or expected.startswith(actual)


def web_check(web_uuid: str) -> dict:
    status, body = post_json(
        "/api/store/quick-renew/lookup",
        {"query": web_uuid},
    )
    result = {
        "status": status,
        "valid": body.get("valid") is True,
        "is_expired": body.get("is_expired"),
        "is_trial": body.get("is_trial"),
        "renew_tariff_count": list_count(body.get("renew_tariffs")),
        "upgrade_tariff_count": list_count(body.get("upgrade_tariffs")),
    }
    result["ok"] = status == 200 and result["valid"]
    return result


def read_tail(path: Path, max_bytes: int = 100_000) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - max_bytes))
            return handle.read().decode(errors="replace")
    except OSError:
        return ""


def lines_since(text: str, since: float | None) -> list[str]:
    lines = text.splitlines()[-500:]
    if since is None:
        return lines
    selected = []
    current_is_recent = False
    for line in lines:
        match = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
        if match:
            try:
                current_is_recent = (
                    datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
                    >= datetime.fromtimestamp(since).replace(tzinfo=None)
                )
            except ValueError:
                current_is_recent = False
        if current_is_recent:
            selected.append(line)
    return selected


def log_checks(processes: list[dict]) -> dict:
    journal_status, journal_output = run_command(
        [
            "journalctl",
            "-u",
            WEBSTORE_SERVICE,
            "-n",
            "200",
            "--since",
            "10 minutes ago",
            "--no-pager",
            "-q",
        ],
        timeout=15,
    )
    lines = journal_output.splitlines()
    pm2_paths: set[Path] = set()
    process_since = None
    for process in processes:
        if process.get("name") != BOT_NAME:
            continue
        env = process.get("pm2_env") or {}
        uptime = env.get("pm_uptime")
        try:
            process_since = float(uptime) / 1000
        except (TypeError, ValueError):
            pass
        for key in ("pm_out_log_path", "pm_err_log_path"):
            value = process.get(key)
            if value:
                pm2_paths.add(Path(str(value)))
    if not pm2_paths:
        pm2_paths.update((Path.home() / ".pm2" / "logs").glob("darimiru*"))
    for path in pm2_paths:
        lines.extend(lines_since(read_tail(path), process_since))

    import_error_hits = sum(
        1
        for line in lines
        if re.search(r"\b(?:ImportError|ModuleNotFoundError)\b", line)
    )
    repeated: dict[str, int] = {}
    adapt_notification_hits = 0
    for line in lines:
        if not re.search(r"adapt", line, re.IGNORECASE):
            continue
        if not re.search(r"notification|notify", line, re.IGNORECASE):
            continue
        adapt_notification_hits += 1
        normalized = re.sub(r"^\d{4}-\d{2}-\d{2}[^:]*:\s*", "", line)
        normalized = re.sub(r"\b\d+\b", "<n>", normalized)
        repeated[normalized] = repeated.get(normalized, 0) + 1
    max_repeat = max(repeated.values(), default=0)
    return {
        "recent_import_error_hits": import_error_hits,
        "adapt_notification_log_hits": adapt_notification_hits,
        "max_repeated_adapt_notification": max_repeat,
        "ok": journal_status == 0 and import_error_hits == 0 and max_repeat < 3,
    }


async def telegram_check(telegram_uuid: str, buy_handler) -> dict:
    from sqlalchemy import select

    from bot.database import async_session
    from bot.models import AdaptSubscription, Subscription, User

    async with async_session() as session:
        row = (
            await session.execute(
                select(Subscription, AdaptSubscription, User.telegram_id)
                .join(User, User.id == Subscription.user_id)
                .join(AdaptSubscription, AdaptSubscription.subscription_id == Subscription.id)
                .where(AdaptSubscription.adapt_uuid == telegram_uuid)
                .limit(1)
            )
        ).first()
    if not row:
        return {"record_found": False, "ok": False}

    subscription, adapt_subscription, telegram_id = row
    status = str(getattr(subscription.status, "value", subscription.status)).lower()
    is_trial = buy_handler.is_adapt_trial_subscription(
        subscription,
        adapt_sub=adapt_subscription,
    )
    expired = bool(
        subscription.expires_at
        and subscription.expires_at <= datetime.now(timezone.utc).replace(tzinfo=None)
    )
    expired_paid = status == "expired" and expired and not is_trial
    has_active_paid = await buy_handler._has_active_paid_subscription(int(telegram_id))

    class FakeMessage:
        def __init__(self) -> None:
            self.reply_markup = None

        async def edit_text(self, *args, **kwargs) -> None:
            self.reply_markup = kwargs.get("reply_markup")

        async def answer(self, *args, **kwargs) -> None:
            return None

    class FakeCallback:
        def __init__(self) -> None:
            self.data = f"purchase_return_{subscription.id}"
            self.from_user = SimpleNamespace(id=int(telegram_id))
            self.message = FakeMessage()

        async def answer(self, *args, **kwargs) -> None:
            return None

    callback = FakeCallback()
    await buy_handler.return_to_purchase_subscription(callback)
    buttons = [
        button
        for row_buttons in getattr(callback.message.reply_markup, "inline_keyboard", [])
        for button in row_buttons
    ]
    has_renew = any(str(getattr(button, "callback_data", "")).startswith("purchase_renew_") for button in buttons)
    has_upgrade = any(str(getattr(button, "callback_data", "")).startswith("purchase_upgrade_") for button in buttons)
    has_new = any(str(getattr(button, "callback_data", "")).startswith("buy_new_") for button in buttons)
    return {
        "record_found": True,
        "status": status,
        "expired": expired,
        "is_trial": is_trial,
        "expired_paid": expired_paid,
        "has_renew": has_renew,
        "has_upgrade": has_upgrade,
        "has_new": has_new,
        "has_active_paid": has_active_paid,
        "ok": bool(callback.message.reply_markup)
        and (not has_new or has_active_paid)
        and (not expired_paid or (has_renew and has_upgrade)),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--web-uuid")
    parser.add_argument("--telegram-uuid")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    os.chdir(APP_DIR)
    sys.path.insert(0, str(APP_DIR))
    result = {}
    try:
        revision = (APP_DIR / "REVISION").read_text().strip()
    except OSError:
        revision = None
    result["revision"] = revision
    result["revision_ok"] = revisions_match(revision, args.expected_revision.strip())

    pm2_status, pm2_output = run_command(["pm2", "jlist"])
    processes = []
    if pm2_status == 0:
        try:
            parsed_processes = json.loads(pm2_output)
            if isinstance(parsed_processes, list):
                processes = parsed_processes
        except (TypeError, ValueError):
            processes = []
    result["bot_online"] = any(
        process.get("name") == BOT_NAME
        and (process.get("pm2_env") or {}).get("status") == "online"
        for process in processes
    )

    service_status, service_output = run_command(["systemctl", "is-active", WEBSTORE_SERVICE])
    result["webstore_active"] = service_status == 0 and service_output.strip() == "active"
    result["root_status"] = get_status("/")
    result["root_ok"] = result["root_status"] == 200

    try:
        from bot.handlers import buy as buy_handler
        import webstore.routes
        imports_ok = True
    except Exception as exc:
        buy_handler = None
        imports_ok = False
        result["import_error_type"] = type(exc).__name__
    result["imports_ok"] = imports_ok

    result["logs"] = log_checks(processes)

    if args.web_uuid:
        result["web_quick_renew"] = web_check(args.web_uuid)
    if args.telegram_uuid:
        if buy_handler is None:
            result["telegram"] = {"ok": False, "error_type": "ImportError"}
        else:
            try:
                result["telegram"] = await telegram_check(args.telegram_uuid, buy_handler)
            except Exception as exc:
                result["telegram"] = {"ok": False, "error_type": type(exc).__name__}

    checks = [
        result["revision_ok"],
        result["bot_online"],
        result["webstore_active"],
        result["root_ok"],
        result["imports_ok"],
        result["logs"]["ok"],
    ]
    if args.web_uuid:
        checks.append(result["web_quick_renew"]["ok"])
    if args.telegram_uuid:
        checks.append(result["telegram"]["ok"])
    result["ok"] = all(checks)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
