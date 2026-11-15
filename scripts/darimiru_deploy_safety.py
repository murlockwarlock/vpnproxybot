from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tarfile


class SafetyError(RuntimeError):
    pass


INDEXES = {
    "bot": {
        "uq_balance_topup_web_balance_wbtr": (
            "balance_topups", ["provider_payment_id"],
            "provider = 'web_balance_migration' AND provider_payment_id LIKE 'wbtr_%'",
        ),
        "uq_referral_payment_logs_payment_id": (
            "referral_payment_logs", ["payment_id"],
            "payment_id IS NOT NULL",
        ),
        "uq_referral_earnings_payment_id": (
            "referral_earnings", ["payment_id"],
            "payment_id IS NOT NULL",
        ),
        "uq_partner_earnings_payment_depth": (
            "partner_earnings", ["payment_id", "depth"],
            "payment_id IS NOT NULL",
        ),
        "uq_balance_tx_payment_cashback": (
            "balance_transactions", ["source_type", "source_id"],
            "kind = 'CASHBACK' AND source_type = 'payment' AND source_id IS NOT NULL",
        ),
        "uq_active_vhq_renewal_per_sub_v2": (
            "vhq_subscription_fulfillments", ["subscription_id"],
            "status IN ('pending', 'processing', 'retry_wait', 'uncertain', 'buy_inflight', 'renew_inflight')",
        ),
    },
    "web": {
        "uq_web_balance_transfers_profile_pending": (
            "web_balance_transfers", ["profile_token"], "status = 'pending'",
        ),
        "uq_web_balance_tx_migration_source": (
            "web_balance_transactions", ["source_id"], "kind = 'balance_migration' AND source_id IS NOT NULL",
        ),
    },
}
BOT_FOREIGN_KEY_ALLOWLIST = (
    ("payments", 57, "subscriptions", 0),
    ("proxy_accounts", 132, "subscriptions", 0),
    ("recurring_payment_profiles", 3, "subscriptions", 1),
    ("recurring_payment_profiles", 3, "users", 2),
    ("referral_earnings", 1, "payments", 0),
    ("subscription_notification_logs", 196, "subscriptions", 0),
    ("subscription_notification_logs", 202, "subscriptions", 0),
    ("subscription_notification_logs", 210, "subscriptions", 0),
    ("subscription_notification_logs", 220, "subscriptions", 0),
    ("subscription_notification_logs", 230, "subscriptions", 0),
    ("subscription_notification_logs", 231, "subscriptions", 0),
)
BOT_FOREIGN_KEY_PARENT_KEYS = {
    ("payments", 57, "subscriptions", 0): (142,),
    ("proxy_accounts", 132, "subscriptions", 0): (140,),
    ("recurring_payment_profiles", 3, "subscriptions", 1): (145,),
    ("recurring_payment_profiles", 3, "users", 2): (31,),
    ("referral_earnings", 1, "payments", 0): (9,),
    ("subscription_notification_logs", 196, "subscriptions", 0): (72,),
    ("subscription_notification_logs", 202, "subscriptions", 0): (72,),
    ("subscription_notification_logs", 210, "subscriptions", 0): (72,),
    ("subscription_notification_logs", 220, "subscriptions", 0): (72,),
    ("subscription_notification_logs", 230, "subscriptions", 0): (140,),
    ("subscription_notification_logs", 231, "subscriptions", 0): (140,),
}
RUNTIME_NAMES = {
    "logs", "backups", "runtime", "cache", ".cache", "tmp", "data", "db", "database",
    "storage", "uploads", "chroma_db", ".venv", "venv", ".git", ".webstore-profile-locks",
}
FINANCIAL_COLUMNS = {
    "users": ["id", "telegram_id", "balance_rub", "referral_balance", "balance_mode_enabled", "balance_autodebit_enabled", "daily_charge_tariff_id"],
    "subscriptions": ["id", "user_id", "status", "expires_at", "billing_mode", "tariff_id", "tariff_days", "tariff_months"],
    "payments": ["id", "user_id", "subscription_id", "status", "amount", "currency", "method", "discount_applied", "provider_payment_id"],
    "balance_topups": None,
    "balance_transactions": None,
    "web_balance_accounts": ["profile_token", "balance_rub", "total_earned_rub", "total_spent_rub", "ref_code", "balance_mode_enabled", "balance_autodebit_enabled"],
    "web_balance_transactions": None,
    "web_balance_transfers": None,
    "web_balance_topups": None,
    "web_orders": ["id", "order_id", "profile_token", "status", "amount_rub", "original_amount_rub", "bonus_applied_rub", "bonus_spent_at", "paid_at", "yookassa_payment_id", "cashback_status", "cashback_amount_rub", "cashback_processed_at", "referral_status", "referral_credited_at"],
}


def require(condition, message):
    if not condition:
        raise SafetyError(message)


def readonly(path):
    path = Path(path).resolve(strict=True)
    require(path.is_file(), "SQLite database is not a regular file")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    connection.execute("PRAGMA query_only=ON")
    return connection


def tables(connection):
    return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def columns(connection, table):
    return [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]


def foreign_key_details(connection):
    details = []
    for table, rowid, parent, fkid in connection.execute("PRAGMA foreign_key_check").fetchall():
        foreign_keys = [
            row for row in connection.execute(f'PRAGMA foreign_key_list("{table}")') if row[0] == fkid
        ]
        foreign_keys.sort(key=lambda row: row[1])
        child_keys = ()
        if rowid is not None and foreign_keys:
            columns_list = [row[3] for row in foreign_keys]
            child = connection.execute(
                "SELECT " + ",".join(f'"{column}"' for column in columns_list)
                + f' FROM "{table}" WHERE rowid=?',
                (rowid,),
            ).fetchone()
            if child is not None:
                child_keys = tuple(child)
        details.append({"violation": (table, rowid, parent, fkid), "child_keys": child_keys})
    return details


def validate_foreign_key_violations(kind, violations, allowlist=None, actual_parent_keys=None, expected_parent_keys=None):
    actual = sorted(tuple(row) for row in violations)
    if allowlist is None:
        require(not actual, f"{kind}: foreign key violation")
        return actual
    expected = sorted(tuple(row) for row in allowlist)
    require(actual == expected, f"{kind}: foreign key violations differ from the approved historical baseline")
    if expected_parent_keys is not None:
        require(actual_parent_keys is not None, f"{kind}: foreign key baseline metadata is unavailable")
        for violation, expected_keys in expected_parent_keys.items():
            require(tuple(violation) in actual, f"{kind}: approved foreign key baseline row is missing")
            require(tuple(expected_keys) == tuple(actual_parent_keys.get(tuple(violation), ())), f"{kind}: foreign key baseline parent key changed")
    return actual


def normalize_predicate(value):
    parts = re.split(r"('(?:''|[^'])*')", value)
    return ''.join(part if index % 2 else re.sub(r'[\s()"`\[\]]', '', part).lower() for index, part in enumerate(parts))


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_database(path, kind, require_indexes=False, foreign_key_allowlist=None, foreign_key_parent_keys=None):
    with contextlib.closing(readonly(path)) as connection:
        require(connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)], f"{kind}: integrity_check failed")
        details = foreign_key_details(connection)
        foreign_key_violations = validate_foreign_key_violations(
            kind,
            [item["violation"] for item in details],
            foreign_key_allowlist,
            actual_parent_keys={item["violation"]: item["child_keys"] for item in details},
            expected_parent_keys=foreign_key_parent_keys,
        )
        existing = tables(connection)
        required = {"users", "balance_topups"} if kind == "bot" else {"web_orders", "web_accounts", "web_balance_accounts", "web_balance_transactions", "web_profile_links"}
        require(required <= existing, f"{kind}: required tables missing")
        duplicate_queries = [
            ("balance_topups", ["provider_payment_id"], "SELECT 1 FROM balance_topups WHERE provider_payment_id IS NOT NULL GROUP BY provider_payment_id HAVING count(*) > 1 LIMIT 1"),
            ("referral_payment_logs", ["payment_id"], "SELECT 1 FROM referral_payment_logs WHERE payment_id IS NOT NULL GROUP BY payment_id HAVING count(*) > 1 LIMIT 1"),
            ("referral_earnings", ["payment_id"], "SELECT 1 FROM referral_earnings WHERE payment_id IS NOT NULL GROUP BY payment_id HAVING count(*) > 1 LIMIT 1"),
            ("partner_earnings", ["payment_id", "depth"], "SELECT 1 FROM partner_earnings WHERE payment_id IS NOT NULL GROUP BY payment_id, depth HAVING count(*) > 1 LIMIT 1"),
            ("balance_transactions", ["kind", "source_type", "source_id"], "SELECT 1 FROM balance_transactions WHERE kind = 'CASHBACK' AND source_type = 'payment' AND source_id IS NOT NULL GROUP BY source_type, source_id HAVING count(*) > 1 LIMIT 1"),
        ] if kind == "bot" else [
            ("web_balance_transfers", ["status", "profile_token"], "SELECT 1 FROM web_balance_transfers WHERE status='pending' GROUP BY profile_token HAVING count(*) > 1 LIMIT 1"),
            ("web_balance_transactions", ["kind", "source_id"], "SELECT 1 FROM web_balance_transactions WHERE kind='balance_migration' AND source_id IS NOT NULL GROUP BY source_id HAVING count(*) > 1 LIMIT 1"),
        ]
        for table, req_cols, query in duplicate_queries:
            if table in existing and set(req_cols) <= set(columns(connection, table)):
                require(not connection.execute(query).fetchone(), f"{kind}: ambiguous financial duplicates in {table}")
        missing = []
        for name, (table, expected_columns, predicate) in INDEXES[kind].items():
            row = connection.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone()
            if row is None:
                missing.append(name)
                continue
            index = next((item for item in connection.execute(f'PRAGMA index_list("{table}")') if item[1] == name), None)
            require(index is not None and index[2] == 1 and index[4] == 1, f"{kind}: index {name} must be unique and partial")
            actual_columns = [item[2] for item in connection.execute(f'PRAGMA index_info("{name}")')]
            require(actual_columns == expected_columns, f"{kind}: wrong index columns for {name}")
            parts = re.split(r'\bWHERE\b', row[0] or '', flags=re.I)
            require(len(parts) == 2 and normalize_predicate(parts[1]) == normalize_predicate(predicate), f"{kind}: wrong index predicate for {name}")
        require(not require_indexes or not missing, f"{kind}: missing critical financial indexes")
        return {"missing_indexes": missing, "foreign_key_violations": foreign_key_violations}


def digest_table(connection, table, selected, where=""):
    info = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    selected = [name for name in selected if name in {row[1] for row in info}]
    primary = [row[1] for row in sorted(info, key=lambda item: item[5]) if row[5]]
    ordering = primary or selected
    query = 'SELECT ' + ','.join(f'"{name}"' for name in selected) + f' FROM "{table}" ' + where + ' ORDER BY ' + ','.join(f'"{name}"' for name in ordering)
    digest = hashlib.sha256()
    count = 0
    for row in connection.execute(query):
        digest.update(json.dumps(row, ensure_ascii=True, separators=(',', ':'), default=str).encode() + b'\n')
        count += 1
    return {"columns": selected, "count": count, "sha256": digest.hexdigest()}


def financial_snapshot(path, template=None):
    with contextlib.closing(readonly(path)) as connection:
        existing = tables(connection)
        result = {}
        for table, selected in FINANCIAL_COLUMNS.items():
            if template is not None and table not in template:
                if table in existing:
                    require(connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] == 0, "New financial table contains unexpected operations")
                continue
            if table not in existing:
                require(template is None, "Financial table disappeared")
                continue
            selected = template[table]["columns"] if template is not None else (selected or columns(connection, table))
            if template is None:
                selected = [name for name in selected if name in columns(connection, table)]
            require(set(selected) <= set(columns(connection, table)), "Financial columns disappeared")
            result[table] = digest_table(connection, table, selected)
        return result


def daily_snapshot(path, kind, active_only=False, configuration_only=False):
    table, key = ("users", "id") if kind == "bot" else ("web_balance_accounts", "profile_token")
    with contextlib.closing(readonly(path)) as connection:
        names = columns(connection, table)
        selected = [key, "balance_mode_enabled", "balance_autodebit_enabled", "daily_charge_tariff_id"]
        if not configuration_only:
            selected += ["balance_grace_until", "last_daily_charge_at", "next_daily_charge_at", "balance_warning_for_charge_at"]
        selected = [name for name in selected if name in names]
        return digest_table(connection, table, selected, "WHERE balance_mode_enabled=1 OR balance_autodebit_enabled=1" if active_only else "")


def schema_snapshot(path):
    with contextlib.closing(readonly(path)) as connection:
        return {table: connection.execute(f'PRAGMA table_info("{table}")').fetchall() for table in sorted(tables(connection)) if not table.startswith('sqlite_')}


def schema_is_additive(before, path):
    after = schema_snapshot(path)
    for table, old_columns in before.items():
        if table not in after:
            return False
        current = {row[1]: row for row in after[table]}
        for row in old_columns:
            if row[1] not in current or tuple(row[1:]) != tuple(current[row[1]][1:]):
                return False
        old_names = {row[1] for row in old_columns}
        for row in after[table]:
            if row[1] not in old_names and (row[5] or (row[3] and row[4] is None)):
                return False
    return True


def coordinated_backup(paths, destination):
    paths = {kind: Path(path).resolve(strict=True) for kind, path in paths.items()}
    require(len(paths) == 2 and set(paths) == {"bot", "web"}, "Both production databases are required")
    require(not os.path.samefile(paths['bot'], paths['web']), "Bot and Webstore must use distinct SQLite files")
    destination = Path(destination)
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.chmod(destination, 0o700)
    with contextlib.ExitStack() as stack:
        for path in sorted(paths.values()):
            guard = stack.enter_context(contextlib.closing(sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=5)))
            guard.execute('BEGIN IMMEDIATE')
        result = {}
        for kind, path in paths.items():
            target = destination / f'{kind}.sqlite3'
            source = stack.enter_context(contextlib.closing(readonly(path)))
            with contextlib.closing(sqlite3.connect(target)) as backup:
                source.backup(backup)
                require(backup.execute('PRAGMA integrity_check').fetchall() == [('ok',)], "Backup integrity check failed")
            os.chmod(target, 0o600)
            result[kind] = {
                "path": str(path), "backup": str(target), "sha256": file_sha256(target),
                "financial": financial_snapshot(path), "daily": daily_snapshot(path, kind), "active_daily": daily_snapshot(path, kind, active_only=True, configuration_only=True), "schema": schema_snapshot(path),
            }
        return result


def protected(relative, root, database_paths=()):
    relative = Path(relative)
    if relative.parts and relative.parts[0] in RUNTIME_NAMES:
        return True
    nested_runtime = RUNTIME_NAMES - {'data', 'db', 'database', 'storage'}
    if any(part in nested_runtime or (part.startswith('.env') and part not in {'.env.example', '.env.deploy.example'}) for part in relative.parts):
        return True
    if re.search(r'\.(db|sqlite|sqlite3)([-.].*)?$', relative.name) or relative.name.endswith('.log') or 'checkpoint' in relative.name:
        return True
    lexical = Path(root) / relative
    for db in database_paths:
        for suffix in ('', '-wal', '-shm', '-journal'):
            target = Path(str(db) + suffix)
            if lexical == target or lexical.resolve() == target.resolve():
                return True
    return False


def code_paths(root, database_paths=()):
    root = Path(root)
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in list(dirs):
            relative = (Path(directory) / name).relative_to(root)
            if protected(relative, root, database_paths):
                dirs.remove(name)
            elif (root / relative).is_symlink():
                dirs.remove(name)
                yield relative
        for name in files:
            relative = (Path(directory) / name).relative_to(root)
            if not protected(relative, root, database_paths):
                yield relative


def backup_code(root, destination, database_paths=()):
    with tarfile.open(destination, 'w:gz') as archive:
        for relative in code_paths(root, database_paths):
            require(not (Path(root) / relative).is_symlink(), "Unprotected code symlinks prevent safe rollback")
            archive.add(Path(root) / relative, arcname=str(relative), recursive=False)
    os.chmod(destination, 0o600)


def clear_code(root, database_paths=()):
    for relative in list(code_paths(root, database_paths)):
        (Path(root) / relative).unlink()


def validate_archive(archive_path, root, database_paths=()):
    with tarfile.open(archive_path, 'r:gz') as archive:
        members = archive.getmembers()
        for member in members:
            relative = Path(member.name)
            require(not relative.is_absolute() and '..' not in relative.parts, "Unsafe archive path")
            require(member.isdir() or member.isfile(), "Archive links and special files are forbidden")
            require(not protected(relative, root, database_paths), "Archive attempts to overwrite protected runtime files")
            target = Path(root) / relative
            require(not target.is_symlink() and all(not parent.is_symlink() for parent in target.parents if parent == Path(root) or Path(root) in parent.parents), "Archive traverses a symlink")
        return members


def install_archive(archive_path, root, database_paths=()):
    validate_archive(archive_path, root, database_paths)
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive.getmembers():
            target = Path(root) / member.name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, target.open('wb') as destination:
                    shutil.copyfileobj(source, destination)
                target.chmod(member.mode & 0o755)


def lock_inventory(root):
    result = {}
    root = Path(root)
    for directory, dirs, files in os.walk(root, followlinks=False):
        if '.webstore-profile-locks' not in Path(directory).parts:
            continue
        for name in files:
            path = Path(directory) / name
            require(not path.is_symlink(), "Symlink in flock directory")
            stat = path.stat()
            result[str(path)] = [stat.st_dev, stat.st_ino]
    return result


def verify_locks(inventory):
    for name, identity in inventory.items():
        path = Path(name)
        require(path.is_file() and not path.is_symlink(), "Profile flock file was removed or replaced")
        stat = path.stat()
        require([stat.st_dev, stat.st_ino] == identity, "Profile flock inode changed")


def assert_locks_idle(directory):
    for path in Path(directory).iterdir():
        require(path.is_file() and not path.is_symlink(), "Unexpected object in flock directory")
        with path.open('r+b') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SafetyError("A profile flock is still held by a writer") from None


def verify_preserved(paths, snapshots):
    for kind, path in paths.items():
        require(financial_snapshot(path, snapshots[kind]['financial']) == snapshots[kind]['financial'], f'{kind}: financial state changed during deployment')
        require(daily_snapshot(path, kind) == snapshots[kind]['daily'], f'{kind}: daily settings changed during deployment')
