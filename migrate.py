import sqlite3
import argparse

def migrate(db_path):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # 1. Add device_slots to subscriptions
    try:
        cursor.execute("ALTER TABLE subscriptions ADD COLUMN device_slots INTEGER NOT NULL DEFAULT 1")
        print("✅ Added 'device_slots' to subscriptions")
    except sqlite3.OperationalError as e:
        print(f"⚠️ subscriptions.device_slots: {e}")

    # 2. Add extra_device_price_rub to bot_settings
    try:
        cursor.execute("ALTER TABLE bot_settings ADD COLUMN extra_device_price_rub INTEGER NOT NULL DEFAULT 50")
        print("✅ Added 'extra_device_price_rub' to bot_settings")
    except sqlite3.OperationalError as e:
        print(f"⚠️ bot_settings.extra_device_price_rub: {e}")

    # 3. Add extra_device_price_stars to bot_settings
    try:
        cursor.execute("ALTER TABLE bot_settings ADD COLUMN extra_device_price_stars INTEGER NOT NULL DEFAULT 25")
        print("✅ Added 'extra_device_price_stars' to bot_settings")
    except sqlite3.OperationalError as e:
        print(f"⚠️ bot_settings.extra_device_price_stars: {e}")

    # 4. Add max_devices_per_sub to bot_settings
    try:
        cursor.execute("ALTER TABLE bot_settings ADD COLUMN max_devices_per_sub INTEGER NOT NULL DEFAULT 3")
        print("✅ Added 'max_devices_per_sub' to bot_settings")
    except sqlite3.OperationalError as e:
        print(f"⚠️ bot_settings.max_devices_per_sub: {e}")

    # 5. Create proxy_accounts table if not exists
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS proxy_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                server_id INTEGER NOT NULL REFERENCES servers(id),
                marzban_username VARCHAR(128) NOT NULL,
                sub_url TEXT NOT NULL,
                device_limit INTEGER NOT NULL DEFAULT 1,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, server_id, marzban_username)
            )
        """)
        print("✅ Created 'proxy_accounts' table")
    except sqlite3.OperationalError as e:
        print(f"⚠️ proxy_accounts: {e}")

    # 6. Add extra_devices to payments for tracking what payment was for
    try:
        cursor.execute("ALTER TABLE payments ADD COLUMN extra_devices INTEGER NOT NULL DEFAULT 0")
        print("✅ Added 'extra_devices' to payments")
    except sqlite3.OperationalError as e:
        print(f"⚠️ payments.extra_devices: {e}")

    # 7. Add tariff_type to tariffs (vpn/tg_proxy/both)
    try:
        cursor.execute("ALTER TABLE tariffs ADD COLUMN tariff_type VARCHAR(16) NOT NULL DEFAULT 'VPN'")
        print("✅ Added 'tariff_type' to tariffs")
    except sqlite3.OperationalError as e:
        print(f"⚠️ tariffs.tariff_type: {e}")

    # 7c. Add adapt_plan_uuid to tariffs (Adapt Group integration)
    try:
        cursor.execute("ALTER TABLE tariffs ADD COLUMN adapt_plan_uuid VARCHAR(64)")
        print("✅ Added 'adapt_plan_uuid' to tariffs")
    except sqlite3.OperationalError as e:
        print(f"⚠️ tariffs.adapt_plan_uuid: {e}")

    # 7d. Add vhq_tier to tariffs (explicit VHQ integration)
    try:
        cursor.execute("ALTER TABLE tariffs ADD COLUMN vhq_tier VARCHAR(16)")
        print("✅ Added 'vhq_tier' to tariffs")
    except sqlite3.OperationalError as e:
        print(f"⚠️ tariffs.vhq_tier: {e}")

    # 7b. Add tariff_days to subscriptions for arbitrary durations
    try:
        cursor.execute("ALTER TABLE subscriptions ADD COLUMN tariff_days INTEGER NOT NULL DEFAULT 0")
        print("✅ Added 'tariff_days' to subscriptions")
    except sqlite3.OperationalError as e:
        print(f"⚠️ subscriptions.tariff_days: {e}")

    # 8. Create mtproto_accounts table
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS mtproto_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                subscription_id INTEGER REFERENCES subscriptions(id),
                secret VARCHAR(64) NOT NULL UNIQUE,
                label VARCHAR(128) NOT NULL UNIQUE,
                is_active BOOLEAN NOT NULL DEFAULT 1,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        print("✅ Created 'mtproto_accounts' table")
    except sqlite3.OperationalError as e:
        print(f"⚠️ mtproto_accounts: {e}")

    # 9. Add recurring payment fields to recurring_payment_profiles
    for col, col_type in [
        ("tariff_id", "INTEGER REFERENCES tariffs(id)"),
        ("payment_attempt_count", "INTEGER NOT NULL DEFAULT 0"),
        ("last_payment_attempt", "DATETIME"),
    ]:
        try:
            cursor.execute(f"ALTER TABLE recurring_payment_profiles ADD COLUMN {col} {col_type}")
            print(f"✅ Added '{col}' to recurring_payment_profiles")
        except sqlite3.OperationalError as e:
            print(f"⚠️ recurring_payment_profiles.{col}: {e}")

    # Multi-level referral chain tracking
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN referral_root_partner_id INTEGER REFERENCES partners(id)")
        print("✅ Added 'referral_root_partner_id' to users")
    except sqlite3.OperationalError as e:
        print(f"⚠️ users.referral_root_partner_id: {e}")

    try:
        cursor.execute("ALTER TABLE users ADD COLUMN referral_depth INTEGER NOT NULL DEFAULT 0")
        print("✅ Added 'referral_depth' to users")
    except sqlite3.OperationalError as e:
        print(f"⚠️ users.referral_depth: {e}")

    # Back-fill referral_root_partner_id / referral_depth for existing direct-partner users
    cursor.execute("""
        UPDATE users
        SET referral_root_partner_id = partner_id,
            referral_depth = 1
        WHERE partner_id IS NOT NULL
          AND referral_root_partner_id IS NULL
    """)
    print(f"✅ Back-filled referral chain for {cursor.rowcount} existing partner users")

    conn.commit()
    conn.close()
    print("Migration complete!")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=str, required=True, help="Path to sqlite DB")
    args = parser.parse_args()
    migrate(args.db)
